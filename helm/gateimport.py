#!/usr/bin/env python3
"""Import one remotely-minted gate receipt into the binding ledger — strictly.

WHY THIS VERB EXISTS. A receipt minted on another machine is a fact about a
tree, an interpreter and a suite — none of which stop being true in transit.
What transit CAN do is tamper, truncate, duplicate and orphan, and on
2026-08-03 it did something worse: seven remotely-minted rows reached the
binding ledger through a one-off hand append inside one seat's bind window
(#167). The rows were genuine; the PATH was undocumented, unrepeatable and
indistinguishable from forgery. This verb sanctions that movement by making
it verifiable, idempotent and auditable — and by existing, it makes the hand
append the wrong tool forever.

WHAT IT REFUSES TO BE. Not a second minting path, and not a weakening of the
verdict guard (bind-provenance-dont-weaken-the-guard): `verdict --approve`
still reads only the global ledger, learns nothing about artifact files, and
this module never widens what it accepts. The import VALIDATES the whole
transported object, then appends each content-addressed failure chunk and the
gate receipt with the same event content in the ledger's canonical JSON shape.
A valid module-timing sibling follows as advisory evidence; its absence,
malformation, conflict, or write failure cannot weaken or erase receipt authority.
The receipt content id is recomputed here and must match, so an edited status,
pasted base-check, doctored diagnostic, or missing identity chunk stops resolving
exactly as it would on the minting machine.

HOW AN OBJECT QUALIFIES, in refusal order — every gate names itself:
  1. the artifact parses and the target gate row is unambiguous (one gate row,
     or --id; referenced failure chunks travel beside it as the same object);
  2. the receipt version is one this helm KNOWS (imports are strict: an
     unknown future schema is REFUSED, never half-understood);
  3. every referenced chunk is present, valid, and reconstructs the receipt's
     declared complete failure identity record;
  4. every event fits the binding ledger's per-event byte limit;
  5. the recomputed receipt content id equals the stored id (tamper evidence);
  6. the cited head resolves in THIS repo and its tree matches the receipt's
     tree exactly — dirty-run receipts carry the head that was actually
     dispatched, so a receipt about a tree this repo cannot produce refuses;
  7. each content id is new, or its stored event has identical canonical
     content (idempotent: importing the same complete object twice is a no-op;
     the same id with DIFFERENT content is a conflict and refuses loudly);
  8. under the canonical binding ledger's own fail-closed lock, the complete
     receipt object is durably present and one versioned binding is appended
     for the pair (receipt, importing common-dir repository).
The binding records importing and origin repository identities, receipt/head/
tree, and the exact artifact byte identity. It survives worktree reaping and is
the ONLY import-placement authority. Fab's separate strict completion ledger
binds its request key/generation/runner/source/exit to that placed receipt while
leaving the historical binding grammar unchanged. Under one receipt-ledger lock, missing
identity chunks append first and the gate receipt appends LAST, so a crash can
leave only inert chunks, never a receipt referring to evidence this import did
not durably install. gate-imports.jsonl is an audit pointer written afterward;
hand-shaped audit syntax cannot bind. A historical pointer may be backfilled
only while its recorded repository and artifact still exist and re-verify
against the receipt now; missing or unreadable evidence remains UNKNOWN. No
origin host/run is inferred or accepted — the artifact does not carry those
facts.
"""
import hashlib
import json
import math
import os
import re
import stat
import sys
import time

from . import eventledger, gate, gateauthority, home, openflags, pk, projscope, seats, vcs

IMPORTS = "gate-imports.jsonl"
BINDINGS = "gate-import-bindings.jsonl"
FAB_COMPLETIONS = "gate-fab-completions.jsonl"
BINDING_EVENT = "gate-import-binding"
FAB_COMPLETION_EVENT = "gate-fab-completion"
FAB_COMPLETION_VERSION = 1
FAB_KEY_FORMAT = "helm-fab-gate-job-v2"
FAB_WHOLE_ARGV = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")
#: The cgroup regimes Fab's own node authority admits, in the order Fab ranks
#: them: the immutable runtime's generation cgroup first, then the direct-scope
#: regime it treats as historical. Fab's node reader holds exactly this set
#: (fab-gate-state, `cgroups`), and every Fab PRODUCER — `fab gate measure` and
#: the expected-runner check inside `fab gate submit` — emits only the first.
#: A reader here that admitted only the second refused every measurement Fab
#: currently makes, so the two halves of the typed boundary could not compose
#: at all: `helm gate fab contract` rejected Fab's own runner identity.
FAB_RUNNER_CGROUPS = ("systemd-user-generation-cgroup-v1",
                      "systemd-user-scope-or-loud-direct")
FAB_COMPLETION_REQUIRED = frozenset((
    "v", "event", "id", "ts", "receipt", "importing_repo", "artifact",
    "authority"))
FAB_AUTHORITY_REQUIRED = frozenset((
    "format", "key", "generation", "job_id", "host", "sha", "identity",
    "budgets", "source_artifact", "artifact_sha256", "exit"))
#: The one key an authority MAY add to FAB_AUTHORITY_REQUIRED: the dispatched
#: node's machine-id hash, carried from Fab's gate-job event. Optional because
#: completions written before Fab emitted it stay valid; never a wider set.
FAB_AUTHORITY_NODE_ID = "node_id"
FAB_ARTIFACT_REQUIRED = frozenset(("sha256", "bytes"))
BINDING_VERSION = 1
BINDING_REQUIRED = frozenset((
    "v", "event", "id", "ts", "receipt", "importing_repo", "origin_repo",
    "head", "tree", "artifact"))
ARTIFACT_REQUIRED = frozenset(("path", "sha256", "bytes"))
ARTIFACT_MAX_BYTES = 16 * 1024 * 1024
LEGACY_IMPORT_CUTOFF = "2026-08-27T15:23:06Z"
BINDING_CANDIDATE_BUDGET_S = 0.25
_CENSUS_TIMEOUT = "historical import repository census deadline exceeded"

# Receipt versions whose binding grammar this module fully understands.
# v4 (host-bound receipts) is now among them. The earlier refusal was written
# when `gate` itself minted only v3 and could not recompute an id over the host
# block — accepting it THEN would have validated nothing. That is no longer the
# shape: `_receipt_id` below is gate's OWN function, so the commit that taught
# gate `host-v4` taught this reader in the same breath, and this file ships in
# that same commit for exactly that reason.
#
# READER FIRST, WRITER LATER, and the gap between them was the point. A helm
# that minted v4 before every helm could read it would produce receipts whose id
# recomputes differently here and in `gate.receipts()` — refused loudly at
# import, and SILENTLY SKIPPED at bind.
#
# THAT GAP IS CLOSED: `gate._mint_result` stamps `"v": 4` with a `host` block and
# the ledger carries live v4 receipts beside v1-v3. Every version listed here
# binds, and a receipt's VERSION IS NOT AN AUTHORIZATION AXIS — `landreq
# .gate_requirement` is explicit that authorization is a property of the WRITER,
# "never of the clock and never of a version int". Reading an older receipt as
# second-class is how a verified round gets filed SUPERSEDE instead of APPROVE
# and rots into contrary debt on the land board (measured 2026-08-04: three rows,
# each prescribing a whole-suite re-gate to earn a "v4 APPROVE" that never existed
# — while 182 APPROVEs already stood on v3). Hence the docstring's law above:
# distinguishable must not mean second-class.
# 5 IS KNOWN AND 6 IS NOT, for opposite reasons. 5 is the cached-receipt
# kind (an `executed` key); it landed with its own schema, which is why the
# focused kind took 6 rather than making two grammars answer to one version
# int — under which every reader would call the other kind's honest rows
# tampered.
#
# 6 IS ABSENT FROM THE GENERIC ARTIFACT DOOR ON PURPOSE. Every check that door
# can run — content id, resolvable head and tree, even a sibling checksum — is a
# computation the SUBMITTER can run too, so it proves consistency, never that a
# focused child ran. A plain arriving v6 remains indistinguishable from an
# assembled scope claim and refuses.
#
# There is ONE travelling v6 path: gateroute's challenge-framed live session.
# The local Helm process chooses and ships the immutable tree, commands that
# tree's Helm to run focus, reads the receipt object and artifact from separately
# framed sections, binds host identity to the same SSH channel, and rechecks the
# caller tree after the run. It passes that custody record directly to
# import_receipt; no CLI flag can manufacture it. The routed receipt must match
# the framed object byte-for-byte before the ordinary hash/tree/import ladder
# runs. gate._bind_focused still re-derives every scope field at verdict time.
#
# THE OWNER OF THE VERSION SET IS `gate`, NOT THIS FILE. Two constants that
# must agree is the defect this file keeps finding, so the door is DERIVED
# from the reader's registry by naming what the door additionally closes:
# the focused kind. That subtraction is the whole difference between "what
# can be read" and "what a plain artifact may bring in", and writing it as a
# subtraction means a new version cannot be admitted here by omission.
FOCUSED_VERSION = gate.FOCUSED_VERSION
WITHDRAWN_SHARD_VERSION = gate.WITHDRAWN_SHARD_VERSION
KNOWN_VERSIONS = tuple(v for v in gate.RECEIPT_VERSIONS
                       if v != FOCUSED_VERSION)

# Every key EVERY minting writes (gate._mint_result). Strict means STRICT:
# a row missing reader-relied keys, or carrying keys no minting writes, did
# not come from a gate and does not enter the ledger.
REQUIRED_KEYS = frozenset((
    "v", "event", "ts", "repo_id", "head", "tree", "dirty", "interpreter",
    "argv", "suite", "label", "rc", "wall", "status", "ran", "skipped",
    "detail", "elapsed", "failures", "failures_unreadable", "id"))
ALLOWED_KEYS = REQUIRED_KEYS | frozenset((
    "head_after", "tree_after", "dirty_after", "base_check",
    # THE READER LEARNS THE KEY BEFORE ANY WRITER EMITS IT. This half lands
    # ALONE and mints nothing new, so its own gate receipt keeps the old shape
    # and imports on the trunk that has not learned it yet. The writer follows
    # on a trunk that already accepts what it will mint. Measured the hard way
    # 2026-08-25: a lane carrying BOTH halves cannot land, because its gate
    # runs the lane's minting code and its import runs TRUNK's reader, so the
    # receipt was refused as foreign and the lane could not produce the
    # evidence its own landing required.
    "stderr_tail", "stderr_tail_meta",
    # THE PROJECT'S OWN DECLARED COMMAND, permitted at every version because it
    # is not a version's field: `gate._receipt_id` binds it BY PRESENCE, so a
    # row carrying one cannot have it edited and a row without one hashes
    # exactly as it always did. See gate.RECEIPT_KIND_KEYS.
    "suite_command"))

# Keys a SPECIFIC version both requires and permits. `host` is not optional
# decoration at v4: it is bound into the receipt id, so a v4 row without one
# recomputes to a different id, and a v1-3 row carrying one describes a minting
# that never happened. Required-at-4 and foreign-below-4 are the same fact
# stated from the two sides a reader can be wrong from. `focus` appears in no
# importable version: it is the focused (v6) kind's key, v6 refuses below, and
# a v1-5 or v8 row wearing a focus block describes a minting that never
# happened — its scope would sit outside the id's protection. The registry in
# `gate` is what says so; restating it here is how the two drift.
VERSION_KEYS = {version: gate.receipt_version_keys(version)
                for version in KNOWN_VERSIONS}


def imports_path():
    return os.path.join(home.global_dir(), IMPORTS)


def bindings_path():
    return os.path.join(home.global_dir(), BINDINGS)


def fab_completions_path():
    return os.path.join(home.global_dir(), FAB_COMPLETIONS)


_REPO_IDENTITIES = {}


def _path_stamp(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _repo_root_stamp(repo):
    root = _path_stamp(repo)
    if root is None or not stat.S_ISDIR(root[2]):
        return None
    return root, _path_stamp(os.path.join(repo, ".git"))


def _repo_generation(repo, gitdir, common):
    """One coherent filesystem generation for Git's resolved authority."""
    root = _repo_root_stamp(repo)
    git_stamp, common_stamp = _path_stamp(gitdir), _path_stamp(common)
    marker = os.path.join(gitdir, "commondir")
    marker_stamp = _path_stamp(marker)
    if root is None or git_stamp is None or common_stamp is None:
        return None
    if marker_stamp is None:
        if common != gitdir:
            return None
    else:
        try:
            with open(marker, encoding="utf-8") as fh:
                raw = fh.read(4097)
        except (OSError, TypeError, ValueError, UnicodeError):
            return None
        target = raw.strip()
        if not target or len(raw) > 4096 or "\0" in target:
            return None
        if not os.path.isabs(target):
            target = os.path.join(gitdir, target)
        if os.path.realpath(target) != common:
            return None
    generation = root, git_stamp, marker_stamp, common_stamp
    if _repo_root_stamp(repo) != root \
            or _path_stamp(gitdir) != git_stamp \
            or _path_stamp(marker) != marker_stamp \
            or _path_stamp(common) != common_stamp:
        return None
    return generation


def _repo_identity(repo, timeout=30):
    """Common-dir identity, cached only for one complete Git generation.

    Historical audit pointers outlive their worktrees. A missing directory is
    therefore the ordinary case, not a reason to spawn Git hundreds of times in
    a stop hook. Negative results are never cached. A positive result binds the
    checkout marker, resolved gitdir, gitdir's commondir indirection, and resolved
    common-dir generation, so linked-worktree metadata can neither retarget nor
    disappear while stale repository authority remains spendable.
    """
    repo = str(repo or "").strip()
    if not repo:
        return None
    try:
        repo = os.path.abspath(repo)
    except (OSError, TypeError, ValueError):
        return None
    before = _repo_root_stamp(repo)
    if before is None:
        return None
    cached = _REPO_IDENTITIES.get(repo)
    if cached:
        generation, identity, gitdir, common = cached
        if _repo_generation(repo, gitdir, common) == generation:
            return identity
    rc, resolved, _err = vcs.backend(repo).text(
        repo, "rev-parse", "--path-format=absolute", "--git-dir",
        "--git-common-dir", timeout=timeout, env=vcs._authority_env())
    paths = resolved.splitlines()
    if rc != 0 or len(paths) != 2 or not all(paths):
        return None
    gitdir, common = (os.path.realpath(path) for path in paths)
    after = _repo_generation(repo, gitdir, common)
    if after is None or after[0] != before:
        return None
    identity = common
    _REPO_IDENTITIES[repo] = (after, identity, gitdir, common)
    return identity


def _canonical(row):
    """One byte shape per content, for the idempotency comparison."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _fab_identity_contract(identity):
    """Canonical whole-gate identity shared by admission and direct import."""
    if not isinstance(identity, dict) or set(identity) != {
            "format", "repository", "tree", "scope", "interpreter", "runner"} \
            or identity.get("format") != FAB_KEY_FORMAT:
        return None, "Fab completion identity is malformed"
    repository = identity.get("repository")
    if not isinstance(repository, dict) or set(repository) != {"common_dir"} \
            or type(repository.get("common_dir")) is not str \
            or not repository["common_dir"] \
            or not os.path.isabs(repository["common_dir"]) \
            or os.path.normpath(repository["common_dir"]) \
            != repository["common_dir"]:
        return None, "Fab completion repository identity is malformed"
    if type(identity.get("tree")) is not str \
            or not re.fullmatch(r"[0-9a-f]{40}", identity["tree"]):
        return None, "Fab completion tree identity is malformed"
    # Reader-first: transport may describe either historical serial scope or
    # the v9 script scope. _fab_receipt_err binds each to its own receipt era.
    if identity.get("scope") not in (
            {"kind": "whole", "argv": list(FAB_WHOLE_ARGV)},
            {"kind": "whole", "argv": list(gateauthority.SUITE_ARGV)}):
        return None, "Fab completion scope identity is malformed"
    interpreter = identity.get("interpreter")
    if not isinstance(interpreter, dict) or set(interpreter) != {
            "name", "version", "language", "executable"} \
            or not all(type(value) is str and value
                       for value in interpreter.values()) \
            or not os.path.isabs(interpreter["executable"]):
        return None, "Fab completion interpreter identity is malformed"
    runner = identity.get("runner")
    if not isinstance(runner, dict) or set(runner) != {
            "format", "argv", "wrapper_version", "cgroup"} \
            or runner.get("format") != "fab-gate-runner-v1" \
            or runner.get("argv") != [
                "-m", "helm", "gate", "run", "--repo", "."] \
            or type(runner.get("wrapper_version")) is not str \
            or not re.fullmatch(r"[0-9a-f]{64}", runner["wrapper_version"]) \
            or runner.get("cgroup") not in FAB_RUNNER_CGROUPS:
        return None, "Fab completion runner identity is malformed"
    try:
        return json.dumps(identity, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False), None
    except (TypeError, ValueError):
        return None, "Fab completion identity is not finite canonical JSON"


def _fab_budgets_err(budgets):
    if not isinstance(budgets, dict) or set(budgets) != {
            "queue_s", "execution_s"}:
        return "Fab completion budgets are malformed"
    for value in budgets.values():
        if value is None:
            continue
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value <= 0:
            return "Fab completion budgets are malformed"
    return None


def _normalized_fab_authority(authority):
    """Validate direct input, then canonicalize numeric replay identity."""
    err = _fab_authority_err(authority)
    if err:
        return None, err
    normalized = dict(authority)
    normalized["budgets"] = {
        name: None if value is None else float(value)
        for name, value in authority["budgets"].items()}
    return normalized, None


def _fab_authority_err(authority):
    if not isinstance(authority, dict) \
            or set(authority) - {FAB_AUTHORITY_NODE_ID} \
            != set(FAB_AUTHORITY_REQUIRED):
        return "Fab completion authority is not the exact v2 shape"
    if FAB_AUTHORITY_NODE_ID in authority and (
            type(authority[FAB_AUTHORITY_NODE_ID]) is not str
            or not re.fullmatch(r"[0-9a-f]{16}",
                                authority[FAB_AUTHORITY_NODE_ID])):
        return "Fab completion node_id is not a 16-hex machine-id hash"
    if authority.get("format") != "helm-fab-completion-v2":
        return "Fab completion authority format is unknown"
    key = authority.get("key")
    if type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key):
        return "Fab completion key is malformed"
    if authority.get("job_id") != "gate-" + key:
        return "Fab completion job id does not derive from its key"
    host, generation = authority.get("host"), authority.get("generation")
    if type(host) is not str \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", host) \
            or type(generation) is not str \
            or not re.fullmatch(
                r"run-[0-9a-f]{32}-[1-9][0-9]*-[0-9a-f]{16}", generation):
        return "Fab completion host/generation is malformed"
    if type(authority.get("sha")) is not str \
            or not re.fullmatch(r"[0-9a-f]{40}", authority["sha"]):
        return "Fab completion sha is malformed"
    identity = authority.get("identity")
    body, err = _fab_identity_contract(identity)
    if err:
        return err
    if hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest() != key:
        return "Fab completion key does not resolve from its identity"
    err = _fab_budgets_err(authority.get("budgets"))
    if err:
        return err
    source = authority.get("source_artifact")
    if type(source) is not str or not source:
        return "Fab completion source artifact is malformed"
    digest = authority.get("artifact_sha256")
    if digest is not None and (type(digest) is not str
                               or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        return "Fab completion artifact digest is malformed"
    if type(authority.get("exit")) is not int \
            or not 0 <= authority["exit"] <= 255:
        return "Fab completion exit is not exact"
    return None


def _fab_receipt_err(receipt, authority):
    identity = authority["identity"]
    sha, tree = authority["sha"], identity["tree"]
    scope_argv = gateauthority.SUITE_ARGV \
        if receipt.get("v") == gate.SHARDED_AUTHORITY_VERSION else FAB_WHOLE_ARGV
    if identity["scope"] != {"kind": "whole", "argv": list(scope_argv)}:
        return "Fab scope differs from the receipt version's canonical suite"
    if receipt.get("v") == gate.SHARDED_AUTHORITY_VERSION:
        refusal = gateauthority.receipt_refusal(receipt)
        if refusal:
            return refusal
    expected = {
        "head": sha, "tree": tree, "dirty": False,
        "head_after": sha, "tree_after": tree, "dirty_after": False,
        "interpreter": identity["interpreter"], "suite": True,
        "argv": [identity["interpreter"]["executable"]]
                + identity["scope"]["argv"],
        "rc": authority["exit"],
        "status": "OK" if authority["exit"] == 0 else "FAILED",
    }
    for name, value in expected.items():
        if receipt.get(name) != value:
            return "Fab receipt %s differs from the completion authority" % name
    host = receipt.get("host")
    if FAB_AUTHORITY_NODE_ID not in authority:
        # A Fab writer that predates node_id: the hostname rung, unchanged.
        if not isinstance(host, dict) or host.get("node") != authority["host"]:
            return "Fab receipt host differs from the completion authority"
        return None
    # Machine id against machine id. One box can answer to two names (a Fab
    # alias and its own uname), so once the id is present the hostname is
    # display only and never decides, in either direction.
    want = authority[FAB_AUTHORITY_NODE_ID]
    got = host.get("id") if isinstance(host, dict) else None
    if got != want:
        shown = got if type(got) is str and re.fullmatch(r"[0-9a-f]{16}", got) \
            else ("absent" if got in (None, "") else "malformed")
        return ("Fab receipt machine id %s differs from the dispatched node's "
                "machine id %s" % (shown, want))
    return None


def _fab_completion_id(row):
    body = {k: v for k, v in row.items() if k not in ("id", "ts")}
    return hashlib.sha256(
        _canonical(body).encode("utf-8", "surrogatepass")).hexdigest()


def _fab_completion_err(row, receipt=None):
    if not isinstance(row, dict) or set(row) != set(FAB_COMPLETION_REQUIRED) \
            or row.get("v") != FAB_COMPLETION_VERSION \
            or row.get("event") != FAB_COMPLETION_EVENT:
        return "Fab completion row is not the canonical v1 shape"
    for name in ("id", "ts", "receipt", "importing_repo"):
        if type(row.get(name)) is not str or not row[name]:
            return "Fab completion %s is malformed" % name
    artifact = row.get("artifact")
    if not isinstance(artifact, dict) \
            or set(artifact) != set(FAB_ARTIFACT_REQUIRED) \
            or type(artifact.get("sha256")) is not str \
            or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) \
            or type(artifact.get("bytes")) is not int \
            or artifact["bytes"] < 0:
        return "Fab completion artifact identity is malformed"
    err = _fab_authority_err(row.get("authority"))
    if err:
        return err
    if artifact["sha256"] != row["authority"]["artifact_sha256"]:
        return "Fab completion artifact digest disagrees with the authority"
    if row["id"] != _fab_completion_id(row):
        return "Fab completion content id does not resolve"
    authority, identity = row["authority"], row["authority"]["identity"]
    repository = identity.get("repository")
    if not isinstance(repository, dict) \
            or repository.get("common_dir") != row["importing_repo"]:
        return "Fab completion repository identity disagrees"
    if receipt is not None:
        if row["receipt"] != receipt.get("id"):
            return "Fab completion authority disagrees with the receipt"
        err = _fab_receipt_err(receipt, authority)
        if err:
            return err
    return None


def _load_rows(path):
    """-> (rows, artifact identity, err) from one bounded fd snapshot.

    Validation and hashing consume the SAME opened file. The aggregate byte cap
    bounds both the retained row graph and the input a local caller can make the
    importer hold; crossing it refuses before JSON decoding or any ledger write.
    """
    fd = None
    try:
        p = os.path.abspath(path)
        flags = openflags.flags(os.O_RDONLY, "O_NOFOLLOW", cloexec=True)
        fd = os.open(p, flags)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError("artifact is not a private regular file")
        if st.st_size > ARTIFACT_MAX_BYTES:
            os.close(fd)
            fd = None
            return None, None, ("artifact exceeds the %d-byte import limit"
                                % ARTIFACT_MAX_BYTES)
        rows, digest, total, n = [], hashlib.sha256(), 0, 0
        with os.fdopen(fd, "rb") as f:
            fd = None
            while True:
                # `for raw in f` may allocate an arbitrarily grown single line
                # before the aggregate check gets to see it. Bound each read by
                # the bytes still admissible plus one refusal byte, so the
                # opened-fd pass remains bounded even when a writer races the
                # fstat snapshot above.
                raw = f.readline(ARTIFACT_MAX_BYTES - total + 1)
                if not raw:
                    break
                total += len(raw)
                if total > ARTIFACT_MAX_BYTES:
                    return None, None, ("artifact exceeds the %d-byte import limit"
                                        % ARTIFACT_MAX_BYTES)
                digest.update(raw)
                n += 1
                if total == ARTIFACT_MAX_BYTES and not raw.endswith(b"\n"):
                    extra = f.read(1)
                    if extra:
                        return None, None, (
                            "artifact exceeds the %d-byte import limit"
                            % ARTIFACT_MAX_BYTES)
                if not raw.strip():
                    continue
                try:
                    rows.append(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, ValueError):
                    return None, None, "line %d of %s is not JSON" % (n, path)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        return None, None, "cannot read %s: %s" % (path, exc)
    if not rows:
        return None, None, "%s holds no receipt rows" % path
    artifact = {"path": p, "sha256": digest.hexdigest(), "bytes": total}
    return rows, artifact, None


def _pick(rows, want_id):
    receipts = [r for r in rows if isinstance(r, dict)
                and r.get("event") == "gate"]
    if want_id:
        hits = [r for r in receipts if r.get("id") == want_id]
        if not hits:
            return None, "no row with id %s in the artifact" % want_id
        if len(hits) > 1:
            return None, ("id %s appears %d times in the artifact — a file "
                          "that repeats an id is not evidence"
                          % (want_id, len(hits)))
        return hits[0], None
    if len(receipts) > 1:
        return None, ("the artifact holds %d receipt rows — name one with "
                      "--id <receipt-id>" % len(receipts))
    if not receipts:
        # Preserve the precise schema refusal for a one-row non-gate artifact;
        # a multi-row chunk-only artifact has no unambiguous candidate.
        return (rows[0], None) if len(rows) == 1 else (
            None, "the artifact holds no gate receipt row")
    return receipts[0], None


def _schema_err(row, routed_focus=False):
    if not isinstance(row, dict):
        return "receipt row is not an object"
    if row.get("event") != "gate":
        return "event is %r, not 'gate'" % (row.get("event"),)
    if row.get("v") == WITHDRAWN_SHARD_VERSION:
        return ("receipt %s is a WITHDRAWN sharded script receipt (v7) — "
                "fresh workers cannot preserve arbitrary serial unittest "
                "process state, so v7 never imports or binds; run the literal "
                "serial gate instead" % row.get("id"))
    if row.get("v") == FOCUSED_VERSION and not routed_focus:
        # NOT the unknown-version refusal below — updating helm would not
        # change the answer. A generic artifact carries no independent origin;
        # only gateroute's live challenge-framed custody may set routed_focus.
        return ("receipt %s is a FOCUSED (v6) receipt — a plain artifact has "
                "no independent run origin, so its scope is indistinguishable "
                "from fields its submitter assembled; route the run through "
                "`helm gate run --focus --box HOST` instead"
                % row.get("id"))
    # TYPE FIRST, MEMBERSHIP SECOND, and never membership alone: `True == 1`
    # in Python, so `row.get("v") not in KNOWN_VERSIONS` is FALSE for a
    # boolean version and a `{"v": true}` row would be admitted AS v1 —
    # validated against v1's grammar and written to the ledger. gate's
    # `receipt_version_known` is the exact-int predicate; the focused kind is
    # readable but closed at this door, and its own refusal fired above.
    if not gate.receipt_version_known(row) \
            and row.get("v") != FOCUSED_VERSION:
        return ("receipt version %r is not one this helm understands "
                "(knows %s) — REFUSED rather than half-validated; update "
                "helm before importing it" % (row.get("v"),
                                              list(KNOWN_VERSIONS)))
    versioned = gate.receipt_version_keys(FOCUSED_VERSION) \
        if row.get("v") == FOCUSED_VERSION else \
        VERSION_KEYS.get(row.get("v"), frozenset())
    missing = sorted((REQUIRED_KEYS | versioned) - set(row))
    if missing:
        return "receipt is missing minted keys: %s" % ", ".join(missing)
    foreign = sorted(set(row) - (ALLOWED_KEYS | versioned))
    if foreign:
        return "receipt carries keys no gate mints: %s" % ", ".join(foreign)
    if row.get("v") == gate.SHARDED_AUTHORITY_VERSION:
        refusal = gateauthority.receipt_refusal(row)
        if refusal:
            return refusal
    return _diagnostic_err(row)


DIAGNOSTIC_KEYS = ("stderr_tail", "stderr_tail_meta")
DIAGNOSTIC_META = {"total_bytes": int, "truncated": bool}
# `source` is minted ONLY by a declared `exit`-protocol run, whose tail is the
# command's combined stdout+stderr and not unittest's stderr; a unittest row
# never carries it. Optional, so every receipt minted before it existed and
# every unittest receipt after it keep validating unchanged.
DIAGNOSTIC_META_OPTIONAL = {"source": str}


def _diagnostic_err(row):
    """Validate the advisory diagnostics as a WHOLE OBJECT, or refuse.

    ADMITTING A KEY IS NOT ACCEPTING WHATEVER ARRIVES UNDER IT. Membership in
    ALLOWED_KEYS says a gate MAY mint this name and says nothing about the
    value, so an allowlist-only importer takes `stderr_tail: {"cmd": "rm -rf"}`
    straight into the ledger.

    WRITTEN AS A POSITIVE GRAMMAR AFTER A PER-CASE VERSION SPIRALLED. The first
    draft enumerated rejections and a reviewer kept finding one more, which is
    the signature of a per-case handler: the next case is always outside the
    set you enumerated. So this states what a WELL-FORMED pair IS — both keys
    present, a non-empty string, a mapping whose key set is EXACTLY the two
    required fields plus any of DIAGNOSTIC_META_OPTIONAL, each of an exact
    type — and refuses everything else by construction. Adding a field means
    changing DIAGNOSTIC_META (or DIAGNOSTIC_META_OPTIONAL), not adding a
    branch.

    PRESENCE IS `in`, NEVER `.get()` OR TRUTHINESS. That collapse is what the
    per-case draft got wrong: `stderr_tail=None` and `stderr_tail=""` both read
    as absent, so an explicit null was admitted as a receipt that simply had no
    diagnostic. Absence and a malformed value are different facts and a reader
    must not be handed one wearing the other.

    `type() is`, NEVER `isinstance`, because bool IS an int in Python: an
    `isinstance` check passes `total_bytes: True`, and a `truncated` of `1`
    would satisfy a numeric reading. The string "false" is TRUTHY, which is the
    difference between a reader believing the tail is a window and believing it
    is the whole stream.

    Absent is always fine: a green receipt carries neither key, and every
    receipt minted before this feature existed carries neither.
    """
    present = [key for key in DIAGNOSTIC_KEYS if key in row]
    if not present:
        return None
    if len(present) != len(DIAGNOSTIC_KEYS):
        missing = [key for key in DIAGNOSTIC_KEYS if key not in row]
        return ("receipt carries %s without %s — a tail that cannot say "
                "whether it was truncated is a window indistinguishable from "
                "a whole stream, and metadata without a tail describes a "
                "diagnostic that is not there"
                % (", ".join(present), ", ".join(missing)))
    tail, meta = row["stderr_tail"], row["stderr_tail_meta"]
    if type(tail) is not str:
        return ("receipt stderr_tail is %s, not a string — a diagnostic is "
                "text a human reads, never structure" % type(tail).__name__)
    if not tail:
        return ("receipt stderr_tail is empty — an empty diagnostic is an "
                "ABSENT one wearing a present key; mint neither instead")
    if type(meta) is not dict:
        return ("receipt stderr_tail_meta is %s, not an object"
                % type(meta).__name__)
    if set(meta) - set(DIAGNOSTIC_META_OPTIONAL) != set(DIAGNOSTIC_META):
        return ("receipt stderr_tail_meta names %s — a gate mints exactly %s "
                "and at most %s beside them"
                % (sorted(meta) or "nothing", sorted(DIAGNOSTIC_META),
                   sorted(DIAGNOSTIC_META_OPTIONAL)))
    grammar = dict(DIAGNOSTIC_META_OPTIONAL, **DIAGNOSTIC_META)
    for name, want in sorted(grammar.items()):
        if name not in meta:
            continue
        value = meta[name]
        if type(value) is not want:
            return ("receipt stderr_tail_meta.%s is %r (%s), not %s"
                    % (name, value, type(value).__name__, want.__name__))
    if "source" in meta and not meta["source"]:
        return ("receipt stderr_tail_meta.source is empty — a tail that names "
                "no source is a unittest tail wearing a key it never mints")
    if meta["total_bytes"] < 0:
        return ("receipt stderr_tail_meta.total_bytes is %d — a byte count of "
                "the ORIGINAL stream cannot be negative" % meta["total_bytes"])
    if len(tail.encode("utf-8")) > meta["total_bytes"]:
        return ("receipt stderr_tail is longer than the total_bytes it claims "
                "to be a tail OF (%d > %d)"
                % (len(tail.encode("utf-8")), meta["total_bytes"]))
    return None


_TREE_OID = re.compile(r"[0-9a-f]{40}\Z")
_UNASKED = object()
_TREE_PASS = [None]                       # (repo, {head: tree or None}) or None


def _tree_env():
    """The view every tree question is asked under, spelled ONCE.

    THE ENV IS PART OF THE QUESTION. A replacement ref rewrites which object
    an id names, so a pass that resolved one head under replacements and the
    next under none would be comparing two repositories. Both the per-head
    `rev-parse` and the batch below take this same overlay, because the batch
    exists to give the SAME answer in fewer processes, not a cheaper one."""
    env = vcs._authority_env()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _batched_trees(repo, heads):
    """{head: tree oid or None} for the heads this ONE `cat-file` could answer.

    A HEAD ABSENT FROM THE RESULT IS UNASKED, NEVER UNRESOLVED. This returns
    only answers it can defend; `_tree_of` re-asks anything missing through the
    single-head `rev-parse` it has always used, so an output form git adds
    later costs the old cost and never produces a wrong tree.

    WHAT IT FEEDS IS `<head>^{tree}`, NEVER A BARE SHA. git echoes the request
    verbatim on a refusal (`<head>^{tree} missing`), so the echo carries the
    literal suffix and cannot be read back as an oid by any parser, this one
    included.

    WHAT IT ACCEPTS IS A SHAPE, POSITIVELY. A line is a tree oid iff it is
    exactly one token of 40 lowercase hex and nothing else. `missing` lines
    carry a space and the echoed request, so they fail that test — and so
    would `<oid> tree <size>`, or any richer form a future git prints. The
    test admits the answer rather than excluding the known refusals, which is
    what makes an unfamiliar output form fail CLOSED.

    ONLY 40-HEX HEADS ARE ASKED. The request stream is line-delimited, so a
    head carrying a newline would desynchronise request from answer and hand
    one receipt another receipt's tree — the exact laundering this rung
    exists to refuse. A head that is not a bare sha1 is left to `rev-parse`,
    which takes it as one argv element and cannot be desynchronised at all.
    The line COUNT is checked for the same reason: fewer or more lines than
    requests means the positional mapping is unproven, and the whole batch is
    discarded rather than aligned by guesswork."""
    wanted = sorted({h for h in heads if isinstance(h, str) and _TREE_OID.match(h)})
    if not wanted:
        return {}
    stdin = "".join(h + "^{tree}\n" for h in wanted).encode("ascii")
    rc, out, _err = vcs.backend(repo).run(
        repo, "cat-file", "--batch-check=%(objectname)", timeout=60,
        env=_tree_env(), stdin=stdin)
    if rc != 0:
        return {}
    lines = os.fsdecode(out).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(wanted):
        return {}
    answers = {}
    for head, line in zip(wanted, lines):
        if _TREE_OID.match(line):
            answers[head] = line
        elif line == head + "^{tree} missing":
            answers[head] = None
    return answers


class tree_batch:
    """Resolve every tree ONE pass needs in one git process, for `repo`.

    WHY A PASS AND NOT A CACHE. `green_receipts` asks `<head>^{tree}` once per
    receipt, and on the live ledger that was 845 git spawns in one Stop — 13.4
    of the seam rung's 13.7 seconds. The question is the same shape every
    time and git answers a whole stream of them in one process.

    THE ANSWER IS STABLE FOR THE ONLY TRANSITIONS THAT EXIST. A tree oid is
    content, so `X^{tree}` cannot change its answer once it has one; the only
    moves are a head MATERIALISING mid-pass (this table says missing, and the
    receipt is refused — the conservative side) and objects being pruned (same
    direction). So a pass-scoped table cannot admit a tree the repository
    could not produce, which is the property the doors above depend on.

    SCOPED TO THE REPOSITORY IT WAS BUILT FOR. A `_tree_of` call naming any
    other repo path misses this table entirely and spawns, because a table
    built for one repository is not evidence about another — worktrees share
    an object store and that is exactly how a foreign receipt would enter."""

    def __init__(self, repo, heads):
        self.repo, self.heads, self.prior = repo, heads, None

    def __enter__(self):
        self.prior = _TREE_PASS[0]
        try:
            answers = _batched_trees(self.repo, self.heads)
        except projscope.Expired:
            _TREE_PASS[0] = self.prior
            raise
        except Exception:                # noqa: BLE001 — the old path answers
            answers = {}
        _TREE_PASS[0] = (self.repo, answers)
        return self

    def __exit__(self, *exc):
        _TREE_PASS[0] = self.prior
        return False


def _pass_tree(repo, head):
    """The open pass's answer for this head, or `_UNASKED`."""
    pass_ = _TREE_PASS[0]
    if not pass_ or pass_[0] != repo:
        return _UNASKED
    return pass_[1].get(head, _UNASKED)


def _tree_of(repo, head):
    hit = _pass_tree(repo, head)
    if hit is not _UNASKED:
        return hit
    rc, tree, _err = vcs.backend(repo).text(
        repo, "rev-parse", head + "^{tree}", timeout=30, env=_tree_env())
    return tree if rc == 0 and tree else None


def _printable(text):
    """A string this note can survive being PRINTED as.

    A REPOSITORY PATH IS RAW BYTES. Python surrogate-escapes an undecodable
    filesystem byte, and a real UTF-8 stderr REFUSES the result — measured:
    the same note is accepted by a str sink and raises UnicodeEncodeError on
    an encoder. So a diagnostic assembled from a path inherits whatever that
    path contains, and this one is printed AFTER the durable appends, where a
    raise turns a completed import into a retry that lands on the duplicate
    path looking like a new bug. Round-tripping through the encoder is what
    makes the note printable by construction rather than by luck."""
    return str(text).encode("utf-8", "backslashreplace").decode("utf-8")


def _head_sha(repo):
    """(sha, why) — the commit this repo would land, or None with the reason.

    ITS OWN GUARDED READ, because unlike `_tree_of`'s other caller this one
    runs AFTER the durable appends. A best-effort note must never turn an
    import that already happened into a non-zero exit, sending the operator
    into a retry that lands on the duplicate path looking like a new bug."""
    try:
        rc, sha, _err = vcs.backend(repo).text(
            repo, "rev-parse", "HEAD", timeout=30, env=_tree_env())
    except Exception as exc:                      # noqa: BLE001 — see above
        return None, "HEAD could not be read here (%s)" % type(exc).__name__
    if rc != 0 or not sha:
        return None, "HEAD does not resolve in %s" % repo
    return sha, None


def _divergence_note(repo, row):
    """The disclosure a `fab gate` author is owed AT THE END of the run.

    -> one line of prose, or None when this receipt covers the commit this
    repo would land.

    WHY HERE. `fab gate` on a dirty tree snapshots tracked and untracked
    content into a THROWAWAY commit, ships that, and the spoke checks it out
    CLEAN — so the receipt honestly records dirty=False about a tree that
    exists in no branch of anyone's history. The only notice is `fab: dirty
    tree snapshotted`, printed by the dispatcher BEFORE a five-to-ten minute
    wait, where nobody is looking; by the time the receipt lands the author
    reads a green line and hands the lane over. `gate import` is the last
    thing a `fab gate` run prints and the first place helm sees BOTH the
    receipt and the author's real repository.

    IT ASKS THE BINDER'S QUESTION, NOT A PROXY FOR IT. Comparing TREES looks
    equivalent and is not: a snapshot S and an author commit C built from the
    same content are SAME-TREE SIBLINGS — same tree, different commits,
    neither carrying the other — so a tree test finds them equal and says
    nothing while `bind` refuses the receipt because S does not carry C. A
    warning door that is silent exactly when binding fails is worse than no
    door, because silence there reads as measured coverage. `gate.carriage`
    is the one predicate both callers ask.

    IT NAMES THE MEASUREMENT AND NOT THE CAUSE. A snapshot commit and a commit
    made DURING the run reach this state identically and this reader cannot
    tell them apart, so it reports the comparison it made and names both
    shapes rather than picking one. And it never orders a re-gate: a receipt
    minted on a DESCENDANT of HEAD carries it and binds perfectly well, which
    is why the predicate above answers CARRIED there and this function stays
    quiet."""
    head = row.get("head")
    if not isinstance(head, str) or not head:
        return ("gate import: NOTE — receipt %s names no head, so whether it "
                "covers the commit you would land is UNKNOWN"
                % (row.get("id"),))
    mine, why = _head_sha(repo)
    if mine is None:
        return ("gate import: NOTE — receipt %s measured commit %s; whether "
                "it covers the commit you would land is UNKNOWN because %s"
                % (row.get("id"), head[:12], why))
    try:
        verdict, _relation, _sequence = gate.carriage(repo, mine, head)
    except Exception as exc:                      # noqa: BLE001 — see _head_sha
        return ("gate import: NOTE — receipt %s measured commit %s; whether "
                "it covers HEAD (%s) is UNKNOWN — the containment read failed "
                "(%s)" % (row.get("id"), head[:12], mine[:12],
                          type(exc).__name__))
    if verdict == gate.CARRIED:
        return None
    if verdict == gate.CARRIAGE_UNKNOWN:
        return ("gate import: NOTE — receipt %s measured commit %s, and "
                "whether that commit carries HEAD (%s) in %s could not be "
                "derived. This receipt is NOT shown to cover the commit you "
                "would land; the BINDER is the authority and will say "
                "so." % (row.get("id"), head[:12], mine[:12], repo))
    return ("gate import: NOTE — receipt %s measured commit %s, which does "
            "NOT carry HEAD (%s) in %s — not by ancestry and not as a "
            "contiguous run of its patches. Either the gate ran on a snapshot "
            "of an UNCOMMITTED working tree (`fab gate` makes one for a dirty "
            "tree) or HEAD moved during the run; this reader cannot tell "
            "which. Either way the BINDER will refuse this receipt for "
            "that commit."
            % (row.get("id"), head[:12], mine[:12], repo))


def head_divergence(repo, row):
    """The note, rendered so it can be PRINTED — or None.

    ONE DOOR, because the raw bytes can enter through any field and did:
    laundering the repository path at the two sites that interpolate it left
    the UNKNOWN branch, whose reason string is built one call down in
    `_head_sha`, still carrying a surrogate. Every future branch would be the
    same bet. So the rendering happens once, on the way out, where no message
    can miss it."""
    note = _divergence_note(repo, row)
    return _printable(note) if note else None


def _disclose(repo, row):
    """Print the divergence note, if there is one, AFTER the verdict line.

    LAST, so it is what the terminal is left showing, and on EVERY exit path
    including duplicate and repaired — a re-import of the same artifact is a
    reader coming back to the same question, and answering it only the first
    time is how the fact gets lost to scrollback all over again. Stderr,
    beside every other disclosure the gate verbs emit, so the stdout verdict
    line stays exactly the string existing callers parse. It never changes the
    exit code: this reports on the run, it does not judge it."""
    if not isinstance(row, dict):
        return
    note = head_divergence(repo, row)
    if not note:
        return
    try:
        print(note, file=sys.stderr)
    except Exception:                           # noqa: BLE001 — see docstring
        pass


_STORED_IDS_CACHE = [None]


def _ledger_stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _stored_ids():
    """Index unique stored content once per immutable ledger generation.

    Canonical binding checks hold the receipt-ledger lock, but the stop path may
    check hundreds of imported receipts in one process. Re-decoding the same
    multi-megabyte append-only ledger for every candidate consumed the remainder
    of the five-second hook after repository placement was bounded. The complete
    file identity invalidates this positive cache on every append/replacement;
    an unstable or unreadable generation is never cached.
    """
    projscope.spend_or_raise("stored receipt index entry")
    path = gate.receipts_path()
    before = _ledger_stamp(path)
    cached = _STORED_IDS_CACHE[0]
    if before is not None and cached and cached[0] == (path, before):
        projscope.spend_or_raise("stored receipt cached result")
        return cached[1]
    rows, unavailable = eventledger.checked_events(path, strict=True)
    projscope.spend_or_raise("stored receipt ledger result")
    if unavailable:
        return None, None, unavailable
    out, poisoned = {}, set()
    for index, row in enumerate(rows):
        projscope.spend_or_raise("stored receipt row %d" % index)
        if row.get("event") not in ("gate", gate._FAILURE_CHUNK_EVENT):
            continue
        rid = row.get("id")
        prior = out.get(rid)
        if prior is not None and _canonical(prior) != _canonical(row):
            poisoned.add(rid)
            out.pop(rid, None)
        elif rid not in poisoned:
            out[rid] = row
    result = out, poisoned, None
    after = _ledger_stamp(path)
    projscope.spend_or_raise("stored receipt index publication")
    if before is not None and after == before:
        _STORED_IDS_CACHE[0] = ((path, before), result)
    return result


def _stored_timings():
    """Index advisory timing separately; it never enters authority ids."""
    rows, unavailable = eventledger.checked_events(
        gate.receipts_path(), strict=True)
    if unavailable:
        return None, None, unavailable
    timings, poisoned, _skipped = gate._timings(rows)
    return timings, poisoned, None


def _stored_plan(row, chunks, stored, poisoned):
    needed = []
    for ref in row.get("failure_chunks") or ():
        if ref in poisoned:
            return None, None, ("failure chunk %s is poisoned: id is claimed by "
                                "conflicting stored content" % ref)
        prior = stored.get(ref)
        if prior is None:
            needed.append(chunks[ref])
        elif _canonical(prior) != _canonical(chunks[ref]):
            return None, None, ("failure chunk %s already exists with DIFFERENT "
                                "content — refusing the conflict" % ref)
    rid = row["id"]
    if rid in poisoned:
        return None, None, ("receipt %s is poisoned: id is claimed by conflicting "
                            "stored content" % rid)
    prior = stored.get(rid)
    if prior is not None and _canonical(prior) != _canonical(row):
        return None, None, ("id %s already in the ledger with DIFFERENT content — "
                            "refusing the conflict; neither row is trusted to "
                            "overwrite the other" % rid)
    return needed, prior is not None, None


def _artifact_object_err(row, rows):
    """Validate authority and select its optional advisory timing sibling."""
    chunks, chunk_errors, _skipped = gate._failure_chunks_with_errors(rows)
    if gate._version_has(row, "failure_record"):
        err = gate._failure_record_error(row, chunks, chunk_errors)
        if err:
            return None, None, "receipt failure record is incomplete: %s" % err
    referenced = [chunks[ref] for ref in row.get("failure_chunks") or ()
                  if ref in chunks]
    try:
        oversized = [event for event in referenced + [row]
                     if gate._event_bytes(event) > eventledger.MAX_EVENT_BYTES]
    except (TypeError, ValueError) as exc:
        return None, None, ("receipt object is not durable UTF-8 JSON: %s — "
                            "refused before provenance" % exc)
    if oversized:
        return None, None, ("receipt object contains %d event(s) exceeding the "
                            "%d-byte ledger limit — refused before provenance"
                            % (len(oversized), eventledger.MAX_EVENT_BYTES))
    timings, poisoned, _skipped = gate._timings(rows)
    timing = None if row["id"] in poisoned else timings.get(row["id"])
    return chunks, timing, None


def _durable_receipt_err(receipt):
    """Is this exact receipt and its complete failure record durable now?"""
    stored, poisoned, err = _stored_ids()
    if err:
        return "receipt ledger is unreadable: %s" % err
    rid = receipt["id"]
    if rid in poisoned:
        return "receipt %s is poisoned by conflicting stored content" % rid
    prior = stored.get(rid)
    if prior is None or _canonical(prior) != _canonical(receipt):
        return "receipt ledger does not hold this exact receipt"
    referenced = []
    for ref in receipt.get("failure_chunks") or ():
        if ref in poisoned:
            return "failure chunk %s is poisoned by conflicting stored content" % ref
        if ref in stored:
            referenced.append(stored[ref])
    if not gate._version_has(receipt, "failure_record"):
        return None
    # THE WALK IS THIS RECEIPT'S QUESTION, NOT THE LEDGER'S. The validator
    # keeps only the chunks a receipt references, so its input is that
    # referenced set. A wider one makes every receipt re-validate every other
    # receipt's chunks -- quadratic in a ledger the stop path checks hundreds
    # of receipts against, and measured as the dominant cost of the seam rung.
    # Scoping weakens nothing: a conflict on these ids is already in
    # `poisoned`, which the loop above refuses before reaching here.
    chunks, chunk_errors, _skipped = gate._failure_chunks_with_errors(referenced)
    err = gate._failure_record_error(receipt, chunks, chunk_errors)
    if err:
        return "receipt failure record is incomplete: %s" % err
    return None


def placement_err(row, repo):
    """Can ``repo`` currently produce the exact head/tree in ``row``?"""
    head, tree = row.get("head"), row.get("tree")
    if not head or not tree:
        return "receipt names no head/tree — nothing can be placed"
    local_tree = _tree_of(repo, str(head))
    if local_tree is None:
        return ("cited head %s does not resolve in %s — this repo cannot "
                "produce the tree the receipt is about" % (head, repo))
    if local_tree != tree:
        return ("tree mismatch: %s^{tree} is %s here, receipt says %s"
                % (head, local_tree, tree))
    if row.get("v") == gate.SHARDED_AUTHORITY_VERSION:
        return gateauthority.runner_tree_refusal(row, repo)
    return None


def _binding_id(row):
    body = {k: v for k, v in row.items() if k not in ("id", "ts")}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _binding_err(binding, receipt=None):
    if not isinstance(binding, dict):
        return "binding is not an object"
    if set(binding) != set(BINDING_REQUIRED):
        return "binding keys are %s, expected %s" % (
            sorted(binding), sorted(BINDING_REQUIRED))
    if binding.get("v") != BINDING_VERSION \
            or binding.get("event") != BINDING_EVENT:
        return "binding version/event is not the canonical v%d shape" \
            % BINDING_VERSION
    for name in ("id", "ts", "receipt", "importing_repo", "head", "tree"):
        if type(binding.get(name)) is not str or not binding[name]:
            return "binding %s is not a non-empty string" % name
    if binding.get("origin_repo") is not None \
            and (type(binding["origin_repo"]) is not str
                 or not binding["origin_repo"]):
        return "binding origin_repo is neither a non-empty string nor null"
    artifact = binding.get("artifact")
    if type(artifact) is not dict or set(artifact) != set(ARTIFACT_REQUIRED):
        return "binding artifact identity is not the canonical shape"
    digest = artifact.get("sha256")
    if type(artifact.get("path")) is not str or not artifact["path"] \
            or not os.path.isabs(artifact["path"]) \
            or type(digest) is not str or len(digest) != 64 \
            or any(c not in "0123456789abcdef" for c in digest) \
            or type(artifact.get("bytes")) is not int \
            or artifact["bytes"] < 0:
        return "binding artifact identity carries malformed values"
    if not os.path.isabs(binding["importing_repo"]) \
            or os.path.realpath(binding["importing_repo"]) \
            != binding["importing_repo"]:
        return "binding importing_repo is not a canonical absolute identity"
    if binding["id"] != _binding_id(binding):
        return "binding content id does not resolve"
    if receipt is not None:
        expected = {"receipt": receipt.get("id"),
                    "origin_repo": receipt.get("repo_id"),
                    "head": receipt.get("head"), "tree": receipt.get("tree")}
        for name, value in expected.items():
            if binding.get(name) != value:
                return "binding %s disagrees with the receipt" % name
    return None


def _binding_row(receipt, importing_repo, artifact):
    row = {"v": BINDING_VERSION, "event": BINDING_EVENT, "ts": pk.now_ts(),
           "receipt": receipt["id"], "importing_repo": importing_repo,
           "origin_repo": receipt.get("repo_id"), "head": receipt["head"],
           "tree": receipt["tree"], "artifact": artifact}
    row["id"] = _binding_id(row)
    return row


def _expired(deadline):
    """True when `deadline` has passed. A None deadline never expires."""
    return deadline is not None and time.monotonic() >= deadline


_BINDING_ROWS_MEMO = {}


def _bindings_identity():
    """The bindings ledger's identity through the validated read's own seam.

    The memo's freshness probe: ONE open and ONE fstat, no read and no parse.
    An identity taken by re-reading the file is not a cache — the first cut of
    this delegated to `checked_events_with_identity` and paid for a full
    validated read on every HIT, which is the cost the memo exists to avoid.
    It shares that function's descriptor discipline — O_NOFOLLOW, private
    regular file — so a symlink or hardlink at the canonical path yields None
    rather than a usable key.
    """
    return eventledger.ledger_identity(bindings_path())


def _binding_rows(deadline=None):
    """Every canonical import binding, validated. -> (rows, err).

    MEMOISED ON THE IDENTITY OF THE DESCRIPTOR THE ROWS CAME FROM, because
    this is asked once per CANDIDATE RECEIPT rather than once per guard:
    `green_receipts` calls `repository_authorization` for every candidate, so
    on a ledger of a few hundred bindings an unmemoised read re-validates
    every row hundreds of times and spends a third of a five-second budget
    recomputing an answer that has not changed.

    A PROCESS-LIFETIME MEMO WOULD BE WRONG HERE, and the obvious precedent
    (`_dispatch_snapshot`'s one-element cache) is exactly that shape. One of
    this function's three callers appends a binding under the ledger lock and
    the next reader must see it, so the key has to move when the file does.

    AND THE KEY MUST DESCRIBE THE BYTES THAT WERE PARSED, not a file that
    happened to be at the path before and after. Stat, validate, stat again is
    THREE descriptors and therefore three chances to be looking at a different
    file: a path replaced A -> B -> A validates B's rows and files them under
    A's identity, where no later append can dislodge them because A's key is
    legitimately A's. Identity and content come from ONE descriptor.
    """
    projscope.spend_or_raise("canonical binding ledger read")
    identity = None
    # THE BUDGET IS CHECKED BEFORE THE MEMO, NOT AFTER IT. A warm memo is
    # cheap, which is exactly why answering from it past the deadline is the
    # dangerous shape: the call succeeds, the caller reads that as "the budget
    # held", and it proceeds to spend the rest of an expired budget on work
    # this answer authorised. Worse, it makes the ANSWER DEPEND ON CACHE
    # WARMTH — the identical call returns rows on a hot process and
    # _CENSUS_TIMEOUT on a cold one, so the guard's verdict stops being a
    # function of the ledger and becomes a function of who ran first.
    if _expired(deadline):
        return None, _CENSUS_TIMEOUT
    try:
        cached = _BINDING_ROWS_MEMO.get(_bindings_identity())
    except Exception:                          # noqa: BLE001
        cached = None
    if cached is not None:
        projscope.spend_or_raise("canonical binding ledger result")
        return cached
    # THE COLD PATH IS THE EXPENSIVE ONE AND IT MUST RESPECT ITS CALLER'S
    # BUDGET. `binding_candidate_ids` declares 0.25s and calls this between
    # two `remaining()` checks, so a grown cold ledger — whole-file read, JSON
    # parse, per-row validation — can consume an entire 20s stop-guard
    # deadline and reach fail-open DESPITE the census budget. A cure for a
    # fail-open guard whose own hot path can reproduce it is not a cure.
    # THE TIMEOUT IS A TYPE, NOT A SENTENCE. `eventledger` RAISES
    # `projscope.Expired` at each of its deadline boundaries, so the only way
    # to be classified as this read's timeout is to BE one. Every text
    # predicate is gone: a substring test, and then a prefix test, both let an
    # unrelated failure whose message merely contains or begins with those
    # words wear the timeout and reach the caller's UNKNOWN branch -- a
    # fail-open on a question nobody asked. `unavailable` now carries only
    # genuinely unreadable ledgers and stays a string.
    try:
        rows, unavailable, identity = eventledger.checked_events_with_identity(
            bindings_path(), strict=True, deadline=deadline)
    except projscope.Expired:
        ambient = projscope.deadline()
        if ambient is not None and (deadline is None or ambient <= deadline):
            raise
        return None, _CENSUS_TIMEOUT
    projscope.spend_or_raise("canonical binding ledger result")
    if unavailable:
        return None, "canonical import bindings unavailable: %s" % unavailable
    # ONE EXIT DOOR FOR EVERY VALIDATION OUTCOME, AND IT IS CONTROL FLOW
    # RATHER THAN A RULE ABOUT WHICH HELPER TO CALL. Returning a malformed
    # verdict from inside the loop is what let a clock mint a corruption
    # verdict: the budget could expire DURING a validation, and the error that
    # validation produced was then interpreted and returned before anything
    # re-read the deadline. Adding a check in front of each return cannot
    # close that -- the crossing happens inside the work, not before it.
    #
    # So the loop DECIDES NOTHING. It checks the budget before each row,
    # retains the FIRST validation error it finds, and leaves. The expiry
    # check below is mandatory and unconditional, and it runs BEFORE that
    # error is interpreted, so a timeout outranks corruption by construction
    # rather than by remembering.
    first_err = None
    for index, row in enumerate(rows):
        projscope.spend_or_raise("canonical binding row %d" % index)
        if _expired(deadline):
            break
        if first_err is None:
            first_err = _binding_err(row)
            if first_err:
                break
    projscope.spend_or_raise("canonical binding fold completion")
    if _expired(deadline):
        return None, _CENSUS_TIMEOUT
    if first_err:
        return None, "canonical import binding is malformed: %s" % first_err
    if identity is not None:
        # ONE ENTRY: two identities in one process means the file moved, and
        # the older answer can never be wanted again.
        _BINDING_ROWS_MEMO.clear()
        _BINDING_ROWS_MEMO[identity] = (rows, None)
    return rows, None


_BINDING_INDEX_MEMO = {}


def _binding_index(rows):
    """(receipt, importing_repo) -> the rows carrying it, in ledger order.

    ONE SCAN PER LEDGER, NOT ONE PER RECEIPT. `_matching_binding` is asked
    once per CANDIDATE RECEIPT, and a scan per ask is quadratic in two ledgers
    that grow together: on a fleet with a few thousand receipts and a thousand
    bindings a single stop-guard pass asks it hundreds of times and the row
    visits reach seven figures, which dominates both the pass's wall and its
    budget charges. An index makes the same question O(rows + receipts), so
    the two ledgers can grow without the product growing with them.

    KEYED ON THE LIST'S IDENTITY AND IT HOLDS THAT LIST. `id()` alone is a
    dangling key: a freed list's address is reused, and the next list at that
    address would be answered from the dead one's index. The entry therefore
    stores the rows themselves and the hit is confirmed with `is`, which both
    proves the identity and keeps the address from being recycled while the
    entry lives.

    ONE ENTRY, the same rule and the same reason as `_BINDING_ROWS_MEMO`
    above: the rows it indexes come from that memo, and when the ledger file
    moves that memo drops its list and builds a new one, so an index for the
    older list can never be wanted again. Nothing in this module mutates a
    returned rows list in place — appends go through the ledger file under its
    lock, which changes the identity and forces a fresh read — so there is no
    live list whose index can silently go stale.
    """
    hit = _BINDING_INDEX_MEMO.get(id(rows))
    if hit is not None and hit[0] is rows:
        return hit[1]
    index = {}
    for row in rows:
        index.setdefault(
            (row.get("receipt"), row.get("importing_repo")), []).append(row)
    _BINDING_INDEX_MEMO.clear()
    _BINDING_INDEX_MEMO[id(rows)] = (rows, index)
    return index


def _matching_binding(rows, receipt_id, importing_repo):
    """The one binding for this receipt at this repository, or its refusal.

    THE BUDGET IS CHARGED AT THE PASS BOUNDARY, NOT PER ROW, AND THAT DECIDES
    WHEN THE DEADLINE MAY FIRE. A spend per row lets an expiry land in the
    middle of one receipt's lookup, so the row the pass dies on is decided by
    where the ledger happened to run out rather than by anything about the
    work. NO PARTIAL ANSWER WAS EVER RETURNED — `spend_or_raise` raises, and
    an expiry propagates out as an exception rather than as a short `hits`
    list — so what the boundary charge buys is a refusal at a place that means
    something, not the prevention of a truncated result. The only instants an
    `Expired` leaves this function are before the lookup and after it.

    WHAT THAT GIVES UP: row granularity. A budget that expires during the
    lookup is not noticed until the lookup ends. The uninterruptible span is
    one dict build over the binding ledger plus one hash lookup — a fraction
    of a millisecond at a thousand bindings, against a stop budget expressed
    in seconds. Row granularity over an in-memory dict comparison resolves
    finer than the clock the deadline is read from and costs several times the
    comparison it guards, so it is a worse deal the larger the ledger gets. A
    fold that genuinely needs row granularity back needs a CHEAP charge, not
    this one.

    THE MULTI-BINDING REFUSAL STILL SKIPS THE COMPLETION SPEND, as it always
    did: a pass that is returning a corruption verdict is not a pass that
    completed, and marking it as one would let the spend ladder read a refusal
    as a finished lookup.
    """
    projscope.spend_or_raise("canonical binding match")
    hits = _binding_index(rows).get((receipt_id, importing_repo), ())
    if len(hits) > 1:
        return None, "multiple canonical bindings exist for one receipt/repository"
    projscope.spend_or_raise("canonical binding match completion")
    return (hits[0] if hits else None), None


def _ensure_receipt(row, chunks, timing=None):
    """Place authority first, then repair its optional advisory timing sibling.

    Missing content-addressed failure chunks append before the gate row. A
    partial write can therefore leave only inert chunks, never a receipt that
    names evidence this import did not durably install. Timing writes happen
    only after the receipt is observable and can warn, never erase authority.
    """
    path = gate.receipts_path()
    left = projscope.spend_or_raise("receipt ledger lock")
    lock = eventledger.locked(path) if left is None \
        else eventledger.locked(path, timeout=left)
    with lock as held:
        projscope.spend_or_raise("receipt ledger lock result")
        if not held:
            return None, ("receipt ledger lock FAILED before provenance or "
                          "receipt append — retry the import")
        stored, poisoned, err = _stored_ids()
        if err:
            return None, "receipt ledger became unreadable: %s" % err
        needed, duplicate, err = _stored_plan(row, chunks, stored, poisoned)
        if err:
            return None, err
        for event in needed + ([] if duplicate else [row]):
            if not eventledger.append_unlocked(path, event):
                return None, "receipt append failed"
        stored, poisoned, err = _stored_ids()
        if err:
            return None, "receipt append was not observable: %s" % err
        missing, present, err = _stored_plan(row, chunks, stored, poisoned)
        if err or missing or not present:
            return None, "receipt append was not observable%s" % (
                ": %s" % err if err else "")
        if timing is not None:
            timings, timing_poisoned, timing_err = _stored_timings()
            if timing_err is None and row["id"] not in timing_poisoned \
                    and row["id"] not in timings:
                eventledger.append_unlocked(path, timing)
        if duplicate:
            return "repaired" if needed else "existing", None
        return "appended", None


def _record_binding(receipt, repo, artifact):
    importing_repo = _repo_identity(repo)
    if not importing_repo:
        return None, None, "importing repository identity is unreadable"
    # Re-lock the receipt ledger across the binding append. Import validation
    # and receipt placement happen first, but releasing that lock before the
    # authority write would let a conflicting receipt/chunk append enter in the
    # gap and leave a durable binding for an object the ledger now poisons.
    with eventledger.locked(gate.receipts_path()) as receipt_held:
        if not receipt_held:
            return None, None, "receipt ledger lock unavailable for binding"
        durable_err = _durable_receipt_err(receipt)
        if durable_err:
            return None, None, ("receipt is not durably available for binding: "
                                "%s" % durable_err)
        with eventledger.locked(bindings_path()) as held:
            if not held:
                return None, None, "canonical binding lock unavailable"
            rows, err = _binding_rows()
            if err:
                return None, None, err
            existing, err = _matching_binding(
                rows, receipt["id"], importing_repo)
            if err:
                return None, None, err
            if existing is not None:
                err = _binding_err(existing, receipt)
                return (existing, "existing", None) if err is None else \
                    (None, None, err)
            binding = _binding_row(receipt, importing_repo, artifact)
            if not eventledger.append_unlocked(bindings_path(), binding):
                return None, None, "canonical binding append failed"
            return binding, "appended", None


def _fab_completion_rows():
    rows, unavailable = eventledger.checked_events(
        fab_completions_path(), strict=True)
    if unavailable:
        return None, "canonical Fab completions unavailable: %s" % unavailable
    for row in rows:
        err = _fab_completion_err(row)
        if err:
            return None, "canonical Fab completion is malformed: %s" % err
    return rows, None


def _fab_completion_row(receipt, importing_repo, artifact, authority):
    row = {"v": FAB_COMPLETION_VERSION, "event": FAB_COMPLETION_EVENT,
           "ts": pk.now_ts(), "receipt": receipt["id"],
           "importing_repo": importing_repo,
           "artifact": {name: artifact[name]
                        for name in FAB_ARTIFACT_REQUIRED},
           "authority": authority}
    row["id"] = _fab_completion_id(row)
    return row


def _record_fab_completion(receipt, repo, artifact, authority):
    importing_repo = _repo_identity(repo)
    if not importing_repo:
        return None, None, "importing repository identity is unreadable"
    row = _fab_completion_row(
        receipt, importing_repo, artifact, authority)
    err = _fab_completion_err(row, receipt)
    if err:
        return None, None, err
    with eventledger.locked(fab_completions_path()) as held:
        if not held:
            return None, None, "canonical Fab completion lock unavailable"
        rows, err = _fab_completion_rows()
        if err:
            return None, None, err
        hits = [stored for stored in rows
                if stored.get("receipt") == receipt["id"]
                and stored.get("importing_repo") == importing_repo]
        if len(hits) > 1:
            return None, None, (
                "multiple Fab completions exist for one receipt/repository")
        if hits:
            if hits[0].get("id") != row["id"]:
                return None, None, (
                    "stored Fab completion conflicts with this authority")
            return hits[0], "existing", None
        if not eventledger.append_unlocked(fab_completions_path(), row):
            return None, None, "canonical Fab completion append failed"
        return row, "appended", None


def _legacy_import_rows(deadline=None):
    """Historical audit pointers, with an optional monotonic read deadline."""
    try:
        with open(imports_path(), "rb") as f:
            rows, n = [], 0
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return None, _CENSUS_TIMEOUT
                raw = f.readline(eventledger.MAX_EVENT_BYTES + 1)
                if not raw:
                    return rows, None
                n += 1
                if len(raw) > eventledger.MAX_EVENT_BYTES:
                    return None, ("historical import row %d exceeds %d bytes"
                                  % (n, eventledger.MAX_EVENT_BYTES))
                if deadline is not None and time.monotonic() >= deadline:
                    return None, _CENSUS_TIMEOUT
                if not raw.endswith(b"\n"):
                    continue                 # torn tail is outside durability
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return None, "historical import row %d is malformed" % n
                if not isinstance(row, dict):
                    return None, "historical import row %d is not an object" % n
                rows.append(row)
                if deadline is not None and time.monotonic() >= deadline:
                    return None, _CENSUS_TIMEOUT
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return None, "historical import ledger unreadable: %s" % exc


def _legacy_pointer(pointer):
    """Only audit rows durably written before bindings existed may migrate."""
    stamp = pk.parse_ts_epoch(pointer.get("ts"))
    cutoff = pk.parse_ts_epoch(LEGACY_IMPORT_CUTOFF)
    return pointer.get("event") == "gate-import" \
        and "importing_repo" not in pointer \
        and stamp is not None and cutoff is not None and stamp < cutoff


def binding_candidate_ids(repo, budget_s=None):
    """(candidate ids, fatal error, legacy warning) for this repository.

    The production receipt projection uses this one-read index before invoking
    canonical_binding per candidate; scanning the binding ledger for every
    historical receipt would turn the global gate ledger into an O(rows²) hook.

    Audit pointers are compatibility evidence, not authority. Their worktree
    paths commonly disappear after GC, so distinct extant repositories are
    inspected only once. Canonical bindings are a completed authority answer and
    survive a later legacy timeout/malformed path: that uncertainty is returned
    separately as `legacy warning`, never promoted into a fatal error that erases
    already-proven ids. Other readers retain the complete compatibility scan.
    """
    projscope.spend_or_raise("binding candidate census")
    started = time.monotonic()
    deadline = None if budget_s is None else started + budget_s
    ambient_deadline = projscope.deadline()
    pointer_deadline = ambient_deadline if deadline is None else deadline
    if ambient_deadline is not None and (pointer_deadline is None
                                         or ambient_deadline < pointer_deadline):
        pointer_deadline = ambient_deadline
    candidates = set()

    def remaining():
        return None if deadline is None else deadline - time.monotonic()

    def expired(done, total=None, phase="repository identity reads"):
        progress = "%d/?" % done if total is None else "%d/%d" % (done, total)
        return frozenset(candidates), None, (
            "historical import repository census exceeded %.3fs during %s "
            "after %s distinct paths; canonical bindings remain valid, legacy "
            "placement is UNKNOWN" % (budget_s, phase, progress))

    left = remaining()
    if left is not None and left <= 0:
        return expired(0)
    ambient = projscope.spend_or_raise("binding importing repository identity")
    child_left = left if ambient is None else (
        ambient if left is None else min(left, ambient))
    importing_repo = _repo_identity(
        repo, timeout=30 if child_left is None else min(30, child_left))
    projscope.spend_or_raise("binding importing identity result")
    if not importing_repo:
        return frozenset(), "importing repository identity is unreadable", None
    left = remaining()
    if left is not None and left <= 0:
        return expired(0, phase="canonical binding read")
    bindings, err = _binding_rows(pointer_deadline)
    if err == _CENSUS_TIMEOUT:
        if ambient_deadline is not None and (
                deadline is None or ambient_deadline <= deadline):
            raise projscope.Expired(
                "budget spent during canonical binding read")
        # THE CANONICAL READ IS NOT THE LEGACY CENSUS, and `expired()` is the
        # LEGACY census's answer. Reached from here it returns `candidates`,
        # which is still the EMPTY set initialised above — empty precisely
        # because the canonical read did not happen — under a warning that
        # says "canonical bindings remain valid, legacy placement is UNKNOWN".
        # Both halves of that sentence are false here: the canonical read is
        # exactly what is unknown, and there are no proven ids to remain
        # valid. A caller then sees no error, zero proven ids and a warning
        # about a DIFFERENT subsystem, which is an ABSENCE CLAIM minted by a
        # timeout.
        #
        # This function's contract — a legacy timeout must never erase
        # ALREADY-PROVEN ids — is untouched by refusing here, because nothing
        # has been proven yet at this point in the function. So the honest
        # answer is the fatal channel: no ids, and a reason that names the
        # read that did not finish.
        return frozenset(), (
            "canonical import bindings could not be read within %.3fs; "
            "no receipt id is proven either way" % budget_s), None
    if err:
        return frozenset(), err, None
    counts = {}
    for index, binding in enumerate(bindings):
        projscope.spend_or_raise("binding candidate row %d" % index)
        if binding["importing_repo"] == importing_repo:
            rid = binding["receipt"]
            counts[rid] = counts.get(rid, 0) + 1
    candidates = {rid for rid, count in counts.items() if count == 1}
    left = remaining()
    if left is not None and left <= 0:
        # THE CANONICAL ANSWER IS COMPLETE HERE, so this is the first
        # boundary where `expired()` tells the truth: the proven ids ride
        # back and only LEGACY placement is unknown. The phase names the read
        # that did NOT happen, which is the one below, not the one above.
        return expired(0, phase="audit pointer read")
    projscope.spend_or_raise("binding legacy pointer read")
    pointers, pointer_err = _legacy_import_rows(deadline=pointer_deadline)
    if pointer_err:
        if pointer_err == _CENSUS_TIMEOUT:
            if ambient_deadline is not None and (deadline is None
                                                  or ambient_deadline <= deadline):
                raise projscope.Expired(
                    "budget spent during binding legacy pointer read")
            return expired(0, phase="audit pointer read")
        projscope.spend_or_raise("binding legacy pointer result")
        return frozenset(candidates), None, (
            "legacy placement is UNKNOWN: %s" % pointer_err)
    projscope.spend_or_raise("binding legacy pointer result")
    by_repo = {}
    for pointer_index, pointer in enumerate(pointers):
        projscope.spend_or_raise(
            "binding legacy pointer %d" % pointer_index)
        left = remaining()
        if left is not None and left <= 0:
            return expired(len(by_repo), phase="audit pointer grouping")
        if not _legacy_pointer(pointer) or not pointer.get("receipt"):
            continue
        raw = str(pointer.get("repo") or "").strip()
        if not raw:
            continue
        try:
            path = os.path.abspath(raw)
            if "\0" in path:
                raise ValueError("embedded NUL")
        except (OSError, TypeError, ValueError) as exc:
            return frozenset(candidates), None, (
                "legacy placement is UNKNOWN: historical import repository "
                "path is malformed (%s)" % type(exc).__name__)
        by_repo.setdefault(path, set()).add(pointer["receipt"])
    total = len(by_repo)
    for done, path in enumerate(by_repo):
        ambient = projscope.spend_or_raise(
            "binding repository identity %d" % done)
        left = remaining()
        if left is not None and left <= 0:
            return expired(done, total)
        child_left = left if ambient is None else (
            ambient if left is None else min(left, ambient))
        identity = _repo_identity(
            path, timeout=30 if child_left is None else min(30, child_left))
        projscope.spend_or_raise("binding repository identity result")
        left = remaining()
        if left is not None and left <= 0:
            return expired(done + 1, total)
        if identity == importing_repo:
            candidates.update(by_repo[path])
    projscope.spend_or_raise("binding candidate census completion")
    return frozenset(candidates), None, None


def _record_audit(provenance):
    """Append one audit pointer, or observe the same pointer already durable."""
    with eventledger.locked(imports_path()) as held:
        if not held:
            return None, "audit pointer lock unavailable; canonical binding remains durable"
        rows, err = _legacy_import_rows()
        if err:
            return None, "%s; canonical binding remains durable" % err
        def same_import(pointer):
            if pointer.get("event") != "gate-import" \
                    or pointer.get("receipt") != provenance["receipt"]:
                return False
            identity = pointer.get("importing_repo")
            if identity:
                return identity == provenance["importing_repo"]
            if not _legacy_pointer(pointer):
                return False
            identity = _repo_identity(str(pointer.get("repo") or ""))
            return identity == provenance["importing_repo"]
        existing = any(same_import(pointer) for pointer in rows)
        if existing:
            return "existing", None
        if not eventledger.append_unlocked(imports_path(), provenance):
            return None, "audit pointer append failed; canonical binding remains durable"
        return "appended", None


def _migrate_binding(receipt, repo, importing_repo):
    """Backfill one old pointer only after its artifact re-verifies now.

    Focused v6 is deliberately excluded. Its travelling authority is the live
    challenge-framed custody object passed to import_routed_focus; neither a
    stored receipt nor an audit pointer can reconstruct that event afterward.
    """
    if receipt.get("v") == FOCUSED_VERSION:
        return None, ("no canonical binding; historical focused receipt custody "
                      "cannot be reconstructed — authority UNKNOWN")
    rows, err = _legacy_import_rows()
    if err:
        return None, err
    failures = []
    for pointer in rows:
        if not _legacy_pointer(pointer) \
                or pointer.get("receipt") != receipt.get("id"):
            continue
        placed = _repo_identity(str(pointer.get("repo") or ""))
        if placed != importing_repo:
            failures.append("recorded repository is missing or different")
            continue
        artifact = pointer.get("artifact")
        if type(artifact) is not str or not artifact:
            failures.append("recorded artifact path is missing")
            continue
        found, identity, load_err = _load_rows(artifact)
        if load_err:
            failures.append(load_err)
            continue
        candidate, pick_err = _pick(found, receipt.get("id"))
        if pick_err or _canonical(candidate) != _canonical(receipt):
            failures.append(pick_err or "recorded artifact receipt differs")
            continue
        _chunks, _timing, object_err = _artifact_object_err(candidate, found)
        if object_err:
            failures.append(object_err)
            continue
        if placement_err(receipt, repo):
            failures.append("receipt no longer places in this repository")
            continue
        binding, _state, bind_err = _record_binding(receipt, repo, identity)
        return binding, bind_err
    detail = failures[0] if failures else "no historical pointer"
    return None, "no canonical binding; historical backfill unavailable: %s" % detail


def _receipt_shape_err(receipt):
    if not isinstance(receipt, dict):
        return "receipt is malformed: receipt row is not an object"
    err = _schema_err(receipt, routed_focus=receipt.get("v") == FOCUSED_VERSION)
    if err:
        return "receipt is malformed: %s" % err
    try:
        if gate._receipt_id(receipt) != receipt.get("id"):
            return "receipt content id does not resolve"
    except ValueError as exc:
        return "receipt content id is unreadable: %s" % exc
    return None


def canonical_binding(receipt, repo, migrate=True):
    """(binding, err) for this receipt in this repository; UNKNOWN is loud.

    The receipt ledger lock spans durable-object validation through the final
    binding decision. A conflicting receipt or chunk cannot enter between those
    observations and turn the returned authority stale before it is returned.
    """
    projscope.spend_or_raise("canonical binding")
    # Compatibility migration writes authority. A deadline-bounded observation
    # may read existing authority but must not begin that irreversible repair and
    # then be interrupted before it can publish the result.
    if migrate and projscope.deadline() is not None:
        migrate = False
    err = _receipt_shape_err(receipt)
    if err:
        return None, err
    importing_repo = _repo_identity(repo)
    projscope.spend_or_raise("binding repository identity result")
    if not importing_repo:
        return None, "importing repository identity is unreadable"
    path = gate.receipts_path()
    left = projscope.spend_or_raise("receipt ledger lock")
    lock = eventledger.locked(path) if left is None \
        else eventledger.locked(path, timeout=left)
    with lock as held:
        projscope.spend_or_raise("receipt ledger lock result")
        if not held:
            return None, "receipt ledger lock unavailable — authority UNKNOWN"
        durable_err = _durable_receipt_err(receipt)
        if durable_err:
            return None, "%s — authority UNKNOWN" % durable_err
        left = projscope.spend_or_raise("canonical binding ledger lock")
        lock = eventledger.locked(bindings_path()) if left is None \
            else eventledger.locked(bindings_path(), timeout=left)
        with lock as binding_held:
            projscope.spend_or_raise("canonical binding ledger lock result")
            if not binding_held:
                return None, ("canonical import bindings unavailable: binding lock "
                              "unavailable — authority UNKNOWN")
            projscope.spend_or_raise("canonical binding row match")
            rows, err = _binding_rows()
            projscope.spend_or_raise("canonical binding row match result")
            if err:
                return None, err
            binding, err = _matching_binding(
                rows, receipt["id"], importing_repo)
            if err:
                return None, err
            if binding is not None:
                err = _binding_err(binding, receipt)
                if err:
                    return None, err
                err = placement_err(receipt, repo)
                if err:
                    return None, ("canonical binding placement is UNKNOWN: %s"
                                  % err)
                projscope.spend_or_raise("canonical binding result")
                return binding, None
    if migrate:
        projscope.spend_or_raise("canonical binding migration")
        binding, err = _migrate_binding(receipt, repo, importing_repo)
        if binding is not None:
            return canonical_binding(receipt, repo, migrate=False)
        if err:
            return None, err
    return None, "no canonical binding for this receipt/repository"


def repository_authorization(receipt, repo, migrate=True):
    """(True|False|None, reason) for one receipt at one repository authority."""
    projscope.spend_or_raise("receipt repository authorization")
    err = _receipt_shape_err(receipt)
    if err:
        return None, err
    importing_repo = _repo_identity(repo)
    projscope.spend_or_raise("binding repository identity result")
    if not importing_repo:
        return None, "importing repository identity is unreadable"
    origin = _repo_identity(str(receipt.get("repo_id") or ""))
    projscope.spend_or_raise("receipt origin identity result")
    if origin == importing_repo:
        left = projscope.spend_or_raise("receipt ledger lock")
        lock = eventledger.locked(gate.receipts_path()) if left is None \
            else eventledger.locked(gate.receipts_path(), timeout=left)
        with lock as held:
            projscope.spend_or_raise("receipt ledger lock result")
            if not held:
                return None, "receipt ledger lock unavailable — authority UNKNOWN"
            durable_err = _durable_receipt_err(receipt)
            if durable_err:
                return None, "%s — authority UNKNOWN" % durable_err
        projscope.spend_or_raise("receipt repository authorization result")
        return True, None
    binding, err = canonical_binding(receipt, repo, migrate=migrate)
    projscope.spend_or_raise("receipt canonical authorization result")
    if binding is not None:
        return True, None
    return (False if origin is not None else None), err


def _import_receipt(artifact, repo, want_id=None, actor=None,
                    routed_focus=None, fab_authority=None):
    """Shared import ladder; optional transport authority is fail-closed."""
    if fab_authority is not None:
        fab_authority, err = _normalized_fab_authority(fab_authority)
        if err:
            return None, None, err
    rows, artifact_identity, err = _load_rows(artifact)
    if err:
        return None, None, err
    row, err = _pick(rows, want_id)
    if err:
        return None, None, err
    routed = routed_focus is not None
    err = _schema_err(row, routed_focus=routed)
    if err:
        return None, None, err
    if routed:
        if not isinstance(routed_focus, dict) or set(routed_focus) != {
                "receipt", "head", "tree", "node", "challenge"}:
            return None, None, ("routed focused custody is malformed — only "
                                "gateroute's complete live-session record admits v6")
        if row.get("v") != FOCUSED_VERSION:
            return None, None, ("routed focused custody accompanies receipt v%r, "
                                "not v6" % row.get("v"))
        if _canonical(row) != _canonical(routed_focus["receipt"]):
            return None, None, ("focused artifact differs from the receipt object "
                                "read from the challenge-framed live session")
        if row.get("head") != routed_focus["head"] \
                or row.get("tree") != routed_focus["tree"]:
            return None, None, ("focused receipt does not bind the exact head/tree "
                                "the local router shipped")
        host_block = row.get("host")
        if not isinstance(host_block, dict) \
                or host_block.get("node") != routed_focus["node"]:
            return None, None, ("focused receipt host does not match the node "
                                "reported on the live transport channel")
    try:
        recomputed = gate._receipt_id(row)
    except ValueError as exc:
        return None, None, "receipt id is not computable: %s" % exc
    if recomputed != row.get("id"):
        return None, None, ("content id mismatch: stored %s, recomputed %s — "
                            "the row was edited after minting and no longer "
                            "resolves" % (row.get("id"), recomputed))
    chunks, timing, err = _artifact_object_err(row, rows)
    if err:
        return None, None, err
    # Identity is a PRECONDITION TO PLACEMENT, not a side effect of binding.
    # A caller may retain `--repo /lane` after that worktree was reaped. Git can
    # no longer fold that dead path to the repository common-dir, so importing
    # the globally valid receipt would create a receipt-only half-state and
    # report success about authority no repository received. Refuse before the
    # first durable write; a surviving checkout of the same repository can retry
    # the exact artifact and bind it idempotently.
    if not _repo_identity(repo):
        return None, None, "importing repository identity is unreadable"
    err = placement_err(row, repo)
    if err:
        return None, None, err
    if fab_authority is not None:
        candidate = _fab_completion_row(
            row, _repo_identity(repo), artifact_identity, fab_authority)
        err = _fab_completion_err(candidate, row)
        if err:
            return None, None, err
    completion_state = None
    if fab_authority is not None:
        _completion, completion_state, err = _record_fab_completion(
            row, repo, artifact_identity, fab_authority)
        if err:
            return row, "completion-pending", err
    receipt_state, err = _ensure_receipt(row, chunks, timing=timing)
    if err:
        return None, None, err
    binding, binding_state, err = _record_binding(
        row, repo, artifact_identity)
    if err:
        return None, None, err
    # Audit syntax is a POINTER only. Authority already lives in the canonical
    # binding above, so an audit failure is loud but cannot erase a completed
    # import or turn a receipt-only half-state into authority. A duplicate still
    # walks this leg: the prior attempt may have completed receipt+binding and
    # failed only its audit append, and retry is the repair path for that state.
    duplicate = binding_state == "existing" and receipt_state == "existing" \
        and completion_state in (None, "existing")
    # Origin is READ, never typed and never parsed-into-existence: origin_repo
    # comes from the receipt's own repo_id. Generic imports keep run/node absent;
    # a routed focus records them only because the live challenge-framed session
    # independently supplied both before this artifact was written.
    provenance = {"event": "gate-import", "ts": pk.now_ts(),
                  "receipt": row["id"], "artifact": artifact_identity["path"],
                  "origin_repo": row.get("repo_id"), "repo": repo,
                  "importing_repo": binding["importing_repo"]}
    if routed:
        provenance.update({"transport": "helm-gateroute-v1",
                           "origin_run": routed_focus["challenge"],
                           "origin_node": routed_focus["node"]})
    if row.get("failure_chunks"):
        provenance["chunks"] = list(row["failure_chunks"])
    actor = actor or _self_actor()
    if actor:
        provenance["actor"] = actor
    audit_state, warning = _record_audit(provenance)
    if duplicate:
        verdict = "audit-repaired" if audit_state == "appended" else "duplicate"
    else:
        verdict = "repaired" if receipt_state == "repaired" \
            or completion_state == "appended" and binding_state == "existing" \
            else "imported"
    return row, verdict, warning


def import_receipt(artifact, repo, want_id=None, actor=None):
    """Generic artifact door: focused v6 receipts always refuse."""
    return _import_receipt(artifact, repo, want_id=want_id, actor=actor)


def import_fab_receipt(artifact, repo, authority, want_id=None, actor=None):
    """Fab v2 door: bind exact completion authority beside generic placement."""
    return _import_receipt(
        artifact, repo, want_id=want_id, actor=actor,
        fab_authority=authority)


def import_routed_focus(artifact, repo, custody, want_id=None, actor=None):
    """Challenge-framed gateroute door for one focused v6 receipt."""
    return _import_receipt(artifact, repo, want_id=want_id, actor=actor,
                           routed_focus=custody)


def _self_actor():
    """The SEAT, resolved by the identity law (seats.acting_seat: declared
    name, then the session->row map, then the auto-name floor) — never the OS
    user as the value. The first live imports recorded actor=<user>@<host> for
    every seat on the box because own_name() reads only the env and importer
    shells declare none: the git-metadata-never-proves-who-wrote-it shape in
    a brand-new field. A provenance field that cannot distinguish actors is a
    hostname with extra steps. An unanswered law
    leaves actor ABSENT rather than replacing identity with process location."""
    # `acting_seat` AND ITS FLOOR, deliberately — this is the one place the
    # minted name is the RIGHT answer, and the repo pins it: importer shells
    # declare no HELM_CHAT_NAME, so requiring an admitted actor here would put
    # the OS user back in a provenance field (or empty it) for the ordinary
    # case. Routing this through helm.actors was tried and reverted; the arms
    # in tests/test_gate_import.ActorResolutionTest are the reason, and they
    # state the contract in their own docstrings.
    try:
        return seats.acting_seat() or None
    except Exception:
        return None


IMPORT_USAGE = "gate import <artifact.jsonl> [--repo PATH] [--id RECEIPT-ID]"


def cmd_import(rest):
    rest = list(rest or ())
    def opt(name):
        if name in rest:
            i = rest.index(name)
            if i + 1 >= len(rest):
                print("gate import: %s needs a value" % name, file=sys.stderr)
                return None, True
            v = rest[i + 1]
            del rest[i:i + 2]
            return v, False
        return None, False
    repo, bad_p = opt("--repo")
    want, bad_i = opt("--id")
    if bad_p or bad_i:
        return 2
    artifact = rest.pop(0) if rest and not rest[0].startswith("-") else None
    from .cli import guard_tail
    rc = guard_tail("helm gate import", rest, usage=IMPORT_USAGE)
    if rc is not None:
        return rc
    if artifact is None:
        print("usage: helm " + IMPORT_USAGE, file=sys.stderr)
        return 2
    repo = os.path.abspath(repo or os.getcwd())
    row, verdict, err = import_receipt(artifact, repo, want_id=want)
    if err and verdict is None:
        print("gate import: REFUSED — %s" % err, file=sys.stderr)
        return 2
    rc = _verdict_lines(row, verdict, err)
    # LAST WORD ON EVERY PATH. The cascade above owns the verdict and the exit
    # code; this owns the one fact none of those lines carry.
    _disclose(repo, row)
    return rc


def _verdict_lines(row, verdict, err):
    """The verdict cascade, unchanged — extracted only so one disclosure can
    follow EVERY one of its exits instead of being pasted onto each."""
    if verdict == "duplicate":
        print("gate import: %s already has a complete receipt, canonical "
              "binding, and audit pointer — no-op" % row["id"])
        if err:
            print("gate import: WARNING — %s" % err, file=sys.stderr)
            return 1
        return 0
    if verdict == "audit-repaired":
        print("gate import: %s repaired — missing audit pointer installed in %s"
              % (row["id"], imports_path()))
        return 0
    if verdict == "repaired":
        print("gate import: %s repaired — missing validated failure chunks "
              "installed" % row["id"])
        if err:
            print("gate import: WARNING — %s" % err, file=sys.stderr)
            return 1
        print("gate import: audit pointer in %s" % imports_path())
        return 0
    if err:
        print("gate import: %s appended — canonical binding durable" % row["id"])
        print("gate import: WARNING — %s" % err, file=sys.stderr)
        return 1
    print("gate import: %s appended — audit pointer in %s"
          % (row["id"], imports_path()))
    return 0
