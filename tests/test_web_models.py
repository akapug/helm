#!/usr/bin/env python3
"""FLEET › MODELS (task/3448): the model scorecard's one home on the page.

The AX is `helm eval board`; the UX mirrors it through /api/models, the same
function. The page is a ranked table the owner reads as totals — score, band,
n, the benchmark prior beside it — with each model opening its detail in
place, and the apprenticeship rungs under it as evidence, never a promotion.
It links both ways with Fleet › credit (supply), and each project's Team tab
links a seat to its model here.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helm import scorecard, web_ui_loader
from tests.test_scorecard import NOW, TRUNK, codex3_rows, grok_lanes, ledger
from tests.test_web_chat_client_runtime import _extract_fn


def _view(src, v):
    m = re.search(r'<div class="view(?: on)?" id="view-%s">' % v, src)
    assert m, "no view-%s in the page" % v
    nxt = src.find('<div class="view', m.end())
    return src[m.start():nxt if nxt > 0 else src.index("<script>")]


class ModelsPlacementTest(unittest.TestCase):
    def setUp(self):
        self.src = web_ui_loader.read_text()

    def test_models_is_a_fleet_section_right_after_credit(self):
        fleet = self.src[self.src.index('<div class="navsec" data-a="fleet">'):]
        fleet = fleet[:fleet.index("</div>")]
        tabs = re.findall(r'data-v="([a-z]+)"', fleet)
        self.assertEqual(tabs[:2], ["quota", "models"])
        self.assertIn('models: "fleet"', self.src)
        self.assertIn('"models"', self.src[self.src.index("const VIEWS = "):][:200])
        self.assertIn('id="view-models"', self.src)

    def test_the_page_reads_the_verbs_own_answer(self):
        from helm import web
        self.assertIn("/api/models", web.QUERY_API)
        self.assertIn('j("/api/models', _extract_fn(self.src, "loadModels"))

    def test_models_and_credit_link_both_ways(self):
        self.assertIn('href="#models"', _view(self.src, "quota"))
        self.assertIn('href="#quota"', _view(self.src, "models"))

    def test_the_models_address_rides_the_one_view_router(self):
        """#models?model= and #models?seat= go through viewQuery/viewRoute
        (task/3445 L4), the one reader and router every view's query uses:
        showView, the boot and a followed link hand the page the same state,
        and no second routing path names the models page."""
        self.assertIn('v === "models" ? modelsQuery()', _extract_fn(self.src, "viewQuery"))
        self.assertIn('v === "models") modelsRoute(query)', _extract_fn(self.src, "viewRoute"))
        # the definition and the router's call, nothing else
        self.assertEqual(self.src.count("modelsRoute("), 2)
        self.assertEqual(self.src.count("modelsQuery("), 2)
        self.assertNotIn("modelsHash", self.src)
        self.assertNotIn("modelsQuery", _extract_fn(self.src, "showView"))


class ModelsRuntimeTest(unittest.TestCase):
    """The page's renderers over the verb's own board, lifted and run."""

    DRIVER = r"""
const out = {};
out.table = modelsTableHTML(BOARD);
out.unread = modelsTableHTML(null);
out.down = modelsTableHTML({unavailable: "the dispatch ledger could not be read"});
const Q = BOARD.models.find(m => m.model === "qwen27");
out.detail = modelDetailHTML(Q, BOARD);
out.claude = modelDetailHTML(BOARD.models.find(m => m.model === "claude"), BOARD);
out.rungs = modelsRungsHTML(BOARD.rungs, BOARD);
MODELS_OPEN = "qwen27";
out.query = modelsQuery();
modelsRoute("model=claude");
out.routed = MODELS_OPEN;
modelsRoute("seat=qwenlocal");
out.by_seat = MODELS_OPEN;
modelsRoute("");
out.none = [MODELS_OPEN, modelsQuery()];
out.member = teamMemberRow("alpha", {seat: "alpha-codex", family: "codex", state: "live", role: "reviewer"}, 0, {reviewer: ["review"]});
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "modelsPct", "modelsDur", "modelsCell", "modelsTableHTML", "modelsUnnamed",
            "modelDetailHTML", "modelsRungsHTML", "modelsQuery", "modelsRoute",
            "modelsResolve", "teamMemberRow"))
        board = cls.board = scorecard.board(ledger(), TRUNK, now=NOW, window_s=30 * 86400)
        support = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
                   '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n'
                   "let MODELS_OPEN = null, MODELS_SEAT = null;\nfunction modelsRender() {}\n"
                   "function viewHashSync() {}\n"
                   "const BOARD = " + json.dumps(board) + ";\nlet MODELS = BOARD;\n")
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-models-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", path], capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, path], capture_output=True, text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: " + (self.proc.stderr or "")[:1200])

    def text(self, key):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", self.out[key]))

    def test_the_table_leads_with_its_totals_then_ranks_every_model(self):
        text = self.text("table")
        self.assertRegex(text, r"\d+ models ranked")
        self.assertIn("lanes in 30d", text)
        self.assertIn("with a public benchmark", text)
        for name in ("qwen27", "qwenlocal", "claude", "gpt-5.6-sol"):
            self.assertIn('data-model="%s"' % name, self.out["table"], name)

    def test_each_row_shows_the_score_its_band_its_n_and_the_prior_beside_it(self):
        row = self.out["table"][self.out["table"].index('data-model="qwen27"'):]
        row = re.sub(r"<[^>]*>", " ", row[:row.index("</tr>")])
        self.assertRegex(row, r"\d+\s*–\s*\d+")            # the band
        self.assertIn("73", row)                           # its maker's Terminal-Bench 2.1
        self.assertIn("prior only", row)                   # n under the floor
        claude = self.out["table"][self.out["table"].index('data-model="claude"'):]
        self.assertIn("no prior", claude[:claude.index("</tr>")])

    def test_a_model_with_no_evidence_reads_not_scored_with_no_band_or_rank(self):  # noqa: VACUOUS_ASSERTION — the band's absence is read off the same row whose score cell must equal "not scored" first, and the qwen27 arm above proves the band pattern matches a real band
        """No number without evidence (task/3448): claude here has no public
        benchmark and no decided lane of ours, so its row shows no score, no
        band and no rank — never the fleet's pooled rate — and sorts last."""
        rows = re.findall(r'<tr class="mrow[^"]*" data-model="([^"]+)"', self.out["table"])
        self.assertEqual(rows[-1], "claude")
        row = self.out["table"][self.out["table"].index('data-model="claude"'):]
        row = row[:row.index("</tr>")]
        cells = [re.sub(r"<[^>]*>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row)]
        self.assertEqual(cells[0], "")                          # no rank
        self.assertEqual(cells[2], "not scored")                # no score, no band
        self.assertNotRegex(row, r"\d+\s*–\s*\d+")

    def test_the_totals_count_ranked_and_not_scored_models_apart(self):
        text = self.text("table")
        scored = [m for m in self.board["models"] if m["score"]["state"] != "not scored"]
        self.assertIn("%d models ranked" % len(scored), text)
        self.assertIn("%d not scored" % (len(self.board["models"]) - len(scored)), text)

    def test_the_detail_of_a_model_with_no_evidence_names_no_number(self):
        text = self.text("claude")
        self.assertIn("not scored", text)
        self.assertNotIn("pooled", text)

    def test_lanes_with_no_model_are_one_footnote_never_a_row(self):
        """"unknown" is not a model (task/3448): the lanes the ledger names no
        model for are one line under the table."""
        table = self.out["table"]
        self.assertIn('data-model="qwen27"', table)
        self.assertNotIn('data-model="unknown"', table)
        foot = re.sub(r"<[^>]*>", " ", table[table.index("</table>"):])
        self.assertRegex(foot, r"1 lane carries no model on the ledger")
        self.assertIn("seat-c", foot)

    def test_the_totals_say_which_lanes_they_count(self):
        """The totals line names every lane in the window and, apart, the
        decided ones the scores rest on, as `helm eval board` does."""
        text = self.text("table")
        self.assertIn("%d lanes in 30d" % self.board["lanes"], text)
        self.assertIn("%d decided" % self.board["decided"], text)
        self.assertIn('<th class="mnum">decided</th>', self.out["table"])

    def test_the_detail_counts_reviews_by_family_never_a_rounded_zero(self):
        text = self.text("detail")
        self.assertIn("3 reviews over 2 lands (anthropic 1, codex 2)", text)
        self.assertIn("1 patch over 2 lands (anthropic 1)", text)

    def test_the_table_reads_as_totals_not_agent_jargon(self):
        text = self.text("table")
        for word in ("helm ", "dispatch", "verdict", "chain", "FIX", "posterior"):
            self.assertNotIn(word, text, word)

    def test_unread_and_unreadable_say_so_never_an_empty_table(self):
        self.assertIn("not read yet", self.out["unread"])
        self.assertIn("could not be read", self.out["down"])

    def test_the_detail_names_the_prior_its_source_and_the_makers_harness(self):
        d = self.out["detail"]
        self.assertIn('href="https://', d)
        self.assertIn('rel="noopener"', d)
        self.assertIn("maker", self.text("detail"))
        self.assertIn("Terminal-Bench 2.1", d)
        for part in ("lands without a patch", "reviews per land", "hand back", "UNKNOWN"):
            self.assertIn(part, self.text("detail"), part)
        self.assertIn("no public benchmark", self.text("claude"))

    def test_the_rungs_show_evidence_and_only_the_owner_admits(self):
        text = self.text("rungs")
        for seat in ("qwen27", "qwenlocal", "bonsai"):
            self.assertIn(seat, text, seat)
        self.assertIn("non-door reviewer", text)
        self.assertIn("only you admit", text)
        self.assertNotIn("<button", self.out["rungs"])         # it never promotes

    def test_a_model_or_a_seat_rides_the_url(self):
        self.assertEqual(self.out["query"], "?model=qwen27")
        self.assertEqual(self.out["routed"], "claude")
        self.assertEqual(self.out["by_seat"], "qwenlocal")     # the seat's model
        self.assertEqual(self.out["none"], [None, ""])

    def test_a_team_member_links_to_its_model(self):
        self.assertIn('href="#models?seat=alpha-codex"', self.out["member"])


def _cells(row):
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", c)).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


class ModelsAgreeWithTheTextTest(unittest.TestCase):
    """THE PAGE AND `helm eval board` PRINT THE SAME NUMBERS: both print what
    the board carries, rounded once in Python, so a half (5 of 8 lands clean,
    62.5; a 30.5-minute hand-back) reads the same in both, and the page
    rounds nothing again."""

    DRIVER = r"""
const out = {table: modelsTableHTML(BOARD), details: {}};
BOARD.models.forEach(m => { out.details[m.model] = modelDetailHTML(m, BOARD); });
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "modelsPct", "modelsDur", "modelsCell", "modelsTableHTML", "modelsUnnamed",
            "modelDetailHTML"))
        cls.board = scorecard.board(ledger() + grok_lanes(clean=5, withdrawn=3, slow=4) + codex3_rows(),
                                    TRUNK, now=NOW, window_s=30 * 86400)
        cls.lines = scorecard.board_lines(cls.board)
        support = ('const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => '
                   '({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\n'
                   "let MODELS_OPEN = null;\n"
                   "const BOARD = " + json.dumps(cls.board) + ";\n")
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-models-agree-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + fns + cls.DRIVER)
        cls.proc = subprocess.run([cls.node, path], capture_output=True, text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: " + (self.proc.stderr or "")[:1200])

    def cli(self):
        head = [ln for ln in self.lines if ln.startswith("rank ")][0]
        cols = list(re.finditer(r"\S+(?: \S+)*", head))      # names are two spaces apart
        names, at = [m.group(0) for m in cols], [m.start() for m in cols]
        rows = {}
        for ln in self.lines[self.lines.index(head) + 1:]:
            if not re.match(r"^(\d+|-)\s", ln):
                break
            vals = [ln[a:b].strip() for a, b in zip(at, at[1:] + [len(ln)])]
            rows[vals[1]] = dict(zip(names, vals))
        return rows

    def page(self):
        return {m.group(1): _cells(m.group(2)) for m in re.finditer(
            r'<tr class="mrow[^"]*" data-model="([^"]+)"[^>]*>(.*?)</tr>', self.out["table"], re.S)}

    def test_every_row_prints_the_same_numbers_on_both(self):
        cli, page = self.cli(), self.page()
        self.assertEqual(sorted(cli), sorted(page))
        self.assertIn("grok-build-0.1", page)
        for name, c in cli.items():
            p = page[name]
            score = "not scored" if c["score"] == "not scored" else c["score"] + " " + c["band"].replace("-", "–")
            self.assertEqual(p[2], score, name)
            self.assertEqual(p[3], c["decided"], name)
            self.assertEqual(p[5], c["ours"] + ("" if c["ours"] == "UNKNOWN" else "%"), name)
            self.assertEqual(p[6], c["reviews/land"], name)
            self.assertEqual(p[7], c["hand-back"], name)

    def test_a_half_reads_the_same_on_both(self):
        p = self.page()["grok-build-0.1"]
        self.assertEqual(p[5], "63%")                  # 5 of 8
        self.assertEqual(p[7], "31m")                  # 30.5 minutes

    def test_the_page_rounds_nothing_again(self):
        src = web_ui_loader.read_text()
        fns = [_extract_fn(src, n) for n in ("modelsPct", "modelsDur", "modelsCell", "modelsTableHTML",
                                             "modelDetailHTML", "modelsRungsHTML", "modelsUnnamed")]
        self.assertTrue(all(fns))
        for word in ("Math.round", "toFixed", "toPrecision"):
            self.assertFalse([f for f in fns if word in f], word)

    def test_a_model_with_no_public_benchmark_has_no_gap_on_the_page(self):
        text = re.sub(r"<[^>]*>", " ", self.out["details"]["grok-build-0.1"])
        self.assertIn("lands without a patch", text)
        self.assertNotIn("against its benchmark", text)


class ModelsWindowTest(unittest.TestCase):
    """A WINDOW'S READ LANDS ONLY WHILE ITS WINDOW IS CHOSEN: switching 30
    days to 7 days while the 30-day read is in flight must not let the slower
    answer draw 30-day numbers under the 7-day chip."""

    DRIVER = r"""
const out = {};
(async () => {
  const a = loadModels();
  MODELS_WINDOW = "7d"; MODELS = null;
  const b = loadModels();
  PENDING[1].res({window: "7d", models: []}); await b;
  PENDING[0].res({window: "30d", models: []}); await a;
  out.race = [MODELS_WINDOW, MODELS && MODELS.window];
  console.log(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        support = ("let MODELS = null, MODELS_WINDOW = '30d';\nconst PENDING = [];\n"
                   "function j(url) { return new Promise(res => PENDING.push({url, res})); }\n"
                   "function modelsResolve() {}\nfunction modelsRender() {}\n")
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-models-win-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + _extract_fn(src, "loadModels") + cls.DRIVER)
        cls.proc = subprocess.run([cls.node, path], capture_output=True, text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_a_late_answer_for_another_window_is_discarded(self):
        self.assertTrue(self.out, "node produced no output: " + (self.proc.stderr or "")[:1200])
        self.assertEqual(self.out["race"], ["7d", "7d"])


class ModelsKeyboardTest(unittest.TestCase):
    """A ROW TAKES THE KEYBOARD: rows carry tabindex=0, so Enter and Space on
    a focused row open or close its detail, through the same toggle a click
    uses."""

    def test_enter_and_space_toggle_a_row_like_a_click(self):
        src = web_ui_loader.read_text()
        toggle = _extract_fn(src, "modelsToggle")
        self.assertIn("MODELS_OPEN", toggle)
        part = src[src.index('$("#view-models").addEventListener'):]
        key = part[part.index('addEventListener("keydown"'):]
        key = key[:key.index("});") + 3]
        self.assertIn('"Enter"', key)
        self.assertIn('" "', key)
        self.assertIn("modelsToggle(", key)
        self.assertIn("preventDefault", key)
        click = part[part.index('addEventListener("click"'):]
        self.assertIn("modelsToggle(", click[:click.index("});") + 3])
        self.assertIn('tabindex="0"', _extract_fn(src, "modelsTableHTML"))


class ModelsAddressTest(unittest.TestCase):
    """THE ADDRESS FOLLOWS L4'S RULE (task/3445): a key the board does not
    answer falls back to the table and the address drops it, as an unknown
    #quota?family= and #work/pipeline?project= do; a seat named before the
    board is read waits in the address, and opens its model once it is."""

    DRIVER = r"""
const out = {};
modelsRoute("seat=qwenlocal");
out.pending = [MODELS_OPEN, MODELS_SEAT, modelsQuery()];
MODELS = BOARD;
modelsResolve();
out.arrived = [MODELS_OPEN, MODELS_SEAT, modelsQuery(), SYNCED.slice(-1)[0]];
modelsRoute("model=qwen27");
out.known = [MODELS_OPEN, modelsQuery()];
modelsRoute("model=no-such-model");
out.unknown_model = [MODELS_OPEN, MODELS_SEAT, modelsQuery(), SYNCED.slice(-1)[0]];
modelsRoute("seat=no-such-seat");
out.unknown_seat = [MODELS_OPEN, MODELS_SEAT, modelsQuery(), SYNCED.slice(-1)[0]];
modelsRoute("seat=seat-c");
out.unnamed_seat = [MODELS_OPEN, MODELS_SEAT, modelsQuery()];
modelsRoute("seat=seat-k");
out.codex3 = MODELS_OPEN;
MODELS = {unavailable: "the dispatch ledger could not be read"};
modelsRoute("model=no-such-model");
out.unread = [MODELS_OPEN, modelsQuery()];
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        fns = "\n\n".join(_extract_fn(src, n) for n in (
            "modelsQuery", "modelsRoute", "modelsResolve"))
        board = scorecard.board(ledger() + codex3_rows(), TRUNK, now=NOW, window_s=30 * 86400)
        support = ("let MODELS_OPEN = null, MODELS_SEAT = null, MODELS = null;\n"
                   "const SYNCED = [];\n"
                   "function viewHashSync(v) { SYNCED.push('#' + v + modelsQuery()); }\n"
                   "function modelsRender() {}\n"
                   "const BOARD = " + json.dumps(board) + ";\n")
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-models-addr-")
        path = os.path.join(cls.tmp, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(support + fns + cls.DRIVER)
        cls.proc = subprocess.run([cls.node, path], capture_output=True, text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: " + (self.proc.stderr or "")[:1200])

    def test_a_seat_named_before_the_board_waits_in_the_address(self):
        self.assertEqual(self.out["pending"], [None, "qwenlocal", "?seat=qwenlocal"])

    def test_the_board_opens_the_seats_model_and_the_address_says_which(self):
        self.assertEqual(self.out["arrived"],
                         ["qwenlocal", None, "?model=qwenlocal", "#models?model=qwenlocal"])
        self.assertEqual(self.out["known"], ["qwen27", "?model=qwen27"])

    def test_an_unknown_model_falls_back_and_the_address_drops_it(self):
        self.assertEqual(self.out["unknown_model"], [None, None, "", "#models"])

    def test_an_unknown_seat_falls_back_and_the_address_drops_it(self):
        self.assertEqual(self.out["unknown_seat"], [None, None, "", "#models"])

    def test_a_seat_whose_model_the_ledger_cannot_name_falls_back(self):
        # seat-c's lanes carry no model: there is no row to open
        self.assertEqual(self.out["unnamed_seat"], [None, None, ""])

    def test_a_seat_opens_the_model_it_runs_now(self):
        # seat-k's verdicts moved from gpt-5.6-sol to gpt-6-astra in the window
        self.assertEqual(self.out["codex3"], "gpt-6-astra")

    def test_an_unread_board_keeps_what_the_address_asked(self):
        # nothing proves the key unknown until the board is read
        self.assertEqual(self.out["unread"], ["no-such-model", "?model=no-such-model"])


if __name__ == "__main__":
    unittest.main()
