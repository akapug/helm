#!/usr/bin/env python3
"""`kind` on a dispatch, and the capacity-mix alarm it makes possible.

WHY: an integrator was told to run BUILD lanes overnight and sent 15 REVIEW
rounds on his own work instead. No surface reported it, because no surface
could — the ledger recorded who, to whom, against which tip, and when, and never
what KIND of work the dispatch was. The question "how is my fleet's capacity
allocated" was unanswerable from the only durable record of the fleet's work.

Every other instrument said healthy, and each was right about its own question:
the heartbeat fires on FLEET-QUIET and nobody was quiet; `lr stalls` bills
stalled loops and none were stalled; the owner board counts lands and they were
landing. BUSY IS NOT DIVERSE, and a fleet 100% occupied reviewing one author is
maximally busy.

The obvious retrofit — infer kind from the lane name, "-r1"/"-r2" means review —
was measured against the real ledger and misclassified 5 of 17. Hence a recorded
field, and hence UNKNOWN as a real value rather than a default.
"""
import contextlib
import io
import json
import re
import os
import shutil
import tempfile
import time
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches as D  # noqa: E402


class MixBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-mix-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        os.makedirs(os.path.dirname(D.ledger_path()), exist_ok=True)

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, kind=None, n=1, sender="opus-integrator", recipient="kimi",
              age_s=600):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - age_s))
        with open(D.ledger_path(), "a") as f:
            for i in range(n):
                # `deadline_s` is not decoration: `_valid_identity` (and so
                # `snapshot`, and now `mix`) REJECTS a row without it. The
                # original fixture omitted it, which meant every row this
                # suite planted was one `snapshot()` would have thrown away —
                # the tests only passed because `mix` validated nothing at all.
                # A fixture that cannot survive the real reader proves nothing
                # about the real reader.
                row = {"v": 3, "seq": 0, "status": "open",
                       "tip": os.urandom(20).hex(),
                       "event": "dispatch", "id": os.urandom(8).hex(),
                       "ts": stamp, "sender": sender, "recipient": recipient,
                       "lane": "lane-%d" % i, "deadline_s": 2700}
                if kind is not None:
                    row["kind"] = kind
                f.write(json.dumps(row) + "\n")


class CleanKindTest(unittest.TestCase):
    def test_the_two_real_kinds_are_accepted(self):
        for k in ("build", "review", "BUILD", " review "):
            got, err = D.clean_kind(k)
            self.assertIsNone(err, k)
            self.assertIn(got, D.KINDS)

    def test_omitting_it_is_allowed_and_means_unrecorded(self):
        """Not an error. A dispatch sent without --kind is UNKNOWN, and that is
        a legitimate state the report must be able to show."""
        self.assertEqual(D.clean_kind(None), (None, None))

    def test_a_junk_kind_is_refused_and_never_silently_coerced(self):
        got, err = D.clean_kind("buld")
        self.assertIsNone(got)
        self.assertIn("build|review", err)
        self.assertIn("guess", err)


class MixTest(MixBase):
    def test_counts_split_by_kind_with_unknown_in_its_own_bucket(self):
        self.plant("review", n=3)
        self.plant("build", n=1)
        self.plant(None, n=2)
        got, err = D.mix(hours=8)
        self.assertIsNone(err)
        b = got["opus-integrator"]
        self.assertEqual((b["build"], b["review"], b["unknown"]), (1, 3, 2))
        self.assertEqual(b["total"], 6)

    def test_unknown_is_NEVER_folded_into_build(self):
        """THE LOAD-BEARING ONE. Every row written before this field existed
        carries no kind. A report that counted those as builds would have made
        the inverted night look balanced — which is the exact failure this
        feature exists to prevent, reintroduced by its own reporting."""
        self.plant(None, n=9)
        got, _ = D.mix(hours=8)
        b = got["opus-integrator"]
        self.assertEqual(b["build"], 0, "an unrecorded kind was counted as a build")
        self.assertEqual(b["unknown"], 9)

    def test_the_window_excludes_older_rows(self):
        self.plant("review", n=4, age_s=9 * 3600)
        self.plant("review", n=1, age_s=60)
        got, _ = D.mix(hours=8)
        self.assertEqual(got["opus-integrator"]["review"], 1)

    def test_an_unparseable_timestamp_is_skipped_not_counted_as_now(self):
        with open(D.ledger_path(), "a") as f:
            f.write(json.dumps({"event": "dispatch", "id": "x", "ts": "whenever",
                                "sender": "s", "recipient": "r",
                                "kind": "review"}) + "\n")
        got, _ = D.mix(hours=8)
        self.assertEqual(got, {})

    def test_recipients_are_reported_so_the_spread_is_visible(self):
        self.plant("review", n=2, recipient="kimi")
        self.plant("review", n=2, recipient="ds4pro")
        got, _ = D.mix(hours=8)
        self.assertEqual(got["opus-integrator"]["recipients"], ["ds4pro", "kimi"])


class AlarmTest(MixBase):
    def test_reviews_with_zero_builds_fires(self):
        self.plant("review", n=D.MIX_NO_BUILD_ALARM)
        alarm, err = D.mix_alarm(hours=8)
        self.assertIsNone(err)
        self.assertIn("CAPACITY-INVERTED", alarm)
        self.assertIn("kimi", alarm)
        self.assertIn("ZERO build lanes", alarm)

    def test_one_real_build_lane_clears_it(self):
        """The alarm asks a yes/no question — is this a team or a queue — so a
        single genuine build lane answers it. It is deliberately not a ratio: a
        ratio invites gaming toward a number, and the failure being caught is
        the total absence of delegated authorship, not its proportion."""
        self.plant("review", n=12)
        self.plant("build", n=1, recipient="ds4pro")
        alarm, _ = D.mix_alarm(hours=8)
        self.assertIsNone(alarm)

    def test_below_the_threshold_is_quiet(self):
        self.plant("review", n=D.MIX_NO_BUILD_ALARM - 1)
        self.assertIsNone(D.mix_alarm(hours=8)[0])

    def test_an_all_UNKNOWN_window_stays_SILENT(self):
        """Deliberate, and the most important restraint here. Before `kind` was
        recorded there is no way to tell an inverted night from a healthy one,
        and firing on that would be inventing a verdict from absent data — the
        precise failure mode this whole feature is a response to. It stays quiet
        until there is something real to read."""
        self.plant(None, n=20)
        self.assertIsNone(D.mix_alarm(hours=8)[0])

    def test_the_alarm_names_who_and_says_why_no_other_surface_sees_it(self):
        self.plant("review", n=6)
        alarm, _ = D.mix_alarm(hours=8)
        self.assertIn("opus-integrator", alarm)
        self.assertIn("busy", alarm.lower())

    def test_EVERY_inverted_sender_is_named_not_just_the_first(self):
        """MEASURED FALSE-QUIET, 2026-07-31. The alarm used to `return` on its
        first hit inside `sorted(got.items())` — sorted by NAME. Live ledger at
        that moment: codex 6, codex-2 18, codex-3 5, gemini 8. FOUR senders
        qualified; it named ONE, `codex`, because "codex" sorts first — and that
        one was the second-SMALLEST. It under-reported by 75% and hid the worst
        by a factor of three, while looking exactly like an alarm that had fired
        correctly."""
        self.plant("review", n=6, sender="alpha")
        self.plant("review", n=7, sender="bravo")
        alarm, _ = D.mix_alarm(hours=8)
        self.assertIn("alpha", alarm)
        self.assertIn("bravo", alarm, "only the first sender was named")

    def test_the_WORST_sender_is_named_first_not_the_alphabetical_one(self):
        """Ordering is the half that makes the list readable. `zulu` sorts LAST
        by name and is the worst by volume; if the reader's eye lands on `alpha`
        first the list is technically complete and still misleads."""
        self.plant("review", n=6, sender="alpha")
        self.plant("review", n=18, sender="zulu")
        alarm, _ = D.mix_alarm(hours=8)
        self.assertLess(alarm.index("zulu"), alarm.index("alpha"),
                        "the worst offender was not reported first")

    def test_an_EQUAL_COUNT_TIE_breaks_by_name_deterministically(self):
        """THE MUTATION THAT SURVIVED, found by @codex-3 in review. The sort is
        `(-review_count, name)` and I tested only the count half — no fixture
        had two senders with EQUAL counts, so dropping the name tie-break left
        all 36 tests GREEN. The tie-break was correct and entirely unmeasured.

        A non-deterministic alarm is worse than a wrong one: it destroys the
        reader's ability to notice change, because the list reshuffles between
        runs for no reason. Planting in reverse-alphabetical order also proves
        the order comes from the SORT and not from insertion."""
        self.plant("review", n=6, sender="zulu")
        self.plant("review", n=6, sender="alpha")      # planted second, equal
        alarm, _ = D.mix_alarm(hours=8)
        self.assertLess(alarm.index("alpha"), alarm.index("zulu"),
                        "an equal-count tie did not break by name")

    def test_the_CLI_indents_EVERY_line_not_just_the_first(self):
        """ALSO FROM @codex-3. `cmd_mix` did `print("\\n  " + alarm)`, which
        indents line one and leaves every following line at column 0 — so the
        multi-sender alarm rendered as an indented header above a flush-left
        wall. The alarm is only ever read by someone already suspecting the
        problem, so the one time it IS read the shape has to hold.

        This binds the CLI, not the helper: the previous tests all called
        mix_alarm directly and could not have seen this."""
        self.plant("review", n=6, sender="alpha")
        self.plant("review", n=7, sender="bravo")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            D.cmd_mix(["--hours", "8"])
        out = [ln for ln in buf.getvalue().split("\n") if ln.strip()]
        self.assertTrue(out, "the alarm did not reach stdout at all")
        # EXACT PREFIXES, NOT "starts with two spaces". @codex-3 caught the
        # weaker assertion: removing the helper's own two-space list indent
        # left all 38 tests GREEN while collapsing header and items to the SAME
        # column — the promised header-2 / items-4 / tail-2 hierarchy was
        # asserted in the commit message and measured nowhere. A test that
        # accepts any indent cannot see a lost one.
        header = [ln for ln in out if "senders are capacity-inverted" in ln]
        items = [ln for ln in out if "CAPACITY-INVERTED" in ln]
        tail = [ln for ln in out if "review queue, not a team" in ln]
        self.assertTrue(header and items and tail,
                        "multi-sender render lost a part: %r" % out)
        for ln in header + tail:
            self.assertTrue(ln.startswith("  ") and not ln.startswith("   "),
                            "header/tail is not at exactly 2: %r" % ln)
        for ln in items:
            self.assertTrue(ln.startswith("    ") and not ln.startswith("     "),
                            "a list item is not at exactly 4: %r" % ln)

    def test_a_single_inverted_sender_still_reads_as_ONE_sentence(self):
        """THE NO-REGRESSION CONTROL. The common case is one sender, and it must
        not acquire a list header and an indent just because the multi case
        exists. Without this the fix could reformat every alarm and stay green."""
        self.plant("review", n=6, sender="alpha")
        alarm, _ = D.mix_alarm(hours=8)
        self.assertNotIn("\n", alarm, "a single hit grew multi-line furniture")
        self.assertIn("CAPACITY-INVERTED", alarm)

    def test_two_senders_are_judged_independently(self):
        """A healthy sender must not mask an inverted one, and vice versa."""
        self.plant("review", n=6, sender="opus-integrator")
        self.plant("build", n=2, sender="console-design", recipient="codex")
        alarm, _ = D.mix_alarm(hours=8)
        self.assertIn("opus-integrator", alarm)
        self.assertNotIn("console-design", alarm)


class UsageAdvertisesEveryVerbTest(unittest.TestCase):
    """A feature the usage line does not mention is a feature nobody finds.

    THREE separate features in this one module shipped undiscoverable and each
    was caught by a different accident: the stdin body (codex-3 review), --kind
    (a rebase check), and `mix` itself — whose usage line I "added" with a
    replace that swapped a string for ITSELF, a silent no-op that read as done.

    Fixing the third instance individually would leave the fourth to chance, so
    this asserts the RULE: every verb cmd_dispatch routes appears in USAGE."""

    def test_every_routed_verb_is_named_in_usage(self):
        import inspect
        src = inspect.getsource(D.cmd_dispatch)
        routed = set(re.findall(r'verb == "([a-z-]+)"', src))
        routed |= set(re.findall(r'verb in \(([^)]*)\)', src)
                      and re.findall(r'"([a-z-]+)"',
                                     " ".join(re.findall(r'verb in \(([^)]*)\)', src))))
        self.assertTrue(routed, "no verbs found — the scan itself broke")
        missing = sorted(v for v in routed if v not in D.USAGE)
        self.assertEqual(missing, [], "verbs routed but absent from USAGE: %s" % missing)

    def test_the_kind_flag_is_named_in_usage(self):
        self.assertIn("--kind", D.USAGE)


class RawEventsAreUntrustedTest(MixBase):
    """`mix` reads RAW events, so it inherits none of snapshot()'s hygiene.

    codex-3's re-gate: "raw events unvalidated". Both cases below produced a
    FALSE CAPACITY-INVERTED — an alarm accusing an integrator of running a
    review queue when they were not, which is the vacuous-pass class turned
    around and pointed at the operator."""

    def plant_raw(self, row):
        with open(D.ledger_path(), "a") as f:
            f.write(json.dumps(row) + "\n")

    def genesis(self, **over):
        row = {"v": 3, "seq": 0, "status": "open", "event": "dispatch",
               "id": os.urandom(8).hex(), "tip": os.urandom(20).hex(),
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "sender": "opus-integrator", "recipient": "kimi",
               "lane": "x", "kind": "review", "deadline_s": 600}
        row.update(over)
        return row

    def test_a_row_whose_identity_does_not_validate_is_not_counted(self):
        self.plant_raw(self.genesis(id="not-a-valid-id!!"))
        got, err = D.mix(hours=8)
        self.assertIsNone(err)
        self.assertEqual(got, {}, "a corrupt row was counted as a dispatch")

    def test_rows_that_pass_identity_but_FAIL_GENESIS_are_not_counted(self):
        """The sharper half of the finding. `_valid_identity` is only snapshot's
        FIRST gate — these rows clear it and still make `snapshot()` count zero,
        yet they were allocating capacity and could raise CAPACITY-INVERTED on
        their own. Canonical genesis, or the two readers disagree."""
        for bad in ({"tip": "not-a-tip"}, {"status": "cancelled"},
                    {"seq": 3}, {"v": 99}):
            with self.subTest(**bad):
                self.setUp()
                for _ in range(D.MIX_NO_BUILD_ALARM + 1):
                    self.plant_raw(self.genesis(**bad))
                snap, _ = D.snapshot()
                self.assertEqual(len(snap), 0, "fixture is not actually malformed")
                self.assertEqual(D.mix(hours=8)[0], {}, bad)
                self.assertIsNone(D.mix_alarm(hours=8)[0],
                                  "malformed rows raised a false alarm")

    def test_the_same_dispatch_id_twice_counts_ONCE(self):
        """A retry, a replay, or a restored ledger segment repeats an id. With
        MIX_NO_BUILD_ALARM at 4, two duplicated reviews are half the distance
        to a false accusation."""
        row = self.genesis(lane="dup")
        self.plant_raw(row)
        self.plant_raw(dict(row))
        got, _ = D.mix(hours=8)
        self.assertEqual(got["opus-integrator"]["review"], 1)
        self.assertEqual(got["opus-integrator"]["total"], 1)

    def test_duplicates_alone_can_no_longer_raise_the_alarm(self):
        row = self.genesis(lane="dup")
        for _ in range(D.MIX_NO_BUILD_ALARM + 2):
            self.plant_raw(dict(row))
        self.assertIsNone(D.mix_alarm(hours=8)[0],
                          "one dispatch, replayed, fired CAPACITY-INVERTED")


class MixArgsAreStrictTest(MixBase):
    """"mix args/help incomplete" — a report that silently ignores the filter
    you typed answers a different question, and looks like the answer to
    yours."""

    def run_mix(self, argv):
        import contextlib, io
        buf, errbuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(errbuf):
            rc = D.cmd_mix(argv)
        return rc, buf.getvalue() + errbuf.getvalue()

    def test_an_unknown_option_is_refused_not_ignored(self):
        rc, out = self.run_mix(["--bogus", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown option", out)

    def test_a_non_positive_window_is_refused(self):
        """`--hours 0` scans a range no row can fall into and prints the fleet
        as idle, with rc 0 — an honest-looking answer that is never true."""
        for bad in ("0", "-8"):
            rc, out = self.run_mix(["--hours", bad])
            self.assertEqual(rc, 2, bad)
            self.assertIn(">= 1", out)

    def test_hours_still_wants_an_integer_and_a_value(self):
        self.assertEqual(self.run_mix(["--hours", "soon"])[0], 2)
        self.assertEqual(self.run_mix(["--hours"])[0], 2)

    def test_sender_filters_instead_of_being_dropped(self):
        self.plant("review", n=2, sender="opus-integrator")
        self.plant("build", n=2, sender="console-design", recipient="codex")
        rc, out = self.run_mix(["--sender", "console-design"])
        self.assertEqual(rc, 0)
        self.assertIn("console-design", out)
        self.assertNotIn("opus-integrator", out)

    def test_sender_wants_a_value(self):
        self.assertEqual(self.run_mix(["--sender"])[0], 2)

    def test_a_repeated_option_is_refused_not_last_wins(self):
        """`--hours 4 --hours 48` quietly answered for 48. The window the
        operator read in their own command line was not the window the report
        was about."""
        rc, out = self.run_mix(["--hours", "4", "--hours", "48"])
        self.assertEqual(rc, 2)
        self.assertIn("twice", out)

    def test_a_bare_positional_is_refused(self):
        self.assertEqual(self.run_mix(["8"])[0], 2)


class BaseValidatesKindTest(MixBase):
    """A guard in argument parsing protects the CLI, not the DATA."""

    def test_base_refuses_a_junk_kind_instead_of_persisting_it(self):
        from unittest import mock
        from helm import seats as S
        with mock.patch.object(D, "_repo_info",
                               return_value={"repo": ".", "repo_id": "r"}), \
             mock.patch.object(D, "_resolve_tip", return_value=("a" * 40, None)), \
             mock.patch.object(S, "resolve_recipient",
                               return_value=("kimi", None)):
            row, err = D._base("kimi", "lane", "HEAD", None, 600, None,
                               kind="buld")
        self.assertIsNone(row, "a typo'd kind reached the ledger")
        self.assertIn("build|review", err)

    def test_base_canonicalizes_a_valid_kind(self):
        from unittest import mock
        from helm import seats as S
        with mock.patch.object(D, "_repo_info",
                               return_value={"repo": ".", "repo_id": "r"}), \
             mock.patch.object(D, "_resolve_tip", return_value=("a" * 40, None)), \
             mock.patch.object(S, "resolve_recipient",
                               return_value=("kimi", None)):
            row, err = D._base("kimi", "lane", "HEAD", None, 600, None,
                               kind=" BUILD ", new_work=True)
        self.assertIsNone(err)
        self.assertEqual(row["kind"], "build")


class OwnerLayerWiringTest(unittest.TestCase):
    """The three wiring defects codex-3's re-gate named. Asserted against the
    SOURCE, the same idiom this file already uses for USAGE — honest about what
    it proves: that the call site is wired, not that the CLI ran."""

    def test_cmd_dispatch_passes_kind_to_add(self):
        """THE LOAD-BEARING ONE. The parser accepted --kind on `add`,
        clean_kind validated it, and the add() call dropped it: every
        `dispatch add` recorded UNKNOWN whatever the operator typed, rc 0, no
        warning. `send` always passed it, so the field looked wired."""
        import inspect
        src = inspect.getsource(D.cmd_dispatch)
        call = src[src.index("row, why = add("):]
        self.assertIn("kind=kind", call[:400],
                      "cmd_dispatch's add() call drops --kind on the floor")

    def test_omitting_kind_is_refused_at_the_cli(self):
        """Optional made the alarm VACUOUS: the inverted night reproduces
        exactly by not typing the flag, and mix_alarm is deliberately silent on
        an all-UNKNOWN window. A guard any caller disarms by omission is not a
        guard. The LIBRARY stays permissive for historical rows."""
        import inspect
        src = inspect.getsource(D.cmd_dispatch)
        self.assertIn("if kind is None:", src)
        self.assertEqual(D.clean_kind(None), (None, None),
                         "the library must still accept unrecorded history")

    def test_kind_is_part_of_operation_key_identity(self):
        """A retry under the same key that corrected build->review returned the
        first row and reported success, keeping a kind the operator had
        explicitly changed."""
        import inspect
        self.assertIn('"kind"',
                      inspect.getsource(D._append_dispatch).split("semantic = ")[1][:220])

    def test_usage_names_kind_on_add_and_sender_on_mix(self):
        add_clause = D.USAGE[D.USAGE.index("| add "):]
        self.assertIn("--kind", add_clause.split("| verdict")[0])
        self.assertIn("--sender", D.USAGE[D.USAGE.index("mix ["):])


if __name__ == "__main__":
    unittest.main()
