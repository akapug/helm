"""Prospective bounded-review exception, ONLY for compose then landed close.

Source hashes identify artifacts, not the truth of an effect judgment. The
reviewer confirms that judgment; the exact-owner diff guard independently
vetoes known contract owners. This is a conservative guard, not a theorem of
semantic completeness. Replay checks the captured observations and immutable
ledger inputs; the writer additionally measures Git and gate evidence live.
"""
import hashlib
import json
import os
import re

from . import dispatches, rowworld, vcs

MARKER = "helm-compose-land/1"
WRITER_ENV = "HELM_COMPOSE_LAND_V1"
PROOF_VERSION = 3  # landed versions 1 (review) and 2 (build) are occupied
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BRIEF = re.compile(
    r"helm-compose-land/1 base=([0-9a-f]{40}|[0-9a-f]{64}) "
    r"tip=([0-9a-f]{40}|[0-9a-f]{64}) bounded=1 effects=([a-z,-]+)\Z")
_DECLARATIONS = frozenset(("reversible", "schema", "ledger-version", "hooks",
                           "cross-seat", "administrative-terminal",
                           "ledger-mutation", "unknown"))

# Exact canonical owner filenames, not path-pattern classification. Changes to
# this list are themselves CONTRACT. Adjacent helper sweep: landgate's
# changed_files is whitespace-split and unsuitable for rename/delete evidence;
# rowworld owns immutable bounds; vcs owns the byte-preserving Git call.
# Harness census follows hookrun.dispatch_specs: hooks.SPECS, cred's
# GUARD_SPECS, record's HOOK_SPECS, plus their install/order/blocking owners.
PROTECTED_OWNERS = (
    "helm/compose_contract.py",  # this exception, parser and effect constructor
    "helm/work/_guard.py",      # reference-transaction/pre-commit generators
    "helm/work/_cli.py",        # guard installation command routing
    "helm/nevertrack.py",       # installed pre-commit scanner
    "helm/vacuous_assertion.py",  # installed pre-commit test advisory
    "helm/hostpath_guard.py",   # installed pre-push scanner
    "helm/inflight_gate.py",    # installed pre-commit gate interlock
    "helm/hardcode.py",         # installed pre-commit portability scanner
    "helm/docref_guard.py",     # installed pre-commit citation scanner
    "helm/conflict_marker.py",  # installed pre-commit conflict scanner
    "helm/world_prose_guard.py",  # installed public-prose scanner
    "helm/seatname_guard.py",   # installed pre-commit identity scanner
    "helm/trailer_rung.py",     # installed commit-msg attribution scanner
    "helm/lane_discipline.py",  # installed pre-commit lane interlock
    "helm/hooks.py",            # harness hook specifications and installation
    "helm/cred/_common.py",     # active/retired credential hook declarations
    "helm/cred/cli.py",         # credential hook retirement routing
    "helm/record.py",           # HOOK_SPECS and deployed recorder hook contract
    "helm/hookrun.py",          # complete hook registry, order and blocking fold
    "helm/foldcompose.py",      # canonical composition manifest/replay authority
    "helm/foldcheck.py",        # five fold rungs and shared tree/gate authority
    "helm/injection_schema.py",  # versioned injection event schema
    "helm/inject/_ledger.py",   # injection ledger protocol/version and mutation
    "helm/seat_ledger.py",      # SCHEMA_V; seat administrative terminals
    "helm/eventledger.py",      # append/locking/framing shared by ledgers
    "helm/dispatches.py",       # verdict versions; administrative close/replay
    # THE SATELLITES A SIZE CEILING FORCED OUT, AND WHY THEY ARE LISTED
    # SEPARATELY. This guard names exact FILES, so a ledger that sheds a
    # module sheds its protection with it: the close/terminal writers and the
    # verb surfaces these four entries protect were inside the two files above
    # them until a never-track ceiling moved them, and nothing about the
    # contract they own changed. The arm below derives this requirement from
    # each owner's own _OWNER_NAMES table, so the next split cannot reopen it.
    "helm/dispatches_close.py",  # administrative close/terminal proof writers
    "helm/dispatches_cli.py",   # the verb surface over that close/replay
    "helm/landreq_close.py",    # terminal/witness close writers and chain proof
    "helm/landreq_cli.py",      # the lr verb surface over those writers
    "helm/landreq.py",          # LAND_SCHEMA; compose/terminal/witness writers
    "helm/verdicts.py",         # cross-seat polarity and basis vocabulary
    "helm/verdict_tier.py",     # cross-seat captured approval policy contract
    "helm/store/policy_history.py",  # immutable policy version ledger
    "helm/store/load.py",       # certain-policy schema and ambiguity contract
    "helm/seats_roster.py",     # cross-seat roster/session and admin mutations
    "helm/seats_common.py",     # canonical roster write acquisition/preservation
    "helm/gate.py",             # receipt versions and authority binding
    "helm/gateauthority.py",    # RECEIPT_VERSION and gate authority schema
    "helm/gateimport.py",       # imported receipt/binding versions
    "helm/fabgate.py",          # request/handle/event version contracts
    "helm/landgate.py",         # cross-seat five-clause landing contract
    "helm/rowstate.py",         # terminal obligation interpretation
    "helm/rowworld.py",         # cross-seat work bounds and proof observations
    "helm/vcs.py",              # byte-preserving diff/proof observation backend
    "helm/actors.py",           # ACTOR_STORE_VERSION; attributed capabilities
    "helm/whoami.py",           # SCHEMA_VERSION; cross-seat runtime identity
    "helm/mcpd.py",             # cross-process PROTOCOL_VERSION
    "helm/tasks.py",            # task ledger mutation and closure
    "helm/store/write.py",      # premise mutation and administrative retirement
    "helm/premise/_chain.py",   # append-only premise attestation chain
    "helm/work/_claims.py",     # cross-seat lease mutation
    "helm/seats_claims.py",     # cross-seat resource claim contracts
    "helm/seat_lifecycle.py",   # lifecycle dispatch and rebind administration
    "helm/seat_exit_owner.py",  # administrative seat terminal owner
    "helm/seat_reassign.py",    # administrative authority transfer
    "helm/seats_rename.py",     # cross-seat identity migration
)


def writer_error():
    """Operator rollout assertion, NOT proof that readers were deployed."""
    if os.environ.get(WRITER_ENV) != "1":
        return ("compose-land writer disabled; deploy compatible readers first, "
                "then explicitly enable " + WRITER_ENV + "=1")
    return None


def parse(row, rows):
    """(immutable contract, error); absence/ambiguity is never zero findings."""
    if not isinstance(rows, dict):
        return None, "UNKNOWN: immutable work snapshot unavailable"
    # THE WHOLE BRIEF, BY REFERENCE WHEN THE ROW HAS ONE. This reader needs the
    # COMPLETE text by construction — it hashes the brief and binds the digest
    # to `message_hash` below — so under the row cap alone it answered UNKNOWN
    # for every brief over that cap, which is now the ordinary size. Reading
    # the referenced file restores the contract for exactly those rows and
    # changes nothing for the rest: a row with no reference, an unresolvable
    # one, or a truncated copy still falls to the same UNKNOWN, and the digest
    # bind below is a SECOND, independent check on whatever text was read.
    body, absent, problem = dispatches.brief_of(row)
    if absent or problem or not isinstance(body, str) \
            or dispatches.BODY_TRUNCATED_MARK in body:
        return None, "UNKNOWN: original complete bounded brief unavailable"
    if body.count(MARKER) != 1:
        return None, "UNKNOWN: brief must contain exactly one prospective opt-in"
    first, sep, scope = body.partition("\n")
    match = _BRIEF.fullmatch(first)
    if not match or not sep or not scope.strip():
        return None, "UNKNOWN: bounded brief grammar or scope is missing"
    base, tip, effects = match.groups()
    declared = effects.split(",")
    if len(set(declared)) != len(declared) or not set(declared) <= _DECLARATIONS:
        return None, "UNKNOWN: ambiguous effect declaration"
    try:
        digest = hashlib.blake2b(body.encode("utf-8"), digest_size=16).hexdigest()
    except UnicodeError:
        return None, "UNKNOWN: original brief is not UTF-8"
    if digest != row.get("message_hash"):
        return None, "UNKNOWN: original brief hash does not bind"
    if row.get("kind") != "review" or row.get("status") != "verdict" \
            or row.get("polarity") != "concur" or row.get("basis") != "measured":
        return None, "UNKNOWN: requires review CONCUR/MEASURED"
    if tip != row.get("tip") or tip != row.get("reviewed_tip") \
            or (base, tip) != rowworld._work_pair(row, rows, {}):
        return None, "UNKNOWN: bounded range does not match immutable work pair"
    expected = (MARKER + " brief=" + digest
                + " pass=complete findings=0 effects=confirmed")
    if row.get("verdict_ref") != expected \
            or row.get("exit_answer") or row.get("worse_than_main_paths"):
        return None, "UNKNOWN: needs positive complete pass, zero findings and effects confirmation"
    return {"base": base, "tip": tip, "brief": digest,
            "effects": declared}, None


def _diff_records(raw):
    """Strict NUL-framed name-status; both rename/copy endpoints participate."""
    if not isinstance(raw, bytes) or not raw or not raw.endswith(b"\0"):
        return None
    try:
        tokens = raw[:-1].decode("utf-8", "strict").split("\0")
    except UnicodeError:
        return None
    records, i = [], 0
    while i < len(tokens):
        status = tokens[i]
        count = 2 if re.fullmatch(r"[RC](?:100|0[0-9]{2})", status) else 1
        if count == 1 and status not in ("A", "M", "D", "T"):
            return None
        paths = tokens[i + 1:i + 1 + count]
        if len(paths) != count or any(not p or p.startswith("/") or "\\" in p
                or any(c in ("", ".", "..") for c in p.split("/"))
                for p in paths):
            return None
        records.append({"status": status, "paths": paths})
        i += 1 + count
    return records


def measure_diff(repo, base, tip):
    """Every commit, not the net diff: modifying then reverting an owner vetoes.

    Only a contiguous single-parent range is admitted. Merge/empty commits or
    unavailable observations are UNKNOWN, not an empty protected-owner set.
    """
    if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (base, tip)) or base == tip:
        return None
    chain = _text(repo, "rev-list", "--reverse", "--parents", base + ".." + tip)
    if not chain:
        return None
    records, parent = [], base
    for line in chain.splitlines():
        parts = line.split()
        if len(parts) != 2 or not SHA.fullmatch(parts[0]) or parts[1] != parent:
            return None
        try:
            rc, raw, _err = vcs.backend(repo).run(
                repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status",
                "-z", "--find-renames", parent, parts[0], "--")
        except (OSError, ValueError):
            return None
        observed = _diff_records(raw) if rc == 0 else None
        if not observed:
            return None
        records.extend(observed)
        parent = parts[0]
    return records if parent == tip else None


def effects(declared, diff):
    """Two independent inputs: attributed judgment AND known-owner veto."""
    if not isinstance(declared, list) or not declared \
            or any(not isinstance(x, str) for x in declared) \
            or len(set(declared)) != len(declared) \
            or not set(declared) <= _DECLARATIONS or "unknown" in declared:
        return "UNKNOWN", "effect declaration is unknown"
    if not isinstance(diff, list) or not diff:
        return "UNKNOWN", "protected diff unavailable or empty"
    # Replay validates precisely the same grammar as the live byte reader.
    try:
        raw = b"".join((r["status"] + "\0" + "\0".join(r["paths"]) + "\0")
                       .encode("utf-8") for r in diff if set(r) == {"status", "paths"})
        if _diff_records(raw) != diff:
            return "UNKNOWN", "protected diff malformed"
    except (KeyError, TypeError, AttributeError, UnicodeError):
        return "UNKNOWN", "protected diff malformed"
    touched = sorted({p for r in diff for p in r["paths"]} & set(PROTECTED_OWNERS))
    if touched or declared != ["reversible"]:
        return "CONTRACT", "APPROVE required: " + (", ".join(touched) or "declared protected effect")
    return "REVERSIBLE", None


def admission(row, rows, repo):
    contract, err = parse(row, rows)
    if err:
        return None, err
    tier, why = dispatches.approval_tier_for_verdict(row)
    if tier not in ("ok", "none"):
        return None, "recorded reviewer tier refuses: " + str(why)
    diff = measure_diff(repo, contract["base"], contract["tip"])
    state, why = effects(contract["effects"], diff)
    if state != "REVERSIBLE":
        return None, state + ": " + str(why)
    return {"contract": contract, "diff": diff}, None


def read_manifest(path):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None, "compose manifest exceeds bound"
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate manifest key")
                result[key] = value
            return result
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as exc:
        return None, "compose manifest unreadable: " + str(exc)
    if not isinstance(result, dict) or result.get("compose_land_version") != 1 \
            or type(result.get("compose_land_version")) is not int \
            or result.get("dry_run") is not False:
        return None, "needs a standing prospective compose manifest"
    return result, None


def _text(repo, *args):
    try:
        rc, text, _err = vcs.backend(repo).text(repo, *args)
    except (OSError, ValueError, UnicodeError):
        return None
    return text if rc == 0 else None


def _identities(repo, base, tip):
    from . import landreq
    text = _text(repo, "rev-list", "--reverse", base + ".." + tip)
    if not text:
        return None
    out = []
    for sha in text.split():
        parents = _text(repo, "rev-list", "--parents", "-n", "1", sha)
        try:
            identity = landreq._commit_content_identity(repo, sha)
        except (OSError, ValueError, UnicodeError):
            return None
        if not parents or len(parents.split()) != 2 or not identity \
                or len(identity) != 3 or any(not x or x == "EMPTY" for x in identity):
            return None
        out.append({"sha": sha, "parent": parents.split()[1],
                    "identity": list(identity)})
    return out


def _same_content(source, carried):
    return (source[0] == carried[0] or source[1] == carried[1]) \
        and source[2] == carried[2]


def group_member(repo, row, rows, cars):
    """Derive one COMPLETE bounded member from canonical car addresses.

    Replay of the whole chain is a separate obligation at both consumers.
    No recorded member bounds, subset of ancestors, or content hashes supplied
    by the manifest can replace the immutable source range and live identities.
    """
    admitted, err = admission(row, rows, repo)
    if err:
        return None, err
    if vcs.backend(repo).common_dir(repo) != row.get("repo_id"):
        return None, "bounded review belongs to another repository"
    bounded = admitted["contract"]
    source = _identities(repo, bounded["base"], bounded["tip"])
    if not source or [c["source_commit"] for c in cars] != [s["sha"] for s in source]:
        return None, "bounded cars must cover the entire immutable source range in order"
    parent = _text(repo, "rev-parse", cars[0]["result_commit"] + "^")
    tip = cars[-1]["result_commit"]
    carried = _identities(repo, parent, tip) if parent else None
    if not carried or [c["result_commit"] for c in cars] != [s["sha"] for s in carried] \
            or len(source) != len(carried) \
            or any(not _same_content(a["identity"], b["identity"])
                   for a, b in zip(source, carried)):
        return None, "bounded result cars change content or are not contiguous"
    return {"id": row["id"], "approved_tip": bounded["tip"],
            "compose_land": {"source_base": bounded["base"],
                             "base": parent, "tip": tip}}, None


def canonical_manifest(repo, candidate, authority, receipt, consuming=None):
    """Resolve a candidate against the sole project-home artifact, then replay.

    Output/sidecars are not authority: even an identical copy cannot substitute
    for an absent canonical record. Same-tip peers share only derived location.
    """
    from . import foldcompose
    common = vcs.backend(repo).common_dir(repo)
    root = os.path.dirname(common) if common else None
    state, project = foldcompose.project_state(root)
    if state != "registered":
        return None, "canonical composition project is " + state
    tip = candidate.get("result_tip")
    if not isinstance(tip, str) or not SHA.fullmatch(tip):
        return None, "canonical composition tip unreadable"
    active, why = foldcompose.in_scope(repo, project, tip)
    if active is not True:
        return None, "canonical composition activation refuses: " + why
    record = foldcompose.read_manifest(project, tip)
    if record is None or record != candidate:
        return None, "canonical composition manifest/tree missing, malformed or changed"
    # `consuming` IS THE ROW'S OWN CHECKOUT, carried through to the composed
    # suite's gate binding. `repo` here is the repository (`repo_id`), and the
    # whole-tree bind below it asks which command a LOCATION declares — a
    # question the shared admin dir cannot answer for a repository whose root
    # and worktrees declare different ones.
    checks, members = foldcompose.record_proof(repo, record, authority, receipt,
                                               consuming=consuming)
    failures = [name + ": " + str(why) for name, ok, why in checks if ok is not True]
    if not checks or failures:
        return None, "canonical composition refuses: " + "; ".join(failures)
    if not members:
        return None, "canonical composition has no bounded members"
    return {"composed_tip": tip, "composed_tree": _text(repo, "rev-parse", tip + "^{tree}"),
            "members": list(members.values())}, None


def capture(row, rows, repo, pinned, manifest, receipt, authority=None):
    """Live writer proof; manifest supplies candidates, never authority."""
    from . import foldcompose, gate, landgate
    if authority is None:
        authority, err = foldcompose._review_snapshot()
        if err:
            return None, err
    # Admission, ordinary review history and bounded peers all use ONE instant.
    # The incoming row supplies an ADDRESS only. project_raw may annotate its
    # raw dictionaries for board scope; never treat those annotations as ledger
    # evidence or compare them with an unannotated canonical snapshot.
    rows = authority[0]
    row = rows.get(row.get("id")) if isinstance(row, dict) else None
    if not isinstance(row, dict):
        return None, "canonical dispatch row unavailable; retry scoped close"
    proof, err = admission(row, rows, repo)
    if err:
        return None, err
    if not isinstance(manifest, dict) or manifest.get("compose_land_version") != 1 \
            or type(manifest.get("compose_land_version")) is not int \
            or manifest.get("dry_run") is not False:
        return None, "needs a prospective standing composition"
    manifest, err = canonical_manifest(repo, manifest, authority, receipt,
                                       consuming=row.get("repo_root"))
    if err:
        return None, err
    composed = manifest.get("composed_tip")
    tree = manifest.get("composed_tree")
    if not all(isinstance(x, str) and SHA.fullmatch(x)
               for x in (composed, tree, pinned)):
        return None, "composition tip/tree/pin unreadable"
    if _text(repo, "rev-parse", composed + "^{tree}") != tree \
            or _text(repo, "rev-parse", pinned + "^{tree}") != tree:
        return None, "whole composed tree is not the pinned landed tree"
    members = manifest.get("members")
    if not isinstance(members, list) or any(not isinstance(m, dict) for m in members):
        return None, "composition members unreadable"
    # Same-tip peers still need THEIR OWN brief/verdict. Only content location
    # is shared; a peer need not have been selected by id in the compose call.
    matches = [m for m in members if m.get("approved_tip") == row["reviewed_tip"]]
    if len(matches) != 1:
        return None, "composition has no unique carrier for this reviewed tip"
    member = matches[0].get("compose_land")
    if not isinstance(member, dict) or set(member) != {"source_base", "base", "tip"} \
            or member["source_base"] != proof["contract"]["base"] \
            or not all(isinstance(x, str) and SHA.fullmatch(x) for x in member.values()):
        return None, "composition member range does not bind the bounded brief"
    if _text(repo, "merge-base", "--is-ancestor", member["tip"], composed) is None:
        return None, "member is not on the composed tree"
    source = _identities(repo, proof["contract"]["base"], row["reviewed_tip"])
    carried = _identities(repo, member["base"], member["tip"])
    if not source or not carried or len(source) != len(carried) \
            or any(not _same_content(a["identity"], b["identity"])
                   for a, b in zip(source, carried)):
        return None, "composition changed the bounded review's content"
    # THE ROW'S CONSUMING CHECKOUT TRAVELS INTO THE CAPTURE. `repo` is the
    # repository; `repo_root` is the tree this row's answer is spent in, and a
    # repository whose root and worktrees declare different commands cannot
    # answer "which command" without it.
    state, gate_id, why = gate.bind(receipt, composed, repo_id=repo,
                                   reviewed_ts=row.get("ts"),
                                   consuming_repo=row.get("repo_root"),
                                   need=gate.NEED_SUITE)
    if state != "VERIFIED":
        return None, "whole composed-tree gate does not bind: " + str(why)
    state, why = landgate.gate_binds_tree(gate_id, tree, repo=repo, tip=pinned)
    if state != landgate.OK:
        return None, "whole composed-tree gate refuses: " + str(why)
    receipt_row, err = gate.by_id(gate_id)
    if err or not receipt_row or receipt_row.get("tree") != tree:
        return None, "composed gate source unavailable or changed"
    proof.update(v=1, composed_tip=composed, composed_tree=tree,
                 gate=gate_id, gate_head=receipt_row.get("head"),
                 source=source, carried=carried, member=member,
                 repo=repo, pinned=pinned)
    err = proof_error(proof, row, rows)
    return (None, err) if err else (proof, None)


def peer_pin(row, rows, source_id, repo, trunk_ref, manifest, receipt, authority=None):
    """Recover a peer's historical landing from a canonical closed row ADDRESS.

    Neither an arbitrary pin nor another review's authority is inherited. The
    source composition is revalidated, then the caller captures this peer's own
    bounded evidence independently. The locked writer repeats this lookup.
    """
    source = rows.get(source_id) if isinstance(rows, dict) and isinstance(source_id, str) else None
    if not isinstance(source, dict) or source.get("close_reason") != "landed" \
            or source.get("close_proof_version") != PROOF_VERSION \
            or source.get("repo_id") != repo or source.get("closing_repo_id") != repo \
            or source.get("closing_trunk_ref") != trunk_ref \
            or source.get("reviewed_tip") != row.get("reviewed_tip"):
        return None, "scoped sibling has no matching recorded landing source"
    pin = source.get("closing_trunk_sha")
    proof, err = capture(source, rows, repo, pin, manifest, receipt, authority=authority)
    if err or proof != source.get("compose_land_proof"):
        return None, err or "scoped sibling changes the recorded composition"
    return pin, None


def proof_error(proof, row, rows):
    """Captured Git facts plus immutable sources, shared by append and replay.

    Git observations are writer-attributed, like existing landed proofs.
    Gate receipts and verdict policy history are independently resolved.
    An anchor is a content checksum, NOT a signature or a defense against
    replacement of both the trusted ledger and its source observations.
    """
    keys = {"v", "contract", "diff", "composed_tip", "composed_tree", "gate",
            "gate_head", "source", "carried", "member", "repo", "pinned"}
    if not isinstance(proof, dict) or set(proof) != keys \
            or type(proof.get("v")) is not int or proof["v"] != 1:
        return "compose-land proof schema mismatch"
    contract, err = parse(row, rows)
    if err or contract != proof["contract"]:
        return err or "compose-land contract does not bind the standing verdict"
    tier, why = dispatches.approval_tier_for_verdict(row)
    if tier not in ("ok", "none"):
        return "recorded reviewer tier refuses: " + str(why)
    effect, why = effects(contract["effects"], proof["diff"])
    if effect != "REVERSIBLE":
        return effect + ": " + str(why)
    if not isinstance(proof["repo"], str) or proof["repo"] != row.get("repo_id") \
            or not all(isinstance(proof[k], str) and SHA.fullmatch(proof[k])
                       for k in ("composed_tip", "composed_tree", "pinned", "gate_head")) \
            or not isinstance(proof["gate"], str) \
            or not re.fullmatch(r"[0-9a-f]{16}", proof["gate"]):
        return "compose-land repository/tree/gate binding malformed"
    # Re-read the immutable gate artifact, never today's trunk or its ancestry.
    # Exact-head bind spends the existing authority primitive without a Git
    # range probe; the writer separately measured equality to the landed tree.
    from . import gate
    receipt_row, err = gate.by_id(proof["gate"])
    if err or not receipt_row or receipt_row.get("tree") != proof["composed_tree"] \
            or receipt_row.get("head") != proof["gate_head"]:
        return "compose-land gate artifact does not bind"
    # AND REPLAY ASKS THE SAME QUESTION THE CAPTURE ASKED. The coordinate is
    # read off the standing ROW, not added to `proof`: the proof schema is
    # compared byte for byte against every recorded landing (`peer_pin`), so a
    # new key there would refuse every proof written before it — and the row is
    # where the coordinate has always lived.
    state, gate_id, why = gate.bind("gate:" + proof["gate"], proof["gate_head"],
                                   repo_id=proof["repo"],
                                   consuming_repo=row.get("repo_root"),
                                   need=gate.NEED_SUITE)
    if state != "VERIFIED" or gate_id != proof["gate"]:
        return "compose-land gate authority refuses: " + str(why)
    member = proof["member"]
    if not isinstance(member, dict) or set(member) != {"source_base", "base", "tip"} \
            or member["source_base"] != contract["base"] \
            or not all(isinstance(x, str) and SHA.fullmatch(x) for x in member.values()):
        return "compose-land member bounds malformed"
    source, carried = proof["source"], proof["carried"]
    if not isinstance(source, list) or not source or not isinstance(carried, list) \
            or len(source) != len(carried):
        return "compose-land source/carrier census malformed"
    for chain, base, tip in ((source, contract["base"], contract["tip"]),
                             (carried, member["base"], member["tip"])):
        parent = base
        for item in chain:
            if not isinstance(item, dict) or set(item) != {"sha", "parent", "identity"} \
                    or not isinstance(item["sha"], str) or not SHA.fullmatch(item["sha"]) \
                    or item["parent"] != parent:
                return "compose-land commit sequence is not contiguous"
            identity = item["identity"]
            if not isinstance(identity, list) or len(identity) != 3 \
                    or any(not isinstance(x, str) for x in identity) \
                    or not SHA.fullmatch(identity[0]) \
                    or any(not re.fullmatch(r"[0-9a-f]{64}", x) for x in identity[1:]):
                return "compose-land content identity malformed"
            parent = item["sha"]
        if parent != tip:
            return "compose-land commit sequence does not reach its bound tip"
    if any(not _same_content(a["identity"], b["identity"])
           for a, b in zip(source, carried)):
        return "compose-land carried content differs"
    return None
