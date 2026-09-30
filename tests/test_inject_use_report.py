#!/usr/bin/env python3
"""`helm inject --use-report` — whether a seat USED what the fire ledger says
fired, read from the seat's own transcript, with UNKNOWN kept apart from
unused.

The fire ledger records delivery: which ids reached which session's turn. A
fire is USED when the assistant records of that same turn (text, thinking or
tool input, up to the next prompt) name the id, or carry at least two of the
entry's rare store keywords. The two signals are reported separately because
they differ in trust. A fire whose transcript cannot be found or read is
UNKNOWN, never unused, and stays out of the rate's denominator.

Fixtures: ledger rows in a temp HELM_HOME, transcripts in a temp
HELM_CLAUDE_DIR (or under a row's own config home), store entries in the temp
store. Nothing here reads the operator's real ledger, store or transcripts.
"""
import calendar
import hashlib
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_inject import InjectBase  # noqa: E402
from helm import inject  # noqa: E402


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def iso_ms(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + \
        ".%03dZ" % int(round((t % 1) * 1000) % 1000)


def prompt(t, text="please carry on"):
    return {"type": "user", "isSidechain": False, "timestamp": iso_ms(t),
            "message": {"role": "user", "content": text}}


def hook(t, event, content="PREMISE some-rule: a line"):
    return {"type": "attachment", "isSidechain": False, "timestamp": iso_ms(t),
            "attachment": {"type": "hook_success",
                           "hookEvent": "UserPromptSubmit",
                           "hookName": "UserPromptSubmit",
                           "toolUseID": event, "content": content}}


def said(t, text):
    return {"type": "assistant", "isSidechain": False, "timestamp": iso_ms(t),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def thought(t, text):
    return {"type": "assistant", "isSidechain": False, "timestamp": iso_ms(t),
            "message": {"role": "assistant",
                        "content": [{"type": "thinking", "thinking": text,
                                     "signature": "sig"}]}}


def acted(t, args):
    return {"type": "assistant", "isSidechain": False, "timestamp": iso_ms(t),
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_1",
                                     "name": "Bash", "input": args}]}}


def result(t, text):
    return {"type": "user", "isSidechain": False, "timestamp": iso_ms(t),
            "toolUseResult": {"stdout": text},
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": "toolu_1",
                                     "content": text}]}}


def meta(t, text):
    return {"type": "user", "isSidechain": False, "isMeta": True,
            "timestamp": iso_ms(t),
            "message": {"role": "user", "content": text}}


def queued(t, text="a message arrived"):
    return {"type": "attachment", "isSidechain": False, "timestamp": iso_ms(t),
            "attachment": {"type": "queued_command", "prompt": text}}


def turn(t, event, *replies):
    """One prompt event: the prompt record, then the hook record the harness
    writes when UserPromptSubmit completes, then whatever the seat did."""
    return [prompt(t - 0.4), hook(t + 0.3, event)] + list(replies)


class UseBase(InjectBase):
    SID = "0f0f0f0f-aaaa-4bbb-8ccc-000000000001"

    def setUp(self):
        self.claude_prior = os.environ.get("HELM_CLAUDE_DIR")
        super().setUp()
        os.environ["HELM_CLAUDE_DIR"] = os.path.join(self.tmp, "claude")
        self.T = int(time.time()) - 3000
        self.rows = []

    def tearDown(self):
        if self.claude_prior is None:
            os.environ.pop("HELM_CLAUDE_DIR", None)
        else:
            os.environ["HELM_CLAUDE_DIR"] = self.claude_prior
        super().tearDown()

    def fire(self, t, jit=(), pinned=(), reflex=(), session=None, jit_bytes=120):
        row = {"v": 1, "ts": iso(t),
               "fired": {"pinned": list(pinned), "jit": list(jit),
                         "reflex": list(reflex)},
               "bytes": {"pinned": 90 * bool(pinned),
                         "jit": jit_bytes * bool(jit),
                         "reflex": 60 * bool(reflex)}}
        if session is not False:
            row["session"] = session or self.SID
        self.rows.append(row)
        return row

    def plant_ledger(self):
        path = inject._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# helm-inject-ledger protocol=1 generation=%s\n" %
                    ("3" * 32))
            f.writelines(json.dumps(r) + "\n" for r in
                         sorted(self.rows, key=lambda r: r["ts"]))

    def transcript(self, records, session=None, root=None, mtime=None,
                   close=True):
        """Write a session transcript. `close` appends a later prompt event so
        the last fired turn is delimited; `mtime` backdates the file."""
        sid = session or self.SID
        root = root or os.environ["HELM_CLAUDE_DIR"]
        d = os.path.join(root, "projects", "-fixture-project")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        recs = list(records)
        if close:
            last = max(calendar.timegm(time.strptime(
                r["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")) for r in recs)
            recs += turn(last + 30, "ev-close", said(last + 31, "closing"))
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                r = dict(r, sessionId=sid)
                f.write(json.dumps(r) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def report(self, **kw):
        self.plant_ledger()
        kw.setdefault("hours", 24)
        kw.setdefault("min_fires", 1)
        return inject.use_report(**kw)

    @staticmethod
    def by_id(r):
        return {i["id"]: i for i in r["ids"]}


class UseSignalTest(UseBase):
    def test_an_id_named_in_its_own_turn_is_used_and_an_unnamed_one_is_not(self):
        T = self.T
        self.fire(T, jit=["alpha-rule", "beta-rule"])
        self.transcript(turn(T, "ev1", said(T + 2, "Per alpha-rule, I measure first.")))
        r = self.report()
        got = self.by_id(r)
        a, b = got["alpha-rule"], got["beta-rule"]
        self.assertEqual((a["fires"], a["known"], a["named"], a["used"]),
                         (1, 1, 1, 1))
        self.assertEqual(a["use_rate"], 1.0)
        self.assertEqual((b["fires"], b["known"], b["named"], b["keyword"],
                          b["used"]), (1, 1, 0, 0, 0))
        self.assertEqual(b["use_rate"], 0.0)
        self.assertEqual((r["fires"], r["known"], r["unknown"]), (2, 2, 0))

    def test_thinking_tool_input_and_the_typed_form_all_name_an_id(self):
        T = self.T
        self.fire(T, jit=["gamma-rule", "zeta-rule"], pinned=["delta-rule"],
                  reflex=["eps-rule"])
        self.transcript(turn(
            T, "ev1",
            thought(T + 1, "gamma-rule applies to this change"),
            acted(T + 2, {"command": "helm store get prior:delta-rule"}),
            result(T + 3, "ok"),
            said(T + 4, "the zeta-rules-extra table is unrelated")))
        got = self.by_id(self.report())
        self.assertEqual(got["gamma-rule"]["named"], 1, "thinking names it")
        self.assertEqual(got["delta-rule"]["named"], 1,
                         "the type:id form inside a tool input names it")
        # an id is a whole token: a longer slug that starts with it is not it
        self.assertEqual(got["zeta-rule"]["named"], 0)
        self.assertEqual(got["eps-rule"]["used"], 0)
        self.assertEqual(sorted(got["delta-rule"]["lanes"]), ["pinned"])
        self.assertEqual(sorted(got["eps-rule"]["lanes"]), ["reflex"])

    def test_two_rare_keywords_are_a_keyword_use_reported_apart_from_naming(self):
        # "deploy" is carried by five entries, so it is not rare; the other
        # keywords each belong to one entry.
        for n in range(4):
            self.plant_jit("filler-%d" % n, "filler %d" % n, "deploy")
        self.plant_jit("kw-pair", "a paired rule", "zebrafish, quasar, deploy")
        self.plant_jit("kw-single", "a lone rule", "narwhal, obsidian")
        self.plant_jit("kw-common", "a common rule", "deploy, marmoset")
        T = self.T
        self.fire(T, jit=["kw-pair", "kw-single", "kw-common"])
        self.transcript(turn(T, "ev1", said(
            T + 1, "The zebrafish quasars deploy; a narwhal and a marmoset.")))
        got = self.by_id(self.report())
        self.assertIn("kw-pair", got)
        pair, single, common = (got["kw-pair"], got["kw-single"],
                                got["kw-common"])
        self.assertGreater(pair["keyword"], 0)
        self.assertEqual((pair["named"], pair["keyword"], pair["used"]),
                         (0, 1, 1), "two rare keywords (one inflected)")
        self.assertEqual((single["keyword"], single["used"]), (0, 0),
                         "one rare keyword is not enough")
        self.assertEqual((common["keyword"], common["used"]), (0, 0),
                         "a keyword five entries share is not rare")


class TurnBoundaryTest(UseBase):
    def test_a_mention_after_the_next_prompt_does_not_count(self):
        T = self.T
        self.fire(T, jit=["later-rule"])
        self.fire(T + 60, jit=["later-rule"])
        self.transcript(
            turn(T, "ev1", said(T + 1, "working on it"))
            + turn(T + 60, "ev2", said(T + 61, "later-rule now applies")))
        rule = self.by_id(self.report())["later-rule"]
        # the second fire's own turn names it; the first fire's turn does not
        self.assertEqual((rule["fires"], rule["known"], rule["used"]), (2, 2, 1))
        self.assertEqual(rule["use_rate"], 0.5)

    def test_stop_hook_feedback_does_not_end_the_turn(self):
        T = self.T
        self.fire(T, jit=["meta-rule"])
        self.transcript(turn(
            T, "ev1", said(T + 1, "first pass"),
            meta(T + 2, "Stop hook feedback: keep going"),
            said(T + 3, "per meta-rule the claim needs a number")))
        rule = self.by_id(self.report())["meta-rule"]
        self.assertGreater(rule["named"], 0)
        self.assertEqual((rule["known"], rule["named"]), (1, 1))

    def test_a_queued_arrival_ends_the_running_turn(self):
        T = self.T
        self.fire(T, jit=["queue-rule"])
        self.fire(T + 6, jit=["queue-rule"])
        self.transcript(turn(
            T, "ev1", acted(T + 1, {"command": "sleep 5"}), result(T + 5, "ok"),
            queued(T + 3), hook(T + 6.2, "ev2"),
            said(T + 7, "queue-rule noted for the new message")))
        rule = self.by_id(self.report())["queue-rule"]
        self.assertGreater(rule["used"], 0)
        self.assertEqual((rule["fires"], rule["known"], rule["used"]), (2, 2, 1))

    def test_an_open_turn_at_the_end_of_a_live_transcript_is_unknown(self):
        T = self.T
        self.fire(T, jit=["open-rule"])
        path = self.transcript(turn(T, "ev1", said(T + 1, "still going")),
                               close=False)
        r = self.report()
        rule = self.by_id(r)["open-rule"]
        self.assertEqual((rule["fires"], rule["known"], rule["unknown"]),
                         (1, 0, 1))
        self.assertEqual(r["unknown_reasons"], {"turn-open": 1})
        # CONTROL: the same bytes, written long ago, end a finished session
        os.utime(path, (T + 100, T + 100))
        rule = self.by_id(inject.use_report(hours=24, min_fires=1))["open-rule"]
        self.assertEqual((rule["known"], rule["unknown"], rule["used"]),
                         (1, 0, 0))

    def test_a_turn_the_seat_never_answered_is_unknown_not_unused(self):
        # a burst: the second prompt event's hook lands before the seat says
        # anything, so the seat answers both events in one reply. The first
        # event's own turn holds no assistant record at all: nothing in it
        # can show use or non-use, so its fire is UNKNOWN, never unused.
        # (Measured live: 27 of 346 finished turns in a 3 h window, mostly
        # hook, queued, hook.)
        T = self.T
        self.fire(T, jit=["burst-first"])
        self.fire(T + 2, jit=["burst-second"])
        self.transcript([prompt(T - 0.4), hook(T + 0.3, "ev1"), queued(T + 1),
                         hook(T + 2.3, "ev2"),
                         said(T + 3, "burst-second and burst-first apply")])
        r = self.report()
        got = self.by_id(r)
        self.assertGreater(got["burst-second"]["used"], 0)
        self.assertGreater(r["unknown"], 0)
        self.assertEqual((got["burst-second"]["known"],
                          got["burst-second"]["used"]), (1, 1))
        first = got["burst-first"]
        self.assertEqual((first["fires"], first["known"], first["unknown"]),
                         (1, 0, 1), "an unanswered turn is not unused")
        self.assertIsNone(first["use_rate"])
        self.assertEqual(r["unknown_reasons"], {"no-reply": 1})
        self.assertNotIn("burst-first", [p["id"] for p in r["prune"]])

    def test_a_prompt_written_just_after_its_own_hook_stays_in_the_event(self):
        # a task notification's prompt record is written 1-3 ms AFTER the
        # UserPromptSubmit hook record of its own event (measured: 144 of
        # 14,332 hook events). It is the anchor's own prompt, not the next
        # prompt event, so the turn goes on to the seat's reply.
        T = self.T
        self.fire(T, jit=["notify-rule"])
        self.transcript([hook(T + 0.3, "ev1"),
                         prompt(T + 0.302, "<task-notification>done"
                                "</task-notification>"),
                         said(T + 1, "notify-rule applies to the result")])
        r = self.report()
        rule = self.by_id(r)["notify-rule"]
        self.assertGreater(rule["named"], 0)
        self.assertEqual((rule["known"], rule["named"], rule["used"]),
                         (1, 1, 1))
        self.assertEqual((r["fires"], r["known"], r["unknown"]), (1, 1, 0))

    def test_two_fires_in_one_second_each_take_their_own_event(self):
        # two prompt events in one second write two rows with the same `ts`
        # (measured: 92 of 2,492 fired rows). The second row must not take
        # the hook record the first row already took.
        T = self.T
        self.fire(T, jit=["one-second-a"])
        self.fire(T, jit=["one-second-b"])
        self.transcript([prompt(T + 0.1), hook(T + 0.2, "ev1"),
                         said(T + 0.4, "one-second-a applied"),
                         prompt(T + 0.6), hook(T + 0.7, "ev2"),
                         said(T + 1.5, "one-second-b applied")])
        got = self.by_id(self.report())
        self.assertGreater(got["one-second-b"]["used"], 0)
        self.assertEqual((got["one-second-a"]["known"],
                          got["one-second-a"]["used"]), (1, 1))
        self.assertEqual((got["one-second-b"]["known"],
                          got["one-second-b"]["used"]), (1, 1),
                         "the second row reads its own event's turn")


class UnknownIsNotUnusedTest(UseBase):
    def test_a_missing_transcript_is_unknown_and_leaves_the_denominator(self):
        T = self.T
        other = "0f0f0f0f-aaaa-4bbb-8ccc-00000000dead"
        self.fire(T, jit=["gone-rule", "only-gone"], session=other)
        self.fire(T + 10, jit=["gone-rule"])
        self.transcript(turn(T + 10, "ev1", said(T + 11, "nothing named")))
        r = self.report()
        got = self.by_id(r)
        gone, only = got["gone-rule"], got["only-gone"]
        self.assertEqual((gone["fires"], gone["known"], gone["unknown"],
                          gone["used"]), (2, 1, 1, 0))
        self.assertEqual(gone["use_rate"], 0.0)
        self.assertGreater(only["unknown"], 0)
        self.assertEqual((only["fires"], only["known"], only["unknown"]),
                         (1, 0, 1))
        self.assertIsNone(only["use_rate"], "no readable fire: no rate")
        self.assertEqual((r["unknown"], r["unknown_reasons"]),
                         (2, {"no-transcript": 2}))
        pruned = [p["id"] for p in r["prune"]]
        self.assertIn("gone-rule", pruned, "must-hit: min_fires=1 prunes "
                      "the id with one readable unused fire")
        self.assertNotIn("only-gone", pruned, "UNKNOWN is not evidence")

    def test_a_fire_without_a_session_is_unknown(self):
        T = self.T
        self.fire(T, jit=["loose-rule"], session=False)
        self.fire(T + 10, jit=["loose-rule"])
        self.transcript(turn(T + 10, "ev1", said(T + 11, "loose-rule seen")))
        r = self.report()
        rule = self.by_id(r)["loose-rule"]
        self.assertEqual((rule["fires"], rule["known"], rule["used"]), (2, 1, 1))
        self.assertEqual(r["unknown_reasons"], {"no-session": 1})

    def test_a_row_without_a_home_finds_its_transcript_in_a_seat_home(self):
        # a row whose runtime context was unavailable records no config home;
        # helm's own seat homes (a family home and an instance home) are
        # searched beside the harness default, by session id in the filename
        T = self.T
        seats = os.path.join(os.environ["HELM_HOME"], "_global", "seats")
        other = "0f0f0f0f-aaaa-4bbb-8ccc-0000000000b2"
        self.fire(T, jit=["seat-rule"])
        self.fire(T + 10, jit=["seat-rule"], session=other)
        self.transcript(turn(T, "ev1", said(T + 1, "seat-rule holds")),
                        root=os.path.join(seats, "fam-a", "claude"))
        self.transcript(turn(T + 10, "ev2", said(T + 11, "seat-rule too")),
                        session=other,
                        root=os.path.join(seats, "fam-b", "instances",
                                          "seat-b", "claude"))
        r = self.report()
        rule = self.by_id(r)["seat-rule"]
        self.assertEqual((rule["fires"], rule["known"], rule["named"]), (2, 2, 2))
        self.assertEqual(r["read"]["transcripts"], 2)

    def test_two_transcripts_for_one_session_are_unlocatable(self):
        T = self.T
        seats = os.path.join(os.environ["HELM_HOME"], "_global", "seats")
        self.fire(T, jit=["twin-rule"])
        self.fire(T + 10, jit=["twin-rule"],
                  session="0f0f0f0f-aaaa-4bbb-8ccc-0000000000b3")
        self.transcript(turn(T + 10, "ev2", said(T + 11, "twin-rule")),
                        session="0f0f0f0f-aaaa-4bbb-8ccc-0000000000b3")
        for fam in ("fam-a", "fam-b"):
            self.transcript(turn(T, "ev1", said(T + 1, "twin-rule")),
                            root=os.path.join(seats, fam, "claude"))
        r = self.report()
        rule = self.by_id(r)["twin-rule"]
        self.assertEqual((rule["fires"], rule["known"], rule["unknown"]),
                         (2, 1, 1))
        self.assertEqual(r["unknown_reasons"], {"unlocatable": 1})

    def test_the_rows_own_config_home_locates_the_transcript(self):
        T = self.T
        home_dir = os.path.join(self.tmp, "seat-home")
        row = {"v": 2, "ts": iso(T), "project": None, "session": self.SID,
               "fired": {"pinned": [], "jit": ["home-rule"], "reflex": []},
               "sample": {"encoding": "utf-8", "rendered_bytes": 101,
                          "lane_bytes": {"whisper": 0, "pinned": 0,
                                         "jit": 100, "reflex": 0}},
               "context": {"session": self.SID, "cwd": self.tmp,
                           "harness": "claude", "config_home": home_dir},
               "context_sources": {"session": "hook-json", "cwd": "hook-json",
                                   "harness": "HELM_AGENT_HARNESS",
                                   "config_home": "CLAUDE_CONFIG_DIR"}}
        self.rows.append(row)
        self.transcript(turn(T, "ev1", said(T + 1, "home-rule holds")),
                        root=home_dir)
        rule = self.by_id(self.report())["home-rule"]
        self.assertEqual((rule["known"], rule["named"]), (1, 1))
        self.assertEqual(rule["bytes"], 100, "the ledger's own lane bytes")


class PruneTest(UseBase):
    def build(self, plan):
        """plan: [(id, fires, named)] — one prompt event per fire, the first
        `named` of them naming the id."""
        T, recs, n = self.T, [], 0
        for rid, fires, named in plan:
            for k in range(fires):
                t = T + 40 * n
                self.fire(t, jit=[rid])
                text = ("%s applied" % rid) if k < named else "carry on"
                recs += turn(t, "ev%d" % n, said(t + 1, text))
                n += 1
        self.transcript(recs)

    def test_prune_applies_the_fire_floor_and_the_rate_ceiling(self):
        self.build([("noisy-rule", 4, 0), ("heeded-rule", 4, 1),
                    ("sparse-rule", 2, 0)])
        r = self.report(min_fires=3)
        self.assertEqual([p["id"] for p in r["prune"]], ["noisy-rule"])
        noisy = r["prune"][0]
        self.assertEqual((noisy["fires"], noisy["known"], noisy["used"]),
                         (4, 4, 0))
        self.assertEqual(len(noisy["examples"]), 3)
        stamps = {row["ts"] for row in self.rows}
        seen = [(ex["session"], ex["ts"]) for ex in noisy["examples"]]
        self.assertEqual(sorted({s for s, _ts in seen}), [self.SID])
        self.assertEqual(len(seen), 3)
        self.assertEqual([ts for _s, ts in seen if ts not in stamps], [])
        # the default floor is 20 readable fires
        floor = inject.use_report(hours=24)
        self.assertGreater(floor["known"], 0)
        self.assertEqual(floor["prune"], [])

    def test_a_pinned_candidate_says_it_is_an_owner_rule(self):
        # a pinned rule rides every prompt and is followed without being
        # named, so it reaches the prune list on naming alone; the report
        # says it is owner canon, so nobody retires it on this report
        T = self.T
        recs = []
        for k in range(3):
            t = T + 40 * k
            self.fire(t, pinned=["canon-rule"], jit=["plain-rule"])
            recs += turn(t, "ev%d" % k, said(t + 1, "carry on"))
        self.transcript(recs)
        r = self.report(min_fires=3)
        got = {p["id"]: p for p in r["prune"]}
        self.assertEqual(sorted(got), ["canon-rule", "plain-rule"])
        self.assertIs(got["canon-rule"]["owner_rule"], True)
        self.assertIs(got["plain-rule"]["owner_rule"], False)
        rc, out, err = self.run_inject(["--use-report", "--min-fires", "3"])
        self.assertEqual((rc, err), (0, ""))
        canon = [line for line in out.splitlines()
                 if line.lstrip().startswith("canon-rule [")]
        plain = [line for line in out.splitlines()
                 if line.lstrip().startswith("plain-rule [")]
        self.assertEqual(len(canon), 1)
        self.assertIn("owner rule", canon[0])
        self.assertEqual(len(plain), 1)
        self.assertNotIn("owner rule", plain[0])

    def test_the_rate_ceiling_is_strict(self):
        # 1 of 20 is exactly 5% (kept); 1 of 21 is under it (pruned)
        self.build([("edge-rule", 20, 1), ("under-rule", 21, 1)])
        r = self.report(min_fires=20)
        self.assertEqual([p["id"] for p in r["prune"]], ["under-rule"])


class ShapeTest(UseBase):
    def plant(self):
        T = self.T
        self.fire(T, jit=["shape-rule"])
        self.fire(T + 20, jit=["shape-rule"], session="0f0f0f0f-aaaa-4bbb-8ccc-0000000000ff")
        self.transcript(turn(T, "ev1", said(T + 1, "carry on")))
        self.plant_ledger()

    def test_json_shape(self):
        self.plant()
        rc, out, err = self.run_inject(["--use-report", "--json", "--hours",
                                        "24", "--min-fires", "1"])
        self.assertEqual((rc, err), (0, ""))
        r = json.loads(out)
        self.assertEqual(set(r), {
            "window_hours", "since", "until", "ledger_complete",
            "ledger_since", "rows",
            "fires", "known", "unknown", "unknown_reasons", "read",
            "min_fires", "max_use_rate", "rule", "ids", "prune"})
        self.assertEqual(set(r["read"]), {"bytes", "transcripts", "caps",
                                          "seconds"})
        self.assertEqual(set(r["read"]["caps"]),
                         {"turn", "transcript", "total"})
        self.assertEqual(set(r["rule"]), {"named", "keyword"})
        self.assertEqual(set(r["ids"][0]), {
            "id", "lanes", "fires", "sessions", "known", "unknown", "named",
            "keyword", "used", "use_rate", "bytes"})
        self.assertEqual(set(r["prune"][0]), {
            "id", "lanes", "owner_rule", "fires", "known", "used", "use_rate",
            "bytes", "examples"})
        self.assertEqual(r["prune"][0]["lanes"], ["jit"])
        self.assertEqual(set(r["prune"][0]["examples"][0]), {"session", "ts"})
        self.assertEqual((r["fires"], r["known"], r["unknown"], r["min_fires"],
                          r["ledger_complete"]), (2, 1, 1, 1, True))
        self.assertEqual(r["ids"][0]["sessions"], 2)
        self.assertGreater(r["read"]["bytes"], 0)

    def test_text_names_unknown_and_what_was_read(self):
        self.plant()
        rc, out, err = self.run_inject(["--use-report", "--min-fires", "1"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("UNKNOWN 1", out)
        self.assertIn("shape-rule", out)
        self.assertRegex(out, r"read [0-9.]+ [KM]?B from 1 transcript")
        self.assertIn("never unused", out)

    def test_a_window_wider_than_the_ledger_says_how_far_back_it_reaches(self):
        self.plant()
        rc, out, err = self.run_inject(["--use-report", "--hours", "48",
                                        "--min-fires", "1"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("the ledger reaches back only to %s" % iso(self.T), out)
        r = inject.use_report(hours=48, min_fires=1)
        self.assertGreater(r["fires"], 0)
        self.assertEqual(r["ledger_since"], iso(self.T))
        # CONTROL: the oldest row is 50 minutes old, so a half-hour window
        # is one the ledger covers, and it carries no such clause
        rc, out, err = self.run_inject(["--use-report", "--hours", "0.5",
                                        "--min-fires", "1"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("last 0.5h", out)
        self.assertNotIn("reaches back only", out)

    def test_bad_values_refuse(self):
        self.plant()
        rc, out, err = self.run_inject(["--use-report", "--min-fires", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("shape-rule", out)
        for argv in (["--use-report", "--hours", "x"],
                     ["--use-report", "--hours", "0"],
                     ["--use-report", "--hours", "1e300"],
                     ["--use-report", "--min-fires", "0"],
                     ["--use-report", "--min-fires"],
                     # the report's own flags without the report: before the
                     # lane these were unknown flags (rc 2); they must not
                     # turn into a live inject of the prompt text "24"
                     ["--hours", "24"],
                     ["--min-fires", "3"]):
            rc, out, err = self.run_inject(argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("helm inject", err)

    def test_every_report_subflag_refuses_without_its_report(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal six-row tuple, so every assertion runs
        """THE SAME CLASS ONE REPORT OVER. The known-flag list admits each
        report's own flags on their own, so without their report they fell
        through to a live inject whose prompt text was the flag's value."""
        for flag, value, parent in (("--days", "7", "--moment-report"),
                                    ("--gold", "g.jsonl", "--replay"),
                                    ("--labels", "l.jsonl", "--replay"),
                                    ("--limit", "5", "--replay"),
                                    ("--out", "o.json", "--replay"),
                                    ("--recorded", None, "--replay")):
            argv = [flag] + ([value] if value else [])
            rc, out, err = self.run_inject(argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("needs %s" % parent, err, argv)

    def test_the_window_excludes_older_fires(self):
        old = self.T - 30 * 3600
        self.fire(old, jit=["old-rule"])
        self.transcript(turn(old, "ev1", said(old + 1, "old-rule")))
        self.assertNotIn("old-rule", self.by_id(self.report(hours=24)))
        self.assertIn("old-rule", self.by_id(self.report(hours=48)))


class ReadOnlyTest(UseBase):
    @staticmethod
    def snapshot(*roots):
        out = {}
        for root in roots:
            for d, _dirs, files in os.walk(root):
                for n in files:
                    p = os.path.join(d, n)
                    with open(p, "rb") as f:
                        out[p] = hashlib.sha256(f.read()).hexdigest()
        return out

    def test_the_store_ledger_and_transcripts_are_untouched(self):
        self.plant_jit("ro-rule", "a read-only rule", "zebrafish, quasar")
        T = self.T
        self.fire(T, jit=["ro-rule"])
        self.transcript(turn(T, "ev1", said(T + 1, "ro-rule zebrafish quasar")))
        self.plant_ledger()
        # the cache dir is a root too: the parsed-entry cache is a state file
        # the per-turn hook writes, and a read-only report must not
        stores = [os.environ["HELM_ADOPTED_DIR"],
                  os.path.join(os.environ["HELM_HOME"], "_global"),
                  os.environ["HELM_CACHE_DIR"]]
        before = self.snapshot(os.environ["HELM_CLAUDE_DIR"], *stores)
        self.assertTrue(any(p.endswith("ro-rule.md") or "ro-rule" in p
                            for p in before), "must-hit: the entry is on disk")
        rc, out, err = self.run_inject(["--use-report", "--min-fires", "1"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("ro-rule", out)
        after = self.snapshot(os.environ["HELM_CLAUDE_DIR"], *stores)
        self.assertGreater(len(after), 1)
        self.assertTrue(os.path.exists(inject._ledger_path()))
        # the ledger reader may create its own empty lock beside the ledger;
        # every file that existed before is byte-identical, and nothing else
        # appeared in the store roots or the transcript tree
        self.assertEqual({p: h for p, h in after.items() if p in before},
                         before)
        new = sorted(set(after) - set(before))
        self.assertEqual([p for p in new
                          if not p.endswith("inject-ledger.jsonl.lock")], [])
        self.assertFalse(os.path.exists(inject._seen_dir()))
        self.assertFalse(os.path.exists(inject._coinage_path()))


class CostTest(UseBase):
    def test_a_fire_late_in_a_long_transcript_reads_only_near_it(self):
        T = self.T
        start = T - 2 * 3600
        recs = []
        for k in range(3000):
            t = start + 2 * k
            recs += turn(t, "filler-%d" % k,
                         said(t + 1, "filler turn %d " % k + "x" * 400))
        self.fire(T, jit=["late-rule"])
        recs += turn(T, "ev-late", said(T + 1, "late-rule applies"))
        path = self.transcript(recs)
        size = os.path.getsize(path)
        self.assertGreater(size, 1048576, "must-hit: the fixture is long")
        r = self.report()
        self.assertGreater(r["read"]["bytes"], 0)
        self.assertEqual(self.by_id(r)["late-rule"]["named"], 1)
        self.assertLess(r["read"]["bytes"], size // 4,
                        "the reader must seek to the fire, not stream the file")

    def test_a_turn_past_the_turn_cap_is_unknown_unless_a_use_came_first(self):
        use = inject._use
        T = self.T
        self.fire(T, jit=["early-rule", "deep-rule"])
        self.transcript(turn(
            T, "ev1", said(T + 1, "early-rule first"),
            result(T + 2, "y" * 20000),
            said(T + 3, "deep-rule only after the big result")))
        with mock.patch.object(use, "TURN_CAP", 8192):
            r = self.report()
        got = self.by_id(r)
        self.assertEqual((got["early-rule"]["known"], got["early-rule"]["used"]),
                         (1, 1), "a use seen before the cap is a use")
        self.assertEqual((got["deep-rule"]["known"], got["deep-rule"]["unknown"]),
                         (0, 1), "an unread remainder is not unused")
        self.assertEqual(r["unknown_reasons"], {"turn-cap": 1})

    def test_light_transcripts_leave_the_report_budget_to_heavy_ones(self):
        # the report budget is shared out lightest-first: a transcript with
        # few fires cannot hold back bytes a heavy one needs
        use = inject._use
        T = self.T
        heavy = []
        for k in range(12):
            t = T + 30 * k
            self.fire(t, jit=["heavy-rule"])
            heavy += turn(t, "ev%d" % k, said(t + 1, "heavy-rule " + "h" * 3000))
        self.transcript(heavy)
        for n in range(3):
            sid = "0f0f0f0f-aaaa-4bbb-8ccc-00000000010%d" % n
            self.fire(T + 5, jit=["light-rule"], session=sid)
            self.transcript(turn(T + 5, "ev-l", said(T + 6, "light-rule")),
                            session=sid)
        with mock.patch.object(use, "TOTAL_CAP", 64 * 1024):
            r = self.report()
        self.assertGreater(r["read"]["bytes"], 0)
        got = self.by_id(r)
        self.assertEqual(got["light-rule"]["known"], 3)
        self.assertEqual((got["heavy-rule"]["known"], got["heavy-rule"]["used"]),
                         (12, 12), "an even split would have cut this one")
        self.assertEqual(r["unknown"], 0)

    def test_a_heavy_transcript_is_not_cut_to_an_even_split(self):
        # three transcripts, read lightest first: the middle one needs more
        # than half of what is left after the first, and the last needs
        # little, so an even split of the remainder would cut the middle one
        use = inject._use
        T = self.T
        mid, last = ("0f0f0f0f-aaaa-4bbb-8ccc-0000000002%02d" % k
                     for k in (1, 2))
        self.fire(T, jit=["one-rule"],
                  session="0f0f0f0f-aaaa-4bbb-8ccc-000000000200")
        self.transcript(turn(T, "ev-a", said(T + 1, "one-rule")),
                        session="0f0f0f0f-aaaa-4bbb-8ccc-000000000200")
        recs = []
        for k in range(10):
            t = T + 30 * k
            self.fire(t, jit=["mid-rule"], session=mid)
            recs += turn(t, "ev%d" % k, said(t + 1, "mid-rule " + "m" * 3500))
        self.transcript(recs, session=mid)
        recs = []
        for k in range(12):
            t = T + 30 * k
            self.fire(t, jit=["last-rule"], session=last)
            recs += turn(t, "ev%d" % k, said(t + 1, "last-rule"))
        self.transcript(recs, session=last)
        with mock.patch.object(use, "TOTAL_CAP", 64 * 1024):
            r = self.report()
        self.assertGreater(r["read"]["bytes"], 0)
        got = self.by_id(r)
        self.assertEqual(got["mid-rule"]["known"], 10)
        self.assertEqual(got["last-rule"]["known"], 12)
        self.assertEqual(r["unknown"], 0)

    def test_the_transcript_cap_bounds_the_bytes_read(self):
        use = inject._use
        T = self.T
        recs = []
        for k in range(40):
            t = T + 30 * k
            self.fire(t, jit=["capped-rule"])
            recs += turn(t, "ev%d" % k, said(t + 1, "carry on " + "z" * 2000))
        self.transcript(recs)
        with mock.patch.object(use, "TRANSCRIPT_CAP", 16384):
            r = self.report()
        rule = self.by_id(r)["capped-rule"]
        self.assertEqual(rule["fires"], 40)
        self.assertGreater(rule["known"], 0, "must-hit: some turns were read")
        self.assertGreater(rule["unknown"], 0)
        self.assertEqual(set(r["unknown_reasons"]), {"read-cap"})
        # one record past the cap at most: the cap stops the next read
        self.assertLess(r["read"]["bytes"], 16384 + 8192)


if __name__ == "__main__":
    unittest.main()
