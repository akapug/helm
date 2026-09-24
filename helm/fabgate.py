"""Helm side of Fab's durable gate-job boundary.

Fab owns dispatch, authoritative remote job existence, state measurement and
artifact retrieval.  Helm owns the exact gate request identity, disposable
following, loud detach output and receipt reconciliation.  The boundary is a
small duck-typed adapter:

``submit(request)``
    Return the exact v2 submit event: admitted request, generation-bound handle,
    disposition and wire snapshot. Submit-or-join is atomic at Fab's sink; Helm
    keeps no existence mirror.
``observe(handle)`` / ``wait(handle, timeout_s)``
    Return one exact v2 ``gate-job`` event. Losing the caller must not signal the
    remote job; stale generation is a terminal ``SUPERSEDED`` observation.
``fetch_receipt(handle)``
    Return one exact v2 ``gate-fetch`` event binding request identity,
    generation, completion exit, source artifact and fetched artifact.

The external Fab implementation is intentionally not duplicated here.  This
module makes the composition contract executable and testable from either side.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import signal
import sys
import time

from . import gate, gateauthority, gateimport


REQUEST_VERSION = 2
HANDLE_VERSION = 2
EVENT_VERSION = 2
KEY_FORMAT = "helm-fab-gate-job-v2"
WHOLE_ARGV = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")
DISPOSITIONS = frozenset(("LAUNCHED", "JOINED", "RECEIPT", "UNKNOWN"))
STATES = frozenset(("QUEUED-KEY", "QUEUED-P0", "QUEUED-SLOT", "RUNNING",
                    "COMPLETED", "CANCELED", "SUPERSEDED", "UNKNOWN"))
QUEUED_STATES = frozenset(("QUEUED-KEY", "QUEUED-P0", "QUEUED-SLOT"))
LIVE_STATES = QUEUED_STATES | frozenset(("RUNNING",))
TERMINAL_STATES = frozenset(("COMPLETED", "CANCELED", "SUPERSEDED"))
OBSERVED_FIELDS = frozenset((
    "v", "event", "key", "job_id", "tree", "sha", "disposition", "node",
    "generation", "identity", "budgets", "snapshot", "reason"))
#: The one field a gate-job event MAY add to OBSERVED_FIELDS: the dispatched
#: node's machine-id hash, measured on the node by `gate.host()`'s recipe. The
#: event stays exact-set: it is OBSERVED_FIELDS, or OBSERVED_FIELDS plus this,
#: and nothing else. Optional so that a Fab writer that predates the field is
#: never refused; see `gateimport._fab_receipt_err` for what it decides.
OBSERVED_NODE_ID = "node_id"
_NODE_ID = re.compile(r"[0-9a-f]{16}\Z")
FETCHED_FIELDS = frozenset((
    "v", "event", "key", "job_id", "tree", "sha", "generation",
    "identity", "source_artifact", "artifact", "artifact_sha256", "exit",
    "receipt", "error"))
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_GENERATION = re.compile(r"run-[0-9a-f]{32}-[1-9][0-9]*-[0-9a-f]{16}\Z")
_SNAPSHOT_FIELDS = frozenset((
    "state", "exit", "exit_class", "artifact", "artifact_sha256", "receipt",
    "queue_elapsed_s", "execution_elapsed_s", "live", "reason"))


class ClientDetached(Exception):
    """The local follower left; the remote job remains owned by Fab."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def whole_scope():
    """The immutable v2 whole-suite request contract."""
    return {"kind": "whole", "argv": list(WHOLE_ARGV)}


def focused_scope(repo):
    """Return the exact current focused-selection contract, or its refusal."""
    plan, err = gate.focus_plan(os.path.realpath(repo))
    if err:
        return None, err
    return {"kind": "focus", "plan": plan}, None


def _json_contract(value, label):
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None, "%s is not a finite JSON value" % label
    try:
        loaded = json.loads(body)
    except ValueError:
        return None, "%s is not readable JSON" % label
    if loaded != value:
        return None, "%s changes under canonical JSON encoding" % label
    return body, None


def _duration(value, label):
    if value is None:
        return None, None
    if type(value) not in (int, float) or value <= 0:
        return None, "%s must be a positive finite number or null" % label
    try:
        value = float(value)
    except OverflowError:
        return None, "%s must be a positive finite number or null" % label
    if not math.isfinite(value):
        return None, "%s must be a positive finite number or null" % label
    return value, None


def _elapsed(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _focus_err(scope):
    plan = scope.get("plan")
    required = {"policy", "trunk", "base", "changed", "selected", "universe"}
    if not isinstance(plan, dict) or set(plan) != required:
        return "focused plan is not helm's exact six-field shape"
    if plan.get("policy") != gate.FOCUS_POLICY:
        return "focused plan names an unknown policy"
    if not all(type(plan.get(name)) is str and _SHA.fullmatch(plan[name])
               for name in ("trunk", "base")):
        return "focused plan does not name exact trunk/base commits"
    for name in ("changed", "selected"):
        values = plan.get(name)
        if not isinstance(values, list) or not values \
                or not all(type(value) is str and value for value in values) \
                or values != sorted(set(values)):
            return "focused plan %s is not a sorted unique non-empty list" % name
    universe = plan.get("universe")
    if type(universe) is not int or universe <= len(plan["selected"]):
        return "focused plan universe does not exceed its selection"
    return None


def _identity_contract(identity):
    required = {"format", "repository", "tree", "scope",
                "interpreter", "runner"}
    if not isinstance(identity, dict) or set(identity) != required \
            or identity.get("format") != KEY_FORMAT:
        return None, "job identity is not helm's exact six-field shape"
    scope = identity.get("scope")
    if isinstance(scope, dict) and scope.get("kind") == "whole":
        return gateimport._fab_identity_contract(identity)
    repository = identity.get("repository")
    if not isinstance(repository, dict) or set(repository) != {"common_dir"} \
            or type(repository.get("common_dir")) is not str \
            or not repository["common_dir"] \
            or not os.path.isabs(repository["common_dir"]) \
            or os.path.normpath(repository["common_dir"]) \
            != repository["common_dir"]:
        return None, "repository identity is not one canonical absolute path"
    if type(identity.get("tree")) is not str \
            or not _SHA.fullmatch(identity["tree"]):
        return None, "tree identity is not one full lowercase object id"
    scope = identity.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in ("whole", "focus"):
        return None, "scope must be the whole or exact focus contract"
    if scope.get("kind") == "whole" and scope not in (
            whole_scope(), {"kind": "whole", "argv": list(gateauthority.SUITE_ARGV)}):
        return None, "whole scope differs from helm's canonical suite"
    if scope.get("kind") == "focus":
        if set(scope) != {"kind", "plan"}:
            return None, "focused scope must carry exactly kind and plan"
        err = _focus_err(scope)
        if err:
            return None, err
    interpreter = identity.get("interpreter")
    if not isinstance(interpreter, dict) or set(interpreter) != {
            "name", "version", "language", "executable"} \
            or not all(type(value) is str and value
                       for value in interpreter.values()) \
            or not os.path.isabs(interpreter["executable"]):
        return None, "interpreter identity is not helm's exact four-field shape"
    runner = identity.get("runner")
    gate_args = [] if scope["kind"] == "whole" else ["--focus"]
    if not isinstance(runner, dict) or set(runner) != {
            "format", "argv", "wrapper_version", "cgroup"} \
            or runner.get("format") != "fab-gate-runner-v1" \
            or runner.get("argv") != [
                "-m", "helm", "gate", "run", "--repo", "."] + gate_args \
            or type(runner.get("wrapper_version")) is not str \
            or not _KEY.fullmatch(runner["wrapper_version"]) \
            or runner.get("cgroup") not in gateimport.FAB_RUNNER_CGROUPS:
        return None, "runner identity is not Fab's exact measured wrapper shape"
    return _json_contract(identity, "job identity")


def _request_contract(job):
    if not isinstance(job, dict) or set(job) != {
            "v", "key", "identity", "budgets"} \
            or type(job.get("v")) is not int or job["v"] != REQUEST_VERSION \
            or type(job.get("key")) is not str or not _KEY.fullmatch(job["key"]):
        return None, "gate-job request is malformed"
    body, err = _identity_contract(job.get("identity"))
    if err:
        return None, "gate-job request is malformed: %s" % err
    key = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
    if key != job["key"]:
        return None, "gate-job request key does not match its identity"
    budgets = job.get("budgets")
    if not isinstance(budgets, dict) or set(budgets) != {
            "queue_s", "execution_s"}:
        return None, "gate-job request budgets are malformed"
    normalized = {}
    for name, label in (("queue_s", "queue timeout"),
                        ("execution_s", "execution timeout")):
        normalized[name], err = _duration(budgets[name], label)
        if err:
            return None, "gate-job request budgets are malformed: %s" % err
    frozen = dict(job, budgets=normalized)
    request_body, err = _json_contract(frozen, "gate-job request")
    if err:
        return None, "gate-job request is malformed: %s" % err
    return json.loads(request_body), None


def _request_err(job):
    _frozen, err = _request_contract(job)
    return err


def _execution_err(job):
    if job["identity"]["scope"]["kind"] == "focus":
        return ("focused Fab execution has no live challenge-framed custody; "
                "use `helm gate run --focus --box HOST`")
    return None


def request(repo, tree, scope, interpreter, runner, queue_timeout=None,
            execution_timeout=None):
    """Build one exact, versioned gate-job request and key.

    Budgets are deliberately outside the key: followers may have different
    patience, while the suite object remains repository + tree + scope +
    interpreter + runner.  The first submitter's queue/execution budgets are
    separate named values for Fab to enforce; a join never turns queue wait into
    suite runtime.
    """
    identity = gateimport._repo_identity(repo)
    if not identity:
        return None, "repository identity is unreadable"
    tree = str(tree or "").strip().lower()
    if not _SHA.fullmatch(tree):
        return None, "tree must be one full lowercase object id"
    if gateimport._tree_of(repo, tree) != tree:
        return None, "tree does not resolve to a tree object in this repository"
    if scope == "whole":
        scope = whole_scope()
    if not isinstance(scope, dict) or scope.get("kind") not in ("whole", "focus"):
        return None, "scope must be the whole or exact focus contract"
    if scope.get("kind") == "whole" and scope not in (
            whole_scope(), {"kind": "whole", "argv": list(gateauthority.SUITE_ARGV)}):
        return None, "whole scope differs from helm's canonical suite"
    if scope.get("kind") == "focus":
        if set(scope) != {"kind", "plan"}:
            return None, "focused scope must carry exactly kind and plan"
        err = _focus_err(scope)
        if err:
            return None, err
    if not isinstance(interpreter, dict) or set(interpreter) != {
            "name", "version", "language", "executable"} \
            or not all(type(value) is str and value
                       for value in interpreter.values()) \
            or not os.path.isabs(interpreter["executable"]):
        return None, "interpreter identity is not helm's exact four-field shape"
    if not isinstance(runner, dict) or not runner:
        return None, "runner identity must be a non-empty object"
    queue, err = _duration(queue_timeout, "queue timeout")
    if err:
        return None, err
    execution, err = _duration(execution_timeout, "execution timeout")
    if err:
        return None, err
    payload = {"format": KEY_FORMAT,
               "repository": {"common_dir": identity},
               "tree": tree, "scope": scope,
               "interpreter": interpreter, "runner": runner}
    body, err = _identity_contract(payload)
    if err:
        return None, err
    key = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
    return {"v": REQUEST_VERSION, "key": key, "identity": payload,
            "budgets": {"queue_s": queue, "execution_s": execution}}, None


def _exit(raw, present):
    if not present or raw is None or raw == "":
        return None, "ABSENT"
    if type(raw) is int and 0 <= raw <= 255:
        return raw, "EXACT"
    if type(raw) is str and re.fullmatch(r"0|[1-9][0-9]{0,2}", raw):
        value = int(raw)
        if value <= 255:
            return value, "EXACT"
    return None, "UNKNOWN"


def snapshot(raw):
    """Normalize one Fab observation without interpreting numeric bands."""
    if not isinstance(raw, dict):
        return {"state": "UNKNOWN", "exit": None, "exit_state": "UNKNOWN",
                "artifact": None, "receipt": None,
                "receipt_state": "UNKNOWN"}
    state = raw.get("state")
    state = state if type(state) is str and state in STATES else "UNKNOWN"
    code, exit_state = _exit(raw.get("exit"), "exit" in raw)
    if exit_state == "UNKNOWN":
        state = "UNKNOWN"
    artifact = raw.get("artifact")
    artifact = artifact if type(artifact) is str and artifact else None
    marker = raw.get("receipt_state")
    marker_present = "receipt_state" in raw
    receipt_raw = raw.get("receipt")
    receipt_absent = "receipt" not in raw or receipt_raw in (None, "")
    receipt_exact = type(receipt_raw) is str \
        and gate._ID.fullmatch(receipt_raw)
    if marker_present:
        if marker == "ABSENT" and receipt_absent:
            receipt, receipt_state = None, "ABSENT"
        elif marker == "EXACT" and receipt_exact:
            receipt, receipt_state = receipt_raw, "EXACT"
        else:
            receipt, receipt_state = None, "UNKNOWN"
    elif receipt_absent:
        receipt, receipt_state = None, "ABSENT"
    elif receipt_exact:
        receipt, receipt_state = receipt_raw, "EXACT"
    else:
        receipt, receipt_state = None, "UNKNOWN"
    digest = raw.get("artifact_sha256")
    digest = digest if type(digest) is str and _KEY.fullmatch(digest) else None
    reason = raw.get("reason")
    reason = reason if type(reason) is str and reason else None
    out = {"state": state, "exit": code, "exit_state": exit_state,
           "artifact": artifact, "artifact_sha256": digest,
           "receipt": receipt, "receipt_state": receipt_state,
           "live": raw.get("live") if type(raw.get("live")) is bool else None,
           "reason": reason}
    for source, target in (("queue_elapsed_s", "queue_elapsed_s"),
                           ("execution_elapsed_s", "execution_elapsed_s")):
        out[target] = _elapsed(raw.get(source))
    return out


def _handle(raw, key):
    if not isinstance(raw, dict) or set(raw) != {
            "v", "key", "job_id", "host", "generation"} \
            or raw.get("v") != HANDLE_VERSION:
        return None, "Fab returned no exact v%d durable job handle" \
            % HANDLE_VERSION
    if raw.get("key") != key or not _KEY.fullmatch(str(raw.get("key") or "")):
        return None, "Fab returned a handle for a different job key"
    job_id, host, generation = (raw.get("job_id"), raw.get("host"),
                                raw.get("generation"))
    if job_id != "gate-" + key:
        return None, "Fab returned a non-canonical job id for the request key"
    if type(job_id) is not str or not _ATOM.fullmatch(job_id) \
            or type(host) is not str or not _ATOM.fullmatch(host) \
            or type(generation) is not str \
            or not _GENERATION.fullmatch(generation):
        return None, "Fab returned a handle without exact v2 safe identities"
    return {"v": HANDLE_VERSION, "key": key, "job_id": job_id,
            "host": host, "generation": generation}, None


def _expected(request, handle):
    frozen, err = _request_contract(request)
    if err:
        return None, None, err
    exact, err = _handle(handle, frozen["key"])
    if err:
        return None, None, err
    return frozen, exact, None


def _identity_echo(raw, expected):
    body, err = _identity_contract(raw)
    if err:
        return "Fab completion identity is malformed: %s" % err
    key = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
    if key != expected["key"] or raw != expected["identity"]:
        return "Fab completion identity differs from the admitted request"
    return None


def _wire_snapshot(raw):
    if not isinstance(raw, dict):
        return None, "Fab snapshot is not an object"
    if set(raw) != _SNAPSHOT_FIELDS:
        return None, "Fab snapshot is not the exact v2 ten-field shape"
    reason = raw.get("reason")
    if reason is not None and (type(reason) is not str or not reason):
        return None, "Fab snapshot reason is malformed"
    if raw.get("state") == "UNKNOWN":
        if any(raw.get(name) is not None for name in (
                "exit", "exit_class", "artifact", "artifact_sha256", "receipt",
                "queue_elapsed_s", "execution_elapsed_s")) \
                or raw.get("live") is not False:
            return None, "Fab UNKNOWN snapshot is not the exact v2 null authority"
        return snapshot(raw), None
    if raw.get("state") not in STATES - {"UNKNOWN"} \
            or type(raw.get("live")) is not bool \
            or (raw.get("exit_class") is not None
                and (type(raw["exit_class"]) is not str
                     or not raw["exit_class"])) \
            or (raw.get("artifact") is not None
                and (type(raw["artifact"]) is not str or not raw["artifact"])) \
            or (raw.get("artifact_sha256") is not None
                and (type(raw["artifact_sha256"]) is not str
                     or not _KEY.fullmatch(raw["artifact_sha256"]))) \
            or (raw.get("artifact") is None) \
            != (raw.get("artifact_sha256") is None) \
            or _elapsed(raw.get("queue_elapsed_s")) is None \
            or (raw.get("execution_elapsed_s") is not None
                and _elapsed(raw["execution_elapsed_s"]) is None):
        return None, "Fab snapshot is not the exact v2 normal shape"
    receipt = raw.get("receipt")
    if receipt is not None and (type(receipt) is not str
                                or not gate._ID.fullmatch(receipt)):
        return None, "Fab snapshot receipt id is malformed"
    state = snapshot(raw)
    terminal = state["state"] in TERMINAL_STATES
    if (state["exit_state"] == "ABSENT") != (raw["exit_class"] is None) \
            or state["exit_state"] == "EXACT" and raw["live"] \
            or terminal != (state["exit_state"] == "EXACT") \
            or state["state"] == "CANCELED" and state["exit"] != 143:
        return None, "Fab snapshot exit class/liveness is contradictory"
    if state["exit_state"] == "UNKNOWN":
        return None, "Fab observed exit is UNKNOWN"
    if state["receipt_state"] == "UNKNOWN":
        return None, "Fab observed receipt id is malformed"
    if not raw["live"] and not terminal:
        return None, "Fab non-live job has no terminal authority"
    return state, None


def _observed_event(raw, request, handle):
    expected, exact, err = _expected(request, handle)
    if err:
        return None, err
    if not isinstance(raw, dict) \
            or set(raw) - {OBSERVED_NODE_ID} != OBSERVED_FIELDS \
            or raw.get("v") != EVENT_VERSION \
            or raw.get("event") != "gate-job" \
            or raw.get("disposition") not in DISPOSITIONS:
        return None, "Fab observation is not the exact v%d gate-job event" \
            % EVENT_VERSION
    if OBSERVED_NODE_ID in raw and (
            type(raw[OBSERVED_NODE_ID]) is not str
            or not _NODE_ID.fullmatch(raw[OBSERVED_NODE_ID])):
        return None, "Fab observation node_id is not a 16-hex machine-id hash"
    for name, value in (("key", expected["key"]),
                        ("job_id", exact["job_id"]),
                        ("node", exact["host"])):
        if raw.get(name) != value:
            return None, "Fab observation %s differs from the admitted job" % name
    state, err = _wire_snapshot(raw.get("snapshot"))
    if err:
        return None, err
    reason = raw.get("reason")
    if reason is not None and (type(reason) is not str or not reason):
        return None, "Fab observation reason is malformed"
    if (raw.get("disposition") == "UNKNOWN") != (state["state"] == "UNKNOWN"):
        return None, "Fab observation UNKNOWN disposition/snapshot is contradictory"
    if state["state"] == "UNKNOWN":
        if raw.get("disposition") != "UNKNOWN" \
                or any(raw.get(name) is not None for name in (
                    "tree", "sha", "generation", "identity")) \
                or raw.get("budgets") != {
                    "queue_s": None, "execution_s": None}:
            return None, "Fab UNKNOWN observation is not the exact null authority"
        return {"request": expected, "handle": exact, "event": raw,
                "snapshot": state}, None
    values = (("tree", expected["identity"]["tree"]),)
    for name, value in values:
        if raw.get(name) != value:
            return None, "Fab observation %s differs from the admitted job" % name
    generation = raw.get("generation")
    if type(generation) is not str or not _GENERATION.fullmatch(generation):
        return None, "Fab observation generation is malformed"
    if state["state"] == "SUPERSEDED":
        if generation == exact["generation"] \
                or state["exit"] != 93 \
                or raw.get("disposition") != "JOINED":
            return None, "Fab SUPERSEDED observation is contradictory"
    elif generation != exact["generation"]:
        return None, "Fab observation generation differs from the admitted job"
    budgets = raw.get("budgets")
    if not isinstance(budgets, dict) or set(budgets) != {
            "queue_s", "execution_s"}:
        return None, "Fab observation budgets are malformed"
    for name in ("queue_s", "execution_s"):
        _value, budget_err = _duration(budgets[name], name)
        if budget_err:
            return None, "Fab observation budgets are malformed"
    if budgets != expected["budgets"]:
        return None, "Fab observation budgets differ from the admitted launch"
    if type(raw.get("sha")) is not str or not _SHA.fullmatch(raw["sha"]):
        return None, "Fab observation sha is malformed"
    err = _identity_echo(raw.get("identity"), expected)
    if err:
        return None, err
    return {"request": expected, "handle": exact, "event": raw,
            "snapshot": state}, None


def _fetched_event(raw, observed):
    expected, exact = observed["request"], observed["handle"]
    if not isinstance(raw, dict) or set(raw) != FETCHED_FIELDS \
            or raw.get("v") != EVENT_VERSION \
            or raw.get("event") != "gate-fetch":
        return None, "Fab fetch is not the exact v%d gate-fetch event" \
            % EVENT_VERSION
    values = (("key", expected["key"]), ("job_id", exact["job_id"]),
              ("generation", exact["generation"]),
              ("tree", expected["identity"]["tree"]),
              ("sha", observed["event"]["sha"]))
    for name, value in values:
        if raw.get(name) != value:
            return None, "Fab fetch %s differs from the observed job" % name
    source = raw.get("source_artifact")
    observed_source = observed["snapshot"]["artifact"]
    if observed_source is not None and source != observed_source:
        return None, "Fab fetch source_artifact differs from the observed job"
    err = _identity_echo(raw.get("identity"), expected)
    if err:
        return None, err
    for name in ("source_artifact", "artifact", "error"):
        value = raw.get(name)
        if value is not None and (type(value) is not str or not value):
            return None, "Fab fetch %s is malformed" % name
    digest = raw.get("artifact_sha256")
    if digest is not None and (type(digest) is not str
                               or not _KEY.fullmatch(digest)):
        return None, "Fab fetch artifact_sha256 is malformed"
    if raw.get("artifact") is not None and raw.get("error") is not None:
        return None, "Fab fetch cannot carry both an artifact and an error"
    if raw.get("artifact") is not None and source is None:
        return None, "Fab fetch artifact has no source artifact identity"
    if (raw.get("artifact") is None) != (digest is None):
        return None, "Fab fetch artifact/digest identity is contradictory"
    state = snapshot(raw)
    if state["exit_state"] == "UNKNOWN":
        return None, "Fab artifact retrieval exit is UNKNOWN"
    if state["receipt_state"] == "UNKNOWN":
        return None, "Fab fetch receipt id is malformed"
    return {"event": raw, "snapshot": state}, None


def submit(builder, job):
    """Submit through Fab's authoritative sink; never decide existence locally."""
    frozen, err = _request_contract(job)
    if err:
        return None, err
    err = _execution_err(frozen)
    if err:
        return None, err
    request_body, _err = _json_contract(frozen, "gate-job request")
    failure = None
    for _attempt in range(2):
        attempt = json.loads(request_body)
        _checked, err = _request_contract(attempt)
        if err:
            return None, err
        try:
            raw = builder.submit(attempt)
            break
        except Exception as exc:             # retry is safe only because the sink
            failure = type(exc).__name__     # decides same-key existence atomically
    else:
        return None, "Fab submit is UNKNOWN (%s)" % failure
    if not isinstance(raw, dict) or set(raw) != {
            "v", "event", "disposition", "handle", "snapshot",
            "reason", "request"} \
            or raw.get("v") != EVENT_VERSION \
            or raw.get("event") != "gate-job" \
            or raw.get("disposition") not in DISPOSITIONS:
        return None, "Fab submit returned a malformed gate-job event"
    echoed, err = _request_contract(raw.get("request"))
    if err or echoed["key"] != frozen["key"] \
            or echoed["identity"] != frozen["identity"]:
        return None, "Fab submit did not echo the exact admitted identity"
    if raw["disposition"] == "LAUNCHED" \
            and echoed["budgets"] != frozen["budgets"]:
        return None, "Fab launch did not preserve the submitted budgets"
    handle, err = _handle(raw.get("handle"), frozen["key"])
    if err:
        return None, err
    state, err = _wire_snapshot(raw.get("snapshot"))
    if err:
        return None, err
    if (raw["disposition"] == "UNKNOWN") != (state["state"] == "UNKNOWN"):
        return None, "Fab submit UNKNOWN disposition/snapshot is contradictory"
    reason = raw.get("reason")
    if reason is not None and (type(reason) is not str or not reason):
        return None, "Fab submit reason is malformed"
    return {"disposition": raw["disposition"], "handle": handle,
            "snapshot": state, "request": echoed}, None


def commands(handle, repo):
    """Exact operator commands for one durable remote handle."""
    host = shlex.quote(handle["host"])
    job_id = shlex.quote(handle["job_id"])
    generation = shlex.quote(handle["generation"])
    repo = shlex.quote(os.path.abspath(repo))
    return {"join": "fab gate --join %s %s --generation %s --repo %s"
                    % (host, job_id, generation, repo),
            "status": "fab gate observe --host %s --job %s --generation %s"
                      % (host, job_id, generation),
            "kill": "fab gate kill --host %s --job %s --generation %s"
                    % (host, job_id, generation),
            "import": "fab gate --import %s %s --generation %s --repo %s"
                      % (host, job_id, generation, repo)}


def detach_text(handle, repo, observed, reason):
    """The loud, copy/pasteable detach report.  UNKNOWN stays literal."""
    raw = observed.get("snapshot") if isinstance(observed, dict) \
        and "snapshot" in observed else observed
    state = snapshot(raw)
    exit_text = "UNKNOWN" if state["state"] == "UNKNOWN" else \
        (str(state["exit"]) if state["exit_state"] == "EXACT" else
         ("pending" if state["exit_state"] == "ABSENT" else "UNKNOWN"))
    cmd = commands(handle, repo)
    return "\n".join((
        "CLIENT DETACHED — %s" % reason,
        "remote job: %s" % handle["job_id"],
        "remote state: %s  exit=%s" % (state["state"], exit_text),
        "join: %s" % cmd["join"],
        "status: %s" % cmd["status"],
        "kill: %s" % cmd["kill"],
        "import: %s" % cmd["import"]))


@contextlib.contextmanager
def _detach_signals():
    prior = {}
    def leave(signum, _frame):
        raise ClientDetached("local signal %d" % signum)
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            prior[signum] = signal.signal(signum, leave)
    except (ValueError, OSError):            # non-main threads retain caller policy
        for signum, handler in prior.items():
            signal.signal(signum, handler)
        prior = {}
    try:
        yield
    finally:
        for signum, handler in prior.items():
            signal.signal(signum, handler)


def _reconcile_receipt_id(observed, fetched):
    """The one exact receipt identity named by either authoritative reading."""
    found = []
    for source, row in (("snapshot", observed),
                        ("fetch", snapshot(fetched))):
        if row["receipt_state"] == "UNKNOWN":
            return None, "Fab %s receipt id is malformed" % source
        if row["receipt_state"] == "EXACT":
            found.append(row["receipt"])
    if not found:
        return None, "Fab completion names no exact receipt id"
    if len(set(found)) != 1:
        return None, "Fab snapshot/fetch receipt ids disagree"
    return found[0], None


def reconcile_events(request, handle, observed_raw, fetched_raw, repo):
    """Validate one atomic Fab completion, then import its exact receipt."""
    observed, err = _observed_event(observed_raw, request, handle)
    if err:
        return None, err
    state = observed["snapshot"]
    if state["state"] == "UNKNOWN":
        return {"state": "UNKNOWN", "exit": None,
                "receipt": None, "verdict": None}, \
            observed["event"].get("reason") or "remote gate authority is UNKNOWN"
    if state["state"] == "SUPERSEDED":
        return {"state": "SUPERSEDED", "exit": state["exit"],
                "receipt": None, "verdict": None}, \
            "remote gate job generation was superseded before completion"
    if state["exit_state"] != "EXACT" and not state["artifact"]:
        return None, "remote completion/artifact is not yet readable"
    fetched, err = _fetched_event(fetched_raw, observed)
    if err:
        return {"state": state["state"], "exit": None,
                "receipt": None, "verdict": None}, err
    fetched_state = fetched["snapshot"]
    if state["exit_state"] == "EXACT" \
            and fetched_state["exit_state"] == "EXACT" \
            and state["exit"] != fetched_state["exit"]:
        return {"state": state["state"], "exit": None,
                "receipt": None, "verdict": None}, \
            "Fab snapshot/fetch exits disagree (%d != %d)" % (
                state["exit"], fetched_state["exit"])
    code = fetched_state["exit"] if fetched_state["exit_state"] == "EXACT" \
        else state["exit"]
    if code is None:
        return {"state": state["state"], "exit": None,
                "receipt": None, "verdict": None}, \
            "Fab completion names no exact exit"
    artifact = fetched["event"]["artifact"]
    if type(artifact) is not str or not artifact:
        why = fetched["event"].get("error") \
            or "remote gate receipt is unretrievable"
        # ABSENT on BOTH readings, never merely no artifact: a completion that
        # names a receipt id ran a suite, whatever exit it reports, and its
        # missing artifact is the ordinary unretrievable case.
        if code == gate.EXIT_NOT_RUN_CAPACITY \
                and state["receipt_state"] == "ABSENT" \
                and fetched_state["receipt_state"] == "ABSENT":
            # THE NODE REFUSED IT: helm's own not-run exit, and no artifact —
            # nothing ran, so there is no receipt to retrieve or to owe.
            why = gate.CapacityRefusal(
                "remote gate NOT RUN — node %s refused it on its own capacity "
                "(exit %d: whole-suite cap, memory-stall floor or pressured "
                "tmp); no receipt exists for this job. Re-run it when that "
                "node frees, or on another node (%s)"
                % (observed["handle"]["host"], code, why))
        return {"state": state["state"], "exit": code,
                "receipt": None, "verdict": None}, why
    want_id, err = _reconcile_receipt_id(state, fetched["snapshot"])
    if err:
        return {"state": state["state"], "exit": code,
                "receipt": None, "verdict": None}, err
    if observed["request"]["identity"]["scope"]["kind"] != "whole":
        return {"state": state["state"], "exit": code,
                "receipt": None, "verdict": None}, (
                    "focused Fab completion has no challenge-framed custody; "
                    "plain artifact import remains closed")
    authority = {
        "format": "helm-fab-completion-v2",
        "key": observed["request"]["key"],
        "generation": observed["handle"]["generation"],
        "job_id": observed["handle"]["job_id"],
        "host": observed["handle"]["host"],
        "sha": observed["event"]["sha"],
        "identity": observed["request"]["identity"],
        "budgets": observed["request"]["budgets"],
        "source_artifact": fetched["event"]["source_artifact"],
        "artifact_sha256": fetched["event"]["artifact_sha256"],
        "exit": code,
    }
    if OBSERVED_NODE_ID in observed["event"]:
        authority["node_id"] = observed["event"][OBSERVED_NODE_ID]
    row, verdict, err = gateimport.import_fab_receipt(
        artifact, repo, authority, want_id=want_id)
    if row is None or verdict == "completion-pending":
        return {"state": state["state"], "exit": code,
                "receipt": None, "verdict": verdict}, err
    return {"state": state["state"], "exit": code,
            "receipt": row["id"], "verdict": verdict}, err


def reconcile(builder, request, handle, repo, observed=None):
    """Fetch/import a completed remote artifact; repeated calls are idempotent."""
    observed = observed if observed is not None else builder.observe(handle)
    checked, err = _observed_event(observed, request, handle)
    if err:
        return None, err
    state = checked["snapshot"]
    if state["state"] in ("UNKNOWN", "SUPERSEDED"):
        return reconcile_events(request, handle, observed, None, repo)
    if state["exit_state"] != "EXACT" and not state["artifact"]:
        return None, "remote completion/artifact is not yet readable"
    fetched = builder.fetch_receipt(handle)
    return reconcile_events(request, handle, observed, fetched, repo)


def follow(builder, request, handle, repo, client_timeout=None, out=None):
    """Follow one remote handle; local loss never invokes remote kill."""
    out = out or sys.stderr
    deadline = None if client_timeout is None else time.monotonic() + client_timeout
    observed = None
    pending = None
    try:
        with _detach_signals():
            while True:
                observed = pending if pending is not None else builder.observe(handle)
                pending = None
                checked, err = _observed_event(observed, request, handle)
                if err:
                    raise ClientDetached("remote observation unreadable (%s)" % err)
                state = checked["snapshot"]
                if state["state"] == "UNKNOWN":
                    raise ClientDetached(
                        checked["event"].get("reason")
                        or "remote authority is UNKNOWN")
                if state["state"] == "SUPERSEDED":
                    return reconcile_events(
                        request, handle, observed, None, repo)
                if state["exit_state"] == "EXACT" or state["artifact"]:
                    try:
                        fetched = builder.fetch_receipt(handle)
                    except Exception as exc:
                        raise ClientDetached(
                            "remote receipt recovery failure (%s)"
                            % type(exc).__name__)
                    _checked_fetch, fetch_err = _fetched_event(fetched, checked)
                    if fetch_err:
                        raise ClientDetached(
                            "remote receipt recovery unreadable (%s)" % fetch_err)
                    return reconcile_events(
                        request, handle, observed, fetched, repo)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise ClientDetached("local follower timeout")
                pending = builder.wait(handle, remaining)
    except KeyboardInterrupt:
        reason = "local interrupt"
    except ClientDetached as exc:
        reason = exc.reason
    except Exception as exc:                 # wrapper loss is not remote death
        reason = "local follower failure (%s)" % type(exc).__name__
    print(detach_text(handle, repo, observed, reason), file=out)
    raw = observed.get("snapshot") if isinstance(observed, dict) \
        and "snapshot" in observed else observed
    return {"detached": True, "handle": handle,
            "snapshot": snapshot(raw)}, None


def submit_and_follow(builder, job, repo, client_timeout=None, out=None):
    """One Helm front door: submit/join/recover, then disposable follow."""
    admitted, err = submit(builder, job)
    if err:
        return None, err
    return follow(builder, admitted["request"], admitted["handle"], repo,
                  client_timeout=client_timeout, out=out)


USAGE = ("gate fab contract --repo PATH --tree TREE --interpreter JSON "
         "--runner JSON [--scope JSON] [--queue-timeout S] "
         "[--execution-timeout S]\n"
         "       helm gate fab reconcile --repo PATH --stdin\n"
         "       helm gate fab detached --repo PATH --host HOST --job ID "
         "--key KEY --generation ID --state JSON [--reason TEXT]")


def _options(rest, valued, flags=()):
    rest, out = list(rest), {}
    while rest:
        name = rest.pop(0)
        if name in flags:
            out[name] = True
            continue
        if name not in valued or not rest:
            return None, "unknown or incomplete option %s" % name
        if name in out:
            return None, "option %s was supplied twice" % name
        out[name] = rest.pop(0)
    return out, None


def _json_option(opts, name):
    raw = opts.get(name)
    if raw is None:
        return None, "%s is required" % name
    try:
        return json.loads(raw), None
    except ValueError:
        return None, "%s is not JSON" % name


def cmd(rest):
    """Machine-facing CLI seam consumed by the external Fab wrapper."""
    rest = list(rest or ())
    sub = rest.pop(0) if rest else ""
    if sub == "contract":
        opts, err = _options(rest, {
            "--repo", "--tree", "--interpreter", "--runner", "--scope",
            "--queue-timeout", "--execution-timeout"})
        if err:
            print("gate fab contract: %s" % err, file=sys.stderr)
            return 2
        interpreter, err = _json_option(opts, "--interpreter")
        if err:
            print("gate fab contract: %s" % err, file=sys.stderr)
            return 2
        runner, err = _json_option(opts, "--runner")
        if err:
            print("gate fab contract: %s" % err, file=sys.stderr)
            return 2
        if "--scope" in opts:
            try:
                scope = json.loads(opts["--scope"])
            except ValueError:
                print("gate fab contract: --scope is not JSON", file=sys.stderr)
                return 2
        else:
            scope = "whole"
        budgets = {}
        for name, target in (("--queue-timeout", "queue_timeout"),
                             ("--execution-timeout", "execution_timeout")):
            try:
                budgets[target] = float(opts[name]) if name in opts else None
            except ValueError:
                print("gate fab contract: %s wants a number" % name,
                      file=sys.stderr)
                return 2
        job, err = request(
            os.path.abspath(opts.get("--repo") or os.getcwd()),
            opts.get("--tree"), scope, interpreter, runner, **budgets)
        if err:
            print("gate fab contract: REFUSED — %s" % err, file=sys.stderr)
            return 2
        err = _execution_err(job)
        if err:
            print("gate fab contract: REFUSED — %s" % err, file=sys.stderr)
            return 2
        print(json.dumps(job, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")))
        return 0
    if sub == "reconcile":
        opts, err = _options(rest, {"--repo"}, flags=("--stdin",))
        if err or not opts.get("--stdin"):
            print("gate fab reconcile: %s" % (
                err or "--stdin is required"), file=sys.stderr)
            return 2
        raw = sys.stdin.read(1024 * 1024 + 1)
        try:
            raw_bytes = raw.encode("utf-8")
        except UnicodeError:
            raw_bytes = b""
        if not raw_bytes or len(raw_bytes) > 1024 * 1024:
            print("gate fab reconcile: stdin exceeds 1048576 UTF-8 bytes",
                  file=sys.stderr)
            return 2
        try:
            values = json.loads(raw)
        except ValueError:
            values = None
        body, body_err = _json_contract(values, "reconcile envelope")
        if body_err or not isinstance(values, dict) \
                or set(values) != {"request", "handle", "observed", "fetched"} \
                or raw != body:
            print("gate fab reconcile: stdin is not one canonical exact envelope",
                  file=sys.stderr)
            return 2
        result, err = reconcile_events(
            values["request"], values["handle"], values["observed"],
            values["fetched"],
            os.path.abspath(opts.get("--repo") or os.getcwd()))
        pending = isinstance(result, dict) \
            and result.get("verdict") == "completion-pending"
        typed = isinstance(result, dict) \
            and result.get("state") in ("UNKNOWN", "SUPERSEDED")
        if typed:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
        if isinstance(err, gate.CapacityRefusal):
            print("gate fab reconcile: %s" % err, file=sys.stderr)
            return gate.EXIT_NOT_RUN_CAPACITY
        if err and not pending and not typed \
                and (not isinstance(result, dict) or not result.get("receipt")):
            print("gate fab reconcile: REFUSED — %s" % err, file=sys.stderr)
            return 1
        if result is not None and not typed:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
        if err:
            print("gate fab reconcile: WARNING — %s" % err, file=sys.stderr)
            return 1
        return 0
    if sub == "detached":
        opts, err = _options(rest, {
            "--repo", "--host", "--job", "--key", "--generation",
            "--state", "--reason"})
        if err:
            print("gate fab detached: %s" % err, file=sys.stderr)
            return 2
        missing = [name for name in (
            "--host", "--job", "--key", "--generation", "--state")
            if name not in opts]
        if missing:
            print("gate fab detached: %s required" % ", ".join(missing),
                  file=sys.stderr)
            return 2
        try:
            observed = json.loads(opts["--state"])
        except ValueError:
            observed = None                 # unreadable means UNKNOWN, never refusal
        handle, err = _handle(
            {"v": HANDLE_VERSION, "key": opts["--key"],
             "job_id": opts["--job"], "host": opts["--host"],
             "generation": opts["--generation"]}, opts["--key"])
        if err:
            print("gate fab detached: REFUSED — %s" % err, file=sys.stderr)
            return 2
        print(detach_text(handle,
                          os.path.abspath(opts.get("--repo") or os.getcwd()),
                          observed, opts.get("--reason") or "local wrapper lost"))
        return 0
    print("usage: helm " + USAGE, file=sys.stderr)
    return 2
