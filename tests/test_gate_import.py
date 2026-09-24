"""gate import — the strict seam for remotely-minted receipts (#167).

Every arm here fails exactly one clause of the import ladder, and the fixture
diverges on precisely the field that clause reads — a receipt built by the
REAL minting grammar (gate._receipt_id over a real commit in a real scratch
repo), then perturbed one field at a time. The happy path is proven by
reading the DESTINATION ledgers back, never the return value alone."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from helm import eventledger, gate, gateimport, pk
from helm import projscope

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_PREFIX",
            "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0", "GIT_ALTERNATE_REFS",
            "GIT_REPLACE_REF_BASE")


_UNSET = object()   # "argument not given", distinct from an explicit None


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args),
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise AssertionError("git %s: %s" % (args, p.stderr))
    return p.stdout.strip()


class ImportBase(unittest.TestCase):
    def setUp(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        # A NEW PROCESS-GLOBAL MEMO IS A NEW WAY FOR ONE ARM TO POISON THE
        # NEXT. Every fixture below builds a fresh HELM_HOME, so a binding
        # ledger from a previous test could otherwise be served under a
        # stale key, which is the same defect as the seam it rides on.
        gateimport._BINDING_ROWS_MEMO.clear()
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-import-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "import-test-seat"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", ".")
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("content\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

    def tearDown(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        for key in ENV_KEYS:
            prior = self.prior.get(key)
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, **over):
        """A receipt the REAL grammar would mint: id computed by the same
        function the gate uses, over this scratch repo's real head/tree."""
        row = {"v": 3, "event": "gate", "ts": pk.now_ts(),
               "repo_id": "/remote/fab/wt/some-lane-abc12345",
               "head": self.head, "tree": self.tree, "dirty": False,
               "head_after": self.head, "tree_after": self.tree,
               "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.14.6",
                               "language": "3.14.6",
                               "executable": "/usr/bin/python3.14"},
               "argv": ["/usr/bin/python3.14", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 12.5,
               "status": "OK", "ran": 100, "skipped": 1, "detail": "",
               "elapsed": 12.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    def _v8(self, n=130):
        failures = [{"kind": "ERROR",
                     "test": "tests.test_many.Many.test_%d" % i,
                     "traceback": "x" * gate._FAILURE_TEXT_CAP}
                    for i in range(n)]
        record, chunks = gate._failure_record(failures)
        row = self._row(
            v=8, status="FAILED", rc=1, ran=n, skipped=0,
            detail="errors=%d" % n, failures_unreadable=False,
            host={"node": "fab", "system": "Linux", "release": "1",
                  "id": "machine"}, **record)
        return row, chunks

    def _timing(self, row):
        return gate._module_timing_event(row, {
            "state": "COMPLETE", "reason": None,
            "planned_modules": 1, "measured_modules": 1,
            "measured_tests": row["ran"], "module_wall": 10.0,
            "process_cpu": 5.0,
            "top": [{"module": "tests.test_remote", "tests": row["ran"],
                     "wall": 10.0, "process_cpu": 5.0}],
        })

    def _artifact(self, *rows):
        path = os.path.join(self.tmp, "gate-receipts.jsonl")
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    def _import(self, artifact, **kw):
        return gateimport.import_receipt(artifact, self.repo, **kw)

    def _ledger(self):
        try:
            with open(gate.receipts_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def _provenance(self):
        try:
            with open(gateimport.imports_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def _bindings(self):
        rows, err = eventledger.checked_events(gateimport.bindings_path(),
                                               strict=True)
        self.assertIsNone(err)
        return rows

    def _repo_identity(self, repo=None):
        return os.path.realpath(_git(repo or self.repo, "rev-parse",
                                    "--path-format=absolute", "--git-common-dir"))


class HappyPathTest(ImportBase):
    def test_a_genuine_row_keeps_canonical_content_and_audits_its_origin(self):
        row = self._row()
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(err)
        self.assertEqual(verdict, "imported")
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        # CONTENT-EQUIVALENT: canonical serialization is identical, id included;
        # an import that "improves" a field breaks its content identity.
        self.assertEqual(gateimport._canonical(stored[0]),
                         gateimport._canonical(row))
        prov = self._provenance()
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]["receipt"], row["id"])
        self.assertEqual(prov[0]["artifact"], os.path.abspath(self._artifact(row)))
        self.assertEqual(prov[0]["repo"], self.repo)
        self.assertEqual(prov[0]["actor"], "import-test-seat")
        # origin is READ, never typed or parsed-into-existence: repo = the
        # receipt's own repo_id; run and host stay ABSENT (a parsed tmp
        # dirname read back as a "run" is the same rumour class as an
        # inferred host — presence control: origin_repo is the real field).
        self.assertEqual(prov[0]["origin_repo"], row["repo_id"])
        self.assertNotIn("origin_host", prov[0])  # noqa: VACUOUS_ASSERTION — receipt/artifact/repo/actor controls above prove the provenance row is real and populated
        self.assertNotIn("origin_run", prov[0])  # noqa: VACUOUS_ASSERTION — same populated-row controls; absence means unknown origin was not invented
        binding = self._bindings()[0]
        self.assertEqual(binding["receipt"], row["id"])
        self.assertEqual(binding["importing_repo"], self._repo_identity())
        self.assertEqual(binding["origin_repo"], row["repo_id"])
        self.assertEqual((binding["head"], binding["tree"]),
                         (row["head"], row["tree"]))
        self.assertEqual(binding["artifact"]["path"],
                         os.path.abspath(self._artifact(row)))
        self.assertEqual(len(binding["artifact"]["sha256"]), 64)

    def test_valid_module_timing_sibling_survives_import(self):
        row = self._row()
        timing = self._timing(row)
        got, verdict, err = self._import(self._artifact(timing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        timings, poisoned, skipped = gate._timings(self._ledger())
        self.assertEqual((poisoned, skipped), (set(), 0))
        self.assertEqual(timings[row["id"]], timing)

    def test_complete_receipt_and_timing_reimport_is_byte_identical_noop(self):
        row = self._row()
        timing = self._timing(row)
        artifact = self._artifact(timing, row)
        got, verdict, err = self._import(artifact)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        paths = (gate.receipts_path(), gateimport.bindings_path(),
                 gateimport.imports_path())
        before = {}
        for path in paths:
            with open(path, "rb") as fh:
                before[path] = fh.read()
        self.assertTrue(all(before.values()),
                        "positive control: every destination ledger has bytes")
        got, verdict, err = self._import(artifact)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "duplicate", None))
        for path in paths:
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), before[path], path)

    def test_differing_valid_timing_does_not_replace_stored_advisory(self):  # noqa: VACUOUS_ASSERTION — the stored timing id is asserted present in nonempty receipt-ledger bytes before importing an independently valid, content-distinct timing and requiring those exact bytes plus the reconciled original timing to survive
        row = self._row()
        timing = self._timing(row)
        got, verdict, err = self._import(self._artifact(timing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        differing = json.loads(json.dumps(timing))
        differing["module_wall"] = 9.0
        differing["process_cpu"] = 4.0
        differing["unattributed_wall"] = 3.0
        differing["top"][0]["wall"] = 9.0
        differing["top"][0]["process_cpu"] = 4.0
        differing["id"] = gate._timing_id(differing)
        self.assertNotEqual(differing["id"], timing["id"],
                            "must-hit control: the incoming advisory differs")
        self.assertIsNone(gate._timing_event_error(differing),
                          "must-hit control: the differing advisory is valid")
        with open(gate.receipts_path(), "rb") as fh:
            before = fh.read()
        self.assertIn(timing["id"].encode(), before,
                      "positive control: stored timing bytes are present")
        got, verdict, err = self._import(self._artifact(differing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "duplicate", None))
        with open(gate.receipts_path(), "rb") as fh:
            self.assertEqual(fh.read(), before)
        timings, poisoned, skipped = gate._timings(self._ledger())
        self.assertEqual((poisoned, skipped), (set(), 0))
        self.assertEqual(timings[row["id"]], timing)

    def test_malformed_timing_sibling_cannot_block_receipt_authority(self):
        row = self._row()
        timing = dict(self._timing(row), top=[{"module": "forged"}])
        got, verdict, err = self._import(self._artifact(timing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual([event["event"] for event in self._ledger()], ["gate"])
        self.assertEqual(self._bindings()[0]["receipt"], row["id"])

    def test_stored_advisory_id_collision_cannot_poison_receipt_authority(self):
        row = self._row()
        got, verdict, err = self._import(self._artifact(row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        timing = self._timing(row)
        timing["id"] = row["id"]
        self.assertIn("content id", gate._timing_event_error(timing),
                      "must-hit control: the colliding sibling is malformed")
        self.assertTrue(eventledger.append(gate.receipts_path(), timing))
        self.assertIsNone(gateimport._durable_receipt_err(row))
        authorized, why = gateimport.repository_authorization(row, self.repo)
        self.assertEqual((authorized, why), (True, None))

    def test_unknown_null_display_total_is_ignored_before_show(self):
        row = self._row()
        timing = gate._module_timing_event(row, {
            "state": "UNKNOWN", "reason": "partial census",
            "planned_modules": 1, "measured_modules": 0,
            "measured_tests": 0, "module_wall": 0.0,
            "process_cpu": 0.0, "top": [],
        })
        timing["module_wall"] = None
        timing["id"] = gate._timing_id(timing)
        self.assertIn("module_wall", gate._timing_event_error(timing))
        got, verdict, err = self._import(self._artifact(timing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual([event["event"] for event in self._ledger()], ["gate"])
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(gate._cmd_show([row["id"]]), 0)
        self.assertIn("no module timing sibling is available", out.getvalue())

    def test_oversized_timing_integer_is_ignored_before_import(self):
        row = self._row()
        timing = gate._module_timing_event(row, {
            "state": "UNKNOWN", "reason": "partial census",
            "planned_modules": 1, "measured_modules": 0,
            "measured_tests": 0, "module_wall": 0.0,
            "process_cpu": 0.0, "top": [],
        })
        timing["module_wall"] = 10 ** 400
        timing["id"] = gate._timing_id(timing)
        self.assertIn("module_wall", gate._timing_event_error(timing),
                      "must-hit control: the oversized JSON integer was read")
        got, verdict, err = self._import(self._artifact(timing, row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual([event["event"] for event in self._ledger()], ["gate"])
        self.assertEqual(self._bindings()[0]["receipt"], row["id"])

    def test_complete_timing_relations_must_agree_before_import(self):  # noqa: VACUOUS_ASSERTION — each case carries a recomputed content id and a successful authoritative receipt import before asserting that its contradictory advisory sibling stayed absent
        row = self._row()
        cases = (
            ("top test total", {"top_tests": row["ran"] + 1}, True),
            ("top wall total", {"top_wall": 11.0}, True),
            ("top CPU total", {"top_cpu": 6.0}, True),
            ("null module wall", {"module_wall": None}, True),
            ("null process CPU", {"process_cpu": None}, True),
            ("module wall exceeds runner", {
                "module_wall": 12.0 + gate._TIMING_RUNNER_TOLERANCE + 0.000001,
                "top_wall": 12.0 + gate._TIMING_RUNNER_TOLERANCE + 0.000001},
             True),
            ("derived unattributed wall", {"unattributed_wall": 99.0}, True),
            ("receipt test census", {
                "measured_tests": row["ran"] - 1,
                "top_tests": row["ran"] - 1}, False),
        )
        for name, changes, event_invalid in cases:
            with self.subTest(name=name):
                timing = json.loads(json.dumps(self._timing(row)))
                timing["measured_tests"] = changes.get(
                    "measured_tests", timing["measured_tests"])
                timing["module_wall"] = changes.get(
                    "module_wall", timing["module_wall"])
                timing["process_cpu"] = changes.get(
                    "process_cpu", timing["process_cpu"])
                timing["unattributed_wall"] = changes.get(
                    "unattributed_wall", timing["unattributed_wall"])
                timing["top"][0]["tests"] = changes.get(
                    "top_tests", timing["top"][0]["tests"])
                timing["top"][0]["wall"] = changes.get(
                    "top_wall", timing["top"][0]["wall"])
                timing["top"][0]["process_cpu"] = changes.get(
                    "top_cpu", timing["top"][0]["process_cpu"])
                timing["id"] = gate._timing_id(timing)
                self.assertEqual(
                    gate._timing_event_error(timing) is not None,
                    event_invalid,
                    "positive control: the receipt-census arm is internally "
                    "consistent and must be rejected only beside its receipt")
                got, _verdict, err = self._import(
                    self._artifact(timing, row))
                self.assertIsNone(err, name)
                self.assertEqual(got["id"], row["id"])
                self.assertNotIn(row["id"], gate._timings(self._ledger())[0])
        self.assertEqual([event["event"] for event in self._ledger()], ["gate"])
        self.assertEqual(self._bindings()[0]["receipt"], row["id"],
                         "contradictory diagnostics cannot erase authority")

    def test_timing_append_failure_cannot_block_receipt_authority_and_retry_repairs_it(self):
        row = self._row()
        timing = self._timing(row)
        artifact = self._artifact(timing, row)
        append = eventledger.append_unlocked

        def fail_timing(path, event):
            if event.get("event") == gate._TIMING_EVENT:
                return False
            return append(path, event)

        with mock.patch.object(eventledger, "append_unlocked",
                               side_effect=fail_timing):
            got, verdict, err = self._import(artifact)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual([event["event"] for event in self._ledger()], ["gate"])
        self.assertEqual(self._bindings()[0]["receipt"], row["id"],
                         "authoritative binding must survive advisory failure")
        got, verdict, err = self._import(artifact)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "duplicate", None))
        self.assertEqual(gate._timings(self._ledger())[0][row["id"]], timing)

    def test_v8_import_validates_and_writes_chunks_before_the_receipt(self):  # noqa: VACUOUS_ASSERTION — stored chunk+receipt order and receipts() admission positively prove the imported object before error absence is asserted
        row, chunks = self._v8()
        unbounded = [{"kind": "ERROR", "test": "tests.Case.test_%d" % i,
                      "traceback": "x" * gate._FAILURE_TEXT_CAP}
                     for i in range(row["failure_total"])]
        self.assertGreater(len(json.dumps(unbounded).encode("utf-8")),
                           eventledger.MAX_EVENT_BYTES,
                           "must-hit control: import object replaces a >64KiB row")
        artifact = self._artifact(*(chunks + [row]))
        got, verdict, err = self._import(artifact, want_id=row["id"])
        self.assertIsNone(err)
        self.assertEqual(verdict, "imported")
        self.assertEqual(got["id"], row["id"])
        stored = self._ledger()
        self.assertEqual(stored[:-1], chunks)
        self.assertEqual(stored[-1], row)
        self.assertTrue(all(len((json.dumps(event, ensure_ascii=False,
                                            separators=(",", ":")) + "\n").encode(
                                                "utf-8"))
                            <= eventledger.MAX_EVENT_BYTES for event in stored))
        receipts, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([receipt["id"] for receipt in receipts], [row["id"]])
        self.assertEqual(self._provenance()[0]["chunks"], row["failure_chunks"])

    def test_v8_id_binds_display_totals_and_chunk_references(self):  # noqa: VACUOUS_ASSERTION — the unedited-row equality positively proves the same receipt-id observable before each bound field is changed
        row, _chunks = self._v8()
        self.assertEqual(gate._receipt_id(dict(row)), row["id"])
        for field, value in (
                ("failures", []),
                ("failure_total", row["failure_total"] + 1),
                ("failure_diagnostics_omitted",
                 row["failure_diagnostics_omitted"] + 1),
                ("failure_chunks", row["failure_chunks"] + ["f" * 32])):
            edited = dict(row, **{field: value})
            self.assertNotEqual(gate._receipt_id(edited), row["id"], field)

    def test_oversized_artifact_refuses_before_provenance_or_receipt_write(self):  # noqa: VACUOUS_ASSERTION — the exact byte-limit refusal positively proves prevalidation fired before both destination ledgers are asserted empty
        row = self._row(label="x" * eventledger.MAX_EVENT_BYTES)
        got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("exceeding the %d-byte ledger limit" %
                      eventledger.MAX_EVENT_BYTES, err)
        self.assertIn("before provenance", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_oversized_referenced_chunk_refuses_before_any_write(self):  # noqa: VACUOUS_ASSERTION — the exact byte-limit refusal proves the referenced chunk was selected before both destination ledgers are asserted empty
        row, chunks = self._v8(gate.FAILURE_CAP + 1)
        chunk = dict(chunks[0], failures=[dict(item) for item in chunks[0]["failures"]])
        chunk["failures"][-1]["test"] = "x" * eventledger.MAX_EVENT_BYTES
        chunk["id"] = gate._failure_chunk_id(chunk)
        row = dict(row, failure_chunks=[chunk["id"]])
        row["id"] = gate._receipt_id(row)
        got, verdict, err = self._import(
            self._artifact(chunk, row), want_id=row["id"])
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("exceeding the %d-byte ledger limit" %
                      eventledger.MAX_EVENT_BYTES, err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_v8_import_refuses_an_impossible_empty_record_before_any_write(self):
        row = self._row(v=8, status="FAILED", rc=1, ran=0, skipped=0,
                        failures=[], failure_total=0,
                        failure_diagnostics_omitted=0, failure_chunks=[],
                        host={"node": "fab", "system": "Linux", "release": "1",
                              "id": "machine"})
        got, verdict, err = self._import(self._artifact(row), want_id=row["id"])
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("not an overflow object", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_v8_import_refuses_empty_and_partial_displayed_overflow(self):  # noqa: VACUOUS_ASSERTION — each fixed subtest positively matches the validator refusal before asserting both destination ledgers remain empty
        row, chunks = self._v8(gate.FAILURE_CAP + 1)
        for name, count in (("empty", 0), ("partial", gate.FAILURE_CAP - 1)):
            with self.subTest(name=name):
                edited = dict(
                    row, failures=row["failures"][:count],
                    failure_diagnostics_omitted=row["failure_total"] - count)
                edited["id"] = gate._receipt_id(edited)
                got, verdict, err = self._import(
                    self._artifact(*(chunks + [edited])), want_id=edited["id"])
                self.assertIsNone(got)
                self.assertIsNone(verdict)
                self.assertIn("complete bounded prefix", err)
                self.assertEqual(self._ledger(), [])
                self.assertEqual(self._provenance(), [])

    def test_v8_import_refuses_a_missing_chunk_before_any_write(self):  # noqa: VACUOUS_ASSERTION — the explicit missing-chunk refusal positively proves the artifact was parsed and the v8 seam fired before both destination ledgers are asserted empty
        row, chunks = self._v8()
        got, verdict, err = self._import(
            self._artifact(*(chunks[1:] + [row])), want_id=row["id"])
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("failure identity chunk", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_valid_artifact_repairs_a_stored_incomplete_v8_object(self):
        row, chunks = self._v8()
        artifact = self._artifact(*(chunks + [row]))
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        got, verdict, err = self._import(artifact, want_id=row["id"])
        self.assertIsNone(err)
        self.assertEqual(verdict, "repaired")
        self.assertEqual(got["id"], row["id"])
        self.assertEqual(self._ledger(), [row] + chunks)
        receipts, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([receipt["id"] for receipt in receipts], [row["id"]])
        self.assertEqual(len(self._provenance()), 1)

    def test_complete_duplicate_writes_nothing(self):
        row, chunks = self._v8()
        artifact = self._artifact(*(chunks + [row]))
        self._import(artifact, want_id=row["id"])
        before_ledger = list(self._ledger())
        before_provenance = list(self._provenance())
        got, verdict, err = self._import(artifact, want_id=row["id"])
        self.assertIsNone(err)
        self.assertEqual(verdict, "duplicate")
        self.assertEqual(got["id"], row["id"])
        self.assertEqual(self._ledger(), before_ledger)
        self.assertEqual(self._provenance(), before_provenance)

    def test_stored_later_chunk_poison_never_reports_complete_duplicate(self):
        row, chunks = self._v8(gate.FAILURE_CAP + 1)
        chunk = chunks[0]
        poison = dict(chunk, failures=list(chunk["failures"]) + [
            {"kind": "FAIL", "test": "tests.Other.test_y"}])
        before = [chunk, poison, row]
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for event in before:
                fh.write(json.dumps(event) + "\n")
        receipts, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(receipts, [])
        self.assertGreater(skipped, 0)

        got, verdict, err = self._import(
            self._artifact(*(chunks + [row])), want_id=row["id"])
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("poisoned", err)
        self.assertIn("conflicting stored content", err)
        self.assertEqual(self._ledger(), before)
        self.assertEqual(self._provenance(), [])

    def test_the_receipt_ledger_itself_never_records_the_import(self):
        # Distinguishable lives in the PROVENANCE ledger; the receipt row
        # stays exactly what the minting machine wrote, so its id resolves.
        row = self._row()
        self._import(self._artifact(row))
        stored = self._ledger()
        self.assertEqual(stored[0]["id"], row["id"])   # positive control:
        # the row IS there — so the absence below is about content, not
        # about an empty ledger.
        self.assertNotIn("imported", json.dumps(stored[0]))  # noqa: VACUOUS_ASSERTION — presence control three lines up: stored[0]["id"] == row["id"] proves the row exists before this absence is read


class OrderingTest(ImportBase):
    def test_receipt_lock_failure_precedes_both_ledger_appends(self):  # noqa: VACUOUS_ASSERTION — the positive lock-failure diagnostic proves the refused-lock branch ran before all destination-ledger absence checks
        row = self._row()

        @contextlib.contextmanager
        def refused(_path):
            yield False

        with mock.patch.object(gateimport.eventledger, "locked",
                               side_effect=refused):
            got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("lock FAILED before provenance or receipt append", err)
        self.assertNotIn("provenance appended", err)
        self.assertEqual(self._provenance(), [])
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._ledger(), [])

    def test_lock_time_conflict_refuses_without_appending_the_object(self):
        row = self._row()
        artifact = self._artifact(row)
        real_locked = eventledger.locked
        poisoned = dict(row, status="FAILED")

        @contextlib.contextmanager
        def raced(path):
            if path == gate.receipts_path():
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(poisoned) + "\n")
            with real_locked(path) as held:
                yield held

        with mock.patch.object(gateimport.eventledger, "locked", side_effect=raced):
            got, verdict, err = self._import(artifact)
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("DIFFERENT content", err)
        self.assertEqual(self._ledger(), [poisoned])
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._provenance(), [])

    def test_provenance_is_durable_before_the_receipt_append(self):
        """Historical symbol retained; the canonical binding inverts its law.

        The audit row called provenance is now a non-authoritative pointer and
        therefore writes LAST. Receipt then binding is the durable authority.
        """
        row, order = self._row(), []
        real_unlocked = eventledger.append_unlocked

        def append_unlocked(path, event):
            order.append(path)
            if path == gateimport.bindings_path():
                self.assertEqual(self._ledger()[0]["id"], row["id"])
            elif path == gateimport.imports_path():
                self.assertEqual(self._bindings()[0]["receipt"], row["id"])
            return real_unlocked(path, event)

        with mock.patch.object(gateimport.eventledger, "append_unlocked",
                               side_effect=append_unlocked):
            _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(order, [gate.receipts_path(), gateimport.bindings_path(),
                                 gateimport.imports_path()])

    def test_receipt_append_failure_leaves_only_the_harmless_provenance_orphan(self):
        """Historical symbol retained; receipt failure now leaves NO orphan."""
        row = self._row()
        real = eventledger.append_unlocked

        def fail_receipt(path, event):
            return False if path == gate.receipts_path() else real(path, event)

        with mock.patch.object(gateimport.eventledger, "append_unlocked",
                               side_effect=fail_receipt):
            got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("receipt append failed", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._provenance(), [])

    def test_binding_append_failure_leaves_a_receipt_that_authorizes_nothing(self):  # noqa: VACUOUS_ASSERTION — blocked=[receipt] proves the binding-append seam fired; the same artifact unblocked then fills both empty ledgers, while gate.bind positively refuses the receipt-only half-state
        row = self._row(v=4, host={"node": "n", "system": "Linux",
                                   "release": "r", "id": "i"})
        real = eventledger.append_unlocked
        blocked = []

        def append(path, event):
            if path == gateimport.bindings_path():
                blocked.append(event["receipt"])
                return False
            return real(path, event)

        with mock.patch.object(gateimport.eventledger, "append_unlocked",
                               side_effect=append):
            got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("canonical binding append failed", err)
        self.assertEqual(blocked, [row["id"]],
                         "the hostile binding append seam must execute once")
        self.assertEqual(self._ledger()[0]["id"], row["id"])
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._provenance(), [])
        binding, why = gateimport.canonical_binding(row, self.repo,
                                                    migrate=False)
        self.assertIsNone(binding)
        self.assertIn("no canonical binding", why)
        state, rid, why = gate.bind(
            gate.evidence_line(row), row["head"], repo_id=self.repo)
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", why)

        got, verdict, err = self._import(self._artifact(row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual(self._bindings()[0]["receipt"], row["id"],
                         "unblocked retry proves the binding ledger is live")
        self.assertEqual(self._provenance()[0]["receipt"], row["id"],
                         "unblocked retry proves the audit ledger is live")

    def test_retry_repairs_only_the_missing_audit_pointer(self):  # noqa: VACUOUS_ASSERTION — blocked=[receipt] proves the audit-append seam fired; the unpatched retry flips provenance from zero rows to exactly one and then reaches the duplicate arm
        row, artifact = self._row(), None
        artifact = self._artifact(row)
        real = eventledger.append_unlocked
        blocked = []

        def fail_audit(path, event):
            if path == gateimport.imports_path():
                blocked.append(event["receipt"])
                return False
            return real(path, event)

        with mock.patch.object(gateimport.eventledger, "append_unlocked",
                               side_effect=fail_audit):
            got, verdict, warning = self._import(artifact)
        self.assertEqual(blocked, [row["id"]],
                         "the first import must reach the hostile audit seam")
        self.assertEqual((got["id"], verdict), (row["id"], "imported"))
        self.assertIn("audit pointer append failed", warning)
        self.assertEqual(len(self._bindings()), 1)
        self.assertEqual(self._provenance(), [])

        got, verdict, err = self._import(artifact)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "audit-repaired", None))
        self.assertEqual(len(self._provenance()), 1,
                         "retry must flip the absent audit row to exactly one")
        self.assertEqual(self._provenance()[0]["receipt"], row["id"])
        self.assertEqual(self._import(artifact)[1:], ("duplicate", None))


class CliShapeTest(ImportBase):
    def test_help_prints_usage_without_reading_or_importing(self):  # noqa: VACUOUS_ASSERTION — exact usage output positively proves the help arm fired before destination-ledger absence is asserted
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gateimport.cmd_import(["--help"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), gateimport.IMPORT_USAGE)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_repaired_object_surfaces_a_missing_audit_pointer_as_warning(self):
        row, chunks = self._v8()
        artifact = self._artifact(*(chunks + [row]))
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        real = eventledger.append_unlocked

        def fail_audit(path, event):
            return False if path == gateimport.imports_path() else real(path, event)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gateimport.eventledger, "append_unlocked",
                               side_effect=fail_audit), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gateimport.cmd_import(
                [artifact, "--repo", self.repo, "--id", row["id"]])
        self.assertEqual(rc, 1)
        self.assertIn("missing validated failure chunks installed", out.getvalue())
        self.assertNotIn("audit pointer in", out.getvalue())
        self.assertIn("audit pointer append failed", err.getvalue())

    def test_origin_host_and_run_flags_are_not_an_import_surface(self):  # noqa: VACUOUS_ASSERTION — the unknown-argument refusal and empty destination ledger positively prove the obsolete flag was parsed and rejected
        row = self._row()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gateimport.cmd_import([
                self._artifact(row), "--repo", self.repo,
                "--host", "inferred-host", "--run", "inferred-run"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--host'", err.getvalue())
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])


class ActorResolutionTest(ImportBase):
    def test_actor_is_the_seat_from_the_identity_law_not_the_os_user(self):
        # importer shells often declare no HELM_CHAT_NAME; the identity law
        # (acting_seat) still resolves a seat via its floor — the OS user is
        # the fallback of last resort, never the value.
        row = self._row()
        with mock.patch("helm.gateimport.seats.acting_seat",
                        return_value="floor-resolved-seat"):
            self._import(self._artifact(row))
        self.assertEqual(self._provenance()[0]["actor"], "floor-resolved-seat")

    def test_actor_is_absent_when_the_identity_law_answers_nothing(self):
        row = self._row()
        with mock.patch("helm.gateimport.seats.acting_seat",
                        return_value=None):
            self._import(self._artifact(row))
        provenance = self._provenance()[0]
        self.assertEqual(provenance["receipt"], row["id"])
        self.assertNotIn("actor", provenance)  # noqa: VACUOUS_ASSERTION — receipt identity above proves the provenance row exists; absence means no process location was laundered into actor identity


class TamperTest(ImportBase):
    def test_an_edited_status_stops_resolving(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the honest row imports and the ledger flips [] -> 1 on the same observable
        row = self._row()
        row["status"] = "OK" if row["status"] != "OK" else "FAILED"
        # id deliberately NOT recomputed: this is the pasted-verdict shape.
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("content id mismatch", err)
        self.assertEqual(self._ledger(), [])
        # positive control on the same observable: the HONEST row imports,
        # so the emptiness above measured the refusal, not a broken pipeline.
        _, verdict, err = self._import(self._artifact(self._row()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_v8_receipt_id_tamper_is_diagnosed_before_missing_chunks(self):
        row, chunks = self._v8()
        row["status"] = "OK"
        _, verdict, err = self._import(
            self._artifact(*(chunks[1:] + [row])), want_id=row["id"])
        self.assertIsNone(verdict)
        self.assertIn("content id mismatch", err)
        self.assertNotIn("incomplete", err)

    def test_chunk_rejections_name_shape_id_conflict_and_oversize(self):
        row, chunks = self._v8(gate.FAILURE_CAP + 1)
        original = chunks[0]
        cases = []
        malformed = dict(original, failures=[{"kind": "ERROR"}])
        cases.append(("shape", [malformed], "unreadable identity"))
        mismatched = dict(original, id="f" * gate._FAILURE_CHUNK_ID_LEN)
        moved = dict(row, failure_chunks=[mismatched["id"]])
        moved["id"] = gate._receipt_id(moved)
        cases.append(("id", [mismatched], "content id does not match"))
        conflict = dict(original, failures=list(original["failures"]) + [
            {"kind": "FAIL", "test": "tests.Other.test_y"}])
        conflict["id"] = original["id"]
        cases.append(("claimed-id-mismatch", [original, conflict],
                      "content id does not match"))
        for name, supplied, reason in cases:
            with self.subTest(name=name):
                target = moved if name == "id" else row
                _, verdict, err = self._import(
                    self._artifact(*(supplied + [target])), want_id=target["id"])
                self.assertIsNone(verdict)
                self.assertIn(reason, err)

    def test_two_valid_chunks_claiming_one_id_are_diagnosed_as_conflict(self):
        row, chunks = self._v8(gate.FAILURE_CAP + 1)
        first = chunks[0]
        second = dict(first, failures=list(first["failures"]) + [
            {"kind": "FAIL", "test": "tests.Other.test_y"}])
        real = gate._failure_chunk_id
        with mock.patch.object(gate, "_failure_chunk_id",
                               return_value=first["id"]):
            second["id"] = gate._failure_chunk_id(second)
            moved = dict(row, failure_chunks=[first["id"]])
            moved["id"] = gate._receipt_id(moved)
            _, verdict, err = self._import(
                self._artifact(first, second, moved), want_id=moved["id"])
        self.assertIsNone(verdict)
        self.assertIn("conflicting content", err)
        self.assertNotEqual(real(second), first["id"],
                            "control: only the bounded hash-collision mock makes "
                            "both distinct chunks individually id-valid")

    def test_import_cmd_refuses_lone_surrogate_without_crashing(self):
        row = self._row(label="hostile-\ud800")
        row["id"] = gate._receipt_id(row)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gateimport.cmd_import([
                self._artifact(row), "--repo", self.repo, "--id", row["id"]])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err.getvalue())
        self.assertIn("not durable UTF-8 JSON", err.getvalue())
        self.assertIn("before provenance", err.getvalue())
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_a_pasted_base_check_stops_resolving(self):
        row = self._row(status="FAILED", rc=1,
                        failures=[{"kind": "FAIL", "test": "t.x",
                                   "traceback": "tb"}])
        row["base_check"] = {"verdict": "STALE_BASE"}
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("content id mismatch", err)


class DiagnosticKeyTest(ImportBase):
    """The READER half of the stderr-diagnostic feature, landed ALONE and
    ahead of any writer. It mints nothing new, so its own gate receipt keeps
    the old shape and imports on a trunk that has not learned the key."""

    GOOD_META = {"total_bytes": 4096, "truncated": True}

    def _diag(self, tail="RemoteDisconnected\n", meta=_UNSET, **over):
        """A red receipt carrying a diagnostic. `tail` and `meta` are POSITIONAL
        CONCEPTS, never **over keys — an earlier version let a caller pass
        stderr_tail through **over while also setting it here, so every case
        raised TypeError for a duplicate argument and the arm ERRORED before
        exercising a single one. A control that cannot run is not a control."""
        row = self._row(status="FAILED", rc=1, detail="errors=1", **over)
        row["stderr_tail"] = tail
        row["stderr_tail_meta"] = dict(self.GOOD_META) if meta is _UNSET \
            else meta
        return row

    def test_a_red_receipt_carrying_its_diagnostic_is_admitted(self):
        # MUST-HIT FIRST: the validator can still refuse, so the None below
        # means ADMITTED rather than "this check is inert".
        self.assertIsNotNone(gateimport._schema_err(
            dict(self._diag(), nonsense=1)))
        self.assertIsNone(gateimport._schema_err(self._diag()))
        # a row with NEITHER key is unaffected — every receipt minted before
        # this feature existed carries neither
        self.assertIsNone(gateimport._schema_err(self._row()))

    def test_admitting_the_key_is_not_accepting_whatever_arrives(self):
        """THE MUST-MISS. Membership says a gate MAY mint this name; it says
        nothing about the value. Each of these is a row an allowlist-only
        check would have taken straight into the ledger."""
        good = dict(self.GOOD_META)
        cases = {
            "tail is structure": self._diag(tail={"cmd": "rm -rf"}),
            "tail is an explicit null": self._diag(tail=None),
            "tail is empty": self._diag(tail=""),
            "meta is an explicit null": self._diag(meta=None),
            "meta is not an object": self._diag(meta="whatever"),
            "truncated is the string false": self._diag(
                meta=dict(good, truncated="false")),
            "truncated is 1": self._diag(meta=dict(good, truncated=1)),
            "total_bytes is a bool": self._diag(
                meta=dict(good, total_bytes=True)),
            "total_bytes is negative": self._diag(
                meta=dict(good, total_bytes=-1)),
            "meta carries a stowaway": self._diag(
                meta=dict(good, cmd="rm -rf")),
            "meta is missing a field": self._diag(
                meta={"truncated": False}),
            "tail longer than its own total": self._diag(
                tail="x" * 99, meta={"total_bytes": 10, "truncated": True}),
        }
        for name, row in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(gateimport._schema_err(row), name)

    def test_presence_is_membership_never_truthiness(self):
        """The collapse the per-case draft got wrong. An explicit null is
        PRESENT and malformed; it must not read as a receipt that simply had
        no diagnostic. Absence and a bad value are different facts."""
        for tail in (None, ""):
            row = self._diag(tail=tail)
            self.assertIn("stderr_tail", row)          # the arm's own premise
            self.assertIsNotNone(gateimport._schema_err(row), repr(tail))
        # positive control on the SAME observable: truly absent is admitted
        absent = self._diag()
        del absent["stderr_tail"], absent["stderr_tail_meta"]
        self.assertIsNone(gateimport._schema_err(absent))

    def test_a_half_present_diagnostic_is_refused_from_either_side(self):
        tail_only = self._diag()
        del tail_only["stderr_tail_meta"]
        self.assertIsNotNone(gateimport._schema_err(tail_only))
        meta_only = self._diag()
        del meta_only["stderr_tail"]
        self.assertIsNotNone(gateimport._schema_err(meta_only))

    def test_a_well_formed_diagnostic_row_survives_a_real_import(self):
        """Not just the shape check — the whole import path, because the
        defect this cures was a REFUSAL at import, not a malformed row."""
        _, verdict, err = self._import(self._artifact(self._diag()))
        self.assertIsNone(err, err)
        self.assertIsNotNone(verdict)


class SchemaTest(ImportBase):
    def test_v7_reaches_explicit_withdrawal_before_shared_schema_checks(self):
        row = {"event": "gate", "v": 7, "id": "withdrawn-v7"}
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("WITHDRAWN sharded script receipt (v7)", err)
        self.assertNotIn("missing minted keys", err)
        refusal = gate.row_refusal(row)
        self.assertIn("WITHDRAWN sharded script receipt (v7)", refusal)
        self.assertNotIn("carries no", refusal)

    def test_stored_v7_is_explicitly_withdrawn_not_future_or_missing(self):
        row = {"event": "gate", "v": 7, "id": "7" * 16}
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        got, err = gate.by_id(row["id"])
        self.assertIsNone(got)
        self.assertIn("WITHDRAWN sharded script receipt", err)
        self.assertNotIn("NEWER helm", err)
        self.assertNotIn("no minted gate receipt", err)

    def test_receipt_id_rejects_boolean_v_at_the_exact_integer_boundary(self):  # noqa: VACUOUS_ASSERTION — the unconditional equality first proves the same _receipt_id accessor accepts the otherwise-valid v8 row before the hostile one-field mutation reaches its exact-integer refusal
        # Positive control on the SAME producer accessor and otherwise-valid
        # current row: if the fixture or some earlier field were malformed, this
        # equality would fail before the hostile probe could earn any credit.
        row, _chunks = self._v8()
        self.assertEqual(gate._receipt_id(row), row["id"])

        hostile = dict(row, v=True)
        with self.assertRaisesRegex(
                ValueError,
                r"^receipt version must be an exact integer; got True$"):
            gate._receipt_id(hostile)

    def test_boolean_version_does_not_alias_receipt_v1(self):  # noqa: VACUOUS_ASSERTION — assertIn("version True", err) is an unconditional positive control on the door's own refusal text and runs BEFORE the two empty-ledger absence claims; it is exactly the assertion that went red without the cure
        # THE ID IS SET BY HAND ON PURPOSE. `gate._receipt_id` refuses a
        # non-int version before it hashes anything, so a fixture that asked
        # it for an id would die in the helper and never reach the door this
        # arm is about. A hand-set id is the hostile case anyway: a submitter
        # writing `{"v": true}` writes the id too.
        row = dict(self._row(), v=True, id="0" * 16)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("version True", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_an_unknown_version_is_refused_not_half_validated(self):
        # FAR-FUTURE, NOT THE NEXT ONE. This arm named v4, then v5, and each
        # time that version SHIPPED the arm silently stopped testing versions:
        # a known version falls past the version clause and is refused on KEYS
        # instead, so the assert on "version" fails and the arm was never
        # about the boundary it appeared to guard (same-refusal-different-gate).
        # Measured when v5 landed: v=5 -> "missing minted keys: executed, host",
        # v=6 and v=99 -> "version ... is not one this helm understands".
        # Naming the NEXT version repeats the trap on the next bump; naming a
        # version no schema will reach makes this a test of THE REFUSAL, which
        # is what its own comment always said it was for. The id is valid for
        # its shape, so only the version can be what stops it.
        row = self._row()
        row["v"] = 99
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("version", err)

    def test_a_row_missing_minted_keys_refuses(self):
        row = self._row()
        del row["interpreter"]
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("missing minted keys", err)
        self.assertIn("interpreter", err)

    def test_a_row_carrying_foreign_keys_refuses(self):
        row = self._row()
        row["imported_by"] = "someone"
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("keys no gate mints", err)

    def test_a_non_gate_event_refuses(self):
        row = self._row()
        row["event"] = "land"
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("not 'gate'", err)


class HostBoundVersionTest(ImportBase):
    """v4 receipts name their HOST, and every READER learns them here — one
    commit before anything mints one.

    WHY THE HALVES SHIP APART. The minting side and the import side share NOT
    ONE FILE, so every changed-file overlap check clears the pair while the
    composition is broken. Ship the writer first and a fab-minted receipt is
    refused loudly at `gate import` and, worse, SILENTLY SKIPPED by
    `gate.receipts()` — whose integrity filter recomputes the id, gets a
    different answer under a pre-v4 reader, and drops the row. `by_id` then
    reports "no minted gate receipt" about a receipt physically present in the
    ledger, and advises re-running the gate, which mints another unreadable
    one. Measured 2026-08-04 on 756b936006bf0e47: 685 rows read, skipped 1, a
    reviewer's APPROVE unable to bind, three functions traced to find out why.

    `host` is REQUIRED at v4 and FOREIGN below it, never merely optional: it is
    bound into the receipt id, so a v4 row without one and a v3 row with one
    both describe a minting that never happened."""

    _HOST = {"node": "snoozy-am5", "system": "Linux",
             "release": "6.17.0-40-generic", "id": "a33110f5fee74a15"}

    def _v4(self, **over):
        # The host block is written LITERALLY, never taken from a gate.host()
        # helper — this tree has none, and that is the property under test.
        return self._row(v=4, host=dict(self._HOST), **over)

    def test_a_v4_host_bound_receipt_imports(self):
        row = self._v4()
        _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], row["id"])
        self.assertEqual(stored[0]["v"], 4)
        # The host block survives unchanged. An import that dropped it would
        # still satisfy "a row landed", and the receipt would no longer name
        # the machine that is the entire point of the version.
        self.assertEqual(stored[0]["host"], self._HOST)

    def test_a_v4_receipt_without_a_host_refuses(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: restoring the host imports through the SAME path and the ledger [] -> 1
        row = self._v4()
        del row["host"]
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("missing minted keys", err)
        self.assertIn("host", err)
        self.assertEqual(self._ledger(), [])
        _, verdict, err = self._import(self._artifact(self._v4()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_a_v3_receipt_carrying_a_host_refuses(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: dropping the host imports through the SAME path and the ledger [] -> 1
        row = self._row(host=dict(self._HOST))
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("keys no gate mints", err)
        self.assertIn("host", err)
        self.assertEqual(self._ledger(), [])
        _, verdict, err = self._import(self._artifact(self._row()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_v3_still_imports_beside_v4(self):
        """The positive control for both refusals, on the SAME seam: the
        version gate still passes everything it always passed."""
        three, four = self._row(), self._v4()
        _, verdict, err = self._import(self._artifact(three))
        self.assertEqual((verdict, err), ("imported", None))
        _, verdict, err = self._import(self._artifact(four))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual({r["v"] for r in self._ledger()}, {3, 4})

    def test_the_host_is_bound_into_the_id_not_merely_carried(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the UNEDITED row imports and the ledger flips [] -> 1 on the same observable. Each _receipt_id() call is a fresh producer by the rung's own provenance rule, so no control it can credit exists for a hash-binding test.
        """Edit the node and the receipt must stop resolving — the same
        tamper-evidence every other bound field gets. A `host` that rode along
        unbound would let a fab receipt be re-labelled with another box's name
        and still verify."""
        row = self._v4()
        original = gate._receipt_id(row)
        moved = dict(row, host=dict(self._HOST, node="drowsy-am5"))
        self.assertNotEqual(gate._receipt_id(moved), original)
        # and the ledger refuses the edited row rather than storing it
        moved["id"] = original
        _, verdict, err = self._import(self._artifact(moved))
        self.assertIsNone(verdict)
        self.assertEqual(self._ledger(), [])
        # POSITIVE CONTROL on the same path: unedited, it imports.
        _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)


    def test_v4_keeps_every_earlier_versions_bindings(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the FIRST line of the body: an unedited copy recomputes to the stored id. The rung cannot credit it because each _receipt_id() call mints a separate producer identity.
        """A version bump must GROW the id, never re-cut it.

        `_receipt_id` gates the failure identities on `v in (2,3,4)` and the
        base check on `v in (3,4)`. Written as `== 2` / `== 3` they would look
        equally correct and would silently stop binding those fields for v4 —
        an edited failure list or a pasted STALE_BASE would resolve fine on a
        v4 receipt while still being caught on a v3 one. The file's comment
        names that hazard; this asserts it, because a mutation reverting the
        tuples passed the entire suite before this test existed."""
        base = self._v4()
        # POSITIVE CONTROL FIRST, unconditional: an UNEDITED copy recomputes to
        # exactly the stored id, so every inequality below is a real difference
        # and not a helper that returns junk for anything handed to it.
        self.assertEqual(gate._receipt_id(dict(base)), base["id"])
        for field, edited in (("failures", [{"id": "tests.forged.Case.test_x"}]),
                              ("failures_unreadable", True),
                              ("base_check", {"verdict": "STALE_BASE"})):
            moved = dict(base)
            moved[field] = edited
            self.assertNotEqual(
                gate._receipt_id(moved), gate._receipt_id(base),
                "%s is not bound into a v4 receipt id, so editing it leaves "
                "the receipt resolving — the version bump dropped a binding "
                "an earlier version had" % field)


class ReaderBeforeWriterTest(ImportBase):
    """THIS TREE READS v4 AND MUST NOT MINT IT.

    The whole value of landing the reader alone is that no v4 receipt exists
    until every helm can verify one. A well-meaning edit that flips
    `_mint_result` here would restore the exact silent-skip this commit
    prevents, and every other test in this file would stay green while it
    happened — they all construct their rows by hand."""

    def test_the_writer_flipped_only_after_every_reader_landed(self):  # noqa: VACUOUS_ASSERTION — the assertIn('6 if focus else 4') and assertIn(4, KNOWN_VERSIONS) are unconditional positive controls on the same two observables (mint source, import grammar) that every assertNotIn below reads
        """THE GUARD DID ITS JOB AND IS NOW INVERTED, deliberately.

        Its previous form asserted `"v": 3` and refused to let the writer flip
        in the same tree that taught the reader. It fired on exactly the change
        it was written for — this commit — which is what forced the flip to be
        its own reviewed land against a trunk that can already read v4. The
        assertion turns over rather than being deleted, because the property
        worth keeping is not "we mint v3", it is that MINT AND READER AGREE.

        The mint is CONDITIONAL now — `6 if focus else 4` — and each branch
        is pinned separately so removing EITHER minted version fails this arm
        on its own. v6 stays absent from the GENERIC import grammar: a plain
        artifact rests entirely on fields its submitter wrote. The separate
        challenge-framed gateroute door does not widen KNOWN_VERSIONS."""
        import inspect
        source = inspect.getsource(gate._mint_result)
        self.assertIn("MINTED_RECEIPT_VERSIONS", source)
        self.assertIn("FOCUSED_VERSION if focus", source)
        self.assertNotIn('"v": 3', source)
        self.assertEqual(gate.MINTED_RECEIPT_VERSIONS, (4, 8))
        # and the reader that must already understand it is HERE, on trunk,
        # not merely promised by this lane
        self.assertIn(4, gateimport.KNOWN_VERSIONS)
        self.assertIn(8, gateimport.KNOWN_VERSIONS)
        self.assertEqual(gateimport.VERSION_KEYS.get(4), frozenset(("host",)))
        # and the v6 reader agreement is the REFUSAL: never importable, and
        # `focus` foreign at every importable version.
        self.assertNotIn(6, gateimport.KNOWN_VERSIONS)
        self.assertEqual(gateimport.FOCUSED_VERSION, 6)
        for version, keys in gateimport.VERSION_KEYS.items():
            self.assertNotIn("focus", keys,
                             "v%d import grammar admits a focus block — an "
                             "imported scope is a submitter-written scope"
                             % version)

    def test_v3_receipt_ids_are_untouched_by_the_v4_grammar(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the first assertion: the v3 row recomputes to its own stored id before anything is compared against it.
        """The one property that makes landing this safe: every id already in
        the wild must recompute to exactly what it did before. A v4 branch that
        perturbed the v3 payload would invalidate every stored receipt at
        once."""
        row = self._row()
        self.assertEqual(row["id"], gate._receipt_id(row))
        # a v3 row is unaffected by the host grammar even when the key exists
        # on some OTHER row in the same ledger
        self.assertEqual(gate._receipt_id(dict(row)), row["id"])
        # and the v4 grammar really is reachable — otherwise the equality above
        # proves only that nothing ran
        four = self._row(v=4, host=dict(HostBoundVersionTest._HOST))
        self.assertNotEqual(gate._receipt_id(four), row["id"])


class RepoResolutionTest(ImportBase):
    def test_a_head_this_repo_never_saw_refuses(self):
        row = self._row(head="deadbeef" * 5)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("does not resolve", err)

    def test_a_tree_the_cited_head_cannot_produce_refuses(self):
        # head is real, tree is another commit's — the receipt-about-a-tree-
        # nobody-can-check-out shape, including every dirty-run receipt.
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("advanced\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "advance")
        other_tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        row = self._row(tree=other_tree)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("tree mismatch", err)


class IdempotencyTest(ImportBase):
    def test_the_same_import_twice_is_a_no_op_not_a_second_row(self):
        row = self._row()
        art = self._artifact(row)
        self._import(art)
        _, verdict, err = self._import(art)
        self.assertIsNone(err)
        self.assertEqual(verdict, "duplicate")
        self.assertEqual(len(self._ledger()), 1)
        # And no second provenance row: a no-op that audits itself as an
        # import would double-count origins.
        self.assertEqual(len(self._provenance()), 1)

    def test_an_edited_advisory_field_is_caught_at_the_conflict_clause(self):
        # detail/wall/label are ADVISORY: the content id deliberately does
        # not bind them ("every field a reader would RELY on"), so editing
        # one passes clause 3 — and the stored-content comparison is the
        # clause that catches it. Two guards, different fields; this arm
        # proves the second exists.
        row = self._row()
        self._import(self._artifact(row))
        conflict = dict(row)
        conflict["detail"] = "edited after storage"
        _, verdict, err = self._import(self._artifact(conflict))
        self.assertIsNone(verdict)
        self.assertIn("DIFFERENT content", err)
        self.assertEqual(len(self._ledger()), 1)

    def test_a_planted_ledger_row_under_the_same_id_refuses(self):
        # The ledger already holds a row wearing this id with OTHER hashed
        # content (only reachable by hand-editing the ledger — the exact
        # 2026-08-03 shape this verb exists to end). The import must refuse
        # both directions rather than trust either copy.
        stored = self._row()
        planted = dict(stored)
        planted["ran"] = 999
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w") as f:
            f.write(json.dumps(planted) + "\n")
        _, verdict, err = self._import(self._artifact(stored))
        self.assertIsNone(verdict)
        self.assertIn("DIFFERENT content", err)


class CanonicalBindingTest(ImportBase):
    def test_receipt_index_is_reused_only_for_the_same_ledger_generation(self):
        first = self._row()
        second = self._row(ran=first["ran"] + 1)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(eventledger.append(gate.receipts_path(), first))
        real = gateimport.eventledger.checked_events
        with mock.patch.object(gateimport.eventledger, "checked_events",
                               wraps=real) as checked:
            one, poisoned, err = gateimport._stored_ids()
            again, again_poisoned, again_err = gateimport._stored_ids()
            self.assertTrue(eventledger.append(gate.receipts_path(), second))
            changed, changed_poisoned, changed_err = gateimport._stored_ids()
        self.assertEqual((set(one), poisoned, err),
                         ({first["id"]}, set(), None))
        self.assertEqual((again, again_poisoned, again_err),
                         (one, poisoned, None))
        self.assertEqual((set(changed), changed_poisoned, changed_err),
                         ({first["id"], second["id"]}, set(), None))
        self.assertEqual(checked.call_count, 2,
                         "one immutable ledger generation was decoded twice")

    def test_empty_repository_identity_never_falls_back_to_the_process_cwd(self):  # noqa: VACUOUS_ASSERTION — the same accessor positively resolves self.repo first; the mocked backend then proves empty input returns before any Git dispatch rather than accidentally resolving cwd
        self.assertEqual(gateimport._repo_identity(self.repo), self._repo_identity(),
                         "control: a real repository must resolve positively")
        with mock.patch.object(gateimport.vcs, "backend") as backend:
            self.assertIsNone(gateimport._repo_identity(""))
        backend.assert_not_called()

    def test_missing_historical_worktrees_spawn_no_git_processes(self):  # noqa: VACUOUS_ASSERTION — after the zero-spawn stale-path assertion, the same test adds one extant pointer and unconditionally proves the backend fires exactly once and admits its id
        gateimport._repo_identity(self.repo)
        for n in range(3):
            self.assertTrue(eventledger.append(
                gateimport.imports_path(),
                {"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
                 "receipt": "gone-%d" % n,
                 "repo": os.path.join(self.tmp, "gone-%d" % n)}))
        with mock.patch.object(gateimport.vcs, "backend") as backend:
            ids, err, warning = gateimport.binding_candidate_ids(self.repo)
        self.assertEqual(ids, frozenset())
        self.assertEqual((err, warning), (None, None))
        backend.assert_not_called()

        live = os.path.join(self.tmp, "live-pointer")
        os.makedirs(live)
        self.assertTrue(eventledger.append(
            gateimport.imports_path(),
            {"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
             "receipt": "live", "repo": live}))
        with mock.patch.object(gateimport.vcs, "backend") as backend:
            common = self._repo_identity()
            backend.return_value.text.return_value = (
                0, "%s\n%s" % (common, common), "")
            live_ids, live_err, live_warning = \
                gateimport.binding_candidate_ids(self.repo)
        self.assertEqual((live_err, live_warning), (None, None))
        self.assertIn("live", live_ids)
        backend.assert_called_once_with(live)

    def test_repeated_live_historical_repository_resolves_identity_once(self):
        room = os.path.join(self.tmp, "room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        gateimport._repo_identity(self.repo)
        for rid in ("legacy-a", "legacy-b"):
            self.assertTrue(eventledger.append(
                gateimport.imports_path(),
                {"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
                 "receipt": rid, "repo": room}))
        real = gateimport.vcs.backend
        with mock.patch.object(gateimport.vcs, "backend", wraps=real) as backend:
            ids, err, warning = gateimport.binding_candidate_ids(self.repo)
            again, again_err, again_warning = \
                gateimport.binding_candidate_ids(self.repo)
        self.assertEqual(ids, frozenset(("legacy-a", "legacy-b")))
        self.assertEqual((again, again_err, again_warning),
                         (ids, None, None))
        self.assertEqual((err, warning), (None, None))
        self.assertEqual(backend.call_count, 1)

    def test_positive_repository_identity_expires_when_the_path_is_reused(self):  # noqa: VACUOUS_ASSERTION — both generations are real Git repositories and first is positively proved to be this repo before the replacement must resolve to a distinct non-None common-dir
        room = os.path.join(self.tmp, "reused-room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        first = gateimport._repo_identity(room)
        self.assertEqual(first, self._repo_identity(),
                         "control: the first path is this repo's worktree")
        _git(self.repo, "worktree", "remove", "--force", room)
        os.makedirs(room)
        _git(room, "init", "-q", ".")
        second = gateimport._repo_identity(room)
        self.assertIsNotNone(second, "control: the replacement is a repository")
        self.assertNotEqual(second, first,
                            "a reused path must not spend its prior authority")

    def test_linked_commondir_mutation_invalidates_an_unchanged_checkout_stamp(self):  # noqa: VACUOUS_ASSERTION — root and .git are positively asserted unchanged while the real linked commondir mutation must force a fresh Git read and UNKNOWN rather than the cached positive identity
        room = os.path.join(self.tmp, "linked-room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        self.assertEqual(gateimport._repo_identity(room), self._repo_identity())
        root = gateimport._path_stamp(room)
        marker = gateimport._path_stamp(os.path.join(room, ".git"))
        gitdir = _git(room, "rev-parse", "--absolute-git-dir")
        with open(os.path.join(gitdir, "commondir"), "w") as fh:
            fh.write("../missing-common-dir\n")
        self.assertEqual(gateimport._path_stamp(room), root)
        self.assertEqual(gateimport._path_stamp(os.path.join(room, ".git")), marker)
        real = gateimport.vcs.backend
        with mock.patch.object(gateimport.vcs, "backend", wraps=real) as backend:
            self.assertIsNone(gateimport._repo_identity(room))
        backend.assert_called_once_with(room)

    def test_commondir_retarget_between_git_resolution_and_fill_is_not_cached(self):  # noqa: VACUOUS_ASSERTION — a real linked worktree and second repository positively establish two distinct common dirs, the mutation callback proves retarget happened after Git resolved the first, and the cache key remains absent
        room = os.path.join(self.tmp, "retargeted-fill-room")
        other = os.path.join(self.tmp, "retargeted-fill-other")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        os.makedirs(other)
        _git(other, "init", "-q", ".")
        gitdir = _git(room, "rev-parse", "--absolute-git-dir")
        marker = os.path.join(gitdir, "commondir")
        other_common = _git(
            other, "rev-parse", "--path-format=absolute", "--git-common-dir")
        real = gateimport._repo_generation
        changed = []

        def retarget(*args):
            with open(marker, "w") as fh:
                fh.write(other_common + "\n")
            changed.append(True)
            return real(*args)

        with mock.patch.object(gateimport, "_repo_generation",
                               side_effect=retarget):
            self.assertIsNone(gateimport._repo_identity(room))
        self.assertEqual(changed, [True],
                         "control: retarget must occur after Git resolved paths")
        self.assertNotIn(os.path.abspath(room), gateimport._REPO_IDENTITIES)

    def test_commondir_deletion_between_git_resolution_and_fill_is_not_cached(self):  # noqa: VACUOUS_ASSERTION — the real linked marker is positively asserted present, the mutation callback proves deletion happened after Git resolution, and the cache key remains absent
        room = os.path.join(self.tmp, "deleted-fill-room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        gitdir = _git(room, "rev-parse", "--absolute-git-dir")
        marker = os.path.join(gitdir, "commondir")
        self.assertTrue(os.path.isfile(marker),
                        "control: linked worktree must start with commondir")
        real = gateimport._repo_generation
        deleted = []

        def delete(*args):
            os.unlink(marker)
            deleted.append(True)
            return real(*args)

        with mock.patch.object(gateimport, "_repo_generation", side_effect=delete):
            self.assertIsNone(gateimport._repo_identity(room))
        self.assertEqual(deleted, [True],
                         "control: deletion must occur after Git resolved paths")
        self.assertNotIn(os.path.abspath(room), gateimport._REPO_IDENTITIES)

    def test_malformed_legacy_repo_path_keeps_canonical_ids_and_warns(self):
        importing = self._repo_identity()
        bindings = [{"importing_repo": importing, "receipt": "canonical"}]
        pointers = [{"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
                     "receipt": "legacy", "repo": "bad\0path"}]
        with mock.patch.object(gateimport, "_binding_rows",
                               return_value=(bindings, None)), \
                mock.patch.object(gateimport, "_legacy_import_rows",
                                  return_value=(pointers, None)):
            ids, err, warning = gateimport.binding_candidate_ids(self.repo)
        self.assertEqual(ids, frozenset(("canonical",)))
        self.assertIsNone(err)
        self.assertIn("path is malformed", warning or "")
        self.assertIn("legacy placement is UNKNOWN", warning or "")

    def test_bounded_historical_scan_returns_partial_candidates_as_unknown(self):
        importing = self._repo_identity()
        bindings = [{"importing_repo": importing, "receipt": "canonical"}]
        pointers = [{"event": "gate-import",
                     "ts": "2026-08-26T00:00:00Z",
                     "receipt": "legacy", "repo": self.repo}]
        with mock.patch.object(gateimport, "_binding_rows",
                               return_value=(bindings, None)), \
                mock.patch.object(gateimport, "_legacy_import_rows",
                                  return_value=(pointers, None)), \
                mock.patch.object(gateimport, "_repo_identity",
                                  side_effect=(importing, importing)), \
                mock.patch.object(gateimport.time, "monotonic",
                                  side_effect=(0.0, 0.1, 0.2, 0.6)):
            ids, err, warning = gateimport.binding_candidate_ids(self.repo, budget_s=0.5)
        self.assertEqual(ids, frozenset(("canonical",)))
        self.assertIsNone(err)
        self.assertIn("exceeded 0.500s", warning or "")
        self.assertIn("legacy placement is UNKNOWN", warning or "")

    def test_budget_expires_while_the_real_audit_ledger_is_decoded(self):
        for rid in ("legacy-a", "legacy-b"):
            self.assertTrue(eventledger.append(
                gateimport.imports_path(),
                {"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
                 "receipt": rid, "repo": self.repo}))
        # THE CLOCK IS DRIVEN BY WHERE THE TIME GOES, NOT BY CALL ORDINAL.
        # This fixture was a fixed side_effect SEQUENCE, so it encoded how
        # many times the function reads the clock: adding a legitimate
        # deadline check anywhere earlier shifted the expiry into a different
        # PHASE and the arm failed for a reason that had nothing to do with
        # its subject. A fixture that breaks when you add a check is testing
        # the implementation's shape.
        clock = [0.0]
        real_legacy = gateimport._legacy_import_rows

        def slow_audit(*a, **kw):
            clock[0] += 10.0            # the audit decode is what overruns
            return real_legacy(*a, **kw)

        with mock.patch.object(gateimport.time, "monotonic",
                               lambda: clock[0]), \
                mock.patch.object(gateimport, "_legacy_import_rows",
                                  slow_audit):
            ids, err, warning = gateimport.binding_candidate_ids(self.repo, budget_s=0.5)
        self.assertEqual(ids, frozenset())
        self.assertIsNone(err)
        self.assertIn("during audit pointer read", warning or "")
        self.assertIn("legacy placement is UNKNOWN", warning or "")

    def test_gate_bind_spends_authority_only_in_the_importing_repository(self):
        row = self._row(v=4, host={"node": "n", "system": "Linux",
                                   "release": "r", "id": "i"})
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        state, rid, _why = gate.bind(
            gate.evidence_line(row), row["head"], repo_id=self.repo)
        self.assertEqual((state, rid), ("VERIFIED", row["id"]))

        other = os.path.join(self.tmp, "unbound-other")
        _git(self.tmp, "clone", "-q", self.repo, other)
        state, rid, why = gate.bind(
            gate.evidence_line(row), row["head"], repo_id=other)
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("no canonical authority", why)

    def test_a_symlink_at_the_canonical_path_gets_no_cached_authority(self):
        """THE MEMO MAY NOT ANSWER WHERE THE COLD READ REFUSES.

        The measured attack on the first cut: fill the cache from a
        legitimate import, rename the ledger, and drop a SYMLINK at the
        canonical path pointing at the SAME INODE. `os.stat` follows it, so
        (dev, ino, size, mtime_ns) came back identical and the memo served
        authority for a path `checked_events` refuses O_NOFOLLOW. The identity
        is now taken through that same seam, so the open raises and there is
        no key at all.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        warm, err = gateimport._binding_rows()
        self.assertEqual((err, len(warm)), (None, 1))
        self.assertIsNotNone(gateimport._bindings_identity(),
                             "MUST-HIT: no identity on a healthy ledger, so "
                             "the refusal below proves nothing")

        path = gateimport.bindings_path()
        moved = path + ".moved"
        os.rename(path, moved)
        os.symlink(moved, path)
        self.addCleanup(lambda: None)
        self.assertIsNone(gateimport._bindings_identity(),
                          "a symlink at the canonical path produced a cache "
                          "key — the memo can now answer where the validated "
                          "read refuses")
        rows, err = gateimport._binding_rows()
        self.assertIsNone(rows, "a symlinked ledger was served as authority")
        self.assertTrue(err)

    def test_a_hardlinked_ledger_gets_no_cached_authority(self):
        """THE SECOND HALF OF THE SAME HOLE. A hardlink moves only st_nlink,
        which `os.stat`-based identity did not carry, so the memo bypassed the
        private-regular-file check that `checked_events` performs."""
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        before = gateimport._bindings_identity()
        os.link(gateimport.bindings_path(),
                gateimport.bindings_path() + ".hard")
        after = gateimport._bindings_identity()
        # ONE ASSERTION OVER BOTH READINGS. "No key after the hardlink" is
        # satisfied by an identity function that never returns a key at all,
        # so the healthy reading is the control and it shares the observable.
        self.assertEqual(
            (before is None, after is None), (False, True),
            "expected a key on the healthy ledger and NONE after the "
            "hardlink; got before=%r after=%r" % (before, after))

    def test_a_cold_binding_read_respects_the_caller_budget(self):
        """THE COLD PATH SPENDS THE CALLER'S BUDGET.

        `binding_candidate_ids` declares budget_s=0.25 and calls
        `_binding_rows` between two `remaining()` checks, which bounds neither
        the read nor the parse: a grown COLD ledger — whole-file read, JSON
        parse, per-row validation — can consume an entire 20s stop-guard
        deadline and reach fail-open despite the census budget. A guard whose
        own hot path can reproduce the class it exists to prevent has not
        prevented it.

        A budget hit is UNKNOWN, never a short row list: returning the rows
        validated so far would hand an authority answer built from an
        unfinished check.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()

        # CONTROL, on the same call with no budget: it answers.
        unbounded, err = gateimport._binding_rows()
        self.assertEqual(err, None)
        self.assertEqual(len(unbounded), 1)

        # Same call, already-expired budget: UNKNOWN, and the module's own
        # timeout sentinel rather than a bespoke string.
        gateimport._BINDING_ROWS_MEMO.clear()
        rows, err = gateimport._binding_rows(deadline=time.monotonic() - 1)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "a cold read on an expired budget returned rows instead of "
            "UNKNOWN, so the caller's deadline bounds nothing")

    def test_a_deadline_that_expires_DURING_the_read_yields_unknown(self):
        """AN ALREADY-EXPIRED DEADLINE IS THE EASY CASE, and it was the only
        one the first arm covered. A probe made the read CROSS the
        deadline mid-call and it returned rows with err=None.

        THE CLOCK ADVANCES ON EVERY READING, so expiry lands wherever the code
        actually looks — no function under test is patched, and this arm does
        not encode which helper does the reading. The version before this one
        stubbed `checked_rows`, and when the reader moved to an incremental
        path that no longer calls it, the arm passed a stale mechanism and
        went red for a reason unrelated to its subject.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))

        # CONTROL: a live budget nothing consumes answers normally.
        gateimport._BINDING_ROWS_MEMO.clear()
        rows, err = gateimport._binding_rows(deadline=time.monotonic() + 300)
        self.assertEqual((err, len(rows or [])), (None, 1),
                         "MUST-HIT: a live budget did not answer, so the "
                         "expiry below proves nothing")

        gateimport._BINDING_ROWS_MEMO.clear()
        clock = [0.0]

        def ticking():
            clock[0] += 1.0            # every look at the clock moves it
            return clock[0]

        with mock.patch.object(gateimport.eventledger, "time",
                               types.SimpleNamespace(monotonic=ticking)):
            rows, err = gateimport._binding_rows(deadline=3.0)

        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "a read that CROSSED its deadline returned rows instead of "
            "UNKNOWN — partial rows presented as an authority answer")

    @staticmethod
    def _binding_bytes(receipt):
        """One VALID canonical binding row, as the ledger stores it.

        Every varying part is fixed-width -- the receipt id is the caller's
        four characters and the content id is 64 hex -- so two rows built
        here always serialise to the SAME NUMBER OF BYTES. That is what lets
        a rewrite move `ctime` and nothing else, which is the only condition
        under which the key under test is the thing being measured.
        """
        row = {"v": gateimport.BINDING_VERSION,
               "event": gateimport.BINDING_EVENT,
               "ts": "2026-01-01T00:00:00Z",
               "receipt": receipt,
               "importing_repo": "/",
               "origin_repo": "/",
               "head": "h" * 40,
               "tree": "t" * 40,
               "artifact": {"path": "/a", "sha256": "0" * 64, "bytes": 1}}
        row["id"] = gateimport._binding_id(row)
        return (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")

    def test_a_same_size_rewrite_with_a_restored_mtime_moves_the_key(self):
        """THE REWRITE mtime CANNOT SEE, AND ctime CAN.

        (dev, ino, size, mtime_ns, mode, nlink) is IDENTICAL before and after
        a rewrite that puts back the same number of bytes and then calls
        `os.utime` to restore the old mtime -- which is not an exotic attack,
        it is what any tool that preserves timestamps does. A memo keyed on
        that tuple then serves the OLD rows for the NEW file, permanently,
        because no later append can dislodge a key that is legitimately the
        old file's.

        `st_ctime_ns` is the inode-change time: the kernel sets it on every
        write and no userspace call restores it -- `utime` moves mtime and
        atime only -- so it is the one field that makes this visible without
        turning a probe whose entire purpose is NOT READING into one that
        reads the file every time.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "bindings.jsonl")
        before_bytes = b'{"receipt": "aaaa", "importing_repo": "/a"}\n'
        after_bytes = b'{"receipt": "bbbb", "importing_repo": "/b"}\n'
        self.assertEqual(len(before_bytes), len(after_bytes),
                         "MUST-HIT: the fixture must rewrite the SAME NUMBER "
                         "OF BYTES or `size` alone would move the key and "
                         "this arm would prove nothing about ctime")

        with open(path, "wb") as fh:
            fh.write(before_bytes)
        st = os.stat(path)
        first = eventledger.ledger_identity(path)
        self.assertIsNotNone(first, "the fixture ledger was not readable")

        with open(path, "wb") as fh:
            fh.write(after_bytes)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))   # put mtime back
        second = eventledger.ledger_identity(path)
        self.assertIsNotNone(second)

        # MUST-HIT ON THE FIXTURE: the rewrite really did restore mtime and
        # keep the size, so the two keys differ ONLY where ctime does.
        after_st = os.stat(path)
        self.assertEqual(
            (after_st.st_size, after_st.st_mtime_ns),
            (st.st_size, st.st_mtime_ns),
            "MUST-HIT: the rewrite moved size or mtime, so a key WITHOUT "
            "ctime would also have moved and this arm cannot see the cure")
        self.assertNotEqual(
            first, second,
            "a same-size rewrite with a restored mtime produced the SAME "
            "identity, so a memo keyed on it serves the old rows for the new "
            "file and no later append dislodges them")

    def test_the_warm_memo_serves_the_new_rows_after_that_rewrite(self):
        """THE KEY MOVING IS THE MECHANISM; THE MEMO NOT LYING IS THE POINT.

        The arm above proves `ledger_identity` produces a different tuple
        after a same-size, restored-mtime rewrite. That is a property of the
        KEY, and a key that moves buys nothing if the memo is consulted with
        a different key, keyed on a path, or bypassed. This arm warms the
        REAL memo through the REAL consumer, rewrites, and asks the consumer
        again: the new content must come back.

        The warmth control is not decoration. Without it this passes against
        a `_binding_rows` that memoises nothing at all -- a cold read every
        time also returns the new rows -- so the whole arm would be a test
        that reading a file twice reads it twice.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "bindings.jsonl")
        before = self._binding_bytes("aaaa")
        after = self._binding_bytes("bbbb")
        self.assertEqual(
            len(before), len(after),
            "MUST-HIT: the two fixtures differ in length, so `size` alone "
            "moves the key and this arm cannot see the ctime cure")

        with open(path, "wb") as fh:
            fh.write(before)
        st = os.stat(path)

        gateimport._BINDING_ROWS_MEMO.clear()
        self.addCleanup(gateimport._BINDING_ROWS_MEMO.clear)
        with mock.patch.object(gateimport, "bindings_path",
                               lambda: path):
            rows, err = gateimport._binding_rows()
            self.assertIsNone(err, "the fixture ledger did not validate: %r"
                              % (err,))
            self.assertEqual([r["receipt"] for r in rows], ["aaaa"])

            # THE WARMTH CONTROL. The memo must actually be holding this
            # answer, or the assertion after the rewrite proves nothing.
            self.assertTrue(
                gateimport._BINDING_ROWS_MEMO,
                "nothing was memoised, so the rewrite below is measured "
                "against a reader that had no stale answer to serve")
            warm, _ = gateimport._binding_rows()
            self.assertIs(
                warm, rows,
                "the second call re-read instead of serving the memo, so "
                "this arm cannot distinguish an invalidated memo from an "
                "absent one")

            with open(path, "wb") as fh:
                fh.write(after)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            fresh_st = os.stat(path)
            self.assertEqual(
                (fresh_st.st_size, fresh_st.st_mtime_ns),
                (st.st_size, st.st_mtime_ns),
                "MUST-HIT: the rewrite moved size or mtime, so a key "
                "WITHOUT ctime would have moved too")

            served, err2 = gateimport._binding_rows()
        self.assertIsNone(err2)
        self.assertEqual(
            [r["receipt"] for r in served], ["bbbb"],
            "the memo served the OLD rows for the NEW file, which is the "
            "permanent-staleness shape the ctime key exists to prevent")

    def test_an_absent_ledger_past_the_deadline_is_a_timeout(self):
        """AN ABSENT LEDGER IS THE MOST CONFIDENT ANSWER THE READ CAN GIVE.

        `([], None, None)` says "read successfully, there are no events",
        and every deadline check in the read sits BEFORE the open. A read
        that crosses its budget and then finds no file returned that triple
        -- an authoritative "no bindings exist" produced after the call lost
        the right to answer, indistinguishable from the same triple produced
        in time. The guard above it then reports NO CANONICAL BINDING on a
        question it never actually asked.

        Expiry outranks absence, and it must do so at a door that a future
        return cannot walk around.
        """
        crossed = [False]

        def prepare_that_burns_the_budget(path):
            crossed[0] = True
            raise FileNotFoundError(path)

        with mock.patch.object(eventledger, "_prepare",
                               prepare_that_burns_the_budget), \
                mock.patch.object(eventledger, "_past",
                                  lambda d: crossed[0] and d is not None):
            with self.assertRaises(projscope.Expired):
                eventledger.checked_events_with_identity(
                    "/nonexistent/bindings.jsonl", deadline=1.0)

        # MUST-HIT: the SAME absent ledger inside its budget is still the
        # ordinary empty answer, so the raise above is the deadline and not
        # a reader that started refusing missing files.
        with mock.patch.object(eventledger, "_prepare",
                               prepare_that_burns_the_budget), \
                mock.patch.object(eventledger, "_past", lambda d: False):
            result = eventledger.checked_events_with_identity(
                "/nonexistent/bindings.jsonl", deadline=1.0)
        self.assertEqual(result, ([], None, None))

    def test_an_ambient_expiry_is_not_folded_into_an_unreadable_ledger(self):
        """WHAT THE EXPLICIT RE-RAISE BUYS THAT THE ONE DOOR CANNOT.

        THE DOOR ALONE COVERS EVERY EXPIRY THAT CAME FROM `deadline`. It
        discards any answer produced past that time, so an `Expired` raised
        by the read's own checks needs no special handling: fold it to a
        string in the broad handler and the door raises `Expired` regardless,
        because the deadline has in fact passed. Against those cases the
        clause is unobservable, which is the property this arm exists to
        bound rather than to trust.

        THE CASE THAT IS NOT REDUNDANT is an expiry that did not come from
        this call's deadline. `projscope` raises `Expired` from an ENCLOSING
        ambient budget, and a caller passing `deadline=None` has no local
        deadline for the door to test -- `_past(None)` is False. Without the
        clause, a genuine budget signal from the scope above becomes
        `(None, "<message>", None)`: the reader reports the LEDGER as
        unreadable, naming a timeout as a fact about the file, and the guard
        states it rather than going UNKNOWN.
        """
        def read_that_hits_the_ambient_budget(path):
            raise projscope.Expired("projection budget exhausted")

        with mock.patch.object(eventledger, "_prepare",
                               read_that_hits_the_ambient_budget):
            with self.assertRaises(projscope.Expired):
                eventledger.checked_events_with_identity(
                    "/nonexistent/bindings.jsonl", deadline=None)

        # MUST-HIT: an ordinary OSError on the same path with the same
        # absent deadline IS folded to an unreadable reason, so the raise
        # above is the type and not the reader refusing everything.
        def read_that_fails_for_real(path):
            raise OSError("ledger is not a private regular file")

        with mock.patch.object(eventledger, "_prepare",
                               read_that_fails_for_real):
            rows, unavailable, identity = \
                eventledger.checked_events_with_identity(
                    "/nonexistent/bindings.jsonl", deadline=None)
        self.assertEqual(rows, None)
        self.assertIn("not a private regular file", unavailable)
        self.assertIsNone(identity)

    def test_an_expired_read_reaches_gateimport_as_a_timeout(self):
        """THE RE-RAISE IS ONLY WORTH ANYTHING AT ITS CONSUMER.

        `checked_events_with_identity` raising `Expired` matters because
        `_binding_rows` classifies it as the census timeout instead of an
        unreadable ledger. Those two verdicts diverge at the guard: a
        timeout is UNKNOWN, an unreadable ledger is a stated fact about the
        bindings. This arm crosses the whole seam rather than either half.
        """
        with mock.patch.object(
                eventledger, "checked_events_with_identity",
                side_effect=projscope.Expired("ledger read deadline")):
            rows, err = gateimport._binding_rows(deadline=None)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "an Expired from the read arrived at the guard as something "
            "other than the census timeout, so a budget overrun is reported "
            "as a fact about the ledger")

        # MUST-HIT: an ordinary unreadable ledger must NOT be the timeout,
        # or the assertion above passes against a consumer that calls
        # everything a timeout.
        with mock.patch.object(
                eventledger, "checked_events_with_identity",
                return_value=(None, "ledger is not a private regular file",
                              None)):
            rows2, err2 = gateimport._binding_rows(deadline=None)
        self.assertIsNone(rows2)
        self.assertNotEqual(err2, gateimport._CENSUS_TIMEOUT)
        self.assertIn("not a private regular file", err2)

    def test_the_first_row_is_bounded_like_every_other(self):
        """SAMPLING EVERY 32nd ROW LEAVES 31 OF EVERY 32 UNBOUNDED, AND ROW
        ZERO WAS WORSE THAN SLOW.

        `if index and not index % 32 and _expired(...)` is FALSE at index 0,
        so a budget already spent before the loop began reached the validator
        anyway and returned its answer -- MALFORMED LEDGER -- for a read that
        had simply run out of time. A confident corruption verdict minted by a
        clock is not a slower bound, it is a wrong one.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()

        with mock.patch.object(gateimport, "_binding_err",
                               return_value="row 0 is malformed"):
            # MUST-HIT FIRST: with a LIVE budget these bindings really do
            # reach the validator and really are reported malformed, so the
            # timeout below is the deadline's doing and not the fixture's.
            live_rows, live_err = gateimport._binding_rows(
                deadline=time.monotonic() + 300)
            self.assertEqual(
                (live_rows, "malformed" in (live_err or "")), (None, True),
                "MUST-HIT: the validator was never reached on a live budget, "
                "so this arm cannot tell a timeout from a clean read (%r)"
                % (live_err,))

            gateimport._BINDING_ROWS_MEMO.clear()
            rows, err = gateimport._binding_rows(
                deadline=time.monotonic() - 1.0)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "an expired budget reported the ledger MALFORMED on its FIRST "
            "row, which is a corruption verdict minted by a clock (%r)"
            % (err,))

    def test_a_reason_that_reads_like_a_timeout_is_still_unreadable(self):
        """THE TIMEOUT IS A TYPE, SO NO SENTENCE CAN WEAR IT.

        A substring test matched any message containing the words; a prefix
        test matched any message beginning with them. Both let an unrelated
        failure -- an OS error, a path, a nested reader's reason -- be
        classified as THIS read's timeout and routed into the caller's UNKNOWN
        branch, which is a fail-open on a question nobody asked.
        `eventledger` now RAISES `projscope.Expired`, so the only way to be
        this timeout is to BE one.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))

        # The exact text the old predicates keyed on, in both the shapes they
        # would have accepted: EQUAL to it, and BEGINNING with it.
        for impostor in ("ledger read deadline exceeded",
                         "ledger read deadline exceeded at line 3",
                         "cannot open /srv/deadline exceeded/bindings.jsonl"):
            gateimport._BINDING_ROWS_MEMO.clear()
            with mock.patch.object(gateimport.eventledger,
                                   "checked_events_with_identity",
                                   return_value=(None, impostor, None)):
                rows, err = gateimport._binding_rows(
                    deadline=time.monotonic() + 300)
            self.assertIsNone(rows)
            self.assertNotEqual(
                err, gateimport._CENSUS_TIMEOUT,
                "an unreadable ledger was reported as this read's TIMEOUT "
                "because its message reads like one: %r" % impostor)
            self.assertIn(impostor, err or "",
                          "the real failure was not carried to the caller "
                          "(%r -> %r)" % (impostor, err))

        # MUST-HIT: a REAL expiry -- the reader raising -- still becomes the
        # census timeout, or the assertions above are satisfied by a consumer
        # that has stopped recognising timeouts at all.
        gateimport._BINDING_ROWS_MEMO.clear()
        with mock.patch.object(
                gateimport.eventledger, "checked_events_with_identity",
                side_effect=projscope.Expired("ledger read deadline exceeded")):
            rows, err = gateimport._binding_rows(
                deadline=time.monotonic() + 300)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "MUST-HIT: a genuine reader expiry was not recognised, so this "
            "arm proves nothing about the impostors above")

    def test_validation_that_crosses_expiry_is_unknown_not_malformed(self):
        """THE CROSSING HAPPENS INSIDE THE WORK, NOT BEFORE IT.

        A check in front of each return cannot close this: the budget expires
        DURING a validation, and the error that validation produced is then
        interpreted and returned before anything re-reads the deadline. The
        loop therefore DECIDES NOTHING -- it retains the first error and
        leaves -- and one mandatory expiry check runs before that error is
        interpreted, so a timeout outranks corruption by control flow.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()
        # THE DEADLINE MUST BE LIVE FOR THE READER AND CROSSED BY THE
        # VALIDATION. `eventledger` reads its own clock, so a synthetic
        # instant would make the READ expire before the validation ever runs
        # and this arm would be about the wrong boundary. The deadline is a
        # real future instant; only gateimport's view of the clock is faked,
        # and it starts BELOW the deadline.
        deadline = time.monotonic() + 300.0
        clock = [deadline - 1.0]

        def slow_and_malformed(_row):
            clock[0] = deadline + 1.0      # the validation itself crosses it
            return "row is malformed"

        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
        with mock.patch.object(gateimport, "time", fake_time), \
                mock.patch.object(gateimport, "_binding_err",
                                  slow_and_malformed):
            rows, err = gateimport._binding_rows(deadline=deadline)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "a validation that CROSSED the deadline returned its malformed "
            "verdict, so a clock minted a corruption answer (%r)" % (err,))

        # MUST-HIT: the same malformed row with a budget nothing crosses is
        # still reported MALFORMED, or the assertion above is satisfied by a
        # function that answers timeout to everything.
        gateimport._BINDING_ROWS_MEMO.clear()
        with mock.patch.object(gateimport, "_binding_err",
                               return_value="row is malformed"):
            rows2, err2 = gateimport._binding_rows(
                deadline=time.monotonic() + 300)
        self.assertIsNone(rows2)
        self.assertIn(
            "malformed", err2 or "",
            "MUST-HIT: a genuinely malformed ledger was not reported "
            "malformed, so the timeout above says nothing (%r)" % (err2,))

    def test_a_warm_memo_does_not_answer_past_the_deadline(self):
        """CACHE WARMTH MUST NOT DECIDE WHETHER A BUDGET WAS RESPECTED.

        A memo hit is cheap, which is exactly why answering from it past the
        deadline is the dangerous shape: the call succeeds, the caller reads
        that as "the budget held", and spends the rest of an expired budget on
        work this answer authorised. It also makes the ANSWER DEPEND ON WHO
        RAN FIRST -- the identical call returns rows on a hot process and
        UNKNOWN on a cold one -- so the guard's verdict stops being a function
        of the ledger.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()
        # WARM IT, and prove it is warm on the same observable the expired
        # call will use: a second live read must come back with rows.
        warm_rows, warm_err = gateimport._binding_rows(
            deadline=time.monotonic() + 300)
        self.assertEqual((warm_err, len(warm_rows or [])), (None, 1),
                         "MUST-HIT: the memo was never populated, so the "
                         "expired call below is exercising the COLD path and "
                         "proves nothing about warmth")
        again_rows, again_err = gateimport._binding_rows(
            deadline=time.monotonic() + 300)
        self.assertEqual((again_err, len(again_rows or [])), (None, 1),
                         "MUST-HIT: the second live read did not answer, so "
                         "the memo is not serving anything")

        rows, err = gateimport._binding_rows(deadline=time.monotonic() - 1.0)
        self.assertEqual(
            (rows, err), (None, gateimport._CENSUS_TIMEOUT),
            "a WARM memo answered past an expired deadline, so the same call "
            "decides differently depending on which process ran first")

    def test_a_legacy_timeout_keeps_the_ids_the_canonical_read_proved(self):
        """THE PROPERTY, AT THE SITE WHERE IT IS ACHIEVABLE.

        `binding_candidate_ids` promises that a census timeout leaves LEGACY
        placement unknown without erasing ids the canonical read already
        proved. That promise has a location: it holds only AFTER the canonical
        bindings have been read and `candidates` populated. This arm times out
        the LEGACY read and asserts the canonical ids survive with a warning
        and no fatal error.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()

        def timed_out(deadline=None):
            return None, gateimport._CENSUS_TIMEOUT

        with mock.patch.object(gateimport, "_legacy_import_rows", timed_out):
            ids, fatal, warning = gateimport.binding_candidate_ids(
                self.repo, budget_s=5.0)
        self.assertIsNone(fatal,
                          "a LEGACY read that ran out of budget was reported "
                          "as fatal, which erases ids the canonical read "
                          "already proved")
        self.assertTrue(warning, "the timeout was not disclosed at all")
        self.assertTrue(
            ids,
            "MUST-HIT: the canonical read proved no ids, so this arm cannot "
            "tell a preserved id from an erased one (%r)" % (ids,))

    def test_a_canonical_read_timeout_proves_nothing_and_says_so(self):
        """AN EMPTY PROVEN SET WITH NO ERROR IS AN ABSENCE CLAIM.

        The canonical read is what POPULATES `candidates`, so a timeout there
        leaves it empty for the reason that nothing was read — not because no
        receipt qualified. Routing that into the legacy-census answer returns
        an empty id set, NO fatal error, and a warning stating that canonical
        bindings remain valid and only legacy placement is unknown: every part
        of which is false at that point, and a caller cannot tell it from a
        completed read that found nothing.

        The contract this preserves is unchanged -- a timeout must not erase
        ALREADY-PROVEN ids -- because at this site none have been proven.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()

        def timed_out(deadline=None):
            return None, gateimport._CENSUS_TIMEOUT

        with mock.patch.object(gateimport, "_binding_rows", timed_out):
            ids, fatal, warning = gateimport.binding_candidate_ids(
                self.repo, budget_s=5.0)
        self.assertEqual(
            frozenset(ids), frozenset(),
            "a read that never happened returned ids (%r)" % (ids,))
        self.assertTrue(
            fatal,
            "a canonical read that never happened answered with NO error, so "
            "its empty id set is indistinguishable from a completed read that "
            "found nothing")
        self.assertIn(
            "no receipt id is proven", fatal,
            "the reason does not say that nothing was decided: %r" % fatal)
        self.assertIsNone(
            warning,
            "the legacy warning was emitted for a canonical-read failure, "
            "which points the reader at the wrong subsystem: %r" % warning)
        # MUST-HIT: the same call WITHOUT the timeout proves an id, so the
        # empty set above is the timeout's doing and not the fixture's.
        gateimport._BINDING_ROWS_MEMO.clear()
        live_ids, live_fatal, _w = gateimport.binding_candidate_ids(
            self.repo, budget_s=5.0)
        self.assertIsNone(live_fatal, "the control run itself failed: %r"
                          % live_fatal)
        self.assertTrue(
            live_ids,
            "MUST-HIT: this repository proves no ids even with a live budget, "
            "so the emptiness above says nothing about the timeout")

    def test_the_memo_key_describes_the_bytes_that_were_parsed(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is INSIDE the single assertion (ident_a is not None, and rows_a/rows_b counts of 1 and 2 prove both reads returned real content); the claim is that two keys DIFFER, which has no positive form on one observable, and the classifier cannot credit a control that shares an assertEqual tuple with the difference it controls
        """A->B->A DEFEATS AN IDENTITY TAKEN AROUND A SEPARATE READ.

        On the first cut it stat-ed, validated, then stat-ed again —
        three descriptors, so a path replaced A -> B -> A validates B's rows
        and files them under A's identity, and no later append dislodges them
        because A's key is legitimately A's. Identity and content now come
        from ONE descriptor, so the key cannot describe a file the rows did
        not come from.

        This arm drives the seam directly: the same descriptor must yield the
        identity that the rows are memoised under, and swapping the file's
        CONTENT must therefore move the key.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        rows_a, err_a, ident_a = eventledger.checked_events_with_identity(
            gateimport.bindings_path(), strict=True)
        self.assertEqual(err_a, None)

        # B: same path, different bytes, read through the same door.
        path = gateimport.bindings_path()
        with open(path, "r+") as fh:
            body = fh.read()
        with open(path, "w") as fh:
            fh.write(body + body)
        rows_b, err_b, ident_b = eventledger.checked_events_with_identity(
            path, strict=True)

        # THE POSITIVE CONTROL RIDES IN THE SAME TUPLE: an identity function
        # that always returned None would satisfy "the keys differ" while
        # proving the opposite, so a live key for A is asserted beside it.
        self.assertEqual(
            (err_b, len(rows_a), len(rows_b),
             ident_a is not None, ident_a == ident_b),
            (None, 1, 2, True, False),
            "expected a live key for A and a DIFFERENT key once the content "
            "changed; got a=%r b=%r" % (ident_a, ident_b))

    def test_reading_bindings_twice_validates_them_once(self):
        """THE MEMO EXISTS BECAUSE THIS READ IS PER-CANDIDATE, NOT PER-GUARD.

        `green_receipts` asks `repository_authorization` once per candidate
        receipt, so an unmemoised read re-validates every binding row once per
        candidate: on a few hundred candidates over a few hundred rows that is
        tens of thousands of validations and a large fraction of a
        five-second budget spent recomputing an answer that has not changed.
        The cost scales with the PRODUCT of two growing numbers, which is why
        it is a memo rather than a faster validator.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        gateimport._BINDING_ROWS_MEMO.clear()
        # SPY ON THE DOOR THE CODE ACTUALLY USES. This watched
        # `checked_events` and reported ZERO reads after `_binding_rows` moved
        # to the single-descriptor helper — the arm failed loudly, which is
        # what it is for, but a `<= 1` assertion here would have passed while
        # measuring nothing.
        real = gateimport.eventledger.checked_events_with_identity
        reads = []

        def counting(path, **kw):
            reads.append(path)
            return real(path, **kw)

        with mock.patch.object(gateimport.eventledger,
                               "checked_events_with_identity", counting):
            first, err1 = gateimport._binding_rows()
            second, err2 = gateimport._binding_rows()
        self.assertEqual((err1, err2), (None, None))
        self.assertEqual(first, second)
        self.assertTrue(first, "MUST-HIT: no bindings to read, so a count of "
                               "one proves nothing about memoisation")
        self.assertEqual(len(reads), 1,
                         "the second read re-validated the whole ledger")

    def test_an_append_is_seen_by_the_very_next_read(self):
        """THE INVALIDATION, WHICH IS THE HALF THAT CAN ROT SILENTLY.

        One of this function's three callers appends a binding under the
        ledger lock and the next reader must see it, so the obvious precedent
        — a process-lifetime cache like `_dispatch_snapshot`'s — would be
        WRONG here. The key is the file's (dev, ino, size, mtime_ns), which an
        append moves by construction rather than by anyone remembering to
        clear anything. This arm is what proves that, and it fails LOUDLY if
        someone later swaps the key for a cheaper one.
        """
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        before, err = gateimport._binding_rows()
        self.assertEqual(err, None)
        self.assertEqual(len(before), 1)

        # A SECOND REPOSITORY IMPORTS THE SAME RECEIPT: a real append through
        # the real door, not a hand-written line, so the arm exercises the
        # caller that actually mutates the file mid-invocation.
        other = os.path.join(self.tmp, "other")
        _git(self.tmp, "clone", "-q", self.repo, other)
        self.assertEqual(gateimport.import_receipt(
            self._artifact(row), other)[1:], ("imported", None))

        after, err = gateimport._binding_rows()
        self.assertEqual(err, None)
        self.assertEqual(len(after), 2,
                         "a read after an append returned the pre-append "
                         "answer — the memo outlived the file it describes")

    def test_one_global_receipt_gets_one_binding_per_importing_repository(self):
        row, artifact = self._row(), None
        artifact = self._artifact(row)
        self.assertEqual(self._import(artifact)[1:], ("imported", None))
        other = os.path.join(self.tmp, "other")
        _git(self.tmp, "clone", "-q", self.repo, other)
        got, verdict, err = gateimport.import_receipt(artifact, other)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertEqual(len(self._ledger()), 1,
                         "a second placement duplicated the global receipt")
        bindings = self._bindings()
        self.assertEqual(len(bindings), 2)
        self.assertEqual({b["importing_repo"] for b in bindings},
                         {self._repo_identity(), self._repo_identity(other)})
        self.assertEqual(len(self._provenance()), 2)

    def test_ambient_git_selection_cannot_redirect_tree_or_repository_identity(self):
        row = self._row()
        expected = self._repo_identity()
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign)
        _git(foreign, "init", "-q", ".")
        with open(os.path.join(foreign, "other"), "w") as f:
            f.write("foreign\n")
        _git(foreign, "add", "other")
        _git(foreign, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "foreign")
        os.environ["GIT_DIR"] = os.path.join(foreign, ".git")
        os.environ["GIT_WORK_TREE"] = foreign
        _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(self._bindings()[0]["importing_repo"], expected)

    def test_replacement_ref_environment_cannot_falsify_receipt_placement(self):  # noqa: VACUOUS_ASSERTION — ambient git positively resolves the hostile replacement; the importer refuses its forged tree, then the same scrubbed production path imports the genuine row and fills the ledger
        row = self._row()
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("replacement\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "replacement")
        replacement = _git(self.repo, "rev-parse", "HEAD")
        replacement_tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        _git(self.repo, "update-ref", "refs/hostile/%s" % row["head"],
             replacement)
        os.environ["GIT_REPLACE_REF_BASE"] = "refs/hostile"
        self.assertEqual(_git(self.repo, "rev-parse", row["head"] + "^{tree}"),
                         replacement_tree,
                         "control: ambient Git must see the hostile replacement")
        forged = dict(row, tree=replacement_tree, tree_after=replacement_tree)
        forged["id"] = gate._receipt_id(forged)
        got, verdict, err = self._import(self._artifact(forged))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("tree mismatch", err)
        self.assertEqual(self._ledger(), [])

        got, verdict, err = self._import(self._artifact(row))
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None),
                         "the same scrubbed production seam must admit the real tree")
        self.assertEqual(self._ledger()[0]["tree"], row["tree"])

    def test_binding_uses_common_dir_identity_and_survives_worktree_reaping(self):
        row = self._row()
        room = os.path.join(self.tmp, "room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        _, verdict, err = gateimport.import_receipt(self._artifact(row), room)
        self.assertEqual((verdict, err), ("imported", None))
        shutil.rmtree(room)
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(err)
        self.assertEqual(binding["importing_repo"], self._repo_identity())

    def test_reaped_worktree_path_refuses_before_receipt_or_binding_append(self):
        row = self._row()
        artifact = self._artifact(row)
        room = os.path.join(self.tmp, "reaped-before-import")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        _git(self.repo, "worktree", "remove", "--force", room)

        got, verdict, err = gateimport.import_receipt(artifact, room)
        self.assertEqual((got, verdict), (None, None))
        self.assertEqual(err, "importing repository identity is unreadable")
        self.assertEqual(self._ledger(), [],
                         "a dead importing identity must not leave a global receipt")
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._provenance(), [])

        got, verdict, err = gateimport.import_receipt(artifact, self.repo)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None),
                         "a surviving checkout of the same repository is the retry door")
        self.assertEqual(len(self._ledger()), 1)
        self.assertEqual(self._bindings()[0]["importing_repo"],
                         self._repo_identity())

    def test_hand_shaped_audit_pointer_without_reverified_artifact_authorizes_nothing(self):
        row = self._row()
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        pointer = {"event": "gate-import", "ts": pk.now_ts(),
                   "receipt": row["id"], "origin_repo": row["repo_id"],
                   "repo": self.repo,
                   "artifact": os.path.join(self.tmp, "fabricated.jsonl")}
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("historical backfill unavailable", err)
        self.assertEqual(self._bindings(), [])

    def test_new_hand_shaped_pointer_cannot_migrate_into_authority(self):  # noqa: VACUOUS_ASSERTION — the post-cutoff pointer leaves bindings empty; changing only its timestamp to pre-cutoff makes canonical_binding migrate it and positively fills that same ledger
        row, artifact = self._row(), None
        artifact = self._artifact(row)
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        pointer = {"event": "gate-import", "ts": "2026-08-27T15:23:07Z",
                   "receipt": row["id"], "repo": self.repo,
                   "artifact": artifact}
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("historical backfill unavailable", err)
        self.assertEqual(self._bindings(), [])

        os.unlink(gateimport.imports_path())
        pointer["ts"] = "2026-08-26T00:00:00Z"
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertEqual(binding["receipt"], row["id"], err)
        self.assertEqual(self._bindings()[0]["receipt"], row["id"],
                         "pre-cutoff control must exercise historical migration")

    def test_hand_shaped_v6_pointer_cannot_reconstruct_live_route_custody(self):
        row = self._row(v=6, host={"node": "n", "system": "Linux",
                                           "release": "r", "id": "i"},
                        focus={})
        artifact = self._artifact(row)
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        pointer = {"event": "gate-import", "ts": pk.now_ts(),
                   "receipt": row["id"], "repo": self.repo,
                   "artifact": artifact, "transport": "helm-gateroute-v1",
                   "origin_run": "claimed", "origin_node": "n"}
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("focused receipt custody", err)
        self.assertIn("UNKNOWN", err)
        self.assertEqual(self._bindings(), [])

    def test_live_routed_v6_writes_a_binding_the_accessor_can_read(self):
        row = self._row(v=6, host={"node": "n", "system": "Linux",
                                           "release": "r", "id": "i"},
                        focus={})
        custody = {"receipt": row, "head": row["head"], "tree": row["tree"],
                   "node": "n", "challenge": "live-route"}
        got, verdict, err = gateimport.import_routed_focus(
            self._artifact(row), self.repo, custody)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(err)
        self.assertEqual(binding["receipt"], row["id"])

    def test_v8_historical_pointer_requires_the_complete_artifact_object(self):  # noqa: VACUOUS_ASSERTION — _load_rows positively proves the hostile artifact is readable and missing only chunk zero; replacing it with the complete object makes migration fill the binding ledger
        row, chunks = self._v8()
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for event in chunks + [row]:
                fh.write(json.dumps(event) + "\n")
        artifact = self._artifact(*(chunks[1:] + [row]))
        loaded, _identity, load_err = gateimport._load_rows(artifact)
        self.assertIsNone(load_err)
        self.assertEqual([event["id"] for event in loaded],
                         [event["id"] for event in chunks[1:] + [row]],
                         "control: the hostile artifact is readable but omits chunk zero")
        pointer = {"event": "gate-import",
                   "ts": "2026-08-26T00:00:00Z",
                   "receipt": row["id"], "repo": self.repo,
                   "artifact": artifact}
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("failure record is incomplete", err)
        self.assertEqual(self._bindings(), [])

        os.unlink(gateimport.imports_path())
        pointer["artifact"] = self._artifact(*(chunks + [row]))
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertEqual(binding["receipt"], row["id"], err)
        self.assertEqual(self._bindings()[0]["receipt"], row["id"],
                         "complete-object control must exercise v8 migration")

    def test_historical_pointer_backfills_only_after_current_reverification(self):
        row, artifact = self._row(), None
        artifact = self._artifact(row)
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        pointer = {"event": "gate-import",
                   "ts": "2026-08-26T00:00:00Z",
                   "receipt": row["id"], "repo": self.repo,
                   "artifact": artifact}
        self.assertTrue(eventledger.append(gateimport.imports_path(), pointer))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(err)
        self.assertEqual(binding["receipt"], row["id"])
        os.unlink(artifact)
        rebound, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(err)
        self.assertEqual(rebound["id"], binding["id"])

    def test_unreadable_binding_ledger_is_unknown_not_absent(self):
        row = self._row()
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        os.makedirs(os.path.dirname(gateimport.bindings_path()), exist_ok=True)
        os.mkdir(gateimport.bindings_path())
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("bindings unavailable", err)

    def test_binding_lock_failure_is_fail_closed(self):
        row, real = self._row(), eventledger.locked

        @contextlib.contextmanager
        def locked(path):
            if path == gateimport.bindings_path():
                yield False
            else:
                with real(path) as held:
                    yield held

        with mock.patch.object(gateimport.eventledger, "locked", locked):
            got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("binding lock unavailable", err)
        self.assertEqual(self._ledger()[0]["id"], row["id"])
        self.assertEqual(self._bindings(), [])
        self.assertEqual(self._provenance(), [])

    def test_accessor_holds_receipt_lock_through_binding_decision(self):  # noqa: VACUOUS_ASSERTION — observed=[True] is the must-hit control proving the real binding reader executed exactly once while the receipt lock was held, and returned the durable binding
        row = self._row()
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        real_locked = eventledger.locked
        real_rows = gateimport._binding_rows
        held = {"receipt": False}
        observed = []

        @contextlib.contextmanager
        def locked(path):
            with real_locked(path) as acquired:
                if path == gate.receipts_path():
                    held["receipt"] = acquired
                try:
                    yield acquired
                finally:
                    if path == gate.receipts_path():
                        held["receipt"] = False

        def binding_rows():
            observed.append(held["receipt"])
            self.assertTrue(held["receipt"],
                            "binding decision escaped the receipt snapshot lock")
            return real_rows()

        with mock.patch.object(gateimport.eventledger, "locked", locked), \
                mock.patch.object(gateimport, "_binding_rows",
                                  side_effect=binding_rows):
            binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertEqual(observed, [True],
                         "the binding reader must execute once inside the receipt lock")
        self.assertIsNone(err)
        self.assertEqual(binding["receipt"], row["id"])

    def test_accessor_revalidates_receipt_availability_and_current_placement(self):
        row = self._row()
        self._import(self._artifact(row))
        with mock.patch.object(gateimport, "_tree_of", return_value=None):
            binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("placement is UNKNOWN", err)
        os.unlink(gate.receipts_path())
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("authority UNKNOWN", err)

    def test_accessor_rejects_non_object_receipts_without_raising(self):  # noqa: VACUOUS_ASSERTION — the unconditional call_args assertion proves all three malformed values reached the real shape guard in order; each then returns the exact non-object refusal instead of raising on .get
        malformed = (None, [], "gate")
        real_shape = gateimport._receipt_shape_err
        with mock.patch.object(gateimport, "_receipt_shape_err",
                               side_effect=real_shape) as shape:
            for receipt in malformed:
                with self.subTest(receipt=receipt):
                    binding, err = gateimport.canonical_binding(receipt, self.repo)
                    self.assertIsNone(binding)
                    self.assertIn("not an object", err)
        self.assertEqual([call.args[0] for call in shape.call_args_list],
                         list(malformed),
                         "all malformed values must reach the production shape guard")

    def test_accessor_rejects_v8_when_a_bound_chunk_disappears(self):
        row, chunks = self._v8()
        artifact = self._artifact(*(chunks + [row]))
        self.assertEqual(self._import(artifact, want_id=row["id"])[1:],
                         ("imported", None))
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            for event in chunks[1:] + [row]:
                fh.write(json.dumps(event) + "\n")
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("failure record is incomplete", err)
        self.assertIn("authority UNKNOWN", err)

    def test_accessor_rejects_a_receipt_poisoned_after_binding(self):
        row = self._row(v=4, host={"node": "n", "system": "Linux",
                                   "release": "r", "id": "i"})
        self.assertEqual(self._import(self._artifact(row))[1:],
                         ("imported", None))
        poisoned = dict(row, detail="later conflicting content")
        self.assertTrue(eventledger.append(gate.receipts_path(), poisoned))
        binding, err = gateimport.canonical_binding(row, self.repo)
        self.assertIsNone(binding)
        self.assertIn("poisoned", err)
        self.assertIn("authority UNKNOWN", err)
        state, rid, why = gate.bind(
            gate.evidence_line(row), row["head"], repo_id=self.repo)
        self.assertEqual((state, rid), ("REFUSED", row["id"]))
        self.assertIn("authority UNKNOWN", why)


class ArtifactShapeTest(ImportBase):
    def test_a_multi_row_artifact_requires_id(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the same call with --id flips to ("imported", None)
        # rows must differ on a HASHED field or they share an id (label and
        # same-second ts are not bound — measured while building this arm).
        a, b = self._row(), self._row(ran=200)
        self.assertNotEqual(a["id"], b["id"])
        _, verdict, err = self._import(self._artifact(a, b))
        self.assertIsNone(verdict)
        self.assertIn("--id", err)
        # positive control: naming the row flips the same call to success.
        _, verdict, err = self._import(self._artifact(a, b), want_id=a["id"])
        self.assertEqual((verdict, err), ("imported", None))

    def test_id_selects_the_named_row_from_a_multi_row_artifact(self):
        a, b = self._row(), self._row(ran=200)
        _, verdict, err = self._import(self._artifact(a, b),
                                       want_id=b["id"])
        self.assertIsNone(err)
        self.assertEqual(verdict, "imported")
        self.assertEqual(self._ledger()[0]["ran"], 200)

    def test_a_repeated_id_inside_one_artifact_refuses_even_with_id(self):
        a = self._row()
        _, verdict, err = self._import(self._artifact(a, dict(a)),
                                       want_id=a["id"])
        self.assertIsNone(verdict)
        self.assertIn("appears 2 times", err)

    def test_artifact_growth_after_fstat_is_bounded_before_row_graph_growth(self):  # noqa: VACUOUS_ASSERTION — stale fstat plus the 64-byte refusal proves the streaming cap fired before writes; the same artifact under the real cap then imports and positively fills the binding ledger
        row = self._row()
        path = self._artifact(row)
        stale = mock.Mock(st_mode=gateimport.stat.S_IFREG,
                          st_nlink=1, st_size=0)
        with mock.patch.object(gateimport, "ARTIFACT_MAX_BYTES", 64), \
                mock.patch.object(gateimport.os, "fstat", return_value=stale):
            got, verdict, err = self._import(path)
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("64-byte import limit", err)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._bindings(), [])

        got, verdict, err = self._import(path)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None),
                         "the same artifact must import when the real bound applies")
        self.assertEqual(self._bindings()[0]["receipt"], row["id"])

    def test_a_torn_artifact_contributes_nothing(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: mending the tear flips the same artifact to ("imported", None) and the ledger [] -> 1
        row = self._row()
        path = self._artifact(row)
        with open(path, "a") as f:
            f.write('{"torn": tru')
        _, verdict, err = self._import(path)
        self.assertIsNone(verdict)
        self.assertIn("not JSON", err)
        self.assertEqual(self._ledger(), [])
        # positive control: mend the tear and the SAME artifact imports —
        # the emptiness above was the refusal, not a dead write path.
        with open(path, "w") as f:
            f.write(json.dumps(row) + "\n")
        _, verdict, err = self._import(path)
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)


class BindingAmbientBudgetTest(unittest.TestCase):
    def test_expiry_between_stored_receipt_rows_stops_the_fold(self):  # noqa: VACUOUS_ASSERTION — the unexpired control proves every planted row is observable
        seen = []

        class Tracked(dict):
            def get(self, key, default=None):
                if key == "event":
                    seen.append(dict.get(self, "id"))
                return dict.get(self, key, default)

        rows = [Tracked(event="gate", id=rid) for rid in ("a", "b", "c")]

        def spend(what):
            if what == "stored receipt row 1":
                raise projscope.Expired("planted")

        gateimport._STORED_IDS_CACHE[0] = None
        with mock.patch.object(gateimport, "_ledger_stamp", return_value=None), \
                mock.patch.object(gateimport.eventledger, "checked_events",
                                  return_value=(rows, None)), \
                mock.patch.object(gateimport.projscope, "spend_or_raise",
                                  side_effect=spend):
            with self.assertRaises(projscope.Expired):
                gateimport._stored_ids()
        self.assertEqual(seen, ["a"],
                         "stored receipt folding continued after expiry")

        seen.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        with mock.patch.object(gateimport, "_ledger_stamp", return_value=None), \
                mock.patch.object(gateimport.eventledger, "checked_events",
                                  return_value=(rows, None)):
            stored, poisoned, unavailable = gateimport._stored_ids()
        self.assertEqual((set(stored), poisoned, unavailable),
                         ({"a", "b", "c"}, set(), None))
        self.assertEqual(seen, ["a", "b", "c"])

    def test_expiry_between_binding_rows_stops_before_later_rows(self):  # noqa: VACUOUS_ASSERTION — seen=['a'] proves validation ran before expiry and exact equality excludes later rows
        rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        seen = []

        def validate(row):
            seen.append(row["id"])
            return None

        def spend(what):
            if what == "canonical binding row 1":
                raise projscope.Expired("planted")

        gateimport._BINDING_ROWS_MEMO.clear()
        with mock.patch.object(
                gateimport.eventledger, "checked_events_with_identity",
                return_value=(rows, None, None)), \
                mock.patch.object(gateimport, "_binding_err", side_effect=validate), \
                mock.patch.object(
                    gateimport.projscope, "spend_or_raise", side_effect=spend):
            with self.assertRaises(projscope.Expired):
                gateimport._binding_rows()
        self.assertEqual(seen, ["a"],
                         "binding validation continued after ambient expiry")

    def test_reader_expiry_distinguishes_ambient_from_local_deadline(self):
        gateimport._BINDING_ROWS_MEMO.clear()
        expired = projscope.Expired("planted reader expiry")
        with mock.patch.object(gateimport, "_bindings_identity",
                               return_value=None), \
             mock.patch.object(
                 gateimport.eventledger, "checked_events_with_identity",
                 side_effect=expired):
            rows, err = gateimport._binding_rows(
                deadline=time.monotonic() + 1)
            self.assertEqual((rows, err),
                             (None, gateimport._CENSUS_TIMEOUT))
            with projscope.scope(deadline=time.monotonic() + 1):
                with self.assertRaises(projscope.Expired):
                    gateimport._binding_rows()

    def test_expired_authorization_never_starts_repository_identity(self):  # noqa: VACUOUS_ASSERTION — Expired is the positive result; the hostile repository double fails if forbidden work starts
        with mock.patch.object(
                gateimport, "_repo_identity",
                side_effect=AssertionError("repository identity started")):
            with projscope.scope(deadline=0):
                with self.assertRaises(projscope.Expired):
                    gateimport.repository_authorization({}, "/repo")


if __name__ == "__main__":
    unittest.main()


class HeadDivergenceTest(ImportBase):
    """`gate import` says when the receipt is not about the commit you would
    land — and it asks the BINDER'S question rather than a proxy for it."""

    def _commit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        _git(self.repo, "add", name)
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", name)
        return _git(self.repo, "rev-parse", "HEAD")

    def test_a_receipt_on_this_very_head_says_nothing(self):  # noqa: VACUOUS_ASSERTION — silence IS the property under test, and the same-tree-sibling arm below proves this door speaks on the same call, so None here discriminates
        """The control. Without it every arm below passes on a door that is
        simply always loud."""
        self.assertIsNone(
            gateimport.head_divergence(self.repo, self._row()))

    def test_a_receipt_on_a_DESCENDANT_of_head_says_nothing(self):  # noqa: VACUOUS_ASSERTION — silence IS the property under test; the same-tree-sibling arm below is the unconditional positive control on this same observable
        """`bind` accepts a train that carries the reviewed tip, so a reader
        that warned here would order a re-gate the binder does not want."""
        base = self.head
        later = self._commit("b.txt", "b\n")
        _git(self.repo, "checkout", "-q", base)
        self.assertIsNone(
            gateimport.head_divergence(self.repo, self._row(head=later)))

    def test_a_SAME_TREE_SIBLING_is_disclosed_because_bind_refuses_it(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual is a FIXTURE PRECONDITION (the two commits must really differ) and it sits beside an unconditional positive control on the observable: assertIsNotNone plus two assertIn checks on the returned note, one of them the snapshot's own sha prefix
        """A fab snapshot and the author's own commits can hold the IDENTICAL
        TREE while neither carries the other — here the author has two commits
        and the snapshot squashes the same content into one. A tree comparison
        calls them equal and goes silent; `bind` refuses because the reviewed
        patch sequence is absent from that train. This surface must not be
        quiet in exactly the case that cannot bind."""
        base = self.head
        self._commit("a.txt", "a\n")
        mine = self._commit("b.txt", "b\n")
        tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        snapshot = _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit-tree", tree, "-p", base, "-m", "fab snapshot")
        self.assertEqual(_git(self.repo, "rev-parse", snapshot + "^{tree}"),
                         _git(self.repo, "rev-parse", mine + "^{tree}"),
                         "control: the fixture must actually build the "
                         "same-tree case, or this arm proves nothing")
        self.assertNotEqual(snapshot, mine)
        note = gateimport.head_divergence(self.repo, self._row(head=snapshot))
        self.assertIsNotNone(note, "a receipt bind will refuse must not be "
                                   "announced by silence")
        self.assertIn("does NOT carry HEAD", note)
        self.assertIn(snapshot[:12], note)

    def test_an_unreadable_HEAD_reads_UNKNOWN_and_never_silence(self):
        """Silence means MEASURED COVERAGE. A read that never completed is a
        third state and says so — the failure that motivated it went quiet,
        which is indistinguishable from a clean answer."""
        with mock.patch.object(gateimport, "_head_sha",
                               return_value=(None, "HEAD does not resolve")):
            note = gateimport.head_divergence(self.repo, self._row())
        self.assertIsNotNone(note)
        self.assertIn("UNKNOWN", note)

    def test_the_note_survives_a_REAL_encoder_not_only_a_str_sink(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone above is the unconditional positive control (a None note would make the encode trivially pass), and the encode is not an assert-no-raise in the vacuous sense: returning the note unrendered makes this arm RED, which is how the door was shown to bite
        """A REPOSITORY PATH IS RAW BYTES and this note is built from one.

        Python surrogate-escapes an undecodable filesystem byte; a str sink
        accepts the result and a real UTF-8 stream RAISES on it, so an arm
        that captures output with io.StringIO cannot see this at all. The
        assertion is therefore an ENCODE, which is the only thing that puts an
        encoder on the path. It matters here because the note prints AFTER the
        durable appends: a raise would turn a completed import into a retry
        that lands on the duplicate path looking like a new bug."""
        hostile = os.path.join(self.tmp, b"repo-\xff".decode("utf-8",
                                                             "surrogateescape"))
        note = gateimport.head_divergence(hostile, self._row())
        self.assertIsNotNone(note, "control: an unreadable repo must still "
                                   "produce a note, or this arm encodes None")
        note.encode("utf-8")            # the whole assertion: no raise here

    def test_a_raising_containment_read_cannot_fail_the_import(self):
        """This runs AFTER the durable appends. A best-effort note that raised
        would turn an import that already happened into a retry that lands on
        the duplicate path looking like a different bug."""
        with mock.patch.object(gate, "carriage", side_effect=OSError("boom")):
            note = gateimport.head_divergence(self.repo, self._row())
        self.assertIsNotNone(note)
        self.assertIn("UNKNOWN", note)


class ChunkWalkScopeTest(ImportBase):
    """The durable-receipt rung walks THIS receipt's chunks, not the ledger's.

    `gate._failure_chunks_with_errors` keeps only the chunks a receipt
    references, so the set it is handed is the set it will keep. Handing it
    every stored row is therefore a superset with no finding in it, and the
    stop path checks thousands of receipts against one ledger, so a wider
    input is paid once per receipt. The arms below pin the INPUT, because the
    output is identical either way and no assertion about the answer sees it.

    N receipts, each minted by the real overflow producer over its own tagged
    failures, so each carries chunk ids no other receipt references. The
    fixture's shape is measured, not assumed: 300 identities with a padded
    test id cross the 64 KiB event boundary, so a receipt owns TWO chunks and
    a four-receipt ledger holds EIGHT.
    """

    CHUNKS_PER_RECEIPT = 2

    def _overflow(self, tag, n=300, pad=200):
        """One real v8 overflow receipt whose identities span two chunks."""
        failures = [{"kind": "ERROR",
                     "test": "tests.test_%s.C%s.test_%d" % (tag, "w" * pad, i),
                     "traceback": "x" * gate._FAILURE_TEXT_CAP}
                    for i in range(n)]
        record, chunks = gate._failure_record(failures)
        self.assertEqual(len(chunks), self.CHUNKS_PER_RECEIPT,
                         "fixture control: this shape must span two chunks, "
                         "or the arm cannot tell a scoped walk from a wide one")
        row = self._row(
            v=8, status="FAILED", rc=1, ran=n, skipped=0, label=tag,
            detail="errors=%d" % n, failures_unreadable=False,
            host={"node": "fab", "system": "Linux", "release": "1",
                  "id": "machine"}, **record)
        return row, chunks

    def _seed(self, *tags):
        """Import one real overflow receipt per tag; return them in order."""
        out = []
        for tag in tags:
            row, chunks = self._overflow(tag)
            got, verdict, err = self._import(self._artifact(*(chunks + [row])))
            self.assertEqual((got["id"], verdict, err),
                             (row["id"], "imported", None))
            out.append(row)
        return out

    @contextlib.contextmanager
    def _spy(self):
        """Record what the REAL walk is called with, without replacing it.

        A double that returns a reconstructed answer would prove only that the
        arm agrees with itself. This wraps the shipped function, so the rung
        under test gets the real validation and the arm gets the real input.
        """
        seen = []
        real = gate._failure_chunks_with_errors

        def wrapped(rows):
            rows = list(rows)
            seen.append(rows)
            return real(rows)

        with mock.patch.object(gate, "_failure_chunks_with_errors", wrapped):
            yield seen

    def test_the_walk_is_entered_once_with_this_receipts_referenced_chunks(self):
        rows = self._seed("lane0", "lane1", "lane2", "lane3")
        mine = rows[0]
        self.assertEqual(len(mine["failure_chunks"]), self.CHUNKS_PER_RECEIPT)
        stored, _poisoned, err = gateimport._stored_ids()
        self.assertIsNone(err)
        wide = [rid for rid, row in stored.items()
                if row.get("event") == gate._FAILURE_CHUNK_EVENT]
        self.assertEqual(len(wide), len(rows) * self.CHUNKS_PER_RECEIPT,
                         "must-hit control: the ledger holds every lane's "
                         "chunks, so a wide walk has something to over-read")
        with self._spy() as seen:
            self.assertIsNone(gateimport._durable_receipt_err(mine))
        self.assertEqual(len(seen), 1, "one receipt, one walk")
        self.assertEqual([row["id"] for row in seen[0]],
                         list(mine["failure_chunks"]),
                         "the walk takes the referenced chunks, in order")

    def test_the_walks_input_does_not_grow_with_the_ledger(self):
        """The equality above still passes if someone re-widens the input on a
        one-receipt fixture. This is the half that does not: the same receipt
        is asked twice, with three more lanes' chunks appended in between, and
        the second answer must be identical rather than larger."""
        mine = self._seed("lane0")[0]
        with self._spy() as first:
            self.assertIsNone(gateimport._durable_receipt_err(mine))
        self._seed("lane1", "lane2", "lane3")
        with self._spy() as second:
            self.assertIsNone(gateimport._durable_receipt_err(mine))
        self.assertEqual([row["id"] for row in second[0]],
                         [row["id"] for row in first[0]],
                         "a receipt's walk is bounded by its own references, "
                         "never by how many other receipts share the ledger")
        self.assertEqual(len(second[0]), self.CHUNKS_PER_RECEIPT)

    def test_a_receipt_with_no_failure_record_enters_no_walk_at_all(self):
        """A v3 receipt has no failure record to validate, so no chunk walk
        can tell it anything and the rung answers without entering one."""
        plain = self._row()
        got, verdict, err = self._import(self._artifact(plain))
        self.assertEqual((got["id"], verdict, err),
                         (plain["id"], "imported", None))
        self._seed("lane0", "lane1")
        overflow = self._seed("lane2")[0]
        self.assertTrue(gate._version_has(overflow, "failure_record"),
                        "control: the companion fixture must be the kind that "
                        "DOES carry one, or the line below reads as a broken "
                        "predicate rather than a versionless receipt")
        self.assertFalse(gate._version_has(plain, "failure_record"),
                         "control: this fixture must be the versionless kind")
        # POSITIVE CONTROL FOR THE SAME SPY. An empty list is also what a spy
        # that never attached would show, and that failure looks identical to
        # the property. A receipt that DOES carry a failure record is driven
        # through the same rung under the same wrapper, so the emptiness below
        # is a fact about the v3 receipt rather than about the instrument.
        # SEEDED OUTSIDE THE SPY: import validates the artifact's own chunks
        # through the same function, and those calls are not what this arm is
        # about.
        with self._spy() as seen:
            self.assertIsNone(gateimport._durable_receipt_err(plain))
            self.assertIsNone(gateimport._durable_receipt_err(overflow))
        self.assertEqual([[row["id"] for row in call] for call in seen],
                         [list(overflow["failure_chunks"])],
                         "the v3 receipt enters no walk; the overflow receipt "
                         "beside it enters exactly one, with its own chunks")


class FailureTextPredicateTest(unittest.TestCase):
    """`_has_failure_text` answers exactly what `_failure_text` truthiness did.

    The chunk walk asks IS THERE TEXT once per identity, and a normalized,
    capped copy is a costly way to answer it. The two must agree on every
    value the identity fields can hold, including the ones where `bool(value)`
    would not: whitespace-only strings are truthy and carry no line, and 0,
    False and the empty containers are falsy through the `or ""` guard.
    """

    CASES = ("x", "  ", "", "   x   ", "\t\n", None, 0, False, 1, [], [0],
             {}, {"a": 1}, "0", " \u00a0 ", "\u200b")

    def test_the_predicate_matches_the_builders_truthiness_on_every_case(self):
        self.assertTrue(gate._has_failure_text("x"),
                        "control: the predicate answers True for plain text")
        self.assertEqual(gate._failure_text("x"), "x",
                         "control: the builder returns that same text")
        answers = set()
        for value in self.CASES:
            answer = bool(gate._has_failure_text(value))
            answers.add(answer)
            self.assertEqual(answer, bool(gate._failure_text(value)),
                             "disagreed on %r" % (value,))
        # THE CONTROL BELONGS IN THIS TEST. Two predicates that agreed by
        # both being constant — or an empty case list — satisfy the loop and
        # prove nothing, and a control in a sibling method does not bind this
        # one's corpus.
        self.assertEqual(answers, {True, False},
                         "the cases must reach both answers")

    def test_whitespace_is_the_case_bool_would_get_wrong(self):
        self.assertTrue(bool("  "), "control: the value itself is truthy")
        self.assertTrue(gate._has_failure_text(" x "),
                        "control: the predicate does answer True for text "
                        "that arrives surrounded by the same whitespace")
        self.assertFalse(gate._has_failure_text("  "))
        self.assertFalse(gate._has_failure_text("\u3000"))


class TreeBatchTest(ImportBase):
    """One `cat-file` per repository per pass, answering exactly what 845
    `rev-parse` spawns answered.

    THE RECEIPT COUNT IS THE INPUT NOBODY VARIED, and it is why this defect
    survived every earlier probe: under a fresh HELM_HOME the gate-import
    ledger is empty, `green_receipts` authorizes nothing, and the whole rung
    costs 0.15s with ten spawns. So every arm here puts MORE THAN ONE HEAD in
    one batch — a table with one row cannot show a misalignment, and
    misalignment is the only way this design could hand a receipt another
    receipt's tree."""

    def _spy(self):
        """(patcher, argv list) — every git argv this backend actually runs."""
        seen = []
        real = gateimport.vcs.GitVcs.run

        def run(inner, cwd, *args, **kw):
            seen.append(tuple(str(a) for a in args))
            return real(inner, cwd, *args, **kw)

        return mock.patch.object(gateimport.vcs.GitVcs, "run", run), seen

    def _garbage(self):
        """A 40-hex head this repo cannot resolve — a real shape, no object."""
        return "0123456789abcdef" * 2 + "01234567"

    def test_one_real_head_and_one_garbage_head_land_on_their_own_rows(self):
        # THE ARM THE ROW ASKED FOR. Both heads ride ONE batch, and the
        # garbage one must produce the CITED-HEAD refusal, never a tree
        # mismatch: a mismatch would mean the batch answered for the wrong
        # request, which is the laundering this rung exists to refuse.
        garbage = self._garbage()
        real = self._row()
        wrong = dict(real, head=garbage)
        with gateimport.tree_batch(self.repo, [real["head"], garbage]):
            self.assertIsNone(gateimport.placement_err(real, self.repo),
                              "the real head must place through the batch")
            err = gateimport.placement_err(wrong, self.repo)
        self.assertIn("does not resolve", err or "")
        self.assertNotIn("tree mismatch", err or "")
        self.assertIn(garbage, err or "",
                      "the refusal must name the head it was asked about")

    def test_the_batch_is_one_process_for_every_head_in_the_pass(self):  # noqa: VACUOUS_ASSERTION — the empty rev-parse list is controlled on the SAME `seen` list by the unconditional cat-file assertion below, which proves the spy observed real argv
        heads = [self.head, self._garbage(),
                 "f" * 40, "a" * 40, "b" * 40, "c" * 40]
        patcher, seen = self._spy()
        with patcher:
            with gateimport.tree_batch(self.repo, heads):
                trees = [gateimport._tree_of(self.repo, h) for h in heads]
        self.assertEqual(trees, [self.tree] + [None] * 5)
        self.assertEqual(
            [a for a in seen if a and a[0] == "rev-parse"], [],
            "no head may be re-asked one process at a time")
        self.assertEqual(
            [a for a in seen if a and a[0] == "cat-file"],
            [("cat-file", "--batch-check=%(objectname)")],
            "six heads must cost exactly one git process")

    def test_the_request_carries_the_tree_suffix_and_never_a_bare_sha(self):
        # A BARE SHA ECHOED BACK IS INDISTINGUISHABLE FROM AN ANSWER. Feeding
        # `<head>^{tree}` means git's own refusal echo carries the suffix, so
        # no reader — this one included — can mistake the request for an oid.
        sent = []
        real = gateimport.vcs.GitVcs.run

        def run(inner, cwd, *args, stdin=None, **kw):
            sent.append(stdin)
            return real(inner, cwd, *args, stdin=stdin, **kw)

        with mock.patch.object(gateimport.vcs.GitVcs, "run", run):
            with gateimport.tree_batch(self.repo, [self.head, self._garbage()]):
                pass
        self.assertEqual(len(sent), 1, "one batch, one request stream")
        lines = sent[0].decode("ascii").splitlines()
        self.assertEqual(sorted(lines),
                         sorted([self.head + "^{tree}",
                                 self._garbage() + "^{tree}"]))

    def test_an_unfamiliar_answer_form_is_refused_and_re_asked_one_by_one(self):  # noqa: VACUOUS_ASSERTION — the None is controlled by the unconditional `answered == self.tree` on the same door, which proves the mangled batch still reached a real read
        # THE POSITIVE SHAPE TEST IS WHAT MAKES A FUTURE GIT FAIL CLOSED.
        # `%(objectname) %(objecttype) %(objectsize)` is git's own default
        # form, and it is the form a careless later edit would reintroduce: it
        # carries the oid, so a parser that only EXCLUDED `missing` would read
        # the first token and answer. This one admits nothing it cannot
        # defend, and the per-head read — which is the answer trunk gave —
        # still produces the right tree.
        real = gateimport.vcs.GitVcs.run

        def run(inner, cwd, *args, **kw):
            rc, out, err = real(inner, cwd, *args, **kw)
            if args and str(args[0]) == "cat-file":
                out = b"".join(
                    l.split(b" ")[0] + b" tree 42\n"
                    for l in out.strip(b"\n").split(b"\n"))
            return rc, out, err

        with mock.patch.object(gateimport.vcs.GitVcs, "run", run):
            with gateimport.tree_batch(self.repo, [self.head, self._garbage()]):
                answered = gateimport._tree_of(self.repo, self.head)
                refused = gateimport._tree_of(self.repo, self._garbage())
        self.assertEqual(answered, self.tree,
                         "an unreadable batch costs the old cost, never a "
                         "wrong or missing tree")
        self.assertIsNone(refused)

    def test_a_short_batch_is_discarded_rather_than_aligned_by_guesswork(self):  # noqa: VACUOUS_ASSERTION — the None is controlled by the unconditional `answers[self.head] == self.tree` on the same table, which proves the truncated batch was replaced by real answers
        # FEWER LINES THAN REQUESTS MEANS THE POSITIONAL MAPPING IS UNPROVEN.
        # Dropping the FIRST line is the case that matters: every surviving
        # answer then belongs to the request before it, so an aligner would
        # hand the garbage head the real head's tree.
        real = gateimport.vcs.GitVcs.run

        def run(inner, cwd, *args, **kw):
            rc, out, err = real(inner, cwd, *args, **kw)
            if args and str(args[0]) == "cat-file":
                out = b"\n".join(out.strip(b"\n").split(b"\n")[1:]) + b"\n"
            return rc, out, err

        heads = sorted([self.head, self._garbage()])
        with mock.patch.object(gateimport.vcs.GitVcs, "run", run):
            with gateimport.tree_batch(self.repo, heads):
                answers = {h: gateimport._tree_of(self.repo, h) for h in heads}
        self.assertEqual(answers[self.head], self.tree)
        self.assertIsNone(answers[self._garbage()],
                          "a truncated batch must never promote the garbage "
                          "head to the real head's tree")

    def test_a_head_that_is_not_a_bare_sha_is_never_fed_to_the_stream(self):  # noqa: VACUOUS_ASSERTION — the empty cat-file list is controlled on the SAME `seen` list by the unconditional rev-parse assertion below
        # THE REQUEST STREAM IS LINE-DELIMITED, so a head carrying a newline
        # would desynchronise it. Such a head is left to `rev-parse`, which
        # takes it as one argv element.
        poisoned = self.head + "\n" + self._garbage()
        patcher, seen = self._spy()
        with patcher:
            with gateimport.tree_batch(self.repo, [poisoned, "HEAD"]):
                self.assertIsNone(gateimport._tree_of(self.repo, poisoned))
        self.assertEqual([a for a in seen if a and a[0] == "cat-file"], [],
                         "a stream with no admissible head is not spawned")
        self.assertEqual(
            [a for a in seen if a and a[0] == "rev-parse"],
            [("rev-parse", poisoned + "^{tree}")],
            "the unbatched head goes through the per-head read unchanged")

    def test_the_table_answers_only_for_the_repository_it_was_built_for(self):  # noqa: VACUOUS_ASSERTION — the None is controlled by the unconditional rev-parse argv assertion, which proves the foreign repo was asked git rather than silently skipped
        # WORKTREES SHARE AN OBJECT STORE, which is exactly how a foreign
        # receipt would enter. A table built for one repository is not
        # evidence about another, so another repo path misses it entirely.
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        _git(other, "init", "-q", ".")
        patcher, seen = self._spy()
        with patcher:
            with gateimport.tree_batch(self.repo, [self.head, self._garbage()]):
                self.assertIsNone(gateimport._tree_of(other, self.head),
                                  "the other repo genuinely lacks the object")
        self.assertEqual(
            [a for a in seen if a and a[0] == "rev-parse"],
            [("rev-parse", self.head + "^{tree}")],
            "a foreign repo must re-ask git rather than read this table")

    def test_the_pass_ends_with_its_scope_and_leaves_no_table_behind(self):  # noqa: VACUOUS_ASSERTION — the _UNASKED sentinel is controlled by the unconditional in-scope `== self.tree` assertion on the same table
        with gateimport.tree_batch(self.repo, [self.head, self._garbage()]):
            self.assertEqual(gateimport._pass_tree(self.repo, self.head),
                             self.tree, "control: the table answers inside")
        self.assertIs(gateimport._pass_tree(self.repo, self.head),
                      gateimport._UNASKED)
        patcher, seen = self._spy()
        with patcher:
            self.assertEqual(gateimport._tree_of(self.repo, self.head),
                             self.tree)
        self.assertEqual([a for a in seen if a and a[0] == "rev-parse"],
                         [("rev-parse", self.head + "^{tree}")],
                         "outside a pass the per-head read is the only door")

    def test_the_batch_runs_under_the_same_view_as_the_per_head_read(self):
        # THE ENV IS PART OF THE QUESTION: a replacement ref changes which
        # object an id names, so the two doors must ask under one view or the
        # cheap door answers about a different repository.
        envs = []
        real = gateimport.vcs.GitVcs.run

        def run(inner, cwd, *args, env=None, **kw):
            envs.append((str(args[0]) if args else "", env))
            return real(inner, cwd, *args, env=env, **kw)

        with mock.patch.object(gateimport.vcs.GitVcs, "run", run):
            with gateimport.tree_batch(self.repo, [self.head, self._garbage()]):
                gateimport._tree_of(self.repo, "HEAD")
        batch = [e for verb, e in envs if verb == "cat-file"]
        single = [e for verb, e in envs if verb == "rev-parse"]
        self.assertEqual(len(batch), 1, "control: the batch did spawn")
        self.assertEqual(len(single), 1, "control: the per-head read did spawn")
        self.assertEqual(batch[0], single[0])
        self.assertEqual(batch[0].get("GIT_NO_REPLACE_OBJECTS"), "1")


class BindingMatchBoundaryTest(unittest.TestCase):
    """The binding matcher charges its budget at the PASS boundary.

    A charge per binding row makes the spend count the product of two growing
    ledgers — hundreds of matcher calls over a thousand-row ledger is seven
    figures of accounting in one stop — and lets a deadline land in the middle
    of one receipt's lookup, where the answer is neither empty nor whole.

    EVERY ARM HERE CARRIES ITS OWN POSITIVE CONTROL on the observable it
    reads, because both properties are about something NOT happening: an arm
    that only counts a small number of spends, or only fails to raise, would
    pass just as well against a matcher that was never called at all.
    """

    def setUp(self):
        gateimport._BINDING_INDEX_MEMO.clear()

    tearDown = setUp

    @staticmethod
    def _rows(n, hit_at=None, receipt="r", repo="/repo"):
        rows = [{"receipt": "other-%d" % i, "importing_repo": "/elsewhere"}
                for i in range(n)]
        if hit_at is not None:
            rows[hit_at] = {"receipt": receipt, "importing_repo": repo}
        return rows

    def test_spends_do_not_scale_with_the_binding_ledger(self):  # noqa: VACUOUS_ASSERTION — the same counter asserts len(seen) > 0 and the planted row is found on BOTH arms, so the equality cannot hold over a matcher that never ran
        """The charge count is the SAME for 4 rows and for 400.

        The old per-row charge makes this red by construction: it spent
        n + 1 times, so the two counts differed by 396.
        """
        counts = {}
        for n in (4, 400):
            rows = self._rows(n, hit_at=n - 1)
            seen = []
            real = projscope.spend_or_raise
            with mock.patch.object(gateimport.projscope, "spend_or_raise",
                                   side_effect=lambda what: (seen.append(what),
                                                             real(what))[1]):
                gateimport._BINDING_INDEX_MEMO.clear()
                hit, err = gateimport._matching_binding(rows, "r", "/repo")
            # CONTROL, unconditional: the matcher ran, found the planted row,
            # and the counter saw real spends. Without these three a count of
            # "the same small number" would also describe a no-op.
            self.assertIsNone(err)
            self.assertIs(hit, rows[n - 1], "the matcher did not find the row")
            self.assertGreater(len(seen), 0, "control: no spend was observed")
            counts[n] = len(seen)
        self.assertEqual(counts[4], counts[400],
                         "the budget is still charged per binding row: %r" % counts)

    def test_a_deadline_cannot_expire_inside_one_lookup(self):
        """A clock that advances on every read expires the OLD scan mid-row.

        `projscope.remaining` reads `time.monotonic` once per spend, so a
        clock that ticks 1ms per read burns a 50ms budget at row ~48 of 400
        under the old matcher and raises `Expired` with a whole answer half
        built. Under the boundary charge the same clock and the same budget
        leave the lookup intact.
        """
        rows = self._rows(400, hit_at=399)
        ticks = {"n": 0}

        def clock():
            ticks["n"] += 1
            return ticks["n"] * 0.001

        with mock.patch.object(projscope.time, "monotonic", side_effect=clock):
            with projscope.scope(deadline=0.050):
                hit, err = gateimport._matching_binding(rows, "r", "/repo")
        self.assertEqual((hit, err), (rows[399], None))
        self.assertGreater(ticks["n"], 0, "control: the planted clock was never read")

        # CONTROL on the same clock and the same door: a budget already spent
        # STILL refuses at the boundary. Without this the arm above would pass
        # against a matcher that had stopped consulting the budget at all.
        ticks["n"] = 0
        gateimport._BINDING_INDEX_MEMO.clear()
        with mock.patch.object(projscope.time, "monotonic", side_effect=clock):
            with projscope.scope(deadline=0.0):
                with self.assertRaises(projscope.Expired):
                    gateimport._matching_binding(rows, "r", "/repo")

    def test_the_multi_binding_refusal_skips_the_completion_spend(self):
        rows = [{"receipt": "r", "importing_repo": "/repo"},
                {"receipt": "r", "importing_repo": "/repo"}]
        seen = []
        real = projscope.spend_or_raise
        with mock.patch.object(gateimport.projscope, "spend_or_raise",
                               side_effect=lambda what: (seen.append(what),
                                                         real(what))[1]):
            hit, err = gateimport._matching_binding(rows, "r", "/repo")
        self.assertIsNone(hit)
        self.assertIn("multiple canonical bindings", err)
        self.assertNotIn("canonical binding match completion", seen)
        # CONTROL: the same counter DOES see the completion spend on the
        # ordinary answer, so its absence above is a fact about this path.
        seen.clear()
        gateimport._BINDING_INDEX_MEMO.clear()
        with mock.patch.object(gateimport.projscope, "spend_or_raise",
                               side_effect=lambda what: (seen.append(what),
                                                         real(what))[1]):
            gateimport._matching_binding(rows[:1], "r", "/repo")
        self.assertIn("canonical binding match completion", seen)

    def test_the_index_never_answers_for_a_different_rows_list(self):  # noqa: VACUOUS_ASSERTION — the two controls below resolve the second list's own question and re-resolve the first, so the None is a miss and not an inert matcher
        """One entry keyed on the list's identity, confirmed with `is`.

        A memo keyed on `id()` alone can be handed a recycled address; the
        entry holds its rows so the address cannot be recycled, and a second
        list is a miss rather than a hit.
        """
        first = [{"receipt": "r", "importing_repo": "/repo"}]
        second = [{"receipt": "r", "importing_repo": "/other"}]
        self.assertIs(gateimport._matching_binding(first, "r", "/repo")[0],
                      first[0])
        self.assertIsNone(
            gateimport._matching_binding(second, "r", "/repo")[0],
            "the second list was answered from the first list's index")
        # CONTROL: the second list is not simply unanswerable — its own
        # question resolves against it.
        self.assertIs(gateimport._matching_binding(second, "r", "/other")[0],
                      second[0])
        # CONTROL: and the first list still answers its own question after the
        # second displaced it, so "one entry" costs correctness nothing.
        self.assertIs(gateimport._matching_binding(first, "r", "/repo")[0],
                      first[0])

    def test_the_first_ledger_row_wins_and_an_empty_ledger_answers_none(self):
        rows = self._rows(3)
        self.assertEqual(gateimport._matching_binding(rows, "r", "/repo"),
                         (None, None))
        # CONTROL: the same three rows DO answer the question they carry.
        self.assertIs(
            gateimport._matching_binding(rows, "other-1", "/elsewhere")[0],
            rows[1])
        self.assertEqual(gateimport._matching_binding([], "r", "/repo"),
                         (None, None))
