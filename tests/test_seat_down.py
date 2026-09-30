#!/usr/bin/env python3
"""`helm seat down` survives its own supervisor — the desired-down record.

THE FAILURE PINNED HERE. An operator stood a retired seat down; `seat down`
stopped its proxy, and the */3 `seat doctor --ensure` pass respawned it on
its next run, after which proxywatch canaried it and darkened its family
again. Nothing recorded that the stop was deliberate, so every supervisor
read a minted, stopped proxy as the starvation it exists to heal.

EVERY ARM FAILS ON THE CODE BEFORE THE RECORD EXISTED, and none of them needs
the new module to plant its fixture: a record is planted as raw JSON at the
path and in the shape helm/seat_down.py documents, so an arm run against the
old tree fails on the BEHAVIOUR it asserts (a refused `--reason`, a respawn,
a probe, a FAMILY-DARK) rather than on an import.

HERMETIC: HELM_HOME is a tmp tree, every proxy primitive (`_up`, `_down`,
pidfile, port) is a double wherever it would touch a process, the suspend
rung and the orca cred-follow rung are stubbed out of the ensure pass, and
no real proxy, pane or room is touched.
"""
import ast
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import _tmphome  # noqa: F401 — plants the tmp config roots first

from helm import pk, proxywatch, seat, seat_health, seat_proxy, suspend

MARKER = "desired-down.json"
WHO = "down-fixture"
WHY = "retired on the operator's call"


def plant(proxy_home, family, seat_name, raw=None, by=WHO, reason=WHY):
    """A desired-down record in the documented shape, written without the
    module under test. `raw` plants arbitrary bytes instead."""
    os.makedirs(proxy_home, exist_ok=True)
    path = os.path.join(proxy_home, MARKER)
    with open(path, "w") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump({"v": 1, "seat": seat_name, "family": family, "by": by,
                       "at": pk.now_ts(), "reason": reason}, f)
    return path


class DownRig(unittest.TestCase):
    """A tmp HELM_HOME holding one minted codex proxy home (config.yaml)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-down-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm-home"),
            "HELM_CHAT_NAME": WHO,
            "HELM_ENSURE_QUIET_HEARTBEAT_DIR": os.path.join(self.tmp, "hb"),
            "HELM_PROXY_CPU_DIR": os.path.join(self.tmp, "cpu")})
        env.start()
        self.addCleanup(env.stop)
        # the ensure pass also runs the suspend rung and the orca cred-follow
        # rung; neither is this file's subject and both read host state
        for patch in (mock.patch.object(suspend, "resume_rung",
                                        return_value=[]),
                      mock.patch.object(seat_health, "_cred_follow_pass",
                                        return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def mint(self, family="codex", seat_name="codex"):
        d = seat._proxy_home(family, seat_name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.yaml"), "w") as f:
            f.write("port: 8317\n")
        return d

    def marker(self, family="codex", seat_name="codex"):
        return os.path.join(seat._proxy_home(family, seat_name), MARKER)

    def record(self, family="codex", seat_name="codex"):
        with open(self.marker(family, seat_name)) as f:
            return json.load(f)

    @staticmethod
    def cli(*argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(list(argv))
        return rc, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def dead_proxy(self, up=None):
        """No pidfile, no pid, port closed: the row the watchdog RESPAWNS.
        `up` (default: a double that brings the proxy up) is the respawn."""
        state = {"live": False}

        def fake_up(family, quiet=False, seat=None):
            state["live"] = True
            return 0
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 4321 if state["live"]
                                  else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: state["live"]), \
                mock.patch.object(seat, "_up",
                                  side_effect=up or fake_up) as double, \
                mock.patch.object(seat, "_down", return_value=0) as down, \
                mock.patch.object(seat, "_cpu_canary",
                                  return_value=("ok", 1.0, 1.0, "")):
            yield double, down

    def health(self, include_upstream=True):
        """The REAL health() over one codex seat whose proxy home is the tmp
        dir, every host reader a double."""
        d = seat._proxy_home("codex", "codex")
        with mock.patch.object(proxywatch, "_watched_seats",
                               return_value=["codex"]), \
                mock.patch.object(proxywatch, "probe",
                                  return_value=("healthy", "HTTP 401", 1)) \
                as self.probe, \
                mock.patch.object(proxywatch, "_seat_canary_observation",
                                  return_value=(("HEALTHY", "ok", 1), "b",
                                                "VERIFIED")) as self.canary, \
                mock.patch.object(proxywatch, "proxy_runtime_proofs",
                                  return_value={}), \
                mock.patch.object(proxywatch, "_live_seats",
                                  return_value=(set(), None, {})), \
                mock.patch.object(proxywatch, "host_suspend_gap_s",
                                  return_value=0), \
                mock.patch("helm.seat._seat_family",
                           return_value=("codex", None)), \
                mock.patch("helm.seat._instance_dir", return_value=d), \
                mock.patch("helm.autocompact._newest_transcript",
                           return_value=None), \
                mock.patch("helm.silent_drop._state_path",
                           return_value=os.path.join(self.tmp, "sd.json")), \
                mock.patch("helm.seats.roster", return_value={}), \
                mock.patch("helm.pi.seat_port", return_value=(8317, None)):
            return proxywatch.health(include_upstream=include_upstream,
                                     prior_state={})

    def ensure(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat._ensure(list(args))
        return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# (a) down, then an ensure pass: no respawn, a DOWN row
# ---------------------------------------------------------------------------

class DownThenEnsureTest(DownRig):

    def test_down_records_who_when_why_BEFORE_it_stops_the_proxy(self):
        """MUTANT: stop first, record after (or not at all) — the double sees
        no record when it is called."""
        self.mint()
        seen = []

        def fake_down(family, seat=None):
            seen.append((family, seat, os.path.exists(self.marker())))
            return 0
        with mock.patch.object(seat, "_down", side_effect=fake_down):
            rc, out, err = self.cli("down", "codex", "--reason", WHY)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen, [("codex", "codex", True)],
                         "the record must exist before the proxy is stopped")
        rec = self.record()
        self.assertEqual((rec["v"], rec["seat"], rec["family"], rec["by"],
                          rec["reason"]), (1, "codex", "codex", WHO, WHY))
        self.assertIsNotNone(pk.parse_ts_epoch(rec["at"]), rec)
        self.assertEqual(os.stat(self.marker()).st_mode & 0o777, 0o600)
        self.assertIn("desired-down by %s" % WHO, out)

    def test_an_ensure_pass_never_respawns_a_desired_down_seat(self):  # noqa: VACUOUS_ASSERTION — the (codex, down) verdict and the record's who/why/hint in its detail are the positive controls; the uncalled _up/_down are the contract
        """THE INCIDENT. MUTANT: `_ensure_row` ignores the record — the
        dead proxy is respawned, exactly as the cron did."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with self.dead_proxy() as (up, down):
            label, state, detail = seat._ensure_row("codex", "codex")
        up.assert_not_called()
        down.assert_not_called()
        self.assertEqual((label, state), ("codex", "down"))
        self.assertIn("desired-down by %s" % WHO, detail)
        self.assertIn(WHY, detail)
        self.assertIn("helm seat up codex", detail)

    def test_the_pass_reads_DOWN_rc0_never_healthy_never_unknown(self):  # noqa: VACUOUS_ASSERTION — rc 0, the DOWN row regex and the JSON down count and row state are the positive controls; the uncalled _up and absent UNKNOWN are the contract
        """The row is its own state on every contract, and it never turns rc
        red. MUTANT: fold DOWN into UNKNOWN (rc 2) or into healthy."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with self.dead_proxy() as (up, _down):
            rc, out, err = self.ensure()
            rc_json, raw, _ = self.ensure("--ensure", "--json")
        up.assert_not_called()
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"(?m)^codex\s+DOWN\s+desired-down by %s" % WHO)
        self.assertNotIn("UNKNOWN", out + err)
        payload = json.loads(raw)
        self.assertEqual(rc_json, 0)
        self.assertEqual(payload["down"], 1)
        self.assertEqual(payload["unknown"], 0)
        [row] = payload["rows"]
        self.assertEqual((row["state"], row["desired_down"]["by"]),
                         ("down", WHO))

    def test_quiet_leaves_a_steady_DOWN_row_to_a_heartbeat_that_names_it(self):
        """--quiet is the cron surface: the same operator decision must not
        print every three minutes, and the heartbeat must not count it
        HEALTHY. MUTANT: heartbeat(total) — 'HEARTBEAT — 1 proxy row(s)
        HEALTHY' for a fleet whose only seat is stood down."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with self.dead_proxy():
            rc, out, err = self.ensure("--ensure", "--quiet")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("HEARTBEAT — 0 proxy row(s) HEALTHY; 1 DOWN by "
                      "operator (codex)", out)
        self.assertNotRegex(out, r"(?m)^codex\s+DOWN")

    def test_a_proxy_still_running_against_the_record_is_said_even_quiet(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the CONTRADICTION row regex are the unconditional positives on the same pass; the uncalled _up/_down are the contract
        """A contradiction is not steady. MUTANT: drop the pid check."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with mock.patch.object(seat, "_running_pid", return_value=4321), \
                mock.patch.object(seat, "_up") as up, \
                mock.patch.object(seat, "_down") as down:
            rc, out, err = self.ensure("--ensure", "--quiet")
        up.assert_not_called()
        down.assert_not_called()
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"(?m)^codex\s+DOWN\s+.*CONTRADICTION: proxy "
                              r"pid 4321 is still running")

    def test_seat_status_says_DOWN_and_names_the_record(self):
        """`seat status`/`doctor` must not render an operator's stop as a bare
        'proxy down' (which routes a maintainer to respawn it)."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with mock.patch.object(seat, "_running_pid_rec", return_value=None):
            live, detail = seat_health._proxy_live_text("codex", "codex")
        self.assertEqual(live, "proxy DOWN (desired)")
        self.assertIn("desired-down by %s" % WHO, detail)
        self.assertIn("helm seat up codex", detail)

    def test_a_down_that_cannot_write_its_record_refuses_to_stop(self):  # noqa: VACUOUS_ASSERTION — rc 1 and both refusal clauses on stderr are the positive controls; the uncalled _down and absent file are the contract
        """A stop with no record is the incident itself, so it refuses.
        MUTANT: stop anyway."""
        self.mint()
        with mock.patch.object(pk, "atomic_write",
                               side_effect=OSError("no space left")), \
                mock.patch.object(seat, "_down", return_value=0) as down:
            rc, out, err = self.cli("down", "codex")
        self.assertEqual(rc, 1, out + err)
        down.assert_not_called()
        self.assertIn("refusing to stop codex", err)
        self.assertIn("no space left", err)
        self.assertFalse(os.path.exists(self.marker()))

    def test_an_unminted_instance_is_refused_and_grows_no_directory(self):  # noqa: VACUOUS_ASSERTION — rc 1 and the named refusal are the positive controls; the uncalled _down and absent directory are the contract
        """A typo'd instance must not mint a proxy home just to hold a record
        nothing enumerates. MUTANT: write the record unconditionally."""
        self.mint()
        with mock.patch.object(seat, "_down", return_value=0) as down:
            rc, out, err = self.cli("down", "codex-43")
        self.assertEqual(rc, 1, out + err)
        down.assert_not_called()
        self.assertIn("has no minted proxy", err)
        self.assertFalse(os.path.exists(seat._proxy_home("codex", "codex-43")))

    def test_junk_after_down_still_refuses_before_any_record(self):  # noqa: VACUOUS_ASSERTION — rc 2 from the tail guard is the positive control; the uncalled _down and absent file are the contract
        self.mint()
        with mock.patch.object(seat, "_down") as down:
            rc, _out, err = self.cli("down", "codex", "--reason")
        self.assertEqual(rc, 2, err)
        down.assert_not_called()
        self.assertFalse(os.path.exists(self.marker()))


# ---------------------------------------------------------------------------
# (b) the operator's run verbs clear it, and ensure supervises again
# ---------------------------------------------------------------------------

class RunVerbsClearTest(DownRig):

    def test_up_clears_the_record_and_the_next_pass_respawns(self):  # noqa: VACUOUS_ASSERTION — the exact _up call, the cleared-record sentence and the RESPAWNED row with its exact respawn call are the positive controls; the absent file is the contract
        """MUTANT: `seat up` starts the proxy and leaves the record — the
        seat is up now and dead again after its first crash."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with mock.patch.object(seat, "_up", return_value=0) as up:
            rc, out, err = self.cli("up", "codex")
        self.assertEqual(rc, 0, out + err)
        up.assert_called_once_with("codex", seat="codex")
        self.assertFalse(os.path.exists(self.marker()))
        self.assertIn("desired-down record cleared (was desired-down by %s"
                      % WHO, out)
        with self.dead_proxy() as (respawn, _down):
            label, state, detail = seat._ensure_row("codex", "codex")
        self.assertEqual(state, "respawned", detail)
        respawn.assert_called_once_with("codex", quiet=True, seat="codex")

    def test_resume_clears_the_record_before_it_relaunches(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the exact (name, record-absent) observation the relaunch double recorded at call time are the positive controls
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        seen = []

        def fake_resume(name, rest):
            seen.append((name, os.path.exists(self.marker())))
            return 0
        with mock.patch.object(seat, "_resume", side_effect=fake_resume):
            rc, out, err = self.cli("resume", "codex")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen, [("codex", False)])

    def test_spawn_clears_it_and_a_dry_run_does_not(self):
        """--print is a plan, not an operator saying "run it"."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with mock.patch.object(seat, "_spawn", return_value=0) as spawn:
            rc, out, err = self.cli("spawn", "codex", "--print")
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(os.path.exists(self.marker()),
                            "a dry run cleared the record")
            rc, out, err = self.cli("spawn", "codex")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(spawn.call_count, 2)
        self.assertFalse(os.path.exists(self.marker()))

    def test_a_resume_refused_at_its_tail_clears_nothing(self):  # noqa: VACUOUS_ASSERTION — rc 2 from the tail guard and the record still on disk are the positive controls; the uncalled _resume is the contract
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        with mock.patch.object(seat, "_resume") as resume:
            rc, _out, err = self.cli("resume", "codex", "--bogus")
        self.assertEqual(rc, 2, err)
        resume.assert_not_called()
        self.assertTrue(os.path.exists(self.marker()))


# ---------------------------------------------------------------------------
# (c) internal stops and starts neither write nor clear it
# ---------------------------------------------------------------------------

class InternalPathsLeaveTheRecordAloneTest(DownRig):

    def test_the_post_suspend_bounce_writes_no_record(self):  # noqa: VACUOUS_ASSERTION — the operator's down proves the writer writes (file present), up proves it clears, and the bounce's True result plus its exact _up call prove the bounce ran; the absent file after it is the contract
        """The bounce is `_down` + `_up` on a LIVE proxy, never the operator's
        verb. The positive half first: the operator's `down` DOES write, so
        the absence below is a measurement of the bounce and not of a writer
        that never writes. MUTANT: record desired-down inside `_down` (where
        the stop happens) — every post-suspend bounce then stands the fleet
        down."""
        self.mint()
        with mock.patch.object(seat, "_down", return_value=0):
            self.assertEqual(self.cli("down", "codex")[0], 0)
        self.assertTrue(os.path.exists(self.marker()))
        with mock.patch.object(seat, "_up", return_value=0):
            self.assertEqual(self.cli("up", "codex")[0], 0)
        self.assertFalse(os.path.exists(self.marker()))
        # the REAL seat_proxy._down (no pidfile: "not running", rc 0); the
        # start is a double so no process is ever launched
        with mock.patch.object(seat_proxy, "_up", return_value=0) as up, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(suspend._bounce("codex", "codex"))
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        self.assertFalse(os.path.exists(self.marker()),
                         "an internal bounce wrote a desired-down record")

    def test_the_bounce_does_not_select_a_desired_down_seat(self):
        """Even with a proxy somehow running against the record, the sweep
        does not restart what an operator stood down."""
        self.mint()
        self.mint("codex", "seat-b")
        plant(seat._proxy_home("codex", "seat-b"), "codex", "seat-b")
        with mock.patch.object(seat, "_running_pid", return_value=4321):
            self.assertEqual(suspend._live_proxies(), [("codex", "codex")])

    def test_ensures_own_respawn_of_an_UP_seat_writes_no_record(self):  # noqa: VACUOUS_ASSERTION — the respawned state and the exact _up call are the positive controls; the absent file is the contract
        self.mint()
        with self.dead_proxy() as (up, _down):
            label, state, _detail = seat._ensure_row("codex", "codex")
        self.assertEqual(state, "respawned")
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        self.assertFalse(os.path.exists(self.marker()))


class OnlyTheOperatorDoorsTouchTheRecordTest(unittest.TestCase):
    """THE CALLER CENSUS, over helm/'s source. The record's writer and
    clearer each have one caller, and those callers are called only from the
    `helm seat` dispatcher. Every internal stop/start path (the bounce, the
    ensure respawn, resume's own start, rehome, the reboot sweep) is proved
    here by ABSENCE from a census that must first find the real call sites.

    A call is attributed to helm/seat_down.py when it goes through a name
    bound to that module (`seat_down.mark`, or any `import ... as` alias of
    it), when a name was imported FROM it, or when it is a bare call inside
    the module itself. Another module's `hooklatency.mark(...)` is not this
    writer. MUTANT: add `seat_down.unmark(...)` to the suspend bounce."""

    HELM = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "helm")
    OWNER = "seat_down"

    def census(self, names, root=None):
        found, root = set(), root or self.HELM
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read(), fn)
            aliases, direct = set(), {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    owner = (node.module or "").rsplit(".", 1)[-1]
                    for alias in node.names:
                        if alias.name == self.OWNER:
                            aliases.add(alias.asname or alias.name)
                        elif owner == self.OWNER and alias.name in names:
                            direct[alias.asname or alias.name] = alias.name
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.rsplit(".", 1)[-1] == self.OWNER:
                            aliases.add(alias.asname or alias.name)
            if fn == self.OWNER + ".py":
                direct.update((name, name) for name in names)
            for outer in ast.walk(tree):
                if not isinstance(outer, (ast.FunctionDef,
                                          ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(outer):
                    if not isinstance(node, ast.Call):
                        continue
                    f_ = node.func
                    if isinstance(f_, ast.Attribute) and f_.attr in names \
                            and isinstance(f_.value, ast.Name) \
                            and f_.value.id in aliases:
                        found.add((f_.attr, fn, outer.name))
                    elif isinstance(f_, ast.Name) and f_.id in direct:
                        found.add((direct[f_.id], fn, outer.name))
        return found

    def test_the_writer_and_clearer_have_exactly_the_operator_callers(self):
        self.assertEqual(self.census({"mark", "unmark"}),
                         {("mark", "seat_down.py", "operator_down"),
                          ("unmark", "seat_down.py", "operator_run")})

    def test_the_operator_doors_are_called_only_by_the_seat_dispatcher(self):
        self.assertEqual(self.census({"operator_down", "operator_run"}),
                         {("operator_down", "seat.py", "cmd_seat"),
                          ("operator_run", "seat.py", "cmd_seat")})

    def test_the_census_sees_through_an_alias_and_a_from_import(self):
        """MUST-HIT on the instrument before its silence is trusted: a planted
        tree that calls the clearer through an alias and through a name
        imported from the module is found, and another module's `.mark` is
        not."""
        root = tempfile.mkdtemp(prefix="helm-test-census-")
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "probe.py"), "w") as f:
            f.write("from . import seat_down as sd\n"
                    "from .seat_down import unmark as clear\n"
                    "from . import hooklatency\n"
                    "def bounce():\n"
                    "    sd.unmark('f', 's')\n"
                    "    hooklatency.mark('x')\n"
                    "def rehome():\n"
                    "    clear('f', 's')\n")
        self.assertEqual(self.census({"mark", "unmark"}, root=root),
                         {("unmark", "probe.py", "bounce"),
                          ("unmark", "probe.py", "rehome")})


# ---------------------------------------------------------------------------
# (d) proxywatch: no probe, no canary, no FAMILY-DARK
# ---------------------------------------------------------------------------

class ProxywatchDownTest(DownRig):

    def test_a_desired_down_seat_is_neither_probed_nor_canaried(self):  # noqa: VACUOUS_ASSERTION — the DOWN row's record, error text, the DOWN family record and the rendered report line are the positive controls; the uncalled probe/canary and empty findings are the contract
        """MUTANT: health() ignores the record — the probe and the family
        canary both reach the seat the operator stood down."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex")
        rep = self.health()
        self.probe.assert_not_called()
        self.canary.assert_not_called()
        [row] = rep["seats"]
        self.assertEqual(row["down"]["by"], WHO)
        self.assertTrue(row["error"].startswith("DOWN — desired-down by"),
                        row["error"])
        family = rep["upstream"]["codex"]
        self.assertEqual((family["state"], family["dark"]), ("DOWN", False))
        self.assertEqual(family["members"], {"codex": "DOWN"})
        self.assertEqual(proxywatch.findings(rep), [])
        self.assertIn("\n  codex     DOWN — desired-down by %s" % WHO,
                      "\n" + "\n".join(proxywatch.report_lines(rep)))

    def test_a_latched_dark_family_whose_seat_is_stood_down_reads_DOWN(self):  # noqa: ORPHANED_MOCK — the canary double is the absence the arm asserts: upstream_health reaches it through its thread pool for every locally healthy row, which the family arm below proves by asserting it IS called
        """THE SECOND HALF OF THE INCIDENT. A family dark on the last pass
        whose only seat is now desired-down reads DOWN, never a latched
        UNKNOWN 'absent from the census' that raises FAMILY-DARK every pass.
        MUTANT: keep carrying the prior dark seat forward."""
        prior = {"upstream": {"codex": {
            "state": "AUTH-401", "since": "episode-start", "dark": True,
            "seats": {"codex": {"state": "AUTH-401", "dark": True,
                                "since": "episode-start"}}}}}
        down_row = {"seat": "codex", "family": "codex",
                    "error": "DOWN — fixture",
                    "down": {"v": 1, "seat": "codex", "family": "codex",
                             "by": WHO, "at": pk.now_ts(), "reason": WHY}}
        with mock.patch.object(proxywatch, "_seat_canary_observation") as c:
            upstream = proxywatch.upstream_health([down_row], now=1000,
                                                  prior=prior)
        c.assert_not_called()
        family = upstream["codex"]
        self.assertEqual((family["state"], family["dark"]), ("DOWN", False))
        self.assertIn("desired-down by %s" % WHO, family["detail"])
        rep = {"ts": 1000, "seats": [], "upstream": upstream,
               "proxy_runtime": {}}
        self.assertEqual([k for k, _t in proxywatch.findings(rep)], [])
        edges = proxywatch.upstream_transitions(rep, prior=prior)
        self.assertEqual([e["kind"] for e in edges], ["family-down"])
        self.assertIn("FAMILY-DOWN codex: DOWN",
                      proxywatch._transition_lines(edges)[0])
        # the persisted record is one every reader accepts, and it is no wall
        state = {"upstream": upstream}
        rec, err = proxywatch.upstream_record(state, "codex")
        self.assertIsNone(err)
        self.assertFalse(rec["dark"])
        self.assertFalse(proxywatch.beacon_paused(rec))
        seat_rec, seat_err = proxywatch.upstream_seat_record(
            state, "codex", "codex")
        self.assertIsNone(seat_err)
        self.assertEqual(seat_rec["state"], "DOWN")

    def test_a_family_with_other_live_seats_is_unaffected(self):  # noqa: ORPHANED_MOCK — reached through upstream_health's thread pool; the exact call list asserted below is the proof it fired
        """Only the stood-down seat leaves the family's verdict; its live
        sibling is canaried and decides it alone."""
        rows = [{"seat": "seat-a", "family": "codex", "probe": "healthy"},
                {"seat": "seat-b", "family": "codex",
                 "error": "DOWN — fixture",
                 "down": {"v": 1, "seat": "seat-b", "family": "codex",
                          "by": WHO, "at": pk.now_ts(), "reason": None}}]
        prior = {"upstream": {"codex": {
            "state": "AUTH-401", "since": "s", "dark": True,
            "seats": {"seat-b": {"state": "AUTH-401", "dark": True,
                                 "since": "s"}}}}}
        with mock.patch.object(proxywatch, "_seat_canary_observation",
                               return_value=(("HEALTHY", "ok", 3), "b",
                                             "VERIFIED")) as canary:
            family = proxywatch.upstream_health(rows, now=1000,
                                                prior=prior)["codex"]
        self.assertEqual([call.args for call in canary.call_args_list],
                         [("seat-a", "codex")])
        self.assertEqual((family["state"], family["dark"]),
                         ("HEALTHY", False))
        self.assertEqual(family["members"], {"seat-a": "HEALTHY"})


class RebindEvidenceTest(unittest.TestCase):
    """THE CONSUMER THE WATCH'S SILENCE WOULD BLIND. Rebind's evidence gate
    reads proxywatch's row for the recipient, and a stopped proxy answers it
    with a `down` probe. A desired-down seat is not probed, so its row must
    carry that answer itself. MUTANT: drop the `down` branch in
    `_proxy_evidence` — the gate reads no evidence at all."""

    def test_a_stood_down_recipient_is_measured_unable_to_serve(self):
        from helm import dispatches
        row = {"seat": "seat-a", "family": "codex", "error": "DOWN — fixture",
               "probe": None, "log": None, "hang_candidate": False,
               "down": {"v": 1, "seat": "seat-a", "family": "codex",
                        "by": WHO, "at": pk.now_ts(), "reason": WHY}}
        with mock.patch.object(proxywatch, "health",
                               return_value={"ts": 1, "seats": [row]}):
            starved, note = dispatches._proxy_evidence("seat-a")
        self.assertIsNone(note)
        self.assertEqual(starved.split(" at ")[0],
                         "proxy desired-down by %s" % WHO)
        self.assertIn(WHY, starved)


class UsabilityRefusesAStoodDownSeatTest(unittest.TestCase):
    """THE ROUTER'S QUESTION. proxywatch hands a desired-down seat back on its
    no-verdict row, which the usability join reads as PANE-ONLY scope — the
    native-seat shape, where a live pane alone means USABLE. A stood-down
    proxy seat is not a native seat: its proxy is off on purpose, so work
    routed to it lands on a pane that cannot turn. MUTANT: drop the DOWN rung
    — the same row reads USABLE."""

    @staticmethod
    def join(row):
        from helm import seat_usability
        return seat_usability.join(
            seats=["seat-a"],
            health=lambda **kw: {"ts": 0, "seats": [row]},
            upstream=lambda: ({"codex": {"state": "HEALTHY", "dark": False}},
                              None),
            open_recipients=lambda: ({}, None),
            register=lambda: {"seat-a": {"runtime_verified": True}},
            canonical=lambda name: (name, None),
            live_seats=lambda: ({"seat-a"}, None, {}),
            beacon_live=lambda name: ([], None))["seat-a"]

    def test_a_live_pane_does_not_make_a_stood_down_seat_usable(self):
        row = {"seat": "seat-a", "family": "codex", "pane_live": True,
               "census_blind": None, "error": "DOWN — fixture",
               "down": {"v": 1, "seat": "seat-a", "family": "codex",
                        "by": WHO, "at": pk.now_ts(), "reason": WHY}}
        got = self.join(row)
        self.assertEqual((got["verdict"], got["can_take_work"],
                          got["refusal"]), ("UNUSABLE", False, "down"))
        self.assertTrue(got["reason"].startswith(
            "proxy desired-down by %s" % WHO), got["reason"])
        self.assertIn("helm seat up seat-a", got["reason"])
        # CONTROL: the identical row without the record is the native shape
        # and reads USABLE, so the refusal above is the record's alone
        control = self.join(dict(row, down=None, error="no proxy watched"))
        self.assertEqual((control["verdict"], control["refusal"]),
                         ("USABLE", None))


# ---------------------------------------------------------------------------
# (e) an unreadable record: SUPERVISED, and loud about it
# ---------------------------------------------------------------------------

class UnreadableRecordTest(DownRig):
    """THE FAIL DIRECTION, chosen: a record helm cannot read is treated as
    ABSENT (the seat is supervised) and said on every surface. Supervising a
    seat an operator wanted down costs one proxy and a visible row fixed by
    one `seat down`; honouring an unreadable record could keep a seat the
    fleet depends on dead on the strength of bytes nobody can read."""

    def test_ensure_supervises_through_a_corrupt_record_and_says_so(self):
        """MUTANT (fail-closed): treat an unreadable record as DOWN — the dead
        proxy is never respawned and nothing says why."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex",
              raw="{ not json")
        with self.dead_proxy() as (up, _down):
            rc, out, err = self.ensure("--ensure", "--quiet")
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        self.assertEqual(rc, 1, out + err)           # a WARN, not a page
        self.assertRegex(out, r"(?m)^codex\s+RESPAWNED\s+.*desired-down "
                              r"marker UNREADABLE")
        self.assertIn("SUPERVISED as if absent", out)
        self.assertIn("record(s) UNREADABLE", err)

    def test_a_record_naming_another_seat_is_unreadable_not_honoured(self):
        """A copied record is not this seat's statement of intent."""
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "seat-b")
        with self.dead_proxy() as (up, _down):
            rc, raw, _err = self.ensure("--ensure", "--json")
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        [row] = json.loads(raw)["rows"]
        self.assertEqual(row["state"], "respawned")
        self.assertIsNone(row["desired_down"])
        self.assertIn("is not a desired-down record", row["marker_error"])
        self.assertEqual(rc, 1)

    def test_status_and_proxywatch_say_it_too(self):
        self.mint()
        plant(seat._proxy_home("codex", "codex"), "codex", "codex",
              raw="[]")
        with mock.patch.object(seat, "_running_pid_rec", return_value=None):
            live, detail = seat_health._proxy_live_text("codex", "codex")
        self.assertEqual(live.split(" ⚠")[0], "proxy down")
        self.assertIn("desired-down marker UNREADABLE", detail or "")
        rep = self.health()
        self.probe.assert_called_once_with(8317)
        self.assertIn("DOWN-MARKER-UNREADABLE",
                      [k for k, _t in proxywatch.findings(rep)])


# ---------------------------------------------------------------------------
# the lifecycle walk: down -> ensure -> up -> ensure -> crash -> ensure
# (the reboot-sweep leg lives in tests/test_seat_resume_all.py, whose fixture
# owns the orca double)
# ---------------------------------------------------------------------------

class LifecycleWalkTest(DownRig):

    def test_down_holds_up_releases_and_a_crash_is_still_healed(self):  # noqa: VACUOUS_ASSERTION — every leg asserts a positive: rc 0 with the DOWN row, the RESPAWNED row with its exact _up call, and the crash leg's respawned state with its exact _up call
        self.mint()
        with mock.patch.object(seat, "_down", return_value=0):
            self.assertEqual(self.cli("down", "codex", "--reason", WHY)[0], 0)
        # 1. an ensure pass over the stopped proxy: DOWN, no respawn
        with self.dead_proxy() as (up, _down):
            rc, out, err = self.ensure()
        self.assertEqual(rc, 0, out + err)
        up.assert_not_called()
        self.assertRegex(out, r"(?m)^codex\s+DOWN\s")
        # 2. the operator's `up` clears it
        with mock.patch.object(seat, "_up", return_value=0):
            self.assertEqual(self.cli("up", "codex")[0], 0)
        self.assertFalse(os.path.exists(self.marker()))
        # 3. supervised again: the next pass respawns a dead proxy
        with self.dead_proxy() as (up, _down):
            rc, out, err = self.ensure()
        self.assertEqual(rc, 0, out + err)
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        self.assertRegex(out, r"(?m)^codex\s+RESPAWNED\s")
        # 4. a CRASH of the up seat (a stale pidfile naming a dead pid) is
        # healed exactly as before the record existed
        state = {"live": False}

        def fake_up(family, quiet=False, seat=None):
            state["live"] = True
            return 0
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 9999,
                                             "identity": "proc:x"}), \
                mock.patch.object(seat, "_pid_alive", return_value=False), \
                mock.patch.object(seat, "_running_pid",
                                  lambda f, s=None: 5555 if state["live"]
                                  else None), \
                mock.patch.object(seat, "_port_open",
                                  lambda p, timeout=0.5: state["live"]), \
                mock.patch.object(seat, "_up", side_effect=fake_up) as up:
            label, row_state, detail = seat._ensure_row("codex", "codex")
        self.assertEqual(row_state, "respawned", detail)
        up.assert_called_once_with("codex", quiet=True, seat="codex")
        self.assertFalse(os.path.exists(self.marker()))


if __name__ == "__main__":
    unittest.main()
