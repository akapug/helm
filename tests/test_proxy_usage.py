#!/usr/bin/env python3
"""The sidecar meter — which seat spent a pooled proxy-family account.

The reader pops the fork's management usage queue on loopback and appends
what it popped to a helm ledger. The fixture server below speaks the fork's
own contract: `GET /v0/management/usage-queue?count=N` behind the
`X-Management-Key` header, popping the oldest N records, and each record's
keys are the Go struct tags of `queuedUsageDetail` embedding `requestDetail`
with `token_breakdown` from the fork's `TokenBreakdown` (accounting.go) —
copied from the producer, never invented. Hermetic: HELM_HOME is a tmp dir,
the server is a thread on an ephemeral loopback port, and the management
secret is a test string."""
import contextlib
import io
import json
import hashlib
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helm import eventledger, proxy_usage, seat, seat_paths

FAKE_SECRET = "test-management-secret-not-a-credential"


def go_record(model="gpt-5.6-sol", source="pool@example.test", inp=120_000,
              cache_read=100_000, out=900, reasoning=300, failed=False,
              stamp="2031-01-02T03:04:05.123456789Z", api_key="inbound-seat-token",
              auth_index="0123456789abcdef"):  # gitleaks:allow — a synthetic auth index
    """One usage-queue record, key for key the fork's JSON: requestDetail
    (timestamp, latency_ms, ttft_ms, source, auth_index, client_ip,
    x_forwarded_for, user_agent, tokens, failed, generate, fail,
    response_headers) + queuedUsageDetail (accounting_version,
    token_breakdown, provider, executor_type, model, alias, endpoint,
    auth_type, api_key, request_id, reasoning_effort, service_tier)."""
    return {
        "timestamp": stamp, "latency_ms": 4321, "ttft_ms": 800,
        "source": source, "auth_index": auth_index,
        "client_ip": "127.0.0.1", "x_forwarded_for": "", "user_agent": "claude-cli/1.0",
        "tokens": {"input_tokens": inp, "output_tokens": out,
                   "reasoning_tokens": reasoning, "cached_tokens": cache_read,
                   "cache_read_tokens": cache_read, "cache_read_tokens_present": True,
                   "cache_creation_tokens": 0, "total_tokens": inp + out},
        "failed": failed, "generate": True,
        "fail": {"status_code": 500 if failed else 200, "body": "upstream said no" if failed else ""},
        "response_headers": {"X-Upstream-Trace": ["abc"]},
        "accounting_version": 2,
        "token_breakdown": {
            "schema_version": 2, "quality": "complete", "total_tokens": inp + out,
            "input": {"total_tokens": inp, "uncached_tokens": inp - cache_read,
                      "cache_read_tokens": cache_read, "cache_write_tokens": 0},
            "output": {"total_tokens": out, "non_reasoning_tokens": out - reasoning,
                       "reasoning_tokens": reasoning},
            "unclassified_tokens": 0},
        "provider": "codex", "executor_type": "CodexExecutor",
        "model": model, "alias": "claude-sonnet-5", "endpoint": "/v1/messages",
        "auth_type": "oauth", "api_key": api_key, "request_id": "req-0001",
        "reasoning_effort": "medium", "service_tier": "auto"}


class ManagementHandler(BaseHTTPRequestHandler):
    """The fork's usage-queue route. `mode` selects the failure class."""
    queue = []
    seen_keys = []
    mode = "ok"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        cls = self.__class__
        if cls.mode == "404":
            self.send_response(404)
            self.end_headers()
            return
        cls.seen_keys.append(self.headers.get("X-Management-Key"))
        if self.headers.get("X-Management-Key") != FAKE_SECRET or cls.mode == "401":
            self._json(401, {"error": "invalid management key"})
            return
        if not self.path.startswith("/v0/management/usage-queue"):
            self.send_response(404)
            self.end_headers()
            return
        if cls.mode == "junk":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not json")
            return
        count = int(self.path.split("count=")[1]) if "count=" in self.path else 1
        page, cls.queue = cls.queue[:count], cls.queue[count:]
        self._json(200, page)

    def _json(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MeterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-proxy-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"HELM_HOME": os.path.join(self.tmp, "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        ManagementHandler.queue = []
        ManagementHandler.seen_keys = []
        ManagementHandler.mode = "ok"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ManagementHandler)
        self.port = self.server.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib default 0.5s) — once
        # per test; see helm/mcpd.serve_background for the poll trade.
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.pid = mock.patch.object(seat, "_running_pid", return_value=4242)
        self.pid.start()
        self.addCleanup(self.pid.stop)

    POOL = ("pool@example.test", "other@example.test")

    def sidecar(self, seat="codex-41", port=None, secret=FAKE_SECRET, config=True,
                pool=POOL, auth_dir=None):
        """One sidecar home: a config.yaml naming the fixture server's port
        and, unless `pool` is None, a top-level `auth-dir:` (the key the
        live config declares) holding one pool file per email in `pool`,
        shaped like the files helm pools (`type`, `email` and identity keys
        only, no token); `auth_dir` declares that path verbatim instead and
        writes no file, for an auth-dir the config names and the disk lacks
        or one spelled through a symlink."""
        d = os.path.join(self.tmp, seat)
        os.makedirs(d, exist_ok=True)
        if pool is not None and auth_dir is None:
            auth_dir = os.path.join(d, "auth")
            os.makedirs(auth_dir, exist_ok=True)
            for n, email in enumerate(pool):
                seat_paths._write_private(
                    os.path.join(auth_dir, "codex-%d.json" % n),
                    json.dumps({"type": "codex", "email": email, "account_id": "acct-%d" % n,
                                "disabled": False, "expired": False}))
        if config:
            text = ('host: "127.0.0.1"\nport: %d\nremote-management:\n  secret-key: ""\n'
                    % (self.port if port is None else port))
            if auth_dir is not None:
                text += "auth-dir: %s\n" % json.dumps(auth_dir)
            seat_paths._write_private(os.path.join(d, "config.yaml"), text)
        if secret is not None:
            seat_paths._write_private(os.path.join(d, "mgmt.token"), secret + "\n")
        return ("codex", seat, d)

    def pool(self, d):
        """(accounts, faults) off the fixture config through the SHIPPED
        pool reader."""
        return proxy_usage.pool_accounts(os.path.join(d, "config.yaml"))

    def index_of(self, d, email):
        """The shipped reader's `auth_index` for the pool file naming
        `email` — every record the fixture queues carries an index the
        reader computed, never one typed by hand."""
        accounts, _faults = self.pool(d)
        return next(i for i, e in accounts.items() if e == email)

    def raw_ledger(self):
        with open(proxy_usage.ledger_path()) as f:
            return f.read()

    def ledger(self):
        return eventledger.events(proxy_usage.ledger_path())

    def sources(self):
        return [e["source"] for e in self.ledger() if e["kind"] == "request"]

    def read_marker(self):
        return [e for e in self.ledger() if e["kind"] == "read"][-1]

    def test_a_read_pops_every_record_into_the_ledger_and_drops_the_credential(self):
        """Positive, through the shipped reader against the fork's route:
        three records over two pages land as three `request` events plus one
        `read` event; the producer's `token_breakdown`, `source`, `model`,
        `alias` and `request_id` are kept verbatim and `at` is the record's
        own timestamp. Controls: the header the server saw is the seat's
        secret (the read was authenticated, not open); the inbound
        `api_key`, `response_headers` and `fail.body` reach no ledger line;
        a second read finds the queue empty (the pop was destructive)."""
        family, seat, d = self.sidecar()
        a, b = self.index_of(d, "pool@example.test"), self.index_of(d, "other@example.test")
        ManagementHandler.queue = [go_record(auth_index=a), go_record(failed=True, auth_index=a),
                                   go_record(source="other@example.test", inp=10, cache_read=0,
                                             auth_index=b)]
        with mock.patch.object(proxy_usage, "POP_COUNT", 2):
            rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual([(r["status"], r["records"], r["persisted"], r["lost"],
                           r["marker"], r["pid"], r["port"]) for r in rows],
                         [(proxy_usage.READ, 3, 3, 0, True, 4242, self.port)])
        self.assertEqual(ManagementHandler.seen_keys, [FAKE_SECRET, FAKE_SECRET])
        events = self.ledger()
        self.assertEqual([e["kind"] for e in events], ["request"] * 3 + ["read"])
        first = events[0]
        self.assertEqual((first["v"], first["seat"], first["family"], first["pid"],
                          first["port"], first["ts"]),
                         (1, "codex-41", "codex", 4242, self.port, 1_700_000_000))
        self.assertEqual(first["token_breakdown"], go_record()["token_breakdown"])
        self.assertEqual((first["source"], first["model"], first["alias"],
                          first["request_id"], first["auth_index"]),
                         ("pool@example.test", "gpt-5.6-sol", "claude-sonnet-5",
                          "req-0001", a))
        self.assertAlmostEqual(first["at"], 1_925_089_445.123456, places=3)
        self.assertTrue(events[1]["failed"])
        self.assertEqual(events[2]["source"], "other@example.test")
        with open(proxy_usage.ledger_path()) as f:
            raw = f.read()
        for secret in ("inbound-seat-token", "api_key", "response_headers",
                       "X-Upstream-Trace", "upstream said no", FAKE_SECRET):
            self.assertNotIn(secret, raw)
        self.assertEqual(stat.S_IMODE(os.stat(proxy_usage.ledger_path()).st_mode), 0o600)
        self.assertEqual(events[3]["status"], proxy_usage.READ)
        self.assertEqual(events[3]["records"], 3)
        again = proxy_usage.snapshot(seats=[self.sidecar()])
        self.assertEqual((again[0]["status"], again[0]["records"]), (proxy_usage.READ, 0))

    def test_a_pooled_account_is_kept_verbatim_through_the_pool_reader(self):
        """Positive control for every hashing arm below, through the shipped
        pool reader: the fixture config declares an auth-dir holding pool
        files, `pool_accounts` maps each file's index to its email with no
        fault, the index IS the producer's derivation (sha256 over the
        lowercased `type`, a colon, and the absolute path of the file under
        the auth-dir as the config spells it, first sixteen hex), and a
        record carrying file A's index with A's email lands verbatim under a
        READ row whose pool object says 2 admitted, no fault, 0 unresolved.
        Control inside the arm: renaming file A away retires its index (the
        map is read off the files, not assumed)."""
        family, seat, d = self.sidecar()
        accounts, faults = self.pool(d)
        self.assertEqual((sorted(accounts.values()), faults), (sorted(self.POOL), []))
        a = self.index_of(d, "pool@example.test")
        seed = "codex:" + os.path.abspath(os.path.join(d, "auth", "codex-0.json"))
        self.assertEqual(len(a), 16)               # sixteen hex, as the fork emits
        self.assertEqual(a, hashlib.sha256(seed.encode()).hexdigest()[:16])
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=a)]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["pool"]),
                         (proxy_usage.READ, 1, {"admitted": 2, "faults": [], "unresolved": 0}))
        self.assertEqual(self.sources(), ["pool@example.test"])
        self.assertEqual(self.read_marker()["pool"],
                         {"admitted": 2, "faults": [], "unresolved": 0})
        os.rename(os.path.join(d, "auth", "codex-0.json"),
                  os.path.join(d, "auth", "codex-0.json.retired"))
        accounts, faults = self.pool(d)
        self.assertEqual((list(accounts.values()), faults), (["other@example.test"], []))

    def test_a_pooled_email_under_another_auths_index_is_hashed(self):
        """THE COLLISION A MEMBERSHIP RULE ADMITS, the arm the rule this join
        replaces would have passed: membership in the pool is not
        provenance. A record whose
        `auth_index` is file B's (another auth spent it) or an index of no
        file at all, with a `source` equal to file A's email, is hashed —
        only the join of THIS record's index to the file carrying that
        email keeps a source. The raw ledger bytes carry the label and never
        A's email, and the pool object counts both as unresolved. Control:
        a record under B's index with B's email in the same read is kept
        verbatim, so the label turns on the join alone."""
        family, seat, d = self.sidecar()
        b = self.index_of(d, "other@example.test")
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=b),
                                   go_record(source="pool@example.test", auth_index="0123456789abcdef"),  # gitleaks:allow — a synthetic auth index
                                   go_record(source="other@example.test", auth_index=b)]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["pool"]["unresolved"]),
                         (proxy_usage.READ, 3, 2))
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual(self.sources(), [label, label, "other@example.test"])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold this read
        self.assertIn("other@example.test", raw)
        self.assertNotIn("pool@example.test", raw)

    def test_a_record_with_no_auth_index_is_hashed(self):
        """A record that carries no `auth_index` (the key absent, empty, or
        null) joins no pool file, so its source is hashed even when a pool
        file names that very email — and it is not counted unresolved: it
        never claimed a file. Control: the same email under its file's
        index in the same read is verbatim."""
        family, seat, d = self.sidecar()
        a = self.index_of(d, "pool@example.test")
        absent = go_record(source="pool@example.test")
        del absent["auth_index"]
        ManagementHandler.queue = [absent,
                                   go_record(source="pool@example.test", auth_index=""),
                                   go_record(source="pool@example.test", auth_index=None),
                                   go_record(source="other@example.test",
                                             auth_index=self.index_of(d, "other@example.test"))]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["pool"]["unresolved"]),
                         (proxy_usage.READ, 4, 0))
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual(self.sources(), [label, label, label, "other@example.test"])
        self.assertEqual(proxy_usage.account_label("pool@example.test",
                                                   {a: "pool@example.test"}, a),
                         "pool@example.test")
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold this read
        self.assertIn("other@example.test", raw)
        self.assertNotIn("pool@example.test", raw)

    def test_an_oauth_record_whose_source_is_not_a_pooled_account_is_hashed(self):
        """THE PRODUCER PAIRING AN AUTH-KIND RULE ADMITS: the fork
        classifies an auth as OAuth by its KIND, and an OAuth auth with no
        account email reports its api-key attribute or the inbound client
        key as `source` while `auth_type` stays "oauth" — so a record can
        carry `auth_type` oauth, a real pool file's index, and an
        email-shaped credential at once. Through the shipped reader, that
        record's source is a `cred:` label (the file under its index does
        not carry that email), it counts unresolved, and the raw ledger
        bytes never contain the credential; `auth_type` decides nothing.
        Control: the pooled email under the same index and `auth_type` in
        the same read is kept verbatim."""
        family, seat, d = self.sidecar()
        a = self.index_of(d, "pool@example.test")
        ManagementHandler.queue = [go_record(source="looks@like.example", auth_index=a),
                                   go_record(source="pool@example.test", auth_index=a)]
        self.assertEqual({r["auth_type"] for r in ManagementHandler.queue}, {"oauth"})
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["pool"]["unresolved"]), (proxy_usage.READ, 1))
        label = "cred:%s" % hashlib.sha256(b"looks@like.example").hexdigest()[:8]
        self.assertEqual(self.sources(), [label, "pool@example.test"])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold this read
        self.assertIn("pool@example.test", raw)
        self.assertNotIn("looks@like.example", raw)
        self.assertNotIn("like.example", raw)

    def test_an_api_key_source_is_labelled_never_persisted(self):
        """Positive on the leak measured at the first live read: for an
        api-key auth the fork's `source` is the key itself, under an index
        no pool file carries. The ledger line carries `cred:` plus the first
        eight hex of its sha256 and never the key; the rollup key stays
        stable across two records of the same key. Control: a pooled email
        under its file's index in the same read is kept verbatim. The bare
        poles of the one rule: verbatim only under the matching index, a
        different index or no pool hashes, an empty or absent source is
        returned as it came."""
        family, seat, d = self.sidecar()
        a, b = self.index_of(d, "pool@example.test"), self.index_of(d, "other@example.test")
        key = "sk-test-not-a-real-key-0123456789abcdef"
        ManagementHandler.queue = [dict(go_record(source=key), auth_type="api-key"),
                                   dict(go_record(source=key), auth_type="api-key"),
                                   go_record(source="pool@example.test", auth_index=a)]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual(rows[0]["status"], proxy_usage.READ)
        label = "cred:%s" % hashlib.sha256(key.encode()).hexdigest()[:8]
        self.assertEqual(self.sources(), [label, label, "pool@example.test"])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold this read
        self.assertIn("pool@example.test", raw)
        self.assertNotIn(key, raw)
        self.assertNotIn("sk-test", raw)
        accounts, _faults = self.pool(d)
        hashed = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual(proxy_usage.account_label("pool@example.test", accounts, a),
                         "pool@example.test")
        self.assertEqual(proxy_usage.account_label("pool@example.test", accounts, b), hashed)
        self.assertEqual(proxy_usage.account_label("pool@example.test", None, a), hashed)
        self.assertEqual(proxy_usage.account_label("pool@example.test"), hashed)
        self.assertEqual(proxy_usage.account_label("", accounts, a), "")
        self.assertIsNone(proxy_usage.account_label(None, accounts, a))

    def test_a_generated_key_leaves_no_window_of_its_bytes_in_the_ledger(self):
        """task/2945 guard: the ledger never holds an upstream key, whole or
        in part. A writer that copies `source` verbatim stores every api-key
        auth's upstream key on the line. The key here is generated per run from letters no hex digest, email or
        fixture uses. It rides the three ways a key reaches `source`: an
        api-key auth with no index, an OAuth fallback with no index, and a
        pooled file's index whose email is not the key. It also rides the
        inbound `api_key`. The raw ledger bytes hold no eight-byte window of
        the key, so a truncated or partly masked key is refused as well, and
        they hold its `cred:` label once per record."""
        import secrets
        family, seat_name, d = self.sidecar()
        a = self.index_of(d, "pool@example.test")
        body = "".join(secrets.choice("ghijklmnopqrstuvwxyzGHIJKLMNOPQRSTUVWXYZ")
                       for _ in range(64))
        key = "fake-" + body
        ManagementHandler.queue = [
            dict(go_record(source=key, api_key=key, auth_index=""), auth_type="api-key"),
            go_record(source=key, api_key=key, auth_index=""),
            go_record(source=key, api_key=key, auth_index=a),
        ]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat_name, d)])
        self.assertEqual((rows[0]["status"], rows[0]["persisted"]), (proxy_usage.READ, 3))
        raw = self.raw_ledger()
        leaked = [i for i in range(len(body) - 7) if body[i:i + 8] in raw]
        self.assertEqual(leaked, [], "the ledger holds %d window(s) of the key" % len(leaked))
        label = "cred:%s" % hashlib.sha256(key.encode()).hexdigest()[:8]
        self.assertEqual(self.sources(), [label, label, label])
        self.assertEqual(raw.count(label), 3)

    def test_an_unreadable_pool_file_is_a_named_fault_while_its_sibling_still_admits(self):
        """Beside the readable pool file for pool@example.test sit a
        DIRECTORY named x.json, a truncated file whose bytes carry the text
        broken@example.test but do not parse, a JSON list, an object with
        no `type` and one with no `email`. Each is a fault by basename and
        reason (never by content), the readable sibling's index is still
        admitted (a record under it is verbatim), a record naming the
        truncated file's email is hashed and its text reaches no ledger
        line, and the read status stays READ while the row and the read
        marker carry the pool object: 1 admitted, the five faults, 1
        unresolved. A fault narrows the set; it never fails the pass or
        opens it, and it is never silent."""
        family, seat, d = self.sidecar(pool=("pool@example.test",))
        auth = os.path.join(d, "auth")
        os.makedirs(os.path.join(auth, "x.json"))
        seat_paths._write_private(os.path.join(auth, "broken.json"),
                                  '{"type": "codex", "email": "broken@example.test", ')
        seat_paths._write_private(os.path.join(auth, "list.json"), "[]")
        seat_paths._write_private(os.path.join(auth, "notype.json"),
                                  '{"email": "notype@example.test"}')
        seat_paths._write_private(os.path.join(auth, "noemail.json"), '{"type": "codex"}')
        accounts, faults = self.pool(d)
        self.assertEqual(list(accounts.values()), ["pool@example.test"])
        self.assertEqual(faults, ["broken.json: not JSON", "list.json: not an object",
                                  "noemail.json: no email", "notype.json: no type",
                                  "x.json: unreadable (EISDIR)"])
        a = self.index_of(d, "pool@example.test")
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=a),
                                   go_record(source="broken@example.test")]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        pool = {"admitted": 1, "faults": faults, "unresolved": 1}
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["pool"]),
                         (proxy_usage.READ, 2, pool))
        self.assertEqual(self.read_marker()["pool"], pool)
        self.assertEqual(self.read_marker()["status"], proxy_usage.READ)
        label = "cred:%s" % hashlib.sha256(b"broken@example.test").hexdigest()[:8]
        self.assertEqual(self.sources(), ["pool@example.test", label])
        raw = self.raw_ledger()
        self.assertIn("pool@example.test", raw)   # the raw bytes hold this read
        self.assertIn(label, raw)
        self.assertIn("x.json: unreadable (EISDIR)", raw)
        self.assertNotIn("broken@example.test", raw)
        self.assertNotIn("notype@example.test", raw)

    def test_no_declared_or_absent_auth_dir_hashes_every_source_and_prints_the_trailer(self):
        """Fail safe, never open, never silent: a config that declares no
        auth-dir, and one whose declared auth-dir is absent on disk, each
        admit no file, so the very email the positive arm keeps verbatim is
        hashed under both and reaches no ledger line; the read stays READ
        with its records counted and exit 0 (a pool fault never moves the
        exit code), the read marker's pool object names the fault, and the
        real verb's printer puts a `pool:` trailer under the seat line
        naming it with the unresolved count. Control: the positive
        fixture's read prints no trailer at all."""
        nowhere = os.path.join(self.tmp, "nowhere")
        cases = [(self.sidecar("codex-42", pool=None), "auth-dir undeclared"),
                 (self.sidecar("codex-43", auth_dir=nowhere), "auth-dir absent: " + nowhere)]
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        for sidecar, fault in cases:
            ManagementHandler.queue = [go_record(source="pool@example.test")]
            out = io.StringIO()
            with mock.patch.object(proxy_usage, "instances", return_value=[sidecar]), \
                    contextlib.redirect_stdout(out):
                rc = proxy_usage.cmd_proxy_usage([])
            self.assertEqual(rc, 0, fault)
            self.assertIn("%-12s codex  port" % sidecar[1], out.getvalue())
            self.assertIn("1 record(s)", out.getvalue())
            self.assertIn("      pool: 0 admitted, 1 fault (%s), 1 unresolved" % fault,
                          out.getvalue())
            self.assertEqual(self.read_marker()["pool"],
                             {"admitted": 0, "faults": [fault], "unresolved": 1})
            self.assertEqual(self.read_marker()["status"], proxy_usage.READ)
        self.assertEqual(self.sources(), [label, label])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold both reads
        self.assertNotIn("pool@example.test", raw)
        family, seat, d = self.sidecar()
        ManagementHandler.queue = [go_record(source="pool@example.test",
                                             auth_index=self.index_of(d, "pool@example.test"))]
        out = io.StringIO()
        with mock.patch.object(proxy_usage, "instances", return_value=[(family, seat, d)]), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage([])
        self.assertEqual(rc, 0)
        self.assertIn("1 record(s)", out.getvalue())
        self.assertNotIn("pool:", out.getvalue())

    def test_the_proxywatch_pass_persists_a_record_with_no_pool_as_cred(self):
        """The consumer path: proxywatch's meter pass over a sidecar whose
        config declares no auth-dir. The collected record PERSISTS — as a
        `cred:` label, since with no pool it joins nothing — the row and
        the read marker say READ with 1 persisted, and pool.faults names
        the undeclared auth-dir. Second leg, the seam the consumer's own
        arms double: read_seat replaced by the five-value read those arms
        return, no config at all; the record still persists through the
        pass, hashed, under a READ marker. A pass that raised inside the
        watch's guard would persist nothing and say so only on stderr,
        which is what these two legs refuse."""
        from helm import proxywatch
        sidecar = self.sidecar("codex-42", pool=None)
        ManagementHandler.queue = [go_record(source="pool@example.test")]
        with mock.patch.object(proxy_usage, "instances", return_value=[sidecar]):
            rows = proxywatch._proxy_usage_pass()
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["persisted"],
                          rows[0]["pool"]),
                         (proxy_usage.READ, 1, 1,
                          {"admitted": 0, "faults": ["auth-dir undeclared"], "unresolved": 1}))
        self.assertEqual(self.sources(), [label])
        self.assertEqual((self.read_marker()["status"], self.read_marker()["pool"]["faults"]),
                         (proxy_usage.READ, ["auth-dir undeclared"]))
        five = (proxy_usage.READ, None, [go_record(source="other@example.test")], 7, 1)
        with mock.patch.object(proxy_usage, "instances",
                               return_value=[("codex", "codex-43", os.path.join(self.tmp, "none"))]), \
                mock.patch.object(proxy_usage, "read_seat", return_value=five):
            rows = proxywatch._proxy_usage_pass()
        other = "cred:%s" % hashlib.sha256(b"other@example.test").hexdigest()[:8]
        self.assertEqual((rows[0]["status"], rows[0]["persisted"], rows[0]["pool"]["admitted"]),
                         (proxy_usage.READ, 1, 0))
        self.assertEqual(self.sources(), [label, other])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold both passes
        self.assertIn(other, raw)
        self.assertNotIn("pool@example.test", raw)
        self.assertNotIn("other@example.test", raw)

    def test_a_pool_file_the_decoder_refuses_is_a_named_fault_and_every_popped_record_persists(self):
        """A pool file that is syntactically JSON but nested deeper than
        the decoder's recursion limit raises RecursionError, which is not
        an OSError or a ValueError; it sits beside a healthy file. The
        healthy file admits (its record is verbatim), the deep one is a
        fault under its exception class, every popped record persists
        (the unknown one hashed), and the status is READ. Control: the
        same reader on the healthy file alone has no fault."""
        family, seat, d = self.sidecar(pool=("pool@example.test",))
        control, _faults = self.pool(d)
        self.assertEqual((list(control.values()), _faults), (["pool@example.test"], []))
        depth = 50_000
        seat_paths._write_private(os.path.join(d, "auth", "deep.json"),
                                  "[" * depth + "]" * depth)
        accounts, faults = self.pool(d)
        self.assertEqual((list(accounts.values()), faults),
                         (["pool@example.test"], ["deep.json: unreadable (RecursionError)"]))
        a = self.index_of(d, "pool@example.test")
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=a),
                                   go_record(source="stray@example.test")]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        label = "cred:%s" % hashlib.sha256(b"stray@example.test").hexdigest()[:8]
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["persisted"],
                          rows[0]["pool"]),
                         (proxy_usage.READ, 2, 2,
                          {"admitted": 1, "faults": ["deep.json: unreadable (RecursionError)"],
                           "unresolved": 1}))
        self.assertEqual(self.sources(), ["pool@example.test", label])
        raw = self.raw_ledger()
        self.assertIn("pool@example.test", raw)   # the raw bytes hold this read
        self.assertIn(label, raw)
        self.assertNotIn("stray@example.test", raw)

    def test_the_pool_is_read_before_the_queue_is_popped(self):
        """THE ORDERING, discriminated two ways. First the call order: the
        real pool reader and the real sidecar read, each wrapped to log its
        name, run as pool_accounts then read_seat, and the record persists
        (the wrappers change nothing). Then the consequence: a pool read
        that raises before the pop — an impossibility the total reader is
        bypassed to simulate — propagates out of snapshot and the fake
        endpoint's queue STILL HOLDS its record, no management call was
        made, and no ledger exists; the next pass reads it. Were the pop
        first, that raise would have taken the record with it."""
        family, seat, d = self.sidecar()
        calls = []
        real_pool, real_read = proxy_usage.pool_accounts, proxy_usage.read_seat

        def pool(config_path):
            calls.append("pool_accounts")
            return real_pool(config_path)

        def read(*args, **kwargs):
            calls.append("read_seat")
            return real_read(*args, **kwargs)
        ManagementHandler.queue = [go_record(source="pool@example.test",
                                             auth_index=self.index_of(d, "pool@example.test"))]
        with mock.patch.object(proxy_usage, "pool_accounts", pool), \
                mock.patch.object(proxy_usage, "read_seat", read):
            rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual(calls, ["pool_accounts", "read_seat"])
        self.assertEqual((rows[0]["status"], rows[0]["persisted"]), (proxy_usage.READ, 1))
        self.assertEqual(self.sources(), ["pool@example.test"])
        os.remove(proxy_usage.ledger_path())
        ManagementHandler.queue = [go_record(source="pool@example.test")]
        ManagementHandler.seen_keys = []
        with mock.patch.object(proxy_usage, "pool_accounts",
                               side_effect=RuntimeError("pool read exploded")):
            with self.assertRaises(RuntimeError):
                proxy_usage.snapshot(now=1_700_000_001, seats=[(family, seat, d)])
        self.assertEqual(len(ManagementHandler.queue), 1)     # not popped
        self.assertEqual(ManagementHandler.seen_keys, [])     # not even called
        self.assertFalse(os.path.exists(proxy_usage.ledger_path()))
        rows = proxy_usage.snapshot(now=1_700_000_002, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["records"]), (proxy_usage.READ, 1))

    def test_an_exception_after_the_pop_is_FAILED_PERSIST_with_the_popped_count(self):
        """THE POST-POP GUARD, mirroring the refused-append arms: two
        records pop and request_event raises. The pass is not a silent
        zero — the row is FAILED-PERSIST naming the exception and 2 popped
        records lost, a read marker with that status, records 2 and
        persisted 0 is on disk, the verb prints FAILED-PERSIST and exits
        1, and proxywatch's meter pass returns that row rather than the
        None a raise would have produced (None is what let the watch exit
        0 with nothing on any surface). Control: the same fixture with
        request_event intact is READ, 2 persisted, exit 0."""
        from helm import proxywatch
        boom = mock.patch.object(proxy_usage, "request_event",
                                 side_effect=RuntimeError("curate exploded"))
        ManagementHandler.queue = [go_record(), go_record()]
        with boom:
            rows = proxy_usage.snapshot(now=1_700_000_000, seats=[self.sidecar()])
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["persisted"],
                          rows[0]["lost"], rows[0]["marker"]),
                         (proxy_usage.FAILED_PERSIST, 2, 0, 2, True))
        self.assertIn("RuntimeError: curate exploded", rows[0]["reason"])
        self.assertIn("2 popped record(s) lost", rows[0]["reason"])
        marker = self.read_marker()
        self.assertEqual((marker["status"], marker["records"], marker["persisted"]),
                         (proxy_usage.FAILED_PERSIST, 2, 0))
        self.assertEqual([e["kind"] for e in self.ledger()], ["read"])
        ManagementHandler.queue = [go_record(), go_record()]
        out, err = io.StringIO(), io.StringIO()
        with boom, mock.patch.object(proxy_usage, "instances", return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = proxy_usage.cmd_proxy_usage([])
            ManagementHandler.queue = [go_record(), go_record()]   # the verb popped them
            rows = proxywatch._proxy_usage_pass()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED-PERSIST — 2 of 2 record(s) not persisted — reader raised after the pop",
                      out.getvalue())
        self.assertIsNotNone(rows)
        self.assertEqual(rows[0]["status"], proxy_usage.FAILED_PERSIST)
        self.assertNotIn("rung failed", err.getvalue())
        ManagementHandler.queue = [go_record(), go_record()]
        out = io.StringIO()
        with mock.patch.object(proxy_usage, "instances", return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage([])
        self.assertEqual(rc, 0)
        self.assertIn("2 record(s)", out.getvalue())
        self.assertEqual(sum(e["kind"] == "request" for e in self.ledger()), 2)

    def test_a_pool_file_removed_before_the_read_leaves_its_record_unresolved(self):
        """A record carrying the index of a pool file that is gone by the
        time the pass reads the pool (the account was unpooled between the
        request and the pop) is hashed and counted unresolved: the pool
        object says 1 admitted, no fault (a file that is not there is not
        an unreadable one), 1 unresolved, and the status stays READ.
        Control: the sibling file's record in the same read is verbatim."""
        family, seat, d = self.sidecar()
        a, b = self.index_of(d, "pool@example.test"), self.index_of(d, "other@example.test")
        os.remove(os.path.join(d, "auth", "codex-0.json"))
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=a),
                                   go_record(source="other@example.test", auth_index=b)]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["records"], rows[0]["pool"]),
                         (proxy_usage.READ, 2, {"admitted": 1, "faults": [], "unresolved": 1}))
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual(self.sources(), [label, "other@example.test"])
        raw = self.raw_ledger()
        self.assertIn(label, raw)                  # the raw bytes hold this read
        self.assertIn("other@example.test", raw)
        self.assertNotIn("pool@example.test", raw)

    def test_the_index_follows_the_auth_dir_as_the_config_spells_it_never_realpath(self):
        """The producer seeds the index with the path it LOADED — the
        declared auth-dir joined to the basename and made absolute, not
        resolved. One pool file reached through two spellings (the real
        directory and a symlink to it, each declared by its own config)
        yields two different indexes, the symlink one being sha256 over the
        symlink spelling; under the symlink config a record carrying the
        symlink-spelled index is verbatim and one carrying the
        realpath-derived index is hashed and unresolved — the fail-safe
        direction of a spelling mismatch."""
        family, seat, d = self.sidecar(pool=("pool@example.test",))
        link = os.path.join(self.tmp, "codex-41-auth-link")
        os.symlink(os.path.join(d, "auth"), link)
        family2, seat2, d2 = self.sidecar("codex-42", auth_dir=link)
        real, _f = self.pool(d)
        linked, faults = self.pool(d2)
        self.assertEqual((list(real.values()), list(linked.values()), faults),
                         (["pool@example.test"], ["pool@example.test"], []))
        real_idx, link_idx = next(iter(real)), next(iter(linked))
        self.assertEqual(len(real_idx), 16)        # sixteen hex, as the fork emits
        self.assertEqual(len(link_idx), 16)
        self.assertNotEqual(real_idx, link_idx)
        seed = "codex:" + os.path.join(link, "codex-0.json")
        self.assertEqual(link_idx, hashlib.sha256(seed.encode()).hexdigest()[:16])
        ManagementHandler.queue = [go_record(source="pool@example.test", auth_index=link_idx),
                                   go_record(source="pool@example.test", auth_index=real_idx)]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family2, seat2, d2)])
        self.assertEqual((rows[0]["status"], rows[0]["pool"]),
                         (proxy_usage.READ, {"admitted": 1, "faults": [], "unresolved": 1}))
        label = "cred:%s" % hashlib.sha256(b"pool@example.test").hexdigest()[:8]
        self.assertEqual(self.sources(), ["pool@example.test", label])
        raw = self.raw_ledger()
        self.assertIn("pool@example.test", raw)   # the raw bytes hold this read
        self.assertIn(label, raw)

    def test_a_type_with_surrounding_whitespace_yields_the_producers_index(self):
        """The producer strips then lowercases the file's `type` before
        seeding the index, so a pool file whose type is " Codex " (spaces,
        capitals) mints the same index as one spelled "codex" at the same
        path, and a record under that index with that file's email is kept
        verbatim, 0 unresolved — an index that only lowercased would have
        hashed every record that file spent (an attribution loss, not a
        leak). A whitespace-only type is a `no type` fault, never a guessed
        index. Positive control beside it: the plainly spelled sibling's
        record in the same read is verbatim."""
        family, seat, d = self.sidecar(pool=("pool@example.test",))
        auth = os.path.join(d, "auth")
        seat_paths._write_private(os.path.join(auth, "spaced.json"),
                                  json.dumps({"type": " Codex ", "email": "spaced@example.test"}))
        seat_paths._write_private(os.path.join(auth, "blank.json"),
                                  json.dumps({"type": "   ", "email": "blank@example.test"}))
        accounts, faults = self.pool(d)
        self.assertEqual((sorted(accounts.values()), faults),
                         (["pool@example.test", "spaced@example.test"], ["blank.json: no type"]))
        spaced = self.index_of(d, "spaced@example.test")
        self.assertEqual(len(spaced), 16)
        self.assertEqual(spaced, proxy_usage.pool_index("codex", os.path.join(auth, "spaced.json")))
        seed = "codex:" + os.path.abspath(os.path.join(auth, "spaced.json"))
        self.assertEqual(spaced, hashlib.sha256(seed.encode()).hexdigest()[:16])
        ManagementHandler.queue = [go_record(source="spaced@example.test", auth_index=spaced),
                                   go_record(source="pool@example.test",
                                             auth_index=self.index_of(d, "pool@example.test"))]
        rows = proxy_usage.snapshot(now=1_700_000_000, seats=[(family, seat, d)])
        self.assertEqual((rows[0]["status"], rows[0]["pool"]),
                         (proxy_usage.READ, {"admitted": 2, "faults": ["blank.json: no type"],
                                             "unresolved": 0}))
        self.assertEqual(self.sources(), ["spaced@example.test", "pool@example.test"])
        raw = self.raw_ledger()
        self.assertIn("spaced@example.test", raw)  # the raw bytes hold this read
        self.assertNotIn("blank@example.test", raw)

    def test_a_refused_key_is_UNREADABLE_and_never_a_zero(self):
        """Negative on an otherwise-valid sidecar: the server answers 401.
        The row and the ledger's read event say UNREADABLE with the HTTP
        class, no request event is minted, and the secret is in no reason.
        Control: the same sidecar with the right key reads (the positive arm
        above), so the status turns on the key alone."""
        ManagementHandler.queue = [go_record()]
        rows = proxy_usage.snapshot(seats=[self.sidecar(secret="wrong-key-for-the-arm")])
        self.assertEqual(rows[0]["status"], proxy_usage.UNREADABLE)
        self.assertIn("401", rows[0]["reason"])
        self.assertNotIn("wrong-key-for-the-arm", rows[0]["reason"])
        events = self.ledger()
        self.assertEqual([e["kind"] for e in events], ["read"])
        self.assertEqual((events[0]["status"], events[0]["records"]),
                         (proxy_usage.UNREADABLE, 0))
        self.assertIn("401", events[0]["reason"])
        with open(proxy_usage.ledger_path()) as f:
            self.assertNotIn("wrong-key-for-the-arm", f.read())

    def test_routes_not_enabled_names_the_respawn(self):
        """A sidecar spawned before the meter answers 404 on every management
        route; the reason says so and names the cure."""
        ManagementHandler.mode = "404"
        rows = proxy_usage.snapshot(seats=[self.sidecar()])
        self.assertEqual(rows[0]["status"], proxy_usage.UNREADABLE)
        self.assertIn("404", rows[0]["reason"])
        self.assertIn("respawn", rows[0]["reason"])

    def test_every_local_failure_is_a_reason(self):
        """No secret minted, a closed port, a config with no port, no config
        at all, and a non-JSON body: each is UNREADABLE with its own reason,
        and none is an empty READ. The control is the positive arm's READ on
        the same fixture shape with everything present."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        cases = [
            (self.sidecar("codex-42", secret=None), "no management secret"),
            (self.sidecar("codex-43", port=closed), "port %d closed" % closed),
            (self.sidecar("codex-44", config=False), "config unreadable"),
        ]
        d = os.path.join(self.tmp, "codex-45")
        os.makedirs(d)
        seat_paths._write_private(os.path.join(d, "config.yaml"), 'host: "127.0.0.1"\n')
        seat_paths._write_private(os.path.join(d, "mgmt.token"), FAKE_SECRET + "\n")
        cases.append((("codex", "codex-45", d), "names no port"))
        rows = proxy_usage.snapshot(seats=[c[0] for c in cases])
        for row, (_seat, expected) in zip(rows, cases):
            self.assertEqual(row["status"], proxy_usage.UNREADABLE, row)
            self.assertIn(expected, row["reason"])
            self.assertEqual(row["records"], 0)
        self.assertEqual([e["status"] for e in self.ledger()],
                         [proxy_usage.UNREADABLE] * 4)

    def test_a_non_json_body_is_unparseable_not_empty(self):
        ManagementHandler.mode = "junk"
        rows = proxy_usage.snapshot(seats=[self.sidecar()])
        self.assertEqual(rows[0]["status"], proxy_usage.UNREADABLE)
        self.assertIn("not JSON", rows[0]["reason"])

    def test_events_window_on_the_records_own_time_and_tokens_read_the_breakdown(self):
        """`events(since)` keeps a request whose proxy timestamp is inside
        the window and drops one outside it, whatever the helm read time
        was; the latest read per seat is always returned. `tokens_of`
        reads input/output totals off `token_breakdown` and counts nothing
        for a record without it."""
        ManagementHandler.queue = [go_record(stamp="2031-01-02T03:04:05Z"),
                                   go_record(stamp="2030-01-01T00:00:00Z")]
        proxy_usage.snapshot(now=1, seats=[self.sidecar()])
        requests, last_read, unknown = proxy_usage.events(since=1_925_000_000)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["timestamp"], "2031-01-02T03:04:05Z")
        self.assertEqual(last_read["codex-41"]["status"], proxy_usage.READ)
        self.assertIsNone(unknown)
        requests, _, _ = proxy_usage.events()
        self.assertEqual(len(requests), 2)
        self.assertEqual(proxy_usage.tokens_of(requests[0]), (120_000, 900))
        self.assertEqual(proxy_usage.tokens_of({"token_breakdown": None}), (0, 0))

    def _failing_append(self, fail_on):
        """A wrapper on the REAL writer the module calls
        (`eventledger.append_unlocked`): every call goes to disk except the
        `fail_on`-th (1-based), which returns False the way a refused write
        does. The count of calls is what makes each arm's injection point
        provable."""
        real = eventledger.append_unlocked
        calls = []

        def append(path, row):
            calls.append(row.get("kind"))
            if len(calls) == fail_on:
                return False
            return real(path, row)
        return mock.patch.object(eventledger, "append_unlocked", append), calls

    def test_a_refused_record_append_is_FAILED_PERSIST_with_the_lost_count(self):
        """F1, through the real verb entrypoint. Three records pop; the
        second append (a request event) is refused at the real writer. The
        row is FAILED-PERSIST (never READ), names 1 of 3 lost, the verb
        exits non-zero in text and --json, and the read marker on disk
        carries the same state with persisted 2. Control: the same fixture
        with every append landing is READ, exit 0, marker READ."""
        ManagementHandler.queue = [go_record(), go_record(), go_record()]
        out = io.StringIO()
        with mock.patch.object(proxy_usage, "instances", return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(proxy_usage.cmd_proxy_usage([]), 0)
        self.assertIn("3 record(s)", out.getvalue())
        marker = self.ledger()[-1]
        self.assertEqual((marker["kind"], marker["status"], marker["persisted"]),
                         ("read", proxy_usage.READ, 3))
        ManagementHandler.queue = [go_record(), go_record(), go_record()]
        patch, calls = self._failing_append(2)
        out = io.StringIO()
        with patch, mock.patch.object(proxy_usage, "instances",
                                      return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage([])
        self.assertEqual(calls, ["request", "request", "request", "read"])
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("FAILED-PERSIST — 1 of 3 record(s) not persisted", text)
        status = [l for l in text.splitlines() if "codex-41" in l][0].split("pid 4242")[1]
        self.assertTrue(status.strip().startswith("FAILED-PERSIST"), status)
        events = self.ledger()
        marker = events[-1]
        self.assertEqual((marker["kind"], marker["status"], marker["records"],
                          marker["persisted"]),
                         ("read", proxy_usage.FAILED_PERSIST, 3, 2))
        self.assertIn("refused 1 of 3", marker["reason"])
        self.assertEqual(sum(e["kind"] == "request" for e in events), 5)
        ManagementHandler.queue = [go_record()]
        patch, _calls = self._failing_append(1)
        out = io.StringIO()
        with patch, mock.patch.object(proxy_usage, "instances",
                                      return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(proxy_usage.cmd_proxy_usage(["--json"]), 1)
        row = json.loads(out.getvalue())["rows"][0]
        self.assertEqual((row["status"], row["records"], row["persisted"], row["lost"]),
                         (proxy_usage.FAILED_PERSIST, 1, 0, 1))

    def test_a_refused_read_marker_is_FAILED_PERSIST_and_no_marker_is_written(self):
        """F1, the marker leg: every record append lands and the read
        marker's append is refused. The row is FAILED-PERSIST with lost 0
        and marker False, the verb exits non-zero, and the ledger's last
        event is a request — no marker claims the pass succeeded, so the
        seat's last durable state stays whatever it was. Control: the
        positive arm above ends every pass with a READ marker."""
        ManagementHandler.queue = [go_record(), go_record()]
        patch, calls = self._failing_append(3)
        out = io.StringIO()
        with patch, mock.patch.object(proxy_usage, "instances",
                                      return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage(["--json"])
        self.assertEqual(calls, ["request", "request", "read"])
        self.assertEqual(rc, 1)
        row = json.loads(out.getvalue())["rows"][0]
        self.assertEqual((row["status"], row["records"], row["persisted"],
                          row["lost"], row["marker"]),
                         (proxy_usage.FAILED_PERSIST, 2, 2, 0, False))
        self.assertIn("read marker not written", row["reason"])
        # the phrase says what `lost` says: nothing but the marker was lost
        self.assertIn("read marker not persisted, 0 of 2 record(s) lost",
                      proxy_usage.row_note(row))
        self.assertNotIn("not persisted —", proxy_usage.row_note(row).replace(
            "read marker not persisted", ""))
        events = self.ledger()
        self.assertEqual([e["kind"] for e in events], ["request", "request"])
        _requests, last_read, unknown = proxy_usage.events()
        self.assertEqual(last_read, {})
        self.assertIsNone(unknown)
        # the text verb renders the same lost-0 phrase (run last: it pops again)
        ManagementHandler.queue = [go_record(), go_record()]
        patch, _calls = self._failing_append(3)
        out = io.StringIO()
        with patch, mock.patch.object(proxy_usage, "instances",
                                      return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(proxy_usage.cmd_proxy_usage([]), 1)
        self.assertIn("FAILED-PERSIST — read marker not persisted, 0 of 2 record(s) lost",
                      out.getvalue())

    def test_a_refused_ledger_lock_loses_every_record_and_says_so(self):
        """F1 at the lock boundary: `eventledger.locked` refuses, so nothing
        can be appended. Every popped record is lost, the marker is not
        written, the status is FAILED-PERSIST and the verb exits non-zero."""
        ManagementHandler.queue = [go_record(), go_record()]

        @contextlib.contextmanager
        def refused(_path, timeout=None):
            yield False
        out = io.StringIO()
        with mock.patch.object(eventledger, "locked", refused), \
                mock.patch.object(proxy_usage, "instances", return_value=[self.sidecar()]), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage([])
        self.assertEqual(rc, 1)
        self.assertIn("FAILED-PERSIST — 2 of 2 record(s) not persisted — ledger lock refused",
                      out.getvalue())
        self.assertFalse(os.path.exists(proxy_usage.ledger_path()))

    def test_events_keeps_an_unreadable_ledger_on_the_UNKNOWN_channel(self):
        """F2 at the reader, real files, no mock. (a) A directory where the
        ledger should be: no rows, `unknown` names the reader's reason. (b)
        One good record and one malformed complete line: the good record
        counts, `unknown` names the malformed row, uncounted. (c) An empty
        ledger: zero rows and `unknown` None — the control that says a
        clean zero is still a zero."""
        path = proxy_usage.ledger_path()
        os.makedirs(path)
        requests, last_read, unknown = proxy_usage.events()
        self.assertEqual((requests, last_read), ([], {}))
        self.assertIn("ledger unreadable", unknown)
        os.rmdir(path)
        good = proxy_usage.request_event("codex", "codex-41", 7, 1, go_record(), 5)
        self.assertTrue(eventledger.append(path, good))
        with open(path, "a") as f:
            f.write('{"this is": "not a row"\n')
        requests, _last_read, unknown = proxy_usage.events()
        self.assertEqual([r["id"] for r in requests], [good["id"]])
        self.assertIn("malformed ledger row(s) skipped, uncounted", unknown)
        self.assertIn("2", unknown)
        os.remove(path)
        open(path, "w").close()
        requests, last_read, unknown = proxy_usage.events()
        self.assertEqual((requests, last_read, unknown), ([], {}, None))

    def test_the_verb_prints_one_line_per_seat_and_exits_1_on_an_unreadable_one(self):
        ManagementHandler.queue = [go_record()]
        seats = [self.sidecar(), self.sidecar("codex-42", secret=None)]
        out = io.StringIO()
        with mock.patch.object(proxy_usage, "instances", return_value=seats), \
                contextlib.redirect_stdout(out):
            rc = proxy_usage.cmd_proxy_usage([])
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("codex-41", text)
        self.assertIn("1 record(s)", text)
        self.assertIn("codex-42", text)
        self.assertIn("UNREADABLE — no management secret", text)
        self.assertNotIn(FAKE_SECRET, text)
        out = io.StringIO()
        with mock.patch.object(proxy_usage, "instances", return_value=seats[:1]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(proxy_usage.cmd_proxy_usage(["--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["rows"][0]["status"], proxy_usage.READ)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(proxy_usage.cmd_proxy_usage(["--bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
