#!/usr/bin/env python3
"""Runtime contract for the ledger's measured delta-DOM cold path."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from helm import web_ui_loader
from tests.test_web_chat_client_runtime import _extract_fn

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "ledger_runtime_harness.js")
EXTRACT = [
    "ledgerAgeHTML", "ledgerAges", "ledgerHTML", "nativeHead", "nativeChat",
    "nativeRows", "caveActivity", "caveActivityTitle", "cavePulseClock",
    "cavePulse", "ledgerStrip", "ledgerBadges", "ledgerEmptyClass",
    "ledgerTurns", "seatState", "ledgerCellClock", "ledgerCells",
]


class TestLedgerClientRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, name) for name in EXTRACT)
        with open(HARNESS, encoding="utf-8") as f:
            template = f.read()
        assert "/*__INJECT__*/" in template
        cls.tmp = tempfile.mkdtemp(prefix="helm-ledger-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(template.replace("/*__INJECT__*/", fns))
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.results = {r["name"]: r for r in json.loads(cls.proc.stdout or "[]")}
        except json.JSONDecodeError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_unchanged_poll_keeps_rows_and_refreshes_ages(self):  # noqa: VACUOUS_ASSERTION — firstWrites==7 proves every panel mounted before secondWrites==0 asserts the unchanged-poll absence
        """The 25-minute live trace saw both ledger trees replaced 501 times.

        Drive every real renderer twice with the same payload. The first poll
        must mount all seven panels, the second must replace none, relative ages
        must still advance, and a real data change must replace its panel.
        """
        self.assertTrue(self.proc.stdout.strip(),
                        "runtime harness produced no output: " + self.proc.stderr)
        self.assertIn("unchanged_poll_keeps_rows_and_refreshes_ages", self.results)
        r = self.results["unchanged_poll_keeps_rows_and_refreshes_ages"]
        d = r["detail"]
        self.assertEqual(d["firstWrites"], 7, d)
        self.assertEqual(d["secondWrites"], 0, d)
        for kind in ("ages", "pulses", "states"):
            self.assertGreater(d["secondDynamicWrites"][kind], 0, d)
        self.assertIn("12s ago", d["firstTexts"])
        self.assertIn("1m ago", d["secondTexts"])
        self.assertNotEqual(d["firstPulse"]["bars"], d["secondPulse"]["bars"])
        self.assertEqual(d["secondPulse"]["span"], "3m")
        self.assertIn("peak 1 in one block", d["secondPulse"]["title"])
        self.assertEqual(d["firstState"], {
            "dot": "ldot active", "badge": "lbadge final", "label": "active"})
        self.assertEqual(d["secondState"], {
            "dot": "ldot quiet", "badge": "lbadge tent", "label": "quiet"})
        self.assertEqual(d["changedWrites"], 1, d)
        self.assertEqual(d["degradedFirstWrites"], 1, d)
        self.assertEqual(d["degradedSecondWrites"], 0, d)
        self.assertEqual(d["degradedAgeBefore"], "12s ago", d)
        self.assertEqual(d["degradedAgeAfter"], "15s ago", d)
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_every_poll_renderer_uses_the_delta_boundary(self):  # noqa: VACUOUS_ASSERTION — seven unconditional assertIn controls precede each direct-write absence assertion on the same extracted body
        """One forgotten direct assignment recreates the measured churn."""
        src = web_ui_loader.read_text()
        bodies = {name: _extract_fn(src, name) for name in
                  ("nativeHead", "nativeChat", "nativeRows", "ledgerStrip",
                   "ledgerTurns", "ledgerCells", "pollLedger")}
        self.assertEqual(len(bodies), 7)
        for name, body in bodies.items():
            self.assertIn("ledgerHTML(", body, name)
            self.assertNotIn(".innerHTML =", body, name)
            self.assertNotIn(".textContent =", body, name)

    def test_poll_wires_each_clock_owner_after_mount(self):  # noqa: VACUOUS_ASSERTION — unconditional pulse declaration control precedes order assertions, and index raises on every missing call
        body = _extract_fn(web_ui_loader.read_text(), "pollLedger")
        self.assertIn('const pulse = $("#cavepulse")', body)
        self.assertLess(body.index("refreshLedgerClocks()"),
                        body.index('await j("/api/ledger/native", 2500)'))
        self.assertIn('await j("/api/ledger", 2500)', body)
        mount = body.index("ledgerHTML(pulse, cavePulse(")
        refresh = body.index("cavePulseClock(pulse)")
        cells = body.index("ledgerCells(d.cells)")
        self.assertLess(mount, refresh)
        self.assertLess(refresh, cells)
        self.assertLess(body.index("ledgerMode(d.transport)"),
                        body.rindex("nativeChat(n.chat)"))

    def test_failure_clock_refresh_reaches_every_age_owner(self):
        fn = _extract_fn(web_ui_loader.read_text(), "refreshLedgerClocks")
        code = """
const ages = [], clocks = [];
function $(id) { return id; }
function ledgerAges(id) { ages.push(id); }
function cavePulseClock(id) { clocks.push(["pulse", id]); }
function ledgerCellClock(id) { clocks.push(["cells", id]); }
""" + fn + """
refreshLedgerClocks();
console.log(JSON.stringify({ages, clocks}));
"""
        p = subprocess.run([self.node, "-e", code], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {
            "ages": ["#ledgerstrip", "#ledgerturns", "#ledgercells",
                     "#nativehead", "#nativechat", "#ledgernative"],
            "clocks": [["pulse", "#cavepulse"], ["cells", "#ledgercells"]],
        })

    def test_native_chat_uses_this_polls_transport_mode(self):
        fn = _extract_fn(web_ui_loader.read_text(), "pollLedger")
        code = """
let LEDGER_SIGNED = true, LEDGER_HEAD = null, NATIVE_COUNT = null;
let LEDGER_NAMES = {};
const order = [];
function $() { return {}; }
function refreshLedgerClocks() {}
async function j(url) {
  return url.endsWith("/native")
    ? {chain: {count: 1}, chat: {}, events: []}
    : {transport: {mode: "degraded"}, offline: false, turns: [], cells: []};
}
function nativeHead() {}
function nativeRows() {}
function nativeChat() { order.push(["chat", LEDGER_SIGNED]); }
function ledgerStrip() {}
function ledgerMode(tp) {
  LEDGER_SIGNED = tp.mode === "signed";
  order.push(["mode", LEDGER_SIGNED]);
}
function ledgerTurns() {}
function ledgerHTML() {}
function cavePulse() { return ""; }
function cavePulseClock() {}
function ledgerCells() {}
""" + fn + """
pollLedger().then(() => console.log(JSON.stringify(order)))
  .catch(e => { console.error(e); process.exit(1); });
"""
        p = subprocess.run([self.node, "-e", code], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [["mode", False], ["chat", False]])

    def test_the_dregg_strip_refreshes_age_without_structural_churn(self):
        src = web_ui_loader.read_text()
        body = _extract_fn(src, "pollDregg")
        self.assertIn("ledgerAgeHTML(t.timestamp)", body)
        self.assertIn("ledgerHTML(el,", body)
        self.assertNotIn("el.innerHTML =", body)
        self.assertNotIn("DREGG_SIG", body)
        self.assertLess(body.index("ledgerAges(el)"), body.index('await j("/api/ledger", 6000)'))
        self.assertIn("run !== DREGG_RUN", body)

    def test_the_independent_dregg_timer_has_a_real_lifecycle(self):
        """Live attribution can stop and restart every timer-owned call."""
        src = web_ui_loader.read_text()
        fns = "\n".join(_extract_fn(src, name) for name in
                        ("stopDreggPolling", "startDreggPolling"))
        code = """
let DREGG_TIMER = null, DREGG_RUN = 0, DREGG_POLLING = false;
let intervals = 0, polls = 0, cleared = [];
function setInterval(fn, ms) { intervals++; return intervals; }
function clearInterval(id) { cleared.push(id); }
function pollDregg() { polls++; }
""" + fns + """
startDreggPolling(); startDreggPolling();
stopDreggPolling(); stopDreggPolling();
startDreggPolling(); stopDreggPolling();
console.log(JSON.stringify({intervals, polls, cleared, timer: DREGG_TIMER}));
"""
        p = subprocess.run([self.node, "-e", code], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {
            "intervals": 2, "polls": 2, "cleared": [1, 2], "timer": None})

    def test_a_stopped_hung_dregg_call_cannot_latch_its_restart(self):
        src = web_ui_loader.read_text()
        fns = "\n".join(_extract_fn(src, name) for name in
                        ("stopDreggPolling", "pollDregg"))
        code = """
let DREGG_TIMER = null, DREGG_RUN = 0, DREGG_POLLING = false;
const pending = []; let calls = 0;
const document = {hidden: false};
function clearInterval() {}
function ledgerAges() {}
function $(sel) { return {}; }
function j(url, ms) { calls++; return new Promise((ok, no) => pending.push(no)); }
""" + fns + """
(async () => {
  pollDregg(); await Promise.resolve();
  stopDreggPolling(); pollDregg(); await Promise.resolve();
  pending[0](new Error("old")); await new Promise(setImmediate);
  const afterOld = DREGG_POLLING;
  pending[1](new Error("new")); await new Promise(setImmediate);
  console.log(JSON.stringify({calls, afterOld, afterNew: DREGG_POLLING}));
})().catch(e => { console.error(e); process.exit(1); });
"""
        p = subprocess.run([self.node, "-e", code], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {
            "calls": 2, "afterOld": True, "afterNew": False})


class LedgerHeaderRuntimeTest(unittest.TestCase):
    """Run the assembled header and same-question consumers, not a Python mirror.

    Only DOM elements are stand-ins, as in CardRuntimeBase. Production mode,
    row, badge, empty-state and incident renderers all execute unchanged.
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        constants = [line for line in src.splitlines()
                     if line.startswith(("const esc = ", "const lsh = "))]
        cls.source = "\n".join(constants + [_extract_fn(src, name) for name in (
            "ledgerMode", "ledgerEmptyClass", "ledgerTurns", "ledgerBadges",
            "nativeChat", "ledgerStrip", "ledgerAgeHTML", "ledgerHTML",
            "ledgerAges")])

    def render(self, steps):
        code = self.source + r'''
let LEDGER_SIGNED = null, LEDGER_HEAD = null, NATIVE_COUNT = null;
const LEDGER_NAMES = {}, nodes = {}, out = [];
let moves = 0, writes = 0;
const parent = {insertBefore() { moves++; }};
function $(id) {
  if (!nodes[id]) {
    let text = "";
    nodes[id] = {innerHTML: "", querySelectorAll() { return []; }, parentNode: parent,
      get textContent() { return text; },
      set textContent(value) { text = value; writes++; }};
  }
  return nodes[id];
}
for (const step of JSON.parse(require("fs").readFileSync(0, "utf8"))) {
  const beforeWrites = writes, beforeMoves = moves;
  ledgerMode(step.transport);
  ledgerTurns(step.turns || []);
  nativeChat({msgs: 1, rooms: 1, last_ts: 1, last_from: "seat", last_room: "helm"});
  ledgerStrip({transport: step.transport, status: {dag_height: 1}, node: "test-node"});
  out.push({header: $("#cavesub").textContent, local: $("#nativesub").textContent,
    rows: $("#ledgerturns").innerHTML, pulse: $("#nativechat").innerHTML,
    strip: $("#ledgerstrip").innerHTML, head: LEDGER_HEAD,
    writes: writes - beforeWrites, moves: moves - beforeMoves});
}
process.stdout.write(JSON.stringify(out));
'''
        p = subprocess.run([self.node, "-e", code], input=json.dumps(steps),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_every_transport_mode_names_its_own_evidence(self):
        modes = ["signed", "degraded", "ready", "unsigned",
                 "unsigned (no signer)", "unexpected", None, "constructor"]
        expected = ["signing is ON", "signing is degraded", "ready (unproven)",
                    "off for this transport", "off (no signer)",
                    "status is unknown", "status is unknown", "status is unknown"]
        rows = self.render([{"transport": {"mode": mode}} for mode in modes])
        for mode, row, label in zip(modes, rows, expected):
            with self.subTest(mode=mode):
                self.assertIn(label, row["header"])
                self.assertIn("local activity is a separate record", row["local"])
                self.assertIn("each post’s committed signing evidence", row["pulse"])
                for field in ("header", "local", "rows", "pulse", "strip"):
                    for false_claim in ("no new rows are being created",
                                        "only record being written",
                                        "chatter, unsigned",
                                        "RAM chat still delivers unsigned"):
                        self.assertNotIn(false_claim, row[field])
        missing = self.render([{}])[0]
        self.assertIn("status is unknown", missing["header"])
        self.assertIn("does not establish", missing["rows"])

    def test_non_signed_transitions_and_profile_changes_refresh_header(self):
        rows = self.render([{"transport": tp} for tp in (
            {"mode": "ready"}, {"mode": "degraded", "profile": "seat-a"},
            {"mode": "degraded", "profile": "seat-b"}, None,
            {"mode": "unsigned"}, {"mode": "signed"})])
        for row, text in zip(rows, ("ready", "seat-a", "seat-b", "unknown", "off", "ON")):
            self.assertIn(text, row["header"])
            self.assertGreater(row["writes"], 0)
        self.assertEqual([r["moves"] for r in rows], [1, 0, 0, 0, 0, 1])

    def test_same_mode_poll_keeps_dom_but_updates_head_evidence(self):
        rows = self.render([{"transport": {"mode": "degraded", "profile": "seat-a",
                                            "head": head}} for head in (0, 12)])
        self.assertGreater(rows[0]["writes"], 0)
        self.assertEqual(rows[1]["writes"], 0)
        self.assertEqual(rows[1]["moves"], 0)
        for row, head in zip(rows, (0, 12)):
            self.assertEqual(row["head"], head)
            self.assertIn("entries up to #" + str(head), row["rows"])
            self.assertIn("this read found none", row["rows"])

    def test_degraded_incident_does_not_erase_signed_row_evidence(self):
        tp = {"mode": "degraded", "profile": "seat-a", "reason": "send timed out",
              "first_failure": "2026-09-13T07:40:00Z",
              "last_failure": "2026-09-13T07:40:00Z", "head": 38}
        turn = {"chain_index": 38, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
                "agent": "c" * 64, "executor_signed": True, "timestamp": 1}
        row = self.render([{"transport": tp, "turns": [turn]}])[0]
        self.assertIn("committed entries may still arrive", row["header"])
        self.assertIn("#38", row["rows"])
        self.assertIn("exec-signed", row["rows"])
        self.assertIn("DEGRADED", row["strip"])
        for incident in ("seat-a", "send timed out", "2026-09-13T07:40:00Z"):
            self.assertIn(incident, row["strip"])
        unsigned = dict(turn, executor_signed=False)
        negative = self.render([{"transport": tp, "turns": [unsigned]}])[0]
        self.assertIn("#38", negative["rows"])
        self.assertNotIn("exec-signed", negative["rows"])

    def test_empty_read_never_overrides_a_readable_head(self):
        rows = self.render([{"transport": {"mode": mode, "head": 0}}
                            for mode in ("signed", "degraded", "ready", "unsigned")])
        for row in rows:
            self.assertIn("entries up to #0", row["rows"])
            self.assertIn("this read found none", row["rows"])
        signed, unknown = self.render([{"transport": {"mode": "signed"}}, {}])
        self.assertIn("signing is available", signed["rows"])
        self.assertIn("does not establish", unknown["rows"])


if __name__ == "__main__":
    unittest.main()
