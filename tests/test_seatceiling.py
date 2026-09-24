"""The kernel's own throttle tally, reported without a verdict.

THE MODULE DOES NOT CLASSIFY, so these arms are about REPORTING HONESTLY
rather than about classifying correctly: what a counter does and does not
license, and which absences may never read as health. Every fixture shape was
read off a live box — both real slice namings, a leaf scope whose own high
reads `max`, and a cgroup ROOT that carries no memory.events at all.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import seatceiling


PID_SLICE = "agents-pid2887201.slice"
NAME_SLICE = "agents-codex_5.slice"
GB = 1024 ** 3

#: VERBATIM shape of a real memory.events file on this fleet.
REAL_EVENTS = "low 0\nhigh 9548\nmax 0\noom 0\noom_kill 0\n"


class Tree(object):
    """A cgroup-shaped tree whose ROOT carries no memory.events, as on a real
    box, plus a /proc/<pid>/cgroup pointing into it."""

    def __init__(self, case, slice_name=NAME_SLICE, rel=None, blind=None):
        """`rel` builds an ARBITRARY chain and populates a counter at every
        level except `blind`, so an arm can name the case it is asking about —
        a process in the cgroup ROOT, one several levels down, one whose chain
        has a level with no counter interface. Without it the fixture keeps
        the fleet-shaped layout the other arms name by attribute."""
        if rel is not None:
            self._arbitrary(case, rel, blind)
            return
        self.root = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, self.root, True)
        self.cg = os.path.join(self.root, "cgroup")
        self.rel = "user.slice/agents.slice/" + slice_name
        self.slice = os.path.join(self.cg, self.rel)
        self.scope = os.path.join(self.slice, "run-p1-i1.scope")
        self.ancestor = os.path.join(self.cg, "user.slice", "agents.slice")
        os.makedirs(self.scope)
        self.proc = os.path.join(self.root, "proc")
        self.pid_dir = os.path.join(self.proc, "7")
        os.makedirs(self.pid_dir)
        self.w(self.pid_dir, "cgroup",
               "0::/" + self.rel + "/run-p1-i1.scope")
        # A PROVABLE GENERATION, because an observation with no bracket is
        # deliberately not a held one — without this every arm would exercise
        # the unbracketed path and none would exercise the ordinary one.
        # beacons reads field 22, which is index 19 after the ") ".
        self.w(self.pid_dir, "stat",
               "7 (helm) " + " ".join(["S"] + ["0"] * 18 + [str(self.START)]))

    def _arbitrary(self, case, rel, blind):
        self.root = tempfile.mkdtemp()
        case.addCleanup(shutil.rmtree, self.root, True)
        self.cg = os.path.join(self.root, "cgroup")
        self.rel = rel.strip("/")
        self.proc = os.path.join(self.root, "proc")
        self.pid_dir = os.path.join(self.proc, "7")
        os.makedirs(self.pid_dir)
        os.makedirs(self.cg, exist_ok=True)
        self.w(self.pid_dir, "cgroup", "0::" + rel)
        self.w(self.pid_dir, "stat",
               "7 (helm) " + " ".join(["S"] + ["0"] * 18 + [str(self.START)]))
        # THE ROOT CARRIES NO COUNTER INTERFACE, as on a real box.
        d = self.cg
        for part in [x for x in self.rel.split("/") if x]:
            d = os.path.join(d, part)
            os.makedirs(d, exist_ok=True)
            if part != blind:
                self.w(d, "memory.events.local", REAL_EVENTS.strip())
        self.scope = self.slice = d

    def w(self, d, name, text):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(str(text) + "\n")

    def events(self, d, high=0, local=None, **files):
        self.w(d, "memory.events",
               "low 0\nhigh %d\nmax 0\noom 0\noom_kill 0" % high)
        if local is not None:
            self.w(d, "memory.events.local",
                   "low 0\nhigh %d\nmax 0\noom 0\noom_kill 0" % local)
        for k, v in files.items():
            self.w(d, "memory." + k, v)

    #: The generation this fixture's /proc stat proves.
    START = 4242

    def obs(self, start=START, **kw):
        return seatceiling.observe(7, start=start, root=self.cg,
                                   proc=self.proc, **kw)


class FourAnswersNotTwoTest(unittest.TestCase):
    """Absence, unreadable and unlimited were one value once, and an
    unparseable ceiling read as healthy because of it."""

    def test_parse_size_separates_all_four(self):  # noqa: VACUOUS_ASSERTION — two unconditional positives run before the None checks on the same call: '12G' must return a real number and 'max' must return UNLIMITED
        self.assertEqual(seatceiling.parse_size("12G"), 12 * GB)
        self.assertIs(seatceiling.parse_size("max"), seatceiling.UNLIMITED)
        self.assertIs(seatceiling.parse_size(seatceiling.ABSENT),
                      seatceiling.ABSENT)
        for junk in ("", "garbage", "12Q", None):
            self.assertIsNone(seatceiling.parse_size(junk), junk)

    def test_parse_events_reads_a_REAL_file(self):
        got = seatceiling.parse_events(REAL_EVENTS)
        self.assertEqual(got["high"], 9548)
        self.assertEqual(got["oom_kill"], 0)

    def test_an_absent_or_unreadable_events_file_is_the_SAME_None(self):
        """DELIBERATE at this seam: a missing events file does not prove the
        controller is off — it is equally a denied read, a vanished level or
        an unsupported hierarchy — so both answer None and the caller must
        say UNKNOWN either way."""
        self.assertIsNone(seatceiling.parse_events(seatceiling.ABSENT))
        self.assertIsNone(seatceiling.parse_events(None))
        self.assertIsNone(seatceiling.parse_events("garbage\n"))
        # MUST-HIT on the same call: a real file DOES parse, so the Nones
        # above are about those inputs and not a parser that never works.
        self.assertTrue(seatceiling.parse_events(REAL_EVENTS))


class EveryLevelIsReportedTest(unittest.TestCase):

    def test_the_chain_is_leaf_first_and_marks_the_root(self):
        t = Tree(self)
        t.events(t.scope, high=1, local=1)
        t.events(t.slice, high=2, local=2)
        got = seatceiling.chain(7, t.cg, t.proc)
        self.assertEqual(os.path.basename(got[0].cgroup), "run-p1-i1.scope")
        self.assertTrue(got[-1].is_root, got[-1].cgroup)
        self.assertEqual(got[-1].cgroup, t.cg)
        self.assertEqual(sum(1 for l in got if l.is_root), 1)

    def test_BOTH_real_slice_namings_resolve(self):  # noqa: VACUOUS_ASSERTION — the control is the unconditional `seen` equality after the loop, which fails if the body never ran for either naming
        seen = []
        for name in (PID_SLICE, NAME_SLICE):
            t = Tree(self, slice_name=name)
            t.events(t.slice, high=5, local=5)
            hit = seatceiling.throttled_levels(t.obs())
            self.assertEqual([n for _l, n in hit], [5], name)
            seen.append(name)
        self.assertEqual(seen, [PID_SLICE, NAME_SLICE])


class LocalIsNotHierarchicalTest(unittest.TestCase):
    """A hierarchical count includes descendants and is NOT evidence of an
    event at that boundary. They are never summed and never substituted."""

    def test_a_hierarchical_count_alone_does_not_make_a_level_throttled(self):
        t = Tree(self)
        t.events(t.slice, high=900, local=0)     # all of it from below
        self.assertEqual(seatceiling.throttled_levels(t.obs()), [])
        # MUST-HIT on the same tree: raising the LOCAL count does report it,
        # so the empty result above is the local/hierarchical split working
        # and not a reader that never reports anything.
        t.events(t.slice, high=900, local=3)
        self.assertEqual([n for _l, n in seatceiling.throttled_levels(t.obs())],
                         [3])

    def test_counts_are_never_summed_across_levels(self):
        t = Tree(self)
        t.events(t.scope, high=4, local=4)
        t.events(t.slice, high=7, local=7)
        got = sorted(n for _l, n in seatceiling.throttled_levels(t.obs()))
        self.assertEqual(got, [4, 7], "a sum would have produced 11")


class UnobtainableIsNeverClearTest(unittest.TestCase):

    def test_a_level_with_no_events_is_unobtainable(self):
        t = Tree(self)
        t.events(t.scope, high=0, local=0)       # slice deliberately has none
        blind = seatceiling.unobtainable(t.obs())
        self.assertTrue(blind)
        self.assertNotIn(t.cg, [l.cgroup for l in blind])

    def test_the_ROOT_alone_is_exempt(self):
        """Controller files do not exist in the root by kernel design, so
        counting it would put one permanent warning on every healthy process.
        The exemption is the root and nothing else."""
        t = Tree(self)
        for d in (t.scope, t.slice, t.ancestor,
                  os.path.join(t.cg, "user.slice")):
            t.events(d, high=0, local=0)
        obs = t.obs()
        # SAME CALL: an empty chain would also produce an empty unobtainable
        # list, so the levels have to be there for the emptiness to mean the
        # exemption worked rather than that nothing was read.
        self.assertEqual(len(obs.levels), 5, [l.cgroup for l in obs.levels])
        self.assertTrue(any(l.is_root for l in obs.levels))
        self.assertEqual(seatceiling.unobtainable(obs), [])


class AttributionNeedsTheGenerationTest(unittest.TestCase):

    def test_a_generation_mismatch_REFUSES_to_attribute(self):
        t = Tree(self)
        t.events(t.slice, high=5, local=5)
        obs = t.obs(start=1)                     # not this process's start
        self.assertFalse(obs.bracket.held)
        self.assertEqual(obs.levels, ())
        self.assertIn("not the one the census bracketed", obs.bracket.why)

    def test_reading_with_NO_generation_says_so(self):
        t = Tree(self)
        t.events(t.slice, high=5, local=5)
        obs = t.obs(start=None)
        self.assertFalse(obs.bracket.held,
                         "a reading with no proven generation is not a held "
                         "bracket, however readable its levels are")
        self.assertIn("no census generation", obs.bracket.why)
        self.assertTrue(obs.levels, "the levels are still REPORTED — an "
                                    "unbracketed reading is a reading, it "
                                    "just cannot claim whose it is")


class TheSummaryNeverSaysHealthyTest(unittest.TestCase):

    def test_every_summary_says_the_current_state_is_unknown(self):  # noqa: VACUOUS_ASSERTION — the control for the in-loop assertions is the unconditional `seen` equality after the loop, which fails if the body never ran for either counter value
        t = Tree(self)
        seen = []
        for local in (0, 9548):
            t.events(t.slice, high=local, local=local)
            line = seatceiling.summary(t.obs())
            self.assertIn("UNKNOWN", line, line)
            self.assertNotIn("FINE", line)
            self.assertNotIn("healthy", line)
            seen.append(local)
        self.assertEqual(seen, [0, 9548], "the loop body never ran")

    def test_zero_is_reported_as_the_COUNTER_READING_zero(self):
        """Scope, accounting semantics and object continuity all bound what a
        zero proves, and none of them are visible here — so it may not be
        rendered as 'never throttled'."""
        t = Tree(self)
        t.events(t.slice, high=0, local=0)
        t.events(t.scope, high=0, local=0)
        t.events(t.ancestor, high=0, local=0)
        t.events(os.path.join(t.cg, "user.slice"), high=0, local=0)
        line = seatceiling.summary(t.obs())
        self.assertIn("counters read 0", line)
        self.assertNotIn("never", line.lower())

    def test_an_unattributed_observation_reports_UNKNOWN_and_why(self):
        t = Tree(self)
        t.events(t.slice, high=5, local=5)
        line = seatceiling.summary(t.obs(start=1))
        self.assertIn("UNKNOWN", line)
        self.assertIn("bracketed", line)


if __name__ == "__main__":
    unittest.main()


class OnlyTheUnifiedLineNamesAV2PathTest(unittest.TestCase):
    """A v1 line carries an absolute path too, and taking the first one walked
    it under the v2 mount — reporting some unrelated cgroup's counters as this
    pid's. The module header always said an unsupported hierarchy is UNKNOWN;
    these arms are that sentence becoming true."""

    def test_a_v1_only_body_is_unknown_rather_than_a_walk(self):  # noqa: VACUOUS_ASSERTION — the control is the unconditional assertIsNotNone on the SAME tree before the body is swapped: an unresolvable fixture would fail there, so the None below is about the v1 line and nothing else
        t = Tree(self)
        t.events(t.scope, high=3, local=3)
        self.assertIsNotNone(seatceiling.chain(7, root=t.cg, proc=t.proc),
                             "control: the unified body must resolve, or this "
                             "arm proves nothing about the v1 body")
        t.w(t.pid_dir, "cgroup", "9:memory:/" + t.rel + "/run-p1-i1.scope")
        self.assertIsNone(seatceiling.chain(7, root=t.cg, proc=t.proc))

    def test_a_hybrid_body_follows_the_unified_line_not_the_first_one(self):  # noqa: VACUOUS_ASSERTION — the control is the unconditional assertEqual on levels[0] from the same call: the decoy's absence is only meaningful because the unified leaf is positively present
        t = Tree(self)
        decoy = os.path.join(t.cg, "decoy.slice", "run-p9-i9.scope")
        os.makedirs(decoy)
        t.events(decoy, high=77, local=77)
        t.events(t.scope, high=3, local=3)
        t.w(t.pid_dir, "cgroup",
            "9:memory:/decoy.slice/run-p9-i9.scope\n"
            "0::/" + t.rel + "/run-p1-i1.scope")
        levels = seatceiling.chain(7, root=t.cg, proc=t.proc)
        self.assertEqual(levels[0].cgroup, t.scope)
        self.assertNotIn(decoy, [l.cgroup for l in levels])

    def test_an_absolute_path_on_a_numbered_hierarchy_is_not_unified(self):
        self.assertEqual(seatceiling._unified("0::/a/b"), "/a/b")
        self.assertIsNone(seatceiling._unified("1:name=systemd:/a/b"))
        self.assertIsNone(seatceiling._unified("0:memory:/a/b"))


class TheDoctorCheckOwnsItsInputTest(unittest.TestCase):
    """The registered check reads a LIVE census, so until its input is
    injectable every arm about it is really an arm about this box. These
    supply the census, the cgroup tree AND the /proc beneath it, so every case
    is determinate on any machine — a root parameter alone still leaves the
    fixture taking the path and the generation from the host that ran it.
    """

    def check(self, **kw):
        from helm import doctor
        return doctor.check_seat_memory_ceilings(**kw)

    def world(self, rel="/a.slice/b.scope", blind=None):
        """A whole world for pid 7: its /proc entry and its cgroup tree."""
        t = Tree(self, rel=rel, blind=blind)
        return t

    #: so `start=None` can mean a genuinely ABSENT bracket rather than
    #: "use the default" — the two are different rows and different answers.
    DEFAULT = object()

    def census(self, start=DEFAULT, name=""):
        return {"rows": [{"pid": 7,
                          "start": Tree.START if start is self.DEFAULT
                                   else start,
                          "environ": {"HELM_CHAT_NAME": name} if name else {}}]}

    def test_a_failed_enumeration_warns_and_an_empty_box_does_not(self):
        from helm import doctor
        rows = self.check(census={"listing_failed": True, "rows": []})
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("not a proven-empty estate", " ".join(m for _, m in rows))
        rows = self.check(census={"listing_failed": False, "rows": []})
        self.assertEqual([lvl for lvl, _ in rows], [doctor.OK])

    def test_an_uncertifiable_census_is_not_an_empty_estate(self):
        from helm import doctor
        self.assertEqual(
            [lvl for lvl, _ in self.check(
                census={"rows": [], "census_partial": True})],
            [doctor.WARN])

    def test_the_root_and_proc_arguments_reach_the_observation(self):
        from helm import doctor
        t = self.world()
        rows = self.check(census=self.census(name="seat-under-test"),
                          root=t.cg, proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.OK],
                         "control: a world that answers must be observable, "
                         "or the WARN below proves nothing")
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        rows = self.check(census=self.census(name="seat-under-test"),
                          root=empty, proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("seat-under-test", " ".join(m for _, m in rows))

    def test_a_process_in_the_cgroup_ROOT_is_observable(self):
        from helm import doctor
        t = self.world(rel="/")
        self.assertEqual([lvl for lvl, _ in self.check(
            census=self.census(), root=t.cg, proc=t.proc)], [doctor.OK])

    def test_a_level_with_no_counter_interface_is_not_observable(self):
        from helm import doctor
        t = self.world(blind="a.slice")
        rows = self.check(census=self.census(), root=t.cg, proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("did not answer", " ".join(m for _, m in rows))

    def test_a_generation_mismatch_is_reported_as_unobservable(self):
        from helm import doctor
        t = self.world()
        self.assertEqual(
            [lvl for lvl, _ in self.check(census=self.census(), root=t.cg,
                                          proc=t.proc)],
            [doctor.OK], "control: the TRUE generation must be observable")
        rows = self.check(census=self.census(start=Tree.START + 1),
                          root=t.cg, proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("bracketed", " ".join(m for _, m in rows))

    def test_a_row_with_no_proven_generation_is_not_an_observation(self):
        """The census can hand out a row whose bracket could not be read.
        `observe` allows a missing bracket and says so in `why` instead of
        refusing, so branching on the levels alone lets such a row earn a
        positive answer."""
        from helm import doctor
        t = self.world()
        self.assertEqual(
            [lvl for lvl, _ in self.check(census=self.census(), root=t.cg,
                                          proc=t.proc)],
            [doctor.OK], "control: the proven generation must be observable")
        rows = self.check(census=self.census(start=None), root=t.cg,
                          proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.WARN])
        self.assertIn("no census generation", " ".join(m for _, m in rows))

    def test_a_PARTIAL_census_qualifies_a_good_row_too(self):
        """The producer says a partial population is a FLOOR, not a certified
        total. Reading that flag only in the empty branch let a seat that WAS
        observed answer for a population nobody could count."""
        from helm import doctor
        t = self.world()
        rows = self.check(census=self.census(), root=t.cg, proc=t.proc)
        self.assertEqual([lvl for lvl, _ in rows], [doctor.OK],
                         "control: a certified census gives a bare OK")
        partial = dict(self.census(), census_partial=True)
        rows = self.check(census=partial, root=t.cg, proc=t.proc)
        levels = [lvl for lvl, _ in rows]
        text = " ".join(m for _, m in rows)
        self.assertIn(doctor.WARN, levels)
        self.assertIn("FLOOR", text)
        self.assertIn(doctor.OK, levels,
                      "and the seats that WERE observed are still reported — "
                      "the qualifier is about the population, not about them")

    def test_the_census_qualifier_is_said_ONCE(self):
        """One census-wide line, not a repetition on every healthy row."""
        from helm import doctor
        t = self.world()
        partial = dict(self.census(), census_partial=True)
        rows = self.check(census=partial, root=t.cg, proc=t.proc)
        self.assertEqual(sum(1 for _, m in rows if "FLOOR" in m), 1)

    def test_a_hostile_seat_name_is_laundered_before_it_is_printed(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        t = self.world()
        text = " ".join(m for _, m in self.check(
            census=self.census(name="a\x1b[2Jb"), root=empty, proc=t.proc))
        self.assertIn("seat a", text)
        self.assertNotIn("\x1b", text)


class AVanishedLevelIsAnAnswerNotAnOmissionTest(unittest.TestCase):
    """THE DEFECT: the walk checks each level's directory independently and
    SKIPS the ones that are gone, so a level that disappeared between the
    lookup and the read is absent from the result rather than reported as
    unobservable. `unobtainable` can only flag Level objects that EXIST, so it
    cannot see an omission — the observation comes back looking complete about
    a chain it did not finish reading.

    A cgroup can go away under a live process: the process migrates, or the
    unit is stopped and its directory removed while /proc still names it.
    """

    def _vanish_on_second_look(self, path, seen):
        """os.path.isdir that removes `path` the FIRST time it is asked.

        Removing it AT the read is the race, expressed as a door rather than a
        sleep: /proc named this level a moment ago and it is gone now. This
        arm was written against the earlier shape, which asked twice — the
        cure collapsed the precheck into the single per-level read, so the
        arm's mechanism had to follow its subject. The PROPERTY it asserts did
        not change.
        """
        real = os.path.isdir

        def door(p):
            seen.append(p)
            if p == path and seen.count(path) == 1 and real(p):
                for name in os.listdir(p):
                    os.remove(os.path.join(p, name))
                os.rmdir(p)
            return real(p)
        return door

    def test_a_level_that_vanishes_between_lookup_and_read_is_reported(self):  # noqa: VACUOUS_ASSERTION — three unconditional positive controls run before the absence assertions: the intact chain must report the leaf, the door must have been asked about it, and the leaf must actually be gone
        t = Tree(self)
        t.events(t.scope, high=3, local=3)
        t.events(t.slice, high=5, local=5)
        t.events(t.ancestor, high=7, local=7)

        intact = seatceiling.chain(7, root=t.cg, proc=t.proc)
        self.assertIn(t.scope, [l.cgroup for l in intact],
                      "control: the intact tree must report the leaf, or this "
                      "arm proves nothing about the vanished one")

        seen = []
        with mock.patch.object(os.path, "isdir",
                               self._vanish_on_second_look(t.scope, seen)):
            obs = seatceiling.observe(7, root=t.cg, proc=t.proc)
        self.assertIn(t.scope, seen, "control: the door must have been asked "
                                     "about the leaf, or nothing was raced")
        self.assertFalse(os.path.isdir(t.scope),
                         "control: the leaf must actually be gone")

        paths = [l.cgroup for l in obs.levels]
        self.assertIn(t.scope, paths,
                      "A LEVEL THAT VANISHED MID-WALK MUST BE REPRESENTED. "
                      "Omitting it makes an incomplete chain read complete, "
                      "because unobtainable() can only flag levels present in "
                      "the result.")
        self.assertIn(t.scope, [l.cgroup for l in seatceiling.unobtainable(obs)],
                      "and it must be UNOBTAINABLE, not a level that answered")


class TheBracketHasTwoEndsTest(unittest.TestCase):
    """Checking the generation and then reading is a PREFIX, not a bracket.
    The pid can be reused, or the process can migrate, in the window between
    the check and the read — and a migrating process keeps its generation, so
    the cgroup line has to be read at both ends too.

    Neither end proves coherence; see `acquisition_complete` for the ceiling. What
    they do is catch a change that DID happen, which the earlier shape could
    not see at all.
    """

    def test_a_generation_that_changes_DURING_the_read_is_caught(self):
        from helm import beacons
        t = Tree(self)
        t.events(t.scope, high=3, local=3)
        self.assertTrue(t.obs().bracket.held,
                        "control: an unchanging generation must hold, or the "
                        "refusal below is not about the change")
        answers = iter([True, False])
        with mock.patch.object(beacons, "pid_alive",
                               lambda *a, **k: next(answers)):
            obs = t.obs()
        self.assertFalse(obs.bracket.held)
        self.assertIn("DURING the read", obs.bracket.why)
        self.assertTrue(obs.levels, "the levels are still REPORTED — what the "
                                    "bracket withdraws is whose they are")

    def test_a_process_that_MOVES_during_the_read_is_caught(self):
        t = Tree(self)
        t.events(t.scope, high=3, local=3)
        os.makedirs(os.path.join(t.cg, "other.slice"))
        self.assertTrue(t.obs().bracket.held,
                        "control: a process that stays put must hold")
        real, seen = seatceiling._read, []

        def moving(path):
            if path.endswith(os.path.join("7", "cgroup")):
                seen.append(path)
                if len(seen) > 1:
                    return "0::/other.slice\n"
            return real(path)
        with mock.patch.object(seatceiling, "_read", moving):
            obs = t.obs()
        self.assertGreater(len(seen), 1,
                           "control: the cgroup line must be read at BOTH "
                           "ends, or there is no second end to disagree")
        self.assertFalse(obs.bracket.held)
        self.assertIn("moved between cgroups", obs.bracket.why)


# ---------------------------------------------------------------------------
# the present tense — THROTTLED and NEAR, read now
# ---------------------------------------------------------------------------

AGENTS = "user.slice/user-1000.slice/user@1000.service/agents.slice"
MB = 1024 * 1024
#: A cumulative high count of the size a real seat slice carries for its whole
#: lifetime after one bad hour. Large on purpose: the arm is that its SIZE
#: says nothing about now.
OLD_COUNT = 1531339


class Fleet(object):
    """A cgroup tree shaped like a real user manager's agents.slice, and a
    /proc whose pids name their slices through their own cgroup lines — the
    two inputs seatceiling.fleet_pressure discovers everything from."""

    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix="helm-seatceiling-now-")
        case.addCleanup(shutil.rmtree, self.root, True)
        self.cg = os.path.join(self.root, "cgroup")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(self.proc)

    @staticmethod
    def w(d, name, text):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(str(text))

    def seat(self, name, pid, current, high, count=0, state="S", wchan="0",
             shmem=None):
        """`shmem` defaults to ALL of `current` — the tmpfs-clone shape this
        plane exists for; a page-cache slice names its small shmem."""
        rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name(name))
        d = os.path.join(self.cg, rel)
        os.makedirs(os.path.join(d, "run-p%d-i1.scope" % pid), exist_ok=True)
        self.w(d, "memory.current", "%d\n" % current)
        self.w(d, "memory.high", "%d\n" % high)
        shared = current if shmem is None else shmem
        self.w(d, "memory.stat", "anon 4096\nfile %d\nkernel 0\nshmem %d\n"
                                 % (max(0, current - shared), shared))
        self.events(d, count)
        pd = os.path.join(self.proc, str(pid))
        os.makedirs(pd, exist_ok=True)
        self.w(pd, "cgroup", "0::/%s/run-p%d-i1.scope\n" % (rel, pid))
        self.w(pd, "stat", "%d (claude) %s 1 0 0 0 0\n" % (pid, state))
        self.w(pd, "wchan", wchan)
        return d

    def events(self, d, count):
        self.w(d, "memory.events",
               "low 0\nhigh %d\nmax 0\noom 0\noom_kill 0\n" % count)

    def read(self, during=None):
        """({slice basename: Pressure}, [sleeps]) — `during` runs INSIDE the
        sample window, which is the only place a present-tense rise lives."""
        slept = []

        def sleep(s):
            slept.append(s)
            if during:
                during()
        got, trouble = seatceiling.fleet_pressure(root=self.cg,
                                                  proc=self.proc, sleep=sleep)
        assert trouble is None, trouble
        return {os.path.basename(p): r for p, r in got.items()}, slept


class PresentTenseTest(unittest.TestCase):
    """IS IT THROTTLED NOW. The counted half of this module refuses that
    question on purpose; this half answers it, and only from reads that are
    present-tense: a stalled process, or a counter RISING inside a window the
    reader sampled. A large counter that does not move is history."""

    SLICE = seatceiling.seat_slice_name("seat-under-test")

    def test_a_large_still_counter_is_history_not_throttling_now(self):
        """THE INVERSION THIS HALF MUST NOT MAKE. A slice at 85% — high enough
        that the rise IS sampled — carrying a lifetime counter in the millions
        that does not move across the window reads NOTHING. The positive
        control is the same slice, same reader, with the counter moved inside
        the window: THROTTLED."""
        f = Fleet(self)
        d = f.seat("seat-under-test", 41, current=85 * MB, high=100 * MB,
                   count=OLD_COUNT)
        still, slept = f.read()
        self.assertEqual(slept, [seatceiling.RISE_WINDOW_S],
                         "control: at 85% the rise must actually be sampled")
        self.assertEqual(still[self.SLICE].rise, 0)
        self.assertIsNone(still[self.SLICE].word,
                          "a counter that did not move was read as a throttle "
                          "happening now")
        rising, _slept = f.read(during=lambda: f.events(d, OLD_COUNT + 812))
        self.assertEqual(rising[self.SLICE].word, seatceiling.THROTTLED)
        self.assertEqual(rising[self.SLICE].rise, 812)

    def test_a_calm_slice_pays_no_sleep_whatever_its_counter_says(self):
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=50 * MB, high=100 * MB,
               count=OLD_COUNT)
        calm, slept = f.read()
        self.assertEqual(slept, [], "a slice under the clear line was "
                                    "sampled — every stop would pay a sleep")
        self.assertIsNone(calm[self.SLICE].word)
        f.seat("seat-under-test", 41, current=95 * MB, high=100 * MB,
               count=OLD_COUNT)
        self.assertEqual(f.read()[0][self.SLICE].word, seatceiling.NEAR,
                         "control: the same reader names a slice at 95% NEAR")

    def test_a_process_stalled_in_the_over_high_throttle_is_throttled_now(self):
        """D on the over-high wchan is conclusive and needs no window — it also
        catches an ANCESTOR's ceiling, where the seat's own ratio is low."""
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=60 * MB, high=100 * MB,
               state="D", wchan="__mem_cgroup_handle_over_high")
        got, slept = f.read()
        self.assertEqual(got[self.SLICE].word, seatceiling.THROTTLED)
        self.assertEqual(got[self.SLICE].stalled, (41,))
        self.assertEqual(slept, [], "a proven stall does not need a sample")
        f.seat("seat-under-test", 41, current=60 * MB, high=100 * MB,
               state="D", wchan="folio_wait_bit_common")
        other = f.read()[0][self.SLICE]
        self.assertEqual(other.stalled, (), "D on some other wait is not the "
                                            "memory throttle")
        self.assertIsNone(other.word)

    def test_a_page_cache_slice_is_HIGH_and_not_pressing(self):
        """NEAR IS UNRECLAIMABLE MEMORY NEAR THE CEILING, NOT ANY MEMORY. A
        slice at 94% that is almost all page cache is not in danger — the
        kernel reclaims cache on its own, and a seat that sits in that band
        routinely would page two seats for nothing. It reads HIGH: a quiet
        badge, no reap, no wake. The positive control is the same slice with
        its memory held as shmem, which is what froze a seat: NEAR."""
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=94 * MB, high=100 * MB,
               shmem=2 * MB)
        cache = f.read()[0][self.SLICE]
        self.assertEqual(cache.word, seatceiling.HIGH)
        self.assertFalse(seatceiling.pressing(cache))
        self.assertEqual(cache.shmem, 2 * MB)
        cells = seatceiling.pressure_cells(cache)
        self.assertEqual(cells["mem_pressure"], seatceiling.HIGH)
        self.assertIn("cache", cells["mem_pressure_mark"])
        self.assertNotIn("\u26a0", cells["mem_pressure_mark"])
        f.seat("seat-under-test", 41, current=94 * MB, high=100 * MB,
               shmem=30 * MB)
        held = f.read()[0][self.SLICE]
        self.assertEqual(held.word, seatceiling.NEAR)
        self.assertTrue(seatceiling.pressing(held))

    def test_an_unreadable_shmem_is_its_own_state_never_plain_HIGH(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-tuple, so every subtest executes; the unconditional control is the readable reading of the SAME slice through the SAME reader, asserted HIGH with its bytes before the loop
        """UNREADABLE AND NEGATIVE MUST NOT SHARE A VALUE. At 94% with no
        memory.stat to read, helm cannot tell page cache from tmpfs. The SAFE
        ACTION stays — not pressing, so no reap and no wake — but the reading
        says what it is: its own word, a mark naming the unreadable file, and
        a published shmem of null, never the 0 or the "mostly cache" a
        measured HIGH carries. The positive control is the same slice with a
        readable, small shmem: plain HIGH, its bytes published."""
        import json
        f = Fleet(self)
        d = f.seat("seat-under-test", 41, current=94 * MB, high=100 * MB,
                   shmem=2 * MB)
        known = f.read()[0][self.SLICE]
        self.assertEqual(known.word, seatceiling.HIGH)
        self.assertEqual(seatceiling.pressure_cells(known)["mem_shmem"],
                         2 * MB)
        for broken in ("absent", "garbled"):
            with self.subTest(memory_stat=broken):
                stat_file = os.path.join(d, "memory.stat")
                if os.path.exists(stat_file):
                    os.remove(stat_file)
                if broken == "garbled":
                    f.w(d, "memory.stat", "anon 4096\nshmem lots\n")
                blind = f.read()[0][self.SLICE]
                self.assertEqual(blind.word, seatceiling.HIGH_UNREAD)
                self.assertNotEqual(blind.word, seatceiling.HIGH)
                self.assertIsNone(blind.shmem)
                self.assertFalse(seatceiling.pressing(blind))
                cells = seatceiling.pressure_cells(blind)
                self.assertIn("unreadable", cells["mem_pressure_mark"])
                self.assertIn("94% of memory.high", cells["mem_pressure_mark"])
                self.assertNotIn("mostly reclaimable", cells["mem_pressure_mark"])
                self.assertIn('"mem_shmem": null', json.dumps(cells))

    def test_a_spell_ends_only_under_the_clear_line(self):
        """HYSTERESIS: a slice that dips from NEAR to 85% has not recovered —
        it is one spell, not two wakes."""
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=85 * MB, high=100 * MB)
        hovering = f.read()[0][self.SLICE]
        self.assertIsNone(hovering.word)
        self.assertFalse(seatceiling.cleared(hovering))
        f.seat("seat-under-test", 41, current=50 * MB, high=100 * MB)
        self.assertTrue(seatceiling.cleared(f.read()[0][self.SLICE]))

    def test_the_slice_is_discovered_from_each_process_and_named_as_launched(self):
        self.assertEqual(seatceiling.seat_slice_name("seat-under-test"),
                         "agents-seat_under_test.slice")
        self.assertEqual(seatceiling.seat_slice_of(
            "/%s/agents-a.slice/agents-a-b.slice/run-p1-i1.scope" % AGENTS),
            "/%s/agents-a.slice/agents-a-b.slice" % AGENTS)
        self.assertIsNone(seatceiling.seat_slice_of("/user.slice/x.scope"))
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=MB, high=100 * MB)
        f.seat("seat-b", 42, current=MB, high=100 * MB)
        self.assertEqual(sorted(f.read()[0]),
                         sorted([self.SLICE,
                                 seatceiling.seat_slice_name("seat-b")]))

    def test_the_cells_publish_the_line_they_were_read_from(self):
        f = Fleet(self)
        f.seat("seat-under-test", 41, current=104 * MB, high=100 * MB,
               state="D", wchan="__mem_cgroup_handle_over_high")
        p = f.read()[0][self.SLICE]
        cells = seatceiling.pressure_cells(p)
        self.assertEqual(cells["mem_pressure"], seatceiling.THROTTLED)
        self.assertEqual(cells["mem_pressure_text"],
                         seatceiling.pressure_line(p))
        self.assertIn("104%", cells["mem_pressure_text"])
        self.assertIn("MEMORY THROTTLED", cells["mem_pressure_mark"])
        f.seat("seat-under-test", 41, current=10 * MB, high=100 * MB)
        self.assertEqual(seatceiling.pressure_cells(f.read()[0][self.SLICE]),
                         {}, "a calm slice publishes nothing")

    def test_a_slice_whose_ceiling_files_will_not_read_publishes_UNKNOWN(self):
        """The empty cell is a calm slice's. A slice whose memory.current or
        memory.high will not read has no share of its ceiling at all, so it
        publishes UNKNOWN naming the file — while the READING's own word stays
        None, so nothing reaps or wakes on it. The controls are the same slice
        readable (calm, empty) and under no ceiling (`max` is an answer)."""
        import json
        f = Fleet(self)
        d = f.seat("seat-under-test", 41, current=10 * MB, high=100 * MB)
        calm = f.read()[0][self.SLICE]
        self.assertEqual((calm.current, calm.high), (10 * MB, 100 * MB))
        self.assertEqual(seatceiling.pressure_cells(calm), {},
                         "control: a readable calm slice publishes nothing")
        f.w(d, "memory.high", "max\n")
        open_ended = f.read()[0][self.SLICE]
        self.assertEqual(open_ended.high, seatceiling.UNLIMITED)
        self.assertEqual(seatceiling.pressure_cells(open_ended), {},
                         "no ceiling is an answer, not an unread file")
        f.w(d, "memory.high", "%d\n" % (100 * MB))
        f.w(d, "memory.current", "lots\n")
        blind = f.read()[0][self.SLICE]
        self.assertIsNone(blind.word, "the reading's word is not the cell's")
        self.assertFalse(seatceiling.pressing(blind))
        cells = seatceiling.pressure_cells(blind)
        self.assertEqual(cells.get("mem_pressure"), seatceiling.UNKNOWN)
        self.assertIn("memory.current would not read", cells["mem_pressure_mark"])
        self.assertIn(self.SLICE, cells["mem_pressure_text"])
        self.assertIn('"mem_shmem": null', json.dumps(cells))
        f.w(d, "memory.current", "%d\n" % (10 * MB))
        f.w(d, "memory.high", "lots\n")
        cells = seatceiling.pressure_cells(f.read()[0][self.SLICE])
        self.assertEqual(cells.get("mem_pressure"), seatceiling.UNKNOWN)
        self.assertIn("memory.high would not read", cells["mem_pressure_mark"])


class CensusRowShowsTheThrottleTest(unittest.TestCase):
    """`helm chat seats` read a throttled seat `fresh`: the process, pane,
    beacon and presence beat all survive a memory throttle. The seat's own
    row must say it, from the same reading the web badge renders."""

    def setUp(self):
        import json
        from helm import seats
        self.tmp = tempfile.mkdtemp(prefix="helm-census-throttle-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._prior = {k: os.environ.get(k)
                       for k in ("HELM_HOME", "HELM_CHAT_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        self.addCleanup(self._restore)
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(seats.roster_path(), "w", encoding="utf-8") as fh:
            json.dump({"seat-under-test": {"session": "m" * 36, "cwd": self.tmp,
                                       "home_room": "helm"},
                       "seat-c": {"session": "c" * 36, "cwd": self.tmp,
                                     "home_room": "helm"},
                       # a roster seat with NO slice in the fleet below: the
                       # one whose empty cell is correct when the read answers
                       "seat-n": {"session": "n" * 36, "cwd": self.tmp,
                                  "home_room": "helm"}}, fh)
        self.fleet = Fleet(self)
        self.fleet.seat("seat-under-test", 41, current=104 * MB, high=100 * MB,
                        state="D", wchan="__mem_cgroup_handle_over_high")
        self.fleet.seat("seat-c", 42, current=10 * MB, high=100 * MB)

    def _restore(self):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_throttled_seats_row_says_THROTTLED_and_the_calm_one_does_not(self):
        import io
        from contextlib import redirect_stdout
        from helm import seats_report
        readings = self.fleet.read()[0]
        buf = io.StringIO()
        with mock.patch.object(seats_report, "_mem_readings",
                               return_value=readings), redirect_stdout(buf):
            seats_report.render_roster("helm", True)
        rows = {line.split()[1]: line for line in buf.getvalue().splitlines()
                if line.strip() and len(line.split()) > 1}
        self.assertIn("MEMORY THROTTLED (104% of memory.high)",
                      rows.get("seat-under-test", ""), buf.getvalue())
        self.assertIn("seat-c", rows, "control: the calm seat has a row")
        self.assertNotIn("MEMORY", rows["seat-c"])
        rep = seats_report.roster_report("helm", pressure=readings)
        by = {s["seat"]: s for s in rep["seats"]}
        self.assertEqual(by["seat-under-test"]["mem_pressure"],
                         seatceiling.THROTTLED)
        self.assertEqual(by["seat-under-test"]["mem_pressure_text"],
                         seatceiling.pressure_line(
                             readings[seatceiling.seat_slice_name(
                                 "seat-under-test")]),
                         "the published sentence was clipped or reworded")
        self.assertNotIn("mem_pressure", by["seat-c"])

    # ── A READ THAT DID NOT ANSWER IS UNKNOWN, NEVER THE EMPTY CELL ─────────
    # The empty cell is what a calm seat, or a seat with no slice, publishes.
    # A raise or an incomplete walk folded into that same {} renders a
    # structural failure of the read as a calm fleet, so each one must reach
    # every row as UNKNOWN. These arms own the reader: fleet_pressure is
    # patched in every one of them, and no arm reads live cgroups or /proc.

    SEATS = ("seat-under-test", "seat-c", "seat-n")

    def _screen_and_payload(self):
        """The `helm chat seats` rows and the /api/chat/roster payload, each
        from a report that takes its OWN memory reading (no `pressure`)."""
        import io
        import json
        from contextlib import redirect_stdout
        from helm import seats_report
        buf = io.StringIO()
        with redirect_stdout(buf):
            seats_report.render_roster("helm", True)
        rows = {line.split()[1]: line for line in buf.getvalue().splitlines()
                if line.strip() and len(line.split()) > 1}
        rep = seats_report.roster_report("helm")
        return (rows, {s["seat"]: s for s in rep["seats"]},
                json.dumps(rep, default=str))

    def _every_seat_reads_unknown(self, why):
        from helm import seats_report
        rows, by, payload = self._screen_and_payload()
        self.assertEqual(sorted(by), sorted(self.SEATS),
                         "control: the report lists every roster seat")
        cells = [by[seat] for seat in self.SEATS]
        self.assertEqual([c.get("mem_pressure") for c in cells],
                         [seats_report.MEM_UNKNOWN] * len(self.SEATS),
                         "an unread seat published the calm (empty) cell")
        self.assertEqual([why in (c.get("mem_pressure_text") or "")
                          for c in cells], [True] * len(self.SEATS))
        self.assertEqual([c.get("mem_shmem", "missing") for c in cells],
                         [None] * len(self.SEATS))
        self.assertEqual(payload.count('"mem_pressure": "UNKNOWN"'),
                         len(self.SEATS), payload)
        self.assertEqual([("memory UNKNOWN (" in rows.get(seat, ""))
                          and (why in rows.get(seat, ""))
                          for seat in self.SEATS],
                         [True] * len(self.SEATS),
                         "every `helm chat seats` row must say UNKNOWN")
        return rows, by, payload

    def _raise_reads_unknown_and_leaves_a_breadcrumb(self, exc):
        """(by, the newest breadcrumb) for a fleet_pressure that raises exc."""
        from helm import record
        with mock.patch.object(seatceiling, "fleet_pressure",
                               side_effect=exc) as fp:
            _rows, by, _payload = self._every_seat_reads_unknown(
                "the seat-memory read raised %s" % type(exc).__name__)
        self.assertEqual(fp.call_count, 2, "one reading per report")
        crumbs = [(c.get("exc"), c.get("msg")) for c in record.swallows()
                  if c.get("where") == "seats_report._mem_readings"]
        return by, crumbs[:1]

    def test_a_reading_that_raises_ImportError_is_UNKNOWN_on_every_seat(self):
        by, crumb = self._raise_reads_unknown_and_leaves_a_breadcrumb(
            ImportError("seatceiling half-imported"))
        self.assertEqual(by["seat-n"]["mem_pressure"], "UNKNOWN")
        self.assertEqual(crumb, [("ImportError", "seatceiling half-imported")],
                         "the swallowed exception left no breadcrumb")

    def test_a_reading_that_raises_RuntimeError_is_UNKNOWN_on_every_seat(self):
        by, crumb = self._raise_reads_unknown_and_leaves_a_breadcrumb(
            RuntimeError("the membership walk blew up"))
        self.assertEqual(by["seat-c"]["mem_pressure"], "UNKNOWN")
        self.assertEqual(crumb, [("RuntimeError", "the membership walk blew up")],
                         "the swallowed exception left no breadcrumb")

    def test_a_seatceiling_that_will_not_import_is_UNKNOWN_on_every_seat(self):
        """The structural failure itself, not a stand-in: the module is gone
        from the import system, so `from . import seatceiling` raises."""
        import sys
        import helm
        saved = sys.modules["helm.seatceiling"]

        def restore():
            sys.modules["helm.seatceiling"] = saved
            helm.seatceiling = saved
        self.addCleanup(restore)
        sys.modules["helm.seatceiling"] = None
        del helm.seatceiling
        try:
            _rows, by, _payload = self._every_seat_reads_unknown(
                "the seat-memory read raised ModuleNotFoundError")
        finally:
            restore()
        self.assertEqual(by["seat-under-test"]["mem_pressure"], "UNKNOWN",
                         "the throttled seat's cell must not survive a read "
                         "that never happened")

    def test_a_walk_that_did_not_complete_is_UNKNOWN_with_its_reason(self):
        """fleet_pressure's OWN refusal — ({}, trouble) when the process table
        will not list — carries its reason to every row. Its {} is not a calm
        fleet."""
        real = seatceiling.fleet_pressure
        gone = os.path.join(self.tmp, "no-such-proc")
        with mock.patch.object(seatceiling, "fleet_pressure",
                               side_effect=lambda: real(root=self.fleet.cg,
                                                        proc=gone)):
            rows, _by, _payload = self._every_seat_reads_unknown(
                "the process table could not be listed (FileNotFoundError)")
        self.assertIn("? memory UNKNOWN (the process table could not be "
                      "listed (FileNotFoundError))", rows["seat-n"])

    def test_a_reading_that_answered_leaves_a_seat_with_no_slice_ABSENT(self):  # noqa: VACUOUS_ASSERTION — every absence here is on a report whose seat list is asserted complete (sorted(by) == SEATS) and, for the fleet read, whose throttled seat is asserted THROTTLED in by, payload and rows
        """THE POSITIVE CONTROL on the same observable: when the read answers,
        a seat with no slice and a calm seat keep the empty cell, and the
        throttled one still says THROTTLED."""
        real = seatceiling.fleet_pressure
        with mock.patch.object(
                seatceiling, "fleet_pressure",
                side_effect=lambda: real(root=self.fleet.cg,
                                         proc=self.fleet.proc,
                                         sleep=lambda s: None)):
            rows, by, payload = self._screen_and_payload()
        self.assertEqual(by["seat-under-test"].get("mem_pressure"),
                         seatceiling.THROTTLED)
        self.assertIn('"mem_pressure": "THROTTLED"', payload)
        self.assertIn("MEMORY THROTTLED", rows["seat-under-test"])
        self.assertEqual(sorted(by), sorted(self.SEATS))
        self.assertEqual([k for k in by["seat-n"] if k.startswith("mem_")], [])
        self.assertEqual([k for k in by["seat-c"] if k.startswith("mem_")], [])
        self.assertNotIn('"mem_pressure": "UNKNOWN"', payload)
        self.assertEqual(["memory UNKNOWN" in rows.get(s, "memory UNKNOWN")
                          for s in ("seat-c", "seat-n")], [False, False])
        with mock.patch.object(seatceiling, "fleet_pressure",
                               return_value=({}, None)):
            _rows, by, payload = self._screen_and_payload()
        self.assertEqual(sorted(by), sorted(self.SEATS))
        self.assertEqual([k for s in self.SEATS for k in by[s]
                          if k.startswith("mem_")], [],
                         "an answered, empty fleet read is calm, not UNKNOWN")

    def test_a_cell_that_raises_is_UNKNOWN_for_its_own_seat_only(self):
        from helm import record, seats_report
        broken = {seatceiling.seat_slice_name("seat-c"): object()}
        rep = seats_report.roster_report("helm", pressure=broken)
        by = {s["seat"]: s for s in rep["seats"]}
        self.assertEqual(by["seat-c"].get("mem_pressure"),
                         seats_report.MEM_UNKNOWN)
        self.assertIn("the seat-memory cell raised AttributeError",
                      by["seat-c"]["mem_pressure_mark"])
        self.assertNotIn("mem_pressure", by["seat-n"],
                         "a seat the reading does not name is not UNKNOWN")
        self.assertEqual([c["exc"] for c in record.swallows()
                          if c.get("where") == "seats_report._mem_cells"][:1],
                         ["AttributeError"])

    def test_the_report_and_seatceiling_spell_UNKNOWN_the_same(self):
        from helm import seats_report
        self.assertEqual(seats_report.MEM_UNKNOWN, seatceiling.UNKNOWN)
