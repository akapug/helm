#!/usr/bin/env python3
"""helm ready — the five-signal fleet-readiness gauge (#163).

The gauge is a READER over five existing authorities, and these tests pin
exactly that: every signal has a green, red (where one exists), and UNKNOWN
arm; the composition law (one red = NOT READY, no red + one UNKNOWN =
UNKNOWN, all green = READY, walled family = READY-with-note); the exit-code
contract (0/1/2, distinct on purpose); and the NO-FRESH-SWEEP pin — signal 4
must read proxywatch's recorded state, so its sweep machinery is
booby-trapped here and the mutation "make signal 4 sweep" turns this file
red instead of spending 8 upstream tokens per family per gauge.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-ready-", var="HELM_HOME")

from helm import proxywatch, ready, web, web_ui_loader  # noqa: E402


def _out(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


class _Adapter:
    """A fake metaharness adapter — the panes()/list() surface only."""

    def __init__(self, name="orca", panes=None, listed=None, err=None):
        self.name = name
        if panes is not None or err is not None:
            self.panes = lambda: (panes or [], err)
        if listed is not None:
            self.list = lambda: listed


class _NoPanesAdapter:
    """herdr-shaped: a CLI list(), no RPC panes surface."""

    def __init__(self, listed=None, boom=None):
        self.name = "herdr"
        self._listed, self._boom = listed, boom

    def list(self):
        if self._boom:
            raise RuntimeError(self._boom)
        return self._listed


class DaemonSignalTest(unittest.TestCase):
    def test_no_metaharness_is_UNKNOWN_not_red(self):
        row = ready.signal_daemon(detect=lambda: None)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("unmeasurable", row["evidence"])

    def test_detection_crash_is_UNKNOWN(self):
        def boom():
            raise OSError("PATH scan died")
        row = ready.signal_daemon(detect=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("PATH scan died", row["evidence"])

    def test_daemon_answering_is_GREEN_with_the_pane_count(self):
        ad = _Adapter(panes=[{"handle": "t1"}, {"handle": "t2"}])
        row = ready.signal_daemon(detect=lambda: ad)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("2 panes listed", row["evidence"])

    def test_daemon_dark_is_RED_with_the_adapters_own_reason_and_a_repair(self):
        ad = _Adapter(err="orca rpc terminal.list: no usable unix transport")
        row = ready.signal_daemon(detect=lambda: ad)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("no usable unix transport", row["evidence"])
        self.assertIn("start orca", row["repair"])

    def test_an_adapter_without_the_rpc_surface_probes_via_its_cli_list(self):
        row = ready.signal_daemon(detect=lambda: _NoPanesAdapter(listed=[{}]))
        self.assertEqual(row["state"], ready.GREEN)
        red = ready.signal_daemon(
            detect=lambda: _NoPanesAdapter(boom="daemon not running"))
        self.assertEqual(red["state"], ready.RED)
        self.assertIn("daemon not running", red["evidence"])


def _lv(state, evidence):
    return {"state": state, "evidence": evidence, "blocked_on": None,
            "detail": None}


class SeatsSignalTest(unittest.TestCase):
    def test_blind_register_refuses_the_whole_signal(self):
        """A partly-readable register renders NO list — a short one would
        read as complete (seat._rebind and doctor._is_genesis precedent)."""
        row = ready.signal_seats(registered=lambda: (["codex"], True))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("PARTLY unreadable", row["evidence"])
        self.assertNotIn("codex", row["evidence"])

    def test_register_crash_is_UNKNOWN(self):
        def boom():
            raise OSError("seats dir vanished")
        row = ready.signal_seats(registered=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("seats dir vanished", row["evidence"])

    def test_empty_register_is_GREEN_with_the_honest_note(self):
        row = ready.signal_seats(registered=lambda: ([], False))
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("register", row["note"])

    def test_every_pane_live_is_GREEN_including_blocked_and_adopted_states(self):
        states = {"a": "RUNNING", "b": "IDLE", "c": "BLOCKED_ON_HUMAN",
                  "d": "LIVE"}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n: _lv(states[n], "pane-tail"))
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("4 registered seats", row["evidence"])

    def test_a_GONE_pane_is_RED_named_with_its_evidence_and_the_resume_verb(self):
        states = {"codex": ("GONE", "pid-dead"), "kimi": ("RUNNING", "pane-tail")}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n: _lv(*states[n]))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("codex GONE (pid-dead)", row["evidence"])
        self.assertIn("helm seat resume", row["repair"])

    def test_an_exited_agent_under_a_live_pane_is_also_down(self):
        row = ready.signal_seats(
            registered=lambda: (["codex"], False),
            liveness=lambda n: _lv("EXITED_PANE_ALIVE", "pane-tail"))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("codex EXITED_PANE_ALIVE (pane-tail)", row["evidence"])
        self.assertIn("helm seat resume", row["repair"])

    def test_an_unprovable_pane_is_UNKNOWN_never_folded_into_pass_or_fail(self):
        row = ready.signal_seats(
            registered=lambda: (["codex", "kimi"], False),
            liveness=lambda n: _lv("RUNNING", "pane-tail") if n == "kimi"
            else _lv("UNKNOWN", "stale-handle"))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("codex: stale-handle", row["evidence"])

    def test_down_outranks_unknown_and_the_unknowns_ride_the_note(self):
        states = {"a": ("GONE", "pid-dead"), "b": ("UNKNOWN", "headless")}
        row = ready.signal_seats(
            registered=lambda: (list(states), False),
            liveness=lambda n: _lv(*states[n]))
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("b: headless", row["note"])

    def test_a_crashing_liveness_probe_lands_in_the_unknown_bucket(self):
        def boom(name):
            raise RuntimeError("adapter died")
        row = ready.signal_seats(registered=lambda: (["x"], False),
                                 liveness=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("adapter died", row["evidence"])


def _census(**kw):
    rep = {"seats": [], "live_probe": True, "agent_probe": True,
           "covered": [], "deaf": [], "vacant": [], "unproven": [],
           "unreachable": [], "ghosts": [], "beacons": 0, "surplus": 0}
    rep.update(kw)
    return rep


class BeaconsSignalTest(unittest.TestCase):
    def test_clean_census_is_GREEN(self):
        rep = _census(seats=[{"seat": "a"}, {"seat": "b"}],
                      covered=[{"seat": "a"}, {"seat": "b"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("2 seats on the roll", row["evidence"])

    def test_beacon_live_UNPROVEN_stays_green_and_rides_the_note(self):
        rep = _census(seats=[{"seat": "a"}], unproven=[{"seat": "a"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("UNPROVEN", row["note"])

    def test_an_unreachable_seat_is_RED_and_named(self):
        rep = _census(seats=[{"seat": "alpha"}],
                      unreachable=[{"seat": "alpha"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("alpha", row["evidence"])
        self.assertIn("helm beacons", row["repair"])

    def test_vacant_and_ghosts_are_faults_too(self):
        rep = _census(seats=[{"seat": "a"}], vacant=[{"seat": "a"}],
                      ghosts=[{"pid": 1, "seat": "a"}])
        row = ready.signal_beacons(census=lambda: rep)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("vacant", row["evidence"])
        self.assertIn("1 ghost waiter", row["evidence"])

    def test_a_failed_session_probe_is_UNKNOWN_not_a_death_claim(self):
        row = ready.signal_beacons(census=lambda: _census(live_probe=False))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("session liveness", row["evidence"])

    def test_a_failed_process_table_probe_is_UNKNOWN(self):
        row = ready.signal_beacons(census=lambda: _census(agent_probe=False))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("process table", row["evidence"])

    def test_a_crashing_census_is_UNKNOWN(self):
        def boom():
            raise OSError("proc gone")
        row = ready.signal_beacons(census=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("proc gone", row["evidence"])


class FamiliesSignalTest(unittest.TestCase):
    def test_recorder_refusal_is_UNKNOWN_carrying_the_recorders_reason(self):
        err = "proxywatch state is 61m old, bar 40m — the watcher is not running"
        row = ready.signal_families(snapshot=lambda: (None, err))
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertEqual(row["evidence"], err)
        self.assertIn("helm proxywatch", row["repair"])

    def test_all_healthy_is_GREEN(self):
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("codex", row["evidence"])

    def test_a_named_dark_cause_is_a_WALL_ready_with_note_never_red(self):
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False},
                "kimi": {"state": "AUTH-UNAVAILABLE",
                         "since": "2026-08-04T18:40:31Z", "dark": True}}
        row = ready.signal_families(
            snapshot=lambda: (snap, None),
            minted=lambda: [("codex", "codex"), ("kimi", "kimi")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("kimi AUTH-UNAVAILABLE since 2026-08-04T18:40:31Z",
                      row["note"])
        self.assertIn("a wall is a fact", row["note"])

    def test_every_family_walled_is_still_ready_with_note(self):
        snap = {"kimi": {"state": "QUOTA-402", "since": "x", "dark": True}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("kimi", "kimi")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("walled", row["evidence"])

    def test_an_UNKNOWN_family_state_blocks_READY(self):
        snap = {"grok": {"state": "UNKNOWN", "since": None, "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None),
                                    minted=lambda: [("grok", "grok")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("grok", row["evidence"])

    def test_a_minted_family_missing_from_the_record_is_UNKNOWN(self):
        """Coverage is checked against the minted census — a hardcoded list
        is how grok starved for two days (proxywatch's own docstring)."""
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(
            snapshot=lambda: (snap, None),
            minted=lambda: [("codex", "codex"), ("grok", "grok")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("grok: minted but no recorded verdict", row["evidence"])

    def test_a_failed_minted_census_is_UNKNOWN_not_a_shorter_answer(self):
        def boom():
            raise OSError("seats root unreadable")
        snap = {"codex": {"state": "HEALTHY", "since": "x", "dark": False}}
        row = ready.signal_families(snapshot=lambda: (snap, None), minted=boom)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("coverage unverifiable", row["evidence"])


class NoFreshSweepTest(unittest.TestCase):
    """THE PIN: signal 4 reads the RECORDED sweep; it never runs one. Every
    entry into proxywatch's canary machinery is booby-trapped, so the
    mutation 'make signal 4 sweep' crashes into an AssertionError, the
    signal stops reading GREEN, and this test goes red — while the honest
    reader path stays green having spent zero upstream tokens."""

    def setUp(self):
        self.traps = [mock.patch.object(
            proxywatch, name, side_effect=AssertionError(
                "signal 4 must READ the recorded sweep, never run one"))
            for name in ("_upstream_once", "upstream_canary",
                         "upstream_health", "health", "cmd_proxywatch")]
        for t in self.traps:
            t.start()
        self.addCleanup(lambda: [t.stop() for t in self.traps])

    def test_a_fresh_record_reads_GREEN_through_the_reader_path_alone(self):
        state = {"ts": time.time(), "upstream": {
            "codex": {"state": "HEALTHY", "since": "x", "dark": False}}}
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, None)):
            row = ready.signal_families(minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("codex", row["evidence"])

    def test_a_stale_record_reads_UNKNOWN_with_the_recorders_reason(self):
        state = {"ts": time.time() - proxywatch.UPSTREAM_CACHE_FRESH_S - 61,
                 "upstream": {
                     "codex": {"state": "HEALTHY", "since": "x", "dark": False}}}
        with mock.patch.object(proxywatch, "_read_watch_state",
                               return_value=(state, None)):
            row = ready.signal_families(minted=lambda: [("codex", "codex")])
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("watcher is not running", row["evidence"])


class CheckoutSignalTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            raise unittest.SkipTest("git not available")
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ready-git-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("1\n")
        self._git("add", "f")
        self._git("commit", "-qm", "one")
        self._git("update-ref", "refs/remotes/origin/main", "HEAD")

    def _git(self, *argv):
        subprocess.run(["git", "-C", self.repo] + list(argv), check=True,
                       capture_output=True, text=True)

    def test_clean_at_origin_main_is_GREEN_and_says_as_last_fetched(self):
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.GREEN)
        self.assertIn("as last fetched", row["evidence"])

    def test_a_dirty_shared_checkout_is_RED_with_the_lane_room_repair(self):
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("2\n")
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("DIRTY", row["evidence"])
        self.assertIn("helm work claim", row["repair"])

    def test_a_head_off_origin_main_is_RED_naming_both_shas(self):
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("2\n")
        self._git("commit", "-aqm", "two")
        row = ready.signal_checkout(root=self.repo)
        self.assertEqual(row["state"], ready.RED)
        self.assertIn("origin/main is", row["evidence"])

    def test_a_non_repo_root_is_UNKNOWN(self):
        row = ready.signal_checkout(root=self.tmp)
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("git could not answer", row["evidence"])
        self.assertIn(self.tmp, row["evidence"])

    def test_no_containing_checkout_is_UNKNOWN(self):
        from helm.work import _lanes
        with mock.patch.object(_lanes, "find_root", return_value=None):
            row = ready.signal_checkout()
        self.assertEqual(row["state"], ready.UNKNOWN)
        self.assertIn("unmeasurable", row["evidence"])


def _stub(signal, state, note=None):
    return lambda: ready._row(signal, state, signal + " evidence", note=note)


class CompositionTest(unittest.TestCase):
    def _gauge(self, **over):
        stubs = {"signal_daemon": _stub("daemon", ready.GREEN),
                 "signal_seats": _stub("seats", ready.GREEN),
                 "signal_beacons": _stub("beacons", ready.GREEN),
                 "signal_families": _stub("families", ready.GREEN),
                 "signal_checkout": _stub("checkout", ready.GREEN)}
        stubs.update(over)
        with contextlib.ExitStack() as st:
            for name, fn in stubs.items():
                st.enter_context(mock.patch.object(ready, name, fn))
            return ready.gauge()

    def test_all_green_is_READY(self):
        rep = self._gauge()
        self.assertEqual(rep["ready"], ready.READY)
        self.assertEqual(len(rep["signals"]), 5)

    def test_one_red_is_NOT_READY_even_beside_an_unknown(self):
        rep = self._gauge(signal_seats=_stub("seats", ready.RED),
                          signal_families=_stub("families", ready.UNKNOWN))
        self.assertEqual(len(rep["signals"]), 5)
        self.assertEqual(rep["ready"], "NOT READY")
        self.assertEqual(rep["signals"][1]["state"], ready.RED)
        self.assertEqual(rep["signals"][3]["state"], ready.UNKNOWN)

    def test_no_red_one_unknown_is_UNKNOWN_never_READY(self):
        rep = self._gauge(signal_families=_stub("families", ready.UNKNOWN))
        self.assertEqual(rep["ready"], ready.UNKNOWN)
        self.assertEqual([r["state"] for r in rep["signals"]],
                         [ready.GREEN, ready.GREEN, ready.GREEN,
                          ready.UNKNOWN, ready.GREEN])

    def test_a_walled_family_keeps_READY_and_the_note_survives(self):
        rep = self._gauge(signal_families=_stub(
            "families", ready.GREEN, note="WALLED: kimi AUTH-UNAVAILABLE"))
        self.assertEqual(rep["ready"], ready.READY)
        self.assertIn("WALLED", rep["signals"][3]["note"])

    def test_a_crashing_signal_becomes_an_UNKNOWN_row_never_a_missing_one(self):
        def boom():
            raise RuntimeError("leg died")
        rep = self._gauge(signal_beacons=boom)
        self.assertEqual(len(rep["signals"]), 5)
        self.assertEqual(rep["signals"][2]["state"], ready.UNKNOWN)
        self.assertIn("leg died", rep["signals"][2]["evidence"])
        self.assertEqual(rep["ready"], ready.UNKNOWN)


def _rep(verdict, rows):
    return {"ready": verdict, "signals": rows, "ts": 0}


class CmdReadyTest(unittest.TestCase):
    """The exit-code contract IS the covering surface: 0 READY, 1 NOT READY,
    2 cannot-prove — distinct on purpose (beacons' precedent)."""

    def test_READY_exits_0_and_renders_the_advisory_line(self):
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 0)
        self.assertIn("ADVISORY", out)
        self.assertIn("helm ready: READY — 1 green, 0 red, 0 unknown", out)

    def test_one_red_exits_1_and_prints_the_repair_verb(self):
        rep = _rep(ready.NOT_READY,
                   [ready._row("seats", ready.RED, "codex GONE",
                               repair="helm seat resume codex"),
                    ready._row("daemon", ready.GREEN, "answering",
                               repair="never shown while green")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 1)
        self.assertIn("-> helm seat resume codex", out)
        self.assertNotIn("never shown while green", out)

    def test_unknown_exits_2(self):
        rep = _rep(ready.UNKNOWN, [ready._row("families", ready.UNKNOWN, "stale")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, _o = _out(ready.cmd_ready, [])
        self.assertEqual(rc, 2)

    def test_json_round_trips(self):
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            rc, out = _out(ready.cmd_ready, ["--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["ready"], "READY")

    def test_junk_args_refuse_before_any_gauge_runs(self):
        with mock.patch.object(ready, "gauge",
                               side_effect=AssertionError("gauged anyway")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = ready.cmd_ready(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg", buf.getvalue())


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self._forget()
        self.addCleanup(self._forget)

    def _forget(self):
        web._qstate.pop("ready", None)
        web._qinflight.pop("ready", None)

    def test_the_route_is_registered_and_serves_the_gauge(self):
        self.assertIn("/api/ready", web.API)
        rep = _rep(ready.READY, [ready._row("daemon", ready.GREEN, "ok")])
        with mock.patch.object(ready, "gauge", return_value=rep):
            body = web.API["/api/ready"]()
        self.assertEqual(body["ready"], "READY")

    def test_the_cache_answers_the_second_poll_without_regauging(self):
        rep = _rep(ready.READY, [])
        with mock.patch.object(ready, "gauge", return_value=rep) as g:
            web.API["/api/ready"]()
            web.API["/api/ready"]()
        self.assertEqual(g.call_count, 1)

    def test_a_crashing_gauge_answers_unavailable_at_200_shape(self):
        with mock.patch.object(ready, "gauge",
                               side_effect=OSError("estate on fire")):
            body = web.API["/api/ready"]()
        self.assertIn("estate on fire", body["unavailable"])


class UiWiringTest(unittest.TestCase):
    """The page half: the section exists, the renderer is a function
    DECLARATION (the node harness lifts declarations only), the wiring line
    binds it to the section, and the card boots pending beside the other
    boots (the TDZ law: boot after every declaration). Each test reads the
    page itself, so the observable's provenance is straight-line."""

    def _ui(self):
        return web_ui_loader.read_text()

    def test_the_section_renderer_wiring_and_boot_all_exist(self):
        ui = self._ui()
        # positive control first: the page actually loaded and is the page
        self.assertGreater(len(ui), 10000)
        self.assertIn("<!doctype html>", ui)
        self.assertIn('<section id="readysec"></section>', ui)
        self.assertIn("function readyCardHTML(", ui)
        self.assertIn('$("#readysec").innerHTML = readyCardHTML(d)', ui)
        self.assertIn('readyShow({pending: true});', ui)
        self.assertIn('j("/api/ready"', ui)

    def test_the_boot_sits_after_the_declarations(self):
        ui = self._ui()
        # both anchors must EXIST (the positive control) before order means
        # anything — index() would raise, but a raise is not an assertion
        self.assertIn("function readyCardHTML(", ui)
        self.assertIn("readyShow({pending: true});", ui)
        self.assertLess(ui.index("function readyCardHTML("),
                        ui.index("readyShow({pending: true});"))


class ClientRuntimeTest(unittest.TestCase):
    """readyCardHTML under node — the exact source the page ships."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        from tests.test_web_chat_client_runtime import _extract_fn
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-ready-runtime-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(line[0] + "\n\n" + _extract_fn(src, "readyCardHTML") + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const name of Object.keys(cases)) out[name] = readyCardHTML(cases[name]);
process.stdout.write(JSON.stringify(out));
""")
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def render(self, **cases):
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_the_three_boards_render_their_states(self):
        board = {"ready": "NOT READY", "ts": 0, "signals": [
            {"signal": "seats", "state": "RED",
             "evidence": "codex GONE (pid-dead)",
             "repair": "helm seat resume codex", "note": None},
            {"signal": "families", "state": "GREEN",
             "evidence": "codex HEALTHY",
             "repair": "hidden while green",
             "note": "WALLED: kimi AUTH-UNAVAILABLE"}]}
        out = self.render(bad=board, pending={"pending": True},
                          dark={"unavailable": "/api/ready did not answer"})
        self.assertIn("NOT READY", out["bad"])
        self.assertIn("helm seat resume codex", out["bad"])
        self.assertNotIn("hidden while green", out["bad"])
        self.assertIn("WALLED: kimi AUTH-UNAVAILABLE", out["bad"])
        self.assertIn("not read yet", out["pending"])
        self.assertIn("UNKNOWN", out["dark"])

    def test_evidence_is_escaped_not_injected(self):
        board = {"ready": "UNKNOWN", "ts": 0, "signals": [
            {"signal": "seats", "state": "UNKNOWN",
             "evidence": "<script>alert(1)</script>", "repair": None,
             "note": None}]}
        html = self.render(x=board)["x"]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
