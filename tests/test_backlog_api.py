#!/usr/bin/env python3
"""API /api/backlog — GET with query params (task/3445 slice L1a).

A full backlog listing: filter by status / priority / owner / project,
sort by priority | updated | created | id, paginate page+per_page (default
50, cap 200), and return facet counts for every filter so the client can
render "P1 (211)" chips. No params -> byte-identical to /api/tasks payload.

Pure Python, stdlib only. Every arm against a STUB that implements exactly
the settled tasks.snapshot() contract.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web  # noqa: E402
from tests.test_web_tasks import (  # noqa: E402
    _row,
    _stub_tasks,
    TasksBase,
    ENV_KEYS,
)


class BacklogBase(TasksBase):
    """Boots a stub tasks module, hits the server, tears down."""

    def install(self, mod):
        sys.modules["helm.tasks"] = mod
        import helm as _helm_pkg
        _helm_pkg.tasks = mod
        return mod

    def req(self, path, payload=None, qs=None, token=True, raw=False):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if qs:
            from urllib.parse import urlencode
            # doseq: a value may be a string or a one-item list (parse_qs)
            url += "?" + urlencode(qs, doseq=True)
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


# ---------------------------------------------------------------------------
# Core: no-param response has required fields (backward compat)
# ---------------------------------------------------------------------------

class NoParamBackwardCompatTest(BacklogBase):
    """With NO query parameters, /api/backlog returns the same shape as
    /api/tasks — paginated entries, total_count, facets."""

    def test_no_params_has_required_fields(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open", priority="P1"),
        }))
        status, d = self.req("/api/backlog")
        self.assertEqual(status, 200)
        self.assertIn("entries", d)
        self.assertIn("total_count", d)
        self.assertIn("facets", d)
        self.assertIn("page", d)
        self.assertIn("total_pages", d)
        json.dumps(d)  # must be serializable


# ---------------------------------------------------------------------------
# Filter: status
# ---------------------------------------------------------------------------

class FilterStatusTest(BacklogBase):
    def test_status_open_returns_only_open(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
            "task/2": _row("task/2", "b", "in_progress"),
            "task/3": _row("task/3", "c", "closed"),
        }))
        status, d = self.req("/api/backlog", qs={"status": "open"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertNotIn("task/2", ids)
        self.assertNotIn("task/3", ids)
        self.assertIn("task/1", ids)

    def test_status_multiple_comma_separated(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
            "task/2": _row("task/2", "b", "in_progress"),
            "task/3": _row("task/3", "c", "closed"),
        }))
        status, d = self.req("/api/backlog", qs={"status": "open,closed"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertNotIn("task/2", ids)
        self.assertIn("task/1", ids)
        self.assertIn("task/3", ids)

    def test_status_all_returns_everything(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
            "task/2": _row("task/2", "b", "closed"),
        }))
        status, d = self.req("/api/backlog", qs={"status": "all"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 2)

    def test_status_unknown_empty(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"status": "unknown"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 0)


# ---------------------------------------------------------------------------
# Filter: priority
# ---------------------------------------------------------------------------

class FilterPriorityTest(BacklogBase):
    def test_priority_p0(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open", priority="P1"),
            "task/3": _row("task/3", "c", "open", priority="P0"),
            "task/4": _row("task/4", "d", "open"),  # unranked
        }))
        status, d = self.req("/api/backlog", qs={"priority": "P0"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/1", ids)
        self.assertIn("task/3", ids)
        self.assertNotIn("task/2", ids)
        self.assertNotIn("task/4", ids)

    def test_priority_all_unchanged(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open", priority="P3"),
        }))
        status, d = self.req("/api/backlog", qs={"priority": "all"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/1", ids)
        self.assertIn("task/2", ids)

    def test_priority_unranked_empty(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
        }))
        status, d = self.req("/api/backlog", qs={"priority": "P5"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 0)

    def test_priority_unranked_returns_none(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"priority": "unranked"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertNotIn("task/1", ids)
        self.assertIn("task/2", ids)

    def test_priority_p1_unranked(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open", priority="P1"),
            "task/3": _row("task/3", "c", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"priority": "P1,unranked"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/2", ids)
        self.assertIn("task/3", ids)
        self.assertNotIn("task/1", ids)


# ---------------------------------------------------------------------------
# Filter: owner (one seat, exact and case-blind: the ledger's own holder rule,
# tasks.held_by, under which Alice and alice are one seat address)
# ---------------------------------------------------------------------------

class FilterOwnerTest(BacklogBase):
    def test_owner_filter_matches(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", owner="alice"),
            "task/2": _row("task/2", "b", "open", owner="bob"),
            "task/3": _row("task/3", "c", "open", owner="Alice"),
        }))
        status, d = self.req("/api/backlog", qs={"owner": "alice"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/1", ids)
        self.assertIn("task/3", ids)
        self.assertNotIn("task/2", ids)
        # never a substring
        status, d = self.req("/api/backlog", qs={"owner": "ali"})
        self.assertEqual(d["entries"], [])

    def test_owner_filter_empty(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", owner="alice"),
        }))
        status, d = self.req("/api/backlog", qs={"owner": "charlie"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 0)

    def test_owner_unowned(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", owner="alice"),
            "task/2": _row("task/2", "b", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"owner": "unowned"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertNotIn("task/1", ids)
        self.assertIn("task/2", ids)


# ---------------------------------------------------------------------------
# Filter: project
# ---------------------------------------------------------------------------

class FilterProjectTest(BacklogBase):
    def test_project_filter(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", project="helm"),
            "task/2": _row("task/2", "b", "open", project="akka"),
            "task/3": _row("task/3", "c", "open"),  # no project
        }))
        status, d = self.req("/api/backlog", qs={"project": "helm"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/1", ids)
        self.assertNotIn("task/2", ids)
        self.assertNotIn("task/3", ids)


# ---------------------------------------------------------------------------
# Filter: q (full-text title search, case-insensitive)
# ---------------------------------------------------------------------------

class FilterQTest(BacklogBase):
    def test_q_search(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "fix the wire", "open"),
            "task/2": _row("task/2", "deploy the app", "open"),
            "task/3": _row("task/3", "wire the console", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"q": "wire"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/1", ids)
        self.assertIn("task/3", ids)
        self.assertNotIn("task/2", ids)

    def test_q_search_empty(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "fix the wire", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"q": "deploy"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 0)


# ---------------------------------------------------------------------------
# Sort: priority (default)
# ---------------------------------------------------------------------------

class SortPriorityTest(BacklogBase):
    def test_sort_by_priority(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P2",
                           ts="2026-01-01T00:00:00Z"),
            "task/2": _row("task/2", "b", "open", priority="P0",
                           ts="2026-01-02T00:00:00Z"),
            "task/3": _row("task/3", "c", "open", priority="P1",
                           ts="2026-01-03T00:00:00Z"),
        }))
        status, d = self.req("/api/backlog", qs={"sort": "priority"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertEqual(ids[0], "task/2")  # P0 first
        self.assertEqual(ids[1], "task/3")  # P1 second
        self.assertEqual(ids[2], "task/1")  # P2 last


# ---------------------------------------------------------------------------
# Sort: created (newest first, GitHub's order; `oldest` is the reverse)
# ---------------------------------------------------------------------------

class SortCreatedTest(BacklogBase):
    def test_sort_by_created(self):
        # epoch stamps: the shared stub's parser reads numbers only
        day = 86400.0
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P1",
                           ts=1_760_000_000 + 2 * day),
            "task/2": _row("task/2", "b", "open", priority="P0",
                           ts=1_760_000_000),
            "task/3": _row("task/3", "c", "open", priority="P0",
                           ts=1_760_000_000 + day),
        }))
        status, d = self.req("/api/backlog", qs={"sort": "created"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertEqual(ids, ["task/1", "task/3", "task/2"])  # newest first
        status, d = self.req("/api/backlog", qs={"sort": ["oldest"]})
        self.assertEqual([e["id"] for e in d["entries"]],
                         ["task/2", "task/3", "task/1"])     # oldest first


# ---------------------------------------------------------------------------
# Sort: id (numeric order)
# ---------------------------------------------------------------------------

class SortIdTest(BacklogBase):
    def test_sort_by_id(self):
        stub = self.install(_stub_tasks({
            "task/263": _row("task/263", "a", "open", priority="P1"),
            "task/45": _row("task/45", "b", "open", priority="P0"),
            "task/7": _row("task/7", "c", "open", priority="P0"),
        }))
        status, d = self.req("/api/backlog", qs={"sort": "id"})
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertEqual(ids, ["task/7", "task/45", "task/263"])


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

class PagingTest(BacklogBase):
    def test_default_page_1(self):
        rows = {
            "task/%d" % i: _row("task/%d" % i, "r%d" % i, "open")
            for i in range(1, 51)
        }
        stub = self.install(_stub_tasks(rows))
        status, d = self.req("/api/backlog")
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 50)
        self.assertEqual(d["total_count"], 50)

    def test_per_page_cap(self):
        rows = {
            "task/%d" % i: _row("task/%d" % i, "r%d" % i, "open")
            for i in range(1, 101)
        }
        stub = self.install(_stub_tasks(rows))
        status, d = self.req("/api/backlog", qs={"per_page": "300"})
        self.assertEqual(status, 200)
        # 100 rows, cap 200 -> all fit in one page
        self.assertEqual(len(d["entries"]), 100)

    def test_page_2_skips_first_page(self):
        rows = {
            "task/%d" % i: _row("task/%d" % i, "r%d" % i, "open",
                                 ts="2026-01-%02dT00:00:00Z" % (i % 28 + 1))
            for i in range(1, 101)
        }
        stub = self.install(_stub_tasks(rows))
        status, d = self.req("/api/backlog", qs={"per_page": "10", "page": "2"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 10)
        self.assertEqual(d["page"], 2)
        ids = [e["id"] for e in d["entries"]]
        self.assertIn("task/11", ids)

    def test_page_past_end_empty(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
        }))
        status, d = self.req("/api/backlog", qs={"per_page": "10", "page": "10"})
        self.assertEqual(status, 200)
        self.assertEqual(len(d["entries"]), 0)


class TotalPagesTest(BacklogBase):
    def test_total_pages(self):
        rows = {
            "task/%d" % i: _row("task/%d" % i, "r%d" % i, "open")
            for i in range(1, 76)  # 75 rows
        }
        stub = self.install(_stub_tasks(rows))
        status, d = self.req("/api/backlog", qs={"per_page": "20"})
        self.assertEqual(status, 200)
        self.assertEqual(d["total_count"], 75)
        self.assertEqual(d["total_pages"], 4)  # ceil(75/20)


# ---------------------------------------------------------------------------
# Facet counts
# ---------------------------------------------------------------------------

class FacetCountsTest(BacklogBase):
    def test_facet_counts_status(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open"),
            "task/2": _row("task/2", "b", "open"),
            "task/3": _row("task/3", "c", "closed"),
        }))
        status, d = self.req("/api/backlog")
        self.assertEqual(status, 200)
        facets = d.get("facets", {})
        self.assertEqual(facets.get("status", {}).get("open", 0), 2)
        self.assertEqual(facets.get("status", {}).get("closed", 0), 1)

    def test_facet_counts_priority(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "open", priority="P1"),
            "task/3": _row("task/3", "c", "open", priority="P1"),
            "task/4": _row("task/4", "d", "open"),  # no priority
        }))
        status, d = self.req("/api/backlog")
        self.assertEqual(status, 200)
        facets = d.get("facets", {})
        pri = facets.get("priority", {})
        self.assertEqual(pri.get("P0", 0), 1)
        self.assertEqual(pri.get("P1", 0), 2)
        # the unranked bucket's facet key is the filter token that selects
        # it (task/3445 L1b), so the page sends back the key it was handed
        self.assertEqual(pri.get("unranked", 0), 1)

    def test_facet_counts_filter_applied(self):
        """Each facet counts what its choices WOULD select with every OTHER
        filter kept (task/3445 L1b): the status facet ignores the status
        filter, so picking "closed" still shows how many are open, while
        the priority facet is narrowed by it."""
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0"),
            "task/2": _row("task/2", "b", "closed", priority="P0"),
            "task/3": _row("task/3", "c", "closed", priority="P1"),
        }))
        status, d = self.req("/api/backlog", qs={"status": "closed"})
        self.assertEqual(status, 200)
        facets = d.get("facets", {})
        self.assertEqual(facets.get("status", {}).get("open", 0), 1)
        self.assertEqual(facets.get("status", {}).get("closed", 0), 2)
        self.assertEqual(facets.get("priority", {}), {"P0": 1, "P1": 1})


# ---------------------------------------------------------------------------
# Combined filters (AND logic)
# ---------------------------------------------------------------------------

class CombinedFilterTest(BacklogBase):
    def test_combined_filters(self):
        stub = self.install(_stub_tasks({
            "task/1": _row("task/1", "a", "open", priority="P0", owner="alice"),
            "task/2": _row("task/2", "b", "open", priority="P1", owner="alice"),
            "task/3": _row("task/3", "c", "open", priority="P0", owner="bob"),
            "task/4": _row("task/4", "d", "closed", priority="P0", owner="alice"),
        }))
        status, d = self.req("/api/backlog", qs={
            "status": "open", "priority": "P0", "owner": "alice",
        })
        self.assertEqual(status, 200)
        ids = [e["id"] for e in d["entries"]]
        self.assertEqual(ids, ["task/1"])


# ---------------------------------------------------------------------------
# Unavailable degrade
# ---------------------------------------------------------------------------

class UnavailableTest(BacklogBase):
    def test_unavailable_becomes_unknown(self):
        self.install(_stub_tasks({}, unavailable="store error"))
        status, d = self.req("/api/backlog")
        self.assertEqual(status, 200)
        self.assertTrue(d.get("unavailable"))
        self.assertIn("why", d)
        self.assertEqual(d["entries"], [])


if __name__ == "__main__":
    unittest.main()
