"""helm relevanced — the local relevance scorer service (stdlib only).

One resident process per scoring host. It holds a logistic HEAD fitted on the
outside evaluator's labels, and an embedding server (llama.cpp's
`llama-server --embedding`) that turns text into vectors. For each turn:

  1. embed the turn ONCE, as a query: the head's instruction prefix, then the
     turn cut to the head's `trunc` characters (70% head, 30% tail);
  2. embed each candidate row once PER CONTENT HASH: a row whose text has not
     changed is never embedded again (in memory, and in an append-only file
     under the cache dir that the next start reloads, one file per
     embedder, so a new head over the same embedder reuses every vector);
  3. score each row with the head, a plain dot product:
     p = sigmoid(w . [q*d, |q-d|, q.d] + b), q and d unit vectors.

    POST /score  {"turn": str, "rows": [{"id": str, "text": str}]}
              -> {"p": {id: prob}, "model": head version, "threshold": t,
                  "ms": {"embed": .., "head": ..}, "embedded": n}
    POST /embed  {"rows": [{"text": str}]}     warm the row cache
              -> {"embedded": n, "cached": m}
    GET  /health -> {"status": "ok", "model": .., "rows": n, "threshold": t}

The service is for the LAN only and carries no authentication: bind it to an
address the LAN reaches and nothing else. It stores no turn text; the row
cache holds vectors keyed by the hash of the row's text.

`helm relevance serve` runs it. With `--llama-server BIN --model GGUF` it
starts the embedding server itself on a loopback port and exits when that
child dies, so one unit supervises both.
"""
import array
import base64
import hashlib
import http.server
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

MAX_BODY = 1024 * 1024
MAX_ROWS = 64
#: WHAT ONE INPUT MAY COST THE EMBEDDER. A 0.6B embedder costs about 1.2 GFLOP
#: per input token on any CPU, so the service bounds every input it embeds, and
#: it does so here rather than trusting each client to. A ROW is cut to
#: MAX_ROW_CHARS: the head's labelled rows never exceeded 408 characters (the
#: lane renders them at 200, a glossed line at 400), so the cap changes nothing
#: the head was fitted on. A TURN is cut to the head's own `trunc` (the form
#: its labels had), or to MAX_TURN_CHARS for a head that declares none.
MAX_ROW_CHARS = 512
MAX_TURN_CHARS = 1000
#: THE LONGEST `trunc` A HEAD MAY DECLARE. A turn reaches the service already
#: in its labelled form (relevance.label_form): past 3,000 characters, its
#: first 2,000 and last 900. The head keeps 70% of `trunc` from the front and
#: 30% from the back, so it sees exactly the bytes it would have seen in the
#: uncapped turn only while 0.7 x trunc <= 2,000 and 0.3 x trunc <= 900.
MAX_HEAD_TRUNC = 2857


class Head:
    """A frozen head file: weights folded over the standardisation, so one
    dot product per row is the whole model."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        self.dim = int(raw["dim"])
        self.w = [float(x) for x in raw["w"]]
        if len(self.w) != 2 * self.dim + 1:
            raise ValueError("head %s: %d weights for dim %d (want %d)"
                             % (path, len(self.w), self.dim, 2 * self.dim + 1))
        self.b = float(raw["b"])
        self.version = str(raw["version"])
        self.threshold = raw.get("threshold")
        self.trunc = int(raw.get("trunc") or 0)
        if self.trunc > MAX_HEAD_TRUNC:
            raise ValueError("head %s: trunc %d is past %d, so its cut of a labelled "
                             "turn would differ from its cut of the turn it was fitted on"
                             % (path, self.trunc, MAX_HEAD_TRUNC))
        self.prefix = "Instruct: %s\nQuery: " % raw["instruction"]
        self.embedder = "%s-%s" % (raw.get("embedder") or "embedder",
                                   raw.get("pooling") or "pool")

    def query_text(self, turn):
        t, cap = turn or "", self.trunc or MAX_TURN_CHARS
        if len(t) > cap:
            t = t[:int(cap * .7)] + "\n[...]\n" + t[-int(cap * .3):]
        return self.prefix + t

    def score(self, q, d):
        w, n = self.w, self.dim
        s, dot = self.b, 0.0
        for i in range(n):
            qi, di = q[i], d[i]
            prod = qi * di
            dot += prod
            s += w[i] * prod + w[n + i] * abs(qi - di)
        s += w[2 * n] * dot
        if s >= 0:
            return 1.0 / (1.0 + math.exp(-s))
        e = math.exp(s)
        return e / (1.0 + e)


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    """The OpenAI-shaped /v1/embeddings client, one request per batch."""

    def __init__(self, url, timeout=60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), 16):
            body = json.dumps({"input": texts[i:i + 16]}).encode("utf-8")
            req = urllib.request.Request(self.url + "/v1/embeddings", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))["data"]
            out += [_unit(x["embedding"]) for x in sorted(data, key=lambda x: x["index"])]
        return out


class RowCache:
    """Row vectors by content hash, in memory and in one append-only file per
    embedder (`rows-<embedder>.b64l`, one `hash<TAB>base64 float32` line
    each). A torn last line is skipped on load, never fatal."""

    def __init__(self, cache_dir, embedder):
        tag = "".join(c if c.isalnum() or c in "._-" else "_" for c in embedder)
        self.path = os.path.join(cache_dir, "rows-%s.b64l" % tag) if cache_dir else None
        self.rows = {}
        self.lock = threading.Lock()
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="ascii", errors="replace") as f:
                for ln in f:
                    h, _, b = ln.strip().partition("\t")
                    try:
                        a = array.array("f")
                        a.frombytes(base64.b64decode(b))
                        self.rows[h] = list(a)
                    except (ValueError, TypeError):
                        continue

    def get(self, h):
        return self.rows.get(h)

    def put(self, h, vec):
        with self.lock:
            if h in self.rows:
                return
            self.rows[h] = vec
            if self.path:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="ascii") as f:
                    f.write("%s\t%s\n" % (h, base64.b64encode(
                        array.array("f", vec).tobytes()).decode("ascii")))


def row_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class Scorer:
    def __init__(self, head, embedder, cache):
        self.head, self.embedder, self.cache = head, embedder, cache

    def vectors(self, texts, query=None):
        """(row vectors, fresh count[, query vector]) for `texts`, each cut to
        MAX_ROW_CHARS, embedding only the hashes not cached; a `query` rides
        in the same request."""
        texts = [t[:MAX_ROW_CHARS] for t in texts]
        hashes = [row_hash(t) for t in texts]
        miss = {}
        for h, t in zip(hashes, texts):
            if self.cache.get(h) is None and h not in miss:
                miss[h] = t
        head = [query] if query is not None else []
        got = self.embedder.embed(head + list(miss.values())) if (head or miss) else []
        for h, v in zip(miss, got[len(head):]):
            self.cache.put(h, v)
        vecs = [self.cache.get(h) for h in hashes]
        return (vecs, len(miss), got[0]) if head else (vecs, len(miss))

    def score(self, turn, rows):
        t0 = time.perf_counter()
        vecs, fresh, q = self.vectors([r["text"] for r in rows],
                                      query=self.head.query_text(turn))
        t1 = time.perf_counter()
        p = {r["id"]: round(self.head.score(q, d), 6) for r, d in zip(rows, vecs)}
        t2 = time.perf_counter()
        return {"p": p, "model": self.head.version, "threshold": self.head.threshold,
                "embedded": fresh,
                "ms": {"embed": round((t1 - t0) * 1000, 1),
                       "head": round((t2 - t1) * 1000, 1)}}


def _rows(body, need_id=True):
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise ValueError("rows must be a list of 1..%d objects" % MAX_ROWS)
    out = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("text"), str) \
                or (need_id and not isinstance(r.get("id"), str)):
            raise ValueError("each row needs a string text%s" % (" and id" if need_id else ""))
        out.append(r)
    return out


def make_handler(scorer):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"status": "ok", "model": scorer.head.version,
                             "threshold": scorer.head.threshold,
                             "rows": len(scorer.cache.rows)})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_BODY:
                    return self._send(413, {"error": "body must be 1..%d bytes" % MAX_BODY})
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                if self.path == "/score":
                    if not isinstance(body.get("turn"), str):
                        raise ValueError("turn must be a string")
                    return self._send(200, scorer.score(body["turn"], _rows(body)))
                if self.path == "/embed":
                    rows = _rows(body, need_id=False)
                    before = len(scorer.cache.rows)
                    scorer.vectors([r["text"] for r in rows])
                    return self._send(200, {"embedded": len(scorer.cache.rows) - before,
                                            "cached": len(scorer.cache.rows)})
                return self._send(404, {"error": "not found"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)[:300]})
            except Exception as exc:        # noqa: BLE001 — a scorer fault is a 502, never a hang
                return self._send(502, {"error": "%s: %s" % (exc.__class__.__name__, str(exc)[:300])})
    return Handler


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_llama(binary, model, threads, parallel, log):
    """Start `llama-server --embedding` on a loopback port; (proc, url) once
    its /health answers ok. The library dir is the binary's own dir."""
    port = _free_port()
    d = os.path.dirname(os.path.abspath(binary))
    env = dict(os.environ, LD_LIBRARY_PATH=d, GGML_BACKEND_DIR=d)
    ctx = 2048 * max(1, parallel)
    argv = [binary, "-m", model, "--embedding", "--pooling", "last",
            "-t", str(threads), "-tb", str(threads), "-c", str(ctx),
            "-ub", "2048", "-b", "2048", "-ngl", "0", "-np", str(parallel),
            "--port", str(port), "--host", "127.0.0.1", "--no-webui"]
    proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env)
    url = "http://127.0.0.1:%d" % port
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("llama-server exited with %s" % proc.returncode)
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if json.loads(r.read().decode("utf-8")).get("status") == "ok":
                    return proc, url
        except Exception:                   # noqa: BLE001 — still loading
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("llama-server did not answer /health within 120 s")


USAGE = ("usage: helm relevance serve --head HEAD.json --port N [--host ADDR] "
         "(--embed-url URL | --llama-server BIN --model GGUF [--threads N] "
         "[--parallel N]) [--cache-dir DIR]")


def serve(args):
    """`helm relevance serve ...` — run the service in the foreground."""
    opts, i = {}, 0
    valued = ("--head", "--port", "--host", "--embed-url", "--llama-server",
              "--model", "--threads", "--parallel", "--cache-dir")
    while i < len(args):
        if args[i] in ("-h", "--help"):
            print(USAGE)
            return 0
        if args[i] not in valued or i + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        opts[args[i]] = args[i + 1]
        i += 2
    if "--head" not in opts or "--port" not in opts or \
            ("--embed-url" in opts) == ("--llama-server" in opts):
        print(USAGE, file=sys.stderr)
        return 2
    head = Head(opts["--head"])
    child = None
    if "--llama-server" in opts:
        if "--model" not in opts:
            print(USAGE, file=sys.stderr)
            return 2
        child, url = start_llama(opts["--llama-server"], opts["--model"],
                                 int(opts.get("--threads", "8")),
                                 int(opts.get("--parallel", "2")), sys.stderr)
    else:
        url = opts["--embed-url"]
    cache_dir = opts.get("--cache-dir") or os.path.join(
        os.path.expanduser("~"), ".cache", "helm", "relevance-rows")
    scorer = Scorer(head, Embedder(url), RowCache(cache_dir, head.embedder))
    server = http.server.ThreadingHTTPServer(
        (opts.get("--host", "127.0.0.1"), int(opts["--port"])), make_handler(scorer))
    server.daemon_threads = True
    if child is not None:
        def watch():
            child.wait()
            print("helm relevance serve: the embedding server exited (%s); "
                  "stopping" % child.returncode, file=sys.stderr)
            server.shutdown()
        threading.Thread(target=watch, daemon=True).start()
    print("helm relevance serve: head %s, %d cached rows, on %s:%s"
          % (head.version, len(scorer.cache.rows), opts.get("--host", "127.0.0.1"),
             opts["--port"]), file=sys.stderr, flush=True)

    # A STOP SIGNAL LEAVES NO EMBEDDING SERVER BEHIND. SIGTERM (a unit stop)
    # would end this process without the cleanup below, and SIGINT arrives
    # ignored when a non-interactive shell started us in the background, so
    # both become an ordinary exit that runs it.
    def stop(signum, frame):
        raise SystemExit(0)
    import signal
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
    return 1 if child is not None and child.returncode not in (None, 0, -15) else 0
