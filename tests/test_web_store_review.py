#!/usr/bin/env python3
"""helm.web store-review surface — hermetic contract tests.

The owner review panel near the configs view (owner canon 2026-07-22): a
candidate + provisional browse/approve/reject surface. GET /api/store/review
lists the two non-ratified states; POST /api/store/confirm|reject route through
the SAME store functions the CLI calls (one writer path) and demand the
per-process bearer (403 without). Runs against an ephemeral-port server over a
tmp HELM_HOME + HELM_ADOPTED_DIR; the real ~/.helm and ~/.claude are untouched.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, store, web  # noqa: E402

TS = "2026-07-20T00:00:00Z"


class TestWebStoreReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webrev-")
        cls.env_prior = {k: os.environ.get(k)
                         for k in ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ.pop("MELD_HOME", None)
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(cls.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # a clean, known fixture per test (the server reads fresh from disk):
        # one live, one candidate, one xrev-cleared provisional.
        pdir = os.path.join(home.global_dir(), "premises")
        ldir = os.path.join(home.global_dir(), "lexicon")
        for d in (pdir, ldir):
            shutil.rmtree(d, ignore_errors=True)
        store.write_prior({"id": "live-law", "statement": "the confirmed truth",
                           "confidence": "0.8", "keywords": "glorpwork",
                           "status": "live", "stated_ts": TS}, root_dir=pdir)
        store.write_prior({"id": "cand-law", "statement": "a raw inferred guess",
                           "confidence": "0.7", "keywords": "glorpwork",
                           "status": "candidate", "source": "inferred",
                           "stated_ts": TS}, root_dir=pdir)
        store.write_prior({"id": "prov-law", "statement": "a cleared belief",
                           "confidence": "0.7", "keywords": "glorpwork",
                           "status": "provisional", "source": "inferred",
                           "xrev_by": "codex-seat", "xrev_ts": TS,
                           "stated_ts": TS}, root_dir=pdir)

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
                return resp.status, (body if raw else json.loads(body or b"null"))
        except urllib.error.HTTPError as e:
            with e:
                body = e.read()
                return e.code, (body if raw else json.loads(body or b"null"))

    # -- GET /api/store/review ---------------------------------------------
    def test_review_lists_candidate_and_provisional_not_live(self):
        status, d = self.req("/api/store/review")
        self.assertEqual(status, 200, d)
        by_id = {r["id"]: r for r in d["entries"]}
        self.assertEqual(set(by_id), {"cand-law", "prov-law"},
                         "the review queue is the two non-ratified states, never live")
        self.assertEqual(d["counts"], {"candidate": 1, "provisional": 1})
        # the provisional row carries the xrev-clear receipt for display
        self.assertEqual(by_id["prov-law"]["xrev_by"], "codex-seat")
        self.assertEqual(by_id["prov-law"]["status"], "provisional")
        # the candidate has no clearance yet
        self.assertEqual(by_id["cand-law"]["xrev_by"], "")
        # captured-by/statement are surfaced
        self.assertEqual(by_id["cand-law"]["source"], "inferred")
        self.assertIn("raw inferred guess", by_id["cand-law"]["statement"])

    def test_review_never_500s(self):
        # the endpoint degrades to unavailable, never a 500 (store-summary law)
        status, _, body = self._get_raw("/api/store/review")
        self.assertEqual(status, 200)

    def _get_raw(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read()

    # -- POST /api/store/confirm (owner ratify, one writer path) -----------
    def test_confirm_ratifies_through_the_store(self):
        status, d = self.req("/api/store/confirm", {"id": "prov-law"})
        self.assertEqual(status, 200, d)
        self.assertEqual((d["status"], d["id"]), ("live", "prov-law"))
        # the SAME store now sees it live + firing (one writer path)
        self.assertEqual(store._find("prov-law")["status"], "live")
        self.assertIn("prov-law", [e["id"] for e in store.resolve_prompt("glorpwork")])
        # and it dropped off the review queue
        _, r = self.req("/api/store/review")
        self.assertNotIn("prov-law", [x["id"] for x in r["entries"]])

    def test_confirm_a_candidate_too(self):
        status, d = self.req("/api/store/confirm", {"id": "cand-law"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "live")
        self.assertEqual(store._find("cand-law")["status"], "live")

    def test_confirm_missing_id_and_unknown_id(self):
        status, d = self.req("/api/store/confirm", {})
        self.assertEqual(status, 400)
        self.assertIn("id is required", d["error"])
        status, d = self.req("/api/store/confirm", {"id": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("not found", d["error"])

    # -- POST /api/store/reject (retire in place, reason box) --------------
    def test_reject_retires_in_place_with_reason(self):
        status, d = self.req("/api/store/reject",
                             {"id": "cand-law", "reason": "misread the log"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "retired")
        self.assertEqual(d["retired_why"], "misread the log")
        # the record law: the file stays, carrying the receipt
        e = store._find("cand-law")
        self.assertEqual((e["status"], e["retired_why"]), ("retired", "misread the log"))
        self.assertTrue(os.path.isfile(e["path"]))
        # gone from the review queue AND from the injecting lanes
        _, r = self.req("/api/store/review")
        self.assertNotIn("cand-law", [x["id"] for x in r["entries"]])

    def test_reject_a_provisional_too(self):
        status, d = self.req("/api/store/reject", {"id": "prov-law"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "retired")
        self.assertEqual([e["id"] for e in store.resolve_prompt("glorpwork")],
                         ["live-law"], "the retired provisional stops firing")

    # -- mutation hardening: every store POST demands the bearer -----------
    def test_store_posts_403_without_token(self):
        for ep in ("/api/store/confirm", "/api/store/reject"):
            status, d = self.req(ep, {"id": "cand-law"}, token=False)
            self.assertEqual(status, 403, (ep, d))
            self.assertIn("error", d)
        # a 403 must not mutate: the candidate is untouched
        self.assertEqual(store._find("cand-law")["status"], "candidate")

    # -- the UI carries the review view markup + JS + the token ------------
    def test_ui_review_markup_and_js(self):
        status, body = self.req("/", raw=True)
        self.assertEqual(status, 200)
        body = body.decode("utf-8")
        for marker in ('id="storerev"', 'id="storerevlist"', 'id="storerevmeta"',
                       'id="storerevReload"', 'class="srevbadge'):
            self.assertIn(marker, body, "store review markup missing: %s" % marker)
        for fn in ("srevInit", "srevRender", "srevRow", "srevAct"):
            self.assertIn("function " + fn, body, "review JS missing: %s" % fn)
        self.assertIn("/api/store/review", body)
        self.assertIn("/api/store/confirm", body)
        self.assertIn("/api/store/reject", body)
        self.assertNotIn("__HELM_TOKEN__", body)
        self.assertIn(web.MUTATION_TOKEN, body)


if __name__ == "__main__":
    unittest.main()
