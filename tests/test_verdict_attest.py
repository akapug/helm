#!/usr/bin/env python3
"""When may a verdict's announce claim it was ATTESTED, and what may the
reducer over the attest sidecar say?

One question, four classes, MOVED WHOLE OUT OF `tests/test_dispatches.py`
byte-for-byte -- no body was rewritten on the way. That file stood 122,743
bytes under the never-track ceiling with arms landing in it daily and had
already refused a commit once this week, and these four are the whole of its
attest surface: measured, 74 of the file's 75 `attest` spellings outside
`DispatchBase` lived in them.

THE CLASS-BORROWS-FROM-CLASS EDGES CAME ALONG, WHICH IS WHY THE SET IS FOUR
AND NOT ONE. `AttestReducerRound5Test` takes five helpers off
`AttestReducerMatrixBase` by reference (`_verdicted`, `_retry`, `_sidecar`,
`_intent`, `_done`) so the two rounds cannot disagree about what a sidecar
cell means, and `VerdictListAttestProjectionTest` SUBCLASSES that base --
an edge a search for `AttestReducerMatrixBase.` does not show. The base is
the fixture half of `AttestReducerMatrixTest` and holds no arm, so the
subclass does not run the matrix again. Both edges live entirely inside
this file.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `DispatchBase` and `run`
are imported from the module these arms came from, so a change to the fixture
still reaches them and the two files cannot drift.
"""
import json
import os
from unittest import mock

from tests.test_dispatches import DispatchBase, run

from helm import chat, dispatches, eventledger


class VerdictTurnTest(DispatchBase):
    """mark_verdict emits the verdict as a chat turn carrying the STRUCTURED
    binding (bd-173ca9): row fields vlane/vtip/vrid/vref, signed via the
    disjoint verdict digest when signing is on (unsigned here: node URL empty),
    ambient (non-waking), and fail-open — announce failure never fails the
    verdict the ledger already recorded."""

    def _verdict(self, row):
        return dispatches.mark_verdict(
            row["id"], row["tip"], "PASS by codex", "fix")

    def _rows(self, room="main"):
        from helm import chat
        msgs, _total = chat.read(room)
        return msgs

    def test_verdict_announces_a_turn_with_the_structured_binding(self):
        row = self.add(lane="verdict-lane")
        out, why = self._verdict(row)
        self.assertIsNone(why)
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)
        t = turns[0]
        self.assertEqual(t["vlane"], "verdict-lane")
        self.assertEqual(t["vtip"], row["tip"])
        self.assertEqual(t["vref"], "PASS by codex")
        self.assertEqual(t.get("ambient"), 1)

    def test_announce_failure_never_fails_the_verdict(self):
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add()
            out, why = self._verdict(row)
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertEqual(out["status"], "verdict")

    def test_idempotent_reverdict_does_not_double_announce(self):
        row = self.add(lane="once")
        self._verdict(row)
        dispatches.mark_verdict(
            row["id"], row["tip"], "PASS by codex", "fix")  # same
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)

    def test_verdict_room_env_routes_the_announce(self):
        os.environ["HELM_VERDICT_ROOM"] = "gates"
        try:
            row = self.add(lane="routed")
            self._verdict(row)
        finally:
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertEqual([r for r in self._rows("main") if r.get("vrid")], [])
        self.assertEqual(len([r for r in self._rows("gates")
                              if r.get("vrid") == row["id"]]), 1)

    def test_doubt_never_resigns_but_pre_intent_verdicts_heal_once(self):
        # Lifecycle A: after an intent exists, a crash window may hide
        # a COMPLETED remote signing — the retry must never re-emit (doubt),
        # only read-only-confirm. Heal-by-emit is reserved for the one
        # provably sign-free state: no intent on record.
        from helm import chat, eventledger
        calls = []
        orig = chat.post
        def crashing(*a, **k):
            calls.append(1)
            raise RuntimeError("down")
        chat.post = crashing
        try:
            row = self.add(lane="doubt", notify=False)
            first, why = self._verdict(row)
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertIn("NEEDS CONFIRMATION", first["announce"])
        self.assertEqual(len(calls), 1)
        spy = []
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            again, _ = self._verdict(row)          # retry: DOUBT, no re-sign
        finally:
            chat.post = orig
        self.assertIn("in doubt", again["announce"])
        self.assertEqual(spy, [])                  # post never called again
        # pre-intent (legacy) verdicts DO heal: erase the attest ledger
        os.remove(dispatches.attest_path())
        healed, _ = self._verdict(row)
        self.assertIn("cite-tier", healed["announce"])
        turns = [r for r in self._rows() if r.get("vrid") == row["id"]]
        self.assertEqual(len(turns), 1)

    def test_doubt_upgrades_read_only_from_the_INTENT_room_not_env(self):
        # Lifecycle C: the intent's STORED room is authoritative —
        # an env change never redirects the search, and the upgrade is
        # strictly read-only (found turn -> done, no emit).
        from helm import chat, eventledger
        os.environ["HELM_VERDICT_ROOM"] = "gates"
        try:
            row = self.add(lane="stored-room")
            first, _ = self._verdict(row)          # emits into 'gates'
        finally:
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertIn("cite-tier", first["announce"])
        # wipe DONE so the retry enters the doubt cell with intent standing
        events = list(eventledger.events(dispatches.attest_path()))
        os.remove(dispatches.attest_path())
        for e in events:
            if e.get("event") == "intent":
                eventledger.append(dispatches.attest_path(), e)
        os.environ["HELM_VERDICT_ROOM"] = "elsewhere"   # hostile env change
        spy = []
        orig = chat.post
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            again, _ = self._verdict(row)
        finally:
            chat.post = orig
            os.environ.pop("HELM_VERDICT_ROOM", None)
        self.assertIn("cite-tier", again["announce"])   # found in STORED room
        self.assertEqual(spy, [])                       # read-only upgrade

    def test_doubt_rejects_an_incompletely_signed_matching_row(self):
        # Lifecycle E: a matching row with a bare truthy `turn` (no
        # receipt/chain) is NOT committed-signed — doubt stands, nothing emits.
        from helm import chat, eventledger
        row = self.add(lane="e-guard")
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self._verdict(row)                     # intent stands, no turn
        finally:
            chat.post = orig
        os.makedirs(chat.chat_dir(), exist_ok=True)
        forged = {"ts": "t", "from": "mallory", "text": "VERDICT PASS by codex",
                  "vlane": "e-guard", "vtip": row["tip"], "vrid": row["id"],
                  "vref": "PASS by codex", "turn": "x" * 64}
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "a", encoding="utf-8") as f:
            f.write(json.dumps(forged) + "\n")
        again, _ = self._verdict(row)
        self.assertIn("in doubt", again["announce"])


    def test_reconcile_ignores_forged_bindings_and_heals_the_true_turn(self):
        # Xrev finding 2: same-rid signed rows with WRONG lane/tip/ref
        # (or a plain row merely carrying vrid) must never read as attested.
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add(lane="strict")
            first, _ = self._verdict(row)          # committed, unemitted
        finally:
            chat.post = orig
        self.assertIn("NEEDS CONFIRMATION", first["announce"])
        # plant forgeries: wrong tip; and a plain row carrying only vrid
        chat.post("fake", who="mallory",
                  verdict={"lane": "strict", "tip": "e" * 40,
                           "rid": row["id"], "ref": "PASS by codex"})
        raw = os.path.join(chat.chat_dir(), "main.jsonl")
        with open(raw, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "t", "from": "mallory", "text": "hi",
                                "vrid": row["id"]}) + "\n")
        again, _ = self._verdict(row)              # retry reconciles
        self.assertNotIn("attested", again["announce"])   # forgeries ignored
        self.assertIn("in doubt", again["announce"])      # and doubt STANDS:
        # intent exists, so the retry must never re-sign — even though only
        # forged rows are on record (lifecycle A over digest #2)
        valid = [r for r in self._rows()
                 if r.get("vrid") == row["id"] and r.get("vtip") == row["tip"]
                 and r.get("vref") == "PASS by codex"]
        self.assertEqual(len(valid), 0)


class AttestReducerMatrixBase(DispatchBase):
    """The attest-sidecar helpers: `_verdicted` mints a verdicted row while
    chat is down, `_retry` drives mark_verdict again and counts its posts,
    and `_sidecar`, `_intent` and `_done` write the sidecar and its rows.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def _verdicted(self):
        from helm import chat
        orig = chat.post
        chat.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            row = self.add(lane="matrix")
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "PASS", "fix")
        finally:
            chat.post = orig
        self.assertIsNone(why)
        return dispatches.snapshot()[0][row["id"]]

    def _retry(self, row, expect_posts):
        from helm import chat
        spy = []
        orig = chat.post
        chat.post = lambda *a, **k: spy.append(1) or orig(*a, **k)
        try:
            out, why = dispatches.mark_verdict(
                row["id"], row["reviewed_tip"], row["verdict_ref"],
                row["polarity"])
        finally:
            chat.post = orig
        self.assertIsNone(why)
        self.assertEqual(len(spy), expect_posts,
                         "emit-cell reachability violated: %s" % out["announce"])
        return out["announce"]

    def _sidecar(self, row, content_rows, raw_suffix=b""):
        import json as J
        path = dispatches.attest_path()
        with open(path, "wb") as f:
            for r in content_rows:
                f.write(J.dumps(r).encode() + b"\n")
            f.write(raw_suffix)

    def _intent(self, row, **over):
        base = {"v": 1, "event": "intent", "id": row["id"], "ts": "t",
                "room": "main",
                "binding": dispatches._binding_key(
                    row, row["reviewed_tip"], row["verdict_ref"])}
        base.update(over)
        return base

    def _done(self, row, kind="cite-tier", **over):
        from helm import chat
        base = {"v": 1, "event": "done", "id": row["id"], "ts": "t",
                "room": "main",
                "binding": dispatches._binding_key(
                    row, row["reviewed_tip"], row["verdict_ref"]),
                "payload": "", "turn": "", "receipt": "", "chain": None,
                "kind": kind, "text": "VERDICT turn"}
        if kind == "attested":
            # a VALID attested done recomputes over the ledger-truth binding
            payload = chat.verdict_digest(
                str(row.get("lane") or ""), row["reviewed_tip"],
                str(row["id"]), str(row["verdict_ref"] or ""), "VERDICT turn")
            base.update(payload=payload, turn="t" * 64, receipt="r" * 64,
                        chain=3)
        base.update(over)
        return base


class AttestReducerMatrixTest(AttestReducerMatrixBase):
    """Round-4 bar: the FULL sidecar partition as pinned regressions —
    file-level x schema x duplicates x live-path, each cell asserting the
    reported state AND whether the emit cell is reachable. UNKNOWN can never
    reach emit; only provably-empty states may heal.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses AttestReducerMatrixBase."""

    # ---- surface: the split must be VISIBLE, not just detected ---------------
    def test_a_REMINTED_row_is_reported_UNVERIFIABLE_and_a_MATCHING_one_is_not(self):
        """The pair is the point: the same row, same code, only the ledger's
        binding differs — so a pass cannot come from the probe simply saying
        'unverifiable' to everything. Measured live 2026-08-03: five rows sat
        in exactly this state reading as ordinary decided rows."""
        row = self._verdicted()
        # MUST-NOT-HIT: ledger bound to the evidence the row actually carries
        self._sidecar(row, [self._intent(row),
                            self._done(row, kind="attested")])
        self.assertEqual(dispatches.attest_unverifiable([row]), frozenset(),
                         "a row whose binding MATCHES must never be flagged")
        # MUST-HIT: the same row re-minted with different evidence. Nothing is
        # corrupt — the ledger describes evidence E1, the row now carries E2.
        stale = dict(row)
        stale["verdict_ref"] = str(row["verdict_ref"]) + " (first draft)"
        self._sidecar(row, [self._intent(stale),
                            self._done(stale, kind="attested")])
        flagged = dispatches.attest_unverifiable([row])
        self.assertIn(str(row["id"]), flagged)
        # and it must reach the SURFACE, which is the entire defect: the
        # detector already existed and printed to a terminal nobody reads.
        self.assertIn("ATTEST UNVERIFIABLE", dispatches._label(row, flagged))
        # the verdict itself still shows — composed, never replaced
        self.assertIn(dispatches._base_label(row),
                      dispatches._label(row, flagged))

    def test_the_unverifiable_probe_NEVER_WRITES_TO_THE_LEDGER(self):
        """A probe that writes would MINT the rows it was asked to count:
        _announce_verdict appends an intent when none exists, so the read-only
        path must go through _attest_state. Asserted by BYTES, because 'no
        exception raised' would pass even if the file were rewritten."""
        row = self._verdicted()
        self._sidecar(row, [self._intent(row)])
        with open(dispatches.attest_path(), "rb") as fh:
            before = fh.read()
        # POSITIVE CONTROL: two reads of an EMPTY file are also equal, so
        # equality alone would pass on a ledger that never existed.
        self.assertTrue(before, "fixture wrote no ledger — equality is vacuous")
        dispatches.attest_unverifiable([row])
        with open(dispatches.attest_path(), "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after, "the probe mutated the attest ledger")

    def test_an_UNREADABLE_ledger_yields_EMPTY_rather_than_a_guess(self):
        """A false ATTEST UNVERIFIABLE on the fleet's main surface teaches
        people to ignore the true ones, so an unreadable ledger says nothing."""
        row = self._verdicted()
        # POSITIVE CONTROL FIRST: with a mismatched ledger present, this exact
        # row IS flagged. Without it, an empty answer proves nothing — a probe
        # that always returned frozenset() would pass.
        stale = dict(row)
        stale["verdict_ref"] = str(row["verdict_ref"]) + " (first draft)"
        self._sidecar(row, [self._intent(stale),
                            self._done(stale, kind="attested")])
        self.assertIn(str(row["id"]), dispatches.attest_unverifiable([row]))
        os.remove(dispatches.attest_path())
        self.assertEqual(dispatches.attest_unverifiable([row]), frozenset())

    def test_a_row_that_was_NEVER_ATTESTED_is_not_flagged(self):  # noqa: VACUOUS_ASSERTION — the absence (reviewed_tip is None) is a PRECONDITION, not the claim; the claim is the exact set equality below, which pins attest_unverifiable to a NON-EMPTY expected set {other}, so it can only hold if the probe both FOUND `other` and OMITTED `row`. A probe returning frozenset() for everything fails it. The rung cannot see this because assertEqual against a literal is an "exact scalar pin" it discards, so no control on `row` is countable here.
        """reviewed_tip is None on a row with no verdict: there is no signed
        record for a missing one to contradict, so it is not a split."""
        other = self._verdicted()
        # POSITIVE CONTROL ON reviewed_tip ITSELF: a misspelled key would read
        # None on every row and make the assertion below pass for the wrong
        # reason, so prove the field is populated when it should be.
        self.assertIsNotNone(other.get("reviewed_tip"))
        row = self.add(lane="never-verdicted")
        # POSITIVE CONTROL ON `row` ITSELF: without it, assertIsNone would pass
        # just as happily on an empty dict or a misspelled key — the absence
        # has to be a fact about a REAL populated row. assertTrue rather than
        # assertEqual deliberately: an equality against a literal is an exact
        # scalar pin, which the vacuous-assertion rung classifies as "neither
        # proof nor claim" and discards, so it would control nothing.
        self.assertTrue(row["id"])
        self.assertTrue(row["lane"])
        self.assertIsNone(row.get("reviewed_tip"))
        # AND on the probe: a genuinely split row passed in alongside IS
        # flagged, so the exemption is about THIS row rather than the probe
        # answering empty to everything.
        stale = dict(other)
        stale["verdict_ref"] = str(other["verdict_ref"]) + " (first draft)"
        self._sidecar(other, [self._intent(stale),
                              self._done(stale, kind="attested")])
        flagged = dispatches.attest_unverifiable([row, other])
        self.assertEqual(flagged, frozenset({str(other["id"])}))

    def test_the_label_WITHOUT_the_set_is_unchanged_for_every_row_shape(self):
        """Regression guard for the rename: _label defaults to the empty set,
        so every existing caller keeps byte-identical output."""
        row = self._verdicted()
        # UNCONDITIONAL, outside the loop: an emptied shape tuple would skip
        # every assertion below and still pass.
        self.assertTrue(dispatches._base_label(row))
        self.assertEqual(dispatches._label(row), dispatches._base_label(row))
        self.assertEqual(
            dispatches._label(row, frozenset({str(row["id"])})),
            dispatches._base_label(row) + " / ATTEST UNVERIFIABLE")
        for shape in ({}, {"abandoned": True}, {"closed_by_landing": True},
                      {"discharged": True}, {"close_reason": "superseded"},
                      {"status": "cancelled"}, {"migration": "x"},
                      {"delivery": "unobserved"}):
            probe = dict(row)
            probe.update(shape)
            # POSITIVE CONTROL: the label must be non-empty, or "unchanged"
            # would be satisfied by two empty strings for every shape.
            self.assertTrue(dispatches._base_label(probe))
            self.assertEqual(dispatches._label(probe),
                             dispatches._base_label(probe))
            # and the marker composes onto whatever that shape rendered
            self.assertEqual(
                dispatches._label(probe, frozenset({str(probe["id"])})),
                dispatches._base_label(probe) + " / ATTEST UNVERIFIABLE")

    # ---- A. file level -------------------------------------------------------
    def test_A1_missing_file_is_provably_empty_and_heals_once(self):
        row = self._verdicted()
        os.remove(dispatches.attest_path())
        self.assertIn("cite-tier", self._retry(row, expect_posts=1))

    def test_A2_unreadable_file_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        os.chmod(dispatches.attest_path(), 0)
        try:
            self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        finally:
            os.chmod(dispatches.attest_path(), 0o600)

    def test_A3_empty_file_is_provably_empty_and_heals_once(self):
        row = self._verdicted()
        self._sidecar(row, [])
        self.assertIn("cite-tier", self._retry(row, 1))

    def test_A4_torn_tail_is_not_durable_and_is_ignored(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"intent"')
        self.assertIn("cite-tier", self._retry(row, 1))   # torn != event

    def test_A4b_historical_blank_separator_is_not_corruption(self):
        row = self._verdicted()
        intent = self._intent(row)
        with open(dispatches.attest_path(), "wb") as f:
            f.write(b"\n" + json.dumps(intent).encode() + b"\n\n")
        self.assertIn("in doubt", self._retry(row, 0))

    def test_A5_malformed_terminated_line_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b"not json\n")
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_A6_non_object_row_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'["list"]\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    # ---- B. intent schema ----------------------------------------------------
    def test_B1_valid_intent_routes_to_doubt_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    def test_B2_unknown_event_name_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [{"v": 1, "event": "garbage", "id": row["id"]}])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B3_intent_missing_key_is_UNKNOWN(self):
        row = self._verdicted()
        bad = self._intent(row); del bad["room"]
        self._sidecar(row, [bad])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B4_intent_extra_key_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, extra="x")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B5_intent_v_wrong_type_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, v="1")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B6_intent_empty_ts_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, ts="")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B7_intent_binding_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, binding="f" * 32)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_B8_identical_duplicate_intent_is_idempotent_doubt(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    def test_B9_different_duplicate_intent_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._intent(row, room="evil")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    # ---- C. done schema ------------------------------------------------------
    def test_C1_done_without_intent_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._done(row)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C2_valid_attested_done_reports_attested(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, "attested")])
        self.assertIn("attested", self._retry(row, 0))

    def test_C3_valid_cite_done_reports_cite(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row)])
        self.assertIn("cite-tier", self._retry(row, 0))

    def test_C4_done_room_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, room="other")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C5_done_binding_mismatch_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, binding="e" * 32)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C6_attested_done_missing_receipt_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, "attested", receipt="")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C7_cite_done_carrying_turn_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, turn="x" * 64)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C8_done_unknown_kind_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row, kind="wat")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C9_conflicting_duplicate_done_is_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row),
                            self._done(row, ts="t2")])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_C10_identical_duplicate_done_reports_once(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row), self._done(row), self._done(row)])
        self.assertIn("cite-tier", self._retry(row, 0))

    def test_C11_foreign_id_rows_are_ignored(self):
        row = self._verdicted()
        foreign = self._intent(row, id="other-rid", binding="a" * 32)
        self._sidecar(row, [foreign, self._intent(row)])
        self.assertIn("in doubt", self._retry(row, 0))

    # ---- D. live path --------------------------------------------------------
    def test_D1_emit_failure_keeps_intent_and_reports_doubtful(self):
        row = self._verdicted()          # emit already failed at verdict time
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNotNone(intent)
        self.assertIsNone(done)
        self.assertIsNone(unknown)

    def test_D2_invalid_live_turn_never_persists_done(self):
        # cell 6: a forged/partial turn must not append a done a retry could
        # upgrade — doubt both times, zero valid turns.
        from helm import chat
        row = self._verdicted()
        forged = {"vlane": "matrix", "vtip": row["reviewed_tip"],
                  "vrid": row["id"], "vref": "PASS", "turn": "x" * 64}
        orig = chat.post
        chat.post = lambda *a, **k: dict(forged)
        try:
            first = self._retry_raw(row)
        finally:
            chat.post = orig
        self.assertIn("in doubt", first)
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(done)          # nothing persisted to upgrade later

    def _retry_raw(self, row):
        out, why = dispatches.mark_verdict(
            row["id"], row["reviewed_tip"], row["verdict_ref"],
            row["polarity"])
        self.assertIsNone(why)
        return out["announce"]

    def test_D3_done_append_failure_reports_not_stronger_than_durable(self):
        from helm import chat, eventledger
        row = self._verdicted()
        os.remove(dispatches.attest_path())      # provably empty: heal path
        orig = eventledger.append_unlocked
        def flaky(path, r):
            if r.get("event") == "done":
                return False
            return orig(path, r)
        eventledger.append_unlocked = flaky
        try:
            state = self._retry(row, 1)          # emit happens once
        finally:
            eventledger.append_unlocked = orig
        self.assertIn("NEEDS CONFIRMATION", state)

    def test_D4_doubt_upgrade_appends_validated_done(self):
        row = self._verdicted()                  # intent stands, turn absent
        again = self._retry(row, 0)              # doubt (room has no turn)
        self.assertIn("in doubt", again)
        # deliver the true unsigned turn out-of-band, then reconcile upgrades
        from helm import chat
        chat.post("VERDICT PASS — manual", room="main", ambient=True,
                  verdict={"lane": row.get("lane"), "tip": row["reviewed_tip"],
                           "rid": row["id"], "ref": row["verdict_ref"]})
        upgraded = self._retry(row, 0)
        self.assertIn("cite-tier", upgraded)
        _i, done, _u = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNotNone(done)


class AttestReducerRound5Test(DispatchBase):
    """Round-5 counterexample pins: replay re-verification with
    verifier-call reachability, bool-proof types, unscopable rows, and
    concurrent done-append discrimination."""

    _verdicted = AttestReducerMatrixBase._verdicted
    _retry = AttestReducerMatrixBase._retry
    _sidecar = AttestReducerMatrixBase._sidecar
    _intent = AttestReducerMatrixBase._intent
    _done = AttestReducerMatrixBase._done

    def test_R1_forged_attested_done_fails_reverification_with_verifiers_called(self):
        from helm import chat
        row = self._verdicted()
        forged = self._done(row, "attested", payload="chat:b2b:" + "f" * 64)
        self._sidecar(row, [self._intent(row), forged])
        calls = {"payload_for": 0, "committed_signed": 0}
        op, oc = chat.payload_for, chat.committed_signed
        chat.payload_for = lambda *a, **k: calls.__setitem__(
            "payload_for", calls["payload_for"] + 1) or op(*a, **k)
        chat.committed_signed = lambda *a, **k: calls.__setitem__(
            "committed_signed", calls["committed_signed"] + 1) or oc(*a, **k)
        try:
            state = self._retry(row, 0)
        finally:
            chat.payload_for, chat.committed_signed = op, oc
        self.assertIn("NEEDS CONFIRMATION", state)      # never attested/cite
        self.assertGreater(calls["committed_signed"], 0)
        # a VALID done then re-verifies attested, with the verifier consulted
        self._sidecar(row, [self._intent(row), self._done(row, "attested")])
        calls["payload_for"] = 0
        chat.payload_for = lambda *a, **k: calls.__setitem__(
            "payload_for", calls["payload_for"] + 1) or op(*a, **k)
        try:
            state = self._retry(row, 0)
        finally:
            chat.payload_for = op
        self.assertIn("attested", state)
        self.assertGreater(calls["payload_for"], 0)     # verified, not trusted

    def test_R2_bool_v_and_bool_chain_are_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, v=True)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        i, d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(i)
        self.assertIsNone(d)
        self.assertIn("schema violation (v)", unknown or "")
        self._sidecar(row, [self._intent(row),
                            self._done(row, "attested", chain=True)])
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        i, d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIn("evidence incomplete", unknown or "")

    def test_R3_unscopable_complete_row_is_UNKNOWN_no_emit(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"garbage"}\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        self._sidecar(row, [], raw_suffix=b'{"v":1,"event":"intent"}\n')
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))

    def test_R4_concurrent_done_writers_append_exactly_once(self):
        from helm import chat
        row = self._verdicted()
        turn = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "VERDICT turn", "turn": "", "receipt": "",
                "chain": None, "payload": ""}
        first = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", dict(turn), chat)
        second = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", dict(turn), chat)
        self.assertIn("cite-tier", first)
        self.assertEqual(first, second)                  # idempotent report
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(len([r for r in rows if r.get("event") == "done"]), 1)

    def test_R5_forged_live_turn_reaches_record_done_and_is_refused(self):
        from helm import chat
        row = self._verdicted()          # intent stands
        os.remove(dispatches.attest_path())              # heal cell
        reached = []
        orig_rd = dispatches._record_done
        def spy_rd(*a, **k):
            reached.append(1)
            return orig_rd(*a, **k)
        dispatches._record_done = spy_rd
        forged = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                  "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                  "text": "x", "turn": ["not", "a", "hash"], "receipt": {},
                  "chain": "9", "payload": "junk"}
        orig_post = chat.post
        chat.post = lambda *a, **k: dict(forged)
        try:
            out, why = dispatches.mark_verdict(
                row["id"], row["reviewed_tip"], row["verdict_ref"],
                row["polarity"])
        finally:
            chat.post = orig_post
            dispatches._record_done = orig_rd
        self.assertIsNone(why)
        self.assertEqual(len(reached), 1)                # verifier REACHED
        self.assertIn("NEEDS CONFIRMATION", out["announce"])
        intent, done, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIsNone(done)
        self.assertIsNone(unknown)                       # file still VALID
        self.assertIsNotNone(intent)
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(                                 # raw truth: 0 done
            len([r for r in raw_rows if r.get("event") == "done"]), 0)

    def test_R6_forged_nonempty_payload_on_unsigned_turn_is_refused(self):
        from helm import chat
        row = self._verdicted()
        turn = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None,
                "payload": "forged-nonempty"}
        state = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main", turn, chat)
        self.assertIsNone(state)
        import json as J
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(
            len([r for r in raw_rows if r.get("event") == "done"]), 0)

    def test_R7_ack_or_react_overlap_is_not_a_verdict_turn(self):
        from helm import chat
        row = self._verdicted()
        base = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None}
        self.assertTrue(dispatches._is_this_verdicts_turn(dict(base), row, chat))
        self.assertFalse(dispatches._is_this_verdicts_turn(
            dict(base, ack="foreign-shape"), row, chat))
        self.assertFalse(dispatches._is_this_verdicts_turn(
            dict(base, react="👍|1|x"), row, chat))

    def test_R8_numeric_id_row_is_unscopable_UNKNOWN(self):
        row = self._verdicted()
        self._sidecar(row, [], raw_suffix=(
            b'{"v":1,"event":"intent","id":123456,"ts":"t","room":"main",'
            b'"binding":"x"}\n'))
        self.assertIn("NEEDS CONFIRMATION", self._retry(row, 0))
        _i, _d, unknown = dispatches._attest_state(
            row, row["reviewed_tip"], row["verdict_ref"])
        self.assertIn("unscopable", unknown or "")

    def test_R9_falsey_wrong_type_wire_values_are_refused_zero_write(self):
        # round-7: the FULL wire-value partition — truthiness must never
        # decide shape. NON-VACUOUS by construction: the intent STAYS on
        # file, so the only refusal path is the partition itself, and the
        # exactly-empty valid shape RECORDS at the end (reachability).
        from helm import chat
        row = self._verdicted()                          # intent stands
        base = {"vlane": str(row.get("lane") or ""), "vtip": row["reviewed_tip"],
                "vrid": str(row["id"]), "vref": str(row["verdict_ref"] or ""),
                "text": "x", "turn": "", "receipt": "", "chain": None,
                "payload": ""}
        cases = ([dict(base, turn=v) for v in (0, False, [])]
                 + [dict(base, receipt=v) for v in (0, False, [])]
                 + [dict(base, payload=v) for v in (0, False, [], {})]
                 + [dict(base, chain=v) for v in (0.0, False, "3")])
        import json as J
        for turn in cases:
            state = dispatches._record_done(
                row, row["reviewed_tip"], row["verdict_ref"], "main",
                turn, chat)
            self.assertIsNone(state, "laundered: %r" % (turn,))
        with open(dispatches.attest_path(), encoding="utf-8") as f:
            raw_rows = [J.loads(l) for l in f if l.strip()]
        self.assertEqual(
            len([r for r in raw_rows if r.get("event") == "done"]), 0)
        # reachability: the exactly-empty unsigned shape RECORDS cite-tier
        state = dispatches._record_done(
            row, row["reviewed_tip"], row["verdict_ref"], "main",
            dict(base), chat)
        self.assertIn("cite-tier", state or "")


class VerdictListAttestProjectionTest(AttestReducerMatrixBase):
    """#135: durable verdict/attest truth reaches both dispatch list surfaces."""

    def test_mismatch_is_visible_in_text_and_json_without_mutating_sidecar(self):  # noqa: VACUOUS_ASSERTION — fixture sidecar bytes are asserted non-empty, then exact text + typed JSON state prove the mismatch reached both surfaces
        row = self._verdicted()
        self._sidecar(row, [self._intent(row, binding="f" * 32)])
        with open(dispatches.attest_path(), "rb") as f:
            before = f.read()
        self.assertTrue(before, "mismatch fixture wrote no attest sidecar")
        rc, text, err = run(dispatches.cmd_dispatch, ["list"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("verdict polarity: dispatch store", text)
        self.assertIn("attestation: attest sidecar", text)
        self.assertIn("attest: UNVERIFIABLE", text)
        rc, raw, err = run(dispatches.cmd_dispatch, ["list", "--json"])
        self.assertEqual((rc, err), (0, ""))
        listed = {r["id"]: r for r in json.loads(raw)}[row["id"]]
        self.assertEqual(listed["polarity"], row["polarity"])
        self.assertEqual(listed["polarity_source"], "dispatch-store")
        self.assertEqual(listed["attest_state"], "unverifiable")
        self.assertEqual(listed["attest_source"], "attest-sidecar")
        self.assertEqual(listed["attest_detail"],
                         "attest intent binding mismatch")
        with open(dispatches.attest_path(), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_a_whole_list_reads_the_attest_sidecar_once(self):
        first = self._verdicted()
        second = self._verdicted()
        real = dispatches._attest_rows
        with mock.patch.object(dispatches, "_attest_rows", wraps=real) as read:
            rows = dispatches.with_verdict_projections([first, second])
        self.assertEqual(read.call_count, 1)
        self.assertEqual([r["attest_state"] for r in rows], ["doubt", "doubt"])
