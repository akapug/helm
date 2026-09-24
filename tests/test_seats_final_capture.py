#!/usr/bin/env python3
"""Which capture is the LAST WORD before the act, and who pays for the gap?

ONE FINAL CAPTURE, THEN THE ACT, THEN THE ACCOUNTING — driven through the
shipped producers rather than a fixture that invents its own world:
`seats.join`, `chat.post`, `seats.deliver`, `beacons.install_repair`, and a
real proxywatch state file. An authorization that moves INSIDE the final
capture restarts the door and refuses. One that moves AFTER the capture and
before the keystroke CANNOT be forbidden — the keystroke is external — so the
act happens, is ACCOUNTED obsolete-authorization, the attempt is charged, and
a repeat of that act for that attempt is refused. A stable world acts once and
records a clean receipt with its measured window.

MOVED WHOLE OUT OF `tests/test_seats.py`, which stood 56,594 bytes under the
1 MiB never-track ceiling with arms still landing in it. This class alone was
85,102 of those bytes. No body was rewritten on the way; both the composer and
the case are the byte-identical text they had there.

`_ActPane` TRAVELS WITH ITS ONLY CALLER, which is why it is here rather than
imported back: it is used by this class and by nothing else in the tree —
measured, zero spellings of the name outside the file it left. Leaving it
behind would have made an import edge out of a private helper with one
consumer.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `SeatsBase` is imported from
the module these arms came from, so one fixture serves both files and the two
cannot drift.
"""
import contextlib
import os
import threading
import time
from unittest import mock

from tests.test_seats import SeatsBase

from helm import beacons, chat, pk, proxywatch, record, seats

class _ActPane(object):
    """A composer that keeps what is typed into it, read and written through
    the REAL inherited act doors. `on_read` and `on_send` are hooks a caller
    uses to make the world move at a named instant."""

    def __new__(cls):
        from helm import harness

        class Pane(harness._CLIAdapter):
            name = "fake"

            def __init__(self):
                self.sent, self.composer = [], ""
                self.on_read, self.on_send = None, None

            def read(self, handle, limit=3000, timeout=60):
                if self.on_read is not None:
                    self.on_read(self)
                return "\n".join(("─" * 40, "❯\xa0" + self.composer,
                                  "─" * 40, "  opus-5 | ~/dev/repo"))

            def send(self, handle, text, enter=True):
                if self.on_send is not None:
                    self.on_send(self, text, enter)
                self.sent.append((text, enter))
                if not (enter and self.swallow_enter):
                    self.composer = "" if enter else self.composer + text
        pane = Pane()
        # An Enter the TUI never ingests: the composer keeps Helm's text.
        pane.swallow_enter = False
        return pane


class TheFinalCaptureIsTheLastWordTest(SeatsBase):
    """ONE FINAL CAPTURE, THEN THE ACT, THEN THE ACCOUNTING -- driven through
    the shipped producers: `seats.join`, `chat.post`, `seats.deliver`, the
    repair episode's own `beacons.install_repair`, and a proxywatch state file.

    An authorization that moves INSIDE the final capture restarts the door
    and refuses. One that moves AFTER the capture and before the keystroke
    cannot be forbidden -- the keystroke is external -- so the act happens and
    is ACCOUNTED obsolete-authorization, the attempt is charged, and a repeat
    of the act for that attempt is refused. A stable world acts once and
    records a clean receipt with its measured window."""

    SEAT, SID = "codex-41", "s-final"

    def setUp(self):
        super().setUp()
        from helm import harness, resumeturn
        resumeturn._OBSOLETE_HERE.clear()
        # `submit` re-reads the composer SUBMIT_VERIFY_READS times; `_ActPane`
        # answers per read and never consults the clock, so every read still
        # runs and only the 0.8s sleeps between them go.
        gap = mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0)
        gap.start()
        self.addCleanup(gap.stop)
        seats.join(session=self.SID, seat=self.SEAT, cwd="/tmp/p")
        # The family has been healthy for a minute, as a watcher records it.
        self.health("HEALTHY", since=proxywatch._iso(time.time() - 60))

    def health(self, state, family="codex", since=None):
        """A watcher pass, COMPOSED by the watcher's own
        `_compose_upstream_records` from the prior records: every pass stamps
        a new `ts`; `since` is kept while the family's state is unchanged and
        re-stamped when it changes; and the record's transition identity is
        the producer's, kept across an unchanged pass and minted on a
        change."""
        path = proxywatch._state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        previous = (pk.read_json(path, {}) or {}).get("upstream") or {}
        prior = previous.get(family) or {}
        if since is None:
            since = prior.get("since") if prior.get("state") == state \
                and prior.get("since") else proxywatch._iso(time.time())
        records, _err = proxywatch._compose_upstream_records(
            {"upstream": {family: {"state": state, "dark": state != "HEALTHY",
                                   "since": since}}}, previous)
        pk.write_json(path, {"ts": time.time(), "upstream": records})

    def mint(self, seat=None, since=None, born=None):
        from helm import beacons
        seat = seat or self.SEAT
        since = time.time() - 5 if since is None else since
        rows = pk.read_json(seats.roster_path(), {}) or {}
        att = {"state": beacons.DEAF_IN_EFFECT, "alarm": True,
               "since": since, "at": since}
        rows[seat]["attendance"] = att
        pk.write_json(seats.roster_path(), rows)
        attempt = beacons.install_repair(
            seat, att, time.time() if born is None else born)
        self.assertIsNotNone(attempt, "fixture: the episode did not install")
        return att, attempt

    def key(self, seat=None, sid=None):
        return "deaf:%s:%s" % (seat or self.SEAT, sid or self.SID)

    def admit(self, attempt, seat=None, sid=None):
        from helm import resumeturn
        return lambda door: resumeturn._prepare_due(
            seat or self.SEAT, sid or self.SID, room="main", door=door,
            attempt=attempt, key=self.key(seat, sid))

    def entry(self):
        from helm import resumeturn
        return resumeturn._peek(self.key()) or {}

    def test_a_stable_world_acts_once_and_records_clean_receipts(self):  # noqa: VACUOUS_ASSERTION — the acts list is asserted equal to two named clean receipts, and the sent list to one placement and one Enter
        from helm import harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(attempt))
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertEqual(pane.sent, [("GO", False), ("", True)])
        acts = self.entry().get("acts") or []
        self.assertEqual([(a["door"], a["outcome"], a["attempt"]) for a in acts],
                         [("placement", harness.ACT_CLEAN, attempt),
                          ("enter", harness.ACT_CLEAN, attempt)],
                         "a stable act was not recorded clean")
        for act in acts:
            self.assertLess(act["capture_to_send_start_s"], act["deadline_s"])
            self.assertGreaterEqual(act["capture_to_send_return_s"],
                                    act["capture_to_send_start_s"])
            self.assertGreaterEqual(
                act["capture_to_send_start_s"],
                act["capture_dependencies_s"] + act["capture_composer_s"]
                - 1e-6)
        self.assertIn("authorization clean", detail)
        self.assertFalse(self.entry().get("obsolete"),
                         "a stable act was accounted obsolete")

    def test_a_pause_that_lands_inside_the_final_capture_refuses(self):
        from helm import harness, seats_stop_fp
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        real, landed = seats_stop_fp.occurrence_owed, []

        def pause_inside(*a, **k):
            # The capture has read the pause verdict; the watcher records a
            # wall before the capture finishes.
            if not landed:
                landed.append(True)
                self.health("AUTH-401")
            return real(*a, **k)
        pane = _ActPane()
        with mock.patch.object(seats_stop_fp, "occurrence_owed",
                               side_effect=pause_inside):
            state, detail = pane.submit("h1", "GO", settle=0,
                                        admit=self.admit(attempt))
        self.assertEqual(landed, [True], "fixture: the final capture never ran")
        self.assertEqual(pane.sent, [],
                         "a pause that landed inside the final capture did "
                         "not stop the placement")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertEqual(getattr(detail, "kind", None), "paused", detail)

    def test_a_human_edit_during_the_dependency_reads_is_seen(self):
        from helm import harness, seats_stop_fp
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        real, typed = seats_stop_fp.occurrence_owed, []

        def type_inside(*a, **k):
            if not typed:
                typed.append(True)
                pane.composer += "a person's draft"
            return real(*a, **k)
        with mock.patch.object(seats_stop_fp, "occurrence_owed",
                               side_effect=type_inside):
            state, detail = pane.submit("h1", "GO", settle=0,
                                        admit=self.admit(attempt))
        self.assertEqual(typed, [True], "fixture: the final capture never ran")
        self.assertEqual(pane.sent, [],
                         "helm typed into a draft written during the final "
                         "capture's dependency reads")
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("may be a human draft", detail)

    def test_an_authorization_change_after_the_capture_is_accounted(self):  # noqa: VACUOUS_ASSERTION — the acts list is asserted equal to one obsolete placement, the receipts to contain the attempt, and the restored pass to DELIVERED with a clean Enter
        """The watcher records a wall AFTER the final capture and before the
        keystroke. The placement happens; it is accounted
        obsolete-authorization and charged; the Enter door prepares afresh and
        refuses on the pause; and a repeat of the placement's attempt is
        refused. Then the same with the wall RESTORED before the accounting
        read: the bytes read the same, the file was rewritten, and it is
        accounted all the same."""
        from helm import harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()

        def wall_at_the_keystroke(pane, text, enter):
            if not enter and not pane.sent:
                self.health("AUTH-401")
        pane = _ActPane()
        pane.on_send = wall_at_the_keystroke
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(attempt))
        self.assertEqual(pane.sent, [("GO", False)],
                         "fixture: the placement did not happen, or Enter was "
                         "pressed through the pause")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        entry = self.entry()
        acts = entry.get("acts") or []
        self.assertEqual([(a["door"], a["outcome"]) for a in acts],
                         [("placement", harness.OBSOLETE_AUTHORIZATION)],
                         "the obsolete act was not accounted: %r" % (acts,))
        self.assertIn("paused", acts[0]["what"])
        self.assertIn(attempt, entry.get("receipts") or {},
                      "the obsolete act was not charged")
        self.assertEqual(len(entry.get("at") or []), 1)
        for door in resumeturn.REPEAT_DOORS:
            again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                            door=door, attempt=attempt,
                                            key=self.key())
            self.assertEqual((again.ok, again.kind), (False, "obsolete"),
                             "a repeat of an obsolete act was not refused at "
                             "the %s door: %s" % (door, again.why))
        # THE WALL IS RESTORED before the accounting read. The family
        # recovered a while ago.
        self.health("HEALTHY", since=proxywatch._iso(time.time() - 30))
        _att, second = self.mint(since=time.time() - 2)
        self.assertNotEqual(second, attempt)

        def wall_and_back(pane, text, enter):
            if not enter and not pane.sent:
                self.health("AUTH-401")
                self.health("HEALTHY")
        pane = _ActPane()
        pane.on_send = wall_and_back
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(second))
        self.assertEqual(state, harness.DELIVERED, detail)
        acts = [a for a in self.entry().get("acts") or []
                if a.get("attempt") == second]
        self.assertEqual([(a["door"], a["outcome"]) for a in acts],
                         [("placement", harness.OBSOLETE_AUTHORIZATION),
                          ("enter", harness.ACT_CLEAN)],
                         "a changed-and-restored pause was not accounted, or "
                         "the untouched Enter was mislabeled: %r" % (acts,))
        self.assertIn("pause", acts[0]["what"])
        again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                        door="recovery-attempt",
                                        attempt=second, key=self.key())
        self.assertEqual(again.kind, "obsolete", again.why)

    def test_a_consumption_during_the_byte_read_is_inside_the_bracket(self):
        from helm import resumeturn, seats_stop_fp
        chat.post("@codex-41 an owed row", who="daria")
        grant = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                        door="enter")
        self.assertTrue(grant.ok, "fixture: %s" % grant.why)
        real, consumed = os.pread, []
        real_owed, verdicts = seats_stop_fp.occurrence_owed, []

        def consume_during_the_read(fd, n, off):
            if not consumed:
                consumed.append(None)
                consumed[0] = seats.deliver(session=self.SID, seat=self.SEAT)
            return real(fd, n, off)

        def owed(*a, **k):
            # The OCCURRENCE's own verdict, beside the capture's: the capture
            # also sees the cursor file rewritten, and that second guard must
            # not be what this arm measures.
            verdicts.append(real_owed(*a, **k))
            return verdicts[-1]
        with mock.patch.object(seats_stop_fp.os, "pread",
                               side_effect=consume_during_the_read), \
                mock.patch.object(seats_stop_fp, "occurrence_owed",
                                  side_effect=owed):
            valid, why = grant.still()
        self.assertIn("an owed row", str(consumed),
                      "fixture: the delivery inside the byte read consumed "
                      "nothing")
        self.assertEqual(len(verdicts), 1, "fixture: the occurrence was not "
                                           "validated once")
        self.assertIs(verdicts[0][0], False,
                      "a row consumed while its bytes were being read still "
                      "validated: %r" % (verdicts[0],))
        self.assertIn("cursor", verdicts[0][1])  # noqa: SEAT_NAME — a chat CURSOR, not the seat of that name; moved whole from tests/test_seats.py
        self.assertIs(valid, False, why)
        # THE CONTROL: an unconsumed row validates.
        chat.post("@codex-41 another owed row", who="daria")
        grant = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                        door="enter")
        self.assertEqual(grant.still(), (True, ""))

    def test_a_retired_attempt_is_refused_at_the_act(self):  # noqa: VACUOUS_ASSERTION — the absence is nothing typed by a superseded attempt; the same arm's unconditional control acts for the current attempt and asserts DELIVERED
        from helm import beacons, harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        att, first = self.mint()
        grant = self.admit(first)("enter")
        self.assertTrue(grant.ok, "fixture: %s" % grant.why)
        born = resumeturn._attempt_standing(self.SEAT, first)[1]
        self.assertEqual(grant.capture()[2].get("horizon"),
                         born + resumeturn._debounce_s())
        second = beacons.install_repair(self.SEAT, att, time.time())
        self.assertNotEqual(second, first, "fixture: nothing replaced it")
        valid, why = grant.still()
        self.assertIs(valid, False, "a retired attempt validated at the act")
        self.assertIn("retired", why)
        # THROUGH THE DOOR: the replacement lands during the placement proof.
        _att, third = self.mint(since=time.time() - 1)
        pane = _ActPane()

        def replace_during_the_proof(p):
            if not getattr(p, "replaced", False):
                p.replaced = True
                beacons.install_repair(self.SEAT, _att, time.time())
        pane.on_read = replace_during_the_proof
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(third))
        self.assertEqual(pane.sent, [], "a superseded attempt typed")
        self.assertEqual(getattr(detail, "kind", None), "stale", detail)
        # THE CONTROL: the current attempt acts.
        current = resumeturn._attempt_standing(self.SEAT, third)[0]
        self.assertEqual(current, "retired", "fixture")
        _att, fourth = self.mint(since=time.time())
        pane = _ActPane()
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(fourth))
        self.assertEqual(state, harness.DELIVERED, detail)

    def test_a_missing_selected_family_record_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the unconditional controls after the loop assert a HEALTHY record permits and a dark one refuses as paused
        from helm import resumeturn
        chat.post("@codex-41 an owed row", who="daria")

        def prepare():
            return resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                           door="placement")
        os.remove(proxywatch._state_path())
        for label, write in (
                ("no snapshot at all", lambda: None),
                ("only another family", lambda: self.health("HEALTHY",
                                                            "opencode")),
                ("HEALTHY record with an unrecognised latch",
                 lambda: pk.write_json(proxywatch._state_path(), {
                     "ts": time.time(),
                     "upstream": {"codex": {"state": "HEALTHY",
                                            "dark": "false"}}}))):
            write()
            grant = prepare()
            self.assertFalse(grant.ok, "a %s authorized a keystroke" % label)
            self.assertEqual(grant.kind, "unknown", "%s: %s" % (label,
                                                                grant.why))
            self.assertIn("no recognised codex pause record", grant.why)
        # THE CONTROLS: HEALTHY permits and a dark record refuses as paused.
        self.health("HEALTHY")
        self.assertTrue(prepare().ok)
        self.health("AUTH-401")
        dark = prepare()
        self.assertEqual((dark.ok, dark.kind), (False, "paused"), dark.why)


    def test_an_unrelated_writer_inside_the_window_leaves_the_act_clean(self):  # noqa: VACUOUS_ASSERTION — the acts list is asserted equal to two named clean receipts, and the fixture asserts both writers landed
        """STABLE AUTHORITY IS NOT MISLABELED. Another seat joins -- a whole
        roster rewrite -- and the watcher runs a pass that rewrites both
        pause files with a new `ts` and this family's verdict unchanged, both
        between the final capture and the keystroke. Neither changed what
        authorized this act, so both acts are CLEAN."""
        from helm import harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        landed = []

        def unrelated_writers(pane, text, enter):
            if not enter and not pane.sent:
                before = (os.stat(seats.roster_path()).st_mtime_ns,
                          os.stat(proxywatch._state_path()).st_mtime_ns)
                time.sleep(0.01)
                seats.join(session="s-other", seat="codex-42", cwd="/tmp/q")
                self.health("HEALTHY")
                landed.append(before != (
                    os.stat(seats.roster_path()).st_mtime_ns,
                    os.stat(proxywatch._state_path()).st_mtime_ns))
        pane = _ActPane()
        pane.on_send = unrelated_writers
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(attempt))
        self.assertEqual(landed, [True],
                         "fixture: the unrelated writers did not rewrite the "
                         "roster and the pause file inside the window")
        self.assertEqual(state, harness.DELIVERED, detail)
        acts = self.entry().get("acts") or []
        self.assertEqual([(a["door"], a["outcome"]) for a in acts],
                         [("placement", harness.ACT_CLEAN),
                          ("enter", harness.ACT_CLEAN)],
                         "stable authority was labeled obsolete by an "
                         "unrelated writer: %r" % (acts,))
        self.assertFalse(self.entry().get("obsolete"))

    def _obsolete_enter(self, attempt, pane=None, during=None):
        """Drive one act pair whose ENTER runs under an authorization changed
        and restored between its final capture and its keystroke."""
        restores = self.__dict__.setdefault("_restores", [])

        def wall_and_back(p, text, enter):
            if enter and not [s for s in p.sent if s[1]]:
                self.health("AUTH-401")
                # Each recovery is stamped a distinct second, as two watcher
                # passes minutes apart are.
                restores.append(True)
                self.health("HEALTHY", since=proxywatch._iso(
                    time.time() - 100 * len(restores)))
                if during is not None:
                    during()
        pane = pane or _ActPane()
        pane.on_send = wall_and_back
        return pane, pane.submit("h1", "GO", settle=0,
                                 admit=self.admit(attempt))

    def test_a_failed_accounting_write_still_refuses_the_repeat(self):  # noqa: VACUOUS_ASSERTION — the receipts are asserted equal to one clean placement and one obsolete Enter, and the fresh-attempt control asserts an admitted preparation
        """THE OUTCOME IS KNOWN BEFORE IT IS WRITTEN. The Enter is accounted
        obsolete and the write that would persist it fails; the same process
        still refuses the recovery and recovery-attempt repeats."""
        from helm import harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        state_file, real_write = resumeturn.state_path(), pk.write_json
        failing = []

        def write(path, value, *a, **k):
            if failing and path == state_file:
                raise OSError("ENOSPC at the accounting write")
            return real_write(path, value, *a, **k)
        with mock.patch.object(pk, "write_json", side_effect=write):
            pane, (state, detail) = self._obsolete_enter(
                attempt, during=lambda: failing.append(True))
        receipts = getattr(pane, "act_receipts", [])
        self.assertEqual([(r["door"], r["outcome"]) for r in receipts],
                         [("placement", harness.ACT_CLEAN),
                          ("enter", harness.OBSOLETE_AUTHORIZATION)],
                         "fixture: the Enter was not accounted obsolete")
        self.assertIn("ENOSPC", receipts[1]["account_error"],
                      "fixture: the accounting write did not fail")
        self.assertNotIn("enter", [a["door"] for a in
                                   self.entry().get("acts") or []],
                         "fixture: the failed write persisted anyway")
        for door in ("recovery", "recovery-attempt"):
            again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                            door=door, attempt=attempt,
                                            key=self.key())
            self.assertEqual((again.ok, again.kind), (False, "obsolete"),
                             "a repeat of an obsolete act whose accounting "
                             "write failed was admitted at the %s door: %s"
                             % (door, again.why))
        # THE CONTROL: a fresh attempt with nothing obsolete is admitted.
        _att, fresh = self.mint(since=time.time() - 1)
        self.assertTrue(resumeturn._prepare_due(
            self.SEAT, self.SID, room="main", door="recovery", attempt=fresh,
            key=self.key()).ok)

    def test_submit_refuses_the_stranded_text_of_an_obsolete_act(self):  # noqa: VACUOUS_ASSERTION — the Enter count is asserted equal to the one the child pressed, the routed note to name the refusal, and submit to answer UNKNOWN obsolete
        """THE DOOR A REFUSAL POINTS AT REFUSES TOO. A child's Enter is
        accounted obsolete and the TUI never ingests it, so Helm's text stays
        in the composer; the recovery is refused and routed; and `helm seat
        composers --submit` on that pane refuses the repeat."""
        from helm import composers, harness, resumeturn, tasks
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        pane.swallow_enter = True
        notes = []

        def wall_and_back(p, text, enter):
            if enter and not [s for s in p.sent if s[1]]:
                self.health("AUTH-401")
                self.health("HEALTHY", since=proxywatch._iso(time.time() - 30))
        pane.on_send = wall_and_back

        def reproved(_row, _adapter, action, **_kw):
            return action(pane, "h1", "re-proved h1"), None

        def add(title, owner, **kw):
            notes.append(kw.get("note"))
            return {"id": kw.get("tid"), "title": title}, None
        with mock.patch.dict(os.environ, {
                "HELM_RESUME_TURN_RECOVERY_PERSIST_S": "0",
                "HELM_RESUME_TURN_RECOVERY_BACKOFF_S": "0",
                "HELM_RESUME_TURN_SETTLE_S": "0",
                "HELM_SUBMIT_SETTLE_S": "0"}), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=reproved), \
                mock.patch.object(resumeturn, "_await_or_withdraw",
                                  return_value=(False, "")), \
                mock.patch.object(resumeturn, "_alert"), \
                mock.patch.object(tasks, "add", side_effect=add):
            mode, detail = resumeturn.child(
                self.SEAT, self.SID, "GO", 0, adapter=pane,
                record_key=self.key(), attempt=attempt, owed_room="main")
            enters = [s for s in pane.sent if s[1]]
            self.assertEqual(len(enters), 1,
                             "fixture: the child did not press exactly one "
                             "Enter: %r (%s)" % (pane.sent, detail))
            self.assertEqual(pane.composer, "GO",
                             "fixture: the text was not stranded")
            self.assertIn("OBSOLETE-AUTHORIZATION", detail)
            self.assertEqual(len(notes), 1, "fixture: nothing was routed")
            self.assertIn("will refuse it", notes[0],
                          "the routed note still points the owner at "
                          "--submit: %s" % notes[0])
            state, why = composers.submit("h1", adapter=pane)
        self.assertEqual([s for s in pane.sent if s[1]], enters,
                         "composers --submit pressed Enter for an obsolete "
                         "act: %r" % (pane.sent,))
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertEqual(getattr(why, "kind", None), "obsolete", why)
        # THE RECOVERY ENTRY REFUSES IT, before the claim and before any pane
        # read. The act door behind the entry refuses the repeat too, so the
        # door that answered is what shows the entry's own check held.
        self.assertEqual(getattr(why, "door", None), "recovery",
                         "composers --submit took an obsolete act past the "
                         "recovery entry to its act door: %s" % why)

    def test_a_crash_after_the_keystroke_leaves_an_intent_that_refuses(self):  # noqa: VACUOUS_ASSERTION — the control after the crash asserts a door that acted on nothing leaves no intent and admits its repeat
        """A PROCESS THAT DIES BETWEEN THE KEYSTROKE AND ITS ACCOUNTING WRITE
        leaves the intent its preparation recorded, with no receipt, and every
        repeat door reads that as an act of unknown outcome."""
        from helm import harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()

        class Died(BaseException):
            pass
        pane = _ActPane()
        with mock.patch.object(resumeturn, "_account_act",
                               side_effect=Died("killed after the keystroke")):
            with self.assertRaises(Died):
                pane.submit("h1", "GO", settle=0, admit=self.admit(attempt))
        self.assertEqual(pane.sent, [("GO", False)],
                         "fixture: the placement keystroke did not happen")
        for door in resumeturn.REPEAT_DOORS:
            again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                            door=door, attempt=attempt,
                                            key=self.key())
            self.assertEqual((again.ok, again.kind), (False, "obsolete"),
                             "an act a crash left unaccounted was repeated "
                             "at the %s door: %s" % (door, again.why))
            self.assertIn("no receipt", again.why)
        # THE CONTROL: a door that refused before acting withdraws its intent,
        # and the repeat is admitted.
        _att, fresh = self.mint(since=time.time() - 1)
        pane = _ActPane()
        pane.composer = "a person's draft"
        state, detail = pane.submit("h1", "GO", settle=0,
                                    admit=self.admit(fresh))
        self.assertEqual((pane.sent, state), ([], harness.UNKNOWN), detail)
        self.assertFalse((self.entry().get("intents") or {}).get(fresh),
                         "a door that acted on nothing left its intent")
        self.assertTrue(resumeturn._prepare_due(
            self.SEAT, self.SID, room="main", door="retry", attempt=fresh,
            key=self.key()).ok)

    def test_an_unnamed_attempt_leaves_no_record_for_the_next_child(self):  # noqa: VACUOUS_ASSERTION — the named control asserts the same obsolete act does refuse its repeat
        """A LEGACY CHILD WITH NO --attempt cannot bind an obsolete record to
        anything, so it leaves none: the next unnamed child of the same key is
        not refused and not charged for it."""
        from helm import harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        for n in (1, 2):
            pane, (state, detail) = self._obsolete_enter(None)
            outcomes = [r["outcome"] for r in pane.act_receipts]
            self.assertEqual(outcomes, [harness.ACT_CLEAN,
                                        harness.OBSOLETE_AUTHORIZATION],
                             "fixture: unnamed child %d's Enter was not "
                             "obsolete" % n)
            again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                            door="retry", attempt=None,
                                            key=self.key())
            self.assertTrue(again.ok,
                            "an unnamed child's obsolete act refused the next "
                            "unnamed child: %s" % again.why)
        entry = self.entry()
        self.assertNotIn("None", entry.get("obsolete") or {})
        self.assertNotIn("None", entry.get("receipts") or {})
        # THE CONTROL: a NAMED attempt's obsolete act refuses its repeat.
        _att, attempt = self.mint()
        self._obsolete_enter(attempt)
        self.assertEqual(resumeturn._prepare_due(
            self.SEAT, self.SID, room="main", door="retry", attempt=attempt,
            key=self.key()).kind, "obsolete")

    def test_a_stalled_dependency_read_refuses_inside_its_deadline(self):  # noqa: VACUOUS_ASSERTION — the same arm's unconditional control drives the same door unstalled and asserts DELIVERED
        """A REAL dependency read of the shipped capture -- the stat of this
        seat's delivery cursor -- blocks on the capture worker. The door
        abandons it at the declared deadline on every round, refuses with the
        measured capture time, and acts on nothing."""
        from helm import harness
        from helm.seats_cursor import cursor_path
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        target = cursor_path("main", self.SEAT, self.SID)
        release, stalls, real_stat = threading.Event(), [], os.stat

        def stat(path, *a, **k):
            if path == target and \
                    threading.current_thread().name == "helm-act-capture":
                stalls.append(time.monotonic())
                release.wait(4.0)
            return real_stat(path, *a, **k)
        pane = _ActPane()
        started = time.monotonic()
        try:
            with mock.patch.object(harness, "ACT_DEADLINE_S", 0.3), \
                    mock.patch("os.stat", side_effect=stat):
                state, detail = pane.submit("h1", "GO", settle=0,
                                            admit=self.admit(attempt))
            elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertTrue(stalls, "fixture: the stalled read was never reached")
        self.assertEqual(pane.sent, [],
                         "acted on a dependency read that stalled past its "
                         "deadline")
        self.assertEqual(len(stalls), 1 + harness.ADMISSION_RESTARTS,
                         "fixture: the stalled read was not reached on every "
                         "round")
        self.assertLess(elapsed, 3.5, "the door waited out the stall")
        self.assertEqual(state, harness.NOT_DELIVERED, detail)
        self.assertEqual(getattr(detail, "kind", None), "moved", detail)
        self.assertIn("deadline", detail)
        self.assertIn("the capture ran", detail)
        # THE CONTROL: the same door, unstalled, acts.
        pane = _ActPane()
        with mock.patch.object(harness, "ACT_DEADLINE_S", 0.3):
            state, detail = pane.submit("h1", "GO", settle=0,
                                        admit=self.admit(attempt))
        self.assertEqual(state, harness.DELIVERED, detail)

    # -- task/2463 r10: the arm specifications a review wrote for its five
    # findings, followed as written. Fresh state per adverse and control case
    # is this class's setUp.

    def _same_second_pause_pass(self, between):
        """Drive one placement and Enter while the selected family's pause
        record is published by proxywatch's OWN seat and family composers,
        stamped by a controlled wall clock (monotonic time untouched). The
        selected producer history starts empty, so setUp's older HEALTHY
        record cannot supply it. `between` names the two states published at
        the placement keystroke: one before the placement lands and one after
        it, 0.10s apart, before the accounting read."""
        clock = [float(int(time.time())) + 0.10]
        selected = [{}]
        history = []

        def publish(state):
            previous = (pk.read_json(proxywatch._state_path(), {}) or {}
                        ).get("upstream") or {}
            selected[0] = proxywatch._compose_upstream_seat(
                self.SEAT, (state, "arm fixture", 0), selected[0], clock[0])
            records, err = proxywatch._compose_upstream_records(
                {"upstream": {"codex": selected[0]}}, previous)
            self.assertIsNone(err)
            pk.write_json(proxywatch._state_path(), {
                "ts": clock[0], "upstream": records})
            history.append(dict(records["codex"]))

        with mock.patch.object(time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(proxywatch._journal, "record_episode_end"), \
                mock.patch.object(proxywatch, "read_vendor_resets",
                                  return_value=({}, None)):
            publish("HEALTHY")
            chat.post("@codex-41 an owed row", who="daria")
            _att, attempt = self.mint()
            pane = _ActPane()
            real_send = pane.send
            changed = []

            def send(handle, text, enter=True):
                if not enter and not changed:
                    changed.append(True)
                    clock[0] += 0.10
                    publish(between[0])
                    real_send(handle, text, enter)
                    clock[0] += 0.10
                    publish(between[1])
                else:
                    real_send(handle, text, enter)

            with mock.patch.object(pane, "send", side_effect=send):
                state, detail = pane.submit(
                    "h1", "GO", settle=0, admit=self.admit(attempt))
        self.assertEqual(changed, [True],
                         "fixture: the placement keystroke never published")
        return state, detail, attempt, pane, history

    def test_a_pause_changed_and_restored_inside_one_second_is_accounted(self):
        """F1, SAME-SECOND PAUSE ABA. The watcher records AUTH-401 before the
        placement lands and HEALTHY again after it, all three records inside
        one second, so the selected family's state, dark latch and `since`
        read back identical to the final capture. The placement ran under a
        wall, so it is accounted obsolete-authorization, charged, and a
        repeat for its attempt is refused at every repeat door."""
        from helm import harness, resumeturn
        state, detail, attempt, pane, history = \
            self._same_second_pause_pass(("AUTH-401", "HEALTHY"))
        self.assertEqual([r["state"] for r in history],
                         ["HEALTHY", "AUTH-401", "HEALTHY"])
        self.assertEqual(len({r["since"] for r in history}), 1,
                         "fixture: the three records did not share one "
                         "second: %r" % (history,))
        self.assertEqual(history[0]["dark"], False)
        self.assertEqual(history[1]["dark"], True)
        self.assertEqual(history[2]["dark"], False)
        self.assertEqual(pane.sent, [("GO", False), ("", True)])
        self.assertEqual(state, harness.DELIVERED, detail)
        placement = next(a for a in self.entry()["acts"]
                         if a["attempt"] == attempt
                         and a["door"] == "placement")
        self.assertEqual(placement["outcome"], harness.OBSOLETE_AUTHORIZATION,
                         "a placement that ran under a pause changed and "
                         "restored inside one second was accounted %r: %r"
                         % (placement["outcome"], placement))
        entry = self.entry()
        self.assertIn(attempt, entry.get("receipts") or {},
                      "the same-second obsolete placement was not charged")
        for door in resumeturn.REPEAT_DOORS:
            again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                            door=door, attempt=attempt,
                                            key=self.key())
            self.assertEqual((again.ok, again.kind), (False, "obsolete"),
                             "a repeat of a same-second obsolete placement "
                             "was not refused at the %s door: %s"
                             % (door, again.why))

    def test_a_same_second_healthy_rewrite_leaves_the_act_clean(self):  # noqa: VACUOUS_ASSERTION — the acts list is asserted equal to two named clean receipts and the sent list to one placement and one Enter
        """F1's HEALTHY CONTROL: the identical producers, writes and clock
        advances, HEALTHY at both callback points. Nothing that authorized the
        act changed, so both acts are CLEAN."""
        from helm import harness
        state, detail, attempt, pane, history = \
            self._same_second_pause_pass(("HEALTHY", "HEALTHY"))
        self.assertEqual([r["state"] for r in history],
                         ["HEALTHY", "HEALTHY", "HEALTHY"])
        self.assertEqual(len({r["since"] for r in history}), 1,
                         "fixture: an unchanged HEALTHY pass re-stamped since")
        self.assertEqual([r["dark"] for r in history], [False, False, False])
        self.assertEqual(pane.sent, [("GO", False), ("", True)])
        self.assertEqual(state, harness.DELIVERED, detail)
        acts = [a for a in self.entry().get("acts") or []
                if a.get("attempt") == attempt]
        self.assertEqual([(a["door"], a["outcome"]) for a in acts],
                         [("placement", harness.ACT_CLEAN),
                          ("enter", harness.ACT_CLEAN)],
                         "identical HEALTHY rewrites were accounted obsolete: "
                         "%r" % (acts,))
        self.assertFalse(self.entry().get("obsolete"))

    def test_the_watchers_acknowledgement_write_inside_the_act_leaves_it_clean(self):  # noqa: VACUOUS_ASSERTION — the acts list is asserted equal to two named clean receipts, and the fixture asserts the real pass wrote once before the capture and replayed its acknowledgement inside the act
        """A PASS THAT LIFTS A PAUSE WRITES TWICE AND CHANGES ONCE. The real
        `cmd_proxywatch --post` records the family AUTH-401 -> HEALTHY, and
        its own acknowledgement call is held and replayed, with the
        arguments the pass gave it, inside the placement keystroke. The final
        capture read the first write, HEALTHY already in force; the second
        write carries the same verdict. Nothing that authorized the act
        changed, so both acts are CLEAN. The same-second ABA arm is the
        control that must stay obsolete."""
        from helm import harness
        self.health("AUTH-401")
        now = time.time()
        recovered = {"ts": now, "seats": [], "proxy_runtime": {},
                     "upstream": {"codex": {
                         "state": "HEALTHY", "dark": False,
                         "since": proxywatch._iso(now),
                         "detail": "arm: recovered", "ms": 1}}}
        real_record = proxywatch.record
        writes, held = [], []

        def codex():
            return dict((pk.read_json(proxywatch._state_path(), {}) or {}
                         )["upstream"]["codex"])

        def record(*args, **kwargs):
            if writes:
                held.append((args, kwargs))  # The pass's acknowledgement.
                return True
            written = real_record(*args, **kwargs)
            writes.append(codex())
            return written

        with mock.patch.object(proxywatch, "read_vendor_resets",
                               return_value=({}, None)):
            with mock.patch.object(proxywatch, "health",
                                   return_value=recovered), \
                    mock.patch.object(proxywatch, "record",
                                      side_effect=record), \
                    mock.patch.object(proxywatch, "_cred_follow_pass",
                                      return_value=None), \
                    mock.patch.object(proxywatch, "_codex_budget_pass",
                                      return_value=None), \
                    mock.patch.object(proxywatch, "_owner_push",
                                      return_value=True), \
                    mock.patch("helm.chat.post"):
                proxywatch.cmd_proxywatch(["--post"])
            self.assertEqual([(w["state"], w["dark"]) for w in writes],
                             [("HEALTHY", False)],
                             "fixture: the pass did not lift the pause")
            self.assertEqual(len(held), 1,
                             "fixture: the pass made no acknowledgement write")
            chat.post("@codex-41 an owed row", who="daria")
            _att, attempt = self.mint()
            acked = []

            def acknowledge(_pane, _text, enter):
                if not enter and not acked:
                    args, kwargs = held[0]
                    self.assertTrue(real_record(*args, **kwargs))
                    acked.append(codex())
            pane = _ActPane()
            pane.on_send = acknowledge
            state, detail = pane.submit("h1", "GO", settle=0,
                                        admit=self.admit(attempt))
        self.assertEqual([(w["state"], w["dark"]) for w in acked],
                         [("HEALTHY", False)],
                         "fixture: the acknowledgement never ran inside the "
                         "act")
        self.assertEqual(pane.sent, [("GO", False), ("", True)])
        self.assertEqual(state, harness.DELIVERED, detail)
        acts = [a for a in self.entry().get("acts") or []
                if a.get("attempt") == attempt]
        self.assertEqual([(a["door"], a["outcome"]) for a in acts],
                         [("placement", harness.ACT_CLEAN),
                          ("enter", harness.ACT_CLEAN)],
                         "the watcher's acknowledgement write of the pass "
                         "that lifted the pause was accounted as a change: "
                         "%r (identities %r then %r)"
                         % (acts, writes[0].get("transition_id"),
                            acked[0].get("transition_id")))
        self.assertFalse(self.entry().get("obsolete"))

    def _recorded(self, pane, text, attempt):
        """The canonical injection F2 and F3 recover: the real record, its
        first held observation older than the persistence interval, and the
        reader's own lookup."""
        from helm import resumeturn
        pane.composer = text
        generation = resumeturn._record_injection(
            self.SEAT, self.SID, "h1", text, adapter=pane.name,
            attempt=attempt, account_key=self.key())
        self.assertTrue(generation)
        resumeturn._observe_injection(
            self.SEAT, "h1", text, generation,
            now=time.time() - resumeturn.recovery_persist_s() - 1)
        injection = resumeturn.recorded_injections(
            include_expired=True)["h1"]
        self.assertTrue(resumeturn._valid_injection(injection))
        self.assertTrue(resumeturn.injection_persistent(injection))
        self.assertEqual(
            resumeturn._recovery_task_delivered("h1", generation),
            (False, None))
        return injection

    def _obsolete_injection(self):
        """A persistent canonical injection whose named attempt another
        process accounted obsolete-authorization."""
        from helm import harness, resumeturn
        _att, attempt = self.mint()
        pane = _ActPane()
        self._recorded(pane, "GO", attempt)
        resumeturn._account_act(
            self.key(), self.SID, self.SEAT, attempt,
            {"door": "enter", "outcome": harness.OBSOLETE_AUTHORIZATION,
             "what": "arm: prior act was obsolete"})
        self.assertIn(attempt, self.entry()["obsolete"])
        resumeturn._OBSOLETE_HERE.clear()  # Model another reader process.
        return pane

    @staticmethod
    def _reproved(pane):
        def reproved(_row, _adapter, action, **_kwargs):
            return action(pane, "h1", "fixture: re-proved h1"), None
        return reproved

    def test_an_unreadable_obsolete_check_refuses_the_recovery_enter(self):  # noqa: VACUOUS_ASSERTION — the must-hit asserts exactly one failed state read; test_a_readable_clean_record_recovers_with_one_enter drives the same door on the same fixture and presses one Enter
        """F2, THE OBSOLETE CHECK FAILS OPEN. Only the obsolete check's read of
        the resume state fails; the store's own reader swallows the error. A
        read that could not see the obsolete record is UNKNOWN and sends
        nothing -- even though the reservation that follows reads fine."""
        import builtins
        import errno
        from helm import composers, harness, resumeturn
        pane = self._obsolete_injection()
        real_open = builtins.open
        real_obsolete = resumeturn._obsolete_act
        faults = []

        def fail_state_read(path, *args, **kwargs):
            if os.fspath(path) == resumeturn.state_path():
                faults.append(os.fspath(path))
                raise OSError(errno.EIO, "arm: obsolete-check read failed")
            return real_open(path, *args, **kwargs)

        def unreadable_obsolete(*args, **kwargs):
            with mock.patch("builtins.open", side_effect=fail_state_read):
                return real_obsolete(*args, **kwargs)

        with mock.patch.object(resumeturn, "_obsolete_act",
                               side_effect=unreadable_obsolete), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=self._reproved(pane)):
            state, detail = composers.submit("h1", adapter=pane)
        self.assertIn(resumeturn.state_path(), faults,
                      "fixture: the obsolete check's read never failed")
        # THE RECOVERY ENTRY REFUSES, before the claim and before any pane
        # read. The act door behind it re-asks the same store, so the door
        # that answered is what shows the entry's own read held.
        self.assertEqual(getattr(detail, "door", None), "recovery",
                         "a recovery went past an obsolete check that could "
                         "not read the resume state: %s" % detail)
        self.assertEqual(faults, [resumeturn.state_path()],
                         "fixture: the obsolete check's read did not fail "
                         "exactly once")
        self.assertEqual(pane.sent, [],
                         "a recovery Enter was pressed past an obsolete check "
                         "that could not read the resume state: %r (%s)"
                         % (pane.sent, detail))
        self.assertEqual(state, harness.UNKNOWN, detail)

    def test_a_readable_obsolete_record_refuses_the_recovery_enter(self):  # noqa: VACUOUS_ASSERTION — the refusal kind is asserted equal to obsolete, which an empty or absent refusal cannot satisfy
        """F2's first CONTROL: the same obsolete record, read without a fault,
        refuses UNKNOWN obsolete and sends nothing."""
        from helm import composers, harness
        pane = self._obsolete_injection()
        with mock.patch("helm.autocompact._pane_action",
                        side_effect=self._reproved(pane)):
            state, detail = composers.submit("h1", adapter=pane)
        self.assertEqual(pane.sent, [], detail)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertEqual(getattr(detail, "kind", None), "obsolete", detail)

    def test_a_readable_clean_record_recovers_with_one_enter(self):  # noqa: VACUOUS_ASSERTION — the sent list is asserted EQUAL to one Enter and the state to DELIVERED, which no empty observable satisfies
        """F2's second CONTROL: the same canonical injection with no obsolete
        accounting reserves and recovers through the real doors with one
        Enter. The row the repair was launched for is still owed, as it is
        for every attempt a repair pass mints."""
        from helm import composers, harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        self._recorded(pane, "GO", attempt)
        with mock.patch("helm.autocompact._pane_action",
                        side_effect=self._reproved(pane)):
            state, detail = composers.submit("h1", adapter=pane)
        self.assertEqual(pane.sent, [("", True)], detail)
        self.assertEqual(state, harness.DELIVERED, detail)

    def _submit_after_the_child(self, attempt, move):
        """THE PRODUCER OF EVERY INJECTION A REPAIR TYPES, driven whole.
        `resumeturn.child` places "GO" for `attempt` into a pane whose TUI
        ingests no Enter, fails its own recovery through its act door the
        same way, and routes the held text. The TUI then ingests Enters again,
        `move()` changes the world, and the owner runs `helm seat composers
        --submit h1`. -> (state, detail, the Enters the submit pressed)."""
        from helm import composers, harness, resumeturn, tasks
        pane = _ActPane()
        pane.swallow_enter = True

        def reproved(_row, _adapter, action, **_kw):
            return action(pane, "h1", "re-proved h1"), None

        def add(title, owner, **kw):
            return {"id": kw.get("tid"), "title": title}, None
        with mock.patch.dict(os.environ, {
                "HELM_RESUME_TURN_RECOVERY_PERSIST_S": "0",
                "HELM_RESUME_TURN_RECOVERY_BACKOFF_S": "0",
                "HELM_RESUME_TURN_SETTLE_S": "0",
                "HELM_SUBMIT_SETTLE_S": "0"}), \
                mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=reproved), \
                mock.patch.object(resumeturn, "_await_or_withdraw",
                                  return_value=(False, "")), \
                mock.patch.object(resumeturn, "_alert"), \
                mock.patch.object(tasks, "add", side_effect=add):
            _mode, detail = resumeturn.child(
                self.SEAT, self.SID, "GO", 0, adapter=pane,
                record_key=self.key(), attempt=attempt, owed_room="main")
            self.assertEqual(pane.composer, "GO",
                             "fixture: the child did not strand its text: %s"
                             % detail)
            spent = len([s for s in pane.sent if s[1]])
            self.assertGreaterEqual(spent, 1, "fixture: the child pressed no "
                                              "Enter: %s" % detail)
            injection = resumeturn.recorded_injections(
                include_expired=True).get("h1") or {}
            self.assertEqual(injection.get("attempt"), attempt,
                             "fixture: the child recorded no injection for "
                             "attempt %s: %r" % (attempt, injection))
            self.assertIsNotNone(injection.get("held_at"),
                                 "fixture: the held text was never observed")
            pane.swallow_enter = False
            move()
            state, why = composers.submit("h1", adapter=pane)
        return state, why, [s for s in pane.sent if s[1]][spent:]

    def test_submit_recovers_the_stranded_text_of_a_current_attempt(self):
        """THE CONTROL for the three refusals below, on the same producer: an
        attempt its episode still names, inside its horizon, for a healthy
        family with the row still owed, recovers with one Enter."""
        from helm import harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        state, why, enters = self._submit_after_the_child(
            attempt, lambda: None)
        self.assertEqual(enters, [("", True)], why)
        self.assertEqual(state, harness.DELIVERED, why)

    def test_submit_refuses_the_stranded_text_of_a_retired_attempt(self):
        """A NEWER ATTEMPT RETIRES THE ONE BEFORE IT FOR GOOD, and the text a
        retired attempt typed is not recovered in its name: the same act door the
        child's own recovery meets refuses it, and no Enter is pressed."""
        from helm import beacons, harness
        chat.post("@codex-41 an owed row", who="daria")
        att, attempt = self.mint()
        minted = []

        def retire():
            minted.append(beacons.install_repair(self.SEAT, att, time.time()))
            self.assertNotIn(minted[0], (None, attempt),
                             "fixture: no newer attempt was minted")
        state, why, enters = self._submit_after_the_child(attempt, retire)
        self.assertEqual(len(minted), 1, "fixture: the attempt never retired")
        self.assertEqual(enters, [],
                         "composers --submit pressed Enter for retired "
                         "attempt %s: %r (%s)" % (attempt, enters, why))
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertEqual(getattr(why, "kind", None), "stale", why)
        self.assertIn("retired", why)

    def test_submit_refuses_the_stranded_text_of_an_expired_attempt(self):  # noqa: VACUOUS_ASSERTION — the helper asserts the child pressed at least one Enter on the same pane before the submit, the refusal kind is asserted EQUAL to stale, and test_submit_recovers_the_stranded_text_of_a_current_attempt drives the same producer to one Enter
        """AN ATTEMPT PAST ITS HORIZON MAY NOT ACT, and that is measured from
        its own birth by the real clock: the child acts inside the horizon,
        the horizon then passes, and the recovery is refused. The horizon
        falls 0.4s after the mint, and the child acts in milliseconds."""
        from helm import harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        born = time.time() - resumeturn._debounce_s() + 0.4
        _att, attempt = self.mint(since=born - 1, born=born)
        waited = []

        def expire():
            waited.append(max(0.0, born + resumeturn._debounce_s()
                              - time.time()) + 0.05)
            time.sleep(waited[0])
        state, why, enters = self._submit_after_the_child(attempt, expire)
        self.assertGreater(time.time(), born + resumeturn._debounce_s(),
                           "fixture: the horizon did not pass")
        self.assertEqual(enters, [],
                         "composers --submit pressed Enter for attempt %s "
                         "past its horizon: %r (%s)" % (attempt, enters, why))
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertEqual(getattr(why, "kind", None), "stale", why)
        self.assertIn("launch horizon", why)

    def test_submit_refuses_the_stranded_text_while_the_family_is_paused(self):
        """THE PAUSE THAT GOVERNS EVERY OTHER DELIVERY GOVERNS THE RECOVERY.
        The watcher records a wall after the child typed; the recovery is
        refused as paused and no Enter is pressed."""
        from helm import harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        state, why, enters = self._submit_after_the_child(
            attempt, lambda: self.health("AUTH-401"))
        self.assertEqual(enters, [],
                         "composers --submit pressed Enter while the codex "
                         "family was paused: %r (%s)" % (enters, why))
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertEqual(getattr(why, "kind", None), "paused", why)

    def test_submit_refuses_an_unnamed_repairs_text_while_the_family_is_paused(self):
        """The manual nudge launches the repair child with no attempt, and its
        placement still met the act door; so does its recovery."""
        from helm import harness
        chat.post("@codex-41 an owed row", who="daria")
        state, why, enters = self._submit_after_the_child(
            None, lambda: self.health("AUTH-401"))
        self.assertEqual(enters, [],
                         "composers --submit pressed Enter for an unnamed "
                         "repair while the codex family was paused: %r (%s)"
                         % (enters, why))
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertEqual(getattr(why, "kind", None), "paused", why)

    def test_submit_refuses_a_repair_record_whose_authority_it_cannot_name(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted to carry the named operator sentence, and test_a_readable_clean_record_recovers_with_one_enter drives the same recorder and door to one Enter
        """FAIL CLOSED. A record typed under a repair authorization whose
        account key is not the repair key of the seat it was filed under
        names no seat to re-read that authorization from, so the recovery
        refuses UNKNOWN with a sentence the owner can act on and presses
        nothing. Its control is test_a_readable_clean_record_recovers_with_
        one_enter: the same record filed under its own seat recovers."""
        from helm import composers, harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        pane.composer = "GO"
        generation = resumeturn._record_injection(
            "session:" + self.SID, self.SID, "h1", "GO", adapter=pane.name,
            attempt=attempt, account_key=self.key())
        self.assertTrue(generation)
        resumeturn._observe_injection(
            "session:" + self.SID, "h1", "GO", generation,
            now=time.time() - resumeturn.recovery_persist_s() - 1)
        with mock.patch("helm.autocompact._pane_action",
                        side_effect=self._reproved(pane)):
            state, why = composers.submit("h1", adapter=pane)
        self.assertEqual(pane.sent, [],
                         "composers --submit pressed Enter for a repair "
                         "record whose authority it could not name: %s" % why)
        self.assertEqual(state, harness.UNKNOWN, why)
        self.assertIn("resolve the held text by hand", why)

    def _routed_then_asked(self, attempt, move=lambda: None, persist="0",
                           in_recovery=None, repair=True):
        """THE CHILD STRANDS AND ROUTES, THEN THE CENSUS AND THE SUBMIT ANSWER
        FOR THE SAME PANE. `resumeturn.child` places "GO" into a pane whose
        TUI ingests no Enter -- for `attempt` under this seat's repair key,
        or, without `repair`, as the compaction leg under no authorization --
        and routes the held text to a recovery task. `in_recovery()` runs as
        the child enters its own recovery. The TUI then ingests Enters again,
        `move()` changes the world, `helm seat composers` reads the pane, and
        the owner runs `helm seat composers --submit h1`.
        -> (the routed note, the census row for h1, the submit's state and
        detail, the Enters the submit pressed)."""
        from helm import composers, harness, resumeturn, tasks
        pane = _ActPane()
        pane.swallow_enter = True
        pane.list = lambda: [{"handle": "h1", "title": self.SEAT,
                              "worktree": "/tmp/p",
                              "last_output_at": time.time()}]
        notes, entered = [], []
        real_recover = resumeturn.recover_injection

        def recover(*args, **kwargs):
            if in_recovery is not None and not entered:
                in_recovery()
            entered.append(True)
            return real_recover(*args, **kwargs)

        def reproved(_row, _adapter, action, **_kw):
            return action(pane, "h1", "re-proved h1"), None

        def add(title, owner, **kw):
            notes.append(kw.get("note"))
            return {"id": kw.get("tid"), "title": title}, None
        authority = ({"record_key": self.key(), "attempt": attempt,
                      "owed_room": "main"} if repair else {})
        with mock.patch.dict(os.environ, {
                "HELM_RESUME_TURN_RECOVERY_PERSIST_S": persist,
                "HELM_RESUME_TURN_RECOVERY_BACKOFF_S": "0",
                "HELM_RESUME_TURN_SETTLE_S": "0",
                "HELM_SUBMIT_SETTLE_S": "0"}), \
                mock.patch.object(harness, "SUBMIT_VERIFY_INTERVAL_S", 0), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=reproved), \
                mock.patch.object(resumeturn, "_await_or_withdraw",
                                  return_value=(False, "")), \
                mock.patch.object(resumeturn, "_alert"), \
                mock.patch.object(tasks, "add", side_effect=add):
            with mock.patch.object(resumeturn, "recover_injection",
                                   side_effect=recover):
                _mode, detail = resumeturn.child(
                    self.SEAT, self.SID, "GO", 0, adapter=pane, **authority)
            self.assertEqual(pane.composer, "GO",
                             "fixture: the child did not strand its text: %s"
                             % detail)
            self.assertEqual(len(notes), 1,
                             "fixture: the child routed %d recovery tasks: %s"
                             % (len(notes), detail))
            injection = resumeturn.recorded_injections(
                include_expired=True).get("h1") or {}
            self.assertEqual(bool(injection.get("account_key")), repair,
                             "fixture: the record's authority is not the "
                             "leg's: %r" % (injection,))
            pane.swallow_enter = False
            move()
            rows, err = composers.scan(adapter=pane)
            self.assertIsNone(err, err)
            census = [r for r in rows if r["handle"] == "h1"]
            self.assertEqual(len(census), 1,
                             "fixture: the census did not read h1: %r" % rows)
            spent = len([s for s in pane.sent if s[1]])
            state, why = composers.submit("h1", adapter=pane)
        return (notes[0], census[0], state, why,
                [s for s in pane.sent if s[1]][spent:])

    def test_a_strand_past_its_horizon_is_not_routed_to_a_submit_that_refuses(self):  # noqa: VACUOUS_ASSERTION — the note is asserted to name the horizon refusal and to say --submit will refuse, and the Enter list is paired with the submit's kind asserted EQUAL to stale
        """A HORIZON THAT PASSES WHILE THE CHILD WAITS OUT PERSISTENCE ENDS
        THAT ATTEMPT FOR GOOD. The child types inside the horizon, the horizon
        passes during its persistence wait, and its own recovery is refused as
        stale. The recovery task it writes must not send the owner to `helm
        seat composers --submit`, which refuses the same attempt and always
        will; it says so and sends the owner to the pane.

        The horizon falls 0.4s after the mint and the persistence wait is
        0.5s, the 4:5 the arm always had: the child types inside the horizon
        and the horizon passes before its wait ends."""
        from helm import resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        born = time.time() - resumeturn._debounce_s() + 0.4
        _att, attempt = self.mint(since=born - 1, born=born)
        note, _row, _state, why, enters = self._routed_then_asked(
            attempt, persist="0.5")
        self.assertIn("launch horizon", note,
                      "fixture: the child's own recovery was not refused past "
                      "the horizon: %s" % note)
        self.assertNotIn("Only if that later reading reports helm-stranded",
                         note,
                         "the recovery task sends the owner to a --submit that "
                         "refuses an attempt past its horizon: %s" % note)
        self.assertIn("will refuse it", note, note)
        self.assertIn("resolve the held text by hand", note, note)
        self.assertEqual(enters, [], why)
        self.assertEqual(getattr(why, "kind", None), "stale", why)

    def test_a_strand_whose_attempt_retired_in_the_childs_recovery_is_not_routed_to_submit(self):  # noqa: VACUOUS_ASSERTION — the note is asserted to name the retirement and to say --submit will refuse, and the Enter list is paired with the submit's kind asserted EQUAL to stale
        """THE SAME NOTE WHEN THE ACT DOOR OF THE CHILD'S OWN RECOVERY REFUSES.
        The attempt is current when the child enters its recovery and retired
        by the time the recovery's door re-reads it; the task that recovery
        routes must not point at a --submit that refuses it too."""
        from helm import beacons
        chat.post("@codex-41 an owed row", who="daria")
        att, attempt = self.mint()
        minted = []

        def retire():
            minted.append(beacons.install_repair(self.SEAT, att, time.time()))
        note, _row, _state, why, enters = self._routed_then_asked(
            attempt, in_recovery=retire)
        self.assertEqual(len(minted), 1,
                         "fixture: the child never entered its own recovery")
        self.assertNotIn(minted[0], (None, attempt),
                         "fixture: no newer attempt was minted")
        self.assertIn("retired", note,
                      "fixture: the child's own recovery was not refused for "
                      "the retired attempt: %s" % note)
        self.assertNotIn("Only if that later reading reports helm-stranded",
                         note,
                         "the recovery task sends the owner to a --submit that "
                         "refuses a retired attempt: %s" % note)
        self.assertIn("will refuse it", note, note)
        self.assertIn("resolve the held text by hand", note, note)
        self.assertEqual(enters, [], why)
        self.assertEqual(getattr(why, "kind", None), "stale", why)

    def test_the_census_does_not_call_a_retired_attempts_strand_eligible(self):  # noqa: VACUOUS_ASSERTION — the same census row's why is asserted to carry the retirement and the will-refuse sentence, and test_a_current_attempts_strand_is_routed_to_submit_and_called_eligible reads that row as eligible
        """THE CENSUS SAYS WHAT THE DOOR WILL SAY. A newer attempt retires the
        one that typed the held text; `helm seat composers` must not call that
        strand eligible for a --submit that refuses it, and it names why and
        what to do instead."""
        from helm import beacons, composers
        chat.post("@codex-41 an owed row", who="daria")
        att, attempt = self.mint()
        minted = []

        def retire():
            minted.append(beacons.install_repair(self.SEAT, att, time.time()))
        _note, row, _state, why, enters = self._routed_then_asked(
            attempt, move=retire)
        self.assertNotIn(minted[0], (None, attempt),
                         "fixture: no newer attempt was minted")
        self.assertEqual((enters, getattr(why, "kind", None)), ([], "stale"),
                         "fixture: --submit did not refuse the retired "
                         "attempt: %s" % why)
        self.assertEqual(row["state"], composers.HELM_STRANDED, row)
        self.assertNotIn("eligible", row["why"],
                         "the census calls the strand of retired attempt %s "
                         "eligible for a --submit that refuses it: %s"
                         % (attempt, row["why"]))
        self.assertIn("will refuse it", row["why"])
        self.assertIn("retired", row["why"])
        self.assertIn("resolve the held text by hand", row["why"])

    def test_a_roster_that_is_a_fifo_makes_the_census_unknown_not_a_hang(self):  # noqa: VACUOUS_ASSERTION — the census row is asserted EQUAL to helm-stranded with its kind unknown, and the submit refusal is asserted to carry kind unknown beside the empty Enter list
        """task/2523 r1, P2: the census now reads the roster to
        judge a repair strand, and the roster reader opened it blocking, so a
        writerless FIFO where the roster belongs hung `helm seat composers`
        (the census never read the roster before this lane). The roster read
        refuses a non-regular file as UNREADABLE, so the census calls the
        strand unknown, never eligible, and --submit presses nothing. Every
        read runs on a thread with a deadline; a reader blocked in open() is
        released by opening the FIFO's write end, so a RED arm fails instead
        of wedging the suite."""
        import stat
        from helm import composers
        from helm.seats_common import roster_path
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        path = roster_path()

        def fifo_roster():
            os.unlink(path)
            os.mkfifo(path)
            self.assertTrue(stat.S_ISFIFO(os.stat(path).st_mode))
        box = {}

        def run():
            try:
                box["value"] = self._routed_then_asked(attempt, move=fifo_roster)
            except BaseException as e:      # noqa: BLE001 — re-raised below
                box["error"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(20)
        if t.is_alive():
            for _ in range(50):
                try:
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass
                t.join(0.2)
                if not t.is_alive():
                    break
            self.fail("the census or --submit blocked opening the FIFO roster "
                      "at %s" % path)
        if "error" in box:
            raise box["error"]
        _note, row, _state, why, enters = box["value"]
        self.assertEqual(row["state"], composers.HELM_STRANDED, row)
        self.assertNotIn("eligible", row["why"], row["why"])
        self.assertIn("could not be re-read", row["why"], row["why"])
        self.assertEqual(enters, [], why)
        self.assertEqual(getattr(why, "kind", None), "unknown", why)

    def test_a_current_attempts_strand_is_routed_to_submit_and_called_eligible(self):
        """THE CONTROL for the three above, on the same producer: the attempt
        is still current, so the task sends the owner to --submit, the census
        calls the strand eligible, and --submit recovers it with one Enter."""
        from helm import composers, harness
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        note, row, state, why, enters = self._routed_then_asked(attempt)
        self.assertIn("Only if that later reading reports helm-stranded run "
                      "`helm seat composers --submit h1`", note, note)
        self.assertEqual(row["state"], composers.HELM_STRANDED, row)
        self.assertIn("eligible for guarded bare-Enter recovery", row["why"])
        self.assertEqual(enters, [("", True)], why)
        self.assertEqual(state, harness.DELIVERED, why)

    def test_a_compaction_strand_keeps_its_submit_note_and_eligible_wording(self):
        """THE CONTROL for a record typed under no authorization: the
        compaction leg's strand keeps the payload-only wording in the task and
        the census, and --submit recovers it with one Enter."""
        from helm import composers, harness
        note, row, state, why, enters = self._routed_then_asked(
            None, repair=False)
        self.assertIn("Only if that later reading reports helm-stranded run "
                      "`helm seat composers --submit h1`", note, note)
        self.assertEqual(row["state"], composers.HELM_STRANDED, row)
        self.assertEqual(row["why"], "composer exactly matches a persistent "
                         "Helm injection and is eligible for guarded "
                         "bare-Enter recovery")
        self.assertEqual(enters, [("", True)], why)
        self.assertEqual(state, harness.DELIVERED, why)

    def test_a_door_refusal_keeps_its_door_and_kind_when_the_claim_release_fails(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted taken and its release asserted failed in the same detail whose door and kind are asserted EQUAL to named values
        """A REFUSAL KEEPS ITS DOOR AND KIND through every suffix the recovery
        joins onto it. The resume state turns unreadable after the recovery
        claim: the act door refuses the attempt as unknown, and releasing the
        claim fails too. The caller still reads the door and kind as fields."""
        import builtins
        import errno
        from helm import composers, harness, resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        pane = _ActPane()
        self._recorded(pane, "GO", attempt)
        real_open = builtins.open
        real_claim = resumeturn._claim_injection_recovery
        claimed = []

        def claim(injection):
            token = real_claim(injection)
            claimed.append(bool(token))
            return token

        def unreadable(path, *args, **kwargs):
            if claimed and isinstance(path, (str, bytes, os.PathLike)) \
                    and os.fspath(path) == resumeturn.state_path():
                raise OSError(errno.EIO, "arm: the resume state turned "
                                         "unreadable after the claim")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(resumeturn, "_claim_injection_recovery",
                               side_effect=claim), \
                mock.patch("builtins.open", side_effect=unreadable), \
                mock.patch("helm.autocompact._pane_action",
                           side_effect=self._reproved(pane)):
            state, detail = composers.submit("h1", adapter=pane)
        self.assertEqual(claimed, [True],
                         "fixture: the recovery claim was not taken")
        self.assertEqual(pane.sent, [], detail)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertIn("recovery claim release failed", detail,
                      "fixture: releasing the claim did not fail")
        self.assertEqual(
            (getattr(detail, "door", None), getattr(detail, "kind", None)),
            ("recovery-attempt", "unknown"),
            "the act door's refusal lost its door and kind when releasing the "
            "recovery claim failed: %s" % detail)

    def test_a_delivery_refused_at_its_door_keeps_its_door_and_kind_through_the_pane_proof(self):  # noqa: VACUOUS_ASSERTION — the pane proof is asserted present in the detail whose door and kind are asserted EQUAL to named values
        """THE SAME JOIN ON THE DELIVERY LEG: a placement refused for a paused
        family answers with the pane proof prefixed, and keeps its door and
        kind."""
        from helm import resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        self.health("AUTH-401")
        pane = _ActPane()
        with mock.patch("helm.autocompact._pane_action",
                        side_effect=self._reproved(pane)):
            _mode, detail = resumeturn.deliver(
                self.SEAT, "GO", self.SID, adapter=pane,
                admit=self.admit(attempt), attempt=attempt,
                account_key=self.key(), room="main")
        self.assertEqual(pane.sent, [], detail)
        self.assertIn("fixture: re-proved h1", detail,
                      "fixture: the pane proof was not joined: %s" % detail)
        self.assertEqual(
            (getattr(detail, "door", None), getattr(detail, "kind", None)),
            ("placement", "paused"),
            "the placement refusal lost its door and kind through the pane "
            "proof: %s" % detail)

    def _bookkeeping_write(self, fault):
        """An attempt another process accounted obsolete-authorization, then
        an unrelated key's ordinary bookkeeping write through the real
        `_record`. With `fault`, only that write's read of the resume state
        fails with EIO beneath the store reader; its own write is untouched.
        -> (attempt, faults)."""
        import builtins
        import errno
        from helm import harness, resumeturn
        _att, attempt = self.mint()
        resumeturn._account_act(
            self.key(), self.SID, self.SEAT, attempt,
            {"door": "enter", "outcome": harness.OBSOLETE_AUTHORIZATION,
             "what": "arm: prior act was obsolete"})
        resumeturn._OBSOLETE_HERE.clear()  # Model another reader process.
        self.assertEqual(
            (resumeturn._obsolete_act(self.key(), attempt) or {}).get("door"),
            "enter", "fixture: the obsolete record is not readable before "
            "the write")
        real_open = builtins.open
        faults = []

        def fail_state_read(path, *args, **kwargs):
            if os.fspath(path) == resumeturn.state_path():
                faults.append(os.fspath(path))
                raise OSError(errno.EIO, "arm: bookkeeping read failed")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open",
                        side_effect=fail_state_read if fault else real_open):
            resumeturn._record("other-key", self.SID, "withdrawn",
                               "arm: an unrelated bookkeeping write")
        return attempt, faults

    def test_a_bookkeeping_write_that_cannot_read_the_state_keeps_the_obsolete_record(self):
        """r11 F2, THE WRITER. The resume-turn bookkeeping writer rewrites the
        whole resume state. When its read fails it must not write, because a
        write from an empty reading erases every other key's obsolete records
        and intents, and the obsolete check then reads a clean attempt."""
        from helm import resumeturn
        attempt, faults = self._bookkeeping_write(fault=True)
        self.assertEqual(faults, [resumeturn.state_path()],
                         "fixture: the bookkeeping read did not fail exactly "
                         "once")
        got = resumeturn._obsolete_act(self.key(), attempt)
        self.assertEqual(
            (got or {}).get("door"), "enter",
            "a bookkeeping write that could not read the resume state erased "
            "the obsolete record of attempt %s: %r" % (attempt, got))
        self.assertIn(self.key(),
                      pk.read_json(resumeturn.state_path(), {}) or {})

    def test_a_bookkeeping_write_that_reads_the_state_keeps_both_keys(self):
        """The writer's CONTROL: the same write with no fault lands beside the
        obsolete record, which stays readable."""
        from helm import resumeturn
        attempt, faults = self._bookkeeping_write(fault=False)
        self.assertEqual(faults, [])
        store = pk.read_json(resumeturn.state_path(), {}) or {}
        self.assertIn(self.key(), store)
        self.assertEqual(store["other-key"]["mode"], "withdrawn")
        self.assertEqual(
            (resumeturn._obsolete_act(self.key(), attempt) or {}).get("door"),
            "enter")

    def _indirection_recovery(self, refuse_payload):
        """F3's fixture: a real long-directive indirection and its token, a
        persistent canonical injection of it, and the real preparation wrapped
        to capture the intent it persisted at the recovery-attempt door. With
        `refuse_payload`, the payload reads unavailable only AFTER that
        intent exists."""
        from helm import resumeturn
        chat.post("@codex-41 an owed row", who="daria")
        _att, attempt = self.mint()
        wire = resumeturn._wire_text(
            self.SEAT, self.SID,
            "a directive long enough to be stored " * 12)
        match = resumeturn._WIRE_INDIRECTION.match(wire)
        self.assertIsNotNone(match)
        token = match.group(1)
        self.assertEqual(resumeturn.indirection_payload_available(wire),
                         (True, token))
        pane = _ActPane()
        injection = self._recorded(pane, wire, attempt)
        prepared = []
        real_admit = self.admit(attempt)

        def admit(door):
            grant = real_admit(door)
            self.assertTrue(grant.ok, grant.why)
            if door == "recovery-attempt":
                intent = (self.entry().get("intents") or {}).get(attempt)
                self.assertIsInstance(intent, dict)
                prepared.append(dict(intent))
            return grant

        def unavailable(text, now=None):
            self.assertEqual(text, wire)
            self.assertEqual(len(prepared), 1)
            return False, token

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch(
                "helm.autocompact._pane_action",
                side_effect=self._reproved(pane)))
            if refuse_payload:
                stack.enter_context(mock.patch.object(
                    resumeturn, "indirection_payload_available",
                    side_effect=unavailable))
            state, detail = resumeturn.recover_injection(
                injection, adapter=pane, admit=admit)
        return state, detail, attempt, pane, prepared, wire, token

    def test_a_payload_refused_after_the_intent_withdraws_the_intent(self):  # noqa: VACUOUS_ASSERTION — the must-hit asserts one prepared intent; test_an_available_payload_recovers_and_leaves_no_intent drives the same door on the same fixture and presses one Enter
        """F3, A KNOWN ZERO-ACT REFUSAL LEFT AN INTENT. The recovery-attempt
        preparation records its intent; the directive the prompt points at
        then reads unavailable and the door refuses before any keystroke.
        Nothing was acted on, so the intent is withdrawn, and the same
        attempt's recovery preparation is not refused as obsolete for it."""
        from helm import harness, resumeturn
        state, detail, attempt, pane, prepared, wire, token = \
            self._indirection_recovery(refuse_payload=True)
        self.assertEqual(len(prepared), 1,
                         "fixture: the recovery-attempt intent was not "
                         "captured once")
        self.assertEqual(pane.sent, [], detail)
        self.assertEqual(state, harness.UNKNOWN, detail)
        self.assertNotIn(attempt, self.entry().get("intents") or {},
                         "a recovery refused on its payload before any "
                         "keystroke left its act intent: %r"
                         % (self.entry().get("intents"),))
        self.assertEqual(resumeturn.indirection_payload_available(wire),
                         (True, token))
        again = resumeturn._prepare_due(self.SEAT, self.SID, room="main",
                                        door="recovery", attempt=attempt,
                                        key=self.key())
        self.assertNotEqual(again.kind, "obsolete",
                            "a known zero-act payload refusal was read back "
                            "as an obsolete act: %s" % again.why)

    def test_an_available_payload_recovers_and_leaves_no_intent(self):
        """F3's HEALTHY CONTROL: the identical fixture with the payload
        available presses one Enter, delivers, and leaves no intent."""
        from helm import harness
        state, detail, attempt, pane, prepared, _wire, _token = \
            self._indirection_recovery(refuse_payload=False)
        self.assertEqual(len(prepared), 1, "fixture: no intent was prepared")
        self.assertEqual(pane.sent, [("", True)], detail)
        self.assertEqual(state, harness.DELIVERED, detail)
        self.assertNotIn(attempt, self.entry().get("intents") or {})
