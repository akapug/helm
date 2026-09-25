#!/usr/bin/env python3
"""Durable dispatch obligations with immutable, write-time-resolved rows.

Shipping state is deliberately small:

* ``dispatch`` opens one exact-tip obligation (persisted before delivery),
* ``delivered`` records that the local DM call returned a row,
* ``verdict`` closes an obligation by naming the exact reviewed tip,
* ``cancel`` honestly ABANDONS an open obligation with a reason (no reviewed
  tip) — the truthful terminal when a verdict will never come (recipient gone,
  work moot), so a stranded row need never be closed by a false verdict,
* ``discharge`` annotates a contrary FIX/SUPERSEDE verdict after landreq proves
  a later approved resolution; it retires debt without rewriting the verdict,
* ``withdraw`` annotates a FIX/SUPERSEDE verdict after landreq proves the change
  stayed off trunk,
* ``close-landed`` annotates an UNDECLARED verdict after landreq independently
  proves its reviewed change landed; polarity remains immutable and undeclared,
* ``abandon`` writes off reviewed work only after its exact commit is MISSING and
  tip, trunk-message, lane-branch, and registered-worktree interlocks all clear;
  land state remains UNKNOWN.

``verdict`` and ``cancel`` are terminal and mutually exclusive. The four strict
post-verdict annotations preserve the verdict and record distinct terminal facts;
every other later event remains inert.
No ACK, retarget, bind, replay-time branch/worktree lookup, automatic retry, or
cross-crash exactly-once claim. Ambiguous delivery remains open as NEEDS
CONFIRMATION; retrying the same operation never sends again. Deadlines are
advisory NEEDS CHECK-IN only.
"""
import calendar
import collections.abc
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata

from . import (carriageckpt, eventledger, foldckpt, freetext, gate, home, pk,
               projscope, rebind_liveness, spiral_findings)
from .verdicts import (POLARITIES, BASES, BASIS_FLAGS, clean_basis,
                       replay_basis, WORK_POLARITIES as _WORK_POLARITIES)

LEDGER = "dispatches.jsonl"
DEFAULT_DEADLINE_S = 2700        # a REVIEW: minutes of reading and a verdict
# A BUILD is not a review, and the same clock over both is what made `overdue`
# cry wolf. 4h is not a new number — `helm/work/_common.py` already answers
# "how long is a build lane" with DEFAULT_TTL = 4 * 3600 ("a build lane, not a
# chat lock"), and a dispatch whose work outlives the lease guarding that work
# is the first honest moment to call it late. Kept as a literal rather than an
# import: dispatches is below work in the stack, and one constant crossing that
# seam for a default would invert the dependency for no gain.
BUILD_DEADLINE_S = 4 * 3600
MAX_DEADLINE_S = 31 * 24 * 60 * 60


def default_deadline_s(kind):
    """Seconds a row of this KIND may run before the clock calls it overdue.

    UNKNOWN keeps the review default deliberately. Legacy rows carry no kind
    (the field postdates them), and silently tripling their deadline would
    change what "overdue" means for a whole cohort on the strength of a guess.
    The progress signal below covers the case that actually matters — a row
    being worked right now — without needing to know what kind it is."""
    return BUILD_DEADLINE_S if str(kind or "").strip().lower() == "build" \
        else DEFAULT_DEADLINE_S
# The reduced core landed 2026-07-22 ~19:22Z; the newest legacy row on the
# live ledger is 09:13Z. Compat branches replay ONLY rows stamped before this
# boundary, so an event appended after landing can never drive the removed
# machinery — replay distinguishes an old retarget from a fresh one by when
# it claims to have been written (fail-closed: a missing or NON-STRING ts is never compat — the
# same type-corruption guard seq gets).
LEGACY_COMPAT_BOUNDARY = "2026-07-22T12:00:00Z"
_ID = re.compile(r"[0-9a-f]{8,64}\Z")
_TIP = re.compile(r"[0-9a-f]{40,64}\Z")
_FULL_TIP = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_WORSE_THAN_MAIN_FLAG = "--worse-than-main"
_EXIT_QUESTION_POLARITIES = ("fix", "supersede")
_FINDING_FLAGS = ("--finding-count", "--prior-relation")
_FINDING_FIELDS = ("finding_count", "prior_relation")
PRIOR_RELATIONS = ("regression-of-cure", "uncured", "new")

#: THE REVIEWER'S OWN CURE, RECORDED AS CO-AUTHOR WORK. A reviewer of either
#: family who finds a MECHANICAL defect patches it in their own worktree on a
#: branch off the exact reviewed tip, commits there, does not push, and
#: names the tip here. The lane owner or the integrator rebases the
#: lane onto that tip or cherry-picks it. A DESIGN finding is not this: it goes
#: to a meld, because a design disagreement settled by one side's patch is the
#: disagreement unrecorded. The row therefore carries TWO names — the lane's
#: author and the reviewer who wrote part of the tree — and `lr close --reason
#: landed` credits both.
_PATCH_TIP_FLAG = "--patch-tip"
_PATCH_FIELDS = ("patch_tip", "patch_author")
#: THE RECORDED REASON A FIX CARRIES NO CURE. A mechanical finding a reader
#: can patch is patched; a DESIGN finding is not, and that is a real answer
#: which must be recordable rather than merely permitted by silence. So a FIX
#: with no `--patch-tip` states why in one argv token, and the row keeps it.
_NO_PATCH_BECAUSE_FLAG = "--no-patch-because"
_VALUED_VERDICT_FLAGS = ((_WORSE_THAN_MAIN_FLAG, _PATCH_TIP_FLAG,
                          _NO_PATCH_BECAUSE_FLAG) + _FINDING_FLAGS
                         + ("--reviewer-model", "--reviewer-run",
                            "--author-model"))

#: THE READER FIXES WHAT IT FINDS, and a brief that forbids editing forbids
#: the cure. The phrase set is CLOSED and spelled the way senders spell it;
#: a brief carrying one owes `--read-only-because REASON`, which the row
#: records so a later reader can tell "decided" from "never considered".
#: EVERY PHRASE IS A DIRECTIVE TO THE READER, never the bare words. In this
#: ledger "read-only" is overwhelmingly a description of how something was
#: MEASURED ("against the live ledger, read-only") or plain grammar ("the
#: composer read only id and lane"), and a door that refuses a sender for
#: describing a measurement teaches it to pass the escape flag without
#: reading it. Bare "do not edit" is left out for the
#: same reason: half its real uses say "do not edit MY worktree, make your
#: own checkout", which is exactly where a reader's cure belongs.
#: KNOWN LIMIT: a directive spelled some other way passes, and the procedure
#: line every review send prints is what reaches that sender.
READ_ONLY_PHRASES = (
    "read-only review", "read-only read", "read-only audit",
    "read-only refute", "read-only re-refute", "review is read-only",
    "source read only", "do not edit source", "do not edit anything",
    "do not edit or commit", "do not commit", "do not patch", "do not fix",
    "no edits", "report only", "findings only")
_READ_ONLY_BECAUSE_FLAG = "--read-only-because"
_WORD = re.compile(r"[a-z0-9]+")

#: The one sentence every review send prints, whatever else it says.
REVIEW_READER_FIXES_LINE = (
    "the reader COMMITS its cure on a branch off the exact tip it read and "
    "returns FIX with --patch-tip; the author reviews that patch; agreement "
    "on the patch is what lands the chain")


def _word_tokens(text):
    return _WORD.findall(str(text or "").lower())


def _phrase_index(phrases):
    """token sequence -> the FIRST declared spelling that produces it.

    Two spellings of one phrase ("read-only", "read only") tokenize
    identically on purpose, so the index reports one hit under the first
    spelling rather than the same finding twice.
    """
    out = {}
    for phrase in phrases:
        out.setdefault(tuple(_word_tokens(phrase)), phrase)
    return out


_READ_ONLY_SEQS = _phrase_index(READ_ONLY_PHRASES)


def read_only_hits(text):
    """The declared DO-NOT-EDIT phrases `text` carries, in declaration order.

    Matched on WORD TOKENS, never on raw substrings: "thread only" carries the
    bytes of "read only" and says nothing at all about editing.
    """
    toks = _word_tokens(text)
    hits = []
    for seq, phrase in _READ_ONLY_SEQS.items():
        n = len(seq)
        if any(tuple(toks[i:i + n]) == seq
               for i in range(len(toks) - n + 1)) and phrase not in hits:
            hits.append(phrase)
    return hits


def read_only_refusal(verb, hits):
    """The door's refusal text: what tripped it, why, and the one escape."""
    return ("%s: this REVIEW brief tells the reader not to edit — %s — and a "
            "reader that cannot edit cannot cure what it finds.\n"
            "  %s\n"
            "  If the reader really must not touch this tree, pass "
            "--read-only-because REASON (ONE argv token — quote it) and the "
            "row records why."
            % (verb, "; ".join("%r" % hit for hit in hits),
               REVIEW_READER_FIXES_LINE))


def check_read_only(verb, text, kind=None, because=None):
    """-> None when the door may proceed, else the refusal text.

    Only a REVIEW brief is asked: a build brief that says "report only" is
    describing its own deliverable, not disarming a reviewer.
    """
    if str(kind or "").strip().lower() != "review":
        return None
    if str(because or "").strip():
        return None
    hits = read_only_hits(text)
    if not hits:
        return None
    return read_only_refusal(verb, hits)


def _stamp_read_only(row, because):
    """THE RECORDED ESCAPE for a review brief that forbids editing.

    Present only when the sender gave one, exactly like `posture_na` beside
    every call site: the row shape of every other dispatch is unchanged, and a
    reader can tell "asked and answered" from "never asked".
    """
    if str(because or "").strip():
        row["read_only_because"] = str(because).strip()
    return row


def _finding_error(count, relation):
    """Typed reviewer observations, never numbers extracted from evidence."""
    if count is not None and (type(count) is not int or not 0 <= count <= 999999999):
        return "finding_count must be a non-negative integer (not bool), at most 9 digits"
    if relation is not None and relation not in PRIOR_RELATIONS:
        return "prior_relation must be regression-of-cure|uncured|new"
    if relation is not None and count is None:
        return "prior_relation requires finding_count"
    return None



def partition_verdict_flags(rest):
    """`verdict <id> <tip> [flags...] [--] <evidence...>` -> (flags, evidence).

    THE ONE OWNER OF THIS SPLIT, and it is one because the tests were a COPY.
    The arms proving the positional cure re-implemented this loop in the test
    file, so they exercised a reimplementation and never the shipped parser —
    they would have passed on a broken verb and, measured, they FAILED on a
    fixed one. A parser worth three rounds of findings is worth not having
    two of.

    id and tip are POSITIONAL; flags are the contiguous run after them;
    everything from the first non-flag token on is evidence VERBATIM. A
    positionless scan reached into the free-text tail and MINTED a basis from
    a reviewer's prose while mutilating the sentence that carried it.

    A BARE `--` ENDS THE OPTIONS AND IS NOT ONE (T1, measured at
    exact tip — the fourth shape, which the positional cure did not reach).
    Positional flags stopped prose from minting a basis and also made evidence
    whose FIRST token is flag-shaped UNREPRESENTABLE: the scan ate it, and the
    conventional escape was eaten too, landing in the polarity bucket as an
    unknown polarity and refusing with rc 2. Only the first bare `--` while
    still scanning flags is punctuation; one inside the evidence is a word the
    reviewer wrote and survives verbatim.
    """
    cut, flags = 2, []                     # id and tip are positional
    while cut < len(rest) and rest[cut].startswith("--"):
        flag = rest[cut]
        if flag == "--":                   # end-of-options, not an option
            break
        flags.append(flag)
        cut += 1
        # The blocking answer names one project-relative path. Its value is
        # still part of the contiguous option prefix; without this one
        # value-aware arm the first path becomes evidence and every later flag
        # is silently swallowed with it.
        if flag in _VALUED_VERDICT_FLAGS and cut < len(rest) \
                and not rest[cut].startswith("--"):
            flags.append(rest[cut])
            cut += 1
    tail = rest[cut:]
    if tail and tail[0] == "--":
        tail = tail[1:]                     # the terminator is not evidence
    return flags, tail


def _clean_worse_than_main_paths(values):
    """One or more named, project-relative paths for the blocking claim."""
    if values is None:
        return (), None
    if not isinstance(values, (list, tuple)):
        return None, "worse-than-main paths must be a list of named paths"
    out = []
    for value in values:
        path, err = _clean(value, "worse-than-main path", 256)
        if err:
            return None, err
        parts = path.replace("\\", "/").split("/")
        if os.path.isabs(path) or re.match(r"[A-Za-z]:[\\/]", path) \
                or path == "." or ".." in parts:
            return None, ("worse-than-main path must be project-relative and "
                          "must not traverse upward (got %r)" % path)
        if path not in out:
            out.append(path)
    return tuple(out), None


#: The two answers a FIX/SUPERSEDE verdict may give the exit question.
#: WORSE-THAN-MAIN blocks the tip it names. IMPERFECT does not block anything
#: — it exists for the read that found the tip no worse than main AND cured
#: something anyway, so the patch has a door instead of travelling by chat.
EXIT_ANSWERS = ("worse-than-main", "imperfect")


def verdict_exit_answer(row):
    """Human projection of the optional durable exit-question answer.

    Absence and malformed/future shapes are UNMARKED, never guessed into a
    blocking answer. That keeps pre-cutover verdicts historically honest.
    """
    if not isinstance(row, dict):
        return "UNMARKED"
    answer = row.get("exit_answer")
    if answer not in EXIT_ANSWERS:
        return "UNMARKED"
    if answer == "imperfect":
        # IMPERFECT IS ONLY EVER THE CARRIER OF A CURE, so a row claiming it
        # without a well-formed patch tip claims nothing: it reads UNMARKED,
        # the same fail-closed shape a forged worse-than-main gets.
        tip = str(row.get("patch_tip") or "").strip().lower()
        if not _FULL_TIP.fullmatch(tip):
            return "UNMARKED"
        return ("IMPERFECT, CURED: not worse than main on any touched path; "
                "the reviewer's patch %s awaits the author's agreement"
                % tip[:12])
    paths, err = _clean_worse_than_main_paths(
        row.get("worse_than_main_paths"))
    if err or not paths:
        return "UNMARKED"
    return "WORSE-THAN-MAIN: " + ", ".join(paths)


def _canonical_recipient(value):
    """One typed canonical operand from the seats identity owner."""
    from . import seats
    return seats._canonical_recipient(value)


def _recipient_operand(value):
    from . import seats
    if isinstance(value, seats._CanonicalRecipient):
        return value, None
    canonical, err = seats.resolve_recipient(value)
    if err:
        return None, err
    return seats._canonical_recipient(canonical)


_PROOF_ANCHOR = re.compile(r"[0-9a-f]{32}\Z")
_GATE_ID = re.compile(r"[0-9a-f]{4,32}\Z")   # a minted gate receipt (helm/gate.py)
# THE WRITER'S OWN CAPABILITIES, stamped by name on every verdict this code
# writes. NOT a version integer.
#
# The first design grandfathered by WALL TIME. A probe measured why that cannot
# work: a timestamp does not say whether the writer COULD mint a receipt. The
# boundary instant passed while this feature was still unlanded and trunk still
# ran the old writer, so the real ledger already holds verdicts stamped AFTER
# it that no gate verb existed to serve. Moving the constant just repeats the
# race on the next round.
#
# The SECOND design stamped an integer and asked `>= 1`. The meld that
# replaced round 7 refuted it: a numeric policy answers today and invites
# policy 2 to mean two incompatible things at once — "newer" and "stricter".
# `>= 1` would then silently accept a row that never met the stricter rule.
# NAMED CAPABILITIES cost nothing now and cannot collapse that way: a future
# signed-receipt policy adds "signed-receipt-v1" and the land check asks for
# the exact capability it needs.
#
# THREE STATES, and they must stay distinguishable through replay:
#   field ABSENT      an old writer — the true reading of every row on this
#                     ledger from before the feature
#   recognized SET    apply exactly the requirements those capabilities imply
#   present, MALFORMED  UNKNOWN — never silently demoted to "old writer",
#                     because that would let a corrupted field launder an
#                     ungated approve into READY, which is the lane's own
#                     defect wearing a data-corruption costume
GATE_CAP_RECEIPT = "receipt-v1"
GATE_CAPS = (GATE_CAP_RECEIPT,)
GATE_CAPS_UNKNOWN = "UNKNOWN"
_CAP = re.compile(r"[a-z0-9][a-z0-9.\-]{0,62}\Z")


def clean_gate_caps(value):
    """The replayed form of a `gate_caps` field. -> tuple | GATE_CAPS_UNKNOWN.

    Only a list/tuple of well-formed capability names is recognized. Anything
    else PRESENT is UNKNOWN, never () — an empty tuple would read as a writer
    that honestly had no capabilities, and a corrupted field has not earned
    that reading."""
    if not isinstance(value, (list, tuple)):
        return GATE_CAPS_UNKNOWN
    out = []
    for cap in value:
        if not isinstance(cap, str) or not _CAP.fullmatch(cap):
            return GATE_CAPS_UNKNOWN
        out.append(cap)
    return tuple(out)


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def _clean(value, label, cap):
    value = str(value or "").strip()
    if not value:
        return None, "%s is required" % label
    if len(value) > cap or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
                               for c in value):
        return None, "%s must be one printable line of at most %d characters" % (label, cap)
    return value, None


def _lane_stem(lane):
    """landreq's ONE read-side lane-family normalisation (#142/#156).
    Deferred import: landreq imports this module."""
    from . import landreq
    return landreq._lane_stem(lane)


def _strip_lane_prefix(lane):
    """landreq's ONE write-time lane canonicalizer (#142). Deferred import:
    landreq imports this module."""
    from . import landreq
    return landreq._strip_lane_prefix(lane)


def _valid_trunk_ref(value):
    """Replay-safe named branch/remote ref; never a raw object or tag."""
    ref, err = _clean(value, "landing trunk ref", 512)
    if err or not ref.startswith(("refs/heads/", "refs/remotes/")):
        return None
    if ref.endswith(("/", ".")) or ".." in ref or "@{" in ref \
            or any(c in ref for c in " ~^:?*[\\"):
        return None
    return ref


def _valid_ts(value):
    """True only for the one UTC timestamp shape emitted by ``pk.now_ts``."""
    if not isinstance(value, str):
        return False
    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return value.startswith("20") and time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", parsed) == value


def _deadline(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None, "deadline takes SECONDS"
    if not 1 <= value <= MAX_DEADLINE_S:
        return None, "deadline must be between 1 and %d seconds" % MAX_DEADLINE_S
    return value, None


# Git repository-SELECTION environment: any of these redirects `git -C` away
# from the directory it names. EVERY identity-bearing git call in this module
# runs under the same scrub — _repo_info, _resolve_tip, and _lane_movement —
# because a hostile GIT_DIR at WRITE time would store a foreign repo_id or
# ref_branch, and the read-side guard would then faithfully protect a lie
# (meld e:1785552432: the write side of the mapping is the identity).
_GIT_SELECTION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                      "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                      "GIT_CEILING_DIRECTORIES")


def _git_env():
    return {k: v for k, v in os.environ.items()
            if k not in _GIT_SELECTION_ENV}


def _repo_info(path=None):
    cwd = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    try:
        env = _git_env()
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5, env=env)
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--git-common-dir"], capture_output=True, text=True, timeout=5,
            env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if top.returncode or common.returncode:
        return None
    return {"repo": os.path.realpath(top.stdout.strip()),
            "repo_id": os.path.realpath(common.stdout.strip())}


def home_repo_id():
    """(repo_id, why) — the canonical gitdir of the project THIS helm IS.

    THE CALLER'S CWD IS NOT PROJECT IDENTITY. A seat cd'd into another checkout
    is still running THIS project's helm, so identity comes from the running
    PACKAGE PATH — the law `web_land_model._lr_repo` was written to and now
    delegates here, so the board and the write door cannot disagree.

    DERIVED FROM THE CODE'S OWN LOCATION, NEVER FROM A REBUILDABLE CACHE, and
    that is a security property rather than a simplification. An earlier
    version resolved this through the project REGISTRY, whose own docstring
    calls it "pure PROJECTION, rebuildable from a re-scan". So an absent,
    corrupt or unreadable cache made the write door's authority UNKNOWN — and
    a door that refuses only on a KNOWN mismatch refuses NOTHING while its
    authority is unknown. That was an unlabelled escape reachable by deleting
    a file helm regenerates on demand: the sixteenth foreign row was possible
    precisely while the projection was unreadable, which is the one moment
    nobody is watching. Found by cross-family review, not by my arms, because
    every arm replaced this function with a lambda and so tested the
    comparison while never testing the thing it compares against.

    THE PACKAGE PATH CANNOT BE REBUILT OUT FROM UNDER THE GUARD — it is where
    the code executing this line lives. `--git-common-dir` resolves a worktree
    to its main repository, which is exactly the identity a dispatch row binds.

    A None HERE IS NOT "missing evidence to be waved through". A helm whose own
    package is not inside a Git working tree cannot establish ANY repository
    identity, and every dispatch row is repo-bound by construction, so the
    caller must REFUSE rather than proceed unguarded.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    info = _repo_info(here)
    if not info:
        return None, ("the running helm package at %s is not inside a Git "
                      "working tree, so no repository identity can be "
                      "established" % here)
    return info["repo_id"], None


def _real(path):
    """realpath, or None for anything unusable. ONE SPELLING for the compare
    two repository identities are made on.

    `_repo_info` realpaths what it stamps, so a stored `repo_id` is already
    canonical — but a caller-supplied `--repo`, a symlinked checkout and a
    home resolved through /tmp are not, and comparing those raw is how two
    names for one repository read as two repositories. Never raises: a path
    Python refuses to normalize is UNKNOWN, and every caller here treats
    unknown as "not a match" rather than as an error."""
    try:
        return os.path.realpath(str(path)) if path else None
    except (OSError, ValueError):
        return None


def _project_of(path, snapshot=False):
    """path -> registry project name, or None. THE MAPPER, NEVER A SECOND ONE.

    `landreq._project_of` delegates HERE, and `inject._ledger.project_for_cwd`
    underneath is the same longest-prefix resolution `helm store` and `helm
    task` scope with ("scoping to project '%s' (from cwd)"). So the write
    door's admission, a row's classification and the board's scope cannot be
    answered by three oracles. It takes a GITDIR string as happily as a cwd —
    measured against all seven repo_id values the live ledger carries.

    THE ORDINARY READ FAILS OPEN TO None, AND None IS NEVER "MINE". The write
    door reads None as UNREGISTERED and refuses; `landreq._mark_foreign_rows`
    reads it as `project_unresolved` and discloses it. Neither side adopts an
    unresolvable repository, which is the UNMARKED pattern both ends already
    obey.

    `snapshot=True` READS THE AUTHORITY WITHOUT TOUCHING THE DISK, and the
    write door needs exactly that. An ordinary `registry.load()` takes the
    owner WRITE LOCK when it finds mixed-era authored fields to migrate, and
    taking that lock CREATES `<HELM_HOME>/_global/.state/` — so consulting the
    registry to decide whether a row may be written materialised the ledger's
    own home directory BEFORE any storage-safety check had run on it. Measured
    on a HELM_HOME pointing through a symlink: `_global` appeared under the
    symlink's target and the write was then refused, which is the one ordering
    `eventledger`'s parent-symlink guard exists to prevent. THE BYTES DECIDING
    IT IS WHY THE SNAPSHOT IS NOT OPTIONAL: an ordinary load is silent on a
    registry with nothing left to migrate and writes on the one load that has,
    so a door that relied on the usual case would materialise that tree on
    exactly the run it must not. `registry.load`'s own docstring states the
    contract this uses — "Strict authority snapshots do not mutate and must not
    create even a lock sidecar" — which holds whatever the bytes say, and the
    CLASSIFICATION is byte-identical either way; only the migration and the
    lock are skipped.

    AND `snapshot=True` RAISES WHERE THE ORDINARY READ FAILS OPEN. That is not
    a wart, it is the whole point of a strict authority read — `registry._load`
    validates the population and `inject._ledger.project_for_cwd` re-raises
    under `strict` rather than swallowing — so a malformed, duplicate-keyed or
    unreadable registry, and a project record whose cwd scope is not a list of
    non-empty strings, all arrive here as an EXCEPTION. So "fail open to None"
    describes the ORDINARY read only, and a caller that reads it as the whole
    contract lets the exception escape its own (value, refusal) pair — out of
    `_base` and `add`, where the CLI has a sentence to print and prints a
    traceback instead. Every `snapshot=True` caller therefore owes the raise an
    answer. `write_scope` is the only one: a repository it cannot prove is this
    package's own is refused, naming the registry error
    (`_unreadable_registry_refusal`), and a PROVEN home never calls this
    function at all.

    READ SURFACES KEEP THE ORDINARY LOAD, deliberately. They run against a
    home that already exists, they are the population that carries the
    migration, and flipping every row classification in the fleet to the
    strict validator is a strictness change this lane has no reading for.
    """
    from .inject._ledger import project_for_cwd
    return project_for_cwd(path, strict=True) if snapshot \
        else project_for_cwd(path)


def _unregistered_repo_refusal(repo_id, home_id, home_why=None):
    """The refusal text for a repository NO registered project claims.

    A DOOR SLAM LOSES FINDINGS; A LABELLED HAND-OFF DOES NOT — and the
    hand-off this replaces named a destination that does not exist. "Dispatch
    it from that repository's own helm" reads like a redirection and is not
    one: `home_repo_id` derives identity from the RUNNING PACKAGE PATH, so the
    only way to satisfy that sentence is to put a helm package inside the
    target repository's tree. One project did exactly that — an untracked,
    un-gitignored copy of the whole package at
    <project>-wt/seats/codex-5/.helm-dispatch-runtime/helm — which is how 63
    non-helm rows reached the live ledger a MONTH after the guard landed. A
    refusal whose only repair is a maintenance fork of the CLI inside somebody
    else's worktree is a slam wearing a hand-off's words.

    SO THE TEXT NAMES A REPAIR THE CALLER CAN PERFORM FROM THEIR OWN CHECKOUT.
    Registration is what gives a row a project to be listed, triaged and
    reviewed under, and `helm sync` is the verb that writes the registry
    (`helm projects` manages membership afterwards; there is no `project add`,
    and naming a verb that does not exist is how a refusal becomes a dead end).
    It still names the repository the ref lives in and what this ledger
    answered, because a refusal the reader cannot attribute is a refusal they
    re-run verbatim.
    """
    where = ("this ledger answers for %s" % home_id if home_id else
             "this ledger's own repository identity is UNKNOWN (%s)"
             % (home_why or "unreadable"))
    return ("%s is not a registered helm project, so a row bound to it has no "
            "project to be listed, triaged or reviewed under.\n"
            "Register it — run `helm sync` from a session in that checkout and "
            "confirm with `helm projects` — then dispatch again: %s, and it "
            "holds rows for ANY registered project, each keyed by the "
            "repository its ref lives in." % (repo_id, where))


def _unreadable_registry_refusal(repo_id, exc):
    """The refusal for a FOREIGN ref whose admission the registry cannot answer.

    A STRICT AUTHORITY READ RAISES, AND A DOOR OWES A REFUSAL PAIR (task/2437
    round four). `_project_of(snapshot=True)` is a strict read by construction,
    so a malformed, duplicate-keyed or unreadable registry — or one project
    record whose cwd scope is not a list of non-empty strings — reaches this
    door as an exception rather than as None. Letting it escape made a valid
    `helm dispatch send` traceback out of `_base`/`add` instead of returning
    the (row, why) pair every caller of those functions handles, so the CLI
    printed a stack trace where it had a sentence to print, and `stalebot`'s
    per-row consultation of this same door died on the first row.

    THE POLARITY IS THE ONE THE DOOR IS BUILT ON: an authority that cannot be
    read admits NOTHING. That is already why an unregistered repository is
    refused; an unreadable registry is the same answer with a different repair,
    and the repair is named because a refusal whose cause is hidden gets re-run
    verbatim. The registry ERROR is quoted rather than summarised — "malformed"
    covers duplicate JSON fields, a non-object population and a bad cwd scope,
    which are three different edits to undo.

    THE PROVEN-HOME PATH NEVER REACHES HERE, deliberately. This helm's own
    repository is admitted on an identity this package establishes by itself,
    so a wiped or garbled registry can never lock helm out of its own ledger —
    the guarantee `write_scope`'s second-admission paragraph states, which the
    strict read had quietly taken back.
    """
    return ("%s cannot be placed: this ledger's project registry could not be "
            "read, so whether any project claims that repository is UNKNOWN — "
            "and an unknown authority admits nothing.\n"
            "  registry: %s\n"
            "  error:    %s: %s\n"
            "Repair the file (restore it, or re-run `helm sync` from a session "
            "in a registered checkout to rewrite the projection) and dispatch "
            "again. Refs in this helm's OWN repository are admitted without "
            "the registry and are unaffected."
            % (repo_id, home.registry_path(), type(exc).__name__, exc))


def write_scope(repo_id):
    """(project, why) — MAY this ledger hold a row bound to `repo_id`?

    THE ADMISSION IS REGISTRY MEMBERSHIP, NOT EQUALITY WITH THIS PACKAGE'S OWN
    REPOSITORY (task/2437). The equality door refused every non-helm ref and
    told the caller to run "that repository's own helm", which only exists if
    they copy this package into their tree — see
    `_unregistered_repo_refusal` for the copy that actually appeared and the 63
    rows it wrote. The owner's standing framing: helm exists to help agent
    teams build ANY project, a team USING helm must never need to know how helm
    is made, and a verb that assumes the helm checkout is a bug. An admission
    satisfiable only by installing this package in the caller's tree is that bug
    stated as a remedy.

    THE AXIS IS THE ONE THE LEDGER ALREADY CARRIES. `repo_id` is stamped on
    every create event, and every read surface already groups, filters and
    discloses on it (`landreq._mark_foreign_rows`, `foldcheck._rows_for`,
    `stalebot`'s per-trunk grouping, the gate's per-repo FIFO). Only WHAT this
    door accepts widens: no field, no event shape, no ledger file moves, and
    nothing new is stamped — the project stays DERIVED AT READ so a
    registry-only remap still re-classifies rows with no ledger write.

    THIS DOES NOT REOPEN THE HOLE 4e7767ac5 CLOSED, because the POLARITY is
    inverted. That hole was a door whose authority came from the rebuildable
    registry projection: an unreadable projection made the authority UNKNOWN
    and the door then refused NOTHING. Here an unresolvable project is a
    REFUSAL, so a wiped registry makes this door refuse EVERYTHING — loud, and
    repaired by `helm sync` — rather than admit everything.

    AND THE PACKAGE PATH REMAINS A SECOND ADMISSION so a wiped registry can
    never lock helm out of its own ledger: a ref in the repository this code is
    running from is admitted whatever the registry says.

    A None PROJECT WITH A None `why` IS AN ADMISSION, not an omission: this
    package's own repository is admitted whether or not the registry places it,
    so the caller reads `why` and never the project name. The project is a
    DERIVED label for a refusal and for the read surfaces, never a stored field.

    THE UNKNOWN-HOME CASE IS NOW ANSWERED BY THE REGISTRY, deliberately. A helm
    whose package is not inside a Git working tree can establish no repository
    identity of its own, but a REGISTERED target repo places the row and every
    read surface can scope it — so the row has a home even though this helm
    does not. Only when NEITHER authority can place it is the write refused,
    and the refusal then says the identity was unknown.

    ADMITS A STRICT SUPERSET of the equality door: every ref that door accepted
    was this package's own repository, which the first clause still accepts. No
    row that lands today is refused tomorrow, which is why this needs no
    against-production count of what it newly refuses — it newly refuses
    nothing.
    """
    home_id, home_why = home_repo_id()
    real = _real(repo_id)
    # THE PROVEN HOME IS DECIDED FIRST AND WITHOUT THE REGISTRY. The paragraph
    # above promises a wiped registry can never lock helm out of its own
    # ledger, and the first cut took that promise back by reading the registry
    # for the home row's PROJECT LABEL after the equality had already admitted
    # it: `_project_of(snapshot=True)` is a STRICT read that RAISES on a
    # malformed or unreadable registry, so the exception escaped this pair
    # through `_base` and `add` and a valid own-repository dispatch died where
    # trunk admitted it. The label was discarded by every caller (the docstring
    # above states that None project + None why IS the admission), so it bought
    # nothing and cost the guarantee.
    if home_id and real and real == _real(home_id):
        return None, None
    # THE STRICT NO-WRITE SNAPSHOT SERVES ONLY THE FOREIGN PATH. It runs BEFORE
    # the ledger write, so its registry consultation must not create the home it
    # is about to be refused for — see `_project_of`'s `snapshot` contract. And
    # because it raises rather than failing open, the raise is an ANSWER here:
    # an authority that cannot be read admits nothing, so it becomes a
    # structured refusal naming the registry error rather than a traceback.
    try:
        project = _project_of(real, snapshot=True) if real else None
    except Exception as exc:  # noqa: BLE001 — every strict registry failure is ONE answer: the authority is UNKNOWN, so this door refuses
        return None, _unreadable_registry_refusal(real or repo_id, exc)
    if project:
        return project, None
    return None, _unregistered_repo_refusal(real or repo_id, home_id, home_why)


def cwd_scope(cwd=None):
    """(repo_id, project, why) — WHOSE ROWS a CLI caller standing here means.

    `home_repo_id` answers "which repository is this helm", which is the right
    question for the write door's fallback admission and the WRONG one for
    "whose rows am I looking at". MEASURED from another registered project's
    checkout: the bare `landreq.board_scope()` answered the HELM project while
    `board_scope(<that repo's gitdir>)` answered that project — every read
    surface was already parametric and only its DEFAULT was helm-anchored, so a
    seat working in another project's checkout was shown helm's board with
    nothing saying so.

    THE FALLBACK IS THE OLD ANSWER, NEVER NOTHING. A cwd outside every
    registered project keeps this package's own repository, so a seat standing
    in /tmp sees the board it saw yesterday rather than an empty one — and a
    cwd inside a repository no project claims does too, because an unregistered
    repository has no rows to show (the write door refuses them).

    ONE DOOR FOR EVERY CLI SCOPE, so `helm lr`, `helm dispatch list` and
    `helm dispatch triage` cannot answer differently about one directory —
    `landreq._cli_scope` consumes this rather than re-deriving it.
    """
    info = _repo_info(cwd or os.getcwd())
    if info:
        project = _project_of(info["repo_id"])
        if project:
            return info["repo_id"], project, None
    home_id, why = home_repo_id()
    return home_id, (_project_of(home_id) if home_id else None), why


def _unique_local_tip_branch(repo, tip):
    """One local branch pointing exactly at ``tip``, or None.

    This is OPTIONAL write-boundary evidence. A missing/ambiguous answer or a
    failed probe must never erase the exact commit `_resolve_tip` already
    proved: absence of branch evidence leaves the moved-lane guard unarmed.
    """
    try:
        p = subprocess.run(
            ["git", "-C", repo, "for-each-ref", "--format=%(refname)",
             "--points-at=" + tip, "refs/heads"], capture_output=True,
            text=True, timeout=5, env=_git_env())
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    refs = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    if p.returncode or len(refs) != 1:
        return None
    branch = refs[0]
    return branch if branch.startswith("refs/heads/") \
        and not any(c.isspace() for c in branch) else None


_INFER_REF_BRANCH = object()


def _resolve_tip(repo, ref, infer_sha_branch=True):
    """(tip, branch) — exactly one commit at WRITE time; replay never calls Git.

    ``branch`` is the canonical refs/heads name when the caller names a local
    branch OR a raw SHA points at exactly one local branch tip at this write
    boundary. The moved-lane guard reads ONLY that durable identity: lane is a
    free-text label that never named a branch, and current topology never
    backfills an old row. Zero/multiple SHA matches and probe failure leave the
    binding absent without refusing the exact commit — evidence, never a guess.
    Rows written before the field carry none and stay unanswerable, following
    the --kind precedent.
    """
    ref, err = _clean(ref, "tip", 256)
    if err or not repo or ref.startswith("-"):
        return None, None
    sha_ref = bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", ref))
    candidates, branch = [], None
    env = _git_env()
    try:
        if sha_ref:
            p = subprocess.run(["git", "-C", repo, "rev-parse",
                                "--disambiguate=" + ref.lower()],
                               capture_output=True, text=True, timeout=5,
                               env=env)
            candidates = [x.strip() for x in p.stdout.splitlines() if x.strip()]
            if p.returncode or len(candidates) != 1:
                return None, None
            ref = candidates[0]
        elif ref != "HEAD" and not ref.startswith("refs/"):
            for name in ("refs/heads/" + ref, "refs/tags/" + ref,
                         "refs/remotes/" + ref):
                p = subprocess.run(["git", "-C", repo, "show-ref", "--verify",
                                    "--hash", name], capture_output=True,
                                   text=True, timeout=5, env=env)
                if p.returncode == 0:
                    candidates.append(name)
            if len(candidates) != 1:
                return None, None
            ref = candidates[0]
            if ref.startswith("refs/heads/"):
                branch = ref
        elif ref.startswith("refs/heads/"):
            branch = ref
        p = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--end-of-options",
             ref + "^{commit}"], capture_output=True, text=True, timeout=5,
            env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    lines = p.stdout.splitlines()
    tip = lines[0].strip().lower() if p.returncode == 0 and len(lines) == 1 else ""
    if not _TIP.fullmatch(tip):
        return None, None
    return tip, _unique_local_tip_branch(repo, tip) \
        if sha_ref and infer_sha_branch else branch


def _pre_boundary(ts):
    """True only for an honest pre-boundary timestamp: a non-empty string
    that LOOKS like one (year 2xxx) and sorts before the boundary. Numeric,
    empty, whitespace, and low-sorting garbage ts all fail CLOSED — the r3
    isinstance guard alone let '' through (delta MED), and the r2
    or-tilde let numerics through; the shape check closes the class."""
    return isinstance(ts, str) and ts.startswith("20") \
        and ts < LEGACY_COMPAT_BOUNDARY


def _int_seq(value, fallback):
    """Adopt a row's seq only when it is a real integer — a type-corrupt seq
    must never enter replay state, where int(state.seq)+1 would crash."""
    return value if type(value) is int else fallback   # bool is an int subclass


def _valid_identity(row):
    if not _ID.fullmatch(str(row.get("id") or "")):
        return False
    if not _TOKEN.fullmatch(str(row.get("recipient") or "")):
        return False
    if row.get("seq") is not None and type(row.get("seq")) is not int:
        return False              # type-corrupt seq would crash replay
    lane, err = _clean(row.get("lane"), "lane", 160)
    if err or lane != row.get("lane"):
        return False
    try:
        return 1 <= int(row.get("deadline_s")) <= MAX_DEADLINE_S
    except (TypeError, ValueError):
        return False


def _recipient_fields(row):
    """Canonical projection key + durable display spelling for one row.

    Historical ledgers stored the display spelling in `recipient` itself. Replay
    canonicalizes that identity without rewriting history and retains the old
    spelling unless a newer writer supplied explicit display metadata.
    """
    canonical, err = _canonical_recipient(row.get("recipient"))
    if err:
        return None, None
    display = str(row.get("recipient_display") or row.get("recipient") or "")
    if not _TOKEN.fullmatch(display):
        display = str(row.get("recipient") or canonical)
    return str(canonical), display


def recipient_reach(names):
    """{name: "JOINED"|"ABSENT"|"UNKNOWN"} for a listing's recipients.

    AN OBLIGATION SURFACE THAT NEVER ASKS WHETHER ITS ADDRESSEE EXISTS READS A
    RENAMED SEAT'S ROWS AS FULLY OWNED. The chat send door already asks and
    already prints ABSENT; no listing here did, so a rename orphaned rows
    SILENTLY and every burn-down, stall detector and `--mine` view counted
    them as somebody's work.

    ABSENT AND UNREADABLE DO NOT SHARE A VALUE, for the same reason they do
    not in the integrator resolver: a roster that cannot be read has proved
    nothing about who exists, and rendering that as ABSENT would tell a reader
    the whole estate had been orphaned by one bad file handle. A failed probe
    is UNKNOWN and says so.

    ONE PROBE PER DISTINCT NAME, not per row. A listing can carry dozens of
    rows across a handful of seats, and this runs on the hot list path beside
    a comment promising it stays parse-only.
    """
    out = {}
    for name in {str(n or "").strip() for n in names}:
        if not name:
            continue
        try:
            from . import seats
            cap = seats.recipient_capability(name)
        except Exception:                       # noqa: BLE001 — fail-UNKNOWN
            out[name] = "UNKNOWN"
            continue
        got = str((cap or {}).get("membership") or "").strip().upper()
        # ANY MEMBERSHIP THIS FUNCTION DOES NOT KNOW IS UNKNOWN, NEVER ABSENT.
        # A new membership word added upstream must not be read as proof a
        # seat is gone; the failure direction that matters here is claiming an
        # orphan that is not one.
        out[name] = got if got in ("JOINED", "ABSENT") else "UNKNOWN"
    return out


def _recipient_key(row):
    """The recipient spelling a roster lookup is performed against.

    NOT `_recipient_label`, which prefers `recipient_display` — a display
    spelling exists precisely because it may differ from the key, and asking
    the roster about a display name would answer a different question than
    the one the row poses.
    """
    return str(row.get("recipient") or "").strip()


def _recipient_label(row):
    """The recipient spelling intended for humans, never identity joins."""
    return str(row.get("recipient_display") or row.get("recipient") or "")


def verdict_resolved_model(row):
    """`alias -> provider/upstream (rung)` THIS VERDICT recorded, else "".

    THE RECORD, NOT TODAY'S ROUTE. The reviewer seat may since have been
    relaunched onto another provider; what decides whether a review was a
    cross-family read is the model that answered WHEN IT WAS WRITTEN, and the
    verdict froze that at append time. Reading the seat's current runtime here
    would quietly re-attribute every past review to the present rung.

    A NATIVE AUTHORITY RECORDS NO ROUTE and therefore prints nothing: it has
    no provider and no upstream id to name, and rendering UNRESOLVED beside
    every claude reviewer would turn an ordinary, correct absence into a
    column of alarms.
    """
    evidence = row.get("verdict_author_runtime_evidence") \
        if isinstance(row, dict) else None
    resolved = evidence.get("resolved") if isinstance(evidence, dict) else None
    if not isinstance(resolved, dict) \
            or not (resolved.get("provider") or resolved.get("upstream_model")):
        return ""
    from . import proxywatch
    return proxywatch.resolved_model_phrase(resolved, resolved.get("family"))


# ==========================================================================
# WHICH INPUT PRODUCED THE FAMILY THE APPROVAL TIER JUDGED.
#
# THE DEFECT. `route.APPROVAL_TIER` is a tuple of FAMILY names and the tier
# evaluates one family per verdict, so the whole cross-family guarantee rests
# on where that one word came from. Two producers write it and the recorded
# verdict cannot tell them apart:
#
#   a MEASURED PROXY ROUTE (authority v3) — `proxywatch._proxy_route_family`
#     resolves the family from the exact alias/provider/upstream_model the
#     proof bound, and refuses on zero or two matches. The seat's label, its
#     roster family string and the proof's storage key contribute nothing.
#     That family IS a fact about the model that answered.
#
#   the SEAT'S OWN ROSTER STAMP (authority v4/v5) — `HELM_MODEL_FAMILY`, a
#     value the launch seam declares about itself. Nothing measures it against
#     the weights that answered the turn, and a native runtime's `model` is
#     declared by that same seam, so carrying a model there does NOT upgrade
#     the family: one self-report does not corroborate another.
#
# WHY IT IS RECORDED RATHER THAN ENFORCED. The roster axis is not a fault and
# refusing it would refuse every native review this fleet writes. It is a
# WEAKER INPUT, and the defect is that a tier computed from a weaker input
# reads identical to one computed from a measured route. So the axis rides the
# verdict, the surface prints it, and a reader deciding whether a leg really
# was cross-family reads the axis instead of inferring it from silence.
#
# UNKNOWN IS NOT ROSTER AND NOT MODEL. An envelope this resolver does not
# recognise gets its own word. The tempting spelling — model when v3, roster
# otherwise — folds every input neither arm anticipated into a FALLBACK CLAIM
# nobody measured: a future authority version would silently be reported as
# "the seat's roster said so" when no roster was read at all. Both known sets
# are named explicitly and everything else is UNKNOWN.
FAMILY_AXIS_MODEL = "model"    # from the measured route that served the turn
FAMILY_AXIS_ROSTER = "roster"  # from the seat's own stamp — a FALLBACK
FAMILY_AXIS_UNKNOWN = "unknown"  # neither established; never read as either
FAMILY_AXES = (FAMILY_AXIS_MODEL, FAMILY_AXIS_ROSTER, FAMILY_AXIS_UNKNOWN)


def verdict_family_axis(evidence):
    """(axis, model) — WHICH input produced this verdict's judged family.

    `model` is the identifier that licensed a MODEL axis and is None on every
    other axis, so a caller can never quote a model as the evidence for a
    family that was not derived from one.

    A v3 ENVELOPE CARRYING NO UPSTREAM MODEL IS UNKNOWN, NOT MODEL. The route
    is what derives the family, and a route that names no model is not a
    reading of the turn — calling it the model axis would let the strongest
    word in this vocabulary be earned by an empty field.
    """
    resolved = evidence.get("resolved") if isinstance(evidence, dict) else None
    authority = evidence.get("authority") if isinstance(evidence, dict) else None
    if not isinstance(resolved, dict) or not isinstance(authority, dict):
        return FAMILY_AXIS_UNKNOWN, None
    version = authority.get("v")
    if version == 3:
        model = resolved.get("upstream_model")
        if not isinstance(model, str) or not model.strip():
            return FAMILY_AXIS_UNKNOWN, None
        return FAMILY_AXIS_MODEL, model.strip()
    if version in (4, 5):
        return FAMILY_AXIS_ROSTER, None
    return FAMILY_AXIS_UNKNOWN, None


def verdict_family_axis_note(row):
    """The word a surface prints when this verdict's family was NOT measured.

    "" ON THE MODEL AXIS, on purpose: `verdict_resolved_model` already renders
    the measured route on exactly those rows, so a second column announcing
    that the axis is fine would be noise standing beside its own evidence.
    What has no surface today is the FALLBACK and the ABSENCE, and those are
    the two a reader must never mistake for a measurement.

    "" ALSO ON A ROW THAT CARRIES NO AUTHOR PROOF AT ALL, and that is a
    different fact from an unreadable one. An open row has no verdict, so
    there is no family for an axis to describe and UNKNOWN would accuse every
    line in the table of an absence that belongs to the question, not the
    answer. A row that HAS the proof and cannot be read prints UNKNOWN, which
    is the one this vocabulary exists for.

    ONE RENDERER, because the triage table and any later detail view printing
    two spellings of this is how two sections of one surface come to disagree
    about what the column means.
    """
    if not isinstance(row, dict) or "verdict_author_runtime_evidence" not in row:
        return ""
    axis, _model = verdict_family_axis(row["verdict_author_runtime_evidence"])
    return "" if axis == FAMILY_AXIS_MODEL else axis.upper() \
        if axis == FAMILY_AXIS_UNKNOWN else axis


def _new_state(row):
    """Normalize one first event, including historical schemas.

    Historical ref-less or short-ref open rows remain visible as
    ``needs-redispatch``; they are never silently dropped or rebound at replay.
    """
    if not _valid_identity(row):
        return None
    recipient, recipient_display = _recipient_fields(row)
    if recipient is None:
        return None
    event = row.get("event")
    status = row.get("status")
    if event == "dispatch" and type(row.get("v")) is int and row.get("v") == 3 \
            and type(row.get("seq")) is int and row.get("seq") == 0:
        if status != "open" or not _TIP.fullmatch(str(row.get("tip") or "")):
            return None
        # AN OPENER IS NOT AN ACTIVITY SOURCE, and `dict(row)` made it one. The
        # three `verdict_author_*` keys are the only evidence that this fold
        # ACCEPTED a verdict's author binding, so a state born carrying them
        # credited a later verdict that carried no binding at all —
        # `mark_verdict(bind_author=False)`, a supported producer shape. Three
        # null keys on a hand-edited opener thereby MANUFACTURED an old
        # validated first act, which is what turns "the author of nothing,
        # ever" — a refusal — into a durable absence, which is a permit.
        # Dropped rather than refused, exactly like the forged `chain_root`
        # below: the field reads ABSENT instead of reaching a consumer as a
        # plausible one, and no obligation is lost over it. THREE POPS, NOT A
        # COMPREHENSION: filtering every key of every opener cost 0.09s per
        # 20,000 rows against 0.001s for this (measured), and the fold's own
        # budget arm allows one second for that many.
        out = dict(row)
        for field in VERDICT_AUTHOR_EVIDENCE_FIELDS:
            out.pop(field, None)
        out.update(status="open", delivery="needs-confirmation",
                   migration=None, seq=0, tip=str(row["tip"]).lower(),
                   recipient=recipient, recipient_display=recipient_display,
                   # Replay is NOT a trust boundary — write-time is the gate —
                   # but a hand-edited or forged chain must read UNKNOWN here
                   # rather than reach a consumer as a plausible id. `dict(row)`
                   # alone passed the raw field straight through.
                   chain_root=_replay_chain(row.get("chain_root")),
                   supersedes=_replay_chain(row.get("supersedes")))
        # A FOUNDING ROW DECLARES NO HOLD, SO IT CANNOT DECLARE ONE CLEAN.
        # `dict(row)` passed an unsolicited field straight into the state, and
        # nothing downstream cleared it: a founder carrying `True` selected as
        # source-clean and then raised TypeError when the label sliced it, and
        # one carrying a valid-looking sha claimed a cleanliness no reviewer
        # had ever declared. Only a hold event may make this claim.
        out.pop("source_clean_tip", None)
        return out
    # v1 snapshots and the short-lived v2 add/posting schemas.
    if event not in (None, "add", "posting"):
        return None
    if status not in ("open", "pending", "posting", "acked", "verdict", "held"):
        return None
    tip = str(row.get("tip") or "").lower()
    exact = tip if _TIP.fullmatch(tip) else None
    closable = _pre_boundary(row.get("ts"))
    out = {"v": row.get("v") or 1, "id": str(row["id"]),
           "event": "dispatch", "seq": int(row.get("seq") or 0),
           "ts": row.get("ts"), "recipient": recipient,
           "recipient_display": recipient_display,
           # REPLAY IS BYTE-TRUTH (#142 r2): the stored lane is
           # never rewritten on read — historical attest sidecar bindings hash
           # these bytes, and joins normalize at the JOIN instead.
           "lane": row.get("lane"), "tip": exact,
           "ref": row.get("ref"), "note": row.get("note"),
           "deadline_s": int(row.get("deadline_s")),
           "source": row.get("source"), "sender": row.get("sender"),
           "repo_id": row.get("repo_id"), "repo_root": row.get("repo_root"),
           "operation_key": row.get("dispatch_key"),
           "message_hash": row.get("message_hash"),
           # A v1/v2 row PREDATES chains, so ABSENT is its true reading — and
           # the field is still read through the validator rather than hardcoded
           # to None, so a v1-shaped row carrying a forged chain reads UNKNOWN
           # instead of silently inheriting the legacy (permissive) branch.
           "chain_root": _replay_chain(row.get("chain_root")),
           "supersedes": _replay_chain(row.get("supersedes")),
           "status": "verdict" if status == "verdict" and closable
                      else "held" if status == "held" else "open",
           "delivery": "observed" if row.get("delivery_ref") else "needs-confirmation",
           "delivery_ref": row.get("delivery_ref"),
           "verdict_ref": row.get("verdict_ref"),
           "reviewed_tip": row.get("reviewed_tip"),
           "migration": None if exact or status == "verdict" and closable
           else "needs-redispatch"}
    if out["status"] == "verdict" and not out["verdict_ref"]:
        return None
    return out


# A dispatch is CLOSED once it carries a VERDICT (a review happened) or a
# CANCEL (honestly abandoned with a reason). Both are terminal to dispatch
# mutation and drop from open / overdue / stop reads. Verdict rows remain input
# to landreq; only a proof-gated discharge may later annotate contrary debt.
CLOSED_STATES = ("verdict", "cancelled", "closed")

# The COMPLEMENT, and it is not merely `not CLOSED_STATES`: a cancel admits an
# OPEN row and a HELD one (a hold is an acknowledged pause, not an ending), and
# it is the only set `mark_cancel` will act on. It lives here as a constant
# because a SECOND reader needs the identical test — `rebind` must predict
# whether its cancel will be refused BEFORE it writes the successor (#178), and
# a predicate spelled twice is a predicate that drifts once.
CANCELLABLE_STATES = ("open", "held")

# AND IT IS NOT THE WHOLE CANCEL DOMAIN, deliberately. `advisory_close_error`
# admits ONE further shape — a VERDICT row whose verdict declared no polarity —
# and that row is not an OPEN STATE, so adding it here would widen the second
# reader too: `rebind` predicts its own cancel from this tuple and must keep
# refusing a reviewed row, whose recipient it may not move. Two admissions, two
# predicates, one door.

# The cancel reason's length budget, named ONCE because two call sites depend on
# agreeing about it: mark_cancel REFUSES a longer reason, and the rebind abort
# CLAMPS to it before calling. A literal in each place is a literal that drifts,
# and the drift is silent — the abort would simply start failing again.
_CANCEL_REASON_CAP = 256

# THE ADVISORY CLOSE'S RECORDED PREFIX, and it rides in the REASON rather than
# in a flag because the reason is the field every listing already prints. A
# terminal whose honest name lives only in a boolean is a terminal most readers
# see as a bare cancellation.
_ADVISORY_CLOSE_PREFIX = "advisory-closed (verdict declared no polarity): "
# WHAT IS LEFT FOR THE OPERATOR, and the writer budgets the COMPOSED string
# against `_CANCEL_REASON_CAP` because the REDUCER budgets the composed string:
# `_clean` at 256 runs again on replay, and a writer that accepted 256 chars of
# operator prose would append an event the fold silently drops — the exact
# window a verdict-evidence drop opens between two budgets that disagree
# about which string they are measuring.
_ADVISORY_REASON_CAP = _CANCEL_REASON_CAP - len(_ADVISORY_CLOSE_PREFIX)

# NO NUMERIC CAP ON THE SUPERSESSION WALK, and the reason is worth keeping.
# A cap was here to bound a cycle — but every walk below carries a VISITED SET,
# and that alone terminates: the ledger is finite, so a walk that never revisits
# a node cannot run forever. The number therefore protected against nothing it
# was written for and could only ever do one thing: TRUNCATE A VALID LONG CHAIN.
# It did exactly that (an adversarial sweep — a legitimate 66-link chain
# had its open frontier left uncancelled while the cleanup reported success).
# A cap that can only ever be wrong is a footgun; the visited set is the bound.

# VERDICT POLARITY — a verdict's DIRECTION, which `status` alone cannot carry.
# `status == "verdict"` only ever asserted THAT a review happened; landreq read
# it as "the review said YES" and parked every rejection in READY forever. Live
# on 2026-07-25: five loops sat READY for a day carrying SUPERSEDED, SUPERSEDED,
# "FIX (3 blockers)", "helm#210 FIX", "helm#217 FIX" — not one an approval —
# and `lr stalls` billed all five as land-side workflow gaps. A state machine
# treating HAS-A-VALUE as HAS-A-GOOD-VALUE.
#
# Polarity is DECLARED at verdict time and replayed as immutable evidence. It is
# never INFERRED from the evidence prose: sniffing free text for "FIX"/"CLEAR"
# is the per-case-handler spiral (the next reviewer writes "needs work", then
# "REJECT", then "blocked"), and that spiral has one cure — whole-object
# validation plus an honest refusal. So a row that never declared reads
# UNDECLARED, and undeclared fails CLOSED: it is never treated as an approval.


def clean_polarity(value):
    """(polarity, err). None/"" means historical UNDECLARED during replay.

    The current writer rejects that shape before calling this normalizer; old
    rows still need it so replay remains truthful. An unrecognised string is an
    ERROR, never quietly coerced into a direction."""
    if value is None or value == "":
        return None, None
    p = str(value).strip().lower()
    if p not in POLARITIES:
        return None, "verdict polarity must be one of %s (got %r)" % (
            "/".join(POLARITIES), value)
    return p, None


def _replay_polarity(value):
    """Replay is NOT a trust boundary. Write-time validation is the gate; this is
    the fail-closed backstop, so a hand-edited, forged, or future-versioned row
    carrying an unknown polarity reads UNDECLARED rather than a direction."""
    p, err = clean_polarity(value)
    return None if err else p


def _open(row):
    """A row actively awaiting a verdict -- not closed and not held. Delegates to query facade."""
    from . import query
    return query.query_is_unheld_open(row)


# A successor that MOVED NOTHING is a pass-through on the way to whoever
# actually took the work, never an endpoint.
#
# TERMINALITY IS NOT ALWAYS A STATUS. This was a tuple of status spellings —
# ("cancelled", "withdrawn", "abandoned") — and TWO OF THE THREE WERE
# UNREACHABLE: replay records a withdrawal or an abandonment as status="verdict"
# plus a withdrawn=True / abandoned=True FLAG. So a withdrawn successor read as
# CARRYING and hid its parent, and no test caught it because every fixture spelt
# the status by hand. Read the replayed shape, never the shape you assumed.
_NON_CARRYING_STATUS = ("cancelled",)
# `retired_admin` joins them (verified against the target modules).
# An administratively RETIRED successor took no obligation — that is what the
# retirement said — so it is a pass-through and its predecessor must stay
# visible. Without this, `carrier()` reads the retired successor as carrying
# and HIDES the parent, and the parent's debt vanishes from every surface.
#
# THE ORDER OF THE TWO CURES IS WHY THIS ONE IS NOT OPTIONAL. Before
# `query_is_open` learned that retired is terminal, the retired child stayed
# "open" and the composition was merely wrong in one direction. The moment
# that lands, a retired successor is closed AND still counted as carrying,
# and the parent silently loses its debt with nothing going red. Two correct
# fixes compose into a regression unless both land together.
_NON_CARRYING_FLAGS = ("withdrawn", "abandoned", "retired_admin",
                       # A RETRACTED verdict took nothing on: the review it
                       # recorded is void, so a successor that retracted is a
                       # pass-through and the obligation it seemed to carry is
                       # visible again on the row above it (task/3060).
                       "verdict_retracted")
# ...AND IT IS NOT ALWAYS A FLAG EITHER (helm task/744, L3).
# The comment above learned that a withdrawal is recorded as a FLAG and
# stopped there — but a STRUCTURED close writes `close_reason="withdrawn"`
# with its own proof fields and no flag at all, and that spelling was still
# read as CARRYING. With siblings, the consequence is worse than a hidden
# parent: the walk's answer depends on which successor it happens to reach
# first, so the same population resolved SUPERSEDED or LANDED according to
# insertion order. `stranded` joins it for the same reason — both say nobody
# owes anything further, which is precisely what a pass-through is.
#
# The three spellings are deliberately kept apart rather than merged. A
# `withdrawn` FLAG annotates a verdict without ending the row; a `withdrawn`
# CLOSE ends it. Only the second belongs here.
_NON_CARRYING_CLOSE = ("withdrawn", "stranded")


def moved_nothing(row):
    """True when this successor took no obligation and is a pass-through."""
    if not isinstance(row, dict):
        return False
    if row.get("status") in _NON_CARRYING_STATUS:
        return True
    if row.get("close_reason") in _NON_CARRYING_CLOSE:
        return True
    return any(bool(row.get(f)) for f in _NON_CARRYING_FLAGS)


# THE ORDERED RETIRED-BY QUESTION, asked ONCE and read two ways (task/2858).
# Each entry is (the field that records it, the STATE it leaves the row in,
# the VERB that did it). `closed_state` wants the middle column and
# `_close_retired_by` the right one, and they MUST agree about the ORDER or
# they answer differently about one row -- which they did.
_RETIRED_BY = (
    ("discharged", "discharged", "discharge"),
    ("withdrawn", "withdrawn", "withdraw"),
    ("closed_by_landing", "landed", "close-landed"),
    ("abandoned", "abandoned", "abandon"),
    # A RETRACTION IS A TERMINAL LIKE THE FOUR ABOVE (task/3060): the verdict
    # stays on the ledger and its authority is withdrawn, so every door that
    # asks "what already ended this row" names it instead of closing it again.
    ("verdict_retracted", "retracted", "retract"),
)


def closed_state(row):
    """The spelling that ENDED this row — the word a refusal has to say.

    `status` alone cannot answer it: replay records a withdrawal or an
    abandonment as status="verdict" plus a FLAG, and a structured close as a
    `close_reason` with no flag at all (see `moved_nothing` above). A refusal
    that said "verdict" for a withdrawn row would send its reader looking for
    a review that was never written."""
    if not isinstance(row, dict):
        return "closed"
    # IT ASKS `_RETIRED_BY` RATHER THAN ITS OWN LIST, which is the whole of
    # task/2858. The old list was `_NON_CARRYING_FLAGS` -- withdrawn,
    # abandoned, retired_admin -- and that answers a DIFFERENT question:
    # those are the successors that CARRY NOTHING, not the ways a row gets
    # retired. Two retiring facts are absent from it, `discharged` and
    # `closed_by_landing`, and both arrive with NO close_reason, so such a
    # row fell through to its bare status and said "verdict" -- sending its
    # reader after a review that was never written, which is the failure
    # this docstring warns about. Measured: 51 live rows, 47 discharged and
    # 4 closed by landing.
    for field, state, _verb in _RETIRED_BY:
        if row.get(field):
            return state
    if row.get("retired_admin"):
        return "retired_admin"
    reason = row.get("close_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return str(row.get("status") or "closed")


def _same_chain(parent, kid):
    """A successor carries only when replay proves it is the SAME WORK.

    `supersedes` is an edge any writer can put on a row; chain_root is the
    replayed identity of the work itself. Without this, a row naming an
    unrelated parent transferred that parent's debt and hid it — one mistaken
    edge silencing a real obligation.

    A LEGACY parent's absent root is not uncertainty. `_resolve_chain` owns the
    compatibility rule: a new child honestly roots that historical chain at the
    parent id, while the parent remains byte-true and rootless. Apply the SAME
    rule here or every valid continuation of pre-chain work reads as a foreign
    edge and both parent and child become owed. A child's absent root and either
    side's UNKNOWN root still refuse toward VISIBLE."""
    proot, kroot = parent.get("chain_root"), kid.get("chain_root")
    if proot == CHAIN_UNKNOWN or kroot in (None, CHAIN_UNKNOWN):
        return False
    expected = parent.get("id") if proot is None else proot
    if not expected:
        return False
    return str(expected) == str(kroot)


def same_chain_children(parent, children):
    """The children of `parent` that are the SAME WORK — the exported door.

    `supersedes` is an edge any writer can put on any row, so a raw walk over it
    answers a question nobody asked: it finds everything that POINTS at this
    row, not everything that CONTINUES it. Every consumer that enumerates a
    row's branches needs the second question, and until now each had to reach
    for `_same_chain` privately and remember to apply it at every hop.

    AN UNREADABLE PARENT YIELDS NOTHING, WHICH IS THE HALF THE FIRST CURE
    MISSED. Admitting all children when the parent is absent looks like being
    permissive with missing data; it is the opposite — two rows naming one
    nonexistent parent then read as a fork of work that does not exist. There
    is no chain identity to compare against, so there is no branch set, and
    UNKNOWN is the honest answer rather than "all of them" (the
    obligation-delivery-seam meld).

    Exported rather than private because the fork question and the debt
    question must not drift: `carrier` refuses a foreign successor through
    `_same_chain`, and anything enumerating a fork population has to refuse the
    same edges or the two surfaces disagree about what a branch is.
    """
    if not isinstance(parent, dict):
        return []
    return [c for c in (children or ())
            if isinstance(c, dict) and _same_chain(parent, c)]


def _successor_index(snap):
    """{parent_id: [child_row, ...]} — the SUCCESSOR SET, built once.

    Every consumer of supersession asks the same question and four of them
    currently do not ask it at all. Building the index once and handing it to a
    predicate is what lets them share one answer instead of four readings of a
    raw field.
    """
    kids = {}
    if not isinstance(snap, dict):
        return kids
    for row in snap.values():
        if not isinstance(row, dict):
            continue
        parent = row.get("supersedes")
        if parent:
            kids.setdefault(str(parent), []).append(row)
    return kids


def _cycle_components(kids):
    """{node_id: component_id} for nodes that participate in a cycle.

    A candidate reached from `root` loops back exactly when both nodes belong to
    the same strongly connected component. Compute those components ONCE in
    O(V+E), rather than running a descendant DFS for every candidate of every
    open row — the latter restored quadratic work underneath the shared index.

    Kosaraju is iterative here: a real ledger can exceed Python's recursion
    limit, and cycle detection is a visibility guard, not a reason for a large
    but valid dispatch graph to crash its obligation surfaces."""
    graph, reverse, nodes = {}, {}, set()
    if not isinstance(kids, dict):
        return {}
    for parent, rows in kids.items():
        parent = str(parent or "")
        if not parent:
            continue
        nodes.add(parent)
        edges = graph.setdefault(parent, [])
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, dict):
                continue
            kid = str(row.get("id") or "")
            if not kid:
                continue
            nodes.add(kid)
            edges.append(kid)
            reverse.setdefault(kid, []).append(parent)
    for node in nodes:
        graph.setdefault(node, [])
        reverse.setdefault(node, [])

    seen, order = set(), []
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, 0)]
        while stack:
            node, offset = stack[-1]
            edges = graph[node]
            if offset < len(edges):
                child = edges[offset]
                stack[-1] = (node, offset + 1)
                if child not in seen:
                    seen.add(child)
                    stack.append((child, 0))
                continue
            order.append(node)
            stack.pop()

    cyclic, assigned, component = {}, set(), 0
    for start in reversed(order):
        if start in assigned:
            continue
        assigned.add(start)
        members, frontier = [], [start]
        while frontier:
            node = frontier.pop()
            members.append(node)
            for parent in reverse[node]:
                if parent not in assigned:
                    assigned.add(parent)
                    frontier.append(parent)
        if len(members) > 1 or members[0] in graph[members[0]]:
            for node in members:
                cyclic[node] = component
        component += 1
    return cyclic


def carrier(row, snap, index=None, cycles=None):
    """The successor that ACTUALLY holds this row's obligation, or None.

    THE POINTER IS NOT THE ANSWER. `add` stamps superseded_by on the parent and
    keeps its FIRST successor forever, by design. So when that first successor
    dies and a SIBLING takes the work — a reviewer stands down, the row is
    re-dispatched to someone else — the pointer still names the corpse and the
    parent reads as owed. Measured live 2026-08-05 against the whole ledger: TWO
    build rows each named a CANCELLED review while a sibling carried the same
    lane all the way to trunk, so both read as owed by a builder who owed
    nothing. (Their dispatch ids are in the meld record; they are ledger rows,
    not commits, so they are deliberately not cited here where a fresh clone
    would read them as dead shas.) That is meld finding (6), and it is why this
    walks the SUCCESSOR SET rather than the frozen link.

    CARRYING IS NOT "NOT TERMINAL", AND IT IS NOT "NOT A PASS-THROUGH"
    EITHER. ONE yes/no per node — is this successor a pass-through? — cannot
    answer this, because its NO arm swallows a third state: a successor that
    TOOK the obligation and AUTHORISES NOTHING FURTHER. A CONCUR verdict is
    exactly that — it endorses, no close door admits it, and nobody is owed
    anything by it — yet it answered "I hold it" and the parent left the owed
    frontier for good. `successor_disposition` names all four answers, and a
    DEAD END is a pass-through here: the walk continues through it, and if
    nothing beyond it holds or discharges, this returns None and the parent
    stays VISIBLE.

    A FIX IS NOT A DEAD END, and the distinction is the whole of the cure. A
    finding is the NEXT ROUND of the same obligation, so a FIX successor is a
    live answer to "who holds this now" even though it discharges nothing;
    billing the parent again would turn one broken chain into two people
    fixing one lane. `NON_CARRYING_POLARITY` therefore names `concur` alone.

    AN UNKNOWN THIS WALK CANNOT READ RESOLVES TOWARD VISIBLE. No successors,
    an unreadable row, or a cycle all return None, which leaves the parent
    SHOWN. A duplicate row has two entries shouting and someone reconciles
    them; a hidden one has nobody, and this whole class of defect is rows that
    stopped being visible while still being owed.
    """
    kids = _successor_index(snap) if index is None else index
    cycles = _cycle_components(kids) if cycles is None else cycles
    root = str(row.get("id") or "")
    root_cycle = cycles.get(root)
    seen, frontier = {root}, [root]
    while frontier:
        pid = frontier.pop()
        for kid in kids.get(pid, ()):
            kid_id = str(kid.get("id") or "")
            # ALREADY-SEEN IS NOT A CARRIER. The visited check used to run on
            # the PARENT at pop time and never on the CHILD before returning
            # it, so a cycle carried itself: a self-edge answered carrier(p)=p
            # and a mutual pair each carried the other, so BOTH left the owed
            # frontier. The docstring claimed cycles resolve toward VISIBLE and
            # they did the exact opposite. The cycle test passed because its
            # fixture made every link CANCELLED, so the walk terminated on
            # state and never reached the visited set at all.
            if not kid_id or kid_id in seen:
                continue
            if not _same_chain(row, kid):
                continue                   # a foreign chain cannot take this debt
            if successor_disposition(kid) in ACCOUNTED_DISPOSITIONS:
                # root already reaches kid through this walk. Kid reaches root
                # iff both are in the same cyclic SCC; a ring holds nothing.
                if root_cycle is not None \
                        and cycles.get(kid_id) == root_cycle:
                    continue
                return kid                 # live, or it ended the chain
            seen.add(kid_id)
            frontier.append(kid_id)
    return None


# THE TERMINALS THAT END AN OBLIGATION, by the reason the close recorded:
# a land, a carried close, and the two polarity-less terminals (a build row
# discharged, a report delivered). A close that recorded NO reason is not on
# this list on purpose: `closed_state` spells such a row by its bare status,
# and a bare status says nothing about whether the work finished, so it
# resolves toward visible like every other unknown here.
DISCHARGING_CLOSE = ("landed", "carried", "discharged", "delivered-report")
# A FIX IS THE NEXT ROUND, NOT AN ENDING, and `concur` authorizes nothing by
# construction, so neither discharges. `supersede` moves the work to another
# artifact rather than finishing this one -- that is a CARRIER's answer, not
# this one's.
DISCHARGING_POLARITY = ("approve",)


def _discharges(row):
    """True when this successor ENDED an obligation rather than taking it."""
    if not isinstance(row, dict):
        return False
    if closed_state(row) in DISCHARGING_CLOSE:
        return True
    return str(row.get("polarity") or "").lower() in DISCHARGING_POLARITY


# WHAT A SUCCESSOR DID WITH THE OBLIGATION IT WAS HANDED — the four answers,
# and the WHOLE of them. A single yes/no per node (is this a pass-through?)
# reads its NO as "it holds the debt", so a successor that took the work and
# left NOTHING behind it lands in the carrying arm by default. The names exist
# so the arm a reader lands in has to be spelled.
PASS_THROUGH = "pass-through"   # took nothing; the work may have moved on
HOLDS = "holds"                 # the obligation is live at this successor
ENDS = "ends"                   # discharged it; nobody owes anything further
DEAD_END = "dead-end"           # took it, and authorises nothing further
# The two that ACCOUNT for a predecessor's debt. A parent may be hidden from an
# owed-work surface only behind one of these, and never behind the other two.
ACCOUNTED_DISPOSITIONS = (HOLDS, ENDS)
# THE ONE POLARITY THAT ENDORSES WITHOUT AUTHORISING ANYTHING. `concur` is
# outside WORK_POLARITIES by construction so that endorsement has a word
# promising no landing, and NO close door admits it — the close tables below
# spell it out twice, and `ConcurAuthorizesNothingTest` is its arm. A verdict
# that authorises nothing anywhere cannot be the thing that accounts for a
# predecessor's debt either, which is the whole of this lane.
#
# FIX AND SUPERSEDE ARE DELIBERATELY ABSENT. A FIX is the NEXT ROUND of the
# same obligation, and `supersede` moves the work to another artifact: both
# took the work, so both are a live answer to "who holds this now" even
# though neither discharges it. That split is this module's existing design
# (see DISCHARGING_POLARITY), and the arms that pin it are
# `test_a_FIX_successor_is_the_NEXT_ROUND_and_ends_nothing` and the fork
# arms in tests/test_obligation.py, which bill one broken chain once.
NON_CARRYING_POLARITY = ("concur",)


def successor_disposition(row):
    """One of PASS_THROUGH / HOLDS / ENDS / DEAD_END for one successor.

    THE DEAD END IS THE STATE THAT HAD NO NAME, and having no name is how it
    came to be read as HOLDS. Measured over the live ledger: of 199 open rows,
    the owed frontier admitted 8. Eleven of the rest were open rows hidden
    behind a successor whose verdict was CONCUR — a terminal that endorses,
    authorises nothing, discharges nothing, and is owed by nobody. `helm
    dispatch list --open` is the flag a reader uses to ask what is still
    outstanding, so each of those rows answered that question with silence,
    which is the one answer nobody re-checks.

    THE SPLIT IS BETWEEN ENDORSING AND TAKING, NOT BETWEEN OPEN AND CLOSED.
    A FIX verdict is terminal too, and it CARRIES: the finding is the next
    round of the same obligation, so somebody holds it and billing the parent
    again would make one broken chain into two people fixing one lane. Only
    `concur` takes an obligation and leaves nothing standing behind it, which
    is why `NON_CARRYING_POLARITY` names it alone rather than every
    non-discharging polarity.

    A ROW THIS CANNOT READ IS A DEAD END, because an unreadable successor
    proves nothing about who owes the work and the only honest thing to do
    with a row nobody can classify is show its parent."""
    if not isinstance(row, dict):
        return DEAD_END
    # A MAPPING IS NOT YET A ROW, and the type check above lets through the
    # one shape that is worse than the ones it catches: a dict carrying no
    # identity. Every field read below is OPTIONAL on a dict, so such a value
    # answers no to each question in turn and lands in the default arm, which
    # announces HOLDS -- the single answer that HIDES the parent. An id is
    # what makes a successor a row at all: the successor index keys on it,
    # both walks track it, and the writer stamps one on every row it stores.
    # Without it nothing was measured, so the answer is the one that resolves
    # toward visible.
    #
    # IT REFUSES ONLY THE UNREADABLE. A row that HAS an id keeps the whole
    # four-way classification, so no cancelled, concurred, discharging or
    # open successor changes its answer because of this arm.
    if not str(row.get("id") or "").strip():
        return DEAD_END
    if moved_nothing(row):
        return PASS_THROUGH
    from . import query
    if query.query_is_open(row):
        # LIVE IS ASKED BEFORE DISCHARGED, and the order is the contract: an
        # OPEN or HELD row cannot have discharged anything, and a held row is
        # a live obligation that is merely gated. Asking `_discharges` first
        # would let a polarity written onto a still-open row end a chain that
        # is manifestly still running.
        return HOLDS
    if _discharges(row):
        return ENDS
    if str(row.get("polarity") or "").lower() in NON_CARRYING_POLARITY:
        return DEAD_END
    # EVERY OTHER TERMINAL TOOK THE WORK. A bare close, a FIX, a supersede or
    # a reason nobody has invented yet all say somebody acted on this chain,
    # and this module's answer to "who holds it now" is that successor.
    return HOLDS


def ended(row, snap, index=None, cycles=None):
    """The successor that ENDED this row's obligation, or None.

    `carrier`'S SIBLING, AND THE QUESTION IT DOES NOT ASK. That one answers
    WHO HOLDS THIS NOW and is right to count an approving, landed successor as
    holding it -- something took the obligation and the chain is accounted
    for. This one answers WHETHER ANYONE STILL OWES ANYTHING, and for that the
    same successor is the opposite answer: a chain that reached a landed
    APPROVE is finished, and a reader told to chase it is being sent after
    work that is already on trunk.

    MEASURED, AND THE COST WAS PAID BEFORE IT WAS FILED (task/2861). A listing
    tagged a row `YOURS TO CHASE` at 542 minutes against a 45-minute deadline;
    its successor was LANDED and the reviewer's patch was already an ancestor
    of trunk. The reader went to close a row that was never hanging. A LISTING
    THAT MANUFACTURES FALSE DEBT MAKES ITS READERS WRITE FALSE CLOSES, which
    is worse than a listing that says nothing.

    IT WALKS `carrier`'S WALK, DELIBERATELY. Same successor set, same
    same-chain rule, same pass-throughs, same cycle guard -- because two walks
    that answer about one graph their own way is exactly how the two legs of
    this listing drifted apart in the first place. Only the question at each
    node differs.

    A CANCELLED SUCCESSOR DOES NOT END ANYTHING, and this is where the filed
    cure shape was wrong. task/2861 counted `cancelled` as discharging; but
    `moved_nothing` already calls it a PASS-THROUGH, because a cancelled
    successor may mean the work MOVED to a sibling rather than finished. Over
    the live board the difference is 31 rows of 121 -- rows the filed rule
    would have silenced while somebody still owed them. The walk continues
    through a cancellation exactly as `carrier` does, and only a landed,
    closed or carried close, or an APPROVE verdict, ends anything.

    NEITHER DOES A DEAD END, AND IT IS NOT AN ENDPOINT EITHER. A successor
    that reached a CONCUR verdict is terminal and authorises nothing, so
    stopping there would assert the chain ended at a row that ended nothing.
    What ended this chain, if anything, lies BEYOND it, and the walk continues
    through it for the same reason it continues through a cancellation. A FIX
    is different and is NOT walked through: it HOLDS, so the question of what
    its own chain reached belongs to it. Both walks read one classification
    from `successor_disposition`, so they cannot come to disagree about which
    node is which.

    None MEANS NOBODY ENDED IT, which leaves the row SHOWN. Every unknown
    resolves toward visible for the same reason `carrier` gives: a row shown
    in error has a reader who reconciles it, and a hidden one has nobody.
    """
    kids = _successor_index(snap) if index is None else index
    cycles = _cycle_components(kids) if cycles is None else cycles
    root = str(row.get("id") or "")
    root_cycle = cycles.get(root)
    seen, frontier = {root}, [root]
    while frontier:
        pid = frontier.pop()
        for kid in kids.get(pid, ()):
            kid_id = str(kid.get("id") or "")
            if not kid_id or kid_id in seen:
                continue
            if not _same_chain(row, kid):
                continue
            disposition = successor_disposition(kid)
            if disposition in ACCOUNTED_DISPOSITIONS:
                if root_cycle is not None \
                        and cycles.get(kid_id) == root_cycle:
                    continue
                if disposition == ENDS:
                    return kid
                # HOLDS: it took the obligation without ending it, and it is
                # still LIVE. Its own successors are ITS chain to answer for,
                # not this row's.
                continue
            # PASS-THROUGH OR DEAD END, AND THE SECOND ONE IS THE CURE. A
            # CONCUR verdict must NOT arrest this arm: it authorises nothing,
            # so whether this row's chain ended is a question about what lies
            # BEYOND it, exactly as beyond a cancellation.
            seen.add(kid_id)
            frontier.append(kid_id)
    return None


class _CycleView:
    """`_cycle_components` answered ONE NODE AT A TIME, and identically.

    `carrier` asks the cycle map exactly two questions per walk — is the ROOT
    in a cyclic component, and is this KID in the SAME one — but
    `_cycle_components` answers them by running Kosaraju over the WHOLE graph
    first. Under replay that is paid per queried row: on a ledger of 17,158
    events folding to 4,381 rows the fold called it 127 times for 7.2s of own
    time, to read two keys each time.

    SAME COMPONENT IS MUTUAL REACHABILITY, which is a LOCAL question. The
    component of a node is `forward(node) & backward(node)` over the same raw
    successor edges `_cycle_components` builds from — every edge, including the
    foreign-chain and pass-through ones `carrier` itself refuses, because the
    ring that must not hold an obligation is a ring in the graph, not in the
    filtered walk. A node is cyclic when that set has more than one member, or
    when it carries a self-edge; that is the same predicate `_cycle_components`
    applies to a Kosaraju component, so the CYCLIC/NOT-CYCLIC split is the
    same split.

    THE COMPONENT KEY IS THE MEMBER SET rather than an integer, and the
    difference is invisible to every reader: `carrier` only ever compares two
    of these values for equality. Two nodes in one component compute the same
    frozenset; two nodes in different components compute disjoint non-empty
    sets and compare unequal; a non-cyclic node answers None exactly where
    `_cycle_components` left it out of the map. No caller can see the integer
    ids, so nothing observable rides on them — but a caller that wants the
    real map still has `_cycle_components`, which is untouched and remains the
    reference this class is tested against.
    """

    def __init__(self, kids):
        graph, reverse = {}, {}
        for parent, rows in (kids if isinstance(kids, dict) else {}).items():
            parent = str(parent or "")
            if not parent:
                continue
            edges = graph.setdefault(parent, [])
            reverse.setdefault(parent, [])
            for row in rows if isinstance(rows, (list, tuple)) else ():
                if not isinstance(row, dict):
                    continue
                kid = str(row.get("id") or "")
                if not kid:
                    continue
                edges.append(kid)
                graph.setdefault(kid, [])
                reverse.setdefault(kid, []).append(parent)
        self._graph, self._reverse, self._memo = graph, reverse, {}

    @staticmethod
    def _reach(start, edges):
        """Every node reachable from `start`, `start` included."""
        seen, frontier = {start}, [start]
        while frontier:
            for nxt in edges.get(frontier.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        return seen

    def _component(self, node):
        if node not in self._graph:
            return None
        scc = self._reach(node, self._graph) & self._reach(node, self._reverse)
        if len(scc) > 1 or node in self._graph[node]:
            return frozenset(scc)
        return None

    def get(self, node, default=None):
        node = str(node or "")
        if node not in self._memo:
            self._memo[node] = self._component(node)
        answer = self._memo[node]
        return default if answer is None else answer


class _CarrierView(collections.abc.Mapping):
    """`rowworld._carriers` for the rows a caller ACTUALLY asks about.

    `_carriers` answers the supersession question for EVERY row in the
    population, and its one replay caller — the `carried` close arm — then
    reads a single key out of it through `rowworld._work_pair`. Replay pays
    that whole population walk per carried close event: on a ledger of 17,158
    events folding to 4,381 rows, 127 such events drove 553,018 `carrier`
    calls, 478,381 `moved_nothing` calls and 478,381 `_same_chain` calls to
    consume 127 answers — about 25s of the fold.

    THE SHARED WORK IS THE INDEX, NOT THE ANSWERS. `_successor_index` and the
    cycle view are built once per view and reused by every lookup, which is
    exactly what `_carriers` does for its own loop; the only thing removed is
    computing answers nobody reads. A missing key means NO CARRIER — the same
    honest stop `_carriers` records by leaving the row out of its map.

    IT IS A FULL MAPPING because it is handed to callers this module does not
    own. A consumer that iterates or sizes it materialises the whole map and
    gets byte-for-byte what `_carriers` would have returned, so the lazy path
    is an optimisation for the one-key reader rather than a narrower contract.
    `__bool__` is pinned True so that the `carriers or {}` idiom in
    `_work_pair` cannot force that materialisation just to ask a question
    whose answer it then throws away — an empty view and an empty dict both
    answer None to every `get`, so the truth value is not observable.

    NEVER CACHED ACROSS EVENTS. `_fold` replaces row dicts as it replays, so
    the successor graph is a different graph at every event; a view is built
    for one caller at one moment and discarded with it.
    """

    def __init__(self, rows):
        self._rows = rows if isinstance(rows, dict) else {}
        self._index = None
        self._cycles = None
        self._answers = {}
        self._full = None

    def _shared(self):
        if self._index is None:
            self._index = _successor_index(self._rows)
            self._cycles = _CycleView(self._index)
        return self._index, self._cycles

    @staticmethod
    def _held(kid):
        if isinstance(kid, dict) and kid.get("id"):
            return str(kid["id"])
        return None

    def _answer(self, key):
        row = self._rows.get(key)
        if not isinstance(row, dict):
            return None
        index, cycles = self._shared()
        return self._held(carrier(row, self._rows, index, cycles))

    def __getitem__(self, key):
        key = str(key)
        if self._full is not None:
            # dict(view) reads every key back through here after iterating
            return self._full[key]
        if key not in self._answers:
            self._answers[key] = self._answer(key)
        answer = self._answers[key]
        if answer is None:
            raise KeyError(key)
        return answer

    def _materialise(self):
        if self._full is None:
            # THE WHOLE POPULATION TAKES THE WHOLE-GRAPH PASS. `_CycleView`
            # pays a reachability walk per node, which is the cheap answer
            # for one key and a superlinear one for every key; a consumer that
            # iterates or sizes the map gets `_cycle_components`, the same
            # linear pass `rowworld._carriers` runs.
            index, _ = self._shared()
            cycles = _cycle_components(index)
            full = {}
            for rid, row in self._rows.items():
                if not isinstance(row, dict):
                    continue
                held = self._held(carrier(row, self._rows, index, cycles))
                if held:
                    full[str(rid)] = held
            self._full = full
        return self._full

    def __iter__(self):
        return iter(self._materialise())

    def __len__(self):
        return len(self._materialise())

    def __bool__(self):
        return True


# ---------------------------------------------------------------------------
# CURE AWAITING REVIEW — the state that falls between both frontiers
# ---------------------------------------------------------------------------

# A FIX verdict says A CURE IS OWED, so every surface reads the row as "waiting
# on the lane". WHO writes the cure is not fixed by the polarity: the reviewer
# may have written it already and named the tip on the verdict (`patch_tip`),
# in which case what the lane owes is the rebase and the re-dispatch, not the
# code. When the cure exists and nobody re-dispatched, the row still says FIX
# and NOBODY IS WAITING ON ANYBODY: the lane believes it is done, the board
# says it owes work, no reviewer holds it.
#
# NO GUARD FIRES, and the reason is structural rather than an oversight.
# `triage` re-measures `owed()` — the OPEN frontier — and a FIX-verdicted row
# is CLOSED, so it is not owed. Nothing else looks at closed rows because a
# closed row is normally finished. This one is not: it is finished work with
# no reader. It falls between both surfaces and stalls in silence, indefinitely
# (measured: the owner's own MCP decision sat 4.5h in this state, 2026-08-05).
#
# THREE CLAUSES, AND THE THIRD IS THE ONE THAT MAKES IT A RUNG RATHER THAN A
# NAG. Measured across 459 FIX-newest rows on the live ledger 2026-08-06:
#   · 112 LANDED (reviewed_tip is an ancestor of trunk) — the row is done
#   ·  67 a cure EXISTS — but 57 of those have a CHAIN SUCCESSOR, so a
#         reviewer IS holding them: healthy in-flight work, not a stall
#   ·  10 cure exists AND no successor — genuinely nobody is waiting
# Reporting the 67 would be 85% false, and a surface that cries "stranded" at
# in-flight work is muted within a day. "The cure exists" and "nobody is
# waiting on it" are DIFFERENT FACTS and only the second is the defect.
#
# RESOLVED BY ANCESTRY, NEVER BY NAME. A lane LABEL is not a branch name and
# nothing enforces that they match — two of the measured cures live on
# `review/68bec-round7` and `prerebase/gate-epoch` whose labels share no words
# with them. `--contains` finds both without knowing either name; a name-keyed
# scan finds neither and reports "no branch, the cure cannot exist", which is
# how three live lanes were called dead the same night.
CURE_LANDED = "LANDED"          # reviewed_tip reached trunk; nothing is owed
CURE_AWAITING = "CURE AWAITING REVIEW"   # cured, unwitnessed, nobody holds it
CURE_AUTHOR_OWES = "AUTHOR OWES"         # tip == reviewed_tip; the FIX stands
CURE_NO_BRANCH = "NO BRANCH"    # reachable from nothing: never started/pruned
CURE_UNKNOWN = "UNKNOWN"        # git could not answer — never a verdict
CURE_AMBIGUOUS = "AMBIGUOUS"    # incomparable live carriers; never select by name
CURE_REVIEWED = "CURE REVIEWED"  # a chain successor recorded a standing review of the cure


class CureDiagnostic(str):
    """A rendered cure diagnostic with machine-visible completeness facts."""

    def __new__(cls, detail, blind=False, ambiguous=0):
        out = str.__new__(cls, detail)
        out.blind = bool(blind)
        out.ambiguous = int(ambiguous or 0)
        return out


def _cure_index(root=None, trunk="origin/main"):
    """({sha: (branch, tip) | (AMBIGUOUS, carriers)} for commits ahead of trunk.

    The index keeps the maximal carrier tips for each commit. A descendant tip
    removes its ancestor, including a merge descendant that contains both prior
    siblings. Incomparable tips remain AMBIGUOUS: ref-name order has no authority
    to decide which branch is the cure a reviewer should receive."""
    from . import vcs
    root = root or (_repo_info() or {}).get("repo")
    if not root:
        return None, "no repository root"
    back = vcs.backend(root)
    rc, out, err = back.text(root, "for-each-ref", "--no-merged", trunk,
                             "--format=%(refname:short) %(objectname)",
                             "refs/heads/")
    if rc != 0:
        return None, "for-each-ref failed: %s" % (err or rc)
    maxima, carried = {}, {}
    for line in sorted(out.splitlines()):
        parts = line.split()
        if len(parts) != 2:
            continue
        name, tip = parts
        rc, shas, err = back.text(root, "rev-list", tip, "--not", trunk)
        if rc != 0:
            return None, "rev-list failed on %s: %s" % (name, err or rc)
        commits = set(shas.split())
        carried[tip] = commits
        for sha in commits:
            kept, dominated = [], False
            for prior in maxima.get(sha, ()):
                if prior[1] == tip:
                    # Identical tips carry identical history. The sorted first
                    # branch is the stable display label; branch names never
                    # decide ancestry, but equal content still needs one name.
                    dominated = True
                    kept.append(prior)
                    continue
                if prior[1] in commits:
                    continue                  # the new tip descends from prior
                if tip in carried.get(prior[1], ()):
                    dominated = True          # a prior tip descends from new
                kept.append(prior)
            if not dominated:
                kept.append((name, tip))
            maxima[sha] = kept
    index = {}
    for sha, carriers in maxima.items():
        index[sha] = carriers[0] if len(carriers) == 1 else (
            CURE_AMBIGUOUS, tuple(sorted(carriers)))
    return index, None


def chain_reviewed_tips(row, snap, index=None):
    """{reviewed tip: successor row} — every commit a SAME-CHAIN successor of
    `row`, at any depth, recorded a verdict on.

    A CURE THAT WAS REVIEWED IS NOT AWAITING REVIEW, whatever happened to the
    row that reviewed it afterwards. `carrier` answers who holds the debt now,
    and a successor closed withdrawn is a pass-through there, correctly. That
    is the wrong question for the cure census: a successor that recorded a
    verdict on the cure tip and was later withdrawn still reviewed it, so the
    walk here passes through every successor and keeps each recorded verdict.
    A foreign edge is not this work and is not walked.

    ONLY A VERDICT THAT STANDS AS A REVIEW COUNTS (`_verdict_is_a_review`). A
    successor with no FIX or APPROVE polarity recorded no review — a cancelled
    re-dispatch carries none, so a cure whose only re-dispatch was cancelled
    stays awaiting review and the redispatch door can still reach it; a
    `concur` authorizes nothing; a `supersede` reviews other work. An APPROVE
    retired `tier-unevaluable-parked` was measured to stand on a DARK tier, so
    it is not a review that counts either. Every excluded shape leaves the cure
    VISIBLE as awaiting, never hidden.
    """
    kids = _successor_index(snap) if index is None else index
    root = str(row.get("id") or "")
    seen, frontier, out = {root}, [root], {}
    while frontier:
        pid = frontier.pop()
        for kid in kids.get(pid, ()):
            kid_id = str(kid.get("id") or "")
            if not kid_id or kid_id in seen or not _same_chain(row, kid):
                continue
            seen.add(kid_id)
            frontier.append(kid_id)
            tip = str(kid.get("reviewed_tip") or "").strip().lower()
            if tip and _verdict_is_a_review(kid):
                out.setdefault(tip, kid)
    return out


#: Retire reasons whose record says the retired verdict never stood as a
#: review. `tier-unevaluable-parked` measured the approval tier DARK.
_RETIRED_UNREVIEWED = frozenset({"tier-unevaluable-parked"})


def _verdict_is_a_review(row):
    """True when `row` recorded a FIX or APPROVE verdict that still counts as
    a review of the commit it names (see `chain_reviewed_tips`)."""
    if _replay_polarity(row.get("polarity")) not in ("fix", "approve"):
        return False
    return not (row.get("retired_admin")
                and row.get("retire_reason") in _RETIRED_UNREVIEWED)


def advisory_close_reason(reason):
    """What an ADVISORY close RECORDS for the operator's `reason`.

    PURE AND CALLED FROM BOTH SIDES OF THE IDEMPOTENCE TEST, so a repeated
    `helm dispatch cancel <id> <same why>` re-composes the identical string and
    reconciles instead of refusing. A retry that had to re-type the composed
    form would be a door only its own author could re-open."""
    return _ADVISORY_CLOSE_PREFIX + reason


def advisory_close_error(state):
    """Why this row may NOT be closed as ADVISORY, or None when it may.

    A VERDICT THAT DECLARES NO POLARITY IS ADVISORY BY CONSTRUCTION: it
    authorizes no land and demands no cure, so the only honest terminal left
    for the row is "somebody read this and nothing was owed either way". Every
    other door refuses it BY CORRECT REASONING about a different shape — the
    cancel door because a REVIEWED row is not cancelled, `lr close` because a
    row carrying no exact tip is not a land request at all — and the row is
    left with no door while the burn-down keeps billing it.

    ONE PREDICATE, TWO CALLERS, AND THAT IS THE POINT. `mark_cancel` admits
    the row through this call and `_apply` re-admits the appended event
    through the SAME call, so the writer's projection and the replay cannot
    disagree about which rows the door takes. A divergence here is the abandon
    lesson: events the state machine ignores while the CLI prints CANCELLED
    over a row that stayed live.

    A DECLARED POLARITY KEEPS ITS REFUSAL. An APPROVE authorized a land and a
    FIX demands a cure; closing either as advisory would erase a claim
    somebody made. Those rows keep the byte-identical refusal `mark_cancel`
    has always given, which names `lr close --reason withdrawn` for the FIX
    whose honest answer is that the artifact should not exist.

    A ROW THE LAND-REQUEST LADDER ALREADY RETIRED IS NOT DOORLESS AND IS
    REFUSED. Measured over the live ledger: 29 rows fold to a verdict with no
    polarity and 28 of them already carry a terminal (landed, stranded,
    superseded, withdrawn) — appending a cancel over one of those would be a
    SECOND, contradictory claim about the same work. The question
    is asked through `landreq._retired_by`, the ladder's own answer to "what
    already retired this row", for the reason `obligation.unanswered_fixes`
    states where it asks the same question of the same rows: a second
    retirement predicate beside the one that owns it is free to drift from it.
    """
    if not isinstance(state, dict):
        return "the row is unreadable"
    if str(state.get("status") or "") != "verdict":
        return "only a verdict row can be closed as advisory"
    if _replay_polarity(state.get("polarity")) is not None:
        return "the verdict declares a polarity, so it is not advisory"
    from . import landreq               # DEFERRED — landreq imports us.
    already = landreq._retired_by(state)
    if already:
        return "already retired by %s" % already
    return None


def cure_state(row, index, successors=(), reviewed=None):
    """Which of CURE_* this FIX row is in. `index` None -> CURE_UNKNOWN.

    Pure given the index, so the git cost is paid once by the caller and every
    row is classified from memory. `reviewed` is `chain_reviewed_tips` for this
    row: a cure tip in it is CURE_REVIEWED, answered with the successor that
    recorded the verdict."""
    if index is None:
        return CURE_UNKNOWN, None
    tip = str(row.get("reviewed_tip") or "").strip().lower()
    if not tip:
        return CURE_UNKNOWN, None
    if successors:
        # Somebody IS waiting: a successor row carries this obligation and the
        # normal open-frontier surfaces already show it.
        return None, None
    hit = index.get(tip)
    if hit is None:
        # Not ahead of trunk on any live branch. Either it LANDED (the work is
        # on trunk and the row is done) or nothing carries it at all. The
        # caller separates those with one ancestry read, because doing it here
        # would put a git call back on the per-row path.
        return CURE_NO_BRANCH, None
    branch, btip = hit
    if branch == CURE_AMBIGUOUS:
        shown = ", ".join("%s@%s" % (name, sha[:12])
                          for name, sha in btip)
        return CURE_UNKNOWN, ("ambiguous live cure carriers: " + shown)
    if btip == tip:
        return CURE_AUTHOR_OWES, (branch, btip)
    if reviewed and btip.lower() in reviewed:
        return CURE_REVIEWED, (branch, btip, reviewed[btip.lower()])
    return CURE_AWAITING, (branch, btip)


def cure_candidate(row, snap=None, index=None, cycles=None):
    """Could this row be a cure awaiting review, WITHOUT asking a repository?

    THE INDEX-FREE CLAUSES OF cure_state, and it exists so a surface counting
    rows it CANNOT PLACE admits the same population the classifier would.
    cure_state needs an `index`, and an index is built from a ROOT —
    so a row whose repository cannot be resolved can never reach clause 3 (a
    live branch carrying reviewed_tip, ahead of it). It is UNKNOWN, not absent.

    cure_state cannot answer this itself: given `index=None` it returns
    CURE_UNKNOWN on its FIRST line, before it looks at reviewed_tip or at
    successors, so it cannot discriminate one unplaceable row from another.
    Hence a predicate rather than a call — kept HERE, beside cure_state, so the
    two cannot drift apart unnoticed.

    Counting on polarity alone was the earlier bug in its second form. The
    first form said 7 unplaceable where 1 carried polarity=fix; this one would
    say 'unplaceable' for a fix row whose reviewed_tip is missing (nothing was
    ever cured) or whose successor already holds it (a reviewer IS waiting, and
    the open-frontier surfaces already show it). Both inflate the number the
    owner is asked to burn down, which is the whole defect this bucket exists
    to avoid.

    PARITY IS A TEST, NOT A COMMENT: every row cure_state calls CURE_AWAITING
    satisfies this predicate, and tests/test_web_owed.py asserts it rather than
    trusting this docstring.
    """
    if not isinstance(row, dict) or row.get("polarity") != "fix" \
            or not str(row.get("reviewed_tip") or "").strip() \
            or _close_retired_by(row):
        return False
    if not isinstance(snap, dict):
        return False
    successors = _successor_index(snap) if index is None else index
    cycles = _cycle_components(successors) if cycles is None else cycles
    return carrier(row, snap, successors, cycles) is None


def cured_unwitnessed(snap, ids=None, index=None, root=None,
                      trunk="origin/main"):
    """FIX debt with a cure, no retirement, and no live carrying successor.

    Ledger filters run before the Git index. A discharged/withdrawn/otherwise
    retired FIX is history, not debt. A raw supersedes edge is not a holder:
    cancelled, withdrawn and other non-carrying successors are pass-throughs,
    so this reuses carrier(), the obligation frontier's existing predicate.
    ``ids`` contains resolved exact ids; prefix resolution belongs at the CLI
    boundary, where ambiguity can be refused rather than widened here."""
    wanted = set(str(i) for i in ids) if ids else None
    candidates = []
    for row in snap.values() if isinstance(snap, dict) else ():
        if not isinstance(row, dict) or row.get("polarity") != "fix" \
                or not str(row.get("reviewed_tip") or "").strip() \
                or _close_retired_by(row):
            continue
        if wanted is not None and str(row.get("id") or "") not in wanted:
            continue
        candidates.append(row)
    if not candidates:
        return [], None
    if index is None:
        index, err = _cure_index(root=root, trunk=trunk)
        if err:
            return [], CureDiagnostic(err, blind=True)
    successors = _successor_index(snap)
    cycles = _cycle_components(successors)
    out, diagnostics, ambiguous = [], [], 0
    for row in candidates:
        state, where = cure_state(
            row, index, carrier(row, snap, successors, cycles) is not None,
            reviewed=chain_reviewed_tips(row, snap, successors))
        if state == CURE_UNKNOWN and isinstance(where, str):
            diagnostics.append("%s: %s" % (
                str(row.get("id") or "")[:12], where))
            if str(where).startswith("ambiguous live cure carriers:"):
                ambiguous += 1
            continue
        if state != CURE_AWAITING:
            continue
        branch, tip = where
        ahead = 0
        from . import vcs
        base = root or (_repo_info() or {}).get("repo")
        if base:
            rc, n, _e = vcs.backend(base).text(
                base, "rev-list", "--count",
                "%s..%s" % (row["reviewed_tip"], tip))
            ahead = int(n) if rc == 0 and n.strip().isdigit() else 0
        out.append((row, (branch, tip, ahead)))
    out.sort(key=lambda p: str(p[0].get("ts") or ""))
    detail = "; ".join(diagnostics)
    return out, CureDiagnostic(detail, ambiguous=ambiguous) if detail else None


def cure_eligible(snap, ids=None):
    """The rows a cure census MAY measure — ONE owner for the predicate.

    IT LIVES OUT HERE SO THE SET-ASIDE COUNT AND THE MEASURED POPULATION COME
    FROM ONE PREDICATE. With this inline in `cured_by_repo`, a caller that needs
    to know how many candidates its own scope holds back has to re-derive it, and
    a second derivation of "which rows are cure candidates" is how a set-aside
    count disagrees with the census printed beside it (task/2437 round two,
    finding 6). The CLI counts with this and then hands the surviving ids back
    through `ids`, so the number and the population are the same predicate.

    `ids=None` MEANS EVERY ELIGIBLE ROW AND `ids=[]` MEANS NONE. The old
    `if ids` read an empty selection as "no selection given" and silently
    measured the WHOLE ledger — the exact direction a scoped caller must not be
    widened in."""
    wanted = None if ids is None else set(str(i) for i in ids)
    return [row for row in snap.values() if isinstance(row, dict)
            and row.get("polarity") == "fix"
            and str(row.get("reviewed_tip") or "").strip()
            and not _close_retired_by(row)
            and (wanted is None or str(row.get("id") or "") in wanted)]


def cured_by_repo(snap, ids=None, trunk=None):
    """Per-repository cure census with explicit completeness facts.

    Rows are grouped by persisted repo_id and each group uses a verified carried
    checkout. A blind repository is never converted into CURE_NO_BRANCH by some
    other process cwd. `scanned_repos` distinguishes a partial fleet answer from
    total blindness; `ambiguous` is first-class population, not buried in prose.
    """
    from . import landreq, obligation
    eligible = cure_eligible(snap, ids)
    if not eligible:
        return [], [], {"eligible": 0, "scanned_repos": 0,
                        "blind_repos": 0, "blind_rows": 0, "ambiguous": 0}
    groups = {}
    for row in eligible:
        repo = row.get("repo_id")
        repo = repo if isinstance(repo, str) else ""
        groups.setdefault(repo, []).append(row)
    out, problems, scanned = [], [], 0
    blind_repos = blind_rows = ambiguous = 0
    for repo, candidates in sorted(groups.items()):
        rids = sorted(str(row.get("id") or "") for row in candidates)
        if not repo or "\0" in repo or repo != repo.strip():
            blind_repos += 1
            blind_rows += len(candidates)
            problems.append("repo %r has no usable repo_id — %d row(s) UNKNOWN"
                            % (repo, len(candidates)))
            continue
        scoped = {rid: row for rid, row in snap.items()
                  if isinstance(row, dict) and row.get("repo_id") == repo}
        carried = sorted({row.get("repo_root") for row in scoped.values()
                          if isinstance(row.get("repo_root"), str)
                          and row.get("repo_root")})
        root = next((placed for placed in (
            obligation._root_for_repo(repo, candidate)
            for candidate in carried) if placed), None)
        if root is None:
            root = obligation._root_for_repo(repo)
        if root is None:
            blind_repos += 1
            blind_rows += len(candidates)
            problems.append("repo %r has no verified checkout — %d row(s) UNKNOWN"
                            % (repo, len(candidates)))
            continue
        try:
            resolved_trunk, _pin, _target, terr = landreq._close_trunk(
                candidates[0], repo, trunk)
            if terr:
                part, err = [], CureDiagnostic(
                    "trunk unreadable: %s" % terr, blind=True)
            else:
                part, err = cured_unwitnessed(
                    scoped, ids=rids, root=root, trunk=resolved_trunk)
        except Exception as exc:              # noqa: BLE001 — source UNKNOWN
            part, err = [], CureDiagnostic(str(exc), blind=True)
        if err and getattr(err, "blind", False):
            blind_repos += 1
            blind_rows += len(candidates)
            problems.append("repo %r is unreadable — %d row(s) UNKNOWN: %s"
                            % (repo, len(candidates), err))
            continue
        scanned += 1
        out.extend(part or ())
        if err:
            ambiguous += int(getattr(err, "ambiguous", 0) or 0)
            problems.append("repo %r is partial: %s" % (repo, err))
    out.sort(key=lambda p: str(p[0].get("ts") or ""))
    return out, problems, {"eligible": len(eligible),
                           "scanned_repos": scanned,
                           "blind_repos": blind_repos,
                           "blind_rows": blind_rows,
                           "ambiguous": ambiguous}


def owed(snap, index=None):
    """The rows still OWED: open, and with no live successor carrying them.

    THE ONE PLACE the four consumers ask the supersession question. They asked
    it four different ways before — three tested status alone and one walked the
    frozen pointer — so a row could be owed on one surface and discharged on
    another. The index is built ONCE here and threaded, because carrier() over
    a 1,300-row ledger per row is the shape that makes a predicate too expensive
    to adopt.
    """
    if not isinstance(snap, dict):
        return []
    index = _successor_index(snap) if index is None else index
    cycles = _cycle_components(index)
    return [r for r in snap.values()
            if isinstance(r, dict) and _open(r)
            and not carrier(r, snap, index, cycles)]


def _superseded_by_live(row, snap):
    """True when a live successor holds this row's obligation.

    DELEGATES to carrier(). It used to walk the frozen superseded_by pointer,
    which names the FIRST successor forever — so when that successor was
    cancelled and a SIBLING took the work, this said "nobody carries it" and the
    parent read as owed. Same question, better walk; keeping two
    implementations would be the second opinion nobody asked for.
    """
    return carrier(row, snap) is not None


def untriaged(row, snap):
    """(label, reason) — why `triage` deliberately does not re-measure `row`.

    THE SKIP USED TO BE SILENT. `triage <id>` intersects the named ids with
    `owed()` and printed only the survivors, so a VERDICTED row answered with
    rc 0 and ZERO BYTES — byte-identical to a garbage id (measured
    2026-08-11T00:20Z: an open row printed its line while a verdicted row and
    a 32-hex token resolving to nothing produced the same empty answer as
    each other). Silence is the one failure mode nobody re-checks, and here
    it meant "your id is a typo" and "this work is finished" were the same
    sentence.

    THE EXCLUSION ITSELF IS CORRECT — triage measures the owed frontier, and
    re-measuring a closed row asks a reader to act on finished work — so the
    cure is not to measure more, it is to make the exclusion say itself. This
    names each excluded state in the row's own words: the terminal status
    (with the verdict's polarity, since that is what closed it), the hold and
    its recorded reason, or the live successor that carries an open-but-
    superseded row's obligation.
    """
    status = str(row.get("status") or "")
    if row.get("verdict_retracted"):
        return "RETRACTED", ("not triaged: verdict RETRACTED (was %s) — "
                             "successor %s"
                             % (str(row.get("retracted_polarity")
                                    or "undeclared").upper(),
                                str(row.get("retract_successor")
                                    or "none")[:12]))
    if status == "verdict":
        return "VERDICT", ("not triaged: closed by %s verdict"
                           % (row.get("polarity") or "undeclared").upper())
    if status == "cancelled":
        return "CANCELLED", ("not triaged: cancelled (%s)"
                             % (row.get("cancel_reason")
                                or "no reason recorded"))
    if status == "held":
        return "HELD", ("not triaged: held (%s)%s"
                        % (row.get("hold_reason") or "no reason recorded",
                           _held_landing_note(row)))
    kid = carrier(row, snap) if isinstance(snap, dict) else None
    if kid is not None:
        return "CARRIED", ("not triaged: open but superseded — %s carries "
                           "the obligation" % str(kid.get("id") or "?")[:12])
    # Unreachable through the CLI today (an open uncarried row is owed and
    # prints its measurement), kept honest rather than asserted away: a state
    # this function does not recognise must still say WHICH state it saw.
    return (status.upper() or "?"), ("not triaged: state %s"
                                     % (status or "unrecorded"))


def _held_landing_note(row, trunk="origin/main"):
    """" — WORK ALREADY ON TRUNK ...", or "" when it is not, or UNKNOWN.

    A HOLD RECORDS WHY SOMEBODY STOPPED, AND NEVER WHETHER THE REASON STILL
    STANDS. The reason is written once, at the moment of holding, and the row
    then leaves `--open` and `--issued` — the two views every seat reads — so
    nothing revisits it. A row whose lane content reached trunk in the
    meantime owes nothing by construction, and the surface that would say so
    never asked: the module's landedness classifier takes only rows whose
    polarity is "fix" AND that carry a `reviewed_tip`, and a held row has
    neither, so two independent clauses exclude every one of them.

    IT LIVES AT TRIAGE RATHER THAN IN THE LISTING, which is this module's own
    ruling about re-measurement and not a fresh preference — `_fmt` states it
    for the clearspan stamp: no re-measure on the hot list path, a row's
    claims re-measure on demand, where the cost is the point of the visit.
    Triage is that visit. The caller named this row.

    BOTH INSTRUMENTS RUN AND THE ANSWER SAYS WHICH ONE ANSWERED. Ancestry
    proves the reviewed commit is reachable; patch identity proves the
    CONTENT arrived under other object ids, which is what a rebased land
    produces and what ancestry reports absent with perfect honesty. Those are
    different evidence and a reader is owed the difference — folding them
    into one word would rebuild, one layer up, the silence this exists to end.

    IT SAYS NOTHING IT CANNOT MEASURE AND IT CLOSES NOTHING. An unreadable
    repository, an unresolvable tip or an underivable sequence renders
    UNKNOWN, never "not landed", because a blind instrument that reports a
    negative is the one failure this surface must not have. And a landed row
    is still only DESCRIBED: a hold can outlive its lane's content when the
    obligation covers something the land did not carry, so the disposition
    stays with the human who reads it."""
    from . import obligation, vcs         # module convention: 8 siblings here
    tip = str(row.get("tip") or row.get("ref") or "").strip()
    if not tip:
        return ""
    repo = row.get("repo_id")
    repo = repo if isinstance(repo, str) and repo.strip() else ""
    if not repo:
        return " — LANDED-CHECK UNKNOWN (row names no usable repo_id)"
    root = obligation._root_for_repo(repo, row.get("repo_root")) \
        or obligation._root_for_repo(repo)
    if not root:
        return " — LANDED-CHECK UNKNOWN (no verified checkout for this repo)"
    try:
        state = vcs.backend(root).landed_state(root, tip, trunk)
    except Exception as exc:                     # the read is best-effort
        return " — LANDED-CHECK UNKNOWN (%s)" % type(exc).__name__
    if state == vcs.ANCESTOR:
        return (" — WORK ALREADY ON TRUNK: every commit this lane carries is "
                "reachable from %s (ANCESTRY). The hold may be moot; read it "
                "before acting." % trunk)
    if state == vcs.PATCH_EQUIVALENT:
        return (" — WORK ALREADY ON TRUNK: every commit this lane carries is "
                "on %s under a DIFFERENT object id (PATCH IDENTITY — a "
                "rebased land, which ancestry reports absent). The hold may "
                "be moot; read it before acting." % trunk)
    if state == vcs.NOT_ANCESTOR:
        # A NEGATIVE IS STATED, NEVER RENDERED AS SILENCE, and this is the
        # rung the rest of this function's honesty stands or falls on. The
        # trunk ref is a REMOTE-TRACKING SNAPSHOT that moves only on fetch, so
        # a checkout that has not fetched answers NOT_ANCESTOR for work that
        # IS on trunk — and an empty string for that is byte-identical to the
        # answer for work that genuinely never landed. That is the same
        # blindness the three UNKNOWN branches above exist to refuse, arriving
        # through the likeliest door of all: every checkout drifts between
        # fetches, and this runs on demand rather than after one.
        #
        # SO THE READING NAMES ITS OWN HORIZON. What this can honestly say is
        # not "not landed" but "not on this ref AS THIS CHECKOUT LAST SAW IT",
        # with the ref, its current tip and when it last moved here — three
        # facts a reader can act on, and the same disclosure `gate show`
        # already makes about the identical hazard. A freshness this function
        # cannot read is omitted rather than guessed.
        return " — not on %s as this checkout last saw it%s" % (
            trunk, _trunk_horizon(root, trunk))
    return " — LANDED-CHECK UNKNOWN (%s)" % state


def _trunk_horizon(root, trunk):
    """" (<ref> = <tip>, last moved here <when>)", or as much of it as is
    readable, or "" — the freshness of the snapshot a negative was read from.

    NEVER GUESSED AND NEVER PARTIAL-IN-A-MISLEADING-WAY. The reflog of a
    remote-tracking ref records when THIS checkout moved it, which is the
    question a reader has; a checkout that has never fetched has no entry, and
    saying nothing is correct there. The tip alone still helps — a reader who
    recognises it as old knows to fetch — so the two facts degrade
    independently rather than all-or-nothing."""
    from . import vcs
    be = vcs.backend(root)
    tip = when = ""
    rc, out, _err = be.text(root, "rev-parse", "--short", trunk)
    if rc == 0 and out.strip():
        tip = out.strip()
    rc, out, _err = be.text(root, "reflog", "show", "--date=iso", "-1", trunk)
    if rc == 0 and "@{" in out:
        when = out.split("@{", 1)[1].split("}", 1)[0].strip()
    if tip and when:
        return " (%s = %s, last moved here %s)" % (trunk, tip, when)
    if tip:
        return " (%s = %s; this checkout records no fetch for it)" % (trunk, tip)
    return ""


def _not_closed(row):
    """A row that is not terminal -- open OR held. Delegates to query facade."""
    from . import query
    return query.query_is_open(row)


def _successor_frontier(current, rid):
    """(open_successor_ids, unknown_ids) for ONE parent off ONE projection.

    THE ONE OWNER of retip's successor-frontier read, writer and replay both
    (P1 on this verb's second cut: each caller wrote its own
    `supersedes == rid`
    equality, and a not-closed row whose supersedes replays CHAIN_UNKNOWN
    satisfies no equality — so an UNREADABLE successor state fell through as
    "no open successor" at BOTH). Three answers, kept apart the way
    `_replay_chain` keeps its three: `successors` are not-closed rows PROVABLY
    superseding `rid`; `unknown` are not-closed rows whose supersedes is
    CHAIN_UNKNOWN and which might therefore supersede `rid` — a frontier the
    check could not read never reads as clear, so BOTH non-empty answers
    refuse at the callers. The parent's own row is excluded: a row can never
    be its own successor (a parent must exist on the ledger before a child can
    name it), so its own chain corruption says nothing about this frontier."""
    successors, unknown = [], []
    for r in (current or {}).values():
        # THE CHEAP DISCRIMINATOR BEFORE THE PREDICATE, AND THAT ORDERING IS
        # THE WHOLE OF THIS SCAN'S COST. Both gates are pure tests on the same
        # row, so their conjunction is commutative and the RESULT cannot move
        # — but one is a dict lookup and the other is a facade call into
        # `query.query_is_open`, and only a handful of rows on any ledger name
        # this parent at all. Asking liveness first ran the expensive
        # predicate over EVERY row of the projection, once per parent:
        # measured on the live 14,444-event ledger, `_fold` called
        # this 98 times over ~2,650 rows and spent 0.74-1.06s inside 259,881
        # `query_is_open` calls — a term QUADRATIC in the ledger's own
        # history, which is the growth no admission budget can be refit
        # against. Filtering on `supersedes` first leaves a few hundred
        # predicate calls and 0.26-0.38s (helm task/2394).
        if not isinstance(r, dict):
            continue
        sup = r.get("supersedes")
        if sup != rid and sup != CHAIN_UNKNOWN:
            continue
        if not _not_closed(r) or r.get("id") == rid:
            continue
        if sup == rid:
            successors.append(str(r.get("id") or ""))
        else:
            unknown.append(str(r.get("id") or ""))
    return sorted(successors), sorted(unknown)


# THE POST-CREATE MUTATOR EVENTS — the ones that move an ACTIVE obligation.
# Declared as data so the reducer guard and the hostile matrix that proves it
# both read the SAME list; a hand-written test list divorced from production
# is how a fourth event escapes. `close` and its terminal-proof siblings are
# deliberately NOT here: their lifecycle can legitimately apply after a
# verdict, and their exclusivity from retirement is proven separately.
_ACTIVE_ONLY_EVENTS = ("delivered", "verdict", "cancel", "hold", "release",
                       "retip", "superseded", "retarget", "advisory-read",
                       "findings-note")


def _close_position(position):
    """The ledger's own APPEND INDEX for a close event, or None.

    IT IS THE ONLY CROSS-ROW CLOSE ORDER THE RECORD HAS. `close_ts` is a
    whole-second stamp, so two closes inside one second are indistinguishable by
    instant; and a projection's row order is the order rows were OPENED, which is
    not the order they closed — open A then B, close B then A, and any reader
    breaking the tie on row order puts the older close first under a newest-first
    heading. The fold already reads this index (it is how a verdict's position is
    bound), so carrying it onto the closed state costs nothing and lets a reader
    order two same-second closes by the record instead of by a guess.

    None WHERE THE PROJECTION CANNOT KNOW IT, and that is a real distinction
    rather than a default: a single-row projection at the writer holds one event
    and no ledger, so it has no index to report, while every reader that folds the
    whole ledger has one for every close it takes. A row whose close position is
    unknown may therefore never claim the newest place among rows that have one.
    Booleans are refused explicitly — `True` is an int in python and would read
    as append index 1.
    """
    if isinstance(position, bool) or not isinstance(position, int):
        return None
    return position


# WHAT EACH EVENT KIND RECORDS ABOUT THE HAND THAT WROTE IT. Each value is a
# PRECEDENCE tuple of top-level fields naming the ACTING seat, read
# first-resolvable-wins; an empty tuple means this writer records no hand in a
# plain field, and absence there is UNRECORDED rather than a guess.
#
# THE KEY SET IS THE REGISTRY OF EVERY KIND THAT REACHES `ledger_path()`,
# historical spellings included, and it is here rather than at a reader because
# this module is the only place that knows: `intent` and `done` are absent
# because they are appended to `attest_path()`, and a reader keeping its own
# list of kinds cannot tell those apart. A reader that answers a question per
# kind enumerates THIS, so a new producer landing in this module is a red arm
# at the reader instead of a kind that silently contributes nothing.
#
# A `verdict` IS THE ONE KIND WHOSE HAND IS NOT A PLAIN FIELD, hence the empty
# tuple: its author is bound inside `verdict_author_runtime_evidence` and is
# only credible through `_verdict_author_runtime_error`, the same validator
# replay uses before it will record that binding. A reader wanting the verdict
# author must go through that door; there is no field here to read instead.
#
# THE VALUES ARE THE ACTOR AND NEVER THE MENTIONED PARTY. A `close` written by
# a sweep stamps the swept rows' authors into `original_author`, and `custody`
# names the seat custody was assigned TO in `custodian` — neither is the hand,
# so neither is here.
LEDGER_EVENT_ACTORS = {
    # A MOVE PRESERVES `sender` FROM THE OBLIGATION IT CONTINUES and records
    # the hand that actually wrote the row in `acted_by`, so `acted_by` must
    # BEAT the inherited name rather than join it.
    "dispatch": ("acted_by", "sender"),
    "verdict": (),
    "close": ("close_actor", "withdrawing_seat"),
    "retire": ("retire_seat",),
    "cancel": (),
    "hold": (),
    "release": (),
    "delivered": (),
    "notify-failed": (),
    "superseded": (),
    "discharge": (),
    "withdraw": (),
    "abandon": (),
    "close-landed": (),
    "close-correction": (),
    # A VERDICT RETRACTION names its hand, the seat whose door admitted it
    # (the verdict's author, the integrator, or the owner) (task/3060).
    "verdict-retract": ("retract_seat",),
    "custody": (),
    "retip": (),
    # A MODEL RUN'S ADVISORY READ: the seat that recorded it is the hand.
    "advisory-read": ("recorded_by",),
    # THE LOCAL FINDINGS PASS'S NOTE (task/2960) is written by a detached
    # machine process that runs under no seat, so it records no hand at all.
    "findings-note": (),
    # HISTORICAL SPELLINGS, still on the ledger and no longer written: the v1
    # snapshot carried the whole row under no `event` key at all and `retarget`
    # carried it under that one. Both hold a `recipient` and no sender, so the
    # only seat-shaped value on them is the party ASKED.
    "retarget": (),
    "": (),
    # THE v2 OPENERS, read by `_new_state` and no longer written. They credit
    # nobody here, exactly as they did while unregistered; they are listed so
    # the fold's vocabulary (`KNOWN_EVENT_KINDS`) does not call a kind it
    # opens rows from UNKNOWN when one reaches a row that already exists.
    "add": (),
    "posting": (),
}


#: THE KINDS THIS MODULE READS AND NO LONGER WRITES. Every other key of
#: `LEDGER_EVENT_ACTORS` is appended by a writer in this module or one of its
#: satellites; an arm holds the two sets exactly complementary, so a kind the
#: registry carries that nobody writes and nobody declared historical is a
#: dead entry, and a historical spelling a writer starts emitting again is
#: named here as history while it is live.
HISTORICAL_EVENT_KINDS = frozenset(("", "retarget", "add", "posting"))

#: THE FOLD'S VOCABULARY: every dispatch-ledger event kind this helm knows.
#: DERIVED, NEVER TYPED. `LEDGER_EVENT_ACTORS` is the one literal a new event
#: kind is added to (it also owes a decision about which field names its
#: hand), and an arm fails when any writer emits a kind that literal lacks —
#: so the fold learns a kind in the same edit that teaches the writer to emit
#: it, and there is no second list to forget.
#:
#: WHAT IT PROTECTS, AND WHAT IT CANNOT. A helm older than the ledger meets
#: events it has no arm for. Its fold tolerates them (a reader keeps reading),
#: but it does not advance the row's `seq` past them, so every seq it would
#: WRITE on that row reuses the unseen event's seq, and every current reader
#: drops the write as out of sequence. The fold therefore records each such
#: kind on the row (`UNKNOWN_KINDS_FIELD`), readers print it, and every writer
#: refuses the row before it computes a seq (`unknown_kinds_refusal`, at
#: `_resolve_row`, the door every writer passes).
#:
#: THAT PROTECTION IS FORWARD ONLY. A binary cannot learn a check it does not
#: carry: a helm built before this constant existed refuses nothing, and the
#: kinds it cannot read include kinds this constant already names. Such a
#: binary is reached only by the stale-tree line `cli.stale_tree_warning`
#: prints, and by the seq-collision doctor row that finds what it wrote.
KNOWN_EVENT_KINDS = frozenset(LEDGER_EVENT_ACTORS)

#: The state field on which the fold records the kinds it does not know. It is
#: ABSENT on every row whose events are all known, so such a row folds byte
#: for byte as it did before this field existed.
UNKNOWN_KINDS_FIELD = "unknown_event_kinds"


def _event_kind_label(kind):
    """ONE printable, bounded spelling of an event kind a reader cannot vouch
    for. A kind arrives from the ledger, so it may be any JSON value; a refusal
    and a listing print it to an operator's terminal."""
    text = kind if isinstance(kind, str) else "<%s>" % type(kind).__name__
    text = "".join(c if c.isprintable() else "?" for c in text)[:64]
    return text or '""'


def _is_known_kind(kind):
    """Whether this helm's fold knows `kind`. A missing `event` key is the v1
    snapshot spelling, registered as ""."""
    if kind is None:
        return True
    return isinstance(kind, str) and kind in KNOWN_EVENT_KINDS


def _note_unknown_kind(state, kind):
    """`state` with `kind` added to its unknown kinds — a NEW dict, sorted and
    de-duplicated, so two folds of one ledger record one value."""
    seen = set(state.get(UNKNOWN_KINDS_FIELD) or ())
    seen.add(_event_kind_label(kind))
    out = dict(state)
    out[UNKNOWN_KINDS_FIELD] = tuple(sorted(seen))
    return out


def unknown_event_kinds(row):
    """The event kinds this helm's fold met on `row` and has no arm for, as a
    tuple; empty when there are none, or when `row` is not a folded row."""
    kinds = row.get(UNKNOWN_KINDS_FIELD) if isinstance(row, dict) else None
    return tuple(kinds) if isinstance(kinds, (list, tuple)) else ()


def unknown_kinds_note(row):
    """The reader's sentence for a row carrying unknown kinds, or "".

    A READER KEEPS READING: the row is still listed, and this says why its
    state is not to be trusted from this binary."""
    kinds = unknown_event_kinds(row)
    if not kinds:
        return ""
    return ("LEDGER NEWER THAN THIS HELM (unknown event kind%s: %s)"
            % ("" if len(kinds) == 1 else "s", ", ".join(kinds)))


def _this_helm_tree():
    """The directory holding this helm package — the tree a stale binary came
    from, which is the one the cure fast-forwards."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def unknown_kinds_refusal(row, noun="dispatch"):
    """The writer's refusal over a row whose fold met a kind this helm does
    not know, or None.

    REFUSED BEFORE ANY SEQ IS COMPUTED. The fold does not advance `seq` past
    an event it has no arm for, so `row["seq"] + 1` is the seq that event
    already holds: the append would land, pass this binary's own self-check
    (which folds with the same vocabulary) and be dropped by every current
    reader as a collision. It also misreads the row itself — a cancelled row
    can read open — so no decision this binary makes about it is sound."""
    kinds = unknown_event_kinds(row)
    if not kinds:
        return None
    tree = _this_helm_tree()
    return ("%s %s carries event kind%s %s that this helm does not know — "
            "this helm (%s) is older than the ledger, so it misreads the row "
            "and the seq it would write would reuse the seq of the event it "
            "cannot see, which every current reader drops. Nothing was "
            "written. Run the trunk helm, or fast-forward this tree: "
            "git -C %s merge --ff-only origin/main"
            % (noun, str(row.get("id") or "?")[:12],
               "" if len(kinds) == 1 else "s", ", ".join(kinds), tree, tree))


#: (kind, field) -> WHY THAT SEAT-SHAPED VALUE NAMES SOMEBODY OTHER THAN THE
#: HAND. The complement of `LEDGER_EVENT_ACTORS` and the same module's
#: business: a registry that says which field IS the actor is only half a law,
#: and the missing half is the one a widening reader breaks. A census closing
#: OTHER people's rows stamps their names into the close event, so reading one
#: of these would report a long-departed seat as acting on the day of the sweep.
#:
#: Every entry is a seat-shaped value on a kind the registry covers, which is
#: what makes it worth pinning: the next hand to widen the registry meets the
#: reason instead of the field.
LEDGER_NOT_AN_ACTOR = {
    ("advisory-read", "author_model"):
        "a MODEL id — the lane author's model the read is compared against — "
        "and never a seat; the hand is `recorded_by`",
    ("advisory-read", "author_model_source"):
        "not a seat at all: the word `runtime` or `declared`, saying where "
        "author_model came from",
    ("findings-note", "reader"):
        "the MODEL FAMILY that read the diff (`qwen27`), which a seat family "
        "shares by name — never a seat's act: the pass is a detached machine "
        "process and this kind records no hand",
    ("close", "original_author"):
        "the seat whose row was closed — the mentioned party, and a sweep "
        "closing other people's rows would republish every one of them as "
        "active on the day of the sweep",
    ("close", "confirmation_recipient"):
        "the seat asked to confirm, named by the closing hand rather than "
        "acting here",
    ("dispatch", "recipient"):
        "the seat asked to act, which is the obligation and not the act; a "
        "recipient who never answered would read as having worked",
    ("retip", "identity"):
        "not a seat at all — the writer stores the verification WORD here "
        "(measured over the live ledger: 84 `verified`, 15 `unverified`), so "
        "reading it would mint two fleet-wide seats out of a status field",
    ("custody", "custodian"):
        "the seat custody was handed TO, named by the hand that handed it "
        "over — an assignment, and assignment is the obligation not the act",
    ("verdict", "recipient"):
        "the party ASKED to review, and on the only spelling that carries it "
        "the event is a whole-row snapshot rather than an act (measured over "
        "the live ledger: 3 verdict events carry this key, all of them that "
        "snapshot, all naming one seat)",
    ("retarget", "recipient"):
        "the party asked to act, carried along by a historical whole-row "
        "spelling that records no hand at all",
    ("", "recipient"):
        "the same party on the oldest spelling of all, which had no `event` "
        "key and was the entire row written down",
    ("dispatch", "recipient_display"):
        "the SAME mentioned party as `recipient`, laundered for a terminal — "
        "measured over the live ledger it equals `recipient` on every one of "
        "the 2620 events carrying it, so it is that exclusion's second "
        "spelling and never an independent fact",
    ("verdict", "verdict_author_session"):
        "not a seat at all — the session the author's binding was PROVED in "
        "(measured over the live ledger: 1052 events, 31 distinct uuids), so "
        "reading it would mint a seat per credential home. It is an INPUT to "
        "the validator, which is the only door that credits a verdict",
    ("verdict", "verdict_author_runtime_anchor"):
        "not a seat at all — the hash keying the envelope (measured over the "
        "live ledger: 1052 events, 553 distinct digests), so reading it would "
        "mint a fresh seat per verdict",
}


#: THE STAMP AN ACT THIS PROJECTION CANNOT PLACE IN TIME READS AS. It sorts
#: ABOVE every ISO-8601 stamp, so an undated act is NEWER than any dated one —
#: and the only reader that compares them refuses on newer. That direction is
#: deliberate: an act nobody can date and nobody can attribute must not read as
#: a seat's silence. Measured over the live ledger the day this landed: 0 of
#: 14577 rows carry an unreadable `ts`, so the direction costs nothing now and
#: refuses rather than admits if that ever stops being true.
UNDATED_ACT = "~undated"


def _note_act(store, name, stamp):
    """Keep the NEWEST stamp per casefolded seat name. Nameless is a no-op."""
    key = str(name or "").strip().lstrip("@").casefold()
    if key and stamp > store.get(key, ""):
        store[key] = stamp


def _credit_act(actors, row, before, after):
    """Record what ONE ledger row says about WHEN A SEAT ACTED, at the instant
    the fold decided whether to accept it. Fills `actors` in place; returns
    nothing and raises nothing a caller must handle.

    IT IS CALLED FROM INSIDE THE FOLD BECAUSE THE FOLD IS THE ONLY PLACE THAT
    KNOWS BOTH HALVES. A reader walking the raw ledger beside the fold can see
    the row but not whether this projection TOOK it, and it has to invent an
    identity to validate a verdict's binding against — which is how the same
    event came to supply the recipient its own author was checked against.
    Here the identity is the ACCEPTED state's, the acceptance is the fold's own
    answer, and neither is re-derived.

    TWO STORES, AND THE SECOND ONE IS THE POINT. `validated` is an act whose
    hand the ledger stands behind. `unresolved` is an act that NAMES a seat and
    whose hand this projection could not stand behind — a refused transition, or
    a verdict the fold accepted while recording no author at all (`mark_verdict`
    with `bind_author` false writes exactly that, and replay accepts it). An
    index with only the first store answers "no recent act by this seat" for
    both cases, and only one of them is silence: the other is RECENT ACTIVITY ON
    THIS SEAT'S OBLIGATION THAT NOBODY COULD ATTRIBUTE. Read as silence it
    AUTHORIZES a terminal, which is why less-credited activity is not
    automatically the safe direction.

    A KIND THE REGISTRY GIVES AN EMPTY TUPLE RECORDS NO HAND BY DESIGN and is
    credited in NEITHER store — not even when refused. It cannot bear on any
    particular seat's silence in either direction, and treating one as doubt
    about the addressed party would refuse every seat forever: 1320 of the live
    ledger's 1329 refused rows are `superseded` annotations on already-terminal
    obligations, which are unattributable by design rather than by failure.

    A `verdict` IS THE ONE KIND WHOSE HAND IS NOT A FIELD, so its rule is here
    rather than in the registry: the acting seat is the one its own obligation
    ADDRESSED, and the only thing that credits it is the fold having accepted
    the author binding. A verdict answering an obligation this fold never opened
    has no accepted state, so it names nobody and credits nothing — there is no
    raw field for it to supply its own join with.

    BOTH STATES, AND EACH ANSWERS A DIFFERENT HALF. The IDENTITY comes from
    `before`, because that is the recipient `_apply` validated the binding
    against; the BINDING is read off `after`, because acceptance is a property of
    what the fold produced. The first spelling of this function took one state
    and read the binding off `before` — where those fields cannot be yet — so
    every verdict in the fleet, valid ones included, read as unresolved: 27
    seats carrying doubt and three whose whole authorship history disappeared
    into it. A single-state signature could not have been right.
    """
    if actors is None or not isinstance(row, dict):
        return
    stamp = row.get("ts")
    if not isinstance(stamp, str) or not _valid_ts(stamp):
        stamp = UNDATED_ACT
    accepted = after is not before
    kind = str(row.get("event") or "")
    if kind == "verdict":
        bound = accepted and all(field in (after or {})
                                 for field in VERDICT_AUTHOR_EVIDENCE_FIELDS)
        _note_act(actors["validated" if bound else "unresolved"],
                  (before or after or {}).get("recipient"), stamp)
        return
    for field in LEDGER_EVENT_ACTORS.get(kind, ()):
        if str(row.get(field) or "").strip():
            _note_act(actors["validated" if accepted else "unresolved"],
                      row.get(field), stamp)
            return


def _apply(state, row, current=None, verdicts=None, position=None):
    """Apply only immutable evidence events; malformed later rows preserve the
    preceding good obligation.

    Every compatibility branch is gated to LEGACY-opened states (v1/v2): an
    obligation opened by a v3 dispatch row accepts only strict seq-ordered
    delivery, verdict, cancellation, structured close, and explicit correction
    events, so a well-shaped forged snapshot, ack, or retarget row can never
    close it or move its tip."""
    if str(row.get("id") or "") != state["id"]:
        return state
    event = row.get("event")
    # RETIREMENT IS TERMINAL IN REPLAY TOO, and this must run BEFORE every
    # individual arm. Resolution-time guards cannot help here: a hand-appended
    # event never passes through `_resolve_row`, so without this a forged or
    # stale row could move a retired obligation on the next replay. The state
    # is returned UNCHANGED — including `seq` — so a retired row's history is
    # bit-for-bit stable no matter what is appended after it.
    if event in _ACTIVE_ONLY_EVENTS and _retired_admin_by(state):
        return state
    expected = int(state.get("seq") or 0) + 1
    strict = type(row.get("v")) is int \
        and (row["v"] == 3 or (row["v"] == 4 and event == "verdict")) \
        and type(row.get("seq")) is int and row.get("seq") == expected
    # An OPEN BUILD row has no verdict of its own. Delivered reports close on
    # their own artifact + handoff evidence; landed builds close through an
    # accepted review descendant. Both strict variants apply before ordinary
    # OPEN transitions; every later event then meets CLOSED_STATES.
    if event == "close" and strict \
            and row.get("close_reason") == "delivered-report" \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts, position=position) is None:
        out = dict(state)
        out.update(status="closed", close_reason="delivered-report",
                   close_ts=row.get("ts"), close_seq=_close_position(position),
                   close_evidence=row.get("close_evidence"), seq=expected)
        for key in _CLOSE_STATE_FIELDS["delivered-report"]:
            out[key] = row.get(key)
        return out
    if event == "close" and strict and row.get("close_reason") == "landed" \
            and row.get("close_proof_version") == 2 \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts, position=position) is None:
        out = dict(state)
        out.update(status="closed", close_reason="landed",
                   close_ts=row.get("ts"), close_seq=_close_position(position),
                   close_evidence=None, seq=expected)
        for key in _CLOSE_STATE_FIELDS["landed"]:
            out[key] = row.get(key)
        return out
    # #177 — the polarity-less row's one terminal. Same early-arm shape as
    # build-landed: an OPEN row never reaches the verdict-only close arm in
    # the CLOSED_STATES block, and the writer's projection is this same call.
    if event == "close" and strict \
            and row.get("close_reason") == "discharged" \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts, position=position) is None:
        out = dict(state)
        out.update(status="closed", close_reason="discharged",
                   close_ts=row.get("ts"), close_seq=_close_position(position),
                   close_evidence=row.get("close_evidence"), seq=expected)
        for key in _CLOSE_STATE_FIELDS["discharged"]:
            out[key] = row.get(key)
        return out
    # CARRIED ON AN OPEN ROW — the SECOND polarity-less terminal, and the
    # arm the comment one rung up did not yet have a sibling for. `carried`
    # was registered in CLOSE_REASONS, in the polarity table (whose tuple
    # already admits None), in the event-field table and in the verdict-only
    # arm below, and in NONE of those places is an OPEN row reachable: an
    # OPEN row never reaches the CLOSED_STATES block, exactly as the
    # discharged comment says. So every dry run said the door would open and
    # every real close appended an event that no arm folded.
    #
    # THE SILENT HALF IS WHY THIS IS A CURE AND NOT A FEATURE. `_apply` is
    # also the WRITER'S projection, so an unmatched close returned `state`
    # unchanged and the writer reported success: the event landed in the
    # ledger, the row stayed open, and the caller was told it had closed.
    #
    # The proof is unchanged and is NOT weakened here: `_close_event_error`
    # runs first and its `carried` arm re-derives `carriage_proof` against
    # this same snapshot, comparing the recorded (base, tip) and witness to
    # the re-derivation. This arm decides only WHERE the fold happens.
    #
    # AND ONLY A LIVE ROW FOLDS HERE. A verdicted row's carried close keeps
    # the arm it always had in the CLOSED_STATES block, so the reason folds
    # one way per kind of row; a cancelled or closed row is refused by the
    # validator itself, because retirement is terminal.
    if event == "close" and strict \
            and row.get("close_reason") == "carried" \
            and state.get("status") not in CLOSED_STATES \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts, position=position) is None:
        out = dict(state)
        out.update(status="closed", close_reason="carried",
                   close_ts=row.get("ts"), close_seq=_close_position(position),
                   close_evidence=row.get("close_evidence"), seq=expected)
        for key in _CLOSE_STATE_FIELDS["carried"]:
            out[key] = row.get(key)
        return out
    # ADMINISTRATIVE RETIREMENT — the terminal that CLAIMS NOTHING ABOUT THE
    # WORK. Every other terminal on this ledger asserts something happened
    # (the change landed, was superseded, was withdrawn, its evidence was
    # destroyed); this one asserts only that the row's PROOF CHAIN was
    # measured permanently unreachable, so no reader can ever act on it and
    # no biller should keep charging for it. It is deliberately admitted for
    # OPEN and HELD rows as well as verdicted ones — an unreachable row bills
    # from whatever stage it is stuck in, and a terminal that only reached
    # verdicts would leave the OPEN half immortal, which is the very defect
    # this event exists to end.
    #
    # THE REPLAY ADMITS EXACTLY WHAT THE WRITER ADMITS (the abandon lesson,
    # one arm up): a divergence here grows events the state machine ignores
    # while the CLI prints RETIRED over a row that stayed live.
    if event == "retire" and strict \
            and state.get("status") in _RETIRABLE_STATUSES \
            and not _close_retired_by(state) and not state.get("retired_admin"):
        reason = row.get("retire_reason")
        measurement, m_err = _clean(
            row.get("retire_measurement"), "retire measurement",
            _RETIRE_MEASUREMENT_CAP)
        seat, s_err = _clean(row.get("retire_seat"), "retire seat", 64)
        note = row.get("retire_note")
        note_err = None
        if note is not None:
            note, note_err = _clean(note, "retire note", _RETIRE_NOTE_CAP)
        # `_TOKEN` ON THE SEAT, because the WRITER requires it and the two
        # must admit the same shapes. This arm was the more permissive half —
        # the harmless direction of the abandon divergence, but a divergence
        # all the same, and the law that governs this arm is quoted three
        # lines above it. A seat is an ADDRESS: an actor field a reader
        # cannot resolve to a seat makes the audit trail unfollowable.
        if reason in RETIRE_REASONS and not m_err and measurement \
                and not s_err and seat and _TOKEN.fullmatch(seat) \
                and not note_err \
                and type(row.get("retire_proof_version")) is int \
                and row.get("retire_proof_version") == _RETIRE_PROOF_V \
                and _valid_ts(row.get("ts")):
            out = dict(state)
            out.update(retired_admin=True, retire_reason=reason,
                       retire_measurement=measurement, retire_seat=seat,
                       retire_note=note, retire_ts=row.get("ts"),
                       retire_proof_version=_RETIRE_PROOF_V, seq=expected)
            return out
    # TERMINAL IS IMMUTABLE except for one NARROW reconciliation annotation:
    # a FIX/SUPERSEDE verdict whose contrary physical land was later resolved
    # by an approved superseding round. DISCHARGE does not rewrite the verdict,
    # tip, or status; it retires only the operational debt while preserving the
    # contradiction as history. Cancelled rows and every other post-terminal
    # event remain inert.
    if state.get("status") in CLOSED_STATES:
        # One explicit correction may follow a historical cancellation. It does
        # not infer from the cancel prose and does not erase it: the projected
        # row keeps cancel_reason plus a correction marker while becoming the
        # canonical delivered-report terminal only after exact artifact/report
        # references are recorded.
        if event == "close-correction" and strict \
                and _delivered_report_event_error(row, state, correction=True) is None:
            out = dict(state)
            out.update(status="closed", close_reason="delivered-report",
                       close_ts=row.get("ts"),
                       close_evidence=row.get("close_evidence"),
                       delivered_report_correction=True, seq=expected)
            for key in _CLOSE_STATE_FIELDS["delivered-report"]:
                out[key] = row.get(key)
            return out
        # THE VERDICT RETRACTION (task/3060) — the second narrow exception to
        # "terminal is immutable", and like the first it rewrites nothing. The
        # verdict event stays exactly as appended; this LATER fact withdraws
        # its authority. The projected polarity becomes RETRACTED and the
        # original moves to `retracted_polarity`, which is the fail-safe
        # encoding: every authority reader asks `== "approve"` or
        # `in ("fix", "supersede")`, so a retracted row authorizes, contests
        # and discharges nothing even at a reader nobody taught this word.
        # THE REPLAY ADMITS EXACTLY WHAT THE WRITER ADMITS: both run
        # `_retract_record` over the same event and the same state.
        if event == RETRACT_EVENT and strict:
            fields, err = _retract_record(row, state)
            if err:
                return state
            out = dict(state)
            out.update(fields)
            out.update(polarity=RETRACTED, verdict_retracted=True,
                       seq=expected)
            return out
        # CLOSED-BY-LANDING is a monotonic historical fact: once Git proved an
        # UNDECLARED reviewed change on one sampled trunk, no later event or ref
        # movement rewrites that receipt or the still-undeclared verdict.
        if state.get("closed_by_landing") or state.get("abandoned"):
            return state
        if event == "abandon" and state.get("status") == "verdict" \
                and state.get("kind") == "review" \
                and state.get("polarity") in _WORK_POLARITIES \
                and strict and not state.get("discharged") \
                and not state.get("withdrawn") \
                and not state.get("close_reason"):
            reviewed = str(row.get("reviewed_tip") or "")
            repo_id, repo_err = _clean(row.get("repo_id"), "abandon repo id", 4096)
            reason, reason_err = _clean(row.get("reason"), "abandon reason", 256)
            stamp = row.get("ts")
            if reviewed == state.get("reviewed_tip") \
                    and _FULL_TIP.fullmatch(reviewed) \
                    and not repo_err and repo_id == state.get("repo_id") \
                    and os.path.isabs(repo_id) \
                    and foldckpt.realpath(repo_id) == repo_id \
                    and not reason_err and reason \
                    and row.get("object_state") == "missing" \
                    and row.get("object_proof_mode") == "cat-file-batch-check" \
                    and type(row.get("object_proof_version")) is int \
                    and row.get("object_proof_version") == 1 \
                    and row.get("trunk_mention_state") == "none" \
                    and type(row.get("trunk_mention_proof_version")) is int \
                    and (row.get("trunk_mention_proof_mode"),
                         row.get("trunk_mention_proof_version")) in (
                             ("structured-message-scan", 1),
                             ("structured-message-and-tag-scan", 2)) \
                    and row.get("branch_state") in ("none", "merged") \
                    and row.get("branch_proof_mode") == "git-ref-and-ancestry" \
                    and type(row.get("branch_proof_version")) is int \
                    and row.get("branch_proof_version") == 1 \
                    and row.get("worktree_state") in ("none", "clean") \
                    and row.get("worktree_proof_mode") == "git-worktree-status" \
                    and type(row.get("worktree_proof_version")) is int \
                    and row.get("worktree_proof_version") == 1 \
                    and row.get("land_state") == "UNKNOWN" \
                    and _valid_ts(stamp):
                out = dict(state)
                out.update(abandoned=True, abandon_reason=reason,
                           abandon_ts=stamp, abandon_repo_id=repo_id,
                           abandon_object_state="missing",
                           abandon_proof_mode="cat-file-batch-check",
                           abandon_proof_version=1,
                           abandon_trunk_mention_state="none",
                           abandon_trunk_mention_proof_mode=
                           row.get("trunk_mention_proof_mode"),
                           abandon_trunk_mention_proof_version=
                           row.get("trunk_mention_proof_version"),
                           abandon_branch_state=row.get("branch_state"),
                           abandon_branch_proof_mode="git-ref-and-ancestry",
                           abandon_branch_proof_version=1,
                           abandon_worktree_state=row.get("worktree_state"),
                           abandon_worktree_proof_mode="git-worktree-status",
                           abandon_worktree_proof_version=1,
                           abandon_land_state="UNKNOWN", seq=expected)
                return out
        if event == "close-landed" and state.get("status") == "verdict" \
                and state.get("polarity") is None and strict \
                and not state.get("discharged") and not state.get("withdrawn") \
                and not state.get("close_reason"):
            reviewed = str(row.get("reviewed_tip") or "")
            repo_id, repo_err = _clean(row.get("landing_repo_id"),
                                       "landing repo id", 4096)
            trunk_ref = _valid_trunk_ref(row.get("landing_trunk_ref"))
            trunk_sha = str(row.get("landing_trunk_sha") or "")
            mode = row.get("landing_proof_mode")
            version = row.get("landing_proof_version")
            stamp = row.get("ts")
            if reviewed == state.get("reviewed_tip") \
                    and not repo_err and os.path.isabs(repo_id) \
                    and trunk_ref and _FULL_TIP.fullmatch(trunk_sha) \
                    and mode in ("ancestor", "patch-equivalent") \
                    and type(version) is int and version == 1 \
                    and _valid_ts(stamp):
                out = dict(state)
                out.update(closed_by_landing=True,
                           landing_repo_id=repo_id,
                           landing_trunk_ref=trunk_ref,
                           landing_trunk_sha=trunk_sha,
                           landing_proof_mode=mode,
                           landing_proof_version=version,
                           landing_ts=stamp,
                           seq=expected)
                return out
        if event == "discharge" and state.get("status") == "verdict" \
                and state.get("polarity") in ("fix", "supersede") \
                and strict and not state.get("discharged") \
                and not state.get("withdrawn") \
                and not state.get("close_reason"):
            reviewed = str(row.get("reviewed_tip") or "")
            superseding = str(row.get("superseding_tip") or "")
            superseding_id = str(row.get("superseding_id") or "")
            evidence, err = _clean(row.get("discharge_ref"),
                                   "discharge evidence", 256)
            contrary_state = row.get("contrary_state")
            contrary_target = row.get("contrary_target")
            if reviewed == state.get("reviewed_tip") \
                    and _FULL_TIP.fullmatch(superseding) \
                    and superseding != reviewed \
                    and _ID.fullmatch(superseding_id) \
                    and contrary_state in ("landed", "merged-local") \
                    and contrary_target in ("local", "upstream") \
                    and not err:
                out = dict(state)
                out.update(discharged=True,
                           superseding_tip=superseding,
                           superseding_id=superseding_id,
                           discharge_ref=evidence,
                           discharge_ts=row.get("ts"),
                           discharge_contrary=contrary_state,
                           discharge_target=contrary_target,
                           seq=expected)
                return out
        # WITHDRAW is the mirror narrow annotation, for the row whose verdict's
        # CORRECT resolution is "never landed" (no superseding tip will ever
        # exist to discharge it). Same shape: no rewrite of verdict/tip/status,
        # only the operational debt retired, history preserved. A withdrawn row
        # accepts no discharge later (retired once); a discharged row accepts no
        # withdraw (the discharge arm above already returned state unchanged for
        # a discharged row, so the two can never both apply).
        if event == "withdraw" and state.get("status") == "verdict" \
                and state.get("polarity") in ("fix", "supersede") \
                and strict and not state.get("withdrawn") \
                and not state.get("discharged") \
                and not state.get("close_reason"):
            reviewed = str(row.get("reviewed_tip") or "")
            evidence, werr = _clean(row.get("withdraw_ref"),
                                    "withdraw evidence", 256)
            if reviewed == state.get("reviewed_tip") and not werr:
                out = dict(state)
                out.update(withdrawn=True,
                           withdraw_ref=evidence,
                           withdraw_ts=row.get("ts"),
                           seq=expected)
                return out
        # ADVISORY CLOSE — the polarity-less verdict row's one terminal, and
        # the replay half of `mark_cancel`'s second admission. THE REPLAY
        # ADMITS EXACTLY WHAT THE WRITER ADMITS: the same
        # `advisory_close_error` call, the same 256 budget on the same
        # composed string, and the explicit `advisory` marker the writer
        # stamps — so a bare `cancel` event appended over a verdict row (by an
        # older writer, or by hand) still drives nothing.
        if event == "cancel" and strict and row.get("advisory") is True \
                and advisory_close_error(state) is None:
            reason, err = _clean(row.get("reason"), "cancel reason",
                                 _CANCEL_REASON_CAP)
            if err or not reason:
                return state
            # THE RECORDED PREFIX IS NOT RE-CHECKED HERE, deliberately. It is
            # prose the writer composed, and a replay that required today's
            # constant would UN-CANCEL every row closed under an earlier
            # wording the moment the sentence was edited. `advisory` is the
            # binding signal; the reason is what a reader reads.
            out = dict(state)
            out.update(status="cancelled", cancel_reason=reason,
                       cancel_advisory=True, seq=expected)
            return out
        # CLOSE — the one terminal verb's event (helm lr close). Strict-only,
        # verdict-only, exclusivity and per-reason polarity/field validation
        # all owned by `_close_event_error`, the SAME structural rule the writer
        # checks before append. The writer additionally rechecks live family and
        # tier sources under the lock; replay deliberately validates their
        # captured, content-addressed snapshots instead, because mutable roster or
        # policy drift must not resurrect an already-recorded terminal. As with
        # every event in this private 0600 ledger, coherent file tampering is
        # outside the replay model; malformed or internally inconsistent rows are
        # inert, and a field replay would refuse never gets written.
        # The out-of-scope reason writes a `cancel` event and never reaches
        # this arm; discharge/withdraw/close-landed keep their arms above
        # forever, but only new `close` events are ever emitted.
        if event == "close" and strict \
                and _close_event_error(row, state, current=current,
                                       verdicts=verdicts, position=position) is None:
            out = dict(state)
            out.update(close_reason=row["close_reason"],
                       close_ts=row.get("ts"),
                       close_seq=_close_position(position),
                       close_evidence=row.get("close_evidence"),
                       seq=expected)
            for key in _CLOSE_STATE_FIELDS[row["close_reason"]]:
                out[key] = row.get(key)
            return out
        return state
    legacy = state.get("v") != 3
    # Compat replays ONLY rows stamped before the reduced core landed: an
    # event appended today can never drive the removed machinery, however
    # well-shaped. Missing ts is never compat (fail-closed, "~" sorts high).
    compat = legacy and _pre_boundary(row.get("ts"))
    # Historical full-snapshot compatibility.
    if event is None:
        if compat and row.get("status") == "verdict" and row.get("verdict_ref"):
            out = dict(state)
            out.update(status="verdict", verdict_ref=row.get("verdict_ref"),
                       reviewed_tip=row.get("reviewed_tip") or state.get("tip"),
                       migration=None)
            return out
        return state
    if event == "retarget" and compat \
            and _TIP.fullmatch(str(row.get("tip") or "")):
        # Compatibility only for already-written rows; there is no shipping verb.
        out = dict(state)
        out.update(tip=str(row["tip"]).lower(), ref=row.get("ref"),
                   migration=None,
                   seq=_int_seq(row.get("seq"), state.get("seq", 0)))
        return out
    if event == "delivered" and state["status"] == "open" \
            and (strict or compat):
        ref, err = _clean(row.get("delivery_ref"), "delivery ref", 256)
        if err:
            return state
        out = dict(state)
        out.update(delivery="observed", delivery_ref=ref, seq=expected)
        return out
    if event == "verdict" and state["status"] == "open" \
            and (strict or compat):
        reviewed = str(row.get("reviewed_tip") or "").lower()
        # THE REDUCER MUST BUDGET THE SAME STRING THE WRITER DID, and until
        # 2026-08-03 it did not. mark_verdict strips `gate:` tokens before its
        # 256 check (they ADDRESS a receipt, they are not prose — the
        # 2026-08-02 drain) and stores at 4096; this arm re-checked 256 against
        # the FULL string. The disagreement window is exactly the token: a
        # 21-char `gate:` plus 256 chars of statement passes the WRITE and
        # fails the REDUCE, so evidence of 257..277 chars was accepted, written,
        # and then silently dropped here — `err` simply skips the branch below
        # and `state` returns unchanged, leaving the row OPEN with no error
        # raised and nothing printed. MEASURED: four rows, discarded lengths
        # 258/260/261/275, every one inside that window (max possible 277).
        #
        # AND THE DROP IS NOT THE WORST OF IT. The attest fired at WRITE time
        # over the text the writer accepted, so the signature binds evidence
        # this reducer threw away, while the projection renders the later
        # re-mint the signature does not cover. There is no re-sign verb (the
        # attest path is "at most once", by design), so every such row is
        # PERMANENTLY split. Closing the window is the only cure available.
        raw_evidence = str(row.get("verdict_ref") or "")
        evidence, err = _clean(raw_evidence, "verdict evidence", 4096)
        if not err and len(_GATE_TOKEN_RE.sub("", raw_evidence)) > 256:
            err = "verdict evidence over the 256 budget"
        if state.get("tip") and reviewed == state["tip"] and not err:
            out = dict(state)
            # A pre-gate verdict carries no `gate` field, and "" is the TRUE
            # reading of it: that verdict genuinely was not bound to a run.
            # Replay must never invent a binding history did not have.
            recorded = str(row.get("gate") or "")
            out.update(status="verdict", reviewed_tip=reviewed,
                       verdict_ref=evidence, seq=expected,
                       verdict_ts=row.get("ts"), verdict_seq=expected,
                       verdict_version=row.get("v"),
                       gate=recorded if _GATE_ID.fullmatch(recorded) else "",
                       polarity=_replay_polarity(row.get("polarity")))
            # Preserve absent versus malformed observations. They are advisory,
            # not verdict authority; an invalid observation must not erase a FIX.
            out.update({key: row[key] for key in _FINDING_FIELDS if key in row})
            # BASIS: ABSENT STAYS ABSENT, exactly like gate_caps below and for
            # the same reason. The 221 verdicts written before this field
            # existed were never ASKED how they knew — they are UNMARKED, not
            # `unverified`, and collapsing those two would put a confidence
            # claim into 221 rows nobody made one in. A row that HAS the field
            # goes through the fail-closed backstop, so a forged or
            # future-versioned value reads `unverified` rather than borrowing
            # a confidence nobody recorded.
            if "basis" in row:
                out["basis"] = replay_basis(row["basis"])
            # EXIT QUESTION: the same absent-stays-absent split. Historical
            # rows were never asked whether the candidate was worse than main,
            # so replay renders them UNMARKED instead of inventing an answer.
            # A malformed/future answer also earns no blocking claim.
            #
            # PROJECTED FROM THE ROW'S OWN ANSWER, never from one hardcoded
            # name. A branch that fires on "is the answer readable at all"
            # and then writes one fixed name replays every other answer as
            # that one — the projection inventing a block nobody recorded.
            if verdict_exit_answer(row) != "UNMARKED":
                out["exit_answer"] = row["exit_answer"]
                if row["exit_answer"] == "worse-than-main":
                    out["worse_than_main_paths"] = tuple(
                        row["worse_than_main_paths"])
            # THE CO-AUTHOR RECORD: absent stays absent, exactly like the
            # observations above. A verdict written before reviewers could
            # patch says nothing about who else wrote the tree, and a default
            # here would credit the reviewer with every historical cure.
            out.update({key: row[key] for key in _PATCH_FIELDS if key in row})
            # THE REASON A FIX CARRIES NO CURE: absent stays absent, for the
            # same reason. A verdict written before the question was asked
            # answered nothing, and a default would put an answer in it.
            if "no_patch_because" in row:
                out["no_patch_because"] = row["no_patch_because"]
            # ABSENT stays ABSENT. Setting a default here would erase the
            # difference between "written by a writer with no gate" and
            # "written by one whose stamp we could not read".
            if "gate_caps" in row:
                out["gate_caps"] = clean_gate_caps(row["gate_caps"])
            present = [key in row
                       for key in VERDICT_AUTHOR_EVIDENCE_FIELDS]
            if any(present):
                if not all(present):
                    return state
                session = row["verdict_author_session"]
                if not isinstance(session, str) or not session \
                        or _verdict_author_runtime_error(
                            row["verdict_author_runtime_evidence"],
                            state.get("recipient"), session,
                            row["verdict_author_runtime_anchor"]):
                    return state
                out.update({key: row[key]
                            for key in VERDICT_AUTHOR_EVIDENCE_FIELDS})
            # Preserve malformed tier claims for the authority reader to deny;
            # dropping them would turn a contradictory record into pre-tier.
            out.update({key: row[key] for key in VERDICT_TIER_FIELDS if key in row})
            return out
        return state
    # CANCEL: honest terminal abandonment of an OPEN dispatch (reviewer gone,
    # work moot). v3-native — no compat history exists — and unlike a verdict
    # it binds NO reviewed tip, only a reason. Terminal: a cancelled or
    # verdict'd obligation ignores every later event.
    if event == "cancel" and state["status"] == "open" and strict:
        reason, err = _clean(row.get("reason"), "cancel reason",
                             _CANCEL_REASON_CAP)
        if err or not reason:
            return state
        out = dict(state)
        out.update(status="cancelled", cancel_reason=reason, seq=expected)
        return out
    if event == "cancel" and state["status"] == "held" and strict:
        reason, err = _clean(row.get("reason"), "cancel reason",
                             _CANCEL_REASON_CAP)
        if err or not reason:
            return state
        out = dict(state)
        out.update(status="cancelled", cancel_reason=reason, seq=expected)
        return out
    # SUPERSEDED: an ANNOTATION, never a terminal. `--supersedes` used to mint
    # the successor and leave the parent looking actionable, so enumeration
    # surfaces kept offering finished work (441c4491).
    #
    # WHY THIS IS NOT A CANCEL, measured the expensive way: I built it as one
    # and 18 tests across test_dispatch_chain / test_lr_close / test_web_lr went
    # red, every one correctly. A BUILD parent is SUPPOSED to stay OPEN until
    # its successor LANDS and then close through `landed`/`discharged` WITH
    # PROOF — `discharging_row` walks the chain and returned (None, None) once
    # the parent was cancelled. Closing at MINT time destroys the very door that
    # closes it properly. The brief said "closes/ANNOTATES" and the ladders
    # settle which: annotate.
    #
    # So this sets ONE field and touches neither status nor seq-terminality: the
    # row keeps every door it had, and the enumeration surfaces read the field.
    if event == "superseded" and strict:
        succ = str(row.get("successor") or "").strip()
        if not _ID.fullmatch(succ) or succ == state.get("id"):
            return state
        out = dict(state)
        out.update(superseded_by=succ, seq=expected)
        return out
    if event == "custody" and strict:
        # THE DELIVERY LEG MOVES; AUTHORSHIP DOES NOT. `sender` is a fact about
        # who wrote the row and is never rewritten — rewriting it would destroy
        # the provenance the ledger exists to keep. `custodian` names who must
        # CHASE it now, which is the obligation a dead or renamed seat actually
        # strands. A closed row has no delivery leg left to owe, so it refuses.
        who = str(row.get("custodian") or "").strip()
        if not who or not _TOKEN.fullmatch(who):
            return state
        if not _valid_ts(row.get("ts")):
            return state
        # THE REDUCER MUST REFUSE WHAT THE WRITER REFUSES. The
        # writer requires a bounded, non-empty reason — a delivery leg that
        # changed hands with no stated cause is unauditable — and the reducer
        # did not check it at all. That asymmetry means a hand-appended or
        # replayed event carrying no reason, or one past the cap, APPLIES on
        # read even though `mark_custody` would never have written it: the
        # ledger's own projection becomes reachable by a shape its door
        # rejects. `_clean` is the writer's own validator, called here rather
        # than re-derived, so the two cannot drift.
        reason, reason_err = _clean(row.get("reason"), "custody reason", 256)
        if reason_err or not reason:
            return state
        if state["status"] in CLOSED_STATES:
            return state
        out = dict(state)
        out.update(custodian=who, custody_ts=row.get("ts"), seq=expected)
        return out
    if event == ADVISORY_READ_EVENT and state["status"] == "open" and strict:
        # A MODEL RUN'S READ, ADVISORY: it rides the row and moves nothing —
        # status, tip and every obligation stay exactly as they were (see
        # REVIEWER_FIELDS). The reducer refuses what the writer refuses.
        record, err = _advisory_record(row, state)
        if err:
            return state
        out = dict(state)
        out.update(advisory_reads=tuple(state.get("advisory_reads") or ())
                   + (record,), seq=expected)
        return out
    if event == FINDINGS_NOTE_EVENT and state["status"] in FINDINGS_NOTE_STATES \
            and strict:
        # THE LOCAL FINDINGS PASS'S NOTE (task/2960): it rides the row as
        # `findings_notes` and moves nothing else. Status, tip, delivery,
        # verdicts and advisory reads stay exactly as they were, and no
        # reader of a verdict, a read or a family looks at this key. The
        # reducer refuses what the writer refuses (`_findings_record`).
        record, err = _findings_record(row, state)
        if err:
            return state
        out = dict(state)
        out.update(findings_notes=tuple(state.get("findings_notes") or ())
                   + (record,), seq=expected)
        return out
    if event == "hold" and state["status"] == "open" and strict:
        reason, err = _clean(row.get("reason"), "hold reason", 256)
        if err or not reason:
            return state
        if not _valid_ts(row.get("ts")):
            return state
        out = dict(state)
        # owner_gated is a STRUCTURED claim, never a reading of the prose. The
        # surfaces that separate "the fleet owes this" from "the OWNER owes
        # this" read this boolean; a scanner over hold_reason would answer on
        # wording, and the wording is whatever the holder happened to type.
        # Absent or non-true reads False, so every historical hold — and every
        # hold whose dependency is a build box, a credential, a vendor — stays
        # an ordinary hold owed by the fleet.
        out.update(status="held", hold_reason=reason, hold_ts=row["ts"],
                   owner_gated=row.get("owner_gated") is True,
                   seq=expected)
        # SOURCE-CLEAN IS THE SAME KIND OF CLAIM AS owner_gated AND FOR THE
        # SAME REASON, one axis over: a hold names WHO OWES THE NEXT MOVE, and
        # there are three answers, not two. An ordinary hold is owed by the
        # fleet, an owner-gated one by the owner, and a SOURCE-CLEAN one by the
        # INTEGRATOR — the delta reads clean and the row waits on the one
        # whole-suite gate on the rebased tree, which is what an approve
        # binds. The verdict door sends a clean read here in prose, and prose
        # is unreadable by the surface that has to find these rows.
        #
        # IT CARRIES THE TIP RATHER THAN A BOOLEAN because the claim is about a
        # specific source. A cure round moves the tip past the one the row was
        # dispatched at, so "clean" with no tip would name no tree.
        #
        # ABSENT STAYS ABSENT: a hold with no such claim gets no key, so every
        # historical hold and every ordinary machine stall reads exactly as it
        # did. An older reader that does not know the key sees an ordinary
        # hold, which is the safe direction — it under-claims, never over.
        # A HOLD OWES ONE HOLDER, AND THE REPLAY ENFORCES THAT TOO. The door
        # refuses the two claims together, but a hand-written, forged or
        # legacy event carrying both would otherwise fold into a row that is
        # BOTH — listed as the integrator's and excluded from the stall count
        # as the owner's, so two surfaces disagree about who owes it. Replay
        # is not a trust boundary, and that is exactly why it must not install
        # a state the door would have refused.
        clean = "" if out["owner_gated"] else _clean_tip_of(row)
        # SET OR CLEAR, NEVER INHERIT: `out` began as a copy of the prior
        # state, so a claim from an earlier hold — or one an unsolicited
        # founding row carried — would otherwise survive a hold that declared
        # nothing.
        if clean:
            out["source_clean_tip"] = clean
        else:
            out.pop("source_clean_tip", None)
        return out
    if event == "release" and state["status"] == "held" and strict:
        out = dict(state)
        out.update(status="open", release_reason=row.get("reason"),
                   release_ts=row.get("ts"), seq=expected)
        for key in ("owner_gated", "hold_reason", "hold_ts",
                    "source_clean_tip"):
            out.pop(key, None)
        return out
    # RETIP: an explicit re-point of an OPEN row's tip with an audit trail —
    # the mirror of rebind for the case where the BASE moved rather than the
    # reviewer (measured 2026-08-02: six hand-composed cancel-and-resends in
    # one day, once per land that moved trunk under an already-dispatched
    # lane). Strict v3 only, OPEN only: a verdict BINDS the tip it was written
    # against, so a retip event appended after a verdict is inert — the status
    # gate here IS the verdict gate, and it is stated so a later reader does
    # not relax it as tidiness. NEVER a history rewrite: the seq-0 event keeps
    # the old tip forever, and the projection carries every hop in `retips`
    # (old tip, old ref, when, why, identity) so "what was the reviewer
    # originally pointed at" stays answerable from the row itself. The event
    # must NAME the tip it moves (`old_tip` == the projected tip): strict seq
    # already orders events, but binding the hop to its predecessor makes a
    # spliced or replayed-out-of-context retip inert rather than silently
    # applied. Chain identity (id / chain_root / supersedes) is deliberately
    # untouched: a retip is the SAME obligation at a new base, not a successor
    # — minting a child row here is exactly what the supersedes chain law
    # reserves for NEW rounds of work.
    #
    # REPLAY ENFORCES EVERY LAW THE WRITER DOES, or the writer's refusal is
    # theater (a FIX on this verb's first cut, both replay P1s). The SUCCESSOR
    # FRONTIER: the writer refuses to retip a row whose OPEN successor already
    # carries the obligation, so a hand-appended event that moves such a
    # parent must be equally inert — replay reads the frontier off the same
    # one coherent projection the fold is building (`current`), through the
    # frontier's ONE owner (`_successor_frontier`), and every UNREADABLE
    # shape refuses: no projection at all AND a not-closed row whose
    # supersedes replays CHAIN_UNKNOWN — a frontier the check could not read
    # never reads as clear (P1, r2: per-caller `== id` equality let
    # UNKNOWN fall through as "no open successor" here and at the writer
    # alike). The IDENTITY STAMP: `identity` is a strict enum, never free
    # text and never omitted — an event that cannot say whether the work
    # identity was verified has not earned application — and it is copied
    # INTO the hop, because an attribution that lives only in the writer's
    # return value is transient and every fresh snapshot erases it. THE
    # LEDGER IS THE ONLY WITNESS THE FOLD HAS (r3, superseding
    # the round-2 `proof` stamp): that stamp was an unkeyed content hash of
    # attacker-supplied fields, and an informed forger recomputes an unkeyed
    # recipe over their own forged event — theater, so it is DELETED from
    # the acceptance path (pinned by test: nothing here reads `proof`, in
    # either direction). What the fold CAN witness is its own derived state:
    # the event must name the row's derived current tip as `old_tip`, and a
    # row whose tip cannot be derived at all (a legacy needs-redispatch
    # shape) anchors NOTHING — before this guard an absent old_tip
    # string-matched an absent tip ("" == "") and a hand-appended hop moved
    # a tip-less row. The identity stamp is the writer's recorded TESTIMONY,
    # never re-proven at fold: work identity lives in git, the fold has no
    # git, and a forger with append access sits outside every
    # ledger-resident scheme — they could as easily rewrite seq-0.
    if event == "retip" and state["status"] == "open" and strict:
        tip = str(row.get("tip") or "").lower()
        reason, err = _clean(row.get("reason"), "retip reason", 256)
        if not _TIP.fullmatch(tip) or err or not reason:
            return state
        if not _valid_ts(row.get("ts")):
            return state
        derived = str(state.get("tip") or "")
        if not _TIP.fullmatch(derived):
            return state
        if str(row.get("old_tip") or "").lower() != derived or tip == derived:
            return state
        identity = row.get("identity")
        if identity not in ("verified", "unverified"):
            return state
        if current is None:
            return state
        successors, unknown = _successor_frontier(current, state.get("id"))
        if successors or unknown:
            return state
        ref, ref_err = _clean(row.get("ref"), "ref", 256)
        out = dict(state)
        hops = list(state.get("retips") or ())
        # An event written before this field, or by an older writer, replays
        # False — never UNKNOWN and never True. The flag is the writer's
        # testimony that the base was replaced; its ABSENCE is only the
        # absence of that testimony, and the honest reading of that is "no
        # replacement was recorded", which is what a fast-forward also says.
        hops.append({"old_tip": state.get("tip"), "old_ref": state.get("ref"),
                     "tip": tip, "ts": row["ts"], "reason": reason,
                     "identity": identity,
                     "base_replaced": row.get("base_replaced") is True})
        # THE BINDING MOVES WITH THE ROW, at replay as at the writer. A row
        # whose base stayed at the ORIGINAL dispatch would prove a second
        # retip's direction against a base two hops stale.
        out.update(_moving_binding(row))
        out.update(tip=tip, ref=ref if ref and not ref_err else tip,
                   retips=hops, seq=expected)
        # DELIVERY STALES ON A TIP MOVE. A row reaching this arm carries a
        # `delivery=observed` earned by a message about the OLD tip, and
        # leaving it observed tells every delivery surface the recipient has
        # been told — when what they were told is now a different sha. The
        # recorded confirmation outlives the fact it confirms, and the row
        # stops re-surfacing at exactly the moment it has something new to
        # say. needs-confirmation puts it back in front of the recipient.
        #
        # `delivery_ref` is KEPT deliberately: it is the audit trail of what
        # WAS confirmed, and the next `delivered` event overwrites it. Only
        # the CLAIM is retracted, never the history of having made it.
        if state.get("delivery") == "observed":
            out["delivery"] = "needs-confirmation"
        return out
    # Historical snapshot verdicts/retarget-derived verdicts (compat only).
    if compat and row.get("status") == "verdict" and row.get("verdict_ref"):
        reviewed = str(row.get("reviewed_tip") or state.get("tip") or "").lower()
        if not state.get("tip") or reviewed == state.get("tip"):
            out = dict(state)
            out.update(status="verdict", reviewed_tip=reviewed or None,
                       verdict_ref=row.get("verdict_ref"), migration=None,
                       seq=_int_seq(row.get("seq"), state.get("seq", 0)))
            return out
    if compat and row.get("delivery_ref") and state["status"] == "open":
        out = dict(state)
        out.update(delivery="observed", delivery_ref=row.get("delivery_ref"),
                   seq=_int_seq(row.get("seq"), state.get("seq", 0)))
        return out
    return state


def verdict_anchor(event):
    """The CONTENT IDENTITY of one accepted verdict EVENT. -> hex | None.

    A HIGH finding, and the FOURTH time this boundary's identity has been wrong:
    a wall clock, a version int, an append position, and then the dispatch
    work-item id. Every one of them was a PROXY that something else can come to
    satisfy, and the last was reproduced end to end — `send()` derives its id
    deterministically from sender/repo/operation key, so removing the founder's
    rows and retrying the SAME operation key recreates a row bearing the SAME
    id at a LATER position. The marker followed it there and every unstamped
    approval it passed over flipped from unknown back to none.

    A work-item id says WHICH LOOP. It cannot say WHICH EVENT, and the epoch is
    an event. So the anchor is a hash of the whole accepted verdict event as it
    sits on the ledger — every field, canonically ordered, nothing selected by
    hand. Selecting a field set is the same proxy mistake one level smaller: the
    one field left out is the one a replacement is free to reuse. Hashing the
    whole event also means a field added in a later `v` participates for free.

    Stable across COMPACTION (which removes whole rows and never rewrites the
    survivors) and across re-serialization (sort_keys + fixed separators), which
    is exactly the pair of properties the frozen founder needs.

    DELIBERATELY NOT GENERALIZED into a shared content-anchor primitive: the
    retip verb borrowed this recipe for a replay-side `proof` stamp, and codex
    (round 3) demolished it — an unkeyed hash of attacker-supplied fields is
    recomputable by exactly the forger it would need to stop. It survives HERE
    because the epoch marker's adversary is accidental id-reuse, never a
    forger; do not lend it to a boundary that needs proof again."""
    if not isinstance(event, dict):
        return None
    try:
        raw = json.dumps(event, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.blake2b(("gate-epoch-anchor-v1\0" + raw).encode("utf-8"),
                           digest_size=16).hexdigest()


def _snapshot(track_verdicts=False):
    projscope.spend_or_raise("dispatch ledger read")
    _events, acc, unavailable = _ledger_fold()
    projscope.spend_or_raise("dispatch ledger fold")
    if unavailable:
        return {}, {}, unavailable
    projscope.spend_or_raise("dispatch ledger result")
    return acc.out, acc.verdicts, None


class _Acc(object):
    """EVERYTHING ONE FOLD ACCUMULATES, in one place, because a checkpoint of
    the fold is only a checkpoint if it carries all of it (task/2770).

    `out`, `verdicts` and `taken` are `_fold`'s three returns. `taken_at` is
    `taken` spelled as ledger POSITIONS: the rows themselves are already on the
    ledger, so a checkpoint stores where they are and a reader holding the
    events rebuilds the identical lists. `actors` is `_credit_act`'s index,
    None when nobody asked for it. `count` is how many events have been folded,
    which is also the position the next event takes."""

    __slots__ = ("out", "verdicts", "taken", "taken_at", "actors", "count")

    def __init__(self, rows=False, actors=None):
        self.out, self.verdicts, self.taken_at = {}, {}, {}
        self.taken = {} if rows else None
        self.actors = actors
        self.count = 0

    @classmethod
    def restored(cls, state, count):
        acc = cls()
        acc.out, acc.verdicts = state["out"], state["verdicts"]
        acc.taken_at, acc.actors = state["taken"], state["actors"]
        acc.count = count
        return acc

    def state(self):
        return {"out": self.out, "verdicts": self.verdicts,
                "taken": self.taken_at, "actors": self.actors}


def _fold(events, track_verdicts=False, actors=None):
    """(state by id, {id: (append index, content anchor)} for every ACCEPTED
    verdict EVENT, ACCEPTED events by id) from ONE already-read event list.

    Split out from the read so a caller that needs BOTH the folded state and
    the raw grouped events can derive them from a single read — see
    `snapshot_and_events`, and the straddle it exists to make impossible.

    THE THIRD RETURN IS WHICH EVENTS THE FOLD ACTUALLY TOOK, and it exists
    because the STATE cannot answer that question. Deferring to the state
    ("date the verdict only if the row IS closed") reconciles WHETHER a
    transition happened and is blind to WHICH event caused it: with two verdict
    events for one row — one refused as out-of-sequence, one accepted — the row
    is legitimately closed either way, and a reader picking the first verdict it
    saw took the REFUSED one's timestamp. A reproduction put a 2001 stamp on
    the refused row and the card printed a verdict from 2001 with a 25-year
    dwell, `ledger_refused: []`, `unavailable: null`.

    So the fold reports its own decisions rather than letting a second pass
    infer them by kind and order. `_apply` already says which event it took, in
    the only way it can be trusted to: EVERY refusal path returns the state
    OBJECT IT WAS GIVEN and every acceptance returns a NEW dict (pinned by
    tests.test_dispatches.ApplySignalsWhatItTook, so a future branch that
    mutates in place is a red test rather than a silent mis-attribution).

    `actors` IS THE FOURTH PRODUCT AND IT IS AN ACCUMULATOR, not a return,
    because only a caller who wants it should pay for it and nobody else's
    signature should move to add it. `_credit_act` fills it and says why the
    question can only be answered here.

    THE BODY IS `_fold_into`, which a checkpointed read also calls to fold only
    the events past a restored position; this door always folds from zero.
    """
    acc = _Acc(rows=True, actors=actors)
    _fold_into(acc, events, 0)
    return acc.out, acc.verdicts, acc.taken


def _fold_into(acc, events, base, bank=None):
    """Fold `events` into `acc`, the first of them at ledger position `base`.
    `bank(acc, index)`, when given, runs before each event, at a row boundary
    (`foldckpt.progress_saver`).

    POSITIONS ARE ABSOLUTE. `position=` is how `_apply` stamps `close_seq`,
    `verdicts` holds each accepted verdict's append index, and the gate-epoch
    comparison and the chain-proof census cutoff read those indices — so a
    fold that resumes from a checkpoint at K must hand its first event K, never
    0, or every close in the tail would claim an order the record never had."""
    out, verdicts, taken = acc.out, acc.verdicts, acc.taken
    taken_at, actors = acc.taken_at, acc.actors
    for offset, row in enumerate(events):
        index = base + offset
        if bank is not None:
            bank(acc, index)
        # Budget expiry is not a malformed event. Check outside the row guard so
        # it cannot be downgraded to "skip this row" and continue folding later
        # obligations after the stop-wide answer became UNKNOWN.
        projscope.spend_or_raise("dispatch ledger row %d" % index)
        # One malformed row must never blind the whole ledger: a crash here
        # would turn every obligation into "no usable obligations" — worse
        # than skipping the bad row and keeping every good state intact.
        try:
            rid = str(row.get("id") or "")
            if rid not in out:
                state = None
                try:
                    state = _new_state(row)
                    if state:
                        out[rid] = state
                        # The event that OPENS the row is an accepted transition
                        # — it is where the opening stamp comes from.
                        if taken is not None:
                            taken[rid] = [row]
                        taken_at[rid] = [index]
                finally:
                    _credit_act(actors, row, None, state)
                continue
            before = out[rid]
            after = before
            try:
                after = _apply(before, row, current=out, verdicts=verdicts,
                               position=index)
                out[rid] = after
            finally:
                # A ROW THAT CRASHED THE REDUCER IS A ROW IT DID NOT TAKE, and
                # the activity projection must hear about it as UNRESOLVED
                # rather than not at all. `finally` is what makes that true on
                # the path the outer handler swallows: an act nobody could
                # attribute is the one shape that must never read as silence.
                _credit_act(actors, row, before, after)
            if after is not before:
                if taken is not None:
                    taken.setdefault(rid, []).append(row)
                taken_at.setdefault(rid, []).append(index)
            if row.get("event") == "verdict" \
                    and before.get("status") != "verdict" \
                    and after.get("status") == "verdict":
                # POSITION AND IDENTITY IN ONE VALUE. Two parallel maps would
                # let gate_epoch answer half of its single question — "is the
                # frozen founding EVENT still on this ledger?" — and the half
                # it could still answer is the half that fails open. Keep this
                # index even when the caller does not request it: close-event
                # replay needs the same already-read verdict order to validate
                # a subsumption confirmation without opening a second snapshot.
                verdicts[rid] = (index, verdict_anchor(row))
            # A KIND THIS FOLD HAS NO ARM FOR IS RECORDED ON ITS ROW, AFTER
            # the take above, so the event is still NOT taken: the fold stays
            # tolerant and the row keeps its state, but a writer can now see
            # that its `seq` stopped at an event it could not read, and refuses
            # (`unknown_kinds_refusal`). See `KNOWN_EVENT_KINDS`.
            if not _is_known_kind(row.get("event")):
                out[rid] = _note_unknown_kind(out[rid], row.get("event"))
        except projscope.Expired:
            raise
        except Exception:
            continue
    acc.count = base + len(events)
    projscope.spend_or_raise("dispatch ledger fold completion")


def _ledger_fold(strict=False, want_events=False, want_actors=False):
    """ONE SCOPE PER FOLD. `projscope.memo` is "outside a scope, always
    compute", and nothing opened one here, so every memo the fold reached
    recomputed -- 46.9% of its git spawns were byte-identical repeats,
    including one trunk ref resolved once per carried row.

    NOTHING IS MEMOISED ACROSS READS, which is this module's own law: the
    cache lives inside one fold and dies with it. Pinning trunk for the whole
    fold is also the cure `_carriage_trunk_sha` already prescribes per row --
    one snapshot is one measurement, and the writer re-derives under its own
    lock, so staleness never binds a write. A nested scope shares its
    caller's cache by design; `tests/test_foldscope.py` holds the evidence
    and the arms."""
    with projscope.scope():
        return _ledger_fold_scoped(strict, want_events, want_actors)


def _ledger_fold_scoped(strict=False, want_events=False, want_actors=False):
    """(events or None, accumulator, unavailable) — THE ONE WHOLE-LEDGER FOLD,
    served from the maintained checkpoint whenever every key of it still holds
    (task/2770; the key and why each part is in it: `helm/foldckpt.py`).

    `strict` is the reader's strictness, exactly as `checked_events` takes it.
    `want_events` returns the whole parsed event list beside the fold, for the
    one caller that groups raw events; every other caller parses only the TAIL
    past the checkpoint. The answer is the full replay's answer BYTE FOR BYTE
    in every case — that equivalence is the whole acceptance bar, pinned by
    tests.test_foldckpt.

    ONE READ, ONE BOUNDARY. The ledger is read ONCE, as bytes, through
    `eventledger.read_bytes` — the same open discipline as `checked_events`,
    and the same words for the same failure — and every road below parses
    those bytes with `checked_rows`, the one framing machine. The checkpoint's
    prefix proof and the tail the fold continues with are therefore cut from
    one read, so no append can land between them.

    AN EPOCH LENS IS PART OF THE KEY, NOT A REFUSAL. `_apply` consumes
    `gate_epoch` while it validates a close, and a lens owns that answer for
    the whole fold — so the lensed fold and the plain fold of one ledger are
    two different folds and keep two different checkpoints, named by the term
    the lens serves (`_lens_epoch_key`, and `helm/foldckpt.py` for the key).
    A lens whose term is not the marker's own names no key, and that fold
    takes the first road below.

    THREE ROADS:
      * NO CHECKPOINT MAY BE TOUCHED (this process cannot name its code, the
        gate-epoch marker cannot be read, or an epoch lens owns `gate_epoch`
        on this thread and serves a term no key can name): a full fold of the
        bytes, and nothing is read from or written to the store.
      * NO CHECKPOINT HOLDS: a full fold of the bytes, recorded, and a
        checkpoint written when the fold is re-verifiable.
      * ONE HOLDS: restore it and fold only the events after it, positions
        continuing, then write the advanced checkpoint.
    The prefix under a checkpoint is CLEAN — every complete line a row — so the
    lenient and the strict reader agree on it, and a tail parse sees exactly
    the rows a whole-file parse would see past it."""
    path = ledger_path()
    data, unavailable = eventledger.read_bytes(path)
    if unavailable:
        return None, None, unavailable
    session = None
    lens = getattr(_EPOCH_LENS, "fn", None)
    if lens is None:
        session = foldckpt.begin(path, epoch_path(), data)
    else:
        key = _lens_epoch_key(lens)
        if key is not None:
            session = foldckpt.begin(path, epoch_path(), data, lens=key)
    restored = session.restore() if session is not None else None
    base_header, count, offset = None, 0, 0
    if restored is not None:
        base_header, state = restored
        count = base_header["ledger"]["events"]
        offset = base_header["ledger"]["offset"]
    events = None
    if want_events or restored is None:
        events, reason = eventledger.checked_rows(data, strict=strict)
        if reason:
            return None, None, reason
        tail = events[count:]
    else:
        tail, reason = eventledger.checked_rows(data[offset:], strict=strict)
        if reason:
            # A corrupt line in the tail poisons a strict read, and the reason
            # must count lines from the top of the FILE, as every other reader
            # of this ledger does — so it is re-derived from the whole bytes.
            _events, reason = eventledger.checked_rows(data, strict=strict)
            return None, None, reason
    if restored is not None:
        acc = _Acc.restored(state, count)
    else:
        acc = _Acc(actors={"validated": {}, "unresolved": {}}
                   if session is not None or want_actors else None)
    if session is None:
        _fold_into(acc, tail, 0)
        return events, acc, None
    # ONLY A CLEAN PREFIX IS A CHECKPOINT: a skipped malformed line would
    # make the lenient and strict readers disagree about what it holds.
    clean = data[offset:session.end].count(b"\n") == len(tail)
    with foldckpt.recording() as rec:
        # A budgeted fold that cannot finish banks its progress (task/2949).
        bank = foldckpt.progress_saver(session, offset, count, base_header,
                                       rec) if clean else None
        _fold_into(acc, tail, count, bank=bank)
    # An EMPTY ledger is not checkpointed — there is nothing to save, and a
    # read of a home with no ledger must not leave a store behind.
    if (restored is None or tail) and acc.count and foldckpt.may_save() \
            and clean:
        session.save(acc.state(), acc.count, session.end, base_header, rec)
    return events, acc, None


def _advance_checkpoint():
    """THE WRITER'S HALF OF WRITE-MAINTAINED: fold what was just appended into
    the checkpoint (task/2770), AFTER the writer has released the ledger lock
    (`_ledger_write`): the fold is a read, and the checkpoint store takes its
    own save lock. A failure here changes nothing about the write that
    already succeeded, so it never raises — not even a spent budget; the next
    reader folds the same tail."""
    try:
        _ledger_fold()
    except Exception as exc:                             # noqa: BLE001
        from . import record
        record.swallow("dispatches._advance_checkpoint", exc)


#: How many tries a dispatch-ledger writer makes (`_ledger_write`). Every try
#: but the last reads without the lock; the last takes the lock for its read
#: as well as its write.
LEDGER_WRITE_TRIES = 4


class _LedgerMoved(BaseException):
    """The ledger changed between one try's read and its write. A
    BaseException, so that no `except Exception` inside a writer's body can
    swallow the retry and append on a read that is no longer current."""


_THEN = object()
_ABSENT = ("absent",)


def _ledger_identity(path):
    """`eventledger.ledger_identity`, with an ABSENT ledger named as such.

    The probe answers None both for a ledger that does not exist and for one
    it cannot read. The first is a known state, a fresh home's first write,
    and a writer that read it and still finds it absent at the write has
    seen nothing change; the second proves nothing, so it stays None and
    never matches."""
    identity = eventledger.ledger_identity(path)
    if identity is None and not os.path.lexists(path):
        return _ABSENT
    return identity


class _LedgerTxn(object):
    """ONE TRY OF ONE DISPATCH-LEDGER WRITE. `_ledger_write` explains it.

    `held` is what a writer's `if not held:` reads: True on an optimistic
    try, which has not asked for the lock yet, and the lock's own answer on
    the last try, which asked before the read."""

    def __init__(self, path, stack, last, redo=False):
        self.path, self.last, self.appended = path, last, 0
        # AN EARLIER TRY OF THIS CALL DECIDED TO WRITE AND FOUND THE LEDGER
        # MOVED. A body that now finds its own intended effect already on the
        # ledger lost a race to another writer: its answer is "already done",
        # never "I did it" (`_append_dispatch`, `_WRITTEN_ELSEWHERE`).
        self.redo = redo
        self._stack, self._then, self.seen = stack, None, None
        self._locked = False
        if last:
            self._locked = bool(stack.enter_context(eventledger.locked(path)))
            self.held = self._locked
        else:
            self.held = True
            # TAKEN BEFORE THE WRITER READS, so an append that lands between
            # this line and the read also sends the try round again.
            self.seen = _ledger_identity(path)

    def lock(self):
        """Take the lock now: True, or False when the ledger is unwritable.

        On an optimistic try this proves the ledger is still the file the try
        read (`eventledger.ledger_identity`: device, inode, size, mtime and
        ctime). If it is not, the try is over and `_ledger_write` runs the
        writer again from a fresh read. A writer calls this itself before a
        LIVE probe whose answer it records as measured at the write (a git
        object, a lane's state, a mint's freshness): from here to the append
        is one critical section, while the fold before it runs without the
        lock."""
        if self._locked:
            return True
        if self.last or not self._stack.enter_context(
                eventledger.locked(self.path)):
            return False
        self._locked = True
        # A ledger that cannot be identified proves nothing unchanged.
        if self.seen is None or _ledger_identity(self.path) != self.seen:
            raise _LedgerMoved()
        return True

    def append(self, event):
        """Append one event: True, or False when the ledger is unwritable.
        The first append of an optimistic try takes the lock (`lock`)."""
        if not self.lock():
            return False
        if not eventledger.append_unlocked(self.path, event):
            return False
        self.appended += 1
        return True

    def then(self, finish):
        """Return this from the writer's body to have `finish()` give the
        writer's answer AFTER the lock is released and the checkpoint has
        advanced: the projection a writer returns can itself re-validate
        with git, which is never work for the lock."""
        self._then = finish
        return _THEN


def _ledger_write(body, path=None, tries=None):
    """Run ONE dispatch-ledger write: `body(txn)` reads, decides and appends
    through `txn.append`, and whatever it returns is the writer's answer.

    THE LOCK COVERS THE WRITE, NEVER THE FOLD. The fold a writer reads is a
    whole-ledger replay whenever its checkpoint is cold, and a land changes
    the code, which makes every checkpoint cold: measured one minute after a
    land, the first `snapshot()` took 102.2 s and the next 0.23 s. A writer
    that folded under the lock made every `dispatch send` and `dispatch
    verdict` in the fleet wait that long. So every try but the last reads
    and validates with no lock held, and under the lock proves only that the
    ledger is the file it read before it appends (`_LedgerTxn.append`). A
    try whose ledger moved is run again from a fresh read, and the redo is
    cheap: the read before it left the fold checkpoint warm.

    THE LAST TRY READS UNDER THE LOCK. A writer that re-takes the lock faster
    than this one can read again would win every optimistic race, so after
    `tries - 1` misses the read, the checks and the write are one critical
    section, as every writer was before this. `tries=1` makes a door locked
    from its first read, for a writer whose checks the ledger's identity
    cannot cover.

    A body is re-run from its start on a miss, so it must rebuild what it
    decides from its own inputs: nothing it computes may leak from one try
    to the next. The checkpoint advance runs after the lock is released."""
    path = path or ledger_path()
    total = LEDGER_WRITE_TRIES if tries is None else max(1, int(tries))
    moved = 0
    for number in range(total):
        try:
            with contextlib.ExitStack() as stack:
                txn = _LedgerTxn(path, stack, number == total - 1,
                                 redo=moved > 0)
                answer = body(txn)
        except _LedgerMoved:
            moved += 1
            continue
        if txn.appended and path == ledger_path():
            _advance_checkpoint()
        return txn._then() if answer is _THEN else answer


def snapshot():
    out, _verdicts, unavailable = _snapshot()
    return out, unavailable


def retire_read():
    """(state by id, unavailable) — THE ONE READ THE RETIREMENT PATH MAKES.

    IT IS THE SWEEP'S DOOR, AND THERE IS NO SECOND ONE. `retire_sweep` reads
    through `landreq.project_raw`, which is STRICT and refuses a ledger it
    cannot read in full before it classifies anything. The single-ID door and
    the locked writer read LENIENTLY instead, so a complete corrupt JSONL line
    was skipped three times over — at the context, at the activity index, and at
    the measurement the lock authorizes the append on — and this lane's
    unrostered eligibility turns that hole into PERMISSION: a current act on a
    target party's obligation, sitting on the skipped line, reads as the silence
    that spends the row.

    THE LENIENT TIER IS RIGHT WHERE IT IS AND WRONG HERE. Refusing to cancel a
    dispatch because some unrelated row is malformed helps nobody. Retirement
    makes the opposite claim — that NOTHING in the record contradicts an absence
    — so a read that dropped part of the record has proved nothing. Named once
    because two spellings of one strictness decision drift apart on the round
    nobody is looking."""
    current, _by_id, _taken, _verdicts, unavailable = snapshot_and_events()
    return current, unavailable


def snapshot_with_verdicts():
    """Canonical state plus {id: (append index, content anchor)} for every
    ACCEPTED verdict EVENT — position and identity, always together."""
    return _snapshot(track_verdicts=True)


def verdict_index(verdicts, rid):
    """The append index of one accepted verdict, or None."""
    pair = (verdicts or {}).get(rid)
    return pair[0] if pair else None


def open_recipients():
    """(recipient -> open-row count, None) — or (None, note) when the ledger
    cannot be read. None is 'helm could not read the obligations', never an
    empty board: proxywatch derives IDLE from a MEASURED zero, and a blind
    read collapsing to {} would call every unreadable-ledger seat out of
    work."""
    current, unavailable = snapshot()
    if unavailable:
        return None, unavailable
    counts = {}
    # OWED, not merely open: a seat whose row a sibling already carried is
    # not holding that obligation, and proxywatch derives IDLE from this count.
    for row in owed(current):
        if row.get("recipient"):
            counts[row["recipient"]] = counts.get(row["recipient"], 0) + 1
    return counts, None


GATE_EPOCH = "gate-epoch.json"
EPOCH_LOST = "EPOCH-LOST"
EPOCH_V = 2                 # v1 = founder id only, unprovable (see _upgrade_epoch)
LEGACY_EPOCH_V = 1
_ANCHOR = re.compile(r"[0-9a-f]{32}\Z")
_EPOCH_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def epoch_path():
    return os.path.join(home.global_dir(), GATE_EPOCH)


def _marker_founder(row, version):
    """The frozen founder id of a marker at `version`, or None if ANY
    authoritative field is malformed.

    A MED finding. When only `index` was validated, AND NOTHING ELSE,
    `{"index": 2, "founder": []}` was accepted as a marker — and then
    `verdict_order.get([])` raised TypeError out of a read path that
    landreq.project() called OUTSIDE its per-row try, so the entire projection
    died where one refused row was the worst case it was designed for.

    Every field is authoritative or it is not on the marker. A validator that
    checks one field of five is not a validator, it is a coincidence."""
    if not isinstance(row, dict):
        return None
    ver = row.get("v")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver != version:
        return None
    index = row.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    ts = row.get("ts")
    if not isinstance(ts, str) or not _EPOCH_TS.fullmatch(ts):
        return None
    founder = row.get("founder")
    if not isinstance(founder, str) or not _ID.fullmatch(founder):
        return None
    return founder


def _valid_marker(row):
    """A CURRENT marker: every field of the frozen founder PLUS the anchor that
    proves which event it names."""
    if _marker_founder(row, EPOCH_V) is None:
        return False
    anchor = row.get("anchor")
    return isinstance(anchor, str) and bool(_ANCHOR.fullmatch(anchor))


def _marker_file():
    """(row, present) — the raw marker object and whether a marker file EXISTS.

    ABSENT AND UNREADABLE ARE NOT THE SAME STATE, one layer up from the very
    distinction `gate_caps` exists to make. `pk.read_json` collapses them into
    its default, and a marker that exists but cannot be parsed would then read
    as "nothing was ever frozen" — so the fresh derivation would run and
    re-authorize exactly what the unreadable marker was refusing."""
    try:
        with pk.open_regular(epoch_path(), encoding="utf-8") as fh:
            return json.load(fh), True
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True


def _read_epoch():
    """The FROZEN founder (a validated marker), None when NOTHING was ever
    frozen, or EPOCH_LOST when a marker is present and cannot be trusted."""
    row, present = _marker_file()
    if not present:
        return None
    return row if _valid_marker(row) else EPOCH_LOST


def _write_epoch(index, founder, anchor):
    row = {"v": EPOCH_V, "index": index, "founder": founder,
           "anchor": anchor, "ts": pk.now_ts()}
    try:
        pk.write_json(epoch_path(), row)
    except OSError:
        return None                     # unwritable: stay UNKNOWN, never guess
    return row


def _upgrade_epoch(row, current=None, verdicts=None):
    """Bind the anchor onto a v1 marker, KEEPING its frozen founder and index.

    A v1 marker named the founder but could not prove which EVENT that name
    refers to — a HIGH finding, on disk. It is not re-derived: re-scanning the
    ledger for today's earliest stamped verdict is the retroactive
    authorization this whole file exists to prevent, and if a compaction has
    already happened the re-derived boundary lands LATER than the frozen one.
    So the founder is carried over verbatim and only the proof is added.

    Anything else unreadable is repaired by a human. Overwriting a marker we
    cannot read is indistinguishable from overwriting one we can."""
    founder = _marker_founder(row, LEGACY_EPOCH_V)
    if founder is None:
        return None
    if current is None or verdicts is None:
        current, verdicts, unavailable = _snapshot(track_verdicts=True)
        if unavailable:
            return None
    found = (verdicts or {}).get(founder)
    if found is None or not found[1]:
        return None                     # founder not on the ledger: unprovable
    return _write_epoch(row["index"], founder, found[1])


def record_gate_epoch(current=None, verdicts=None):
    """Freeze the cutover the FIRST time a gate-capable writer records one.

    WHY THIS IS A FILE AND NOT A RECOMPUTE. Recomputing the epoch from
    the current ledger on every read is RETROACTIVE POLICY, and it has a
    concrete vector, not only an argument — dispatch replay SILENTLY SKIPS
    complete malformed rows, so if the founding verdict ever becomes
    unparseable the founder vanishes, the epoch recomputes to a LATER position,
    and every unstamped verdict in between flips from UNKNOWN back to none.
    Authorized retroactively, by one corrupted row, with nothing announcing it.

    So the boundary is computed ONCE from the ledger as it stands at that
    moment, written, and never derived again. Later corruption cannot move a
    number that is no longer being calculated.

    Idempotent and never overwrites: a marker that already exists IS the
    answer, even if today's ledger would compute a different one. The ONE
    exception is the v1 upgrade, which keeps that answer and only adds the
    proof of it."""
    stored, present = _marker_file()
    if present:
        if _valid_marker(stored):
            return None                 # frozen AND provable: never overwrite
        return _upgrade_epoch(stored, current, verdicts)
    if current is None or verdicts is None:
        current, verdicts, unavailable = _snapshot(track_verdicts=True)
        if unavailable:
            return None
    index, founder = _scan_epoch(current, verdicts)
    if index is None:
        return None                     # nothing to freeze yet
    anchor = (verdicts or {}).get(founder, (None, None))[1]
    if not anchor:
        # A boundary we could not prove later is not one to freeze now: the
        # marker would name a founder and be unable to say which event it is,
        # which is precisely the state this version exists to end.
        return None
    return _write_epoch(index, founder, anchor)


def _scan_epoch(current, verdicts):
    """(index, founder_id) by APPEND ORDER — the one-time derivation.

    Ascending, and it returns at the FIRST position that settles the question.
    A malformed-PRESENT stamp founds at its OWN position and fails closed from
    there: skipping it would move the boundary later and launder every
    unstamped approval in between. An honest set WITHOUT receipt-v1 — [] or
    ["other-cap"] — is a writer that could not mint one, so it does not found."""
    for rid, (index, _anchor) in sorted((verdicts or {}).items(),
                                        key=lambda kv: kv[1][0]):
        row = current.get(rid) or {}
        if "gate_caps" not in row:
            continue                    # absent — says nothing either way
        caps = clean_gate_caps(row["gate_caps"])
        if caps == GATE_CAPS_UNKNOWN:
            return index, rid           # unreadable: the boundary starts HERE
        if GATE_CAP_RECEIPT in caps:
            return index, rid           # the real cutover
    return None, None


def gate_epoch(current=None, verdicts=None):
    """The FROZEN cutover, EPOCH_LOST, or None. NEVER RAISES. See the body.

    THIS IS THE DOOR; `_gate_epoch_uncached` IS THE BODY, split for exactly the
    reason `approval_tier` is: the land projection must consume this read
    THROUGH its read-set, and the read-set's reader cannot call a function that
    would route straight back into the read-set. The body is what the reader
    calls; every other caller arrives here.

    A LENS INSTALLED ON THIS THREAD OWNS THE ANSWER. `landreq.project_raw`
    consumes the epoch to DEMOTE approvals that predate the freeze, four frames
    below anything `web_land_model` wrote. The epoch answers from a MARKER FILE
    no ledger stat describes, so without this the witness could not name an
    input that had moved — measured at CL96: a marker-only mutation
    restored a stale READY/REVIEWED body as FRESH.

    OUTSIDE A LENS THIS IS EXACTLY WHAT IT WAS. The land door, the close
    ladders and every CLI reader install none and resolve live, so the only
    behaviour that changes is inside one projection's snapshot."""
    lens = getattr(_EPOCH_LENS, "fn", None)
    if lens is not None:
        served = lens()
        # THE FOLD'S ANSWER DEPENDS ON THIS TERM, so a checkpoint of the fold
        # that consumed it is keyed on it. The recorder carries the term to
        # the save, which refuses any fold the lens answered with a term its
        # key does not name (`helm/foldckpt.py`).
        foldckpt.epoch_served(_epoch_identity(served) or _EPOCH_UNKEYABLE)
        return served
    return _gate_epoch_uncached(current, verdicts)


# An identity no checkpoint key can ever equal: the three answers `gate_epoch`
# is contracted to give all spell themselves, so a lens serving anything else
# records a term that matches no key and the save refuses.
_EPOCH_UNKEYABLE = "unkeyable"


def _epoch_identity(value):
    """One gate-epoch answer as a checkpoint-key identity, or None when the
    value is outside the three the contract admits (an index, the lost
    boundary, or no boundary at all).

    `type(value) is int` rather than isinstance, because a bool is an int and
    an index it is not: spelling True as `index:1` would key two different
    worlds the same."""
    if value is None:
        return "none"
    if value == EPOCH_LOST:
        return "lost"
    if type(value) is int:
        return "index:%d" % value
    return None


def _lens_epoch_key(lens):
    """The checkpoint key for a fold running under an epoch lens, or None when
    this fold must touch no checkpoint at all.

    A LENS IS PART OF THE FOLD POLICY. `_apply` consumes `gate_epoch` while it
    validates a landed or subsumed close, so the fold's answer is a function of
    the term the lens serves as much as of the ledger bytes.

    AND THE TERM MUST BE THE MARKER'S OWN. A lens may serve anything; a
    checkpoint keyed on an answer no file backs would be a fold judged against
    an invented boundary, stored and served to every later reader whose lens
    says the same. So the served term is compared against the marker-derived
    epoch with the lens suspended, and a fold whose lens disagrees is keyed on
    nothing and writes no checkpoint — which is the road every lensed fold
    takes when no key can be named.

    THE COMPARISON IS ONE READING PER SCOPE, keyed on the marker's own bytes:
    inside a projection the read-set's discipline is one reading of a moving
    input, and a marker that moves changes the key so the memo cannot answer
    for it. The lens is suspended around both halves so neither routes back
    into the reader that installed it."""
    from . import record
    try:
        with epoch_lens(None):
            served = lens()
            derived = projscope.memo(
                ("dispatches.marker-epoch",
                 foldckpt.marker_key(epoch_path())),
                _gate_epoch_uncached)
        if served != derived:
            return None
    except projscope.Expired:
        raise
    except Exception as exc:                             # noqa: BLE001
        record.swallow("dispatches._lens_epoch_key", exc)
        return None
    return _epoch_identity(served)


def _gate_epoch_uncached(current=None, verdicts=None):
    """The live resolution behind `gate_epoch`. NEVER RAISES.

    -> an int      the boundary. Before it a missing stamp is legacy; at or
                   after it a missing stamp is UNKNOWN and never READY.
    -> EPOCH_LOST  the boundary cannot be located or cannot be trusted: no
                   marker while stamped verdicts exist, a marker that is
                   present but malformed, or a frozen founding event that is no
                   longer on the ledger. Nothing reaches READY until it is
                   repaired. Loud and total, by design: the alternative
                   is a boundary that silently slides later and retroactively
                   authorizes everything it passed over.
    -> None        no stamped verdict has ever been written. Every row is
                   honestly legacy and there is nothing to freeze.

    THE MARKER IS READ, NEVER RECOMPUTED. Recomputing per projection is
    retroactive policy, and the vector is concrete: replay SILENTLY SKIPS
    complete malformed rows, so one unparseable founder moves the boundary
    later and flips every unstamped verdict in between from UNKNOWN to none.
    A number that is no longer being calculated cannot be moved by corrupting
    its inputs."""
    marker = _read_epoch()
    if marker == EPOCH_LOST:
        return EPOCH_LOST               # present and untrustworthy: refuse
    if marker:
        if current is None or verdicts is None:
            current, verdicts, unavailable = _snapshot(track_verdicts=True)
            if unavailable:
                return EPOCH_LOST
        # ONE QUESTION, ASKED ONCE: is the frozen founding EVENT still here?
        # "Compacted away" and "replaced by a same-id recreation" are the same
        # answer — we cannot locate the boundary — and asking them as two
        # checks would leave NEITHER measurable, because reverting either one
        # leaves the other refusing the identical input.
        #
        # The HIGH finding lives on the second line. The founder's dispatch id
        # survives compaction, which is why it was chosen, but `send()` derives
        # that id deterministically, so retrying the same operation key
        # RECREATES it — at a later position, with the marker following it
        # there. The anchor is over the accepted verdict EVENT, so a recreated
        # row bearing the same work-item id does not satisfy it.
        found = (verdicts or {}).get(marker["founder"])
        if found is None or found[1] != marker["anchor"]:
            return EPOCH_LOST
        return found[0]
    if current is None or verdicts is None:
        current, verdicts, unavailable = _snapshot(track_verdicts=True)
        if unavailable:
            return EPOCH_LOST           # cannot look: never authorize
    index, _founder = _scan_epoch(current, verdicts)
    if index is None:
        return None                     # a fleet that never gated — honest
    # Stamped verdicts exist and the marker does not. Either it was never
    # written (the first stamped write records it) or it was LOST. We cannot
    # tell those apart, and one of them is the retroactive-authorization bug,
    # so this is UNKNOWN rather than a fresh derivation.
    return EPOCH_LOST



def rows():
    return snapshot()[0]


def repo_ids():
    """(distinct repo_id, ledger order, or None) + unavailable reason.

    THE IDENTITY QUESTION IS NOT THE CARRIAGE QUESTION, and answering it
    through `rows()` charged the caller for the second. `rows()` is
    `snapshot()[0]`: the whole carriage/landing projection, which spawns git
    per row to decide each obligation's STATUS. A caller that wants only
    "which repositories does this ledger name" reads none of that status and
    pays all of it — measured on the live ledger, over a thousand git spawns
    and the largest single share of `helm doctor`'s wall, to harvest ten
    strings every raw line already carries verbatim.

    WHY THE SET IS THE SAME SET, which is the whole safety argument: a row's
    `repo_id` is stamped by `_new_state` when the row OPENS and no later event
    rewrites it. `_apply` builds every transition from `dict(state)` and the
    fields it copies off an event are the CLOSING ones — `closing_repo_id`,
    `landing_repo_id`, `abandon_repo_id` — never bare `repo_id`. So the
    genesis fold and the full fold agree on this field by construction, and
    `tests.test_dispatches` pins the equality against `rows()` over a ledger
    carrying closes, verdicts and retargets rather than against an absence.

    IT IS THE LEDGER'S OWN PARSER, NOT A SECOND READER. The cheap read is
    cheap because it skips `_apply`, not because it skips validation: the same
    `checked_events` door, the same `_new_state`, the same first-accepted-
    genesis-wins rule `_fold` uses — including that a genesis `_new_state`
    declines does NOT consume the id, so a later well-formed event for that id
    still opens it. A hand-rolled JSON scan would drift from all four.

    UNAVAILABLE IS None, NEVER AN EMPTY LIST. An unreadable ledger has not
    proved that no repository hosts retips, and a caller asserting an absence
    over [] would be asserting it over a read that never happened.
    """
    projscope.spend_or_raise("dispatch repository identity read")
    events, unavailable = eventledger.checked_events(ledger_path())
    if unavailable:
        return None, unavailable
    opened, out = set(), []
    for row in events:
        rid = str(row.get("id") or "")
        if rid in opened:
            continue
        try:
            state = _new_state(row)
        except Exception:                               # noqa: BLE001
            continue        # a raising genesis leaves the id UNOPENED, exactly
        if not state:       # as `_fold` does, so a later event may still open it
            continue
        opened.add(rid)
        repo = state.get("repo_id")
        if isinstance(repo, str) and repo and repo not in out:
            out.append(repo)
    return out, None


# The event kinds that can OPEN a row. `_new_state` accepts a v3 `dispatch`,
# and the historical v1/v2 snapshots whose event is absent, "add" or "posting".
# Anything else - a close, a verdict, a hold - is legitimately NOT a genesis
# and skipping it asserts nothing.
_GENESIS_EVENTS = (None, "dispatch", "add", "posting")


def genesis_lanes(events):
    """({lane label}, None) - or (None, reason) when a genesis is UNREADABLE.

    THE QUESTION is "did a real dispatch ever open a row under this label",
    which is this module's to answer: eventledger validates STRUCTURE and only
    dispatches knows IDENTITY. A caller collecting `lane` off raw events
    accepts anything that merely NAMES a lane and reports rows never opened.

    IT RETURNS THREE STATES BECAUSE TWO IS THE BUG. The first cure returned
    labels-or-nothing and folded "a genesis I cannot canonically understand"
    into "no genesis" - so a FUTURE v4 dispatch, which passes strict parsing
    AND `_valid_identity` but which `_new_state` declines because it accepts
    only v3, made the caller print a confident absence for a lane that has a
    row. Strictly worse than the raw projection it replaced, and reachable
    under ordinary rolling version skew on a shared ledger.

    So an identity-bearing row whose event IS a genesis kind and which
    `_new_state` still declines POISONS THE WHOLE READ. That is deliberate and
    it is the conservative direction: this projection's only consumer uses it
    to assert an ABSENCE, and one row it cannot read means it cannot know the
    label set is complete. A valid NON-genesis - a close, a verdict - is
    skipped silently, because skipping it asserts nothing.

    THE THREE-STATE DISCIPLINE APPLIES AT EVERY LAYER THAT CAN SKIP AN INPUT,
    not once at the boundary. The caller already distinguished a missing
    ledger from an unreadable one and that correctness is exactly what made
    this level feel handled.
    """
    lanes, seen = set(), set()
    for row in events or ():
        try:
            rid = str(row.get("id") or "")
            if rid in seen:
                continue
            state = _new_state(row)
            if state:
                seen.add(rid)
                lane = str(state.get("lane") or "")
                if lane:
                    lanes.add(lane)
                continue
            if row.get("event") in _GENESIS_EVENTS and _valid_identity(row):
                return None, ("unreadable genesis candidate id=%s event=%r v=%r"
                              % (rid[:12] or "?", row.get("event"),
                                 row.get("v")))
        except Exception as exc:
            # A row that RAISES is equally unreadable - the same refusal, not
            # a silent skip, or the exception path becomes the hole the
            # version path just stopped being.
            return None, "genesis read raised %s" % type(exc).__name__
    return lanes, None


def history(rid):
    return [row for row in eventledger.events(ledger_path())
            if str(row.get("id") or "") == str(rid)]


def _group(events):
    """{row id: [every event]} from ONE already-read event list — grouped in a
    single pass, the shape a whole-ledger projection wants instead of one
    history() reparse per row (that per-row reparse is O(N^2) here)."""
    out = {}
    for row in events:
        out.setdefault(str(row.get("id") or ""), []).append(row)
    return out


def snapshot_and_events():
    """(state by id, events by id, ACCEPTED events by id, accepted verdicts,
    unavailable) — every projection of the dispatch ledger a whole-board reader
    needs, from ONE read.

    THREE, NOT TWO, and the third is the one a caller cannot reconstruct. The
    raw slice is what the record HOLDS (annotations included — a notify-failed
    marker is a real fact the fold never applies), and the accepted slice is
    what this projection TOOK. A reader that has only the first has to guess
    which same-kind event moved the state, and guessing by order picks the
    refused one; a reader that has only the second silently loses every fact
    the fold does not act on. The DIFFERENCE between them is a finding in its
    own right — the ledger holds a transition this projection would not honour
    — and `landreq` names it on the row rather than dropping it.

    THE REASON IS RETURNED, NOT SWALLOWED, and that half is older than the
    straddle below. `events()` drops checked_events' unavailable and answers
    `[]`, byte-identical to a ledger that is genuinely empty; a projection
    built on that keeps its rows and loses every TRANSITION TIMESTAMP, so
    `_age_s` fabricates 0 and a day-old loop renders as seconds old with
    nothing stalled. This function has one read and one reason, so there is no
    longer a half of it that can fail quietly.

    THE READS WERE BOTH FINE AND THE BOARD STILL CONTRADICTED ITSELF. This is
    not the missing-value shape that every other defect on the land card is:
    nothing here is absent, unreadable or malformed. `landreq.project` called
    `snapshot()` and then `events_by_id()`, two separate reads of an
    append-only file, and an append that landed BETWEEN them was in one and not
    the other. A reproduction: the snapshot says the row is OPEN, the grouped
    events already carry its `delivered`, and the card renders the headline
    OPEN — owed by the integrator — above its own timeline showing
    AWAITING_REVIEW, with `notified: true` beside it. Every read succeeded, so
    nothing was unavailable and nothing was going to say so.

    A value check cannot fix that; the two derivations have to share ONE
    boundary. A single `checked_events` is exactly that boundary: an
    append-only ledger read once yields a coherent PREFIX of history (the
    parser drops an unterminated tail, so a half-written line is never part of
    it), and both projections are folds of that same prefix. A row appended
    during the read is simply not in this projection — it is in the next one,
    which is what "a board as of one instant" means. It is also one ledger
    parse instead of two.

    AND THE READ IS STRICT, which `snapshot()` is not. A complete corrupt JSONL
    line is SKIPPED by the default reader — the historical projection contract,
    written so one bad row cannot blind every obligation a mutation verb needs
    — and that leniency reaching this surface meant a corrupted ledger answered
    `unavailable: null` with whatever rows survived, which the card renders as
    "the dispatch ledger READ cleanly" with the nav badge at 0. A probe
    reproduced exactly that. This projection makes a CLAIM ABOUT THE WHOLE
    RECORD, so it is the one read that may not silently drop part of it; the
    receipt index next door has been strict for the same reason since the day
    an unreadable one rendered as "no receipt". The verbs keep their lenient
    read deliberately: refusing to cancel a dispatch because some other row is
    malformed helps nobody, while a board that quietly omits a row is the one
    sentence this surface exists never to say.

    AND THE ACCEPTED VERDICTS RIDE THE SAME READ, for the same reason one layer
    up. The gate epoch is an APPEND POSITION, so the boundary and the rows
    judged against it have to be counted in one pass over one prefix; counted
    by a second read they are two numberings, and a row would be placed before
    or after a cutover measured over events this board never saw. `_fold`
    already counts them while it folds, so this is the fold reporting what it
    counted rather than a caller re-reading the ledger to recount it."""
    events, acc, unavailable = _ledger_fold(strict=True, want_events=True)
    if unavailable:
        return {}, {}, {}, {}, unavailable
    taken = {rid: [events[i] for i in at] for rid, at in acc.taken_at.items()}
    return acc.out, _group(events), taken, acc.verdicts, None


def _row_ended(row, snap, index, cycles):
    """(word, carrier) — the word that ENDED `row`, plus the successor that
    carries it when that word is `superseded`; (None, None) while it is LIVE.

    LIVE IS OPEN OR HELD, NOT RETIRED, AND CARRIED BY NO SUCCESSOR. A held row
    is paused, not finished, so it is live. A closed or retired row is named by
    `closed_state`, the word a refusal on that row would say. An open row whose
    obligation a successor holds is `superseded`, and `carrier` answers that
    rather than the frozen `superseded_by` pointer: when the first successor
    died and nobody took the work, the row still owes it and stays live.

    AN UNREADABLE ROW IS LIVE. Nothing proves it ended, and every unknown in
    this module resolves toward visible. That includes a row whose fold met a
    kind this helm has no arm for (`unknown_event_kinds`): its `status` here
    is this binary's reading, which stopped at that event, and a kind it
    cannot read may be the one that reopened the row — so its ended word is
    not this binary's to say, and the collision on it stays visible."""
    from . import query                 # DEFERRED, as `_open` does.
    if not isinstance(row, dict) or unknown_event_kinds(row):
        return None, None
    if not query.query_is_open(row):
        return closed_state(row), None
    kid = carrier(row, snap, index, cycles)
    return ("superseded", kid) if kid is not None else (None, None)


def seq_collisions():
    """([collision], unavailable) — every event whose `seq` REUSES a seq the
    fold already applied on the same row. Each collision is a dict: `id`,
    `seq`, `event`, `ts`, `position` (its append index), `reused` (the kind
    of the applied event that holds that seq), `ended` (the word that ended
    the row carrying it, or None while that row is live — see `_row_ended`)
    and `carried_by` (the successor's id when `ended` is `superseded`).

    WHAT ONE MEANS. The fold takes one event per seq per row, in append order,
    so a later event carrying a taken seq is DROPPED: it is on the ledger and
    no reader honours it. The writer that appended it computed `seq` from a
    fold that could not see the event already holding it, which is exactly
    what a helm older than an event kind on that row does
    (`KNOWN_EVENT_KINDS` says why its own self-check passed). The dropped
    event is usually a real decision, such as a verdict, that its author
    believes was recorded.

    EVERY COLLISION IS RETURNED, WHATEVER STATE ITS ROW IS IN. A dropped event
    on a row that has since ended is history, not debt, and the renderers
    split on `ended`: the doctor row WARNs only on a live row and counts the
    rest, and `helm dispatch collisions` lists all of them. The split is not
    made here, so a census and its drilldown cannot disagree about the whole.

    ONE STRICT READ, THE SAME ONE `snapshot_and_events` MAKES: a census of the
    whole record may not silently skip part of it, and the taken positions
    come from the fold of exactly these events. The rows' states come from the
    same fold, so each collision is classified against the record it was
    found in."""
    events, acc, unavailable = _ledger_fold(strict=True, want_events=True)
    if unavailable:
        return None, unavailable
    took = {index for at in acc.taken_at.values() for index in at}
    applied, out = {}, []
    for index, event in enumerate(events or ()):
        if not isinstance(event, dict) or type(event.get("seq")) is not int:
            continue
        rid = str(event.get("id") or "")
        held = applied.setdefault(rid, {})
        if index in took:
            held.setdefault(event["seq"], event.get("event"))
        # ONLY A SEQ-ORDERED EVENT CAN COLLIDE. A v1/v2 snapshot the fold
        # declines carries whatever seq its whole-row copy held; the fold never
        # ordered those by seq, so an equal number there is not a writer
        # reusing one.
        elif type(event.get("v")) is int and event["v"] >= 3 \
                and event["seq"] in held:
            out.append({"id": rid, "seq": event["seq"],
                        "event": event.get("event"), "ts": event.get("ts"),
                        "position": index, "reused": held[event["seq"]]})
    kids = _successor_index(acc.out) if out else {}
    cycles = _cycle_components(kids) if out else {}
    for c in out:
        word, kid = _row_ended(acc.out.get(c["id"]), acc.out, kids, cycles)
        # `carrier` returns only a successor with an id.
        c.update(ended=word, carried_by=str(kid["id"]) if kid else None)
    return out, None


def seat_activity(events=None):
    """(validated, unresolved, newest validated stamp, unavailable) — WHEN EACH
    SEAT LAST ACTED, as the projection that VALIDATES those acts answers it.

    THE ONE POSITIVE INSTRUMENT AN UNROSTERED NAME STILL HAS. Every liveness
    reader in helm is keyed on the roster — `landreq._presence_state` says so in
    as many words — so a name with no roster row is described by none of them,
    which is a MISS and not a measurement. This ledger is not roster-keyed: it
    is append-only, it reaches back 52 days on this board, and it records who
    ASKED and who ANSWERED.

    IT LIVES AT THE WRITER AND IS COMPUTED BY THE FOLD, and three rounds of one
    finding are why. A reader that walks the raw events beside this projection
    has to answer, in its own words, which kinds record a hand, whether a
    transition counted, and which identity a verdict's binding is validated
    against — and each local answer was wrong in the one direction that spends
    somebody's rows. The last of them let a verdict carrying a `recipient`
    field supply the very identity its own author was then checked against. The
    fold already decides all three, for the whole fleet, once.

    IT IS A LOWER BOUND ON ACTIVITY IN THE `validated` DIRECTION, which is the
    safe one: an act this cannot credit reads as MORE silent, never more active.
    `unresolved` is what stops that bound from being read as silence — see
    `_credit_act`, which is also where the empty-tuple kinds and the verdict's
    own rule are argued.

    THE NEWEST STAMP IS THE INDEX'S OWN CURRENCY and only DATED validated acts
    count toward it: a ledger that stopped being written must not expire a
    fleet, and an act nobody can date says nothing about whether it is still
    being written. `unavailable` non-None is UNKNOWN and every caller refuses;
    an unreadable ledger and an empty one are deliberately different answers.

    WHAT IT COSTS, MEASURED RATHER THAN GUESSED: reading the validating
    projection is a whole fold, and the raw walk it replaces was not. On the
    live ledger's 14577 rows: the fold 7.5s, the crediting above 1.0s, against
    0.3s for the walk — so one `retire` action went from 4.6s to 10.2s
    in-process, paying exactly ONE extra fold (counted: this is called once per
    action, because `landreq._reach_bundle` measures every instrument at most
    once and hands the same reading to every party and every reason). That
    saving is now the one this paragraph asked for: the index rides the fold
    `snapshot()` maintains (task/2770), so this read restores the checkpoint
    and credits only the tail; no memo of THIS index exists apart from it, and
    the checkpoint lives under HELM_HOME so no test shares another's. (Measured
    on the 17,542-event ledger, the crediting itself was 0.28s of a 14.9s
    fold.) The board is unaffected — `landreq.stalls()` calls this ZERO times
    (counted, over the live board).
    """
    if events is None:
        # STRICT, LIKE EVERY READER THAT CLAIMS SOMETHING ABOUT THE WHOLE
        # RECORD. This index's answer is "nothing in this ledger shows that seat
        # acting", and a read that silently skipped a complete corrupt line
        # cannot say it — the skipped line is exactly the shape that spends a
        # row. Unreadable is UNKNOWN here and every caller refuses.
        #
        # AND IT IS THE SAME FOLD `snapshot()` MAINTAINS (task/2770): the
        # checkpoint carries this index beside the rows, so asking it costs a
        # restore and a tail rather than a second whole replay.
        _events, acc, unavailable = _ledger_fold(strict=True, want_actors=True)
        if unavailable:
            return None, None, None, unavailable
        actors = acc.actors
    else:
        actors = {"validated": {}, "unresolved": {}}
        _fold(events or (), actors=actors)
    dated = [stamp for stamp in actors["validated"].values()
             if stamp != UNDATED_ACT]
    return (actors["validated"], actors["unresolved"],
            max(dated) if dated else None, None)


def _retired_admin_by(row):
    """The bounded REASON this row was administratively retired, or None.

    THE ONE PURE OWNER of the question "is this row terminal by retirement".
    Pure and action-agnostic on purpose: it carries no OPEN/HELD rule and no
    idempotence rule, because those belong to each verb's own status law and
    mixing them here is how a shared predicate acquires a caller's opinion.

    THE REASON IS AN ENUM VALUE OR THE WORD "unspecified" — never the row's
    free-form note or measurement. A refusal is printed to an operator and
    replayed into logs; echoing an unbounded field there would put arbitrary
    recorded text on a surface that has no business carrying it.

    Retirement PRESERVES `status`, which is exactly why this exists: every
    reader and writer keyed on the status word alone saw an open row.
    """
    from . import query
    # ONE SEAM FOR THE QUESTION, ONE FOR THE REASON. `query_is_retired_admin`
    # already owns "is this row administratively retired"; re-testing the
    # field here would be a second copy of the same rule, and the two would
    # drift the first time the flag's spelling or its shape changed. This
    # function adds exactly ONE thing on top of it — the bounded reason — and
    # asks the canonical predicate for the rest.
    if not query.query_is_retired_admin(row):
        return None
    reason = str(row.get("retire_reason") or "").strip()
    return reason if reason in RETIRE_REASONS else "unspecified"


def _retired_admin_refusal(row, noun="dispatch"):
    """The refusal a door shows over a retired row — bounded, and it says
    WHEN, because an operator hitting this needs to know the terminality was
    deliberate rather than corruption."""
    when = str(row.get("retire_ts") or "").strip() or "an unrecorded time"
    return ("%s %s was administratively retired (%s) at %s — retirement is "
            "terminal. Open a successor row; a retired row is not mutated "
            "back to life" % (noun, str(row.get("id") or "?")[:12],
                              _retired_admin_by(row), when))


def _resolve_row(current, rid, noun="dispatch",
                 list_hint="helm dispatch list", allow_retired=False,
                 allow_unknown_kinds=False):
    """Resolve one canonical row from an unambiguous printable ID prefix.

    List surfaces print 12 characters, so every sibling mutation verb accepts
    that identifier. An exact historical short ID does NOT outrank a longer ID
    it prefixes: both are matches, and mutating either would be a guess. The
    ambiguity refusal names the CANDIDATE COUNT beside the token: "use more
    characters" is a different amount of work over 2 collisions than over 40,
    and the reader deciding whether to lengthen the prefix or re-list should
    not have to run the listing to find out which.

    `allow_unknown_kinds` is the READER'S opt-in, and only a reader passes it:
    a row whose fold met an event kind this helm does not know is refused to
    every writer by default (see `unknown_kinds_refusal`), while a listing
    keeps reading it and prints `unknown_kinds_note` beside it.
    """
    rid = str(rid or "").strip()
    if not _ID.fullmatch(rid):
        return None, "no such %s: %s (%s)" % (noun, rid, list_hint)
    hits = [row for key, row in current.items() if key.startswith(rid)]
    if len(hits) == 1:
        row = hits[0]
        # THE VOCABULARY RUNG, DEFAULT-CLOSED, AHEAD OF TERMINALITY. It is
        # the same door the terminality rung below chose and for the same
        # reason: every writer resolves its row here before it computes a
        # seq, so a NEW mutation door inherits the refusal without knowing it
        # exists. It runs first because a binary that cannot read the row
        # cannot trust its own reading of retirement either.
        if not allow_unknown_kinds:
            refusal = unknown_kinds_refusal(row, noun)
            if refusal:
                return None, refusal
        # THE TERMINALITY RUNG, DEFAULT-CLOSED, AT THE ONE DOOR EVERY WRITER
        # PASSES THROUGH. 15 of this resolver's 17 call sites are mutation
        # paths, so the safe default is the common case; a reader that
        # legitimately wants a retired row says `allow_retired=True` out loud,
        # and those opt-ins are few enough to read. A NEW mutation door
        # inherits this guard without knowing it exists, which is the whole
        # point — the previous shape needed every door to remember.
        if not allow_retired and _retired_admin_by(row):
            return None, _retired_admin_refusal(row, noun)
        return row, None
    if len(hits) > 1:
        return None, ("ambiguous %s id prefix: %s (%d candidates — use more "
                      "characters)" % (noun, rid, len(hits)))
    return None, "no such %s: %s (%s)" % (noun, rid, list_hint)


def _first_difference(left, right):
    """The 1-based first differing character of two unequal strings."""
    for i, pair in enumerate(zip(left, right), 1):
        if pair[0] != pair[1]:
            return i
    return min(len(left), len(right)) + 1


# WHAT KIND OF WORK A DISPATCH IS, recorded rather than inferred.
#
# The ledger could say WHO sent a dispatch, to WHOM, against WHICH tip, and
# WHEN — and could not say whether it asked the recipient to BUILD something or
# to REVIEW something already built. That is the one fact needed to answer "how
# is my fleet's capacity allocated", so the question was unanswerable from the
# only durable record of the fleet's work.
#
# It cost a full night. The integrator was told to run BUILD lanes overnight and
# sent 15 REVIEW rounds on his own work instead; no surface reported it, because
# no surface could. The obvious retrofit — read the lane name, treat "-r1"/"-r2"
# as review — was measured against the real ledger and MISCLASSIFIED 5 OF 17:
# cursor-reap, watchdog-response-side, verdict-polarity, retrieval-stopwords and
# dangling-remedy-scope were all reviews that simply were not named like one. A
# 70%-accurate classifier cannot carry an alarm threshold.
#
# UNKNOWN IS A VALUE, NOT A DEFAULT. Every row written before this field existed
# has kind=None, and the mix report counts those in their own bucket rather than
# assuming. Guessing would put a made-up number under a real-looking heading,
# which is worse than the gap it papers over.
KINDS = ("build", "review")


def clean_kind(kind):
    """(kind, None) or (None, reason). None is ALLOWED and means unrecorded."""
    if kind is None:
        return None, None
    k = str(kind).strip().lower()
    if k in KINDS:
        return k, None
    return None, ("kind must be one of %s (got %r) — or omit it, which records "
                  "UNKNOWN rather than a guess" % ("|".join(KINDS), kind))


# WORK IDENTITY IS A CHAIN, NOT A LANE STRING.
#
# `_base` minted a row whose only statement of WHAT WORK IT WAS was the LANE:
# free text, with no link to any prior row — no parent, no supersedes, no
# lookup. Three separate failures in a single night, pointing in OPPOSITE
# directions, all reduce to that one missing relation, which is why patching any
# one of them individually kept failing:
#
#   1. 124 open land loops with ZERO terminal rows, and 43 of 46 READY rows
#      pointing at commits that no longer exist. A close-out discharged 10 with
#      proof and REFUSED 11 more where the verb WOULD have accepted a candidate
#      — because any later approved trunk commit trivially "contains" a change
#      already on trunk, so accepting it would have been a verb-blessed lie.
#      Nothing on the ledger said WHICH later round continued THIS one.
#   2. Two seats built the SAME cold-start work in parallel and neither could
#      know. The integrator then told one to stop and was WRONG — their lane
#      touched a file the landed fix never did.
#   3. The meld spiral guard counted a LANE STRING, so it OVERCOUNTED a finished
#      spiral (a name reused by unrelated later work) and UNDERCOUNTED a
#      continuing one: when lane `gate-mints-its-own-evidence` landed,
#      `gate-epoch-is-append-order` opened immediately to close a hole in it —
#      round 10 of the same work under a new name, invisible to a same-lane rule.
#
# So EVERY new dispatch declares EXACTLY ONE of `--new-work` or
# `--supersedes <dispatch-id>`. The lane stays a LABEL — free text, renamable,
# useful to a human reading a list — and the CHAIN ROOT is work identity. A
# REQUIRED field is how you make a relation real; an optional one is how the 124
# got built, because the caller who most needs to declare a link is exactly the
# caller in a hurry.
#
# REJECTED IN THE MELD, do not revive: "require --supersedes when the same lane
# already has an open FIX row". It misses renamed continuations, and failure 3
# is the example that killed it — same work, new lane string, invisible to any
# same-lane rule.
#
# HISTORY IS LEGACY AND STAYS LEGACY. Every row written before this field
# carries no chain, and nothing retro-fits one: a chain invented at replay time
# would be a guess wearing a relation's clothes.
CHAIN_UNKNOWN = "UNKNOWN"


def _replay_chain(value):
    """The replayed form of a chain field -> id | None | CHAIN_UNKNOWN.

    THE SINGLE OWNER, called ONLY by `_new_state`. Every consumer reads the
    chain off a snapshot row, which has already been through here, and compares
    against None / CHAIN_UNKNOWN directly. DO NOT re-validate at the consumers:
    two checks rejecting the same bad value means reverting either leaves the
    other rejecting, so NEITHER is measured and the pair silently rots.

    THREE STATES, and they must stay distinguishable through replay — the same
    shape `clean_gate_caps` uses, for the same reason:

      ABSENT (None)       a row from before chains existed. LEGACY, and the
                          honest reading of it. Not "new work", not unknown.
      well-formed id      the chain.
      present, MALFORMED  UNKNOWN — never quietly demoted to ABSENT, because
                          ABSENT is the branch that PERMITS (legacy discharge,
                          lane-keyed spiral), and a corrupted field has not
                          earned the permissive reading.
    """
    if value is None:
        return None
    if isinstance(value, str) and _ID.fullmatch(value):
        return value
    return CHAIN_UNKNOWN


CHAIN_REQUIRED = (
    "every dispatch must declare its WORK IDENTITY: pass exactly one of "
    "--new-work or --supersedes <dispatch-id> (new_work=True / "
    "supersedes=<id> in the library). The lane is a LABEL and cannot say "
    "whether this continues earlier work; a row that names neither is unlinked "
    "work, which is how 124 land loops were built with no way to close them")

CHAIN_EXCLUSIVE = (
    "--new-work and --supersedes are exclusive: work is either new or a "
    "continuation of exactly one earlier row, never both")


# THE BRIEF'S DURABLE CAP, in UTF-8 BYTES — never characters. A char cap and a
# byte cap are THE SAME FUNCTION on ASCII, so an arm written against ASCII
# fixtures cannot tell them apart; the ledger line, meanwhile, is bytes. 4000
# characters of CJK is 12000 bytes on disk, and the cap that matters is the one
# the file pays.
#
# WHY 4000, MEASURED, NOT ESTIMATED. Cross-referencing every `message_hash` on
# the live ledger against the live chat store (blake2b-128 over each DM body)
# recovered 10 dispatch briefs whose text still exists: byte lengths 362, 370,
# 736, 762, 845, 845, 887, 1714, 2665, 3212 — median 845, mean 1240, max 3212.
# 4000 stores every one of them WHOLE, with room above the observed maximum,
# while refusing the pathological tail: `send` accepts 16000 CHARACTERS, which
# is up to 64000 bytes, and a 64 KB line in an append-only JSONL every list read
# parses is not a brief, it is a document.
#
# WHAT IT COSTS THE LEDGER, MEASURED 2026-08-27 on <helm home>/_global/
# dispatches.jsonl: 10359 lines, 4469542 bytes, mean 431.5 bytes/line; 2831 of
# those lines are v3 creations (mean 673.2 bytes) and 2608 of THOSE (92.1%)
# carry a message_hash, i.e. would now also carry a body. At the measured mean
# brief of 1240 bytes those 2608 rows add 3.23 MB — the ledger would be 7.70 MB
# instead of 4.47 MB, 1.72x. At the CAP (every brief maximal, which no measured
# brief is) they add 10.4 MB, 3.3x. Growth is bounded and linear in sends; it is
# not bounded in the absence of a cap, which is the whole reason for one.
#
# RE-MEASURED, AND THE POPULATION MOVED. The ten briefs above maxed at 3212
# bytes and 4000 stored every one of them whole. Briefs of 5 to 11 KB are
# NORMAL now — delegated builds and adversarial reviews write coverage lists,
# and the list is the part a reviewer needs. Four consecutive sends from one
# seat measured 10606, 7714, 7440 and 6906 bytes and stored 3986, 3978, 3980
# and 3978: every one cut BEFORE or INSIDE the coverage list the brief existed
# to carry, and the stored notice told the reader the tail was not recoverable
# from this ledger. So the number below is no longer "stores every brief
# whole"; it is what the LEDGER can afford per row, and that is all it claims.
#
# THE CAP STAYS AND STOPPED BEING THE ONLY COPY. Raising it trades a review
# defect for a ledger every list read parses more slowly, and no single value
# fixes both. The brief is now stored WHOLE in one file the row references
# (BRIEF_DIR / `write_brief_file` below); this bounds what the ROW costs, and
# every reader that renders a brief reads that file instead. A row's bounded
# copy is the FALLBACK, and a reader that shows it says so out loud.
MESSAGE_BODY_CAP = 4000
# THE BINDING CAP IS ON THE SERIALIZED VALUE, NOT THE RAW TEXT (review's
# ruling). What costs the ledger is what json.dumps writes, and the two
# diverge in both directions at once: the old cap kept 4000 raw bytes and then
# APPENDED its truncation notice on top (4001 ASCII bytes in -> 4194 stored),
# while a body of control characters escapes roughly SIXFOLD (4000 raw ->
# 24002 serialized). A cap measured on the wrong side of its own encoding is
# not a cap. MESSAGE_BODY_CAP survives as a cheap RAW ADMISSION bound — it
# rejects the obviously-oversized before any encoding work — but it cannot
# substitute for the one below, which is the one the ledger actually pays.
MESSAGE_BODY_SERIALIZED_CAP = 8192

# `body_of`'s second slot. STRINGS, and they live in the second slot ONLY, so
# no body can ever be mistaken for one of them.
BODY_UNRECORDED = "unrecorded"    # UNKNOWN: the row predates body storage
BODY_NONE = "no-dm-body"          # KNOWN-EMPTY: the path carried no DM at all

# The truncation notice's opening, named ONCE because the WRITER emits it and
# READERS test for it. Spelled twice it drifts once, and the drift is silent:
# every truncated brief would start reading as complete.
BODY_TRUNCATED_MARK = "[helm: BRIEF TRUNCATED"


def redact_bodies(rows):
    """The same structure with every `message_body` replaced by its SIZE.

    RECURSIVE, because the verbs that leak it do not all emit a flat row list.
    `list --json` emits rows; `retip --json` emits {"retips": [...]}; `rebind
    --json` emits {"old": ..., "new": ...}. Redacting only the flat case fixed
    ONE verb and left its siblings publishing the same field — which is the
    partway-wiring defect, and this is the fourth time tonight I have shipped
    it. Following the value to EVERY emitter is the whole cure.

    THE BRIEF DOES NOT RIDE A LISTING. `dispatch list --json` dumps whole rows,
    and storing `message_body` ON the row turned a verb that emitted metadata
    into one that emits the full text of every brief the ledger holds —
    including CLOSED rows — to anyone who can run it. The listing verb predates
    the field; the BOUNDARY is the field's to owe, because shipping it without
    one makes every historical brief readable by a command written when there
    was nothing to read.

    A SIZE, NOT A DELETION. The body stays reachable per-row through
    `dispatch triage <id>`, which is scoped to one obligation the caller names.
    Publishing the byte count in its place keeps "this row HAS a brief"
    answerable from the listing, because absence and privacy are different
    facts and a redaction that erased both would make an unbriefed row and a
    private one look identical.

    Extracted rather than left inline at the print: a boundary nothing can
    exercise is a boundary nobody can show holds, and the live ledger carries
    no bodies yet.
    """
    if isinstance(rows, dict):
        out = {}
        for k, v in rows.items():
            if k == "message_body":
                if v is not None:
                    out["message_body_bytes"] = len(str(v).encode("utf-8"))
                continue
            out[k] = redact_bodies(v)
        return out
    if isinstance(rows, list):
        return [redact_bodies(r) for r in rows]
    if isinstance(rows, tuple):
        return tuple(redact_bodies(r) for r in rows)
    return rows


def _serialized_len(text):
    r"""The bytes this string actually costs the LEDGER once encoded.

    THE FLAGS MUST MATCH THE WRITER'S, and mine did not. The bare
    `json.dumps` default is ensure_ascii=True, which escapes every non-ASCII
    codepoint to \uXXXX; `eventledger` writes with ensure_ascii=False and
    encodes UTF-8. Measured: 1000 emoji cost 4002 bytes on disk and my counter
    called them 12002 — a 3x OVER-count that truncated briefs which would have
    fit whole.

    That is the SAME defect this cap was rewritten to fix, reintroduced inside
    its own cure: the original bug measured raw bytes when the ledger paid
    serialized ones, and my fix measured a serialization the ledger never
    performs. Measuring "the encoded size" is not enough — it has to be the
    encoder that actually writes.
    """
    return len(json.dumps(text, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _body_fits(text):
    """BOTH BOUNDS, MEASURED ON THE ASSEMBLED VALUE.

    The raw cap is an ADMISSION bound and the serialized cap is what the ledger
    pays; the review ruling is that the second binds and the first "may exist
    but cannot substitute" — which is not a licence to stop enforcing the
    first. Demoting it to a fast path would silently TRIPLE what a brief may
    store, so both are checked, and both are checked against the value actually
    written (text PLUS its truncation notice) rather than the text alone. The
    original defect was precisely that gap: 4000 raw kept, notice appended,
    4194 stored under a cap of 4000.
    """
    return (len(text.encode("utf-8")) <= MESSAGE_BODY_CAP
            and _serialized_len(text) <= MESSAGE_BODY_SERIALIZED_CAP)


def _body_with_notice(kept, total, has_ref=False):
    """The stored value for a truncated brief: the kept text AND the notice.

    The notice is part of the STORED VALUE and not a flag beside it, so every
    reader that can render a body at all renders the fact that it is short —
    including readers that never learn this field has a cap.
    """
    if has_ref:
        return kept + (
            "\n\n%s — %d of %d UTF-8 bytes stored on the dispatch row. The whole "
            "brief is stored by reference; read it with `helm dispatch triage <id>`.]"
            % (BODY_TRUNCATED_MARK, len(kept.encode("utf-8")), total))
    return kept + (
        "\n\n%s — %d of %d UTF-8 bytes stored on the dispatch row. The rest "
        "went out in the original DM only and is NOT recoverable from this "
        "ledger; ask the sender for the tail.]"
        % (BODY_TRUNCATED_MARK, len(kept.encode("utf-8")), total))


def _store_body(message, has_ref=False):
    """The durable form of one dispatch brief: the text, capped, SAYING SO.

    A SILENT TRUNCATION READS AS A COMPLETE BRIEF, which is worse than storing
    nothing — the recipient of a rebound obligation would work the stored prefix
    believing they had the whole instruction. So the notice is part of the
    STORED VALUE, not a flag beside it: every reader that can render the body at
    all renders the fact that it is short, including the ones that never learn
    this field has a cap.

    CUT ON A CHARACTER BOUNDARY. Slicing UTF-8 bytes mid-codepoint and decoding
    strictly would raise, and decoding it away would emit a replacement
    character; `errors="ignore"` drops exactly the split tail (the source is a
    `str`, so there are no other invalid bytes to lose).

    Returns None for no message at all, so `_base` writes the KNOWN-EMPTY None
    rather than an empty string that reads as a brief with no words in it."""
    message = str(message or "")
    if not message:
        return None
    raw = message.encode("utf-8")
    if _body_fits(message):
        return message
    # BINARY SEARCH ON THE WHOLE STORED VALUE. The notice carries the byte
    # counts, so its own length depends on the answer — reserving a fixed
    # allowance for it would be another guess about encoding. Measuring the
    # ASSEMBLED value instead makes the notice, the escape expansion and the
    # split-codepoint tail all fall out of one real measurement.
    total = len(raw)
    lo, hi = 0, len(message)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _body_fits(_body_with_notice(message[:mid], total, has_ref=has_ref)):
            lo = mid
        else:
            hi = mid - 1
    if not _body_fits(_body_with_notice("", total, has_ref=has_ref)):
        # THE BOUND WINS, AND AN UNFITTABLE NOTICE IS A MISCONFIGURATION
        #. I had made the
        # NOTICE win and knowingly exceeded the cap, reasoning that a silently
        # truncated brief reads as a COMPLETE instruction and truth beats
        # size. The ruling is that a declared final serialized-byte cap which
        # may be knowingly exceeded IS NOT A CAP — every downstream sizing and
        # append guarantee built on it becomes false, which is a broader harm
        # than the one I was protecting against.
        #
        # So neither value is written: not the over-bound truthful one, not the
        # silent lie. The CONFIGURATION is refused at the writer boundary,
        # loudly, naming the two numbers that cannot both be satisfied. This is
        # unreachable at any real cap — the notice is ~150 bytes against caps
        # of 4000 and 8192 — so it fires only when someone has set a cap that
        # cannot express a truncation at all.
        raise ValueError(
            "dispatch body cap is too small to hold a truncation notice: "
            "MESSAGE_BODY_CAP=%d / MESSAGE_BODY_SERIALIZED_CAP=%d cannot fit "
            "the %d-byte notice, so no value would both fit the bound and say "
            "it was cut. Raise the cap; a cap that may be knowingly exceeded "
            "is not a cap."
            % (MESSAGE_BODY_CAP, MESSAGE_BODY_SERIALIZED_CAP,
               len(_body_with_notice("", total, has_ref=has_ref).encode("utf-8"))))
    if lo >= len(message):
        # NOTHING WAS CUT, SO NOTHING MAY CLAIM IT WAS. The raw admission bound
        # is deliberately lower than the serialized one, so a body can fail the
        # cheap check and still fit whole — and my first draft appended a
        # notice reading "4001 of 4001 bytes stored", a truncation warning on
        # an intact brief. A recipient who trusts that goes asking the sender
        # for a tail that does not exist. Caught by probing the function, not
        # by reading it.
        return message
    return _body_with_notice(message[:lo], total, has_ref=has_ref)


# ------------------------------------------------------------- the brief file
#
# THE ROW KEEPS A BOUNDED COPY; THE WHOLE BRIEF LIVES IN A FILE BESIDE THE
# LEDGER. The cap above is right about the LEDGER and wrong about the BRIEF:
# every list read parses every line, so an unbounded body on the row is a real
# cost paid by every reader — but a brief that is CUT is a review-quality
# defect at the door, because the cut lands inside the coverage list the brief
# exists to carry and the reviewer reads half an instruction without knowing
# it. Both facts are true at once, so they are answered in two places: the row
# stays bounded, and the brief is stored WHOLE in one file the row REFERENCES.
#
# CONTENT-ADDRESSED, NOT ROW-ADDRESSED, and the reason is an ordering one. The
# row's id is not final until inside the append lock (`send` derives it from an
# operation key after `_base` has built the row), so a file named for the id
# could not be written BEFORE the row without duplicating that derivation
# outside the lock. The brief's blake2b-128 digest is known the instant the
# message is — it is the same function `message_hash` already uses — so naming
# the file for the digest lets the write happen first, makes the write
# idempotent (the same brief re-sent is the same file), and makes the reference
# SELF-PROVING: a reader recomputes the digest over the bytes it read and knows
# whether the file is the brief the sender sent, without trusting the ledger.
#
# FILE FIRST, ROW SECOND, ALWAYS. A kill between the two leaves an ORPHAN FILE,
# which costs a few kilobytes and is invisible to every reader. The opposite
# order would leave a DANGLING REFERENCE — a row promising a brief that does
# not exist — which is worse than the truncation this replaces, because a
# truncated body at least says so in its own text.
BRIEF_DIR = "dispatch-briefs"

# THE HARD CEILING, IN UTF-8 BYTES, AND IT REFUSES RATHER THAN CUTS.
#
# WHERE THE NUMBER COMES FROM. Two measurements of the same population. The
# cap's own comment above recovered ten briefs off the live ledger whose
# byte lengths ran 362..3212, max 3212. A later census of four consecutive
# sends from one seat measured 10606, 7714, 7440 and 6906 bytes — briefs now
# carry coverage lists, so 5..11 KB is the normal band and the 3212 the cap was
# sized from is no longer near the top of it. 32768 is 3.1x the largest brief
# ever measured here and ~3x the top of the current band: room for a brief
# three times bigger than any that has existed, and still far below the 64 KB
# the cap's comment already names as "not a brief, it is a document".
#
# IT IS REACHABLE, WHICH IS WHY IT IS A BYTE BOUND. `send` already refuses
# anything over 16000 CHARACTERS — 16000 bytes of ASCII, but up to 64000 bytes
# of multibyte text. So the character bound stops meaning anything exactly
# where this one starts binding, which is the same byte-versus-character lesson
# MESSAGE_BODY_CAP carries: a bound measured in the wrong unit is not a bound.
# A ceiling of 64 KB would sit ABOVE everything the character bound admits and
# could never fire at all, which is a decoration, not a guard.
#
# REFUSAL, NOT TRUNCATION, is the whole point of the number existing: the
# sender is present and can split the brief, and a refusal that names the two
# numbers costs one retry. Cutting costs a review.
BRIEF_CEILING = 32768

# The broken-reference notice's opening, named ONCE because the READERS emit it
# and the arms test for it — the same reason BODY_TRUNCATED_MARK is a constant.
# A reference that cannot be resolved must never be silent: the row's bounded
# copy is then all there is, and a reader shown it without this line believes
# it has the whole brief.
BRIEF_REF_BROKEN_MARK = "[helm: BRIEF FILE"

# A REFERENCE READ OFF A LEDGER ROW IS UNTRUSTED INPUT and is about to become a
# path. Exactly 32 hex characters — the spelling `brief_digest` produces —
# so no value on any row, hand-edited or corrupt, can escape the directory.
_BRIEF_REF = re.compile(r"[0-9a-f]{32}")


def brief_dir():
    """Where whole briefs live: beside the ledger, under the helm home.
    Declared in `helm/registry.py` `projections()` at birth — an undeclared
    store under that root is a squatter, which is how the ledger's own
    neighbours came to be unclassified."""
    return os.path.join(home.global_dir(), BRIEF_DIR)


def brief_digest(text):
    """blake2b-128 over the brief's UTF-8 bytes — the SAME function and digest
    size `send` computes `message_hash` with, deliberately, so the two can
    never disagree about what identifies a brief."""
    return hashlib.blake2b(str(text).encode("utf-8"),
                           digest_size=16).hexdigest()


def brief_file_path(ref):
    """The file one reference names, or None if the reference is not one.

    Returns None rather than raising on a malformed ref, because every caller
    is a READER of a possibly-corrupt row and has a fallback to take."""
    ref = str(ref or "")
    if not _BRIEF_REF.fullmatch(ref):
        return None
    return os.path.join(brief_dir(), ref + ".txt")


def write_brief_file(message):
    """(ref, nbytes, err) — store one brief WHOLE, before any row names it.

    IDEMPOTENT BY CONSTRUCTION. The path is the content's digest, so re-sending
    the same brief re-writes the same bytes to the same name; there is no
    version to reconcile and no id to collide.

    ATOMIC, through `pk.atomic_write` — a torn brief would fail its own digest
    check at every reader, which is loud but needless when a sibling-plus-rename
    costs nothing.

    An OSError here is returned, never raised: the caller is the send door, and
    the honest answer to "the brief could not be stored" is a refusal naming the
    reason, not a traceback and not a row that quietly points nowhere."""
    message = str(message or "")
    raw = message.encode("utf-8")
    ref = brief_digest(message)
    path = brief_file_path(ref)
    try:
        pk.atomic_write(path, message)
    except OSError as exc:
        return None, len(raw), (
            "the brief could not be stored whole under %s (%s), so this row "
            "would reference a file that does not exist. Nothing was written."
            % (brief_dir(), exc))
    return ref, len(raw), None


def read_brief_file(ref, declared_bytes=None):
    """(text, problem) — the whole brief a reference names, PROVEN.

    THE DIGEST IS RECOMPUTED, NEVER TRUSTED. The reference IS the digest, so
    verifying costs one hash over bytes already in memory and turns "the row
    says this file is the brief" into "these bytes hash to what the row says".
    Without it a replaced, truncated or half-written file would be rendered as
    the sender's instruction with no reader able to tell.

    EVERY FAILURE IS A SENTENCE, NEVER None. A caller that gets a problem has a
    fallback to take — the row's bounded copy — and the whole defect this
    module is curing is a bounded copy presented as if it were whole. So the
    problem text is written to be PRINTED beside that fallback, not swallowed."""
    path = brief_file_path(ref)
    if not path:
        return None, ("%s REFERENCE MALFORMED — the row carries %r where a "
                      "32-character brief digest belongs, so the whole brief "
                      "cannot be located. What follows is the row's BOUNDED "
                      "copy, which may be TRUNCATED.]"
                      % (BRIEF_REF_BROKEN_MARK, str(ref)[:64]))
    try:
        # newline="" — NO universal-newline translation. The digest was taken
        # over the sender's bytes, and a brief with an internal CR or CRLF
        # (pasted from a Windows editor, a heredoc with CR) hashed differently
        # after the default reader folded its CRLF to LF, so an INTACT file
        # reported DIGEST MISMATCH and the whole brief fell back to its cut
        # copy. The reader returns the bytes the writer wrote.
        with pk.open_regular(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, ("%s MISSING — the row references %s and that file could "
                      "not be read (%s). What follows is the row's BOUNDED "
                      "copy, which may be TRUNCATED.]"
                      % (BRIEF_REF_BROKEN_MARK, path, exc))
    actual = brief_digest(text)
    if actual != str(ref):
        return None, ("%s DIGEST MISMATCH — %s hashes to %s and the row says "
                      "%s, so that file is NOT the brief this row was sent "
                      "with. What follows is the row's BOUNDED copy, which may "
                      "be TRUNCATED.]"
                      % (BRIEF_REF_BROKEN_MARK, path, actual, str(ref)))
    if isinstance(declared_bytes, int) \
            and len(text.encode("utf-8")) != declared_bytes:
        # UNREACHABLE WHILE THE DIGEST HOLDS, and kept anyway: the row states
        # two independent facts about the same file and a reader that checks
        # only one cannot report the other going wrong. It costs one len().
        return None, ("%s LENGTH MISMATCH — %s is %d bytes and the row "
                      "declares %d. What follows is the row's BOUNDED copy, "
                      "which may be TRUNCATED.]"
                      % (BRIEF_REF_BROKEN_MARK, path,
                         len(text.encode("utf-8")), declared_bytes))
    return text, None


def brief_of(row):
    """(text, absent_reason, problem) — the brief, as WHOLE as it can be had.

    THE ONE DOOR EVERY RENDERER GOES THROUGH. `body_of` answers what is ON THE
    ROW and keeps its tri-state; this answers what the SENDER SENT, which is
    the question every surface that shows a human a brief is actually asking.
    Keeping them separate matters: `body_of`'s contract is about a ledger
    field and is pinned by arms that predate the file store, while this one may
    reach the disk and may therefore fail in ways a field read cannot.

    THREE OUTCOMES, and the middle one is the reason the tuple has a third slot:

      (whole, None, None)        the row references a file, the file read, and
                                 its bytes hash to what the row says.
      (bounded, absent, LOUD)    the row references a file that is missing,
                                 unreadable, or not the brief. The caller gets
                                 the row's bounded copy AND a sentence saying
                                 so, and printing the copy without the sentence
                                 is the defect this whole lane is about.
      (body, absent, None)       the row references nothing — a row from before
                                 this store, or a path that carried no brief.
                                 `body_of`'s own three readings, unchanged.

    A ROW WITH NO REFERENCE IS NOT A FAILURE. The fallback is the ONLY path for
    every row written before this lane, and it must stay silent for them or the
    loud line stops meaning anything."""
    body, absent = body_of(row)
    ref = row.get("brief_ref") if isinstance(row, dict) else None
    if ref is None:
        return body, absent, None
    whole, problem = read_brief_file(ref, row.get("brief_bytes"))
    if problem:
        return body, absent, problem
    return whole, None, None


def brief_was_cut(row):
    """Whether this row's ONLY copy of its brief is a truncated one.

    The census predicate, and the polarity is deliberate: a row whose stored
    body carries the truncation mark but which ALSO references a whole file has
    lost nothing and must not be listed, or the census fills up with rows that
    need no action and the ones that do get lost in it."""
    if not isinstance(row, dict):
        return False
    if row.get("brief_ref") is not None:
        return False
    body, _absent = body_of(row)
    return bool(body) and BODY_TRUNCATED_MARK in body


def _cut_sent_bytes(body):
    """The ORIGINAL byte length a truncation notice reports, or None.

    Read back out of the notice rather than stored beside it, because the
    notice is the only witness a pre-lane row has: `_body_with_notice` writes
    "<kept> of <total> UTF-8 bytes stored", and <total> is what the sender
    actually sent. Derived from the WRITER's own format string so the two
    cannot drift into disagreement."""
    probe = _body_with_notice("", 0)
    head, _sep, tail = probe.partition("0 of 0 UTF-8 bytes")
    if not _sep:
        return None                       # the notice's shape moved; say so by
    marker = head.split(BODY_TRUNCATED_MARK)[-1]   # answering UNKNOWN, never a
    match = re.search(                             # number derived from a guess
        re.escape(BODY_TRUNCATED_MARK) + re.escape(marker)
        + r"(\d+) of (\d+) UTF-8 bytes", str(body or ""))
    return int(match.group(2)) if match else None


def _cut_kept_bytes(body):
    """The KEPT byte length a truncation notice reports, or None."""
    probe = _body_with_notice("", 0)
    head, _sep, tail = probe.partition("0 of 0 UTF-8 bytes")
    if not _sep:
        return None
    marker = head.split(BODY_TRUNCATED_MARK)[-1]
    match = re.search(
        re.escape(BODY_TRUNCATED_MARK) + re.escape(marker)
        + r"(\d+) of (\d+) UTF-8 bytes", str(body or ""))
    return int(match.group(1)) if match else None


def brief_census(current=None):
    """Every OPEN row whose brief survives only as a CUT copy.

    READ-ONLY, and it exists because the cure is not retroactive: rows sent
    before this lane stored a prefix and the tail went out in the DM only. The
    integrator cannot re-send what nobody enumerates, and a defect whose
    population is unknown is a defect nobody finishes closing."""
    if current is None:
        current, unavailable = snapshot()
        if unavailable:
            return None, unavailable
    out = []
    for row in (current or {}).values():
        if str(row.get("status") or "") != "open" or not brief_was_cut(row):
            continue
        body, _absent = body_of(row)
        out.append({
            "id": str(row.get("id") or ""),
            "lane": str(row.get("lane") or "-"),
            "recipient": str(row.get("recipient") or "-"),
            "sender": str(row.get("sender") or "-"),
            "stored_bytes": len(str(body or "").encode("utf-8")),
            "sent_bytes": _cut_sent_bytes(body),
        })
    out.sort(key=lambda r: -(r["sent_bytes"] or 0))
    return out, None


def body_of(row):
    """(body, absent_reason) — the dispatch brief stored ON the row.

    THREE READINGS, and the two absent ones are NOT the same fact:

      ("text", None)          the row carries a brief. It may be TRUNCATED, and
                              if so the text says so itself (`_store_body`).
      (None, BODY_NONE)       KNOWN-EMPTY: this row was minted by a path that
                              carries no DM brief at all (`dispatch add`), so
                              there is nothing to have lost.
      (None, BODY_UNRECORDED) UNKNOWN: this row PREDATES body storage. A brief
                              may well have been sent; this ledger cannot say.
                              Never report it as "no brief".

    THE DISCRIMINATOR IS KEY PRESENCE, not the value — the same reasoning
    `stale_writer_rows` uses on `_V3_TRACKED_KEYS`: a row is not malformed for
    lacking a field it PREDATES. Every row `_base` writes from now on carries
    `message_body`, explicitly None when the path had no DM; no row written
    before this carries the key at all. v3 replay is `dict(row)` so the
    distinction survives it, and the v1/v2 branch builds a fixed key set that
    never included this one — which is correct, because those schemas are
    historical by construction.

    A TUPLE, DELIBERATELY. A bare return would make `if body_of(row):` collapse
    UNKNOWN into EMPTY at every call site, which is the exact bug this reader
    exists to prevent; unpacking is forced instead.

    HONEST LIMIT: a caller that hands this a row REBUILT from a subset of
    fields gets BODY_UNRECORDED even when the stored row has a body. That is the
    safe direction — unknown, never falsely-empty — but it means this must be
    given a snapshot/ledger row, not a projection."""
    if not isinstance(row, dict) or "message_body" not in row:
        return None, BODY_UNRECORDED
    body = row["message_body"]
    # A PRESENT-BUT-MALFORMED VALUE READS UNKNOWN, never KNOWN-EMPTY — exactly
    # the split `_replay_chain` makes for the same reason. BODY_NONE is the
    # branch that PERMITS (nothing was lost, no re-brief owed), and a field of
    # the wrong TYPE has not earned it. `None` and `""` are well-formed
    # statements that there were no words; anything else is corruption.
    if body is None or body == "":
        return None, BODY_NONE
    if not isinstance(body, str):
        return None, BODY_UNRECORDED
    return body, None


def _resolve_chain(new_work, supersedes, current=None, repo_id=None):
    """(parent_id | None, chain_root | None, err) for one NEW row.

    `repo_id` is the repository the NEW row binds, and it is the CHAIN
    AUTHORITY: a parent belonging to another repository cannot authorize a
    child here. Omitted, it falls back to this package's own repository, which
    is what every caller predating multi-project writes meant.

    `chain_root=None` with no error means THIS ROW ROOTS ITS OWN CHAIN; the
    single writer seals it to the row's own id once that id is final (`send`
    derives the id from an operation key AFTER `_base` builds the row, so the
    seal cannot live here).

    ONE HOP, never a walk: the parent already carries its resolved root, so a
    cycle is impossible by construction — a parent must exist on the ledger
    before a child can name it, so no child can be its own ancestor.

    UNKNOWN NEVER MEANS NEW WORK. An unreadable ledger, an id that resolves to
    nothing, an ambiguous prefix, and a parent whose own chain is corrupt all
    REFUSE the write. Refusing costs one retry; permitting mints another
    unlinked row, and unlinked rows are the defect.
    """
    supersedes = str(supersedes or "").strip()
    if bool(new_work) == bool(supersedes):
        return None, None, CHAIN_EXCLUSIVE if new_work else CHAIN_REQUIRED
    if new_work:
        return None, None, None
    if current is None:
        current, unavailable = snapshot()
        if unavailable:
            return None, None, (
                "dispatch ledger unavailable (%s) — the superseded chain is "
                "UNKNOWN and the row is NOT recorded; an unreadable parent is "
                "never silently new work" % unavailable)
    parent, err = _resolve_row(current, supersedes, allow_retired=True)
    if err:
        return None, None, "--supersedes " + err
    # A CHAIN STAYS INSIDE ONE REPOSITORY, AND THE AUTHORITY IS THE ROW'S OWN
    # REPOSITORY — NOT THIS PACKAGE'S (task/2437). This compared the parent
    # against `home_repo_id()`, which is the same helm-checkout assumption
    # `write_scope` removed one layer up: a registered project's second row
    # could never supersede its own first one, because neither row's repository
    # is helm's. `repo_id` is the repository the NEW row binds, resolved by the
    # caller before this runs, so the invariant is stated where it actually
    # lives. It is exactly as strict for everything the old door admitted: back
    # then the new row's repository HAD to equal home, so parent == home and
    # parent == the new row's repository were the same comparison.
    # AN UNRECORDED PROVENANCE AUTHORIZES NOTHING BEYOND THE HOME REPOSITORY
    # (task/2437 round two, the P1). A parent carrying no `repo_id` — the true
    # reading of every v1 row, since `_replay` preserves an absent repo rather
    # than inventing one — must not SKIP this comparison. Skipping costs nothing
    # while the write door is an equality test: the only repository
    # a new row could bind was this package's own, so a skipped comparison and
    # a satisfied one were the same outcome. Registry admission separated them.
    # Now a newly admitted project B can name a HISTORICAL helm row as its
    # parent, and the whole close ladder then treats the old obligation as
    # carried: `_same_chain` accepts the derived root, the carrier index takes
    # the child, and `owed` suppresses the parent. A helm obligation nobody
    # reviewed would disappear because another repository claimed its chain.
    #
    # SO UNKNOWN IS READ AS THE HOME REPOSITORY, WHICH IS WHAT IT MEANT. Every
    # row that predates the `repo_id` stamp was written through the equality
    # door, i.e. from this package's own checkout, so home is the provenance
    # those rows actually have — and only a child in THAT repository may
    # continue their chain. The refusal names the missing field, because a
    # caller told "parent repository None is not this row's repository" cannot
    # tell a cross-repository attempt from a corrupt parent.
    authority = repo_id or home_repo_id()[0]
    parent_repo_id = parent.get("repo_id")
    if parent_repo_id is None:
        parent_repo_id, home_why = home_repo_id()
        if parent_repo_id is None:
            return None, None, (
                "--supersedes %s: that row records NO repository and this helm "
                "can establish none of its own (%s), so the repository whose "
                "chain this would continue is UNKNOWN — refusing rather than "
                "letting an unrecorded provenance authorize an arbitrary "
                "repository" % (parent["id"][:12], home_why or "unreadable"))
        if authority is None or _real(parent_repo_id) != _real(authority):
            return None, None, (
                "--supersedes %s: that row records NO repository, so its "
                "provenance is this helm's own %s — and this row binds %r. An "
                "unrecorded provenance authorizes nothing beyond the "
                "repository the row could only have been written from; "
                "supersede it from there, or `--new-work`."
                % (parent["id"][:12], parent_repo_id, authority))
    elif authority is None or _real(parent_repo_id) != _real(authority):
        return None, None, (
            "--supersedes %s: parent repository %r is not this row's "
            "repository %r — refusing foreign chain authority"
            % (parent["id"][:12], parent_repo_id, authority))
    root = parent.get("chain_root")     # already replayed; see `_replay_chain`
    if root == CHAIN_UNKNOWN:
        return None, None, (
            "--supersedes %s: that row's chain_root is malformed, so the chain "
            "it belongs to is UNKNOWN — refusing rather than rooting new work "
            "at a corrupt relation" % parent["id"][:12])
    # A CANCELLED or still-OPEN parent is a legitimate parent, and a legitimate
    # FORK (two rows naming one parent) is legitimate too — see the lifecycle
    # table in docs/VERBS.md. Refusing any of them would push the caller to
    # `--new-work`, i.e. to a lie, which is strictly worse than the honest link.
    # `rebind` proves it: it CANCELS the old row and then opens the replacement,
    # so its parent is always cancelled and its chain is always real.
    return parent["id"], root or parent["id"], None


def _base(recipient, lane, ref, note, deadline_s, repo, sender=None,
          operation_key=None, message_hash=None, rid=None, kind=None,
          new_work=False, supersedes=None, _current=None,
          _ref_branch=_INFER_REF_BRANCH, message_body=None, acted_by=None,
          custodian=None, brief_ref=None, brief_bytes=None):
    recipient, err = _recipient_operand(recipient)
    if err:
        return None, err
    recipient_display = recipient.display
    lane, err = _clean(lane, "lane", 160)
    if err:
        return None, err
    lane = _strip_lane_prefix(lane) or lane
    if note is not None:
        note, err = _clean(note, "note", 1000)
        if err:
            return None, err
    # THE ONE PLACE THAT KNOWS BOTH. `_base` is the single writer of a dispatch
    # row and the only seam holding the deadline and the kind at once, so it is
    # where an unstated deadline becomes a kind-appropriate one. Callers that
    # state a deadline are untouched.
    if deadline_s is None:
        deadline_s = default_deadline_s(kind)
    deadline_s, err = _deadline(deadline_s)
    if err:
        return None, err
    if sender is not None and not _TOKEN.fullmatch(str(sender)):
        return None, "sender must be an exact 1-64 character seat token"
    # SAME GATE, SAME SENTENCE, for the seat that PERFORMED a move. `acted_by`
    # is read off a live process identity rather than off an argument, but it
    # reaches the ledger through this one writer like every other stamp, and a
    # writer that validates one identity field and waves the next one through
    # is a writer with a hole in it.
    if acted_by is not None and not _TOKEN.fullmatch(str(acted_by)):
        return None, "acting seat must be an exact 1-64 character seat token"
    # VALIDATED AT THE OWNER LAYER, not only at the CLI. `_base` is the single
    # writer of a dispatch row, and it accepted `kind="buld"` verbatim: the
    # typo reached the ledger, counted as neither build nor review, and showed
    # up in the report as UNKNOWN — indistinguishable from an honestly
    # unrecorded historical row. A guard that lives only in argument parsing
    # protects the CLI, not the DATA.
    kind, err = clean_kind(kind)
    if err:
        return None, err
    # THE REPOSITORY IS RESOLVED BEFORE THE CHAIN NOW, because the chain's
    # authority IS the repository (task/2437): a parent may only authorize a
    # child in its own repository, and `_resolve_chain` can no longer answer
    # that from this package's location. The chain grammar refusals
    # (CHAIN_REQUIRED / CHAIN_EXCLUSIVE) stay cheap and loud; they now come
    # SECOND to "this path is not a Git working tree", which is a caller making
    # two mistakes at once and either sentence repairs one of them.
    info = _repo_info(repo)
    if not info:
        return None, "ref needs a Git working tree (--repo PATH)"
    # BEFORE the tip resolution, because a ref no project claims is not this
    # ledger's to validate.
    #
    # AN UNKNOWN AUTHORITY REFUSES, IT DOES NOT WAVE THROUGH — and `write_scope`
    # keeps that, one layer down: an unresolvable REGISTRY refuses everything
    # rather than admitting it, which is the polarity the earlier registry-based
    # door got backwards (it refused only on a KNOWN mismatch, so it refused
    # NOTHING while its authority was unknown, an escape reachable by deleting
    # a rebuildable cache file). The package path stays a second admission so a
    # wiped registry cannot lock helm out of its own ledger.
    #
    # A ROW IS KEYED BY THE REPOSITORY ITS REF LIVES IN, and this ledger holds
    # a row for ANY repository a registered project claims (task/2437).
    # `write_scope` carries the whole argument: the equality door that stood
    # here could only be satisfied by copying the helm package into the target
    # repository, and one project did, so 63 rows came in through a copy rather
    # than through a door. An unregistered repository is still refused — with
    # the registration step named, which is a repair the caller can perform.
    _project, scope_why = write_scope(info["repo_id"])
    if scope_why:
        return None, scope_why
    # INSIDE `_base`, BECAUSE FOUR WRITERS SHARE IT AND NOTHING ELSE. `add` and
    # `send`'s three branches each reach the ledger through here, and a gate
    # bolted beside each of them is the shape that guarantees one never gets it
    # (task/2480 said so about the usability door, one function up).
    light_ok, light_refusal, _light_note = _project_light_rung(
        info["repo"], kind, new_work)
    if not light_ok:
        return None, light_refusal
    parent, chain_root, err = _resolve_chain(new_work, supersedes, _current,
                                             repo_id=info["repo_id"])
    if err:
        return None, err
    display, err = _clean(ref, "ref", 256)
    if err:
        return None, "ref is required so the verdict can bind an exact tip"
    tip, inferred_branch = _resolve_tip(
        info["repo"], display, infer_sha_branch=_ref_branch is _INFER_REF_BRANCH)
    if not tip:
        return None, "ref is missing, ambiguous, or not a commit in this repository"
    # THE DOOR BELONGS HERE, on the EXACT tip every append stores. Its first
    # home was an early check in `send` against `raw_tip`, which is
    # `str(ref).strip().lower()` — so `HEAD` arrived as `head` and any
    # mixed-case ref was mangled BEFORE the door could resolve it, while
    # `_base` went on to resolve the ORIGINAL spelling. A door reading a
    # different string from the one the ledger binds is not a door.
    refusal = snapshot_tip_refusal(info["repo"], tip)
    if refusal:
        return None, refusal
    ref_branch = inferred_branch if _ref_branch is _INFER_REF_BRANCH \
        else _ref_branch
    if ref_branch is not None and (not isinstance(ref_branch, str)
                                   or not ref_branch.startswith("refs/heads/")
                                   or any(c.isspace() for c in ref_branch)):
        return None, "ref branch override must be a canonical local branch or null"
    ts = pk.now_ts()
    return {"v": 3, "event": "dispatch", "seq": 0,
            "id": rid or os.urandom(16).hex(), "ts": ts,
            "recipient": str(recipient), "recipient_display": recipient_display,
            "lane": lane, "tip": tip, "ref": display,
            "ref_branch": ref_branch,
            "note": note, "deadline_s": deadline_s, "kind": kind,
            "source": home.session_id() or "cli", "sender": sender,
            "repo_id": info["repo_id"], "repo_root": info["repo"],
            "operation_key": operation_key,
            "message_hash": message_hash, "status": "open",
            # THE BRIEF, DURABLY. Written on EVERY row from here on — as None
            # when the minting path carried no DM — because `body_of` reads the
            # ABSENCE OF THE KEY as "predates body storage" and the presence of
            # a None as "known to have had no brief". Writing it conditionally
            # would make a modern `dispatch add` row indistinguishable from a
            # 2026-07 send whose text is genuinely lost.
            #
            # `message_hash` STAYS, and stays over the ORIGINAL FULL message
            # (see `send`): it is an existing contract — the auto operation key
            # hashes it, and three historical key derivations in `send` replay
            # through it — and it is the only thing that can still recognise a
            # brief this field had to truncate.
            "message_body": message_body,
            "supersedes": parent, "chain_root": chain_root,
            # THE TRUNK AUTHORITY BINDING, resolved ONCE here so no later
            # retip has to DISCOVER it. Absent when the repository declares
            # none — a send is never refused for lack of one, and the cost
            # lands on the non-FF BUILD retip that actually needs the proof.
            #
            # BUILD ROWS ONLY, and that is a SIDE-EFFECT bound rather than a
            # tidiness one. A REVIEW retip never consumes the stored binding —
            # its identity observes the current authority for itself — so
            # binding one would fetch and ls-remote against a remote for every
            # review dispatch, with the credential prompts, timeouts and
            # offline failures that implies, and write fields nothing reads.
            # PRESENT ONLY ON A MOVE, and its presence is the fact: a row
            # carrying `acted_by` is a row whose `sender` was INHERITED from
            # the obligation it continues rather than authored by the seat that
            # wrote it. Absent on ordinary work, where the two are one seat and
            # a field repeating `sender` would say nothing.
            **({"acted_by": str(acted_by)} if acted_by else {}),
            # CUSTODY TRAVELS WITH THE OBLIGATION, LIKE THE SENDER DOES
            #. A move already inherits `sender` from the row it
            # continues; it did not inherit the CUSTODIAN. So after A authored
            # a row and custody moved A -> B, a later recipient rebind minted a
            # child carrying sender=A and no custodian — and `custodian_of`
            # falls back to the sender, silently handing the delivery leg back
            # to A. B, who had actually taken it, owed nothing and was told
            # nothing. Present only when it DIFFERS from the inherited sender:
            # a field repeating the fallback would say nothing.
            **({"custodian": str(custodian)}
               if custodian and str(custodian) != str(sender or "") else {}),
            # THE WHOLE BRIEF, BY REFERENCE. Two additive fields, PRESENT ONLY
            # WHEN A FILE WAS WRITTEN, and the conditionality is the opposite
            # decision from `message_body` above on purpose. `message_body`'s
            # ABSENCE carries meaning — it is what separates "predates storage"
            # from "had no brief" — so it must be written even as None. These
            # two carry no meaning when absent: no reference simply means the
            # row's bounded copy is all there is, which is exactly the reading
            # every row written before this lane already gets. Writing them as
            # None on every row would add bytes to the commonest row in the
            # ledger (a `dispatch add`, which never has a brief) to say
            # something the absence already says.
            #
            # BOTH OR NEITHER. A length without a digest cannot be proven and a
            # digest without a length loses one of the two independent checks
            # `read_brief_file` makes, so the pair is written as a pair.
            **({"brief_ref": str(brief_ref), "brief_bytes": int(brief_bytes)}
               if brief_ref and isinstance(brief_bytes, int) else {}),
            **(_authority_binding(info["repo_id"], tip)
               if kind == "build" else {})}, None


# Fields `_base` has gained over time. A row is not malformed for lacking one
# it PREDATES — that is ordinary schema growth — so the floor is derived from
# the ledger itself rather than hardcoded.
_V3_TRACKED_KEYS = ("chain_root", "supersedes", "ref_branch", "kind",
                    "repo_root")


def stale_writer_rows(path=None):
    """Creations written by a helm OLDER than the ledger had already seen.

    THE RULE IS MONOTONIC, and that is what makes it self-calibrating: once a
    field has appeared on a v3 creation, every LATER creation should carry it.
    A row missing a field that rows before it already had was written by a
    binary behind the one already in use.

    THE NAIVE VERSION IS WRONG AND I SHIPPED IT FIRST. Comparing against the
    key set `_base` writes today flagged 714 rows, because chain_root and
    supersedes appear on 977 of 1375 creations, ref_branch on 852 and
    recipient_display on 185 — those fields were ADDED at different times and a
    row predating one is old, not malformed. The ledger's own history is the
    only honest floor.

    READS THE RAW LEDGER, never the snapshot: replay fills defaults, so a row
    that arrived malformed looks well-formed to every consumer downstream.

    A DETECTOR, NOT A REPAIR. Council ruling 2026-08-05: an absent chain_root
    is LEGACY identity and is never retrofitted or guessed. This names the
    WRITER, which is the thing that can be fixed.

    HONEST LIMIT: a binary old enough to omit a field is old enough to lack
    this check. It attributes damage after the fact; it cannot stop the write.
    Returns None when the ledger cannot be read — UNKNOWN, never "none found"."""
    path = path or ledger_path()
    seen_at = {}                      # key -> earliest ts that carried it
    creations = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("event") != "dispatch":
                    continue
                if row.get("v") != 3:
                    continue          # v1/v2 are honestly old, not malformed
                ts = str(row.get("ts") or "")
                creations.append((line_no, ts, row))
                for key in _V3_TRACKED_KEYS:
                    if key in row and (key not in seen_at or ts < seen_at[key]):
                        seen_at[key] = ts
    except OSError:
        return None
    out = []
    for line_no, ts, row in creations:
        behind = sorted(k for k in _V3_TRACKED_KEYS
                        if k in seen_at and ts > seen_at[k] and k not in row)
        if behind:
            out.append({"line": line_no, "id": str(row.get("id") or ""),
                        "ts": ts, "sender": str(row.get("sender") or ""),
                        "missing": behind,
                        "first_seen": {k: seen_at[k] for k in behind}})
    return out


_WRITE_WARNINGS = "_write_warnings"  # ephemeral; never persisted in the ledger
# THE ADMISSION ADVISORY, ON ITS OWN EPHEMERAL KEY AND NOT ON THE ONE ABOVE.
# _WRITE_WARNINGS carries the duplicate-successor contract, and
# tests/test_dispatch_chain.py pins its ABSENCE on four ordinary chain
# writes — a recipient-usability note sharing that key made those four go
# red while saying nothing about duplicates. Different claim, different
# channel; the CLI prints both.
_ADMISSION_NOTES = "_admission_notes"  # ephemeral; never persisted either


def _warning_candidate_state(row):
    """The truthful per-row state rendered beside a warning candidate."""
    status = str(row.get("status") or "UNKNOWN").upper()
    polarity = str(row.get("polarity") or "").upper()
    return ("%s/%s" % (status, polarity)
            if status == "VERDICT" and polarity else status)


def _warning_candidates(rows):
    """(rendered, repair) for rows named by a write warning.

    STATUS IS PER ROW, never inferred from the predicate that selected it.
    `_not_closed` deliberately includes HELD work, while `dispatch list --open`
    deliberately excludes it; calling every candidate OPEN sends the operator to
    a surface that cannot show the row. A contrary verdict is rendered with its
    polarity because it can remain an active work branch after its review turn
    closed. The held repair is emitted once after the candidate list, not once per
    row, so mixed populations stay scannable.
    """
    rows = sorted(rows, key=lambda r: str(r.get("id") or ""))
    rendered = ", ".join("%s (%s)" % (
        str(r.get("id") or "")[:12], _warning_candidate_state(r))
        for r in rows[:3])
    if len(rows) > 3:
        rendered += ", …"
    repair = (" Inspect HELD candidates with `helm dispatch list --held`."
              if any(r.get("status") == "held" for r in rows) else "")
    return rendered, repair


def _duplicate_branch_live(row):
    """Whether this direct-successor branch still occupies its parent.

    FIX and SUPERSEDE close a REVIEW turn, not the work identity. Their branch
    remains active until the existing close vocabulary says it was operationally
    retired. A carrier continues that same branch; it does not erase the branch
    from its original parent, so carriage is deliberately not consulted here.
    """
    return _not_closed(row) or (
        row.get("status") == "verdict"
        and row.get("polarity") in ("fix", "supersede")
        and _close_retired_by(row) is None)


def _carries_the_same_patch(repo, reviewed, ref):
    """Does `ref`'s history CONTAIN the change `reviewed` made, after a rebase?

    ANCESTRY ANSWERS NOTHING ABOUT A REBASED BRANCH, and a rebase is the one
    thing that happens to nearly every lane between a FIX verdict and its
    cure: trunk moves, the author rebases, every commit is rewritten, and the
    reviewed tip stops being an ancestor of its own continuation while the
    work is unchanged.

    THIS ASKS THE MODULE THAT ALREADY OWNS THE QUESTION rather than growing a
    second copy of the law. `vcs.landed_state` is the landedness seam — the
    same door `_superseded_work_is_finished` a few thousand lines below
    already spends — and its docstring states this exact finding from its own
    side: "ANCESTRY ASKS THE WRONG QUESTION AND GETS A TRUTHFUL 'NO'. Almost
    nothing lands under the sha its author wrote."

    AND IT CARRIES TWO GUARDS A HAND-ROLLED PATCH-ID COMPARISON DOES NOT. An
    EMPTY DIFF HAS AN EMPTY PATCH-ID, so every empty commit is
    "patch-identical" to every other one — a lane carrying one
    `commit --allow-empty` marker would otherwise read as landed against any
    trunk holding an unrelated empty commit. And `git patch-id` NORMALISES
    WHITESPACE, so the seam demands byte-exactness separately. Either check
    silent answers UNKNOWN, which keeps rather than claims.

    A FAILURE ANSWERS FALSE, never blocks — the same doctrine the ancestry
    probe follows. A guard that turns a slow disk into a blocked fleet is
    worse than the defect it prevents.
    """
    try:
        from . import vcs
        be = vcs.backend(repo)
        return be.landed_state(repo, reviewed, ref) == vcs.PATCH_EQUIVALENT
    except Exception:
        return False


def _continuation_ancestor(row, current):
    """The live CONTRARY-verdicted row on this lane whose reviewed tip this
    row's ref DESCENDS from, or None.

    THE THREE CONJUNCTS ARE EACH DOING WORK. Same canonicalized lane and same
    repository keeps it to work that could plausibly be one thread.
    `_duplicate_branch_live` rather than `_not_closed` is the whole point —
    see the caller — and it is the SAME relation the `supersedes` arm already
    spends, so the two cannot drift into disagreeing about what "still
    active" means. Ancestry is what turns a coincidence of labels into a
    measured continuation.

    A GIT FAILURE NEVER BLOCKS THE WRITE, which is `_branch_moved`'s doctrine
    a few screens down and the right one here too: an unreadable repository, a
    pruned object, an ambient GIT_DIR or a timeout all answer None. Absence of
    evidence has never been allowed to refuse a dispatch in this module, and a
    guard that turns a slow disk into a blocked fleet is worse than the defect
    it prevents.

    THE PROBES ARE BOUNDED BY THE LANE, not by the ledger: only same-lane
    live contrary rows are asked about, which is 0 for the overwhelming
    majority of mints and single digits otherwise.

    RETURNS (row, relation) — "descends" when the ref is a descendant of the
    reviewed tip, "rebased" when it is not but its history CARRIES that
    commit's change under the same patch-id. The caller needs the difference
    because the two license different sentences: one may say the ref descends
    from that commit, and the other may not."""
    repo = row.get("repo_id")
    ref = str(row.get("tip") or "").strip().lower()
    lane = _strip_lane_prefix(row.get("lane"))
    if not _TIP.fullmatch(ref) or not lane or not isinstance(repo, str) \
            or not os.path.isabs(repo) or not os.path.isdir(repo):
        return None, None
    env = _git_env()
    for other in current.values():
        if not isinstance(other, dict) or other.get("id") == row.get("id") \
                or other.get("repo_id") != repo \
                or _strip_lane_prefix(other.get("lane")) != lane:
            continue
        if str(other.get("kind") or "") != "review" \
                or other.get("status") != "verdict" \
                or other.get("polarity") not in ("fix", "supersede") \
                or not _duplicate_branch_live(other):
            continue
        reviewed = str(other.get("reviewed_tip") or "").strip().lower()
        if not _TIP.fullmatch(reviewed) or reviewed == ref:
            continue
        try:
            probe = subprocess.run(
                ["git", "-C", repo, "merge-base", "--is-ancestor",
                 reviewed, ref], capture_output=True, text=True,
                timeout=5, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        if probe.returncode == 0:
            return other, "descends"
        # ANCESTRY MISSED, AND ON THIS LANE THAT IS THE COMMON CASE RATHER
        # THAN THE EXOTIC ONE. Re-derived over this ledger: of the same-lane
        # `--new-work` mints whose ref does NOT descend from a live contrary
        # verdict's reviewed tip and whose objects still exist, 33 of the 37
        # that could be evaluated carry that exact commit's patch-id inside
        # their own history. They are continuations that were rebased, and the
        # rung above is structurally incapable of seeing any of them.
        #
        # THE CENSUS THAT CHOSE ANCESTRY COULD NOT HAVE FOUND THIS: it counted
        # mints whose reviewed tip IS an ancestor, so the rebased population
        # was outside the set it measured. The number it reported was true
        # about the population it asked about.
        #
        # FALSE POSITIVES ARE BOUNDED AND MEASURED: 4 of those 37 are
        # genuinely distinct work sharing a lane label, and `--force` with a
        # recorded reason is the door that already exists for them.
        if _carries_the_same_patch(repo, reviewed, ref):
            return other, "rebased"
    return None, None


def _duplicate_mint_warning(row, current):
    """(warning, needs_force) from the SAME snapshot the writer appends under.

    A shared supersedes parent is strong work identity: another active child
    branch means this mint duplicates a live continuation unless the caller
    explicitly forces a legitimate fork. A shared lane label on the same repo is
    the #1 born-wrong pattern — lanes are names, not identity, but the cost of an
    undetected duplicate is higher than the cost of a deliberate --force. Both
    arms now REFUSE; the writer must declare their intent at write time.
    """
    parent = row.get("supersedes")
    if parent:
        hits = [r for r in current.values()
                if r.get("id") != row.get("id")
                and _duplicate_branch_live(r)
                and r.get("supersedes") == parent]
        if hits:
            candidates, repair = _warning_candidates(hits)
            return ("ACTIVE successor branch%s %s already supersede%s %s — this "
                    "would mint duplicate live work; inspect the existing row%s. "
                    "Continue a later review round with --supersedes "
                    "<active-row-id>, not this parent; pass --force only for a "
                    "deliberate fork (an intentional parallel branch).%s"
                    % ("es" if len(hits) != 1 else "", candidates,
                       "" if len(hits) != 1 else "s", str(parent)[:12],
                       "s" if len(hits) != 1 else "", repair)), True
        return None, False
    hits = [r for r in current.values()
            if r.get("id") != row.get("id") and _not_closed(r)
            and r.get("repo_id") == row.get("repo_id")
            and _strip_lane_prefix(r.get("lane"))
                == _strip_lane_prefix(row.get("lane"))]
    if hits:
        candidates, repair = _warning_candidates(hits)
        return ("NOT-CLOSED row%s %s already use%s lane label %r — same-repo "
                "same-lane duplicates are the #1 born-wrong pattern; use "
                "--supersedes for a continuation, or --force for a deliberate "
                "fork.%s"
                % ("s" if len(hits) != 1 else "", candidates,
                   "" if len(hits) != 1 else "s", row.get("lane"), repair)), True
    # THE CONTINUATION THE ARM ABOVE CANNOT SEE (task/close-ladder). It asks
    # `_not_closed`, which is FALSE for a FIX-verdicted review — and a FIX
    # verdict is precisely the state whose next round IS a continuation. The
    # `supersedes` arm at the top of this same function already knows that and
    # uses `_duplicate_branch_live` ("FIX and SUPERSEDE close a REVIEW turn,
    # not the work identity"); the lane arm was left on the weaker relation,
    # so the one shape that matters walked straight through it.
    #
    # MEASURED on this board 2026-08-12: 40 of 974 `--new-work` mints (4.1%)
    # had a same-lane contrary-verdicted review whose reviewed tip is a git
    # ANCESTOR of the new ref, and in 40 of 40 that row was invisible to the
    # arm above. Live-only rate 31 of 769, also 4.0%.
    #
    # ANCESTRY IS THE RUNG, NOT THE LABEL, and that is what holds it at 4.1%
    # instead of refusing every dispatch onto a lane that holds an open
    # finding. A shared lane label is a NAME; a ref that DESCENDS FROM the very
    # commit an outstanding FIX was about is the same work by construction —
    # which is also why this can name ONE row to supersede instead of handing
    # back a list to choose from.
    #
    # WHAT MISSING IT COSTS is why it is here at all: `--new-work` severs the
    # chain, `superseded` needs a later APPROVE on the SAME chain, and the
    # parent is left with no honest terminal. Three rows sat FINISHED and
    # unclosable for 2 to 14 hours from exactly this.
    ancestor, relation = _continuation_ancestor(row, current)
    if ancestor:
        # THE SENTENCE MUST SAY WHICH RELATION IT MEASURED. "Descends" is a
        # falsifiable claim about this repository, and a writer who checks it
        # on a rebased lane will find it false and discount the whole warning.
        # The rebased branch has its own true sentence, and it is the more
        # useful of the two to read: it tells the author their lane was
        # rebased, which is exactly what made the chain link look unnecessary.
        how = ("this ref DESCENDS from it" if relation == "descends" else
               "this ref's history CARRIES that commit's change under the "
               "same patch-id — the lane was REBASED, which destroys ancestry "
               "and leaves the work identical")
        return ("%s holds a live %s verdict on %s and %s — that is a "
                "continuation, and --new-work severs the chain link "
                "`superseded` needs to close it later. Use --supersedes "
                "%s; --force only if this really is unrelated work that "
                "happens to build on that commit."
                % (str(ancestor.get("id"))[:12],
                   str(ancestor.get("polarity") or "").upper(),
                   str(ancestor.get("reviewed_tip"))[:12], how,
                   str(ancestor.get("id"))[:12])), True
    # WORK IDENTITY BY ROLE, because the label test above CANNOT SEE A RENAMED
    # CONTINUATION — which is the only case the heuristic it enforces exists
    # for. `a-review-of-someone-elses-build-supersedes-it-never-roots-new-work`
    # [1.00] says it in one line: "the LANE NAME being new is not the test,
    # WORK IDENTITY is." The arm above then uses the name as the identity, and
    # its own docstring concedes "lanes are names, not identity" one sentence
    # earlier. So the guard was structurally silent on the shape it was written
    # for, TWICE IN TWO DAYS, both times under an integrator's build row: the
    # 2026-08-05 instance read a warning and overrode it; the 2026-08-06 one
    # (mine) never got a warning at all, because I had renamed the lane —
    # `cured-fix-sweep` -> `cured-fix-awaits-a-reviewer`.
    #
    # THE COST OF MISSING IT IS NOT A DUPLICATE, IT IS AN UNCLOSABLE ROW. The
    # heuristic measured all three doors refusing correctly afterwards: LANDED
    # refuses on ancestry after a rebase, DISCHARGED refuses on the missing
    # link, and `lr land` refuses because a BUILD row's ref is a BASE. Three
    # correct refusals, one row with no honest terminal, and the only verb left
    # is cancel-with-a-reason.
    #
    # WARN, NEVER REFUSE — unlike both arms above. A seat legitimately holds
    # several not-closed builds and may review something unrelated to all of
    # them; role-overlap is evidence, not proof, and refusing on evidence would
    # make the honest case pay for the dishonest one. Naming the candidates is
    # what a writer needs to declare intent.
    if str(row.get("kind") or "").strip() == "review":
        sender = str(row.get("sender") or "").strip().casefold()
        # A CARRIED ROW HAS THE HONEST TERMINAL THIS WARNING EXISTS TO PROTECT,
        # so naming it is crying wolf about the state it wants. `_not_closed`
        # is a STATUS test and the sentence below makes a claim about the row's
        # FUTURE — that it will be left with no way to close — which is false
        # the moment a successor carries the obligation. `carrier` already
        # answers that, and answers it properly: it walks the successor SET
        # rather than the frozen superseded_by pointer, so a dead first
        # successor with a live sibling does not read as uncarried.
        #
        # MEASURED ON THE LIVE LEDGER BEFORE THE CURE, both directions: of 73
        # not-closed BUILD rows, 72 were already CARRIED and exactly 1 was not.
        # So the warning fired on 72 rows whose terminal was already arranged
        # and on 1 that genuinely needed it — and a reader who dismisses it 72
        # times is trained past the case it was built for.
        owed = [r for r in current.values()
                if sender and r.get("id") != row.get("id") and _not_closed(r)
                and str(r.get("kind") or "").strip() == "build"
                and str(r.get("recipient") or "").strip().casefold() == sender]
        if owed:
            # One index and one cycle map for every candidate, not one each.
            index = _successor_index(current)
            cycles = _cycle_components(index)
            owed = [r for r in owed
                    if carrier(r, current, index, cycles) is None]
        if owed:
            candidates, repair = _warning_candidates(owed)
            return ("you have NOT-CLOSED BUILD row%s %s — if this review is "
                    "that work, pass --supersedes <id> instead of --new-work: "
                    "a RENAMED continuation is invisible to every same-lane "
                    "rule, and the build row is then left with no honest "
                    "terminal at all (landed/discharged/lr-land all refuse "
                    "correctly). Ignore this if the review is unrelated.%s"
                    % ("s" if len(owed) != 1 else "", candidates, repair)), False
    return None, False


def _cured_operation_reconciliation(operation, current):
    """Reconcile one cured successor from the locked authoritative snapshot.

    This primitive is deliberately operation-scoped: ordinary explicit-key
    sends keep their historical never-resend contract. Cured retries alone may
    return an already-observed successor as successful proof to stalebot.
    """
    intent = operation["intent"]
    key = intent.get("operation_key")
    parent = intent.get("supersedes")
    if not key or not isinstance(parent, str) or not _ID.fullmatch(parent):
        return None, None, False
    parent_row = current.get(parent)
    if not isinstance(parent_row, dict):
        return None, None, False
    chain_root = parent_row.get("chain_root") or parent
    # A CHILD THAT MOVED NOTHING IS NOT A SUCCESSOR HERE. `carrier`,
    # `cured_unwitnessed` and `moved_nothing` all read a cancelled, withdrawn,
    # abandoned or stranded child as a PASS-THROUGH, and this was the one seam
    # that read it as PROOF — in both directions. A cured retry whose recorded
    # child had been cancelled matched on every semantic field and came back as
    # an already-delivered operation, so the caller was told a live review
    # existed; none did, the cure stayed unwitnessed, and the sweep re-proposed
    # the same row every window with no verb able to end it. Change the
    # reviewer and the SAME dead child failed the count below instead, refusing
    # the retry as "a different or additional successor already carries this
    # row" — an accusation about a corpse. One filter answers both.
    children = [row for row in current.values()
                if isinstance(row, dict) and row.get("supersedes") == parent
                and not moved_nothing(row)]
    if not children:
        return None, None, False
    semantic = ("recipient", "tip", "note", "deadline_s", "repo_id",
                "repo_root", "message_hash", "kind", "supersedes")
    hits = [row for row in children
            if row.get("operation_key") == key
            and all(row.get(field) == intent.get(field) for field in semantic)
            and row.get("chain_root") == chain_root
            and _strip_lane_prefix(row.get("lane"))
                == _strip_lane_prefix(intent.get("lane"))]
    if len(children) != 1 or len(hits) != 1:
        return None, ("a different or additional successor already carries "
                      "this row"), True
    existing = hits[0]
    warning, _ = _duplicate_mint_warning(existing, current)
    if warning:
        existing = dict(existing)
        existing[_WRITE_WARNINGS] = [warning]
    return existing, None, True


#: Carried by the row `_append_dispatch` answers with when THIS call decided
#: to write, lost the race to another writer of the same operation, and found
#: that writer's row on its redo. Never stored: `send` and `add` read it off
#: and drop it.
_WRITTEN_ELSEWHERE = "_written_elsewhere"


def _written_elsewhere(existing, txn):
    """`existing`, marked when this call only found it on a redo."""
    if not txn.redo or not isinstance(existing, dict):
        return existing
    return dict(existing, **{_WRITTEN_ELSEWHERE: True})


def _append_dispatch(row, force=False, alt_ops=(), unique_key=False,
                     cured_operation=None):
    """(row, err, existed) — existed=True means the operation was already on
    the ledger; the caller must treat that as NEVER-SEND-AGAIN.

    `alt_ops` is (key, id) pairs re-derived under HISTORICAL auto-key
    schemas (#142 rounds 3+4, codex): 115 live v3 rows were minted under
    AUTO keys hashed over the lane/-prefixed spelling, and one older live
    row (1ddf37fc) under the PRE-PARENT formula with no chain field at all,
    so the same command hashed today derives a different id and would sail
    past its own row into duplicate refusal. A hit on a legacy id
    reconciles onto the historical row; nothing stored is ever rewritten.

    THE SINGLE WRITER, so this is where a new-work row's chain is SEALED to its
    own id. `send` derives the row id from an operation key after `_base`
    returns, so `_base` cannot know the id it is rooting; doing the seal in each
    caller instead would be two seams that must agree forever, and the one that
    drifts mints a chainless row.
    """
    path = ledger_path()
    given_row, given_alt_ops = row, alt_ops

    def attempt(txn):
        # EACH TRY STARTS FROM THE CALLER'S ROW: a try that re-derived it
        # (`prepare`) must not hand that derivation to the next one.
        row, alt_ops = given_row, given_alt_ops
        if not txn.held:
            return None, "ledger unwritable (%s) — dispatch NOT recorded" % path, False
        current, unavailable = snapshot()
        if unavailable:
            return None, ("dispatch ledger unavailable: %s — duplicate-mint "
                          "check UNKNOWN and dispatch NOT recorded"
                          % unavailable), False
        if cured_operation:
            existing, why, existed = _cured_operation_reconciliation(
                cured_operation, current)
            if existed:
                return _written_elsewhere(existing, txn), why, True
            try:
                row, alt_ops, why = cured_operation["prepare"](current)
            except Exception as exc:              # noqa: BLE001
                return None, ("dispatch preparation failed: %s: %s"
                              % (type(exc).__name__, exc)), False
            if why:
                return None, why, False
        if row.get("chain_root") is None:
            row = dict(row)
            row["chain_root"] = row["id"]        # a root names itself
        if unique_key:
            collision = next((r for r in current.values()
                              if r.get("id") != row.get("id")
                              and r.get("operation_key") == row.get("operation_key")),
                             None)
            if collision is not None:
                return None, ("operation key already recorded as %s — refusing "
                              "a second successor under another sender or route"
                              % str(collision.get("id") or "")[:12]), False
        existing = current.get(row["id"])
        matched_key = row.get("operation_key")
        if not existing:
            for alt_key, alt_id in alt_ops:
                hit = current.get(alt_id)
                if hit is not None:
                    existing, matched_key = hit, alt_key
                    break
        if existing:
            # `kind` IS SEMANTIC. Left out, a retry under the same operation
            # key that changed build->review returned the FIRST row and
            # reported success, so the ledger kept a kind the caller had
            # explicitly corrected — a silent disagreement between what the
            # operator asked for and what the capacity report will count.
            # `supersedes`/`chain_root` ARE SEMANTIC, for the same reason
            # `kind` is: a retry that changed the parent is naming DIFFERENT
            # WORK, and returning the first row would silently keep a relation
            # the caller explicitly corrected. `ref_branch` is deliberately NOT:
            # it is first-write topology evidence, and a retry after rename,
            # movement, or an added alias must return that frozen first row.
            # `message_body` IS DELIBERATELY ABSENT, and leaving it out is the
            # load-bearing choice. `message_hash` already discriminates the
            # message — it is the hash OF it — so adding the body says nothing
            # new about whether two sends are the same work. What it WOULD do is
            # break every legacy row: 2608 send rows on the live ledger carry a
            # hash and no body, so an idempotent retry of any of them would
            # compare None against today's stored text and be refused as
            # "already names different work". `acted_by` is out for the sibling
            # reason: WHO replayed a move does not change WHICH operation it is.
            semantic = ("recipient", "tip", "note", "deadline_s",
                        "sender", "repo_id", "message_hash",
                        "kind", "supersedes")
            # LANE compares CANONICALIZED (#142 r2, finding 3): 128
            # historical v3 rows store the lane/ spelling, a retry re-derived
            # through today's writer arrives bare, and byte-equality read that
            # as different work instead of an idempotent retry. The OPERATION
            # KEY compares against the candidate that MATCHED (a legacy hit
            # carries its historical key), and two SELF-ROOTED rows share
            # chain semantics even though each roots at its own id — the ids
            # differ only because the legacy id was hashed over the old
            # spelling. A LEGACY NULL ROOT (#142 round 4: a row from before
            # chains existed replays chain_root=None) reconciles ONLY against
            # a SELF-ROOTED retry — the semantic gate below already demands
            # supersedes None==None, so this is exactly the new-work replay
            # of a pre-chain command. `is None` is deliberate: a MALFORMED
            # stored root replays CHAIN_UNKNOWN, which has not earned the
            # permissive reading (see `_replay_chain`).
            same_chain = (existing.get("chain_root") == row.get("chain_root")
                          or (existing.get("chain_root") == existing.get("id")
                              and row.get("chain_root") == row.get("id"))
                          or (existing.get("chain_root") is None
                              and row.get("chain_root") == row.get("id")))
            if all(existing.get(k) == row.get(k) for k in semantic) \
                    and existing.get("operation_key") == matched_key \
                    and same_chain \
                    and _strip_lane_prefix(existing.get("lane")) \
                        == _strip_lane_prefix(row.get("lane")):
                # The warning is intentionally not ledger state, but an
                # idempotent retry still needs the writer's current advisory.
                # Recompute it from this same locked snapshot without turning a
                # retry into a second refusal or a second send.
                warning, _ = _duplicate_mint_warning(existing, current)
                if warning:
                    existing = dict(existing)
                    existing[_WRITE_WARNINGS] = [warning]
                return _written_elsewhere(existing, txn), None, True
            return None, "operation key already names different work", True
        warning, needs_force = _duplicate_mint_warning(row, current)
        if warning and needs_force and not force:
            return None, warning, False
        # THE PARENT'S SEQ IS WRITTEN BELOW, SO ITS VOCABULARY IS CHECKED
        # HERE, under the lock and before the successor lands. The chain was
        # resolved on an earlier read (`_resolve_chain`), and a parent can
        # gain an event this helm cannot read between that read and this
        # lock; refusing after the successor had landed would leave a
        # half-written move.
        if row.get("supersedes") and not force:
            refusal = unknown_kinds_refusal(
                current.get(str(row["supersedes"])), "--supersedes dispatch")
            if refusal:
                return None, refusal, False
        if not txn.append(row):
            return None, "ledger unwritable (%s) — dispatch NOT recorded" % path, False
        # ANNOTATE THE PARENT IN THE SAME WRITE SEQUENCE (441c4491). The
        # successor is already on the ledger, so a failure here leaves the
        # parent un-annotated — visible and harmless — rather than a parent
        # marked superseded by a row that never landed. Not transactional, and
        # this says so rather than implying otherwise.
        #
        # --force IS A DELIBERATE FORK: the caller has declared BOTH rows live
        # work, so annotating the parent would erase the intent --force exists
        # to express.
        #
        # IDEMPOTENT: a parent already carrying superseded_by keeps its FIRST
        # successor. Re-annotating would rewrite which round continued it.
        if row.get("supersedes") and not force:
            parent = current.get(str(row["supersedes"]))
            if isinstance(parent, dict) and not parent.get("superseded_by"):
                txn.append({
                    "v": 3, "event": "superseded",
                    "seq": (parent.get("seq") or 0) + 1,
                    "id": parent["id"], "ts": pk.now_ts(),
                    "successor": row["id"]})

        def added():
            pk.event("dispatch-add", row["id"],
                     "%s -> %s" % (row["recipient"], row["lane"]))
            out = dict(row)
            out.update(delivery="needs-confirmation", migration=None,
                       delivery_ref=None, verdict_ref=None, reviewed_tip=None)
            if warning:
                out[_WRITE_WARNINGS] = [warning]
            return out, None, False
        return txn.then(added)
    return _ledger_write(attempt, path)


def acting_author(action="author this dispatch"):
    """PUBLIC name for the caller-identity law below (task/1074).

    It was private and had zero callers outside this module, so `task --mine`
    reached for `seats.own_name()` instead and inherited three defects this
    already refuses: a roster-bound session with no env FALSELY REFUSED, a
    stale inherited HELM_CHAT_NAME read a stranger's rows as its own, and the
    family floor — a name three seats share — would have answered "which rows
    are mine". The docstring below already named `list --mine` as the case it
    serves; only the leading underscore kept it from being used."""
    return _acting_author(action)


def _acting_author(action="author this dispatch"):
    """(seat, err) — THE ONE caller-identity derivation this module owns, and
    THE FLOOR IS NEVER AN AUTHOR (owner-declared P0, 2026-08-02). Both paths used
    seats.derive_seat, whose last rung mints the bare family floor ('claude'
    from a cwd-less auto-name), so dispatch rows were authored by a name three
    seats share: `helm dispatch mix` showed sender 'claude' with 84 reviews,
    and 6 stalled rows owned by that name stop-guard-nagged every claude seat
    and could never be chased, credited, or disowned.

    This is handoff._own_seat's law ("a floor-derived stamp would let them
    claim each other's handoffs") relaxed by exactly one rung: a ROSTER-BOUND
    session may author too — the binding is single-valued (write_roster now
    refuses foreign-sid steals), and refusing it would refuse every
    hand-launched-but-rostered seat. Declared vs rostered DISAGREEING refuses
    (seats.identity_disagreement — the 2026-08-02 inherited-env class); both
    absent refuses with the repair, never a silent floor.

    `action` names what the caller is about to do, so a READ path can refuse in
    its own words WITHOUT minting a second identity mechanism. `list --mine`
    (task/1007) asks the question this has always answered — which seat is this
    process — and every clause here is exactly as load-bearing for "which rows
    are MINE" as for "who authored this row": an inherited HELM_CHAT_NAME reads
    a stranger's obligations as its own, and a floor name like 'claude' would
    hand one seat the rows of three. The write path merely noticed first. The
    default keeps both authoring callers' refusal text byte-identical."""
    from . import seats
    try:
        own = home.chat_name()
    except home.SeatNameError:
        # rejected at the validated source; same graceful refusal shape the
        # exact-token check gives, never an uncaught raise
        return None, "sender must be an exact 1-64 character seat token"
    sid = home.session_id()
    try:
        bound = seats.seat_for_session(sid) if sid else None
    except Exception:      # noqa: BLE001 — an unreadable roster answers
        bound = None       # nothing; the declared name (or the instructive
                           # refusal below) carries the stamp
    if own and bound and str(own).casefold() != str(bound).casefold():
        return None, (
            "refusing to " + action + " under a DISPUTED identity: this "
            "process declares %r but session %.8s is rostered to %r. An "
            "inherited HELM_CHAT_NAME is free; the roster can be corrupt; "
            "only agreement is clean. Fix: unset/re-export HELM_CHAT_NAME, or "
            "`helm chat seat disown %s %.8s`"
            % (own, str(sid), bound, bound, str(sid)))
    sender = own or bound
    if sender:
        return sender, None
    return None, (
        "refusing to " + action + " as the family floor: no declared "
        "seat name and no roster binding for this session. Fix your identity "
        "first: export HELM_CHAT_NAME=<your-seat> (a helm-launched pane has "
        "it; helm launch/seat spawn mint it), or bind this session: `helm "
        "chat join`, or claim your name: `helm chat seat rename <sid8> "
        "<seat>`. A floor name like 'claude' is shared by every claude seat "
        "— its rows can never be chased, credited, or disowned")




def _validate_recipient_rostered(recipient, force):
    """(ok, reason) — refuse an unrostered recipient at write time unless forced.

    Unlike _recipient_gate (the CLI door), this validates INSIDE the library
    function so every entry point — CLI, web, direct library call — gets the
    same roster guard. send() and add() now own the refusal instead of
    outsourcing it only to the CLI parser.

    ABSENT  (recipient resolved, roster populated, not in it) -> REFUSED.
    UNKNOWN (empty/corrupt/unreadable roster)                -> PROCEED (fail-open).
    JOINED                                                    -> PROCEED.
    force=True                                                -> PROCEED unconditionally.
    """
    if force:
        return True, None
    from . import seats, cli
    cap = seats.recipient_capability(str(recipient))
    if cap["membership"] != "ABSENT":
        return True, None
    hint = ""
    candidates = cap.get("_candidates", ())
    if candidates:
        try:
            hint = cli.suggest(cap["canonical"], candidates, n=3)
        except Exception:                # noqa: BLE001
            pass
    return False, (
        "recipient %r has no roster row, so nobody can receive it%s — "
        "`helm chat seats` lists the live seats. Pass `force=True` to "
        "address a seat before it joins."
        % (cap["canonical"], hint))


def _tier_note(recipient, kind):
    """The approval-tier advisory for a REVIEW write, or None.

    ONE OWNER FOR TWO WRITE PATHS, AND THAT IS THE WHOLE REASON IT IS A
    FUNCTION. send() builds its own row and does NOT route through add(), so a
    note attached inside one of them rides exactly half the writes. Measured:
    siting this in add() alone removed it from every `helm dispatch send`, and
    two existing arms caught it — which is also why the advisory must never be
    re-homed by editing one call site.

    The kind restriction is the advisory's own scope, not a caller's policy: it
    asks whether an APPROVE could bind, and a build row produces no verdict.

    AND IT ASKS THE KIND THE ROW WILL CARRY, NOT THE ONE THE CALLER TYPED.
    `clean_kind` lowercases and strips, and BOTH doors normalise AFTER calling
    this — `add` one line before `_base`, `send` while holding its own
    `kind_value`. So `--kind Review` minted a row recorded as "review" and got
    no advisory, because a raw-string comparison had already answered no. That
    is the silent half of the very defect this function exists to end: the row
    is a review by every later reader and the one write-time warning about an
    unbindable approve is simply absent. Normalising HERE keeps the answer with
    its owner instead of asking two call sites to remember."""
    return _approval_tier_advisory(recipient) \
        if clean_kind(kind)[0] == "review" else None


_FAMILY_UNRESOLVED = object()


def _verified_family(recipient):
    """The recipient's family from its VERIFIED LAUNCH METADATA, falling back
    to name parsing — `seat.family_for`'s own contract, asked with the
    authority it needs to answer (task/2480 R1).

    DISPLAY SPELLING IS NOT CAPABILITY IDENTITY. A pi harness names its seat
    `pi-codex` while the credential wall — and the pooled budget — belong to
    family `codex`; `helm seat list`, proxywatch and stalebot all resolve that
    through the roster's self-written runtime row. Only this rung asked
    `family_for(name)` with no runtime at all, so it charged the ceiling to
    every seat SPELLED like codex and admitted every seat that IS codex under
    another name — the two errors that matter, in opposite directions, on the
    same door.

    `roster_runtimes` is the one-read producer for this and never raises."""
    from . import seat, seat_usability
    runtime, verified = seat_usability.roster_runtimes().get(
        str(recipient), (None, False))
    family, err = seat.family_for(str(recipient), runtime, verified)
    return None if err else family


def _validate_recipient_budget(recipient, force, family=_FAMILY_UNRESOLVED):
    """(ok, refusal, warning) — does the recipient's FAMILY still have budget?

    THE THIRD SIBLING RUNG. `_validate_recipient_rostered` asks whether the
    name exists; `_validate_recipient_usable` asks whether that seat can work
    right now; this asks whether the CREDENTIAL POOL underneath it can still
    pay for the work (task/2480). A pooled codex account can be weekly-capped
    while the 5h window a seat is paced on still reads healthy, and without
    this rung the fleet files codex rows into a budget that is gone — which is
    how a pool of ultra creds empties overnight.

    ONE READER FOR THE FLEET, AND NO FAMILY SCOPING. The rung reads the burn
    flag, which answers for every family that has a reader and says GREY for
    the ones that do not. A rung bound to one family's pool can only admit
    every other family unconditionally, which is a silence that reads exactly
    like a measurement of health. A family with no reading is still
    admitted, which is the same law every rung here follows — what changed is
    that "no reading" is now a measured fact rather than the default for six
    families out of seven.

    `family` is the VERIFIED RUNTIME family, threaded in by the door above so
    both rungs decide about the same seat (task/2480 R1). Leave it unset and it
    is resolved here from the same producer; it is never derived from the
    recipient's display spelling alone, which is a name and not a capability.

    REFUSE ONLY WHEN EVERY ACCOUNT HAS DIRECT MEASURED NEGATIVE AUTHORITY. A
    measured cap or hard wall dominates unread sibling windows on the SAME
    account, but an entirely unread account is neither headroom nor evidence
    that the whole family is walled. That partial reading WARNS and proceeds;
    refusing from the unread input itself would be the guard-fires-on-absence
    bug class, one layer down.

    NO NETWORK ON THIS PATH. The gate reads the persisted fold
    (`burnflags.family_flag`); a stale or absent one admits silently. A
    dispatch must never pay five vendor round-trips, and a vendor outage must
    never be able to stall the fleet's routing.
    """
    if force:
        return True, None, None
    try:
        from . import burnflags
        # THE ANSWER THE USABILITY RUNG ALREADY RESOLVED, THREADED THROUGH.
        # `_validate_recipient_usable` hands over the family off the joined row
        # it already holds — one resolution, not two, and no chance of the two
        # rungs disagreeing about which family this recipient is. A caller with
        # no joined row (a direct call, or a seat outside proxywatch's census)
        # resolves it here from the same verified-runtime producer.
        if family is _FAMILY_UNRESOLVED or family is None:
            family = _verified_family(recipient)
        flag = burnflags.family_flag(family)
        if flag is None:
            # NO FRESH FOLD, OR A FAMILY THE FOLD DOES NOT MINT. Both admit
            # silently, which is the behaviour the pooled read had for an
            # absent snapshot and for every family but one — a write door
            # must never be the thing that stops on an unread input.
            return True, None, None
    except Exception as e:                  # noqa: BLE001
        # THE GUARD FAILING IS NOT THE POOL FAILING — same law as the
        # usability rung: a broken reader must not lose a dispatch, and the
        # caller is told so it cannot masquerade as a clean admission.
        return True, None, ("recipient family budget UNKNOWN — the check "
                            "itself failed (%s: %s); this dispatch was "
                            "admitted unverified" % (e.__class__.__name__, e))
    axes = flag.get("axes") or {}
    cause = flag.get("cause") or "no cause recorded"
    if axes.get("money") == burnflags.RED:
        until = flag.get("expires_at")
        when = (" until %s" % burnflags._when(until, time.time())) if until \
            else ""
        refusal = ("recipient %r is UNUSABLE right now: %s is walled on "
                   "MONEY%s — %s. Filing work here spends a budget that is "
                   "gone. Wait for the reset, or pass force=True to file it "
                   "anyway." % (recipient, family, when, cause))
        return False, refusal, None
    notes = []
    if flag.get("colour") == burnflags.ORANGE:
        notes.append("%s: %s is ORANGE — %s; %s"
                     % (recipient, family, cause,
                        burnflags.BEHAVIOUR[burnflags.ORANGE]["say"]))
    # AN UNREAD ACCOUNT WARNS WHETHER OR NOT A SIBLING IS OVER (task/2480 R6).
    # This help text and docs/VERBS both promise that any unread account
    # proceeds AND warns, and the promise was kept only through the `mixed`
    # branch — which needs some OTHER account to be over the ceiling. A pass
    # where every reachable account answered 401 therefore admitted in total
    # silence, the one shape that is indistinguishable from a healthy pool.
    # The fold carries the same fact in `coverage`, so the promise survives
    # the swap rather than riding a branch that no longer exists.
    coverage = flag.get("coverage") or {}
    if coverage.get("unread"):
        notes.append("%s: INCOMPLETE reading — %d of %d %s account(s) "
                     "could not be read (%s); this dispatch was admitted on "
                     "a partial reading"
                     % (recipient, coverage["unread"],
                        coverage.get("total") or 0, family,
                        coverage.get("unread_why") or "no reason recorded"))
    # BOTH NOTES OR NEITHER. An unread account beside an ORANGE colour is two
    # different facts about the same send, and returning on the first would
    # drop whichever one happened to be tested second.
    return True, None, ("  ".join(notes) if notes else None)


def _project_light_rung(path, kind, new_work):
    """(ok, refusal, note) — does the PROJECT this row's work lives in allow it?

    THE THIRD QUESTION AT THIS DOOR, AND THE ONE THAT OUTRANKS THE OTHER TWO.
    The seat rung asks whether the recipient can answer and the budget rung
    whether its family can pay; both are about SUPPLY. This asks whether the
    owner wants the work started at all, and a green family buys nothing in a
    project he has stopped: capacity is never permission.

    `registry.admits` is the whole decision — the same one `helm work claim`
    asks — so the two doors cannot grade one colour two ways. What this rung
    adds is only the reading of a ROW as new work or a continuation: a BUILD
    that roots a fresh chain starts something, while a build continuing a chain
    and every REVIEW finish something already in flight. Orange refuses the
    first and admits the rest; red refuses all of it.

    IT DOES NOT YIELD TO `force`, unlike its two siblings, and that is the
    subject and not an oversight. `force` on this door means "I know the
    recipient is unreachable, file it anyway" — a statement about a seat. The
    way past a project's light is to change the light, which puts a name and a
    reason on the card the owner reads.
    """
    from . import registry
    return registry.admits(path, new_work=bool(new_work) and kind == "build")


def _validate_recipient_usable(recipient, force):
    """(ok, refusal, warning) — can the recipient TAKE this right now?

    TWO RUNGS BEHIND ONE DOOR, and the door is the whole point (task/2480).
    The SEAT rung asks whether this seat can answer; the BUDGET rung asks
    whether its family's credential pool can still PAY for the answer. Both
    are the same question about the same instant, both must fire wherever a
    dispatch names a recipient, and `send` alone reaches this door from THREE
    branches — bolting a second gate beside each of them is precisely the
    shape that guarantees one branch never gets it. A refusal from either rung
    is a refusal; the warnings compose.
    """
    joined = None if force else _recipient_join(recipient)
    ok, refusal, warning = _recipient_seat_rung(recipient, force, joined=joined)
    if not ok:
        return ok, refusal + " " + review_fallback_text(), warning
    # THE FAMILY IS RESOLVED ONCE, BY THE READ ABOVE. proxywatch stamps each
    # health row with `family_for(name, runtime, runtime_verified)`, so the
    # joined row this rung just used already carries the VERIFIED family — and
    # handing it over is what stops the budget rung re-deriving a different
    # answer from the display spelling (task/2480 R1).
    budget_ok, budget_refusal, budget_warning = _validate_recipient_budget(
        recipient, force, family=(joined[2] or {}).get("family")
        if joined else _FAMILY_UNRESOLVED)
    if not budget_ok:
        return False, budget_refusal + " " + review_fallback_text(), None
    notes = [n for n in (warning, budget_warning) if n]
    return True, None, "; ".join(notes) or None


#: THE SEAT THAT KEEPS REVIEW ALIVE WHEN EVERY PAID FAMILY IS WALLED (store
#: premise openrouter-seat-is-the-always-available-free-reviewer-lane).
REVIEW_FALLBACK_SEAT = "openrouter"
#: The model a Fable one-agent Workflow names in its per-agent opts.model:
#: the launch ALIAS, never a constructed id (store reference canon-model-ids).
REVIEW_FALLBACK_MODEL = "fable"


def review_fallback_text(row=None, tip=None):
    """THE FALLBACK FOR A MISSING REVIEWER, spelled once (task/2948).

    OWNER RULE: "no cross-family reviewer" must never read as a blocker
    anywhere in helm, and a review is CROSS-FAMILY or, when only a different
    model is needed, FABLE (store premise
    review-is-cross-family-or-fable-never-sonnet, which narrows the earlier
    same-family clause). So every surface that tells a seat its reviewer is
    unusable, walled or absent ends on this ladder, with the command for each
    rung, instead of on "repair it" alone:

      1  any other-family seat, the openrouter seat always among them — it is
         the free lane that exists so this rung is never empty;
      2  a Fable one-agent Workflow;
      3  on a Fable limit: Fable through ANOTHER credential or seat — a limit
         on the running seat's credential is not a wall and never a reason to
         step down a model — then the openrouter seat.

    Sonnet and Haiku never review anything, and the text says so, because a
    reader who meets "a different model" with no floor under it reaches for
    the nearest one.

    ONE OWNER, because four surfaces say it — the dispatch door, the owed
    digest, `helm owed` and `helm reviewers` — and a ladder copied four times
    is four ladders the day one rung changes. `row` and `tip` fill the
    placeholders when the caller has them.

    The Workflow tool and never the Agent tool: the Agent tool ignores its
    model flag and runs the seat's own model (store heuristic
    fable-via-workflow-not-agent-tool), so an Agent "Fable" read is the
    author's model reading its own work."""
    rid = str((row or {}).get("id") or "")[:12] or "<row>"
    lane = str((row or {}).get("lane") or "") or "<lane>"
    ref = str(tip or (row or {}).get("reviewed_tip")
              or (row or {}).get("tip") or "") or "<tip>"
    send = ("`helm dispatch send %s %s --ref %s --kind review --supersedes %s`"
            % (REVIEW_FALLBACK_SEAT, lane, ref, rid))
    return (
        "NO REVIEWER IS NEVER A BLOCKER — a review is CROSS-FAMILY, or FABLE "
        "when only a different model is needed; Sonnet and Haiku never review "
        "anything. Fallback, in order: (1) any other-family seat, and the %s "
        "seat is always one — the free lane: %s (if it reads DEAF with a live "
        "pane, add --force, then `helm seat resume-turn --nudge --seat %s` "
        "types the wake into that pane); (2) a Fable one-agent Workflow: the "
        "Workflow tool, one agent, opts.model %s, given the review brief "
        "(never the Agent tool, which runs your own model); (3) on \"You have "
        "reached your Fable limit\": that limit is this credential's, not a "
        "wall — get Fable through another credential or seat, and never step "
        "down a model; failing that, the %s seat. A Workflow read goes on the "
        "row as an ADVISORY read — the row stays owed until helm can verify "
        "the run: `helm dispatch verdict %s %s --concur|--fix --measured "
        "--reviewer-model <model> --reviewer-run <run id> [--author-model "
        "<yours>] <evidence>`."
        % (REVIEW_FALLBACK_SEAT, send, REVIEW_FALLBACK_SEAT,
           REVIEW_FALLBACK_MODEL, REVIEW_FALLBACK_SEAT, rid, ref))


def _recipient_join(recipient):
    """(state, why, row, err) — THE one usability read a dispatch pays for.

    Split out of `_recipient_seat_rung` so the seat rung and the budget rung
    share ONE `seat_verdict` call: the row it returns carries both the
    usability verdict and the VERIFIED runtime family, and a second call would
    be a second proxywatch read on every dispatch write.

    Never raises: `err` carries the exception instead, because a broken
    usability reader must not lose a dispatch (see the rung below)."""
    try:
        from . import seat_usability
        # need_holding=False: no rung of the verdict reads the count, and
        # folding the ledger here added a SECOND snapshot read to every
        # dispatch write (pinned by test_the_duplicate_check_adds_no_
        # snapshot_read, which measured it at 2 where the contract is 1).
        state, why, row = seat_usability.seat_verdict(
            str(recipient), need_holding=False)
    except Exception as e:                  # noqa: BLE001
        return None, None, None, e
    return state, why, row, None


def _census_interval_min():
    """The beacons census cadence in minutes, read from its owner so the
    advisory's bound moves with the timer."""
    try:
        from . import beacons
        return max(1, int(beacons.INTERVAL_S) // 60)
    except Exception:                       # noqa: BLE001 — prose only
        return 5


def _recipient_seat_rung(recipient, force, joined=None):
    """(ok, refusal, warning) — can this SEAT work right now?

    THE SIBLING RUNG TO `_validate_recipient_rostered`, and the reason it is
    separate: that one asks "does this name exist on the roster", a fact a
    seat keeps forever. This asks "can it work right now", which changes
    hourly. Measured on the live fleet 2026-08-11 — all four while the roster
    answered JOINED and every proxy answered UP — codex 42 hours dark on a
    STARVED turn, another codex seat with its PANE GONE, kimi AUTH-UNAVAILABLE, gemini
    walled behind MALFORMED200. Work routed to any of those lands in a hole,
    and the sender finds out when the deadline passes.

    ONE AUTHORITY, NOT A SECOND MATCHER: `seat_usability.seat_verdict` is the
    join over proxywatch's cached readers, the seat register and this very
    ledger — scoped to this one recipient, so it costs one seat's measurement
    and one ledger fold, and never a probe (`proxywatch.health`'s
    include_probe=False).

    REFUSE ONLY ON A MEASURED CONTRADICTION. `can_take_work is False` is a
    positive measurement that this seat cannot work. UNKNOWN is helm saying it
    could not tell, and refusing on THAT would brick every box where
    proxywatch has never run, and would repeat the bug class where a guard
    firing on ABSENCE downgrades every case whose evidence merely aged out. So
    UNKNOWN and DEGRADED both PROCEED and both SAY SO on the row: an unknown
    is announced, never silently promoted to healthy.

    force=True proceeds unconditionally, exactly as the roster gate does — an
    operator addressing a seat they are about to repair is a real case, and a
    guard with no door is a guard people route around.
    """
    if force:
        return True, None, None
    state, why, row, err = joined if joined else _recipient_join(recipient)
    if err is not None:
        # THE GUARD FAILING IS NOT THE SEAT FAILING. A dispatch must not be
        # lost because a usability reader raised — but the caller is TOLD, so
        # a broken guard cannot masquerade as a clean admission.
        return True, None, ("recipient usability UNKNOWN — the check itself "
                            "failed (%s: %s); this dispatch was admitted "
                            "unverified" % (err.__class__.__name__, err))
    from . import seat_usability
    can = (row or {}).get("can_take_work")
    # THE ROUTING POLICY LIVES HERE, NOT IN THE JOIN. The join answers "can
    # this seat work" for everyone; what a DISPATCH should do about each answer
    # is this caller's decision, and the two are not the same question.
    #
    # A GONE PANE IS A DELAY; A WALL OR A DEAD TURN LOOP IS A HOLE. The
    # dispatch ledger is DURABLE and the recipient's beacon replays open rows
    # on relaunch — so filing work for a seat whose pane is down is the normal,
    # working case (helm's fleet dispatches across relaunches constantly),
    # refusing it would break routing to every seat between panes, and SAYING
    # so on every such row is noise nobody would keep reading. A seat that is
    # LIVE and cannot work is the opposite: it receives the DM, consumes the
    # obligation, and does nothing with it until a human notices — which is the
    # 42-hour case this lane exists for. So the gate speaks for exactly that
    # case, and is silent for the ordinary between-panes one.
    if can is False and (row or {}).get("pane") is False:
        # A GONE PANE IS A DELAY. A WALL STACKED UNDER IT IS STILL A HOLE, AND
        # THIS BRANCH SAID NOTHING ABOUT EITHER. A seat in exactly that shape
        # — pane GONE with a dark family under it — took review dispatches with
        # no advisory at all. The argument above is sound for a pane-ONLY outage
        # (the ledger is durable and the beacon replays on relaunch) and it is
        # false when the vendor is also dark: relaunching that pane produces a
        # seat that still cannot answer, and the sender would learn nothing
        # until the deadline passed.
        #
        # STILL ADMITTED, and deliberately: the wall clears on its own on the
        # next green proxywatch pass with no human step, so the row is filed and
        # waits, exactly as it does across an ordinary relaunch. What changes is
        # that the sender is TOLD, which is the whole difference between a delay
        # and a silent hole. NO EXTRA READ: the wall fields are already on the
        # joined row this function holds.
        if (row or {}).get("upstream_dark"):
            from . import seat_usability
            return True, None, (
                "recipient %r has NO LIVE PANE and its family is ALSO not "
                "answering usably: %s — filed, because the ledger is durable "
                "and the beacon replays on relaunch, and because that state "
                "clears itself on the next green proxywatch pass. But a "
                "relaunch alone will NOT make this seat able to work the row."
                % (recipient, seat_usability.availability_text(
                    {"state": seat_usability.UNAVAILABLE,
                     "family": (row or {}).get("family"),
                     "reason": (row or {}).get("upstream"),
                     "since": (row or {}).get("upstream_since")})))
        return True, None, None
    if seat_usability.deaf_only(row):
        # A DEAF PANE IS A DELAY TOO (task/3055), and the argument is the GONE
        # pane's above, word for word: the ledger is DURABLE, and the row is
        # delivered when the recipient's beacon re-arms. What made a live-pane
        # refusal right — a seat that takes the obligation and never works it
        # — is not this seat: it cannot take anything until it re-arms, and
        # then it can work it. Refusing here only made the sender re-type the
        # row later (measured: a review booking refused for a seat whose
        # waiter was 31 seconds from re-arming after its 30-minute lease).
        # And the filed row is what makes the seat OWE, which is the input the
        # beacons census's re-arm nudge fires on. A second refusal rung — a
        # wall, a dead turn loop — keeps the refusal below; `deaf_only` is
        # False for it.
        return True, None, (
            "recipient %r is DEAF (%s): filed, because the ledger is durable "
            "and the row is delivered when its beacon re-arms. This row makes "
            "it owe work, so the beacons census asks its pane to re-arm on "
            "its next pass (at most %d minutes) when that pane declares the "
            "seat and the seat is of the project the census runs for; `helm "
            "seat resume-turn --nudge --seat %s` types the same wake now." % (recipient, (row or {}).get("reachable_why")
                           or "no live beacon", _census_interval_min(),
                           (row or {}).get("seat") or recipient))
    if can is False:
        return False, (
            "recipient %r is UNUSABLE right now: %s. Its pane is LIVE, so it "
            "will take this obligation and not work it — repair it (`helm seat "
            "list` shows this same verdict) or pass force=True to file it "
            "anyway." % (recipient, why or "no reason recorded")), None
    if state == seat_usability.UNKNOWN:
        return True, None, ("recipient %r usability UNKNOWN: %s — admitted, "
                            "but nothing has confirmed this seat can answer"
                            % (recipient, why or "no reason recorded"))
    if state == seat_usability.DEGRADED:
        return True, None, ("recipient %r is DEGRADED: %s" % (recipient, why))
    return True, None, None


def _inherited_origin(supersedes, acting):
    """(sender, body, acted_by, advisory, err) — the ORIGIN of the obligation
    named by `supersedes`, read off the ledger, for a row that MOVES it.

    THE SECURITY PROPERTY, and it is the whole design: **a sender is never
    ACCEPTED, only INHERITED**. There is no `sender=` argument anywhere on this
    path. The only name a caller can put in that field is one ALREADY DURABLY
    RECORDED as the author of a row that exists in this ledger, and only by
    superseding that exact row. A plain passthrough would have been strictly
    worse than the bug it cures: `helm dispatch rebind` is reachable by any seat
    with a live pane, so `sender="opus-integrator"` would have let any of them
    mint work under any other seat's name, and `landreq` projects that field
    straight into `author`. Under inheritance the worst a hostile caller
    achieves is to re-state, on a continuation of one specific row, the author
    that row already had — the identity function on that chain, not an
    escalation.

    THE ACTING SEAT IS STILL DERIVED AND STILL REFUSES. `_acting_author` runs
    first and unchanged at the caller, so a DISPUTED identity or the bare family
    floor cannot move an obligation any more than it can author one; its answer
    arrives here as `acting` and is recorded as `acted_by` so the trail keeps
    BOTH names. Preserving authorship hides nothing — it separates who wrote the
    work from who moved it, where the old code had only one slot and put the
    mover in it.

    NO PARENT IS A REFUSAL, not a fallback. "Preserve the author" with no row to
    read one off is a caller ASSERTING a name, which is precisely what this
    function exists to make impossible.

    A PARENT THAT RECORDS NO SENDER cannot have its authorship preserved — every
    `dispatch add` row written before 2026-07-28 carries sender=None. Refusing
    would break `rebind` on exactly the oldest rows, which is the population most
    likely to need moving, so the acting seat authors it and the returned
    ADVISORY says so out loud. Silence there would be the original defect
    (authorship rewritten with nobody told) wearing a new field's clothes."""
    parent_id = str(supersedes or "").strip()
    if not parent_id:
        return None, None, None, None, (
            "refusing to preserve authorship with NO superseded row: a "
            "preserved author is READ off a parent, never supplied, and there "
            "is no parent here. This path moves an existing obligation; new "
            "work authors itself.")
    current, unavailable = snapshot()
    if unavailable:
        return None, None, None, None, (
            "dispatch ledger unavailable (%s) — the superseded row's author is "
            "UNKNOWN and the move is NOT recorded; an unreadable parent never "
            "silently becomes the mover's own work" % unavailable)
    parent, err = _resolve_row(current, parent_id)
    if err:
        return None, None, None, None, "--supersedes " + err
    # THE REASON A BODY IS ABSENT IS ITSELF INHERITED. `body_of` answers a
    # TRI-STATE — text, KNOWN-EMPTY (this row was minted by a path that carries
    # no brief), or UNRECORDED (this row predates body storage, so a brief may
    # have existed and is not recoverable here) — and this discarded the
    # second half, collapsing it to "no body". The child then reads as a row
    # that never had a brief, when the truth may be that one existed and was
    # lost. The rebound recipient asks the wrong person for the wrong thing.
    body, body_absent = body_of(parent)
    lost_note = ("; its brief PREDATES body storage, so the text is not "
                 "recoverable from this ledger — ask @%s for it"
                 % str(parent.get("sender") or "the author")
                 if body is None and body_absent == BODY_UNRECORDED else "")
    inherited = parent.get("sender")
    if inherited is None:
        return acting, body, None, (
            "the superseded row %s records NO sender, so its authorship could "
            "not be preserved; this row is authored by @%s, the seat that moved "
            "it%s" % (parent["id"][:12], acting, lost_note)), None
    if not _TOKEN.fullmatch(str(inherited)):
        # A hand-edited or corrupt row is not an author. Refuse rather than
        # write a malformed stamp `_base` would refuse anyway with a sentence
        # that names the wrong culprit.
        return None, None, None, None, (
            "refusing to preserve authorship from %s: its recorded sender is "
            "not a seat token, so there is no name to carry"
            % parent["id"][:12])
    inherited = str(inherited)
    # SAME SEAT, NOTHING TO DISCLOSE. `acted_by` means "this sender was
    # inherited"; stamping it when the mover IS the author would make the field
    # fire on the case it has nothing to say about.
    # THE NOTE RIDES THE SUCCESS PATH TOO. My first wiring put the lost-brief
    # advisory only on the no-sender branch — the negative half — so an
    # ordinary move of a pre-storage row carried the body absence with no word
    # about WHY, which is the whole distinction. Wiring a new evidence source
    # into one branch and calling it done is the defect I keep repeating; the
    # cure is to follow the value to EVERY reader, not to the first one.
    return (inherited, body, (None if inherited == acting else acting),
            (lost_note.lstrip("; ") if lost_note else None), None)


# AUTHORSHIP-PRESERVING WRITES ARE MINTED, NEVER REQUESTED. A bare
# `_preserve_origin=True` on the generic `add` let ANY library caller mint a
# child row carrying another seat's `sender` with no rebind evidence behind it
# — an authorship forgery reachable by keyword argument. The underscore was
# privacy by CONVENTION, and convention is not a boundary.
#
# This sentinel cannot be constructed from outside the module: a caller can
# pass True, and True is now REFUSED. Same shape as takeover's `_REASSIGN_MINT`
# — the capability is the object, not the value.
_MOVE_MINT = object()


def add(recipient, lane, ref=None, note=None, deadline_s=None,
        repo=None, kind=None, notify=True, _reason=False,
        new_work=False, supersedes=None, force=False,
        _ref_branch=_INFER_REF_BRANCH, posture_na=None,
        read_only_because=None, _preserve_origin=False):
    """Persist a dispatch and post a public @mention to main so the
    reviewer's beacon picks up the obligation — the counterpart to send()
    (which also DMs the reviewer). When notify=False, the dispatch is
    persisted without any mention; the caller owns the hand-off. A failed
    mention does not block the dispatch.

    RECORDS THE SENDER, like send() always has. This path did not, so every
    `dispatch add` row landed with sender=None and the ledger could not say who
    owed it. The stop-guard then billed the delivery-confirmation nag to
    WHOEVER STOPPED NEXT: in one measured case, two rows minted by one seat
    (63cf625c, 9e237a33) nagged the integrator ten minutes apart to confirm
    hand-offs it never made. That is worse than billing nobody — it makes an
    uninvolved seat feel responsible and, in a busy fleet, invites the duplicate
    resend the same guard exists to prevent.

    A row that cannot name its sender cannot be chased, cancelled, or credited.

    Same add-vs-send asymmetry as the notification gap (fea7232, "dispatch:
    `add` mints an obligation and tells NOBODY — say so out loud"): send() was
    complete and add() was the quiet path nobody re-derived.

    SUPERSEDED (owner call, 2026-08-02): this path used to fail OPEN — a
    hostile HELM_CHAT_NAME or no identity at all still wrote the row, as
    sender=None or the family floor, on the theory that "an unattributed row
    is a real defect, but losing the obligation entirely is worse". Measured
    consequence: 6 stalled rows owned by 'claude' — a name three seats share —
    permanently stop-guard-nagging every claude seat, un-disownable.
    Un-disownable shared-name rows are worse than a lost add: identityless or
    disputed callers are REFUSED with the repair (_acting_author).

    `_preserve_origin=_MOVE_MINT` MAKES THIS A MOVE INSTEAD OF AN AUTHORSHIP
    (the MINT, never the value `True`, which is refused). The
    row's sender and its stored brief are INHERITED from the row named by
    `supersedes`, and the acting seat is recorded as `acted_by`. Private, and
    passed today by `rebind` alone, because it is the difference between "I am
    continuing this work" (ordinary `--supersedes`, which authors itself) and "I
    am moving someone else's obligation to a new seat".

    IT CURED A SILENT RE-AUTHORING: `landreq` projects `"author": row["sender"]`,
    so every rebind rewrote the land request's author to whoever ran the rebind —
    and the verdict nudge (`_nudge(sender, "VERDICT ... IS BACK WITH YOU")`) woke
    that seat instead of the person who wrote the code. The security property is
    in `_inherited_origin`: a sender is never accepted from a caller, only read
    off the parent row.

    THE CONSEQUENCE THAT MOVES THE OTHER WAY, stated rather than discovered
    later: `sender` is also the field the stop-guard bills for chasing a row
    (`seats_stop_signals._beacon_obligation`, `stop_candidate`). After a move the
    ORIGINAL author is the one offered the row again, not the mover. That is the
    better default — they wrote the brief and can re-send it — but a reader that
    wants the mover instead now has `acted_by` to read, and does not have to
    guess."""
    # THE POSTURE GUARD AT THE INVARIANT (helm/posture.py, same as send):
    # add() has no prose body, so its note is the body it can carry a seam
    # strategy in; a hit owes the three questions or a recorded --posture-na.
    from . import posture
    refused = posture.check("helm dispatch add", str(note or ""),
                            posture_na=posture_na)
    if refused:
        return (None, refused) if _reason else None
    # THE READ-ONLY GUARD, same door and same law: `add` carries no prose body,
    # so its note is where a review row can tell its reader not to edit.
    refused = check_read_only("helm dispatch add", str(note or ""),
                              kind=kind, because=read_only_because)
    if refused:
        return (None, refused) if _reason else None
    acting, err = _acting_author()
    if err:
        return (None, err) if _reason else None
    # THE ACTING SEAT AUTHORS, UNLESS THIS IS A MOVE. `_acting_author` has
    # already refused a disputed identity and the family floor above, so the
    # move path cannot be reached by a caller that could not author either.
    sender, message_body, acted_by, origin_note = acting, None, None, None
    # BOUND ON EVERY PATH, not just the move one. The inherited-custody read
    # below lives inside the MOVE branch, and using it unconditionally at the
    # `_base` call would NameError on every ordinary dispatch — the same
    # extracted-without-its-scope defect that bit `_row_is_live` earlier
    # tonight. An ordinary authorship inherits no custody: the author holds it.
    inherited_custodian = None
    # THE REFERENCE TRAVELS WITH THE OBLIGATION, for the same reason the body
    # and the sender do — and it is the half that makes "the brief travels
    # WHOLE" true of a rebind. `_inherited_origin` hands back the parent's
    # BOUNDED copy and the move re-caps it below; without the reference the
    # rebound recipient inherits the cut, which is precisely the seat least
    # able to go ask the sender for the tail. The file is content-addressed, so
    # carrying the digest costs nothing and copies no bytes.
    inherited_brief_ref = inherited_brief_bytes = None
    if _preserve_origin is not False and _preserve_origin is not _MOVE_MINT:
        # A TRUTHY VALUE THAT IS NOT THE MINT IS A FORGERY ATTEMPT, and it is
        # refused rather than silently downgraded to an ordinary authorship:
        # the caller asked to write under another seat's name, and answering
        # that by quietly writing under its own would hide the attempt.
        err = ("_preserve_origin is minted inside dispatches for the MOVE "
               "path only; it cannot be requested by value")
        return (None, err) if _reason else None
    if _preserve_origin is _MOVE_MINT:
        sender, message_body, acted_by, origin_note, err = \
            _inherited_origin(supersedes, acting)
        if err:
            return (None, err) if _reason else None
        # READ THE PARENT'S CUSTODIAN HERE rather than widening
        # `_inherited_origin`'s tuple. Twice tonight I widened a return
        # contract and broke a caller that reached past the helper to unpack
        # it; the parent row is already resolvable at this point, so the value
        # can be fetched without changing a shape anything else depends on.
        try:
            _parent = (snapshot()[0] or {}).get(str(supersedes or ""))
            inherited_custodian = custodian_of(_parent) if _parent else None
            # SAME READ, SAME ROW. The parent is already resolved here, so the
            # brief reference is taken from it rather than widening
            # `_inherited_origin`'s tuple a second time.
            if _parent and _parent.get("brief_ref") is not None:
                inherited_brief_ref = _parent.get("brief_ref")
                inherited_brief_bytes = _parent.get("brief_bytes")
        except Exception:                    # noqa: BLE001 — a custodian we
            inherited_custodian = None       # cannot read is not a custody move
            inherited_brief_ref = inherited_brief_bytes = None
        # THE INHERITED BODY GETS THE CURRENT CAPS. A move COPIED the parent's
        # stored body straight through to `_base`, while the ordinary send path
        # runs it through `_store_body` — so a row written under a larger cap,
        # or one carried across a cap that was later LOWERED, propagated
        # unbounded through every rebind, and the bound this field advertises
        # held only for rows nobody had moved. `_store_body` returns a body
        # that already fits UNCHANGED, so this costs nothing in the ordinary
        # case and only bites the one it exists for.
        if message_body:
            message_body = _store_body(message_body, has_ref=bool(inherited_brief_ref))
    recipient, err = _recipient_operand(recipient)
    if err:
        return (None, err) if _reason else None
    ok, why = _validate_recipient_rostered(recipient, force)
    if not ok:
        return (None, why) if _reason else None
    # THE USABILITY GATE — rostered says the name exists, this says the seat
    # can answer today. Refusal is a measured UNUSABLE only; a DEGRADED or
    # UNKNOWN recipient rides through carrying its note.
    ok, why, usability_note = _validate_recipient_usable(recipient, force)
    if not ok:
        return (None, why) if _reason else None
    # THE TIER ADVISORY BELONGS AT THIS DOOR, NOT AT A VERB. It answers whether
    # this recipient's APPROVE COULD BIND, so it is meaningful for exactly the
    # review kind and for every WRITER of one — send, rebind, and any library
    # caller. Sited in the CLI on the send branch, it was invisible to a
    # REBOUND review (the reviewer a re-route just installed is precisely the
    # one nobody vouched for) and to every caller that is not a terminal.
    # ADVISORY, NEVER A GATE: an unbindable verdict is a fact the sender must
    # know at write time, and refusing here would block legitimate reads by
    # seats whose approve was never the point.
    tier_note = _tier_note(recipient, kind)
    row, err = _base(recipient, lane, ref, note, deadline_s, repo, kind=kind,
                     sender=sender, new_work=new_work, supersedes=supersedes,
                     _ref_branch=_ref_branch, message_body=message_body,
                     acted_by=acted_by, custodian=inherited_custodian,
                     brief_ref=inherited_brief_ref,
                     brief_bytes=inherited_brief_bytes)
    if not row:
        return (None, err) if _reason else None
    if str(posture_na or "").strip():
        row["posture_na"] = str(posture_na).strip()   # the recorded escape
    _stamp_read_only(row, read_only_because)
    out, why, existed = _append_dispatch(row, force=force)
    if not out:
        return (None, why) if _reason else None
    out = {k: v for k, v in out.items() if k != _WRITTEN_ELSEWHERE}
    if not existed:
        _queue_findings_pass(out)
    if notify:
        mention_id = _notify_public(out, note or lane or ref)
        # THE MENTION IS THIS PATH'S DELIVERY, so it is this path's evidence.
        # send() marks delivered with the DM row seats.dm() returns; add()
        # reaches its recipient through the mention and nothing else, so the
        # mention's id is the exact counterpart. Without this the row can never
        # leave needs-confirmation and the nag chases an unreachable state.
        #
        # FAIL-OPEN, in BOTH directions and for different reasons. No mention
        # (post failed) => stay needs-confirmation: delivery genuinely IS
        # unknown then, _record_notify_failed has already left the durable
        # trail, and claiming observed here would launder a failure into a
        # green. A _mark_delivered that errors (lock, storage) => keep the
        # unmarked row: losing the obligation is far worse than under-reporting
        # its delivery, which is the same trade add() makes for a failed
        # mention one line up.
        if mention_id:
            # The delivery re-read is persisted state and can never contain the
            # ephemeral write advisory the locked writer proved — re-carry it
            # across the swap, exactly as send() does two paths over.
            warnings = out.get(_WRITE_WARNINGS, ())
            observed, _err = _mark_delivered(out["id"], mention_id)
            if observed:
                out, _, _ = _with_write_warnings((observed, None, None),
                                                 warnings)
    # AFTER THE NOTIFY LEG, NOT BEFORE IT — the placement is the fix. These
    # advisories used to be attached above, and the delivery re-read a few lines
    # up REPLACES `out` while re-carrying only _WRITE_WARNINGS: with notify=True
    # (the CLI's default, and rebind's) every recipient-usability note was built,
    # attached, and then silently dropped before the caller saw it. The origin
    # advisory rides the same channel — "authorship could not be preserved" is a
    # fact about THIS WRITE, not durable row state, and the caller is the only
    # one who can act on it — so it would have been lost on exactly the path
    # that needs it. Attaching last cannot be undone by a later swap.
    # THE LIGHT'S NOTE RIDES THE SAME CHANNEL: a yellow project admits the row
    # and says its reason, and the row carries the repository it resolved.
    light_note = _project_light_rung(row.get("repo_root") or repo, kind,
                                     new_work)[2]
    for advisory in (usability_note, tier_note, origin_note, light_note):
        if advisory:
            out[_ADMISSION_NOTES] = list(out.get(_ADMISSION_NOTES, ())) \
                + [advisory]
    return (out, None) if _reason else out


def _mark_delivered(rid, delivery_ref):
    """Record delivery evidence for a dispatch.  Idempotent for the same ref;
    a DIFFERENT ref on an already-delivered row appends a new delivered event
    with a warning -- the latest delivered event wins in replay.  A closed row
    (verdict, cancelled) is refused silently.

    Returns (row, err).  On a delivery-ref update the returned row carries a
    _WRITE_WARNINGS entry containing the change note.

    Uses CLOSED_STATES directly rather than _open() so the guard stays correct
    if/when HOLD introduces a non-terminal suspended state."""
    ref, err = _clean(delivery_ref, "delivery ref", 256)
    if err:
        return None, err
    path = ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "delivery observed but ledger update failed"
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("status") in CLOSED_STATES:
            return row, None            # closed (verdict or cancelled) -- never
                                        # append a delivered event onto a terminal
                                        # row
        existing = row.get("delivery_ref")
        if row.get("delivery") == "observed":
            if existing == ref:
                return row, None        # idempotent -- same ref, no new event
            # Different ref: APPEND a new delivered event.  The latest wins in
            # replay; _prior_delivery_ref is audit metadata, not validated.
            event = {"v": 3, "event": "delivered", "seq": row["seq"] + 1,
                     "id": row["id"], "ts": pk.now_ts(), "delivery_ref": ref,
                     "_prior_delivery_ref": existing}
            warning = ("delivery_ref updated: was %r, now %r"
                       % (str(existing)[:24], ref[:24]))
        else:
            event = {"v": 3, "event": "delivered", "seq": row["seq"] + 1,
                     "id": row["id"], "ts": pk.now_ts(), "delivery_ref": ref}
            warning = None
        if not txn.append(event):
            return None, "delivery observed but ledger update failed"
        out = dict(row)
        out.update(delivery="observed", delivery_ref=ref, seq=event["seq"])
        if warning:
            out[_WRITE_WARNINGS] = list(out.get(_WRITE_WARNINGS, ())) + [warning]
        return out, None
    return _ledger_write(attempt, path)


mark_delivered = _mark_delivered    # public alias -- the verb for updating
                                    # delivery_ref after the initial send


def _notify_public(row, context):
    """Post a public @mention to main so the reviewer's beacon picks up the
    obligation — the notification leg of the fleet-stall fix (2026-07-26).
    Best-effort: a failed post must not block the dispatch itself, but the
    FAILURE must leave a DURABLE trail so the gap is not silently recreated.

    RETURNS THE MENTION'S CHAT ID, not a bool, because that id IS the delivery
    evidence and this function was the seam that destroyed it. `add()` reaches
    its recipient through this mention and nothing else — its own docstring
    says so — but it could never mark the row delivered, since the only thing
    it got back was True. So every add()-minted row sat at needs-confirmation
    permanently, and the delivery-confirmation nag chased a state no code path
    could reach: four seats each spent a check-in on one such row on 2026-08-02,
    and the same nag misbilled the integrator twice.

    Truthiness is unchanged (a non-empty id where True stood, None where False
    did), so every caller that only asked "did it post" keeps working."""
    from . import chat
    notice = "@%s %s: %s" % (_recipient_label(row), row["id"][:12], context)
    mention_id = None
    why = ""
    try:
        # POSTED BY THE SEAT THAT WROTE THE ROW, which after a MOVE is not the
        # row's author. `sender` is now inherited on a rebound row, so reading
        # it here would put this notice in the original author's mouth in the
        # public room — the same "words in someone's mouth" objection that keeps
        # `rebind` from re-sending the DM, arriving through the notify leg.
        # `acted_by` is present ONLY on a move, so ordinary rows are unchanged.
        posted = chat.post(notice, room="main",
                           who=row.get("acted_by") or row.get("sender"),
                           sign=False)
        mention_id = (posted or {}).get("id") or None
    except Exception as exc:
        why = "%s: %s" % (exc.__class__.__name__, exc)
    if not mention_id:
        try:
            _record_notify_failed(row["id"], why or "mention post returned nothing")
        except Exception:
            pass  # failed to record the failure — worse, but the dispatch stands
    return mention_id


def _record_notify_failed(rid, reason):
    """A DURABLE event on the dispatch row. The notify-failed event is
    separate from the dispatch itself — a failed notification is not the
    same as a dispatch that was never told.

    A failure-to-record here raises: a durability function that silently
    swallows its own write failure is not durable."""
    path = ledger_path()
    event = {"v": 3, "event": "notify-failed", "id": rid,
             "ts": pk.now_ts(), "reason": reason[:256]}
    if not eventledger.append(path, event):
        raise RuntimeError("failed to record notify-failed for %s" % rid[:12])


def _notify_failed_for(rid):
    """Read back the durable notification-failure marker from the event ledger."""
    try:
        for ev in eventledger.events(ledger_path()) or ():
            if isinstance(ev, dict) and ev.get("id") == rid \
                    and ev.get("event") == "notify-failed":
                return ev
    except Exception:
        pass
    return None


def _reconcile_send(rid, detail):
    """The canonical live result after send()'s DM window. The ledger lock is
    released for the network DM, so by the time we return the row may have been
    TERMINALIZED (a concurrent cancel/verdict) or storage may have gone away.
    This is the ONE owner of the send-path invariant: the live return never
    disagrees with the durable ledger — and NEVER carries a pre-DM row.

    TOTAL over (unavailable?) x (rid present?) x (_open?) — meld-converged:
      1. snapshot unavailable                 -> (None, UNKNOWN)   storage gone
      2. readable, rid ABSENT                  -> (None, UNKNOWN)   obligation
         (a missing/rotated/known-empty ledger reads as ({}, None), so a just-
          persisted row can be absent from a READABLE snapshot — that is UNKNOWN,
          never the stale pre-DM OPEN row; xrev 4th defect)
      3. readable, present, terminal           -> (row, None)      canonical close
      4. readable, present, open/other         -> (row, NEEDS CONFIRMATION+detail)
    Reads via snapshot() — NOT rows(), which discards the `unavailable` flag."""
    current, unavailable = snapshot()
    if unavailable:
        return None, ("dispatch ledger unavailable (%s) — obligation UNKNOWN"
                      % unavailable), False
    row = current.get(str(rid))
    if row is None:
        return None, ("dispatch %s absent from the ledger after delivery — "
                      "obligation UNKNOWN" % rid), False
    if not _not_closed(row):
        return row, None, False
    return row, ("dispatch persisted but delivery is NEEDS CONFIRMATION: %s"
                 % detail), False


def _with_write_warnings(result, warnings, notes=()):
    """Keep write-time advisories visible across ledger reconciliation.

    They remain ephemeral — never replayed as dispatch state — but UNKNOWN must
    not erase an advisory the locked writer already proved. When reconciliation
    cannot return a row, carry the warning on the only remaining user-visible
    channel: the reason string.

    `notes` is the RECIPIENT-ADMISSION channel and rides the same reconciliation
    for the same reason — the row `send` finally returns is not the row the gate
    inspected, so a note attached before the DM is lost by the time the CLI
    prints. It stays a SEPARATE key: _WRITE_WARNINGS carries the
    duplicate-successor contract and four chain tests pin its absence.
    """
    row, why, sent = result
    for key, items, label in ((_WRITE_WARNINGS, warnings, "write warning"),
                              (_ADMISSION_NOTES, notes, "recipient")):
        if not items:
            continue
        if row is not None:
            row = dict(row)
            row[key] = list(items)
        elif why:
            why += "; %s: %s" % (label, "; ".join(items))
    return row, why, sent


# fab mints this subject at exactly ONE site — fab/bin/fab, the `commit-tree`
# that snapshots a DIRTY worktree so the node can gate the tree you are
# standing in. That object is a dangling commit: it moves no branch, it is
# pushed to the mirror as a non-branch ref, and no `git commit` ever runs for
# it, so every commit-time rung in this repo is structurally unable to see its
# content. `test_the_snapshot_marker_matches_the_producer` pins this literal
# against fab's own source so the coupling cannot rot silently.
_SNAPSHOT_SUBJECT = "fab snapshot"


def snapshot_tip_refusal(root, tip):
    """Refuse a reviewed tip that is fab's synthetic snapshot of a dirty tree.

    WHY THE SUBJECT AND NOT CONTAINMENT. The obvious structural rule — refuse a
    tip no branch contains — was measured against this ledger and REFUSES THE
    HEALTHY POPULATION: of 211 open and held rows, 57 name a tip no branch
    contains, and they are ordinary work. Uncontained is the RESTING STATE of a
    row whose lane landed, because the fold rewrites the sha, the lane branch is
    deleted, and a rebase moves the branch off the tip the row still names. Over
    the whole 3722-row ledger the same read gives 944 uncontained against 4 tips
    whose subject is a snapshot — and those 4 are every known instance. The
    narrow marker is the discriminator; the structural-looking rule is a trap.

    AND AN UNREADABLE TIP IS NOT A REFUSAL. 26 of those 211 rows name an object
    this checkout cannot read at all, which is a different question (another
    repo, a pruned object, a fork). Silence there is correct: this door answers
    "is this tip a dirty-tree snapshot", and it may only answer when it knows.
    """
    if not isinstance(root, str) or not root or not os.path.isdir(root):
        return None
    # RESOLVE BEFORE JUDGING, for any caller that hands a SPELLING rather than
    # the resolved tip. `_base` calls this with the exact object it is about to
    # store, so that path resolves nothing; a direct caller may pass a short
    # sha or a symbolic name and gets the same answer. Never lowercase the
    # input: git refs are case-sensitive and `HEAD` is not `head`.
    if not isinstance(tip, str) or not tip:
        return None
    if not _FULL_TIP.fullmatch(tip):
        resolved, _branch = _resolve_tip(root, tip, infer_sha_branch=False)
        if not resolved or not _FULL_TIP.fullmatch(resolved):
            return None
        tip = resolved
    # `vcs` IS A FUNCTION-SCOPE IMPORT EVERYWHERE ELSE IN THIS MODULE and must
    # be one here too. The first cut called it at module scope inside a bare
    # `except Exception`, so the NameError was swallowed and the door silently
    # admitted BOTH real snapshot commits on this trunk while every probe read
    # "NO REFUSAL" — a fail-open guard that looked like a passing control. The
    # except is narrowed for the same reason: an environment failure is an
    # honest UNKNOWN, a programming error is not.
    from . import vcs
    try:
        rc, out, _err = vcs.backend(root).text(
            root, "show", "-s", "--format=%s", tip)
    except (OSError, ValueError):
        return None
    if rc != 0:
        return None
    if not str(out or "").strip().startswith(_SNAPSHOT_SUBJECT):
        return None
    return ("reviewed tip %s is fab's snapshot of a DIRTY tree, not a commit "
            "of your work: COMMIT BEFORE GATING. fab snapshots tracked and "
            "untracked files into a dangling commit so the node can gate what "
            "you are standing in; nothing ever commits it, so every "
            "commit-time rung was skipped on that content, and a reviewer "
            "binding it binds a tree that exists on no branch. Commit, gate "
            "the commit, and send that tip." % tip[:12])


def send(recipient, lane, message, ref, note=None, deadline_s=None,
         key=None, repo=None, sign=None, kind=None, new_work=False,
         supersedes=None, force=False, posture_na=None, unique_key=False,
         read_only_because=None, _cured_operation=None):
    """Persist first, attempt one DM, never auto-retry an existing operation.

    Explicit-key successor retries reconcile inside the append lock from their
    complete durable intent before mutable admission. A new operation is built
    and appended from that same locked snapshot, so concurrent retries append at
    most once and a different/additional successor refuses atomically.
    """
    message = str(message or "").strip()
    if not message or len(message) > 16000 or "\x00" in message:
        return None, "message must be 1-16000 characters without NUL", False
    # THE BYTE CEILING, AND IT REFUSES RATHER THAN CUTS (see BRIEF_CEILING).
    # Checked here, at the send door, BEFORE any file is written and before any
    # row is built — the sender is present, can split the brief, and a refusal
    # naming both numbers costs one retry. It is a strictly narrower bound than
    # the character one above for multibyte text and a wider one for ASCII,
    # which is why both exist: the character bound is what argv can carry, this
    # is what a brief may weigh.
    _brief_bytes = len(message.encode("utf-8"))
    if _brief_bytes > BRIEF_CEILING:
        return None, (
            "brief is %d UTF-8 bytes, over the %d-byte ceiling: this is a "
            "document, not a dispatch brief. Measured briefs run 5-11 KB and "
            "the largest ever recorded here was 10606 bytes, so the ceiling "
            "is roughly three times the biggest brief that has existed. It "
            "REFUSES rather than cutting, because a cut brief reads as a whole "
            "one to its recipient: split the work into two dispatches, or put "
            "the long part in a file the brief names."
            % (_brief_bytes, BRIEF_CEILING)), False
    # THE POSTURE GUARD AT THE INVARIANT (helm/posture.py): the body that
    # applies a strategy verb to a named dependency owes the three seam
    # questions whoever calls — the CLI forwarded its --posture-na here once
    # it stopped being the only door (dispatch fff5cef99aec).
    from . import posture
    refused = posture.check("helm dispatch send",
                            message + "\n" + str(note or ""),
                            posture_na=posture_na)
    if refused:
        return None, refused, False
    # THE READ-ONLY GUARD AT THE INVARIANT, same position and same law as the
    # posture guard beside it: a REVIEW brief that tells its reader not to edit
    # removes the one move the review procedure is built on.
    refused = check_read_only("helm dispatch send",
                              message + "\n" + str(note or ""),
                              kind=kind, because=read_only_because)
    if refused:
        return None, refused, False
    key = str(key or "").strip()
    if key:
        key, err = _clean(key, "operation key", 256)
        if err:
            return None, err, False
    from . import seats
    raw_recipient = recipient
    if _cured_operation:
        recipient, err = _canonical_recipient(recipient)
        if err:
            return None, err, False
    lane_value, err = _clean(lane, "lane", 160)
    if err:
        return None, err, False
    lane_value = _strip_lane_prefix(lane_value) or lane_value
    note_value = note
    if note is not None:
        note_value, err = _clean(note, "note", 1000)
        if err:
            return None, err, False
    kind_value, err = clean_kind(kind)
    if err:
        return None, err, False
    deadline_value = default_deadline_s(kind_value) if deadline_s is None else deadline_s
    deadline_value, err = _deadline(deadline_value)
    if err:
        return None, err, False
    message_hash = hashlib.blake2b(
        message.encode("utf-8"), digest_size=16).hexdigest()
    # THE HASH IS OVER THE ORIGINAL, THE BODY IS CAPPED — and they are computed
    # from the SAME `message` on purpose. The auto operation key hashes
    # `message_hash`, and three historical key derivations below replay through
    # it, so hashing the STORED (possibly truncated) body would silently mint a
    # second row for every long brief already on the ledger. `_store_body` is a
    # storage decision; `message_hash` is an identity contract. Computed ONCE
    # here because every admission path below builds its row through `_base`,
    # and the brief must reach whichever row is the one appended.
    # THE FILE IS WRITTEN HERE, BEFORE ANY ROW EXISTS, and the ordering is the
    # whole safety argument. Every `_base` call below is downstream of this
    # line and every append is downstream of them, so there is no interleaving
    # in which a row naming `brief_ref` reaches the ledger before the bytes it
    # names reach the disk. A kill in this window leaves an orphan file, which
    # no reader can see and no row can point at.
    #
    # A REFUSAL, NOT A DEGRADED WRITE. If the brief cannot be stored whole the
    # send is refused rather than falling back to the truncated row copy: the
    # sender is here and can retry, and silently downgrading to the behaviour
    # this lane exists to end would make the cure invisible exactly when it
    # failed.
    brief_ref, brief_bytes, brief_err = write_brief_file(message)
    if brief_err:
        return None, brief_err, False
    # The row copy is cut AFTER the file exists, so its notice can say where the
    # whole brief is instead of telling the reader the tail is lost.
    message_body = _store_body(message, has_ref=bool(brief_ref))
    info = _repo_info(repo)
    raw_parent = str(supersedes or "").strip() or None
    raw_tip = str(ref or "").strip().lower()
    intent = None
    if _cured_operation and key and info and _FULL_TIP.fullmatch(raw_tip) \
            and isinstance(raw_parent, str) and _ID.fullmatch(raw_parent):
        intent = {"operation_key": key, "recipient": str(recipient),
                  "lane": lane_value, "tip": raw_tip, "note": note_value,
                  "deadline_s": deadline_value, "repo_id": info["repo_id"],
                  "repo_root": info["repo"], "message_hash": message_hash,
                  "kind": kind_value, "supersedes": raw_parent}

    sender = usability_note = None

    def _op_id(author, repo_id, operation):
        return hashlib.blake2b(
            ("dispatch\0" + "\0".join((author, repo_id, operation)))
            .encode("utf-8"), digest_size=16).hexdigest()

    probe = alt_ops = None
    if not key:
        sender, err = _acting_author()
        if err:
            return None, err, False
        recipient, err = _recipient_operand(raw_recipient)
        if err:
            return None, err, False
        ok, why = _validate_recipient_rostered(recipient, force)
        if not ok:
            return None, why, False
        ok, why, usability_note = _validate_recipient_usable(recipient, force)
        if not ok:
            return None, why, False
        probe, err = _base(
            recipient, lane, ref, note, deadline_s, repo, kind=kind,
            sender=sender, new_work=new_work, supersedes=supersedes,
            message_hash=message_hash, message_body=message_body,
            brief_ref=brief_ref, brief_bytes=brief_bytes)
        if err:
            return None, err, False
        def _auto_key(lane_spelling):
            return "auto:" + hashlib.blake2b(
                "\0".join((probe["recipient"], lane_spelling, probe["tip"],
                            probe.get("note") or "", str(probe["deadline_s"]),
                            probe["message_hash"],
                            probe.get("supersedes") or "new-work")).encode("utf-8"),
                digest_size=16).hexdigest()
        key = _auto_key(probe["lane"])
        alt_ops = []
        raw_lane = str(lane or "").strip()
        for spelling in (raw_lane, "lane/" + probe["lane"]):
            if spelling and spelling != probe["lane"]:
                ak = _auto_key(spelling)
                pair = (ak, _op_id(sender, probe["repo_id"], ak))
                if pair not in alt_ops:
                    alt_ops.append(pair)
        if probe.get("supersedes") is None:
            def _auto_key_pre_parent(lane_spelling):
                return "auto:" + hashlib.blake2b(
                    "\0".join((probe["recipient"], lane_spelling,
                                probe["tip"], probe.get("note") or "",
                                str(probe["deadline_s"]),
                                probe["message_hash"])).encode("utf-8"),
                    digest_size=16).hexdigest()
            for spelling in (probe["lane"], raw_lane, "lane/" + probe["lane"]):
                if spelling:
                    ak = _auto_key_pre_parent(spelling)
                    pair = (ak, _op_id(sender, probe["repo_id"], ak))
                    if pair not in alt_ops:
                        alt_ops.append(pair)
        probe["id"] = _op_id(sender, probe["repo_id"], key)
        probe["operation_key"] = key
        if str(posture_na or "").strip():
            # THE RECORDED ESCAPE (helm/posture.py): the sender's reason the
            # seam questions do not apply rides the row, so a reader can tell
            # "asked and answered" from "never asked". Present only when given
            # — the row shape of every other dispatch is unchanged.
            probe["posture_na"] = str(posture_na).strip()
        _stamp_read_only(probe, read_only_because)
    elif not intent:
        sender, err = _acting_author()
        if err:
            return None, err, False
        recipient, err = _recipient_operand(raw_recipient)
        if err:
            return None, err, False
        ok, why = _validate_recipient_rostered(recipient, force)
        if not ok:
            return None, why, False
        ok, why, usability_note = _validate_recipient_usable(recipient, force)
        if not ok:
            return None, why, False
        probe, err = _base(
            recipient, lane, ref, note, deadline_s, repo, kind=kind,
            sender=sender, new_work=new_work, supersedes=supersedes,
            message_hash=message_hash, message_body=message_body,
            brief_ref=brief_ref, brief_bytes=brief_bytes)
        if err:
            return None, err, False
        probe["id"] = _op_id(sender, probe["repo_id"], key)
        probe["operation_key"] = key
        if str(posture_na or "").strip():
            # THE RECORDED ESCAPE rides every appended row, whichever
            # admission path built it (see the probe site above).
            probe["posture_na"] = str(posture_na).strip()
        _stamp_read_only(probe, read_only_because)

    def prepare(current):
        nonlocal sender, recipient, usability_note
        sender, why = _acting_author()
        if why:
            return None, (), why
        recipient, why = _recipient_operand(recipient)
        if why:
            return None, (), why
        ok, why = _validate_recipient_rostered(recipient, force)
        if not ok:
            return None, (), why
        ok, why, usability_note = _validate_recipient_usable(recipient, force)
        if not ok:
            return None, (), why
        if seats.recipient_matches(sender, recipient):
            return None, (), ("reviewer resolves to the executing sender; "
                              "self-delivery is refused before persistence")
        built, why = _base(
            recipient, lane, ref, note, deadline_s, repo, kind=kind,
            sender=sender, new_work=new_work, supersedes=supersedes,
            message_hash=message_hash, message_body=message_body,
            brief_ref=brief_ref, brief_bytes=brief_bytes, _current=current)
        if why:
            return None, (), why
        built["id"] = _op_id(sender, built["repo_id"], key)
        built["operation_key"] = key
        if str(posture_na or "").strip():
            # THE RECORDED ESCAPE rides every appended row, whichever
            # admission path built it (see the probe site above).
            built["posture_na"] = str(posture_na).strip()
        _stamp_read_only(built, read_only_because)
        # LAST FALLIBLE READ BEFORE APPEND. Git refs do not share the dispatch
        # lock, so cure membership must be re-measured after every other
        # admission/build step rather than allowed to age while `_base` probes.
        if _cured_operation:
            why = _cured_operation["validate"](current)
            if why:
                return None, (), why
        return built, (), None

    operation = dict(_cured_operation or (), intent=intent, prepare=prepare) \
        if intent else None
    row, why, existed = _append_dispatch(
        probe or intent, force=force, alt_ops=tuple(alt_ops or ()),
        unique_key=unique_key, cured_operation=operation)
    if why:
        return None, why, False
    # A RACE LOST IS NOT A SEND. Another send of this same operation wrote
    # the row, and delivered it, while this call was reading; this call did
    # neither, so it must not report `sent`, whatever the row now says.
    raced = bool(row.get(_WRITTEN_ELSEWHERE))
    row = {k: v for k, v in row.items() if k != _WRITTEN_ELSEWHERE}
    warnings = row.get(_WRITE_WARNINGS, ())
    notes = [n for n in (usability_note, _tier_note(recipient, kind),
                         _project_light_rung(row.get("repo_root") or repo, kind,
                                             new_work)[2]) if n]
    if notes:
        row = dict(row)
        row[_ADMISSION_NOTES] = list(notes)
    if existed:
        # THE TERMINAL STATE IS READ FIRST, before any reconciliation may
        # answer with the row. Ordering the cured-operation acceptance above
        # this made a CLOSED row a successful send, because `delivery` is
        # "observed" FOREVER on a row that was delivered and later cancelled:
        # the DM did happen, and the row still stopped awaiting a verdict.
        if not _open(row):
            if not operation:
                # the prior obligation is already TERMINAL (verdict or cancel) —
                # report that true state, never confirmation debt on a closed row
                return row, None, False
            # AND IT REFUSES OUT LOUD. Silence here is what made the caller
            # unable to tell "already reviewed" from "nothing is coming": the
            # only honest continuation names the dead row and the parent whose
            # obligation is still owed.
            return row, ("the row already recorded for this operation (%s) is "
                         "%s and no longer awaits a verdict, so it is not the "
                         "successor this send asks for — dispatch a fresh "
                         "review naming --supersedes %s if the obligation is "
                         "still owed"
                         % (str(row.get("id") or "")[:12], closed_state(row),
                            str(row.get("supersedes") or "") or "the parent")), \
                False
        if operation and row.get("delivery") == "observed" and not raced:
            return row, None, True
        return row, ("dispatch already recorded; delivery is %s — confirm at the "
                     "recipient, do not resend automatically" % row["delivery"]), False
    _queue_findings_pass(row)
    try:
        _notify_public(row, note or lane)
        delivered, dm_err = seats.dm(
            recipient, message, who=sender, profile=sender, sign=sign,
            session=home.session_id())
    except Exception as exc:
        delivered, dm_err = None, "%s: %s" % (type(exc).__name__, exc)
    if dm_err or not delivered:
        return _with_write_warnings(
            _reconcile_send(row["id"], dm_err or "DM returned no row"),
            warnings, notes)
    observed, err = _mark_delivered(row["id"], delivered.get("id"))
    if err:
        return _with_write_warnings(_reconcile_send(row["id"], err), warnings, notes)
    return _with_write_warnings(
        (observed, None, _open(observed)), warnings, notes)


def _lane_movement(row, reviewed):
    """Positive evidence that the row's OWN branch moved past the reviewed tip.

    A rebase after review orphans an APPROVE at birth: the write succeeds,
    the row closes, and the attestation authorizes landing a commit no branch
    carries — 7 such rows were live on 2026-07-31 (task #51). The cure is an
    ordering (rebase FIRST, then review) and this is the seam that enforces
    it — for land-authorizing writes only. A FIX on the old tip stays
    truthful when the author advances while fixing (the movement is often
    CAUSED by the review), and a SUPERSEDE is directly caused by it, so the
    caller consults this only for APPROVE.

    Identity is the row's ``ref_branch``, bound at the write boundary when
    the dispatch ref resolved through a local branch — never inferred from
    the free-text lane, which both refuses rows that never bound a branch
    and misses rows whose ref names one. Movement needs history proof:
    reviewed is an ancestor of the head (forward movement) or a recorded
    head in the branch's reflog (rebase). No branch binding, no such branch,
    no proof, git failure — all None: absence of evidence never blocks the
    write, and legacy rows carry no binding by design. Probes run under a
    scrubbed environment (ambient GIT_DIR must not redirect them), with
    replacements and legacy grafts disabled, and under the ledger lock —
    which brackets the ledger, not the repository, so movement between read
    and append remains possible; the class this stops has hours of skew, not
    milliseconds.
    """
    repo, branch = row.get("repo_id"), row.get("ref_branch")
    if not isinstance(repo, str) or not os.path.isabs(repo) \
            or not os.path.isdir(repo) or not isinstance(branch, str) \
            or not branch.startswith("refs/heads/"):
        return None
    from . import rowworld              # same immutable view as patch proofs
    env = _git_env()
    env.update(rowworld._history_view_env())
    try:
        p = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--end-of-options",
             branch + "^{commit}"],
            capture_output=True, text=True, timeout=5, env=env)
        head = p.stdout.strip().lower()
        if p.returncode != 0 or not _TIP.fullmatch(head) or head == reviewed:
            return None
        anc = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor",
             reviewed, head], capture_output=True, text=True, timeout=5,
            env=env)
        if anc.returncode == 0:
            return branch, head    # moved forward over the reviewed tip
        log = subprocess.run(
            ["git", "-C", repo, "rev-list", "-g", branch],
            capture_output=True, text=True, timeout=5, env=env)
        if log.returncode == 0 and reviewed in (
                x.strip().lower() for x in log.stdout.splitlines()):
            return branch, head    # the branch USED to sit at reviewed: rebase
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def _patch_tip_ancestry(row, reviewed, patch):
    """None when `patch` is a commit whose ancestry contains `reviewed`.

    THE CLAIM THE FIELD MAKES IS THE ONE CHECKED. Naming a patch tip says "I
    committed this cure on a branch off the exact tip I reviewed", which is
    exactly the claim `merge-base --is-ancestor` answers in the bound
    repository's immutable object view: no replacements or legacy grafts,
    and no shallow history mistaken for a complete negative. The history
    overlay is shared with rowworld/landreq, not a second spelling here.
    A tip that does not descend from the reviewed one was written against a
    different tree, so rebasing the lane onto it would silently carry work
    nobody reviewed — the lane-discipline class, arriving through a co-author
    instead of through a commit on main.

    UNREADABLE IS A REFUSAL HERE, not a shrug, and the split from
    `_lane_movement` is deliberate: there, absence of evidence must not block a
    write that is true anyway; here, the field IS the evidence, and recording
    an unverifiable co-author claim is worse than refusing it.
    """
    stored = row.get("repo_id")
    if not stored:
        return ("the row carries no repository identity (repo_id), so a patch "
                "tip's ancestry cannot be proven against it")
    if not isinstance(stored, str) or not os.path.isabs(stored):
        return "the row's repository identity (repo_id) is invalid"
    repo = row.get("repo_root") or stored
    if not isinstance(repo, str) or not os.path.isabs(repo) \
            or not os.path.isdir(stored) or not os.path.isdir(repo):
        return ("the named repository or checkout path is unavailable, so a "
                "patch tip's ancestry cannot be proven here")
    from . import gitfacts             # function-scope by module convention
    from . import rowworld              # deferred: rowworld imports dispatches
    from . import vcs                   # function-scope by module convention
    # vcs takes an OVERLAY: omission would reintroduce ambient selection vars.
    # Explicit removals preserve _git_env's scrub; a nonempty overlay also
    # bypasses projection memoisation. Do not use the cached common_dir API:
    # this proof must detect a checkout path reused for another repository.
    #
    # AND NO READ HERE MAY BE ANSWERED FROM THE CROSS-PROCESS FACT TABLE, for
    # the same reason the common_dir cache is refused a line above: this proof
    # re-measures on purpose. `merge-base --is-ancestor` below is an ADMITTED
    # question there — two full object ids under a pinned, scrubbed overlay,
    # indistinguishable from the one `rowworld` asks for the projection — and
    # the proof reads its 128 as "this repository cannot measure it", which a
    # stored 0 or 1 answers instead. The declaration goes on the OVERLAY, so
    # every read below carries it and so does the next one added here.
    env = {key: None for key in _GIT_SELECTION_ENV}
    env.update(rowworld._history_view_env())
    env[gitfacts.UNCACHED] = "1"
    be = vcs.backend(repo)
    rc, common, _err = be.text(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
        timeout=10, env=env)
    if rc or not common:
        return "patch tip repository identity could not be measured"
    bound, measured = _real(stored), _real(common)
    if bound is None or measured is None:
        return "patch tip repository identity could not be measured"
    if measured != bound:
        return ("patch tip repository differs from the row's repo_id — "
                "refusing cross-repository proof")
    # Read objects through the bound common-dir, not a checkout pathname
    # that can be repointed after the identity check. Linked worktrees
    # still share this identity; an independent clone does not.
    repo = bound
    rc, shallow, _err = be.text(
        repo, "rev-parse", "--is-shallow-repository", timeout=10, env=env)
    if rc or shallow != "false":
        return ("patch tip ancestry cannot be proven: repository history "
                "is shallow or its completeness could not be measured")
    rc, seen, _err = be.text(
        repo, "rev-parse", "--verify", "--end-of-options", patch + "^{commit}",
        timeout=10, env=env)
    if rc or seen.lower() != patch:
        return ("patch tip %s does not resolve to a commit in %s — commit "
                "the cure in your own worktree and name that exact id"
                % (patch[:12], repo))
    rc, _out, _err = be.text(
        repo, "merge-base", "--is-ancestor", reviewed, patch,
        timeout=10, env=env)
    if rc == 1:
        return ("patch tip %s does not descend from the reviewed tip %s — "
                "branch off the exact tip you reviewed and commit there, or "
                "the lane would carry work reviewed against a different tree"
                % (patch[:12], reviewed[:12]))
    if rc != 0:
        return ("patch tip ancestry could not be measured (git exited %d)"
                % rc)
    return None


#: A MODEL RUN'S READ, RECORDED ON ITS BEHALF — AND ADVISORY (task/2948). The
#: canonical fallback for a missing reviewer is a fresh-context read by another
#: family or by Fable (store premise review-is-cross-family-or-fable-never-
#: sonnet), and a Fable one-agent Workflow is not a seat: it has no roster row,
#: no session and no pane, so the seat-bound ledger had no way to take its
#: answer. The SEAT that ran the Workflow records the read and is named as
#: `recorded_by`; the model that read is `reviewer_model` and its family
#: `reviewer_family`, the run is `reviewer_run`, the lane author's model is
#: `author_model` (with `author_model_source`: a runtime record or the
#: recorder's declaration), and `independence` says WHY the read counts.
#:
#: IT COUNTS ONLY AS INDEPENDENT READS DO (owner ruling, same premise): the
#: model is ANOTHER FAMILY than the author's, or it is FABLE for a Claude
#: author. A model helm does not recognise is refused, a Sonnet or Haiku model
#: is refused, the author's own model is refused, and only the row's sender,
#: custodian or recipient may record one. Family is read off the catalog the
#: rest of helm routes by (seat_catalog), never off a seat label.
#:
#: AND IT DISCHARGES NOTHING. A declared model is not evidence — native seats
#: record no model, and a transcript's model field is not what ran — so until
#: helm verifies the named run's own record on disk (task/2966) the read is
#: written as an `advisory-read` event: the row keeps its status and stays
#: OWED, the read rides on it as `advisory_reads`, and the integrator reads it
#: for itself. APPROVE is refused outright; a model run CONCURs or FIXes.
REVIEWER_FIELDS = ("reviewer_model", "reviewer_run", "author_model",
                   "author_model_source", "recorded_by", "reviewer_family",
                   "independence")
ADVISORY_READ_EVENT = "advisory-read"
#: why an advisory read counts: another family, or Fable for a Claude author
INDEPENDENCE = ("cross-family", "fable")
_REVIEWER_MODEL_FLAG = "--reviewer-model"
_REVIEWER_RUN_FLAG = "--reviewer-run"
_AUTHOR_MODEL_FLAG = "--author-model"
_ON_BEHALF_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}\Z")
#: The models that never review anything (store premise
#: review-is-cross-family-or-fable-never-sonnet), matched as a whole name part
#: of the casefolded id: `claude-sonnet-4-5`, `claude-3-5-haiku`, `sonnet`.
_NEVER_REVIEWS = re.compile(r"(?:^|[^a-z])(sonnet|haiku)(?:[^a-z]|$)")


def _model_key(model):
    """A model id compared the way two spellings of ONE model agree: case
    folded, and the context-window suffix (`claude-fable-5[1m]`) dropped,
    because that suffix picks a window and never a different model."""
    return re.sub(r"\[[^\]]*\]\Z", "", str(model or "").strip()).casefold()


#: The Claude model aliases a Workflow agent or a launch flag names (store
#: reference canon-model-ids). Every other Claude id helm knows is in
#: seat_catalog.CC_AGENT_FRONTMATTER_MODELS or a catalogued family's models.
_CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")
_FABLE = re.compile(r"(?:^|[^a-z])fable(?:[^a-z]|$)")


def _catalog_models(entry):
    """Every model id one seat_catalog family entry names: its catalogued
    models, its per-instance models, and each provider row's alias and
    upstream model."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    out = set(seat_catalog.family_catalogued_models(entry))
    instance = entry.get("instance_models")
    if isinstance(instance, dict):
        out.update(v for v in instance.values() if isinstance(v, str))
    providers = entry.get("model_providers")
    if isinstance(providers, dict):
        for key, row in providers.items():
            out.add(key)
            if isinstance(row, dict):
                out.update(row.get(f) for f in ("alias", "upstream_model")
                           if isinstance(row.get(f), str))
    return {m.casefold() for m in out if m}


def _family_lineage(family):
    """The model family a helm seat family belongs to. A catalog family whose
    every model is a Claude id (the opus46 proxy seat) is CLAUDE: the owner's
    rule is about who made the model, and a proxy in front of it changes
    nothing about that."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    entry = seat_catalog.FAMILIES.get(family)
    models = _catalog_models(entry) if isinstance(entry, dict) else set()
    if models and all(m.startswith("claude-") for m in models):
        return "claude"
    return family


def _model_family(model):
    """The family of one model id, or None when helm does not recognise it.

    Recognised means NAMED somewhere helm routes by: a Claude alias or id
    helm's own catalog carries, or a model some seat family serves. An id
    that merely LOOKS like a vendor's (`claude-opus-5-4`) is not recognised,
    because a spelling nobody catalogued is exactly what an invented model
    looks like. Two families answering for one id is no answer either."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    key = _model_key(model)
    name = key.rsplit("/", 1)[-1]
    found = set()
    claude = set(_CLAUDE_ALIASES) | {
        m.casefold() for m in seat_catalog.CC_AGENT_FRONTMATTER_MODELS}
    if name in claude or key in claude:
        found.add("claude")
    for family, entry in seat_catalog.FAMILIES.items():
        if isinstance(entry, dict):
            models = _catalog_models(entry)
            if name in models or key in models:
                found.add(_family_lineage(family))
    return next(iter(found)) if len(found) == 1 else None


def _same_model(a, b):
    """Two names of ONE model: the same id, or Fable spelled two ways."""
    return _model_key(a) == _model_key(b) or bool(
        _FABLE.search(_model_key(a)) and _FABLE.search(_model_key(b)))


def _on_behalf_shape(reviewer_model, reviewer_run, author_model, polarity):
    """({field: value} or None, error) — the argument half, before any row.

    All-or-nothing on the pair: a model with no run cannot be traced, and a
    run with no model says nothing about independence."""
    given = {k: str(v).strip() for k, v in (
        ("reviewer_model", reviewer_model), ("reviewer_run", reviewer_run),
        ("author_model", author_model)) if str(v or "").strip()}
    if not given:
        return None, None
    if not ("reviewer_model" in given and "reviewer_run" in given):
        return None, ("a verdict recorded for a model run names BOTH %s and "
                      "%s — the model that read, and the run it read in"
                      % (_REVIEWER_MODEL_FLAG, _REVIEWER_RUN_FLAG))
    for key, value in given.items():
        if not _ON_BEHALF_TOKEN.fullmatch(value):
            return None, ("%s must be one token of letters, digits and "
                          "._:/@[]- (at most 128), got %r" % (key, value[:40]))
    never = _NEVER_REVIEWS.search(_model_key(given["reviewer_model"]))
    if never:
        return None, ("%s %s is a %s model, and %s never reviews anything: a "
                      "review is another family's read, or Fable's when only "
                      "a different model is needed. On a Fable limit, get "
                      "Fable through another credential or seat, or use the "
                      "%s seat — never step down a model"
                      % (_REVIEWER_MODEL_FLAG, given["reviewer_model"],
                         never.group(1).capitalize(),
                         never.group(1).capitalize(), REVIEW_FALLBACK_SEAT))
    # THE FINDINGS PASS'S MODEL IS NEVER THE DIFFERENT-MODEL READ (owner
    # contract, task/2960). Its read already rides the row as a findings
    # NOTE for the approving reviewer to adjudicate; recorded here it would
    # count as another family's independent read, which is the one thing
    # the owner ruled it never is.
    if _model_family(given["reviewer_model"]) == FINDINGS_READER:
        return None, ("%s %s is the %s findings pass's model: its read lands "
                      "on the row as a findings NOTE for the approving "
                      "reviewer to adjudicate, and it is never recorded as "
                      "the different-model read. Get another family's read "
                      "(the %s seat is always one) or Fable's"
                      % (_REVIEWER_MODEL_FLAG, given["reviewer_model"],
                         FINDINGS_READER, REVIEW_FALLBACK_SEAT))
    if polarity == "approve":
        return None, ("a model run recorded on its behalf does not APPROVE: "
                      "that verdict authorizes a land and binds a seat's own "
                      "runtime proof. Record the read with --concur (it "
                      "endorses and authorizes nothing) or --fix/--supersede")
    return given, None


def _on_behalf_binding(row, given):
    """({field: value}, error) — the row half: WHO may record it, and whether
    the reader really is a different model from the author.

    The author is the row's SENDER: the review row was filed by the seat
    whose work it reviews. Its model comes from its runtime record when one
    carries a model (a proxy seat's route names it), and otherwise from the
    recorder's declaration; a declaration that contradicts the record is
    refused, because a row must not carry two answers to one question."""
    from . import seats
    recorder, err = _acting_author()
    if err:
        return None, "the recording seat could not be named: %s" % err
    parties = [p for p in (row.get("sender"), custodian_of(row),
                           row.get("recipient")) if p]
    if not any(seats.recipient_matches(recorder, p) for p in parties):
        return None, ("only this row's sender or recipient records a model "
                      "run's verdict on it; @%s is neither (%s)"
                      % (recorder, ", ".join("@" + p for p in parties)))
    declared = given.get("author_model")
    recorded = _runtime_model(row.get("sender"))
    if recorded and declared and _model_key(recorded) != _model_key(declared):
        return None, ("%s %s contradicts the author's runtime record, which "
                      "names %s" % (_AUTHOR_MODEL_FLAG, declared, recorded))
    author = recorded or declared
    if not author:
        return None, ("the author's model is not recorded for @%s, so a "
                      "different-model read cannot be told from its own — "
                      "name it with %s <model>"
                      % (row.get("sender") or "?", _AUTHOR_MODEL_FLAG))
    reader = given["reviewer_model"]
    if _same_model(author, reader):
        return None, ("the reviewing model %s IS the author's model %s: a "
                      "read by the author's own model is not an independent "
                      "review. Get another family's read (the %s seat is "
                      "always one) or Fable's, never Sonnet or Haiku"
                      % (reader, author, REVIEW_FALLBACK_SEAT))
    family = _model_family(reader)
    if family is None:
        return None, ("helm does not recognise the model %s: it is no Claude "
                      "alias or id helm carries and no model a seat family in "
                      "the catalog serves, so its family — the one thing that "
                      "makes the read independent — cannot be told" % reader)
    authors = {_family_lineage(f) for f in _runtime_families(row.get("sender"))}
    author_family = _model_family(author)
    if author_family:
        authors.add(author_family)
    if not authors:
        return None, ("the author's family cannot be told for @%s (%s is not "
                      "a model helm recognises and no runtime record names a "
                      "family), so no read can be shown to be another "
                      "family's" % (row.get("sender") or "?", author))
    if family not in authors:
        independence = "cross-family"
    elif authors == {"claude"} and family == "claude" and _FABLE.search(
            _model_key(reader)):
        independence = "fable"
    else:
        return None, ("%s is the same family (%s) as the author's %s, and it "
                      "is not Fable reading a Claude author: a review is "
                      "another family's read, or Fable's. The %s seat is "
                      "always another family" % (reader, family, author,
                                                  REVIEW_FALLBACK_SEAT))
    return {"reviewer_model": reader,
            "reviewer_run": given["reviewer_run"], "author_model": author,
            "author_model_source": "runtime" if recorded else "declared",
            "recorded_by": recorder, "reviewer_family": family,
            "independence": independence}, None


_ADVISORY_EXIT_FIELDS = ("exit_answer", "worse_than_main_paths", "patch_tip",
                         "no_patch_because")


def _advisory_record(event, state):
    """(record, error) — one advisory-read event checked the way its writer
    checks it, and the record it projects onto the row. The reducer calls
    this and so does the writer (through `_apply`), so no shape the writer
    refuses can be replayed onto a row."""
    reviewed = str(event.get("reviewed_tip") or "").lower()
    if not state.get("tip") or reviewed != state["tip"]:
        return None, "advisory read names another tip"
    if event.get("polarity") not in ("concur", "fix", "supersede"):
        return None, "advisory read carries no admitted polarity"
    if not _valid_ts(event.get("ts")):
        return None, "advisory read has no valid time"
    evidence, err = _clean(event.get("verdict_ref"), "advisory evidence", 4096)
    if err:
        return None, err
    for key in ("reviewer_model", "reviewer_run", "author_model"):
        if not _ON_BEHALF_TOKEN.fullmatch(str(event.get(key) or "")):
            return None, "advisory read has a malformed %s" % key
    if _NEVER_REVIEWS.search(_model_key(event["reviewer_model"])) \
            or event.get("independence") not in INDEPENDENCE \
            or event.get("author_model_source") not in ("runtime", "declared") \
            or not _TOKEN.fullmatch(str(event.get("recorded_by") or "")) \
            or not _TOKEN.fullmatch(str(event.get("reviewer_family") or "")) \
            or event.get("reviewer_family") == FINDINGS_READER:
        return None, "advisory read fails its independence fields"
    record = {"ts": event["ts"], "reviewed_tip": reviewed,
              "verdict_ref": evidence, "polarity": event["polarity"]}
    if "basis" in event:
        record["basis"] = replay_basis(event["basis"])
    record.update({key: event[key] for key in REVIEWER_FIELDS})
    record.update({key: event[key] for key in _ADVISORY_EXIT_FIELDS
                   if key in event})
    return record, None


#: THE LOCAL FINDINGS PASS (task/2960, owner contract). Every review row gets
#: one qwen27 read, run by `helm/findingspass.py` in a detached process, and
#: its result lands HERE as a NOTE for the approving reviewer to adjudicate.
#: The note is NEVER an approval, NEVER a gate and NEVER the different-model
#: read: it is projected as `findings_notes` and nothing that reads a verdict,
#: an advisory read or a family reads that key. `FINDINGS_READER` is refused
#: as the model of an advisory read for the same reason (`_on_behalf_shape`).
FINDINGS_NOTE_EVENT = "findings-note"
FINDINGS_READER = "qwen27"
#: A note lands only on a row that is still owed a review. A verdicted or
#: closed row has nobody left to adjudicate it, and appending there would
#: move the seq that the close ladders bind.
FINDINGS_NOTE_STATES = ("open", "held")
#: complete / partial / unread are the script's own three answers; not-run,
#: failed and timeout are the pass's, for a read that never produced one.
FINDINGS_OUTCOMES = ("complete", "partial", "unread", "not-run", "failed",
                     "timeout")
#: The script's exit code for each of its own answers (its calling contract).
FINDINGS_EXIT = {"complete": 0, "partial": 2, "unread": 3}
#: The row keeps a BOUNDED copy of the findings, because every list read
#: parses every row; the whole output is stored by reference
#: (`write_brief_file`) and the note names it.
FINDINGS_TEXT_CAP = 3000
FINDINGS_REASON_CAP = 480
_FINDINGS_STATUS = re.compile(
    r"LOCAL-REVIEW-STATUS (complete|partial|unread)(?: [a-z_]+=\d+)*")
_FINDINGS_COUNTS = ("rc", "reads", "kept", "wall_s", "output_bytes")
_FINDINGS_FIELDS = _FINDINGS_COUNTS + ("outcome", "reason", "status_line",
                                       "findings", "output_ref")


def findings_text_ok(text):
    """Is this bounded, printable, multi-line text a note may carry? Newlines
    and tabs are the only control characters admitted, so a model's answer
    can never carry a terminal escape onto a reviewer's screen."""
    return isinstance(text, str) and 0 < len(text) <= FINDINGS_TEXT_CAP \
        and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
                    and c not in "\n\t" for c in text)


def _findings_record(event, state):
    """(record, error) — one findings-note event checked the way its writer
    checks it, and the record it projects onto the row.

    "NO FINDINGS" IS EARNED, NEVER DEFAULTED. An outcome the script answered
    (complete, partial, unread) must carry the exit code that answer has in
    the script's contract, a status line saying the same word, and both
    counts. So a pass that could not read — or a writer that mislabels an
    unread run — cannot put a note on the ledger that renders as clean."""
    reviewed = str(event.get("reviewed_tip") or "").lower()
    if not state.get("tip") or reviewed != state["tip"]:
        return None, "the note names another tip than the row's"
    if not _valid_ts(event.get("ts")):
        return None, "the note has no valid time"
    if event.get("reader") != FINDINGS_READER:
        return None, "the note names a reader other than %s" % FINDINGS_READER
    outcome = event.get("outcome")
    if outcome not in FINDINGS_OUTCOMES:
        return None, "the note carries no known outcome"
    record = {"ts": event["ts"], "reviewed_tip": reviewed,
              "reader": FINDINGS_READER, "outcome": outcome}
    for key in _FINDINGS_COUNTS:
        if key in event:
            value = event[key]
            if type(value) is not int or (key != "rc" and value < 0):
                return None, "the note has a malformed %s" % key
            record[key] = value
    if "reason" in event:
        reason, err = _clean(event.get("reason"), "findings reason",
                             FINDINGS_REASON_CAP)
        if err:
            return None, err
        record["reason"] = reason
    status = None
    if "status_line" in event:
        line, err = _clean(event.get("status_line"), "status line", 512)
        status = None if err else _FINDINGS_STATUS.fullmatch(line)
        if not status:
            return None, "the note's status line is not a LOCAL-REVIEW-STATUS line"
        record["status_line"] = line
    if "findings" in event:
        if not findings_text_ok(event.get("findings")):
            return None, "the note's findings are not bounded printable text"
        record["findings"] = event["findings"]
    if "output_ref" in event:
        if not _BRIEF_REF.fullmatch(str(event.get("output_ref") or "")) \
                or "output_bytes" not in record:
            return None, "the note's output reference is malformed"
        record["output_ref"] = event["output_ref"]
    if outcome in FINDINGS_EXIT and (
            record.get("rc") != FINDINGS_EXIT[outcome] or not status
            or status.group(1) != outcome
            or "reads" not in record or "kept" not in record):
        return None, ("a %s note carries exit %d, a status line saying %s, "
                      "and its read and kept counts"
                      % (outcome, FINDINGS_EXIT[outcome], outcome))
    if outcome != "complete" and "reason" not in record:
        return None, "a %s note says why" % outcome
    return record, None


def _one_line(text, cap):
    """One printable line of at most `cap` characters, the cut MARKED."""
    line = "".join(c if unicodedata.category(c)[0] != "C" else " "
                   for c in " ".join(str(text or "").split()))
    mark = " [cut]"
    return line if len(line) <= cap else line[:cap - len(mark)] + mark


#: How many times one findings note reads the ledger before it writes. Every
#: read but the last is UNLOCKED, and the write after it lands only if the
#: ledger is still the file that read saw; the read after a miss restores the
#: fold checkpoint the read before it saved, so it folds only the new tail
#: and is short. THE LAST READ IS UNDER THE LOCK. Measured with real
#: processes: a batch of back-to-back locked closes (fold under the lock,
#: append, no pause — the shape of `helm lr retire --apply`) re-takes the
#: lock faster than an unlocked reader can read again, and eight optimistic
#: tries lost 3 of 3 races in 45-194 ms, dropping the note, while the locked
#: writer before this cure landed it after one wait. No pause between tries
#: can cure that (a pause only lets more appends land), so the final try
#: reads and writes under one lock: with the checkpoint the earlier reads
#: saved it folds the tail only, and a cold fold there is the price of a
#: ledger written faster than it can be read.
FINDINGS_NOTE_TRIES = 8


def record_findings_note(rid, tip, fields):
    """(row, error) — append ONE findings-note event to a row still owed a
    review. `fields` holds the pass's answer (see `_FINDINGS_FIELDS`); the
    reader, the time and the tip are stamped here.

    THROUGH `_apply` BEFORE THE APPEND, exactly as the advisory read is: a
    shape the reducer would refuse is refused here instead, so no note is
    ever on the ledger that replay would drop.

    THE LEDGER LOCK COVERS THE WRITE, NEVER THE FOLD, on every try but the
    last: the discipline every dispatch-ledger writer shares
    (`_ledger_write`). It takes `FINDINGS_NOTE_TRIES` tries rather than the
    writers' default, and that constant says why: a writer that re-takes the
    lock faster than this one can read again wins every optimistic race, and
    a note that gave up there was dropped."""
    path = ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — findings note NOT " \
                "recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("status") not in FINDINGS_NOTE_STATES:
            return None, ("dispatch %s is %s — a findings note lands only "
                          "on a row still owed a review"
                          % (row["id"][:12], row.get("status")))
        event = {"v": 3, "event": "findings-note", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reader": FINDINGS_READER,
                 "reviewed_tip": str(tip or "").lower()}
        event.update({k: v for k, v in (fields or {}).items()
                      if k in _FINDINGS_FIELDS and v is not None})
        if "reason" in event:
            event["reason"] = _one_line(event["reason"], FINDINGS_REASON_CAP)
        out = _apply(row, event)
        if out is row:
            return None, ("the findings note was refused by the reducer "
                          "before append — nothing was recorded")
        if not txn.append(event):
            return None, "ledger unwritable (%s) — findings note " \
                "NOT recorded" % path
        return out, None
    return _ledger_write(attempt, path, tries=FINDINGS_NOTE_TRIES)


def _queue_findings_pass(row):
    """Start the qwen27 findings pass on a review row just filed or retipped.

    NEVER SLOWER AND NEVER A FAILURE for the filing: the pass runs in a
    detached process that queues behind every other pass, and nothing here
    raises. The one ledger write this can make is on the path where the
    process could not even be started, because the owner's contract is that
    the row still gets its one line saying why."""
    if not isinstance(row, dict) or row.get("kind") != "review" \
            or not row.get("id"):
        return
    try:
        from . import findingspass
        started, why = findingspass.queue(row)
    except Exception as exc:                   # noqa: BLE001 — never the filing's
        started, why = False, "%s: %s" % (type(exc).__name__, exc)
    if started or not why:
        return
    try:
        record_findings_note(row["id"], row.get("tip"), {
            "outcome": "not-run",
            "reason": "the pass could not be started: %s" % why})
    except Exception:                          # noqa: BLE001 — never the filing's
        pass


def _runtime_families(seat):
    """The families the approval tier's family-of(actor) resolver proves for
    a seat, or an empty set when it proves none."""
    if not seat:
        return set()
    try:
        families, _evidence, _anchor, why = \
            _approval_identity_family_evidence(seat)
    except Exception:                       # noqa: BLE001 — unread is empty
        return set()
    return set() if why or not families else set(families)


def _runtime_model(seat):
    """The model a seat's runtime record names, or None when it names none
    or cannot be read — the same family-of(actor) resolver the approval tier
    reads, so this adds no second reading of the roster."""
    if not seat:
        return None
    try:
        _families, evidence, _anchor, why = \
            _approval_identity_family_evidence(seat)
    except Exception:                       # noqa: BLE001 — unread is None
        return None
    if why or not isinstance(evidence, dict):
        return None
    source = evidence.get("proxy_proof") if evidence.get("v") == 3 \
        else evidence.get("runtime")
    model = source.get("model") if isinstance(source, dict) else None
    return str(model).strip() if isinstance(model, str) and model.strip() \
        else None



def _verdict_author_refusal(row, reviewed, why):
    """Name the recipient when a verdict author proof fails for another seat.

    A land-authorizing verdict binds its author to the row's RECIPIENT: the
    family proof is looked up for that seat under the calling session. When a
    different seat calls, the lookup fails as 'no exact runtime record', which
    is true and useless: it sends the caller hunting a runtime problem that
    does not exist. This only rewrites a refusal that already happened;
    it never refuses anything the proof would have accepted. An unknown caller
    and the recipient itself keep the underlying reason unchanged."""
    recipient = str(row.get("recipient") or "")
    seat, _err = _acting_author("record this verdict")
    if not seat or not recipient or seat.casefold() == recipient.casefold():
        return why
    return ("this row is addressed to @%s, not @%s: a land-authorizing verdict "
            "binds its author to the row's recipient, so no proof under your "
            "session can satisfy it. Ask @%s to re-dispatch it to you (helm "
            "dispatch send %s %s --ref %s --kind %s --supersedes %s), or record "
            "an advisory read on a model run's behalf with %s and %s "
            "(task/2948). Underlying: %s"
            % (recipient, seat, recipient, seat, row.get("lane") or "<lane>",
               reviewed, row.get("kind") or "review", row["id"],
               _REVIEWER_MODEL_FLAG, _REVIEWER_RUN_FLAG, why))


def mark_verdict(rid, reviewed_tip, evidence, polarity=None, basis=None,
                 bind_author=False, worse_than_main_paths=None,
                 finding_count=None, prior_relation=None, patch_tip=None,
                 imperfect=False, no_patch_because=None, reviewer_model=None,
                 reviewer_run=None, author_model=None):
    reviewed = str(reviewed_tip or "").strip().lower()
    if not _TIP.fullmatch(reviewed):
        return None, "verdict needs the full exact reviewed commit id"
    # The gate token is an ADDRESS, not prose: it resolves to a minted
    # receipt and nothing after it is trusted, so it must not spend the
    # evidence budget. The 2026-08-02 drain measured a valid subsumption
    # statement + token overflowing 256 together and dying as "too long"
    # with no hint why. Strip the token(s) for the length check only; the
    # STORED evidence keeps them verbatim.
    budgeted = _GATE_TOKEN_RE.sub("", str(evidence or ""))
    if len(budgeted) > 256:
        return None, ("verdict evidence is %d chars over the 256 budget "
                      "(gate: tokens excluded — they address a receipt, they "
                      "are not prose): %d chars of statement" %
                      (len(budgeted) - 256, len(budgeted)))
    evidence, err = _clean(evidence, "verdict evidence", 4096)
    if err:
        return None, err
    # BIND-TIME door-check (clause 2 of the close-door grammar drain): parse
    # the WHOLE attestation ONCE, while re-minting is cheap — not later at lr
    # close, where the immutable verdict has already wasted a round. A
    # confirmation verdict is APPROVE for both original APPROVE and original
    # FIX debt, so the statement grammar (not this verdict's polarity) declares
    # which prior debt it may close. Historical close replay deliberately keeps
    # _subsumption_ref's permissive pre-cutover grammar; only new writes pass
    # this strict door.
    _statement, statement_err = _parse_subsumption(evidence)
    if statement_err:
        return None, ("evidence names a malformed subsumption: %s — the close "
                      "door needs `Subsumption verified <what later trunk work "
                      "did> on trunk <how it resolved>` (any casing), or "
                      "`Subsumption verified FIX findings were answered on "
                      "trunk: <resolution>`" % statement_err)
    if polarity is None or not str(polarity).strip():
        return None, ("verdict polarity is required: an undeclared verdict is "
                      "immutable and can never be retired; use --fix when unsure "
                      "(a later approved land can discharge it)")
    polarity, err = clean_polarity(polarity)
    if err:
        return None, err
    # BASIS IS REQUIRED AT THE CLI, PERMISSIVE HERE — the split this file
    # already settled twice, for `polarity` and for `kind`, and its own comment
    # in the verdict branch says why: "REQUIRED at the CLI for new writes,
    # still accepted as None by mark_verdict() so historical replay and
    # projection are untouched."
    #
    # I BUILT IT THE OTHER WAY FIRST and the measurement corrected me: making
    # it required here breaks 289 existing call sites across 9 test files, none
    # of which are ABOUT basis. That is not a rule with teeth, it is a 289-site
    # diff nobody can review, landing on a file three other lanes are already
    # editing. The value of a required argument is that a REAL WRITER cannot
    # omit it; every real writer comes through the CLI branch below.
    #
    # AN INVALID value is still an error HERE, exactly as clean_polarity is:
    # absent is a permissive default, wrong is never quietly coerced.
    basis, err = clean_basis(basis)
    if err:
        return None, err
    err = _finding_error(finding_count, prior_relation)
    if err:
        return None, err
    observations = {key: value for key, value in zip(
        _FINDING_FIELDS, (finding_count, prior_relation)) if value is not None}
    # THE REVIEWER'S CURE IS A FIX VERDICT'S FIELD AND ONLY A FIX VERDICT'S.
    # APPROVE ends the loop and needs no cure; SUPERSEDE says this work is
    # replaced, so a patch on top of it names a tree nobody will land; CONCUR
    # blocks nothing. A patch tip on any of those is a claim about the wrong
    # row, so it refuses rather than being recorded where no reader looks.
    patch = str(patch_tip or "").strip().lower()
    if patch and not _FULL_TIP.fullmatch(patch):
        return None, ("patch tip must be the full exact commit id of your "
                      "committed cure")
    if patch and polarity != "fix":
        return None, ("--patch-tip belongs to --fix: it names the mechanical "
                      "cure you committed off the reviewed tip. A DESIGN "
                      "finding is not patched under review — take it to a meld")
    worse_paths, err = _clean_worse_than_main_paths(worse_than_main_paths)
    if err:
        return None, err
    if worse_paths and polarity not in _EXIT_QUESTION_POLARITIES:
        return None, ("worse-than-main paths belong only to FIX/SUPERSEDE "
                      "verdicts")
    # THE SECOND EXIT ANSWER, AND IT ONLY EXISTS TO CARRY A CURE. A read that
    # found the tip no worse than main and committed a real improvement had no
    # door: approve refuses the patch field and imperfect refused outright, so
    # the patch travelled by chat and the author never owed an answer on it.
    # IMPERFECT WITH NO PATCH IS STILL REFUSED HERE, at the invariant and not
    # only at the CLI: that case is genuinely "approve and file the remainder",
    # and a guard at one door is a guard the next caller walks around.
    imperfect = bool(imperfect)
    if imperfect and polarity not in _EXIT_QUESTION_POLARITIES:
        return None, ("an imperfect exit answer belongs only to FIX/SUPERSEDE "
                      "verdicts")
    if imperfect and worse_paths:
        return None, ("a verdict answers the exit question ONCE: "
                      "worse-than-main paths or imperfect, never both")
    if imperfect and not patch:
        return None, ("IMPERFECT IS NOT A BLOCK: without a committed cure to "
                      "carry, use APPROVE with a verified gate and file the "
                      "remaining findings as dispatch rows")
    no_patch_reason = None
    if str(no_patch_because or "").strip():
        no_patch_reason, err = _clean(no_patch_because, "no-patch reason", 256)
        if err:
            return None, err
    if no_patch_reason and patch:
        return None, ("a verdict names the cure OR says why there is none, "
                      "never both")
    if no_patch_reason and polarity != "fix":
        return None, ("--no-patch-because answers the cure question a FIX "
                      "verdict owes; no other polarity is asked it")
    on_behalf, err = _on_behalf_shape(reviewer_model, reviewer_run,
                                      author_model, polarity)
    if err:
        return None, err
    if on_behalf:
        # NO SEAT AUTHOR PROOF ON THIS VERDICT, deliberately. That proof binds
        # the row's RECIPIENT and is what the approval tier reads; the reader
        # here is a model run, not a seat, so the event is written without it
        # (v3, and a tier reader answers PRE-TIER: it authorizes nothing).
        bind_author = False
    path = ledger_path()
    given_on_behalf = on_behalf

    def attempt(txn):
        # EACH TRY STARTS FROM THE CALLER'S BINDING: the row binds it below.
        on_behalf = given_on_behalf
        # BOUND BEFORE THE READ, deliberately. The projection below tests this
        # name, and a guard that can raise NameError instead of answering is
        # the exact defect the stale `author_keys` rename shipped last round.
        event = None
        if not txn.held:
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("verdict_retracted"):
            return None, retracted_refusal(
                row, "a retracted row takes no new verdict")
        if row["status"] == "verdict":
            if row.get("reviewed_tip") == reviewed \
                    and row.get("verdict_ref") == evidence \
                    and row.get("polarity") == polarity \
                    and row.get("basis") == basis \
                    and {k: row[k] for k in _FINDING_FIELDS if k in row} \
                    == observations \
                    and str(row.get("patch_tip") or "") == patch \
                    and str(row.get("no_patch_because") or "") \
                    == str(no_patch_reason or "") \
                    and row.get("exit_answer") \
                    == ("worse-than-main" if worse_paths
                        else "imperfect" if imperfect else None) \
                    and tuple(row.get("worse_than_main_paths") or ()) \
                    == tuple(worse_paths or ()) and not on_behalf:
                # Idempotent retry RECONCILES the standing attestation (the
                # _reconcile_send law): report what IS, never re-emit.
                out = dict(row)
                out["announce"] = _reconcile_announce(row)
                return out, None
            # A DIFFERENT polarity or basis on the same tip+evidence is not a
            # retry, it is an attempt to flip a standing verdict's semantics.
            # Terminal is immutable: it falls through to the refusal below,
            # which names the one door that corrects a wrong verdict without
            # rewriting it (task/3060).
            return None, ("dispatch %s already has a verdict (closed) — a "
                          "standing verdict is immutable. If it is WRONG, its "
                          "author or the integrator retracts it: `helm "
                          "dispatch retract %s --reason R --reads "
                          "source-clean|fix|supersede|unknown "
                          "--measured|--inferred --reissue`"
                          % (rid, row["id"][:12]))
        if row["status"] == "cancelled":
            return None, ("dispatch %s was cancelled (abandoned) — a verdict "
                          "asserts a review happened, so it is refused" % rid)
        if row["status"] == "closed":
            return None, ("dispatch %s is already closed through its approved "
                          "review descendant" % rid)
        if row["status"] == "held":
            return None, ("dispatch %s is held (%s) -- release it first with "
                          "`helm dispatch release %s`, then verdict"
                          % (rid, row.get("hold_reason") or "no reason given",
                             row["id"][:12]))
        if not row.get("tip"):
            return None, "historical dispatch lacks an exact tip; redispatch it"
        if reviewed != row["tip"]:
            return None, ("stale verdict: reviewed %s but dispatched tip is %s "
                          "(first difference at character %d)" %
                          (reviewed, row["tip"],
                           _first_difference(reviewed, row["tip"])))
        if on_behalf:
            on_behalf, err = _on_behalf_binding(row, on_behalf)
            if err:
                return None, err
        if patch:
            err = _patch_tip_ancestry(row, reviewed, patch)
            if err:
                return None, err
        if on_behalf:
            # ADVISORY, NEVER A VERDICT (see REVIEWER_FIELDS). One run is
            # recorded once: a retry of the same run on the same tip reports
            # the row as it stands.
            if any(r.get("reviewer_run") == on_behalf["reviewer_run"]
                   and r.get("reviewed_tip") == reviewed
                   for r in row.get("advisory_reads") or ()):
                return dict(row), None
            event = {"v": 3, "event": ADVISORY_READ_EVENT,
                     "seq": row["seq"] + 1, "id": row["id"],
                     "ts": pk.now_ts(), "reviewed_tip": reviewed,
                     "verdict_ref": evidence, "polarity": polarity}
            if basis:
                event["basis"] = basis
            event.update(on_behalf)
            if worse_paths:
                event.update(exit_answer="worse-than-main",
                             worse_than_main_paths=list(worse_paths))
            elif imperfect:
                event["exit_answer"] = "imperfect"
            if patch:
                event["patch_tip"] = patch
            if no_patch_reason:
                event["no_patch_because"] = no_patch_reason
            out = _apply(row, event)
            if out is row:
                return None, ("the advisory read was refused by the reducer "
                              "before append — nothing was recorded")
            if not txn.append(event):
                return None, ("ledger unwritable (%s) — advisory read NOT "
                              "recorded" % path)
            return out, None
        # Land-authorizing writes only: a FIX on the old tip is truthful when
        # the author advanced while fixing, and a SUPERSEDE is directly
        # caused by movement (46f0e21 review).
        moved = _lane_movement(row, reviewed) if polarity == "approve" else None
        if moved:
            branch, head = moved
            return None, ("the lane moved under this review: branch %s is now "
                          "at %s while this dispatch still names %s, so the "
                          "verdict would be orphaned the moment it is written. "
                          "Re-review at the current head and verdict the "
                          "re-issue: helm dispatch send %s %s --ref %s --kind "
                          "%s --supersedes %s (body on stdin); or cancel this "
                          "row if the work is superseded"
                          % (branch, head, reviewed, row["recipient"],
                             row["lane"], head, row.get("kind") or "review",
                             row["id"]))
        author = None
        if bind_author:
            author_session = home.session_id()
            families, family_evidence, family_anchor, family_why = \
                _approval_identity_family_evidence(
                    row.get("recipient") or "", session=author_session,
                    require_exact_session=True)
            if family_why or not families or len(families) != 1:
                return None, _verdict_author_refusal(
                    row, reviewed,
                    "verdict author session/family proof is unavailable: "
                    + (family_why or "family is not uniquely proven"))
            family = next(iter(families))
            family_why = _family_evidence_error(
                family_evidence, row.get("recipient") or "", family,
                family_anchor)
            if family_why:
                return None, _verdict_author_refusal(
                    row, reviewed,
                    "verdict author session/family proof is invalid: "
                    + family_why)
            session = family_evidence.get("session") \
                if isinstance(family_evidence, dict) else None
            if not isinstance(session, str) or not session \
                    or family_evidence.get("v") not in (3, 5):
                return None, ("verdict author proof is not bound to one exact "
                              "runtime session")
            runtime_evidence = _verdict_author_runtime_evidence(
                row.get("recipient") or "", session, family, family_evidence)
            runtime_anchor = _verdict_author_runtime_anchor(runtime_evidence)
            if not runtime_evidence or not runtime_anchor:
                return None, "verdict author runtime proof could not be normalized"
            # DERIVED FROM THE OWNED TUPLE, NOT HAND-SPELLED.
            # This producer and `VERDICT_AUTHOR_EVIDENCE_FIELDS` were two
            # independent spellings of one bundle, so a fourth field added at
            # the consumer would leave the writer emitting three — the exact
            # shape whose mirror image (a stale `author_keys`) took every
            # verdict down last round. Zipping the tuple makes the producer
            # UNABLE to disagree with the schema.
            author = dict(zip(VERDICT_AUTHOR_EVIDENCE_FIELDS,
                              (session, runtime_evidence, runtime_anchor)))
            if set(author) != set(VERDICT_AUTHOR_EVIDENCE_FIELDS):
                return None, ("verdict author bundle does not match the "
                              "recorded schema")
        # THE GATE BINDING. Until this existed, a verdict's evidence was prose
        # and the only thing checked about it was its LENGTH — so "whole-suite
        # Ran 5115 OK" and a green run were indistinguishable to every reader
        # downstream. `helm gate run` mints a receipt from a real run; a
        # `gate:<id>` token here RESOLVES to it. A token naming a run of a
        # DIFFERENT commit, a dirty tree, or a non-OK status is refused
        # outright: that is a claim already proven false, and there is no
        # compatibility argument for recording one. Evidence with NO token is
        # still valid for FIX/SUPERSEDE: those verdicts authorize no land and a
        # later gated APPROVE can discharge them. APPROVE is different — this
        # writer stamps receipt-v1, so an ungated approve is immutable evidence
        # that can never authorize landing. Refuse it before append; historical
        # ungated rows remain untouched and replay honestly as UNVERIFIED.
        # THE NEED NAMES WHAT THE VERDICT SPENDS. An APPROVE authorizes a
        # land, so it asks the whole-suite question and a focused receipt
        # REFUSES it inside bind. Every other polarity is a cure-round
        # verdict that authorizes nothing — a focused receipt whose recorded
        # scope covers the reviewed diff is exactly the evidence it needs,
        # and a whole-suite receipt still satisfies the weaker need too.
        # AND THE ROW'S OWN CONSUMING CHECKOUT GOES WITH IT. `repo_id` is the
        # shared admin dir, which is one value for a repository root and every
        # linked worktree of it — so a root and a lane worktree that declare
        # DIFFERENT commands make that census ambiguous and an honest receipt
        # from the lane's own declared command was refused as "which of these
        # two". The row has known the answer since it was minted: `repo_root`
        # is the checkout `dispatch send` resolved. Discarding it here made
        # this reader re-derive a narrower fact from a wider one.
        gate_state, gate_id, gate_why = gate.bind(
            evidence, reviewed, repo_id=row.get("repo_id"),
            reviewed_ts=row.get("ts"),
            consuming_repo=row.get("repo_root"),
            need=gate.NEED_SUITE if polarity == "approve"
            else gate.NEED_FOCUSED)
        if gate_state == "REFUSED":
            return None, "gate evidence does not bind: " + gate_why
        if polarity == "approve" and GATE_CAP_RECEIPT in GATE_CAPS \
                and gate_state != "VERIFIED":
            # THE REPAIR THIS NAMES IS THE ONE THE SEQUENCE ACTUALLY RUNS.
            # Sending the reviewer to run the whole suite on the REVIEWED tip
            # spends a suite on a tree nothing will land. A reviewer whose
            # source read is clean HOLDS; the integrator rebases and runs the
            # one whole-suite gate on the tree that lands; this approve binds
            # THAT token. The refusal names exactly that one repair, because a
            # refusal naming two teaches neither.
            return None, ("approve verdict requires a verified gate:<token>: an "
                          "ungated approve is immutable and can never authorize "
                          "landing. If your source read is clean, do not mint "
                          "this yourself: `helm dispatch hold <row> "
                          "--source-clean <tip> <reason>`, which records the "
                          "claim where the integrator's "
                          "`helm dispatch list --source-clean` can find it, "
                          "then record the approve against the token the "
                          "integrator's land gate mints on the rebased tree")
        # v4 is a reader barrier: a v3 reader must refuse this verdict rather
        # than ignore its recorded policy and authorize against today's state.
        event = {"v": 4 if bind_author else 3, "event": "verdict", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed, "verdict_ref": evidence,
                 "polarity": polarity,
                 "gate": gate_id or "", "gate_caps": list(GATE_CAPS)}
        event.update(observations)
        # THE KEY IS OMITTED WHEN THERE IS NO BASIS, not written as null.
        # Writing `"basis": None` on every event made the key ALWAYS present,
        # which defeats the absent-stays-absent rule three lines of comment
        # above it claimed to honour — and it showed up immediately as an
        # idempotency break: the first return lacked the key and the replayed
        # retry carried it, so two identical verdicts compared unequal. A
        # record that says nothing about basis is the truthful shape for a
        # write that declared none.
        if basis:
            event["basis"] = basis
        if worse_paths:
            event.update(exit_answer="worse-than-main",
                         worse_than_main_paths=list(worse_paths))
        elif imperfect:
            # NO PATH LIST, DELIBERATELY: this answer names nothing that
            # regresses, which is the whole claim. What it carries is the
            # patch below, and `verdict_exit_answer` refuses to read it
            # without one.
            event["exit_answer"] = "imperfect"
        if no_patch_reason:
            event["no_patch_because"] = no_patch_reason
        # THE PATCH AND ITS AUTHOR TRAVEL TOGETHER, and the author is the ROW'S
        # RECIPIENT rather than a value the caller supplies: the reviewer this
        # row was dispatched to is the one seat that could have read this tip
        # and written a cure for it, and a name the writer accepts is a name
        # the writer cannot check.
        if patch:
            event.update(patch_tip=patch,
                         patch_author=row.get("recipient") or "")
        if author:
            event.update(author)
            projected = _apply(row, event)
            if projected is row:
                return None, "verdict author proof was refused before append"
            captured, err = _record_verdict_tier(projected)
            if err:
                return None, "record-time approval tier was not retained: " + err
            event.update(captured)
            tier, why = approval_tier_for_verdict(_apply(row, event))
            if tier == "unknown":
                return None, "record-time approval tier is unavailable: " + str(why)
        if not txn.append(event):
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path

        # AFTER THE LOCK IS RELEASED: the projection, the epoch marker, the
        # lease release and the announcement are none of them the write.
        def finish():
            # THE CANONICAL REDUCER OWNS THE PROJECTION. Rebuilding the
            # verdict row by hand here made the WRITE path and the REPLAY path two
            # independent readers of one event, free to disagree — so a new field could
            # report immediate success and then fail replay or leave the row open, which
            # is precisely the class the stale-rename incident exposed. `_apply` is what
            # replay runs; running it here means the caller's answer IS the projection.
            out = _apply(row, event) if event is not None else dict(row)
            # AND THE REDUCER'S REFUSAL IS AN ERROR, NOT A SHRUG. `_apply` returns the
            # state OBJECT it was handed on every refusal path (pinned by
            # ApplySignalsWhatItTook), so identity IS the signal. Reaching here with a
            # refused event means the ledger has an appended verdict the projection
            # will not take: the caller would be told SUCCESS and the row would replay
            # OPEN. That divergence is the whole class this cure closes, so it fails
            # loudly at the one place that can still see both halves.
            if event is not None and out is row:
                return None, ("verdict was appended but the canonical reducer refused "
                              "it — the row would replay OPEN, so the ledger and the "
                              "projection disagree; do not trust this write")
            # `gate` is the ONLY field recorded, and everything a reader wants
            # is derived from it (gate_state). The answer carries no second
            # spelling of it: the idempotent-retry path early-returns the
            # REPLAYED row and cannot carry one, so a retry would answer in a
            # different shape than the original call (tests/test_dispatches),
            # and two stored spellings of one fact are two places to update.
            # NO FIELD IS RE-SET BY HAND HERE — status, tip, ref, polarity,
            # seq, gate, gate_caps, basis, the exit answer and the author
            # bundle all come from `_apply` one line above. A hand re-set would
            # make the caller's answer a SECOND derivation of the event, able
            # to agree with replay today and diverge silently tomorrow, and it
            # would mask the divergence the parity arm exists to catch.
            # FREEZE THE CUTOVER on the first stamped write, so it is a recorded fact
            # rather than a per-read derivation. No-op once a marker exists.
            if GATE_CAPS:
                try:
                    record_gate_epoch()
                except Exception:               # noqa: BLE001 — never fail a verdict
                    pass
            # WHAT THE ACTUATOR CLAIMS, THE VERDICT RELEASES. When a row is
            # dispatched to an idle seat the offer layer claims `dispatch:<row id>`
            # ON THAT SEAT'S BEHALF and tells it to start -- and it discards the
            # lease id it minted, so the holder is never handed the token `release`
            # demands. Binding a verdict finishes that work and closes the row, and
            # nothing released the lease: three stood on one seat in one night, every
            # one found by the stop-guard rather than by the seat, while every reader
            # of `helm chat claims` saw a seat mid-work on lanes it had already
            # verdicted -- on the night reviewer availability was the scarcest thing
            # the fleet had. An entry and an exit belong to the same owner; a release
            # only a guard remembers is not an exit.
            _release_autoclaim(row["id"])
            pk.event("dispatch-verdict", row["id"], evidence)
            out["announce"] = _announce_verdict(out, reviewed, evidence)
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


#: THE ACTUATOR'S RESOURCE SPELLING, WRITTEN ONCE. Six sites spelled this by
#: hand -- two here, one in the offer layer, three in idle_dispatch -- and the
#: seventh got it wrong: a cure that composed the FULL row id looked up a
#: resource nothing mints and released nothing, while its own arms minted the
#: same wrong spelling and agreed with it. The 8-char prefix is the OFFER
#: LAYER'S choice (seats_work_offer builds the offer tuple from `rid[:8]`), so
#: this function is a reader of that decision and not a second author of it.
def autoclaim_resource(rid):
    """The `dispatch:` claim resource for a row id, or None if it cannot be
    spelled. A row id shorter than the prefix has no resource rather than a
    short one -- the offer layer skips such rows for the same reason."""
    rid = str(rid or "")
    return ("dispatch:" + rid[:8]) if len(rid) >= 8 else None


def _release_autoclaim(rid):
    """Release the `dispatch:<rid>` lease the offer layer claimed for us.

    THE TOKEN IS RECOVERED, NOT DEMANDED. `_binding_ok` requires the lease
    nonce and the actuator threw its copy away, so the holder cannot present
    what it was never given -- and the binding check's own message names the
    cure for exactly that strand: `own_leases` hands a holder its own token
    back. This asks for it the same way `helm chat claims` does.

    IT CAN ONLY RELEASE OUR OWN, AND ONLY IF HELM CAN ADMIT US. The seat
    name is resolved through `helm.actors.resolve_actor` rather than read off
    the identity floor, because a release is a durable mutation and a DERIVED
    name that collided with a real holder would recover that holder's token.
    `own_leases` then returns rows held by that admitted seat and nothing
    else, so the resource is released exactly when the
    holder is the caller -- the same identity `release` would test anyway.
    Another seat's auto-claim is invisible here, which is the point.

    SILENT ON ABSENCE, BECAUSE ABSENCE IS THE ORDINARY CASE. Most verdicts
    are bound by a seat that claimed its own room (`worktree:...`) and holds
    no dispatch resource at all, so "no such lease" is not a problem to
    report. And the verdict is ALREADY WRITTEN by the time this runs: a
    lease that cannot be released costs one stale row on one surface, while
    a raised exception would cost the verdict its answer, which is the
    trade every fail-soft on this path makes.
    """
    try:
        from . import actors, seats
        # THE NAME COMES THROUGH THE ADMISSION DOOR, NOT OFF THE FLOOR. A
        # release DELETES a row from the claims ledger, so this is an
        # ACTUATOR site by the identity layer's own three-way split, and its
        # rule for one is not "classify it" but "resolve it": a DERIVED name
        # that happened to match a real holder would recover that holder's
        # token and release work still running. A caller helm cannot admit
        # releases nothing, which is the right answer and not a degradation.
        actor, err = actors.resolve_actor(
            seats._env_session(), seats.safe_cwd(),
            act="release the lease the actuator claimed for this seat")
        if err:
            return
        seat = actor.canonical_name
        resource = autoclaim_resource(rid)
        lease = seats.own_leases(seat).get(resource) if resource else None
        if lease:
            seats.release(resource, seat, lease=lease,
                          session=seats._env_session())
    except Exception:                   # noqa: BLE001 — never fail a verdict
        pass


def gate_state(row):
    """VERIFIED <receipt> / UNVERIFIED — DERIVED from the one recorded field.

    Never stored beside `gate`: a second spelling of one fact is two places to
    update and one of them gets forgotten. UNVERIFIED is the honest reading of
    both a verdict written before `helm gate` existed and one whose evidence
    carries no token — in neither case did anything check the claim."""
    rid = str((row or {}).get("gate") or "")
    return ("VERIFIED " + rid) if _GATE_ID.fullmatch(rid) else "UNVERIFIED"


def attest_path():
    return os.path.join(os.path.dirname(ledger_path()), "attests.jsonl")


INTENT_KEYS = frozenset(("v", "event", "id", "ts", "room", "binding"))
DONE_KEYS = frozenset(("v", "event", "id", "ts", "room", "binding",
                       "payload", "turn", "receipt", "chain", "kind", "text"))
POLARITY_SOURCE = "dispatch-store"
ATTEST_SOURCE = "attest-sidecar"


def _source_label(value):
    return str(value or "source unknown").replace("-", " ")


def _attest_rows():
    """({row id: [attest events]}, unavailable) from ONE sidecar read.

    The generic event-ledger reader owns path safety, event-size bounds, torn
    tails and strict JSON parsing. Attestation adds only its historical blank-
    line grammar and domain event-name validation before reusing `_group`.
    """
    rows, unavailable = eventledger.checked_events(
        attest_path(), strict=True, skip_blank=True)
    if unavailable:
        return {}, "attest ledger %s" % unavailable
    if any(row.get("event") not in ("intent", "done")
           or type(row.get("id")) is not str or not row["id"] for row in rows):
        return {}, "attest ledger unscopable row"
    return _group(rows), None


def _reduce_attest(events, row, reviewed, evidence):
    """Pure per-verdict attest reduction.

    Returns (intent, done, detail, category), where category is a stable
    `unverifiable`/`unknown` protocol value and detail remains human wording.
    The writer API still exposes its historical three-tuple through
    `_attest_state`.
    """
    want = _binding_key(row, reviewed, evidence)
    rid = str(row["id"])
    intent = done = None
    for event in events:
        if event["event"] == "intent":
            bad = _intent_schema_error(event, rid, want)
            if bad:
                return None, None, bad[1], bad[0]
            if intent is not None:
                if event != intent:
                    return None, None, "conflicting attest intents", "unknown"
                continue          # byte-semantic identical duplicate: idempotent
            intent = event
        elif event["event"] == "done":
            bad = _done_schema_error(event, rid, want, intent)
            if bad:
                return None, None, bad[1], bad[0]
            if done is not None:
                if event != done:
                    return None, None, "conflicting attest done rows", "unknown"
                continue
            done = event
    return intent, done, None, None


def _attest_state(row, reviewed, evidence):
    """Single-row read wrapper preserving the writer's historical API."""
    grouped, unavailable = _attest_rows()
    if unavailable:
        return None, None, unavailable
    intent, done, detail, _category = _reduce_attest(
        grouped.get(str(row["id"]), ()), row, reviewed, evidence)
    return intent, done, detail


def attest_unverifiable(rows):
    """The decided row ids whose signed delivery record is unverifiable.

    This compatibility surface from the attest-split lane delegates to the
    typed projection rather than interpreting the sidecar again. It is read-only,
    reads the sidecar at most once for the page, and intentionally excludes
    unreadable/unknown state: those are not evidence of a binding split.
    """
    projected = attest_projections(rows)
    return frozenset(rid for rid, state in projected.items()
                     if state.get("attest_state") == "unverifiable")


def _intent_schema_error(r, rid, want):
    if set(r.keys()) != INTENT_KEYS:
        return "unknown", "attest intent schema violation (keys)"
    if type(r["v"]) is not int or r["v"] != 1:   # bool is not int here
        return "unknown", "attest intent schema violation (v)"
    if not (isinstance(r["ts"], str) and r["ts"]):
        return "unknown", "attest intent schema violation (ts)"
    if not (isinstance(r["room"], str) and r["room"]):
        return "unknown", "attest intent schema violation (room)"
    if r["binding"] != want:
        return "unverifiable", "attest intent binding mismatch"
    return None


def _done_schema_error(r, rid, want, intent):
    if intent is None:
        return "unknown", "attest done without intent"
    if set(r.keys()) != DONE_KEYS:
        return "unknown", "attest done schema violation (keys)"
    if type(r["v"]) is not int or r["v"] != 1:   # bool is not int here
        return "unknown", "attest done schema violation (v)"
    if not (isinstance(r["ts"], str) and r["ts"]):
        return "unknown", "attest done schema violation (ts)"
    if r["room"] != intent["room"]:
        return "unverifiable", "attest done room mismatch"
    if r["binding"] != want:
        return "unverifiable", "attest done binding mismatch"
    if not isinstance(r["text"], str):
        return "unknown", "attest done schema violation (text)"
    if r["kind"] == "attested":
        if not (isinstance(r["payload"], str) and r["payload"]
                and isinstance(r["turn"], str) and r["turn"]
                and isinstance(r["receipt"], str) and r["receipt"]
                and type(r["chain"]) is int):
            return "unknown", "attest done evidence incomplete for attested"
    elif r["kind"] == "cite-tier":
        if not (r["payload"] == "" and r["turn"] == "" and r["receipt"] == ""
                and r["chain"] is None):
            return "unknown", "attest done evidence inconsistent for cite-tier"
    else:
        return "unknown", "attest done unknown kind"
    return None


def _binding_key(row, reviewed, evidence):
    import hashlib
    raw = "\x1e".join([str(row.get("lane") or ""), reviewed,
                        str(row["id"]), str(evidence or "")])
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


def _report_from_done(done, row, reviewed, evidence, chat):
    """The replayed report is RE-VERIFIED, never trusted (round-5 blocker 1):
    an attested done must recompute — committed-signed shape over its stored
    transport evidence AND chat.payload_for over the LEDGER-truth binding +
    the stored text must equal the stored payload. Verification failure is
    UNKNOWN-grade (None), not a downgrade to cite."""
    if done["kind"] == "cite-tier":
        return "delivered UNSIGNED (cite-tier) to %s" % done["room"]
    synthetic = {"vlane": str(row.get("lane") or ""), "vtip": reviewed,
                 "vrid": str(row["id"]), "vref": str(evidence or ""),
                 "text": done["text"], "payload": done["payload"],
                 "turn": done["turn"], "receipt": done["receipt"],
                 "chain": done["chain"]}
    if (chat.committed_signed(synthetic)
            and chat.payload_for(synthetic) == done["payload"]):
        return "attested (signed turn, room %s)" % done["room"]
    return None


def _attest_projection(row, grouped, unavailable):
    """Typed, read-only attestation axis for one canonical dispatch row.

    Dispatch status/polarity and verdict attestation are separate ledgers and
    separate facts. The old code detected a cross-ledger binding mismatch only
    while a writer retried `verdict`, then printed it to an ephemeral console
    line. A reader of either durable row surface saw nothing. This projection
    makes the already-durable mismatch visible without appending or re-signing.
    """
    has_verdict = row.get("status") in ("verdict", "closed") \
        and row.get("verdict_ref") is not None
    base = {"polarity_source": POLARITY_SOURCE if has_verdict else None,
            "attest_source": ATTEST_SOURCE if has_verdict else None,
            "attest_state": None, "attest_detail": None}
    if not has_verdict:
        return base
    reviewed = str(row.get("reviewed_tip") or "")
    evidence = str(row.get("verdict_ref") or "")
    if unavailable:
        intent = done = None
        detail, category = unavailable, "unknown"
    else:
        intent, done, detail, category = _reduce_attest(
            grouped.get(str(row["id"]), ()), row, reviewed, evidence)
    if detail:
        base["attest_state"] = category
        base["attest_detail"] = detail
        return base
    if done:
        from . import chat
        report = _report_from_done(done, row, reviewed, evidence, chat)
        if report is None:
            base["attest_state"] = "unverifiable"
            base["attest_detail"] = (
                "attest done failed signed-turn/payload re-verification")
            return base
        base["attest_state"] = done["kind"]
        base["attest_detail"] = report
        return base
    if intent:
        base["attest_state"] = "doubt"
        base["attest_detail"] = (
            "attest intent exists without a durable done; read-only, never re-sign")
        return base
    base["attest_state"] = "none"
    base["attest_detail"] = "no attest intent recorded for this verdict"
    return base


def _has_verdict(row):
    return row.get("status") in ("verdict", "closed") \
        and row.get("verdict_ref") is not None


def attest_projections(rows):
    """{dispatch id: typed attestation fields} from at most ONE sidecar read."""
    rows = list(rows)
    grouped, unavailable = _attest_rows() if any(map(_has_verdict, rows)) \
        else ({}, None)
    return {str(row["id"]): _attest_projection(row, grouped, unavailable)
            for row in rows}


def with_verdict_projections(rows):
    """Copy rows with source/attestation axes, from at most ONE sidecar read."""
    rows = list(rows)
    grouped, unavailable = _attest_rows() if any(map(_has_verdict, rows)) \
        else ({}, None)
    out = []
    for row in rows:
        item = dict(row)
        item.update(_attest_projection(row, grouped, unavailable))
        out.append(item)
    return out


def _announce_verdict(row, reviewed, evidence):
    """Attest a fresh verdict AT MOST ONCE with replay closedness.

      unknown ledger state -> NEEDS CONFIRMATION, read-only (never no-intent)
      done                 -> report DERIVED from its fields
      intent without done  -> DOUBT: read-only confirmation from the intent's
                              STORED room; never re-signs
      no intent            -> the one provably sign-free state: append intent
                              under lock (single-flight), emit outside it."""
    path = attest_path()
    with eventledger.locked(path) as held:
        if not held:
            return ("NEEDS CONFIRMATION — attest ledger unwritable (%s); "
                    "announce not attempted" % path)
        intent, done, unknown = _attest_state(row, reviewed, evidence)
        if unknown:
            return ("NEEDS CONFIRMATION — %s; read-only until repaired"
                    % unknown)
        if done:
            from . import chat
            state = _report_from_done(done, row, reviewed, evidence, chat)
            return state or ("NEEDS CONFIRMATION — attest done fails "
                             "re-verification; read-only until repaired")
        if not intent:
            from . import chat as _chat
            room = os.environ.get("HELM_VERDICT_ROOM") or _chat._default_post_room()
            # attestations follow the project room the verdict belongs to
            # (the owner asked why they landed in #main while the fleet works
            # in #helm); HELM_VERDICT_ROOM stays the deliberate-centralization
            # override
            if not eventledger.append_unlocked(path, {
                    "v": 1, "event": "intent", "id": row["id"],
                    "ts": pk.now_ts(), "room": room,
                    "binding": _binding_key(row, reviewed, evidence)}):
                return ("NEEDS CONFIRMATION — attest intent unwritable; "
                        "announce not attempted")
    if intent:
        return _confirm_from_room(row, reviewed, evidence, intent)
    return _emit_and_record(row, reviewed, evidence, room)


def _land_nudge_parts(row):
    """landreq's (word, why, command) for this row, for the nudge below.

    THE WHOLE TRIPLE, because taking [0] threw away the half that says WHY.
    A nudge reading UNVERIFIED with no reason tells the reader something is
    wrong and nothing about what — and the reason was already computed one
    call away. Deferred import: landreq imports this module."""
    from . import landreq
    return landreq.land_nudge_instruction(row["id"])


def _verdict_land_nudge(row):
    """An APPROVE binds — DM the lander the landing command.

    2026-07-29 council G2: 4 stale rows closed by hand because the APPROVE
    settled and then NOTHING woke the lander, and land had no surface at all
    (memory lr-assigns-reviewers-but-never-wakes-them). The review leg has
    idle_dispatch; the LAND leg should have this — one contextual DM per
    approve, never a nag loop. Called AFTER mark_verdict returns (the verdict
    is durable under the ledger lock; the nudge is delivery and must NOT hold
    the lock — a stalled chat transport would stall every verdict in the
    fleet behind it, measured: OI posted the exact trap shape an hour ago).
    A failed DM is invisible (never stops the verdict from binding: the
    verdict is durable; the nudge is delivery).

    IT PRESCRIBES ONLY WHAT THE LADDER PLAINLY ALLOWS — one defect with two
    faces, not two defects. This line was built from verdict POLARITY ALONE
    and never consulted the row's readiness at all, so every approve got the
    identical "ready: helm lr land <id>". Two live specimens, both
    misinforming a real recipient: a row whose author and builder were the
    same seat and whose review field was EMPTY while the projection already
    read READY-SELF-REVIEW; and a row hundreds of commits behind trunk whose
    gate bound a tree that no longer existed. The first cure special-cased
    SELF-REVIEW; self-review is ONE rung of a ladder.

    ONE RULE, AND THE ENUMERATION IS NOT HERE. `landreq.land_instruction`
    answers what this row IS through the projection the board itself reads;
    this asks for the word, prescribes only on plain READY, and carries
    whatever else it gets — SELF-REVIEW, CONTESTED, STALE-BASE, UNVERIFIED,
    a terminal, or a rung the ladder learns tomorrow — without naming one of
    them. A list of states in the notifier is the same defect regrowing one
    branch at a time.

    THE CURE DISCLOSES, NEVER WITHHOLDS. A suppressed DM would rebuild the G2
    defect this nudge exists to cure (an APPROVE that wakes nobody), and the
    threat model is accident — the recipient is misinformed, not attacked, so
    the fix is the missing field said out loud. The command still arrives;
    the "ready:" prescription does not, because ready is exactly what the
    projection says this row is not. Both legs carry the word — the fallback
    room post is the half that is easy to get wrong twice."""
    tip = str(row.get("reviewed_tip") or "?")[:10]
    lane = row.get("lane") or "?"
    # ONE MINTING FOR BOTH LEGS. The command carries --ack-deletions when the
    # door will demand it, and the word and reason come from the same call —
    # so the DM and the room post cannot disagree about what this row is.
    word, why, cmd = _land_nudge_parts(row)
    if word == "READY":
        # THE ROOM LEG SAYS READY TOO. It used to carry only the command, so
        # the one word a reader scans for was present on one leg and absent
        # on the other, and the quieter leg read like a weaker claim.
        return _nudge(_default_lander(),
                      "VERDICT APPROVE at %s — ready: %s" % (tip, cmd),
                      "%s: VERDICT APPROVE — ready: %s" % (lane, cmd))
    # THE REASON RIDES WITH THE WORD. "UNVERIFIED" alone names a problem and
    # withholds the only part that lets the reader act on it.
    detail = "%s (%s)" % (word, why) if why else word
    return _nudge(_default_lander(),
                  "VERDICT APPROVE at %s — %s, not plainly READY. Read it, "
                  "then decide: %s" % (tip, detail, cmd),
                  "%s: VERDICT APPROVE %s — %s" % (lane, detail, cmd))


def _default_lander():
    """The seat that folds, ASKED in ONE place.

    Both nudges route through here and a third copy would have put the same
    question in shipping logic three times — the hardcode rung flagged the
    second one the moment it appeared.

    THE ANSWER IS READ, NOT SPELLED. A literal default keeps naming that seat
    after the roster stops carrying it, and nothing fails loudly: the string
    is truthy, the DM is addressed, the room-post fallback is addressed, and
    both report a delivery neither made. seats_lander OBEYS HELM_LANDER when
    it is set — an override exists for the case where the ordinary resolution
    is wrong, and the roster is among the things that can be wrong, so
    confirming it there would take the hatch away at the one moment it is
    needed — and otherwise asks the integrator role, which is the
    relationship the literal was silently asserting anyway.

    SO THE TYPO'S COST LANDS HERE, and `_nudge` below is where it is now
    reported: an override that names nobody is obeyed, the DM does not
    deliver, and the fallback addresses the same name. That is the whole
    reason this call has a warning beside it rather than a silent False.

    IT STILL CANNOT RETURN EMPTY, and `_nudge` below depends on that: its
    fallback is ADDRESSED BY CONSTRUCTION, so a refusal here would emit the
    unaddressed room post that fallback exists to cure. `lander_seat_or_default`
    keeps that invariant and announces a substitution rather than making one
    silently.
    """
    from . import seats_lander
    return seats_lander.lander_seat_or_default()


def _nudge_undelivered(to, why):
    """Say once that a nudge's DM did not land -> None. Never raises.

    ONE LINE PER RECIPIENT PER PROCESS, through the same `_warn_once` the
    integrator and lander doors use for their own unresolved cases, so a
    verdict loop cannot turn a real signal into noise.
    """
    try:
        from .seats_identity import _warn_once
        _warn_once("nudge-undelivered:%s" % to,
                   "a verdict nudge to %r did not deliver (%s) — it was "
                   "posted to the room addressed to the lander instead, which "
                   "reaches nobody if that name has no roster row\n"
                   % (to, why or "no reason given"))
    except Exception:                                       # noqa: BLE001
        pass          # a report about a failed delivery must not fail a verdict


def _nudge(to, body, context):
    """DM `to`; on ANY failure fall back to an ADDRESSED room post. Never raises.

    ONE shape for both poles, because the fallback is the half that is easy to
    get wrong twice — and it already was wrong once. The approve nudge's own
    fallback was DEAD CODE: it branched on `if lander:` immediately after
    binding the lander to a value that cannot be falsy, so the else could
    never execute; and had it executed it would have posted the string
    "@None". The real failure mode is a DM that does not DELIVER, never a name
    that is empty, so that is what this branches on instead.

    The fallback is ADDRESSED BY CONSTRUCTION — _default_lander() cannot return
    empty, so this can never emit the unaddressed room post that is the exact
    defect the author nudge exists to cure.

    An EMPTY `to` skips straight to the fallback rather than reaching the
    transport with nothing to address — an unrecorded author is a known state,
    not an error to discover by exception.

    Returns True when the DM itself landed.
    """
    from . import seats
    try:
        if to:
            # THE DM'S TWO FAILURE EXITS ARE REPORTED BY ONE PATH. A returned
            # (None, reason) and a RAISED transport error both mean the same
            # thing to a reader — the verdict did not reach this name — and
            # the report covered only the first because it sat after the call
            # inside the outer guard, so an exception jumped past it to the
            # silent pass. Narrowing the try to the CALL puts both exits in
            # front of the same reporter.
            #
            # THE RAISE BECOMES ITS OWN REASON rather than a generic word:
            # the exception type and message are what was OBSERVED, and this
            # door still cannot say whether the name is wrong or the
            # transport is down. Saying which would be a diagnosis it has no
            # evidence for.
            try:
                sent, why = seats.dm(to, body, who="dispatches")
            except Exception as exc:                        # noqa: BLE001
                sent, why = None, "the DM raised %s: %s" % (
                    type(exc).__name__, exc)
            # BOTH LEGS MUST AGREE before this reads as delivered. dm's
            # contract is exactly (row, None) or (None, reason); a (row,
            # reason) shape is a CONTRACT VIOLATION, and treating any
            # non-None row as delivery would suppress the fallback while
            # reporting success — a silent wake-path loss inside the cure
            # for silent wake-path loss. Violations
            # fall through to the addressed fallback, which over-delivers;
            # over-waking the lander is recoverable, a swallowed verdict
            # is the founding defect.
            if sent is not None and why is None:
                return True
            # THE DM'S REASON IS THE ONLY EVIDENCE ANYONE EVER GETS, and
            # until now it was spent as a BRANCH and never said out loud.
            # Every later chance to notice is closed on purpose: the
            # fallback's `chat.post` returns a row and reports nothing about
            # whether the addressee resolves (the ABSENT line belongs to the
            # command surface, not to `post`), this function returns False,
            # and both call sites are bare statements. So a verdict addressed
            # to a name nothing answers to went to nobody and said so nowhere.
            #
            # NOT BLOCKING IS NOT THE SAME PROPERTY AS NOT REPORTING. The
            # silence below is deliberate and stays — a nudge must never block
            # a verdict — but a warning is not a block, and the operator who
            # can fix a misaddressed lander is the one running this command.
            # The text says what was OBSERVED, never what it means: a DM can
            # fail for a name that does not resolve and for a transport that
            # did not answer, and this cannot tell them apart.
            _nudge_undelivered(to, why)
    except Exception:
        pass                          # a nudge must never block a verdict
    try:
        from . import chat
        chat.post("@%s %s" % (_default_lander(), context), who="dispatches")
    except Exception:
        pass
    return False


def _verdict_author_nudge(row):
    """A NON-APPROVE verdict hands the row BACK — wake the seat it went back to.

    IT HANDS BACK A DECISION, NOT NECESSARILY THE TYPING. A FIX may carry the
    reviewer's own committed cure (`patch_tip`), and then what goes back is a
    rebase and a re-dispatch. The wake is owed either way: the lane owner is
    the one seat that can compose the next tip.

    _verdict_land_nudge above covers the APPROVE pole and DMs the LANDER.
    NOTHING covered the other pole, and that omission is the owner's P0 word
    for word: a CHANGES_REQUESTED verdict hands the row back to its author and
    nothing wakes them. The verdict is not lost — _announce_verdict posts it
    and the ledger holds it — but it is announced into the project room with
    NO @mention, and helm's own beacon contract is that a home room does not
    wake a seat row-by-row. Correct, durable, readable on demand, and reaching
    nobody. The half of the loop where the news is good got a wake path a
    fortnight ago; the half where work comes back never did.

    MEASURED 2026-08-11T00:02Z, which is why this ships as a nudge and not as
    a rule telling seats to poll: 22 codex-family verdicts in the preceding two
    hours, 21 of them fix. In the ten minutes before the ledger was read, one seat
    posted that its review was "OPEN ... sent 20 minutes ago" when the verdict
    was already 26 minutes old, and another posted that its three lanes
    were awaiting a reviewer when all three had come back 17-23 minutes
    earlier. Four rows, two seats, every one idle on news that had arrived —
    and the integrator held two more of its own in the same state.

    THE FALLBACK IS ADDRESSED, NEVER AMBIENT. An unusable sender is the one
    case that could rebuild this defect inside its own cure, because posting
    into the room with nobody named is precisely what already fails. So an
    author who cannot be resolved routes to the LANDER BY NAME and says the
    author could not be woken. Waking someone who cannot act is recoverable;
    waking nobody is the thing just measured.

    Delivery, never binding: called AFTER mark_verdict returns, outside the
    ledger lock, and every failure is swallowed — a stalled transport must not
    stall the fleet's verdicts behind it (the finding on the approve pole,
    which applies here unchanged).
    """
    pol = (row.get("polarity") or "UNDECLARED").upper()
    rid = row["id"][:17]
    # resolve_recipient inside dm() is the membership test — an unknown or
    # family-floor sender comes back (None, reason) rather than routing a DM
    # into a lane no seat reads, and _nudge turns that into the room fallback.
    # THE CUSTODIAN IS TOLD, NOT THE AUTHOR. This DM is the delivery leg
    # itself — "your row is back with you" — so after a transfer it must reach
    # the seat that now has to act, never the dead one that wrote it.
    sender = custodian_of(row)
    # RETURNED, NOT SWALLOWED: True means the author's own DM landed, False
    # means it fell back to the lander. A delivery leg that reports nothing
    # can only be tested through its mocks, and an assertion about a mock is
    # an assertion about the test (the vacuous-assertion rung's rule, which
    # fired on this lane's first two drafts and was right both times).
    return _nudge(sender,
                  "VERDICT %s — %s IS BACK WITH YOU (reviewed tip %s). Nothing "
                  "else will tell you: read it with helm dispatch triage %s"
                  % (pol, row.get("lane") or "your lane",
                     str(row.get("reviewed_tip") or "?")[:12], rid),
                  "VERDICT %s on %s (%s) — ITS AUTHOR COULD NOT BE WOKEN "
                  "(sender %s). Route it by hand."
                  % (pol, row.get("lane") or "?", rid, sender or "UNRECORDED"))


def _record_done(row, reviewed, evidence, room, turn, chat):
    """Verify the turn FIRST — exact wire types AND the full verdict binding
    against ledger truth — then append under a lock that RE-READS state:
    already-done returns the standing report idempotently (two stale
    confirmation writers can never both append — round-5 blocker 4); any
    unknown/conflict bails. Returns the state string on durable success,
    None otherwise."""
    if not _is_this_verdicts_turn(turn, row, chat):
        return None                          # wrong/absent binding: never
    text = turn.get("text")
    if not isinstance(text, str):
        return None
    # The wire-value partition is EXACT (round-7): a turn is either the
    # complete signed shape, or the exactly-empty unsigned shape — anything
    # else (falsey wrong types included: 0/False/[]/{} launder through
    # truthiness) is NEEDS CONFIRMATION with zero write.
    def _empty_str(v):
        return type(v) is str and v == ""
    signed_shape = (isinstance(turn.get("turn"), str) and turn.get("turn")
                    and isinstance(turn.get("receipt"), str)
                    and turn.get("receipt")
                    and type(turn.get("chain")) is int)
    unsigned_shape = (_empty_str(turn.get("turn") if "turn" in turn else "")
                      and _empty_str(turn.get("receipt") if "receipt" in turn else "")
                      and turn.get("chain") is None
                      and _empty_str(turn.get("payload") if "payload" in turn else ""))
    if not signed_shape and not unsigned_shape:
        return None                          # outside the partition: refuse
    if signed_shape:
        if not (isinstance(turn.get("payload"), str) and turn["payload"]
                and chat.payload_for(turn) == turn["payload"]):
            return None                      # forged/invalid: no done, ever
        done = {"v": 1, "event": "done", "id": row["id"], "ts": pk.now_ts(),
                "room": room, "binding": _binding_key(row, reviewed, evidence),
                "payload": turn["payload"], "turn": turn["turn"],
                "receipt": turn["receipt"], "chain": turn["chain"],
                "kind": "attested", "text": text}
    else:
        done = {"v": 1, "event": "done", "id": row["id"], "ts": pk.now_ts(),
                "room": room, "binding": _binding_key(row, reviewed, evidence),
                "payload": "", "turn": "", "receipt": "", "chain": None,
                "kind": "cite-tier", "text": text}
    with eventledger.locked(attest_path()) as held:
        if not held:
            return None
        intent, existing, unknown = _attest_state(row, reviewed, evidence)
        if unknown:
            return None
        if existing is not None:
            return _report_from_done(existing, row, reviewed, evidence, chat)
        if intent is None:
            return None                      # state moved under us: bail
        if not eventledger.append_unlocked(attest_path(), done):
            return None
    return _report_from_done(done, row, reviewed, evidence, chat)


def _emit_and_record(row, reviewed, evidence, room):
    try:
        from . import chat
        turn = chat.post(
            "VERDICT %s — lane %s tip %s (dispatch %s)" % (
                evidence, row.get("lane") or "?", reviewed[:12], row["id"]),
            room=room, ambient=True,
            # The signed vlane is the row's STORED lane, byte-for-byte —
            # _report_from_done reconstructs the payload from that same field,
            # so signing any normalized spelling self-invalidates the attest
            # (#142 r2, finding 1). Family joins normalize at read.
            verdict={"lane": row.get("lane"), "tip": reviewed,
                     "rid": row["id"], "ref": evidence})
    except Exception as exc:
        why = "%s: %s" % (exc.__class__.__name__, exc)
        pk.event("dispatch-verdict-announce-failed", row["id"], why)
        # intent stands; the next retry enters the DOUBT cell (never re-signs)
        return "NEEDS CONFIRMATION — attestation turn not delivered (%s)" % why
    state = _record_done(row, reviewed, evidence, room, turn, chat)
    return state or ("NEEDS CONFIRMATION — emitted turn failed verification "
                     "or the done record is unwritable; durable state still "
                     "shows doubt (room %s)" % room)


def _confirm_from_room(row, reviewed, evidence, intent):
    """The DOUBT cell: intent recorded, completion unknown. Upgrade ONLY by
    read-only evidence — the true turn found in the intent's STORED room
    (env changes never redirect the search: finding C). Unreadable
    or absent evidence keeps the doubt; nothing here ever emits or signs."""
    room = intent.get("room") or "main"
    try:
        from . import chat
        msgs, _total = chat.read(room)
    except Exception as exc:
        return ("NEEDS CONFIRMATION — announce in doubt; room %s unreadable "
                "(%s: %s)" % (room, exc.__class__.__name__, exc))
    for r in reversed(msgs):
        if not _is_this_verdicts_turn(r, row, chat):
            continue
        state = _record_done(row, reviewed, evidence, room, r, chat)
        if state:
            return state
    return ("NEEDS CONFIRMATION — announce in doubt (intent recorded for "
            "room %s, completion unknown; will not re-sign)" % room)


def _is_this_verdicts_turn(r, row, chat):
    """The FULL binding must match — vrid alone is spoofable by any signed
    row carrying the field (xrev finding #2). Exact verdict shape,
    no shape overlap, and all four fields equal to the ledger's truth."""
    return (chat.is_verdict(r) and not chat.is_reply(r)
            and not r.get("ack") and not r.get("react")
            and r.get("vrid") == str(row["id"])
            and r.get("vlane") == str(row.get("lane") or "")
            and r.get("vtip") == row.get("reviewed_tip")
            and r.get("vref") == str(row.get("verdict_ref") or ""))


def _reconcile_announce(row):
    """Idempotent-retry truth: report the durable attest state; the doubt
    cell may upgrade read-only; heal-by-emit happens ONLY when no intent
    exists (a verdict recorded before the attest ledger existed, or a crash
    strictly before intent — the one provably sign-free state)."""
    return _announce_verdict(row, row["reviewed_tip"],
                             str(row.get("verdict_ref") or ""))


_RETIRE_EVENT_FIELDS = frozenset((
    "v", "event", "seq", "id", "ts", "retire_reason", "retire_measurement",
    "retire_seat", "retire_note", "retire_proof_version"))


# THE ONE TERMINAL VERB'S LEDGER EVENT. `helm lr close` records exactly one
# `close` event per row, carrying WHICH terminal (`close_reason`) plus the
# per-reason proof fields landreq established. The CLI's fifth reason —
# out-of-scope — deliberately writes a `cancel` event through mark_cancel
# instead (an open row's honest abandonment already has an event kind and a
# replay arm). The delivered-report reason is the other explicit exception: it
# closes an OPEN BUILD with artifact + handoff evidence and claims no verdict.
#
# Old events (`discharge`, `withdraw`, `close-landed`, `cancel`) keep their
# replay arms forever; only new writes emit `close`.
# WHICH FAMILY AUTHORIZED A `carried` CLOSE. Deliberately NOT folded into
# `CLOSE_PROOF_MODES`: that tuple is the BUILD-landed door's vocabulary and
# adding names to it would silently teach that door two words it has no
# derivation for. Same field, two disjoint vocabularies, each validated by
# the arm that owns it.
CARRIAGE_REPLAY = "carriage-replay"
REACHED_TRUNK = "reached-trunk"
CARRIED_WITNESSES = (CARRIAGE_REPLAY, REACHED_TRUNK)

# THE HISTORY VIEW EVERY CARRIAGE WITNESS RUNS UNDER, WHY NO WITNESS ANSWER IS
# STORED ON ITS OWN (helm task/2394, round four), AND HOW THE FOLD THAT CARRIES
# ITS CLOSE MAY BE REUSED (task/2770).
#
# THE COST THAT STARTED THIS, measured on the live 14,444-event 8,166,754-byte
# dispatch ledger. A cold `dispatches.snapshot()` cost 5.384s, of which 2.362s
# was two `carried`-close `carriage_proof` re-derivations (18 git subprocesses,
# one `merge-tree --write-tree` at 1.183s and one `git cherry` at 0.619s), and
# the stop guard's dispatch-ledger rung ended at or past its local reserve on 22
# of 175 recorded ladders, which is the cut that publishes COVERAGE UNKNOWN.
#
# NO VERDICT HERE IS PERSISTED, AND THAT IS A DESIGN RULE RATHER THAN AN
# OMISSION. A durable memo of the reached-trunk verdict is the obvious cure for
# that cost, and it cannot be made correct: git answers about an id through a
# HISTORY VIEW, and `refs/replace`, `info/grafts` and a shallow boundary each
# reinterpret the same immutable ids without changing one of them. So a stored
# answer needs BOTH a semantic generation that refuses every entry written under
# a different interpretation AND a view held immutable across the whole interval
# between the eligibility probe and the witness — and a cache whose answer gates
# a `carried` close pays that in correctness, not in speed. Nothing is stored:
# there is no entry to invalidate and no interval to bind.
#
# WHAT task/2770 CHANGED, AND WHY IT IS NOT THAT MEMO. The WHOLE dispatch fold
# is now a maintained checkpoint (`helm/foldckpt.py`), and a checkpoint holds
# every close this function decided, accepted or refused. It is never consulted
# per verdict: it is reused only after EVERY git answer the fold read is asked
# again — each object and ref expression through one `cat-file --batch-check`
# under this same pinned view, and a fingerprint of the shallow state, the
# replacement refs, the grafts and attributes files, the listed config and the
# git binary — and any difference discards the whole checkpoint for a full
# replay, which derives every verdict here again. The semantic generation the
# paragraph above asks for is that fingerprint. The interval it could not bind
# is now the one between the re-verification and the read's answer, which is
# the interval every live replay already has between two of its own git calls.
#
# THE VERDICT IS RE-DERIVED ON EVERY READ, under a history view this module PINS
# rather than measures:
# `rowworld._scrubbed_env`, the overlay behind every git read in that module,
# disables replacement objects AND points `GIT_GRAFT_FILE` at `os.devnull`. That
# is the view `landreq._object_view` already defines, measured there on git
# 2.53.0, and this module reuses its spelling rather than inventing a third one.
#
# A SHALLOW BOUNDARY IS REFUSED INSTEAD OF PINNED, because it is the one
# rewriter with no bypass: the parent objects behind it are genuinely absent.
# `_carriage_shallow_refusal` answers UNKNOWN there and never affirms, which is
# the same refusal `landreq._is_parentless` makes on the same measurement.
#
# AND THE COST THE CUT LEAVES BEHIND IS MEASURED ON THE FINAL TREE, NOT ASSUMED.
# Timed through `dispatches.snapshot()`, the read the Stop guard's
# dispatch-ledger rung then made (that fold has since moved off every hook path
# into the stop-facts resident) — on THE TREE THIS COMMENT SHIPS IN, identified by the
# property that decides the cost: no witness store is present anywhere under
# `helm/` and both witness families are re-derived. The exact tree of the run is
# recorded on the lane's review row. Against the live
# 14,567-event 8,349,380-byte ledger and the real helm home, read-only, five cold
# processes of one call each: at load average 29.7-30.7 the rung is 3.351s
# median, 52% of the 6.5s admission cost that rung then carried, min 1.578s and max
# 4.452s — so EVERY sample fit the budget on a box near load 30. Two carriage
# witnesses are derived per read, counted at this function's own call.
#
# THE FIGURES THAT MOTIVATED THE CUT ARE AN EARLIER, DIFFERENT COMPARISON —
# store-DISABLED against store-ENABLED arms on the predecessor tree, the
# remembering write replaced by a no-op, author-reported at that time against a
# 14,540-event 8,301,262-byte ledger, five cold processes per arm interleaved so
# box load is common-mode. At load 10.4-10.8 the DISABLED arm was 1.532s median
# against 1.058s enabled, and the two carried rows it witnesses spent 0.795s
# median in the witnesses against 0.374s. At load 9-15 the DISABLED arm was the
# FASTER of the two (4.470s median against 5.425s), and at load 27-51 the ENABLED
# median fit the budget (6.161s under 6.5s) while the DISABLED median did not
# (8.384s), with one enabled run producing a 12.867s TAIL sample rather than a
# failing median. That box load rather than the memo sets the absolute scale is
# the INFERENCE those interleaved observations support, not a controlled
# isolation of load; the cut rests on it together with the correctness class the
# store carried, not on a claim that the store never mattered.
#
# AND THE REPLAY WITNESS'S OWN ANSWER NOW GOES THROUGH THAT SAME DOOR FOR A
# READER THAT DECLARES ONE (`helm/carriageckpt.py`, task/2813), which is this
# paragraph's two conditions met rather than an exception to it. The off-frontier
# census re-derived the replay 141 times per board read, 19.7s, for questions
# the previous read had answered against the same objects — and the bill was
# measured to be irreducible within one projection (no droppable row class, two
# families that partition by the routed classification, a cheap twin that does
# not predict the expensive one, and a disjoint question set from the World
# build's). The SEMANTIC GENERATION is `foldckpt.policy()`, the code this
# process executes. The IMMUTABLE VIEW is re-established on every read by that
# module's own re-verification: one `cat-file --batch-check` per repository over
# every object and ref expression the witness read, plus the fingerprint of the
# git binary, the shallow state, the listed config, the replacement refs and the
# attributes and grafts files. And the trunk OBJECT `_carriage_trunk_sha`
# resolves is part of the KEY, so a land is a different question rather than an
# entry somebody must remember to invalidate. It is served only inside a
# declared region, which no writer opens.
#
# A PER-INVOCATION MEMO WAS REFUSED ON THE SAME MEASUREMENT. The live ledger
# holds two `carried` closes; one stop evaluation asks `carriage_proof` exactly
# twice, once per row, and those two rows bind DIFFERENT tips — so they are two
# different questions and a memo scoped to one read would have held ZERO hits.
# (The instrument that says so counts the calls and their row ids, and it
# recorded two distinct ids, never one id twice.) A mechanism with no measured
# hit is invention rather than a cure.


def _carriage_trunk_sha(gitdir, trunk_ref):
    """The trunk object this proof is ABOUT, resolved ONCE. -> sha or None.

    ITS CALLER RESOLVES IT BEFORE EITHER HALF OF THE PROOF, and that ordering
    is a round-one finding of task/2394: two resolutions of one ref name are two
    questions, so a trunk that moved between them lets one family answer about
    trunk A while the other answers about trunk B and the caller reports a
    single verdict. Resolving once and handing the OBJECT down removes the
    window rather than narrowing it.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    rc, out = rowworld._git(gitdir, "rev-parse", "--verify", "--quiet",
                            str(trunk_ref or "") + "^{commit}")
    sha = out.strip().lower() if rc == 0 else ""
    return sha if _FULL_TIP.fullmatch(sha) else None


def _carriage_shallow_refusal(gitdir):
    """Why this repository cannot witness carriage at all. -> reason or None

    A SHALLOW BOUNDARY IS THE ONE HISTORY-VIEW REWRITER WITH NO BYPASS, and
    that is why it is a REFUSAL rather than something the read pins off.
    Replacement objects and a legacy grafts file are both disabled at every
    read (`rowworld._scrubbed_env`, the view `landreq._object_view` defines),
    so under them git reads the object an id names. A `.git/shallow` boundary
    is different in kind: the parent objects behind it are genuinely ABSENT, so
    no variable and no flag restores the range `git cherry` would have walked,
    and a truncated range that comes back uniformly `-` is an artifact of the
    truncation rather than a patch-identity match. `landreq._is_parentless`
    refuses outright for this reason on the same measurement.

    UNKNOWN, NEVER A NEGATIVE AND NEVER AN AFFIRMATION. The caller returns
    `(None, reason)`, which `_close_event_error`'s `carried` arm reads as trunk
    no longer affirming the work — the fail-safe direction, because a close
    whose proof cannot be re-derived goes INERT rather than being accepted.

    AND AN UNREADABLE ANSWER IS A REFUSAL TOO: a git that cannot say whether it
    is shallow is exactly the case where this proof must not speak.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    rc, out = rowworld._git(gitdir, "rev-parse", "--is-shallow-repository")
    shallow = out.strip() if rc == 0 else ""
    if shallow == "false":
        return None
    if shallow == "true":
        return ("%s is SHALLOW: its boundary truncates every traversal and the "
                "parent objects behind it are absent, so no flag restores the "
                "range this witness would have walked" % gitdir)
    return ("git did not say whether %s is shallow (%r), and a history view "
            "that cannot be read is not one a proof may run in"
            % (gitdir, shallow))


def _carriage_replay_witness(gitdir, base, tip, trunk_ref, trunk_sha):
    """WITNESS FAMILY 1, DERIVED LIVE ON EVERY READ. -> (answer, detail)

    `(None, prose)` when this family has nothing to say, and that prose is the
    sentence the second family quotes inside its own refusal.

    IT RUNS FIRST AND IT RUNS EVERY TIME, and the ordering is the contract.
    `rowworld._carriage` reaches `merge-tree --write-tree` and asks about trunk
    HEAD's CONTENT, which is the stronger question; the second family asks about
    trunk's HISTORY. The fallthrough is one-directional — only SILENCE here may
    reach it — because a measured True or False from the stronger question must
    not be second-guessed by a weaker one.

    AND ITS ANSWER IS NOT A FUNCTION OF THE IDS IT WAS HANDED, which is why
    nothing in this module has ever been allowed to persist it: git resolves
    content through its MERGE MACHINERY, so a merge driver (an external
    program), an attributes file (content behind a pathname) and repeated config
    records (resolved by last-value precedence) all move this answer while every
    id stands still. No answer of this witness is stored on its own; the fold
    checkpoint that carries the close it decided is reused only after the
    config, the attributes files and the git binary are fingerprinted again and
    match — see the note above `_carriage_trunk_sha`.

    AND THE SAME DOOR NOW CARRIES THE ANSWER ACROSS PROJECTIONS, for the reader
    that declared one (`carriageckpt.replayed`). A census re-derived this
    relation 141 times per board read for questions the previous read had
    already answered against the same objects; inside a declared region the
    answer is served only after that module re-asks git every question this
    witness put to it, under the same fingerprint, and a trunk that moved is a
    different key rather than a stale entry. Outside a region — the ladder that
    authorizes a real close, and the writer that re-derives one under the lock
    — this is byte-identical to the derivation below.
    """
    if not (base and tip):
        return None, "no immutable (base, tip) to replay from"
    from . import rowworld              # DEFERRED — rowworld imports us.
    answer = carriageckpt.replayed(
        gitdir, base, tip, trunk_sha,
        lambda: rowworld._carriage(gitdir, base, tip, trunk_sha or trunk_ref))
    if answer is not None:
        return answer, {"gitdir": gitdir, "trunk_ref": trunk_ref,
                        "trunk_sha": trunk_sha, "base": base, "tip": tip,
                        "witness": CARRIAGE_REPLAY}
    return None, ("no content witness at %s could see %s..%s"
                  % (trunk_ref, base[:12], tip[:12]))


def _reached_by_ancestry(gitdir, tip, trunk):
    """Is `tip` literally reachable from `trunk`? -> True/False

    FOR THE SENTENCE ONLY, NEVER FOR THE GATE: `rowworld._reached_trunk`
    answers `(None, None)` for an ancestor and for five other conditions
    alike, so this re-asks the one cheap probe and leaves that contract alone.
    FALSE ON AN UNREADABLE PROBE, deliberately: a failed read falls back to
    the older, weaker sentence and never manufactures a claim of ancestry.
    THAT IS WHY THIS ONE MUST NOT AUTHORIZE ANYTHING -- see
    `_ancestry_authorizes`, the tri-state sibling a gate uses, where an
    unreadable probe takes its own value instead of reading as a measured no.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    sha = rowworld._sha(tip)
    if not sha or not trunk:
        return False
    rc, _out = rowworld._git(gitdir, "merge-base", "--is-ancestor",
                             sha, str(trunk))
    return rc == 0


def _work_tip_of(row, rows):
    """The one tip `carriage_proof` would measure for this row: the chain's
    work pair tip, else the reviewed tip, else the sha the row was filed at.
    -> full sha or "" """
    from . import rowworld              # DEFERRED — rowworld imports us.
    _base, tip = rowworld._work_pair(row, rows, _CarrierView(rows))
    tip = tip or rowworld._sha((row or {}).get("reviewed_tip")) \
        or rowworld._sha((row or {}).get("tip"))
    return str(tip or "")


def _ancestry_authorizes(gitdir, tip, trunk):
    """Is `tip` literally reachable from `trunk`? -> True / False / None

    THE GATE-GRADE SIBLING OF `_reached_by_ancestry`, and the whole
    difference is the THIRD ANSWER. That one collapses an unreadable probe
    to False on purpose and says so, because it only picks a refusal's
    WORDING -- a failed read there costs a weaker sentence and nothing else.
    This one AUTHORIZES, so an unreadable probe must never be spelled the
    same as a measured no: `unreadable` and `empty` cannot share a value,
    or a git that could not run would silently read as "the work is not on
    trunk" and refuse a close that is perfectly good.

    None therefore means THE QUESTION COULD NOT BE ASKED, and the caller
    refuses naming that rather than guessing in either direction. The two
    functions are deliberately not merged: one may lie towards the weaker
    sentence and the other may not lie at all.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    sha = rowworld._sha(tip)
    if not sha or not trunk:
        return None
    rc, out = rowworld._git(gitdir, "rev-parse", "--verify", "--quiet",
                            sha + "^{commit}")
    if rc != 0 or not rowworld._sha(out.strip()):
        return None
    rc, _out = rowworld._git(gitdir, "merge-base", "--is-ancestor",
                             sha, str(trunk))
    # git answers 0 for yes and 1 for no; ANYTHING ELSE IS THE PROBE FAILING,
    # not an answer about history, and it takes the third value.
    return True if rc == 0 else (False if rc == 1 else None)


def _carriage_reached_witness(gitdir, work_tip, trunk_ref, trunk_sha,
                              replay_gap):
    """WITNESS FAMILY 2, ANCESTRY AND PATCH IDENTITY. -> (answer, detail)

    A FUNCTION OF THE COMMIT IDS IT IS HANDED, UNDER A HISTORY VIEW THE READ
    PINS. `git cherry` and `rev-parse` reach no merge policy at all — but git
    interprets an id through a VIEW rather than in a vacuum, and three
    mechanisms rewrite that view without changing one id:

    1. REPLACEMENT IS DISABLED AT EVERY READ. `rowworld._scrubbed_env` sets
       `GIT_NO_REPLACE_OBJECTS`, so a `refs/replace/<oid>` entry cannot
       substitute a different object for one the ids name. Without it,
       replacing the landed twin whose patch identity is the whole of the match
       turns a uniformly `-` range into `+` with every id unchanged.
    2. GRAFTS ARE DISABLED AT EVERY READ. The same overlay points
       `GIT_GRAFT_FILE` at `os.devnull` — the spelling `landreq._object_view`
       established and measured, because `--no-replace-objects` does NOT cover
       a legacy grafts file and a graft rewrites parentage outright.
    3. A SHALLOW BOUNDARY IS REFUSED, not pinned. There is no bypass: the
       parent objects are genuinely absent. `carriage_proof` asks
       `_carriage_shallow_refusal` before either family runs and answers
       UNKNOWN there.

    THE GIT READS TAKE THE RESOLVED SHA, THE PROSE TAKES THE REF (round-one
    finding 1). `trunk_sha` is the object this measurement is about; `trunk_ref`
    is what the operator called it and appears only in the sentences and in the
    detail beside the sha. An unresolvable ref keeps the old behaviour — the
    witness runs against the name and answers silence.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    reached, unmatched = rowworld._reached_trunk(gitdir, work_tip,
                                                 trunk_sha or trunk_ref)
    if reached is True:
        return True, {"gitdir": gitdir, "trunk_ref": trunk_ref,
                      "trunk_sha": trunk_sha, "base": None, "tip": work_tip,
                      "witness": REACHED_TRUNK}
    if unmatched:
        # NAMED, AND NOT CALLED ABSENT. `git cherry` marks the right side of
        # a symmetric difference, so a lane that merged trunk after its own
        # work landed loses the twin it would have matched against and reads
        # `+` for commits trunk demonstrably has. The refusal stands either
        # way — an unmatched commit is never affirmed — but the sentence says
        # what was measured instead of what would have been convenient.
        return None, ("%s could not be matched onto %s by patch identity "
                      "(%s) — either it never landed, or this tip has merged "
                      "%s since, which leaves `git cherry` nothing upstream "
                      "to match against"
                      % ("%d commit(s) up to %s" % (len(unmatched),
                                                    work_tip[:12]),
                         trunk_ref,
                         ", ".join(sha[:12] for sha in unmatched[:4]),
                         trunk_ref))
    # PATCH IDENTITY IS NOT ALWAYS ASKED, SO IT MUST NOT ALWAYS BE BLAMED.
    # `_reached_trunk` short-circuits on ancestry and never runs `git cherry`
    # for an ancestor, so nothing unmatched here has two OPPOSITE causes. A
    # row's `tip` is its filing sha, so an ancestor tip is the ordinary case.
    # THE GATE IS UNCHANGED: only the sentence learns which question went
    # unanswered.
    if _reached_by_ancestry(gitdir, work_tip, trunk_sha or trunk_ref):
        return None, ("%s, and %s is ALREADY history of %s — `git cherry` "
                      "was never asked, because an ancestor leaves it an "
                      "empty range and an empty range affirms every possible "
                      "trunk. This witness cannot speak for this tip; it is "
                      "NOT a finding that the work did not land"
                      % (replay_gap, work_tip[:12], trunk_ref))
    return None, ("%s, and %s did not reach %s by patch identity either"
                  % (replay_gap, work_tip[:12], trunk_ref))


def carriage_proof(row, rows, carriers, gitdir, trunk_ref):
    """Is this row's work CARRIED AT TRUNK HEAD RIGHT NOW?
    -> (True/False/None, detail)

    ONE derivation, three callers — the `discharging_row` shape, and for the
    same reason: the ladder authorizes with it, the writer re-derives it under
    the lock, and replay re-derives it again, so a forged `close` event is
    INERT rather than merely unlikely. A proof each caller computes its own
    way is a proof the weakest caller defines.

    IT DELEGATES AND DOES NOT REIMPLEMENT. `rowworld` owns the relation —
    replay the work onto HEAD from its chain-bound base and ask whether the
    result IS HEAD — and this module must not grow a second, weaker answer to
    the same question. The import is DEFERRED because `rowworld` imports this
    module at load time; a module-level import here would be the cycle.

    THE TRI-STATE SURVIVES THE TRIP, deliberately. True affirms. False is a
    MEASURED refusal against a POSITIVE anti-carriage artifact — which the
    relation does not currently mint, so in practice a non-affirming answer
    arrives as None. None means NO WITNESS AFFIRMED, which includes both "the
    question could not be asked" and "no witness could see it". A caller that
    reads either as "absent" converts unmeasurable into gone, which is the
    whole bug class this verb exists inside — and the one that cost 98 of 105
    known-landed rows a false FALSE before the revised ruling. `detail` names the (base, tip) the
    answer was computed from so a close can record its own re-derivation.

    TWO WITNESS FAMILIES, TRIED IN STRENGTH ORDER, and the second one is why
    this door had a floor above zero for weeks (task/close-ladder). The
    replay family asks about trunk HEAD's CONTENT and needs an immutable
    (base, tip); the REACHED-TRUNK family asks about trunk's HISTORY and
    needs only a tip. Neither implies the other and the fallthrough is
    one-directional: only SILENCE from the replay family reaches the second,
    because a measured True or False from the stronger question is the
    answer and must not be second-guessed by a weaker one. BOTH families are
    derived on every FOLD and NEITHER answer is ever stored on its own — see
    the note above `_carriage_trunk_sha` for why a memo of either one cannot be
    made correct, and for the fold checkpoint that reuses the close only after
    re-asking git every question this function asked.

    WHY A SECOND FAMILY RATHER THAN A LOOSER FIRST ONE. Both content
    witnesses go quiet the moment trunk edits the same files again, which is
    every land older than about an hour on a busy lane. And `_work_pair`
    correctly returns no pair at all for a `--new-work` review row — it is
    its own chain root, so there IS no base to replay from and none can be
    invented. Loosening either witness would have bought those rows with a
    proof that no longer discriminated; `git cherry` buys them with a
    STRONGER git proof than `landed` already accepts (a uniformly `-` range,
    against `landed`'s single-commit patch id).

    `detail["witness"]` NAMES WHICH FAMILY ANSWERED, and the close records
    it. Relief that cannot say which proof carried it records a measurement
    nobody can re-run — the same law the landed doors were held to."""
    from . import rowworld              # DEFERRED — rowworld imports us.
    base, tip = rowworld._work_pair(row, rows, carriers)
    work_tip = tip or rowworld._sha((row or {}).get("reviewed_tip"))
    if not work_tip:
        # THE SHA THE ROW WAS FILED AT IS CHAIN-BOUND TOO (task/2090). A
        # review row awaiting its FIRST verdict has no `reviewed_tip` — that
        # field is populated by the verdict alone — and no carrier, so the
        # rung above refused it "bound to no immutable tip" while its own
        # `tip`, the immutable sha the dispatch itself bound, sat beside the
        # refusal unread. That is not a recomputed base, which is the vacuity
        # carriage-replay-base-must-be-chain-bound forbids: it is the
        # chain-bound dispatched sha itself, exactly the anchor that premise
        # names. It reaches only the reached-trunk family below — there is
        # still no immutable base, so nothing can be replayed — which is
        # also the family that answers "did this work reach trunk" without
        # needing one. A row with neither field still refuses, unchanged.
        work_tip = rowworld._sha((row or {}).get("tip"))
    if not work_tip:
        return None, ("this row is bound to no immutable tip: neither its "
                      "own reviewed tip, a carrier's, nor the sha it was "
                      "filed at, and a proof needs something immutable to "
                      "be about")
    rc, _out = rowworld._git(gitdir, "cat-file", "-e",
                             work_tip + "^{commit}")
    if rc != 0:
        return None, ("the tip object %s is not in %s — carriage cannot "
                      "be measured across repositories, and this is the "
                      "gap rather than a reason to weaken the proof"
                      % (work_tip[:12], gitdir))
    if base and tip:
        rc, _out = rowworld._git(gitdir, "cat-file", "-e",
                                 base + "^{commit}")
        if rc != 0:
            return None, ("the base object %s is not in %s — carriage "
                          "cannot be measured across repositories, and this "
                          "is the gap rather than a reason to weaken the "
                          "proof" % (base[:12], gitdir))
    # ONE RESOLUTION FEEDS BOTH FAMILIES, and it happens HERE rather than
    # inside either of them: the sha below is what every git read behind the
    # witnesses is handed, so there is no window in which trunk can move
    # between the question and the answer (round-one finding 1).
    trunk_sha = _carriage_trunk_sha(gitdir, trunk_ref)
    # AND A SHALLOW REPOSITORY IS REFUSED BEFORE EITHER FAMILY RUNS. Replacement
    # and grafts are pinned off at every read; a shallow boundary cannot be,
    # because the parent objects behind it are absent, so it is the one history
    # view this proof declines to run in at all.
    shallow = _carriage_shallow_refusal(gitdir)
    if shallow:
        return None, shallow
    answer, detail = _carriage_replay_witness(gitdir, base, tip, trunk_ref,
                                             trunk_sha)
    if answer is not None:
        return answer, detail
    # AND THE SECOND FAMILY IS DERIVED TOO. No answer of it is stored on its
    # own, which is what leaves no entry to invalidate; the fold checkpoint
    # that carries the close re-asks every git question below before it is
    # reused (see the note above `_carriage_trunk_sha`).
    return _carriage_reached_witness(gitdir, work_tip, trunk_ref, trunk_sha,
                                     detail)


# THE WRITER'S REGISTER IS NOT THE CLI'S, AND THAT IS THE TRAP. A reason can be
# spelled correctly in landreq.CLOSE_CLI_REASONS, documented, rendered in every
# usage string and exercised by a full ladder of arms, and STILL be refused
# here — because the ladder decides WHETHER a close is provable and this tuple
# decides whether the write is admissible at all. `chain-proof` shipped in that
# state: eleven reasons on the CLI, nine accepted by the writer, and the gap
# invisible to every test that stopped at the ladder.
#
# `out-of-scope` IS ABSENT ON PURPOSE and must stay absent: it cancels a moot
# row through the CANCEL boundary and never reaches this writer, so adding it
# here would admit a write that has no proof path. The parity arm encodes that
# one exception by name rather than letting the next reader guess which
# omissions are deliberate.
CLOSE_REASONS = ("landed", "superseded", "withdrawn", "stranded",
                 "discharged",
                 "subsumed", "delivered-report", "resolved", "carried",
                 "chain-proof", "expired", "endorsement-moot")
# ---------------------------------------------------------------------------
# ADMINISTRATIVE RETIREMENT — a terminal that is NOT a close.
#
# It is deliberately NOT a `close --reason`: every member of CLOSE_REASONS
# above says something about the WORK, and the close writer's per-reason
# schemas, idempotence identities and proof fields are all built to bind such
# a claim. Retirement binds the opposite kind of fact — that the row's proof
# chain cannot be reached at all — so borrowing the close vocabulary would
# put a claim about the work into a record that never measured one. Its own
# event kind is what keeps `helm lr list`, the board and every landed/closed
# consumer able to count it separately and never as work done.
#
# THE REASON CODES ARE THE MEASUREMENTS, not opinions: landreq owns each
# probe and this boundary RE-RUNS it under the ledger lock (the abandon
# shape), so a caller cannot assert unreachability that is not true at the
# moment of the write.
RETIRE_REASONS = (
    # an APPROVE exists, its reviewer's approval tier is measurably DARK (no
    # proxywatch runtime proof was EVER recorded for that seat) and the seat
    # holds no current roster session and no live pane
    "tier-unevaluable-parked",
    # the declared succession carrier's proof cannot be read
    "succession-unreadable",
    # the seat this row is owed by resolves to no roster row
    "author-unresolvable",
    # Git for the row's own repository cannot be read
    "repo-unreadable",
    # the row's OWN reviewed proof object no longer exists in its repository,
    # is not translatable by the recorded rewrite sidecar, and is therefore
    # one of the ids `lr refs` reports unresolvable. NOTHING can be
    # re-adjudicated over a commit that is gone: not the reviewer who
    # attested it, not the author who offered it.
    #
    # IT IS A RETIREMENT AND NOT `close --reason stranded` / `lr abandon`,
    # and the whole difference is one surviving ref. Both of those claim the
    # WORK is gone, so both veto on a live lane-family ref — correctly, and
    # that veto is exactly what leaves this population doorless: on the live
    # board every such row carries a surviving refs/heads/,
    # refs/tags/archive/ or refs/helm-retired/ lane ref.
    # Retirement claims nothing about the work, so a surviving ref cannot
    # contradict it; the measurement DISCLOSES those ref names instead of
    # blocking on them, and the work stays exactly as available to a fresh
    # review as it was before.
    "reviewed-object-destroyed",
    # a FIX-verdicted row whose author is absent from every roster surface,
    # whose bound lane branch has not moved for the author-silence window,
    # and whose chain has nothing left on the board. The branch and its tip
    # are RETAINED and named in the measurement; the reviewer's verdict
    # discharged its move, so the single-id belt does not ask about it.
    "author-absent-lane-idle",
)
_RETIRE_PROOF_V = 1
_RETIRE_MEASUREMENT_CAP = 1024
_RETIRE_NOTE_CAP = 256
# EVERY status a live obligation can bill from. `verdict` is the held/contrary
# half, `open` and `held` the pre-verdict half. `closed` and `cancelled` are
# excluded because they are already terminal — a row cannot be retired twice,
# and `_close_retired_by` catches the annotation-shaped terminals beside them.
_RETIRABLE_STATUSES = ("open", "held", "verdict")
CLOSE_PROOF_MODES = ("ancestor", "patch-equivalent",
                     "translated-ancestor", "translated-patch-equivalent",
                     # THE FOURTH RUNG (task/777). Without this name the
                     # writer REFUSES a content-equivalent close outright —
                     # and a dry run never learns, because a dry run appends
                     # nothing and so never reaches this validator. The door
                     # measured "WOULD close" for hours while a real write
                     # could not have bound.
                     "content-equivalent")
WITHDRAWN_PROOF_MODES = ("absent", "translated-absent")
# DELIVERY CLASS — the LIVE step of a landing, recorded because trunk and the
# RUNNING FLEET are different facts (premise land-to-live-compression-owner-
# directive). "cli": every helm invocation is a fresh process off main, so the
# change is live AT LAND. "process": beacons, the web service, proxies,
# daemons and anything a long-running seat holds in memory keep executing the
# code they loaded at start, so until each re-arms the running fleet holds
# PRE-LAND code. `close_delivery_restart` names what still does. Nothing here
# restarts anything and nothing here marks the debt paid: `helm rearm` is the
# verb that measures live processes against HEAD, and this ledger field is the
# per-row DECLARATION that the debt was ever taken on.
CLOSE_DELIVERY_CLASSES = ("cli", "process")
# Which verdict polarities each reason may terminate. landed excludes
# FIX/SUPERSEDE (a contrary on trunk is a CONTRARY, not a resolution);
# withdrawn is the do-not-land debt — own-declared, or UNDECLARED (None) with
# the declaration living on a CHAINED round (landreq._chain_polarity gates
# that at the writer; a single-event replay cannot walk the chain, so None is
# admissible here exactly as superseded/stranded already admit it);
# superseded and stranded admit any polarity including historical UNDECLARED;
# subsumed admits approved work plus FIX debt whose later confirmation explicitly
# records that the findings were answered; SUPERSEDE remains a different terminal.
_CLOSE_POLARITY = {
    "landed": ("approve", None),
    # `carried` ADMITS EVERY POLARITY, and that is the point of it (task/756).
    # The rows it exists for are precisely the ones every other door refuses:
    # a READY row whose lander was never recorded, and a FIX-verdicted row
    # whose work reached trunk anyway. Polarity cannot gate a reason whose
    # entire claim is a measured fact about trunk HEAD — the gate is the
    # carriage proof, which no polarity can forge.
    # ...EXCEPT `concur`, which is absent here on purpose. `concur` is
    # outside WORK_POLARITIES by construction so that endorsement has a word
    # promising no landing, and no door that LANDS OR SETTLES work admits it —
    # a law with its own arm (tests/test_verdicts.py
    # ConcurAuthorizesNothingTest). THE SCOPE IS THE LANDING AND SETTLEMENT
    # DOORS, NEVER EVERY DOOR: the three NONAUTHORIZING doors below admit
    # concur, and a reader who takes the law for the wider claim concludes
    # that a landed concur has no terminal anywhere — which is exactly the
    # state this table was in.
    #
    # AND THE EXCEPTION WOULD BUY NOTHING HERE, which is the stronger reason
    # than consistency. This door replays the row's chain-bound work onto
    # trunk HEAD and asks whether the result IS HEAD; work that already landed
    # cannot answer that, so the population a widened tuple would be for is
    # refused one rung EARLIER, before polarity is ever consulted. Its door is
    # `endorsement-moot`, whose proof is the ancestry this one does not ask.
    "carried": ("approve", "fix", "supersede", None),
    # THE TARGET'S OWN POLARITY CANNOT GATE THIS REASON, for the same
    # argument the block above makes for `carried`: the claim is a measured
    # fact about the CHAIN — an authorizing descendant carried this row's
    # obligations — and no polarity on the target can forge or forbid it.
    # `concur` stays absent here too, and for this door the exception is
    # likewise unreachable rather than merely unwanted: `chain-proof` requires
    # the reviewed OBJECT to be PRUNED, and a landed concur's object reads.
    "chain-proof": ("approve", "fix", "supersede", None),
    "superseded": ("approve", "fix", "supersede", None),
    # WITHDRAWN TAKES APPROVE, BECAUSE THE QUESTION IS ABOUT THE WORK AND NOT
    # ABOUT THE VERDICT'S SIGN. An APPROVE whose lane conflicts with trunk and
    # will never land is exactly as stuck as a FIX, and admitting only
    # FIX/SUPERSEDE leaves those rows with no terminal at all — measured, 18
    # withdrawable approvals across 400 open rows carrying a reviewed tip.
    # The invariant is unchanged and lives in landreq's absence proof: Git
    # must prove the reviewed change OFF TRUNK BY ANCESTRY and ABSENT BY
    # PATCH-ID. A landed row cannot be withdrawn whatever its polarity says.
    #
    # CONCUR IS ADMITTED HERE AND NOWHERE ELSE, and the exception is exact.
    # "Concur authorizes nothing" is a law about AUTHORIZATION: an endorsement
    # must not permit a land or settle work. Retiring a row whose work is
    # PROVABLY ABSENT FROM TRUNK permits nothing and settles nothing — it
    # records that work will not land, which is the opposite of authorizing
    # it. So withdrawn is the one close door concur opens; every LANDING and
    # SETTLEMENT door still refuses it, and the invariant's own test now
    # enumerates those by name rather than saying "no close door".
    #
    # AN UNDECLARED POLARITY STAYS OUT. It keeps the refusal below, whose
    # negative control is named "the gate did not loosen"; the census measured
    # zero withdrawable undeclared rows, so admitting them would loosen a
    # guard for no population.
    "withdrawn": ("approve", "concur", "fix", "supersede", None),
    "stranded": ("approve", "fix", "supersede", None),
    # `expired` IS THE SECOND ABSENCE DOOR, AND IT ADMITS CONCUR FOR EXACTLY
    # WITHDRAWN'S REASON. The law is that concur authorizes nothing: an
    # endorsement must not permit a land or settle work. This door permits no
    # land and settles nothing — it records that a review nobody can act on
    # will not produce one, over work Git proves is absent from trunk. That is
    # the opposite of authorizing, which is the same sentence `withdrawn`
    # carries above and the reason the law is stated about LANDING AND
    # SETTLEMENT doors rather than about every door.
    #
    # IT IS TIGHTER THAN EVERY SIBLING, not looser. Two polarities only, and
    # they are the two that authorize nothing: `concur`, and an `approve`
    # whose authorization tier was never evidenced at record time. FIX and
    # SUPERSEDE are REAL DEBT — somebody is owed a cure — and an UNDECLARED
    # polarity recorded no review at all; the predicate refuses all three as
    # `unbillable` before this table is consulted, and listing them here would
    # let a future caller reach the writer around it.
    #
    # AND THE BOUNDARY IS STRONGER THAN WITHDRAWN'S. Withdrawn proves absence
    # in its ladder; this door refuses any row whose land state is not a
    # MEASURED ABSENT, so a LANDED concur and an UNMEASURABLE one are both
    # refused before polarity is ever reached.
    "expired": ("approve", "concur"),
    # `endorsement-moot` IS THE THIRD NONAUTHORIZING DOOR AND THE ONLY ONE
    # THAT PROVES PRESENCE. Read it against the three reasons above it in this
    # table and the hole it fills is a MISSING CELL, never a new exception.
    # One git fact — THIS ROW'S OWN REVIEWED TIP IS ON TRUNK — is asked by four
    # doors, one per verdict shape:
    #     APPROVE, authorizing        -> `landed`
    #     FIX/SUPERSEDE, contrary     -> `resolved`
    #     UNDECLARED, no verdict      -> `discharged`
    #     CONCUR, nonauthorizing      -> THIS DOOR, and there was none
    # Every other cell got its OWN reason rather than a widened `landed`,
    # twice, and for the same stated argument each time: LANDED is a claim
    # about THIS row's work at HEAD under THIS row's authority. So the fourth
    # cell is owed a reason too, and folding it into `landed` would be the one
    # move the other three cells' authors each refused.
    #
    # AND THE LAW IS WHAT ADMITS IT, not an exception carved out of it.
    # "Concur authorizes nothing" has a permissive consequence beside its
    # prohibitive one, and only the prohibitive half had been read. If the work
    # is ON TRUNK and this row authorized nothing, then THIS ROW DID NOT PUT IT
    # THERE — the landing necessarily holds some other authority. Retiring the
    # row against that measurement therefore grants nothing and transfers
    # nothing; it records where the authority was NOT. `withdrawn` and
    # `expired` are the mirror image over a measured ABSENCE, and between the
    # three every MEASURED land state a concur row can be in has a terminal.
    # What stays refused is every door that would make the endorsement itself
    # the authority, which is the whole list `LANDING_AND_SETTLEMENT` names.
    #
    # IT SETTLES NOTHING BECAUSE THERE IS NOTHING TO SETTLE. A settlement door
    # records that an obligation was discharged; a concur bills nobody, so this
    # row is paperwork and never a debt. That is why it may take a door the
    # settlement reasons must refuse.
    #
    # ONE POLARITY, WHICH IS TIGHTER THAN EVERY SIBLING. `expired` admits a
    # pre-tier APPROVE beside concur; this door must not, because a pre-tier
    # approve over landed work ALREADY HAS `landed` — its cell in the table
    # above is filled. Admitting it here would give an approve a second,
    # weaker landing door, so the ladder gates on the hold kind `advisory`
    # alone and this tuple says the same thing one layer up.
    "endorsement-moot": ("concur",),
    "subsumed": ("approve", "fix"),
    # THE POLARITY-WRONG DOMAIN — the sibling of #177's polarity-LESS one
    # below. These rows HAVE a polarity and it is the wrong one: a FIX or
    # SUPERSEDE verdict whose own reviewed tip then reached trunk. `landed`
    # refuses them (a non-approve tip on trunk is a CONTRARY, not a
    # resolution), `superseded` refuses (a verdict tip cannot supersede
    # itself), `subsumed` refuses (the original is not absent), `discharged`
    # refuses (they are verdicted) — and `withdrawn` would be a LIE, because
    # it proves ABSENCE and this work is demonstrably present. Five rows sat
    # doorless on 2026-08-04. The discriminator against withdrawn is
    # land_state: ABSENT is withdrawn's population, LANDED is this one's.
    "resolved": ("fix", "supersede"),
    # #177 — THE POLARITY-LESS DOMAIN. Every reason above keys on the row's
    # OWN verdict polarity; a build row whose successor was minted first has
    # none and sat doorless while its work ran on trunk (five refusals across
    # four verbs, one night; one row billed the fleet's most loaded reviewer
    # 227 minutes for landed work). discharged admits EXACTLY that row — a
    # verdicted row keeps its own proof-bearing doors, and the authority here
    # is never the row's own anything: it is the discharging row's landed,
    # gate-verified APPROVE, discovered by discharging_row and recorded on
    # the event so the replay arm re-walks the same ledger.
    "discharged": (None,),
}
# THE CONTRADICTED-WITHDRAWAL DISCHARGE PROOF (the lifecycle clause in
# landreq._lr): a row retired withdrawn whose FIX/SUPERSEDE-verdicted change
# later LANDED is re-exposed CONTRARY, and its discharging close must CAPTURE
# what the door (`landreq._retirement_stands`) re-derived live — which
# withdrawn-class retirement stood, whose declaration carried the contrary
# polarity, which `landed_ever` leg proved the land (ancestry vs patch
# identity), and the pinned trunk it was measured on — so REPLAY admits the
# event from these fields without probing git, exactly as discharged/resolved
# already record their writer-side proofs. OPTIONAL BY CONSTRUCTION like the
# content pair above: an ordinary superseded/resolved close carries none of
# them; a discharging close over a retired row carries ALL of them or is
# refused. subsumed is deliberately ABSENT from the reasons: it proves the
# original's ABSENCE from trunk, and the contradiction proves PRESENCE, so
# one row can never honestly satisfy both.
#
# TWO PROVENANCE HALVES, BOUND AT DIFFERENT MOMENTS ON PURPOSE. The GIT legs
# (land leg + trunk pin) are the door's, probed seconds before the lock like
# every close proof. The STATE legs — retirement class, polarity, its
# provenance (`"own"` or the LIST of declaring dispatch ids), and the
# same-code tip set the declarations were matched against — are REBOUND under
# the ledger lock (`_rebind_contradiction`) from the locked snapshot, because
# a chain verdict can land between the door's read and the write and the
# event must record the truth it bound (the measured-early-used-late
# law). Replay then RE-WALKS the captured provenance against its own ledger
# read (`_chain_declarers`) instead of trusting a bare string.
_CONTRADICTION_PROOF_FIELDS = (
    "contradicted_retirement", "contradiction_polarity",
    "contradiction_polarity_via", "contradiction_land_leg",
    "contradiction_trunk_ref", "contradiction_trunk_sha",
    "contradiction_same_code")
_CONTRADICTION_DISCHARGE_REASONS = ("superseded", "resolved")
_ALREADY_RETIRED = "already retired"
# The state fields one applied `close` event contributes, per reason. ONE
# table drives both the replay arm and the writer's post-append projection so
# a field added later cannot be honoured in one and forgotten in the other.
_CLOSE_STATE_FIELDS = {
    "landed": ("closing_repo_id", "closing_trunk_ref", "closing_trunk_sha",
               "close_proof_mode", "translated_tip", "close_proof_version",
               "close_delivery_class", "close_delivery_restart",
               "landing_review_id", "landing_review_tip",
               "landing_review_verdict_anchor", "landing_review_tier_state",
               "landing_review_gate_requirement", "landing_review_gate",
               "landing_review_approval_anchor",
               # THE CONTENT RUNG'S PROOF IS ONLY AS DURABLE AS THIS TUPLE.
               # _record_close_proven copies ONLY keys admitted here, so a
               # witness computed, passed and never listed is dropped in
               # silence — proof_mode=content-equivalent would close with
               # nothing for replay to re-check (task/777).
               "content_witness", "content_witness_anchor",
               "compose_land_proof", "compose_land_anchor"),
    "superseded": ("superseding_tip", "superseding_id", "close_contrary_state",
                   "close_contrary_target", "closing_trunk_ref",
                   "closing_trunk_sha", "close_proof_version",
                   # #101 — present ONLY on a translated-object supersession;
                   # the validator below refuses them on any other shape.
                   "close_proof_mode", "translated_tip")
                  + _CONTRADICTION_PROOF_FIELDS,
    "withdrawn": ("absence_trunk_ref", "absence_trunk_sha",
                  "close_proof_mode", "translated_tip",
                  "withdrawing_seat", "close_proof_version"),
    "stranded": ("closing_repo_id", "control_sha", "close_proof_mode",
                 "close_proof_version"),
    # `expired` RECORDS THE SAME TRIO AS `stranded` because its ladder proves
    # the same shape: the repository it asked, the trunk object it proved that
    # repository could read (the mass-termination control), and which
    # predicate admitted the row. The predicate's own line rides as the close
    # EVIDENCE rather than a field here — it is per-row prose derived from the
    # measurement, and this table is for the measurement's inputs.
    #
    # AND THIS WAS THE SIXTH REGISTER THIS ONE REASON OWED. It was spelled
    # correctly in CLOSE_CLI_REASONS, CLOSE_REASONS, the CLI help, the usage
    # string, the docs synopsis and rowstate._CLOSE_TERMINAL, and the close
    # would still have persisted NOTHING — `_record_close_proven` copies only
    # keys admitted here — because every arm written for it was a DRY RUN, the
    # exact hole `chain-proof` names one entry below as its own.
    # `close_hold_kind` IS THE AUTHORIZATION HALF OF THIS PROOF and it is
    # STAMPED BY THE LOCK, never by the caller. The trio above proves the tip
    # was absent; without this field nothing in the record says the verdict
    # AUTHORIZED NOTHING, which is the other half of what the door claims — so
    # a stamped authorizing approve and a pre-tier one were indistinguishable
    # at the writer and at replay, and only the unlocked ladder had ever asked.
    "expired": ("closing_repo_id", "control_sha", "close_proof_mode",
                "close_proof_version", "close_hold_kind"),
    # `endorsement-moot` RECORDS A PINNED TRUNK, WHERE `expired` RECORDS A
    # CONTROL, and the difference is which direction each one measured. An
    # absence door needs a known-good object to prove the repository could
    # read ANYTHING, because "not found" is what an unreadable repo says about
    # everything; a PRESENCE door has no such failure mode — its answer is an
    # ancestry reading against a named trunk, and the ref plus the sha it
    # pinned are what a later hand needs to re-run it. So the trio here is
    # `landed`'s and `resolved`'s, not `stranded`'s.
    #
    # `close_hold_kind` IS THE AUTHORIZATION HALF, for the reason `expired`
    # states directly above: the trio proves the tip reached trunk, and
    # WITHOUT THIS FIELD NOTHING IN THE RECORD SAYS THE VERDICT AUTHORIZED
    # NOTHING. That is the exact sentence that keeps the terminal from reading
    # as a landing — a close claiming a tip on trunk and NOT claiming
    # non-authorization is `landed` wearing another name.
    "endorsement-moot": ("closing_repo_id", "closing_trunk_ref",
                         "closing_trunk_sha", "close_proof_mode",
                         "close_proof_version", "close_hold_kind"),
    "discharged": ("discharging_id", "discharging_tip", "discharge_tier"),
    # `carried` records the RE-DERIVATION, not a sentence about it (task/756).
    # Every field here is what `dispatches.carriage_proof` was handed and what
    # it computed from, so the close can be re-run by hand years later against
    # the same objects: the repository, the trunk ref and the sha it pinned,
    # and the immutable (base, tip) the replay used. A close whose evidence is
    # prose is a close nobody can audit — the bar `subsumed` set.
    # `close_proof_mode` HERE NAMES WHICH WITNESS FAMILY AUTHORIZED — one of
    # `CARRIED_WITNESSES`, never `CLOSE_PROOF_MODES` (which is the BUILD
    # door's disjoint vocabulary for the same field name). Without it the
    # record cannot say whether a content replay or a patch-identity range
    # carried the row, and `carried_base` being absent would be the only
    # hint — an implicit encoding nobody reading the event years later would
    # decode. Validated against the RE-DERIVED witness in
    # `_close_event_error`, so it is a captured measurement and not a label.
    "carried": ("closing_repo_id", "closing_trunk_ref", "closing_trunk_sha",
                "carried_base", "carried_tip", "close_proof_mode",
                "close_proof_version"),
    # THE SEVENTH REGISTRATION POINT, and the arms that found it are the
    # first ones to drive a NON-DRY-RUN close. The ladder proves a close;
    # THIS TABLE PERSISTS IT — so the reason was spelled correctly in
    # every enumeration, documented, and admissible at the writer, and
    # the close still raised KeyError here while every dry-run arm stayed
    # green. Same fields as `carried` because the writer records the same
    # shape: the authorizing descendant's tip as the base and its row id
    # as the tip, with the usual proof and trunk trio.
    "chain-proof": ("closing_repo_id", "closing_trunk_ref",
                    "closing_trunk_sha", "carried_base", "carried_tip",
                    # THE RAW SUPERSEDES PATH IS THE PROOF, so it is recorded
                    # rather than left to be recomputed. Endpoints say an
                    # authority transfer happened and cannot show WHICH edges
                    # carried it — and those edges are ledger data that
                    # survive the history rewrite which took the objects,
                    # which is the whole reason this door can exist at all.
                    "chain_path", "chain_fork_census", "chain_census_cutoff",
                    # THE WRITE-TIME AUTHORITY CAPTURE (review ruling 2).
                    # Replay may not re-ask a MUTABLE policy — a later
                    # approval-tier edit must not resurrect or dissolve an
                    # append-only terminal — so the decision travels WITH the
                    # close and replay checks its STRUCTURE against the anchor.
                    # `chain_attestation` is present only where the tier was
                    # UNMEASURABLE and a discharging seat admitted it on the
                    # record (ruling 1).
                    "chain_tier_state", "chain_gate_requirement",
                    "chain_attestation", "chain_authority_anchor",
                    "close_proof_mode", "close_proof_version"),
    "subsumed": ("confirmation_id", "confirmation_tip", "confirmation_ref",
                 "original_author", "confirmation_recipient",
                 "original_author_family", "confirmation_recipient_family",
                 "original_verdict_anchor", "confirmation_verdict_anchor",
                 "original_family_evidence", "confirmation_family_evidence",
                 "original_family_anchor", "confirmation_family_anchor",
                 "confirmation_tier_state", "confirmation_gate_requirement",
                 "confirmation_gate", "confirmation_approval_anchor",
                 "closing_repo_id", "closing_trunk_ref", "closing_trunk_sha",
                 "close_proof_mode", "original_proof_mode",
                 "close_proof_version"),
    # `resolved` carries the confirmation trio like subsumed, plus the PIN.
    #
    # THE PIN IS NOT DECORATION AND IT IS NOT WHAT IT WAS ASKED TO BE. The
    # refutation asked for it so "replay validates ancestry AGAINST THE PIN",
    # and that is NOT implementable here: the replay arm never probes git, by
    # design (see the discharged branch — "Replay never probes git"). What the
    # pin does instead is exactly what discharged's asymmetry already
    # established: the WRITER proved ancestry with a live probe, the event
    # records the sha it was proven against, and replay re-derives only the
    # LEDGER-side rungs. The writer's leg is strictly stronger, never weaker,
    # and a later trunk rewrite cannot silently re-decide an old close because
    # the sha it was decided on travels with it and stays auditable by hand.
    "resolved": ("confirmation_id", "confirmation_tip", "confirmation_ref",
                 "original_author", "closing_repo_id", "closing_trunk_ref",
                 "closing_trunk_sha", "close_proof_mode",
                 "close_proof_version") + _CONTRADICTION_PROOF_FIELDS,
    "delivered-report": ("artifact_ref", "report_ref", "close_proof_version"),
}
_CLOSE_EVENT_BASE_FIELDS = {
    "v", "event", "seq", "id", "ts", "close_reason", "reviewed_tip",
    "close_evidence", "close_proof_version",
}
#: Stamped by the locked writer from this process's DECLARED identity and
#: ABSENT when it declares none — see the actor block in
#: `_record_close_proven`. It is deliberately in NO schema set: every
#: exact-set check subtracts it from the side being COMPARED and never adds it
#: to the side EXPECTED, because a field that is present or absent by
#: environment cannot sit inside an equality about proof shape. Putting it in
#: the base set instead made three derived schemas REQUIRE it, and an unseated
#: writer's event then refused as "missing close_actor" — the same bug wearing
#: the opposite sign.
CLOSE_ACTOR_FIELD = "close_actor"
# The delivery declaration is ADDITIVE over the build-landed schema: every new
# write carries both keys (the writer sets them unconditionally), and a
# PRE-DECLARATION event that carries neither still replays. Absence is never
# read as "cli" anywhere — it reads UNDECLARED, which is the whole law here.
_DELIVERY_FIELDS = {"close_delivery_class", "close_delivery_restart"}
# OPTIONAL BY CONSTRUCTION, like the delivery pair above. The BUILD landed
# schema is checked by EXACT SET EQUALITY, so a field listed there becomes
# MANDATORY for every build close — and only a content-equivalent close has a
# witness. Adding them to the set without this exemption broke every BUILD
# close proved by ancestry or patch-identity, which carry no witness at all.
# Measured before it shipped: expected 22 fields, an ancestry close builds 20,
# refused as "missing=content_witness,content_witness_anchor".
_CONTENT_PROOF_FIELDS = {"content_witness", "content_witness_anchor"}
_WITNESS_FIELDS = frozenset((
    "v", "algorithm", "trunk_ref", "trunk", "source", "carrier",
    "payload_digest", "newline_fingerprint", "matches"))
_WITNESS_SIDE_FIELDS = frozenset(("commit", "parents", "tree"))
# THE CONTENT IDENTITY LANGUAGE, copied from what the WRITER emits:
# _commit_content_identity returns sha256 hex for a real diff and the
# literal "EMPTY" for a true empty commit. Admission requires the HEX and
# refuses EMPTY on purpose — replay treats EMPTY as "no readable content
# identity" and rejects, so admitting it would admit a proof that cannot
# replay, which is the exact class this validator exists to close.
_CONTENT_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_BUILD_LANDED_EVENT_FIELDS = {
    "v", "event", "seq", "id", "ts", "close_reason",
    "close_proof_version", "landing_review_id", "landing_review_tip",
    "landing_review_verdict_anchor", "landing_review_tier_state",
    "landing_review_gate_requirement", "landing_review_gate",
    "landing_review_approval_anchor", "closing_repo_id",
    "closing_trunk_ref", "closing_trunk_sha", "close_proof_mode",
    "translated_tip", "close_delivery_class", "close_delivery_restart",
}
# THE WITNESS PAIR IS DELIBERATELY *NOT* IN THE SET ABOVE. That set is the
# EXACT field list of a build-landed event — a standing guard asserts
# `set(event) == _BUILD_LANDED_EVENT_FIELDS` — so a name listed there is
# REQUIRED of every build close, and only a content-equivalent close has a
# witness. Admission is handled by subtracting this pair at both enforcement
# points instead, which tolerates the fields when present without demanding
# them when absent. Adding them to the set directly refused every ordinary
# BUILD close (measured: 9 arms in tests/test_lr_close.CloseBuildLandedTest
# went red, "missing=content_witness,content_witness_anchor").
_DELIVERED_REPORT_PAYLOAD_FIELDS = (
    "artifact_ref", "report_ref", "close_evidence",
)
_DELIVERED_REPORT_EVENT_FIELDS = (_CLOSE_EVENT_BASE_FIELDS - {"reviewed_tip"}) \
    | set(_DELIVERED_REPORT_PAYLOAD_FIELDS[:-1])
_DELIVERED_REPORT_CORRECTION_FIELDS = _DELIVERED_REPORT_EVENT_FIELDS | {
    "corrects_event", "corrects_reason",
}
DELIVERED_ARTIFACT_REF_CAP = 512
DELIVERED_REPORT_REF_CAP = 256
_DELIVERED_ARTIFACT_REF = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*:[^\s]+\Z")
_DELIVERED_REPORT_REF = re.compile(r"[0-9a-f]{12}\Z")


def clean_delivered_report_refs(artifact_ref, report_ref, evidence):
    """((artifact, report, evidence), err) for the shared report schema.

    These checks prove syntactic identity only. They do not dereference the
    artifact or prove that the named chat row still exists."""
    artifact_ref, err = _clean(
        artifact_ref, "artifact ref", DELIVERED_ARTIFACT_REF_CAP)
    if err:
        return None, err
    if not _DELIVERED_ARTIFACT_REF.fullmatch(artifact_ref):
        return None, ("artifact ref must be <scheme>:<nonempty-value> with a "
                      "URI-style scheme and no whitespace")
    report_ref, err = _clean(report_ref, "report ref", DELIVERED_REPORT_REF_CAP)
    if err:
        return None, err
    if not _DELIVERED_REPORT_REF.fullmatch(report_ref):
        return None, "report ref must be a full 12-character lowercase hex Helm chat row id"
    evidence, err = _clean(evidence, "close evidence", 256)
    if err:
        return None, err
    return (artifact_ref, report_ref, evidence), None


_GATE_TOKEN_RE = re.compile(r"gate:[0-9a-f]{4,32}\s*")
_APPROVE_SUBSUMPTION = "Subsumption verified "
_FIX_SUBSUMPTION = \
    "Subsumption verified FIX findings were answered on trunk: "
_SUBSUMPTION_MARK = re.compile(re.escape(_APPROVE_SUBSUMPTION), re.I)

# The `resolved` door's marker. HEAD-ANCHORED and nowhere else, which is the
# whole difference from _marked_statement's permissive search.
#
# WHY THE POSITION IS LOAD-BEARING HERE AND NOT THERE: subsumption spends
# POLARITY as its check (the confirmation must be an APPROVE), so its phrase is
# corroboration. `resolved` deliberately admits a SUPERSEDE confirmation — the
# temporal deadlock it exists to end is a reviewer who content-verified the
# work and could not bind APPROVE because every receipt in existence predated
# the dispatch. Having spent polarity, the phrase IS the check, and a check
# that matches a subordinate clause is not one: a reviewer writing "the older
# claim that Resolution verified on trunk: X is what I am disputing" would
# otherwise mint a close out of its own refutation. Anchored, that sentence
# cannot qualify (amendment A of the refutation pass;
# #191 measured flag-prose incoherence three times).
_RESOLUTION = "Resolution verified on trunk: "
_RESOLUTION_MARK = re.compile(r"^\s*" + re.escape(_RESOLUTION), re.I)


def resolution_statement(value):
    """The concrete resolution a `resolved` confirmation carries, or None.

    None means 'this verdict does not claim a trunk resolution at its head' —
    never 'the verdict is bad'. The caller turns that into the refusal."""
    text = str(value or "")
    match = _RESOLUTION_MARK.match(text)
    if not match:
        return None
    return text[match.end():].strip() or None


def _marked_statement(value, prefix):
    """The LEGACY close-side payload reader for pre-cutover verdicts.

    Historical verdicts are immutable and keep the permissive grammar under
    which they were written. New verdicts go through _parse_subsumption at
    append and cannot carry ambiguous evidence."""
    text = str(value or "")
    low, plow = text.lower(), prefix.lower()
    if low.startswith(plow):
        return text[len(prefix):].strip()
    start = low.find(plow)
    gate = re.match(r"^gate:[0-9a-f]{16}(?![0-9a-f])", text)
    if start < 0 or not gate:
        return None
    between = text[gate.end():start].rstrip()
    if not between or between[-1] not in ".\n|—":
        return None
    return text[start + len(prefix):].strip()


def _parse_subsumption(value):
    """One strict append-time parse. -> (typed statement | None, error | None).

    Zero markers means ordinary verdict prose. Exactly one marker resolves to
    one typed APPROVE/FIX statement. More than one is ambiguity, never a parser
    choice: one verdict closes one debt, so the author must split the evidence.
    """
    text = str(value or "")
    low = text.lower()
    matches = list(_SUBSUMPTION_MARK.finditer(text))
    if not matches:
        return None, None
    if len(matches) != 1:
        offsets = ", ".join(str(m.start()) for m in matches)
        return None, ("one verdict closes one debt — split the evidence "
                      "(%d subsumption markers at offsets %s)" %
                      (len(matches), offsets))
    start = matches[0].start()
    fix = low.startswith(_FIX_SUBSUMPTION.lower(), start)
    kind, prefix = ("fix", _FIX_SUBSUMPTION) if fix else \
        ("approve", _APPROVE_SUBSUMPTION)
    if start == 0:
        payload = text[len(prefix):].strip()
    else:
        gate = re.match(r"^gate:[0-9a-f]{4,32}(?![0-9a-f])", text)
        # Strip horizontal padding only: newline is itself one of the licensed
        # boundaries, so generic rstrip() would erase the evidence before the
        # membership check and make that allowlist entry unreachable.
        between = text[gate.end():start].rstrip(" \t\r") if gate else ""
        if not between or between[-1] not in ".\n|—":
            return None, ("subsumption marker at offset %d is not at a "
                          "licensed gate-evidence boundary" % start)
        payload = text[start + len(prefix):].strip()
    if kind == "fix":
        if not payload:
            return None, "FIX findings-answered statement has no resolution"
    else:
        before, sep, after = payload.partition(" on trunk")
        if not sep or not before.strip() or not after.strip():
            return None, "APPROVE statement needs what changed and how trunk resolved it"
    return {"polarity": kind, "payload": payload,
            "marker_offset": start}, None


def _subsumption_ref(value, polarity="approve"):
    """Legacy close-side check; strictness belongs only to new append writes."""
    if polarity == "fix":
        return bool(_marked_statement(value, _FIX_SUBSUMPTION))
    if polarity != "approve":
        return False
    statement = _marked_statement(value, _APPROVE_SUBSUMPTION)
    if statement is None:
        return False
    before, sep, after = statement.partition(" on trunk")
    return bool(sep and before.strip() and after.strip())


def _proof_anchor(domain, value):
    """Stable content identity for one captured write-time proof object."""
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.blake2b((domain + "\0" + raw).encode("utf-8"),
                           digest_size=16).hexdigest()


def _verdict_author_runtime_evidence(identity, session, family, authority):
    """Generic immutable author-runtime envelope; policy interpretation is later."""
    if not isinstance(authority, dict) or authority.get("session") != session:
        return None
    runtime = {}
    resolved = {"agent_harness": None, "backend": None, "family": family,
                "model": None, "provider": None, "upstream_model": None}
    if authority.get("v") == 3:
        proof = authority.get("proxy_proof")
        if not isinstance(proof, dict):
            return None
        from . import proxywatch
        shape, err = proxywatch._proxy_proof_shape(proof)
        if err:
            return None
        runtime = {"family": family, "backend": "proxy"}
        if shape["agent_harness"] is not None:
            runtime["agent_harness"] = shape["agent_harness"]
        route = shape["route"]
        resolved.update(agent_harness=shape["agent_harness"],
                        backend="proxy", model=proof.get("model"),
                        provider=route.get("provider"),
                        upstream_model=route.get("upstream_model"))
    elif authority.get("v") == 5:
        runtime = dict(authority.get("runtime") or {})
        if runtime.get("family") != family:
            return None
        # THE MODEL THE NATIVE RUNTIME ALREADY RECORDS, carried instead of
        # dropped. `seats_runtime` learned a `model` field (HELM_MODEL_ID)
        # with its own validation rule, and this branch copied the two
        # neighbouring fields and left it behind — so one envelope could hold
        # the model in `runtime` and None in `resolved` and disagree with
        # itself about which model answered. Additive by measurement: no
        # stored native authority on this ledger carries the field, so every
        # minted verdict re-derives byte-identically.
        #
        # IT DOES NOT MAKE THE FAMILY MODEL-DERIVED, and that distinction is
        # what `verdict_family_axis` below exists to keep: a native family and
        # a native model are declared by the SAME launch seam, so recording
        # the model here adds a fact and grants no independence. Only a
        # measured proxy route derives the one from the other.
        resolved.update(agent_harness=runtime.get("agent_harness"),
                        backend=runtime.get("backend"),
                        model=runtime.get("model"))
    else:
        return None
    return {"v": 1, "identity": identity, "session": session,
            "runtime": runtime, "resolved": resolved,
            "authority": authority}


def _verdict_author_runtime_anchor(evidence):
    return _proof_anchor("verdict-author-runtime-v1", evidence) \
        if isinstance(evidence, dict) and evidence.get("v") == 1 else None


def _verdict_author_proxy_error(authority, identity, session):
    if not isinstance(authority, dict) or set(authority) != {
            "v", "identity", "roster_identity", "session", "proxy_proof"} \
            or authority.get("v") != 3:
        return "verdict author proxy authority is malformed"
    if authority.get("identity") != identity:
        return "verdict author proxy authority identity does not match"
    if authority.get("session") != session:
        return "verdict author proxy authority session does not match"
    from . import proxywatch, seats
    if not seats.recipient_matches(authority.get("roster_identity"), identity):
        return "verdict author proxy roster identity does not match"
    proof = authority.get("proxy_proof")
    if not isinstance(proof, dict) or proof.get("session") != session:
        return "verdict author proxy proof does not match its session"
    _shape, err = proxywatch._proxy_proof_shape(proof)
    return ("verdict author proxy proof is malformed: " + err) if err else None


def _verdict_author_runtime_error(evidence, identity, session, anchor):
    if not isinstance(evidence, dict) or set(evidence) != {
            "v", "identity", "session", "runtime", "resolved", "authority"} \
            or type(evidence.get("v")) is not int or evidence["v"] != 1 \
            or evidence.get("identity") != identity \
            or evidence.get("session") != session:
        return "verdict author runtime evidence is malformed"
    resolved = evidence.get("resolved")
    if not isinstance(resolved, dict) or set(resolved) != {
            "agent_harness", "backend", "family", "model", "provider",
            "upstream_model"}:
        return "verdict author resolved runtime is malformed"
    family = resolved.get("family")
    if not isinstance(family, str) or not _TOKEN.fullmatch(family):
        return "verdict author resolved family is malformed"
    authority = evidence.get("authority")
    if isinstance(authority, dict) and authority.get("v") == 3:
        why = _verdict_author_proxy_error(authority, identity, session)
    elif isinstance(authority, dict) and authority.get("v") == 5 \
            and authority.get("session") != session:
        why = "verdict author native authority session does not match"
    else:
        why = _family_evidence_error(
            authority, identity, family, _subsumed_family_anchor(authority))
    if why:
        return why
    expected = _verdict_author_runtime_evidence(
        identity, session, family, authority)
    if expected != evidence:
        return "verdict author resolved runtime does not match its authority proof"
    if anchor != _verdict_author_runtime_anchor(evidence):
        return "verdict author runtime anchor does not match its evidence"
    return None


def _subsumed_family_anchor(evidence):
    version = evidence.get("v") if isinstance(evidence, dict) else None
    return _proof_anchor("subsumed-family-v%d" % version, evidence) \
        if type(version) is int and version in (1, 2, 3, 4, 5) else None


def _subsumed_approval_anchor(verdict, tier, requirement, gate_id):
    return _proof_anchor("subsumed-approval-v1", {
        "verdict_anchor": verdict, "tier_state": tier,
        "gate_requirement": requirement, "gate": gate_id})


def _landed_review_approval_anchor(verdict, tier, requirement, gate_id):
    return _proof_anchor("landed-review-approval-v1", {
        "verdict_anchor": verdict, "tier_state": tier,
        "gate_requirement": requirement, "gate": gate_id})


def _chain_reaches(candidate, target_id, current, review_predecessor_tip=None):
    """(True|False|None, why): does candidate's exact parent walk reach target?

    A trajectory additionally names its prior distinct review tip. Build hops
    and same-tip fan-out are transparent; another reviewed tip is not. The
    default remains ancestry-only for the existing discharge callers.
    """
    target = str(target_id or "")
    node = candidate
    seen = set()
    while isinstance(node, dict):
        rid = str(node.get("id") or "")
        if review_predecessor_tip is not None:
            if node.get("chain_root") != candidate.get("chain_root") \
                    or node.get("repo_id") != candidate.get("repo_id") \
                    or node.get("kind") not in ("build", "review"):
                return None, "the prior-review walk crosses an unknown or foreign work identity"
            if node.get("kind") == "review" and node.get("tip") not in (
                    candidate.get("tip"), review_predecessor_tip):
                return False, "another distinct reviewed tip intervenes before the claimed prior round"
        if rid == target:
            return True, None
        parent = node.get("supersedes")
        if parent is None:
            return False, "its supersedes walk reaches its root without the target"
        if parent == CHAIN_UNKNOWN or parent in seen:
            return None, "its supersedes walk is corrupt or self-referential"
        seen.add(parent)
        node = (current or {}).get(parent)
        if node is None:
            return None, "its supersedes walk names a parent absent from the ledger"
    return None, "its supersedes walk is unreadable"


DISCHARGE_TIER_CHAIN = "chain"     # a successor whose supersedes-walk reaches this row
DISCHARGE_TIER_TIP = "tip"         # a peer bound to the IDENTICAL reviewed tip
DISCHARGE_TIERS = (DISCHARGE_TIER_CHAIN, DISCHARGE_TIER_TIP)


def bound_tip(row):
    """The exact commit a row's review obligation BINDS, or None.

    A REVIEW row's `ref` IS the tip under review from the instant it is
    dispatched — `reviewed_tip` only appears once a verdict is written on it,
    so reading only `reviewed_tip` makes every not-yet-reviewed row look like
    it binds nothing. A BUILD row's `ref` is a BASE, never reviewed content,
    so a build row binds nothing until a landing review names a tip for it.

    Full-id only, casefolded. A short or malformed value returns None rather
    than a prefix anyone could later widen into a match.
    """
    if not isinstance(row, dict):
        return None
    tip = row.get("reviewed_tip")
    if not tip and str(row.get("kind") or "").strip() == "review":
        tip = row.get("ref")
    tip = str(tip or "").strip().lower()
    return tip if _FULL_TIP.fullmatch(tip) else None


def _tip_on_trunk(repo_id, trunk_ref, tip):
    """is_landed probe for the WRITER side of the discharged door: is the
    discharging row's reviewed tip on this repo's trunk right now? Tri-state —
    could-not-look is None, and discharging_row fails closed on it. Replay
    never calls this; it reads the ledger's own landing closures instead.
    Through the vcs seam, like every spawn in this file's cohort."""
    if not isinstance(repo_id, str) or not repo_id or not trunk_ref \
            or not _FULL_TIP.fullmatch(str(tip or "")):
        return None
    from . import vcs
    # repo_id is the recorded GIT COMMON DIR; ancestry wants the repo root.
    root = repo_id[:-5] if repo_id.endswith("/.git") else repo_id
    relation = vcs.backend(root).ancestry(root, tip, trunk_ref)
    if relation == vcs.ANCESTOR:
        return True
    return False if relation == vcs.NOT_ANCESTOR else None


def _family_evidence_error(evidence, identity, family, anchor):
    """Validate immutable family proof without reinterpreting old closes.

    v1 is the historical minted+roster shape and remains replayable unchanged.
    v2 is the historical verified-runtime shape and also replays unchanged. v3
    is proxywatch's sanitized exact session-to-route proof; v4 is the stricter
    verified-NATIVE runtime written after backend became authoritative. This is
    deliberately forward-only: labels, seat types, minted proxy families, proof
    storage keys, recorded family strings, and harness names can neither mint
    nor revise cross-family proof.
    """
    if not isinstance(evidence, dict) or type(evidence.get("v")) is not int:
        return "family evidence must be one versioned identity snapshot"
    if evidence.get("identity") != identity:
        return "family evidence identity does not match the dispatch"
    if evidence["v"] == 1:
        if set(evidence) != {"v", "identity", "minted_families",
                            "roster_family", "roster_verified"}:
            return "family evidence must be one exact v1 identity snapshot"
        minted = evidence.get("minted_families")
        if not isinstance(minted, list) or minted != sorted(set(minted)) \
                or any(not isinstance(item, str) or not _TOKEN.fullmatch(item)
                       for item in minted):
            return "family evidence minted_families is malformed"
        roster_family = evidence.get("roster_family")
        if roster_family is not None \
                and (not isinstance(roster_family, str)
                     or not _TOKEN.fullmatch(roster_family)):
            return "family evidence roster_family is malformed"
        if type(evidence.get("roster_verified")) is not bool:
            return "family evidence roster_verified must be boolean"
        families = set(minted)
        if evidence["roster_verified"] and roster_family:
            families.add(roster_family)
        if families != {family}:
            return "family evidence does not prove exactly the recorded family"
    elif evidence["v"] == 2:
        if set(evidence) != {"v", "identity", "roster_identity", "runtime",
                            "runtime_verified"}:
            return "family evidence must be one exact v2 runtime snapshot"
        from . import seats
        if not seats.recipient_matches(evidence.get("roster_identity"), identity):
            return "family evidence roster identity does not match the dispatch"
        runtime = evidence.get("runtime")
        metadata, rejected = seats._runtime_metadata(runtime)
        if not isinstance(runtime, dict) or not runtime or rejected \
                or metadata != runtime:
            return "family evidence runtime record is malformed"
        if evidence.get("runtime_verified") is not True:
            return "family evidence runtime record is not verified"
        if runtime.get("family") != family:
            return "family evidence does not prove the recorded runtime family"
    elif evidence["v"] == 3:
        if set(evidence) != {"v", "identity", "roster_identity", "session",
                            "proxy_proof"}:
            return "family evidence must be one exact v3 proxy runtime snapshot"
        from . import proxywatch, seats
        if not seats.recipient_matches(evidence.get("roster_identity"), identity):
            return "family evidence roster identity does not match the dispatch"
        proof = evidence.get("proxy_proof")
        if not isinstance(proof, dict) \
                or evidence.get("session") != proof.get("session"):
            return "family evidence proxy proof does not match its roster session"
        measured, err = proxywatch._proxy_proof_family(proof)
        if err:
            return "family evidence proxy proof is malformed: %s" % err
        if measured != family:
            return "family evidence measured proxy route does not prove the recorded family"
    elif evidence["v"] in (4, 5):
        keys = {"v", "identity", "roster_identity", "runtime",
                "runtime_verified"}
        if evidence["v"] == 5:
            keys.add("session")
        if set(evidence) != keys:
            return ("family evidence must be one exact v%d native runtime "
                    "snapshot" % evidence["v"])
        from . import seats
        if not seats.recipient_matches(evidence.get("roster_identity"), identity):
            return "family evidence roster identity does not match the dispatch"
        if evidence["v"] == 5 and (not isinstance(evidence.get("session"), str)
                                    or not evidence["session"]):
            return "family evidence native session is malformed"
        runtime = evidence.get("runtime")
        metadata, rejected = seats._runtime_metadata(runtime)
        if not isinstance(runtime, dict) or not runtime or rejected \
                or metadata != runtime:
            return "family evidence native runtime record is malformed"
        if evidence.get("runtime_verified") is not True \
                or runtime.get("backend") not in (None, "native"):
            return "family evidence runtime record is not verified native runtime"
        if runtime.get("family") != family:
            return "family evidence does not prove the recorded native family"
    else:
        return "family evidence version is unsupported"
    if not _PROOF_ANCHOR.fullmatch(str(anchor or "")) \
            or anchor != _subsumed_family_anchor(evidence):
        return "family evidence anchor does not match its captured snapshot"
    return None


#: THE THREE FIELDS A VERDICT CARRIES TO PROVE WHO WROTE IT, named once.
#: Two call sites spelled this tuple out independently and a third (the
#: chain-proof authority rung) was about to make it three. A transcribed
#: constant is pinned to nothing: a fourth field added at the producer would
#: leave every copy quietly answering the old question. Present-but-incomplete
#: is a MALFORMED claim and must refuse; NONE-present is a HISTORICAL row that
#: predates the stamp, which is a different fact with a different remedy.
VERDICT_AUTHOR_EVIDENCE_FIELDS = ("verdict_author_session",
                                  "verdict_author_runtime_evidence",
                                  "verdict_author_runtime_anchor")
VERDICT_TIER_FIELDS = ("verdict_tier_evidence", "verdict_tier_anchor")
_VERDICT_TIER_CONTEXT = ("id", "recipient", "repo_id", "reviewed_tip",
                         "verdict_ref", "polarity", "gate", "gate_caps",
                         "basis", "verdict_author_runtime_anchor",
                         "verdict_ts", "verdict_seq", "verdict_version")


#: THE EXACT proof mode one close reason may record. A reason ABSENT here has
#: its mode policed by its own arm, because `landed`, `superseded` and
#: `withdrawn` each admit a VOCABULARY rather than one value. A reason PRESENT
#: here records exactly one string and nothing else binds — which is what makes
#: it checkable on the retry path, where no per-reason arm runs.
#: Reasons whose terminal may record exactly ONE proof mode, ever. This is
#: read by `_close_mode_error`, which the LOCKED WRITER runs for every
#: reason before the idempotence fork and which the chain-proof replay arm
#: calls too — so an entry here pins the mode at both doors from one line.
#: `expired` records the mode its ladder is named for and no other: the
#: terminal renders as a nonauthorizing verdict over an unlanded tip, and a
#: close wearing a witness it never measured is the exact class chain-proof
#: added this table for.
CLOSE_EXACT_PROOF_MODE = {
    "chain-proof": "chain-proof",
    "expired": "nonauthorizing-verdict-unlanded-tip",
    # THE TWIN OF THE LINE ABOVE, AND THE ONE WORD THAT DIFFERS IS THE WHOLE
    # DOOR. Both modes name a verdict that authorizes nothing; one measured
    # the tip absent from trunk and the other measured it present. Pinning
    # both here means neither door can ever record the other's witness, which
    # is what stops a presence close from replaying as an absence one.
    "endorsement-moot": "nonauthorizing-verdict-landed-tip",
}


def _close_retired_by(row):
    """What already retired this row, for the unified exclusivity refusal."""
    for field, _state, verb in _RETIRED_BY:
        if row.get(field):
            return verb
    if row.get("retired_admin"):
        return "retire --reason %s" % row.get("retire_reason")
    if row.get("close_reason"):
        return "close --reason %s" % row["close_reason"]
    return None


def mark_cancel(rid, reason):
    """Honestly ABANDON a dispatch with a reason — the only truthful terminal
    event when a verdict will never come (recipient gone, work moot,
    superseded). Idempotent on the same reason; refuses a dispatch whose
    verdict DECLARED a polarity (that one is already honestly closed).

    Resolve the displayed id FIRST. A bad short id and a bad reason are two
    independent errors; reporting the reason first made operators repair inputs
    in series before learning the identifier printed by `lr stalls` was usable.

    IT ALSO CLOSES THE ADVISORY ROW, which is a second admission and not a
    widening of the first. `advisory_close_error` owns that domain and states
    it: a verdict that declared NO polarity authorized nothing and demanded
    nothing, so an ADVISORY CLOSE is its one honest terminal, recorded as a
    cancel event whose reason NAMES the polarity-less verdict. The reviewed
    row with a real polarity keeps the refusal below unchanged.

    A V2 ROW WITH `verdict_tier_evidence.state` OUTSIDE IS A DIFFERENT CLASS
    AND IS NOT TOUCHED. `outside` is a MEASURED answer about the reviewer's
    authority over a verdict that DID declare a direction — the tier reader
    denies the authorization and the polarity still says what was asked for,
    so the row keeps every proof-bearing door its polarity earns. The rows
    this admits declared no direction at all, so there is nothing to deny and
    nothing to owe. Decided from `approval_tier_for_verdict`, which reads tier
    evidence and polarity as independent axes.
    """
    path = ledger_path()
    given_reason = reason

    def attempt(txn):
        # EACH TRY STARTS FROM THE CALLER'S VALUES: nothing one try
        # derives may leak into the next.
        reason = given_reason
        if not txn.held:
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        # THE ADVISORY ADMISSION IS DECIDED BEFORE THE REASON IS BUDGETED,
        # because the two answers are coupled: an advisory close records a
        # PREFIXED reason, so the budget it must meet is the operator's share
        # of the cap and not the whole cap.
        advisory = advisory_close_error(row) is None \
            or bool(row.get("cancel_advisory"))
        cap = _ADVISORY_REASON_CAP if advisory else _CANCEL_REASON_CAP
        reason, err = _clean(reason, "cancel reason", cap)
        if err:
            return None, err
        if not reason:
            return None, "cancel needs a reason (why the dispatch is abandoned)"
        if advisory:
            reason = advisory_close_reason(reason)
        if row["status"] == "cancelled":
            if row.get("cancel_reason") == reason:
                return row, None            # idempotent re-cancel
            return None, "dispatch %s already cancelled" % row["id"]
        # ONE APPEND PATH FOR BOTH ADMISSIONS. The advisory row skips the
        # status refusals below and joins the same event, projection and
        # `pk.event` the ordinary cancel has always used, so a field added to
        # a cancel cannot reach one admission and miss the other.
        advisory_close = row["status"] == "verdict" and advisory
        if row.get("verdict_retracted"):
            return None, retracted_refusal(
                row, "there is no standing verdict left to cancel")
        if row["status"] == "verdict" and not advisory_close:
            # THE REFUSAL NAMES THE DOOR IT IS NOT (task/2619). This sentence
            # is correct for the cancel invariant and, stopping there, it was
            # the whole instrument an author met after accepting a FIX in full
            # and DELETING the artifact. There is no successor tip and there
            # never can be, so the only move the tooling still offered was
            # `--supersedes`, which mints a fresh review obligation over
            # nothing — the shape quietly pressures somebody into re-dispatching
            # the thing they were just correctly told not to build.
            #
            # The withdrawn close already ADMITS exactly this row: measured on
            # a scratch ledger, a FIX-verdicted row closed `--reason withdrawn`
            # by its author is accepted, reads terminal / owed_by nobody /
            # stalled False, and leaves `obligation.unanswered_fixes` (the
            # owed-bot's population) in the same pass. NOTHING NAMED IT, which
            # is the entire defect — so the door is named where the wrong verb
            # refuses, not documented one surface away.
            #
            # "A DELETED ARTIFACT IS ALREADY ABSENT" HOLDS ONLY WHILE ITS OBJECT
            # READS. Measured on one FIX row each way: with the tip object
            # present `withdrawn` admits; once gc prunes it the landing reads
            # UNKNOWN, `withdrawn` refuses fail-closed, and `stranded` admits.
            # So the parenthetical names both, each with its condition.
            return None, ("dispatch %s already has a verdict (closed) — a "
                          "reviewed dispatch is not cancelled. If the verdict "
                          "was a FIX and the honest answer is that the "
                          "artifact should NOT exist, that is a WITHDRAWAL "
                          "and not a cancel: `helm lr close %s --reason "
                          "withdrawn --evidence \"<you accept the verdict, "
                          "and where the refutation is recorded>\"` retires it "
                          "without minting a review over nothing (git must "
                          "prove the reviewed tip absent from trunk, which it "
                          "can while that object still reads; once gc has "
                          "pruned it the absence reads UNKNOWN and withdrawn "
                          "refuses, and `helm lr close %s --reason stranded` "
                          "is the door for the destroyed object once nothing "
                          "live holds the work)"
                          % (row["id"], row["id"][:12], row["id"][:12]))
        if not advisory_close and row["status"] == "closed":
            return None, ("dispatch %s is already closed through its approved "
                          "review descendant" % row["id"])
        if not advisory_close and row["status"] not in CANCELLABLE_STATES:
            return None, ("dispatch %s is %s -- only an open or held dispatch "
                         "can be cancelled" % (row["id"], row["status"]))
        event = {"v": 3, "event": "cancel", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "reason": reason}
        if advisory_close:
            event["advisory"] = True
        if not txn.append(event):
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path

        def finish():
            out = dict(row)
            out.update(status="cancelled", cancel_reason=reason, seq=event["seq"])
            if advisory_close:
                out["cancel_advisory"] = True
            pk.event("dispatch-advisory-close" if advisory_close else "dispatch-cancel",
                     row["id"], reason)
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)

def mark_custody(rid, auth, outcome=None):
    """(row, err) — move a row's DELIVERY LEG to another seat.

    `outcome`, when a dict is given, gets `already=True` when the row's
    current custodian already matches the target and nothing is written. That
    answer is given AFTER the locked current-row, status and compare-and-swap
    checks, so a caller can tell a checked no-op from a write without trusting
    an earlier snapshot of who held the row (task/2529).

    THE MIRROR OF `rebind`, AND DELIBERATELY NOT THE SAME VERB. `rebind` moves
    the RECIPIENT: right for a row addressed TO a seat we are emptying, and
    wrong for one that seat SENT, where it hands a third party's review
    obligation to the successor and leaves the orphaned sender orphaned. This
    moves the other half — who must chase the row — and touches neither the
    recipient nor the sender.

    AUTHORSHIP IS NEVER REWRITTEN. `sender` stays exactly as recorded, because
    a ledger whose authorship can be edited cannot answer who wrote anything.
    Custody is a claim about the present and is therefore the mutable one.

    A CLOSED ROW REFUSES: a discharged obligation has no delivery leg left to
    owe, so moving custody of one would manufacture work rather than transfer
    it.
    """
    from . import seats, takeover
    path = ledger_path()
    # PROOF FIRST. A public mutation door that takes a bare seat token and no
    # evidence is the mint defect one surface over: it would let any
    # caller move a live seat's delivery leg with no measurement behind it. The
    # capability is an OBJECT that cannot be constructed outside takeover, so
    # this cannot be satisfied by spelling a value.
    err = takeover.reassign_custody_error(auth)
    if err:
        return None, err
    who = str(auth.target or "").strip()
    if not who or not _TOKEN.fullmatch(who):
        return None, ("the authorization carries no usable target seat (%r)"
                      % auth.target)
    reason, err = _clean(auth.reason, "custody reason", 256)
    if err:
        return None, err
    if not reason:
        return None, ("custody needs a reason — a delivery leg that changed "
                      "hands with no stated cause is unauditable")

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) -- custody NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row["status"] in CLOSED_STATES:
            return None, ("dispatch %s is %s -- a closed row has no delivery "
                          "leg to transfer" % (row["id"], row["status"]))
        # COMPARE AND SWAP AT THE WRITE: the append below proves this read is
        # the ledger it holds (`_ledger_write`). The caller
        # measured this row's holder in `holdings()`, and another transfer can
        # land in the window between that read and this write — at which point
        # a stale reassignment would silently overwrite a fresher one and the
        # delivery leg would land on a seat nobody chose.
        #
        # THE EXPECTATION IS READ OFF THE CAPABILITY, NOT ACCEPTED BESIDE IT.
        # It used to arrive as `expected=None` by default, so the swap was
        # OPT-IN: a caller that simply did not pass it got the overwrite with
        # no diagnostic, and the guard's own signature invited that. The only
        # production caller passed `expected=seat` while minting the auth with
        # `source=seat` — the same value twice, one of which could be
        # forgotten. The mint already refuses an authorization without a
        # resolved source, so reading it here makes the swap unskippable and
        # deletes the parameter that made skipping possible.
        expected = str(getattr(auth, "source", "") or "").strip()
        if not expected:
            return None, ("the authorization carries no source seat, so "
                          "custody cannot be compared and swapped")
        held = custodian_of(row)
        if not seats.recipient_matches(held, expected):
            return None, ("custody of %s is held by %s, not the %s this move "
                          "measured — it moved under us; re-read and retry"
                          % (row["id"][:12], held or "nobody", expected))
        if seats.recipient_matches(held, who):
            if outcome is not None:
                outcome["already"] = True
            return row, None             # already there; idempotent
        # Admission can precede a long lock wait or census. Recheck at the
        # append boundary, UNDER the lock; mint-time freshness cannot
        # authorize a later write.
        if not txn.lock():
            return None, ("ledger unwritable (%s) -- custody NOT recorded"
                          % path)
        err = takeover.reassign_custody_error(auth)
        if err:
            return None, err
        event = {"v": 3, "event": "custody", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "custodian": who,
                 "reason": reason}
        if not txn.append(event):
            return None, ("ledger unwritable (%s) -- custody NOT recorded"
                          % path)

        def finish():
            out = dict(row)
            out.update(custodian=who, custody_ts=event["ts"], seq=event["seq"])
            pk.event("dispatch-custody", row["id"], "%s: %s" % (who, reason))
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


# A FULL object id in either hash — 40 for sha1, 64 for sha256 — and nothing
# between them. `_TIP` deliberately spans that range for callers that accept
# abbreviations; a claim the integrator will gate against must name one exact
# commit.
_FULL_TIP = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _clean_tip_of(event):
    """The SOURCE-CLEAN tip a hold event declares, or "" when it declares none.

    One reader for the writer and the replay, so the two cannot drift: a hold
    whose stored tip the replay spelled differently from the one the door
    verified would let a surface bind a gate to a tree nobody read.

    THE VALUE IS THE ANSWER HERE, so a key the writer SENT in a shape this
    cannot read is refused rather than treated as absent — a hold claiming
    cleanliness at an unreadable tip must not read as an ordinary hold that
    claimed nothing."""
    if not isinstance(event, dict):
        return ""
    tip = event.get("source_clean_tip")
    if tip is None or not isinstance(tip, str):
        return ""
    tip = tip.strip().lower()
    # EXACTLY ONE FULL OBJECT ID, IN EITHER HASH. A private 40-only pattern
    # meant the door RESOLVED and STORED a sha256 tip and the replay then
    # dropped it, so a hold that succeeded lost its claim on the next
    # snapshot. Reaching for the module's `_TIP` to fix that OVERSHOT: it
    # admits every length from 40 to 64 because it also serves abbreviation-
    # tolerant callers, so a 63-character value replayed as a valid claim and
    # a surface declared a tree clean that no repository could name. The
    # writer resolves FULL ids, so nothing here justifies the widening.
    return tip if _FULL_TIP.fullmatch(tip) else ""


def mark_hold(rid, reason, owner_gated=False, source_clean_tip=None):
    """Put an OPEN dispatch on HOLD.

    `owner_gated` says the dependency is A DECISION ONLY THE OWNER CAN MAKE —
    an authorization, a ruling, a publication word — as opposed to a build
    box, a credential, or another seat's lane. The distinction is not
    cosmetic: an owner-gated row is NOT a machine stall, and every surface
    that counts stalls must exclude it or the owner reads his own queue as
    the fleet failing him (measured 2026-08-09: three of seven in-flight rows
    on his console said STALLED — owed by builder / owed by the integrator
    when all three were waiting on his word, and he read the board as frozen).

    It is a FLAG ON THE HOLD, not a property of the row, because the same row
    can be held for a machine reason today and an owner decision tomorrow.

    `source_clean_tip` is the third answer on that same axis and it is the
    INTEGRATOR'S: the reviewer read the delta, found nothing, and cannot mint
    an approve because an approve binds a verified whole-suite token that only
    the land gate on the rebased tree produces. The verdict door already tells
    reviewers to hold and name the tip, in prose — so the claim existed and
    nothing could read it, and the integrator learned about it by someone
    saying so in chat.

    THE TIP IS RESOLVED AGAINST THE ROW'S OWN REPOSITORY at this write
    boundary, exactly as `send` resolves `--ref`, so the ledger stores one
    exact commit and never a spelling. It is deliberately NOT required to equal
    the row's dispatched ref: a cure round moves the tip past it, which is the
    ordinary case, and the listing shows both so a divergence is visible rather
    than silently accepted or wrongly refused."""
    path = ledger_path()
    given_reason = reason

    def attempt(txn):
        # EACH TRY STARTS FROM THE CALLER'S VALUES: nothing one try
        # derives may leak into the next.
        reason = given_reason
        if not txn.held:
            return None, "ledger unwritable (%s) -- hold NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        reason, err = _clean(reason, "hold reason", 256)
        if err:
            return None, err
        if not reason:
            return None, "hold needs a reason (what external dependency blocks it)"
        clean_tip = ""
        if source_clean_tip is not None:
            # TWO HOLDERS CANNOT BOTH OWE THE NEXT MOVE. An owner-gated hold
            # waits on a decision only the owner can make; a source-clean hold
            # waits on a gate only the integrator can run. A row claiming both
            # would be counted in one surface and excluded by the other, so the
            # combination is refused at the door rather than left representable.
            if owner_gated:
                return None, ("a hold is owed by ONE holder: --owner-gated "
                              "waits on the owner's decision and --source-clean "
                              "waits on the integrator's land gate. Pass one")
            resolved, _branch = _resolve_tip(row.get("repo_root"),
                                             source_clean_tip)
            # THROUGH THE SAME READER THE REPLAY USES, which is what makes the
            # one-reader claim true rather than merely stated: whatever the
            # resolver returns is accepted here ONLY if the fold will accept it
            # too, so a hold can never succeed and then lose its claim.
            clean_tip = _clean_tip_of({"source_clean_tip": resolved})
            if not clean_tip:
                return None, ("--source-clean %s is missing, ambiguous, or not "
                              "a commit in %s: the tip a reviewer read clean "
                              "must be one the integrator can gate"
                              % (source_clean_tip,
                                 row.get("repo_root") or "this row's repository"))
        if row["status"] == "held":
            # THE WHOLE CLAIM, not only its prose. Comparing reasons alone made
            # a re-hold at a DIFFERENT source-clean tip a silent no-op that
            # returned the old row, so a reviewer re-declaring cleanliness
            # after a cure would have left the integrator gating the tip from
            # the round before.
            if row.get("hold_reason") == reason \
                    and (row.get("source_clean_tip") or "") == clean_tip:
                return row, None
            if row.get("hold_reason") == reason:
                return None, ("dispatch %s is already held at source-clean tip "
                              "%s -- release it and hold again to move the "
                              "claim to %s"
                              % (row["id"],
                                 row.get("source_clean_tip") or "none",
                                 clean_tip or "none"))
            return None, ("dispatch %s is already held with a different reason "
                         "(%s) -- release it first, or cancel and re-dispatch"
                         % (row["id"], row.get("hold_reason") or "unspecified"))
        if row["status"] in CLOSED_STATES:
            return None, ("dispatch %s is %s -- only an OPEN row can be held"
                         % (row["id"], row["status"]))
        event = {"v": 3, "event": "hold", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "reason": reason,
                 "owner_gated": bool(owner_gated)}
        # OMITTED WHEN THERE IS NO CLAIM, never written as null: an event that
        # says nothing about source-cleanliness is the truthful shape for a
        # hold that made no such claim, and it keeps two identical holds
        # comparing equal across the door that added this field.
        if clean_tip:
            event["source_clean_tip"] = clean_tip
        if not txn.append(event):
            return None, "ledger unwritable (%s) -- hold NOT recorded" % path

        def finish():
            out = dict(row)
            out.update(status="held", hold_reason=reason, hold_ts=event["ts"],
                       owner_gated=bool(owner_gated), seq=event["seq"])
            if clean_tip:
                out["source_clean_tip"] = clean_tip
            pk.event("dispatch-hold", row["id"],
                     "%s%s" % (reason, " [source-clean %s]" % clean_tip[:12]
                               if clean_tip else ""))
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


def mark_release(rid):
    """Return a HELD dispatch to OPEN."""
    path = ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) -- release NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row["status"] not in ("held", "open"):
            return None, ("dispatch %s is %s -- only a HELD (or already open) "
                         "row can be released" % (row["id"], row["status"]))
        if row["status"] == "open":
            return row, None
        event = {"v": 3, "event": "release", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reason": row.get("hold_reason")}
        if not txn.append(event):
            return None, "ledger unwritable (%s) -- release NOT recorded" % path

        def finish():
            out = dict(row)
            out.update(status="open", release_reason=event["reason"],
                       release_ts=event["ts"], seq=event["seq"])
            # THE SAME KEYS THE REPLAY DROPS. A return that kept the claim while the
            # fold dropped it made the in-process answer and the stored one disagree
            # about an OPEN row, which is the kind of split that survives until some
            # future caller reads the wrong one.
            for key in ("owner_gated", "hold_reason", "hold_ts", "source_clean_tip"):
                out.pop(key, None)
            pk.event("dispatch-release", row["id"], event["reason"])
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


# ---------------------------------------------------------------------------
# THE VERDICT RETRACTION (task/3060)
# ---------------------------------------------------------------------------
# A VERDICT IS IMMUTABLE, AND UNTIL THIS EXISTED A WRONG ONE HAD NO CORRECTIVE.
# The door refuses a second verdict on a verdicted row ("terminal is
# immutable"), `cancel` refuses a row whose verdict declared a polarity, and
# the only moves left were to contest it with a successor review (whose FIX
# became chain debt while the parent APPROVE still read READY for its tip), to
# retire it, or to not land it. The measured case: a delegated reader running
# inside a seat wrote an APPROVE with no findings that its brief never
# authorized, and that immutable row kept authorizing a land nobody meant.
#
# A RETRACTION IS A LATER FACT, NEVER A REWRITE. It is one `verdict-retract`
# event appended after the verdict. The verdict event, its evidence, its gate
# binding and its attestation stay exactly as recorded; the projection reads
# polarity RETRACTED and keeps the original in `retracted_polarity`. The
# retraction carries the corrected READING as a labelled claim for the
# integrator — it is not a polarity and re-enters no verdict semantics.
#
# WHO MAY RETRACT, and the list is short on purpose: the verdict's AUTHOR (the
# row's recipient seat, from any session, because the session that erred is
# usually gone), the INTEGRATOR role resolved from the roster, or the OWNER
# through his own capability. The row's SENDER is refused: a sender who
# disagrees with a review contests it with `dispatch send --supersedes`, the
# path whose reviewer-shopping residual is already disclosed.
RETRACT_EVENT = "verdict-retract"
#: What the retracting hand now says the review found. A CLAIM for the
#: integrator to read, never a polarity: source-clean (the delta reads clean
#: and waits on the land gate), fix, supersede, or unknown (the verdict was
#: wrong and nobody has re-read it yet).
RETRACT_READS = ("source-clean", "fix", "supersede", "unknown")
#: How the retracting hand knows. `unverified` is absent on purpose: a
#: retraction removes authority, and one that cannot say how it knows is a
#: guess about somebody else's review.
RETRACT_BASES = ("measured", "inferred")
#: The door that admitted the retraction, recorded on the event.
RETRACT_ROLES = ("author", "integrator", "owner")
#: The projected polarity of a retracted verdict. It is outside POLARITIES,
#: so `clean_polarity` refuses it at every writer and `_replay_polarity`
#: reads it as UNDECLARED: authority fails closed.
RETRACTED = "retracted"
_RETRACT_PROOF_V = 1
_RETRACT_REASON_CAP = 256


def _retract_admission_error(state):
    """Why this row's verdict cannot be retracted, or None when it can.

    ONE PREDICATE FOR THE WRITER AND THE REPLAY. `retract` asks it before it
    mints anything, `_record_retract` asks it under the ledger lock, and
    `_apply` asks it through `_retract_record` for every event it folds."""
    if not isinstance(state, dict):
        return "the row is unreadable"
    if state.get("verdict_retracted"):
        return "its verdict is already retracted"
    if state.get("status") != "verdict":
        return ("only a verdicted row has a verdict to retract (this one is "
                "%s)" % (state.get("status") or "in an unknown state"))
    if _replay_polarity(state.get("polarity")) is None:
        return ("its verdict declared no polarity, so it authorized nothing "
                "to retract: `helm dispatch cancel %s <reason>` closes it as "
                "advisory" % str(state.get("id") or "")[:12])
    terminal = _close_retired_by(state)
    if terminal:
        return "it is already retired by %s" % terminal
    return None


def _retract_record(event, state):
    """(state fields, None) for a well-formed retraction of `state`, else
    (None, why). The replay's validator and the writer's projection."""
    err = _retract_admission_error(state)
    if err:
        return None, err
    reason, err = _clean(event.get("retract_reason"), "retract reason",
                         _RETRACT_REASON_CAP)
    if err:
        return None, err
    if event.get("retract_reads") not in RETRACT_READS:
        return None, "retract reads must be one of %s" % "|".join(RETRACT_READS)
    if event.get("retract_basis") not in RETRACT_BASES:
        return None, "retract basis must be one of %s" % "|".join(RETRACT_BASES)
    if event.get("retract_role") not in RETRACT_ROLES:
        return None, "retract role must be one of %s" % "|".join(RETRACT_ROLES)
    seat = event.get("retract_seat")
    if not isinstance(seat, str) or not _TOKEN.fullmatch(seat):
        return None, "retract seat must be an exact seat token"
    # THE EVENT NAMES WHAT IT RETRACTS, the way a retip names the tip it
    # moves: a retraction spliced onto a different verdict is inert.
    if event.get("retracted_polarity") != state.get("polarity") \
            or str(event.get("retracted_tip") or "") \
            != str(state.get("reviewed_tip") or ""):
        return None, "the retraction names a verdict this row does not carry"
    successor = event.get("retract_successor")
    if successor is not None and (not isinstance(successor, str)
                                  or not _ID.fullmatch(successor)
                                  or successor == state.get("id")):
        return None, "retract successor must be another row's id"
    same = event.get("retract_same_session")
    if same is not None and not isinstance(same, bool):
        return None, "retract same-session must be true, false or absent"
    if type(event.get("retract_proof_version")) is not int \
            or event.get("retract_proof_version") != _RETRACT_PROOF_V:
        return None, "unknown retract proof version"
    if not _valid_ts(event.get("ts")):
        return None, "retract timestamp is unreadable"
    fields = {"retracted_polarity": state.get("polarity"),
              "retract_ts": event.get("ts"), "retract_reason": reason,
              "retract_reads": event["retract_reads"],
              "retract_basis": event["retract_basis"],
              "retract_role": event["retract_role"], "retract_seat": seat,
              "retract_successor": successor}
    # ABSENT STAYS ABSENT: a verdict that recorded no author session cannot
    # be compared, and a default would claim a comparison nobody made.
    if same is not None:
        fields["retract_same_session"] = same
    return fields, None


def retracted_refusal(row, act):
    """The sentence a door says over a retracted row: what was retracted,
    when, by whom, and where the review now lives."""
    rid = str(row.get("id") or "")
    successor = str(row.get("retract_successor") or "")
    where = ("the successor %s carries the review" % successor[:12]
             if successor else
             "re-request it: `helm dispatch send %s %s --ref %s --kind %s "
             "--supersedes %s` (body on stdin)"
             % (row.get("recipient") or "<reviewer>",
                row.get("lane") or "<lane>",
                row.get("tip") or "<tip>", row.get("kind") or "review",
                rid[:12]))
    return ("dispatch %s: its %s verdict was RETRACTED at %s by @%s (%s), so "
            "%s; %s" % (rid[:12],
                        str(row.get("retracted_polarity") or "?").upper(),
                        row.get("retract_ts") or "an unrecorded time",
                        row.get("retract_seat") or "?",
                        row.get("retract_role") or "?", act, where))


def _retract_role(row, seat, owner=None):
    """(role, None) for the door that admits `seat` to retract `row`'s
    verdict, else (None, why).

    THE AUTHOR IS THE ROW'S RECIPIENT, because a land-authorizing verdict
    binds its author to exactly that seat (`mark_verdict`). The identity is
    the seat, never the session: the session that wrote a wrong verdict is
    usually gone, and requiring it would leave the error with no author able
    to take it back."""
    if owner is not None:
        return "owner", None
    from . import seats
    if seats.recipient_matches(row.get("recipient") or "", seat):
        return "author", None
    from . import seats_integrator
    integrator, why = seats_integrator.integrator_seat()
    if integrator and seats.recipient_matches(integrator, seat):
        return "integrator", None
    return None, (
        "refusing to retract dispatch %s's verdict as @%s: a verdict is "
        "retracted by its AUTHOR (@%s, from any session), the INTEGRATOR (%s) "
        "or the OWNER. A seat that disagrees with a review contests it with "
        "a successor: `helm dispatch send %s %s --ref %s --kind %s "
        "--supersedes %s`"
        % (str(row.get("id") or "")[:12], seat, row.get("recipient") or "?",
           "@" + integrator if integrator else "unresolved: %s" % why,
           row.get("recipient") or "<reviewer>", row.get("lane") or "<lane>",
           row.get("tip") or "<tip>", row.get("kind") or "review",
           str(row.get("id") or "")[:12]))


def _retract_matches(row, seat, reason, reads, basis, successor):
    """Is `row` already retracted exactly as asked? The idempotent retry
    reconciles the standing retraction and never writes a second one."""
    return bool(row.get("verdict_retracted")) \
        and str(row.get("retract_seat") or "").casefold() == seat.casefold() \
        and row.get("retract_reason") == reason \
        and row.get("retract_reads") == reads \
        and row.get("retract_basis") == basis \
        and (successor is None or row.get("retract_successor") == successor)


def _record_retract(rid, seat, role, reason, reads, basis, successor=None,
                    session=None):
    """(row, err) — append ONE `verdict-retract` event, under the lock.

    Every admission is decided again here from the locked read: the caller's
    pre-read may be seconds old, and a verdict retracted or retired in that
    window must not be retracted twice."""
    path = ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) -- retraction NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if _retract_matches(row, seat, reason, reads, basis, successor):
            return row, None
        err = _retract_admission_error(row)
        if err:
            if row.get("verdict_retracted"):
                return None, retracted_refusal(row, "it is not retracted again")
            return None, "dispatch %s cannot be retracted: %s" % (row["id"], err)
        event = {"v": 3, "event": RETRACT_EVENT, "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "retract_reason": reason, "retract_reads": reads,
                 "retract_basis": basis, "retract_role": role,
                 "retract_seat": seat,
                 "retracted_polarity": row.get("polarity"),
                 "retracted_tip": row.get("reviewed_tip"),
                 "retract_proof_version": _RETRACT_PROOF_V}
        # OMITTED WHEN THERE IS NONE, never written as null.
        if successor:
            event["retract_successor"] = successor
        author_session = row.get("verdict_author_session")
        if session and isinstance(author_session, str) and author_session:
            event["retract_same_session"] = session == author_session
        # THE REDUCER IS THE WRITER'S PROJECTION, and its refusal is an error:
        # an event it would not fold must never reach the ledger.
        out = _apply(row, event)
        if out is row:
            fields, why = _retract_record(event, row)
            return None, ("the retraction was refused by the reducer before "
                          "append (%s) — nothing was recorded" % why)
        if not txn.append(event):
            return None, "ledger unwritable (%s) -- retraction NOT recorded" % path

        def finish():
            pk.event("dispatch-retract", row["id"],
                     "%s retracted by %s (%s): reads %s" % (
                         str(row.get("polarity") or "").upper(), seat, role,
                         reads))
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


def retract(rid, reason, reads, basis, reissue=False, successor=None,
            owner=None, notify=True):
    """(row, err) — RETRACT a standing verdict: `helm dispatch retract`.

    The row keeps its verdict and gains a `verdict-retract` event after it;
    the projection reads RETRACTED everywhere authority is read. `reads` is
    the corrected reading (RETRACT_READS), `basis` how the retracting hand
    knows (RETRACT_BASES).

    `reissue` MINTS THE SUCCESSOR REVIEW in the same motion: same recipient,
    lane, tip and kind, `--supersedes` this row, the lane author's name
    inherited as sender (the move mint `rebind` uses, so the successor does
    not read as a self-review by whoever retracted). The successor is written
    FIRST, and a retraction that then fails disowns it, so a failed call never
    leaves a successor claiming an obligation that did not move. `successor`
    instead links an existing row that already supersedes this one.

    `owner` is the owner's capability (`ownerasks.OwnerDoor`) and nothing
    else: a caller-stated name is never the owner. Without it the acting seat
    is resolved through `_acting_author` and must hold the author's or the
    integrator's door (`_retract_role`)."""
    reason, err = _clean(reason, "retract reason", _RETRACT_REASON_CAP)
    if err:
        return None, err
    if reads not in RETRACT_READS:
        return None, ("--reads must be one of %s (what the review now reads)"
                      % "|".join(RETRACT_READS))
    if basis not in RETRACT_BASES:
        return None, ("a retraction declares its basis: --%s"
                      % "|--".join(RETRACT_BASES))
    if reissue and successor:
        return None, ("--reissue mints the successor and --successor names an "
                      "existing one: pass one")
    # THE ROW FIRST: a row this helm cannot read is refused by the vocabulary
    # rung before anything else is asked about it (`unknown_kinds_refusal`).
    current, unavailable = snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = _resolve_row(current, rid)
    if err:
        return None, err
    if owner is not None:
        from . import ownerasks
        if not isinstance(owner, ownerasks.OwnerDoor):
            return None, ("the owner's door is a capability, never a name: "
                          "retract as your own seat")
        seat = ownerasks.OWNER
    else:
        seat, err = _acting_author("retract this verdict")
        if err:
            return None, err
    successor_id = None
    if successor:
        # NO allow_retired: a successor retired by the terminality rung
        # carries nothing, so it cannot be named as the row that does.
        kid, err = _resolve_row(current, successor)
        if err:
            return None, "--successor: " + err
        if kid.get("supersedes") != row["id"]:
            return None, ("--successor %s does not supersede %s, so it does "
                          "not carry this review; name the row minted with "
                          "--supersedes %s, or pass --reissue"
                          % (kid["id"][:12], row["id"][:12], row["id"][:12]))
        successor_id = kid["id"]
    if _retract_matches(row, seat, reason, reads, basis, successor_id):
        return dict(row), None
    err = _retract_admission_error(row)
    if err:
        if row.get("verdict_retracted"):
            return None, retracted_refusal(row, "it is not retracted again")
        return None, "dispatch %s cannot be retracted: %s" % (row["id"], err)
    role, err = _retract_role(row, seat, owner)
    if err:
        return None, err
    new = None
    if reissue:
        # THE ROW'S OWN REPOSITORY, exactly as rebind resolves it: the
        # successor re-requests the same obligation, never a new one.
        repo_path = str(row.get("repo_id") or "")[:-5] or None
        new, add_err = add(
            row.get("recipient"), row.get("lane"),
            ref=row.get("tip") or row.get("ref"), note=row.get("note"),
            deadline_s=int(row["deadline_s"]) if row.get("deadline_s")
            else None,
            repo=repo_path, kind=row.get("kind"), notify=notify,
            supersedes=row["id"], _reason=True,
            _ref_branch=row.get("ref_branch"), _preserve_origin=_MOVE_MINT)
        if new is None:
            return None, ("retraction NOT recorded: the successor review was "
                          "refused: %s" % (add_err or "dispatch NOT recorded"))
        successor_id = new["id"]
    out, err = _record_retract(row["id"], seat, role, reason, reads, basis,
                               successor=successor_id,
                               session=home.session_id())
    if err:
        if new is not None:
            fate = _rebind_disown_child(
                new["id"], "retract aborted: the verdict on %s was not "
                "retracted" % row["id"][:12])
            err = "%s; %s" % (err, fate)
        return None, err
    out = dict(out)
    if new is not None:
        out["reissued"] = new
    return out, None



_STARVED_SIGNALS = ("down", "hang")


def _proxy_evidence(recipient):
    """(starved-reason, None) when proxywatch measures the recipient's PROXY
    as unable to serve it, else (None, unavailable-note). The signal surface is
    health()'s row for the seat: proxy probe down/hang, log refusal streak
    (STARVED), or a live pane with a transcript that has stopped growing
    (HANG?). An unreadable health pass is NOT evidence — rebind by default
    requires the measured signal, so a blind watch means no rebinding, which
    is the safe polarity (the judgment path is --force)."""
    from . import proxywatch
    try:
        # Rebind evidence is LOCAL ability-to-act evidence. It must never spend
        # authenticated family-canary tokens or wait on sibling corroboration.
        rep = proxywatch.health(seats=[recipient], include_upstream=False)
    except Exception as e:
        return None, "proxywatch health unreadable (%s)" % e.__class__.__name__
    rows = rep.get("seats") or []
    if not rows:
        return None, "proxywatch has no row for %s" % recipient
    row = rows[0]
    if row.get("probe") in _STARVED_SIGNALS:
        return ("proxy probe %s (%s)" % (row["probe"], row.get("probe_detail"))), None
    if row.get("log") == "streak":
        return ("proxy refusing streak (%s)" % row.get("log_detail")), None
    if row.get("hang_candidate"):
        age = row.get("transcript_age_s") or 0
        return ("pane live, transcript silent %dm" % (age // 60)), None
    return None, None


CONTEXT_WALL_PCT = 100.0

# autocompact.read()'s `status` is an elif CHAIN, so an earlier verdict MASKS
# every later check. Only these two are reached AFTER the freshness test, and
# so are the only statuses whose pct is provably THIS pane's CURRENT context.
# `claude-model` in particular short-circuits BEFORE the age check, so a
# claude-model row may be arbitrarily stale while still carrying a pct.
_CONTEXT_FRESH_STATUSES = ("ok", "session-unbound")


def _context_wall(recipient):
    """(wall-reason, None) when the recipient's CONTEXT WINDOW is measurably
    exhausted, else (None, None).

    THE BLINDNESS THIS CURES. Rebind's only evidence surface was proxywatch,
    which measures the PROXY. A seat at 100% of its context window has a
    perfectly healthy proxy and cannot take a turn, so the gate refused every
    rebind off it and the judgment path (--force) was the only way through.
    Measured: one codex seat climbed 82% -> 116% over twenty minutes across
    21 consecutive autocompact refusals — proxywatch reported it healthy the
    whole time, because it WAS healthy. The instrument was sound and pointed at
    the wrong subject.

    THE CLASSIFIER ALREADY EXISTS — autocompact.read() is the owner of the
    context question and idle_dispatch._context_pressure already composes it
    read-only. This asks that one owner rather than growing a second gauge,
    which is the same choice _provider_wall made one file over.

    PRESENT-TENSE ABILITY IS THE QUESTION, not recoverability. A context-full
    seat may well be rescued by a later /compact, and that does not make it able
    to act NOW — conflating the two is what kept this arm from existing. Rebind
    is also non-destructive (cancel-as-REBOUND plus a superseding re-add), so a
    seat that recovers a minute later has lost nothing.

    A STALE READING IS NOT A MEASUREMENT OF NOW, which is why the status
    allowlist is narrow and derived from reading autocompact's own elif chain
    rather than from the status names sounding trustworthy."""
    try:
        from . import autocompact
        row = autocompact.read(recipient) or {}
    except Exception:
        return None, None
    if row.get("status") not in _CONTEXT_FRESH_STATUSES:
        return None, None
    pct, win = row.get("pct"), row.get("window")
    if pct is None or not win or pct < CONTEXT_WALL_PCT:
        return None, None
    return ("context window exhausted: %.1f%% of %dk (autocompact status %s)"
            % (pct, int(win) // 1000, row.get("status"))), None


def _recipient_evidence(recipient):
    """(starved-reason, None) when the recipient is measurably unable to act,
    else (None, unavailable-note). TWO INDEPENDENT SURFACES, because a seat can
    be blocked by its transport OR by its own window and neither one can see
    the other: proxywatch answers "is the proxy serving it", autocompact
    answers "has it any window left".

    THE CONTEXT ARM RUNS EVEN WHEN PROXYWATCH IS BLIND. A proxywatch that
    cannot be read is exactly the moment a second, independent measurement is
    worth most, so its unavailable-note is carried and only returned when BOTH
    arms come back silent — never as an early exit that suppresses the other."""
    reason, note = _proxy_evidence(recipient)
    if reason:
        return reason, None
    wall, _ = _context_wall(recipient)
    if wall:
        return wall, None
    return None, note


_PARENT_REQUIRED = object()


def _successor_finished(kid, cache, parent=_PARENT_REQUIRED):
    """Does this successor hold a verdict whose work is PROVABLY on trunk?

    THE AUTHORIZATION RUNG. Everything else in the sweep keys on SHAPE (open,
    has a successor, unannotated), and shape is a thing the fleet keeps
    producing: a live sweep run 2026-08-04 matched a row that had entered the
    shape 54 SECONDS earlier. So an id allowlist cannot authorize this sweep —
    it expires in minutes. This predicate can, because it asks whether the
    work is FINISHED, which is the harm statement itself.

    BOUND TO `reviewed_tip`, AND THAT BINDING IS THE RUNG. `verdict_ref` reads
    like the field for this and is NOT: measured over the live ledger, it
    holds free-text evidence that merely BEGINS with a gate token — the shape
    is `gate:<token> VERIFIED <tree> host=<node>. <prose>`, never a bare sha,
    and the prose runs to the evidence budget. Fed to `merge-base` it is UNKNOWN
    for every row, and UNKNOWN fails closed — so bound there this sweep would
    have selected NOTHING, silently, forever, with a green suite, because a
    fixture supplies its own `verdict_ref` in whatever shape its author
    imagined. Only the live population could refute it.

    PATCH IDENTITY COUNTS HERE, unlike the `resolved` door's ancestry-only
    rung, and the difference is the QUESTION. There it was "did this exact
    object reach trunk"; here it is "is the work finished" — and the
    integrator rebases every chain, so what lands is patch-identical and
    object-different. Ancestry-only would skip most genuinely-finished
    parents, which is precisely the miss `vcs.landed_state` exists to end.

    POLARITY IS DELIBERATELY NOT A RUNG. The fact being annotated — that this
    parent was superseded — was declared by the successor's author at write
    time; the successor's REVIEW outcome does not revoke it. Landedness is
    what proves the parent is finished, and a tip nobody approved does not
    reach trunk."""
    if not isinstance(kid, dict) or kid.get("status") != "verdict":
        return False
    tip = str(kid.get("reviewed_tip") or "").strip().lower()
    repo = kid.get("repo_id")
    if not _FULL_TIP.fullmatch(tip) or not isinstance(repo, str) \
            or not os.path.isdir(repo):
        return False
    # ONE REPOSITORY'S TRUNK PROVES NOTHING ABOUT ANOTHER'S ROW. Everything
    # below measures the KID against the KID's trunk, and the caller then
    # annotates the PARENT — so without this the sweep discharges a row in
    # repo A on the strength of work that landed in repo B. Mechanism A
    # settled the identical invariant for carriers (spec A-REPO, the
    # `cross-repo` proof value); this is that rung applied to the sweep,
    # which never had it.
    #
    # repo_id IS THE KEY, NEVER THE PROJECT LABEL: `_repo_project` is NOT
    # injective, and the live ledger proves it rather than the docs
    # asserting it — TWO checked-out copies of one upstream sit at
    # different paths whose BASENAME is identical, so both derive the
    # same label. A label comparison would call those one repository.
    #
    # FAIL CLOSED ON ABSENCE, because this authorizes a WRITE: a parent whose
    # repo_id is missing or unreadable is not proven same-repo, and unproven
    # is not permission. Measured 2026-08-05: 667 live parent/successor pairs,
    # ALL same-repo, zero cross-repo and zero missing — so this refuses
    # nothing that happens today and refuses the first thing that does.
    # THE DEFAULT IS A REFUSAL, NOT A SKIP. `parent=None` would have made
    # "caller forgot" indistinguishable from "no parent to check", and the
    # forgetting is silent — a future consumer would inherit zero protection
    # and nothing would say so. A sentinel makes the omission FAIL CLOSED, so
    # the guarantee is a contract of this rung rather than of whoever calls it.
    prepo = parent.get("repo_id") if isinstance(parent, dict) else None
    if not isinstance(prepo, str) or not prepo or prepo != repo:
        return False
    try:
        from . import vcs
        be = cache.get(repo)
        if be is None:
            be = cache[repo] = vcs.backend(repo)
        return be.landed_state(repo, tip, be.trunk_ref(repo)) in (
            vcs.ANCESTOR, vcs.PATCH_EQUIVALENT)
    except Exception:                       # a write this authorizes fails CLOSED
        return False


def superseded_parent_sweep(apply=False):
    """([(parent_id, successor_id)], err) — OPEN rows a successor supersedes
    that carry no annotation yet AND whose successor's work is FINISHED.
    Dry-run by DEFAULT; `apply` annotates each exactly as the write path does.

    AUTHORIZED BY PREDICATE, NEVER BY AN ID LIST (integrator ruling
    2026-08-04): "open + successor holds a verdict + the successor's work is
    ON TRUNK". `_successor_finished` is that last clause and carries the
    argument. A predicate is also what makes this safe to run UNATTENDED — an
    allowlist for a shape-matching selector is stale before it is typed.

    SCOPED TO PRESENTED-AS-ACTIONABLE ROWS, on the integrator's ruling and
    against my own first instinct. Measured 2026-08-04: 124 rows are
    structurally unterminated (a successor exists, no close_reason), but only
    OPEN rows are OFFERED by any enumeration surface — the lr projection
    already suppresses superseded parents, and ZERO appear there. The brief's
    harm is "told seats to redo finished work", a claim about ENUMERATION: a
    row no surface offers has told nobody anything. The 124-row bulk write was
    REFUSED on the record; append-only writes are justified by OCCURRING harm.

    ANNOTATES, NEVER CANCELS — the same law the write path learned the hard
    way: a BUILD parent must keep its status so `landed`/`discharged` can close
    it on its successor's PROOF.

    IDEMPOTENT BY CONSTRUCTION, not by a flag: it selects rows WITHOUT
    `superseded_by`, and applying sets it, so a second run selects nothing."""
    current, unavailable = snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    succ = {}
    for r in current.values():
        if isinstance(r, dict) and r.get("supersedes"):
            succ.setdefault(str(r["supersedes"]), []).append(r["id"])
    shaped = sorted((r["id"], sorted(succ[str(r["id"])])[0])
                    for r in current.values()
                    if isinstance(r, dict) and r.get("status") == "open"
                    and not r.get("superseded_by")
                    and succ.get(str(r.get("id")) or ""))
    cache = {}
    hits = [(pid, kid) for pid, kid in shaped
            if _successor_finished(current.get(kid), cache,
                                   parent=current.get(pid))]
    if not apply:
        return hits, None
    path = ledger_path()

    def attempt(txn):
        # PER TRY: a refusal counted on a read the write then discarded must
        # not be counted twice.
        done, refused = [], []
        if not txn.held:
            return done, "ledger unwritable (%s) — sweep NOT recorded" % path
        fresh, unavailable = snapshot()
        if unavailable:
            return done, "dispatch ledger unavailable: %s" % unavailable
        for pid, kid in hits:
            parent = fresh.get(pid)
            if not isinstance(parent, dict) or parent.get("superseded_by"):
                continue            # re-checked on the read the write binds
            # A PARENT THIS HELM CANNOT READ IS NOT ANNOTATED, AND IS NAMED.
            # One such row refuses only itself: the sweep's other parents are
            # rows this helm reads in full.
            refusal = unknown_kinds_refusal(parent)
            if refusal:
                refused.append(refusal)
                continue
            if not txn.append({
                    "v": 3, "event": "superseded",
                    "seq": (parent.get("seq") or 0) + 1,
                    "id": pid, "ts": pk.now_ts(), "successor": kid}):
                return done, "ledger unwritable (%s)" % path
            done.append((pid, kid))

        def finish():
            return done, ("; ".join(refused) if refused else None)
        return txn.then(finish)
    return _ledger_write(attempt, path)


def rebind_room_fence(row, old_recipient):
    """[{lane, holder, remaining_s}] — rooms the OLD recipient still holds for
    this row's lane FAMILY. WARN-only data; this never releases anything.

    THE SURPRISE IT EXISTS TO END, measured 2026-08-04: a rebind moves the
    OBLIGATION and leaves the WORKTREE LEASE with the old recipient. Row
    069406da7cf6 was rebound to a live seat while the room stayed fenced under
    the WALLED seat it came from, so the new builder could not start the work
    they had just been handed. The integrator's fix is a FRESH ROOM (`<lane>-r2`)
    — one line, no force-release, no risk to a walled seat's tree — and that
    workaround costs a LANE-LABEL/BRANCH DIVERGENCE the builder must be told
    about: the row's lane label keeps naming a branch that will never contain
    the work, so every surface resolving lane->branch reads the wrong one.
    `dispatch send` catches it ("--ref is NOT one of lane X's own commits") and
    nothing downstream does.

    WHY WARN AND NOT RELEASE (integrator ruling, #203): a walled seat is not a
    dead one, and its room may hold real work in the general case. Transfer is
    a separate lane. This rung only makes the fence VISIBLE at the moment the
    rebind is decided, which is the moment the information is free.

    Every reader here is prior art, deliberately: `_lane_family_names` already
    walks the -rN stems, `claims_list` is the ONE scrubbed publish boundary for
    holder/remaining, and `_lanes.resource` owns the key shape. Re-deriving any
    of them would be a second spelling of an identity that must stay single."""
    lane = str(row.get("lane") or "").strip()
    gitdir = str(row.get("repo_id") or "").strip()
    if not lane or not old_recipient:
        return []
    try:
        from . import seats
        from .work import _lanes
        from . import landreq
    except Exception:                       # noqa: BLE001 — a warn never raises
        return []
    root = os.path.dirname(gitdir.rstrip(os.sep)) if gitdir else ""
    if not root:
        return []
    # DIRECTION MATTERS AND THE FIRST CUT HAD IT BACKWARDS. I pre-computed
    # `_lane_family_names(lane)` and looked those resources up — but that walks
    # from a name to BROADER stems, while the fresh-room workaround creates
    # NARROWER ones (`<lane>-r2`). So the rung went quiet for exactly the rooms
    # the workaround mints, which is precisely when it matters. Caught by the
    # -rN test the ruling asked for. The fix is to ask the question of each
    # HELD room instead: does its lane stem-match the row's? `_stems_match` is
    # generous by design ("exact equality always; containment either way above
    # the floor"), and over-matching here costs one read of a warn nobody has
    # to act on, while under-matching costs the whole rung.
    prefix = _lanes.resource(root, "")
    try:
        mine = landreq._stem(lane)
        held = seats.claims_list()
    except Exception:                       # noqa: BLE001 — a warn never raises
        return []
    # SHAPE MEASURED, NOT ASSUMED: claims_list() returns a LIST of
    # {resource, holder, fence, remaining, liveness, stale}. The first cut of
    # this read a `remaining_s`/`left` key that does not exist and hedged on a
    # dict-vs-list return that never happens — both would have produced a
    # silent None rather than an error, which is the failure mode a warn rung
    # can least afford: it would print a fence with a blank TTL and read as
    # noise.
    out = []
    for c in (held or []):
        if not isinstance(c, dict) or c.get("holder") != old_recipient:
            continue
        res = str(c.get("resource") or "")
        # SCOPED TO THIS REPOSITORY by the resource prefix: another project's
        # identically-named lane is not this rebind's business, and a warn that
        # names someone else's room is the kind that gets skimmed.
        if not res.startswith(prefix):
            continue
        if landreq._stems_match(mine, landreq._stem(res[len(prefix):])):
            out.append({"lane": res, "holder": c.get("holder"),
                        "remaining": c.get("remaining"),
                        "liveness": c.get("liveness")})
    return out


def _rebind_disown_child(child_id, why):
    """Cancel a successor that must NOT outlive the source it replaced, and
    say what actually happened to it.

    A rebind writes TWO rows under TWO separately-acquired locks. Whenever the
    second write does not happen, the first one has already created a child
    that claims an obligation which never moved. Leaving it OPEN is the #178
    stranding: a row telling a seat it owes work nobody can discharge.

    THE REASON IS CLAMPED, AND THAT CLAMP IS LOAD-BEARING. mark_cancel runs
    _clean(reason, "cancel reason", 256), which refuses on length AND on any
    control character. The residual path used to interpolate the source's own
    error text into this reason — and that error is exactly where a long repo
    path or an embedded newline lives ("ledger unwritable (<278-char path>)"),
    so the child's cancel was REFUSED precisely in the failure this function
    exists to clean up, leaving BOTH rows open (found in review of
    the #178 rebind-race lane).
    Callers now pass a bounded reason AND the clamp holds the floor, so no
    future caller can reintroduce the fault from a distance.

    This REPORTS rather than assumes. The child's own cancel can fail for other
    reasons too (the ledger that refused the source's cancel is the same file),
    and an abort that swears the child is gone when it is still OPEN would hide
    exactly the row an operator has to go clean up by hand.
    """
    why = " ".join(str(why or "").split())        # newlines/controls -> spaces
    if len(why) > _CANCEL_REASON_CAP:
        why = why[:_CANCEL_REASON_CAP - 1] + "…"
    # THE CHILD MAY ALREADY HAVE CHILDREN. If it was itself rebound before this
    # cleanup ran, cancelling only the id we were handed cancels an INTERMEDIATE
    # and leaves the grandchild OPEN, still claiming an obligation that never
    # moved — the same stranding one generation down (an adversarial sweep
    # of the #178 rebind-race lane). Walk the whole descent and cancel every row
    # still able to be cancelled; a chain that cycles or runs away stops here and
    # is REPORTED rather than silently half-cleaned.
    snap = snapshot()[0] or {}
    chain, sid, seen, unreached = [], str(child_id), set(), None
    while sid:
        if sid in seen:
            unreached = "the chain repeats at %s" % sid[:12]
            break
        seen.add(sid)
        chain.append(sid)
        kid = snap.get(sid)
        if not isinstance(kid, dict):
            # We cannot read this row, so we cannot know what lies BEYOND it.
            # Anything past here is an unwalked frontier and must be reported.
            unreached = "%s is unreadable, so its descendants were not walked" \
                        % sid[:12]
            break
        nxt = kid.get("superseded_by")
        sid = str(nxt) if nxt else None
    done, failed = [], []
    for rid in chain:
        row = snap.get(rid)
        if isinstance(row, dict) and row.get("status") not in CANCELLABLE_STATES:
            # PRESERVE A LEGITIMATE TERMINAL STATE. A descendant that reached a
            # VERDICT earned it, and a cleanup has no business rewriting real
            # history to CANCELLED; the invariant is that no reachable
            # descendant remains OPEN, not that every one reads cancelled.
            continue
        _, err = mark_cancel(rid, why)
        (failed if err else done).append(
            "%s (%s)" % (rid[:12], err) if err else rid[:12])
    # A TRUNCATED WALK IS A FAILED CLEANUP, NEVER A SUCCESS LIST. Reporting
    # "cancelled A, B, C" while an unwalked frontier is still OPEN is the same
    # laundering this whole lane exists to remove, one level up: the caller
    # believes the obligation is contained when it is not.
    if failed or unreached:
        parts = []
        if failed:
            parts.append("could NOT cancel %s — still OPEN, cancel by hand"
                         % ", ".join(failed))
        if unreached:
            parts.append("CLEANUP INCOMPLETE: %s" % unreached)
        if done:
            parts.append("cancelled " + ", ".join(done))
        return "; ".join(parts)
    if not done:
        return "child %s was already terminal, nothing to cancel" % child_id[:12]
    return "cancelled " + ", ".join(done)


def _brief_travel_note(old, new):
    """One sentence for the rebinder about WHAT REACHED the new recipient.

    THE OLD NOTE FIRED UNCONDITIONALLY and said one thing in three different
    situations, only one of which it described. It told the operator "no
    recoverable DM message body traveled ... (send rows retain only a hash)"
    even when the superseded row was an `add` that never had a DM in the first
    place — nothing was lost, and the note asked for a re-brief anyway.

    Three states, three sentences, and the two ABSENCES are not merged:
      the brief travelled          — say so, and whether it was truncated
      there was never a brief      — KNOWN-EMPTY, nothing is owed
      the row predates storage     — UNKNOWN, and this is the one that owes a
                                     re-brief (`message_hash` present, no text)
    """
    # THROUGH THE REFERENCE DOOR. A rebind is the one move whose whole purpose
    # is that the instruction arrives with the obligation, so it is the last
    # place that may report a bounded copy as "the original brief".
    brief, _why, problem = brief_of(new)
    if brief and not problem and new.get("brief_ref") is not None:
        return ("the ORIGINAL BRIEF TRAVELED WHOLE with this rebind — %d "
                "UTF-8 bytes, stored by reference and read back proven, so @%s "
                "starts with the instruction, not just a lane label."
                % (len(brief.encode("utf-8")), new["recipient"]))
    if brief:
        return ("the ORIGINAL BRIEF TRAVELED with this rebind — %d characters "
                "stored on %s%s, so @%s starts with the instruction, not just "
                "a lane label.%s"
                % (len(brief), new["id"][:12],
                   " (TRUNCATED; the stored text says exactly where it stops)"
                   if BODY_TRUNCATED_MARK in brief else "",
                   new["recipient"], (" " + problem) if problem else ""))
    _body, why = body_of(old)
    if why == BODY_NONE or not old.get("message_hash"):
        # KNOWN-EMPTY. An `add` row carries no DM by construction, so its hash
        # is None — and that is true of an add row from BEFORE body storage too,
        # which is why the hash decides here and not the key's presence.
        return ("no brief travelled because THERE NEVER WAS ONE: %s was filed "
                "with `dispatch add`, which sends no DM. @%s has the lane, the "
                "ref and the rebind reason, and nothing was lost."
                % (old["id"][:12], new["recipient"]))
    return ("NO recoverable DM message body travelled with this rebind: %s "
            "PREDATES body storage — it carries a message_hash and no text, so "
            "its brief is UNKNOWN to this ledger rather than empty. Re-brief "
            "@%s directly, or they start without the original brief."
            % (old["id"][:12], new["recipient"]))


def rebind(rid, to, reason=None, force=False, repo=None, notify=True):
    """Move one OPEN dispatch to a new recipient, atomically in intent:
    cancel the old row as REBOUND (not abandoned — the trail says where the
    obligation went) and open a NEW row preserving lane/ref/kind/note/deadline,
    to `to`.

    EVIDENCE-GATED (council 0.3 gap G1): the default path REFUSES unless
    either proxywatch measures the current recipient starved/hung/down OR
    autocompact proves its fresh context window exhausted. The arms are
    independent: an unreadable proxywatch is not evidence by itself and never
    suppresses a current context measurement; only UNKNOWN across BOTH arms
    refuses. The manual 3-4 step re-route a judgment seat did by hand becomes
    one verb, and a verb that can later be CALLED automatically must require a
    measured signal by default. --force overrides with a mandatory reason (a
    judgment seat's prerogative, recorded).

    AND A LIVE READER IS REFUSED EARLIER, BY NAME (helm/rebind_liveness.py).
    The evidence gate asks whether the recipient CAN act; it answers "not
    measurably unable" for every healthy seat and names nobody, then
    advertises the override in the same breath. A row whose recipient is
    measurably live AND measurably mid-read is therefore refused by its own
    rung first, naming the reader, the evidence that they are live and the
    row's state. Under --force that rung writes instead of refusing: the
    recorded reason says it WAS an override and names the seat the row came
    from, which is the one sentence the party who lost it needs.

    The two ledger writes are NOT transactional. The linked successor is
    appended first so the writer's locked duplicate check can refuse without
    cancelling the source row; only then is the source cancelled. Between the
    writes both rows may be OPEN, but the supersedes edge makes the child the
    one active frontier rather than inventing unrelated work. A cancel failure
    is surfaced with the recorded child id for bounded recovery."""
    current, unavailable = snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = _resolve_row(current, rid)
    if err:
        return None, err
    if row["status"] != "open":
        return None, ("dispatch %s is %s — only an OPEN row can be rebound"
                      % (row["id"], row["status"]))
    # OPEN IS NOT THE SAME AS OWED. A parent whose successor already carried
    # the work to a verdict is still status=open, so rebinding it minted a NEW
    # sibling against finished work and resurrected a discharged obligation.
    # Resolved against the SAME snapshot the row came from, so the answer
    # cannot drift between the read and the write.
    _held_by = carrier(row, current)
    if _held_by is not None:
        # THE PHRASE IS LOAD-BEARING. The writer's own duplicate-fork refusal
        # already said "old row remains OPEN", and an operator reading a
        # refusal needs to know the parent was not left in some half-state.
        # This guard REPLACES that path because it is strictly broader and
        # fires before any write: measured, the writer refuses only for an
        # OPEN successor, so a parent whose successor reached a VERDICT was
        # rebound and MINTED A SIBLING against finished work with no refusal
        # at all. Same sentence, earlier, and now covering the terminal case.
        return None, ("rebind NOT started; old row remains OPEN because %s "
                      "already carries this work (status %s). Rebinding would "
                      "mint a sibling against work that has moved on; act on "
                      "that row instead."
                      % (str(_held_by.get("id"))[:12], _held_by.get("status")))
    to, terr = _recipient_operand(to)
    if terr:
        return None, terr
    if to == row.get("recipient"):
        return None, "dispatch %s is already addressed to %s" % (row["id"], to)
    # THE LIVE-READER RUNG, in a sibling module because this file is past its
    # split budget. Asked BEFORE the capacity gate below because it is the
    # more specific question: the capacity gate can only say the recipient is
    # not measurably unable to act, which is true of every healthy seat and
    # names nobody. Asked on the --force path too, where it produces nothing
    # but the sentence recorded on the ledger — the reader who loses the row
    # is the one party the cancel reason must be legible to.
    reading = rebind_liveness.live_reader(row)
    if reading and not force:
        return None, rebind_liveness.refusal(reading)
    if not force:
        evidence, unavailable_note = _recipient_evidence(row["recipient"])
        if not evidence:
            why = ("; proxywatch: %s; context arm produced no fresh-exhaustion "
                   "evidence" % unavailable_note) if unavailable_note else (
                       " — neither proxywatch starvation/hang nor fresh context "
                       "exhaustion was measured")
            return None, ("rebind REFUSED: %s is not measurably unable to act%s. "
                          "The default path requires either measured proxy "
                          "starvation/hang or fresh context exhaustion so the "
                          "verb can later be called automatically; a judgment "
                          "seat overrides with --force --reason '...'"
                          % (row.get("recipient"), why))
        reason = reason or evidence
    if not (reason or "").strip():
        return None, "rebind needs a reason (--reason, or the measured one)"
    if reading:
        # Only reachable under --force: the refusal above returns on every
        # other path. The reason is rewritten rather than appended to so the
        # override survives the cap the cancel reason is cut at.
        reason = rebind_liveness.override_reason(reading, reason)
    # A rebind moves the SAME obligation, so its repo identity is immutable:
    # --repo may only spell an alternate path to the repo already recorded on
    # the row. A same-tip foreign clone resolves to a different .git and is
    # refused BEFORE the cancel — a silent identity swap at rc0 was the
    # finding on 6a8f9530. On a validated row the RECORDED root is what
    # flows onward (checking the caller's path and then re-resolving that
    # same path in add() would be a check/use hole — a retargeted symlink
    # between the two resolutions swaps the identity the check just blessed).
    # Rows older than repo_id carry none and keep the caller-supplied path
    # (legacy, never guessed).
    repo_path = str(row.get("repo_id") or "")[:-5] or None
    if repo is not None:
        want = row.get("repo_id")
        info = _repo_info(repo) if want else None
        if want and (not info or info["repo_id"] != want):
            return None, ("rebind REFUSED: --repo %s resolves to %s but the "
                          "obligation is bound to %s — the same obligation "
                          "cannot change repos; name a path inside the "
                          "recorded repo or re-dispatch"
                          % (repo, (info or {}).get("repo_id", "no git repo"),
                             want))
        if not want:
            repo_path = repo
    reason = ("rebound to %s: %s" % (to, str(reason).strip()))[:256]
    # The new row is an `add` (persist + public notice), never a `send`: this
    # verb moves an obligation, it does not deliver a second DM. What it CAN do
    # now is carry the brief — `_preserve_origin` copies the parent's stored
    # `message_body` onto the child, so a row sent after 2026-08-27 arrives with
    # its instruction attached instead of a lane label and a ref. Nothing is
    # invented: a parent that stored no body still hands the child None, and the
    # CLI says WHICH of the two absences it is (`_brief_travel_note`).
    #
    # AND THE AUTHOR TRAVELS WITH IT. Before `_preserve_origin` the child was
    # stamped with whoever ran the rebind, which `landreq` projects straight
    # into `author` — a silent re-authoring of the land request. The sender is
    # INHERITED off the parent row (never accepted as an argument, see
    # `_inherited_origin`) and the mover is recorded as `acted_by`.
    # The ref validates against the row's OWN repo (repo_id is the .git
    # dir recorded at dispatch time) — the caller's cwd is irrelevant.
    # SUPERSEDES, never --new-work: a rebind is the SAME OBLIGATION addressed to
    # a different seat, and stamping it as new work would fabricate a second
    # piece of work out of one — the precise lie the required field exists to
    # prevent, committed by helm itself. Append the child FIRST: its writer lock
    # sees any existing live child and refuses before the old row is touched.
    # Rebind's --force is only recipient-health authority; it must never double
    # as duplicate-fork authority. The branch binding moves with the SAME
    # obligation too: preserve the parent's write-time evidence (including
    # None), rather than manufacturing identity from today's topology.
    # WARN-ONLY, COMPUTED BEFORE THE MOVE because it is a fact about the OLD
    # recipient and the row stops naming them the moment the cancel lands.
    # Never raises and never blocks: a rebind whose fence probe fails is still
    # a correct rebind, and #203 is explicitly a warn rung, not a gate.
    fence = rebind_room_fence(row, row.get("recipient"))
    new, add_err = add(
        to, row.get("lane"), ref=row.get("tip") or row.get("ref"),
        note=row.get("note"),
        deadline_s=int(row["deadline_s"]) if row.get("deadline_s")
        else None,              # no stated deadline -> the new row's KIND decides
        repo=repo_path, kind=row.get("kind"), notify=notify,
        supersedes=row["id"], _reason=True,
        _ref_branch=row.get("ref_branch"), _preserve_origin=_MOVE_MINT)
    if new is None:
        return None, ("rebind NOT started; old row remains OPEN because the "
                      "replacement was refused: %s"
                      % (add_err or "dispatch NOT recorded"))
    # THE TWO-LOCK RACE (#178). `add` appended the child under the WRITER lock
    # and released it; `mark_cancel` takes the EVENTLEDGER lock separately. A
    # verdict landing BETWEEN the two writes terminalizes the source, the cancel
    # is then REFUSED, and the child survives OPEN pointing at a source nobody
    # can discharge — the #173/#177 stranded-obligation class arriving by RACE
    # instead of by a missing verb.
    #
    # ONE SHARED LOCK IS NOT THE CURE. Both writes take fcntl.flock(LOCK_EX) on
    # the SAME sibling lock file — eventledger.locked() opens `<ledger>.lock`,
    # never the ledger itself — through SEPARATE fds, and flock is per-fd:
    # holding the first across the second blocks forever on our own lock. The window is
    # inherent to two independently-locked writes, so the honest cure is to
    # notice and refuse to leave a child behind.
    #
    # The predicate is CANCELLABLE_STATES, NOT `status != "open"`: a source that
    # went HELD mid-rebind is still perfectly cancellable, and aborting on it
    # would kill a rebind that was about to succeed. An unreadable row fails the
    # test too, which is the safe direction — a source we cannot see is a source
    # we cannot cancel.
    fresh = snapshot()[0].get(row["id"]) or {}
    if fresh.get("status") not in CANCELLABLE_STATES:
        reached = fresh.get("status") or "unreadable"
        fate = _rebind_disown_child(
            new["id"], "rebind aborted: source %s reached %s mid-rebind"
            % (row["id"][:12], reached))
        return None, ("rebind aborted: dispatch %s reached %s while the "
                      "replacement was being written, so it can no longer be "
                      "cancelled; %s" % (row["id"][:12], reached, fate))
    cancelled, err = mark_cancel(row["id"], reason)
    if err:
        # The recheck above narrows this window, it does not close it: the
        # recheck reads WITHOUT the lock, so the source can still terminalize
        # between that read and the cancel's own acquisition. Whatever the
        # reason the cancel did not happen, the obligation did not move, so the
        # child must not go on claiming it did.
        # BOUNDED BY CONSTRUCTION: the source's own error does NOT go in the
        # cancel reason. It is unbounded prose that can carry a long path or a
        # newline, and mark_cancel refuses on either — which used to make this
        # disown fail in exactly the case it exists for. The full error still
        # reaches the caller in the message returned below, which has no cap.
        fate = _rebind_disown_child(
            new["id"], "rebind incomplete: source %s was not cancelled"
            % row["id"][:12])
        return {"old": row, "new": new, "reason": reason,
                "room_fence": fence}, (
            "replacement %s was recorded, but old row %s was NOT cancelled: "
            "%s; %s" % (new["id"], row["id"], err, fate))
    return {"old": cancelled, "new": new, "reason": reason,
            "room_fence": fence}, None


def _retip_replay_tree(gitdir, base, new_tip, old_tip):
    """The tree the OLD lane's work produces when replayed onto the NEW tip;
    None = UNKNOWN.

    A DIFF HASH CANNOT BIND LOCATION, which is why this replaced one. A file
    carrying the same block WITH THE SAME SURROUNDING CONTEXT at two places
    gives `git patch-id` — `--stable` and `--verbatim` alike — ONE id for an
    edit to either copy, because patch-id ignores hunk locations by
    construction. Trees differ, identity read verified: a false accept in the
    direction that matters. No stronger hash helps; location is precisely what
    a patch id discards.

    AND THE REPLAY NEEDS NO TRUNK. Merge the old tip into the new one over
    THEIR OWN fork point and compare the emitted tree to the new tip's. Exact
    equality proves the recipient inherits the same final content — the actual
    claim — rather than that two descriptions of a change hash alike.
    `merge-base(old_tip, new_tip)` lands BELOW the new tip's real base whenever
    trunk moved between them, so the new side's COMMIT SET is contaminated
    with trunk's commits; that is true and it does not reach this, because
    those commits are already inside the new tip and merging the old lane's
    work into it cannot be fooled by them. Measured on a real fold: 17 commits
    against 19 on the two sides, and an exact tree match.

    THE MERGE BASE IS PASSED EXPLICITLY, and that is a determinism POLICY
    rather than a behavioural guard: in an ordinary single-best-base DAG it
    coincides with the base git would infer, so removing it changes no arm —
    measured across five shapes and pinned as an ARGUMENT CONTRACT. The
    residual it exists for is criss-cross history, where several BEST bases
    exist and git's inferred single choice can differ from the one this
    computed. A mutation matrix reading that as a defect is reading a known
    semantic redundancy.

    CONFLICT, an unreadable tree and an unsupported plumbing shape all return
    None and the caller refuses on each — content is the half this predicate
    can always answer, so an unanswerable content check is a refusal rather
    than a degradation. One plumbing call, and `--no-messages` because the
    conflict diagnostics are buffered and then discarded."""
    from . import landreq
    got = landreq._git(gitdir, "merge-tree", "--write-tree", "--no-messages",
                       "--merge-base=" + base, new_tip, old_tip)
    if got is None or got.returncode != 0:
        return None
    first = got.stdout.split("\n")[0].strip() if got.stdout else ""
    return first if _TIP.fullmatch(first.lower()) else None


def _retip_tree_of(gitdir, tip):
    """`tip^{tree}`, or None when it cannot be read."""
    from . import landreq
    got = landreq._git(gitdir, "rev-parse", "--verify", "--quiet",
                       tip + "^{tree}")
    out = got.stdout.strip() if got is not None and got.returncode == 0 else ""
    return out if _TIP.fullmatch(out.lower()) else None


def _retip_build_refusal(row, old_tip, new_tip, replayed, wanted, relation):
    """The refusal text for a build base whose REPLAY did not reproduce the new
    tip's tree, or None when it did.

    EVERY SENTENCE STATES A STRUCTURAL FACT AND STOPS. An earlier cut said "it
    is not a rebase of the same work" whenever the comparison failed, which is
    a CONTENT verdict drawn from STRUCTURAL evidence — the overclaim task/1470
    names across ~25 consumers. Refusing is right; saying what it MEANS about
    the author's work is not this function's to say."""
    from . import landreq
    stale = "" if relation == landreq.NOT_ANCESTOR else \
        " Ancestry was UNANSWERABLE here, so the replay was the only evidence " \
        "available and it did not answer either."
    if replayed is None:
        detail = ("replaying this work onto the new base did not produce a "
                  "readable tree — a conflict, an unsupported plumbing shape, "
                  "or a merge git could not complete — so nothing proved the "
                  "recipient would inherit the same content")
    elif wanted is None:
        detail = ("the new tip's own tree could not be read, so there was "
                  "nothing to compare the replay against")
    else:
        detail = ("replaying this work onto the new base produces tree %s "
                  "while the new tip's tree is %s. A patch that hashes alike "
                  "can still land in a different place, so the tree is the "
                  "thing compared" % (replayed[:12], wanted[:12]))
    return ("retip REFUSED: %s does not descend from %s, and %s. Nothing was "
            "changed.%s Cancel + a fresh dispatch (--supersedes %s) if this "
            "base really was replaced by work the recipient has not read"
            % (new_tip[:12], old_tip[:12], detail, stale, row["id"][:12]))


_TRUNK_REF_KEY, _TRUNK_REMOTE_KEY = "helm.trunkref", "helm.trunkremote"


# THE SHA-SHAPED HALF OF THE BINDING. The other two fields are not shas and
# each has its own validator below, which is the whole reason this is a
# per-field function rather than a list walked with one pattern.
_MOVING_SHAS = ("base_sha", "trunk_sha")


def _moving_binding(row):
    """The binding fields a hop carries forward, each validated AS ITS OWN
    TYPE.

    THESE FIELDS TRAVEL WITH A ROW WHEN ITS BASE MOVES, and the receipt is the
    one that was left behind: persisted at dispatch and dropped by every
    retip, so a row's receipt described the ORIGINAL observation while its base
    described the latest one. A stale receipt is worse than none — it is a
    currency claim about a reading that never happened on this hop.

    AND A NAMED CONSTANT LISTING ALL FOUR WAS THE FIRST FIX, WHICH WAS DEAD ON
    ARRIVAL: this function never read it, so the tuple documented an intention
    nothing enforced and a mutation aimed at it changed nothing. The suite was
    green either way. What is left is the shas, which genuinely share one
    validator, and two fields that do not.

    The first cut ran every field through the sha pattern, which is correct for
    two of them and silently DROPS the other two: a receipt reads `local` and
    an instant reads like a timestamp, so replay would have kept the shas and
    lost exactly the freshness evidence this list was widened to carry — while
    the WRITER kept all four. A row that disagrees with its own replay is the
    defect this module keeps finding, and a per-field validator is what stops
    one list from meaning two things."""
    from . import vcs
    out = {}
    for key in _MOVING_SHAS:
        got = str(row.get(key) or "").lower()
        if _TIP.fullmatch(got):
            out[key] = got
    receipt = str(row.get("trunk_receipt") or "")
    if receipt in (vcs.TRUNK_LOCAL, vcs.TRUNK_FETCHED):
        out["trunk_receipt"] = receipt
    if _valid_ts(row.get("trunk_observed_at")):
        out["trunk_observed_at"] = str(row["trunk_observed_at"])
    return out


def _declared_authority(gitdir):
    """(ref, remote, sha, failure, receipt, seen_at) — the repository's
    DECLARED trunk authority, OBSERVED NOW.

    THE AUTHORITY IS DECLARED, NEVER DISCOVERED, and that is what ends a
    sequence rather than extending it. Four resolvers shipped before this one —
    hardcoded main|master, the branch's tracking remote, the configured merge
    ref, then git's own `@{upstream}` — and each closed exactly the
    counterexample in front of it while the next appeared inside the cure for
    the last. They were answers to a question with no answer: if configuration
    names the wrong remote, no heuristic can discover the metaphysical real
    one. A declared authority does not need discovering.

    TWO KEYS, READ FROM THE COMMON DIR. `helm.trunkRef` is the canonical
    SOURCE ref; `helm.trunkRemote` is the remote to observe it on, absent or
    `.` meaning THIS repository. Source rather than destination, because fetch
    refspecs map one to the other however the operator configured them and
    inferring across that mapping is the same discovery problem again.

    FRESHNESS IS NOT THIS FUNCTION'S TO INVENT — `vcs.observe_trunk_authority`
    owns it, and returns a typed receipt or UNKNOWN. A remote-tracking ref read
    locally is a SNAPSHOT and cannot testify to its own currency, so this never
    reads one.

    THREE SHAPES, and the callers that need proof must tell them apart.
    NOTHING DECLARED is (None, None, None, None) — a gap, the legacy row.
    DECLARED AND OBSERVED is a ref, its remote, and a sha. DECLARED AND FAILED
    is a ref, its remote, no sha, and a reason — a contradiction rather than a
    gap, and it must never be erased into the shape of the first."""
    from . import landreq, vcs
    common = landreq._git(gitdir, "rev-parse", "--path-format=absolute",
                          "--git-common-dir")
    root = common.stdout.strip() if common is not None \
        and common.returncode == 0 and common.stdout.strip() else gitdir

    def cfg(key, canon=lambda v: v):
        """(value, ambiguous, present) — the declaration `key` carries here.

        --local, BECAUSE THIS FUNCTION CLAIMS TO BE REPOSITORY-LOCAL. A bare
        `config --get` reads the whole stack, so one line in a user's
        ~/.gitconfig would declare a trunk authority for EVERY repository they
        touch — measured. An authority is a property of the repository, not of
        whoever happens to be running the command.

        --get-all, BECAUSE `--get` RETURNS THE LAST VALUE AND SAYS NOTHING
        ABOUT THE FIRST. A config file can carry the key twice, and a
        declaration that says two different things is AMBIGUOUS rather than
        equal to whichever git printed — silently picking one is the arbitrary
        choice this whole design exists to stop making.

        AN EMPTY VALUE IS A VALUE, AND DROPPING IT REOPENED THAT EXACT HOLE
        ONE LAYER DOWN. The first cut filtered blank lines out of `--get-all`
        before counting — a truthiness test standing in for a decision about
        meaning — so `helm.trunkRemote` declared twice as `` and `origin`
        arrived here as the single value `origin`: the local authority and a
        remote one, in conflict, silently resolved to the remote. Measured:
        git prints those two entries as `'\norigin\n'`, whose blank first
        line IS the empty declaration. PRESENCE IS THE RETURN CODE, never the
        emptiness of what was printed — rc 1 is the absent key, rc 0 with a
        blank line is a key present and empty, and those are different facts
        about the repository. `canon` folds the spellings that genuinely mean
        one thing BEFORE the uniqueness test, so equivalence and conflict are
        decided on meaning rather than on bytes.
        """
        got = landreq._git(root, "config", "--local", "--get-all", key)
        if got is None or got.returncode != 0:
            return "", False, False
        values = {canon(v) for v in got.stdout.splitlines()}
        if len(values) > 1:
            return "", True, True
        return (values.pop() if values else ""), False, True

    ref, ref_ambiguous, ref_present = cfg(_TRUNK_REF_KEY)
    # `.`, EMPTY AND ABSENT ARE ONE AUTHORITY — this repository's own
    # refs/heads — canonicalized BEFORE the uniqueness test so two spellings of
    # it read as agreement and a genuine remote beside it reads as conflict.
    # Persisting `.` while comparing a raw string made toggling `.` to unset
    # read as a MIGRATION and refuse.
    remote, remote_ambiguous, _ = cfg(_TRUNK_REMOTE_KEY,
                                      lambda v: "" if v == "." else v)
    if ref_ambiguous or remote_ambiguous:
        return ref or "<ambiguous>", None, None, (
            "the repository declares its trunk authority more than once with "
            "different values, so which one it means cannot be read"
        ), None, None
    # ABSENT IS A GAP; PRESENT-AND-EMPTY IS A CONTRADICTION. The legacy row
    # never declared anything and is owed the gap shape. A repository that
    # wrote the key and left it blank declared a trunk source nobody can read,
    # and repairing that into "never declared" is the same silent choice.
    if not ref_present:
        return None, None, None, None, None, None
    if not ref:
        return "", remote or None, None, (
            "the repository declares %s with an empty value, which names no "
            "branch — unset the key to declare no authority" % _TRUNK_REF_KEY
        ), None, None
    bad = vcs.canonical_source_refusal(ref)
    if bad:
        return ref, remote or None, None, bad, None, None
    sha, receipt, failure = vcs.observe_trunk_authority(root, ref,
                                                        remote or None)
    return ref, remote or None, sha, failure, receipt, pk.now_ts()


def _authority_binding(gitdir, tip):
    """The fields a dispatch row carries so a later retip need not DISCOVER
    trunk: the declared authority, the SHA observed for it now, and this tip's
    fork point under that authority.

    A SEND IS NEVER REFUSED FOR LACK OF A BINDING. Fast-forward retips need
    none, and refusing every dispatch in an unbound repository is a far wider
    blast radius than the rule requires; the cost lands where the proof is
    actually needed, on a non-FF BUILD retip. But A DECLARATION THAT FAILED TO
    OBSERVE IS STILL PERSISTED, without a sha — erasing it would make a
    repository that DECLARED an authority indistinguishable from one that never
    did, and those two states owe the reader different sentences."""
    ref, remote, sha, _failure, receipt, seen_at = _declared_authority(gitdir)
    # PRESENCE, NOT TRUTHINESS — the same distinction the parser below this
    # one had to learn, one layer out. `None` is the repository that declared
    # nothing and is owed the gap. `""` is a repository that DECLARED its
    # trunk source and left it blank, and collapsing that into the gap makes
    # the row say it PREDATES authority binding — a sentence about the row's
    # AGE, drawn from a fact about the operator's config, and false.
    if ref is None:
        return {}
    out = {"trunk_ref": ref}
    if remote:
        out["trunk_remote"] = remote
    if not sha:
        return out
    # THE RECEIPT IS THE POINT, NOT DECORATION. A typed local-vs-fetched
    # answer was being computed and then dropped, leaving a row that recorded
    # WHAT was observed and not HOW — so a sha fetched from a remote and a sha
    # read out of a local branch were indistinguishable afterwards, which is
    # exactly the distinction the freshness argument turns on. The instant is
    # the OBSERVATION's, taken by the observer: the event's own `ts` is
    # stamped before the binding runs and would date the write rather than
    # the reading.
    out["trunk_sha"] = sha
    out["trunk_receipt"] = receipt
    out["trunk_observed_at"] = seen_at
    from . import landreq
    got = landreq._git(gitdir, "merge-base", tip, sha)
    base = got.stdout.strip() if got is not None and got.returncode == 0 \
        and got.stdout.strip() else ""
    if _TIP.fullmatch(base.lower()):
        out["base_sha"] = base
    return out


def _retip_fork(gitdir, tip, trunk_sha):
    """`tip`'s fork point under the observed authority, or None."""
    from . import landreq
    got = landreq._git(gitdir, "merge-base", tip, trunk_sha)
    out = got.stdout.strip() if got is not None and got.returncode == 0 else ""
    return out if _TIP.fullmatch(out.lower()) else None


def _retip_identity(row, new_tip):
    """(identity, refusal) — is `new_tip` the SAME WORK this row already names?

    THREE ANSWERS, TWO ADMISSION OUTCOMES. "verified" means the substrate
    proved the identity. A mismatch and UNKNOWN both refuse, with different
    reasons: missing patch-id evidence is not proof of different work, but
    neither can it authorize moving the brief. Restore the proof and retry,
    or use a fresh dispatch with --supersedes to state the new obligation.
    Historical unverified hops still replay as recorded; new writes require
    verified identity.

    KIND-AWARE, because the two kinds name different work. A REVIEW row's work
    is the per-commit ORDERED patch-id sequence its tip adds to trunk —
    patch-id, not sha, because a rebase is the whole reason retip exists (every
    sha changes, no patch does), and per-commit ordered because a lane that
    lost or reordered a commit is not the same work wearing a new base. The
    row's sequence may be one contiguous run inside a larger integration train:
    unrelated reviewed cars before or after it do not change what this reviewer
    was asked to read. A repeated run refuses as ambiguous rather than choosing
    which identical spelling is "the" car. The ordered walk and containment
    verdict are one VCS-owned primitive against the row's recorded repository,
    so retip and gate binding cannot drift into two meanings of the same proof.

    A BUILD row's ref is a BASE, so a fast-forward verifies on ancestry alone.
    BUT ANCESTRY WAS THE ONLY ANSWER IT HAD, AND A REBASE CAN NEVER PRODUCE A
    DESCENDANT — so the one operation the fold procedure REQUIRES of every
    author (rebase onto the new trunk, then gate) was the one operation this
    verb could not express, and each one cost a cancel-and-re-cut that
    fragments a chain the substrate could have proved continuous (measured
    2026-08-25, twice in one night on one lane, task/1463).

    THE BUILD ARM ASKS A DIFFERENT QUESTION FROM THE REVIEW ARM, and the split
    is the meld's ruling rather than a convenience. What a build row's
    recipient inherits is the FINAL CONTENT on the new base; what a review
    row's reader read is the HISTORY. So this REPLAYS the old lane's work onto
    the new base and compares the emitted tree to the new tip's own
    (`_retip_replay_tree`), while the review arm below keeps the ordered
    per-commit walk. A history-only REORDER whose result is byte-identical is
    ACCEPTED here and refused there; a MERGE that resolved a conflict by hand
    cannot be lost, because the replay carries whatever the merge produced.

    AND NO PATCH ID ANYWHERE IN BUILD IDENTITY, because a diff hash cannot bind
    LOCATION. The cut before this compared one aggregate `--verbatim` diff per
    side, and review broke it: a file carrying the same block WITH THE SAME
    SURROUNDING CONTEXT at two places gives ONE id for an edit to either copy,
    under `--stable` and `--verbatim` alike, while the trees differ. Reproduced
    here, both modes colliding, and pinned as an arm. A stronger hash does
    not help; location is precisely what a patch id discards.

    AND UNKNOWN REFUSES ON THIS ARM. An unanswerable ancestry, an unreadable
    fork point and an uncomputable delta all turn the retip away. The first cut
    returned `unverified` — proceed — for unanswerable ancestry, which made the
    WEAKER signal more permissive than a known non-descendant and left "make
    ancestry unanswerable" as the way past the guard. An EMPTY range is the
    same answer here for the same reason it is on the review arm: two tips that
    add nothing to their bases have no delta to compare, and blessing that
    would let any trunk commit stand in for any other.

    A PROVEN REBASE IS STILL A CHANGE THE RECIPIENT MUST SEE — the delta is
    identical and THE TREE UNDER IT IS NOT — so it returns a third value AND
    the writer persists `base_replaced` on the event and the hop. The
    notification alone lost the fact permanently whenever the post failed after
    a successful append, or on an exact retry that reconciles. The stamp itself
    stays the two-value enum the fold accepts: widening it would make an older
    reader drop the whole hop in SILENCE, while an unknown extra FIELD is
    ignored by that same reader with the hop still applied.

    AN EMPTY REVIEWED SEQUENCE CAN NEVER VERIFY. It identifies no car, so every
    candidate contains it everywhere and nowhere; treating that as containment
    would let any trunk commit inherit any other trunk commit's brief. The VCS
    primitive reports EMPTY distinctly and this review arm refuses it."""
    old_tip = str(row.get("tip") or "")
    gitdir = str(row.get("repo_id") or "")
    from . import landreq
    # ONE AUTHORITY, OBSERVED ONCE, FOR THE BUILD ARM. Review identity is now
    # relative to the two artifacts' unique common base and needs no moving
    # trunk; build direction still consumes the repository's declared authority.
    # OBSERVED LAZILY, ONCE. Hoisting this to the top made the BUILD
    # FAST-FORWARD path pay for an authority it never asks about — measured at
    # four git calls including two config reads on a plain FF retip, which is a
    # network round trip on a remote authority. The memo keeps the
    # observe-once property that removed a mid-check fetch race, without
    # charging the path that returns before ever needing it.
    _observed = {}

    def authority():
        if not _observed:
            ref, remote, sha, failure, receipt, seen = \
                _declared_authority(gitdir)
            _observed.update(ref=ref, remote=remote, sha=sha, failure=failure,
                             receipt=receipt, seen=seen)
        return _observed

    def base_of(tip):
        """The tip's fork point under the observed authority; None = UNKNOWN."""
        sha = authority()["sha"]
        return _retip_fork(gitdir, tip, sha) if sha else None

    if row.get("kind") == "build":
        # THE FAST PATH FIRST, AND IT COSTS NO AUTHORITY. An ordinary
        # fast-forward retip is answered by ancestry alone; observing trunk
        # ahead of it spent a network round trip on a question it never asks.
        relation = landreq._ancestry(gitdir, old_tip, new_tip)
        if relation == landreq.ANCESTOR:
            return "verified", None, None, None

        # CONTENT IS TRUNK-FREE, and it is the half that can always be
        # answered. The two tips' own fork point needs no authority, no
        # config, no remote and no HEAD — which is why four rounds of
        # trunk-resolution holes never touched it. `merge-base(old, new)`
        # lands BELOW the new tip's real base whenever trunk moved between
        # them, so the new side's COMMIT SET is contaminated; that is true and
        # it does not reach this, because those commits are already inside the
        # new tip and merging the old lane's work into it cannot be fooled by
        # them. Measured on a real fold: 17 commits against 19, exact tree
        # match.
        # --all, BECAUSE A CRISS-CROSS HISTORY HAS MORE THAN ONE BEST BASE
        # and picking one of them arbitrarily makes the replay's answer depend
        # on which git happened to name. A plain `merge-base` prints exactly
        # one line and looks unambiguous whether or not it is; asking for all
        # of them is the only way to see the difference. Several bases is an
        # ambiguous question, so it is UNKNOWN and refuses rather than being
        # decided by an arbitrary pick.
        fork = landreq._git(gitdir, "merge-base", "--all", old_tip, new_tip)
        bases = [b for b in (fork.stdout.split() if fork is not None
                             and fork.returncode == 0 else [])
                 if _TIP.fullmatch(b.lower())]
        pair_base = bases[0] if len(bases) == 1 else ""
        replayed = _retip_replay_tree(gitdir, pair_base, new_tip, old_tip) \
            if pair_base else None
        wanted = _retip_tree_of(gitdir, new_tip) if replayed else None
        if replayed is None or wanted is None or replayed != wanted:
            return None, _retip_build_refusal(row, old_tip, new_tip, replayed,
                                              wanted, relation), None, None

        # DIRECTION IS THE ONE AUTHORITY QUESTION, AND THE ROW ALREADY CARRIES
        # ITS ANSWER'S BASE. Content equality says the recipient inherits the
        # same work and says NOTHING about whether the base moved forward:
        # when trunk's movement is TREE-NEUTRAL both spellings have identical
        # content and only the BASE is older. Measured, and it is why UNKNOWN
        # here REFUSES rather than degrading — a row that moves on
        # proven-content, unproven-direction re-points the recipient at an
        # older base, which is the fail-open this predicate keeps curing.
        # Compatibility is not authority.
        bound = "trunk_ref" in row
        stored_ref = str(row.get("trunk_ref") or "")
        stored_base = str(row.get("base_sha") or "").lower()
        stored_remote = str(row.get("trunk_remote") or "")
        why = None
        if not bound:
            why = ("this row carries NO TRUNK AUTHORITY BINDING, so whether "
                   "the base moved FORWARD cannot be proven. Rows written "
                   "before the binding read this way by construction — cancel "
                   "+ a fresh dispatch (--supersedes %s) re-binds it"
                   % row["id"][:12])
        elif not stored_ref:
            # THE ROW IS BOUND TO A CONTRADICTION, NOT MISSING A BINDING, and
            # the difference is the whole reason the field is kept: the
            # sentence above blames the row's AGE and would send its author
            # to re-dispatch, which mints an identical row against the same
            # unreadable config. This one names the config.
            why = ("this row was dispatched under %s declared with an EMPTY "
                   "value, which names no branch. The repository contradicted "
                   "itself at dispatch and the row PRESERVED that rather than "
                   "reading as one written before the binding — fix the "
                   "declaration, then cancel + a fresh dispatch (--supersedes "
                   "%s)" % (_TRUNK_REF_KEY, row["id"][:12]))
        elif not _TIP.fullmatch(stored_base):
            why = ("this row declares authority %s but recorded no base under "
                   "it, so there is nothing to compare the new fork point "
                   "against" % stored_ref)
        elif authority()["failure"] or not authority()["sha"]:
            why = ("%s. The repository DECLARED an authority and it could not "
                   "be honoured, which is a contradiction rather than a gap"
                   % (authority()["failure"]
                      or "its declared trunk authority is gone"))
        elif (authority()["ref"], authority()["remote"] or "") != \
                (stored_ref, stored_remote):
            # AN AUTHORITY IS A REF AND THE REMOTE IT LIVES ON. Comparing the
            # ref alone let `origin` become `.` — the same branch name over a
            # different authority — and read as unchanged.
            why = ("this row is bound to authority %s%s while the repository "
                   "now declares %s%s. An authority CHANGE is a migration, "
                   "not a re-point"
                   % (stored_ref, (" on " + stored_remote) if stored_remote
                      else " (local)", authority()["ref"],
                      (" on " + authority()["remote"])
                      if authority()["remote"] else " (local)"))
        if why:
            return None, (
                "retip REFUSED: %s delivers the same content as %s, but %s. "
                "The content is known and the DIRECTION is not, and a base "
                "that silently moved BACKWARD hands the recipient a stale "
                "tree. Nothing was changed"
                % (new_tip[:12], old_tip[:12], why)), None, None
        new_base = base_of(new_tip)
        if new_base is None:
            return None, (
                "retip REFUSED: %s delivers the same content as %s, but its "
                "fork point under %s could not be read, so the direction of "
                "the base is unproven. Nothing was changed"
                % (new_tip[:12], old_tip[:12], stored_ref)), None, None
        if stored_base == new_base:
            return "verified", None, None, {
                "trunk_sha": authority()["sha"],
                "trunk_receipt": authority()["receipt"],
                "trunk_observed_at": authority()["seen"]}
        if landreq._ancestry(gitdir, stored_base,
                             new_base) != landreq.ANCESTOR:
            return None, (
                "retip REFUSED: %s delivers the same content as %s, but its "
                "fork point %s is not provably forward of the base this row "
                "was dispatched against, %s. Content equality says the work "
                "is the same and says nothing about which trunk it sits on. "
                "Nothing was changed"
                % (new_tip[:12], old_tip[:12], new_base[:12],
                   stored_base[:12])), None, None
        return "verified", None, (
            "the base was REBASED, not fast-forwarded — replaying this work "
            "onto %s reproduces its tree %s exactly, fork point %s -> %s under "
            "declared authority %s observed at %s. The work is the same; THE "
            "TREE UNDER IT IS NOT. Rebase work in progress onto the new tip "
            "before continuing"
            % (new_tip[:12], wanted[:12], stored_base[:12], new_base[:12],
               stored_ref, authority()["sha"][:12])), {"base_sha": new_base,
                                          "trunk_sha": authority()["sha"],
                "trunk_receipt": authority()["receipt"],
                "trunk_observed_at": authority()["seen"]}

    from . import vcs
    state, _start, old_n, new_n = \
        vcs.backend(gitdir).patch_sequence_containment(
            gitdir, old_tip, new_tip)
    if state in (vcs.PATCH_SEQUENCE_EXACT,
                 vcs.PATCH_SEQUENCE_CONTAINED):
        return "verified", None, None, None
    if state == vcs.PATCH_SEQUENCE_UNKNOWN:
        return None, (
            "retip REFUSED: review identity UNKNOWN — the ordered patch-id "
            "sequence from %s to %s could not be verified. Missing proof is "
            "not a match or a mismatch. Nothing was changed. Restore the "
            "proof and retry, or send a fresh dispatch (--supersedes %s) "
            "to state the new obligation"
            % (old_tip[:12], new_tip[:12], row["id"][:12])), None, None
    # A REFUSAL NAMES THE INPUT THAT FAILED, AND BOTH COUNTS ABOVE ARE
    # MEASURED FROM THE TWO TIPS' COMMON BASE — so when one tip is an ANCESTOR
    # of the other, that base IS one of the tips and its own count collapses
    # to zero. Reporting the collapse as the finding described an artifact of
    # the measurement instead of the input: a reader told "the old 0-commit
    # sequence is empty, so it identifies no reviewed work" goes and measures
    # the reviewed tip against trunk, finds its commits intact, and concludes
    # the instrument is broken. The reviewed tip is FINE in both directions
    # below; what failed is the tip handed to --ref. Measured on an append of
    # one commit to a one-commit lane: the sentence said 0-commit and 1-commit
    # about tips that are one and two commits off trunk — three numbers in one
    # sentence, none of which the reader can reproduce.
    #
    # BOTH DEGENERATE DIRECTIONS ARE ALREADY IN HAND and cost no further git
    # call. The needle is `base..old` and the train is `base..new` over the
    # unique merge base, so an EMPTY needle means the base IS the old tip —
    # the new tip descends from it — and `new_n` then counts exactly the
    # commits it adds. An ABSENT verdict over an EMPTY train means the base IS
    # the new tip — it is an ancestor of the reviewed one — and `old_n` then
    # counts exactly the reviewed commits it drops. Empty on BOTH sides is
    # EXACT and returned above, so the two cases cannot overlap.
    if state == vcs.PATCH_SEQUENCE_EMPTY:
        return None, (
            "retip REFUSED: --ref %s DESCENDS from the reviewed tip %s, "
            "adding %s commit(s) this row never asked anyone to read. The "
            "reviewed patches are intact and nothing about them failed — "
            "retip moves a reviewed car to a new BASE, and work stacked on "
            "top of it is a LARGER obligation, not a moved one. Nothing was "
            "changed. Send a fresh dispatch (--supersedes %s), which keeps "
            "the chain, to put the added commits under review"
            % (new_tip[:12], old_tip[:12], new_n, row["id"][:12])), None, None
    if state == vcs.PATCH_SEQUENCE_ABSENT and not new_n:
        return None, (
            "retip REFUSED: --ref %s is an ANCESTOR of the reviewed tip %s, "
            "dropping the %s commit(s) on top of it that this row asked for. "
            "A retip re-points the SAME work at a new base and can never "
            "shrink what was dispatched. Nothing was changed. Send a fresh "
            "dispatch (--supersedes %s) if the shorter tip is the real "
            "obligation"
            % (new_tip[:12], old_tip[:12], old_n, row["id"][:12])), None, None
    detail = {
        vcs.PATCH_SEQUENCE_AMBIGUOUS:
            "appears more than once, so the reviewed car is ambiguous",
        vcs.PATCH_SEQUENCE_ABSENT:
            "does not appear as one contiguous run",
    }.get(state, "could not be classified")
    # THE SURVIVING SENTENCE NAMES --ref TOO, AND IT NAMES THE BASE ITS COUNTS
    # ARE MEASURED FROM. Neither tip is an ancestor of the other here, so both
    # counts are real commit counts — but they are counted from the PAIR'S
    # common base, which is the base of neither tip, so a reader who measures
    # either tip against trunk gets a different number for a refusal that is
    # correct. Naming the base is what makes the two numbers checkable.
    fork = landreq._git(gitdir, "merge-base", old_tip, new_tip)
    got = fork.stdout.strip().lower() \
        if fork is not None and fork.returncode == 0 else ""
    base = (" " + got[:12]) if _TIP.fullmatch(got) else ""
    return None, ("retip REFUSED: --ref %s does not carry the work reviewed "
                  "at %s — measured from the two tips' common base%s, the "
                  "reviewed %s-commit patch-id sequence %s in the new tip's "
                  "%s-commit history. retip may carry a reviewed car into a "
                  "larger train, but may never invent, reorder, or choose an "
                  "ambiguous copy of that car. Nothing was changed. Use "
                  "cancel + a fresh dispatch (--supersedes %s) if this really "
                  "is new work"
                  % (new_tip[:12], old_tip[:12], base, old_n, detail, new_n,
                     row["id"][:12])), None, None


def retip(rid, ref, reason=None, repo=None, notify=True):
    """Re-point one OPEN dispatch at a NEW TIP — same row, same recipient,
    same obligation, same chain. The mirror of rebind for the case where the
    BASE moved rather than the reviewer: measured 2026-08-02, six times in one
    day by one seat, once per land that moved trunk under an already-dispatched
    lane, each a hand-composed cancel-and-resend a reader then had to diff
    against the last.

    ONE EVENT, NOT A CANCEL+ADD PAIR — the deliberate departure from the first
    build of this verb (lane/dispatch-retip-a-moved-tip), and from rebind,
    whose shape it borrowed. That build closed the old row as retipped and
    minted a successor via --supersedes, which mis-states what happened three
    ways under the chain law: it burns a chain hop on work that did not round
    (the chain exists to say WHICH ROUND continued which, and a rebase is not a
    round); it retires the row id the recipient is already watching; and its
    two writes can strand the obligation between them — the exact crash window
    its own docstring mis-described as recoverable. A retip is an explicit
    re-point of the OPEN row's ref with an audit event, never a new row and
    never a silent rewrite: the seq-0 event keeps the original tip forever and
    the projection lists every hop in `retips`.

    WHY ONLY WHILE OPEN, and this is the whole guard: a VERDICT BINDS A TIP.
    Re-pointing a verdicted row would move a reviewer's binding onto code they
    never read — the exact thing a review refused a countersign over ("a DM and
    a 3/3 range-diff do not retarget that verdict"). An OPEN row has no
    verdict by construction, so the status check IS the verdict check. The
    replay arm enforces the same gate, so even a hand-appended retip event
    after a verdict is inert.

    EVERY REFUSAL HAPPENS BEFORE THE ONE WRITE — the fix on the first
    build, made structural: that build's first cut cancelled FIRST and
    validated nothing, so a made-up --ref cancelled a live obligation and
    then failed to replace it (done to another seat's open review
    while probing the verb). With a single event there is nothing to strand:
    a refusal writes nothing, and an append failure changes nothing.

    COMPOSES WITH THE DUPLICATE-SUCCESSOR LAW rather than tripping it: a row
    that already has a LIVE successor superseding it is not retipped — its
    obligation has demonstrably moved to the child, and re-pointing the parent
    would stand up a second live frontier for the same work, the precise
    duplicate `_duplicate_mint_warning` exists to refuse. A CLOSED successor
    does not block: the continuation ended and the still-open parent is again
    the one frontier. And an UNREADABLE frontier refuses the same way
    (P1, r2): a not-closed row whose supersedes replays CHAIN_UNKNOWN
    might name this row, and a check that could not look must never answer
    as if it had — `_successor_frontier` is the one owner of that read, for
    the writer here and the replay arm both.

    Git-facing validation (tip resolution, work identity) runs OUTSIDE the
    ledger lock — subprocess seconds must not serialize every other writer —
    and the ledger facts are then RE-PROVEN under the lock before the append:
    the row must still be open, still at the tip the identity check blessed,
    and still successor-free. A concurrent verdict, cancel, or retip between
    the check and the lock is a refusal, never a mis-sequenced event."""
    current, unavailable = snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = _resolve_row(current, rid)
    if err:
        return None, err
    if not _TIP.fullmatch(str(row.get("tip") or "")):
        # THE WITNESS GUARD (r3): a hop must anchor to the tip it
        # moves, and replay refuses any hop it cannot bind to its own derived
        # state — so a row with no derivable tip (a legacy needs-redispatch
        # shape) could only ever mint an event the fold refuses, a committed
        # write that replays inert. Refuse HERE, with the name, before
        # anything is written. Measured before adding (2026-08-04): without
        # this guard the writer proceeded — None == None passed the
        # under-lock tip re-check — and old replay ACCEPTED the anchorless
        # hop ("" == ""), so the writer was NOT already refusing.
        return None, ("retip REFUSED: dispatch %s has no derivable current "
                      "tip (a legacy needs-redispatch row) — a retip hop "
                      "must anchor to the tip it moves, and replay refuses "
                      "an anchorless hop; re-dispatch the obligation with a "
                      "real --ref instead. Nothing was changed" % row["id"])
    ref = (ref or "").strip()
    if not ref:
        return None, "retip needs the NEW tip (--ref)"
    reason, rerr = _clean(reason, "retip reason", 256)
    if rerr:
        return None, rerr
    if not reason:
        return None, ("retip needs a reason (--reason) — the recipient is "
                      "being asked to re-read, and 'why' is the difference "
                      "between a rebase and a rewrite")
    # Repo identity is IMMUTABLE for the same obligation, exactly as in
    # rebind: --repo may only spell an alternate path to the repo already
    # recorded on the row; the RECORDED root is what the git checks run
    # against either way (re-resolving the caller's path would be a check/use
    # hole). Rows older than repo_id carry none and keep the caller's path.
    repo_path = str(row.get("repo_id") or "")[:-5] or None
    if repo is not None:
        want = row.get("repo_id")
        info = _repo_info(repo) if want else None
        if want and (not info or info["repo_id"] != want):
            return None, ("retip REFUSED: --repo %s resolves to %s but the "
                          "obligation is bound to %s — the same obligation "
                          "cannot change repos"
                          % (repo, (info or {}).get("repo_id", "no git repo"),
                             want))
        if not want:
            repo_path = repo
    new_tip, _nb = _resolve_tip(repo_path, ref)
    if not new_tip:
        return None, ("retip REFUSED: %s does not resolve to a commit in %s — "
                      "a tip that does not exist cannot carry an obligation; "
                      "nothing was changed" % (ref[:40], repo_path or "this repo"))
    if new_tip == row.get("tip"):
        # IDEMPOTENT ON THE EXACT RETRY (a FIX on this verb's first cut,
        # P2), the same
        # law mark_cancel and the duplicate-send guard already keep: a
        # committed write whose RESPONSE was lost gets re-run, and the re-run
        # must reconcile onto the achieved state, not refuse it. The row's own
        # last hop is the receipt — same tip AND same reason is this exact
        # operation already applied, returned without another append (and
        # without re-notifying: never-send-again). AND RECONCILIATION RUNS
        # BEFORE THE OPEN-ONLY GATE (P2, r2): a retry is DELAYED
        # by construction, so the row may have gone terminal since the
        # committed write, and the gate used to refuse the exact retry it
        # exists to protect nothing from — reconciliation is a READ of
        # achieved state, a verdict or cancel landing later cannot un-achieve
        # it, and the gate below still guards every path that would WRITE.
        # Anything else at the same tip is a genuine no-op (or a closed row)
        # and still refuses.
        hops = row.get("retips") or ()
        last = hops[-1] if hops else None
        if last and last.get("tip") == new_tip \
                and last.get("reason") == reason:
            out = dict(row)
            out["identity"] = last.get("identity")
            return out, None
    if row["status"] != "open":
        return None, ("dispatch %s is %s — only an OPEN row can be retipped, "
                      "because a verdict BINDS the tip it was written against "
                      "and re-pointing a closed row would retarget someone's "
                      "review onto code they never read"
                      % (row["id"], row["status"]))
    if new_tip == row.get("tip"):
        return None, ("dispatch %s already names %s — nothing to re-point"
                      % (row["id"], new_tip[:12]))
    identity, why, how, rebound = _retip_identity(row, new_tip)
    if why:
        return None, why
    path = ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) — retip NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        live = current.get(row["id"])
        # THE RE-READ THE WRITE BINDS IS WHERE THIS WRITER'S SEQ COMES FROM,
        # so the vocabulary is checked on IT; the resolve above read an
        # earlier snapshot, and an event this helm cannot read may have
        # landed since.
        refusal = unknown_kinds_refusal(live)
        if refusal:
            return None, refusal
        if not live or live.get("status") != "open" \
                or live.get("tip") != row.get("tip"):
            return None, ("dispatch %s moved while retip was validating "
                          "(now %s at %s) — nothing was changed; re-run "
                          "against its current state"
                          % (row["id"],
                             (live or {}).get("status", "gone"),
                             str((live or {}).get("tip") or "?")[:12]))
        successors, unknown = _successor_frontier(current, row["id"])
        if successors:
            return None, ("retip REFUSED: OPEN successor%s %s already "
                          "supersede%s %s — the obligation has a live "
                          "continuation, and re-pointing this row would stand "
                          "up a second live frontier for the same work (the "
                          "duplicate-successor law). Act on the successor "
                          "instead" % ("s" if len(successors) != 1 else "",
                                       ", ".join(s[:12] for s in successors[:3])
                                       + (", …" if len(successors) > 3 else ""),
                                       "" if len(successors) != 1 else "s",
                                       row["id"][:12]))
        if unknown:
            return None, ("retip REFUSED: successor state UNREADABLE — "
                          "not-closed row%s %s carr%s a malformed supersedes "
                          "(chain UNKNOWN), so whether an open successor "
                          "already holds this obligation cannot be answered, "
                          "and a frontier that could not be read never reads "
                          "as clear. Nothing was changed; repair or close "
                          "th%s row%s and re-run"
                          % ("s" if len(unknown) != 1 else "",
                             ", ".join(u[:12] for u in unknown[:3])
                             + (", …" if len(unknown) > 3 else ""),
                             "y" if len(unknown) != 1 else "ies",
                             "ose" if len(unknown) != 1 else "at",
                             "s" if len(unknown) != 1 else ""))
        # `base_replaced` IS PERSISTED, and it is NOT a third identity value.
        # A proven rebase swaps the TREE under identical patches, so work in
        # progress may no longer apply — the recipient must be told. The first
        # cut told them in the NOTIFICATION ONLY, which loses the fact
        # permanently if the post fails after the append, or on an exact retry
        # that reconciles onto the committed write without re-notifying
        # (measured). Widening the identity enum would have been the
        # obvious home and is the wrong one: `_new_state` returns state
        # UNCHANGED on an identity it does not know, so an older reader would
        # drop the whole hop in SILENCE. An unknown extra FIELD is ignored by
        # that same reader while the hop still applies.
        event = {"v": 3, "event": "retip", "seq": live["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "tip": new_tip,
                 "ref": ref, "old_tip": live["tip"], "reason": reason,
                 "identity": identity, "base_replaced": bool(how),
                 # THE ROW'S BASE MOVES WITH IT. Without this a SECOND retip
                 # would prove its direction against the base of the ORIGINAL
                 # dispatch rather than the one this hop just established, so a
                 # lane rebased twice would compare the third tip to the first
                 # base. The observed authority sha rides along as the instant
                 # the direction was proven at.
                 **{k: v for k, v in (rebound or {}).items() if v}}
        # NO `proof` FIELD, deliberately (r3): the round-2 stamp
        # was an unkeyed content hash a forger recomputes over their own
        # fields, and the fold now refuses to read one — the writer stamps
        # nothing the acceptance path is pinned to ignore.
        if not txn.append(event):
            return None, "ledger unwritable (%s) — retip NOT recorded" % path

        def finish():
            out = dict(live)
            hops = list(live.get("retips") or ())
            hops.append({"old_tip": live.get("tip"), "old_ref": live.get("ref"),
                         "tip": new_tip, "ts": event["ts"], "reason": reason,
                         "identity": identity,
                         "base_replaced": bool(event.get("base_replaced"))})
            out.update(_moving_binding(event))
            out.update(tip=new_tip, ref=ref, retips=hops, seq=event["seq"],
                       identity=identity)
            # THE SAME STALING THE FOLD APPLIES, so the returned row is the row a
            # reader replays. The writer's in-memory answer diverging from replay is
            # the two-spellings-of-one-fact defect this module keeps catching; a caller
            # acting on `out` would see a delivery state the ledger disagrees with.
            if live.get("delivery") == "observed":
                out["delivery"] = "needs-confirmation"
            pk.event("dispatch-retip", row["id"],
                     "%s -> %s [%s]" % (event["old_tip"][:12], new_tip[:12], identity))
            # A NEW TIP IS NEW CODE TO READ: the note on the old tip no longer
            # describes what the reviewer will adjudicate.
            _queue_findings_pass(out)
            if notify:
                _notify_public(out, "RETIPPED %s -> %s [identity %s]: %s%s"
                               % (event["old_tip"][:12], new_tip[:12], identity,
                                  reason, (" — " + how) if how else ""))
            return out, None
        return txn.then(finish)
    return _ledger_write(attempt, path)


def _age_s(row, now=None):
    try:
        stamp = time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ")
        return max(0, int((time.time() if now is None else now)
                          - calendar.timegm(stamp)))
    except (OverflowError, ValueError, TypeError):
        return 0


def _read_stamp(now=None):
    """The instant a listing was READ, in the ledger's own UTC spelling.

    A REPORT NEEDS THE SAME HONESTY A ROW DOES. The fleet renders per-row ages
    everywhere precisely so a reader can tell how old a fact is; a listing that
    carries no read instant hands the reader relative ages with NO ORIGIN to
    measure them from, so the whole snapshot silently re-anchors to whenever it
    is read next.

    THE ABSENCE CLAIM IS THE ONE THAT BITES. "no land loops in flight" quoted
    forward reads as a standing fact about the board, and it is the reading an
    integrator acts on by standing down — hours after it stopped being true.

    Deliberately the SAME format as a row's `ts` (%Y-%m-%dT%H:%M:%SZ, UTC): the
    stamp is quoted into chat beside row timestamps, and a second spelling is
    one more thing for a reader to mis-compare."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() if now is None else now))


def open_rows(snap=None):
    snap = rows() if snap is None else snap
    out = owed(snap)
    return sorted(out, key=lambda r: (str(r.get("ts") or ""), r["id"]))


WORKING = "working"          # the recipient holds a live claim on this work
IDLE = "idle"                # readable claims, none of them this row's
PROGRESS_UNKNOWN = "unknown"  # the claims ledger could not be read


def _repo_project(repo_id):
    """Canonical project token from the current writer's standard Git identity.

    THE TOKEN'S OWNER IS THE CLAIM MINTER, so this DELEGATES to
    `work._lanes.project_token` instead of re-deriving the same basename.
    `_lanes.resource` builds the very resource this feeds
    (`worktree:<token>:<lane>`), and two independent derivations of one lease
    key is a mismatch waiting to happen — on the day they disagree
    `progress_state` reads IDLE for a seat that IS holding the dispatched lane,
    and the row reads overdue while the work is being done.

    AND THAT IS WHY IT IS NOT ROUTED THROUGH THE REGISTRY (task/2437). The
    obvious cure for the collision below is `_project_of`, the mapper the write
    door and the board now share, and here it would BREAK the match wherever a
    registered project's NAME differs from its repo directory's basename,
    because the CLAIM is still minted under the basename. The two agree today
    only because both sides are the repo root's basename.

    THE NON-INJECTIVITY IS REAL AND STAYS FOR NOW: basename-of-dirname collides
    on the live ledger — a project's checkout and a FORK of it kept elsewhere
    have equal directory basenames, so both gitdirs yield one token — so two
    repositories share one lease namespace. Curing it
    moves the CLAIMS key on both sides and owes a migration for live claims;
    filed as its own row (named in this commit's body) rather than smuggled in
    behind a read-side cure.

    Replayed rows may carry malformed historical data, including path bytes
    Python refuses to normalize. That is never positive progress evidence: an
    unbindable repository stays noisy rather than crashing or shielding a row."""
    if not os.path.isabs(repo_id) or os.path.basename(repo_id) != ".git":
        return None
    try:
        if os.path.realpath(repo_id) != repo_id:
            return None
    except (OSError, ValueError):
        return None
    from .work import _lanes
    return _lanes.project_token(os.path.dirname(repo_id)) or None


# Git's own `.git`-file grammar, stated where it is parsed: `write_file(...,
# "gitdir: %s", path)` plus the newline write_file appends, and a reader that
# requires the 8-byte prefix and takes the REST as one pathname. Not a line
# format — a path may legally contain a newline.
_GITDIR_PREFIX = "gitdir:"
# The cap is a REFUSAL threshold, not a truncation: PATH_MAX is 4096 on Linux
# and a pointer beyond twice that is not a pathname this reader will guess at.
_POINTER_MAX = 8192


def _lane_claim_binds(repo_id, claim_lane):
    """Does the lane room `claim_lane` names belong to THIS row's repository?

    THE LANE TOKEN IS NOT INJECTIVE AND THE CLAIM IS THE ONLY THING THAT WAS
    ASKED (task/2437 round two, finding 7). `_repo_project` is the repo root's
    BASENAME — deliberately, because the CLAIM is minted under that basename and
    a second derivation would desync the two — so a project's checkout and a
    fork of it kept elsewhere yield ONE token and share one lease namespace.
    While the write door was an equality test only helm's own rows existed and
    the collision could not be reached from the ledger. Registry admission
    reached it: a seat holding `worktree:<basename>:<lane>` in a FORK now
    suppresses the overdue verdict on a row belonging to the OTHER repository of
    that name, and the row reads as being worked while nobody is working it —
    a silent alarm, which is the one direction `_is_overdue`'s docstring forbids.
    Round one filed the token's non-injectivity as a remainder because CURING
    THE TOKEN moves the claims key and owes a live migration. This does not
    touch the token: it asks the ROW's repository whether the room exists, so
    the claims key is byte-identical and the migration stays filed.

    THE EVIDENCE IS THE ROOM'S OWN GITDIR POINTER, which is unambiguous by
    construction. `work/_lanes` mints every room at `<root>-wt/<lane>` — "the
    pure function IS the registry key" — and git writes that room a `.git` FILE
    naming the repository it was cut from. So a room under THIS row's root whose
    pointer resolves inside THIS row's gitdir is the dispatched lane; a room of
    the same name under a fork's root is not, and cannot be mistaken for one.
    One small read, no subprocess, no registry: the two repositories that share
    a token do not share a room.

    THE ROOM IS HALF OF THE ANSWER AND NEVER THE WHOLE OF IT (round three).
    A room OUTLIVES the grant that cut it — an unlanded `helm work release`
    keeps both branch and worktree by design — so room existence is evidence
    about a DIRECTORY and never about a current lease. `_grant_binds_repo` is
    the other half and both must hold; this one asks only "is this room the
    dispatched lane's room, in THIS repository".

    THE POINTER IS ONE PATHNAME, so it is read WHOLE and never split on line
    separators. A newline is a legal byte in a path, and `git worktree add`
    inside a root whose own path contains one writes exactly that pointer; the
    first cut walked `splitlines()` and so truncated the path at the root's
    newline, resolved the fragment, and reported a genuine same-repository room
    as foreign — a live claim reading IDLE, the noisy direction but still a
    wrong answer about real progress. Git's own writer emits `gitdir: <path>\\n`
    and its reader takes everything after that 8-byte prefix, so this strips
    exactly one leading space and exactly one trailing newline and touches
    nothing else. Read as BYTES through `os.fsdecode`, because a path is
    filesystem bytes (`work/_lanes._git_bytes` says so) and a decode that
    replaced un-decodable bytes would resolve a different path than the one on
    disk.

    FAILING TO FIND THE ROOM LEAVES THE CLOCK VERDICT STANDING, which is the
    fail-safe direction this whole predicate is built in: a released room under
    a still-live lease makes a row read overdue (noisy) rather than worked
    (silent), and `_is_overdue` only ever lets WORKING suppress."""
    root = os.path.dirname(repo_id)
    real_repo = _real(repo_id)
    if not root or not real_repo or not claim_lane:
        return False
    from .work import _lanes
    try:
        room = _lanes.lane_path(root, claim_lane)
        with open(os.path.join(room, ".git"), "rb") as handle:
            raw = handle.read(_POINTER_MAX + 1)
    except (OSError, ValueError):
        return False
    # A POINTER LONGER THAN THE CAP IS UNREAD, NOT PARSED. A truncated read
    # yields a path PREFIX, and a prefix of a foreign gitdir can compare equal
    # to this repository's — evidence manufactured by the reader's own buffer.
    if len(raw) > _POINTER_MAX:
        return False
    pointer = os.fsdecode(raw)
    if not pointer.startswith(_GITDIR_PREFIX):
        return False
    path = pointer[len(_GITDIR_PREFIX):]
    if path.startswith(" "):
        path = path[1:]
    if path.endswith("\n"):
        path = path[:-1]
    target = _real(path)
    return bool(target and (target == real_repo
                            or target.startswith(real_repo + os.sep)))


def _grant_binds_repo(repo_id, grant):
    """Is this LIVE GRANT a lease taken out for THIS row's repository?

    ROOM EXISTENCE IS NOT A CURRENT GRANT (task/2437 round three, the seventh
    root's second half). The round-two cure asked the row's repository whether
    the claimed room exists, which is a true fact about a DIRECTORY and a false
    proxy for a lease: `work/_claims.release_lane` keeps both room and branch
    whenever the lane is not proven landed, and it also leaves the worktree's
    `lease:<id8>` lock string behind. So a seat that worked lane L in repository
    A, released the lease UNLANDED, and then claimed the same lane in repository
    B — one lease key, because the token is the root's basename — made A's row
    read WORKING off a room nobody holds and a lock naming a lease that no
    longer exists. Same silent alarm as round two, one artifact further out.

    SO THE EVIDENCE IS THE GRANT RECORD ITSELF, and all three of its facts must
    line up: it is LIVE NOW (`live_claims` is `seats._sweep`, so every row a
    caller passes here is unexpired), it carries a LEASE ID (the grant's own
    identity — a record with no nonce is not a grant helm minted), the HOLDER is
    the row's recipient (`progress_state` matched that before calling), and its
    RECORDED REPOSITORY is this row's. `seats_claims.claim` writes that
    repository under the same flock as the nonce, from `work/_claims._grant_repo`
    — the same `_repo_info` resolver that stamps a row's `repo_id`.

    UNKNOWN PROVENANCE SUPPRESSES NOTHING. Every grant minted before the field
    existed carries None, as does any lease taken directly on a `worktree:` name
    without a room (`helm chat claim`), and None is not a match: those rows go
    back to reading overdue on the clock alone, which is the noisy direction and
    the behaviour they had before progress evidence existed at all. The
    `dispatch:<id8>` row-claim is untouched and stays the unambiguous positive —
    it names the row, so no repository can be confused for another, and it is
    the escape for a seat whose lease carries no provenance."""
    if not isinstance(grant, dict):
        return False
    if not str(grant.get("lease") or "").strip():
        return False
    mine, theirs = _real(repo_id), _real(grant.get("repo"))
    return bool(mine and theirs and mine == theirs)


def live_claims():
    """The swept claims ledger, or None when it cannot be read.

    `seats._sweep` is THE definition of a live claim (exp_mono against the
    monotonic clock). Re-deriving expiry here would be a second owner of that
    rule and would drift from it; a private name is the smaller cost."""
    try:
        from . import seats
        raw = pk.read_json(seats.claims_path(), None)
        if not isinstance(raw, dict):
            return None
        return seats._sweep(raw)
    except Exception:            # noqa: BLE001 — unreadable is UNKNOWN below,
        return None              # never a traceback out of a deadline check


def progress_state(row, live=None):
    """(WORKING|IDLE|PROGRESS_UNKNOWN, detail) — is the RECIPIENT visibly
    working this row RIGHT NOW?

    "Overdue" used to mean only that a clock elapsed, never that nothing was
    happening. Measured cost: a seat 3h into a build read overdue while holding
    a 3h50m lease on the dispatched lane and actively editing files, and the
    stop-whisper then spent a turn at whichever seat stopped next. A row being
    worked is not late; it is being done.

    TWO SHAPES COUNT, and the row-claim is the stronger one because it names
    this row rather than a lane that could be anything:
      dispatch:<id8>        — the recipient claimed THIS ROW
      worktree:<proj>:<lane> — the recipient holds the dispatched lane, AND the
        GRANT records this row's repository, AND that lane's room belongs to it.
        The project token is the repo root's basename and is NOT injective, so
        the token alone let a claim in a FORK suppress the overdue verdict on a
        row of the other repository sharing that name. `_grant_binds_repo` reads
        the live grant's own recorded repository; `_lane_claim_binds` reads the
        room's gitdir pointer. Two repositories share neither.

    A ROOM IS NOT A GRANT, so both are required (round three). An unlanded
    release keeps the room by design, so a seat who moved the same lane to
    another checkout left a directory behind that answered YES for a repository
    nobody is working — the grant is what expires, and it is the fact that
    decides. A grant with no recorded repository is UNKNOWN provenance and
    suppresses nothing; the row-claim above is its escape.

    Absence of a claim is IDLE, not proof of idleness — plenty of real work
    holds no lease. That is why this only ever SUPPRESSES an overdue verdict
    and never manufactures one."""
    recipient = str(row.get("recipient") or "").strip()
    lane = str(row.get("lane") or "").strip()
    stem_lane = _lane_stem(lane)
    rid = str(row.get("id") or "")
    repo_id = str(row.get("repo_id") or "")
    project = _repo_project(repo_id)
    lane_resource = "worktree:%s:%s" % (project, lane) \
        if project and lane else None
    if not recipient:
        return PROGRESS_UNKNOWN, "row names no recipient to look up"
    claims = live_claims() if live is None else live
    if claims is None:
        return PROGRESS_UNKNOWN, "claims ledger is unreadable"
    for res, v in claims.items():
        if not isinstance(v, dict):
            continue
        from . import seats
        if not seats.recipient_matches(v.get("holder"), recipient):
            continue
        if rid and res == autoclaim_resource(rid):
            # THE ROW-CLAIM IS UNAMBIGUOUS AND STAYS EXACTLY AS IT WAS: it names
            # THIS row's id, so no repository can be confused for another.
            return WORKING, "recipient holds a claim on this row (%s)" % res
        if lane_resource and res == lane_resource \
                and _grant_binds_repo(repo_id, v) \
                and _lane_claim_binds(repo_id, lane):
            return WORKING, "recipient holds the dispatched lane (%s)" % res
        if res.startswith("worktree:") and project and stem_lane:
            # The claim's lane spelling and the row's need not match byte-for-
            # byte (#142: lane/-prefixed vs bare, round suffixes) — same
            # project, same lane FAMILY is the same dispatched lane.
            parts = res.split(":", 2)
            if len(parts) == 3 and parts[1] == project \
                    and _lane_stem(parts[2]) == stem_lane \
                    and _grant_binds_repo(repo_id, v) \
                    and _lane_claim_binds(repo_id, parts[2]):
                return WORKING, "recipient holds the dispatched lane (%s)" % res
    return IDLE, "recipient holds no live claim on %s" % (lane or "this row")


def _is_overdue(row, now=None, live=None):
    """The clock AND the absence of visible progress.

    Fail-safe direction is deliberate: an unreadable claims ledger leaves the
    clock verdict standing, so a broken lookup can only ever keep the old
    behaviour. Unknown must not quietly become "fine" — that would convert a
    noisy alarm into a silent one, which is the worse trade for an alarm whose
    whole job is to notice a stalled obligation."""
    if not (_open(row) and _age_s(row, now) >= int(row["deadline_s"])):
        return False
    return progress_state(row, live)[0] != WORKING


def overdue():
    now, live = time.time(), live_claims()
    return [r for r in open_rows() if _is_overdue(r, now, live)]


def _mine_or_unprovable(row, seat, roster, roster_failed):
    """True when this seat should be shown the row: it is MINE, or ownership
    cannot be proven and dropping it would strand the obligation entirely.

    THE UNPROVABLE CASE IS NOT A TECHNICALITY. Rows written before the identity
    fix (3e0fe8e) carry sender="claude" — the bare family floor every seat
    without HELM_CHAT_NAME in its environ resolved to — so a whole cohort has a
    sender that names no seat. Scoping those to "the seat called claude" hides
    them from EVERYONE, which converts a noisy net into a silent one: strictly
    worse, because nobody would ever see it fail.

    So the test is roster membership, not string inequality. A sender that IS a
    known seat and is not me is provably someone else's obligation and is
    skipped. A sender that names no seat, or an unreadable roster, is UNKNOWN
    and still surfaces — with the caller free to say so.
    """
    from . import seats
    sender = custodian_of(row)
    if not sender or roster_failed:
        return True                      # cannot look -> keep the net
    if seats.recipient_matches(sender, seat):
        return True
    if not any(seats.recipient_matches(sender, key) for key in roster):
        return True                      # names no seat -> ambiguous -> keep
    # MEMBERSHIP IS NOT LIVENESS, and this rung must not treat it as one
    # (a peer seat, measured): the roster carries 99 dead
    # `tmp-claude-N` rows, so a sender naming a rostered-but-gone seat would be
    # skipped for EVERY seat and its obligation stranded silently — the exact
    # failure the ambiguity branch above exists to prevent, arriving by a
    # different door. An absent sender cannot discharge anything, so the row
    # stays visible. Over-showing is the status quo; under-showing is the harm.
    # `last_seen` is a FUNCTION over the row, not the row's field of that name.
    # Reading row["last_seen"] directly returns a join-era timestamp — it read
    # 47 HOURS for this seat while it was mid-build — so every sender graded
    # "absent", the predicate kept every row, and the fix silently did nothing.
    # Caught only by running the live ledger before and after and seeing NO
    # DIFFERENCE. The roster table itself uses this accessor (seats.py:5639).
    row_ = next((value for key, value in roster.items()
                 if seats.recipient_matches(sender, key)), {})
    return seats.presence_of(seats.last_seen(sender, row_)) == "absent"


def custodian_of(row):
    """WHO OWES THE DELIVERY LEG NOW — the custodian if one was recorded, else
    the row's original sender.

    A SENT ROW'S OBLIGATION IS THE DELIVERY LEG (the integrator's 2026-08-12
    ruling, quoted in `_sent_by` below): making sure the recipient knows the
    row exists, and chasing it when they go quiet. When a seat dies or is
    renamed that obligation is stranded — nobody is positioned to chase, and
    `dispatch rebind` is NOT the repair because it moves the RECIPIENT, handing
    a third party's review to somebody else while the orphaned sender stays
    orphaned.

    So custody is a SEPARATE field from authorship, and that separation is the
    whole design: `sender` is an immutable fact about who wrote the row, and
    rewriting it would destroy the provenance the ledger exists to keep.
    `custodian` is mutable because it answers a question about the PRESENT.

    Absent on every row written before this field existed, which is why the
    fallback is the sender rather than an error: the default answer is the one
    the ledger has always given.
    """
    who = str(row.get("custodian") or "").strip()
    return who or str(row.get("sender") or "").strip()


def _sent_by(row, seat):
    """Did THIS seat OWE the row's delivery leg? The issuer half of "whose
    obligation" — read through `custodian_of`, so a transferred leg follows
    its custodian and an untransferred one still answers the sender.

    THE ISSUER GENUINELY HOLDS ONE, which is why this exists at all: the
    integrator's own ruling (2026-08-12) is that what a sender owes is the
    DELIVERY LEG — making sure the recipient knows the row exists and what it
    needs. A row sent but never delivered, or delivered to a seat that has gone
    quiet, is the SENDER's to chase, and nobody else is positioned to.

    STRICT, AND DELIBERATELY UNLIKE `_mine_or_unprovable` TWENTY LINES UP.
    That predicate keeps a row whose sender names NO seat, because it feeds the
    stop-guard NAG, where over-showing an orphan beats stranding it. This feeds
    a LISTING the reader is told answers "what do I owe", so the same
    generosity would hand every seat the whole pre-3e0fe8e cohort of floor-
    sender rows as work it had personally issued — task/1007's adoption failure
    arriving from the sender side. An unattributable author is therefore
    matched by NOBODY here, which is the honest answer: a row whose author
    cannot be named cannot be shown to a seat as ITS authorship.

    WHAT THAT DOES AND DOES NOT GUARANTEE, measured rather than assumed — the
    first draft of this comment claimed `_acting_author` "refuses the floor
    name for any caller", and it does not. It refuses a DERIVED floor (no
    declared name, no roster binding); a process that explicitly DECLARES
    `HELM_CHAT_NAME=claude` resolves to it, exactly as it already does for
    `--mine`. Live ledger, 2026-08-12: 2257 rows, 417 with a bare-family or
    absent sender (169 of them "claude") — and ZERO of those are OPEN, so the
    `--open` form the resume hook prints exposes none of them today. This flag
    therefore adds no identity rule of its own: a second, stricter door here
    would drift from the one that STAMPS the sender, and a listing scoped by a
    rule delivery does not use is how a seat gets told it authored nothing.
    The contradiction that IS measurable — declared name versus roster binding
    — already refuses upstream, for both flags, at that one door.

    The comparator is `seats.recipient_matches` — the same one
    `_mine_or_unprovable` uses on this same field — so sender scoping can never
    disagree with the ONE way this module compares a stamp to a seat.
    """
    from . import seats
    holder = custodian_of(row)
    return bool(holder) and seats.recipient_matches(holder, seat)


def stop_candidate(seat=None, snap=None):
    """The rows THIS seat is answerable for. `seat=None` keeps the old
    fleet-wide behaviour for callers that have no identity to offer.

    Scoping was always the intent — `seats._dispatch_candidate` documents this
    rung as "work YOU handed to another seat and have not checked on" — but the
    filter was never written, so every stopping seat was offered the same
    globally-oldest row. Measured 2026-07-29: three different seats each spent a
    turn on ONE dispatch that belonged to none of them, and this seat was
    offered kimi's row, twice offered rows whose sender it could not prove.

    `snap` is one already-read `(state, unavailable)` pair shared by the stop
    ladder. It prevents this rung, review-spiral, and work-offer from folding the
    same append-only ledger independently during one five-second hook.
    """
    current, unavailable = snapshot() if snap is None else snap
    if unavailable:
        return None, None, unavailable
    ordered = sorted(owed(current),
                     key=lambda r: (str(r.get("ts") or ""), r["id"]))
    if seat:
        roster, roster_failed = {}, True
        try:
            from . import seats
            roster, roster_failed = seats.roster_checked()
        except Exception:
            pass                          # unreadable -> every row stays visible
        ordered = [r for r in ordered
                   if _mine_or_unprovable(r, seat, roster, roster_failed)]
    migrate = next((r for r in ordered if r.get("migration")), None)
    if migrate:
        return migrate, "redispatch", None
    uncertain = next((r for r in ordered
                      if r.get("delivery") != "observed"), None)
    if uncertain:
        return uncertain, "confirm", None
    now = time.time()
    late = next((r for r in ordered if _is_overdue(r, now)), None)
    return late, "overdue" if late else None, None


# NOT BRACKETED on send or add — a P2 finding. `cmd_dispatch` REQUIRES --kind
# on both, and usage that renders a required flag as optional teaches the exact
# omission the requirement exists to prevent.
USAGE = ("usage: helm dispatch send <recipient> <lane> <message...> --ref TIP "
         "--kind build|review --new-work|--supersedes ID "
         "(or OMIT the message and pipe the body on stdin, or use a "
         "QUOTED-delimiter heredoc like <<'EOF' — argv bodies and UNQUOTED "
         "heredocs substitute backticks and $() before helm ever sees them) "
         "[--key K] [--note N] [--deadline SECONDS] [--repo PATH] [--force] "
         "[--posture-na REASON] [--read-only-because REASON] | add "
         "<recipient> <lane> --ref TIP --kind build|review "
         "--new-work|--supersedes ID [--note N] "
         "[--deadline SECONDS] [--repo PATH] [--force] [--posture-na REASON] "
         "[--read-only-because REASON] "
         "(the lane is a LABEL; --new-work / --supersedes is WORK IDENTITY and "
         "exactly one is REQUIRED, because a renamed continuation is invisible "
         "to any same-lane rule) | verdict <id-or-unique-prefix> "
         "<full-reviewed-tip> --approve|--fix|--supersede|--concur "
         "--measured|--inferred|--unverified "
         "[--worse-than-main PATH ...|--imperfect] [--finding-count N] "
         "[--prior-relation regression-of-cure|uncured|new] "
         "[--patch-tip FULL_SHA|--no-patch-because REASON] "
         "[--reviewer-model M --reviewer-run RUN [--author-model M]] <evidence> "
         "(polarity is REQUIRED: an omitted flag records immutable UNDECLARED; "
         "FIX/SUPERSEDE also require the exit answer: only a named touched path "
         "that regresses relative to main is a block; --imperfect teaches "
         "APPROVE plus separately filed remainder. --approve also "
         "requires evidence containing a verified gate:<token>, because an "
         "ungated approve is immutable and can never authorize landing. "
         "--patch-tip rides a FIX and names the cure the REVIEWER committed on "
         "a branch off the exact reviewed tip: the row then records two "
         "authors, and the lane owner or integrator rebases onto that tip or "
         "cherry-picks it. A design finding takes a meld instead, and a FIX "
         "with no --patch-tip REFUSES unless --no-patch-because REASON (one "
         "quoted argv token) records which reason applied. --imperfect WITH a "
         "--patch-tip is the reader whose read found the tip no worse than "
         "main and who cured something anyway: it records exit answer "
         "IMPERFECT and asks the author to agree to the patch, blocking "
         "nothing. A REVIEW brief that tells its reader not to cure what it "
         "finds (a read-only review, do not commit, do not patch, no edits, "
         "report only, findings only) is "
         "REFUSED unless --read-only-because REASON records why) | "
         "cancel <id-or-unique-prefix> "
         "<reason...> | mark-delivered <id-or-unique-prefix> <delivery-ref> "
         "(update delivery_ref after a send -- retry evidence, changed "
         "mechanism, manual confirmation; idempotent for the same ref, "
         "a different ref appends a new delivered event with a warning) | "
         "hold <id-or-unique-prefix> <reason...> [--owner-gated] "
         "[--source-clean TIP] "
         "(acknowledge but gate on external dependency; release back to open. "
         "A hold names WHO OWES THE NEXT MOVE and there are three answers: the "
         "fleet by default, the OWNER under --owner-gated, and the INTEGRATOR "
         "under --source-clean TIP, which is the reviewer whose source read "
         "found nothing and who cannot mint an approve because an approve "
         "binds a verified whole-suite token only the land gate produces. The "
         "two are refused together because one row cannot owe two holders) | "
         "release <id-or-unique-prefix> (return a HELD row to OPEN) | "
         "retract <id-or-unique-prefix> --reason R "
         "--reads source-clean|fix|supersede|unknown --measured|--inferred "
         "[--reissue|--successor ID] [--json] (withdraw a WRONG verdict's "
         "authority without rewriting it: one verdict-retract event appended "
         "after the verdict, and the row reads RETRACTED wherever authority is "
         "read. Only the verdict's author seat (any session), the integrator "
         "or the owner may retract; --reads is the corrected reading for the "
         "integrator, not a new polarity; --reissue mints the successor "
         "review for the same recipient and tip, --successor links one that "
         "already supersedes this row) | "
         "rebind <id-or-unique-prefix> --to <seat> [--force] "
         "[--reason R] [--repo PATH] [--json] "
         "(move an OPEN row to a new recipient, one operation; REFUSED unless "
         "proxywatch measures starvation/hang or autocompact proves fresh "
         "context exhaustion; either arm suffices, else --force needs a reason. "
         "A recipient who is measurably LIVE and measurably MID-READ — an open "
         "row whose delivery was observed — is refused earlier and by name, "
         "because the capacity gate can only say 'not measurably unable to "
         "act' and names nobody; --force --reason moves it anyway and the "
         "cancel records the override and the seat it was taken from) | "
         "retip <id-or-unique-prefix> --ref NEW_TIP --reason R [--repo PATH] "
         "[--json] "
         "(re-point an OPEN row at a NEW TIP in place — same row, same "
         "recipient, same chain; the mirror of rebind for the case where the "
         "BASE moved rather than the reviewer. REFUSED on any non-open row "
         "because a verdict BINDS its tip, on different work — per-commit "
         "patch-id identity for reviews, descendant-only for builds — on "
         "a row whose OPEN successor already carries the obligation, on a "
         "row with no derivable current tip (legacy needs-redispatch), and "
         "when the successor frontier cannot be READ because a not-closed "
         "row replays a malformed supersedes) | "
         "list [--open|--overdue|--held] [--source-clean] [--mine] [--issued] "
         "[--to SEAT] "
         "[--all-projects] [--json] "
         "(--mine keeps ONLY the rows naming THIS seat — the filter the "
         "resume-turn hook has always told compacted seats to apply; it "
         "REFUSES rather than showing everything when this process's identity "
         "cannot be resolved. --issued is its ISSUER half: the rows THIS seat "
         "SENT and has not yet seen discharged, which are obligations too "
         "because the sender owns the DELIVERY LEG. Together they are the "
         "UNION ON THE DIRECTION AXIS — every OPEN row in either direction. "
         "It is SILENT ON THE STATE AXIS: --open selects status open, so a "
         "row in state HELD is in neither view whichever way it points, and "
         "an empty union means 'nothing OPEN owed', never 'nothing owed'. "
         "Ask --held for those. --to SEAT "
         "asks the recipient question about a named seat. THE LISTING IS "
         "SCOPED TO THE PROJECT OF THE DIRECTORY YOU RUN IT IN — this "
         "ledger holds rows for every registered project, keyed by the "
         "repository each ref lives in — and --all-projects lists them "
         "all; an identity-scoped listing (--mine/--issued) is NEVER "
         "narrowed by project, because a row that names you is yours "
         "wherever its code lives. A ROW THIS REGISTRY CANNOT PLACE IS NOT "
         "COUNTED IN THE PROJECT YOU ARE STANDING IN: one naming no "
         "repository at all, or one whose project lookup came back empty "
         "(nobody registered it, its path would not resolve, or the "
         "registry could not be read — ONE answer here), is listed under "
         "its own UNKNOWN PROVENANCE heading with a count, never inside "
         "that project. What that reports is the failed LOOKUP and never "
         "proof that the row is in no project. --json gives every row a "
         "`scope_class` field (local / origin_unknown / project_unresolved) "
         "so a machine reader can make the same distinction) | "
         "triage [ID...] [--all-projects] "
         "(the bulk listing is scoped to this directory's project, with the "
         "same UNKNOWN PROVENANCE bucket for rows this registry could not "
         "place; a "
         "NAMED id is always answered, and rows set aside are counted "
         "on stderr) | "
         "mix [--hours N] [--sender SEAT] [--json] | "
         "briefs [--cut] "
         "(read-only census of OPEN rows whose brief survives ONLY as a "
         "truncated copy on the row — rows sent before briefs were stored "
         "whole beside the ledger. Bare gives the denominator too; --cut is "
         "the table alone. Re-send what it lists: the tail of a cut brief "
         "went out in the original DM and is not in this ledger) | "
         "collisions [--json] "
         "(read-only: every event the fold DROPPED because it reused a seq "
         "an applied event on its row already held. A LIVE line is on a row "
         "still open or held that no successor carries, and helm doctor "
         "WARNs on each; a HISTORY line is on a row that has ended — closed, "
         "retired or superseded — and the doctor only counts those. --json "
         "is the whole list, each entry carrying its row's ended word)")


def _parse(rest, names, positional_flags=()):
    pos, opts, i = [], {}, 0
    while i < len(rest):
        arg = rest[i]
        if not arg.startswith("--") or arg in positional_flags:
            pos.append(arg)
            i += 1
            continue
        if arg not in names:
            return None, None, "unknown option %s" % arg
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return None, None, "%s wants a value" % arg
        opts[arg] = rest[i + 1]
        i += 2
    return pos, opts, None


def rebind_argv_ok(argv):
    """The rebind arm's EXACT admission, shared with the wiring census so a
    command the parser would refuse can never census as a live consumer
    (meld e:1785584307: a second partial parser drifts; the grammar
    has one home). Returns (ok, pos, opts, perr); the CLI arm consumes this
    same tuple, so census and parser cannot disagree."""
    rest = [a for a in argv if a not in ("--force", "--json")]
    pos, opts, perr = _parse(rest, ("--to", "--reason", "--repo"))
    ok = not perr and len(pos or ()) == 1 and bool((opts or {}).get("--to"))
    return ok, pos, opts, perr


def _base_label(row):
    """A CLOSED ROW MUST STATE ITS DECISION, not just the noun.

    "VERDICT" alone was read as "a verdict is OWED" — by the integrator, on
    2026-07-28, off this very list. The open label is "PENDING VERDICT", so a
    closed row reading the same noun minus one word invites exactly that
    inversion: scanning a column of "VERDICT" next to an age like 4513m/180m,
    the honest reading is a fleet drowning in overdue reviews. Five discharged
    rows were relayed to a seat as five missed obligations, and it took that
    seat going to the source to catch it.

    So the closed label carries the polarity. That also surfaces the UNDECLARED
    verdicts — helm's own usage text says 36% of this ledger was filed with no
    polarity, a number nothing in the default view had ever shown, because a
    decision with no direction rendered identically to a decided one."""
    if row.get("verdict_retracted"):
        # THE ORIGINAL DECISION STAYS IN THE LABEL and the retraction follows
        # it, the way every other terminal here reads: the ledger holds both
        # facts, and a label that dropped the first would hide what was
        # withdrawn (task/3060).
        successor = str(row.get("retract_successor") or "")
        return "VERDICT %s / RETRACTED (reads %s) by %s%s" % (
            row.get("retracted_polarity") or "UNDECLARED",
            row.get("retract_reads") or "unknown",
            row.get("retract_seat") or "?",
            " -> " + successor[:12] if successor else " (no successor)")
    if row.get("abandoned"):
        return "VERDICT %s / ABANDONED (LAND UNKNOWN)" % (
            row.get("polarity") or "UNDECLARED")
    if row.get("closed_by_landing"):
        return "VERDICT UNDECLARED / CLOSED BY LANDING"
    if row.get("discharged"):
        return "VERDICT %s / DISCHARGED" % (row.get("polarity") or "UNDECLARED")
    if row.get("close_reason"):
        if row.get("close_reason") == "delivered-report":
            return "BUILD / CLOSED (DELIVERED REPORT%s)" % (
                "; CORRECTED CANCEL" if row.get("delivered_report_correction") else "")
        if row.get("status") == "closed" and row.get("landing_review_id"):
            return "BUILD / CLOSED (LANDED via APPROVED REVIEW)"
        return "VERDICT %s / CLOSED (%s)" % (
            row.get("polarity") or "UNDECLARED",
            str(row["close_reason"]).upper().replace("-", "_"))
    if row.get("status") == "verdict":
        return "VERDICT %s" % (row.get("polarity") or "UNDECLARED")
    if row.get("status") == "cancelled":
        return "CANCELLED"
    if row.get("status") == "held":
        # THE HOLDER IS NAMED IN THE STATE WORD, because every reader of this
        # line is deciding whether the row is theirs. A source-clean hold owes
        # the integrator one land gate and owes the reviewer nothing, and it
        # names the exact tip so the gate binds the tree that was read.
        if row.get("source_clean_tip"):
            return "HELD SOURCE-CLEAN at %s ON THE INTEGRATOR (%s)" % (
                row["source_clean_tip"][:12],
                row.get("hold_reason") or "unspecified")
        return "HELD (%s)" % (row.get("hold_reason") or "unspecified")
    if row.get("migration"):
        return "NEEDS REDISPATCH"
    if row.get("delivery") != "observed":
        return "PENDING VERDICT / NEEDS CONFIRMATION"
    return "PENDING VERDICT"


def _label(row, unverifiable=()):
    """The base label, PLUS the attest state when it cannot be verified.

    COMPOSES rather than replaces, and that is the whole design. The two facts
    are independent — WHAT was decided (approve/fix/cancelled) and WHETHER its
    signed delivery still verifies — so collapsing them into one string would
    force a choice between showing a verdict and showing that its record is
    unverifiable. A row reads `VERDICT approve / ATTEST UNVERIFIABLE`: the
    decision stands, and the audit trail for it does not.

    DEFAULTS TO EMPTY so every existing caller keeps its exact output. The set
    is passed IN rather than computed here because computing it per row would
    re-read a 500KB+ ledger once per printed line."""
    base = _base_label(row)
    if unverifiable and str(row.get("id")) in unverifiable:
        base += " / ATTEST UNVERIFIABLE"
    # THE SAME COMPOSITION FOR A ROW THIS HELM CANNOT READ IN FULL: the label
    # above is this binary's reading, and this says that reading stopped at an
    # event it has no arm for. Absent on every row whose kinds are all known.
    note = unknown_kinds_note(row)
    return base + " / " + note if note else base


def _fmt(row, now, late=False, unverifiable=(), chase=False, reach=None,
         ended_by=None):
    """One row, rendered against the LISTING'S read instant.

    `now` is REQUIRED and positional on purpose. The listing binds one instant
    before its selector chain and threads it through the filter and the header
    stamp; this function used to call `_age_s(row)` bare, so the age it PRINTED
    was measured against a clock the verdict beside it had never seen. Reading
    at 1059 leaves a 1000-stamped/60s row not-late; rendering that same row a
    minute later printed `2m/1m` with NO NEEDS CHECK-IN — a marker and an age
    contradicting each other on one line, which is worse than either alone
    because the reader cannot tell which half to believe. A default of None
    here would leave that split one forgotten argument away."""
    age = _age_s(row, now) // 60
    deadline = int(row["deadline_s"]) // 60
    suffix = "  NEEDS CHECK-IN (OVERDUE)" if late else ""
    # THE ROLE, on the row, whenever a listing mixes both directions. A row
    # rendered here under `--issued` is addressed to ANOTHER seat, and the
    # recipient column alone does not read as a role to a compacted seat that
    # has only ever seen its own name there.
    if chase:
        suffix += "  YOURS TO CHASE (you sent it)"
    # WHETHER ANYONE STILL OWES ANYTHING. A row whose chain reached a landed
    # approve is finished even though this row never closed, and the honest
    # sentence is that it has not closed -- never that the reader owes
    # something. The debt markers above are suppressed for it by the caller,
    # so this is what remains: the row stays VISIBLE and says why it is quiet.
    # A HELD RUNG IS THE EXCEPTION, and it gets a second line. Its chain ended
    # in fact, but a hold that names findings owes them a VERDICT, because a
    # close reason is not one; only a source-clean hold (zero findings) is
    # owed nothing, and it names the one verb that closes it.
    owed = ""
    if ended_by:
        ended = "%s %s" % (closed_state(ended_by) or "discharged",
                           str(ended_by.get("id") or "")[:12])
        herr = held_discharge_error(row) \
            if row.get("status") == "held" else None
        if herr:
            suffix += ("  DISCHARGED IN FACT (%s) — this row has not closed, "
                       "and %s is owed"
                       % (ended, "the OWNER's decision"
                          if row.get("owner_gated") is True else "a VERDICT"))
            owed = "\n      owed on %s: %s; %s" % (
                str(row["id"])[:12], herr, held_rung_remedy(row))
        elif row.get("status") == "held":
            suffix += ("  CHAIN ENDED (%s) — SOURCE-CLEAN, zero findings, no "
                       "verdict owed: `helm lr close %s --reason discharged` "
                       "closes it%s" % (
                           ended, str(row["id"])[:12],
                           "" if closed_state(ended_by) == "landed" else
                           " once the chain's APPROVE is closed landed"))
        else:
            suffix += ("  CHAIN ENDED (%s) — this row has not closed; you owe "
                       "nothing" % ended)
    # WHETHER ANYONE CAN RECEIVE THIS. A row addressed to a name the roster
    # does not carry is owed by nobody and chased by nobody, and until this
    # marker existed it rendered exactly like a live obligation. The word is
    # the chat send door's word, deliberately: a reader who has seen ABSENT
    # there must not have to learn a second vocabulary here.
    state = (reach or {}).get(_recipient_key(row))
    if state == "ABSENT":
        suffix += "  RECIPIENT ABSENT (no roster row)"
    elif state == "UNKNOWN":
        suffix += "  RECIPIENT UNKNOWN (roster unreadable)"
    if row.get("attest_state"):
        suffix += "  [polarity: %s; attest: %s from %s]" % (
            _source_label(row.get("polarity_source")),
            str(row["attest_state"]).upper(),
            _source_label(row.get("attest_source")))
    # NO re-measure on the hot list path. Measured 2026-08-04: clearspan
    # costs 0.60s/row against 0.25s for the whole listing without it, and the
    # module's own docstring promises parse-only lightweight here. The stamp
    # is the triage-time surface's job now (helm dispatch triage); a row's
    # claims re-measure on demand, where the cost is the point of the visit.
    stamp = "·"
    return " %s %s  %-16s %-24s %-36s %3dm/%dm%s  %s%s" % (
        stamp, row["id"], _recipient_label(row), row["lane"],
        _label(row, unverifiable),
        age, deadline, suffix, str(row.get("tip") or row.get("ref") or "-")[:12],
        owed)


# ---------------------------------------------------------------------------
# capacity mix — the surface that would have caught the inverted night
# ---------------------------------------------------------------------------

# A fleet 100% occupied REVIEWING ONE AUTHOR is maximally busy and minimally
# useful, and every existing surface reported it as healthy. The heartbeat fires
# on FLEET-QUIET; nobody was quiet. `lr stalls` bills stalled loops; none were
# stalled. The owner board counts LANDS; they were landing. Busy is not diverse,
# and none of those instruments could tell the difference.
MIX_WINDOW_H = 8
# An integrator who has sent this many dispatches with NO build lane among them
# is running a review queue, not a team. Low on purpose: the point is to fire in
# the first couple of hours, not to be provably right at dawn.
MIX_NO_BUILD_ALARM = 4


def mix(hours=MIX_WINDOW_H, sender=None, now=None):
    """Capacity allocation over a window: {sender: {build, review, unknown,
    recipients}}.

    UNKNOWN IS REPORTED, NEVER FOLDED. Rows written before `kind` existed carry
    None, and a report that silently counted them as builds would have made the
    inverted night look balanced — the precise failure this exists to prevent.
    """
    import time
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    # RAW EVENTS, not snapshot(), and the FIRST version of this comment justified
    # that with a claim a review refuted: it said the projection "does not carry
    # `kind` at all". FALSE. `_new_state` does `out = dict(row)` for a native v3
    # row, so it preserves every field including this one. Measured after the
    # refutation: snapshot() returns MORE rows than there are dispatch events
    # (56 vs 50 — it also normalizes `add` rows) and loses ZERO dispatch ids. The
    # projection was never the obstacle.
    #
    # THE REAL REASON is narrower and worth stating accurately: this counts
    # dispatch EVENTS EMITTED IN A WINDOW, and snapshot() answers "what
    # obligations exist and in what state" — one normalized row per id, with
    # status and delivery rewritten. Those questions coincide in the count today
    # because one dispatch is one id, but the direct source for "what was sent"
    # is the sent events, and a projection built for a different question is free
    # to change its shape without warning this counter.
    #
    # Recorded because the wrong reason was in a COMMIT MESSAGE, which is the
    # surface a future reader trusts without re-deriving.
    events, unavailable = eventledger.checked_events(ledger_path())
    if unavailable:
        return None, "dispatch ledger unreadable — allocation UNKNOWN, not zero"
    # RAW EVENTS ARE UNTRUSTED INPUT, and this is the cost of reading them
    # instead of the projection. snapshot() runs `_valid_identity` and keys by
    # id; a raw scan inherits NEITHER. Two ways that produced a FALSE alarm,
    # both measured on the live ledger:
    #   - a row whose identity does not validate (corrupt id/recipient/lane, or
    #     a type-corrupt seq) is not a dispatch anyone sent, but it counted;
    #   - the SAME dispatch id appearing twice — a retry, a replay, a restored
    #     ledger segment — counted twice, and MIX_NO_BUILD_ALARM is 4, so two
    #     duplicated reviews are half the distance to accusing an integrator of
    #     running a review queue.
    # An alarm that cries CAPACITY-INVERTED at a fleet that was not is worse
    # than no alarm: it is the vacuous-pass class pointed at the operator.
    out = {}
    seen = set()
    for r in events or []:
        if not isinstance(r, dict):
            continue
        if r.get("event") != "dispatch":
            continue
        # `_new_state`, not `_valid_identity` — the CANONICAL genesis test, and
        # the distinction is the finding. `_valid_identity` is only the first of
        # snapshot's gates; `_new_state` also demands a v3 row be status=open
        # with a real 40-64 hex tip, or a historical row carry a known status.
        # Rows that clear identity but fail genesis are ones snapshot counts at
        # ZERO, and they were still allocating capacity here. One reader, one
        # definition of "a dispatch that happened".
        if not _new_state(r):
            continue
        if r["id"] in seen:
            continue                 # one dispatch is one id, however many
        seen.add(r["id"])            # times the ledger replayed it
        ts = r.get("ts") or ""
        try:
            import calendar
            when = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            continue                 # unparseable stamp is not evidence of age
        if when < cutoff:
            continue
        who = r.get("sender") or "?"
        if sender and who != sender:
            continue
        b = out.setdefault(who, {"build": 0, "review": 0, "unknown": 0,
                                 "recipients": set()})
        k = r.get("kind")
        b["build" if k == "build" else "review" if k == "review" else "unknown"] += 1
        if r.get("recipient"):
            b["recipients"].add(r["recipient"])
    for b in out.values():
        b["recipients"] = sorted(b["recipients"])
        b["total"] = b["build"] + b["review"] + b["unknown"]
    return out, None


def mix_alarm(hours=MIX_WINDOW_H, sender=None, now=None):
    """(alarm_text or None, err). Fires when a sender has dispatched
    MIX_NO_BUILD_ALARM+ times in the window with ZERO build lanes among them,
    and names EVERY such sender, worst first — see the note in the body for the
    measured incident where naming only the first hid three of four.

    Deliberately silent while `kind` is unrecorded: an all-UNKNOWN window cannot
    distinguish a healthy night from an inverted one, and inventing a verdict
    from absent data is the failure mode this whole feature is a response to."""
    got, err = mix(hours, sender, now)
    if err:
        return None, err
    # EVERY qualifying sender, WORST FIRST — not the first one alphabetically.
    #
    # MEASURED 2026-07-31. The loop below used to `return` on its first hit
    # inside `sorted(got.items())`, which sorts by NAME. Live ledger at that
    # moment: codex 6, codex-2 18, codex-3 5, gemini 8 — FOUR senders qualified
    # and the alarm named ONE of them, `codex`, because "codex" sorts first. It
    # was also the second-SMALLEST. So the alarm under-reported by 75% and hid
    # the worst offender by a factor of three, while looking exactly like an
    # alarm that had fired correctly.
    #
    # This is the shape helm keeps re-learning: a bound that SKIPS work must
    # never yield a confident answer. An early `return` is a cap, the caller
    # cannot see that anything was skipped, and one-of-four reads identical to
    # one-of-one. The docstring already promised "no other surface will tell
    # you", which is precisely why a partial answer here is worse than a loud
    # one — there is no second reader to catch the omission.
    hits = [(b["review"], who, b) for who, b in got.items()
            if b["build"] == 0 and b["review"] >= MIX_NO_BUILD_ALARM]
    if not hits:
        return None, None
    hits.sort(key=lambda h: (-h[0], h[1]))    # worst first; name breaks the tie
    lines = ["CAPACITY-INVERTED %s sent %d review dispatch%s to %s in %dh with "
             "ZERO build lanes" % (who, b["review"],
                                   ("es" if b["review"] != 1 else ""),
                                   ", ".join(b["recipients"]) or "nobody", hours)
             for _n, who, b in hits]
    tail = ("— that is a review queue, not a team. The seats are busy, which is "
            "why no other surface will tell you.")
    if len(lines) == 1:
        return lines[0] + " " + tail, None
    return ("%d senders are capacity-inverted, worst first:\n  %s\n%s"
            % (len(lines), "\n  ".join(lines), tail)), None


def cmd_mix(args):
    """dispatch mix [--hours N] [--sender SEAT] [--json] — who is spending the
    fleet, on what.

    Hand-rolled rather than `_parse`d because `--json` is a bare switch and
    _parse demands a value for every option. Strict all the same: an unknown
    option is refused, never ignored. A capacity report that silently drops the
    filter you asked for answers a DIFFERENT question than the one you typed,
    and looks exactly like the answer to yours."""
    import json as _json
    args = list(args or [])
    hours, sender, as_json = MIX_WINDOW_H, None, False
    i = 0
    givens = set()
    while i < len(args):
        a = args[i]
        # A REPEATED OPTION IS AMBIGUOUS, so it is refused rather than resolved.
        # `--hours 4 --hours 48` silently took the last one, which means the
        # window the operator READ in their own command line was not the window
        # the report answered for.
        if a in givens:
            print("helm dispatch: %s given twice — which one did you mean?" % a,
                  file=sys.stderr)
            return 2
        if a.startswith("--"):
            givens.add(a)
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a in ("--hours", "--sender"):
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                print("helm dispatch: %s wants a value" % a, file=sys.stderr)
                return 2
            if a == "--sender":
                sender = args[i + 1]
            else:
                try:
                    hours = int(args[i + 1])
                except ValueError:
                    print("helm dispatch: --hours wants an integer",
                          file=sys.stderr)
                    return 2
                if hours < 1:
                    # A zero or negative window makes `mix` scan a range no row
                    # can fall into, and an EMPTY result prints as a fleet that
                    # sent nothing — indistinguishable from a genuinely idle
                    # night, with rc 0.
                    print("helm dispatch: --hours must be >= 1 (got %d) — a "
                          "non-positive window reports every fleet as idle"
                          % hours, file=sys.stderr)
                    return 2
            i += 2
            continue
        print("helm dispatch: unknown option %s\n%s" % (a, USAGE),
              file=sys.stderr)
        return 2
    # ONE INSTANT FOR BOTH READS. `mix` and `mix_alarm` each defaulted to
    # time.time(), so a single command performed TWO full ledger reads at TWO
    # cutoffs. Against the hard MIX_NO_BUILD_ALARM threshold that lets the
    # TABLE print a sender at the alarm count while the ALARM below it stays
    # silent — or the reverse, the alarm naming a sender whose printed row is
    # one lower. One command, two contradictory statements about one sender.
    read_now = time.time()
    got, err = mix(hours, sender, read_now)
    if err:
        print("helm dispatch: " + err, file=sys.stderr)
        return 1
    if as_json:
        print(_json.dumps(got, indent=1, sort_keys=True))
        return 0
    if not got:
        print("helm dispatch: no dispatches in the last %dh" % hours)
        return 0
    print("helm dispatch — capacity mix, last %dh" % hours)
    for who, b in sorted(got.items()):
        print("  %-18s build %-3d review %-3d unknown %-3d  -> %s"
              % (who, b["build"], b["review"], b["unknown"],
                 ", ".join(b["recipients"]) or "-"))
    unknown = sum(b["unknown"] for b in got.values())
    if unknown:
        print("  (%d dispatch%s predate the `kind` field and are counted as "
              "UNKNOWN, never assumed)" % (unknown, ("es" if unknown != 1 else "")))
    alarm, _e = mix_alarm(hours, sender, read_now)
    if alarm:
        # EVERY line, not just the first. `"\n  " + alarm` indented line one and
        # left the rest at column 0, so the multi-sender alarm rendered as an
        # indented header followed by a flush-left wall — caught in
        # review, on the very output whose readability I had asked a reviewer to
        # judge. The alarm's own list items carry a further two spaces, so this
        # gives header 2 / items 4 / tail 2 and the shape survives the CLI.
        print("\n" + "\n".join("  " + ln for ln in alarm.split("\n")))
    return 0


# ---------------------------------------------------------------------------
# review spiral — serialized rounds on ONE lane, where a MELD is the cure
# ---------------------------------------------------------------------------

# THE INCIDENT (2026-07-29/30). helm's typed store already holds the rule. The
# `review-begins-with-cat-file` heuristic says, verbatim: "at TWO rounds the
# cure is a MELD, never round three. Live cost of getting this wrong: ~6 async
# rounds on one small lane, 2026-07-29." That entry FIRED in the integrator's
# injected context on EVERY TURN of the session in which he then ran six
# serialized rounds on one lane, until the owner asked why a meld had not
# ended it before round 6 — after which ONE meld exchange closed
# all three remaining questions.
#
# So this is not a knowledge gap and another store entry cannot fix it. Owner,
# same night: "we still fail to reach for them automatically. maybe stophooks
# that recognize situations where they would be handy?" A rule that fires and
# is not followed needs a GATE, not a louder rule (premise
# enforce-not-advise-for-repeated-behavior).
#
# THE SIGNAL: DISTINCT REVIEWED TIPS PER LANE, NOT DISPATCH COUNT.
# Counting review dispatches per lane is the AVAILABLE signal; counting the
# distinct TIPS those dispatches bound is the RIGHT one, and the live ledger
# says so out loud. Measured over the real ledger (1041 events, 202 review
# rows, 130 sender/lane pairs), lane `stop-candidate-seat-scope` carries two
# review dispatches from `claude` at the SAME tip ad7f2b1ac951, 38 seconds
# apart, to gemini and to ds4pro. That is a deliberate cross-family fan-out —
# helm's OWN law (heuristic 14: cross-family refutation, not same-family
# refinement) — and it is the healthiest move a dispatcher makes. Raw dispatch
# count calls it two rounds and would gate the one behaviour we want more of.
# Distinct tips calls it one round, correctly: A ROUND IS A NEW TIP, because a
# round happens when the author changed the code and re-submitted. The ledger
# records the tip natively (`_resolve_tip` resolves it at write time), so this
# is measured, not inferred — unlike the "-r2 means review" lane-name heuristic
# that misclassified 5 of 17 and is the reason `kind` exists at all.
#
# THE READER IS `snapshot()`, NOT RAW EVENTS — the deliberate inverse of
# `mix()` above, for a stated reason. `mix` asks "what was SENT in a window"
# and so reads sent events. This asks "what obligations exist on this lane and
# in what STATE", which is precisely snapshot's question: it dedupes by id,
# validates identity, and carries the terminal status this needs to drop
# CANCELLED rounds. A cancelled dispatch is a withdrawn round — it never
# consumed a reviewer round-trip — and counting it inflates a healthy lane
# toward the threshold on work that did not happen. Measured: dropping
# cancelled rows halves the firing population (10 lanes -> 5 of 122) while both
# real incidents survive at their FULL round counts, 8 and 7.
SPIRAL_WINDOW_H = 12
# Measured distinct-tip distribution over those 122 sender/lane review pairs:
#   1 round 104 | 2 rounds 13 | 3 rounds 2 | 4 rounds 1 | 7 rounds 1 | 8 rounds 1
# TWO is the store's stated CURE point, not its failure point, and 2 rounds is
# ordinary: one round of findings, a fix, a re-review, done. Gating there would
# stop ~13% of all review lanes, and this very file already carries the lesson
# that a rung which blocks everybody is switched off within a day (see the
# built-but-not-wired latch in seats.py). So TWO WARNS.
# THREE is the round the rule forbids by name. Blocking there fires on 5 of 122
# pairs (4%) and catches BOTH measured incidents — `gate-mints-its-own-evidence`
# (the integrator -> codex, 7 tips in 1h58m) and
# `resolve-matches-session-not-just-env` (claude -> codex, 8 tips in 6h46m).
# The block lands the moment round three has been dispatched and the seat tries
# to go idle waiting for its verdict, which is exactly where the owner
# interrupted by hand: it converts round three into the LAST async round
# instead of the third of six.
SPIRAL_MELD_ROUNDS = 2
SPIRAL_BLOCK_ROUNDS = 3

# A CHAIN THAT ENDED IS NOT A SPIRAL — it is a conversation that CONVERGED.
# Measured false positive, 2026-07-31: the guard fired on `land-pipeline-card`
# at 4 rounds. That chain's rounds read fix, fix, fix, fix, fix, fix, APPROVE —
# It was approved and it LANDED at e5e4cad before the guard ever spoke. The
# rounds were real; the spiral was over. Counting rounds answers "how much
# ping-pong has there been", but the guard's actual question is "is there a
# ping-pong I can still interrupt", and only the LAST round's polarity answers
# that one. Same narrower-neighbour defect this file already documents twice.
#
# `cancelled` is dropped earlier as a WITHDRAWN round; these are the polarities
# that END one. `approve` ends the work; `supersede` hands it to a new chain
# root, which then counts on its own from one. A `fix` verdict is NOT terminal
# and must keep counting — a chain sitting at round three with findings
# outstanding is precisely the live spiral about to become round four, and that
# is the one the block exists to catch.
#
# ONE RULE DOES THIS, NOT TWO. The obvious first fix — skip a chain whose own
# last round carries a terminal polarity — was written, and MUTATION TESTING
# KILLED IT: reverting it left the suite green, because the spent-prefix rule
# below already rejects every input it rejected. A chain that ends in its own
# approve has last-round-ts == that approve's ts, so `<=` catches it too. Two
# checks refusing the same input measure NEITHER, since reverting either leaves
# the other refusing; the redundant one was deleted rather than kept for
# comfort. If you are tempted to re-add it, the surviving rule is strictly
# weaker and strictly sufficient.
#
# FAIL-OPEN IN THE RIGHT DIRECTION. The cost of the miss and the cost of the
# false fire are not symmetric: a missed spiral wastes reviewer round-trips,
# while a false block stops a seat from going idle over work that already
# shipped — and this file already carries the lesson that a rung which fires on
# healthy behaviour is switched off within a day (the built-but-not-wired latch
# in seats.py). A guard that gates converged work teaches the fleet to ignore it.
SPIRAL_TERMINAL_POLARITIES = ("approve", "supersede")

# WHICH PRESCRIPTIONS ADVISE RATHER THAN BLOCK, read by the stop rung so the
# two surfaces cannot disagree about which one walls a seat. FINISH is a
# convergence the typed counts proved. UNDER-ARMED is the opposite reading of
# the same three rounds — every round found a defect the earlier arms could
# not see — and that is the behaviour a review exists to produce, so it is
# said and not walled. MELD, the spiral itself, is absent here and blocks.
SPIRAL_ADVISORY_PRESCRIPTIONS = ("FINISH", spiral_findings.UNDER_ARMED)


def _sender_strings(now=None, hours=SPIRAL_WINDOW_H, snap=None):
    """Every sender string the ledger actually recorded in the window.

    The spiral gate keys on this rather than trusting that a seat's resolved
    display name is the string it writes under — they diverge, silently, and
    the divergence exempts the seat instead of failing it.

    `snap` is the caller's ALREADY-READ state. `review_spiral` reads the
    ledger and then called this, which read it AGAIN — two ~190ms folds of
    the same 1,577-event file, both on the Stop path, for one answer. The
    parameter is optional so every other caller is unchanged."""
    if isinstance(snap, dict):
        rows = snap
    else:
        try:
            rows, _err = snapshot()
        except Exception:
            return set()
    # snapshot() returns a DICT KEYED BY ID, not a list. Iterating it directly
    # walks the id STRINGS, every isinstance(row, dict) is False, and the set
    # comes back empty — which made this helper report EVERY seat as unmatched
    # on its first cut, reproducing the exact blindness it exists to remove.
    out = set()
    for r in (rows or {}).values():
        if isinstance(r, dict) and r.get("sender"):
            out.add(str(r["sender"]).casefold())
    return out


def _spiral_prescription(bucket, current):
    """Choose from typed observations on this already-folded chain, not prose.

    Fan-out is one round, never a sum of possibly overlapping findings. Every
    reviewer must agree; absent/contradictory observations buy no exemption.
    The canonical parent walk permits intervening build rows and lane renames.
    """
    counts, previous = [], None
    ordered = sorted(bucket["tips"], key=bucket["tips"].get)
    if len(set(bucket["tips"].values())) != len(ordered):
        return "MELD", "finding trajectory UNKNOWN: ambiguous round order"
    for tip in ordered:
        rows = bucket["observations"][tip]
        count, polarity = rows[0].get("finding_count"), rows[0].get("polarity")
        for row in rows:
            if row.get("status") != "verdict" or row.get("reviewed_tip") != tip \
                    or count is None or _finding_error(
                        row.get("finding_count"), row.get("prior_relation")) \
                    or row.get("finding_count") != count \
                    or row.get("polarity") != polarity \
                    or polarity not in ("fix", "concur"):
                return "MELD", "finding trajectory UNKNOWN: missing, malformed or conflicting verdict observations"
            if previous is not None and not any(
                    row.get("chain_root") not in (None, CHAIN_UNKNOWN)
                    and row.get("chain_root") == parent.get("chain_root")
                    and row.get("repo_id")
                    and row.get("repo_id") == parent.get("repo_id")
                    and _chain_reaches(row, parent["id"], current,
                                       review_predecessor_tip=parent["tip"])[0] is True
                    for parent in previous):
                return "MELD", "finding trajectory UNKNOWN: prior review is not proven on the supersedes chain"
        counts.append(count)
        previous = rows
    slope = " -> ".join(map(str, counts))
    if not all(a > b for a, b in zip(counts, counts[1:])):
        return "MELD", "findings %s are not strictly falling" % slope
    relations = [row.get("prior_relation") for row in previous]
    if not counts[-1] or any(r not in PRIOR_RELATIONS for r in relations) \
            or len(set(relations)) != 1:
        return "MELD", "findings %s; newest all-findings prior-cure relation UNKNOWN (zero is not a regression)" % slope
    if relations[0] != "regression-of-cure":
        return "MELD", "findings %s; newest findings are %s, not regressions of the prior cure" % (slope, relations[0])
    return "FINISH", "convergence: findings %s; all newest findings are regressions of the prior cure" % slope


def review_spiral(sender, hours=SPIRAL_WINDOW_H, now=None, snap=None):
    """(info | None, err) — the worst eligible review chain `sender` is running.
    Blocking MELD chains outrank FINISH advisories; within that tier choose
    the most DISTINCT tips inside the window, then the newest round.
    Nothing below SPIRAL_MELD_ROUNDS is reported.

    info = {chain, lane, rounds, peer, recipients, span_h, since_h,
            prescription, finding_evidence}. The last two are typed-observation
    advice, never approval authority. `span_h` is first tip to newest tip;
    `since_h` is first tip to `now`, the window "did this seat meld DURING
    this spiral" asks about, so it does not shrink as the spiral ages. `peer` is the
    recipient of the most recent round — the seat you are ping-ponging with, and
    so the seat to invite into the meld. `lane` is the MOST RECENT round's
    label, because that is what the seat currently calls this work.

    THE CHAIN, NOT THE LANE STRING. Grouping by lane was wrong in BOTH
    directions at once, which is why neither half could be patched alone:

      OVERCOUNT — two unrelated pieces of work reusing one lane name merged into
      a single fake spiral, and a finished spiral kept counting because the name
      stayed in the window.
      UNDERCOUNT — the real incident. `gate-mints-its-own-evidence` landed and
      `gate-epoch-is-append-order` opened immediately to close a hole in it:
      round 10 of the same work under a new name, and every same-lane rule read
      it as round 1.

    Chain keying fixes both from one relation: a renamed continuation carries
    its parent's root and keeps counting; a reused name roots a new chain and
    starts over.

    ONE CHAIN, NOT ALL OF THEM: two chains at two rounds each is a healthy
    night, and a rung that listed every one would be the wall this prevents.

    SAME SENDER: the guard bills the seat that is stopping, and a seat is never
    gated for someone else's spiral. Rows whose sender was never recorded
    (every row written before `add()` learned to stamp it) match no seat and are
    silently invisible here — fail-open, by construction.

    FAIL-OPEN: an unreadable ledger returns an err and NO finding. Absence
    unproven is never absence, and the caller must not block on the err.

    `snap` is the stop ladder's one `(state, unavailable)` observation; ordinary
    callers omit it and retain an independent current read.
    """
    import calendar
    import time
    if not sender:
        return None, None
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    current, unavailable = snapshot() if snap is None else snap
    if unavailable:
        return None, ("dispatch ledger unavailable (%s) — review rounds "
                      "UNKNOWN, not zero" % unavailable)
    want = str(sender).casefold()
    # THE KEY WAS THE BUG, NOT THE PREDICATE. This matched the caller's
    # RESOLVED SEAT NAME against the ledger's recorded sender, and those are
    # not the same string for every seat. Measured on the live ledger
    # 2026-08-01, nine distinct senders: `helm-claude-2` authors 17 rows under
    # its own name and IS seen; `helm-claude` authors as bare `claude` (the
    # family floor) and was structurally invisible — TEN ROUNDS on one chain,
    # all night, zero detections. Same code, same rung, opposite outcomes,
    # decided entirely by which string got recorded. A seat in a ten-round
    # spiral looked identical to a seat with no rows at all, and NOTHING made
    # that visible, which is the property a guard may never have.
    seen = _sender_strings(now=now, hours=hours, snap=current)
    if want not in seen:
        # NOT ZERO ROUNDS — UNMATCHED. The distinction is the whole fix: this
        # seat may be running any number of rounds under a name this query
        # cannot reach, and saying so is the difference between a quiet gate
        # and a blind one.
        return None, ("no dispatch row is authored by %r — this seat writes "
                      "under a different name than it resolves to, so its "
                      "round count is UNKNOWN, not zero (ledger authors: %s)"
                      % (str(sender), ", ".join(sorted(seen)[:8]) or "none"))
    chains = {}                 # chain id (or "lane:<name>" for legacy) -> rounds
    observations = {}           # verdicts are work facts, across all senders
    settled = {}                # lane -> newest terminal-verdict timestamp
    live_chains = set()         # chains still holding an OPEN row
    for r in (current or {}).values():
        # `kind == "review"` EXPLICITLY. UNKNOWN is a value, not a default (the
        # law `mix` was built on): a row that predates the field is not a review
        # round, it is a row we cannot classify, and inventing rounds out of it
        # would put a made-up number behind a hard block.
        if r.get("kind") != "review":
            continue
        # THE SENDER FILTER USED TO SIT HERE, above `settled`, and that one line
        # of placement was the whole bug — see where it moved to, below.
        if r.get("status") == "cancelled":
            continue
        lane = str(r.get("lane") or "")
        tip = str(r.get("tip") or "")
        peer = str(r.get("recipient") or "")
        if not lane or not tip or not peer:
            continue      # nothing to name in the cure command -> not a finding
        try:
            when = calendar.timegm(time.strptime(str(r.get("ts") or ""),
                                                 "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            continue      # an unparseable stamp is not evidence of age
        if when < cutoff:
            continue
        chain = r.get("chain_root")     # already replayed; see `_replay_chain`
        if chain == CHAIN_UNKNOWN:
            continue      # a corrupt chain is not evidence of a round, and it
                          # must not be merged into a real one either
        # A LEGACY row has no chain, and inventing one would be the lane-name
        # heuristic all over again. It keeps EXACTLY today's behaviour — keyed by
        # its lane, in a namespace no chain id can collide with — so history
        # neither loses its rounds nor contaminates a chained one.
        pol = str(r.get("polarity") or "").casefold()
        if pol in SPIRAL_TERMINAL_POLARITIES:
            # Newest decision per LANE, tracked across every bucket. See the
            # spent-prefix note under the reduce below for why the lane, and not
            # the chain, is the right key for this one fact. Keyed by the lane
            # STEM (#142): a decision recorded under either spelling of the
            # family settles both.
            if when > settled.get(_lane_stem(lane), 0):
                settled[_lane_stem(lane)] = when
        # TWO QUESTIONS, TWO SCOPES — and one filter used to answer both.
        # Counting ROUNDS is PER-SENDER: the guard bills the seat that is
        # stopping, and a seat is never gated for someone else's spiral. So the
        # filter belongs HERE, gating the chain bookkeeping below and nothing
        # above it. Recognising a DECISION is SENDER-BLIND: a verdict is a fact
        # about the WORK, not about who dispatched the round that carried it.
        #
        # With the filter above `settled`, a verdict only counted if the seat
        # being billed had dispatched it — so HANDING A LANE ON, the healthy
        # move, froze your own round count at its high-water mark forever:
        # nothing you dispatched could ever close it again. Live cost: three FIX
        # rounds from the integrator, gemini takes the lane over and APPROVEs
        # at 07:25, and the guard blocked a seat whose lane had been finished
        # for an hour. Rows with no recorded sender still settle, which is the
        # fail-open direction here — an unattributable approve is still an
        # approve, and reading it SUPPRESSES a warning rather than raising one.
        key = chain or ("lane:" + _lane_stem(lane))
        # AN OPEN ROW IS THE ONE UNAMBIGUOUS SIGN A CONVERSATION IS STILL RUNNING,
        # and like `settled` it is SENDER-BLIND: somebody is waiting on a verdict
        # in this chain whoever dispatched the round that asked for it. Read here,
        # above the sender filter, for the same reason `settled` is — a fact about
        # the WORK belongs to the work.
        if r.get("status") == "open":
            live_chains.add(key)
        observations.setdefault(key, {}).setdefault(tip, []).append(r)
        if str(r.get("sender") or "").casefold() != want:
            continue
        b = chains.setdefault(key, {"tips": {}, "last": None, "who": set(),
                                    "observations": observations[key]})
        b["tips"].setdefault(tip, when)
        b["who"].add(peer)
        if b["last"] is None or when >= b["last"][0]:
            b["last"] = (when, peer, lane,
                         str(r.get("polarity") or "").casefold())
            # THE TIP AND ITS REPOSITORY TRAVEL TOGETHER OR NEITHER IS USABLE.
            # A sha is only an identity inside the repository that holds it, and
            # this ledger is global; asking the wrong repo about a real sha is
            # how a probe answers truthfully about something else entirely.
            b["last_tip"] = tip
            b["repo_id"] = r.get("repo_id")
    best = None
    for key in sorted(chains):
        b = chains[key]
        # A SPENT PREFIX. Terminality alone was not enough, and the live ledger
        # is what said so: `land-pipeline-card` is ONE seven-round conversation
        # split across TWO buckets, because its first four rounds predate
        # `chain_root` and land in the legacy `lane:` bucket while the last three
        # carry a real chain. The approve arrives on the chained half, so the
        # legacy half's last round is FOREVER a `fix` — a fragment frozen one
        # step before the ending that already happened. It cannot terminate by
        # its own rows no matter how long you wait, so the terminality check
        # above can never reach it. That fragment is what was still firing after
        # the first fix, and it is why this needed a second one.
        #
        # THE LANE IS THE RIGHT KEY FOR THIS ONE FACT, AND ONLY THIS ONE. The
        # file's standing warning is that a REUSED lane name merges unrelated
        # work — so this never merges counts, and never lets one bucket's rounds
        # raise another's. It asks a strictly weaker question: has this lane been
        # decided SINCE this bucket's last round? Ordering is what makes that
        # safe. Work under a recycled name is NEWER than the old approve, so it
        # is untouched; only rounds that precede a decision are suppressed, and
        # rounds that precede a decision are history by definition.
        if b["last"][0] <= settled.get(_lane_stem(b["last"][2]), 0):
            continue
        rounds = len(b["tips"])
        if rounds < SPIRAL_MELD_ROUNDS \
                or _spiral_conversation_is_over(key, b, live_chains):
            continue
        prescription, evidence = _spiral_prescription(b, current)
        # ROUNDS ARE COUNTED HERE; WHAT THE ROUNDS FOUND IS COUNTED THERE.
        # This rung's whole question was "how many rounds", and three rounds
        # re-litigating one finding read identically to three rounds each
        # finding a defect the last one's arms could not see. Only the second
        # reading is a spiral. The sibling can turn MELD into UNDER-ARMED and
        # can do nothing else — see its docstring for the direction of doubt.
        prescription, evidence = spiral_findings.sharpen(
            b, rounds, SPIRAL_BLOCK_ROUNDS, prescription, evidence)
        # A four-round convergence must not hide a three-round live spiral.
        # Within the same prescription tier retain round/time ordering.
        rank = (rounds >= SPIRAL_BLOCK_ROUNDS and prescription == "MELD",
                rounds, b["last"][0])
        if best is None or rank > best[0]:
            best = (rank, {"chain": key, "lane": b["last"][2], "rounds": rounds,
                           "peer": b["last"][1],
                           "recipients": sorted(b["who"]),
                           "prescription": prescription,
                           "finding_evidence": evidence,
                           "span_h": (b["last"][0] - min(b["tips"].values()))
                           / 3600.0,
                           "since_h": max(0.0, now - min(b["tips"].values()))
                           / 3600.0})
    return (best[1], None) if best is not None else (None, None)


def _spiral_conversation_is_over(key, bucket, live_chains):
    """Did this chain's work SHIP, with nobody still waiting on a verdict?

    THE SECOND SETTLEDNESS SOURCE, and the rung needed one because polarity was
    its only one. A chain whose last decision is not in
    SPIRAL_TERMINAL_POLARITIES can never settle by the rule above, no matter
    what happened to its code — and a whole landing door produces exactly that
    state, so the gate kept firing on a conversation that had ended and
    prescribed a LIVE MELD, which is a conversation, about work already on
    trunk. The remedy names the failure: you cannot converge with anybody about
    a lane that shipped.

    A LANDING IS A STRONGER SETTLEDNESS SIGNAL THAN ANY VERDICT, which is why
    this belongs beside the polarity rule rather than inside it. It also keeps
    the two questions apart: SPIRAL_TERMINAL_POLARITIES answers "may this
    land", this answers "is anyone still arguing", and folding the second into
    the first is what would have put an endorsement into an authorization set.

    TWO CLAUSES, BOTH REQUIRED. An OPEN row means somebody is waiting on a
    verdict right now, and no amount of landed history makes that untrue — a
    chain can ship one tip and immediately open the next round on the next.

    ANCESTRY ALONE CANNOT ANSWER THIS AND USING IT WOULD FAIL ON THE EXACT CASE
    THIS EXISTS FOR. `_tip_on_trunk` in this file is ancestry-only, and the
    incident that produced this clause was a chain landed by CHERRY-PICK: its
    content is on trunk under three other shas and ancestry truthfully says no.
    So this asks `landed_ever`, the named door for "reachable OR
    patch-present", and says which fact it means as that door's own contract
    demands of new callers.

    UNKNOWN NEVER SUPPRESSES. This clause only ever ADDS suppression, so
    failing to measure it leaves the rung exactly as it was — no regression,
    and no way to disarm a live spiral by breaking git. That is the opposite
    choice from `settled` above and deliberately so: an unreadable verdict
    could only hide a warning, while an unreadable landing would hide a BLOCK.
    """
    if key in live_chains:
        return False
    tip, repo_id = bucket.get("last_tip"), bucket.get("repo_id")
    if not _FULL_TIP.fullmatch(str(tip or "")) or not repo_id:
        return False
    try:
        from . import landreq
        # THE GITDIR, PASSED THROUGH UNCHANGED, AND THE FIRST CUT STRIPPED IT.
        # `_tip_on_trunk` two screens up peels "/.git" off with the comment
        # "ancestry wants the repo root", which is TRUE OF THAT CALL and false
        # of this one: landed_ever takes a gitdir. Copying the neighbouring
        # call site's argument convention along with its value made this answer
        # UNKNOWN for every row ever — measured against the live ledger, on the
        # exact chain this clause was written for.
        return landreq.landed_ever(str(repo_id), tip, "origin/main") is True
    except Exception:
        # A rung that cannot look says nothing, and NEVER takes the guard down:
        # this runs inside the Stop hook.
        return False


# ==========================================================================
# FOUR KINDS OF "helm cannot say", and why there are four and not three.
#
# THE DEFECT THIS CLOSES. `approval_tier` answers UNKNOWN many different
# ways, and every consumer treated them as one: the projection memo could
# only evict all of them or none as though the answers were interchangeable,
# and the compose refusal worded them all identically. Measured 2026-08-11
# against the live roster: of ELEVEN seats reading UNKNOWN, ZERO were the
# transient case a retry heals. Six carried a proxywatch record that is
# malformed and will read malformed forever; five carried NO record at all
# because their upstream is dark (gemini MALFORMED200 since 16:33Z, kimi
# AUTH-UNAVAILABLE, three seats with no session). A surface that promises
# every one of them a healing cannot deliver it, and a memo that re-derives
# every one of them per row per pass pays full price for the identical
# answer.
#
# THE FOURTH STATE IS THE POINT. The lane began with three — transient,
# definitively-outside, immutably-unknown — and DARK does not fit any of them.
# It is not transient: nothing is stored, so there is nothing whose next read
# could differ, and a retry is not merely slow but structurally incapable of
# answering. It is not outside: an unproven family is not a disproven one, and
# calling it outside would accuse a seat of a violation nobody measured. And
# it is NOT the same as DAMAGED even though both are permanent under re-reading
# — they have opposite CURES and opposite OWNERS. Damaged says a stored object
# contradicts itself and a human must repair it; the upstream coming back
# changes nothing. Dark says no object exists and no repair is possible or
# owed; it clears when the seat's upstream returns and a proxywatch pass stamps
# it, and touching the ledger is the wrong move entirely. Fold them together
# and the surface sends the 5am integrator to repair a healthy ledger because a
# provider is rate-limiting a seat. That is a whole diagnosis spent.
#
# UNNAMED is separated on the same test: it is permanent under re-reading, but
# its cure is neither repair nor patience — the ROW names a reviewer that is
# not one roster seat, so there is no seat to be in or out of the tier at all.
#
# THE TEST FOR ADDING A FIFTH is not "is this a different sentence" but "does a
# reader who acts on the nearest existing kind do the wrong thing". Stale-clock
# vs failed-canary both mean run it again, so they share TRANSIENT and keep
# their distinct sentences inside it.
TIER_TRANSIENT = "transient"      # the read failed at a LIVE step; the subject
                                  # is unchanged and the next read may answer
TIER_DARK = "dark"                # no proof is stored for this seat AT ALL —
                                  # nothing to re-read, no repair to make
TIER_DAMAGED = "damaged"          # a stored proof/policy exists and is
                                  # malformed or self-contradicting — forever
TIER_UNNAMED = "unnamed"          # the recipient is not one canonical seat
TIER_UNCLASSIFIED = "unclassified"  # NOT a kind of unknown — the ABSENCE of a
                                  # classification. Never inferred; see below.
TIER_PRE_TIER = "pre-tier"        # historical authority was never recorded;
                                  # readable evidence, never authorization
TIER_UNKNOWN_KINDS = (TIER_TRANSIENT, TIER_DARK, TIER_DAMAGED, TIER_UNNAMED,
                      TIER_UNCLASSIFIED, TIER_PRE_TIER)


class TierUnknown(str):
    """A string that also says WHICH kind of unknown produced it.

    USED TWICE, both times to move a kind ACROSS a hop that would otherwise
    destroy it: on the WHY sentence inside the resolver (which gets wrapped and
    concatenated twice more before it leaves), and on the STATE WORD that the
    projection stores. Both hops end in `%`-formatting or a plain str, which is
    exactly why the kind cannot be recovered downstream by reading the prose.

    THE CARRIER RIDES THE STATE WORD RATHER THAN WIDENING THE RETURN because
    `approval_tier` -> `_approval_refusal` -> the projected row is a chain with
    many live call sites, and every one of them compares that word to a plain
    string, hashes it into an anchor, or writes it onto a row. A `str`
    subclass keeps all of that byte-identical — equality, formatting,
    `in ("outside", "unknown")` and `json.dumps` all see exactly "unknown" —
    while the one reader that needs the kind asks for it. Widening the tuple
    would have edited every call site and test double to carry one word to
    one surface."""

    __slots__ = ("kind",)

    def __new__(cls, kind, text):
        if kind not in TIER_UNKNOWN_KINDS:
            raise ValueError("unknown tier-unknown kind %r" % (kind,))
        self = str.__new__(cls, text)
        self.kind = kind
        return self


def _kind_of(text):
    """The kind riding on this string, or None. Untagged is NEVER inferred."""
    kind = getattr(text, "kind", None)
    return kind if kind in TIER_UNKNOWN_KINDS else None


def tier_unknown_kind(tier_state):
    """Which TIER_* this tier answer is, or None when it is not an unknown.

    AN UNTAGGED UNKNOWN IS `TIER_UNCLASSIFIED`, NEVER A GUESS. A plain
    "unknown" string reaching here means nobody measured the kind — a test
    double, a path added later without a tag. Substituting the convenient
    answer (transient, so the surface says retry) is precisely the confident
    wrongness this vocabulary exists to end, so the absence gets its own word
    and its own sentence telling the reader helm did not classify it."""
    if str(tier_state) != "unknown":
        return None
    kind = getattr(tier_state, "kind", None)
    return kind if kind in TIER_UNKNOWN_KINDS else TIER_UNCLASSIFIED


def tier_unknown_heals_itself(kind):
    """Does this unknown clear WITHOUT anyone doing anything?

    ONLY TRANSIENT. Dark clears when an upstream returns (an event, not a
    re-read), damaged never clears without a repair, unnamed never clears
    without an edit to the row, and unclassified is by construction a state
    nobody measured. Any surface that promises healing must ask HERE."""
    return kind == TIER_TRANSIENT


def _tier_unknown(kind, text):
    """Mint one classified UNKNOWN: ("unknown"-with-a-kind, sentence)."""
    return TierUnknown(kind, "unknown"), text


def _approval_identity_family_evidence(recipient, session=None,
                                       require_exact_session=False):
    """Resolve one actor from verified native runtime or measured proxy route.

    This is THE family-of(actor) resolver for approval tiers, contrary-family
    evidence, and cross-family close gates. Native authority is a verified roster
    runtime explicitly stamped backend=native. Proxy authority is separate:
    roster session -> exact live pid -> /proc model/base URL -> exact listener and
    loaded config digest -> unique alias/provider/upstream route -> authenticated
    proxywatch canary (the upstream answering, or the proxy's own cooldown
    refusal naming that route: a wall attests identity, not availability). Seat names, labels, harness/type names,
    unverified roster family, proof storage keys, and recorded family strings
    contribute zero.

    Session-bound native decisions emit v5; sessionless compatibility reads emit
    v4; measured proxy decisions emit v3. Historical v1/v2 close proofs replay
    exactly as recorded and are never reinterpreted.

    EVERY `why` IT RETURNS IS A `TierUnknown` carrying one of TIER_* — see
    that vocabulary above. Callers that only print it are unaffected; the
    tier resolver reads the kind off it before wrapping the sentence. The
    one deliberately UNCLASSIFIED member is the proxywatch snapshot error:
    proxywatch on this base answers its four failure worlds (absent, damaged,
    stale, live-read-failed) in one untagged sentence, so nobody measured
    which — and UNCLASSIFIED is the honest word for that, never a borrowed
    neighbour's confidence.
    """
    from . import proxywatch, seats
    roster, failed = seats.roster_checked()
    if failed:
        # TRANSIENT, and deliberately so on a tri-state that also covers a
        # corrupt file. The roster is rewritten by every seat that heartbeats,
        # so caught-mid-write is the common member; and the fail-safe direction
        # for a kind that decides CACHING is the one that re-asks — a wrongly
        # transient answer costs a cheap re-read, a wrongly durable one freezes
        # a blip for the whole projection. The sentence never promises healing,
        # only "run it again once", which is right for both members.
        return None, None, None, TierUnknown(
            TIER_TRANSIENT, "roster runtime record is unreadable")
    canonical, err = seats._resolve_against(recipient, roster)
    if err:
        return None, None, None, TierUnknown(TIER_UNNAMED, err)
    matches = [(name, row) for name, row in roster.items()
               if seats.recipient_matches(name, canonical)]
    if len(matches) != 1 or not isinstance(matches[0][1], dict):
        return None, None, None, TierUnknown(
            TIER_UNNAMED, "no unique canonical roster runtime record for "
                          "@%s" % canonical)
    roster_identity, row = matches[0]
    session = row.get("session") if session is None else str(session)
    if require_exact_session and not seats.runtime_entry_for_session(row, session):
        # DARK, on the vocabulary's own test: nothing is stored under that
        # author session, so there is nothing whose next read could differ and
        # nothing on the ledger to repair — it clears only by an event (a
        # re-review from a session the roster does record). This site
        # postdates the vocabulary's source lane; the kind is assigned by its
        # rule, not copied from it.
        return None, None, None, TierUnknown(
            TIER_DARK, "@%s has no exact runtime record for author "
                       "session %s" % (canonical, session or "(none)"))
    runtime, verified = seats.runtime_for_session(row, session)
    metadata, rejected = seats._runtime_metadata(runtime)
    native = isinstance(runtime, dict) and bool(runtime) and not rejected \
        and metadata == runtime and verified \
        and runtime.get("backend") in (None, "native")
    family = runtime.get("family") if native else None
    if native and isinstance(family, str) and _TOKEN.fullmatch(family):
        evidence = {"v": 5 if isinstance(session, str) and session else 4,
                    "identity": recipient, "roster_identity": roster_identity,
                    "runtime": dict(runtime), "runtime_verified": True}
        if evidence["v"] == 5:
            evidence["session"] = session
        return {family}, evidence, _subsumed_family_anchor(evidence), None

    if not isinstance(session, str) or not session:
        # DARK, and this is the shape the whole vocabulary was written for: an
        # empty seat slot stores no session, so there is no key under which a
        # proof could ever be looked up. Three live members on 2026-08-11
        # (three claude seats). Nothing to re-read; nothing to repair.
        return None, None, None, TierUnknown(
            TIER_DARK, "no verified native runtime and @%s has no exact "
                       "roster session for proxywatch proof" % canonical)
    entry = seats.runtime_entry_for_session(row, session)
    proof = entry.get("proxy_proof") if entry and \
        entry.get("source") == "proxywatch" else None
    if not proof or not verified or runtime.get("backend") != "proxy":
        # DARK — the seat is seated and NO proxywatch pass has stamped it. This
        # is the measured gemini/kimi case: a dark upstream (MALFORMED200 since
        # 16:33Z, AUTH-UNAVAILABLE) is never stamped, so the absence is not a
        # stale clock and no re-read reaches it. It clears when the upstream
        # returns and a pass stamps the seat.
        return None, None, None, TierUnknown(
            TIER_DARK, "no verified native runtime and @%s has no measured "
                       "exact-session proxy runtime stamp" % canonical)
    family, proof, why = proxywatch.proxy_runtime_snapshot(
        session, expected_proof=proof)
    if why:
        # UNCLASSIFIED, STATED RATHER THAN GUESSED. proxywatch's snapshot
        # errors span four worlds with opposite cures — nothing recorded,
        # record malformed, record aged out, live re-proof failed this
        # instant — and on this base they arrive as ONE untagged sentence, so
        # the kind was never measured. The source lane mapped proxywatch's
        # own proof-kind tags here; until this proxywatch tags its errors,
        # borrowing any neighbour's kind would rebuild the exact confident
        # wrongness the vocabulary ends. UNCLASSIFIED evicts with the
        # transients (the fail-safe: a re-ask costs a read, freezing a blip
        # costs correctness).
        return None, None, None, TierUnknown(
            TIER_UNCLASSIFIED,
            "no verified native runtime for @%s; %s" % (canonical, why))
    if family != runtime.get("family"):
        # DAMAGED: the re-proved family and the seat's own exact-session stamp
        # are both stored, and they disagree. Neither yields on a re-read.
        return None, None, None, TierUnknown(
            TIER_DAMAGED, "measured proxy runtime family for @%s contradicts "
                          "its exact-session stamp" % canonical)
    if not isinstance(family, str) or not _TOKEN.fullmatch(family) or not proof:
        return None, None, None, TierUnknown(
            TIER_DAMAGED,
            "measured proxy runtime family is malformed for @%s" % canonical)
    evidence = {"v": 3, "identity": recipient,
                "roster_identity": roster_identity, "session": session,
                "proxy_proof": proof}
    return {family}, evidence, _subsumed_family_anchor(evidence), None


def _approval_identity_families(recipient):
    """Explicit family evidence for one canonical seat, or why it is unknown."""
    families, _evidence, _anchor, err = \
        _approval_identity_family_evidence(recipient)
    return families, err


_LIVE_APPROVAL_FAMILIES = object()


def _verdict_tier_context(row):
    context = {key: row.get(key) for key in _VERDICT_TIER_CONTEXT}
    if isinstance(context["gate_caps"], tuple):
        context["gate_caps"] = list(context["gate_caps"])
    return context


#: The tier-evidence shapes this reader accepts, newest first. v2 adds the
#: family AXIS and the model that licensed it; v1 is every verdict minted
#: before the axis existed and REPLAYS EXACTLY AS RECORDED — a version rather
#: than a widened v1 because `approval_tier_for_verdict` tests the field set
#: by equality and anchors the whole dict, so growing v1 would turn every
#: minted verdict on this ledger into DAMAGED at once.
_TIER_EVIDENCE_FIELDS = {
    2: {"v", "context", "policy_version", "state", "reason", "kind",
        "family_axis", "family_model"},
    1: {"v", "context", "policy_version", "state", "reason", "kind"}}
_TIER_EVIDENCE_VERSION = 2


def _record_verdict_tier(row):
    """Capture authority at append time, bound to the exact verdict context."""
    from . import verdict_tier
    from .store import policy_history
    context = _verdict_tier_context(row)
    reference, err = policy_history.capture(row.get("repo_id"), context)
    if err:
        return None, err
    record, err = policy_history.resolve(reference, context)
    if err:
        return None, err
    author = row["verdict_author_runtime_evidence"]
    family = author["resolved"]["family"]
    axis, model = verdict_family_axis(author)
    state, why = verdict_tier.evaluate(record["policy"], row.get("recipient"), {family})
    # THE AXIS IS RECORDED, NEVER A VETO. The tier answers MEMBERSHIP and the
    # family it judged is bound by the author proof either way; what the axis
    # says is how strong the input behind that one word was. Denying on a
    # roster axis would refuse every native review this fleet writes, which is
    # a fleet-wide outage wearing the shape of a fix. The reader that cares
    # about diversity reads `family_axis`; nobody has to infer it from silence.
    evidence = {"v": _TIER_EVIDENCE_VERSION, "context": context,
                "policy_version": reference, "state": str(state), "reason": why,
                "kind": tier_unknown_kind(state),
                "family_axis": axis, "family_model": model}
    return {"verdict_tier_evidence": evidence,
            "verdict_tier_anchor": _proof_anchor("verdict-tier-v1", evidence)}, None


def approval_tier_for_verdict(row):
    """Read recorded authority, never today's policy or runtime.

    Absent historical evidence is PRE-TIER, not an unread projection. Partial,
    future-versioned, or contradictory evidence remains DAMAGED. Both deny.
    """
    if not isinstance(row, dict):
        return _tier_unknown(TIER_DAMAGED, "verdict is unreadable")
    present = [key in row for key in VERDICT_AUTHOR_EVIDENCE_FIELDS]
    tier_present = [key in row for key in VERDICT_TIER_FIELDS]
    if any(present):
        if not all(present):
            return _tier_unknown(TIER_DAMAGED, "verdict author runtime proof is incomplete")
        session = row["verdict_author_session"]
        if not isinstance(session, str) or not session:
            return _tier_unknown(TIER_DAMAGED, "verdict author runtime proof is malformed")
        why = _verdict_author_runtime_error(
            row["verdict_author_runtime_evidence"], row.get("recipient"), session,
            row["verdict_author_runtime_anchor"])
        if why:
            return _tier_unknown(TIER_DAMAGED, why)
    if not any(tier_present):
        if row.get("verdict_version") == 4:
            return _tier_unknown(TIER_DAMAGED, "v4 verdict lacks its required record-time tier proof")
        return _tier_unknown(TIER_PRE_TIER,
                             "PRE-TIER: no record-time approval-tier evidence; "
                             "readable historical verdict, nonauthorizing — re-review "
                             "with the current writer, never infer authority from today's roster or policy")
    if not all(tier_present) or not all(present):
        return _tier_unknown(TIER_DAMAGED, "recorded tier or author proof is incomplete")
    evidence = row["verdict_tier_evidence"]
    version = evidence.get("v") if isinstance(evidence, dict) else None
    if not isinstance(evidence, dict) or type(version) is not int \
            or set(evidence) != _TIER_EVIDENCE_FIELDS.get(version, ()):
        return _tier_unknown(TIER_DAMAGED, "recorded tier evidence is malformed or unsupported")
    if evidence["context"] != _verdict_tier_context(row) \
            or row["verdict_tier_anchor"] != _proof_anchor("verdict-tier-v1", evidence):
        return _tier_unknown(TIER_DAMAGED, "recorded tier evidence does not bind this verdict")
    from . import verdict_tier
    from .store import policy_history
    record, err = policy_history.resolve(evidence["policy_version"], evidence["context"])
    if err:
        return _tier_unknown(TIER_DAMAGED, err)
    author = row["verdict_author_runtime_evidence"]
    family = author["resolved"]["family"]
    state, why = verdict_tier.evaluate(record["policy"], row.get("recipient"), {family})
    if (evidence["state"], evidence["reason"], evidence["kind"]) != \
            (str(state), why, tier_unknown_kind(state)):
        return _tier_unknown(TIER_DAMAGED, "recorded tier contradicts its policy/runtime evidence")
    # THE RECORDED AXIS IS RE-DERIVED FROM THE AUTHOR PROOF, exactly as the
    # state above is re-derived from the policy. A stamp nobody re-checks is a
    # claim, not evidence — and this one says how strong the tier's input was,
    # so a row that could carry a false axis would be worse than one carrying
    # none. v1 rows predate the axis and are not asked for it: absent is
    # honest, invented would not be.
    if version >= 2 and (evidence["family_axis"], evidence["family_model"]) \
            != verdict_family_axis(author):
        return _tier_unknown(TIER_DAMAGED,
                             "recorded tier family axis contradicts its author proof")
    return state, why


def approval_tier(recipient, repo=None):
    """(state, message) for a review recipient. THREE outcomes, not two.

      "none"     no approval-tier policy is configured. There is no tier, so
                 there is nothing to be outside of — this is a real answer.
      "ok"       the recipient is inside the tier.
      "outside"  the recipient is DEFINITIVELY outside it.
      "unknown"  a policy exists and could not be evaluated — unreadable,
                 malformed selector, no family evidence, conflicting families.

    UNKNOWN IS SEVERAL ANSWERS, NOT ONE. The returned state word carries which
    — `tier_unknown_kind(state)` -> TIER_TRANSIENT / TIER_DARK / TIER_DAMAGED
    / TIER_UNNAMED (or TIER_UNCLASSIFIED for a state nobody tagged). It is a
    str subclass, so every existing caller is unaffected; see the vocabulary
    above for why the kinds are distinct. ANY consumer that words an unknown
    for a human, or decides whether to cache one, must read the kind — the
    two that did not are the defects the vocabulary exists to close.

    `repo` scopes policy lookup to the reviewed row's repository. Only the send
    advisory omits it and deliberately keeps the caller-CWD behavior.

    THE SPLIT IS THE POINT. `_approval_tier_advisory` returned a STRING for
    every non-ok case, so "there is no policy" and "I could not read the
    policy" and "this seat is not allowed" were one bucket to every caller. A
    land gate cannot be built on that: two of those must permit and one must
    refuse. Measured 2026-07-31 — an out-of-tier APPROVE was recorded on the
    ledger and the row read READY; only one agent noticing held the land.

    The CLI wrapper below preserves its exact wording, so this is a widening,
    not a behaviour change on the send path.

    LIVE ADVISORY CALLS SHARE AN ANSWER inside `projscope.scope()` for the
    same recipient and repository, subject to the unknown-state eviction rule
    below. Outside a scope they recompute. Verdict consumers instead call
    `approval_tier_for_verdict`, which reads retained policy/runtime evidence;
    neither a land refusal nor a close's verdict check borrows this live memo.

    The memo predates that split: measured 2026-08-06 on the live ledger, one
    `helm lr list --all` made 627 calls over sixteen distinct keys and spent
    143 of 226 wall seconds resolving tiers. That historical cost explains the
    advisory memo, not a claim that today's verdict reader still resolves live.

    A LENS INSTALLED ON THIS THREAD OWNS THE ADVISORY ANSWER. It routes this
    function to the read-set serving a build before the ordinary memo, including
    when an unhashable key would make projscope compute again. Recorded verdict
    authority does not enter this lens: its retained policy history has its own
    sealed read-set and per-projection index."""
    lens = getattr(_TIER_LENS, "fn", None)
    if lens is not None:
        return lens(recipient, repo)
    return _approval_tier_memo(
        _approval_tier_key(recipient, repo, _LIVE_APPROVAL_FAMILIES, None),
        lambda: _approval_tier_uncached(recipient, repo))


# PER-THREAD, because helm web is a ThreadingHTTPServer and one build's
# read-set must never answer another's rows.
_TIER_LENS = threading.local()


@contextlib.contextmanager
def tier_lens(fn):
    """Route `approval_tier` through `fn` for the duration of one projection.

    RESTORES THE PREVIOUS LENS rather than clearing, so composing two of these
    cannot silently leave the inner one's resolver installed over the outer's
    remaining rows."""
    prev = getattr(_TIER_LENS, "fn", None)
    _TIER_LENS.fn = fn
    try:
        yield
    finally:
        _TIER_LENS.fn = prev


# PER-THREAD for `_TIER_LENS`'s reason, unchanged: helm web is a
# ThreadingHTTPServer and one build's read-set must never answer another's.
_EPOCH_LENS = threading.local()


@contextlib.contextmanager
def epoch_lens(fn):
    """Route `gate_epoch` through `fn` for the duration of one projection.

    `fn` TAKES NO ARGUMENTS, which is the difference from `tier_lens` and is
    deliberate. The epoch's other two inputs — the ledger snapshot and its
    verdict index — are DERIVED from reads the read-set already holds, so the
    reader re-resolves them rather than accepting a caller's pair. That keeps
    the served term's operand set empty, which is what makes it replayable
    with no saved operand to discard.

    RESTORES THE PREVIOUS LENS rather than clearing, exactly as `tier_lens`
    does, so composing two projections cannot leave the inner resolver
    installed over the outer's remaining rows."""
    prev = getattr(_EPOCH_LENS, "fn", None)
    _EPOCH_LENS.fn = fn
    try:
        yield
    finally:
        _EPOCH_LENS.fn = prev


def _approval_tier_key(recipient, repo, families, canonical_recipient):
    """Name the live resolver's explicit inputs in a hashable memo key.

    The production caller is `approval_tier`: it supplies the live-family
    sentinel and canonical_recipient=None. Recorded verdicts no longer call
    this builder. Selector separation remains a property of the builder, not
    evidence of multiple current production paths through the memo.

    CWD IS NOT IN THE KEY. With repo=None, the live resolver consults
    `project_for_cwd(os.getcwd())`. A scope using that default must keep cwd
    stable; this builder does not enforce that condition. A future scope that
    spans a chdir must address it rather than reuse an answer for another cwd.

    Explicit family sets are normalised to frozensets because projscope.memo
    computes rather than caches for an unhashable key. The live sentinel stays
    itself; None stays None, since no selector and an empty set are different
    inputs. The key-builder tests assert separation and hashability directly,
    not a mutation through distinct live-verdict callers.
    """
    if isinstance(families, (set, frozenset)):
        families = frozenset(families)
    return ("dispatches.approval_tier", recipient, repo, families,
            canonical_recipient)


def _approval_tier_memo(key, compute):
    """Memoise a live advisory resolution, evicting transient unknowns.

    Recorded verdict authority uses retained history, not this memo. The
    historical measurements below explain the advisory eviction rule.

    A TRANSIENT UNKNOWN IS FORGOTTEN; A DURABLE ONE IS KEPT, BECAUSE IT IS AN
    ANSWER. projscope.forget's contract is about a fact "about the MOMENT,
    not about the subject" — a spawn failure, a timeout, an unreadable file.
    That is the TRANSIENT arm and it is why this call exists: measured
    2026-08-11 (task/1067), one canary failure early in an ~8-minute
    projection froze "unknown" for the seat's every row, a whole batch of
    gated approves projected REVIEWED, and the fold paid a second full
    compose to watch them admit.

    FORGETTING ALL OF THEM IS A DIFFERENT BUG WEARING THE CURE'S CLOTHES. A
    malformed proxywatch record, a policy with no reason, a recipient that
    names no seat, a seat whose upstream is dark and stores nothing — none of
    those is a fact about the moment. They are measurements of a subject that
    cannot change inside one projection, so re-deriving them per row buys the
    identical answer at full price and, worse, tells every downstream read
    that helm is still trying. Measured on the live roster the same night:
    ELEVEN seats read unknown and NOT ONE was transient — six damaged, five
    dark. Forgetting every unknown optimises the empty case and pays for the
    whole population.

    UNCLASSIFIED EVICTS WITH THE TRANSIENTS deliberately. An unknown nobody
    tagged might be either, and of the two errors, re-asking costs a read
    while freezing costs correctness. On this base that includes every
    proxywatch snapshot failure — see the resolver's UNCLASSIFIED mint.

    The projection LENS is deliberately outside this door: a lens routes
    `approval_tier` to the read-set serving one build, whose whole contract
    is one recorded resolution per key so the body and its freshness witness
    cannot disagree mid-render."""
    from . import projscope
    hit = projscope.memo(key, compute)
    kind = tier_unknown_kind(hit[0])
    if kind in (TIER_TRANSIENT, TIER_UNCLASSIFIED):
        projscope.forget(key)
    return hit


def _approval_tier_uncached(recipient, repo=None,
                            families=_LIVE_APPROVAL_FAMILIES,
                            canonical_recipient=None):
    """The live resolution. `approval_tier` is the memoising door; this is the
    body it guards, split out so the memo has something to call and so a
    caller that must re-measure can say so."""
    from . import seats, store
    try:
        from .inject._ledger import project_for_cwd
        project = project_for_cwd(repo if repo is not None else os.getcwd())
    except Exception:
        project = None
    policy, why = store.load_certain_policy("approval-tier", project=project)
    if why:
        # ABSENT vs UNREADABLE, asked directly rather than parsed out of the
        # sentence load_certain_policy returns for both.
        if not store.policy_declared("approval-tier", project=project):
            # NO TIER EXISTS. The LAND gate treats that as permission — there
            # is nothing to be outside of — but the SEND path still says it did
            # not validate, because "I did not check" is true either way and
            # that wording is the documented behaviour. Same fact, two
            # consumers, different needs: the resolver carries both.
            return "none", ("approval-tier check unavailable: %s" % why)
        # DAMAGED. Every `why` load_certain_policy returns past this point is
        # STRUCTURAL — ambiguous kind, not explicitly certain, no human source,
        # no members, control characters — and each names the offending prior's
        # id. None of them is I/O: an unreadable store RAISES out of here, and
        # policy_declared above has already taken the absent case. So a re-read
        # returns this identical sentence until somebody edits the store, and
        # the sentence already names the object to edit.
        return _tier_unknown(TIER_DAMAGED,
                             "approval-tier check unavailable: %s" % why)
    if canonical_recipient is None:
        canonical, err = seats.resolve_recipient(recipient)
        if err:
            return _tier_unknown(
                TIER_UNNAMED, "approval-tier check unavailable: %s" % err)
    else:
        canonical = canonical_recipient
    reason = str(policy.get("policy_reason") or "").strip()
    if not reason:
        return _tier_unknown(
            TIER_DAMAGED, "approval-tier check unavailable: policy %s has "
                          "no policy_reason" % policy["id"])
    exact, family_selectors, selectors = set(), set(), []
    for raw in policy.get("policy_members") or []:
        selector = str(raw or "").strip()
        head, sep, token = selector.partition(":")
        if sep != ":" or head not in ("seat", "family") \
                or not _TOKEN.fullmatch(token):
            return _tier_unknown(
                TIER_DAMAGED, "approval-tier check unavailable: policy %s "
                              "has malformed selector %r"
                              % (policy["id"], selector))
        selectors.append(selector)
        if head == "seat":
            seat_token, _err = seats._canonical_recipient(token)
            exact.add(str(seat_token))
        else:
            family_selectors.add(token)
    if canonical in exact:
        return "ok", None
    if family_selectors:
        if families is _LIVE_APPROVAL_FAMILIES:
            families, why = _approval_identity_families(canonical)
            if why:
                # CARRY THE RESOLVER'S OWN CLASSIFICATION. It measured which
                # kind this is; wrapping the sentence must not lose it, and
                # re-deriving it from the wrapped prose is the parse this
                # vocabulary replaced. An untagged why (a test double, a path
                # added later without a tag) stays UNCLASSIFIED rather than
                # borrowing a neighbour.
                return _tier_unknown(
                    _kind_of(why) or TIER_UNCLASSIFIED,
                    "approval-tier check unavailable for @%s: %s"
                    % (canonical, why))
        if not families:
            # DARK by meaning: no author-runtime evidence EXISTS for this
            # historical verdict — nothing stored to re-read, nothing on the
            # ledger to repair, and it clears only by an event (a re-review
            # from a snapshot-stamped writer), never by asking again.
            return _tier_unknown(
                TIER_DARK, "approval-tier check unavailable for @%s: no "
                           "immutable verdict-time runtime family evidence "
                           "(current roster sessions never rewrite history)"
                           % canonical)
        if len(families) > 1:
            # DAMAGED: two explicit family proofs for one seat contradict each
            # other. Both are stored; neither yields on a re-read.
            return _tier_unknown(
                TIER_DAMAGED, "approval-tier check unavailable for @%s: "
                              "conflicting explicit families %s"
                              % (canonical, ", ".join(sorted(families))))
        if next(iter(families)) in family_selectors:
            return "ok", None
    valid = ", ".join(sorted(set(selectors)))
    return "outside", ("@%s is outside the current approval tier; reason: %s; "
                       "source prior: %s; valid set: %s"
                       % (canonical, reason, policy["id"], valid))


def _approval_tier_advisory(recipient):
    """The WRITE-DOOR wording. Advice only: it never blocks a dispatch, and
    the land gate reads `approval_tier` directly instead.

    Called from the recipient rungs rather than from a verb, so a REBOUND
    review carries it too — the reviewer a re-route just installed is exactly
    the one nobody vouched for."""
    state, msg = approval_tier(recipient)
    if state == "ok":
        return None
    if state == "outside":
        return ("WARNING: review recipient %s. Warning only — review dispatch "
                "continues." % msg)
    return "NOTE: %s; review dispatch continues." % msg


def _ref_sanity(ref, lane, repo=None, kind=None):
    """Advisory warnings about a --ref that probably does not mean what the
    sender thinks. -> list of one-line strings, [] when fine or unknowable.

    WARN, NEVER REFUSE. A post-land review is legitimate and valuable, and a
    false refusal blocks real work; a zombie row only costs one surfaced row.
    Every git call is bounded and fails OPEN — an unreadable repo yields no
    warnings rather than noise.

    TWO WAYS A REF LIES, both committed by the integrator on 2026-07-26:

    1. ALREADY ON TRUNK. A build/review dispatch whose ref is already merged is
       usually asking for the past. Live: a build sent to one seat with --ref
       at that seat's OWN commit, already on main as a cherry-pick, so the row
       asked the recipient to gate its own finished work. It surfaced only
       because the recipient refused it instead of complying.

    2. UNRELATED TO ITS LANE. Row 1bc1b2c2 (lane aspublic-test-fixtures) carried
       a real, accurate review bound to a tip that was a DIFFERENT lane's merge
       three commits earlier — a tip still containing everything the change
       removed. Every surface read gated-and-landed, and the LR row said LANDED,
       because the tip WAS on main. A true statement about the wrong commit.

    WHY THIS CHECK ONLY WORKS AT WRITE TIME: after the lane merges, its own
    commits become indistinguishable from trunk's, so a retrospective audit
    cannot answer "was this the lane's tip when it was reviewed" at all. Two
    scans attempting exactly that were both unsound. Here the lane has not
    merged and one merge-base answers it.

    Review rows also name WHO owns base freshness. A fresh author-side ancestry
    check can become stale one instruction later, so it is never a land proof.
    Authors preserve the reviewed tip; the integrator serializes landing order,
    chain-rebases, and runs the one final exact-tree gate. This is guidance, not
    a refusal — a stale tip is still valid review content.
    """
    out = []
    ref = (ref or "").strip()
    if not ref:
        return out
    root = repo or "."
    # Through the vcs SEAM, never a raw subprocess: tests/test_vcs.py pins the
    # number of direct git spawns outside vcs.py and fails when one appears.
    # It caught this function's first draft, which shelled out directly — the
    # guard doing exactly its job, and the seam is the better call anyway since
    # it already handles timeouts and a missing repo.
    try:
        from . import vcs
        _be = vcs.backend(root)
    except Exception:
        return out

    class _R(object):
        def __init__(self, stdout):
            self.stdout = stdout

    def git(*a):
        try:
            rc, o, _e = _be.text(root, *a)
        except Exception:
            return None
        return _R(o) if rc == 0 else None

    if git("cat-file", "-e", ref + "^{commit}") is None:
        return out                       # not a commit here: unknowable, silent
    # THE TRUNK NAME IS NOT ALWAYS "main" — ask helm's own resolver rather than
    # guessing. vcs.base_branch() is "main when it exists, else the shared
    # checkout's own HEAD", which is the same integration branch every other
    # helm surface uses. Hardcoding main/origin-main made this whole check
    # SILENTLY SKIP on any repo trunked elsewhere: the first version answered
    # zero warnings against a fixture on `master` and looked like it passed.
    trunk = None
    try:
        b = _be.base_branch(root)
        for cand in ("origin/" + b, b):
            if b and git("rev-parse", "--verify", cand) is not None:
                trunk = cand
                break
    except Exception:
        trunk = None
    # The seam's TRI-STATE, not `is not None` over a folded exit code: the old
    # read made rc 128 (unreadable object — the phantom-unlanded-lanes shape)
    # indistinguishable from a clean "not on trunk", so an ancestry this check
    # could not SEE produced the same silence as one it had verified. A clean
    # negative stays silent; UNKNOWN now says so — warn, never refuse, and an
    # unknown-flavored note is still just a note.
    if trunk:
        anc = _be.ancestry(root, ref, trunk)
        if anc == vcs.ANCESTOR:
            out.append("NOTE: --ref %s is ALREADY on %s. A review of landed "
                       "work is legitimate; a review of work you meant to gate "
                       "BEFORE it landed is not. Check you meant this tip."
                       % (ref[:12], trunk))
        elif anc == vcs.UNKNOWN:
            out.append("NOTE: cannot tell whether --ref %s is on %s — the "
                       "ancestry read failed (missing/corrupt object, e.g. a "
                       "tip orphaned by a history rewrite). UNKNOWN, not a "
                       "clean 'unmerged': check the tip by hand."
                       % (ref[:12], trunk))

        # BASE FRESHNESS IS NOT AN AUTHOR CAPABILITY. Three seats each rebased,
        # checked trunk ancestry, gated, and truthfully reported; another land
        # moved trunk between that check and the report every time. The only
        # actor that knows landing order is the integrator. Review still binds
        # this immutable content tip; final composition + one exact-tree gate
        # happen after the order is fixed. Advisory, never refusal: refusing a
        # stale author tip would make the structural queue impossible to review.
        # THE SAME NORMALISATION _tier_note OWNS, for the same reason. This
        # is LATENT rather than live: `cmd_dispatch` binds kind through
        # clean_kind at 12830 and is the only caller today, so no fleet path
        # reaches here with a raw spelling. It is cured anyway because the
        # asymmetry is the defect — a predicate that answers about a kind
        # should decide what a kind IS, rather than trusting every future
        # caller to have normalised first. That trust is exactly what broke
        # _tier_note, where BOTH doors normalised one line too late.
        if clean_kind(kind)[0] == "review" and anc != vcs.ANCESTOR:
            relation = _be.ancestry(root, trunk, ref)
            if relation == vcs.ANCESTOR:
                out.append("NOTE: review --ref %s is ff-able from %s NOW, but "
                           "that author-side check cannot stay true while "
                           "another land moves trunk. Do not rebase or re-gate "
                           "as author; the reviewer binds this exact tip, then "
                           "the integrator chooses landing order, rebases the "
                           "chain, and runs the final exact-tree gate once."
                           % (ref[:12], trunk))
            elif relation == vcs.NOT_ANCESTOR:
                out.append("NOTE: review --ref %s is NOT ff-able from %s. This "
                           "is structural, not author error: do not rebase or "
                           "re-gate as author. The reviewer binds this exact "
                           "tip; the integrator chooses landing order, rebases "
                           "the chain, and runs the final exact-tree gate once."
                           % (ref[:12], trunk))
            else:
                out.append("NOTE: review --ref %s base relation to %s is "
                           "UNKNOWN. Silence is not freshness: do not rebase or "
                           "re-gate as author. The integrator owns landing "
                           "order, final composition, and the exact-tree gate."
                           % (ref[:12], trunk))
    # The row's OWN branch is lane/<prefix-stripped lane>, SUFFIXES INTACT
    # (#142 r2, finding 5): collapsing to the family stem probed
    # lane/feature for a real lane/feature-review; the raw lane probed
    # lane/lane/foo for historical prefixed rows.
    own_lane = _strip_lane_prefix(lane)
    br = "lane/%s" % own_lane if own_lane else ""
    if br and git("rev-parse", "--verify", br) is not None and trunk:
        mb = git("merge-base", trunk, br)
        if mb:
            own = git("rev-list", "%s..%s" % (mb.stdout.strip(), br))
            full = git("rev-parse", ref + "^{commit}")
            if own is not None and full is not None \
                    and full.stdout.strip() not in own.stdout.split():
                out.append("NOTE: --ref %s is NOT one of %s's own commits — it "
                           "may belong to another lane. A verdict bound to an "
                           "unrelated tip reads as gated on every surface."
                           % (ref[:12], br))
    return out


def _recipient_gate(raw, force, door):
    """(cap, refusal) — PER-DOOR POLICY over seats.recipient_capability.

    IT DECIDES NOTHING ABOUT IDENTITY. seats owns canonical identity and
    roster evidence; this owns only what each door does with those facts, so
    the re-derivation that lost a fact in each of three review rounds is gone
    rather than relocated.

    THE DOORS DIFFER BECAUSE THEIR MISTAKES COST DIFFERENTLY:
      send/add FAIL OPEN on UNKNOWN. They mint a NEW row, so a wrong refusal
        destroys a real dispatch while a wrong admit costs one unowned row.
      rebind FAILS CLOSED on every non-JOINED state — ABSENT, EMPTY and
        UNREADABLE alike. It MOVES AN OBLIGATION A SEAT IS ALREADY CARRYING,
        so a wrong admit strands live work; that is strictly worse than
        refusing a recovery and being asked again.
      --force is send/add ONLY, and deliberately NOT honoured at rebind:
        rebind's --force already attests SOURCE starvation and carries its own
        mandatory reason. Reusing it as TARGET-existence evidence is ONE FLAG
        PROVING TWO FACTS, and since every manual rebind carries it the gate
        would be disabled on exactly the population it exists for
        (a proposed bypass, refused correctly).
        Recovery through unreadable TARGET evidence, if it is ever actually
        needed, wants its OWN attestation with its OWN reason."""
    from . import seats as _s
    cap = _s.recipient_capability(raw)
    if cap["error"]:
        # SURFACED UNCHANGED. Swallowing it made a malformed address report as
        # "no roster row" — true-sounding, and an answer to a question nobody
        # asked. The grammar error is the one the caller can act on.
        return cap, cap["error"]
    if cap["membership"] == "JOINED":
        return cap, None
    if door != "rebind" and cap["membership"] == "UNKNOWN":
        return cap, None
    if door != "rebind" and force:
        return cap, None                 # deliberate pre-join address
    return cap, _unroutable_text(cap, door)


def _unroutable_text(cap, door):
    """The refusal a door shows, naming the evidence it actually has."""
    why = {
        "populated": "%r has no roster row, so nobody can receive it" % cap["canonical"],
        "empty": "no seat has joined this box yet, so %r cannot be reached"
                 % cap["canonical"],
        "read-failed": "the roster could not be read, so %r cannot be shown to"
                       " hold a row" % cap["canonical"],
    }.get(cap["evidence"], "%r cannot be routed" % cap["canonical"])
    hint = ""
    if cap["evidence"] == "populated":
        try:
            from . import cli
            # The candidates were laundered from the SAME validated roster that
            # produced membership. Re-reading here made the refusal a second,
            # potentially contradictory capability snapshot.
            hint = cli.suggest(cap["canonical"], cap.get("_candidates", ()), n=3)
        except Exception:                # noqa: BLE001 — a hint is never load-bearing
            hint = ""
    # THE REMEDY IS PER-DOOR AND MUST BE RUNNABLE. An earlier version told a
    # rebind caller to "send anyway with --force" — a flag they had already
    # passed, which cannot open that door.
    remedy = {
        "send": ("Send anyway with `--force` to address a seat before it joins;"
                 " that row will NOT claim delivery."),
        "add": ("Add anyway with `--force` to address a seat before it joins;"
                " that row will NOT promise pickup."),
        "rebind": ("`--force` cannot open this door — at rebind it attests the"
                   " SOURCE recipient is starved, not that the TARGET exists,"
                   " and one flag cannot prove both. Rebind moves an"
                   " obligation a seat already carries, so it refuses unless"
                   " the target is proven joined. See `helm dispatch --help`;"
                   " to hand work to a seat that has not joined, cancel this"
                   " row and send a new one with --force."),
    }.get(door, "")
    return ("recipient %s%s — `helm chat seats` lists the live seats. %s"
            % (why, hint, remedy))


def _stdin_has_a_body_fd(stream, window=0.2):
    """Is there ACTUAL DATA (or EOF) waiting on `stream`, within a window?

    ONE PREDICATE, TWO CALLERS: `helm chat post|dm` asks the identical question
    of the identical fd, so this delegates rather than keeping a second copy.
    The terms live with the definition in chat.stdin_has_a_body_fd -- EOF
    counts as ready so the /dev/null scripted pole still reaches its read,
    unselectable stdin answers True so this can only convert a hang into a
    usage error and never invent a refusal, and the window is advisory because
    every real producer is ready at fork. Those are exactly the details that
    drift when one question has two implementations.
    """
    from . import chat          # deferred, as every other chat use here is
    return chat.stdin_has_a_body_fd(stream, window)


# THE READ VERBS OF THIS DOOR, and deliberately not the whole verb table.
#
# A LITERAL TUPLE, never `frozenset(...)`, and the difference is not taste.
# Two of this tree's censuses read module constants out of the SOURCE: the
# dispatcher-honesty sweep recognises a door by its comparison against a
# literal string collection, and the documented-subverb scan resolves the name
# it is compared with. A collection built by a CALL is not a literal to the
# first of them, and a door it cannot recognise is silently dropped from the
# sweep rather than failed by it — the guard would be lost, not tripped.
DISPATCH_READ_VERBS = ("list", "triage", "mix", "briefs", "collisions")


def cmd_dispatch(args):
    """`helm dispatch` — one memo scope for the READ verbs, none for the rest.

    WHAT WAS MEASURED. This door answers both the projections and the writes,
    and it entered no `projscope.scope()` at all, so helm's per-pass memo was
    allocated and never consulted on the busiest read path it has. One
    `dispatch list` over a four-thousand-row ledger spawned git about fourteen
    hundred times; inside a single scope the same listing asks several hundred
    fewer questions and prints the same bytes. Every spawn the scope removes is
    a BYTE-IDENTICAL REPEAT of an argv already run in the same pass — the memo
    is not making an answer cheaper, it is refusing to ask one question twice.

    WHY NOT WRAP THE WHOLE TABLE, which is the obvious and wrong alternative.
    `projscope`'s docstring states the contract this narrowing exists to keep:
    outside a scope nothing is cached, and that is exactly what keeps the write
    paths safe — the land door, the close ladders and the send advisory all
    call the same derived readers and none of them may be answered from an
    older question. `send`, `verdict`, `cancel`, `hold`, `release`, `rebind`,
    `retip` and `mark-delivered` decide what to APPEND from those same reads.
    A memo around them would let a row be written against an ancestry fact
    resolved before the writer's own effect, and the resulting entry would be
    wrong in a way no later read could detect, because the ledger records the
    decision and never the instant that justified it. A listing that is one
    pass stale is a listing; a write that is one pass stale is a lie.

    WHY A WRAPPER RATHER THAN A `with` INSIDE THE BODY. The verb table is one
    long function with many exits, and a scope opened at one of them binds only
    the verbs a later editor remembers to re-check. Selecting on the verb at
    the door makes the read set a NAMED, greppable value: a verb added to the
    table gets no scope until someone adds it here on purpose, which is the
    safe direction to fail. The public name stays `cmd_dispatch` because
    `cli.py` resolves it by that string; the body it delegates to is
    `_cmd_dispatch` and is otherwise untouched.
    """
    args = list(args or [])
    verb = args[0] if args else None
    if verb in DISPATCH_READ_VERBS:
        with projscope.scope():
            return _cmd_dispatch(args)
    return _cmd_dispatch(args)


# ---------------------------------------------------------------------------
# THE OWNER-NAMES TABLE: what `dispatches_cli` owns and this module re-exports
# ---------------------------------------------------------------------------
# EVERY NAME BELOW IS IMPORTABLE FROM `dispatches` EXACTLY AS BEFORE, which is
# what makes the split behaviour-neutral for callers: `cli.py` resolves
# `cmd_dispatch` by string, and four arms name `dispatches._cmd_dispatch`
# directly — three through `inspect.getsource`, which follows a re-bound
# function to its real file, and one registry keyed by the bare name whose own
# comment says the name FOLLOWS THE BODY.
#
# IT IMPORTS THE MODULE, NEVER THE NAMES, AND THAT IS WHAT MAKES BOTH IMPORT
# ORDERS SAFE. `dispatches_cli` imports this module EAGERLY, the way
# `rowworld` and `landreq` do, and publishes its own names back onto this one
# as `web_compat` does for `web`. Importing NAMES here instead deadlocks the
# reverse order -- measured: `from helm import dispatches_cli` in a fresh
# interpreter raised ImportError from a partially initialised module, and a
# test module that imports the satellite directly is exactly what the
# UNTESTED rung requires somebody to write. A MODULE object exists in
# sys.modules from the first line of its execution, so this binding succeeds
# whichever module the process reaches first.
# ONE LITERAL, AND IT DRIVES THE BINDING. `_OWNER_NAMES` is the form
# `web_compat` uses for the web split: (satellite module token, (names...)).
# The satellite's publish loop reads THIS tuple, so the declaration and the
# runtime binding cannot drift -- there is no second list to forget.
#
# IT IS ALSO WHAT THE RETIRED-NAME RUNG READS. A name removed from this file
# is not retired when this literal hands it to a satellite that actually
# defines it; without the declaration the rung refuses, because a move that
# forgot to republish leaves every `dispatches.NAME` consumer dangling and a
# focused set cannot see that.
_OWNER_NAMES = (
    ("dispatches_cli", (
        "_IMPERFECT_FLAG", "_chain_note", "_cmd_dispatch",
        "_hold_holder_nudge", "_json_scope_accounting", "_parse_send", "_rc",
        "_scope_class", "_unplaceable_classes", "_unplaceable_fact",
        "_unplaceable_heading", "list_scope_argv", "patch_note",
        "retip_argv_ok", "UNPLACEABLE_SCOPE_CLASSES",
    )),
    ("dispatches_close", (
        "_build_landed_event_error", "_carried_unverdicted",
        "_chain_declarers", "_close_event_error", "_close_idempotent",
        "_close_mode_error", "_content_proof_pair_error",
        "_contradiction_discharge_error",
        "_delivered_report_event_error", "_delivery_error",
        "_rebind_contradiction", "_record_abandon_proven",
        "_record_close_landed_proven", "_record_close_proven",
        "_record_discharge_proven", "_record_retire_proven",
        "_record_withdraw_proven", "_witness_side_error",
        "_writer_landed", "author_evidence_claimed", "discharging_row",
        "held_discharge_error", "held_rung_remedy",
        "record_delivered_report_correction",
    )),
)

from . import dispatches_cli          # noqa: E402  (tail binding)
from . import dispatches_close        # noqa: E402  (tail binding)
