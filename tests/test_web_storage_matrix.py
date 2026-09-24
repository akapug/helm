#!/usr/bin/env python3
"""Storage-matrix web seam: real HTTP states and actual client rendering."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helm import storage_matrix as matrix
from helm import web, web_ui_loader
from tests.test_storage_matrix import snapshot
from tests.test_web_chat_client_runtime import _extract_fn


NOW = dt.datetime(2026, 8, 4, 12, tzinfo=dt.timezone.utc)


class WebStorageMatrixHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-storage-web-")
        cls.artifact = os.path.join(cls.tmp, "storage-matrix.json")
        cls.prior = {key: os.environ.get(key) for key in (
            "HELM_HOME", "MELD_HOME", "HELM_STORAGE_MATRIX")}
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ.pop("MELD_HOME", None)
        os.environ["HELM_STORAGE_MATRIX"] = cls.artifact
        cls.server = web.make_server(0)
        cls.port = cls.server.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        for key, value in cls.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        try:
            os.unlink(self.artifact)
        except FileNotFoundError:
            pass

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def api(self):
        status, content_type, body = self.get("/api/storage-matrix")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        return json.loads(body)

    def test_real_endpoint_serves_partial_snapshot_and_unavailable_row(self):
        value = snapshot(status="partial")
        value["rows"].extend([{
            "id": "offline:local-disk", "box": "offline",
            "box_label": "Offline", "tier": "local-disk",
            "tier_label": "Local disk", "transport": "ssh",
            "path_label": "host-local scratch storage", "order": 2,
            "status": "unavailable", "measured_at": value["measured_at"],
            "error": "inventory reports this box unreachable",
        }, {
            "id": "offline:memory", "box": "offline",
            "box_label": "Offline", "tier": "memory",
            "tier_label": "Memory", "transport": "ssh",
            "path_label": "/dev/shm", "order": 3,
            "status": "unavailable", "measured_at": value["measured_at"],
            "error": "inventory reports this box unreachable",
        }])
        matrix.pk.write_json(self.artifact, value)
        with mock.patch.object(matrix, "_now", return_value=NOW):
            data = self.api()
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["status"], "partial")
        self.assertFalse(data["stale"])
        self.assertEqual(data["rows"][0]["metrics"]["seq_read_mib_s"], 202.0)
        self.assertEqual(data["rows"][2]["status"], "unavailable")
        self.assertIn("unreachable", data["rows"][2]["error"])
        self.assertEqual({row["tier"] for row in data["rows"] if row["box"] == "offline"},
                         {"local-disk", "memory"})

    def test_missing_and_malformed_artifacts_are_honest_data_states(self):
        missing = self.api()
        self.assertEqual(missing["state"], "missing")
        self.assertIn("helm storage-matrix --measure", missing["message"])
        with open(self.artifact, "w", encoding="utf-8") as handle:
            handle.write("not json")
        malformed = self.api()
        self.assertEqual(malformed["state"], "unavailable")
        self.assertIn("unreadable", malformed["message"])

    def test_nonfinite_optional_metrics_never_reach_browser_json(self):
        def strict(body):
            def reject(value):
                raise ValueError("non-standard JSON constant: " + value)
            return json.loads(body, parse_constant=reject)

        value = snapshot()
        value["rows"][0]["metrics"]["free_bytes_before"] = float("nan")
        matrix.pk.write_json(self.artifact, value)
        status, _content_type, body = self.get("/api/storage-matrix")
        self.assertEqual(status, 200)
        rejected = strict(body)
        self.assertEqual(rejected["state"], "unavailable")
        self.assertIn("free_bytes_before is invalid", rejected["message"])

        value = snapshot()
        value["rows"][0]["metrics"]["future_metric"] = float("nan")
        matrix.pk.write_json(self.artifact, value)
        status, _content_type, body = self.get("/api/storage-matrix")
        self.assertEqual(status, 200)
        projected = strict(body)
        self.assertEqual(projected["state"], "ok")
        self.assertNotIn("future_metric", projected["rows"][0]["metrics"])

    def test_query_and_mutation_nan_return_strict_json_500(self):
        def response(request):
            try:
                urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())
            self.fail("non-finite response unexpectedly returned success")

        query = lambda _qs: ({"value": float("nan")}, 200)
        with mock.patch.dict(web.QUERY_API, {"/api/strict-query": query}):
            status, body = response("http://127.0.0.1:%d/api/strict-query" % self.port)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "response contains a non-JSON value")

        mutation = lambda _payload: ({"value": float("nan")}, 200)
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/strict-post" % self.port,
            data=b"{}", headers={"Authorization": "Bearer " + web.MUTATION_TOKEN,
                                  "Content-Type": "application/json"})
        with mock.patch.dict(web.POST_API, {"/api/strict-post": mutation}):
            status, body = response(request)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "response contains a non-JSON value")

    def test_stale_and_future_times_remain_distinct(self):
        matrix.pk.write_json(self.artifact, snapshot("2026-07-20T12:00:00Z"))
        with mock.patch.object(matrix, "_now", return_value=NOW):
            stale = self.api()
        self.assertTrue(stale["stale"])
        self.assertGreater(stale["age_s"], stale["stale_after_s"])

        matrix.pk.write_json(self.artifact, snapshot("2026-08-05T12:00:00Z"))
        with mock.patch.object(matrix, "_now", return_value=NOW):
            future = self.api()
        self.assertFalse(future["stale"])
        self.assertIsNone(future["age_s"])
        self.assertIn("future", future["age_message"])

    def test_unexpected_reader_failure_degrades_at_http_200(self):
        with mock.patch.object(matrix, "view", side_effect=OSError("synthetic")):
            data = self.api()
        self.assertEqual(data["state"], "unavailable")
        self.assertIn("could not be read", data["message"])

    def test_root_serves_boxes_navigation_and_accessible_table(self):
        status, content_type, body = self.get("/")
        page = body.decode("utf-8")
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertIn('data-v="boxes"', page)
        self.assertIn('id="view-boxes"', page)
        self.assertNotIn('data-v="storage"', page,
                         "the storage tab was RENAMED to boxes — no button "
                         "may keep the old view id")
        self.assertIn("every machine helm can see, with its live speed "
                      "benchmarks", page)
        self.assertIn('id="storageRows"', page)
        self.assertIn('id="storagestatus" role="status" aria-live="polite" '
                      'aria-atomic="true"', page)
        self.assertIn('<caption>Exact values stay visible.', page)
        self.assertEqual(page.count('<th scope="col">'), 6)
        self.assertIn("one row per box", page)
        self.assertIn("disk (solid) and memory (hatched)", page)
        self.assertIn("Fsync is lower-is-faster", page)


class StorageMatrixWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = web_ui_loader.read_text()

    def test_shared_json_serializer_returns_a_strict_500_for_nan(self):
        class Sink:
            sent = None
            def _send(self, *args, **kwargs):
                self.sent = (args, kwargs)
        sink = Sink()
        web.Handler._json(sink, {"value": float("nan")})
        self.assertIsNotNone(sink.sent)
        args, _kwargs = sink.sent
        self.assertEqual(args[2], 500)
        self.assertEqual(json.loads(args[0]),
                         {"error": "response contains a non-JSON value"})

    def test_boxes_view_initializes_only_when_selected(self):
        show = _extract_fn(self.source, "showView")
        self.assertIn('if (v === "boxes") initStorage()', show)
        views_line = self.source[self.source.index("const VIEWS"):].split("\n")[0]
        self.assertIn('"boxes"', views_line)
        self.assertNotIn('"storage"', views_line,
                         "the old view id must not survive in VIEWS — "
                         "canonView owns the migration instead")

    def test_saved_storage_view_key_and_old_hash_migrate_to_boxes(self):
        """Half-arc law: the rename ships its migration. A stored
        helm.view=storage (or an old #storage bookmark) must land on the
        boxes tab, and both read paths must go through the one mapper."""
        canon = _extract_fn(self.source, "canonView")
        self.assertIn('storage: "boxes"', canon)
        self.assertIn('const raw = location.hash.slice(1), v = canonView(raw)',
                      self.source)
        self.assertIn('canonView(localStorage.getItem("helm.view"))',
                      self.source)

    def test_loader_reads_snapshot_endpoint_and_reload_uses_same_path(self):
        load = _extract_fn(self.source, "loadStorage")
        init = _extract_fn(self.source, "initStorage")
        self.assertIn('j("/api/storage-matrix")', load)
        self.assertIn("renderStorage", load)
        self.assertIn('$("#storageReload").onclick = loadStorage', init)
        self.assertTrue(init.rstrip().endswith("}"))
        self.assertIn("loadStorage();", init)

    def test_renderer_retains_non_color_accessibility_contract(self):
        render = _extract_fn(self.source, "renderStorage")
        self.assertIn('th.scope = "row"', render)
        self.assertIn('item.setAttribute("aria-label", item.dataset.tip)', render)
        self.assertIn('track.setAttribute("aria-hidden", "true")', render)
        self.assertIn('smNode("span", "smlabel", short)', render)
        self.assertIn("valueEl", render)
        self.assertIn("separate logarithmic scale for each metric", render)
        self.assertIn(".smtier:focus-visible", self.source)
        self.assertIn(".smtier.memory .smfill", self.source)


class StorageMatrixClientRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        source = web_ui_loader.read_text()
        metrics = re.search(
            r"const STORAGE_METRICS\s*=\s*\[.*?\n\];", source, re.DOTALL)
        if not metrics:
            raise AssertionError("STORAGE_METRICS not found in assembled web UI")
        functions = "\n\n".join(_extract_fn(source, name) for name in (
            "storageAge", "smNode", "renderStorage", "canonView"))
        cls.tmp = tempfile.mkdtemp(prefix="helm-storage-client-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as handle:
            handle.write(metrics.group(0) + "\n" + functions + "\n" + _DRIVER)
        checked = subprocess.run([cls.node, "--check", cls.path],
                                 capture_output=True, text=True)
        assert checked.returncode == 0, "node --check failed:\n" + checked.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=30)
        try:
            cls.results = json.loads(cls.proc.stdout)
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _result(self, key):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIn(key, self.results)
        return self.results[key]

    def test_renderer_pairs_disk_and_memory_once_per_box(self):
        rendered = self._result("rendered")
        self.assertEqual(rendered["rowCount"], 2)
        self.assertEqual(rendered["boxes"], ["Box", "Offline"])
        self.assertEqual(rendered["pairCounts"], [2] * 5)
        self.assertEqual(rendered["tierLabels"], ["disk", "memory"])
        self.assertEqual(rendered["memoryValues"], [
            "101.2 MiB/s", "202.4 MiB/s", "303 IOPS", "404 IOPS", "0.9 ms"])
        self.assertIn("disk: inventory reports disk unreachable",
                      rendered["offlineState"])
        self.assertIn("memory: inventory reports memory unreachable",
                      rendered["offlineState"])
        self.assertEqual(rendered["unavailableSlots"], 10)

    def test_paired_metric_marks_preserve_scale_cache_and_accessibility(self):  # noqa: VACUOUS_ASSERTION — paired widths and exact labels are positive controls
        rendered = self._result("rendered")
        self.assertEqual(rendered["memoryWidths"], ["100%"] * 5)
        self.assertEqual(rendered["diskWidths"], ["4%", "0%", "4%", "0%", "4%"])
        self.assertIn("Box · memory · sequential write: 101.2 MiB/s",
                      rendered["memoryAria"][0])
        self.assertIn("cache not cleared", rendered["diskValues"][1])
        self.assertIn("excluded from bar comparison", rendered["diskAria"][1])
        self.assertIn("smnc", rendered["diskClasses"][1])
        self.assertEqual(self._result("single")["memoryWidths"], ["100%"] * 5,
                         "one comparable tier must not look like the worst tier")

    def test_status_method_and_missing_state_explain_the_snapshot(self):
        rendered = self._result("rendered")
        self.assertIn("measured 1m ago", rendered["status"])
        self.assertIn("2/4 tiers available", rendered["status"])
        self.assertIn("64 MiB sequential I/O in 1 MiB blocks", rendered["method"])
        self.assertIn("4,096 random operations at 4 KiB", rendered["method"])
        self.assertIn("24 fsync samples", rendered["method"])
        self.assertIn("not a device specification. Bars use", rendered["method"])
        missing = self._result("missing")
        self.assertEqual(missing["status"], "not measured yet")
        self.assertEqual(missing["rowCount"], 1)
        self.assertEqual(missing["rowText"], "No measured values are shown.")

    def test_old_stored_view_value_migrates_and_new_ones_pass_through(self):
        migrate = self._result("migrate")
        self.assertEqual(migrate["stored"], "boxes",
                         "a device that saved the old tab name must land on "
                         "the renamed tab, not on the default view")
        self.assertEqual(migrate["kept"], "boxes")
        self.assertEqual(migrate["other"], "chat")
        self.assertIsNone(migrate["absent"])


_DRIVER = r"""
class NodeEl {
  constructor(tag) {
    this.tagName = tag; this.children = []; this._text = ""; this._classes = new Set();
    this.dataset = {}; this.style = {}; this.attrs = {}; this.colSpan = 1;
  }
  set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(" "); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  get classList() {
    return {
      add: (...names) => names.forEach(name => this._classes.add(name)),
      toggle: (name, force) => force ? this._classes.add(name) : this._classes.delete(name),
    };
  }
}
const roots = {storagestatus: new NodeEl("div"), storageRows: new NodeEl("tbody"),
               storagemethodbody: new NodeEl("div")};
const document = {createElement: tag => new NodeEl(tag)};
const $ = selector => roots[selector.slice(1)];
const byClass = (node, name) => {
  if (node._classes && node._classes.has(name)) return node;
  for (const child of node.children || []) { const found = byClass(child, name); if (found) return found; }
  return null;
};
const row = (id, box, tier, order, metrics) => ({
  id, box: box.toLowerCase(), box_label: box, tier: tier === "Memory" ? "memory" : "local-disk",
  tier_label: tier, order, status: "ok", path_label: tier === "Memory" ? "/dev/shm" : "/disk",
  metrics,
});
const fast = {seq_write_mib_s:101.2, seq_read_mib_s:202.4, random_write_iops:303,
              random_read_iops:404, fsync_p50_ms:.5, fsync_p95_ms:.9,
              seq_read_cache_evicted:true, random_read_cache_evicted:true,
              cache_evict_supported:true, free_bytes_before:8589934592};
const slow = {seq_write_mib_s:10.12, seq_read_mib_s:20.24, random_write_iops:30,
              random_read_iops:40, fsync_p50_ms:5, fsync_p95_ms:9,
              seq_read_cache_evicted:false, random_read_cache_evicted:false,
              cache_evict_supported:false};
renderStorage({state:"ok", status:"partial", measured_at:"2026-08-04T12:00:00Z", age_s:90,
  stale:false, rows:[
    {id:"offline:local-disk", box:"offline", box_label:"Offline", tier:"local-disk",
     tier_label:"Local disk", order:2, status:"unavailable", error:"inventory reports disk unreachable"},
    {id:"offline:memory", box:"offline", box_label:"Offline", tier:"memory",
     tier_label:"Memory", order:3, status:"unavailable", error:"inventory reports memory unreachable"},
    row("box:local-disk", "Box", "Local disk", 0, slow),
    row("box:memory", "Box", "Memory", 1, fast),
  ], method:{sequential_bytes:67108864, sequential_block_bytes:1048576,
    random_operations:4096, random_block_bytes:4096, fsync_operations:24,
    notes:"one bounded snapshot, not a device specification"}});
const rows = roots.storageRows.children;
const metric = tr => tr.children.slice(1);
const tierMarks = (tr, index) => metric(tr)[index].children[0].children;
const across = (tr, tier, fn) => metric(tr).map((td, index) => fn(tierMarks(tr, index)[tier]));
const width = item => { const fill = byClass(item, "smfill"); return fill ? fill.style.width : ""; };
const rendered = {
  status: roots.storagestatus.textContent, method: roots.storagemethodbody.textContent,
  rowCount: rows.length, boxes: rows.map(tr => tr.children[0].children[0].textContent),
  pairCounts: metric(rows[0]).map((td, index) => tierMarks(rows[0], index).length),
  tierLabels: tierMarks(rows[0], 0).map(item => byClass(item, "smlabel").textContent),
  diskValues: across(rows[0], 0, item => byClass(item, "smvalue").textContent),
  memoryValues: across(rows[0], 1, item => byClass(item, "smvalue").textContent),
  diskWidths: across(rows[0], 0, width), memoryWidths: across(rows[0], 1, width),
  diskAria: across(rows[0], 0, item => item.attrs["aria-label"]),
  memoryAria: across(rows[0], 1, item => item.attrs["aria-label"]),
  diskClasses: across(rows[0], 0, item => item.className),
  offlineState: rows[1].children[0].textContent,
  unavailableSlots: metric(rows[1]).flatMap((td, index) => tierMarks(rows[1], index))
    .filter(item => item._classes.has("smna")).length,
};
renderStorage({state:"ok", status:"complete", measured_at:"2026-08-04T12:00:00Z", age_s:90,
  stale:false, rows:[row("fast:memory", "Fast", "Memory", 0, fast)],
  method:{sequential_bytes:67108864, sequential_block_bytes:1048576,
    random_operations:4096, random_block_bytes:4096, fsync_operations:24}});
const singleRow = roots.storageRows.children[0];
const single = {memoryWidths: across(singleRow, 1, width)};
renderStorage({state:"missing", message:"not measured yet"});
const missing = {status: roots.storagestatus.textContent, rowCount: roots.storageRows.children.length,
                 rowText: roots.storageRows.children[0].textContent};
const migrate = {stored: canonView("storage"), kept: canonView("boxes"),
                 other: canonView("chat"), absent: canonView(null)};
console.log(JSON.stringify({rendered, single, missing, migrate}));
"""


if __name__ == "__main__":
    unittest.main()
