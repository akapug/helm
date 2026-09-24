#!/usr/bin/env python3
"""A seat is renamed — what must travel with the name, and for how long?

Renaming a live agent is not a string edit, and these arms are the three
answers that cost the most to learn. The verb itself binds a running agent to
a memorable @name. The ALIAS half keeps the old name reachable for a window,
because the retained session still carries the old `HELM_CHAT_NAME` and was
otherwise refused as a takeover at join, post and beacon-arm while @old
mentions were written ABSENT. The SPAWN-REGISTER half is the rename that came
back 300 seconds later: a durable rename must write the register's `identity`
field, and nothing ever wrote it, so the rebind timer minted a second roster
row under the pre-rename name and destroyed the very alias covering the
window. Every arm carries its negative — no alias, an expired one, `--dry-run`
leaving the roster and chat dir byte-identical.

MOVED WHOLE OUT OF `tests/test_seats.py`, which stood 56,594 bytes under the
1 MiB never-track ceiling with arms still landing in it. No body was rewritten
on the way; each class is the byte-identical text it had there.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `SeatsBase` and
`THREAD_TIMEOUT` are imported from the module these arms came from, so one
fixture and one timeout serve both files and the two cannot drift.
"""
import contextlib
import io
import os
import shutil
import threading
import time
from unittest import mock

from tests.test_seats import SeatsBase, THREAD_TIMEOUT

from helm import (actors, beacons, chat, home, pathenv, pk, seats, seats_cursor,
                  seats_delivery, seats_join, seats_receipts, seats_rename, seats_report,
                  seats_stop_claims, seats_stop_seam, seats_stop_signals, web)
from helm import seat as seatmod
from helm import seat_lifecycle_runtime as seat_runtime
from helm import seats_roster as seatmod_roster

class RenameTest(SeatsBase):
    """helm chat seat rename — bind a live agent to a memorable @name."""

    def test_rename_rebinds_delivery_and_keeps_tracked_ground(self):
        seats.join(session="s-r1", seat="agent-3f2a", cwd="/tmp/p")
        chat.post("noise", who="bob")
        self.assertIsNone(seats.deliver(session="s-r1"))
        off = seats._cursor("main", "agent-3f2a", "s-r1")["off"]
        ok, msg = seats.rename_seat("agent-3f2a", "art3mis")
        self.assertTrue(ok, msg)
        self.assertIn("re-arm", msg)                       # beacon note
        self.assertNotIn("agent-3f2a", seats.roster())
        self.assertIn("art3mis", seats.roster())
        # the cursor moved WITH the seat — no EOF re-baseline, no loss
        self.assertEqual(seats._cursor("main", "art3mis", "s-r1")["off"], off)
        chat.post("@art3mis go", who="bob")
        self.assertIn("go", seats.deliver(session="s-r1"))  # hook path rebound
        self.assertIsNone(seats.deliver(session="s-r1"))

    def test_rename_advances_actor_revision_through_the_public_seat_path(self):
        seats.join(session="s-rev1", seat="agent-rev", cwd=self.tmp)
        before, err = actors.resolve_actor("s-rev1", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(before.version, 1)
        ok, note = seats.rename_seat("agent-rev", "agent-next")
        self.assertTrue(ok, note)
        after, err = actors.resolve_actor("s-rev1", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(after.actor_id, before.actor_id)
        self.assertEqual(after.version, 2)
        self.assertEqual(before.version, 1)
        self.assertEqual(actors.lookup("agent-rev")["actor_id"], before.actor_id)
        self.assertIn("agent-rev", after.aliases)

    def test_starting_rename_admits_explicit_default_chat_exact(self):  # noqa: VACUOUS_ASSERTION — apply unconditionally moves the roster and Actor after dry-run proves preflight is also admitted
        shared_home = os.path.join(self.tmp, "same-chat-home")
        shared_chat = os.path.join(self.tmp, "same-chat")
        os.makedirs(shared_chat)
        env = dict(os.environ)
        for key in pathenv.IDENTITY_PATH_ENV_KEYS:
            env.pop(key, None)
        env.update({"HELM_HOME": shared_home, "HELM_CHAT_DIR": shared_chat})
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(home, "default_home",
                                  return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", shared_chat):
            actor, err = actors.bind("actor-old-exact")
            self.assertIsNone(err, err)
            seats.join(session="session-exact", seat="actor-old-exact",
                       cwd=self.tmp)
            roster = seats.roster_path()
            with open(roster, "rb") as fh:
                before = fh.read()
            ok, note = seats.rename_seat(
                "actor-old-exact", "actor-new-exact", dry_run=True)
            self.assertTrue(ok, note)
            with open(roster, "rb") as fh:
                self.assertEqual(fh.read(), before,
                                 "admitted dry-run changed the roster")
            ok, note = seats.rename_seat("actor-old-exact", "actor-new-exact")
            self.assertTrue(ok, note)
            self.assertNotIn("actor-old-exact", seats.roster())
            self.assertIn("actor-new-exact", seats.roster())
            self.assertEqual(actors.lookup("actor-new-exact")["actor_id"],
                             actor["actor_id"])

    def test_actor_rename_start_admits_explicit_default_chat_symlink(self):
        shared_home = os.path.join(self.tmp, "linked-chat-home")
        shared_chat = os.path.join(self.tmp, "linked-chat")
        linked_chat = os.path.join(self.tmp, "linked-chat-alias")
        os.makedirs(shared_chat)
        os.symlink(shared_chat, linked_chat)
        env = dict(os.environ)
        for key in pathenv.IDENTITY_PATH_ENV_KEYS:
            env.pop(key, None)
        env["HELM_HOME"] = shared_home
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(home, "default_home",
                                  return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", shared_chat):
            actor, err = actors.bind("actor-old-linked")
            self.assertIsNone(err, err)
            chat_key = "MELD_CHAT_DIR"
            os.environ[chat_key] = linked_chat  # outer patch restores the key
            ok, note, reason = actors.rename_preflight(
                "actor-old-linked", "actor-new-linked")
            self.assertTrue(ok, note)
            self.assertEqual(reason, actors.RELABEL)
            with actors._store_lock():
                ok, note, reason, expected = actors.rename_expectation(
                    "actor-old-linked", "actor-new-linked")
            self.assertTrue(ok, note)
            self.assertEqual(reason, actors.RELABEL)
            self.assertEqual(expected["actor_id"], actor["actor_id"])

    def test_starting_rename_refuses_split_chat_and_shared_actor_store(self):  # noqa: VACUOUS_ASSERTION — the redirected retry unconditionally completes the same public rename, moves the roster, and preserves the Actor id
        shared_home = os.path.join(self.tmp, "shared-home")
        default_chat = os.path.join(self.tmp, "default-chat")
        explicit_chat = os.path.join(self.tmp, "explicit-chat")
        env = dict(os.environ)
        for key in pathenv.IDENTITY_PATH_ENV_KEYS:
            env.pop(key, None)
        env["HELM_HOME"] = shared_home
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(home, "default_home",
                                  return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", default_chat):
            actor, err = actors.bind("actor-old")
            self.assertIsNone(err, err)
            self.assertEqual(actor["canonical_name"], "actor-old")
            store = actors.store_path()
            with open(store, "rb") as fh:
                before_store = fh.read()

            chat_key = "HELM_CHAT_DIR"
            os.environ[chat_key] = explicit_chat  # outer patch restores the key
            seats.join(session="s-split", seat="actor-old", cwd=self.tmp)
            roster = seats.roster_path()
            with open(roster, "rb") as fh:
                before_roster = fh.read()
            ok, note = seats.rename_seat("actor-old", "actor-new")
            self.assertFalse(ok, note)
            self.assertIn("split identity configuration", note)
            self.assertIn(os.path.abspath(explicit_chat), note)
            self.assertIn(os.path.abspath(store), note)
            self.assertIn("HELM_ACTORS", note)
            with open(store, "rb") as fh:
                self.assertEqual(fh.read(), before_store,
                                 "refused rename changed Actor authority")
            with open(roster, "rb") as fh:
                self.assertEqual(fh.read(), before_roster,
                                 "refused rename changed the explicit roster")
            self.assertIn("actor-old", seats.roster())
            self.assertNotIn("actor-new", seats.roster())
            self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

            redirected = os.path.join(self.tmp, "isolated", "actors.json")
            os.makedirs(os.path.dirname(redirected))
            shutil.copyfile(store, redirected)
            with mock.patch.dict(os.environ, {"HELM_ACTORS": redirected}):
                ok, note = seats.rename_seat("actor-old", "actor-new")
                self.assertTrue(ok, note)
                self.assertNotIn("actor-old", seats.roster())
                self.assertIn("actor-new", seats.roster())
                self.assertEqual(actors.lookup("actor-new")["actor_id"],
                                 actor["actor_id"])
            with open(store, "rb") as fh:
                self.assertEqual(fh.read(), before_store,
                                 "paired rename wrote the shared Actor store")

    def test_failed_actor_commit_keeps_journal_for_exact_replay(self):
        seats.join(session="s-actor-fail", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-fail", self.tmp)
        self.assertIsNone(err, err)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, note = seats.rename_seat("actor-old", "actor-new")
        self.assertFalse(ok, note)
        self.assertIn("journal was retained", note)
        self.assertIn("actor-new", seats.roster())
        journal = pk.read_json(seats_rename.rename_journal_path(), None)
        self.assertEqual(journal["actor_rename"]["actor_id"], before.actor_id)
        self.assertEqual(journal["actor_rename"]["revision"], before.version)
        self.assertEqual(journal["actor_rename"]["canonical_name"],
                         "actor-old")
        self.assertEqual(actors.lookup("actor-old")["revision"], before.version)
        self.assertIsNone(actors.lookup("actor-new"))
        with open(actors.store_path(), "rb") as fh:
            stranded = fh.read()

        source, err = actors.bind("actor-old")
        self.assertIsNone(err, err)
        self.assertEqual(source["actor_id"], before.actor_id)
        direct, err = actors.bind("actor-new")
        self.assertIsNone(direct)
        self.assertIn("pending seat rename", err)
        self.assertIn("target name", err)
        self.assertIn("`helm chat seats`", err)
        public, err = actors.resolve_actor("s-actor-fail", self.tmp)
        self.assertIsNone(public)
        self.assertIn("pending seat rename", err)
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), stranded,
                             "refused admissions mutated the Actor store")
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))

        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))
        after = actors.lookup("actor-new")
        self.assertEqual(after["actor_id"], before.actor_id)
        self.assertEqual(after["revision"], before.version + 1)
        self.assertEqual(after["canonical_name"], "actor-new")
        retry, err = actors.bind("actor-new")
        self.assertIsNone(err, err)
        self.assertEqual(retry["actor_id"], before.actor_id)
        public, err = actors.resolve_actor("s-actor-fail", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(public.actor_id, before.actor_id)
        self.assertEqual(public.version, before.version + 1)

    def test_v2_journal_without_actor_payload_is_retained(self):  # noqa: VACUOUS_ASSERTION — positive v2 journal and original actor controls precede missing-payload refusals
        seats.join(session="s-actor-truncated", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-truncated", self.tmp)
        self.assertIsNone(err, err)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, _note = seats.rename_seat("actor-old", "actor-new")
        self.assertFalse(ok)
        path = seats_rename.rename_journal_path()
        journal = pk.read_json(path, None)
        self.assertEqual(journal["v"], 2)
        journal["actor_rename"] = None
        pk.write_json(path, journal)
        self.assertFalse(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(path))
        self.assertEqual(actors.lookup("actor-old")["revision"], before.version)

        journal.pop("actor_rename")
        pk.write_json(path, journal)
        self.assertFalse(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(path))
        self.assertEqual(actors.lookup("actor-old")["revision"], before.version)
        self.assertIsNone(actors.lookup("actor-new"))

    def test_public_rename_recovers_pending_actor_leg_without_relocking(self):
        seats.join(session="s-actor-next", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-next", self.tmp)
        self.assertIsNone(err, err)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, _note = seats.rename_seat("actor-old", "actor-new")
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))

        ok, note = seats.rename_seat("actor-new", "actor-final")
        self.assertTrue(ok, note)
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))
        after = actors.lookup("actor-final")
        self.assertEqual(after["actor_id"], before.actor_id)
        self.assertEqual(after["revision"], before.version + 2)
        self.assertEqual(after["canonical_name"], "actor-final")
        self.assertEqual(actors.lookup("actor-new")["actor_id"], before.actor_id)

    def test_postcommit_actor_replay_is_an_idempotent_noop(self):  # noqa: VACUOUS_ASSERTION — committed actor and retained journal are positive controls before cleanup
        seats.join(session="s-actor-post", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-post", self.tmp)
        self.assertIsNone(err, err)
        with mock.patch.object(seats_rename, "_clear_journal",
                               side_effect=SystemExit("after actor commit")):
            with self.assertRaises(SystemExit):
                seats.rename_seat("actor-old", "actor-new")
        committed = actors.lookup("actor-new")
        self.assertEqual(committed["actor_id"], before.actor_id)
        self.assertEqual(committed["revision"], before.version + 1)
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))

        self.assertTrue(seats_rename.recover_seat_rename())
        replayed = actors.lookup("actor-new")
        self.assertEqual(replayed["revision"], committed["revision"])
        self.assertEqual(replayed["aliases"], committed["aliases"])
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))
        with open(actors.store_path(), "rb") as fh:
            settled = fh.read()
        self.assertTrue(seats_rename.recover_seat_rename())
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), settled)
        self.assertEqual(actors.lookup("actor-new")["revision"],
                         committed["revision"])

    def test_actor_replay_refuses_target_collision_without_clobbering(self):  # noqa: VACUOUS_ASSERTION — the planted out-of-band actor is the positive control for collision refusal
        seats.join(session="s-actor-conflict", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-conflict", self.tmp)
        self.assertIsNone(err, err)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, _note = seats.rename_seat("actor-old", "actor-new")
        self.assertFalse(ok)
        # The admission fence now closes the ordinary collision route. Plant
        # hostile out-of-band drift directly so this remains a recovery
        # collision test rather than becoming a duplicate fence test.
        divergent = pk.read_json(actors.store_path(), None)
        intruder_id = actors._actor_id_for("actor-new")
        intruder = {"actor_id": intruder_id, "canonical_name": "actor-new",
                    "aliases": [], "created": pk.now_ts(),
                    "v": actors.ACTOR_STORE_VERSION, "revision": 1,
                    "evidence": [["hostile-test", "out-of-band drift"]]}
        divergent["actors"][intruder_id] = intruder
        divergent["by_name"]["actor-new"] = intruder_id
        pk.write_json(actors.store_path(), divergent)
        self.assertNotEqual(intruder["actor_id"], before.actor_id)

        self.assertFalse(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))
        self.assertEqual(pk.read_json(actors.store_path(), None), divergent)
        self.assertEqual(actors.lookup("actor-new")["actor_id"],
                         intruder["actor_id"])
        self.assertIn("actor-new", seats.roster())

    def test_actor_replay_refuses_newer_revision_without_clobbering(self):
        seats.join(session="s-actor-drift", seat="actor-old", cwd=self.tmp)
        before, err = actors.resolve_actor("s-actor-drift", self.tmp)
        self.assertIsNone(err, err)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, _note = seats.rename_seat("actor-old", "actor-new")
        self.assertFalse(ok)
        ok, note = actors.rename("actor-old", "actor-third")
        self.assertTrue(ok, note)
        self.assertEqual(actors.lookup("actor-third")["revision"],
                         before.version + 1)
        divergent = pk.read_json(actors.store_path(), None)

        self.assertFalse(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))
        self.assertEqual(pk.read_json(actors.store_path(), None), divergent)
        self.assertEqual(actors.lookup("actor-third")["canonical_name"],
                         "actor-third")
        self.assertIn("actor-new", seats.roster())

    def test_resolving_new_name_mid_rename_cannot_fork_actor(self):  # noqa: VACUOUS_ASSERTION — finished.wait(0) and the committed actor ID after join prove this resolver completed and bound the same actor
        seats.join(session="s-race", seat="actor-old", cwd=self.tmp)
        prior, err = actors.bind("actor-old")
        self.assertIsNone(err, err)
        replay, acquire = actors.replay_rename, actors._store_lock
        attempted, finished, outcome = threading.Event(), threading.Event(), []
        worker = []

        @contextlib.contextmanager
        def actor_lock():
            if threading.current_thread().name == "new-name-reader":
                attempted.set()
            with acquire():
                yield

        def bind_new():
            outcome.append(actors.bind("actor-new"))
            finished.set()

        def during_replay(expected, *args, **kwargs):
            if kwargs.get("apply", True) and not worker:
                probe = threading.Thread(target=bind_new,
                                         name="new-name-reader")
                worker.append(probe)
                probe.start()
                self.assertTrue(attempted.wait(3),
                                "positive control: resolver tried to bind")
                self.assertFalse(
                    finished.wait(.2),
                    "the new roster name minted an actor before relabel")
            return replay(expected, *args, **kwargs)

        try:
            with mock.patch.object(actors, "replay_rename",
                                   side_effect=during_replay), \
                    mock.patch.object(actors, "_store_lock", side_effect=actor_lock):
                ok, note = seats.rename_seat("actor-old", "actor-new")
        finally:
            for probe in worker:
                probe.join(3)
        self.assertTrue(ok, note)
        self.assertTrue(finished.wait(0), "positive control: resolver completed")
        self.assertIsNone(outcome[0][1], outcome[0])
        self.assertEqual(outcome[0][0]["actor_id"], prior["actor_id"])
        self.assertEqual(actors.lookup("actor-old")["revision"], 2)

    def test_rename_without_prior_actor_record_stays_roster_only(self):  # noqa: VACUOUS_ASSERTION — published new roster key is the positive control for absent actor
        seats.join(session="s-roster-only", seat="roster-old", cwd=self.tmp)
        self.assertIsNone(actors.lookup("roster-old"))
        ok, note = seats.rename_seat("roster-old", "roster-new")
        self.assertTrue(ok, note)
        self.assertIn("roster-new", seats.roster())
        self.assertIsNone(actors.lookup("roster-old"))
        self.assertIsNone(actors.lookup("roster-new"))
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

    def test_rename_by_session_prefix(self):
        seats.join(session="sess-abcdef1234", seat="agent-xyz", cwd="/tmp/p")
        ok, _msg = seats.rename_seat("sess-abc", "nice")
        self.assertTrue(ok)
        self.assertEqual(seats.seat_for_session("sess-abcdef1234"), "nice")

    def test_rename_refusals(self):
        seats.join(session="s-1", seat="a", cwd="/tmp/p")
        seats.join(session="s-2", seat="b", cwd="/tmp/p")
        for bad, why in (("b", "taken"), ("daria", "reserved"),
                         ("all", "reserved"), ("sp ace", "chars"),
                         ("", "chars")):
            ok, msg = seats.rename_seat("a", bad)
            self.assertFalse(ok, "%s should refuse (%s): %s" % (bad, why, msg))
        ok, msg = seats.rename_seat("ghost", "x")
        self.assertFalse(ok)
        self.assertIn("no roster row", msg)
        ok, msg = seats.rename_seat("a", "a")          # no-op, not an error
        self.assertTrue(ok)

    def test_case_only_rename_preserves_dm_receipts(self):
        seats.join(session="s-k", seat="kimi", cwd="/tmp/p")
        before, err = actors.bind("kimi")
        self.assertIsNone(err, err)
        row, err = seats.dm("kimi", "@all private", who="owner")
        self.assertIsNone(err)
        lane = chat.DM_PREFIX + seats._seat_key("kimi")
        seats_receipts.record_delivery_receipt(
            row, lane, "kimi", "s-k", "hook")
        ok, message = seats.rename_seat("kimi", "Kimi")
        self.assertTrue(ok, message)
        self.assertEqual(len(seats_receipts.delivery_receipts(
            row["id"], lane)), 1)
        self.assertTrue(os.path.exists(chat.room_path(lane)))
        after = actors.lookup("Kimi")
        self.assertEqual(after["actor_id"], before["actor_id"])
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["canonical_name"], "Kimi")

    def test_case_only_actor_write_failure_replays_without_touching_receipts(self):
        seats.join(session="s-case-fail", seat="kimi", cwd=self.tmp)
        before, err = actors.bind("kimi")
        self.assertIsNone(err, err)
        row, err = seats.dm("kimi", "private", who="owner")
        self.assertIsNone(err, err)
        lane = chat.DM_PREFIX + seats._seat_key("kimi")
        seats_receipts.record_delivery_receipt(
            row, lane, "kimi", "s-case-fail", "hook")
        receipts = seats_receipts.delivery_receipts(row["id"], lane)
        write = actors.pk.write_json

        def fail_actor(path, value):
            if path == actors.store_path():
                raise OSError("actor disk full")
            return write(path, value)

        with mock.patch.object(actors.pk, "write_json", side_effect=fail_actor):
            ok, note = seats.rename_seat("kimi", "Kimi")
        self.assertFalse(ok, note)
        self.assertIn("Kimi", seats.roster())
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))
        self.assertEqual(seats_receipts.delivery_receipts(row["id"], lane),
                         receipts)
        self.assertEqual(actors.lookup("kimi")["revision"],
                         before["revision"])

        self.assertTrue(seats_rename.recover_seat_rename())
        after = actors.lookup("Kimi")
        self.assertEqual(after["actor_id"], before["actor_id"])
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["canonical_name"], "Kimi")
        self.assertEqual(seats_receipts.delivery_receipts(row["id"], lane),
                         receipts)
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

    def test_prepublication_crash_with_actor_rolls_back_without_revision(self):
        seats.join(session="s-before-roster", seat="actor-old", cwd=self.tmp)
        before, err = actors.bind("actor-old")
        self.assertIsNone(err, err)
        pk.atomic_write(seats.seen_path("actor-old"), "seen")
        old_seen = seats.seen_path("actor-old")
        new_seen = seats.seen_path("actor-new")
        with open(actors.store_path(), "rb") as fh:
            store_before = fh.read()
        write = pk.write_json

        def crash_before_roster(path, value):
            if path == seats.roster_path():
                raise SystemExit("before roster publication")
            return write(path, value)

        with mock.patch.object(pk, "write_json", side_effect=crash_before_roster):
            with self.assertRaises(SystemExit):
                seats.rename_seat("actor-old", "actor-new")
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))
        self.assertFalse(os.path.exists(old_seen))
        self.assertTrue(os.path.exists(new_seen))
        self.assertIn("actor-old", seats.roster())
        self.assertNotIn("actor-new", seats.roster())
        unrelated, err = actors.bind("actor-unrelated")
        self.assertIsNone(err, err)
        self.assertEqual(unrelated["canonical_name"], "actor-unrelated")
        with open(actors.store_path(), "rb") as fh:
            store_with_unrelated = fh.read()
        self.assertNotEqual(store_with_unrelated, store_before,
                            "control: unrelated first admission did not commit")

        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(old_seen))
        self.assertFalse(os.path.exists(new_seen))
        actor = actors.lookup("actor-old")
        self.assertEqual(actor["actor_id"], before["actor_id"])
        self.assertEqual(actor["revision"], before["revision"])
        self.assertEqual(actor["canonical_name"], "actor-old")
        self.assertIsNone(actors.lookup("actor-new"))
        self.assertEqual(actors.lookup("actor-unrelated")["actor_id"],
                         unrelated["actor_id"])
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), store_with_unrelated)
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

    def test_process_death_mid_rename_is_rolled_back_from_journal(self):  # noqa: VACUOUS_ASSERTION — pre-crash cursor and journal are positive controls for rollback
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        chat.post("@alice retained", who="owner")
        old = seats.cursor_path("main", "alice", "s-a")
        real_replace, moved = seats_rename.os.replace, []

        def crash(source, target):
            if ".cursor." in source:
                if moved:
                    raise SystemExit("process death")
                moved.append((source, target))
            return real_replace(source, target)

        with mock.patch("helm.seats_rename.os.replace", side_effect=crash):
            with self.assertRaises(SystemExit):
                seats.rename_seat("alice", "renamed")
        self.assertTrue(os.path.exists(seats_rename.rename_journal_path()))
        legacy = pk.read_json(seats_rename.rename_journal_path(), None)
        self.assertNotIn("actor_rename", legacy)
        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(old))
        self.assertIn("alice", seats.roster())
        self.assertNotIn("renamed", seats.roster())
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

    def test_process_death_during_source_prune_keeps_complete_staged_receipts(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        old_lane = chat.DM_PREFIX + seats._seat_key("alice")
        for channel in ("hook", "beacon"):
            seats_receipts.record_delivery_receipt(
                row, old_lane, "alice", "s-a", channel)
        self.assertEqual(len(seats_receipts.delivery_receipts(
            row["id"], old_lane)), 2)

        def crash_during_prune(room, ids):
            root = seats_receipts._receipt_path(room, row["id"])
            group = os.path.join(root, os.listdir(root)[0])
            os.remove(os.path.join(group, sorted(os.listdir(group))[0]))
            raise SystemExit("source prune crash")

        with mock.patch("helm.seats_receipts.finish_room_receipts",
                        side_effect=crash_during_prune):
            with self.assertRaises(SystemExit):
                seats.rename_seat("alice", "renamed")
        self.assertIn("renamed", seats.roster())
        self.assertTrue(seats_rename.recover_seat_rename())
        new_lane = chat.DM_PREFIX + seats._seat_key("renamed")
        receipts = seats_receipts.delivery_receipts(row["id"], new_lane)
        self.assertEqual({item["effect"] for item in receipts},
                         {"hook-delivered", "wake-attempted"})
        self.assertFalse(os.path.exists(seats_rename.rename_journal_path()))

    def test_round_trip_rename_clears_destination_redirect_cycle(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        first, err = seats.dm("alice", "first", who="owner")
        self.assertIsNone(err)
        self.assertIsNotNone(first)
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        ok, message = seats.rename_seat("renamed", "alice")
        self.assertTrue(ok, message)
        live = chat.DM_PREFIX + seats._seat_key("alice")
        retired = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertIsNone(chat._dm_redirect(live))
        self.assertEqual(chat._dm_redirect(retired), live)
        second, err = seats.dm("alice", "round trip", who="owner")
        self.assertIsNone(err)
        self.assertEqual(second["text"], "round trip")
        self.assertTrue(seats.touch_seen("alice"))
        self.assertTrue(os.path.exists(seats.seen_path("alice")))

    def test_retired_name_can_be_lawfully_reused_without_old_redirect(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        old_lane = chat.DM_PREFIX + seats._seat_key("alice")
        self.assertIsNotNone(chat._dm_redirect(old_lane))
        seats.join(session="s-b", seat="alice", cwd="/tmp/p")
        self.assertIsNone(chat._dm_redirect(old_lane))
        row, err = seats.dm("alice", "new owner", who="owner")
        self.assertIsNone(err)
        self.assertIn("new owner", seats.deliver_any(
            session="s-b", seat="alice"))
        self.assertIsNone(seats.deliver_any(
            session="s-a", seat="renamed"))
        self.assertEqual(row["dm"], "alice")

    def test_pre_redirect_crash_cannot_recreate_old_sidecar(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        old_seen = seats.seen_path("alice")
        old_lane = chat.DM_PREFIX + seats._seat_key("alice")
        real = seats_rename.pk.atomic_write

        def crash(path, value):
            if path == chat._dm_redirect_path(old_lane):
                raise SystemExit("before redirect")
            return real(path, value)

        with mock.patch("helm.seats_rename.pk.atomic_write", side_effect=crash):
            with self.assertRaises(SystemExit):
                seats.rename_seat("alice", "renamed")
        self.assertFalse(os.path.exists(old_seen))
        with mock.patch("helm.seats_rename.recover_seat_rename",
                        return_value=False):
            self.assertFalse(seats.touch_seen("alice"))
        self.assertFalse(os.path.exists(old_seen))
        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertIn("alice", seats.roster())
        self.assertNotIn("renamed", seats.roster())

    def test_rename_recovery_preserves_unrelated_roster_update(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        def crash(room, ids):
            raise SystemExit("after roster publish")

        with mock.patch("helm.seats_receipts.finish_room_receipts",
                        side_effect=crash):
            with self.assertRaises(SystemExit):
                seats.rename_seat("alice", "renamed")
        current = seats.roster()
        current["unrelated"] = {"session": "s-u", "last_seen": 1}
        pk.write_json(seats.roster_path(), current)
        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertIn("renamed", seats.roster())
        self.assertEqual(seats.roster()["unrelated"]["session"], "s-u")

    def test_direct_state_move_without_roster_images_can_still_rollback(self):
        pk.atomic_write(seats.seen_path("foo"), "seen")
        with mock.patch("helm.seats_receipts.finish_room_receipts",
                        side_effect=SystemExit("after move")):
            with self.assertRaises(SystemExit):
                seats._move_seat_state("foo", "bar")
        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertTrue(os.path.exists(seats.seen_path("foo")))
        self.assertFalse(os.path.exists(seats.seen_path("bar")))

    def test_absent_rename_recovery_is_side_effect_free(self):
        root = chat.chat_dir()
        self.assertFalse(os.path.exists(root))
        self.assertTrue(seats_rename.recover_seat_rename())
        self.assertFalse(os.path.exists(root),
                         "proven absence created recovery lock state")

    def test_roster_locked_recovery_never_reacquires_the_roster_lock(self):
        with mock.patch("helm.seats_rename._existing_roster_scope",
                        side_effect=AssertionError("self-deadlock")):
            self.assertTrue(seats_rename.recover_seat_rename(
                roster_locked=True))

    def test_absent_recovery_waits_through_roster_lock_window(self):
        from helm.seats_common import _flocked
        chat._ensure_dir()
        entered, release, attempting, acquired, done = (
            threading.Event() for _ in range(5))
        recovered, real_scope = [], seats_rename._existing_roster_scope

        def writer():
            with _flocked(seats.roster_path() + ".lock"):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("roster writer seam was never released")

        @contextlib.contextmanager
        def observed_scope():
            attempting.set()
            with real_scope():
                acquired.set()
                yield

        def recover():
            recovered.append(seats_rename.recover_seat_rename())
            done.set()

        owner = threading.Thread(target=writer)
        owner.start()
        self.assertTrue(entered.wait(1))
        with mock.patch("helm.seats_rename._existing_roster_scope",
                        observed_scope):
            reader = threading.Thread(target=recover)
            reader.start()
            self.assertTrue(attempting.wait(1))
            # A BOUNDED WAIT, NOT AN INSTANT. is_set() samples one moment, and
            # against a NON-blocking implementation attempting.set() and
            # acquired.set() are a few instructions apart — so the sample can
            # land in that window and the arm passes a broken build on
            # scheduling luck. wait() turns the instant into an interval: it
            # returns the flag, so False means the lock was still unacquired
            # for the whole window, which is the property being claimed.
            self.assertFalse(acquired.wait(0.25),
                             "recovery crossed an active roster writer")
            release.set()
            self.assertTrue(acquired.wait(1))
            reader.join(2)
        owner.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(recovered, [True])

    def test_absent_recovery_waits_through_prejournal_writer(self):
        pk.atomic_write(seats.seen_path("foo"), "seen")
        entered, release, attempting, acquired, done = (
            threading.Event() for _ in range(5))
        moved, recovered = [], []
        real_journal, real_scope = (seats_rename._journal,
                                    seats_rename._existing_roster_scope)
        first = [True]

        def paused_journal():
            if first[0]:
                first[0] = False
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("writer seam was never released")
            return real_journal()

        @contextlib.contextmanager
        def observed_scope():
            attempting.set()
            with real_scope():
                acquired.set()
                yield

        def move():
            moved.append(seats._move_seat_state("foo", "bar"))

        def recover():
            recovered.append(seats_rename.recover_seat_rename())
            done.set()

        with mock.patch("helm.seats_rename._journal", side_effect=paused_journal):
            writer = threading.Thread(target=move)
            writer.start()
            self.assertTrue(entered.wait(1))
            with mock.patch("helm.seats_rename._existing_roster_scope",
                            observed_scope):
                reader = threading.Thread(target=recover)
                reader.start()
                self.assertTrue(attempting.wait(1))
                # A direct mover now owns roster then actor before its journal
                # window. Recovery must wait at that outer boundary rather than
                # reaching the rename directory lock out of order.
                self.assertFalse(acquired.wait(0.25),
                                 "recovery crossed the pre-journal writer")
                release.set()
                self.assertTrue(acquired.wait(1))
                reader.join(2)
            writer.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual((moved, recovered), ([True], [True]))
        self.assertTrue(os.path.exists(seats.seen_path("bar")))

    def test_concurrent_completed_recovery_is_success_not_false_absence(self):
        # The probe sees work; a concurrent owner finishes before the canonical
        # roster-locked re-read. Proven absence there is successful recovery.
        with mock.patch("helm.seats_rename._rename_scope"), \
                mock.patch("helm.seats_rename._journal",
                           side_effect=({}, None)):
            self.assertTrue(seats_rename.recover_seat_rename())

    def test_rename_moves_every_stop_marker_family(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        kinds = ("stopfp", "stopbeacon", "stopwhisper", "stopclaime",
                 "stopwiring", "stoppunt", "stoplease", "stopndp",
                 "stopseam", "stopseamshare", "stopspiral")
        paths = [seats._stop_fp_path("main", "alice", "s-a", kind=kind)
                 for kind in kinds]
        for path in paths:
            pk.atomic_write(path, "latched")
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        for kind, old in zip(kinds, paths):
            new = seats._stop_fp_path("main", "renamed", "s-a", kind=kind)
            self.assertFalse(os.path.exists(old), kind)
            self.assertTrue(os.path.exists(new), kind)

    def test_rename_stages_receipts_after_inflight_delivery_effect(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        row, err = seats.dm("alice", "@all private", who="owner")
        self.assertIsNone(err)
        entered, release, staged, delivered, renamed = (
            threading.Event(), threading.Event(), threading.Event(), [], [])
        real_record = seats_delivery.record_delivery_receipt
        real_move = seats_receipts.move_room_receipts

        def blocked_record(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real_record(*args, **kwargs)

        def marked_move(*args, **kwargs):
            staged.set()
            return real_move(*args, **kwargs)

        with mock.patch("helm.seats_delivery.record_delivery_receipt",
                        side_effect=blocked_record), mock.patch(
                "helm.seats_receipts.move_room_receipts", side_effect=marked_move):
            delivery = threading.Thread(target=lambda: delivered.append(
                seats.deliver_any(session="s-a", seat="alice",
                                  emit=lambda _line: None, channel="hook")))
            delivery.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renamer = threading.Thread(target=lambda: renamed.append(
                seats.rename_seat("alice", "renamed")))
            renamer.start()
            self.assertFalse(staged.wait(0.05))
            release.set()
            delivery.join(THREAD_TIMEOUT)
            renamer.join(THREAD_TIMEOUT)
        self.assertFalse(delivery.is_alive())
        self.assertFalse(renamer.is_alive())
        self.assertTrue(staged.is_set())
        self.assertTrue(renamed[0][0], renamed[0][1])
        new_lane = chat.DM_PREFIX + seats._seat_key("renamed")
        self.assertEqual(len(seats_receipts.delivery_receipts(
            row["id"], new_lane)), 1)

    def test_presence_writer_holds_rename_estate_until_write_finishes(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        entered, release, renamed = threading.Event(), threading.Event(), []
        real_utime = os.utime

        def blocked(path, *args, **kwargs):
            if path == seats.seen_path("alice"):
                entered.set()
                self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real_utime(path, *args, **kwargs)

        with mock.patch("helm.seats_roster.os.utime", side_effect=blocked):
            writer = threading.Thread(target=lambda: seats.touch_seen("alice"))
            writer.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renamer = threading.Thread(target=lambda: renamed.append(
                seats.rename_seat("alice", "renamed")))
            renamer.start()
            renamer.join(0.05)
            self.assertTrue(renamer.is_alive())
            release.set()
            writer.join(THREAD_TIMEOUT)
            renamer.join(THREAD_TIMEOUT)
        self.assertFalse(writer.is_alive())
        self.assertTrue(renamed[0][0], renamed[0][1])
        self.assertFalse(seats.touch_seen("alice"))
        self.assertFalse(os.path.exists(seats.seen_path("alice")))
        self.assertTrue(os.path.exists(seats.seen_path("renamed")))

    def test_stop_latch_writer_serializes_with_rename_and_refuses_retired_key(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        old = seats._stop_fp_path("main", "alice", "s-a", kind="stopndp")
        new = seats._stop_fp_path("main", "renamed", "s-a", kind="stopndp")
        entered, release, wrote, renamed = (
            threading.Event(), threading.Event(), [], [])
        real = seats_stop_signals.pk.atomic_write

        def blocked(path, value):
            if path == old:
                entered.set()
                self.assertTrue(release.wait(THREAD_TIMEOUT))
            return real(path, value)

        with mock.patch("helm.seats_stop_signals.pk.atomic_write",
                        side_effect=blocked):
            writer = threading.Thread(target=lambda: wrote.append(
                seats_cursor._write_stop_latch(
                    old, "alice", "first", session="s-a")))
            writer.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            renamer = threading.Thread(target=lambda: renamed.append(
                seats.rename_seat("alice", "renamed")))
            renamer.start()
            renamer.join(0.05)
            self.assertTrue(renamer.is_alive())
            release.set()
            writer.join(THREAD_TIMEOUT)
            renamer.join(THREAD_TIMEOUT)
        self.assertFalse(writer.is_alive())
        self.assertFalse(renamer.is_alive())
        self.assertEqual(wrote, [True])
        self.assertTrue(renamed[0][0], renamed[0][1])
        self.assertFalse(os.path.exists(old))
        self.assertEqual(pk.read_json(new, None), None)
        with open(new, encoding="utf-8") as f:
            self.assertEqual(f.read(), "first")
        self.assertFalse(seats_cursor._write_stop_latch(
            old, "alice", "late", session="s-a"))
        self.assertFalse(os.path.exists(old))

    def test_state_lock_modes_fence_key_reincarnation(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        incarnation = seats_cursor.seat_incarnation("alice", session="s-a")
        old = seats._stop_fp_path("main", "alice", "s-a", kind="modes")
        entered, release, result = threading.Event(), threading.Event(), []
        real = seats_cursor.seat_incarnation
        first = [True]

        def delayed(seat, session=None):
            value = real(seat, session=session)
            if first[0]:
                first[0] = False
                entered.set()
                self.assertTrue(release.wait(THREAD_TIMEOUT))
            return value

        with mock.patch("helm.seats_cursor.seat_incarnation",
                        side_effect=delayed):
            writer = threading.Thread(target=lambda: result.append(
                seats_cursor._write_stop_latch(old, "alice", "neither")))
            writer.start()
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            ok, message = seats.rename_seat("alice", "renamed")
            self.assertTrue(ok, message)
            seats.join(session="s-new", seat="alice", cwd="/tmp/p")
            release.set()
            writer.join(THREAD_TIMEOUT)
        self.assertEqual(result, [False])       # neither: owner-derived token
        self.assertFalse(seats_cursor._write_stop_latch(
            old, "alice", "session", session="s-a"))
        self.assertFalse(seats_cursor._write_stop_latch(
            old, "alice", "incarnation", incarnation=incarnation))
        self.assertFalse(seats_cursor._write_stop_latch(
            old, "alice", "both", session="s-a", incarnation=incarnation))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(seats_cursor._write_stop_latch(
            old, "alice", "current", session="s-new"))

    def test_deferred_seam_latch_cannot_cross_retired_key_reuse(self):
        seats.join(session="s-a", seat="alice", cwd="/tmp/p")
        incarnation = seats_cursor.seat_incarnation("alice", session="s-a")
        self.assertTrue(incarnation)
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        seats_stop_seam._SURVIVES_REFUSAL.clear()
        self.addCleanup(seats_stop_seam._PENDING_DISCLOSURES.clear)
        self.addCleanup(seats_stop_seam._SURVIVES_REFUSAL.clear)
        shared = "/tmp/shared-room"
        text = seats_stop_seam._cotenancy_warn(
            [{"path": shared, "seats": ["alice", "bob"]}], {shared},
            "main", "alice", "s-a")
        self.assertIsNotNone(text)
        self.assertEqual(
            seats_stop_seam._PENDING_DISCLOSURES[-1][-1], incarnation)
        old = seats._stop_fp_path(
            "main", "alice", "s-a", kind=seats_stop_seam.COTENANCY_LATCH)
        ok, message = seats.rename_seat("alice", "renamed")
        self.assertTrue(ok, message)
        seats.join(session="s-new", seat="alice", cwd="/tmp/p")
        self.assertNotEqual(
            seats_cursor.seat_incarnation("alice", session="s-new"),
            incarnation)
        seen = seats.seen_path("alice")
        os.remove(seen)
        self.assertFalse(seats.touch_seen("alice", session="s-a"))
        self.assertFalse(os.path.exists(seen))
        self.assertTrue(seats.touch_seen("alice", session="s-new"))
        stale_scan = seats_delivery.scan_path("alice", "s-a", "reuse")
        self.assertEqual(seats_delivery._fair_room_slice(
            ["z", "a"], "alice", "s-a", 1, "reuse"), ["a"])
        self.assertFalse(os.path.exists(stale_scan))
        live_scan = seats_delivery.scan_path("alice", "s-new", "reuse")
        self.assertEqual(seats_delivery._fair_room_slice(
            ["z", "a"], "alice", "s-new", 1, "reuse"), ["a"])
        self.assertTrue(os.path.exists(live_scan))
        out = io.StringIO()
        self.assertEqual(seats_stop_seam.emit_warns([text], stream=out), 0)
        self.assertIn("SEAM RUNG BLIND SPOT", out.getvalue())
        self.assertFalse(os.path.exists(old))

    def test_every_core_stop_latch_writer_names_the_rename_guard_owner(self):
        import ast
        import inspect
        from helm import (seats_catchup, seats_delegation, seats_roomscan,
                          seats_roster, seats_stop_claims, seats_stop_guard,
                          seats_stop_ndp, seats_work_offer)

        cases = {
            seats_catchup.catchup: ("seat_state_lock",),
            seats_stop_guard._ndp_gate: ("_write_stop_latch",),
            # MOVED, NOT DROPPED: the claims rung left _stop_guard for
            # seats_stop_claims and took `_remove_stop_latch` with it, so the
            # name is asserted where the writer now lives. The SET of asserted
            # names is conserved across the two entries — losing one here
            # while adding none there is how this check would quietly stop
            # covering a latch writer.
            seats_stop_guard._stop_guard: (
                "_write_stop_latch", "seat_state_lock"),
            seats_stop_claims.claims_rung: (
                "_write_stop_latch", "_remove_stop_latch"),
            seats_stop_signals._beacon_block: ("seat_state_lock",),
            seats_stop_signals._spiral_gate: ("_write_stop_latch",),
            seats_stop_seam._latch_dir_writable: ("_probe_stop_latch",),
            seats_stop_seam.commit_disclosures: ("_write_stop_latch",),
            seats_work_offer._stop_whisper: ("seat_state_lock",),
            seats_delegation._claim_evidence_warning: ("seat_state_lock",),
        }
        for fn, owners in cases.items():
            source = inspect.getsource(fn)
            with self.subTest(fn=fn.__name__):
                for owner in owners:
                    self.assertIn(owner, source)
        # seats_roomscan is here because the room ROTATION took its
        # `seat_state_lock` call with it when it left seats_delivery: a census
        # that walks a fixed module list stops seeing a guarded call the moment
        # the code moves, and reads as one fewer writer rather than as a gap.
        modules = (seats_catchup, seats_cursor, seats_delegation,
                   seats_delivery, seats_roomscan, seats_roster,
                   seats_stop_claims, seats_stop_guard, seats_stop_ndp,
                   seats_stop_seam, seats_stop_signals, seats_work_offer)
        calls = []
        for module in modules:
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or \
                    getattr(node.func, "attr", None)
                if name not in ("seat_state_lock", "_write_stop_latch"):
                    continue
                calls.append((module.__name__, node.lineno,
                              {kw.arg for kw in node.keywords}))
        # 17, not 16: the clean-stop line gained a same-state latch, which is a
        # seventeenth writer and is exactly what this census exists to notice.
        self.assertEqual(len(calls), 17, "void/drifted guard census, not clean")
        self.assertEqual([
            (module, line) for module, line, keywords in calls
            if not keywords.intersection(("session", "incarnation"))], [])

    def test_rename_refuses_case_collision(self):
        """A case-variant of a live seat is the SAME address + keyed state
        downstream (casefold keys, re.I mentions) — renaming INTO one must be
        refused, else the two rows alias mentions/presence and the reaper
        cross-fires onto the live seat's state (kimi cross-family review,
        live-probed 2026-07-21). A pure self-case-change is still allowed."""
        seats.join(session="s-k", seat="kimi", cwd="/tmp/p")
        seats.join(session="s-a", seat="alpha", cwd="/tmp/p")
        ok, msg = seats.rename_seat("alpha", "KIMI")
        self.assertFalse(ok, msg)
        self.assertIn("taken", msg)
        self.assertNotIn("KIMI", seats.roster())         # no aliased row minted
        self.assertIn("kimi", seats.roster())
        ok, _ = seats.rename_seat("kimi", "Kimi")         # self-case-change ok
        self.assertTrue(ok)

    def test_cli_and_web_rename(self):
        seats.join(session="s-9", seat="blob", cwd="/tmp/p")
        rc, _out, err = self.cmd("seat", ["rename", "blob", "buddy"])
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy", seats.roster())
        obj, code = web._api_chat_seat({"action": "rename",
                                        "seat": "buddy", "new": "pal"})
        self.assertEqual(code, 200, obj)
        self.assertIn("pal", seats.roster())
        obj, code = web._api_chat_seat({"action": "rename",
                                        "seat": "ghost", "new": "x"})
        self.assertEqual(code, 400)
        obj, code = web._api_chat_seat({"action": "nuke", "seat": "pal"})
        self.assertEqual(code, 400)
        rc, _out, err = self.cmd("seat", ["rename"])   # usage
        self.assertEqual(rc, 2)


class RenameAliasTest(SeatsBase):
    """task/2338 — a renamed seat keeps its old name reachable for a window.

    MEASURED BEFORE THE CURE (scratch seat under an isolated home): after
    `seat rename old new` the retained session, still
    carrying HELM_CHAT_NAME=old, was refused as a TAKEOVER at join, post and
    beacon-arm; an @old room mention was written ABSENT and never delivered
    to the renamed seat; `wait --seat old` from the renamed process refused
    "cannot receive for another seat". Every arm here has its negative: the
    same surface with NO alias (--alias-hours 0) or an EXPIRED one refuses
    or drops exactly as before, and --dry-run leaves the roster and the chat
    dir byte-identical."""

    OLD, NEW, PEER = "seat-old", "seat-new", "seat-b"

    def _rename(self, **kw):
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        ok, msg = seats.rename_seat(self.OLD, self.NEW, **kw)
        self.assertTrue(ok, msg)
        return msg

    def _expire(self):
        """Age the alias window out, through the roster the doors read."""
        r = seats.roster()
        event = r[self.NEW][seats.RENAME_ALIAS_FIELD]
        event["until"] = pk.epoch_ts(time.time() - 60)
        pk.write_json(seats.roster_path(), r)

    def test_the_rename_records_the_alias_and_the_listing_shows_it(self):  # noqa: VACUOUS_ASSERTION — the absence arms (no alias, no listing line) follow an unconditional positive control on the same observables (alias recorded, `was @` rendered) earlier in this arm
        msg = self._rename()
        self.assertIn("the old name still answers", msg)
        row = seats.roster()[self.NEW]
        event = row[seats.RENAME_ALIAS_FIELD]
        self.assertEqual(event["old"], self.OLD)
        self.assertGreater(pk.parse_ts_epoch(event["until"]),
                           time.time() + 23 * 3600)
        self.assertEqual(seats.alias_names(self.NEW), [self.OLD])
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW)
        rep = next(s for s in seats_report.roster_report()["seats"]
                   if s["seat"] == self.NEW)
        self.assertEqual(rep["aliases"],
                         [{"old": self.OLD, "until": event["until"]}])
        _rc, out, _err = self.cmd("seats", ["--all"])
        self.assertIn("was @%s until %s" % (self.OLD, event["until"]), out)
        # NEGATIVE: no window, no alias, no listing
        self._expire()
        self.assertEqual(seats.alias_names(self.NEW), [])
        self.assertEqual(seats.live_alias(self.OLD), (None, None))
        _rc, out, _err = self.cmd("seats", ["--all"])
        self.assertNotIn("was @", out)

    def test_alias_hours_zero_records_no_alias(self):
        self._rename(alias_hours=0)
        self.assertNotIn(seats.RENAME_ALIAS_FIELD, seats.roster()[self.NEW])
        self.assertEqual(seats.live_alias(self.OLD), (None, None))

    def test_old_name_mention_reply_and_dm_deliver_inside_the_window(self):
        self._rename()
        chat.post("@%s go" % self.OLD, who=self.PEER)
        self.assertIn("go", seats.deliver(session="s-old", seat=self.NEW))
        # the addressee snapshot and its line name the row the alias reaches
        row = chat.post("@%s again" % self.OLD, who=self.PEER)
        cap = next(c for c in row["addressees"] if c["raw"] == self.OLD)
        self.assertEqual((cap["membership"], cap["canonical"]),
                         ("JOINED", self.NEW))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chat._disclose_post_addressees(row)
        self.assertIn("addressee @%s is JOINED as %s" % (self.OLD, self.NEW),
                      out.getvalue())
        self.assertIn("again", seats.deliver(session="s-old", seat=self.NEW))
        # a reply to a row the seat authored under its OLD name still wakes it
        parent = chat.post("mine", who=self.OLD)
        chat.post("re", who=self.PEER, reply_to=parent["id"])
        self.assertIn("re", seats.deliver(session="s-old", seat=self.NEW))
        # a DM to the old name lands as a DM to the new one
        row, err = seats.dm(self.OLD, "psst", who=self.PEER)
        self.assertIsNone(err)
        self.assertEqual(str(row["dm"]), self.NEW)
        self.assertIn("psst", seats.deliver_any(session="s-old", seat=self.NEW))
        self.assertIsNone(seats.deliver_any(session="s-old", seat=self.NEW))
        # NEGATIVE: expired — the mention is dropped again, the DM still
        # rides the lane redirect (that continuity predates this lane)
        self._expire()
        row = chat.post("@%s late" % self.OLD, who=self.PEER)
        self.assertIsNone(seats.deliver(session="s-old", seat=self.NEW))
        cap = next(c for c in row["addressees"] if c["raw"] == self.OLD)
        self.assertEqual((cap["membership"], cap["canonical"]),
                         ("ABSENT", self.OLD))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chat._disclose_post_addressees(row)
        self.assertIn("addressee @%s is ABSENT" % self.OLD, out.getvalue())
        row, err = seats.dm(self.OLD, "late-dm", who=self.PEER)   # the lane
        self.assertIsNone(err)                                     # redirect
        self.assertEqual(str(row["dm"]), self.OLD)                 # is what
        self.assertIn("late-dm", seats.deliver_any(session="s-old",  # carries
                                                    seat=self.NEW))  # it now

    def test_no_alias_drops_the_old_name_mention(self):
        self._rename(alias_hours=0)
        chat.post("@%s go" % self.OLD, who=self.PEER)
        self.assertIsNone(seats.deliver(session="s-old", seat=self.NEW))
        chat.post("@%s go" % self.NEW, who=self.PEER)      # control
        self.assertIn("go", seats.deliver(session="s-old", seat=self.NEW))

    def test_recipient_doors_resolve_the_alias_to_the_canonical_name(self):
        self._rename()
        got, err = seats.resolve_recipient(self.OLD)
        self.assertIsNone(err)
        self.assertEqual((str(got), got.display), (self.NEW, self.NEW))
        cap = seats.recipient_capability(self.OLD)
        self.assertEqual((cap["membership"], cap["canonical"]),
                         ("JOINED", self.NEW))
        self._expire()
        cap = seats.recipient_capability(self.OLD)
        self.assertEqual((cap["membership"], cap["canonical"]),
                         ("ABSENT", self.OLD))
        got, err = seats.resolve_recipient("seat-nobody")   # unaliased
        self.assertIsNone(err)
        self.assertEqual(str(got), "seat-nobody")

    def test_the_inherited_pin_is_admitted_as_the_new_seat_and_says_so(self):
        self._rename()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.OLD}):
            self.assertEqual(seats.own_name(), self.NEW)
            self.assertEqual(seats.declared_name()[1][0], self.OLD)
            self.assertIsNone(seats.identity_disagreement("s-old"))
            self.assertEqual(seats.acting_seat("s-old"), self.NEW)
            self.assertEqual(chat.whoname(), self.NEW)
            seat, line = seats.join(session="s-old", cwd="/tmp/p")
            self.assertEqual(seat, self.NEW)
            self.assertIn("seat '%s'" % self.NEW, line)
            self.assertIn("HELM_CHAT_NAME is still '%s' — a rename alias of "
                          "%s until" % (self.OLD, self.NEW), line)
            # the beacon's --seat assertion under the old name arms the row
            self.assertEqual(seats._assert_own_seat(self.OLD),
                             (self.NEW, None))
            self.assertEqual(seats._assert_own_seat(self.NEW),
                             (self.NEW, None))
            self.assertIsNotNone(seats._assert_own_seat(self.PEER)[1])
            # NEGATIVE: expired — the pin is a stranger again, everywhere
            self._expire()
            self.assertEqual(seats.own_name(), self.OLD)
            self.assertEqual(seats.identity_disagreement("s-old"),
                             (self.OLD, self.NEW))
            _seat, line = seats.join(session="s-old", cwd="/tmp/p")
            self.assertIn("JOIN REFUSED", line)
            # the beacon door refuses the pin again (the assertion itself is
            # self-consistent — a stranger naming itself — so the refusal
            # is the dispute, exactly as measured before the cure)
            self.assertIn("REFUSING to arm",
                          seats_join._beacon_identity_refusal("s-old") or "")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.NEW}):
            # the new-named process may still name its old self while live
            self._rename_window_reset()
            self.assertEqual(seats._assert_own_seat(self.OLD),
                             (self.NEW, None))

    def _rename_window_reset(self):
        r = seats.roster()
        r[self.NEW][seats.RENAME_ALIAS_FIELD]["until"] = pk.epoch_ts(
            time.time() + 3600)
        pk.write_json(seats.roster_path(), r)

    def test_a_re_admitted_old_name_is_nobody_s_alias(self):  # noqa: VACUOUS_ASSERTION — every absence (alias gone, no wake, no listing) is paired with its positive: alias_names==[OLD] before reuse, and the occupant receiving yours/from-the-occupant/re after
        """CL99: the reverse lookup must go through the same resolver
        as the forward one. A real rename, then the old name re-admitted as
        its own row through the shipped join door, then every control on the
        renamed seat: no wake on @old, no reply/reaction wake for old-authored
        rows, no own-post suppression of the occupant, recipient selects the
        occupant, listing shows no alias. The pre-reuse half of each control
        is asserted first, so the arm cannot pass on an inert alias."""
        self._rename()
        self.assertEqual(seats.alias_names(self.NEW), [self.OLD])   # live
        self.assertEqual(seats.seat_scope(self.NEW)["aliases"], [self.OLD])
        # NAME REUSE through the shipped producer: a fresh session joins as
        # the retired name and write_roster admits it as its own row
        seat, _line = seats.join(session="s-reuse", seat=self.OLD,
                                 cwd="/tmp/p")
        self.assertEqual(seat, self.OLD)
        self.assertIn(self.OLD, seats.roster())
        self.assertNotIn(seats.RENAME_ALIAS_FIELD, seats.roster()[self.NEW],
                         "the admission retired B's claim to A in the same "
                         "publication; nothing is left to resolve")
        self.assertEqual(seats.alias_names(self.NEW), [])
        self.assertEqual(seats.row_alias(self.NEW), (None, None))
        self.assertEqual(seats.seat_scope(self.NEW)["aliases"], [])
        self.assertEqual(seats.live_alias(self.OLD), (None, None))
        # recipient: the occupant, spelled as itself
        got, err = seats.resolve_recipient(self.OLD)
        self.assertIsNone(err)
        self.assertEqual(str(got), self.OLD)
        # delivery: @old reaches the occupant, never the renamed seat
        chat.post("@%s yours" % self.OLD, who=self.PEER)
        self.assertIsNone(seats.deliver(session="s-old", seat=self.NEW))
        self.assertIn("yours", seats.deliver(session="s-reuse", seat=self.OLD))
        # the occupant's mention of the renamed seat is NOT suppressed as own
        chat.post("@%s from the occupant" % self.NEW, who=self.OLD)
        self.assertIn("occupant", seats.deliver(session="s-old", seat=self.NEW))
        # a reply to an occupant-authored row wakes the occupant only
        parent = chat.post("occupant row", who=self.OLD)
        chat.post("re", who=self.PEER, reply_to=parent["id"])
        self.assertIsNone(seats.deliver(session="s-old", seat=self.NEW))
        self.assertIn("re", seats.deliver(session="s-reuse", seat=self.OLD))
        # a reaction on an occupant-authored row wakes the occupant only
        chat.react(-2, "👍", who=self.PEER)          # the occupant row (the
                                                   # reply is the last one)
        self.assertIsNone(seats.deliver(session="s-old", seat=self.NEW))
        self.assertIsNotNone(seats.deliver(session="s-reuse", seat=self.OLD))
        # listing: no alias on the renamed row
        rep = {s["seat"]: s for s in seats_report.roster_report()["seats"]}
        self.assertEqual(rep[self.NEW]["aliases"], [])
        _rc, out, _err = self.cmd("seats", ["--all"])
        self.assertNotIn("was @", out)

    def test_admitting_a_name_retires_the_older_generations_claim(self):
        """A->B, rejoin fresh A, rename that A to C — all inside the original
        window. The exact-key rule alone
        left B's hop for A on the roster, so once A was renamed away again
        two rows claimed A and dict order decided. Admission now retires the
        older claim in the same locked publication; a Z hop on B (the same
        generation) keeps its original expiry; deleting C revives nothing."""
        seats.join(session="s-z", seat="seat-z", cwd="/tmp/p")
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        ok, msg = seats.rename_seat("seat-z", self.OLD)      # Z -> A
        self.assertTrue(ok, msg)
        ok, msg = seats.rename_seat(self.OLD, self.NEW)      # A -> B
        self.assertTrue(ok, msg)
        hops = dict(seats.row_aliases(self.NEW))
        self.assertEqual(sorted(hops), [self.OLD, "seat-z"])
        z_until = hops["seat-z"]
        seat, _line = seats.join(session="s-reuse", seat=self.OLD,  # fresh A
                                 cwd="/tmp/p")
        self.assertEqual(seat, self.OLD)
        self.assertEqual(seats.row_aliases(self.NEW), [("seat-z", z_until)],
                         "B keeps its Z hop with the original expiry; A gone")
        rec = seats.roster()[self.NEW][seats.RENAME_ALIAS_FIELD]
        self.assertEqual([h["old"] for h in [rec] + rec["prior"]], ["seat-z"])
        ok, msg = seats.rename_seat(self.OLD, "seat-c")      # A -> C, live
        self.assertTrue(ok, msg)
        self.assertEqual(seats.live_alias(self.OLD)[0], "seat-c")
        self.assertEqual(seats.row_aliases("seat-c")[0][0], self.OLD)
        self.assertEqual(seats.alias_names(self.NEW), ["seat-z"])
        got, err = seats.resolve_recipient(self.OLD)
        self.assertIsNone(err)
        self.assertEqual(str(got), "seat-c")
        chat.post("@%s which" % self.OLD, who=self.PEER)
        self.assertIsNone(seats.deliver(session="s-z", seat=self.NEW))
        self.assertIn("which", seats.deliver(session="s-reuse", seat="seat-c"))
        parent = chat.post("mine", who=self.OLD)          # C-era row by old name
        chat.post("re", who=self.PEER, reply_to=parent["id"])
        self.assertIsNone(seats.deliver(session="s-z", seat=self.NEW))
        self.assertIn("re", seats.deliver(session="s-reuse", seat="seat-c"))
        chat.post("@%s own" % self.NEW, who=self.OLD)     # not B's own post
        self.assertIn("own", seats.deliver(session="s-z", seat=self.NEW))
        # removing C afterwards revives no prior-generation claim
        r = seats.roster()
        r.pop("seat-c")
        pk.write_json(seats.roster_path(), r)
        self.assertEqual(seats.live_alias(self.OLD), (None, None))
        self.assertEqual(seats.alias_names(self.NEW), ["seat-z"])

    def test_a_second_rename_keeps_the_first_window_and_its_expiry(self):
        self._rename()
        first = seats.roster()[self.NEW][seats.RENAME_ALIAS_FIELD]["until"]
        ok, msg = seats.rename_seat(self.NEW, "seat-c", alias_hours=1)
        self.assertTrue(ok, msg)
        hops = dict(seats.row_aliases("seat-c"))
        self.assertEqual(sorted(hops), [self.NEW, self.OLD])
        self.assertEqual(pk.epoch_ts(hops[self.OLD]), first, "original expiry")
        self.assertLess(hops[self.NEW], hops[self.OLD])
        self.assertEqual(seats.alias_names("seat-c"), [self.NEW, self.OLD])
        for name in (self.OLD, self.NEW):                # both still address
            self.assertEqual(seats.live_alias(name)[0], "seat-c")
            chat.post("@%s hop" % name, who=self.PEER)
            self.assertIn("hop", seats.deliver(session="s-old", seat="seat-c"))
        _rc, out, _err = self.cmd("seats", ["--all"])
        self.assertIn("was @%s until" % self.OLD, out)
        self.assertIn("was @%s until" % self.NEW, out)
        # NEGATIVE: the first hop expires on ITS clock, the second stays
        r = seats.roster()
        r["seat-c"][seats.RENAME_ALIAS_FIELD]["prior"][0]["until"] = \
            pk.epoch_ts(time.time() - 1)
        pk.write_json(seats.roster_path(), r)
        self.assertEqual(seats.alias_names("seat-c"), [self.NEW])
        chat.post("@%s gone" % self.OLD, who=self.PEER)
        self.assertIsNone(seats.deliver(session="s-old", seat="seat-c"))
        # a round trip is still a rename: the old key wins by precedence
        ok, msg = seats.rename_seat("seat-c", self.NEW)
        self.assertTrue(ok, msg)
        self.assertEqual(seats.alias_names(self.NEW), ["seat-c"])

    def test_a_non_finite_window_is_refused_and_writes_nothing(self):
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        with open(seats.roster_path(), "rb") as f:
            before = f.read()
        for bad in ("inf", "nan", "-1", "-inf", "x"):
            rc, _out, err = self.cmd("seat", ["rename", self.OLD, self.NEW,
                                              "--alias-hours", bad])
            self.assertNotEqual(rc, 0, bad)     # `-1` reads as a FLAG: rc 2
            self.assertIn("finite, non-negative", err)
            ok, msg = seats.rename_seat(self.OLD, self.NEW,
                                        alias_hours=float(bad)
                                        if bad != "x" else bad)
            self.assertFalse(ok, msg)
        with open(seats.roster_path(), "rb") as f:
            self.assertEqual(f.read(), before)
        ok, msg = seats.rename_seat(self.OLD, self.NEW, alias_hours="1.5")
        self.assertTrue(ok, msg)                        # control: finite

    def test_a_failed_roster_read_is_never_cached_as_no_alias(self):
        """CL98: the identity cache keys on inode+mtime+size, and a
        chmod changes none of them. A read that FAILED must therefore never
        be memoised as an empty roster, or the same long-lived process keeps
        disputing its own retained pin after the file is readable again."""
        from helm import seats_common
        self._rename()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": self.OLD}):
            seats_common._DECLARED_CACHE.clear()
            path = seats.roster_path()     # captured NOW: tearDown restores
            os.chmod(path, 0)              # the env before cleanups run
            self.addCleanup(lambda: os.path.exists(path)
                            and os.chmod(path, 0o600))
            self.assertEqual(seats.declared_name(), (self.OLD, None))
            self.assertEqual(seats_common._DECLARED_CACHE, {},
                             "a failed read was memoised")
            os.chmod(seats.roster_path(), 0o600)      # same ino/mtime/size
            name, alias = seats.declared_name()
            self.assertEqual((name, alias[0]), (self.NEW, self.OLD))
            self.assertIsNone(seats.identity_disagreement("s-old"))
            # the positive IS cached: one entry, keyed on the file identity
            self.assertEqual(len(seats_common._DECLARED_CACHE), 1)

    def test_waiter_attribution_follows_the_alias(self):  # noqa: VACUOUS_ASSERTION — the expired arm asserts the OLD name, after the live arm asserted the NEW one through the same waiter_seat call
        self._rename()
        argv = ["/usr/bin/helm", "chat", "wait", "--seat", self.OLD, "--follow"]
        self.assertEqual(beacons.waiter_seat(0, argv=argv), self.NEW)
        self.assertEqual(beacons.waiter_seat(
            0, argv=["/usr/bin/helm", "chat", "wait", "--follow"],
            env={"HELM_CHAT_NAME": self.OLD}), self.NEW)
        self._expire()
        self.assertEqual(beacons.waiter_seat(0, argv=argv), self.OLD)

    def test_dry_run_prints_six_surfaces_and_changes_nothing(self):  # noqa: VACUOUS_ASSERTION — byte-identical images after the dry-run are controlled by the real verb through the same door changing the image at the end of the arm
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")

        def image():
            out = {}
            for name in sorted(os.listdir(chat.chat_dir())):
                path = os.path.join(chat.chat_dir(), name)
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        out[name] = f.read()
            return out
        before = image()
        ok, msg = seats.rename_seat(self.OLD, self.NEW, dry_run=True)
        self.assertTrue(ok, msg)
        self.assertIn("nothing was written", msg)
        self.assertIn("Six surfaces would change", msg)
        for n, word in ((1, "roster"), (2, "identity admission"),
                        (3, "chat addressing"), (4, "beacon"),
                        (5, "dispatch"), (6, "spawn register")):
            self.assertIn("  %d %s:" % (n, word), msg)
        self.assertEqual(image(), before, "dry-run wrote something")
        self.assertIn(self.OLD, seats.roster())
        # a dry-run still REPORTS a refusal, and still writes nothing
        ok, msg = seats.rename_seat(self.OLD, self.PEER, dry_run=True)
        self.assertFalse(ok)
        self.assertIn("taken", msg)
        self.assertEqual(image(), before)
        # CONTROL: the real verb through the same door does write
        rc, out, _err = self.cmd("seat", ["rename", self.OLD, self.NEW,
                                          "--alias-hours", "2"])
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(image(), before)
        until = seats.roster()[self.NEW][seats.RENAME_ALIAS_FIELD]["until"]
        self.assertLess(abs(pk.parse_ts_epoch(until) - time.time() - 7200), 60)
        rc, out, err = self.cmd("seat", ["rename", self.PEER, "seat-c",
                                         "--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertIn("would rename %s -> seat-c" % self.PEER, out)
        self.assertIn(self.PEER, seats.roster())


class RenameSpawnRegisterTest(SeatsBase):
    """task/2444 — the rename that came back, 300 seconds later.

    MEASURED ON A LIVE FLEET: `helm chat seat rename` succeeded, and a bare
    row under the OLD name appeared 280 seconds later — inside
    `helm-seat-rebind.timer`'s 300s cadence, one process, two roster
    rows, and the `renamed` alias that was covering the window destroyed by the
    admission that minted the second row.

    TWO DEFECTS IN ONE CHAIN, and each half is measured here on its own.
    `seat_lifecycle_runtime._record_identity` reads `identity or seat` off the
    spawn register; two docstrings state that a durable rename writes
    `identity` and keeps the register KEYED by the original name, and NOTHING
    EVER WROTE THAT FIELD — so the timer's `_bind_runtime_session
    (_record_identity(rec), ...)` handed `write_roster` the pre-rename name.
    `write_roster` then resolved that name only by casefold against existing
    KEYS, never through a live rename hop, so it ADMITTED it, minted the bare
    row, and `retire_alias_claims` popped the alias on the way out.

    REPRODUCED BEFORE THE CURE with these same producers on the un-cured tree:
    `_record_identity` answered the OLD name after a successful rename, and
    `write_roster(<old>)` returned `admits True` and a second roster key.
    """

    # NOT A REAL SEAT, AND NOT THE HOUSE CONVENTION EITHER — MEASURED, because
    # the two guards disagree here and the disagreement is the interesting bit.
    # tests/ is public-bound so a live identity is publication debt; but the
    # storage key of a spawn register MUST resolve a family (`_seat_family`
    # answers `unknown seat 'seat-a' (families: codex, ...; instances:
    # <family>-N)`), and `_identity_register` skips a storage key it cannot
    # resolve as UNREADABLE. So `seat-a` makes this whole class read
    # UNKNOWN rather than exercise the walk. OLD is therefore family-SHAPED
    # and deliberately outside the authority: no `codex-99` seat exists, and
    # the defect is about the SHAPE of a rename rather than about whoever it
    # happened to. The house names stay where no family has to resolve.
    OLD, NEW, PEER = "codex-99", "seat-b", "seat-peer"

    # A CONTROLLED INSTANT the ordinary fixture can never produce, so an
    # equality against it is a statement about THIS call's write.
    STAMP = 1_700_000_123.5

    def _register(self, storage=None, **fields):
        """Plant a REAL spawn register where `registered_seats` finds it.

        NESTED, never flat: an instance register lives at
        `<seats>/<family>/instances/<seat>/spawn.json`, and `registered_seats`
        documents that a flat fixture makes this whole class invisible."""
        storage = storage or self.OLD
        d = seatmod._instance_dir("codex", storage)
        os.makedirs(d, mode=0o700, exist_ok=True)
        rec = {"seat": storage, "harness": "orca", "session": "s-old",
               "handle": "h-1", "pane_key": "pk-1", "ts": 1234,
               "model": "sonnet"}
        rec.update(fields)
        pk.write_json(seatmod._spawn_path(d), rec)
        return d

    def _rename(self, **kw):
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        ok, msg = seats.rename_seat(self.OLD, self.NEW, **kw)
        self.assertTrue(ok, msg)
        return msg

    def test_ONE_readable_register_does_not_authorize_a_UNIQUE_rename(self):
        """A PARTIAL CENSUS MAY NOT NAME A UNIQUE OWNER.

        The blindness check sat BELOW the single-hit return, so one readable
        register short-circuited it: a tree with a hidden second claimant
        returned the first as the unique one. Rereading the file we happened
        to select proves a local declaration, never exclusive ownership.
        """
        self._register()
        # A SECOND STORAGE KEY THAT CANNOT BE READ. Family-shaped so the walk
        # ENUMERATES it — the point is an entry present and unreadable, not an
        # entry the walk never saw.
        blind = seatmod._instance_dir("codex", "codex-98")
        os.makedirs(blind, mode=0o700, exist_ok=True)
        with io.open(seatmod._spawn_path(blind), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        # MUST-HIT ON THE INPUT: both keys are enumerated, so a refusal below
        # is about readability and not about a walk that saw one entry.
        found, _blindwalk = seatmod.registered_seats()
        self.assertEqual(sorted(found), ["codex-98", self.OLD],
                         "fixture: the walk did not enumerate both registers")

        storage, d, rec, err = seat_runtime._identity_register(self.OLD)
        self.assertIsNone(storage,
                          "a partial census named a unique owner: %r" % storage)
        self.assertIsNone(d)
        self.assertIsNone(rec)
        self.assertIn("UNKNOWN", err or "")
        self.assertIn("ONLY one", err or "",
                      "the refusal does not say that UNIQUENESS is what is "
                      "unknown, which is the whole finding: %r" % err)

        # THE POSITIVE CONTROL ON THE SAME DOOR: remove the unreadable entry
        # and the identical call RESOLVES, so the refusal is about the partial
        # census and not about a function that refuses everything.
        os.remove(seatmod._spawn_path(blind))
        os.rmdir(blind)
        storage2, d2, rec2, err2 = seat_runtime._identity_register(self.OLD)
        self.assertIsNone(err2, err2)
        self.assertEqual(storage2, self.OLD)
        self.assertIsNotNone(d2)
        self.assertEqual(rec2.get("seat"), self.OLD)

    def test_a_FAILED_register_write_advertises_no_retry_that_cannot_run(self):
        """THE ADVICE WAS DEAD IN BOTH SPELLINGS.

        By the time this message is produced, the roster and chat writes have
        COMMITTED — so `rename OLD NEW` refuses ("no roster row matches") and
        `rename NEW NEW` returns "already named" without touching anything.
        Advertising a re-run promises a recovery that never runs, which is
        worse than reporting the split, because the operator stops looking.
        """
        self._register()

        def refuse(path, rec):
            raise OSError("register is read-only")

        # PATCH THE SOURCE MODULE, not a name on the caller: this function
        # imports `pk` LOCALLY (`from . import pk, seat`), so the module has
        # no `pk` attribute to rebind and a patch there would silently miss.
        with mock.patch.object(pk, "write_json", refuse):
            msg = seat_runtime.rename_register_identity(
                self.OLD, self.NEW, apply=True)
        self.assertIn("could NOT be re-pointed", msg)
        self.assertIn("DOES NOT REACH", msg,
                      "the message does not say the re-run cannot reach this "
                      "surface: %r" % msg)
        self.assertNotIn("re-run `helm chat seat rename", msg,
                         "the dead retry advice came back: %r" % msg)
        # AND IT REPORTS BOTH HALVES OF THE SPLIT, which is what an operator
        # acts on: which surfaces committed, and which one did not.
        self.assertIn("ARE committed", msg, msg)
        self.assertIn(self.NEW, msg, msg)
        self.assertIn(self.OLD, msg, msg)

    def test_the_rename_writes_identity_and_touches_nothing_else(self):
        d = self._register()
        # POSITIVE CONTROL ON THE INPUT: the walk reads the tree's own
        # enumerator, and what it finds declares the OLD name before the verb.
        self.assertEqual(seatmod.registered_seats(), ([self.OLD], False))
        before = seatmod._spawn_record(d)
        self.assertEqual(seat_runtime._record_identity(before), self.OLD)
        msg = self._rename()
        self.assertIn("Re-pointed the spawn register KEYED %s" % self.OLD, msg)
        after = seatmod._spawn_record(d)
        # THE KEY STAYS, THE IDENTITY MOVES, AND NOTHING ELSE CHANGES — one
        # equality rather than a field list, so a field that APPEARS is caught
        # as loudly as one that is overwritten.
        self.assertEqual(after, dict(before, identity=self.NEW))
        self.assertEqual(after["seat"], self.OLD, "the register was re-KEYED")
        self.assertEqual(seat_runtime._record_identity(after), self.NEW)
        self.assertEqual(seatmod.registered_seats(), ([self.OLD], False))

    def test_a_seat_with_no_register_is_a_clean_no_op(self):
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        self.assertEqual(seatmod.registered_seats(), ([], False))
        ok, msg = seats.rename_seat(self.PEER, "peer-2")
        self.assertTrue(ok, msg)     # a missing register is not a failure
        self.assertIn("no spawn register declares %s" % self.PEER, msg)
        self.assertIn("peer-2", seats.roster())
        self.assertNotIn(self.PEER, seats.roster())

    def test_two_registers_claiming_one_identity_refuse_rather_than_guess(self):  # noqa: VACUOUS_ASSERTION — the contract IS that NEITHER register was written, and its unconditional positive control is the refusal MESSAGE asserted on the same call plus the sibling arm above writing exactly one register through the same door
        d_nine = self._register()
        # Same reason as OLD: a storage key that resolves no family is skipped
        # as unreadable, so a second CLAIMANT has to be family-shaped to be a
        # claimant at all.
        d_eight = self._register(storage="codex-98", identity=self.OLD)
        msg = self._rename()
        self.assertIn("2 spawn registers declare %s" % self.OLD, msg)
        self.assertIn("refusing to guess", msg)
        # NEITHER was written: an ambiguous answer writes nothing at all.
        self.assertNotIn("identity", seatmod._spawn_record(d_nine))
        self.assertEqual(seatmod._spawn_record(d_eight)["identity"], self.OLD)
        self.assertIn(self.NEW, seats.roster())   # the rename still HAPPENED

    def test_the_rebind_sweeps_declaration_joins_the_renamed_row(self):  # noqa: VACUOUS_ASSERTION — the contract IS the ABSENCE of a second roster key, and its unconditional positive control is last_seen advancing on the same call, which proves the write ran rather than silently no-opping
        """The whole chain, driven through the producers the timer drives."""
        d = self._register()
        self._rename()
        seen_before = seats.roster()[self.NEW]["last_seen"]
        keys_before = sorted(seats.roster())
        # EXACTLY WHAT THE TIMER HANDS THE MINT DOOR: `rebind_seat` ->
        # `bind_runtime` -> `_bind_runtime_session(_record_identity(rec), ...)`
        # -> `write_roster`. The argument is computed here the same way.
        declared = seat_runtime._record_identity(seatmod._spawn_record(d))
        self.assertEqual(declared, self.NEW, "half one: the register moved")
        key, _row, admits = seats.write_roster(
            declared, presence_beat=False, keyed=True, admission=True)
        self.assertEqual((key, admits), (self.NEW, False))
        # THE ALIAS SURVIVES THE SWEEP'S OWN DECLARATION — the half that
        # actually broke. The admission calls `retire_alias_claims`, and the
        # alias covering THIS rename window must outlive the timer's write.
        row = seats.roster()[self.NEW]
        self.assertEqual(row[seats.RENAME_ALIAS_FIELD]["old"], self.OLD)
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW)
        # AND THE MINT DOOR STAYS DUMB, DELIBERATELY. A first cut resolved a
        # live rename hop here so any stored spelling of the old name would
        # join the renamed row — and its discriminator read a MISSING SESSION
        # as "this row describing itself", which is also what a real
        # `chat join --seat OLD` looks like. A released name re-admits as
        # ITSELF now; reaching the renamed row is the register's job, proven
        # above, not this writer's guess.
        key, _row, admits = seats.write_roster(
            self.OLD, presence_beat=False, keyed=True, admission=True)
        self.assertEqual((key, admits), (self.OLD, True),
                         "the mint door resolved a name it was given no proof "
                         "about, which is the defect F2 named")
        # ONE ROW FROM THE SWEEP'S OWN DECLARATION. This is the observable
        # the owner saw: the timer's write lands on the renamed row and mints
        # nothing, because the REGISTER declares the new name.
        self.assertEqual(sorted(k for k in seats.roster()
                                if k in keys_before), keys_before)
        # CONTROL THAT THE WRITE ACTUALLY RAN: a resolution that silently did
        # nothing would satisfy every assertion above.
        self.assertGreater(seats.roster()[self.NEW]["last_seen"], seen_before)
        # AND A DELIBERATE RE-ADMISSION OF THE OLD NAME RETIRES THAT ALIAS,
        # which is the tree's settled ruling and not a regression: a released
        # name taken by a new generation ends the older one's claim. The
        # defect was never that an admission retires — it was that the TIMER
        # produced one, which it no longer does.
        self.assertEqual(seats.live_alias(self.OLD), (None, None))

    def test_a_fresh_joiner_still_admits_and_still_retires_the_stale_claim(self):
        """THE CONTROL WITHOUT WHICH THE CURE IS A BLANKET REFUSAL.

        A RELEASED NAME IS LAWFULLY RE-ADMITTED INSIDE AN OPEN WINDOW and the
        tree decided that long before this lane (CL99, task/2338):
        a fresh session joins under the old name, gets its OWN row, and the
        admission retires the older generation's claim. An unconditional alias
        resolution in `write_roster` reddened four arms holding that law, and
        they were right — so the discriminator is a session the renamed row
        does not know.

        AND THIS DOOR NO LONGER DISCRIMINATES AT ALL, which is the point:
        every spelling of a released name admits as ITSELF here, whether the
        caller brings the renamed row's own session or a stranger's. Reaching
        the renamed row is the register's job, proven in the sibling arm.
        A first cut tried to tell the two apart from a name plus a
        maybe-session and got it backwards — a MISSING session read as "the
        row describing itself", which is also what a real `chat join --seat
        OLD` looks like."""
        self._register()
        self._rename(alias_hours=1)
        known = seats.roster()[self.NEW]["session"]
        self.assertEqual(known, "s-old")     # the row's own session, carried
        # A STRANGER'S SESSION IS A NEW AGENT: it admits, takes its own row,
        # and retires the older generation's claim in the same publication.
        key, _row, admits = seats.write_roster(
            self.OLD, session="s-fresh", cwd="/tmp/fresh",
            keyed=True, admission=True)
        self.assertEqual((key, admits), (self.OLD, True))
        # AND THE RENAMED ROW'S OWN SESSION BUYS NOTHING HERE EITHER. Spelled
        # with the old name it lands on the row the stranger just took —
        # `admits` is False only because that row now EXISTS, never because
        # this door resolved anything — and it does not reach the new name.
        key, _row, admits = seats.write_roster(
            self.OLD, session=known, cwd="/tmp/p", keyed=True, admission=True)
        self.assertEqual((key, admits), (self.OLD, False),
                         "the mint door resolved a name on evidence it is not "
                         "the place to weigh")
        rows = seats.roster()
        self.assertIn(self.OLD, rows)        # a real re-admission, not stranded
        self.assertIn(self.NEW, rows)
        self.assertEqual(rows[self.OLD]["session"], "s-fresh")
        self.assertNotIn(seats.RENAME_ALIAS_FIELD, rows[self.NEW],
                         "the stale claim was not retired")
        self.assertEqual(seats.live_alias(self.OLD), (None, None))

    def test_a_stale_lifecycle_rebind_refuses_before_it_writes_anything(self):  # noqa: VACUOUS_ASSERTION — the contract IS that nothing was written, and its positive control cannot be unconditional in-arm because the refusal is what stops the write; the control is test_the_ordinary_lifecycle_rebind_still_binds, which drives THIS producer on THIS fixture to a roster write and a bound session
        """THE REFUSAL ARRIVES BEFORE THE MUTATION IT EXISTS TO PREVENT.

        Admitting first hands a stale name to the door that MINTS: the bare
        row appears, the alias covering the rename window is retired on the
        way out, and only then does the binder ask whether this session is
        already another row's. A refusal that lands after its own mutation is
        not a refusal.

        THIS IS THE POST-COMMIT REGISTER FAILURE DRIVEN END TO END: the rename
        happened, the register still declares the OLD name, and the rebind
        sweep hands that name to the lifecycle boundary.
        """
        self._register()
        self._rename(alias_hours=1)
        sid = seats.roster()[self.NEW]["session"]
        keys_before = sorted(seats.roster())
        err = seat_runtime._bind_runtime_session(
            self.OLD, sid, storage_seat=self.OLD)
        self.assertIsNotNone(err, "the stale rebind was admitted")
        self.assertIn("is already current for %s" % self.NEW, err)
        # THE TWO OBSERVATIONS THAT MAKE IT REFUSAL-BEFORE-MUTATION rather
        # than refusal-after-one: no second row, and the window intact.
        self.assertEqual(sorted(seats.roster()), keys_before,
                         "the refusal still minted a row")
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW,
                         "the refusal still retired the rename alias")

    def test_a_live_alias_is_refused_at_the_lifecycle_boundary_too(self):  # noqa: VACUOUS_ASSERTION — same contract and same control as the sibling above: absence of a second key is the assertion, and test_the_ordinary_lifecycle_rebind_still_binds proves this producer writes on this fixture when it is not refused
        """THE SECOND MECHANISM, WHICH THE OWNER-SESSION CHECK CANNOT SEE.

        A session nobody currently holds passes the owner check outright, so
        without this the old name is admitted on a brand-new sid and the mint
        lands exactly as before. The name is a live alias for another row;
        that is the fact that refuses, and it is weighed here rather than at
        `write_roster`, which says in its own admission comment that the proof
        belongs at this boundary.
        """
        self._register()
        self._rename(alias_hours=1)
        keys_before = sorted(seats.roster())
        err = seat_runtime._bind_runtime_session(
            self.OLD, "s-nobody-holds-this", storage_seat=self.OLD)
        self.assertIsNotNone(err, "a live alias was admitted on a fresh sid")
        self.assertIn("live rename alias for %s" % self.NEW, err)
        self.assertEqual(sorted(seats.roster()), keys_before)
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW)

    def test_the_ordinary_lifecycle_rebind_WRITES_what_it_is_asked_for(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the two FIXTURE premises (this session is unknown to the row, and no runtime evidence exists for it); they are must-hits guarding the positives below, every one of which is an unconditional assertion on state this call had to write
        """THE CONTROL WITHOUT WHICH BOTH REFUSALS ABOVE ARE A BLANKET — and
        it asserts what THIS CALL WRITES, never what the fixture already wrote.

        An arm that binds the session a row already carries and then asserts
        that session, the rename record and the live alias asserts three
        values `_rename` wrote BEFORE the call, so it passes over a writer
        that does nothing at all. This one starts from a session the row has
        NEVER seen and from ABSENT runtime evidence for it, and asserts the
        lifecycle provenance the bind newly writes.
        """
        self._register()
        self._rename(alias_hours=1)
        fresh = "s-fresh-lifecycle"
        row = seats.roster()[self.NEW]
        self.assertNotIn(fresh, row.get("sessions") or [],
                         "fixture: the row already knows this session, so the "
                         "bind below would not be writing anything new")
        self.assertIsNone(seats.runtime_entry_for_session(row, fresh),
                          "fixture: runtime evidence for this session already "
                          "exists, so its presence afterwards proves nothing")
        self.assertNotEqual(row["last_seen"], self.STAMP,
                            "fixture: the row already carries the stamp, so "
                            "the oracle below cannot tell a write from a "
                            "no-op")
        with mock.patch.object(seatmod_roster.time, "time",
                               return_value=self.STAMP):
            err = seat_runtime._bind_runtime_session(
                self.NEW, fresh, storage_seat=self.OLD)
        self.assertIsNone(err, err)
        # THE PROVENANCE THIS CALL WROTE, which nothing in the fixture could
        # have supplied: an exact-session runtime entry stamped by the
        # lifecycle owner rather than by proxywatch measurement.
        row = seats.roster()[self.NEW]
        entry = seats.runtime_entry_for_session(row, fresh)
        self.assertIsInstance(entry, dict, row.get("runtime_sessions"))
        self.assertEqual(entry.get("source"), "lifecycle")
        self.assertIs(entry.get("verified"), True)
        self.assertEqual(row["session"], fresh)
        # A SEPARATE ORACLE FOR THE ROSTER REFRESH, because the provenance
        # above is the BINDER's write and this is the ADMISSION's: folding
        # them into one assertion cannot tell which of the two ran.
        #
        # AND IT IS AN EXACT VALUE, NEVER `>=`. A greater-or-equal permits an
        # UNCHANGED last_seen, so the arm passes over a boundary that skips
        # the metadata-free write entirely — the binder never stamps it, so
        # nothing else would fail. The clock is controlled and the stamp is
        # asserted to the value only that write can have left.
        self.assertEqual(row["last_seen"], self.STAMP,
                         "the admission's roster refresh did not run")
        # AND THE WINDOW IS UNTOUCHED — the ordinary path retires nothing.
        self.assertEqual(row[seats.RENAME_ALIAS_FIELD]["old"], self.OLD)
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW)

    def test_a_RENAME_BETWEEN_THE_PROOF_AND_THE_WRITE_MINTS_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the old name NOT appearing as a roster key; its unconditional positive control is test_a_FIRST_bind_with_no_row_at_all_still_mints, which drives THIS producer on THIS fixture and does mint, plus the alias and key-set equalities asserted on the same call
        """THE INTERLEAVING NO PREFLIGHT OF A NAME COULD SEE.

        A rename PUBLISHES the roster and RELEASES the roster lock before it
        ever reaches the spawn lock this caller holds, so the row can vanish
        after the proof and before the write with nothing in between to
        observe it. An admission that mints there puts a bare second row on
        the roster for one identity and retires the alias still addressing the
        real one, and the binder's refusal arrives third.

        THE FENCE IS THE GENERATION, NOT THE NAME. The proof reads the row's
        own `incarnation`; a row that was THERE must still be there when the
        write lands, so the write declines to create and nothing is written.
        Driven through the shipped writer with the real rename verb running
        inside the window, not simulated by hand.
        """
        self._register()
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        keys_before = sorted(seats.roster())
        real_write = seats.write_roster
        landed = {}

        def rename_then_write(*a, **kw):
            if not landed:
                ok, msg = seats.rename_seat(self.OLD, self.NEW, alias_hours=1)
                landed["ok"] = (ok, msg)
            return real_write(*a, **kw)

        with mock.patch.object(seats, "write_roster", rename_then_write):
            err = seat_runtime._bind_runtime_session(
                self.OLD, "s-old", storage_seat=self.OLD)
        self.assertTrue(landed.get("ok", (False, "never ran"))[0],
                        "fixture: the rename did not land inside the window, "
                        "so this arm models nothing: %r" % (landed,))
        self.assertIsNotNone(err, "the vanished row was bound anyway")
        rows = seats.roster()
        self.assertNotIn(self.OLD, rows, "the write minted the renamed name")
        self.assertIn(self.NEW, rows)
        self.assertEqual(rows[self.NEW][seats.RENAME_ALIAS_FIELD]["old"],
                         self.OLD, "the alias covering the window was retired")
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW)
        self.assertEqual(sorted(rows),
                         sorted(set(keys_before) - {self.OLD} | {self.NEW}))

    def test_a_ROW_THAT_APPEARS_IN_THE_WINDOW_refuses_the_FIRST_write(self):
        """THE MUTATION THE EARLIER FENCE LEFT UNGUARDED.

        Fencing only the LATER writer left the FIRST one open: a proof that
        found nothing handed the mint door no expectation at all, so a
        concurrent admission installing a row with its OWN current session
        was refreshed on a proof about a world that no longer existed, and
        the binder then overwrote that session with the incoming one.

        The proof now says ABSENT, which is a claim the write can be held to.
        """
        self._register()
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        self.assertNotIn(self.OLD, seats.roster())
        real_write = seats.write_roster
        landed = {}

        def join_then_write(*a, **kw):
            if not landed:
                # A LAWFUL ADMISSION BY SOMEBODY ELSE, through the real door.
                landed["row"] = real_write(self.OLD, session="s-someone-else",
                                           cwd="/tmp/other")
            return real_write(*a, **kw)

        with mock.patch.object(seats, "write_roster", join_then_write):
            err = seat_runtime._bind_runtime_session(
                self.OLD, "s-first-bind", storage_seat=self.OLD)
        self.assertTrue(landed, "fixture: nothing landed in the window")
        self.assertIsNotNone(err, "the first write ran on a stale proof")
        self.assertIn("not the row this rebind proved", err)
        # THE OTHER SEAT'S SESSION SURVIVED, which is the harm: the binder
        # would have overwritten it with the incoming one.
        self.assertEqual(seats.roster()[self.OLD]["session"],
                         "s-someone-else")

    def test_a_LEGACY_row_with_no_marker_is_REFUSED_not_fenced(self):
        """A MARKERLESS ROW IS A MIGRATION FAULT, NOT A FENCE STATE.

        A sentinel meaning "some row with no generation" fences a CLASS, and
        every markerless row satisfies it — so a real rename can carry a
        DIFFERENT markerless row into this key between the proof and the
        admission and pass. There is no value the proof could return that
        NAMES such a row, so it refuses and names the migration; the refusal
        is a door somebody can walk through, which a silent fence hole is not.
        """
        self._register()
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        rows = seats.roster()
        rows[self.OLD].pop("incarnation", None)      # the legacy shape
        pk.write_json(seats.roster_path(), rows)
        self.assertNotIn("incarnation", seats.roster()[self.OLD])

        expect, err = seats.lifecycle_bind_refusal(self.OLD, "s-old")
        self.assertIsNone(expect)
        self.assertIn("predates the identity generation marker", err)
        self.assertIn(seatmod_roster.INCARNATION_MIGRATION, err,
                      "the refusal named no cure, so nobody can clear it")

        # THE MIGRATION IS THAT CURE, AND IT IS THE POSITIVE CONTROL: after it
        # the SAME proof on the SAME row answers a generation string.
        stamped, _marked, _un = seatmod_roster.migrate_incarnations(apply=True)
        self.assertIn(self.OLD, stamped)
        expect, err = seats.lifecycle_bind_refusal(self.OLD, "s-old")
        self.assertIsNone(err, err)
        self.assertIsInstance(expect, str)
        self.assertEqual(expect, seats.roster()[self.OLD]["incarnation"])

        # AND IT IS IDEMPOTENT — a second run stamps nothing.
        again, _marked, _un = seatmod_roster.migrate_incarnations(apply=True)
        self.assertEqual(again, [])
        self.assertEqual(seats.roster()[self.OLD]["incarnation"], expect)

    def test_the_migration_is_DRY_by_default(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn("incarnation", ...); its unconditional positive control is the last block, where the SAME call with apply=True on the SAME row writes the marker
        """A ROSTER REWRITE LEAVES NOTHING TO INSPECT AFTERWARDS, so the
        default run reports what it would stamp and changes nothing."""
        self._register()
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        rows = seats.roster()
        rows[self.OLD].pop("incarnation", None)
        pk.write_json(seats.roster_path(), rows)
        would, _marked, _un = seatmod_roster.migrate_incarnations()
        self.assertIn(self.OLD, would)
        self.assertNotIn("incarnation", seats.roster()[self.OLD],
                         "the dry run wrote")
        # THE UNCONDITIONAL POSITIVE on the same observable: the SAME call
        # with apply=True stamps that exact row, so the absence above is
        # about the DRY-RUN and not about a call that can never write.
        seatmod_roster.migrate_incarnations(apply=True)
        self.assertIsInstance(seats.roster()[self.OLD]["incarnation"], str)

    def test_the_advertised_migration_is_a_command_that_APPLIES(self):
        """A REFUSAL THAT NAMES A DRY COMMAND SENDS THE READER ROUND THE LOOP.

        `seat rebind --all` is dry by DEFAULT, so a lifecycle refusal naming
        it told the operator to run something that changes nothing and then
        refused them again. The literal carries --apply, and the preview is a
        separate deliberate ask."""
        self.assertIn("--apply", seatmod_roster.INCARNATION_MIGRATION)
        # THE MUST-HIT: the advertised literal is not merely well-worded, it
        # is the flag the CLI actually reads to write.
        self.assertIn("--apply", seatmod_roster.INCARNATION_MIGRATION.split())

    def test_the_dry_preview_does_not_MOVE_an_unparseable_roster(self):
        """A PREVIEW THAT MUTATES HAS DONE THE ONE THING IT PROMISED NOT TO.

        `roster_for_write` is a WRITE-PATH accessor: on a roster it cannot
        parse it performs corruption recovery, os.replace-ing the canonical
        bytes aside so the imminent write cannot delete the only copy. Reached
        from a DRY run that was only supposed to look, it relocates the
        canonical roster and reports nothing to stamp while doing it."""
        self._register()
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        before = open(path, "rb").read()

        would, _marked, unread = seatmod_roster.migrate_incarnations()
        # THE PATH AND THE BYTES, not merely "no incarnation appeared": an
        # absence-of-marker oracle passes over a cure that moved the file.
        self.assertTrue(os.path.exists(path), "the dry run MOVED the roster")
        self.assertEqual(open(path, "rb").read(), before,
                         "the dry run rewrote the canonical bytes")
        self.assertEqual(would, [])
        # AND UNREADABLE IS ITS OWN ANSWER — reporting [] with no reason is
        # indistinguishable from a fully migrated fleet.
        self.assertIn("UNKNOWN", unread)
        self.assertFalse(any(f.startswith(os.path.basename(path) +
                                          ".unreadable")
                             for f in os.listdir(os.path.dirname(path))),
                         "the dry run ran the write path's corruption "
                         "recovery")

    def test_a_MARKED_row_whose_session_was_disowned_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the (key, None, False) refusal tuple; its unconditional positive control is the last block of this same method, where the SAME door with the SAME generation and a lifecycle sid nobody else owns returns a real row dict. The rung credits a control only from the same CALL's other channel, and a refusal tuple has no second channel, so this one has to be a second call
        """A GENERATION MATCH PROVES WHICH ROW, NOT WHOSE SESSION IT IS.

        A disown hands the current session to another row and leaves this
        row's generation intact, so the marked branch matched, refreshed,
        projected and published — and only the binder afterwards discovered
        another row is the owner."""
        # AT LEAST EIGHT CHARACTERS: disown_session refuses a shorter prefix
        # because it could disown the wrong session, so the fixture has to be
        # a sid the real producer accepts.
        OWNED = "s-owned-by-old-1111"
        self._register()
        seats.join(session=OWNED, seat=self.OLD, cwd="/tmp/p")
        # THE RECIPIENT MUST HAVE NO CURRENT SESSION. disown --to transfers
        # addressing history always but sets the recipient's CURRENT session
        # only if it is absent, so a peer that already holds one blocks the
        # handoff and this arm would measure a fixture, not a fence. The
        # must-hit below is what caught that on the first run.
        seats.write_roster(self.PEER, presence_beat=False)
        self.assertIsNone(seats.roster()[self.PEER].get("session"))
        expect, err = seats.lifecycle_bind_refusal(self.OLD, OWNED)
        self.assertIsNone(err, err)
        self.assertIsInstance(expect, str)

        # THE WINDOW: the session moves to another row; the generation does
        # not move with it.
        ok, why = seatmod_roster.disown_session(self.OLD, OWNED,
                                                to=self.PEER)
        self.assertTrue(ok, why)
        self.assertEqual(seats.roster()[self.OLD]["incarnation"], expect,
                         "fixture: the generation moved, so this arm is no "
                         "longer about a MATCHING generation")
        self.assertEqual(seats.roster()[self.PEER]["session"], OWNED,
                         "fixture: the session did not move, so there is no "
                         "other owner to refuse on")

        key, row, admits = seats.write_roster(
            self.OLD, presence_beat=False, keyed=True, admission=True,
            expect=expect, lifecycle_sid=OWNED)
        self.assertEqual((key, row, admits), (self.OLD, None, False))
        # THE UNCONDITIONAL POSITIVE on the same door and the same generation:
        # a lifecycle sid nobody else owns still binds, so this is not a
        # blanket refusal of the marked branch.
        key, row, _admits = seats.write_roster(
            self.OLD, presence_beat=False, keyed=True, admission=True,
            expect=expect, lifecycle_sid="s-nobody-owns-this")
        self.assertEqual(key, self.OLD)
        self.assertIsInstance(row, dict)

    def test_an_ABSENCE_that_a_rename_refilled_is_REFUSED(self):
        """ABSENCE HAS NO IDENTITY, SO THE KEY CHECK ALONE IS NOT THE PROOF.

        absent -> admit -> rename leaves the key absent AGAIN, and "still no
        key here" is true of that second absence exactly as it was of the
        first. What differs is the LIFECYCLE: the name now carries a live
        rename alias addressing the row that moved. Only the binder caught
        this, after the mint, the alias retire and the publication.
        """
        self._register()
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        self.assertNotIn(self.NEW, seats.roster())
        expect, err = seats.lifecycle_bind_refusal(self.NEW, "s-fresh")
        self.assertIsNone(err, err)
        self.assertIs(expect, seats.ABSENT)

        # THE WINDOW: the proved-free name becomes a live alias for a row that
        # moved into it and out again.
        ok, why = seats.rename_seat(self.OLD, self.NEW)
        self.assertTrue(ok, why)
        ok, why = seats.rename_seat(self.NEW, self.OLD)
        self.assertTrue(ok, why)
        self.assertNotIn(self.NEW, seats.roster(),
                         "fixture: the key is not absent again, so this arm "
                         "is not about a refilled absence")

        key, row, admits = seats.write_roster(
            self.NEW, session="s-fresh", presence_beat=False, keyed=True,
            admission=True, expect=expect)
        self.assertEqual((key, row, admits), (self.NEW, None, False))
        # THE UNCONDITIONAL POSITIVE: a name with no alias and no other owner
        # still admits on the same proof, so this is not a blanket refusal.
        key, row, _admits = seats.write_roster(
            self.PEER, session="s-peer2", presence_beat=False, keyed=True,
            admission=True, expect=seats.ABSENT)
        self.assertEqual(key, self.PEER)
        self.assertIsInstance(row, dict)

    def test_a_FIRST_bind_with_no_row_at_all_still_mints(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the fixture premise that the old name has no row yet; the assertions after it are unconditional positives on the row and the runtime entry this call created
        """THE OTHER LAWFUL CASE, and the control that keeps the fence from
        becoming a blanket refusal.

        A seat whose first bind has no row to refresh is this path WORKING,
        not the defect: the proof finds nothing, holds the writer to no
        generation, and the write mints. Without this arm the fence above is
        satisfied by a boundary that can no longer admit anything at all.
        """
        self._register()
        seats.join(session="s-peer", seat=self.PEER, cwd="/tmp/p")
        self.assertNotIn(self.OLD, seats.roster())
        err = seat_runtime._bind_runtime_session(
            self.OLD, "s-first-bind", storage_seat=self.OLD)
        self.assertIsNone(err, err)
        row = seats.roster()[self.OLD]
        self.assertEqual(row["session"], "s-first-bind")
        entry = seats.runtime_entry_for_session(row, "s-first-bind")
        self.assertEqual((entry or {}).get("source"), "lifecycle")

    def test_the_renamed_rows_own_session_still_does_not_reach_the_new_name(self):
        """THE SELECTIVE KNOWN-SID MUTANT DIES HERE, AND ONLY ON ITS OWN WORLD.

        A door that resolved the alias only when the caller brings the session
        the renamed row carries SURVIVES an arm that runs after the old name
        has already been re-admitted: there the key comes back OLD because
        that row EXISTS, so the assertion is satisfied by the fixture rather
        than by the door. This arm therefore buys its own world — OLD is
        ABSENT, the alias window is OPEN, and the session presented is exactly
        the one the renamed row carries. A door weighing that evidence would
        answer NEW and mint nothing; the contract is that it answers OLD and
        mints, because reaching the renamed row is the register's job.
        """
        self._register()
        self._rename(alias_hours=1)
        known = seats.roster()[self.NEW]["session"]
        self.assertEqual(known, "s-old")      # the renamed row's own session
        # THE TWO FIXTURE PREMISES, ASSERTED RATHER THAN ASSUMED: without an
        # absent OLD and an open window the arm below proves nothing.
        self.assertNotIn(self.OLD, seats.roster(),
                         "the rename left the old name behind, so this arm "
                         "would be measuring an existing row")
        self.assertEqual(seats.live_alias(self.OLD)[0], self.NEW,
                         "the alias window is shut, so a refusal to resolve "
                         "would be explained by expiry and not by the door")
        key, _row, admits = seats.write_roster(
            self.OLD, session=known, cwd="/tmp/p", keyed=True, admission=True)
        self.assertEqual((key, admits), (self.OLD, True),
                         "the mint door resolved a name on evidence it is not "
                         "the place to weigh")
        rows = seats.roster()
        self.assertIn(self.OLD, rows, "the admission minted nothing")
        # AND THE SESSION STAYED WHERE IT WAS. write_roster drops a sid that
        # is another row's CURRENT session, so the minted row carries none —
        # which is the second half of the same law: this door neither resolves
        # the name nor steals the session it was shown.
        self.assertNotIn("session", rows[self.OLD])
        self.assertEqual(rows[self.NEW]["session"], known)

    def test_the_dry_run_names_the_register_and_writes_none_of_it(self):  # noqa: VACUOUS_ASSERTION — the contract IS that the dry run wrote nothing, and its unconditional positive control is the surface LINE asserted on the same call plus the sibling arm applying the same rename and finding the field written
        d = self._register()
        seats.join(session="s-old", seat=self.OLD, cwd="/tmp/p")
        before = seatmod._spawn_record(d)
        ok, msg = seats.rename_seat(self.OLD, self.NEW, dry_run=True)
        self.assertTrue(ok, msg)
        self.assertIn("  6 spawn register: the register KEYED %s declares %s "
                      "and would carry identity=%s"
                      % (self.OLD, self.OLD, self.NEW), msg)
        self.assertEqual(seatmod._spawn_record(d), before,
                         "the dry run wrote the register it only described")
        # CONTROL: the real verb through the same door DOES write it, so the
        # equality above is a measured absence and not an inert probe.
        ok, msg = seats.rename_seat(self.OLD, self.NEW)
        self.assertTrue(ok, msg)
        self.assertEqual(seatmod._spawn_record(d)["identity"], self.NEW)
