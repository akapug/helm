#!/usr/bin/env python3
"""A HELM OLDER THAN THE LEDGER MUST NOT WRITE ON A ROW IT CANNOT READ.

THE INCIDENT (task/3024). A seat ran `./bin/helm` from a tree older than the
`findings-note` event kind. Its dispatch fold had no arm for that kind, so it
did not advance the row's `seq` past it: it read a cancelled row as open, and
every verdict it wrote took `row seq + 1` from that fold, which is the seq the
note already held. The trunk fold drops an event whose seq is not the next
one, so four FIX verdicts are on the ledger and no reader honours them. The
writer's own self-check passed, because it validated with the same old fold.

THE CURE, AND WHAT EACH CLASS BELOW PINS:
  * the fold records, per row, every kind it has no arm for, and the set of
    kinds it knows is one derived constant held against every kind the
    writers emit, in both directions (`TheFoldKnowsItsVocabularyTest`);
  * every writer that appends a seq refuses such a row before it computes
    one, naming the kind and the cure, and writes nothing
    (`EveryWriterRefusesARowItCannotReadTest`);
  * readers keep reading and say which rows they cannot read in full
    (`ReadersKeepReadingAndSayTest`);
  * the doctor names every event the fold dropped for a reused seq on a row
    that is still live, counts the ones on rows that have ended, and
    `helm dispatch collisions` lists them all (`SeqCollisionsSurfaceTest`).
The stale-tree line lives beside its sibling in `tests.test_cli_tree_warning`.

A newer helm is simulated the only way one can be from inside this tree: an
event of a kind this helm has no arm for is appended at the row's next seq,
which is byte for byte what a newer writer's append looks like to this fold.
"""
import ast
import json
import os
import re
import types
import unittest
from unittest import mock

from helm import (dispatches, dispatches_close, doctor, eventledger,
                  findingspass, stalebot)
from tests import test_dispatches as td
from tests import test_lr_retire as lr_retire
from tests._satellite_resolution import ledger_sources

FUTURE = "future-kind"
#: The cure names the tree this helm came from — derived here from the
#: package's own file, never from the code under test.
CURE = "git -C %s merge --ff-only origin/main" % os.path.dirname(
    os.path.dirname(os.path.abspath(dispatches.__file__)))


class _Base(lr_retire.RetireBase):
    """Rows, the planted newer event, and the ledger's bytes."""

    _lane = 0

    def row(self, **kw):
        type(self)._lane += 1
        return self.open_row(lane="lane/unknown-kind-%d" % type(self)._lane,
                             **kw)

    def state(self, rid):
        return dispatches.snapshot()[0][rid]

    def plant(self, rid, kind=FUTURE):
        """What a NEWER helm appends: an event of a kind this one has no arm
        for, at the row's next seq."""
        event = {"v": 3, "event": kind, "seq": self.state(rid)["seq"] + 1,
                 "id": rid, "ts": dispatches.pk.now_ts()}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        return event

    def ledger(self):
        with open(dispatches.ledger_path(), "rb") as f:
            return f.read()

    def assertRefusedUnread(self, result, before, rid, noun="dispatch"):
        """Refused by THE VOCABULARY RUNG — the text only that rung writes —
        and nothing appended."""
        self.assertIsNotNone(result, "the writer returned nothing at all")
        err = result[1]
        self.assertIsNone(result[0], "a writer handed back a row: %r" % (err,))
        self.assertIn(FUTURE, str(err))
        self.assertIn("this helm does not know", str(err))
        self.assertIn("%s %s" % (noun, rid[:12]), str(err))
        self.assertIn(CURE, str(err), "the refusal must name the cure")
        self.assertEqual(self.ledger(), before,
                         "a refused writer appended to the ledger")


class TheFoldKnowsItsVocabularyTest(_Base):
    """The fold stays tolerant, records what it cannot read on THAT row only,
    and knows exactly the kinds the writers emit plus the declared history."""

    def test_a_row_of_known_kinds_carries_no_field(self):
        """BYTE-IDENTICAL FOR EVERY ROW THAT NEEDS NOTHING: the field is
        absent, not empty, so no existing row changes shape."""
        row = self.row()
        dispatches._mark_delivered(row["id"], "post-known")
        dispatches.mark_hold(row["id"], "known kinds only")
        got = self.state(row["id"])
        self.assertEqual(got["seq"], 2, "the prefix did not fold, so the "
                         "absence below measures nothing")
        self.assertNotIn(dispatches.UNKNOWN_KINDS_FIELD, got)
        self.assertEqual(dispatches.unknown_event_kinds(got), ())

    def test_an_unknown_kind_is_recorded_on_ITS_row_and_the_fold_keeps_reading(self):  # noqa: VACUOUS_ASSERTION — the field's PRESENCE on the planted row is asserted first and unconditionally on the same snapshot; the other row's absence is read against it
        row, other = self.row(), self.row()
        before = self.state(row["id"])
        self.plant(row["id"])
        self.plant(row["id"], "another-kind")
        got = self.state(row["id"])
        self.assertEqual(got[dispatches.UNKNOWN_KINDS_FIELD],
                         ("another-kind", FUTURE))
        # TOLERANT: the row's reading is what it was, seq included — the event
        # is recorded, never taken.
        self.assertEqual({k: v for k, v in got.items()
                          if k != dispatches.UNKNOWN_KINDS_FIELD}, before)
        _state, _events, taken, _v, err = dispatches.snapshot_and_events()
        self.assertIsNone(err, err)
        self.assertEqual([e.get("event") for e in taken[row["id"]]],
                         ["dispatch"], "an unknown event was TAKEN")
        # A DIFFERENT ROW IS UNTOUCHED.
        self.assertNotIn(dispatches.UNKNOWN_KINDS_FIELD, self.state(other["id"]))

    def test_a_hostile_kind_is_recorded_by_a_bounded_printable_label(self):
        row = self.row()
        for kind in (7, ["x"], "bad\x1b[2Jkind" + "k" * 200):
            dispatches_seq = self.state(row["id"])["seq"]
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": kind, "seq": dispatches_seq + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts()}))
        kinds = self.state(row["id"])[dispatches.UNKNOWN_KINDS_FIELD]
        self.assertIn("<int>", kinds)
        self.assertIn("<list>", kinds)
        text = [k for k in kinds if k.startswith("bad")]
        self.assertEqual(len(text), 1, kinds)
        self.assertTrue(text[0].isprintable() and len(text[0]) <= 64, text)

    def test_the_historical_spellings_are_known(self):
        """A v1 snapshot carries no `event` key; v2 opened rows with `add`
        and `posting`. The fold reads all three, so none is UNKNOWN."""
        self.assertFalse(dispatches._is_known_kind(FUTURE),
                         "the predicate knows everything, so the loop below "
                         "measures nothing")
        self.assertTrue(dispatches._is_known_kind("add"),
                        "a v2 opener reads as unknown")
        for kind in (None, "", "retarget", "add", "posting"):
            self.assertTrue(dispatches._is_known_kind(kind), kind)


def _emitted_kinds(sources):
    """{kind} every writer function in `sources` puts in an `event` field
    of a ledger-bound dict — literals AND module-level string constants (the
    `advisory-read` writer spells its kind `ADVISORY_READ_EVENT`) — plus the
    kinds that go to the ATTEST ledger instead, separately."""
    tree = ast.parse("\n".join(source for _path, source in sources))
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            constants[node.targets[0].id] = node.value.value

    def kind_of(value):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            return constants.get(value.id)
        return None

    def calls(node, name):
        return any(isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                   and sub.func.id == name for sub in ast.walk(node))

    ledger, attest = set(), set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kinds = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Dict):
                kinds |= {kind_of(v) for k, v in zip(sub.keys, sub.values)
                          if isinstance(k, ast.Constant) and k.value == "event"}
        kinds.discard(None)
        if calls(fn, "attest_path") and not calls(fn, "ledger_path"):
            attest |= kinds
        else:
            ledger |= kinds
    return ledger, attest


class TheVocabularyIsTheWritersBothWaysTest(unittest.TestCase):
    """THE SINGLE PLACE A NEW KIND IS ADDED. `KNOWN_EVENT_KINDS` is derived
    from `LEDGER_EVENT_ACTORS`; this arm holds it against every kind the
    writer modules emit. A kind a writer starts emitting without teaching the
    fold goes red here, and so does a registry entry nobody writes that is not
    declared history."""

    def test_every_emitted_kind_is_known_and_every_known_kind_is_emitted_or_history(self):  # noqa: VACUOUS_ASSERTION — three unconditional must-hits run first on the same walk, and test_the_census_can_go_red drives the same subtraction to a non-empty answer
        emitted, attest = _emitted_kinds(ledger_sources(dispatches))
        # THE MUST-HITS: the kind the incident's binary could not read, the
        # kind spelled through a constant, and a kind of the OTHER ledger.
        self.assertIn("findings-note", emitted)
        self.assertIn("advisory-read", emitted,
                      "the walk cannot read a kind spelled as a constant")
        self.assertTrue(attest, "the walk found no attest-ledger kind, so it "
                        "cannot tell the two ledgers apart")
        self.assertEqual(sorted(emitted - dispatches.KNOWN_EVENT_KINDS), [],
                         "a writer emits a kind the fold does not know — add "
                         "it to LEDGER_EVENT_ACTORS")
        self.assertEqual(
            sorted(dispatches.KNOWN_EVENT_KINDS - emitted),
            sorted(dispatches.HISTORICAL_EVENT_KINDS),
            "the vocabulary carries a kind nobody writes and nobody declared "
            "historical, or a declared-historical kind is written again")
        self.assertEqual(sorted(attest & dispatches.KNOWN_EVENT_KINDS), [])
        self.assertEqual(dispatches.KNOWN_EVENT_KINDS,
                         frozenset(dispatches.LEDGER_EVENT_ACTORS),
                         "the vocabulary is no longer derived from the one "
                         "registry literal, so there are two lists")

    def test_the_census_can_go_red(self):
        """THE POSITIVE CONTROL ON THE DECISION: a writer emitting a kind
        through a constant the registry lacks is found by the same walk."""
        source = ("NEW_KIND = 'brand-new-kind'\n"
                  "def writer(rid):\n"
                  "    path = ledger_path()\n"
                  "    return {'v': 3, 'event': NEW_KIND, 'seq': 1, 'id': rid}\n")
        emitted, _attest = _emitted_kinds([("synthetic.py", source)])
        self.assertEqual(sorted(emitted - dispatches.KNOWN_EVENT_KINDS),
                         ["brand-new-kind"])


def _seq_writers(sources):
    """{function name} of every function that builds a ledger event whose
    `seq` is computed as something PLUS ONE — a dict literal's "seq" key or a
    `seq=` keyword. That is the one expression every seq writer shares; a
    reducer assigns `seq=expected` or copies a recorded value, never adds."""
    tree = ast.parse("\n".join(source for _path, source in sources))
    found = set()

    def plus_one(node):
        return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)

    # THE WRITER IS THE OUTERMOST FUNCTION: its body runs as a nested
    # `attempt` (`dispatches._ledger_write`), which is no writer of its own.
    outer = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    outer += [m for n in tree.body if isinstance(n, ast.ClassDef)
              for m in n.body
              if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in outer:
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "seq"
                    and plus_one(v) for k, v in zip(sub.keys, sub.values)):
                found.add(fn.name)
            if isinstance(sub, ast.Call) and any(
                    kw.arg == "seq" and plus_one(kw.value)
                    for kw in sub.keywords):
                found.add(fn.name)
    return found


#: EVERY SEQ WRITER, and the arm (a `_WRITERS` key) that proves it refuses.
#: A new seq writer is red in `test_every_seq_writer_is_in_the_matrix` until
#: it is named here and exercised below.
_SEQ_WRITER_ARMS = {
    "_append_dispatch": ("add --supersedes", "send --supersedes",
                         "successor race"),
    "_mark_delivered": ("delivered",),
    "record_findings_note": ("findings-note",),
    "mark_verdict": ("verdict", "advisory-read"),
    "mark_cancel": ("cancel",),
    "mark_custody": ("custody",),
    "mark_hold": ("hold",),
    "mark_release": ("release",),
    "superseded_parent_sweep": ("superseded sweep",),
    "retip": ("retip", "retip race"),
    "_record_discharge_proven": ("discharge",),
    "_record_withdraw_proven": ("withdraw",),
    "_record_abandon_proven": ("abandon",),
    "_record_retire_proven": ("retire",),
    "_record_close_landed_proven": ("close-landed",),
    "_record_close_proven": ("close",),
    "record_delivered_report_correction": ("close-correction",),
    "_record_retract": ("retract",),
}


def _prepare_retip(test, rid):
    if not getattr(test, "retip_target", None):
        lr_retire._prepare_retip_car(test, rid)


def _custody(test, rid):
    from helm import takeover
    auth = types.SimpleNamespace(target="seat-b", source="ghost-author",
                                 reason="the unknown-kind matrix")
    with mock.patch.object(takeover, "reassign_custody_error",
                           return_value=None):
        return dispatches.mark_custody(rid, auth)


def _successor(test, rid, via):
    type(test)._lane += 1
    lane = "lane/successor-%d" % test._lane
    with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "ghost-author"}):
        if via == "send":
            return dispatches.send("ghost-reviewer", lane, "review this tip",
                                   test.side, repo=test.repo, sign=False,
                                   kind="review", supersedes=rid)
        return dispatches.add("ghost-reviewer", lane, ref=test.side,
                              repo=test.repo, kind="review", notify=False,
                              supersedes=rid, _reason=True)


_REPORT = ("git:unknown-kind-matrix", "0123456789ab", "handed off")

#: (arm, prepare(test, rid) or None, write(test, rid)) — every writer's door.
_WRITERS = (
    ("delivered", None,
     lambda test, rid: dispatches._mark_delivered(rid, "matrix-probe")),
    ("hold", None,
     lambda test, rid: dispatches.mark_hold(rid, "matrix probe hold")),
    ("cancel", None,
     lambda test, rid: dispatches.mark_cancel(rid, "matrix probe cancel")),
    ("release",
     lambda test, rid: dispatches.mark_hold(rid, "matrix probe predecessor"),
     lambda test, rid: dispatches.mark_release(rid)),
    ("retip", _prepare_retip,
     lambda test, rid: dispatches.retip(
         rid, test.retip_target, reason="matrix probe", repo=test.repo,
         notify=False)),
    ("verdict",
     lambda test, rid: dispatches._mark_delivered(rid, "matrix-probe"),
     lambda test, rid: dispatches.mark_verdict(rid, test.side, "matrix probe",
                                               polarity="fix")),
    ("advisory-read", None, lr_retire._write_advisory_read),
    ("findings-note", None,
     lambda test, rid: dispatches.record_findings_note(
         rid, test.side, dict(lr_retire._FINDINGS_NOTE_FIELDS))),
    ("custody", None, _custody),
    ("add --supersedes", None, lambda test, rid: _successor(test, rid, "add")),
    ("send --supersedes", None,
     lambda test, rid: _successor(test, rid, "send")),
    ("rebind", None,
     lambda test, rid: dispatches.rebind(rid, "seat-z", reason="matrix",
                                         force=True, repo=test.repo,
                                         notify=False)),
    ("discharge", None,
     lambda test, rid: dispatches_close._record_discharge_proven(
         rid, test.side, test.a, "f" * 32, "matrix", "landed", "local")),
    ("withdraw", None,
     lambda test, rid: dispatches_close._record_withdraw_proven(
         rid, test.side, "matrix")),
    ("abandon", None,
     lambda test, rid: dispatches_close._record_abandon_proven(
         rid, test.side, "matrix", lambda *a: False, lambda *a: None)),
    ("retire", None,
     lambda test, rid: dispatches_close._record_retire_proven(
         rid, dispatches.RETIRE_REASONS[0], "seat-a", None,
         lambda *a: (None, "matrix"))),
    ("close-landed", None,
     lambda test, rid: dispatches_close._record_close_landed_proven(
         rid, test.side, os.path.realpath(os.path.join(test.repo, ".git")),
         "refs/heads/%s" % test.main, test.a, "ancestor")),
    ("close", None,
     lambda test, rid: dispatches_close._record_close_proven(
         rid, "delivered-report", None, evidence=_REPORT[2],
         artifact_ref=_REPORT[0], report_ref=_REPORT[1])),
    ("close-correction", None,
     lambda test, rid: dispatches_close.record_delivered_report_correction(
         rid, *_REPORT)),
    ("retract",
     lambda test, rid: dispatches.mark_verdict(rid, test.side, "matrix probe",
                                               polarity="fix"),
     lambda test, rid: dispatches.retract(rid, "matrix probe", "unknown",
                                          "inferred")),
    ("findings pass", None,
     lambda test, rid: findingspass._run_locked(rid, 1)),
    ("stale-cure redispatch", None,
     lambda test, rid: stalebot.redispatch_cured(rid, "seat-z",
                                                 repo=test.repo)),
)

#: The arms whose PRODUCTION success on a clean row this fixture can show:
#: the clean twin appends at least one event. The rest need proofs this
#: fixture does not mint, so their clean twin is shown to get PAST the
#: vocabulary rung (its answer is some other one) — never refused by it.
_SUCCEEDS_CLEAN = {"delivered", "hold", "cancel", "release", "retip",
                   "verdict", "advisory-read", "findings-note", "custody",
                   "add --supersedes", "send --supersedes"}


class EveryWriterRefusesARowItCannotReadTest(_Base):
    """BEFORE IT COMPUTES A SEQ, NAMING THE KIND AND THE CURE, WRITING
    NOTHING. And only that row: a clean row beside one carrying an unknown
    kind is written exactly as before."""

    def test_every_seq_writer_is_in_the_matrix(self):  # noqa: VACUOUS_ASSERTION — the census must hit mark_verdict unconditionally before it is compared to a seventeen-entry map
        found = _seq_writers(ledger_sources(dispatches))
        self.assertIn("mark_verdict", found, "the census reads nothing")
        self.assertEqual(sorted(found), sorted(_SEQ_WRITER_ARMS),
                         "a function builds a seq-bearing ledger event and is "
                         "not in the writer matrix, or one left and the map "
                         "still names it")
        arms = {name for name, _p, _w in _WRITERS} | {
            "successor race", "retip race", "superseded sweep"}
        for writer, named in _SEQ_WRITER_ARMS.items():
            self.assertTrue(set(named) <= arms, (writer, named))

    def test_each_writer_refuses_a_row_it_cannot_read(self):  # noqa: VACUOUS_ASSERTION — each refusal is read by its text, which only the vocabulary rung writes; the unchanged ledger is controlled by the clean-twin method, where the same writers append
        for name, prepare, write in _WRITERS:
            with self.subTest(writer=name):
                row = self.row()
                if prepare:
                    prepare(self, row["id"])
                self.plant(row["id"])
                before = self.ledger()
                result = write(self, row["id"])
                self.assertRefusedUnread(
                    result, before, row["id"],
                    noun="--supersedes dispatch" if "supersedes" in name
                    else "dispatch")

    def test_each_writer_on_a_CLEAN_row_beside_one_it_cannot_read(self):  # noqa: VACUOUS_ASSERTION — the absent refusal is paired per writer with the ledger GROWING for every writer this fixture can drive to success
        """THE POSITIVE CONTROL, AND THE DIFFERENT-ROW CELL AT ONCE: an
        unknown kind on ANOTHER row refuses nothing here."""
        for name, prepare, write in _WRITERS:
            with self.subTest(writer=name):
                self.plant(self.row()["id"])           # the other row
                row = self.row()
                if prepare:
                    prepare(self, row["id"])
                before = self.ledger()
                result = write(self, row["id"])
                err = result[1] if result else None
                self.assertNotIn("this helm does not know", str(err),
                                 "the vocabulary rung fired on a clean row")
                if name in _SUCCEEDS_CLEAN:
                    self.assertIsNone(err, err)
                    self.assertGreater(len(self.ledger()), len(before),
                                       "the clean twin appended nothing, so "
                                       "the refusal arm proves only a no")

    def test_a_successor_racing_a_newer_event_is_refused_before_it_lands(self):  # noqa: VACUOUS_ASSERTION — the refusal text naming the kind and the parent is asserted unconditionally before the absence of the successor row
        """THE LOCKED HALF OF `add`/`send --supersedes`. The chain resolves on
        an unlocked read; an event this helm cannot read lands on the parent
        before the lock. The successor must not land either: refusing after
        it would leave a half-written move."""
        parent = self.row()
        real = dispatches._resolve_chain

        def racing(*a, **k):
            out = real(*a, **k)
            self.plant(parent["id"])
            return out

        with mock.patch.object(dispatches, "_resolve_chain", racing):
            before_rows = set(dispatches.snapshot()[0])
            out, err = _successor(self, parent["id"], "add")
        self.assertIsNone(out)
        self.assertIn(FUTURE, err)
        self.assertIn("--supersedes dispatch %s" % parent["id"][:12], err)
        self.assertEqual(set(dispatches.snapshot()[0]), before_rows,
                         "the successor row landed before the refusal")
        self.assertNotIn("superseded_by", self.state(parent["id"]))

    def test_a_retip_racing_a_newer_event_is_refused_under_the_lock(self):  # noqa: VACUOUS_ASSERTION — the refusal text naming the kind is asserted unconditionally before the unchanged tip
        row = self.row()
        _prepare_retip(self, row["id"])
        real = dispatches._retip_identity

        def racing(*a, **k):
            out = real(*a, **k)
            self.plant(row["id"])
            return out

        with mock.patch.object(dispatches, "_retip_identity", racing):
            result = dispatches.retip(row["id"], self.retip_target,
                                      reason="race", repo=self.repo,
                                      notify=False)
        self.assertIsNone(result[0])
        self.assertIn(FUTURE, result[1])
        self.assertIn("dispatch %s" % row["id"][:12], result[1])
        self.assertEqual(self.state(row["id"])["tip"], row["tip"])

    def test_the_sweep_refuses_only_the_parent_it_cannot_read(self):  # noqa: VACUOUS_ASSERTION — the dry run must select both parents and the clean one must be annotated, both unconditional, beside the unread parent's absence
        """ONE UNREADABLE PARENT REFUSES ONLY ITSELF."""
        pairs = []
        for _ in range(2):
            parent = self.row()
            with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "ghost-author"}):
                type(self)._lane += 1
                kid = dispatches.add("ghost-reviewer",
                                     "lane/sweep-kid-%d" % self._lane,
                                     ref=self.side, repo=self.repo,
                                     kind="review", notify=False,
                                     supersedes=parent["id"], force=True)
            self.assertIsNotNone(kid)
            pairs.append((parent["id"], kid["id"]))
        unread, clean = pairs
        self.plant(unread[0])
        with mock.patch.object(dispatches, "_successor_finished",
                               return_value=True):
            hits, err = dispatches.superseded_parent_sweep()
            self.assertIsNone(err, err)
            self.assertEqual(sorted(hits), sorted(pairs),
                             "the dry run did not select both parents, so the "
                             "refusal below discriminates nothing")
            done, err = dispatches.superseded_parent_sweep(apply=True)
        self.assertEqual(done, [clean])
        self.assertIn(FUTURE, err)
        self.assertIn(unread[0][:12], err)
        self.assertNotIn("superseded_by", self.state(unread[0]))
        self.assertEqual(self.state(clean[0]).get("superseded_by"), clean[1])

    def test_notify_failed_carries_no_seq_and_is_not_refused(self):  # noqa: VACUOUS_ASSERTION — the positive is the ledger GROWING and the marker reading back, both unconditional
        """THE ONE LEDGER WRITE THAT IS NOT A SEQ WRITER: a notify-failed
        marker carries no seq and moves no state, so it cannot collide, and a
        durable failure trail is not dropped for a row this helm cannot read."""
        row = self.row()
        self.plant(row["id"])
        before = len(self.ledger())
        dispatches._record_notify_failed(row["id"], "matrix")
        self.assertGreater(len(self.ledger()), before)
        self.assertIsNotNone(dispatches._notify_failed_for(row["id"]))


class ReadersKeepReadingAndSayTest(_Base):
    """A READER NEVER REFUSES: every listing still prints the row, beside the
    sentence saying this helm stopped reading it at an event it does not know,
    and prints nothing new for a row whose kinds are all known."""

    NOTE = "LEDGER NEWER THAN THIS HELM (unknown event kind: %s)" % FUTURE

    def _pair(self):
        unread, clean = self.row(), self.row()
        self.plant(unread["id"])
        return unread, clean

    def _line(self, out, rid):
        lines = [line for line in out.splitlines() if rid[:12] in line]
        self.assertTrue(lines, "row %s is not in the listing:\n%s"
                        % (rid[:12], out))
        return lines[0]

    def test_dispatch_list_prints_both_rows_and_marks_one(self):
        unread, clean = self._pair()
        rc, out, err = _run_dispatch(["list", "--all-projects"])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.NOTE, self._line(out, unread["id"]))
        self.assertNotIn("LEDGER NEWER", self._line(out, clean["id"]))

    def test_dispatch_list_json_carries_the_kinds_on_that_row_only(self):
        import json
        unread, clean = self._pair()
        rc, out, err = _run_dispatch(["list", "--all-projects", "--json"])
        self.assertEqual(rc, 0, err)
        rows = {r["id"]: r for r in json.loads(out)}
        self.assertEqual(rows[unread["id"]][dispatches.UNKNOWN_KINDS_FIELD],
                         [FUTURE])
        self.assertNotIn(dispatches.UNKNOWN_KINDS_FIELD, rows[clean["id"]])

    def test_triage_measures_the_named_row_and_marks_it(self):
        unread, clean = self._pair()
        rc, out, err = _run_dispatch(["triage", unread["id"], clean["id"],
                                      "--all-projects"])
        self.assertNotIn("does not know", err, "triage REFUSED a row")
        self.assertIn(rc, (0, 1), err)
        self.assertIn("[%s]" % self.NOTE, self._line(out, unread["id"]))
        self.assertNotIn("LEDGER NEWER", self._line(out, clean["id"]))

    def test_lr_show_prints_the_row_and_says_where_its_reading_stopped(self):
        unread, clean = self._pair()
        rc, out, err = lr_retire.run(["show", unread["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("LAND REQUEST %s" % unread["id"], out)
        self.assertIn("  ledger    " + self.NOTE, out)
        rc, out, err = lr_retire.run(["show", clean["id"]])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("LEDGER NEWER", out)


def _run_dispatch(args):
    return td.run(dispatches.cmd_dispatch, args)


class SeqCollisionsSurfaceTest(_Base):
    """THE DOCTOR NAMES EVERY EVENT THE FOLD DROPPED FOR A REUSED SEQ ON A
    LIVE ROW, AND COUNTS THE REST AS HISTORY — the incident's residue,
    planted: a stale writer's verdict at the seq an event it could not see
    already holds. A row that has ended (closed, retired or superseded) owes
    nothing further, so its dropped event is counted, and the listing verb
    the doctor names prints every one."""

    def _stale_verdict(self, rid, seq):
        """The event a helm older than the row's newest kind appends: its own
        fold's seq plus one, which is the seq that newest event holds."""
        row = self.state(rid)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": seq, "id": rid,
            "ts": dispatches.pk.now_ts(), "reviewed_tip": row["tip"],
            "verdict_ref": "a stale writer's FIX", "polarity": "fix"}))

    def test_a_reused_seq_is_named_and_said_to_be_dropped(self):
        row, bystander = self.row(), self.row()
        dispatches._mark_delivered(row["id"], "post-a")       # takes seq 1
        self._stale_verdict(row["id"], 1)                     # reuses it
        # NOT A COLLISION: out of order, but no applied event holds seq 9.
        self._stale_verdict(bystander["id"], 9)
        found, err = dispatches.seq_collisions()
        self.assertIsNone(err, err)
        self.assertEqual([(c["id"], c["seq"], c["event"], c["reused"])
                          for c in found],
                         [(row["id"], 1, "verdict", "delivered")])
        self.assertEqual(self.state(row["id"])["status"], "open",
                         "the reused-seq verdict was TAKEN, so it was not "
                         "dropped and nothing here is a collision")
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN)
        self.assertIn(row["id"][:12], message)
        self.assertIn("verdict seq 1, reusing delivered's", message)
        self.assertIn("DROPPED", message)
        self.assertNotIn(bystander["id"][:12], message)
        # A WRITER ON THAT ROW IS UNAFFECTED: the dropped event holds no seq
        # the fold advanced to, so the next write continues from the applied
        # one and is taken.
        _held, err = dispatches.mark_hold(row["id"], "after a collision")
        self.assertIsNone(err, err)
        self.assertEqual(self.state(row["id"])["seq"], 2)
        self.assertEqual(self.state(row["id"])["status"], "held")
        # A HELD ROW IS PAUSED, NOT ENDED: its dropped verdict still WARNs.
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN, message)
        self.assertIn(row["id"][:12], message)

    def test_a_ledger_without_one_is_OK(self):  # noqa: VACUOUS_ASSERTION — the same call on the same row turns WARN after one reused seq, and then carries the history clause once that row ends, unconditionally, below the OK
        row = self.row()
        dispatches._mark_delivered(row["id"], "post-a")
        self._stale_verdict(row["id"], 9)          # declined, not a reuse
        # AN UNKNOWN KIND IS NOT A COLLISION: it sits at the next seq, which
        # no applied event holds. The writers refuse that row; this row does
        # not count it.
        self.plant(self.row()["id"])
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.OK, message)
        self.assertNotIn("historical", message)
        # THE CONTROL ON THE SAME ROW AND THE SAME CALL: one reused seq turns
        # it, so the OK above is the census answering and not a row that
        # cannot speak...
        self._stale_verdict(row["id"], 1)
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN, message)
        # ...and once that row ends, the same collision is the history clause,
        # so the absence of that clause above is not a line that cannot print
        # it.
        _row, err = dispatches.mark_cancel(row["id"], "the work is moot")
        self.assertIsNone(err, err)
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.OK, message)
        self.assertIn("1 historical dropped event on rows that have ended",
                      message)

    def _collide(self, rid):
        """One applied event on the row, then a stale verdict reusing its
        seq."""
        dispatches._mark_delivered(rid, "post-a")
        self._stale_verdict(rid, self.state(rid)["seq"])

    def _ended_pair(self):
        """A dropped verdict on a CLOSED row and one on a SUPERSEDED row. The
        superseded row stays status open: only its successor ends it."""
        closed, parent = self.row(), self.row()
        self._collide(closed["id"])
        self._collide(parent["id"])
        _row, err = dispatches.mark_cancel(closed["id"], "the work is moot")
        self.assertIsNone(err, err)
        child = self.row(supersedes=parent["id"])
        self.assertEqual(self.state(closed["id"])["status"], "cancelled")
        self.assertEqual(self.state(parent["id"])["status"], "open",
                         "the parent was closed, so this is not the "
                         "superseded arm")
        return closed, parent, child

    def test_a_collision_on_an_ended_row_is_history_and_never_warns(self):
        closed, parent, child = self._ended_pair()
        # THE HELPER STILL RETURNS EVERY ONE, each with its row's ended word.
        found, err = dispatches.seq_collisions()
        self.assertIsNone(err, err)
        self.assertEqual({c["id"]: (c["ended"], c["carried_by"])
                          for c in found},
                         {closed["id"]: ("cancelled", None),
                          parent["id"]: ("superseded", child["id"])})
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.OK, message)
        self.assertIn("2 historical dropped events on rows that have ended",
                      message)
        self.assertIn("`helm dispatch collisions`", message)
        # THE CONTROL, SAME CALL: a collision on a LIVE row WARNs and names
        # only that row, and the history is still counted beside it — so the
        # two ended rows are absent below because they are history, not
        # because this line names nobody.
        live = self.row()
        self._collide(live["id"])
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN, message)
        self.assertIn("1 event on 1 live row", message)
        self.assertIn("%s (verdict seq 1, reusing delivered's)"
                      % live["id"][:12], message)
        self.assertIn("plus 2 historical dropped events", message)
        self.assertNotIn(closed["id"][:12], message)
        self.assertNotIn(parent["id"][:12], message)

    def test_a_collision_on_a_row_this_helm_cannot_read_stays_live(self):  # noqa: VACUOUS_ASSERTION — the same row is shown to read as history first, unconditionally, before the kind this helm cannot read is planted on it
        """A ROW THIS BINARY CANNOT READ IN FULL HAS NO ENDED WORD IT CAN
        SAY. Its `cancelled` here is a reading that stopped at an event it
        has no arm for, and that event may be the one that reopened the row;
        so its dropped event is LIVE, in the doctor's line and the listing."""
        row = self.row()
        self._collide(row["id"])
        _row, err = dispatches.mark_cancel(row["id"], "the work is moot")
        self.assertIsNone(err, err)
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.OK, "the control: a cancelled row "
                         "this helm reads in full is history — " + message)
        self.plant(row["id"])
        self.assertEqual(self.state(row["id"])["status"], "cancelled",
                         "this binary's reading of the row must still say "
                         "cancelled, or the arm measures a live status")
        found, err = dispatches.seq_collisions()
        self.assertIsNone(err, err)
        self.assertEqual([(c["id"], c["ended"], c["carried_by"])
                          for c in found], [(row["id"], None, None)])
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN, message)
        self.assertIn("%s (verdict seq 1, reusing delivered's)"
                      % row["id"][:12], message)
        self.assertNotIn("historical", message)
        rc, out, err = _run_dispatch(["collisions"])
        self.assertEqual(rc, 0, err)
        self.assertEqual([line.split()[1] for line in out.splitlines()
                          if line.startswith("LIVE ")], [row["id"]], out)
        self.assertEqual([line for line in out.splitlines()
                          if line.startswith("HISTORY ")], [], out)

    def test_the_history_count_is_what_the_listing_prints(self):
        closed, parent, _child = self._ended_pair()
        live = self.row()
        self._collide(live["id"])
        (level, message), = doctor.check_dispatch_seq_collisions()
        self.assertEqual(level, doctor.WARN, message)
        self.assertIn("1 event on 1 live row", message)
        count = re.search(r"(\d+) historical dropped event", message)
        self.assertIsNotNone(count, message)
        # THE VERB THE DOCTOR NAMES, run as the doctor names it.
        verb = re.search(r"`helm dispatch ([a-z-]+)`", message)
        self.assertIsNotNone(verb, message)
        rc, out, err = _run_dispatch([verb.group(1)])
        self.assertEqual(rc, 0, err)
        history = [line for line in out.splitlines()
                   if line.startswith("HISTORY ")]
        self.assertEqual(len(history), int(count.group(1)), out)
        self.assertEqual(len(history), 2, out)
        self.assertEqual(sorted(line.split()[1] for line in history),
                         sorted([closed["id"], parent["id"]]), out)
        self.assertEqual([line.split()[1] for line in out.splitlines()
                          if line.startswith("LIVE ")], [live["id"]], out)
        # --json IS THE SAME LIST WHOLE.
        rc, out, err = _run_dispatch([verb.group(1), "--json"])
        self.assertEqual(rc, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 3, out)
        self.assertEqual(sum(1 for r in rows if r["ended"]),
                         int(count.group(1)), out)

    def test_the_listing_refuses_an_unknown_argument(self):
        rc, out, err = _run_dispatch(["collisions", "--bogus"])
        self.assertEqual(rc, 2, out)
        self.assertIn("--bogus", err)

    def test_an_unreadable_ledger_is_UNKNOWN_never_OK(self):
        (level, message), = doctor.check_dispatch_seq_collisions(
            census=lambda: (None, "the ledger is a directory"))
        self.assertEqual(level, doctor.WARN)
        self.assertIn("UNKNOWN", message)

    def test_the_row_is_in_the_report(self):
        self.assertIn("check_dispatch_seq_collisions", doctor.CHECKS)


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
