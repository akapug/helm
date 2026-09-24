#!/usr/bin/env python3
"""helm mcpd — the stateless MCP shell (spec rev 2026-07-28), wire-level.

Every test speaks real HTTP against an ephemeral server on an OS-assigned
port: the wire is the contract, so the tests exercise the wire. The CLI is
the diff oracle by owner ruling (card 9d3a61c5, his words: the two fronts
'can always be COMPARED AGAINST EACH OTHER … and against the
METHOD-AGNOSTIC SPEC') — test_the_two_front_ends_agree is that ruling as
an executable check."""
import ast
import http.client
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import mcpd, pk

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME")

# EVERY WAY PYTHON BINDS A NAME, AS DATA (the finding: "producer census
# misses AnnAssign"). The census below used to ask `isinstance(node,
# ast.Assign)`, so `seat: str = arguments["seat"]` — the same assignment with
# a type annotation on it — was invisible to the one probe whose entire job is
# to be exhaustive. An if-chain over node types will keep missing the next
# form; a table makes adding one a ROW, and makes the omission visible as a
# gap in a list rather than as an absent branch nobody reads.
#
# Each entry maps a node type to (targets, value): the names it binds, and the
# expression they are bound FROM — which is what the security property is
# actually about.
_SEAT_BINDERS = {
    ast.Assign:        lambda n: (n.targets, n.value),
    ast.AnnAssign:     lambda n: ([n.target], n.value),
    ast.NamedExpr:     lambda n: ([n.target], n.value),
    ast.For:           lambda n: ([n.target], n.iter),
    ast.AsyncFor:      lambda n: ([n.target], n.iter),
    ast.comprehension: lambda n: ([n.target], n.iter),
    ast.withitem:      lambda n: ([n.optional_vars], n.context_expr),
}


def _seat_producers(source):
    """Every expression that binds the name `seat`, unparsed.

    PARSED, NOT GREPPED. The first cut of this probe matched any line holding
    "seat" and "=", which also caught the pass-through `dispatch(body,
    seat=seat)` — a probe bound to the wrong claim."""
    out = []
    for node in ast.walk(ast.parse(source)):
        binder = _SEAT_BINDERS.get(type(node))
        if binder is None:
            continue
        targets, value = binder(node)
        # A bare `seat: str` annotation binds nothing at runtime, and a `with`
        # item without `as` has no target at all — neither is a producer.
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "seat":
                out.append(ast.unparse(value))
    return out


def _mint_in_child(seat, barrier, out):
    """One child of the concurrent-mint proof; module scope so a spawn-start
    platform can import it. Reports its own exception rather than dying
    silently — a child that vanishes would look exactly like a child that
    minted nothing, and the parent asserts on what it receives."""
    try:
        barrier.wait(timeout=30)
        tok, err = mcpd.mint(seat)
    except Exception as exc:
        out.put((seat, None, "child raised: %r" % (exc,)))
        return
    out.put((seat, tok, err))


class McpdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-mcpd-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "mcpd-test"
        # POLL_INTERVAL IS OPT-IN, NOT A DEFAULT: this fixture starts a
        # server per test and registers shutdown() as cleanup, so it pays
        # one poll interval per test. The stdlib default is 0.5, which
        # made test_mcpd 19.6s. Production and embedders keep that
        # default — defaulting it small here was a 100Hz idle loop in
        # every embedder, measured at ~44x idle CPU (codex, row
        # 37f557c04ffa). The cost belongs to the caller that wants it.
        self.httpd, self.thread = mcpd.serve_background(
            port=0, poll_interval=0.01)
        self.port = self.httpd.server_address[1]
        # shutdown() stops the serve loop but does NOT close the listening
        # socket — a focused run of many methods leaked one fd each until the
        # process ran out (codex). server_close() is the half that frees it,
        # and it must run AFTER shutdown, so it registers FIRST (cleanups run
        # last-registered-first).
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def tearDown(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, body, headers=None, conforming=True):
        """One POST. CONFORMING BY DEFAULT: the required mirrored headers are
        derived from the body, because a suite that sent none was codifying
        non-conforming client traffic and passing only because the server was
        lax about presence. Pass conforming=False to send exactly `headers`."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        raw = json.dumps(body)
        hdrs = {"Content-Type": "application/json"}
        if conforming and isinstance(body, dict) and body.get("id") is not None:
            params = body.get("params") if isinstance(
                body.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(
                params.get("_meta"), dict) else {}
            hdrs["MCP-Protocol-Version"] = meta.get(
                mcpd._META_VERSION, mcpd.PROTOCOL_VERSION)
            hdrs["Mcp-Method"] = body.get("method") or ""
            if body.get("method") == "tools/call" and params.get("name"):
                hdrs["Mcp-Name"] = params["name"]
        hdrs.update(headers or {})
        conn.request("POST", "/mcp", raw, hdrs)
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        out = json.loads(payload) if payload else None
        conn.close()
        return resp.status, out

    def call(self, method, params=None, headers=None, conforming=True):
        """A CONFORMING request by default — body metadata included.

        The helper used to omit params._meta entirely, so every test sent
        traffic the spec forbids and passed only because the server did not
        check. That is the SECOND time this fixture modelled a
        non-conforming client (the first was the missing mirrored headers),
        and both times the server's laxness is what hid it. Tests that MEAN
        to send a malformed body pass conforming=False.
        """
        params = dict(params or {})
        if conforming:
            meta = dict(params.get("_meta") or {})
            meta.setdefault(mcpd._META_VERSION, mcpd.PROTOCOL_VERSION)
            params["_meta"] = meta
        return self.post({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}, headers=headers,
                         conforming=conforming)


class WireTest(McpdBase):
    def test_discover_answers_without_any_handshake(self):
        status, out = self.call("server/discover")
        self.assertEqual(status, 200)
        got = out["result"]
        self.assertIn(mcpd.PROTOCOL_VERSION, got["supportedVersions"])
        self.assertEqual(got["resultType"], "complete")
        self.assertTrue(got["instructions"])
        self.assertIn("tools", got["capabilities"])
        # caching hints are a MUST on every complete result from discover too
        self.assertEqual(got["ttlMs"], mcpd.DISCOVER_TTL_MS)
        self.assertEqual(got["cacheScope"], "public")

    def test_tools_list_is_cacheable_and_deterministic(self):
        status, one = self.call("tools/list")
        self.assertEqual(status, 200)
        got = one["result"]
        self.assertEqual(got["ttlMs"], mcpd.LIST_TTL_MS)
        self.assertEqual(got["cacheScope"], "private")
        names = [t["name"] for t in got["tools"]]
        self.assertIn("store_resolve", names)
        _s, two = self.call("tools/list")
        self.assertEqual(names, [t["name"] for t in two["result"]["tools"]])

    def test_tools_call_round_trips_a_real_resolve(self):
        status, out = self.call("tools/call", {
            "name": "store_resolve",
            "arguments": {"text": "does anything fire for this sentence"}})
        self.assertEqual(status, 200)
        got = out["result"]
        self.assertEqual(got["resultType"], "complete")
        self.assertNotIn("isError", got)
        self.assertTrue(got["content"][0]["text"])
        self.assertIn("fired", got["structuredContent"])

    def test_a_tool_argument_error_is_execution_tier_not_protocol(self):
        status, out = self.call("tools/call",
                                {"name": "store_resolve", "arguments": {}})
        self.assertEqual(status, 200)      # the PROTOCOL succeeded
        got = out["result"]
        self.assertTrue(got["isError"])    # the EXECUTION carries the error
        self.assertIn("required", got["content"][0]["text"])

    def test_unknown_tool_is_invalid_params_and_unknown_method_is_32601(self):
        """The two are DIFFERENT cases and the spec codes them apart: an
        unknown TOOL is -32602 ('Unknown tool: ...' is the spec's own example),
        while 404/-32601 is reserved for an unimplemented METHOD — a meaning
        that doubles as the legacy-server fallback signal, so conflating them
        sends a modern client hunting for a 2024-era endpoint."""
        status, out = self.call("tools/call", {"name": "no_such_tool"},
                                headers={"Mcp-Name": "no_such_tool"})
        self.assertEqual((status, out["error"]["code"]), (200, mcpd.E_INVALID))
        self.assertIn("Unknown tool", out["error"]["message"])
        status, out = self.call("no/such_method")
        self.assertEqual((status, out["error"]["code"]), (404, mcpd.E_METHOD))

    def test_unsupported_version_refuses_with_the_supported_list(self):
        status, out = self.call("server/discover", {
            "_meta": {mcpd._META_VERSION: "2024-11-05"}})
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_UNSUPPORTED_VERSION)
        self.assertEqual(out["error"]["data"]["supported"],
                         [mcpd.PROTOCOL_VERSION])

    def test_mirrored_header_mismatch_refuses_with_32020(self):
        status, out = self.call("tools/list",
                                headers={"Mcp-Method": "tools/call"})
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_HEADER_MISMATCH)
        # MUST-HIT control: the SAME header, agreeing, passes
        status, _out = self.call("tools/list",
                                 headers={"Mcp-Method": "tools/list"})
        self.assertEqual(status, 200)

    def test_foreign_origin_refuses_and_localhost_passes(self):
        status, out = self.call("tools/list",
                                headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _out = self.call("tools/list",
                                 headers={"Origin": "http://localhost:7461"})
        self.assertEqual(status, 200)

    def test_get_is_refused_the_side_channel_is_gone(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 405)
        resp.read()
        conn.close()

    def seed(self, entry_id, statement, keywords):
        """Plant ONE entry the probe sentence will hit. Without this the
        differential oracle is decoration: an empty store returns zero hits on
        BOTH fronts, the banner filters delete the only lines that differ, and
        the assertion compares [] == [] — it would pass while the MCP front
        rendered hits with a different separator, order, or formatter."""
        from helm.store import cli as store_cli
        rc = store_cli.cmd_store(
            ["add", "premise", "%s | %s | %s" % (entry_id, statement, keywords)])
        self.assertEqual(rc, 0, "seeding must succeed or the oracle is blind")

    def cli_resolve(self, text):
        import contextlib
        import io
        from helm.store import cli as store_cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = store_cli.cmd_store(["resolve", text])
        self.assertEqual(rc, 0)
        return [ln for ln in buf.getvalue().splitlines()
                if not ln.startswith("helm store resolve:")]

    def test_the_two_front_ends_agree_on_a_SEEDED_hit(self):
        """The owner's card-1 ruling as an executable check — and it only
        earns the name with a MUST-HIT: seed an entry, prove the CLI front
        renders a NON-EMPTY answer, then demand the wire match it line for
        line. Any drift in real-hit rendering (a different formatter, a
        different join, reordered hits) fails here; the pre-seed version of
        this test could not have caught one."""
        probe = ("zzqx marker: rendering parity front, and the "
                 "ordering separators rows question")
        # TWO entries, not one: with a single hit there is no SEPARATOR and no
        # ORDER to differ, so a rendering mutation stays invisible — measured,
        # the one-entry version survived a join-separator mutation. The count
        # assertion below is what keeps this honest if resolve's cap changes.
        # The two statements must be GENUINELY different, not near-copies:
        # the store's own duplicate guard refuses a second entry that shares a
        # symptom phrase with the first (it caught this fixture being lazy).
        self.seed("mcpd-oracle-probe-a",
                  "Rendering parity between the CLI front and the wire front "
                  "is proven by comparing their emitted lines",
                  "zzqx marker, rendering parity front")
        self.seed("mcpd-oracle-probe-b",
                  "Ordering and separators only become observable once more "
                  "than a single row is returned",
                  "zzqx marker, ordering separators rows")
        cli_lines = self.cli_resolve(probe)
        # MUST-HIT, and MUST-BE-PLURAL: without >=2 lines the comparison
        # cannot see separator or ordering drift at all
        self.assertGreaterEqual(
            len(cli_lines), 2,
            "the oracle needs MULTIPLE hits or it cannot see rendering drift")
        self.assertTrue(any("mcpd-oracle-probe" in ln for ln in cli_lines))
        _s, out = self.call("tools/call", {
            "name": "store_resolve", "arguments": {"text": probe}})
        got = out["result"]
        mcp_lines = got["content"][0]["text"].splitlines()
        self.assertEqual(mcp_lines, cli_lines)
        self.assertTrue(got["structuredContent"]["fired"])
        self.assertEqual(got["structuredContent"]["hits"], len(cli_lines))

    def test_the_two_front_ends_agree(self):  # noqa: VACUOUS_ASSERTION — the ZERO-HIT companion to the seeded test above: it pins the two banner semantics, and the seeded test carries the must-hit
        """The ZERO-HIT companion: with an empty store both fronts must be
        empty of ENTRIES while each prints its own banner. Named honestly —
        alone it is vacuous, which is exactly why the seeded test above
        exists; together they cover both populations."""
        import contextlib
        import io
        from helm.store import cli as store_cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = store_cli.cmd_store(
                ["resolve", "does anything fire for this sentence"])
        self.assertEqual(rc, 0)
        cli_lines = [ln for ln in buf.getvalue().splitlines()
                     if not ln.startswith("helm store resolve:")]
        _s, out = self.call("tools/call", {
            "name": "store_resolve",
            "arguments": {"text": "does anything fire for this sentence"}})
        got = out["result"]
        mcp_lines = [ln for ln in got["content"][0]["text"].splitlines()
                     if ln != "(no entries fire)"]
        self.assertEqual(mcp_lines, cli_lines)
        # the fired flag and the CLI's own no-match banner must agree too
        self.assertEqual(got["structuredContent"]["fired"],
                         bool(cli_lines))

    def test_a_headerless_request_is_refused_not_served(self):
        """A modern-only server MUST reject a request missing the required
        mirrored headers — 'a required standard header is missing' is Server
        Validation's first failure condition, and omitting the version header
        is precisely how a pre-2025-06-18 client speaks. The conforming call
        is the must-hit control."""
        status, out = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            conforming=False)
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_HEADER_MISMATCH)
        self.assertIn("missing", out["error"]["message"])
        status, _out = self.call("tools/list")      # MUST-HIT: same call, headers
        self.assertEqual(status, 200)

    def test_tools_call_without_the_name_header_is_refused(self):
        status, out = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "store_resolve", "arguments": {"text": "x"}}},
            headers={"MCP-Protocol-Version": mcpd.PROTOCOL_VERSION,
                     "Mcp-Method": "tools/call"}, conforming=False)
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_HEADER_MISMATCH)
        self.assertIn("Mcp-Name", out["error"]["message"])

    def test_a_base64_encoded_name_header_is_decoded_before_comparison(self):
        """Servers MUST decode the sentinel before comparing — a raw compare
        would refuse a conforming client with a false HeaderMismatch."""
        import base64 as b64
        encoded = "=?base64?%s?=" % b64.b64encode(
            b"store_resolve").decode("ascii")
        status, out = self.call(
            "tools/call",
            {"name": "store_resolve", "arguments": {"text": "anything"}},
            headers={"Mcp-Name": encoded})
        self.assertEqual(status, 200)
        self.assertNotIn("error", out)

    def test_a_notification_gets_202_with_no_body(self):
        """'If the server accepts it, the server MUST return HTTP status code
        202 Accepted with no body.' Answering one with a result also breaks
        JSON-RPC, which forbids replying to a notification at all."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp",
                     json.dumps({"jsonrpc": "2.0", "method": "tools/list"}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 202)
        self.assertEqual(body, b"")

    def test_hostile_body_shapes_get_an_http_response_not_a_dead_socket(self):  # noqa: VACUOUS_ASSERTION — the positive control (a well-shaped call answering 200 with a result) IS unconditional and precedes the loop; the rung sees the in-loop assertions and cannot tell the control moved out
        """Three valid-JSON bodies used to raise inside the handler thread;
        socketserver ate the traceback and closed the socket, so a compliant
        client saw 'closed without response' — neither error tier, and
        indistinguishable from a dead server."""
        # UNCONDITIONAL positive control: the same endpoint answers a WELL
        # shaped call, so a loop that somehow ran zero iterations could not
        # masquerade as proof (the rung's own point, and it is right).
        status, out = self.call("tools/call", {
            "name": "store_resolve", "arguments": {"text": "control"}})
        self.assertEqual(status, 200)
        self.assertIn("result", out)
        for params in ("notadict", {"_meta": "notadict"},
                       {"name": {"unhashable": 1}}):
            with self.subTest(params=params):
                status, out = self.post(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": params},
                    headers={"MCP-Protocol-Version": mcpd.PROTOCOL_VERSION,
                             "Mcp-Method": "tools/call",
                             "Mcp-Name": "store_resolve"}, conforming=False)
                self.assertIn(status, (200, 400))
                self.assertIsNotNone(out)
                self.assertIn("error", out)

    def test_a_batch_body_is_invalid_request_not_parse_error(self):
        status, out = self.post([{"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list"}], conforming=False)
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_REQUEST)

    def test_delete_is_405_like_get(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("DELETE", "/mcp")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.getheader("Allow"), "POST")
        resp.read()
        conn.close()

    def test_tool_inputs_are_validated_against_the_advertised_schema(self):
        """'Servers MUST: Validate all tool inputs.' The schema is a contract:
        a non-string text was str()-coerced into a Python repr, and an extra
        key was silently swallowed though additionalProperties is false."""
        _s, out = self.call("tools/call", {
            "name": "store_resolve", "arguments": {"text": 123}})
        self.assertTrue(out["result"]["isError"])
        self.assertIn("must be a string", out["result"]["content"][0]["text"])
        _s, out = self.call("tools/call", {
            "name": "store_resolve",
            "arguments": {"text": "fine", "bogus": "extra"}})
        self.assertTrue(out["result"]["isError"])
        self.assertIn("bogus", out["result"]["content"][0]["text"])

    def test_the_endpoint_path_is_the_only_one_served(self):
        status, out = self.post({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list", "params": {}},
                                conforming=False)
        self.assertEqual(status, 400)   # MUST-HIT: /mcp itself still answers
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/anything", json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
            {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 404)
        self.assertIn("no MCP endpoint", body["error"]["message"])

    def test_the_wire_carries_project_scope_like_the_CLI_does(self):  # noqa: VACUOUS_ASSERTION — the assertFalse global-only miss is the CONTROL, not the claim; the unconditional positive half (scoped read fires AND its content names the entry) follows it and would fail if the seed never landed, so the pair cannot both pass vacuously
        """The finding, and MY ORACLE'S BLIND SPOT: `helm store resolve`
        INFERS a project from cwd on reads, so the CLI saw project entries the
        wire could not. My differential test could not catch it because its
        fixture had NO project — a one-project env cannot see a project bug.

        This pins the scope as PER-REQUEST input (the stateless idiom) and
        proves the wire reaches an entry that global-only resolution misses."""
        from helm.store import cli as store_cli
        rc = store_cli.cmd_store(
            ["add", "premise", "--project", "zzproj",
             "mcpd-project-scoped | Only a project-scoped read reaches this "
             "entry | zzscope marker phrase"])
        self.assertEqual(rc, 0)
        probe = "zzscope marker phrase for the scoped read"
        # global-only (no project) must NOT see it — the control that makes
        # the positive result mean something
        _s, out = self.call("tools/call", {
            "name": "store_resolve", "arguments": {"text": probe}})
        self.assertFalse(out["result"]["structuredContent"]["fired"])
        # the SAME call with the project scope DOES see it
        _s, out = self.call("tools/call", {
            "name": "store_resolve",
            "arguments": {"text": probe, "project": "zzproj"}})
        got = out["result"]
        self.assertTrue(got["structuredContent"]["fired"])
        self.assertIn("mcpd-project-scoped", got["content"][0]["text"])

    def test_the_slash_variants_do_not_bypass_the_single_route(self):
        """The bug was IN THE FIX FOR THE FIRST ROUND: the
        path check rstrip()ed the trailing slash and then admitted "", so
        both "/" and "/mcp/" walked through the guard written to stop
        exactly that. A fix that re-opens the hole it closes is worse than
        no fix, so this pins the literal path."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                           "params": {}})
        for path in ("/", "/mcp/", "/mcp/x", "/anything"):
            with self.subTest(path=path):
                conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                                  timeout=10)
                conn.request("POST", path, body,
                             {"Content-Type": "application/json"})
                resp = conn.getresponse()
                out = json.loads(resp.read().decode("utf-8"))
                conn.close()
                self.assertEqual(resp.status, 404, path)
                self.assertIn("no MCP endpoint", out["error"]["message"])
        # MUST-HIT: the exact path still answers, so the four refusals above
        # are the guard working rather than the server being dead
        status, _out = self.call("tools/list")
        self.assertEqual(status, 200)

    def test_a_jsonrpc_1_0_body_is_refused(self):
        status, out = self.post(
            {"jsonrpc": "1.0", "id": 1, "method": "tools/list",
             "params": {"_meta": {mcpd._META_VERSION: mcpd.PROTOCOL_VERSION}}},
            headers={"MCP-Protocol-Version": mcpd.PROTOCOL_VERSION,
                     "Mcp-Method": "tools/list"}, conforming=False)
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_REQUEST)

    def test_a_request_without_body_metadata_is_refused(self):
        """Headers mirror the body, so a request with correct HEADERS and no
        _meta satisfied the mirror check while being non-conforming — which
        is exactly what this suite itself was sending until now."""
        status, out = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"MCP-Protocol-Version": mcpd.PROTOCOL_VERSION,
                     "Mcp-Method": "tools/list"}, conforming=False)
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], mcpd.E_INVALID)
        self.assertIn("_meta", out["error"]["message"])

    def test_a_bad_port_argument_is_a_sentence_not_a_traceback(self):
        import contextlib
        import io

        # UNCONDITIONAL POSITIVE CONTROL: a VALID port gets PAST parsing and
        # reaches serve(). Without it a loop that ran zero iterations would
        # pass, and the rung was right to say so. serve is stubbed because
        # the happy path would otherwise bind and block forever.
        reached = []

        class _FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt      # cmd_mcpd's own clean exit path
            def shutdown(self):
                pass

        def _stub(port=None, host=None):
            reached.append(port)
            return _FakeServer()

        with mock.patch.object(mcpd, "serve", _stub):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = mcpd.cmd_mcpd(["serve", "--port", "7999"])
        self.assertEqual(reached, [7999])
        self.assertEqual(rc, 0)

        for bad in ("notanint", "70000", "0"):
            with self.subTest(port=bad):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = mcpd.cmd_mcpd(["serve", "--port", bad])
                self.assertEqual(rc, 2)
                self.assertTrue(err.getvalue().startswith("helm mcpd:"))


class SeatIdentityTest(McpdBase):
    """Slice 1's whole security surface. THE BAR IS EQUALITY WITH THE CLI, not
    purity: `helm chat post` fills `who` from HELM_CHAT_NAME, so CLI
    attribution already reduces to trusting the local unix user. A token
    minted under that same identity is exactly as strong; reading a seat NAME
    out of a request would be strictly WEAKER, and that is the line."""

    def test_a_token_resolves_to_its_seat_and_nothing_else_does(self):
        tok, err = mcpd.mint("cj")
        self.assertIsNone(err, err)
        self.assertEqual(mcpd._seat_for(tok), "cj")
        # the negative half, which is the actual security property
        self.assertIsNone(mcpd._seat_for("deadbeef" * 6))
        self.assertIsNone(mcpd._seat_for(""))
        self.assertIsNone(mcpd._seat_for(None))

    def test_minting_is_idempotent_and_per_seat(self):
        first, _ = mcpd.mint("cj")
        self.assertEqual(mcpd.mint("cj")[0], first)   # one live token per seat
        other, _ = mcpd.mint("codex")
        self.assertNotEqual(other, first)
        self.assertEqual(mcpd._seat_for(other), "codex")

    def test_minting_is_idempotent_by_IDENTITY_not_by_SPELLING(self):
        """Measured through the documented path with nothing but
        env, and it breaks the docstring's own stated invariant.

        `one live token per seat` was one token per SPELLING: the lookup
        compared RAW strings while every other helm seam compares canonically
        (_seat_key casefolds, _canonical_recipient casefolds, and seats_roster
        REFUSES a rename that collides case-insensitively). So HELM_CHAT_NAME
        =Attacker then =attacker minted TWO live tokens for what helm elsewhere
        says out loud is ONE seat, and the RAW spelling was what reached a row
        the owner reads — the display/identity split arriving by a route the
        forgery pins could not see, because the value comes through the FILE.

        Not a forgery escalation: the display half needs local user access,
        which this design already trusts. The IDEMPOTENCY break needs nothing
        but two spellings of your own name."""
        first, err = mcpd.mint("Attacker")
        self.assertIsNone(err)
        again, err = mcpd.mint("attacker")
        self.assertIsNone(err)
        self.assertEqual(again, first, "a case variant minted a SECOND token "
                                       "for one seat")
        # the stored side folds too, so the table cannot hold both spellings
        table = pk.read_json(mcpd._tokens_path(), {})
        self.assertEqual(len(table), 1)
        self.assertEqual(sorted(set(table.values())), ["attacker"])
        # and the attribution that reaches a row is the CANONICAL identity
        self.assertEqual(mcpd._seat_for(first), "attacker")
        # unconditional positive control: distinct seats still get distinct
        # tokens, so folding has not collapsed identity altogether
        other, err = mcpd.mint("codex")
        self.assertIsNone(err)
        self.assertNotEqual(other, first)
        self.assertEqual(mcpd._seat_for(other), "codex")

    def test_CONCURRENT_MINTS_ALL_SURVIVE(self):
        """No single-process test could have seen this.

        mint is READ-MODIFY-WRITE over one JSON file and pk.write_json
        replaces the WHOLE file, so two seats minting at once both read the
        same table, each add their own token, and the second replace DROPS the
        first. Nothing is corrupt and nothing errors — the losing seat is
        simply handed a bearer token the table has never heard of, which
        authenticates as anonymous forever. atomic_write makes every
        INDIVIDUAL write safe and says nothing whatever about the pair, which
        is precisely why this survived repeated review.

        THE BARRIER IS WHAT MAKES THIS A PROOF. Without it the children
        serialize by luck and the test passes on the broken code; every child
        blocks until all of them are ready, so the read-modify-write windows
        genuinely overlap."""
        import multiprocessing
        # SPAWN, NOT FORK. This class runs an HTTP server thread, and forking
        # a multi-threaded process is a documented deadlock hazard (CPython
        # 3.14 warns about it by name) — a rare hang inside a 10k-test gate is
        # far worse than the second spawn costs, and the child needs nothing
        # from this process but the environment, which spawn carries.
        ctx = multiprocessing.get_context("spawn")
        seats = ["seat-%02d" % i for i in range(8)]
        barrier, out = ctx.Barrier(len(seats)), ctx.Queue()
        procs = [ctx.Process(target=_mint_in_child, args=(s, barrier, out))
                 for s in seats]
        for p in procs:
            p.start()
        try:
            got = [out.get(timeout=60) for _ in seats]
        finally:
            for p in procs:
                p.join(timeout=60)

        minted = {s: t for s, t, err in got if err is None and t}
        # UNCONDITIONAL POSITIVE CONTROL: the probe must have actually minted,
        # or every assertion below is a statement about an empty set.
        self.assertTrue(minted, "no child minted at all: %r" % (got,))
        # Every child should SUCCEED — the lock serializes them. A refusal
        # here is the read-back firing, which means _flocked failed open on
        # this filesystem; that is a real finding about the substrate, not a
        # flake to retry, so it is named rather than tolerated.
        self.assertEqual(sorted(minted), sorted(seats),
                         "a child was refused, so the lock failed open and "
                         "the read-back caught the race: %r" % (got,))
        # THE PROPERTY, and the line that reddens on the pre-cure code: a
        # token that was handed out must resolve to the seat it was handed to.
        for seat, tok in sorted(minted.items()):
            self.assertEqual(mcpd._seat_for(tok), seat,
                             "%s holds a token the table lost" % seat)
        # and the table holds exactly one entry per seat, no more
        table = pk.read_json(mcpd._tokens_path(), {})
        self.assertEqual(sorted(table.values()), sorted(seats))
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: minting once
        # more in THIS process still resolves, so every `_seat_for(tok) ==
        # seat` above is a discrimination rather than a resolver that cannot
        # answer at all under this fixture. Last, so it perturbs no assertion
        # written before it.
        local, err = mcpd.mint("control")
        self.assertIsNone(err, err)
        self.assertEqual(mcpd._seat_for(local), "control")

    def test_mint_REFUSES_when_it_cannot_OWN_the_table(self):
        """A LOCK THAT FAILS OPEN IS AVAILABILITY SEMANTICS, AND MINT ISSUES A
        CAPABILITY (on the first cure written for the finding).

        `seats_common._flocked` yields holding nothing when flock raises, and
        the body runs anyway. That is right for a shared-state mutation, where
        finishing matters more than serialising. Handing out a bearer token
        under those semantics promises something we cannot keep, so mint
        refuses instead of inheriting them."""
        from helm import seats_common

        class _NoLock:                      # exactly _flocked's fail-open state
            def __init__(self, *a, **k):
                self.f = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(seats_common, "_flocked", _NoLock):
            tok, err = mcpd.mint("cj")
        self.assertIsNone(tok, "a token was issued without owning the table")
        self.assertIn("lock", err)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: with the real
        # lock the same call succeeds, so the refusal above is a discrimination
        # and not mint being broken for every input.
        tok, err = mcpd.mint("cj")
        self.assertIsNone(err, err)
        self.assertEqual(mcpd._seat_for(tok), "cj")

    def test_NO_CALLER_IS_TOLD_SUCCESS_WHILE_ITS_TOKEN_IS_ABSENT(self):
        """The deterministic schedule, as an arm — because a start
        barrier can MISS this interleaving and stay green.

        With _flocked yielding no lock, and B's write gated on A having
        RETURNED (stronger than gating on A's readback, and simpler to drive):

            A and B both read {}
            A writes {tok-A}, reads back tok-A, returns SUCCESS
            B writes its STALE {tok-B}, reads back tok-B, returns SUCCESS
            final table is {tok-B}; A holds a token that authenticates as
            nobody and was told it succeeded

        THE ASSERTION IS THE INVARIANT, NOT THE MECHANISM: no caller may be
        told success while its token is absent from the final table. That
        stays true of any future design that achieves it a different way,
        where asserting "it refuses" would pin today's implementation."""
        import threading
        from helm import seats_common

        entered = []

        class _NoLock:
            def __init__(self, *a, **k):
                self.f = None

            def __enter__(self):
                entered.append(1)
                return self

            def __exit__(self, *exc):
                return False

        both_read = threading.Barrier(2)
        a_returned = threading.Event()
        first_read, results = set(), {}
        seen_lock = threading.Lock()
        real_read, real_write = pk.read_json, pk.write_json

        def staged_read(path, default=None):
            out = real_read(path, default)
            name = threading.current_thread().name
            if name in ("A", "B") and str(path).endswith(mcpd.TOKENS):
                with seen_lock:
                    first = name not in first_read
                    first_read.add(name)
                if first:            # the readback must NOT re-enter this
                    both_read.wait(timeout=15)
            return out

        def staged_write(path, data):
            name = threading.current_thread().name
            if name == "B" and str(path).endswith(mcpd.TOKENS):
                a_returned.wait(timeout=15)
            return real_write(path, data)

        def run():
            name = threading.current_thread().name
            try:
                results[name] = mcpd.mint("seat-" + name)
            finally:
                if name == "A":
                    a_returned.set()

        threads = [threading.Thread(target=run, name=n) for n in ("A", "B")]
        with mock.patch.object(seats_common, "_flocked", _NoLock), \
             mock.patch.object(pk, "read_json", staged_read), \
             mock.patch.object(pk, "write_json", staged_write):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(sorted(results), ["A", "B"], "a caller never returned")
        # PROBE-IS-NOT-BLIND CONTROL: the fail-open state must actually have
        # been entered, or this whole schedule ran under a REAL lock and
        # proved nothing about the case it exists for.
        self.assertGreaterEqual(len(entered), 2,
                                "the fail-open lock stub never ran")
        final = pk.read_json(mcpd._tokens_path(), {})
        for name in sorted(results):
            tok, err = results[name]
            if err is None:
                self.assertIn(tok, final,
                              "%s was told SUCCESS and its token is absent "
                              "from the final table, so it authenticates as "
                              "nobody" % name)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: a normal mint
        # still lands in the table, so the loop above is a real check rather
        # than a statement about a table nothing can ever reach.
        good, err = mcpd.mint("control")
        self.assertIsNone(err, err)
        self.assertIn(good, pk.read_json(mcpd._tokens_path(), {}))

    def test_case_fold_duplicates_ALREADY_IN_THE_TABLE_are_pruned(self):
        """Canonicalising the COMPARISON stopped mint from
        ADDING a duplicate and left every duplicate already written LIVE.
        Two working bearer tokens for one identity means
        revoking one revokes nothing, so this module's stated "one live token
        per seat" was false for exactly the tables that predate that fix —
        the ones a real deployment has."""
        path = mcpd._tokens_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        first, second = "aa" * 24, "bb" * 24
        pk.write_json(path, {first: "Attacker", second: "attacker"})
        # PRECONDITION, ASSERTED RATHER THAN ASSUMED: both spellings really do
        # resolve to the one identity before the mint, so this fixture is the
        # two-live-tokens state and not something I got wrong while writing it.
        self.assertEqual(mcpd._seat_for(first), "attacker")
        self.assertEqual(mcpd._seat_for(second), "attacker")

        tok, err = mcpd.mint("attacker")
        self.assertIsNone(err, err)
        table = pk.read_json(path, {})
        self.assertEqual(list(table.values()), ["attacker"])
        self.assertEqual(list(table), [tok])
        # REVOKED, not merely unreturned — the whole finding is that the loser
        # kept working.
        dead = second if tok == first else first
        self.assertIsNone(mcpd._seat_for(dead),
                          "the duplicate is still a live bearer token")

    def test_a_SCALAR_owner_never_yields_a_usable_token(self):
        """mint and _seat_for disagreed about what an owner entry IS.
        mint asked `str(owner or "")`, so an entry of `123`
        stringified into a legal seat name and mint HANDED THAT TOKEN BACK;
        _seat_for demanded a real str and served anonymous. The caller was
        given a token that could never authenticate, and nothing said why."""
        path = mcpd._tokens_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        poisoned = "cc" * 24
        pk.write_json(path, {poisoned: 123})
        # PRECONDITION: the reader already refuses it. That half is what made
        # the mismatch invisible — only mint was wrong, and only about a value
        # it would never resolve itself.
        self.assertIsNone(mcpd._seat_for(poisoned))

        tok, err = mcpd.mint("123")
        self.assertIsNone(err, err)
        self.assertNotEqual(tok, poisoned,
                            "mint returned a token its own reader refuses")
        # POSITIVE: what mint DID hand back works, so the refusal above is a
        # real discrimination and not mint failing at everything.
        self.assertEqual(mcpd._seat_for(tok), "123")

    def test_a_seatless_mint_is_REFUSED(self):
        tok, err = mcpd.mint("")
        self.assertIsNone(tok)
        self.assertIn("no seat identity", err)

    def test_chat_post_REFUSES_without_a_resolved_seat(self):
        """The refusal IS the feature. A post the server cannot attribute
        would put text on a surface the OWNER READS under a name it did not
        verify — and signing it with the server's key makes that laundering
        look authentic."""
        payload, err = mcpd._tool_chat_post({"text": "hello"}, None)
        self.assertIsNone(payload)
        self.assertIn("needs a seat", err)
        self.assertIn("helm mcpd token", err)

    def test_NO_REQUEST_FIELD_CAN_SUPPLY_A_SEAT(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the EXACT producer set (assertEqual against three named expressions) plus assertTrue(producers, 'probe is blind'); the assertNotIn lines are the security property layered on top of a probe already proven non-empty
        """THE ONE LINE THAT MUST NOT EXIST. If any path lets a caller name
        its own seat, MCP becomes strictly weaker than the CLI it mirrors and
        every attribution on the owner's surface becomes a claim rather than a
        fact. Pinned against the SOURCE of the request handler, because this
        is a property of what the code does NOT do."""
        import inspect
        producers = _seat_producers(inspect.getsource(mcpd))
        self.assertTrue(producers, "no seat assignment found — probe is blind")
        # NEGATIVE: no seat value may derive from anything the CALLER sends.
        for value in producers:
            for forbidden in ("_meta", "header", "body", "arguments"):
                self.assertNotIn(forbidden, value.lower(),
                                 "a seat derived from request data: %s"
                                 % value)
        # POSITIVE: the exact producer set, pinned. A NEW way to produce a
        # seat reddens here and forces whoever adds it through this question,
        # which is the whole point — the property is about what the module
        # does NOT do, and only an enumeration can defend that.
        self.assertEqual(sorted(producers), sorted([
            "str(seat or '').strip()",    # inside mint: normalising its arg
            "_seat_for(token)",           # the handler: bearer -> seat
            # AND IT REDDENED ON ITS AUTHOR A SECOND TIME, which is the point:
            # `table.get(str(token))` USED to be a producer here, because
            # _seat_for assigned the table value to `seat` and then validated
            # it in place. Collapsing both doors onto _owner_seat
            # turned that expression into an ARGUMENT rather than a
            # binding, so the module now has one fewer way to produce this
            # name. Removing a producer is the safe direction and it still
            # has to be stated here — the enumeration is measured from the
            # module, never from my memory of what I edited.
            # THE PIN DID ITS JOB ON ITS AUTHOR: adding canonicalisation for
            # the two-spellings finding introduced a new producer
            # and reddened this line, which is exactly the forcing function it
            # exists to be. ADMITTED, because neither site reads request data:
            # in `mint` the input is the caller's OWN home.chat_name(), and in
            # `_seat_for` it is the table value already validated by
            # _SEAT_NAME_RE. `_canonical_recipient` only casefolds and strips
            # a leading @; it cannot introduce a name that was not already
            # there. ONE entry, not two: `_seat_for` RETURNS its canonical
            # value rather than assigning it to `seat`, so it is not a
            # producer of this name at all. I wrote two and the probe said
            # one — the enumeration is measured from the module, never from
            # my memory of what I edited.
            "str(canon)",                 # inside mint: fold to identity
        ]))

    def test_the_seat_census_sees_EVERY_way_python_binds_a_name(self):
        """The census above is only a security property if it is EXHAUSTIVE,
        and it was not. It walked ast.Assign alone, so
        `seat: str = arguments["seat"]` — an annotated assignment, the same
        statement with a type on it — rode straight past the one pin whose
        whole job is to catch exactly that.

        Each form is driven through the REAL walker the census uses, not a
        hand-shaped equivalent, and must be SEEN."""
        forms = {
            "AnnAssign":     "seat: str = arguments['x']",
            "Assign":        "seat = arguments['x']",
            "AsyncFor":      ("async def f():\n"
                              "    async for seat in arguments['x']: pass"),
            "For":           "for seat in arguments['x']: pass",
            "NamedExpr":     "if (seat := arguments['x']): pass",
            "comprehension": "[1 for seat in arguments['x']]",
            "withitem":      "with arguments['x'] as seat: pass",
        }
        for name, src in sorted(forms.items()):
            with self.subTest(binder=name):
                found = _seat_producers(src)
                self.assertTrue(found, "%s binds `seat` and the census is "
                                       "blind to it" % name)
                self.assertIn("arguments", " ".join(found),
                              "%s was seen but its VALUE was not captured, "
                              "so the forbidden-source check has nothing to "
                              "read" % name)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the walker
        # returns the bound expression for the form it has ALWAYS handled.
        # The per-form loop above cannot serve as this control — a loop body
        # may run zero times, so it proves nothing on its own.
        self.assertEqual(_seat_producers("seat = arguments['x']"),
                         ["arguments['x']"])
        # NEGATIVE CONTROL: the walker is not just returning something for
        # every input. A module that binds a DIFFERENT name yields nothing,
        # which is what makes each assertTrue above a real observation.
        self.assertEqual(_seat_producers("other = arguments['x']"), [])
        # and a bare annotation binds no value at runtime, so it is not a
        # producer — the guard that keeps `seat: str` from unparsing None.
        self.assertEqual(_seat_producers("seat: str"), [])

    def test_a_HOSTILE_token_table_cannot_inject_a_seat_name(self):
        """The residual on an otherwise-clean approve, and the reason it
        matters is that my AST pin CANNOT SEE IT: that test proves no CODE
        path produces a seat from request input, while this value arrives
        through a FILE. A hand-edited or corrupted table holding
        {"tok": {"evil": 1}} would stringify to the seat name "{'evil': 1}"
        and ride onto a surface the OWNER READS.

        Validated with helm's OWN seat-name rule rather than a second
        spelling of it — a private definition of "legitimate seat name" here
        is the two-surfaces disease one field over."""
        import json
        path = mcpd._tokens_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"good": "cj",
                       "dicty": {"evil": 1},
                       "listy": ["x"],
                       "ansi": "cj\x1b[31m",
                       "toolong": "z" * 99}, fh)
        # unconditional positive control FIRST: a legitimate row still resolves
        self.assertEqual(mcpd._seat_for("good"), "cj")
        for bad in ("dicty", "listy", "ansi", "toolong"):
            with self.subTest(shape=bad):
                self.assertIsNone(mcpd._seat_for(bad))

    def test_chat_read_is_ANONYMOUS_on_purpose(self):
        """Reading is not attribution: a caller who can reach the port can
        already read the room on disk, so gating it would buy nothing and
        would make the write refusal look like access control rather than an
        identity rule."""
        payload, err = mcpd._tool_chat_read({"limit": 5}, None)
        self.assertIsNone(err, err)
        self.assertIn("content", payload)

class ServeBackgroundPollIntervalTest(unittest.TestCase):
    """PINS THE DEFAULT AND THE FORWARDING, because getting this wrong cost a
    100 Hz idle loop in every embedder once already (row 37f557c04ffa: 0.01 as
    the DEFAULT measured ~44x idle CPU, service_actions 4 -> 203 over 2.05s).

    Both arms watch the SAME observable — the kwargs serve_forever is actually
    called with — so a regression in either direction reddens one of them."""

    def _captured(self, **kw):
        seen = {}

        class FakeHttpd:
            def serve_forever(inner, **fkw):
                seen.update(fkw)
                seen["called"] = True

            def server_close(inner):
                pass

        real_serve = mcpd.serve
        mcpd.serve = lambda port, host: FakeHttpd()
        self.addCleanup(setattr, mcpd, "serve", real_serve)
        httpd, thread = mcpd.serve_background(port=0, **kw)
        thread.join(timeout=5)
        self.assertTrue(seen.get("called"), "serve_forever must have run")
        return seen

    def test_the_default_forwards_nothing_so_the_stdlib_default_stands(self):  # noqa: VACUOUS_ASSERTION — FORWARDING NOTHING IS THE PRODUCT LAW: the whole cure is that an embedder asking for nothing gets the stdlib cadence, so the ABSENCE of the kwarg is the contract, not a weak proxy for it. The body carries an unconditional positive control on the same dict (serve_forever ran and populated `seen`), and the sibling arm asserts the PRESENT case, so a memo that forwarded nothing ever would redden that one
        """PRODUCTION PATH. Passing no poll_interval must pass NO kwarg at all,
        leaving serve_forever's own 0.5 — not a small number chosen here."""
        seen = self._captured()
        # POSITIVE CONTROL IN THE TEST BODY, on the same dict the absence
        # assertion reads: serve_forever really ran and really populated
        # `seen`. Without this, an empty dict from a call that never happened
        # would satisfy the assertNotIn below just as well.
        self.assertIs(seen.get("called"), True,
                      "serve_forever must have been invoked at all")
        self.assertNotIn("poll_interval", seen,
                         "an embedder that asks for nothing must get the "
                         "stdlib cadence, never a fast idle loop")

    def test_an_explicit_interval_is_forwarded_verbatim(self):
        """TEST PATH. The caller that wants a fast shutdown asks for one, and
        the value reaches serve_forever unchanged."""
        seen = self._captured(poll_interval=0.01)
        self.assertEqual(seen.get("poll_interval"), 0.01)

class McpdBaseAsksForAFastShutdownTest(unittest.TestCase):
    """PINS THE FIXTURE'S OWN USE, which is a DIFFERENT hole from pinning
    serve_background's forwarding — named twice in review and only one closed.

    ServeBackgroundPollIntervalTest proves the FUNCTION forwards correctly.
    NEITHER of those arms notices if McpdBase stops ASKING: delete
    poll_interval=0.01 from setUp and every test still passes while the module
    silently returns to ~19.6s. The saving is invisible to every assertion
    about behaviour, so it needs a pin on the CALL, exactly like the
    filter-branch squelch.

    This drives the REAL McpdBase.setUp through a spy rather than re-stating
    what it ought to do, so a rewrite of the fixture cannot drift past it."""

    def test_the_fixture_requests_a_fast_shutdown_interval(self):  # noqa: VACUOUS_ASSERTION — ABSENCE IS ALREADY CAUGHT BY THE DEFAULT: seen.get('poll_interval', 0.5) yields the stdlib value when the fixture does not ask, and 0.5 <= 0.05 is FALSE, so a missing key reddens rather than passes. Mutation-proven against the real McpdBase: deleting the argument gives 20.072s and this arm fails. The body also carries an unconditional positive control on the same dict
        seen = {}
        real = mcpd.serve_background

        def spy(*a, **kw):
            seen.update(kw)
            seen["called"] = True
            return real(*a, **kw)

        mcpd.serve_background = spy
        self.addCleanup(setattr, mcpd, "serve_background", real)

        case = McpdBase("run")            # the real fixture, not a copy of it
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)

        # POSITIVE CONTROL on the same dict: the fixture really started a
        # server, so a missing key below means "did not ask", never "did not
        # run".
        self.assertIs(seen.get("called"), True,
                      "McpdBase.setUp must start a background server")
        self.assertLessEqual(
            seen.get("poll_interval", 0.5), 0.05,
            "McpdBase must ASK for a fast shutdown — without it every test "
            "pays one stdlib 0.5s poll on cleanup and the module returns to "
            "~19.6s with the whole suite still green")
