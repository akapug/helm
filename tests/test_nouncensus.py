#!/usr/bin/env python3
"""The per-site uses-closure census (task/1300 slice 2).

WHY EVERY NEGATIVE HERE CARRIES A PAIRED POSITIVE, and why the ambiguity arm
is the one that matters: the first cut of this module computed ambiguity from
SEED OVERLAP — a token listed under two nouns — and no token is, so the state
was unreachable by construction while the docstring announced it. A suite that
only asserted "no false ambiguity" would have scored full marks on a dead
state. So each arm proves the state it tests CAN fire before proving it does
not fire wrongly.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import nouncensus as nc  # noqa: E402


class CensusTest(unittest.TestCase):
    def _tree(self, **files):
        d = tempfile.mkdtemp(prefix="helm-test-nouncensus-")
        self.addCleanup(shutil.rmtree, d, True)
        for name, src in files.items():
            with open(os.path.join(d, name + ".py"), "w") as fh:
                fh.write(src)
        return d

    def test_a_single_noun_line_is_ASSIGNED_and_a_shared_one_is_AMBIGUOUS(self):
        """THE PAIR. Same call, two lines, opposite states — so neither result
        can come from the census being uniformly one thing."""
        root = self._tree(m=("def f(row):\n"
                             "    a = row['seat']\n"
                             "    b = row['seat'] + row['session']\n"))
        rows, unparsed = nc.census(root)
        # UNCONDITIONAL POSITIVE on the same call the absence below reads, so
        # "no unparsed modules" cannot be satisfied by a census that
        # produced nothing at all.
        self.assertTrue(rows, "positive control: the census produced no sites")
        self.assertEqual(unparsed, [])
        by_line = {}
        for r in rows:
            by_line.setdefault(r["line"], set()).add(r["state"])
        self.assertEqual(by_line.get(2), {nc.ASSIGNED},
                         "a single-noun line must be ASSIGNED")
        self.assertEqual(by_line.get(3), {nc.AMBIGUOUS},
                         "positive control: the AMBIGUOUS state must be "
                         "REACHABLE — it was dead in the first cut")

    def test_a_censused_line_past_the_bound_says_it_was_cut(self):
        """A census row is a claim about a source line, and a reader checks
        it against the file. A row that reads as the whole line when it is a
        prefix makes that check silently disagree. Measured over this tree
        the case is rare — 91 lines of 282,467 — and the longest is ten
        thousand characters, so the bound stays and the cut speaks."""
        long_line = "    a = row['seat']  # %s\n" % ("why " * 80)
        root = self._tree(m="def f(row):\n" + long_line)
        rows, _unparsed = nc.census(root)
        self.assertTrue(rows, "positive control: the census produced no sites")
        text = rows[0]["text"]
        self.assertIn("[cut: 160 of %d chars]" % len(long_line.strip()), text)
        self.assertEqual(text, nc.pk.cut_marked(long_line.strip(), 160))

    def test_a_censused_line_inside_the_bound_is_byte_identical(self):
        """The must-hit control: the common row is exactly the source line,
        so the mark above is evidence of a real cut."""
        root = self._tree(m="def f(row):\n    a = row['seat']\n")
        rows, _unparsed = nc.census(root)
        self.assertTrue(rows, "positive control: the census produced no sites")
        self.assertEqual(rows[0]["text"], "a = row['seat']")

    def test_an_ambiguous_row_carries_EVERY_noun_on_its_line(self):
        """A row marked AMBIGUOUS that named only its own noun would read as
        resolved to anyone scanning one column."""
        root = self._tree(m="x = seat + sid + lease\n")
        rows, _ = nc.census(root)
        amb = [r for r in rows if r["state"] == nc.AMBIGUOUS]
        self.assertTrue(amb, "positive control dead")
        for r in amb:
            self.assertEqual(r["nouns_here"], ["lease", "seat", "session"])

    def test_all_nine_nouns_keep_the_seven_existing_token_counts(self):
        """Split Lane's old lease tokens without changing any other owner."""
        root = self._tree(m=("actor_id = 1\nseat = 1\nsession = 1\n"
                             "pane = 1\nlane = 1\nlease = 1\n"
                             "lease_id = 1\nfence = 1\nrecipient = 1\n"
                             "obligation = 1\nverb = 1\n"
                             "SEAT_VERBS = 1\nMELD_VERBS = 1\n"
                             "_TEXT_VERBS = 1\n"))
        rows, unparsed = nc.census(root)
        self.assertEqual(unparsed, [])
        by_noun = nc.summary(rows, unparsed)
        for noun in ("actor", "seat", "session", "pane", "routing", "obligation"):
            self.assertEqual(by_noun[noun]["sites"], 1, noun)
        self.assertEqual(by_noun["lane"]["sites"] + by_noun["lease"]["sites"], 4,
                         "the original Lane count must be conserved")
        self.assertEqual(by_noun["lane"]["sites"], 1)
        self.assertEqual(by_noun["lease"]["sites"], 3)
        self.assertEqual(by_noun["verb"]["sites"], 4)
        self.assertEqual({r["token"] for r in rows if r["noun"] == "verb"},
                         {"verb", "SEAT_VERBS", "MELD_VERBS", "_TEXT_VERBS"})
        self.assertEqual({r["token"] for r in rows if r["noun"] == "lease"},
                         {"lease", "lease_id", "fence"})
        self.assertTrue(all(r["state"] == nc.ASSIGNED for r in rows),
                        "positive control: the fixture produced all nine nouns")

    def test_real_tree_preserves_all_seven_previous_noun_counts(self):
        from unittest import mock
        old = {
            "actor": {"actor_id", "runtime_verified", "runtime_sessions",
                      "resolve_identity", "identity_disagreement"},
            "seat": {"seat", "seat_id", "HELM_CHAT_NAME", "acting_seat",
                     "own_name", "foreign_seat", "_seat_label"},
            "session": {"session", "session_id", "sid", "runtime_for_session",
                        "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"},
            "pane": {"pane", "pane_key", "ORCA_PANE_KEY",
                     "ORCA_TERMINAL_HANDLE", "ORCA_TAB_ID", "leafId"},
            "lane": {"lane", "lease", "lease_id", "fence"},
            "routing": {"recipient", "supersedes", "dispatch_id", "reviewed_tip"},
            "obligation": {"obligation", "owed", "discharge", "ownerasks"},
        }
        previous_owner = {tok: noun for noun, tokens in old.items()
                          for tok in tokens}
        with mock.patch.object(nc, "_OWNER", previous_owner):
            previous, previous_unparsed = nc.census()
        current, current_unparsed = nc.census()
        self.assertEqual(previous_unparsed, [])
        self.assertEqual(current_unparsed, [])
        self.assertTrue(previous, "positive control: the seven-noun tree has sites")
        before = nc.summary(previous, previous_unparsed)
        after = nc.summary(current, current_unparsed)
        for noun in old:
            with self.subTest(noun=noun):
                old_counts = [before[noun][key] for key in ("sites", "readers", "writers")]
                new_counts = [after[noun][key] for key in ("sites", "readers", "writers")]
                if noun == "lane":
                    new_counts = [after["lane"][key] + after["lease"][key]
                                  for key in ("sites", "readers", "writers")]
                self.assertEqual(new_counts, old_counts)
        self.assertGreater(after["lease"]["sites"], 0,
                           "positive control: the Lease split must move real sites")
        self.assertGreater(after["verb"]["sites"], 0,
                           "positive control: Verb must measure real sites")

    def test_lane_lease_and_verb_on_one_site_are_ambiguous(self):
        root = self._tree(m="x = lane + lease_id + verb\ny = lane\nz = fence\n")
        rows, unparsed = nc.census(root)
        self.assertEqual(unparsed, [])
        by_line = {line: [r for r in rows if r["line"] == line]
                   for line in (1, 2, 3)}
        self.assertEqual({r["noun"] for r in by_line[1]},
                         {"lane", "lease", "verb"}, "positive control: all three hit")
        for row in by_line[1]:
            self.assertEqual(row["state"], nc.AMBIGUOUS)
            self.assertEqual(row["nouns_here"], ["lane", "lease", "verb"])
        self.assertEqual([r["state"] for r in by_line[2]], [nc.ASSIGNED])
        self.assertEqual([r["state"] for r in by_line[3]], [nc.ASSIGNED])

    def test_direction_comes_from_the_AST_not_the_spelling(self):
        """`seat = x` is a WRITE and `y = seat` is a READ because of Store vs
        Load, never because a name looks like a setter."""
        root = self._tree(m="seat = 1\nlane = seat\n")
        rows, _ = nc.census(root)
        dirs = {(r["line"], r["token"]): r["dir"] for r in rows}
        self.assertEqual(dirs[(1, "seat")], "write")
        self.assertEqual(dirs[(2, "seat")], "read")   # same token, opposite dir
        self.assertEqual(dirs[(2, "lane")], "write")

    def test_R2_separates_kernel_proof_from_forgeable_candidate_evidence(self):
        """The record's R2: /proc/<pid>/exe is observer-side and unforgeable;
        environ and argv are written by the process they describe."""
        root = self._tree(m=("seat = read('/proc/%d/exe' % pid)\n"
                             "seat = env['HELM_CHAT_NAME']\n"
                             "seat = row\n"))
        rows, _ = nc.census(root)
        tier = {r["line"]: r["tier"] for r in rows}
        # THE FIXTURE MUST HAVE PRODUCED A SITE ON EACH LINE BEFORE ANY TIER
        # CLAIM MEANS ANYTHING. My first cut wrote `seat_from(...)`, which
        # contains no noun token at all, so line 1 yielded NO ROW and the
        # tier assertion read that absence as a wrong value. An absent site
        # and a mis-tiered site must not fail the same way.
        self.assertEqual(sorted(tier), [1, 2, 3],
                         "fixture produced no site on some line: %r" % sorted(tier))
        self.assertEqual(tier[1], "kernel")
        self.assertEqual(tier[2], "candidate")
        self.assertEqual(tier[3], "",
                         "an attribution-free line claims no tier")

    def test_an_UNPARSEABLE_module_is_reported_not_dropped(self):
        """A hole in the closure that vanishes makes the census report a
        smaller, cleaner world than the one it measured."""
        root = self._tree(good="x = seat\n", bad="def broken(:\n")
        rows, unparsed = nc.census(root)
        self.assertTrue(rows, "positive control: the readable module counted")
        self.assertEqual([u["node"] for u in unparsed], ["bad"])
        self.assertIn("SyntaxError", unparsed[0]["why"])

    def test_the_census_measures_the_REAL_tree_and_reads_all_of_it(self):
        """The must-hit. A census that silently read nothing would satisfy
        every arm above, all of which build their own tiny tree."""
        rows, unparsed = nc.census()
        self.assertEqual(unparsed, [], "every helm module must parse")
        s = nc.summary(rows, unparsed)
        self.assertGreater(s["_sites"], 1000, "the real tree is not tiny")
        for noun in nc.NOUNS:
            self.assertGreater(s[noun]["sites"], 0,
                               "noun %r has no sites — its seed is dead" % noun)


class RealTreeCensusOncePerOwnerTableTest(unittest.TestCase):
    """The real tree is censused once per process for each `_OWNER` table
    (task/3039).

    MEASURED BEFORE THE MEMO: `census()` ran 22 times in this module, 13 of
    them on the real tree at about 8.3 s profiled each, which was 90% of the
    module. The key is the IDENTITY of `_OWNER`, because an arm above patches
    it to the seven-noun table and must get that table's census, not the
    remembered nine-noun one. Any other root is censused on every call.
    """

    def _counted(self):
        real = nc._mark_ambiguous
        calls = []

        def spy(rows):
            calls.append(1)
            return real(rows)

        patch = mock.patch.object(nc, "_mark_ambiguous", spy)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_a_second_real_census_parses_nothing(self):  # noqa: VACUOUS_ASSERTION — the counter is proven live in this arm: a planted census under the same spy must add one
        censused = self._counted()
        rows, unparsed = nc.census()
        seen = len(censused)
        self.assertGreater(len(rows), 1000, "control: the real tree was read")
        self.assertEqual(nc.census(), (rows, unparsed))
        self.assertEqual(len(censused), seen,
                         "a second real-tree census parsed the package again")
        d = tempfile.mkdtemp(prefix="helm-test-nouncensus-control-")
        self.addCleanup(shutil.rmtree, d, True)
        nc.census(d)
        self.assertEqual(len(censused), seen + 1,
                         "control: the counter sees a census that runs")

    def test_a_patched_owner_table_gets_its_own_census(self):
        """On a small stand-in for the real package, so the arm does not pay
        two more whole-tree censuses: `wiring.modules()` names one planted
        file, and the memo starts empty for this case."""
        d = tempfile.mkdtemp(prefix="helm-test-nouncensus-owner-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "m.py")
        with open(path, "w") as fh:
            fh.write("seat = 1\nverb = 2\n")
        for patch in (mock.patch.object(nc.wiring, "modules",
                                        lambda root=None: {"m": path}),
                      mock.patch.object(nc, "_REAL", [])):
            patch.start()
            self.addCleanup(patch.stop)
        censused = self._counted()
        real_rows, _ = nc.census()
        seven = {tok: noun for tok, noun in nc._OWNER.items()
                 if noun not in ("lease", "verb")}
        with mock.patch.object(nc, "_OWNER", seven):
            patched_rows, _ = nc.census()
        self.assertEqual(len(censused), 2,
                         "the patched table was served the remembered census")
        self.assertEqual([r["noun"] for r in patched_rows], ["seat"])
        self.assertEqual([r["noun"] for r in real_rows], ["seat", "verb"],
                         "control: the real table does census verb sites")
        self.assertEqual(nc.census()[0], real_rows,
                         "the real table got the patched table's census")
        self.assertEqual(len(censused), 2,
                         "the real table's census was not remembered")

    def test_a_planted_root_is_censused_every_time(self):
        nc.census()                          # the real tree is remembered
        d = tempfile.mkdtemp(prefix="helm-test-nouncensus-planted-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "m.py"), "w") as fh:
            fh.write("seat = 1\n")
        censused = self._counted()
        first, _ = nc.census(d)
        with open(os.path.join(d, "m.py"), "a") as fh:
            fh.write("lane = 2\n")
        second, _ = nc.census(d)
        self.assertEqual(len(censused), 2)
        self.assertEqual([r["token"] for r in first], ["seat"])
        self.assertEqual([r["token"] for r in second], ["seat", "lane"])

    def test_a_caller_that_edits_a_row_cannot_change_the_next_census(self):
        rows, _ = nc.census()
        self.assertTrue(rows, "control: the real tree has sites")
        rows[0]["noun"] = "planted-3039"
        rows.append({"noun": "planted-3039"})
        self.assertNotIn("planted-3039", {r["noun"] for r in nc.census()[0]})


class CensusVerbTest(unittest.TestCase):
    """The verb, because a census nobody can run is not a deliverable.

    IT ALSO FIXES A REAL DEFECT I SHIPPED. Before this existed, helm's own
    wiring ladder classified nouncensus as UNREACHABLE from the entry points:
    223 of 225 modules were reachable and the exceptions were __init__ (an
    ALLOWED intrinsic) and mine. That is BUILT-NOT-WIRED, the exact class
    wiring.py exists to catch and whose docstring calls it the reason
    90%-done-never-100% happens. One verb answers both that and the
    human-surface rule: the census now has a reader.
    """

    def _run(self, args):
        import contextlib
        import io as _io
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nc.cmd_nouncensus(args)
        return rc, out.getvalue(), err.getvalue()

    def test_the_module_is_REACHABLE_from_the_cli(self):
        """The arm that would have caught the original defect. wiring is the
        tree's own instrument, so this asks it rather than asserting a rule."""
        from helm import wiring
        graph = wiring.graph()
        self.assertIn("nouncensus", graph, "positive control: it is a module")
        self.assertIn("nouncensus", wiring.reachable(graph),
                      "nouncensus is BUILT-NOT-WIRED — nothing can run it")

    def test_bare_prints_the_table_and_names_the_violation(self):
        rc, out, _ = self._run([])
        self.assertEqual(rc, 0)
        for noun in nc.NOUNS:
            self.assertIn(noun, out)
        self.assertIn("AMBIGUOUS", out)
        self.assertIn("assigned exactly once", out,
                      "the table must say what an ambiguous site MEANS")

    def test_a_VALUED_flag_actually_receives_its_value(self):
        """THE ARM THAT WAS MISSING, and its absence let a dead flag ship.

        guard_tail was handed a FILTERED tail — only the "--" tokens — so the
        VALUE was stripped off every valued flag and both --noun and --tier
        refused calls that supplied one. Measured on trunk the minute the verb
        landed. Asserting rc alone cannot see it: `--noun nosuchnoun` exited 2
        BEFORE the fix ("--noun wants a value") and exits 2 AFTER it ("unknown
        noun"), so the arm below asserts the MESSAGE, which is the only thing
        that separates the two causes."""
        rc, out, _ = self._run(["--noun", "seat"])
        self.assertEqual(rc, 0, "a valued flag must reach the verb")
        self.assertTrue(out.strip(), "positive control: --noun produced rows")
        for line in out.splitlines():
            if line.startswith("  ") and "helm nouncensus:" not in line:
                self.assertIn("seat", line, "--noun did not FILTER: %r" % line)
        rc, out, _ = self._run(["--tier", "candidate"])
        self.assertEqual(rc, 0, "--tier must reach the verb too")

    def test_lease_and_verb_filters_measure_the_real_tree(self):
        import json as _json
        rc, out, _err = self._run(["--noun", "lease", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(_json.loads(out)["rows"],
                        "positive control: --noun lease produced real sites")
        for noun, tokens in (("lease", {"lease", "lease_id", "fence"}),
                             ("verb", {"verb", "SEAT_VERBS", "MELD_VERBS",
                                       "_TEXT_VERBS"})):
            with self.subTest(noun=noun):
                rc, out, _err = self._run(["--noun", noun, "--json"])
                self.assertEqual(rc, 0)
                payload = _json.loads(out)
                rows = payload["rows"]
                self.assertTrue(rows, "positive control: %s has no real sites" % noun)
                self.assertEqual(payload["unparsed"], [])
                self.assertEqual({r["noun"] for r in rows}, {noun})
                self.assertEqual({r["token"] for r in rows}, tokens)

    def test_an_invalid_TIER_refuses_and_a_valid_EMPTY_one_does_not(self):
        """A FIX, and the pairing is the whole test.

        `--tier nosuchtier` exited 0 printing "0 sites" — byte-identical to
        what a CORRECT filter with nothing to show prints. So a typo read as
        "no attributions rest on forgeable evidence", the opposite of what R2
        exists to say. Absence and all-clear shared one representation.

        THE PAIR MATTERS MORE THAN THE REFUSAL. `--tier kernel` legitimately
        matches nothing on this tree (no LINE both touches a noun and carries
        kernel evidence), and it must still exit 0 — otherwise the cure would
        just be "refuse whenever the result is empty", which would hide the
        real finding behind the same wall as the typo."""
        rc, _out, err = self._run(["--tier", "nosuchtier"])
        self.assertEqual(rc, 2, "an invalid tier must refuse, not return empty")
        self.assertIn("unknown tier", err)
        for tier in nc.TIERS:
            self.assertIn(tier, err, "the refusal must name the known tiers")
        # the pair: a VALID tier that matches nothing is data, not an error
        rc_empty, out_empty, _ = self._run(["--tier", "kernel"])
        self.assertEqual(rc_empty, 0,
                         "a valid tier matching nothing is a RESULT, not a refusal")
        self.assertIn("site", out_empty, "and it still reports what it found")

    def test_the_TIERS_the_verb_accepts_are_the_ones_tier_produces(self):
        """A filter whose vocabulary drifts from its producer either refuses
        valid input or admits invalid, and both look like data. So the verb
        validates against TIERS and _tier is checked to emit exactly those."""
        # UNCONDITIONAL AND STRUCTURAL: an assertEqual against a literal is a
        # scalar pin, which this repo's vacuous rung treats as neither proof
        # nor claim, and it is weak evidence besides — these assertIns prove
        # the two vocabularies overlap at all before the loop compares them.
        self.assertIn("kernel", nc.TIERS)
        self.assertIn("candidate", nc.TIERS)
        produced = set()
        for text in ('os.readlink("/proc/%d/exe" % pid)',
                     'env.get("HELM_CHAT_NAME")'):
            produced.add(nc._tier(text))
        self.assertEqual(produced, set(nc.TIERS),
                         "_tier emits a tier the verb will not accept, or vice versa")

    def test_the_unknown_noun_refusal_names_THAT_cause(self):
        """rc 2 is produced by two different failures — a missing value and an
        unknown noun. An arm that reads only the code cannot tell them apart,
        and mine passed for the wrong one until this existed."""
        rc, _out, err = self._run(["--noun", "nosuchnoun"])
        self.assertEqual(rc, 2)
        blob = _out + err if False else err
        self.assertIn("unknown noun", blob,
                      "refused for the wrong reason: %r" % blob[:120])
        self.assertNotIn("wants a value", blob,
                         "the VALUE was stripped before the verb saw it")

    def test_an_unknown_flag_and_an_unknown_noun_both_REFUSE(self):
        """rc 2, not a silently wider answer. Paired with the bare call above,
        which proves the verb can succeed."""
        # UNCONDITIONAL, STRUCTURAL POSITIVE on the same call. An exit-code
        # assertion is an EXACT SCALAR PIN, which vacuous_assertion classifies
        # as "neither proof nor claim" (its _record returns early on one) — so
        # `assertEqual(rc, 0)` proves nothing pairable and my first two
        # attempts at this control were invisible to the rung for that reason.
        # Asserting the verb produced a TABLE is the stronger claim anyway: a
        # verb that refused everything, or emitted nothing, fails here.
        _rc, out, _err = self._run([])
        self.assertIn("noun", out,
                      "positive control dead: the verb produced no table")
        for args in (["--nope"], ["--noun", "nosuchnoun"]):
            with self.subTest(args=args):
                rc, _out, _err = self._run(args)
                self.assertEqual(rc, 2, "refusal must exit 2: %r" % args)

    def test_json_carries_rows_AND_the_unreadable_holes(self):
        import json as _json
        rc, out, _ = self._run(["--json"])
        self.assertEqual(rc, 0)
        payload = _json.loads(out)
        self.assertGreater(len(payload["rows"]), 1000)
        self.assertIn("unparsed", payload,
                      "a consumer must be able to see the closure's holes")

    def test_ambiguous_lists_only_ambiguous_sites(self):
        rc, out, _ = self._run(["--ambiguous"])
        self.assertEqual(rc, 0)
        body = [l for l in out.splitlines() if l.startswith("  ")]
        self.assertTrue(body, "positive control: there ARE ambiguous sites")
        for line in body:
            if "helm nouncensus:" in line:
                continue
            self.assertIn("[", line, "every listed site must name its noun set")


class ProofTierTest(unittest.TestCase):
    """R2's tier, and the over-match class a probe measured on the first cut.

    THE DEFECT: _KERNEL was a bare substring tuple, so ALL FOUR kernel-tier
    sites on trunk were false — a usage string in cli.py, "agent_starttime"
    and "starttime" as ordinary FIELD NAMES, and the prose "/proc evidence)"
    inside an error message. Not one was a kernel read. A tier that
    over-claims is worse than no tier: R2 exists to say which attributions
    rest on evidence the subject could have FORGED, so a false KERNEL row
    says "trust this one" about a dict key.

    Every negative below is paired with a true positive on the same call, and
    the four negatives are the four real sites from that measurement rather
    than shapes I invented afterwards.
    """

    def test_a_real_proc_exe_read_is_KERNEL(self):
        for text in ('exe = os.readlink("/proc/%s/exe" % d)',
                     'open("/proc/%d/stat" % pid)'):
            with self.subTest(text=text):
                self.assertEqual(nc._tier(text), "kernel", text)
        # unconditional, outside the loop
        self.assertEqual(nc._tier('os.readlink("/proc/%d/exe" % pid)'), "kernel")

    def test_the_four_MEASURED_over_matches_are_not_kernel(self):
        """The exact lines the first cut mis-tiered, kept verbatim so a
        regression names its own history."""
        self.assertEqual(nc._tier('os.readlink("/proc/%d/exe" % pid)'), "kernel",
                         "positive control dead")
        for text in ('"agent_starttime", "model",',
                     '"holder": seat, "pid": pid, "starttime": starttime,',
                     '"or /proc evidence)" % session)',
                     'spawn|where|resume|rebind <seat>'):
            with self.subTest(text=text):
                self.assertNotEqual(nc._tier(text), "kernel", text)

    def test_the_candidate_tier_is_word_bounded_too(self):
        """The sibling class. Curing one tier and leaving the other matching
        by substring is how the same defect returns wearing the other name."""
        self.assertEqual(nc._tier('env.get("HELM_CHAT_NAME")'), "candidate",
                         "positive control dead")
        self.assertEqual(nc._tier("os.environ.get(k)"), "candidate")
        self.assertEqual(nc._tier("the environment was already prepared"), "",
                         "'environment' is not an environ READ")

    def test_a_line_with_no_attribution_claims_no_tier(self):
        self.assertEqual(nc._tier('os.readlink("/proc/%d/exe" % pid)'), "kernel",
                         "positive control dead")
        self.assertEqual(nc._tier("rows = [r for r in rows if r]"), "")
