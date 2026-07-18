"""helm.web sessions-surface contract tests (slice B: catalog / search /
session / cmd / cwd / prune + the sessions view UI). Hermetic: ephemeral-port
server over a tmp HELM_HOME, catalog roots + caches + overrides + the claude
projects dir repointed at tmp dirs, HELM_CATALOG=scanner (cv is never invoked
for the catalog; cv-touching seams are stubbed per test), broken providers
injected in both web and transcripts — the real ~/.helm, ~/.claude and
~/.cache are never touched."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import catalog, home, pk, transcripts, web  # noqa: E402
from helm.providers import ProviderError  # noqa: E402

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"    # claude, project alpha
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"    # claude, project beta
SID_SYN = "cccccccc-1111-2222-3333-444444444444"  # synthetic summarizer, alpha
SID_CX = "dddddddd-1111-2222-3333-444444444444"   # codex
SID_NEW = "ffffffff-1111-2222-3333-444444444444"  # planted mid-suite (refresh)


class BrokenProvider:
    """Every verb raises ProviderError — the no-provider machine."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise ProviderError("no quota provider on this machine")
        return boom


def _plant_claude(sid, cwd, title, body_lines=(), pad=True):
    d = os.path.join(catalog.CLAUDE_ROOTS[0], "slug-" + sid[:8])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sid + ".jsonl")
    lines = [{"cwd": cwd, "gitBranch": "main", "timestamp": "2026-07-01T10:00:00Z",
              "message": {"role": "user", "content": title}}]
    lines += [{"message": {"role": "assistant", "content": t}} for t in body_lines]
    if pad:  # the scanner ignores files under 200 bytes
        lines.append({"message": {"role": "assistant", "content": "x" * 300}})
    with open(path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    return path


def _plant_codex(sid, cwd):
    d = os.path.join(catalog.CODEX_ROOTS[0], "2026")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"rollout-2026-07-01T10-00-00-{sid}.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"timestamp": "2026-07-01T10:00:00Z", "cwd": cwd,
                            "text": "a codex session doing codex things"}) + "\n")
        f.write(json.dumps({"text": "y" * 300}) + "\n")
    return path


def _fake_cv_show(alpha, n=9, target_at=3):
    msgs = []
    for i in range(n):
        text = ("here is the target payload" if i == target_at
                else "filler item %d" % i)
        msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                     "timestamp": "2026-07-01T10:0%d:00Z" % (i % 10),
                     "content": [{"kind": "text", "text": text}]})

    def fake(sid, rng=None, harness=None):
        a, b = (int(x) for x in rng.split("-"))
        return {"messages": msgs[a:b], "title": "stub title", "cwd": alpha}
    return fake


class TestWebSessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-websess-")
        j = lambda *p: os.path.join(cls.tmp, *p)
        cls.env_prior = {k: os.environ.get(k)
                         for k in ("HELM_HOME", "MELD_HOME", "HELM_CATALOG")}
        os.environ["HELM_HOME"] = cls.tmp
        os.environ["HELM_CATALOG"] = "scanner"  # never shell out to cv for the catalog
        os.environ.pop("MELD_HOME", None)
        assert home.helm_home() == cls.tmp, "HELM_HOME override must win"
        pk.write_json(home.registry_path(), {"version": 1, "generated_ts": pk.now_ts(),
                                             "projects": {"alpha": {
                                                 "name": "alpha", "path": j("work", "alpha"),
                                                 "sessions": {"claude": 1}}}})
        # catalog + transcripts state -> tmp (the test_transcripts repoint pattern)
        cls._cat = {k: getattr(catalog, k) for k in
                    ("CLAUDE_ROOTS", "CODEX_ROOTS", "CACHE_DIR", "CACHE", "SYN_CACHE")}
        cls._tr = {k: getattr(transcripts, k) for k in
                   ("OVERRIDES_PATH", "MINTS_PATH", "CLAUDE_PROJECTS")}
        catalog.CLAUDE_ROOTS = [j("claude-root")]
        catalog.CODEX_ROOTS = [j("codex-root")]
        catalog.CACHE_DIR = j("cache")
        catalog.CACHE = j("cache", "catalog-cache.json")
        catalog.SYN_CACHE = j("cache", "syn-cache.json")
        transcripts.OVERRIDES_PATH = j("cache", "cwd-overrides.json")
        transcripts.MINTS_PATH = j("cache", "mints.jsonl")
        transcripts.CLAUDE_PROJECTS = j("claude-projects")
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        web._qstate.clear()
        cls.web_provider_prior = web._PROVIDER
        cls.tr_provider_prior = transcripts._PROVIDER
        web._PROVIDER = BrokenProvider()
        transcripts._PROVIDER = BrokenProvider()
        cls.alpha = j("work", "alpha")
        cls.beta = j("work", "beta")
        os.makedirs(cls.alpha)
        os.makedirs(cls.beta)
        _plant_claude(SID_A, cls.alpha, "fix the flux capacitor wiring")
        _plant_claude(SID_B, cls.beta, "unrelated beta work",
                      body_lines=["l%d" % i for i in range(8)], pad=False)
        _plant_claude(SID_SYN, cls.alpha,
                      "You are summarizing a Claude Code session about the flux capacitor")
        _plant_codex(SID_CX, cls.beta)
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        web._PROVIDER = cls.web_provider_prior
        transcripts._PROVIDER = cls.tr_provider_prior
        web._qstate.clear()
        transcripts._state.clear()
        transcripts._cwd_overrides = {}
        for k, v in cls._cat.items():
            setattr(catalog, k, v)
        for k, v in cls._tr.items():
            setattr(transcripts, k, v)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- plumbing ----------------------------------------------------------
    def req(self, path, payload=None):
        """(status, body_json) — 4xx/5xx returned, not raised. payload -> POST."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(url, data=data, headers=(
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + web.MUTATION_TOKEN} if data else {}))
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    # -- /api/catalog ------------------------------------------------------
    def test_catalog_shape_and_refresh(self):
        status, d = self.req("/api/catalog")
        self.assertEqual(status, 200)
        for key in ("rows", "stats", "scanned_at"):
            self.assertIn(key, d, "sesh /api/catalog shape: missing %s" % key)
        sids = {r["i"] for r in d["rows"]}
        self.assertLessEqual({SID_A, SID_B, SID_SYN, SID_CX}, sids)
        row = next(r for r in d["rows"] if r["i"] == SID_A)
        for key in ("h", "i", "c", "b", "t", "z", "m", "cr", "u", "mt", "p",
                    "cwd", "syn", "xl"):
            self.assertIn(key, row, "catalog row shape: missing %s" % key)
        self.assertEqual(row["h"], "claude")
        self.assertEqual(row["cwd"], self.alpha)
        syn = next(r for r in d["rows"] if r["i"] == SID_SYN)
        self.assertTrue(syn["syn"])
        # incremental refresh picks up a newly planted transcript
        _plant_claude(SID_NEW, self.alpha, "freshly planted work")
        status, d = self.req("/api/catalog")
        self.assertNotIn(SID_NEW, {r["i"] for r in d["rows"]},
                         "unrefreshed catalog must serve the cache")
        status, d = self.req("/api/catalog?refresh=1")
        self.assertIn(SID_NEW, {r["i"] for r in d["rows"]})

    def test_catalog_opensession_format(self):
        status, d = self.req("/api/catalog?format=opensession")
        self.assertEqual(status, 200)
        self.assertIn("sessions", d)
        s = next(x for x in d["sessions"] if x["id"] == SID_A)
        for key in ("harness", "id", "cwd", "title", "git", "createdAt",
                    "updatedAt", "messageCount", "sizeBytes", "path"):
            self.assertIn(key, s)
        self.assertEqual(s["git"], {"branch": "main"})

    # -- /api/search -------------------------------------------------------
    def test_search_scoped_grep_hides_synthetic(self):
        q = urllib.parse.urlencode({"q": "flux capacitor", "scope": "alpha"})
        status, d = self.req("/api/search?" + q)
        self.assertEqual(status, 200)
        self.assertEqual([h["sid"] for h in d["hits"]], [SID_A])
        self.assertTrue(d["source"].startswith("scoped-grep"))
        self.assertEqual(d["scope"], "alpha")
        hit = d["hits"][0]
        for key in ("harness", "id8", "date", "title", "snippet", "sid", "cvid", "syn"):
            self.assertIn(key, hit, "sesh /api/search hit shape: missing %s" % key)
        self.assertIn("flux capacitor", hit["snippet"])
        # synthetic=1 surfaces the reference transcript too
        status, d = self.req("/api/search?" + q + "&synthetic=1")
        self.assertEqual(sorted(h["sid"] for h in d["hits"]), sorted([SID_A, SID_SYN]))
        self.assertTrue(next(h for h in d["hits"] if h["sid"] == SID_SYN)["syn"])

    def test_search_requires_q(self):
        status, d = self.req("/api/search")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- /api/session ------------------------------------------------------
    def test_session_tail_window_paging_and_find(self):
        fake = _fake_cv_show(self.alpha, n=9, target_at=3)
        with mock.patch.object(transcripts, "_cv_show", fake):
            status, d = self.req("/api/session?sid=%s&limit=4" % SID_B)
            self.assertEqual(status, 200)
            self.assertEqual((d["total"], d["start"], d["end"]), (9, 5, 9))
            self.assertEqual(len(d["messages"]), 4)
            m = d["messages"][0]
            for key in ("role", "ts", "items"):
                self.assertIn(key, m)
            self.assertTrue(d["session"]["resumable"])
            self.assertEqual(d["session"]["harness"], "claude")
            # before= pages backward (the drawer's "load earlier")
            status, d = self.req("/api/session?sid=%s&before=5&limit=4" % SID_B)
            self.assertEqual((d["start"], d["end"]), (1, 5))
            # find= centers the window on the last match
            status, d = self.req("/api/session?sid=%s&limit=4&find=target" % SID_B)
            self.assertEqual(d["match"], {"index": 2})
            self.assertIn("target", d["messages"][2]["items"][0]["t"])
            status, d = self.req("/api/session?sid=%s&limit=4&find=zzz-absent" % SID_B)
            self.assertIsNone(d["match"])
            self.assertIn("no match", d["note"])

    def test_session_requires_sid_and_int_params(self):
        status, d = self.req("/api/session")
        self.assertEqual(status, 400)
        status, d = self.req("/api/session?sid=%s&before=nope" % SID_B)
        self.assertEqual(status, 400)

    # -- /api/cmd ----------------------------------------------------------
    def test_cmd_omitted_account_degrades_to_default(self):
        # broken provider => no creds => "(default)" (sesh's degraded handling)
        status, d = self.req("/api/cmd?sid=%s" % SID_A[:12])
        self.assertEqual(status, 200, d)
        for key in ("cmd", "preflight", "warnings", "session"):
            self.assertIn(key, d, "sesh /api/cmd shape: missing %s" % key)
        self.assertIn("claude", d["cmd"])
        self.assertIn("--resume %s" % SID_A, d["cmd"])
        self.assertIn("cd ", d["cmd"])  # recorded cwd exists -> cd prefix
        self.assertIn("preflight_error", d["preflight"])
        self.assertEqual(d["session"]["id"], SID_A)
        for key in ("id", "title", "harness", "cwd"):
            self.assertIn(key, d["session"])

    def test_cmd_named_account_falls_back_natively(self):
        status, d = self.req("/api/cmd?sid=%s&account=%s&model=fable"
                             % (SID_A, urllib.parse.quote("alice@example.com")))
        self.assertEqual(status, 200, d)
        self.assertIn("--resume %s" % SID_A, d["cmd"])
        self.assertIn("--model fable", d["cmd"])
        self.assertTrue(any("quota provider unavailable" in w for w in d["warnings"]),
                        "provider fallback must be a visible warning: %r" % d)

    def test_cmd_requires_sid(self):
        status, d = self.req("/api/cmd")
        self.assertEqual(status, 400)
        self.assertIn("error", d)

    # -- POST /api/cwd -----------------------------------------------------
    def test_cwd_post_rehomes_then_resets(self):
        new_cwd = os.path.join(self.tmp, "rehome-target")
        os.makedirs(new_cwd, exist_ok=True)
        status, d = self.req("/api/cwd", {"sid": SID_A[:12], "cwd": new_cwd})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["ok"] and d["linked"])
        slug = new_cwd.replace("/", "-").replace(".", "-")
        target = os.path.join(transcripts.CLAUDE_PROJECTS, slug, SID_A + ".jsonl")
        self.assertTrue(os.path.islink(target))
        # override applied at read time on the next catalog read
        status, cat = self.req("/api/catalog")
        row = next(r for r in cat["rows"] if r["i"] == SID_A)
        self.assertEqual(row["cwd"], new_cwd)
        self.assertTrue(row.get("cwdOverride"))
        # --reset: cwd null removes the override, leaves the symlink (harmless)
        status, d = self.req("/api/cwd", {"sid": SID_A, "cwd": None})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["ok"])
        self.assertIsNone(d["cwd"])
        self.assertEqual(json.load(open(transcripts.OVERRIDES_PATH)), {})
        self.assertTrue(os.path.islink(target))
        status, cat = self.req("/api/catalog")
        row = next(r for r in cat["rows"] if r["i"] == SID_A)
        self.assertEqual(row["cwd"], self.alpha)

    def test_cwd_post_rejects_bad_input(self):
        status, d = self.req("/api/cwd", {"sid": SID_A, "cwd": "rel/path"})
        self.assertEqual(status, 400)
        self.assertIn("error", d)
        status, d = self.req("/api/cwd", {"sid": SID_A,
                                          "cwd": os.path.join(self.tmp, "absent")})
        self.assertEqual(status, 400)
        status, d = self.req("/api/cwd", {"sid": "zzzz"})
        self.assertEqual(status, 400)

    # -- POST /api/prune ---------------------------------------------------
    def test_prune_post_dry_run(self):
        with mock.patch.object(transcripts, "_cv_prune_help",
                               lambda: "--thinking --window"):
            status, d = self.req("/api/prune", {"sid": SID_A[:12], "preset": "lean",
                                                "dry": True})
            self.assertEqual(status, 200, d)
            self.assertTrue(d["dry"])
            self.assertEqual(d["sid"], SID_A)
            self.assertEqual(d["willRun"], "cv prune %s --thinking" % SID_A)
            for key in ("beforeBytes", "beforeMsgs", "note"):
                self.assertIn(key, d["estimate"])
            # claude-only: a codex sid is a 400, and NO prune ran
            status, d = self.req("/api/prune", {"sid": SID_CX, "dry": True})
            self.assertEqual(status, 400)
            self.assertIn("claude sessions only", d["error"])
            status, d = self.req("/api/prune", {"sid": SID_A, "preset": "bogus",
                                                "dry": True})
            self.assertEqual(status, 400)

    # -- the sessions view UI ---------------------------------------------
    def test_ui_carries_sessions_controls_popover_and_drawer(self):
        url = "http://127.0.0.1:%d/" % self.port
        with urllib.request.urlopen(url, timeout=10) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
        for marker in ('id="sesscontrols"', 'id="sq"', 'id="scope"',
                       'id="scopelist"', 'id="synToggle"', 'data-h="claude"',
                       'data-h="codex"', 'id="viewmode"', 'id="model"',
                       'id="rec"', 'id="refresh"', 'id="sesscount"',
                       'id="resumeAs"', 'id="list"'):
            self.assertIn(marker, body, "sessions controls markup missing: %s" % marker)
        for marker in ('id="cmdpop"', 'id="drawer"', 'id="dhead"', 'id="dbody"',
                       'id="tray"', 'id="trayBtn"', 'id="seats"', 'id="savePreset"'):
            self.assertIn(marker, body, "popover/drawer/tray markup missing: %s" % marker)
        for fn in ("function renderSessions(", "function rowHTML(",
                   "async function runContent(", "async function openSession(",
                   "async function loadEarlier(", "async function showPop(",
                   "function renderResumeAs("):
            self.assertIn(fn, body, "sessions view JS missing: %s" % fn)

    def test_existing_endpoints_still_work(self):
        status, d = self.req("/api/registry")
        self.assertEqual(status, 200)
        self.assertIn("alpha", d.get("projects", {}))
        status, d = self.req("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("provider", d)


if __name__ == "__main__":
    unittest.main()
