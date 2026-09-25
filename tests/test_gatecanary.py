"""The nightly serial canary: its comparison, its marker and its launch.

Receipts are MINTED through the real paths (`gate._mint_result` for serial,
the sliced door for v10) on the slice fixture's repository, so what the
canary compares is what a ledger holds. No suite runs here: the launcher is
planted, and it mints the receipts a real launcher would bring home.
"""
import json
import os
import unittest
from unittest import mock

from helm import gate, gateauthority, gatecanary, gateslice
from tests.test_gate_slice_receipt import SliceFixture

RULE = "=" * 70 + "\n"
DASH = "-" * 70 + "\n"


def _block(kind, test):
    name = test.rsplit(".", 1)[1]
    return (RULE + "%s: %s (%s)\n" % (kind, name, test) + DASH
            + "Traceback (most recent call last):\n"
            + '  File "tests/x.py", line 1, in %s\n' % name
            + "AssertionError: planted\n\n")


def _text(blocks=(), ran=2, marker=False):
    failures = sum(kind == "FAIL" for kind, _t in blocks)
    errors = sum(kind == "ERROR" for kind, _t in blocks)
    detail = ", ".join(part for part in (
        "failures=%d" % failures if failures else "",
        "errors=%d" % errors if errors else "") if part)
    body = "".join(_block(kind, test) for kind, test in blocks)
    footer = DASH + "Ran %d tests in 0.1s\n\n%s\n" % (
        ran, "FAILED (%s)" % detail if blocks else "OK")
    return (gateslice.DIAGNOSTIC_MARKER + "\n" if marker else "") + body \
        + footer


B_FAILS = ("FAIL", "tests.test_b.Case.test_b")
AUDIT = ("ERROR", "tests.test_a." + gateslice.LEAK_TEST)


class CanaryFixture(SliceFixture):
    def setUp(self):
        super().setUp()
        # Only the canary's own posts are counted; the gate's queue lines
        # travel the same seam to the fixture's scratch chat dir.
        self.posts = []
        real = gatecanary.chat.post

        def post(text, **kw):
            if kw.get("who") != gatecanary.WHO:
                return real(text, **kw)
            self.assertEqual(kw.get("room"), gatecanary.ROOM)
            self.posts.append(text)
            return {"ok": 1}
        patch = mock.patch.object(gatecanary.chat, "post", side_effect=post)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = os.path.join(self.tmp, "canary-home")

    def serial_row(self, blocks=(), ran=2):
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        row, minted, err = gate._mint_result(
            self.repo, self.head, tree, False, ident,
            [ident["executable"]] + list(gateauthority.SERIAL_ARGV), True,
            None, 1 if blocks else 0, 0, _text(blocks, ran))
        self.assertTrue(minted, err)
        return row

    def sliced_row(self, blocks=()):
        failures = sum(kind == "FAIL" for kind, _t in blocks)
        errors = sum(kind == "ERROR" for kind, _t in blocks)
        outcome = dict(self.evidence()["outcome"], failures=failures,
                       errors=errors, ok=not blocks)
        row, err = self.mint(self.evidence(outcome=outcome),
                             text=_text(blocks, marker=True),
                             rc=1 if blocks else 0)
        self.assertIsNone(err, err)
        return row


class CompareTest(CanaryFixture):
    def test_the_same_outcomes_agree_and_leave_no_marker(self):  # noqa: VACUOUS_ASSERTION — the next arm plants one differing failure on the same fixture and asserts the marker and the one post PRESENT
        result = gatecanary.judge(self.serial_row([B_FAILS]),
                                  self.sliced_row([B_FAILS]), self.home)
        self.assertEqual(result["verdict"], gatecanary.AGREE, result)
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        self.assertEqual(self.posts, [])
        with open(gatecanary.last_path(self.home)) as fh:
            self.assertEqual(json.load(fh)["verdict"], gatecanary.AGREE)

    def test_a_failure_only_serial_sees_disables_sliced_at_land(self):
        """The codex shape at receipt level: serial red, slices green."""
        serial, sliced = self.serial_row([B_FAILS]), self.sliced_row()
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        result = gatecanary.judge(serial, sliced, self.home)
        self.assertEqual(result["verdict"], gatecanary.DIVERGED, result)
        self.assertEqual(result["divergences"], [{
            "test": "tests.test_b.Case.test_b", "serial": "FAIL x1",
            "sliced": "not FAIL", "kind": "serial-only"}])
        why = gate.sliced_land_disabled(self.home)
        self.assertIn(serial["tree"][:12], why)
        self.assertIn(serial["id"], why)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("DIVERGED", self.posts[0])
        self.assertIn("tests.test_b.Case.test_b", self.posts[0])

    def test_an_audit_error_only_slices_report_is_a_divergence(self):
        result = gatecanary.judge(self.serial_row(),
                                  self.sliced_row([AUDIT]), self.home)
        self.assertEqual(result["verdict"], gatecanary.DIVERGED, result)
        self.assertEqual([d["kind"] for d in result["divergences"]],
                         ["audit"])
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))

    def test_a_count_difference_is_a_divergence(self):
        result = gatecanary.compare(self.serial_row(ran=3), self.sliced_row(),
                                    {}, {})
        self.assertEqual(result["verdict"], gatecanary.DIVERGED, result)
        self.assertEqual(result["divergences"], [{
            "test": "<ran count>", "serial": "3", "sliced": "2",
            "kind": "count"}])

    def test_rows_that_are_not_a_serial_and_sliced_pair_are_unknown(self):
        serial, sliced = self.serial_row(), self.sliced_row()
        for pair in ((sliced, sliced), (serial, serial),
                     (dict(serial, tree="0" * 40), sliced)):
            result = gatecanary.compare(pair[0], pair[1], {}, {})
            self.assertEqual(result["verdict"], gatecanary.UNKNOWN, result)
        result = gatecanary.compare(serial, sliced, None, {}, "planted why")
        self.assertEqual((result["verdict"], result["reason"]),
                         (gatecanary.UNKNOWN, "planted why"))
        # Positive control: the same pair with readable records agrees.
        self.assertEqual(gatecanary.compare(serial, sliced, {}, {})["verdict"],
                         gatecanary.AGREE)

    def test_an_overflowing_failure_record_is_read_whole_from_its_chunks(self):
        blocks = [("FAIL", "tests.test_b.Case.test_%02d" % i)
                  for i in range(gate.FAILURE_CAP + 5)]
        serial = self.serial_row(blocks, ran=len(blocks))
        self.assertTrue(serial["failure_chunks"], "fixture broken: inline")
        rows, _unavailable = gate.eventledger.checked_events(
            gate.receipts_path(), strict=True)
        found, why = gatecanary.failure_identities(serial, rows)
        self.assertIsNone(why)
        self.assertEqual(sorted(test for _kind, test in found),
                         sorted(test for _kind, test in blocks))
        found, why = gatecanary.failure_identities(serial, [])
        self.assertIsNone(found)
        self.assertIn("chunk", why)


class MarkerTest(CanaryFixture):
    def _diverge(self):
        gatecanary.judge(self.serial_row([B_FAILS]), self.sliced_row(),
                         self.home)

    def test_the_marker_outlives_a_later_agreement_until_a_person_clears_it(self):
        self._diverge()
        path = gate.sliced_land_marker_path(self.home)
        with open(path) as fh:
            since = json.load(fh)["since"]
        gatecanary.judge(self.serial_row(), self.sliced_row(), self.home)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))
        self._diverge()
        with open(path) as fh:
            self.assertEqual(json.load(fh)["since"], since)
        rc, line = gatecanary.clear_marker("", self.home)
        self.assertEqual(rc, 2, line)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))
        rc, line = gatecanary.clear_marker("flake traced to test_b's fixture",
                                           self.home)
        self.assertEqual(rc, 0, line)
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        archived = [name for name in os.listdir(os.path.dirname(path))
                    if name.startswith(os.path.basename(path) + ".cleared-")]
        self.assertEqual(len(archived), 1)
        with open(os.path.join(os.path.dirname(path), archived[0])) as fh:
            kept = json.load(fh)
        self.assertEqual(kept["cleared"]["reason"],
                         "flake traced to test_b's fixture")
        self.assertEqual(kept["divergence_total"], 1)

    def test_an_unreadable_marker_disables_like_a_readable_one(self):
        path = gate.sliced_land_marker_path(self.home)
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{half a marker")
        self.assertIn("cannot be read", gate.sliced_land_disabled(self.home))

    def test_the_verb_reads_the_marker_and_exits_by_it(self):
        with mock.patch.object(gatecanary.home, "global_dir",
                               return_value=self.home):
            self.assertEqual(gatecanary.cmd(["status"]), 0)
            self._diverge()
            self.assertEqual(gatecanary.cmd([]), 1)
            self.assertEqual(gatecanary.cmd(["clear"]), 2)
            self.assertEqual(gatecanary.cmd(["clear", "--reason", "read"]), 0)
            self.assertEqual(gatecanary.cmd(["status"]), 0)
            self.assertEqual(gatecanary.cmd(["bogus"]), 2)


class RunTest(CanaryFixture):
    """The nightly path: trunk's tip, gated for whichever kind it lacks."""

    def setUp(self):
        super().setUp()
        self._git("branch", "-f", "main", "HEAD")
        self.room = os.path.join(self.tmp, "peek-room")
        for name, value in (
                ("work._gc.refresh_trunk", (True, "trunk main is local")),
                ("work.peek", (0, {"path": self.room, "sha": self.head}))):
            patch = mock.patch("helm.%s" % name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        drop = mock.patch("helm.work.peek_drop", return_value=(0, []))
        self.dropped = drop.start()
        self.addCleanup(drop.stop)
        self.launched = []

    def _runner(self, serial_blocks=(), mints=True):
        def runner(argv, log, env):
            self.launched.append(argv)
            self.assertEqual(env.get(gate.CANARY_ENV), "1")
            if mints:
                if argv[-1] == "--sliced":
                    self.sliced_row()
                else:
                    self.assertEqual(argv[-1], "--serial")
                    self.serial_row(serial_blocks)
            return 0
        return runner

    def test_a_night_gates_each_kind_it_lacks_and_judges_the_pair(self):  # noqa: VACUOUS_ASSERTION — the two launches are asserted EQUAL to the non-empty argv pair; the no-post claim is controlled by the divergent-night arm
        result = gatecanary.run(self.repo, self.home, runner=self._runner())
        self.assertEqual(result["verdict"], gatecanary.AGREE, result)
        self.assertEqual(self.launched, [
            ["fab", "gate", "--repo", self.room, "--serial"],
            ["fab", "gate", "--repo", self.room, "--sliced"]])
        self.dropped.assert_called_once()
        self.assertEqual(self.posts, [])

    def test_the_train_gates_serial_receipt_is_used_and_only_slices_run(self):
        """The ordinary night: trunk's tree already holds the serial receipt
        its own train gate minted, so the door would refuse a second one."""
        self.serial_row()
        result = gatecanary.run(self.repo, self.home, runner=self._runner())
        self.assertEqual(result["verdict"], gatecanary.AGREE, result)
        self.assertEqual(self.launched,
                         [["fab", "gate", "--repo", self.room, "--sliced"]])

    def test_a_tree_holding_both_kinds_is_judged_without_a_run_then_skipped(self):
        self.serial_row()
        self.sliced_row()
        result = gatecanary.run(self.repo, self.home, runner=self._runner())
        self.assertEqual(result["verdict"], gatecanary.AGREE, result)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.dropped.call_count, 0)
        again = gatecanary.run(self.repo, self.home, runner=self._runner())
        self.assertEqual(again["verdict"], gatecanary.SKIPPED, again)
        self.assertEqual(self.launched, [])
        with open(gatecanary.last_path(self.home)) as fh:
            self.assertEqual(json.load(fh)["verdict"], gatecanary.AGREE)

    def test_the_launcher_is_the_operators_to_name(self):
        with mock.patch.dict(os.environ, {
                "HELM_GATE_CANARY_LAUNCH": "helm gate run --box build-box"}):
            gatecanary.run(self.repo, self.home, runner=self._runner())
        self.assertEqual(self.launched[0],
                         ["helm", "gate", "run", "--box", "build-box", "--repo",
                          self.room, "--serial"])

    def test_a_divergent_night_marks_and_alerts_once(self):  # noqa: VACUOUS_ASSERTION — the marker is asserted PRESENT and the post count EQUAL to one
        result = gatecanary.run(self.repo, self.home,
                                runner=self._runner([B_FAILS]))
        self.assertEqual(result["verdict"], gatecanary.DIVERGED, result)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))
        self.assertEqual(len(self.posts), 1)

    def test_a_night_whose_receipts_never_came_home_is_unknown_and_said(self):  # noqa: VACUOUS_ASSERTION — the absent marker is controlled by the divergent-night arm; the one post is asserted PRESENT and names UNKNOWN
        result = gatecanary.run(self.repo, self.home,
                                runner=self._runner(mints=False))
        self.assertEqual(result["verdict"], gatecanary.UNKNOWN, result)
        self.assertIn("no serial or sliced receipt", result["reason"])
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("UNKNOWN", self.posts[0])
        self.dropped.assert_called_once()


class CanaryDoorTest(CanaryFixture):
    """train197's one-suite-per-tree door refuses a second whole suite on a
    green tree; the canary's run of the OTHER kind is the one it admits."""

    def test_the_door_admits_a_canary_run_of_the_kind_the_tree_lacks(self):
        self.serial_row()
        verdict, note, label = gate.whole_suite_door(self.repo)
        self.assertEqual(verdict, gate.REFUSE, note)
        self.assertIn("already holds a GREEN whole-suite receipt", note)
        verdict, note, label = gate.whole_suite_door(self.repo,
                                                     canary=gate.SERIAL)
        self.assertEqual(verdict, gate.REFUSE, note)
        verdict, note, label = gate.whole_suite_door(self.repo,
                                                     canary=gate.SLICED)
        self.assertEqual((verdict, label), (gate.ADMIT, ["canary"]), note)
        self.assertIn("comparison needs one of each", note)

    def test_the_verb_reads_the_canary_declaration_from_the_environment(self):
        self.serial_row()
        seen = {}

        def door(target, **kw):
            seen.update(kw)
            return gate.REFUSE, "planted", []
        with mock.patch.object(gate, "whole_suite_door", side_effect=door), \
                mock.patch.dict(os.environ, {gate.CANARY_ENV: "1"}):
            gate.cmd_gate(["run", "--repo", self.repo, "--sliced"])
        self.assertEqual(seen.get("canary"), gate.SLICED)
        seen.clear()
        with mock.patch.object(gate, "whole_suite_door", side_effect=door), \
                mock.patch.dict(os.environ, {gate.CANARY_ENV: ""}):
            gate.cmd_gate(["run", "--repo", self.repo, "--sliced"])
        self.assertIsNone(seen.get("canary"))


class TimerTest(unittest.TestCase):
    def test_the_nightly_unit_runs_the_canary_from_the_shared_root(self):
        from helm import work
        spath, service, tpath, timer = gatecanary.timer_units()
        root = os.path.dirname(os.path.dirname(os.path.abspath(
            gatecanary.__file__)))
        cwd = work.find_root(root) or root
        self.assertIn("WorkingDirectory=%s\n" % cwd, service)
        self.assertIn(" gate canary run --repo %s\n" % cwd, service)
        self.assertIn("OnCalendar=*-*-* %s\n" % gatecanary.TIMER_AT, timer)
        self.assertIn("Persistent=true", timer)
        self.assertTrue(tpath.endswith(gatecanary.TIMER_NAME))
        self.assertTrue(spath.endswith(gatecanary.SERVICE_NAME))


if __name__ == "__main__":
    unittest.main()
