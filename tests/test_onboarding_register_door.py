"""The onboarding register: one writer, one reader door, one closed result.

The subject of this file is a SCHEMA, not a feature. THREE fields — an
outcome, the refusal text and the moment it was observed — are written together
by one function in one write and read back by one parser, and every consumer
downstream reads the parser's closed result rather than the raw fields. A
record carrying only some of them was not written by that writer. The arms
below exist because the opposite shape — the fields travelling as INDEPENDENT
optionals — lets each reader invent its own answer to "what does a missing
part mean", and those answers do not agree.

So the load-bearing arms are not the happy paths. They are:

  * every malformed shape is INVALID and NAMES why (never silently absent),
  * ABSENT and VALID leave every existing ladder verdict EXACTLY as it was,
  * no INVALID case can reach an admissible takeover, at any claim age,
  * the door's copy claims only what it measured — the LATEST record — and
    never anything about a seat's lifetime.
"""
import ast
import inspect
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from helm import (
    harness,
    proxywatch,
    seat,
    seat_lifecycle_runtime as runtime,
    seat_usability,
    takeover,
)


STAMP = "2026-09-09T01:02:03Z"
#: The exact epoch `STAMP` denotes, computed here rather than transcribed so a
#: change to the format constant cannot leave this file quietly agreeing with
#: an old one.
STAMP_EPOCH = int(runtime.calendar.timegm(
    runtime.time.strptime(STAMP, runtime.ONBOARDING_STAMP_FMT)))
#: Far enough after the stamp that nothing under test is near the future bound.
LATER = STAMP_EPOCH + 86400

#: A spawn register as production writes one, minus the onboarding fields.
#: Frozen from the live shape observed on the fleet, with synthetic values.
LEGACY_RECORD = {
    "v": 1, "seat": "seat-under-test", "identity": "seat-under-test",
    "harness": "claude", "handle": "pane-0000", "pty_id": "pty-0000",
    "pane_key": "key-0000", "room": "room-under-test",
    "room_source": "explicit", "room_worktree": "/tmp/does-not-exist",
    "launch_sh": "/tmp/does-not-exist/launch.sh", "model": None,
    "role": None, "session": "session-0000", "session_pid": 4242,
    "session_pid_identity": "boot:4242", "ts": "2026-09-09T01:00:00Z",
    "worktree": "/tmp/does-not-exist", "worktree_id": "wt-0000",
}


def valid_record(outcome=None, proof="composer held helm's own text"):
    rec = dict(LEGACY_RECORD)
    rec["onboarding"] = harness.DELIVERED if outcome is None else outcome
    rec["onboarding_proof"] = proof
    rec["onboarding_at"] = STAMP
    return rec


#: EVERY malformed shape, in one place, so the takeover arm below can iterate
#: exactly the same population the parser arms assert on. A case added here is
#: automatically carried into the admissibility proof — which is the point: the
#: two must never be able to drift into different populations.
INVALID_RECORDS = (
    ("outcome absent", dict(LEGACY_RECORD, onboarding_at=STAMP)),
    ("outcome null", dict(LEGACY_RECORD, onboarding=None,
                          onboarding_at=STAMP)),
    ("outcome not a string", dict(LEGACY_RECORD, onboarding=1,
                                  onboarding_at=STAMP)),
    ("outcome unrecognised", dict(LEGACY_RECORD, onboarding="shipped",
                                  onboarding_at=STAMP)),
    ("stamp absent", dict(LEGACY_RECORD, onboarding=harness.DELIVERED)),
    ("stamp null", dict(LEGACY_RECORD, onboarding=harness.DELIVERED,
                        onboarding_at=None)),
    ("stamp not a string", dict(LEGACY_RECORD, onboarding=harness.DELIVERED,
                                onboarding_at=STAMP_EPOCH)),
    ("stamp unparseable", dict(LEGACY_RECORD, onboarding=harness.DELIVERED,
                               onboarding_at="yesterday")),
    ("stamp in another format", dict(LEGACY_RECORD,
                                     onboarding=harness.DELIVERED,
                                     onboarding_at="2026-09-09 01:02:03")),
    ("stamp in the future", dict(LEGACY_RECORD, onboarding=harness.DELIVERED,
                                 onboarding_at="2099-01-01T00:00:00Z")),
    ("proof alone", dict(LEGACY_RECORD, onboarding_proof="p")),
    # THE HALF-WRITTEN TRIPLE. The writer emits all three keys in one call, so
    # a record carrying two of them was not written by it.
    ("proof key missing", {k: v for k, v in valid_record().items()
                           if k != "onboarding_proof"}),
    ("stamp key missing, proof present",
     {k: v for k, v in valid_record().items() if k != "onboarding_at"}),
    # SPELLINGS strptime ACCEPTS AND THE WRITER CANNOT EMIT.
    ("stamp unpadded", dict(valid_record(), onboarding_at="2026-9-9T1:2:3Z")),
    ("stamp leap second",
     dict(valid_record(), onboarding_at="2026-09-09T01:02:60Z")),
)


class TheDoorParsesEveryShapeIntoOneClosedResult(unittest.TestCase):
    def test_a_record_without_the_fields_is_ABSENT_not_invalid(self):  # noqa: VACUOUS_ASSERTION — the planted proof-only record on the SAME parser returns INVALID — an unconditional must-hit on this exact probe
        """Legacy. Every register on a fleet that predates the field."""
        got = runtime.parse_onboarding(LEGACY_RECORD, now=LATER)
        self.assertEqual(got.kind, runtime.ONBOARDING_ABSENT)
        self.assertEqual(got, runtime.ONBOARDING_NONE)
        # MUST-HIT on the SAME probe: one added field moves the answer, so the
        # ABSENT above is a reading rather than an instrument that says ABSENT
        # to everything.
        self.assertEqual(
            runtime.parse_onboarding(dict(LEGACY_RECORD, onboarding_proof="p"),
                                     now=LATER).kind,
            runtime.ONBOARDING_INVALID)

    def test_a_missing_or_non_dict_record_is_ABSENT(self):  # noqa: VACUOUS_ASSERTION — the trailing VALID parse is an unconditional control on the same parser, and the population size is asserted before the loop
        """A failed LOOK is not a reading. It must change nothing."""
        cases = (None, [], "", 0)
        self.assertEqual(len(cases), 4, "the population went empty")
        for rec in cases:
            with self.subTest(rec=rec):
                self.assertEqual(runtime.parse_onboarding(rec, now=LATER).kind,
                                 runtime.ONBOARDING_ABSENT)
        self.assertEqual(runtime.parse_onboarding(valid_record(),
                                                  now=LATER).kind,
                         runtime.ONBOARDING_VALID)

    def test_each_recognised_outcome_parses_VALID_with_an_absolute_stamp(self):  # noqa: VACUOUS_ASSERTION — len(cases) is asserted unconditionally before the loop, so an emptied population fails rather than passes
        """proven is TRUE only for DELIVERED; the other two are recorded
        refusals, which are known-unproven and emphatically not unknown."""
        cases = ((harness.DELIVERED, True), (harness.NOT_DELIVERED, False),
                 (harness.UNKNOWN, False))
        self.assertEqual(len(cases), 3, "an outcome fell out of the fixture")
        for outcome, proven in cases:
            with self.subTest(outcome=outcome):
                got = runtime.parse_onboarding(valid_record(outcome),
                                               now=LATER)
                self.assertEqual(got.kind, runtime.ONBOARDING_VALID)
                self.assertIs(got.proven, proven)
                # AN ABSOLUTE MOMENT, NOT AN AGE. The door does no arithmetic
                # against `now`, so a suspend-aware age stays the one
                # consumer's job and cannot be baked in here wrongly.
                self.assertEqual(got.event_at, STAMP_EPOCH)
                self.assertEqual(got.proof, "composer held helm's own text")
                self.assertIsNone(got.reason)

    def test_an_empty_proof_reads_as_no_proof_rather_than_an_empty_sentence(self):
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, first: a real proof survives
        # the same call, so the None below is the empty string being read and
        # not the field being dropped.
        self.assertEqual(runtime.parse_onboarding(valid_record(proof="held"),
                                                  now=LATER).proof, "held")
        got = runtime.parse_onboarding(valid_record(proof=""), now=LATER)
        self.assertEqual(got.kind, runtime.ONBOARDING_VALID)
        self.assertIsNone(got.proof)

    def test_every_malformed_shape_is_INVALID_and_carries_NO_reading(self):  # noqa: VACUOUS_ASSERTION — len(INVALID_RECORDS) is asserted unconditionally before the loop, and every arm here asserts a POSITIVE kind and a non-empty reason
        """The closed-result invariant, asserted on the whole population.

        An INVALID that leaked a `proven` or an `event_at` would be exactly the
        half-read record this door exists to abolish — a consumer could act on
        the half and never look at the kind."""
        self.assertEqual(len(INVALID_RECORDS), 15,
                         "the malformed population changed size")
        for label, rec in INVALID_RECORDS:
            with self.subTest(case=label):
                got = runtime.parse_onboarding(rec, now=LATER)
                self.assertEqual(got.kind, runtime.ONBOARDING_INVALID)
                self.assertTrue(got.unreadable)
                self.assertFalse(got.readable)
                self.assertIsNone(got.proven)
                self.assertIsNone(got.event_at)
                self.assertIsInstance(got.reason, str)
                self.assertTrue(got.reason.strip())

    def test_the_THREE_KEYS_ARE_ONE_FACT_and_two_of_them_is_half_written(self):  # noqa: VACUOUS_ASSERTION — the full triple is asserted VALID first, unconditionally, so each INVALID below is the missing key and not a broken parser
        """An empty proof is a VALUE and stays VALID; a MISSING proof key is a
        record this writer cannot have produced."""
        self.assertEqual(runtime.parse_onboarding(valid_record(), now=LATER).kind,
                         runtime.ONBOARDING_VALID)
        self.assertEqual(runtime.parse_onboarding(valid_record(proof=""),
                                                  now=LATER).kind,
                         runtime.ONBOARDING_VALID)
        full = valid_record()
        for drop in ("onboarding", "onboarding_at", "onboarding_proof"):
            with self.subTest(missing=drop):
                rec = {k: v for k, v in full.items() if k != drop}
                got = runtime.parse_onboarding(rec, now=LATER)
                self.assertEqual(got.kind, runtime.ONBOARDING_INVALID)
                self.assertIn(drop, got.reason)

    def test_PARSEABLE_IS_NOT_THE_BAR_the_stamp_must_be_CANONICAL(self):  # noqa: VACUOUS_ASSERTION — the canonical spelling is asserted VALID first, unconditionally, so each INVALID below is the spelling and not a dead check
        """strptime is lenient: %d/%m/%H take one OR two digits and %S takes a
        leap second, so shapes this writer can never emit would otherwise
        parse. The round trip accepts exactly what strftime produces."""
        self.assertEqual(runtime.parse_onboarding(valid_record(),
                                                  now=LATER).kind,
                         runtime.ONBOARDING_VALID)
        for spelling in ("2026-9-9T1:2:3Z", "2026-09-09T1:02:03Z",
                         "2026-9-09T01:02:03Z", "2026-09-09T01:02:60Z"):
            with self.subTest(stamp=spelling):
                got = runtime.parse_onboarding(
                    dict(valid_record(), onboarding_at=spelling), now=LATER)
                self.assertEqual(got.kind, runtime.ONBOARDING_INVALID,
                                 "%r parsed" % (spelling,))
                self.assertIn("canonical", got.reason)
        # AND THE WRITER'S OWN OUTPUT IS ACCEPTED, which is what makes the
        # rule a spelling rule and not a stricter clock.
        stamp = runtime.time.strftime(runtime.ONBOARDING_STAMP_FMT,
                                      runtime.time.gmtime(STAMP_EPOCH))
        self.assertEqual(runtime.parse_onboarding(
            dict(valid_record(), onboarding_at=stamp), now=LATER).kind,
            runtime.ONBOARDING_VALID)

    def test_the_future_bound_is_the_grace_and_the_grace_is_the_bound(self):
        """Pin the BOUNDARY, not the direction. A rule tested only far from
        its edge is a rule whose edge nobody has read."""
        grace = runtime.ONBOARDING_FUTURE_GRACE_S
        at_bound = runtime.parse_onboarding(valid_record(),
                                            now=STAMP_EPOCH - grace)
        self.assertEqual(at_bound.kind, runtime.ONBOARDING_VALID)
        past_bound = runtime.parse_onboarding(valid_record(),
                                              now=STAMP_EPOCH - grace - 1)
        self.assertEqual(past_bound.kind, runtime.ONBOARDING_INVALID)
        self.assertIn("FUTURE", past_bound.reason)

    def test_a_broken_pair_NEVER_falls_back_to_the_spawn_stamp(self):  # noqa: VACUOUS_ASSERTION — the leading VALID parse asserts event_at IS populated on the same attribute, so the Nones are a refusal to guess
        """`ts` is a live, parseable stamp on every one of these records, and
        reading it would produce a confident number for an event nobody
        recorded. No fallback, no clamp, no consumer-supplied default."""
        # POSITIVE CONTROL: the same field IS populated when the pair parses,
        # so the Nones below are a refusal to guess and not a dead attribute.
        self.assertEqual(runtime.parse_onboarding(valid_record(),
                                                  now=LATER).event_at,
                         STAMP_EPOCH)
        self.assertEqual(len(INVALID_RECORDS), 15,
                         "the malformed population changed size")
        for label, rec in INVALID_RECORDS:
            with self.subTest(case=label):
                self.assertTrue(rec.get("ts"), "fixture lost its `ts`")
                got = runtime.parse_onboarding(rec, now=LATER)
                self.assertIsNone(got.event_at)

    def test_the_parser_opens_nothing(self):  # noqa: VACUOUS_ASSERTION — the same AST census over the WRITER finds a read, proving the census can see one before the empty intersection is read as clean
        """PURE ON ITS INPUT — the loader owns the read, the identity guard and
        the exception boundary, and this owns only the schema. A parser that
        reached for a file would be a second reader of the register."""
        # Every name in this tree by which a function reaches the register or
        # the filesystem. Both call shapes are counted, because the writer
        # reaches the record through a bare name and the file through an
        # attribute, and a census that saw only one shape would call the
        # writer clean too.
        touches = {"read_json", "write_json", "open", "listdir", "run",
                   "_spawn_record", "_spawn_path"}

        def called(fn):
            names = set()
            for n in ast.walk(ast.parse(inspect.getsource(fn))):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
                elif isinstance(n.func, ast.Name):
                    names.add(n.func.id)
            return names

        # POSITIVE CONTROL: the same census over the WRITER finds three of
        # them, so an empty intersection below means the parser is clean
        # rather than that this census cannot see a read at all.
        self.assertEqual(called(runtime._record_onboarding) & touches,
                         {"write_json", "_spawn_record", "_spawn_path"})
        self.assertEqual(called(runtime.parse_onboarding) & touches, set())


class TheProductionRegisterShapeStillReadsLEGACY(unittest.TestCase):
    def test_the_frozen_production_key_set_parses_ABSENT(self):  # noqa: VACUOUS_ASSERTION — the planted-record must-hit at the end returns INVALID from the same probe on the same fixture
        """A STRICTNESS CHANGE IS TESTED AGAINST PRODUCTION DATA. Measured on
        the fleet the day this was written: every live spawn register carried
        exactly these keys and none of the three onboarding ones, so this door
        refuses NOTHING that exists today. The fixture is the frozen key set,
        so a later writer that starts emitting a half-record fails here."""
        self.assertNotIn("onboarding", LEGACY_RECORD)  # noqa: VACUOUS_ASSERTION — the planted-record must-hit below is this arm's positive control on the same probe
        self.assertEqual(runtime.parse_onboarding(LEGACY_RECORD, now=LATER),
                         runtime.ONBOARDING_NONE)
        # MUST-HIT: prove this probe can distinguish at all, so the ABSENT
        # above is a reading and not an inert call.
        self.assertEqual(
            runtime.parse_onboarding(dict(LEGACY_RECORD, onboarding="x"),
                                     now=LATER).kind,
            runtime.ONBOARDING_INVALID)


class TheWriterAndTheDoorShareOneFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        from helm import pk
        self.pk = pk
        pk.write_json(os.path.join(self.tmp, "spawn.json"), dict(LEGACY_RECORD))

    def stored(self):
        return self.pk.read_json(os.path.join(self.tmp, "spawn.json"), None)

    def test_BOTH_outcomes_are_written_and_read_back_VALID(self):  # noqa: VACUOUS_ASSERTION — len(outcomes) is asserted unconditionally before the loop, and each iteration asserts a POSITIVE kind, proven and proof
        """THE ROUND TRIP IS THE ARM. Asserting the writer's output against a
        transcribed string would let the two drift apart the moment either
        moved; driving the real writer into the real parser cannot."""
        outcomes = (harness.DELIVERED, harness.NOT_DELIVERED, harness.UNKNOWN)
        self.assertEqual(len(outcomes), 3, "an outcome fell out of the fixture")
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.pk.write_json(os.path.join(self.tmp, "spawn.json"),
                                   dict(LEGACY_RECORD))
                ok = runtime._record_onboarding(self.tmp, "seat-under-test",
                                                outcome, "why it refused")
                self.assertTrue(ok)
                rec = self.stored()
                self.assertEqual(sorted(set(rec) - set(LEGACY_RECORD)),
                                 ["onboarding", "onboarding_at",
                                  "onboarding_proof"])
                got = runtime.parse_onboarding(rec)
                self.assertEqual(got.kind, runtime.ONBOARDING_VALID)
                self.assertIs(got.proven, outcome == harness.DELIVERED)
                self.assertEqual(got.proof, "why it refused")

    def test_the_written_stamp_is_the_moment_of_the_write(self):
        before = int(time.time())
        self.assertTrue(runtime._record_onboarding(
            self.tmp, "seat-under-test", harness.DELIVERED, "p"))
        after = int(time.time())
        parsed = runtime.parse_onboarding(self.stored())
        self.assertEqual(parsed.kind, runtime.ONBOARDING_VALID)
        event_at = parsed.event_at
        self.assertTrue(before <= event_at <= after,
                        "%r not in [%r, %r]" % (event_at, before, after))

    def test_the_stamp_reads_the_same_clock_as_time_time(self):
        """gmtime() with no argument reads time(NULL), a coarse clock that
        lagged time.time() by a second on a fab node (1790150587 not in
        [1790150588, 1790150588]). Pinning time.time() proves which clock
        the writer reads."""
        with mock.patch.object(runtime.time, "time", return_value=1234567890.9):
            self.assertTrue(runtime._record_onboarding(
                self.tmp, "seat-under-test", harness.DELIVERED, "p"))
        parsed = runtime.parse_onboarding(self.stored(), now=1234567999)
        self.assertEqual(parsed.kind, runtime.ONBOARDING_VALID)
        self.assertEqual(parsed.event_at, 1234567890)

    def test_an_unrecognised_state_is_DECLINED_and_nothing_is_written(self):  # noqa: VACUOUS_ASSERTION — the leading recognised-state write returns True on this exact fixture, so the False is the whitelist and not an unwritable directory
        """The register would otherwise hold a value the door must call
        INVALID, which costs a pane read; ABSENT is already a defined reading
        and the caller still prints its own refusal to whoever ran it."""
        # POSITIVE CONTROL: this exact fixture and this exact call DO write
        # when the state is recognised, so the False below is the whitelist and
        # not an unwritable directory.
        self.assertTrue(runtime._record_onboarding(
            self.tmp, "seat-under-test", harness.DELIVERED, "p"))
        self.pk.write_json(os.path.join(self.tmp, "spawn.json"),
                           dict(LEGACY_RECORD))
        ok = runtime._record_onboarding(self.tmp, "seat-under-test",
                                        "shipped", "p")
        self.assertFalse(ok)
        self.assertEqual(self.stored(), LEGACY_RECORD)

    def test_another_seats_register_is_refused(self):  # noqa: VACUOUS_ASSERTION — the leading own-seat write returns True on this exact fixture, so the False is the identity guard
        self.assertTrue(runtime._record_onboarding(
            self.tmp, "seat-under-test", harness.DELIVERED, "p"))
        self.pk.write_json(os.path.join(self.tmp, "spawn.json"),
                           dict(LEGACY_RECORD))
        ok = runtime._record_onboarding(self.tmp, "a-different-seat",
                                        harness.DELIVERED, "p")
        self.assertFalse(ok)
        self.assertEqual(self.stored(), LEGACY_RECORD)

    def test_an_unwritable_register_is_FAIL_SOFT(self):  # noqa: VACUOUS_ASSERTION — the leading unpatched write returns True on this exact fixture, so the False is the OSError path
        """A spawn that briefed its seat correctly must not be failed by a
        register write."""
        self.assertTrue(runtime._record_onboarding(
            self.tmp, "seat-under-test", harness.DELIVERED, "p"))
        self.pk.write_json(os.path.join(self.tmp, "spawn.json"),
                           dict(LEGACY_RECORD))
        with mock.patch.object(self.pk, "write_json",
                               side_effect=OSError("read-only")):
            ok = runtime._record_onboarding(self.tmp, "seat-under-test",
                                            harness.DELIVERED, "p")
        self.assertFalse(ok)
        self.assertEqual(self.stored(), LEGACY_RECORD)


class TheLadderDecidesTheDoorFirstAndChangesNothingElse(unittest.TestCase):
    #: Inputs chosen so the pre-door ladder returns a DIFFERENT verdict in each
    #: row — including the two it reaches by refusing to answer.
    LADDER_CASES = (
        ("ok", dict(pane_live=True, age=10, log_state=None, inflight_n=0,
                    ctx_pct=1.0, ctx_threshold=90, spawn_age=100)),
        ("off", dict(pane_live=False, age=10, log_state=None, inflight_n=0,
                     ctx_pct=1.0, ctx_threshold=90, spawn_age=100)),
        ("blind census", dict(pane_live=None, age=10, log_state=None,
                              inflight_n=0, ctx_pct=1.0, ctx_threshold=90,
                              spawn_age=100)),
        ("starved", dict(pane_live=True, age=999999, log_state="streak",
                         inflight_n=0, ctx_pct=1.0, ctx_threshold=90,
                         spawn_age=100)),
        ("compact bar", dict(pane_live=True, age=999999, log_state=None,
                             inflight_n=0, ctx_pct=95.0, ctx_threshold=90,
                             spawn_age=100)),
        ("unreadable suspend", dict(pane_live=True, age=999999,
                                    log_state=None, inflight_n=0, ctx_pct=1.0,
                                    ctx_threshold=90, spawn_age=100,
                                    suspend_gap_s=None)),
    )

    def test_an_unreadable_register_outranks_every_other_rung(self):  # noqa: VACUOUS_ASSERTION — the matrix size is asserted unconditionally and each iteration asserts the baseline verdict DIFFERS before asserting the new one
        invalid = runtime.parse_onboarding(dict(LEGACY_RECORD,
                                                onboarding="shipped",
                                                onboarding_at=STAMP),
                                           now=LATER)
        self.assertEqual(len(self.LADDER_CASES), 6, "the ladder matrix shrank")
        for label, kwargs in self.LADDER_CASES:
            with self.subTest(case=label):
                baseline, _ = proxywatch.turn_state(**kwargs)
                verdict, evidence = proxywatch.turn_state(onboarding=invalid,
                                                          **kwargs)
                self.assertNotEqual(baseline, "onboarding-unreadable",
                                    "fixture already produced the verdict")
                self.assertEqual(verdict, "onboarding-unreadable")
                self.assertIn(invalid.reason, evidence)

    def test_ABSENT_and_VALID_leave_every_verdict_EXACTLY_as_it_was(self):
        """THE REGRESSION ARM. For any register the door can actually read,
        every verdict below it must be identical to the verdict the same
        inputs produce with no register read at all."""
        readable = (runtime.ONBOARDING_NONE,
                    runtime.parse_onboarding(valid_record(harness.DELIVERED),
                                             now=LATER),
                    runtime.parse_onboarding(
                        valid_record(harness.NOT_DELIVERED), now=LATER),
                    runtime.parse_onboarding(valid_record(harness.UNKNOWN),
                                             now=LATER))
        self.assertEqual(len(readable), 4, "a readable kind fell out")
        self.assertEqual(len(self.LADDER_CASES), 6, "the ladder matrix shrank")
        # THE MATRIX MUST BE DISCRIMINATING. Six inputs that all produced the
        # same verdict would make the equality below true for free.
        self.assertGreaterEqual(
            len({proxywatch.turn_state(**k)[0] for _, k in self.LADDER_CASES}),
            5, "the ladder matrix stopped separating verdicts")
        for label, kwargs in self.LADDER_CASES:
            baseline = proxywatch.turn_state(**kwargs)
            for onboarding in readable:
                with self.subTest(case=label, kind=onboarding.kind,
                                  proven=onboarding.proven):
                    self.assertTrue(onboarding.readable)
                    self.assertEqual(
                        proxywatch.turn_state(onboarding=onboarding, **kwargs),
                        baseline)

    def test_the_verdict_copy_claims_only_the_LATEST_record(self):
        """A resume REWRITES the spawn register, so a seat with an unreadable
        onboarding record may have run for hours before its last relaunch. Copy
        that says the seat never ran, never woke, or holds no context would be
        a lifetime claim this reader has no evidence for."""
        invalid = runtime.parse_onboarding(dict(LEGACY_RECORD,
                                                onboarding="shipped",
                                                onboarding_at=STAMP),
                                           now=LATER)
        _, evidence = proxywatch.turn_state(
            pane_live=True, age=10, log_state=None, inflight_n=0, ctx_pct=1.0,
            ctx_threshold=90, spawn_age=100, onboarding=invalid)
        self.assertIn("LATEST", evidence)
        self.assertIn("UNKNOWN", evidence)
        lowered = evidence.lower()
        # A PLAIN SUBSTRING GUARD CANNOT READ A NEGATION, so the copy must not
        # contain one: a sentence saying "nothing here says the seat never
        # ran" trips this and is right to, because a reader skimming it takes
        # away the claim it was disclaiming.
        for forbidden in ("never ran", "never run", "no context",
                          "never woke", "never armed", "has never",
                          "has ever run"):
            self.assertNotIn(forbidden, lowered)

    def test_a_caller_that_read_no_register_is_unaffected(self):  # noqa: VACUOUS_ASSERTION — the matrix size is asserted unconditionally before the loop and each pair compares two live turn_state calls
        self.assertEqual(len(self.LADDER_CASES), 6, "the ladder matrix shrank")
        for label, kwargs in self.LADDER_CASES:
            with self.subTest(case=label):
                self.assertEqual(proxywatch.turn_state(onboarding=None,
                                                       **kwargs),
                                 proxywatch.turn_state(**kwargs))


class TheFindingIsIndependentOfTheVerdict(unittest.TestCase):
    #: Every key `findings()` indexes off a row WITHOUT a default, so a
    #: missing one is a KeyError rather than a silent skip. Derived by reading
    #: the reducer rather than copied from a sibling suite: a suite that
    #: imports another suite inherits its module state and collection order
    #: along with the fixture.
    def row(self, **fields):
        base = {"seat": "seat-under-test", "family": "family-under-test",
                "config_ok": True, "drift": [], "transcript_age_s": 0,
                "pane_live": False, "hang_candidate": False,
                "log": None, "log_detail": None,
                "probe": None, "probe_detail": None,
                "turn_state": "off", "turn_evidence": None,
                "liveness": None, "error": None,
                "upstream": None, "upstream_detail": None,
                "upstream_ms": None, "upstream_since": None,
                "onboarding_kind": "absent", "onboarding_unreadable": False,
                "onboarding_reason": None}
        base.update(fields)
        return {"seats": [base], "upstream": {}, "proxy_runtime": {},
                "ts": 1000}

    def test_the_fixture_is_quiet_before_anything_is_planted(self):
        """THE FIXTURE IS AN INSTRUMENT AND IT GETS A CONTROL. A baseline row
        that already tripped another rung would make every `assertIn` below
        true for a reason that has nothing to do with this door."""
        self.assertEqual(proxywatch.findings(self.row()), [])
        # And the same fixture DOES speak when something is planted, so the
        # emptiness above is a quiet baseline rather than a dead reducer.
        self.assertTrue(proxywatch.findings(
            self.row(onboarding_unreadable=True, onboarding_reason="r")))

    def kinds(self, rep):
        return [k for k, _ in proxywatch.findings(rep)]

    def test_an_unreadable_register_is_reported_even_when_another_verdict_won(self):
        """Neither fact may mask the other: the ladder returns ONE verdict and
        a blind census can win it, but the register is still unreadable and
        somebody still has to go and look."""
        rep = self.row(onboarding_unreadable=True,
                       onboarding_reason="outcome 'shipped' is not recognised",
                       onboarding_kind="invalid", turn_state="off")
        found = proxywatch.findings(rep)
        self.assertIn("ONBOARDING-UNREADABLE", [k for k, _ in found])
        text = "".join(t for k, t in found if k == "ONBOARDING-UNREADABLE")
        self.assertIn("outcome 'shipped' is not recognised", text)
        self.assertIn("seat-under-test", text)
        self.assertIn("UNKNOWN", text)

    def test_a_readable_register_reports_nothing(self):  # noqa: VACUOUS_ASSERTION — the leading unreadable row DOES emit the finding through the same helper, so the absences are the guard and not a silent findings()
        # POSITIVE CONTROL through the same helper: it DOES emit when the
        # decision says unreadable, so the absences below are the guard and not
        # a findings() that has stopped reporting anything.
        self.assertIn("ONBOARDING-UNREADABLE",
                      self.kinds(self.row(onboarding_unreadable=True,
                                          onboarding_reason="r")))
        for kind in ("absent", "valid"):
            with self.subTest(kind=kind):
                self.assertNotIn("ONBOARDING-UNREADABLE",
                                 self.kinds(self.row(onboarding_kind=kind)))

    def test_the_finding_makes_no_lifetime_claim_either(self):
        found = proxywatch.findings(self.row(onboarding_unreadable=True,
                                             onboarding_reason="r"))
        text = "".join(t for k, t in found
                       if k == "ONBOARDING-UNREADABLE").lower()
        self.assertTrue(text)
        self.assertIn("latest", text)
        for forbidden in ("never ran", "never run", "no context", "never woke",
                          "has never", "has ever run"):
            self.assertNotIn(forbidden, text)


class NoUnreadableRegisterCanAuthorizeATakeover(unittest.TestCase):
    """Run through the REAL path rather than a hand-built state.

    Each malformed register is parsed by the real door, fed to the real ladder,
    shaped into the row `_proxy` builds, and offered to `_admissible` with an
    EXPIRED claim — the most permissive input a takeover can be given.
    """

    def sample(self, verdict):
        return {"state": verdict, "proxywatch_state": verdict,
                "pane_live": True, "inflight": 0, "pending": 0,
                "pending_after": False, "open_dispatches": 0}

    def test_every_INVALID_case_refuses_at_the_most_permissive_claim(self):
        # THE CONTROL FIRST: the very state this matrix would otherwise reach
        # IS admissible on a socket census alone, with no claim expiry and no
        # pending census. That is what makes the refusals below load-bearing.
        self.assertEqual(takeover._admissible(self.sample("context-full"),
                                              {"expired": False}),
                         "context-full")
        self.assertEqual(len(INVALID_RECORDS), 15,
                         "the malformed population changed size")
        for label, rec in INVALID_RECORDS:
            with self.subTest(case=label):
                parsed = runtime.parse_onboarding(rec, now=LATER)
                verdict, _ = proxywatch.turn_state(
                    pane_live=True, age=999999, log_state=None, inflight_n=0,
                    # A context-full row: the ONE state that authorizes on a
                    # socket census alone, with no claim expiry and no pending
                    # census — so it is the state that would have taken the
                    # pane if the door had not decided first.
                    ctx_pct=99.0, ctx_threshold=90, spawn_age=100,
                    onboarding=parsed)
                self.assertEqual(verdict, "onboarding-unreadable")
                with self.assertRaises(takeover.TakeoverRefused):
                    takeover._admissible(self.sample(verdict),
                                         {"expired": True})

    def test_the_refusal_names_what_replacing_the_pane_would_cost(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted to CONTAIN two required phrases before the one forbidden phrase is excluded
        with self.assertRaises(takeover.TakeoverRefused) as caught:
            takeover._admissible(self.sample("onboarding-unreadable"),
                                 {"expired": True})
        said = str(caught.exception)
        self.assertIn("does not parse", said)
        self.assertIn("UNKNOWN", said)
        self.assertIn("composer", said)
        # THE CATCH-ALL ALREADY REFUSED THIS. The branch exists so the SENTENCE
        # is true, and "not a measured contradiction" was false of a register
        # that is present and does not parse.
        self.assertNotIn("not a measured contradiction", said)


class TheOwnerScanLineNeverReadsUSABLE(unittest.TestCase):
    def test_an_unreadable_register_renders_UNKNOWN_not_a_false_green(self):
        """An unenumerated turn state falls through every rung and renders
        USABLE with no reason — a false green on the one line the owner
        scans."""
        row = {"seat": "seat-under-test", "turn_state": "onboarding-unreadable",
               "turn_evidence": "the register does not parse",
               "semantic_age_s": None, "pane": True, "unknown": {},
               "upstream_dark": False, "registered": True}
        state, why = seat_usability.verdict(row)
        self.assertEqual(state, seat_usability.UNKNOWN)
        self.assertIn("the register does not parse", why)


class TheDoorIsWiredAtEveryLeg(unittest.TestCase):
    def test_the_facade_exports_the_whole_door(self):  # noqa: VACUOUS_ASSERTION — len(names) is asserted unconditionally before the loop and each name is asserted identical to the impl object
        """Without these the writer is a NameError at spawn time and every
        consumer has to reach into an impl module."""
        names = ("ONBOARDING_ABSENT", "ONBOARDING_VALID", "ONBOARDING_INVALID",
                 "ONBOARDING_NONE", "Onboarding", "parse_onboarding",
                 "onboarding_invalid", "_record_onboarding")
        self.assertEqual(len(names), 8, "a door name fell out of the fixture")
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(seat, name), getattr(runtime, name))

    def test_BOTH_launch_legs_record_the_outcome(self):
        """Spawn and resume each reach the refusal independently, and a leg
        that printed without recording is the whole defect."""
        tree = ast.parse(inspect.getsource(seat))
        legs = {n.name: n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name in ("_spawn",
                                                                 "_resume")}
        self.assertEqual(sorted(legs), ["_resume", "_spawn"])
        for name, node in sorted(legs.items()):
            with self.subTest(leg=name):
                calls = {c.func.id for c in ast.walk(node)
                         if isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Name)}
                self.assertIn("_record_onboarding", calls)

    def test_health_hands_the_ladder_the_CLOSED_result(self):
        """Not the raw fields, and not a re-derived boolean — the ladder must
        receive the object the door built, or a second reading exists."""
        tree = ast.parse(inspect.getsource(proxywatch.health))
        calls = [c for c in ast.walk(tree)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "turn_state"]
        self.assertEqual(len(calls), 1,
                         "health() calls turn_state %d times" % len(calls))
        self.assertIn("onboarding", [kw.arg for kw in calls[0].keywords])

    def test_the_loader_reads_the_register_through_the_door(self):
        src = inspect.getsource(proxywatch._spawn_onboarded)
        self.assertIn("parse_onboarding", src)  # noqa: VACUOUS_ASSERTION — the assertIn above is this arm's unconditional positive control on the same source text
        # NO SECOND SCHEMA. A loader that touched the field names would be
        # deciding what they mean, which is the door's only job.
        for field in ("onboarding_at", "onboarding_proof"):
            self.assertNotIn(field, src)

    # ---- the loader owns ONE distinction: absent versus unreadable --------

    def _instance(self, contents=None):
        """A real instance directory, so `os.path.exists` is answering about a
        real file rather than about a mock."""
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        if contents is not None:
            with open(os.path.join(d, "spawn.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(contents)
        return d

    def _load(self, inst, family="family-under-test", pass_family=True,
              derived=None):
        """Drive the REAL loader with the REAL locators pointed at `inst`."""
        dirs = {(family, "seat-under-test"): inst}
        if derived is not None:
            dirs[(derived, "seat-under-test")] = self._instance()
        with mock.patch.object(
                seat, "_seat_family",
                return_value=(derived or family, None)), \
                mock.patch.object(
                    seat, "_instance_dir",
                    side_effect=lambda f, n: dirs.get((f, n), "/nonexistent")):
            return proxywatch._spawn_onboarded(
                "seat-under-test", family=family if pass_family else None)

    def test_the_loader_returns_the_doors_reading_for_a_real_record(self):  # noqa: VACUOUS_ASSERTION — the result is asserted equal to a live parse AND to the VALID kind, both positive
        """THE LOADER'S HAPPY PATH: a register that IS this seat's and DOES
        parse comes back exactly as the door read it."""
        inst = self._instance(json.dumps(valid_record()))
        got = self._load(inst)
        self.assertEqual(got.kind, runtime.ONBOARDING_VALID)
        self.assertEqual(got, runtime.parse_onboarding(valid_record()))

    def test_NO_FILE_is_ABSENT_but_a_FILE_THAT_WILL_NOT_READ_is_INVALID(self):  # noqa: VACUOUS_ASSERTION — the first assertion is an unconditional VALID read through the same loader and the same patches, so the ABSENT and INVALID readings below are the file and not the harness
        """`_spawn_record` answers None for a missing file and for a corrupt
        one alike, so a loader passing that None straight through delivers a
        register that exists and cannot be read as legacy-ABSENT — the door's
        own distinction, undone one layer below it."""
        # POSITIVE CONTROL FIRST: the same loader, same patches, reads a good
        # register, so the readings below are the file and not the harness.
        self.assertEqual(
            self._load(self._instance(json.dumps(valid_record()))).kind,
            runtime.ONBOARDING_VALID)
        self.assertEqual(self._load(self._instance()).kind,
                         runtime.ONBOARDING_ABSENT)
        for label, body in (("not json", "{ this is not json"),
                            ("json but not an object", "[1, 2, 3]"),
                            ("empty file", "")):
            with self.subTest(case=label):
                got = self._load(self._instance(body))
                self.assertEqual(got.kind, runtime.ONBOARDING_INVALID,
                                 "%s read as %s" % (label, got.kind))
                self.assertIn("exists", got.reason)

    def test_ANOTHER_SEATS_RECORD_AT_THIS_PATH_is_INVALID_not_absent(self):  # noqa: VACUOUS_ASSERTION — the own-seat record is asserted VALID immediately above the foreign one, through the same loader and the same patches
        """A register that is there and does not describe this seat is a
        contradiction, not a failed look."""
        self.assertEqual(
            self._load(self._instance(json.dumps(valid_record()))).kind,
            runtime.ONBOARDING_VALID)
        foreign = dict(valid_record(), seat="a-different-seat")
        got = self._load(self._instance(json.dumps(foreign)))
        self.assertEqual(got.kind, runtime.ONBOARDING_INVALID)
        self.assertIn("a-different-seat", got.reason)

    def test_the_family_is_TAKEN_not_re_derived_from_the_display_name(self):  # noqa: VACUOUS_ASSERTION — the taken-family read is asserted VALID unconditionally before the re-derived read is asserted ABSENT, and the second assertion exists to prove the fixture still reproduces the divergence
        """THE ALIAS DIVERGENCE. `_seat_family` resolves from the DISPLAY NAME;
        when a seat's runtime family differs, re-deriving sends this reader to
        a different instance directory, where it finds nothing and answers
        ABSENT about a record that exists. The caller has already resolved and
        verified the family for its row, so the loader takes it."""
        inst = self._instance(json.dumps(valid_record()))
        # The display-name derivation lands on a DIFFERENT, empty directory.
        taken = self._load(inst, family="verified-family",
                           derived="display-name-family")
        self.assertEqual(taken.kind, runtime.ONBOARDING_VALID,
                         "the verified family was not used")
        rederived = self._load(inst, family="verified-family",
                               pass_family=False,
                               derived="display-name-family")
        self.assertEqual(rederived.kind, runtime.ONBOARDING_ABSENT,
                         "the fixture no longer reproduces the divergence, so "
                         "the arm above proves nothing")

    def test_health_hands_the_loader_the_rows_own_family(self):
        """The wiring half: the caller must pass what it already resolved."""
        tree = ast.parse(inspect.getsource(proxywatch.health))
        calls = [c for c in ast.walk(tree)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "_spawn_onboarded"]
        self.assertEqual(len(calls), 1)
        self.assertIn("family", [kw.arg for kw in calls[0].keywords])

    def test_a_failed_look_loads_as_ABSENT(self):  # noqa: VACUOUS_ASSERTION — the sibling arms above prove this same loader returns VALID and INVALID, so ABSENT here is the failed-look path and not a stuck instrument
        """An unresolvable seat never reaches a register, so it has observed
        nothing and must change nothing."""
        self.assertEqual(proxywatch._spawn_onboarded("no-such-seat-exists"),
                         runtime.ONBOARDING_NONE)


class TheUnonboardedRungConsumesTheDoor(unittest.TestCase):
    """A seat whose LATEST launch never got its brief in.

    Without this rung such a seat reports `idle`, with the evidence "out of
    work, not stuck" — every clause TRUE, and `idle` is the one verdict that
    tells an operator to leave a seat alone.

    THE RUNG DECIDES NOTHING ABOUT THE RECORD. It consumes `Onboarding.refused`
    and a wall age the caller derived, so a malformed outcome cannot reach it
    by construction rather than by a check written here.
    """

    LADDER = dict(pane_live=True, log_state=None, inflight_n=0, ctx_pct=1.0,
                  ctx_threshold=90, spawn_age=100)

    @staticmethod
    def refused(outcome=None):
        return runtime.parse_onboarding(
            valid_record(harness.NOT_DELIVERED if outcome is None else outcome),
            now=LATER)

    def test_the_REFUSED_predicate_is_true_for_exactly_one_shape(self):
        """Never for ABSENT, which nobody wrote; never for INVALID, which
        nobody can read; never for a proven submit."""
        self.assertTrue(self.refused(harness.NOT_DELIVERED).refused)
        self.assertTrue(self.refused(harness.UNKNOWN).refused)
        for other in (runtime.ONBOARDING_NONE,
                      runtime.onboarding_invalid("unreadable"),
                      runtime.parse_onboarding(
                          valid_record(harness.DELIVERED), now=LATER)):
            with self.subTest(kind=other.kind, proven=other.proven):
                self.assertFalse(other.refused)

    def test_the_rung_fires_when_no_turn_completed_since_the_EVENT(self):
        verdict, evidence = proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)
        self.assertEqual(verdict, "unonboarded")
        self.assertIn("NOT PROVEN submitted", evidence)
        self.assertIn("composer held helm's own text", evidence)

    def test_a_completed_turn_since_the_event_is_NOT_unonboarded(self):  # noqa: VACUOUS_ASSERTION — the firing case is asserted first, unconditionally, so the non-firing reading below is the comparison and not a dead rung
        """The whole predicate: `age >= onboarding_age_s` says the last
        COMPLETED entry predates the launch that failed to brief."""
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)[0], "unonboarded")
        self.assertNotEqual(proxywatch.turn_state(
            age=3600, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)[0], "unonboarded")

    def test_NO_OTHER_reading_can_produce_this_verdict(self):  # noqa: VACUOUS_ASSERTION — the refused reading is asserted to produce the verdict first, unconditionally, on the same inputs
        """ABSENT, INVALID and a proven submit must all decline, and INVALID
        must decline by having ALREADY been answered further up the ladder."""
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)[0], "unonboarded")
        for onboarding, wanted in (
                (runtime.ONBOARDING_NONE, None),
                (runtime.parse_onboarding(valid_record(harness.DELIVERED),
                                          now=LATER), None),
                (runtime.onboarding_invalid("the register does not parse"),
                 "onboarding-unreadable")):
            with self.subTest(kind=onboarding.kind):
                got, _ = proxywatch.turn_state(
                    age=36000, onboarding=onboarding, onboarding_age_s=28800,
                    **self.LADDER)
                self.assertNotEqual(got, "unonboarded")
                if wanted:
                    self.assertEqual(got, wanted)

    def test_a_missing_age_never_becomes_a_confident_verdict(self):  # noqa: VACUOUS_ASSERTION — the both-ages-present case is asserted to fire first, unconditionally, so each None below is the guard and not a dead rung
        """No fallback to spawn age and no clamp: an age nobody derived leaves
        this rung silent rather than guessing."""
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)[0], "unonboarded")
        for age, event_age in ((None, 28800), (36000, None), (None, None)):
            with self.subTest(age=age, event_age=event_age):
                self.assertNotEqual(proxywatch.turn_state(
                    age=age, onboarding=self.refused(),
                    onboarding_age_s=event_age, **self.LADDER)[0],
                    "unonboarded")

    def test_the_two_ages_are_compared_on_the_SAME_clock(self):
        """A host suspend must not hide the answer. Staleness is corrected so a
        suspend is not blamed on the seat; this comparison is of two WALL
        stamps, and correcting only one of them lets a suspend erase a seat
        that has completed no turn since its launch."""
        ladder = dict(self.LADDER, suspend_gap_s=10800)
        verdict, evidence = proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **ladder)
        self.assertEqual(verdict, "unonboarded",
                         "the suspend correction reached the wall comparison")
        # The corrected agent age (7h) is BELOW the event age (8h), so a rung
        # reading the corrected value declines — which is what makes this
        # fixture discriminating rather than decorative.
        self.assertLess(36000 - 10800, 28800)
        self.assertIn("onboarding event 480m", evidence)

    def test_it_OUTRANKS_compact_needed_and_DEFERS_to_a_blind_census(self):
        """Precedence, both directions. compact-needed says autocompact owns
        the fix, which is FALSE here because autocompact refuses on unsent
        input — and unsent input is exactly this state. But a census that
        could not look has made no claim, and UNKNOWN outranks everything."""
        over_bar = dict(self.LADDER, ctx_pct=99.0)
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **over_bar)[0], "unonboarded")
        # ...and the same row WITHOUT the recorded refusal is compact-needed,
        # so the rung is what moved the verdict.
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=runtime.ONBOARDING_NONE,
            onboarding_age_s=None, **over_bar)[0], "compact-needed")
        blind = dict(self.LADDER, pane_live=None)
        self.assertEqual(proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **blind)[0], "hung-unknown")

    def test_the_verdict_PRESCRIBES_NO_KEYSTROKE(self):
        """The load-bearing half. `submit` refuses for FOUR reasons and only
        one — a positively foreign composer — means a person's text is sitting
        there. This reader has taken no fresh look, so "press Enter" would be
        right for three and would SUBMIT A HUMAN'S TEXT for the fourth."""
        _, evidence = proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)
        self.assertIn("Do NOT send a bare Enter", evidence)
        self.assertIn("--submit", evidence)
        self.assertIn("helm seat composers", evidence)

    def test_the_verdict_copy_claims_only_the_LATEST_launch(self):
        """Resume rewrites the spawn register, so a seat here may have run for
        hours before its last relaunch."""
        _, evidence = proxywatch.turn_state(
            age=36000, onboarding=self.refused(), onboarding_age_s=28800,
            **self.LADDER)
        self.assertIn("LATEST", evidence)
        lowered = evidence.lower()
        for forbidden in ("never ran", "never run", "no context",
                          "has never", "has ever run"):
            self.assertNotIn(forbidden, lowered)

    def test_the_derived_age_KEEPS_ITS_FRACTION(self):  # noqa: VACUOUS_ASSERTION — the assertAlmostEqual and the assertNotEqual on int(got) are both unconditional positives on the returned value
        """TRUNCATING ONE SIDE OF A COMPARISON CHANGES ITS ANSWER, inside the
        same second. `transcript_age_s` is a fractional semantic age, so an
        onboarding age rounded down to whole seconds can read as at-or-past an
        event it is actually below."""
        onboarding = self.refused()
        now = onboarding.event_at + 3600.9
        got = proxywatch.onboarding_age_s(onboarding, now=now)
        self.assertIsInstance(got, float)
        self.assertAlmostEqual(got, 3600.9, places=3)
        self.assertNotEqual(int(got), got, "the fixture lost its fraction, so "
                                           "this arm cannot see a truncation")

    def test_a_SUB_SECOND_difference_decides_the_verdict(self):
        """The exact pair: a 3600.4s semantic age against a 3600.9s onboarding
        event is BELOW it, so a turn completed since and the seat is not
        unonboarded. Truncating the event age to 3600 flips it."""
        precise = proxywatch.turn_state(
            age=3600.4, onboarding=self.refused(), onboarding_age_s=3600.9,
            **self.LADDER)[0]
        self.assertNotEqual(precise, "unonboarded")
        # THE MUTATION, PINNED IN THE ARM: the same inputs with the event age
        # truncated DO produce the verdict, so this pair is discriminating and
        # the float is load-bearing rather than decorative.
        truncated = proxywatch.turn_state(
            age=3600.4, onboarding=self.refused(), onboarding_age_s=3600,
            **self.LADDER)[0]
        self.assertEqual(truncated, "unonboarded")
        # ...and the genuinely-past case still fires at the same resolution.
        self.assertEqual(proxywatch.turn_state(
            age=3600.9, onboarding=self.refused(), onboarding_age_s=3600.4,
            **self.LADDER)[0], "unonboarded")

    def test_no_reading_without_an_event_produces_an_age(self):  # noqa: VACUOUS_ASSERTION — the fractional VALID reading is asserted to produce a float in the sibling arm above, through the same function
        for onboarding in (None, runtime.ONBOARDING_NONE,
                           runtime.onboarding_invalid("unreadable"),
                           runtime.parse_onboarding(
                               valid_record(harness.DELIVERED), now=LATER)):
            with self.subTest(reading=getattr(onboarding, "kind", None)):
                got = proxywatch.onboarding_age_s(onboarding, now=LATER)
                if onboarding is not None and onboarding.kind == \
                        runtime.ONBOARDING_VALID:
                    self.assertIsInstance(got, float)
                else:
                    self.assertIsNone(got)

    def test_health_derives_the_event_age_and_hands_it_down(self):
        """The age is derived where `now` lives and nowhere else; the door
        returns an absolute moment and never an age."""
        tree = ast.parse(inspect.getsource(proxywatch.health))
        calls = [c for c in ast.walk(tree)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "turn_state"]
        self.assertEqual(len(calls), 1)
        self.assertIn("onboarding_age_s", [kw.arg for kw in calls[0].keywords])
        # ...and through the NAMED derivation, not an inline expression that
        # could quietly re-acquire a truncation.
        src = inspect.getsource(proxywatch.health)
        self.assertIn("onboarding_age_s(onboarding", src)
        self.assertNotIn("int(now - onboarding.event_at)", src)


class AnUnonboardedSeatIsNotTakenOver(unittest.TestCase):
    def sample(self, verdict):
        return {"state": verdict, "proxywatch_state": verdict,
                "pane_live": True, "inflight": 0, "pending": 0,
                "pending_after": False, "open_dispatches": 0}

    def test_the_context_full_seam_is_closed_at_the_ladder(self):
        """THE SEAM: with no such rung, a failed relaunch onto an old
        high-context transcript reports compact-needed, and `context-full`
        authorizes on a readable socket census ALONE — no claim expiry, no
        pending census. The pane could then be replaced, and the pending brief
        destroyed, on evidence that never mentioned it."""
        # THE CONTROL: that state IS admissible, at expired=False.
        self.assertEqual(takeover._admissible(self.sample("context-full"),
                                              {"expired": False}),
                         "context-full")
        onboarding = runtime.parse_onboarding(
            valid_record(harness.NOT_DELIVERED), now=LATER)
        verdict, _ = proxywatch.turn_state(
            pane_live=True, age=36000, log_state=None, inflight_n=0,
            ctx_pct=99.0, ctx_threshold=90, spawn_age=100,
            onboarding=onboarding, onboarding_age_s=28800)
        self.assertEqual(verdict, "unonboarded")
        with self.assertRaises(takeover.TakeoverRefused):
            takeover._admissible(self.sample(verdict), {"expired": True})

    def test_the_refusal_names_what_replacing_the_pane_would_cost(self):
        with self.assertRaises(takeover.TakeoverRefused) as caught:
            takeover._admissible(self.sample("unonboarded"), {"expired": True})
        said = str(caught.exception)
        self.assertIn("DISCARDS", said)
        self.assertIn("--submit", said)
        self.assertNotIn("not a measured contradiction", said)


class TheOwnerSurfacesReportAnUnonboardedSeat(unittest.TestCase):
    def test_the_scan_line_reads_UNUSABLE_with_the_evidence(self):
        row = {"seat": "seat-under-test", "turn_state": "unonboarded",
               "turn_evidence": "the brief was NOT PROVEN submitted",
               "semantic_age_s": 36000, "pane": True, "unknown": {},
               "upstream_dark": False, "registered": True}
        state, why = seat_usability.verdict(row)
        self.assertEqual(state, seat_usability.UNUSABLE)
        self.assertIn("the brief was NOT PROVEN submitted", why)

    def test_findings_prescribes_rather_than_reports(self):
        base = {"seat": "seat-under-test", "family": "family-under-test",
                "config_ok": True, "drift": [], "transcript_age_s": 36000,
                "pane_live": True, "hang_candidate": False, "log": None,
                "log_detail": None, "probe": None, "probe_detail": None,
                "turn_state": "unonboarded", "liveness": None, "error": None,
                "turn_evidence": "the brief was NOT PROVEN submitted",
                "upstream": None, "upstream_detail": None, "upstream_ms": None,
                "upstream_since": None, "onboarding_kind": "valid",
                "onboarding_unreadable": False, "onboarding_reason": None}
        rep = {"seats": [base], "upstream": {}, "proxy_runtime": {}, "ts": 1000}
        found = proxywatch.findings(rep)
        self.assertIn("UNONBOARDED", [k for k, _ in found])
        text = "".join(t for k, t in found if k == "UNONBOARDED")
        self.assertIn("the brief was NOT PROVEN submitted", text)
        # ...and a seat whose launch DID prove delivery says nothing.
        quiet = dict(base, turn_state="ok", turn_evidence=None)
        self.assertNotIn("UNONBOARDED",
                         [k for k, _ in proxywatch.findings(
                             {"seats": [quiet], "upstream": {},
                              "proxy_runtime": {}, "ts": 1000})])


if __name__ == "__main__":
    unittest.main()
