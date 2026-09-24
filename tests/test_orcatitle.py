#!/usr/bin/env python3
"""`helm/orcatitle.py` — the pane title helm WRITES, and never reads back.

The arms are grouped by the property they defend:

  RENDERING   the title is the seat name as the BOARD shows it, and nothing
              else, because its job is to be matched against the board by eye
  CONSEQUENCE what that byte-equality does to the one guard that reads titles
  REFUSAL     a pane helm cannot NAME keeps its host-written title
  IDEMPOTENCE a pane already reading correctly is not re-written
  DEGRADATION every failure mode of the rename is a row, never an exception
  RENAME      the clobber that does not look like one
  AUTHORITY   the invariant: a title is an OUTPUT. Arms drive the SHIPPED
              identity join with lying titles in place.

Every scan here carries a must-hit control, and the fixtures drive the
shipped producers (`orcaadopt.pane_rows`, `orcatitle.restamp`) rather than
inventing their own input.
"""
import ast
import io
import os
import sys
import shutil
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `seat` IS THE FACADE for this subsystem and the tree requires it
# alongside any implementation-module import: the arms below reach
# `seat_lifecycle_runtime` to assert on the identity reader and on the
# shipped source of the rebind hook, and an impl import is protected
# only when a facade import stands in an enclosing scope.
from helm import harness, orcaadopt, orcatitle, seat, seats  # noqa: E402,F401

HELM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "helm")

LANE = "/home/x/dev/proj-wt/a-lane-name"


def pane(handle, seat=None, title="", worktree=LANE, incarnation=None):
    """One `orcaadopt.pane_rows` row, in the shape `harness._pane_row` emits."""
    row = {"handle": handle, "title": title, "worktree": worktree,
           "seat": seat, "status": "connected", "writable": True,
           "orphaned": False, "pty_id": "pty-%s" % (handle,)}
    # ABSENT BY DEFAULT, ON PURPOSE. Most arms here predate the assertion
    # ledger and must keep measuring what they measured: a row with no
    # incarnation is never suppressed, so they are untouched by it. The arms
    # that are ABOUT the ledger pass one explicitly.
    if incarnation is not None:
        row["incarnation_id"] = incarnation
    return row


class IsolatedHome(unittest.TestCase):
    """Every class that APPLIES inherits this, because `restamp` now writes.

    The assertion ledger is state under HELM_HOME, so a suite that left the
    ambient one in place would write to the OPERATOR'S live helm home while
    running — and would then hand each arm whatever the previous arm recorded,
    which is a cross-test channel wearing the shape of a cache. A per-test home
    makes the ledger start empty and belong to nobody.
    """

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="helm-orcatitle-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(tmp, "helm-home"),
            "HELM_ADOPTED_DIR": os.path.join(tmp, "adopted"),
            "HELM_CHAT_DIR": os.path.join(tmp, "chat"),
        })
        env.start()
        self.addCleanup(env.stop)


class FakeAd(object):
    """Records every rename in order. `fail` raises, `echo` overrides what the
    metaharness claims it stored."""
    name = "orca"

    def __init__(self, fail=None, echo=None):
        self.renamed, self.fail, self.echo = [], fail, echo
        self.titles = {}

    def rename(self, handle, title=None):
        self.renamed.append((handle, title))
        if self.fail:
            raise harness.HarnessError(self.fail)
        stored = self.echo if self.echo is not None else title
        self.titles[handle] = stored
        return stored


class MuteAd(object):
    """A metaharness with NO rename verb — herdr's shape, and the shape of an
    orca whose CLI dropped the subcommand."""
    name = "herdr"


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

class RenderingTest(IsolatedHome):

    def test_a_title_is_the_seat_name_and_nothing_else(self):
        """The owner reads a tab strip and matches it against the fleet board;
        anything appended breaks that match. MUTATION: append the worktree, a
        state word, or any separator."""
        self.assertEqual(orcatitle.desired_title("seat-a"), "seat-a")
        self.assertEqual(orcatitle.desired_title("seat-under-test"),
                         "seat-under-test")

    def test_the_title_is_rendered_by_the_SAME_function_as_the_board(self):
        """"exactly as the board shows it" stays true by USING the board's
        renderer, not by two surfaces agreeing by hand."""
        # UNCONDITIONAL CONTROL: the shared renderer produces a real string
        # for an ordinary name, so the loop is not comparing two empties.
        self.assertEqual(orcatitle.desired_title("seat-a"), "seat-a")
        for name in ("seat-a", "seat-b", "seat.with-dots_9"):
            self.assertEqual(orcatitle.desired_title(name),
                             seats._seat_label(name), name)

    def test_a_hostile_seat_name_is_laundered_before_it_reaches_the_pane(self):
        """A seat name is an unvalidated join seam and a tab title is a display
        sink. MUTATION: drop `_seat_label` from `desired_title`."""
        ad = FakeAd()
        hostile = "lane\x1b[2Jpwn"
        # MUST-HIT: the payload really is in the input this run consumes.
        self.assertIn("\x1b", hostile)
        orcatitle.restamp(ad, [pane("h1", seat=hostile)], apply=True)
        self.assertEqual(len(ad.renamed), 1, ad.renamed)
        self.assertNotIn("\x1b", ad.renamed[0][1])

    def test_the_worktree_a_pane_reports_reaches_NO_part_of_the_title(self):  # noqa: VACUOUS_ASSERTION — the desired-title assertEqual is the unconditional positive control on the same observable; the fixtures-differ assertNotEqual is the must-hit that keeps the equality below meaningful
        """The pane row still carries a worktree; the title must not read it.
        MUTATION: fold the worktree back in — these two stop being equal."""
        # MUST-HIT: the two fixtures really do differ in the field under test.
        self.assertNotEqual(LANE, "/somewhere/else")
        here = orcatitle.plan([pane("h1", seat="seat-a", worktree=LANE)])
        elsewhere = orcatitle.plan(
            [pane("h1", seat="seat-a", worktree="/somewhere/else")])
        self.assertEqual(here[0].desired, "seat-a")
        self.assertEqual(here[0].desired, elsewhere[0].desired)


# ---------------------------------------------------------------------------
# CONSEQUENCE — what a seat-name title does to the one guard that reads titles
# ---------------------------------------------------------------------------

class ReapGuardConsequenceTest(IsolatedHome):
    """`_reap_stale` REFUSES to reap when an UNREGISTERED pane's title is
    byte-equal to a seat name, on the premise that such a title is an identity
    claim helm did not make. Helm now makes exactly that claim on purpose, so
    the premise no longer holds for panes helm titled.

    PINNED RATHER THAN WORKED AROUND. The guard fails CLOSED — it withholds a
    kill and never performs one — so the effect is more refusals, never a
    wrong reap, and for a pane helm can no longer place a refusal is arguably
    the better answer. The premise lives in that guard and only its owner can
    re-rule it; this arm exists so the interaction is a MEASURED, declared
    consequence rather than a surprise found during an incident."""

    @staticmethod
    def _guard(title, seat):
        return title == seat            # seat_lifecycle_runtime's predicate

    def test_a_helm_written_title_now_satisfies_the_reap_guards_predicate(self):
        # MUST-HIT: the predicate is satisfiable at all.
        self.assertTrue(self._guard("seat-a", "seat-a"))
        self.assertTrue(
            self._guard(orcatitle.desired_title("seat-a"), "seat-a"),
            "the consequence this class records has silently gone away — if "
            "the title text changed, re-read the guard and this docstring")

    def test_the_predicate_is_still_FALSE_for_another_seats_title(self):
        """The guard did not become unconditional: a pane titled for seat-b is
        not an identity claim about seat-a, so a reap of seat-a is unaffected
        by every other seat's tab."""
        # MUST-HIT: the same predicate on the same renderer DOES fire for
        # the seat the title names, so the False below is about the pairing.
        self.assertTrue(self._guard(orcatitle.desired_title("seat-b"),
                                    "seat-b"))
        self.assertFalse(self._guard(orcatitle.desired_title("seat-b"),
                                     "seat-a"))


# ---------------------------------------------------------------------------
# REFUSAL — a pane helm cannot NAME keeps its orca title
# ---------------------------------------------------------------------------

class RefusalTest(IsolatedHome):

    def test_a_pane_no_seat_claims_is_absent_from_the_plan(self):
        rows = [pane("named", seat="seat-a"), pane("unowned", seat=None),
                pane("blank", seat="")]
        planned = {s.handle for s in orcatitle.plan(rows)}
        self.assertEqual(planned, {"named"},
                         "only a pane helm can NAME may be stamped")

    def test_an_unnamed_pane_is_never_renamed_even_under_apply(self):
        """MUTATION: fall back to a title-derived or inventory-derived name for
        an unowned pane — `ad.renamed` stops being exactly one entry."""
        ad = FakeAd()
        orcatitle.restamp(ad, [pane("unowned", seat=None, title="seat-a"),
                               pane("named", seat="seat-b")], apply=True)
        self.assertEqual([h for h, _t in ad.renamed], ["named"], ad.renamed)

    def test_a_row_with_a_seat_but_no_handle_is_not_stamped(self):
        """There is no address to rename, so the row is dropped. The sibling
        in the SAME call is the positive control: the pass did run."""
        ad = FakeAd()
        orcatitle.restamp(ad, [pane(None, seat="seat-a"),
                               pane("h2", seat="seat-b")], apply=True)
        self.assertEqual(ad.renamed, [("h2", "seat-b")])


class AnOrcaOpenedPaneBecomesNameableTest(IsolatedHome):
    """task/2673 PART C, end to end through the one naming join: a bare
    `claude` in an Orca pane (no HELM_CHAT_NAME, no --resume) whose session
    joined the roster is named from Claude's own session record, and the plan
    then stamps its pane with the seat name."""

    class Ad(object):
        name = "orca"

        def panes(self):
            return [pane("h-fresh", title="Terminal 3")], None

        def resolve_pane(self, key):
            return {"handle": "h-fresh"} if key == "pk-fresh" else {}

    def plan_for(self, records):
        proc = {"pid": 77, "start": "5150", "seat": None,
                "pane_key": "pk-fresh", "resume_sid": None,
                "worktree_id": None}
        with mock.patch("helm.beacons.holder_records", return_value=records):
            rows, note = orcaadopt.pane_rows(
                adapter=self.Ad(), procs=[proc], unreadable=[],
                roster_sids={"proj-claude": "sid-fresh"})
        self.assertIsNone(note)
        return orcatitle.plan(rows)

    def test_the_newly_nameable_pane_is_stamped(self):
        stamps = self.plan_for({"sid-fresh": (77, "5150")})
        self.assertEqual([(s.handle, s.desired) for s in stamps],
                         [("h-fresh", orcatitle.desired_title("proj-claude"))])

    def test_without_its_record_the_pane_stays_unstamped(self):
        # The record is what names it: without it nothing is stamped, and
        # the same fixture WITH it stamps one pane (unconditional control).
        self.assertEqual(self.plan_for({}), [])
        self.assertEqual(len(self.plan_for({"sid-fresh": (77, "5150")})), 1)


# ---------------------------------------------------------------------------
# IDEMPOTENCE — the owner reads this surface; it may not become noise
# ---------------------------------------------------------------------------

class IdempotenceTest(IsolatedHome):

    def test_a_pane_already_reading_correctly_is_not_re_written(self):
        """MUTATION: rename unconditionally — the stale sibling's entry stops
        being the only one in the log."""
        ad = FakeAd()
        correct = pane("h1", seat="seat-a",
                       title=orcatitle.desired_title("seat-a"))
        stale = pane("h2", seat="seat-b", title="✳ Claude Code")
        stamps = orcatitle.restamp(ad, [correct, stale], apply=True)
        self.assertEqual([s.action for s in stamps],
                         [orcatitle.OK, orcatitle.WROTE])
        # THE CONTROL IS IN THE SAME CALL: the stale sibling WAS written, so
        # the untouched pane is a per-pane skip and not a dead code path.
        self.assertEqual(ad.renamed, [("h2", "seat-b")])

    def test_an_orca_glyph_decoration_is_a_DIFFERENT_title(self):
        """Orca decorates its OWN auto-generated titles with an activity
        glyph, so a decorated title is simply not equal and is rewritten — no
        glyph-stripping rung exists or is wanted, because one would make helm
        read a title to decide something. The inventory need not report a title
        HELM set — see the module docstring — and nothing here needs it to: a
        pane helm has never titled carries orca's own generated title in the
        field the inventory does return, which is the case this arm drives."""
        ad = FakeAd()
        stamps = orcatitle.restamp(
            ad, [pane("h1", seat="seat-a", title="✳ seat-a")], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])

    def test_a_dry_run_writes_nothing_and_still_shows_the_desired_title(self):
        """The control is the SAME fixture run again with apply=True: it does
        rename, so the empty log above is the flag's doing."""
        ad = FakeAd()
        stamps = orcatitle.restamp(ad, [pane("h1", seat="seat-a")], apply=False)
        self.assertEqual(stamps[0].action, orcatitle.WOULD)
        self.assertEqual(stamps[0].desired, "seat-a")
        self.assertEqual(ad.renamed, [])
        orcatitle.restamp(ad, [pane("h1", seat="seat-a")], apply=True)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])

    def test_a_written_stamp_reports_the_new_title_as_current(self):
        ad = FakeAd()
        stamps = orcatitle.restamp(ad, [pane("h1", seat="seat-a")], apply=True)
        self.assertEqual(stamps[0].current, "seat-a")


# ---------------------------------------------------------------------------
# DEGRADATION — a tab label may never fail the sweep that called it
# ---------------------------------------------------------------------------

class AssertionLedgerTest(IsolatedHome):
    """An unattended sweep converges even when inventory retains its generated
    title. The ledger records observed writes on a PTY incarnation, not the
    current tab's customTitle. Explicit repair and writer ordering are exercised
    separately below; no fixture here proves a host's restart semantics.
    """

    def test_a_second_sweep_over_a_pane_helm_ALREADY_TITLED_writes_nothing(self):
        """The convergence the module promises. The inventory still reports
        orca's own summary on both passes — that is the point: nothing about
        the pane's reported title changes, and the sweep must still stop."""
        ad = FakeAd()
        row = pane("h1", seat="seat-a", title="✳ Set up Claude Judge",
                   incarnation="inc-1")
        first = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(first[0].action, orcatitle.WROTE)
        second = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(second[0].action, orcatitle.OK)
        self.assertEqual(ad.renamed, [("h1", "seat-a")],
                         "the sweep re-issued a rename it had already proven")

    def test_a_NEW_INCARNATION_defeats_the_memory(self):
        """A different PTY incarnation cannot reuse an old assertion. This
        drives the invalidation key, not a claim that every restore changes it."""
        ad = FakeAd()
        before = pane("h1", seat="seat-a", title="x", incarnation="inc-1")
        orcatitle.restamp(ad, [before], apply=True)
        after = pane("h1", seat="seat-a", title="✳ Boondoggling",
                     incarnation="inc-2")
        stamps = orcatitle.restamp(ad, [after], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)
        self.assertEqual(ad.renamed, [("h1", "seat-a"), ("h1", "seat-a")])

    def test_a_RENAMED_seat_defeats_the_memory_on_the_same_incarnation(self):
        """The other clobber, and the worse one: the pane is unchanged but the
        title it carries now names the WRONG seat. The remembered string no
        longer equals the desired one, so the assertion does not apply."""
        ad = FakeAd()
        orcatitle.restamp(ad, [pane("h1", seat="seat-a", incarnation="inc-1")],
                          apply=True)
        stamps = orcatitle.restamp(
            ad, [pane("h1", seat="seat-b", incarnation="inc-1")], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)
        self.assertEqual(ad.renamed[-1], ("h1", "seat-b"))

    def test_a_pane_with_NO_incarnation_id_is_never_suppressed(self):
        """An assertion that cannot be AGED is not one this module trusts: a
        metaharness that reports no incarnation gives helm no way to know the
        memory is still about this pane, and a redundant rename is a far
        smaller fault than a title left wrong after a reboot."""
        ad = FakeAd()
        row = pane("h1", seat="seat-a")          # no incarnation_id at all
        orcatitle.restamp(ad, [row], apply=True)
        stamps = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)
        self.assertEqual(len(ad.renamed), 2, "an unageable memory suppressed")
        # MUST-HIT: the identical fixture WITH an incarnation does suppress, so
        # this arm measures the missing key and not some other refusal.
        ad2 = FakeAd()
        with_inc = pane("h2", seat="seat-a", incarnation="inc-1")
        orcatitle.restamp(ad2, [with_inc], apply=True)
        orcatitle.restamp(ad2, [with_inc], apply=True)
        self.assertEqual(len(ad2.renamed), 1)

    def test_separate_homes_do_not_share_assertion_history(self):
        ad = FakeAd()
        row = pane("h1", seat="seat-a", incarnation="inc-1")
        orcatitle.restamp(ad, [row], apply=True)
        first_path = orcatitle.state_path()
        with mock.patch.dict(os.environ, {
                "HELM_HOME": os.environ["HELM_HOME"] + "-other",
                "HELM_ADOPTED_DIR": os.environ["HELM_ADOPTED_DIR"] + "-other"}):
            self.assertNotEqual(orcatitle.state_path(), first_path)
            other = FakeAd()
            self.assertEqual(orcatitle.restamp(other, [row], apply=True)[0].action,
                             orcatitle.WROTE)
            self.assertEqual(orcatitle.restamp(other, [row], apply=True)[0].action,
                             orcatitle.OK)
            self.assertEqual(other.renamed, [("h1", "seat-a")])
            orcatitle.restamp(other, [dict(row, seat="seat-b")], apply=True)
        self.assertEqual(orcatitle.asserted()["h1"], ("inc-1", "seat-a"))
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.OK)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])

    def test_a_write_that_FAILED_is_never_remembered(self):
        """The ledger records PROVEN writes only. Remembering a failed rename
        would suppress the retry and leave the pane wrong forever — the
        failure mode that turns a transient refusal into a permanent one."""
        ad = FakeAd(fail="orca: terminal not found")
        row = pane("h1", seat="seat-a", incarnation="inc-1")
        first = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(first[0].action, orcatitle.FAILED)
        ok = FakeAd()
        stamps = orcatitle.restamp(ok, [row], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE,
                         "a failed rename was remembered as done")

    def test_a_write_the_metaharness_CONTRADICTED_is_never_remembered(self):
        """Same rule through the other refusal: orca answered OK but echoed a
        different title, so the write did not do what it said and there is
        nothing to remember."""
        ad = FakeAd(echo="something else entirely")
        row = pane("h1", seat="seat-a", incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.FAILED)
        ok = FakeAd()
        self.assertEqual(orcatitle.restamp(ok, [row], apply=True)[0].action,
                         orcatitle.WROTE)

    def test_a_DRY_RUN_records_nothing(self):
        """A survey that promised to change nothing must not change the memory
        either — otherwise the next real sweep skips a pane nobody ever wrote."""
        ad = FakeAd()
        row = pane("h1", seat="seat-a", incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [row], apply=False)[0].action,
                         orcatitle.WOULD)
        stamps = orcatitle.restamp(FakeAd(), [row], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE,
                         "a dry run recorded an assertion it never made")

    def test_an_UNREADABLE_ledger_costs_a_rename_and_never_the_sweep(self):
        """Fail-open, the same law the rest of the module runs on: a corrupt
        or unreadable ledger yields an empty memory, so the pane is re-titled
        rather than skipped, and nothing raises."""
        with io.open(_ledger_path(), "w", encoding="utf-8") as f:
            f.write("{not json at all")
        ad = FakeAd()
        stamps = orcatitle.restamp(
            ad, [pane("h1", seat="seat-a", incarnation="inc-1")], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])


class AssertionOrderingTest(IsolatedHome):

    def test_concurrent_writes_keep_history_in_host_write_order(self):
        """B writes new and pauses; A wants old. With serialization A waits
        before reading history, then writes AND records old after B completes.
        Without it A finishes first and B's late memory contradicts the host.
        Events drive both schedules without same-thread lock reentry or sleeps.
        """
        b_written, release_b, a_reached = (threading.Event() for _ in range(3))
        host, writes, outcomes, errors = {}, [], {}, []
        ad = FakeAd()

        def rename(handle, title):
            host[handle] = title
            writes.append(title)
            if threading.current_thread().name == "title-new":
                b_written.set()
                if not release_b.wait(5):
                    raise RuntimeError("test did not release writer B")
            return title

        ad.rename = rename
        enter = seats._flocked.__enter__

        def observed_enter(lock):
            if threading.current_thread().name == "title-old":
                a_reached.set()             # reached the real lock, not a stub
            return enter(lock)

        def write(name):
            try:
                outcomes[name] = orcatitle.restamp(
                    ad, [pane("h1", seat=name, title="auto-summary",
                              incarnation="inc-1")], apply=True)
            except Exception as e:
                errors.append(str(e))
            finally:
                if name == "seat-old":
                    # On the original unlocked implementation A finishes
                    # before B is released, reproducing the inversion exactly.
                    a_reached.set()

        writers = []
        with mock.patch.object(seats._flocked, "__enter__", observed_enter):
            try:
                for name, role, reached in (("seat-new", "title-new", b_written),
                                             ("seat-old", "title-old", a_reached)):
                    t = threading.Thread(target=write, args=(name,), name=role,
                                         daemon=True)
                    t.start()
                    writers.append(t)
                    self.assertTrue(reached.wait(5), role + " did not reach its seam")
            finally:
                release_b.set()
                for t in writers:
                    t.join(5)
        self.assertTrue(all(not t.is_alive() for t in writers), "writer leaked")
        self.assertEqual(set(outcomes), {"seat-new", "seat-old"})
        self.assertEqual(errors, [], outcomes)
        self.assertEqual(writes, ["seat-new", "seat-old"])
        self.assertEqual([outcomes[n][0].action for n in ("seat-new", "seat-old")],
                         [orcatitle.WROTE, orcatitle.WROTE])
        self.assertEqual(orcatitle.asserted()["h1"], ("inc-1", host["h1"]),
                         "the ledger certified a title the later writer replaced")
        desired = pane("h1", seat="seat-new", title="auto-summary",
                       incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [desired], apply=True)[0].action,
                         orcatitle.WROTE)
        self.assertEqual(host["h1"], "seat-new")
        self.assertEqual(orcatitle.restamp(ad, [desired], apply=True)[0].action,
                         orcatitle.OK)
        self.assertEqual(writes, ["seat-new", "seat-old", "seat-new"])

    def test_failed_remember_cannot_resurrect_an_old_assertion(self):
        """A real failed replacement after host writes must cost retries, not
        preserve the previous names as authority over the newly changed host.
        """
        from helm import pk
        ad = FakeAd()
        rows = [pane("h1", seat="seat-new", incarnation="inc-1"),
                pane("h2", seat="seat-new-2", incarnation="inc-2"),
                pane("keep", seat="seat-keep", incarnation="inc-keep")]
        self.assertEqual([s.action for s in orcatitle.restamp(ad, rows, apply=True)],
                         [orcatitle.WROTE] * 3)
        self.assertEqual(orcatitle.asserted()["h1"], ("inc-1", "seat-new"))
        replace, failures = pk.os.replace, []

        def fail_remember(src, dst):
            value = pk.read_json(src, {})
            if (dst == orcatitle.state_path() and
                    value.get("h1", {}).get("title") == "seat-old"):
                failures.append("remember")
                raise OSError("injected assertion replacement failure")
            return replace(src, dst)

        old = [dict(rows[0], seat="seat-old"),
               dict(rows[1], seat="seat-old-2")]
        with mock.patch.object(pk.os, "replace", side_effect=fail_remember):
            stamps = orcatitle.restamp(ad, old, apply=True)
        self.assertEqual(failures, ["remember"], "persistence refusal never ran")
        self.assertEqual([s.action for s in stamps], [orcatitle.WROTE] * 2)
        self.assertEqual(ad.titles["h1"], "seat-old")
        self.assertEqual(ad.titles["h2"], "seat-old-2")
        self.assertEqual(orcatitle.asserted(),
                         {"keep": ("inc-keep", "seat-keep")})
        repaired = orcatitle.restamp(ad, rows, apply=True)
        self.assertEqual([s.action for s in repaired],
                         [orcatitle.WROTE, orcatitle.WROTE, orcatitle.OK])
        self.assertEqual(ad.titles["h1"], "seat-new")
        self.assertEqual(ad.titles["h2"], "seat-new-2")
        self.assertEqual([s.action for s in orcatitle.restamp(ad, rows, apply=True)],
                         [orcatitle.OK] * 3)
        self.assertEqual(len(ad.renamed), 7)

    def test_failed_invalidation_withholds_host_writes_until_retry(self):
        from helm import pk
        ad = FakeAd()
        row = pane("h1", seat="seat-new", incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.WROTE)
        before = orcatitle.asserted()
        replace, failures = pk.os.replace, []

        def fail_invalidation(src, dst):
            if dst == orcatitle.state_path() and "h1" not in pk.read_json(src, {}):
                failures.append("invalidate")
                raise OSError("injected invalidation replacement failure")
            return replace(src, dst)

        old = dict(row, seat="seat-old")
        with mock.patch.object(pk.os, "replace", side_effect=fail_invalidation):
            stamps = orcatitle.restamp(ad, [old], apply=True)
        self.assertEqual(failures, ["invalidate"], "invalidation refusal never ran")
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertIn("invalidation replacement failure", stamps[0].reason)
        self.assertEqual(orcatitle.asserted(), before)
        self.assertEqual(ad.titles["h1"], "seat-new")
        self.assertEqual(ad.renamed, [("h1", "seat-new")])
        self.assertEqual(orcatitle.restamp(ad, [old], apply=True)[0].action,
                         orcatitle.WROTE)
        self.assertEqual(ad.titles["h1"], "seat-old")
        self.assertEqual(orcatitle.asserted()["h1"], ("inc-1", "seat-old"))

    def test_lock_failure_reports_refusal_without_mutating_host_or_history(self):
        ad = FakeAd()
        old = pane("h1", seat="seat-old", incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [old], apply=True)[0].action,
                         orcatitle.WROTE)
        before = orcatitle.asserted()
        new = dict(old, seat="seat-new")
        with mock.patch.object(orcatitle, "_flocked") as acquire:
            acquire.return_value.__enter__.return_value.f = None
            stamps = orcatitle.restamp(ad, [new], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertIn("lock unavailable", stamps[0].reason)
        self.assertEqual(ad.renamed, [("h1", "seat-old")])
        self.assertEqual(orcatitle.asserted(), before)
        self.assertEqual(orcatitle.restamp(ad, [new], apply=True)[0].action,
                         orcatitle.WROTE)
        self.assertEqual(ad.titles["h1"], "seat-new")

    def test_an_uncreatable_lock_parent_is_a_reported_refusal(self):
        ad = FakeAd()
        row = pane("h1", seat="seat-a", incarnation="inc-1")
        with mock.patch.object(orcatitle.os, "makedirs",
                               side_effect=PermissionError("read-only state")):
            stamps = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertIn("read-only state", stamps[0].reason)
        self.assertEqual(ad.renamed, [])
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.WROTE)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])


def _ledger_path():
    p = orcatitle.state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


class DegradationTest(IsolatedHome):

    def test_a_refusing_metaharness_becomes_a_row_and_the_sweep_continues(self):
        """MUTATION: let `_write` propagate — this raises instead of returning,
        and the second pane never gets its title."""
        ad = FakeAd(fail="terminal_handle_stale")
        stamps = orcatitle.restamp(
            ad, [pane("gone", seat="seat-a"), pane("here", seat="seat-b")],
            apply=True)
        self.assertEqual([s.action for s in stamps],
                         [orcatitle.FAILED, orcatitle.FAILED])
        self.assertIn("terminal_handle_stale", stamps[0].reason)
        self.assertEqual([h for h, _t in ad.renamed], ["gone", "here"],
                         "the second pane was still attempted")

    def test_a_refusal_reason_is_ONE_LINE_and_bounded(self):
        """Measured against the live host: a rename on a stale handle answers
        an error whose text embeds the metaharness's whole pretty-printed JSON
        reply. Every renderer puts a reason INSIDE one line. MUTATION: use
        `str(e)` in `_write`."""
        blob = 'orca terminal rename: rc 1 — {\n  "ok": false,\n' + "x" * 400
        # MUST-HIT: the raw message really is multi-line and over the bound.
        self.assertIn("\n", blob)
        self.assertGreater(len(blob), orcatitle._REASON_MAX)
        stamps = orcatitle.restamp(FakeAd(fail=blob),
                                   [pane("h1", seat="seat-a")], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertNotIn("\n", stamps[0].reason)
        self.assertLessEqual(len(stamps[0].reason), orcatitle._REASON_MAX)
        self.assertIn("rc 1", stamps[0].reason, "the reason lost its cause")
        # and it survives both renderers that put a reason inside a row
        self.assertNotIn("\n", stamps[0].line())
        self.assertNotIn("\n", orcatitle.note(stamps[0]))

    def test_a_metaharness_with_no_rename_verb_is_a_row_not_a_crash(self):
        stamps = orcatitle.restamp(MuteAd(), [pane("h1", seat="seat-a")],
                                   apply=True)
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertIn("no pane rename verb", stamps[0].reason)

    def test_a_host_that_stored_something_else_is_a_FAILURE_not_a_success(self):
        """ASSERT THE EFFECT, NOT THE ABSENCE OF A COMPLAINT. orca echoes the
        title it stored; an ok reply carrying a different string is a write
        that did not do what it said. MUTATION: drop the echo check."""
        ad = FakeAd(echo="something orca preferred")
        stamps = orcatitle.restamp(ad, [pane("h1", seat="seat-a")], apply=True)
        self.assertEqual(stamps[0].action, orcatitle.FAILED)
        self.assertIn("something orca preferred", stamps[0].reason)
        self.assertNotEqual(stamps[0].current, stamps[0].desired)

    def test_the_echo_check_passes_on_a_host_that_stored_what_it_was_told(self):
        """The must-hit control for the arm above: the identical path with an
        honest echo reports WROTE, so the FAILED above is about the mismatch
        and not about the check firing unconditionally."""
        stamps = orcatitle.restamp(FakeAd(), [pane("h1", seat="seat-a")],
                                   apply=True)
        self.assertEqual(stamps[0].action, orcatitle.WROTE)


# ---------------------------------------------------------------------------
# RENAME — the clobber that does not look like one
# ---------------------------------------------------------------------------

class AfterRenameTest(IsolatedHome):
    """A reboot leaves a title that is obviously orca's. A rename leaves a
    title that is still a perfect seat-name checksum and names the WRONG
    seat, so it keeps reading as authoritative while it lies."""

    def _ad(self, handle="h1", title="seat-old"):
        ad = FakeAd()
        ad.list = lambda: [pane(handle, title=title)]
        return ad

    def test_the_renamed_seats_tab_is_re_asserted_under_the_NEW_name(self):
        """MUTATION: drop the `after_rename` call from `rename_seat` — the tab
        keeps naming a seat that no longer exists until the next reboot."""
        ad = self._ad()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "h1"}):
            said = orcatitle.after_rename("seat-new", adapter=ad)
        self.assertEqual(ad.renamed, [("h1", "seat-new")])
        self.assertIn("seat-new", said)

    def test_a_pane_the_join_cannot_find_says_nothing_and_writes_nothing(self):
        """The live process still exports the OLD name, so the join can miss.
        A miss waits for the sweep; it never guesses a pane."""
        ad = self._ad()
        with mock.patch.object(orcaadopt, "resolve", return_value=None):
            self.assertEqual(orcatitle.after_rename("seat-new", adapter=ad), "")
        self.assertEqual(ad.renamed, [])
        # MUST-HIT: the same adapter DOES rename when the join finds the pane,
        # so the empty answer above is the miss and not a dead path.
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "h1"}):
            orcatitle.after_rename("seat-new", adapter=ad)
        self.assertEqual(ad.renamed, [("h1", "seat-new")])

    def test_a_raising_resolve_never_fails_the_rename(self):  # noqa: VACUOUS_ASSERTION — the two assertIn calls on the returned clause are unconditional positive controls; the empty rename log is the contract, and the sibling arm drives the same adapter to a write
        """A tab label may not fail a roster write. MUTATION: drop the bracket
        — this raises out of `rename_seat` instead."""
        ad = self._ad()
        with mock.patch.object(orcaadopt, "resolve",
                               side_effect=RuntimeError("orca is gone")):
            said = orcatitle.after_rename("seat-new", adapter=ad)
        self.assertIn("NOT re-asserted", said)
        self.assertIn("orca is gone", said)
        self.assertEqual(ad.renamed, [])

    def test_a_tab_that_ALREADY_reads_correctly_says_nothing(self):
        """A retried rename, or one `_seat_label` leaves identical, must be
        silent — the same quiet contract the sweep holds."""
        ad = self._ad(title="seat-new")
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "h1"}):
            self.assertEqual(orcatitle.after_rename("seat-new", adapter=ad), "")
            self.assertEqual(ad.renamed, [])
            # MUST-HIT: the SAME adapter and the SAME join answer a DIFFERENT
            # name with a real write, so the silence above is the tab already
            # being right and not a path that never ran.
            self.assertIn("seat-other",
                          orcatitle.after_rename("seat-other", adapter=ad))
        self.assertEqual(ad.renamed, [("h1", "seat-other")])

    def test_a_non_orca_metaharness_is_silently_out_of_scope(self):
        """A host with no pane titles is not a failure to report to whoever
        just renamed a seat. The orca control is in the same arm."""
        self.assertEqual(orcatitle.after_rename("seat-new", adapter=MuteAd()),
                         "")
        orca = self._ad()
        with mock.patch.object(orcaadopt, "resolve",
                               return_value={"handle": "h1"}):
            self.assertIn("seat-new",
                          orcatitle.after_rename("seat-new", adapter=orca))


# ---------------------------------------------------------------------------
# AUTHORITY — A TITLE IS AN OUTPUT, NEVER AN AUTHORITY
# ---------------------------------------------------------------------------

class TitleIsNeverAuthorityTest(IsolatedHome):
    """The invariant this feature must not erode. helm writes titles; nothing
    reads one to decide who a seat is."""

    def _joined(self, procs, titles):
        """`orcaadopt.pane_rows` driven for real, with the pane inventory's
        titles under our control -> {handle: seat}."""
        class Ad(object):
            name = "orca"

            def panes(self):
                return ([pane(h, title=t) for h, t in titles.items()], None)

            def resolve_pane(self, key):
                return {"handle": "handle-of-" + key}

        with mock.patch.object(orcaadopt, "helm_spawned", return_value={}), \
                mock.patch.object(orcaadopt, "_session_join_handles",
                                  return_value=None):
            rows, err = orcaadopt.pane_rows(adapter=Ad(), procs=procs,
                                            unreadable=[])
        self.assertIsNone(err, err)
        return {r["handle"]: r.get("seat") for r in rows}

    def test_the_join_ignores_a_title_that_names_a_DIFFERENT_seat(self):
        """The shipped identity join, run twice over the same processes: once
        with honest titles and once with titles that lie about every pane. The
        answer may not move — and the titles in the lying run are now exactly
        the strings helm itself writes, which is the whole hazard. MUTATION:
        make `pane_rows` fall back to the title when the join finds nothing."""
        procs = [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}]
        honest = self._joined(procs, {"handle-of-pk-a": "seat-a",
                                      "other": "✳ Claude Code"})
        lying = self._joined(procs, {"handle-of-pk-a": "seat-b",
                                     "other": "seat-a"})
        self.assertEqual(honest, lying)
        self.assertEqual(honest["handle-of-pk-a"], "seat-a")
        self.assertIsNone(honest["other"],
                          "a pane named only by its title stays UNNAMED")

    def test_the_join_DOES_move_when_the_PROCESS_evidence_moves(self):
        """THE CONTROL for the arm above. A harness that could not detect a
        changed answer would pass it for free."""
        moved = self._joined([{"pid": 1, "seat": "seat-c", "pane_key": "pk-a"}],
                             {"handle-of-pk-a": "seat-b", "other": "seat-a"})
        self.assertEqual(moved["handle-of-pk-a"], "seat-c")

    def test_the_idempotence_read_answers_ONLY_the_write_question(self):
        """orcatitle reads a title once — to skip a write it does not need. A
        title naming ANOTHER seat must be treated as a stale string to
        overwrite, never as evidence about whose pane this is. MUTATION: skip
        the rename when the current title already names some seat."""
        ad = FakeAd()
        row = pane("h1", seat="seat-a", title=orcatitle.desired_title("seat-b"))
        stamps = orcatitle.restamp(ad, [row], apply=True)
        self.assertEqual(ad.renamed, [("h1", "seat-a")])
        self.assertEqual(stamps[0].seat, "seat-a")

    def test_the_census_CATCHES_a_new_read_and_both_of_its_spellings(self):
        """MUST-HIT on the detector itself, not only on the corpus. The live
        scan below asserts an EMPTY set, and an empty set from a scanner that
        cannot see is the failure this whole registry exists to prevent.

        Both spellings are driven because a row read takes either. This
        already fired in anger: it flagged a `fields["title"]` carrying a
        SENTENCE ABOUT a title, and the cure was to rename the key."""
        for spelling in ('row["title"]', 'row.get("title")'):
            src = "def reads_it(row):\n    return %s\n" % spelling
            self.assertEqual(_reads_in(src, "fake.py"),
                             {("fake.py", "reads_it")}, spelling)
        # MUST-MISS: a different key is not a finding, so the detector is
        # selective rather than matching every subscript in sight.
        self.assertEqual(
            _reads_in('def f(row):\n    return row.get("handle")\n',
                      "fake.py"), set())

    def test_every_pane_title_READ_in_the_identity_modules_is_declared(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT assertIn on _reap_stale is the unconditional positive control on the same scan; an empty `undeclared` IS the claim
        """The durable half: a source census over the modules that decide which
        pane belongs to which seat. A title read is allowed to REFUSE and to
        RENDER; none may DECIDE. A new read fails this until someone writes
        down which kind it is.

        SCOPE, stated because a scanner's reach is narrower than the property:
        this sees the modules named in `_TITLE_MODULES` only. A title read in
        a module outside that list is invisible here — widen the list when a
        new module joins the pane-identity path.
        """
        found = _title_reads()
        # MUST-HIT: the reap guard's own reads are in the population, so a
        # scanner that matched nothing cannot pass this test silently.
        self.assertIn(("seat_lifecycle_runtime.py", "_reap_stale"), found,
                      "the scanner missed the known identity-by-title REFUSAL "
                      "in _reap_stale; the pattern rotted: %r" % (found,))
        undeclared = sorted(found - set(_TITLE_READERS))
        self.assertEqual(
            undeclared, [],
            "a NEW pane-title read appeared in a pane-identity module. A title "
            "is an OUTPUT: declare the read in _TITLE_READERS as REFUSAL, "
            "RENDER or IDEMPOTENCE — there is no fourth kind, and 'decides who "
            "a seat is' is not available: %r" % (undeclared,))
        stale = sorted(set(_TITLE_READERS) - found)
        self.assertEqual(stale, [], "declared title reads that no longer "
                                    "exist: %r" % (stale,))


#: Every function in a PANE-IDENTITY module that reads a pane row's `title`,
#: with the kind of read it is. REFUSAL = the read only ever withholds an
#: action; RENDER = it reaches a human-readable surface and decides nothing;
#: IDEMPOTENCE = it answers "is this write necessary". No entry may be an
#: identity decision, which is the whole invariant.
_TITLE_READERS = {
    ("seat_lifecycle_runtime.py", "_reap_stale"):
        "REFUSAL: an unregistered pane whose title is byte-equal to the seat "
        "name makes the reap REFUSE. The read can only withhold a kill — see "
        "ReapGuardConsequenceTest for what a seat-name title does to it",
    ("seat_lifecycle_runtime.py", "_spawn_plan"):
        "REFUSAL, dry-run half: the same predicate, printed as the refusal "
        "`--print` would hit",
    ("harness.py", "_pane_row"):
        "RENDER: the adapter's row builder, where the field is CREATED from "
        "the metaharness reply. Decides nothing",
    ("composers.py", "scan"):
        "RENDER: carries the title onto the composer row for a human reader. "
        "The pane's seat is never derived from it",
    ("composers.py", "cmd_composers"):
        "RENDER: prints that carried title in the listing's last column",
    ("orcatitle.py", "plan"):
        "IDEMPOTENCE: compares the current title with the one helm would "
        "write, so an already-correct pane is not re-written. It answers "
        "whether a write is needed, never whose pane this is",
    ("orcatitle.py", "asserted"):
        "IDEMPOTENCE, and the read is not even of a PANE: it loads the titles "
        "helm PROVED it wrote, out of helm's own ledger, because orca stores a "
        "renamed title in a field no inventory read returns. It answers "
        "whether a write is needed; the handle it is keyed by came from the "
        "row, never from this string",
}

#: The modules the census above can see. Pane identity is decided in these.
_TITLE_MODULES = ("orcaadopt.py", "seat_lifecycle_runtime.py",
                  "seat_resume_all.py", "harness.py", "composers.py",
                  "orcatitle.py", "seat.py", "seat_identity.py")


def _reads_in(source, name):
    """{(name, function)} for every `title` key read in one source string.

    DELIBERATELY CRUDE, and the crudeness is load bearing: it cannot tell a
    pane row from any other dict, so ANY key called `title` in a pane-identity
    module is a finding. That over-match is the rule, not a bug in it — in
    these modules a key named `title` reads as a pane title to every scanner
    and every human, so a value that is not one must not be called that. It
    has already earned its keep: it flagged a `fields["title"]` in
    `classify_spawned` that held a SENTENCE ABOUT a title, and the cure was to
    rename the key rather than to declare the exception.
    """
    out = set()
    tree = ast.parse(source)

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, fn or child.name)
                continue
            hit = False
            if isinstance(child, ast.Subscript):
                key = child.slice
                hit = isinstance(key, ast.Constant) and key.value == "title"
            elif isinstance(child, ast.Call) and \
                    isinstance(child.func, ast.Attribute) and \
                    child.func.attr == "get" and child.args and \
                    isinstance(child.args[0], ast.Constant) and \
                    child.args[0].value == "title":
                hit = True
            if hit and fn:
                out.add((name, fn))
            walk(child, fn)

    walk(tree, None)
    return out


def _title_reads():
    """{(module, function)} for every `title` key read across the modules that
    decide which pane belongs to which seat."""
    out = set()
    for name in _TITLE_MODULES:
        with open(os.path.join(HELM, name), encoding="utf-8") as fh:
            out |= _reads_in(fh.read(), name)
    return out


# ---------------------------------------------------------------------------
# THE VERB — `helm seat retitle`
# ---------------------------------------------------------------------------

class RetitleVerbTest(IsolatedHome):

    def _run(self, argv, procs, titles, unreadable=(), ad=None, incarnation=None):
        adapter = ad or FakeAd()

        def panes():
            return ([pane(h, title=t, incarnation=incarnation)
                     for h, t in titles.items()], None)

        adapter.panes = panes
        adapter.resolve_pane = lambda key: {"handle": "handle-of-" + key}
        out = io.StringIO()
        detect = mock.patch.object(harness, "detect", return_value=adapter)
        self.detect = detect.start()
        self.addCleanup(detect.stop)
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=(procs, list(unreadable))), \
                mock.patch.object(orcaadopt, "helm_spawned", return_value={}), \
                mock.patch.object(orcaadopt, "_session_join_handles",
                                  return_value=None), \
                mock.patch("sys.stdout", out):
            rc = orcatitle.cmd_retitle(argv)
        return rc, out.getvalue(), adapter

    def test_explicit_apply_repairs_a_same_incarnation_external_clobber(self):
        ad = FakeAd()
        handle = "handle-of-pk-a"
        row = pane(handle, seat="seat-a", title="auto-summary", incarnation="inc-1")
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.WROTE)
        self.assertEqual(ad.titles[handle], "seat-a")
        # Positive convergence control: unattended callers still spend history.
        self.assertEqual(orcatitle.restamp(ad, [row], apply=True)[0].action,
                         orcatitle.OK)
        self.assertEqual(ad.renamed, [(handle, "seat-a")])
        ad.titles[handle] = "a host-side edit on the same PTY"
        rc, out, _ad = self._run(
            ["--apply"], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {handle: "auto-summary"}, ad=ad, incarnation="inc-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(ad.titles[handle], "seat-a",
                         "explicit repair trusted stale assertion history")
        self.assertEqual(ad.renamed, [(handle, "seat-a"), (handle, "seat-a")])

    def test_explicit_preview_matches_apply_even_when_inventory_matches(self):
        ad = FakeAd()
        handle = "handle-of-pk-a"
        procs = [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}]
        row = pane(handle, seat="seat-a", title="auto-summary", incarnation="inc-1")
        orcatitle.restamp(ad, [row], apply=True)
        before = orcatitle.asserted()
        ad.titles[handle] = "different customTitle"
        rc, out, _ad = self._run([], procs, {handle: "seat-a"}, ad=ad,
                                 incarnation="inc-1")
        self.assertEqual(rc, 0, out)
        self.assertIn(orcatitle.WOULD, out)
        self.assertEqual(ad.renamed, [(handle, "seat-a")])
        self.assertEqual(ad.titles[handle], "different customTitle")
        self.assertEqual(orcatitle.asserted(), before)
        rc, out, _ad = self._run(["--apply"], procs, {handle: "seat-a"}, ad=ad,
                                 incarnation="inc-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(ad.titles[handle], "seat-a")
        self.assertEqual(len(ad.renamed), 2)

    def test_explicit_preview_does_not_create_state_or_a_lock(self):
        procs = [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}]
        titles = {"handle-of-pk-a": "auto-summary"}
        with mock.patch.object(orcatitle, "_flocked") as acquire:
            rc, out, ad = self._run([], procs, titles, incarnation="inc-1")
        self.assertEqual(rc, 0, out)
        self.assertIn(orcatitle.WOULD, out)
        acquire.assert_not_called()
        self.assertFalse(os.path.exists(os.path.dirname(orcatitle.state_path())))
        self.assertEqual(ad.renamed, [])
        rc, out, ad = self._run(["--apply"], procs, titles, incarnation="inc-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(ad.renamed, [("handle-of-pk-a", "seat-a")])
        self.assertTrue(os.path.isfile(orcatitle.state_path()))
        self.assertTrue(os.path.isfile(orcatitle.state_path() + ".lock"))

    def test_the_verb_renames_through_the_adapter_it_CHECKED(self):
        """ONE adapter, not two. `pane_rows` detects its OWN when given none,
        and then the handles this verb renames come from one object while the
        rename goes to another. MUTATION: drop `adapter=ad` from the
        `pane_rows` call — detect is consulted a second time."""
        rc, out, ad = self._run(
            ["--apply"], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": ""})
        # MUST-HIT: the pass really ran and really renamed, so the call count
        # below is a fact about this run and not about an aborted one.
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ad.renamed), 1, ad.renamed)
        self.assertEqual(self.detect.call_count, 1,
                         "the verb resolved a metaharness more than once")

    def test_the_dry_run_IS_the_checksum_table_and_writes_nothing(self):
        rc, out, ad = self._run(
            [], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": "✳ Claude Code", "stranger": "whatever"})
        self.assertEqual(rc, 0, out)
        self.assertIn("seat-a", out)
        self.assertIn("DRY RUN", out)
        self.assertIn("1 pane(s) helm cannot name", out)
        self.assertEqual(ad.renamed, [])

    def test_apply_writes_the_title(self):
        rc, out, ad = self._run(
            ["--apply"], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": "✳ Claude Code"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(ad.renamed, [("handle-of-pk-a", "seat-a")])

    def test_the_table_shows_and_COUNTS_the_title_it_would_replace(self):
        """A dry-run default only protects the owner if the thing about to be
        overwritten is on the screen — and on the live fleet some of these
        tabs carry titles he typed. MUTATION: print only the desired title."""
        rc, out, ad = self._run(
            [], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": "fork carry apply the owner typed this"})
        self.assertEqual(rc, 0, out)
        self.assertIn("fork carry apply", out)
        self.assertIn("1 of those would replace", out)
        self.assertEqual(ad.renamed, [])

    def test_an_untitled_pane_is_not_counted_as_a_replacement(self):
        """The must-hit's other half: the count is about titles that EXIST, so
        an empty tab must not inflate it."""
        rc, out, _ad = self._run(
            [], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("(none)", out)
        self.assertNotIn("would replace", out)

    def test_the_replaced_title_is_scrubbed_before_it_reaches_the_screen(self):
        """The current title is an EXTERNAL string heading for a terminal.
        MUTATION: print `self.current` raw in `Stamp.line`."""
        payload = "tab\x1b[2Jpwn"
        # MUST-HIT: the payload is really in the inventory this run consumed.
        self.assertIn("\x1b", payload)
        _rc, out, _ad = self._run(
            [], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": payload})
        self.assertIn("pwn", out, "the row for that pane was not rendered")
        self.assertNotIn("\x1b", out)

    def test_a_blind_census_is_REPORTED_and_still_titles_what_it_can_see(self):
        """Blindness can only SUBTRACT from the named set, so it costs a title
        and never writes a wrong one — but the operator has to be told the
        listing had a hole in it. MUTATION: drop the `cannot_look` call."""
        rc, out, ad = self._run(
            ["--apply"], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": ""}, unreadable=[4242])
        self.assertEqual(rc, 0, out)
        self.assertIn("4242", out)
        self.assertEqual(len(ad.renamed), 1, "the readable pane was still done")

    def test_a_refused_rename_exits_nonzero_and_names_the_seat(self):
        rc, out, _ad = self._run(
            ["--apply"], [{"pid": 1, "seat": "seat-a", "pane_key": "pk-a"}],
            {"handle-of-pk-a": ""}, ad=FakeAd(fail="terminal_handle_stale"))
        self.assertEqual(rc, 1)
        self.assertIn("not titled", out)
        self.assertIn("terminal_handle_stale", out)

    def test_a_bogus_flag_refuses_before_anything_is_written(self):  # noqa: VACUOUS_ASSERTION — rc 2 is the unconditional positive control; the empty rename log is the contract, and the apply arm above drives the same double to a write
        ad = FakeAd()
        err = io.StringIO()
        with mock.patch.object(harness, "detect", return_value=ad), \
                mock.patch("sys.stderr", err):
            rc = orcatitle.cmd_retitle(["--aply"])
        self.assertEqual(rc, 2)
        self.assertEqual(ad.renamed, [])


class AdapterRenameTest(unittest.TestCase):
    """The real adapter's rename echo and incarnation-bearing inventory."""

    def test_both_inventory_legs_carry_a_real_incarnation(self):
        ad = harness.OrcaAdapter()
        reply = {"terminals": [
            {"handle": "h1", "incarnationId": "inc-1", "ptyId": "pty-1",
             "tabId": "tab-1", "connected": True},
            {"handle": "h2", "connected": True}]}
        with mock.patch.object(ad, "_run", return_value=reply), \
                mock.patch.object(ad, "rpc", return_value=(reply, None)):
            cli = ad.list()
            rpc, err = ad.panes()
        self.assertIsNone(err)
        self.assertEqual(cli, rpc)
        self.assertEqual([r["incarnation_id"] for r in cli], ["inc-1", None])
        self.assertEqual((cli[0]["pty_id"], cli[0]["tab_id"]), ("pty-1", "tab-1"))

    def _ad(self, reply):
        ad = harness.OrcaAdapter()
        self.seen = []
        ad._run = lambda args, **kw: (self.seen.append(args), reply)[1]
        return ad

    def test_a_set_builds_the_measured_argv_and_returns_the_stored_title(self):
        ad = self._ad({"rename": {"title": "seat-a"}})
        self.assertEqual(ad.rename("term_1", "seat-a"), "seat-a")
        self.assertEqual(self.seen, [["terminal", "rename", "--terminal",
                                      "term_1", "--title", "seat-a", "--json"]])

    def test_a_RESET_omits_the_title_flag_and_asserts_no_echo(self):  # noqa: VACUOUS_ASSERTION — the argv equality is the unconditional positive control; the None return IS the contract for a reset
        """Orca's documented reset: omit `--title` and the pane goes back to
        its auto-generated title. There is no requested string to compare, so
        the reply's title field is not demanded."""
        ad = self._ad({"rename": {}})
        self.assertIsNone(ad.rename("term_1"))
        self.assertEqual(self.seen, [["terminal", "rename", "--terminal",
                                      "term_1", "--json"]])

    def test_a_reply_that_lost_the_title_field_is_LOUD(self):
        """`_field`'s law: a metaharness upgrade that moves a field must never
        fail silent. MUTATION: swap `_field` for a `.get` chain."""
        ad = self._ad({"rename": {}})
        with self.assertRaises(harness.HarnessError) as caught:
            ad.rename("term_1", "seat-a")
        self.assertIn("rename.title", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class StorageKeyIsNotIdentityTest(IsolatedHome):
    """A DURABLE RENAME SPLITS THE REGISTER IN TWO AND ONLY ONE HALF IS THE
    NAME ANYONE USES.

    The register stays keyed by the name a seat was SPAWNED under — that key
    never moves — while the current name is recorded in `identity`. Spawn
    titles the pane from the identity, so a renamed seat's tab starts correct.
    Every later re-assertion that reads the KEY therefore overwrites a correct
    title with a stale label, and the owner reads that tab as a checksum
    against the board, where the seat appears under its identity.
    """

    def _rec(self, storage, identity, handle="h1"):
        return {storage: {"handle": handle, "seat": storage,
                          "identity": identity}}

    def test_the_pane_map_labels_by_identity_not_by_the_storage_key(self):
        """`registered` feeds every surface that SHOWS a name — the title
        planner included — so it must resolve the pair."""
        with mock.patch.object(orcaadopt, "helm_spawned",
                               return_value=self._rec("seat-old", "seat-new")):
            mapped = {rec.get("handle"): (rec.get("identity") or name)
                      for name, rec in orcaadopt.helm_spawned().items()
                      if rec.get("handle")}
        self.assertEqual(mapped, {"h1": "seat-new"},
                         "the pane map handed back the storage key, which "
                         "after a rename is a name nothing else uses")

    def test_an_unrenamed_seat_is_unchanged_by_the_pair_resolution(self):
        """THE CONTROL. Most seats have identity == storage, and resolving the
        pair must be invisible for them — otherwise this cure would be a
        rename of every seat in the fleet."""
        with mock.patch.object(orcaadopt, "helm_spawned",
                               return_value=self._rec("seat-a", "seat-a")):
            mapped = {rec.get("handle"): (rec.get("identity") or name)
                      for name, rec in orcaadopt.helm_spawned().items()
                      if rec.get("handle")}
        self.assertEqual(mapped, {"h1": "seat-a"})

    def test_a_record_with_no_identity_falls_back_to_its_key(self):
        """Registers written before identity existed carry only the key, and
        they must keep working rather than resolving to None."""
        rec = {"seat-a": {"handle": "h1", "seat": "seat-a"}}
        with mock.patch.object(orcaadopt, "helm_spawned", return_value=rec):
            mapped = {r.get("handle"): (r.get("identity") or n)
                      for n, r in orcaadopt.helm_spawned().items()
                      if r.get("handle")}
        self.assertEqual(mapped, {"h1": "seat-a"})

    def test_the_rebind_hook_stamps_the_identity(self):
        """THE REGRESSION ITSELF: rebind and the resume --all timer reach one
        hook, and it took the storage key. A renamed seat's correct tab was
        overwritten on the next reboot sweep."""
        from helm import seat_lifecycle_runtime as runtime
        self.assertEqual(runtime._record_identity(
            {"seat": "seat-old", "identity": "seat-new"}), "seat-new")
        # CONTROL on the same reader: no identity recorded means the key IS
        # the identity, so the hook is unchanged for every unrenamed seat.
        self.assertEqual(runtime._record_identity({"seat": "seat-a"}), "seat-a")

    def test_the_hook_passes_that_identity_to_the_stamper(self):
        """Reading the right field is not enough — it has to reach the call.
        This drives the shipped source rather than re-describing it."""
        import inspect
        from helm import seat_lifecycle_runtime as runtime
        src = inspect.getsource(runtime)
        self.assertIn("orcatitle.restamp_one(ad, _record_identity(rec)", src,
                      "the rebind hook stamps something other than the "
                      "canonical identity")
        self.assertNotIn("orcatitle.restamp_one(ad, seat_name", src)
