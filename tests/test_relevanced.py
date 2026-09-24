"""The local scorer service (helm/relevanced.py).

The head is one dot product and must equal the logistic model it was folded
from; a row is embedded once per content hash and never again until its text
changes, across a restart too; the HTTP surface answers /health, /score and
/embed and refuses a malformed body instead of hanging. The embedding server
is a double: these arms are about the service, not about llama.cpp.
"""
import http.server
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

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import relevanced  # noqa: E402

DIM = 3


class FakeEmbedder:
    """Deterministic unit vectors from the text; counts every text embedded."""

    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        out = []
        for t in texts:
            v = [float(len(t) % 7 + 1), float(sum(map(ord, t)) % 5 + 1), 1.0]
            out.append(relevanced._unit(v))
        return out


def write_head(tmp, w=None, b=-0.2, trunc=0):
    w = w or [0.5, -0.25, 1.0, 0.1, 0.2, -0.3, 2.0]
    path = os.path.join(tmp, "head.json")
    with open(path, "w") as f:
        json.dump({"dim": DIM, "w": w, "b": b, "version": "test-head-1",
                   "threshold": 0.4, "trunc": trunc, "instruction": "judge it",
                   "embedder": "fake", "pooling": "last"}, f)
    return path


class HeadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-relevanced-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_score_is_the_logistic_model_over_the_three_feature_blocks(self):  # noqa: VACUOUS_ASSERTION — assertAlmostEqual pins the score to an independently computed value; there is no absence here
        head = relevanced.Head(write_head(self.tmp))
        q, d = relevanced._unit([1.0, 2.0, 2.0]), relevanced._unit([2.0, 1.0, 0.5])
        feats = [a * b for a, b in zip(q, d)] + [abs(a - b) for a, b in zip(q, d)] \
            + [sum(a * b for a, b in zip(q, d))]
        z = sum(w * x for w, x in zip(head.w, feats)) + head.b
        self.assertAlmostEqual(head.score(q, d), 1 / (1 + math.exp(-z)), places=12)

    def test_a_head_with_the_wrong_weight_count_is_refused(self):
        with self.assertRaises(ValueError):
            relevanced.Head(write_head(self.tmp, w=[1.0, 2.0]))

    def test_a_head_whose_trunc_outreaches_the_label_cut_is_refused(self):  # noqa: VACUOUS_ASSERTION — the 2857 head is asserted to load and the equality at 2857 is the control for the inequality at 2900
        """0.7 x 2857 = 1999.9 and 0.3 x 2857 = 857.1 sit inside the labelled
        form's first 2,000 and last 900; one more character does not."""
        self.assertEqual(relevanced.Head(write_head(self.tmp, trunc=2857)).trunc, 2857)
        with self.assertRaises(ValueError) as ctx:
            relevanced.Head(write_head(self.tmp, trunc=2858))
        self.assertIn("trunc 2858", str(ctx.exception))
        cut = lambda t, n: t if len(t) <= n else t[:int(n * .7)] + "\n[...]\n" + t[-int(n * .3):]
        label = lambda t: t if len(t) <= 3000 else t[:2000] + "\n[...]\n" + t[-900:]
        x = "".join(chr(97 + i % 26) for i in range(9000))
        self.assertEqual(cut(label(x), 2857), cut(x, 2857))
        self.assertNotEqual(cut(label(x), 2900), cut(x, 2900))

    def test_the_query_is_prefixed_and_cut_to_the_heads_trunc(self):
        head = relevanced.Head(write_head(self.tmp, trunc=10))
        self.assertEqual(head.query_text("short"), "Instruct: judge it\nQuery: short")
        cut = head.query_text("abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(cut, "Instruct: judge it\nQuery: abcdefg\n[...]\nxyz")


class RowCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-relevanced-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.head = relevanced.Head(write_head(self.tmp))

    def scorer(self):
        emb = FakeEmbedder()
        return relevanced.Scorer(self.head, emb, relevanced.RowCache(
            os.path.join(self.tmp, "rows"), self.head.embedder)), emb

    def test_a_row_is_embedded_once_per_content_hash(self):
        s, emb = self.scorer()
        rows = [{"id": "a", "text": "note a"}, {"id": "b", "text": "note b"}]
        first = s.score("turn one", rows)
        self.assertEqual((first["embedded"], len(emb.seen)), (2, 3))    # query + 2 rows
        second = s.score("turn two", rows)
        self.assertEqual((second["embedded"], len(emb.seen)), (0, 4))   # the query only
        third = s.score("turn two", [{"id": "a", "text": "note a, edited"}, rows[1]])
        self.assertEqual((third["embedded"], emb.seen[-1]), (1, "note a, edited"))
        self.assertEqual(sorted(first["p"]), ["a", "b"])
        self.assertEqual((first["model"], first["threshold"]), ("test-head-1", 0.4))

    def test_the_row_cache_survives_a_restart_and_skips_a_torn_line(self):
        s, _emb = self.scorer()
        s.score("turn", [{"id": "a", "text": "note a"}])
        with open(s.cache.path, "a") as f:
            f.write("deadbeef\tnot-base64!!")
        again, emb = self.scorer()
        self.assertEqual(len(again.cache.rows), 1)
        again.score("turn", [{"id": "a", "text": "note a"}])
        self.assertEqual(emb.seen, ["Instruct: judge it\nQuery: turn"])


class InputCapTest(unittest.TestCase):
    """Every input the service embeds is bounded, whatever the client sent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-relevanced-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_row_is_cut_to_the_row_cap_and_hashed_as_cut(self):
        head = relevanced.Head(write_head(self.tmp, trunc=1000))
        emb = FakeEmbedder()
        s = relevanced.Scorer(head, emb, relevanced.RowCache(None, "x"))
        row = "r" * 5000
        s.score("turn", [{"id": "a", "text": row}])
        self.assertEqual([len(t) for t in emb.seen[1:]], [relevanced.MAX_ROW_CHARS])
        s.score("turn", [{"id": "a", "text": row[:relevanced.MAX_ROW_CHARS] + "tail"}])
        self.assertEqual(len(emb.seen), 3)              # same cut row: cached, only the query
        self.assertEqual(relevanced.MAX_ROW_CHARS, 512)

    def test_a_turn_is_cut_to_the_heads_trunc_or_the_turn_cap(self):  # noqa: VACUOUS_ASSERTION — each case is an equality on the cut query's exact length
        prefix = len("Instruct: judge it\nQuery: ")
        for trunc, want in ((1000, 1000), (0, relevanced.MAX_TURN_CHARS), (600, 600)):
            with self.subTest(trunc=trunc):
                head = relevanced.Head(write_head(self.tmp, trunc=trunc))
                q = head.query_text("x" * 9000)
                self.assertEqual(len(q) - prefix, int(want * .7) + len("\n[...]\n") + int(want * .3))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-relevanced-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        head = relevanced.Head(write_head(self.tmp))
        self.scorer = relevanced.Scorer(head, FakeEmbedder(), relevanced.RowCache(None, "x"))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), relevanced.make_handler(self.scorer))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.url = "http://127.0.0.1:%d" % srv.server_address[1]

    def call(self, path, body=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_score_and_embed(self):
        self.assertEqual(self.call("/health"), (200, {"status": "ok", "model": "test-head-1",
                                                      "threshold": 0.4, "rows": 0}))
        code, body = self.call("/score", {"turn": "t", "rows": [{"id": "x", "text": "n"}]})
        self.assertEqual((code, sorted(body["p"]), body["embedded"]), (200, ["x"], 1))
        self.assertTrue(0.0 < body["p"]["x"] < 1.0)
        code, body = self.call("/embed", {"rows": [{"text": "n"}, {"text": "m"}]})
        self.assertEqual((code, body), (200, {"embedded": 1, "cached": 2}))

    def test_a_malformed_body_is_a_4xx_never_a_hang(self):  # noqa: VACUOUS_ASSERTION — the unconditional /health read after the loop proves the same server still answers 200
        for path, body, code in (("/score", {"turn": 3, "rows": []}, 400),
                                 ("/score", {"turn": "t", "rows": [{"text": "n"}]}, 400),
                                 ("/score", b"{nope", 400), ("/nowhere", {"x": 1}, 404),
                                 ("/embed", {"rows": [{"text": "n"}] * 65}, 400)):
            with self.subTest(path=path, body=str(body)[:30]):
                got, answer = self.call(path, body)
                self.assertEqual(got, code)
                self.assertIn("error", answer)
        self.assertEqual(self.call("/health")[0], 200)                     # still serving


FAKE_LLAMA = r"""#!/usr/bin/env python3
import http.server, json, os, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
with open(os.environ["FAKE_LLAMA_PIDFILE"], "w") as f:
    f.write(str(os.getpid()))
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        return
    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self._send({"status": "ok"})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send({"data": [{"index": i, "embedding": [1.0, float(len(t) % 5), 2.0]}
                             for i, t in enumerate(body["input"])]})
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


class ServiceLifecycleTest(unittest.TestCase):
    """The unit's stop is SIGTERM: the service must answer, then leave no
    embedding server running behind it."""

    def test_a_stopped_service_leaves_no_embedding_server_behind(self):  # noqa: VACUOUS_ASSERTION — os.kill(child, 0) succeeds on the same pid before the stop (the control), and /health is asserted to name the head
        import signal
        import subprocess
        import time
        tmp = tempfile.mkdtemp(prefix="helm-test-relevanced-")
        self.addCleanup(shutil.rmtree, tmp, True)
        llama = os.path.join(tmp, "llama-server")
        with open(llama, "w") as f:
            f.write(FAKE_LLAMA)
        os.chmod(llama, 0o755)
        pidfile = os.path.join(tmp, "llama.pid")
        port = relevanced._free_port()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, FAKE_LLAMA_PIDFILE=pidfile,
                   PYTHONPATH=os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH")) if p))
        svc = subprocess.Popen([sys.executable, "-m", "helm", "relevance", "serve",
                                "--head", write_head(tmp), "--port", str(port),
                                "--llama-server", llama, "--model", "unused.gguf",
                                "--threads", "1", "--parallel", "1",
                                "--cache-dir", os.path.join(tmp, "rows")],
                               cwd=root, env=env, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: svc.poll() is None and svc.kill())
        deadline, health = time.time() + 60, None
        while time.time() < deadline and health is None:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                    health = json.loads(r.read())
            except OSError:
                time.sleep(0.2)
        self.assertEqual((health or {}).get("model"), "test-head-1")
        with open(pidfile) as f:
            child = int(f.read())
        os.kill(child, 0)                                  # the control: it is running
        svc.send_signal(signal.SIGTERM)
        self.assertEqual(svc.wait(timeout=30), 0)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)


class ServeArgsTest(unittest.TestCase):
    def test_serve_refuses_an_incomplete_command_line(self):  # noqa: VACUOUS_ASSERTION — the unconditional --help control reads the same serve() and the same usage text with rc 0
        import contextlib
        import io
        for argv in ([], ["--head", "h.json"], ["--head", "h", "--port", "1"],
                     ["--head", "h", "--port", "1", "--embed-url", "u", "--llama-server", "b"],
                     ["--bogus", "1"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(relevanced.serve(argv), 2)
                self.assertIn("usage: helm relevance serve", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(relevanced.serve(["--help"]), 0)
        self.assertIn("usage: helm relevance serve", out.getvalue())


if __name__ == "__main__":
    unittest.main()
