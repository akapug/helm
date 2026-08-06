#!/usr/bin/env python3
"""helm council — an EMBARGOED N-of-M quorum over a pinned member set.

The FORMAL convergence species (owner canon 2026-07-23: meld = the genus,
standup = the informal 2+, council = the big formal one with agenda/quorum/
recorded verdict). Semantics ported from the tested Rust substrate at
meld/crates/meld-verbs/src/council.rs — the SEMANTICS and the embargo
invariant, not the code (helm is stdlib-only Python).

WHY EMBARGO (the load-bearing idea, design-locked with codex-3 2026-07-24):
a council is SEALED INDEPENDENT JUDGMENT, not collaborative fixing. Nothing —
not the verdicts, not even WHO has signalled — is revealed until quorum, so
members cannot contaminate each other's judgment. This is the fix for the
exact failure we lived through: a serial review chain where each reviewer read
the prior one, so every later round was anchored on the first one's frame.
A reviewer who discovers a FIX submits it SEALED; the author fixes AFTER
reveal. If collaboration is genuinely needed before quorum, ABORT the council
and run a standup, then reconvene on the superseding tip — never partially
leak.

The embargo is a PROTOCOL embargo, not a crypto one: the ledger is a same-uid
0700 file, so a local process could read it directly. What it defends is the
real threat — an agent (or a prompt-injected one) reading its peers' judgment
through legit tooling before forming its own. Principal crypto stays dregg's,
exactly as chat's `origin` field does.

(That paragraph said "tmpfs file like every other helm room" until the
durability rework moved the ledger to disk — a comment that outlived its own
subject by one commit, which is the same staleness class as the help text
that advertised this feature as DEFERRED for a release after it shipped.
Fixed in the pass that caused it.)
"""
import hashlib
import os

from . import chat, home, pk

# DECISION vocabulary, not gate vocabulary (kimi's scoping refutation, meld
# 2026-07-24): a council RATIFIES — release go/no-go, adopt-this-design,
# accept-this-policy. It is NOT the per-slice correctness gate. That gate
# stays the OPEN reviewer-finds -> reviewer-FIXES -> re-gate ping-pong,
# because embargoing a found defect would strand the hot-context fix: the
# author would not know to stop and the finder would have to sit on a FIX
# until two others weighed in. Embargo is right where CONTAMINATION is the
# risk (independent judgment), wrong where SPEED-TO-FIX is the value.
VERDICTS = ("YES", "NO", "ABSTAIN")

# The default bar. NOT unanimity: with an embargo you cannot chase the
# missing member — hiding WHO signed necessarily hides who has NOT, so
# threshold=ALL yields a wedge you cannot even diagnose (the structural
# reason neither reviewer named; it decides the fork between codex-3's
# least-surprise ALL and kimi's liveness 2-of-3). Majority is a canonical
# rule rather than a fleet-size guess, and it is PRINTED on every surface so
# it is never silent — which was codex-3's actual concern.
def default_threshold(n):
    return n // 2 + 1


def registry_path(room):
    """ONE append-only EVENT LEDGER per council, on DISK (codex-3 re-gate).

    It was a JSON snapshot in /dev/shm — the same tmpfs chat lives in. Chat is
    RAM by design because a lost message is a lost message; a council's whole
    product is a RECORDED verdict, so losing it on reboot means the record was
    never a record. I first declined this as 'a substrate decision', which was
    wrong: helm already HAS the durable primitive (eventledger, on disk under
    home.global_dir(), replayed by a reducer) and this module was already
    using its LOCK. Using its append and replay too is not new substrate — it
    is using the one already depended on, correctly."""
    return os.path.join(home.global_dir(), "councils",
                        "%s.jsonl" % pk.slug(room))


_TYPES = {"convene", "signal", "reveal", "abort", "import"}

V1 = 1                                    # the legacy snapshot schema we know


def _valid_import(ev, room=None):
    """(ok, why) — THE ONE RULE for an import event, called by BOTH the writer
    (on what it is about to emit) and the reducer (on anything it reads).

    Seven review rounds on this migration, and the recurring shape was never
    really the object — it was the PAIR. I would tighten the normalizer and
    leave the reducer loose, or the reverse, and the next round found the gap
    between them: `evidence or ""` fixed on one path and left on the other,
    a required ts enforced when writing and INDEXED unchecked when reading.
    Two validators for one contract will always drift, because nothing forces
    an edit to land in both.

    So there is one. The writer refuses to emit what this would drop; the
    reducer drops anything this refuses. Symmetry stops being something I
    maintain by hand and becomes something the code cannot violate."""
    if ev.get("event") != "import":
        return False, "not an import"
    # THE ENVELOPE IS PART OF THE EVENT (codex round 8). The writer used to
    # validate BEFORE _emit stamped v/ts/room/id, so what was checked was not
    # what was written — planted imports with v missing, v=2, or id=OTHER
    # validated and folded, and the state was silently rewritten to v=1. That
    # is the schema-version and identity laundering AGAIN, one layer further
    # out. Nothing may be added to an event after it is validated.
    if ev.get("v") != V1:
        return False, "envelope v"
    if room is not None and ev.get("id") != room:
        return False, "envelope id"
    # `ts` was the LAST field _emit still stamped after validation — I closed
    # v and id last round and left this one doing the identical thing, which
    # is the instance-not-the-class inside the fix for the class. The writer
    # stamps it now, so nothing whatsoever is added to an import after it is
    # checked: validated and written are the same object, with no exceptions
    # left to remember.
    if not isinstance(ev.get("ts"), str) or not ev["ts"]:
        return False, "envelope ts"
    # v1 convener semantics are identity-or-None; a dict is neither
    if ev.get("convener") is not None and not (
            isinstance(ev["convener"], str) and ev["convener"]):
        return False, "convener"
    # the ledger binding belongs IN the rule, not beside it. Without this the
    # validator accepted an event the fold then dropped for being addressed to
    # another room — a disagreement I found by attacking my own symmetry claim
    # after asserting it in a commit message, which is the exact overclaim the
    # commit-message-is-a-surface lesson is about. Caller passes the room it
    # is writing to or reading from; None means "no ledger context to bind".
    if room is not None and ev.get("room") != room:
        return False, "room"
    for key in ("room", "epoch", "members", "threshold", "tip", "status",
                "created", "signals"):
        if key not in ev:
            return False, "missing %s" % key
    if not isinstance(ev["created"], str) or not ev["created"]:
        return False, "created type"      # a KEY is not a VALUE: created=[]
    if ev["status"] not in TERMINAL:      # used to import and replay verbatim
        return False, "status"
    members, k = ev["members"], ev["threshold"]
    if (not isinstance(members, list) or not members
            or not all(isinstance(m, str) and m for m in members)
            or len(set(members)) != len(members)):
        return False, "members"
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= len(members):
        return False, "threshold"
    if isinstance(ev["epoch"], bool) or not isinstance(ev["epoch"], int):
        return False, "epoch"
    if not isinstance(ev["tip"], str) or not ev["tip"].strip():
        return False, "tip"
    if not isinstance(ev["signals"], list):
        return False, "signals container"
    seen = set()
    for s in ev["signals"]:
        if not isinstance(s, dict):
            return False, "signal entry"
        seat = s.get("seat")
        if seat not in members or seat in seen:
            return False, "signal seat"   # a duplicate is not a tally
        seen.add(seat)
        if s.get("verdict") not in VERDICTS:
            return False, "signal verdict"
        if s.get("tip") != ev["tip"]:
            return False, "signal tip"
        # EXACT types, never coerced: `str(x or "")` accepted [] / 0 / False
        # and rewrote them as "" — the same erasure the normalizer already
        # refused, left standing on the read path for a full round.
        if not isinstance(s.get("evidence"), str):
            return False, "signal evidence"
        if not isinstance(s.get("ts"), str) or not s["ts"]:
            return False, "signal ts"     # indexed unchecked before: KeyError
        if s.get("digest") != evidence_digest(
                ev["tip"], s["verdict"], s["evidence"],
                room=ev["room"], epoch=ev["epoch"], seat=seat):
            return False, "signal digest"
    if ev["status"] == "revealed" and len(ev["signals"]) < k:
        return False, "revealed below threshold"
    if ev["status"] == "aborted":
        # THE TERMINAL UNION IS TYPED TOO: status=aborted carried whatever
        # aborted_by/reason happened to be there, so a dict actor and a list
        # reason folded and were stringified downstream. An outsider actor is
        # allowed — historical truth may record one — but it must be an
        # identity, and the reason must be text.
        if not isinstance(ev.get("aborted_by"), str) or not ev["aborted_by"]:
            return False, "aborted_by"
        if not isinstance(ev.get("reason"), str):
            return False, "abort reason"
    return True, None


def _apply(state, ev, room=None):
    """Fold ONE event. Strict by construction (codex-3 re-gate found probes
    where an outsider's signal satisfied the threshold, a 0 threshold opened
    quorum, a string signal reached reveal, and a malformed status raised
    KeyError): an event that does not typecheck against the convened council
    is DROPPED rather than trusted, so a corrupt tail can never manufacture a
    quorum."""
    t = ev.get("event")
    if t in ("convene", "import"):
        # FIRST valid convene wins and a later one NEVER replaces it — an
        # append-only ledger means anyone who can write the file could
        # otherwise re-convene a live council out from under its members.
        # An INVALID convene leaves the existing state alone rather than
        # nulling it (found by this module's own hostile-row test: returning
        # None here let a planted malformed convene ERASE a real council).
        if state is not None:
            return state
        # THE AUTHORITATIVE FIRST EVENT IS BOUND TOO (codex-3 migration attack):
        # room=OTHER / epoch="bad" were accepted here and permanently squatted
        # the ledger they were written into. Later events were checked against
        # the convene; the convene itself was checked against nothing.
        if room is not None and ev.get("room") != room:
            return state
        if not isinstance(ev.get("epoch"), int) or isinstance(ev.get("epoch"), bool):
            return state
        members = ev.get("members")
        if (not isinstance(members, list) or not members
                or not all(isinstance(m, str) and m for m in members)
                or len(set(members)) != len(members)):
            return state
        k = ev.get("threshold")
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= len(members):
            return state
        if not isinstance(ev.get("tip"), str) or not ev["tip"].strip():
            return state
        fresh = {"v": 1, "room": ev.get("room"), "epoch": ev.get("epoch"),
                 "convener": ev.get("convener"), "members": members,
                 "threshold": k, "tip": ev["tip"], "signals": {},
                 # THE QUESTION TEXT IS THE RECORD, the digest is only the key.
                 # `tip` carries question:<digest> for a design council, which
                 # binds every member to one subject but is unreadable by a
                 # human — and a council's whole product is a RECORDED verdict,
                 # so a record nobody can interpret is not one. Absent on every
                 # council minted before this field existed, and "" there is the
                 # honest reading rather than a guess.
                 "question": str(ev.get("question") or ""),
                 "status": "open",
                 # An IMPORT carries the council's OWN created; a fresh convene
                 # has none yet and takes the event's ts. No `or` fallback on
                 # the import path: inventing a missing timestamp as
                 # migration-time is exactly what the provenance fix promised
                 # not to do — and my last commit message SAID so while the
                 # code did the opposite (codex round 6). A planted import
                 # without it is refused below, never defaulted.
                 "created": (ev.get("created") if t == "import"
                             else ev.get("ts"))}
        if t == "import":
            # ONE VALIDATOR, shared with the writer — the reducer folds only
            # what _valid_import accepts, so the two paths cannot drift apart
            # the way they did for seven rounds.
            ok, _why = _valid_import(ev, room=room)
            if not ok:
                return state
            fresh["status"] = ev["status"]
            if ev["status"] == "aborted":
                fresh["aborted_by"] = ev.get("aborted_by")
                fresh["abort_reason"] = str(ev.get("reason") or "")[:256]
            for s_ in ev["signals"]:
                fresh["signals"][s_["seat"]] = {
                    "seat": s_["seat"], "verdict": s_["verdict"],
                    "tip": ev["tip"], "evidence": s_["evidence"],
                    "digest": s_["digest"], "ts": s_["ts"]}
        return fresh
    if state is None or state.get("status") in TERMINAL:
        return state                      # nothing precedes convene, nothing
    # EVERY event must belong to THIS council (codex-3 re-gate: a planted row
    # carrying room=OTHER and epoch=999 was folded in and reached quorum). The
    # binding fields rode the DIGEST but nothing ever CHECKED them — a binding
    # you never verify is decoration.
    if ev.get("room") != state["room"] or ev.get("epoch") != state["epoch"]:
        return state
    if t == "signal":                     # follows a terminal event
        seat, verdict = ev.get("seat"), ev.get("verdict")
        if not isinstance(seat, str) or seat not in state["members"]:
            return state                  # outsider: never counts toward quorum
        if verdict not in VERDICTS or seat in state["signals"]:
            return state
        if ev.get("tip") != state["tip"]:  # one council, one artifact
            return state
        # RECOMPUTE the digest instead of trusting the one on the row: a
        # stored digest that is never re-derived proves nothing about the
        # fields beside it (the probe planted a bogus digest and it stuck).
        evidence = str(ev.get("evidence") or "")
        if ev.get("digest") != evidence_digest(ev["tip"], verdict, evidence,
                                               room=state["room"],
                                               epoch=state["epoch"], seat=seat):
            return state
        state["signals"][seat] = {"seat": seat, "verdict": verdict,
                                  "tip": ev["tip"], "evidence": evidence,
                                  "digest": ev["digest"], "ts": ev.get("ts")}
        return state
    if t == "reveal" and len(state["signals"]) >= state["threshold"]:
        state["status"] = "revealed"
    elif t == "abort":
        # ACTOR-GATED IN THE REDUCER TOO (codex-3 re-gate: the VERB checked
        # membership but the FOLD did not, so a planted abort row permanently
        # killed any council). A guard that lives only in the write path is
        # no guard at all on a file anyone can append to.
        by = ev.get("seat")
        if by != state.get("convener") and by not in state["members"]:
            return state
        state["status"] = "aborted"
        state["abort_reason"] = str(ev.get("reason") or "")[:256]
        state["aborted_by"] = by
    return state


def registry(room):
    """The council, REPLAYED from its durable ledger. None when no valid
    convene has been recorded — a torn or hostile tail reduces to nothing
    rather than to a usable council."""
    from . import eventledger
    # checked_events returns (rows, unavailable) — an UNREADABLE ledger is not
    # an empty one, and collapsing those two was the vacuous-pass class that
    # has bitten this codebase repeatedly. Unavailable reduces to None (the
    # honest "I could not look"), never to a usable council.
    return read(room)[0]


def read(room):
    """(state, unavailable) — the TRI-STATE every verb must carry (codex-3
    re-gate). registry() collapsed 'could not read the ledger' into the same
    None as 'no council here', so an unreadable ledger made status answer the
    confident lie 'no council convened'. That is the vacuous-pass class this
    codebase keeps re-learning: a check that passes because its input was
    missing reports the opposite of the truth. Callers that can act on UNKNOWN
    use this; registry() stays the convenience view for callers that only need
    the state."""
    from . import eventledger
    rows, unavailable = eventledger.checked_events(registry_path(room))
    if unavailable:
        return None, unavailable
    state = None
    for ev in rows or []:
        if isinstance(ev, dict) and ev.get("event") in _TYPES:
            state = _apply(state, ev, room=room)
    return state, None


def _legacy_path(room):
    """Where the pre-durability SNAPSHOT lived, on tmpfs."""
    return os.path.join(chat.chat_dir(), "%s.council.json" % pk.slug(room))


def _normalize_legacy(old, room):
    """(import_event, why_not) — the WHOLE legacy snapshot as ONE normalized
    event, or nothing. Pure: reads no files, writes none.

    THE REDESIGN codex's fourth round earned. Three consecutive rounds of
    migration defects were all ONE shape — FIELD-FILTERING where the job is
    ALL-OR-NOTHING. The old body parsed, validated and skipped as it went, so
    each round found another way for a partly-understood snapshot to become a
    confidently-wrong council:
      * `old.signals` was assumed to be a mapping; a list raised AttributeError
        straight out of convene;
      * invalid signal entries were silently `continue`d while status was set
        to revealed regardless, so a 2-of-N revealed snapshot with one good and
        one bad signal replayed as revealed-with-1 and reveal() said EMBARGOED
        1/2 — the identical label-versus-product contradiction the previous
        round was supposed to have closed, one layer down;
      * a TERMINAL-but-unparseable snapshot returned "absent", letting a fresh
        OPEN council open over an abort.
    Patching those individually would have been the fourth instance of one
    class. So: ONE function establishes the entire snapshot or refuses it, and
    the reducer re-validates the complete event atomically. Nothing partial is
    emitted; nothing partial is folded.
    """
    if not isinstance(old, dict):
        return None, "not an object"
    if old.get("v") != V1:
        return None, "schema version"     # v1 wrote v=1; an absent or foreign
                                          # version is a schema I do not know,
                                          # not a v1 to import on faith
    status = old.get("status")
    if status not in TERMINAL:
        return None, "not terminal"       # unfinished: nothing worth replaying
    members = old.get("members")
    if (not isinstance(members, list) or not members
            or not all(isinstance(m, str) and m for m in members)
            or len(set(members)) != len(members)):
        return None, "members"
    threshold = old.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        return None, "threshold"          # "2" is not 2; never coerce a claim
    if not 1 <= threshold <= len(members):
        return None, "threshold range"
    epoch = old.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return None, "epoch"
    tip = old.get("tip")
    if not isinstance(tip, str) or not tip.strip():
        return None, "tip"
    # VALIDATE THE SNAPSHOT'S OWN CLAIMS — never silently rewrite them (codex,
    # round 5, and this is the sharpest of the migration findings). The
    # previous version IGNORED `old.room` and took the council's identity from
    # the path, and mapped each signal by its dict KEY while ignoring the
    # embedded `seat` and `tip`. So a body claiming room=OTHER imported as this
    # room, and signals keyed a/b but carrying seat b/a on a foreign tip
    # imported as valid a/b judgments on ours. Accepting a claim by overwriting
    # it is not validation, it is laundering: the record ends up asserting
    # something its source never said.
    # REQUIRED MEANS REQUIRED (codex round 6). The previous version wrote
    # `if x is not None and x != expected` — validate-IF-PRESENT — and then
    # filled the absence from the path, the dict key, or the top level. That is
    # the same laundering one level down: a v1 snapshot ALWAYS wrote a
    # top-level room and a seat+tip on every signal (the
    # council lineage — "cash the 0.3 deferral" + its codex-3 xrev fix),
    # so a snapshot missing them is not a lenient case to accommodate, it is a
    # snapshot I do not understand. Optionalising a required field and then
    # inventing its value is how five rounds of this bug kept finding a sixth.
    if old.get("room") != room:
        return None, "room"
    raw = old.get("signals")
    if not isinstance(raw, dict):         # PRESENT and a mapping; `or {}` both
        return None, "signals container"  # erased the type and forgave absence
    signals = []
    for seat, s in raw.items():
        # EVERY entry must be understood — one bad signal fails the WHOLE
        # snapshot rather than quietly shrinking the recorded verdicts
        if seat not in members or not isinstance(s, dict):
            return None, "signal entry"
        if s.get("seat") != seat:
            return None, "signal seat"    # required, not required-if-present
        if s.get("tip") != tip:
            return None, "signal tip"
        if s.get("verdict") not in VERDICTS:
            return None, "signal verdict"
        # NO FALSY COERCION — `or ""` turned [] / 0 / False into a valid empty
        # string BEFORE the isinstance check that existed to catch them. This
        # is the SAME erasure as the `or {}` two lines up, which I fixed last
        # round and left standing here: the instance, not the class, one line
        # away in the same function.
        evidence = s.get("evidence")
        if not isinstance(evidence, str):
            return None, "signal evidence"
        if "ts" not in s:
            return None, "signal ts"      # v1 always wrote it; absence is not
        ts = s["ts"]                       # a case to fill in
        # PROVENANCE CARRIED, not restamped (codex round 5): the old `ts` is
        # WHEN THE JUDGMENT WAS MADE. Replacing it with import time would date
        # every legacy verdict to the migration, quietly rewriting history in
        # the one record whose whole purpose is to preserve it. Absent is left
        # absent rather than invented.
        entry = {"seat": seat, "verdict": s["verdict"], "tip": tip,
                 "evidence": evidence,
                 "digest": evidence_digest(tip, s["verdict"], evidence,
                                           room=room, epoch=epoch, seat=seat)}
        entry["ts"] = ts
        signals.append(entry)
    if status == "revealed" and len(signals) < threshold:
        # a revealed council that cannot show its quorum is a label with no
        # product — refuse rather than replay the contradiction
        return None, "revealed below threshold"
    if "created" not in old:
        return None, "created"            # v1 always wrote it; a refusal must
                                          # REFUSE, never raise (the comment
                                          # said "validated above" while the
                                          # only check was the index itself)
    ev = {"v": V1, "id": room, "ts": pk.now_ts(), "event": "import",
          "epoch": epoch,
          "convener": old.get("convener"),
          "created": old["created"], "room": room,
          "members": members, "threshold": threshold, "tip": tip,
          "status": status, "signals": signals,
          "aborted_by": old.get("aborted_by"),
          "reason": str(old.get("abort_reason") or status)[:180]}
    # THE WRITER SELF-CHECKS against the READER's own rule: never emit what the
    # read path would silently drop. One line replaces seven rounds of keeping
    # two validators in step by hand.
    ok, why = _valid_import(ev, room=room)
    if not ok:
        return None, why
    return ev, None


def _import_legacy(room):
    """Carry a pre-durability snapshot's TERMINAL TRUTH into the ledger, once
    (codex-3 re-gate). Councils that ran before the ledger left snapshots the
    new code cannot see, so registry() answered None and convene would hand
    out that same room name again — silently resurrecting a council somebody
    had already ABORTED, which is the one outcome an abort exists to make
    impossible. Only TERMINAL state is imported: an unfinished old council has
    no sealed judgments worth replaying, but a closed one's ending is exactly
    what must not be lost. Caller holds the lock."""
    # EXISTENCE IS NOT VALID STATE — again, one function over (codex re-gate).
    # I fixed exactly this in convene() and left it here: keying on the PATH
    # meant a ledger holding nothing but readable junk SUPPRESSED the
    # migration, and convene then appended a fresh OPEN council over a snapshot
    # that had already been aborted. Fixing the instance in one caller and not
    # the class in the file beside it is how this came back.
    # Returns: "present" (valid state already, nothing to do), "absent"
    # (nothing to import), "imported", or "failed" — never a bare None that a
    # caller can read as success.
    existing, unavailable = read(room)
    if unavailable:
        return "failed"                   # cannot see: never assume it is free
    if existing is not None:
        return "present"
    # TRI-STATE ON THE LEGACY FILE TOO (codex round 5 — the FOURTH time this
    # exact collapse appears in this file). A file that EXISTS but will not
    # parse is not "no snapshot here": it is a snapshot I cannot read, and
    # returning absent let convene open a fresh council over it. Missing is
    # absent; present-and-unreadable is FAILED.
    legacy = _legacy_path(room)
    old = pk.read_json(legacy, None)
    if not isinstance(old, dict):
        return "failed" if os.path.exists(legacy) else "absent"
    # VALIDATE THE WHOLE SNAPSHOT BEFORE ANY APPEND (codex-3 migration attack).
    # The first version validated as it went and wrote in two steps, so hostile
    # or merely broken legacy data got PARTIALLY imported:
    #   * threshold="bogus" raised an uncaught ValueError out of int() — mid-verb,
    #     after the convene had already been appended;
    #   * duplicate/blank members wrote a convene the reducer then dropped;
    #   * status="revealed" was imported as an ABORT, destroying the very
    #     terminal truth the import exists to preserve.
    ev, why = _normalize_legacy(old, room)
    if why:
        # TERMINAL-BUT-INVALID IS *FAILED*, NEVER "ABSENT" (codex, round 4).
        # Returning absent let an aborted snapshot with duplicate members or a
        # bogus threshold permit a fresh OPEN council — UNKNOWN collapsed to
        # empty for the third time in this file. A snapshot that CLAIMS a
        # terminal status and cannot be parsed is exactly the state we must
        # never convene over.
        return "failed" if str(old.get("status")) in TERMINAL else "absent"
    return "imported" if _emit(room, ev) else "failed"


def _emit(room, ev):
    """Append one council event; the caller holds the lock."""
    from . import eventledger
    # setdefault only — an event that arrived COMPLETE (the import, which is
    # validated envelope-and-all before it gets here) is written verbatim, so
    # validated and written are the same object rather than two that drift.
    ev.setdefault("v", V1)
    ev.setdefault("ts", pk.now_ts())
    ev.setdefault("room", room)
    # eventledger.checked_events DROPS id-less rows (same contract the attest
    # ledger documents) — an event with no id is silently discarded, which
    # would make every council reduce to "never convened"
    ev.setdefault("id", room)
    return eventledger.append_unlocked(registry_path(room), ev)


QUESTION_SUBJECT_PREFIX = "question:"


def question_subject(question):
    """The SUBJECT id for a council that judges a QUESTION, not an artifact.

    A council's invariant was never "a tip" — it is that EVERY MEMBER RULES ON
    THE SAME NAMED THING, FIXED BEFORE ANYONE SIGNALS. codex-3 named the hazard
    when they made the tip required: an optional tip "fell back to first-signal
    selection, which lets the fastest member choose the question every other
    member is answering". A question supplied AT CONVENE is bound exactly as
    hard as a tip is — what was never acceptable is a subject nobody named.

    IT OCCUPIES THE TIP SLOT ON PURPOSE. `evidence_digest` RS-joins six fields;
    adding a seventh would change the digest of every council ever recorded and
    break the idempotency key that tells a retry from a second vote. So an
    artifact council keeps its real tip and digests byte-identically to before,
    and a question council carries `question:<digest>` in that slot — a subject
    id either way, distinguishable by prefix and never confusable with a sha
    (40 hex has no colon)."""
    text = " ".join(str(question or "").split())
    if not text:
        return ""
    return QUESTION_SUBJECT_PREFIX + hashlib.blake2b(
        text.encode("utf-8"), digest_size=16).hexdigest()


def evidence_digest(tip, verdict, evidence, room="", epoch="", seat=""):
    """The signal's FULL BINDING (codex-3's design-lock, now honoured whole):
    council room + epoch + exact tip + verdict + evidence + MEMBER. The first
    version covered only tip+verdict+evidence, so the same judgment digested
    identically across councils, across epochs, and across members — an
    idempotency key that cannot tell two different assertions apart is not a
    binding. Every field is RS-joined so no value can impersonate a boundary."""
    raw = "\x1e".join([str(room or ""), str(epoch or ""), str(seat or ""),
                       str(tip or ""), str(verdict or ""), str(evidence or "")])
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


TERMINAL = ("revealed", "aborted")


def _valid(reg, room):
    """(reg, err) — STRICT schema on every read (codex-3 re-gate: a registry
    whose `signals` was a list crashed the verbs, and an unknown `status`
    sailed straight past the terminal guards into accepting a signal). A
    malformed registry is UNKNOWN and read-only, never a usable council."""
    if not isinstance(reg, dict):
        return None, "council registry for %s is unreadable — treat as UNKNOWN" % room
    if not isinstance(reg.get("signals"), dict):
        return None, ("council registry for %s is malformed (signals is %s, "
                      "expected an object) — refusing to act on it"
                      % (room, type(reg.get("signals")).__name__))
    if not isinstance(reg.get("members"), list) or not reg["members"]:
        return None, "council registry for %s is malformed (members)" % room
    if not isinstance(reg.get("threshold"), int):
        return None, "council registry for %s is malformed (threshold)" % room
    if reg.get("status") not in ("open",) + TERMINAL:
        return None, ("council registry for %s carries an unknown status %r — "
                      "refusing to act on it" % (room, reg.get("status")))
    return reg, None


def _held(room):
    """The WHOLE lifecycle serializes on one lock (codex-3 re-gate: only
    signal() took it, so convene/reveal/abort raced each other and could
    interleave with a signal mid-write)."""
    from . import eventledger
    return eventledger.locked(registry_path(room))


def convene(room, members, epoch, threshold=None, convener=None,
            tip=None, question=None):
    """Open the embargoed quorum for an already-seeded council room.

    `threshold` defaults to MAJORITY (see default_threshold): unanimity plus
    an embargo is an undiagnosable wedge. Any K is settable explicitly —
    `--threshold N` for unanimity when a decision genuinely needs every
    voice — and the bar is printed on every surface, so it is never silent."""
    members = [m for m in dict.fromkeys(members or []) if m]
    if not members:
        return None, "council needs at least one member"
    # A NAMED SUBJECT IS REQUIRED, not an artifact specifically. codex-3's
    # re-gate made the TIP required and their reason survives intact: an
    # optional tip "fell back to first-signal selection, which lets the fastest
    # member choose the question every other member is answering". That hazard
    # is about NAMING THE SUBJECT AT CONVENE, not about it being a sha — so a
    # QUESTION fixed at convene satisfies it exactly as a tip does, and a
    # council with neither is still a vibe and still refused.
    tip = str(tip or "").strip()
    question = " ".join(str(question or "").split())
    if tip and question:
        return None, ("a council judges ONE subject — pass --tip for an "
                      "artifact or --question for a design decision, never both")
    subject = tip or question_subject(question)
    if not subject:
        return None, ("a council judges a NAMED subject — pass --tip <sha> for "
                      "an artifact, or --question <text> for a design decision, "
                      "so every member rules on the same thing")
    k = default_threshold(len(members)) if threshold is None else int(threshold)
    if not 1 <= k <= len(members):
        return None, ("threshold %d out of range — a council of %d members "
                      "needs 1..%d" % (k, len(members), len(members)))
    # EXISTENCE, not parseability (codex-3 xrev: registry() returns None for a
    # CORRUPT file exactly as it does for a missing one, so a damaged registry
    # read as "no council here" and convene OVERWROTE it — destroying sealed
    # judgments. A file we cannot read is the one we must never clobber.)
    with _held(room) as held:
        if not held:
            return None, "council registry unwritable (%s)" % registry_path(room)
        # EXISTENCE IS NOT VALID STATE (codex-3 re-gate). Keying the refusal on
        # the PATH made any junk file — an id-less row, a truncated write, an
        # empty touch — squat that council name permanently: the official API
        # could never append a first valid convene, with no recovery but
        # deleting a file by hand. Distinguish the three cases honestly:
        #   unavailable -> UNKNOWN, refuse and say so (never assume it is free)
        #   a valid convene present -> already convened
        #   readable, no valid convene -> the name is FREE; append.
        # ACT ON THE MIGRATION'S RESULT (codex re-gate): ignoring it made a
        # FAILED import indistinguishable from "nothing to import", and convene
        # then opened a fresh council over an already-aborted snapshot.
        if _import_legacy(room) == "failed":
            return None, ("could not migrate the pre-ledger snapshot for %s — "
                          "refusing to convene over state I cannot establish"
                          % room)
        existing, unavailable = read(room)
        if unavailable:
            return None, ("council ledger for %s is UNREADABLE (%s) — refusing "
                          "to convene over state I cannot see; this is UNKNOWN, "
                          "not empty" % (room, unavailable))
        if existing is not None:
            return None, "council already convened for room %s" % room
        # the ARTIFACT is fixed HERE, by the convener, not by whoever signals
        # first (codex-3 re-gate): members must know what they are judging
        # before any of them rules, and a first-signal-selected target lets the
        # fastest member choose the question.
        if not _emit(room, {"event": "convene", "epoch": int(epoch),
                            "convener": convener, "members": members,
                            "threshold": k, "tip": subject,
                            "question": question}):
            return None, "council ledger unwritable (%s)" % registry_path(room)
        reg = registry(room)
    return reg, None


def signal(room, seat, verdict, tip, evidence):
    """Seal ONE member's judgment. Actor-gated on the exact member set; a
    non-member signal is refused (it can never reach quorum, so accepting it
    would be a lie about the tally). An IDENTICAL retry is idempotent — the
    same member re-sending the same judgment is a retry, not a second vote —
    but a CONFLICTING second signal is REFUSED, never a silent overwrite: a
    recorded judgment that can be quietly replaced is not a judgment."""
    verdict = str(verdict or "").strip().upper()
    if verdict not in VERDICTS:
        return None, "verdict must be one of %s" % "|".join(VERDICTS)
    # A SIGNAL BINDS THE COUNCIL'S OWN SUBJECT, whatever kind it is. Requiring
    # the caller to re-supply a tip made a question council unsignallable, and
    # re-deriving the subject here would reintroduce exactly what codex-3's
    # re-gate forbade — a member choosing what they are answering. The subject
    # was fixed at convene; a signal reads it rather than naming it.
    tip = str(tip or "").strip()
    # READ-MODIFY-WRITE UNDER THE LOCK (codex-3 xrev of the council landing: the unlocked
    # version LOST UPDATES — two members signalling concurrently each read the
    # same registry and the second write erased the first, silently
    # under-counting a quorum). Same discipline as the dispatch ledger.
    with _held(room) as held:
        if not held:
            return None, ("council registry unwritable (%s) — signal NOT "
                          "recorded" % registry_path(room))
        # TRI-STATE, never binary (codex-3 re-gate): "could not read the
        # ledger" must not answer with the confident "no council convened".
        reg, unavailable = read(room)
        if unavailable:
            return None, ("council ledger for %s is UNREADABLE (%s) — this is "
                          "UNKNOWN, not empty" % (room, unavailable))
        if not reg:
            return None, "no council convened for room %s" % room
        if reg.get("status") == "aborted":
            return None, ("council %s was ABORTED — nothing further is "
                          "recorded" % room)
        if reg.get("status") == "revealed":
            # TERMINAL (codex-3 xrev): a judgment cast once the others are
            # visible is not INDEPENDENT — the one property the embargo
            # exists to guarantee. Reveal closes the council.
            return None, ("council %s is already REVEALED — a judgment cast "
                          "after the embargo lifts is not independent; "
                          "reconvene on the superseding tip" % room)
        if seat not in reg["members"]:
            # every identity in an EMITTED string is laundered — a member name
            # is a stored chat-row identity and this error prints
            return None, ("%s is not a member of council %s (members: %s) — a "
                          "non-member signal can never reach quorum" %
                          (chat._dsan(seat), room,
                           ",".join(chat._dsan(m) for m in reg["members"])))
        # ONE COUNCIL, ONE ARTIFACT (codex-3 xrev: signals binding DIFFERENT
        # tips could reach "quorum" over judgments about different code — a
        # tally that means nothing). The first signal fixes the bound tip.
        bound = reg.get("tip")
        # ADOPT THE COUNCIL'S OWN SUBJECT when the caller names none. A member
        # answering a QUESTION council has no sha to pass, and demanding one
        # made such a council unsignallable. Adopting is not first-signal
        # selection — the subject was fixed at convene and this reads it; the
        # DISAGREEMENT refusal below is untouched, so a member who names a
        # DIFFERENT subject is still refused exactly as before.
        if bound and not tip:
            tip = bound
        if bound and tip != bound:
            # NAME THE SUBJECT KIND. "bound to tip <sha>" is the accurate
            # sentence for an artifact council and a lie for a question one,
            # and the existing guard tests pin that word because the wording IS
            # the contract a reader acts on.
            kind = ("question" if bound.startswith(QUESTION_SUBJECT_PREFIX)
                    else "tip")
            return None, ("council %s is bound to %s %s, not %s — a quorum "
                          "must judge ONE subject; reconvene for the new one"
                          % (room, kind, bound[:20], tip[:20]))
        dig = evidence_digest(tip, verdict, evidence, room=room,
                              epoch=reg.get("epoch"), seat=seat)
        prior = reg["signals"].get(seat)
        if prior:
            if prior.get("digest") == dig:
                return reg, None                  # identical retry — idempotent
            return None, ("%s already signalled council %s with a DIFFERENT "
                          "judgment — a sealed verdict is never silently "
                          "replaced (ABORT and reconvene on the superseding "
                          "tip)" % (chat._dsan(seat), room))
        if not _emit(room, {"event": "signal", "epoch": reg["epoch"], "seat": seat,
                            "verdict": verdict, "tip": tip,
                            "evidence": str(evidence or ""), "digest": dig}):
            return None, ("council ledger unwritable (%s) — signal NOT "
                          "recorded" % registry_path(room))
        reg = registry(room)
    return reg, None


def tally(room):
    """(signed, threshold, is_open) — the ONLY thing readable before quorum.
    Deliberately COUNTS without naming: a partial signer list is exactly the
    contamination the embargo exists to prevent (knowing WHO has already
    ruled is itself an anchor). A malformed registry counts as nothing rather
    than crashing its callers."""
    reg, err = _valid(registry(room) or {}, room)
    if err:
        return 0, 0, False
    n = len(reg["signals"])
    k = int(reg["threshold"])
    return n, k, n >= k and reg.get("status") != "aborted"


def outcome(signals):
    """(label, counts) — what the revealed judgments ADD UP TO.

    Quorum is a REVEAL bar, not a decision (codex-3 re-gate: YES+NO reached
    reveal with no aggregate rule, and calling that 'ratified' would be a
    surface lying about what happened). A ratification needs POSITIVE support,
    so the rule is deliberately conservative: more YES than NO carries;
    anything else — a tie, more NO, or all abstentions — does NOT. Abstentions
    count toward reaching quorum and never toward carrying the decision."""
    c = {v: 0 for v in VERDICTS}
    for s in signals or []:
        if s.get("verdict") in c:
            c[s["verdict"]] += 1
    if c["YES"] > c["NO"]:
        return "CARRIED", c
    if c["YES"] == c["NO"]:
        return "NOT CARRIED (tie — ratification needs positive support)", c
    return "NOT CARRIED", c


def reveal(room):
    """(signals, err) — the embargo LIFTS only at quorum. Below it, this
    returns the honest refusal rather than a partial list, and it never
    reveals the signer set (see tally). Serialized with every other lifecycle
    verb so a reveal cannot interleave with a signal mid-write."""
    with _held(room) as held:
        if not held:
            return None, "council registry unwritable (%s)" % registry_path(room)
        # TRI-STATE, never binary (codex-3 re-gate): "could not read the
        # ledger" must not answer with the confident "no council convened".
        reg, unavailable = read(room)
        if unavailable:
            return None, ("council ledger for %s is UNREADABLE (%s) — this is "
                          "UNKNOWN, not empty" % (room, unavailable))
        if not reg:
            return None, "no council convened for room %s" % room
        if reg.get("status") == "aborted":
            return None, ("council %s was ABORTED — there is nothing to reveal"
                          % room)
        n = len(reg["signals"])
        k = int(reg["threshold"])
        if n < k:
            return None, ("council %s is EMBARGOED — %d of %d signalled; "
                          "nothing (not even who) is revealed before quorum"
                          % (room, n, k))
        if reg.get("status") != "revealed":
            if not _emit(room, {"event": "reveal", "epoch": reg["epoch"]}):
                return None, ("council ledger unwritable (%s) — the embargo "
                              "stays sealed rather than lifting unrecorded"
                              % registry_path(room))
            reg = registry(room)
        return [reg["signals"][m] for m in reg["members"]
                if m in reg["signals"]], None


def abort(room, seat, reason):
    """The honest escape hatch (design-lock): when members must COLLABORATE
    before quorum, the council does not partially leak — it dies, the work
    moves to a standup, and a fresh council convenes on the superseding tip.
    Aborting seals the embargo permanently: an aborted council never reveals,
    because a judgment formed under collaboration is not independent."""
    with _held(room) as held:
        if not held:
            return None, "council registry unwritable (%s)" % registry_path(room)
        # TRI-STATE, never binary (codex-3 re-gate): "could not read the
        # ledger" must not answer with the confident "no council convened".
        reg, unavailable = read(room)
        if unavailable:
            return None, ("council ledger for %s is UNREADABLE (%s) — this is "
                          "UNKNOWN, not empty" % (room, unavailable))
        if not reg:
            return None, "no council convened for room %s" % room
        # TERMINAL TRUTH IS WRITE-ONCE (codex-3 re-gate: abort-after-reveal and
        # a repeated abort both overwrote the recorded end state — a second
        # abort could rewrite who aborted it and why, and aborting a REVEALED
        # council would retroactively unpublish judgments already read).
        if reg.get("status") in TERMINAL:
            return None, ("council %s is already %s — a terminal state is "
                          "written once and never rewritten (reconvene on the "
                          "superseding tip)" % (room, reg["status"].upper()))
        if seat not in reg["members"] and seat != reg.get("convener"):
            return None, ("%s is not a member of council %s"
                          % (chat._dsan(seat), room))
        if not _emit(room, {"event": "abort", "epoch": reg["epoch"], "seat": seat,
                            "reason": str(reason or "")[:256]}):
            return None, ("council ledger unwritable (%s) — abort NOT recorded"
                          % registry_path(room))
        reg = registry(room)
    return reg, None


def subject_lines(reg):
    """The SUBJECT, named on the surface a member reads before voting.

    A council printed its room, its bar and its members and NEVER what it was
    judging — so a member saw "COUNCIL x EMBARGOED, members: a,b,c" and had to
    go find the artifact themselves. That was survivable while the subject was
    always a sha someone had just pasted into chat; it is not survivable for a
    QUESTION council, where the subject IS the whole council and exists nowhere
    else. Computed, recorded, and discarded one frame before the reader is the
    shape this repo keeps finding, so the subject is now on the surface.
    """
    q = str((reg or {}).get("question") or "").strip()
    if q:
        return ["question: %s" % q]
    tip = str((reg or {}).get("tip") or "").strip()
    return ["artifact: %s" % tip[:12]] if tip else []


def sign_line(room, reg):
    """The sign instruction, matching the KIND of subject.

    Telling a QUESTION council's members to pass --tip <sha> is an instruction
    they cannot follow — there is no sha — and it was WRONG the moment tip-less
    councils existed. A member signs the council's own bound subject; for an
    artifact they may still name it explicitly."""
    if str((reg or {}).get("question") or "").strip():
        return ("helm chat verdict %s <%s> --evidence <ref>"
                % (room, "|".join(VERDICTS)))
    return ("helm chat verdict %s <%s> --tip <sha> --evidence <ref>"
            % (room, "|".join(VERDICTS)))


def status_lines(room, via="council"):
    """What anyone may see at any time: the BAR, never the count, never who.

    Tri-state like every other verb — this was the surface that told the
    confident lie, answering 'no council convened' when the truth was that the
    ledger could not be read at all."""
    reg, unavailable = read(room)
    if unavailable:
        return ["council ledger for %s is UNREADABLE (%s) — UNKNOWN, not "
                "empty; nothing here says a council does or does not exist"
                % (room, unavailable)]
    if not reg:
        return ["no council convened for room %s" % room]
    n, k, is_open = tally(room)
    if reg.get("status") == "aborted":
        return ["COUNCIL %s ABORTED by %s — %s (no reveal, ever)"
                % (room, chat._dsan(reg.get("aborted_by") or "?"),
                   reg.get("abort_reason") or "no reason recorded")]
    # ZERO-WHO ON THE PULL SURFACE TOO (codex-3 re-gate). Deleting the pushed
    # chat row closed only half the leak: this READ published "1 of 2" beside
    # the member list, so a member who knows they have not signalled learns by
    # subtraction exactly who has. The signalled COUNT is the leak — publish
    # the BAR (threshold of N members), never the progress, until the embargo
    # lifts. What you cannot see, you cannot be anchored by.
    bar = "%d of %d signalled (threshold %d of %d members)" % (
        n, k, k, len(reg["members"]))
    if not is_open:
        bar = "threshold %d of %d members" % (k, len(reg["members"]))
    if is_open:
        return ["COUNCIL %s QUORUM REACHED — %s" % (room, bar),
                "reveal: helm chat reveal %s" % room]
    # SAY the unanimity case rather than let it be discovered when the council
    # wedges. Majority of 2 IS 2, so the commonest council (two reviewers)
    # defaults to needing BOTH — and with an embargo you cannot chase the
    # missing one, which is the wedge the majority default exists to avoid.
    # There is no threshold that fixes it: 1-of-2 is not a quorum, it is one
    # seat deciding alone. The honest move is to name it up front.
    if k == len(reg["members"]) and k > 1:
        return ["COUNCIL %s EMBARGOED — %s" % (room, bar),
                "members: %s" % ",".join(chat._dsan(m) for m in reg["members"]),
                ] + subject_lines(reg) + [
                "NOTE: this council needs EVERY member (majority of %d is %d). "
                "If one member goes quiet the council cannot conclude and the "
                "embargo hides which one — abort and reconvene, or convene "
                "with more members." % (len(reg["members"]), k),
                "nothing — not the verdicts, not WHO, not even HOW MANY — is "
                "revealed before quorum. sign: " + sign_line(room, reg)]
    return ["COUNCIL %s EMBARGOED — %s" % (room, bar),
            "members: %s" % ",".join(chat._dsan(m) for m in reg["members"]),
            ] + subject_lines(reg) + [
            # vocabulary from the constant, never a literal — a stale
            # copy told operators to type CLEAR/FIX after the decision-shape
            # rename (dogfood-caught 2026-07-24, fixed in-pass)
            "nothing — not the verdicts, not WHO, not even HOW MANY — is "
            "revealed before quorum. sign: " + sign_line(room, reg)]
