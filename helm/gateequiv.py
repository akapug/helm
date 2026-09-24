#!/usr/bin/env python3
"""Observe serial authority against diagnostic fresh-process sharding."""
import argparse
import collections
import contextlib
import glob
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid

from . import gate, gateshard, gatetestrecord
from .gateauthority import (
    _root_artifact, _sample_reason, _validated_sharded_evidence,
)
from . import pk


SERIAL_SUITE = ("-m", "unittest", "discover", "-s", "tests", "-t", ".")
SHARDED_SUITE = (os.path.abspath(gateshard.__file__),)
OBSERVATIONAL_ONLY = (
    "Diagnostic only: an equivalent result means no outcome divergence was "
    "observed in these runs. It never qualifies, caches, or authorizes a "
    "sharded receipt; literal same-process serial discovery remains authority."
)
_ARM_ENV_KEYS = gatetestrecord.ENV_KEYS + ("HELM_GATESHARD_VERBOSE",)
TIMING_DISCLOSURE = (
    "Outcome arms are instrumented and excluded from fleet-tax timing because "
    "sharded workers pay multiple atomic fsync artifact writes. Timing uses "
    "unchanged gate paths with all outcome-recording environment removed, equal "
    "serial/sharded populations in alternating S,H then H,S pairs, and per-arm "
    "load, RAM, core, and competing-suite samples. The receipt wall is the comparison "
    "and driver wall is diagnostic only; overlap or sign changes are inconclusive."
)


def sample():
    return gatetestrecord.system_sample()


@contextlib.contextmanager
def _without_measurement_env():
    previous = {name: os.environ.get(name) for name in _ARM_ENV_KEYS}
    for name in _ARM_ENV_KEYS:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name in _ARM_ENV_KEYS:
            os.environ.pop(name, None)
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value


@contextlib.contextmanager
def _measurement_env(directory, token, role, owner):
    values = {
        "HELM_GATE_RECORD_DIR": directory,
        "HELM_GATE_RECORD_TOKEN": token,
        "HELM_GATE_RECORD_ROLE": role,
        "HELM_GATE_RECORD_ROOT_PID": str(owner["pid"]),
        "HELM_GATE_RECORD_ROOT_START": str(owner["start"]),
    }
    previous = {name: os.environ.get(name) for name in _ARM_ENV_KEYS}
    for name in _ARM_ENV_KEYS:
        os.environ.pop(name, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for name in _ARM_ENV_KEYS:
            os.environ.pop(name, None)
        for name, value in previous.items():
            if value is not None:
                os.environ[name] = value


def _receipt_reason(row, head, tree, argv, host=None, authoritative=True):
    if type(row) is not dict:
        return "gate did not return a receipt"
    if row.get("head") != head or row.get("head_after") != head \
            or row.get("tree") != tree or row.get("tree_after") != tree:
        return "receipt tree bracket moved"
    if row.get("dirty") is not False or row.get("dirty_after") is not False:
        return "receipt tree was dirty"
    if row.get("suite") is not authoritative or row.get("argv") != argv:
        return ("serial arm does not name the authoritative suite" if authoritative
                else "sharded arm is not a custom nonbinding diagnostic")
    if row.get("status") not in ("OK", "FAILED"):
        return "receipt outcome is incomplete"
    if host is not None and row.get("host") != host:
        return "measurement arms ran on different hosts"
    return None


def _run_arm(repo, suite, role, head, tree, timeout, host=None):
    token = uuid.uuid4().hex
    owner = gatetestrecord.process_identity()
    before = sample()
    driver_started = time.monotonic()
    argv = [sys.executable] + list(suite)
    with tempfile.TemporaryDirectory(prefix="helm-gate-equivalence-") as directory:
        with _measurement_env(directory, token, role, owner):
            if role == "serial" and tuple(suite) == SERIAL_SUITE:
                row, err = gate.run(repo=repo, timeout=timeout)
            elif role == "sharded" and tuple(suite) == SHARDED_SUITE:
                row, err = gate.run(
                    repo=repo, argv=argv,
                    label="DIAGNOSTIC sharded outcome observation",
                    timeout=timeout)
            else:
                row, err = None, "measurement arm is not canonical"
        driver_wall = time.monotonic() - driver_started
        after = sample()
        reason = err or _receipt_reason(
            row, head, tree, argv, host=host,
            authoritative=role == "serial") \
            or _sample_reason(before) or _sample_reason(after)
        paths = sorted(glob.glob(os.path.join(directory, "*.json")))
        # PRESERVE THE BYTES' DIGEST WHILE THE DIRECTORY STILL EXISTS.
        # The supervisor witness claims a SHA-256 of the exact worker file,
        # and a later binder cannot verify that from a parsed row: json.dumps
        # is not canonical, so re-serializing produces different bytes and a
        # different hash for identical content. This loop is the ONLY moment
        # those bytes exist -- the TemporaryDirectory is destroyed straight
        # after -- so the digest is taken here or it is lost permanently.
        #
        # `artifacts` keeps its shape (a list of rows) because every existing
        # reader indexes it that way; the byte evidence rides alongside in
        # `artifact_files`, keyed by basename, which is what the binder needs
        # to derive the exact worker filename from token/shard/inner pid.
        # ONE COLLECTION IS THE SOURCE OF TRUTH. Built as two independent
        # structures, `artifacts` and `artifact_files` let a reader trust one
        # while the other disagreed. `artifacts` is DERIVED from the
        # envelopes below, never accumulated in parallel, so there is exactly
        # one place a row can come from.
        artifact_files = {}
        for path in paths:
            try:
                with pk.open_regular(path, "rb") as fh:
                    raw = fh.read()
                # NOT `row`: that name already holds the GATE RECEIPT in this
                # scope, and reusing it here silently replaced the receipt
                # with the last artifact read. A loop variable that shadows an
                # outer binding is a rename away from a defect nothing tests.
                artifact_row = json.loads(raw.decode("utf-8"))
                artifact_files[os.path.basename(path)] = {
                    "basename": os.path.basename(path),
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "row": artifact_row,
                }
            except (OSError, ValueError, TypeError) as exc:
                reason = reason or "measurement artifact is unreadable: %s" % exc
        artifacts = [envelope["row"]
                     for _name, envelope in sorted(artifact_files.items())]
        return {
            "kind": role,
            "token": token,
            "artifact_files": artifact_files,
            "owner": owner,
            "receipt": row,
            "reason": reason,
            "artifacts": artifacts,
            "driver_wall": round(driver_wall, 3),
            "receipt_wall": row.get("wall") if isinstance(row, dict) else None,
            "before": before,
            "after": after,
            "instrumented": True,
            "fab_run_id": os.environ.get("FAB_ID"),
        }


def _run_timing_arm(repo, suite, kind, head, tree, timeout, host):
    before = sample()
    driver_started = time.monotonic()
    argv = [sys.executable] + list(suite)
    with _without_measurement_env():
        if kind == "serial" and tuple(suite) == SERIAL_SUITE:
            row, err = gate.run(repo=repo, timeout=timeout)
        elif kind == "sharded" and tuple(suite) == SHARDED_SUITE:
            row, err = gate.run(
                repo=repo, argv=argv,
                label="DIAGNOSTIC sharded timing observation",
                timeout=timeout)
        else:
            row, err = None, "timing arm is not canonical"
    driver_wall = time.monotonic() - driver_started
    after = sample()
    reason = err or _receipt_reason(
        row, head, tree, argv, host=host, authoritative=kind == "serial") \
        or _sample_reason(before) or _sample_reason(after)
    return {
        "kind": kind,
        "receipt": row,
        "reason": reason,
        "driver_wall": round(driver_wall, 3),
        "receipt_wall": row.get("wall") if isinstance(row, dict) else None,
        "before": before,
        "after": after,
        "instrumented": False,
        "fab_run_id": os.environ.get("FAB_ID"),
    }


def _serial_artifact(arm):
    if arm["reason"]:
        raise ValueError(arm["reason"])
    if len(arm["artifacts"]) != 1:
        raise ValueError("serial arm did not publish exactly one artifact")
    row = gatetestrecord.validate_artifact(
        arm["artifacts"][0], arm["token"])
    if row["role"] != "serial" or row["shard"] is not None \
            or row["modules"] or row["root_pid"] != arm["owner"]["pid"] \
            or row["root_start"] != arm["owner"]["start"]:
        raise ValueError("serial artifact ownership is invalid")
    receipt = arm["receipt"]
    if receipt["ran"] != row["counts"]["ran"] \
            or receipt["skipped"] != row["counts"]["skipped"] \
            or (receipt["status"] == "OK") != row["counts"]["ok"]:
        raise ValueError("serial artifact disagrees with its receipt")
    return row


def _sharded_artifacts(arm):
    if arm["reason"]:
        raise ValueError(arm["reason"])
    # ONE CONSTRUCTOR, AND IT IS THE WHOLE VALIDATION. Envelope identity,
    # typed census, shard ownership, throughput, the three-hop binder and
    # the plan binding all happen inside it, so this caller cannot obtain a
    # partially-checked object no matter what it forgets to do next.
    evidence = _validated_sharded_evidence(arm)
    return evidence["root"], evidence["workers"]


def _outcomes(rows):
    planned = []
    unexecuted = []
    outcomes = []
    for row in rows:
        planned.extend(row["planned"])
        unexecuted.extend(row["unexecuted"])
        active = collections.defaultdict(list)
        for event in row["events"]:
            name = event["test"]
            kind = event["kind"]
            if kind == "start":
                active[name].append([])
                continue
            if kind == "stop":
                if not active[name]:
                    raise ValueError("stop event has no matching start")
                events = active[name].pop(0)
                if not events:
                    raise ValueError("test has no terminal outcome")
                outcomes.append((name, tuple(events)))
                continue
            value = tuple(sorted(event.items()))
            if active[name]:
                active[name][0].append(value)
            else:
                outcomes.append((name, (value,)))
        if any(active.values()):
            raise ValueError("started test has no matching stop")
    return (sorted(planned), sorted(unexecuted), sorted(outcomes))


def compare(serial_arm, sharded_arm):
    try:
        serial = _serial_artifact(serial_arm)
        _root, workers = _sharded_artifacts(sharded_arm)
        serial_outcomes = _outcomes([serial])
        sharded_outcomes = _outcomes(workers)
    except (KeyError, TypeError, ValueError) as exc:
        return {"equivalence_state": "unknown", "reason": str(exc)}
    if serial_outcomes != sharded_outcomes:
        return {
            "equivalence_state": "divergent",
            "reason": "serial and sharded per-test outcomes differ",
            "serial": serial_outcomes,
            "sharded": sharded_outcomes,
        }
    return {"equivalence_state": "equivalent", "reason": None}


def _same_receipt_outcome(left, right):
    return type(left) is dict and type(right) is dict and all(
        left.get(name) == right.get(name)
        for name in ("status", "ran", "skipped"))


def _timing_trials(repeats):
    rows = []
    for pair in range(1, repeats + 1):
        kinds = ("serial", "sharded") if pair % 2 else (
            "sharded", "serial")
        for kind in kinds:
            rows.append({
                "pair_id": pair,
                "block_id": (pair + 1) // 2,
                "control_id": kind,
                "sequence": len(rows) + 1,
                "kind": kind,
            })
    return rows


def _pair_resource_reason(serial, sharded):
    samples = [serial["before"], serial["after"],
               sharded["before"], sharded["after"]]
    cores = {(row["affinity_cores"], row["host_cores"]) for row in samples}
    if len(cores) != 1:
        return "core allocation changed between timing controls"
    if any(row["competing_suites"] for row in samples):
        return "competing suite work overlaps timing controls"
    affinity = samples[0]["affinity_cores"]
    left = (serial["before"]["load"][0], serial["after"]["load"][0])
    right = (sharded["before"]["load"][0], sharded["after"]["load"][0])
    if max(abs(a - b) for a, b in zip(left, right)) > max(1.0, affinity * .1):
        return "load imbalance between timing controls"
    memory = [row["memory"]["MemAvailable"] for row in samples]
    if max(memory) - min(memory) > max(1 << 30, min(memory) * .1):
        return "memory imbalance between timing controls"
    return None


def timing_comparison(arms):
    pairs = collections.defaultdict(dict)
    for arm in arms:
        pair = arm.get("pair_id")
        kind = arm.get("control_id")
        if type(pair) is not int or pair <= 0 or kind not in (
                "serial", "sharded") or kind in pairs[pair]:
            return {"state": "unknown", "reason": "timing trial ids are invalid"}
        pairs[pair][kind] = arm
    if not pairs or sorted(pairs) != list(range(1, len(pairs) + 1)) \
            or any(set(pair) != {"serial", "sharded"}
                   for pair in pairs.values()):
        return {"state": "unknown", "reason": "timing pairs are incomplete"}
    expected = _timing_trials(len(pairs))
    actual = arms
    fields = ("pair_id", "block_id", "control_id", "sequence", "kind")
    if any(tuple(arm.get(name) for name in fields) != tuple(
            trial[name] for name in fields)
            for arm, trial in zip(actual, expected)):
        return {"state": "unknown", "reason": "timing trial order is invalid"}
    differences = []
    serial_walls = []
    sharded_walls = []
    incomparable = []
    for pair_id in sorted(pairs):
        serial = pairs[pair_id]["serial"]
        sharded = pairs[pair_id]["sharded"]
        serial_wall = serial.get("receipt_wall")
        sharded_wall = sharded.get("receipt_wall")
        if not all(isinstance(value, (int, float)) and value >= 0
                   for value in (serial_wall, sharded_wall)):
            return {"state": "unknown", "reason": "receipt wall is unavailable"}
        serial_walls.append(serial_wall)
        sharded_walls.append(sharded_wall)
        differences.append(round(serial_wall - sharded_wall, 3))
        reason = _pair_resource_reason(serial, sharded)
        if reason:
            incomparable.append({"pair_id": pair_id, "reason": reason})
    overlap = min(serial_walls) <= max(sharded_walls) \
        and min(sharded_walls) <= max(serial_walls)
    signs_change = min(differences) < 0 < max(differences)
    if incomparable:
        state = "inconclusive"
        reason = "timing controls have incomparable resource conditions"
    elif signs_change or 0 in differences:
        state = "inconclusive"
        reason = "timing differences change sign"
    elif overlap:
        state = "inconclusive"
        reason = "serial and sharded ranges overlap"
    elif min(differences) > 0:
        state = "sharded-faster"
        reason = None
    else:
        state = "serial-faster"
        reason = None
    return {
        "state": state,
        "reason": reason,
        "sample_count": len(pairs),
        "serial_range": [min(serial_walls), max(serial_walls)],
        "sharded_range": [min(sharded_walls), max(sharded_walls)],
        "serial_minus_sharded": differences,
        "incomparable_pairs": incomparable,
    }


def _measurement_result(equivalence_state, reason, gate_status=None, **rows):
    return dict(rows, equivalence_state=equivalence_state,
                gate_status=gate_status, reason=reason,
                authority="serial-only", observation=OBSERVATIONAL_ONLY)


def run(repo=None, repeats=3, timeout=None, timing=True):
    repo = os.path.realpath(repo or os.getcwd())
    if type(repeats) is not int or repeats < 1:
        return _measurement_result("unknown", "repeats must be positive")
    if not os.environ.get("FAB_ID"):
        return _measurement_result("unknown", "FAB run id is unavailable")
    head, tree, dirty, err = gate.tree_state(repo)
    if err or dirty:
        return _measurement_result("unknown", err or "tree is dirty")
    serial = _run_arm(repo, SERIAL_SUITE, "serial", head, tree, timeout)
    try:
        _serial_artifact(serial)
    except (KeyError, TypeError, ValueError) as exc:
        return _measurement_result(
            "unknown", "authoritative serial reference unavailable: %s" % exc,
            head=head, tree=tree, serial=serial, sharded=[])
    evidence_status = serial["receipt"]["status"]
    host = serial["receipt"].get("host")
    arms = []
    for _index in range(repeats):
        arm = _run_arm(
            repo, SHARDED_SUITE, "sharded", head, tree, timeout, host=host)
        verdict = compare(serial, arm)
        arms.append(dict(arm, verdict=verdict))
        if verdict["equivalence_state"] != "equivalent":
            return _measurement_result(
                verdict["equivalence_state"], verdict["reason"], evidence_status,
                head=head, tree=tree, serial=serial, sharded=arms)
    trial_count = repeats if timing and evidence_status == "OK" else 1
    timing_arms = []
    for trial in _timing_trials(trial_count):
        suite = SERIAL_SUITE if trial["kind"] == "serial" else SHARDED_SUITE
        arm = _run_timing_arm(
            repo, suite, trial["kind"], head, tree, timeout, host)
        timing_arms.append(dict(arm, **trial))
        if arm["reason"]:
            break
    timing_reason = next(
        (arm["reason"] for arm in timing_arms if arm["reason"]), None)
    timing_row = {
        "arms": timing_arms,
        "comparison": (timing_comparison(timing_arms)
                       if timing and not timing_reason else None),
        "disclosure": TIMING_DISCLOSURE,
    }
    if timing_reason:
        return _measurement_result(
            "unknown", timing_reason, evidence_status,
            head=head, tree=tree, serial=serial, sharded=arms,
            timing=timing_row)
    controls = {kind: next(arm["receipt"] for arm in timing_arms
                           if arm["kind"] == kind)
                for kind in ("serial", "sharded")}
    if not _same_receipt_outcome(controls["serial"], controls["sharded"]):
        return _measurement_result(
            "divergent", "serial authority and diagnostic sharding disagree",
            None, head=head, tree=tree, serial=serial, sharded=arms,
            timing=timing_row)
    gate_status = controls["serial"]["status"]
    evidence = {
        "serial": serial["receipt"],
        "sharded": arms[0]["receipt"],
    }
    if any(not _same_receipt_outcome(
            arm["receipt"], evidence[arm["kind"]])
           for arm in timing_arms):
        return _measurement_result(
            "unknown", "instrumented evidence changed gate outcome",
            gate_status, head=head, tree=tree, serial=serial,
            sharded=arms, timing=timing_row)
    after_head, after_tree, after_dirty, after_err = gate.tree_state(repo)
    if after_err or after_dirty or after_head != head or after_tree != tree:
        return _measurement_result(
            "unknown", "tree moved during equivalence measurement", gate_status,
            head=head, tree=tree, serial=serial, sharded=arms,
            timing=timing_row)
    return _measurement_result(
        "equivalent", None, gate_status, head=head, tree=tree,
        serial=serial, sharded=arms, timing=timing_row)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=("observe literal serial gate versus diagnostic sharded "
                     "outcomes; only serial evidence is authoritative"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--no-timing", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run(args.repo, repeats=args.repeats, timeout=args.timeout,
                 timing=not args.no_timing)
    if args.output:
        path = os.path.abspath(args.output)
        gatetestrecord.write_json(
            os.path.dirname(path), os.path.basename(path).removesuffix(".json"),
            result)
    else:
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
    state = result["equivalence_state"]
    if state == "unknown":
        return 2
    return int(state == "divergent" or result["gate_status"] != "OK")


if __name__ == "__main__":
    raise SystemExit(main())
