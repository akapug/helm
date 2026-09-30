"""The land door and a sliced receipt: admitted only on the canary's evidence.

`gate.land_refusal` is the one predicate every land door asks about a
receipt's kind (landgate's post-rebase clause, foldcheck's tree-vs-gate rung,
`bind(need=NEED_LAND)`). A serial whole-suite receipt passes it exactly as it
always has. A sliced (v10) receipt passes only while the canary stands for
slices: no DISABLE marker, and the canary record (`gatecanary.standing`)
holding, since its newest DIVERGED,

  (i)   a clean one-pass finder run: every module alone in a fresh process;
  (ii)  a clean report-mode sliced whole suite: no leak and no failure;
  (iii) three distinct trees whose serial and fail-mode sliced receipts AGREE
        test for test, their sliced runs on at least two distinct hosts;
  (iv)  a red tree whose every real failure the sliced run caught too;
  (v)   a canary still speaking: the freshest evidence a verdict compared,
        dated by the older of its two receipts' own stamps, minted within
        36 hours (HELM_GATE_CANARY_MAX_AGE_H may only tighten it), since
        nothing in (i)-(iv) expires on its own.

A DIVERGED restarts all of it; an UNKNOWN neither counts nor restarts it; a
tree compared twice counts once. The receipt itself must be able to stand for
the serial suite: its leak census covers module data, its audit ran in fail
mode, no module leaked. The record is written only through the canary's own
writers (`append_verdict`, `finder_rows`, `catch_rows`), and an unreadable
record refuses.

The canary's nightly timer is installed by the step that installs the rail
guards (`helm work install-guard --apply`), and `helm doctor` shows it.
"""
import calendar
import contextlib
import io
import json
import os
import shutil
import subprocess
import time
import types
from unittest import mock

from helm import doctor, gate, gatecanary, gateslice
from helm.work import _guard
from tests.test_gate_slice_receipt import SliceFixture
from tests.test_gatecanary import AUDIT, B_FAILS, CanaryFixture
from tests.test_never_track_hook import HookBase

AGREES = gatecanary.STANDING_AGREE
HOSTS = gatecanary.STANDING_HOSTS
STAMP = "%Y-%m-%dT%H:%M:%SZ"
BOUND_ENV = "HELM_GATE_CANARY_MAX_AGE_H"


def stamp(hours_ago=0):
    """A receipt or record stamp `hours_ago` hours before now."""
    return time.strftime(STAMP, time.gmtime(time.time() - hours_ago * 3600))


def age_record(path, hours):
    """Rewrite every entry of the canary record at `path` as though it, and
    the evidence each verdict compared, were `hours` older: the clock pinned
    `hours` after its newest verdict, with no seam in the reader."""
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    for row in rows:
        for key in ("at", "evidence_at"):
            if isinstance(row.get(key), str):
                then = calendar.timegm(time.strptime(row[key], STAMP))
                row[key] = time.strftime(STAMP,
                                         time.gmtime(then - hours * 3600))
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)


class SlicedLandBase(CanaryFixture):

    def land_row(self):
        """A green v10 receipt that can stand for the serial suite: its leak
        census covers module data, fail mode, nothing leaked."""
        row, err = self.mint(self.evidence(v=gate.SLICE_DATA_AUDIT_EVIDENCE))
        self.assertIsNone(err, err)
        return row

    def reshaped(self, row, **auth):
        """`row` with its slice evidence changed. The predicate reads the
        row it is handed, so no mint is needed to ask it about this shape."""
        return dict(row, slice_authority=dict(row["slice_authority"], **auth))

    def verdict(self, verdict, tree, red=False, leak_mode="fail", n=0,
                host="sliced-node", minted=0):
        """One verdict of a planted pair, recorded as `compare` records it.
        `minted` is how many hours before now both receipts were minted (the
        receipts' own `ts`), or None for receipts that carry none."""
        serial = {"id": "serial-%d" % n, "tree": tree,
                  "status": "FAILED" if red else "OK",
                  "host": {"node": "serial-node"}}
        sliced = {"id": "sliced-%d" % n, "tree": tree,
                  "host": {"node": host},
                  "slice_authority": {"leak_mode": leak_mode}}
        if minted is not None:
            serial["ts"] = sliced["ts"] = stamp(minted)
        result = {"verdict": verdict, "reason": "planted %s" % verdict,
                  "shared_failures": 1 if red else 0,
                  "divergences": [] if verdict == gatecanary.AGREE
                  else [{"test": "tests.test_b.Case.test_b"}]}
        self.assertTrue(gatecanary.append_verdict(
            result, serial, sliced, gatecanary.COMPARE, self.home))

    def agree(self, count, first=0, hosts=None, leak_mode="fail"):
        """`count` AGREE verdicts on distinct trees, each sliced on its own
        host unless `hosts` names them."""
        for i in range(first, first + count):
            host = (hosts or [])[i - first] if hosts else "node-%d" % i
            self.verdict(gatecanary.AGREE, "%040x" % (i + 1), n=i,
                         leak_mode=leak_mode, host=host)

    def finder(self, each=True, whole=True, tree="c" * 40):
        self.assertTrue(gatecanary.finder_rows({
            "host": "finder-node",
            "each": {"clean": each, "modules": 2,
                     "leaking": {} if each else {
                         "tests.test_x": "environ: HELM_HOME"}},
            "whole": {"clean": whole, "ran": 2,
                      "failing": [] if whole else [
                          "FAIL tests.test_y.T.test_y"]}}, tree, self.home))

    def caught(self, missing=False):
        """One red tree through the real catch door: the serial run failed
        on test_b, and the sliced run failed on it too (or did not)."""
        serial = self.serial_row([B_FAILS])
        sliced = self.sliced_row([] if missing else [B_FAILS, AUDIT])
        result, why = gatecanary.catch_rows(serial, sliced, self.home)
        self.assertIsNone(why, why)
        return result

    def stand(self):
        """All four pieces of the rule, recorded."""
        self.finder()
        self.agree(AGREES)
        self.caught()

    def refusal(self, row):
        return gate.land_refusal(row, global_dir=self.home)


class TheDoorAdmitsASlicedReceiptOnlyOnTheCanarysEvidence(SlicedLandBase):

    def test_the_four_pieces_and_no_marker_admit_it(self):
        row = self.land_row()
        self.stand()
        self.assertIsNone(gate.sliced_land_disabled(self.home))
        self.assertIsNone(self.refusal(row))
        held = gatecanary.standing(self.home)
        self.assertEqual((held["agree"], len(held["hosts"]), held["met"],
                          held["finder"]["clean"],
                          held["interaction"]["clean"], len(held["red"])),
                         (AGREES, AGREES, True, True, True, 1))

    def test_the_marker_refuses_it_however_much_the_record_holds(self):
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stands
        gatecanary.write_marker(
            {"reason": "planted divergence", "divergences": []},
            {"tree": "f" * 40, "id": "s-marked"}, {"id": "l-marked"},
            self.home)
        why = self.refusal(row)
        self.assertIn("is a sliced receipt", why)
        self.assertIn("helm gate canary clear --reason", why)
        self.assertIn("s-marked", why)

    def test_i_no_finder_run_or_a_dirty_one_refuses_and_names_it(self):
        row = self.land_row()
        self.agree(AGREES)
        self.caught()
        why = self.refusal(row)
        self.assertIn("(i) no one-pass finder run (every module alone in a "
                      "fresh process) is recorded", why)
        self.assertIn("helm gate canary finder --repo <checkout>", why)
        self.finder(each=False)
        why = self.refusal(row)
        self.assertIn("(i) the newest one-pass finder run", why)
        self.assertIn("tests.test_x", why)
        self.finder()
        self.assertIsNone(self.refusal(row))        # the newest run decides

    def test_ii_an_interaction_in_the_sliced_whole_suite_refuses(self):
        row = self.land_row()
        self.stand()
        self.finder(whole=False)
        why = self.refusal(row)
        self.assertIn("(ii) the newest report-mode sliced whole suite", why)
        self.assertIn("not clean", why)
        self.assertNotIn("(i) ", why)

    def test_iii_too_few_trees_or_too_few_hosts_refuse(self):
        row = self.land_row()
        self.finder()
        self.caught()
        self.agree(AGREES - 1, hosts=["node-0"] * (AGREES - 1))
        why = self.refusal(row)
        self.assertIn("(iii) it holds %d tree(s)" % (AGREES - 1), why)
        self.assertIn("a sliced land needs %d trees on %d hosts"
                      % (AGREES, HOSTS), why)
        self.assertIn("helm gate canary compare <serial-id> <sliced-id>", why)
        self.agree(1, first=10, hosts=["node-0"])     # a host already counted
        why = self.refusal(row)
        self.assertIn("(iii) it holds %d tree(s)" % AGREES, why)
        self.assertIn("sliced on 1 host(s)", why)
        self.agree(HOSTS - 1, first=20,
                   hosts=["another-node-%d" % i for i in range(HOSTS - 1)])
        self.assertIsNone(self.refusal(row))

    def test_iv_no_caught_red_tree_refuses(self):
        row = self.land_row()
        self.finder()
        self.agree(AGREES)
        why = self.refusal(row)
        self.assertIn("(iv) no red tree is recorded", why)
        self.assertIn("helm gate canary catch <serial-id> <sliced-id>", why)
        missed = self.caught(missing=True)
        self.assertEqual((missed["caught"], missed["missing"]),
                         (False, ["FAIL %s" % B_FAILS[1]]))
        self.assertIn("(iv) no red tree", self.refusal(row))
        self.verdict(gatecanary.AGREE, "%040x" % 99, red=True, n=99)
        self.assertIsNone(self.refusal(row))        # a red AGREE is a catch

    def test_a_divergence_restarts_every_piece(self):
        """THE RULE: everything counts from the newest DIVERGED, so one
        recorded after the last AGREE leaves nothing standing."""
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stood
        self.verdict(gatecanary.DIVERGED, "e" * 40, n=50)
        why = self.refusal(row)
        for piece in ("(i) no one-pass finder run", "(ii) no report-mode",
                      "(iii) it holds 0 tree(s)", "(iv) no red tree"):
            self.assertIn(piece, why)
        self.assertIn("since the DIVERGED recorded at", why)
        self.assertIn("e" * 12, why)
        self.stand()
        self.assertIsNone(self.refusal(row))

    def test_a_tree_compared_twice_counts_once(self):
        row = self.land_row()
        self.finder()
        self.caught()
        self.agree(AGREES - 1)
        self.verdict(gatecanary.AGREE, "%040x" % 1, n=77, host="fresh-node")
        self.assertIn("(iii) it holds %d tree(s)" % (AGREES - 1),
                      self.refusal(row))

    def test_an_unknown_neither_counts_nor_restarts_the_count(self):
        row = self.land_row()
        self.finder()
        self.caught()
        self.agree(AGREES - 1)
        self.verdict(gatecanary.UNKNOWN, "d" * 40, n=60)
        self.assertIn("(iii) it holds %d tree(s)" % (AGREES - 1),
                      self.refusal(row))
        self.agree(1, first=300)
        self.verdict(gatecanary.UNKNOWN, "d" * 40, n=61)
        self.assertIsNone(self.refusal(row))

    def test_an_agreement_from_a_report_mode_sliced_run_does_not_count(self):
        row = self.land_row()
        self.finder()
        self.caught()
        self.agree(AGREES, leak_mode="report")
        self.assertIn("(iii) it holds 0 tree(s)", self.refusal(row))

    def test_the_receipt_itself_must_be_able_to_stand_for_serial(self):
        self.stand()
        row = self.land_row()
        self.assertIsNone(self.refusal(row))                 # control
        for auth, needle in (({"v": 1}, "predates the module-data audit"),
                             ({"leaks": 1}, "1 leaking module"),
                             ({"leak_mode": "report"}, "'report' mode")):
            with self.subTest(**auth):
                self.assertIn(needle, self.refusal(self.reshaped(row, **auth)))



class ASilentCanaryRefuses(SlicedLandBase):
    """(v) THE CANARY MUST STILL BE SPEAKING. Nothing in (i)-(iv) expires,
    so a canary that stops producing verdicts (its timer gone, its host off,
    its run crashing before the compare every night, a DIVERGED that reached
    neither the marker nor the record) would leave sliced lands admitted for
    good. The freshest evidence a verdict of any source compared, dated by
    the older of its two receipts' own stamps (never by when the verdict was
    written), must be minted within 36 hours, one nightly run plus slack,
    and not ahead of this host's clock; HELM_GATE_CANARY_MAX_AGE_H may only
    tighten the bound. Past it the door refuses and names the silence. A
    DIVERGED or the marker still closes the door whatever the age."""

    def setUp(self):
        super().setUp()
        bound = mock.patch.dict(os.environ)
        bound.start()
        self.addCleanup(bound.stop)
        os.environ.pop(BOUND_ENV, None)

    def age(self, hours):
        age_record(gatecanary.history_path(self.home), hours)

    def newest_compared_at(self):
        """When the freshest evidence any compared verdict holds was minted
        (a record written before verdicts carried it: when it was
        recorded)."""
        rows = gate.eventledger.checked_events(
            gatecanary.history_path(self.home), strict=True)[0]
        return max(r.get("evidence_at") or r["at"] for r in rows
                   if r.get("verdict") in (gatecanary.AGREE,
                                           gatecanary.DIVERGED))

    def test_a_record_silent_past_the_bound_refuses_and_names_it(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone is the open control; the refusal after aging is asserted by its content
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stands
        self.age(37)
        why = self.refusal(row)
        self.assertIn("(v) the canary has been silent since %s"
                      % self.newest_compared_at(), why)
        self.assertIn("over the 36 h bound;", why)
        self.assertNotIn(BOUND_ENV, why)
        self.assertFalse(gatecanary.standing(self.home)["met"])

    def test_a_record_inside_the_bound_admits(self):
        row = self.land_row()
        self.stand()
        self.age(35)
        self.assertIsNone(self.refusal(row))
        self.age(2)                                 # control: 37 h refuses
        self.assertIn("(v) the canary has been silent since",
                      self.refusal(row))

    def test_a_stamp_in_the_future_is_not_fresh(self):
        """A stamp this host's clock has not reached (a clock that ran ahead
        when the verdict was written, then was set back) says nothing about
        when the canary last spoke. Read as a negative age it would sit
        inside every bound and admit slices until the clock caught up, so it
        refuses, and the refusal says the stamp is ahead of the clock."""
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stands
        self.age(-1000)
        why = self.refusal(row)
        self.assertIsNotNone(why)
        self.assertIn("(v) the canary's newest verdict that compared a "
                      "serial and a sliced run", why)
        self.assertIn("is stamped %s, 1000.0 h ahead of this host's clock"
                      % self.newest_compared_at(), why)
        self.assertFalse(gatecanary.standing(self.home)["met"])
        out = io.StringIO()
        with mock.patch.object(gatecanary.home, "global_dir",
                               return_value=self.home), \
                contextlib.redirect_stdout(out):
            gatecanary.cmd(["status"])
        self.assertIn("(compare, AGREE), 1000.0 h AHEAD of this host's clock",
                      out.getvalue())
        self.assertNotIn("h ago", out.getvalue())

    def test_a_future_dated_verdict_does_not_stay_fresh(self):
        row = self.land_row()
        self.stand()
        self.age(-48)
        why = self.refusal(row)
        self.assertIn("dated", why)
        self.assertIn("ahead of this host's clock", why)

    def test_the_bound_may_only_tighten_and_a_bad_value_keeps_it(self):  # noqa: VACUOUS_ASSERTION — the assertIsNone control precedes each refusal, asserted by its bound
        """The config can only make the canary stricter: a value above the
        default is clamped to it, so no operator setting can keep an old
        record fresh. The refusal never names the knob, so it does not read
        as the way out."""
        row = self.land_row()
        self.stand()
        self.age(13)
        self.assertIsNone(self.refusal(row))                      # control
        with mock.patch.dict(os.environ, {BOUND_ENV: "12"}):
            why = self.refusal(row)
        self.assertIn("over the 12 h bound", why)
        self.assertNotIn(BOUND_ENV, why)
        self.age(24)
        with mock.patch.dict(os.environ, {BOUND_ENV: "48"}):
            why = self.refusal(row)
        self.assertIn("over the 36 h bound", why)
        self.assertNotIn(BOUND_ENV, why)
        for bad in ("soon", "0", "-5", "nan", "inf"):
            with self.subTest(bound=bad), \
                    mock.patch.dict(os.environ, {BOUND_ENV: bad}):
                self.assertIn("over the 36 h bound", self.refusal(row))

    def test_re_comparing_stale_evidence_refreshes_nothing(self):
        """(v) is measured when the EVIDENCE was produced, the older of the
        two compared receipts' own stamps, never when a verdict was written:
        a `compare` of an old pair records a new line now, and that line may
        not keep the record fresh."""
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stands
        self.age(37)
        self.assertIn("(v) the canary has been silent since",
                      self.refusal(row))
        self.verdict(gatecanary.AGREE, "%040x" % 1, n=500, minted=37)
        self.assertIn("(v) the canary has been silent since",
                      self.refusal(row))
        self.verdict(gatecanary.AGREE, "%040x" % 2, n=501, minted=None)
        self.assertIn("(v) the canary has been silent since",
                      self.refusal(row))
        self.agree(1, first=600)                    # a pair minted now
        self.assertIsNone(self.refusal(row))

    def test_a_record_whose_verdicts_name_no_evidence_time_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted by its words, and the fresh pair's admission is the control
        """A verdict whose receipts carry no stamp (a record written before
        verdicts named when their evidence was minted) cannot show when the
        canary last produced evidence, so it refreshes nothing."""
        row = self.land_row()
        self.finder()
        self.caught()
        for i in range(AGREES):
            self.verdict(gatecanary.AGREE, "%040x" % (i + 1), n=i,
                         host="node-%d" % i, minted=None)
        why = self.refusal(row)
        self.assertIn("(v) no verdict in the canary record names when the "
                      "receipts it compared were minted", why)
        self.agree(1, first=700)
        self.assertIsNone(self.refusal(row))

    def test_only_a_verdict_that_compared_a_pair_refreshes_it(self):
        """An UNKNOWN compared nothing (a run that crashes before its compare
        records one every night), and a finder run or a catch is no verdict:
        none of them refreshes the record. A fresh AGREE does."""
        row = self.land_row()
        self.stand()
        self.age(37)
        self.verdict(gatecanary.UNKNOWN, "d" * 40, n=60)
        self.finder()
        self.caught()
        self.assertIn("(v) the canary has been silent since",
                      self.refusal(row))
        self.agree(1, first=400)
        self.assertIsNone(self.refusal(row))

    def test_status_prints_the_age_of_the_newest_compared_verdict(self):  # noqa: VACUOUS_ASSERTION — the age line is matched whole, number included
        self.stand()
        self.age(5)
        out = io.StringIO()
        with mock.patch.object(gatecanary.home, "global_dir",
                               return_value=self.home), \
                contextlib.redirect_stdout(out):
            gatecanary.cmd(["status"])
        self.assertRegex(
            out.getvalue(), r"freshness: the freshest compared evidence was "
            r"minted at \S+Z \(compare, AGREE\), 5\.\d h ago; recorded at "
            r"\S+Z; the bound is 36 h \(%s may only tighten it\)" % BOUND_ENV)


class AnUnreadableRecordRefuses(SlicedLandBase):

    def test_a_line_that_is_not_a_verdict_refuses_and_names_it(self):
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))        # control: it stood
        entries = len(gate.eventledger.checked_events(
            gatecanary.history_path(self.home), strict=True)[0])
        with open(gatecanary.history_path(self.home), "a") as fh:
            fh.write(json.dumps({"id": "planted", "v": 1,
                                 "verdict": "MAYBE"}) + "\n")
        why = self.refusal(row)
        self.assertIn("cannot be read whole", why)
        self.assertIn("entry %d is not a verdict" % (entries + 1), why)

    def test_a_record_that_is_not_json_refuses(self):
        row = self.land_row()
        self.stand()
        with open(gatecanary.history_path(self.home), "a") as fh:
            fh.write("{half a verdict\n")
        self.assertIn("cannot be read whole", self.refusal(row))

    def test_a_record_that_is_not_a_file_refuses(self):
        row = self.land_row()
        os.makedirs(gatecanary.history_path(self.home))
        self.assertIn("cannot be read whole", self.refusal(row))

    def test_a_reader_that_raises_refuses(self):
        row = self.land_row()
        self.stand()
        with mock.patch.object(gatecanary, "standing",
                               side_effect=RuntimeError("planted")):
            why = self.refusal(row)
        self.assertIn("could not be read (RuntimeError: planted)", why)

    def test_no_record_at_all_names_every_missing_piece(self):
        why = self.refusal(self.land_row())
        for piece in ("(i) no one-pass finder run", "(ii) no report-mode",
                      "(iii) it holds 0 tree(s)", "(iv) no red tree"):
            self.assertIn(piece, why)


class ASerialReceiptIsJudgedAsBefore(SlicedLandBase):

    def test_no_state_of_the_canary_changes_a_serial_receipts_answer(self):
        serial = self.serial_row()
        self.assertIsNone(self.refusal(serial))
        gatecanary.write_marker({"reason": "planted", "divergences": []},
                                {"tree": "f" * 40}, {}, self.home)
        self.assertIsNone(self.refusal(serial))
        os.makedirs(gatecanary.history_path(self.home))
        self.assertIsNone(self.refusal(serial))
        self.assertIsNone(gate.land_refusal(serial))


class TheRecordIsWrittenByEveryVerdict(SlicedLandBase):

    def test_a_judged_red_pair_is_a_red_agreeing_tree(self):
        serial, sliced = self.serial_row([B_FAILS]), self.sliced_row([B_FAILS])
        result = gatecanary.judge(serial, sliced, self.home)
        self.assertEqual(result["verdict"], gatecanary.AGREE, result)
        held = gatecanary.standing(self.home)
        self.assertEqual((held["trees"], held["red"]),
                         ([serial["tree"]], [serial["tree"]]))

    def test_a_catch_on_an_uncured_red_tree_ignores_its_audit_rows(self):
        """The red tree may predate a leak's cure, so its sliced run carries
        a LeakAudit error: the compare DIVERGES, and the catch still reads
        only whether every real failure serial caught was caught."""
        serial = self.serial_row([B_FAILS])
        sliced = self.sliced_row([B_FAILS, AUDIT])
        self.assertEqual(gatecanary.compare(serial, sliced, {
            ("FAIL", B_FAILS[1]): 1}, {("FAIL", B_FAILS[1]): 1,
                                       ("ERROR", AUDIT[1]): 1})["verdict"],
            gatecanary.DIVERGED)
        result, why = gatecanary.catch_rows(serial, sliced, self.home)
        self.assertIsNone(why, why)
        self.assertEqual((result["caught"], result["real_failures"]),
                         (True, 1))
        self.assertEqual(gatecanary.standing(self.home)["red"],
                         [serial["tree"]])
        self.assertIsNone(gatecanary.standing(self.home)["last_diverged"])

    def test_the_catch_verb_records_and_exits_by_it(self):
        serial = self.serial_row([B_FAILS])
        sliced = self.sliced_row([B_FAILS])
        out = io.StringIO()
        self.home = gate.home.global_dir()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gatecanary.cmd(
                ["catch", serial["id"], sliced["id"]]), 0)
        self.assertIn("CAUGHT — 1 real failure(s)", out.getvalue())

    def test_compare_records_its_verdict_and_a_divergence_marks(self):
        serial, sliced = self.serial_row(), self.land_row()
        out = io.StringIO()
        # The verb reads the receipts and writes the record where the fixture
        # home's own helm keeps them.
        self.home = gate.home.global_dir()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gatecanary.cmd(
                ["compare", serial["id"], sliced["id"]]), 0)
        held = gatecanary.standing(self.home)
        self.assertEqual((held["agree"], held["red"]), (1, []))
        # A compare never tells the nightly run that trunk was judged.
        self.assertFalse(os.path.exists(gatecanary.last_path(self.home)))
        red = self.serial_row([B_FAILS])
        with contextlib.redirect_stdout(out):
            self.assertEqual(gatecanary.cmd(
                ["compare", red["id"], sliced["id"]]), 1)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))
        self.assertIsNotNone(gatecanary.standing(self.home)["last_diverged"])

    def test_status_prints_what_the_door_would_answer(self):
        self.finder()
        self.agree(AGREES - 1)
        out = io.StringIO()
        with mock.patch.object(gatecanary.home, "global_dir",
                               return_value=self.home), \
                contextlib.redirect_stdout(out):
            self.assertEqual(gatecanary.cmd(["status"]), 0)
        self.assertIn("record: finder CLEAN, interaction CLEAN, %d of %d "
                      "agreeing tree(s) on %d of %d host(s), red caught: "
                      "none" % (AGREES - 1, AGREES, AGREES - 1, HOSTS),
                      out.getvalue())
        self.assertIn("refuses a sliced receipt", out.getvalue())


class TheFinderRecordsBothMeasurements(SlicedLandBase):
    """`helm gate canary finder` runs `gateslice.py --finder` on a peek of the
    tree through the finder launcher and records what it brings back; a
    launch that brings back nothing records nothing."""

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
        drop.start()
        self.addCleanup(drop.stop)
        self.argvs = []

    def runner(self, line):
        def run(argv, log, _env):
            self.argvs.append(argv)
            with open(log, "a") as fh:
                fh.write("fab: some fab chatter\n")
                if line is not None:
                    fh.write(json.dumps(line) + "\n")
            return 0
        return run

    def test_a_clean_result_is_two_clean_rows_of_the_trunk_tree(self):
        line = {"event": gateslice.FINDER_EVENT, "host": "finder-node",
                "each": {"clean": True, "modules": 2},
                "whole": {"clean": True, "ran": 2}}
        rc, text = gatecanary.finder(self.repo, global_dir=self.home,
                                     runner=self.runner(line))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.argvs, [gatecanary.finder_launcher() + [
            "--repo", self.room, "--", "python3", gate.SLICE_RUNNER,
            gateslice.FINDER_FLAG]])
        held = gatecanary.standing(self.home)
        tree = self._git("rev-parse", "HEAD^{tree}")
        self.assertEqual((held["finder"]["tree"], held["finder"]["clean"],
                          held["interaction"]["clean"],
                          held["finder"]["host"]),
                         (tree, True, True, "finder-node"))
        self.assertIn("each CLEAN; whole CLEAN", text)

    def test_a_launch_with_no_result_records_nothing(self):
        rc, text = gatecanary.finder(self.repo, global_dir=self.home,
                                     runner=self.runner(None))
        self.assertEqual(rc, 3)
        self.assertIn("brought back no finder result; nothing was recorded",
                      text)
        self.assertIsNone(gatecanary.standing(self.home)["finder"])


class TheFinderRunnerMeasuresEachModuleAlone(SliceFixture):
    """`gateslice.py --finder`: every module alone in a fresh process, then
    one report-mode sliced whole suite, reported as one JSON line."""

    def finder(self):
        env = dict(os.environ, HELM_GATE_SUITE_CAP="1")
        for key in gateslice.PARENT_ONLY_ENV + ("HELM_GATESLICE_WORKERS",
                                                "HELM_GATESLICE_LEAKS"):
            env.pop(key, None)
        proc = subprocess.run(
            [gate.interpreter()["executable"],
             os.path.join(self.repo, *gate.SLICE_RUNNER.split("/")),
             gateslice.FINDER_FLAG, "--jobs", "2"],
            cwd=self.repo, env=env, capture_output=True, text=True,
            timeout=600)
        lines = [json.loads(line) for line in proc.stdout.splitlines()
                 if line.startswith("{")]
        self.assertEqual(len(lines), 1, proc.stderr[-2000:])
        self.assertEqual(lines[0]["event"], gateslice.FINDER_EVENT)
        self.assertIn(gateslice.DIAGNOSTIC_MARKER, proc.stderr)
        return proc.returncode, lines[0]

    def test_a_clean_tree_is_clean_both_ways(self):
        rc, got = self.finder()
        self.assertEqual(rc, 0, got)
        self.assertEqual((got["each"]["clean"], got["each"]["modules"],
                          got["whole"]["clean"], got["whole"]["ran"]),
                         (True, len(self.labels), True, 2))
        self.assertEqual(got["host"], os.uname().nodename)

    def test_a_module_that_leaves_state_is_named_by_each(self):
        with open(os.path.join(self.repo, "tests", "test_c.py"), "w") as fh:
            fh.write("import os, unittest\nclass Case(unittest.TestCase):\n"
                     "    def test_c(self):\n"
                     "        os.environ['FINDER_LEFT_BEHIND'] = '1'\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "a module that leaks")
        rc, got = self.finder()
        self.assertEqual(rc, 1, got)
        self.assertFalse(got["each"]["clean"])
        self.assertIn("FINDER_LEFT_BEHIND",
                      got["each"]["leaking"]["tests.test_c"])
        self.assertEqual(sorted(got["each"]["leaking"]), ["tests.test_c"])
        self.assertIn("tests.test_c", got["whole"]["leaking"])


class TheCanaryRunsWhereItsConfigSays(SlicedLandBase):

    def test_the_host_is_config_and_the_launcher_names_it(self):
        with mock.patch.dict(os.environ, {gatecanary.HOST_ENV: "night-node"}):
            os.environ.pop(gatecanary.LAUNCH_ENV, None)
            self.assertEqual(gatecanary.launcher(),
                             ["fab", "gate", "--host", "night-node"])
            os.environ[gatecanary.LAUNCH_ENV] = "fab gate"
            self.assertEqual(gatecanary.launcher(), ["fab", "gate"])
        with mock.patch.dict(os.environ, {gatecanary.HOST_ENV: "no;way"}):
            os.environ.pop(gatecanary.LAUNCH_ENV, None)
            self.assertEqual(gatecanary.launcher(),
                             list(gatecanary.DEFAULT_LAUNCH))

    def test_install_timer_host_writes_the_config_the_service_reads(self):
        fake_home = os.path.join(self.tmp, "home")
        xdg = os.path.join(self.tmp, "xdg")
        with mock.patch.dict(os.environ, {"HOME": fake_home}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(gatecanary.config_env_path(), os.path.join(
                fake_home, ".config", "helm", "gate-canary.env"))
            os.environ["XDG_CONFIG_HOME"] = xdg
            self.assertEqual(gatecanary.config_env_path(),
                             os.path.join(xdg, "helm", "gate-canary.env"))
            ok, detail = gatecanary.write_host("night-node")
            self.assertTrue(ok, detail)
            self.assertFalse(os.path.exists(os.path.join(fake_home,
                                                         ".config")))
            with open(gatecanary.config_env_path()) as fh:
                self.assertEqual(fh.read(), "%s=night-node\n"
                                 % gatecanary.HOST_ENV)
            service = gatecanary.timer_units()[1]
            self.assertIn("EnvironmentFile=-%s" % gatecanary.config_env_path(),
                          service)
            self.assertNotIn("night-node", service)
            self.assertFalse(gatecanary.write_host("a b")[0])


class ADivergenceSendsLandsBackToSerial(SlicedLandBase):
    """The safety net: after the flip, the nightly canary keeps comparing,
    and its first DIVERGED writes the marker that refuses every sliced
    receipt at the land door again, with no person in the loop."""

    def test_a_planted_divergence_from_the_nightly_run_disables_the_door(self):
        row = self.land_row()
        # The standing, recorded without minting a serial receipt of this
        # tree, so the night's run finds it lacks one and gates it.
        self.finder()
        self.agree(AGREES)
        self.verdict(gatecanary.AGREE, "%040x" % 99, red=True, n=99)
        self.assertIsNone(self.refusal(row))        # the flip stands
        self._git("branch", "-f", "main", "HEAD")
        room = os.path.join(self.tmp, "peek-room")
        with mock.patch("helm.work._gc.refresh_trunk",
                        return_value=(True, "local")), \
                mock.patch("helm.work.peek",
                           return_value=(0, {"path": room,
                                             "sha": self.head})), \
                mock.patch("helm.work.peek_drop", return_value=(0, [])):
            launched = []

            def runner(argv, log, env):
                # The night's serial suite fails where the slices did not.
                launched.append(argv[-1])
                self.serial_row([B_FAILS])
                return 0
            result = gatecanary.run(self.repo, self.home, runner=runner)
        self.assertEqual(launched, ["--serial"])
        self.assertEqual(result["verdict"], gatecanary.DIVERGED, result)
        why = self.refusal(row)
        self.assertIn("the gate canary saw serial and sliced disagree", why)
        self.assertIn("helm gate canary clear --reason", why)

    def test_a_marker_write_failure_still_records_the_divergence(self):  # noqa: VACUOUS_ASSERTION — prior no-refusal control proves it was open
        """One broken marker path cannot leave prior standing in force."""
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))
        serial = {"id": "serial-broken-marker", "tree": "f" * 40,
                  "status": "FAILED", "host": {"node": "serial-node"}}
        sliced = {"id": "sliced-broken-marker", "tree": "f" * 40,
                  "host": {"node": "sliced-node"},
                  "slice_authority": {"leak_mode": "fail"}}
        result = {"verdict": gatecanary.DIVERGED,
                  "reason": "planted divergence",
                  "divergences": [{"test": "tests.test_b.Case.test_b"}]}
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("planted marker failure")):
            gatecanary.record(result, serial, sliced, self.home,
                              source=gatecanary.COMPARE)
        why = self.refusal(row)
        self.assertIn("since the DIVERGED recorded at", why)
        self.assertIn("it holds 0 tree(s)", why)

    def test_clearing_a_marker_cannot_restore_pre_divergence_standing(self):
        row = self.land_row()
        self.stand()
        self.assertIsNone(self.refusal(row))
        with mock.patch.object(gatecanary, "append_verdict",
                               return_value=False):
            gatecanary.record(*self.broken(), self.home,
                              source=gatecanary.COMPARE)
        self.assertIsNotNone(self.refusal(row))       # the marker holds
        with mock.patch.object(gatecanary, "append_verdict",
                               return_value=False):
            rc, line = gatecanary.clear_marker(
                "the divergence was understood", self.home)
        self.assertEqual(rc, 1, line)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))
        rc, line = gatecanary.clear_marker("the divergence was understood",
                                           self.home)
        self.assertEqual(rc, 0, line)
        why = self.refusal(row)
        self.assertIsNotNone(why)
        self.assertIn("since the DIVERGED recorded at", why)

    def broken(self, tree="f" * 40):
        """A planted DIVERGED: its result and its two receipts."""
        serial = {"id": "serial-broken", "tree": tree, "status": "FAILED",
                  "host": {"node": "serial-node"}}
        sliced = {"id": "sliced-broken", "tree": tree,
                  "host": {"node": "sliced-node"},
                  "slice_authority": {"leak_mode": "fail"}}
        result = {"verdict": gatecanary.DIVERGED,
                  "reason": "planted divergence",
                  "divergences": [{"test": "tests.test_b.Case.test_b",
                                   "serial": "FAIL x1", "sliced": "not FAIL",
                                   "kind": "serial-only"}]}
        return result, serial, sliced

    def record_heard(self, post, source):
        """`record` of the planted DIVERGED with the room and stderr caught.
        -> ([(text, kwargs) posted], stderr)"""
        posts, err = [], io.StringIO()
        with mock.patch.object(gatecanary.chat, "post", side_effect=(
                lambda text, **kw: posts.append((text, kw)) or True)), \
                contextlib.redirect_stderr(err):
            gatecanary.record(*self.broken(), self.home, post=post,
                              source=source)
        return posts, err.getvalue()

    def test_no_durable_veto_is_said_loudly_on_every_path(self):  # noqa: VACUOUS_ASSERTION — the two sources are a literal list, and each asserts the loud row count EQUAL to one
        """The marker AND the record both failed, so nothing durable closes
        sliced land. The words reach stderr and the room, as a row that is
        not ambient, on the compare path and the nightly one alike, and the
        night's alert never claims sliced-at-land is DISABLED."""
        for source, post in ((gatecanary.COMPARE, False),
                             (gatecanary.RUN, True)):
            with self.subTest(source=source), \
                    mock.patch.object(gatecanary, "write_marker",
                                      side_effect=OSError("planted")), \
                    mock.patch.object(gatecanary, "append_verdict",
                                      return_value=False):
                posts, err = self.record_heard(post, source)
                self.assertIn("NO DURABLE VETO", err)
                loud = [(t, kw) for t, kw in posts if "NO DURABLE VETO" in t]
                self.assertEqual(len(loud), 1, posts)
                self.assertFalse(loud[0][1].get("ambient"), loud)
                self.assertFalse([t for t, _kw in posts
                                  if "is DISABLED while" in t], posts)

    def test_a_failed_marker_beside_a_recorded_divergence_is_said(self):
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("planted")):
            posts, err = self.record_heard(False, gatecanary.COMPARE)
        self.assertIn("the DISABLE marker could not be written (OSError: "
                      "planted)", err)
        self.assertIn("the DIVERGED is recorded in %s, which closes sliced "
                      "land" % gatecanary.history_path(self.home), err)
        self.assertEqual(posts, [])             # compare posts nothing
        self.assertIsNotNone(
            gatecanary.standing(self.home)["last_diverged"])

    def test_the_nights_alert_survives_an_unwritable_last_json(self):
        write = gatecanary.pk.write_json

        def refuse_last(path, *args, **kw):
            if path == gatecanary.last_path(self.home):
                raise OSError("planted last.json failure")
            return write(path, *args, **kw)
        with mock.patch.object(gatecanary.pk, "write_json",
                               side_effect=refuse_last):
            posts, err = self.record_heard(True, gatecanary.RUN)
        self.assertEqual(len([t for t, _kw in posts
                              if t.startswith("[gate canary] DIVERGED")]), 1,
                         posts)
        self.assertIn("could not write %s" % gatecanary.last_path(self.home),
                      err)
        self.assertIsNotNone(gate.sliced_land_disabled(self.home))

    def test_compare_says_no_durable_veto(self):
        serial, sliced = self.serial_row([B_FAILS]), self.land_row()
        self.home = gate.home.global_dir()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("planted")), \
                mock.patch.object(gatecanary, "append_verdict",
                                  return_value=False), \
                mock.patch.object(gatecanary.chat, "post",
                                  return_value=True), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = gatecanary.cmd(["compare", serial["id"], sliced["id"]])
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("NO DURABLE VETO", err.getvalue())


class TheLandDoorsAskTheSamePredicate(SlicedLandBase):
    """The one predicate reaches every door that authorizes a land."""

    def test_bind_for_a_land_takes_the_sliced_receipt_once_it_stands(self):
        # The record the door reads by default: this fixture home's own.
        self.home = gate.home.global_dir()
        row = self.land_row()
        handle = "gate:" + row["id"]
        state, _rid, why = gate.bind(handle, self.head, need=gate.NEED_LAND)
        self.assertEqual(state, "REFUSED")
        self.assertIn("is a sliced receipt", why)
        self.stand()
        state, _rid, why = gate.bind(handle, self.head, need=gate.NEED_LAND)
        self.assertNotIn("is a sliced receipt", str(why))


class TheCanaryTimerRidesTheRail(HookBase):
    """`helm work install-guard --apply` installs the nightly canary's timer
    with the rail, idempotently, and `helm doctor` shows it. The host's
    systemctl is a spy and HOME is the fixture's, so nothing here changes
    this host's scheduler."""

    def setUp(self):
        super().setUp()
        self.calls = []
        real_which = shutil.which

        def which(name, *a, **kw):
            if name == "systemctl":
                return self.systemctl
            return real_which(name, *a, **kw)
        self.systemctl = "/usr/bin/systemctl"
        # The canary module's own subprocess seam, never the shared module:
        # the install's git reads run for real.
        for patch in (mock.patch("shutil.which", side_effect=which),
                      mock.patch.object(gatecanary, "subprocess",
                                        types.SimpleNamespace(
                                            run=self._run,
                                            TimeoutExpired=(
                                                subprocess.TimeoutExpired)))):
            patch.start()
            self.addCleanup(patch.stop)
        # ONE KEY, restored by its own cleanup: a patch.dict of the whole
        # environment stopped after HookBase's tearDown would put the
        # fixture's HOME and homes back over the restore.
        prior = os.environ.pop(gatecanary.TIMER_ENV, None)
        self.addCleanup(_put_env, gatecanary.TIMER_ENV, prior)
        self.units = gatecanary.timer_units()

    def _run(self, argv, **_kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_the_rail_installs_the_timer_and_a_second_install_changes_nothing(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("helm work: gate canary timer installed nightly at %s"
                      % gatecanary.TIMER_AT, out)
        spath, service, tpath, timer = self.units
        self.assertTrue(spath.startswith(os.environ["HOME"]), spath)
        with open(spath) as fh:
            self.assertEqual(fh.read(), service)
        self.assertIn("gate canary run", service)
        with open(tpath) as fh:
            self.assertIn("OnCalendar=*-*-* %s" % gatecanary.TIMER_AT,
                          fh.read())
        self.assertEqual(self.calls, [
            [self.systemctl, "--user", "daemon-reload"],
            [self.systemctl, "--user", "enable", "--now",
             gatecanary.TIMER_NAME]])
        stamps = [os.stat(p).st_mtime_ns for p in (spath, tpath)]
        self.calls[:] = []
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("gate canary timer already installed, unchanged", out)
        self.assertEqual([os.stat(p).st_mtime_ns for p in (spath, tpath)],
                         stamps)
        self.assertEqual(self.calls, [[self.systemctl, "--user", "enable",
                                       "--now", gatecanary.TIMER_NAME]])
        rows = doctor.check_gate_canary(self.root)
        self.assertEqual([r[0] for r in rows], [doctor.OK], rows)
        self.assertIn("%s installed, nightly at" % gatecanary.TIMER_NAME,
                      rows[0][1])
        self.assertIn("record 0 of %d agreeing tree(s)" % AGREES, rows[0][1])
        self.assertIn("the land door refuses a sliced receipt", rows[0][1])

    def test_the_dry_run_names_the_timer_and_touches_nothing(self):
        rc, out, err = self.cli("install-guard")
        self.assertEqual(rc, 0, err)
        self.assertIn("would also install or refresh %s"
                      % gatecanary.TIMER_NAME, out)
        self.assertFalse(os.path.exists(self.units[2]))
        self.assertEqual(self.calls, [])

    def test_a_host_without_systemd_still_gets_the_rail_and_is_told(self):
        self.systemctl = None
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("gate canary timer NOT installed (systemctl "
                      "unavailable", out)
        self.assertTrue(os.path.exists(_guard.hook_path(self.root,
                                                        "pre-commit")))
        rows = doctor.check_gate_canary(self.root)
        self.assertEqual([r[0] for r in rows], [doctor.WARN], rows)
        self.assertIn("is NOT installed", rows[0][1])
        self.assertIn("helm work install-guard --apply --profile rail",
                      rows[0][1])

    def test_the_switch_keeps_the_scheduler_untouched(self):
        os.environ[gatecanary.TIMER_ENV] = "0"
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("gate canary timer not installed — install skipped by "
                      "%s=0" % gatecanary.TIMER_ENV, out)
        self.assertFalse(os.path.exists(self.units[2]))
        self.assertEqual(self.calls, [])

    def test_the_leak_profile_runs_no_canary(self):
        rc, out, err = self.cli("install-guard", "--apply", "--profile",
                                "leak")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("gate canary", out)
        self.assertFalse(os.path.exists(self.units[2]))
        self.assertEqual(doctor.check_gate_canary(self.root), [])


def _put_env(key, value):
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
