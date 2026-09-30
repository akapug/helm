#!/usr/bin/env python3
"""`helm classify` on ANY install: the backend is a host setting, `none` is a
fresh install's, and the injection trim's shadow never changes a delivered byte.

THE BACKEND IS A SETTING. `<helm home>/_global/classify.json` (or
HELM_CLASSIFY_BACKEND) names one of `none`, `openai-compatible`, `jev`. With
nothing set and nothing in the legacy discovery the answer is `none`, and a
`none` host opens no socket at all.

EVERY WIRE ARM TALKS TO A FAKE SERVER on 127.0.0.1 that this module starts:
200, 503, 413 and a reply slower than the budget, for the classify contract
and for the Jev gateway's evaluate call. No arm reaches a real stream.
"""
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-classify-backend-", var="HELM_HOME")

from helm import classify, home, inject, pk, store  # noqa: E402

LABELS = {"yes": "the text says yes", "no": "the text says no"}
ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CF_ENDPOINT", "MELD_CF_ENDPOINT", "HELM_SEAT_NAMES",
            "MELD_SEAT_NAMES", "CLAUDE_CODE_SESSION_ID", "HELM_AGENT_HARNESS",
            classify.URL_ENV, classify.TIMEOUT_ENV, classify.BACKEND_ENV,
            classify.KEY_ENV_ENV, classify.INJECT_ENV, "AI_GATEWAY_API_KEY",
            "ARM_GATEWAY_KEY")


class Fake(http.server.BaseHTTPRequestHandler):
    """One scripted reply per request: (status, body, delay_s)."""
    protocol_version = "HTTP/1.1"
    script = [(200, {}, 0)]
    seen = []

    def log_message(self, *a):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"path": self.path, "body": body,
                          "auth": self.headers.get("Authorization")})
        status, reply, delay = Fake.script.pop(0) if len(Fake.script) > 1 \
            else Fake.script[0]
        if delay:
            time.sleep(delay)
        data = json.dumps(reply).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


class Drip(http.server.BaseHTTPRequestHandler):
    """Headers at once, then the body one byte per DRIP_S: every socket wait
    is short, the whole reply is long (REVIEW 3092 finding 1)."""
    protocol_version = "HTTP/1.1"
    DRIP_S = 0.1
    # Set by a test's cleanup: the drip stops at its next byte, so the
    # request thread and the abandoned classify thread end with the test
    # instead of outliving it (the land gate's leak audit fails a live one).
    STOP = threading.Event()

    def log_message(self, *a):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        data = json.dumps(contract()).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            for i in range(len(data)):
                if Drip.STOP.is_set():
                    break
                self.wfile.write(data[i:i + 1])
                self.wfile.flush()
                time.sleep(self.DRIP_S)
        except OSError:
            pass


def contract(scores=None, label="yes"):
    return {"label": label, "scores": scores or {"yes": 0.9, "no": 0.1},
            "backend": "fake", "ms": 3}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-classify-backend-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SEAT_NAMES"] = os.path.join(self.tmp, "seat-names.txt")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.makedirs(os.path.join(home.global_dir(), ".state"), exist_ok=True)
        Fake.script, Fake.seen = [(200, contract(), 0)], []
        Drip.STOP.clear()
        self.threads_before = set(threading.enumerate())
        # Registered first, so it runs LAST: after every server this test
        # started has been stopped, shut down and closed.
        self.addCleanup(self.settle_threads)

    def settle_threads(self):
        """Every thread this test started ends inside the test: a request
        thread, the server loop, a classify call abandoned at its budget."""
        for t in set(threading.enumerate()) - self.threads_before:
            t.join(5)
        left = [t.name for t in set(threading.enumerate()) - self.threads_before
                if t.is_alive()]
        self.assertEqual(left, [], "threads outlived the test")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def server(self, handler=None):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                              handler or Fake)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # Cleanups run last-added first: stop the drip, stop the loop, then
        # close the socket; setUp's settle_threads waits out the rest.
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(Drip.STOP.set)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def set(self, **cfg):
        pk.write_json(os.path.join(home.global_dir(), classify.CONFIG), cfg)

    def no_socket(self):
        """Any socket opened inside the block fails the arm."""
        return mock.patch.object(socket, "socket",
                                 side_effect=AssertionError("a socket opened"))


class BackendSettingTest(Base):
    def test_a_fresh_install_is_none_and_opens_no_socket(self):
        b = classify.backend_setting()
        self.assertEqual((b["kind"], b["url"]), ("none", None))
        with self.no_socket(), mock.patch.object(
                classify, "_post", side_effect=AssertionError("sent")):
            got = classify.classify("hello", LABELS, "lane-diff")
            self.assertIsNone(classify.scores("hello", LABELS, "lane-diff"))
        self.assertEqual((got.ok, got.outcome), (False, "unconfigured"))
        # THE CONTROL: the same call with a backend set does send.
        self.set(backend="openai-compatible", url=self.server())
        self.assertEqual(classify.scores("hello", LABELS, "lane-diff"),
                         {"yes": 0.9, "no": 0.1})
        self.assertEqual(len(Fake.seen), 1)

    def test_the_settings_file_names_the_backend_and_the_env_overrides_it(self):
        url = self.server()
        self.set(backend="openai-compatible", url=url + "/")
        b = classify.backend_setting()
        self.assertEqual((b["kind"], b["url"], b["set_by"]),
                         ("openai-compatible", url, "settings"))
        os.environ[classify.BACKEND_ENV] = "none"
        self.assertEqual(classify.backend_setting()["kind"], "none")
        self.assertIsNone(classify.scores("hello", LABELS, "lane-diff"))
        self.assertEqual(Fake.seen, [], "the env pinned it to none")

    def test_the_legacy_discovery_still_answers_without_a_setting(self):
        os.environ[classify.URL_ENV] = "http://classify-host:8095"
        b = classify.backend_setting()
        self.assertEqual((b["kind"], b["url"]),
                         ("openai-compatible", "http://classify-host:8095"))
        self.assertEqual(classify.backend_setting(legacy=False)["kind"], "none",
                         "a consumer that asks for a SET backend sees none")

    def test_a_broken_or_unknown_setting_never_turns_a_backend_on(self):
        path = os.path.join(home.global_dir(), classify.CONFIG)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[not an object")
        os.environ[classify.URL_ENV] = "http://classify-host:8095"
        b = classify.backend_setting()
        self.assertEqual(b["kind"], "none")
        self.assertIn("did not read", b["why"])
        self.set(backend="bogus")
        self.assertIn("bogus", classify.backend_setting()["why"])
        self.assertEqual(classify.backend_setting()["kind"], "none")

    def test_a_key_written_in_the_settings_file_is_refused(self):
        self.set(backend="jev", key="sk-not-a-real-key")
        b = classify.backend_setting()
        self.assertEqual(b["kind"], "none")
        self.assertIn("key_env", b["why"])
        self.assertNotIn("sk-not-a-real-key", b["why"])

    def test_where_names_the_backend(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = classify.cmd(["where", "--json"])
        self.assertEqual((rc, json.loads(out.getvalue())["backend"]),
                         (1, "none"))
        self.set(backend="openai-compatible", url="http://classify-host:8095")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = classify.cmd(["where"])
        self.assertEqual(rc, 0)
        self.assertIn("openai-compatible", out.getvalue())


class OpenAICompatibleWireTest(Base):
    def setUp(self):
        super().setUp()
        self.set(backend="openai-compatible", url=self.server())

    def test_200_answers_every_label_score(self):
        Fake.script = [(200, contract({"yes": 0.2, "no": 0.8}, "no"), 0)]
        self.assertEqual(classify.scores("hello", LABELS, "helm-chat"),
                         {"yes": 0.2, "no": 0.8})
        self.assertEqual(Fake.seen[0]["path"], "/v1/classify")
        self.assertEqual(Fake.seen[0]["body"]["labels"], LABELS)

    def test_503_and_413_are_no_opinion(self):
        for status, want in ((503, "no-backend"), (413, "too-long")):
            with self.subTest(status=status):
                Fake.script = [(status, {"error": "x"}, 0)]
                self.assertIsNone(classify.scores("t", LABELS, "ledger"))
                self.assertEqual(classify.classify("t", LABELS, "ledger")
                                 .outcome, want)
        self.assertEqual(len(Fake.seen), 4, "each call was sent")

    def test_a_slow_backend_is_a_timeout_inside_the_budget(self):
        Fake.script = [(200, contract(), 1.0)]
        got = classify.classify("t", LABELS, "lane-diff", budget_ms=150)
        # A 1 s reply against a 150 ms budget: `timeout` is the call that
        # stopped waiting, and the reply's 200 would have been `ok`.
        self.assertEqual(got.outcome, "timeout")
        self.assertIsNone(classify.scores("t", LABELS, "lane-diff",
                                          budget_ms=150))

    def test_a_dripping_reply_is_cut_at_the_budget_as_a_whole(self):
        self.set(backend="openai-compatible", url=self.server(Drip))
        got = classify.classify("t", LABELS, "lane-diff", budget_ms=300)
        # each socket wait is 0.1 s and the reply ~6 s: a per-wait timeout
        # never fires and the call answers 200 `ok`; one wall-clock limit
        # answers `timeout` (no clock ceiling here: the outcome decides)
        self.assertEqual(got.outcome, "timeout")

    def test_a_503_names_tried_only_when_it_is_a_list(self):
        Fake.script = [(503, {"tried": "xyz"}, 0)]
        got = classify.classify("t", LABELS, "ledger")
        self.assertEqual(got.outcome, "no-backend")
        self.assertNotIn("x, y", got.detail)
        Fake.script = [(503, {"tried": ["a", "b"]}, 0)]
        self.assertIn("tried a, b",
                      classify.classify("t", LABELS, "ledger").detail)

    def test_an_answer_over_the_size_cap_is_malformed(self):
        Fake.script = [(200, dict(contract(),
                                  pad="x" * (classify.ANSWER_MAX + 10)), 0)]
        self.assertEqual(classify.classify("t", LABELS, "ledger").outcome,
                         "malformed")

    def test_a_named_key_variable_rides_the_header_only(self):
        self.set(backend="openai-compatible", url=self.server(),
                 key_env="ARM_GATEWAY_KEY")
        os.environ["ARM_GATEWAY_KEY"] = "arm-value-123"
        classify.scores("t", LABELS, "lane-diff")
        self.assertEqual(Fake.seen[-1]["auth"], "Bearer arm-value-123")
        self.assertNotIn("arm-value-123", json.dumps(Fake.seen[-1]["body"]))


class JevWireTest(Base):
    """ASSUMED WIRE (helm/classify.py _jev_body): the gateway's evaluate call
    that helm/relevance.py already speaks, one boolean question per label."""

    def setUp(self):
        super().setUp()
        self.url = self.server()
        self.set(backend="jev", url=self.url, key_env="ARM_GATEWAY_KEY")
        os.environ["ARM_GATEWAY_KEY"] = "arm-value-123"

    def test_one_question_per_label_and_the_key_from_the_named_variable(self):
        Fake.script = [(200, {"answers": {"q0": {"probability": 0.7},
                                          "q1": {"probability": 0.2}}}, 0)]
        self.assertEqual(classify.scores("hello", LABELS, "lane-diff"),
                         {"yes": 0.7, "no": 0.2})
        sent = Fake.seen[0]
        self.assertEqual((sent["path"], sent["auth"]),
                         ("/v1/evaluate", "Bearer arm-value-123"))
        self.assertEqual(sent["body"]["state"], {"message": "hello"})
        self.assertEqual(sorted(sent["body"]["questions"]), ["q0", "q1"])
        self.assertIn("the text says yes",
                      sent["body"]["questions"]["q0"]["instructions"])

    def test_answers_that_are_not_an_object_are_malformed(self):
        for answers in ([0.7, 0.2], "yes", 3):
            with self.subTest(answers=answers):
                Fake.script = [(200, {"answers": answers}, 0)]
                self.assertEqual(classify.classify("t", LABELS, "lane-diff")
                                 .outcome, "malformed")

    def test_the_built_in_gateway_url_reads_as_default(self):
        self.set(backend="jev", key_env="ARM_GATEWAY_KEY")
        self.assertEqual(classify.backend_setting()["origin"], "default")

    def test_an_unset_key_variable_sends_nothing(self):
        os.environ.pop("ARM_GATEWAY_KEY")
        got = classify.classify("hello", LABELS, "lane-diff")
        self.assertEqual(got.outcome, "unconfigured")
        self.assertIn("ARM_GATEWAY_KEY", got.detail)
        self.assertEqual(Fake.seen, [])

    def test_fail_open_answers(self):
        cases = ((503, {}, 0, None), (500, {}, 0, None),
                 (200, {"answers": {}}, 0, None), (200, {}, 1.0, 150))
        for status, reply, delay, budget in cases:
            with self.subTest(status=status, delay=delay):
                Fake.script = [(status, reply, delay)]
                self.assertIsNone(classify.scores("t", LABELS, "lane-diff",
                                                  budget_ms=budget))
        self.assertEqual(len(Fake.seen), 4)

    def test_an_over_size_or_secret_bearing_text_is_not_sent(self):
        self.assertEqual(classify.classify("x" * (classify.TEXT_MAX + 1),
                                           LABELS, "lane-diff").outcome,
                         "too-long")
        self.assertEqual(classify.classify(
            "deploy with token=abcdefghijklmnop now", LABELS,
            "lane-diff").outcome, "scrubbed")
        self.assertEqual(Fake.seen, [])
        Fake.script = [(200, {"answers": {"q0": {"probability": 0.6}}}, 0)]
        self.assertIsNotNone(classify.scores("ok", LABELS, "lane-diff"),
                             "control: a plain text is sent")


class AllowlistAndMetricTest(Base):
    def test_scores_refuses_every_other_kind_and_sends_nothing(self):
        self.set(backend="openai-compatible", url=self.server())
        for source in ("client-export", "store", "", None):
            with self.subTest(source=source):
                self.assertIsNone(classify.scores("t", LABELS, source))
        self.assertEqual(Fake.seen, [])
        self.assertIsNotNone(classify.scores("t", LABELS, "lane-transcript"))

    def test_each_consumer_counts_calls_and_answers(self):
        self.set(backend="openai-compatible", url=self.server())
        classify.scores("t", LABELS, "ledger", consumer="arm-a")
        Fake.script = [(503, {}, 0)]
        classify.scores("t", LABELS, "ledger", consumer="arm-a")
        classify.scores("t", LABELS, "ledger", consumer="arm-b")
        m = classify.metrics()["consumers"]
        self.assertEqual((m["arm-a"]["calls"], m["arm-a"]["answered"]), (2, 1))
        self.assertEqual((m["arm-b"]["calls"], m["arm-b"]["answered"]), (1, 0))


TEXT = "the zebra quokka migration needs a rollback plan before the deploy"


class InjectShadowTest(Base):
    """Through inject.gather(): the shadow scores the delivered jit lines and
    logs them on the turn's ledger row; the delivered sections never change."""

    def setUp(self):
        super().setUp()
        store.write_prior({"id": "zebra-rule",
                           "statement": "Zebra rule: check the rollback plan.",
                           "confidence": "0.8", "keywords": "zebra"})
        store.write_prior({"id": "quokka-rule",
                           "statement": "Quokka rule: stage the migration.",
                           "confidence": "0.8", "keywords": "quokka"})
        self.lane = os.path.join(self.tmp, "dev", "proj-wt", "lane-a")
        os.makedirs(self.lane)

    def gather(self, session, cwd=None):
        out = inject.gather(TEXT, session=session, cwd=cwd or self.lane)
        return out, inject._ledger_rows()[-1]

    def test_none_makes_no_call_and_adds_nothing_to_the_row(self):
        with mock.patch.object(classify, "classify",
                               side_effect=AssertionError("called")), \
                self.no_socket():
            out, row = self.gather("s-none")
        self.assertTrue(any("zebra-rule" in l for l in out["jit"]))
        self.assertNotIn("classify_shadow", row)

    def test_none_costs_the_hook_one_settings_read(self):
        reads = []
        real = home.global_json
        with mock.patch.object(home, "global_json",
                               lambda name: reads.append(name) or real(name)), \
                mock.patch.object(classify, "classify",
                                  side_effect=AssertionError("called")), \
                mock.patch.object(classify, "_lane_room",
                                  side_effect=AssertionError("looked")), \
                self.no_socket():
            self.assertIsNone(classify.inject_shadow(
                TEXT, ["one line", "two line"], self.lane))
        self.assertEqual(reads, [classify.CONFIG])

    def test_shadow_logs_scores_beside_the_injection_and_changes_no_byte(self):
        off, _row = self.gather("s-off")
        self.set(backend="openai-compatible", url=self.server())
        n = len(off["jit"])
        Fake.script = [(200, {"label": "n0", "backend": "fake", "scores": dict(
            {"n%d" % i: 0.9 if i == 0 else 0.05 for i in range(n)},
            none=0.05)}, 0)]
        on, row = self.gather("s-on")
        # (whisper is left out: its day's greeting fires once, on s-off)
        for lane in ("pinned", "jit", "reflex"):
            self.assertEqual(on[lane], off[lane], lane)
        shadow = row["classify_shadow"]
        self.assertEqual(shadow["outcome"], "ok")
        self.assertEqual(len(shadow["scores"]), n)
        self.assertEqual(shadow["relevant"], [0])
        sent = Fake.seen[0]["body"]
        self.assertEqual(sent["text"], TEXT)
        self.assertEqual(sent["labels"]["n0"], off["jit"][0])
        self.assertIn("none", sent["labels"])
        m = classify.metrics(classify.INJECT_CONSUMER)["consumers"]
        self.assertEqual(m[classify.INJECT_CONSUMER]["answered"], 1)

    def test_a_down_or_slow_backend_changes_no_byte_and_is_bounded(self):
        off, _row = self.gather("s-off")
        self.set(backend="openai-compatible", url=self.server(), inject_ms=150)
        for status, delay, want in ((503, 0, "no-backend"),
                                    (200, 1.0, "timeout")):
            with self.subTest(want=want):
                Fake.script = [(status, contract(), delay)]
                on, row = self.gather("s-" + want)
                self.assertEqual(on["jit"], off["jit"])
                self.assertEqual(row["classify_shadow"]["outcome"], want)

    def test_a_dripping_backend_leaves_the_injection_whole(self):
        off, _row = self.gather("s-off")
        self.set(backend="openai-compatible", url=self.server(Drip),
                 inject_ms=300)
        on, row = self.gather("s-drip")
        for lane in ("pinned", "jit", "reflex"):
            self.assertEqual(on[lane], off[lane], lane)
        self.assertEqual(row["classify_shadow"]["outcome"], "timeout")

    def test_only_a_lane_rooms_turn_is_sent(self):
        self.set(backend="openai-compatible", url=self.server())
        plain = os.path.join(self.tmp, "dev", "proj")
        os.makedirs(plain)
        _out, row = self.gather("s-plain", cwd=plain)
        self.assertNotIn("classify_shadow", row)
        self.assertEqual(Fake.seen, [])
        _out, row = self.gather("s-lane")
        self.assertIn("classify_shadow", row, "control: a lane room is sent")

    def test_inject_ms_zero_turns_the_shadow_off(self):
        self.set(backend="openai-compatible", url=self.server(), inject_ms=0)
        _out, row = self.gather("s-zero")
        self.assertNotIn("classify_shadow", row)
        self.assertEqual(Fake.seen, [])


if __name__ == "__main__":
    unittest.main()
