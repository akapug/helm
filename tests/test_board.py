"""The board write path — the lost-update and torn-file cases it exists for.

COLLECTION NOTE (2026-07-30). This file was written pytest-style, with bare
`def test_*(tmp_path)` functions. The authoritative gate uses unittest
`discover`, which collects TestCase subclasses ONLY, so the direct module run
reported `Ran 0 tests` and every guard below contributed NOTHING to the receipt
that gates landing. Fifteen tests protecting the console's only safe write path
read as protection and measured nothing. Converted to TestCase; the census in
tests/test_suite_collection.py now fails if it ever recurs.
"""

import json
import multiprocessing
import os
import tempfile
import threading
import unittest
import contextlib
import io
from unittest import mock

from helm import board, eventledger, fleetnotes, pk


def _write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _adder(args):
    """Module level so multiprocessing can pickle it."""
    p, i = args
    board.add_landed("row-%d" % i, "sha-%d" % i, "n", board_path=p)


class BoardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.notes_path = os.path.join(self.tmp, "fleet-notes.json")
        self._notes_env = mock.patch.dict(
            os.environ, {"HELM_FLEET_NOTES": self.notes_path})
        self._notes_env.start()
        self.addCleanup(self._notes_env.stop)

    def seed(self, rows=None):
        p = os.path.join(self.tmp, "integration-board.json")
        _write(p, {"landed": rows if rows is not None else [], "tasks": []})
        return p

    def board(self, obj):
        p = os.path.join(self.tmp, "integration-board.json")
        _write(p, obj)
        return p

    def read(self, p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)


class AddLandedTest(BoardBase):
    def test_add_landed_prepends(self):
        p = self.seed([{"name": "old", "sha": "aaa", "note": "n"}])
        ok, err = board.add_landed("new", "bbb", "note", board_path=p)
        self.assertEqual((ok, err), (True, None))
        self.assertEqual([r["name"] for r in self.read(p)["landed"]],
                         ["new", "old"])

    def test_add_landed_is_idempotent_on_name_and_sha(self):
        """A retry after an ambiguous failure must not double-post."""
        p = self.seed()
        board.add_landed("x", "sha1", "first", board_path=p)
        board.add_landed("x", "sha1", "second", board_path=p)
        rows = self.read(p)["landed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "first")   # retry does not rewrite

    def test_same_name_different_sha_is_a_new_row(self):
        p = self.seed()
        board.add_landed("lane", "sha1", "n", board_path=p)
        board.add_landed("lane", "sha2", "n", board_path=p)
        self.assertEqual(len(self.read(p)["landed"]), 2)

    def test_a_new_landing_derives_the_homepage_headline_from_the_board_write(self):
        p = self.seed()
        ok, err = board.add_landed(
            "fleet-notes-headlines", "abcdef1234567890", "owner can act now",
            board_path=p, seat="codex-2")
        self.assertEqual((ok, err), (True, None))
        note = fleetnotes.rows()[0]
        self.assertEqual(note["key"], "board-landed")
        self.assertEqual(note["headline"],
                         "Landed fleet-notes-headlines @ abcdef123456")
        self.assertEqual(note["detail"], "owner can act now")
        self.assertEqual(note["goto"], "board")

    def test_a_duplicate_landing_does_not_refresh_the_derived_note(self):  # noqa: VACUOUS_ASSERTION — matching detail and timestamp positively prove the locked idempotent skip
        p = self.seed()
        board.add_landed("lane", "sha1", "first", board_path=p)
        before = fleetnotes.rows()[0]
        with mock.patch.object(fleetnotes.pk, "now_ts",
                               return_value="2099-01-01T00:00:00Z"):
            ok, err = board.add_landed("lane", "sha1", "retry", board_path=p)
        self.assertEqual((ok, err), (True, None))
        after = fleetnotes.rows()[0]
        self.assertEqual(after["detail"], "first")
        self.assertEqual(after["ts"], before["ts"])

    def test_concurrent_note_change_before_locked_decision_is_reconciled(self):
        p = self.seed()
        board.add_landed("lane", "sha1", "first", board_path=p)
        reached, result = threading.Event(), []
        real = fleetnotes.set_note

        def _signalled(*args, **kwargs):
            reached.set()
            return real(*args, **kwargs)

        with mock.patch.object(board.fleetnotes, "set_note", side_effect=_signalled):
            with eventledger.locked(self.notes_path) as held:
                self.assertTrue(held)
                worker = threading.Thread(
                    target=lambda: result.append(board.add_landed(
                        "lane", "sha1", "retry", board_path=p)))
                worker.start()
                self.assertTrue(reached.wait(5), "projection did not reach notes lock")
                planted = pk.read_json(self.notes_path, default={})
                planted["board-landed"]["text"] = "concurrent unrelated note"
                planted["board-landed"]["headline"] = "concurrent unrelated note"
                pk.write_json(self.notes_path, planted)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [(True, None)])
        self.assertEqual(fleetnotes.rows()[0]["headline"], "Landed lane @ sha1")

    def test_projection_failure_is_a_loud_warning_after_the_board_commits(self):
        p = self.seed()
        with mock.patch.object(board.fleetnotes, "set_note",
                               return_value=(None, "notes locked")):
            ok, warning = board.add_landed("lane", "sha1", "n", board_path=p)
        self.assertTrue(ok, "the authoritative board write already committed")
        self.assertIn("fleet-note headline failed", warning)
        self.assertEqual(self.read(p)["landed"][0]["name"], "lane")
        ok, err = board.add_landed("lane", "sha1", "n", board_path=p)
        self.assertEqual((ok, err), (True, None),
                         "an idempotent retry must repair the missing projection")
        self.assertEqual(fleetnotes.rows()[0]["headline"], "Landed lane @ sha1")

    def test_derived_overlong_headline_returns_the_owner_surface_warning(self):
        p = self.seed()
        ok, warning = board.add_landed("lane-" + "x" * 90, "sha", "n",
                                       board_path=p)
        self.assertTrue(ok)
        self.assertIn("owner-facing headlines", warning)

    def test_unexpected_projection_raise_cannot_lie_about_committed_board(self):
        p = self.seed()
        with mock.patch.object(board.fleetnotes, "read",
                               side_effect=RuntimeError("renderer vanished")):
            ok, warning = board.add_landed("lane", "sha", "n", board_path=p)
        self.assertTrue(ok)
        self.assertIn("board committed", warning)
        self.assertIn("RuntimeError", warning)
        self.assertEqual(self.read(p)["landed"][0]["name"], "lane")


class RefusalTest(BoardBase):
    def test_refuses_when_board_is_missing(self):  # noqa: VACUOUS_ASSERTION — the named missing-path refusal positively proves why neither file exists
        """Refusal is the point: an absent board must not be CREATED from one
        agent's partial edit, which would silently drop everyone else's rows."""
        p = os.path.join(self.tmp, "nope.json")
        ok, err = board.add_landed("x", "y", "z", board_path=p)
        self.assertFalse(ok)
        self.assertIn("missing", err)
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(self.notes_path),
                         "a refused source write cannot emit a derived headline")

    def test_refuses_when_board_is_not_an_object(self):
        p = self.board(["a", "list"])
        ok, _err = board.add_landed("x", "y", "z", board_path=p)
        self.assertFalse(ok)
        self.assertEqual(self.read(p), ["a", "list"])   # untouched

    def test_a_raising_mutate_leaves_the_board_untouched(self):
        p = self.seed([{"name": "keep", "sha": "s", "note": "n"}])

        def _boom(b):
            b["landed"] = []
            raise ValueError("half-applied")

        with self.assertRaises(ValueError):
            board.update(_boom, board_path=p)
        self.assertEqual([r["name"] for r in self.read(p)["landed"]], ["keep"])


class ConcurrencyTest(BoardBase):
    def test_concurrent_writers_lose_nothing(self):  # noqa: VACUOUS_ASSERTION — exact twelve-name census and matching newest headline are unconditional positive controls
        """THE defect this module exists for. Twelve processes each prepend one
        row; an unlocked read-modify-write drops most of them (last writer wins
        on a board it read before the others wrote)."""
        p = self.seed()
        # FORK, NAMED, NOT THE INTERPRETER'S DEFAULT. On CPython 3.14 the
        # default is forkserver, which binds a listener socket under this
        # process's TMPDIR: an ambient TMPDIR of 48 to 51 bytes put it past
        # sun_path (task/2539, the band arms in tests/test_socket_paths.py),
        # and tests/__init__.py now refuses a socket under the suite root at
        # any length. Fork makes no socket, its workers inherit this test's
        # patched HELM_FLEET_NOTES rather than the environment a forkserver
        # started with, and it is the start method CI's 3.9 to 3.13 columns
        # always used.
        with multiprocessing.get_context("fork").Pool(6) as pool:
            pool.map(_adder, [(p, i) for i in range(12)])
        landed = self.read(p)["landed"]
        names = {r["name"] for r in landed}
        self.assertEqual(names, {"row-%d" % i for i in range(12)})
        note = fleetnotes.rows()[0]
        # ASSERT THE WHOLE STRING. The headline gained board depth (#234), and
        # the tempting repair was to loosen this to startswith + assertIn so it
        # would tolerate the addition. That is a false positive twice over:
        # "Landed row-1 @ sha-1" is a PREFIX of "...sha-10", so the test accepts
        # the WRONG ROW, and "12 on the board" is a SUBSTRING of "112 on the
        # board". Widening an assertion to accommodate an addition is how it
        # stops asserting. The expected headline is fully derivable, so there is
        # no reason to match loosely — derive the new expected value instead.
        self.assertEqual(
            note["headline"],
            "Landed %s @ %s — %d on the board" % (
                landed[0]["name"], landed[0]["sha"], len(landed)),
            "the projection must name the authoritative newest board row and "
            "count every row the board holds")

    def test_write_is_atomic_no_tmp_left_behind(self):
        """A torn file is the other half: pk.atomic_write renames into place, so
        a reader never sees a truncated board and no .tmp survives."""
        p = self.seed()
        board.add_landed("x", "y", "z", board_path=p)
        self.assertEqual([f for f in os.listdir(self.tmp)
                          if f.endswith(".tmp")], [])
        self.assertEqual(self.read(p)["landed"][0]["name"], "x")

    def test_env_override_selects_the_board(self):
        p = self.seed()
        prior = os.environ.get("HELM_BOARD")
        os.environ["HELM_BOARD"] = p
        try:
            self.assertEqual(board.path(), p)
            ok, _ = board.add_landed("via-env", "s", "n")
            self.assertTrue(ok)
            self.assertEqual(self.read(p)["landed"][0]["name"], "via-env")
        finally:
            if prior is None:
                os.environ.pop("HELM_BOARD", None)
            else:
                os.environ["HELM_BOARD"] = prior


class SetValueTest(BoardBase):
    """`set`: the guarded scalar write. Added 2026-07-29 after the integrator
    hit the gap live — `helm board` could only append LANDED rows, so recording
    an owner-facing item meant reaching past the verb into board.update() from
    Python: the safe write path existing with only half of it usable, which is
    the same shape as the bug that got this module wired in the first place."""

    def test_set_updates_an_existing_scalar(self):
        p = self.board({"milestone": "0.2"})
        ok, err = board.set_value("milestone", "0.3", board_path=p)
        self.assertTrue(ok, err)
        self.assertEqual(self.read(p)["milestone"], "0.3")

    def test_set_REFUSES_a_new_key_and_names_what_exists(self):
        """helm does NOT own the console that renders this file, so the rendered
        key set is unknowable from here and an invented key writes a row that
        reads as recorded and is invisible to the person it was recorded for.
        The negative IS knowable: a key the board never carried is not
        rendered."""
        p = self.board({"milestone": "0.2"})
        ok, err = board.set_value("invented", "x", board_path=p)
        self.assertFalse(ok)
        self.assertIn("no key 'invented'", err)
        self.assertIn("milestone", err, "the refusal must name what DOES exist")

    def test_set_allows_a_new_key_with_explicit_intent(self):
        p = self.board({"milestone": "0.2"})
        ok, err = board.set_value("fresh", "v", allow_new=True, board_path=p)
        self.assertTrue(ok, err)
        self.assertEqual(self.read(p)["fresh"], "v")

    def test_set_REFUSES_to_clobber_a_list_with_a_scalar(self):
        """`landed` is an audit trail. A scalar set would replace it wholesale,
        atomically, under a lock — the most convincing way to destroy it."""
        p = self.board({"landed": [{"name": "a"}, {"name": "b"}]})
        ok, err = board.set_value("landed", "done", board_path=p)
        self.assertFalse(ok)
        self.assertIn("list of 2 entries", err)
        self.assertEqual(len(self.read(p)["landed"]), 2, "the list must survive")

    def test_set_REFUSES_to_clobber_a_dict_too(self):
        p = self.board({"map": {"k": "v"}})
        ok, _err = board.set_value("map", "flat", board_path=p)
        self.assertFalse(ok)
        self.assertEqual(self.read(p)["map"], {"k": "v"})

    def test_a_refused_set_leaves_the_board_BYTE_IDENTICAL(self):
        """update() only writes after mutate returns, so a raising mutate must
        leave the file untouched — not merely logically unchanged."""
        p = self.board({"landed": [{"name": "a"}], "milestone": "0.2"})
        with open(p, "rb") as f:
            before = f.read()
        board.set_value("landed", "x", board_path=p)
        board.set_value("nope", "x", board_path=p)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), before)


class AddNoteTest(BoardBase):
    """`note`: the running-log write. The board carries several newest-first
    logs the console renders — burn_down_0_2, notes, rescue_queue — and nothing
    could write any of them: `set` refuses a list by design and `add_landed`
    understands one key's dict shape. The 0.2 burn-down sat FIVE DAYS stale
    while every item inside it moved, which is the very class it exists to
    report."""

    def test_note_prepends_to_an_existing_log(self):
        p = self.board({"burn_down_0_2": ["older"]})
        ok, err = board.add_note("burn_down_0_2", "newest", board_path=p)
        self.assertTrue(ok, err)
        self.assertEqual(self.read(p)["burn_down_0_2"], ["newest", "older"])

    def test_note_starts_an_empty_log(self):
        """An empty list is the ONE case where row shape cannot be checked, and
        it is allowed: no existing row can be contradicted, and the key had to
        exist already, which means it was coordinated with the renderer."""
        p = self.board({"notes": []})
        ok, err = board.add_note("notes", "first", board_path=p)
        self.assertTrue(ok, err)
        self.assertEqual(self.read(p)["notes"], ["first"])

    def test_note_REFUSES_a_new_key(self):
        p = self.board({"notes": []})
        ok, err = board.add_note("invented", "x", board_path=p)
        self.assertFalse(ok)
        self.assertIn("no key 'invented'", err)
        self.assertNotIn("invented", self.read(p))

    def test_note_REFUSES_a_scalar_key(self):
        p = self.board({"milestone": "0.2"})
        ok, err = board.add_note("milestone", "x", board_path=p)
        self.assertFalse(ok)
        self.assertIn("not a list", err)
        self.assertEqual(self.read(p)["milestone"], "0.2")

    def test_note_REFUSES_a_list_of_dicts_and_names_the_verb_that_fits(self):
        """`landed` holds dicts. A bare string prepended to it is a row the
        console reads with .get() — it silently drops, or it raises."""
        p = self.board({"landed": [{"name": "a", "sha": "s", "note": "n"}]})
        ok, err = board.add_note("landed", "a bare string", board_path=p)
        self.assertFalse(ok)
        self.assertIn("not of strings", err)
        self.assertIn("landed", err)
        self.assertEqual(self.read(p)["landed"],
                         [{"name": "a", "sha": "s", "note": "n"}])

    def test_note_is_idempotent_against_the_row_it_just_wrote(self):
        """A retry after an ambiguous failure must not double-post."""
        p = self.board({"notes": ["old"]})
        board.add_note("notes", "same", board_path=p)
        board.add_note("notes", "same", board_path=p)
        self.assertEqual(self.read(p)["notes"], ["same", "old"])

    def test_note_ALLOWS_an_older_row_to_recur(self):
        """Deliberately not idempotent against older rows: this is a running
        log, and a status that legitimately recurs later must be able to say
        so."""
        p = self.board({"notes": ["fleet green"]})
        board.add_note("notes", "fleet red", board_path=p)
        board.add_note("notes", "fleet green", board_path=p)
        self.assertEqual(self.read(p)["notes"],
                         ["fleet green", "fleet red", "fleet green"])

    def test_a_refused_note_leaves_the_board_BYTE_IDENTICAL(self):
        p = self.board({"landed": [{"name": "a"}], "milestone": "0.2"})
        with open(p, "rb") as f:
            before = f.read()
        board.add_note("landed", "x", board_path=p)
        board.add_note("milestone", "x", board_path=p)
        board.add_note("invented", "x", board_path=p)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()


class KeyOwnershipTest(BoardBase):
    """Writer-per-KEY, enforced — the meld's outcome made mechanical.

    The rule is NOT a list of key names, it is APPEND vs REPLACE
    (the meld's sharpening): two seats appending two findings is two
    findings, and no contradiction is constructible; two seats replacing one
    narrative slot is a contradiction by construction. So a key added later
    defaults to open-for-annotation and closed-for-replacement, which is the
    safe direction — the contract this replaces defaulted to a rule nobody
    could enforce, and it went stale for days without anyone noticing."""

    def owned(self, extra=None):
        obj = {"_writer_seat": "opus-integrator", "landed": [], "tasks": [],
               "defects": [{"id": "a"}], "notes": ["one"]}
        obj.update(extra or {})
        return self.board(obj)

    def test_a_NON_OWNER_may_APPEND_to_a_narrative_key(self):
        p = self.owned({"landed": [{"name": "x", "sha": "1", "note": "n"}]})
        ok, err = board.add_landed("y", "2", "n2", board_path=p,
                                   seat="helm-claude")
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(len(self.read(p)["landed"]), 2,
                         "an append by a non-owner must land")

    def test_a_NON_OWNER_may_not_REPLACE_a_narrative_key(self):
        p = self.owned({"tasks": ["a", "b"]})
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["wiped"]),
                               board_path=p, seat="helm-claude")
        self.assertIs(ok, False)
        self.assertIn("NARRATIVE", err)
        self.assertIn("opus-integrator", err)
        self.assertEqual(self.read(p)["tasks"], ["a", "b"],
                         "the refusal must leave the board UNTOUCHED")

    def test_the_OWNER_may_replace_its_own_narrative_key(self):
        p = self.owned({"tasks": ["a"]})
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["rewritten"]),
                               board_path=p, seat="opus-integrator")
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(self.read(p)["tasks"], ["rewritten"])

    def test_a_NON_narrative_key_is_open_to_replacement(self):
        """defects is a FINDING key: two seats filing two defects is two
        defects, and a finding must never queue behind the busiest seat."""
        p = self.owned()
        ok, err = board.update(lambda b: b.__setitem__("defects", [{"id": "z"}]),
                               board_path=p, seat="helm-claude")
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(self.read(p)["defects"], [{"id": "z"}])

    def test_an_UNDECLARED_caller_fails_OPEN(self):
        """Same law as seats.foreign_seat: a process that declares no seat is
        not provably foreign. The owner's own CLI and operator scripts keep
        working, and this catches the honest mistake rather than pretending to
        stop a determined bypass."""
        p = self.owned({"tasks": ["a"]})
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["by hand"]),
                               board_path=p)          # no seat declared
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(self.read(p)["tasks"], ["by hand"])

    def test_a_board_with_NO_declared_owner_is_not_enforced(self):
        p = self.board({"tasks": ["a"], "landed": []})   # no _writer_seat
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["free"]),
                               board_path=p, seat="helm-claude")
        self.assertEqual((ok, err), (True, None))

    def test_a_TRUNCATION_is_a_replace_not_an_append(self):
        """The dangerous shape: dropping rows while keeping the rest looks like
        an edit and destroys another writer's work."""
        p = self.owned({"tasks": ["a", "b", "c"]})
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["a"]),
                               board_path=p, seat="helm-claude")
        self.assertIs(ok, False)
        self.assertIn("NARRATIVE", err)

    def test_a_REORDER_is_a_replace_too(self):
        p = self.owned({"tasks": ["a", "b"]})
        ok, err = board.update(lambda b: b.__setitem__("tasks", ["b", "a"]),
                               board_path=p, seat="helm-claude")
        self.assertIs(ok, False)

    def test_appending_at_EITHER_end_counts_as_an_append(self):
        for new in (["z", "a", "b"], ["a", "b", "z"]):
            p = self.owned({"tasks": ["a", "b"]})
            ok, err = board.update(lambda b, n=new: b.__setitem__("tasks", n),
                                   board_path=p, seat="helm-claude")
            self.assertEqual((ok, err), (True, None), new)


class NarrativeMapAuditTest(unittest.TestCase):
    """The map itself, pinned — because a key allowlist is EXACTLY the artifact
    that went stale the first time.

    The previous contract was a sentence naming one writer, enforced by nobody,
    obsolete the moment the lock landed. These assertions make the successor
    fail loudly rather than quietly: every narrative key must carry a REASON
    (so nobody adds one by reflex), and the owner key must itself be narrative
    (or a non-owner could reassign ownership and then replace anything)."""

    def test_every_narrative_key_carries_a_reason(self):
        for key, why in board.NARRATIVE.items():
            self.assertGreater(len(why), 20,
                               "%r is gated with no reason a reader can weigh "
                               "— an unexplained rule is the one that rots" % key)

    def test_the_OWNER_KEY_is_itself_narrative(self):
        """Otherwise a non-owner reassigns _writer_seat to itself and then
        replaces every other narrative key legally — the enforcement would be
        one write deep."""
        self.assertIn(board._OWNER_KEY, board.NARRATIVE)

    def test_the_append_predicate_SEES_a_replace(self):
        """The control: a predicate that answered True unconditionally would
        make every refusal above vacuous."""
        self.assertTrue(board._appended_only(["a"], ["z", "a"]))
        self.assertFalse(board._appended_only(["a", "b"], ["a"]))
        self.assertFalse(board._appended_only(["a", "b"], ["b", "a"]))
        self.assertFalse(board._appended_only("scalar", "other"))


def _run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(args))
    return rc, out.getvalue(), err.getvalue()


class CLIDeclaresItsSeatTest(BoardBase):
    """A guard the PRIMARY ENTRY POINT never invokes does not exist.

    The first cut enforced ownership inside update() and then had the CLI call
    set_value with no seat, so `helm board set` from any seat failed open and
    the whole enforcement was decorative — the same shape as a tri-state
    nobody prints, which is the defect I spent tonight fixing one layer up."""

    def owned(self, extra=None):
        # `freeze` is narrative AND scalar — `tasks` is a list, and the
        # pre-existing structure guard would refuse a scalar set before
        # ownership was ever consulted, which would test the wrong refusal.
        obj = {"_writer_seat": "opus-integrator", "freeze": "open",
               "tasks": ["a"], "landed": []}
        obj.update(extra or {})
        return self.board(obj)

    def test_a_seat_replacing_a_narrative_key_is_REFUSED_at_the_CLI(self):
        p = self.owned()
        with mock.patch.dict(os.environ, {"HELM_BOARD": p,
                                          "HELM_CHAT_NAME": "helm-claude"}):
            rc, _out, err = _run(board.cmd_board, ["set", "freeze", "wiped"])
        self.assertEqual(rc, 1, "a seat replacing a narrative key must refuse")
        self.assertIn("NARRATIVE", err)
        self.assertEqual(self.read(p)["freeze"], "open",
                         "and the board is untouched")

    def test_an_UNDECLARED_operator_shell_still_writes(self):
        """The owner's own shell declares no seat and must keep working — the
        intended asymmetry, not an oversight."""
        p = self.owned()
        env = {k: v for k, v in os.environ.items() if k != "HELM_CHAT_NAME"}
        env["HELM_BOARD"] = p
        with mock.patch.dict(os.environ, env, clear=True):
            rc, _out, err = _run(board.cmd_board, ["set", "freeze", "by hand"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read(p)["freeze"], "by hand")
