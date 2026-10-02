#!/usr/bin/env python3
"""The Loop editor's read model (helm/contextloop.py) and its two endpoints.

THE FIXTURE LINES ARE CLAUDE CODE'S OWN SHAPE, KEY ORDER INCLUDED. The reader
filters on raw bytes before it parses (that is what keeps a 494 MB transcript
near one second), so a fixture that put `"type":"assistant"` first, or the
`isCompactSummary` flag at the head of its line, would pass while the real
file is misread. Each builder below writes the keys in the order the harness
does: an assistant record opens with its `message` (role inside it, usage at
the end), a compact-summary user record carries its flag after the content.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import contextloop  # noqa: E402

_CACHE_AT_IMPORT = dict(contextloop._CACHE)


def tearDownModule():
    # Several classes read transcripts through the real cache; put it back
    # whole so no other unit in the same process sees this module's entries.
    contextloop._CACHE.clear()
    contextloop._CACHE.update(_CACHE_AT_IMPORT)


SESSION = "11111111-2222-3333-4444-555555555555"


def _ts(sec):
    return "2026-01-01T00:%02d:%02dZ" % divmod(sec, 60)


def _asst(sec, mid, cr, cw, inp=2, out=50, sidechain=False):
    return {"parentUuid": "p", "isSidechain": sidechain, "message": {
        "id": mid, "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                  "cache_read_input_tokens": cr, "output_tokens": out}},
        "type": "assistant", "uuid": "u%s" % sec, "timestamp": _ts(sec)}


def _boundary(sec, pre, post=9000, trigger="auto"):
    return {"parentUuid": None, "isSidechain": False, "type": "system",
            "subtype": "compact_boundary", "content": "Conversation compacted",
            "timestamp": _ts(sec), "compactMetadata": {
                "trigger": trigger, "preTokens": pre, "postTokens": post}}


def _attach(sec, attachment):
    return {"parentUuid": "p", "isSidechain": False, "attachment": attachment,
            "type": "attachment", "uuid": "a%s" % sec, "timestamp": _ts(sec)}


def _summary(sec, text):
    return {"parentUuid": "p", "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": text},
            "uuid": "s%s" % sec, "timestamp": _ts(sec),
            "isCompactSummary": True}


def _ups(sec, text, command="/x/helm/bin/helm-inject"):
    return _attach(sec, {"type": "hook_success", "hookName": "UserPromptSubmit",
                         "hookEvent": "UserPromptSubmit", "content": text,
                         "command": command})


def _two_compactions():
    """Three windows: start -> compaction (manual) -> compaction (auto)."""
    return [
        _asst(1, "m1", 0, 20000),
        _asst(2, "m2", 20000, 5000),
        _ups(3, "<helm>first turn text</helm>"),
        _asst(4, "m3", 25000, 7000),
        _boundary(10, 32000, trigger="manual"),
        _asst(11, "m4", 15000, 30000),
        _asst(12, "m5", 45000, 4000),
        _boundary(20, 49000),
        _attach(20, {"type": "instructions", "files": [
            {"path": os.path.expanduser("~/dev/CLAUDE.md"), "content": "a" * 270},
            {"path": os.path.expanduser("~/.claude/projects/x/memory/MEMORY.md"),
             "content": "b" * 540}]}),
        _attach(20, {"type": "invoked_skills", "skills": [
            {"name": "learn", "content": "c" * 27}]}),
        _summary(20, "s" * 2700),
        _asst(21, "m6", 15000, 26000, inp=3),
        _asst(22, "m7", 41000, 2000),
    ]


def _write(lines, tail=b""):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "wb") as f:
        for rec in lines:
            f.write(json.dumps(rec, separators=(",", ":")).encode() + b"\n")
        f.write(tail)
    return path


class ReadTest(unittest.TestCase):

    def setUp(self):
        contextloop._CACHE.clear()
        self.addCleanup(contextloop._CACHE.clear)

    def path(self, lines, tail=b""):
        p = _write(lines, tail)
        self.addCleanup(os.unlink, p)
        return p

    def test_two_compactions_yield_two_markers(self):
        d = contextloop.view(path=self.path(_two_compactions()))
        self.assertEqual(d["state"], "observed")
        self.assertEqual(len(d["C"]), 2)
        self.assertEqual([c[3] for c in d["C"]], ["manual", "auto"])
        self.assertEqual([c[1] for c in d["C"]], [32000, 49000])
        self.assertEqual(len(d["S"]), 7)
        # the series is the standalone page's shape: seconds since t0
        self.assertEqual(d["S"][0][:2], [0, 20002])
        self.assertEqual(d["C"][0][0], 9)
        # each marker carries the index of the first request after it
        self.assertEqual([c[4] for c in d["C"]], [3, 5])

    def test_a_boundary_in_the_same_second_is_placed_by_order(self):
        # The series' times are whole seconds. A compaction in the same
        # second as the request before it ties with that request on time, so
        # only its position in the transcript can say which side it is on.
        def at(rec, ts):
            rec["timestamp"] = ts
            return rec
        lines = [_asst(1, "m1", 0, 100000),
                 at(_asst(5, "m2", 100000, 50000), "2026-01-01T00:00:05.200Z"),
                 at(_boundary(5, 155000), "2026-01-01T00:00:05.700Z"),
                 _asst(6, "m3", 25000, 5000),
                 _boundary(9, 31000)]       # no request after it yet
        p = self.path(lines)
        d = contextloop.view(path=p)
        self.assertEqual(d["S"][1][0], d["C"][0][0])    # the tie is real
        self.assertEqual(d["C"][0][4], 2)
        self.assertEqual(d["C"][1][4], len(d["S"]))
        self.assertEqual(contextloop.floors(contextloop.read(p)), [30002])

    def test_the_control_one_compaction_is_one_marker(self):
        # the must-differ control for the arm above: drop one boundary and
        # the count follows, so the 2 is read off the file, not a constant
        lines = [r for r in _two_compactions()
                 if r.get("compactMetadata", {}).get("trigger") != "manual"]
        d = contextloop.view(path=self.path(lines))
        self.assertEqual(len(d["C"]), 1)

    def test_a_missing_transcript_is_no_transcript_not_a_crash(self):
        d = contextloop.view(path="/nonexistent/%s.jsonl" % SESSION)
        self.assertEqual(d["state"], "none")
        self.assertIn("no transcript", d["why"])
        self.assertEqual(d.get("S", []), [])

    def test_an_empty_transcript_is_no_transcript_not_a_crash(self):
        d = contextloop.view(path=self.path([]))
        self.assertEqual(d["state"], "none")
        self.assertIn("no transcript", d["why"])
        self.assertEqual(d["S"], [])

    def test_an_unbound_seat_is_no_transcript(self):
        with mock.patch.object(contextloop, "locate", return_value={
                "seat": "s", "session": None, "path": None, "cwd": None,
                "project": None, "why": "", "unavailable": []}):
            d = contextloop.view("s")
        self.assertEqual(d["state"], "none")
        self.assertIn("no transcript", d["why"])

    def test_one_message_split_over_records_is_one_request_last_wins(self):
        lines = [_asst(1, "m1", 0, 100, out=1), _asst(2, "m2", 100, 50),
                 _asst(3, "m1", 0, 100, out=99)]
        st = contextloop.read(self.path(lines))
        self.assertEqual(len(st["reqs"]), 2)
        self.assertEqual(st["reqs"][0][5], 99)     # last record's values
        self.assertEqual(st["reqs"][0][0], contextloop._epoch(_ts(3)))
        # control: distinct ids are distinct requests
        lines[2] = _asst(3, "m3", 0, 100, out=99)
        contextloop._CACHE.clear()
        self.assertEqual(len(contextloop.read(self.path(lines))["reqs"]), 3)

    def test_sidechain_requests_are_not_the_main_loop(self):
        lines = [_asst(1, "m1", 0, 100), _asst(2, "m2", 0, 900, sidechain=True)]
        self.assertEqual(len(contextloop.read(self.path(lines))["reqs"]), 1)

    def test_a_partial_last_line_waits_and_the_read_resumes(self):
        lines = _two_compactions()
        whole = json.dumps(_asst(30, "m8", 43000, 1000),
                           separators=(",", ":")).encode()
        p = self.path(lines, whole[:40])
        st = contextloop.read(p)
        self.assertEqual(len(st["reqs"]), 7)
        offset = st["offset"]
        with open(p, "ab") as f:
            f.write(whole[40:] + b"\n")
        st2 = contextloop.read(p)
        self.assertEqual(len(st2["reqs"]), 8)
        self.assertGreater(st2["offset"], offset)
        # resumed, not re-read: lines counted once each
        self.assertEqual(st2["lines"], len(lines) + 1)

    def test_a_replaced_file_starts_over(self):
        p = self.path(_two_compactions())
        contextloop.read(p)
        with open(p, "wb") as f:
            f.write(json.dumps(_asst(1, "z", 0, 10),
                               separators=(",", ":")).encode() + b"\n")
        self.assertEqual(len(contextloop.read(p)["reqs"]), 1)


class FloorTest(unittest.TestCase):

    def setUp(self):
        contextloop._CACHE.clear()
        self.p = _write(_two_compactions())
        self.addCleanup(os.unlink, self.p)
        os.environ["HELM_FLOOR_PARTS"] = "/nonexistent/parts_index.json"
        self.addCleanup(os.environ.pop, "HELM_FLOOR_PARTS", None)

    def test_the_floor_is_the_first_request_after_the_last_compaction(self):
        f = contextloop.floor(contextloop.read(self.p))
        self.assertEqual(f["state"], "observed")
        self.assertEqual(f["after"], "compaction")
        self.assertEqual(f["measured"]["ctx"], 3 + 15000 + 26000)
        self.assertEqual(f["prefix"], 15000)
        self.assertEqual(f["boundary"]["pre"], 49000)
        kinds = {p["name"]: (p["kind"], p["estimate"]) for p in f["parts"]}
        self.assertEqual(kinds["~/dev/CLAUDE.md"], ("claude_md", 100))
        self.assertEqual(kinds["~/.claude/projects/x/memory/MEMORY.md"],
                         ("memory", 200))
        self.assertEqual(kinds["learn"], ("skill", 10))
        self.assertEqual(kinds["compaction summary"], ("summary", 1000))
        self.assertEqual(f["remainder"], 41003 - 15000 - 1310)

    def test_parts_before_an_earlier_window_do_not_leak_forward(self):
        # control: the parts belong to the LAST window. Moving the
        # instructions before the first boundary takes them off the floor.
        lines = _two_compactions()
        inst = lines.pop(8)
        lines.insert(4, inst)
        p = _write(lines)
        self.addCleanup(os.unlink, p)
        names = [x["name"] for x in contextloop.floor(contextloop.read(p))["parts"]]
        self.assertNotIn("~/dev/CLAUDE.md", names)
        self.assertIn("learn", names)

    def test_an_exact_count_is_cited_only_for_the_same_text(self):
        d = tempfile.mkdtemp()
        idx = os.path.join(d, "parts_index.json")
        with open(idx, "w") as f:
            json.dump({"parts": {
                "skill:learn": {"chars": 27, "tokens_exact": 9},
                "claudemd:~/dev/CLAUDE.md": {"chars": 999, "tokens_exact": 77}}}, f)
        os.environ["HELM_FLOOR_PARTS"] = idx
        f = contextloop.floor(contextloop.read(self.p))
        exact = {p["name"]: p["exact"] for p in f["parts"]}
        self.assertEqual(exact["learn"], 9)
        self.assertIsNone(exact["~/dev/CLAUDE.md"])    # length differs: stale
        self.assertEqual(f["exact_source"], idx)

    def test_floors_and_median(self):
        st = contextloop.read(self.p)
        self.assertEqual(contextloop.floors(st), [45002, 41003])
        self.assertEqual(contextloop._median([3, 1, 2]), 2)
        self.assertEqual(contextloop._median([4, 1, 3, 2]), 2)
        self.assertIsNone(contextloop._median([]))


class SummarySizeTest(unittest.TestCase):

    def setUp(self):
        contextloop._CACHE.clear()
        os.environ["HELM_FLOOR_PARTS"] = "/nonexistent/parts_index.json"
        self.addCleanup(os.environ.pop, "HELM_FLOOR_PARTS", None)

    def read(self, lines):
        p = _write(lines)
        self.addCleanup(os.unlink, p)
        return contextloop.read(p)

    def test_the_summary_is_this_seats_own_median_and_says_estimate(self):
        lines = _two_compactions()
        # a second summary, after the first boundary: 540 and 2700 chars
        lines.insert(5, _summary(10, "t" * 540))
        st = self.read(lines)
        self.assertEqual(st["summaries"], [540, 2700])
        got = contextloop.summary_size(st)
        self.assertEqual(got["how"], "estimate")
        self.assertEqual(got["n"], 2)
        self.assertEqual(got["tokens"], round(1620 / 2.7))

    def test_the_panel_facts_carry_the_measured_size(self):
        p = _write(_two_compactions())
        self.addCleanup(os.unlink, p)
        d = contextloop.view(path=p)
        self.assertEqual(d["facts"]["summary_tokens"], 1000)    # 2700 / 2.7
        self.assertEqual(d["facts"]["summary"]["how"], "estimate")

    def test_no_summary_falls_back_to_the_census_figure_and_says_so(self):
        st = self.read([_asst(1, "m1", 0, 100), _boundary(5, 900),
                        _asst(6, "m2", 50, 50)])
        self.assertEqual(len(st["reqs"]), 2)        # control: the read happened
        self.assertEqual(len(st["comp"]), 1)
        got = contextloop.summary_size(st)
        self.assertEqual(got, dict(got, tokens=6700, how="fallback", n=0))
        self.assertEqual(contextloop.SUMMARY_TOKENS, 6700)

    def test_a_summary_the_census_counted_cites_the_exact_count(self):
        d = tempfile.mkdtemp()
        idx = os.path.join(d, "parts_index.json")
        with open(idx, "w") as f:
            json.dump({"parts": {"compact_summary": {
                "chars": 2700, "tokens_exact": 1100}}}, f)
        os.environ["HELM_FLOOR_PARTS"] = idx
        got = contextloop.summary_size(self.read(_two_compactions()))
        self.assertEqual(got["exact_one"], {"tokens": 1100, "chars": 2700})
        # control: a different length is not the same text
        with open(idx, "w") as f:
            json.dump({"parts": {"compact_summary": {
                "chars": 2701, "tokens_exact": 1100}}}, f)
        contextloop._CACHE.clear()
        self.assertNotIn("exact_one",
                         contextloop.summary_size(self.read(_two_compactions())))


class TurnTextTest(unittest.TestCase):

    def setUp(self):
        contextloop._CACHE.clear()
        self.p = _write([
            _ups(3, "other hook", command="/x/some-other-hook"),
            _ups(4, "<helm>the injected text</helm>"),
            _ups(50, "<helm>a later turn</helm>")])
        self.addCleanup(os.unlink, self.p)

    def test_the_turn_gets_helms_own_text_in_its_window(self):
        got = contextloop.turn_text(ts=_ts(2), path=self.p)
        self.assertEqual(got["text"], "<helm>the injected text</helm>")
        self.assertEqual(got["bytes"], len("<helm>the injected text</helm>"))
        self.assertEqual(got["command"], "helm-inject")

    def test_a_turn_with_no_record_in_its_window_says_why(self):
        got = contextloop.turn_text(ts=_ts(100), path=self.p)
        self.assertIsNone(got["text"])
        self.assertIn("no hook text", got["why"])
        # control: the same reader finds the later turn when asked for it
        self.assertEqual(contextloop.turn_text(ts=_ts(49), path=self.p)["text"],
                         "<helm>a later turn</helm>")

    def test_a_bad_ts_is_refused(self):
        got = contextloop.turn_text(ts="nope", path=self.p)
        self.assertIsNone(got["text"])
        self.assertIn("ts", got["why"])
        # control: the same path with a good ts does answer
        self.assertIsNotNone(contextloop.turn_text(ts=_ts(2), path=self.p)["text"])


class LiveWindowTest(unittest.TestCase):

    def proc(self, env, start=4242):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "77"))
        stat = "77 (claude) S" + " 0" * 18 + " %d 0 0\n" % start
        with open(os.path.join(d, "77", "stat"), "w") as f:
            f.write(stat)
        with open(os.path.join(d, "77", "environ"), "wb") as f:
            f.write(b"\0".join(("%s=%s" % kv).encode() for kv in env.items()) + b"\0")
        return d

    def test_the_window_is_read_off_the_live_process(self):
        d = self.proc({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
                       "SECRET_TOKEN": "never-leaves"})
        with mock.patch.object(contextloop, "_ledger_runtime",
                               return_value=(77, "4242")):
            got = contextloop.live_window(SESSION, proc_dir=d)
        self.assertEqual(got["value"], 400000)
        self.assertNotIn("never-leaves", json.dumps(got))

    def test_a_recycled_pid_does_not_answer(self):
        d = self.proc({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, start=9)
        with mock.patch.object(contextloop, "_ledger_runtime",
                               return_value=(77, "4242")):
            got = contextloop.live_window(SESSION, proc_dir=d)
        self.assertIsNone(got["value"])
        self.assertIn("exited", got["source"])

    def test_an_unset_window_says_the_default_applies(self):
        d = self.proc({"PATH": "/bin"})
        with mock.patch.object(contextloop, "_ledger_runtime",
                               return_value=(77, "4242")):
            got = contextloop.live_window(SESSION, proc_dir=d)
        self.assertIsNone(got["value"])
        self.assertIn("default", got["source"])

    def test_budgets_are_the_injectors_own_constants(self):
        from helm.inject import _common as c
        rows = {r["name"]: r["limit"] for r in contextloop.budgets()}
        self.assertEqual(rows["PINNED_BUDGET"], c.PINNED_BUDGET)
        self.assertEqual(rows["JIT_CAP"], c.JIT_CAP)

    def test_the_override_is_reported_unsettable_with_its_seam(self):
        with mock.patch.object(contextloop, "live_window",
                               return_value={"value": 200000, "source": "t"}):
            w = contextloop.window_view(None, SESSION, 50000)
        self.assertFalse(w["override"]["settable"])
        self.assertEqual(w["override"]["seam"], "task/3593")
        self.assertEqual(w["gauge_pct"], 25.0)


class EndpointTest(unittest.TestCase):

    def test_both_reads_are_get_routes_not_post(self):  # noqa: VACUOUS_ASSERTION — the identity asserts on QUERY_API and the POST_API membership of /api/inject/act are unconditional controls on the same tables the absence checks read
        from helm import web
        self.assertIn("/api/inject/act", web.POST_API)   # control: the table is the POST one
        self.assertIs(web.QUERY_API["/api/seat/contextloop"],
                      web._api_seat_contextloop)
        self.assertIs(web.QUERY_API["/api/inject/turntext"],
                      web._api_inject_turntext)
        for table in (web.API, web.POST_API):
            self.assertNotIn("/api/seat/contextloop", table)
            self.assertNotIn("/api/inject/turntext", table)

    def test_the_loop_endpoint_never_raises(self):
        from helm import web
        with mock.patch.object(contextloop, "view", side_effect=RuntimeError("x")):
            obj, status = web._api_seat_contextloop({"seat": ["s"]})
        self.assertEqual(status, 200)
        self.assertEqual(obj["state"], "none")
        self.assertEqual(obj["S"], [])

    def test_the_loop_endpoint_passes_seat_and_session(self):
        from helm import web
        with mock.patch.object(contextloop, "view",
                               return_value={"state": "none"}) as v:
            web._api_seat_contextloop({"seat": ["s"], "session": [SESSION]})
        v.assert_called_once_with("s", SESSION)


class FragmentTest(unittest.TestCase):

    def page(self):
        from helm import web_ui_loader
        return web_ui_loader.read_text()

    def test_the_loop_editor_section_hosts_and_loads_the_panel(self):
        page = self.page()
        self.assertIn("<h3>Loop editor</h3>", page)
        self.assertIn('id="loophost"', page)
        self.assertIn("async function loopLoad", page)
        self.assertIn('typeof loopLoad === "function"', page)
        self.assertIn("/api/seat/contextloop", page)
        # the inject pack now sits inside the section, after the loop panel
        sec = page[page.index('<section class="loopsec">'):]
        sec = sec[:sec.index("</section>")]
        self.assertLess(sec.index('id="loophost"'), sec.index('id="ipkhost"'))

    def test_the_pack_rows_open_their_exact_text(self):
        page = self.page()
        self.assertIn("/api/inject/turntext", page)
        self.assertIn("function ipkTextToggle", page)

    def test_the_summary_copy_follows_the_measured_fact(self):
        page = self.page()
        self.assertIn("loopK(d.facts.summary_tokens) + ' summary as output (' + esc(loopSummHow(", page)
        self.assertIn("function loopSummHow(", page)
        self.assertNotIn("38000", page[page.index("const LOOP = "):])

    def test_the_window_note_names_no_cli_verb(self):
        # RULE 2: the console never tells the owner to run a command.
        page = self.page()
        start = page.index("function loopBudgetsHtml")
        body = page[start:page.index("function loopOnPack", start)]
        self.assertIn("task/3593", body)        # control: the note is here
        for verb in ("helm ", "--", "run `", "export "):
            self.assertNotIn(verb, body)


def _lift_fn(src, name):
    """-> the verbatim `function NAME(...) {...}` from the assembled page, or
    "" when the page has no such function (an arm that calls it then fails
    on its own, and the other arms still run)."""
    import re
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    j, depth, quote, escaped = src.index("{", m.end()), 0, None, False
    while j < len(src):
        c = src[j]
        if quote:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif src.startswith("//", j):
            j = src.find("\n", j)
            continue
        elif src.startswith("/*", j):
            j = src.index("*/", j) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise AssertionError("unbalanced function " + name)


#: The simulator's own functions, lifted verbatim and run under node.
_LOOP_FNS = ("loopK", "loopN", "loopPrep", "loopWMin", "loopSim", "loopActual",
             "loopWLabel", "loopBest", "loopVs", "loopTable")

_LOOP_HARNESS = r"""
const els = {};
function $(sel) { return els[sel] || (els[sel] = {innerHTML: "", value: null}); }
function esc(s) { return String(s == null ? "" : s); }
const LOOP = {st: {}};
/*__FNS__*/
const PRICE = {cache_read: 0.1, cache_write: 1.25, output: 5};
const FACTS = {fire_pct: 74, summary_tokens: 6700, price: PRICE};
function rows(ctxs, step) {
  return ctxs.map((c, i) => [i * step, c, c, 0, 0, 0]);
}
const flat = loopPrep({S: rows(Array(50).fill(100000), 60), C: [], facts: FACTS});
const grow = loopPrep({S: rows(Array.from({length: 50}, (_, i) => 100000 + i * 10000), 60),
                       C: [], facts: FACTS});
const out = [];
function arm(name, fn) {
  try { out.push(Object.assign({name}, fn())); }
  catch (e) { out.push({name, pass: false, detail: String(e && e.stack || e)}); }
}
arm("no_thrash_when_fire_at_or_below_floor", () => {
  const s = loopSim(flat, 120000, 74, 106000, 0), g = loopSim(grow, 120000, 74, 106000, 0);
  return {pass: s.n === 0 && s.fire > 106000 && g.n > 0,
          detail: "flat n=" + s.n + " fire=" + s.fire + "; growing n=" + g.n};
});
arm("clamped_window_says_so", () => {
  const lo = loopWLabel({W: 120000, F: 106000}, loopSim(flat, 120000, 74, 106000, 0));
  const hi = loopWLabel({W: 400000, F: 106000}, loopSim(flat, 400000, 74, 106000, 0));
  return {pass: lo.includes("raised to 150k") && lo.includes("fires at 111k")
                && !hi.includes("raised") && hi.includes("fires at 296k"),
          detail: lo + " | " + hi};
});
arm("no_summary_share_without_compactions", () => {
  const s = loopSim(flat, 1000000, 74, 106000, 0), g = loopSim(grow, 160000, 74, 106000, 0);
  return {pass: s.n === 0 && s.share === 0 && g.n > 0 && g.share > 0,
          detail: "flat n=" + s.n + " share=" + s.share + "; growing n=" + g.n + " share=" + g.share};
});
arm("same_second_boundary_is_a_crossing", () => {
  const P = loopPrep({S: [[0, 100000, 0, 0, 0, 0], [5, 150000, 0, 0, 0, 0],
                          [5, 30000, 0, 0, 0, 0], [6, 32000, 0, 0, 0, 0]],
                      C: [[5, 155000, 25000, "auto", 2]], facts: FACTS});
  return {pass: P.delta[1] === 50000 && P.delta[2] === 0 && P.delta[3] === 2000,
          detail: Array.from(P.delta).join(",")};
});
arm("an_exact_tie_says_the_same_not_zero_percent", () => {
  /* a cost exactly equal to the 1M reference must not print '0% more than
     1M' (task/4058): it is not more, and it is not less either. The controls
     keep both ordinary directions, and the footer must not pair 'the same'
     with a compactions ratio that is also 1.0x. */
  const flat2 = loopPrep({S: rows(Array(10).fill(106000), 9600), C: [], facts: FACTS});
  LOOP.P = flat2; LOOP.st = {W: 1000000, F: 106000, G: 0, M: 10, unit: "priced"};
  LOOP.cur = loopSim(flat2, 1000000, 76, 106000, 0);
  loopTable();
  const html = $("#loopcmp").innerHTML;
  const tie = loopVs(500, 500), less = loopVs(400, 500), more = loopVs(600, 500);
  return {pass: tie === "the same as 1M" && less === "20% less than 1M"
                && more === "20% more than 1M"
                && html.includes("At your knobs: the same as 1M.")
                && !html.includes("0% "),
          detail: tie + " | " + less + " | " + more + " | foot: "
                  + html.slice(html.indexOf("loopfoot"), html.indexOf("loopfoot") + 120)};
});
arm("a_rounded_tie_keeps_a_different_compaction_count", () => {
  /* a cost within half a percent of 1M reads 'the same', but the knobs can
     still compact more often than 1M: the footer keeps that ratio and drops
     it only when it is 1.0x as well. */
  const steep = loopPrep({S: rows(Array.from({length: 50}, (_, i) => 100000 + i * 20000), 60),
                          C: [], facts: FACTS});
  LOOP.P = steep; LOOP.st = {W: 1000000, F: 106000, G: 0, M: 10, unit: "priced"};
  const ref = loopSim(steep, 1000000, 76, 106000, 0);
  LOOP.cur = Object.assign({}, ref, {pr: ref.pr * 1.002, raw: ref.raw * 1.002, n: ref.n * 2});
  loopTable();
  const html = $("#loopcmp").innerHTML;
  const want = "At your knobs: the same as 1M, with 2.0× the compactions.";
  return {pass: ref.n > 0 && html.includes(want),
          detail: "1M n=" + ref.n + " | " + want + " | foot: "
                  + html.slice(html.indexOf("loopfoot"), html.indexOf("loopfoot") + 120)};
});
arm("knobs_that_compact_where_1m_never_does_say_so", () => {
  /* 1M never compacts on a flat seat, so a ratio against it is undefined: the
     footer must not print the knobs' count as 'N.0x the compactions' (one
     compaction even read as 1.0x and was hidden), and must not print '0.0x'
     when neither compacts. */
  const flat4 = loopPrep({S: rows(Array(10).fill(106000), 9600), C: [], facts: FACTS});
  LOOP.P = flat4; LOOP.st = {W: 1000000, F: 106000, G: 0, M: 10, unit: "priced"};
  const ref = loopSim(flat4, 1000000, 76, 106000, 0);
  const foot = (n, k) => {
    LOOP.cur = Object.assign({}, ref, {pr: ref.pr * k, raw: ref.raw * k, n});
    loopTable();
    const h = $("#loopcmp").innerHTML;
    return h.slice(h.indexOf("At your knobs"), h.indexOf("</td>", h.indexOf("At your knobs")));
  };
  const two = foot(2, 1.002), one = foot(1, 1.002), none = foot(0, 0.8);
  return {pass: ref.n === 0
                && two === "At your knobs: the same as 1M, with 2 compactions where 1M has none."
                && one === "At your knobs: the same as 1M, with 1 compaction where 1M has none."
                && none === "At your knobs: 20% less than 1M.",
          detail: "1M n=" + ref.n + " | " + two + " | " + one + " | " + none};
});
arm("best_window_costing_more_says_more", () => {
  /* one jump at the last request: every window under 1M fires on it, 1M
     (at 76%) does not, so with a heavy re-grounding cost the best window
     costs MORE than 1M; with none it costs less (the control) */
  const P = loopPrep({S: rows(Array(9).fill(106000).concat([745000]), 9600),
                      C: [], facts: FACTS});
  const line = G => {
    LOOP.P = P; LOOP.st = {W: 400000, F: 106000, G, M: 2, unit: "priced"};
    LOOP.cur = loopSim(P, 400000, 74, 106000, G);
    loopTable();
    return $("#loopbest").innerHTML;
  };
  const heavy = line(60), light = line(0);
  return {pass: heavy.includes("% more than 1M") && !/-\d+%/.test(heavy)
                && light.includes("% less than 1M"),
          detail: heavy + " | " + light};
});
console.log(JSON.stringify(out));
"""


class LoopSimRuntimeTest(unittest.TestCase):
    """The Loop editor's simulator (56-loop.js.part), executed under node."""

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess
        from helm import web_ui_loader
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_lift_fn(src, n) for n in _LOOP_FNS)
        cls.tmp = tempfile.mkdtemp(prefix="helm-loop-runtime-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_LOOP_HARNESS.replace("/*__FNS__*/", fns))
        cls.proc = subprocess.run([node, path], capture_output=True, text=True,
                                  timeout=30)
        try:
            cls.results = {r["name"]: r for r in json.loads(cls.proc.stdout or "[]")}
        except ValueError:
            cls.results = {}

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def arm(self, name):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIn(name, self.results, "stdout=%r stderr=%r"
                      % (self.proc.stdout, self.proc.stderr))
        r = self.results[name]
        self.assertTrue(r["pass"], r["detail"])

    def test_a_window_that_fires_at_or_below_the_floor_does_not_thrash(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("no_thrash_when_fire_at_or_below_floor")

    def test_a_clamped_window_says_so(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("clamped_window_says_so")

    def test_no_compaction_means_no_summary_share(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("no_summary_share_without_compactions")

    def test_a_same_second_boundary_is_a_crossing(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("same_second_boundary_is_a_crossing")

    def test_a_rounded_tie_keeps_a_different_compaction_count(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("a_rounded_tie_keeps_a_different_compaction_count")

    def test_knobs_that_compact_where_1m_never_does_say_so(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("knobs_that_compact_where_1m_never_does_say_so")

    def test_a_best_window_that_costs_more_says_more(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("best_window_costing_more_says_more")

    def test_an_exact_tie_says_the_same_not_zero_percent(self):  # noqa: VACUOUS_ASSERTION — arm() asserts node exited 0 and the named arm ran and passed; each arm holds its own control
        self.arm("an_exact_tie_says_the_same_not_zero_percent")


class LoopPanelLayoutTest(unittest.TestCase):
    """The owner-surface overflow cure (task/4058), on the SHIPPED parts:
    #loopcmp scrolls inside its own wrapper and the sliders can shrink."""

    def test_the_comparison_table_sits_in_an_overflow_wrapper(self):
        from helm import web_ui_loader
        js = web_ui_loader.read_text()
        self.assertIn('<div class="loopcmpwrap"><table class="looptab" '
                      'id="loopcmp"></table></div>', js)

    def test_the_wrapper_owns_overflow_x_and_the_sliders_shrink(self):  # noqa: VACUOUS_ASSERTION — positive control: the pre-cure source lacked .loopcmpwrap and min-width:0, so these regexes could not match it; they can only pass on the real rule bodies
        from helm import web_ui_loader
        css = web_ui_loader.read_text()
        self.assertRegex(css, r"\.loopcmpwrap\{[^}]*overflow-x:auto")
        self.assertRegex(css,
                         r"\.loopknob input\[type=range\]\{[^}]*min-width:0")

    def test_every_looptab_keeps_its_bottom_margin(self):  # noqa: VACUOUS_ASSERTION — positive control: the wrapper moved the margin off .looptab, so the floor and budget tables (not wrapped) lost it; this regex fails on that rule body
        from helm import web_ui_loader
        css = web_ui_loader.read_text()
        self.assertRegex(css, r"\.looptab\{[^}]*margin:0 0 6px")


if __name__ == "__main__":
    unittest.main()
