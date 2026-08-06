#!/usr/bin/env python3
"""helm mcpd — the stateless MCP shell (spec rev 2026-07-28), wire-level.

Every test speaks real HTTP against an ephemeral server on an OS-assigned
port: the wire is the contract, so the tests exercise the wire. The CLI is
the diff oracle by owner ruling (card 9d3a61c5, his words: the two fronts
'can always be COMPARED AGAINST EACH OTHER … and against the
METHOD-AGNOSTIC SPEC') — test_the_two_front_ends_agree is that ruling as
an executable check."""
import http.client
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import mcpd

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME")


class McpdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-mcpd-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "mcpd-test"
        self.httpd, self.thread = mcpd.serve_background(port=0)
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
        """codex's finding, and MY ORACLE'S BLIND SPOT: `helm store resolve`
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
        """codex round 2, and the bug was IN MY FIX FOR THEIR ROUND 1: my
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
