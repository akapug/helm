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


def _hold_policy(repo):
    """Read one certain policy with a verified project and policy population."""
    from . import registry
    from .inject._ledger import project_for_cwd
    from .store.load import _certain_policy_from_hits, _policy_hits
    projects = registry.load(strict=True)["projects"]
    project = project_for_cwd(repo, projects=projects, strict=True)
    return _certain_policy_from_hits("approval-tier", _policy_hits(
        "approval-tier", project=project, strict=True, projects=projects))


def _resting_run(row, actor, tip, run=None):
    """The fresh-context read run a source-clean hold by `actor` at `tip`
    rests on, or None: a CONCUR of exactly `tip` that `actor` recorded on
    `row` (`landreq._fresh_instance_read`, the door's own predicate), whose
    independence is `fresh-context`. Such a read is on the ledger only after
    its run record passed every check on disk (`_fresh_context_read`), and
    replay re-admits it only as an Opus read. `run`, when given, binds the
    one run a proof names, in either spelling of its id."""
    from . import landreq                # DEFERRED — landreq imports us.
    key = dispatches._advisory_run_key(run) if run else None
    reads = [r for r in (row or {}).get("advisory_reads") or ()
             if isinstance(r, dict) and r.get("independence") == "fresh-context"
             and (key is None
                  or dispatches._advisory_run_key(r.get("reviewer_run")) == key)]
    return landreq._fresh_instance_read({"advisory_reads": reads}, actor, tip) \
        if reads else None


def record_hold_approval(row, actor, tip, stamp):
    """Freeze the exact-session holder and the policy that admitted this hold.

    A hold that RESTS ON A FRESH-CONTEXT READ RUN the holder recorded at the
    held tip (`_resting_run`) is judged on that run: the reader is the run,
    and the seat only records it. The proof is version 3: the seat's
    identity and family, the run's model as `model`, and the run id as
    `run`. Another model in the seat's own window (an unrelated Sonnet build
    subagent) does not void it. Where the run's model is not admitted, the
    seat's window rule below still applies.

    Otherwise a NATIVE seat whose subagents named other models in the window
    before the hold records it only when that cannot change admission: every
    model the seat's family and admitted by the family rule. The proof then
    names the seat's own answer and lists every model in `window_models`
    (v2)."""
    from . import home, native_turn, reviewer_eligibility, verdict_tier
    from .store.policy_history import POLICY_FIELDS
    session = home.session_id()
    if not session:
        return None
    families, evidence, anchor, why = \
        dispatches._approval_identity_family_evidence(
            actor, session=session, require_exact_session=True)
    if why or not families or len(families) != 1:
        return None
    family = next(iter(families))
    if not isinstance(evidence, dict) or evidence.get("v") not in (3, 5) \
            or evidence.get("session") != session or \
            dispatches._family_evidence_error(evidence, actor, family, anchor):
        return None
    try:
        policy, why = dispatches._hold_policy(row.get("repo_root"))
    except Exception:  # noqa: BLE001 — an unread policy cannot mint proof
        return None
    if why or not policy:
        return None
    snapshot = {key: policy.get(key) for key in POLICY_FIELDS}
    proof = {"row": row["id"], "actor": actor, "tip": tip, "ts": stamp,
             "family": family, "authority": evidence, "policy": snapshot}
    read = dispatches._resting_run(row, actor, tip)
    run_model = read.get("reviewer_model") if read else None
    # The run's model must be the holder's family, a model that reviews,
    # and admitted by the policy on its own.
    if isinstance(run_model, str) and run_model \
            and verdict_tier.window_family(run_model, family) == family \
            and reviewer_eligibility.input_only(run_model, family)[0] is False \
            and verdict_tier.admit_window(
                snapshot, actor, family, [run_model])[0] == "ok":
        proof.update(v=3, model=run_model, run=read["reviewer_run"])
    else:
        # The stamp or the seat's transcript answers first. Where that fails
        # closed, a native seat's transcript names its candidates instead:
        # its own answer and every other model a subagent named in the window.
        model = dispatches._evidence_model(evidence, stamp)
        got = (model, frozenset()) if model \
            else native_turn.evidence_turn_candidates(evidence, stamp)
        if not got:
            return None
        model, models = got[0], sorted({got[0]} | got[1])
        if any(reviewer_eligibility.input_only(m, family)[0] is not False
               for m in models):
            return None
        if verdict_tier.admit_window(snapshot, actor, family, models)[0] \
                != "ok":
            return None
        proof.update(v=1, model=model)
        if len(models) > 1:
            proof.update(v=2, window_models=models)
    proof["anchor"] = dispatches._proof_anchor(
        "source-clean-holder-v%d" % proof["v"], proof)
    return proof


def hold_approval(row, repo, current=True):
    """Judge frozen hold proof; a later owner policy may still demote it.

    Three versions, each with its exact key set and anchor domain: v2 is v1
    plus `window_models`, every model a native seat's window named
    (`record_hold_approval`), and the whole window is judged again here. v3
    is v1 plus `run`: the fresh-context read run the hold rests on, which
    the row must still carry as a CONCUR of the held tip recorded by the
    holder, with `model` as its model; that model alone is judged."""
    from . import native_turn, reviewer_eligibility, verdict_tier
    from .store.policy_history import POLICY_FIELDS
    proof = row.get("hold_approval") if isinstance(row, dict) else None
    version = proof.get("v") if isinstance(proof, dict) else None
    keys = {"v", "row", "actor", "tip", "ts", "family", "model", "authority",
            "policy", "anchor"}
    extra = {1: set(), 2: {"window_models"}, 3: {"run"}}
    if type(version) is not int or version not in extra \
            or set(proof) != keys | extra[version]:
        return False, "no proven approval-tier holder at the hold"
    if proof["anchor"] != dispatches._proof_anchor(
            "source-clean-holder-v%d" % version,
            {k: v for k, v in proof.items() if k != "anchor"}):
        return False, "hold approval proof does not match its anchor"
    if (proof["row"], proof["actor"], proof["tip"], proof["ts"]) != (
            row.get("id"), row.get("hold_actor"), row.get("source_clean_tip"),
            row.get("hold_ts")):
        return False, "hold approval proof belongs to another hold"
    family, model, evidence = (proof["family"], proof["model"],
                               proof["authority"])
    if not isinstance(family, str) or not isinstance(model, str) or not model \
            or not isinstance(evidence, dict) or evidence.get("v") not in (3, 5) \
            or not evidence.get("session") or \
            dispatches._family_evidence_error(
                evidence, proof["actor"], family,
                dispatches._subsumed_family_anchor(evidence)):
        return False, "hold approval runtime is unproven"
    if version == 3:
        # THE READER IS THE RUN, so the seat's own runtime model is no
        # input: the run must still be on the row, and `model` must be its.
        read = dispatches._resting_run(row, proof["actor"], proof["tip"],
                                       run=proof["run"]) \
            if isinstance(proof["run"], str) and proof["run"] else None
        if not read or read.get("reviewer_model") != model:
            return False, ("hold approval run is not a fresh-context read "
                           "the holder recorded at the held tip")
        if verdict_tier.window_family(model, family) != family:
            return False, "hold approval run model is not the holder's family"
    else:
        stamped = dispatches._evidence_model(evidence, proof["ts"],
                                             stamped_only=True)
        if stamped and stamped != model or evidence["v"] == 3 and not stamped:
            return False, "hold approval model contradicts its runtime proof"
    models = proof.get("window_models", [model])
    if version == 2 and (
            not isinstance(models, list) or len(models) < 2
            or not all(isinstance(m, str) and m for m in models)
            or models != sorted(set(models)) or model not in models
            or not native_turn.reads_window(evidence)):
        return False, "hold approval window is malformed"
    if any(reviewer_eligibility.input_only(m, family)[0] is not False
           for m in models):
        return False, "hold approval model is input only or unknown"
    if verdict_tier.admit_window(
            proof["policy"], proof["actor"], family, models)[0] != "ok":
        return False, "holder was not admitted at the hold"
    if not current:
        return True, None
    # `repo` can be the shared Git common-dir's checkout, not the linked
    # worktree the hold was recorded in. Policy scope belongs to that row.
    recorded_repo = row.get("repo_root")
    if not isinstance(recorded_repo, str) or not os.path.isabs(recorded_repo):
        return False, "hold approval has no recorded checkout for current policy"
    try:
        policy, why = dispatches._hold_policy(recorded_repo)
    except Exception as exc:  # noqa: BLE001 — unread is not admitted
        return False, "current approval tier is unreadable (%s)" % type(exc).__name__
    if why or not policy:
        return False, "current approval tier is unavailable (%s)" % (why or "absent")
    snapshot = {key: policy.get(key) for key in POLICY_FIELDS}
    state, why = verdict_tier.admit_window(
        snapshot, proof["actor"], family, models)
    return (True, None) if state == "ok" else (
        False, "holder is not admitted by the current approval tier (%s: %s)" %
        (state, why or "unread"))


def record_applied_approval(row, tip, stamp):
    """Freeze the approval-tier proof an unchanged applied cure spends
    (task/3937 cure round), built ONLY from the parent FIX's own verdict-time
    authority — the exact-session reviewer identity, resolved family and model,
    and the certain policy `mark_verdict` captured and RETAINED at the hold —
    re-admitted against THAT retained snapshot, never today's roster, runtime
    or store.

    The applied event itself proves PATCH IDENTITY only (`_diff_applied_refusal`
    measured it). What makes the recorded cure landable is that the reviewer's
    own receipt bound the bytes AND the reviewer's hand was approval-tier at
    the hold; both facts already sit on the parent row, frozen. This mints the
    second onto the event so a land reader can re-judge it the way
    `hold_approval` re-judges a source-clean hold. A pre-tier parent (no
    verdict author proof, a tier record that did not admit the reviewer, or a
    retained policy version that no longer resolves) mints NOTHING: the cure
    still records, and `applied_approval` below refuses it by name. Never
    returns a refusal — no-proof is the honest shape for a reviewer whose tier
    the hold did not freeze."""
    from . import reviewer_eligibility, verdict_tier
    from .store import policy_history
    if not isinstance(row, dict):
        return None
    evidence = row.get("verdict_author_runtime_evidence")
    session = row.get("verdict_author_session")
    anchor = row.get("verdict_author_runtime_anchor")
    actor = row.get("recipient")
    if not isinstance(evidence, dict) or not isinstance(session, str) \
            or not session or not isinstance(actor, str) or not actor:
        return None
    if dispatches._verdict_author_runtime_error(evidence, actor, session,
                                                anchor):
        return None
    authority = evidence.get("authority")
    if not isinstance(authority, dict) or authority.get("v") not in (3, 5) \
            or authority.get("session") != session:
        return None
    family = evidence.get("resolved", {}).get("family")
    if not isinstance(family, str) or not family:
        return None
    model = dispatches._verdict_policy_model(evidence)
    if not model or reviewer_eligibility.input_only(model, family)[0] is not False:
        return None
    tier = row.get("verdict_tier_evidence")
    if not isinstance(tier, dict) or tier.get("state") != "ok" \
            or not isinstance(tier.get("policy_version"), dict):
        return None
    # THE POLICY FROZEN AT THE HOLD, re-resolved from the retained-version
    # store the verdict writer captured into — never today's population.
    record, err = policy_history.resolve(tier["policy_version"],
                                         tier.get("context"))
    if err or not isinstance(record.get("policy"), dict):
        return None
    policy = record["policy"]
    state, _why, _rule = verdict_tier.admit(policy, actor, {family}, model)
    if state != "ok":
        return None
    proof = {"v": 1, "row": row["id"], "actor": actor, "tip": tip,
             "ts": stamp, "family": family, "model": model,
             "authority": authority, "policy": policy}
    proof["anchor"] = dispatches._proof_anchor("diff-applied-holder-v1", proof)
    return proof


def applied_approval(row, repo, current=True):
    """(ok, why) — re-judge the frozen proof `record_applied_approval` spent
    onto a diff-applied event, the way `hold_approval` re-judges a
    source-clean hold: the anchor must re-derive over the exact fields, the
    proof must belong to THIS row's confirmed applied tip, the recorded
    authority must still self-check, and the CURRENT owner policy remains a
    separate veto over the frozen identity (task/3976's law, reused).

    Absent proof is the named refusal, never a pass: a cure recorded from a
    pre-tier parent has patch identity and nothing else."""
    from . import reviewer_eligibility, verdict_tier
    from .store.policy_history import POLICY_FIELDS
    applied = row.get("diff_applied") if isinstance(row, dict) else None
    proof = applied.get("hold_approval") if isinstance(applied, dict) else None
    # task/4114 item 4: a RETRACTED verdict withdrew the reviewer's authority;
    # the frozen proof is void, so both the YIELD-suppression path and the
    # LAND-AUTHORITY path must refuse even though the proof bytes are intact.
    # The POLARITY is the fail-safe spelling: retraction moves it off "fix",
    # and only a live FIX ever carried this proof, so any other polarity
    # (retracted, or a reader nobody taught this flag) authorizes nothing.
    if not isinstance(row, dict) or row.get("verdict_retracted") \
            or row.get("polarity") != "fix":
        return False, ("the review that froze this proof is no longer a live "
                       "FIX (retracted or re-polarized), so its authority is "
                       "void")
    if not isinstance(proof, dict) or set(proof) != {
            "v", "row", "actor", "tip", "ts", "family", "model",
            "authority", "policy", "anchor"} or type(proof.get("v")) is not int \
            or proof["v"] != 1:
        return False, ("no proven approval-tier reviewer frozen on the "
                       "diff-applied event")
    if proof["anchor"] != dispatches._proof_anchor(
            "diff-applied-holder-v1", {k: v for k, v in proof.items()
                                       if k != "anchor"}):
        return False, "applied approval proof does not match its anchor"
    if proof["row"] != row.get("id") \
            or proof["tip"] != applied.get("tip"):
        return False, "applied approval proof belongs to another row or tip"
    family, model, evidence = (proof["family"], proof["model"],
                               proof["authority"])
    if not isinstance(family, str) or not isinstance(model, str) or not model \
            or not isinstance(evidence, dict) or evidence.get("v") not in (3, 5) \
            or not evidence.get("session") or \
            dispatches._family_evidence_error(
                evidence, proof["actor"], family,
                dispatches._subsumed_family_anchor(evidence)):
        return False, "applied approval reviewer runtime is unproven"
    if reviewer_eligibility.input_only(model, family)[0] is not False:
        return False, "applied approval model is input only or unknown"
    state, _why, _rule = verdict_tier.admit(
        proof["policy"], proof["actor"], {family}, model)
    if state != "ok":
        return False, "reviewer was not admitted by the policy frozen at the hold"
    if not current:
        return True, None
    recorded_repo = row.get("repo_root")
    if not isinstance(recorded_repo, str) or not os.path.isabs(recorded_repo):
        return False, "applied approval has no recorded checkout for current policy"
    try:
        policy, why = dispatches._hold_policy(recorded_repo)
    except Exception as exc:  # noqa: BLE001 — unread is not admitted
        return False, "current approval tier is unreadable (%s)" % type(exc).__name__
    if why or not policy:
        return False, "current approval tier is unavailable (%s)" % (why or "absent")
    snapshot = {key: policy.get(key) for key in POLICY_FIELDS}
    state, why, _rule = verdict_tier.admit(
        snapshot, proof["actor"], {family}, model)
    return (True, None) if state == "ok" else (
        False, "reviewer is not admitted by the current approval tier (%s: %s)" %
        (state, why or "unread"))


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
