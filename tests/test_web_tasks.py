#!/usr/bin/env python3
"""helm.web task BACKLOG surface (#218 Half D) — hermetic contract tests.

The work tab's section container was BUILT for a second section and nothing
was ever plugged in; this suite pins the plug. Two seams, both pinned from
THIS side only:
  * the settled row contract with helm.tasks (task-corpus-destination lane):
    snapshot() -> (rows_by_id, unavailable), sort_key over rows, in-row
    comments via comment(). helm.tasks may land BEFORE or AFTER this
    surface, so the endpoint import-guards and every arm here runs against a
    STUB implementing exactly the settled contract — plus one arm against
    the genuinely-absent module (the land-order-independence case itself).
  * the owner-visible laws: unavailable is NEVER drawn as empty (a console
    that draws them the same says "fleet idle" at the exact moment it lost
    the ability to answer), the home badge counts DECISIONS ONLY (the
    19-row collapse precedent), and the #231 composer protections exist in
    the shipped JS (the owner reported that wipe on this exact tab)."""
import importlib.util
import json
import os
import re
import shutil
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helm as _helm_pkg  # noqa: E402
from helm import web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_BOARD",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


def _row(rid, title, status, **over):
    row = {"id": rid, "ts": over.pop("ts", "2026-08-05T00:00:00Z"),
           "last_updated": None, "title": title, "status": status,
           "owner": None, "note": None, "refs": [], "source": None,
           "origin": None, "closed_reason": None, "comments": []}
    row.update(over)
    return row


def _stub_tasks(rows, unavailable=None):
    """A helm.tasks implementing EXACTLY the settled contract — and
    recording what the surface asked of it, so the arms below assert the
    endpoint uses the STORE's ordering and writer, never its own."""
    mod = types.ModuleType("helm.tasks")
    mod.calls = {"snapshot": 0, "sort_key": 0, "comment": []}

    def snapshot():
        mod.calls["snapshot"] += 1
        return dict(rows), unavailable

    def sort_key(row):
        mod.calls["sort_key"] += 1
        rid = str(row.get("id") or "")
        tail = rid.rsplit("/", 1)[-1]
        return (0, int(tail)) if tail.isdigit() else (1, tail)

    def comment(rid, text):
        mod.calls["comment"].append((rid, text))
        hit = rows.get(rid)
        if not hit:
            return None, "no such task: %s" % rid
        hit = dict(hit)
        hit["comments"] = list(hit.get("comments") or []) + [
            {"ts": "now", "text": text, "by": "owner"}]
        rows[rid] = hit
        return hit, None

    mod.snapshot = snapshot
    mod.sort_key = sort_key
    mod.comment = comment
    # origin_of JOINS THE SETTLED CONTRACT DELIBERATELY. The surface must not
    # re-implement "which origin values are real" — a second copy of that rule
    # is the two-surfaces disease this whole lane exists to cure, one field
    # over. So the STORE owns the normalization and the surface asks it, which
    # means the stub owes it too. The real implementation is one line and this
    # mirrors it exactly rather than paraphrasing.
    mod.ORIGINS = ("owner", "agent")
    mod.origin_of = lambda row: (row.get("origin")
                                 if row.get("origin") in mod.ORIGINS else None)
    return mod


class TasksBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webtasks-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_BOARD"] = os.path.join(cls.tmp, "board.json")
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        os.chdir(cls.cwd_prior)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._tasks_prior = sys.modules.get("helm.tasks")
        self._tasks_attr = getattr(_helm_pkg, "tasks", None)

    def tearDown(self):
        if self._tasks_prior is None:
            sys.modules.pop("helm.tasks", None)
        else:
            sys.modules["helm.tasks"] = self._tasks_prior
        if self._tasks_attr is None:
            if hasattr(_helm_pkg, "tasks"):
                delattr(_helm_pkg, "tasks")
        else:
            _helm_pkg.tasks = self._tasks_attr

    def install(self, mod):
        sys.modules["helm.tasks"] = mod
        _helm_pkg.tasks = mod
        return mod

    def req(self, path, payload=None, token=True, raw=False):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if data and token:
            headers["Authorization"] = "Bearer " + web.MUTATION_TOKEN
        r = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                body = resp.read()
                return resp.status, (body if raw
                                     else json.loads(body or b"null"))
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
                return e.code, (body if raw else json.loads(body or b"null"))


class LandOrderIndependenceTest(TasksBase):
    def test_absent_module_reads_unavailable_never_empty(self):
        # The other lane may land after this one: helm.tasks genuinely does
        # not exist on this tree, and the surface must say CANNOT-SEE, not
        # "backlog clear" and not 500.
        # THE PREDICATE ASKS WHETHER THE MODULE EXISTS, NOT WHETHER SOMETHING
        # HAPPENED TO IMPORT IT ALREADY. `self._tasks_prior` is a sys.modules
        # snapshot kept for tearDown's restore, and it is None whenever nothing
        # in THIS process has imported helm.tasks yet — which is exactly the
        # case when this method is run ALONE, the one way the fab suite guard
        # permits a local run. So after helm.tasks landed, the arm skipped
        # inside the full suite (something imports it first) and FAILED
        # standalone, on trunk, for anyone following the mandated workflow.
        if importlib.util.find_spec("helm.tasks") is not None:
            self.skipTest("helm.tasks landed — the absent arm is history")
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(d.get("unavailable"))
        self.assertIn("not landed", d.get("why", ""))
        self.assertNotIn("entries", d)


class SettledContractTest(TasksBase):
    def test_entries_project_the_settled_fields_in_store_order(self):
        stub = self.install(_stub_tasks({
            "task/263": _row("task/263", "wire the console", "open",
                             owner="offbox-claude", refs=["#218"],
                             comments=[{"ts": "t", "text": "x", "by": "o"}]),
            "task/45": _row("task/45", "old fix", "in_progress"),
            "task/178": _row("task/178", "landed thing", "closed",
                             closed_reason="stale: landed at abc1234"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        ids = [e["id"] for e in d["entries"]]
        # numeric id order, the store's key — and the stub PROVES the
        # endpoint asked the store rather than sorting privately.
        self.assertEqual(ids, ["task/45", "task/178", "task/263"])
        self.assertGreater(stub.calls["sort_key"], 0)
        self.assertEqual(d["counts"],
                         {"open": 1, "in_progress": 1, "closed": 1})
        e263 = [e for e in d["entries"] if e["id"] == "task/263"][0]
        self.assertEqual(e263["owner"], "offbox-claude")
        self.assertEqual(e263["refs"], ["#218"])
        self.assertEqual(e263["comments"], 1)
        e178 = [e for e in d["entries"] if e["id"] == "task/178"][0]
        self.assertEqual(e178["closed_reason"], "stale: landed at abc1234")

    def test_unavailable_is_never_drawn_as_empty(self):
        # Same observable, both polarities: a locked ledger reads
        # CANNOT-SEE with the store's why; a truly empty one reads as a
        # clear backlog with zero counts.
        self.install(_stub_tasks({}, unavailable="ledger locked by writer"))
        status, d = self.req("/api/tasks")
        self.assertTrue(d.get("unavailable"))
        self.assertIn("locked", d["why"])
        self.install(_stub_tasks({}))
        status, d = self.req("/api/tasks")
        self.assertNotIn("unavailable", d)
        self.assertEqual(d["entries"], [])
        self.assertEqual(d["counts"],
                         {"open": 0, "in_progress": 0, "closed": 0})


class CommentPathTest(TasksBase):
    def test_comment_routes_through_the_store_writer(self):
        stub = self.install(_stub_tasks({
            "task/263": _row("task/263", "wire the console", "open")}))
        status, d = self.req("/api/tasks/comment",
                             {"id": "task/263", "text": "ship it"})
        self.assertEqual(status, 200, d)
        self.assertTrue(d.get("ok"))
        self.assertEqual(d["comments"], 1)
        self.assertEqual(stub.calls["comment"], [("task/263", "ship it")])

    def test_missing_fields_refuse_without_touching_the_store(self):
        stub = self.install(_stub_tasks({
            "task/263": _row("task/263", "wire the console", "open")}))
        status, d = self.req("/api/tasks/comment", {"id": "task/263"})
        self.assertEqual(status, 400)
        status, d = self.req("/api/tasks/comment", {"text": "orphan note"})
        self.assertEqual(status, 400)
        self.assertEqual(stub.calls["comment"], [])

    def test_a_post_without_the_bearer_is_refused(self):
        stub = self.install(_stub_tasks({
            "task/263": _row("task/263", "wire the console", "open")}))
        status, _d = self.req("/api/tasks/comment",
                              {"id": "task/263", "text": "x"}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(stub.calls["comment"], [])


class OwnerSurfaceLawsTest(TasksBase):
    def test_the_section_joins_the_socket_after_decisions(self):  # noqa: VACUOUS_ASSERTION — body.index() RAISES on any missing marker, so presence is asserted before order can compare
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        work = body.index('id="view-work"')
        sections = body.index('id="worksections"')
        odq = body.index('id="odq"')
        otq = body.index('id="otq"')
        nxt = body.index('id="view-chat"')
        self.assertTrue(work < sections < odq < otq < nxt,
                        "the task section must sit inside the work tab's "
                        "socket, after the decision queue")
        for route in ("/api/tasks", "/api/tasks/comment"):
            self.assertIn(route, body)

    def test_the_owner_can_SEE_which_rows_he_asked_for(self):
        """The owner asked for "major requests FROM ME" to be visible. A CLI
        glyph answers that for AGENTS; his surface is this console, and under
        the standing canon an agent-experience gain owes an owner-visible one
        in the same breath. TRI-STATE, on the wire and in the mark: `owner`
        is a witnessed fact, `agent` is a witnessed fact, and NULL is the 251
        legacy rows nobody witnessed either way — drawn as neither."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "he asked for this", "open",
                           origin="owner"),
            "task/2": _row("task/2", "an agent filed this", "open",
                           origin="agent"),
            "task/3": _row("task/3", "legacy, nobody witnessed", "open"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        by = {e["id"]: e for e in d["entries"]}
        self.assertEqual(by["task/1"]["origin"], "owner")
        self.assertEqual(by["task/2"]["origin"], "agent")
        # NOT "agent", and NOT absent-from-the-payload: an unwitnessed row
        # must reach the browser as a knowable UNKNOWN.
        self.assertIn("origin", by["task/3"])
        self.assertIsNone(by["task/3"]["origin"])

    def test_the_REAL_migration_tag_never_reaches_the_browser(self):
        """@codex round 1. The real ledger holds 251 rows stamped
        `corpus-2026-08-05` — 73 live — and this endpoint forwarded the raw
        field, so the browser received a FOURTH state while every consumer
        here is written against three. My earlier arm faked legacy as None,
        which is the one value the real data does not have."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "migrated", "open",
                           origin="corpus-2026-08-05"),
            "task/2": _row("task/2", "he asked", "open", origin="owner"),
        }))
        _status, d = self.req("/api/tasks")
        by = {e["id"]: e for e in d["entries"]}
        self.assertIsNone(by["task/1"]["origin"])
        self.assertNotIn("corpus", str(by["task/1"]["origin"]))
        # unconditional positive control: a declared value still crosses
        self.assertEqual(by["task/2"]["origin"], "owner")

    def test_a_CODE_bug_is_not_reported_as_CANNOT_SEE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the SIBLING arm test_a_STORE_outage_IS_still_reported_as_CANNOT_SEE, which asserts the same endpoint DOES return unavailable:true for a real outage; without it this would pass on an endpoint that never reports unavailability at all
        """@codex. The first cut wrapped the whole body, so MY code failing
        with the store perfectly available surfaced as HTTP 200
        {unavailable: true} — sending the reader to check the ledger while
        the fault was here. UNAVAILABLE is a claim about the STORE and
        nothing else may borrow it."""
        class Exploding(dict):
            def get(self, *a, **k):
                raise RuntimeError("a programming error, not a store outage")

        stub = _stub_tasks({})
        stub.snapshot = lambda: ({"task/1": Exploding()}, None)
        self.install(stub)
        with self.assertRaises(Exception):
            # the server surfaces it; what must NOT happen is a calm 200
            # saying the ledger cannot be seen
            status, d = self.req("/api/task/notes?id=task/1")
            self.assertNotEqual(
                (status, d.get("unavailable")), (200, True),
                "a code bug was reported as CANNOT SEE")
            raise AssertionError("endpoint did not raise")

    def test_a_STORE_outage_IS_still_reported_as_CANNOT_SEE(self):
        """The control that keeps the narrowing honest: a real snapshot
        failure must still answer unavailable rather than raising."""
        stub = _stub_tasks({})
        def boom():
            raise OSError("ledger gone")
        stub.snapshot = boom
        self.install(stub)
        status, d = self.req("/api/task/notes?id=task/1")
        self.assertEqual(status, 200)
        self.assertTrue(d.get("unavailable"))

    def test_the_refresh_HOLDS_while_a_comment_is_open(self):
        """@codex reproduced the alternative at the exact tip: task/329 open
        with 8,148 chars and scrolled, tqInit fires on its 45s timer, the
        list is replaced via innerHTML — disclosure closed, fetched body
        discarded, card 1,671px above the viewport. Reading a long ruling is
        the use case this feature exists for, so a 45-second guillotine made
        it worse than the counter it replaced. Same shape as the composer
        guard this file already had."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        self.assertIn("function tqNotesOpen", tq)
        self.assertIn(".otqnotes[open]", tq)
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        guard = render.index("tqNotesOpen()")
        self.assertLess(guard, render.index("innerHTML"),
                        "the hold must precede every list replacement")

    def test_the_shipped_card_marks_owner_rows_and_ONLY_owner_rows(self):
        """The mark is gated on STRICT equality with "owner" in the shipped
        asset. A truthiness test (`r.origin ?`) would mark every agent-filed
        row as the owner's own — the loudest possible way to get provenance
        wrong, on the one surface built to show it."""
        # THE SHIPPED PAGE, not the source file: the assertion is about what
        # the owner's browser actually received. Same slice the typing-guard
        # arm takes, for the same reason.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        card = tq[tq.index("function tqCard"):tq.index("async function tqAct")]
        self.assertIn("otqasked", card)
        self.assertIn('r.origin === "owner"', card)
        self.assertNotIn("r.origin ?", card)
        # and the COUNT beside it uses the same strict predicate
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        self.assertIn('r.origin === "owner"', render)

    def test_the_owner_can_READ_a_task_comment_not_just_count_it(self):
        """THE DEFECT THIS LANE EXISTS FOR. /api/tasks sends len(comments) and
        the card rendered "3 comments" — so a ruling filed ONTO A ROW, which
        is where owner decisions are supposed to live precisely BECAUSE chat
        scrolls away, reached his console as a number he could not open. The
        data was there, the route was there, the content was unreachable."""
        self.install(_stub_tasks({
            "task/341": _row("task/341", "the delivery ladder", "open",
                             comments=[{"ts": "2026-08-06T03:00:00Z",
                                        "by": "cj",
                                        "text": "FOUR RUNGS, not six."},
                                       {"ts": "2026-08-06T03:05:00Z",
                                        "by": "opus", "text": "ratified"}]),
        }))
        status, d = self.req("/api/task/notes?id=task/341")
        self.assertEqual(status, 200)
        self.assertNotIn("unavailable", d)
        self.assertEqual([c["text"] for c in d["comments"]],
                         ["FOUR RUNGS, not six.", "ratified"])
        self.assertEqual(d["comments"][0]["by"], "cj")

    def test_a_LONG_comment_is_served_WHOLE_never_truncated(self):
        """A capped ruling is the same defect one layer down: he would read a
        decision that stops mid-sentence with no way to tell that it had."""
        long = "R" * 20000
        self.install(_stub_tasks({
            "task/9": _row("task/9", "big", "open",
                           comments=[{"ts": "t", "by": "cj", "text": long}]),
        }))
        _status, d = self.req("/api/task/notes?id=task/9")
        self.assertEqual(len(d["comments"][0]["text"]), 20000)

    def test_an_UNREADABLE_ledger_is_SAID_not_drawn_as_no_comments(self):
        """The state triad, one row down. Drawing an unreadable ledger as an
        empty comment list tells the owner nobody wrote anything at the exact
        moment we lost the ability to say."""
        self.install(_stub_tasks({}, unavailable="ledger unreadable"))
        _status, d = self.req("/api/task/notes?id=task/341")
        self.assertTrue(d.get("unavailable"))
        self.assertIn("unreadable", d.get("why", ""))
        self.assertNotIn("comments", d)

    def test_the_shipped_card_FETCHES_the_notes_instead_of_printing_a_count(self):
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        self.assertIn("otqnotes", tq)
        self.assertIn("/api/task/notes?id=", tq)
        # capture:true — `toggle` does not bubble, so a listener without it
        # silently never fires and the panel stays on its placeholder
        self.assertIn("}, true);", tq)

    def test_the_home_badge_counts_decisions_only(self):
        # The 19-row collapse law, pinned at the text level on the shipped
        # page: the home view and the badge function reference no task
        # surface, and the task block writes no badge. Presence controls
        # first — the markers this absence is measured against exist.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        self.assertIn("odqBadge", body)               # the decisions badge
        self.assertIn("task backlog (#218", body)     # the task block
        home_start = body.index('id="view-helm"') if 'id="view-helm"' \
            in body else body.index('id="view-home"')
        home_end = body.index('id="view-quota"')
        home = body[home_start:home_end]
        self.assertNotIn("otq", home)
        self.assertNotIn("/api/tasks", home)
        # bound to MY segment (its own end marker), not to end-of-page —
        # later parts legitimately reference the decisions badge.
        tq_start = body.index("task backlog (#218")
        tq_block = body[tq_start:body.index("setInterval(tqInit", tq_start)]
        self.assertNotIn("odqBadge", tq_block)
        self.assertNotIn("odqbadge", tq_block)

    def test_the_typing_guard_is_semantic_not_decorative(self):
        # codex-3's mutation: replace tqTyping() with unconditional false and
        # the old pins stayed green — presence-of-a-string is not behavior.
        # These arms pin the atoms a gutting cannot keep: the guard function
        # must READ THE FOCUSED ELEMENT and scope it to the task composer,
        # and the call site must be a conditional EARLY RETURN ahead of every
        # list replacement. Each named mutation (body -> return false; call
        # site unconditioned; return dropped) deletes at least one pinned
        # atom — run them and the arms go red (done at review, recorded in
        # the commit).
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        fn = tq[tq.index("function tqTyping"):]
        nxt = fn.find("function ", 9)
        fn = fn[:nxt] if nxt > 0 else fn
        # THE PREDICATE IS BYTE-PINNED, deliberately: codex-3's second round
        # produced mutants every atom-pin survives (`return false && <orig>`
        # keeps all atoms; inverted containment keeps all atoms) — a static
        # suite cannot EXECUTE JS, so the honest stdlib ceiling is an exact
        # pin: ANY semantic mutation changes these bytes by construction,
        # and an intentional edit changes the pin consciously, stating why.
        # The BEHAVIORAL layer is the recorded browser execution (both #231
        # polarities run live at review); a JS-engine arm needs a runtime
        # helm's suite does not carry, and saying so beats pretending.
        want = ('return !!(a && host && host.contains(a) && a.classList '
                '&& a.classList.contains("otqcomment"));')
        ret = fn[fn.index("return"):]
        got = " ".join(ret[:ret.index(";") + 1].split())
        self.assertEqual(got, want,
                         "tqTyping's predicate changed — if intentional, "
                         "re-pin HERE with the reason; if not, a mutant "
                         "survived into the tree")
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        m = re.search(r"if \(tqTyping\(\)\) \{[^}]*return;", render)
        self.assertIsNotNone(
            m, "tqRender's typing guard must be a conditional EARLY RETURN")
        self.assertLess(m.start(), render.index("innerHTML"),
                        "the early return must precede every replacement")
        m2 = re.search(r"if \(tqTyping\(\)\) \{[^}]*return;", tq[:tq.index(
            "function tqTyping")])
        self.assertIsNotNone(
            m2, "tqInit's catch path must hold under a focused composer too")

    def test_the_231_composer_protections_exist_in_the_shipped_js(self):
        # The owner reported the wipe on THIS tab (#231). The shipped JS
        # must hold a render under a focused task composer and park drafts
        # before every list replacement. Text-level pins on the assembled
        # page — crude, but they fail loudly if someone strips the guard.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):]
        self.assertIn("function tqTyping", tq)
        self.assertIn("refresh held — you are typing", tq)
        self.assertIn("tqComposerState()", tq)
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        self.assertLess(render.index("tqTyping()"),
                        render.index("innerHTML"),
                        "tqRender must check the composer before replacing "
                        "the list")
        self.assertLess(render.index("tqComposerState()"),
                        render.index("innerHTML"),
                        "drafts must be parked before the replacement")


if __name__ == "__main__":
    unittest.main()
