"""A dispatch row is never filed for a seat that cannot answer it, and a
seat's stop-guard never shows it a row between two other seats (task/3531).

THE INCIDENT, measured on the live fleet. qwenlocal, a local-model seat inside
the Claude Code harness, filed its hand-back for a review lane as `helm
dispatch send claude ...` while its brief named the reviewer demo-claude-2
exactly. `claude` is an old orca-adopted seat name, and also the name of a
model family and of the harness: the roster still carried its row, so the
door accepted it, and `helm seat where claude` read DEAD. The review sat with
nobody. Then bonsai, another local seat in the same harness, was shown that
row by ITS stop-guard ("NEEDS CHECK-IN (OVERDUE) ... verify at the exact
recipient") and spent a long turn deciding whether it was `claude`.

WHAT THE STOP-GUARD HALF MEASURED IN CODE. That line is the SENDER rung
(`seats_stop_signals._dispatch_candidate`), scoped by
`dispatches._mine_or_unprovable`. Its comparator is exact
(`seats.recipient_matches`), so no family or harness name ever matched: the
row reached bonsai through the ORPHAN NET, which kept a row for EVERY stopping
seat whenever its custodian named no roster seat or read absent. A local
seat that has handed its work back and stopped reads absent, so its row went
to the whole fleet.
"""
import json
import os
import time
import unittest
from unittest import mock

from helm import dispatches, seats
from tests.test_dispatch_retract import RetractBase
from tests.test_dispatches import DispatchBase, run

DAY = 86400


class DeadRecipientDoorTest(DispatchBase):
    """THE SEND DOOR: a recipient the roster reads DEAD is refused, with the
    live seats named, unless `--force` says the sender means it."""

    BRIEF = ("hand-back for lane rollout-client-gone-1957: the reviewer is "
             "demo-claude-2, please read the tip and record a verdict.")

    def _plant(self, name, age_s=None):
        """A roster row for `name` whose newest presence beat is `age_s`
        seconds old (None: no beat recorded at all). No seen sidecar is
        written, so the row's own stamp is the only evidence."""
        seats.write_roster(name, presence_beat=False)
        path = seats.roster_path()
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        if age_s is None:
            r[name].pop("last_seen", None)
        else:
            r[name]["last_seen"] = time.time() - age_s
        with open(path, "w", encoding="utf-8") as f:
            json.dump(r, f)

    def _fleet(self):
        """The measured shape: `claude` rostered and a month dark, the
        reviewer the brief names live, and one unrelated live seat."""
        self._plant("claude", 30 * DAY)
        seats.write_roster("demo-claude-2", presence_beat=False)
        seats.write_roster("opus-integrator", presence_beat=False)

    def _send(self, recipient, message=None, kind="build", **kw):
        # One lane per send: a second open row on one lane is its own refusal.
        self._lanes = getattr(self, "_lanes", 0) + 1
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "post-1"}, None)):
            return dispatches.send(
                recipient, "rollout-client-gone-%d" % self._lanes,
                message or self.BRIEF,
                self.a, repo=self.repo, sign=False, kind=kind,
                new_work=True, **kw)

    def test_a_row_to_a_DEAD_seat_is_refused_naming_the_briefs_reviewer(self):
        self._fleet()
        row, why, _sent = self._send("claude", kind="review")
        self.assertIsNone(row, "a month-dark seat cannot answer this row")
        self.assertIn("DEAD", why)
        self.assertIn("did you mean demo-claude-2 (the reviewer your "
                      "brief names)?", why)
        self.assertIn("--force", why, "the refusal names its door")
        self.assertEqual(dispatches.open_rows(), [], "nothing was filed")

    def test_the_refusal_names_the_live_seats_when_the_brief_names_none(self):
        self._fleet()
        row, why, _sent = self._send("claude", message="please review the tip")
        self.assertIsNone(row)
        self.assertIn("demo-claude-2", why)
        self.assertIn("opus-integrator", why)
        self.assertNotIn("the reviewer your brief names", why)

    #: Seats named exactly like their catalog family (spawn names a family's
    #: first instance after it), plus the harness word `claude` itself.
    FAMILY_SEATS = ("ds4pro", "kimi", "qwenlocal", "bonsai", "grok", "gemini",
                    "openrouter", "codex", "claude")

    def test_a_seat_named_like_its_family_is_an_ordinary_named_seat(self):
        """A roster row is a seat whatever its name: quiet two hours it is
        between panes and admitted, as any named seat is (review F1)."""
        seats.write_roster("demo-claude-2", presence_beat=False)
        for name in self.FAMILY_SEATS:
            with self.subTest(seat=name):
                self._plant(name, 2 * 3600)
                row, why, _sent = self._send(name)
                if name == "openrouter":
                    # Its own door refuses this private tip (or-free reads
                    # public code only); this door must not be the one.
                    self.assertNotIn("DEAD", why or "")
                    self.assertEqual(dispatches._validate_recipient_rostered(
                        name, False), (True, None))
                    continue
                self.assertIsNotNone(row, why)
                self.assertEqual(row["recipient"], name)

    def test_a_family_named_row_with_no_beat_is_unknown_and_admitted(self):
        """No presence beat at all is UNKNOWN for every rostered name, a
        family or harness word included; only a name with NO roster row is
        refused, and that is the ABSENT door's refusal."""
        seats.write_roster("demo-claude-2", presence_beat=False)
        for name in ("claude", "codex"):
            with self.subTest(seat=name):
                self._plant(name, None)
                row, why, _sent = self._send(name)
                self.assertIsNotNone(row, why)
        row, why, _sent = self._send("claude-code")
        self.assertIsNone(row, "a bare harness word nobody sits in")
        self.assertIn("no roster row", why)

    def test_a_family_named_seat_dark_a_day_is_DEAD_like_any_named_seat(self):
        seats.write_roster("demo-claude-2", presence_beat=False)
        for name in self.FAMILY_SEATS:
            with self.subTest(seat=name):
                self._plant(name, 2 * DAY)
                row, why, _sent = self._send(name)
                self.assertIsNone(row)
                self.assertIn("reads DEAD (its last presence beat was 2d ago)",
                              why)
                self.assertNotIn("family or harness name", why)

    def test_the_measured_claude_row_is_refused_by_the_24h_named_seat_rule(self):
        """The live-fleet shape that motivated the door: `claude` rostered and
        a month dark. The named-seat DEAD rule refuses it, not its name."""
        self._fleet()
        row, why, _sent = self._send("claude", kind="review")
        self.assertIsNone(row)
        self.assertIn("reads DEAD (its last presence beat was 30d ago)", why)
        self.assertNotIn("family or harness name", why)

    def test_a_just_respawned_seat_reads_its_fresh_row_not_an_old_sidecar(self):
        """spawn re-registers a seat with its row stamped now and no beat
        (`presence_beat=False`); a seen sidecar left from its last life must
        not read it DEAD until its first beat (review F2)."""
        seats.write_roster("demo-claude-2", presence_beat=False)
        old = time.time() - 3 * DAY

        def sidecar(name, when):
            path = seats.seen_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
            os.utime(path, (when, when))

        self._plant("codex-2", 0)                 # spawn's re-register: now
        sidecar("codex-2", old)
        row, why, _sent = self._send("codex-2")
        self.assertIsNotNone(row, why)
        # CONTROL: the same old sidecar over an old row still reads DEAD.
        self._plant("codex-4", 3 * DAY)
        sidecar("codex-4", old)
        row, why, _sent = self._send("codex-4")
        self.assertIsNone(row)
        self.assertIn("3d ago", why)
        # CONTROL: a fresh sidecar over an old row is live, as before.
        self._plant("codex-5", 3 * DAY)
        sidecar("codex-5", time.time())
        row, why, _sent = self._send("codex-5")
        self.assertIsNotNone(row, why)

    def test_the_send_refusal_speaks_the_cli_not_python(self):
        self._fleet()
        _row_, why, _sent = self._send("claude")
        self.assertIn("`--force`", why)
        self.assertNotIn("force=True", why)

    def test_a_LIVE_seat_named_exactly_like_a_family_still_sends(self):
        seats.write_roster("codex", presence_beat=False)   # its row: now
        row, why, _sent = self._send("codex")
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "codex")

    def test_a_row_to_a_live_seat_still_sends(self):
        self._fleet()
        row, why, sent = self._send("demo-claude-2")
        self.assertIsNotNone(row, why)
        self.assertTrue(sent)
        self.assertEqual(row["recipient"], "demo-claude-2")

    def test_a_seat_between_panes_is_a_delay_not_a_death(self):
        """A named seat quiet for hours is between panes: the ledger is
        durable and its beacon replays the row, so the door still admits it.
        So does a named seat whose row records no beat at all (UNKNOWN)."""
        self._plant("seat-between-panes", 6 * 3600)
        row, why, _sent = self._send("seat-between-panes")
        self.assertIsNotNone(row, why)
        self._plant("seat-no-beat", None)
        row, why, _sent = self._send("seat-no-beat")
        self.assertIsNotNone(row, why)

    def test_a_long_dead_named_seat_is_refused_too(self):
        self._plant("tmp-claude-7", 3 * DAY)
        seats.write_roster("demo-claude-2", presence_beat=False)
        row, why, _sent = self._send("tmp-claude-7")
        self.assertIsNone(row)
        self.assertIn("DEAD", why)
        self.assertIn("demo-claude-2", why)

    def test_force_still_files_for_a_known_offline_seat(self):
        self._fleet()
        row, why, _sent = self._send("claude", force=True)
        self.assertIsNotNone(row, why)
        self.assertEqual(row["recipient"], "claude")

    def test_add_is_the_same_door(self):
        self._fleet()
        row, why = dispatches.add("claude", "rollout-client-gone-1957",
                                  ref=self.a, repo=self.repo, kind="build",
                                  new_work=True, notify=False, _reason=True)
        self.assertIsNone(row)
        self.assertIn("DEAD", why)
        row, why = dispatches.add("claude", "rollout-client-gone-1957",
                                  ref=self.a, repo=self.repo, kind="build",
                                  new_work=True, notify=False, force=True,
                                  _reason=True)
        self.assertIsNotNone(row, why)

    def test_the_cli_refuses_and_force_opens_it(self):
        self._fleet()
        argv = ["send", "claude", "rollout-client-gone-1957", self.BRIEF,
                "--ref", self.a, "--repo", self.repo, "--kind", "build",
                "--new-work"]
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "post-1"}, None)):
            rc, _out, err = run(dispatches.cmd_dispatch, argv)
            self.assertNotEqual(rc, 0)
            self.assertIn("did you mean demo-claude-2", err)
            self.assertEqual(dispatches.open_rows(), [])
            rc, _out, err = run(dispatches.cmd_dispatch, argv + ["--force"])
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["recipient"] for r in dispatches.open_rows()],
                         ["claude"])


class MoveDoorsTest(DispatchBase):
    """THE MOVE DOORS — rebind, seat reassign's preflight and retract
    --reissue — pass no force to add(), so the DEAD refusal names a remedy
    each of them can run (review F3), and a family-named seat between panes
    is as reachable through them as through send (review F1)."""

    _plant = DeadRecipientDoorTest._plant

    def _open_row(self):
        seats.write_roster("demo-claude-2", presence_beat=False)
        row = dispatches.add("demo-claude-2", "rollout-client-gone-1957",
                             ref=self.a, repo=self.repo, kind="review",
                             new_work=True, notify=False)
        self.assertIsNotNone(row)
        return row

    def test_rebind_to_a_DEAD_seat_names_a_runnable_remedy(self):
        row = self._open_row()
        self._plant("tmp-claude-7", 3 * DAY)
        new, why = dispatches.rebind(row["id"], "tmp-claude-7",
                                     reason="reviewer gone", force=True,
                                     notify=False)
        self.assertIsNone(new)
        self.assertIn("DEAD", why)
        self.assertNotIn("Pass `--force`", why,
                         "rebind's --force cannot open this door")
        self.assertNotIn("force=True", why)
        self.assertIn("helm dispatch cancel '<id>' '<reason>'", why)
        self._assert_runnable_send(why)

    def test_rebind_to_a_family_named_seat_between_panes_moves_the_row(self):
        row = self._open_row()
        self._plant("kimi", 2 * 3600)
        new, why = dispatches.rebind(row["id"], "kimi", reason="reroute",
                                     force=True, notify=False)
        self.assertIsNotNone(new, why)
        self.assertEqual(new["new"]["recipient"], "kimi")

    def test_the_reassign_preflight_is_the_rebind_door(self):
        from helm import seat_reassign
        seats.write_roster("demo-claude-2", presence_beat=False)
        self._plant("ds4pro", 2 * 3600)
        self.assertIsNone(seat_reassign._rebind_rung("ds4pro", "reroute"))
        self._plant("tmp-claude-7", 3 * DAY)
        why = seat_reassign._rebind_rung("tmp-claude-7", "reroute")
        self.assertIn("DEAD", why)
        self.assertNotIn("Pass `--force`", why)
        self._assert_runnable_send(why)

    def _assert_runnable_send(self, why):
        # A printed remedy is a command the reader can run, never an
        # elided `send ... --force` (test_instructions_are_runnable).
        self.assertNotIn("dispatch send ...", why)
        self.assertIn("helm dispatch send '<recipient>' '<lane>' '<brief>' "
                      "--ref '<tip>' --kind '<build|review>' "
                      "--supersedes '<id>' --force`", why)
        self.assertEqual(len(assert_remedies_parse(self, why, self.a)), 2)


class ReissueDoorTest(RetractBase):
    """retract --reissue re-requests the review of the same seat through
    add() with no force: its refusal says how to reissue by hand."""

    _plant = DeadRecipientDoorTest._plant

    def test_reissue_to_a_DEAD_reviewer_names_a_runnable_remedy(self):
        row = self.verdicted("fix")              # an empty roster admits it
        self._plant(self.REVIEWER, 3 * DAY)
        seats.write_roster("demo-claude-2", presence_beat=False)
        out, why = self.retract(row["id"], reissue=True)
        self.assertIsNone(out)
        self.assertIn("DEAD", why)
        self.assertNotIn("Pass `--force`", why)
        self.assertNotIn("force=True", why)
        self.assertIn("without --reissue", why)
        self.assertNotIn("dispatch send ...", why)
        self.assertIn("helm dispatch send '<recipient>' '<lane>' '<brief>' "
                      "--ref '<tip>' --kind review --supersedes '<id>' "
                      "--force`", why)
        self.assertEqual(len(assert_remedies_parse(self, why, self.a)), 1)


#: A real value for each placeholder a DEAD remedy prints (`<tip>` is the
#: fixture's own commit, filled per call).
_FILLS = {"<recipient>": "demo-claude-2",
          "<lane>": "rollout-client-gone-1957",
          "<brief>": "please read the tip and record a verdict",
          "<build|review>": "review", "<id>": "deadbeef",
          "<reason>": "reviewer gone"}


def _unquoted(line):
    """The characters of `line` outside shell quotes."""
    out, quote = [], None
    for ch in line:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        else:
            out.append(ch)
    return "".join(out)


def assert_remedies_parse(case, why, tip):
    """EVERY `helm dispatch ...` COMMAND A REMEDY PRINTS RUNS ONCE ITS
    PLACEHOLDERS ARE FILLED (task/3531 N1). The printed send omitted its
    brief, and a bare
    `<id>` is a redirect to bash. Each command carries no angle bracket
    outside quotes, and with real values it is run through bin/helm in the
    fixture's temp home: it may refuse (no such row), never with a usage
    error. Returns the commands checked."""
    import re
    import shlex
    import subprocess
    import sys
    helm = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bin", "helm")
    fills = dict(_FILLS, **{"<tip>": tip})
    env = dict(os.environ, HELM_LANE_COORDINATION="1")
    # The commands to paste, not a verb named in prose (`helm seat reassign`).
    cmds = re.findall(r"`(helm dispatch [^`]*)`", why)
    for cmd in cmds:
        bare = _unquoted(cmd)
        case.assertNotIn("<", bare, cmd)
        case.assertNotIn(">", bare, cmd)
        argv = [fills.get(a, a) for a in shlex.split(cmd)]
        case.assertFalse([a for a in argv if "<" in a], argv)
        proc = subprocess.run([sys.executable, helm] + argv[1:],
                              cwd=case.repo, env=env, capture_output=True,
                              text=True, timeout=120)
        case.assertFalse(proc.returncode == 2 and "usage" in proc.stderr,
                         "%s\n%s" % (argv, proc.stderr))
    return cmds


def _row(rid, sender, recipient, ts="2026-09-28T00:00:00Z"):
    return {"id": rid, "sender": sender, "recipient": recipient,
            "lane": "rollout-client-gone-1957", "status": "open",
            "delivery": "observed", "ts": ts, "deadline_s": 2700,
            "kind": "review"}


def _live():
    return {"last_seen": time.time()}


def _dead():
    return {"last_seen": time.time() - 30 * DAY}


def _roster(sender_state):
    """bonsai live in the Claude Code harness with a family of its own, the
    reviewer live, `claude` a month dark, and qwenlocal in `sender_state`."""
    roster = {"bonsai": dict(_live(), runtime={"family": "bonsai",
                                               "harness": "claude"}),
              "demo-claude-2": _live(), "claude": _dead()}
    if sender_state == "live":
        roster["qwenlocal"] = _live()
    elif sender_state == "absent":
        roster["qwenlocal"] = _dead()
    return roster


class BonsaiNeverSeesClaudesRowTest(unittest.TestCase):
    """THE STOP-GUARD: a seat is shown only rows it is a party to."""

    def _pick(self, rows, seat, roster, roster_failed=False):
        snap = {r["id"]: r for r in rows}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(snap, None)), \
             mock.patch.object(dispatches, "_open", return_value=True), \
             mock.patch.object(dispatches, "_is_overdue", return_value=True), \
             mock.patch.object(seats, "roster_checked",
                               return_value=(roster, roster_failed)):
            r, _kind, _unavailable = dispatches.stop_candidate(seat=seat)
        return None if r is None else r["id"]

    def test_the_bonsai_case_for_every_state_of_the_sender(self):
        """qwenlocal's row to `claude` reaches bonsai under no reading of
        qwenlocal: live, absent, unrostered, or behind an unreadable roster."""
        row = _row("r1957", "qwenlocal", "claude")
        for state in ("live", "absent", "unrostered", "unreadable"):
            with self.subTest(sender=state):
                self.assertIsNone(self._pick(
                    [row], "bonsai", _roster(state),
                    roster_failed=state == "unreadable"))

    def test_a_floor_sender_row_between_two_other_seats_is_not_mine(self):
        self.assertIsNone(self._pick([_row("legacy", "claude", "kimi")],
                                     "bonsai", _roster("live")))

    def test_the_sender_still_sees_its_own_row(self):
        self.assertEqual(self._pick([_row("r1957", "qwenlocal", "claude")],
                                    "qwenlocal", _roster("live")),
                         "r1957")

    def test_an_orphaned_row_still_reaches_its_recipient(self):
        """The net is kept for the one seat that can discharge the row."""
        row = _row("r1957", "qwenlocal", "bonsai")
        for state in ("absent", "unrostered", "unreadable"):
            with self.subTest(sender=state):
                self.assertEqual(self._pick(
                    [row], "bonsai", _roster(state),
                    roster_failed=state == "unreadable"), "r1957")

    def test_a_live_senders_row_stays_the_senders(self):
        """The recipient already has its own rung (owed_to); a live sender
        chases its own delivery leg."""
        self.assertIsNone(self._pick([_row("r1957", "qwenlocal", "bonsai")],
                                     "bonsai", _roster("live")))

    def test_the_recipient_rung_never_reads_a_family_as_a_seat(self):
        snap = {"r1957": _row("r1957", "qwenlocal", "claude")}
        with mock.patch.object(dispatches, "owed", return_value=list(
                snap.values())):
            rows, why = dispatches.owed_to("bonsai", snap=(snap, None),
                                           live={})
        self.assertIsNone(why)
        self.assertEqual(rows, [])



class BonsaiStopRungTest(DispatchBase):
    """End to end through the rung bonsai's stop runs, with its own declared
    name resolved by the real identity law, the Claude Code harness in its
    environment, and a family of its own."""

    def test_the_rung_itself_under_the_claude_code_harness(self):
        from helm import seats_stop_signals
        env = {"HELM_CHAT_NAME": "bonsai", "CLAUDECODE": "1",
               "CLAUDE_CODE_SUBAGENT_MODEL": "bonsai"}
        roster = _roster("absent")
        snap = ({"r1957": _row("r1957", "qwenlocal", "claude")}, None)
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(dispatches, "_open", return_value=True), \
             mock.patch.object(dispatches, "_is_overdue", return_value=True), \
             mock.patch.object(seats, "roster_checked",
                               return_value=(roster, False)):
            self.assertEqual(seats_stop_signals.acting_seat(), "bonsai")
            got = seats_stop_signals._dispatch_candidate(snap)
            self.assertIsNone(got, "bonsai was shown qwenlocal's row to "
                                   "claude: %r" % (got,))
            # CONTROL: the same rung, the same row, from its sender's stop
            os.environ["HELM_CHAT_NAME"] = "qwenlocal"
            got = seats_stop_signals._dispatch_candidate(snap)
        self.assertIsNotNone(got)
        self.assertIn("NEEDS CHECK-IN (OVERDUE)", got[1])


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
