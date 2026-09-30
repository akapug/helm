#!/usr/bin/env python3
"""The short read: one line per row for a reader whose context is scarce
(task/3382).

THE SYMPTOM. Measured over 24 h of a local seat's transcript: it ran
`helm chat read` 558 times for 1.46M characters, 17 % of its context-window
growth. Most of those characters are row BODIES: a train's merge line, a
review verdict, a brief of twenty lines.

THE RULING these arms pin: THE SHORT READ CUTS ONLY ROWS NOBODY OWES THE
READER. A row owed to the reader (an @mention, a reply, a DM, a row of its
home room, a row the delivery layer holds for it: whatever its tool-boundary
hook would show it) and any row an owner door stamped print WHOLE, exactly as
a full read prints them. Only a row the reader is not owed may be cut, so a
cut row is never a row the hook later shows clipped to 200 bytes.

THE MATRIX, every read through `chat.cmd_chat` as a declared seat on the
doorbell fixture (PullBase): {an owed @mention, a DM, an owner-door row under
a name that is not his, an ambient long row, an ambient short row} x {the
short read, `--full`, a native reader}, plus the whole-row reader `--id`, an
empty window, the `[n]` a cut row printed, and the owner-unread marker. A
native reader's output is byte for byte the full renderer's.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, chatshort, seat, seat_catalog, seats  # noqa: E402,F401 — the facade first (seat_compat)
from helm import cli, hooks, seats_delivery, seats_identity  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests.test_pull_delivery import PullBase  # noqa: E402
from tests.test_beacon_doorbell import SEAT, SID  # noqa: E402

#: A family the catalog saves context for, and one it does not: read from the
#: catalog, never named here.
LEAN = next(f for f in sorted(seat_catalog.FAMILIES)
            if seat_catalog.context_lean(f))
NOT_LEAN = next(f for f in sorted(seat_catalog.FAMILIES)
                if not seat_catalog.context_lean(f))

_TOPICS = ("the doorbell rings once per burst and the hook still owes each "
           "row it announced, so a seat that resumes pulls its backlog at "
           "its own pace instead of paying a turn per row",
           "coordination verbs run the trunk binary from any lane tree, "
           "with argv and stdin byte for byte, so a stale lane binary can no "
           "longer write ledger rows that current readers drop",
           "the five slowest test modules stop paying for waits and censuses "
           "they never assert on, and the same test ids load before and "
           "after the change")

#: A brief of the shape a seat acts on: longer than SHORT_CHARS, its lines
#: and their indentation part of what it says.
BRIEF = ("review lane/feature-9 at 0123456789ab\n\nWHAT: %s.\n" % _TOPICS[0]
         + "".join("    - check %d: the arm for cell %d goes red on main\n"
                   % (k, k) for k in range(12))
         + "Reply with helm dispatch verdict on this row.")


def fixture_rows():
    """Fifty rows in the shapes the fleet's rooms carry, as (who, text) or
    ("react", who): merge lines, verdicts, multi-line briefs, chatter and
    reactions, in the proportions a busy room holds them."""
    rows = []
    for i in range(10):
        rows.append(("integrator", (
            "train%d: merge lane feature-%d (task/%d, P1, not a door; built "
            "by a subagent of the integrator): %s; %s (a non-author read "
            "SOURCE-CLEAN %012x)" % (300 + i, i, 3000 + i, _TOPICS[i % 3],
                                     "; ".join(_TOPICS), 0xabc000 + i))))
        rows.append(("kimi", "verdict on dispatch %08x: CONCUR at %012x — "
                     "every arm of the matrix ran red first and green after; "
                     "the one risk named in the brief is covered by the "
                     "restart arm, and the docs match the code" % (i, i)))
        rows.append(("bob", "on it — rebasing lane feature-%d now" % i))
    for i in range(6):
        rows.append(("integrator", "@kimi review lane/feature-%d at %012x\n\n"
                     "WHAT: %s.\nWHY: %s.\n\n" % (i, i, _TOPICS[i % 3],
                                                    _TOPICS[(i + 2) % 3])
                     + "".join("- check %d: the arm for cell %d holds a "
                               "control that goes red on main\n" % (k, k)
                               for k in range(12))
                     + "Reply with helm dispatch verdict on this row."))
        rows.append(("carol", "@gemini can you take the doc pass on %d?"
                     % i))
        rows.append(("react", "carol"))
    rows.append(("bob", "landed, thanks"))
    rows.append(("carol", "@all standup in ten minutes"))
    return rows


def post_fixture(room="main"):
    for who, text in fixture_rows():
        if who == "react":
            chat.react(-1, "+1", room=room, who=text)
        else:
            chat.post(text, who=who, room=room)


#: The launch stamp's four spellings: HELM_ preferred, legacy MELD_.
STAMP = ("HELM_MODEL_FAMILY", "MELD_MODEL_FAMILY", "HELM_MODEL_BACKEND",
         "MELD_MODEL_BACKEND")


class _ShortBase(PullBase):
    """The reader joins un-homed (DoorbellBase joins with no explicit room,
    from a cwd in no project), so a row addressed to it in main is owed to
    it and a plain row there is not."""

    def read(self, *args, family=None, backend=None, rc=0, stamp=None):
        """`helm chat read ARGS` as the seat, its launch stamp the model
        `family` and `backend` (None: absent from the environment), or the
        `stamp` {name: value} over the four spellings (STAMP) ->
        (stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        stamp = stamp if stamp is not None else {
            "HELM_MODEL_FAMILY": family, "HELM_MODEL_BACKEND": backend}
        env = {k: stamp.get(k) or "" for k in STAMP}
        with mock.patch.dict(os.environ, env), \
                _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            for key in [k for k, v in env.items() if not v]:
                os.environ.pop(key)
            self.assertEqual(chat.cmd_chat(["read"] + list(args)), rc,
                             err.getvalue())
        return out.getvalue(), err.getvalue()

    def full_render(self, room="main", since=0):
        """What the full renderer prints for `room` from `since`: the read's
        loop, composed from `chat.run_line` over `chat.ack_runs`."""
        rows, _total = chat.read(room)
        tag, idx = chat.read_prefix(rows), chat.index_rows(rows)
        return "".join(chat.run_line(run, tag, idx) + "\n" for run in
                       chat.ack_runs(enumerate(rows[since:], since)))

    def row(self, needle, room="main"):
        return next(m for m in chat.read(room)[0]
                    if needle in (m.get("text") or ""))

    def native_line(self, row, room="main"):
        """The line a full read prints for `row`, newlines and all."""
        rows = chat.read(room)[0]
        i = next(k for k, m in enumerate(rows) if m.get("id") == row["id"])
        return chat.run_line([(i, rows[i])], chat.read_prefix(rows),
                             chat.index_rows(rows))

    def assertWhole(self, out, row, room="main"):
        """`row` printed in `out` exactly as a full read prints it."""
        self.assertIn(self.native_line(row, room) + "\n", out)
        self.assertNotIn("… [+", "".join(
            x for x in out.splitlines() if row["id"][:chat.ID_SHOWN] in x))

    def cut_line(self, out, row, room="main"):
        """The ONE line of `out` that printed `row` cut: its folded body to
        SHORT_CHARS and the count of what was cut -> that line."""
        lines = [x for x in out.splitlines()
                 if row["id"][:chat.ID_SHOWN] in x]
        self.assertEqual(len(lines), 1, out)
        folded = " ".join(row["text"].split())
        self.assertIn("%s… [+%d chars]" % (
            folded[:chatshort.SHORT_CHARS],
            len(folded) - chatshort.SHORT_CHARS), lines[0])
        self.assertNotIn(self.native_line(row, room), out)
        return lines[0]

    @contextlib.contextmanager
    def owes_nothing(self):
        """A double of the ruling's predicate that owes the reader no row,
        so the short read cuts an owed row: the defence arms read what the
        machinery after the cut does with one."""
        with mock.patch.object(chatshort, "owed_to",
                               lambda *_a, **_k: (lambda _i, _m: False)):
            yield


class _HomedBase(_ShortBase):
    """The reader is homed in helm (an explicit join room): a row a person
    posts there reaches it at every tool boundary, so it is owed; a plain
    row in main is not, and the short read may cut it."""
    HOME = "helm"


class NativeReaderTest(_ShortBase):

    def test_a_native_reader_prints_every_row_whole_and_unchanged(self):
        """The control for every short arm: no family, the native family and
        a family the catalog does not save context for print the same bytes,
        and those bytes are the full renderer's, long rows whole."""
        post_fixture()
        want = self.full_render()
        for family, backend in ((None, None), ("claude", "native"),
                                ("claude", None), (NOT_LEAN, "proxy"),
                                (LEAN, "native")):
            with self.subTest(family=family, backend=backend):
                out, _err = self.read("--room", "main", family=family,
                                      backend=backend)
                self.assertEqual(out, want)
        self.assertIn("- check 11: the arm for cell 11", want)
        self.assertIn("Reply with helm dispatch verdict on this row.", want)

    def test_a_native_reader_of_its_dm_lane_and_of_an_empty_window(self):  # noqa: VACUOUS_ASSERTION — the DM text is asserted present and the empty window equals the non-empty empty_room_line, both on the read's own stdout
        seats.dm(SEAT, "first line\nsecond line of the direct ask", who="bob")
        out, _err = self.read("--dm", family="claude")
        self.assertIn("first line\nsecond line of the direct ask", out)
        chat.post("one row", who="bob")
        total = chat.read("main")[1]
        out, _err = self.read("--room", "main", "--since", str(total),
                              family="claude")
        self.assertEqual(out, chat.empty_room_line("main", "main") + "\n")

    def test_full_turns_a_local_reader_back_to_the_native_bytes(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes over the fifty-row fixture is the positive control
        post_fixture()
        out, _err = self.read("--room", "main", "--full", family=LEAN)
        self.assertEqual(out, self.full_render())


class TheRulingMatrixTest(_HomedBase):
    """{an owed @mention, a DM, an owner-door row under another name, an
    ambient long row, an ambient short row} x {short, --full, native}."""

    def kinds(self):
        """One row of each kind -> {kind: (room, row)}. The reader is homed
        in helm, so each row but the ambient ones is owed for its own
        reason, and none for the home room's."""
        lane = chat.dm_room(SEAT)
        rows = {
            "owed @mention": chat.post("@gemini " + BRIEF, who="bob"),
            "owner door, another name": chat.post(
                "hold every land until I say\n" + BRIEF, who="davidweb",
                origin="web"),
            "ambient long": chat.post("@kimi " + BRIEF, who="bob"),
            "ambient short": chat.post("landed,\n    thanks", who="bob"),
        }
        seats.dm(SEAT, "direct ask\n" + BRIEF, who="bob")
        out = {k: ("main", m) for k, m in rows.items()}
        out["DM"] = (lane, self.row("direct ask", room=lane))
        return out

    def test_the_short_read_cuts_only_the_ambient_long_row(self):  # noqa: VACUOUS_ASSERTION — the exact equality of the whole read to the full renderer's bytes with one line replaced, and the cut line's own text, are the positive controls
        """RED at 3b9a8f48cfd: the owed @mention and the owner's web post
        under his name box's other name were cut, and every uncut row, the
        DM and the ambient short row among them, printed on one line with
        its newlines and indentation gone."""
        kinds = self.kinds()
        short, _err = self.read("--room", "main", family=LEAN)
        dm, _err = self.read("--dm", family=LEAN)
        outs = {"main": short, chat.dm_room(SEAT): dm}
        for kind, (room, row) in kinds.items():
            with self.subTest(kind=kind):
                if kind == "ambient long":
                    self.cut_line(outs[room], row, room)
                else:
                    self.assertWhole(outs[room], row, room)
        # everything but the one cut row is the full read's bytes, and the
        # read ends with the legend for the one row it cut
        cut = self.cut_line(short, kinds["ambient long"][1])
        native = self.full_render()
        self.assertEqual(short, native.replace(
            self.native_line(kinds["ambient long"][1]) + "\n", cut + "\n")
            + chatshort.legend_line(1) + "\n")
        self.assertEqual(dm, self.full_render(chat.dm_room(SEAT)))

    def test_full_and_a_native_reader_print_every_kind_whole(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's bytes over rows asserted to hold every kind's text
        kinds = self.kinds()
        for mode, args, family in (("--full", ("--full",), LEAN),
                                   ("native", (), "claude")):
            with self.subTest(mode=mode):
                out, _err = self.read("--room", "main", *args, family=family)
                dm, _err = self.read("--dm", *args, family=family)
                self.assertEqual(out, self.full_render())
                self.assertEqual(dm, self.full_render(chat.dm_room(SEAT)))
                for kind, (room, row) in kinds.items():
                    self.assertWhole(out if room == "main" else dm, row, room)


class AnOwedBriefIsNeverCutTest(_ShortBase):
    """F3: a cut row stayed owed only until the next tool boundary, where
    the hook showed it clipped to 200 bytes and moved the delivery cursor
    past it, so a lean seat's brief reached it as 160 characters plus 200
    bytes. An owed row is never cut, so the read delivers it whole."""

    def test_an_owed_long_brief_prints_whole_with_its_newlines(self):  # noqa: VACUOUS_ASSERTION — exact equality of the short read to the full renderer's non-empty bytes, the brief's twelfth indented line asserted in it, and the counts (1, 1) before the read on the same rows are the positive controls
        """RED at 3b9a8f48cfd: the brief printed as 160 folded characters
        and stayed owed to the hook, which shows it clipped."""
        self.rung(["@gemini " + BRIEF])
        self.assertEqual(self.counts(), (1, 1))
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertEqual(short, self.full_render())
        self.assertIn("\n    - check 11: the arm for cell 11 goes red on "
                      "main\n", short)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.hook_texts(pattern=r"@gemini \w+"), [])


class AHomeRoomRowIsOwedTest(_HomedBase):
    """A plain row a person posted in the reader's home room reaches it at
    every tool boundary (seats_identity.deliverable's home-room tier), so
    it is owed and prints whole; the same row in main, a room the reader is
    not homed in, is cut (TheRulingMatrixTest). The stop guard counts what
    the hook owes, so each arm reads it before and after."""

    def test_a_home_room_row_prints_whole_and_is_delivered(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes, and the guard's count of 1 before the read on the same row
        """RED at 3b9a8f48cfd: the home-room row was cut and stayed owed."""
        row = chat.post("plain chatter, " + BRIEF, who="bob", room="helm")
        self.assertEqual(self.guard_count(), 1)
        short, _err = self.read("--room", "helm", family=LEAN)
        self.assertEqual(short, self.full_render("helm"))
        self.assertWhole(short, row, "helm")
        self.assertEqual(self.guard_count(), 0)

    def test_a_cut_row_is_never_a_row_the_hook_owes(self):  # noqa: VACUOUS_ASSERTION — the guard's count of 2 before the read and the two cut lines and two whole rows asserted in the read's output are the positive controls for the empty hook pass
        """The rows a short read cuts are exactly rows no tool boundary
        shows: a subsystem's row and the reader's own post. The rows it
        prints whole are delivered, so after the read the stop guard counts
        nothing and the hook shows nothing. RED at 3b9a8f48cfd: the plain
        row and the @mention were cut too, and stayed owed."""
        machine = chat.post("proxy health: " + BRIEF, who="proxywatch",
                            room="helm")
        own = chat.post("my own notes: " + BRIEF, who=SEAT, room="helm")
        plain = chat.post("plain chatter, " + BRIEF, who="bob", room="helm")
        owed = chat.post("@gemini " + BRIEF, who="carol", room="helm")
        self.assertEqual(self.guard_count(), 2)
        short, _err = self.read("--room", "helm", family=LEAN)
        self.cut_line(short, machine, "helm")
        self.cut_line(short, own, "helm")
        self.assertWhole(short, plain, "helm")
        self.assertWhole(short, owed, "helm")
        self.assertTrue(short.endswith(chatshort.legend_line(2) + "\n"),
                        short)
        self.assertEqual(self.guard_count(), 0)
        got = []
        for _ in range(4):
            seats_delivery.deliver_any(session=SID, seat=SEAT,
                                       emit=got.append)
        self.assertEqual(got, [])

    def test_a_row_the_hook_holds_prints_whole(self):
        """A row the delivery layer holds for the seat (the wake cursor's
        `held`: the doorbell rang it and the hook still owes it) prints
        whole even when the seat's scope no longer reaches it: here an
        --ambient ring held a plain home-room row, and the seat was then
        re-homed. A plain row posted there after the re-home is held by
        nothing and owed to nobody, so it is cut, the control. RED at
        3b9a8f48cfd: the held row was cut."""
        chat.post("plain rung row, " + BRIEF, who="bob", room="helm")
        lines, _seen = self.follow(passes=2, flags=("--ambient",),
                                   clock=True)
        self.assertEqual(len(lines), 1, (lines, self.stderr))
        wake = seats_delivery._cursor("helm", SEAT, SID, beacon=True)
        self.assertEqual(len(wake.get("held") or ()), 1, wake)
        with _tmp_declaring(["--seat", SEAT]):
            seats.join(session=SID, seat=SEAT, cwd="/tmp/p", room="team-x")
        self.assertEqual(seats.seat_scope(SEAT)["home"], "team-x")
        held = self.row("plain rung row", "helm")
        after = chat.post("plain row after the re-home, " + BRIEF, who="bob",
                          room="helm")
        short, _err = self.read("--room", "helm", family=LEAN)
        self.assertWhole(short, held, "helm")
        self.cut_line(short, after, "helm")


class LocalReaderTest(_HomedBase):

    def test_a_local_reader_gets_one_line_per_row_and_the_numbers(self):  # noqa: VACUOUS_ASSERTION — the line count, the legend, the size bound and the home-room equality are all on the reads' non-empty stdout
        """The fifty-row fixture in a room the reader is not homed in: every
        row it is not owed prints on one line, and the read costs less than
        half the native one. MEASURED on stderr, beside the same fixture in
        the reader's home room, where every person's row is owed."""
        post_fixture()
        native, _err = self.read("--room", "main", family="claude")
        short, _err = self.read("--room", "main", family=LEAN)
        rows = chat.read("main")[0]
        lines = short.splitlines()
        self.assertEqual(len(lines), len(chat.ack_runs(enumerate(rows))) + 1,
                         short)
        self.assertIn("helm chat read --id", lines[-1])
        self.assertEqual(len(fixture_rows()), 50)
        self.assertLess(len(short), len(native) / 2)
        post_fixture(room="helm")
        home_native, _err = self.read("--room", "helm", family="claude")
        home_short, _err = self.read("--room", "helm", family=LEAN)
        self.assertEqual(home_short, home_native)
        sys.stderr.write(
            "\nMEASURED 50-row read chars: not homed there: native %d, short "
            "%d (%.0f%%); its home room: native %d, short %d (%.0f%%)\n"
            % (len(native), len(short), 100.0 * len(short) / len(native),
               len(home_native), len(home_short),
               100.0 * len(home_short) / len(home_native)))

    def test_a_short_row_prints_as_the_native_line(self):  # noqa: VACUOUS_ASSERTION — equality of the two non-empty reads is the contract; the native read prints the posted row
        chat.post("landed, thanks", who="bob")
        native, _err = self.read("--room", "main", family="claude")
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertEqual(short, native, "nothing was cut, so no legend")

    def test_a_long_row_is_cut_on_one_line_and_names_the_whole_row_reader(self):
        body = "@kimi review this\n\n" + "word " * 200
        chat.post(body, who="bob")
        m = self.row("@kimi review this")
        short, _err = self.read("--room", "main", family=LEAN)
        line, legend = short.splitlines()
        folded = " ".join(body.split())
        cut = len(folded) - chatshort.SHORT_CHARS
        self.assertIn(m["id"][:chat.ID_SHOWN], line)
        self.assertTrue(line.endswith("bob: %s… [+%d chars]%s" % (
            folded[:chatshort.SHORT_CHARS], cut, chat._transport_tag(m))),
            line)
        self.assertEqual(legend, chatshort.legend_line(1))
        self.assertIn("helm chat read --id", legend)
        self.assertIn("cut at %d characters" % chatshort.SHORT_CHARS,
                      chat.HELP["read"], "the usage states another length")

    def test_a_dm_prints_as_the_full_read_prints_it(self):
        """RED at 3b9a8f48cfd: the DM printed whole but on one line, its
        newlines and indentation folded away."""
        text = "direct ask\n    indented detail\n\tand a tab\n" + "detail " * 60
        seats.dm(SEAT, text, who="bob")
        short, _err = self.read("--dm", family=LEAN)
        self.assertEqual(short, self.full_render(chat.dm_room(SEAT)))
        self.assertIn(text, short)

    def test_an_empty_window_says_so_in_one_line(self):  # noqa: VACUOUS_ASSERTION — exact equality to a non-empty line naming the since and the row count
        chat.post("one row", who="bob")
        chat.post("two rows", who="bob")
        total = chat.read("main")[1]
        short, _err = self.read("--room", "main", "--since", str(total),
                                family=LEAN)
        self.assertEqual(short, "helm chat [main]: no rows past --since %d "
                         "(the room holds %d)\n" % (total, total))

    def test_an_empty_room_keeps_the_full_answer(self):  # noqa: VACUOUS_ASSERTION — exact equality to the non-empty empty_room_line
        """No row at all is one of three facts (empty, absent, unreadable),
        so the short read keeps the full read's line for it."""
        short, _err = self.read("--room", "never-posted", family=LEAN)
        self.assertEqual(short, chat.empty_room_line("never-posted",
                                                     "never-posted") + "\n")


class ShortFlagTest(_HomedBase):

    def test_short_gives_a_native_reader_the_short_read(self):  # noqa: VACUOUS_ASSERTION — equality with the local reader's short read and inequality with the native read, all three non-empty reads of the same fixture
        post_fixture()
        native, _err = self.read("--room", "main")
        asked, _err = self.read("--room", "main", "--short")
        local, _err = self.read("--room", "main", family=LEAN)
        self.assertEqual(asked, local)
        self.assertNotEqual(asked, native)

    def test_a_read_no_door_admits_cuts_nothing_and_says_so(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes, and the control read as the seat asserted to cut the same row
        """A process that declared no seat and holds no session (the owner's
        shell) cannot say which rows nobody owes its reader, so `--short`
        cuts none and says why in one stderr line. RED at 3b9a8f48cfd: it
        cut the row all the same. The control: the same read as the seat
        cuts it."""
        row = chat.post("@kimi review this " + "word " * 100, who="bob")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["read", "--room", "main", "--short"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(out.getvalue(), self.full_render())
        said = [x for x in err.getvalue().splitlines()
                if x.startswith("helm chat read: no row is cut")]
        self.assertEqual(len(said), 1, err.getvalue())
        short, _err = self.read("--room", "main", "--short")
        self.cut_line(short, row)

    def test_contradicting_flags_refuse_before_any_row_prints(self):  # noqa: VACUOUS_ASSERTION — each refusal is asserted by its rc 2 and the stderr line naming the verb; the empty stdout is the contract
        chat.post("one row", who="bob")
        for args in (("--short", "--full"), ("--short", "--follow"),
                     ("--id", "abcd1234", "--since", "0"),
                     ("--id", "abcd1234", "--short")):
            with self.subTest(args=args):
                out, err = self.read(*args, rc=2)
                self.assertEqual(out, "")
                self.assertIn("helm chat read:", err)


class WholeRowReaderTest(_HomedBase):

    def test_id_prints_the_whole_row_a_short_read_cut(self):
        post_fixture()
        m = self.row("@kimi review lane/feature-5")
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertNotIn("- check 11", short)
        self.assertIn(m["id"][:chat.ID_SHOWN], short)
        whole, _err = self.read("--id", m["id"][:chat.ID_SHOWN], family=LEAN)
        rows = chat.read("main")[0]
        i = next(k for k, r in enumerate(rows) if r.get("id") == m["id"])
        self.assertEqual(whole, "helm chat [main] %s\n" % chat.run_line(
            [(i, m)], chat.read_prefix(rows), chat.index_rows(rows)))
        self.assertIn("- check 11: the arm for cell 11", whole)

    def test_id_finds_a_dm_and_refuses_an_unknown_id(self):
        seats.dm(SEAT, "the direct ask\nwith its second line", who="bob")
        m = self.row("the direct ask", room=chat.dm_room(SEAT))
        whole, _err = self.read("--id", m["id"][:chat.ID_SHOWN])
        self.assertIn("the direct ask\nwith its second line", whole)
        self.assertTrue(whole.startswith("helm chat [dm] "), whole)
        out, err = self.read("--id", "0123abcd", rc=1)
        self.assertEqual(out, "")
        self.assertIn("no message matches id '0123abcd'", err)


class APartialLineIsDefenceTest(_ShortBase):
    """The short read never cuts an owed row, and the machinery after the
    cut stays as the defence for a predicate that ever disagrees with the
    hook: a cut line is a `pull_delivery.Partial`, which the discharge does
    not count, and the whole-row reader delivers the row. A double of the
    predicate that owes nothing makes the read cut an owed row."""

    def test_a_cut_owed_row_stays_owed_and_the_whole_rows_do_not(self):
        self.rung(["@gemini long ask " + "z" * 400, "@gemini short ask 2"])
        self.assertEqual(self.counts(), (2, 2))
        with self.owes_nothing():
            short, _err = self.read("--room", "main", family=LEAN)
        self.assertIn("@gemini short ask 2", short)
        self.assertIn("chars]", short)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.hook_texts(pattern=r"@gemini \w+ ask"),
                         ["@gemini long ask"])

    def test_the_whole_row_reader_delivers_the_row_it_printed(self):
        self.rung(["@gemini long ask " + "z" * 400])
        with self.owes_nothing():
            short, _err = self.read("--room", "main", family=LEAN)
        self.assertEqual(self.counts(), (1, 1), short)
        m = self.row("@gemini long ask")
        self.read("--id", m["id"][:chat.ID_SHOWN], family=LEAN)
        self.assertEqual(self.counts(), (0, 0))


class ANumberAcksOnlyARowPrintedWholeTest(_ShortBase):
    """`helm chat ack [n]` names the row the reader's last read printed at
    [n]. A read that printed a row cut (the owes-nothing double) printed it
    in part, so its [n] refuses until `helm chat read --id` printed that row
    whole; a whole row's [n] acks."""

    def ack(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(["--seat", SEAT]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(["ack"] + list(args))
        return rc, out.getvalue(), err.getvalue()

    def acked(self):
        return [a for m in chat.read("main")[0] if m.get("ack")
                for a in chat.ack_ids(m)]

    def owed(self):
        """A long row and a short row addressed to the seat, read short
        under the double -> (long, short): [1] is cut, [2] whole."""
        long = chat.post("@gemini long ask " + "z" * 400, who="bob")
        short = chat.post("@gemini short ask", who="bob")
        with self.owes_nothing():
            out, _err = self.read("--room", "main", family=LEAN)
        cut, whole = out.splitlines()[:2]
        self.assertTrue(cut.startswith("[1] %s" % long["id"][:8]), out)
        self.assertIn("chars]", cut)
        self.assertTrue(whole.startswith("[2] %s" % short["id"][:8]), out)
        self.assertNotIn("chars]", whole)
        return long, short

    def test_a_whole_rows_number_acks(self):
        _long, short = self.owed()
        rc, said, err = self.ack("[2]")
        self.assertEqual(rc, 0, err)
        self.assertIn(short["id"][:8], said)
        self.assertEqual(self.acked(), [short["id"]])

    def test_a_cut_rows_number_refuses_in_one_line_and_acks_nothing(self):
        long, _short = self.owed()
        self.assertEqual(self.acked(), [])
        for tokens in (("[1]",), ("1",), ("[2]", "[1]")):
            with self.subTest(tokens=tokens):
                rc, said, err = self.ack(*tokens)
                self.assertEqual(rc, 1, (said, err))
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertIn("helm chat read --id %s" % long["id"], err)
                self.assertIn("nothing was acked", err)
                self.assertEqual(self.acked(), [])
        rc, _said, err = self.ack("[2]")
        self.assertEqual(rc, 0, "the whole row's [n] still acks: " + err)
        self.assertEqual(self.acked(), [_short["id"]])

    def test_a_cut_rows_number_acks_once_the_whole_row_reader_printed_it(self):
        long, _short = self.owed()
        whole, _err = self.read("--id", long["id"][:chat.ID_SHOWN],
                                family=LEAN)
        self.assertIn("z" * 400, whole)
        rc, said, err = self.ack("[1]")
        self.assertEqual(rc, 0, err)
        self.assertIn(long["id"][:8], said)
        self.assertEqual(self.acked(), [long["id"]])


class AnOwnerDoorRowIsNeverCutTest(_HomedBase):
    """A row an owner door stamped (its origin one of `seats.OWNER_RAILS`)
    prints whole, whatever name it carries: the web surface posts under
    its name box and the TUI under HELM_CHAT_NAME, neither of which need
    be one of `owner_names()`, and the owner-unread marker drops on every
    such post. The marker (`chat.consume`) says the owner's rows were read,
    so a read moves it past every row it printed, whole or cut
    (`chatshort.seen`); only a cut owner-door row, which the short read no
    longer makes, holds it. SeatsBase names the owner daria; the reader is
    homed in helm, so nothing else keeps these main rows whole."""

    def owner(self, text, who="daria"):
        row = chat.post(text, who=who, origin="web")
        chat.mark_owner_unread("main")
        return row

    def marked(self):
        return os.path.exists(chat.marker_path("main"))

    def test_an_owner_rows_long_body_prints_whole(self):
        body = "hold every land until I say\n" + "because " * 60
        owner = self.owner(body)
        bob = chat.post("bob said: " + body, who="bob")
        name_only = chat.post("daria but not a door: " + body, who="daria")
        self.assertTrue(self.marked())
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertWhole(short, owner)
        self.cut_line(short, bob)
        self.cut_line(short, name_only)
        self.assertTrue(short.endswith(chatshort.legend_line(2) + "\n"))
        self.assertFalse(self.marked())

    def test_an_owner_door_row_under_another_name_prints_whole(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes and the marker asserted present before the read
        """RED at 3b9a8f48cfd: 'never cut' asked for one of the owner's
        names, so his web post under the name box's other name was cut at
        160 characters, and the marker cleared past it all the same."""
        body = "hold every land until I say\n    " + "because " * 60
        owner = self.owner(body, who="davidweb")
        self.assertTrue(self.marked())
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertWhole(short, owner)
        self.assertEqual(short, self.full_render())
        self.assertFalse(self.marked())

    def test_an_owner_short_row_read_clears_the_marker(self):
        self.owner("hold every land")
        self.assertTrue(self.marked())
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertIn("daria: hold every land", short)
        self.assertFalse(self.marked())

    def test_a_row_cut_before_the_owners_post_clears_the_marker(self):
        """The marker says the owner's rows were read, and the owner's post
        printed whole, so a cut row of someone else's before it must not
        hold it: held, one old long room row would keep the
        owner-chat-unread steer firing on every prompt of a seat that reads
        short, a nag no read of the room could end."""
        chat.post("@kimi review this " + "word " * 100, who="bob")
        self.owner("hold every land")
        self.assertTrue(self.marked())
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertIn("chars]", short)
        self.assertIn("daria: hold every land", short)
        self.assertFalse(self.marked(), short)

    def test_a_cut_owner_door_row_holds_the_marker_at_itself(self):
        """The defence `chatshort.seen` keeps for a cut owner-door row,
        which the short read no longer makes: the owes-nothing double cuts
        the owner's long web post, under a name that is not one of his, as
        it cuts anyone's. The cut row of bob's before it does not hold the
        marker, the cut owner-door row holds it at its own index, and a read
        whose window starts past it clears it."""
        chat.post("@kimi review this " + "word " * 100, who="bob")
        owner = self.owner("hold every land " + "because " * 40,
                           who="davidweb")
        chat.post("landed, thanks", who="bob")
        rows = chat.read("main")[0]
        at = next(i for i, m in enumerate(rows) if m["id"] == owner["id"])
        with self.owes_nothing(), \
                mock.patch.object(chat, "consume",
                                  wraps=chat.consume) as consume:
            short, _err = self.read("--room", "main", family=LEAN)
        self.cut_line(short, owner)
        self.assertEqual(consume.call_args_list, [mock.call("main", at)])
        self.assertTrue(self.marked(), short)
        self.read("--room", "main", "--since", str(at + 1), family=LEAN)
        self.assertFalse(self.marked())

    def test_a_row_cut_after_the_owners_post_clears_the_marker(self):
        self.owner("hold every land")
        chat.post("@kimi review this " + "word " * 100, who="bob")
        self.assertTrue(self.marked())
        short, _err = self.read("--room", "main", family=LEAN)
        self.assertIn("chars]", short)
        self.assertFalse(self.marked(), short)


class OneNamespaceTest(_HomedBase):
    """The family and the backend are one launch stamp, so they are read
    from ONE namespace (`home.env_pair`): a HELM_ family never takes a stale
    MELD_ backend, and a MELD_ family never takes a stray HELM_ backend."""

    def test_the_family_and_its_backend_come_from_one_namespace(self):
        chat.post("@kimi review this " + "word " * 100, who="bob")
        native = self.full_render()
        short = self.read("--room", "main", family=LEAN)[0].splitlines()
        self.assertEqual(short[1], chatshort.legend_line(1))
        stamps = (
            ("HELM pair, proxy", {"HELM_MODEL_FAMILY": LEAN,
                                  "HELM_MODEL_BACKEND": "proxy"}),
            ("HELM pair, native", {"HELM_MODEL_FAMILY": LEAN,
                                   "HELM_MODEL_BACKEND": "native"}),
            ("MELD pair, proxy", {"MELD_MODEL_FAMILY": LEAN,
                                  "MELD_MODEL_BACKEND": "proxy"}),
            ("MELD pair, native", {"MELD_MODEL_FAMILY": LEAN,
                                   "MELD_MODEL_BACKEND": "native"}),
            ("HELM native pair over a MELD lean pair", {
                "HELM_MODEL_FAMILY": "claude", "HELM_MODEL_BACKEND": "native",
                "MELD_MODEL_FAMILY": LEAN, "MELD_MODEL_BACKEND": "proxy"}),
            ("HELM family, stale MELD backend", {
                "HELM_MODEL_FAMILY": LEAN, "MELD_MODEL_BACKEND": "native"}),
            ("MELD family, stray HELM backend", {
                "MELD_MODEL_FAMILY": LEAN, "HELM_MODEL_BACKEND": "native"}))
        reads = {name: self.read("--room", "main", stamp=stamp)[0]
                 for name, stamp in stamps}
        got = {name: "short" if out.splitlines() == short
               else "full" if out == native else out
               for name, out in reads.items()}
        self.assertEqual(got, {
            "HELM pair, proxy": "short", "HELM pair, native": "full",
            "MELD pair, proxy": "short", "MELD pair, native": "full",
            "HELM native pair over a MELD lean pair": "full",
            "HELM family, stale MELD backend": "short",
            "MELD family, stray HELM backend": "short"})


class AnOrcaPaneOutsideHelmTest(_HomedBase):
    """ONE SOURCE OF THE READER'S SCOPE (R1 of the approval-tier read at
    41f8d8ad857). The reader's roster home is helm, its explicit join room,
    and it works as a pane Orca opened in another project's checkout
    (`hooks.orca_foreign`). The tool-boundary hook scopes such a pane by the
    room it homes to from the session's cwd, that project's room, and not
    by the roster's home (`seats_address.seat_scope`'s own_room), so a
    person's plain row in that room is owed to the reader. Only the
    registry is a double, as in test_delivery_truth's Orca door arms: helm's
    checkout is helm's project and every other path is the other one's."""

    PROJECT = "sitka-inc"

    @contextlib.contextmanager
    def pane(self):
        """The reader as a pane Orca opened in PROJECT's checkout, its launch
        seam homing it there, for the body of one arm."""
        root = os.path.join(self.tmp, "helm-checkout")
        self.cwd = os.path.join(self.tmp, self.PROJECT, "work")
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        os.makedirs(self.cwd, exist_ok=True)

        def project(path):
            return "helm" if os.path.abspath(str(path)) == root \
                else self.PROJECT
        with mock.patch.dict(os.environ, {
                    "ORCA_PANE_KEY": "pane-key-fixture",
                    "HELM_CHAT_ROOM": self.PROJECT,
                    "HELM_CHAT_ROOM_SOURCE": "derived",
                    "HELM_NO_TREE_WARNING": "1"}), \
                mock.patch.object(hooks, "helm_bin", return_value=os.path.join(
                    root, "bin", "helm")), \
                mock.patch("helm.inject._ledger.project_for_cwd", project):
            self.assertIs(hooks.orca_foreign(), True,
                          "control: the double places the pane outside helm")
            self.assertEqual(seats.seat_scope(SEAT)["home"], "helm",
                             "control: the roster homes the reader in helm")
            yield

    def hook(self):
        """One PostToolUse boundary through the installed door, `helm chat
        deliver --hook-json` with the session's payload on stdin, the answer
        read off fd 1 -> the contexts it showed."""
        payload = json.dumps({
            "session_id": SID, "cwd": self.cwd,
            "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_response": {"originalFile": "x" * 8}})
        stdin = io.TextIOWrapper(io.BytesIO(payload.encode()),
                                 encoding="utf-8")
        with tempfile.TemporaryFile() as fd1:
            saved = os.dup(1)
            try:
                os.dup2(fd1.fileno(), 1)
                with mock.patch.object(sys, "stdin", stdin), \
                        _tmp_declaring(["--seat", SEAT]), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = cli.main(["chat", "deliver", "--hook-json"])
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            fd1.seek(0)
            raw = fd1.read().decode("utf-8")
        self.assertEqual(rc, 0)
        return [json.loads(x)["hookSpecificOutput"]["additionalContext"]
                for x in raw.splitlines() if x.strip()]

    def drain(self, limit=8):
        """Every context the pane's boundaries show, one per boundary, until
        one shows nothing."""
        shown = []
        for _ in range(limit):
            got = self.hook()
            if not got:
                return shown
            shown.extend(got)
        self.fail("the boundary never went quiet: %r" % shown)

    def test_a_plain_row_of_the_panes_project_prints_whole_and_is_delivered(self):  # noqa: VACUOUS_ASSERTION — the empty drain after the read is paired with the control row the same boundaries show after it, and the read equals the full renderer's non-empty bytes
        """RED at 41f8d8ad857: the short read asked the roster's home, so it
        cut the row, and the hook then showed it clipped."""
        with self.pane():
            chat.post("the preview deploy is up", who="bob", room=self.PROJECT)
            self.drain()        # the pane's cursor in its room now exists
            row = chat.post("plain chatter, " + BRIEF, who="bob",
                            room=self.PROJECT)
            short, _err = self.read("--room", self.PROJECT, family=LEAN)
            self.assertWhole(short, row, self.PROJECT)
            self.assertEqual(short, self.full_render(self.PROJECT))
            self.assertEqual(self.drain(), [], "the read delivered the row")
            chat.post("control: a plain row after the read", who="bob",
                      room=self.PROJECT)
            shown = self.drain()
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("control: a plain row after the read", shown[0])

    def test_an_all_row_in_main_the_waiter_owes_prints_whole(self):  # noqa: VACUOUS_ASSERTION — the empty ring and drain follow the whole @all row and the cut plain row asserted in the read's non-empty stdout, and the scope controls that the waiter's scope owes the row
        """THE WAITER'S SCOPE IS OWED TOO (R4 of the approval-tier read at
        d5d70cb8dff). The pane's hook, narrowed to its own mail, owes no
        @all in #main; the reader's beacon waiter calls `deliver_any` with no
        `project_only`, so it scopes by the roster's home, rings a long @all
        there and holds it, and the hook shows every held row whatever its
        own scope says. So the row is owed, and the short read prints it
        whole; the read delivers it, the waiter then rings nothing and the
        hook shows nothing. A long plain row in #main, which neither scope
        owes, is still cut, the control. RED at d5d70cb8dff: the @all row
        was cut, the waiter held it, and the hook showed it clipped."""
        with self.pane():
            row = chat.post("@all " + BRIEF, who="bob", room="main")
            plain = chat.post("plain chatter in main, " + BRIEF, who="bob",
                              room="main")
            hook = seats_identity.boundary_scope(SEAT, self.PROJECT, True)
            waiter = seats_identity.boundary_scope(SEAT, None)
            for m, owed in ((row, (False, True)), (plain, (False, False))):
                self.assertEqual(tuple(seats_identity.deliverable(
                    m, SEAT, "main", sc, True, False) for sc in (hook, waiter)),
                    owed, "control: which scope owes %r" % m["text"][:24])
            short, _err = self.read("--room", "main", family=LEAN)
            self.assertWhole(short, row)
            self.cut_line(short, plain)
            self.assertTrue(short.endswith(chatshort.legend_line(1) + "\n"),
                            short)
            lines, _seen = self.follow(passes=2, clock=True)
            self.assertEqual(lines, [], "the read delivered the @all row")
            self.assertEqual(self.drain(), [])

    def test_a_waiter_scope_that_raises_cuts_no_row(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes, and the control read without the fault cuts the same row
        """The waiter's scope is an input the whole read asks, so its lookup
        raising keeps every row whole and says so on stderr, as the hook's
        scope raising does (R3). RED at d5d70cb8dff: the read never asked
        it, so the plain row was cut."""
        real = seats_identity.boundary_scope

        def flaky(seat, room, project_only=False):
            if not project_only:
                raise OSError(5, "roster unreadable")
            return real(seat, room, project_only)
        with self.pane():
            plain = chat.post("plain chatter in main, " + BRIEF, who="bob",
                              room="main")
            with mock.patch.object(seats_identity, "boundary_scope", flaky):
                short, err = self.read("--room", "main", family=LEAN)
            self.assertEqual(short, self.full_render())
            said = [x for x in err.splitlines()
                    if x.startswith("helm chat read: no row is cut")]
            self.assertEqual(len(said), 1, err)
            self.assertIn("roster unreadable", said[0])
            short, _err = self.read("--room", "main", family=LEAN)
        self.cut_line(short, plain)

    def test_a_facade_patch_of_the_scope_reaches_the_hooks_scope(self):
        """`seats_address.boundary_scope` is where the hook's pass and the
        short read both ask for the reader's scope, and it resolves
        `seat_scope` in seats_address, so a patch of seats.seat_scope
        reaches it as it reached `deliver_any` when the narrowing lived
        there (seats._IMPL_MODULES)."""
        with mock.patch.object(seats, "seat_scope",
                               return_value={"home": "pinned"}) as scope:
            self.assertEqual(seats_identity.boundary_scope(
                SEAT, self.PROJECT, True), {"home": "pinned"})
            self.assertEqual(seats_identity.boundary_scope(SEAT, "x"),
                             {"home": "pinned"})
        self.assertEqual(scope.call_args_list, [
            mock.call(SEAT, own_room=self.PROJECT),
            mock.call(SEAT, own_room=None)])

    def test_inside_helm_the_same_row_is_cut(self):  # noqa: VACUOUS_ASSERTION — the cut line's own text, the folded body and the count of what was cut, is the positive control
        """MUST-HIT for the arm above: a pane the Orca door does not narrow is
        scoped by the roster's home, helm, where the project's plain row is
        owed to nobody, so the short read cuts it. Only the pane's scope
        makes the row owed."""
        row = chat.post("plain chatter, " + BRIEF, who="bob",
                        room=self.PROJECT)
        with mock.patch.dict(os.environ):
            os.environ.pop("ORCA_PANE_KEY", None)
            self.assertIs(hooks.orca_foreign(), False)
            short, _err = self.read("--room", self.PROJECT, family=LEAN)
        self.cut_line(short, row, self.PROJECT)


class AFailingInputFailsTowardWholeTest(_HomedBase):
    """EVERY INPUT `owed_to` READS FAILS TOWARD WHOLE (R3 of the
    approval-tier read at 41f8d8ad857). `deliverable` runs row by row as the
    read prints, outside the guard around the scope and the wake cursor, so
    a raise there ended the read mid-output. A row whose question raised is
    owed: it prints whole, the read goes on and exits 0, and one stderr line
    names the row. An input the whole read asks (the scope) failing keeps
    every row whole, as a read no door admits does."""

    def test_a_row_whose_deliverable_raises_prints_whole_and_the_read_goes_on(self):
        """RED at 41f8d8ad857: the raise left `helm chat read` after the rows
        before it, and the rows after it never printed."""
        before = chat.post("@kimi before, " + BRIEF, who="bob")
        broken = chat.post("@kimi broken, " + BRIEF, who="bob")
        after = chat.post("@kimi after, " + BRIEF, who="bob")
        real = seats_identity.deliverable

        def flaky(m, *args, **kw):
            if m.get("id") == broken["id"]:
                raise RuntimeError("the roster went away mid-read")
            return real(m, *args, **kw)
        with mock.patch.object(seats_identity, "deliverable", flaky):
            short, err = self.read("--room", "main", family=LEAN)
        self.cut_line(short, before)
        self.cut_line(short, after)
        self.assertWhole(short, broken)
        self.assertTrue(short.endswith(chatshort.legend_line(2) + "\n"),
                        short)
        said = [x for x in err.splitlines()
                if x.startswith("helm chat read: row ")]
        self.assertEqual(len(said), 1, err)
        self.assertIn(broken["id"][:chat.ID_SHOWN], said[0])
        self.assertIn("the roster went away mid-read", said[0])

    def test_a_scope_lookup_that_raises_cuts_no_row(self):  # noqa: VACUOUS_ASSERTION — exact equality to the full renderer's non-empty bytes, and the control read without the fault cuts the same row
        """The pane test the hook's scope asks (`hooks.orca_foreign`) raising
        keeps every row whole and says so on stderr. RED at 41f8d8ad857: the
        short read never asked it, so the row was cut."""
        row = chat.post("@kimi review this " + "word " * 100, who="bob")
        with mock.patch.object(hooks, "orca_foreign",
                               side_effect=OSError(5, "registry unreadable")):
            short, err = self.read("--room", "main", family=LEAN)
        self.assertEqual(short, self.full_render())
        said = [x for x in err.splitlines()
                if x.startswith("helm chat read: no row is cut")]
        self.assertEqual(len(said), 1, err)
        self.assertIn("registry unreadable", said[0])
        short, _err = self.read("--room", "main", family=LEAN)
        self.cut_line(short, row)


class ClassificationTest(unittest.TestCase):

    def test_the_lean_families_are_the_ones_denied_the_local_unused_tools(self):
        lean = [f for f in seat_catalog.FAMILIES
                if seat_catalog.context_lean(f)]
        self.assertTrue(lean, "no family reads short")
        for family in lean:
            denied = seat_catalog.denied_tools(family)
            for tool in seat_catalog.LOCAL_UNUSED_TOOLS:
                self.assertIn(tool, denied, family)
        self.assertFalse(seat_catalog.context_lean("claude"))
        self.assertFalse(seat_catalog.context_lean(NOT_LEAN))
        self.assertFalse(seat_catalog.context_lean(None))


if __name__ == "__main__":
    unittest.main()
