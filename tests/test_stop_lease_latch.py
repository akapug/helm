#!/usr/bin/env python3
"""The lease arm latches per held-set state instead of re-sermoning forever.

OWNER-SURFACED 2026-08-04: the stop-guard's LEASE arm re-printed its full
multi-line warning on EVERY stop while the held set was unchanged — the owner
watched the identical wall twice in a row and asked "is that perfect or does
it need adjusting?". Every sibling rung already latched per state (inbox on
the pending fingerprint, beacon on armed|missing, spiral on chain|rounds);
this arm was the one publisher without a memory.

The contract these tests pin, arm by arm:
  * the FIRST stop on a held set prints the full sermon and BLOCKS;
  * a re-stop on the SAME set (same resources, same lease ids, same TTL
    band) compresses to ONE warn line and the stop passes — dropping the
    latch turns this test red;
  * ANY change to the set — a new lease, a release, a re-claim (new lease
    id under the old resource), or a remainder crossing LEASE_TTL_ALARM_S —
    re-prints the full block: the latch compresses repetition, never
    severity escalation;
  * an UNWRITABLE latch degrades the sermon to a WARN, never an
    unconditional re-block (the refusal-honesty law: a gate that cannot
    remember must never become a wall);
  * no state word is spoken without a measurement: the arm never claims a
    live delegate it did not prove (#112's computable half was probed
    2026-08-04 and no reliable in-guard signal exists — the transcript-dir
    subagent records carry no liveness marker and record the parent's cwd,
    never the lane room);
  * EVERY LEASE THE SESSION HOLDS APPEARS, in one of the two promised shapes
    and never as an absence (task/2388, the sixth stop-guard rung hole after
    1069 1098 1418 1874 2290). The class behind all six is rung eligibility
    predicates composed without a coverage matrix: each exemption here was
    added on its own measured case and written as `warns.append(...); continue`,
    which is right in isolation and wrong in composition, because a stop with
    any held lane publishes the sermon as a BLOCK and discards the warn channel
    whole. Until a matrix enumerates (every exemption) x (every exit), the next
    rung reopens the hole; what is enforced here meanwhile is ONE door out of
    the enumeration loop that cannot emit without printing.
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import pk, seats, seats_stop_budget, seats_stop_timing  # noqa: E402
from helm import seats_stop_guard as stop_guard_impl  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_PRIVATE_NEEDLES",
            "HELM_SCRATCH_GC", "HELM_STOP_TIMING_AFTER")

ME = "latch-seat"
SESSION = "aaaa1111-2222-3333-4444-555566667777"

FULL = "act per line, then stop:"                     # the sermon's key
COMPRESSED = "unchanged. Reprint:"                    # the one-liner's key

# THE WHOLE BLOCK A BLOCKED STOP PRINTS, IN CHARACTERS. The owner reads this in
# his terminal every time a seat is held, and it reached ~900 characters of
# header and nested parentheticals in front of one lane. A number here is what
# stops that creeping back: prose has no natural bound, and every sentence
# added to this block was individually defensible.
SERMON_BUDGET = 600


FENCE = chr(96)   # the run-line quote character in docs/VERBS.md


class LeaseLatchBase(unittest.TestCase):
    """Hermetic tmp HELM_HOME/HELM_CHAT_DIR; non-lane resources on purpose so
    the delegation and gate-pending exemptions resolve to None fast and the
    latch is the only variable under test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-leaselatch-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CHAT_NAME"] = ME
        # THE TRACE IS BUFFERED IN PRODUCTION and speaks only once a ladder is
        # in danger (seats_stop_timing). These arms are ABOUT the trace's
        # content, so they ask for it: 0 streams from the first line. The
        # buffering itself is measured in tests/test_hook_wrapper.py, and
        # `reset` is what keeps one arm's threshold crossing out of the next
        # one — the state is process-wide and a suite is one process.
        os.environ["HELM_STOP_TIMING_AFTER"] = "0"
        seats_stop_timing.reset()

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        seats_stop_timing.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claim(self, resource, ttl=600):
        ok, msg, lease = seats.claim(resource, ME, ttl=ttl, session=SESSION)
        self.assertTrue(ok, msg)
        return lease

    def drop(self, resource):
        """Remove a row the way a release/expiry leaves the ledger."""
        c = pk.read_json(seats.claims_path(), {}) or {}
        del c[resource]
        pk.write_json(seats.claims_path(), c)

    def decay(self, resource, left):
        """Rewrite one row's remaining TTL — the crossing under test."""
        c = pk.read_json(seats.claims_path(), {}) or {}
        c[resource]["exp_mono"] = seats._now_mono() + left
        pk.write_json(seats.claims_path(), c)

    def guard(self):
        return seats.stop_guard(session=SESSION, room="main", seat=ME)

    def sermons(self, lines):
        return [x for x in lines if FULL in x]

    def compressed(self, lines):
        return [x for x in lines if COMPRESSED in x]


class RungTimingTest(LeaseLatchBase):
    def test_each_completed_rung_names_the_next_stage_before_it_runs(self):
        self.claim("db-migration")
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.guard()
        lines = [line for line in out.getvalue().splitlines()
                 if line.startswith("[helm stop-guard timing]")]
        self.assertTrue(lines[0].startswith(
            "[helm stop-guard timing] BEGIN identity total="), lines)
        # DERIVED FROM THE LADDER, NOT COPIED OUT OF IT. This tuple was
        # written out by hand, so reordering the rungs — which is half the
        # cure for one slow rung disabling the rest — broke an arm that has
        # no opinion about the order, only about every transition being
        # announced. A second copy of a fact cannot notice the first one
        # moved. `response` is published by the caller and ends no boundary
        # here, so the chain runs to `claim-evidence`.
        rungs = seats_stop_budget.RUNGS
        expected = tuple(zip(rungs[:-2], rungs[1:-1]))
        self.assertEqual(expected[0], ("identity", rungs[1]),
                         "control: the derived chain starts at the first rung")
        self.assertEqual(expected[-1][1], "claim-evidence",
                         "control: the derived chain ends at the last boundary")
        for name, next_name in expected:
            hits = [line for line in lines
                    if "DONE %s " % name in line
                    and " next=%s" % next_name in line]
            self.assertEqual(len(hits), 1, (name, next_name, lines))
            self.assertIn("elapsed=", hits[0])
            self.assertIn("total=", hits[0])
        self.assertEqual(sum("DONE claim-evidence " in line for line in lines),
                         1, lines)
        # THE LADDER NO LONGER FOLDS THE DISPATCH LEDGER: that span is gone,
        # not renamed — the `helm web` resident computes the stop facts.
        self.assertFalse(any("dispatch-ledger" in line for line in lines),
                         lines)

    def test_interrupted_rung_leaves_its_begin_line_as_the_last_stage(self):  # noqa: VACUOUS_ASSERTION — the final BEGIN line is the unconditional positive control; DONE must be absent because interruption occurred inside that exact span
        self.claim("db-migration")
        out = io.StringIO()
        with mock.patch.object(stop_guard_impl, "claims_rung",
                               side_effect=KeyboardInterrupt), \
                contextlib.redirect_stderr(out), \
                self.assertRaises(KeyboardInterrupt):
            self.guard()
        lines = [line for line in out.getvalue().splitlines()
                 if line.startswith("[helm stop-guard timing]")]
        self.assertIn("BEGIN claims ", lines[-1], lines)
        self.assertFalse(any("DONE claims " in line for line in lines),
                         lines)

    def test_continuing_stop_times_only_the_warn_path(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            seats.stop_guard(session=SESSION, room="main", seat=ME,
                             stop_active=True)
        lines = [line for line in out.getvalue().splitlines()
                 if line.startswith("[helm stop-guard timing]")]
        self.assertEqual(len(lines), 4, lines)
        self.assertIn("BEGIN identity ", lines[0])
        self.assertIn("DONE identity ", lines[1])
        self.assertIn(" next=continuing-inbox", lines[1])
        self.assertIn("BEGIN continuing-inbox ", lines[2])
        self.assertIn("DONE continuing-inbox ", lines[3])

    def test_disabled_guard_emits_no_timing(self):  # noqa: VACUOUS_ASSERTION — self.guard() is the unconditional exercised observable; exact empty stderr is the disabled contract
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HELM_STOP_GUARD": "0"}), \
                contextlib.redirect_stderr(out):
            self.guard()
        self.assertEqual(out.getvalue(), "")

    def test_an_expired_ladder_leaves_no_flush_behind_it(self):
        """THE EXIT THAT NEVER REACHES THE TERMINAL, and therefore never
        reaches `RungTiming.finish`.

        The streaming flush is scheduled when the ladder emits its first line
        and fires on wall time. An EXPIRED ladder publishes through its own
        `response-fallback` rung and holds no run identity, so nothing on that
        path can cancel it — and the buffer then lands on stderr after the
        verb has already returned, inside whatever runs next. The verb that
        ARMED the ladder (`State()` constructs the RungTiming) is the one door
        every exit passes through, so the belt lives there rather than as a
        cancel per exit, which is a list the next exit is left off.
        """
        from helm import projscope, seats_cli
        os.environ[seats_stop_timing.STREAM_AFTER_ENV] = "0.25"
        seats_stop_timing.reset()
        during, after = io.StringIO(), io.StringIO()
        with mock.patch.object(seats_cli, "stop_guard",
                               side_effect=projscope.Expired("forced")), \
                contextlib.redirect_stderr(during):
            rc = seats_cli.cmd("stop-guard", [], room="main")
        self.assertEqual(rc, 0, "an expired ladder must still fail open")
        # MUST-HIT: this arm really did drive the expiry path. An expired
        # ladder publishes its UNKNOWN line, so an empty channel here would
        # mean the mock never reached `publish_expired` and the silence below
        # would prove nothing.
        self.assertTrue(during.getvalue().strip(),
                        "the expired path published nothing")
        with contextlib.redirect_stderr(after):
            time.sleep(0.45)                    # past the threshold, twice
        self.assertEqual(after.getvalue(), "",
                         "the ladder's buffer arrived after the verb returned")


class TheFooterAndTheVerbAgreeTest(unittest.TestCase):
    """A BLOCK THAT NAMES A FLAG THE VERB'S OWN SYNOPSIS DENIES.

    The short block ends by telling its reader to run `helm chat stop-guard
    --detail`, and the verb's synopsis listed only `[--seat S] [--room R]` --
    so the next reader who checks before running learns the flag does not
    exist. A synopsis omission reads as ABSENCE, and this one was introduced
    by the same change that started advertising the flag.

    EVERY PUBLIC SYNOPSIS, NOT THE ONE I CURED. Two surfaces answer "what
    flags does this verb take": `chat.HELP["stop-guard"]`, which `helm chat
    stop-guard --help` prints, and docs/VERBS.md, the written reference.
    (`cli._VERB_HELP["chat"]` is NOT a site: measured, it does not name this
    subverb at all, so it makes no claim about its flags -- a surface that
    says nothing cannot say something false.)

    AND THE SYNOPSIS, NOT THE PAGE IT SITS ON. The first cut of this arm
    asked whether `--detail` appeared ANYWHERE in docs/VERBS.md, where the
    string occurs four times for other verbs: deleting the flag from this
    verb's own entry left the arm GREEN, measured against that mutant. A
    containment test over a half-megabyte document asserts almost nothing.
    Each site is therefore narrowed to the RUN LINE for this verb -- the
    quoted command in VERBS.md, and the `usage:` clause before the prose in
    the HELP entry -- which is the span a reader actually reads to learn
    what the verb accepts.

    DERIVED, NEVER TRANSCRIBED: the flags come out of the one string both
    renderings interpolate, so adding a second flag to that instruction is
    red here until every synopsis offers it too.
    """

    def _verbs_md_synopsis(self):
        """The quoted run line(s) for this verb in docs/VERBS.md."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with io.open(os.path.join(root, "docs", "VERBS.md"),
                     encoding="utf-8") as fh:
            doc = fh.read()
        pattern = "%s(helm chat stop-guard[^%s]*)%s" % (FENCE, FENCE, FENCE)
        return "\n".join(re.findall(pattern, doc))

    def _help_synopsis(self):
        """The `usage:` clause of the HELP entry, before the prose."""
        from helm import chat
        return chat.HELP["stop-guard"].split("(")[0]

    def test_every_flag_the_block_advertises_is_in_every_synopsis(self):
        from helm import seats_stop_claims
        verb = seats_stop_claims.DETAIL_VERB
        flags = set(re.findall(r"--[a-z][a-z0-9-]*", verb))
        # MUST-HIT: an instruction with no flag in it would satisfy every
        # assertion below by asserting nothing at all.
        self.assertTrue(flags, "the footer advertises no flag, so this arm "
                               "checks nothing: %r" % verb)
        # THE VERB HALF: the instruction must name THIS verb, or the
        # synopses it is checked against are the wrong ones.
        self.assertIn("helm chat stop-guard", verb)
        usage, runline = self._help_synopsis(), self._verbs_md_synopsis()
        # UNCONDITIONAL, ON EACH SITE BY NAME, BEFORE THE LOOP TOUCHES IT.
        # Both extractors can lie in two directions -- match nothing, or
        # swallow the document -- and the loop below can only ever assert
        # that about its own variable. These are the twins that make the
        # per-site checks inside the loop mean something.
        self.assertIn("helm chat stop-guard", usage)
        self.assertLess(len(usage), 2000)
        self.assertIn("helm chat stop-guard", runline)
        self.assertLess(len(runline), 2000)
        sites = {"chat.HELP[stop-guard] usage clause": usage,
                 "docs/VERBS.md run line": runline}
        # UNCONDITIONAL: losing a site is exactly the regression this arm
        # exists to catch, and a shrunken table would pass in silence.
        self.assertEqual(len(sites), 2,
                         "the synopsis table changed size, so this arm no "
                         "longer checks the population it was written for")
        for name, text in sites.items():
            # MUST-HIT PER SITE, IN BOTH DIRECTIONS. An extractor that
            # matched nothing would fail the flag checks for the wrong
            # reason; one that swallowed the whole document would pass them
            # for the wrong reason, which is the defect a mutant found in
            # this arm's first cut. Naming the verb catches the first, and
            # the width bound catches the second.
            self.assertIn("helm chat stop-guard", text,
                          "%s yielded no synopsis for this verb: %r"
                          % (name, text[:200]))
            self.assertLess(len(text), 2000,  # noqa: VACUOUS_ASSERTION -- a width bound is absence-shaped, and its subject is the LOOP VARIABLE, which can carry no unconditional twin however the arm is written; the twins are the two named extractions asserted above, on the same two values this loop is iterating.
                            "%s is too wide to be a synopsis -- a containment "
                            "test over it would assert almost nothing: %r"
                            % (name, text[:200]))
            for flag in sorted(flags):
                self.assertIn(flag, text,
                              "the block sends its reader to %r and %s does "
                              "not offer %s, so that surface reads as the "
                              "flag not existing: %r"
                              % (verb, name, flag, text[:200]))


class SameSetLatchTest(LeaseLatchBase):
    def test_the_first_stop_blocks_with_the_full_sermon(self):  # noqa: VACUOUS_ASSERTION — len(hit)==1 + the assertIns are the unconditional positive controls; the compressed-line absence is the finding
        self.claim("db-migration")
        blocks, warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertIn("helm chat release db-migration", hit[0])
        self.assertEqual(self.compressed(warns), [])

    def test_a_restop_on_the_same_set_compresses_to_one_warn_line(self):  # noqa: VACUOUS_ASSERTION — the compressed line (len==1 + content) is the unconditional positive control on the same stop's output; the sermon absence is the finding
        """THE LATCH ITSELF — drop it and this test is red on the re-block."""
        self.claim("db-migration")
        self.guard()
        blocks, warns = self.guard()
        self.assertEqual(self.sermons(blocks), [],
                         "the identical wall printed twice — the owner's "
                         "exact defect")
        line = self.compressed(warns)
        self.assertEqual(len(line), 1, warns)
        self.assertIn("1 lease(s) held", line[0])
        self.assertNotIn("\n", line[0])           # ONE line, not a wall
        self.assertNotIn("--lease", line[0])      # detail lives in the sermon
        self.assertIn("helm chat stop-guard --detail", line[0])

    def test_a_new_lease_reprints_the_full_block(self):
        self.claim("db-migration")
        self.guard()
        self.claim("cache-rebuild")
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertIn("cache-rebuild", hit[0])

    def test_a_release_reprints_the_full_block_for_the_remainder(self):
        self.claim("db-migration")
        self.claim("cache-rebuild")
        self.guard()
        self.drop("cache-rebuild")
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertNotIn("cache-rebuild", hit[0])

    def test_an_all_exempt_stop_rearms_the_full_sermon(self):  # noqa: VACUOUS_ASSERTION — the giver warn and the final len(sermons)==1 are the unconditional positive controls; the mid-test sermon absence is the exempt state itself
        """sermon (latched) -> the row turns exempt for one stop (the giver
        shape: holder flipped to a rostered peer) -> back to held. Without
        the exempt-stop clear, the return fingerprints identically to the
        latched sermon and a lapsed exemption — a delegate's death, in the
        delegation arm's version of this shape — would compress into the
        one-liner. The stop after an exemption lapses must be LOUD."""
        self.claim("db-migration")
        self.guard()                                   # sermon, latched
        c = pk.read_json(seats.claims_path(), {}) or {}
        c["db-migration"]["holder"] = "peer-seat"
        pk.write_json(seats.claims_path(), c)
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault("peer-seat", {})["session"] = (
            "99998888-7777-6666-5555-444433332222")
        pk.write_json(seats.roster_path(), r)
        blocks, warns = self.guard()                   # giver: warn, allowed
        self.assertEqual(self.sermons(blocks), [], blocks)
        self.assertTrue([w for w in warns if "not yours to release" in w],
                        warns)
        c = pk.read_json(seats.claims_path(), {}) or {}
        c["db-migration"]["holder"] = ME
        pk.write_json(seats.claims_path(), c)
        blocks, _warns = self.guard()
        self.assertEqual(len(self.sermons(blocks)), 1,
                         "the return to held after an exempt stop was "
                         "compressed — the lapse was silent")

    def test_a_reclaim_is_a_new_lease_id_and_reprints(self):  # noqa: VACUOUS_ASSERTION — len(sermons)==1 on the reclaim stop is the unconditional positive control
        """The fingerprint keys on the LEASE ID, not the resource name alone:
        release-then-reclaim of the same resource is a fresh obligation."""
        first = self.claim("db-migration")
        self.guard()
        self.drop("db-migration")
        second = self.claim("db-migration")
        self.assertNotEqual(first, second)
        blocks, _warns = self.guard()
        self.assertEqual(len(self.sermons(blocks)), 1, blocks)


class TTLEscalationTest(LeaseLatchBase):
    def test_crossing_the_alarm_escalates_through_the_latch(self):  # noqa: VACUOUS_ASSERTION — the post-crossing block (len==1 + EXPIRING) is the unconditional positive control; the latched quiet stop is the state under test
        """A latched set whose remainder crosses LEASE_TTL_ALARM_S is a
        CHANGED state: the full block re-fires and says EXPIRING. Severity is
        never compressed."""
        self.claim("db-migration", ttl=600)
        self.guard()
        blocks, _w = self.guard()
        self.assertEqual(self.sermons(blocks), [])       # latched, quiet
        self.decay("db-migration", seats.LEASE_TTL_ALARM_S - 30)
        blocks, _w = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("EXPIRING", hit[0])

    def test_a_lease_born_below_the_alarm_is_loud_at_first_sight(self):
        self.claim("db-migration", ttl=60)
        blocks, _w = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("EXPIRING", hit[0])

    def test_repetition_in_the_expiring_band_still_compresses_but_counts(self):  # noqa: VACUOUS_ASSERTION — the compressed line (len==1, '1 EXPIRING') is the unconditional positive control
        """After the crossing fired once, an unchanged expiring set is
        repetition again — but the one-liner carries the EXPIRING tally, so
        the state stays visible without the wall."""
        self.claim("db-migration", ttl=60)
        self.guard()
        blocks, warns = self.guard()
        self.assertEqual(self.sermons(blocks), [])
        line = self.compressed(warns)
        self.assertEqual(len(line), 1, warns)
        self.assertIn("1 EXPIRING", line[0])


class UnwritableLatchTest(LeaseLatchBase):
    def test_an_unwritable_latch_degrades_to_the_warn_and_never_walls(self):  # noqa: VACUOUS_ASSERTION — the loop body runs unconditionally twice and asserts len(sermons(warns))==1 each pass
        """The refusal-honesty law: a latch that cannot remember must degrade
        to warn semantics, never to an unconditional re-block — the exact bug
        the beacon/spiral rungs fixed ('no latch, no block')."""
        self.claim("db-migration")
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only")):
            for _ in range(2):        # every stop, not just the first
                blocks, warns = self.guard()
                self.assertEqual(self.sermons(blocks), [],
                                 "an unwritable latch became a wall")
                self.assertEqual(len(self.sermons(warns)), 1, warns)


class NoForgedDelegationSignalTest(LeaseLatchBase):
    def test_no_delegate_claim_is_ever_spoken_without_a_measurement(self):  # noqa: VACUOUS_ASSERTION — the two len==1 must-hits prove the scanned corpus is the armed guard speaking about this lease before the absences are read
        """#112's computable half, probed 2026-08-04: no reliable in-guard
        read of live subagents exists (no liveness marker in the transcript
        records, parent-cwd only, tasklist rows are declarations). So the arm
        must never render a HELD-FOR-LIVE-DELEGATE state it cannot measure —
        in either the sermon or the compressed line."""
        self.claim("db-migration")
        b1, w1 = self.guard()
        b2, w2 = self.guard()
        # MUST-HIT first (absence-of-a-match is not a finding): the corpus
        # under scan really is the armed guard talking about this lease.
        self.assertEqual(len(self.sermons(b1)), 1, b1)
        self.assertEqual(len(self.compressed(w2)), 1, w2)
        everything = "\n".join(b1 + w1 + b2 + w2)
        self.assertNotIn("HELD FOR LIVE DELEGATE", everything)
        self.assertNotIn("live delegated build", everything)


class EveryHeldLeaseAppearsInOneOfTheTwoShapesTest(LeaseLatchBase):
    """task/2388 — AN OMITTED LEASE READS AS RELEASED, and the guard omitted one.

    MEASURED on a seat holding three leases: the sermon named two, and the third
    — the lane holding nine dirty files — was ABSENT from it, while `helm work
    list` in the same minute showed that lane GUARDED, dirty and locked under a
    live lease. The header promises exactly two shapes per lane (a measurable
    lane names its discharge command, an unprovable one says why and offers
    none); absence is a third, and it is the one reading that makes a seat walk
    away from uncommitted work.

    THE CAUSE IS NOT THE ENUMERATION, which is the claims registry itself. It is
    an exemption written as `warns.append(...); continue`: on a stop where ANY
    lane is held, the publisher emits the sermon as a BLOCK and discards the
    warn channel whole, so an exempted lane's only line is thrown away. Driven
    against those three rows before the cure, the exempted lane appeared in
    `warns` and nowhere in `blocks`.

    The task row's own first candidates — a filter keyed on dispatch rows or on
    branch names — were REFUTED by that drive: the three claim rows were
    identical in holder, session and shape, and the discriminator was which lane
    happened to have a live in-room process when the stop ran. So these arms
    plant the exemption shape that was MEASURED to exempt (the renewing-lock
    branch and the giver branch, both driven through the shipped predicates with
    real registry bytes) rather than the inferred one.
    """

    RENEWING = "gatelock:helm"      # the one renewed claim shape on the box

    def test_an_exempt_lease_is_a_line_in_the_sermon_beside_a_held_one(self):
        """THE MUST-HIT. Two leases, one held and one exempt, one stop: before
        the cure the sermon carried exactly one of them."""
        self.claim("db-migration")
        self.claim(self.RENEWING)            # renews above the floor -> exempt
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        # POSITIVE CONTROLS on the same output: the sermon fired at all, and the
        # HELD lane still renders in shape one with its exact command.
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertIn("helm chat release db-migration", hit[0])
        # THE FINDING: the exempt lane is in the printed set, in shape two.
        self.assertIn(self.RENEWING, hit[0],
                      "an exempted lease was omitted from the sermon — the "
                      "omission a seat reads as released")
        self.assertIn("NO ACTION OWED", hit[0])
        self.assertIn("LIVE RENEWER", hit[0])     # its measured state word
        # ...and shape two offers NO command, which is the half of the promise
        # that keeps a seat from revoking a live renewer's lock.
        self.assertNotIn("release %s" % self.RENEWING, hit[0])
        # AND NO REASSURANCE PROSE IN THE RED EMISSION, which is the
        # owner-surfaced half: the accounting line carries a state word, never
        # "stop allowed, lease retained" glowing red as an error.
        self.assertNotIn("lease retained", hit[0])
        # the full sentence still reaches the seat on an allowed stop
        self.assertIn("RENEWED since this stop began",
                      "\n".join(_warns))

    def test_the_giver_shape_is_a_line_too_when_another_lane_is_held(self):
        """The second exemption branch, planted the way the all-exempt arm
        above plants it (holder flipped to a rostered peer with a live session
        of its own) — but with a HELD lane beside it, which is the state that
        turns the warn channel into a discard."""
        self.claim("db-migration")
        self.claim("cache-rebuild")
        c = pk.read_json(seats.claims_path(), {}) or {}
        c["cache-rebuild"]["holder"] = "peer-seat"
        pk.write_json(seats.claims_path(), c)
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault("peer-seat", {})["session"] = (
            "99998888-7777-6666-5555-444433332222")
        pk.write_json(seats.roster_path(), r)
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])             # positive control
        self.assertIn("cache-rebuild", hit[0],
                      "the giver lane was omitted from the sermon")
        self.assertIn("PEER HOLD", hit[0])
        self.assertIn("NO ACTION OWED", hit[0])
        self.assertNotIn("not yours to release", hit[0])   # prose, not red
        self.assertIn("not yours to release", "\n".join(_warns))

    def test_a_released_exempt_lease_is_named_by_neither_shape(self):  # noqa: VACUOUS_ASSERTION — len(sermons)==1 plus the held lane's own line are the unconditional positive controls on this stop's blocks; the released row's absence is the finding
        """THE MUST-STAY-QUIET. The cure prints every lease the session HOLDS,
        which is not the same as printing every lease it ever held: a released
        row must vanish from both shapes. It also pins the exemption as a
        fingerprint coordinate — the set CHANGED, so this is a fresh sermon and
        not the compressed one-liner."""
        self.claim("db-migration")
        self.claim(self.RENEWING)
        self.guard()
        self.drop(self.RENEWING)
        blocks, warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)             # positive control
        self.assertIn("db-migration", hit[0])
        self.assertEqual(self.compressed(warns), [],
                         "a released exemption compressed — the change was "
                         "invisible to the latch")
        self.assertNotIn(self.RENEWING, "\n".join(blocks + warns),
                         "a released lease was still named")

    def test_the_compressed_line_counts_the_exempt_lanes_too(self):
        """A count that excludes them is the same omission one surface smaller:
        a seat comparing '1 lease(s) held' against two rows in `helm work list`
        reads the missing one as released."""
        self.claim("db-migration")
        self.claim(self.RENEWING)
        self.guard()
        _blocks, warns = self.guard()
        line = self.compressed(warns)
        self.assertEqual(len(line), 1, warns)             # positive control
        self.assertIn("1 lease(s) held", line[0])
        self.assertIn("+1 exempt", line[0])


class TheExemptionIsAnAnnotationNotAnExclusionTest(unittest.TestCase):
    """THE MUTATION-STYLE ARM: the old enumeration path cannot come back.

    The defect was not a value, it was a SHAPE — five exemption branches, each
    written as `warns.append(...); continue`, each locally correct and jointly
    able to drop a lane from the only surface a stopping seat reads. A runtime
    arm proves the current branches print; this proves no SIXTH branch can be
    added in the old shape, because there is exactly one door out of the
    enumeration loop for an exempted lease and that door cannot emit a warn
    without also queueing the printed line and the latch coordinate.

    Read off the AST rather than the text, so reformatting cannot pass it.
    """

    def _loop_and_tree(self):
        import ast
        import inspect
        from helm import seats_stop_claims
        tree = ast.parse(inspect.getsource(seats_stop_claims.claims_rung))
        loops = [n for n in ast.walk(tree)
                 if isinstance(n, ast.For)
                 and isinstance(n.iter, ast.Call)
                 and isinstance(n.iter.func, ast.Name)
                 and n.iter.func.id == "sorted"]
        # MUST-HIT: the scan really found the enumeration loop. Absence of a
        # match is not a finding, and every assertion below is an absence.
        self.assertEqual(len(loops), 1,
                         "the claims enumeration loop was not located")
        return loops[0], tree

    def test_no_exemption_emits_a_warn_inside_the_enumeration_loop(self):
        import ast
        loop, _tree = self._loop_and_tree()
        calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)]
        doors = [n for n in calls
                 if isinstance(n.func, ast.Name) and n.func.id == "_exempt"]
        # MUST-HIT: the exemptions still exist and still go through the door.
        self.assertGreaterEqual(len(doors), 4, "the exempt door lost callers")
        warn_appends = [n for n in calls
                        if isinstance(n.func, ast.Attribute)
                        and n.func.attr == "append"
                        and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == "warns"]
        self.assertEqual(warn_appends, [],
                         "an exemption emits a warn inline again — that is the "
                         "2388 shape: on a stop with any held lane the warn "
                         "channel is discarded and the lane reads as released")

    def test_the_exempt_door_cannot_speak_without_printing(self):
        import ast
        _loop, tree = self._loop_and_tree()
        doors = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_exempt"]
        self.assertEqual(len(doors), 1, "the exempt door is not a function")
        targets = {n.func.value.id for n in ast.walk(doors[0])
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "append"
                   and isinstance(n.func.value, ast.Name)}
        self.assertEqual(targets, {"warns", "exempt", "lease_fps"},
                         "the door no longer does all three in one call: the "
                         "warn, the printed line, and the latch coordinate")


class TheClaimsRungCallSiteBindsEveryFreeNameTest(unittest.TestCase):
    """The rung moved out of `_stop_guard`; its six free names became six
    arguments, and an argument is droppable where a closure variable was not.

    WHY THIS NEEDS AN ARM AND THE EXTRACTION ITSELF DID NOT. The moved body is
    byte-identical to the closure it replaced, so nothing in the rung can drift
    on its own. The CALL SITE is the new surface: as a closure, `blocks`,
    `warns` and the stop's one reading of the facts were simply in scope and
    could not be forgotten. Now they are keywords with defaults, and a caller
    that drops one fails SILENTLY — a dropped `facts` makes the rung take a
    second, unsettled reading of the stop facts, so two rungs of one stop can
    judge two different readings while every test of the rung's own logic
    stays green.

    Read off the AST rather than the source text, so reformatting the call
    cannot pass while dropping an argument.
    """

    def test_every_parameter_of_claims_rung_is_bound_at_the_call_site(self):
        import ast
        import inspect
        from helm import seats_stop_claims, seats_stop_guard

        src = inspect.getsource(seats_stop_guard)
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "claims_rung"]
        # POSITIVE CONTROL on the same observable: the call EXISTS. An
        # extraction that deleted the rung entirely would otherwise satisfy
        # every "all parameters bound" check by having nothing to bind.
        self.assertEqual(len(calls), 1, "expected exactly one claims_rung call")
        call = calls[0]

        bound = {kw.arg for kw in call.keywords if kw.arg}
        bound |= set(list(inspect.signature(
            seats_stop_claims.claims_rung).parameters)[:len(call.args)])
        expected = set(inspect.signature(
            seats_stop_claims.claims_rung).parameters)
        self.assertEqual(bound, expected,
                         "unbound at the call site: %s"
                         % sorted(expected - bound))
        # and the accumulators are the CALLER'S, not fresh lists: the rung
        # appends, so a copy would silently discard every block it raises.
        for kw in call.keywords:
            if kw.arg in ("blocks", "warns"):
                self.assertIsInstance(kw.value, ast.Name)
                self.assertEqual(kw.value.id, kw.arg)


class TheBlockedStopFitsInATerminalTest(LeaseLatchBase):
    """THE OWNER'S COMPLAINT AS A NUMBER, twice asked: "why is this stophook so
    fuggin long? are our stophooks TRULY optimized for concision and usefulness
    each time they run?"

    He reads this block in his terminal every time a seat's stop is held, and
    the agent reads the same text as its instruction. Both audiences need two
    things — what is held, and the one next act — and neither needs a
    four-clause parenthetical about which of four reads failed. That detail is
    not deleted: `helm chat stop-guard --detail` prints it, and this arm pins
    BOTH halves, because a budget met by throwing the diagnostic away is a
    different change from a budget met by moving it.

    A NUMBER RATHER THAN A STYLE RULE. Prose has no natural bound and every
    sentence that grew this block was individually defensible; only a measured
    ceiling makes the next one arrive as a red arm instead of as another line
    the owner scrolls past.
    """

    LANE = "worktree:proj:a-lane-whose-room-is-not-on-this-box"
    TTL = 14299        # the remainder from the owner's own paste

    def test_one_unreadable_leased_lane_fits_the_budget(self):
        self.claim(self.LANE, ttl=self.TTL)
        blocks, warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, (blocks, warns))   # positive control
        self.assertLessEqual(
            len(hit[0]), SERMON_BUDGET,
            "the blocked stop's message is over budget again:\n%s" % hit[0])
        header = hit[0].splitlines()[0]
        self.assertLessEqual(
            len(header.split()) - 2, 12,
            "the header is a paragraph again: %s" % header)
        self.assertIn("3h58m", hit[0], "the remainder is not in human units")
        self.assertNotIn("%ds" % self.TTL, hit[0])
        self.assertIn("helm chat stop-guard --detail", hit[0],
                      "the full diagnostic is not reachable from the block")

    def test_the_detail_verb_still_prints_the_long_form(self):
        """THE OTHER HALF. A short block that lost the diagnostic would satisfy
        the budget above and be a worse surface, so the long form is asserted
        to still exist, to be LONGER, and to bypass the same-state latch a
        reader who asked for it has already seen."""
        self.claim(self.LANE, ttl=self.TTL)
        short = self.sermons(self.guard()[0])
        self.assertEqual(len(short), 1)                  # positive control
        blocks, warns = seats.stop_guard(session=SESSION, room="main",
                                         seat=ME, detail=True)
        long_form = self.sermons(blocks)
        self.assertEqual(len(long_form), 1,
                         "--detail compressed to the one-liner instead of "
                         "reprinting: %r" % (blocks + warns))
        self.assertGreater(
            len(long_form[0]), len(short[0]),
            "--detail prints no more than the blocked stop does, so the "
            "diagnostic the block dropped is now reachable from nowhere")
        self.assertNotIn("helm chat stop-guard --detail", long_form[0],
                         "the long form points at itself")
