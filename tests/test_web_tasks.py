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
import subprocess
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
from helm import web_ui_loader  # noqa: E402
from tests.test_web_chat_client_runtime import _extract_fn  # noqa: E402

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
    mod.calls = {"snapshot": 0, "sort_key": 0, "rank_key": 0, "board_key": 0,
                 "board_order": 0, "row_ages": 0, "queue_totals": 0,
                 "stamp_epoch": 0, "last_note": 0, "comment": []}

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
    # `PRIORITIES` AND `rank_key` JOIN THE CONTRACT FOR origin_of's REASON.
    # The endpoint must not hold a second rank table — "P0 first, UNRANKED
    # last" is the store's rule and a copy of it in the browser or in the
    # route would be the same question answered twice. So the stub owes both,
    # and `calls["rank_key"]` is how an arm proves the route asked the STORE
    # for its order instead of composing one. The SEMANTICS of the real key
    # are pinned where the real key lives (tests/test_tasks.py); this double
    # only has to be the same SHAPE.
    mod.PRIORITIES = ("P0", "P1", "P2", "P3")

    def rank_key(row):
        mod.calls["rank_key"] += 1
        got = (row or {}).get("priority")
        rank = (mod.PRIORITIES.index(got) if got in mod.PRIORITIES
                else len(mod.PRIORITIES))
        return (rank,) + tuple(sort_key(row))

    mod.rank_key = rank_key

    # `board_key` JOINS THE CONTRACT FOR rank_key's REASON, one question over
    # (task/2622). The owner's board sorts RANK FIRST and then OLDEST FIRST,
    # because the numbering rank_key falls through to is filing order only for
    # rows this ledger minted. That composition is the STORE's and a copy of it
    # in the route or the browser would be the same question answered twice —
    # so the double owes it, and it COMPOSES on rank_key here exactly as the
    # real one composes on sort_key, which is why asking this stub for the
    # board order still records a rank_key call. The real key's SEMANTICS are
    # pinned where the real key lives (tests/test_tasks.py).
    def board_key(row):
        mod.calls["board_key"] += 1
        ranked = rank_key(row)
        try:
            filed = float((row or {}).get("ts"))
        except (TypeError, ValueError):
            filed = None
        head = ((ranked[0], 1, 0.0) if filed is None
                else (ranked[0], 0, filed))
        return head + tuple(ranked[1:])

    mod.board_key = board_key

    # AND THE ORDERING ITSELF JOINS THE CONTRACT, not only the key. A
    # published KEY is an invitation each caller answers its own way, and that
    # is exactly how the route and `helm task list` came to sort one snapshot
    # two ways; the ORDERING is the answer, so the stub owes that instead and
    # `calls["board_order"]` is how an arm proves the route asked for it.
    def board_order(rows_):
        mod.calls["board_order"] += 1
        return sorted(rows_.values() if hasattr(rows_, "values") else rows_,
                      key=board_key)

    mod.board_order = board_order

    # THE AGES AND THE HEADLINE ARE THE STORE'S TOO, for the same reason and
    # with the same consequence when they were not: the browser held a second
    # timestamp reader and a second counter, and each rendered a confident
    # answer out of its own opinion. The double mirrors the real shapes — a
    # number or None, never 0 for unknown — rather than paraphrasing them.
    mod.STALE_NOTE_S = 7 * 86400

    def stamp_epoch(value):
        mod.calls["stamp_epoch"] += 1
        if isinstance(value, bool):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 and seconds == seconds else None

    mod.stamp_epoch = stamp_epoch

    def row_ages(row, now=None):
        mod.calls["row_ages"] += 1
        at = stamp_epoch((row or {}).get("ts"))
        note = last_note(row)
        noted = stamp_epoch(note.get("ts")) if note else None
        if noted is None:
            noted = at
        now = 0.0 if now is None else now
        noted_age = None if noted is None else max(0.0, now - noted)
        return {"ts_epoch": at,
                "age_s": None if at is None else max(0.0, now - at),
                "noted_age_s": noted_age,
                "stale": noted_age is not None
                         and noted_age >= mod.STALE_NOTE_S}

    mod.row_ages = row_ages

    def queue_totals(rows_, now=None):
        mod.calls["queue_totals"] += 1
        t = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, "unranked": 0,
             "in_progress": 0, "live": 0, "oldest": {"P0": None, "P1": None}}
        now = 0.0 if now is None else now
        for row in (rows_.values() if hasattr(rows_, "values") else rows_):
            if not isinstance(row, dict) or row.get("status") == "closed":
                continue
            t["live"] += 1
            if row.get("status") == "in_progress":
                t["in_progress"] += 1
            rank = row.get("priority")
            if rank in mod.PRIORITIES:
                t[rank] += 1
            else:
                t["unranked"] += 1
            if rank not in ("P0", "P1"):
                continue
            at = stamp_epoch(row.get("ts"))
            if at is None:
                continue
            age = max(0.0, now - at)
            if t["oldest"][rank] is None or age > t["oldest"][rank]:
                t["oldest"][rank] = age
        return t

    mod.queue_totals = queue_totals

    def last_note(row):
        mod.calls["last_note"] += 1
        notes = [c for c in ((row or {}).get("comments") or ())
                 if isinstance(c, dict)]
        return notes[-1] if notes else None

    mod.last_note = last_note
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
                                      kwargs={"poll_interval": 0.01},
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
                             owner="infra-claude", refs=["#218"],
                             comments=[{"ts": "t", "text": "x", "by": "o"}]),
            "task/45": _row("task/45", "old fix", "in_progress"),
            "task/178": _row("task/178", "landed thing", "closed",
                             closed_reason="stale: landed at 4bdca444"),
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
        self.assertEqual(e263["owner"], "infra-claude")
        self.assertEqual(e263["refs"], ["#218"])
        self.assertEqual(e263["comments"], 1)
        e178 = [e for e in d["entries"] if e["id"] == "task/178"][0]
        self.assertEqual(e178["closed_reason"], "stale: landed at 4bdca444")

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
        odq = body.index('id="odq"')
        sections = body.index('id="worksections"')
        otq = body.index('id="otq"')
        nxt = body.index('id="view-quota"')
        self.assertTrue(work < odq < sections < otq < nxt,
                        "the task section must sit on the Work page, in its "
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
        """The real ledger holds 251 rows stamped
        `corpus-2026-08-05` — 73 live — and this endpoint forwarded the raw
        field, so the browser received a FOURTH state while every consumer
        here is written against three. The earlier arm faked legacy as None,
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
        """The first cut wrapped the whole body, so the wrapper's own code failing
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

    def test_an_OPEN_comment_holds_the_SNAPSHOT_not_just_the_render(self):  # noqa: VACUOUS_ASSERTION — the ordering assertions are the observable; the unconditional controls on the same slice are the assertIn("function tqNotesOpen") and the assertIsNotNone(m) regex match, either of which fails if the shipped segment is empty or the function moved
        """The alternative was reproduced at the exact tip: task/329 open
        with 8,148 chars and scrolled, tqInit fires on its 45s timer, the
        list is replaced via innerHTML — disclosure closed, fetched body
        discarded, card 1,671px above the viewport. Reading a long ruling is
        the use case this feature exists for, so a 45-second guillotine made
        it worse than the counter it replaced.

        THE HOLD IS PINNED AT ACCEPTANCE, NOT AT RENDER, AND THE DIFFERENCE
        IS A SPLIT VERSION. A hold inside `tqRender` holds ONE of the two
        cells this payload feeds: the board home's queue cell advances to the
        new read while the card stays on the old one, off a single fetch, with
        nothing on either saying so. Holding at acceptance parks the snapshot
        in TQ_PENDING and leaves TQ_VIEW where it was, so both cells stay on
        one version — and that version expires on the clock rather than
        standing forever."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        self.assertIn("function tqNotesOpen", tq)
        self.assertIn(".otqnotes[open]", tq)
        accept = tq[tq.index("function tqAccept"):tq.index("function tqPaint")]
        m = re.search(r"if \(tqNotesOpen\(\)\) \{ TQ_PENDING = d; return;",
                      accept)
        self.assertIsNotNone(
            m, "the hold must be an EARLY RETURN in tqAccept that parks the "
               "body — if this moved, re-pin here with the reason")
        self.assertLess(m.start(), accept.index("TQ_VIEW = "),
                        "a snapshot reached TQ_VIEW before the hold could "
                        "park it, so the card and the board home would be "
                        "showing two versions of one queue")
        # AND THE PARKED VERSION IS NOT ABANDONED. A hold that never releases
        # is a card frozen for as long as a reader leaves a disclosure open.
        self.assertIn("if (TQ_PENDING && !tqNotesOpen()) tqAccept(TQ_PENDING);",
                      tq, "nothing lands the parked snapshot when the reader "
                          "closes the disclosure")

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

    def test_the_shipped_card_CHIPS_a_rank_and_draws_nothing_for_UNRANKED(self):
        """THE OWNER ASKED WHETHER TASKS ARE PRIORITIZED, so the answer has
        to be legible on the card he reads. The chip is drawn on a truthy
        `r.priority` — safe HERE because the server normalizes the wire value
        to one of four ranks or null, unlike `origin`, whose raw field carries
        a third state and whose arm above forbids exactly this test. An
        UNRANKED row draws NO chip: a dash or a "P3" would render
        nobody-has-judged-this as judged-lowest."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        card = tq[tq.index("function tqCard"):tq.index("async function tqAct")]
        self.assertIn("otqrank", card)
        self.assertIn("r.priority ?", card)
        # THE COUNT BESIDE IT, on the same shipped slice: the meta line says
        # how many rows carry no rank, because a board showing four ranked
        # rows at the top and nothing about the rest answers the owner's
        # question optimistically.
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        self.assertIn("UNRANKED", render)
        self.assertIn("!r.priority", render)

    def test_the_api_SENDS_a_rank_and_orders_P0_FIRST(self):
        """THE ORDER IS THE SERVER'S, and that is the point: a second
        PRIORITIES table in JavaScript would be the same question answered
        twice. The wire value is tri-state like `origin` — one of four ranks
        or null — so an unrecognised spelling normalizes here and the browser
        can never meet a fifth state."""
        stub = self.install(_stub_tasks({
            "task/1": {"id": "task/1", "title": "unranked", "status": "open"},
            "task/2": {"id": "task/2", "title": "lowest", "status": "open",
                       "priority": "P3"},
            "task/3": {"id": "task/3", "title": "blocker", "status": "open",
                       "priority": "P0"},
            "task/4": {"id": "task/4", "title": "bogus", "status": "open",
                       "priority": "URGENT"},
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        rows = (d.get("entries") or []) + (d.get("unscoped") or [])
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(4, len(rows), d)
        self.assertEqual("P0", by_id["task/3"]["priority"])
        self.assertEqual("P3", by_id["task/2"]["priority"])
        self.assertIsNone(by_id["task/1"]["priority"])
        self.assertIsNone(by_id["task/4"]["priority"],
                          "an eighth spelling reached the browser")
        self.assertGreater(stub.calls["rank_key"], 0,
                           "the route ordered the board with a key of its "
                           "own instead of the store's")
        ordered = [r["id"] for r in rows]
        self.assertLess(ordered.index("task/3"), ordered.index("task/2"),
                        "P0 did not sort above P3")
        self.assertLess(ordered.index("task/2"), ordered.index("task/1"),
                        "UNRANKED did not sort last")

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

    def test_the_on_you_line_counts_decisions_only(self):
        # The 19-row collapse law, pinned at the text level on the shipped
        # page: what is waiting on HIM (the ON YOU section and the badge
        # function) references no task surface, and the task block writes no
        # badge. Presence controls first — the markers this absence is
        # measured against exist.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        self.assertIn("odqBadge", body)               # the decisions badge
        self.assertIn("task backlog (#218", body)     # the task block
        onyou = body[body.index('<section id="onyou">'):
                     body.index('<section id="board">')]
        self.assertIn('id="odq"', onyou)             # the section WAS cut
        self.assertNotIn("otq", onyou)
        self.assertNotIn("/api/tasks", onyou)
        badge = body[body.index("function odqBadge("):]
        badge = badge[:badge.index("\n}\n")]
        self.assertNotIn("otq", badge)
        self.assertNotIn("/api/tasks", badge)
        # bound to MY segment (its own end marker), not to end-of-page —
        # later parts legitimately reference the decisions badge.
        tq_start = body.index("task backlog (#218")
        tq_block = body[tq_start:body.index("setInterval(tqInit", tq_start)]
        self.assertNotIn("odqBadge", tq_block)
        self.assertNotIn("odqbadge", tq_block)

    def test_a_typing_reader_KEEPS_the_draft_AND_the_caret(self):
        """RE-PINNED FROM A HOLD TO A PRESERVATION, and the reason is that the
        hold was buying the wipe protection at the price of a split version.

        WHAT WAS HERE: `tqRender` returned early while a task composer had
        focus, so a refresh under a typing reader was dropped — and the board
        home's queue cell, fed off the same fetch outside that guard,
        advanced anyway. One read, two versions, neither saying so.

        WHAT IS HERE NOW: the draft text lives in TQ_DRAFTS and the caret in
        `tqFocusState`, both OUTSIDE the rows being replaced, so the render
        happens and the reader loses nothing. This is the shape the decision
        queue beside it has always used (odqComposerState). The atoms pinned
        below are the ones a gutting cannot keep: the focus reader must READ
        THE FOCUSED ELEMENT and scope it to the task composer, it must carry
        the SELECTION and not merely the id, and the restore must run AFTER
        the replacement — a restore before it would put the caret into a node
        about to be discarded, which is a wipe with extra steps.

        A static suite cannot EXECUTE this (there is no DOM under node in
        this tree), so these are byte-and-order pins and the behavioural layer
        is the recorded browser run — the same honest ceiling the guard this
        replaces declared."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        fn = tq[tq.index("function tqFocusState"):]
        fn = fn[:fn.index("function tqRestoreFocus")]
        self.assertIn("document.activeElement", fn,
                      "the focus reader stopped reading the focused element")
        self.assertIn('a.classList.contains("otqcomment")', fn,
                      "the focus reader is no longer scoped to the task "
                      "composer, so any focused input would be restored into "
                      "a comment box")
        self.assertIn("a.selectionStart", fn,
                      "the caret position is not carried, so a restored draft "
                      "sends the cursor to the front of the text")
        self.assertIn("a.selectionEnd", fn)
        restore = tq[tq.index("function tqRestoreFocus"):]
        restore = restore[:restore.index("function tqWhen")]
        self.assertIn("box.focus();", restore)
        self.assertIn("box.setSelectionRange(f.start, f.end);", restore)
        render = tq[tq.index("function tqRender"):tq.index(
            "function tqFocusState")]
        self.assertLess(render.index("tqFocusState()"),
                        render.index("innerHTML"),
                        "the caret must be captured before the replacement")
        self.assertLess(render.index("tqComposerState()"),
                        render.index("innerHTML"),
                        "drafts must be parked before the replacement")
        self.assertGreater(render.index("tqRestoreFocus(focus)"),
                           render.rindex("innerHTML"),
                           "the caret is restored into markup that is about "
                           "to be replaced")
        # AND THE HOLD IT REPLACES IS GONE, not merely unused: a surviving
        # typing hold would bring the split version straight back.
        self.assertNotIn("function tqTyping", tq,
                         "the typing hold survived the cure that replaced it")
        self.assertNotIn("refresh held — you are typing", tq)

    def test_the_231_composer_protections_exist_in_the_shipped_js(self):
        # The owner reported the wipe on THIS tab (#231). The shipped JS must
        # park drafts before every list replacement and put them back after —
        # which is now the WHOLE protection, because the render no longer
        # refuses to run while somebody is typing. Text-level pins on the
        # assembled page — crude, but they fail loudly if someone strips it.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):]
        self.assertIn("const TQ_DRAFTS = {}", tq,
                      "the draft store the whole protection rests on is gone")
        self.assertIn("function tqComposerState", tq)
        render = tq[tq.index("function tqRender"):tq.index(
            "function tqFocusState")]
        self.assertLess(render.index("tqComposerState()"),
                        render.index("innerHTML"),
                        "drafts must be parked before the replacement")
        self.assertIn("if (box && saved[el.dataset.id]) box.value = "
                      "saved[el.dataset.id];", render,
                      "drafts are parked and never put back, which is the "
                      "wipe with one extra step")


class ProjectAxisSurfaceTest(TasksBase):
    """task/974 — the project axis on the owner surface. The card NAMES the
    project it renders (two projects' consoles must be distinguishable),
    WITHHOLDS foreign-project rows as a count (the measured leak: a
    sibling project's row rendering inside helm's own pipeline), and serves the
    UNSCOPED legacy bucket separately — disclosed, never guessed into a
    scope, never silently dropped (it is the whole pre-axis live board).

    The stub gains the two axis functions EXPLICITLY per arm: the contract
    extension is opt-in, so the degrade arm below proves a pre-axis
    helm.tasks still serves the whole ledger (land-order independence, this
    surface's founding law)."""

    ROWS = {
        "task/1": _row("task/1", "ours row", "open", project="fixproj"),
        "task/2": _row("task/2", "foreign row", "open", project="otherproj"),
        "task/3": _row("task/3", "legacy row", "open"),
        "task/4": _row("task/4", "foreign closed", "closed",
                       project="otherproj", closed_reason="done elsewhere"),
    }

    def _axis_stub(self, scope="fixproj"):
        stub = _stub_tasks(dict(self.ROWS))
        stub.current_project = lambda: scope
        # mirrors the store's one-line reader exactly, never paraphrased —
        # the same law the origin_of stub entry records above
        stub.project_of_row = lambda row: (
            str(row.get("project") or "").strip() or None)
        return self.install(stub)

    def test_the_surface_names_its_project_and_withholds_foreign_rows(self):
        self._axis_stub()
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(d.get("project"), "fixproj")
        self.assertEqual([e["id"] for e in d["entries"]], ["task/1"])
        # the withheld population is a COUNT on the wire — the foreign rows
        # themselves appear NOWHERE in the payload, which is the no-leak
        # claim measured on the serialized whole rather than one key. The
        # needles are the rows' CONTENT (titles + project name), never the
        # bare word "foreign": the gate measured that spelling matching the
        # payload's own `withheld_foreign` key — a true reading bound to the
        # schema instead of the leak it was written to catch.
        self.assertEqual(d.get("withheld_foreign"), 2)
        payload = json.dumps(d)
        self.assertTrue("ours row" in payload, d)      # the probe can see rows
        self.assertFalse("foreign row" in payload, d)
        self.assertFalse("foreign closed" in payload, d)
        self.assertFalse("otherproj" in payload, d)

    def test_the_unscoped_bucket_is_separate_never_inside_the_scope(self):
        self._axis_stub()
        _status, d = self.req("/api/tasks")
        self.assertEqual([e["id"] for e in d.get("unscoped") or []],
                         ["task/3"])
        self.assertEqual([e["id"] for e in d["entries"]], ["task/1"])
        self.assertEqual((d.get("unscoped") or [{}])[0].get("project"), None)
        # counts describe what the card RENDERS (scoped + bucket), so the
        # meta line and the visible rows cannot disagree
        self.assertEqual(d["counts"],
                         {"open": 2, "in_progress": 0, "closed": 0})

    def test_a_pre_axis_store_still_serves_the_whole_ledger(self):
        # NO current_project/project_of_row on the stub: helm.tasks may
        # predate the axis on this trunk, and the surface must degrade to
        # the unscoped console, never 500 and never an empty board.
        self.install(_stub_tasks(dict(self.ROWS)))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(d.get("project"), None)
        self.assertEqual(len(d["entries"]), 4)
        self.assertEqual(d.get("unscoped"), [])
        self.assertEqual(d.get("withheld_foreign"), 0)

    def test_the_shipped_card_names_the_project_and_the_bucket(self):
        # THE SHIPPED PAGE, not the source file — the same slice the other
        # shipped-asset arms take, for the same reason.
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        render = tq[tq.index("function tqRender"):tq.index("function tqCard")]
        self.assertTrue("d.project" in render,
                        "the card never names its project")
        self.assertTrue("withheld_foreign" in render,
                        "the withheld count never reaches the owner")
        self.assertTrue("unscoped legacy row" in render,
                        "the legacy bucket is not disclosed as such")
        self.assertTrue("otqbucket" in render,
                        "the bucket is not rendered as its own section")


class CommentRouteShapeTest(TasksBase):
    """WHICH VERB THE ROUTE ANSWERS, measured — the client arm's premise.

    The comment route is POST-only. A GET at it is not a lenient spelling of
    the same request; it is a 404 whose body NAMES the path, and that sentence
    is what the page now shows the owner when any read misses."""

    def test_a_GET_at_the_comment_route_is_a_named_404(self):
        self.install(_stub_tasks({
            "task/263": _row("task/263", "wire the console", "open")}))
        status, d = self.req("/api/tasks/comment")        # no payload -> GET
        self.assertEqual(status, 404, d)
        self.assertEqual(d.get("error"), "not found: /api/tasks/comment")
        # THE CONTROL that makes the 404 mean something: the SAME path under
        # the right verb is alive, so the refusal above is about the METHOD
        # and not about a route nobody registered.
        status, d = self.req("/api/tasks/comment",
                             {"id": "task/263", "text": "ship it"})
        self.assertEqual(status, 200, d)


class CommentClientRuntimeTest(unittest.TestCase):
    """The CLIENT leg, EXECUTED: the page's own j/post/tqAct run under node.

    EVERY SOURCE-TEXT ARM IN THIS FILE PASSED WHILE THE SURFACE WAS BROKEN.
    The composer called `j("/api/tasks/comment", 8000, {method: "POST", body})`
    — and `j` takes (url, ms). The third argument was dropped on the floor, the
    page issued a plain GET at a POST-only route, and the owner's note never
    left the browser: he typed a comment on a task, pressed the button, and
    read "✗ /api/tasks/comment -> 404" while the route, the handler, the
    ledger and the bearer were all healthy. The arm above this one asserts the
    string "/api/tasks/comment" is IN the served page — it was, inside a call
    that never sent a POST. Only running the real functions and reading the
    REQUEST THEY EMIT can tell those two worlds apart, which is what this does
    (the CardRuntimeBase idiom from tests/test_web_lr.py).

    Requires node; skipped where the toolchain is absent, like every other
    runtime leg."""

    HARNESS = r"""
TOKEN = "test-bearer";
let REFRESHED = 0;
async function refreshToken() { REFRESHED++; }

let REQ = [], REPLY = null, TOASTS = [], REFRESHES = 0, HANG = false;
global.fetch = (url, init) => {
  const i = init || {};
  REQ.push({url: String(url), method: i.method || "GET",
            auth: (i.headers || {}).Authorization || null,
            body: i.body === undefined ? null : String(i.body)});
  // HANG honours the abort signal, which is the only way to reach j()'s
  // timeout branch: a fetch that resolves can never abort.
  if (HANG) return new Promise((_res, rej) => {
    if (i.signal) i.signal.addEventListener("abort",
                                            () => rej(new Error("aborted")));
  });
  const r = REPLY;
  return Promise.resolve({ok: r.ok, status: r.status,
                          json: async () => r.body});
};
function toast(m, ms) { TOASTS.push({text: m, ms: ms === undefined ? null : ms}); }
function tqInit() { REFRESHES++; }

function card(text) {
  const box = {value: text};
  return {box, el: {style: {}, dataset: {id: "task/263"}, isConnected: true,
                    querySelector: s => s === ".otqcomment" ? box : null}};
}
function reset(reply) {
  REQ = []; TOASTS = []; REFRESHES = 0; REFRESHED = 0; REPLY = reply;
  HANG = false;
  for (const k of Object.keys(TQ_DRAFTS)) delete TQ_DRAFTS[k];
}

const out = {};
(async () => {
  let c = card("ship it");
  reset({ok: true, status: 200, body: {ok: true, id: "task/263", comments: 2}});
  await tqAct({id: "task/263", text: "ship it"}, c.el);
  out.sent = {requests: REQ, toasts: TOASTS, box: c.box.value,
              refreshes: REFRESHES};

  c = card("ship it");
  reset({ok: true, status: 200, body: {ok: true, id: "task/263", comments: 2,
                                       warning: "the board did not sync"}});
  await tqAct({id: "task/263", text: "ship it"}, c.el);
  out.warned = {toasts: TOASTS};

  c = card("ship it");
  reset({ok: false, status: 400, body: {error: "id and text are required"}});
  await tqAct({id: "task/263", text: "ship it"}, c.el);
  out.refused = {toasts: TOASTS, box: c.box.value, refreshes: REFRESHES};

  reset({ok: true, status: 200, body: {}});
  out.j_init = {message: null, requests: null};
  try { await j("/api/tasks/comment", 8000, {method: "POST", body: "{}"}); }
  catch (e) { out.j_init.message = e.message; }
  out.j_init.requests = REQ;

  reset({ok: false, status: 404, body: {error: "task/9 does not exist"}});
  out.j_read = {message: null, requests: null};
  try { await j("/api/task/notes?id=task/9", 8000); }
  catch (e) { out.j_read.message = e.message; }
  out.j_read.requests = REQ;

  reset({ok: true, status: 200, body: {}});
  HANG = true;
  out.j_timeout = {message: null, requests: null};
  try { await j("/api/task/notes?id=task/9", 5); }
  catch (e) { out.j_timeout.message = e.message; }
  out.j_timeout.requests = REQ;
  HANG = false;

  process.stdout.write(JSON.stringify(out));
})();
"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()
        decls = []
        for pat in (r"^let TOKEN = .+$", r"^const TQ_DRAFTS = .+$"):
            m = re.search(pat, src, re.M)
            assert m, "declaration not found in assembled web UI: " + pat
            decls.append(m.group(0))
        body = "\n".join(decls) + "\n" + "\n\n".join(
            _extract_fn(src, n) for n in ("j", "post", "tqAct"))
        cls.tmp = tempfile.mkdtemp(prefix="helm-tqact-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(body + cls.HARNESS)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        p = subprocess.run([cls.node, cls.path], capture_output=True,
                           text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        cls.out = json.loads(p.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_the_comment_leaves_the_page_as_a_POST_carrying_the_bearer(self):
        """THE DEFECT ITSELF. One request, and it is the mutation: the method,
        the payload and the per-process bearer all on the wire."""
        got = self.out["sent"]
        # MUST-HIT: the harness's fetch really ran. A tqAct that returned
        # early would leave this list empty and every assertion below would be
        # vacuously about nothing.
        self.assertEqual(len(got["requests"]), 1, got)
        req = got["requests"][0]
        self.assertEqual(req["url"], "/api/tasks/comment")
        self.assertEqual(req["method"], "POST",
                         "the comment leaves the page as a %s — the init was "
                         "dropped again" % req["method"])
        self.assertEqual(req["auth"], "Bearer test-bearer",
                         "the mutation went out without the bearer the "
                         "server demands")
        self.assertEqual(json.loads(req["body"]),
                         {"id": "task/263", "text": "ship it"})
        self.assertEqual([t["text"] for t in got["toasts"]],
                         ["commented on task/263"])
        self.assertEqual(got["box"], "", "a delivered comment was resurrected")
        self.assertEqual(got["refreshes"], 1, "the list never refreshed")

    def test_a_warning_on_a_written_row_reaches_the_toast(self):
        """A note that landed CARRYING A COMPLAINT is not the same event as
        one that landed clean, and the surface must not draw them alike."""
        texts = [t["text"] for t in self.out["warned"]["toasts"]]
        self.assertEqual(len(texts), 1, self.out["warned"])
        self.assertIn("commented on task/263", texts[0])
        self.assertIn("the board did not sync", texts[0])
        # and it holds the eye longer than the clean case, like the decision
        # queue's delivery_error beside it
        self.assertEqual(self.out["warned"]["toasts"][0]["ms"], 5000)

    def test_a_refusal_says_WHY_and_WHERE_and_hands_the_text_back(self):
        """The server's own sentence AND the path and status beside it — and
        the comment the owner typed survives the failure.

        ALL THREE TIERS, AND THE SAME THREE THE READ DOOR OWES. An assertion
        that accepts only the server's sentence is satisfied by a `post` that
        renders one tier and drops the other two, so it cannot see the defect
        it is standing in front of — the mutation door telling the owner LESS
        about a failure than the read door tells him about the same one.

        Its sibling `test_a_failed_read_carries_the_servers_own_reason` holds
        the read door to this bar and says why: the status tier is a fact
        about the failure too. The arm below compares the two doors directly,
        because neither per-door arm can see them drift apart. task/2650."""
        got = self.out["refused"]
        self.assertEqual(len(got["toasts"]), 1, got)
        text = got["toasts"][0]["text"]
        self.assertIn("id and text are required", text,
                      "the server's own sentence is the only part that says "
                      "WHY")
        self.assertIn("400", text,
                      "the status tier is a fact about the failure too — the "
                      "same argument task/2621 made for j()")
        self.assertIn("/api/tasks/comment", text,
                      "a failure with no path sends the owner to a browser "
                      "network tab to learn which call broke")
        self.assertEqual(got["box"], "ship it",
                         "a failed send ate the owner's comment")
        self.assertEqual(got["refreshes"], 0,
                         "a failed send refreshed as though it had worked")

    def test_j_refuses_a_request_body_instead_of_downgrading_it_to_a_GET(self):
        """THE CURE FOR THE CLASS, not just for this call site. `j` reads; a
        caller that hands it a method and a body is told so LOUDLY, because
        the alternative — silently issuing a GET — is the defect that shipped:
        every layer stays healthy and the only evidence is a 404 in a toast."""
        got = self.out["j_init"]
        self.assertTrue(got["message"], "j() accepted an init it cannot honour")
        self.assertIn("post()", got["message"])
        self.assertIn("POST", got["message"])
        self.assertIn("/api/tasks/comment", got["message"])
        # THE CONTROL: it refused BEFORE the network. A helper that fetched
        # first and complained after would have already sent the wrong verb.
        self.assertEqual(got["requests"], [],
                         "j() issued a request for a call it cannot make")

    def test_a_failed_read_carries_the_servers_own_reason(self):
        """`/api/task/notes -> 404` tells the owner nothing he can act on;
        the body always says more, and post() has always read it."""
        got = self.out["j_read"]
        self.assertEqual(len(got["requests"]), 1, got)   # must-hit
        self.assertIn("task/9 does not exist", got["message"])
        self.assertIn("404", got["message"],
                      "the status tier is a fact about the failure too")


    def test_the_MUTATION_door_and_the_READ_door_draw_a_failure_ALIKE(self):
        """THE SYMMETRY ITSELF, asserted once so the pair cannot drift again.

        Two helpers answer the same question — what went wrong — and they
        drifted because each was cured alone. Neither arm above can catch a
        future divergence, because each checks only its own door. This one
        compares them, so curing one helper and not its sibling goes red
        here rather than shipping and being found by the owner."""
        mutation = self.out["refused"]["toasts"][0]["text"]
        read = self.out["j_read"]["message"]
        for label, text, status, path in (
                ("mutation", mutation, "400", "/api/tasks/comment"),
                ("read", read, "404", "/api/task/notes")):
            with self.subTest(label):
                self.assertIn(status, text,
                              "%s door dropped the status tier" % label)
                self.assertIn(path, text,
                              "%s door dropped the path" % label)
                self.assertRegex(text, r"[a-z] [a-z]",
                                 "%s door dropped the server's sentence"
                                 % label)


    def test_a_TIMEOUT_names_the_call_that_hung(self):
        """`timed out after 8s` names no call, so the owner learns that
        SOMETHING hung and nothing about what.

        Found by sweeping this file's helpers for siblings of the post()
        defect: three lines apart, one branch answered "does this failure say
        WHERE" with yes and the other with no. A timeout is the failure most
        likely to be transient and least likely to be reproduced on demand,
        so it is the worst one to leave anonymous. task/2650.

        THE SECONDS ARE NOT ASSERTED, deliberately. This arm passes ms=5 to
        keep the run fast, and `Math.round(5 / 1000)` renders "0s" — an
        artifact of the test's own timeout and not of the message, which says
        "8s" for the 8000ms every real caller uses. Asserting the number here
        would pin the artifact."""
        got = self.out["j_timeout"]
        self.assertEqual(len(got["requests"]), 1, got)   # must-hit
        self.assertTrue(got["message"], "the timeout branch never fired")
        self.assertIn("timed out", got["message"])
        self.assertIn("/api/task/notes", got["message"],
                      "a timeout with no path sends the owner to a network "
                      "tab to learn which call hung")


if __name__ == "__main__":
    unittest.main()


class QueueOrderAndLastNoteTest(TasksBase):
    """THE WIRE HALF OF THE OWNER'S QUEUE (task/2622). He asked how he is
    supposed to cross-check the ordering of a 477-row backlog, and how to stop
    re-asking for things already on it. Both answers need two things this
    payload did not carry: an order he can check (oldest first inside a rank)
    and, per row, WHEN anybody last said anything and WHAT they said."""

    def test_the_route_asks_the_STORE_for_the_board_order(self):
        """A second ordering table in this route — or in the browser — would be
        the same question answered twice, which is the law `rank_key` is on
        this wire for. The stub composes board_key on rank_key exactly as the
        store composes it on sort_key, so both counters move."""
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "young P1", "open", priority="P1",
                           ts="1700000000"),
            "task/2": _row("task/2", "OLD P1", "open", priority="P1",
                           ts="1600000000"),
            "task/3": _row("task/3", "a blocker", "open", priority="P0",
                           ts="1690000000"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        ordered = [e["id"] for e in (d.get("entries") or [])
                   + (d.get("unscoped") or [])]
        self.assertEqual(["task/3", "task/2", "task/1"], ordered,
                         "the board did not arrive P0-first and then oldest "
                         "first inside the rank")
        self.assertGreater(stub.calls["board_order"], 0,
                           "the route ordered the board itself instead of "
                           "asking the store for its one ordering — which is "
                           "how this route and `helm task list` came to sort "
                           "one snapshot two ways")

    def test_the_row_carries_the_last_notes_STAMP_and_FIRST_LINE(self):
        """The count answers "is anything written here". It cannot answer "has
        anybody touched this in a month", which is the question a backlog
        suspected of being ignored actually raises. FIRST LINE, not a
        truncation: a cut mid-word reads as a rendering fault, a first line is
        a thing its author wrote."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "annotated", "open", comments=[
                {"ts": "1600000000", "by": "seat-c", "text": "older"},
                {"ts": "1700000000", "by": "seat-d",
                 "text": "THE HEADLINE LINE\nand then a long second paragraph "
                         "that belongs on /api/task/notes, not on a payload "
                         "carrying every row in the backlog"}]),
            "task/2": _row("task/2", "nobody has said anything", "open"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        by = {e["id"]: e for e in d["entries"]}
        note = by["task/1"]["last_note"]
        # MUST-HIT: the field arrived at all, so the assertions below are
        # about a value this branch produced rather than about a missing key.
        self.assertIsNotNone(note, "the row carried no last_note at all")
        self.assertEqual("1700000000", note["ts"],
                         "the NEWEST comment is the last element; an older "
                         "stamp here means the route picked the wrong end")
        self.assertEqual("seat-d", note["by"])
        self.assertEqual("THE HEADLINE LINE", note["line"])
        self.assertNotIn("second paragraph", json.dumps(by["task/1"]),
                         "the unbounded prose rode the list payload, which is "
                         "exactly what /api/task/notes exists to keep off it")
        # A ROW WITH NO COMMENTS SAYS SO AS null, not as an empty object: the
        # browser must be able to tell "nobody has written here" from "the
        # newest note is blank".
        self.assertIsNone(by["task/2"]["last_note"])
        self.assertIn("last_note", by["task/2"])

    # ---- finding 2: ONE read, ONE version ---------------------------------

    def test_ONE_read_stamps_the_payload_and_the_counts_and_rows_agree(self):
        """The headline, the counts and the ordered rows are one projection of
        ONE snapshot, and `read_at` is how the two cells that render it can
        prove they are showing the same version. The first cut had the board
        home fed straight off the fetch and the card able to HOLD, which is
        two versions off one read with nothing on either saying so."""
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "young P1", "open", priority="P1",
                           ts="1700000000"),
            "task/2": _row("task/2", "OLD P1", "open", priority="P1",
                           ts="1600000000"),
            "task/3": _row("task/3", "a blocker", "open", priority="P0",
                           ts="1690000000"),
            "task/4": _row("task/4", "done", "closed", ts="1690000000",
                           closed_reason="landed"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertIsInstance(d.get("read_at"), float,
                              "the payload carries no read_at, so nothing can "
                              "prove two cells are on one version")
        self.assertEqual(1, stub.calls["snapshot"],
                         "the route read the ledger %d times for one payload"
                         % stub.calls["snapshot"])
        # THE HEADLINE IS THE SERVER'S, COUNTED ONCE.
        q = d.get("queue")
        self.assertIsNotNone(q, "the payload carries no queue totals")
        self.assertEqual(1, q["P0"])
        self.assertEqual(2, q["P1"])
        self.assertEqual(3, q["live"], "a CLOSED row was counted as live work")
        self.assertGreater(stub.calls["queue_totals"], 0,
                           "the route counted the headline itself instead of "
                           "asking the store")
        # AND THE HEADLINE DESCRIBES THE ROWS THAT WERE SERVED. A count over a
        # different population is the disagreement this lane exists to close.
        live = [e for e in d["entries"] + d["unscoped"]
                if e["status"] != "closed"]
        self.assertEqual(q["live"], len(live),
                         "the headline counts a population the payload does "
                         "not carry")

    def test_the_route_publishes_the_AGES_so_the_browser_parses_nothing(self):
        """FINDING 5 on the wire. Every age is a number or null resolved by
        the store's one parser at `read_at`; a browser handed a raw stamp is a
        SECOND reader with its own opinion about what counts as a date."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "dated", "open", ts="1700000000"),
            "task/2": _row("task/2", "undated", "open", ts="not a timestamp"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        by = {e["id"]: e for e in d["entries"] + d["unscoped"]}
        for key in ("ts_epoch", "age_s", "noted_age_s", "stale"):
            self.assertIn(key, by["task/1"],
                          "the row carries no %s, so the browser has to "
                          "resolve the stamp itself" % key)
        self.assertEqual(1700000000.0, by["task/1"]["ts_epoch"])
        self.assertIsInstance(by["task/1"]["age_s"], float)
        # UNKNOWN IS null AND NEVER 0 — a zero would arrive as "just now".
        self.assertIsNone(by["task/2"]["ts_epoch"],
                          "an unreadable stamp was resolved to a number")
        self.assertIsNone(by["task/2"]["age_s"])
        self.assertIs(False, by["task/2"]["stale"],
                      "an undated row was MARKED stale, which claims a "
                      "measurement nobody made")

    # ---- finding 1: a failed or malformed read clears EVERY number ---------

    def test_an_UNAVAILABLE_read_sends_the_UNKNOWN_SHAPE_not_a_missing_key(self):
        """A payload that says `unavailable` and simply OMITS the totals lets
        a consumer fall through to whatever it drew last. `queue: null` and an
        empty `entries` say the number is not known RIGHT NOW, which is the
        whole difference between "cannot see the backlog" and "it is clear"."""
        self.install(_stub_tasks({}, unavailable="ledger is a directory"))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertTrue(d.get("unavailable"))
        self.assertIn("ledger is a directory", str(d.get("why")))
        # EVERY FIELD A CONSUMER MIGHT FALL BACK ON IS PRESENT AND EMPTY.
        for key in ("read_at", "queue"):
            self.assertIn(key, d, "the UNKNOWN payload omits %r, so a "
                                  "consumer keeps its last value" % key)
            self.assertIsNone(d[key], "%r was not cleared on a failed read"
                                      % key)
        self.assertEqual([], d.get("entries"))
        self.assertEqual([], d.get("unscoped"))
        # THE CONTROL: a healthy read on the same route DOES carry them, so
        # the nulls above are a cleared value and not a route that never
        # sends one.
        self.install(_stub_tasks({
            "task/1": _row("task/1", "a live row", "open", priority="P0",
                           ts="1700000000")}))
        _status, ok = self.req("/api/tasks")
        self.assertIsNotNone(ok.get("queue"),
                             "MUST-HIT: the healthy read carries no totals "
                             "either, so the clearing above proves nothing")
        self.assertEqual(1, ok["queue"]["P0"])

    def test_a_PRE_QUEUE_helm_tasks_sends_UNKNOWN_rather_than_zeroes(self):
        """LAND-ORDER INDEPENDENCE, this surface's founding law, with the
        absence answered honestly. A helm.tasks that predates the headline has
        no totals to give and this route must not invent a queue of zeroes —
        which would tell the owner the backlog is clear."""
        stub = _stub_tasks({
            "task/1": _row("task/1", "a live row", "open", priority="P0",
                           ts="1700000000")})
        del stub.queue_totals
        del stub.row_ages
        del stub.board_order
        self.install(stub)
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertIsNone(d.get("queue"),
                          "the route invented totals a pre-queue store never "
                          "published")
        # THE ROWS STILL RENDER — degrade, never 500 and never empty.
        ids = [e["id"] for e in d["entries"] + d["unscoped"]]
        self.assertEqual(["task/1"], ids,
                         "the surface stopped serving rows when the store was "
                         "older than it")
        by = {e["id"]: e for e in d["entries"] + d["unscoped"]}
        self.assertIsNone(by["task/1"]["age_s"],
                          "the route resolved an age with no parser to "
                          "resolve it, which is the second reader all over "
                          "again")

    # ---- finding 4: one ordering serves the API and the CLI ---------------

    def test_the_SAME_snapshot_ordered_by_the_CLI_and_by_the_API_is_ONE_list(self):  # noqa: VACUOUS_ASSERTION — assertEqual(cli_ids, api_ids) could be two empty lists agreeing; the unconditional controls on the same two observables are the assertEqual(len(seeded), len(cli_ids)) and the same on api_ids, both ahead of the comparison
        """He cross-checks the board against a terminal. When the two orders
        differ he is reading a difference that is not in the ledger.

        THE REAL MODULES, NOT THE STUB: this arm's whole subject is that two
        production doors agree, and a double standing in for either would make
        it agree with itself."""
        import io as _io
        import contextlib as _ctx
        from helm import tasks as real_tasks
        self.install(real_tasks)
        ledger = real_tasks.ledger_path()
        if os.path.exists(ledger):
            os.remove(ledger)
        seeded = [("a blocker on the signer export", "P0", 1_690_000_000.0),
                  ("the youngest P1 about the proxy pool", "P1",
                   1_700_000_000.0),
                  ("the oldest P1 about credential rotation", "P1",
                   1_600_000_000.0),
                  ("an unranked row nobody has judged", None,
                   1_650_000_000.0),
                  ("a P2 about the burndown card", "P2", 1_695_000_000.0)]
        want = {}
        for title, rank, ts in seeded:
            row, err = real_tasks.add(title, "seat-a", priority=rank)
            self.assertIsNone(err, err)
            want[title] = ts
        # BACKDATED IN THE LEDGER'S OWN FORMAT — one whole row per line — so
        # the projection reads real rows rather than a shape invented here.
        # `add` stamps now, and this arm's subject is the order AGES produce.
        with open(ledger, encoding="utf-8") as fh:
            lines = [json.loads(ln) for ln in fh.read().splitlines() if ln]
        for rec in lines:
            if rec.get("title") in want:
                rec["ts"] = want[rec["title"]]
        with open(ledger, "w", encoding="utf-8") as fh:
            for rec in lines:
                fh.write(json.dumps(rec) + "\n")
        rows, un = real_tasks.snapshot()
        self.assertIsNone(un, un)
        self.assertEqual(len(seeded), len(rows),
                         "the backdating rewrite lost rows, so this arm is "
                         "measuring a ledger it broke rather than an order")
        # THE CLI'S OWN LISTING, through its own door.
        out = _io.StringIO()
        with _ctx.redirect_stdout(out), _ctx.redirect_stderr(_io.StringIO()):
            rc = real_tasks.cmd_task(["list", "--json", "--all-projects"])
        self.assertEqual(0, rc)
        cli_ids = [r["id"] for r in json.loads(out.getvalue())]
        # THE API'S, through the real route over the real socket.
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        api_ids = [e["id"] for e in d["entries"] + d["unscoped"]]
        # MUST-HIT: both doors answered with the seeded rows, so "identical"
        # is not two empty lists agreeing.
        self.assertEqual(len(seeded), len(cli_ids),
                         "the CLI listed %d of %d rows" % (len(cli_ids),
                                                           len(seeded)))
        self.assertEqual(len(seeded), len(api_ids))
        self.assertEqual(cli_ids, api_ids,
                         "one snapshot came out of the terminal and out of "
                         "the browser as two different lists")
        # AND THE ORDER IS THE ONE HE WAS PROMISED: rank first, oldest first
        # inside the rank, UNRANKED last. Without this both doors could agree
        # on an order neither of them should have.
        ranked = [(e["priority"], e["ts_epoch"]) for e in
                  d["entries"] + d["unscoped"]]
        self.assertEqual("P0", ranked[0][0])
        self.assertIsNone(ranked[-1][0], "UNRANKED did not sort last")
        p1 = [ts for rank, ts in ranked if rank == "P1"]
        self.assertEqual(sorted(p1), p1,
                         "inside one rank the board was not oldest-first")


class PayloadBoundsTest(TasksBase):
    """WHAT THIS LIST MAY WEIGH, and how a reader tells short from complete.

    THE DEFECT: measured on the owner's own ledger, `/api/tasks` shipped
    4,361,132 bytes on one call — 2,772 rows, no paging, no field trim, and
    half of it one unbounded prose field. Server time was 0.43-0.59s, so this
    was never seconds; it was bytes, and bytes are what a phone on a cellular
    link pays. Every arm below pins one of the three bounds AND the thing that
    makes the bound honest: a reader must never be able to mistake a bounded
    answer for a whole one.
    """

    def test_a_long_note_is_bounded_AND_SAYS_SO_while_a_short_one_says_so_too(self):
        """BOTH POLARITIES OF THE SAME FIELD, in one payload. `note_more` is
        the entire contract — it must be True where text was withheld and
        False where none was, because a flag that only ever appears one way
        cannot be read as an answer to anything."""
        long_note = ("the signer is exporting again and the chain head moved "
                     "under two seats at once " * 12)
        self.assertGreater(len(long_note), 240)
        self.install(_stub_tasks({
            "task/1": _row("task/1", "long", "open", note=long_note),
            "task/2": _row("task/2", "short", "open", note="one line only"),
            "task/3": _row("task/3", "none", "open"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        by = {e["id"]: e for e in d["entries"] + d["unscoped"]}
        # THE BOUNDED ONE: shorter than it was, flagged, and a PREFIX of the
        # real text rather than a summary or a reflow of it.
        self.assertIs(True, by["task/1"]["note_more"])
        self.assertLess(len(by["task/1"]["note"]), len(long_note))
        self.assertTrue(long_note.startswith(by["task/1"]["note"]),
                        "the excerpt is not the opening of the real note")
        # AND NEVER MID-WORD: a cut inside a word reads as a rendering fault.
        self.assertTrue(long_note[len(by["task/1"]["note"])].isspace(),
                        "the excerpt stopped inside a word")
        # THE CONTROL, on the same observable in the same payload: a note that
        # FITS is untouched and flagged False, so `note_more` is proved to be
        # reporting the text and not simply always true.
        self.assertIs(False, by["task/2"]["note_more"])
        self.assertEqual("one line only", by["task/2"]["note"])
        # AND A ROW WITH NO NOTE AT ALL still carries the flag, so its absence
        # can never be read as either answer.
        self.assertIs(False, by["task/3"]["note_more"])
        self.assertIsNone(by["task/3"]["note"])

    def test_the_whole_note_is_reachable_on_the_route_that_bounds_nothing(self):
        """A BOUND IS ONLY HONEST IF THE REST IS REACHABLE. The list carries an
        excerpt; `/api/task/notes` carries the text whole, which is the door
        the flag promises."""
        long_note = "an argument nobody could fit on a card. " * 30
        self.install(_stub_tasks({
            "task/1": _row("task/1", "long", "open", note=long_note),
        }))
        _s, listing = self.req("/api/tasks")
        row = listing["entries"][0] if listing["entries"] else listing["unscoped"][0]
        self.assertIs(True, row["note_more"], "MUST-HIT: the fixture note was "
                                              "not long enough to be bounded")
        status, one = self.req("/api/task/notes?id=task/1")
        self.assertEqual(status, 200)
        self.assertEqual(long_note, one["note"],
                         "the full note is not served where the excerpt "
                         "says it is")

    def test_the_SHIPPED_card_draws_the_door_and_only_on_the_strict_flag(self):
        """THE SERVER'S FLAG IS ONLY HONEST IF THE PAGE DRAWS IT. Everything
        above proves the wire distinguishes "that is all of it" from "there is
        more"; this proves the thing the owner actually looks at does too. On
        the SHIPPED page, not the source file, for the reason the provenance
        arm above takes the same slice: the assertion is about what his
        browser received.

        STRICT `=== true`, and the falsifier is the point. A truthy test would
        draw a door off any value a future server put in that field, and a
        door that leads nowhere is worse than no door — it tells the reader
        text was withheld when none was. The absence of the door is this card
        saying THAT IS ALL OF IT, so it may only ever appear on the one value
        that means it."""
        _status, body = self.req("/", raw=True)
        body = body.decode("utf-8", "replace")
        tq = body[body.index("task backlog (#218"):body.index(
            "setInterval(tqInit")]
        card = tq[tq.index("function tqCard"):tq.index("async function tqAct")]
        # THE MUST-HIT, unconditional and on the same slice every claim below
        # is made about: the note itself renders here. Without it the arm is
        # searching a card that never drew a note and passing on its absence.
        self.assertIn("r.note ?", card,
                      "the excerpt is not rendered on the shipped card at "
                      "all, so no arm here reads the surface it claims to")
        self.assertIn("r.note_more === true", card,
                      "the card never draws the door the bounded note owes")
        self.assertNotIn("r.note_more ?", card,
                         "a truthy test would draw a door for any value the "
                         "field ever carries")
        self.assertIn('data-kind="note"', card)
        # AND THE DOOR OPENS ON THE ROUTE THAT BOUNDS NOTHING. The filler and
        # the toggle that picks it ride the same shipped slice, so a door
        # wired to nothing cannot ship past this arm.
        self.assertIn("async function tqNoteFull", tq)
        self.assertIn('det.dataset.kind === "note"', tq)
        self.assertIn("/api/task/notes", tq)

    def test_a_CLOSED_row_is_a_tombstone_and_the_payload_says_which(self):
        """A closed row renders as id, title and why it closed. Serving the
        other fourteen fields for 855 of them cost 1.67 MB to draw 0.53 MB.
        The trimmed keys are ABSENT and `tombstone` is what says so — absent
        must never be confused with UNKNOWN, which is this surface's word for
        null."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "still going", "open",
                           owner="seat-a", note="context"),
            "task/2": _row("task/2", "landed", "closed",
                           owner="seat-b", note="context",
                           closed_reason="landed at 4bdca444"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        by = {e["id"]: e for e in d["entries"] + d["unscoped"]}
        tomb, live = by["task/2"], by["task/1"]
        # WHAT THE TOMBSTONE KEEPS is exactly what the card draws.
        self.assertIs(True, tomb["tombstone"])
        self.assertEqual("landed", tomb["title"])
        self.assertEqual("landed at 4bdca444", tomb["closed_reason"])
        self.assertEqual("closed", tomb["status"])
        # WHAT IT DROPS is dropped, not nulled.
        for key in ("note", "owner", "refs", "age_s", "ts_epoch", "last_note"):
            self.assertNotIn(key, tomb,
                             "%r rode a tombstone that never renders it" % key)
        # THE CONTROL: the live row in the SAME payload keeps every one of
        # them, so the absences above are a trim and not a route that stopped
        # sending those fields to anybody.
        for key in ("note", "owner", "refs", "age_s", "ts_epoch", "last_note"):
            self.assertIn(key, live, "the live row lost %r too, so this arm "
                                     "proves nothing about closed rows" % key)
        self.assertNotIn("tombstone", live,
                         "a live row was marked as a tombstone")

    def test_two_fields_with_no_reader_left_the_wire(self):
        """`source` and `last_updated` were serialized on every row and read by
        NOTHING — no renderer under web_ui, no assertion in these suites. 234
        KB of wire for a field with no consumer."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "a row", "open", source="corpus",
                           last_updated="2026-08-05T00:00:00Z"),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        row = (d["entries"] + d["unscoped"])[0]
        self.assertNotIn("source", row)
        self.assertNotIn("last_updated", row)
        # THE CONTROL on the same row: the fields that DO have readers are
        # still here, so the two absences above are a trim rather than a
        # projection that collapsed.
        for key in ("id", "title", "status", "owner", "refs", "priority"):
            self.assertIn(key, row, "the projection lost %r, so the absences "
                                    "above are not a deliberate trim" % key)

    def test_the_headline_still_counts_the_rows_the_trim_did_not_change(self):
        """THE TRIM IS AT EMIT AND NOWHERE EARLIER. `counts` and `queue` are
        computed over the FULL projection, so bounding the bytes may not move
        a single number — a headline describing a population the trim invented
        is the exact disagreement this endpoint was built to prevent."""
        self.install(_stub_tasks({
            "task/1": _row("task/1", "blocker", "open", priority="P0",
                           ts="1690000000", note="x" * 4000),
            "task/2": _row("task/2", "working", "in_progress", priority="P1",
                           ts="1700000000", note="y" * 4000),
            "task/3": _row("task/3", "done", "closed", ts="1690000000",
                           closed_reason="landed", note="z" * 4000),
        }))
        status, d = self.req("/api/tasks")
        self.assertEqual(status, 200)
        self.assertEqual({"open": 1, "in_progress": 1, "closed": 1},
                         d["counts"],
                         "the trim moved the counts")
        q = d["queue"]
        self.assertEqual(1, q["P0"])
        self.assertEqual(1, q["P1"])
        self.assertEqual(2, q["live"],
                         "the headline counted a population the trim invented")
        # AND EVERY ROW IS STILL ON THE WIRE. The cure is fewer BYTES, never
        # fewer rows: a reader counting this list must get the same answer it
        # always did.
        self.assertEqual(3, len(d["entries"]) + len(d["unscoped"]),
                         "the byte trim dropped a row")
