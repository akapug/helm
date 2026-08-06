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
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

from . import eventledger, gate, home, pk
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

    A BARE `--` ENDS THE OPTIONS AND IS NOT ONE (@codex-2, T1, measured at
    exact tip — the fourth shape, which the positional cure did not reach).
    Positional flags stopped prose from minting a basis and also made evidence
    whose FIRST token is flag-shaped UNREPRESENTABLE: the scan ate it, and the
    conventional escape was eaten too, landing in the polarity bucket as an
    unknown polarity and refusing with rc 2. Only the first bare `--` while
    still scanning flags is punctuation; one inside the evidence is a word the
    reviewer wrote and survives verbatim.
    """
    cut = 2                                 # id and tip are positional
    while cut < len(rest) and rest[cut].startswith("--"):
        if rest[cut] == "--":               # end-of-options, not an option
            break
        cut += 1
    flags = rest[2:cut]
    tail = rest[cut:]
    if tail and tail[0] == "--":
        tail = tail[1:]                     # the terminator is not evidence
    return flags, tail


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
# The first design grandfathered by WALL TIME. @codex measured why that cannot
# work: a timestamp does not say whether the writer COULD mint a receipt. The
# boundary instant passed while this feature was still unlanded and trunk still
# ran the old writer, so the real ledger already holds verdicts stamped AFTER
# it that no gate verb existed to serve. Moving the constant just repeats the
# race on the next round.
#
# The SECOND design stamped an integer and asked `>= 1`. Also @codex, in the
# meld that replaced round 7: a numeric policy answers today and invites
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
# (codex-3, meld e:1785552432: the write side of the mapping is the identity).
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
    isinstance guard alone let '' through (fable delta MED), and the r2
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


def _recipient_label(row):
    """The recipient spelling intended for humans, never identity joins."""
    return str(row.get("recipient_display") or row.get("recipient") or "")


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
        out = dict(row)
        out.update(status="open", delivery="needs-confirmation",
                   migration=None, seq=0, tip=str(row["tip"]).lower(),
                   recipient=recipient, recipient_display=recipient_display,
                   # Replay is NOT a trust boundary — write-time is the gate —
                   # but a hand-edited or forged chain must read UNKNOWN here
                   # rather than reach a consumer as a plausible id. `dict(row)`
                   # alone passed the raw field straight through.
                   chain_root=_replay_chain(row.get("chain_root")),
                   supersedes=_replay_chain(row.get("supersedes")))
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
           # REPLAY IS BYTE-TRUTH (#142 round 2, codex): the stored lane is
           # never rewritten on read — historical attest sidecar bindings hash
           # these bytes, and joins normalize at the JOIN instead.
           "lane": row.get("lane"), "tip": exact,
           "ref": row.get("ref"), "note": row.get("note"),
           "deadline_s": int(row.get("deadline_s")),
           "source": row.get("source"), "sender": row.get("sender"),
           "repo_id": row.get("repo_id"),
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

# The cancel reason's length budget, named ONCE because two call sites depend on
# agreeing about it: mark_cancel REFUSES a longer reason, and the rebind abort
# CLAMPS to it before calling. A literal in each place is a literal that drifts,
# and the drift is silent — the abort would simply start failing again.
_CANCEL_REASON_CAP = 256

# NO NUMERIC CAP ON THE SUPERSESSION WALK, and the reason is worth keeping.
# A cap was here to bound a cycle — but every walk below carries a VISITED SET,
# and that alone terminates: the ledger is finite, so a walk that never revisits
# a node cannot run forever. The number therefore protected against nothing it
# was written for and could only ever do one thing: TRUNCATE A VALID LONG CHAIN.
# It did exactly that (codex-2, adversarial sweep — a legitimate 66-link chain
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
    """A row actively awaiting a verdict -- not closed and not held."""
    return row.get("status") not in CLOSED_STATES and row.get("status") != "held"


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
_NON_CARRYING_FLAGS = ("withdrawn", "abandoned")


def moved_nothing(row):
    """True when this successor took no obligation and is a pass-through."""
    if not isinstance(row, dict):
        return False
    if row.get("status") in _NON_CARRYING_STATUS:
        return True
    return any(bool(row.get(f)) for f in _NON_CARRYING_FLAGS)


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

    CARRYING IS NOT "NOT TERMINAL". A cancelled, withdrawn or abandoned
    successor moved nothing and is a PASS-THROUGH: the walk continues into its
    own successors, because the work may have moved on twice. A successor that
    is open OR reached a verdict DID take the obligation and carries it.

    EVERY UNKNOWN RESOLVES TOWARD VISIBLE. No successors, an unreadable row, or
    a cycle all return None, which leaves the parent SHOWN. A duplicate row has
    two entries shouting and someone reconciles them; a hidden one has nobody,
    and this whole class of defect is rows that stopped being visible while
    still being owed.
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
            if not moved_nothing(kid):
                # root already reaches kid through this walk. Kid reaches root
                # iff both are in the same cyclic SCC; a ring holds nothing.
                if root_cycle is not None \
                        and cycles.get(kid_id) == root_cycle:
                    continue
                return kid                 # open or verdict'd: it holds the debt
            seen.add(kid_id)
            frontier.append(kid_id)
    return None


# ---------------------------------------------------------------------------
# CURE AWAITING REVIEW — the state that falls between both frontiers
# ---------------------------------------------------------------------------

# A FIX verdict says the AUTHOR owes a cure, so every surface reads the row as
# "waiting on the author". When the author HAS cured and never re-dispatched,
# the row still says FIX and NOBODY IS WAITING ON ANYBODY: the author believes
# they are done, the board says they owe work, no reviewer holds it.
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


def _cure_index(root=None, trunk="origin/main"):
    """({sha: (branch, tip)} for every commit AHEAD of trunk on a live branch,
    err) — the ONE git read this whole rung costs.

    O(BRANCHES), NOT O(ROWS), and that is the whole reason it can exist. The
    obvious shape is `git for-each-ref --contains <reviewed_tip>` per row: 105ms
    each, 459 rows, 48 SECONDS. Inverting it — walk each unmerged branch once
    and map every commit it carries ahead of trunk — costs 1.14s total and
    answers every row from a dict. `--no-merged` halves the branch set (183 of
    233 here) with IDENTICAL coverage, because a merged branch carries nothing
    ahead of trunk by definition.

    ANY FAILURE RETURNS (None, err) AND EVERY CALLER MUST DEGRADE TO UNKNOWN.
    A rung that cannot read git must never say "the author owes" — that is a
    verdict about somebody's obligations built on a missing measurement."""
    from . import vcs
    # `repo` is the WORKING TREE, `repo_id` is the git COMMON DIR — ancestry
    # wants the tree, and the file already warns about this exact confusion
    # elsewhere ("repo_id is the recorded GIT COMMON DIR; ancestry wants the
    # repo root"). Reading the wrong key here fails closed to UNKNOWN, which
    # is how I found it: 455 rows UNKNOWN and not one false verdict.
    root = root or (_repo_info() or {}).get("repo")
    if not root:
        return None, "no repository root"
    back = vcs.backend(root)
    rc, out, err = back.text(root, "for-each-ref", "--no-merged", trunk,
                             "--format=%(refname:short) %(objectname)",
                             "refs/heads/")
    if rc != 0:
        return None, "for-each-ref failed: %s" % (err or rc)
    index = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, tip = parts
        rc, shas, err = back.text(root, "rev-list", tip, "--not", trunk)
        if rc != 0:
            # A branch we cannot walk makes the index INCOMPLETE, and an
            # incomplete index answers "no cure exists" for rows whose cure is
            # on exactly that branch — a false AUTHOR OWES. Refuse whole.
            return None, "rev-list failed on %s: %s" % (name, err or rc)
        for sha in shas.split():
            index.setdefault(sha, (name, tip))
    return index, None


def cure_state(row, index, successors=()):
    """Which of CURE_* this FIX row is in. `index` None -> CURE_UNKNOWN.

    Pure given the index, so the git cost is paid once by the caller and every
    row is classified from memory."""
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
    if btip == tip:
        return CURE_AUTHOR_OWES, (branch, btip)
    return CURE_AWAITING, (branch, btip)


def cured_unwitnessed(snap, ids=None, index=None, root=None,
                      trunk="origin/main"):
    """([(row, (branch, tip, ahead))], err) — FIX rows whose author CURED and
    never re-dispatched, so NOBODY is waiting on them.

    err IS NOT AN EMPTY LIST. A git failure returns ([], "reason") and every
    caller must say UNAVAILABLE rather than print nothing: an empty result and
    a broken walk are indistinguishable to a reader, and publishing the second
    as the first is how a false all-clear gets believed. (The integrator
    published two false zeroes the night this was specified, from exactly
    that.)

    THE THREE CLAUSES, in cost order — ledger first, git last:
      1. newest verdict is FIX and it names a reviewed_tip
      2. NO chain successor: a successor means a reviewer already holds it
      3. a live branch carries reviewed_tip with a tip AHEAD of it
    Clause 2 before clause 3 is not an optimisation, it is the difference
    between a rung and a nag: 57 of 67 cured rows on the live ledger HAVE a
    successor and are healthy in-flight work."""
    if index is None:
        index, err = _cure_index(root=root, trunk=trunk)
        if err:
            return [], err
    successors = {}
    for row in snap.values():
        parent = row.get("supersedes")
        if parent:
            successors.setdefault(parent, []).append(row.get("id"))
    out = []
    for row in snap.values():
        if row.get("polarity") != "fix":
            continue
        if ids and not any(str(row.get("id", "")).startswith(w) for w in ids):
            continue
        state, where = cure_state(row, index,
                                  successors.get(row.get("id"), ()))
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
    return out, None


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


def _not_closed(row):
    """A row that is not terminal -- open OR held."""
    return row.get("status") not in CLOSED_STATES


def _successor_frontier(current, rid):
    """(open_successor_ids, unknown_ids) for ONE parent off ONE projection.

    THE ONE OWNER of retip's successor-frontier read, writer and replay both
    (codex P1 on this verb's second cut: each caller wrote its own
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
        if not isinstance(r, dict) or not _not_closed(r) \
                or r.get("id") == rid:
            continue
        if r.get("supersedes") == rid:
            successors.append(str(r.get("id") or ""))
        elif r.get("supersedes") == CHAIN_UNKNOWN:
            unknown.append(str(r.get("id") or ""))
    return sorted(successors), sorted(unknown)


def _apply(state, row, current=None, verdicts=None):
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
    expected = int(state.get("seq") or 0) + 1
    strict = type(row.get("v")) is int and row.get("v") == 3 \
        and type(row.get("seq")) is int and row.get("seq") == expected
    # An OPEN BUILD row has no verdict of its own. Delivered reports close on
    # their own artifact + handoff evidence; landed builds close through an
    # accepted review descendant. Both strict variants apply before ordinary
    # OPEN transitions; every later event then meets CLOSED_STATES.
    if event == "close" and strict \
            and row.get("close_reason") == "delivered-report" \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts) is None:
        out = dict(state)
        out.update(status="closed", close_reason="delivered-report",
                   close_ts=row.get("ts"),
                   close_evidence=row.get("close_evidence"), seq=expected)
        for key in _CLOSE_STATE_FIELDS["delivered-report"]:
            out[key] = row.get(key)
        return out
    if event == "close" and strict and row.get("close_reason") == "landed" \
            and row.get("close_proof_version") == 2 \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts) is None:
        out = dict(state)
        out.update(status="closed", close_reason="landed",
                   close_ts=row.get("ts"), close_evidence=None, seq=expected)
        for key in _CLOSE_STATE_FIELDS["landed"]:
            out[key] = row.get(key)
        return out
    # #177 — the polarity-less row's one terminal. Same early-arm shape as
    # build-landed: an OPEN row never reaches the verdict-only close arm in
    # the CLOSED_STATES block, and the writer's projection is this same call.
    if event == "close" and strict \
            and row.get("close_reason") == "discharged" \
            and _close_event_error(row, state, current=current,
                                   verdicts=verdicts) is None:
        out = dict(state)
        out.update(status="closed", close_reason="discharged",
                   close_ts=row.get("ts"),
                   close_evidence=row.get("close_evidence"), seq=expected)
        for key in _CLOSE_STATE_FIELDS["discharged"]:
            out[key] = row.get(key)
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
                    and os.path.realpath(repo_id) == repo_id \
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
                                       verdicts=verdicts) is None:
            out = dict(state)
            out.update(close_reason=row["close_reason"],
                       close_ts=row.get("ts"),
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
                       gate=recorded if _GATE_ID.fullmatch(recorded) else "",
                       polarity=_replay_polarity(row.get("polarity")))
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
            # ABSENT stays ABSENT. Setting a default here would erase the
            # difference between "written by a writer with no gate" and
            # "written by one whose stamp we could not read".
            if "gate_caps" in row:
                out["gate_caps"] = clean_gate_caps(row["gate_caps"])
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
    if event == "hold" and state["status"] == "open" and strict:
        reason, err = _clean(row.get("reason"), "hold reason", 256)
        if err or not reason:
            return state
        if not _valid_ts(row.get("ts")):
            return state
        out = dict(state)
        out.update(status="held", hold_reason=reason, hold_ts=row["ts"],
                   seq=expected)
        return out
    if event == "release" and state["status"] == "held" and strict:
        out = dict(state)
        out.update(status="open", release_reason=row.get("reason"),
                   release_ts=row.get("ts"), seq=expected)
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
    # theater (codex FIX on this verb's first cut, both replay P1s). The SUCCESSOR
    # FRONTIER: the writer refuses to retip a row whose OPEN successor already
    # carries the obligation, so a hand-appended event that moves such a
    # parent must be equally inert — replay reads the frontier off the same
    # one coherent projection the fold is building (`current`), through the
    # frontier's ONE owner (`_successor_frontier`), and every UNREADABLE
    # shape refuses: no projection at all AND a not-closed row whose
    # supersedes replays CHAIN_UNKNOWN — a frontier the check could not read
    # never reads as clear (codex P1 round 2: per-caller `== id` equality let
    # UNKNOWN fall through as "no open successor" here and at the writer
    # alike). The IDENTITY STAMP: `identity` is a strict enum, never free
    # text and never omitted — an event that cannot say whether the work
    # identity was verified has not earned application — and it is copied
    # INTO the hop, because an attribution that lives only in the writer's
    # return value is transient and every fresh snapshot erases it. THE
    # LEDGER IS THE ONLY WITNESS THE FOLD HAS (codex round 3, superseding
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
        hops.append({"old_tip": state.get("tip"), "old_ref": state.get("ref"),
                     "tip": tip, "ts": row["ts"], "reason": reason,
                     "identity": identity})
        out.update(tip=tip, ref=ref if ref and not ref_err else tip,
                   retips=hops, seq=expected)
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

    @codex's HIGH, and the FOURTH time this boundary's identity has been wrong:
    a wall clock, a version int, an append position, and then the dispatch
    work-item id. Every one of them was a PROXY that something else can come to
    satisfy, and he reproduced the last one end to end — `send()` derives its id
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
    events, unavailable = eventledger.checked_events(ledger_path())
    if unavailable:
        return {}, {}, unavailable
    out, verdicts, _taken = _fold(events, track_verdicts)
    return out, verdicts, None


def _fold(events, track_verdicts=False):
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
    saw took the REFUSED one's timestamp. @codex-3's repro put a 2001 stamp on
    the refused row and the card printed a verdict from 2001 with a 25-year
    dwell, `ledger_refused: []`, `unavailable: null`.

    So the fold reports its own decisions rather than letting a second pass
    infer them by kind and order. `_apply` already says which event it took, in
    the only way it can be trusted to: EVERY refusal path returns the state
    OBJECT IT WAS GIVEN and every acceptance returns a NEW dict (pinned by
    tests.test_dispatches.ApplySignalsWhatItTook, so a future branch that
    mutates in place is a red test rather than a silent mis-attribution)."""
    out, verdicts, taken = {}, {}, {}
    for index, row in enumerate(events):
        # One malformed row must never blind the whole ledger: a crash here
        # would turn every obligation into "no usable obligations" — worse
        # than skipping the bad row and keeping every good state intact.
        try:
            rid = str(row.get("id") or "")
            if rid not in out:
                state = _new_state(row)
                if state:
                    out[rid] = state
                    # The event that OPENS the row is an accepted transition —
                    # it is where the opening stamp comes from.
                    taken[rid] = [row]
                continue
            before = out[rid]
            after = _apply(before, row, current=out, verdicts=verdicts)
            out[rid] = after
            if after is not before:
                taken.setdefault(rid, []).append(row)
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
        except Exception:
            continue
    return out, verdicts, taken


def snapshot():
    out, _verdicts, unavailable = _snapshot()
    return out, unavailable


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

    @codex's MED. The first version validated `index` AND NOTHING ELSE, so
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
        with open(epoch_path(), encoding="utf-8") as fh:
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
    refers to — @codex's HIGH, on disk. It is not re-derived: re-scanning the
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

    WHY THIS IS A FILE AND NOT A RECOMPUTE. @codex: recomputing the epoch from
    the current ledger on every read is RETROACTIVE POLICY, and he found the
    concrete vector rather than arguing it — dispatch replay SILENTLY SKIPS
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
    """The FROZEN cutover, EPOCH_LOST, or None. NEVER RAISES.

    -> an int      the boundary. Before it a missing stamp is legacy; at or
                   after it a missing stamp is UNKNOWN and never READY.
    -> EPOCH_LOST  the boundary cannot be located or cannot be trusted: no
                   marker while stamped verdicts exist, a marker that is
                   present but malformed, or a frozen founding event that is no
                   longer on the ledger. Nothing reaches READY until it is
                   repaired. Loud and total, by @codex's call: the alternative
                   is a boundary that silently slides later and retroactively
                   authorizes everything it passed over.
    -> None        no stamped verdict has ever been written. Every row is
                   honestly legacy and there is nothing to freeze.

    THE MARKER IS READ, NEVER RECOMPUTED. Recomputing per projection is
    retroactive policy, and @codex found the vector: replay SILENTLY SKIPS
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
        # @codex's HIGH lives on the second line. The founder's dispatch id
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
    the other. @codex's repro: the snapshot says the row is OPEN, the grouped
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
    "the dispatch ledger READ cleanly" with the nav badge at 0. @codex-3
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
    events, unavailable = eventledger.checked_events(ledger_path(), strict=True)
    if unavailable:
        return {}, {}, {}, {}, unavailable
    current, verdicts, taken = _fold(events, track_verdicts=True)
    return current, _group(events), taken, verdicts, None


def _resolve_row(current, rid, noun="dispatch", list_hint="helm dispatch list"):
    """Resolve one canonical row from an unambiguous printable ID prefix.

    List surfaces print 12 characters, so every sibling mutation verb accepts
    that identifier. An exact historical short ID does NOT outrank a longer ID
    it prefixes: both are matches, and mutating either would be a guess.
    """
    rid = str(rid or "").strip()
    if not _ID.fullmatch(rid):
        return None, "no such %s: %s (%s)" % (noun, rid, list_hint)
    hits = [row for key, row in current.items() if key.startswith(rid)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "ambiguous %s id prefix: %s (use more characters)" % (noun, rid)
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


def _resolve_chain(new_work, supersedes):
    """(parent_id | None, chain_root | None, err) for one NEW row.

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
    current, unavailable = snapshot()
    if unavailable:
        return None, None, ("dispatch ledger unavailable (%s) — the superseded "
                            "chain is UNKNOWN and the row is NOT recorded; an "
                            "unreadable parent is never silently new work"
                            % unavailable)
    parent, err = _resolve_row(current, supersedes)
    if err:
        return None, None, "--supersedes " + err
    root = parent.get("chain_root")     # already replayed; see `_replay_chain`
    if root == CHAIN_UNKNOWN:
        return None, None, ("--supersedes %s: that row's chain_root is "
                            "malformed, so the chain it belongs to is UNKNOWN "
                            "— refusing rather than rooting new work at a "
                            "corrupt relation" % parent["id"][:12])
    # A CANCELLED or still-OPEN parent is a legitimate parent, and a legitimate
    # FORK (two rows naming one parent) is legitimate too — see the lifecycle
    # table in docs/VERBS.md. Refusing any of them would push the caller to
    # `--new-work`, i.e. to a lie, which is strictly worse than the honest link.
    # `rebind` proves it: it CANCELS the old row and then opens the replacement,
    # so its parent is always cancelled and its chain is always real.
    return parent["id"], root or parent["id"], None


def _base(recipient, lane, ref, note, deadline_s, repo, sender=None,
          operation_key=None, message_hash=None, rid=None, kind=None,
          new_work=False, supersedes=None,
          _ref_branch=_INFER_REF_BRANCH):
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
    # VALIDATED AT THE OWNER LAYER, not only at the CLI. `_base` is the single
    # writer of a dispatch row, and it accepted `kind="buld"` verbatim: the
    # typo reached the ledger, counted as neither build nor review, and showed
    # up in the report as UNKNOWN — indistinguishable from an honestly
    # unrecorded historical row. A guard that lives only in argument parsing
    # protects the CLI, not the DATA.
    kind, err = clean_kind(kind)
    if err:
        return None, err
    # BEFORE the git work, because this is the cheapest refusal and the loudest
    # one: a caller that named no work identity is asking for an unlinked row,
    # and the answer does not depend on the repository.
    parent, chain_root, err = _resolve_chain(new_work, supersedes)
    if err:
        return None, err
    info = _repo_info(repo)
    if not info:
        return None, "ref needs a Git working tree (--repo PATH)"
    display, err = _clean(ref, "ref", 256)
    if err:
        return None, "ref is required so the verdict can bind an exact tip"
    tip, inferred_branch = _resolve_tip(
        info["repo"], display, infer_sha_branch=_ref_branch is _INFER_REF_BRANCH)
    if not tip:
        return None, "ref is missing, ambiguous, or not a commit in this repository"
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
            "repo_id": info["repo_id"], "operation_key": operation_key,
            "message_hash": message_hash, "status": "open",
            "supersedes": parent, "chain_root": chain_root}, None


# Fields `_base` has gained over time. A row is not malformed for lacking one
# it PREDATES — that is ordinary schema growth — so the floor is derived from
# the ledger itself rather than hardcoded.
_V3_TRACKED_KEYS = ("chain_root", "supersedes", "ref_branch", "kind")


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


def _duplicate_mint_warning(row, current):
    """(warning, needs_force) from the SAME snapshot the writer appends under.

    A shared supersedes parent is strong work identity: another OPEN child means
    this mint duplicates a live continuation unless the caller explicitly forces
    a legitimate fork. A shared lane label on the same repo is the #1 born-wrong
    pattern — lanes are names, not identity, but the cost of an undetected
    duplicate is higher than the cost of a deliberate --force. Both arms now
    REFUSE; the writer must declare their intent at write time.
    """
    parent = row.get("supersedes")
    if parent:
        hits = sorted(r["id"] for r in current.values()
                      if r.get("id") != row.get("id") and _not_closed(r)
                      and r.get("supersedes") == parent)
        if hits:
            return ("OPEN successor%s %s already supersede%s %s — this would "
                    "mint duplicate live work; inspect the existing row%s and "
                    "pass --force only for a deliberate fork"
                    % ("s" if len(hits) != 1 else "",
                       ", ".join(r[:12] for r in hits[:3])
                       + (", …" if len(hits) > 3 else ""),
                       "" if len(hits) != 1 else "s", str(parent)[:12],
                       "s" if len(hits) != 1 else "")), True
        return None, False
    hits = sorted(r["id"] for r in current.values()
                  if r.get("id") != row.get("id") and _not_closed(r)
                  and r.get("repo_id") == row.get("repo_id")
                  and _strip_lane_prefix(r.get("lane")) == _strip_lane_prefix(row.get("lane")))
    if hits:
        return ("OPEN row%s %s already use%s lane label %r — same-repo same-lane "
                "duplicates are the #1 born-wrong pattern; use --supersedes for "
                "a continuation, or --force for a deliberate fork"
                % ("s" if len(hits) != 1 else "",
                   ", ".join(r[:12] for r in hits[:3])
                   + (", …" if len(hits) > 3 else ""),
                   "" if len(hits) != 1 else "s", row.get("lane"))), True
    return None, False


def _append_dispatch(row, force=False, alt_ops=()):
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
    if row.get("chain_root") is None:
        row = dict(row)
        row["chain_root"] = row["id"]        # a root names itself
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — dispatch NOT recorded" % path, False
        current, unavailable = snapshot()
        if unavailable:
            return None, ("dispatch ledger unavailable: %s — duplicate-mint "
                          "check UNKNOWN and dispatch NOT recorded"
                          % unavailable), False
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
            semantic = ("recipient", "tip", "note", "deadline_s",
                        "sender", "repo_id", "message_hash",
                        "kind", "supersedes")
            # LANE compares CANONICALIZED (#142 round 2, codex finding 3): 128
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
                return existing, None, True
            return None, "operation key already names different work", True
        warning, needs_force = _duplicate_mint_warning(row, current)
        if warning and needs_force and not force:
            return None, warning, False
        if not eventledger.append_unlocked(path, row):
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
                eventledger.append_unlocked(path, {
                    "v": 3, "event": "superseded",
                    "seq": (parent.get("seq") or 0) + 1,
                    "id": parent["id"], "ts": pk.now_ts(),
                    "successor": row["id"]})
    pk.event("dispatch-add", row["id"], "%s -> %s" % (row["recipient"], row["lane"]))
    out = dict(row)
    out.update(delivery="needs-confirmation", migration=None,
               delivery_ref=None, verdict_ref=None, reviewed_tip=None)
    if warning:
        out[_WRITE_WARNINGS] = [warning]
    return out, None, False


def _acting_author():
    """(sender, err) — the ONE author stamp for add()/send(), and THE FLOOR IS
    NEVER AN AUTHOR (owner-declared P0, 2026-08-02). Both paths used
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
    absent refuses with the repair, never a silent floor."""
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
            "refusing to author this dispatch under a DISPUTED identity: this "
            "process declares %r but session %.8s is rostered to %r. An "
            "inherited HELM_CHAT_NAME is free; the roster can be corrupt; "
            "only agreement is clean. Fix: unset/re-export HELM_CHAT_NAME, or "
            "`helm chat seat disown %s %.8s`"
            % (own, str(sid), bound, bound, str(sid)))
    sender = own or bound
    if sender:
        return sender, None
    return None, (
        "refusing to author this dispatch as the family floor: no declared "
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



def add(recipient, lane, ref=None, note=None, deadline_s=None,
        repo=None, kind=None, notify=True, _reason=False,
        new_work=False, supersedes=None, force=False,
        _ref_branch=_INFER_REF_BRANCH):
    """Persist a dispatch and post a public @mention to main so the
    reviewer's beacon picks up the obligation — the counterpart to send()
    (which also DMs the reviewer). When notify=False, the dispatch is
    persisted without any mention; the caller owns the hand-off. A failed
    mention does not block the dispatch.

    RECORDS THE SENDER, like send() always has. This path did not, so every
    `dispatch add` row landed with sender=None and the ledger could not say who
    owed it. The stop-guard then billed the delivery-confirmation nag to
    WHOEVER STOPPED NEXT: measured 2026-07-28, two rows minted by helm-claude-2
    (63cf625c, 9e237a33) nagged opus-integrator ten minutes apart to confirm
    hand-offs it never made. That is worse than billing nobody — it makes an
    uninvolved seat feel responsible and, in a busy fleet, invites the duplicate
    resend the same guard exists to prevent.

    A row that cannot name its sender cannot be chased, cancelled, or credited.

    Same add-vs-send asymmetry as the notification gap ("dispatch:
    `add` mints an obligation and tells NOBODY — say so out loud"): send() was
    complete and add() was the quiet path nobody re-derived.

    SUPERSEDED (owner call, 2026-08-02): this path used to fail OPEN — a
    hostile HELM_CHAT_NAME or no identity at all still wrote the row, as
    sender=None or the family floor, on the theory that "an unattributed row
    is a real defect, but losing the obligation entirely is worse". Measured
    consequence: 6 stalled rows owned by 'claude' — a name three seats share —
    permanently stop-guard-nagging every claude seat, un-disownable.
    Un-disownable shared-name rows are worse than a lost add: identityless or
    disputed callers are REFUSED with the repair (_acting_author)."""
    sender, err = _acting_author()
    if err:
        return (None, err) if _reason else None
    recipient, err = _recipient_operand(recipient)
    if err:
        return (None, err) if _reason else None
    ok, why = _validate_recipient_rostered(recipient, force)
    if not ok:
        return (None, why) if _reason else None
    row, err = _base(recipient, lane, ref, note, deadline_s, repo, kind=kind,
                     sender=sender, new_work=new_work, supersedes=supersedes,
                     _ref_branch=_ref_branch)
    if not row:
        return (None, err) if _reason else None
    out, why, _ = _append_dispatch(row, force=force)
    if not out:
        return (None, why) if _reason else None
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
    with eventledger.locked(path) as held:
        if not held:
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
        if not eventledger.append_unlocked(path, event):
            return None, "delivery observed but ledger update failed"
    out = dict(row)
    out.update(delivery="observed", delivery_ref=ref, seq=event["seq"])
    if warning:
        out[_WRITE_WARNINGS] = list(out.get(_WRITE_WARNINGS, ())) + [warning]
    return out, None


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
    and the same nag misbilled @opus-integrator twice on 2026-07-28.

    Truthiness is unchanged (a non-empty id where True stood, None where False
    did), so every caller that only asked "did it post" keeps working."""
    from . import chat
    notice = "@%s %s: %s" % (_recipient_label(row), row["id"][:12], context)
    mention_id = None
    why = ""
    try:
        posted = chat.post(notice, room="main", who=row.get("sender"),
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
          never the stale pre-DM OPEN row; codex-3 xrev 4th defect)
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


def _with_write_warnings(result, warnings):
    """Keep write-time advisories visible across ledger reconciliation.

    They remain ephemeral — never replayed as dispatch state — but UNKNOWN must
    not erase an advisory the locked writer already proved. When reconciliation
    cannot return a row, carry the warning on the only remaining user-visible
    channel: the reason string.
    """
    row, why, sent = result
    if warnings:
        if row is not None:
            row = dict(row)
            row[_WRITE_WARNINGS] = list(warnings)
        elif why:
            why += "; write warning: " + "; ".join(warnings)
    return row, why, sent


def send(recipient, lane, message, ref, note=None, deadline_s=None,
         key=None, repo=None, sign=None, kind=None, new_work=False,
         supersedes=None, force=False):
    """Persist first, attempt one DM, never auto-retry an existing operation.

    Returns (row, reason, sent_now). An existing OPEN operation returns NEEDS
    CONFIRMATION (never re-DMs); an existing CLOSED one returns its true
    terminal state. After the DM the return reflects CANONICAL durable state
    via _reconcile_send — UNKNOWN (row=None) if the ledger cannot be read,
    never a stale pre-DM row.
    """
    message = str(message or "").strip()
    if not message or len(message) > 16000 or "\x00" in message:
        return None, "message must be 1-16000 characters without NUL", False
    key = str(key or "").strip()
    if key:
        key, err = _clean(key, "operation key", 256)
        if err:
            return None, err, False
    from . import seats
    # Same refusal SHAPE as always (rc-bearing (None, why, False)); the
    # message now instructs — _acting_author refuses the family floor and a
    # declared-vs-rostered dispute instead of stamping either (2026-08-02).
    sender, err = _acting_author()
    if err:
        return None, err, False
    recipient, err = _recipient_operand(recipient)
    if err:
        return None, err, False
    ok, why = _validate_recipient_rostered(recipient, force)
    if not ok:
        return None, why, False
    probe, err = _base(recipient, lane, ref, note, deadline_s, repo, kind=kind,
                       sender=sender, new_work=new_work, supersedes=supersedes,
                       message_hash=hashlib.blake2b(
                           message.encode("utf-8"), digest_size=16).hexdigest())
    if err:
        return None, err, False
    def _op_id(k):
        return hashlib.blake2b(
            ("dispatch\0" + "\0".join((sender, probe["repo_id"], k)))
            .encode("utf-8"), digest_size=16).hexdigest()

    alt_ops = []
    if not key:
        # THE PARENT IS PART OF THE OPERATION. Two sends identical in every
        # visible field but continuing DIFFERENT work are different operations;
        # leaving the chain out of the auto key would collapse them onto one id
        # and refuse the second as "already names different work" — a real
        # dispatch lost to a hash collision the caller cannot see. Two
        # `--new-work` sends that match everywhere still collide, which is the
        # duplicate-send guard doing its job.
        def _auto_key(lane_spelling):
            return "auto:" + hashlib.blake2b(
                "\0".join((probe["recipient"], lane_spelling, probe["tip"],
                            probe.get("note") or "", str(probe["deadline_s"]),
                            probe["message_hash"],
                            probe.get("supersedes") or "new-work")).encode("utf-8"),
                digest_size=16).hexdigest()

        key = _auto_key(probe["lane"])
        # HISTORICAL AUTO-KEY CANDIDATES (#142 rounds 3+4, codex): the old
        # writer hashed the lane AS GIVEN. Re-derive the id under the raw
        # input spelling (the same command replayed) and under the
        # lane/-prefixed spelling of the canonical lane (a modernized retry
        # of the live 115-row cohort); `_append_dispatch` reconciles a hit.
        raw_lane = str(lane or "").strip()
        for spelling in (raw_lane, "lane/" + probe["lane"]):
            if spelling and spelling != probe["lane"]:
                ak = _auto_key(spelling)
                pair = (ak, _op_id(ak))
                if pair not in alt_ops:
                    alt_ops.append(pair)
        # THE OLDEST WRITER HASHED NO PARENT FIELD AT ALL (#142 round 4,
        # codex census through round 3's tip: live row 1ddf37fc — the pre-chain
        # schema over recipient/lane/tip/note/deadline/message_hash, stored
        # with no chain_root). Round 3's candidates all carry the parent
        # field, so that row's key/id are underivable under them and the
        # same command replayed would mint again. NEW-WORK SENDS ONLY: the
        # pre-parent schema could not express a parent, so a
        # supersedes-bearing send is NEVER the same operation as a
        # pre-parent row — probing those ids from a parented send could
        # only misbind different work.
        if probe.get("supersedes") is None:
            def _auto_key_pre_parent(lane_spelling):
                return "auto:" + hashlib.blake2b(
                    "\0".join((probe["recipient"], lane_spelling,
                                probe["tip"], probe.get("note") or "",
                                str(probe["deadline_s"]),
                                probe["message_hash"])).encode("utf-8"),
                    digest_size=16).hexdigest()

            for spelling in (probe["lane"], raw_lane,
                             "lane/" + probe["lane"]):
                if spelling:
                    ak = _auto_key_pre_parent(spelling)
                    pair = (ak, _op_id(ak))
                    if pair not in alt_ops:
                        alt_ops.append(pair)
    probe["id"] = _op_id(key)
    probe["operation_key"] = key
    row, why, existed = _append_dispatch(probe, force=force,
                                         alt_ops=tuple(alt_ops))
    if why:
        return None, why, False
    warnings = row.get(_WRITE_WARNINGS, ())
    if existed:
        if not _open(row):
            # the prior obligation is already TERMINAL (verdict or cancel) —
            # report that true state, never confirmation debt on a closed row
            return row, None, False
        # One operation = at most one send, ever. A prior attempt whose
        # delivery evidence is missing is AMBIGUOUS, not absent — resending
        # here is exactly the duplicate-message hazard the reduced core
        # refuses to automate away.
        return row, ("dispatch already recorded; delivery is %s — confirm at the "
                     "recipient, do not resend automatically" % row["delivery"]), False
    try:
        # THE NOTIFICATION LEG (2026-07-26 fleet-stall root cause). DM goes
        # to the private lane AND a public @mention lands in main so the beacon
        # picks it up. A failed mention leaves a DURABLE notify-failed marker
        # so the gap is never silently recreated.
        _notify_public(row, note or lane)
        delivered, dm_err = seats.dm(
            recipient, message, who=sender, profile=sender, sign=sign,
            session=home.session_id())
    except Exception as exc:
        delivered, dm_err = None, "%s: %s" % (type(exc).__name__, exc)
    if dm_err or not delivered:
        # the DM failed/raised — reconcile against the durable ledger (a cancel
        # may have landed during it; storage may be unavailable -> UNKNOWN)
        return _with_write_warnings(
            _reconcile_send(row["id"], dm_err or "DM returned no row"),
            warnings)
    observed, err = _mark_delivered(row["id"], delivered.get("id"))
    if err:
        # the delivered-write failed (lock/storage) — same reconciliation: never
        # carry the stale pre-DM row, surface UNKNOWN if the ledger is unreadable
        return _with_write_warnings(_reconcile_send(row["id"], err), warnings)
    # The lock is released for the DM, so a concurrent cancel/verdict may have
    # terminalized the row mid-flight — report the TRUE state, never a false
    # "delivery observed" on a dispatch that is already closed.
    return _with_write_warnings(
        (observed, None, _open(observed)), warnings)


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
    scrubbed environment (ambient GIT_DIR must not redirect them) and under
    the ledger lock — which brackets the ledger, not the repository, so
    movement between read and append remains possible; the class this stops
    has hours of skew, not milliseconds.
    """
    repo, branch = row.get("repo_id"), row.get("ref_branch")
    if not isinstance(repo, str) or not os.path.isabs(repo) \
            or not os.path.isdir(repo) or not isinstance(branch, str) \
            or not branch.startswith("refs/heads/"):
        return None
    env = _git_env()
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


def mark_verdict(rid, reviewed_tip, evidence, polarity=None, basis=None):
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
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row["status"] == "verdict":
            if row.get("reviewed_tip") == reviewed \
                    and row.get("verdict_ref") == evidence \
                    and row.get("polarity") == polarity \
                    and row.get("basis") == basis:
                # Idempotent retry RECONCILES the standing attestation (the
                # _reconcile_send law): report what IS, never re-emit.
                out = dict(row)
                out["announce"] = _reconcile_announce(row)
                return out, None
            # A DIFFERENT polarity or basis on the same tip+evidence is not a
            # retry, it is an attempt to flip a standing verdict's semantics.
            # Terminal is immutable: it falls through to the refusal below.
            return None, "dispatch %s already has a verdict (closed)" % rid
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
        # Land-authorizing writes only: a FIX on the old tip is truthful when
        # the author advanced while fixing, and a SUPERSEDE is directly
        # caused by movement (codex-3 review).
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
        gate_state, gate_id, gate_why = gate.bind(
            evidence, reviewed, repo_id=row.get("repo_id"),
            reviewed_ts=row.get("ts"))
        if gate_state == "REFUSED":
            return None, "gate evidence does not bind: " + gate_why
        if polarity == "approve" and GATE_CAP_RECEIPT in GATE_CAPS \
                and gate_state != "VERIFIED":
            return None, ("approve verdict requires a verified gate:<token>: an "
                          "ungated approve is immutable and can never authorize "
                          "landing; run `helm gate run` at the exact reviewed tip "
                          "and include its token")
        event = {"v": 3, "event": "verdict", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed, "verdict_ref": evidence,
                 "polarity": polarity,
                 "gate": gate_id or "", "gate_caps": list(GATE_CAPS)}
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
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
    out = dict(row)
    # `gate` is the ONLY field recorded, and everything a reader wants is
    # derived from it (gate_state). The first draft also returned gate_state
    # and gate_why on this path — and the idempotent-retry path, which
    # early-returns the REPLAYED row, could not carry them, so a retry answered
    # in a different shape than the original call. tests/test_dispatches caught
    # it. A second stored spelling of one fact is the same defect as the
    # duplicated STALE_ON_REMINT list: two places to update, one of them
    # forgotten.
    out.update(status="verdict", reviewed_tip=reviewed,
               verdict_ref=evidence, polarity=polarity, seq=event["seq"],
               gate=gate_id or "", gate_caps=tuple(GATE_CAPS))
    # SAME OPTIONAL KEY ON BOTH PATHS: the first CLI response once said UNMARKED
    # while the event and replay said MEASURED. Absence still means no key.
    if basis:
        out["basis"] = basis
    # FREEZE THE CUTOVER on the first stamped write, so it is a recorded fact
    # rather than a per-read derivation. No-op once a marker exists.
    if GATE_CAPS:
        try:
            record_gate_epoch()
        except Exception:               # noqa: BLE001 — never fail a verdict
            pass
    pk.event("dispatch-verdict", row["id"], evidence)
    out["announce"] = _announce_verdict(out, reviewed, evidence)
    return out, None


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
    """
    from . import seats
    try:
        lander = (os.environ.get("HELM_LANDER")
                  or "opus-integrator")
        cmd = "helm lr land %s" % row["id"][:17]
        if lander:
            seats.dm(lander,
                     "VERDICT APPROVE at %s — ready: %s"
                     % (row["reviewed_tip"][:10], cmd),
                     who="dispatches")
        else:
            from . import chat
            chat.post("@%s %s: VERDICT APPROVE — %s"
                      % (lander, row["lane"], cmd),
                      who="dispatches")
    except Exception:                      # a land nudge must never block a verdict
        pass


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
            # (#142 round 2, codex finding 1). Family joins normalize at read.
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
    (env changes never redirect the search: codex-3 finding C). Unreadable
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
    row carrying the field (codex-3 xrev finding #2). Exact verdict shape,
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


def _record_discharge_proven(rid, reviewed_tip, superseding_tip,
                             superseding_id, evidence, contrary_state,
                             contrary_target):
    """Append a Git-proven contrary discharge without rewriting its verdict.

    landreq owns the live Git + approved-successor proof. This ledger boundary
    revalidates immutable row identity under lock, then records exactly one
    post-verdict annotation. Identical retries are idempotent; conflicts cannot
    rewrite which round discharged the debt.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    superseding = str(superseding_tip or "").strip().lower()
    superseding_id = str(superseding_id or "").strip()
    if not _FULL_TIP.fullmatch(reviewed):
        return None, "discharge needs the original full reviewed commit id"
    if not _FULL_TIP.fullmatch(superseding):
        return None, "discharge needs a full 40- or 64-character superseding tip"
    if superseding == reviewed:
        return None, "a verdict tip cannot discharge itself"
    if not _ID.fullmatch(superseding_id):
        return None, "discharge needs the approved superseding dispatch id"
    evidence, err = _clean(evidence, "discharge evidence", 256)
    if err:
        return None, err
    if contrary_state not in ("landed", "merged-local"):
        return None, "discharge needs the observed contrary land state"
    if contrary_target not in ("local", "upstream"):
        return None, "discharge needs the observed contrary trunk target"
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — discharge NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("discharged"):
            same = row.get("reviewed_tip") == reviewed \
                and row.get("superseding_tip") == superseding \
                and row.get("superseding_id") == superseding_id \
                and row.get("discharge_ref") == evidence \
                and row.get("discharge_contrary") == contrary_state \
                and row.get("discharge_target") == contrary_target
            if same:
                return row, None
            return None, "dispatch %s already has a different discharge" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("withdrawn"):
            return None, ("dispatch %s is already withdrawn — a debt is retired "
                          "once, by land (discharge) or by abandonment "
                          "(withdraw), never both" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a row is retired once — refusing a different closure"
                          % (row["id"], row["close_reason"]))
        if row.get("status") != "verdict" \
                or row.get("polarity") not in ("fix", "supersede"):
            return None, "dispatch %s is not a FIX/SUPERSEDE verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "discharge reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "discharge", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "superseding_tip": superseding,
                 "superseding_id": superseding_id,
                 "discharge_ref": evidence,
                 "contrary_state": contrary_state,
                 "contrary_target": contrary_target}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — discharge NOT recorded" % path
    out = _apply(row, event)
    pk.event("dispatch-discharge", row["id"], superseding)
    return out, None


def _record_withdraw_proven(rid, reviewed_tip, evidence):
    """Append a Git-proven WITHDRAWAL of a do-not-land verdict — the mirror of
    _record_discharge_proven, for the row whose CORRECT resolution is "this is
    never landed at all", so no superseding tip will ever exist to discharge it.

    landreq owns the live Git proof (the reviewed change is provably ABSENT from
    trunk). This ledger boundary revalidates immutable row identity under lock,
    then records exactly one post-verdict annotation. Identical retries are
    idempotent; a conflicting withdraw is refused, never a rewrite. The verdict
    and the withdraw both stay visible — history is preserved, not rewritten."""
    reviewed = str(reviewed_tip or "").strip().lower()
    if not _FULL_TIP.fullmatch(reviewed):
        return None, "withdraw needs the original full reviewed commit id"
    evidence, err = _clean(evidence, "withdraw evidence", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — withdraw NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("withdrawn"):
            same = row.get("reviewed_tip") == reviewed \
                and row.get("withdraw_ref") == evidence
            if same:
                return row, None
            return None, "dispatch %s already has a different withdraw" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("discharged"):
            return None, ("dispatch %s is already discharged — a row is retired "
                          "once, by land (discharge) or by abandonment (withdraw), "
                          "never both" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a row is retired once — refusing a different closure"
                          % (row["id"], row["close_reason"]))
        if row.get("status") != "verdict" \
                or row.get("polarity") not in ("fix", "supersede"):
            return None, "dispatch %s is not a FIX/SUPERSEDE verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "withdraw reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "withdraw", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "withdraw_ref": evidence}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — withdraw NOT recorded" % path
    out = _apply(row, event)
    pk.event("dispatch-withdraw", row["id"], reviewed)
    return out, None


def _record_abandon_proven(rid, reviewed_tip, reason, object_exists_probe,
                             interlock_probe):
    """Append an honest terminal when the reviewed commit is MISSING.

    landreq owns the public lifecycle decision and Git interpretation. This
    boundary re-resolves the immutable row under the ledger lock and invokes
    one bounded exact-object probe immediately before append. Only explicit
    MISSING (False) authorizes the event; EXISTS and UNKNOWN both refuse.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    if not _FULL_TIP.fullmatch(reviewed):
        return None, "abandon needs the original full reviewed commit id"
    reason, err = _clean(reason, "abandon reason", 256)
    if err:
        return None, err
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — abandon NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("abandoned"):
            if row.get("reviewed_tip") == reviewed \
                    and row.get("abandon_reason") == reason:
                return row, None
            return None, "dispatch %s already has a different abandon" % row["id"]
        if row.get("closed_by_landing") or row.get("discharged") \
                or row.get("withdrawn") or row.get("close_reason"):
            terminal = "landing closure" if row.get("closed_by_landing") else \
                "discharge" if row.get("discharged") else "withdraw" \
                if row.get("withdrawn") else "close --reason %s" % \
                row["close_reason"]
            return None, "dispatch %s is already retired by %s" % (row["id"], terminal)
        if row.get("kind") != "review":
            return None, ("dispatch %s is not a review row — abandon can never "
                          "close a build dispatch" % row["id"])
        if row.get("status") != "verdict":
            return None, "dispatch %s is not a verdict" % row["id"]
        if row.get("polarity") == "supersede":
            return None, "dispatch %s is already terminal through SUPERSEDE" % row["id"]
        # THE WRITER ADMITS EXACTLY WHAT THE REPLAY ADMITS, AND THAT AGREEMENT
        # IS THE POINT. `_apply`'s abandon arm requires a WORK polarity; this
        # door blocked only `supersede`, so `concur` and historical UNDECLARED
        # passed HERE and were refused THERE. The divergence is worse than
        # either half alone: the ledger grows an abandon event the state
        # machine ignores, and the CLI prints ABANDONED over a row that stayed
        # REVIEWED — an operator told a write happened that did not.
        # (Reproduced at the exact lane tip before the cure: err=None, state
        # REVIEWED, history 2->3.) A blocklist must be widened for every
        # polarity ever added and is silently wrong until someone remembers;
        # this allowlist
        # refuses the next polarity by construction, which is the same shape
        # `_CLOSE_POLARITY` already uses for every close door.
        if row.get("polarity") not in _WORK_POLARITIES:
            return None, ("dispatch %s carries polarity %s, which speaks about "
                          "no landable work — abandon writes off REVIEWED WORK"
                          % (row["id"], row.get("polarity") or "UNDECLARED"))
        if row.get("reviewed_tip") != reviewed:
            return None, "abandon reviewed tip does not match dispatch %s" % row["id"]
        repo_id = str(row.get("repo_id") or "")
        if not os.path.isabs(repo_id) or os.path.realpath(repo_id) != repo_id:
            return None, "dispatch %s has no canonical repository binding" % row["id"]
        try:
            exists = object_exists_probe(repo_id, reviewed) \
                if callable(object_exists_probe) else None
        except Exception:                       # noqa: BLE001 — proof UNKNOWN
            exists = None
        if exists is True:
            return None, "reviewed commit exists at the mutation boundary"
        if exists is not False:
            return None, "reviewed commit state is UNKNOWN at the mutation boundary"
        try:
            interlocks = interlock_probe(repo_id, row.get("lane")) \
                if callable(interlock_probe) else None
        except Exception:                       # noqa: BLE001 — proof UNKNOWN
            interlocks = None
        if not isinstance(interlocks, dict):
            return None, "abandon interlock state is UNKNOWN at the mutation boundary"
        mention = interlocks.get("mention")
        if not isinstance(mention, dict) or mention.get("state") == "unknown":
            return None, "trunk mention state is UNKNOWN at the mutation boundary"
        if mention.get("state") in ("structured", "ambiguous", "tag"):
            evidence = mention.get("line") or mention.get("tag") \
                or mention.get("state")
            return None, ("trunk mention blocks abandon at the mutation boundary: "
                          "%s" % evidence)
        if mention.get("state") != "none":
            return None, "trunk mention state is UNKNOWN at the mutation boundary"
        if interlocks.get("branch_state") == "unlanded":
            return None, "unlanded lane branch blocks abandon at the mutation boundary"
        if interlocks.get("worktree_state") == "dirty":
            return None, "dirty worktree blocks abandon at the mutation boundary"
        if interlocks.get("branch_state") not in ("none", "merged") \
                or interlocks.get("worktree_state") not in ("none", "clean"):
            return None, "lane work state is UNKNOWN at the mutation boundary"
        event = {"v": 3, "event": "abandon", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed, "repo_id": repo_id,
                 "reason": reason, "object_state": "missing",
                 "object_proof_mode": "cat-file-batch-check",
                 "object_proof_version": 1,
                 "trunk_mention_state": "none",
                 "trunk_mention_proof_mode":
                 "structured-message-and-tag-scan",
                 "trunk_mention_proof_version": 2,
                 "branch_state": interlocks["branch_state"],
                 "branch_proof_mode": "git-ref-and-ancestry",
                 "branch_proof_version": 1,
                 "worktree_state": interlocks["worktree_state"],
                 "worktree_proof_mode": "git-worktree-status",
                 "worktree_proof_version": 1,
                 "land_state": "UNKNOWN"}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — abandon NOT recorded" % path
    out = _apply(row, event)
    pk.event("dispatch-abandon", row["id"], reviewed)
    return out, None


def _record_close_landed_proven(rid, reviewed_tip, repo_id, trunk_ref,
                                trunk_sha, proof_mode):
    """Append one Git-proven landing closure without inventing a verdict.

    landreq owns the live repository/ref/proof work. This locked boundary
    revalidates the immutable verdict identity and writes one monotonic receipt.
    """
    reviewed = str(reviewed_tip or "").strip().lower()
    if not _FULL_TIP.fullmatch(reviewed):
        return None, "close-landed needs the original full reviewed commit id"
    repo_id, err = _clean(repo_id, "landing repo id", 4096)
    if err or not os.path.isabs(repo_id) or os.path.realpath(repo_id) != repo_id:
        return None, "close-landed needs a canonical absolute Git common-dir"
    trunk_ref = _valid_trunk_ref(trunk_ref)
    if not trunk_ref:
        return None, "close-landed needs a canonical branch or remote-tracking ref"
    trunk_sha = str(trunk_sha or "").strip().lower()
    if not _FULL_TIP.fullmatch(trunk_sha):
        return None, "close-landed needs the sampled trunk commit's full id"
    if proof_mode not in ("ancestor", "patch-equivalent"):
        return None, "close-landed proof mode must be ancestor or patch-equivalent"
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — close-landed NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("closed_by_landing"):
            same = row.get("landing_repo_id") == repo_id \
                and row.get("landing_trunk_ref") == trunk_ref
            if same:
                return row, None
            return None, "dispatch %s already has a different landing closure" % row["id"]
        if row.get("abandoned"):
            return None, ("dispatch %s is already ABANDONED with land state "
                          "UNKNOWN" % row["id"])
        if row.get("discharged") or row.get("withdrawn") \
                or row.get("close_reason"):
            return None, ("dispatch %s is already retired by %s" % (
                row["id"], "discharge" if row.get("discharged")
                else "withdraw" if row.get("withdrawn")
                else "close --reason %s" % row["close_reason"]))
        if row.get("status") != "verdict" or row.get("polarity") is not None:
            return None, "dispatch %s is not an UNDECLARED verdict" % row["id"]
        if row.get("reviewed_tip") != reviewed:
            return None, "close-landed reviewed tip does not match dispatch %s" % row["id"]
        event = {"v": 3, "event": "close-landed", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "reviewed_tip": reviewed,
                 "landing_repo_id": repo_id,
                 "landing_trunk_ref": trunk_ref,
                 "landing_trunk_sha": trunk_sha,
                 "landing_proof_mode": proof_mode,
                 "landing_proof_version": 1}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — close-landed NOT recorded" % path
    out = _apply(row, event)
    pk.event("dispatch-close-landed", row["id"], trunk_sha)
    return out, None


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
CLOSE_REASONS = ("landed", "superseded", "withdrawn", "stranded",
                 "discharged",
                 "subsumed", "delivered-report", "resolved")
CLOSE_PROOF_MODES = ("ancestor", "patch-equivalent",
                     "translated-ancestor", "translated-patch-equivalent")
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
    "superseded": ("approve", "fix", "supersede", None),
    "withdrawn": ("fix", "supersede", None),
    "stranded": ("approve", "fix", "supersede", None),
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
               "landing_review_approval_anchor"),
    "superseded": ("superseding_tip", "superseding_id", "close_contrary_state",
                   "close_contrary_target", "closing_trunk_ref",
                   "closing_trunk_sha", "close_proof_version",
                   # #101 — present ONLY on a translated-object supersession;
                   # the validator below refuses them on any other shape.
                   "close_proof_mode", "translated_tip"),
    "withdrawn": ("absence_trunk_ref", "absence_trunk_sha",
                  "close_proof_mode", "translated_tip",
                  "close_proof_version"),
    "stranded": ("closing_repo_id", "control_sha", "close_proof_mode",
                 "close_proof_version"),
    "discharged": ("discharging_id", "discharging_tip", "discharge_tier"),
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
                 "close_proof_version"),
    "delivered-report": ("artifact_ref", "report_ref", "close_proof_version"),
}
_CLOSE_EVENT_BASE_FIELDS = {
    "v", "event", "seq", "id", "ts", "close_reason", "reviewed_tip",
    "close_evidence", "close_proof_version",
}
# The delivery declaration is ADDITIVE over the build-landed schema: every new
# write carries both keys (the writer sets them unconditionally), and a
# PRE-DECLARATION event that carries neither still replays. Absence is never
# read as "cli" anywhere — it reads UNDECLARED, which is the whole law here.
_DELIVERY_FIELDS = {"close_delivery_class", "close_delivery_restart"}
_BUILD_LANDED_EVENT_FIELDS = {
    "v", "event", "seq", "id", "ts", "close_reason",
    "close_proof_version", "landing_review_id", "landing_review_tip",
    "landing_review_verdict_anchor", "landing_review_tier_state",
    "landing_review_gate_requirement", "landing_review_gate",
    "landing_review_approval_anchor", "closing_repo_id",
    "closing_trunk_ref", "closing_trunk_sha", "close_proof_mode",
    "translated_tip", "close_delivery_class", "close_delivery_restart",
}
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
# cannot qualify (@opus-integrator's amendment A, refutation pass 2026-08-04;
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


def _subsumed_family_anchor(evidence):
    version = evidence.get("v") if isinstance(evidence, dict) else None
    return _proof_anchor("subsumed-family-v%d" % version, evidence) \
        if type(version) is int and version in (1, 2, 3, 4) else None


def _subsumed_approval_anchor(verdict, tier, requirement, gate_id):
    return _proof_anchor("subsumed-approval-v1", {
        "verdict_anchor": verdict, "tier_state": tier,
        "gate_requirement": requirement, "gate": gate_id})


def _landed_review_approval_anchor(verdict, tier, requirement, gate_id):
    return _proof_anchor("landed-review-approval-v1", {
        "verdict_anchor": verdict, "tier_state": tier,
        "gate_requirement": requirement, "gate": gate_id})


def _chain_reaches(candidate, target_id, current):
    """(True|False|None, why): does candidate's exact parent walk reach target?"""
    target = str(target_id or "")
    node = candidate
    seen = set()
    while isinstance(node, dict):
        rid = str(node.get("id") or "")
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


def discharging_row(row_id, current, is_landed=None):
    """(tier, discharging_row_id, why) — the row whose LAND discharges `row_id`,
    or (None, None, why) when nothing does.

    #177: A BUILD ROW HAS NO DOOR OF ITS OWN. Every reason in _CLOSE_POLARITY
    keys on the row's OWN polarity, and a build row that was never verdicted
    has none — its successor was minted first, so the parent sits AWAITING
    REVIEW forever while its work is on trunk. Measured 2026-08-04: five
    refusals across four verbs and two files (here and landreq's supersede
    rung), three rows, one predicate. One of them billed the fleet's most
    loaded reviewer 227 minutes for work that had landed three hours earlier.

    SO THE AUTHORITY IS ANOTHER ROW'S VERDICT, which is the #101 precedent in
    landreq: admit a row missing one binding ONLY when every other authority
    vouches. Both tiers demand the same three legs of that voucher — polarity
    approve, a gate that BINDS, and a reviewed tip actually on trunk.

    ONE TIER, AND IT IS A HARD EDGE: a successor whose supersedes-walk REACHES
    this row. `continues <row-id>` is recorded, never inferred.

    A SAME-LANE TIER WAS RULED IN AND THEN WITHDRAWN, and that belongs here as
    the cautionary half. It was ruled in on evidence that two rows shared a
    lane — evidence I produced by printing the labels truncated to 34
    characters, where 'jit-cooldown-measures-turns-not-compaction' and
    '...-not-context' render identically. They diverge at 35. The tier added
    for two specific rows would have discharged NEITHER of them, and would have
    admitted every future pair whose labels merely start alike.

    SO THIS REFUSES ROWS WHOSE RELATION WAS NEVER RECORDED, deliberately. A
    build row whose work landed under a row sharing neither its chain nor its
    lane cannot be discharged by derivation at all: no scan can prove a
    relation nobody wrote down, and a door that closed it anyway would be
    inventing the authority. Those need an ATTESTED close, where an accountable
    party asserts the linkage and the row records that it was ASSERTED rather
    than derived (#147) — legibly weaker on its face, which is exactly what
    keeps it from being laundering.

    Direction matters and it is the reverse of the intuitive read: the CHILD
    names the PARENT, so this SCANS for successors pointing AT row_id rather
    than reading a field on it. The walk itself is _chain_reaches — reused, not
    reimplemented, because two walks would eventually disagree about the same
    ledger.

    is_landed(tip) -> bool decides "on trunk"; injected so the caller owns the
    repo probe and this stays pure. A None answer from it is NOT landed:
    could-not-look is never looked-and-found."""
    target = str(row_id or "")
    row = (current or {}).get(target)
    if not isinstance(row, dict):
        return None, None, "no such row"
    if row.get("status") in CLOSED_STATES:
        return None, None, "row is already terminal"

    def _vouches(cand):
        """The three legs every tier demands of the discharging row."""
        if str(cand.get("id") or "") == target:
            return False
        if cand.get("status") != "verdict" or cand.get("polarity") != "approve":
            return False
        tip = cand.get("reviewed_tip")
        if not tip or not is_landed:
            return False
        return is_landed(tip) is True

    # TIER 1 — a successor whose parent-walk reaches this row.
    chain_edge = row.get("supersedes") not in (None, CHAIN_UNKNOWN)
    for cand in (current or {}).values():
        if not isinstance(cand, dict) or str(cand.get("id") or "") == target:
            continue          # a row reaches ITSELF; that is not a successor
        reaches, _why = _chain_reaches(cand, target, current)
        if reaches is True:
            chain_edge = True
            if _vouches(cand):
                return DISCHARGE_TIER_CHAIN, str(cand.get("id")), None
    if chain_edge:
        return None, None, ("a chain edge exists but no successor on it carries "
                            "a landed, gate-verified APPROVE")
    return None, None, ("nothing on the ledger records what discharged this row "
                        "— it needs an attested close, not a derived one")


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
    elif evidence["v"] == 4:
        if set(evidence) != {"v", "identity", "roster_identity", "runtime",
                            "runtime_verified"}:
            return "family evidence must be one exact v4 native runtime snapshot"
        from . import seats
        if not seats.recipient_matches(evidence.get("roster_identity"), identity):
            return "family evidence roster identity does not match the dispatch"
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


def _delivery_error(event):
    """Why this event's DELIVERY DECLARATION may not bind — None when it does.

    Tri-state on purpose, and the middle state is the point: BOTH fields
    absent is a pre-declaration event (UNDECLARED, admissible, and rendered
    as unknown rather than live); "cli" carries no restart because a fresh
    process off main needs none; "process" MUST name what still holds
    pre-land code, because an unnamed restart obligation is one nobody can
    discharge. Anything else does not bind — a malformed declaration is never
    softened into the quiet answer."""
    klass = event.get("close_delivery_class")
    restart = event.get("close_delivery_restart")
    if klass is None:
        if restart is not None:
            return "a restart target without a declared delivery class"
        return None
    if klass not in CLOSE_DELIVERY_CLASSES:
        return "close delivery class must be one of %s" \
            % "/".join(CLOSE_DELIVERY_CLASSES)
    if klass == "cli":
        if restart is not None:
            return "a CLI-class land declares no restart target"
        return None
    cleaned, err = _clean(restart, "close delivery restart", 256)
    if err or cleaned != restart:
        return err or "close delivery restart must round-trip clean"
    return None


def _build_landed_event_error(event, state, current, verdicts, verify_live):
    """Validate one OPEN BUILD close through an accepted review descendant."""
    core = set(event) - _DELIVERY_FIELDS
    if core != _BUILD_LANDED_EVENT_FIELDS - _DELIVERY_FIELDS:
        expected = _BUILD_LANDED_EVENT_FIELDS - _DELIVERY_FIELDS
        missing = sorted(expected - core)
        extra = sorted(core - expected)
        return "close event fields do not match build-landed schema (missing=%s extra=%s)" \
            % (",".join(missing) or "-", ",".join(extra) or "-")
    derr = _delivery_error(event)
    if derr:
        return derr
    if state.get("status") != "open" or state.get("kind") != "build":
        return "build-landed needs one open BUILD dispatch"
    if state.get("close_reason"):
        return "already retired"
    repo, err = _clean(event.get("closing_repo_id"), "closing repo id", 4096)
    if err or not os.path.isabs(repo) or os.path.realpath(repo) != repo \
            or repo != state.get("repo_id"):
        return "closing repo id must match the BUILD dispatch repository"
    if not _valid_trunk_ref(event.get("closing_trunk_ref")):
        return "closing trunk ref must be a named branch/remote ref"
    if not _FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
        return "closing trunk sha must be the pinned full commit id"
    mode = event.get("close_proof_mode")
    if mode not in CLOSE_PROOF_MODES:
        return "close proof mode must be one of %s" % "/".join(CLOSE_PROOF_MODES)
    translated = event.get("translated_tip")
    if mode.startswith("translated-"):
        if not _FULL_TIP.fullmatch(str(translated or "")):
            return "a translated proof must carry the full translated tip"
    elif translated is not None:
        return "translated_tip on an untranslated proof"
    if current is None or verdicts is None:
        return "build-landed replay needs its one dispatch snapshot"
    review_id = str(event.get("landing_review_id") or "")
    review_tip = str(event.get("landing_review_tip") or "")
    if not _ID.fullmatch(review_id):
        return "landing review id must be a full dispatch id"
    if not _FULL_TIP.fullmatch(review_tip):
        return "landing review tip must be a full commit id"
    review = current.get(review_id)
    pair = verdicts.get(review_id)
    index = verdict_index(verdicts, review_id)
    if review is None or review.get("status") != "verdict" or pair is None \
            or index is None:
        return "landing review is not one accepted durable verdict"
    if review.get("kind") != "review" or review.get("polarity") != "approve" \
            or review.get("repo_id") != repo \
            or review.get("reviewed_tip") != review_tip:
        return "landing review fields do not match an approved review in the repository"
    anchor = str(event.get("landing_review_verdict_anchor") or "")
    if not _PROOF_ANCHOR.fullmatch(anchor) or anchor != pair[1]:
        return "landing review verdict anchor does not match the accepted event"
    reaches, why = _chain_reaches(review, state.get("id"), current)
    if reaches is not True or review_id == state.get("id"):
        return "landing review is not an exact descendant of the BUILD dispatch%s" \
            % (" (%s)" % why if why else "")
    from . import landreq
    epoch = gate_epoch(current, verdicts)
    requirement = landreq.gate_requirement(review, index=index, epoch=epoch)
    if requirement == "unknown" \
            or event.get("landing_review_gate_requirement") != requirement:
        return "landing review gate requirement does not match the verdict epoch"
    gate_id = str(review.get("gate") or "")
    if event.get("landing_review_gate") != gate_id \
            or requirement == "required" and not _GATE_ID.fullmatch(gate_id):
        return "landing review gate binding does not match the standing verdict"
    tier = event.get("landing_review_tier_state")
    if tier not in ("none", "ok"):
        return "landing review tier state must be none or ok"
    approval = event.get("landing_review_approval_anchor")
    if not _PROOF_ANCHOR.fullmatch(str(approval or "")) \
            or approval != _landed_review_approval_anchor(
                anchor, tier, requirement, gate_id):
        return "landing review approval anchor does not match its captured proof"
    if verify_live:
        refusal, live_tier = landreq._approval_refusal(
            review, index=index, epoch=epoch)
        if refusal or live_tier != tier:
            return "landing review approval no longer matches the locked proof"
    return None


def _delivered_report_event_error(event, state, correction=False):
    """Validate one delivered-report close/correction at writer and replay.

    Artifact identity, the chat handoff reference, and concise evidence are
    distinct required fields. None is a transport ``delivery_ref`` and none is
    allowed to arrive through prose inference. The exact whole-object schema is
    shared by the locked append boundary and the replay reducer."""
    expected = _DELIVERED_REPORT_CORRECTION_FIELDS if correction \
        else _DELIVERED_REPORT_EVENT_FIELDS
    if set(event) != expected:
        missing = sorted(expected - set(event))
        extra = sorted(set(event) - expected)
        return ("%s fields do not match delivered-report schema "
                "(missing=%s extra=%s)" % (
                    "correction event" if correction else "close event",
                    ",".join(missing) or "-", ",".join(extra) or "-"))
    if event.get("v") != 3 or type(event.get("v")) is not int:
        return "delivered-report event must be a v3 object"
    want_event = "close-correction" if correction else "close"
    if event.get("event") != want_event \
            or event.get("close_reason") != "delivered-report":
        return "delivered-report event kind/reason does not match its schema"
    if str(event.get("id") or "") != state.get("id"):
        return "delivered-report event id does not match the standing dispatch"
    if type(event.get("seq")) is not int \
            or event.get("seq") != int(state.get("seq") or 0) + 1:
        return "delivered-report event sequence does not follow the standing dispatch"
    if type(event.get("close_proof_version")) is not int \
            or event.get("close_proof_version") != 1:
        return "delivered-report proof version must be 1"
    cleaned, err = clean_delivered_report_refs(
        event.get("artifact_ref"), event.get("report_ref"),
        event.get("close_evidence"))
    if err:
        return err
    if cleaned != (event.get("artifact_ref"), event.get("report_ref"),
                    event.get("close_evidence")):
        return "delivered-report references must round-trip clean"
    if state.get("kind") != "build":
        return "delivered-report needs an explicit BUILD dispatch"
    if correction:
        if state.get("status") != "cancelled":
            return "delivered-report correction needs one cancelled BUILD dispatch"
        if event.get("corrects_event") != "cancel" \
                or event.get("corrects_reason") != state.get("cancel_reason"):
            return "delivered-report correction does not bind the standing cancel event"
    elif state.get("status") != "open":
        return "delivered-report needs one OPEN BUILD dispatch"
    return None


def _close_event_error(event, state, current=None, verdicts=None,
                       verify_families=False):
    """Why one `close` EVENT may NOT bind to `state` — None when it binds.

    ONE rule for the writer (checked immediately before append, under the
    lock) and for replay (`_apply`), so a forged later event that the writer
    would refuse is inert on replay: the three-layer immutability trio
    carried whole. Every field is validated or the event does not apply."""
    if not isinstance(event, dict):
        return "event is not an object"
    reason = event.get("close_reason")
    if reason not in CLOSE_REASONS:
        return "close reason must be one of %s" % "/".join(CLOSE_REASONS)
    if event.get("v") != 3 or type(event.get("v")) is not int \
            or event.get("event") != "close":
        return "close event must be a v3 close object"
    if str(event.get("id") or "") != state.get("id"):
        return "close event id does not match the standing dispatch"
    if type(event.get("seq")) is not int \
            or event.get("seq") != int(state.get("seq") or 0) + 1:
        return "close event sequence does not follow the standing dispatch"
    version = event.get("close_proof_version")
    if reason == "delivered-report":
        return _delivered_report_event_error(event, state)
    if reason == "landed" and version == 2:
        return _build_landed_event_error(
            event, state, current, verdicts, verify_families)
    if reason == "subsumed":
        expected = _CLOSE_EVENT_BASE_FIELDS | set(_CLOSE_STATE_FIELDS[reason])
        if set(event) != expected:
            missing = sorted(expected - set(event))
            extra = sorted(set(event) - expected)
            return "close event fields do not match %s schema (missing=%s extra=%s)" \
                % (reason, ",".join(missing) or "-", ",".join(extra) or "-")
    # #177 — the discharged domain gate stands BEFORE the verdict gate, and
    # FIRST inside it: a verdicted row (including UNDECLARED, whose polarity
    # is None — the one value _CLOSE_POLARITY["discharged"] admits) must
    # never reach this door's ledger walk at all. Measured 2026-08-04 by the
    # integrator's adversarial pass: nested under `status != "verdict"`, a
    # forged discharged close on an UNDECLARED verdict row SKIPPED every
    # check here and bound downstream — replay closed a verdicted row on a
    # discharging id naming nothing on the ledger.
    if reason == "discharged":
        if state.get("status") != "open":
            return "discharged closes an OPEN build row, never a verdict"
    if state.get("status") != "verdict":
        if reason == "discharged":
            # #177 — the ONE door a polarity-less row has. The writer and this
            # replay arm re-derive the discharge from the SAME ledger read
            # rather than trusting the event's say-so: the recorded
            # discharging row must still vouch (landed, gate-verified,
            # APPROVE) for THIS row right now. An event naming a row that no
            # longer reaches — or never did — is inert, not binding.
            if current is None:
                return "discharged needs the full ledger to re-walk the chain"
            dis_id = str(event.get("discharging_id") or "")
            if not _ID.fullmatch(dis_id):
                return "discharging id must be a dispatch id"
            if not _FULL_TIP.fullmatch(str(event.get("discharging_tip") or "")):
                return "discharging tip must be a full commit id"
            if event.get("discharge_tier") != DISCHARGE_TIER_CHAIN:
                return "discharge tier must be %s" % DISCHARGE_TIER_CHAIN
            # Replay never probes git; the landed leg reads the LEDGER's own
            # terminal — a `landed` close on the discharging row, or the
            # legacy close-landed closure — and could-not-tell is UNKNOWN,
            # which discharging_row fails closed.
            def _replay_landed(tip):
                for cand in (current or {}).values():
                    if not isinstance(cand, dict):
                        continue
                    if str(cand.get("reviewed_tip") or "") != str(tip):
                        continue
                    if cand.get("close_reason") == "landed" \
                            or cand.get("closed_by_landing"):
                        return True
                return None
            tier, by, why = discharging_row(
                state["id"], current, is_landed=_replay_landed)
            if tier is None:
                return "discharge refused on re-walk: %s" % why
            if by != dis_id:
                return ("event names %s but the ledger says %s discharges "
                        "this row" % (dis_id[:12], str(by)[:12]))
            dis_row = (current or {}).get(by) or {}
            if str(dis_row.get("reviewed_tip") or "") != \
                    str(event.get("discharging_tip")):
                return "discharging tip does not match the discharging row"
            # The two callers inject DIFFERENT landed legs — the writer adds
            # a live git probe the replay arm cannot run. The event records
            # which authority vouched, so the claim survives the asymmetry:
            # the writer's leg is strictly stronger, never weaker.
        else:
            return "not a verdict row"
    if state.get("discharged") or state.get("withdrawn") \
            or state.get("closed_by_landing") or state.get("abandoned") \
            or state.get("close_reason"):
        return "already retired"
    reviewed = str(event.get("reviewed_tip") or "")
    if reason != "discharged" and (not _FULL_TIP.fullmatch(reviewed)
                                   or reviewed != state.get("reviewed_tip")):
        return "reviewed tip does not match the standing verdict"
    if state.get("polarity") not in _CLOSE_POLARITY[reason]:
        return "verdict polarity outside --reason %s's domain" % reason
    version = event.get("close_proof_version")
    if type(version) is not int or version != 1:
        return "close proof version must be 1"
    # The ts is deliberately NOT validated here. A close's WHEN rides the
    # projection's typed closure-stamp machinery (absent / unreadable /
    # impossible, each first-class), and the standing physics is explicit:
    # the flag asserts the retirement, the stamp only says when. Making a
    # corrupt ts byte refuse the whole event would RESURRECT proven-retired
    # debt — the one direction that machinery exists to prevent.
    evidence = event.get("close_evidence")
    if evidence is not None or reason != "landed":
        # evidence is OPTIONAL for landed (git alone authors that terminal)
        # and REQUIRED everywhere else; present it must always be clean.
        cleaned, err = _clean(evidence, "close evidence", 256)
        if err or cleaned != evidence:
            return err or "close evidence must round-trip clean"
    if reason == "landed":
        repo, err = _clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo):
            return "closing repo id must be an absolute path"
        if not _valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not _FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        mode = event.get("close_proof_mode")
        if mode not in CLOSE_PROOF_MODES:
            return "close proof mode must be one of %s" % \
                "/".join(CLOSE_PROOF_MODES)
        translated = event.get("translated_tip")
        if mode.startswith("translated-"):
            if not _FULL_TIP.fullmatch(str(translated or "")):
                return "a translated proof must carry the full translated tip"
        elif translated is not None:
            return "translated_tip on an untranslated proof"
        derr = _delivery_error(event)
        if derr:
            return derr
    elif reason == "superseded":
        superseding = str(event.get("superseding_tip") or "")
        if not _FULL_TIP.fullmatch(superseding):
            return "superseding tip must be a full commit id"
        if superseding == reviewed:
            return "a verdict tip cannot supersede itself"
        if not _ID.fullmatch(str(event.get("superseding_id") or "")):
            return "superseding id must be a dispatch id"
        if event.get("close_contrary_state") not in ("landed", "none"):
            return "contrary state must be landed or none"
        if event.get("close_contrary_target") not in ("local", "upstream"):
            return "contrary target must be local or upstream"
        if not _valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not _FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        # #101 — the translated-object-superseded door records HOW the row's
        # destroyed reviewed object was adjudicated. Both fields are present
        # together or neither; a plain supersession never carries them.
        mode = event.get("close_proof_mode")
        translated = event.get("translated_tip")
        if mode == "translated-superseded":
            if not _FULL_TIP.fullmatch(str(translated or "")):
                return "a translated supersession must carry the full translated tip"
        elif mode in ("archived-superseded", "attested-superseded"):
            if translated is not None:
                return "an archived/attested supersession has no translated object"
        elif mode is not None:
            return "superseded proof mode must be a door mode or absent"
        elif translated is not None:
            return "translated_tip on an untranslated supersession"
    elif reason == "withdrawn":
        if not _valid_trunk_ref(event.get("absence_trunk_ref")):
            return "absence trunk ref must be a named branch/remote ref"
        if not _FULL_TIP.fullmatch(str(event.get("absence_trunk_sha") or "")):
            return "absence trunk sha must be the pinned full commit id"
        mode = event.get("close_proof_mode")
        translated = event.get("translated_tip")
        if mode is None:
            # legacy pre-translation withdrawn events carry neither field
            if translated is not None:
                return "translated_tip on an untranslated absence proof"
        elif mode not in WITHDRAWN_PROOF_MODES:
            return "withdrawn proof mode must be one of %s" \
                % "/".join(WITHDRAWN_PROOF_MODES)
        elif mode == "translated-absent":
            if not _FULL_TIP.fullmatch(str(translated or "")):
                return ("a translated absence proof must carry the full "
                        "translated tip")
        elif translated is not None:
            return "translated_tip on an untranslated absence proof"
    elif reason == "stranded":
        repo, err = _clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo):
            return "closing repo id must be an absolute path"
        if not _FULL_TIP.fullmatch(str(event.get("control_sha") or "")):
            return "control sha must be the full trunk object the repo proved"
        if event.get("close_proof_mode") != "object-pruned":
            return "stranded proof mode must be object-pruned"
    elif reason == "subsumed":
        if not state.get("verdict_ref") or not state.get("repo_id"):
            return "subsumed needs an approved verdict on a readable repository"
        if state.get("chain_root") == CHAIN_UNKNOWN:
            return "subsumed needs a readable work chain"
        if state.get("kind") != "review":
            return "subsumed needs an original review dispatch"
        confirmation_id = str(event.get("confirmation_id") or "")
        confirmation_tip = str(event.get("confirmation_tip") or "")
        confirmation_ref, err = _clean(event.get("confirmation_ref"),
                                       "confirmation ref", 4096)
        if not err and len(_GATE_TOKEN_RE.sub("", confirmation_ref)) > 256:
            err = ("confirmation ref is over the 256-char statement budget "
                   "(gate: tokens excluded)")
        if not _ID.fullmatch(confirmation_id):
            return "confirmation id must be a full dispatch id"
        if not _FULL_TIP.fullmatch(confirmation_tip):
            return "confirmation tip must be a full commit id"
        if err or not _subsumption_ref(
                confirmation_ref, state.get("polarity")):
            return ("confirmation verdict needs an explicit FIX findings-answered "
                    "statement" if state.get("polarity") == "fix" else
                    "confirmation verdict needs an explicit subsumption statement")
        if not _ID.fullmatch(str(evidence or "")) \
                or not confirmation_id.startswith(evidence):
            return "close evidence selector does not identify confirmation id"
        original_author = str(event.get("original_author") or "")
        confirmation_recipient = str(event.get("confirmation_recipient") or "")
        original_family = str(event.get("original_author_family") or "")
        confirmation_family = str(event.get("confirmation_recipient_family") or "")
        if original_author != state.get("sender") \
                or not _TOKEN.fullmatch(original_author):
            return "original author binding does not match the standing verdict"
        if not _TOKEN.fullmatch(confirmation_recipient):
            return "confirmation recipient binding is malformed"
        if not _TOKEN.fullmatch(original_family) \
                or not _TOKEN.fullmatch(confirmation_family) \
                or original_family == confirmation_family:
            return "subsumed needs two distinct canonical identity families"
        err = _family_evidence_error(
            event.get("original_family_evidence"), original_author,
            original_family, event.get("original_family_anchor"))
        if err:
            return "original author %s" % err
        err = _family_evidence_error(
            event.get("confirmation_family_evidence"), confirmation_recipient,
            confirmation_family, event.get("confirmation_family_anchor"))
        if err:
            return "confirmation recipient %s" % err
        repo, err = _clean(event.get("closing_repo_id"), "closing repo id", 4096)
        if err or not os.path.isabs(repo) or os.path.realpath(repo) != repo \
                or repo != state.get("repo_id"):
            return "closing repo id must match the original canonical repository"
        if not _valid_trunk_ref(event.get("closing_trunk_ref")):
            return "closing trunk ref must be a named branch/remote ref"
        if not _FULL_TIP.fullmatch(str(event.get("closing_trunk_sha") or "")):
            return "closing trunk sha must be the pinned full commit id"
        if event.get("close_proof_mode") not in ("ancestor", "patch-equivalent"):
            return "confirmation proof mode must be ancestor or patch-equivalent"
        if event.get("original_proof_mode") != "absent":
            return "original proof mode must be absent"
        # The original is deliberately off trunk and may be pruned after lane
        # cleanup, so replay cannot re-run Git without resurrecting valid debt.
        # Reject contradictions still decidable from immutable event identities;
        # the locked writer below owns the full live Git proof.
        trunk_sha = str(event.get("closing_trunk_sha") or "")
        if reviewed == trunk_sha:
            return "original Git proof contradicts the pinned trunk sha"
        if confirmation_tip == reviewed:
            return "one reviewed tip cannot be both present and absent on trunk"
        if current is None or verdicts is None:
            return "subsumed replay needs its one dispatch snapshot"
        confirmation = current.get(confirmation_id)
        if confirmation is None or confirmation.get("status") != "verdict":
            return "confirmation dispatch is not a standing verdict"
        original_pair = verdicts.get(state["id"])
        confirmation_pair = verdicts.get(confirmation_id)
        original_index = verdict_index(verdicts, state["id"])
        confirmation_index = verdict_index(verdicts, confirmation_id)
        if original_index is None or confirmation_index is None \
                or confirmation_index <= original_index:
            return "confirmation verdict must be later than the original"
        original_anchor = str(event.get("original_verdict_anchor") or "")
        confirmation_anchor = str(event.get("confirmation_verdict_anchor") or "")
        if not _PROOF_ANCHOR.fullmatch(original_anchor) \
                or not original_pair or original_anchor != original_pair[1]:
            return "original verdict anchor does not match the accepted event"
        if not _PROOF_ANCHOR.fullmatch(confirmation_anchor) \
                or not confirmation_pair \
                or confirmation_anchor != confirmation_pair[1]:
            return "confirmation verdict anchor does not match the accepted event"
        expected_chain = state["id"] if state.get("chain_root") is None \
            else state.get("chain_root")
        if confirmation.get("repo_id") != state.get("repo_id") \
                or confirmation.get("chain_root") != expected_chain:
            return "confirmation verdict must be linked to the same chain/repo"
        if confirmation.get("recipient") != confirmation_recipient:
            return "confirmation recipient does not match the standing verdict"
        if confirmation.get("kind") != "review" \
                or confirmation.get("polarity") != "approve" \
                or confirmation.get("reviewed_tip") != confirmation_tip \
                or confirmation.get("verdict_ref") != confirmation_ref:
            return "confirmation fields do not match the standing review verdict"
        from . import landreq
        epoch = gate_epoch(current, verdicts)
        requirement = landreq.gate_requirement(
            confirmation, index=confirmation_index, epoch=epoch)
        if requirement == "unknown" \
                or event.get("confirmation_gate_requirement") != requirement:
            return "confirmation gate requirement does not match the verdict epoch"
        gate_id = str(confirmation.get("gate") or "")
        if event.get("confirmation_gate") != gate_id \
                or requirement == "required" \
                and not _GATE_ID.fullmatch(gate_id):
            return "confirmation gate binding does not match the standing verdict"
        tier = event.get("confirmation_tier_state")
        if tier not in ("none", "ok"):
            return "confirmation tier state must be none or ok"
        approval_anchor = event.get("confirmation_approval_anchor")
        if not _PROOF_ANCHOR.fullmatch(str(approval_anchor or "")) \
                or approval_anchor != _subsumed_approval_anchor(
                    confirmation_anchor, tier, requirement, gate_id):
            return "confirmation approval anchor does not match its captured proof"
        if verify_families:
            refusal, live_tier = landreq._approval_refusal(
                confirmation, index=confirmation_index, epoch=epoch)
            if refusal or live_tier != tier:
                return "confirmation approval no longer matches the locked proof"
            original_families, original_evidence, original_live_anchor, why = \
                _approval_identity_family_evidence(original_author)
            if why or original_families != {original_family} \
                    or original_evidence != event.get("original_family_evidence") \
                    or original_live_anchor != event.get("original_family_anchor"):
                return "original author family is UNKNOWN or conflicts"
            confirmation_families, confirmation_evidence, \
                confirmation_live_anchor, why = \
                _approval_identity_family_evidence(confirmation_recipient)
            if why or confirmation_families != {confirmation_family} \
                    or confirmation_evidence != \
                    event.get("confirmation_family_evidence") \
                    or confirmation_live_anchor != \
                    event.get("confirmation_family_anchor"):
                return "confirmation recipient family is UNKNOWN or conflicts"
    return None


def _close_retired_by(row):
    """What already retired this row, for the unified exclusivity refusal."""
    if row.get("discharged"):
        return "discharge"
    if row.get("withdrawn"):
        return "withdraw"
    if row.get("closed_by_landing"):
        return "close-landed"
    if row.get("abandoned"):
        return "abandon"
    if row.get("close_reason"):
        return "close --reason %s" % row["close_reason"]
    return None


def _close_idempotent(reason, row, event):
    """True when a second identical close is a RETRY of the standing one.

    Per-reason identity, deliberately narrow:
      landed      closing repo + trunk REF only — the sha is EXCLUDED so a
                  retry after trunk movement reconciles instead of refusing
                  (the recorded pin stays the historical anchor)
      superseded  superseding tip + evidence
      withdrawn   evidence
      stranded    closing repo + evidence
      delivered-report artifact ref + report ref + evidence
      subsumed    canonical confirmation + captured authorization + repo/trunk
                  ref; selector spelling and moving trunk sha are excluded"""
    if row.get("close_reason") != reason:
        return False
    if reason == "landed":
        if row.get("landing_review_id") or event.get("landing_review_id"):
            stable = ("landing_review_id", "landing_review_tip",
                      "landing_review_verdict_anchor",
                      "landing_review_tier_state",
                      "landing_review_gate_requirement", "landing_review_gate",
                      "landing_review_approval_anchor", "closing_repo_id",
                      "closing_trunk_ref")
            return all(row.get(key) == event.get(key) for key in stable)
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("closing_trunk_ref") == event.get("closing_trunk_ref")
    if reason == "superseded":
        return row.get("superseding_tip") == event.get("superseding_tip") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "withdrawn":
        return row.get("close_evidence") == event.get("close_evidence")
    if reason == "stranded":
        return row.get("closing_repo_id") == event.get("closing_repo_id") \
            and row.get("close_evidence") == event.get("close_evidence")
    if reason == "delivered-report":
        return all(row.get(key) == event.get(key)
                   for key in _DELIVERED_REPORT_PAYLOAD_FIELDS)
    if reason == "subsumed":
        stable = ("confirmation_id", "confirmation_tip", "confirmation_ref",
                  "original_author", "confirmation_recipient",
                  "original_author_family", "confirmation_recipient_family",
                  "original_verdict_anchor", "confirmation_verdict_anchor",
                  "original_family_evidence", "confirmation_family_evidence",
                  "original_family_anchor", "confirmation_family_anchor",
                  "confirmation_tier_state", "confirmation_gate_requirement",
                  "confirmation_gate", "confirmation_approval_anchor",
                  "original_proof_mode", "closing_repo_id",
                  "closing_trunk_ref")
        return all(row.get(key) == event.get(key) for key in stable)
    return False


def _writer_landed(current, tip):
    """The discharged door's live landed leg: the discharging row's own
    terminal names the repo and trunk (a `landed` close, or the legacy
    close-landed closure); the reviewed tip must be an ancestor of it NOW.
    UNKNOWN (no terminal, no probe answer) fails closed."""
    for cand in (current or {}).values():
        if not isinstance(cand, dict):
            continue
        if str(cand.get("reviewed_tip") or "") != str(tip):
            continue
        if cand.get("close_reason") == "landed":
            return _tip_on_trunk(cand.get("closing_repo_id"),
                                 cand.get("closing_trunk_ref"), tip)
        if cand.get("closed_by_landing"):
            return _tip_on_trunk(cand.get("landing_repo_id"),
                                 cand.get("landing_trunk_ref"), tip)
    return None


def _record_close_proven(rid, reason, reviewed_tip, evidence=None,
                         closing_repo_id=None, closing_trunk_ref=None,
                         closing_trunk_sha=None, proof_mode=None,
                         translated_tip=None, superseding_tip=None,
                         superseding_id=None, contrary_state=None,
                         contrary_target=None, absence_trunk_ref=None,
                         absence_trunk_sha=None, control_sha=None,
                         confirmation_id=None, confirmation_tip=None,
                         confirmation_ref=None, original_author=None,
                         confirmation_recipient=None,
                         original_author_family=None,
                         confirmation_recipient_family=None,
                         original_verdict_anchor=None,
                         confirmation_verdict_anchor=None,
                         original_family_evidence=None,
                         confirmation_family_evidence=None,
                         original_family_anchor=None,
                         confirmation_family_anchor=None,
                         confirmation_tier_state=None,
                         confirmation_gate_requirement=None,
                         confirmation_gate=None,
                         confirmation_approval_anchor=None,
                         original_proof_mode=None, close_proof_version=1,
                         landing_review_id=None, landing_review_tip=None,
                         landing_review_verdict_anchor=None,
                         landing_review_tier_state=None,
                         landing_review_gate_requirement=None,
                         landing_review_gate=None,
                         landing_review_approval_anchor=None,
                         delivery_class=None, delivery_restart=None,
                         artifact_ref=None, report_ref=None,
                         discharging_id=None, discharging_tip=None,
                         discharge_tier=None):
    """Append ONE proven `close` event without rewriting its verdict.

    landreq owns every live Git/liveness proof; this locked boundary
    re-validates the immutable row state per reason and records exactly one
    terminal annotation. Identical retries are idempotent under the
    per-reason identity; everything else refuses. The git proofs necessarily
    ran OUTSIDE the lock (seconds-wide window) — the row-STATE facts they
    depended on are all re-read here under the lock, and the recorded pinned
    trunk sha keeps a proof that went stale-but-was-true auditable."""
    reviewed = str(reviewed_tip or "").strip().lower()
    build_landed = reason == "landed" and close_proof_version == 2
    delivered_report = reason == "delivered-report"
    discharged = reason == "discharged"
    if not build_landed and not delivered_report and not discharged \
            and not _FULL_TIP.fullmatch(reviewed):
        return None, "close needs the original full reviewed commit id"
    if reason not in CLOSE_REASONS:
        return None, "close reason must be one of %s" % "/".join(CLOSE_REASONS)
    if type(close_proof_version) is not int \
            or close_proof_version not in ((1, 2) if reason == "landed" else (1,)):
        return None, "close proof version is not valid for this reason"
    if evidence is not None and not delivered_report:
        evidence, err = _clean(evidence, "close evidence", 256)
        if err:
            return None, err
    if delivered_report:
        values, err = clean_delivered_report_refs(
            artifact_ref, report_ref, evidence)
        if err:
            return None, err
        artifact_ref, report_ref, evidence = values
    if closing_repo_id is not None \
            and os.path.realpath(closing_repo_id) != closing_repo_id:
        return None, "close needs a canonical absolute Git common-dir"
    candidate = {"v": 3, "event": "close", "id": None, "ts": None,
                 "close_reason": reason,
                 "close_proof_version": close_proof_version}
    if build_landed:
        candidate.update(
            landing_review_id=landing_review_id,
            landing_review_tip=landing_review_tip,
            landing_review_verdict_anchor=landing_review_verdict_anchor,
            landing_review_tier_state=landing_review_tier_state,
            landing_review_gate_requirement=landing_review_gate_requirement,
            landing_review_gate=landing_review_gate,
            landing_review_approval_anchor=landing_review_approval_anchor,
            translated_tip=translated_tip,
            # Unconditionally, both keys — a v2 row written from here always
            # carries its delivery answer, so "absent" can only ever mean
            # "written before this field existed", never "we forgot".
            close_delivery_class=delivery_class,
            close_delivery_restart=delivery_restart)
    elif delivered_report:
        candidate["close_evidence"] = evidence
    elif discharged:
        # A polarity-less row has no reviewed tip of its own; the event's
        # authority is the discharging row's identity, carried explicitly.
        if evidence is not None:
            candidate["close_evidence"] = evidence
    else:
        candidate["reviewed_tip"] = reviewed
        if evidence is not None:
            candidate["close_evidence"] = evidence
    for key, value in (("closing_repo_id", closing_repo_id),
                       ("closing_trunk_ref", closing_trunk_ref),
                       ("closing_trunk_sha", closing_trunk_sha),
                       ("close_proof_mode", proof_mode),
                       ("translated_tip", translated_tip),
                       ("superseding_tip", superseding_tip),
                       ("superseding_id", superseding_id),
                       ("close_contrary_state", contrary_state),
                       ("close_contrary_target", contrary_target),
                       ("absence_trunk_ref", absence_trunk_ref),
                       ("absence_trunk_sha", absence_trunk_sha),
                       ("control_sha", control_sha),
                       ("confirmation_id", confirmation_id),
                       ("confirmation_tip", confirmation_tip),
                       ("confirmation_ref", confirmation_ref),
                       ("original_author", original_author),
                       ("confirmation_recipient", confirmation_recipient),
                       ("original_author_family", original_author_family),
                       ("confirmation_recipient_family",
                        confirmation_recipient_family),
                       ("original_verdict_anchor", original_verdict_anchor),
                       ("confirmation_verdict_anchor",
                        confirmation_verdict_anchor),
                       ("original_family_evidence", original_family_evidence),
                       ("confirmation_family_evidence",
                        confirmation_family_evidence),
                       ("original_family_anchor", original_family_anchor),
                       ("confirmation_family_anchor",
                        confirmation_family_anchor),
                       ("confirmation_tier_state", confirmation_tier_state),
                       ("confirmation_gate_requirement",
                        confirmation_gate_requirement),
                       ("confirmation_gate", confirmation_gate),
                       ("confirmation_approval_anchor",
                        confirmation_approval_anchor),
                       ("original_proof_mode", original_proof_mode),
                       ("close_delivery_class", delivery_class),
                       ("close_delivery_restart", delivery_restart),
                       ("artifact_ref", artifact_ref),
                       ("report_ref", report_ref),
                       ("discharging_id", discharging_id),
                       ("discharging_tip", discharging_tip),
                       ("discharge_tier", discharge_tier)):
        if key in _CLOSE_STATE_FIELDS[reason] and value is not None:
            candidate[key] = value
    if build_landed:
        expected = _BUILD_LANDED_EVENT_FIELDS - {"seq"}
        if set(candidate) != expected:
            missing = sorted(expected - set(candidate))
            extra = sorted(set(candidate) - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match build-landed schema (missing=%s extra=%s)" % (
                              ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    if delivered_report:
        expected = _DELIVERED_REPORT_EVENT_FIELDS - {"seq"}
        if set(candidate) != expected:
            missing = sorted(expected - set(candidate))
            extra = sorted(set(candidate) - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match delivered-report schema (missing=%s extra=%s)" % (
                              ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    if reason == "subsumed":
        expected = (_CLOSE_EVENT_BASE_FIELDS | set(_CLOSE_STATE_FIELDS[reason])) \
            - {"seq"}
        if set(candidate) != expected:
            missing = sorted(expected - set(candidate))
            extra = sorted(set(candidate) - expected)
            return None, ("close does not bind: close candidate fields do not "
                          "match %s schema (missing=%s extra=%s)" % (
                              reason, ",".join(missing) or "-",
                              ",".join(extra) or "-"))
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — close NOT recorded" % path
        current, verdicts, unavailable = snapshot_with_verdicts()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if reason == "subsumed":
            selected, err = _resolve_row(
                current, evidence, noun="confirmation dispatch",
                list_hint="helm dispatch list")
            if err:
                return None, "close does not bind: %s" % err
            if selected["id"] != confirmation_id:
                return None, ("close does not bind: confirmation selector no "
                              "longer identifies the captured confirmation")
        retired = _close_retired_by(row)
        if retired:
            if _close_idempotent(reason, row, candidate):
                return row, None
            return None, ("dispatch %s is already retired by %s; a row is "
                          "retired once — refusing a different closure"
                          % (row["id"], retired))
        event = dict(candidate, id=row["id"], seq=row["seq"] + 1,
                     ts=pk.now_ts())
        if reason == "discharged":
            # WRITER-SIDE re-walk under the lock, with the live git probe the
            # replay arm cannot run: the discharging row's recorded landing
            # names repo + trunk, and the reviewed tip must STILL be an
            # ancestor of it. The candidate's fields then go through
            # _close_event_error like every other reason, which re-walks the
            # same ledger with the content-addressed landed leg.
            tier, by, why = discharging_row(
                row["id"], current,
                is_landed=lambda tip: _writer_landed(current, tip))
            if tier is None:
                return None, "close does not bind: discharge refused — %s" % why
            if by != str(candidate.get("discharging_id") or ""):
                return None, ("close does not bind: the ledger says %s "
                              "discharges this row, not %s"
                              % (str(by)[:12],
                                 str(candidate.get("discharging_id"))[:12]))
            dis_row = current.get(by) or {}
            if str(dis_row.get("reviewed_tip") or "") != \
                    str(candidate.get("discharging_tip") or ""):
                return None, ("close does not bind: discharging tip does not "
                              "match the discharging row's reviewed tip")
        err = _close_event_error(
            event, row, current=current, verdicts=verdicts,
            verify_families=reason == "subsumed" or build_landed)
        if err:
            return None, "close does not bind: %s" % err
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — close NOT recorded" % path
    out = _apply(row, event, current=current, verdicts=verdicts)
    pk.event("dispatch-close", row["id"], reason)
    return out, None


def record_delivered_report_correction(rid, artifact_ref, report_ref, evidence):
    """Explicitly correct one historical cancelled BUILD to delivered-report.

    The original cancel event and reason remain in history and on the projected
    row. No wording is interpreted: only this append-only event, carrying fresh
    artifact identity and chat handoff references, changes the canonical
    terminal. Identical retries reconcile; any different annotation conflicts."""
    values, err = clean_delivered_report_refs(
        artifact_ref, report_ref, evidence)
    if err:
        return None, err
    artifact_ref, report_ref, evidence = values
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, ("ledger unwritable (%s) — delivered-report correction "
                          "NOT recorded" % path)
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        if row.get("delivered_report_correction"):
            values = (artifact_ref, report_ref, evidence)
            if all(row.get(key) == value for key, value in zip(
                    _DELIVERED_REPORT_PAYLOAD_FIELDS, values)):
                return row, None
            return None, ("dispatch %s already has a delivered-report correction; "
                          "refusing different artifact/report evidence" % row["id"])
        if row.get("close_reason"):
            return None, ("dispatch %s is already retired by close --reason %s; "
                          "a historical correction only annotates a cancellation"
                          % (row["id"], row["close_reason"]))
        event = {"v": 3, "event": "close-correction",
                 "seq": row["seq"] + 1, "id": row["id"], "ts": pk.now_ts(),
                 "close_reason": "delivered-report", "close_proof_version": 1,
                 "artifact_ref": artifact_ref, "report_ref": report_ref,
                 "close_evidence": evidence, "corrects_event": "cancel",
                 "corrects_reason": row.get("cancel_reason")}
        err = _delivered_report_event_error(event, row, correction=True)
        if err:
            return None, "delivered-report correction does not bind: %s" % err
        if not eventledger.append_unlocked(path, event):
            return None, ("ledger unwritable (%s) — delivered-report correction "
                          "NOT recorded" % path)
    out = _apply(row, event)
    pk.event("dispatch-close-correction", row["id"], "delivered-report")
    return out, None


def mark_cancel(rid, reason):
    """Honestly ABANDON an open dispatch with a reason — the only truthful
    terminal event when a verdict will never come (recipient gone, work moot,
    superseded). Idempotent on the same reason; refuses a dispatch that already
    carries a verdict (that one is already honestly closed).

    Resolve the displayed id FIRST. A bad short id and a bad reason are two
    independent errors; reporting the reason first made operators repair inputs
    in series before learning the identifier printed by `lr stalls` was usable.
    """
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = _resolve_row(current, rid)
        if err:
            return None, err
        reason, err = _clean(reason, "cancel reason", _CANCEL_REASON_CAP)
        if err:
            return None, err
        if not reason:
            return None, "cancel needs a reason (why the dispatch is abandoned)"
        if row["status"] == "cancelled":
            if row.get("cancel_reason") == reason:
                return row, None            # idempotent re-cancel
            return None, "dispatch %s already cancelled" % row["id"]
        if row["status"] == "verdict":
            return None, ("dispatch %s already has a verdict (closed) — a "
                          "reviewed dispatch is not cancelled" % row["id"])
        if row["status"] == "closed":
            return None, ("dispatch %s is already closed through its approved "
                          "review descendant" % row["id"])
        if row["status"] not in CANCELLABLE_STATES:
            return None, ("dispatch %s is %s -- only an open or held dispatch "
                         "can be cancelled" % (row["id"], row["status"]))
        event = {"v": 3, "event": "cancel", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "reason": reason}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — cancel NOT recorded" % path
    out = dict(row)
    out.update(status="cancelled", cancel_reason=reason, seq=event["seq"])
    pk.event("dispatch-cancel", row["id"], reason)
    return out, None

def mark_hold(rid, reason):
    """Put an OPEN dispatch on HOLD."""
    path = ledger_path()
    with eventledger.locked(path) as locked:
        if not locked:
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
        if row["status"] == "held":
            if row.get("hold_reason") == reason:
                return row, None
            return None, ("dispatch %s is already held with a different reason "
                         "(%s) -- release it first, or cancel and re-dispatch"
                         % (row["id"], row.get("hold_reason") or "unspecified"))
        if row["status"] in CLOSED_STATES:
            return None, ("dispatch %s is %s -- only an OPEN row can be held"
                         % (row["id"], row["status"]))
        event = {"v": 3, "event": "hold", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "reason": reason}
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) -- hold NOT recorded" % path
    out = dict(row)
    out.update(status="held", hold_reason=reason, hold_ts=event["ts"],
               seq=event["seq"])
    pk.event("dispatch-hold", row["id"], reason)
    return out, None


def mark_release(rid):
    """Return a HELD dispatch to OPEN."""
    path = ledger_path()
    with eventledger.locked(path) as locked:
        if not locked:
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
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) -- release NOT recorded" % path
    out = dict(row)
    out.update(status="open", release_reason=event["reason"],
               release_ts=event["ts"], seq=event["seq"])
    for key in ("hold_reason", "hold_ts"):
        out.pop(key, None)
    pk.event("dispatch-release", row["id"], event["reason"])
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
    Measured 2026-08-05: codex-3 climbed 82% -> 116% over twenty minutes across
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
    done = []
    with eventledger.locked(path) as held:
        if not held:
            return done, "ledger unwritable (%s) — sweep NOT recorded" % path
        fresh, unavailable = snapshot()
        if unavailable:
            return done, "dispatch ledger unavailable: %s" % unavailable
        for pid, kid in hits:
            parent = fresh.get(pid)
            if not isinstance(parent, dict) or parent.get("superseded_by"):
                continue            # re-checked under the lock, never assumed
            if not eventledger.append_unlocked(path, {
                    "v": 3, "event": "superseded",
                    "seq": (parent.get("seq") or 0) + 1,
                    "id": pid, "ts": pk.now_ts(), "successor": kid}):
                return done, "ledger unwritable (%s)" % path
            done.append((pid, kid))
    return done, None


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
    exists to clean up, leaving BOTH rows open (codex-2, first review round of
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
    # moved — the same stranding one generation down (codex-2, adversarial sweep
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
    # A rebind moves the SAME obligation, so its repo identity is immutable:
    # --repo may only spell an alternate path to the repo already recorded on
    # the row. A same-tip foreign clone resolves to a different .git and is
    # refused BEFORE the cancel — a silent identity swap at rc0 was the
    # codex finding on 6a8f9530. On a validated row the RECORDED root is what
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
    # The new row is an `add` (persist + public notice), never a `send`:
    # the old row's DM body is not recoverable from the ledger (only its
    # hash), and a rebind that invented a message would put words in the
    # original sender's mouth. The recipient's beacon fires on the public
    # notice; the rebind reason names why the obligation moved.
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
        _ref_branch=row.get("ref_branch"))
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


def _retip_identity(row, new_tip):
    """(identity, refusal) — is `new_tip` the SAME WORK this row already names?

    THREE ANSWERS, and the third is not a soft no. "verified" means the
    substrate proved the identity; a refusal means it proved the OPPOSITE; and
    "unverified" means the question could not be answered — patch-id
    unavailable, no trunk, an unreadable repo. UNKNOWN never reads as a
    mismatch (that would refuse a legitimate retip wherever git cannot run)
    and never reads as a match (that would wave an unrelated commit into a
    brief written about something else); it proceeds and SAYS SO, in the
    identity stamp that travels on the event.

    KIND-AWARE, because the two kinds name different work. A REVIEW row's work
    is the per-commit ORDERED patch-id sequence its tip adds to trunk —
    patch-id, not sha, because a rebase is the whole reason retip exists (every
    sha changes, no patch does), and per-commit ordered because a lane that
    gained, lost, or reordered a commit is not the same work wearing a new
    base. A BUILD row's ref is a BASE, and a base may only move FORWARD: the
    new tip must descend from the old one. All plumbing is landreq's own
    (_git/_patch_id/_ancestry against the row's recorded repo_id) — one owner,
    never a second walker beside it.

    TWO EMPTY SEQUENCES CAN NEVER VERIFY (codex FIX on this verb's first
    cut): when both
    tips already sit ON trunk, both post-merge-base ranges are [] and []==[]
    is a statement about the RANGES, not about the WORK — on a linear A->B->C
    trunk it blessed A->B as "verified" while B carries a whole commit A does
    not, which is exactly the different-code move this check exists to refuse.
    With the lane range collapsed, the only honest comparison left is the
    reviewed patch itself: the tips' own diffs (diff-tree --root, so a root
    commit still answers) must carry the same stable patch-id. Equal own
    patches verify (the one real empty/empty case: a lane sha and its
    patch-identical landed spelling); different own patches REFUSE; an
    uncomputable own patch is UNKNOWN and proceeds stamped unverified."""
    old_tip = str(row.get("tip") or "")
    gitdir = str(row.get("repo_id") or "")
    from . import landreq
    if row.get("kind") == "build":
        relation = landreq._ancestry(gitdir, old_tip, new_tip)
        if relation == landreq.ANCESTOR:
            return "verified", None
        if relation == landreq.NOT_ANCESTOR:
            return None, ("retip REFUSED: a build base may only move FORWARD "
                          "— %s does not descend from %s, so this is "
                          "rewritten or unrelated work; nothing was changed. "
                          "Cancel + a fresh dispatch if the base really was "
                          "replaced" % (new_tip[:12], old_tip[:12]))
        return "unverified", None
    trunk = landreq._resolve_ref(gitdir, landreq.LOCAL_TRUNK)

    def sequence(tip):
        """Ordered per-commit patch-ids `tip` adds to trunk; None = UNKNOWN.
        [] is a VALID answer — "this tip adds nothing" — and must stay
        distinguishable from "could not compute": collapsing them let a tip
        already on trunk read as unverifiable instead of as different work."""
        if not trunk:
            return None
        mb = landreq._git(gitdir, "merge-base", tip, trunk)
        if mb is None or mb.returncode != 0 or not mb.stdout.strip():
            return None
        walked = landreq._git(gitdir, "rev-list", "--reverse",
                              mb.stdout.strip() + ".." + tip)
        if walked is None or walked.returncode != 0:
            return None
        ids = [landreq._patch_id(gitdir, c) for c in walked.stdout.split()]
        return ids if all(ids) else None

    old_seq, new_seq = sequence(old_tip), sequence(new_tip)
    if old_seq is None or new_seq is None:
        return "unverified", None
    if old_seq == new_seq == []:
        # Both tips are trunk ancestors: the ranges say nothing, so the
        # reviewed patch itself decides (see the docstring's empty/empty law).
        def own_patch(tip):
            d = landreq._git(gitdir, "diff-tree", "-p", "--no-commit-id",
                             "--root", tip)
            text = d.stdout if d is not None and d.returncode == 0 else ""
            if not text:
                return None
            p = landreq._git(gitdir, "patch-id", "--stable", input_text=text)
            parts = p.stdout.split() if p is not None else []
            return parts[0] if p is not None and p.returncode == 0 and parts \
                else None
        old_own, new_own = own_patch(old_tip), own_patch(new_tip)
        if old_own is None or new_own is None:
            return "unverified", None
        if old_own == new_own:
            return "verified", None
        return None, ("retip REFUSED: %s is not the same work as %s — both "
                      "tips already sit on trunk and their own patches "
                      "differ, so the new tip carries different code than "
                      "the one the recipient's brief was written about. "
                      "Nothing was changed. Use cancel + a fresh dispatch "
                      "(--supersedes %s) if this really is new work"
                      % (new_tip[:12], old_tip[:12], row["id"][:12]))
    if old_seq == new_seq:
        return "verified", None
    return None, ("retip REFUSED: %s is not the same work as %s — the "
                  "per-commit patch-id sequence differs (%d commit(s) -> %d). "
                  "retip moves an obligation to a NEW BASE, never to different "
                  "code; the recipient's brief was written about the old "
                  "sequence. Nothing was changed. Use cancel + a fresh "
                  "dispatch (--supersedes %s) if this really is new work"
                  % (new_tip[:12], old_tip[:12], len(old_seq), len(new_seq),
                     row["id"][:12]))


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
    never read — the exact thing @codex refused a countersign over ("a DM and
    a 3/3 range-diff do not retarget that verdict"). An OPEN row has no
    verdict by construction, so the status check IS the verdict check. The
    replay arm enforces the same gate, so even a hand-appended retip event
    after a verdict is inert.

    EVERY REFUSAL HAPPENS BEFORE THE ONE WRITE — @codex's fix on the first
    build, made structural: that build's first cut cancelled FIRST and
    validated nothing, so a made-up --ref cancelled a live obligation and
    then failed to replace it (done to @codex-3's open review on 2026-08-02
    while probing the verb). With a single event there is nothing to strand:
    a refusal writes nothing, and an append failure changes nothing.

    COMPOSES WITH THE DUPLICATE-SUCCESSOR LAW rather than tripping it: a row
    that already has a LIVE successor superseding it is not retipped — its
    obligation has demonstrably moved to the child, and re-pointing the parent
    would stand up a second live frontier for the same work, the precise
    duplicate `_duplicate_mint_warning` exists to refuse. A CLOSED successor
    does not block: the continuation ended and the still-open parent is again
    the one frontier. And an UNREADABLE frontier refuses the same way (codex
    P1, round 2): a not-closed row whose supersedes replays CHAIN_UNKNOWN
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
        # THE WITNESS GUARD (codex round 3): a hop must anchor to the tip it
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
        # IDEMPOTENT ON THE EXACT RETRY (codex FIX on this verb's first cut,
        # P2), the same
        # law mark_cancel and the duplicate-send guard already keep: a
        # committed write whose RESPONSE was lost gets re-run, and the re-run
        # must reconcile onto the achieved state, not refuse it. The row's own
        # last hop is the receipt — same tip AND same reason is this exact
        # operation already applied, returned without another append (and
        # without re-notifying: never-send-again). AND RECONCILIATION RUNS
        # BEFORE THE OPEN-ONLY GATE (codex P2, round 2): a retry is DELAYED
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
    identity, why = _retip_identity(row, new_tip)
    if why:
        return None, why
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — retip NOT recorded" % path
        current, unavailable = snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        live = current.get(row["id"])
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
        event = {"v": 3, "event": "retip", "seq": live["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(), "tip": new_tip,
                 "ref": ref, "old_tip": live["tip"], "reason": reason,
                 "identity": identity}
        # NO `proof` FIELD, deliberately (codex round 3): the round-2 stamp
        # was an unkeyed content hash a forger recomputes over their own
        # fields, and the fold now refuses to read one — the writer stamps
        # nothing the acceptance path is pinned to ignore.
        if not eventledger.append_unlocked(path, event):
            return None, "ledger unwritable (%s) — retip NOT recorded" % path
    out = dict(live)
    hops = list(live.get("retips") or ())
    hops.append({"old_tip": live.get("tip"), "old_ref": live.get("ref"),
                 "tip": new_tip, "ts": event["ts"], "reason": reason,
                 "identity": identity})
    out.update(tip=new_tip, ref=ref, retips=hops, seq=event["seq"],
               identity=identity)
    pk.event("dispatch-retip", row["id"],
             "%s -> %s [%s]" % (event["old_tip"][:12], new_tip[:12], identity))
    if notify:
        _notify_public(out, "RETIPPED %s -> %s [identity %s]: %s"
                       % (event["old_tip"][:12], new_tip[:12], identity,
                          reason))
    return out, None


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


def open_rows():
    snap = rows()
    out = owed(snap)
    return sorted(out, key=lambda r: (str(r.get("ts") or ""), r["id"]))


WORKING = "working"          # the recipient holds a live claim on this work
IDLE = "idle"                # readable claims, none of them this row's
PROGRESS_UNKNOWN = "unknown"  # the claims ledger could not be read


def _repo_project(repo_id):
    """Canonical project token from the current writer's standard Git identity.

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
    return os.path.basename(os.path.dirname(repo_id)) or None


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
      worktree:<proj>:<lane> — the recipient holds the dispatched lane

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
        if rid and res == "dispatch:" + rid[:8]:
            return WORKING, "recipient holds a claim on this row (%s)" % res
        if lane_resource and res == lane_resource:
            return WORKING, "recipient holds the dispatched lane (%s)" % res
        if res.startswith("worktree:") and project and stem_lane:
            # The claim's lane spelling and the row's need not match byte-for-
            # byte (#142: lane/-prefixed vs bare, round suffixes) — same
            # project, same lane FAMILY is the same dispatched lane.
            parts = res.split(":", 2)
            if len(parts) == 3 and parts[1] == project \
                    and _lane_stem(parts[2]) == stem_lane:
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
    fix carry sender="claude" — the bare family floor every seat
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
    sender = str(row.get("sender") or "").strip()
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


def stop_candidate(seat=None):
    """The rows THIS seat is answerable for. `seat=None` keeps the old
    fleet-wide behaviour for callers that have no identity to offer.

    Scoping was always the intent — `seats._dispatch_candidate` documents this
    rung as "work YOU handed to another seat and have not checked on" — but the
    filter was never written, so every stopping seat was offered the same
    globally-oldest row. Measured 2026-07-29: three different seats each spent a
    turn on ONE dispatch that belonged to none of them, and this seat was
    offered kimi's row, twice offered rows whose sender it could not prove.
    """
    current, unavailable = snapshot()
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


# NOT BRACKETED on send or add — codex-3's P2. `cmd_dispatch` REQUIRES --kind
# on both, and usage that renders a required flag as optional teaches the exact
# omission the requirement exists to prevent.
USAGE = ("usage: helm dispatch send <recipient> <lane> <message...> --ref TIP "
         "--kind build|review --new-work|--supersedes ID "
         "(or OMIT the message and pipe the body on stdin, or use a "
         "QUOTED-delimiter heredoc like <<'EOF' — argv bodies and UNQUOTED "
         "heredocs substitute backticks and $() before helm ever sees them) "
         "[--key K] [--note N] [--deadline SECONDS] [--repo PATH] [--force] | add "
         "<recipient> <lane> --ref TIP --kind build|review "
         "--new-work|--supersedes ID [--note N] "
         "[--deadline SECONDS] [--repo PATH] [--force] "
         "(the lane is a LABEL; --new-work / --supersedes is WORK IDENTITY and "
         "exactly one is REQUIRED, because a renamed continuation is invisible "
         "to any same-lane rule) | verdict <id-or-unique-prefix> "
         "<full-reviewed-tip> --approve|--fix|--supersede "
         "--measured|--inferred|--unverified <evidence> "
         "(polarity is REQUIRED: an omitted flag records immutable UNDECLARED; "
         "28 of 314 historical verdicts have that shape. --approve also "
         "requires evidence containing a verified gate:<token>, because an "
         "ungated approve is immutable and can never authorize landing) | "
         "cancel <id-or-unique-prefix> "
         "<reason...> | mark-delivered <id-or-unique-prefix> <delivery-ref> "
         "(update delivery_ref after a send -- retry evidence, changed "
         "mechanism, manual confirmation; idempotent for the same ref, "
         "a different ref appends a new delivered event with a warning) | "
         "hold <id-or-unique-prefix> <reason...> "
         "(acknowledge but gate on external dependency; release back to open) | "
         "release <id-or-unique-prefix> (return a HELD row to OPEN) | "
         "rebind <id-or-unique-prefix> --to <seat> [--force] "
         "[--reason R] [--repo PATH] [--json] "
         "(move an OPEN row to a new recipient, one operation; REFUSED unless "
         "proxywatch measures starvation/hang or autocompact proves fresh "
         "context exhaustion; either arm suffices, else --force needs a reason) | "
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
         "list [--open|--overdue|--held] [--json] | triage [ID...] | "
         "mix [--hours N] [--sender SEAT] [--json]")


def _rc(why):
    """Exit code for one refusal from the writer. 2 = USAGE, 1 = the operation
    failed.

    The RULE has one owner — `_resolve_chain` — and this maps its two USAGE
    refusals to the usage exit code by EXACT IDENTITY against the exported
    constants, never by sniffing the prose. Re-checking the flags here would be
    a second rejecter of the same input, which measures neither.

    Missing `--kind` already exits 2, so a missing work identity exiting 1 would
    make one required flag rc2 and the other rc1 for the same class of mistake.
    """
    return 2 if why in (CHAIN_REQUIRED, CHAIN_EXCLUSIVE) else 1


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
    (codex meld e:1785584307: a second partial parser drifts; the grammar
    has one home). Returns (ok, pos, opts, perr); the CLI arm consumes this
    same tuple, so census and parser cannot disagree."""
    rest = [a for a in argv if a not in ("--force", "--json")]
    pos, opts, perr = _parse(rest, ("--to", "--reason", "--repo"))
    ok = not perr and len(pos or ()) == 1 and bool((opts or {}).get("--to"))
    return ok, pos, opts, perr


def retip_argv_ok(argv):
    """The retip arm's EXACT admission, one home for the grammar, for the same
    reason rebind's is (the codex meld rebind_argv_ok cites): a second partial
    grammar beside the parser drifts, and the one that drifts certifies shapes
    the parser refuses. Returns (ok, pos, opts, perr); the CLI arm consumes
    this same tuple."""
    rest = [a for a in argv if a not in ("--json",)]
    pos, opts, perr = _parse(rest, ("--ref", "--reason", "--repo"))
    ok = not perr and len(pos or ()) == 1 and bool((opts or {}).get("--ref"))
    return ok, pos, opts, perr


def _parse_send(rest, names):
    """Parse send flags without consuming an exact ``--force`` in prose.

    Value options keep `_parse`'s historical anywhere-in-argv grammar, and
    unknown options still refuse. Only ``--force`` is ambiguous with message
    prose: it becomes authority when it belongs to the complete trailing option
    block, while an earlier occurrence stays positional text. ``--new-work``
    keeps its pre-existing bare-flag behaviour.
    """
    bare = {"--new-work", "--force"}
    cut = len(rest)
    while cut:
        if rest[cut - 1] in bare:
            cut -= 1
            continue
        if cut >= 2 and rest[cut - 2] in names \
                and not rest[cut - 1].startswith("--"):
            cut -= 2
            continue
        break
    force_at = {i for i in range(cut, len(rest)) if rest[i] == "--force"}
    flags = {"--new-work"} if "--new-work" in rest else set()
    if force_at:
        flags.add("--force")
    parsed = [a for i, a in enumerate(rest)
              if a != "--new-work" and i not in force_at]
    pos, opts, err = _parse(parsed, names, positional_flags=("--force",))
    return pos, opts, flags, err


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
        return base + " / ATTEST UNVERIFIABLE"
    return base


def _chain_note(row):
    """SAY WHAT WORK THIS ROW JOINED, at the moment it is minted.

    The writer is the only person who can catch a wrong `--supersedes`, and they
    can only catch it if the surface tells them which chain they just extended.
    A relation nobody is shown is a relation nobody corrects."""
    root = str(row.get("chain_root") or "")[:12] or "-"
    parent = str(row.get("supersedes") or "")[:12]
    if parent:
        return "chain %s — CONTINUES %s" % (root, parent)
    return "chain %s — NEW WORK (roots its own chain)" % root


def _fmt(row, now, late=False, unverifiable=()):
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
    return " %s %s  %-16s %-24s %-36s %3dm/%dm%s  %s" % (
        stamp, row["id"], _recipient_label(row), row["lane"],
        _label(row, unverifiable),
        age, deadline, suffix, str(row.get("tip") or row.get("ref") or "-")[:12])


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
    # that with a claim codex-3 refuted: it said the projection "does not carry
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
        # indented header followed by a flush-left wall — caught by @codex-3 in
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
# serialized review rounds on one lane, until the owner asked "codex round 6?
# couldn't have been fixed with a meld?" — after which ONE meld exchange closed
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
# review dispatches from `claude` at the SAME tip, 38 seconds
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
# (opus-integrator -> codex, 7 tips in 1h58m) and
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
# codex-3 approved it and it LANDED before the guard ever spoke. The
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


def review_spiral(sender, hours=SPIRAL_WINDOW_H, now=None):
    """(info | None, err) — the worst review spiral `sender` is running: the
    CHAIN where THEY have review-dispatched the most DISTINCT tips inside the
    window, once that count reaches SPIRAL_MELD_ROUNDS.

    info = {chain, lane, rounds, peer, recipients, span_h}. `peer` is the
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
    unproven is never absence, and the caller must not block on the err."""
    import calendar
    import time
    if not sender:
        return None, None
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    current, unavailable = snapshot()
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
    settled = {}                # lane -> newest terminal-verdict timestamp
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
        # rounds from opus-integrator, gemini takes the lane over and APPROVEs
        # at 07:25, and the guard blocked a seat whose lane had been finished
        # for an hour. Rows with no recorded sender still settle, which is the
        # fail-open direction here — an unattributable approve is still an
        # approve, and reading it SUPPRESSES a warning rather than raising one.
        if str(r.get("sender") or "").casefold() != want:
            continue
        key = chain or ("lane:" + _lane_stem(lane))
        b = chains.setdefault(key, {"tips": {}, "last": None, "who": set()})
        b["tips"].setdefault(tip, when)
        b["who"].add(peer)
        if b["last"] is None or when >= b["last"][0]:
            b["last"] = (when, peer, lane,
                         str(r.get("polarity") or "").casefold())
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
        rank = (rounds, b["last"][0])       # worst chain; newest breaks the tie
        if best is None or rank > best[0]:
            best = (rank, {"chain": key, "lane": b["last"][2], "rounds": rounds,
                           "peer": b["last"][1],
                           "recipients": sorted(b["who"]),
                           "span_h": (b["last"][0] - min(b["tips"].values()))
                           / 3600.0})
    if best is None or best[1]["rounds"] < SPIRAL_MELD_ROUNDS:
        return None, None
    return best[1], None


def _approval_identity_family_evidence(recipient):
    """Resolve one actor from verified native runtime or measured proxy route.

    This is THE family-of(actor) resolver for approval tiers, contrary-family
    evidence, and cross-family close gates. Native authority is a verified roster
    runtime explicitly stamped backend=native. Proxy authority is separate:
    roster session -> exact live pid -> /proc model/base URL -> exact listener and
    loaded config digest -> unique alias/provider/upstream route -> successful
    authenticated proxywatch canary. Seat names, labels, harness/type names,
    unverified roster family, proof storage keys, and recorded family strings
    contribute zero.

    New native decisions emit v4; measured proxy decisions emit v3. Historical
    v1/v2 close proofs replay exactly as recorded and are never reinterpreted.
    """
    from . import proxywatch, seats
    roster, failed = seats.roster_checked()
    if failed:
        return None, None, None, "roster runtime record is unreadable"
    canonical, err = seats._resolve_against(recipient, roster)
    if err:
        return None, None, None, err
    matches = [(name, row) for name, row in roster.items()
               if seats.recipient_matches(name, canonical)]
    if len(matches) != 1 or not isinstance(matches[0][1], dict):
        return None, None, None, ("no unique canonical roster runtime record for "
                                  "@%s" % canonical)
    roster_identity, row = matches[0]
    runtime = row.get("runtime")
    metadata, rejected = seats._runtime_metadata(runtime)
    native = isinstance(runtime, dict) and bool(runtime) and not rejected \
        and metadata == runtime and row.get("runtime_verified") is True \
        and runtime.get("backend") in (None, "native")
    family = runtime.get("family") if native else None
    if native and isinstance(family, str) and _TOKEN.fullmatch(family):
        evidence = {"v": 4, "identity": recipient,
                    "roster_identity": roster_identity,
                    "runtime": dict(runtime), "runtime_verified": True}
        return {family}, evidence, _subsumed_family_anchor(evidence), None

    session = row.get("session")
    if not isinstance(session, str) or not session:
        return None, None, None, ("no verified native runtime and @%s has no exact "
                                  "roster session for proxywatch proof" % canonical)
    family, proof, why = proxywatch.proxy_runtime_snapshot(session)
    if why:
        return None, None, None, ("no verified native runtime for @%s; %s" %
                                  (canonical, why))
    if not isinstance(family, str) or not _TOKEN.fullmatch(family) or not proof:
        return None, None, None, ("measured proxy runtime family is malformed for "
                                  "@%s" % canonical)
    evidence = {"v": 3, "identity": recipient,
                "roster_identity": roster_identity, "session": session,
                "proxy_proof": proof}
    return {family}, evidence, _subsumed_family_anchor(evidence), None


def _approval_identity_families(recipient):
    """Explicit family evidence for one canonical seat, or why it is unknown."""
    families, _evidence, _anchor, err = \
        _approval_identity_family_evidence(recipient)
    return families, err


def approval_tier(recipient, repo=None):
    """(state, message) for a review recipient. THREE outcomes, not two.

      "none"     no approval-tier policy is configured. There is no tier, so
                 there is nothing to be outside of — this is a real answer.
      "ok"       the recipient is inside the tier.
      "outside"  the recipient is DEFINITIVELY outside it.
      "unknown"  a policy exists and could not be evaluated — unreadable,
                 malformed selector, no family evidence, conflicting families.

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

    THE ANSWER IS ONE PER (recipient, repo) PER PROJECTION, and only inside a
    `projscope.scope()` — every write path (the land door, the close ladders,
    the send advisory) opens no scope, so every one of them still resolves the
    tier live, exactly as before. This is here because resolving a tier is not
    a lookup: it walks every fd in /proc to find the seat's proxy listener and
    fires an authenticated canary at it. MEASURED 2026-08-06 on the live
    ledger, one `helm lr list --all`: 627 calls, SIXTEEN distinct keys, 143 of
    the 226 wall seconds. The read is per SEAT and the board has nine of them;
    it was being re-measured once per ROW."""
    from . import projscope
    return projscope.memo(("dispatches.approval_tier", recipient, repo),
                          lambda: _approval_tier_uncached(recipient, repo))


def _approval_tier_uncached(recipient, repo=None):
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
        return "unknown", ("approval-tier check unavailable: %s" % why)
    canonical, err = seats.resolve_recipient(recipient)
    if err:
        return "unknown", ("approval-tier check unavailable: %s" % err)
    reason = str(policy.get("policy_reason") or "").strip()
    if not reason:
        return "unknown", ("approval-tier check unavailable: policy %s has "
                           "no policy_reason" % policy["id"])
    exact, family_selectors, selectors = set(), set(), []
    for raw in policy.get("policy_members") or []:
        selector = str(raw or "").strip()
        head, sep, token = selector.partition(":")
        if sep != ":" or head not in ("seat", "family") \
                or not _TOKEN.fullmatch(token):
            return "unknown", ("approval-tier check unavailable: policy %s "
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
        families, why = _approval_identity_families(canonical)
        if why:
            return "unknown", ("approval-tier check unavailable for @%s: %s"
                               % (canonical, why))
        if len(families) > 1:
            return "unknown", ("approval-tier check unavailable for @%s: "
                               "conflicting explicit families %s"
                               % (canonical, ", ".join(sorted(families))))
        if not families:
            return "unknown", ("approval-tier check unavailable for @%s: no "
                               "verified resolved runtime family evidence "
                               "(labels and seat names are never authority)"
                               % canonical)
        if next(iter(families)) in family_selectors:
            return "ok", None
    valid = ", ".join(sorted(set(selectors)))
    return "outside", ("@%s is outside the current approval tier; reason: %s; "
                       "source prior: %s; valid set: %s"
                       % (canonical, reason, policy["id"], valid))


def _approval_tier_advisory(recipient):
    """The SEND-PATH wording, unchanged. Advice only: it never blocks a
    dispatch, and the land gate reads `approval_tier` directly instead."""
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
       usually asking for the past. Live: a build sent to kimi with --ref at
       kimi's OWN commit, already on main as a cherry-pick, so the row asked the
       recipient to gate its own finished work. It surfaced only because kimi
       refused it instead of complying.

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
        if kind == "review" and anc != vcs.ANCESTOR:
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
    # (#142 round 2, codex finding 5): collapsing to the family stem probed
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
        would be disabled on exactly the population it exists for (@codex,
        2026-08-03 — I proposed that bypass and it was refused, correctly).
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


def cmd_dispatch(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "mix":
        return cmd_mix(rest)
    if verb in ("add", "send"):
        names = {"--ref", "--note", "--deadline", "--repo", "--kind",
                 "--supersedes"}
        if verb == "send":
            names.add("--key")
        # `send` owns a prose tail. An exact `--force` inside that prose must
        # not silently become authority to mint a fork, so only its complete
        # trailing option block is parsed as flags. `add` has no prose and can
        # remove bare flags directly before the value-option parser.
        if verb == "send":
            pos, opts, flags, err = _parse_send(rest, names)
            new_work = "--new-work" in flags
            force = "--force" in flags
        else:
            new_work = "--new-work" in rest
            force = "--force" in rest
            rest = [a for a in rest if a not in ("--new-work", "--force")]
            pos, opts, err = _parse(rest, names)
        # STDIN BODY — literal when piped or fed by a quoted-delimiter heredoc,
        # times in one evening across two different model families.
        #
        # A dispatch body is PROSE, and prose contains backticks. Passed as argv
        # inside a double-quoted shell string, bash performs COMMAND SUBSTITUTION
        # on them: a message explaining `kind` silently became a message with
        # `kind` executed and its (empty) output spliced in, plus a stray
        # "kind: command not found" on stderr. Observed on four of my own
        # dispatches and once on codex-3's, whose reply carried its own
        # "disregard its mangled command examples".
        #
        # It is not only mangling — it is arbitrary command execution driven by
        # the CONTENT of a message. `helm chat post` already solved this exactly
        # here, with a stdin path; the asymmetry was that `dispatch send` never
        # grew the same door. A QUOTED heredoc delimiter costs the caller nothing
        # and cannot expand; an unquoted one still performs shell substitution.
        # `not err` FIRST: _parse returns pos=None on an unknown option or a
        # value-less one, so len(pos) tracebacks before the error check two lines
        # below ever runs. I inserted this branch ABOVE that check and turned
        # three clean usage errors into a TypeError — `--bogus`, a value-less
        # `--ref`, and a literal `--` all crashed. Found by codex-3 with live
        # probes; the ordering was the whole defect.
        if verb == "send" and not err and pos and len(pos) == 2 \
                and not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                pos = list(pos) + [piped]
        if err or len(pos) < (3 if verb == "send" else 2):
            print("helm dispatch: " + (err or USAGE), file=sys.stderr)
            return 2
        if not opts.get("--ref"):
            print("helm dispatch: %s requires --ref TIP" % verb, file=sys.stderr)
            return 2
        # An ABSENT --deadline stays None all the way to `_base`, which is the
        # only place that knows the row's KIND. Substituting the review default
        # here is what put 2700s on every build row ever dispatched from the
        # CLI: the flag was optional, so the omission was silent and universal.
        deadline = opts.get("--deadline")
        if deadline is not None:
            deadline, err = _deadline(deadline)
            if err:
                print("helm dispatch: " + err, file=sys.stderr)
                return 2
        kind, err = clean_kind(opts.get("--kind"))
        if err:
            print("helm dispatch: " + err, file=sys.stderr)
            return 2
        # REQUIRED AT THE OWNER LAYER, optional in the library — and the split
        # is the whole point. `clean_kind(None)` must stay legal because every
        # row written before this field existed carries no kind, and the report
        # has to show those as UNKNOWN rather than invent one.
        #
        # But an OPTIONAL flag makes the alarm VACUOUS, which is the finding:
        # the inverted night this feature exists to catch reproduces exactly by
        # omitting --kind, and `mix_alarm` stays deliberately silent on an
        # all-UNKNOWN window. A guard that any caller disarms by not typing a
        # flag is not a guard. New rows must declare; history stays honest.
        if kind is None:
            print("helm dispatch: %s requires --kind build|review — an "
                  "unrecorded kind makes the capacity alarm silent, which is "
                  "the exact failure it exists to catch" % verb,
                  file=sys.stderr)
            return 2
        if verb == "send" and kind == "review":
            advisory = _approval_tier_advisory(pos[0])
            if advisory:
                print("helm dispatch: " + advisory, file=sys.stderr)
        for w in _ref_sanity(opts["--ref"], pos[1], opts.get("--repo"),
                             kind=kind):
            print("helm dispatch: " + w, file=sys.stderr)
        cap, unroutable = _recipient_gate(pos[0], force, verb)
        if unroutable:
            print("helm dispatch: " + unroutable, file=sys.stderr)
            return 2
        target, target_err = _canonical_recipient(cap["canonical"])
        if target_err:
            print("helm dispatch: " + target_err, file=sys.stderr)
            return 1
        if verb == "send":
            # THE CANONICAL FROM THE CAPABILITY, never raw argv. The gate
            # proved membership for cap["canonical"]; handing the writer
            # pos[0] made it RESOLVE AGAIN, so the row could store a key
            # the proof was never about — #116's own unowned-row class,
            # recreated inside #116's fix. One resolution, one identity.
            row, why, sent = send(target, pos[1], " ".join(pos[2:]),
                                  opts["--ref"], note=opts.get("--note"),
                                  deadline_s=deadline, key=opts.get("--key"),
                                  repo=opts.get("--repo"), kind=kind,
                                  new_work=new_work,
                                  supersedes=opts.get("--supersedes"),
                                  force=force)
            if row is None:
                print("helm dispatch: " + why, file=sys.stderr)
                return _rc(why)
            for warning in row.get(_WRITE_WARNINGS, ()):
                print("helm dispatch: WARNING: " + warning, file=sys.stderr)
            if why:
                print("helm dispatch: " + why, file=sys.stderr)
                return 1
            # KEYED ON THE MEMBERSHIP ENUM, never on truthiness. This comment
            # used to describe a `joined is True` boolean-or-None and outlived
            # it — the exact stale-comment hazard this lane has been punished
            # for twice, so it is rewritten rather than left to read as
            # authoritative. UNKNOWN is its own arm and can never fall through
            # to a delivery claim.
            #
            # SAYS "NO RECIPIENT", NOT "NOT DELIVERED", AND THE DIFFERENCE IS
            # DELIBERATE. The ledger's own `delivered` event means the mention
            # or DM row was PUBLISHED — add()'s path states it outright: "THE
            # MENTION IS THIS PATH'S DELIVERY". A forced send to a pre-join
            # address genuinely does publish, so it genuinely does mark
            # delivered, and a line here reading NOT DELIVERED would contradict
            # `dispatch list` using the ledger's own vocabulary for a different
            # question. This line answers the question we actually measured —
            # whether any seat holds that address — and leaves "was it
            # published" to the field that owns it.
            # THREE ARMS FOR A THREE-STATE VALUE. This was `is False` / else,
            # which is a TWO-way branch — so joined=None fell to the else and
            # printed "(delivery observed)", the exact thing the comment above
            # forbids. The comment was right and the code below it was wrong,
            # which is the worst combination: a reader checks the invariant,
            # finds it stated correctly, and stops looking. Enumerate the arms
            # so a tri-state cannot collapse into a binary again.
            if cap["membership"] == "JOINED":
                tail = " (delivery observed)" if sent else ""
            elif cap["membership"] == "ABSENT":
                tail = " (NO RECIPIENT — @%s holds no roster row)" % _recipient_label(row)
            else:
                tail = (" (recipient membership UNKNOWN — %s, so delivery is"
                        " unproven)"
                        % ("the roster could not be read"
                           if cap["evidence"] == "read-failed"
                           else "no seat has joined this box yet"))
            print("helm dispatch: %s @%s %s — %s%s" % (
                row["id"], _recipient_label(row), row["lane"],
                _label(row), tail))
            print("helm dispatch: %s" % _chain_note(row))
            return 0
        # `kind=kind` — ABSENT HERE UNTIL NOW, and it is the flag's whole point.
        # The parser accepted --kind on `add`, clean_kind VALIDATED it, and this
        # call then dropped it on the floor: every `dispatch add` recorded
        # UNKNOWN no matter what the operator typed, with rc 0 and no warning.
        # `send` two branches up always passed it, so the field looked wired.
        row, why = add(
            # THE CANONICAL, not raw argv — same reason as send: the gate's
            # membership proof is about cap["canonical"], so that is the only
            # identity the row may store.
            target, pos[1], opts["--ref"], note=opts.get("--note"),
            deadline_s=deadline, repo=opts.get("--repo"), kind=kind,
            new_work=new_work, supersedes=opts.get("--supersedes"),
            force=force, _reason=True)
        if row is None:
            print("helm dispatch: " + (why or "dispatch NOT recorded"),
                  file=sys.stderr)
            return _rc(why)
        for warning in row.get(_WRITE_WARNINGS, ()):
            print("helm dispatch: WARNING: " + warning, file=sys.stderr)
        # SAY THAT NOBODY WAS TOLD. `add` deliberately does not notify — that is
        # its whole difference from `send`, and it is the right primitive for an
        # obligation the recipient already agreed to out of band. But the line
        # it printed, "PENDING VERDICT / NEEDS CONFIRMATION", reads as a STATUS
        # the ledger will resolve on its own, so the writer walks away believing
        # the hand-off happened.
        #
        # LIVE 2026-07-27, by me, one hour after I called the same defect on
        # another lane: I minted a re-gate row for @codex with `add`, told the
        # room it was minted, and @helm-claude-3 had to go read codex's pane to
        # discover it was never in codex's task list. An obligation exists that
        # its holder does not know about — which is the whole bug class the lr
        # delivery leg is open on ("add() does not notify"), in this file.
        #
        # The fix is NOT to make `add` send; that would delete the primitive.
        # It is to make the silence LOUD at the surface, so the next line the
        # writer reads tells them what they still owe.
        shown = _recipient_label(row)
        print("helm dispatch: %s -> @%s %s — PENDING VERDICT / NEEDS CONFIRMATION"
              % (row["id"], shown, row["lane"]))
        print("helm dispatch: %s" % _chain_note(row))
        # THREE OUTCOMES, THREE MESSAGES — because THIS MERGE makes `add` notify.
        #
        # An earlier fix ("dispatch: `add` mints an obligation and tells NOBODY —
        # say so out loud") landed an unconditional "add records the
        # obligation but sends NOTHING" warning, which was exactly true
        # against a main where add()
        # never notified. ds4pro's lane gives add() a public @mention, so that
        # same line became a LIE the moment these two met: the recipient WAS
        # told and the CLI said they were not. Fixed IN the merge commit rather
        # than after it, so main is never once in a state where the code
        # notifies and its own surface denies it.
        #
        # The outcome is read from the DURABLE marker, not a return value.
        # _notify_public computes post_ok and add() discards it; threading it
        # out would make the truth a transient. _record_notify_failed already
        # writes a notify-failed event and _notify_failed_for reads it back, so
        # the ledger is the canonical answer — which is also what
        # docs/DISPATCH-ADD-CONTRACT.md (landing in this same merge) tells a
        # caller to do.
        #
        # notify=False keeps the original warning verbatim: there it is true.
        nf = _notify_failed_for(row["id"])
        if nf:
            print("helm dispatch: the mention FAILED (%s) — @%s has NOT been "
                  "told, though the obligation is recorded. `helm chat post` an "
                  "@%s mention naming %s."
                  % (str(nf.get("reason") or "unknown")[:120], shown,
                     shown, row["id"][:12]), file=sys.stderr)
        elif cap["membership"] == "JOINED":
            print("helm dispatch: @%s was mentioned in #main — their beacon "
                  "picks it up on next wake. Delivery is unconfirmed until they "
                  "read it, which is what NEEDS CONFIRMATION means."
                  % shown)
        else:
            # THE PICKUP PROMISE IS FALSE FOR A SEAT THAT HAS NOT JOINED, and
            # falsely REASSURING, which is worse than silence. A joining seat
            # BASELINES the public rooms at EOF — only its DM lane starts at 0
            # — so a mention posted before it joins is skipped forever, not
            # queued. `is True` and not truthiness: an unreadable roster (None)
            # cannot promise a pickup it did not establish either.
            if cap["membership"] == "ABSENT":
                # KNOWN ABSENT: the roster is readable, non-empty, and lacks
                # them. The skip is a fact, so state it as one.
                print("helm dispatch: @%s was mentioned in #main, but they "
                      "hold no roster row at assignment — a seat that joins "
                      "LATER baselines public rooms at EOF, so a public "
                      "mention is not a durable pre-join path. DM them, which "
                      "does start at 0, or re-send once they join."
                      % shown)
            else:
                # UNKNOWN. The previous wording said the mention WILL be
                # SKIPPED, which is the OPPOSITE CERTAINTY — unknown
                # membership means they may already be joined and take it
                # normally. Withhold the promise; never invent its negation.
                print("helm dispatch: @%s was mentioned in #main, but %s, so "
                      "PICKUP IS UNPROVEN either way — already joined and "
                      "their beacon takes it; joining later and public rooms "
                      "baseline at EOF, so it is missed. Confirm at the "
                      "recipient, or DM them, which starts at 0."
                      % (shown,
                         "the roster could not be read"
                         if cap["evidence"] == "read-failed"
                         else "no seat has joined this box yet"))
        return 0
    if verb == "verdict":
        # Polarity is a FLAG, not a positional, so it can never be swallowed by
        # the free-text evidence tail. Omitting it is REFUSED for a new write —
        # see the measured rationale at the required-check below. (This comment
        # used to say omitting it "stays legal"; that was true before 2026-07-26
        # and contradicted the check twelve lines down, which is exactly the
        # doc-drift class codex-3 has been filing against this file.)
        # FLAGS ARE POSITIONAL — THEY PRECEDE THE EVIDENCE, AND THE EVIDENCE
        # IS WHATEVER REMAINS, VERBATIM (@offbox-claude, blast-radius lens).
        #
        # The first cut scanned the WHOLE argv for anything starting with
        # "--", which reaches into the free-text evidence tail. Measured: a
        # reviewer who FORGETS the flag and writes "I could not run it so this
        # is --unverified at best" got a basis MINTED FROM THEIR PROSE — a
        # confidence claim they never made — and their evidence MUTILATED to
        # "I could not run it so this is at best". Both halves are worse than
        # the refusal the flag exists to produce.
        #
        # AND I ARGUED THE EXACT OPPOSITE when I chose a bare flag over
        # `--basis X`: I said a valued flag "would have to be pulled out
        # before the free-text evidence tail is joined, which is exactly how a
        # value gets swallowed into prose". The direction was backwards. A
        # positionless scan does not let prose swallow a value; it lets a
        # value be swallowed FROM prose.
        #
        # THE POLARITY HALF HAD THE SAME BUG AND IT PREDATES THIS LANE —
        # "...this is a --fix at best" in evidence would mint a polarity the
        # same way. One parse, one cure, because splitting it would leave the
        # older half of the same defect standing.
        flags, tail = partition_verdict_flags(rest)
        rest = rest[:2] + tail
        # BASIS IS A BARE FLAG, THE SAME SHAPE AS POLARITY, not `--basis X`.
        # Every flag on this verb is already a bare word matched against a
        # closed vocabulary, and a valued flag here would have to be pulled
        # out of `rest` BEFORE the free-text evidence tail is joined — which
        # is exactly how a value gets swallowed into prose. Same shape, same
        # parser, nothing new to get wrong.
        bas = [a for a in flags if a in BASIS_FLAGS]
        pol = [a for a in flags if a not in BASIS_FLAGS]
        if len(pol) > 1 or any(p not in ["--" + p2 for p2 in POLARITIES]
                               for p in pol):
            print("helm dispatch verdict: polarity is one of %s (at most one)"
                  % " ".join("--" + p for p in POLARITIES), file=sys.stderr)
            return 2
        if len(bas) > 1:
            print("helm dispatch verdict: basis is one of %s (at most one)"
                  % " ".join("--" + b for b in BASES), file=sys.stderr)
            return 2
        if len(rest) < 3:
            print(USAGE, file=sys.stderr)
            return 2
        # A NEW verdict must DECLARE its direction. Omitting the flag used to be
        # legal and recorded UNDECLARED — "the honest state, never an implied
        # approval" — which is right for REPLAY of rows written before polarity
        # existed, and wrong as a default for a fresh write nobody intends.
        #
        # Measured 2026-07-26: 28 of 77 verdicts (36%) are UNDECLARED, by SEVEN
        # different seats — codex, codex-3, codex-orch, ds4pro, gemini, grok,
        # kimi — and the integrator. Two arrived within the hour: gemini and
        # grok each wrote APPROVE in chat and recorded UNDECLARED in the ledger.
        # A verdict is immutable, so all 28 are permanently unclassifiable and
        # 10 land loops can never be stall-checked. When every agent omits a
        # flag, the flag is not optional — it is missing a requirement.
        #
        # Same split the `kind` field already landed on: REQUIRED at the CLI for
        # new writes, still accepted as None by mark_verdict() so historical
        # replay and projection are untouched.
        if not pol:
            print("helm dispatch verdict: DECLARE the polarity — %s.\n"
                  "  A verdict is a decision and it is IMMUTABLE: omitting the "
                  "flag records UNDECLARED, which fails closed (never read as "
                  "an approval) and can never be corrected.\n"
                  "  36%% of verdicts on this ledger are already UNDECLARED, "
                  "by every seat in the fleet."
                  % " ".join("--" + p for p in POLARITIES), file=sys.stderr)
            return 2
        # DECLARE HOW YOU KNOW. task/338, owner ask: "always making legible
        # how much doubt should go into a conversation". Captured as a premise
        # at certainty 1.00 and never delivered — 2.34% adoption DECAYING TO
        # ZERO, 221 verdicts unmarked BY CONSTRUCTION, and exactly 0% among
        # every non-Claude family. Prose is not model-family-proof; a refusing
        # verb is. Same split as polarity and kind: required for a NEW write,
        # permissive in mark_verdict so replay and projection are untouched.
        #
        # THE REFUSAL DEFINES THE THREE WORDS, because a reviewer meeting it
        # mid-verdict should not have to go read a premise to answer it.
        if not bas:
            print("helm dispatch verdict: DECLARE HOW YOU KNOW — %s.\n"
                  "  measured   = a tool ran and produced this finding\n"
                  "  inferred   = reasoned from code or output you read\n"
                  "  unverified = you have not checked\n"
                  "  A claim with no basis is UNVERIFIED, never a decorative "
                  "number. 2.34%% of verdicts on this ledger carry one, and "
                  "the rest are unmarked because nothing ever asked."
                  % " ".join("--" + b for b in BASES), file=sys.stderr)
            return 2
        row, why = mark_verdict(rest[0], rest[1], " ".join(rest[2:]),
                                polarity=pol[0][2:] if pol else None,
                                basis=bas[0][2:] if bas else None)
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        # THE MARKER IS SAID BACK. A basis nothing renders is a basis nobody
        # mints — the 2.34%-adoption lesson in one line.
        print("helm dispatch: %s — VERDICT (%s/%s) at %s" % (
            row["id"], (row.get("polarity") or "UNDECLARED").upper(),
            (row.get("basis") or "UNMARKED").upper(),
            row["tip"][:12]))
        print("helm dispatch: gate: %s" % gate_state(row))
        print("helm dispatch: attestation: %s" % row.get("announce", "n/a"))
        if row.get("polarity") == "approve":
            _verdict_land_nudge(row)
        return 0
    if verb == "retip":
        as_json = "--json" in rest
        ok, pos, opts, perr = retip_argv_ok(rest)
        if not ok:
            if perr:
                print("helm dispatch retip: " + perr, file=sys.stderr)
            print("usage: helm dispatch retip <id-or-unique-prefix> --ref "
                  "NEW_TIP --reason R [--repo PATH] [--json]", file=sys.stderr)
            return 2
        out, why = retip(pos[0], opts["--ref"], reason=opts.get("--reason"),
                         repo=opts.get("--repo"))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        hop = out["retips"][-1]
        if as_json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print("helm dispatch: %s RETIPPED %s -> %s [identity %s] — %s"
                  % (out["id"][:12], str(hop["old_tip"])[:12],
                     out["tip"][:12], out["identity"], hop["reason"]))
        return 0
    if verb == "rebind":
        # --force/--json are bare booleans; --to/--reason/--repo carry values
        force = "--force" in rest
        as_json = "--json" in rest
        ok, pos, opts, perr = rebind_argv_ok(rest)
        if not ok:
            if perr:
                print("helm dispatch rebind: " + perr, file=sys.stderr)
            print("usage: helm dispatch rebind <id-or-unique-prefix> --to "
                  "<seat> [--force] [--reason R] [--repo PATH] [--json]",
                  file=sys.stderr)
            return 2
        # THE SAME ROSTER DOOR AS send/add — rebind() calls resolve_recipient,
        # which NORMALISES and never checks membership, so without this a
        # rebind hands a LIVE obligation to an address nobody holds. That is
        # strictly worse than the send case it shares a class with: send
        # invents an unowned row, rebind takes work a seat is already carrying
        # and strands it. Measured before fixing — the move succeeded and helm
        # then advised "Re-brief @claude directly", naming a seat that does not
        # exist.
        #
        # DELIBERATELY NOT force-BYPASSED, unlike send. Here --force attests
        # that the recipient is STARVED, not that the caller knows the target
        # is pre-join; and since every manual rebind already carries --force to
        # clear the starvation gate, honouring it here would disable this door
        # on the exact path that needs it. A genuine pre-join handoff is
        # cancel + `send --force`, which states that intent explicitly.
        _cap, unroutable = _recipient_gate(opts["--to"], False, "rebind")
        if unroutable:
            print("helm dispatch rebind: " + unroutable, file=sys.stderr)
            return 2
        target, target_err = _canonical_recipient(_cap["canonical"])
        if target_err:
            print("helm dispatch rebind: " + target_err, file=sys.stderr)
            return 1
        # This typed TARGET is the identity the gate proved; rebind must not
        # mistake it for raw argv and resolve it into a different key.
        out, why = rebind(pos[0], target,
                          reason=opts.get("--reason"), force=force,
                          repo=opts.get("--repo"))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        if as_json:
            # The machine receipt for the automation caller rebind()'s own
            # docstring anticipates ("a verb that can later be CALLED
            # automatically"): the full {old,new,reason} move on stdout; the
            # re-brief NOTE below stays on stderr where it never corrupts a
            # JSON consumer.
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            print("helm dispatch: %s REBOUND to @%s -> %s — %s"
                  % (out["old"]["id"][:12], out["new"]["recipient"],
                     out["new"]["id"][:12], out["reason"]))
        # THE ROOM DOES NOT MOVE WITH THE OBLIGATION (#203). stderr in BOTH
        # branches: the JSON receipt already carries room_fence as data, and a
        # warn on stdout would corrupt the automation consumer this verb's own
        # docstring anticipates.
        for f in (out.get("room_fence") or []):
            left = f.get("remaining")
            print("helm dispatch: NOTE — @%s still holds the room %s%s. The "
                  "REBIND MOVED THE OBLIGATION, NOT THE LEASE, so the new "
                  "builder cannot claim it."
                  % (f.get("holder"), f.get("lane"),
                     "" if left is None else " for %ss more" % left),
                  file=sys.stderr)
            print("helm dispatch:   Fresh room: `helm work claim %s-r2` — "
                  "that unblocks in one line and needs no force-release, which "
                  "matters because a walled seat is not a dead one and its "
                  "room may hold real work."
                  % str(f.get("lane") or "").rsplit(":", 1)[-1],
                  file=sys.stderr)
            print("helm dispatch:   THEN EXPECT A LANE-LABEL/BRANCH "
                  "DIVERGENCE: the row keeps naming a branch that will never "
                  "contain the work, so `dispatch send` will warn `--ref is "
                  "NOT one of lane X's own commits` and every surface "
                  "resolving lane->branch reads the wrong one. Tell the "
                  "reviewer which branch the tip is really on.",
                  file=sys.stderr)
        # THE BRIEF DOES NOT TRAVEL, AND ONLY A CODE COMMENT SAID SO.
        # rebind() builds the new row with add(), never send(), because the
        # ledger stores no recoverable DM message body (send rows retain only a
        # hash; add rows had no DM) and inventing one would put words in the
        # original sender's mouth. That reasoning is right; the CONSEQUENCE was
        # invisible. The new recipient
        # IS notified — add() posts a public @mention, so the OBLIGATION
        # travels — but it arrives as a lane label and a ref with no brief, and
        # nothing prompted anyone to re-send one.
        #
        # MEASURED 2026-07-31: I rebound two rows off a dark family and BOTH
        # recipients came back asking for the brief. @codex-3 named the gap
        # itself: "the superseding row carries only lane + base... please
        # resend". The knowledge lived in a comment the operator never sees.
        print("helm dispatch: NOTE — no recoverable DM message body traveled "
              "with this rebind (send rows retain only a hash). Re-brief @%s "
              "directly, or they start without the original brief."
              % out["new"]["recipient"], file=sys.stderr)
        return 0
    if verb == "mark-delivered":
        if len(rest) < 2:
            print("usage: helm dispatch mark-delivered <id-or-unique-prefix> "
                  "<delivery-ref>  (update delivery_ref after a send -- retry "
                  "evidence, changed mechanism, manual confirmation)",
                  file=sys.stderr)
            return 2
        row, why = mark_delivered(rest[0], rest[1])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        warnings = row.get(_WRITE_WARNINGS, ())
        if warnings:
            print("helm dispatch: %s -- DELIVERY UPDATED" % row["id"])
            for w in warnings:
                print("helm dispatch: WARNING: " + w, file=sys.stderr)
        else:
            print("helm dispatch: %s -- DELIVERED (%s)" % (
                row["id"], row.get("delivery_ref") or "none"))
        return 0
    if verb == "cancel":
        if not rest:
            print("usage: helm dispatch cancel <id-or-unique-prefix> <reason...>  "
                  "(honestly abandon a stranded dispatch — recipient gone / "
                  "work moot; never a substitute for a real verdict)",
                  file=sys.stderr)
            return 2
        row, why = mark_cancel(rest[0], " ".join(rest[1:]))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — CANCELLED (%s)" % (
            row["id"], row.get("cancel_reason") or ""))
        return 0
    if verb == "hold":
        if not rest:
            print("usage: helm dispatch hold <id-or-unique-prefix> <reason...>  "
                  "(acknowledge an obligation gated on an external dependency)",
                  file=sys.stderr)
            return 2
        row, why = mark_hold(rest[0], " ".join(rest[1:]))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — HELD (%s)" % (
            row["id"], row.get("hold_reason") or ""))
        return 0
    if verb == "release":
        if not rest:
            print("usage: helm dispatch release <id-or-unique-prefix>  "
                  "(return a HELD row to OPEN -- the dependency is resolved)",
                  file=sys.stderr)
            return 2
        row, why = mark_release(rest[0])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — RELEASED to OPEN" % row["id"])
        return 0
    if verb == "triage":
        # ON-DEMAND re-measure, where the git cost is the point of the visit
        # (the list renderer deliberately dropped it: 0.60s/row against 0.25s
        # for the whole listing). Each open row's measurable claims are
        # re-checked against the tree checked out RIGHT NOW — file:line by
        # CONTENT against the filing-era tree, count claims against the live
        # ledger, cited shas by ancestry AND patch-identity — and the row
        # prints its verdict beside its id so a seat picking work up reads
        # the decay before it reads the prose.
        current, unavailable = snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; triage cannot run: %s"
                  % unavailable, file=sys.stderr)
            return 1
        from . import clearspan
        # TRIAGE IS ACTIONABLE WORK, so it reads the owed frontier: offering
        # both a superseded parent and its successor asks a human to
        # re-measure the same obligation twice.
        selected = owed(current)
        if rest:
            wanted = set(rest)
            selected = [r for r in selected
                        if r["id"] in wanted
                        or any(r["id"].startswith(w) for w in wanted)]
        if not selected and not rest:
            print("helm dispatch triage: no matching open rows")
        for row in sorted(selected, key=lambda r: str(r.get("ts") or "")):
            verdict, detail = clearspan.re_measure(row)
            print("%s %-7s %-12s %-28s %s" % (
                row["id"][:12], verdict.upper(), _recipient_label(row),
                str(row.get("lane") or "-")[:28], detail))
        # CURED FIXES RIDE TRIAGE, NOT A FLAG. These rows are CLOSED, so
        # `owed()` above cannot see them by construction — a FIX verdict means
        # the author owes, and the moment the author cures without
        # re-dispatching the row is finished work with no reader. Putting them
        # behind `--cured` would fix nothing: the defect is that NOBODY LOOKS,
        # and a flag you have to already suspect is a flag nobody types.
        # Printed AFTER the owed frontier because owed work is due now and
        # this is due to somebody else.
        cured, cure_err = cured_unwitnessed(current, ids=rest or None)
        if cure_err:
            print("helm dispatch triage: cured-fix scan UNAVAILABLE (%s) — "
                  "this says nothing about whether any exist" % cure_err,
                  file=sys.stderr)
        elif cured:
            print("\nCURE AWAITING REVIEW — %d row(s) whose author CURED and "
                  "never re-dispatched. Nobody is waiting on these; the board "
                  "reads them as owed BY the author." % len(cured))
            for row, (branch, tip, ahead) in cured:
                print("%s %-7s %-12s %-28s cure at %s +%d on %s — re-dispatch "
                      "or `helm dispatch rebind`" % (
                          row["id"][:12], "CURED", _recipient_label(row),
                          str(row.get("lane") or "-")[:28], tip[:12], ahead,
                          branch))
        return 0
    if verb == "list":
        # BOUND BEFORE THE READ, not after it. The instant must not POSTDATE
        # the rows it stamps: snapshot() used to run first and read_now was
        # taken below it, so a row that appeared between the two was absent
        # from a listing whose stamp said it was read later than that row
        # existed. The stamp then makes a stronger claim than the read
        # supports — an absence claim about a moment the read never saw.
        # (@codex, reviewing the cure that bound this instant in the first
        # place: the ORDER was the residual half.)
        read_now = time.time()
        current, unavailable = snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; obligations UNKNOWN: %s"
                  % unavailable, file=sys.stderr)
            return 1
        # NAME THE OFFENDING TOKEN, and keep the two rejections DISTINCT.
        # Both used to print the bare USAGE, so `list --limit 10` answered with
        # a 900-character wall that never contained the string "--limit" and the
        # reader had to diff their command against the whole grammar to find it.
        # `send` already names its unknown option; only this path did not.
        # The two causes are also different mistakes with different repairs —
        # a typo/absent flag versus two selectors that cannot both hold — so
        # collapsing them into one message loses the repair, not just the token.
        flags = set(rest)
        unknown = sorted(flags - {"--open", "--overdue", "--held", "--json"})
        if unknown:
            print("helm dispatch list: unknown option%s %s (accepts --open, "
                  "--overdue, --held, --json)\n%s"
                  % ("" if len(unknown) == 1 else "s", " ".join(unknown), USAGE),
                  file=sys.stderr)
            return 2
        if len(flags & {"--open", "--overdue", "--held"}) > 1:
            print("helm dispatch list: --open and --overdue select different "
                  "rows and cannot be combined — pass one, or neither for all "
                  "rows", file=sys.stderr)
            return 2
        # The owed frontier, computed ONCE with a single successor index. Every
        # selector below and the overdue LABELLING both read it, so no two
        # surfaces can disagree about who owes what — which is the whole point
        # of owed() and was exactly what the raw-row selectors defeated. Doing
        # it per row also rebuilt the index each time, which is the quadratic
        # shape over a 1,300-row ledger that owed() exists to remove.
        owed_ids = {r.get("id") for r in owed(current)}
        # ONE INSTANT FOR THE WHOLE LISTING, bound ABOVE — before snapshot() —
        # and threaded through the selector chain. This surface used to sample
        # the clock THREE times — once inside the --overdue arm, once for the
        # header stamp, once for the per-row ages — so the FILTER and the AGES
        # it rendered were measured against different clocks, and a row could
        # be selected as overdue then printed with an age that did not agree.
        # `_read_stamp` already took a `now` for exactly this.
        selected = sorted(current.values(),
                          key=lambda r: (str(r.get("ts") or ""), r["id"]))
        if "--open" in flags:
            # A SUPERSEDED PARENT IS NOT ACTIONABLE (441c4491). It stays OPEN on
            # purpose — a BUILD parent closes through `landed`/`discharged` on
            # its successor's PROOF, and cancelling it here would destroy that
            # door (measured: 18 close-ladder tests). So the row keeps its
            # status and this SURFACE reads the annotation instead.
            #
            # FILTERED HERE, NOT IN `_open`: that predicate is shared, and a
            # superseded parent is still genuinely open to every door that
            # needs it. Only the ACTIONABLE-WORK question has a different
            # answer, and only this surface is asking it.
            # ...and only while that successor is ALIVE: a parent whose child
            # was cancelled (an aborted rebind) is owed again, and hiding it
            # would replace duplicate debt with hidden debt.
            selected = [r for r in selected if r.get("id") in owed_ids]
        elif "--overdue" in flags:
            # OWED FIRST, THEN LATE. Filtering raw rows by age alone resurrected
            # an already-carried parent as pickup work: --open showed the child
            # and --overdue showed the PARENT, from one snapshot, in the same
            # breath. Overdue is a property of a debt, so a row that owes
            # nothing cannot be late for it. Late is measured against the ONE
            # instant this listing bound, not a fresh sample taken here.
            selected = [r for r in selected
                        if r.get("id") in owed_ids and _is_overdue(r, read_now)]
        elif "--held" in flags:
            selected = [r for r in selected
                        if r.get("status") == "held"]
        # The projection runs AFTER the whole selector chain, never inside it:
        # trunk added it beside --open/--overdue and this lane added --held as
        # another arm, so a resolution that kept only one side would either
        # drop the new filter or leave --held rows unprojected — the same
        # verdict polarity every other arm gets.
        selected = with_verdict_projections(selected)
        if "--json" in flags:
            print(json.dumps(selected, ensure_ascii=False, indent=1))
            return 0
        if not selected:
            # AN ABSENCE IS THE CLAIM MOST IN NEED OF AN INSTANT, and it was
            # the one line without one: the POPULATED header two lines below
            # has been stamped all along. "no matching rows" with no read time
            # cannot be told apart from a stale empty — the reader has nothing
            # to measure the nothing against.
            print("helm dispatch: no matching rows  (read %s)"
                  % _read_stamp(read_now))
            return 0
        print("helm dispatch — %d logical row%s  "
              "(verdict polarity: %s; attestation: %s; read %s)" % (
                  len(selected), "" if len(selected) == 1 else "s",
                  _source_label(POLARITY_SOURCE), _source_label(ATTEST_SOURCE),
                  _read_stamp(read_now)))
        now = read_now
        # `selected` already carries the one-read typed projection. Derive the
        # marker set from it rather than reading the sidecar a second time.
        unverifiable = frozenset(
            str(row["id"]) for row in selected
            if row.get("attest_state") == "unverifiable")
        def _late(r):
            return r.get("id") in owed_ids and _is_overdue(r, now)

        for row in selected:
            # BOTH PROPERTIES. `_late` (from trunk) is owed-aware, so a row
            # that owes nothing cannot be late; the `now` this lane made
            # REQUIRED and positional is the listing's one instant, so the age
            # printed and the marker beside it are measured against the same
            # clock. The marker can now disagree with neither the age nor the
            # debt.
            print(_fmt(row, now, _late(row), unverifiable))
        if any(_late(r) for r in selected):
            print("NEEDS CHECK-IN is advisory only; do not reassign on age alone")
        if unverifiable:
            # The marker is two words; a reader who has never seen it needs to
            # know it is NOT a work item and NOT a corruption, or the honest
            # response is alarm followed by a wasted hour.
            print("ATTEST UNVERIFIABLE (%d): the VERDICT STANDS — what cannot "
                  "be verified is the signed delivery record, because the "
                  "attest is bound to a different evidence string than the row "
                  "now carries (a re-mint). Nothing is blocked and nothing is "
                  "owed; the attest path is at-most-once and never re-signs."
                  % len(unverifiable))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
