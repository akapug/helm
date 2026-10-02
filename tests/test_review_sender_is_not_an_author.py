#!/usr/bin/env python3
"""A seat that only SENT a row is not a lane author (task/3356).

THE SYMPTOM. The chain of lane clip-launder-3257 recorded the integrator as
one of its three authors, but the integrator wrote no code in it: it only sent
a row asking for a read, at a tip another seat had already put on the ledger,
and rebound that row to itself. Its source-clean hold was refused as LANE
AUTHOR. Another seat read READY-SELF-REVIEW on lanes where it only dispatched
the build and reviewed. Each case cost a second seat's read of a door.

THE RULE THESE ARMS PIN. A seat is a lane author only if the ledger records it
producing code in the lane:
  * every recipient of a BUILD row. Rebinding transfers the obligation but
    proves nothing about whether the first recipient wrote adopted code;
  * the patch author of a FIX that named a cure with --patch-tip;
  * the seat that FIRST brought, anywhere on the ledger, a tip a round of
    the chain reads (F3: tip provenance is ledger-wide), because that tip is
    new work and its first bringer is the one seat the ledger can name for
    it. A build's base is read only when its own verdict reviewed exactly it.
A seat that dispatched the build, relayed a tip already on the ledger, in
this chain or another, or rebound a row wrote nothing. A chain with NO build
row keeps today's answer (every sender is an author), because there the
sender is the only record of who built the lane. Git cannot answer this:
every commit is authored akapug.

Every surface that asks "is this seat a lane author" is driven here through
its own door: the `--source-clean` hold, READY-SELF-REVIEW, the non-author
rule, and the AUTHORS line a landed close prints. (The fresh-context verdict
door stopped asking it in task/3658: it judges the reading instance.) A
reviewer's patch its author agreed at exactly the patch tip carries the
chain on a reversible lane (PairAgreementTest, task/3561).
Every admitting arm has a refusing twin one fact apart, and an unreadable
chain still refuses.
"""
import calendar
import os
import time
import unittest
from unittest import mock

from helm import dispatches, eventledger, home, landreq, seats
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_source_clean_landed as _sc
from tests import test_verdict_fresh_context as _fresh
from tests.test_landreq import run as _lr_run

OTHER = _sc.OTHER                # a reader nobody asks to hold anything
DISPATCHER = "seat-dispatcher"   # sends the build row, writes nothing
BUILDER = "seat-builder"         # the build row's recipient
RELAY = "seat-relay"             # asks for reads of tips already made
PATCHER = "seat-patcher"         # a reviewer whose FIX names its own cure
LEGACY = "seat-legacy"           # the author of a chain with no build row
FIRST = "seat-first"             # a builder the build moved away from
REBINDER = "seat-rebinder"       # moves a row and does nothing else
CLEAN = "SOURCE-CLEAN: read clean; fab Ran 5 tests OK"


def setUpModule():
    """No dispatch row this module writes walks the host's process table."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


class AuthorBase(_sc.SourceCleanBase):
    """Chains written through the real doors, each row sent by a named seat."""

    def setUp(self):
        super().setUp()
        # THE JOIN IS MEMOISED ON THE LEDGER'S STAT, and an arm here installs
        # an unreadable fold: the next arm must never read that memo.
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        self.addCleanup(landreq._CHAIN_CONTRIB_MEMO.clear)
        self.addCleanup(landreq._LEDGER_FOLD_MEMO.clear)
        # These arms deliberately use many recipients rather than the common
        # fixture's seat-reader. Give each a real exact-session native runtime;
        # the inherited policy admits this family. An actor-name mock alone
        # cannot make a proven DOOR hold.
        for seat in (OTHER, DISPATCHER, BUILDER, RELAY, PATCHER, LEGACY,
                     FIRST, REBINDER):
            with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat}):
                seats.write_roster(
                    seat, session=self.session_for(seat),
                    runtime={"family": "claude", "agent_harness": "claude",
                             "backend": "native", "model": "claude-opus-5-5"},
                    presence_beat=False)

    def session_for(self, seat):
        return self.READER_SESSION if seat == _sc.READER else "review-author-" + seat

    def send(self, sender, recipient, tip, lane, kind="review",
             parent=None, **extra):
        """One row, recorded by `sender` through `dispatches.add`; `extra`
        rides to it (a `decline_patch`)."""
        if kind == "review" and parent is None:
            extra.setdefault("task", self.review_task["id"])
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": sender}):
            row, why = dispatches.add(
                recipient, lane, ref=tip, repo=self.repo, kind=kind,
                notify=False, new_work=parent is None,
                supersedes=parent["id"] if parent else None, force=True,
                _reason=True, **extra)
        self.assertIsNone(why, why)
        self.assertEqual((row["sender"], row["recipient"], row["kind"]),
                         (sender, recipient, kind),
                         "the fixture did not record the row it names")
        return row

    def send_after(self, prior, sender, recipient, tip, lane, kind="review",
                   **extra):
        """Send one second after every recorded event on `prior`.

        These arms prove a settled relay, not the deliberately conservative
        equal-second case pinned separately below."""
        row = self.state(prior["id"])
        stamps = [row.get("ts"), row.get("verdict_ts")]
        stamps += [hop.get("ts") for hop in row.get("retips") or ()
                   if isinstance(hop, dict)]
        stamp = max(value for value in stamps
                    if dispatches._valid_ts(value))
        shape = "%Y-%m-%dT%H:%M:%SZ"
        later = time.strftime(shape, time.gmtime(
            calendar.timegm(time.strptime(stamp, shape)) + 1))
        with mock.patch.object(dispatches.pk, "now_ts", return_value=later):
            return self.send(sender, recipient, tip, lane, kind=kind,
                             parent=prior, **extra)

    def built(self, lane):
        """(build, delivered): DISPATCHER sends BUILDER a build on trunk's
        first commit, and BUILDER sends its delivered tip `side` to OTHER."""
        build = self.send(DISPATCHER, BUILDER, self.a, lane, kind="build")
        delivered = self.send(BUILDER, OTHER, self.side, lane, parent=build)
        return build, delivered

    def rebind(self, row, to, actor):
        """`row` moved to `to` by `actor`, through the real verb."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": actor}):
            out, why = dispatches.rebind(row["id"], to, force=True,
                                         reason="the reader went dark",
                                         notify=False)
        self.assertIsNone(why, why)
        return out["new"]

    def try_hold(self, row, actor, tip=None):
        """(out, why) for a `--source-clean` hold by `actor`, through the
        door, and the ledger UNCHANGED when it refuses."""
        before = self.history()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(actor, None)), \
                mock.patch.object(home, "session_id",
                                  return_value=self.session_for(actor)):
            out, why = dispatches.mark_hold(row["id"], CLEAN,
                                            source_clean_tip=tip or self.side)
        if out is None:
            self.assertEqual(self.history(), before,
                             "a refused source-clean hold APPENDED")
            self.assertEqual(self.state(row["id"])["status"], "open")
        return out, why

    def held_by(self, row, actor, tip=None):
        """Assert the hold is ACCEPTED and stamped with `actor`."""
        out, why = self.try_hold(row, actor, tip)
        self.assertIsNone(why, why)
        self.assertEqual(out["status"], "held")
        state = self.state(row["id"])
        self.assertEqual(state["hold_actor"], actor)
        self.assertEqual(state["source_clean_tip"], tip or self.side)
        self.assertIn("hold_approval", state,
                      "the accepted hold needs exact-session authority")

    def refused_as_author(self, row, actor, tip=None):
        """Assert the hold is REFUSED as a LANE AUTHOR's, naming `actor`."""
        out, why = self.try_hold(row, actor, tip)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", why or "")
        self.assertIn(actor, why)
        return why

    def approved(self, row):
        """`row`'s recipient APPROVEs its tip -> the land request."""
        _v, why = self.mark_verdict(row["id"], row["tip"], "read clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(lr["state"], "READY",
                         "fixture premise: the row must be READY, or the arm "
                         "measures some other rung")
        return lr

    def cure(self, text="the reviewer's own cure"):
        """A commit off `side`, where a reviewer's --patch-tip cure lives."""
        self.git("checkout", "-q", "side")
        tip = self.commit(text, path="g")
        self.git("checkout", "-q", self.main)
        return tip

    def patched(self, row):
        """`row`'s recipient returns FIX naming a cure it committed."""
        cure = self.cure()
        _v, why = self.mark_verdict(row["id"], row["tip"],
                                    "the guard is inverted", polarity="fix",
                                    patch_tip=cure)
        self.assertIsNone(why, why)
        self.assertEqual(self.state(row["id"]).get("patch_author"),
                         row["recipient"],
                         "fixture premise: the ledger must RECORD the patch")
        return cure


class TheHoldDoorAsksWhoWroteCodeTest(AuthorBase):
    """`helm dispatch hold --source-clean`: the recipient must have written
    none of the lane, and sending a row is not writing it."""

    def test_a_seat_that_only_SENT_review_rows_holds_source_clean(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside held_by(): the hold is accepted, its status is HELD and the ledger stamps this holder and tip by value
        """RELAY sent a round at the tip BUILDER delivered, and is later sent
        the lane to read. It wrote nothing, so its clean read is independent.
        Before the cure the chain recorded RELAY as an author for sending."""
        _build, delivered = self.built("lane/relay-holds")
        relayed = self.send(RELAY, OTHER, self.side, "lane/relay-holds",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/relay-holds",
                          parent=relayed)
        self.held_by(asked, RELAY)

    def test_the_clip_launder_shape_a_relay_rebound_to_itself_holds(self):
        """THE SPECIMEN (lane clip-launder-3257): the relay sent a round at
        an existing tip, the reader it chose could not take it, and it rebound
        the round to itself. The row's sender and recipient are now the same
        seat, and that seat still wrote nothing."""
        _build, delivered = self.built("lane/clip-launder")
        relayed = self.send(RELAY, OTHER, self.side, "lane/clip-launder",
                            parent=delivered)
        mine = self.rebind(relayed, RELAY, actor=RELAY)
        self.assertEqual((mine["sender"], mine["recipient"]), (RELAY, RELAY),
                         "fixture premise: the rebound row is the relay's own")
        self.held_by(mine, RELAY)

    def test_the_DISPATCHER_of_the_build_row_holds_source_clean(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside held_by(): the hold is accepted, its status is HELD and the ledger stamps this holder and tip by value
        """Dispatching is not authoring. DISPATCHER sent the build row and is
        sent the builder's delivery to read."""
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/dispatcher-holds",
                          kind="build")
        asked = self.send(BUILDER, DISPATCHER, self.side,
                          "lane/dispatcher-holds", parent=build)
        self.held_by(asked, DISPATCHER)

    def test_a_seat_that_REBOUND_a_row_holds_source_clean(self):
        """The mover is recorded as `acted_by`; the sender stays the row's
        own. REBINDER moved the round to itself and wrote nothing."""
        _build, delivered = self.built("lane/rebinder-holds")
        moved = self.rebind(delivered, REBINDER, actor=REBINDER)
        self.assertEqual((moved["sender"], moved["recipient"]),
                         (BUILDER, REBINDER))
        self.assertEqual(self.state(moved["id"]).get("acted_by"), REBINDER,
                         "fixture premise: the ledger records who moved it")
        self.held_by(moved, REBINDER)

    def test_the_BUILDER_is_an_author_even_when_it_sent_nothing(self):  # noqa: VACUOUS_ASSERTION — refused_as_author() asserts the refusal by its words with the ledger unchanged, and the control hold on the same chain is asserted HELD and stamped by value in held_by()
        """THE BUILD ROW IS THE RECORD OF WHO BUILT. Here the dispatcher sends
        the delivered tip on, so BUILDER appears nowhere as a sender, and
        before the cure its clean read of its own lane was accepted."""
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/builder-silent",
                          kind="build")
        relayed = self.send(DISPATCHER, OTHER, self.side,
                            "lane/builder-silent", parent=build)
        asked = self.send(RELAY, BUILDER, self.side, "lane/builder-silent",
                          parent=relayed)
        self.refused_as_author(asked, BUILDER)
        # CONTROL, one fact apart on the same chain: the relay is sent the
        # same tip and holds it.
        other = self.send(DISPATCHER, RELAY, self.side, "lane/builder-silent",
                          parent=relayed)
        self.held_by(other, RELAY)

    def test_a_PATCH_AUTHOR_is_refused(self):  # noqa: VACUOUS_ASSERTION — refused_as_author() asserts the refusal by its words with the ledger unchanged, and the control hold on the same chain is asserted HELD and stamped by value in held_by()
        """A reviewer whose FIX named a cure it committed wrote code."""
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/patcher",
                          kind="build")
        first = self.send(BUILDER, PATCHER, self.side, "lane/patcher",
                          parent=build)
        cure = self.patched(first)
        asked = self.send_after(first, RELAY, PATCHER, cure,
                                "lane/patcher")
        self.refused_as_author(asked, PATCHER, tip=cure)
        # CONTROL: the relay that sent the cure on holds the same tip.
        other = self.send_after(asked, PATCHER, RELAY, cure,
                                "lane/patcher")
        self.held_by(other, RELAY, tip=cure)

    def test_a_round_that_brings_a_NEW_tip_makes_its_sender_an_author(self):  # noqa: VACUOUS_ASSERTION — the claim is asserted inside refused_as_author(): the refusal names LANE AUTHOR and the seat, and try_hold() asserts the ledger unchanged and the row still open
        """A round at a tip no earlier round carried is new work, and its
        sender is the one seat the ledger can name for it. RELAY sends a tip
        of its own after the delivery, so RELAY wrote code here."""
        _build, delivered = self.built("lane/relay-new-tip")
        cure = self.cure("the relay's own rework")
        reworked = self.send(RELAY, OTHER, cure, "lane/relay-new-tip",
                             parent=delivered)
        asked = self.send(BUILDER, RELAY, cure, "lane/relay-new-tip",
                          parent=reworked)
        self.refused_as_author(asked, RELAY, tip=cure)

    def test_a_LEGACY_chain_with_no_build_row_keeps_every_sender_an_author(self):  # noqa: VACUOUS_ASSERTION — refused_as_author() asserts the refusal by its words with the ledger unchanged, and the control hold on the same chain is asserted HELD and stamped by value in held_by()
        """No build row names a builder, so the sender is the only record of
        who built the lane: today's answer stands, and the relay stays an
        author there."""
        first = self.send(LEGACY, OTHER, self.side, "lane/legacy")
        relayed = self.send(RELAY, OTHER, self.side, "lane/legacy",
                            parent=first)
        asked = self.send(LEGACY, RELAY, self.side, "lane/legacy",
                          parent=relayed)
        self.refused_as_author(asked, RELAY)
        # CONTROL, one fact apart: the same three rounds rooted in a build
        # row, and the relay holds.
        _build, delivered = self.built("lane/legacy-control")
        relayed = self.send(RELAY, OTHER, self.side, "lane/legacy-control",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/legacy-control",
                          parent=relayed)
        self.held_by(asked, RELAY)

    def test_a_build_that_MOVED_keeps_both_recipients_as_authors(self):
        """A rebind proves the obligation moved, not that FIRST wrote nothing.
        The safe door keeps both possible builders; an extra read costs less
        than admitting an author's read as independent."""
        build = self.send(DISPATCHER, FIRST, self.a, "lane/moved-build",
                          kind="build")
        moved = self.rebind(build, BUILDER, actor=DISPATCHER)
        self.assertEqual((moved["kind"], moved["tip"]), ("build", self.a))
        delivered = self.send(BUILDER, OTHER, self.side, "lane/moved-build",
                              parent=moved)
        asked = self.send(RELAY, FIRST, self.side, "lane/moved-build",
                          parent=delivered)
        self.refused_as_author(asked, FIRST)
        # CONTROL: the replacement builder is an author too.
        other = self.send(RELAY, BUILDER, self.side, "lane/moved-build",
                          parent=delivered)
        self.refused_as_author(other, BUILDER)

    def test_a_moved_builder_who_relayed_the_tip_cannot_hold_its_own_read(self):
        """THE REGRESSION. FIRST may write before a forced rebind and BUILDER
        may adopt that work. FIRST then relays the final tip and the row is
        rebound to it for a read. The prior blanket-sender rule refused the
        hold; erasing FIRST on the build move admitted it without no-write
        evidence."""
        build = self.send(DISPATCHER, FIRST, self.a, "lane/moved-relay",
                          kind="build")
        moved = self.rebind(build, BUILDER, actor=DISPATCHER)
        delivered = self.send(BUILDER, OTHER, self.side, "lane/moved-relay",
                              parent=moved)
        relayed = self.send(FIRST, OTHER, self.side, "lane/moved-relay",
                            parent=delivered)
        mine = self.rebind(relayed, FIRST, actor=FIRST)
        self.assertEqual((mine["sender"], mine["recipient"]), (FIRST, FIRST))
        self.refused_as_author(mine, FIRST)

    def test_an_UNREADABLE_chain_refuses_the_hold_of_a_seat_that_wrote_nothing(self):
        """FAIL CLOSED. The relay's hold is accepted on this very shape; with
        the build row's identity damaged the chain cannot be read, and an
        unreadable chain is never a chain nobody wrote."""
        build, delivered = self.built("lane/unreadable")
        relayed = self.send(RELAY, OTHER, self.side, "lane/unreadable",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/unreadable",
                          parent=relayed)
        # The rewrite recipe the typed-refusal arm uses, reused rather than
        # copied: one hand-edit of the ledger, one place.
        _sc.TheHoldDoorRefusesALaneAuthorTest.rewrite_founding(
            self, build["id"], chain_root="not-a-hex-id")
        self.assertEqual(self.state(build["id"])["chain_root"],
                         dispatches.CHAIN_UNKNOWN)
        out, why = self.try_hold(asked, RELAY)
        self.assertIsNone(out)
        self.assertIn("could not be read", why or "")
        self.assertNotIn("wrote none of this lane", why)

    def test_an_UNREADABLE_ledger_refuses_the_close_rung_for_a_non_author(self):
        """The read side (`helm train`, the close ladder's holder rung) reads
        the memoised join. With the ledger unreadable it refuses the relay's
        stamped hold as UNREADABLE, where a readable ledger admits it."""
        _build, delivered = self.built("lane/unreadable-ledger")
        relayed = self.send(RELAY, OTHER, self.side, "lane/unreadable-ledger",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/unreadable-ledger",
                          parent=relayed)
        held = self.planted(asked, self.side, actor=RELAY)
        self.assertIsNone(landreq.source_clean_holder_error(held),
                          "the control: a readable ledger admits the relay")
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        with mock.patch.object(landreq, "_ledger_fold", return_value=(
                None, {}, "dispatch ledger unreadable: planted")):
            why = landreq.source_clean_holder_error(held)
        self.assertEqual(getattr(why, "kind", None),
                         landreq.SourceCleanRefusal.UNREADABLE, why)
        self.assertIn("could not be read", why)

    def test_a_MALFORMED_complete_row_is_unreadable_not_absent(self):
        """A first-bringer record silently skipped by a lenient fold can make
        its later relay look like the writer. The authorship join therefore
        reads the whole ledger strictly and refuses on any malformed complete
        row, even one outside this chain."""
        _build, delivered = self.built("lane/malformed-ledger")
        relayed = self.send(RELAY, OTHER, self.side, "lane/malformed-ledger",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/malformed-ledger",
                          parent=relayed)
        held = self.planted(asked, self.side, actor=RELAY)
        self.assertIsNone(landreq.source_clean_holder_error(held))
        with open(dispatches.ledger_path(), "ab") as f:
            f.write(b"{not-json}\n")
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        why = landreq.source_clean_holder_error(held)
        self.assertEqual(getattr(why, "kind", None),
                         landreq.SourceCleanRefusal.UNREADABLE, why)
        self.assertIn("corrupt", why.lower())

    def test_an_UNOPENABLE_complete_row_is_unreadable_not_absent(self):
        """Strict JSON alone is insufficient: a complete orphan transition has
        an id but folds to no row, so its possible provenance cannot disappear
        into an absence claim."""
        _build, delivered = self.built("lane/unopenable-ledger")
        asked = self.send(BUILDER, RELAY, self.side,
                          "lane/unopenable-ledger", parent=delivered)
        held = self.planted(asked, self.side, actor=RELAY)
        self.assertIsNone(landreq.source_clean_holder_error(held))
        eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": 1, "id": "f" * 32,
            "ts": "2026-01-01T00:00:00Z", "reviewed_tip": self.side,
            "polarity": "approve", "verdict_ref": "orphan"})
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        why = landreq.source_clean_holder_error(held)
        self.assertEqual(getattr(why, "kind", None),
                         landreq.SourceCleanRefusal.UNREADABLE, why)
        self.assertIn("no openable dispatch", why.lower())


class ReadyAsksWhoWroteCodeTest(AuthorBase):
    """READY-SELF-REVIEW, the non-author rule and the fresh-context bound (c)
    read the same writers the hold door reads."""

    def test_the_RELAYs_approve_of_a_build_chain_is_independent(self):  # noqa: VACUOUS_ASSERTION — independence is asserted True by identity on the rung's own predicate, and the READY state is asserted by value in approved()
        _build, delivered = self.built("lane/relay-ready")
        relayed = self.send(RELAY, OTHER, self.side, "lane/relay-ready",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/relay-ready",
                          parent=relayed)
        lr = self.approved(asked)
        independent, why = landreq.independent_review(lr)
        self.assertIs(independent, True, why)
        self.assertNotEqual(landreq.ready_rung(lr), "SELF-REVIEW")

    def test_the_DISPATCHERs_approve_is_independent(self):
        """A seat dispatched the build and then reviewed the delivery: the
        READY-SELF-REVIEW shape measured on the board."""
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/dispatcher-ready",
                          kind="build")
        asked = self.send(BUILDER, DISPATCHER, self.side,
                          "lane/dispatcher-ready", parent=build)
        lr = self.approved(asked)
        independent, why = landreq.independent_review(lr)
        self.assertIs(independent, True, why)
        self.assertIn("independent — %s" % DISPATCHER, why)

    def test_the_BUILDERs_approve_is_SELF_REVIEW_even_when_it_sent_nothing(self):
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/builder-ready",
                          kind="build")
        relayed = self.send(DISPATCHER, OTHER, self.side, "lane/builder-ready",
                            parent=build)
        asked = self.send(DISPATCHER, BUILDER, self.side,
                          "lane/builder-ready", parent=relayed)
        lr = self.approved(asked)
        self.assertEqual(landreq.ready_rung(lr), "SELF-REVIEW")
        independent, why = landreq.independent_review(lr)
        self.assertIs(independent, False)
        self.assertIn("contributor approval present (%s)" % BUILDER, why)

    def test_a_PATCH_AUTHORs_approve_is_SELF_REVIEW(self):
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/patcher-ready",
                          kind="build")
        first = self.send(BUILDER, PATCHER, self.side, "lane/patcher-ready",
                          parent=build)
        cure = self.patched(first)
        asked = self.send(RELAY, PATCHER, cure, "lane/patcher-ready",
                          parent=first)
        lr = self.approved(asked)
        self.assertEqual(landreq.ready_rung(lr), "SELF-REVIEW")
        self.assertIn("contributor approval present (%s)" % PATCHER,
                      landreq.independent_review(lr)[1])

    def test_a_LEGACY_relays_approve_stays_SELF_REVIEW(self):
        first = self.send(LEGACY, OTHER, self.side, "lane/legacy-ready")
        relayed = self.send(RELAY, OTHER, self.side, "lane/legacy-ready",
                            parent=first)
        asked = self.send(LEGACY, RELAY, self.side, "lane/legacy-ready",
                          parent=relayed)
        lr = self.approved(asked)
        self.assertEqual(landreq.ready_rung(lr), "SELF-REVIEW")

    def test_an_UNREADABLE_ledger_answers_UNKNOWN_never_independent(self):
        _build, delivered = self.built("lane/unreadable-ready")
        relayed = self.send(RELAY, OTHER, self.side, "lane/unreadable-ready",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/unreadable-ready",
                          parent=relayed)
        lr = self.approved(asked)
        landreq._CHAIN_CONTRIB_MEMO.clear()
        landreq._LEDGER_FOLD_MEMO.clear()
        with mock.patch.object(landreq, "_ledger_fold", return_value=(
                None, {}, "dispatch ledger unreadable: planted")):
            independent, why = landreq.independent_review(lr)
        self.assertIsNone(independent)
        self.assertIn("UNKNOWN", why)

    def test_the_NON_AUTHOR_rule_reads_the_same_writers(self):
        """`non_author_error`, the rule derived APPROVED and the verdict door
        read. Its tier half is not this lane's question, so it is held open;
        its authorship half must admit the relay and refuse the builder."""
        _build, delivered = self.built("lane/non-author")
        relayed = self.send(RELAY, OTHER, self.side, "lane/non-author",
                            parent=delivered)
        asked = self.send(BUILDER, RELAY, self.side, "lane/non-author",
                          parent=relayed)
        self.approved(asked)
        builder = self.send(RELAY, BUILDER, self.side, "lane/non-author",
                            parent=asked)
        self.approved(builder)
        with mock.patch.object(dispatches, "non_author_tier_error",
                               return_value=None):
            relay_why = landreq.non_author_error(self.state(asked["id"]),
                                                 (self.state(asked["id"]),))
            builder_why = landreq.non_author_error(
                self.state(builder["id"]), (self.state(builder["id"]),))
        self.assertIsNone(relay_why, relay_why)
        self.assertIn("@%s wrote this work" % BUILDER, builder_why or "")


class PairAgreementTest(AuthorBase):
    """A REVIEWER'S PATCH ITS AUTHOR AGREED LANDS THE CHAIN (task/3561, store
    premise pair-agreement-lands-a-mechanical-patch-a-door-patch-owes-a-re-
    read). BUILDER wrote the lane; PATCHER, sent it to read, returned FIX
    naming a cure it committed, and handed that cure to BUILDER. On a
    REVERSIBLE lane two shapes carry it through the gate with no third
    reader, each at EXACTLY the patch tip:
      * BUILDER holds the patch row source-clean: its hold is its agreeing
        read of the patch, and PATCHER's FIX read everything under it;
      * BUILDER records an agreeing verdict and sends the tip back, and
        PATCHER holds it.
    One fact apart each still refuses: a patch its author disagreed with, a
    hold one commit past the agreed tip, and a DOOR lane, which owes one
    re-read by a fresh-context reader that wrote none of it. No shape mints
    an APPROVE, and a lane author's approve of the pair tip still reads
    READY-SELF-REVIEW."""

    def door_cure(self):
        """A cure off `side` that adds a money door."""
        self.git("checkout", "-q", "side")
        path = os.path.join(self.repo, "helm", "ledger_pay.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("PROVIDER = 'stripe'\n")
        self.git("add", "helm/ledger_pay.py")
        self.git("commit", "-q", "-m", "the reviewer's cure, at a door")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        return tip

    def pair(self, lane, door=False):
        """(cure, back): BUILDER's lane read by PATCHER, PATCHER's FIX naming
        its cure, and PATCHER's row handing that cure to BUILDER."""
        build = self.send(DISPATCHER, BUILDER, self.a, lane, kind="build")
        first = self.send(BUILDER, PATCHER, self.side, lane, parent=build)
        if door:
            cure = self.door_cure()
            _v, why = self.mark_verdict(first["id"], first["tip"],
                                        "the guard is inverted",
                                        polarity="fix", patch_tip=cure)
            self.assertIsNone(why, why)
        else:
            cure = self.patched(first)
        return cure, self.send_after(first, PATCHER, BUILDER, cure, lane)

    def rides(self, row, tip):
        """The close's holder rung, the billing surface and the train's car
        predicate each admit the held row at `tip`, and it minted no
        APPROVE."""
        state = self.state(row["id"])
        self.assertIsNone(landreq.source_clean_holder_error(state))
        self.assertIsNone(landreq.source_clean_rehold(state))
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(landreq.source_clean_car(lr), (tip, None))
        self.assertIsNone(state.get("polarity"))

    def test_ALPHA_the_author_holds_the_reviewers_patch_and_it_rides(self):  # noqa: VACUOUS_ASSERTION — held_by() asserts the hold HELD and stamped by value; rides() asserts each predicate's admission by value
        cure, back = self.pair("lane/pair-alpha")
        self.held_by(back, BUILDER, tip=cure)
        self.rides(back, cure)

    def test_BETA_the_patcher_holds_once_the_author_agreed_at_the_patch_tip(self):  # noqa: VACUOUS_ASSERTION — held_by() asserts the hold HELD and stamped by value; rides() asserts each predicate's admission by value
        cure, back = self.pair("lane/pair-beta")
        _v, why = self.mark_verdict(back["id"], cure, "the patch is right",
                                    polarity="concur")
        self.assertIsNone(why, why)
        again = self.send_after(back, BUILDER, PATCHER, cure,
                                "lane/pair-beta")
        self.held_by(again, PATCHER, tip=cure)
        self.rides(again, cure)

    def test_a_patch_its_author_DISAGREED_with_still_refuses_its_patcher(self):  # noqa: VACUOUS_ASSERTION — refused_as_author() asserts the refusal by its words with the ledger unchanged
        cure, back = self.pair("lane/pair-no")
        _v, why = self.mark_verdict(back["id"], cure, "the patch is wrong",
                                    polarity="fix",
                                    no_patch_because="design: a meld owns it")
        self.assertIsNone(why, why)
        again = self.send_after(back, BUILDER, PATCHER, cure,
                                "lane/pair-no")
        self.refused_as_author(again, PATCHER, tip=cure)

    def test_a_hold_one_commit_PAST_the_agreed_tip_refuses(self):  # noqa: VACUOUS_ASSERTION — refused_as_author() asserts the refusal by its words with the ledger unchanged; the control hold at the agreed tip is asserted HELD by value
        cure, back = self.pair("lane/pair-past")
        _v, why = self.mark_verdict(back["id"], cure, "the patch is right",
                                    polarity="concur")
        self.assertIsNone(why, why)
        again = self.send_after(back, BUILDER, PATCHER, cure,
                                "lane/pair-past")
        past = self.cure("the patcher's next commit, nobody's agreement")
        self.refused_as_author(again, PATCHER, tip=past)
        self.held_by(again, PATCHER, tip=cure)

    def test_the_authors_APPROVE_of_the_pair_tip_is_still_SELF_REVIEW(self):  # noqa: VACUOUS_ASSERTION — the state word and the rung are asserted EQUAL to exact values
        """A lane author never approves its own work: the pair's agreement is
        a hold, and an approve by BUILDER at the patch tip carries nothing."""
        cure, back = self.pair("lane/pair-approve")
        _v, why = self.mark_verdict(back["id"], cure, "read clean",
                                    polarity="approve")
        self.assertIsNone(why, why)
        lr, why = landreq.get(back["id"])
        self.assertIsNone(why, why)
        self.assertEqual((lr["state"], landreq.ready_rung(lr)),
                         ("READY", "SELF-REVIEW"))

    def test_on_a_DOOR_lane_the_pair_owes_one_fresh_reread(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted to name the door and the re-read, with the ledger unchanged; the hold after the fresh read is asserted HELD and stamped by value
        cure, back = self.pair("lane/pair-door", door=True)
        why = self.refused_as_author(back, BUILDER, tip=cure)
        self.assertIn("DOOR (money", why)
        self.assertIn("--reviewer-run", why)
        # THE ONE RE-READ: a fresh subagent BUILDER spawned reads the tip.
        from helm import runrecord
        root = os.path.join(self.tmp, "claude-projects")
        os.makedirs(root)
        with mock.patch.object(runrecord, "roots", return_value=[root]):
            _fresh.plant_agent(root, _fresh.READER_SESSION, tip=cure)
            with mock.patch.object(dispatches, "_acting_author",
                                   return_value=(BUILDER, None)):
                _out, why = dispatches.mark_verdict(
                    back["id"], cure, "read clean", polarity="concur",
                    basis="measured", bind_author=True,
                    reviewer_model="opus", reviewer_run=_fresh.AGENT,
                    author_model=_fresh.OPUS)
        self.assertIsNone(why, why)
        self.held_by(back, BUILDER, tip=cure)
        self.rides(back, cure)

    def below(self, lane, cure):
        """PATCHER's FIX on BUILDER's `lane` names `cure`, and PATCHER sends
        the lane back at the REVIEWED tip, below its cure. The send door
        refuses a round that does not carry a FIX's cure unless it declines
        that cure by name, so the decline is the one way to name the lower
        tip -> the row sent back."""
        build = self.send(DISPATCHER, BUILDER, self.a, lane, kind="build")
        first = self.send(BUILDER, PATCHER, self.side, lane, parent=build)
        _v, why = self.mark_verdict(first["id"], first["tip"],
                                    "the guard is inverted", polarity="fix",
                                    patch_tip=cure)
        self.assertIsNone(why, why)
        back = self.send_after(
            first, PATCHER, BUILDER, self.side, lane,
            decline_patch="%s=the reviewed tip is read first" % cure[:12])
        self.assertEqual(back["tip"], self.side,
                         "fixture premise: the row names the reviewed tip")
        return back

    def test_the_doors_are_read_at_the_HELD_tip_not_the_rows_own(self):  # noqa: VACUOUS_ASSERTION — the control hold is asserted HELD and stamped by value; refused_as_author() asserts the refusal by its words with the ledger unchanged
        """A hold may name a DESCENDANT of the row's dispatched ref, and the
        cure between the two can make the lane a door. The row names the
        reviewed tip, which carries no door, and the hold names the cure,
        which adds the money door: the doors that decide the pair are the
        held tip's. The control, one fact apart: the same shape at a
        reversible cure holds."""
        cure = self.cure()
        self.held_by(self.below("lane/pair-past-ok", cure), BUILDER, tip=cure)
        door = self.door_cure()
        why = self.refused_as_author(self.below("lane/pair-past-door", door),
                                     BUILDER, tip=door)
        self.assertIn("DOOR (money", why)

    def backfill(self, row):
        """(out, why) from the holder backfill's writer for `row`'s standing
        pre-stamp hold, on evidence of that writer's shape inside the hold's
        window."""
        state = self.state(row["id"])
        tool = dispatches.instant_epoch(state["hold_ts"]) - 3
        return dispatches.record_hold_actor_backfill(
            row["id"], state["hold_seq"],
            {"transcript": "/fixture/t.jsonl", "line": 7,
             "tool_use_id": "tool_P1", "command_sha256": "ab" * 32,
             "tool_ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                      time.gmtime(tool))})

    def test_the_holder_BACKFILL_reads_the_doors_too(self):  # noqa: VACUOUS_ASSERTION — the reversible backfill is asserted written and stamped by value; the door refusal is asserted by its words with the ledger unchanged
        """The holder a pre-stamp hold never recorded is written by a second
        writer, `record_hold_actor_backfill`, which asks the same author rung
        as the hold door. The replay reads only the ledger half of a pair, so
        that writer reads the doors too: a pre-stamp hold on a reversible
        pair is written, and the same hold on a door lane's pair is refused
        as the hold door refuses it."""
        cure, back = self.pair("lane/pair-bf-ok")
        self.planted(back, cure)
        out, why = self.backfill(back)
        self.assertIsNone(why, why)
        self.assertEqual(out["hold_actor"], BUILDER)
        door, back = self.pair("lane/pair-bf-door", door=True)
        self.planted(back, door)
        before = self.history()
        out, why = self.backfill(back)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", why or "")
        self.assertIn("DOOR (money", why or "")
        self.assertEqual(self.history(), before)
        self.assertNotIn("hold_actor", self.state(back["id"]))


class TipProvenanceIsLedgerWideTest(AuthorBase):
    """F3 (meta-claude's final read of this lane, verdict 87d1f541b90e): a
    tip's writer is the seat whose row FIRST brought it anywhere on the
    ledger, and that seat is named in every chain that reads the tip.

    THE SPECIMEN, lane pi-proxy-key-env: ds4pro was sent the build, then sent
    its own r2..r6 revisions as BUILD rows to a reviewer, one new chain each,
    and the reviewer's FIX read exactly each revision. A build row names only
    its recipient, so ds4pro was named in none of those chains and its APPROVE
    there would have counted as independent."""

    def own_tips(self):
        """(r1, r2): two revisions BUILDER commits after its build, made
        before either is sent, so r1 is no branch's tip when it is sent (no
        `ref_branch`, like the specimen's rows) and r2 is `side`'s."""
        return self.cure("r1: the builder's own revision"), \
            self.cure("r2: the builder's next revision")

    def chain_writers_of(self, row):
        wrote, why = landreq.chain_authors(self.state(row["id"]))
        self.assertIsNone(why, why)
        return set(wrote)

    def test_a_builder_that_sends_its_own_tips_as_builds_writes_every_chain(self):  # noqa: VACUOUS_ASSERTION — the absent ref_branch is a fixture premise; the claim is the refused hold, the control hold stamped by value and the SELF-REVIEW rung asserted by value
        """THE SPECIMEN: BUILDER sends r1 and r2 as build rows into two new
        chains, and the reviewer's FIX reads exactly each one. BUILDER is an
        author in both: its hold of r1 is refused and its APPROVE of r2 is
        SELF-REVIEW. Before the cure both chains named only the reviewer."""
        self.send(DISPATCHER, BUILDER, self.a, "lane/specimen", kind="build")
        r1, r2 = self.own_tips()
        sent = []
        for tip in (r1, r2):
            row = self.send(BUILDER, OTHER, tip, "lane/specimen", kind="build")
            _v, why = self.mark_verdict(row["id"], tip, "two findings",
                                        polarity="fix")
            self.assertIsNone(why, why)
            sent.append(row)
        keys = {landreq.chain_key(self.state(row["id"])) for row in sent}
        self.assertEqual(len(keys), 2, "fixture premise: each send is a chain")
        self.assertIsNone(self.state(sent[0]["id"]).get("ref_branch"),
                          "fixture premise: r1 is sent bound to no branch")
        asked = self.send(OTHER, BUILDER, r1, "lane/specimen", parent=sent[0])
        self.refused_as_author(asked, BUILDER, tip=r1)
        # CONTROL, one fact apart on the same chain: a seat that wrote nothing
        # is sent the same tip and holds it.
        other = self.send(OTHER, RELAY, r1, "lane/specimen", parent=sent[0])
        self.held_by(other, RELAY, tip=r1)
        mine = self.send(OTHER, BUILDER, r2, "lane/specimen", parent=sent[1])
        lr = self.approved(mine)
        self.assertEqual(landreq.ready_rung(lr), "SELF-REVIEW")
        independent, why = landreq.independent_review(lr)
        self.assertIs(independent, False)
        self.assertIn("contributor approval present (%s)" % BUILDER, why)

    def test_a_tip_carried_into_another_chain_names_its_first_bringer(self):
        """BUILDER brings `side` in chain X; RELAY carries it into chain Y,
        whose build went to FIRST. `side` is BUILDER's work under review in
        Y, so BUILDER is an author there, and RELAY, which only carried it,
        is not. Before the cure the rule judged a new tip against the round's
        own chain only: RELAY was Y's author and BUILDER was not."""
        self.built("lane/x")
        build = self.send(DISPATCHER, FIRST, self.a, "lane/y", kind="build")
        relayed = self.send(RELAY, OTHER, self.side, "lane/y", parent=build)
        asked = self.send(FIRST, RELAY, self.side, "lane/y", parent=relayed)
        self.held_by(asked, RELAY)
        builder = self.send(RELAY, BUILDER, self.side, "lane/y",
                            parent=relayed)
        self.refused_as_author(builder, BUILDER)
        self.assertEqual(self.chain_writers_of(builder), {FIRST, BUILDER})

    def test_a_TRUNK_tip_used_as_a_build_ref_names_nobody(self):  # noqa: VACUOUS_ASSERTION — the unbound twin's absent ref_branch is a fixture premise beside the trunk leg's ref_branch asserted by value; both chains' writer sets are asserted by value
        """A build dispatched from trunk, whose recipient read the base
        itself (the research-build shape), names only its recipient: the
        write door bound the base to trunk, and trunk is nobody's new work.
        TWIN, one fact apart: the same shape on a base the door bound to no
        branch. The ledger cannot tell that base from the dispatcher's own
        commit once a seat read it as the work, so the dispatcher is named:
        the conservative side. (Unbound and never read, it names nobody:
        test_the_DISPATCHER_of_the_build_row_holds_source_clean.)"""
        trunk = self.send(DISPATCHER, BUILDER, self.main, "lane/trunk-base",
                          kind="build")
        self.assertEqual((trunk["tip"], self.state(trunk["id"])["ref_branch"]),
                         (self.c, "refs/heads/" + self.main),
                         "fixture premise: the base is bound to trunk")
        unbound = self.send(DISPATCHER, BUILDER, self.a, "lane/unbound-base",
                            kind="build")
        self.assertIsNone(self.state(unbound["id"]).get("ref_branch"))
        for row in (trunk, unbound):
            _v, why = self.mark_verdict(row["id"], row["tip"],
                                        "nothing to change", polarity="fix")
            self.assertIsNone(why, why)
        self.assertEqual(self.chain_writers_of(trunk), {BUILDER})
        self.assertEqual(self.chain_writers_of(unbound), {BUILDER, DISPATCHER})

    def test_a_later_ancestor_retip_cannot_erase_the_first_sender(self):
        """A descendant send brings `side` first. Its build ancestor retips to
        `side` later, after the descendant closes. The ancestor's final folded
        tips must not erase the earlier candidate before ordering."""
        build_id, sent_id = "1" * 32, "2" * 32
        rows = {
            build_id: {
                "id": build_id, "chain_root": build_id, "kind": "build",
                "tip": self.side, "ref": self.a,
                "sender": DISPATCHER, "recipient": FIRST,
                "ts": "2026-01-01T00:00:00Z",
                "retips": ({"old_tip": self.a, "tip": self.side,
                             "ts": "2026-01-01T00:00:03Z"},),
            },
            sent_id: {
                "id": sent_id, "chain_root": build_id,
                "supersedes": build_id, "kind": "review",
                "tip": self.side, "ref": self.side,
                "sender": BUILDER, "recipient": OTHER,
                "ts": "2026-01-01T00:00:02Z",
            },
        }
        self.assertEqual(landreq.tip_writers(rows)[self.side],
                         (frozenset({BUILDER}), True))

    def test_a_later_ancestor_FIX_cannot_erase_the_first_sender(self):
        """A folded ancestor's final patch tip may also postdate its child."""
        first_id, sent_id = "1" * 32, "2" * 32
        rows = {
            first_id: {
                "id": first_id, "chain_root": first_id, "kind": "review",
                "tip": self.side, "ref": self.side,
                "sender": BUILDER, "recipient": PATCHER,
                "ts": "2026-01-01T00:00:00Z",
                "reviewed_tip": self.side, "patch_tip": self.c,
                "patch_author": PATCHER,
                "verdict_ts": "2026-01-01T00:00:03Z",
            },
            sent_id: {
                "id": sent_id, "chain_root": first_id,
                "supersedes": first_id, "kind": "review",
                "tip": self.c, "ref": self.c,
                "sender": FIRST, "recipient": OTHER,
                "ts": "2026-01-01T00:00:02Z",
            },
        }
        self.assertEqual(landreq.tip_writers(rows)[self.c],
                         (frozenset({FIRST}), True))

    def test_an_equal_timestamp_ancestor_retip_keeps_both_candidates(self):
        """The same shape at second precision cannot say whether the retip or
        descendant send came first. Neither candidate may disappear."""
        build_id, sent_id = "1" * 32, "2" * 32
        rows = {
            build_id: {
                "id": build_id, "chain_root": build_id, "kind": "build",
                "tip": self.side, "ref": self.a,
                "sender": DISPATCHER, "recipient": FIRST,
                "ts": "2026-01-01T00:00:00Z",
                "retips": ({"old_tip": self.a, "tip": self.side,
                             "ts": "2026-01-01T00:00:02Z"},),
            },
            sent_id: {
                "id": sent_id, "chain_root": build_id,
                "supersedes": build_id, "kind": "review",
                "tip": self.side, "ref": self.side,
                "sender": BUILDER, "recipient": OTHER,
                "ts": "2026-01-01T00:00:02Z",
            },
        }
        self.assertEqual(landreq.tip_writers(rows)[self.side],
                         (frozenset({FIRST, BUILDER}), False))

    def test_fold_order_beats_a_rolled_back_late_timestamp(self):
        """A send at position 0 happened before any later event on the row
        first created at position 1. That append order remains proof when the
        later event's otherwise-valid wall clock moved backwards."""
        found = [
            ("2026-01-01T00:00:02Z", 0, False, FIRST),
            ("2026-01-01T00:00:01Z", 1, True, PATCHER),
        ]
        self.assertEqual(landreq._first_bringers(found),
                         (frozenset({FIRST}), True))

    def test_a_rollback_cycle_names_every_bringer_beside_an_unreadable_one(self):
        """A wall-clock rollback can make the order cycle: a send (position 5)
        precedes a later row's retip by append order, while the timestamps put
        that retip before an earlier row's retip and that one before the send.
        A cycle orders nothing, so every bringer is named and the answer is
        unsettled, also when a candidate with an unreadable timestamp sits
        outside the cycle (it alone would otherwise be "first")."""
        send = ("2026-01-01T00:00:10Z", 5, False, FIRST)
        later = ("2026-01-01T00:00:05Z", 7, True, PATCHER)
        earlier = ("2026-01-01T00:00:08Z", 3, True, BUILDER)
        unreadable = (None, 1, True, OTHER)
        for found in ([send, later, earlier],
                      [send, later, earlier, unreadable],
                      [unreadable, earlier, later, send]):
            self.assertEqual(landreq._first_bringers(found),
                             (frozenset(c[3] for c in found), False), found)

    def test_an_AMBIGUOUS_first_bringer_names_the_carrier_too(self):
        """FIRST-BRINGER ORDER. Rounds are ordered by the ledger's own append
        order, so two sends of one tip are never a tie. A cure a FIX names is
        brought by a LATER event (the verdict). When append order cannot settle
        that event against a send, their timestamps decide; equal or unreadable
        timestamps name every candidate AND the seat that carried the tip into
        this chain: the conservative side.

        The rows are the real doors' rows read back from the ledger, with one
        timestamp edited; no door can mint a tie on demand."""
        build, _delivered = self.built("lane/cured")
        first = self.send(BUILDER, PATCHER, self.side, "lane/cured",
                          parent=build)
        cure = self.patched(first)
        rival = self.send(LEGACY, OTHER, cure, "lane/rival")
        other = self.send(DISPATCHER, FIRST, self.a, "lane/carried",
                          kind="build")
        carried = self.send(RELAY, OTHER, cure, "lane/carried", parent=other)
        rows, verdicts, err = landreq._ledger_fold()
        self.assertIsNone(err, err)
        patch_ts = rows[first["id"]]["verdict_ts"]
        chain = [rows[other["id"]], rows[carried["id"]]]

        def writers(ts):
            fold = dict(rows)
            fold[rival["id"]] = dict(rows[rival["id"]], ts=ts)
            return set(landreq.chain_writers(chain, fold))

        stamp = "%Y-%m-%dT%H:%M:%SZ"
        later = time.strftime(stamp, time.gmtime(
            calendar.timegm(time.strptime(patch_ts, stamp)) + 1))
        # CONTROL: the rival sent the cure a second after the FIX named it,
        # so the patch author is its one first bringer.
        self.assertEqual(writers(later), {FIRST, PATCHER})
        # The same second: order unknown, so both, and the carrier.
        self.assertEqual(writers(patch_ts), {FIRST, PATCHER, LEGACY, RELAY})
        # A timestamp that does not read: the same answer.
        self.assertEqual(writers("not-a-time"),
                         {FIRST, PATCHER, LEGACY, RELAY})


class TheAuthorsLineCreditsWhoWroteCodeTest(AuthorBase):
    """`helm lr close --reason landed` prints the AUTHORS line from
    `chain_credits`: the builder and the patch author, never the seat that
    dispatched the build or relayed a tip."""

    def test_a_landed_close_credits_the_builder_and_the_patch_author_only(self):
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/credits",
                          kind="build")
        first = self.send(BUILDER, PATCHER, self.side, "lane/credits",
                          parent=build)
        cure = self.patched(first)
        final = self.send_after(first, RELAY, OTHER, cure, "lane/credits")
        self.approved(final)
        self.git("merge", "--no-edit", "-q", "side")
        rc, out, err = _lr_run(["close", final["id"][:12], "--reason",
                                "landed", "--live"])
        self.assertEqual(rc, 0, err)
        line = [text for text in out.splitlines() if "AUTHORS" in text]
        self.assertEqual(len(line), 1, out)
        self.assertIn("AUTHORS %s, %s —" % (BUILDER, PATCHER), line[0])
        self.assertNotIn(DISPATCHER, line[0])
        self.assertNotIn(RELAY, line[0])

    def test_the_credit_list_is_the_writers_in_the_order_they_wrote(self):
        """The same answer off the chain's land request, before any close:
        the builder first, then the reviewer whose cure it carries."""
        build = self.send(DISPATCHER, BUILDER, self.a, "lane/credit-order",
                          kind="build")
        first = self.send(BUILDER, PATCHER, self.side, "lane/credit-order",
                          parent=build)
        cure = self.patched(first)
        final = self.send_after(first, RELAY, OTHER, cure,
                                "lane/credit-order")
        lr, why = landreq.get(final["id"])
        self.assertIsNone(why, why)
        lrs, unavailable = landreq.project(selector=final["id"])
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(landreq.chain_credits(lr, lrs), [BUILDER, PATCHER])


if __name__ == "__main__":
    unittest.main()
