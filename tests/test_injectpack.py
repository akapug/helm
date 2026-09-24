#!/usr/bin/env python3
"""The injection-pack read model and its owner-facing edit door.

THE FIXTURE ROWS ARE THE PRODUCER'S SHAPE, NOT A CONVENIENT ONE. Every row
below is built by `_row`, which emits the exact envelope `inject._ledger`
writes and `injection_schema.valid_exact` admits -- a `sample` block carrying
`encoding: "utf-8"`, a `rendered_bytes` total that is not less than its
`lane_bytes` parts, and the four frozen lane names. A fixture that invented a
simpler row would test a world the injector does not produce, and the arm that
proves it is `test_the_fixture_rows_are_what_the_schema_admits`: it asserts the
fixture passes the REAL validator, so every later arm is known to be reading
rows the production reader would also accept.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import home, injectpack, injection_schema, pk, store  # noqa: E402
from helm.store import write as store_write  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"


def _row(turn, jit=(), pinned=(), reflex=(), lanes=None, suppressed=(),
         suppressed_pinned=(), suppressed_bytes=None, candidates=None,
         session=SESSION, silent=False, ts=None):
    """One fire-ledger row in the envelope `inject._ledger` actually writes."""
    lanes = dict({"whisper": 0, "pinned": 0, "jit": 0, "reflex": 0},
                 **(lanes or {}))
    row = {
        "v": injection_schema.V3, "session": session, "turn": turn,
        "ts": ts or "2026-01-01T00:%02d:00Z" % min(turn, 59),
        "project": "helm",
        "sample": {"encoding": "utf-8", "rendered_bytes": sum(lanes.values()),
                   "lane_bytes": lanes},
        "fired": {"jit": list(jit), "pinned": list(pinned),
                  "reflex": list(reflex)},
    }
    if silent:
        row["silent"] = True
    if suppressed:
        row["suppressed"] = list(suppressed)
    if suppressed_pinned:
        row["suppressed_pinned"] = list(suppressed_pinned)
    if suppressed_bytes is not None:
        row["suppressed_utf8_bytes"] = {"pinned": suppressed_bytes}
    if candidates is not None:
        row["candidates"] = candidates
    return row


def _get(eid):
    """The entry as the store now holds it, retired rows included -- reading
    back through `find_typed` is what proves a write LANDED rather than
    proving `act` returned a dict it built itself."""
    return store.find_typed(eid)


def _prior(eid, statement, load_class="jit", evidence=None, project=None):
    e = {"type": "prior", "id": eid, "statement": statement, "class": "certain",
         "confidence": 0.9, "load_class": load_class}
    if evidence is not None:
        e["evidence_log"] = evidence
    if project is not None:
        e["project"] = project
    return e


class InjectPackFixtureTest(unittest.TestCase):

    def test_the_fixture_rows_are_what_the_schema_admits(self):
        # THE CONTROL FOR EVERY OTHER ARM IN THIS FILE. If this fails, the
        # rows below are a private dialect and nothing they prove is about
        # the ledger the injector writes.
        self.assertTrue(injection_schema.valid_exact(
            _row(1, jit=["a"], lanes={"jit": 40})))
        self.assertTrue(injectpack._exact(_row(2, lanes={"pinned": 7})))
        # and the validator is not simply saying yes to everything:
        self.assertFalse(injectpack._exact({"v": 3, "sample": {}}))
        self.assertFalse(injectpack._exact({"v": 1, "bytes": {"jit": 10}}))


class InjectPackWindowTest(unittest.TestCase):

    def test_a_turn_counter_reset_starts_a_new_window(self):
        rows = [_row(1), _row(2), _row(3), _row(1), _row(2)]
        segments = injectpack.windows(rows)
        self.assertEqual([len(s) for s in segments], [3, 2])
        self.assertEqual([s[0]["turn"] for s in segments], [1, 1])

    def test_a_repeated_turn_number_is_not_a_boundary(self):
        # MEASURED on the live ledger: one session carried turn 19 twice in a
        # row, which is two gathers inside ONE turn. Splitting there would
        # manufacture a window and hide the repeat it contains.
        rows = [_row(1), _row(2), _row(2), _row(3)]
        segments = injectpack.windows(rows)
        self.assertEqual(len(segments), 1)
        self.assertEqual(injectpack.fold(rows)["out_of_order"], 1)
        # the control: a real reset in the same shape DOES split
        self.assertEqual(len(injectpack.windows([_row(1), _row(2), _row(1)])), 2)

    def test_one_window_folds_the_ledgers_own_numbers(self):
        rows = [_row(1, jit=["a", "b"], lanes={"jit": 100, "reflex": 20},
                     candidates=50, suppressed=["x", "y"], suppressed_bytes=300),
                _row(2, jit=["a"], pinned=["p"], lanes={"jit": 60, "pinned": 40},
                     candidates=51, suppressed=["x"], suppressed_bytes=200),
                _row(3, silent=True, candidates=52, suppressed=["x", "y", "z"])]
        win = injectpack.fold(rows)
        self.assertEqual(win["turns"], 3)
        self.assertEqual(win["silent"], 1)
        self.assertEqual(win["bytes"], 120 + 100)
        self.assertEqual(win["lanes"]["jit"], 160)
        self.assertEqual(win["lanes"]["pinned"], 40)
        self.assertEqual(win["fires"], 4)
        self.assertEqual(sorted(win["ids"]), ["a", "b", "p"])
        self.assertEqual(win["ids"]["a"]["fires"], 2)
        self.assertEqual(win["ids"]["a"]["last_turn"], 2)
        self.assertEqual(win["suppressed_fires"], 6)
        self.assertEqual(win["suppressed_bytes"], 500)
        self.assertEqual(win["candidates"], 52)
        self.assertEqual(injectpack.per_turn(win), round(220 / 3.0, 1))

    def test_repeat_is_counted_inside_one_window_and_never_across_two(self):
        # THE ERROR THIS ARM EXISTS TO PREVENT: at a context boundary the seat
        # genuinely loses its premises and helm re-fires them on purpose, so
        # the same id in two windows is the cure, not the disease.
        first, second = [_row(1, jit=["a"])], [_row(1, jit=["a"])]
        self.assertEqual(injectpack.repeat(injectpack.fold(first)), 0.0)
        self.assertEqual(injectpack.repeat(injectpack.fold(second)), 0.0)
        both = injectpack.fold(first + second)
        self.assertEqual(both["fires"], 2)
        self.assertEqual(len(both["ids"]), 1)
        # folding them TOGETHER is what would score 50%; windows() is what
        # stops view() from ever doing that.
        self.assertEqual(injectpack.repeat(both), 0.5)
        self.assertEqual(len(injectpack.windows(first + second)), 2)

    def test_repeat_and_per_turn_answer_unknown_rather_than_zero(self):
        empty = injectpack.fold([])
        self.assertIsNone(injectpack.repeat(empty))
        self.assertIsNone(injectpack.per_turn(empty))
        # the control: a window with real content answers a number
        live = injectpack.fold([_row(1, jit=["a"], lanes={"jit": 10})])
        self.assertEqual(injectpack.repeat(live), 0.0)
        self.assertEqual(injectpack.per_turn(live), 10.0)


class InjectPackEntriesTest(unittest.TestCase):

    def _entries(self, win, store_entries, who=()):
        from helm import inject
        with mock.patch.object(inject, "load_entries",
                               return_value=list(store_entries)), \
                mock.patch.object(inject, "_who_lines", return_value=list(who)):
            return injectpack.entries(win)

    def test_an_entry_costs_its_rendered_line_times_its_fires(self):
        win = injectpack.fold([_row(1, jit=["loud"]), _row(2, jit=["loud"]),
                               _row(3, jit=["quiet"])])
        rows, derived, missing = self._entries(
            win, [_prior("loud", "x" * 100), _prior("quiet", "y" * 10)])
        self.assertIsNone(missing)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["loud"]["bytes"], by_id["loud"]["line_bytes"] * 2)
        self.assertEqual(by_id["quiet"]["fires"], 1)
        # costliest first, which is the only order that answers the question
        self.assertEqual([r["id"] for r in rows], ["loud", "quiet"])
        self.assertEqual(derived, sum(r["bytes"] for r in rows))

    def test_the_cost_is_utf8_bytes_and_not_a_character_count(self):  # noqa: VACUOUS_ASSERTION — the control is the non-empty rendering asserted first (chars > 0) plus the exact equality against the real encoder's own output; neither is satisfiable by an empty result
        # `lane_report`'s own bytes~ column is len(str); the ledger's
        # rendered_bytes is declared UTF-8. This module encodes so its derived
        # total is comparable to the ledgered one it is the control for.
        from helm import inject
        entry = _prior("accented", "café " * 20)
        chars = len(inject._entry_line(entry))
        win = injectpack.fold([_row(1, jit=["accented"])])
        rows, _derived, _missing = self._entries(win, [entry])
        self.assertGreater(chars, 0)          # the rendering is not empty
        self.assertEqual(rows[0]["line_bytes"],
                         len(inject._entry_line(entry).encode("utf-8")))
        self.assertGreater(rows[0]["line_bytes"], chars)

    def test_an_entry_the_store_no_longer_holds_is_marked_not_priced(self):
        win = injectpack.fold([_row(1, jit=["gone", "here"])])
        rows, _derived, _missing = self._entries(win, [_prior("here", "z" * 30)])
        by_id = {r["id"]: r for r in rows}
        self.assertFalse(by_id["gone"]["present"])
        self.assertEqual(by_id["gone"]["bytes"], 0)
        self.assertEqual(by_id["gone"]["actions"], [])
        # the control: the sibling that IS in the store prices and offers doors
        self.assertTrue(by_id["here"]["present"])
        self.assertGreater(by_id["here"]["bytes"], 0)
        self.assertIn("rescope", by_id["here"]["actions"])

    def test_the_operator_digest_is_priced_from_its_own_lines(self):
        # who:operator has no store row, so a renderer that only consults the
        # store prices the whole WHO leg at zero.
        win = injectpack.fold([_row(1, pinned=[injectpack._who_id()])])
        rows, _derived, _missing = self._entries(win, [], who=["ab", "cde"])
        self.assertEqual(rows[0]["id"], injectpack._who_id())
        self.assertEqual(rows[0]["line_bytes"], 5)

    def test_a_store_that_cannot_be_read_is_reported_not_swallowed(self):
        from helm import inject
        win = injectpack.fold([_row(1, jit=["a"])])
        with mock.patch.object(inject, "load_entries",
                               side_effect=OSError("store is gone")):
            rows, derived, missing = injectpack.entries(win)
        self.assertEqual(missing, "store")
        self.assertEqual(derived, 0)
        self.assertEqual([r["id"] for r in rows], ["a"])


class InjectPackActionsTest(unittest.TestCase):
    """The buttons a row offers must be the buttons `store.write` admits."""

    def test_an_always_entry_offers_the_demote_that_takes_it_out_of_the_lane(self):
        acts = injectpack._actions(_prior("p", "s", load_class="always"))
        self.assertIn("demote", acts)
        self.assertNotIn("undemote", acts)
        self.assertIn("rescope", acts)
        self.assertIn("retire", acts)

    def test_a_jit_prior_without_a_demote_receipt_is_offered_no_undemote(self):
        # THE DEFECT THIS ARM CLOSES: `undemote` reads as if it belongs on
        # every entry outside the always lane, which is most of them, and
        # `write.demote(undo=True)` refuses a prior with no receipt to replay.
        # Offered on that reading it would refuse on nearly every row.
        self.assertNotIn("undemote", injectpack._actions(_prior("p", "s")))
        receipted = _prior("p", "s", evidence=[
            {"type": "demoted", "was": {"load_class": "always", "pin": False}}])
        self.assertIn("undemote", injectpack._actions(receipted))

    def test_a_type_that_cannot_hold_the_always_lane_gets_no_tier_button(self):
        heuristic = {"type": "heuristic", "id": "h", "move": "do the thing",
                     "load_class": "jit"}
        acts = injectpack._actions(heuristic)
        self.assertEqual([a for a in acts if a in ("demote", "undemote")], [])
        # a heuristic still has the two doors that work on it
        self.assertEqual(acts, ["rescope", "retire"])

    def test_every_offered_verb_is_one_the_module_will_accept(self):  # noqa: VACUOUS_ASSERTION — the unconditional cardinality assertion proves `offered` has more members than there are entries, so the loop below cannot be empty when it runs
        entries = (_prior("a", "s", load_class="always"), _prior("b", "s"),
                   {"type": "lexicon", "id": "c", "definition": "d"})
        offered = [v for e in entries for v in injectpack._actions(e)]
        # the loop below says nothing if nothing was offered
        self.assertGreater(len(offered), len(entries))
        for verb in offered:
            self.assertIn(verb, injectpack.ACTIONS)


class InjectPackScopeTest(unittest.TestCase):

    def _rows(self, *projects):
        return [{"id": "e%d" % n, "present": True, "project": p, "bytes": 10}
                for n, p in enumerate(projects)]

    def test_entries_recording_no_project_are_counted_as_firing_everywhere(self):
        split = injectpack.scope_split(
            self._rows("fleet", "fleet", "helm"), "helm")
        self.assertEqual(split["fleet"], 2)
        self.assertEqual(split["fleet_bytes"], 20)
        self.assertEqual(split["owned"], 1)
        self.assertEqual(split["off_project"], 0)

    def test_an_entry_about_another_project_is_reported_separately(self):
        # A DIFFERENT AND STRONGER READING than the fleet count: load_entries
        # fences by scope, so one of these reaching this seat is evidence
        # about the fence. Folding it into the fleet number would inflate the
        # fleet figure and hide the fence finding at the same time.
        split = injectpack.scope_split(self._rows("fleet", "clientproj"), "helm")
        self.assertEqual(split["off_project"], 1)
        self.assertEqual(split["off_project_ids"], ["e1"])
        self.assertEqual(split["fleet"], 1)


class InjectPackDerivedNoteTest(unittest.TestCase):

    def test_the_note_is_silent_across_the_measured_undercount_population(self):
        # the control first: this function CAN speak, so the silences below
        # are a decision about their inputs and not a dead code path
        self.assertTrue(injectpack._derived_note(400, 1000))
        self.assertEqual(injectpack._derived_note(1000, 1000), "")
        # 0.6% to 8.7% is what the six busiest live sessions actually produce;
        # a band drawn through that population would print on every window
        self.assertEqual(injectpack._derived_note(994, 1000), "")
        self.assertEqual(injectpack._derived_note(913, 1000), "")

    def test_any_overcount_speaks_however_small(self):
        # Per-entry lines cannot outweigh the delivery that carried them, so
        # size is not the question here — one byte over is the model breaking.
        note = injectpack._derived_note(1001, 1000)
        self.assertIn("MORE", note)
        self.assertIn("1001", note)
        # and the neighbouring undercount of the same size stays silent
        self.assertEqual(injectpack._derived_note(999, 1000), "")

    def test_an_undercount_past_the_band_says_both_numbers(self):
        note = injectpack._derived_note(400, 1000)
        self.assertIn("400", note)
        self.assertIn("1000", note)

    def test_the_unattributed_remainder_is_reported_not_thresholded(self):
        # THE BIAS IS ONE-SIDED AND SITS UNDER ITS OWN ALARM, so it is carried
        # as a number rather than left to a threshold to disclose.
        self.assertEqual(injectpack.unattributed(913, 1000), 87)
        self.assertEqual(injectpack._derived_note(913, 1000), "")
        # never negative, so an overcount cannot read as a negative remainder
        self.assertEqual(injectpack.unattributed(1200, 1000), 0)


class InjectPackViewTest(unittest.TestCase):

    def _view(self, rows, **kw):
        with mock.patch.object(injectpack, "_bind",
                               return_value=("seat-under-test", SESSION,
                                             "helm", None)):
            return injectpack.view("seat-under-test", SESSION, rows=rows, **kw)

    def test_the_model_reports_the_current_window_and_the_session_behind_it(self):
        rows = [_row(1, jit=["a"], lanes={"jit": 50}),
                _row(2, jit=["a"], lanes={"jit": 50}),
                _row(1, jit=["b"], lanes={"jit": 80})]
        model = self._view(rows)
        self.assertEqual(model["state"], "observed")
        self.assertEqual(model["windows"], 2)
        self.assertEqual(model["window"]["turns"], 1)
        self.assertEqual(model["window"]["bytes"], 80)
        self.assertEqual(model["session_total"]["bytes"], 180)
        self.assertEqual([r["id"] for r in model["entries"]], ["b"])

    def test_rows_from_another_session_are_never_folded_in(self):
        rows = [_row(1, jit=["mine"], lanes={"jit": 10}),
                _row(1, jit=["theirs"], lanes={"jit": 999},
                     session=OTHER_SESSION)]
        model = self._view(rows)
        self.assertEqual(model["window"]["bytes"], 10)
        self.assertEqual([r["id"] for r in model["entries"]], ["mine"])

    def test_a_seat_with_no_session_is_told_why_rather_than_shown_zero(self):
        with mock.patch.object(injectpack, "_bind",
                               return_value=("seat-under-test", None, None, None)):
            model = injectpack.view("seat-under-test", None, rows=[])
        self.assertEqual(model["state"], "unknown")
        self.assertIn("session", model["why"])
        self.assertEqual(model["entries"], [])

    def test_a_session_with_no_recorded_turn_is_told_why(self):
        model = self._view([])
        self.assertEqual(model["state"], "unknown")
        self.assertTrue(model["why"])

    def test_an_unreadable_ledger_is_named_rather_than_read_as_empty(self):
        from helm import inject
        with mock.patch.object(injectpack, "_bind",
                               return_value=("seat-under-test", SESSION,
                                             "helm", None)), \
                mock.patch.object(inject, "_ledger_rows",
                                  side_effect=OSError("no ledger")):
            model = injectpack.view("seat-under-test", SESSION)
        self.assertIn("ledger", model["unavailable"])

    def test_the_turn_list_is_capped_and_keeps_the_most_recent(self):
        rows = [_row(n, jit=["a"], lanes={"jit": n}) for n in range(1, 21)]
        model = self._view(rows, cap=5)
        self.assertEqual(len(model["turns"]), 5)
        self.assertEqual([t["turn"] for t in model["turns"]],
                         [16, 17, 18, 19, 20])

    def test_the_whole_model_survives_a_json_round_trip(self):
        # It is served over the wire; a set or a tuple in it is a 500 the
        # owner would read as "the page is broken".
        model = self._view([_row(1, jit=["a"], lanes={"jit": 10})])
        self.assertEqual(json.loads(json.dumps(model))["state"], "observed")


class InjectPackActTest(unittest.TestCase):
    """The edit door, against a real store rather than a mocked writer."""

    ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_STORE_FORCE_NEW")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-injectpack-")
        self.env_prior = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])

    def tearDown(self):
        import shutil
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, eid, **kw):
        ts = pk.now_ts()
        row = {"id": eid, "statement": "a durable thing about the tree",
               "confidence": 0.8, "keywords": "durable thing, tree fact",
               "status": store.STATUS_LIVE, "source": "test",
               "stated_ts": ts, "last_updated": ts}
        row.update(kw)
        store.write_prior(row)
        return eid

    def test_retire_really_stops_the_entry_and_keeps_its_file(self):  # noqa: VACUOUS_ASSERTION — the seeded-LIVE read before the act and the RETIRED read-back from the store after it are unconditional controls on the same entry; a no-op act() fails both
        self._seed("a-durable-thing")
        self.assertEqual(_get("a-durable-thing")["status"], store.STATUS_LIVE)
        out, err = injectpack.act("retire", "a-durable-thing",
                                  reason="it fires on every project")
        self.assertIsNone(err)
        self.assertEqual(out["status"], store.STATUS_RETIRED)
        after = _get("a-durable-thing")
        self.assertEqual(after["status"], store.STATUS_RETIRED)
        self.assertTrue(os.path.exists(after["path"]))

    def test_rescope_records_the_project_the_entry_is_about(self):
        self._seed("a-scoped-thing")
        out, err = injectpack.act("rescope", "a-scoped-thing", owner="helm")
        self.assertIsNone(err)
        self.assertEqual(out["project"], "helm")
        self.assertEqual(_get("a-scoped-thing")["project"], "helm")

    def test_demote_takes_an_always_entry_out_of_the_lane_and_undo_restores_it(self):
        self._seed("an-always-thing", load_class="always")
        out, err = injectpack.act("demote", "an-always-thing",
                                  reason="it does not need to be on every turn")
        self.assertIsNone(err)
        self.assertEqual(out["load_class"], "jit")
        back, err2 = injectpack.act("undemote", "an-always-thing",
                                    reason="put it back")
        self.assertIsNone(err2)
        self.assertEqual(back["load_class"], "always")

    def test_an_unknown_action_is_refused_before_anything_is_written(self):  # noqa: VACUOUS_ASSERTION — `err` is asserted non-empty and its text matched, and the entry is read back LIVE from the store, so the refusal is proven to have happened rather than merely not-succeeded
        self._seed("an-untouched-thing")
        out, err = injectpack.act("delete", "an-untouched-thing", reason="x")
        self.assertTrue(err)
        self.assertIsNone(out)
        self.assertIn("unknown action", err)
        self.assertEqual(_get("an-untouched-thing")["status"],
                         store.STATUS_LIVE)

    def test_a_tier_flip_without_a_reason_is_refused(self):
        self._seed("a-reasonless-thing", load_class="always")
        out, err = injectpack.act("demote", "a-reasonless-thing")
        self.assertIsNone(out)
        self.assertIn("reason", err)
        self.assertEqual(_get("a-reasonless-thing")["load_class"],
                         "always")

    def test_the_writers_own_refusal_reaches_the_caller_unchanged(self):
        self._seed("a-jit-thing")
        out, err = injectpack.act("demote", "a-jit-thing", reason="try it")
        self.assertIsNone(out)
        self.assertIn("not in the always lane", err)

    def _seed_in_project_root(self, eid, project):
        """Write a live prior into the PROJECT root, where `roots(None)` does
        not look. `store.roots()` returns adopted-global plus helm-global and
        adds the project roots only when it is given a project, so an entry
        written here is the exact population the write door can lose."""
        from helm.store._common import TYPE_SUBDIR
        root = os.path.join(home.project_dir(project), TYPE_SUBDIR["prior"])
        os.makedirs(root, exist_ok=True)
        ts = pk.now_ts()
        store_write.write_prior({
            "id": eid, "statement": "a thing that lives under a project root",
            "confidence": 0.8, "keywords": "project resident, root thing",
            "status": store.STATUS_LIVE, "source": "test",
            "stated_ts": ts, "last_updated": ts}, root_dir=root)
        return eid

    def test_an_entry_in_a_project_root_is_invisible_without_the_project(self):
        # THE NEGATIVE POLE, and the reason the page must forward its scope:
        # this is what the owner's click did when the body carried no project.
        self._seed_in_project_root("a-project-root-thing", "someproj")
        self.assertIsNone(store.find_typed("a-project-root-thing"))
        out, err = injectpack.act("retire", "a-project-root-thing",
                                  reason="pressed without a scope")
        self.assertIsNone(out)
        self.assertIn("not found", err)
        # and the entry is untouched, so the refusal cost nothing but the click
        self.assertEqual(
            store.find_typed("a-project-root-thing", project="someproj")["status"],
            store.STATUS_LIVE)

    def test_the_write_lands_on_a_project_root_entry_when_the_scope_travels(self):
        # THE POSITIVE POLE: the same id, the same verb, the scope forwarded —
        # and the assertion is the STORE read-back, not act()'s return value,
        # because the door is what is under test and not its reporting.
        self._seed_in_project_root("a-scoped-root-thing", "someproj")
        out, err = injectpack.act("retire", "a-scoped-root-thing",
                                  reason="pressed with the seat's scope",
                                  project="someproj")
        self.assertIsNone(err)
        self.assertEqual(out["id"], "a-scoped-root-thing")
        self.assertEqual(
            store.find_typed("a-scoped-root-thing", project="someproj")["status"],
            store.STATUS_RETIRED)

    def test_rescope_also_reaches_a_project_root_entry(self):
        # rescope is the verb the page leads with, and it resolves its operand
        # through the same `_find`, so it loses the same population.
        self._seed_in_project_root("a-rescopable-root-thing", "someproj")
        out, err = injectpack.act("rescope", "a-rescopable-root-thing",
                                  owner="someproj", project="someproj")
        self.assertIsNone(err)
        self.assertEqual(out["project"], "someproj")

    def test_an_id_that_does_not_exist_is_refused_by_name(self):
        out, err = injectpack.act("retire", "no-such-entry-at-all",
                                  reason="probe")
        self.assertIsNone(out)
        self.assertIn("no-such-entry-at-all", err)


class InjectPackWebTest(unittest.TestCase):

    def test_both_routes_are_registered_on_the_right_verbs(self):
        from helm import web
        self.assertIn("/api/inject/pack", web.QUERY_API)
        self.assertIn("/api/inject/act", web.POST_API)
        # a read route must not also be a mutation door
        self.assertNotIn("/api/inject/pack", web.POST_API)
        self.assertIs(web.QUERY_API["/api/inject/pack"], web._api_inject_pack)
        self.assertIs(web.POST_API["/api/inject/act"], web._api_inject_act)

    def test_the_read_endpoint_answers_a_model_rather_than_raising(self):
        from helm import web
        with mock.patch.object(injectpack, "view",
                               side_effect=RuntimeError("ledger exploded")):
            obj, status = web._api_inject_pack({"seat": ["seat-under-test"]})
        self.assertEqual(status, 200)
        self.assertEqual(obj["unavailable"], ["pack"])
        self.assertIn("ledger exploded", obj["why"])

    def test_a_refused_edit_answers_400_with_the_writers_sentence(self):
        from helm import web
        obj, status = web._api_inject_act({"action": "sideways", "id": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(obj["code"], "refused")
        self.assertIn("sideways", obj["error"])

    def test_an_accepted_edit_answers_200_and_says_what_changed(self):
        from helm import web
        with mock.patch.object(injectpack, "act",
                               return_value=({"id": "e", "action": "rescope",
                                              "project": "helm"}, None)):
            obj, status = web._api_inject_act({"action": "rescope", "id": "e",
                                               "owner": "helm"})
        self.assertEqual(status, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["project"], "helm")


class InjectPackFragmentTest(unittest.TestCase):

    def test_the_pane_that_hosts_the_pack_also_loads_it(self):
        # A HOST DIV WITH NO LOADER IS A PERMANENT SPINNER. The two live in
        # different fragments on purpose, so nothing but an arm keeps them
        # paired.
        from helm import web_ui_loader
        page = web_ui_loader.read_bytes()
        self.assertIn(b'id="ipkhost"', page)
        self.assertIn(b"async function ipkLoad", page)
        # the call site is guarded so a missing fragment cannot take the seat
        # identity down with it; this arm is what keeps the guard honest by
        # proving the fragment it guards against is actually shipped
        self.assertIn(b'typeof ipkLoad === "function"', page)
        self.assertIn(b"/api/inject/pack", page)
        self.assertIn(b"/api/inject/act", page)

    def test_the_edit_body_carries_the_scope_the_read_was_made_with(self):
        # The door arms above prove `act()` needs the project; this proves the
        # page SENDS it. Both are required: the door arms pass while the caller
        # omits the field, which is exactly how it shipped missing.
        from helm import web_ui_loader
        page = web_ui_loader.read_text()
        body = page[page.index("async function ipkAct("):]
        body = body[:body.index("\n}")]
        self.assertIn("/api/inject/act", body)      # the slice is the sender
        self.assertIn("IPK_PACK.project", body)
        self.assertIn("project:", body)

    def test_the_page_carries_no_cost_or_credential_wording(self):
        # The standing constraint on this surface: nobody has measured whether
        # these bytes explain any credential burn, and injections are
        # cache-read on most turns, so a cost column would launder an
        # unmeasured claim into a dashboard. Bytes and repeats only.
        #
        # THE FIRST SPELLING OF THIS ARM SEARCHED FOR "$" AND CAUGHT THE DOM
        # HELPER, which is what a needle chosen for looking strict rather than
        # for naming the thing does. The needles below are words that only a
        # money column would bring.
        from helm import web_ui_loader
        page = web_ui_loader.read_text()
        start = page.index("THE INJECTION PACK")
        # THE ANCHOR MUST BE UNIQUE, NOT MERELY CONTAINING: "function ipkAct"
        # matches `ipkActs` sixty lines earlier and silently cut the slice
        # short of the fetch it was meant to cover, which the control below
        # is what caught.
        body = page[start:page.index("async function ipkAct(", start)]
        for word in ("USD", "dollar", "cents", "credits", "$" + "{cost",
                     "cost per", "spend"):
            self.assertNotIn(word, body)
        # the control: the slice really is the pack section and not an empty
        # string that would satisfy every line above
        self.assertIn("ipkB", body)
        self.assertIn("/api/inject/pack", body)

    def test_the_wire_model_cannot_grow_a_cost_field_unnoticed(self):
        # An absence proved by a whitelist rather than by a search: a future
        # field named anything at all fails here, which a list of forbidden
        # words never would.
        with mock.patch.object(injectpack, "_bind",
                               return_value=("seat-under-test", SESSION,
                                             "helm", None)):
            model = injectpack.view("seat-under-test", SESSION,
                                    rows=[_row(1, jit=["a"], lanes={"jit": 9})])
        self.assertEqual(set(model), {
            "requested", "session", "project", "state", "window",
            "session_total", "windows", "entries", "turns", "unavailable",
            "derived", "unattributed", "derived_note", "why", "scope"})
        self.assertEqual(set(model["window"]), {
            "turns", "silent", "bytes", "lanes", "fires", "suppressed_fires",
            "suppressed_bytes", "candidates", "out_of_order", "first_ts",
            "last_ts", "last_turn", "distinct", "repeat", "per_turn",
            "index", "of"})
        self.assertEqual(set(model["entries"][0]), {
            "id", "lane", "fires", "last_turn", "line_bytes", "bytes",
            "present", "type", "load_class", "text", "project", "scope_how",
            "actions"})


class AnEntryPreviewSaysWhenItIsShortTest(unittest.TestCase):
    """The row's `text` is a preview of a gloss whose whole text lives in the
    store. Cut where a sentence turns, a silent preview can invert what the
    entry appears to say — on the surface built to decide which entries to
    change."""

    def preview(self, statement):
        from helm import inject
        win = injectpack.fold([_row(1, jit=["long"])])
        with mock.patch.object(inject, "load_entries",
                               return_value=[_prior("long", statement)]), \
                mock.patch.object(inject, "_who_lines", return_value=[]):
            rows, _derived, _missing = injectpack.entries(win)
        return rows[0]["text"]

    def test_a_long_gloss_is_cut_and_says_so(self):
        statement = "the number belongs beside the claim " * 20
        got = self.preview(statement)
        self.assertIn("[cut: 400 of %d chars]" % len(statement), got)
        self.assertEqual(got, pk.cut_marked(statement, 400))

    def test_a_short_gloss_is_byte_identical(self):
        """The must-hit control: under the bound the preview IS the gloss."""
        self.assertEqual(self.preview("short and whole"), "short and whole")


if __name__ == "__main__":
    unittest.main()
