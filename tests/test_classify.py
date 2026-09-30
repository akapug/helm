#!/usr/bin/env python3
"""`helm classify`: discovery, the closed source set, every fail-open answer,
the journal, and the verb — through a fake transport only.

NO ARM TOUCHES THE NETWORK. Every call that would send goes through the
`post` seam (or `classify._post` patched), and the configured endpoint is the
placeholder `http://classify-host:8095`, a name no resolver answers. An arm
that forgot the seam would fail as `unreachable` rather than reach a real
stream, and the arms assert the seam was called.
"""
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-classify-", var="HELM_HOME")

from helm import classify, home, localnames, private_names  # noqa: E402

URL = "http://classify-host:8095"
LABELS = {"yes": "the text says yes", "no": "the text says no"}


def answer(label="yes", scores=None, backend="fake", forced=None):
    body = {"v": 1, "label": label, "scores": scores or {"yes": 0.9, "no": 0.1},
            "backend": backend, "ms": 5}
    if forced:
        body["forced"] = forced
    return body


class Transport:
    """The fake HTTP seam: records every request, replies from a script."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, url, body, timeout_s):
        self.sent.append((url, body, timeout_s))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-classify-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_CACHE_DIR": os.path.join(self.tmp, "cache"),
            classify.URL_ENV: URL})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(classify.TIMEOUT_ENV, None)
        self.addCleanup(self.set_host, None)

    def set_host(self, host):
        path = home.authored_path()
        if host is None:
            if os.path.exists(path):
                os.unlink(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "projects": {}, "host": host}, f)

    def journal(self):
        rows, unreadable = classify.read_journal()
        self.assertIsNone(unreadable)
        return rows

    def ask(self, transport, text="hello", source="lane-diff", **kw):
        return classify.classify(text, LABELS, source, post=transport, **kw)


class DiscoveryTest(Base):
    def test_the_environment_wins_over_the_registry(self):
        self.set_host({"classify_url": "http://registry-host:8095"})
        self.assertEqual(classify.endpoint(), (URL, "env"))

    def test_the_registry_host_block_answers_when_the_environment_is_unset(self):
        os.environ.pop(classify.URL_ENV)
        self.set_host({"classify_url": "http://registry-host:8095/"})
        self.assertEqual(classify.endpoint(),
                         ("http://registry-host:8095", "registry"))

    def test_nothing_configured_is_the_unconfigured_answer(self):
        os.environ.pop(classify.URL_ENV)
        base, why = classify.endpoint()
        self.assertIsNone(base)
        self.assertIn("classify_url", why)
        t = Transport((200, answer()))
        got = self.ask(t)
        self.assertEqual((got.ok, got.outcome), (False, "unconfigured"))
        self.assertEqual(t.sent, [], "unconfigured sends nothing")
        os.environ[classify.URL_ENV] = URL
        self.assertTrue(self.ask(t).ok)
        self.assertEqual(len(t.sent), 1, "control: configured, it sends")

    def test_off_pins_the_client_off_whatever_the_registry_says(self):
        self.set_host({"classify_url": "http://registry-host:8095"})
        os.environ[classify.URL_ENV] = "off"
        self.assertIsNone(classify.endpoint()[0])
        self.assertEqual(self.ask(Transport(answer())).outcome, "unconfigured")
        os.environ.pop(classify.URL_ENV)
        self.assertEqual(classify.endpoint(),
                         ("http://registry-host:8095", "registry"),
                         "control: unpinned, the registry answers")

    def test_an_unreadable_registry_is_unconfigured_and_says_so(self):
        os.environ.pop(classify.URL_ENV)
        path = home.authored_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[not an object")
        base, why = classify.endpoint()
        self.assertIsNone(base)
        self.assertIn("did not read", why)

    def test_a_value_that_is_not_an_http_url_configures_nothing(self):
        os.environ[classify.URL_ENV] = "classify-host:8095"
        self.assertIsNone(classify.endpoint()[0])
        os.environ[classify.URL_ENV] = "https://classify-host:8095/"
        self.assertEqual(classify.endpoint(),
                         ("https://classify-host:8095", "env"))

    def test_the_contract_path_is_appended_to_the_base(self):
        os.environ[classify.URL_ENV] = URL + "/v1/classify"
        t = Transport((200, answer()))
        self.assertTrue(self.ask(t).ok)
        self.assertEqual(t.sent[0][0], URL + "/v1/classify")


class FailOpenTest(Base):
    def outcome(self, reply, **kw):
        t = Transport(reply)
        got = self.ask(t, **kw)
        self.assertFalse(got.ok)
        self.assertIsNone(got.label)
        return got.outcome, t

    def test_every_fail_open_answer_names_why(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal nine-row table and every row asserts its exact outcome and one send
        cases = (
            (urllib.error.URLError(ConnectionRefusedError(111, "refused")),
             "unreachable"),
            (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
            (socket.timeout("timed out"), "timeout"),
            ((503, {"error": "no backend", "tried": ["one", "two"]}),
             "no-backend"),
            ((413, {"error": "too long"}), "too-long"),
            ((400, {"error": "labels"}), "rejected"),
            ((500, None), "unreachable"),
            ((200, None), "malformed"),
            ((200, {"label": "yes"}), "malformed"),
        )
        for reply, want in cases:
            with self.subTest(want=want, reply=repr(reply)[:40]):
                got, t = self.outcome(reply)
                self.assertEqual(got, want)
                self.assertEqual(len(t.sent), 1)

    def test_no_backend_names_what_the_stream_tried(self):
        t = Transport((503, {"tried": ["first", "second"]}))
        self.assertIn("first, second", self.ask(t).detail)

    def test_a_spent_budget_answers_timeout_and_sends_nothing(self):
        got, t = self.outcome((200, answer()), budget_ms=0)
        self.assertEqual(got, "timeout")
        self.assertEqual(t.sent, [])

    def test_the_budget_is_the_socket_timeout(self):
        t = Transport((200, answer()))
        self.ask(t, budget_ms=350)
        self.assertEqual(t.sent[0][2], 0.35)
        with mock.patch.dict(os.environ, {classify.TIMEOUT_ENV: "900"}):
            self.ask(t)
        self.assertEqual(t.sent[1][2], 0.9)

    def test_an_over_long_text_is_surfaced_and_never_cut(self):
        text = "x" * 70000
        got, t = self.outcome((413, {"error": "text over 60000"}), text=text)
        self.assertEqual(got, "too-long")
        self.assertEqual(len(t.sent[0][1]["text"]), 70000,
                         "the client sent the whole text; the stream refused it")
        self.assertIn("not cut", self.ask(Transport((413, None)),
                                          text=text).detail)


class SourceAllowlistTest(Base):
    def test_a_source_outside_the_closed_set_is_refused_and_nothing_is_sent(self):  # noqa: VACUOUS_ASSERTION — the closing control sends an allowed kind through the same transport and asserts one send
        for source in ("client-export", "member-data", "", None, "LANE-DIFF"):
            with self.subTest(source=source):
                t = Transport((200, answer()))
                got = self.ask(t, source=source)
                self.assertEqual(got.outcome, "refused-source")
                self.assertEqual(t.sent, [])
        t = Transport((200, answer()))
        self.assertTrue(self.ask(t, source="lane-diff").ok)
        self.assertEqual(len(t.sent), 1, "control: an allowed kind is sent")

    def test_every_source_in_the_closed_set_is_sent(self):  # noqa: VACUOUS_ASSERTION — the loop runs over SOURCES, pinned to four kinds by the unconditional equality above it, and each asserts one send
        self.assertEqual(classify.SOURCES, ("helm-chat", "ledger", "lane-diff",
                                            "lane-transcript"))
        for source in classify.SOURCES:
            t = Transport((200, answer()))
            self.assertTrue(self.ask(t, source=source).ok, source)
            self.assertEqual(len(t.sent), 1)

    def test_the_refusal_comes_before_discovery(self):
        os.environ.pop(classify.URL_ENV)
        with mock.patch.object(classify, "endpoint",
                               side_effect=AssertionError("discovered")):
            self.assertEqual(self.ask(Transport(answer()),
                                      source="client-export").outcome,
                             "refused-source")


class AnswerTest(Base):
    def test_an_answer_carries_label_score_backend_and_every_score(self):
        t = Transport((200, answer(scores={"yes": 0.8, "no": 0.2})))
        got = self.ask(t, task="decide", prefix="line:")
        self.assertEqual((got.ok, got.label, got.score, got.backend),
                         (True, "yes", 0.8, "fake"))
        self.assertEqual(got.scores, {"yes": 0.8, "no": 0.2})
        self.assertIsInstance(got.ms, int)
        body = t.sent[0][1]
        self.assertEqual((body["task"], body["prefix"], body["labels"]),
                         ("decide", "line:", LABELS))

    def test_unsure_scores_the_forced_label(self):
        t = Transport((200, answer(label="unsure", forced="no",
                                   scores={"yes": 0.3, "no": 0.7})))
        got = self.ask(t)
        self.assertTrue(got.ok)
        self.assertEqual((got.label, got.forced, got.score),
                         ("unsure", "no", 0.7))

    def test_labels_no_stream_takes_are_a_caller_error(self):
        for labels in (["one"], ["l%d" % i for i in range(17)],
                       ["ok", "not ok"], {"a": "", "b": "x"}, "a,b",
                       ["same", "same"]):
            with self.subTest(labels=labels):
                with self.assertRaises(ValueError):
                    classify.classify("t", labels, "lane-diff",
                                      post=Transport(answer()))
        self.assertTrue(classify.classify(
            "t", ["yes", "no"], "lane-diff",
            post=Transport((200, answer()))).ok)


class JournalTest(Base):
    def test_every_call_appends_one_row_and_never_the_text(self):
        secret = "the-text-itself-" + "q" * 12
        self.ask(Transport((200, answer())), text=secret, consumer="arm-a")
        self.ask(Transport((503, {})), text=secret, consumer="arm-a")
        self.ask(Transport((200, answer())), text=secret, source="client-export",
                 consumer="arm-b")
        rows = self.journal()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [(r["kind"], r["consumer"], r["source"], r["outcome"], r["label"])
             for r in rows],
            [("call", "arm-a", "lane-diff", "ok", "yes"),
             ("call", "arm-a", "lane-diff", "no-backend", None),
             ("call", "arm-b", "refused", "refused-source", None)])
        self.assertEqual(rows[0]["score"], 0.9)
        for key in ("ms", "backend", "ts", "id"):
            self.assertIn(key, rows[0])
        with open(classify.journal_path(), encoding="utf-8") as f:
            body = f.read()
        self.assertIn('"consumer":"arm-a"', body, "control: the read is the journal")
        self.assertNotIn(secret, body)

    def test_metrics_read_each_consumer_back(self):
        for score in (0.9, 0.5):
            self.ask(Transport((200, answer(scores={"yes": score,
                                                    "no": 1 - score}))),
                     consumer="arm-a")
        self.ask(Transport(socket.timeout("t")), consumer="arm-a")
        self.ask(Transport((200, answer())), consumer="arm-b")
        self.assertTrue(classify.record_labelled("arm-a", "yes", "ab" * 8, 3))
        m = classify.metrics()
        self.assertIsNone(m["unreadable"])
        a = m["consumers"]["arm-a"]
        self.assertEqual((a["calls"], a["answered"], a["confident"]), (3, 2, 1))
        self.assertEqual(a["outcomes"], {"ok": 2, "timeout": 1})
        self.assertEqual(a["labelled"], {"yes": 1})
        self.assertEqual(set(classify.metrics("arm-b")["consumers"]), {"arm-b"})

    def test_a_labelled_example_keeps_a_hash_and_a_number_only(self):
        self.assertFalse(classify.record_labelled("arm", "yes", "not a hash"))
        self.assertFalse(classify.record_labelled("arm", "bad label", "ab" * 8))
        self.assertTrue(classify.record_labelled("arm", "private", "cd" * 8, 7))
        (row,) = self.journal()
        self.assertEqual((row["kind"], row["label"], row["hash"], row["index"]),
                         ("labelled", "private", "cd" * 8, 7))

    def test_an_unreadable_journal_is_never_zero_calls(self):
        os.makedirs(classify.journal_path())
        m = classify.metrics()
        self.assertTrue(m["unreadable"])
        self.assertIsNone(m["consumers"])


class VerbTest(Base):
    def run_cmd(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = classify.cmd(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_each_line_is_its_own_call_and_says_which_line(self):
        t = Transport((200, answer()), (200, answer(label="no",
                                                    scores={"yes": .2, "no": .8})))
        with mock.patch.object(classify, "_post", t):
            rc, out, _err = self.run_cmd(
                ["--labels", "yes,no", "--source", "lane-diff", "--each-line",
                 "--json"], "first\n\nthird\n")
        self.assertEqual(rc, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([(r["line"], r["label"]) for r in rows],
                         [(1, "yes"), (3, "no")])
        self.assertEqual([b["text"] for _u, b, _t in t.sent], ["first", "third"])

    def test_a_dead_stream_is_asked_once_per_run(self):
        t = Transport(urllib.error.URLError(ConnectionRefusedError(111, "x")))
        with mock.patch.object(classify, "_post", t):
            rc, out, _err = self.run_cmd(
                ["--labels", "yes,no", "--source", "lane-diff", "--each-line",
                 "--json"], "a\nb\nc\n")
        self.assertEqual(rc, 1)
        self.assertEqual(len(t.sent), 1)
        self.assertEqual([json.loads(x)["outcome"] for x in out.splitlines()],
                         ["unreachable"] * 3)

    def test_the_budget_is_shared_by_every_line(self):  # noqa: VACUOUS_ASSERTION — the closing control reruns the verb with budget and asserts both lines were sent
        t = Transport((200, answer()))
        with mock.patch.object(classify, "_post", t):
            rc, out, _err = self.run_cmd(
                ["--labels", "yes,no", "--source", "lane-diff", "--each-line",
                 "--budget-ms", "0", "--json"], "a\nb\n")
        self.assertEqual(rc, 1)
        self.assertEqual(t.sent, [])
        self.assertEqual([json.loads(x)["outcome"] for x in out.splitlines()],
                         ["timeout", "timeout"])
        with mock.patch.object(classify, "_post", t):
            self.assertEqual(self.run_cmd(
                ["--labels", "yes,no", "--source", "lane-diff", "--each-line",
                 "--budget-ms", "5000", "--json"], "a\nb\n")[0], 0)
        self.assertEqual(len(t.sent), 2, "control: with budget, both are sent")

    def test_a_refused_source_exits_one_and_sends_nothing(self):  # noqa: VACUOUS_ASSERTION — the closing control sends an allowed kind through the same transport and asserts one send
        t = Transport((200, answer()))
        with mock.patch.object(classify, "_post", t):
            rc, out, _err = self.run_cmd(
                ["--labels", "yes,no", "--source", "member-data"], "text")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL-OPEN refused-source", out)
        self.assertEqual(t.sent, [])
        with mock.patch.object(classify, "_post", t):
            self.assertEqual(self.run_cmd(
                ["--labels", "yes,no", "--source", "lane-diff"], "text")[0], 0)
        self.assertEqual(len(t.sent), 1, "control: an allowed kind is sent")

    def test_usage_errors_exit_two(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal five-row table and every row asserts exit 2
        for argv, stdin in ((["--source", "lane-diff"], "t"),
                            (["--labels", "only", "--source", "lane-diff"], "t"),
                            (["--labels", "@nothing", "--source", "lane-diff"], "t"),
                            (["--labels", "a,b", "--source", "lane-diff"], ""),
                            (["--labels", "a,b", "--bogus"], "t")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cmd(argv, stdin)[0], 2)

    def test_where_names_the_endpoint_and_its_origin(self):
        rc, out, _err = self.run_cmd(["where", "--json"])
        self.assertEqual((rc, json.loads(out)["url"], json.loads(out)["from"]),
                         (0, URL, "env"))
        os.environ[classify.URL_ENV] = "off"
        self.assertEqual(self.run_cmd(["where"])[0], 1)

    def test_label_writes_hashed_examples_from_stdin(self):
        rc, _out, _err = self.run_cmd(
            ["label", "--consumer", "private-name", "--label", "private"],
            "%s 3\n%s\nnot-a-hash 4\n" % ("ab" * 8, "cd" * 8))
        self.assertEqual(rc, 0)
        self.assertEqual([(r["hash"], r["index"]) for r in self.journal()],
                         [("ab" * 8, 3), ("cd" * 8, None)])

    def test_metrics_prints_each_consumer(self):
        self.ask(Transport((200, answer())), consumer="arm-a")
        rc, out, _err = self.run_cmd(["metrics"])
        self.assertEqual(rc, 0)
        self.assertIn("arm-a: 1 call(s), 1 answered", out)


class PrivateNameLabelSetTest(Base):
    def test_the_label_set_asks_the_internal_names_question(self):
        labels, task = classify.label_set("private-name")
        self.assertEqual(set(labels), {"private", "clean"})
        for name in private_names.PUBLIC_NAMES:
            self.assertIn(name, task)
        self.assertIn("OWN internal things", labels["private"])
        classify.check_labels(labels)

    def test_a_public_name_outside_the_allowlist_is_not_private(self):  # noqa: VACUOUS_ASSERTION — each equality pins a non-empty literal (the four kinds, the four vendors), so an empty result fails
        # the allowlist question defined private as "not in the list", so it
        # flagged public vendors, models and versions the list does not carry
        labels, _task = classify.label_set("private-name")
        kinds = ("products", "vendors", "AI models", "version numbers")
        verdict = labels["private"].partition(". ")[2]
        self.assertEqual([k for k in kinds if k in verdict], list(kinds))
        self.assertTrue(verdict.endswith("are NOT private."), verdict)
        vendors = {"Anthropic", "OpenRouter", "Cloudflare", "DeepSeek"}
        self.assertEqual(vendors - set(private_names.PUBLIC_NAMES), vendors,
                         "control: every vendor is outside the allowlist")
        negatives = [t for t, p in private_names.labelled_set() if not p]
        self.assertEqual({v for v in vendors if any(v in t for t in negatives)},
                         vendors)

    def test_the_host_adds_its_own_public_names(self):
        path = os.path.join(home.global_dir(), localnames.CONFIG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"public-names": ["acme-cli"]}, f)
        self.addCleanup(os.unlink, path)
        _labels, task = classify.label_set("private-name")
        self.assertIn("acme-cli", task)
        self.assertIn("helm", task)

    def test_eval_counts_hits_and_misses_at_the_threshold(self):
        positives = {t for t, p in private_names.labelled_set() if p}

        def post(_url, body, _timeout):
            p = 0.9 if body["text"] in positives else 0.1
            if body["text"].startswith("see "):
                p = 0.5                      # a miss below the threshold
            return 200, answer(label="private" if p > .5 else "clean",
                               scores={"private": p, "clean": 1 - p})
        res = classify.evaluate("private-name", post=post)
        n_pos = len(positives)
        missed = sum(1 for t in positives if t.startswith("see "))
        self.assertGreater(missed, 0, "control: the fixture plants misses")
        self.assertEqual((res["tp"], res["fn"], res["fp"], res["unanswered"]),
                         (n_pos - missed, missed, 0, 0))
        self.assertEqual(res["tn"], len(private_names.labelled_set()) - n_pos)

    def test_the_labelled_set_covers_every_listed_name(self):  # noqa: VACUOUS_ASSERTION — `covered` must equal the full NAMES set and the negatives count is pinned at 33; `named` being empty is the contract
        rows = private_names.labelled_set()
        covered = {name for name in private_names.NAMES
                   if any(p and private_names.hits_in(t, (name,))
                          for t, p in rows)}
        self.assertEqual(covered, set(private_names.NAMES))
        named = [t for t, p in rows if not p and private_names.hits_in(t)]
        self.assertEqual(named, [], "a neutral line names a listed name")
        self.assertEqual(sum(1 for _t, p in rows if not p), 18 + 15)


if __name__ == "__main__":
    unittest.main()
