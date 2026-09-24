"""Reader-first v9 contract, with nonempty duplicate-preserving evidence."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import fabgate, gate, gateauthority, gateequiv, gateimport, gateshard
from helm import gatetestrecord


def _sample(phase, pids):
    return {"phase": phase, "workers": pids, "load": [0.0, 0.0, 0.0],
            "affinity_cores": 4, "host_cores": 4,
            "memory": {"MemAvailable": 1024, "MemFree": 1024},
            "competing_suites": []}


def _bundle():
    token = "a" * 32
    ids = {"tests.test_one": ["tests.test_one.C.test_a"] * 2
           + ["tests.test_one.C.test_b"],
           "tests.test_two": ["tests.test_two.C.test_c"]}
    counts = {name: len(tests) for name, tests in ids.items()}
    workers, supervisors, assignments, files = [], [], [], {}

    def envelope(row):
        name = gatetestrecord.artifact_basename(row)
        raw = json.dumps(row, sort_keys=True).encode("utf-8")
        files[name] = {"basename": name, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                       "row": row}
        return files[name]["raw_sha256"]

    for index, (module, tests) in enumerate(ids.items(), 1):
        supervisor, inner = 100 + index * 2, 101 + index * 2
        result = {name: 0 for name in gatetestrecord.COUNT_FIELDS}
        result.update(ran=len(tests), ok=True)
        worker = {"v": 1, "token": token, "role": "worker", "pid": inner,
                  "start": 10, "root_pid": supervisor, "root_start": 10,
                  "shard": index, "modules": [module], "planned": list(tests),
                  "started": list(tests), "stopped": list(tests), "unexecuted": [],
                  "events": [{"test": test, "kind": kind} for test in tests
                             for kind in ("start", "ok", "stop")], "counts": result}
        digest = envelope(worker)
        chain = {"v": 1, "type": "worker-supervisor", "token": token,
                 "shard": index, "modules": [module], "pid": supervisor,
                 "start": 10, "root_pid": 20, "root_start": 10,
                 "inner_pid": inner, "inner_start": 10,
                 "inner_artifact_digest": digest, "inner_rc": 0, "sweep_ok": True}
        envelope(chain)
        workers.append(worker)
        supervisors.append(chain)
        assignments.append({"index": index, "modules": [module],
                            "pid": supervisor, "start": 10})
    totals = {name: sum(row["counts"][name] for row in workers)
              for name in gatetestrecord.COUNT_FIELDS}
    totals["ok"] = True
    pids = [row["pid"] for row in supervisors]
    root = {"v": 2, "type": "sharded-root", "token": token, "pid": 20,
            "start": 10, "owner_pid": 10, "owner_start": 10,
            "bins": [[name] for name in ids], "assignments": assignments,
            "samples": [_sample(phase, pids) for phase in ("launch", "steady", "peak")],
            "counts": totals, "rc": 0,
            "plan": {"modules": sorted(ids), "planned": sum(counts.values()),
                     "digest": gateshard.plan_digest(counts, ids)}}
    envelope(root)
    arm = {"token": token, "owner": {"pid": 10, "start": 10},
           "artifact_files": files, "artifacts": [root] + workers + supervisors,
           "receipt": {"ran": 4, "skipped": 0, "status": "OK"}}
    witness = {"v": 1, "token": token, "owner": dict(arm["owner"]),
               "supervisor": {"pid": 30, "start": 10},
               "process": {"pid": 31, "start": 10}, "counts": counts,
               "ids": ids, "rc": 0, "containment": "subreaper",
               "sweep": "empty-after-exit"}
    return arm, witness


def _receipt():
    arm, witness = _bundle()
    evidence = gateauthority._validated_sharded_evidence(arm)
    confirmation = gateauthority.validate_rediscovery(evidence, witness)
    raw = b"# immutable runner fixture\n"
    blob = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()
    executable = os.path.realpath(os.sys.executable)
    argv = [executable, gateauthority.RUNNER_PATH]
    authority = {
        "v": 1, "kind": "whole-suite-sharded",
        "bundle_digest": gateauthority.canonical_digest(arm["artifact_files"]),
        "artifact_census": {"count": len(arm["artifact_files"]),
                            "digest": gateauthority.canonical_digest(sorted(arm["artifact_files"]))},
        "plan": gateauthority.plan_facts(witness["counts"], witness["ids"]),
        "rediscovery": confirmation,
        "assignments_digest": gateauthority.canonical_digest(evidence["root"]["assignments"]),
        "process_chain_digest": gateauthority.canonical_digest(evidence["supervisors"]),
        "owner": dict(arm["owner"]), "root": {"pid": 20, "start": 10},
        "workers": 2, "peak_workers": 2,
        "containment": {"method": "subreaper", "supervisors": 2, "swept": 2},
        "runner": {"path": gateauthority.RUNNER_PATH, "checkout_root": "/remote/checkout",
                   "blob": blob, "sha256": hashlib.sha256(raw).hexdigest(),
                   "tree": "b" * 40, "argv": argv},
    }
    row = {"v": 9, "event": "gate", "ts": "2026-09-10T00:00:00Z",
           "repo_id": "/remote/repository", "head": "a" * 40, "tree": "b" * 40,
           "dirty": False, "head_after": "a" * 40, "tree_after": "b" * 40,
           "dirty_after": False, "interpreter": dict(gate.interpreter(), executable=executable),
           "host": {"node": "builder", "system": "Linux", "release": "test", "id": "c" * 16},
           "argv": argv, "suite": True, "label": None, "rc": 0, "wall": 1.0,
           "status": "OK", "ran": 4, "skipped": 0, "detail": "", "elapsed": 0.1,
           "failures": [], "failures_unreadable": False, "failure_total": 0,
           "failure_diagnostics_omitted": 0, "failure_chunks": [], "base_check": None,
           "sharded_authority": authority}
    row["id"] = gate._receipt_id(row)
    return row, raw


class EvidenceConstructorTest(unittest.TestCase):
    def test_both_readers_use_one_constructor(self):
        self.assertIs(gateequiv._validated_sharded_evidence,
                      gateauthority._validated_sharded_evidence)
        arm, witness = _bundle()
        evidence = gateauthority._validated_sharded_evidence(arm)
        self.assertEqual(2, evidence["count"])
        proof = gateauthority.validate_rediscovery(evidence, witness)
        self.assertEqual(4, proof["plan"]["planned"])
        self.assertEqual(3, len(evidence["workers"][0]["planned"]))

    def test_missing_swapped_foreign_packed_and_drifted_rows_refuse(self):  # noqa: VACUOUS_ASSERTION — _bundle validates first and every named mutation must make the same constructor raise
        for defect in ("missing", "swapped", "generation", "packed", "digest",
                       "sweep", "plan-count", "assignment-bool", "root-rc-bool"):
            with self.subTest(defect=defect):
                arm, _witness = _bundle()
                rows = arm["artifacts"]
                root = rows[0]
                workers = [row for row in rows if row.get("role") == "worker"]
                chains = [row for row in rows if row.get("type") == "worker-supervisor"]
                if defect == "missing":
                    del arm["artifact_files"][gatetestrecord.artifact_basename(workers[0])]
                elif defect == "swapped":
                    names = [gatetestrecord.artifact_basename(row) for row in workers]
                    left, right = (arm["artifact_files"][name] for name in names)
                    left["row"], right["row"] = right["row"], left["row"]
                elif defect == "generation":
                    workers[0]["root_start"] += 1
                elif defect == "packed":
                    root["bins"][0].append(root["bins"][1][0])
                elif defect == "digest":
                    chains[0]["inner_artifact_digest"] = "f" * 64
                elif defect == "sweep":
                    chains[0]["sweep_ok"] = False
                elif defect == "plan-count":
                    root["plan"]["planned"] -= 1
                elif defect == "assignment-bool":
                    root["assignments"][0]["index"] = True
                else:
                    root["rc"] = False
                with self.assertRaises(ValueError):
                    gateauthority._validated_sharded_evidence(arm)

    def test_coherently_packed_chain_is_not_singleton_authority(self):  # noqa: VACUOUS_ASSERTION — the rebuilt coherent bundle is nonempty and must reach the singleton-process refusal
        arm, witness = _bundle()
        root = arm["artifacts"][0]
        worker = next(row for row in arm["artifacts"] if row.get("role") == "worker")
        chain = next(row for row in arm["artifacts"] if row.get("type") == "worker-supervisor")
        modules = sorted(witness["ids"])
        tests = [test for name in modules for test in witness["ids"][name]]
        worker.update(modules=modules, planned=tests, started=list(tests), stopped=list(tests),
                      events=[{"test": test, "kind": kind} for test in tests
                              for kind in ("start", "ok", "stop")])
        worker["counts"]["ran"] = len(tests)
        chain["modules"] = modules
        root["bins"] = [modules]
        root["assignments"] = [dict(root["assignments"][0], modules=modules)]
        root["samples"] = [_sample(phase, [chain["pid"]])
                           for phase in ("launch", "steady", "peak")]
        root["plan"]["digest"] = gateshard.plan_digest(
            {name: len(tests) for name in modules}, {name: tests for name in modules})
        chain["inner_artifact_digest"] = hashlib.sha256(
            json.dumps(worker, sort_keys=True).encode()).hexdigest()
        arm["artifacts"] = [worker, chain, root]
        arm["artifact_files"] = {}
        for row in arm["artifacts"]:
            name = gatetestrecord.artifact_basename(row)
            digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            arm["artifact_files"][name] = {"basename": name, "raw_sha256": digest, "row": row}
        with self.assertRaisesRegex(ValueError, "one module per process"):
            gateauthority._validated_sharded_evidence(arm)

    def test_rediscovery_preserves_duplicates_order_and_complete_census(self):  # noqa: VACUOUS_ASSERTION — each iteration first constructs fully validated nonempty evidence before mutating one witness axis
        for defect in ("deduplicate", "reorder", "same-count-different-id", "drop-module",
                       "owner", "reused-process", "containment", "sweep", "exit", "missing"):
            with self.subTest(defect=defect):
                arm, witness = _bundle()
                evidence = gateauthority._validated_sharded_evidence(arm)
                if defect == "deduplicate":
                    witness["ids"]["tests.test_one"].pop(0)
                    witness["counts"]["tests.test_one"] -= 1
                elif defect == "reorder":
                    witness["ids"]["tests.test_one"].reverse()
                elif defect == "same-count-different-id":
                    witness["ids"]["tests.test_one"][0] += "_foreign"
                elif defect == "drop-module":
                    del witness["ids"]["tests.test_two"]
                    del witness["counts"]["tests.test_two"]
                elif defect == "owner":
                    witness["owner"]["start"] += 1
                elif defect == "reused-process":
                    witness["process"] = dict(witness["supervisor"])
                elif defect in ("containment", "sweep"):
                    witness[defect] = "unknown"
                elif defect == "exit":
                    witness["rc"] = False
                else:
                    del witness["ids"]
                with self.assertRaises(ValueError):
                    gateauthority.validate_rediscovery(evidence, witness)

    def test_duplicate_and_order_changes_alter_both_plan_digests(self):  # noqa: VACUOUS_ASSERTION — the baseline plan is nonempty and every changed sequence is compared through both digest producers
        ids = {"tests.test_one": ["a", "a", "b"]}
        full = gateauthority.plan_facts({"tests.test_one": 3}, ids)
        for changed in (["a", "b"], ["b", "a", "a"]):
            other = gateauthority.plan_facts({"tests.test_one": len(changed)},
                                             {"tests.test_one": changed})
            self.assertNotEqual(full["digest"], other["digest"])
            self.assertNotEqual(full["ids_digest"], other["ids_digest"])


class ReceiptReaderTest(unittest.TestCase):
    def test_complete_v9_is_readable_importable_and_bindable(self):
        row, _raw = _receipt()
        self.assertIn(9, gate.RECEIPT_VERSIONS)
        self.assertIn(9, gateimport.KNOWN_VERSIONS)
        self.assertNotIn(9, gate.MINTED_RECEIPT_VERSIONS)
        self.assertIsNone(gateauthority.receipt_refusal(row))
        self.assertIsNone(gateimport._schema_err(row))
        self.assertIsNone(gate.row_refusal(row))
        self.assertTrue(gate._id_matches(row, {}))
        chunks, _timing, err = gateimport._artifact_object_err(row, [row])
        self.assertEqual({}, chunks)
        self.assertIsNone(err)
        with mock.patch.object(gate, "by_id", return_value=(row, None)), \
                mock.patch.object(gate, "_repository_authority_refusal", return_value=None):
            self.assertEqual("VERIFIED", gate.bind("gate:" + row["id"], row["head"])[0])

    def test_historical_serial_receipt_grammar_is_immutable(self):
        """Existing receipt identity is data, not active writer configuration.

        A different runner takes a new receipt version; changing either tuple
        would reinterpret already-minted authority under a new grammar.
        """
        self.assertEqual(gate.HISTORICAL_SERIAL_VERSIONS, (4, 8))
        self.assertEqual(
            gateauthority.SERIAL_ARGV,
            ("-m", "unittest", "discover", "-s", "tests", "-t", "."))
        self.assertEqual(gateauthority.SUITE_ARGV, ("helm/gateshard.py",))

    def test_v9_failure_record_accepts_inline_and_preserves_overflow_rules(self):
        row, _raw = _receipt()
        failure = {"kind": "ERROR", "test": "tests.test_one.C.test_a",
                   "traceback": "RuntimeError: test failed"}
        row.update(status="FAILED", rc=1, failures=[failure],
                   failure_total=1, failure_diagnostics_omitted=0)
        self.assertIsNone(gateauthority.receipt_refusal(row))
        self.assertIsNone(gate._failure_record_error(row, {}))
        record, chunks = gate._failure_record([dict(failure) for _ in range(21)])
        row.update(record)
        chunk_map = {chunk["id"]: chunk for chunk in chunks}
        self.assertEqual(21, row["failure_total"])
        self.assertIsNone(gate._failure_record_error(row, chunk_map))
        self.assertIn("absent", gate._failure_record_error(row, {}))
        inline, _raw = _receipt()
        inline["failure_chunks"] = ["foreign"]
        self.assertIn("overflow", gate._failure_record_error(inline, {}))
        for field in ("failure_total", "failures"):
            incomplete, _raw = _receipt()
            del incomplete[field]
            refusal = gateauthority.receipt_refusal(incomplete)
            self.assertIn("failure record is incomplete", refusal)
            self.assertNotIn("carries diagnostics", refusal)
        contradictory, _raw = _receipt()
        contradictory.update(failures=[failure], failure_total=1)
        contradictory["id"] = gate._receipt_id(contradictory)
        self.assertIn("failure diagnostics",
                      gateauthority.receipt_refusal(contradictory))
        self.assertIsNotNone(gate.row_refusal(contradictory))
        self.assertFalse(gate._id_matches(contradictory, {}))
        with mock.patch.object(gate, "by_id",
                               return_value=(contradictory, None)):
            self.assertEqual("REFUSED", gate.bind(
                "gate:" + contradictory["id"], contradictory["head"])[0])

    def test_v9_requires_every_planned_test_and_bounded_skip_count(self):  # noqa: VACUOUS_ASSERTION — the valid complete receipt is asserted before the mutation matrix exercises each count boundary
        row, _raw = _receipt()
        planned = row["sharded_authority"]["plan"]["planned"]
        self.assertEqual(4, planned)
        self.assertIsNone(gateauthority.receipt_refusal(row))
        all_skipped = dict(row, skipped=planned)
        all_skipped["id"] = gate._receipt_id(all_skipped)
        self.assertIsNone(gateauthority.receipt_refusal(all_skipped))
        for status, rc in (("OK", 0), ("FAILED", 1)):
            for ran, skipped in ((0, 0), (planned - 1, 0), (planned + 1, 0),
                                 (planned, planned + 1), (planned, -1),
                                 (True, 0), (planned, True)):
                with self.subTest(status=status, ran=ran, skipped=skipped):
                    changed = dict(row, status=status, rc=rc, ran=ran, skipped=skipped)
                    changed["id"] = gate._receipt_id(changed)
                    self.assertIn("counts", gateauthority.receipt_refusal(changed))
                    self.assertIsNotNone(gateimport._schema_err(changed))
                    self.assertFalse(gate._id_matches(changed, {}))
                    with mock.patch.object(gate, "by_id", return_value=(changed, None)):
                        self.assertEqual("REFUSED", gate.bind(
                            "gate:" + changed["id"], changed["head"])[0])

    def test_rehashed_legacy_whole_suite_requires_frozen_serial_argv(self):  # noqa: VACUOUS_ASSERTION — each receipt first passes the frozen serial reader before rehashed nonserial variants are refused
        for version in (4, 8):
            with self.subTest(version=version):
                row, _raw = _receipt()
                row["v"] = version
                del row["sharded_authority"]
                if version == 4:
                    for key in ("failure_total", "failure_diagnostics_omitted", "failure_chunks"):
                        del row[key]
                else:
                    failure = {"kind": "ERROR", "test": "tests.test_one.C.test_a",
                               "traceback": "RuntimeError: recorded failure"}
                    record, _chunks = gate._failure_record([dict(failure) for _ in range(21)])
                    row.update(record, status="FAILED", rc=1)
                executable = row["interpreter"]["executable"]
                serial = [executable] + list(gateauthority.SERIAL_ARGV)
                row["argv"] = serial
                row["id"] = gate._receipt_id(row)
                self.assertIsNone(gate.row_refusal(row))
                with mock.patch.object(gate, "by_id", return_value=(row, None)), \
                        mock.patch.object(gate, "_repository_authority_refusal", return_value=None), \
                        mock.patch.object(gate, "SUITE", gateauthority.SUITE_ARGV):
                    state, _bound, why = gate.bind("gate:" + row["id"], row["head"])
                    self.assertEqual("VERIFIED" if version == 4 else "REFUSED", state)
                    if version == 8:
                        self.assertIn("not OK", why)
                candidates = ([executable, "helm/gateshard.py"],
                              [executable, "-c", "print('OK')"],
                              [executable, "-m", "unittest", "tests.test_one"],
                              serial + ["-k", "test_a"],
                              ["/foreign/python"] + list(gateauthority.SERIAL_ARGV))
                for argv in candidates:
                    with self.subTest(argv=argv):
                        changed = dict(row, argv=argv, status="OK", rc=0)
                        changed["id"] = gate._receipt_id(changed)
                        self.assertEqual(changed["id"], gate._receipt_id(changed))
                        self.assertIn("historical serial", gate.row_refusal(changed))
                        with mock.patch.object(gate, "by_id", return_value=(changed, None)):
                            state, _bound, why = gate.bind("gate:" + changed["id"], changed["head"])
                        self.assertEqual("REFUSED", state)
                        self.assertIn("historical serial", why)

    def test_valid_v9_import_reaches_existing_durability_boundary(self):
        row, _raw = _receipt()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "receipt.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
            with mock.patch.object(gateimport, "_repo_identity", return_value=tmp), \
                    mock.patch.object(gateimport, "placement_err", return_value=None) as placement, \
                    mock.patch.object(gateimport, "_ensure_receipt", return_value=("appended", None)) as durable, \
                    mock.patch.object(gateimport, "_record_binding",
                                      return_value=({"importing_repo": tmp}, "new", None)), \
                    mock.patch.object(gateimport, "_record_audit", return_value=("appended", None)):
                imported, state, err = gateimport.import_receipt(path, tmp, actor="test")
            self.assertEqual(row["id"], imported["id"])
            self.assertEqual("imported", state)
            self.assertIsNone(err)
            placement.assert_called_once_with(row, tmp)
            durable.assert_called_once()
            self.assertEqual(row, durable.call_args.args[0])

    def test_each_attested_fact_changes_receipt_identity(self):  # noqa: VACUOUS_ASSERTION — more than thirty concrete leaves are required before every mutation is compared with the baseline id
        row, _raw = _receipt()
        original = row["id"]

        def leaves(value, prefix=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from leaves(child, prefix + (key,))
            elif isinstance(value, list):
                for key, child in enumerate(value):
                    yield from leaves(child, prefix + (key,))
            else:
                yield prefix

        paths = list(leaves(row["sharded_authority"]))
        self.assertGreater(len(paths), 30)
        for path in paths:
            with self.subTest(path=path):
                changed = copy.deepcopy(row)
                target = changed["sharded_authority"]
                for key in path[:-1]:
                    target = target[key]
                old = target[path[-1]]
                target[path[-1]] = old + 1 if type(old) is int else str(old) + "changed"
                self.assertNotEqual(original, gate._receipt_id(changed))

    def test_malformed_attestations_refuse_even_after_rehash(self):  # noqa: VACUOUS_ASSERTION — every mutation is rehashed before three independent reader doors must refuse it
        row, _raw = _receipt()
        mutations = [
            lambda a: a.pop("plan"),
            lambda a: a.update(extra=True),
            lambda a: a.update(v=True),
            lambda a: a.update(workers=True),
            lambda a: a.update(peak_workers=3),
            lambda a: a["artifact_census"].update(count=4),
            lambda a: a["rediscovery"]["plan"].update(planned=3),
            lambda a: a["rediscovery"].update(sweep="unknown"),
            lambda a: a["root"].update(a["owner"]),
            lambda a: a["containment"].update(swept=1),
            lambda a: a["runner"].update(tree="d" * 40),
            lambda a: a["runner"].update(path="../helm/gateshard.py"),
            lambda a: a["runner"].update(blob=True),
            lambda a: a["runner"].update(sha256="UNKNOWN"),
            lambda a: a["runner"].update(checkout_root="/remote/../checkout"),
            lambda a: a["runner"].update(argv=["/foreign/python", "helm/gateshard.py"]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(row)
                mutate(changed["sharded_authority"])
                changed["id"] = gate._receipt_id(changed)
                self.assertIsNotNone(gateimport._schema_err(changed))
                self.assertIsNotNone(gate.row_refusal(changed))
                self.assertFalse(gate._id_matches(changed, {}))

    def test_unknown_and_serial_cannot_wear_v9_authority(self):  # noqa: VACUOUS_ASSERTION — each named defect mutates a complete valid v9 receipt before the shared reader must refuse
        for defect in ("unknown", "serial", "interpreter", "custom", "missing"):
            with self.subTest(defect=defect):
                row, _raw = _receipt()
                if defect == "unknown":
                    row.update(status="UNKNOWN", rc=None)
                elif defect == "serial":
                    row["argv"] = [row["interpreter"]["executable"]] + list(gate.SUITE)
                elif defect == "interpreter":
                    row["interpreter"]["executable"] = "/foreign/python"
                elif defect == "custom":
                    row["suite"] = False
                else:
                    row["sharded_authority"] = None
                self.assertIsNotNone(gateauthority.receipt_refusal(row))

    def test_v7_stays_burned_and_v8_cannot_carry_v9_fields(self):  # noqa: VACUOUS_ASSERTION — both malformed rows use a complete v9 authority object and the version registries are asserted directly
        for version in (7, 8):
            row, _raw = _receipt()
            row["v"] = version
            self.assertIsNotNone(gateimport._schema_err(row))
        self.assertNotIn(7, gate.RECEIPT_VERSIONS)
        self.assertEqual((4, 8), gate.MINTED_RECEIPT_VERSIONS)
        self.assertEqual(("-m", "unittest", "discover", "-s", "tests", "-t", "."), gate.SUITE)

    def test_import_rejects_edited_digest_before_durable_write(self):  # noqa: VACUOUS_ASSERTION — a complete receipt is changed and the durable writer mock proves rejection occurs before persistence
        row, _raw = _receipt()
        row["sharded_authority"]["bundle_digest"] = "e" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "receipt.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
            with mock.patch.object(gateimport, "_ensure_receipt") as write:
                _row, _state, err = gateimport.import_receipt(path, tmp)
            self.assertIn("content id mismatch", err)
            write.assert_not_called()


class ImmutableRunnerTest(unittest.TestCase):
    def test_import_reads_remote_runner_from_immutable_tree_not_worktree(self):  # noqa: VACUOUS_ASSERTION — the mocked immutable-tree calls and exact arguments positively prove which byte source was read
        row, raw = _receipt()
        backend = mock.Mock()
        runner = row["sharded_authority"]["runner"]
        backend.text.side_effect = [(0, row["tree"], ""), (0, runner["blob"], "")]
        entry = ("100644 blob %s\thelm/gateshard.py\0" % runner["blob"]).encode()
        backend.run.side_effect = [(0, entry, b""), (0, raw, b"")]
        with mock.patch.object(gateauthority.vcs, "backend", return_value=backend):
            self.assertIsNone(gateimport.placement_err(row, "/local/other-checkout"))
        self.assertEqual(("/local/other-checkout", "rev-parse", row["tree"] + ":helm/gateshard.py"),
                         backend.text.call_args_list[-1].args)
        self.assertEqual(("/local/other-checkout", "cat-file", "blob", runner["blob"]),
                         backend.run.call_args.args)

    def test_wrong_tree_blob_bytes_and_sha256_refuse(self):  # noqa: VACUOUS_ASSERTION — every case begins with a complete runner receipt and supplies concrete immutable-tree backend results
        for defect in ("blob", "bytes", "sha256", "missing", "symlink"):
            with self.subTest(defect=defect):
                row, raw = _receipt()
                runner = row["sharded_authority"]["runner"]
                backend = mock.Mock()
                backend.text.return_value = (0, runner["blob"], "")
                entry = ("100644 blob %s\thelm/gateshard.py\0" % runner["blob"]).encode()
                result = (0, raw, b"")
                if defect == "blob":
                    backend.text.return_value = (0, "e" * 40, "")
                elif defect == "bytes":
                    result = (0, raw + b"drift", b"")
                elif defect == "sha256":
                    runner["sha256"] = "e" * 64
                elif defect == "symlink":
                    entry = entry.replace(b"100644", b"120000")
                else:
                    result = (1, b"", b"missing")
                backend.run.side_effect = [(0, entry, b""), result]
                with mock.patch.object(gateauthority.vcs, "backend", return_value=backend):
                    self.assertIsNotNone(gateauthority.runner_tree_refusal(row, "/local/repo"))

    def test_fab_scope_is_version_specific_and_writer_default_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — both receipt eras are checked against opposite scopes and the active writer scope is asserted directly
        row, _raw = _receipt()
        authority = {"sha": row["head"], "host": "builder", "exit": 0,
                     "identity": {"tree": row["tree"], "interpreter": row["interpreter"],
                                  "scope": {"kind": "whole", "argv": ["helm/gateshard.py"]}}}
        self.assertIsNone(gateimport._fab_receipt_err(row, authority))
        authority["identity"]["scope"] = fabgate.whole_scope()
        self.assertIn("receipt version", gateimport._fab_receipt_err(row, authority))
        row["v"] = 8
        authority["identity"]["scope"] = {"kind": "whole", "argv": ["helm/gateshard.py"]}
        self.assertIn("receipt version", gateimport._fab_receipt_err(row, authority))
        self.assertEqual(list(gate.SUITE), fabgate.whole_scope()["argv"])
