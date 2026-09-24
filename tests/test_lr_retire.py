#!/usr/bin/env python3
"""helm lr retire — the ADMINISTRATIVE terminal, the one that claims nothing
about the work.

Hermetic, exactly as tests/test_landreq.py and tests/test_lr_close.py:
HELM_HOME/HELM_CHAT_DIR are tmp dirs, every git repo is minted in setUp, the
real ledgers and the real roster are never touched.

THE DISCIPLINE IN THIS FILE, because this verb writes an irreversible terminal
on somebody else's work:

  * every REFUSAL arm asserts the LEDGER HISTORY LENGTH IS UNCHANGED — the
    effect, never the absence of a complaint. A refusal that still appended
    would pass an `assertIsNotNone(err)` and destroy a row;
  * every vocabulary constant is IMPORTED OR DERIVED from the module under
    test, never transcribed. A copied tuple is pinned to nothing: a reason
    minted at the producer would leave a hand-written arm green while the
    verb refused it in production (store: my-tests-check-intent-probes, the
    COPIED-CONSTANT face). `test_the_vocabulary_is_not_a_copy`
    is the arm that proves the derivation by MOVING its input;
  * the reachability instrument is fed FAKE DATA through REAL LOGIC — the
    roster dict and the pane census are doubles, `seats._resolve_against`,
    `seats.recipient_matches` and `landreq.seat_reach` are the real thing —
    and `assert_doubles_in_effect` proves the doubles are actually being read
    before any arm trusts them.
"""
import contextlib
import inspect
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import textwrap
import time
import unittest
from unittest import mock

from helm import beacons, dispatches, eventledger, home, landreq, seats
from helm import seats_report
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq
from tests.test_landreq import run
from tests._satellite_resolution import ledger_sources


# THE FIXTURE-DEAD SEATS. Arms in this module name a party they intend to be
# GONE using one of three conventional prefixes. Under the ratified semantics
# "gone" is not the absence of a roster row — a seat helm has no row for is
# UNMEASURABLE, not absent — so these are rostered WITH NO SESSION and carry a
# positively-ABSENT presence row. That is the world in which a retirement is
# legitimately permitted, and it is what these arms have always meant.
# `test_the_dead_seat_roll_covers_every_conventional_name` keeps this honest.
_FIXTURE_DEAD_PREFIXES = ("dead-", "deleted-", "ghost-")
_FIXTURE_DEAD_SEATS = ("dead-author", "dead-custodian", "dead-recipient",
                       "dead-reviewer", "dead-sender", "deleted-author",
                       "deleted-seat", "ghost-author", "ghost-carrier",
                       "ghost-reviewer")

#: A sentinel distinct from None, because `activity=None` IS a world — the
#: authorship index could not be read — and must not collapse into "you did not
#: say". The same distinction `_UNPROBED` makes in the module under test.
_UNSET = object()


#: THE FIXTURE'S ONE NOW, read ONCE for the process. Every stamp and every
#: dated commit below is an offset from THIS instant, which is what makes two
#: readings of "the same age" comparable for EQUALITY at all. See `_ago`.
_FIXTURE_NOW = time.time()


def _ago_epoch(days, now=None):
    """Whole seconds since the epoch, `days` before the fixture's now."""
    return int((_FIXTURE_NOW if now is None else now) - days * 86400)


def _ago(days, now=None):
    """An ISO stamp `days` whole days in the past, in the spelling
    `dispatches._valid_ts` admits.

    DERIVED FROM THE CLOCK THE CODE READS, never a frozen literal. The rung
    under test compares against `time.time()`, so a hard-coded date would make
    every silence arm expire quietly: the fixture would drift past the window on
    its own and the arm asserting a seat is ACTING would flip to ABSENT months
    later for no reason anybody could see.

    AND READ ONCE, which is the other half of the same law. An arm mints a
    stamp INTO an event and then names THE SAME EXPRESSION as its expectation —
    `index.get(SEAT) == _ago(30)` — so a per-call clock read made the two ends
    of that comparison two separate readings of the wall clock. They agree on
    almost every run and differ by exactly one second whenever a second
    boundary falls between them: a coin flip the arm neither describes nor can
    see. `AnInvalidVerdictBindingCreditsNOTHINGTest` lost it once, red in a
    whole-suite gate and green on the same tree on the next run, and around
    forty arms in this module carry the same two-read shape. The pin costs
    nothing they measure — every window here is whole DAYS wide, and the pin
    drifts from the live clock by at most this suite's own runtime.

    `now=` is the seam for a stamp off some OTHER instant. Handing it
    `time.time()` reproduces the unpinned reading deliberately, which is how
    the boundary control drives the credit arms through a straddle.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(_ago_epoch(days, now)))


#: ONE EVENT PER LEDGER KIND, IN THE SHAPE ITS PRODUCER REALLY WRITES. Field
#: sets are copied off the live dispatch ledger and every id, seat, session and
#: sha is replaced with a synthetic one (tests/ is public-bound). They are
#: copied rather than composed because a fixture that invents its input tests no
#: world: the first spelling of the authorship index read a `verdict` at
#: top-level `recipient`, which the verdict writer does not emit, and every arm
#: over hand-built two-key dicts was green about a reading that saw almost no
#: review work at all.
#:
#: Each row is (why, expected actor or None, event). `None` is a MUST-MISS: the
#: kind records no hand, or the seat-shaped field on it names the MENTIONED
#: party instead of the actor.
_SESSION = "11111111-2222-3333-4444-555555555555"
#: The obligation the modern verdict shape below ANSWERS. One id, because the
#: identity a verdict's binding is validated against is the recipient of its own
#: dispatch, so the two events are one world and a fixture that carries only the
#: verdict is a ledger slice the producers never wrote.
_VERDICT_ID = "3" * 32


def _authority(seat, session=_SESSION, family="fixture"):
    """The v5 NATIVE authority snapshot a fixture seat's verdict is bound by.

    The exact shape `dispatches._family_evidence_error` admits for v5 — one
    verified native runtime, the roster identity matching, the session carried —
    so the anchor over it reproduces and the whole envelope validates.
    """
    return {"v": 5, "identity": seat, "roster_identity": seat,
            "session": session,
            "runtime": {"family": family, "backend": "native"},
            "runtime_verified": True}


def _bound_verdict(seat, rid=_VERDICT_ID, session=_SESSION, family="fixture",
                   **overrides):
    """A verdict event whose author binding the LEDGER'S OWN VALIDATOR accepts.

    BUILT BY THE PRODUCERS, never composed: `_verdict_author_runtime_evidence`
    derives the envelope from the authority proof and
    `_verdict_author_runtime_anchor` keys the hash over the result, so an arm
    cannot type an envelope that looks right and is not. The index resolves a
    verdict's hand through `dispatches._verdict_author_runtime_error` — the same
    door the reducer uses before it will record the binding at all — so a
    hand-built envelope is one the shipped reader REFUSES, and every arm over
    one would be green about a reading production rejects.
    """
    evidence = dispatches._verdict_author_runtime_evidence(
        seat, session, family, _authority(seat, session, family))
    assert evidence is not None, \
        "the envelope producer refused this fixture's authority proof"
    event = {"v": 4, "event": "verdict", "seq": 2, "id": rid, "ts": None,
             "verdict_ref": "4" * 32, "reviewed_tip": "b" * 40,
             "polarity": "fix", "gate": "", "gate_caps": [],
             "verdict_author_session": session,
             "verdict_author_runtime_evidence": evidence,
             "verdict_author_runtime_anchor":
                 dispatches._verdict_author_runtime_anchor(evidence)}
    event.update(overrides)
    return event


def _obligation(seat, rid=_VERDICT_ID, sender="asking-seat", **overrides):
    """The dispatch that ADDRESSES `seat`, which is what makes a verdict by it
    resolvable: the fold validates a verdict's binding against the recipient of
    the ACCEPTED obligation, so the verdict and its dispatch are one world and a
    fixture carrying only the verdict describes none."""
    event = dict(_REAL_EVENT_SHAPES[0][2], id=rid, recipient=seat,
                 sender=sender, operation_key="9" * 32)
    event.update(overrides)
    return event


_REAL_EVENT_SHAPES = (
    ("a plain dispatch is authored by its sender", "sender-seat",
     {"v": 3, "event": "dispatch", "seq": 0, "id": "a" * 32,
      "ts": None, "recipient": "recipient-seat", "lane": "lane/fixture",
      "tip": "b" * 40, "ref": "refs/heads/lane/fixture", "note": "n",
      "deadline_s": 86400, "source": "cli", "sender": "sender-seat",
      "repo_id": "c" * 12, "operation_key": "d" * 32,
      "message_hash": "e" * 64, "status": "open", "kind": "review"}),

    ("a MOVE credits the hand that WROTE the row, never the sender it "
     "inherited from the obligation it continues", "moving-seat",
     {"v": 3, "event": "dispatch", "seq": 0, "id": "f" * 32,
      "ts": None, "recipient": "recipient-seat", "lane": "lane/fixture",
      "tip": "b" * 40, "ref": "refs/heads/lane/fixture", "note": "n",
      "deadline_s": 86400, "source": "cli", "sender": "sender-seat",
      "repo_id": "c" * 12, "operation_key": "1" * 32,
      "message_hash": "2" * 64, "status": "open", "kind": "review",
      "acted_by": "moving-seat"}),

    ("the obligation a verdict answers, whose RECIPIENT is the identity the "
     "verdict's runtime binding is validated against", "asking-seat",
     {"v": 3, "event": "dispatch", "seq": 0, "id": _VERDICT_ID,
      "ts": None, "recipient": "reviewing-seat", "lane": "lane/fixture",
      "tip": "b" * 40, "ref": "refs/heads/lane/fixture", "note": "n",
      "deadline_s": 86400, "source": "cli", "sender": "asking-seat",
      "repo_id": "c" * 12, "operation_key": "9" * 32,
      "message_hash": "a" * 64, "status": "open", "kind": "review"}),

    ("A MODEL RUN'S ADVISORY READ IS CREDITED TO THE SEAT THAT RECORDED IT "
     "(task/2948): the model is not a seat, and the recorder is the hand",
     "recording-seat",
     {"v": 3, "event": "advisory-read", "seq": 1, "id": _VERDICT_ID,
      "ts": None, "reviewed_tip": "b" * 40, "verdict_ref": "read clean",
      "polarity": "concur", "reviewer_model": "gpt-6-astra",
      "reviewer_run": "wf-1", "author_model": "claude-opus-5-5",
      "author_model_source": "declared", "recorded_by": "recording-seat",
      "reviewer_family": "codex", "independence": "cross-family"}),

    ("A FINDINGS NOTE IS A MACHINE'S AND CREDITS NOBODY (task/2960): the "
     "local findings pass runs detached under no seat, and `reader` names a "
     "model family", None,
     {"v": 3, "event": "findings-note", "seq": 1, "id": "e" * 32,
      "ts": None, "reader": dispatches.FINDINGS_READER,
      "reviewed_tip": "b" * 40,
      "outcome": "complete", "rc": 0, "reads": 1, "kept": 0,
      "status_line": "LOCAL-REVIEW-STATUS complete reads=1 errors=0 empty=0 "
                     "cut=0 truncated=0 kept=0 rejected=0 unjudged=0"}),

    ("A VERDICT IS AUTHORED BY THE SEAT ITS RUNTIME ENVELOPE PROVES, and this "
     "one is built by the envelope producers so the shipped validator accepts "
     "it — the only reading that credits anybody", "reviewing-seat",
     _bound_verdict("reviewing-seat")),

    ("THE OLDEST VERDICT SPELLING IS A WHOLE-ROW SNAPSHOT AND ITS TOP-LEVEL "
     "`recipient` IS THE PARTY ASKED, not a hand: the row was written down, "
     "nobody was proven to have written it, and crediting that key is the "
     "mentioned-party law broken on the kind the law was written for", None,
     {"event": "verdict", "seq": 2, "id": "6" * 32, "ts": None,
      "recipient": "legacy-reviewer", "lane": "lane/fixture",
      "ref": "refs/heads/lane/fixture", "note": "n", "deadline_s": 86400,
      "source": "cli", "status": "reviewed", "ack_ref": "7" * 32,
      "verdict_ref": "8" * 32, "last_updated": None,
      "reviewed_tip": "b" * 40}),

    ("a close credits the closing hand, which the writer RESOLVES from its "
     "own declared seat rather than taking from a caller", "closing-seat",
     {"v": 3, "event": "close", "seq": 3, "id": "9" * 32, "ts": None,
      "close_reason": "landed", "close_proof_version": 2,
      "close_proof_mode": "ancestry", "reviewed_tip": "b" * 40,
      "closing_trunk_ref": "refs/remotes/origin/main",
      "closing_trunk_sha": "c" * 40, "closing_repo_id": "c" * 12,
      "close_delivery_class": "cli", "close_actor": "closing-seat"}),

    ("a withdrawal credits the seat that withdrew", "withdrawing-seat",
     {"v": 3, "event": "close", "seq": 3, "id": "0" * 32, "ts": None,
      "close_reason": "withdrawn", "close_proof_version": 2,
      "reviewed_tip": "b" * 40, "withdrawing_seat": "withdrawing-seat"}),

    ("A CONFIRMATION CLOSE STAMPS SOMEBODY ELSE'S NAMES INTO THE EVENT AND "
     "RECORDS NO HAND AT ALL, and that combination is the producer's, not a "
     "fixture's: measured over the live ledger, 28 closes carry "
     "`original_author` and 0 of them carry `close_actor`. So the mentioned "
     "party is the ONLY seat-shaped value on this event, and a table that read "
     "it would republish the author of every swept row as active on the day of "
     "the sweep — with nothing else on the event to notice", None,
     {"event": "close", "seq": 3, "id": "1a" * 16, "ts": None,
      "close_reason": "superseded", "close_proof_version": 2,
      "close_proof_mode": "confirmation", "reviewed_tip": "b" * 40,
      "closing_trunk_ref": "refs/remotes/origin/main",
      "closing_trunk_sha": "c" * 40, "closing_repo_id": "c" * 12,
      "confirmation_id": "2b" * 16, "confirmation_tip": "d" * 40,
      "confirmation_ref": "refs/heads/lane/fixture",
      "original_author": "mentioned-seat",
      "confirmation_recipient": "mentioned-reviewer",
      "original_author_family": "fixture",
      "confirmation_recipient_family": "fixture"}),

    ("AN ADMINISTRATIVE RETIREMENT CREDITS THE ACTING SEAT ITS WRITER "
     "RESOLVED, refusing to record a terminal with no actor on it at all — "
     "and this is the kind a reader keeping its own table of kinds had never "
     "heard of", "retiring-seat",
     {"v": 3, "event": "retire", "seq": 4, "id": "7b" * 16, "ts": None,
      "retire_reason": "author-unresolvable",
      "retire_measurement": "the owing seat is gone", "retire_note": None,
      "retire_seat": "retiring-seat",
      # DERIVED, NEVER TRANSCRIBED — this file's own first discipline, and this
      # is the field that proved why. The table says its shapes are copied off
      # the live ledger; this one could not be, because the fleet ledger holds
      # ZERO retire events across all 14577 of its rows. It was typed, and it
      # was typed WRONG: `2`, where the shipped replay admits only
      # `_RETIRE_PROOF_V`. Under an attribution reader that walked raw events
      # nothing noticed, so the arm asserting "a retirement today is work" was
      # green over an event this ledger would have refused.
      "retire_proof_version": dispatches._RETIRE_PROOF_V}),

    ("a cancel records no hand at all", None,
     {"v": 3, "event": "cancel", "seq": 1, "id": "3c" * 16, "ts": None,
      "reason": "no longer needed"}),

    ("a hold records no hand at all", None,
     {"v": 3, "event": "hold", "seq": 1, "id": "4d" * 16, "ts": None,
      "reason": "owner gate", "owner_gated": True}),

    ("a release records no hand at all", None,
     {"v": 3, "event": "release", "seq": 2, "id": "5e" * 16, "ts": None,
      "reason": "gate cleared"}),

    ("a delivery marker records no hand at all", None,
     {"v": 3, "event": "delivered", "seq": 1, "id": "6f" * 16, "ts": None,
      "delivery_ref": "7a" * 16}),

    ("a supersession marker records no hand at all", None,
     {"v": 3, "event": "superseded", "seq": 4, "id": "8b" * 16, "ts": None,
      "successor": "9c" * 16}),

    ("a retip stores a VERIFICATION WORD in `identity`, not a seat — reading "
     "it would mint two fleet-wide seats out of a status field", None,
     {"v": 3, "event": "retip", "seq": 2, "id": "0d" * 16, "ts": None,
      "tip": "b" * 40, "ref": "refs/heads/lane/fixture", "old_tip": "d" * 40,
      "reason": "rebased", "identity": "verified"}),
)


def _shapes(days):
    """`_REAL_EVENT_SHAPES` with every `ts` stamped `days` ago.

    The stamp is the one field a fixture must supply, because the shapes are
    about WHICH FIELD NAMES THE HAND and the rung under test compares the
    stamp against the clock the code reads (`_ago` states why a literal date
    would expire every silence arm quietly).
    """
    return [dict(event, ts=_ago(days)) for _why, _who, event in
            _REAL_EVENT_SHAPES]


def _activity_from(*events):
    """The activity reading AS THE SHIPPED PROJECTION COMPUTES IT.

    A fixture may not hand `seat_reach` a hand-typed tuple for the paths whose
    subject is what the projection reads: the tuple would agree with whatever
    the arm believed and the arm would measure its own belief. Every fixture
    below therefore passes producer-shaped EVENTS through
    `dispatches.seat_activity` and hands the caller its answer — which is now
    FOUR values, and the second is the one three rounds of one finding were
    about: the newest act NAMING a seat the fold could not attribute to
    anybody.

    IT TAKES EVENTS, NEVER A PATH, so no arm here can reach a real ledger; the
    hermetic half is `TheActivityReadingIsHERMETICTest`, which drives the
    no-argument form against a fixture HELM_HOME and asserts WHICH FILE it
    opened.
    """
    return dispatches.seat_activity(events=list(events))


def _shape_where(event_kind, field, **overrides):
    """The producer-shaped event of this kind that CARRIES `field`.

    `_one_shape` takes the first shape of a kind, which is the wrong selector
    when one kind has several real spellings that differ in exactly the field
    an arm is about — a close that records the closing hand and a close that
    records only the parties it mentions are two worlds, and no live event is
    both.
    """
    for _why, _who, event in _REAL_EVENT_SHAPES:
        if event.get("event") == event_kind and event.get(field):
            return dict(event, **overrides)
    raise AssertionError("no %s shape in the table carries %r"
                         % (event_kind, field))


def _one_shape(event_kind, **overrides):
    """One producer-shaped event of this kind, with fields replaced.

    DERIVED FROM THE SHAPE TABLE, never retyped: an arm that spells its own
    event out is the defect this whole class exists to close, and a field the
    producer renames must break the arms rather than leave them green.
    """
    for _why, _who, event in _REAL_EVENT_SHAPES:
        if event.get("event") == event_kind:
            return dict(event, **overrides)
    raise AssertionError("no shape in the table for kind %r" % event_kind)


#: ONE DECLARED ACT PER OBLIGATION, and the id is minted from the act's position
#: so no two can collide. THIS IS THE PROPERTY THE FIXTURES DID NOT HAVE: the
#: fold opens a row ONCE per id and every later event on that id must be the
#: next in sequence, so three `sender=` dispatches sharing one id is not a
#: ledger — it is one obligation plus two out-of-sequence rows the projection
#: refuses. The raw walk this projection replaces credited all three, which is
#: exactly why the fixtures could be invalid and green at the same time.
def _rid(position):
    return "%032x" % (0xD10 + position)


def _acts(*declared):
    """A VALID LEDGER PREFIX from (seat, kind, days-ago) declarations.

    `kind` is one of:
      * `send` — the seat OPENS an obligation, which is what a `sender` is;
      * `move` — the seat writes a row it inherited somebody else's `sender`
        on, so `acted_by` must beat the inherited name;
      * `verdict` — the seat is ASKED (its own obligation, opened a day
        earlier by somebody else) and answers with a verdict whose binding the
        shipped validator accepts. THE OBLIGATION RIDES WITH IT because the
        identity that binding is checked against is the recipient of the
        accepted row, and a verdict with no obligation is a slice no producer
        ever wrote;
      * `unbound-verdict` — the same obligation answered by a verdict carrying
        NO author binding at all. `mark_verdict(bind_author=False)` writes
        exactly this and replay ACCEPTS it: the act is real, the hand is
        unrecorded, and that combination is a review's second countertrace.

    EVERY ACT GETS ITS OWN OBLIGATION ID. That is not tidiness — see `_rid`.
    """
    events = []
    for position, (seat, kind, days) in enumerate(declared):
        rid = _rid(position)
        if kind == "send":
            events.append(_one_shape("dispatch", id=rid, ts=_ago(days),
                                     sender=seat, operation_key=_rid(position)))
            continue
        if kind == "move":
            events.append(_one_shape("dispatch", id=rid, ts=_ago(days),
                                     sender="inherited-sender", acted_by=seat,
                                     operation_key=_rid(position)))
            continue
        events.append(_obligation(seat, rid=rid, ts=_ago(days + 1)))
        verdict = _bound_verdict(seat, rid=rid, ts=_ago(days), seq=1)
        if kind == "unbound-verdict":
            for field in dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS:
                verdict.pop(field, None)
        elif kind != "verdict":
            raise AssertionError("no declared act of kind %r" % kind)
        events.append(verdict)
    return events


def _prefix_for(event, ts=None):
    """[the obligation this event belongs to, the event itself] — the smallest
    VALID LEDGER PREFIX carrying one producer-shaped row.

    A LEDGER NEVER BEGINS WITH A FOLLOW-ON. `close`, `retire`, `cancel`,
    `delivered` and every verdict spelling are transitions on an obligation that
    was opened first, and the projection reads the opened row's ACCEPTED
    recipient to resolve who a verdict was addressed to. Handing the fold the
    follow-on alone describes no world at all.

    The obligation takes the event's own id (distinct per row of the shape
    table, so two prefixes in one arm cannot collide) and the event is renumbered
    to the sequence that follows the opener. WHETHER THE FOLD THEN ACCEPTS IT IS
    THE FOLD'S BUSINESS and arms must not assume it does: a landed close proves
    itself against a repo, so the transition is legitimately refused here and the
    act lands in the doubt store instead.
    """
    rid = event["id"]
    opener = _obligation(event.get("recipient") or "reviewing-seat", rid=rid,
                         ts=_ago(40))
    return [opener, dict(event, seq=1, **({"ts": ts} if ts else {}))]

class RetireBase(_landreq.LandReqBase):
    """Fixture verbs shared by every retirement suite."""

    def setUp(self):
        """THE PROJECTION DESCRIBES WHATEVER ROSTER IS IN EFFECT.

        The retire door reads three instruments, and the arms that drive the
        REAL verb end-to-end (the CLI and sweep arms) do not go through
        `reach()` — so without this they read the LIVE presence projection,
        which has no row for a fixture's dead seats. Under the ratified
        semantics that is a MISS, which is UNKNOWN, which refuses: every such
        arm would fail with a refusal that looks like a code defect and is
        actually a fixture reading production.

        The default is computed from `seats.roster_checked()` at CALL time, so
        it always agrees with whatever roster the arm installed, and every
        rostered seat reads positively ABSENT — the world these fixtures mean.
        An arm wanting a LIVE seat overrides this by patching presence itself
        (`reach()` does exactly that, and the inner patch wins).
        """
        super().setUp()

        def describe_the_roster():
            roster, _failed = seats.roster_checked()
            return [{"seat": key, "presence": "absent"}
                    for key in (roster or {})]

        patch = mock.patch.object(seats_report, "presence_report",
                                  side_effect=describe_the_roster)
        patch.start()
        self.addCleanup(patch.stop)

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------
    def open_row(self, author="ghost-author", reviewer="ghost-reviewer",
                 lane="lane/retire", ref=None, **kw):
        """One OPEN dispatch with an exact author and reviewer."""
        kw.setdefault("new_work", "supersedes" not in kw)
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = author
        try:
            row = dispatches.add(reviewer, lane, ref=ref or self.side,
                                 repo=self.repo, kind="review", notify=False,
                                 **kw)
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        self.assertIsNotNone(row)
        self.assertEqual(row["sender"], author)
        return row

    def verdicted(self, polarity="fix", author="ghost-author",
                  reviewer="ghost-reviewer", lane="lane/retire", ref=None,
                  **kw):
        """One row carrying a real verdict event of the named polarity."""
        row = self.open_row(author=author, reviewer=reviewer, lane=lane,
                            ref=ref, **kw)
        dispatches._mark_delivered(row["id"], "post-retire")
        out, err = self.mark_verdict(row["id"], ref or self.side,
                                     "reviewed", polarity=polarity)
        self.assertIsNone(err, err)
        self.assertEqual(out["status"], "verdict")
        return out

    # ------------------------------------------------------------------
    # the reachability doubles
    # ------------------------------------------------------------------
    @staticmethod
    def roster(rows):
        """{seat: roster row} from a MAPPING — never kwargs.

        A SEAT NAME CARRIES HYPHENS AND A KWARG CANNOT, and the first version
        of this took `**rows`: every hyphenated seat silently became a
        DIFFERENT roster key, so "parked-reviewer" resolved against a roster
        containing "parked_reviewer", read UNNAMED instead of DARK, and one
        POSITIVE arm passed for a reason it was not testing while three
        hostile arms went red. The fixture was measuring its own spelling.
        `assert_reach` is the standing cure — every arm now PINS the
        instrument's answer before exercising the verb through it.
        """
        return {name: ({"session": session} if session else {})
                for name, session in rows.items()}

    @staticmethod
    def panes(*seats_):
        """The pane census shape `beacons.agent_index` returns."""
        return {"by_seat": {name.casefold(): [4000 + i]
                            for i, name in enumerate(seats_)},
                "by_pid": {}, "home_by_pid": {}}

    @staticmethod
    @contextlib.contextmanager
    def _both(first, second):
        """Two patches as one context manager, so `reach` can double a THIRD
        instrument without rewriting every `r_patch, a_patch = ...` call."""
        with first, second:
            yield

    def reach(self, roster=None, panes=None, roster_failed=False,
              presence=None, activity=_UNSET, silent_days=30):
        """Patch EVERY reachability instrument for the duration of a block.

        This used to patch two and the production code grew a third, so every
        arm below silently read the REAL presence projection: the fixture's
        dead seats were not in the live roster, so they came back as MISSES,
        and 34 arms measured production instead of their own world. The same
        enumerate-only-the-known blindness the once-per-action census had.

        AND THEN IT GREW A FOURTH, which is why this now doubles the ledger's
        authorship index too. `seat_reach` reads it on BOTH permit paths (an
        absence with no measured DURATION cannot spend a row), so without a
        double every arm expecting SEAT_ABSENT would be asking the real
        dispatch ledger about a fixture seat, get "authored nothing, ever", and
        read UNKNOWN — the third occurrence of this exact class in one helper.
        The `_both` docstring above anticipated it verbatim.

        `presence` DEFAULTS to one ABSENT row per roster seat — the world
        nearly every arm here intends, since these fixtures exist to describe
        seats that are gone. An arm that wants a live or unverified seat
        passes its own rows.

        `activity` DEFAULTS to every roster and pane name having last authored
        `silent_days` ago, in a CURRENT index — the world these fixtures mean
        when they say a seat is gone. An arm about the duration rung itself
        passes its own tuple (or `silent_days=3` for a seat that is merely
        between sessions), and `activity=None` is the unreadable instrument.
        """
        # AN EXPLICIT EMPTY ROSTER IS A WORLD, NOT AN OMISSION. `roster={}`
        # means "the whole fleet is down", which must keep reading UNKNOWN —
        # merging the dead-seat roll into it would quietly destroy that arm's
        # premise. `roster=None` means unspecified, and there the roll is
        # a convenience.
        explicitly_empty = roster is not None and not roster
        roster = dict(roster or {})
        if not explicitly_empty:
            for name in _FIXTURE_DEAD_SEATS:
                roster.setdefault(name, {})     # rostered, no session
        if presence is None:
            presence = [{"seat": key, "presence": "absent"} for key in roster]
        if activity is _UNSET:
            names = set(roster)
            names.update((panes or {}).get("by_seat") or {})
            # NO DOUBT BY DEFAULT. The second store is the newest act naming a
            # seat that the projection could NOT attribute, and these fixtures
            # mean a seat whose absence is plain — an arm about the doubt rung
            # passes its own reading.
            activity = ({name.casefold(): _ago(silent_days) for name in names},
                        {}, _ago(0), None)
        return (self._both(
            self._both(
                mock.patch.object(seats, "roster_checked",
                                  return_value=(roster, roster_failed)),
                mock.patch.object(landreq, "_seat_activity",
                                  return_value=activity)),
            mock.patch.object(seats_report, "presence_report",
                              return_value=list(presence))),
            mock.patch.object(beacons, "agent_index", return_value=panes))

    def assert_reach(self, seat, expected, roster, panes,
                     roster_failed=False, presence=None, activity=_UNSET,
                     silent_days=30):
        """PIN WHAT THE INSTRUMENT ANSWERS before an arm relies on it.

        An arm that only asserts the verb's OUTCOME cannot tell a correct
        refusal from a fixture that fed it the wrong world. This states the
        premise as an assertion, so a mis-keyed roster is a red line naming
        the seat rather than a green arm testing something else.
        """
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      roster_failed=roster_failed,
                                      presence=presence, activity=activity,
                                      silent_days=silent_days)
        with r_patch, a_patch:
            state, detail = landreq.seat_reach(seat)
        self.assertEqual(state, expected, "@%s read %s: %s"
                         % (seat, state, detail))
        return detail

    def assert_doubles_in_effect(self, roster, panes):
        """PROVE THE FIXTURE IS WHAT THE CODE READS before trusting an arm.

        A double that is bypassed makes every arm above it a measurement of
        production (store: a-green-suite-does-not-prove-the-edit-did-what-i-
        meant). `landreq.seat_reach` imports `seats` and `beacons` as MODULES
        and reads the attributes at call time, so patching the attribute is
        the right seam — and this asserts it, rather than assuming it.
        """
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch:
            # THE EFFECTIVE ROSTER, not the one the arm typed: `reach` also
            # rosters `_FIXTURE_DEAD_SEATS` as positively-absent, because
            # "gone" is now a rostered-and-absent fact rather than a missing
            # row. Comparing against the typed dict would fail for a reason
            # that has nothing to do with whether the double is installed.
            live, failed = seats.roster_checked()
            self.assertFalse(failed)
            for key, value in roster.items():
                self.assertEqual(live.get(key), value,
                                 "the double dropped or altered @%s" % key)
            for name in _FIXTURE_DEAD_SEATS:
                self.assertIn(name, live,
                              "the dead-seat roll is not in the effective "
                              "roster, so @%s would read UNMEASURABLE" % name)
            self.assertIs(beacons.agent_index(), panes)

    # ------------------------------------------------------------------
    # ledger
    # ------------------------------------------------------------------
    def history(self, rid=None):
        rows = eventledger.events(dispatches.ledger_path())
        return [e for e in rows if rid is None or e.get("id") == rid]

    def retire_events(self, rid):
        return [e for e in self.history(rid) if e.get("event") == "retire"]

    def retired_row(self, **kw):
        """One row really retired through the real verb, for the arms whose
        subject is what a RETIRED row does to everything else."""
        row = self.verdicted(polarity="fix", author="deleted-author", **kw)
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "author-unresolvable",
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        return row

    def ledger_state(self, rid):
        """(events for this row, retire events for this row) — the pair a
        refusal must leave EXACTLY as it found it.

        BOTH NUMBERS, because either alone is blind: a total that did not move
        misses a rewrite, and a retire count alone misses a refusal that
        appended some OTHER event. The retire count is not asserted to be
        zero, because the refusal arms include a row that is ALREADY retired
        and must not gain a second one.
        """
        return len(self.history(rid)), len(self.retire_events(rid))

    def assert_nothing_appended(self, rid, before, why):
        """A refusal must leave the ledger byte-for-byte where it was."""
        self.assertIsNotNone(why)
        self.assertEqual(self.ledger_state(rid), before,
                         "a refusal APPENDED to the ledger: %s" % why)

    # ------------------------------------------------------------------
    # tier double
    # ------------------------------------------------------------------
    @staticmethod
    def tier(kind, why="fixture tier"):
        """The (state, why) PAIR `approval_tier_for_verdict` really returns,
        minted by `dispatches._tier_unknown` so the state word is a genuine
        `TierUnknown` carrying the kind — a plain "unknown" string would be
        classified UNCLASSIFIED and the arm would be testing the wrong
        vocabulary member."""
        return dispatches._tier_unknown(kind, why)

    def tier_lens(self, answer):
        return mock.patch.object(dispatches, "approval_tier_for_verdict",
                                 return_value=answer)


class VocabularyTest(RetireBase):
    """The reason codes are a CONTRACT between three modules; nothing here
    may be a hand-copied tuple."""

    def test_the_vocabulary_is_not_a_copy(self):
        """DERIVED, AND THE DERIVATION IS PROVED BY MOVING ITS INPUT.

        `landreq.RETIRE_REASONS` must BE `dispatches.RETIRE_REASONS`, not a
        tuple that happens to coincide with it. The proof is the move: a reason
        minted at the producer appears at the consumer with no edit. A
        transcribed tuple passes an equality assertion forever and fails the
        moment somebody adds a fifth code — silently, which is the whole
        problem.
        """
        self.assertIs(landreq.RETIRE_REASONS, dispatches.RETIRE_REASONS)
        minted = dispatches.RETIRE_REASONS + ("fixture-only-reason",)
        with mock.patch.object(dispatches, "RETIRE_REASONS", minted):
            # The CLI's own refusal sentence is built from the live tuple, so
            # it must name the new code without this file being edited.
            self.assertIn("fixture-only-reason",
                          "|".join(dispatches.RETIRE_REASONS))

    def test_every_reason_has_a_measurement_and_a_sweep_position(self):  # noqa: VACUOUS_ASSERTION — the two unconditional set-equality assertions above the loop ARE the positive control on the same observable
        """A code with no probe is a code that can never be measured, and a
        code missing from the sweep order is one the sweep can never apply.
        Both are silent until somebody tries the verb in anger."""
        self.assertEqual(set(landreq.RETIRE_MEASUREMENTS),
                         set(dispatches.RETIRE_REASONS))
        self.assertEqual(set(landreq.RETIRE_SWEEP_ORDER),
                         set(dispatches.RETIRE_REASONS))
        for reason in dispatches.RETIRE_REASONS:
            self.assertTrue(callable(landreq.RETIRE_MEASUREMENTS[reason]))

    def test_a_reason_outside_the_vocabulary_never_appends(self):  # noqa: VACUOUS_ASSERTION — assert_nothing_appended compares a REAL pre-call (events, retire-events) reading to the post-call one — a positive binding on both counters
        """THE MUST-MISS, and its input is DERIVED so it cannot go stale: a
        real code with one character changed. If this file could only name
        rejects the vocabulary already knows, it would be measuring the
        intent rather than the verb."""
        row = self.verdicted()
        near_miss = dispatches.RETIRE_REASONS[0] + "-x"
        self.assertNotIn(near_miss, dispatches.RETIRE_REASONS)
        before = self.ledger_state(row["id"])
        out, why = landreq.retire(row["id"], near_miss, seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("must be one of", why)


class SeatReachTest(RetireBase):
    """The instrument that separates a PARKED seat from a WALLED one."""

    def test_a_roster_session_is_reachable(self):  # noqa: VACUOUS_ASSERTION — assertEqual(state, SEAT_LIVE) plus assertIn on the session fragment are both unconditional positives
        roster = self.roster({"walled": "session-abc"})
        self.assert_doubles_in_effect(roster, self.panes())
        r_patch, a_patch = self.reach(roster=roster, panes=self.panes())
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("walled")
        self.assertEqual(state, landreq.SEAT_LIVE)
        self.assertIn("session-abc"[:8], detail)

    def test_a_live_pane_is_reachable_even_with_no_roster_session(self):  # noqa: VACUOUS_ASSERTION — assertEqual(state, SEAT_LIVE) and the assertIn on the pane sentence are unconditional positives
        """THE CASE THIS VERB EXISTS TO REFUSE. A seat whose upstream is
        rate-limited stops heartbeating — its roster row loses its session —
        while its pane is alive and its operator is waiting. Reading the
        roster alone would call that seat gone and write off its reviews."""
        roster = self.roster({"walled": None, "other": "s"})
        panes = self.panes("walled")
        self.assert_doubles_in_effect(roster, panes)
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("walled")
        self.assertEqual(state, landreq.SEAT_LIVE)
        self.assertIn("declares seat @walled", detail)

    def test_rostered_with_no_session_and_no_pane_is_positively_ABSENT(self):
        """DARK COLLAPSED INTO ABSENT — a real narrowing, stated rather than
        left as a surprise.

        DARK meant "rostered, no session, no pane". ABSENT means that AND the
        canonical projection positively reporting no recent beat. Because the
        projection emits one row per ROSTER seat, every seat that could have
        been DARK is described by it, so the two coincide and DARK is no
        longer reachable from this door. The distinction that survives is the
        one that carries the safety: measured versus not measurable at all.
        """
        roster = self.roster({"parked": None, "other": "s"})
        r_patch, a_patch = self.reach(roster=roster, panes=self.panes("other"))
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("parked")
        self.assertEqual(state, landreq.SEAT_ABSENT, detail)

    def test_a_rostered_seat_that_acted_inside_the_window_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the first assert_reach in this arm is an unconditional SEAT_ABSENT control on the SAME seat, roster, projection and pane census, plus assertIn on 3d ago and ACTING
        """A SEAT BETWEEN SESSIONS IS NOT A DEPARTED SEAT.

        Its four clauses are all positive and all read at ONE INSTANT: a roster
        row exists, the projection says absent, no session, no pane. None of
        them dates the absence, and SEAT_ABSENT is the only state that permits a
        retirement — so a seat between sessions reaches it exactly as a departed
        one does. Measured on the live board, a rostered standing-current seat
        whose last authored event was 3 DAYS old reached the permit state on 73
        OPEN ROWS.

        The paired arm above is the unconditional positive control: the SAME
        seat, SAME roster, SAME projection, SAME pane census, and only the
        silence duration different, reads ABSENT. So this arm cannot pass
        because the fixture failed to describe a gone seat.
        """
        roster = self.roster({"parked": None, "other": "s"})
        panes = self.panes("other")
        self.assert_reach("parked", landreq.SEAT_ABSENT, roster, panes,
                          silent_days=30)
        detail = self.assert_reach("parked", landreq.SEAT_UNKNOWN, roster,
                                   panes, silent_days=3)
        self.assertIn("3d ago", detail)
        self.assertIn("ACTING", detail,
                      "the refusal must say the seat was measured ACTING, not "
                      "merely that the door declined")

    def test_a_live_session_outranks_any_measured_silence(self):  # noqa: VACUOUS_ASSERTION — assertEqual(state, SEAT_LIVE) and the assertIn on the session fragment are both unconditional positives
        """THE RUNG MUST NARROW THE PERMIT AND NEVER TOUCH LIVE.

        Measured beside the cure: one seat had authored nothing for 32 days and
        read LIVE off a current roster session, on 13 open rows. A duration rung
        that could downgrade that would be reading silence AS absence, which is
        the defect this cures, pointing the other way. Both permit paths reach
        the rung only after every liveness exit, so a positive liveness proof
        always wins — asserted here rather than left to the reading order.
        """
        roster = self.roster({"walled": "session-abc"})
        detail = self.assert_reach("walled", landreq.SEAT_LIVE, roster,
                                   self.panes(), silent_days=400)
        self.assertIn("session-abc"[:8], detail)

    def test_an_unreadable_authorship_index_refuses_a_rostered_absence(self):  # noqa: VACUOUS_ASSERTION — the first assert_reach is an unconditional SEAT_ABSENT control in the same world with a readable index
        """FAIL-CLOSED ON THE FIFTH INSTRUMENT, like the other four.

        `activity=None` is the tri-state UNKNOWN — the ledger could not be read
        — and an absence with no measured duration never spends a row. The
        control is the same world with a readable index, which admits.
        """
        roster = self.roster({"parked": None, "other": "s"})
        panes = self.panes("other")
        self.assert_reach("parked", landreq.SEAT_ABSENT, roster, panes)
        detail = self.assert_reach("parked", landreq.SEAT_UNKNOWN, roster,
                                   panes, activity=None)
        self.assertIn("no measured DURATION", detail)

    def test_a_stale_authorship_index_cannot_expire_a_fleet(self):  # noqa: VACUOUS_ASSERTION — the first assert_reach is an unconditional SEAT_ABSENT control in the same world with a current index
        """A LEDGER THAT STOPPED BEING WRITTEN MAKES EVERYONE LOOK GONE.

        The per-seat reading is only meaningful against a CURRENT index, so the
        index's own newest authorship must be inside the window. Without this
        rung a hub whose ledger went quiet for a fortnight would read every seat
        on the board as departed at once — the mass-termination shape `stranded`
        names, arriving through the seat instrument instead of the repo.
        """
        roster = self.roster({"parked": None, "other": "s"})
        panes = self.panes("other")
        self.assert_reach("parked", landreq.SEAT_ABSENT, roster, panes)
        detail = self.assert_reach(
            "parked", landreq.SEAT_UNKNOWN, roster, panes,
            activity=({"parked": _ago(40)}, {}, _ago(20), None))
        self.assertIn("not current", detail)

    def test_a_seat_the_ledger_never_saw_act_is_not_proven_departed(self):  # noqa: VACUOUS_ASSERTION — the first assert_reach is an unconditional SEAT_ABSENT control in the same world with a record for this seat
        """A NAME HELM NEVER RECORDED AS AN ACTOR IS A TYPO, NOT A DEPARTURE.

        The index is a LOWER BOUND on activity, so "no record" is the one
        reading it cannot date at all. Refusing costs a revisit; admitting would
        retire on a misspelling.
        """
        roster = self.roster({"parked": None, "other": "s"})
        panes = self.panes("other")
        self.assert_reach("parked", landreq.SEAT_ABSENT, roster, panes)
        detail = self.assert_reach(
            "parked", landreq.SEAT_UNKNOWN, roster, panes,
            activity=({"somebody-else": _ago(1)}, {}, _ago(0), None))
        self.assertIn("author of nothing, ever", detail)

    def test_both_permit_paths_clear_the_SAME_duration_rung(self):  # noqa: VACUOUS_ASSERTION — the 30d leg of the loop asserts SEAT_ABSENT unconditionally on BOTH permit paths before the 3d leg asserts the refusal
        """ONE RUNG, NOT TWO SPELLINGS OF ONE IDEA.

        The rostered and unrostered routes to SEAT_ABSENT must not mean two
        different things, which is exactly what the first cut of phase 2 did:
        it wrote the duration law down on `SEAT_SILENCE_DAYS` and then applied
        it to the unrostered path only. This drives BOTH routes through the same
        two worlds and asserts they agree, so re-splitting the rung reddens here
        rather than in production six weeks later.
        """
        rostered = self.roster({"parked": None, "other": "s"})
        # `other` is the live-projection + declaring-pane world the unrostered
        # path additionally requires; `stranger` is on no roster row.
        unrostered = {"other": {"session": "s"}}
        panes = self.panes("other")
        live = [{"seat": "other", "presence": "fresh"}]
        for days, expected in ((30, landreq.SEAT_ABSENT),
                               (3, landreq.SEAT_UNKNOWN)):
            self.assert_reach("parked", expected, rostered, panes,
                              silent_days=days)
            self.assert_reach(
                "stranger", expected, unrostered, self.panes("other"),
                presence=live,
                activity=({"stranger": _ago(days), "other": _ago(0)},
                          {}, _ago(0), None))

    def test_a_name_matching_no_roster_row_is_UNKNOWN_not_absent(self):
        """NO ROSTER ROW IS A MISS, NOT A MEASUREMENT.

        This arm used to expect UNNAMED and treat it as "gone". Every helm
        liveness instrument is keyed on the roster, so a seat with no row is
        described by NONE of them — reading that as absence is precisely what
        would retire an unrostered live seat. The seat here is deliberately
        NOT on `_FIXTURE_DEAD_SEATS`, because that roll rosters its names.
        """
        roster = self.roster({"seat-a": "s"})
        r_patch, a_patch = self.reach(roster=roster, panes=self.panes("seat-a"))
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("never-rostered-seat")
        self.assertEqual(state, landreq.SEAT_UNKNOWN, detail)
        self.assertNotEqual(state, landreq.SEAT_ABSENT,
                            "an unrostered seat reached the permit state")
        self.assertIn("no row", detail,
                      "the refusal must say the projection has no row for "
                      "this seat, which is what makes it a MISS")

    def test_an_EMPTY_roster_is_UNKNOWN_never_a_board_of_deleted_seats(self):
        """A fleet that is entirely down is not a fleet of deleted seats.
        Absence of an instrument's OUTPUT is never evidence against a seat —
        and an empty roster reads `failed=False`, so nothing else catches
        it."""
        r_patch, a_patch = self.reach(roster={}, panes=self.panes())
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("anyone")
        self.assertEqual(state, landreq.SEAT_UNKNOWN)
        self.assertIn("EMPTY", detail)

    def test_an_unreadable_roster_is_UNKNOWN(self):
        r_patch, a_patch = self.reach(roster={"x": {}}, panes=self.panes(),
                                      roster_failed=True)
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("anyone")
        self.assertEqual(state, landreq.SEAT_UNKNOWN)
        self.assertIn("NOT measured", detail)

    def test_an_unlistable_process_table_is_UNKNOWN(self):
        """`agent_index` answers None when /proc could not be walked. Reading
        that as "no pane declares this seat" would turn one unreadable
        instrument into a fleet of dead seats."""
        r_patch, a_patch = self.reach(roster=self.roster({"x": None}), panes=None)
        with r_patch, a_patch:
            state, detail = landreq.seat_reach("x")
        self.assertEqual(state, landreq.SEAT_UNKNOWN)
        self.assertIn("process table", detail)


class ReasonPositiveTest(RetireBase):
    """One positive arm per reason code, each on a REAL row."""

    def test_author_unresolvable_retires_a_row_owed_by_a_deleted_seat(self):  # noqa: VACUOUS_ASSERTION — assert_reach pins the instrument and the retire_events count is asserted == 1, both unconditional positives
        row = self.verdicted(polarity="fix", author="deleted-author")
        roster = self.roster({"seat-a": "s"})
        panes = self.panes("seat-a")
        self.assert_doubles_in_effect(roster, panes)
        self.assert_reach("deleted-author", landreq.SEAT_ABSENT, roster, panes)
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "author-unresolvable",
                                      seat="integrator", note="15d cruft")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(out["retire_reason"], "author-unresolvable")
        self.assertEqual(out["retire_seat"], "integrator")
        self.assertEqual(out["retire_note"], "15d cruft")
        self.assertIn("deleted-author", out["retire_measurement"])
        self.assertEqual(len(self.retire_events(row["id"])), 1)
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "RETIRED")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertFalse(lr["stalled"])

    def test_repo_unreadable_retires_a_row_whose_repository_is_gone(self):
        """MEASURED BY ASKING GIT, on a repository this arm really destroys."""
        row = self.verdicted(polarity="fix", author="deleted-author")
        gone = os.path.join(self.tmp, "vanished", ".git")
        probe = subprocess.run(["git", "--git-dir", gone, "rev-parse",
                                "--git-dir"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the positive arm must really be unreadable")
        fresh = dispatches.snapshot()[0][row["id"]]
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_repo_unreadable(
                dict(fresh, repo_id=gone), ctx)
        self.assertIsNone(refusal, refusal)
        self.assertIn(gone, measurement)

    def test_repo_readable_REFUSES_repo_unreadable(self):
        """The paired contradiction — without it the arm above would pass for
        a probe that never asked Git anything."""
        row = self.verdicted(polarity="fix", author="deleted-author")
        fresh = dispatches.snapshot()[0][row["id"]]
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_repo_unreadable(fresh, ctx)
        self.assertIsNone(measurement)
        self.assertIn("git ANSWERS", refusal)

    def test_succession_unreadable_retires_a_row_whose_carrier_was_pruned(self):
        """THE ROWS ARE BUILT FIRST AND THE OBJECT DESTROYED AFTER, because
        that is the real sequence: a carrier is reviewed while its commit
        exists and the commit is pruned weeks later by a history rewrite.
        Pruning first made `dispatch add` refuse the unresolvable ref, so the
        arm never reached the code it was written for."""
        tip = self.doomed_tip()
        carrier = self.verdicted(polarity="fix", author="deleted-author",
                                 lane="lane/carrier", ref=tip)
        child = self.open_row(author="deleted-author", lane="lane/child",
                              supersedes=carrier["id"])
        dispatches._mark_delivered(child["id"], "post-child")
        self.prune(tip)
        fresh = dispatches.snapshot()[0][child["id"]]
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_succession_unreadable(
                fresh, ctx)
        self.assertIsNone(refusal, refusal)
        self.assertIn("cannot read its reviewed commit", measurement)

    def test_a_READABLE_carrier_REFUSES_succession_unreadable(self):
        carrier = self.verdicted(polarity="fix", author="deleted-author",
                                 lane="lane/carrier")
        child = self.open_row(author="deleted-author", lane="lane/child",
                              supersedes=carrier["id"], new_work=False)
        fresh = dispatches.snapshot()[0][child["id"]]
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_succession_unreadable(
                fresh, ctx)
        self.assertIsNone(measurement)
        self.assertIn("READS", refusal)

    def test_tier_unevaluable_parked_retires_a_dark_tier_on_a_parked_seat(self):  # noqa: VACUOUS_ASSERTION — assert_reach + the two assertIn on the recorded measurement are unconditional positives
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="parked-reviewer")
        roster = self.roster({"parked-reviewer": None, "seat-a": "s"})
        panes = self.panes("seat-a")
        self.assert_doubles_in_effect(roster, panes)
        self.assert_reach("parked-reviewer", landreq.SEAT_ABSENT, roster,
                          panes)
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        dark = self.tier(dispatches.TIER_DARK, "no runtime proof ever stored")
        self.assertEqual(dispatches.tier_unknown_kind(dark[0]),
                         dispatches.TIER_DARK)
        with r_patch, a_patch, self.tier_lens(dark):
            out, why = landreq.retire(row["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertEqual(out["retire_reason"], "tier-unevaluable-parked")
        self.assertIn("DARK", out["retire_measurement"])
        self.assertIn("parked-reviewer", out["retire_measurement"])

    def doomed_tip(self):
        """A real commit on a throwaway branch — reviewable now, prunable
        later. Same shape as tests/test_landreq.py's `ghost`, split in two so
        the ledger rows can be written while the object still resolves."""
        self.git("checkout", "-q", "-b", "retire-ghost", self.main)
        tip = self.commit("ghost carrier", path="ghost-carrier")
        self.git("checkout", "-q", self.main)
        return tip

    def prune(self, tip):
        """Destroy the object and ASSERT the destruction — `gc` is not a
        promise, and an arm built on an object that survived would be
        measuring nothing."""
        self.git("branch", "-D", "retire-ghost")
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        probe = subprocess.run(["git", "-C", self.repo, "cat-file", "-e",
                                tip + "^{commit}"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the positive arm must really prune the carrier")


class HostileTest(RetireBase):
    """The population this verb must NEVER touch."""

    def test_a_LIVE_reviewer_refuses_tier_unevaluable_parked(self):
        """A live-and-stampable reviewer goes the normal path. The tier is
        DARK here — identical to the positive arm — and ONLY the seat's
        reachability differs, so this arm can only pass if the reachability
        clause is the thing deciding."""
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="live-reviewer")
        roster = self.roster({"live-reviewer": "session-live"})
        self.assert_reach("live-reviewer", landreq.SEAT_LIVE, roster,
                          self.panes())
        r_patch, a_patch = self.reach(roster=roster, panes=self.panes())
        dark = self.tier(dispatches.TIER_DARK, "no runtime proof ever stored")
        before = self.ledger_state(row["id"])
        with r_patch, a_patch, self.tier_lens(dark):
            out, why = landreq.retire(row["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("REACHABLE", why)

    def test_a_TEMPORARILY_WALLED_seat_refuses(self):  # noqa: VACUOUS_ASSERTION — assert_doubles_in_effect and the assertIn on the refusal sentence bind a real refusal, and the paired positive is ReasonPositiveTest's identical-tier arm
        """THE DISCRIMINATION THAT MATTERS. This seat has NO roster session —
        it stopped heartbeating when its upstream started refusing — and its
        tier is DARK for exactly that reason. It is not parked: its pane is
        alive. The only thing separating it from the positive arm is the pane
        census, which is what makes this arm a test of the discriminator and
        not of the tier."""
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="walled-reviewer")
        roster = self.roster({"walled-reviewer": None, "other": "s"})
        panes = self.panes("walled-reviewer")
        self.assert_doubles_in_effect(roster, panes)
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        dark = self.tier(dispatches.TIER_DARK, "upstream walled since 16:33Z")
        before = self.ledger_state(row["id"])
        with r_patch, a_patch, self.tier_lens(dark):
            out, why = landreq.retire(row["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("WALLED", why)

    def test_a_TRANSIENT_tier_refuses_even_on_a_parked_seat(self):
        """THE OTHER MUST-MISS. TIER_DARK is the only kind that means
        "nothing is stored to re-read"; TRANSIENT means the read failed at a
        live step and the NEXT one may answer. Retiring on it would write off
        a row over one flaky probe."""
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="parked-reviewer")
        r_patch, a_patch = self.reach(
            roster=self.roster({"parked-reviewer": None, "seat-a": "s"}),
            panes=self.panes("seat-a"))
        flaky = self.tier(dispatches.TIER_TRANSIENT, "read failed live")
        before = self.ledger_state(row["id"])
        with r_patch, a_patch, self.tier_lens(flaky):
            out, why = landreq.retire(row["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn(dispatches.TIER_TRANSIENT, why)

    def test_an_EVALUABLE_tier_refuses(self):
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="parked-reviewer")
        r_patch, a_patch = self.reach(
            roster=self.roster({"parked-reviewer": None, "seat-a": "s"}),
            panes=self.panes("seat-a"))
        before = self.ledger_state(row["id"])
        with r_patch, a_patch, self.tier_lens(("ok", None)):
            out, why = landreq.retire(row["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("EVALUATES", why)

    def test_a_DARK_owing_seat_refuses_author_unresolvable(self):
        """An idle seat is WOKEN, not written off. DARK and UNNAMED are one
        keystroke apart in the vocabulary and opposite facts in the world."""
        row = self.verdicted(polarity="fix", author="idle-author")
        roster = self.roster({"idle-author": None, "seat-a": "s"})
        panes = self.panes("seat-a")
        # IDLE IS NOW SAID IN THE PROJECTION'S OWN WORD. "Rostered with no
        # session" used to stand in for idle; it now reads positively ABSENT,
        # which is a different fact. A seat merely between turns says so as
        # `quiet`, and that is what must be woken rather than written off.
        idle = [{"seat": "idle-author", "presence": "quiet"},
                {"seat": "seat-a", "presence": "fresh"}]
        self.assert_reach("idle-author", landreq.SEAT_LIVE, roster, panes,
                          presence=idle)
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      presence=idle)
        before = self.ledger_state(row["id"])
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "author-unresolvable",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("REACHABLE", why,
                      "an idle seat must be reported as reachable, so the "
                      "refusal tells an operator to wake it")

    def test_an_unmeasurable_roster_refuses_every_reason(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts a non-None refusal AND an unchanged (events, retire-events) pair read from the real ledger
        """FAIL CLOSED ACROSS THE WHOLE VOCABULARY, derived by iterating it —
        so a reason cannot ship with a fail-OPEN blind spot.

        WHAT AN UNREADABLE ROSTER BUYS IS NOT THE SAME FOR EVERY REASON, and
        naming that is what keeps this arm honest. For the belt-bound reasons
        the refusal IS the roster: nobody could be measured absent. The
        `_RETIRE_BELT_EXEMPT` reason does not consult the roster at all, so
        this row refuses it on its OWN subject — the reviewed object is still
        present — and the clause below pins that, rather than letting the
        exempt reason pass this census for a reason the census is not about.
        """
        row = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="parked-reviewer")
        dark = self.tier(dispatches.TIER_DARK, "no runtime proof")
        for reason in dispatches.RETIRE_REASONS:
            with self.subTest(reason=reason):
                r_patch, a_patch = self.reach(roster={"x": {}},
                                              panes=self.panes(),
                                              roster_failed=True)
                before = self.ledger_state(row["id"])
                with r_patch, a_patch, self.tier_lens(dark):
                    out, why = landreq.retire(row["id"], reason,
                                              seat="integrator")
                self.assertIsNone(out)
                self.assert_nothing_appended(row["id"], before, why)
                if reason in landreq._RETIRE_BELT_EXEMPT:
                    self.assertIn(
                        row["reviewed_tip"][:12], why,
                        "the exempt reason must refuse on its OWN subject "
                        "here — this fixture's reviewed object is present")

    def test_a_live_owing_seat_refuses_repo_unreadable(self):
        """The universal guard. A repository that vanished is a permanent
        fact, but a LIVE owner can still close the row properly, and the
        normal path always beats an administrative terminal because it
        produces a claim about the work."""
        row = self.verdicted(polarity="fix", author="live-author")
        fresh = dispatches.snapshot()[0][row["id"]]
        gone = os.path.join(self.tmp, "vanished", ".git")
        roster = self.roster({"live-author": "session-live"})
        self.assert_reach("live-author", landreq.SEAT_LIVE, roster,
                          self.panes())
        r_patch, a_patch = self.reach(roster=roster, panes=self.panes())
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            # THROUGH THE ONE PATH, because that is where the belt lives now.
            # Calling the reason directly used to apply the belt (each reason
            # carried a copy); it no longer does, so a direct call reaches the
            # git probe and this arm would assert about the wrong refusal.
            measurement, refusal = landreq._measure_retire(
                "repo-unreadable", dict(fresh, repo_id=gone), ctx)
        self.assertIsNone(measurement)
        self.assertIn("REACHABLE", refusal)

    def test_a_git_that_did_NOT_RUN_refuses_repo_unreadable(self):
        """A TIMEOUT IS NOT A DESTROYED REPOSITORY, and `landreq._git` returns
        the SAME None for both — an unspawnable git and a five-second timeout
        on a loaded box. The first cut read that None as a measurement and
        would have retired live work during a busy moment. Only a git that
        RAN and answered non-zero is the finding.

        The double replaces `landreq._git` itself, which is the function the
        probe calls and the seam every other test in this repo patches.
        """
        row = self.verdicted(polarity="fix", author="seat-b")
        fresh = dispatches.snapshot()[0][row["id"]]
        gone = os.path.join(self.tmp, "vanished", ".git")
        # seat-b is ROSTERED with no session so it reads positively ABSENT.
        # Left out of the roster it is UNMEASURABLE, the belt refuses first,
        # and git is never spawned — the arm would then assert about a probe
        # that never ran.
        roster = self.roster({"seat-a": "s", "seat-b": None})
        panes = self.panes("seat-a")
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch, mock.patch.object(landreq, "_git",
                                                 return_value=None) as spawn:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_repo_unreadable(
                dict(fresh, repo_id=gone), ctx)
        self.assertTrue(spawn.called, "the double must be what git ran through")
        self.assertIsNone(measurement)
        self.assertIn("did not RUN", refusal)

    def test_a_git_that_did_NOT_RUN_refuses_succession_unreadable(self):
        """Same law on the carrier probe, and it needs its own arm: the two
        measurements call git separately and a cure applied to one of them is
        exactly how the other keeps the defect."""
        carrier = self.verdicted(polarity="fix", author="seat-b",
                                 lane="lane/carrier")
        child = self.open_row(author="seat-b", lane="lane/child",
                              supersedes=carrier["id"])
        fresh = dispatches.snapshot()[0][child["id"]]
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s", "seat-b": None}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch, mock.patch.object(landreq, "_git",
                                                 return_value=None) as spawn:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            measurement, refusal = landreq._measure_succession_unreadable(
                fresh, ctx)
        self.assertTrue(spawn.called, "the double must be what git ran through")
        self.assertIsNone(measurement)
        self.assertIn("did not RUN", refusal)

    def test_the_locked_write_re_reads_reachability_not_the_door_reading(self):
        """MEASURED-EARLY-USED-LATE, closed at the moment of action.

        A sweep reads the roster and the process table ONCE so a hundred
        classifications cannot straddle a roster rewrite. If the WRITE then
        re-ran its measurement against that same cached reading, a seat that
        came back between the classification and the append would be written
        off on evidence taken before it did — and on a sweep of thirty rows
        that reading is minutes old by the end. The door context is therefore
        NOT what the locked probe uses.

        THE ARM IS THE DISAGREEMENT: the door is handed a world where the
        owing seat is gone, the LIVE instruments say it is back, and the only
        thing that can produce a refusal is the write re-reading them.
        """
        row = self.verdicted(polarity="fix", author="seat-b")
        # seat-b is ROSTERED here with no session and a positively-ABSENT
        # presence row: that is what "retirable" now means. An `agents`-and-
        # `roster`-only bundle would leave presence UNPROBED, the real
        # projection would be read, and this would classify UNKNOWN — the
        # fixture would be measuring production instead of its own world.
        stale = {"snapshot": dispatches.snapshot()[0],
                 "reach": {"roster": self.roster({"seat-a": "s"}),
                           "roster_failed": False,
                           "presence": [{"seat": "seat-b",
                                         "presence": "absent"},
                                        {"seat": "ghost-reviewer",
                                         "presence": "absent"}],
                           "agents": self.panes("seat-a")}}
        # A HAND-BUILT BUNDLE GETS NO DEAD-SEAT ROLL, so every party on the
        # row has to be described here — the RECIPIENT too. Left out, it is
        # unrostered, reads UNMEASURABLE, and the door refuses for a reason
        # this arm is not about.
        stale["reach"]["roster"]["seat-b"] = {}
        stale["reach"]["roster"]["ghost-reviewer"] = {}
        # NOR DOES IT GET AN AUTHORSHIP INDEX, and the same sentence applies to
        # the new instrument: the rostered permit path now dates the absence,
        # so an omitted `activity` sends `seat_reach` to the REAL dispatch
        # ledger about these fixture seats and classifies the DOOR world
        # UNKNOWN — the arm's premise assertion below, not its outcome, is
        # what catches that. Both parties are silent past the window and a
        # decoy keeps the index current, which is the world "retirable" means.
        stale["reach"]["activity"] = _activity_from(
            *_acts(("seat-b", "send", 30), ("ghost-reviewer", "send", 30),
                   ("seat-a", "send", 0)))
        self.assertEqual(landreq.seat_reach("seat-b", **stale["reach"])[0],
                         landreq.SEAT_ABSENT,
                         "the door world must classify this row as retirable")
        live_roster = self.roster({"seat-a": "s", "seat-b": "came-back"})
        self.assert_reach("seat-b", landreq.SEAT_LIVE, live_roster,
                          self.panes("seat-a"))
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=live_roster,
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "author-unresolvable",
                                      seat="integrator", ctx=stale)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("REACHABLE", why)

    def test_retirement_refuses_over_a_standing_close(self):
        row = self.verdicted(polarity="fix", author="deleted-author")
        out, err = dispatches._record_abandon_proven(
            row["id"], self.side, "evidence destroyed",
            lambda *_a, **_k: False,
            lambda *_a, **_k: {"mention": {"state": "none"},
                               "branch_state": "none",
                               "worktree_state": "none"})
        self.assertIsNone(err, err)
        self.assertTrue(out["abandoned"])
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            _out, why = landreq.retire(row["id"], "author-unresolvable",
                                       seat="integrator")
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("already retired by abandon", why)

    def test_a_close_refuses_over_a_standing_retirement(self):  # noqa: VACUOUS_ASSERTION — retired_row asserts the retirement positively before this arm runs, and the assertIn names the standing terminal
        """THE OTHER DIRECTION, and the one that matters more: a retired row
        must not be re-closed as LANDED by a later ladder."""
        row = self.retired_row()
        before = self.ledger_state(row["id"])
        _out, why = landreq.close(row["id"], "withdrawn", evidence="x")
        self.assertIsNotNone(why)
        self.assertEqual(self.ledger_state(row["id"]), before)
        self.assertIn("retire --reason author-unresolvable", why)


class NeverLandedTest(RetireBase):
    """A retirement claims NOTHING about the work — and every consumer of the
    landed/closed states is armed here, one arm per seam."""

    def setUp(self):
        super().setUp()
        self.row = self.retired_row()

    def test_the_filed_strip_counts_it_CLOSED_not_LANDED(self):
        """THE SEAM THAT WOULD HAVE LIED. `filed_split` bucketed on
        `lr["landed"] or state == LANDED or close_reason == landed`, and a
        retired row whose tip Git happens to observe on trunk satisfies the
        first clause. The owner reads that strip as work that shipped."""
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        lr = lrs[self.row["id"]]
        # MERGE THE REVIEWED TIP so the git leg really answers landed — the
        # arm is worthless against a row Git calls absent anyway.
        self.git("merge", "-q", "--no-ff", "-m", "carry", self.side)
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        lr = lrs[self.row["id"]]
        self.assertTrue(lr["landed"], "the fixture must really read landed")
        self.assertTrue(lr["retired_admin"])
        filed = landreq.filed_split(lrs, raw)
        self.assertEqual(
            filed["landed"], 0,
            "a retirement was counted as a landing: %r" % (filed,))
        self.assertGreaterEqual(filed["closed"], 1)

    def test_it_is_not_in_flight_and_not_a_loop(self):
        rows, unavailable = landreq.loops()
        self.assertIsNone(unavailable, unavailable)
        self.assertNotIn(self.row["id"], [r["id"] for r in rows])
        allrows, _un = landreq.loops(True)
        ids = [r["id"] for r in allrows]
        self.assertIn(self.row["id"], ids)
        live = landreq.inflight_rows(allrows)
        self.assertNotIn(self.row["id"], [r["id"] for r in live])

    def test_it_bills_no_stall_and_no_unmeasurable_dwell(self):  # noqa: VACUOUS_ASSERTION — the paired positive is test_it_is_not_in_flight_and_not_a_loop, which asserts the row IS present under --all on the same projection
        stalls, unavailable = landreq.stalls()
        self.assertIsNone(unavailable, unavailable)
        self.assertNotIn(self.row["id"], [r["id"] for r in stalls])
        unmeasured, unavailable = landreq.unmeasurable()
        self.assertIsNone(unavailable, unavailable)
        self.assertNotIn(self.row["id"], [r["id"] for r, _why in unmeasured])

    def test_it_carries_no_debt_forward_for_a_predecessor(self):
        """`_absorbs_debt` — a retirement is helm giving up on READING the
        row, never the row handing its work to a successor. Same law as
        ABANDONED."""
        lrs, _raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        kid = lrs[self.row["id"]]
        self.assertTrue(kid["terminal"])
        self.assertFalse(landreq._absorbs_debt(kid))

    def test_the_land_nudge_names_the_terminal_not_READY(self):
        lr, err = landreq.get(self.row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(landreq.land_instruction_word(lr), "RETIRED")
        self.assertEqual(landreq.terminal_annotation(lr)[0], "RETIRED")


class RenderTest(RetireBase):
    """The owner-visible surface. A terminal nobody can see on the board is a
    terminal that did not happen."""

    def test_lr_list_renders_the_ROW_and_counts_it(self):
        """ASSERT THE ROW, NOT THE LEGEND. A header that says "1
        ADMINISTRATIVELY RETIRED" while printing no such row is a legend, and
        a legend is exactly what a hand-written count line degrades into."""
        row = self.retired_row()
        rc, out, err = run(["list", "--all", "--cold"])
        self.assertEqual(rc, 0, err)
        line = [ln for ln in out.splitlines()
                if ln.strip().startswith(row["id"][:12])]
        self.assertEqual(len(line), 1, out)
        self.assertIn("RETIRED", line[0])
        self.assertIn("ADMINISTRATIVELY RETIRED", out)
        self.assertRegex(out, r"1 of them ADMINISTRATIVELY RETIRED")

    def test_the_count_line_is_absent_when_no_row_carries_it(self):  # noqa: VACUOUS_ASSERTION — this IS the control for test_lr_list_renders_the_ROW_and_counts_it, which asserts the same phrase present
        """THE CONTROL for the arm above: without it, a header that printed
        the phrase unconditionally would pass."""
        self.verdicted(polarity="fix", author="deleted-author")
        rc, out, err = run(["list", "--all", "--cold"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("ADMINISTRATIVELY RETIRED", out)

    def test_show_states_the_measurement_and_the_actor(self):
        row = self.retired_row()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        shown = landreq._render_show(lr)
        self.assertIn("RETIRED", shown)
        self.assertIn("deleted-author", lr["retire_measurement"])

    def test_the_console_card_carries_the_retirement(self):
        row = self.retired_row()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        card = landreq.card(lr)
        self.assertTrue(card["retired_admin"])
        self.assertEqual(card["retire_reason"], "author-unresolvable")
        self.assertEqual(card["retire_seat"], "integrator")
        self.assertIn("deleted-author", card["retire_measurement"])
        json.dumps(card)                      # the wire must encode


class ReplayTest(RetireBase):
    """The ledger is the record; a projection is a reading of it."""

    def test_a_cold_replay_preserves_the_retirement(self):
        row = self.retired_row()
        events = self.history(row["id"])
        self.assertEqual(len([e for e in events
                              if e.get("event") == "retire"]), 1)
        # A SECOND, INDEPENDENT FOLD of the same bytes.
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        fresh = snap[row["id"]]
        self.assertTrue(fresh["retired_admin"])
        self.assertEqual(fresh["retire_reason"], "author-unresolvable")
        self.assertEqual(fresh["retire_proof_version"],
                         dispatches._RETIRE_PROOF_V)

    def test_a_retire_event_with_a_foreign_reason_does_not_replay(self):
        """MUTATE WHAT THE REAL WRITER WROTE, never a hand-built event: a
        hand-built one proves only that the validator refuses a shape the
        test author invented."""
        row = self.retired_row()
        event = [e for e in self.history(row["id"])
                 if e.get("event") == "retire"][0]
        other = self.verdicted(polarity="fix", author="deleted-author",
                               lane="lane/second")
        forged = dict(event, id=other["id"],
                      seq=other["seq"] + 1,
                      retire_reason=dispatches.RETIRE_REASONS[0] + "-x")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertFalse(snap[other["id"]].get("retired_admin"),
                         "an unknown reason code REPLAYED")

    def test_a_retire_event_whose_seat_is_not_a_token_does_not_replay(self):
        """THE WRITER AND THE REPLAY ADMIT THE SAME SHAPES.

        `_record_retire_proven` requires the acting seat to match
        `dispatches._TOKEN`; the replay arm originally checked only that it
        was non-empty. That is the harmless DIRECTION of the abandon
        divergence — the ledger cannot grow an event the machine ignores —
        but it is a divergence all the same, and a retirement whose actor a
        reader cannot resolve to a seat has an unfollowable audit trail.
        """
        row = self.retired_row()
        event = [e for e in self.history(row["id"])
                 if e.get("event") == "retire"][0]
        other = self.verdicted(polarity="fix", author="seat-b",
                               lane="lane/fourth")
        bad = "not a seat name"
        self.assertIsNone(dispatches._TOKEN.fullmatch(bad),
                          "the fixture must really be outside the token shape")
        forged = dict(event, id=other["id"], seq=other["seq"] + 1,
                      retire_seat=bad)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertFalse(snap[other["id"]].get("retired_admin"),
                         "a non-token acting seat REPLAYED")

    def test_a_retire_event_with_no_measurement_does_not_replay(self):
        row = self.retired_row()
        event = [e for e in self.history(row["id"])
                 if e.get("event") == "retire"][0]
        other = self.verdicted(polarity="fix", author="deleted-author",
                               lane="lane/third")
        forged = dict(event, id=other["id"], seq=other["seq"] + 1,
                      retire_measurement="")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), forged))
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, unavailable)
        self.assertFalse(snap[other["id"]].get("retired_admin"),
                         "a measurement-less retirement REPLAYED")

    def test_a_retry_under_the_same_reason_appends_nothing(self):  # noqa: VACUOUS_ASSERTION — assertTrue(out[retired_admin]) is the unconditional positive on the same row the count assertion covers
        row = self.retired_row()
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "author-unresolvable",
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(self.ledger_state(row["id"]), before)

    def test_a_retry_under_a_DIFFERENT_reason_refuses(self):  # noqa: VACUOUS_ASSERTION — retired_row asserts the standing retirement positively, and the unchanged pair includes a retire-event count of exactly 1
        row = self.retired_row()
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], "repo-unreadable",
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)


class SweepTest(RetireBase):
    """`--sweep` classifies; `--dry-run` writes nothing."""

    def test_a_dry_run_plans_and_appends_nothing(self):  # noqa: VACUOUS_ASSERTION — the plan list is asserted EQUAL to the real row id and would_clear == 1 — unconditional positives beside the unchanged ledger
        row = self.verdicted(polarity="fix", author="deleted-author")
        self.age(row["id"], 20 * 86400)
        before = len(self.history())
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=True,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual([e["id"] for e in report["plan"]], [row["id"]])
        self.assertEqual(report["retired"], [])
        self.assertEqual(report["would_clear"], 1)
        self.assertEqual(len(self.history()), before)

    def test_the_sweep_writes_when_it_is_not_a_rehearsal(self):
        row = self.verdicted(polarity="fix", author="deleted-author")
        self.age(row["id"], 20 * 86400)
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=False,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual([e["id"] for e in report["retired"]], [row["id"]])
        self.assertEqual(len(self.retire_events(row["id"])), 1)

    def test_a_row_younger_than_the_bar_is_not_considered(self):
        row = self.verdicted(polarity="fix", author="deleted-author")
        self.age(row["id"], 2 * 86400)
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=True,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(report["plan"], [])
        self.assertGreaterEqual(report["billing"], 1)
        self.assertEqual(report["considered"], 0)

    def test_a_live_TLA_owner_is_SKIPPED_and_named(self):
        """These are somebody's current obligations, not cruft — and the
        sweep must SAY it skipped them rather than silently not clearing
        them."""
        # THE LIVE SET IS THE ROSTER, not a hand-written roll — so the seat
        # this arm skips has to be IN the roster it installs. Naming a
        # constant here would re-create the rival static truth the cure
        # removed, and the arm would pass against a roll that no longer
        # matches who is actually alive.
        tla = "a-currently-rostered-seat"
        row = self.verdicted(polarity="fix", author=tla)
        self.age(row["id"], 30 * 86400)
        r_patch, a_patch = self.reach(
            roster=self.roster({"seat-a": "s", tla: "session-live"}),
            panes=self.panes("seat-a"))
        with r_patch, a_patch:
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=True,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual([e["id"] for e in report["skipped_live_tla"]],
                         [row["id"]])
        self.assertEqual(report["plan"], [])

    def test_a_refused_row_is_reported_with_every_refusal(self):
        """THE REFUSALS ARE THE PRODUCT. A sweep that printed only its
        clearances would be indistinguishable from one whose measurements
        were broken."""
        row = self.verdicted(polarity="fix", author="unrostered-author")
        self.age(row["id"], 30 * 86400)
        # A REFUSED ROW IS NOT A SKIPPED ROW, and the difference is the whole
        # point of this arm. A row whose owner is measurably LIVE is SKIPPED
        # outright — it lands in skipped_live_tla, never reaching the reasons,
        # so it produces no refusals to report. To exercise the refusal path
        # the owner must be NOT-LIVE but also NOT PROVEN GONE: unrostered
        # reads UNKNOWN, the sweep considers the row, and every reason then
        # refuses on the belt. That is a considered-and-refused row, which is
        # what "the refusals are the product" means.
        r_patch, a_patch = self.reach(
            roster=self.roster({"seat-a": "s"}),
            presence=[{"seat": "seat-a", "presence": "fresh"}],
            panes=self.panes("seat-a"))
        with r_patch, a_patch:
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=True,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual([e["id"] for e in report["refused"]], [row["id"]])
        named = {r["reason"] for r in report["refused"][0]["refusals"]}
        self.assertEqual(named, set(dispatches.RETIRE_REASONS))


class CliTest(RetireBase):
    def test_the_verb_is_reachable_and_dry_runs_without_writing(self):  # noqa: VACUOUS_ASSERTION — rc == 0 and two assertIn on the printed plan are unconditional positives
        row = self.verdicted(polarity="fix", author="deleted-author")
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=self.roster({"seat-a": "s"}),
                                      panes=self.panes("seat-a"))
        with r_patch, a_patch:
            rc, out, err = run(["retire", row["id"][:12], "--reason",
                                "author-unresolvable", "--dry-run",
                                "--seat", "integrator"])
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run", out)
        self.assertIn("deleted-author", out)
        self.assertEqual(self.ledger_state(row["id"]), before)

    def test_an_unknown_reason_is_a_usage_error(self):
        row = self.verdicted(polarity="fix", author="deleted-author")
        rc, _out, err = run(["retire", row["id"][:12], "--reason", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown --reason", err)

    def test_usage_names_the_verb(self):
        self.assertIn("retire", landreq.USAGE)
        for reason in dispatches.RETIRE_REASONS:
            self.assertIn(reason, landreq.USAGE)



class LedgerActorIndexTest(RetireBase):
    """The authorship index, measured against the events the PRODUCERS write.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the index was derived from the send
    API rather than from the ledger, so it read a `verdict` at top-level
    `recipient` — a key the verdict writer does not emit — and read a MOVE at
    `sender`, which that writer deliberately fills with the name it INHERITED.
    A reviewer whose recent work is verdicts, and a seat that took over
    somebody else's obligation, both read as silent. Those are precisely the
    seats the silence rung is supposed to protect, and silence is what spends
    their rows.

    EVERY ARM DRIVES `dispatches.seat_activity` ITSELF over producer-shaped
    events. An arm that hands `seat_reach` a hand-typed reading would agree with
    whatever the arm's author believed the shape was, which is the defect rather
    than a test of it.

    AND THE EVENTS ARE NOW A VALID LEDGER PREFIX, which the first spelling of
    these arms was not. Attribution is computed by the FOLD, so an event the
    fold refuses credits nobody — and the table's rows share obligation ids and
    carry sequence numbers no opener leads to, which the raw walk this replaced
    could not notice. `_acts` builds the prefix; `_rid` says why each act needs
    its own obligation.
    """

    def test_a_kind_that_records_NO_hand_credits_nobody_in_EITHER_store(self):  # noqa: VACUOUS_ASSERTION — both subtractions are covered UNCONDITIONALLY on the same locals by assertTrue(credited) and assertTrue(doubted) right above them, and the method scans CLEAN under _analyze_source in isolation; the finding appears only in whole-file context, where the rung cannot pair a local unpacked from a module-level helper call
        """THE MUST-MISS, PER KIND, AND ON BOTH STORES.

        A kind the registry gives an empty tuple records no hand BY DESIGN, so
        it is doubt about nobody as much as it is credit to nobody. That second
        half is the one with teeth: reading such a row as an unattributable act
        by the seat its obligation names would put doubt on every seat in the
        fleet forever — 1320 of the live ledger's refused rows are `superseded`
        annotations — and the door would refuse everything.

        Each row is fed as a VALID PREFIX (its own obligation, opened by
        `asking-seat`), so the only names either store may carry are that opener
        and the row's own hand.
        """
        # THE POSITIVE CONTROLS FIRST AND UNCONDITIONALLY, both of them, on the
        # two expressions the loop below asserts are EMPTY. One prefix whose
        # follow-on records a hand fills BOTH stores — the opener is credited
        # and the unbindable retirement-shaped hand is doubted — so a reader
        # that names nobody at all fails here rather than passing the loop.
        index, doubt, _newest, err = _activity_from(
            *_prefix_for(_one_shape("retire", ts=_ago(9),
                                    retire_proof_version=0)))
        self.assertIsNone(err, err)
        self.assertEqual(sorted(index), ["asking-seat"],
                         "the prefix builder's own opener is uncredited, so "
                         "every subtraction below removes nothing")
        self.assertEqual(sorted(doubt), ["retiring-seat"],
                         "a hand-bearing follow-on the fold refuses produced NO "
                         "doubt, so the doubt assertions below are vacuous")
        credited, doubted, reasons = set(), set(), []
        for why, who, event in _REAL_EVENT_SHAPES:
            if who or event.get("event") == "dispatch":
                continue
            index, doubt, _newest, err = _activity_from(
                *_prefix_for(event, ts=_ago(9)))
            self.assertIsNone(err, err)
            credited |= set(index)
            doubted |= set(doubt)
            reasons.append(why)
        # ACCUMULATED AND ASSERTED AFTER THE LOOP, so nothing here is behind a
        # branch: the two positives say the readings HAPPENED and the two
        # subtractions say what they may not contain.
        self.assertTrue(reasons, "no hand-less kind was read at all")
        self.assertTrue(credited,
                        "the readings credited nobody at all, not even the "
                        "prefix's own opener, so the subtraction below removes "
                        "nothing")
        self.assertEqual(sorted(credited - {"asking-seat"}), [],
                         "a kind that records no hand CREDITED a seat: %s"
                         % "; ".join(reasons))
        self.assertTrue(doubted,
                        "the readings produced no doubt at all, so the "
                        "subtraction below removes nothing")
        self.assertEqual(sorted(doubted - {"legacy-reviewer"}), [],
                         "a kind that records no hand put DOUBT on a seat: %s"
                         % "; ".join(reasons))

    def test_every_hand_the_producers_record_is_credited_from_a_VALID_prefix(self):  # noqa: VACUOUS_ASSERTION — both stores are asserted to exact non-empty name lists and `newest` to an exact stamp
        """THE MUST-HIT, per kind that DOES record a hand, over a prefix the
        fold accepts: a send, a move, and a verdict whose binding the shipped
        validator admits. `asking-seat` rides along because it really did open
        the verdict's obligation — a fact of the world, not of the arm."""
        index, doubt, newest, err = _activity_from(
            *_acts(("sending-seat", "send", 9), ("moving-seat", "move", 9),
                   ("reviewing-seat", "verdict", 9)))
        self.assertIsNone(err, err)
        self.assertEqual(sorted(index),
                         ["asking-seat", "moving-seat", "reviewing-seat",
                          "sending-seat"],
                         "a hand its own producer records was not credited")
        self.assertEqual(doubt, {},
                         "a prefix the fold accepts in full still produced "
                         "doubt, so the two stores are not exclusive")
        self.assertEqual(newest, _ago(9),
                         "the reading's newest authorship is not the stamp the "
                         "fixture wrote, so it read some other corpus")

    def test_a_close_does_not_credit_the_seat_whose_row_it_closed(self):
        """A SWEEP IS ONE HAND CLOSING MANY PEOPLE'S ROWS. Crediting the
        mentioned party would report every author of every swept row as active
        on the day of the sweep — the exact reading the index's own contract
        forbids, and the reason a row-STATE reading cannot answer this."""
        # TWO CLOSES, EACH AS ITS OWN WRITER EMITS IT, because the hazard only
        # exists on the one that records NO hand: no close event on the live
        # ledger carries both an actor and a mentioned party, so a single
        # fixture carrying both would be a world the producer never wrote and
        # the actor's precedence would mask the mentioned party anyway.
        swept = _shape_where("close", "original_author", ts=_ago(9))
        acted = _shape_where("close", "close_actor", ts=_ago(9))
        index, doubt, _newest, err = _activity_from(
            *(_prefix_for(swept) + _prefix_for(acted)))
        self.assertIsNone(err, err)
        # THE DISCRIMINATION, ON WHICHEVER STORE THE ACT LANDS IN. Neither
        # close is a transition this prefix's obligation can take — a landed
        # close proves itself against a repo these events do not have — so the
        # hand-bearing one is DOUBT about `closing-seat`. What matters is the
        # same either way: the hand appears and the mentioned party does not.
        self.assertIn("closing-seat", set(index) | set(doubt),
                      "the actor-bearing close named no hand at all, so the "
                      "absences below are a reader that reads no close")
        for mentioned in ("mentioned-seat", "mentioned-reviewer"):
            self.assertNotIn(mentioned, index,
                             "the seat whose row was closed was CREDITED with "
                             "the closing")
            self.assertNotIn(mentioned, doubt,
                             "the seat whose row was closed had DOUBT put on "
                             "it by somebody else's sweep")

    def test_a_MOVE_credits_the_acting_hand_and_not_the_inherited_sender(self):
        """The move writer says it in as many words: a row carrying `acted_by`
        is a row whose `sender` was INHERITED. Crediting both would credit a
        seat that did not write the row."""
        index, _doubt, _newest, err = _activity_from(
            *_acts(("moving-seat", "move", 9)))
        self.assertIsNone(err, err)
        self.assertEqual(sorted(index), ["moving-seat"],
                         "a move credited the inherited sender")
        self.assertNotIn("inherited-sender", index)
        # THE CONTROL ON THE SAME PATH: with no `acted_by` the sender IS the
        # hand, so the precedence is a precedence and not a dropped field.
        index, _doubt, _newest, err = _activity_from(
            *_acts(("sender-seat", "send", 9)))
        self.assertIsNone(err, err)
        self.assertEqual(sorted(index), ["sender-seat"],
                         "an ordinary dispatch lost its sender")

    def test_the_excluded_fields_are_absent_from_the_actor_table(self):
        """`_NOT_AN_ACTOR` is the mentioned-party law as DATA, and this is what
        makes it load-bearing: every seat-shaped field the exclusions name is a
        field a future reader could widen the table with, and the reason has to
        be in their way rather than in a comment somewhere else."""
        self.assertTrue(dispatches.LEDGER_NOT_AN_ACTOR,
                        "the exclusion table is empty, so this arm proves "
                        "nothing about any field")
        for (kind, field), why in dispatches.LEDGER_NOT_AN_ACTOR.items():
            with self.subTest(kind=kind, field=field):
                self.assertTrue(why.strip(),
                                "an exclusion with no reason is a bare "
                                "denylist entry")
                fields = dispatches.LEDGER_EVENT_ACTORS.get(kind, ())
                self.assertNotIn(field, fields,
                                 "%s.%s is excluded AND read: %s"
                                 % (kind, field, why))
        # THE MUST-HIT on the same comparison: a field that IS read is found by
        # it, so the assertNotIn above is a discrimination.
        self.assertIn("sender", dispatches.LEDGER_EVENT_ACTORS["dispatch"],
                      "the comparison cannot see a field that IS read, so "
                      "every exclusion above passed vacuously")

    def test_the_shape_table_covers_every_field_the_registry_reads(self):
        """A FIELD WITH NO SHAPE IS AN UNTESTED READING. The corpus above is
        the only description of the producers this suite has, so a field added
        to the registry without an event to exercise it must be a red line here
        rather than a green suite."""
        exercised = set()
        for _why, _who, event in _REAL_EVENT_SHAPES:
            kind = event.get("event")
            for field in dispatches.LEDGER_EVENT_ACTORS.get(kind, ()):
                if event.get(field):
                    exercised.add((kind, field))
        declared = {(kind, field)
                    for kind, fields in dispatches.LEDGER_EVENT_ACTORS.items()
                    for field in fields}
        self.assertTrue(declared, "the actor registry reads no field at all")
        self.assertEqual(sorted(declared - exercised), [],
                         "the registry reads a field no producer-shaped event "
                         "in this corpus carries")


class AnUnrosteredReviewerWhoJustVerdictedIsACTINGTest(RetireBase):
    """THE DISCRIMINATING CASE, and the one the door gets wrong when the index
    reads a field the writer does not emit.

    The seat is unrostered, so every roster-keyed instrument misses it and the
    authorship index is the only positive evidence left. Its last SEND is a
    month old and it recorded a VERDICT today. That is a seat at work. Under an
    index that could not see a verdict, the same world read a month of silence
    and the door permitted an irreversible terminal on its rows.
    """

    SEAT = "unrostered-reviewer"
    #: The row's OTHER party. `author-unresolvable` measures every party of the
    #: chain, so a reviewer left undescribed refuses for a reason these arms
    #: are not about — it must be positively gone, which under the ratified
    #: semantics means rostered, session-less, presence-ABSENT and long silent.
    OTHER = "ghost-reviewer"

    def _world(self):
        """A live fleet that does not contain this seat: the projection
        describes a live seat, a pane declares one, and neither is it — so the
        only rung left to answer for THIS seat is the measured silence."""
        return dict(roster={"decoy-seat": {"session": "s"}, self.OTHER: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"},
                              {"seat": self.OTHER, "presence": "absent"}])

    def _activity(self, with_verdict):
        """An old send by this seat, a verdict TODAY by it when asked, and a
        decoy authoring today so the index is CURRENT either way.

        THE CURRENCY DECOY IS NOT DECORATION: without it, dropping the verdict
        also makes the index stale and the refusal would come from the wrong
        rung — the arms would agree on the outcome and disagree about why.
        """
        declared = [(self.SEAT, "send", 30), (self.OTHER, "send", 30),
                    ("decoy-seat", "send", 0)]
        if with_verdict:
            # THE OBLIGATION RIDES WITH THE VERDICT because the projection
            # validates the binding against the seat the row was ADDRESSED to,
            # and `_acts` builds both: the envelope that credits this seat is
            # the one `dispatches._verdict_author_runtime_error` accepts, on an
            # obligation the fold has actually opened.
            declared.append((self.SEAT, "verdict", 0))
        return _activity_from(*_acts(*declared))

    def test_the_index_sees_the_verdict_the_seat_recorded_today(self):  # noqa: VACUOUS_ASSERTION — both readings assert an EXACT stamp (index.get(SEAT) == _ago(0), newest == _ago(0)), and the verdict-removed control asserts _ago(30) on the same observable
        """THE PREMISE, PINNED BEFORE THE DOOR IS ASKED. An arm that only
        asserted the verb's outcome could not tell a correct refusal from a
        fixture that fed it the wrong index."""
        index, _doubt, newest, err = self._activity(with_verdict=True)
        self.assertIsNone(err, err)
        self.assertEqual(index.get(self.SEAT), _ago(0),
                         "the verdict this seat recorded today is invisible to "
                         "the index, so its month-old send is the only thing "
                         "the door will see")
        self.assertEqual(newest, _ago(0))
        # THE CONTROL: with the verdict removed the SAME fixture reads a month
        # of silence, which is what makes the reading above a measurement of
        # the verdict rather than of the decoy.
        quiet, _doubt, _newest, err = self._activity(with_verdict=False)
        self.assertIsNone(err, err)
        self.assertEqual(quiet.get(self.SEAT), _ago(30))

    def test_a_verdict_today_reads_ACTING_where_the_old_index_read_ABSENT(self):
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(with_verdict=True),
            **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertNotEqual(state, landreq.SEAT_ABSENT,
                            "a seat that recorded a verdict today reached the "
                            "permit state")
        self.assertIn("ACTING", why,
                      "the refusal does not name the silence rung, so an "
                      "operator cannot tell which instrument saved this seat")
        # THE POSITIVE CONTROL ON THE SAME DOOR: strip only the verdict and
        # this identical world PERMITS, so the refusal above is the verdict's
        # doing and not a door that refuses everything.
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(with_verdict=False),
            **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)

    def test_the_retire_dry_run_on_such_a_row_REFUSES_naming_the_rung(self):
        """THROUGH THE REAL VERB, because the index is only worth what the door
        does with it."""
        row = self.verdicted(polarity="fix", author=self.SEAT,
                             reviewer=self.OTHER)
        world = self._world()
        before = self.ledger_state(row["id"])
        ctx = {"snapshot": dispatches.snapshot()[0],
               "reach": dict(world,
                             activity=self._activity(with_verdict=True))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=ctx)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("ACTING", why)
        # THE POSITIVE CONTROL: the same row, the same world, the verdict
        # removed — the door admits, so this reason is not simply unreachable.
        quiet = {"snapshot": dispatches.snapshot()[0],
                 "reach": dict(world,
                               activity=self._activity(with_verdict=False))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=quiet)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"],
                        "the positive control did not admit, so the refusal "
                        "above says nothing about the verdict")


if __name__ == "__main__":
    unittest.main()


class EveryPartyOnTheRowRefusesTest(RetireBase):
    """RETIREMENT REFUSES WHILE ANY PARTY ON THE ROW IS STILL REACHABLE.

    FAILURE: `retire()`'s real door returned would_append=True for a row with
    a LIVE sender AND a LIVE recipient — it would have retired work out from
    under two seats both able to move it.

    Traced: the guard asked `_retire_owing_seat`, which resolves an OPEN row's
    role to `integrator` — a role that names no field — so the seat came back
    None and the guard passed. `_measure_repo_unreadable` then admitted on
    `if not gitdir` without ever asking who was alive. Two of the four reason
    classifiers never consult liveness at all, so the rule belongs at the one
    door all four already call.

    A chain is unreachable only when NO party can act. These arms pin both
    directions, because a guard that refuses everything is as broken as one
    that refuses nothing.
    """

    ROW = {"id": "aaaaaaaaaaaa", "status": "open", "delivery": "pending",
           "sender": "dead-author", "recipient": "live-reviewer",
           "repo_id": ""}

    def _ctx(self, roster, panes):
        return {"reach": {}}, self.reach(roster=roster, panes=panes)

    def test_a_LIVE_recipient_refuses_a_row_whose_SENDER_is_gone(self):
        """THE HOSTILE ARM. Dead on one side is not unreachable."""
        roster = self.roster({"live-reviewer": "session-xyz"})
        panes = self.panes()
        # PIN THE INSTRUMENT FIRST: the arm is only about the guard if the
        # world it reads is the world the fixture meant to build.
        self.assert_reach("live-reviewer", landreq.SEAT_LIVE, roster, panes)
        self.assert_reach("dead-author", landreq.SEAT_ABSENT, roster, panes)

        ctx, (r_patch, a_patch) = self._ctx(roster, panes)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
        self.assertTrue(
            refusal,
            "a row with a LIVE recipient was admitted for retirement because "
            "its SENDER was gone — this door retires the unreachable, never "
            "the merely-orphaned-on-one-side")
        self.assertIn("live-reviewer", refusal,
                      "the refusal must NAME the party that still can act")

    def test_a_row_NOBODY_can_act_on_still_retires(self):
        """THE MUST-MISS. Without it the arm above passes for a guard that
        refuses every row, which would close the door entirely."""
        # A DECOY ENTRY, BECAUSE AN EMPTY ROSTER IS NOT AN EMPTY FLEET.
        # `seat_reach`'s own docstring: "UNKNOWN IS A REAL ANSWER AND EVERY
        # BLIND PATH LANDS ON IT: an unreadable roster, an EMPTY roster (a
        # whole fleet that is down is not a fleet of unnamed seats)". So
        # `self.roster({})` reads UNKNOWN, and the guard REFUSES on UNKNOWN —
        # correctly, since retirement must never act on an unmeasured chain.
        # To build "measurably nobody", the roster has to EXIST and not
        # contain these seats. Asserting UNNAMED against an EMPTY roster is
        # the trap here, and the fixture-pinning convention is what catches
        # it — which is exactly what that convention is for.
        # "MEASURABLY NOBODY" IS NOW A ROSTERED-AND-ABSENT FACT. Leaving a
        # party out of the roster no longer builds that world — it builds an
        # UNMEASURABLE one, which refuses. Both parties are rostered here with
        # no session, and the projection reports them absent.
        roster = self.roster({"someone-else": "session-zzz",
                              "live-reviewer": None})
        panes = self.panes()
        self.assert_reach("dead-author", landreq.SEAT_ABSENT, roster, panes)
        self.assert_reach("live-reviewer", landreq.SEAT_ABSENT, roster, panes)

        ctx, (r_patch, a_patch) = self._ctx(roster, panes)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
            pass
        self.assertIsNone(
            refusal,
            "a row NO party can act on was refused — the door has stopped "
            "retiring the population it exists for")

        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE. The None
        # above means nothing unless this guard CAN return a refusal — a
        # guard that answered None for everything would satisfy it and close
        # nothing. Same row, same call, ONE party made reachable: it must
        # flip. A control that asserts None twice and calls itself positive
        # is the exact vacuity the rung exists to complain about.
        live_roster = self.roster({"live-reviewer": "session-xyz"})
        self.assert_reach("live-reviewer", landreq.SEAT_LIVE,
                          live_roster, panes)
        ctl_ctx, (r2, a2) = self._ctx(live_roster, panes)
        with r2, a2:
            control = landreq._retire_live_guard(dict(self.ROW), ctl_ctx)
        self.assertTrue(
            control,
            "positive control: the guard did not refuse even with a LIVE "
            "party, so it cannot refuse at all and the None above is vacuous")

    def test_a_LIVE_SENDER_refuses_too_not_only_the_recipient(self):
        """Both sides, because a loop that only ever reaches its first element
        passes the hostile arm above by accident."""
        roster = self.roster({"dead-author": "session-abc"})
        panes = self.panes()
        self.assert_reach("dead-author", landreq.SEAT_LIVE, roster, panes)

        ctx, (r_patch, a_patch) = self._ctx(roster, panes)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
        self.assertTrue(refusal, "a LIVE sender did not refuse")
        self.assertIn("dead-author", refusal)

    def test_an_UNMEASURABLE_party_refuses_rather_than_passing(self):
        """Absence of an answer is never an answer. A roster read that FAILED
        must not read as 'nobody is there'."""
        roster = self.roster({})
        panes = self.panes()
        ctx = {"reach": {}}
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      roster_failed=True)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
        self.assertTrue(
            refusal,
            "an UNMEASURED party let the row through; retirement must refuse "
            "on an unmeasured chain, never on absence")


class EveryReasonPassesTheSharedBeltTest(RetireBase):
    """EVERY REGISTERED REASON PASSES THE SAME LIVENESS BELT.

    FAILURE: the all-parties guard was wired into repo/succession only, so
    _measure_author_unresolvable and _measure_tier_unevaluable_parked reached
    the ADMIT without ever asking whether a party was still reachable.

    THE FALSE CLAIM THIS REPLACES: that the all-parties rule sits at "the
    ONE door all four classifiers already call". Only TWO called it. The other two ask `seat_reach` themselves, about the
    OWING seat or the REVIEWER, which is each reason's own question and is not
    the belt: a row whose owing seat is genuinely unresolvable can still have a
    LIVE recipient or custodian, and nothing was asking.

    THE REAL DEFECT WAS THAT THE CLAIM WAS UNPINNED. "All four" was asserted
    from a reading, not a measurement, and nothing in the suite would catch it
    drifting — so a false claim about it can stand indefinitely. This census
    is the fix: it derives the set from the AST, so a fifth reason added
    later without the belt goes red on arrival rather than inheriting the hole
    by default.
    """

    def test_every_REGISTERED_reason_is_REACHED_through_the_belt(self):
        """THE GUARANTEE MOVED, AND SO DID ITS PROOF.

        This arm used to census each registered reason for its OWN call to
        `_retire_live_guard`. That was the right guarantee while the belt was
        copied into every reason — and the copies had drifted, two guarding
        their entry and two their admit. The belt now lives in exactly one
        place, so "does each reason call it" is the WRONG question: the answer
        is no, by design, and an arm still asking it would fail a correct
        cure.

        The guarantee is now structural: every reason is DISPATCHED through
        `_measure_retire`, which applies the belt once. That is asserted by
        TheSingleMeasureRetirePathIsStructuralTest. What this arm keeps is the
        REGISTRY half — that the registry is non-empty and every entry is
        callable through the one path — so a reason registered but unreachable
        cannot hide.
        """
        registry = landreq.RETIRE_MEASUREMENTS
        self.assertTrue(registry, "the reason registry is EMPTY — every "
                                  "census over it would pass vacuously")
        for reason in registry:
            with self.subTest(reason=reason):
                self.assertTrue(
                    callable(registry[reason]),
                    "%r is registered but not callable, so the single path "
                    "cannot reach it" % reason)

    def test_the_census_would_CATCH_an_unwired_reason(self):
        """The census's own must-fail control.

        A census that cannot go red is a decoration. This registers a reason
        that does NOT call the belt and asserts the same predicate rejects it,
        so the green above means the predicate discriminates rather than that
        it always passes.
        """
        import ast as _ast
        import inspect

        def _measure_pretend_reason(row, ctx):        # no belt call
            return None, "nothing"

        tree = _ast.parse(textwrap.dedent(
            inspect.getsource(_measure_pretend_reason)))
        calls = [n for n in _ast.walk(tree.body[0])
                 if isinstance(n, _ast.Call)
                 and isinstance(n.func, _ast.Name)
                 and n.func.id == "_retire_live_guard"]
        self.assertEqual(
            calls, [],
            "the census predicate found a belt call in a function that has "
            "none — it cannot detect an unwired reason")

        # AND THE OTHER DIRECTION, unconditionally, on the same predicate: a
        # function that DOES call the belt must be found. Without this, a
        # predicate that never matches anything satisfies the assertion above
        # while being incapable of passing a real reason.
        def _measure_pretend_wired(row, ctx):
            refusal = _retire_live_guard(row, ctx)
            return None, refusal

        found = [n for n in _ast.walk(_ast.parse(textwrap.dedent(
            inspect.getsource(_measure_pretend_wired))).body[0])
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
            and n.func.id == "_retire_live_guard"]
        self.assertEqual(
            len(found), 1,
            "positive control: the predicate did not find the belt call in a "
            "function that plainly makes one, so its zero above is vacuous")


class TheReachBundleIsMeasuredOnceTest(RetireBase):
    """THE REACH BUNDLE IS MEASURED ONCE PER DOOR, NOT ONCE PER PARTY.

    Measured, and pinned here so it cannot come back.

    Widening the guard from ONE owing seat to EVERY party multiplied its cost
    by the number of parties: with an empty `reach`, each `seat_reach` re-reads
    both instruments, and `beacons.agent_index` WALKS /proc — under the
    dispatch write lock. One retirement called roster_checked and agent_index
    three times, once per party.

    The all-parties rule is right and its naive loop was not. Correctness at
    the door does not license paying for the door once per name.
    """

    ROW = {"id": "cccccccccccc", "status": "open", "delivery": "pending",
           "sender": "dead-sender", "recipient": "dead-recipient",
           "custodian": "dead-custodian", "repo_id": ""}

    def _counted(self, roster, panes, presence=()):
        """One counter per instrument IN THE PROBE TABLE, not a hand-listed
        pair.

        The previous version counted `roster` and `agents` by name. A third
        instrument was then added beside them, and this guard — whose entire
        job is to catch a per-party re-measure — could not see it. A census
        that enumerates what it already knows about cannot report on what it
        does not.

        A probe with no fixture answer FAILS here rather than being skipped,
        so adding an instrument forces a human to say what it returns.
        """
        # THE NEW INSTRUMENT'S FIXTURE ANSWER, and the assertion below is what
        # demanded it: the authorship index joined the probe table and this
        # census could count it but not stub it. It is built from
        # producer-shaped events through the shipped `seat_activity`, like
        # every other index fixture in this module, so the census counts the
        # real reader rather than a tuple somebody typed.
        answers = {"roster": (roster, False), "agents": panes,
                   "presence": list(presence),
                   "activity": _activity_from(
                       *_acts(("dead-sender", "send", 30),
                              ("someone-else", "send", 0)))}
        calls, patches = {}, []
        for key, probe_name, _fail in landreq._REACH_PROBES:
            self.assertIn(
                key, answers,
                "probe %r has no fixture answer — this census cannot count "
                "an instrument it cannot stub, and silently skipping it is "
                "how the last one escaped" % key)
            calls[key] = 0

            def make(k):
                def f():
                    calls[k] += 1
                    return answers[k]
                return f

            patches.append(mock.patch.object(landreq, probe_name, make(key)))
        return calls, patches

    def test_three_parties_read_each_instrument_ONCE(self):
        roster = self.roster({"someone-else": "session-zzz"})
        panes = self.panes()
        # MUST-HIT: the row really does carry three distinct parties, or
        # "called once" is trivially true of a row with one.
        parties = landreq._retire_all_parties(dict(self.ROW))
        self.assertEqual(len(parties), 3,
                         "fixture: expected three distinct parties, got %r"
                         % (parties,))

        calls, patches = self._counted(roster, panes)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            landreq._retire_live_guard(dict(self.ROW), {"reach": {}})
        self.assertTrue(calls, "the probe table is EMPTY — this arm counted "
                               "nothing and would pass against any code")
        self.assertGreaterEqual(
            len(calls), 3,
            "expected at least roster/agents/presence in the probe table, "
            "got %r — an instrument was removed from the table without "
            "being removed from the door" % (sorted(calls),))
        repeated = {k: n for k, n in calls.items() if n != 1}
        self.assertFalse(
            repeated,
            "the guard did not measure each instrument exactly once for a "
            "three-party row: %r. agent_index walks /proc and the presence "
            "projection reads claims and the roster — all under the dispatch "
            "write lock" % (repeated,))

    def test_a_SUPPLIED_reach_is_not_re_measured_at_all(self):
        """A sweep that already holds a measurement must not pay again."""
        roster = self.roster({"someone-else": "session-zzz"})
        panes = self.panes()
        calls, patches = self._counted(roster, panes)
        # THE SUPPLIED BUNDLE MUST CARRY EVERY INSTRUMENT. A bundle naming
        # only roster+agents leaves presence unprobed, so the door measures it
        # — which is the very re-measure this arm exists to forbid, and it
        # would pass anyway because the old counters could not see presence.
        # ... AND THE AUTHORSHIP INDEX IS NOW ONE OF THEM. The comment above
        # predicted this case in its own words and the lane that added the
        # instrument left the bundle at three, so the door measured `activity`
        # once and the count read [0, 1]. Supplied from the shipped producer,
        # like every other index fixture here.
        supplied = {"roster": roster, "roster_failed": False,
                    "agents": panes, "presence": [],
                    "activity": _activity_from(
                        *_acts(("dead-sender", "send", 30),
                               ("someone-else", "send", 0)))}
        for key, _probe, _fail in landreq._REACH_PROBES:
            self.assertIn(key, supplied,
                          "the supplied bundle omits probe %r, so the door "
                          "measures it and this arm forbids exactly that"
                          % key)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            landreq._retire_live_guard(dict(self.ROW), {"reach": supplied})
        self.assertEqual(
            sorted(calls), sorted(k for k, _n, _f in landreq._REACH_PROBES),
            "the counter does not cover every registered instrument")
        self.assertEqual(
            sorted(set(calls.values())), [0],
            # THE MESSAGE IS DERIVED FROM THE COUNTERS, not from two names.
            # Hand-listing roster and agents left a refusal that stayed silent
            # about the instrument that actually moved.
            "an explicitly supplied reach bundle was ignored and the guard "
            "measured anyway: %s"
            % ", ".join("%s x%d" % (k, n) for k, n in sorted(calls.items())))

        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME COUNTERS. (0, 0) means
        # nothing unless these counters CAN count — a patch that never took,
        # or a guard that returned before reaching any party, would satisfy
        # the assertion above while proving the opposite of what it claims.
        # Same row, same doubles, EMPTY reach: it must measure once each.
        ctl, ctl_patches = self._counted(roster, panes)
        with contextlib.ExitStack() as ctl_stack:
            for patch in ctl_patches:
                ctl_stack.enter_context(patch)
            landreq._retire_live_guard(dict(self.ROW), {"reach": {}})
        self.assertEqual(
            sorted(set(ctl.values())), [1],
            "positive control: with an EMPTY reach the guard did not "
            "measure each instrument exactly once (%r), so the zeros "
            "above are vacuous" % (ctl,))


class MovedCustodyIsConsultedTest(RetireBase):
    """MOVED CUSTODY IS CONSULTED, NOT DOCUMENTED AS A KNOWN HOLE.

    FAILURE: a row whose custody had moved retired while its custodian was
    LIVE, because only sender and recipient were ever consulted.

    `custodian` is a DURABLE field — written by `dispatches.mark_custody`,
    carried in the ledger — and only its reader `custodian_of` is unlanded
    elsewhere. So this is not the invented-field cure rejected for
    `integrator`, which named nothing anywhere; it reads a real field through
    a local helper with custodian_of's exact contract.
    """

    ROW = {"id": "bbbbbbbbbbbb", "status": "open", "delivery": "pending",
           "sender": "dead-author", "recipient": "dead-reviewer",
           "custodian": "live-custodian", "repo_id": ""}

    def test_a_LIVE_moved_custodian_refuses_when_BOTH_named_parties_are_dead(self):
        """Before this rung the row retired: sender and recipient are both
        gone, and nothing consulted custody."""
        roster = self.roster({"live-custodian": "session-cus"})
        panes = self.panes()
        self.assert_reach("live-custodian", landreq.SEAT_LIVE, roster, panes)
        self.assert_reach("dead-author", landreq.SEAT_ABSENT, roster, panes)
        self.assert_reach("dead-reviewer", landreq.SEAT_ABSENT, roster, panes)

        ctx = {"reach": {}}
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
        self.assertTrue(
            refusal,
            "a row whose custody had MOVED to a LIVE seat was admitted for "
            "retirement because its sender and recipient were both gone — "
            "the custodian owes the delivery leg and can still act")
        self.assertIn("live-custodian", refusal,
                      "the refusal must NAME the custodian")

    def test_the_same_row_with_a_DEAD_custodian_still_retires(self):
        """MUST-MISS. Without it the arm above passes for a guard that refuses
        any row carrying a custodian field at all."""
        # Decoy entry for the same reason as the sibling arm: an EMPTY roster
        # reads UNKNOWN, not UNNAMED, and the guard refuses on UNKNOWN.
        # Rostered-and-absent, not missing: a party helm has no row for is
        # UNMEASURABLE and refuses, which would make this MUST-MISS pass for
        # the wrong reason.
        roster = self.roster({"someone-else": "session-zzz",
                              "live-custodian": None})
        panes = self.panes()
        self.assert_reach("live-custodian", landreq.SEAT_ABSENT, roster, panes)

        ctx = {"reach": {}}
        r_patch, a_patch = self.reach(roster=roster, panes=panes)
        with r_patch, a_patch:
            refusal = landreq._retire_live_guard(dict(self.ROW), ctx)
        self.assertIsNone(
            refusal,
            "a row every party is gone from was refused — carrying a custody "
            "field is not itself a reason to keep a dead chain alive")

        # UNCONDITIONAL POSITIVE CONTROL: the SAME guard, the SAME row, one
        # roster entry added. It must refuse — otherwise this guard cannot
        # produce a refusal under this fixture and the None above is vacuous.
        # A FRESH CTX, and that is a property of the cure rather than a
        # nicety: the guard now STORES its reach bundle back into ctx so the
        # reasons that run after it do not re-measure. A ctx therefore belongs
        # to ONE action — reusing this one would hand the control the first
        # world's reading and it would "pass" by never looking at the second.
        live_roster = self.roster({"live-custodian": "session-cus"})
        control_ctx = {"reach": {}}
        r2, a2 = self.reach(roster=live_roster, panes=panes)
        with r2, a2:
            control = landreq._retire_live_guard(dict(self.ROW), control_ctx)
        self.assertTrue(
            control,
            "positive control: the guard did not refuse even with the "
            "custodian LIVE, so it cannot refuse at all")

    def test_custody_equal_to_sender_is_counted_ONCE(self):
        """A row that never had custody moved must behave exactly as before:
        custodian_of falls back to sender, so the two collapse."""
        parties = landreq._retire_all_parties(
            dict(self.ROW, custodian="dead-author"))
        seats_named = [seat for _role, seat in parties]
        self.assertEqual(
            len(seats_named), len(set(seats_named)),
            "the same seat was consulted twice: %r" % (parties,))
        self.assertIn("dead-author", seats_named)
        self.assertIn("dead-reviewer", seats_named)


class RetiredIsTerminalAndPassesThroughTest(RetireBase):
    """RETIRED IS TERMINAL FOR EVERY READER, AND SUCCESSION STILL PASSES
    THROUGH. These are ONE round because they share one cause.

    Retirement preserves `status` and adds `retired_admin`, so every reader
    keyed on the status word kept retired rows live. Fixing that alone makes
    a retired successor closed AND still counted as carrying, which hides its
    predecessor's debt — two correct fixes composing into a regression.
    """

    def test_a_retired_OPEN_row_is_not_an_open_obligation(self):
        from helm import query
        live = {"status": "open"}
        retired = {"status": "open", "retired_admin": True}
        # MUST-HIT: the same predicate says YES to the un-retired row, so the
        # False below is the retirement flag and not a broken predicate.
        self.assertTrue(query.query_is_open(live),
                        "must-hit: a plain OPEN row is not an obligation")
        self.assertFalse(
            query.query_is_open(retired),
            "a RETIRED row still reads as an open obligation, so owed/"
            "open_rows keep feeding wake, work-offer and stalebot with work "
            "the door already cleared")

    def test_retirement_is_terminal_for_HELD_too_not_only_OPEN(self):
        from helm import query
        self.assertTrue(query.query_is_held({"status": "held"}))
        self.assertFalse(
            query.query_is_held({"status": "held", "retired_admin": True}),
            "a retired HELD row still read as held")
        self.assertFalse(
            query.query_is_unheld_open({"status": "open",
                                        "retired_admin": True}),
            "a retired OPEN row still read as strictly open")

    def test_a_RETIRED_successor_carries_nothing_so_its_parent_stays_visible(self):
        # MUST-HIT on the same predicate: an ordinary successor DOES carry.
        self.assertFalse(
            dispatches.moved_nothing({"status": "open"}),
            "must-hit: an ordinary successor was already a pass-through, so "
            "the assertion below proves nothing about retirement")
        self.assertTrue(
            dispatches.moved_nothing({"status": "open",
                                      "retired_admin": True}),
            "a RETIRED successor was read as CARRYING its parent's "
            "obligation, which hides the parent and loses its debt")


class ThePresenceRungSeesBothFamiliesTest(RetireBase):
    """THE LIVENESS RUNG MUST SEE EVERY SEAT FAMILY.

    FAILURE: the pane rung is blind to the claude-native family, so a
    canonical presence projection has to be consulted too.

    Measured on the seat host: `beacons.agent_index()` returns 11 seats and
    EVERY one is proxy-family; `seats_report.presence_report()` returns 22 and
    includes helm-claude, hc2, helm-claude-3.

    THESE ARMS MUST NOT RUN AGAINST THE REAL PROJECTION. Doing so fails on
    fab with "reported no verified-fresh seat at all (0 rows)" — a build node
    has no live seats, so the projection is empty there. The must-hit
    caught it, which is the only reason it did not pass vacuously.

    THE GENERAL RULE, and it has cost this suite more than once: a must-hit
    seeded from the LIVE HOST comes back empty on the box the suite actually
    runs on. Seed must-hits from the fixture, never from the fleet.

    So the rung's LOGIC is tested against a controlled projection, and a
    separate arm pins that the rung reads the canonical projection at all —
    which is the part a double cannot fake and the part that would regress if
    someone swapped it back to an environ-derived census.
    """

    NATIVE = {"seat": "native-seat", "presence": "fresh",
              "runtime": {"family": "claude", "backend": "native"}}
    PROXY = {"seat": "proxy-seat", "presence": "fresh",
             "runtime": {"family": "codex", "backend": "proxy"}}
    QUIET = {"seat": "quiet-seat", "presence": "quiet",
             "runtime": {"family": "claude", "backend": "native"}}
    UNVER = {"seat": "unverified-seat", "presence": "fresh",
             "unverified": True,
             "runtime": {"family": "claude", "backend": "native"}}

    def _projection(self, rows):
        from helm import seats_report
        return mock.patch.object(seats_report, "presence_report",
                                 lambda: list(rows))

    def test_a_NATIVE_seat_reads_FRESH_which_the_pane_rung_cannot_see(self):
        """THE MUST-HIT for the whole cure. A claude-native seat is exactly
        what `agent_index` misses, so this is the case the second rung
        exists for."""
        with self._projection([self.NATIVE, self.PROXY]):
            native = landreq._presence_state("native-seat")
            proxy = landreq._presence_state("proxy-seat")
        self.assertEqual(
            native[0], landreq.SEAT_LIVE,
            "a FRESH claude-native seat was not seen by the presence rung — "
            "the family-blind hole is still open: %r" % (native,))
        self.assertIn("claude/native", native[1])
        self.assertEqual(
            proxy[0], landreq.SEAT_LIVE,
            "control: a proxy seat must still read live, or the rung has "
            "traded one blindness for another: %r" % (proxy,))

    def test_a_seat_the_projection_does_NOT_name_reads_None(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the line above the None check: `assertIsNotNone(present)` for a seat the SAME projection names, in the SAME call. A rung answering None to everything fails there first.
        """A MISS IS UNKNOWN, NEVER ABSENT — the distinction the whole
        fail-closed belt rests on.

        The projection emits one row per ROSTER seat, so silence about a seat
        is silence, not evidence it is gone. Reading a miss as absence is what
        would let an unrostered live native seat be retired.
        """
        with self._projection([self.NATIVE]):
            present = landreq._presence_state("native-seat")
            missing = landreq._presence_state("no-such-seat")
        self.assertEqual(
            present[0], landreq.SEAT_LIVE,
            "positive control: the rung reported nothing live even for a seat "
            "the projection names: %r" % (present,))
        self.assertEqual(
            missing[0], landreq.SEAT_UNKNOWN,
            "a seat the projection never named must read UNKNOWN — reading it "
            "as ABSENT would permit retiring an unrostered live seat: %r"
            % (missing,))
        self.assertNotEqual(missing[0], landreq.SEAT_ABSENT)

    def test_QUIET_is_LIVE_and_UNVERIFIED_is_UNKNOWN(self):
        """SEATED IS LIVE; AN UNATTRIBUTABLE BEAT IS UNMEASURED.

        This arm previously asserted that `quiet` is NOT live, reasoning that
        only verified-fresh may "authorize" an irreversible retirement. That
        had the polarity backwards: this rung answering LIVE makes the door
        REFUSE, so accepting MORE words is strictly more conservative. `quiet`
        is "seated, idle a while" — a seat with a pane, between turns — and
        calling it gone is how live work gets retired.

        UNVERIFIED stays not-live for a different and still-good reason: the
        beat may belong to another process, so it measures nothing about THIS
        seat. It reads UNKNOWN, which refuses — it never reads ABSENT.
        """
        with self._projection([self.NATIVE, self.QUIET, self.UNVER]):
            fresh = landreq._presence_state("native-seat")
            quiet = landreq._presence_state("quiet-seat")
            unver = landreq._presence_state("unverified-seat")
        self.assertEqual(fresh[0], landreq.SEAT_LIVE, fresh)
        self.assertEqual(quiet[0], landreq.SEAT_LIVE,
                         "a SEATED seat was read as not-live: %r" % (quiet,))
        self.assertEqual(unver[0], landreq.SEAT_UNKNOWN,
                         "an unattributable beat was treated as a measurement "
                         "of this seat: %r" % (unver,))
        self.assertNotEqual(unver[0], landreq.SEAT_ABSENT,
                            "UNVERIFIED must never reach the permit state")

    def test_an_UNREADABLE_projection_leaves_the_caller_where_it_was(self):  # noqa: VACUOUS_ASSERTION — `assertIsNotNone(control)` against a READABLE projection runs first and unconditionally, so the two degradation Nones below cannot be satisfied by a rung that never answers at all.
        """The rung is ADDITIVE: every failure answers None, so it can only
        ever ADD liveness evidence and never be the thing that makes a seat
        look gone."""
        from helm import seats_report

        def boom():
            raise RuntimeError("projection is down")

        with self._projection([self.NATIVE]):
            control = landreq._presence_state("native-seat")
        self.assertEqual(
            control[0], landreq.SEAT_LIVE,
            "positive control: the rung answers for a readable projection")
        # A FAILURE MUST READ UNKNOWN, NEVER ABSENT. This is the whole
        # fail-closed contract: an instrument that cannot look has not looked,
        # and its silence may not spend a row.
        with mock.patch.object(seats_report, "presence_report", boom):
            state, _why, reason = landreq._presence_state("native-seat")
            self.assertEqual(state, landreq.SEAT_UNKNOWN,
                             "a raising projection did not read UNKNOWN")
            self.assertNotEqual(state, landreq.SEAT_ABSENT)
            # THE REASON IS THE PART A CALLER ACTS ON: an instrument that never
            # answered must not read as one that answered and missed this name.
            self.assertEqual(reason, landreq.PRESENCE_UNREADABLE)
        with self._projection([]):
            state, _why, reason = landreq._presence_state("native-seat")
            self.assertEqual(state, landreq.SEAT_UNKNOWN,
                             "an empty projection did not read UNKNOWN")
            self.assertNotEqual(state, landreq.SEAT_ABSENT)
            self.assertEqual(reason, landreq.PRESENCE_MISS,
                             "an EMPTY projection is readable and describes no "
                             "row for anybody, which is a miss and not an "
                             "unreadable instrument")

    def test_the_rung_READS_the_canonical_projection(self):
        """The part a double cannot fake: if someone swaps this back to an
        environ-derived census, the family-blind hole reopens silently and
        every arm above still passes against its own double. This one goes
        red instead."""
        from helm import seats_report
        seen = {"n": 0}

        def counted():
            seen["n"] += 1
            return [self.NATIVE]

        with mock.patch.object(seats_report, "presence_report", counted):
            landreq._presence_state("native-seat")
        self.assertEqual(
            seen["n"], 1,
            "the presence rung did not call seats_report.presence_report — it "
            "is reading some other liveness source, and the one it replaced "
            "could not see the claude-native family at all")


class ARetiredRowLeavesTheHeldListingTest(RetireBase):
    """`helm dispatch list --held` is the listing a human is POINTED AT to
    inspect held candidates — dispatches prints that hint by name in its own
    repair line. Retirement PRESERVES the status word and only adds
    `retired_admin`, so a filter keyed on `status == "held"` went on offering
    back rows the retire door had already cleared.

    FAILURE SCENARIO: a request is held waiting on an author who no longer
    exists, ages out, and is administratively retired. The operator opens the
    listing the tool told them to open, and the retired row is still sitting
    in it as a candidate for work nobody can act on.
    """

    @staticmethod
    def _dispatch_cli(*args):
        """(rc, stdout) from the REAL dispatch CLI — the surface a human
        actually reads. tests.test_landreq's `run` is hardwired to cmd_lr."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = dispatches.cmd_dispatch(list(args))
        return rc, buf.getvalue()

    def _held_row(self, reason, lane, repo=None):
        """An OPEN row moved to HELD through the real verb.

        It must be OPEN first — `mark_hold` refuses anything else ("only an
        OPEN row can be held"). `repo` is a parameter because the retire
        reason that reaches a held row is `repo-unreadable`: the two reasons
        keyed to a seat cannot touch one (an OPEN row is owed by the
        integrator, who is on no row, and the parked reason demands a
        verdict), while repo-unreadable asks only about the row's repository
        binding and so is indifferent to the status word.
        """
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = "deleted-author"
        try:
            row = dispatches.add("ghost-reviewer", lane, ref=self.side,
                                 repo=repo or self.repo, kind="review",
                                 notify=False, new_work=True)
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        self.assertIsNotNone(row)
        out, why = dispatches.mark_hold(row["id"], reason)
        self.assertIsNone(why, why)
        self.assertEqual(str(out.get("status") or "").strip().lower(), "held",
                         "the fixture did not actually reach HELD")
        return row

    def test_a_retired_HELD_row_is_not_offered_by_dispatch_list_held(self):
        held = self._held_row("waiting on a deleted author",
                              lane="lane/retire-held")
        control = self._held_row("waiting on upstream CI",
                                 lane="lane/retire-control")
        # THE REPO REALLY GOES AWAY, because `dispatches.add` validates the
        # binding at creation — a row cannot be BORN unreadable, it becomes so
        # when the checkout is moved or deleted under it. That is the whole
        # production story for this reason, so the fixture tells it rather
        # than fabricating a row that could not exist.
        stashed = self.repo + ".gone"
        os.rename(self.repo, stashed)
        try:
            r_patch, a_patch = self.reach(
                roster=self.roster({"seat-a": "s"}),
                panes=self.panes("seat-a"))
            with r_patch, a_patch:
                out, why = landreq.retire(held["id"], "repo-unreadable",
                                          seat="integrator")
        finally:
            os.rename(stashed, self.repo)
        self.assertIsNone(why, why)
        # THE PREMISE THIS ARM RESTS ON, asserted rather than assumed: retiring
        # does NOT move the status word. If retirement closed the row instead,
        # its absence below would prove nothing about the --held filter.
        self.assertTrue(out["retired_admin"])
        self.assertEqual(str(out.get("status") or "").strip().lower(), "held",
                         "retirement moved the status word — this arm's whole "
                         "premise (a RETIRED row that is still spelled HELD) "
                         "no longer holds, so it can no longer discriminate")
        rc, listed = self._dispatch_cli("list", "--held")
        self.assertEqual(rc, 0)
        self.assertNotIn(
            held["id"], listed,
            "a RETIRED row is still offered by `dispatch list --held` — the "
            "filter is reading the raw status word, which retirement leaves "
            "alone")
        # THE MUST-HIT: an identically-held row that was NOT retired is still
        # listed, so the filter is discriminating on retirement and has not
        # simply gone blind (an empty listing would pass the assert above).
        self.assertIn(
            control["id"], listed,
            "the un-retired held row vanished too — `--held` is not filtering "
            "on retirement, it is returning nothing at all")


class ASeatedButIdleSeatIsLiveTest(RetireBase):
    """SEATED IS LIVE. The presence projection answers one of four words —
    fresh / quiet / unverified / absent — and `quiet` means "seated, idle a
    while": a seat with a pane, between turns.

    FAILURE SCENARIO: the rung accepted only `fresh`, collapsing a four-word
    answer into a boolean and making IDLE indistinguishable from GONE. A
    claude-native seat is invisible to the pane rung (that names seats from
    HELM_CHAT_NAME in a process environ, which native processes do not
    carry), so for a quiet native seat the roster was the only voice left —
    and a stale session there reads DARK. Retirement would then take work
    from a seat sitting right there, which is the exact failure this whole
    verb exists to prevent.
    """

    SEAT = "quiet-native-seat"

    def _reach(self, word):
        """A rostered seat with NO current session and NO pane, so the ONLY
        thing that can save it is the presence projection.

        THE AUTHORSHIP INDEX IS PART OF THE WORLD NOW, and a bundle that omits
        it is not a bare fixture but a fixture that reads PRODUCTION: the
        rostered permit path measures the absence's DURATION, an omitted
        `activity` makes `seat_reach` ask the REAL dispatch ledger about a
        fixture seat, and the permit arm below would then hold only because a
        name helm never saw act cannot be proven departed — an accident, in a
        class whose subject is the presence vocabulary.

        It is built by passing producer-shaped events through the shipped
        `seat_activity`, so the shape is the writers' and not this arm's: the
        seat last authored a month ago (silent past the window) and a decoy
        authored today, which is what keeps the INDEX current. That is the
        world these arms have always meant.
        """
        return dict(
            roster={self.SEAT: {}, "decoy-seat": {"session": "s"}},
            roster_failed=False,
            agents={"by_seat": {}},
            activity=_activity_from(
                *_acts((self.SEAT, "send", 30), ("decoy-seat", "send", 0))),
            presence=[{"seat": self.SEAT, "presence": word,
                       "runtime": {"family": "claude", "backend": "native"}}])

    def test_the_fixture_index_is_the_one_the_producer_builds(self):  # noqa: VACUOUS_ASSERTION — the two assertEquals pin exact stamps on the same tuple the assertIsNone reads, so neither can hold over an empty index
        """THE PREMISE OF EVERY ARM IN THIS CLASS, asserted rather than
        assumed. A bundle whose `activity` were absent, or keyed on some other
        spelling of the seat, would leave the permit arm below green for a
        reason that has nothing to do with the presence word it is about."""
        index, _doubt, newest, err = self._reach("absent")["activity"]
        self.assertIsNone(err, err)
        self.assertEqual(index.get(self.SEAT), _ago(30),
                         "the fixture index does not date this seat's silence, "
                         "so the permit arm is measuring production")
        self.assertEqual(newest, _ago(0),
                         "the fixture index is not current, so the duration "
                         "rung would refuse for the wrong reason")

    def test_a_QUIET_seat_reads_LIVE(self):
        state, why = landreq.seat_reach(self.SEAT, **self._reach("quiet"))
        self.assertEqual(state, landreq.SEAT_LIVE, why)
        self.assertIn("QUIET", why)

    def test_a_FRESH_seat_still_reads_LIVE(self):
        state, why = landreq.seat_reach(self.SEAT, **self._reach("fresh"))
        self.assertEqual(state, landreq.SEAT_LIVE, why)

    def test_a_positively_ABSENT_seat_is_the_ONLY_thing_that_permits(self):
        """THE CONTROL that makes the LIVE arms mean something, and the one
        place the permit state is reachable at all.

        `absent` is the projection POSITIVELY reporting no recent beat for a
        seat it DOES describe, so with no session and no pane every clause is
        a positive fact and the door may proceed. `unverified` is a beat that
        cannot be attributed to this seat, which measures nothing — it must
        read UNKNOWN and refuse, never reach the permit state.
        """
        state, why = landreq.seat_reach(self.SEAT, **self._reach("absent"))
        self.assertEqual(state, landreq.SEAT_ABSENT, why)

        state, why = landreq.seat_reach(self.SEAT, **self._reach("unverified"))
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertNotEqual(state, landreq.SEAT_ABSENT,
                            "an unattributable beat reached the permit state")

    def test_an_UNROSTERED_seat_is_UNKNOWN_not_absent(self):
        """The phase-2 hole, pinned so phase 1 cannot silently grow into it.

        A seat with NO roster row is described by no instrument helm has, so
        its silence is a MISS. Reading that as absence is exactly what would
        retire an unrostered live native seat.
        """
        reach = self._reach("absent")
        reach["roster"] = {"decoy-seat": {"session": "s"}}   # ours is gone
        reach["presence"] = []                               # so is its row
        state, why = landreq.seat_reach(self.SEAT, **reach)
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertNotEqual(state, landreq.SEAT_ABSENT,
                            "an UNROSTERED seat reached the permit state — "
                            "this is the phase-2 hole, and it must refuse")

    def test_the_live_words_are_a_SUBSET_of_the_projection_vocabulary(self):
        """The accepted words must be words the projection can actually emit.
        A typo here fails OPEN — the rung would simply never fire and the
        arms above are the only thing that would notice."""
        from helm import seats_report
        vocabulary = set(seats_report.PRESENCE_DOTS)
        self.assertTrue(vocabulary, "presence vocabulary is empty")
        unknown = sorted(landreq._PRESENCE_LIVE_WORDS - vocabulary)
        self.assertFalse(
            unknown,
            "these accepted presence words are not in the projection's own "
            "vocabulary %r, so the rung can never fire on them: %r"
            % (sorted(vocabulary), unknown))


class TheSweepCarriesEveryInstrumentTest(RetireBase):
    """A sweep classifies dozens of rows off ONE reading of the fleet.

    FAILURE SCENARIO: `_retire_context` built its own two-instrument dict
    while the door used a three-instrument one, so a sweep carried no presence
    reading at all and every row measured the projection for itself. Both now
    come from the same constructor, which is the only way the two can't drift.
    """

    def test_the_sweep_bundle_carries_every_probe_in_the_table(self):
        seen = []
        answers = {"roster": ({}, False), "agents": {"by_seat": {}},
                   "presence": []}
        patches = []
        for key, probe_name, _fail in landreq._REACH_PROBES:
            def make(k):
                def f():
                    seen.append(k)
                    return answers[k]
                return f
            patches.append(mock.patch.object(landreq, probe_name, make(key)))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            reach = landreq._reach_bundle()
        expected = [k for k, _n, _f in landreq._REACH_PROBES]
        self.assertEqual(sorted(seen), sorted(expected),
                         "the bundle did not probe every registered "
                         "instrument: probed %r, table says %r"
                         % (sorted(seen), sorted(expected)))
        for key in expected:
            self.assertIn(key, reach,
                          "instrument %r was probed but never stored, so "
                          "every reader re-measures it" % key)


class TheDeadSeatRollIsHonestTest(RetireBase):
    """The fixture roll cannot silently miss a name.

    `reach()` rosters `_FIXTURE_DEAD_SEATS` as positively-absent so an arm
    exercising a legitimate retirement does not have to restate the world. A
    name that follows the convention but is missing from the roll would be
    UNROSTERED, read UNKNOWN, and its arm would fail with a refusal that looks
    like a code defect instead of a fixture gap.
    """

    def test_the_dead_seat_roll_covers_every_conventional_name(self):
        import re
        with io.open(__file__, encoding="utf-8") as fh:
            src = fh.read()
        found = set(re.findall(r'"((?:dead|deleted|ghost)-[a-z0-9-]+)"', src))
        self.assertTrue(found, "the scan found no conventional names at all — "
                               "the pattern rotted and this arm proves nothing")
        missing = sorted(found - set(_FIXTURE_DEAD_SEATS))
        self.assertFalse(
            missing,
            "these fixture-dead names are not on the roll, so they are "
            "UNROSTERED and will read UNKNOWN rather than absent: %r"
            % (missing,))

    def test_every_name_on_the_roll_reads_positively_ABSENT(self):
        """The roll is only useful if it produces the permit state."""
        r_patch, a_patch = self.reach(panes=self.panes())
        with r_patch, a_patch:
            for name in _FIXTURE_DEAD_SEATS:
                with self.subTest(seat=name):
                    state, why = landreq.seat_reach(name)
                    self.assertEqual(state, landreq.SEAT_ABSENT, why)


# Every function permitted to resolve a RETIRED row, and the reason it may.
# `_resolve_row` is default-closed, so this is the complete exemption list and
# adding to it is a review decision, not an edit.
_ALLOW_RETIRED_OPT_INS = {
    # IDEMPOTENCE — must see the row to answer "already retired"
    "_record_retire_proven": "IDEMPOTENCE",
    "retire": "IDEMPOTENCE",
    # READ — a retirement is not a deletion; these surfaces must still show it
    "_resolve_chain": "READ",
    # The dispatch verb table, which is `_cmd_dispatch` since the public
    # `cmd_dispatch` became a door that scopes the read verbs and delegates.
    # The census reads the FUNCTION that calls `_resolve_row`, so the name
    # here follows the body, never the CLI's entry point.
    "_cmd_dispatch": "READ",
    "get": "READ",
    "_selected_chain_ids": "READ",
    "_cmd_compose": "READ",
    # DIAGNOSIS — exactly ONE, and it is an EARNED PROPERTY rather than a
    # kind label: `_resolve_row_for_diagnosis` consumes a retired row into
    # (None, specific terminal refusal), so no caller can receive that row as
    # actionable. The five close ladders call it with no opt-out of their own.
    # The property is proven by TheDiagnosticResolverNeverReturnsATerminalRow.
    "_resolve_row_for_diagnosis": "DIAGNOSIS (terminal row never returned)",
}
# `dispatches_cli` carries the verb table, whose `_cmd_dispatch` is a
# declared opt-in below. The census walks each module's OWN file, so the
# satellite has to be named here or the entry reads STALE for a move that
# changed no behaviour -- the registry comment already says the name
# follows the BODY, never the CLI entry point.
_ALLOW_RETIRED_MODULES = ("dispatches", "dispatches_cli", "landreq")


class TheRetiredRowExemptionsAreDeclaredTest(RetireBase):
    """WHO MAY TOUCH A RETIRED ROW IS A DECLARED LIST, NOT A HABIT.

    `_resolve_row` refuses a retired row by default, so every mutation door
    inherits the guard without knowing it exists. The risk moves to the
    exemption: a future caller adds `allow_retired=True` to get past a
    refusal, and the terminality guarantee quietly loses a door. This censuses
    the exemptions from PRODUCTION and fails on any that is not declared with
    a reason above.
    """

    def test_every_allow_retired_opt_in_is_declared_with_a_reason(self):
        import ast
        import importlib
        opted = set()
        for modname in _ALLOW_RETIRED_MODULES:
            prod = importlib.import_module("helm.%s" % modname)
            # EVERY FILE THE MODULE'S SURFACE SPANS, not just its own. When
            # the never-track ceiling split `landreq`, `_cmd_compose` moved
            # to a satellite and this census reported its exemption as
            # "declared but no longer used" -- the call site was still there
            # and still exempting, it had just stopped being text in the file
            # being opened. An exemption census that cannot see a moved call
            # site would invite pruning a LIVE exemption.
            for _path, source in ledger_sources(prod):
                tree = ast.parse(source)
                for fn in ast.walk(tree):
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    for call in ast.walk(fn):
                        if not isinstance(call, ast.Call):
                            continue
                        name = (getattr(call.func, "id", None)
                                or getattr(call.func, "attr", None))
                        if name != "_resolve_row":
                            continue
                        if any(kw.arg == "allow_retired"
                               and getattr(kw.value, "value", False) is True
                               for kw in call.keywords):
                            opted.add(fn.name)
        # MUST-HIT: the scan finds the exemptions we know exist, so a zero
        # here is a rotted scan rather than a clean tree.
        self.assertTrue(opted, "the census found NO allow_retired call sites "
                               "at all — the scan rotted and a zero from it "
                               "proves nothing")
        undeclared = sorted(opted - set(_ALLOW_RETIRED_OPT_INS))
        self.assertFalse(
            undeclared,
            "these functions resolve RETIRED rows without a declared reason, "
            "so a mutation door may have quietly exempted itself from "
            "terminality: %r" % (undeclared,))
        stale = sorted(set(_ALLOW_RETIRED_OPT_INS) - opted)
        self.assertFalse(
            stale,
            "these exemptions are declared but no longer used — prune them so "
            "the list stays a description of the code: %r" % (stale,))

    def test_the_default_really_is_CLOSED(self):
        """The census above is worthless if the default admits anyway."""
        retired = {"id": "f" * 32, "status": "open", "retired_admin": True,
                   "retire_reason": "repo-unreadable",
                   "retire_ts": "2026-08-27T00:00:00Z"}
        current = {retired["id"]: retired}
        row, err = dispatches._resolve_row(current, retired["id"])
        self.assertIsNone(row, "a retired row resolved for mutation")
        self.assertIn("administratively retired", err)
        self.assertIn("repo-unreadable", err, "the refusal must name the "
                                              "bounded reason")
        self.assertIn("2026-08-27", err, "the refusal must say WHEN")
        # ...and the opt-in really opens it, or the default proves nothing
        row, err = dispatches._resolve_row(current, retired["id"],
                                           allow_retired=True)
        self.assertIsNotNone(row, err)

    def test_the_reason_is_BOUNDED_never_the_free_form_note(self):
        """A refusal reaches an operator's terminal and a log. An unbounded
        recorded field has no business on either."""
        row = {"id": "e" * 32, "retired_admin": True,
               "retire_reason": "not-a-registered-reason",
               "retire_note": "SECRET-NOTE-SHOULD-NOT-APPEAR",
               "retire_ts": "2026-08-27T00:00:00Z"}
        self.assertEqual(dispatches._retired_admin_by(row), "unspecified")
        refusal = dispatches._retired_admin_refusal(row)
        self.assertNotIn("SECRET-NOTE", refusal)
        self.assertNotIn("not-a-registered-reason", refusal)
        # a REGISTERED reason does pass through, or the clamp is a black hole
        row["retire_reason"] = "repo-unreadable"
        self.assertEqual(dispatches._retired_admin_by(row), "repo-unreadable")
        self.assertIn("repo-unreadable", dispatches._retired_admin_refusal(row))


def _prepare_retip_car(test, _rid):
    """One rewritten target that really carries the reviewed side patch."""
    test.git("checkout", "-q", "-b", "retip-matrix", test.c)
    test.git("cherry-pick", test.side)
    test.retip_target = test.git("rev-parse", "HEAD")
    test.git("checkout", "-q", test.main)


# (event kind, prepare(self, rid), write(self, rid)) — the PRODUCTION writer
# for each kind. The matrix never hand-spells a payload: it makes production
# emit the event and transplants THAT. So if a writer and its reducer arm ever
# drift apart, the active control fails and says so, instead of the transplant
# quietly doing nothing and reading as retirement safety.
_PRODUCTION_WRITERS = (
    ("delivered", None,
     lambda test, rid: dispatches._mark_delivered(rid, "matrix-probe")),
    ("hold", None,
     lambda test, rid: dispatches.mark_hold(rid, "matrix probe hold")),
    ("cancel", None,
     lambda test, rid: dispatches.mark_cancel(rid, "matrix probe cancel")),
    # `release` needs a real HELD predecessor, minted by the production hold
    # writer — not a status field set by hand.
    ("release",
     lambda test, rid: dispatches.mark_hold(rid, "matrix probe predecessor"),
     lambda test, rid: dispatches.mark_release(rid)),
    # `retip` needs a repo-bound OPEN row and a rewritten tip carrying the
    # reviewed work. An unrelated resolvable commit is correctly refused and
    # emits nothing, so the preparation cherry-picks the reviewed side patch
    # onto trunk before invoking the production writer.
    ("retip", _prepare_retip_car,
     lambda test, rid: dispatches.retip(
         rid, test.retip_target, reason="matrix probe", repo=test.repo,
         notify=False)),
    # `verdict` needs a real DELIVERED predecessor AND a tip that RESOLVES.
    # A fabricated 40-hex sha made mark_verdict append NOTHING, and the arm's
    # own "appended 0 events" guard is what caught it — the transplant would
    # otherwise have been an empty event proving inertness vacuously.
    ("verdict",
     lambda test, rid: dispatches._mark_delivered(rid, "matrix-probe"),
     lambda test, rid: dispatches.mark_verdict(rid, test.side, "matrix probe",
                                               polarity="fix")),
    # a model run's ADVISORY read (task/2948), recorded by the row's sender
    ("advisory-read", None,
     lambda test, rid: _write_advisory_read(test, rid)),
    # the qwen27 findings pass's NOTE (task/2960), by its production writer
    ("findings-note", None,
     lambda test, rid: dispatches.record_findings_note(
         rid, test.side, dict(_FINDINGS_NOTE_FIELDS))),
)

#: A complete read that kept nothing, exactly as the pass records one.
_FINDINGS_NOTE_FIELDS = (
    ("outcome", "complete"), ("rc", 0), ("reads", 1), ("kept", 0),
    ("status_line", "LOCAL-REVIEW-STATUS complete reads=1 errors=0 empty=0 "
                    "cut=0 truncated=0 kept=0 rejected=0 unjudged=0"))


def _write_advisory_read(test, rid):
    """The production writer of an `advisory-read` event, by the row's own
    sender: a codex-family read of a Claude author's row."""
    with mock.patch.object(dispatches, "_acting_author",
                           return_value=("ghost-author", None)):
        return dispatches.mark_verdict(
            rid, test.side, "matrix probe", polarity="concur",
            reviewer_model="gpt-6-astra", reviewer_run="matrix-probe",
            author_model="claude-opus-5-5", bind_author=True)

# EVENT KINDS DELIBERATELY OUTSIDE THE WRITER MATRIX, and why. The reducer
# guard still covers them — they are in `_ACTIVE_ONLY_EVENTS` and
# TheActiveOnlyTableIsFullyAccountedFor proves this list plus the matrix
# accounts for every one — but they have no writer this harness can invoke on
# a freshly-built twin, and padding the matrix with a transplant whose proof
# is identity-bound would assert something the fixture cannot honestly show.
_WRITERS_NOT_TRANSPLANTABLE = {
    "superseded": "minted by the successor-creation path, not by a writer "
                  "taking a row id — the event's proof is the successor's "
                  "identity, so transplanting it onto another row is a "
                  "different claim.",
    "retarget": "LEGACY COMPAT with no shipping writer. It stays in "
                "_ACTIVE_ONLY_EVENTS so a compat replay after a retirement is "
                "inert, but no production path emits one, "
                "so there is nothing to capture.",
}


class ARetiredRowIsInertInReplayTest(RetireBase):
    """A HAND-APPENDED EVENT NEVER PASSES THROUGH `_resolve_row`.

    The resolver guard closes the doors a verb or operator goes through. It
    cannot close the ledger: anything appending bytes — a forged row, a stale
    writer, a replayed file — reaches the reducer directly. Without a rung
    there, a retired obligation could be moved on the next replay.

    THE MATRIX USES THE PRODUCTION WRITER, and that is the third shape this
    arm has had. Synthetic state dicts proved nothing (the reducer's
    preconditions were never met). A real-ledger append still proved nothing
    when the payload is hand-spelled and omits `delivery_ref` — the reducer
    bailed and returned state unchanged, which is INDISTINGUISHABLE from the
    terminality rung working. So: production writes the event on an ACTIVE
    twin, the arm asserts that really moved it, and only then is that exact
    event transplanted onto the retired twin.
    """

    def _events(self):
        return eventledger.events(dispatches.ledger_path())

    def test_the_active_only_table_is_fully_accounted_for(self):
        """EVERY event the reducer guards is either exercised by the writer
        matrix or explicitly excluded with a reason. A kind added to
        `_ACTIVE_ONLY_EVENTS` with neither goes red here rather than silently
        inheriting a guarantee nothing proves."""
        covered = {kind for kind, _p, _w in _PRODUCTION_WRITERS}
        excluded = set(_WRITERS_NOT_TRANSPLANTABLE)
        guarded = set(dispatches._ACTIVE_ONLY_EVENTS)
        self.assertTrue(guarded, "the production event table is empty")
        unaccounted = sorted(guarded - covered - excluded)
        self.assertFalse(
            unaccounted,
            "these guarded events are neither exercised by the writer matrix "
            "nor declared untransplantable: %r" % (unaccounted,))
        stale = sorted((covered | excluded) - guarded)
        self.assertFalse(
            stale,
            "these are covered or excluded but the reducer no longer guards "
            "them — prune so this stays a description of production: %r"
            % (stale,))

    def _state(self, rid):
        return dispatches.snapshot()[0].get(rid)

    def test_every_production_writer_is_INERT_after_a_retirement(self):
        self.assertTrue(_PRODUCTION_WRITERS, "the writer table is empty")
        for kind, prepare, write in _PRODUCTION_WRITERS:
            with self.subTest(event=kind):
                # --- the ACTIVE twin, written by PRODUCTION ---
                active = self.open_row(author="ghost-author",
                                       lane="lane/matrix-%s" % kind)
                if prepare:
                    prepare(self, active["id"])
                before_state = dict(self._state(active["id"]))
                before_events = len(self._events())
                write(self, active["id"])
                appended = self._events()[before_events:]
                self.assertEqual(
                    len(appended), 1,
                    "production writer for %r appended %d events, so there is "
                    "no single event to transplant" % (kind, len(appended)))
                captured = appended[0]
                self.assertEqual(captured.get("event"), kind,
                                 "writer emitted %r, not %r"
                                 % (captured.get("event"), kind))
                after_state = self._state(active["id"])
                self.assertNotEqual(
                    after_state, before_state,
                    "the PRODUCTION writer for %r did not move an ACTIVE row "
                    "— writer and reducer have drifted, and every inertness "
                    "assertion below would be vacuous" % kind)
                self.assertNotEqual(after_state.get("seq"),
                                    before_state.get("seq"),
                                    "%r did not advance seq on an active row"
                                    % kind)

                # --- transplant that EXACT event onto the RETIRED twin ---
                retired = self.retired_row(lane="lane/matrix-%s-dead" % kind)
                r_before = dict(self._state(retired["id"]))
                moved = dict(captured)
                moved["id"] = retired["id"]
                moved["seq"] = int(r_before.get("seq") or 0) + 1
                count_before = len(self._events())
                with eventledger.locked(dispatches.ledger_path()):
                    eventledger.append_unlocked(dispatches.ledger_path(), moved)
                self.assertEqual(
                    len(self._events()), count_before + 1,
                    "the transplant did not append, so nothing was tested")
                self.assertEqual(
                    self._state(retired["id"]), r_before,
                    "a byte-valid production %r event moved a RETIRED row" % kind)
                self.assertEqual(
                    self._state(retired["id"]).get("seq"), r_before.get("seq"),
                    "%r advanced seq on a retired row" % kind)


class TheDiagnosticResolverNeverReturnsATerminalRowTest(RetireBase):
    """THE EARNED PROPERTY behind the one DIAGNOSIS exemption.

    `_resolve_row_for_diagnosis` is the only close-path function permitted to
    see a retired row, and it is permitted BECAUSE it never hands one back. If
    that property ever lapses, five close ladders silently start receiving
    retired rows as actionable — so the exemption is only as good as this arm.
    """

    def test_a_retired_row_is_consumed_into_a_refusal(self):
        row = self.retired_row()
        current = dispatches.snapshot()[0]
        got, err = landreq._resolve_row_for_diagnosis(current, row["id"])
        self.assertIsNone(got, "the diagnostic resolver RETURNED a retired "
                               "row — every close ladder now receives it as "
                               "actionable")
        self.assertTrue(err, "it returned neither a row nor an error, so a "
                             "caller cannot tell it refused")
        # names the bounded terminal VERB and its REGISTERED reason...
        self.assertIn("retire --reason", err)
        self.assertIn("author-unresolvable", err)
        # ...and says WHEN
        self.assertRegex(err, r"\d{4}-\d{2}-\d{2}T")

    def test_it_never_echoes_the_free_form_note(self):
        row = self.retired_row()
        current = dict(dispatches.snapshot()[0])
        poisoned = dict(current[row["id"]])
        poisoned["retire_note"] = "SECRET-NOTE-MUST-NOT-APPEAR"
        current[row["id"]] = poisoned
        _got, err = landreq._resolve_row_for_diagnosis(current, row["id"])
        self.assertNotIn("SECRET-NOTE", err or "")

    def test_it_APPENDS_NOTHING(self):
        row = self.retired_row()
        before = self.ledger_state(row["id"])
        landreq._resolve_row_for_diagnosis(dispatches.snapshot()[0], row["id"])
        self.assertEqual(self.ledger_state(row["id"]), before,
                         "the diagnostic resolver wrote to the ledger")

    def test_an_ACTIVE_row_comes_back_normally(self):  # noqa: VACUOUS_ASSERTION — assertIsNotNone(got) and the id equality on the SAME call below are the unconditional positive control; the assertIsNone(err) is never asserted alone
        """THE CONTROL. Without it the property above is satisfied by a
        function that refuses EVERYTHING, which would break all five close
        ladders while passing every assertion here."""
        row = self.open_row(author="ghost-author", lane="lane/diagnosis-ctl")
        got, err = landreq._resolve_row_for_diagnosis(
            dispatches.snapshot()[0], row["id"])
        self.assertIsNone(err, err)
        self.assertIsNotNone(got, "an ACTIVE row was refused by the "
                                  "diagnostic resolver")
        self.assertEqual(got["id"], row["id"])


class TheSingleMeasureRetirePathIsStructuralTest(RetireBase):
    """ONE PATH, PROVEN BY CENSUS RATHER THAN BY CONVENTION.

    Every retire reason is invoked through `_measure_retire`, which applies
    the shared liveness belt exactly once and only at the ADMIT. This used to
    be four per-reason copies, and they had DRIFTED: two called the belt at
    their entry (shadowing their own diagnosis) and two at their admit. One
    path cannot drift from itself, and a fifth reason inherits the belt
    without having to remember it — but only while nothing dispatches around
    the wrapper, which is what this censuses.
    """

    def test_only_the_wrapper_dispatches_a_reason(self):
        import ast
        with io.open(landreq.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        dispatchers = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Subscript) and \
                        getattr(node.value, "id", None) == "RETIRE_MEASUREMENTS":
                    dispatchers.add(fn.name)
        self.assertTrue(dispatchers, "the census found NO dispatch of "
                                     "RETIRE_MEASUREMENTS at all — it rotted")
        self.assertEqual(
            dispatchers, {"_measure_retire"},
            "a reason is dispatched OUTSIDE the single path, so it runs "
            "without the shared belt: %r" % (sorted(dispatchers),))

    def test_only_the_wrapper_applies_the_belt(self):
        import ast
        with io.open(landreq.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        callers = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and \
                        getattr(node.func, "id", None) == "_retire_live_guard":
                    callers.add(fn.name)
        self.assertTrue(callers, "no belt call site found — census rotted")
        self.assertEqual(
            callers, {"_measure_retire"},
            "the belt is applied outside the single path, which is how the "
            "entry-vs-admit drift happened before: %r" % (sorted(callers),))

    def test_the_belt_runs_AFTER_the_reason_so_it_cannot_shadow(self):
        """THE ORDERING IS THE DESIGN. Run first, the belt turns every
        reason-specific diagnosis into a generic 'a party is reachable'."""
        row = self.verdicted(polarity="fix", author="dead-author")
        # a world where the reason REFUSES on its own grounds AND a party is
        # live: the reason's message must win, not the belt's.
        roster = self.roster({"live-reviewer": "session-live", "seat-a": "s"})
        r_patch, a_patch = self.reach(
            roster=roster, panes=self.panes("seat-a"),
            presence=[{"seat": "live-reviewer", "presence": "fresh"},
                      {"seat": "seat-a", "presence": "fresh"}])
        with r_patch, a_patch:
            _m, refusal = landreq._measure_retire(
                "tier-unevaluable-parked", dict(row), {"reach": {}})
        self.assertTrue(refusal, "the pair produced no refusal at all")
        self.assertNotIn(
            "which is NOT PROVEN GONE", refusal,
            "the BELT answered where the REASON should have — its generic "
            "message shadowed the specific diagnosis: %s" % refusal)


class TheActorRegistryBELONGSToTheWriterTest(RetireBase):
    """THE READER KEEPS NO LIST OF KINDS — and after this cut it keeps no
    reader either. `dispatches.LEDGER_EVENT_ACTORS` is the registry the producer
    module owns and `dispatches._credit_act`, inside the fold, is the only thing
    that enumerates it.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the authorship index kept a table of
    three kinds, derived by hand from three producers. An administrative
    RETIREMENT records its acting seat in `retire_seat` and was in neither the
    table nor anybody's sweep of it, so a seat whose only recent act was one read
    as silent against a month-old send — and silence is exactly what spends a
    row. A table at the READER has to be widened by hand for every producer that
    lands at the WRITER, and nothing makes that happen.

    ONE INSTRUMENT, AND THE SECOND ONE WAS REMOVED FOR CAUSE: the WRITER'S
    SOURCE, walked by AST, is what reddens when a NEW producer lands in that
    module — which is the failure that happened. The other half read the FLEET's
    ledger off the default root to catch a HISTORICAL kind nobody writes any
    more, and that made an ordinary discovered arm a property of the HOST rather
    than of the tree. `TheActivityReadingIsHERMETICTest` states the cost of that
    in full. What the tree CAN prove about the historical spellings is proved
    below; the census over a real corpus belongs to an opt-in instrument.
    """

    def test_every_kind_the_WRITER_appends_to_THIS_ledger_is_registered(self):
        """THE HERMETIC HALF, and the one that reddens on a new producer.

        Each `"event": "<kind>"` literal in the writer's own source is required
        to be registered UNLESS the function carrying it appends to the ATTEST
        ledger instead — which is the discrimination a reader cannot make from
        outside, and the reason the registry lives at the writer.
        """
        import ast
        # THE WRITER SPANS SEVERAL FILES, so this census must read every one:
        # it works on module TEXT, and a literal that sits in a satellite is
        # invisible to a reader that opens only the facade -- the registry
        # entry for it then reads stale and a behaviour-neutral move goes red.
        # THE FILE LIST IS DERIVED, NEVER TYPED. `ledger_sources` follows the
        # `_OWNER_NAMES` declaration, which the retired-name rung independently
        # requires to be correct, so a census written against it stays right
        # across a split with no edit here. A list of satellite names spelled
        # out at this call site would be wrong the day the next one lands.
        tree = ast.parse("\n".join(
            source for _path, source in ledger_sources(dispatches)))

        def literals(node):
            found = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Dict):
                    continue
                for key, value in zip(sub.keys, sub.values):
                    if isinstance(key, ast.Constant) and key.value == "event" \
                            and isinstance(value, ast.Constant) \
                            and isinstance(value.value, str):
                        found.add(value.value)
            return found

        def calls(node, name):
            return any(isinstance(sub, ast.Call)
                       and isinstance(sub.func, ast.Name)
                       and sub.func.id == name
                       for sub in ast.walk(node))

        required, elsewhere = set(), set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            kinds = literals(fn)
            if not kinds:
                continue
            if calls(fn, "attest_path") and not calls(fn, "ledger_path"):
                elsewhere |= kinds
            else:
                required |= kinds
        # THE MUST-HIT PAIR: the walk found the kind the hand-typed table
        # missed, and it found at least one kind that goes somewhere else — so
        # neither the requirement nor the exemption below is an empty set
        # quietly satisfying a subset test.
        self.assertIn("retire", required,
                      "the source walk did not find the producer this reader "
                      "had never heard of, so it is reading nothing")
        self.assertTrue(elsewhere,
                        "the walk found no attest-ledger kind, so it cannot "
                        "tell the two ledgers apart and every kind would be "
                        "required by accident")
        self.assertEqual(
            sorted(required - set(dispatches.LEDGER_EVENT_ACTORS)), [],
            "this module appends an event kind its own registry does not "
            "classify — the authorship reader answers nothing for it and reads "
            "every seat whose only act is one of those as silent")
        self.assertEqual(
            sorted(elsewhere & set(dispatches.LEDGER_EVENT_ACTORS)), [],
            "a kind written to the ATTEST ledger is registered as a kind of "
            "THIS one, so the registry describes events the reader never sees")

    def test_the_registry_CLASSIFIES_the_historical_spellings_it_names(self):
        """THE HALF A TREE CAN PROVE, AND THE HALF IT CANNOT, SAID OUT LOUD.

        The registry's coverage has two directions. A NEW producer landing in
        the writer is caught by the AST arm above, hermetically. A HISTORICAL
        kind nobody writes any more lives only in a CORPUS — and the two arms
        covering that half read the FLEET'S ledger off `home.default_home()`,
        escaping the HELM_HOME this suite runs under.
        That made an ordinary discovered test a property of this HOST: it
        skipped where the file was missing, failed where it was empty, and
        changed its assertions for identical source as the fleet worked. A
        suite's oracle may not be somebody's afternoon.

        So this arm claims only what the tree holds: the registry explicitly
        names the spellings the module's own comment says are historical, and
        the shipped projection READS a ledger carrying them without crediting
        anybody for them. The stronger census belongs to a separate opt-in
        instrument over a real corpus, not to a discovered arm — and the
        measurement it would make is recorded beside the registry.
        """
        # THROUGH THE SHIPPED PROJECTION, so the classification is a reading and
        # not a dict lookup: the historical verdict snapshot names the party
        # ASKED, and that name must not be credited. THE MUST-HIT COMES FIRST
        # AND UNCONDITIONALLY — the same projection credits the prefix's own
        # opener, so the absence after it is a discrimination.
        legacy = _one_shape("verdict", recipient="legacy-reviewer", ts=_ago(9))
        index, _doubt, _newest, err = _activity_from(*_prefix_for(legacy))
        self.assertIsNone(err, err)
        self.assertEqual(sorted(index), ["asking-seat"],
                         "the projection credited nobody at all, so the "
                         "exclusion below measures nothing")
        self.assertNotIn("legacy-reviewer", index,
                         "the party ASKED on a historical whole-row snapshot "
                         "was credited as the hand that wrote it")
        # AND THE CONTROL ON THE REGISTRY EXPRESSION ITSELF, unconditionally: a
        # kind that DOES record a hand reads as a non-empty precedence through
        # the same lookup, so asserting the historical ones empty is a reading
        # and not a lookup that answers nothing for everybody.
        self.assertTrue(dispatches.LEDGER_EVENT_ACTORS.get("dispatch"),
                        "the registry answers an empty precedence for the kind "
                        "that carries most of the corpus, so every emptiness "
                        "asserted below is vacuous")
        for spelling in ("", "retarget"):
            self.assertIn(spelling, dispatches.LEDGER_EVENT_ACTORS,
                          "a spelling the registry's own comment calls "
                          "historical is not classified, so every event "
                          "carrying it contributes nothing and says nothing")
            self.assertEqual(dispatches.LEDGER_EVENT_ACTORS[spelling], (),
                             "a whole-row historical snapshot records no hand, "
                             "so reading a field on one is the mentioned-party "
                             "law broken on the oldest rows there are")

    def test_every_seat_shaped_field_in_the_CORPUS_is_READ_or_EXCLUDED(self):
        """THE MENTIONED-PARTY LAW, MEASURED AGAINST A CORPUS rather than
        asserted — the same arm as before, over the corpus the TREE owns.

        A seat-shaped field on a producer-shaped event is either the hand (the
        registry reads it) or somebody else (`LEDGER_NOT_AN_ACTOR` says who and
        why). A third case is a field nobody has decided about.
        """
        shaped = set()
        for _why, _who, event in _REAL_EVENT_SHAPES:
            kind = str(event.get("event") or "")
            for field in event:
                if any(word in field for word in
                       ("seat", "sender", "recipient", "actor", "author",
                        "custodian", "acted_by")) \
                        and not field.endswith("_family") \
                        and isinstance(event[field], str):
                    shaped.add((kind, field))
        # THE MUST-HIT PAIR: the scan finds one field that IS read and one that
        # is EXCLUDED, so neither half of the decision below is vacuous.
        self.assertIn(("dispatch", "sender"), shaped,
                      "the scan did not find the field the registry reads, so "
                      "it is not looking at producer-shaped events")
        self.assertIn(("close", "original_author"), shaped,
                      "the scan did not find the mentioned party the exclusion "
                      "table is written about")

        def undecided(pairs):
            return sorted(
                (kind, field) for kind, field in pairs
                if field not in dispatches.LEDGER_EVENT_ACTORS.get(kind, ())
                and (kind, field) not in dispatches.LEDGER_NOT_AN_ACTOR)

        # THE POSITIVE CONTROL ON THE DECISION ITSELF, unconditionally.
        planted = ("dispatch", "undecided_seat")
        self.assertEqual(undecided(shaped | {planted}), [planted],
                         "the decision cannot name an undecided field, so an "
                         "empty answer from it measures nothing")
        self.assertEqual(undecided(shaped), [],
                         "a seat-shaped field on a producer-shaped event is "
                         "neither read as the hand nor excluded with a reason")


    def test_a_RETIREMENT_credits_the_acting_seat_ITS_WRITER_recorded(self):  # noqa: VACUOUS_ASSERTION — the positive reading runs FIRST and unconditionally on the same observable (sorted(index) == ["retiring-seat"], newest == _ago(9)); the empty-index assertion below is the actor-removed control on that same field
        """The kind the hand-built table missed. `_record_retire_proven`
        refuses to append at all without an acting seat token, so this field is
        always there to read."""
        retire = _one_shape("retire", ts=_ago(9))
        index, doubt, newest, err = _activity_from(*_prefix_for(retire))
        self.assertIsNone(err, err)
        self.assertEqual(index.get("retiring-seat"), _ago(9),
                         "a retirement credited nobody, so a seat whose only "
                         "recent act is one still reads as silent")
        self.assertEqual(newest, _ago(9))
        self.assertEqual(doubt, {},
                         "a retirement the fold ACCEPTED still put doubt on "
                         "somebody, so the two stores are not exclusive")
        # THE CONTROL ON THE SAME FIELD: with the actor removed the kind
        # contributes nothing rather than guessing, so the reading above is of
        # `retire_seat` and not of the kind's mere presence. It is also the only
        # assertion here that survives the WRITER refusing such a row at all —
        # `_record_retire_proven` will not append without a seat token — so this
        # is replay's half of a contract the two halves must share.
        anonymous = dict(retire)
        anonymous.pop("retire_seat")
        index, doubt, _newest, err = _activity_from(*_prefix_for(anonymous))
        self.assertIsNone(err, err)
        self.assertNotIn("retiring-seat", set(index) | set(doubt),
                         "a retirement with no actor on it named somebody")

    def test_the_registry_names_the_kinds_THIS_module_appends(self):
        """THE REGISTRY IS A REGISTRY OF THE DISPATCH LEDGER'S KINDS, and the
        two kinds that go to the ATTEST ledger are the discrimination: a reader
        that took every event literal in the producer module would answer for
        events that never reach the ledger it reads."""
        self.assertIn("retire", dispatches.LEDGER_EVENT_ACTORS,
                      "the kind this reader missed is still unregistered")
        self.assertNotIn("done", dispatches.LEDGER_EVENT_ACTORS,
                         "an attest-ledger kind is registered as a dispatch "
                         "ledger kind")
        self.assertNotIn("intent", dispatches.LEDGER_EVENT_ACTORS)


class AnInvalidVerdictBindingCreditsNOTHINGTest(RetireBase):
    """A VERDICT'S HAND IS RESOLVED THROUGH THE LEDGER'S OWN VALIDATOR.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the index read
    `verdict_author_runtime_evidence.identity` raw. An expanding index is only
    safe while it REFUSES more, and crediting an unvalidated envelope does the
    opposite in the one direction that matters: a name the ledger never validly
    recorded gains a FIRST timestamp, and an OLD first timestamp turns "the
    author of nothing, ever" — a refusal — into a measured durable absence,
    which is a permit on somebody's rows.
    """

    SEAT = "unrostered-reviewer"
    OTHER = "ghost-reviewer"

    def _world(self):
        return dict(roster={"decoy-seat": {"session": "s"}, self.OTHER: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"},
                              {"seat": self.OTHER, "presence": "absent"}])

    def _activity(self, verdict):
        """The subject's ONLY trace is the verdict handed in, so the index has
        nothing else to credit it with. A decoy authors today, which keeps the
        index current either way — without it a refusal would come from the
        staleness rung and the arms would agree on the answer while disagreeing
        about which rung gave it."""
        events = _acts((self.OTHER, "send", 30), ("decoy-seat", "send", 0))
        events.append(_obligation(self.SEAT, rid=_VERDICT_ID, ts=_ago(31)))
        if verdict is not None:
            events.append(verdict)
        return _activity_from(*events)

    def _tampered(self):
        """(why, event) per way a real envelope goes bad, each produced by
        breaking ONE binding the ledger checks."""
        valid = _bound_verdict(self.SEAT, ts=_ago(30), seq=1)
        return (
            ("the anchor does not key the envelope it rides on",
             dict(valid, verdict_author_runtime_anchor="f" * 32)),
            ("the authority proof names another seat's roster identity",
             dict(valid, verdict_author_runtime_evidence=dict(
                 valid["verdict_author_runtime_evidence"],
                 authority=dict(
                     valid["verdict_author_runtime_evidence"]["authority"],
                     roster_identity="somebody-else")))),
            ("the envelope's session contradicts the event's",
             dict(valid, verdict_author_session="0" * 36)),
            ("the envelope names a seat the obligation was not addressed to",
             dict(valid, verdict_author_runtime_evidence=dict(
                 valid["verdict_author_runtime_evidence"],
                 identity="squatter-seat"))),
        )

    def test_a_VALID_old_envelope_is_the_control_and_IS_credited(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control: it asserts an EXACT stamp (index.get(SEAT) == _ago(30)) and an exact newest, and the assertIsNone is on the reader's error channel, whose absence the exact stamps below make non-vacuous
        """THE POSITIVE HALF, STATED FIRST: the producer-built envelope credits
        the seat at the stamp it carries. Without this the arm below could pass
        against a reader that credits no verdict at all."""
        index, _doubt, newest, err = self._activity(
            _bound_verdict(self.SEAT, ts=_ago(30), seq=1))
        self.assertIsNone(err, err)
        self.assertEqual(index.get(self.SEAT), _ago(30),
                         "the ledger's own validator accepts this envelope and "
                         "the index still credited nobody")
        self.assertEqual(newest, _ago(0))

    def test_each_broken_binding_credits_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the unconditional control above the loop asserts index.get(SEAT) == _ago(30) for the UNBROKEN envelope on the SAME observable, so a reader that credits nothing at all fails there first
        """ONE BINDING BROKEN AT A TIME, and every one of them must cost the
        whole credit: a partial check is the reading this cures."""
        control, _doubt, _newest, err = self._activity(
            _bound_verdict(self.SEAT, ts=_ago(30), seq=1))
        self.assertIsNone(err, err)
        self.assertEqual(control.get(self.SEAT), _ago(30),
                         "positive control: the intact envelope credits the "
                         "seat, so the misses below are discriminations")
        for why, event in self._tampered():
            with self.subTest(why=why):
                index, _doubt, _newest, err = self._activity(event)
                self.assertIsNone(err, err)
                self.assertIsNone(index.get(self.SEAT),
                                  "an envelope whose binding the ledger "
                                  "REJECTS manufactured a timestamp: %s" % why)

    def test_the_credit_arms_survive_a_SECOND_BOUNDARY(self):  # noqa: VACUOUS_ASSERTION — the observable is the two DELEGATED arms, each of which asserts an EXACT stamp unconditionally (index.get(SEAT) == _ago(30), newest == _ago(0)); the assertNotEqual here is the MUST-HIT that proves this world's clock really crosses a second boundary, without which the delegation would be green about nothing
        """THE FLAKE THE TWO ARMS ABOVE ONCE HAD, DRIVEN DETERMINISTICALLY.

        Each of them mints `_ago(30)` INTO an envelope and then names THE SAME
        EXPRESSION as its expectation, so while the fixture read the wall clock
        per call the two ends of that equality were two separate readings: one
        second apart whenever a second boundary fell between them, and equal on
        every other run. That is how this class went red in a whole-suite gate
        and green on the same tree on the next.

        IT DRIVES THE ARMS THEMSELVES, never a copy of their bodies — a
        transcribed body would go on passing after theirs changed — under a
        clock whose every read lands in a LATER second.

        THE MUST-HIT COMES FIRST, because a control over a clock that never
        moves proves nothing: `now=time.time()` is the unpinned reading, and
        two of them here DISAGREE, so this world really does straddle the
        boundary that the pinned expression below must be indifferent to.
        """
        with mock.patch("time.time",
                        side_effect=itertools.count(_FIXTURE_NOW, 1.0)):
            self.assertNotEqual(_ago(30, now=time.time()),
                                _ago(30, now=time.time()),
                                "two unpinned readings agreed, so this clock "
                                "crosses no second boundary and the arms below "
                                "run in the world they already run in")
            self.test_a_VALID_old_envelope_is_the_control_and_IS_credited()
            self.test_each_broken_binding_credits_NOTHING()
            self.assertEqual(_ago(30), _ago(30),
                             "one expression still reads the moving clock "
                             "twice, so every arm in this module comparing a "
                             "minted stamp to its own spelling is a coin flip")

    def test_an_invalid_old_envelope_does_not_become_a_PERMIT(self):
        """THE COUNTERTRACE END TO END, through the shipped instrument. A
        never-validly-seen subject named in an old broken envelope must refuse
        as a name helm never saw act — not admit on a month of manufactured
        silence."""
        broken = self._tampered()[0][1]
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(broken), **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertIn("the author of nothing, ever", why,
                      "the refusal does not name the never-seen rung, so the "
                      "broken envelope was credited somewhere")
        # THE POSITIVE CONTROL ON THE SAME DOOR: swap the broken envelope for
        # the VALID one of the same age and this identical world permits, so
        # the refusal above is the binding's doing and not a door that refuses
        # every unrostered name.
        state, why = landreq.seat_reach(
            self.SEAT,
            activity=self._activity(_bound_verdict(self.SEAT, ts=_ago(30), seq=1)),
            **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)

    def test_the_retire_dry_run_REFUSES_on_a_manufactured_authorship(self):
        """THROUGH THE REAL VERB, because an index is only worth what the door
        does with it."""
        row = self.verdicted(polarity="fix", author=self.SEAT,
                             reviewer=self.OTHER)
        before = self.ledger_state(row["id"])
        broken = self._tampered()[0][1]
        ctx = {"snapshot": dispatches.snapshot()[0],
               "reach": dict(self._world(), activity=self._activity(broken))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=ctx)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("the author of nothing, ever", why)
        # THE POSITIVE CONTROL: the same row and world with the VALID envelope
        # of the same age admits, so this reason is not simply unreachable.
        valid = {"snapshot": dispatches.snapshot()[0],
                 "reach": dict(self._world(), activity=self._activity(
                     _bound_verdict(self.SEAT, ts=_ago(30), seq=1)))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=valid)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"],
                        "the positive control did not admit, so the refusal "
                        "above says nothing about the binding")


class ARetirementTODAYIsWorkTest(RetireBase):
    """A SEAT WHOSE ONLY RECENT ACT IS A RETIREMENT IS AT WORK.

    The administrative terminal is itself a dispatch-ledger event with an acting
    seat on it. Under a reader whose kind table was written by hand, that seat's
    month-old send was the newest thing anybody could see about it — so the door
    was about to spend the rows of a seat that had been clearing rows all day.
    """

    SEAT = "unrostered-sweeper"
    OTHER = "ghost-reviewer"

    def _world(self):
        return dict(roster={"decoy-seat": {"session": "s"}, self.OTHER: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"},
                              {"seat": self.OTHER, "presence": "absent"}])

    def _activity(self, with_retirement):
        events = _acts((self.SEAT, "send", 30), (self.OTHER, "send", 30),
                       ("decoy-seat", "send", 0))
        if with_retirement:
            events.extend(_prefix_for(_one_shape("retire", ts=_ago(0),
                                                 retire_seat=self.SEAT)))
        return _activity_from(*events)

    def test_the_index_sees_the_retirement_this_seat_wrote_today(self):  # noqa: VACUOUS_ASSERTION — both readings assert an EXACT stamp on the same observable (index.get(SEAT)), _ago(0) with the retirement and _ago(30) without it
        """THE PREMISE, PINNED BEFORE THE DOOR IS ASKED."""
        index, _doubt, newest, err = self._activity(with_retirement=True)
        self.assertIsNone(err, err)
        self.assertEqual(index.get(self.SEAT), _ago(0),
                         "the retirement this seat recorded today is invisible "
                         "to the index, so its month-old send is all the door "
                         "will see")
        self.assertEqual(newest, _ago(0))
        quiet, _doubt, _newest, err = self._activity(with_retirement=False)
        self.assertIsNone(err, err)
        self.assertEqual(quiet.get(self.SEAT), _ago(30))

    def test_a_retirement_today_reads_ACTING(self):
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(with_retirement=True),
            **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertIn("ACTING", why,
                      "the refusal does not name the silence rung, so an "
                      "operator cannot tell which instrument saved this seat")
        # THE POSITIVE CONTROL: strip only the retirement and this identical
        # world PERMITS.
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(with_retirement=False),
            **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)

    def test_the_retire_dry_run_on_such_a_row_REFUSES(self):
        row = self.verdicted(polarity="fix", author=self.SEAT,
                             reviewer=self.OTHER)
        before = self.ledger_state(row["id"])
        ctx = {"snapshot": dispatches.snapshot()[0],
               "reach": dict(self._world(),
                             activity=self._activity(with_retirement=True))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=ctx)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("ACTING", why)
        quiet = {"snapshot": dispatches.snapshot()[0],
                 "reach": dict(self._world(),
                               activity=self._activity(with_retirement=False))}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=quiet)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"],
                        "the positive control did not admit, so the refusal "
                        "above says nothing about the retirement")


class TheUNROSTEREDExitOrderIsDECLAREDAndWALKEDTest(RetireBase):
    """EVERY UNKNOWN EXIT PRECEDES THE ONE ADMIT, walked through the shipped
    rung with a spy on the real exit door rather than read off its prose.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: every fact `_unrostered_absence`
    learned to read arrived as one more rung appended at the bottom, and the
    two PROJECTION rungs — the ones deciding whether the estate was read about
    this name AT ALL — arrived last while belonging first. So a projection that
    explicitly DESCRIBED the subject and could not attribute its beat was
    overtaken by rungs about the fleet, the panes and the ledger, and the rung
    admitted a retirement on an estate it had never positively read. An
    instrument that acquires a rung acquires an ORDERING question, and a
    declared list is worth nothing if the code answers in a different order.
    """

    SEAT = "unrostered-subject"

    #: exit id -> the ONE admit condition `_world` breaks to reach it. Typed
    #: here and then checked against the declared table, so an exit this arm
    #: cannot drive is a red line instead of an untested rung.
    _BREAKS = ("projection-unreadable", "projection-describes-the-subject",
               "projection-describes-no-live-seat", "pane-census-names-nobody",
               "absence-has-no-measured-duration")

    #: The one pair of exits that cannot both hold: they are two VALUES of one
    #: producer word, so no world satisfies both and the pairwise walk below
    #: must skip exactly this pair and no other.
    _EXCLUSIVE = ("projection-unreadable", "projection-describes-the-subject")

    def _world(self, broken=()):
        """The ADMIT world with the named conditions broken, one field each.

        Every other condition stays TRUE in each perturbation, which is what
        makes each answer below a statement about precedence: the rung had
        everything it needed to admit and answered the earlier exit anyway.
        """
        reason = landreq.PRESENCE_MISS
        if "projection-unreadable" in broken:
            reason = landreq.PRESENCE_UNREADABLE
        if "projection-describes-the-subject" in broken:
            reason = landreq.PRESENCE_UNATTRIBUTED
        word = ("absent" if "projection-describes-no-live-seat" in broken
                else "fresh")
        agents = ({"by_seat": {}} if "pane-census-names-nobody" in broken
                  else {"by_seat": {"decoy-seat": [1]}})
        quiet = (0 if "absence-has-no-measured-duration" in broken else 30)
        return dict(
            name=self.SEAT,
            presence=[{"seat": "decoy-seat", "presence": word}],
            agents=agents,
            activity=_activity_from(
                *_acts((self.SEAT, "send", quiet),
                       ("decoy-seat", "send", 0))),
            presence_reason=reason)

    @contextlib.contextmanager
    def _spy(self):
        """The REAL exit door, wrapped so an arm can read WHICH exit answered.

        The prose cannot answer that question and an arm matching on it would
        be a measurement bound to a sentence; the spy delegates to the shipped
        function, so the state and the detail are production's own.
        """
        real = landreq._unrostered_exit
        seen = []

        def watch(exit_id, detail):
            seen.append(exit_id)
            return real(exit_id, detail)

        with mock.patch.object(landreq, "_unrostered_exit", watch):
            yield seen

    def _answer(self, broken=()):
        """(exit id, state, detail) from the shipped rung on that world."""
        with self._spy() as seen:
            state, why = landreq._unrostered_absence(**self._world(broken))
        self.assertEqual(len(seen), 1,
                         "the rung answered through %d exits, so the walk "
                         "below cannot say which one decided: %s"
                         % (len(seen), seen))
        return seen[0], state, why

    def test_the_spy_reads_the_REAL_exit_and_the_ADMIT_is_reachable(self):
        """THE UNCONDITIONAL CONTROL FOR EVERY ARM IN THIS CLASS. The unbroken
        world admits, through the declared `absent` exit, with the state the
        table declares — so a refusal below is a precedence fact and not a door
        that refuses every unrostered name, and the spy is reading real code."""
        exit_id, state, why = self._answer()
        self.assertEqual(exit_id, "absent", why)
        self.assertEqual(state, landreq.SEAT_ABSENT, why)
        self.assertEqual(state, landreq._UNROSTERED_EXIT_STATE["absent"],
                         "the rung answered a state the table does not declare "
                         "for the exit it used")

    def test_the_declared_order_IS_the_source_order(self):
        """A LIST THE CODE DOES NOT FOLLOW IS A COMMENT. The exit ids appear in
        `_unrostered_absence` in the order it asks them, so the two orders are
        comparable and drift between them is a red line here."""
        import inspect
        import re
        src = inspect.getsource(landreq._unrostered_absence)
        in_source = re.findall(r'_unrostered_exit\(\s*"([a-z-]+)"', src)
        # THE MUST-HIT: the scan found the exit that every arm above reaches,
        # so an empty or rotted pattern cannot satisfy the comparison quietly.
        self.assertIn("absent", in_source,
                      "the source scan found no exit at all, so the order "
                      "comparison below is between two empty lists")
        declared = [exit_id for exit_id, _state, _why
                    in landreq._UNROSTERED_EXITS]
        self.assertEqual(in_source, declared,
                         "the rung asks its exits in an order the declared "
                         "table does not describe")

    def test_every_declared_exit_is_REACHED_and_answers_its_declared_state(self):  # noqa: VACUOUS_ASSERTION — the coverage comparison is bracketed by unconditional positive readings: `_BREAKS` is asserted non-empty by its equality with the declared UNKNOWN ids, and the walk asserts an exact exit id and an exact state per declared exit
        """AN EXIT NOBODY CAN REACH IS PROSE. Each declared exit is driven
        through the shipped rung and must answer with the state the table
        declares for it."""
        declared = [exit_id for exit_id, _state, _why
                    in landreq._UNROSTERED_EXITS]
        self.assertEqual(sorted(self._BREAKS), sorted(set(declared) - {"absent"}),
                         "a declared UNKNOWN exit has no perturbation in this "
                         "class, so it is an undriven rung")
        for exit_id in declared:
            with self.subTest(exit=exit_id):
                broken = () if exit_id == "absent" else (exit_id,)
                got, state, why = self._answer(broken)
                self.assertEqual(got, exit_id, why)
                self.assertEqual(
                    state, landreq._UNROSTERED_EXIT_STATE[exit_id], why)

    def test_every_UNKNOWN_condition_BEATS_the_admit(self):  # noqa: VACUOUS_ASSERTION — test_the_spy_reads_the_REAL_exit_and_the_ADMIT_is_reachable asserts SEAT_ABSENT on the same observable from the same rung with nothing broken, so a rung that never admits fails there
        """THE INVARIANT, ONE CONDITION AT A TIME: with every other admit
        condition TRUE, each UNKNOWN condition still costs the permit."""
        for exit_id in self._BREAKS:
            with self.subTest(exit=exit_id):
                got, state, why = self._answer((exit_id,))
                self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
                self.assertNotEqual(state, landreq.SEAT_ABSENT,
                                    "an unmeasured estate reached the permit "
                                    "state through %s: %s" % (got, why))

    def test_an_EARLIER_exit_answers_when_a_LATER_one_ALSO_holds(self):  # noqa: VACUOUS_ASSERTION — the final assertion is an unconditional EXACT count of the pairs walked (len(order) choose 2, less the one mutually exclusive pair), so a loop that ran zero times fails there rather than passing empty
        """PRECEDENCE, PAIRWISE. Two conditions true at once must be answered by
        the one the table puts first — that is the whole content of an order,
        and a rung appended at the bottom breaks exactly this."""
        order = [exit_id for exit_id, _state, _why
                 in landreq._UNROSTERED_EXITS if exit_id != "absent"]
        self.assertEqual(sorted(self._EXCLUSIVE), sorted(
            name for name in order if name in self._EXCLUSIVE),
            "the pair this walk skips is not a pair of declared exits")
        pairs = 0
        for i, earlier in enumerate(order):
            for later in order[i + 1:]:
                if {earlier, later} == set(self._EXCLUSIVE):
                    continue
                with self.subTest(earlier=earlier, later=later):
                    got, state, why = self._answer((earlier, later))
                    self.assertEqual(got, earlier,
                                     "%s answered where %s is declared first: "
                                     "%s" % (got, earlier, why))
                    self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
                pairs += 1
        # THE MUST-HIT: the walk actually ran, so a generator that produced no
        # pair at all cannot pass this arm silently.
        self.assertEqual(pairs, len(order) * (len(order) - 1) // 2 - 1,
                         "the pairwise walk skipped more than the one "
                         "mutually exclusive pair")


class AnUnverifiedPROJECTIONRowIsNotAProjectionMISSTest(RetireBase):
    """A PROJECTION THAT NAMES THE SUBJECT HAS NOT MISSED IT.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the roster is read before the
    projection, so a name with no roster row reached the unrostered rung while
    the projection DID carry a row for it — one whose beat could not be
    attributed to it. The rung's first documented prerequisite is that no row in
    the projection is this name, and it never asked: it counted the DECOY's live
    row, found a declaring pane for the decoy, dated the subject's old silence
    and admitted, with the words "was NOT measured" carried into its own
    admission. The projection had explicitly declined to measure the subject and
    the door spent its rows anyway.
    """

    SEAT = "unrostered-subject"
    OTHER = "ghost-reviewer"

    def _world(self, with_subject_row):
        """The eligible projection-MISS world, plus one unverified row naming
        the subject. Nothing else moves: the decoy still supplies the live row
        and the only declaring pane, and the subject is still off the roster."""
        presence = [{"seat": "decoy-seat", "presence": "fresh",
                     "runtime": {"family": "fixture", "backend": "native"}},
                    {"seat": self.OTHER, "presence": "absent"}]
        if with_subject_row:
            presence.append({"seat": self.SEAT, "presence": "fresh",
                             "unverified": True,
                             "runtime": {"family": "fixture",
                                         "backend": "native"}})
        return dict(roster={"decoy-seat": {"session": "s"}, self.OTHER: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=presence)

    def _activity(self):
        """The subject is a month quiet and the index is current, so every
        other rung of the permit path is satisfied."""
        return _activity_from(
            *_acts((self.SEAT, "send", 30), (self.OTHER, "send", 30),
                   ("decoy-seat", "send", 0)))

    def test_the_unverified_row_turns_the_admit_into_a_REFUSAL(self):
        """THE POSITIVE CONTROL FIRST, unconditionally: without the row this
        identical world PERMITS, so the refusal is the row's doing."""
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(),
            **self._world(with_subject_row=False))
        self.assertEqual(state, landreq.SEAT_ABSENT,
                         "the projection-miss world does not admit, so this "
                         "class cannot say what the row changed: %s" % why)
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(),
            **self._world(with_subject_row=True))
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertNotEqual(state, landreq.SEAT_ABSENT,
                            "an explicitly unmeasured subject reached the "
                            "permit state: %s" % why)
        self.assertIn("DESCRIBED", why,
                      "the refusal does not say the projection described this "
                      "name, so an operator cannot tell a described subject "
                      "from a missed one: %s" % why)

    def test_the_retire_dry_run_REFUSES_on_a_described_subject(self):
        """THROUGH THE REAL VERB, because a rung is only worth what the door
        does with it."""
        row = self.verdicted(polarity="fix", author=self.SEAT,
                             reviewer=self.OTHER)
        before = self.ledger_state(row["id"])
        described = {"snapshot": dispatches.snapshot()[0],
                     "reach": dict(self._world(with_subject_row=True),
                                   activity=self._activity())}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True,
                                  ctx=described)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("DESCRIBED", why)
        # THE POSITIVE CONTROL ON THE SAME DOOR: drop only the unverified row
        # and this row is retirable, so the refusal is not an unreachable one.
        missed = {"snapshot": dispatches.snapshot()[0],
                  "reach": dict(self._world(with_subject_row=False),
                                activity=self._activity())}
        out, why = landreq.retire(row["id"], "author-unresolvable",
                                  seat="integrator", dry_run=True, ctx=missed)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"],
                        "the positive control did not admit, so the refusal "
                        "above says nothing about the projection row")


class TheActivityReadingIsHERMETICTest(RetireBase):
    """THE READING IS OF THE HOME THIS SUITE RUNS UNDER, and nothing else.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: two ordinary discovered arms reached
    `home.default_home()` explicitly, so a test in this file opened the FLEET's
    dispatch ledger. The consequences are all three at once — it SKIPPED on a
    host with no such file, FAILED on a host where it was empty, and changed its
    assertions for identical source every time somebody on this fleet sent a
    dispatch. A suite that reads production has no oracle: a red line means the
    tree broke, or that a seat did something this afternoon, and nobody can tell
    which.

    THE MUST-HIT IS THE POINT OF THE FIRST ARM. Asserting a path is under
    HELM_HOME proves nothing about what the code opened; asserting that the
    reading names EXACTLY the seats this arm planted does, because the fleet
    ledger carries twenty-eight of them.
    """

    def _fleet_ledger(self):
        """The path this suite must NOT read. Named, never opened."""
        return os.path.join(home.default_home(), home.GLOBAL, dispatches.LEDGER)

    def test_the_projection_reads_the_FIXTURE_ledger_and_not_the_FLEET_one(self):
        planted = dispatches.ledger_path()
        fixture = os.path.realpath(os.environ["HELM_HOME"])
        self.assertTrue(
            os.path.realpath(planted).startswith(fixture + os.sep),
            "the projection's own source path is outside the fixture home: %s"
            % planted)
        self.assertNotEqual(os.path.realpath(planted),
                            os.path.realpath(self._fleet_ledger()),
                            "the projection would read the fleet's ledger")
        # THE MUST-HIT: one planted obligation, and the reading names its sender
        # AND NOBODY ELSE. A read that had escaped to the fleet ledger would
        # carry the whole board's seats and pass a bare `assertIn`.
        self.open_row(author="hermetic-sender", reviewer="ghost-reviewer")
        validated, doubt, newest, unavailable = dispatches.seat_activity()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(sorted(validated), ["hermetic-sender"],
                         "the reading names a seat this arm never planted, so "
                         "it read some other corpus")
        self.assertEqual(doubt, {})
        self.assertTrue(newest, "the reading dated nothing at all")

    def test_the_SHIPPED_writers_produce_a_reading_the_fold_credits_in_full(self):
        """THE PRODUCERS, NOT A FIXTURE'S IDEA OF THEM. `send`, the delivery
        marker and `mark_verdict` write the ledger; the projection then credits
        the sender for opening it and the reviewer through the binding the fold
        ACCEPTED. Nothing is left unattributed, which is what makes the doubt
        store in the countertraces below a signal rather than a default."""
        self.verdicted(polarity="fix", author="fixture-sender",
                       reviewer="fixture-reviewer")
        validated, doubt, _newest, unavailable = dispatches.seat_activity()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(sorted(validated),
                         ["fixture-reviewer", "fixture-sender"],
                         "a ledger written entirely by the shipped producers "
                         "did not credit both hands")
        self.assertEqual(doubt, {},
                         "the shipped writers produced an act the projection "
                         "could not attribute")


class AVerdictMayNotSupplyTheIdentityItIsCHECKEDAgainstTest(RetireBase):
    """A FORGED ENVELOPE CANNOT NOMINATE ITS OWN VALIDATOR INPUT.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the reader carried each row's
    recipient forward from ANY event that had the field, including the verdict
    it was about to validate. So an obligation addressed to `right-reviewer`
    could receive a verdict carrying a self-consistent envelope for
    `wrong-reviewer` plus a top-level `recipient` naming that same seat — and the
    envelope was then checked against the identity IT had just supplied. It
    passed. `wrong-reviewer` gained an OLD FIRST TIMESTAMP, and an old first
    timestamp is what turns "the author of nothing, ever" — a refusal — into a
    measured durable absence, which is a permit.

    THE CURE IS STRUCTURAL RATHER THAN A CHECK: attribution is computed by the
    fold, whose accepted state holds the recipient the obligation was OPENED
    with, and `_apply` never rewrites it. There is no raw field left to read.

    Internally inconsistent by construction — no producer writes this — which is
    exactly what a countertrace is.
    """

    ASKED = "right-reviewer"
    SEAT = "wrong-reviewer"

    def _events(self, carry_raw_recipient, for_seat=None):
        """The obligation, a verdict bound for `for_seat`, and a decoy.

        `carry_raw_recipient` is a review's falsifier: the answer must be the
        SAME with and without that field, because the field must not be read.
        """
        forged = _bound_verdict(for_seat or self.SEAT, rid=_VERDICT_ID,
                                ts=_ago(30), seq=1)
        if carry_raw_recipient:
            forged["recipient"] = for_seat or self.SEAT
        return ([_obligation(self.ASKED, rid=_VERDICT_ID, ts=_ago(31)), forged]
                + _acts(("decoy-seat", "send", 0)))

    def _world(self):
        return dict(roster={"decoy-seat": {"session": "s"}, self.ASKED: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"},
                              {"seat": self.ASKED, "presence": "absent"}])

    def test_the_forged_recipient_is_credited_in_NEITHER_store(self):  # noqa: VACUOUS_ASSERTION — every absence here is preceded UNCONDITIONALLY by assertEqual(<same store>.get(ASKED), _ago(30)) on the very reading it is about, plus an unconditional credited-store control on the valid variant; CLEAN under _analyze_source in isolation
        """AND IDENTICALLY WITH OR WITHOUT THE RAW FIELD, which is the whole
        claim: the field is not an input."""
        # THE POSITIVE CONTROL FIRST AND UNCONDITIONALLY: the same expression,
        # with the envelope built for the seat the obligation ADDRESSED, puts
        # that name in the credited store — so a reader that credits nobody
        # fails here instead of passing every miss below.
        validated, doubt, _newest, err = _activity_from(
            *self._events(carry_raw_recipient=False, for_seat=self.ASKED))
        self.assertIsNone(err, err)
        self.assertEqual(validated.get(self.ASKED), _ago(30),
                         "no verdict is credited at all, so the absences below "
                         "are a blind reader rather than a discrimination")
        self.assertEqual(doubt, {},
                         "an attributable ledger produced doubt, so the doubt "
                         "readings below are not about the forgery")
        # BOTH WORLDS UNROLLED rather than looped, so no assertion here is
        # behind a branch and the two answers can be compared as values.
        carried, carried_doubt, _newest, err = _activity_from(
            *self._events(carry_raw_recipient=True))
        self.assertIsNone(err, err)
        # THE ADDRESSED SEAT KEEPS THE DOUBT: something happened on ITS
        # obligation that nobody could attribute, which is the true reading —
        # it refuses, where crediting the forger permitted.
        self.assertEqual(carried_doubt.get(self.ASKED), _ago(30),
                         "the refused verdict left no doubt on the seat whose "
                         "obligation it was written on")
        self.assertNotIn(self.SEAT, carried,
                         "a verdict nominated the identity its own binding was "
                         "validated against")
        self.assertNotIn(self.SEAT, carried_doubt,
                         "the forged name gained a dated act it could then be "
                         "measured silent against")
        bare, bare_doubt, _newest, err = _activity_from(
            *self._events(carry_raw_recipient=False))
        self.assertIsNone(err, err)
        self.assertEqual(bare_doubt.get(self.ASKED), _ago(30))
        self.assertNotIn(self.SEAT, bare)
        self.assertNotIn(self.SEAT, bare_doubt)
        self.assertEqual((sorted(carried), sorted(carried_doubt)),
                         (sorted(bare), sorted(bare_doubt)),
                         "the raw `recipient` field changed the answer, so it "
                         "is still an input to attribution")

    def test_the_SAME_envelope_bound_to_the_ADDRESSED_seat_IS_credited(self):  # noqa: VACUOUS_ASSERTION — the control for the empty doubt store cannot share a binding with it (it is empty in THIS world by construction); the unconditional read above proves the SAME expression FILLS that store when only the envelope's seat changes
        """THE POSITIVE CONTROL, on the same expression: only the seat the
        envelope is built for changes, and the credit appears. Without this the
        misses above could come from a reader that credits no verdict at all."""
        # THE CONTROL ON THE EMPTY OBSERVABLE, unconditionally and first: the
        # SAME expression with the envelope bound to the wrong seat DOES fill
        # the doubt store, so asserting it empty here is a discrimination.
        _v, doubt, _n, err = _activity_from(
            *self._events(carry_raw_recipient=False))
        self.assertIsNone(err, err)
        self.assertEqual(doubt.get(self.ASKED), _ago(30),
                         "the doubt store cannot fill on this expression at "
                         "all, so asserting it empty proves nothing")
        validated, doubt, _newest, err = _activity_from(
            *self._events(carry_raw_recipient=False, for_seat=self.ASKED))
        self.assertIsNone(err, err)
        self.assertEqual(validated.get(self.ASKED), _ago(30),
                         "a verdict bound to the seat its obligation addressed "
                         "was not credited")
        self.assertEqual(doubt, {},
                         "a fully attributable ledger still produced doubt")

    def test_the_forged_name_refuses_as_a_name_helm_NEVER_SAW_ACT(self):
        """END TO END THROUGH THE SHIPPED INSTRUMENT. The forged seat is on no
        roster, in no projection and in no pane — every condition the permit
        needs except a dated act — so the ONLY thing that could admit it is the
        timestamp the forgery manufactured."""
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity_reading(), **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertIn("the author of nothing, ever", why,
                      "the refusal does not name the never-seen rung, so the "
                      "forged envelope was credited somewhere")

    def _activity_reading(self):
        return _activity_from(*self._events(carry_raw_recipient=True))


class AnUnattributableNEWERActIsNotSilenceTest(RetireBase):
    """RECENT WORK NOBODY COULD ATTRIBUTE MUST NOT READ AS A DEPARTURE.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the reader kept only what it could
    CREDIT. A seat with a valid send thirty days back, whose obligation then
    received a verdict TODAY carrying no author binding at all, read as thirty
    days silent — and thirty days of silence spends the row. The act was real,
    the hand was unrecorded, and an index that counts only credited acts
    published the one reading that permits a terminal.

    `mark_verdict` DEFAULTS TO `bind_author=False`, so this is a supported
    producer shape and a supported replay shape, not invented malformed input.
    The correction is not to credit the subject — that would manufacture
    authorship — it is to refuse: the projection cannot establish that the act
    was NOT this seat's.

    AND ONLY INSIDE THE WINDOW. Outside it the seat is silent under both
    readings, which is why this rung moves no answer on the live board: of the
    36 seats the open board names, four carry an unattributable act newer than
    their newest credited one and every one is more than a fortnight old.
    """

    SEAT = "unrostered-subject"
    OTHER = "ghost-reviewer"

    def _world(self):
        return dict(roster={"decoy-seat": {"session": "s"}, self.OTHER: {}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"},
                              {"seat": self.OTHER, "presence": "absent"}])

    def _activity(self, doubt_days=None, doubt_on=None):
        """An old credited send by the subject, a decoy keeping the reading
        current, and optionally an UNBOUND verdict `doubt_days` ago on an
        obligation addressed to `doubt_on` (the subject unless said otherwise).
        """
        declared = [(self.SEAT, "send", 30), (self.OTHER, "send", 30),
                    ("decoy-seat", "send", 0)]
        if doubt_days is not None:
            declared.append((doubt_on or self.SEAT, "unbound-verdict",
                             doubt_days))
        return _activity_from(*_acts(*declared))

    def test_the_reading_keeps_the_unattributable_act_AND_the_credited_one(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an exact stamp equality on a named store, and the control asserts absence on the same key
        """THE PREMISE, PINNED. Two stores, two facts: the subject's credited
        act is a month old and something on its obligation happened today."""
        validated, doubt, newest, err = self._activity(doubt_days=0)
        self.assertIsNone(err, err)
        self.assertEqual(validated.get(self.SEAT), _ago(30),
                         "the subject's credited act moved, so this arm is not "
                         "about an old absence any more")
        self.assertEqual(doubt.get(self.SEAT), _ago(0),
                         "today's unattributable act on this seat's own "
                         "obligation is invisible, so its month of silence is "
                         "all the door will see")
        self.assertEqual(newest, _ago(0),
                         "the reading is not current, so a refusal below would "
                         "come from the staleness rung instead")
        # THE CONTROL ON THE SAME OBSERVABLE: without the unbound verdict the
        # doubt store has nothing for this seat.
        _validated, quiet, _newest, err = self._activity()
        self.assertIsNone(err, err)
        self.assertNotIn(self.SEAT, quiet)

    def test_an_unattributable_act_TODAY_refuses_where_it_once_PERMITTED(self):
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(doubt_days=0), **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertIn("records no hand this ledger can bind", why,
                      "the refusal does not name the unattributable act, so an "
                      "operator cannot tell which instrument saved this seat")
        self.assertNotIn("authored a ledger event", why,
                         "the refusal CLAIMS this seat authored the act — the "
                         "very attribution the fold refused to make")
        # THE POSITIVE CONTROL ON THE SAME DOOR: drop only the unbound verdict
        # and this identical world PERMITS.
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(), **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)

    def test_an_unattributable_act_OUTSIDE_the_window_still_permits(self):
        """THE OTHER DIRECTION, and the reason this rung moves nothing on the
        live board. A seat silent for the window under BOTH readings is silent,
        so doubt older than the window must not refuse — a rung that refused on
        any doubt at all would take the population from every measured absence
        to none."""
        state, why = landreq.seat_reach(
            self.SEAT, activity=self._activity(doubt_days=20), **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)
        self.assertIn("could not attribute", why,
                      "the admission does not disclose the unattributable act "
                      "it measured, so an operator reading the receipt cannot "
                      "see what was weighed")

    def test_somebody_ELSES_unattributable_act_is_not_this_seats_doubt(self):  # noqa: VACUOUS_ASSERTION — the absence is covered UNCONDITIONALLY by assertEqual(elsewhere.get("a-third-seat"), _ago(0)) on the same local plus a doubt-reaches-this-key control aimed at the subject; CLEAN under _analyze_source in isolation
        """THE FALSIFIER a review named: unrelated acts must not be attributed
        to the subject. The doubt is keyed on the obligation's ACCEPTED
        recipient, so an unbound verdict on another seat's row leaves this one
        exactly as it was."""
        # THE CONTROL ON THIS SEAT'S OWN KEY, unconditionally and first: aimed
        # at the subject, the identical expression DOES put doubt on it, so the
        # absence below is about WHOSE obligation the act was on.
        _v, doubt, _n, err = self._activity(doubt_days=0)
        self.assertIsNone(err, err)
        self.assertEqual(doubt.get(self.SEAT), _ago(0),
                         "doubt never reaches this key at all, so asserting it "
                         "absent below measures nothing")
        validated, elsewhere, _newest, err = self._activity(
            doubt_days=0, doubt_on="a-third-seat")
        self.assertIsNone(err, err)
        self.assertEqual(elsewhere.get("a-third-seat"), _ago(0),
                         "the unbound verdict put doubt on nobody, so the "
                         "absence below is a reader that records no doubt")
        self.assertNotIn(self.SEAT, elsewhere,
                         "another seat's unattributable act became this "
                         "seat's doubt")
        state, why = landreq.seat_reach(
            self.SEAT, activity=(validated, elsewhere, _ago(0), None),
            **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)


class AnOpenersINHERITEDAuthorKeysAreNotAVerdictBINDINGTest(RetireBase):
    """THE KEYS AN OPENER CARRIED ARE NOT THE VERDICT'S OWN BINDING.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: `_new_state` opens a native v3 row
    with `dict(row)`, so an opener carrying the three `verdict_author_*` keys
    kept them in the ACCEPTED state. A later verdict carrying NO binding at all
    — `mark_verdict(bind_author=False)`, a supported producer shape — was then
    credited as VALIDATED, because the only thing the crediting rung can see is
    those keys' presence on the accepted state and it could not tell an
    inherited key from one this event supplied. Three null keys on an opener
    therefore MANUFACTURED an old validated first act for the addressed seat,
    and an old validated first act is exactly what turns "the author of nothing,
    ever" — a refusal — into a measured durable absence, which is a permit.

    THE CURE IS A REMOVED SOURCE, NOT A SECOND CHECK: the envelope validator
    that opens the row drops those keys, the same way it already refuses to
    carry a forged `chain_root` through. After it, a key present on a state is
    the fold's own record that it ACCEPTED a binding, and the crediting rung's
    question is answerable from the state alone because nothing else can put it
    there.

    Internally inconsistent by construction — no producer writes an opener with
    verdict-author keys — which is what a countertrace is.
    """

    SEAT = "reviewing-seat"

    def _events(self, inherit, bind=False):
        """The obligation, a verdict on it, and a decoy keeping the index
        current. `inherit` puts the three author keys on the OPENER (the null
        spelling a review named); `bind` makes the verdict carry its OWN
        validated envelope, which is the positive control."""
        opener = _obligation(self.SEAT, rid=_VERDICT_ID, ts=_ago(31))
        if inherit:
            opener.update({field: None for field
                           in dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS})
        verdict = _bound_verdict(self.SEAT, rid=_VERDICT_ID, ts=_ago(30), seq=1)
        if not bind:
            for field in dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS:
                verdict.pop(field, None)
        return [opener, verdict] + _acts(("decoy-seat", "send", 0))

    def _world(self):
        return dict(roster={"decoy-seat": {"session": "s"}},
                    roster_failed=False,
                    agents={"by_seat": {"decoy-seat": [1]}},
                    presence=[{"seat": "decoy-seat", "presence": "fresh"}])

    def test_the_inherited_keys_change_NOTHING_about_the_reading(self):  # noqa: VACUOUS_ASSERTION — the two absences are preceded UNCONDITIONALLY by assertEqual(doubt.get(SEAT), _ago(30)) on the very readings they are about, and the bound control asserts the credited store FILLS on the same expression
        """The FALSIFIER, as the arm: removing only the opener's three
        null extras must not move the verdict between the stores."""
        bare, bare_doubt, _newest, err = _activity_from(
            *self._events(inherit=False))
        self.assertIsNone(err, err)
        self.assertEqual(bare_doubt.get(self.SEAT), _ago(30),
                         "an unbound verdict on this seat's obligation is in "
                         "neither store, so the comparison below is between "
                         "two blind readings")
        self.assertNotIn(self.SEAT, bare)
        carried, carried_doubt, _newest, err = _activity_from(
            *self._events(inherit=True))
        self.assertIsNone(err, err)
        self.assertEqual(carried_doubt.get(self.SEAT), _ago(30),
                         "the opener's inherited keys moved the act out of the "
                         "doubt store, so three null keys still supply a "
                         "binding this event never carried")
        self.assertNotIn(self.SEAT, carried,
                         "an opener's inherited author keys manufactured a "
                         "VALIDATED act for a verdict carrying no binding")
        self.assertEqual((sorted(carried), sorted(carried_doubt)),
                         (sorted(bare), sorted(bare_doubt)),
                         "the opener's extra keys changed the answer, so they "
                         "are still an input to attribution")

    def test_a_verdict_carrying_its_OWN_envelope_is_still_credited(self):  # noqa: VACUOUS_ASSERTION — the control for the empty doubt store cannot share a binding with it (in THIS world it is empty by construction); the unconditional read above proves the SAME expression FILLS that store when only the verdict's own binding changes
        """THE POSITIVE CONTROL, on the same expression with the same inherited
        opener: only the verdict's own binding changes, and the credit appears.
        Without it the misses above would also pass for a reader that credits no
        verdict at all — and the cure would be free to drop every real one."""
        # THE CONTROL ON THE EMPTY OBSERVABLE, unconditionally and first: the
        # SAME expression with the binding removed DOES fill the doubt store, so
        # asserting it empty below is a discrimination.
        _v, doubt, _n, err = _activity_from(*self._events(inherit=True))
        self.assertIsNone(err, err)
        self.assertEqual(doubt.get(self.SEAT), _ago(30),
                         "the doubt store cannot fill on this expression at "
                         "all, so asserting it empty proves nothing")
        validated, doubt, _newest, err = _activity_from(
            *self._events(inherit=True, bind=True))
        self.assertIsNone(err, err)
        self.assertEqual(validated.get(self.SEAT), _ago(30),
                         "a verdict whose own envelope the validator accepts "
                         "was not credited")
        self.assertEqual(doubt, {},
                         "a fully attributable ledger still produced doubt")

    def test_the_manufactured_first_act_no_longer_PERMITS(self):
        """END TO END THROUGH THE SHIPPED DOOR, which is where the harm was.
        The seat is on no roster, in no projection and in no pane; the only
        thing that could admit it is a dated validated act, and the only source
        of one is the opener's three null keys."""
        state, why = landreq.seat_reach(
            self.SEAT, activity=_activity_from(*self._events(inherit=True)),
            **self._world())
        self.assertEqual(state, landreq.SEAT_UNKNOWN, why)
        self.assertIn("the author of nothing, ever", why,
                      "the never-seen rung did not fire, so the inherited keys "
                      "still manufacture a first act to measure silence from")
        # THE POSITIVE CONTROL ON THE SAME DOOR: give the verdict its own
        # accepted binding and this identical world PERMITS.
        state, why = landreq.seat_reach(
            self.SEAT,
            activity=_activity_from(*self._events(inherit=True, bind=True)),
            **self._world())
        self.assertEqual(state, landreq.SEAT_ABSENT, why)


class ACorruptCOMPLETELineRefusesTheSingleIDRetireTest(RetireBase):
    """ONE READ PATH, AND IT IS THE STRICT ONE THE SWEEP ALREADY USES.

    FAILURE SCENARIO THIS CLASS EXISTS FOR: the single-ID retire door read the
    dispatch ledger LENIENTLY — a complete corrupt JSONL line was silently
    skipped — and so did the activity index and the LOCKED writer that
    authorizes the append. A current act on a target party's obligation sitting
    on such a line was therefore invisible to every measurement retirement
    makes, and this lane's unrostered eligibility turns invisible activity into
    PERMISSION: the row is retired on measured silence the ledger contradicts.
    The sweep never had this hole, because `project_raw` reads strictly and
    refuses a corrupt ledger before it classifies anything.

    THE CURE IS THE SWEEP'S DOOR, not a check beside the lenient one: the
    context, the activity read and the locked re-measure all go through the
    strict read, and a corrupt COMPLETE line is now an UNAVAILABLE instrument.
    An unavailable instrument refuses — a retirement is only ever authorized by
    a positive finding, and a ledger nobody can read in full has made none.

    NOT the unterminated tail, which is outside the durability boundary and is
    ignored in both modes by design.
    """

    REASON = "author-unresolvable"

    def _row(self):
        return self.verdicted(polarity="fix", author="deleted-author")

    def _world(self):
        return self.reach(roster=self.roster({"seat-a": "s"}),
                          panes=self.panes("seat-a"))

    def _corrupt_the_ledger(self):
        """One COMPLETE, newline-terminated line that is not a JSON object.

        Complete is the whole point: the lenient reader skips exactly this and
        keeps every other row, which is why the omission left no trace anywhere
        a caller could see."""
        path = dispatches.ledger_path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"v": 3, "event": "verdict", "id": "%s"\n'
                         % ("9" * 32))
        return path

    def test_the_door_ADMITS_before_the_corruption_and_REFUSES_after(self):
        """THE CONTROL IS THE FIRST HALF, unconditionally: this exact call on
        this exact world admits, so the refusal below is the corrupt line and
        not a fixture that never reached the measurement."""
        row = self._row()
        r_patch, a_patch = self._world()
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], self.REASON, seat="integrator",
                                      dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])
        self._corrupt_the_ledger()
        r_patch, a_patch = self._world()
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], self.REASON, seat="integrator",
                                      dry_run=True)
        self.assertIsNone(out, out)
        self.assertIn(eventledger.CORRUPT_PREFIX, why,
                      "the refusal does not name the corrupt line, so the "
                      "retire door read the ledger leniently and measured "
                      "silence over a record it could not read in full")

    def test_the_WRITE_path_records_nothing_over_a_corrupt_ledger(self):
        # THE CONTROL ON THE OBSERVABLE, unconditionally and first: this exact
        # expression counts ONE event for a retirement that really happened, so
        # the empty reading below is a refusal and not a blind helper.
        control = self._row()
        r_patch, a_patch = self._world()
        with r_patch, a_patch:
            out, why = landreq.retire(control["id"], self.REASON,
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertEqual(len(self.retire_events(control["id"])), 1)
        row = self._row()
        self._corrupt_the_ledger()
        r_patch, a_patch = self._world()
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], self.REASON,
                                      seat="integrator")
        self.assertIsNone(out, out)
        self.assertIn(eventledger.CORRUPT_PREFIX, why)
        self.assertEqual(self.retire_events(row["id"]), [],
                         "a retirement was appended over a ledger the reader "
                         "could not read in full")

    def test_the_LOCKED_writer_refuses_it_too(self):
        """THE WRITER IS ITS OWN DOOR and it re-measures under the lock, so a
        caller that reached it with a clean read must still refuse when the
        ledger it is HOLDING cannot be read in full. Called directly, because
        no shipped caller can reach it once the door above refuses."""
        row = self._row()
        # THE CONTROL, UNCONDITIONALLY AND FIRST: the same call on the same
        # probe APPENDS, so the refusal after the corruption is the read.
        out, why = dispatches._record_retire_proven(
            row["id"], self.REASON, "integrator", None,
            lambda fresh, current: ("measured by this arm", None))
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(len(self.retire_events(row["id"])), 1,
                         "the event counter reads nothing for a retirement "
                         "that DID happen, so the empty reading below would "
                         "prove nothing about the refusal")
        other = self._row()
        self._corrupt_the_ledger()
        out, why = dispatches._record_retire_proven(
            other["id"], self.REASON, "integrator", None,
            lambda fresh, current: ("measured by this arm", None))
        self.assertIsNone(out, out)
        self.assertIn(eventledger.CORRUPT_PREFIX, why)
        self.assertEqual(self.retire_events(other["id"]), [],
                         "the locked writer appended a retirement over a "
                         "ledger it could not read in full")


class DestroyedReviewedObjectTest(RetireBase):
    """`--reason reviewed-object-destroyed` — the exit for a row whose own
    reviewed proof object no longer exists.

    THE WORLD THESE ARMS REPRODUCE, measured on the live board: five rows
    (three pre-tier APPROVEs, two advisory CONCURs) whose reviewed tip fails
    `cat-file -e`, which `helm lr refs` names among thousands of
    unresolvable recorded ids, and which every terminal refuses — `close
    --reason stranded` and `lr abandon` at their lane-family veto, the four
    older retire reasons each on their own grounds. Every fixture here drives
    the SHIPPED writer against a REAL destroyed object: the commit is made,
    dispatched, verdicted and then pruned by the shared `prune` recipe, which
    positively asserts rc 1 from a bare `cat-file -e` before any arm runs.
    """

    REASON = "reviewed-object-destroyed"

    def doomed_row(self, polarity="concur", lane="lane/destroyed-proof",
                   branch="doomed"):
        """(row, tip) — a verdicted row whose reviewed tip is a commit of its
        own on a temp branch. NOT yet pruned: the arms that need the object
        gone call `prune`, and the arm that needs it PRESENT is the control
        over this identical fixture."""
        base = self.git("rev-parse", self.main)
        self.git("checkout", "-q", "-b", branch, base)
        with open(os.path.join(self.repo, "doomed.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("work a history rewrite destroys\n")
        self.git("add", "doomed.txt")
        self.git("commit", "-q", "-m", "doomed work")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        row = self.verdicted(polarity=polarity, lane=lane, ref=tip)
        self.assertEqual(row["reviewed_tip"], tip)
        return row, tip

    def buckets(self):
        """({loops}, {stalled}, {nonbillable}, {id: hold kind}) from the THREE
        shipped producers the pipeline card's body is built from
        (`helm/web_land.py`: `loops`, `stalled_ids`, `unmeasurable` + its
        per-row `kind`). Reading the projection rather than the card is what
        makes this a statement about the board and not about a renderer."""
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        loops = {lr["id"] for lr in landreq._loop_rows(lrs, raw)}
        stalled = {lr["id"] for lr in landreq._stalled_rows(lrs, raw)}
        nonbillable = {lr["id"]
                       for lr, _why in landreq._unmeasurable_rows(lrs, raw)}
        kinds = {lr["id"]: landreq.review_hold_kind(lr)
                 for lr in lrs.values()}
        return loops, stalled, nonbillable, kinds

    def test_a_pruned_reviewed_object_retires_and_leaves_every_bucket(self):  # noqa: VACUOUS_ASSERTION — the three assertNotIn are preceded by unconditional assertIn/assertEqual on the SAME three producers, read before the retirement
        """THE POSITIVE, through the real writer, with the board read BEFORE
        as its own control: an advisory CONCUR hold that the card is charging
        for, and that no other terminal can take."""
        row, tip = self.doomed_row(polarity="concur")
        self.prune(tip, "refs/heads/doomed")
        loops, _stalled, nonbillable, kinds = self.buckets()
        self.assertIn(row["id"], loops,
                      "the control failed: the card is not counting this row "
                      "at all, so its disappearance below would prove nothing")
        self.assertIn(row["id"], nonbillable)
        self.assertEqual(kinds[row["id"]], "advisory",
                         "the fixture must sit in one of the card's named "
                         "nonbillable buckets before it is retired")
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(out["retire_reason"], self.REASON)
        self.assertIn(tip[:12], out["retire_measurement"])
        loops, stalled, nonbillable, _kinds = self.buckets()
        for name, ids in (("loops", loops), ("stalled", stalled),
                          ("nonbillable", nonbillable)):
            self.assertNotIn(row["id"], ids,
                             "a retired row is still counted in %s" % name)

    def test_a_present_reviewed_object_is_refused_by_name(self):
        """THE CONTROL ON THE POSITIVE: the identical fixture WITHOUT the
        prune. It can only fail at the object rung, and the refusal has to
        name the object rather than assert a category."""
        row, tip = self.doomed_row(polarity="concur")
        before = self.ledger_state(row["id"])
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn(tip[:12], why)
        self.assertIn("measurement 2 of 3", why,
                      "the refusal must say WHICH of the three measurements "
                      "failed")

    def test_a_surviving_lane_family_ref_is_disclosed_not_a_veto(self):
        """THE WHOLE DIFFERENCE FROM `stranded` AND `abandon`. Both refuse on
        a live lane-family ref because both claim the WORK is gone. This
        reason claims only that the row's PROOF is unreachable, so the ref is
        recorded in the measurement — the operator is told where to look —
        and the retirement proceeds."""
        row, tip = self.doomed_row(polarity="approve")
        self.git("branch", "lane/destroyed-proof-r2", self.main)
        self.prune(tip, "refs/heads/doomed")
        family, err = landreq._lane_family_refs(
            self.gitdir(), row.get("lane"), row.get("branch"))
        self.assertIsNone(err, err)
        self.assertIn("refs/heads/lane/destroyed-proof-r2",
                      [ref for ref, _obj in family],
                      "the control failed: the probe that blocks stranded "
                      "does not see this ref, so admitting below says nothing")
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(why, why)
        self.assertIn("refs/heads/lane/destroyed-proof-r2",
                      out["retire_measurement"],
                      "the surviving ref must be DISCLOSED on the record")

    def test_a_translatable_tip_is_refused_with_its_translation(self):
        """A recorded rewrite is a live identity, not a destroyed one, and the
        refusal hands over the sha to adjudicate through."""
        row, tip = self.doomed_row(polarity="approve")
        self.prune(tip, "refs/heads/doomed")
        self.sidecar(tip, self.b)
        before = self.ledger_state(row["id"])
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn(self.b[:12], why)
        self.assertIn("measurement 3 of 3", why)

    def test_a_row_binding_no_reviewed_commit_names_that(self):
        """THE MUST-MISS on the reason's own subject: an OPEN row has no
        reviewed proof object at all, and this reason must say so rather than
        measure the absence as a destruction."""
        row = self.open_row(lane="lane/destroyed-proof")
        before = self.ledger_state(row["id"])
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("binds no reviewed commit", why)

    def test_a_live_party_does_not_block_a_destroyed_object(self):
        """THE BELT EXEMPTION AT THE SINGLE-ID DOOR, with the belt itself as
        the control AND the sweep call shape as the counter-control.

        Every other reason refuses while any party on the row is reachable,
        because a live party can take the normal path. Here the normal path is
        adjudicating a commit that does not exist, so liveness is not evidence
        about it — but only a human acting on one named row may make that
        substitution. Three readings of ONE world, which is what makes this an
        exemption rather than a fixture with nobody home in it:
          * the belt run directly REFUSES (the control);
          * `_measure_retire` under `RETIRE_CALL_SWEEP` refuses too, with the
            belt's own words — the call-shape half of the key;
          * the single-id door admits, and SAYS the belt was set aside.
        """
        row, tip = self.doomed_row(polarity="approve")
        self.prune(tip, "refs/heads/doomed")
        roster = self.roster({"ghost-author": "session-live"})
        panes = self.panes("ghost-author")
        presence = [{"seat": "ghost-author", "presence": "fresh"}]
        self.assert_reach("ghost-author", landreq.SEAT_LIVE, roster, panes,
                          presence=presence)
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      presence=presence)
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            fresh = ctx["snapshot"][row["id"]]
            self.assertIsNotNone(
                landreq._retire_live_guard(fresh, ctx),
                "the control failed: the belt does NOT refuse in this world, "
                "so admitting below is not an exemption")
            swept, sweep_why = landreq._measure_retire(
                self.REASON, fresh, ctx,
                call_shape=landreq.RETIRE_CALL_SWEEP)
            out, why = landreq.retire(row["id"], self.REASON,
                                      seat="integrator", ctx=ctx)
        self.assertIsNone(swept)
        self.assertIn("REACHABLE", sweep_why,
                      "the SWEEP call shape must take the belt's refusal on "
                      "this very reason: %s" % sweep_why)
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertIn("LIVENESS BELT SET ASIDE", out["retire_measurement"],
                      "a terminal that skipped a shared guard must not be "
                      "indistinguishable on the record from one that passed "
                      "it")

    def test_the_sweep_refuses_a_destroyed_row_with_a_live_sender(self):
        """THE MIXED-LIVENESS SWEEP. An unattended pass may not spend a row a
        human never named, whatever the reason measures.

        THE FAILURE MODE THIS WORLD IS BUILT TO CATCH: a belt exemption keyed
        on the REASON alone, at the wrapper both doors share, reaches the
        sweep. The sweep's pre-skip is not a containment — it asks about the
        ONE OWING seat, while the belt requires EVERY party absent — so a row
        whose owing seat (the reviewer) is positively ABSENT and whose SENDER
        is LIVE walks past the skip, and a reason-keyed exemption would then
        append a retirement with a live party on the row.

        THE TWO PRECONDITIONS ARE ASSERTED, not assumed, because either one
        being wrong would make the refusal below true for a reason that is not
        this cure: the reviewer must read ABSENT (or the sweep skips the row
        and never reaches any reason at all) and the sender must read LIVE (or
        the belt has nothing to refuse on). Both are pinned through
        `seat_reach`, the reader the belt itself calls.

        BLAST RADIUS OF THE CONTROLS: `assert_reach` touches only this arm's
        fixture world — it installs its own patches, runs one read and drops
        them — so a red from either precondition names a broken fixture and
        can never mask or mimic a defect in the sweep.
        """
        row, tip = self.doomed_row(polarity="concur")
        # AGE BEFORE PRUNE, deliberately: `age` recreates the verdict through
        # the REAL producer from its original call, and that call names this
        # tip — re-running it after the object is gone would be asking the
        # producer to attest a commit that no longer exists.
        self.age(row["id"], 30 * 86400)
        self.prune(tip, "refs/heads/doomed")
        roster = self.roster({"ghost-author": "session-live",
                              "ghost-reviewer": ""})
        panes = self.panes("ghost-author")
        presence = [{"seat": "ghost-author", "presence": "fresh"},
                    {"seat": "ghost-reviewer", "presence": "absent"}]
        self.assert_reach("ghost-reviewer", landreq.SEAT_ABSENT, roster,
                          panes, presence=presence)
        self.assert_reach("ghost-author", landreq.SEAT_LIVE, roster, panes,
                          presence=presence)
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      presence=presence)
        with r_patch, a_patch:
            # dry_run=False DELIBERATELY: a rehearsal cannot demonstrate that
            # nothing was appended, and the append is the whole harm.
            report, unavailable = landreq.retire_sweep(older_than_days=14,
                                                       dry_run=False,
                                                       seat="integrator")
        self.assertIsNone(unavailable, unavailable)
        self.assertNotIn(row["id"],
                         [e["id"] for e in report["skipped_live_tla"]],
                         "the premise failed: the sweep SKIPPED this row on "
                         "its owing seat, so it never reached the reasons and "
                         "this arm proves nothing about the belt")
        self.assertEqual(report["plan"], [],
                         "the sweep PLANNED an unattended retirement over a "
                         "live sender: %r" % (report["plan"],))
        self.assertEqual(report["retired"], [])
        self.assertEqual([e["id"] for e in report["refused"]], [row["id"]])
        named = {r["reason"]: r["refusal"] for r in
                 report["refused"][0]["refusals"]}
        self.assertIn(self.REASON, named,
                      "the sweep did not even run the exempt reason here")
        self.assertIn("ghost-author", named[self.REASON],
                      "the refusal must NAME the live party: %s"
                      % named[self.REASON])
        self.assertIn("REACHABLE", named[self.REASON])
        self.assert_nothing_appended(row["id"], before, named[self.REASON])
        # …AND THE SAME ROW, IN THE SAME WORLD, THROUGH THE SINGLE-ID DOOR.
        # This is the other half of the rule: the exemption is not deleted,
        # it is moved to the explicit act, and the pair proves the difference
        # is the CALL SHAPE and nothing about the fixture.
        #
        # A SECOND `reach()` FOR THE SECOND BLOCK, not the same pair reused:
        # these are `@contextlib.contextmanager` generators and a second
        # `with` over an exhausted one raises. Same arguments, so the world
        # is identical — which is the whole claim this half makes.
        r_patch, a_patch = self.reach(roster=roster, panes=panes,
                                      presence=presence)
        with r_patch, a_patch:
            out, why = landreq.retire(row["id"], self.REASON,
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertIn("LIVENESS BELT SET ASIDE", out["retire_measurement"])

    def test_the_exemption_register_holds_exactly_this_reason(self):  # noqa: VACUOUS_ASSERTION — every observable here is read unconditionally and asserted EQUAL to a named value (the register tuple, the wrapper's declared default); the one assertNotEqual states that the two shape constants are distinct, which is a property of two present values, not an absence
        """THE REGISTER IS THE CONTRACT — BOTH HALVES OF THE KEY. A reason
        joins it by argument, never by convenience, so a second member must
        turn this red and make its author write the argument down; and the
        register alone never exempts anything, which is the defect this arm
        grew to hold. The default call shape is asserted because a caller who
        forgets must land on the BELTED pole: forgetting can then only
        over-refuse, never spend a row."""
        self.assertEqual(landreq._RETIRE_BELT_EXEMPT, (self.REASON,))
        for reason in landreq._RETIRE_BELT_EXEMPT:
            self.assertIn(reason, dispatches.RETIRE_REASONS)
        self.assertNotEqual(landreq.RETIRE_CALL_SINGLE_ID,
                            landreq.RETIRE_CALL_SWEEP)
        shapes = inspect.signature(landreq._measure_retire).parameters
        self.assertEqual(shapes["call_shape"].default,
                         landreq.RETIRE_CALL_SWEEP,
                         "an undeclared call shape must be treated as "
                         "unattended, or a future caller inherits the "
                         "exemption by forgetting about it")

    def test_the_sweep_declares_its_call_shape_at_every_measurement(self):
        """THE STRUCTURAL HALF. The defect was one call site inheriting a
        default; this censuses the source so a new call site inside the sweep
        cannot quietly stop declaring itself.

        BLAST RADIUS: this reads `landreq.__file__` and asserts nothing about
        behaviour, so it can only go red when the sweep's own text changes —
        it cannot mask a behavioural red in the arm above."""
        import ast
        with io.open(landreq.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        sweep = [fn for fn in ast.walk(tree)
                 if isinstance(fn, ast.FunctionDef)
                 and fn.name == "retire_sweep"]
        self.assertEqual(len(sweep), 1, "the census lost its subject")
        calls = [n for n in ast.walk(sweep[0]) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) in ("_measure_retire",
                                                     "retire")]
        self.assertTrue(calls, "the census found no retire call in the sweep "
                               "at all — it rotted")
        for call in calls:
            passed = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn("call_shape", passed,
                          "a sweep call site does not declare its call shape, "
                          "so it inherits a default — which is exactly how "
                          "the exemption leaked")
            self.assertEqual(getattr(passed["call_shape"], "id", None),
                             "RETIRE_CALL_SWEEP",
                             "a sweep call site declares a shape that is not "
                             "the sweep's")

    def test_the_audit_leg_uses_the_audits_own_predicate(self):
        """LEG 1 is a claim about ANOTHER SURFACE — `helm lr refs` — so it is
        made with that surface's rule. The control moves the rule: with
        `reviewed_tip` out of the audited field set the measurement must
        refuse, because the audit would then report nothing about this id."""
        row, tip = self.doomed_row(polarity="approve")
        self.prune(tip, "refs/heads/doomed")
        fields = tuple(f for f in landreq._REF_FIELDS if f != "reviewed_tip")
        before = self.ledger_state(row["id"])
        with mock.patch.object(landreq, "_REF_FIELDS", fields):
            out, why = landreq.retire(row["id"], self.REASON,
                                      seat="integrator")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("measurement 1 of 3", why)
        out, why = landreq.retire(row["id"], self.REASON, seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"],
                        "the positive control: the same row retires once the "
                        "audit's real field set is restored")


class AbsentAuthorLaneIdleTest(RetireBase):
    """`--reason author-absent-lane-idle` — the exit for a FIX-verdicted row
    whose author left the fleet while its unlanded work stays on the lane.

    THE WORLD EVERY ARM BUILDS, through the shipped producers: a lane branch
    whose commits are dated past the silence window, a review dispatched on
    that branch by an author no roster surface answers to, a FIX verdict
    recorded by a reviewer that is still LIVE, and a cure commit above the
    reviewed tip. `author-unresolvable` refuses that row on the all-parties
    belt because the reviewer is reachable; this reason admits it at the
    single-id door, names the branch and tip it retains, and says which party
    the belt did not ask about.
    """

    REASON = "author-absent-lane-idle"
    AUTHOR = "departed-author"
    REVIEWER = "codex-live"
    BRANCH = "lane/absent-author"

    def dated_commit(self, text, days):
        """A commit on the checked-out branch, authored and committed `days`
        ago, so the lane's idleness is a property of real Git history."""
        stamp = "@%d +0000" % _ago_epoch(days)
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        with open(os.path.join(self.repo, "lane.txt"), "a",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")
        for argv in (("add", "lane.txt"), ("commit", "-q", "-m", text)):
            subprocess.run(["git", "-C", self.repo, *argv], check=True,
                           capture_output=True, text=True, env=env)
        return self.git("rev-parse", "HEAD")

    def fix_row(self, days=20, **kw):
        """(row, reviewed, cure) — a FIX-verdicted review of `BRANCH`, with a
        cure committed above the reviewed tip, every commit `days` old. `kw`
        goes to the dispatch producer, so a caller can supersede a row."""
        self.git("checkout", "-q", "-b", self.BRANCH, self.main)
        reviewed = self.dated_commit("reviewed work", days)
        self.git("checkout", "-q", self.main)
        row = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                            lane="absent-author", ref=self.BRANCH, **kw)
        self.assertEqual(row.get("ref_branch"), "refs/heads/" + self.BRANCH,
                         "the premise failed: the dispatch did not bind the "
                         "lane branch, so no arm below measures a lane")
        dispatches._mark_delivered(row["id"], "post-retire")
        out, err = self.mark_verdict(row["id"], reviewed, "reviewed",
                                     polarity="fix")
        self.assertIsNone(err, err)
        self.git("checkout", "-q", self.BRANCH)
        cure = self.dated_commit("the cure", days)
        self.git("checkout", "-q", self.main)
        return out, reviewed, cure

    def world(self, author_days=30, roster_failed=False, roster=None,
              presence=None):
        """The reach doubles: a LIVE reviewer with a session, a pane and a
        fresh presence row, and an author with no roster row whose last
        authored act is `author_days` old in a current index. An arm that
        rosters the author also passes the presence row describing it, since
        the projection describes one row per roster seat."""
        rows = dict(roster or {self.REVIEWER: {"session": "live-session"}})
        activity = ({self.AUTHOR: _ago(author_days),
                     self.REVIEWER: _ago(0)}, {}, _ago(0), None)
        return self.reach(
            roster=rows, panes=self.panes(self.REVIEWER),
            roster_failed=roster_failed,
            presence=presence or [{"seat": self.REVIEWER,
                                   "presence": "fresh"}],
            activity=activity)

    def retire(self, row, **kw):
        r_patch, a_patch = self.world(**{k: kw.pop(k) for k in
                                         ("author_days", "roster_failed",
                                          "roster", "presence") if k in kw})
        with r_patch, a_patch:
            return landreq.retire(row["id"], kw.pop("reason", self.REASON),
                                  seat="integrator", **kw)

    def test_a_live_discharged_reviewer_does_not_block_an_absent_authors_cure(self):  # noqa: VACUOUS_ASSERTION — the author-unresolvable refusal and the sweep-shape refusal are read first on the same world as positive controls; the admit then asserts the retained branch, tip and one appended event
        """THE GAP: absent owing author, live discharged reviewer.

        CONTROLS ON ONE WORLD, first and unconditionally: the author reads
        ABSENT and the reviewer LIVE through `seat_reach`; `author-unresolvable`
        refuses on the belt naming the reviewer; the new reason under the
        SWEEP call shape refuses the same way. Then the single-id door admits.
        MUTATION: deleting the `discharged` skip in `_retire_live_guard` turns
        the admit into the belt's REACHABLE refusal.
        """
        row, _reviewed, cure = self.fix_row()
        roster = self.roster({self.REVIEWER: "live-session"})
        presence = [{"seat": self.REVIEWER, "presence": "fresh"}]
        activity = ({self.AUTHOR: _ago(30), self.REVIEWER: _ago(0)}, {},
                    _ago(0), None)
        self.assert_reach(self.AUTHOR, landreq.SEAT_ABSENT, roster,
                          self.panes(self.REVIEWER), presence=presence,
                          activity=activity)
        self.assert_reach(self.REVIEWER, landreq.SEAT_LIVE, roster,
                          self.panes(self.REVIEWER), presence=presence,
                          activity=activity)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row, reason="author-unresolvable")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn(self.REVIEWER, why)
        self.assertIn("REACHABLE", why)
        r_patch, a_patch = self.world()
        with r_patch, a_patch:
            ctx, err = landreq._retire_context()
            self.assertIsNone(err, err)
            swept, sweep_why = landreq._measure_retire(
                self.REASON, ctx["snapshot"][row["id"]], ctx,
                call_shape=landreq.RETIRE_CALL_SWEEP)
        self.assertIsNone(swept)
        self.assertIn("REACHABLE", sweep_why)
        out, why = self.retire(row)
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(out["retire_reason"], self.REASON)
        measured = out["retire_measurement"]
        self.assertIn("RETAINS refs/heads/%s at %s" % (self.BRANCH, cure),
                      measured)
        self.assertIn("@%s (recipient) not asked by the liveness belt"
                      % self.REVIEWER, measured)
        self.assertEqual(self.ledger_state(row["id"]),
                         (before[0] + 1, before[1] + 1))
        self.assertEqual(self.git("rev-parse", self.BRANCH), cure,
                         "retirement moved the lane it says it retains")

    def test_the_retire_is_idempotent_and_appends_exactly_one_event(self):
        """A retry under the same reason returns the standing terminal and
        appends nothing; a retry under another reason refuses. The first call
        is the positive control that an event CAN be counted here."""
        row, _reviewed, _cure = self.fix_row()
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(why, why)
        once = self.ledger_state(row["id"])
        self.assertEqual(once, (before[0] + 1, before[1] + 1))
        again, why = self.retire(row)
        self.assertIsNone(why, why)
        self.assertEqual(again["retire_reason"], self.REASON)
        self.assertEqual(self.ledger_state(row["id"]), once)
        other, why = self.retire(row, reason="author-unresolvable")
        self.assertIsNone(other)
        self.assert_nothing_appended(row["id"], once, why)

    def test_a_lane_committed_inside_the_window_refuses_and_names_when(self):
        """MUTATION: flipping the window comparison admits this row. The admit
        control is the first arm's world with the same producers."""
        row, _reviewed, cure = self.fix_row(
            days=landreq.SEAT_SILENCE_DAYS - 2)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("inside the %dd silence window"
                      % landreq.SEAT_SILENCE_DAYS, why)
        self.assertIn(cure[:12], why)
        self.assertIn("--reason %s --dry-run" % self.REASON, why)

    def test_a_reachable_author_refuses_and_names_the_cure_as_its_move(self):
        """The author is the party that owes the move, so a live author is
        never set aside. MUTATION: skipping the author's reach turns this into
        an admit."""
        row, _reviewed, _cure = self.fix_row()
        before = self.ledger_state(row["id"])
        roster = {self.REVIEWER: {"session": "live-session"},
                  self.AUTHOR: {"session": "author-session"}}
        presence = [{"seat": self.REVIEWER, "presence": "fresh"},
                    {"seat": self.AUTHOR, "presence": "fresh"}]
        out, why = self.retire(row, roster=roster, presence=presence)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("author @%s is REACHABLE" % self.AUTHOR, why)
        self.assertIn("helm dispatch triage", why)

    def test_an_unreadable_roster_is_unknown_and_refuses(self):
        row, _reviewed, _cure = self.fix_row()
        before = self.ledger_state(row["id"])
        out, why = self.retire(row, roster_failed=True)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("UNKNOWN", why)
        self.assertIn("roster is unreadable", why)

    def test_a_renamed_author_is_present_under_its_new_name(self):
        """A roster row carrying the author's key in its `seat_keys` lineage is
        the author still present. The control is the same row with that
        lineage removed, which admits. MUTATION: dropping the RENAMED branch
        admits the renamed author's row."""
        from helm import seats_common
        row, _reviewed, _cure = self.fix_row()
        renamed = {self.REVIEWER: {"session": "live-session"},
                   "renamed-author": {"seat_keys": [
                       seats_common._seat_key(self.AUTHOR)]}}
        before = self.ledger_state(row["id"])
        out, why = self.retire(row, roster=renamed, dry_run=True)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("PRESENT under another name", why)
        self.assertIn("@renamed-author's move", why)
        self.assertIn("helm dispatch triage %s" % row["id"][:12], why)
        self.assertNotIn("seat reassign", why,
                         "reassign moves open and held rows only, so it "
                         "cannot move a verdicted row's owed cure")
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    def test_a_successor_still_on_the_board_refuses_and_a_cancelled_one_does_not(self):
        """The chain carries the obligation while a successor is not terminal.
        The successor is written by the real dispatch producer; cancelling it
        through the real cancel door is the control that admits. MUTATION:
        `_chain_unfinished` answering no descendants admits the first call."""
        row, _reviewed, _cure = self.fix_row()
        kid = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                            lane="absent-author", ref=self.BRANCH,
                            supersedes=row["id"])
        before = self.ledger_state(row["id"])
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("continues at %s" % kid["id"][:12], why)
        _cancelled, err = dispatches.mark_cancel(kid["id"], "stood down")
        self.assertIsNone(err, err)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    DROP = object()

    def rewrite_opener(self, rid, field, value):
        """Rewrite the DURABLE dispatch opener of `rid` so `field` reads
        `value`, or is removed when `value` is DROP, every other byte of the
        ledger kept. The line stays a complete JSON object, so the strict
        reader accepts it and replay decides what the field means."""
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == "dispatch" \
                        and field in ev:
                    if value is self.DROP:
                        del ev[field]
                    else:
                        ev[field] = value
                    hit += 1
                fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(hit, 1, "the premise failed: no single opener of %s "
                         "carries a %s to rewrite" % (rid, field))
        _snap, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)

    def chain(self):
        """(R, K, G) through the real dispatch producer: R carries the FIX,
        K supersedes R and is cancelled, G supersedes K and is open. The
        replayed roots of K and G are asserted to be R's chain first."""
        row, _reviewed, _cure = self.fix_row()
        k = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                          lane="absent-author-k", ref=self.BRANCH,
                          supersedes=row["id"])
        g = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                          lane="absent-author-g", ref=self.BRANCH,
                          supersedes=k["id"])
        _out, err = dispatches.mark_cancel(k["id"], "stood down")
        self.assertIsNone(err, err)
        snap = dispatches.snapshot()[0]
        for node in (k, g):
            self.assertEqual(snap[node["id"]]["chain_root"],
                             row.get("chain_root") or row["id"])
        return row, k, g

    def assert_unresolved(self, row, node, reason):
        """The row refuses naming `node` and exactly `reason`, at the door and
        in the locked re-measure alone, and appends nothing either time. The
        reason is matched whole, so a mutant that falls through to another
        branch whose text begins the same way still fails."""
        before = self.ledger_state(row["id"])
        for kw in ({}, {"preflight": "door measurement skipped"}):
            out, why = self.retire(row, **kw)
            self.assertIsNone(out, out)
            self.assert_nothing_appended(row["id"], before, why)
            self.assertIn("UNRESOLVED at %s (%s)" % (node["id"][:12], reason),
                          why)

    def assert_blocks_then_admits(self, row, open_kid):
        """CONTROL on the same durable ledger: the open successor blocks, and
        cancelled, the terminal chain admits."""
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("continues at %s" % open_kid["id"][:12], why)
        _out, err = dispatches.mark_cancel(open_kid["id"], "stood down")
        self.assertIsNone(err, err)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    def test_a_damaged_chain_node_refuses_as_unresolved_topology(self):  # noqa: VACUOUS_ASSERTION — the replayed UNKNOWN root is asserted before the refusal; the restored root blocking on the open grandchild and the all-terminal chain admitting are positive controls on the same durable ledger
        """R -> K -> G, K cancelled and G open. K's durable opener then has
        its chain_root damaged to a string that is still valid JSON, which
        replay reads UNKNOWN. `_same_chain` answers False for that, and the
        old walk skipped K and never reached G. The row now refuses naming K
        and why, at the door and in the locked re-measure. CONTROLS on the
        same ledger: K's root restored, G blocks as open; G cancelled, the
        terminal chain admits. MUTATION: disabling the UNKNOWN-child branch
        refuses under another reason, and fails the assertIn on this one."""
        row, k, g = self.chain()
        root = dispatches.snapshot()[0][k["id"]]["chain_root"]
        self.rewrite_opener(k["id"], "chain_root", "damaged chain root")
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[k["id"]]["chain_root"], dispatches.CHAIN_UNKNOWN)
        self.assertEqual(snap[k["id"]]["status"], "cancelled")
        self.assertEqual(snap[g["id"]]["status"], "open")
        self.assert_unresolved(row, k, "chain_root UNKNOWN")
        self.rewrite_opener(k["id"], "chain_root", root)
        self.assert_blocks_then_admits(row, g)

    def test_a_well_formed_root_naming_another_chain_is_unresolved(self):  # noqa: VACUOUS_ASSERTION — the replayed roots are asserted equal to the planted ids before each refusal; the restored root blocking on the open grandchild and the terminal chain admitting are positive controls on the same durable ledger
        """No producer writes a child whose root differs from its parent's
        chain, so a well-formed root that is not R's chain is damage, not a
        foreign edge. K's root is rewritten first to an id that names no row,
        then to the id of a real row that roots its own chain. Both refuse
        naming K, at the door and in the locked re-measure, where skipping K
        as foreign would never reach the open G. CONTROL: K's root restored,
        G blocks; G cancelled, the chain admits. MUTATION: skipping a child
        whose root is well-formed and not this chain's admits the first
        call."""
        row, k, g = self.chain()
        root = dispatches.snapshot()[0][k["id"]]["chain_root"]
        other = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                              lane="another-chain", ref=self.BRANCH)
        self.assertEqual(dispatches.snapshot()[0][other["id"]]["chain_root"],
                         other["id"])
        for planted in ("deadbeefcafe", other["id"]):
            self.rewrite_opener(k["id"], "chain_root", planted)
            self.assertEqual(dispatches.snapshot()[0][k["id"]]["chain_root"],
                             planted)
            self.assert_unresolved(
                row, k, "chain_root %s is not this chain's %s"
                % (planted[:12], row["id"][:12]))
        self.rewrite_opener(k["id"], "chain_root", root)
        self.assert_blocks_then_admits(row, g)

    def test_a_rows_own_unknown_root_refuses_naming_the_row(self):  # noqa: VACUOUS_ASSERTION — the replayed UNKNOWN root of R is asserted before the refusal; the restored root blocking on the open successor and the terminal chain admitting are positive controls on the same durable ledger
        """R's own durable opener has its chain_root damaged while its
        successor K is open. R has no chain to compare K against, so the row
        refuses naming R itself, at the door and in the locked re-measure.
        CONTROL: R's root restored, K blocks; K cancelled, the chain admits.
        MUTATION: disabling the row's-own-UNKNOWN branch refuses naming K
        instead, and fails the assertIn on R."""
        row, _reviewed, _cure = self.fix_row()
        k = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                          lane="absent-author-k", ref=self.BRANCH,
                          supersedes=row["id"])
        root = dispatches.snapshot()[0][row["id"]]["chain_root"]
        self.assertEqual(root, row["id"])
        self.rewrite_opener(row["id"], "chain_root", "damaged chain root")
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["chain_root"],
                         dispatches.CHAIN_UNKNOWN)
        self.assert_unresolved(row, row, "its own chain_root is UNKNOWN")
        self.rewrite_opener(row["id"], "chain_root", root)
        self.assert_blocks_then_admits(row, k)

    def test_a_child_with_no_chain_root_refuses_naming_it(self):  # noqa: VACUOUS_ASSERTION — the replayed absent root of K is asserted before the refusal; the restored root blocking on the open grandchild and the terminal chain admitting are positive controls on the same durable ledger
        """K's durable opener loses its chain_root key, which replay reads as
        None, while G under it is open. The row refuses naming K and
        "chain_root absent", at the door and in the locked re-measure.
        CONTROL: K's root restored, G blocks; G cancelled, the chain admits.
        MUTATION: disabling the absent-root branch refuses under another
        reason, and fails the assertIn on this one."""
        row, k, g = self.chain()
        root = dispatches.snapshot()[0][k["id"]]["chain_root"]
        self.rewrite_opener(k["id"], "chain_root", self.DROP)
        self.assertIsNone(dispatches.snapshot()[0][k["id"]]["chain_root"])
        self.assert_unresolved(row, k, "chain_root absent")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                if ev.get("id") == k["id"] and ev.get("event") == "dispatch" \
                        and "chain_root" not in ev:
                    ev["chain_root"] = root
                fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assert_blocks_then_admits(row, g)

    def test_a_successor_whose_parent_field_is_damaged_is_unresolved(self):  # noqa: VACUOUS_ASSERTION — the replayed supersedes of K is asserted before each refusal; the restored parent blocking on the open successor and the terminal chain admitting are positive controls on the same durable ledger
        """K supersedes R and is open, and K's durable opener has its
        `supersedes` rewritten, first to a string replay reads UNKNOWN, then
        to a well-formed id no row holds. The successor index keys K under
        that value, so no walk from R reaches it, and the old walk read R's
        chain as clear. K still carries R's chain, so the row refuses naming
        K, at the door and in the locked re-measure. CONTROL: K's parent
        restored, K blocks; K cancelled, the chain admits. MUTATION: dropping
        the census of same-chain rows the index cannot reach admits the first
        call."""
        row, _reviewed, _cure = self.fix_row()
        k = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                          lane="absent-author-k", ref=self.BRANCH,
                          supersedes=row["id"])
        for planted, replayed, reason in (
                ("damaged parent", dispatches.CHAIN_UNKNOWN,
                 "its supersedes is UNKNOWN, so no walk reaches it"),
                ("deadbeefcafe0001", "deadbeefcafe0001",
                 "its supersedes names deadbeefcafe, which the snapshot "
                 "does not hold")):
            self.rewrite_opener(k["id"], "supersedes", planted)
            snap = dispatches.snapshot()[0]
            self.assertEqual(snap[k["id"]]["supersedes"], replayed)
            self.assertEqual(snap[k["id"]]["chain_root"], row["id"])
            self.assertEqual(snap[k["id"]]["status"], "open")
            self.assert_unresolved(row, k, reason)
        self.rewrite_opener(k["id"], "supersedes", row["id"])
        self.assert_blocks_then_admits(row, k)

    def test_a_same_root_successor_whose_parent_field_is_gone_is_unresolved(self):  # noqa: VACUOUS_ASSERTION — the replayed None supersedes and R's root on K are asserted before each refusal; the restored edge blocking on K and the cancelled K admitting are positive controls on the same durable ledger
        """K supersedes R and is open. K's durable opener then has its
        `supersedes` written JSON null, and then the key dropped; replay reads
        both as None while K keeps R's chain_root. The successor index keys
        only a truthy parent, so no walk from R reaches K, and the old pre-scan
        skipped a row with no parent, which read R's chain as clear. Only a
        chain's root row supersedes nothing, so the row refuses naming K, at
        the door and in the locked re-measure. CONTROLS on the same ledger:
        K's edge restored, K blocks as continuing; K cancelled, the chain
        admits with a legacy row (no chain_root, no supersedes) on the board.
        The retired row here is R itself, which the pre-scan never inspects,
        so the root exemption is exercised by
        `test_a_fix_row_above_its_parentless_root_admits`. MUTATION: skipping
        a same-root row with no parent admits the first call."""
        row, _reviewed, _cure = self.fix_row()
        k = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                          lane="absent-author-k", ref=self.BRANCH,
                          supersedes=row["id"])
        legacy = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                               lane="absent-author-legacy", ref=self.BRANCH)
        self.rewrite_opener(legacy["id"], "chain_root", self.DROP)
        snap = dispatches.snapshot()[0]
        self.assertIsNone(snap[legacy["id"]]["chain_root"])
        self.assertIsNone(snap[legacy["id"]]["supersedes"])
        self.assertEqual(snap[row["id"]]["chain_root"], row["id"])
        self.assertIsNone(snap[row["id"]]["supersedes"])
        self.assertNotEqual(k["id"], row["id"])
        for planted in (None, self.DROP):
            self.rewrite_opener(k["id"], "supersedes", planted)
            snap = dispatches.snapshot()[0]
            self.assertIsNone(snap[k["id"]]["supersedes"])
            self.assertEqual(snap[k["id"]]["chain_root"], row["id"])
            self.assertEqual(snap[k["id"]]["status"], "open")
            self.assert_unresolved(
                row, k, "it carries this chain's root but supersedes nothing, "
                "so no walk reaches it")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        restored = 0
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                if ev.get("id") == k["id"] and ev.get("event") == "dispatch" \
                        and "supersedes" not in ev:
                    ev["supersedes"] = row["id"]
                    restored += 1
                fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertEqual(restored, 1)
        self.assertEqual(dispatches.snapshot()[0][k["id"]]["supersedes"],
                         row["id"])
        self.assert_blocks_then_admits(row, k)

    def test_a_fix_row_above_its_parentless_root_admits(self):  # noqa: VACUOUS_ASSERTION — the admits are the claim; R replaying its own root with no parent while K2 carries R's root is asserted first, and the root-exemption mutant refuses naming R
        """R is an OPEN root: its chain_root is its own id and it supersedes
        nothing. K2 supersedes R, carries the FIX verdict on the lane and has
        no successor. Retiring K2 makes the pre-scan read R, the one same-root
        row that may supersede nothing, so the row admits at the door and in
        the locked re-measure alone. MUTATION: dropping the root-id condition
        refuses naming R as a parentless successor."""
        root = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                             lane="absent-author-root")
        row, _reviewed, cure = self.fix_row(supersedes=root["id"])
        snap = dispatches.snapshot()[0]
        self.assertEqual(snap[root["id"]]["chain_root"], root["id"])
        self.assertIsNone(snap[root["id"]]["supersedes"])
        self.assertEqual(snap[root["id"]]["status"], "open")
        self.assertEqual(snap[row["id"]]["chain_root"], root["id"])
        self.assertEqual(snap[row["id"]]["supersedes"], root["id"])
        self.assertNotEqual(row["id"], root["id"])
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])
        before = len(self.retire_events(row["id"]))
        out, why = self.retire(row, preflight="door measurement skipped")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(out["retire_reason"], self.REASON)
        self.assertIn("RETAINS refs/heads/%s at %s" % (self.BRANCH, cure),
                      out["retire_measurement"])
        self.assertEqual(len(self.retire_events(row["id"])), before + 1)

    def assert_landed_refusal(self, row, why, readings):
        """A landed-tip refusal names each tip's reading and no command, and
        the close doors its prose describes refuse this same row. `readings`
        is {sha: proof} for both tips. MUTATION: restoring the old refusal
        that named `helm lr close <id> --reason landed` fails the assertNotIn,
        and that door's own refusal below is why."""
        for sha, proof in readings.items():
            self.assertIn("%s reads %s" % (sha[:12], proof), why)
        self.assertIn("this reason does not apply", why)
        self.assertIn("No command is named here", why)
        self.assertNotIn("helm lr", why,
                         "the refusal names a command, and the close doors "
                         "below refuse this row")
        before = self.ledger_state(row["id"])
        lane_tip = self.git("rev-parse", self.BRANCH)
        out, err = landreq.close(row["id"], "landed", dry_run=True)
        self.assertIsNone(out, out)
        self.assertIn("CONTRARY", err)
        out, err = landreq.close(row["id"], "superseded", tip=lane_tip,
                                 evidence="the lane landed", dry_run=True)
        self.assertIsNone(out, out)
        self.assertIn("APPROVE", err)
        self.assertEqual(self.ledger_state(row["id"]), before)

    def test_a_landed_lane_refuses_and_names_both_tips_and_no_command(self):  # noqa: VACUOUS_ASSERTION — the landing proof of each tip is read into the refusal and asserted by name, and both close doors are run on the same row and asserted to refuse with their own reason
        """A fast-forward land puts both tips on trunk. The refusal names both
        readings, so the landed lane does not hide the reviewed tip, and it
        names no command, because `close --reason landed` refuses a row whose
        own verdict is FIX and `--reason superseded` finds no later APPROVE:
        both doors are run on this row."""
        row, reviewed, cure = self.fix_row()
        self.git("merge", "-q", "--ff-only", self.BRANCH)
        self.assertEqual(self.git("rev-parse", self.main), cure,
                         "the premise failed: the lane did not reach trunk")
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assert_landed_refusal(row, why, {cure: "ancestor",
                                              reviewed: "ancestor"})

    def test_a_reviewed_tip_on_trunk_without_its_cure_refuses_and_names_it(self):  # noqa: VACUOUS_ASSERTION — the landing proof of each tip is read into the refusal and asserted by name, and both close doors are run on the same row and asserted to refuse with their own reason
        """Only the reviewed commit was picked onto trunk; the cure was not.
        The lane tip reads absent and the reviewed tip patch-equivalent, and
        the refusal names both. MUTATION: gating the refusal on the lane tip
        only falls through to the undetermined-object refusal, which names
        neither reading, and fails the first assertIn on a tip's
        reading."""
        row, reviewed, cure = self.fix_row()
        picked = subprocess.run(["git", "-C", self.repo, "cherry-pick",
                                 reviewed], capture_output=True, text=True)
        self.assertEqual(picked.returncode, 0, picked.stderr)
        pinned = self.git("rev-parse", self.main)
        gitdir = row["repo_id"]
        self.assertEqual(landreq._landing_proof(gitdir, reviewed, pinned),
                         "patch-equivalent")
        self.assertEqual(landreq._landing_proof(gitdir, cure, pinned),
                         "absent")
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assert_landed_refusal(row, why, {cure: "absent",
                                              reviewed: "patch-equivalent"})

    def test_a_landed_cure_whose_reviewed_commit_is_not_on_trunk_refuses(self):  # noqa: VACUOUS_ASSERTION — the landing proof of each tip is read into the refusal and asserted by name, and both close doors are run on the same row and asserted to refuse with their own reason
        """Trunk carries the reviewed content under a different patch, and
        the cure picked on top of it. The lane tip reads patch-equivalent and
        the reviewed tip absent, and the refusal names both. MUTATION: gating
        the refusal on the reviewed tip only falls through to the
        undetermined-object refusal, which names neither reading, and fails
        the first assertIn on a tip's reading."""
        row, reviewed, cure = self.fix_row()
        with open(os.path.join(self.repo, "lane.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("reviewed work\n")
        with open(os.path.join(self.repo, "other.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("a different patch\n")
        for argv in (("add", "lane.txt", "other.txt"),
                     ("commit", "-q", "-m", "reviewed content, other patch"),
                     ("cherry-pick", cure)):
            done = subprocess.run(["git", "-C", self.repo, *argv],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        pinned = self.git("rev-parse", self.main)
        gitdir = row["repo_id"]
        self.assertEqual(landreq._landing_proof(gitdir, cure, pinned),
                         "patch-equivalent")
        self.assertEqual(landreq._landing_proof(gitdir, reviewed, pinned),
                         "absent")
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assert_landed_refusal(row, why, {cure: "patch-equivalent",
                                              reviewed: "absent"})

    def test_a_deleted_lane_branch_retains_nothing_and_refuses(self):
        row, _reviewed, _cure = self.fix_row()
        self.git("branch", "-D", self.BRANCH)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("no longer exists", why)
        self.assertIn("reviewed-object-destroyed", why)

    def test_a_row_not_owed_by_its_author_is_not_this_reason(self):
        """An APPROVE hands the next move to the lander, not the author."""
        self.git("checkout", "-q", "-b", self.BRANCH, self.main)
        tip = self.dated_commit("approved work", 20)
        self.git("checkout", "-q", self.main)
        row = self.verdicted(polarity="approve", author=self.AUTHOR,
                             reviewer=self.REVIEWER, ref=tip)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("only an owed cure", why)

    def test_the_cli_dry_run_prints_the_retained_lane_and_writes_nothing(self):
        row, _reviewed, cure = self.fix_row()
        before = self.ledger_state(row["id"])
        r_patch, a_patch = self.world()
        with r_patch, a_patch:
            rc, out, err = run(["retire", row["id"][:12], "--reason",
                                self.REASON, "--dry-run", "--seat",
                                "integrator"])
        self.assertEqual(rc, 0, err)
        self.assertIn("RETAINS refs/heads/%s at %s" % (self.BRANCH, cure), out)
        self.assertEqual(self.ledger_state(row["id"]), before)

    def test_the_discharge_register_names_only_the_reviewer_and_only_at_one_door(self):
        """The register's reasons are registered reasons, and the set-aside is
        empty for the sweep and for a row with no recorded FIX verdict. The
        admitting arm above is the positive control that it is non-empty for a
        FIX row at the single-id door."""
        for reason, roles in landreq._RETIRE_BELT_DISCHARGED.items():
            self.assertIn(reason, dispatches.RETIRE_REASONS)
            self.assertEqual(tuple(roles), ("recipient",))
        row, _reviewed, _cure = self.fix_row()
        fresh = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(landreq._retire_discharged_roles(
            self.REASON, fresh, landreq.RETIRE_CALL_SINGLE_ID), ("recipient",))
        self.assertEqual(landreq._retire_discharged_roles(
            self.REASON, fresh, landreq.RETIRE_CALL_SWEEP), ())
        self.assertEqual(landreq._retire_discharged_roles(
            "author-unresolvable", fresh, landreq.RETIRE_CALL_SINGLE_ID), ())
        opened = self.open_row(author=self.AUTHOR, reviewer=self.REVIEWER,
                               lane="absent-author-open", ref=self.BRANCH)
        self.assertEqual(landreq._retire_discharged_roles(
            self.REASON, dispatches.snapshot()[0][opened["id"]],
            landreq.RETIRE_CALL_SINGLE_ID), ())

    def test_a_cherry_picked_land_refuses_and_names_no_command(self):  # noqa: VACUOUS_ASSERTION — the patch-equivalent landing proof and the readings asserted on the refusal are unconditional positives read before the unchanged ledger pair
        """A land that gave the cure a NEW sha on trunk. Ancestry reads both
        tips off trunk; the patch-id reading finds them landed, so the row
        refuses naming both readings and no command. The control is the admitting arm on
        the same producers with no pick. MUTATION: reading the tips with
        `_ancestry` instead of `_landing_proof` admits this row."""
        row, reviewed, cure = self.fix_row()
        picked = subprocess.run(
            ["git", "-C", self.repo, "cherry-pick",
             "%s..%s" % (self.main, self.BRANCH)],
            capture_output=True, text=True)
        self.assertEqual(picked.returncode, 0, picked.stderr)
        pinned = self.git("rev-parse", self.main)
        self.assertNotEqual(pinned, cure)
        gitdir = row["repo_id"]
        for sha in (reviewed, cure):
            self.assertEqual(landreq._ancestry(gitdir, sha, pinned),
                             landreq.NOT_ANCESTOR,
                             "the premise failed: the pick kept an ancestry "
                             "link, so this arm is the fast-forward arm")
        self.assertEqual(landreq._landing_proof(gitdir, cure, pinned),
                         "patch-equivalent")
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assert_landed_refusal(row, why, {cure: "patch-equivalent",
                                              reviewed: "patch-equivalent"})

    def plant_spawn_record(self, name):
        """The spawn register entry helm's own launcher leaves: a minted family
        and instance config plus spawn.json at seat's path for it."""
        from helm import orcaadopt, pk, seat
        family = "codex"
        for d in (seat.seat_dir(family), seat._instance_dir(family, name)):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "config.yaml"), "w",
                      encoding="utf-8") as fh:
                fh.write("port: 0\n")
        path = seat._spawn_path(seat._instance_dir(family, name))
        pk.write_json(path, {"seat": name, "worktree": self.repo})
        self.assertIn(name, orcaadopt.helm_spawned(),
                      "the premise failed: the register does not list the "
                      "planted record, so the arm would measure nothing")
        return path

    def test_an_author_in_the_spawn_register_is_present_to_helm(self):
        """helm can resume a seat its own register holds, so that author is not
        absent. CONTROL in the same arm: the record removed, the same row
        admits. MUTATION: deleting the spawn-register read admits the first
        call."""
        row, _reviewed, _cure = self.fix_row()
        path = self.plant_spawn_record(self.AUTHOR)
        r_patch, a_patch = self.world()
        with r_patch, a_patch:
            state, detail = landreq.seat_reach(self.AUTHOR)
        self.assertEqual(state, landreq.SEAT_ABSENT, detail)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("spawn register record at %s" % path, why)
        self.assertIn("PRESENT to helm", why)
        self.assertNotIn("seat resume", why,
                         "the register proves helm holds a record, not that "
                         "the seat's session, identity or endpoint resume")
        os.remove(path)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    def test_an_unreadable_spawn_register_is_unknown_and_refuses(self):
        """The census itself raises: the refusal is this reason's UNKNOWN and
        not a crash. Only the measurement's census fails; `seat_reach` reads
        the lenient resolver, which still answers. MUTATION: reading an
        exception as an empty register admits. The control is the admitting
        arm, whose register reads."""
        from helm import orcaadopt
        row, _reviewed, _cure = self.fix_row()
        before = self.ledger_state(row["id"])
        with mock.patch.object(orcaadopt, "spawn_register_census",
                               side_effect=OSError("seats root unreadable")) \
                as census:
            out, why = self.retire(row)
        self.assertTrue(census.called, "the measurement never read the register")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("spawn register could not be read", why)
        self.assertIn("seats root unreadable", why)
        self.assertIn("UNKNOWN", why)
        self.assertIn("--reason %s --dry-run" % self.REASON, why)

    def test_an_unreadable_spawn_record_is_unknown_not_absent(self):  # noqa: VACUOUS_ASSERTION — the premise reads assert the lenient resolver omits the malformed record and the census names its path; the same file then made readable refuses PRESENT and removed admits, both positive controls on one world
        """A spawn.json that exists and does not parse. The lenient resolver
        `orcaadopt.helm_spawned` omits it, which is what made its silence
        read as absence. Through the real measurement the row refuses
        UNKNOWN naming the file, at the door and in the locked re-measure,
        and appends nothing. CONTROLS on the same file: made readable and
        matching, it refuses PRESENT; removed, the row admits. MUTATION:
        ignoring the census's unreadable list admits the first call."""
        from helm import orcaadopt
        row, _reviewed, _cure = self.fix_row()
        path = self.plant_spawn_record(self.AUTHOR)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"seat": "%s", "worktree": ' % self.AUTHOR)
        self.assertNotIn(self.AUTHOR, orcaadopt.helm_spawned(),
                         "the premise failed: the resolver read the record, so "
                         "this arm is the PRESENT arm")
        _records, unreadable = orcaadopt.spawn_register_census()
        self.assertEqual([p for p, _why in unreadable], [path])
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("UNKNOWN", why)
        self.assertIn(path, why)
        self.assertIn("--reason %s --dry-run" % self.REASON, why)
        # THE LOCKED RE-MEASURE, alone: a preflight handed in skips the door's
        # measurement, so only the run under the ledger lock can refuse.
        out, why = self.retire(row, preflight="door measurement skipped")
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("UNKNOWN", why)
        self.assertIn(path, why)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"seat": self.AUTHOR, "worktree": self.repo}, fh)
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("PRESENT to helm", why)
        os.remove(path)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    def test_a_non_object_spawn_record_and_an_unlistable_instances_dir_are_unknown(self):  # noqa: VACUOUS_ASSERTION — each world asserts the census names the planted path and the refusal names it; the admitting arm is the control on the same producers
        """The census names the two other failures the lenient walk reads as
        absence: a spawn.json holding a JSON value that is not an object, and
        an `instances` path that cannot be listed. Each refuses UNKNOWN
        naming its path and appends nothing."""
        from helm import orcaadopt, seat
        row, _reviewed, _cure = self.fix_row()
        path = self.plant_spawn_record(self.AUTHOR)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("null\n")
        before = self.ledger_state(row["id"])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("UNKNOWN", why)
        self.assertIn(path, why)
        os.remove(path)
        root = os.path.join(seat.seat_dir("codex"), "instances")
        shutil.rmtree(root)
        with open(root, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")
        _records, unreadable = orcaadopt.spawn_register_census()
        self.assertEqual([p for p, _why in unreadable], [root])
        out, why = self.retire(row)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("UNKNOWN", why)
        self.assertIn(root, why)

    def assert_present(self, row, path):
        """The row refuses PRESENT naming the register record at `path`, at
        the door and in the locked re-measure alone, appends nothing, and
        promises no resume."""
        before = self.ledger_state(row["id"])
        for kw in ({}, {"preflight": "door measurement skipped"}):
            out, why = self.retire(row, **kw)
            self.assertIsNone(out, out)
            self.assert_nothing_appended(row["id"], before, why)
            self.assertIn("spawn register record at %s" % path, why)
            self.assertIn("PRESENT to helm", why)
            self.assertNotIn("seat resume", why)

    def assert_unknown(self, row, path):
        """The row refuses UNKNOWN naming `path`, at the door and in the locked
        re-measure alone, and appends nothing."""
        before = self.ledger_state(row["id"])
        for kw in ({}, {"preflight": "door measurement skipped"}):
            out, why = self.retire(row, **kw)
            self.assertIsNone(out, out)
            self.assert_nothing_appended(row["id"], before, why)
            self.assertIn("could not be read at %s" % path, why)
            self.assertIn("UNKNOWN", why)

    def produce_proxy_register(self, name):
        """(record path, instance config path) for a numbered proxy seat, made
        by the producers `helm seat spawn` runs: `_mint_instance_proxy` writes
        the instance's own config.yaml and token, and `_register_spawn` writes
        its spawn.json. The family config is the one a minted family holds.
        The register's roster mirror is refused, because the author this
        reason measures has no roster row; spawn.json stays authoritative."""
        from helm import orcaadopt, pk, seat, seats
        family = "codex"
        os.makedirs(seat.seat_dir(family), exist_ok=True)
        with open(os.path.join(seat.seat_dir(family), "config.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write("port: 0\n")
        home_dir = seat._mint_instance_proxy(family, name)
        d = seat._instance_dir(family, name)
        self.assertEqual(os.path.realpath(home_dir), os.path.realpath(d))
        rec = {"v": 1, "seat": name, "identity": name, "family": family,
               "project": None, "role": None, "worktree": self.repo,
               "room": None, "room_source": None, "room_worktree": None,
               "launch_sh": os.path.join(d, "launch.sh"), "model": None,
               "ts": pk.now_ts(), "session": None}
        with mock.patch.object(seats, "write_roster",
                               side_effect=OSError("no roster row")):
            self.assertTrue(seat._register_spawn(name, name, d, rec))
        path = seat._spawn_path(d)
        config = os.path.join(d, "config.yaml")
        self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(os.path.isfile(config), config)
        self.assertIn(name, orcaadopt.helm_spawned(),
                      "the premise failed: the minted-proxy resolver does not "
                      "list the produced register")
        return path, config

    def test_a_register_whose_config_is_gone_is_still_present(self):  # noqa: VACUOUS_ASSERTION — each world first asserts the minted-proxy resolver omits the author while registered_seats and the census hold its record; the restored config refusing PRESENT, the malformed record refusing UNKNOWN and the removed record admitting are positive controls on the same producers
        """A numbered proxy seat's register, made by the spawn producers,
        survives while ONLY its instance config.yaml is gone, then while only
        its family config.yaml is gone. `helm_spawned` skips a seat whose
        config is missing before it looks for spawn.json, so a census over
        that domain read the author as absent. The register domain is the one
        `registered_seats` walks, and there the record is still helm's own:
        the row refuses PRESENT naming the record, at the door and in the
        locked re-measure. CONTROLS, every other gate fixed: the config
        restored, the same record refuses PRESENT; config present and the
        record malformed, UNKNOWN naming it; the record removed, the row
        admits. MUTATION: gating the census on the instance config admits the
        first call."""
        from helm import orcaadopt, seat
        self.AUTHOR = "codex-97"
        row, _reviewed, _cure = self.fix_row()
        path, config = self.produce_proxy_register(self.AUTHOR)
        family_config = os.path.join(seat.seat_dir("codex"), "config.yaml")
        for gone in (config, family_config):
            with open(gone, encoding="utf-8") as fh:
                kept = fh.read()
            os.remove(gone)
            self.assertNotIn(self.AUTHOR, orcaadopt.helm_spawned(),
                             "the premise failed: the resolver still lists the "
                             "author with %s gone" % gone)
            names, blind = seat.registered_seats()
            self.assertIn(self.AUTHOR, names)
            self.assertFalse(blind)
            self.assert_present(row, path)
            records, unreadable = orcaadopt.spawn_register_census()
            self.assertEqual(unreadable, [])
            self.assertIn(path, [p for _n, p, _r in records])
            with open(gone, "w", encoding="utf-8") as fh:
                fh.write(kept)
            self.assert_present(row, path)
        with open(path, encoding="utf-8") as fh:
            produced = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(produced[:len(produced) // 2])
        self.assertTrue(os.path.isfile(config))
        self.assert_unknown(row, path)
        os.remove(path)
        self.assertEqual(orcaadopt.spawn_register_census(), ([], []))
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])

    def test_a_dangling_config_link_is_present_and_a_dangling_instance_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the premise reads assert the resolver omits the author and the census holds its record; the removed record admitting and the dangling entry the census names are positive controls on one world
        """The author's spawn.json is intact, but the instance config.yaml
        beside it is a link to nothing. The register does not read config, so
        the record is still helm's and the row refuses PRESENT naming it, at
        the door and in the locked re-measure. A dangling instance DIRECTORY
        is an entry that exists and cannot be read, so it refuses UNKNOWN
        naming that entry. CONTROL: the record removed, the row admits.
        MUTATION: reading a dangling link as absence admits the last call."""
        from helm import orcaadopt, seat
        row, _reviewed, _cure = self.fix_row()
        path = self.plant_spawn_record(self.AUTHOR)
        config = os.path.join(os.path.dirname(path), "config.yaml")
        self.assertTrue(os.path.isfile(config), config)
        os.remove(config)
        os.symlink(config + ".gone", config)
        self.assertNotIn(self.AUTHOR, orcaadopt.helm_spawned(),
                         "the premise failed: the resolver still reads the "
                         "record, so the config link is not what this arm "
                         "measures")
        records, unreadable = orcaadopt.spawn_register_census()
        self.assertEqual(unreadable, [])
        self.assertIn(path, [p for _n, p, _r in records])
        self.assert_present(row, path)
        os.remove(path)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])
        entry = os.path.join(seat.seat_dir("codex"), "instances", "dangling")
        os.symlink(entry + ".gone", entry)
        _records, unreadable = orcaadopt.spawn_register_census()
        self.assertEqual([p for p, _why in unreadable], [entry])
        self.assert_unknown(row, entry)

    def test_helms_own_port_files_beside_the_seats_hold_no_register(self):  # noqa: VACUOUS_ASSERTION — the PRESENT refusal beside the produced port files is the positive control, the files are asserted regular at the producer's paths, and the removed record admits at both measurements
        """Allocating a project instance's port writes
        `<seats>/.instance-ports.lock` and `<seats>/instance-ports.json`, two
        regular files beside the seat directories. They hold no register, so
        an author with a readable record beside them still refuses PRESENT
        naming it, at the door and in the locked re-measure, and with the
        record removed the row admits at both. MUTATION: enumerating a regular
        file as a seats entry reads its `spawn.json` and `instances` as
        unreadable, and every call refuses UNKNOWN."""
        import stat
        from helm import orcaadopt, seat, seat_paths
        row, _reviewed, _cure = self.fix_row()
        path = self.plant_spawn_record(self.AUTHOR)
        port, err = seat.allocate_instance_port("proj-absent-codex")
        self.assertIsNone(err, err)
        self.assertIsNotNone(port)
        root = seat_paths.seats_root()
        self.assertEqual(os.path.dirname(seat.seat_dir("codex")), root)
        for produced in (os.path.join(root, ".instance-ports.lock"),
                         seat_paths._instance_ports_path()):
            self.assertTrue(stat.S_ISREG(os.lstat(produced).st_mode),
                            produced)
        self.assert_present(row, path)
        records, unreadable = orcaadopt.spawn_register_census()
        self.assertEqual(unreadable, [])
        self.assertEqual([p for _n, p, _r in records], [path])
        names, blind = seat.registered_seats()
        self.assertIn(self.AUTHOR, names)
        self.assertFalse(blind)
        os.remove(path)
        out, why = self.retire(row, dry_run=True)
        self.assertIsNone(why, why)
        self.assertTrue(out["would_append"])
        out, why = self.retire(row, preflight="door measurement skipped")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        self.assertEqual(orcaadopt.spawn_register_census(), ([], []))

    def test_a_rostered_absent_author_is_out_of_scope_and_says_so(self):
        """An author with its own roster row reads ABSENT through reach and
        CURRENT through lineage. The refusal says this reason does not apply
        and names no door that refuses the row. MUTATION: the old text that
        pointed at `author-unresolvable` fails the assertNotIn."""
        row, _reviewed, _cure = self.fix_row()
        roster = {self.REVIEWER: {"session": "live-session"},
                  self.AUTHOR: {}}
        presence = [{"seat": self.REVIEWER, "presence": "fresh"},
                    {"seat": self.AUTHOR, "presence": "absent"}]
        r_patch, a_patch = self.world(roster=roster, presence=presence)
        with r_patch, a_patch:
            state, detail = landreq.seat_reach(self.AUTHOR)
        self.assertEqual(state, landreq.SEAT_ABSENT, detail)
        before = self.ledger_state(row["id"])
        out, why = self.retire(row, roster=roster, presence=presence)
        self.assertIsNone(out)
        self.assert_nothing_appended(row["id"], before, why)
        self.assertIn("still has a roster row", why)
        self.assertIn("No helm verb removes a roster row", why)
        self.assertIn("helm seat where %s" % self.AUTHOR, why)
        self.assertNotIn("author-unresolvable", why)

    def test_a_tier_parked_approve_successor_is_not_a_review_of_the_cure(self):
        """An APPROVE successor on the parent's chain counts as a review of the
        commit it names, until it is retired `tier-unevaluable-parked`: that
        retirement measured its tier DARK. CONTROL in the same arm: the same
        successor before retirement is listed. MUTATION: dropping the
        retire-reason clause in `dispatches._verdict_is_a_review` keeps it."""
        self.assertLessEqual(dispatches._RETIRED_UNREVIEWED,
                             set(dispatches.RETIRE_REASONS))
        parent = self.verdicted(polarity="fix", author="deleted-author",
                                reviewer="parked-reviewer")
        kid = self.verdicted(polarity="approve", author="deleted-author",
                             reviewer="parked-reviewer",
                             supersedes=parent["id"])
        snap = dispatches.snapshot()[0]
        self.assertEqual(
            {t: r["id"] for t, r in dispatches.chain_reviewed_tips(
                snap[parent["id"]], snap).items()},
            {self.side.lower(): kid["id"]})
        roster = self.roster({"parked-reviewer": None, "seat-a": "s"})
        r_patch, a_patch = self.reach(roster=roster,
                                      panes=self.panes("seat-a"))
        dark = self.tier(dispatches.TIER_DARK, "no runtime proof ever stored")
        with r_patch, a_patch, self.tier_lens(dark):
            out, why = landreq.retire(kid["id"], "tier-unevaluable-parked",
                                      seat="integrator")
        self.assertIsNone(why, why)
        self.assertTrue(out["retired_admin"])
        snap = dispatches.snapshot()[0]
        self.assertEqual(dispatches.chain_reviewed_tips(
            snap[parent["id"]], snap), {})


class RetireRefusalsNameMeasuredDoorsTest(RetireBase):
    """EVERY RETIREMENT A RETIRE REFUSAL OFFERS IS DRIVEN ON THE ROW THAT
    REACHED IT, the same discipline `RefusalsNameMeasuredDoorsTest` holds
    the close doors to.

    THE LIVENESS BELT IS WHAT MAKES THESE OFFERS CONDITIONAL. Two reasons set
    part of the belt aside for one named row — `reviewed-object-destroyed` all
    of it, `author-absent-lane-idle` the reviewer who already spent its move —
    and both offer `repo-unreadable` for a row with no repository binding.
    `repo-unreadable` sets nothing aside. So over the rows those two reasons
    exist for, where a party is still live, it refuses; with every party
    ABSENT it admits. An unconditional "wants --reason repo-unreadable" is
    false for the first world, and each arm measures both.

    OFFERED means spelled `NAME` or `--reason NAME`, over the retire reasons.
    """

    OFFER = r"(`%s`|--reason %s(?![\w-]))"

    AUTHOR = AbsentAuthorLaneIdleTest.AUTHOR
    REVIEWER = AbsentAuthorLaneIdleTest.REVIEWER
    BRANCH = AbsentAuthorLaneIdleTest.BRANCH
    DROP = AbsentAuthorLaneIdleTest.DROP
    dated_commit = AbsentAuthorLaneIdleTest.dated_commit
    fix_row = AbsentAuthorLaneIdleTest.fix_row
    live_reviewer_world = AbsentAuthorLaneIdleTest.world
    rewrite_opener = AbsentAuthorLaneIdleTest.rewrite_opener
    doomed_row = DestroyedReviewedObjectTest.doomed_row

    def offered(self, why):
        return [reason for reason in landreq.RETIRE_REASONS
                if re.search(self.OFFER % (re.escape(reason),
                                           re.escape(reason)), why)]

    def seat_live_world(self, seat):
        """`seat` LIVE — a session, a pane, a fresh presence row and an act
        today — and every other party on the row not proven gone."""
        activity = ({seat: _ago(0), self.AUTHOR: _ago(30)}, {}, _ago(0), None)
        return self.reach(roster=self.roster({seat: "live-session"}),
                          panes=self.panes(seat),
                          presence=[{"seat": seat, "presence": "fresh"}],
                          activity=activity)

    def all_absent_world(self):
        """Every party on either fixture's rows measured ABSENT. A live
        bystander is rostered because the presence projection describes the
        fleet only while it reports some live seat; with none it measures
        nothing, and every absence reads UNKNOWN."""
        quiet = {name: _ago(30) for name in (
            self.AUTHOR, self.REVIEWER, "ghost-author", "ghost-reviewer")}
        quiet["bystander"] = _ago(0)
        return self.reach(
            roster=self.roster({self.REVIEWER: None,
                                "bystander": "live-session"}),
            panes=self.panes("bystander"),
            presence=[{"seat": self.REVIEWER, "presence": "absent"},
                      {"seat": "ghost-author", "presence": "absent"},
                      {"seat": "ghost-reviewer", "presence": "absent"},
                      {"seat": "bystander", "presence": "fresh"}],
            activity=(quiet, {}, _ago(0), None))

    def retire_in(self, world, row, reason):
        r_patch, a_patch = world
        with r_patch, a_patch:
            return landreq.retire(row["id"], reason, seat="integrator",
                                  dry_run=True)

    def admits(self, world, row, reason):
        out, why = self.retire_in(world, row, reason)
        self.assertIsNone(why, "`%s` refused a row a refusal offered it for: "
                          "%s" % (reason, why))
        self.assertEqual(out["reason"], reason)
        self.assertIs(out["would_append"], True)
        return out

    def refuses(self, world, row, reason):
        out, why = self.retire_in(world, row, reason)
        self.assertIsNone(out, "`%s` admitted: %r" % (reason, out))
        self.assertTrue(why)
        return why

    def test_absent_author_offers_repo_unreadable_with_the_belt_it_runs(self):
        """THE REASON'S OWN POPULATION: a live reviewer, an absent author. The
        row loses its repository binding, `author-absent-lane-idle` refuses
        and offers `repo-unreadable`, and on that same world `repo-unreadable`
        REFUSES — its belt asks about the reviewer this reason set aside.
        With every party ABSENT it admits, which is the condition the offer
        states."""
        row, _reviewed, _cure = self.fix_row()
        self.admits(self.live_reviewer_world(), row,
                    "author-absent-lane-idle")
        self.rewrite_opener(row["id"], "repo_id", self.DROP)
        why = self.refuses(self.live_reviewer_world(), row,
                           "author-absent-lane-idle")
        self.assertIn("records no repository binding", why)
        self.assertEqual(self.offered(why), ["repo-unreadable"], why)
        self.assertIn("including the reviewer this reason sets aside", why)
        self.assertIn("admits only once every party on the row reads ABSENT",
                      why)
        why = self.refuses(self.live_reviewer_world(), row,
                           "repo-unreadable")
        self.assertIn("@%s (recipient), which is REACHABLE" % self.REVIEWER,
                      why)
        self.admits(self.all_absent_world(), row, "repo-unreadable")

    def test_a_destroyed_object_offers_repo_unreadable_with_the_belt_it_runs(self):
        """`reviewed-object-destroyed` sets the whole belt aside for one named
        row. Over a row with no repository binding it cannot measure the
        object and offers `repo-unreadable`, which refuses while the reviewer
        is live and admits once every party reads ABSENT."""
        row, tip = self.doomed_row(polarity="concur")
        self.prune(tip, "refs/heads/doomed")
        self.admits(self.seat_live_world("ghost-reviewer"), row,
                    "reviewed-object-destroyed")
        self.rewrite_opener(row["id"], "repo_id", self.DROP)
        why = self.refuses(self.seat_live_world("ghost-reviewer"), row,
                           "reviewed-object-destroyed")
        self.assertIn("records no repository binding", why)
        self.assertEqual(self.offered(why), ["repo-unreadable"], why)
        self.assertIn("runs the full liveness belt, which this reason sets "
                      "aside", why)
        self.refuses(self.seat_live_world("ghost-reviewer"), row,
                     "repo-unreadable")
        self.admits(self.all_absent_world(), row, "repo-unreadable")

    def test_a_deleted_lane_branch_offers_the_object_that_went_with_it(self):
        """`author-absent-lane-idle` over a lane branch that is gone offers
        `reviewed-object-destroyed` for "a row whose reviewed object went
        with it". Measured both ways on one row: while the object still
        reads, that reason refuses it; once gc takes it, that reason
        admits."""
        row, reviewed, _cure = self.fix_row()
        self.git("branch", "-D", self.BRANCH)
        why = self.refuses(self.live_reviewer_world(), row,
                           "author-absent-lane-idle")
        self.assertIn("no longer exists", why)
        self.assertEqual(self.offered(why), ["reviewed-object-destroyed"],
                         why)
        why = self.refuses(self.live_reviewer_world(), row,
                           "reviewed-object-destroyed")
        self.assertIn("IS still present", why)
        self.prune(reviewed)
        self.admits(self.live_reviewer_world(), row,
                    "reviewed-object-destroyed")

    def test_an_unmeasurable_landing_offers_the_gone_objects_reason(self):
        """The lane was rewritten past the reviewed commit and gc took it, so
        its landing reads UNKNOWN. `author-absent-lane-idle` offers
        `reviewed-object-destroyed` for "a reviewed object that is gone", and
        it admits."""
        row, reviewed, _cure = self.fix_row()
        self.git("checkout", "-q", self.BRANCH)
        self.git("reset", "-q", "--hard", self.main)
        self.dated_commit("the lane rewritten past its reviewed commit", 20)
        self.git("checkout", "-q", self.main)
        self.prune(reviewed)
        why = self.refuses(self.live_reviewer_world(), row,
                           "author-absent-lane-idle")
        self.assertIn("NOT measured unlanded", why)
        self.assertEqual(self.offered(why), ["reviewed-object-destroyed"],
                         why)
        self.admits(self.live_reviewer_world(), row,
                    "reviewed-object-destroyed")


class OffFrontierTest(RetireBase):
    """`helm lr retire --off-frontier` — the residue the header counts apart
    from work, and the predicate that decides which side a row is on.

    EVERY ARM DRIVES THE REAL GIT, never a mock of it. The fixture repo mints
    the branch, the commit and the reap, so the four worlds under test — a
    live lane, a tip on trunk, a tip on no ref at all, and a tip git gc
    destroyed — are worlds this repository is really in. The only thing
    doubled here is the roster (inherited from `RetireBase`), because the
    reachability instruments are not this verb's subject.

    THE MUST-HIT IS THE ON-FRONTIER ARM. A predicate that answered
    `abandoned-unreachable` for everything would pass every clearance arm
    below and destroy the board, so the first arm proves the probe can say NO
    about a row whose only difference is one live ref.
    """

    def row_off_frontier(self, lane, ref, author="ghost-author"):
        """One FIX-verdicted row on a lane that has NO ref of its own."""
        row = self.verdicted(polarity="fix", author=author, lane=lane, ref=ref)
        self.assertEqual(
            landreq.lane_branch_presence(landreq.get(row["id"])[0])[0],
            "absent", "the fixture meant this lane to be gone")
        return row

    def census(self, apply_it=False, lrs=None, raw=None):
        report, err = landreq.off_frontier_census(apply_it=apply_it, lrs=lrs,
                                                  raw=raw)
        self.assertIsNone(err, err)
        return report

    def claims_file(self, text):
        """Write the fixture host's claims ledger and hand back its path.

        THE REAL PATH THE REAL READER OPENS — `seats_claims.claims_path()`
        under this test's own HELM_CHAT_DIR — so an arm about an unreadable
        claims file drives the accessor instead of describing it."""
        from helm import seats_claims
        path = seats_claims.claims_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    @staticmethod
    def planned(report):
        return {e["id"]: e for e in report["plan"]}

    def test_a_row_whose_lane_branch_still_exists_is_ON_the_frontier(self):
        """THE MUST-HIT. One live ref is the whole difference between this row
        and the retirable one beside it: same repository, same trunk, a tip
        that is equally an ancestor of main. The probe must answer ON for the
        first and a retirement reason for the second, or it is not measuring
        the lane at all.

        CONTROL: the second row is asserted retirable in the same arm, so an
        `off_frontier_reason` stuck at ON_FRONTIER cannot pass this."""
        landed = self.commit("carried-to-trunk")
        self.git("branch", "lane/still-cut-here", landed)
        live = self.verdicted(polarity="fix", author="ghost-author",
                              lane="still-cut-here", ref=landed)
        gone = self.row_off_frontier("reaped-lane-alpha", landed)
        self.assertEqual(
            landreq.off_frontier_reason(landreq.get(live["id"])[0])["reason"],
            landreq.ON_FRONTIER)
        self.assertEqual(
            landreq.off_frontier_reason(landreq.get(gone["id"])[0])["reason"],
            landreq.OFF_FRONTIER_LANDED_ANCESTRY)
        report = self.census(apply_it=True)
        # THE POSITIVE CONTROL ON THE SAME REPORT: the reaped row IS in the
        # plan, so a census that returned an empty plan — or never ran —
        # cannot satisfy the three absences below.
        self.assertIn(gone["id"], self.planned(report))
        self.assertNotIn(live["id"], self.planned(report))
        self.assertNotIn(live["id"],
                         {e["id"] for e in report["unclassified_rows"]})
        self.assertEqual(landreq.get(live["id"])[0]["terminal"], False)

    def test_a_tip_on_trunk_under_a_reaped_lane_is_landed_by_ancestry(self):
        """The dominant shape: the lane was reaped after the work landed, so
        ancestry answers YES and the row is retired under the close reason the
        EXISTING registry already holds for exactly that proof."""
        landed = self.commit("this-one-landed")
        row = self.row_off_frontier("reaped-lane-bravo", landed)
        verdict = landreq.off_frontier_reason(landreq.get(row["id"])[0])
        self.assertEqual(verdict["reason"],
                         landreq.OFF_FRONTIER_LANDED_ANCESTRY)
        self.assertEqual(verdict["close_proof_mode"], "ancestor")
        self.assertIn(landed[:12], verdict["evidence"])
        entry = self.planned(self.census())[row["id"]]
        # THE ROUTE READS THE POLARITY TOO: this row is a FIX, so the door is
        # the contrary one. `landed` would be refused by its own ladder, which
        # is what the first cut of this verb did 286 times out of 286.
        self.assertEqual(entry["polarity"], "fix")
        self.assertEqual(entry["close_reason"],
                         landreq.off_frontier_route(
                             landreq.OFF_FRONTIER_LANDED_ANCESTRY, "fix"))
        self.assertEqual(entry["close_reason"], "carried")
        self.assertIn(entry["close_reason"], dispatches.CLOSE_REASONS)

    def test_a_tip_on_no_ref_at_all_is_abandoned_unreachable(self):
        """The lane was cut, work was committed on it, and the branch was
        deleted without landing. The object survives in the object store — so
        this is NOT the `stranded` world, where the object is pruned — and no
        ref in the repository reaches it."""
        self.git("checkout", "-q", "-b", "lane/reaped-lane-charlie", self.main)
        orphan = self.commit("never-landed", path="orphan")
        self.git("checkout", "-q", self.main)
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-charlie", ref=orphan)
        self.git("branch", "-D", "lane/reaped-lane-charlie")
        # THE FIXTURE'S OWN CONTROLS, both directions: the object is still
        # there (else this is the pruned world one arm down) and nothing
        # reaches it (else the probe is right to refuse).
        self.assertIs(landreq._object_exists(self.gitdir(), orphan), True)
        self.assertEqual(landreq._reaching_ref(self.gitdir(), orphan),
                         (None, None))
        verdict = landreq.off_frontier_reason(landreq.get(row["id"])[0])
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_ABANDONED)
        entry = self.planned(self.census())[row["id"]]
        self.assertEqual(entry["close_reason"],
                         landreq.off_frontier_route(
                             landreq.OFF_FRONTIER_ABANDONED, "fix"))
        # THE ROUTE IS `withdrawn`, NOT `stranded`, and the difference is the
        # whole reachability of this door. `stranded` demands a PRUNED object
        # while the two fixture controls above prove this one PRESENT — so
        # that route was refused by construction, every time, for a reason the
        # route itself guaranteed. `withdrawn` asks what this bucket actually
        # measured: off trunk by ancestry AND absent by patch id.
        self.assertEqual(entry["close_reason"], "withdrawn")
        # AND THE WRITE REALLY HAPPENS — the arm that the first cut could not
        # have: the row CLOSES, under the routed word, with the measurement on
        # the event.
        report = self.census(apply_it=True)
        closed = {e["id"]: e for e in report["closed"]}
        self.assertIn(row["id"], closed, report["refused"])
        self.assertEqual(closed[row["id"]]["close_reason"], "withdrawn")
        after = landreq.get(row["id"])[0]
        self.assertEqual(after["terminal"], True)
        self.assertEqual(after["close_reason"], "withdrawn")
        self.assertIn(orphan[:12], after["close_evidence"])

    def test_a_tip_git_gc_destroyed_is_UNCLASSIFIED_and_never_applied(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same report is `assertGreaterEqual(report['unclassified'], 1)` plus this row's id in unclassified_rows, so the plan-absence and the unchanged ledger beside it are measured non-effects
        """task/2383's population. The lane is gone AND the tip no longer
        resolves, so no reader can place it — and a door that cannot tell must
        not close work. The row is counted, printed with its rung, and left
        exactly where it was.

        THE ASSERTION IS THE EFFECT, not the absence of a complaint: the
        row's ledger state is read before and after an `--apply` run."""
        self.git("checkout", "-q", "-b", "lane/reaped-lane-delta", self.main)
        doomed = self.commit("about-to-be-pruned", path="doomed")
        self.git("checkout", "-q", self.main)
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-delta", ref=doomed)
        self.prune(doomed, "refs/heads/lane/reaped-lane-delta")
        verdict = landreq.off_frontier_reason(landreq.get(row["id"])[0])
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(verdict["rung"], "object")
        self.assertIn("task/2383", verdict["evidence"])
        before = self.ledger_state(row["id"])
        report = self.census(apply_it=True)
        # POSITIVE CONTROL, unconditional and on the same report: the row was
        # really classified, and counted into the bucket whose whole contract
        # is that `--apply` skips it.
        self.assertGreaterEqual(report["unclassified"], 1)
        self.assertIn(row["id"],
                      {e["id"] for e in report["unclassified_rows"]})
        self.assertNotIn(row["id"], self.planned(report))
        self.assertEqual(self.ledger_state(row["id"]), before)
        self.assertEqual(landreq.get(row["id"])[0]["terminal"], False)

    def test_a_ref_outside_the_lane_family_keeps_the_row_UNCLASSIFIED(self):
        """Measured, and still a refusal. The tip is not on trunk and the lane
        family is gone, but SOMETHING in this repository still reaches the
        commit — a retired-lane copy, an archive tag, a rescue branch. That is
        work retained under another name, which is neither a landing nor
        debris, so it is a row a human reads and never one `--apply` closes."""
        self.git("checkout", "-q", "-b", "lane/reaped-lane-echo", self.main)
        kept = self.commit("retained-elsewhere", path="kept")
        self.git("checkout", "-q", self.main)
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-echo", ref=kept)
        self.git("update-ref", "refs/helm-retired/salvage/unrelated-name", kept)
        self.git("branch", "-D", "lane/reaped-lane-echo")
        verdict = landreq.off_frontier_reason(landreq.get(row["id"])[0])
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(verdict["rung"], "reachability")
        self.assertIn("refs/helm-retired/salvage/unrelated-name",
                      verdict["evidence"])

    def test_a_second_apply_writes_nothing_the_first_did_not(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SECOND report is this row's id in its plan: the census still sees and classifies the row, so `closed == []` is a measurement and not the silence of a census that stopped looking
        """IDEMPOTENCE IS A PROPERTY OF THE POPULATION, not of a retry code
        path. A row the first run CLOSED is terminal, so it leaves
        `open_bucket_rows` and the second census cannot see it; a row the
        first run's ladder REFUSED appended nothing, so the second run reaches
        the same ladder and gets the same answer. Either way the ledger after
        the second run is byte-for-byte what it was after the first, and that
        is what this asserts — never merely that the second run did not crash.

        THE CONTROL IS THE FIRST RUN'S OWN EFFECT: the arm asserts the row was
        classified and routed (it is in the plan) before asserting the second
        run changed nothing, so a census that silently stopped seeing the row
        cannot pass this."""
        landed = self.commit("landed-then-swept")
        row = self.row_off_frontier("reaped-lane-foxtrot", landed)
        first = self.census(apply_it=True)
        self.assertIn(row["id"], self.planned(first))
        after_first = self.ledger_state(row["id"])
        second = self.census(apply_it=True)
        # POSITIVE CONTROL on the SECOND report: it still sees and classifies
        # the row, so "nothing was written" is a measurement and not the
        # silence of a census that stopped looking.
        self.assertIn(row["id"], self.planned(second))
        self.assertEqual(self.ledger_state(row["id"]), after_first)
        self.assertEqual(len(second["closed"]), 0)
        self.assertEqual({e["id"] for e in first["closed"]},
                         {e["id"] for e in second["closed"]})

    def test_a_ladder_refusal_is_REPORTED_and_never_forced(self):
        """THE SAFETY PROPERTY OF `--apply`, and the one this verb's whole
        design rests on: it proposes a close reason, and the EXISTING ladder
        decides. This row is the class no door admits — a FIX verdict whose
        reviewed tip is an EXACT ANCESTOR of trunk, so `carried`'s witness has
        an empty range to replay and refuses to measure carriage at all. The
        verb must carry that refusal back in the ladder's own words, name the
        row's own tip in it, and leave the row exactly as it was.

        MEASURED, not assumed: the arm reads the refusal text out of the
        report rather than pinning a sentence this module wrote, and it
        asserts the LEDGER, not the absence of a complaint."""
        landed = self.commit("contrary-debt-on-trunk")
        row = self.row_off_frontier("reaped-lane-juliett", landed)
        before = self.ledger_state(row["id"])
        report = self.census(apply_it=True)
        refused = {e["id"]: e for e in report["refused"]}
        self.assertIn(row["id"], refused)
        self.assertEqual(refused[row["id"]]["close_reason"], "carried")
        self.assertIn(landed[:12], refused[row["id"]]["refusal"])
        self.assertNotIn("working tree", refused[row["id"]]["refusal"])
        self.assertEqual(self.ledger_state(row["id"]), before)
        self.assertEqual(landreq.get(row["id"])[0]["terminal"], False)
        self.assertEqual(report["closed"], [])

    def test_the_header_splits_the_open_count_it_used_to_print_bare(self):
        """THE OWNER-VISIBLE ROW, asserted on the numbers and on the rendered
        line — never on the legend. `open` is unchanged, because the six-bucket
        partition still has to sum to `total` and every consumer reads it; the
        two new terms partition IT, and the rendered line leads with the
        frontier count and names the verb that clears the rest."""
        landed = self.commit("carried-to-trunk")
        self.git("branch", "lane/still-cut-here", landed)
        self.verdicted(polarity="fix", author="ghost-author",
                       lane="still-cut-here", ref=landed)
        self.row_off_frontier("reaped-lane-golf", landed)
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        filed = landreq.filed_split(lrs, raw)
        self.assertEqual(filed["open"],
                         filed["open_frontier"] + filed["off_frontier"])
        self.assertGreaterEqual(filed["off_frontier"], 1)
        self.assertGreaterEqual(filed["open_frontier"], 1)
        line = landreq.filed_line(filed)
        self.assertIn("%d open on the live frontier" % filed["open_frontier"],
                      line)
        self.assertIn("%d OFF-FRONTIER" % filed["off_frontier"], line)
        self.assertIn("helm lr retire --off-frontier", line)
        self.assertNotIn("%d open ·" % filed["open"], line)

    def test_an_unreadable_lane_reading_counts_as_work_OWED(self):
        """THE FAIL-CLOSED DIRECTION, asserted where it costs the owner:
        `unknown` is not `absent`, so a row helm could not place stays in the
        frontier count and never in the residue. Under-reporting a backlog is
        the one error this header may not make.

        The control is the SAME row read twice — once through the real ref
        table (absent, so it lands in the residue) and once with the table
        read refusing — so the arm cannot pass on a probe that answers
        `unknown` for everything."""
        landed = self.commit("carried-to-trunk")
        row = self.row_off_frontier("reaped-lane-hotel", landed)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["id"], row["id"])
        refused = (None, "Git could not enumerate the repository's refs")
        state, why = landreq.lane_branch_presence(lr, table=refused)
        self.assertEqual(state, "unknown")
        self.assertEqual(why, refused[1])
        self.assertEqual(
            landreq.off_frontier_reason(lr, table=refused)["reason"],
            landreq.OFF_FRONTIER_UNCLASSIFIED)
        with mock.patch.object(landreq, "ref_table", return_value=refused):
            lrs, raw, _u = landreq.project_raw()
            filed = landreq.filed_split(lrs, raw)
        self.assertEqual(filed["off_frontier"], 0)
        self.assertEqual(filed["open_frontier"], filed["open"])
        self.assertEqual(landreq.off_frontier_line(filed), "")

    def test_every_off_frontier_reason_maps_into_the_close_registry(self):  # noqa: VACUOUS_ASSERTION — the loop's positives are preceded by two unconditional ones on the same observable: the mapped value set is non-empty and is a subset of dispatches.CLOSE_REASONS
        """NO NEW VOCABULARY AT THE WRITER. Every reason this verb can produce
        resolves to a word `dispatches.CLOSE_REASONS` already admits and that
        `rowstate._CLOSE_TERMINAL` already gives a terminal to — the two
        registers the close writer and the board read. DERIVED from the
        module's own tuple, never transcribed, so a reason added upstream with
        no mapping turns this red instead of reaching an operator."""
        from helm import rowstate
        mapped = {word for doors in landreq.OFF_FRONTIER_CLOSE_REASON.values()
                  for word in doors.values()}
        # UNCONDITIONAL, so the per-reason loop below cannot be the only
        # positive: the mapping is non-empty and every word in it is already
        # in the close writer's own registry.
        self.assertTrue(mapped)
        self.assertTrue(mapped <= set(dispatches.CLOSE_REASONS), mapped)
        self.assertEqual(set(landreq.OFF_FRONTIER_CLOSE_REASON),
                         set(landreq.OFF_FRONTIER_REASONS))
        self.assertNotIn(landreq.OFF_FRONTIER_UNCLASSIFIED,
                         landreq.OFF_FRONTIER_CLOSE_REASON)
        self.assertNotIn(landreq.ON_FRONTIER,
                         landreq.OFF_FRONTIER_CLOSE_REASON)
        # THE ROUTE IS TOTAL OVER BOTH INPUTS, so no measured row can reach
        # `--apply` with no door: every reason crossed with every polarity the
        # close writer knows resolves to a registered word.
        from helm.verdicts import WORK_POLARITIES
        polarities = set(WORK_POLARITIES) | {None}
        for reason in landreq.OFF_FRONTIER_REASONS:
            for polarity in polarities:
                close_reason = landreq.off_frontier_route(reason, polarity)
                self.assertIn(close_reason, dispatches.CLOSE_REASONS,
                              (reason, polarity))
                self.assertIn(close_reason, landreq.CLOSE_CLI_REASONS,
                              (reason, polarity))
                self.assertIn(close_reason, rowstate._CLOSE_TERMINAL,
                              (reason, polarity))
                # AND THE DOOR ADMITS THE POLARITY IT IS HANDED. A route into
                # a reason whose `_CLOSE_POLARITY` refuses this verdict is a
                # refusal the route itself guaranteed — which is exactly what
                # `landed` for a FIX row was, 286 times out of 286.
                self.assertIn(polarity,
                              dispatches._CLOSE_POLARITY[close_reason],
                              (reason, polarity, close_reason))
        # CONTRARY DEBT NEVER ROUTES TO A LANDING WORD, and a resolution never
        # routes to the contrary door — the two halves of the same law.
        for reason in (landreq.OFF_FRONTIER_LANDED_ANCESTRY,
                       landreq.OFF_FRONTIER_LANDED_PATCH_ID):
            self.assertEqual(landreq.off_frontier_route(reason, "approve"),
                             "landed")
            for polarity in landreq.CONTRARY_POLARITIES:
                self.assertEqual(landreq.off_frontier_route(reason, polarity),
                                 "carried")
        # AND `stranded` IS ROUTED TO BY NOTHING: it demands a pruned object,
        # which `off_frontier_reason` disproves before it ever answers
        # `abandoned-unreachable`.
        self.assertNotIn("stranded", mapped)

    def test_the_cli_censuses_without_writing_and_names_the_two_modes(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same observable is `assertIn(row id, out)` plus rc == 0: the census really rendered THIS row, so the unchanged ledger state beside it is a measured non-effect and not an empty run
        """The verb is reachable, the census is the default, and the two
        classifications refuse to run as one command."""
        landed = self.commit("landed-then-listed")
        row = self.row_off_frontier("reaped-lane-india", landed)
        before = self.ledger_state(row["id"])
        rc, out, err = run(["retire", "--off-frontier"])
        self.assertEqual(rc, 0, err)
        self.assertIn("census — nothing written", out)
        self.assertIn(row["id"][:12], out)
        self.assertEqual(self.ledger_state(row["id"]), before)
        rc, _out, err = run(["retire", "--off-frontier", "--sweep"])
        self.assertEqual(rc, 2)
        self.assertIn("two different classifications", err)
        rc, _out, err = run(["retire", "--off-frontier", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("already on", err)
        self.assertIn("retire --off-frontier", landreq.USAGE)

    # ------------------------------------------------------------------
    # THE FRONTIER IS FOUR READINGS, because a NAME is not an IDENTITY
    # ------------------------------------------------------------------
    def held_off_trunk(self, branch, path):
        """A commit on `branch`, off trunk, with trunk checked out after."""
        self.git("checkout", "-q", "-b", branch, self.main)
        tip = self.commit("work-on-" + path, path=path)
        self.git("checkout", "-q", self.main)
        return tip

    def test_a_live_branch_under_another_spelling_keeps_the_row_ON(self):
        """THE MUST-HIT FOR THE COMMIT-KEYED RUNG, and the shape measured on
        the live board: the row's lane LABEL and the branch SPELLING differ,
        so every name probe says the lane is gone while the work is sitting on
        a live branch anybody can check out.

        THE CONTROLS ARE BOTH DIRECTIONS AND BOTH ARE ASSERTED HERE. The name
        probe really does miss (`lane_branch_presence` is `absent` and the two
        stems really do not match — so this arm cannot pass because some other
        rung happened to match the name), and the TWIN row, identical but for
        the one live ref, is placed for retirement on the same census."""
        tip = self.held_off_trunk("lane/derive-row-state-from-artifacts",
                                  "derive-row-state")
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="review-derive-row-state", ref=tip)
        lr = landreq.get(row["id"])[0]
        # CONTROL 1 — the name probes MISS. If they matched, the ON answer
        # below would prove nothing about the new rung.
        self.assertFalse(landreq._stems_match("review-derive-row-state",
                                              "derive-row-state-from-artifacts"))
        self.assertEqual(landreq.lane_branch_presence(lr)[0], "absent")
        self.assertEqual(landreq.lane_room_presence(lr)[0], "absent")
        self.assertEqual(landreq.lane_lease_presence(lr)[0], "absent")
        verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.ON_FRONTIER)
        self.assertEqual(verdict["rung"], "live-hold")
        self.assertIn("lane/derive-row-state-from-artifacts",
                      verdict["evidence"])
        # CONTROL 2 — the twin. Same fixture, same repository, one ref cut.
        twin_tip = self.held_off_trunk("lane/cut-after-review", "cut-after")
        twin = self.verdicted(polarity="fix", author="ghost-author",
                              lane="review-cut-after", ref=twin_tip)
        self.git("branch", "-D", "lane/cut-after-review")
        report = self.census()
        self.assertIn(twin["id"], self.planned(report))
        self.assertNotIn(row["id"], self.planned(report))
        self.assertNotIn(row["id"],
                         {e["id"] for e in report["unclassified_rows"]})

    def test_a_live_lease_on_the_lane_keeps_the_row_ON_the_frontier(self):
        """The brief's arm, and the one that cost two GUARDED rooms: the lane
        branch is DELETED and a seat still holds the lease. `helm work list`
        renders such a lane with `resume: helm work claim <lane>` — it is work
        somebody is holding, whatever the refs say.

        The lease is taken through the REAL claims writer against the
        fixture's own isolated home, so the rung reads a ledger row a seat
        could really have written."""
        from helm import seats_claims
        tip = self.held_off_trunk("lane/leased-lane-mike", "leased")
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="leased-lane-mike", ref=tip)
        self.git("branch", "-D", "lane/leased-lane-mike")
        lr = landreq.get(row["id"])[0]
        # CONTROL, ON THE SAME OBSERVABLE THE ASSERTION BELOW READS: with no
        # lease this row IS in the census plan, so its later absence from that
        # same plan is the LEASE talking and not a census that stopped seeing
        # the row.
        self.assertEqual(landreq.off_frontier_reason(lr)["reason"],
                         landreq.OFF_FRONTIER_ABANDONED)
        self.assertIn(row["id"], self.planned(self.census()))
        # THE HOLDER NAME IS DELIBERATELY OUTSIDE THE `ghost-`/`dead-`
        # CONVENTION: those names are the fixture roll of seats that read
        # positively ABSENT, and this seat is the opposite — it is holding a
        # lane right now.
        ok, why, _lease = seats_claims.claim(
            "worktree:%s:leased-lane-mike" % os.path.basename(self.repo),
            "leasing-seat")
        self.assertTrue(ok, why)
        state, evidence = landreq.lane_lease_presence(lr)
        self.assertEqual(state, "present", evidence)
        verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.ON_FRONTIER)
        self.assertEqual(verdict["rung"], "lease")
        self.assertNotIn(row["id"], self.planned(self.census()))

    def test_a_detached_room_holding_the_tip_keeps_the_row_ON(self):
        """The room with NO ref at all. A detached worktree is a place work
        lives that no branch names, and the porcelain's HEAD field is the only
        identity it has — measured on the live board, eight rows' tips were
        held by exactly that and read as debris.

        CONTROL: the same row is retirable once the room is removed."""
        tip = self.held_off_trunk("lane/room-tip-november", "room-work")
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-november", ref=tip)
        room = os.path.join(self.tmp, "an-unrelated-room-name")
        self.git("worktree", "add", "--detach", "-q", room, tip)
        self.git("branch", "-D", "lane/room-tip-november")
        lr = landreq.get(row["id"])[0]
        # CONTROL 1 — the name probes miss this room: its path basename and
        # its (absent) branch are nothing like the row's lane.
        self.assertEqual(landreq.lane_branch_presence(lr)[0], "absent")
        self.assertEqual(landreq.lane_room_presence(lr)[0], "absent")
        verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.ON_FRONTIER)
        self.assertEqual(verdict["rung"], "live-hold")
        # CONTROL 2 — remove the room and the SAME row is placed.
        self.git("worktree", "remove", "--force", room)
        self.assertEqual(landreq.off_frontier_reason(lr)["reason"],
                         landreq.OFF_FRONTIER_ABANDONED)

    def test_an_unreadable_room_or_lease_reading_is_UNCLASSIFIED(self):
        """FAIL-CLOSED ON THE NEW RUNGS TOO. A registry helm could not read is
        `unknown`, which is UNCLASSIFIED, which `--apply` never touches — the
        same contract the ref table already had. The control is the SAME row
        read once through the real registries (placed) and once through each
        refusing one."""
        landed = self.commit("fail-closed-oscar")
        row = self.row_off_frontier("reaped-lane-oscar", landed)
        lr = landreq.get(row["id"])[0]
        self.assertIn(landreq.off_frontier_reason(lr)["reason"],
                      landreq.OFF_FRONTIER_REASONS)
        broken_rooms = (None, "git worktree list could not be run")
        with mock.patch.object(landreq, "_worktree_records",
                               return_value=broken_rooms):
            verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(verdict["rung"], "room")
        broken_claims = (None, "the claims ledger is unreadable: OSError")
        with mock.patch.object(landreq, "_cached_leases",
                               return_value=broken_claims):
            verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(verdict["rung"], "lease")
        broken_walk = (None, "Git could not walk what live refs hold off main")
        with mock.patch.object(landreq, "live_frontier_commits",
                               return_value=broken_walk):
            verdict = landreq.off_frontier_reason(lr)
        self.assertEqual(verdict["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(verdict["rung"], "live-hold")

    def test_a_claims_file_helm_CANNOT_READ_is_UNCLASSIFIED_not_ABSENT(self):  # noqa: VACUOUS_ASSERTION — the positive control runs UNCONDITIONALLY before the loop and on the same observables the loop reads: with a readable claims file `claims_unavailable()` is None, `lane_lease_presence` answers ABSENT, the row classifies into OFF_FRONTIER_REASONS and it IS in the census plan; each subTest then asserts EXACT values (state "unknown", reason UNCLASSIFIED, rung "lease") beside the absence
        """THE ARM ABOVE MOCKS THE ACCESSOR; THIS ONE DRIVES IT. The lease
        rung is the one the frontier calls decisive on its own, and it read a
        claims file it could not open as "no live lease names this lane" —
        an ABSENCE, which is what clears a row for retirement. The cause is
        one layer down: `claims_list` reads through `pk.read_json(path, {})`,
        whose lenient branch catches every exception and answers the DEFAULT,
        so no file-level failure ever reaches this rung's `except`.

        TWO REAL FAILURES, NOT A DOUBLE: a claims file at mode 000, and a
        truncated one. Both are shapes a host really produces, and both are
        what the STRICT reader — the same one the claims MOVER takes — exists
        to refuse.

        THE POSITIVE CONTROL IS THE SAME ROW THROUGH A READABLE LEDGER, read
        first and unconditionally: with a well-formed claims file the row IS
        placed and IS in the plan, so the two UNCLASSIFIED readings below are
        the unreadable file talking and not a rung that refuses everything."""
        from helm import seats_claims
        landed = self.commit("unreadable-claims-yankee")
        row = self.row_off_frontier("reaped-lane-yankee", landed)
        lr = landreq.get(row["id"])[0]
        good = self.claims_file('{"_fence": 1}')
        # THE CLEANUP TOLERATES A GONE FILE, because `doCleanups` runs AFTER
        # tearDown and tearDown removes this fixture's whole tmp tree — a
        # bare `addCleanup(os.chmod, ...)` turned the mode restore into a
        # FileNotFoundError that failed the arm from outside its own body.
        self.addCleanup(lambda p=good: os.path.exists(p)
                        and os.chmod(p, 0o600))
        # CONTROL — a READABLE ledger, through the real accessor.
        self.assertIsNone(seats_claims.claims_unavailable())
        self.assertEqual(landreq.lane_lease_presence(lr)[0], "absent")
        self.assertIn(landreq.off_frontier_reason(lr)["reason"],
                      landreq.OFF_FRONTIER_REASONS)
        self.assertIn(row["id"], self.planned(self.census()))
        for name, damage in (("mode 000", None),
                             ("truncated", '{"_fence": 1, "worktree:x:y"')):
            with self.subTest(claims=name):
                if damage is None:
                    os.chmod(good, 0)
                else:
                    os.chmod(good, 0o600)
                    self.claims_file(damage)
                # THE TREE'S OWN STRICT READER REFUSES IT — the premise this
                # rung now stands on, read rather than assumed.
                self.assertIsNotNone(seats_claims.claims_unavailable())
                state, why = landreq.lane_lease_presence(lr)
                self.assertEqual(state, "unknown", why)
                self.assertIn("unreadable", why)
                verdict = landreq.off_frontier_reason(lr)
                self.assertEqual(verdict["reason"],
                                 landreq.OFF_FRONTIER_UNCLASSIFIED)
                self.assertEqual(verdict["rung"], "lease")
                report = self.census(apply_it=True)
                self.assertNotIn(row["id"], self.planned(report))
                self.assertIn(row["id"],
                              {e["id"] for e in report["unclassified_rows"]})
                self.assertEqual(report["closed"], [])
                self.assertIs(landreq.get(row["id"])[0]["terminal"], False)

    # ------------------------------------------------------------------
    # THE HEADER AND THE VERB ARE ONE READING
    # ------------------------------------------------------------------
    def test_the_header_residue_is_the_count_the_verb_will_act_on(self):
        """The strip's OFF-FRONTIER number and the census's OFF number come
        from one classification, and the rows helm could NOT place are counted
        as work owed and disclosed by name.

        The first cut had the header ask a different, wider predicate: it
        printed 541 `not work owed` against a verb that reported 286 placed
        and 255 unplaceable, so 255 rows were advertised to the owner as
        debris that no verb would ever clear. Three distinct populations are
        built here so a header that counted any of them wrongly cannot pass."""
        live = self.commit("frontier-papa")
        self.git("branch", "lane/still-cut-papa", live)
        self.verdicted(polarity="fix", author="ghost-author",
                       lane="still-cut-papa", ref=live)
        residue = self.row_off_frontier("reaped-lane-papa", live)
        doomed = self.held_off_trunk("lane/reaped-lane-quebec", "quebec")
        self.verdicted(polarity="fix", author="ghost-author",
                       lane="reaped-lane-quebec", ref=doomed)
        self.prune(doomed, "refs/heads/lane/reaped-lane-quebec")
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        # ONE READ ANSWERS BOTH. The header and the verb agreed by luck while
        # each opened its own projection: measured in one process on a copy of
        # the live ledger, `filed_split` read 304 placed rows and the census
        # read 316 on the next line, because `open_bucket_rows` membership
        # moves with the wall-clock derive budget each projection gets. The
        # census takes the caller's projection, so the two numbers here are one
        # computation over one population rather than two that happen to match.
        filed = landreq.filed_split(lrs, raw)
        report = self.census(lrs=lrs, raw=raw)
        # THE THREE POPULATIONS ARE REALLY THERE — unconditional positives,
        # so the equalities below cannot pass over an empty board.
        self.assertGreaterEqual(report["on_frontier"], 1)
        self.assertGreaterEqual(report["off_frontier"], 1)
        self.assertGreaterEqual(report["unclassified"], 1)
        self.assertEqual(filed["off_frontier"], report["off_frontier"])
        self.assertEqual(filed["off_frontier"], len(report["plan"]))
        self.assertEqual(filed["closable"], report["closable"])
        self.assertEqual(filed["not_closable"], report["not_closable"])
        self.assertEqual(filed["closable"] + filed["not_closable"],
                         filed["off_frontier"])
        self.assertEqual(filed["unclassified"], report["unclassified"])
        self.assertEqual(filed["open_frontier"],
                         report["on_frontier"] + report["unclassified"])
        self.assertEqual(filed["open"],
                         filed["open_frontier"] + filed["off_frontier"])
        line = landreq.filed_line(filed)
        self.assertIn("incl. %d unclassified" % filed["unclassified"], line)
        # AND THE LINE NEVER PROMISES THE WHOLE RESIDUE AGAIN. The old
        # sentence named the placed count as the count `--apply` acts on; the
        # authorizing gate refuses 182 of 316 such rows on the live board.
        self.assertNotIn("--apply acts on these %d" % filed["off_frontier"],
                         line)
        self.assertIn("%d OFF-FRONTIER" % filed["off_frontier"], line)
        # AND THE CENSUS REALLY READ THE PROJECTION IT WAS HANDED. Equality
        # over one deterministic fixture ledger would also hold if the census
        # quietly opened its own read, which is the defect: on the live board
        # two reads of one copy differ by whatever the derive budget finished.
        # Hand it a projection with this row REMOVED and the plan must lose
        # exactly that row, while the bare call — same process, same ledger —
        # still plans it.
        narrowed = {k: v for k, v in lrs.items() if k != residue["id"]}
        self.assertEqual(len(narrowed), len(lrs) - 1)
        self.assertIn(residue["id"], self.planned(self.census()))
        self.assertNotIn(residue["id"],
                         self.planned(self.census(lrs=narrowed, raw=raw)))

    def test_the_header_splits_PLACED_from_CLOSABLE_and_apply_agrees(self):
        """CURE 2, END TO END AND ON ONE READ. A row the classification places
        is not a row the close ladder will take: `carried` needs an
        affirmative carriage witness, and an EXACT-ANCESTOR tip leaves `git
        cherry`'s range empty — an empty range affirms every possible trunk,
        so the witness fails closed. MEASURED over the whole placed population
        of a copy of the live ledger: 316 placed, 122 admitted by `carried`,
        12 by `withdrawn`, 182 refused. The strip printed 316 as closable.

        THE TWO ROWS HERE DIFFER ONLY IN HOW THE WORK REACHED TRUNK — one
        cherry-picked (patch identity, witness AFFIRMS), one landed by
        ancestry (witness UNASKABLE) — so a split stuck at all-closable and a
        split stuck at none-closable both fail.

        AND `--apply` CLOSES EXACTLY THE CLOSABLE SET, which is the assertion
        that keeps the preflight bound to the door: if the gate this walk
        asked were weaker or stronger than the ladder the act runs, the two
        sets would differ here."""
        takeable = self.cherry_picked("lane/reaped-lane-alfa2", "alfa2")
        admits = self.verdicted(polarity="fix", author="ghost-author",
                                lane="reaped-lane-alfa2", ref=takeable)
        refuses = self.row_off_frontier("reaped-lane-bravo2",
                                        self.commit("ancestry-bravo2"))
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        filed = landreq.filed_split(lrs, raw)
        report = self.census(lrs=lrs, raw=raw)
        plan = self.planned(report)
        # BOTH ROWS ARE PLACED — the classification puts them in one bucket.
        self.assertIn(admits["id"], plan)
        self.assertIn(refuses["id"], plan)
        self.assertEqual(plan[admits["id"]]["close_reason"], "carried")
        self.assertEqual(plan[refuses["id"]]["close_reason"], "carried")
        # AND THE DOOR SEPARATES THEM.
        self.assertIs(plan[admits["id"]]["closable"], True)
        self.assertIs(plan[refuses["id"]]["closable"], False)
        self.assertEqual(filed["closable"], report["closable"])
        self.assertEqual({e["id"] for e in report["plan"] if e["closable"]},
                         {admits["id"]})
        self.assertGreaterEqual(filed["closable"], 1)
        self.assertGreaterEqual(filed["not_closable"], 1)
        refusal = {e["id"]: e for e in report["not_closable_rows"]}
        self.assertIn(refuses["id"], refusal)
        self.assertIn("carriage could not be MEASURED",
                      refusal[refuses["id"]]["refusal"])
        # THE OWNER'S LINE CARRIES BOTH TOTALS, never one word for two.
        line = landreq.filed_line(filed)
        self.assertIn("%d closable now" % filed["closable"], line)
        self.assertIn("%d placed but NOT closable yet" % filed["not_closable"],
                      line)
        # AND THE ACT MATCHES THE PROMISE, on the same rows.
        applied = self.census(apply_it=True)
        self.assertEqual({e["id"] for e in applied["closed"]},
                         {e["id"] for e in applied["plan"] if e["closable"]})
        self.assertIn(admits["id"], {e["id"] for e in applied["closed"]})
        self.assertIn(refuses["id"], {e["id"] for e in applied["refused"]})
        self.assertIs(landreq.get(admits["id"])[0]["terminal"], True)
        self.assertIs(landreq.get(refuses["id"])[0]["terminal"], False)

    def test_a_body_with_no_split_makes_NO_clearance_claim(self):
        """THE VERSION-SKEW HALF, which is the same law the rest of this strip
        follows: a WARM body minted by a server older than this split carries
        the residue and no `closable` key, and inventing a zero there would
        tell the owner nothing can be cleared on a board nobody measured. The
        line names the census verb instead, which is true whatever the split
        turns out to be.

        CONTROL: the same dict WITH the split prints both numbers."""
        old = {"total": 9, "open": 4, "open_frontier": 1, "off_frontier": 3,
               "held": 1, "underived": 1, "landed": 1, "closed": 1,
               "non_loop": 1, "unclassified": 0}
        line = landreq.off_frontier_line(old)
        self.assertIn("3 OFF-FRONTIER", line)
        self.assertIn("censuses them", line)
        self.assertNotIn("closable now", line)
        new = dict(old, closable=2, not_closable=1)
        split = landreq.off_frontier_line(new)
        self.assertIn("2 closable now", split)
        self.assertIn("1 placed but NOT closable yet", split)

    # ------------------------------------------------------------------
    # --apply WRITES, and the arms measure the write
    # ------------------------------------------------------------------
    def cherry_picked(self, branch, path):
        """A reviewed tip whose PATCH is on trunk under a different sha —
        the rebased/cherry-picked land, which is what most of this population
        looks like."""
        tip = self.held_off_trunk(branch, path)
        # TRUNK MOVES FIRST, AND THAT IS NOT DECORATION: `git cherry-pick`
        # FAST-FORWARDS when HEAD is the commit's own parent, so without this
        # the "cherry-picked" tip IS trunk's tip and the fixture would be
        # measuring the ancestry world under a patch-identity name (measured —
        # this arm read `landed-by-ancestry` until trunk diverged).
        self.commit("trunk-moved-before-" + path)
        self.git("cherry-pick", tip)
        self.git("branch", "-D", branch)
        return tip

    def test_apply_closes_a_contrary_whose_patch_reached_trunk(self):
        """THE WRITE PATH, EXECUTED. A FIX-verdicted row whose lane was reaped
        after its patch reached trunk is what `carried` exists for, and this
        arm runs `--apply` and reads the ledger afterwards.

        POSITIVE CONTROL ON THE SAME READ: the dry census listed this row with
        this close reason before the apply run closed it, so the close cannot
        be a row the census never saw."""
        tip = self.cherry_picked("lane/reaped-lane-romeo", "romeo")
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-romeo", ref=tip)
        verdict = landreq.off_frontier_reason(landreq.get(row["id"])[0])
        self.assertEqual(verdict["reason"],
                         landreq.OFF_FRONTIER_LANDED_PATCH_ID)
        dry = self.planned(self.census())
        self.assertIn(row["id"], dry)
        self.assertEqual(dry[row["id"]]["close_reason"], "carried")
        before = self.ledger_state(row["id"])
        report = self.census(apply_it=True)
        closed = {e["id"]: e for e in report["closed"]}
        self.assertIn(row["id"], closed, report["refused"])
        self.assertEqual(closed[row["id"]]["close_reason"], "carried")
        after = landreq.get(row["id"])[0]
        self.assertEqual(after["close_reason"], "carried")
        self.assertEqual(after["terminal"], True)
        self.assertNotEqual(self.ledger_state(row["id"]), before)

    def test_a_second_apply_after_a_real_close_writes_nothing(self):
        """IDEMPOTENCE OVER A ROW THAT ACTUALLY CLOSED — the half the first
        cut could not test, because nothing it routed ever closed. A closed
        row is terminal, so it leaves `open_bucket_rows` and the second census
        cannot see it; the ledger after the second run is what the first left.

        THE CONTROL IS ON THE SECOND REPORT ITSELF: a companion row that no
        door admits is still classified and still routed by the second run, so
        `closed == []` is a measurement and not the silence of a census that
        stopped looking. The first run's effect is asserted too — the row is
        CLOSED before the second run is asked to change nothing."""
        tip = self.cherry_picked("lane/reaped-lane-sierra", "sierra")
        row = self.verdicted(polarity="fix", author="ghost-author",
                             lane="reaped-lane-sierra", ref=tip)
        witness = self.row_off_frontier("reaped-lane-uniform",
                                        self.commit("uniform-on-trunk"))
        first = self.census(apply_it=True)
        self.assertIn(row["id"], {e["id"] for e in first["closed"]},
                      first["refused"])
        after_first = self.ledger_state(row["id"])
        self.assertEqual(landreq.get(row["id"])[0]["terminal"], True)
        second = self.census(apply_it=True)
        # POSITIVE CONTROL, UNCONDITIONAL, ON THE SECOND REPORT: the companion
        # row is still seen, still placed, still routed.
        self.assertIn(witness["id"], self.planned(second))
        self.assertNotIn(row["id"], self.planned(second))
        self.assertEqual(len(second["closed"]), 0)
        self.assertEqual(self.ledger_state(row["id"]), after_first)

    def test_a_NON_contrary_landing_never_reaches_this_verb_at_all(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls on the same observables come first: the row IS classified `landed-by-ancestry` by the predicate and the census DID run and place the contrary row beside it, so the approve row's absence from the plan is a measured non-effect
        """WHY THE `landed` COLUMN CARRIES NO POPULATION, measured rather than
        assumed — and the arm exists because the review's third finding was a
        route that could not open and that no arm ever ran.

        A NON-CONTRARY row whose reviewed tip reached trunk is TERMINAL before
        this verb sees it: the projection observes the landing and reads the
        row LANDED, so it leaves `open_bucket_rows` and never enters the
        residue at all. The route entry stays because it is the right door for
        that measurement if such a row ever does reach here; what must not
        stand is a silent belief that it is exercised."""
        landed = self.commit("approved-and-landed")
        row = self.verdicted(polarity="approve", author="ghost-author",
                             lane="reaped-lane-tango", ref=landed)
        contrary = self.row_off_frontier("reaped-lane-tango-peer", landed)
        lr = landreq.get(row["id"])[0]
        # THE PREDICATE DOES PLACE IT — the row is off the frontier and its
        # measurement is a landing. It is the BUCKET that excludes it.
        self.assertEqual(landreq.lane_branch_presence(lr)[0], "absent")
        self.assertEqual(landreq.off_frontier_reason(lr)["reason"],
                         landreq.OFF_FRONTIER_LANDED_ANCESTRY)
        self.assertEqual(landreq.off_frontier_route(
            landreq.OFF_FRONTIER_LANDED_ANCESTRY, "approve"), "landed")
        self.assertEqual(lr["state"], "LANDED")
        self.assertIs(lr["terminal"], True)
        lrs, _raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        self.assertNotIn(row["id"],
                         {r["id"] for r in landreq.open_bucket_rows(lrs)})
        # AND THE CENSUS REALLY RAN: the contrary peer on the same commit is
        # placed, so the approve row's absence is a measurement.
        report = self.census()
        self.assertIn(contrary["id"], self.planned(report))
        self.assertNotIn(row["id"], self.planned(report))

    def test_apply_closes_an_approve_row_whose_work_is_absent(self):
        """THE RESOLUTION COLUMN, END TO END, on the measurement that CAN
        reach this verb. An APPROVE-verdicted row whose work never landed and
        whose lane was reaped is not terminal — nothing observed a landing —
        so it sits in the open bucket as debris, and `withdrawn` is its door:
        Git proves the reviewed change off trunk by ancestry and absent by
        patch id, which is the same measurement the predicate made.

        POSITIVE CONTROL ON THE SAME READ: the dry census routed this row
        before the apply run closed it."""
        tip = self.held_off_trunk("lane/reaped-lane-victor", "victor")
        row = self.verdicted(polarity="approve", author="ghost-author",
                             lane="reaped-lane-victor", ref=tip)
        self.git("branch", "-D", "lane/reaped-lane-victor")
        lr = landreq.get(row["id"])[0]
        self.assertIs(lr["terminal"], False)
        dry = self.planned(self.census())
        self.assertIn(row["id"], dry,
                      "state=%s terminal=%s reason=%s"
                      % (lr["state"], lr["terminal"],
                         landreq.off_frontier_reason(lr)["reason"]))
        self.assertEqual(dry[row["id"]]["polarity"], "approve")
        self.assertEqual(dry[row["id"]]["close_reason"], "withdrawn")
        report = self.census(apply_it=True)
        closed = {e["id"]: e for e in report["closed"]}
        self.assertIn(row["id"], closed, report["refused"])
        after = landreq.get(row["id"])[0]
        self.assertEqual(after["close_reason"], "withdrawn")
        self.assertIs(after["terminal"], True)

    def test_an_underivable_polarity_is_UNCLASSIFIED_on_BOTH_surfaces(self):
        """THE RUNG THAT REFUSES AFTER THE ROW IS PLACED. A row can be
        measurably off the frontier and still have no derivable polarity — a
        chain that disagrees with itself, an unreadable translation sidecar —
        and an unreadable chain is not a non-contrary one, so the row cannot
        be routed and must not be counted as clearable residue.

        THE PARITY IS THE POINT: the header and the verb must put such a row
        in the SAME bucket. They would not have, had the route stayed a second
        pass that only the verb ran — the two would have disagreed again one
        rung below where they last did.

        CONTROL, unconditional and first: with the chain readable, this row IS
        in the plan and IS counted as residue by the header."""
        landed = self.commit("chain-polarity-whiskey")
        row = self.row_off_frontier("reaped-lane-whiskey", landed)
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        filed = landreq.filed_split(lrs, raw)
        report = self.census()
        self.assertIn(row["id"], self.planned(report))
        self.assertGreaterEqual(filed["off_frontier"], 1)
        self.assertEqual(filed["off_frontier"], report["off_frontier"])
        blind = (None, None, "the chain disagrees with itself about polarity")
        with mock.patch.object(landreq, "_chain_polarity", return_value=blind):
            lrs, raw, _u = landreq.project_raw()
            blind_filed = landreq.filed_split(lrs, raw)
            blind_report = self.census(apply_it=True)
        self.assertNotIn(row["id"], self.planned(blind_report))
        self.assertIn(row["id"],
                      {e["id"] for e in blind_report["unclassified_rows"]})
        self.assertEqual(blind_filed["off_frontier"],
                         blind_report["off_frontier"])
        self.assertEqual(blind_filed["unclassified"],
                         blind_report["unclassified"])
        self.assertEqual(blind_report["closed"], [])
        self.assertIs(landreq.get(row["id"])[0]["terminal"], False)

    def test_a_row_this_board_withholds_is_never_counted_as_residue(self):
        """THE POPULATIONS MUST BE THE SAME ONE. The verb withholds other
        projects' rows by default (task/974), so a foreign row counted as
        residue would put the header back over the verb by exactly the rows
        the verb declines to touch — measured on a copy of the live ledger,
        283 residue against the verb's 275.

        A withheld row counts as FRONTIER, which is the safe direction, and
        the withheld line beside the strip is where it is disclosed.

        CONTROL, unconditional and first: while the board owns the row, it IS
        residue and both surfaces say the same number."""
        landed = self.commit("withheld-xray")
        row = self.row_off_frontier("reaped-lane-xray", landed)
        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        mine = landreq.filed_split(lrs, raw)
        owned = self.census()
        # THE CONTROL IS ON THE SAME OBSERVABLE as the absence below: while
        # the board owns this row it IS in the plan, so its later absence from
        # that same plan is the withholding and not a census that stopped
        # seeing it.
        self.assertIn(row["id"], self.planned(owned))
        self.assertEqual(mine["off_frontier"], owned["off_frontier"])
        self.assertGreaterEqual(mine["off_frontier"], 1)
        foreign = lambda lr: lr["id"] != row["id"]                # noqa: E731
        with mock.patch.object(landreq, "_this_boards_row",
                               side_effect=foreign):
            lrs, raw, _u = landreq.project_raw()
            filed = landreq.filed_split(lrs, raw)
            report = self.census()
        self.assertEqual(filed["off_frontier"], report["off_frontier"])
        self.assertEqual(filed["off_frontier"], mine["off_frontier"] - 1)
        self.assertNotIn(row["id"], self.planned(report))
        self.assertEqual(filed["open"],
                         filed["open_frontier"] + filed["off_frontier"])
