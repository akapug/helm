"""`helm.dispatches_tier` -- WHO IS INSIDE THE APPROVAL TIER, or which unknown.

ONE QUESTION. `approval_tier` answers whether a recipient's runtime family is
inside the current approval tier; `approval_tier_for_verdict` and
`_record_verdict_tier` answer it for the verdict being written; the identity
family evidence is the proof both read; and `TierUnknown` names WHICH of four
kinds of "helm cannot say" an unknown answer is, because each has a different
cure and owner. The per-thread projection lenses (`tier_lens`, `epoch_lens`)
that let one board read answer these once are here with them.

MEASURED AT THE CUT, THE REMAINDER REACHES IN THROUGH EIGHT NAMES, all read
inside function bodies, so each resolves the published object at call time:
the verdict writer (`approval_tier_for_verdict`, `_record_verdict_tier`,
`_approval_identity_family_evidence`), the pair and runtime family readers
(`_approval_identity_family_evidence`, `_kind_of`, `TIER_DAMAGED`), the
recipient rung's tier note (`_approval_tier_advisory`) and the fold's epoch
key (`_EPOCH_LENS`, `epoch_lens`).

ONE ROSTER READ MOVED WITH IT. `_approval_identity_family_evidence` is one of
the ledger's `seats.roster_checked()` sites, so the display-launder tripwire
counts it under this file now, with the reason it always carried.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 106 reads of 34 distinct names; no other byte of a moved line
changed. A `from` import, or a bare global left behind, binds the object ONCE
at import, so an arm that patches an attribute on `dispatches` and then drives
this code would reach the original and measure nothing, while every structural
guard stayed green. The module spelling keeps the lookup at CALL TIME, exactly
as a bare global did inside the ledger. Names this file OWNS are spelled that
way too, because they are published back onto `dispatches` and the suites
patch them there.

THE ONE EXCEPTION IS A READ THAT RUNS AT IMPORT, and it is the faithful
spelling rather than a gap: `TIER_UNKNOWN_KINDS`, built from the six kind
constants above it, and the default `_live=_LIVE_APPROVAL_FAMILIES` on
`_approval_tier_uncached`. Such a read binds once in either spelling, as it
did in the ledger, and at that moment only this module's own global exists --
the publish loop at the bottom has not run yet, so `dispatches.NAME` there
would raise.

THE CYCLE IS BROKEN THE WAY THE LEDGER'S OTHER SATELLITES BREAK IT: this
module imports `dispatches` EAGERLY, and `dispatches` imports this one at the
END of its own body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
import contextlib
import os
import threading

from . import dispatches


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
        if kind not in dispatches.TIER_UNKNOWN_KINDS:
            raise ValueError("unknown tier-unknown kind %r" % (kind,))
        self = str.__new__(cls, text)
        self.kind = kind
        return self


def _kind_of(text):
    """The kind riding on this string, or None. Untagged is NEVER inferred."""
    kind = getattr(text, "kind", None)
    return kind if kind in dispatches.TIER_UNKNOWN_KINDS else None


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
    return kind if kind in dispatches.TIER_UNKNOWN_KINDS else dispatches.TIER_UNCLASSIFIED


def tier_unknown_heals_itself(kind):
    """Does this unknown clear WITHOUT anyone doing anything?

    ONLY TRANSIENT. Dark clears when an upstream returns (an event, not a
    re-read), damaged never clears without a repair, unnamed never clears
    without an edit to the row, and unclassified is by construction a state
    nobody measured. Any surface that promises healing must ask HERE."""
    return kind == dispatches.TIER_TRANSIENT


def _tier_unknown(kind, text):
    """Mint one classified UNKNOWN: ("unknown"-with-a-kind, sentence)."""
    return dispatches.TierUnknown(kind, "unknown"), text


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
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_TRANSIENT, "roster runtime record is unreadable")
    canonical, err = seats._resolve_against(recipient, roster)
    if err:
        return None, None, None, dispatches.TierUnknown(dispatches.TIER_UNNAMED, err)
    matches = [(name, row) for name, row in roster.items()
               if seats.recipient_matches(name, canonical)]
    if len(matches) != 1 or not isinstance(matches[0][1], dict):
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_UNNAMED, "no unique canonical roster runtime record for "
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
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_DARK, "@%s has no exact runtime record for author "
                       "session %s" % (canonical, session or "(none)"))
    runtime, verified = seats.runtime_for_session(row, session)
    metadata, rejected = seats._runtime_metadata(runtime)
    native = isinstance(runtime, dict) and bool(runtime) and not rejected \
        and metadata == runtime and verified \
        and runtime.get("backend") in (None, "native")
    family = runtime.get("family") if native else None
    if native and isinstance(family, str) and dispatches._TOKEN.fullmatch(family):
        evidence = {"v": 5 if isinstance(session, str) and session else 4,
                    "identity": recipient, "roster_identity": roster_identity,
                    "runtime": dict(runtime), "runtime_verified": True}
        if evidence["v"] == 5:
            evidence["session"] = session
        return {family}, evidence, dispatches._subsumed_family_anchor(evidence), None

    if not isinstance(session, str) or not session:
        # DARK, and this is the shape the whole vocabulary was written for: an
        # empty seat slot stores no session, so there is no key under which a
        # proof could ever be looked up. Three live members on 2026-08-11
        # (three claude seats). Nothing to re-read; nothing to repair.
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_DARK, "no verified native runtime and @%s has no exact "
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
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_DARK, "no verified native runtime and @%s has no measured "
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
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_UNCLASSIFIED,
            "no verified native runtime for @%s; %s" % (canonical, why))
    if family != runtime.get("family"):
        # DAMAGED: the re-proved family and the seat's own exact-session stamp
        # are both stored, and they disagree. Neither yields on a re-read.
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_DAMAGED, "measured proxy runtime family for @%s contradicts "
                          "its exact-session stamp" % canonical)
    if not isinstance(family, str) or not dispatches._TOKEN.fullmatch(family) or not proof:
        return None, None, None, dispatches.TierUnknown(
            dispatches.TIER_DAMAGED,
            "measured proxy runtime family is malformed for @%s" % canonical)
    evidence = {"v": 3, "identity": recipient,
                "roster_identity": roster_identity, "session": session,
                "proxy_proof": proof}
    return {family}, evidence, dispatches._subsumed_family_anchor(evidence), None


def _approval_identity_families(recipient):
    """Explicit family evidence for one canonical seat, or why it is unknown."""
    families, _evidence, _anchor, err = \
        dispatches._approval_identity_family_evidence(recipient)
    return families, err


_LIVE_APPROVAL_FAMILIES = object()


def _verdict_tier_context(row):
    context = {key: row.get(key) for key in dispatches._VERDICT_TIER_CONTEXT}
    if isinstance(context["gate_caps"], tuple):
        context["gate_caps"] = list(context["gate_caps"])
    return context


#: The tier-evidence shapes this reader accepts, newest first. v3 adds the
#: APPROVAL RULE the recorded policy gave the reader (`verdict_tier.admit`):
#: `non-author` when a `model:` selector admitted the model its runtime
#: records, `family` otherwise. v2 adds the family AXIS and the model that
#: licensed it; v1 is every verdict minted before the axis existed. v1 and v2
#: REPLAY EXACTLY AS RECORDED, under the grammar they were derived under, and
#: keep the family rule — a version rather than a widened one because
#: `approval_tier_for_verdict` tests the field set by equality and anchors
#: the whole dict, so growing an old shape would turn every minted verdict on
#: this ledger into DAMAGED at once.
_TIER_EVIDENCE_FIELDS = {
    3: {"v", "context", "policy_version", "state", "reason", "kind",
        "family_axis", "family_model", "approval_rule"},
    2: {"v", "context", "policy_version", "state", "reason", "kind",
        "family_axis", "family_model"},
    1: {"v", "context", "policy_version", "state", "reason", "kind"}}
_TIER_EVIDENCE_VERSION = 3


def _record_verdict_tier(row):
    """Capture authority at append time, bound to the exact verdict context."""
    from . import verdict_tier
    from .store import policy_history
    context = dispatches._verdict_tier_context(row)
    reference, err = policy_history.capture(row.get("repo_id"), context)
    if err:
        return None, err
    record, err = policy_history.resolve(reference, context)
    if err:
        return None, err
    author = row["verdict_author_runtime_evidence"]
    family = author["resolved"]["family"]
    axis, model = dispatches.verdict_family_axis(author)
    # THE MODEL THAT ANSWERED. Native authority records it directly; proxy
    # authority keeps the requested Claude alias in `resolved.model` and the
    # measured answer in `resolved.upstream_model`. Only the latter may match
    # a model selector — an alias is routing, not authority. None keeps the
    # family rule.
    state, why, rule = verdict_tier.admit(
        record["policy"], row.get("recipient"), {family},
        dispatches._verdict_policy_model(author))
    # THE AXIS IS RECORDED, NEVER A VETO. The tier answers MEMBERSHIP and the
    # family it judged is bound by the author proof either way; what the axis
    # says is how strong the input behind that one word was. Denying on a
    # roster axis would refuse every native review this fleet writes, which is
    # a fleet-wide outage wearing the shape of a fix. The reader that cares
    # about diversity reads `family_axis`; nobody has to infer it from silence.
    evidence = {"v": dispatches._TIER_EVIDENCE_VERSION, "context": context,
                "policy_version": reference, "state": str(state), "reason": why,
                "kind": dispatches.tier_unknown_kind(state),
                "family_axis": axis, "family_model": model,
                "approval_rule": rule}
    return {"verdict_tier_evidence": evidence,
            "verdict_tier_anchor": dispatches._proof_anchor("verdict-tier-v1", evidence)}, None


def approval_tier_for_verdict(row):
    """Read recorded authority, never today's policy or runtime.

    Absent historical evidence is PRE-TIER, not an unread projection. Partial,
    future-versioned, or contradictory evidence remains DAMAGED. Both deny.
    """
    if not isinstance(row, dict):
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "verdict is unreadable")
    present = [key in row for key in dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS]
    tier_present = [key in row for key in dispatches.VERDICT_TIER_FIELDS]
    if any(present):
        if not all(present):
            return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "verdict author runtime proof is incomplete")
        session = row["verdict_author_session"]
        if not isinstance(session, str) or not session:
            return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "verdict author runtime proof is malformed")
        why = dispatches._verdict_author_runtime_error(
            row["verdict_author_runtime_evidence"], row.get("recipient"), session,
            row["verdict_author_runtime_anchor"])
        if why:
            return dispatches._tier_unknown(dispatches.TIER_DAMAGED, why)
    if not any(tier_present):
        if row.get("verdict_version") == 4:
            return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "v4 verdict lacks its required record-time tier proof")
        return dispatches._tier_unknown(dispatches.TIER_PRE_TIER,
                             "PRE-TIER: no record-time approval-tier evidence; "
                             "readable historical verdict, nonauthorizing — re-review "
                             "with the current writer, never infer authority from today's roster or policy")
    if not all(tier_present) or not all(present):
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "recorded tier or author proof is incomplete")
    evidence = row["verdict_tier_evidence"]
    version = evidence.get("v") if isinstance(evidence, dict) else None
    if not isinstance(evidence, dict) or type(version) is not int \
            or set(evidence) != dispatches._TIER_EVIDENCE_FIELDS.get(version, ()):
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "recorded tier evidence is malformed or unsupported")
    if evidence["context"] != dispatches._verdict_tier_context(row) \
            or row["verdict_tier_anchor"] != dispatches._proof_anchor("verdict-tier-v1", evidence):
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "recorded tier evidence does not bind this verdict")
    from . import verdict_tier
    from .store import policy_history
    record, err = policy_history.resolve(evidence["policy_version"], evidence["context"])
    if err:
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, err)
    author = row["verdict_author_runtime_evidence"]
    family = author["resolved"]["family"]
    # EACH VERSION IS RE-DERIVED UNDER THE GRAMMAR IT WAS MINTED UNDER: v1
    # and v2 by `evaluate`, which reads a `model:` selector as malformed
    # exactly as their writer did, and v3 by `admit`, which reads it and
    # re-derives the rule it recorded.
    if version >= 3:
        state, why, rule = verdict_tier.admit(
            record["policy"], row.get("recipient"), {family},
            dispatches._verdict_policy_model(author))
    else:
        (state, why), rule = verdict_tier.evaluate(
            record["policy"], row.get("recipient"), {family}), None
    if (evidence["state"], evidence["reason"], evidence["kind"]) != \
            (str(state), why, dispatches.tier_unknown_kind(state)) \
            or evidence.get("approval_rule") != rule:
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED, "recorded tier contradicts its policy/runtime evidence")
    # THE RECORDED AXIS IS RE-DERIVED FROM THE AUTHOR PROOF, exactly as the
    # state above is re-derived from the policy. A stamp nobody re-checks is a
    # claim, not evidence — and this one says how strong the tier's input was,
    # so a row that could carry a false axis would be worse than one carrying
    # none. v1 rows predate the axis and are not asked for it: absent is
    # honest, invented would not be.
    if version >= 2 and (evidence["family_axis"], evidence["family_model"]) \
            != dispatches.verdict_family_axis(author):
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED,
                             "recorded tier family axis contradicts its author proof")
    return state, why


def non_author_tier_error(row, verify=True):
    """Why this verdict's RECORDED approval tier did not give its reader the
    NON-AUTHOR rule, or None when it did: the tier half of the owner's ruling
    that Opus seats are in the upper tier (the seat half is
    `landreq.non_author_error`).

    ONLY A v3 RECORD CARRIES THE RULE, and only when the policy version it
    names admitted the reader by a `model:` selector naming the model its
    seat's runtime records, with the state `ok`. Every v1 and v2 record,
    every record minted under a family-only policy, and every reader whose
    runtime names no model or another one answers a reason here, and the
    caller keeps the family rule, so an old event replays as it did.

    `verify` re-derives the record against the policy version it names
    (`approval_tier_for_verdict`), as every projection and writer reads
    authority. Replay passes False: a fold reads no policy (the `subsumed`
    law, `landreq.recorded_hold_kind`), so it reads the recorded fields its
    writer verified under the lock."""
    from . import verdict_tier
    evidence = row.get("verdict_tier_evidence") if isinstance(row, dict) \
        else None
    if not isinstance(evidence, dict) or type(evidence.get("v")) is not int \
            or evidence["v"] != 3 or evidence.get("state") != "ok" \
            or evidence.get("approval_rule") != verdict_tier.RULE_NON_AUTHOR:
        return ("its recorded approval tier gives the family rule: no "
                "`model:` selector of the policy version it was recorded "
                "under names the model its seat's runtime records")
    if verify:
        state, why = dispatches.approval_tier_for_verdict(row)
        if state != "ok":
            return ("its recorded approval tier does not authorize it: %s"
                    % (why or state))
    return None


def approval_tier(recipient, repo=None, at=None):
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
    sealed read-set and per-projection index.

    `at` IS A RECORDED READ'S MOMENT (task/3508): a `model:` selector then
    judges the model the recipient answered on at that moment, not now (a
    seat that held on Sonnet and switched to Opus afterwards is not an Opus
    holder). Such a call resolves live, outside the lens and the memo, whose
    keys carry no moment. None is today's advisory question, unchanged."""
    if at is not None:
        return dispatches._approval_tier_uncached(recipient, repo, at=at)
    lens = getattr(dispatches._TIER_LENS, "fn", None)
    if lens is not None:
        return lens(recipient, repo)
    return dispatches._approval_tier_memo(
        dispatches._approval_tier_key(recipient, repo, dispatches._LIVE_APPROVAL_FAMILIES, None),
        lambda: dispatches._approval_tier_uncached(recipient, repo))


# PER-THREAD, because helm web is a ThreadingHTTPServer and one build's
# read-set must never answer another's rows.
_TIER_LENS = threading.local()


@contextlib.contextmanager
def tier_lens(fn):
    """Route `approval_tier` through `fn` for the duration of one projection.

    RESTORES THE PREVIOUS LENS rather than clearing, so composing two of these
    cannot silently leave the inner one's resolver installed over the outer's
    remaining rows."""
    prev = getattr(dispatches._TIER_LENS, "fn", None)
    dispatches._TIER_LENS.fn = fn
    try:
        yield
    finally:
        dispatches._TIER_LENS.fn = prev


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
    prev = getattr(dispatches._EPOCH_LENS, "fn", None)
    dispatches._EPOCH_LENS.fn = fn
    try:
        yield
    finally:
        dispatches._EPOCH_LENS.fn = prev


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
    kind = dispatches.tier_unknown_kind(hit[0])
    if kind in (dispatches.TIER_TRANSIENT, dispatches.TIER_UNCLASSIFIED):
        projscope.forget(key)
    return hit


def _approval_tier_uncached(recipient, repo=None,
                            families=_LIVE_APPROVAL_FAMILIES,
                            canonical_recipient=None, at=None):
    """The live resolution. `approval_tier` is the memoising door; this is the
    body it guards, split out so the memo has something to call and so a
    caller that must re-measure can say so. `at` is the moment a `model:`
    selector judges (`approval_tier`)."""
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
        return dispatches._tier_unknown(dispatches.TIER_DAMAGED,
                             "approval-tier check unavailable: %s" % why)
    if canonical_recipient is None:
        canonical, err = seats.resolve_recipient(recipient)
        if err:
            return dispatches._tier_unknown(
                dispatches.TIER_UNNAMED, "approval-tier check unavailable: %s" % err)
    else:
        canonical = canonical_recipient
    reason = str(policy.get("policy_reason") or "").strip()
    if not reason:
        return dispatches._tier_unknown(
            dispatches.TIER_DAMAGED, "approval-tier check unavailable: policy %s has "
                          "no policy_reason" % policy["id"])
    exact, family_selectors, model_selectors, selectors = set(), set(), set(), []
    for raw in policy.get("policy_members") or []:
        selector = str(raw or "").strip()
        head, sep, token = selector.partition(":")
        if sep != ":" or head not in ("seat", "family", "model") \
                or not dispatches._TOKEN.fullmatch(token):
            return dispatches._tier_unknown(
                dispatches.TIER_DAMAGED, "approval-tier check unavailable: policy %s "
                              "has malformed selector %r"
                              % (policy["id"], selector))
        selectors.append(selector)
        if head == "seat":
            seat_token, _err = seats._canonical_recipient(token)
            exact.add(str(seat_token))
        elif head == "model":
            model_selectors.add(token.casefold())
        else:
            family_selectors.add(token)
    if canonical in exact:
        return "ok", None
    # A `model:` SELECTOR ADMITS THE SEAT WHOSE RUNTIME RECORDS THAT MODEL
    # (`verdict_tier.admit` reads the same selector off the recorded policy).
    # A runtime that names no model is not admitted by it. ONE live read
    # serves this and the family selectors below: a proxied seat's costs a
    # canary. The model is `_runtime_model`'s reading of that evidence, so a
    # native claude seat's transcript turn at `at` answers here too.
    live = None
    if model_selectors and families is dispatches._LIVE_APPROVAL_FAMILIES:
        from . import verdict_tier
        live = dispatches._approval_identity_family_evidence(canonical)
        model = None if live[3] else dispatches._evidence_model(live[1], at)
        if model and verdict_tier._model_keys(model) & model_selectors:
            return "ok", None
    if family_selectors:
        if families is dispatches._LIVE_APPROVAL_FAMILIES:
            families, why = (live[0], live[3]) if live \
                else dispatches._approval_identity_families(canonical)
            if why:
                # CARRY THE RESOLVER'S OWN CLASSIFICATION. It measured which
                # kind this is; wrapping the sentence must not lose it, and
                # re-deriving it from the wrapped prose is the parse this
                # vocabulary replaced. An untagged why (a test double, a path
                # added later without a tag) stays UNCLASSIFIED rather than
                # borrowing a neighbour.
                return dispatches._tier_unknown(
                    dispatches._kind_of(why) or dispatches.TIER_UNCLASSIFIED,
                    "approval-tier check unavailable for @%s: %s"
                    % (canonical, why))
        if not families:
            # DARK by meaning: no author-runtime evidence EXISTS for this
            # historical verdict — nothing stored to re-read, nothing on the
            # ledger to repair, and it clears only by an event (a re-review
            # from a snapshot-stamped writer), never by asking again.
            return dispatches._tier_unknown(
                dispatches.TIER_DARK, "approval-tier check unavailable for @%s: no "
                           "immutable verdict-time runtime family evidence "
                           "(current roster sessions never rewrite history)"
                           % canonical)
        if len(families) > 1:
            # DAMAGED: two explicit family proofs for one seat contradict each
            # other. Both are stored; neither yields on a re-read.
            return dispatches._tier_unknown(
                dispatches.TIER_DAMAGED, "approval-tier check unavailable for @%s: "
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
    state, msg = dispatches.approval_tier(recipient)
    if state == "ok":
        return None
    if state == "outside":
        return ("WARNING: review recipient %s. Warning only — review dispatch "
                "continues." % msg)
    return "NOTE: %s; review dispatch continues." % msg


# ---------------------------------------------------------------------------
# PUBLISH BACK ONTO THE LEDGER
# ---------------------------------------------------------------------------
def owned():
    """The names this module owns, read from the LEDGER'S declaration.

    ONE SOURCE OF TRUTH. `dispatches._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this publish loop walks, so a name
    added here without a declaration is not silently bound, and a declared
    name this module does not define fails loudly at publish rather than
    quietly at a caller.
    """
    token = __name__.rsplit(".", 1)[-1]
    for module, names in dispatches._OWNER_NAMES:
        if module == token:
            return names
    return ()


def _publish():
    """Bind the declared names onto the ledger module. Once, at import."""
    for _name in owned():
        setattr(dispatches, _name, globals()[_name])


_publish()
