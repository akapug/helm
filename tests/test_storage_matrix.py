import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helm import cli
from helm import storage_matrix as matrix


METRICS = {
    "seq_write_mib_s": 101.0,
    "seq_read_mib_s": 202.0,
    "random_write_iops": 303.0,
    "random_read_iops": 404.0,
    "fsync_p50_ms": 0.5,
    "fsync_p95_ms": 0.9,
    "seq_read_cache_evicted": True,
    "random_read_cache_evicted": True,
    "cache_evict_supported": True,
}


def snapshot(stamp="2026-08-04T12:00:00Z", status="complete"):
    return {
        "schema": 1,
        "measured_at": stamp,
        "duration_s": 1.25,
        "status": status,
        "method": matrix._method(),
        "inventory_error": None,
        "rows": [{
            "id": "box:local-disk", "box": "box", "box_label": "Box",
            "tier": "local-disk", "tier_label": "Local disk", "transport": "local",
            "path_label": "local storage", "order": 0, "status": "ok",
            "measured_at": stamp, "metrics": dict(METRICS),
        }, {
            "id": "box:memory", "box": "box", "box_label": "Box",
            "tier": "memory", "tier_label": "Memory", "transport": "local",
            "path_label": "/dev/shm", "order": 1, "status": "ok",
            "measured_at": stamp, "metrics": dict(METRICS),
        }],
    }


class PathAndInventoryTest(unittest.TestCase):
    def test_path_override_is_strict(self):
        with mock.patch.dict(os.environ, {"HELM_STORAGE_MATRIX": "/tmp/synthetic.json"}):
            self.assertEqual(matrix.path(), "/tmp/synthetic.json")

    def test_inventory_builds_local_active_and_unavailable_rows(self):
        inventory = {"nodes": [
            {"host": "hub", "label": "This box", "probe_mode": "local",
             "fab_hot_base": "/synthetic/local"},
            {"host": "fast", "label": "Fast box", "device_kind": "fab-node",
             "reachable": True, "ssh_host": "fast", "fab_hot_base": "/fast",
             "inventory_order": 1},
            {"host": "retired", "label": "Retired box", "device_kind": "standby",
             "reachable": False, "display_only": True, "inventory_order": 2},
        ]}
        with mock.patch.object(matrix, "_inventory", return_value=(inventory, None)), \
                mock.patch.object(matrix, "_local_disk_target",
                                  return_value=("/synthetic/local", "test disk")):
            rows, err = matrix.targets()
        self.assertIsNone(err)
        self.assertEqual([(r["box"], r["tier"]) for r in rows], [
            ("hub", "local-disk"), ("hub", "memory"),
            ("fast", "local-disk"), ("fast", "memory"),
            ("retired", "local-disk"), ("retired", "memory")])
        self.assertFalse(rows[-1]["reachable"])

    def test_host_override_rejects_shell_syntax_before_inventory_use(self):  # noqa: VACUOUS_ASSERTION — valid inventory behavior is pinned above
        for host in ("bad;host", "-V"):
            with self.subTest(host=host), \
                    mock.patch.dict(os.environ, {"HELM_STORAGE_MATRIX_HOSTS":
                                                 "good," + host}):
                with self.assertRaisesRegex(ValueError, "invalid host"):
                    matrix._requested_hosts()

    def test_local_disk_failure_keeps_memory_and_remote_targets(self):
        inventory = {"nodes": [
            {"host": "hub", "label": "This box", "probe_mode": "local"},
            {"host": "fast", "label": "Fast", "device_kind": "fab-node",
             "reachable": True, "ssh_host": "fast", "fab_hot_base": "/fast"},
        ]}
        with mock.patch.object(matrix, "_inventory", return_value=(inventory, None)), \
                mock.patch.object(matrix, "_local_disk_target",
                                  side_effect=OSError("disk unavailable")):
            rows, err = matrix.targets()
        self.assertIsNone(err)
        self.assertEqual([(row["box"], row["tier"]) for row in rows], [
            ("hub", "local-disk"), ("hub", "memory"),
            ("fast", "local-disk"), ("fast", "memory")])
        self.assertFalse(rows[0]["reachable"])
        self.assertIn("disk unavailable", rows[0]["unavailable_reason"])
        self.assertTrue(rows[1]["reachable"])
        self.assertTrue(rows[2]["reachable"])

    def test_malformed_inventory_nodes_are_isolated_and_warned(self):
        value, warning = matrix._normalized_inventory({"nodes": [
            None,
            {"host": "hub", "probe_mode": "local", "label": 7},
            {"host": "remote", "device_kind": "fab-node", "label": 7,
             "reachable": True, "fab_hot_base": "/remote"},
            {"host": "remote", "device_kind": "fab-node", "label": "Duplicate",
             "reachable": True, "fab_hot_base": "/other"},
            {"host": "mobile", "device_kind": "mobile", "label": "Mobile",
             "reachable": False, "display_only": True, "ssh_host": ""},
        ]})
        self.assertEqual([node["host"] for node in value["nodes"]],
                         ["hub", "remote", "mobile"])
        self.assertEqual(value["nodes"][0]["label"], "hub")
        self.assertFalse(value["nodes"][1]["reachable"])
        self.assertTrue(value["nodes"][1]["display_only"])
        self.assertIn("not an object", warning)
        self.assertIn("invalid label", warning)
        self.assertIn("duplicate host remote", warning)
        self.assertNotIn("mobile", warning)
        with mock.patch.object(matrix, "_inventory", return_value=(value, warning)), \
                mock.patch.object(matrix, "_local_disk_target",
                                  return_value=("/local", "local")):
            rows, err = matrix.targets()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(isinstance(row["box_label"], str) for row in rows))
        self.assertEqual(rows[2]["reachable"], False)
        self.assertEqual(err, warning)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.values = {
            "SEQUENTIAL_BYTES": matrix.SEQUENTIAL_BYTES,
            "SEQUENTIAL_BLOCK_BYTES": matrix.SEQUENTIAL_BLOCK_BYTES,
            "RANDOM_OPERATIONS": matrix.RANDOM_OPERATIONS,
            "FSYNC_OPERATIONS": matrix.FSYNC_OPERATIONS,
            "MIN_FREE_BYTES": matrix.MIN_FREE_BYTES,
        }
        matrix.SEQUENTIAL_BYTES = 1024 * 1024
        matrix.SEQUENTIAL_BLOCK_BYTES = 256 * 1024
        matrix.RANDOM_OPERATIONS = 32
        matrix.FSYNC_OPERATIONS = 4
        matrix.MIN_FREE_BYTES = 3 * 1024 * 1024
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self.values.items():
            setattr(matrix, name, value)

    def test_probe_works_without_pread_or_pwrite(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(matrix.os, "pread", None, create=True), \
                mock.patch.object(matrix.os, "pwrite", None, create=True):
            result = matrix._probe(d)
        self.assertEqual(set(METRICS), set(result) & set(METRICS))
        self.assertTrue(all(result[key] >= 0 for key in METRICS))

    def test_probe_cleans_up_after_failure(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(matrix.os, "fsync", side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(OSError, "synthetic"):
                matrix._probe(d)
            self.assertEqual(os.listdir(d), [])

    def test_unsupported_cache_evict_is_best_effort(self):  # noqa: VACUOUS_ASSERTION — False is the recorded fallback capability
        with mock.patch.object(matrix.os, "posix_fadvise",
                               side_effect=OSError(95, "unsupported"), create=True), \
                mock.patch.object(matrix.os, "POSIX_FADV_DONTNEED", 4, create=True):
            self.assertFalse(matrix._evict(7))

    def test_short_random_write_refuses_and_cleans_up(self):  # noqa: VACUOUS_ASSERTION — raised error plus empty directory prove refusal and cleanup
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(matrix, "_pwrite", return_value=0):
            with self.assertRaisesRegex(OSError, "short random write"):
                matrix._probe(d)
            self.assertEqual(os.listdir(d), [])

    def test_pwrite_all_retries_partial_writes_at_the_next_offset(self):
        with mock.patch.object(matrix, "_pwrite", side_effect=(2, 2)) as write:
            self.assertEqual(matrix._pwrite_all(7, b"abcd", 11), 4)
        self.assertEqual([call.args[2] for call in write.call_args_list], [11, 13])

    def test_each_read_phase_records_cache_evict_evidence(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(matrix, "_evict", side_effect=(False, True)):
            result = matrix._probe(d)
        self.assertFalse(result["seq_read_cache_evicted"])
        self.assertTrue(result["random_read_cache_evicted"])
        self.assertFalse(result["cache_evict_supported"])

    def test_remote_program_is_the_same_probe_and_returns_json(self):  # noqa: VACUOUS_ASSERTION — status/engine/metric equalities are the positive controls on the emitted JSON
        with tempfile.TemporaryDirectory() as d:
            proc = subprocess.run([shutil.which("python3") or sys.executable,
                                   "-", d],
                                  input=matrix._remote_program(), text=True,
                                  capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["method"]["engine"], "python-stdlib-v1")
        self.assertIn("fsync_p95_ms", result["metrics"])

    def test_remote_host_is_validated_before_ssh(self):  # noqa: VACUOUS_ASSERTION — the next test is the safe-host positive control
        with mock.patch.object(matrix.subprocess, "run") as run:
            for host in ("bad;host", "-V"):
                with self.subTest(host=host), \
                        self.assertRaisesRegex(ValueError, "invalid SSH host"):
                    matrix._remote_probe(host, "/tmp")
        run.assert_not_called()

    def test_remote_ssh_terminates_options_before_host(self):
        proc = subprocess.CompletedProcess([], 0,
                                           json.dumps({"status": "unavailable"}), "")
        with mock.patch.object(matrix.subprocess, "run", return_value=proc) as run:
            matrix._remote_probe("safe-host", "/tmp")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--") + 1], "safe-host")
        self.assertEqual(argv[0], "ssh")

    def test_malformed_remote_metrics_become_one_unavailable_row(self):
        target = {"box": "remote", "box_label": "Remote", "tier": "memory",
                  "tier_label": "Memory", "transport": "ssh",
                  "ssh_host": "remote", "path": "/dev/shm",
                  "path_label": "/dev/shm", "reachable": True, "order": 1}
        with mock.patch.object(matrix, "_remote_probe",
                               return_value={"status": "ok", "metrics": {}}):
            row = matrix._row(target)
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("invalid metrics", row["error"])

        result = {"status": "ok", "metrics": dict(METRICS), "measured_at": 7}
        with mock.patch.object(matrix, "_remote_probe", return_value=result):
            row = matrix._row(target)
        self.assertEqual(row["status"], "unavailable")
        self.assertIn("invalid measured_at", row["error"])


class SnapshotTest(unittest.TestCase):
    def test_partial_measurement_is_written_after_all_rows(self):
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "state", "matrix.json")
            targets = [{"box": "a", "tier": "local-disk"},
                       {"box": "a", "tier": "memory"}]
            rows = [dict(snapshot()["rows"][0], id="a:local-disk", box="a",
                         box_label="A"),
                    {"id": "a:memory", "box": "a", "box_label": "A",
                     "tier": "memory", "tier_label": "Memory", "status": "unavailable",
                     "transport": "ssh", "path_label": "/dev/shm", "order": 1,
                     "measured_at": "2026-08-04T12:00:00Z", "error": "offline"}]
            @contextlib.contextmanager
            def locked(_path):
                yield True
            with mock.patch.object(matrix, "path", return_value=artifact), \
                    mock.patch.object(matrix, "targets", return_value=(targets, None)), \
                    mock.patch.object(matrix, "_row", side_effect=rows), \
                    mock.patch.object(matrix.eventledger, "locked", locked), \
                    mock.patch.object(matrix.pk, "event"):
                value = matrix.measure(now=dt.datetime(
                    2026, 8, 4, 12, tzinfo=dt.timezone.utc))
            self.assertEqual(value["status"], "partial")
            with open(artifact, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["rows"], rows)
            names = os.listdir(os.path.dirname(artifact))
            self.assertIn("matrix.json", names)
            self.assertFalse(any(name.endswith(".tmp") for name in names))

    def test_inventory_warning_prevents_false_complete_status(self):
        self.assertEqual(matrix._snapshot_status([{"status": "ok"}],
                                                 "inventory row malformed"),
                         "partial")
        self.assertEqual(matrix._snapshot_status([{"status": "ok"}]), "complete")

    def test_failure_before_final_write_preserves_prior_artifact(self):  # noqa: VACUOUS_ASSERTION — preserved JSON is the positive control
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "matrix.json")
            prior = snapshot()
            matrix.pk.write_json(artifact, prior)
            @contextlib.contextmanager
            def locked(_path):
                yield True
            with mock.patch.object(matrix, "path", return_value=artifact), \
                    mock.patch.object(matrix, "targets", side_effect=ValueError("bad target")), \
                    mock.patch.object(matrix.eventledger, "locked", locked):
                with self.assertRaisesRegex(ValueError, "bad target"):
                    matrix.measure()
            self.assertTrue(os.path.isfile(artifact))
            with open(artifact, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), prior)

    def test_invalid_candidate_preserves_prior_artifact(self):  # noqa: VACUOUS_ASSERTION — prior JSON equality is the positive control
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "matrix.json")
            prior = snapshot()
            matrix.pk.write_json(artifact, prior)
            bad = dict(snapshot()["rows"][0], metrics={})
            @contextlib.contextmanager
            def locked(_path):
                yield True
            with mock.patch.object(matrix, "path", return_value=artifact), \
                    mock.patch.object(matrix, "targets", return_value=([{}], None)), \
                    mock.patch.object(matrix, "_row", return_value=bad), \
                    mock.patch.object(matrix.eventledger, "locked", locked):
                with self.assertRaisesRegex(ValueError, "metrics are incomplete"):
                    matrix.measure()
            with open(artifact, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), prior)

    def test_consumer_fields_and_aggregate_status_are_validated(self):
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "matrix.json")
            missing_box = snapshot()
            del missing_box["rows"][0]["box"]
            matrix.pk.write_json(artifact, missing_box)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("row box is missing", value["message"])

            false_complete = snapshot()
            false_complete["rows"][0].update(status="unavailable", error="offline")
            false_complete["rows"][0].pop("metrics")
            matrix.pk.write_json(artifact, false_complete)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("expected partial", value["message"])

            inconsistent = snapshot()
            inconsistent["rows"][0]["metrics"]["seq_read_cache_evicted"] = False
            matrix.pk.write_json(artifact, inconsistent)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("evidence is inconsistent", value["message"])

    def test_tier_enum_and_complete_pair_are_schema_invariants(self):
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "matrix.json")
            one_tier = snapshot()
            one_tier["rows"] = one_tier["rows"][:1]
            matrix.pk.write_json(artifact, one_tier)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("tier pair is incomplete", value["message"])

            unknown = snapshot()
            unknown["rows"][1].update(id="box:ssd", tier="ssd", tier_label="SSD")
            matrix.pk.write_json(artifact, unknown)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("unknown storage tier: ssd", value["message"])

            empty = snapshot(status="unavailable")
            empty["rows"] = []
            matrix.pk.write_json(artifact, empty)
            with mock.patch.object(matrix, "path", return_value=artifact):
                value = matrix.view()
            self.assertEqual(value["state"], "unavailable")
            self.assertIn("no storage boxes", value["message"])

    def test_missing_corrupt_stale_and_future_are_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            artifact = os.path.join(d, "matrix.json")
            with mock.patch.object(matrix, "path", return_value=artifact):
                self.assertEqual(matrix.view()["state"], "missing")
                with open(artifact, "w", encoding="utf-8") as handle:
                    handle.write("not json")
                self.assertEqual(matrix.view()["state"], "unavailable")
                matrix.pk.write_json(artifact, snapshot("2026-07-20T12:00:00Z"))
                old = matrix.view(now=dt.datetime(
                    2026, 8, 4, 12, tzinfo=dt.timezone.utc))
                self.assertTrue(old["stale"])
                matrix.pk.write_json(artifact, snapshot("2026-08-05T12:00:00Z"))
                future = matrix.view(now=dt.datetime(
                    2026, 8, 4, 12, tzinfo=dt.timezone.utc))
                self.assertIsNone(future["age_s"])
                self.assertIn("future", future["age_message"])


class CliTest(unittest.TestCase):
    def run_cmd(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = matrix.cmd_storage_matrix(args)
        return rc, out.getvalue(), err.getvalue()

    def test_verb_is_registered_with_findable_help(self):
        self.assertIn("storage-matrix", cli.VERBS)
        self.assertIn("explicit bounded measurement",
                      cli._VERB_HELP["storage-matrix"])

    def test_bare_and_json_reads_never_measure(self):
        with mock.patch.object(matrix, "view", return_value=dict(
                snapshot(), state="ok", age_s=1, stale=False,
                age_message=None, stale_after_s=matrix.STALE_S)), \
                mock.patch.object(matrix, "measure") as measure:
            self.assertEqual(self.run_cmd([])[0], 0)
            self.assertEqual(self.run_cmd(["--json"])[0], 0)
        measure.assert_not_called()

    def test_human_read_marks_cache_uncleared_metrics(self):
        value = dict(snapshot(), state="ok", age_s=1, stale=False,
                     age_message=None, stale_after_s=matrix.STALE_S)
        metrics = value["rows"][0]["metrics"]
        metrics["seq_read_cache_evicted"] = False
        metrics["cache_evict_supported"] = False
        with mock.patch.object(matrix, "view", return_value=value):
            rc, out, _err = self.run_cmd([])
        self.assertEqual(rc, 0)
        self.assertIn("cache not cleared: sequential read", out)

    def test_unknown_flag_refuses_before_measure(self):
        with mock.patch.object(matrix, "measure") as measure:
            rc, _out, err = self.run_cmd(["--measure", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", err)
        measure.assert_not_called()

    def test_measure_json_reports_partial_with_exit_one(self):
        value = dict(snapshot(status="partial"), state="ok", age_s=0,
                     stale=False, age_message=None, stale_after_s=matrix.STALE_S)
        with mock.patch.object(matrix, "measure", return_value=snapshot(status="partial")), \
                mock.patch.object(matrix, "view", return_value=value):
            rc, out, _err = self.run_cmd(["--measure", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["status"], "partial")


if __name__ == "__main__":
    unittest.main()
