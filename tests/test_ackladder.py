#!/usr/bin/env python3
"""AX primitive #3 — the ACK / CONSUME LADDER (SENT != SEEN != ACTED).

Hermetic like test_seats: HELM_HOME + HELM_CHAT_DIR are tmp dirs, the signed
transport is killed (HELM_CHAT_NODE_URL set-but-empty), owner names pinned,
and every ambient session/model mark scrubbed so no live harness leaks into
derive_seat/whoname. Seats join with a NON-git tmp cwd so they stay un-homed
(all-room legacy scope) — the ladder is tested off the cursor, not homing."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pk, seats  # noqa: E402
from tests._tmphome import declare as _tmp_declare  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_CELL_BIN",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")


class LadderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ack-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def join(self, seat, session=None):
        seats.join(session=session or ("sess-" + seat), seat=seat,
                   cwd=self.tmp)      # tmp is not a git repo => un-homed

    def prow(self, out, name):
        """The pending ROW for `name`, never the legend.

        The legend names every state unconditionally, so a bare
        assertIn("UNRES", out) is satisfied by the key at the bottom of the
        screen no matter what the row above it renders. An assertion about a
        state belongs to the ROW that carries it, never to the buffer."""
        # MATCH THE RECIPIENT COLUMN, NEVER THE WHOLE LINE. A state row ends
        # in the MESSAGE TEXT, so a body that happens to mention another seat
        # made this helper return the wrong row and every assertion after it
        # describe a row the test did not mean — measured by a reviewer.
        # Two formats, and the split is exact rather than heuristic: a state
        # row renders the recipient immediately after the arrow, and an UNKN
        # row renders NO message text at all (its trailing prose is fixed),
        # so a bare name match is safe only there.
        def _addressee(ln):
            """The recipient FIELD, so a prefix cannot impersonate a name."""
            if "\u2192 " in ln:          # state row: recipient follows the arrow
                tail = ln.split("\u2192 ", 1)[1].split()
                return tail[0] if tail else None
            return None
        rows = [ln for ln in out.splitlines()
                if _addressee(ln) == name
                or (ln.lstrip().startswith("\u26a0 UNKN")
                    and name in ln.split())]
        self.assertEqual(len(rows), 1,
                         "expected exactly one pending row for %r, got %d:\n%s"
                         % (name, len(rows), out))
        return rows[0]

    def mask_old(self, *seats_):
        """Freeze a seat's .seen mtime in the distant past so the SEEN
        derivation can only come from the delivery CURSOR — isolates the
        touch_seen fallback out of the cursor tests."""
        old = time.time() - 100000
        for s in seats_:
            os.utime(seats.seen_path(s), (old, old))

    def states(self, sender="senderS"):
        items, _total, _faults = seats.pending(seat=sender)
        return sorted((i["to"], i["state"]) for i in items)

    def run_cmd(self, verb, args=()):
        out, err = io.StringIO(), io.StringIO()
        fake = types.SimpleNamespace(buffer=io.BytesIO(b"{}"))
        with mock.patch.object(sys, "stdin", fake), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), "main")
        return rc, out.getvalue(), err.getvalue()


class SentSeenTests(LadderBase):
    def test_sent_then_seen_off_the_cursor(self):
        """A DM is SENT-not-SEEN until the recipient's cursor passes it; then
        SEEN — proven off the CURSOR alone (seen mtime re-masked to the past
        after delivery, so the fallback cannot be the thing that flipped it)."""
        row, err = seats.dm("recipT", "please review the tip", who="senderS")
        self.assertIsNone(err)
        self.join("recipT")
        self.mask_old("recipT")
        self.assertEqual(self.states(), [("recipT", "sent")])

        seats.deliver(session="sess-recipT", room=seats.dm_lane("recipT"),
                      seat="recipT", emit=lambda ln: None)
        self.mask_old("recipT")           # SEEN must now come from the cursor
        self.assertEqual(self.states(), [("recipT", "seen")])

    def test_no_cursor_no_ground_stays_sent(self):
        """A recipient that never joined has no cursor and no fresh presence —
        the row is SENT (stranded), never falsely SEEN."""
        seats.dm("ghost", "anyone home?", who="senderS")
        # ghost never joined: no cursor, no .seen file
        self.assertEqual(self.states(), [("ghost", "sent")])

    def test_touch_seen_fallback_later_second_is_seen(self):
        """The second SEEN signal: activity in a strictly-later second than
        the row (no cursor movement at all). Same-second activity does NOT
        count — it guards against the row's own post-second."""
        row, _ = seats.dm("recipT", "hi", who="senderS")
        self.join("recipT")
        mts = seats._ts_epoch(row["ts"])
        os.utime(seats.seen_path("recipT"), (mts + 5, mts + 5))
        st, _a = seats.consume_state(row, seats.dm_lane("recipT"), "recipT")
        self.assertEqual(st, "seen")
        os.utime(seats.seen_path("recipT"), (mts, mts))    # same second
        st, _a = seats.consume_state(row, seats.dm_lane("recipT"), "recipT")
        self.assertEqual(st, "sent")

    def test_pre_join_room_mention_reads_BASELINED_not_SENT(self):  # noqa: VACUOUS_ASSERTION — the empty `surfaced` list now HAS its positive pole on the same observable: a later ADDRESSED post is delivered through the same emit callback and asserted non-empty, so a dead wiring reddens this arm. The pole sits below rather than above because it must not exist while prow() reads the render (that helper demands exactly one row per recipient, and the control row gives recipT a second)
        """A ROOM @mention posted BEFORE the recipient joins. Join baselines
        the room cursor at EOF (pre-join backlog never floods), so the mention
        sits at/below that JOIN BASELINE — deliver never surfaces it and never
        will.

        THE STATE WAS RENAMED AND THE LAW DID NOT MOVE. This arm was written
        as stays_SENT, and SENT was the closest state that existed at the time
        — its real content was NOT falsely SEEN, off neither the EOF baseline
        cursor nor the touch_seen beat, and that content is asserted unchanged
        below. But SENT carries a remedy — the recipient acks it — and the
        recipient cannot ack a row deliver never showed them. task/914 item 4:
        BASELINED is the state whose remedy is SENDER-side. This is
        coverage hole: every other room-mention test joins the recipient
        FIRST, and the one pre-join test uses a DM (which baselines at 0 and
        dodges the EOF skip). It FAILED before the join-baseline gate."""
        m = chat.post("@recipT urgent — before you joined", room="main",
                      who="senderS")
        self.join("recipT")            # baselines main cursor at EOF > mention

        surfaced = []                  # deliver surfaces nothing: below base
        seats.deliver(session="sess-recipT", room="main", seat="recipT",
                      emit=lambda ln: surfaced.append(ln))
        self.assertEqual(surfaced, [])

        # cursor-SEEN cannot fire: the row is at/below the join baseline
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "baselined")
        # nor may touch_seen: recipT LIVE a strictly-later second than the row
        # still is not SEEN (both SEEN paths gated on the join baseline)
        mts = seats._ts_epoch(m["ts"])
        os.utime(seats.seen_path("recipT"), (mts + 5, mts + 5))
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "baselined")
        # and it stays that way even AFTER recipT consumes a post-join row (the
        # cursor's active flag flips True but base pins the stranded mention)
        # AND THE REMEDY IT PRINTS IS THE SENDER'S. The whole point of the
        # state: SENT told the sender the RECIPIENT would ack a row deliver
        # never showed them, so the obligation named a clearer who could not
        # clear it. Assert the row AND that the ack remedy is not offered.
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        row = self.prow(out, "recipT")
        self.assertIn("BASED", row)
        self.assertIn("repost", out)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE as the empty `surfaced`
        # above — and it took two tries, which is the point of running it:
        # unaddressed chatter emits NOTHING (deliver surfaces only addressed
        # rows), so the first version of this control was itself vacuous. An
        # ADDRESSED post above the baseline DOES emit, which is what proves
        # the earlier empty list was the baseline skipping the row rather
        # than a dead emit wiring.
        chat.post("@recipT a later addressed row", room="main", who="senderS")
        later = []
        seats.deliver(session="sess-recipT", room="main", seat="recipT",
                      emit=lambda ln: later.append(ln))
        self.assertTrue(later, "deliver emitted nothing at all, so the empty "
                               "surfaced list above proved nothing")
        # CONTAINMENT rather than equality now: the control row legitimately
        # adds a second obligation, and the property under test is that the
        # baselined one SURVIVES a later consumption — the cursor's active
        # flag flips True while base still pins the stranded mention.
        self.assertIn(("recipT", "baselined"), self.states())

    def test_rotation_voids_join_baseline_no_false_SENT(self):
        """A room-file ROTATION (replacement → new inode) must VOID the join
        baseline: it indexes the now-gone old file. recipT joins main at a high
        EOF (base = that offset); main is then replaced by a fresh, SMALLER file
        whose @recipT mention deliver actually surfaces. Its end offset sits
        BELOW the stale old-file base, so a carried-forward base would gate the
        cursor-SEEN off and false-SENT a just-delivered row. deliver re-baselines
        base to 0 on the new inode, so the delivered row reads SEEN. Fail-safe
        (over-reports SENT, never a false clear) — but still a wart it closes."""
        for i in range(12):
            chat.post("filler %d — push main EOF well past one row" % i,
                      room="main", who="senderS")
        self.join("recipT")                     # base = high pre-rotation EOF
        old_ino = seats._recipient_cursor("main", "recipT")[0]["ino"]

        # Replace main with a fresh, smaller file (new inode) carrying a fresh
        # mention. os.replace GUARANTEES a distinct inode — a bare remove+create
        # can REUSE the freed inode (ext4), which would dodge the dev,ino branch.
        row = chat.post("@recipT after the rotation", room="rotsrc",
                        who="senderS")
        os.replace(chat.room_path("rotsrc"), chat.room_path("main"))
        self.assertNotEqual(os.stat(chat.room_path("main")).st_ino, old_ino)

        surfaced = []                           # the new-file mention delivers
        seats.deliver(session="sess-recipT", room="main", seat="recipT",
                      emit=lambda ln: surfaced.append(ln))
        self.assertTrue(surfaced)
        self.assertEqual(seats._recipient_cursor("main", "recipT")[0]["base"], 0)

        self.mask_old("recipT")                 # SEEN must come from the CURSOR
        st, _a = seats.consume_state(row, "main", "recipT")
        self.assertEqual(st, "seen")            # not false-SENT off a stale base

    def test_never_joined_seat_mention_stays_visible(self):
        """A ROOM @mention of a seat that never joined has no cursor ground
        and is not even in the roster — it must NOT vanish from pending (the
        most-stranded case). It surfaces distinctly as 'unresolved', never a
        silent drop, never a false SEEN off a global presence beat."""
        chat.post("@ghostSeat are you there?", room="main", who="senderS")
        self.assertEqual(self.states(), [("ghostSeat", "unresolved")])
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("UNRES", self.prow(out, "ghostSeat"))
        self.assertNotIn("nothing outbound is waiting", out)

    def test_an_unresolved_name_is_not_rendered_as_a_dead_one(self):
        """The state means "this ROSTER could not ground the name".
        It was rendered `⊘ GONE = no seat answers that @name (typo / never
        online)`, which is a claim about the WORLD, and the two coincide only
        if the roster is a complete census of who exists. It is not: a row is
        created solely by an explicit `helm chat join` and nothing repairs a
        missing one, so every seat is unresolvable for the window
        between spawning and joining.

        THE CONTROL IS LIVE, NOT THEORETICAL: a seat answered a row ONE
        MINUTE after this surface printed GONE for it. The word was wrong
        about a seat that was working the whole time.
        """      # noqa: VACUOUS_ASSERTION — positive control is assertIn("UNRES"); GONE's absence is asserted only after the row is proven rendered
        chat.post("@ghostSeat are you there?", room="main", who="senderS")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        row = self.prow(out, "ghostSeat")
        self.assertIn("UNRES", row)              # the ROW says unresolved...
        self.assertNotIn("GONE", row)            # ...and never says dead
        self.assertNotIn("GONE", out)            # nor does the legend now
        self.assertNotIn("no such seat", out)    # nor the header
        # and it says what would make it resolvable, which is the whole
        # difference between a diagnosis and an accusation
        self.assertIn("has not joined", out)
        self.assertIn("helm chat join", out)

    def test_the_SAME_name_stops_being_unresolved_once_it_joins(self):  # noqa: VACUOUS_ASSERTION — positive control is assertIn("BASED", row) on the SAME observable, and prow() asserts exactly one row exists
        """The must-miss control, and the one that proves the render tracks
        RESOLVABILITY rather than some property of the name itself. Identical
        mention, identical sender, identical room — the only difference is
        that the recipient joined. An UNRES that fired unconditionally would
        satisfy the arm above while telling every sender their working
        recipients are unreachable."""
        chat.post("@ghostSeat are you there?", room="main", who="senderS")
        self.assertEqual(self.states(), [("ghostSeat", "unresolved")])
        self.join("ghostSeat")                   # the one thing that changes
        self.assertNotEqual(self.states(), [("ghostSeat", "unresolved")],
                            "joining did not resolve the name; the arm below "
                            "would pass for the wrong reason")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        row = self.prow(out, "ghostSeat")
        self.assertNotIn("UNRES", row)           # the ROW, not the legend
        # IT BECAME AN ORDINARY ROW, AND ORDINARY HERE IS **BASED**, NOT SENT.
        # The mention was posted BEFORE the join, so it sits below the new
        # recipient's join baseline — the state whose remedy is sender-side,
        # pinned in its own right by test_pre_join_room_mention_reads_
        # BASELINED_not_SENT. This arm asserted SENT while that one asserted
        # BASELINED for the same ordering, so the pair CONTRADICTED each other
        # and the suite went red the moment BASELINED stopped being folded into
        # SENT. That contradiction is the finding: a semantic change was
        # applied to one arm and left standing in its sibling.
        # What this test owns is RESOLVABILITY, not which delivered state
        # follows — so it asserts the real one rather than a stale proxy.
        self.assertIn("BASED", row)

    def test_an_UNREADABLE_roster_reads_UNKNOWN_not_unresolved(self):
        """A reviewer's finding, and the deeper half of this whole change.

        UNRES asserts "no roster row answers that @name". That is only honest
        if the roster was READ. seats_common.roster() is pk.read_json, which
        swallows missing, unreadable, malformed and wrong-shaped alike and
        returns {} — it never raises. So one unreadable roster made every
        mention render as an address nobody answers, and the new wording would
        have asserted "no roster row" from the one moment it had no evidence.
        Curing GONE to UNRES without this replaces a confident wrong word with
        a confident wrong word.

        roster_checked separates what the fail-open reader merges, and this
        fixture writes REAL malformed bytes rather than mocking an exception
        the production path cannot deliver."""
        chat.post("@ghostSeat are you there?", room="main", who="senderS")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        row = self.prow(out, "ghostSeat")
        self.assertIn("UNKN", row)               # the row IS rendered...
        self.assertIn("roster unreadable", row)  # ...and names WHICH input
        self.assertNotIn("UNRES", row)           # never absence from a blind read

    def test_MISSING_and_UNREADABLE_rosters_do_NOT_render_the_same(self):  # noqa: VACUOUS_ASSERTION — each absence is paired with an assertIn on the SAME row (UNRES on missing, UNKN on unreadable), prow() asserts exactly one row exists, and the closing assertNotEqual is unconditional
        """EXECUTING THE OTHER HALF OF _seat_checked's DOCSTRING.

        That docstring claims roster_checked "draws the line the fail-open
        reader erases: a MISSING file is a proven-empty roster (failed False,
        so a genuine miss still reads as a miss); only unreadable or malformed
        state is failed." The unreadable half has its own arm. This one
        executes the MISSING half, and it is the quiet direction: if missing
        were also treated as failed, every unresolved mention would render
        UNKN, UNRES would never fire for anyone, and nothing would go red —
        a silent regression behind a green suite.

        One fixture, two states, differing only in whether the file exists,
        because asserting each separately cannot show they are DISTINGUISHED.
        """
        chat.post("@ghostSeat are you there?", room="main", who="senderS")

        try:                                   # MISSING: proven empty
            os.unlink(seats.roster_path())
        except FileNotFoundError:
            pass
        self.assertFalse(os.path.exists(seats.roster_path()),
                         "fixture did not produce the missing case")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        missing_row = self.prow(out, "ghostSeat")
        self.assertIn("UNRES", missing_row)
        self.assertNotIn("UNKN", missing_row)

        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")      # UNREADABLE: failed probe
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        unreadable_row = self.prow(out, "ghostSeat")
        self.assertIn("UNKN", unreadable_row)
        self.assertNotIn("UNRES", unreadable_row)

        self.assertNotEqual(
            missing_row, unreadable_row,
            "missing and unreadable rendered identically — the distinction "
            "roster_checked exists to draw is not reaching the surface")

    def test_a_corrupt_roster_never_produces_a_FALSE_ALL_CLEAR(self):
        """A reply-only recipient must not VANISH when the roster is corrupt.

        Worse than any wording this change set out to fix. `_recipients`
        builds its key set from the fail-open roster, so an unreadable roster
        empties it — and a reply whose recipient is the parent's AUTHOR, with
        no @mention anywhere in the text, then matched nothing and was dropped
        before consume_state could answer. Measured: total went 1 -> 0 and
        pending printed "nothing outbound is waiting — every addressed
        message you sent is consumed (acted)". A false all-clear, with a
        checkmark, for every sender at once.

        The mention branch already survived this through include_unresolved;
        the reply-parent did not. This pins that a real obligation stays
        VISIBLE across both roster states."""
        self.join("recipR")
        parent = chat.post("here is my question", room="main", who="recipR")
        chat.post("answering you", room="main", who="senderS",
                  reply_to=parent["id"])

        healthy = self.states()
        self.assertEqual(healthy, [("recipR", "sent")],
                         "fixture did not produce a reply-only obligation; "
                         "nothing below is tested")

        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        items, total, _faults = seats.pending(seat="senderS")
        self.assertEqual(total, 1,
                         "the obligation VANISHED on a corrupt roster — a "
                         "false all-clear, which is what this arm exists for")
        self.assertEqual([i["to"] for i in items], ["recipR"])

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertNotIn("nothing outbound is waiting", out)
        self.assertIn("recipR", out)

    def test_prow_does_not_match_a_name_that_only_appears_in_the_BODY(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is assertIn("recipR", out): the name IS on screen, in the body, so prow refusing to return a row for it is the measured behaviour and not an empty render
        """The helper itself, because a reviewer found it returning the wrong
        row. A state row ends in the MESSAGE TEXT, so a body mentioning
        another seat made a whole-line match hand back that row, and every
        assertion after it then described a row the test did not mean —
        silently, and in the direction that makes an arm look green.

        Here the only row is addressed to ghostSeat and its BODY names
        recipR. prow("recipR") must find nothing; prow("ghostSeat") must find
        the row."""
        chat.post("@ghostSeat please ask recipR about it",
                  room="main", who="senderS")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("recipR", out,
                      "the body text is not on screen; this arm would be "
                      "vacuous")                      # must-hit
        self.assertIn("ghostSeat", self.prow(out, "ghostSeat"))
        with self.assertRaises(AssertionError):
            self.prow(out, "recipR")   # addressed to nobody of that name

    def test_a_capped_list_SAYS_it_dropped_rows(self):
        """pending() returns items[:cap] alongside the FULL count, so past the
        cap the header's total described everything while the breakdown beside
        it described only what fits — two numbers on one line measuring
        different sets, with nothing saying rows had been dropped.

        A bounded surface has to name what it left out, or it reads as
        complete to the one reader who most needs to know it is not."""
        for i in range(55):                       # more rows than the cap
            chat.post("@ghost%d ping" % i, room="main", who="senderS")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        header = out.splitlines()[0]
        # THE PROPERTY IS THE TRUE TOTAL; the unit word moved under it.
        # `items` is one entry per (recipient, row), so a broadcast to five
        # seats is five entries — calling them "rows" overstated the backlog
        # by the fan-out, and this arm's literal was pinning that misstatement
        # in place. Edited by task/2443's lane, which renamed the unit; the
        # assertion below is the same property against the corrected word.
        self.assertIn("55 addressed deliveries", header)   # the true total...
        self.assertIn("not listed", header)          # ...and the shortfall
        self.assertIn("5 more", header)

        # MUST-MISS, and without it this arm asserts only the SOMETIMES
        # effect. A guard with a sometimes-effect and an always-effect needs
        # both poles: if the shortfall were printed unconditionally — "0 more
        # not listed" on every render — every assertion above still passes
        # while every ordinary pending view carries a false claim that rows
        # were dropped. The disclosure has to be absent when nothing WAS.
        for i in range(3):
            chat.post("@short%d ping" % i, room="main", who="senderT")
        rc, out2, _ = self.run_cmd("pending", ["--seat", "senderT"])
        self.assertEqual(rc, 0)
        short_header = out2.splitlines()[0]
        self.assertIn("addressed deliveries", short_header)  # must-hit: it rendered
        self.assertNotIn("not listed", short_header,
                         "a list that dropped nothing announced a shortfall")
        # THE DIRECTION IS ON THE RUNNING LINE, task/2443. Three places in the
        # tree said OUTBOUND — the help text, the stray-selector refusal, and
        # the all-clear — and all three are unreachable while a non-empty list
        # renders, which is the only moment a reader can confuse this with
        # their inbox. A helm USER compared this number against `catchup`'s
        # "parked N" and read the difference as a defect; the two sets are
        # disjoint by construction and cannot be reconciled.
        for h in (header, short_header):
            self.assertIn("OUTBOUND", h,
                          "the running line does not state its direction, so "
                          "it reads as an inbox to anyone without the helm "
                          "checkout: %r" % h)
            self.assertIn("NOT your inbox", h,
                          "the line does not say what it is NOT, which is the "
                          "half that stops the comparison against catchup")
            # AND THE POINTER MUST BE TRUE ABOUT THE VERB IT NAMES. The first
            # cut of this line said catchup "parks" the inbound backlog;
            # `catchup` takes apply=False by default and protects mentions
            # behind --including-mentions, so a bare run parks NOTHING. A
            # cure for a misleading surface that misleads about a sibling
            # surface has moved the defect rather than fixed it.
            self.assertIn("PREVIEWS", h,
                          "the pointer claims catchup parks by default, which "
                          "is false — it previews unless --apply is given")
            self.assertIn("--apply", h,
                          "the pointer names no way to actually park, so a "
                          "reader who follows it gets a preview and no change")

    def test_UNREADABLE_LANES_do_not_consume_the_MESSAGE_cap(self):
        """Measured on the exact tip: 50 unreadable lanes plus ONE
        real unresolved row rendered `51 addressed rows … showing the 50
        oldest`, and the row that did not fit was the only actionable one.

        THREE FALSE THINGS AT ONCE, all from one list holding two populations.
        Lane sentinels were counted as addressed rows. They carry no
        timestamp, so `str(None or "")` sorted them AHEAD of every real row
        while the header called them the oldest. And by filling items[:cap]
        first, diagnostic uncertainty crowded out the work it exists to
        annotate — the surface went blindest exactly when it was loudest.

        The cap belongs to MESSAGES. The cap arm above cannot see any of this:
        its rows are homogeneous and timestamped, so no error BETWEEN the two
        populations can show up in it."""
        chat.post("@zzRealRecipient please look", room="main", who="senderS")
        for i in range(55):                  # >= cap, every one unreadable
            os.makedirs(chat.room_path("zzdead%02d" % i), exist_ok=True)

        items, total, faults = seats.pending(seat="senderS")
        # MUST-HIT: the fixture really created the condition. Without it every
        # assertion below passes on a box with no unreadable lane at all.
        self.assertGreaterEqual(len(faults), 55,
                                "the fixture created no unreadable lanes")
        # AND THE FAULT CARRIES PROVENANCE, which is the whole reason it is a
        # structured value and not a bare `failed` flag: a renderer that has
        # to rediscover which input broke will collapse them all into one
        # generic UNKNOWN, the mislabel this ladder already shipped once.
        self.assertEqual({f.kind for f in faults}, {"lane"})
        self.assertTrue(all(f.where.startswith("zzdead") for f in faults))
        self.assertEqual({f.reason for f in faults}, {"not-a-file"})
        self.assertEqual(total, 1, "lane diagnostics counted as messages")
        self.assertEqual([i["to"] for i in items], ["zzRealRecipient"],
                         "the real obligation was crowded out by diagnostics")

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        header = out.splitlines()[0]
        # Singular unit, renamed with its plural by task/2443: the count is
        # (recipient, row) pairs, not rows. The property — that 50 unreadable
        # lanes did not consume the cap and the ONE real row still renders —
        # is unchanged.
        self.assertIn("1 addressed delivery ", header)
        self.assertNotIn("oldest", header,
                         "claimed an age ordering over undated entries")
        self.prow(out, "zzRealRecipient")     # VISIBLE, not merely counted
        self.assertIn("THIS VIEW IS INCOMPLETE", out,
                      "the diagnostics vanished instead of moving aside")
        self.assertIn("not-a-file", out,
                      "the screen named no reason the reader could act on")

    def test_a_MALFORMED_ROW_reads_UNKNOWN_instead_of_CRASHING(self):
        """A REAL FILE, never a raising mock: production cannot deliver the
        exception a mock injects, and an arm built on one certifies a handler
        for a state that never occurs.

        `{"recipR": []}` is a PARSEABLE file with an unreadable row. The
        cursor lookup read it through the FAIL-OPEN `roster()`, called `.get`
        on a list, and took the WHOLE COMMAND down with an AttributeError
        (exact tip) — every other pending row lost to one bad row.
        pending() now threads the CHECKED snapshot, so that reader never sees
        an unvalidated row, and the state is UNKNOWN.

        AND THE DIAGNOSIS IS FILE-LEVEL ON PURPOSE. I first wrote this arm
        expecting a row-level `why`, which failed: roster_checked fails the
        WHOLE file on any invalid row (seats_roster.py:84), so there is no
        such state as one bad row in a good file, and the branch I had added
        for it was unreachable. The arm is what measured that."""
        chat.post("@recipR please look", room="main", who="senderS")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write('{"recipR": []}')       # after the post: it makes the dir

        items, total, _faults = seats.pending(seat="senderS")  # must not raise
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["state"], "unknown",
                         "a malformed row read as a healthy seat")
        self.assertEqual(items[0]["why"], "roster unreadable",
                         "the unknown did not name which input failed")
        # CONTROL: the same row against a VALID roster is not unknown, so the
        # verdict above is the malformed file and not this arm's fixture.
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write('{"recipR": {"session": "s1"}}')
        ok, _t, _l = seats.pending(seat="senderS")
        self.assertNotEqual(ok[0]["state"], "unknown",
                            "control: this row is unknown even when readable")

    def test_an_UNREADABLE_DM_NAMESPACE_is_not_a_fleet_with_no_DMs(self):
        """Injecting a read failure on <chat>/dm ALONE turned one
        genuinely pending DM into total=0/items=[] and the CLI printed the
        green all-clear. _dm_lanes answered an unreadable namespace with the
        same [] it answers a missing one with, and the caller could not
        recover a distinction its reader had already thrown away.

        Made unreadable by putting a FILE where the directory belongs
        (listdir -> NotADirectoryError), deterministic and uid-independent —
        chmod 000 does nothing under root, which is how a fixture like this
        passes while creating no condition at all."""
        row, _ = seats.dm("t1", "solo", who="senderS")
        self.join("t1")
        self.assertIsNotNone(row)
        # CONTROL: the obligation is real and visible BEFORE the namespace
        # breaks, so the total below is the reader and not an empty fixture.
        _i, pre_total, pre_faults = seats.pending(seat="senderS")
        self.assertEqual((pre_total, pre_faults), (1, []))

        dmdir = os.path.join(chat.chat_dir(), "dm")
        shutil.rmtree(dmdir)
        with open(dmdir, "w", encoding="utf-8") as f:
            f.write("not a directory")

        _items, total, faults = seats.pending(seat="senderS")
        self.assertEqual(total, 0)          # the DM is genuinely unreachable
        self.assertEqual([(f.kind, f.reason) for f in faults],
                         [("dm-namespace", "not-a-file")],
                         "an unreadable DM namespace read as no DMs")
        # AND THE SURFACE REFUSES THE ALL-CLEAR, which is the whole point: a
        # zero nobody could verify must never print as everything consumed.
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("consumed (acted) ✓", out,
                         "printed the green all-clear over an unread input")

    def test_a_lane_that_OPENS_but_holds_an_UNDECODABLE_row_is_incomplete(self):
        """Replacing the sole outbound row with a nonempty truncated
        JSON chunk made _row_offsets return a READABLE lane with rows=[], and
        pending answered total=0 over a corrupt file. Opening is not reading:
        an undecodable nonempty chunk was silently dropped, so the lane looked
        exactly like one that opened fine and held nothing.

        Completeness is WHOLE-LANE — one bad chunk means no NEGATIVE claim
        about this lane is safe — while the rows that DID parse stay as
        positive evidence, since discarding them would hide real obligations
        to punish the file for the row it lost."""
        chat.post("@t1 ping", room="main", who="senderS")
        self.join("t1")
        _i, pre_total, pre_faults = seats.pending(seat="senderS")
        self.assertEqual((pre_total, pre_faults), (1, []))   # control

        with open(chat.room_path("main"), "w", encoding="utf-8") as f:
            f.write('{"id": "abc", "from": "senderS"\n')     # truncated, real

        _items, total, faults = seats.pending(seat="senderS")
        self.assertEqual(total, 0)
        self.assertEqual([(f.kind, f.where, f.reason) for f in faults],
                         [("row", "main", "undecodable")],
                         "a corrupt lane read as a readable empty one")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("consumed (acted) ✓", out)

    def test_an_UNREADABLE_ROSTER_never_reports_SENT_off_a_STALE_cursor(self):
        """The final exact-tip consequence, and the one my read-count
        fix did NOT cure: reducing acquisitions is not the same as ORDERING
        the questions, and I would have shipped believing it was.

        consume_state asked for the CURSOR first and consulted the roster only
        when the cursor came back None. An unreadable roster fail-opens to {},
        losing the recipient's SESSION binding, so the lookup falls back to a
        stale SEAT-level cursor, returns non-None, and the failure branch never
        runs — the row reports `sent` while the session cursor proves it was
        consumed. The fallback is SELECTED BY the input that failed, so it can
        never be the evidence that the failure was harmless."""
        self.join("recipT")
        seats.deliver(seat="recipT", room="main")      # seat cursor: EOF is 0
        m = chat.post("@recipT ping", room="main", who="senderS")
        seats.deliver(session="sess-recipT", room="main", seat="recipT",
                      emit=lambda ln: None)            # session cursor past it

        # CONTROL, and it is what makes the verdict below a discrimination:
        # with a READABLE roster this exact fixture resolves the session and
        # reads SEEN. Any unknown below is the roster, never the fixture.
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "seen",
                         "control: the session cursor never passed the row, "
                         "so this arm could not tell the states apart")

        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json\n")
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "unknown",
                         "reported a measured state off a cursor it could "
                         "not have resolved")

    def test_THREE_KINDS_of_fault_in_ONE_render_all_survive(self):
        """The soft spot I named in review before it could be found: each
        fault kind was exercised alone, and the AGGREGATE is exactly where a
        collapse would hide. Their acceptance bar, verbatim: parsed
        obligations still render, every distinct fault provenance survives
        dedup/aggregation, and no all-clear appears anywhere.

        Dedup is the specific hazard. Faults are reported once per ROW they
        block, so the list needs collapsing — and a dedup written by kind
        rather than by identity would merge two different lanes into one
        "lane unreadable", losing exactly the provenance the structured fault
        exists to carry."""
        self.join("recipT")
        m = chat.post("@recipT ping", room="main", who="senderS")

        os.makedirs(chat.room_path("zzdeadlane"), exist_ok=True)   # LANE
        chat.post("seed", room="trouble", who="other")             # ROW
        with open(chat.room_path("trouble"), "w", encoding="utf-8") as f:
            f.write('{"id": "abc", "from": "x"\n')
        with open(seats.cursor_path("main", "recipT", "sess-recipT"),
                  "w", encoding="utf-8") as f:
            f.write("{ not json\n")                                # CURSOR

        items, total, faults = seats.pending(seat="senderS")
        kinds = {f.kind for f in faults}
        self.assertEqual(kinds, {"lane", "row", "cursor"},
                         "a fault kind was lost in aggregation: %r" % (faults,))
        # DISTINCT PROVENANCE, not just distinct kinds: each names its own
        # subject, so a reader knows WHICH lane and WHICH cursor to open.
        self.assertEqual(len({(f.kind, f.where) for f in faults}), 3)
        # POSITIVES PRESERVED: the real obligation still renders from a run
        # that could not read three of its inputs.
        self.assertEqual(total, 1)
        self.assertEqual([i["to"] for i in items], ["recipT"])

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("consumed (acted) ✓", out,
                         "an all-clear appeared in a render missing three "
                         "inputs")

    def test_a_MALFORMED_SESSION_CURSOR_never_falls_back_to_the_stale_one(self):
        """Requirement (1), and the generalisation of a rule I had
        already written for the roster and applied to only one of the two
        readers: THE FALLBACK IS SELECTED BY THE INPUT THAT FAILED, so it can
        never be the evidence that the failure was harmless.

        _cursor collapsed three states into None — MISSING (this seat never
        joined this lane, a proven absence), UNREADABLE, and parses-but-wrong-
        shape — so `session or seat` silently produced the STALE seat-level
        cursor whenever the session one could not be read, and the row was then
        judged against a cursor nobody had. A seat-level fallback is admissible
        ONLY after a PROVEN session absence."""
        self.join("recipT")
        seats.deliver(seat="recipT", room="main")      # seat cursor: EOF is 0
        m = chat.post("@recipT ping", room="main", who="senderS")
        seats.deliver(session="sess-recipT", room="main", seat="recipT",
                      emit=lambda ln: None)            # session cursor past it

        # CONTROL: with a readable session cursor this exact fixture reads
        # SEEN, so any unknown below is the corrupt file and not the fixture.
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "seen", "control: the session cursor never "
                                     "passed the row")

        with open(seats.cursor_path("main", "recipT", "sess-recipT"),
                  "w", encoding="utf-8") as f:
            f.write("{ not json at all\n")
        st, _a = seats.consume_state(m, "main", "recipT")
        self.assertEqual(st, "unknown",
                         "fell back to the STALE seat cursor after the "
                         "session cursor could not be read")

        # AND THE FAULT NAMES ITSELF. Without provenance the render would say
        # "roster unreadable" over a CURSOR failure — the same mislabel this
        # ladder shipped once, which sends the reader to a file that is fine.
        _items, _total, faults = seats.pending(seat="senderS")
        self.assertIn("cursor", [f.kind for f in faults],
                      "a cursor failure was not attributed to the cursor")

    def test_the_whole_command_acquires_the_roster_ONCE(self):
        """N pending rows used to cost N+1 fail-open reads plus N strict
        validations, so two rows on one screen could be judged against two
        different files — a torn answer, paid for in O(messages x roster).

        PINNED AT ONE, not `<=`: a bound that can only loosen is how a per-row
        re-read creeps back in, and an equality is what exposed the same defect
        on the claims render. Patches the CALLER's binding, because seats_ack
        binds `roster` at import — patching the defining module measures zero
        and reads as a pass."""
        from helm import seats_ack, seats_roster
        for i in range(10):
            chat.post("@zzr%d ping" % i, room="main", who="senderS")
        n = {"r": 0, "c": 0}
        real_r, real_c = seats_ack.roster, seats_roster.roster_checked

        def count_r(*a, **k):
            n["r"] += 1
            return real_r(*a, **k)

        def count_c(*a, **k):
            n["c"] += 1
            return real_c(*a, **k)

        seats_ack.roster = count_r
        seats_roster.roster_checked = count_c
        try:
            _items, total, _faults = seats_ack.pending(seat="senderS")
        finally:
            seats_ack.roster = real_r
            seats_roster.roster_checked = real_c

        self.assertEqual(total, 10, "control: the rows were not measured")
        self.assertEqual(n["c"], 1, "the roster was validated per ROW")
        self.assertEqual(n["r"], 0, "a fail-open re-read survived on the path")

    def test_prow_does_not_PREFIX_match_a_longer_recipient(self):  # noqa: VACUOUS_ASSERTION — unconditional controls run first: assertIn("ghostSeat", out) proves the row rendered, and prow("ghostSeat") returns it, so the raise for the PREFIX is a discrimination and not an empty screen
        """The second way a substring match lies, and it was introduced by the
        cure for the first: matching "-> ghost" also matches "-> ghostSeat",
        so a query for one seat returns another seat's row. The helper now
        compares the recipient FIELD, not a fragment of the line."""
        chat.post("@ghostSeat are you there?", room="main", who="senderS")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("ghostSeat", out)          # must-hit: the row IS there
        self.assertIn("ghostSeat", self.prow(out, "ghostSeat"))
        with self.assertRaises(AssertionError):
            self.prow(out, "ghost")              # a PREFIX is a different seat

    def test_a_name_nobody_could_ever_answer_still_reads_UNRES(self):
        """Deliberately NOT a regression: `@nosuchseat` is a genuine typo and
        it renders exactly like the unjoined seat above, because this surface
        cannot tell them apart and must not pretend otherwise. The old GONE
        wording read as certainty the roster never had. Distinguishing a typo
        from an unjoined seat needs evidence outside the roster — that is
        the roster's write path, not this render's job."""
        chat.post("@nosuchseat hello?", room="main", who="senderS")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("UNRES", self.prow(out, "nosuchseat"))


class AckTests(LadderBase):
    def test_seen_then_acted_via_ack(self):
        """An explicit ack moves SEEN -> ACTED and the row drops out of the
        sender's pending view (consumed)."""
        row, _ = seats.dm("recipT", "do the thing", who="senderS")
        self.join("recipT")
        seats.deliver(session="sess-recipT", room=seats.dm_lane("recipT"),
                      seat="recipT", emit=lambda ln: None)
        self.mask_old("recipT")
        self.assertEqual(self.states(), [("recipT", "seen")])

        res, err = seats.ack(row["id"], "done", who="recipT")
        self.assertIsNone(err)
        self.assertFalse(res["dup"])
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "done"))
        self.assertEqual(self.states(), [])          # hidden once acted

    def test_ack_done_vs_blocked_with_note(self):
        """done carries no reason; blocked carries its reason in the ack row's
        text, and the derived ackstate reflects each."""
        a, _ = seats.dm("recipT", "task A", who="senderS")
        b, _ = seats.dm("recipT", "task B", who="senderS")
        self.join("recipT")

        ra, _ = seats.ack(a["id"], "done", who="recipT")
        self.assertEqual(ra["row"].get("ackstate"), "done")
        self.assertEqual(ra["row"].get("text"), "")

        rb, _ = seats.ack(b["id"], "blocked", note="waiting on creds",
                          who="recipT")
        self.assertEqual(rb["row"].get("ackstate"), "blocked")
        self.assertEqual(rb["row"].get("text"), "waiting on creds")

        st_a, ack_a = seats.consume_state(a, seats.dm_lane("recipT"), "recipT")
        st_b, ack_b = seats.consume_state(b, seats.dm_lane("recipT"), "recipT")
        self.assertEqual((st_a, ack_a), ("acted", "done"))
        self.assertEqual((st_b, ack_b), ("acted", "blocked"))

    def test_ack_nonexistent_id_refused(self):
        res, err = seats.ack("deadbeefdead", "done", who="recipT")
        self.assertIsNone(res)
        self.assertIn("no message matches", err)

    def test_ack_foreign_row_refused(self):
        """Only a recipient can ack — a stranger acking a DM addressed to
        someone else is refused, and nothing is written."""
        row, _ = seats.dm("recipT", "yours only", who="senderS")
        before = len(chat.read(seats.dm_lane("recipT"))[0])
        res, err = seats.ack(row["id"], "done", who="stranger")
        self.assertIsNone(res)
        self.assertIn("not you", err)
        self.assertEqual(len(chat.read(seats.dm_lane("recipT"))[0]), before)

    def test_ack_non_addressed_row_refused(self):
        """A plain room post addresses nobody — there is nothing to ack."""
        row = chat.post("just chatter", room="main", who="senderS")
        res, err = seats.ack(row["id"], "done", who="recipT")
        self.assertIsNone(res)
        self.assertIn("not an addressed message", err)

    def test_double_ack_is_idempotent(self):
        """A repeat identical ack appends NO second row (append-only, but
        idempotent)."""
        row, _ = seats.dm("recipT", "once", who="senderS")
        self.join("recipT")
        r1, _ = seats.ack(row["id"], "done", who="recipT")
        self.assertFalse(r1["dup"])
        lane = seats.dm_lane("recipT")
        n1 = sum(1 for m in chat.read(lane)[0] if m.get("ack") == row["id"])
        r2, _ = seats.ack(row["id"], "done", who="recipT")
        self.assertTrue(r2["dup"])
        n2 = sum(1 for m in chat.read(lane)[0] if m.get("ack") == row["id"])
        self.assertEqual((n1, n2), (1, 1))

    def test_ack_state_change_appends_and_last_wins(self):
        """A DIFFERENT state (done -> blocked) is not a duplicate: it appends,
        and the derived state is the latest ack."""
        row, _ = seats.dm("recipT", "changes", who="senderS")
        self.join("recipT")
        seats.ack(row["id"], "done", who="recipT")
        r2, _ = seats.ack(row["id"], "blocked", note="regressed", who="recipT")
        self.assertFalse(r2["dup"])
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "blocked"))

    def test_ack_row_never_wakes(self):
        """An ack marker is state the sender PULLS, never a wake — deliverable
        drops it like a reaction, even with a blocked note as text."""
        row, _ = seats.dm("recipT", "x", who="senderS")
        self.join("recipT")
        res, _ = seats.ack(row["id"], "blocked", note="reason text here",
                           who="recipT")
        ackrow = res["row"]
        self.assertFalse(seats.deliverable(ackrow, "senderS",
                                           seats.dm_lane("recipT")))
        self.assertFalse(seats.deliverable(ackrow, "recipT",
                                           seats.dm_lane("recipT")))


class PendingViewTests(LadderBase):
    def test_pending_shows_unconsumed_hides_acted(self):
        """The sender's pending view shows EXACTLY the un-consumed/un-acted
        rows (SENT and SEEN) and hides the ones a recipient acted on."""
        for s in ("t1", "t2", "t3"):
            self.join(s)
        s1, _ = seats.dm("t1", "still open", who="senderS")           # sent
        s2, _ = seats.dm("t2", "will be acked", who="senderS")        # acted
        chat.post("@t3 look here", room="main", who="senderS")        # sent
        self.mask_old("t1", "t2", "t3")

        seats.ack(s2["id"], "done", who="t2")
        self.assertEqual(self.states(), [("t1", "sent"), ("t3", "sent")])

    def test_partial_ack_of_multi_recipient_keeps_the_rest(self):
        """A mention of two seats: one acks, the other still shows — the unit
        is per (recipient, row), not per row."""
        for s in ("t1", "t3"):
            self.join(s)
        m = chat.post("@t1 and @t3 please", room="main", who="senderS")
        self.mask_old("t1", "t3")
        seats.ack(m["id"], "done", who="t1")
        self.assertEqual(self.states(), [("t3", "sent")])

    def test_pending_ordered_oldest_first(self):
        """Longest-stranded on top. Both rows land in ONE lane (main) so file
        order is deterministic; the OLDER row is posted SECOND with a pinned
        older ts (pk.now_ts is only second-resolution) — so ONLY the ts sort,
        not append order, can float it to the top."""
        self.join("t1")
        self.join("t2")
        with mock.patch.object(pk, "now_ts", return_value="2026-01-01T00:00:09Z"):
            chat.post("@t1 newer", room="main", who="senderS")
        with mock.patch.object(pk, "now_ts", return_value="2026-01-01T00:00:01Z"):
            chat.post("@t2 older", room="main", who="senderS")
        items, total, _faults = seats.pending(seat="senderS")
        self.assertEqual(total, 2)
        self.assertEqual([i["ts"] for i in items],
                         ["2026-01-01T00:00:01Z", "2026-01-01T00:00:09Z"])
        self.assertEqual([i["to"] for i in items], ["t2", "t1"])

    def test_pending_empty_when_all_consumed(self):
        row, _ = seats.dm("t1", "solo", who="senderS")
        self.join("t1")
        # CONTROL, unconditional: the obligation EXISTED before the ack. An
        # emptiness assertion with no positive pole passes just as happily on
        # a fixture that never created one — which is the whole failure mode
        # of asserting an absence.
        _pre, pre_total, _pl = seats.pending(seat="senderS")
        self.assertEqual(pre_total, 1, "control: nothing was pending to ack")
        seats.ack(row["id"], "done", who="t1")
        items, total, _faults = seats.pending(seat="senderS")
        self.assertEqual((items, total), ([], 0))

    def test_unreadable_lane_surfaces_unknown_not_clear(self):
        """A lane that cannot be read is UNKNOWN, never a silent empty: pending
        must fail CLOSED — never the green all-clear over a lane whose consume
        state it could not determine. Made unreadable by swapping the room's
        backing file for a DIRECTORY (open() -> IsADirectoryError, an OSError)
        — deterministic and uid-independent, unlike chmod 000 under root.

        THE CHANNEL MOVED AND THE LAW DID NOT. This used to assert a synthetic
        `unknown` ITEM, which is exactly what let unreadable lanes sort ahead
        of real rows and eat the message cap; the lane is now a FAULT beside
        the answer instead of a fake row inside it. What must never change is
        the property this arm was written for: no green all-clear over a lane
        nobody could read."""
        chat.post("@t1 seed so the room file exists", room="trouble",
                  who="other")
        p = chat.room_path("trouble")
        os.remove(p)
        os.mkdir(p)                    # now unreadable: open() raises OSError
        # senderS has NO readable outbound anywhere: WITHOUT the fix pending is
        # empty -> a FALSE green clear; WITH it the lane surfaces as a fault.
        items, total, faults = seats.pending(seat="senderS")
        self.assertEqual(items, [])            # nothing was addressed BY it
        self.assertIn(("lane", "trouble"), [(f.kind, f.where) for f in faults],
                      "an unreadable lane vanished from the verdict")
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertNotIn("nothing outbound is waiting", out)
        self.assertIn("INCOMPLETE", out)

    def test_pending_only_my_outbound(self):
        """pending is scoped to the querying sender — another seat's outbound
        rows never appear."""
        self.join("t1")
        seats.dm("t1", "from S", who="senderS")
        seats.dm("t1", "from other", who="otherSender")
        items, _, _faults = seats.pending(seat="senderS")
        self.assertEqual([i["text"] for i in items], ["from S"])


class CliTests(LadderBase):
    def setUp(self):
        super().setUp()
        # The acking session IS recipT (the DM recipient): `--seat recipT` on
        # the ack SIGNING verb ASSERTS this ambient identity (post-actor-
        # binding). `pending --seat senderS` below is a READ — its --seat stays
        # a free lane selector, independent of the ambient acker.
        # DECLARE *AND* JOIN. A bare export models the name and not the
        # identity: an ack is an ACT, and an act door now requires a session
        # the roster resolves back to this seat.
        _tmp_declare(self, "recipT")

    def test_cli_ack_and_pending_roundtrip(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "cli path", who="senderS")
        self.mask_old("recipT")

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("SENT", out)
        self.assertIn(row["id"][:8], out)
        self.assertIn("recipT", out)

        rc, out, err = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "recipT"])
        self.assertEqual(rc, 0, err)
        self.assertIn("acked", out)
        self.assertIn("DONE", out)

        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing outbound is waiting", out)

    def test_cli_ack_blocked_with_multiword_note(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "blocked path", who="senderS")
        rc, out, err = self.run_cmd(
            "ack", [row["id"], "blocked", "--seat", "recipT",
                    "--note", "waiting on the creds handoff"])
        self.assertEqual(rc, 0, err)
        self.assertIn("BLOCKED", out)
        self.assertIn("waiting on the creds handoff", out)
        st, ackstate = seats.consume_state(row, seats.dm_lane("recipT"),
                                           "recipT")
        self.assertEqual((st, ackstate), ("acted", "blocked"))

    def test_cli_ack_foreign_refused_nonzero(self):
        # A non-recipient cannot ack another seat's obligation. Under post-
        # actor-binding the acker is the AMBIENT identity, so we BECOME stranger
        # (not `--seat stranger`, which is refused earlier as a cross-seat claim
        # — that layer is covered by test_chat.py::SeatActorBindingTest). The
        # recipient check then refuses: the row is addressed to recipT.
        _tmp_declare(self, "stranger")
        row, _ = seats.dm("recipT", "not yours", who="senderS")
        rc, out, err = self.run_cmd("ack", [row["id"], "done"])
        self.assertEqual(rc, 1)
        self.assertIn("not you", err)

    def test_cli_ack_missing_id_usage(self):
        rc, out, err = self.run_cmd("ack", ["--seat", "recipT"])
        self.assertEqual(rc, 2)
        self.assertIn("usage: helm chat ack", err)

    def test_cli_double_ack_idempotent_message(self):
        self.join("recipT")
        row, _ = seats.dm("recipT", "twice", who="senderS")
        self.run_cmd("ack", [row["id"], "done", "--seat", "recipT"])
        rc, out, _ = self.run_cmd(
            "ack", [row["id"], "done", "--seat", "recipT"])
        self.assertEqual(rc, 0)
        self.assertIn("already acked", out)


class HostileNameSinkTests(LadderBase):
    """The display-launder tripwire (test_display_launder_tripwire) names this
    class as the runtime proof that the consume-ladder sinks strip a planted
    identity. ESC (screen-clear) + BIDI (line reorder) are the two markers."""
    ESC, BIDI = "\x1b[2J", "‮"

    def _hostile_dm(self, sender="senderS"):
        # Historical/corrupt row planted below chat.post's final-write validator:
        # the current writer rejects this token, but readers still launder bytes
        # that an older writer or hand-edited ledger already persisted.
        name = "evil%s%s" % (self.ESC, self.BIDI)
        row = {"ts": pk.now_ts(), "from": sender, "text": "payload", "dm": name}
        return chat._append(row, chat.dm_room(name)), name

    def test_pending_launders_planted_recipient(self):
        row, _name = self._hostile_dm()
        rc, out, _ = self.run_cmd("pending", ["--seat", "senderS"])
        self.assertEqual(rc, 0)
        self.assertNotIn(self.ESC, out)
        self.assertNotIn(self.BIDI, out)
        self.assertIn(row["id"][:8], out)

    def test_ack_refusal_launders_planted_recipient(self):
        # The refusal message scrubs a planted control/bidi recipient name.
        # Under post-actor-binding the acker is the AMBIENT identity, so we
        # BECOME a non-recipient ("stranger") and ack the hostile row without
        # --seat — the recipient-check refusal (rc 1) fires and its error stays
        # scrubbed (a cross-seat `--seat` claim is refused earlier; see
        # test_chat.py::SeatActorBindingTest).
        _tmp_declare(self, "stranger")
        row, _name = self._hostile_dm()
        rc, out, err = self.run_cmd("ack", [row["id"], "done"])
        self.assertEqual(rc, 1)
        self.assertNotIn(self.ESC, err)
        self.assertNotIn(self.BIDI, err)


if __name__ == "__main__":
    unittest.main()


class UndatedRowIsNotCalledFreshTest(unittest.TestCase):
    """A FIX on the extraction: the renderer invented an age its own
    reader refuses to claim.

    `pending()` is explicit that a message MAY legitimately lack a timestamp,
    sorts those LAST, and "never describes them as oldest" (seats_ack.py
    :527-533). The moved `render_pending` then computed
    `_fmt_age(now - (_ts_epoch(it["ts"]) or now))` — `or now` makes the delta
    ZERO, so an undated obligation printed "<1m" and read as the FRESHEST thing
    on the operator's screen. A surface asserting a fact its payload disowns.

    THE FIXTURE CARRIES PRODUCTION'S OWN KEY SET, not an invented one — dm, id,
    kind, room, state, text, to, ts, why, taken from a live `pending()` row and
    with exactly ONE field changed. My first attempt invented the shape and died
    on KeyError('dm'), which is its own small lesson about fixtures.
    """

    ROW = {"id": "abc12345", "to": "someone", "room": "main", "dm": False,
           "kind": "message", "state": "sent", "text": "an obligation",
           "why": None, "ts": None}

    def _render(self, row):
        from helm import seats_ack
        original = seats_ack.pending
        seats_ack.pending = lambda seat=None, session=None: ([row], 1, [])
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                seats_ack.render_pending("me", "sess")
        finally:
            seats_ack.pending = original
        lines = [l for l in buf.getvalue().splitlines() if row["id"][:8] in l]
        self.assertTrue(lines, "the renderer emitted no row for the fixture")
        return lines[0]

    def test_an_undated_row_renders_an_honest_unknown_not_an_age(self):
        line = self._render(dict(self.ROW))
        self.assertIn("?", line, "no unknown-age token: %r" % line)
        self.assertNotIn("<1m", line,
                         "an undated obligation was called the freshest thing "
                         "on the screen — the exact claim pending() refuses to "
                         "make: %r" % line)

    def test_epoch_zero_is_a_VALID_stamp_not_a_missing_one(self):
        """A second finding, and it is the premise I had just filed
        wearing a different mask.

        `_ts_epoch` reserves None for garbage and returns 0 for
        1970-01-01T00:00:00Z — a VALID timestamp. My cure tested `if stamp`, so
        zero was falsy and the row rendered "?" — the MEASURED case paying for
        the UNKNOWN one. My pole existed and used a 3-hours-ago stamp, which is
        nowhere near the boundary where truthiness and None-ness diverge.

        A pole for a three-state surface has to sit ON the boundary, not in the
        comfortable middle of the range.
        """
        line = self._render(dict(self.ROW, ts="1970-01-01T00:00:00Z"))
        self.assertNotIn("?", line,
                         "epoch 0 is a real timestamp and was called undated: "
                         "%r" % line)

    def test_a_DATED_row_still_shows_its_age(self):
        """THE POLE. Without it, a renderer that printed "?" for every row —
        losing the age column entirely — satisfies the arm above."""
        # _ts_epoch parses exactly "%Y-%m-%dT%H:%M:%SZ" (seats_ack), so the
        # fixture is built with that format rather than an invented helper.
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 3 * 3600))
        dated = dict(self.ROW, ts=stamp)
        line = self._render(dated)
        self.assertNotIn("  ?", line,
                         "a row WITH a timestamp lost its age: %r" % line)
        self.assertRegex(line, r"\d+[mhd]",
                         "no age rendered for a dated row: %r" % line)


class PendingHelpMirrorsTheLegend(unittest.TestCase):
    """The help and the renderer are two surfaces describing ONE state set, and
    nothing connected them until now.

    THE DEFECT (task/1046): `helm chat pending --help` listed SENT, SEEN, UNRES
    and UNKN while the renderer emitted a fifth, BASED, with its own legend
    entry. The help cure landed BEFORE the BASELINED state existed, in the same
    lane, and the registry was never revisited. Every arm in this file asserted
    that BASED RENDERS; not one asserted that the help ADMITS it, so the gap was
    invisible to a green suite.

    DERIVED, NEVER ENUMERATED. The state list comes from the renderer's own
    legend glyphs, so a state added tomorrow is covered by this arm the moment
    it gets a legend line — which is exactly the event that produced the defect.
    A hardcoded list here would reproduce the original failure one layer up: it
    would pass until someone added a state and forgot two places instead of one.

    SCOPE, corrected after MEASURING the derivation rather than predicting it:
    I wrote this paragraph claiming the walk pinned FOUR glyph states and that
    SEEN sat outside it. Run, the walk returns FIVE — BASED, SEEN, SENT, UNKN,
    UNRES — because SEEN carries a legend line the glyph survey I eyeballed had
    missed. The arm is WIDER than I described it, which is the safe direction,
    but a docstring that describes an arm it never ran is the same defect one
    layer up from the one this row cures.
    """

    def _legend_states(self):
        import inspect
        import re
        from helm import seats_ack
        src = inspect.getsource(seats_ack)
        # [ \t] AND NOT \s, BECAUSE \s MATCHES A NEWLINE. With \s this
        # pattern pairs punctuation at the end of one line with an
        # ordinary CONSTANT at the start of the next, and it does: run
        # against helm/todos.py the loose form invents ACTIVE, GONE,
        # STAMP and TOOLS, none of which is a state. On seats_ack.py the
        # two forms happen to agree today, so this arm was CORRECT and
        # resting on luck — one punctuation-ending line above a constant
        # would have made it derive a phantom state and redden for a
        # reason having nothing to do with the help.
        return sorted(set(re.findall(r"[^\w\s][ \t]([A-Z]{4,6}) = ", src)))

    def test_every_legend_state_appears_in_the_pending_help(self):
        from helm import chat
        states = self._legend_states()
        # POSITIVE CONTROL FIRST, on the walk every claim below reads from: a
        # derivation that found nothing would make the emptiness assertion
        # vacuously true, which is the shape this arm exists to refuse.
        self.assertGreaterEqual(
            len(states), 4,
            "the legend walk found %d states — the renderer's legend format "
            "changed and this arm is measuring nothing" % len(states))
        help_text = chat.HELP["pending"]
        missing = [s for s in states if s not in help_text]
        self.assertEqual(
            missing, [],
            "the renderer names %s in its legend and `helm chat pending "
            "--help` never mentions it — a reader is told a state exists by "
            "one surface and that it does not by the other" % (missing,))

    def test_the_arm_would_REDDEN_if_a_state_were_dropped_from_the_help(self):
        """The must-hit: without this, the arm above could pass because the
        help happens to contain every short uppercase word, rather than because
        anyone kept the two surfaces in step."""
        from helm import chat
        states = self._legend_states()
        self.assertIn("BASED", states)          # the state that was missing
        gutted = chat.HELP["pending"].replace("BASED", "")
        missing = [s for s in states if s not in gutted]
        self.assertEqual(missing, ["BASED"],
                         "removing BASED from the help must make this arm "
                         "name exactly BASED and nothing else")
