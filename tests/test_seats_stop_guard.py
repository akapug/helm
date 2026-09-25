#!/usr/bin/env python3
"""`helm stop` — may this seat let go yet, and does the refusal name what holds it?

One question, asked at the one door every seat leaves by. A stop is refused
for an unread inbox, for a budget, for a claim it still holds, or for a lease
whose exemption has to be proven rather than asserted — and each of those
refusals is only useful if the text a seat reads names the thing that is
holding it. So the arms here pair a DECISION with its PRINTED account: the
idle gate itself, the per-firing byte ceilings that keep the block from being
a recurring context tax, the gate-pending and awaiting-land carve-outs that
must prove lane correspondence AND a ref match before they fire, the exempt
lane's one sermon line read off `stop-guard --hook-json` rather than off the
accumulators, and the renewing lease that must name the live run instead of
offering the release command that would kill it.

MOVED WHOLE OUT OF `tests/test_seats.py`, which stood 56,594 bytes under the
1 MiB never-track ceiling with arms still landing in it. No body was rewritten
on the way; each class is the byte-identical text it had there.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `SeatsBase` is imported from
the module these arms came from, so one fixture serves both files and the two
cannot drift. Nothing here borrows a helper from a class left behind, and
nothing left behind borrows from these — measured, zero class-scope
`X = Other.helper` edges in the whole file.
"""
import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from tests._gate_receipt import serial_process
from tests.test_seats import SeatsBase

from helm import (chat, dispatches, pk, proxywatch, record, seats,
                  seats_stop_budget, seats_stop_seam, seats_stop_timing)

# THE FIXTURE THAT PROTECTS THESE ARMS LIVES IN ANOTHER FILE, and two
# source-driven audits read THIS one. `SeatsBase.setUp` snapshots every key in
# `ENV_KEYS`, sets `HELM_SCRATCH_GC=0` so the stop hook's silent-mechanical
# lane cannot reap real `/tmp/claude-*` host scratch, and restores the lot in
# tearDown. tests/test_env_hygiene.py and tests/test_scratch.py each parse a
# module ON ITS OWN, so neither can follow an imported base class -- and moving
# these arms out of tests/test_seats.py moved them out of the only file where
# those two audits could see the guarantee. Both went red on the whole-suite
# gate for exactly that reason; neither was wrong to.
#
# SO IT IS RESTATED HERE AS SOMETHING THAT RUNS, not as a comment and not as an
# allowlist entry. This module snapshots the keys its own arms write, puts them
# back when the module is done, and disables the scratch reaper ITSELF rather
# than trusting that it inherited the setting. If SeatsBase ever stops doing
# either, this module is still safe instead of quietly deleting host scratch.
_ENV_PRIOR = {}


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    _ENV_PRIOR["HELM_STOP_GUARD_CLAIMS"] = os.environ.get("HELM_STOP_GUARD_CLAIMS")
    _ENV_PRIOR["HELM_STOP_GUARD_LEASE_TTL"] = os.environ.get("HELM_STOP_GUARD_LEASE_TTL")
    _ENV_PRIOR["HELM_STOP_GUARD_WHISPER"] = os.environ.get("HELM_STOP_GUARD_WHISPER")
    os.environ["HELM_SCRATCH_GC"] = "0"
    # No dispatch row this module writes walks the host's process table
    # (task/3039; see tests._tmphome.pin_live_seats).
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was
    # A stop that armed a surviving disclosure and never emitted it leaves
    # the text queued for whatever refuses next in this process, which is
    # another module's stop; drain it the way an interrupted response does
    # (task/3039: the slice runner's data audit named it).
    from helm import seats_stop_seam
    seats_stop_seam.fallback_lines(())


class StopGuardTest(SeatsBase):
    """The idle gate (a port of the predecessor harness's arbiter). Hermetic: room + claims in tmp,
    HELM_ADOPTED_DIR in tmp so the silent index-cap leg can never touch a live
    MEMORY.md."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def test_empty_reasons_cannot_refuse_a_stop(self):  # noqa: VACUOUS_ASSERTION — rc=0 is the positive allow control; filtering only timing lines proves blank reasons publish no refusal text
        """bug-class empty-reason-refusal: no text means no refusal."""
        with mock.patch.object(seats, "stop_guard",
                               return_value=(["", " \t"], ["", "\t"])):
            rc, out, err = self.guard()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(
            [line for line in err.splitlines()
             if not line.startswith("[helm stop-guard timing]")], [])

    def test_real_reason_still_refuses_and_prints_its_text(self):
        with mock.patch.object(
                seats, "stop_guard",
                return_value=(["", "real blocker", " \t"], ["", "\t"])):
            rc, out, err = self.guard()
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(
            [line for line in err.splitlines()
             if not line.startswith("[helm stop-guard timing]")],
            ["real blocker"])

    def test_claim_evidence_warn_latches_and_refires_per_message(self):  # noqa: VACUOUS_ASSERTION — first stop proves the same warning is present before the intentional latch-absence assertion
        """Hook payload -> transcript -> WARN is live end to end. The same
        record latches; byte-identical text in a later record re-fires."""
        tp = os.path.join(self.tmp, "claim-turn.jsonl")

        def write(uuid):
            records = [
                {"message": {"role": "user", "content": "report"}},
                {"type": "attachment", "attachment": {}},
                {"uuid": uuid, "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "42 tests green"}]}},
            ]
            with open(tp, "a") as f:
                f.write("\n".join(json.dumps(r) for r in records) + "\n")

        os.environ["HELM_STOP_GUARD_WIRING"] = "0"
        os.environ["HELM_STOP_GUARD_WHISPER"] = "0"
        write("answer-1")
        payload = {"session_id": "s-claim", "transcript_path": tp}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

        write("answer-2")
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

    def test_claim_evidence_warns_while_stop_is_already_continuing(self):
        """stop_hook_active suppresses blocks, not advisory truth. A non-string
        UUID uses the record-offset fallback rather than escaping the rung."""
        tp = os.path.join(self.tmp, "claim-active.jsonl")
        records = [
            {"message": {"role": "user", "content": "report"}},
            {"uuid": 123, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "42 tests green"}]}},
        ]
        with open(tp, "w") as f:
            f.write("\n".join(json.dumps(r) for r in records))
        payload = {"session_id": "s-active", "transcript_path": tp,
                   "stop_hook_active": True}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIMS WITHOUT THIS TURN'S EVIDENCE", err)

    def test_unreadable_claim_evidence_warns_skipped_not_clean(self):  # noqa: VACUOUS_ASSERTION — the same SKIPPED line is asserted present before the latch-absence check
        """Unreadable and clean are different operator states. The same broken
        transcript latches; appended broken input re-arms the warning."""
        tp = os.path.join(self.tmp, "claim-unreadable.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"message": {"content": "missing role"}}))
        payload = {"session_id": "s-skip", "transcript_path": tp}
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIM-EVIDENCE SKIPPED", err)
        # The independent inbox may still be clean; the claim-evidence state is
        # now separately visible instead of being inferred from that line.

        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("CLAIM-EVIDENCE SKIPPED", err)

        with open(tp, "a") as f:
            f.write("\n" + json.dumps({"message": {"content": "still broken"}}))
        rc, _out, err = self.guard(payload, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)
        self.assertIn("CLAIM-EVIDENCE SKIPPED", err)

    def test_credential_wall_suppresses_stop_inbox_without_consuming_it(self):
        seats.join(session="s-codex", seat="codex", cwd="/tmp/p")
        chat.post("@codex cannot act until healthy", who="owner")
        pk.write_json(proxywatch._state_path(), {"ts": time.time(),
                      "upstream": {"codex": {"state": "AUTH-401",
                                               "dark": True}}})
        cur = dict(seats._cursor("main", "codex", "s-codex"))
        blocks, warns = seats.stop_guard(
            session="s-codex", room="main", seat="codex")
        self.assertFalse(any("undelivered message(s)" in b for b in blocks),
                         blocks)
        self.assertFalse(any("undelivered message(s)" in w for w in warns),
                         warns)
        blocks, warns = seats.stop_guard(
            session="s-codex", room="main", seat="codex", stop_active=True)
        self.assertFalse(any("arrived DURING" in w for w in warns), warns)
        self.assertEqual(seats._cursor("main", "codex", "s-codex"), cur)
        self.assertTrue(seats._pending_all(
            "main", "codex", "s-codex", scan_lane="test"))

    def test_block_exit_skips_invisible_claim_evidence_work(self):  # noqa: VACUOUS_ASSERTION — the unconditional inbox block proves this is the block-exit arm before assert_not_called
        """A block exit discards WARNs, so it must not parse or latch one."""
        from helm import claimev
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice review the branch", who="bob")
        with mock.patch.object(claimev, "assessment") as assessment:
            rc, _out, err = self.guard(
                {"session_id": "s-block", "transcript_path": "/missing"},
                args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)
        # the block no longer echoes the row body (task 692 dropped samples);
        # the runnable park verb proves it is the full inbox block that fired
        self.assertIn("helm chat catchup --including-mentions --apply", err)
        assessment.assert_not_called()

    def test_an_unwritable_inbox_latch_degrades_to_warn_not_block(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls bracket the loop: the MUST-HIT block before it and the healed-latch block after it both assert presence on the same observable; the loop's absence checks are the reviewed warn-not-block shape
        """The block's own closing promise — 'a re-stop on the same rows
        passes' — is the LATCH's promise. When the latch write failed, the
        old code swallowed the OSError and blocked anyway: an unconditional
        re-block on EVERY stop, forever, wearing a sentence that said the
        opposite. The beacon rung already states the law (no latch, no
        block); this proves the inbox rung obeys it: unwritable latch =>
        the rows surface as a WARN, never a block, and the block (with its
        now-true promise) returns once the latch heals."""
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice first obligation", who="bob")
        # MUST-HIT control: a writable latch blocks these very rows once
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        self.assertTrue([b for b in blocks
                         if "undelivered message(s)" in b], blocks)
        chat.post("@alice second obligation", who="bob")   # new fp re-arms
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only latch dir")):
            first = seats.stop_guard(session="s-lat", room="main",
                                     seat="alice")
            second = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        for blocks, warns in (first, second):    # BOTH stops: warn, no block
            self.assertEqual(
                [b for b in blocks if "undelivered message(s)" in b], [],
                blocks)
            hit = [w for w in warns if "undelivered message(s)" in w]
            self.assertEqual(len(hit), 1, warns)
            self.assertIn("NOT BLOCKING", hit[0])
            self.assertIn("latch is unwritable", hit[0])
            # the warn still teaches the runnable cure
            self.assertIn("helm chat catchup --including-mentions --apply",
                          hit[0])
            # and never wears the latch's promise it cannot keep
            self.assertNotIn("a re-stop on the same rows passes", hit[0])
        # latch healed: one honest block, whose promise then comes true
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        hit = [b for b in blocks if "undelivered message(s)" in b]
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("a re-stop on the same rows passes", hit[0])
        blocks, _w = seats.stop_guard(session="s-lat", room="main",
                                      seat="alice")
        self.assertEqual(
            [b for b in blocks if "undelivered message(s)" in b], [], blocks)

    def test_guard_crash_names_and_completes_error_publication(self):
        with mock.patch("helm.seats_cli.stop_guard",
                        side_effect=RuntimeError("broken guard")):
            rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("THE GUARD COULD NOT RUN", err)
        self.assertIn("[helm stop-guard timing] BEGIN response", err)
        self.assertIn("[helm stop-guard timing] DONE response elapsed=", err)

    def test_expired_nested_span_never_emits_false_done(self):  # noqa: VACUOUS_ASSERTION — BEGIN is the must-hit control; DONE absence proves expiry did not claim completion
        from helm import projscope, seats_stop_timing
        timing = seats_stop_timing.RungTiming()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                projscope.scope(deadline=time.monotonic() - 1), \
                self.assertRaises(projscope.Expired):
            timing.measure("dispatch-ledger", lambda: self.fail("must not run"))
        rendered = err.getvalue()
        self.assertIn("BEGIN dispatch-ledger", rendered)
        self.assertNotIn("DONE dispatch-ledger", rendered)

    def test_unrecognized_active_rung_is_not_misattributed(self):  # noqa: VACUOUS_ASSERTION — both loop specimens assert the explicit UNKNOWN replacement
        for stage in (None, "future-rung"):
            text = seats_stop_budget.unknown(stage)
            self.assertIn("active-rung=UNKNOWN", text)
            self.assertIn("later-rungs=UNKNOWN", text)
            self.assertNotIn("identity=UNFINISHED", text)

    def test_zero_budget_names_every_rung_unknown_and_lets_the_stop_pass(self):
        """A LADDER THAT EXAMINED NOTHING REFUSES NOTHING, and it still says
        so. This asserted rc 2 and was renamed with its behaviour: a zero
        budget means every rung is UNREACHED, which is an absence of looking
        rather than a finding, and refusing on it refuses every stop forever
        (task/2300). The three UNFINISHED/UNREACHED readings are unchanged and
        are what keeps the pass from being silent."""
        with mock.patch("helm.seats_stop_budget.BUDGET_S", 0):
            rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("identity=UNFINISHED", err)
        self.assertIn("inbox=UNREACHED", err)
        self.assertIn("response=UNREACHED", err)
        self.assertIn("This Stop PASSES", err)

    def test_ambient_budget_keeps_measured_margin_under_outer_timeout(self):
        observed_sample_max = 8.7
        immutable_outer = 20.0
        self.assertGreaterEqual(seats_stop_budget.BUDGET_S,
                                2 * observed_sample_max)
        self.assertEqual(seats_stop_budget.BUDGET_S, 17.5)
        self.assertEqual(immutable_outer - seats_stop_budget.BUDGET_S, 2.5)

    def test_admission_costs_round_25_percent_headroom_upward(self):
        self.assertEqual(seats_stop_budget.MEASURED_RUNS, 339)
        for stage, observed in seats_stop_budget.OBSERVED_MAX_S.items():
            with self.subTest(stage=stage):
                cost = seats_stop_budget.ADMISSION_COST_S[stage]
                fitted = observed * seats_stop_budget.COST_MARGIN
                self.assertGreaterEqual(cost, fitted)
                self.assertLess(cost - fitted, 0.1)

    def test_provisional_harvest_counts_one_mirrored_stop_once(self):
        """Only the selected live carrier survives transcript mirrors."""
        path = os.path.join(os.path.dirname(__file__), "fixtures",
                            "stop-guard-timing-mirror.jsonl")
        blocks = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                if record.get("type") == "attachment":
                    attachment = record.get("attachment") or {}
                    if (attachment.get("type") == "hook_success"
                            and attachment.get("hookName") == "Stop"):
                        blocks.extend(attachment.get(name) for name in
                                      ("stdout", "stderr")
                                      if attachment.get(name))
                elif record.get("type") == "system":
                    blocks.extend(record.get("hookErrors") or [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].count(
            "[helm stop-guard timing] BEGIN identity"), 1)
        self.assertEqual(blocks[0].count(
            "[helm stop-guard timing] DONE identity"), 1)

    def test_provisional_fleet_replay_admits_every_observed_fat_tail(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures",
                            "stop-guard-ladder-replay-2026-09-10.tsv")
        with open(path, encoding="utf-8") as f:
            source = f.readline().strip()
            columns = f.readline().strip()
            rows = [line.rstrip("\n").split("\t") for line in f]
        self.assertEqual(
            source,
            "# provisional_transcript_source_sha256="
            "a3a276907ff5ede569592ba016bfb464866519157264396223225a5a236fffd7",
        )
        self.assertIn("dispatch_start", columns)
        self.assertEqual(len(rows), seats_stop_budget.MEASURED_RUNS)
        self.assertEqual(
            {seat: sum(row[1] == seat for row in rows)
             for seat in ("opus-integrator", "helm-claude-2",  # noqa: SEAT_NAME — exact time-local evidence subjects
                          "helm-claude-4")},  # noqa: SEAT_NAME — exact time-local evidence subject
            {"opus-integrator": 297, "helm-claude-2": 38,  # noqa: SEAT_NAME — exact measured split
             "helm-claude-4": 4},  # noqa: SEAT_NAME — exact measured split
        )
        self.assertEqual(
            {carrier: sum(row[2] == carrier for row in rows)
             for carrier in ("attachment.stderr", "hookErrors.[]")},
            {"attachment.stderr": 328, "hookErrors.[]": 11},
        )
        self.assertAlmostEqual(max(float(row[4]) for row in rows),
                               10.319, places=6)
        # THE dispatch-ledger COLUMN STAYS IN THE FIXTURE AS EVIDENCE, and its
        # stage left this loop with its rung: the fold runs in the `helm web`
        # resident, so no reserve or admission cost is pinned for it.
        self.assertNotIn("dispatch-ledger", seats_stop_budget.RESERVE_S)
        for stage, column, latest in (
                ("claims", 6, 0.404),
                ("seam", 7, 5.508)):
            starts = [float(row[column]) for row in rows
                      if row[column] != "-"]
            with self.subTest(stage=stage):
                self.assertEqual(len(starts), 329)
                self.assertAlmostEqual(max(starts), latest, places=6)
                threshold = (seats_stop_budget.BUDGET_S
                             - seats_stop_budget.RESERVE_S[stage]
                             - seats_stop_budget.ADMISSION_COST_S[stage])
                self.assertEqual([start for start in starts
                                  if start > threshold], [])

    def test_measured_6_395s_prefix_reaches_whisper_and_response(self):
        """The production specimen, without a 6.4-second sleeping test.

        The recorded 6.395s pre-whisper prefix was 0.147s before the shared
        dispatch observation + 1.324s dispatch fold + 4.924s seam. The fold is
        no longer on this ladder (the `helm web` resident computes the stop
        facts), so the same stop is 0.147s + 4.924s before the whisper. Each
        producer advances one fake monotonic clock and the exact timing lines
        are the positive control that the arm drove those inputs. The whisper
        returns a marker block after another 4.000s, so the complete path is
        9.071s and still reaches response.
        """
        from helm import projscope
        from helm import seats_stop_guard as guard_impl
        clock = [100.0]

        def advance(seconds, answer):
            clock[0] += seconds
            return answer

        def whisper(*_args, **_kwargs):
            marker = advance(4.000, "[helm stop-whisper] reached")
            projscope.spend_or_raise("delayed whisper result control")
            return marker

        with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(guard_impl, "_pending_all",
                                  side_effect=lambda *a, **k:
                                  advance(0.147, [])), \
                mock.patch.object(guard_impl, "_beacon_block",
                                  return_value=None), \
                mock.patch.object(guard_impl, "_spiral_gate",
                                  return_value=(None, None)), \
                mock.patch.object(guard_impl, "_seam_gate",
                                  side_effect=lambda *a, **k:
                                  advance(4.924, (None, None))), \
                mock.patch.object(guard_impl, "_stop_whisper",
                                  side_effect=whisper) as whisper_spy, \
                mock.patch.dict(os.environ,
                                {"HELM_STOP_GUARD_WIRING": "0",
                                 "HELM_STOP_GUARD_INDEX": "0"}):
            rc, _out, err = self.guard({"session_id": "s-6395"},
                                       args=["--seat", "alice"])
        self.assertEqual(rc, 2, err)
        whisper_spy.assert_called_once()
        self.assertNotIn("dispatch-ledger", err,
                         "the ladder folded the dispatch ledger again")
        self.assertIn("DONE seam elapsed=4.924s total=5.071s", err)
        self.assertIn("BEGIN whisper total=5.071s", err)
        self.assertIn("[helm stop-whisper] reached", err)
        self.assertIn("DONE response elapsed=", err)
        self.assertNotIn("COVERAGE UNKNOWN", err)
        self.assertAlmostEqual(clock[0] - 100.0, 9.071, places=6)
        self.assertLess(clock[0] - 100.0, seats_stop_budget.BUDGET_S)
        self.assertIsNone(projscope.deadline())

    def test_claims_yield_aborts_remaining_claim_work_then_runs_tail(self):
        from helm import projscope
        from helm import seats_stop_guard as guard_impl
        clock = [30.0]

        def claims_start(_rows):
            clock[0] += (seats_stop_budget.BUDGET_S
                         - seats_stop_budget.RESERVE_S["claims"] + 0.001)
            projscope.spend_or_raise("claims start result control")

        # TWO MODULES, BECAUSE THE RUNG SPLIT AND THE DOUBLES FOLLOW THE
        # CODE. `_sweep` and `_now_mono` are read by the claims rung, which
        # now lives in seats_stop_claims; everything below is still read by
        # _stop_guard. Patching all six on one module would raise on the two
        # that moved — and if it did not, the two spies would sit on a path
        # nothing walks.
        from helm import seats_stop_claims as claims_impl
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(claims_impl, "_sweep",
                                  side_effect=claims_start), \
                mock.patch.object(claims_impl, "_now_mono") as later_claims, \
                mock.patch.object(guard_impl, "_beacon_block",
                                  return_value=None), \
                mock.patch.object(guard_impl, "_spiral_gate",
                                  return_value=(None, None)), \
                mock.patch.object(guard_impl, "_seam_gate",
                                  return_value=(None, None)), \
                mock.patch.object(guard_impl, "_stop_whisper",
                                  return_value="[helm stop-whisper] claims tail"), \
                mock.patch.dict(os.environ,
                                {"HELM_STOP_GUARD_WIRING": "0",
                                 "HELM_STOP_GUARD_INDEX": "0"}):
            rc, _out, err = self.guard({"session_id": "s-claims-yield"},
                                       args=["--seat", "alice"])
        # RC 2 COMES FROM THE MOCKED WHISPER, NOT FROM THE YIELD, and that
        # distinction is the whole point of this arm now. `_stop_whisper`'s
        # return value is APPENDED TO BLOCKS ("rides an existing block, or IS
        # the soft hold"), so this fixture always had a blocking rung beside
        # the yield — the coverage line's channel was never observable here,
        # it just happened to share the stream. Now that an unexamined rung
        # files a WARN, `publish` suppresses it beside that block, and its
        # ABSENCE is the reading: before the cure this stderr carried
        # COVERAGE UNKNOWN. The positive — the same yield with no block,
        # printing the line and passing — is
        # test_a_MEASURED_block_still_refuses_beside_an_unexamined_rung.
        self.assertEqual(rc, 2, err)
        later_claims.assert_not_called()
        self.assertNotIn("COVERAGE UNKNOWN", err,
                         "an unexamined rung rode the block channel")
        self.assertIn("YIELD claims", err)
        self.assertIn("[helm stop-whisper] claims tail", err)
        self.assertIn("DONE response elapsed=", err)

    def test_a_MEASURED_block_still_refuses_beside_an_unexamined_rung(self):
        """THE CONTROL ON THE WHOLE task/2300 CURE, and it is why the cure is
        "an absence does not refuse" rather than "nothing refuses". Both halves
        run in ONE arm on ONE fixture, because the difference between them is a
        single pending row and asserting either alone would be a claim about
        the fixture.

        FIRST CALL, nothing pending: the seam rung overruns its reserve, the
        coverage line goes out on the WARN channel and the stop PASSES.
        SECOND CALL, one undelivered row: the SAME overrun happens and the stop
        is REFUSED — by the inbox rung's measured finding, which is evidence
        about the world rather than about the guard's own clock. The coverage
        line is absent from that exit because `publish` suppresses every
        advisory beside a block, which is the existing rule and is stated here
        rather than discovered later."""
        from helm import projscope
        from helm import seats_stop_guard as guard_impl
        clock = [10.0]

        def seam(*_args, **_kwargs):
            clock[0] = start[0] + (seats_stop_budget.BUDGET_S
                                   - seats_stop_budget.RESERVE_S["seam"]
                                   + 0.001)
            projscope.spend_or_raise("seam result control")

        start = [10.0]

        def run():
            clock[0] = start[0]
            with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(guard_impl, "_beacon_block",
                                      return_value=None), \
                    mock.patch.object(guard_impl, "_spiral_gate",
                                      return_value=(None, None)), \
                    mock.patch.object(guard_impl, "_seam_gate",
                                      side_effect=seam), \
                    mock.patch.object(guard_impl, "_stop_whisper",
                                      return_value=None), \
                    mock.patch.dict(os.environ,
                                    {"HELM_STOP_GUARD_WIRING": "0",
                                     "HELM_STOP_GUARD_INDEX": "0"}):
                return self.guard({"session_id": "s-block-beside-yield"},
                                  args=["--seat", "alice"])

        seats.join(session="s-block-beside-yield", seat="alice", cwd="/tmp/p")
        rc, _out, err = run()
        self.assertEqual(rc, 0, err)
        self.assertIn("seam=UNFINISHED", err)
        self.assertIn("This Stop PASSES", err)

        chat.post("@alice a measured obligation", who="bob")
        rc, _out, err = run()
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered message(s)", err)
        self.assertNotIn("This Stop PASSES", err)

    def test_measured_preflight_never_starts_a_late_fat_tail(self):  # noqa: VACUOUS_ASSERTION — each subtest unconditionally proves the early callback fired and the late callback did not
        """Each no-start has the one-millisecond-before positive pole."""
        from helm import projscope
        for stage, cost in seats_stop_budget.ADMISSION_COST_S.items():
            # THE RESOLVER, NOT THE PINNED TABLE. `RESERVE_S` is now the
            # OVERRIDE half of a total answer — a rung may be fitted a cost
            # here and take its reserve from the ladder's shape — so reading
            # the dict directly made this arm KeyError on the first such rung.
            reserve = seats_stop_budget.reserve(stage)
            with self.subTest(stage=stage):
                clock = [15.0]
                late = mock.Mock(return_value="must not run")
                with mock.patch("time.monotonic",
                                side_effect=lambda: clock[0]):
                    state = seats_stop_budget.State()
                    ambient = clock[0] + seats_stop_budget.BUDGET_S
                    expected = ambient - reserve - cost
                    with projscope.scope(deadline=ambient):
                        state.bind_deadline()
                        self.assertAlmostEqual(state.start_deadline(stage),
                                               expected, places=6)
                        clock[0] = expected + 0.001
                        answer = state.run(stage, late, fallback="yielded")
                        left = projscope.spend_or_raise(
                            "later work still admitted")
                self.assertEqual(answer, "yielded")
                late.assert_not_called()
                self.assertAlmostEqual(left, reserve + cost - 0.001,
                                       places=6)
                self.assertIn(stage + "=UNFINISHED",
                              "\n".join(state.warns))
                self.assertEqual(state.blocks, [],
                                 "an unexamined rung was filed as a finding")

                clock[0] = 40.0
                early = mock.Mock(return_value="ran")
                with mock.patch("time.monotonic",
                                side_effect=lambda: clock[0]):
                    state = seats_stop_budget.State()
                    ambient = clock[0] + seats_stop_budget.BUDGET_S
                    expected = ambient - reserve - cost
                    with projscope.scope(deadline=ambient):
                        state.bind_deadline()
                        self.assertAlmostEqual(state.start_deadline(stage),
                                               expected, places=6)
                        clock[0] = expected - 0.001
                        answer = state.run(stage, early, fallback="yielded")
                self.assertEqual(answer, "ran")
                early.assert_called_once_with()
                self.assertEqual(state.yielded, set())

    def test_measured_5_412s_seam_starts_early_and_completes_normally(self):  # noqa: VACUOUS_ASSERTION — exact elapsed and remaining-time controls prove the seam callback advanced the clock
        """Replay the fresh 6.875-second ordinary Stop control."""
        from helm import projscope
        clock = [70.0]
        started = clock[0]

        def seam():
            clock[0] += 5.412
            return "complete"

        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            state = seats_stop_budget.State()
            with projscope.scope(
                    deadline=started + seats_stop_budget.BUDGET_S):
                state.bind_deadline()
                clock[0] += 1.463
                answer = state.run("seam", seam, fallback="yielded")
                left = projscope.spend_or_raise("tail positive control")
        self.assertEqual(answer, "complete")
        self.assertEqual(state.yielded, set())
        self.assertAlmostEqual(clock[0] - started, 6.875, places=6)
        self.assertAlmostEqual(left, 10.625, places=6)

    def test_fresh_6_942s_seam_and_response_fit_the_margin(self):  # noqa: VACUOUS_ASSERTION — exact 8.799s elapsed and 8.701s remaining prove seam and response work executed
        """Replay the current 8.799-second installed-main falsifier."""
        from helm import projscope
        clock = [90.0]
        started = clock[0]

        def seam():
            clock[0] += 6.942
            return "complete"

        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            state = seats_stop_budget.State()
            with projscope.scope(
                    deadline=started + seats_stop_budget.BUDGET_S):
                state.bind_deadline()
                clock[0] += 1.821
                answer = state.run("seam", seam, fallback="yielded")
                clock[0] += 0.036
                left = projscope.spend_or_raise("response positive control")
        self.assertEqual(answer, "complete")
        self.assertEqual(state.yielded, set())
        self.assertAlmostEqual(clock[0] - started, 8.799, places=6)
        self.assertAlmostEqual(left, 8.701, places=6)

    def test_each_fat_tail_reserve_is_local_fail_closed_and_leaves_tail(self):
        """Every configured reserve has a must-hit and a later-work pole."""
        from helm import projscope
        for stage, reserve in seats_stop_budget.RESERVE_S.items():
            with self.subTest(stage=stage):
                clock = [50.0]
                with mock.patch("time.monotonic",
                                side_effect=lambda: clock[0]):
                    state = seats_stop_budget.State()
                    with projscope.scope(
                            deadline=clock[0] + seats_stop_budget.BUDGET_S):
                        with state.rung(stage) as rung:
                            clock[0] += (seats_stop_budget.BUDGET_S
                                         - reserve + 0.001)
                            projscope.spend_or_raise(stage + " must-hit")
                        self.assertFalse(rung.complete,
                                         "reserve did not interrupt its rung")
                        left = projscope.spend_or_raise(
                            "later-rung positive control")
                    self.assertAlmostEqual(left, reserve - 0.001, places=6)
                    text = "\n".join(state.warns)
                    self.assertEqual(state.blocks, [],
                                     "an unexamined rung was filed as a finding")
                    self.assertIn(stage + "=UNFINISHED", text)
                    self.assertIn("later-rungs=CONTINUED", text)
                    self.assertFalse(state.expired,
                                     "a local yield was collapsed into ambient "
                                     "expiry")

    def test_expiry_preserves_earlier_block_and_names_unfinished_rungs(self):
        from helm import projscope
        seats.join(session="s-exp", seat="alice", cwd="/tmp/p")
        chat.post("@alice pending before expiry", who="bob")
        real_spend = projscope.spend_or_raise
        def expire_at_beacon(what):
            if what == "starting stop rung beacon":
                raise projscope.Expired("fixture")
            return real_spend(what)
        with mock.patch.object(projscope, "spend_or_raise",
                               side_effect=expire_at_beacon):
            rc, _out, err = self.guard({"session_id": "s-exp"},
                                       args=["--seat", "alice"])
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered message(s)", err)
        self.assertIn("beacon=UNFINISHED", err)
        self.assertIn("wiring=UNREACHED", err)
        self.assertIn("response=UNREACHED", err)

    def test_expired_fallback_filters_non_string_partial_findings(self):
        def incomplete(**kw):
            budget = kw["budget"]
            budget.blocks.append(123)
            budget.timing.current = "beacon"
            return budget.expire()

        with mock.patch("helm.seats_cli.stop_guard", side_effect=incomplete):
            rc, _out, err = self.guard()
        # THE NON-STRING WAS THE ONLY "BLOCK" AND IT IS FILTERED, so after
        # the coverage line moved to the warn channel there is nothing left
        # to refuse on: a partial finding that cannot even be rendered is not
        # evidence of work. The rendering property this arm exists for is
        # unchanged and still asserted — the ladder's state prints, and no
        # TypeError escapes the filter.
        self.assertEqual(rc, 0, err)
        self.assertIn("beacon=UNFINISHED", err)
        self.assertNotIn("TypeError", err)

    def test_response_expiry_refuses_without_erasing_guard_block(self):
        from helm import projscope
        seats.join(session="s-response-exp", seat="alice", cwd="/tmp/p")
        chat.post("@alice pending before response", who="bob")
        with mock.patch.object(seats_stop_seam, "emit_blocks",
                               side_effect=projscope.Expired("fixture")):
            rc, _out, err = self.guard({"session_id": "s-response-exp"},
                                       args=["--seat", "alice"])
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered message(s)", err)
        self.assertIn("response=UNFINISHED", err)
        self.assertNotIn("DONE response elapsed=", err)

    def test_response_fallback_preserves_then_drains_survivor_disclosure(self):  # noqa: VACUOUS_ASSERTION — populated queues and printed survivor are unconditional controls before asserting both queues drain and the latch stays absent
        from helm import projscope
        session, seat = "s-response-survivor", "alice"
        seats.join(session=session, seat=seat, cwd="/tmp/p")
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        seats_stop_seam._SURVIVES_REFUSAL.clear()
        self.addCleanup(seats_stop_seam._PENDING_DISCLOSURES.clear)
        self.addCleanup(seats_stop_seam._SURVIVES_REFUSAL.clear)
        shared = "/tmp/shared-room"
        text = seats_stop_seam._cotenancy_warn(
            [{"path": shared, "seats": [seat, "bob"]}], {shared},
            "main", seat, session)
        self.assertIsNotNone(text)
        self.assertEqual(len(seats_stop_seam._PENDING_DISCLOSURES), 1)
        self.assertEqual(seats_stop_seam._SURVIVES_REFUSAL, [text])
        latch = seats._stop_fp_path(
            "main", seat, session, kind=seats_stop_seam.COTENANCY_LATCH)
        self.assertFalse(os.path.exists(latch))
        chat.post("@alice pending before response", who="bob")
        with mock.patch.object(seats_stop_seam, "emit_blocks",
                               side_effect=projscope.Expired("fixture")):
            rc, _out, err = self.guard(
                {"session_id": session}, args=["--seat", seat])
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered message(s)", err)
        self.assertEqual(err.count(text), 1)
        self.assertIn("response=UNFINISHED", err)
        self.assertEqual(seats_stop_seam._PENDING_DISCLOSURES, [])
        self.assertEqual(seats_stop_seam._SURVIVES_REFUSAL, [])
        self.assertFalse(os.path.exists(latch))
        out = io.StringIO()
        seats_stop_seam.emit_blocks(["later refusal"], stream=out)
        self.assertEqual(out.getvalue(), "later refusal\n")
        self.assertFalse(os.path.exists(latch))
        self.assertIsNotNone(seats_stop_seam._cotenancy_warn(
            [{"path": shared, "seats": [seat, "bob"]}], {shared},
            "main", seat, session))

    def test_an_ACTIVE_stop_and_an_ORDINARY_one_report_expiry_the_SAME_way(self):
        """THE HARNESS'S OWN STATE IS NOT A REASON AN ABSENCE DOES NOT
        REFUSE. A guard that warns a continuing stop and refuses an ordinary
        one about the SAME unexamined rung refuses that seat forever
        (task/2300) — an absence of looking is one fact either way, and the
        two exits must say it in one sentence.

        BOTH PAYLOADS RUN HERE because "the same" is a claim about a PAIR, and
        an arm that drove one of them would go on passing if the other
        diverged again."""
        from helm import projscope
        seats.join(session="s-active-exp", seat="alice", cwd="/tmp/p")
        seen = []
        for active in (True, False):
            payload = {"session_id": "s-active-exp"}
            if active:
                payload["stop_hook_active"] = True
            with mock.patch("helm.seats_stop_guard._pending_all",
                            side_effect=projscope.Expired("fixture")):
                rc, _out, err = self.guard(payload, args=["--seat", "alice"])
            self.assertEqual(rc, 0, err)
            self.assertIn("inbox=UNFINISHED", err)
            self.assertIn("response=UNREACHED", err)
            self.assertIn("This Stop PASSES", err)
            seen.append(err)
        self.assertNotIn("Warning only because stop_hook_active", seen[0])
        self.assertNotIn("This Stop is REFUSED", seen[1])

    def test_a_SURVIVOR_disclosure_reaches_the_CONTINUING_exit_too(self):
        """A DISCLOSURE ARMED TO SURVIVE EVERY EXIT WAS DISCARDED ON ONE OF
        THEM. `publish_expired` passed `include_survivors=not stop_active`
        while `fallback_lines` clears `_SURVIVES_REFUSAL` UNCONDITIONALLY — so
        on a continuing stop the survivor queue was drained and never printed,
        and the next stop found it spent. Their own law is that they are true
        no matter which rung, if any, blocks.

        The armed queue and the warn line are unconditional controls: the
        queue is non-empty before the call and the coverage line is a second
        string that must also arrive, so an empty render cannot pass."""
        from helm import seats_stop_response
        seats_stop_seam._SURVIVES_REFUSAL.clear()
        self.addCleanup(seats_stop_seam._SURVIVES_REFUSAL.clear)
        survivor = "[helm stop-guard] I cannot see inside your own room"
        seats_stop_seam._SURVIVES_REFUSAL.append(survivor)
        self.assertEqual(seats_stop_seam._SURVIVES_REFUSAL, [survivor])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seats_stop_response.publish_expired(
                [], ["[helm stop-guard] COVERAGE UNKNOWN — fixture"],
                stop_active=True)
        self.assertEqual(rc, 0)
        self.assertIn(survivor, err.getvalue())
        self.assertIn("COVERAGE UNKNOWN", err.getvalue())
        self.assertEqual(seats_stop_seam._SURVIVES_REFUSAL, [],
                         "the queue was drained without being printed")

    def test_claim_evidence_failure_isolated_on_an_allow_exit(self):  # noqa: VACUOUS_ASSERTION — assert_called_once proves the crashing assessment path ran
        """The helper that owns failure isolation swallows an assessment crash.

        Allow-exit wiring is proved by the warning tests above; routing this
        property through every unrelated Stop rung made its result depend on
        process state and never proved the mocked assessment actually ran.
        """
        from helm import claimev
        with mock.patch.object(
                claimev, "assessment",
                side_effect=RuntimeError("broken advisory")) as assessment:
            warning = seats._claim_evidence_warning(
                "/missing", "main", "alice", "s-fail")
        self.assertIsNone(warning)
        assessment.assert_called_once_with("/missing")

    def test_a_repeating_watchdog_cannot_flood_the_inbox_block(self):
        """One unresolved condition must not become N obligations on screen —
        and now it cannot, because the block enumerates NO rows at all.

        A watchdog posts the SAME sentence every cycle and each repeat is a
        separate undelivered row. Measured on the live estate 2026-07-30: a
        seat reached 497 undelivered of which FOUR OF THE FIRST FIVE were a
        single idle-dispatch nag re-posted every 15 minutes for 37 hours, and
        the old block spent its bytes re-printing sample rows — so a real
        obligation at #300 was buried in the noise. Task 692 dropped the
        samples entirely: the owner never read them ("i dont think i ever read
        these stophooks", 2026-08-06), and the seat is told to `helm chat read`
        for the rows themselves. The block is now a COUNT plus two runnable
        verbs, so no volume of repeats can crowd anything out.

        The COUNT stays honest (they are still rows, still undelivered)."""
        seats.join(seat="alice", cwd="/tmp/p")
        for _ in range(4):
            chat.post("@alice STRANDED — dispatch abc123 to @bob", who="watchdog")
        chat.post("@alice the gate at deadbeef needs your verdict", who="carol")
        rc, _out, err = self.guard({"session_id": "s-dedupe"},
                                   args=["--seat", "alice"])
        # POSITIVE CONTROLS bracket the absence checks: the block fired (rc 2),
        # carries the honest count, and carries the runnable park verb — so err
        # is non-empty and the two assertNotIns below are meaningful.
        self.assertEqual(rc, 2)
        self.assertIn("5 undelivered", err, "the COUNT is still every row")
        self.assertIn("helm chat catchup --including-mentions --apply", err)
        # NO row body reaches the block — neither the flood nor the buried one,
        # because nothing is enumerated. The old bug was samples crowding the
        # real obligation; the cure is showing no samples, so neither appears.
        self.assertNotIn("STRANDED — dispatch abc123", err,
                         "the block must not enumerate rows (samples dropped)")
        self.assertNotIn("the gate at deadbeef", err,
                         "and so the distinct row cannot be crowded out either")

    def test_pending_mention_blocks_once_with_count_and_verbs(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice review the branch", who="bob")
        rc, _out, err = self.guard({"session_id": "s-1"}, args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered message(s)", err)
        self.assertIn("helm chat read", err)
        self.assertIn("helm chat catchup --including-mentions --apply", err)
        # the block no longer echoes the row body (task 692 dropped samples) —
        # the seat runs `helm chat read` for the rows themselves
        self.assertNotIn("bob: @alice review the branch", err)
        # the guard is a gate, not a delivery: the cursor never moved, the
        # tool-boundary lane still delivers the row afterwards (POSITIVE control
        # that the block did not consume it)
        self.assertIn("review the branch", seats.deliver(seat="alice"))

    def test_same_fingerprint_second_stop_passes(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice go", who="bob")
        rc, _o, _e = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 2)                    # first stop on this set blocks
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)               # same rows: pass — never a loop
        self.assertNotIn("inbox clean", err)       # latched ≠ clean — no false warn

    def test_new_row_after_a_passed_stop_blocks_again(self):  # noqa: VACUOUS_ASSERTION — the latched rc==0 middle stop is an intentional absence, bracketed by unconditional positive controls: the first stop asserts rc 2, and the new-row stop asserts rc 2 AND "2 undelivered message(s)" on err
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice one", who="bob")
        self.assertEqual(self.guard(args=["--seat", "alice"])[0], 2)
        self.assertEqual(self.guard(args=["--seat", "alice"])[0], 0)  # latched
        chat.post("@alice two", who="bob")         # NEW pending set
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        # the new row re-armed the block; it no longer echoes the row body
        # (task 692), so the COUNT rising to 2 proves the new set is what fired
        self.assertIn("2 undelivered message(s)", err)

    def test_held_lease_blocks_naming_the_resource(self):
        ok, _m, _l = seats.claim("worktree-main", "alice", ttl=60, session="s-9")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-9"})
        self.assertEqual(rc, 2)
        self.assertIn("worktree-main", err)          # the held lane is NAMED
        self.assertIn("act per line, then stop:", err)
        # a DIFFERENT session holds nothing — no claims block
        rc, _o, err = self.guard({"session_id": "s-other"})
        self.assertEqual(rc, 0, err)

    def test_held_lease_blocks_once_per_set_then_compresses(self):  # noqa: VACUOUS_ASSERTION — rc 2 then the compressed-line assertIn are the unconditional positive controls; the two assertNotIns pin what must stay down
        """The claim-lease rung LATCHES like every other block — since
        2026-08-04, when the owner surfaced the old behaviour as the defect:
        the identical multi-line wall printed on two consecutive stops of an
        UNCHANGED held set. (This test previously pinned that behaviour by
        name, test_held_lease_reblocks_every_stop_it_is_NOT_latched, and the
        docs it bound have been updated in the same commit — the doc sentence
        stays bound to behaviour, in its new polarity.)

        One full block per held-set fingerprint (resource + lease id + TTL
        band); the re-stop compresses to a one-line WARN that still carries
        the count; the release empties the arm entirely. The detailed spec's
        'positive proof only, re-verified every stop' stays true of the
        CLASSIFICATION — the latch governs emission only.
        tests/test_stop_lease_latch.py owns the full latch contract
        (changed-set re-print, TTL escalation, unwritable-latch degrade)."""
        ok, _m, lease = seats.claim("port:9931", "alice", ttl=600,
                                    session="s-nl")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 2)
        self.assertIn("port:9931", err)
        # SAME state, second stop: compresses to one warn line, stop passes
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged. Reprint:", err)
        self.assertNotIn("run the EXACT command", err)   # the wall stayed down
        # the cure is still the release — afterwards the arm says nothing
        ok, _m = seats.release("port:9931", "alice", lease=lease,
                               session="s-nl")
        self.assertTrue(ok)
        rc, _o, err = self.guard({"session_id": "s-nl"})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("lease(s) held", err)

    def test_both_blockers_surface_in_one_exit_2(self):
        seats.join(session="s-9", seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        seats.claim("worktree-main", "alice", ttl=60, session="s-9")
        rc, _o, err = self.guard({"session_id": "s-9"}, args=["--seat", "alice"])
        self.assertEqual(rc, 2)
        self.assertIn("undelivered", err)          # the whole picture in ONE shot
        self.assertIn("worktree-main", err)

    def test_clean_stop_passes_with_beacon_warn(self):
        seats.join(seat="bob", cwd="/tmp/p")
        rc, _o, err = self.guard(args=["--seat", "bob"])
        self.assertEqual(rc, 0)
        self.assertIn("helm chat wait --seat bob --follow", err)  # the arm line
        self.assertIn("Monitor", err)

    def test_kill_switches(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        os.environ["HELM_STOP_GUARD"] = "0"
        rc, _o, err = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")                  # off = silent, always
        os.environ.pop("HELM_STOP_GUARD")
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"  # per-check off
        rc, _o, _e = self.guard(args=["--seat", "alice"])
        self.assertEqual(rc, 0)
        os.environ.pop("HELM_STOP_GUARD_INBOX")
        seats.claim("worktree-x", "alice", ttl=60, session="s-9")
        os.environ["HELM_STOP_GUARD_CLAIMS"] = "0"
        rc, _o, _e = self.guard({"session_id": "s-9"})
        self.assertEqual(rc, 0)

    def test_stop_hook_active_never_reblocks(self):
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice pending", who="bob")
        rc, _o, err = self.guard({"stop_hook_active": True}, args=["--seat", "alice"])
        self.assertEqual(rc, 0, err)               # the harness is already continuing

    def test_an_UNPROVEN_lane_lease_is_held_but_release_is_NOT_prescribed(self):
        """#66. A lease on a lane worktree cannot be proven idle, and the guard
        was prescribing release as though it had been.

        MEASURED on the integrator's live work-peek lease, 2026-08-01:
        _delegated_build returns None and BOTH its proofs are unreachable for
        that delegation shape — 0 live processes have cwd inside the claimed
        room (a subagent inherits its parent's cwd; it does not chdir into the
        lane) and the documented-subagent activity file was never written.
        UNKNOWN, not idle.

        The BLOCK is unchanged: widening an exemption on absent evidence is
        the fleet-wide un-guarding the delegation contract forbids by name.
        What changes is that a guard may not prescribe a DESTRUCTIVE action on
        a premise it could not establish.

        #112 REPLACED THE PREMISE ITSELF. The caveat used to ask about
        DELEGATION, which is both unanswerable and — measured over four live
        holds — the right basis 1 time in 4. It now reports the four reads of
        UNFINISHED WORK BOUND TO THE ROOM. This fixture's lease resolves to
        no lane room at all (planted from the test's own cwd), so the honest
        answer is FOUR missed reads and a withheld command — a strictly
        stronger version of the same refusal to prescribe.

        THE ALL-WITHHELD END of the sermon's range. The header used to
        promise "run the EXACT command shown" unconditionally — instructing
        a seat to run something no line contained. It no longer asserts
        anything about the lines at all (see the MIXED arm for why a
        per-stop promise was the wrong repair), so what is pinned here is
        that the single LINE withholds and explains itself."""
        seats.claim("worktree:helm:some-lane", "alice", ttl=60, session="s-66")
        blocks, _w = seats.stop_guard(session="s-66", seat="alice")
        self.assertTrue(blocks, "an unproven lane lease must still hold")
        text = " ".join(blocks)
        # THE BLOCKED STOP'S SPELLING. The four-read breakdown is the long
        # form and lives behind `helm chat stop-guard --detail`; what the owner
        # reads is the ruling and the next act.
        self.assertIn("4 of 4 reads failed", text)
        self.assertIn("IDLE is unproven", text)
        self.assertIn("check the lane by hand", text)
        self.assertIn("act per line, then stop:", text)
        self.assertNotIn("run the EXACT command shown", text)
        self.assertNotIn("helm work release", text)
        self.assertNotIn("delegation is UNKNOWN, not disproven", text)

    def test_a_NON_lane_lease_keeps_its_plain_release_advice(self):
        """The control, and the reason this is scoped rather than blanket: a
        lease that names no worktree has no subagent to strand, so its advice
        stays exactly as it was. A caveat on every lease is a caveat nobody
        reads.

        landlock, not gatelock, on purpose: a fresh gatelock lease sits
        above the renewer floor and takes the renewed arm (stop allowed),
        so it was never this control's subject — with it, the old
        assertions passed on the RENEWED hint, which contains no release
        advice at all. The tightened assertIn is the positive control the
        old shape lacked."""
        seats.claim("landlock:helm", "alice", ttl=60, session="s-66b")
        blocks, _w = seats.stop_guard(session="s-66b", seat="alice")
        self.assertTrue(blocks)
        joined = " ".join(blocks)
        self.assertIn("helm chat release landlock:helm", joined)
        self.assertNotIn("mid-build", joined)

    def test_a_row_arriving_MID_TURN_is_still_SURFACED_never_blocked(self):
        """#74. stop_hook_active must stop the guard BLOCKING, not stop it
        LOOKING — and the old short-circuit did both.

        MEASURED COST: a verdict reached the reviewed seat mid-turn at
        18:05 on 2026-08-01; that turn ended at 18:11 with stop_hook_active
        set, so the guard returned before reading the inbox. The row was never
        shown and never latched, and the fleet idled 5.5 hours waiting on the
        land that verdict authorized.

        Blocking under stop_hook_active is the infinite-loop shape. A WARN is
        not: it never re-enters the stop path."""
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice a verdict that landed mid-turn", who="bob")
        blocks, warns = seats.stop_guard(session=None, seat="alice",
                                         stop_active=True)
        self.assertEqual(blocks, [], "stop_hook_active must never re-block")
        self.assertTrue(warns, "the mid-turn row was not surfaced at all — "
                        "this is the 5.5h hole")
        self.assertIn("arrived DURING this turn", " ".join(warns))

    def test_stop_active_with_an_EMPTY_inbox_stays_silent(self):
        """The control for the arm above: the new read must not manufacture a
        warn on every continued turn, or the signal is noise within a day."""
        seats.join(seat="quiet", cwd="/tmp/p")
        blocks, warns = seats.stop_guard(session=None, seat="quiet",
                                         stop_active=True)
        self.assertEqual((blocks, warns), ([], []))
        # Unconditional control on the SAME observable: the very same call
        # DOES speak once a row exists, so the silence above is a decision
        # rather than a rung that never fires.
        chat.post("@quiet now there is one", who="bob")
        _b, w2 = seats.stop_guard(session=None, seat="quiet", stop_active=True)
        self.assertTrue(w2)

    def test_stop_active_honors_the_inbox_kill_switch(self):
        """A disabled inbox check performs no room scan on either Stop path."""
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"
        with mock.patch.object(seats, "_pending_all",
                               side_effect=AssertionError("scanned inbox")):
            self.assertEqual(seats.stop_guard(
                session=None, seat="quiet", stop_active=True), ([], []))

    def test_garbage_stdin_fails_open(self):
        rc, _o, _e = self.guard(b"not json{{")
        self.assertEqual(rc, 0)


class GuardrailTextTrim692Test(SeatsBase):
    """Task 692 (owner canon: context-bloat fixes are always P0). The
    stop-guard inbox block fires at EVERY blocked stop and the SessionStart
    join banner at every session start — guard TEXT a live seat loads
    per-firing — so their verbosity is a recurring tax. These arms pin the
    trim: every load-bearing token (the count, the runnable verbs, the beacon
    directive) survives, the per-firing boilerplate (sample rows, thrice-said
    rationale) is gone, and each surface sits under a byte ceiling the verbose
    version cannot meet. The guard's DECISION is untouched — proven by the
    still-blocks arm and the deliver-after invariant — only its VERBOSITY.

    The ceilings are the mutation seam: reverting either trim reddens the
    len<=ceiling arm (the old block reached 839-1548B, the old banner 1108B),
    AND the boilerplate-absence arms (the sampled row body / the dropped
    rationale reappear). Every absence arm is bracketed by a MUST-HIT."""

    def guard(self, payload=None, args=()):
        stdin = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return self.cmd("stop-guard", ["--hook-json", *args], stdin=stdin or b"{}")

    def test_inbox_block_is_trimmed_but_keeps_the_count_and_both_verbs(self):
        seats.join(session="s-t", seat="alice", cwd="/tmp/p")
        for i in range(6):
            chat.post("@alice a fairly long obligation sentence number %d that "
                      "would once have been sampled verbatim into the block" % i,
                      who="sender-%d" % i)
        blocks, _w = seats.stop_guard(session="s-t", room="main", seat="alice")
        inbox = [b for b in blocks if "undelivered message(s)" in b]
        self.assertEqual(len(inbox), 1, blocks)
        b = inbox[0]
        # MUST-HITs — the whole payload the trim keeps
        self.assertIn("6 undelivered message(s)", b)         # honest raw count
        self.assertIn("helm chat read", b)                    # see them
        self.assertIn("helm chat catchup --including-mentions --apply", b)  # park
        # boilerplate gone: no posted row body is echoed into the block
        self.assertNotIn("would once have been sampled verbatim", b)
        # and the whole block fits under a ceiling the verbose block (839-1548B)
        # cannot meet — reverting the trim reddens this line
        self.assertLessEqual(len(b.encode("utf-8")), 400,
                             "the trimmed inbox block grew past its ceiling:\n" + b)

    def test_inbox_block_size_is_constant_regardless_of_pending_volume(self):
        # samples were the only volume-dependent part; with them gone the block
        # is the same length for 2 rows as for 40 (only the count digits + seat
        # name vary). A revert reintroduces up to 5 sample lines + a "+N more"
        # tail, and this equality breaks.
        seats.join(session="s-a", seat="al", cwd="/tmp/p")
        chat.post("@al one", who="bob"); chat.post("@al two", who="bob")
        small = [x for x in seats.stop_guard(
            session="s-a", room="main", seat="al")[0]
            if "undelivered message(s)" in x][0]
        seats.join(session="s-b", seat="bl", cwd="/tmp/p")
        for i in range(40):
            chat.post("@bl row %d with a distinct sender and body" % i,
                      who="w%d" % i)
        big = [x for x in seats.stop_guard(
            session="s-b", room="main", seat="bl")[0]
            if "undelivered message(s)" in x][0]
        # POSITIVE CONTROLS: both blocks actually fired and carry the payload,
        # so the equality below is comparing two real, non-empty blocks (never
        # two empty strings that would match vacuously).
        self.assertIn("2 undelivered message(s)", small)
        self.assertIn("40 undelivered message(s)", big)
        self.assertIn("helm chat catchup --including-mentions --apply", small)
        import re as _re
        norm = lambda s: _re.sub(r"\d+ undelivered", "N undelivered",
                                 _re.sub(r"for seat '[^']*'", "for seat 'S'", s))
        self.assertEqual(
            norm(small), norm(big),
            "the block length still depends on pending volume — the samples "
            "were not fully dropped")

    def test_inbox_block_still_blocks_on_real_pending(self):
        # the DECISION is unchanged: a real undelivered @mention still refuses
        # the stop (rc 2). This is the positive control that the text trim did
        # not weaken the guard.
        seats.join(seat="alice", cwd="/tmp/p")
        chat.post("@alice a genuine obligation", who="bob")
        rc, _o, err = self.guard({"session_id": "s-fires"}, args=["--seat", "alice"])
        self.assertEqual(rc, 2, err)
        self.assertIn("undelivered message(s)", err)

    def test_join_banner_is_trimmed_but_keeps_the_beacon_directive(self):
        _seat, line = seats.join(seat="codex", cwd="/tmp/p")
        # MUST-HITs — identity + the exact copy-pasteable beacon directive
        self.assertIn("seat 'codex'", line)
        self.assertIn("First action", line)
        self.assertIn('Monitor(command: "helm chat wait --seat codex --follow", '
                      'timeout_ms: 1800000)', line)
        self.assertIn('ToolSearch(query: "select:Monitor")', line)
        from helm.seats_common import GUIDE_PATH
        self.assertIn(GUIDE_PATH, line)
        self.assertIn("NEW_AGENT_GUIDE.md", line)
        # the thrice-said rationale is gone
        self.assertNotIn("you must never report it as one", line)
        self.assertNotIn("the ONLY way an idle session ever wakes", line)
        # fits under a ceiling the verbose banner (~1108B) cannot meet
        self.assertLessEqual(len(line.encode("utf-8")), 750,
                             "the trimmed join banner grew past its ceiling:\n" + line)


class StopGuardGatePendingTest(SeatsBase):
    """STAGES 2+3 of the claims-block exemption: LEASE-HELD-WHILE-GATE-
    PENDING (measured live 2026-07-29 — three seats force-continued all
    evening on gate-pending leases; release opens the lane mid-gate,
    finishing is the reviewer's move) and LEASE-HELD-AWAITING-LAND
    (measured live 2026-07-31, bug class verb-designed-noun-lifecycle-
    holed: lane lr-land-ack-deletions-reachable sat APPROVED with a
    VERIFIED gate and every stop still exited 2 — the exemption died the
    stage after it fired). The proof is TWO-PART — lane correspondence
    (stem-family, premise lane-names-are-a-family-probe-the-stem) AND a
    ref↔HEAD match — against a kind=review dispatch row that is OPEN, or
    VERDICT polarity=approve with a BOUND gate token. Neither half alone:
    lane name without the ref is a naming coincidence, ref without the
    lane is a branching coincidence (measured live 2026-07-31: a fresh
    lane branched at an under-review tip inherited ANOTHER lane's gate).
    UNKNOWN → BLOCK at every rung, recomputed per stop; fix/supersede/
    ungated verdicts re-block."""

    def setUp(self):
        super().setUp()
        # scratch project + lane room, anchored from the project root —
        # the SAME pure functions helm work mints leases with
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        # THIS FIXTURE IS ITS OWN PROJECT: the dispatch write door would
        # otherwise refuse every row here as FOREIGN.
        from tests._tmphome import helm_tree, pin_admission, pin_dispatch_home
        self._real_home_repo_id = pin_dispatch_home(self, self.root)
        # `plant` mints through gate.run: admission on a fixture box, never
        # this node's live cap (task/1740).
        pin_admission(self)
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        helm_tree(self, self.root)
        # one repo, one real git worktree: the dispatch ref must be a real
        # commit (the writer validates), and the room's HEAD must equal it
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "tip"], check=True, capture_output=True,
                       timeout=30)
        self.head = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        self.wt = self.root + "-wt/lane-g"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-g", self.wt],
                       check=True, capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-g"
        self.sid = "s-gate-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self._ledger = dispatches.ledger_path
        self.ledger = os.path.join(self.tmp, "dispatches.jsonl")
        dispatches.ledger_path = lambda: self.ledger

    def tearDown(self):
        dispatches.ledger_path = self._ledger
        os.chdir(self._cwd)
        super().tearDown()

    def guard(self, payload):
        import json as _json
        stdin = _json.dumps(payload).encode()
        return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def _plant_foreign_ref(self, verdict=False):
        """A REAL review row whose ref is a real commit that is NOT the
        worktree's HEAD (a moved-head / other-lane gate)."""
        other = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD~0"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        subprocess.run(["git", "-C", self.root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "second"], check=True, capture_output=True,
                       timeout=30)
        newer = subprocess.run(
            ["git", "-C", self.root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            timeout=30).stdout.strip()
        assert newer != self.head
        return self.plant(ref=newer, verdict=verdict)

    def plant(self, ref=None, kind="review", recipient="ds4pro",
              lane="lane-g", repo=None, verdict=False, force=False):  # noqa: SEAT_NAME — moved whole; this exact line is already tracked in tests/test_seats.py, so the move is not a new copy
        """A REAL ledger row through the real writer (the projection's
        grammar is v3/seq-0; hand-planted JSONL is a fiction the replay
        discards). Rows land in the swapped ledger path from setUp.
        verdict=True mints a BOUND approve; verdict="fix" records a fix
        verdict (token-free evidence — mark_verdict accepts that, only an
        APPROVE demands a verified gate)."""
        # the real writer now refuses an identityless author (floor-is-never-
        # an-author, 2026-08-02); the planting identity is SCOPED to the write
        # so the stop-guard under test still runs exactly as un-named as the
        # hook process it models
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "gate-fixture"}):
            row = dispatches.add(recipient, lane, ref=ref or self.head,
                                 kind=kind, notify=False,
                                 repo=repo or self.root, new_work=True,
                                 force=force)
        self.assertIsNotNone(row, "plant: the real writer refused")
        # delivery OBSERVED, or the stop-whisper's needs-confirmation rung
        # blocks every test here for a reason orthogonal to the claims block
        dispatches._mark_delivered(row["id"], ref or self.head)
        if verdict == "fix":
            out, verr = dispatches.mark_verdict(
                row["id"], ref or self.head, "rework: see review notes",
                polarity="fix")
            self.assertIsNone(verr, "plant fix verdict: %s" % verr)
        elif verdict:
            # BIND the approve — the writer refuses an ungated one outright
            # (an ungated approve can never authorize a landing). Mint a
            # real receipt at the root repo's HEAD; every approve caller
            # reviews that same commit (self.head, or _plant_foreign_ref's
            # fresh tip, which moves root HEAD with it), so it binds. Replace
            # only queued child execution: gate.run retains its canonical
            # serial argv and interpreter rather than minting custom authority.
            from helm import gate as _gate
            with serial_process():
                g, gerr = _gate.run(repo=self.root)
            self.assertIsNone(gerr, "plant gate: %s" % gerr)
            out, verr = dispatches.mark_verdict(
                row["id"], ref or self.head, _gate.evidence_line(g),
                polarity="approve")
            self.assertIsNone(verr, "plant verdict: %s" % verr)
        return row

    def test_open_review_dispatch_matching_HEAD_exempts_and_SAYS_so(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)
        self.assertIn("ds4pro", err)  # noqa: SEAT_NAME — moved whole; this exact line is already tracked in tests/test_seats.py, so the move is not a new copy
        self.assertIn("lease retained", err)

    def test_an_ABBREVIATED_ref_matches_the_full_HEAD(self):
        """The ledger stores the ref as typed at dispatch time — measured
        live: kimi's own gate row carried a22d98d for a full head."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(ref=self.head[:7])
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)
        self.assertIn("lane in gate", err)

    def test_a_BOUND_approve_matching_HEAD_exempts_awaiting_land(self):
        """STAGE 3 (was test_a_BOUND_verdict_reblocks_the_very_next_stop,
        which pinned the lifecycle hole itself: the exemption lapsed the
        moment the reviewer APPROVED, exactly when the holder could least
        act — measured live 2026-07-31, every stop rc 2 on a lane whose
        only remaining verb was the integrator's land)."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(verdict=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIns below are the positive controls on err
        self.assertIn("approved", err)
        self.assertIn("awaiting the land window", err)
        self.assertIn("lease retained", err)

    def test_a_FIX_verdict_does_NOT_exempt_rework_is_owed(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(verdict="fix")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)
        self.assertNotIn("awaiting the land window", err)

    def test_an_APPROVE_at_a_foreign_ref_does_NOT_exempt(self):
        """The ref↔HEAD match is the proof for the approved arm too — an
        approve of some OTHER commit says nothing about this worktree."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self._plant_foreign_ref(verdict=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)
        self.assertNotIn("awaiting the land window", err)

    def test_a_ref_mismatch_blocks_moved_head_is_new_work(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(ref="f" * 40) if False else self._plant_foreign_ref()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_build_kind_row_never_exempts(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(kind="build")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_no_ledger_at_all_blocks(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_two_live_gates_for_one_head_are_ambiguous_and_block(self):
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        self.plant(force=True)
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_lane_NAME_alone_is_never_evidence(self):
        """A review row for the same lane NAME but a different ref must
        not exempt — the ref↔HEAD match is the only proof."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self._plant_foreign_ref()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertNotIn("in gate", err)

    def test_a_ref_alone_is_never_evidence_foreign_lane_same_tip_blocks(self):
        """The other half of the coincidence: every fresh lane starts at
        some existing tip, so ANOTHER lane's review row at the same ref
        proves nothing about THIS lane (measured live 2026-07-31 — lane
        approved-lane-exempts-stop, freshly branched at an unlanded stack
        tip, was exempted by verdict-polarity-consolidated's row 1267f509
        purely because the refs coincided)."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(lane="prospero")   # same ref (HEAD), different family
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn(self.res, err)
        self.assertNotIn("in gate", err)

    def test_a_round_suffixed_lane_row_is_the_same_family_and_exempts(self):
        """Premise lane-names-are-a-family-probe-the-stem: a -review/-rN/
        -xrev rename is the SAME work family, so the correspondence check
        must not reintroduce exact-string blindness."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant(lane="lane-g-review-r2")
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIn below is the positive control on err
        self.assertIn("in gate", err)

    def test_the_ALLOW_exit_carries_the_advisory_and_no_block_text(self):
        """Emission routing, allow side: an advisory-only state exits 0
        with the advisory, and nothing block-shaped rides along."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 0, err)  # noqa: VACUOUS_ASSERTION — rc 0 is the allow; the assertIn below is the positive control on err
        self.assertIn("lease retained", err)
        self.assertNotIn("act per line, then stop:", err)

    def test_a_blocking_lease_keeps_advisories_OFF_the_exit2_emission(self):
        """Emission routing, block side: Claude Code renders EVERY exit-2
        stop-hook emission as a red "Stop hook error:", so an advisory
        printed beside a block turns "stop allowed, lease retained" into
        error text — owner-surfaced, and the reason a block carries no
        reassurance PROSE. What it does carry, per task/2388, is one
        scannable NO ACTION OWED line per exempt lane, because an absent
        lane reads as a released one."""
        seats.claim(self.res, "alice", ttl=60, session=self.sid)
        self.plant()          # this lease is exempt: advisory, not a demand
        seats.claim("worktree:proj:ghost", "alice", ttl=60,
                    session=self.sid)   # no lane room: UNKNOWN, blocks
        rc, _o, err = self.guard({"session_id": self.sid})
        self.assertEqual(rc, 2)
        self.assertIn("worktree:proj:ghost", err)
        self.assertNotIn("lease retained", err)
        self.assertNotIn("in gate", err)


class StopGuardExemptStateIsPrintedTruthTest(SeatsBase):
    """task/2388 round one — what a stop PRINTS about an exempt lease, read off
    the hook verb rather than off the accumulators.

    THE ROUND-ONE FINDINGS BOTH LIVED WHERE THE EXISTING CONTROLS COULD NOT
    LOOK. `seats.stop_guard` returns (blocks, warns); every latch arm reads
    those, which is construction, not delivery. What a seat actually reads is
    one published stream, and on a mixed set the exempt lane's whole account is
    the one indented sermon line — which carried a state word the reason
    sentence contradicted (finding 2) and a TTL the latch did not fingerprint
    (finding 3). These arms drive `stop-guard --hook-json` the way Claude Code
    does and assert on stderr.

    The exempt shape planted here is the GIVER branch — my session minted the
    lease, another seat holds it — because it is the one exemption needing no
    live process and no lane room, so the roster tri-state and the TTL are the
    only variables.

    AN UNREADABLE ROSTER ALSO COSTS THE LATCH, measured here: `seat_incarnation`
    answers None off a roster that will not parse, so `_write_stop_latch`
    refuses and the sermon degrades to a WARN (the refusal-honesty law). So on
    an UNKNOWN-owner stop the block-shaped sermon and the full sentence arrive
    together rather than the sentence being dropped — the state word was false
    either way, and a block raised by any other rung still discards both
    (task/2390).
    """

    ME = "alice"
    HELD = "db-migration"          # holder is me: a plain held lease
    GIVEN = "cache-rebuild"        # my session, someone else's hold
    PEER = "peer-seat"
    PEER_SESSION = "99998888-7777-6666-5555-444433332222"

    def setUp(self):
        super().setUp()
        self.sid = "s-2388-" + os.urandom(6).hex()
        # The INBOX rung reads the roster far upstream (stop_guard ->
        # _pending_all -> seat_scope -> roster), so an unreadable roster would
        # fail THERE and the claims rung's own tri-state would go unmeasured.
        # Same fence, same reason, as test_lease_recovery's UNKNOWN arm.
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"

    def guard(self):
        stdin = json.dumps({"session_id": self.sid}).encode()
        return self.cmd("stop-guard", ["--hook-json", "--seat", self.ME],
                        stdin=stdin)

    def claim(self, res, holder=None, ttl=600):
        ok, msg, _lease = seats.claim(res, holder or self.ME, ttl=ttl,
                                      session=self.sid)
        self.assertTrue(ok, msg)

    def roster_reads(self):
        """A roster NAMING the peer's own live session: ownership is PROVEN."""
        pk.write_json(seats.roster_path(),
                      {self.PEER: {"session": self.PEER_SESSION}})

    def roster_unreadable(self):
        """UNKNOWN, never proven-empty — the tri-state the giver branch turns
        on. A missing roster reads as an empty one and takes another path."""
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this file is not json")

    def decay(self, res, left):
        """Advance the clock against ONE row. Its lease token never changes,
        which is the half of the fingerprint that must not carry the
        crossing."""
        c = pk.read_json(seats.claims_path(), {}) or {}
        c[res]["exp_mono"] = seats._now_mono() + left
        pk.write_json(seats.claims_path(), c)

    def sermon_line(self, res, text):
        """The ONE line the sermon prints about that lane. The sermon body is
        indented; the exempt lane's full sentence rides its own line, so this
        reads the account a mixed set publishes and nothing else."""
        hit = [line for line in text.splitlines()
               if line.startswith("  ") and res in line]
        self.assertEqual(len(hit), 1, text)      # must-hit: the lane printed
        return hit[0]

    def sentence(self, res, text):
        hit = [line for line in text.splitlines()
               if line.startswith("[helm stop-guard] lease " + res)]
        self.assertEqual(len(hit), 1, text)      # must-hit
        return hit[0]

    def test_an_unreadable_roster_prints_UNKNOWN_never_PEER_HOLD(self):
        """FINDING 2. The reason sentence says the owner is UNKNOWN and the
        state word said PEER HOLD — the guard asserting KNOWN peer ownership of
        a lease whose owner it could not name, on the line that IS the lane's
        whole account in the sermon. A negative may not share a value with a
        measured answer."""
        self.claim(self.HELD)
        self.claim(self.GIVEN, holder=self.PEER)
        self.roster_unreadable()
        rc, _out, err = self.guard()
        # POSITIVE CONTROLS: the sermon fired on the mixed set, and the HELD
        # lane is in it with its own exact command.
        self.assertIn("act per line, then stop:", err)
        self.assertIn("helm chat release " + self.HELD, err)
        # an unwritable latch degrades the sermon to a warn, so this stop is
        # ALLOWED — the refusal-honesty law, not this finding.
        self.assertEqual(rc, 0, err)
        # THE FINDING, in the sermon's own line about the given lease.
        line = self.sermon_line(self.GIVEN, err)
        self.assertIn("NO ACTION OWED", line)
        self.assertIn("UNKNOWN", line,
                      "an ownership the guard could not read printed as a "
                      "measured state")
        self.assertNotIn("PEER HOLD", err,
                         "UNKNOWN ownership rendered as proven peer ownership")
        # THE EXEMPTION IS INTACT: no release command for someone else's hold.
        self.assertIn("UNKNOWN", self.sentence(self.GIVEN, err))
        self.assertNotIn("release " + self.GIVEN, err)

    def test_losing_the_owner_is_a_CHANGED_set_and_never_compresses(self):
        """FINDING 2, the latch half. PEER HOLD and OWNER UNKNOWN are different
        states, so one fingerprint may not cover both: the stop that LOSES the
        answer must re-print the set, never report it unchanged. The first stop
        latches under a readable roster, so the compressed branch is reachable
        — and under the old fingerprint it was what this stop printed."""
        self.claim(self.HELD)
        self.claim(self.GIVEN, holder=self.PEER)
        self.roster_reads()
        rc, _out, first = self.guard()
        self.assertEqual(rc, 2, first)                   # control: latched
        self.assertIn("PEER HOLD", self.sermon_line(self.GIVEN, first))
        self.roster_unreadable()          # same lease, ownership now UNKNOWN
        rc2, _out2, second = self.guard()
        self.assertNotIn("unchanged. Reprint:", second,
                         "the stop that lost the owner reported the set "
                         "unchanged — one fingerprint covered a proven peer "
                         "hold and an UNKNOWN one")
        self.assertIn("act per line, then stop:", second)
        self.assertEqual(rc2, 0, second)       # unwritable latch -> warn
        line = self.sermon_line(self.GIVEN, second)
        self.assertIn("OWNER UNKNOWN", line)
        self.assertNotIn("PEER HOLD", second)

    def test_an_exempt_lease_crossing_the_alarm_reprints_and_is_tallied(self):
        """FINDING 3. Held steady, the exempt lease's token unchanged, the
        clock past LEASE_TTL_ALARM_S: the exempt fingerprint hashed no band, so
        the set looked unchanged and the crossing that the compressed line
        PROMISES a reprint for was compressed away — no reprint, and no
        EXPIRING in the tally."""
        self.claim(self.HELD)                          # 600s, steady
        self.claim(self.GIVEN, holder=self.PEER)       # 600s, exempt
        self.roster_reads()
        rc, _out, first = self.guard()
        self.assertEqual(rc, 2, first)                              # control
        self.assertIn("PEER HOLD", self.sermon_line(self.GIVEN, first))
        rc2, _out2, second = self.guard()            # unchanged -> compresses
        self.assertEqual(rc2, 0, second)
        self.assertIn("unchanged. Reprint:", second)   # control
        self.assertIn("+1 exempt", second)                          # control
        self.assertNotIn("EXPIRING", second)
        self.decay(self.GIVEN, 60)                  # THE CROSSING, exempt only
        rc3, _out3, third = self.guard()
        self.assertEqual(rc3, 2, third)
        self.assertIn("act per line, then stop:", third,
                      "an exempt lease crossed the alarm and the latch "
                      "compressed it away: the fingerprint carried no band")
        line = self.sermon_line(self.GIVEN, third)
        self.assertIn("EXPIRING", line)
        self.assertIn("NO ACTION OWED", line)       # accounting, not a demand
        # the held lease is steady, so the severity is the exempt lane's alone
        self.assertNotIn("EXPIRING", self.sermon_line(self.HELD, third))
        rc4, _out4, fourth = self.guard()           # and the tally counts it
        self.assertEqual(rc4, 0, fourth)
        self.assertIn("unchanged. Reprint:", fourth)   # control
        self.assertIn("(1 EXPIRING)", fourth)

    def test_the_allow_exit_sentence_names_the_TTL_it_re_fired_on(self):
        """FINDING 3, the warn channel. An all-exempt stop publishes the full
        sentence and nothing else, and that sentence carried no TTL at all — so
        a re-print driven by a crossing read as a verbatim repeat."""
        self.claim(self.GIVEN, holder=self.PEER, ttl=60)
        self.roster_reads()
        rc, _out, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("not yours to release", err)      # control: the sentence
        self.assertIn("s left, EXPIRING", self.sentence(self.GIVEN, err))


class StopGuardRenewingLeaseTest(SeatsBase):
    """A lease whose remaining TTL RISES (or holds flat) between two reads
    has a live renewer behind it — the gate legacy-lock daemon refreshes
    gatelock:<project> every 5s against a 30s TTL (gate.py
    _GATE_LEGACY_RENEW_S/_GATE_LEGACY_TTL_S), so a healthy in-flight gate
    lease displays ~26-29s left INDEFINITELY. The claims block printed that
    number beside a pre-filled release command and led with "run the EXACT
    command shown": a number that looks like an expiry plus a command
    presented as the remedy made releasing the correct-looking act, and
    releasing the runner's lock kills the suite mid-run — measured live
    twice in one day (caught only by sampling twice and seeing
    the remainder go UP; kimi 18:29Z gate #29, released, renewer failed
    "gatelock:helm is not claimed; suite killed", gate closed UNKNOWN).
    Only a strictly DECREASING remainder is a real strand; a renewing lease
    must NAME the live run instead of offering the command that breaks it.
    One read cannot tell renewing from lapsing, so the guard reads the
    claims file twice with a short pause; a slow hook is fine — a killed
    suite is not."""

    def setUp(self):
        super().setUp()
        self.sid = "s-renew-" + os.urandom(6).hex()

    def guard(self):
        import json as _json
        stdin = _json.dumps({"session_id": self.sid}).encode()
        return self.cmd("stop-guard", ["--hook-json"], stdin=stdin)

    def _expire_in(self, resource, seconds):
        """Point the stored row's expiry at now+seconds — the shape a live
        renewer (near full TTL) or a genuinely lapsing lease (near 0)
        presents, without sleeping the test for either."""
        with seats._flocked(seats.claims_path() + ".lock"):
            c = pk.read_json(seats.claims_path(), {}) or {}
            c[resource]["exp_mono"] = seats._now_mono() + seconds
            c[resource]["exp_wall"] = time.time() + seconds
            pk.write_json(seats.claims_path(), c)

    def test_renewing_lease_allows_the_stop_and_retains_the_lease(self):
        # production shape: renewed every 5s against a 30s TTL, the row a
        # stop actually meets shows ~24-29s remaining — never near 0.
        # The renewal is proof of an OUT-OF-TURN holder, so the stop is
        # ALLOWED (a blocked stop only forces a poll loop against the
        # seat's own suite — three in one session, 2026-08-03); what the
        # b4485c30 fix established is RETAINED: no release command.
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 27.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("RENEWED", err)
        self.assertIn("Stop allowed, lease retained", err)
        self.assertNotIn("act per line, then stop:", err)
        self.assertNotIn("helm chat release", err)
        # AND THE LEASE SURVIVED THE ALLOWED STOP — retained, not released
        c = pk.read_json(seats.claims_path(), {}) or {}
        self.assertIn("gatelock:proj", c)

    def test_decreasing_lease_keeps_the_release_command(self):
        # a genuinely lapsing lease: minted 30s and left alone, it falls
        # toward 0 — THIS is the strand the release command exists for
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 20.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)
        self.assertNotIn("RENEWED", err)

    def test_kill_switch_restores_the_release_command(self):
        os.environ["HELM_STOP_GUARD_LEASE_TTL"] = "0"
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 27.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)
        self.assertNotIn("RENEWED", err)

    def test_manually_claimed_gatelock_fails_safe_then_falls_through(self):
        # a MANUAL gatelock claim has no renewer: it reads as "renewed"
        # only while its remainder is still near the full TTL (the first
        # ~6s at 30/5), then falls through to the release command — the
        # conservative direction throughout, and DECLARED rather than
        # discovered (observation 1 on b4485c30's review). The first phase now
        # ALLOWS the stop (an allowed stop cannot worsen a lease that
        # self-expires within one TTL); it still withholds the release
        # command, which is the half that kills suites.
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        self._expire_in("gatelock:proj", 25.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 0, err)
        self.assertIn("RENEWED", err)          # fails safe: withholds
        self.assertNotIn("helm chat release", err)
        self._expire_in("gatelock:proj", 23.0)
        rc, _o, err = self.guard()
        self.assertEqual(rc, 2, err)
        self.assertIn("helm chat release gatelock:proj", err)

    def test_floor_tracks_the_renewers_constants_not_hardcoded_30_5(self):
        # the import-time fallback (30, 5) must never silently take over:
        # if gate._GATE_LEGACY_* moves or is renamed, THIS reddens instead
        # of the guard degrading on a stale floor (observation 2).
        from helm import gate
        self.assertEqual(float(gate._GATE_LEGACY_TTL_S), 30.0)
        self.assertEqual(float(gate._GATE_LEGACY_RENEW_S), 5.0)
        seats.claim("gatelock:proj", "alice", ttl=30, session=self.sid)
        floor = float(gate._GATE_LEGACY_TTL_S) - float(
            gate._GATE_LEGACY_RENEW_S) - 1.0
        with mock.patch.object(gate, "_GATE_LEGACY_TTL_S", 60.0), \
                mock.patch.object(gate, "_GATE_LEGACY_RENEW_S", 10.0):
            moved_floor = 60.0 - 10.0 - 1.0    # 49.0
            # below the MOVED floor but above the old one: the guard must
            # follow the constants, not the fallback
            self._expire_in("gatelock:proj", moved_floor - 2.0)
            rc, _o, err = self.guard()
            self.assertEqual(rc, 2, err)
            self.assertIn("helm chat release gatelock:proj", err)
            self.assertNotIn("RENEWED", err)
            self.assertNotEqual(floor, moved_floor)


class SuccessorReserveIsTotalTest(unittest.TestCase):
    """NO RUNG MAY SPEND THE BUDGET OF THE RUNGS AFTER IT.

    `RESERVE_S` named three rungs and `rung_deadline` answered None for the
    other ten, which does not mean "no reserve" — it means NO LOCAL DEADLINE:
    an unlisted rung ran against the ambient budget and could therefore spend
    every successor's share of it. The three-entry table was not a
    conservative subset of a general rule, it WAS the rule.
    """

    def test_every_ladder_rung_gets_a_local_deadline(self):
        # THE POSITIVE POLE RUNS FIRST AND RUNS UNCONDITIONALLY, because the
        # absence below reads the SAME call and a resolver that answered None
        # for every argument would satisfy it just as well. Outside the loop
        # and outside the subTest, so neither an empty `RUNGS` nor a subTest
        # that swallows its failure can leave this arm asserting nothing.
        #
        # AND IT ASSERTS A NUMBER RATHER THAN NOT-NONE. `assertIsNotNone`
        # would state the weaker half of the same thing — that something came
        # back — while what makes the None below meaningful is that a real
        # rung gets a real, spendable reserve. A resolver returning a
        # placeholder object would satisfy not-None and fail this.
        self.assertGreater(
            seats_stop_budget.reserve("identity"), 0.0,
            "control: a real rung DOES get a real reserve, so the None below "
            "is the unknown-stage arm and not a dead resolver")
        self.assertGreater(
            seats_stop_budget.cost("identity"), 0.0,
            "control: a real rung DOES get a real fitted cost")
        for stage in seats_stop_budget.RUNGS:
            with self.subTest(stage=stage):
                self.assertIsNotNone(
                    seats_stop_budget.reserve(stage),
                    "rung has no reserve, so it may spend the whole ladder")
                self.assertIsNotNone(seats_stop_budget.cost(stage))
        # THE CONTRAST POLE: `None` still means something, and it means "not
        # a rung on this ladder" rather than "a rung with nothing to reserve".
        self.assertIsNone(seats_stop_budget.reserve("not-a-rung"))

    def test_a_reserve_shrinks_monotonically_down_the_ladder(self):
        """A rung owes its successors, so the debt falls as they run out.

        Pinned fat tails are exempt by construction — `claims` reserves for
        `seam` — so the derived arm is checked among the derived rungs."""
        derived = [r for r in seats_stop_budget.RUNGS
                   if r not in seats_stop_budget.RESERVE_S]
        owed = [seats_stop_budget.reserve(r) for r in derived]
        self.assertEqual(owed, sorted(owed, reverse=True))
        self.assertGreater(owed[0], 0.0, "control: the first rung owes the "
                                         "rest of the ladder something")
        self.assertEqual(seats_stop_budget.reserve("response"), 0.0,
                         "the last rung has no successors to reserve for")

    def test_no_rung_is_refused_admission_at_the_ladders_own_start(self):
        """A reserve big enough to forbid the ladder is not a reserve.

        Summing every successor's FITTED cost totals more than `BUDGET_S`
        before a single rung runs, which would publish COVERAGE UNKNOWN on
        `identity` — a new outage in place of the cured one. This arm is the
        falsifier for that whole class of derivation."""
        from helm import projscope
        clock = [500.0]
        first, second = seats_stop_budget.RUNGS[:2]
        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            state = seats_stop_budget.State()
            with projscope.scope(
                    deadline=clock[0] + seats_stop_budget.BUDGET_S):
                state.bind_deadline()
                # ADMITTED BY TWO UNCONDITIONAL CALLS rather than a loop: a
                # loop over a slice is one `RUNGS` edit away from asserting
                # nothing, and this is the arm that must not go quiet.
                self.assertTrue(state.admit(first),
                                "%s refused at the ladder's own start" % first)
                self.assertTrue(state.admit(second),
                                "%s refused at the ladder's own start"
                                % second)
                self.assertEqual(state.yielded, set())

            # THE POSITIVE POLE ON THAT SAME EMPTY OBSERVABLE, unconditional
            # and on a second State: a budget too small for the first rung's
            # own cost DOES refuse it and DOES record the yield. Without this
            # the arm above passes on a `yielded` that never fills at all —
            # which is exactly what a broken `admit` looks like.
            starved = seats_stop_budget.State()
            with projscope.scope(deadline=clock[0] + 0.001):
                starved.bind_deadline()
                self.assertFalse(starved.admit(first),
                                 "control: a budget of one millisecond still "
                                 "admitted the first rung")
            self.assertEqual(starved.yielded, {first},
                             "control: a refused rung must be recorded, or "
                             "the empty set above says nothing")


class OneSlowRungCannotDisableTheRestTest(unittest.TestCase):
    """THE MEASURED OUTAGE, AS AN ARM.

    A cold `wiring` spent the entire ambient budget and the ladder published
    `spiral=UNFINISHED, seam=UNREACHED, ndp=UNREACHED, punt=UNREACHED,
    whisper=UNREACHED, claim-evidence=UNREACHED, mechanical=UNREACHED,
    response=UNREACHED` — and PASSED the stop on that line. Failing open is
    correct for a Stop hook and is exactly what made it invisible.
    """

    def test_an_overrunning_wiring_rung_leaves_every_successor_admitted(self):  # noqa: VACUOUS_ASSERTION — the burn callback's own assertion proves it ran, and each successor is admitted by an unconditional call
        from helm import projscope
        clock = [200.0]
        ran = []

        # BURN PAST THE LOCAL DEADLINE, WHICH IS WHERE THE WALK ACTUALLY
        # STOPS. `wiring.graph` yields to the ambient scope once per module,
        # and inside this rung that scope carries the LOCAL deadline — so a
        # cold walk raises at `ambient - reserve`, never at `ambient`. A first
        # cut of this arm burned 30s, sailed past the ambient deadline too,
        # and got the whole-ladder expiry that `_RungScope` correctly declines
        # to swallow: it was testing a path the checkpoint prevents.
        overrun = (seats_stop_budget.BUDGET_S
                   - seats_stop_budget.reserve("wiring") + 0.001)

        def burn():
            ran.append("wiring")
            clock[0] += overrun
            return "never published"

        with mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            state = seats_stop_budget.State()
            with projscope.scope(
                    deadline=clock[0] + seats_stop_budget.BUDGET_S):
                state.bind_deadline()
                answer = state.run("wiring", burn, fallback="yielded")
                # MUST-HIT: the rung really ran and really overran. Without
                # this the arm would pass on a rung that was never admitted.
                self.assertEqual(ran, ["wiring"])
                self.assertEqual(answer, "yielded")
                left = projscope.spend_or_raise("successors still admitted")
        self.assertIn("wiring=UNFINISHED", "\n".join(state.warns))
        self.assertEqual(state.blocks, [],
                         "an unexamined rung was filed as a finding")
        # THE PROPERTY: the rung was cut at its local reserve, so exactly the
        # successors' share of the budget is what survives it.
        self.assertAlmostEqual(left,
                               seats_stop_budget.reserve("wiring") - 0.001,
                               places=6)
        self.assertGreater(left, 0.0)

    def test_the_yield_names_two_successors_and_not_eight(self):
        """What a `wiring` overrun now costs, spelled in the verdict itself.

        The rung moved behind every perishable rung on the ladder, so the
        blast radius of its worst case is the cheap ones after it — two, since
        the mechanical tail moved to the `helm web` resident."""
        text = seats_stop_budget.unknown("wiring")
        unreached = [r for r in seats_stop_budget.RUNGS
                     if r + "=UNREACHED" in text]
        self.assertEqual(unreached, ["claim-evidence", "response"])
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE: for an EARLY rung this
        # same renderer DOES name the long tail, so a short tail above is a
        # fact about `wiring`'s position and not a renderer that emits
        # nothing. Without this pole the arm passes on a dead renderer.
        early = seats_stop_budget.unknown("beacon")
        for rung in ("spiral", "seam", "ndp", "punt", "whisper", "wiring"):
            self.assertIn(rung + "=UNREACHED", early)

    def test_the_perishable_rungs_all_run_before_the_durable_one(self):
        """Order is the cheapest half of this budget and it was unset.

        Every rung from `spiral` on reads state THIS TURN produced and has one
        moment to fire; an unreachable module is still unreachable at the next
        stop. So the durable finding goes last among the rungs that block."""
        order = seats_stop_budget.RUNGS
        perishable = ("inbox", "claims", "beacon", "spiral", "seam",
                      "ndp", "punt", "whisper")
        for rung in perishable:
            with self.subTest(rung=rung):
                self.assertLess(order.index(rung), order.index("wiring"),
                                "a perishable rung runs after the durable one")
        self.assertEqual(list(order[order.index("wiring") + 1:]),
                         ["claim-evidence", "response"])
        self.assertNotIn("mechanical", order,
                         "the index/scratch tail runs in the resident now")
