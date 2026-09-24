#!/usr/bin/env python3
"""Runtime facts, exact-session evidence, and their roster binding.

Launch metadata and its validator are pure. Exact-session readers and the two
host-owned writers live beside them because runtime provenance is one concern,
not generic roster administration. The writers defer their two session-history
helpers back to ``seats_roster`` at call time; roster imports this module at
load time, so the dependency graph stays acyclic.

Public callers reach these names through the ``seats`` facade. Internal callers
import this owner directly rather than making ``seats_roster`` grow past the
project's 1000-line module budget.
"""
import json
import os
import re

from . import pk
from .seats_common import _flocked, roster_for_write, roster_path


_RUNTIME_ENV = {"agent_harness": "HELM_AGENT_HARNESS",
                "family": "HELM_MODEL_FAMILY",
                "backend": "HELM_MODEL_BACKEND",
                # WHICH MODEL ANSWERED, as opposed to which family the seat is
                # labelled. Those are different facts and they come apart
                # exactly when a credential cools, which is exactly when a seat
                # reaches for a substitute -- so the divergence is concentrated
                # in the moments the cross-family rule exists for, not spread
                # thinly as noise.
                "model": "HELM_MODEL_ID"}
_RUNTIME_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
#: THE MODEL ID BELONGS TO THE PROVIDER'S GRAMMAR, NOT TO HELM'S. Real ids
#: carry punctuation the seat-token rule forbids -- `nex-n2.5-pro:free` is
#: live on this fleet, and provider-qualified ids carry a slash -- and a value
#: that fails validation here does not merely go unrecorded: it sets
#: `rejected`, which CLEARS the standing label. Reusing `_RUNTIME_TOKEN` would
#: therefore make a correctly measured model erase the runtime facts that
#: already work. The length bound matches the one the producer already
#: enforces on the route value (`proxywatch._proof_text`, 512), so a value
#: that proxywatch accepted can never be rejected here.
_RUNTIME_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,512}\Z")
#: field -> (pattern, casefold). Absent means the historical rule, unchanged:
#: the seat token, lowercased. The model is NOT lowercased because it is an
#: external identifier and re-spelling another producer's identity is how two
#: systems come to disagree about which one thing they are naming.
_RUNTIME_RULES = {"model": (_RUNTIME_MODEL, False)}
#: THE HARNESS'S OWN MARKER FOR A TURN NO MODEL PRODUCED. `<synthetic>` sits
#: in the transcript's model field on messages the harness composed locally
#: (an interrupt notice, a replayed local error), and it is TESTIMONY THAT NO
#: MODEL ANSWERED rather than a model id -- 17 of 66 transcripts on one live
#: host carry it. It is named here, and NOT merely dropped for failing the id
#: pattern, because those are different facts: this value is a known non-model
#: and is skipped, while any OTHER unparseable value is a spelling this reader
#: does not recognise and makes the whole answer UNKNOWN. Folding the two
#: together would let a future marker quietly subtract turns from the census
#: that decides whether a session used one model or several.
_TRANSCRIPT_NON_MODEL = ("<synthetic>",)
#: HOW MUCH OF ONE TRANSCRIPT THIS READER WILL READ BEFORE IT ANSWERS
#: UNKNOWN. `join` runs on a SessionStart hook with a 5s budget, and a
#: transcript is not a small file: the largest on one live host is 1.5 GB and
#: 4 of 66 exceed this bound. Parsing every line of that file cost 12.9s
#: MEASURED -- a fleet-wide join timeout in the shape of an added fact -- and
#: 2.5s once lines without a model key are skipped, which is still most of a
#: hook budget that has other work to do. AN INSTRUMENT'S BOUND MUST BE
#: VISIBLE IN ITS OUTPUT: past this many bytes the answer is UNKNOWN, because
#: a reader that stopped early has not established that the session named one
#: model and must not report a partial reading as a clean one.
_TRANSCRIPT_READ_BYTES = 512 << 20

# ==========================================================================
# WHERE A RUNTIME'S `model` CAME FROM, AND WHY THAT IS NOT ONE BIT.
#
# A MEASURED PROXY ROUTE is a third party's record: CLIProxyAPI dialed an
# upstream and `proxywatch._proxy_proof_runtime` derives the model from the
# exact route that proof bound. The seat contributes nothing to it and cannot
# write it.
#
# A NATIVE SEAT HAS NO SUCH THIRD PARTY. The strongest thing it can reach is
# its own harness's transcript, which records the resolved model id per turn.
# That is a BETTER self-report than the launch label -- it names a resolved id
# the launcher never supplied, and it is written after the turn rather than
# before it -- but it is still written by the seat's own process, the same
# process whose environment carries `HELM_MODEL_FAMILY`. ONE SELF-REPORT
# CANNOT CORROBORATE ANOTHER, so it gets its own word and never the measured
# one, and no consumer may read a native model as evidence about the family.
#
# ABSENT AND UNKNOWN ARE NOT THE SAME ANSWER, and collapsing them is the
# defect this vocabulary exists to prevent. ABSENT means nothing claims a
# model and that is ordinary and correct -- a proxy entry bound by lifecycle,
# a session before its first turn. UNKNOWN means a record exists and this
# reader could not reduce it to one id: an unreadable transcript, an
# ambiguous session id, a value in no recognised spelling, or -- measured at
# 16 of 62 live sessions -- a session whose transcript names TWO OR MORE
# models. A field that answered "absent" to all of those would report a
# session that used three models exactly like one that has not started.
MODEL_MEASURED = "measured"            # a third party's route bound it
MODEL_SELF_REPORTED = "self-reported"  # the seat's own harness recorded it
MODEL_ABSENT = "absent"                # nothing claims one; not a fault
MODEL_UNKNOWN = "unknown"              # a record exists and does not reduce
MODEL_SOURCES = (MODEL_MEASURED, MODEL_SELF_REPORTED,
                 MODEL_ABSENT, MODEL_UNKNOWN)


def runtime_model_source(runtime):
    """Which producer established this runtime's `model`. Never a guess.

    BOTH KNOWN PRODUCERS ARE NAMED AND EVERYTHING ELSE IS UNKNOWN. The
    tempting spelling -- measured when the backend is proxy, self-reported
    otherwise -- folds every backend neither arm anticipated into a claim
    nobody checked, so a runtime stamped by some future third path would be
    reported as the seat's own testimony when the seat said nothing.

    THE BACKEND IS THE RECORD OF THE PRODUCER, because only
    `_proxy_proof_runtime` ever writes a model onto a proxy runtime and it
    derives it from the same route it derives the family from, while only the
    native reader below ever writes one onto a native runtime.
    """
    model = runtime.get("model") if isinstance(runtime, dict) else None
    if not isinstance(model, str) or not model.strip():
        return MODEL_ABSENT
    backend = runtime.get("backend")
    if backend == "proxy":
        return MODEL_MEASURED
    if backend == "native":
        return MODEL_SELF_REPORTED
    return MODEL_UNKNOWN


def native_session_model(session, root=None):
    """(model, source) a NATIVE seat's own harness recorded for one session.

    NEVER `MODEL_MEASURED`: this function reads a file the seat's own process
    wrote, so its strongest possible answer is `MODEL_SELF_REPORTED`. The
    caller cannot launder that by where it stores the result.

    A SESSION NAMING TWO MODELS ANSWERS UNKNOWN AND NOT ONE OF THEM. A seat
    resumed onto another model, or switched mid-session, genuinely has no
    single model for the session, and 16 of 62 live sessions are in that
    state. Picking the newest would mint a fact about the whole session out of
    its last turn; picking any is inventing evidence where the record refuses
    to answer. UNKNOWN and not ABSENT, because a session that named several
    models is a record this reader could not reduce, never a session with
    nothing to say -- and a chosen id would be neither.

    A VALUE THE ID PATTERN REFUSES MAKES THE ANSWER UNKNOWN RATHER THAN BEING
    DROPPED, because `_runtime_metadata` treats a rejected model by CLEARING
    the standing runtime label -- so quietly discarding an unrecognised
    spelling here and returning a neighbouring id would report a session this
    reader could not read as one it read cleanly.
    """
    sid = str(session or "")
    if not sid:
        return None, MODEL_ABSENT
    from . import turnresponse
    path, err = turnresponse.transcript_path(sid, root)
    if err:
        # AMBIGUOUS OR UNUSABLE, never absent: two files carrying one session
        # id is a record this reader refuses to choose between.
        return None, MODEL_UNKNOWN
    if not path:
        return None, MODEL_ABSENT
    found, budget = set(), _TRANSCRIPT_READ_BYTES
    try:
        # BYTES, AND A SUBSTRING TEST BEFORE THE PARSER. Decoding and parsing
        # every line of the largest live transcript costs 12.9s against a 5s
        # hook budget; skipping the lines that cannot carry the key first
        # costs 2.5s for the same answer, and a second model ends the read at
        # once. A decode error is a ValueError here and joins the partial-line
        # arm below, which is where an unfinished last line belongs.
        with open(path, "rb") as handle:
            for line in handle:
                budget -= len(line)
                if budget < 0:
                    return None, MODEL_UNKNOWN
                if b'"model"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    # A TRAILING PARTIAL LINE IS THE NORMAL STATE of a file
                    # the harness is still appending to, so it is skipped
                    # rather than poisoning the answer. What must not be
                    # skipped is a well-formed line naming a value this
                    # reader does not recognise, which is the arm below.
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                name = message.get("model") if isinstance(message, dict) else None
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                if name in _TRANSCRIPT_NON_MODEL:
                    continue
                if not _RUNTIME_MODEL.fullmatch(name):
                    return None, MODEL_UNKNOWN
                found.add(name)
                if len(found) > 1:
                    # THE ANSWER CANNOT CHANGE BACK. A session naming two
                    # models is UNKNOWN however the rest of the file reads,
                    # and this is also what keeps the common expensive case
                    # cheap.
                    return None, MODEL_UNKNOWN
    except OSError:
        return None, MODEL_UNKNOWN
    if not found:
        return None, MODEL_ABSENT
    return found.pop(), MODEL_SELF_REPORTED


def _runtime_metadata(runtime=None):
    """(metadata, rejected) — THE one validator, and the owner of both facts.

    Agent harness (claude/pi) and model backend (native/proxy) are independent
    axes: pi can deliberately traverse a helm seat's proxy. Values are optional
    and self-declared by the launch seam; an old/unlabelled row stays unlabelled
    rather than being guessed from its display name.

    `rejected` is True when a KNOWN field arrived non-empty and did not survive
    validation. That fact belongs HERE and nowhere else: every path into the
    roster — the env translator, `join(runtime=...)`, a direct launch mirror —
    funnels through this function, so a caller cannot bypass the distinction
    between "no evidence at all" (preserve the standing label) and "explicit
    but unreadable" (clear it). An earlier cut carried the fact on a private
    key from the env translator only, which left the public writer blind to a
    malformed direct call (three rounds of the same validation-then-drop
    class: reject-then-drop makes broken testimony indistinguishable from
    silence, and silence is an upgrade).
    """
    supplied = runtime if isinstance(runtime, dict) else {}
    out, rejected = {}, False
    for field in _RUNTIME_ENV:
        pattern, casefold = _RUNTIME_RULES.get(field, (_RUNTIME_TOKEN, True))
        value = str(supplied.get(field) or "").strip()
        if casefold:
            value = value.lower()
        if not value:
            continue
        if pattern.fullmatch(value):
            out[field] = value
        else:
            rejected = True
    return out, rejected
def _runtime_environment(env=None):
    """Translate one joining process's launch environment into runtime facts.

    Two self-detection legs mirror each other: pi stamps PI_CODING_AGENT into
    its own environment, and claude-code stamps CLAUDECODE/CLAUDE_CODE_SESSION_ID
    into its. Both may identify the HARNESS, never a display name. FAMILY is
    stricter: CLAUDECODE is inherited by arbitrary children, so it can testify
    only that the claude-code harness is somewhere above this process. Native
    Claude family requires this process's CLAUDE_CODE_SESSION_ID, a native
    backend, and no ANTHROPIC_BASE_URL override. Helm's proxy seats run the same
    harness pointed at CLIProxyAPI; their family comes from proxywatch's measured
    session-to-route proof, never from inherited environment or a launch label.
    """
    env = os.environ if env is None else env
    # RAW pass-through: the values reach the roster's validator unparsed, so a
    # malformed launch stamp is rejected at the SAME boundary a malformed
    # direct call is, rather than being silently dropped here.
    # STRIPPED-LOWERCASE raw: the validator's contract is case-insensitive
    # (its token class spans both cases), so the derive predicates must read
    # values the same way it will — HELM_AGENT_HARNESS=Claude normalizes to
    # claude and must still permit the family derivation. Lowercasing cannot
    # change any accept/reject decision, so malformed testimony is preserved
    # exactly as before (meld e:1785568214: normalization-order
    # regression introduced by the raw pass-through).
    # PER-FIELD CASEFOLDING, taken from the SAME table the validator reads.
    # A flat `.lower()` here silently undoes `_RUNTIME_RULES`' one exception:
    # the model is deliberately NOT casefolded, because re-spelling another
    # producer's identifier is how two systems come to disagree about which
    # one thing they are naming. Every other field keeps the historical
    # lowercase exactly, so no accept/reject decision and no derive predicate
    # below moves -- they all read casefolded fields.
    out = {}
    for field, name in _RUNTIME_ENV.items():
        _pattern, casefold = _RUNTIME_RULES.get(field, (_RUNTIME_TOKEN, True))
        value = str(env.get(name) or "").strip()
        out[field] = value.lower() if casefold else value
    out = {field: value for field, value in out.items() if value}
    # RAW presence is testimony even when the value is malformed: a rejected
    # explicit stamp must stay UNKNOWN, never be treated as absent and then
    # re-derived into an authorization-bearing label (2a899c9317:
    # HELM_MODEL_FAMILY="codex/family" fails the token check, vanishes from
    # `out`, and the derive leg would have upgraded the seat to claude).
    raw = {field: field in out for field in _RUNTIME_ENV}
    claude_session = bool(str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip())
    claude_harness = bool(str(env.get("CLAUDECODE") or "").strip()
                          or claude_session)
    if "agent_harness" not in out and not raw["agent_harness"] \
            and str(env.get("PI_CODING_AGENT") or "").lower() == "true":
        out["agent_harness"] = "pi"
    if "agent_harness" not in out and not raw["agent_harness"] and claude_harness:
        out["agent_harness"] = "claude"
    # An EXPLICIT backend is launch testimony about the serving path: proxy
    # (or any non-native token, or a malformed one) contradicts a native
    # claude-family derivation even without ANTHROPIC_BASE_URL set.
    backend_native = (not raw["backend"]) or out.get("backend") == "native"
    # CLAUDECODE alone is inherited harness testimony. Only the session-bearing
    # runtime can claim native Claude family; stamp backend=native beside the
    # derived family so dispatch can distinguish this measured native path from
    # an unverified/legacy family string.
    if "family" not in out and not raw["family"] and claude_session \
            and out.get("agent_harness") == "claude" and backend_native \
            and not str(env.get("ANTHROPIC_BASE_URL") or "").strip():
        out["family"] = "claude"
        if "backend" not in out and not raw["backend"]:
            out["backend"] = "native"
    return out


def launch_runtime(env=None, root=None):
    """The joining process's runtime facts, WITH the native model stamped.

    THE LAUNCH SEAM NEVER KNEW WHICH MODEL ANSWERED. Everything
    `_runtime_environment` reads is a label the launcher wrote before the
    first turn: `HELM_MODEL_FAMILY` says claude, and the model behind it was
    unrecorded, so a native seat's whole identity was the seam describing
    itself. The proxy side has carried a model since the route proof did; this
    is the native counterpart, and it is a SELF-REPORT (see
    `runtime_model_source`) -- it adds a fact and buys no independence,
    because this model and that family come from one self-declaring seam.

    ONLY ONTO A DERIVED-NATIVE CLAUDE RUNTIME. The transcript is addressed by
    `CLAUDE_CODE_SESSION_ID`, which is exactly the evidence
    `_runtime_environment` already requires before it will call a runtime
    native claude, so this never invents a model for a proxied seat whose
    family comes from a measured route instead.

    AN EXPLICIT STAMP IS TESTIMONY AND IS NEVER OVERWRITTEN, INCLUDING A
    MALFORMED ONE. A launcher that set `HELM_MODEL_ID` has spoken about this
    turn; replacing a value that will be REJECTED downstream with a readable
    one derived here would turn broken testimony into a clean record, which is
    the one upgrade `_runtime_metadata` exists to refuse.

    IT LANDS ON A RE-JOIN, NOT ON THE FIRST TURN, and that is a real bound
    rather than a hidden one. `join` runs on SessionStart, and a FRESH
    session's transcript has no assistant turn yet, so the honest answer then
    is ABSENT and no model is stamped. A resume or a compaction re-runs
    SessionStart against the SAME session id with a populated transcript, and
    that is where the stamp arrives.
    """
    env = os.environ if env is None else env
    runtime = _runtime_environment(env)
    if str(env.get(_RUNTIME_ENV["model"]) or "").strip():
        return runtime
    if runtime.get("family") != "claude" or runtime.get("backend") != "native":
        return runtime
    model, _source = native_session_model(
        str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip(), root)
    return dict(runtime, model=model) if model else runtime


def runtime_entry_for_session(row, session):
    """The exact per-session runtime record, never the row-level fallback."""
    if not isinstance(row, dict) or not session:
        return None
    entries = row.get("runtime_sessions")
    if not isinstance(entries, dict):
        return None
    entry = entries.get(str(session))
    return entry if isinstance(entry, dict) else None


def _validated_proxywatch_entry(entry, session):
    """One exact measured entry, re-derived from its immutable proof or None."""
    if not isinstance(entry, dict) or set(entry) != {
            "runtime", "verified", "source", "proxy_proof"} \
            or entry.get("source") != "proxywatch" \
            or entry.get("verified") is not True:
        return None
    from . import proxywatch
    proof = entry.get("proxy_proof")
    measured, err = proxywatch._proxy_proof_runtime(proof)
    metadata, rejected = _runtime_metadata(entry.get("runtime"))
    # THE STORED TESTIMONY MUST BE A SUBSET OF WHAT THE PROOF RE-DERIVES, not
    # byte-equal to it. Byte-equality makes every field this derivation LEARNS
    # retroactively invalidate every entry written before it existed: the
    # re-derivation grows a key, no stored row has it, `measured != metadata`
    # for all of them, and every seat silently loses its verified family the
    # moment the new code lands. That is a fleet-wide authority outage
    # produced by an ADDITIVE improvement, and the suite caught it here when
    # `model` arrived -- 28 arms went red on runtimes that were never wrong.
    #
    # SUBSET IS NOT A WEAKER CHECK IN THE DIRECTION THAT MATTERS. The proof is
    # the authority and the derivation is what reads it, so a stored entry can
    # still never CLAIM anything the proof does not say: every key it does
    # carry must be present in the derivation and equal there, and a stored
    # family contradicting the measured one is refused exactly as before. What
    # it may now do is carry FEWER keys, which asserts less rather than more.
    agrees = all(measured.get(key) == value
                 for key, value in metadata.items()) \
        if isinstance(measured, dict) else False
    if err or rejected or not agrees or metadata != entry.get("runtime") \
            or not isinstance(proof, dict) \
            or proof.get("session") != str(session):
        return None
    return entry


def _verified_exact_runtime(row, session):
    """(runtime, entry) for exact verified testimony; never row fallback."""
    entry = runtime_entry_for_session(row, session)
    if not entry or entry.get("verified") is not True \
            or not isinstance(entry.get("runtime"), dict):
        return None, None
    if entry.get("source") == "proxywatch" \
            and _validated_proxywatch_entry(entry, session) is None:
        return None, None
    return entry["runtime"], entry


def _prune_runtime_sessions(row, drop=None):
    """Keep exact runtime authority in lockstep with retained session history."""
    entries = row.get("runtime_sessions")
    if not isinstance(entries, dict):
        row.pop("runtime_sessions", None)
        return
    kept = {str(s) for s in row.get("sessions") or [] if s}
    if row.get("session"):
        kept.add(str(row["session"]))
    entries = {str(s): value for s, value in entries.items()
               if str(s) in kept and str(s) != str(drop)}
    if entries:
        row["runtime_sessions"] = entries
    else:
        row.pop("runtime_sessions", None)


def _roster_proxywatch_entry(entries, session):
    """Classify one roster proof and poison invalid proxy authority in place."""
    prior = entries.get(session)
    measured = _validated_proxywatch_entry(prior, session)
    invalid = isinstance(prior, dict) \
        and prior.get("source") == "proxywatch" and measured is None
    if invalid:
        runtime = prior.get("runtime")
        entries[session] = {
            "runtime": runtime if isinstance(runtime, dict) else {},
            "verified": False, "source": "invalid-proxywatch"}
    return measured, invalid


def runtime_for_session(row, session=None):
    """(runtime, verified) for one delivery process, never a seat-level guess.

    Co-named sessions fan out through distinct cursors and may use different
    providers. New joins therefore retain verified launch metadata beside the
    exact session id. The row-level label remains the owner-facing newest-launch
    summary and is a compatibility fallback only when the current session has no
    exact record (or for a legacy sessionless caller). A present malformed,
    unverified, or contradictory exact record blocks that fallback.
    """
    if not isinstance(row, dict):
        return None, False
    if session:
        entries = row.get("runtime_sessions")
        if entries is not None and not isinstance(entries, dict):
            return None, False
        sid = str(session)
        if isinstance(entries, dict) and sid in entries:
            runtime, entry = _verified_exact_runtime(row, sid)
            return (runtime, True) if entry else (None, False)
        if row.get("session") != sid:
            return None, False
    runtime = row.get("runtime")
    verified = row.get("runtime_verified") is True
    return (runtime, verified) if isinstance(runtime, dict) else (None, False)


def _carry_row_summary(row, session, entry):
    """Let a HOST-PROVEN exact stamp also refresh the row-level summary.

    THE DEFECT THIS CLOSES, and it is the whole of "stamped at launch only".
    Three writers can establish a seat's runtime identity: the seat's own join
    (``write_roster``), the host-owned lifecycle bind, and proxywatch's
    measurement. Only the FIRST wrote ``runtime``/``runtime_verified`` — the
    row-level label — while the other two wrote the exact per-session entry and
    nothing else. The exact entry is keyed BY SESSION ID, so a fact established
    on either non-launch path lives exactly as long as that id does: the moment
    the seat's next session arrives, `write_roster`'s carry-forward leg asks
    ``row["runtime_verified"] is True``, reads None, and the seat is unlabelled
    again until something re-measures it. Measured on a live roster: eight rows
    held verified exact evidence for their CURRENT session and no row-level
    label at all, every one of them established by lifecycle or proxywatch.

    ONLY FOR THE ROW'S CURRENT SESSION. The summary is the fallback
    ``runtime_for_session`` uses when a session has no exact record, and that
    fallback is already fenced to ``row["session"]``. Writing it from evidence
    about an OLDER id would make the label answer for a session the evidence
    was never about, so the current-session test is stated here rather than
    inherited from the callers that happen to satisfy it today.

    THE SUMMARY FOLLOWS THE ENTRY, NEVER THE CALLER'S ARGUMENT. A lifecycle
    bind that finds a prior proxywatch measurement KEEPS the measurement and
    discards its own launch label; the row-level label has to agree with what
    was actually retained, or the two surfaces disagree about one session.
    """
    if not isinstance(row, dict) or not isinstance(entry, dict):
        return
    if str(row.get("session") or "") != str(session or ""):
        return
    if entry.get("verified") is not True:
        return
    runtime = entry.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        return
    row["runtime"] = dict(runtime)
    row["runtime_verified"] = True


def _bind_refusal(rows, seat, sid):
    """Why a lifecycle rebind of `seat` to `sid` must not happen, or None —
    decided on ONE roster snapshot and writing nothing.

    TWO MECHANISMS, and the second is the one 2444 measured. A session that is
    already some other row's CURRENT session cannot be re-bound under a second
    name without splitting one process across two identities. And a `seat`
    that is still a LIVE RENAME ALIAS for another row is a name whose row was
    deliberately moved: admitting it mints a second row for one identity and
    `retire_alias_claims` pops the very alias that was covering the window.

    `live_alias` rather than the raw rename record, because a claim on a name
    that has since been lawfully re-admitted as its own row is a RETIRED alias
    and that distinction is already settled (task/2338).
    """
    from .seats_common import live_alias
    want = str(seat).casefold()
    owner = next((name for name, row in rows.items()
                  if isinstance(row, dict) and row.get("session") == sid
                  and str(name).casefold() != want), None)
    if owner is not None:
        return ("lifecycle session %s is already current for %s" % (sid, owner))
    alias, _until = live_alias(seat, rows)
    if alias is not None:
        return ("lifecycle seat %s is a live rename alias for %s, so admitting "
                "it would mint a second row for one identity and retire the "
                "alias still addressing it" % (seat, alias))
    return None


def lifecycle_bind_refusal(seat, session):
    """(incarnation, refusal) — why a lifecycle rebind of `seat` must not be
    admitted, decided with NOTHING WRITTEN, plus the row generation it decided
    ON.

    THE ADMISSION RAN FIRST AND THE PROOF SECOND. `_bind_runtime_session` wrote
    the roster row and only then asked `bind_lifecycle_runtime` whether this
    session was already another row's — so a stale name reached the door that
    MINTS: the bare row appeared, the rename alias covering the window was
    retired on the way out, and the refusal arrived after both. `write_roster`
    names this boundary in its own admission comment: resolving a name at the
    mint would make absence of evidence into ownership, so the proof lives
    HERE. A refusal that arrives after the mutation it was meant to prevent is
    not a refusal, it is a log line.

    THE GENERATION IS THE FENCE, AND THE ROSTER ALREADY MINTS ONE. Every
    admission stamps `incarnation` on the row and a lawful key reuse mints a
    fresh one, precisely so a deferred writer cannot cross identity
    generations. This returns the value it decided on; `bind_lifecycle_runtime`
    CONSUMES it, and a row whose generation moved — or which is no longer there
    at all — is refused before that function writes anything.

    A SAME-STRING NAME IS NEVER AGREEMENT, which is why the fence is the
    generation rather than the name. A rename can publish the roster and
    release its lock before it ever reaches the spawn lock this caller holds,
    so no preflight of a NAME could see it; a generation that no longer matches
    says so whatever the name spells.
    """
    sid = str(session or "")
    if not sid:
        return None, "lifecycle session is empty"
    with _flocked(roster_path() + ".lock"):
        rows = roster_for_write()
        why = _bind_refusal(rows, seat, sid)
        if why:
            return None, why
        want = str(seat).casefold()
        mine = next((v for k, v in rows.items()
                     if str(k).casefold() == want and isinstance(v, dict)),
                    None)
        # TWO OUTCOMES, AND A MARKERLESS ROW IS NEITHER OF THEM.
        # An absent row is not a refusal — a seat that has never joined has
        # nothing for a first bind to refresh, and minting there is this path
        # working. A present row answers its own generation.
        #
        # A PRESENT ROW WITH NO GENERATION IS A MIGRATION FAULT, NOT A FENCE
        # STATE. A sentinel meaning "some markerless row" names a CLASS and
        # not an IDENTITY, and every markerless row satisfies it — so a real
        # rename can carry a DIFFERENT markerless row into this key between
        # the proof and the admission and pass the fence. There is no value
        # this function could return that NAMES such a row, so it refuses and
        # names the cure instead of inventing a token.
        from .seats_roster import ABSENT, INCARNATION_MIGRATION
        if mine is None:
            return ABSENT, None
        mark = mine.get("incarnation")
        if not isinstance(mark, str):
            return None, ("lifecycle seat %s predates the identity generation "
                          "marker, so nothing here can name WHICH row it is — "
                          "stamp it first with `%s`"
                          % (seat, INCARNATION_MIGRATION))
        return mark, None


def bind_lifecycle_runtime(seat, session, runtime,
                           expect_incarnation=None):
    """Bind host-proven lifecycle testimony without impersonating the seat.

    ``write_roster`` is the process-self-write boundary and deliberately drops a
    foreign session. Resume/rebind/backfill are host operations instead: their
    caller has already proven the pane and session. This narrower owner requires
    an admitted roster row, refuses another current owner and native/proxy
    disagreement, records no presence beat, and stamps provenance so approval can
    distinguish launch testimony from proxywatch measurement.

    AND IT REFRESHES THE ROW-LEVEL SUMMARY from whatever entry was RETAINED
    (`_carry_row_summary`), which is the measurement when one already exists
    rather than this caller's launch label. Without that half a resume/rebind
    established the seat's runtime for exactly one session id.
    """
    from .seats_roster import _evict_session, _keep_sessions

    sid = str(session or "")
    metadata, rejected = _runtime_metadata(runtime)
    if not sid or rejected or metadata != runtime \
            or metadata.get("backend") != "proxy" \
            or metadata.get("agent_harness") != "claude" \
            or not metadata.get("family"):
        return None, "lifecycle runtime stamp is malformed"
    with _flocked(roster_path() + ".lock"):
        rows = roster_for_write()
        matches = [(name, row) for name, row in rows.items()
                   if str(name).casefold() == str(seat).casefold()
                   and isinstance(row, dict)]
        if len(matches) != 1:
            return None, ("lifecycle seat %s matched %d roster identities" %
                          (seat, len(matches)))
        name, row = matches[0]
        refusal = _bind_refusal(rows, seat, sid)
        if refusal:
            return None, refusal
        if expect_incarnation is not None \
                and row.get("incarnation") != expect_incarnation:
            # THE ROW MOVED BETWEEN THE PROOF AND THE BIND. A rename publishes
            # the roster and releases its lock before it reaches the caller's
            # spawn lock, so the NAME can still resolve while the generation
            # behind it is a different one. Refused here, before this function
            # writes anything.
            return None, ("lifecycle seat %s is a different incarnation than "
                          "the one this rebind proved (%r, not %r)"
                          % (seat, row.get("incarnation"), expect_incarnation))
        standing, verified = runtime_for_session(row, sid)
        if verified and standing.get("backend") == "native":
            return None, ("lifecycle proxy runtime contradicts verified native "
                          "runtime for %s" % name)
        sessions = [value for value in row.get("sessions") or []
                    if value != sid]
        sessions.append(sid)
        row["session"] = sid
        row["sessions"] = _keep_sessions(sessions, sid)
        runtimes = row.get("runtime_sessions")
        runtimes = dict(runtimes) if isinstance(runtimes, dict) else {}
        measured = _validated_proxywatch_entry(runtimes.get(sid), sid)
        entry = measured or {"runtime": metadata, "verified": True,
                             "source": "lifecycle"}
        runtimes[sid] = entry
        row["runtime_sessions"] = runtimes
        _prune_runtime_sessions(row)
        _carry_row_summary(row, sid, entry)
        rows[name] = row
        _evict_session(rows, sid, name)
        pk.write_json(roster_path(), rows)
    return entry, None


def stamp_proxy_runtime(session, runtime, proof):
    """Promote one proof-derived proxy runtime into exact-session evidence.

    IT ALSO REFRESHES THE ROW-LEVEL SUMMARY (`_carry_row_summary`). The exact
    entry is keyed by session id and dies with it; the summary is what the
    seat's NEXT session inherits. Recording only the exact half is what made
    this a launch-only fact.

    This is the sole minter of ``source=proxywatch`` plus ``verified=True``, so
    it decodes the proof itself and requires the caller's runtime to equal that
    derivation exactly. Launch-time proxy labels may be replaced by measurement;
    verified native testimony contradicts a proxy route and refuses rather than
    being silently relabelled.
    """
    from . import proxywatch

    sid = str(session or "")
    measured, proof_error = proxywatch._proxy_proof_runtime(proof)
    metadata, rejected = _runtime_metadata(runtime)
    if not sid or proof_error or rejected or metadata != runtime:
        return None, ("measured proxy runtime stamp is malformed%s" %
                      (": " + proof_error if proof_error else ""))
    if proof["session"] != sid:
        return None, "measured proxy runtime proof names another session"
    if measured != metadata:
        return None, "measured proxy runtime does not match its proof"
    with _flocked(roster_path() + ".lock"):
        rows = roster_for_write()
        matches = [(name, row) for name, row in rows.items()
                   if isinstance(row, dict) and row.get("session") == sid]
        if len(matches) != 1:
            return None, ("measured proxy session %s matched %d current roster "
                          "identities" % (sid, len(matches)))
        name, row = matches[0]
        standing, verified = runtime_for_session(row, sid)
        if verified and standing.get("backend") == "native":
            return None, ("measured proxy runtime contradicts verified native "
                          "runtime for %s" % name)
        runtimes = row.get("runtime_sessions")
        runtimes = dict(runtimes) if isinstance(runtimes, dict) else {}
        entry = {"runtime": metadata, "verified": True,
                 "source": "proxywatch", "proxy_proof": proof}
        runtimes[sid] = entry
        row["runtime_sessions"] = runtimes
        _carry_row_summary(row, sid, entry)
        rows[name] = row
        pk.write_json(roster_path(), rows)
    return entry, None
