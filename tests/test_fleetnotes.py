#!/usr/bin/env python3
"""helm fleetnotes — the fleet's durable notes TO THE OWNER, and the cockpit
surface that renders them.

These notes existed before this module did: agents wrote the owner's "what
happened overnight" answers as cells on the web cockpit's multiplayer demo
board. That board is tmpfs (/dev/shm) and disposable by design, so a reboot ate
exactly the notes that explain the reboot; and it sat behind a nav tab called
"cave", a word that means the ATTESTATION NODE one tab over. The store moved to
disk and the tab was retired — so what this file pins is (a) the store keeps
what was written, (b) it stays BOUNDED and refuses loudly at both caps rather
than trimming the owner's page, and (c) every read is fail-open, because one of
the readers is the owner's home page and a corrupt file may not blank it.

Hermetic: tmp HELM_FLEET_NOTES + tmp HELM_HOME. The real ~/.helm is never
touched.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-notes-home-", var="HELM_HOME")

from helm import fleetnotes, web  # noqa: E402


class FleetNotesFreshnessTest(unittest.TestCase):
    """#234 — the owner asked TWICE why the card showed old notes instead of
    the latest. Measured that night: one note 2 minutes old and five 5-8 DAYS
    old, all rendered as equal rows under a header reading "6 left for you".
    The ORDER was already newest-first and each row already showed its age;
    nothing SEPARATED them, so a live note sat in a week-stale list.

    Staleness here is a DISPLAY TIER and never a deletion — every test below
    that dims a note also asserts it is still reachable."""

    NOW = 1785900000                      # a fixed clock: these tests own time

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-fresh-")
        self.path = os.path.join(self.tmp, "fleet-notes.json")
        self.prior = os.environ.get("HELM_FLEET_NOTES")
        os.environ["HELM_FLEET_NOTES"] = self.path

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_FLEET_NOTES", None)
        else:
            os.environ["HELM_FLEET_NOTES"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stamp(self, secs_ago):
        import calendar
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(self.NOW - secs_ago))

    def _seed(self, **ages):
        notes = {k: {"text": k, "by": "seat", "ts": self._stamp(v)}
                 for k, v in ages.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(notes, f)

    def test_the_boundary_is_exact_in_both_directions(self):
        """A window nobody pins drifts. 48h is the contract."""
        w = fleetnotes.FRESH_WINDOW_S
        self.assertFalse(fleetnotes.is_stale(self._stamp(w - 60), self.NOW),
                         "a note one minute INSIDE the window went stale")
        self.assertFalse(fleetnotes.is_stale(self._stamp(w), self.NOW),
                         "the boundary itself must still be fresh")
        self.assertTrue(fleetnotes.is_stale(self._stamp(w + 60), self.NOW),
                        "a note one minute PAST the window stayed fresh")

    def test_an_unreadable_stamp_is_never_dimmed(self):
        """The failure to prefer is the one that KEEPS a note in front of the
        owner. Dimming a note we could not date would hide it on the strength
        of a number nobody read — this whole lane exists because a live note
        was not visible."""
        for bad in ("", None, "not-a-date", "2026-13-45T99:99:99Z"):
            self.assertIsNone(fleetnotes.age_s(bad, self.NOW), repr(bad))
            self.assertFalse(fleetnotes.is_stale(bad, self.NOW), repr(bad))
        # MUST-HIT CONTROL: a readable OLD stamp really does go stale, so this
        # is not a predicate that answers False for everything.
        self.assertTrue(fleetnotes.is_stale(
            self._stamp(fleetnotes.FRESH_WINDOW_S * 3), self.NOW))

    def test_nothing_is_ever_dropped_by_the_split(self):
        """The one outcome this lane must never produce is a note that stopped
        being reachable."""
        self._seed(live=60, yesterday=3600 * 20,
                   old_a=86400 * 5, old_b=86400 * 8, old_c=86400 * 30)
        got = fleetnotes.rows(now=self.NOW)
        fresh, older = fleetnotes.split_fresh(got)
        self.assertEqual(len(fresh) + len(older), len(got))
        self.assertEqual(len(got), 5, "the fixture did not seed 5 notes")
        self.assertEqual([n["key"] for n in fresh], ["live", "yesterday"])
        self.assertEqual(sorted(n["key"] for n in older),
                         ["old_a", "old_b", "old_c"])
        # every seeded key survives the round trip, in one bucket or the other
        self.assertEqual(
            sorted(n["key"] for n in fresh + older),
            ["live", "old_a", "old_b", "old_c", "yesterday"])

    def test_staleness_does_not_reorder_the_feed(self):
        """A tier, not a re-sort. Nothing may move under the owner between two
        reads just because a note crossed the boundary."""
        self._seed(live=60, old_a=86400 * 5, mid=3600 * 10)
        order = [n["key"] for n in fleetnotes.rows(now=self.NOW)]
        self.assertEqual(order, ["live", "mid", "old_a"],
                         "rows() is no longer newest-first")

    def test_SPLITTING_NEVER_REORDERS_including_undatable_notes(self):
        """codex's P1: an undatable note sorted LAST (an empty stamp is the
        smallest string) while classifying FRESH, so partitioning lifted it
        ABOVE genuinely older notes — the feed order and the rendered order
        disagreed. My previous no-reorder test never fed it an undatable note,
        so it passed while the property was false.

        The invariant asserted here is the strong one: concatenating
        (fresh + older) reproduces rows() EXACTLY."""
        notes = {"undated": {"text": "u", "by": "s", "ts": ""},
                 "garbage": {"text": "g", "by": "s", "ts": "not-a-date"},
                 "live": {"text": "l", "by": "s", "ts": self._stamp(60)},
                 "old": {"text": "o", "by": "s", "ts": self._stamp(86400 * 6)}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(notes, f)
        got = fleetnotes.rows(now=self.NOW)
        fresh, older = fleetnotes.split_fresh(got)
        self.assertEqual([n["key"] for n in fresh + older],
                         [n["key"] for n in got],
                         "splitting reordered the feed")
        # and the undatable ones lead, which is what makes that possible
        self.assertEqual([n["key"] for n in got][:2], ["garbage", "undated"],
                         "undatable notes are no longer first, so the split "
                         "cannot be order-preserving")
        self.assertEqual([n["key"] for n in older], ["old"])

    def test_RETIRING_the_newest_note_does_not_reorder_the_feed(self):
        """codex r2: I fixed the undatable reorder, then ADDED retirement and
        broke the same invariant again — retiring the NEWEST note left it at the
        top of the feed and in the bottom section. My no-reorder test did not
        retire anything, so it passed while the property was false. Third time
        in this lane a test was bound to the cases already in my head.

        The cure is structural: the feed order IS the tier order, so splitting
        cannot reorder by construction rather than by coincidence."""
        self._seed(newest=60, mid=3600, oldest=7200)
        ok, err = fleetnotes.retire("newest")     # retire the TOP row
        self.assertTrue(ok, err)
        got = fleetnotes.rows(now=self.NOW)
        fresh, older = fleetnotes.split_fresh(got)
        self.assertEqual([n["key"] for n in fresh + older],
                         [n["key"] for n in got],
                         "retiring the newest note reordered the feed")
        self.assertEqual([n["key"] for n in older], ["newest"])
        self.assertEqual([n["key"] for n in fresh], ["mid", "oldest"])

    def test_every_tier_combination_preserves_the_order(self):
        """The general property, over the whole cross-product this lane can
        produce: undatable, fresh, stale-by-age, and retired-at-any-age."""
        notes = {"undated": {"text": "u", "by": "s", "ts": ""},
                 "fresh": {"text": "f", "by": "s", "ts": self._stamp(60)},
                 "aged": {"text": "a", "by": "s",
                          "ts": self._stamp(86400 * 9)},
                 "retired_new": {"text": "rn", "by": "s",
                                 "ts": self._stamp(120), "retired": True},
                 "retired_old": {"text": "ro", "by": "s",
                                 "ts": self._stamp(86400 * 9), "retired": True}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(notes, f)
        got = fleetnotes.rows(now=self.NOW)
        fresh, older = fleetnotes.split_fresh(got)
        self.assertEqual([n["key"] for n in fresh + older],
                         [n["key"] for n in got])
        self.assertEqual(len(fresh) + len(older), len(notes))
        self.assertEqual(sorted(n["key"] for n in fresh),
                         ["fresh", "undated"])
        self.assertEqual(sorted(n["key"] for n in older),
                         ["aged", "retired_new", "retired_old"])

    def test_retiring_a_YOUNG_note_from_the_MIDDLE_holds_the_invariant(self):
        """codex named this case in the meld before either of us had tested it:
        a note that is young (well inside the window) and sits in the MIDDLE of
        the feed, retired. It is the hardest input for the tier sort, because
        neither age nor position puts it where it belongs.

        The consequence is intended and worth stating out loud: retiring a note
        MOVES it in the feed, because the feed order IS the tier order now. That
        is the price of making splitting order-preserving by construction, and
        it is the right trade — a note the owner cannot see is worse than a note
        that changed position when its author retired it."""
        notes = {"newest": {"text": "n", "by": "s", "ts": self._stamp(60)},
                 "middle": {"text": "m", "by": "s", "ts": self._stamp(600),
                            "retired": True},
                 "third": {"text": "t", "by": "s", "ts": self._stamp(1200)},
                 "ancient": {"text": "a", "by": "s",
                             "ts": self._stamp(86400 * 9)}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(notes, f)
        got = fleetnotes.rows(now=self.NOW)
        fresh, older = fleetnotes.split_fresh(got)
        self.assertEqual([n["key"] for n in fresh + older],
                         [n["key"] for n in got],
                         "a young retired note in the middle broke the order")
        self.assertEqual([n["key"] for n in fresh], ["newest", "third"])
        self.assertEqual([n["key"] for n in older], ["middle", "ancient"],
                         "the retired young note must fold WITH the old ones, "
                         "and above them — it is newer")
        self.assertEqual(len(fresh) + len(older), 4, "a note was dropped")

    def test_age_and_staleness_describe_ONE_instant(self):
        """age_s and stale each fell through to their own time.time() when now
        was None, so a row could report 47h59m59s beside stale=True at the
        boundary. One clock read, threaded."""
        w = fleetnotes.FRESH_WINDOW_S
        self._seed(edge=w + 1)
        # UNCONDITIONAL CONTROL before the loop: an empty read would make every
        # assertion below unreachable and the test would pass having compared
        # nothing.
        first = fleetnotes.rows()
        self.assertEqual(len(first), 1, "the fixture seeded no readable row")
        self.assertIsNotNone(first[0]["age_s"], "the seeded stamp did not parse")
        for _ in range(200):              # hammer it: a split clock flaps here
            row = fleetnotes.rows()[0]
            self.assertEqual(row["stale"], row["age_s"] > w,
                             "age_s=%s and stale=%s disagree — two clock reads"
                             % (row["age_s"], row["stale"]))

    def test_retire_is_undoable_and_keeps_the_note_on_disk(self):
        """codex's P2: `rm` DELETES, and I called it a retirement path. A lane
        whose thesis is "dimmed, never dropped" cannot offer deletion as its
        exit arc."""
        self._seed(alpha=60, beta=60)
        ok, err = fleetnotes.retire("alpha")
        self.assertTrue(ok, err)
        rows = {n["key"]: n for n in fleetnotes.rows(now=self.NOW)}
        self.assertTrue(rows["alpha"]["retired"])
        self.assertTrue(rows["alpha"]["stale"],
                        "a retired note still competes for the fresh section")
        self.assertFalse(rows["beta"]["stale"], "retiring one hid another")
        self.assertIn("alpha", fleetnotes.read(),
                      "retire DELETED the note — that is rm, not retire")
        ok, err = fleetnotes.retire("alpha", retired=False)
        self.assertTrue(ok, err)
        back = {n["key"]: n for n in fleetnotes.rows(now=self.NOW)}["alpha"]
        self.assertFalse(back["retired"], "restore left the flag set")
        self.assertFalse(back["stale"], "restore did not bring the note back")
        # rm still deletes — the two verbs stay distinct
        fleetnotes.remove("beta")
        self.assertNotIn("beta", fleetnotes.read())

    def _real_seed(self, **ages):
        """Stamps relative to the ACTUAL clock.

        The seam test cannot use the frozen NOW: it drives cmd_note, which reads
        the wall clock, so seeding one side from a fixed constant made the test
        agree only until the real clock drifted past the boundary — it was
        already dated to expire 2026-08-07 (codex, review of the first #234
        tip). Both surfaces must read the SAME clock, and the only clock the CLI
        has is the real one. Ages are far from the boundary in both directions so
        the test cannot flap."""
        import time
        now = time.time()
        notes = {k: {"text": k, "by": "seat",
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now - v))}
                 for k, v in ages.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(notes, f)

    def test_the_CLI_and_the_row_payload_agree_on_which_notes_are_old(self):
        """THE SEAM. Two surfaces read this boundary — the web card folds stale
        notes, the CLI rules them off. If they ever disagree, one of them is
        lying to the owner about what is current. Testing either alone cannot
        catch that, so this compares the two renderings of ONE seeding.

        BOTH SIDES READ THE REAL CLOCK HERE, deliberately — see _real_seed."""
        self._real_seed(live=60, old_a=86400 * 5, old_b=86400 * 9)
        payload_stale = {n["key"] for n in fleetnotes.rows() if n["stale"]}
        self.assertEqual(payload_stale, {"old_a", "old_b"},
                         "the fixture is not straddling the boundary as "
                         "intended, so the comparison below proves nothing")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fleetnotes.cmd_note(["list"])
        out = buf.getvalue()
        self.assertIn("older than", out, "the CLI printed no divider at all")
        below = out.split("older than", 1)[1]
        cli_stale = {k for k in ("live", "old_a", "old_b") if k in below}
        self.assertEqual(cli_stale, payload_stale,
                         "the CLI and the card disagree about which notes are "
                         "old: CLI=%s payload=%s" % (cli_stale, payload_stale))
        # and the CLI still PRINTS every note — a divider, never a filter
        for k in ("live", "old_a", "old_b"):
            self.assertIn(k, out, "%s vanished from the CLI listing" % k)


class FleetNotesStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-fleetnotes-")
        self.path = os.path.join(self.tmp, "fleet-notes.json")
        self.prior = os.environ.get("HELM_FLEET_NOTES")
        os.environ["HELM_FLEET_NOTES"] = self.path

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_FLEET_NOTES", None)
        else:
            os.environ["HELM_FLEET_NOTES"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def disk(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    # ── round trip ──────────────────────────────────────────────────────────

    def test_set_list_rm_round_trip(self):
        row, err = fleetnotes.set_note("reboot-recovery", "all 4 casualties closed",
                                       by="helm-claude-2")
        self.assertIsNone(err)
        self.assertEqual(row["key"], "reboot-recovery")
        self.assertEqual(row["by"], "helm-claude-2")
        # the WRITE reached the file, not just the return value
        self.assertEqual(self.disk()["reboot-recovery"]["text"],
                         "all 4 casualties closed")
        listed = fleetnotes.rows()
        self.assertEqual([r["key"] for r in listed], ["reboot-recovery"])
        self.assertEqual(listed[0]["text"], "all 4 casualties closed")

        ok, err = fleetnotes.remove("reboot-recovery")
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(self.disk(), {})
        self.assertEqual(fleetnotes.rows(), [])

    def test_structured_headline_detail_and_pointer_round_trip(self):  # noqa: VACUOUS_ASSERTION — exact stored and projected fields positively control the transient-warning absence
        row, err = fleetnotes.set_note(
            "landed", "Gate is green", detail="Review the exact receipt",
            goto="ledger", by="codex-2")
        self.assertIsNone(err)
        self.assertNotIn("warning", self.disk()["landed"],
                         "advice is transient, never stored as fleet truth")
        self.assertEqual(row["detail"], "Review the exact receipt")
        listed = fleetnotes.rows()[0]
        self.assertEqual(listed["headline"], "Gate is green")
        self.assertEqual(listed["detail"], "Review the exact receipt")
        self.assertEqual(listed["goto"], "ledger")
        self.assertEqual(listed["text"], "Gate is green",
                         "the compatibility field stays on the wire")

    def test_legacy_positional_provenance_arguments_keep_their_meaning(self):  # noqa: VACUOUS_ASSERTION — exact by/ts values positively control the absent-detail compatibility assertion
        row, err = fleetnotes.set_note(
            "legacy-call", "headline", "legacy-agent",
            "2026-07-01T00:00:00Z", self.path)
        self.assertIsNone(err)
        self.assertEqual(row["by"], "legacy-agent")
        self.assertEqual(row["ts"], "2026-07-01T00:00:00Z")
        self.assertNotIn("detail", self.disk()["legacy-call"])

    def test_legacy_long_body_auto_collapses_without_rewriting_disk(self):  # noqa: VACUOUS_ASSERTION — exact projected detail and ellipsized headline positively control the no-rewrite assertion
        body = "A" * 120 + "\nsecond paragraph with the owner action"
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"legacy": {"text": body, "by": "old", "ts": ""}}, f)
        row = fleetnotes.rows()[0]
        self.assertLessEqual(len(row["headline"]), fleetnotes.MAX_HEADLINE)
        self.assertTrue(row["headline"].endswith("…"))
        self.assertEqual(row["detail"], body)
        self.assertNotIn("detail", self.disk()["legacy"],
                         "projection must not rewrite historical rows")

    def test_rm_of_an_absent_key_is_an_honest_false(self):
        ok, err = fleetnotes.remove("never-written")
        self.assertIs(ok, False)
        self.assertIsNone(err)     # not there is not a failure to write
        self.assertFalse(os.path.exists(self.path),
                         "a no-op rm must not CREATE the store as a side effect")

    def test_env_override_selects_the_file(self):
        self.assertEqual(fleetnotes.path(), self.path)
        fleetnotes.set_note("k", "v")
        self.assertTrue(os.path.exists(self.path))

    def test_rows_are_newest_first(self):
        fleetnotes.set_note("old", "first", ts="2026-07-01T00:00:00Z")
        fleetnotes.set_note("new", "second", ts="2026-07-28T00:00:00Z")
        fleetnotes.set_note("mid", "third", ts="2026-07-14T00:00:00Z")
        self.assertEqual([r["key"] for r in fleetnotes.rows()],
                         ["new", "mid", "old"])

    # ── replace by key (the LWW half the demo board had) ─────────────────────

    def test_a_second_write_REPLACES_the_note_under_that_key(self):
        fleetnotes.set_note("signing-status", "signing is down", by="ds4pro")
        row, err = fleetnotes.set_note("signing-status", "signing LIVE fleet-wide",
                                       by="helm-claude-2")
        self.assertIsNone(err)
        self.assertEqual(len(self.disk()), 1, "replace, never append a second row")
        self.assertEqual(row["text"], "signing LIVE fleet-wide")
        self.assertEqual(self.disk()["signing-status"]["by"], "helm-claude-2")

    # ── the caps: both REFUSE, neither trims ────────────────────────────────

    def test_key_cap_refuses_loudly_and_names_a_key_to_retire(self):
        for i in range(fleetnotes.MAX_KEYS):
            _row, err = fleetnotes.set_note("k%02d" % i, "note %d" % i,
                                            ts="2026-07-%02dT00:00:00Z" % (i % 28 + 1))
            self.assertIsNone(err, "key %d should fit under the cap" % i)
            # POSITIVE CONTROL on the same observable: a SUCCESSFUL set returns
            # a row. Without it, a set_note that returned None for everything
            # would satisfy the assertIsNone(row) below and read as a cap
            # refusal that never happened.
            self.assertIsNotNone(_row, "key %d returned no row at all" % i)
        row, err = fleetnotes.set_note("one-too-many", "this must not land")
        self.assertIsNone(row)
        self.assertIn("the cap", err)
        # the refusal must say HOW to fix it, and there are now TWO ways —
        # retire (undoable) and rm (destructive). Asserting both keeps the
        # message from silently losing one; asserting the literal old string
        # would have failed an IMPROVED message, which is what it just did.
        self.assertIn("helm note retire", err,
                      "the refusal must offer the undoable way out")
        self.assertIn("rm to DELETE", err,
                      "the refusal must still name deletion, and name it as "
                      "deletion")
        # and the REFUSED write left the store exactly as it was
        on_disk = self.disk()
        self.assertEqual(len(on_disk), fleetnotes.MAX_KEYS)
        self.assertNotIn("one-too-many", on_disk)

    def test_at_the_cap_an_EXISTING_key_still_updates(self):
        """The cap must not freeze the board in whatever state it hit 64 in —
        replacing a note is how the fleet keeps its own status current."""
        for i in range(fleetnotes.MAX_KEYS):
            fleetnotes.set_note("k%02d" % i, "old %d" % i)
        row, err = fleetnotes.set_note("k00", "fresher text")
        self.assertIsNone(err)
        self.assertEqual(row["text"], "fresher text")
        self.assertEqual(len(self.disk()), fleetnotes.MAX_KEYS)

    def test_text_cap_refuses_and_says_the_two_numbers(self):
        row, err = fleetnotes.set_note("wall", "x" * (fleetnotes.MAX_TEXT + 1))
        self.assertIsNone(row)
        self.assertIn(str(fleetnotes.MAX_TEXT + 1), err)
        self.assertIn(str(fleetnotes.MAX_TEXT), err)
        self.assertFalse(os.path.exists(self.path), "a refused write creates nothing")

    def test_text_at_exactly_the_cap_is_accepted(self):
        _row, err = fleetnotes.set_note("edge", "x" * fleetnotes.MAX_TEXT)
        self.assertIsNone(err)
        self.assertEqual(len(self.disk()["edge"]["text"]), fleetnotes.MAX_TEXT)

    def test_headline_and_detail_share_the_existing_note_cap(self):  # noqa: VACUOUS_ASSERTION — the exact combined-cap refusal positively controls the absent file
        row, err = fleetnotes.set_note(
            "wall", "h" * 80, detail="d" * (fleetnotes.MAX_TEXT - 79))
        self.assertIsNone(row)
        self.assertIn("headline + detail", err)
        self.assertFalse(os.path.exists(self.path))

    def test_goto_accepts_tabs_and_http_but_refuses_active_schemes(self):
        for goto in fleetnotes.GOTO_TABS + ("https://example.test/runbook",):
            with self.subTest(goto=goto):
                row, err = fleetnotes.set_note("pointer", "Act here", goto=goto)
                self.assertIsNone(err)
                self.assertEqual(row["goto"], goto)
        row, err = fleetnotes.set_note(
            "hostile", "Do not click", goto="javascript:alert(1)")
        self.assertIsNone(row)
        self.assertIn("http(s)", err)

    def test_a_hand_planted_unsafe_goto_is_dropped_at_the_render_seam(self):  # noqa: VACUOUS_ASSERTION — the surviving headline positively proves the row rendered while only its pointer was dropped
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"x": {"text": "headline", "goto": "javascript:boom"}}, f)
        row = fleetnotes.rows()[0]
        self.assertEqual(row["headline"], "headline")
        self.assertIsNone(row["goto"])

    def test_empty_text_refuses(self):
        row, err = fleetnotes.set_note("blank", "   ")
        self.assertIsNone(row)
        self.assertIn("non-empty", err)

    # ── the seam: a hostile key or payload never reaches the owner's page ────

    def test_a_control_or_bidi_payload_is_REFUSED_at_the_seam(self):
        for bad in ("hello\x1b[2Jworld", "invoice ‮ gnp.exe"):
            row, err = fleetnotes.set_note("payload", bad)
            self.assertIsNone(row, bad)
            self.assertIn("control or bidi", err)
        self.assertFalse(os.path.exists(self.path))

    def test_a_hand_planted_row_is_LAUNDERED_at_the_render_seam(self):
        """The file is plain JSON and a hand-edit is legal, so a payload can
        enter outside the write seam that refuses it. Both readers are sinks a
        control character reshapes — the owner's page and a terminal — so the
        rows() path launders too (chat._dsan's law)."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"planted": {"text": "clean\x1b[2Jwiped‮gnp.exe",
                                   "by": "ag\x1bent", "ts": "2026-07-01T00:00:00Z"}}, f)
        row = fleetnotes.rows()[0]
        # the ESC and the bidi override are gone; the now-inert printable tail
        # of the sequence stays visible, exactly as chat._dsan leaves it
        self.assertEqual(row["text"], "clean[2Jwiped‮gnp.exe".replace("‮", ""))
        self.assertNotIn("\x1b", row["text"])
        self.assertEqual(row["by"], "agent")

    def test_a_control_char_in_by_never_reaches_the_stored_row(self):
        fleetnotes.set_note("k", "v", by="seat\x1b[31m")
        self.assertEqual(self.disk()["k"]["by"], "seat[31m")

    def test_newlines_and_tabs_are_legitimate_note_text(self):
        _row, err = fleetnotes.set_note("multi", "line one\nline two\tindented")
        self.assertIsNone(err)
        self.assertEqual(self.disk()["multi"]["text"], "line one\nline two\tindented")

    def test_a_hostile_key_is_refused_and_the_message_carries_no_payload(self):
        row, err = fleetnotes.set_note("ok\x1b[31m", "text")
        self.assertIsNone(row)
        self.assertIn("not a legitimate key", err)
        self.assertNotIn("\x1b", err, "the refusal must not carry the ESC onward")

    def test_an_over_long_key_is_refused(self):
        row, err = fleetnotes.set_note("k" * (fleetnotes.MAX_KEY_CHARS + 1), "text")
        self.assertIsNone(row)
        self.assertIn("not a legitimate key", err)

    def test_a_non_string_key_REFUSES_rather_than_raising(self):
        for bad in (None, 17, ["k"]):
            row, err = fleetnotes.set_note(bad, "text")
            self.assertIsNone(row, bad)
            self.assertIn("not a legitimate key", err)

    def test_an_explicit_ts_must_be_a_real_stamp(self):
        row, err = fleetnotes.set_note("k", "v", ts="last tuesday")
        self.assertIsNone(row)
        self.assertIn("ts must be", err)

    # ── fail-open reads (the owner's home page may never blank) ──────────────

    def test_a_missing_file_reads_as_no_notes(self):
        self.assertEqual(fleetnotes.read(), {})
        self.assertEqual(fleetnotes.rows(), [])

    def test_a_corrupt_file_reads_as_no_notes_and_never_raises(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        self.assertEqual(fleetnotes.read(), {})
        self.assertEqual(fleetnotes.rows(), [])

    def test_a_json_file_of_the_WRONG_SHAPE_reads_as_no_notes(self):
        for junk in (["a", "list"], "a bare string", 17):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(junk, f)
            self.assertEqual(fleetnotes.read(), {}, junk)

    def test_junk_rows_are_dropped_and_the_GOOD_ones_survive(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"good": {"text": "keep me", "by": "x", "ts": "2026-07-01T00:00:00Z"},
                       "shapeless": "not a note object",
                       "no-text": {"by": "x"},
                       "bad\x1bkey": {"text": "planted outside the seam"}}, f)
        self.assertEqual([r["key"] for r in fleetnotes.rows()], ["good"])

    def test_a_write_over_a_corrupt_file_repairs_it(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("}}} torn")
        _row, err = fleetnotes.set_note("fresh", "after the corruption")
        self.assertIsNone(err)
        self.assertEqual(self.disk()["fresh"]["text"], "after the corruption")

    def test_the_write_is_atomic_and_leaves_no_tmp_behind(self):
        fleetnotes.set_note("k", "v")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])
        self.assertEqual(self.disk()["k"]["text"], "v")


class FleetNotesVerbTest(unittest.TestCase):
    """`helm note` — the verb agents actually type."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-notecli-")
        self.prior = os.environ.get("HELM_FLEET_NOTES")
        os.environ["HELM_FLEET_NOTES"] = os.path.join(self.tmp, "notes.json")

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_FLEET_NOTES", None)
        else:
            os.environ["HELM_FLEET_NOTES"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_note(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fleetnotes.cmd_note(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_verb_is_registered_on_the_cli(self):
        from helm import cli
        self.assertIn("note", cli.VERBS)
        self.assertIn("note", cli._VERB_HELP)

    def test_set_then_list_json_round_trip(self):
        rc, _out, _err = self.run_note(["set", "cd-open-lanes", "slate", "otherwise", "clean"])
        self.assertEqual(rc, 0)
        rc, out, _err = self.run_note(["list", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(rows[0]["key"], "cd-open-lanes")
        self.assertEqual(rows[0]["text"], "slate otherwise clean")   # argv tail joins

    def test_set_parses_headline_detail_and_goto_without_requiring_quotes(self):
        rc, _out, err = self.run_note([
            "set", "gate", "Review", "is", "ready", "--detail",
            "Open", "the", "receipt", "--goto", "ledger"])
        self.assertEqual((rc, err), (0, ""))
        row = fleetnotes.rows()[0]
        self.assertEqual(row["headline"], "Review is ready")
        self.assertEqual(row["detail"], "Open the receipt")
        self.assertEqual(row["goto"], "ledger")

    def test_a_91_character_headline_warns_but_still_writes(self):  # noqa: VACUOUS_ASSERTION — exact persisted 91-character headline positively proves the warning did not refuse
        rc, _out, err = self.run_note([
            "set", "long", "x" * (fleetnotes.MAX_HEADLINE + 1)])
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", err)
        self.assertIn(str(fleetnotes.MAX_HEADLINE), err)
        self.assertEqual(len(fleetnotes.rows()[0]["headline"]),
                         fleetnotes.MAX_HEADLINE + 1)

    def test_a_90_character_headline_does_not_warn(self):  # noqa: VACUOUS_ASSERTION — exact stored boundary headline is the positive control for empty stderr
        rc, _out, err = self.run_note([
            "set", "edge", "x" * fleetnotes.MAX_HEADLINE])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(fleetnotes.rows()[0]["headline"],
                         "x" * fleetnotes.MAX_HEADLINE)

    def test_multiline_headline_warns_on_the_one_line_it_renders_as(self):
        rc, _out, err = self.run_note([
            "set", "multi", "x" * 60 + "\n" + "y" * 60])
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", err)
        self.assertIn("121 characters", err)

    def test_missing_structured_values_and_unknown_flags_refuse_as_usage(self):  # noqa: VACUOUS_ASSERTION — each subtest positively reaches rc2 plus usage text for its distinct malformed tail
        for tail in (["set", "k", "headline", "--detail"],
                     ["set", "k", "headline", "--goto"],
                     ["set", "k", "headline", "--unknown", "x"]):
            with self.subTest(tail=tail):
                rc, _out, err = self.run_note(tail)
                self.assertEqual(rc, 2)
                self.assertIn("usage:", err)

    def test_unsafe_goto_refuses_without_writing(self):  # noqa: VACUOUS_ASSERTION — rc1 and the scheme-specific refusal positively control the empty store
        rc, _out, err = self.run_note([
            "set", "bad", "click", "--goto", "javascript:alert(1)"])
        self.assertEqual(rc, 1)
        self.assertIn("http(s)", err)
        self.assertEqual(fleetnotes.rows(), [])

    def test_list_of_an_empty_store_says_so_at_rc_0(self):
        rc, out, _err = self.run_note(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("no fleet notes yet", out)

    def test_rm_of_an_absent_key_fails_LOUDLY(self):
        rc, _out, err = self.run_note(["rm", "never-there"])
        self.assertEqual(rc, 1)
        self.assertIn("no note under", err)

    def test_a_refused_set_exits_1_and_says_why(self):
        rc, _out, err = self.run_note(["set", "wall", "x" * (fleetnotes.MAX_TEXT + 1)])
        self.assertEqual(rc, 1)
        self.assertIn("the cap is", err)

    def test_unknown_subverb_refuses_with_exit_2_and_a_suggestion(self):
        rc, _out, err = self.run_note(["lst"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'lst'", err)
        self.assertIn("did you mean 'list'?", err)

    def test_an_unknown_verb_is_echoed_as_printable_ascii(self):
        """A refusal must not RUN the payload it is refusing: the unknown verb
        is unvalidated argv going straight to a terminal."""
        rc, _out, err = self.run_note(["\x1b[2Jls"])
        self.assertEqual(rc, 2)
        self.assertNotIn("\x1b", err)
        self.assertIn("\\x1b", err)

    def test_bare_and_help_forms(self):
        self.assertEqual(self.run_note([])[0], 2)          # bare = usage, refuse
        self.assertEqual(self.run_note(["--help"])[0], 0)  # asked for = rc 0


class WebNotesTest(unittest.TestCase):
    """GET /api/notes — the home tab's card, and the owner presence row the
    retired cave tab used to own."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-webnotes-")
        cls.prior = os.environ.get("HELM_FLEET_NOTES")
        os.environ["HELM_FLEET_NOTES"] = os.path.join(cls.tmp, "fleet-notes.json")
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        if cls.prior is None:
            os.environ.pop("HELM_FLEET_NOTES", None)
        else:
            os.environ["HELM_FLEET_NOTES"] = cls.prior
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        try:
            os.unlink(os.environ["HELM_FLEET_NOTES"])
        except OSError:
            pass
        web._COCKPIT_BEAT[0] = 0.0
        # The roster REPORT cache is process-wide while the server thread is
        # class-level, so a report the owner-graft mutated is served to the
        # NEXT test. The contract requires each site to redden ONLY its
        # named arm; without this, removing the copy-graft reddens the bracket
        # arm too — through the shared cache rather than through anything that
        # test claims.
        web._ROSTER_REP_CACHE.clear()

    # THE SECOND FACE OF THE SAME STARVATION, and it reaches EVERY test here,
    # not just the presence one. 10s against a loopback server is fine on an
    # idle box and is pure impatience on a box running several whole-suite
    # gates at once: the read blows and unittest reports an ERROR, which looks
    # nothing like the presence FAILURE and is the same cause. Nothing in this
    # file asserts on latency, so this bound only decides how long we wait
    # before calling a wedged server wedged — it can be generous without
    # weakening a single assertion. Teardown stays bounded (thread join, 5s).
    HTTP_TIMEOUT = 60

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=self.HTTP_TIMEOUT) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    # ── /api/notes ──────────────────────────────────────────────────────────

    def test_seeded_notes_ride_the_wire_newest_first(self):
        fleetnotes.set_note("reboot-recovery", "stable + reboot-proofed",
                            by="helm-claude-2", ts="2026-07-28T20:10:56Z")
        fleetnotes.set_note("signing-status", "signing LIVE fleet-wide",
                            by="helm-claude-2", ts="2026-07-29T00:49:25Z")
        status, d = self.get("/api/notes")
        self.assertEqual(status, 200)
        self.assertEqual([n["key"] for n in d["notes"]],
                         ["signing-status", "reboot-recovery"])
        self.assertEqual(d["notes"][1]["text"], "stable + reboot-proofed")
        self.assertEqual(d["notes"][1]["by"], "helm-claude-2")
        self.assertEqual(d["notes"][1]["ts"], "2026-07-28T20:10:56Z")
        self.assertEqual(d["cap"], fleetnotes.MAX_KEYS)

    def test_freshness_rides_the_REAL_WIRE_not_just_the_projection(self):
        """codex's P2: age_s and stale were pinned in the module and in the
        rendered card, but nothing asserted them on the actual HTTP payload —
        so a serializer that dropped or renamed either field would pass
        everything and reach the browser broken.

        Stamps are relative to the REAL clock because the server reads the real
        clock; a frozen constant here would date the test to expire."""
        import time
        now = time.time()
        stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(now - ago))
        fleetnotes.set_note("live-one", "just landed", ts=stamp(120))
        fleetnotes.set_note("week-old", "ancient", ts=stamp(86400 * 7))
        status, d = self.get("/api/notes")
        self.assertEqual(status, 200)
        wire = {n["key"]: n for n in d["notes"]}
        self.assertEqual(sorted(wire), ["live-one", "week-old"])
        for key in ("live-one", "week-old"):
            self.assertIn("age_s", wire[key], "%s lost age_s on the wire" % key)
            self.assertIn("stale", wire[key], "%s lost stale on the wire" % key)
        self.assertFalse(wire["live-one"]["stale"])
        self.assertTrue(wire["week-old"]["stale"])
        # the values are USABLE, not merely present: age_s is a real number of
        # seconds and agrees with the stale flag the browser will partition on
        self.assertIsInstance(wire["live-one"]["age_s"], int)
        self.assertLess(wire["live-one"]["age_s"], fleetnotes.FRESH_WINDOW_S)
        self.assertGreater(wire["week-old"]["age_s"], fleetnotes.FRESH_WINDOW_S)
        # AND JSON SURVIVES AN UNDATABLE NOTE, whose age_s is null — the one
        # value a naive serializer is most likely to mangle. Seeded by writing
        # the file DIRECTLY, because set_note cannot produce this shape: it
        # validates the stamp and reads an empty ts as "now". An undatable note
        # therefore only ever arrives from a hand-edited or legacy store, which
        # is exactly the case the browser must survive and the case no
        # writer-side test can reach.
        path = os.environ["HELM_FLEET_NOTES"]
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        raw["no-stamp"] = {"text": "undatable", "by": "legacy", "ts": ""}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        _s, d2 = self.get("/api/notes")
        undated = {n["key"]: n for n in d2["notes"]}["no-stamp"]
        self.assertIsNone(undated["age_s"], "an undatable age became a number")
        self.assertFalse(undated["stale"], "an undatable note was dimmed")

    def test_structured_note_rides_the_wire_with_compatibility_text(self):
        fleetnotes.set_note("action", "Land is ready",
                            detail="Open the exact gate", goto="ledger")
        status, d = self.get("/api/notes")
        self.assertEqual(status, 200)
        note = d["notes"][0]
        self.assertEqual(note["text"], "Land is ready")
        self.assertEqual(note["headline"], "Land is ready")
        self.assertEqual(note["detail"], "Open the exact gate")
        self.assertEqual(note["goto"], "ledger")

    def test_an_empty_store_answers_an_empty_list_at_200(self):
        status, d = self.get("/api/notes")
        self.assertEqual((status, d["notes"]), (200, []))
        self.assertNotIn("unavailable", d)

    def test_a_corrupt_store_answers_empty_at_200_not_a_500(self):
        with open(os.environ["HELM_FLEET_NOTES"], "w", encoding="utf-8") as f:
            f.write("{{{ torn")
        status, d = self.get("/api/notes")
        self.assertEqual((status, d["notes"]), (200, []))

    def test_a_raising_read_FAILS_OPEN_rather_than_500ing_the_home_tab(self):
        real = fleetnotes.rows
        fleetnotes.rows = lambda *a, **k: 1 / 0
        try:
            status, d = self.get("/api/notes")
        finally:
            fleetnotes.rows = real
        self.assertEqual(status, 200)
        self.assertEqual(d["notes"], [])
        self.assertTrue(d["unavailable"])

    # ── the owner's roster row ──────────────────────────────────────────────

    def test_no_cockpit_no_owner_row(self):
        status, d = self.get("/api/chat/roster")
        self.assertEqual(status, 200)
        self.assertEqual([s for s in d.get("seats", []) if s.get("owner")], [])

    def test_poll_stamp_is_bracketed(self):
        """TWO CLAIMS, SPLIT SO NEITHER RACES A CLOCK.

        The poll must STAMP the beat, and a fresh beat must RENDER as "fresh".
        Asserting the second THROUGH the first made this test race real wall
        clock: `presence` is derived from the stamp's AGE against a TEN-second
        window (web.py `"fresh" if age < 10 else "quiet"`) — not the 30s
        OWNER_PRESENCE_TTL, which only decides whether the row appears at all.
        Under PARALLEL whole-suite gates this box starves hard enough that ten
        seconds can pass between two localhost calls.

        Measured 2026-08-01 across four whole-suite runs — two faces, ONE
        budget: `AssertionError: 'quiet' != 'fresh'` when the age crossed 10s,
        and `TimeoutError` when the SAME ten seconds blew the client timeout in
        `get`. An ERROR and a FAILURE that look like two bugs and are one
        clock; the gate receipt reports only a count, so it cost five probes to
        tell apart. Cross-confirmed by kimi, who hit it twice the same night on
        unrelated lanes — it reddens whichever gate is unlucky, not whichever
        diff is wrong.

        The beat is zeroed FIRST so the stamp the assertion reads is provably
        the one this test's poll wrote: a value leaked from an earlier test
        would satisfy the freshness assertion while proving nothing about the
        poll. setUp already zeroes it; doing it here too makes the claim local
        and survives a future reordering. The wire path is still exercised end
        to end — the poll really is what stamps."""
        import time as _t
        web._COCKPIT_BEAT[0] = 0.0                  # the stamp below must be OURS
        before = _t.time()
        self.get("/api/chat?room=main&since=0")     # the poll EVERY open page runs
        after = _t.time()
        stamp = web._COCKPIT_BEAT[0]
        # BRACKET, not overwrite. My first cut re-stamped the beat with now(),
        # which made the render deterministic and ALSO papered over a stale
        # stamp: a poll writing now-15s would still have passed (a review on
        # b5c75deef94d). Bracketing proves the stamp is THIS poll's AND that it
        # is now-ish, without any assertion racing a clock.
        self.assertGreaterEqual(stamp, before,
                                "the poll must stamp NOW, not a stale clock")
        self.assertLessEqual(stamp, after,
                             "the stamp must not be from the future either")
        # FREEZE for the render. Injecting before the call was not enough: the
        # SECOND HTTP hop can itself take longer than the 10s window on a
        # starved box, so age crossed again after the injection (a probe
        # measured 10.2s). Frozen at the stamp, no elapsed wall time can decide
        # what this assertion sees.
        with mock.patch.object(web.time, "time", return_value=stamp):
            status, d = self.get("/api/chat/roster")
        self.assertEqual(status, 200)
        owner = [s for s in d["seats"] if s.get("owner")]
        self.assertEqual(len(owner), 1)
        self.assertEqual(owner[0]["presence"], "fresh")
        self.assertEqual(owner[0]["connection"], web.MP_OWNER_CONNECTION)
        self.assertTrue(owner[0]["seat"], "the owner's row must carry a name")
        self.assertIsNone(owner[0]["session"], "a person has no session to open")

    def test_fresh_quiet_boundary(self):
        """The other route: test the owner row DIRECTLY rather than
        through two network hops. The flake was never about the wire — it was
        the render crossing a TEN-second age boundary (not the 30s TTL, which
        only decides whether the row appears at all). Pinned here against a
        controlled clock, so the law holds even if every HTTP test in this file
        were deleted.

        Both sides of the boundary, because a one-sided test would pass with
        the comparison inverted."""
        import time as _t
        now = 1_800_000_000.0
        with mock.patch.object(web.time, "time", return_value=now):
            web._COCKPIT_BEAT[0] = now - 1          # comfortably inside
            self.assertEqual(web._owner_row()["presence"], "fresh")
            web._COCKPIT_BEAT[0] = now - 9.99       # just inside
            self.assertEqual(web._owner_row()["presence"], "fresh")
            web._COCKPIT_BEAT[0] = now - 10.01      # just outside: the flake
            self.assertEqual(web._owner_row()["presence"], "quiet")
        self.assertGreater(_t.time(), 0)            # the clock is unfrozen again
        # NO TTL ASSERTION HERE, deliberately. It lives in
        # test_owner_row_ttl_boundary, and duplicating it made a single TTL
        # mutation redden TWO arms — which is exactly the signal the
        # contract exists to keep clean: one site, one arm.

    def test_owner_row_ttl_boundary(self):
        """SITE 3 of the mutation contract: the TTL gate in _owner_row.

        Both sides, and the zero-beat arm alongside — mutating the gate to
        `age > TTL * 10` must redden here, and deleting it must too, which
        requires asserting the row is GONE past the TTL AND that a never-polled
        cockpit has no row at all. Pinning only the first would survive a
        deletion whenever the beat happened to be zero."""
        now = 1_800_000_000.0
        with mock.patch.object(web.time, "time", return_value=now):
            web._COCKPIT_BEAT[0] = now - (web.OWNER_PRESENCE_TTL - 1)
            self.assertIsNotNone(web._owner_row(),
                                 "inside the TTL the row is present")
            web._COCKPIT_BEAT[0] = now - (web.OWNER_PRESENCE_TTL + 1)
            self.assertIsNone(web._owner_row(),
                              "past the TTL the row is gone")
            web._COCKPIT_BEAT[0] = 0.0
            self.assertIsNone(web._owner_row(),
                              "a cockpit that never polled has no row, and "
                              "this arm is what a DELETED gate fails")

    def test_owner_graft_does_not_mutate_cached_roster(self):
        """SITE 4 of the mutation contract: the `rep = dict(rep)` copy in
        _api_chat_roster.

        The roster report is served from a SHARED TTL CACHE. Grafting the owner
        into that object instead of onto a copy mutates the cached report, so
        every later read inside the cache window carries one more owner row —
        the count grows without bound while the code looks correct.

        TWO reads, not five, and under a clock pinned at the beat: elapsed time
        is irrelevant to what this arm claims, and my earlier five-hop version
        raced the 30s TTL by accumulation until the row vanished entirely
        (a review on 6024baa2f2ad). The contract calls for the graft to be the
        only thing under test here."""
        self.get("/api/chat?room=main&since=0")
        with mock.patch.object(web.time, "time",
                               return_value=web._COCKPIT_BEAT[0]):
            for read in range(2):
                _status, d = self.get("/api/chat/roster")
                self.assertEqual(
                    len([s for s in d["seats"] if s.get("owner")]), 1,
                    "read %d: the graft must not accumulate in the cache"
                    % read)


if __name__ == "__main__":
    unittest.main()
