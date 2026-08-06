#!/usr/bin/env python3
"""The fleet whole-suite cap: admission counts PROCESSES, never lock-holders.

Six concurrent whole-suite runs starved this box on 2026-08-03 and the PTY
daemon died with every pane on it. The ruled cap is TWO on a box that CARRIES
PANES (the fleet-wide half of that ruling was superseded 2026-08-05 — see
`PerHostCapTest`), and the
existing gatelock cannot carry it: a bare `python3 -m unittest discover`
never touches the lock. Every fixture here fabricates its own proc tree —
the guard under test is exactly the difference between what a process IS and
what it holds, so the tests hand it processes, not locks."""
import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import gate, pk, seats

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_PROC",
            "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
            "CODEX_SESSION_ID")

SUITE_ARGV = ("python3", "-m", "unittest", "discover", "-s", "tests",
              "-t", ".")


class CapBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-cap-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_ROOM"] = "cap-room"
        os.environ["HELM_CHAT_NODE_URL"] = ""
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        os.environ["HELM_PROC"] = self.proc
        # THE FIXTURE BOX CARRIES A PANE, because that is the box every test
        # below is about: the cap is per-HOST now, and on a paneless build
        # host it derives from core count — which would make these assertions
        # read a different number on every machine. Declaring the pane makes
        # each one say what it always meant: on a box with panes to lose, the
        # cap is two. `PaneHostCapTest` owns the other kind of box.
        self.pane(90001)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, val in self.prior.items():
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val

    def pane(self, pid, var="HELM_CHAT_NAME", seat="seat-a", comm="claude"):
        """One fabricated AGENT PANE, in the shape `beacons.is_agent` reads.

        THE SHAPE CHANGED WITH THE MECHANISM, and that is the point. This
        used to plant only an environ stamp, because gate hand-rolled its own
        /proc scan. gate now delegates to `beacons.agent_index`, which decides
        on comm/argv — evidence that stays readable when environ does not —
        so the fixture plants what a real claude pane actually looks like.
        The environ stamp is still written because a pane does carry one; it
        is simply no longer what makes it an agent."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"/usr/bin/" + comm.encode() + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"PATH=/usr/bin\0" + var.encode() + b"=" +
                    seat.encode() + b"\0")

    def plant(self, pid, argv, cgroup=None, cwd=None, start=7, state="S"):
        """One fabricated process: cmdline + stat (+ cgroup, + cwd)."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in argv))
        fields = [state, "1"] + ["0"] * 17 + [str(start)]
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%d (suite) %s\n" % (pid, " ".join(fields)))
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write("0::/app.slice/%s\n" % (cgroup or "app-fake.scope"))
        if cwd:
            os.symlink(cwd, os.path.join(d, "cwd"))

    def launcher(self, pid, start=7):
        """A live non-suite launcher process an intent row can bind to."""
        self.plant(pid, ("python3", "-m", "helm", "gate", "run"), start=start)

    def intent(self, position, pid, start=7, holder="seat-a"):
        pk.write_json(gate._admissions_path(), {"v": 1, "admissions": [
            {"position": position, "pid": pid, "starttime": start,
             "holder": holder, "ts": "2026-01-01T00:00:00Z"}]})

    def psi(self, avg10):
        d = os.path.join(self.proc, "pressure")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "memory"), "w") as f:
            f.write("some avg10=%.2f avg60=0.00 avg300=0.00 total=1\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
                    % avg10)


class SuiteShapeTest(CapBase):
    def test_the_gates_own_suite_argv_is_the_must_hit(self):
        # Seed the scan with a MUST-HIT: the exact argv gate.run spawns. If
        # this row ever reads False the census counts NOTHING and every
        # other assertion here is measuring an empty set.
        self.assertTrue(gate._suite_shaped(
            [os.path.realpath("/usr/bin/python3")] + list(gate.SUITE)))
        self.assertTrue(gate._suite_shaped(list(SUITE_ARGV)))

    def test_discovery_shapes_count(self):
        # Unconditional MUST-HIT before the table: the canonical bare form.
        self.assertTrue(gate._suite_shaped(["python3", "-m", "unittest"]))
        for argv in (
                ["python3", "-m", "unittest"],
                ["python", "-m", "unittest", "discover"],
                ["/usr/bin/python3.14", "-munittest", "discover"],
                ["graalpy", "-m", "unittest", "discover", "-s", "tests"],
                ["python3", "-u", "-m", "unittest", "-v"],
                ["python3", "-m", "unittest", "-k", "gate"]):
            self.assertTrue(gate._suite_shaped(argv), argv)

    def test_targeted_and_wrapper_shapes_do_not_count(self):
        # Positive control on the SAME observable: the predicate can say yes.
        self.assertTrue(gate._suite_shaped(list(SUITE_ARGV)))
        for argv in (
                ["python3", "-m", "unittest", "tests.test_one"],
                ["python3", "-m", "helm", "gate", "run"],
                ["python3", "/x/helm/gatechild.py", "--guard", "--parent",
                 "1", "--", "python3", "-m", "unittest", "discover"],
                ["python3", "/x/helm/gatechild.py", "--supervise", "--",
                 "python3", "-m", "unittest", "discover"],
                ["bash", "-c", "python3 -m unittest discover"],
                ["python3", "runner.py", "-m", "unittest"],
                []):
            self.assertFalse(gate._suite_shaped(argv), argv)


class CensusTest(CapBase):
    def test_census_counts_suites_and_reads_their_cgroup_position(self):
        self.plant(101, SUITE_ARGV, cgroup="helm-gate-aabbccdd11223344",
                   cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest"), cwd="/lane/two")
        self.plant(103, ("python3", "-m", "unittest", "tests.test_one"))
        self.plant(104, ("python3", "/x/gatechild.py", "--guard", "--",
                         "python3", "-m", "unittest", "discover"))
        rows = gate.suite_census(self.proc)
        self.assertEqual([r["pid"] for r in rows], [101, 102])
        self.assertEqual(rows[0]["position"], "aabbccdd11223344")
        self.assertEqual(rows[0]["cwd"], "/lane/one")
        self.assertIsNone(rows[1]["position"])

    def test_an_unreadable_proc_tree_is_none_never_empty(self):
        # Positive control: the same call reads a REAL tree as non-empty,
        # so the None below is the unreadable tree and not a broken census.
        self.plant(101, SUITE_ARGV)
        self.assertEqual([r["pid"] for r in gate.suite_census(self.proc)],
                         [101])
        self.assertIsNone(gate.suite_census(
            os.path.join(self.tmp, "no-such-proc")))


class PerHostCapTest(CapBase):
    """The cap is PER-HOST, and the predicate is panes — never a hostname.

    Owner ruling 2026-08-05. The fleet-wide number was justified by an outage
    that killed AGENT PANES, then applied unchanged to build hosts that have
    none; since 2026-08-03 gate suites route to the fab, so the number that
    bites now bites where its justification does not reach.

    MEASURED before the change (whole suite on the build host, /usr/bin/time -v):
    123056 KB peak RSS, 26% of one core, against 179 GB available. A cap of
    two there was a memory argument off by three orders of magnitude.

    A hostname allowlist would rot on the next host added or renamed, and it
    encodes the wrong fact: the casualty was irreplaceable agent context, not
    a machine's name. So these tests hand the predicate BOXES.
    """

    def paneless(self):
        """This fixture box with its pane removed — a build host."""
        shutil.rmtree(os.path.join(self.proc, "90001"), ignore_errors=True)

    def test_a_box_carrying_panes_keeps_the_ruled_cap(self):
        self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)
        self.assertEqual(gate.SUITE_CAP, 2)

    def test_ONE_canonical_agent_process_is_enough(self):
        """One pane is one daemon's worth of irreplaceable context.

        RENAMED, because the old name (`..._and_MELD_counts_too`) described a
        mechanism this no longer uses: it passed a MELD_CHAT_NAME override to
        the fixture, and detection is now comm/argv, so the override changed
        nothing and the test silently asserted something else. A test named
        for a mechanism it does not exercise is worse than a missing one —
        it reads as coverage. (@codex-3 caught it.)"""
        self.paneless()
        with mock.patch.object(os, "cpu_count", return_value=32):
            # CONTROL on the same observable: this box DOES raise the cap
            # when empty, so the equality below is the agent being seen and
            # not suite_cap answering 2 to everything.
            self.assertEqual(gate.suite_cap(self.proc), 8)
            self.pane(90002)
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_a_paneless_box_derives_its_cap_from_cores(self):
        self.paneless()
        with mock.patch.object(os, "cpu_count", return_value=32):
            self.assertEqual(gate.suite_cap(self.proc), 8)
        with mock.patch.object(os, "cpu_count", return_value=16):
            self.assertEqual(gate.suite_cap(self.proc), 4)

    def test_the_derived_cap_is_bounded_at_both_ends(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the 256-core assertEqual(BUILD_HOST_SUITE_CAP_MAX) before the loop; the loop itself asserts EXACT caps, not >=
        """A huge box does not get an unbounded cap, and a tiny one never
        drops BELOW the ruled floor — the change may only ever raise."""
        self.paneless()
        with mock.patch.object(os, "cpu_count", return_value=256):
            self.assertEqual(gate.suite_cap(self.proc),
                             gate.BUILD_HOST_SUITE_CAP_MAX)
        # `assertGreaterEqual(cap, 2)` alone is satisfied by a suite_cap that
        # returns 2 forever, so each small box asserts its EXACT cap. The
        # ceiling above is the paired control that the value does move.
        for cores, want in ((None, 2), (0, 2), (1, 2), (2, 2), (4, 2),
                            (7, 2), (8, 2), (12, 3), (20, 5)):
            with self.subTest(cores=cores):
                with mock.patch.object(os, "cpu_count", return_value=cores):
                    self.assertEqual(gate.suite_cap(self.proc), want)

    def test_an_unreadable_proc_cannot_buy_the_raised_cap(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the readable paneless tree raising the cap to BUILD_HOST_SUITE_CAP_MAX on the same call, two lines above
        """THE FAIL-CLOSED ARM. The raised cap is granted only on positive
        evidence of an empty box; a /proc that cannot be read has proven
        nothing, so it takes the pane-host cap even on a 256-core machine."""
        # control: the SAME call on a readable paneless tree does raise it
        self.paneless()
        with mock.patch.object(os, "cpu_count", return_value=256):
            self.assertEqual(gate.suite_cap(self.proc),
                             gate.BUILD_HOST_SUITE_CAP_MAX)
            self.assertIsNone(gate._agent_pane_pids(
                os.path.join(self.tmp, "no-such-proc")))
            self.assertEqual(
                gate.suite_cap(os.path.join(self.tmp, "no-such-proc")),
                gate.SUITE_CAP)

    def test_a_LEAKED_env_stamp_alone_does_not_make_a_pane_host(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the paneless assertion of BUILD_HOST_SUITE_CAP_MAX before any process is planted
        """REWRITTEN — the old version tested the OPPOSITE mechanism and only
        passed by accident.

        It was named `..._makes_a_build_host_STRICTER_never_looser` and argued
        that a leaked HELM_CHAT_NAME would pin a build host at 2 — conservative
        but wrong. It then called `self.pane()`, which now plants comm/argv
        `claude`, so it was planting a REAL agent and proving nothing about a
        leaked stamp. (@codex-3 caught that the name, the doc and the control
        had all drifted off the mechanism.)

        The truth under canonical identity is the opposite and better: an
        inherited stamp is IGNORED, because `is_agent` reads comm/argv, which
        do not propagate to children. That is what stopped this box being
        counted as 47 panes when it has ~12."""
        self.paneless()
        with mock.patch.object(os, "cpu_count", return_value=256):
            self.assertEqual(gate.suite_cap(self.proc),
                             gate.BUILD_HOST_SUITE_CAP_MAX)   # control
            # a NON-agent process carrying an inherited seat stamp: exactly
            # what a subprocess of a pane looks like.
            d = os.path.join(self.proc, "90003")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "comm"), "w") as f:
                f.write("bash\n")
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"/bin/bash\0-c\0make\0")
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(b"HELM_CHAT_NAME=seat-a\0")
            self.assertEqual(gate._agent_pane_pids(self.proc), [])
            self.assertEqual(gate.suite_cap(self.proc),
                             gate.BUILD_HOST_SUITE_CAP_MAX)
            # and the REAL agent on the same box still pins it
            self.pane(90004)
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_a_HUGE_environment_can_no_longer_hide_a_pane_at_all(self):
        """SUCCESSOR to the two 64 KB-truncation arms this file used to carry.

        Those defended a real defect — a pane whose HELM_CHAT_NAME sat past a
        64 KB read counted as absent, taking a box from 2 to 8 — against a
        bounded environ read that gate no longer performs. gate identifies an
        agent by comm/argv now, so an environ of ANY size cannot hide one from
        it. The property is stronger than the arms that defended it, so it is
        re-asserted against the new mechanism rather than deleted."""
        self.paneless()
        self.pane(90007)
        d = os.path.join(self.proc, "90007")
        filler = b"".join(b"PAD%05d=%s\0" % (i, b"x" * 80) for i in range(800))
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(filler + b"HELM_CHAT_NAME=seat-a\0")
        self.assertGreater(os.path.getsize(os.path.join(d, "environ")),
                           64 * 1024, "fixture must exceed the old 64 KB read")
        with mock.patch.object(os, "cpu_count", return_value=32):
            # CONTROL: same box, agent removed, really does raise.
            shutil.rmtree(d)
            self.assertEqual(gate.suite_cap(self.proc), 8)
            self.pane(90007)
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(filler + b"HELM_CHAT_NAME=seat-a\0")
            self.assertEqual(gate._agent_pane_pids(self.proc), [90007])
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_an_AGENT_with_an_unreadable_environ_is_STILL_a_pane(self):
        """@codex-3's original finding, now handled where it belongs.

        gate no longer reads environs to decide what an agent is, so a denied
        environ cannot hide a pane: `is_agent` decides on comm/argv, which
        stay readable, and `agent_index` deliberately KEEPS a pane whose
        environ it cannot read rather than dropping it. The hole closes
        because the evidence does not disappear — not because a guard assumed
        the worst, which is the version that made the feature inert."""
        self.paneless()
        self.pane(90007)
        real_open = open

        def denied(path, *a, **kw):
            if str(path).endswith("90007/environ"):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *a, **kw)

        with mock.patch.object(os, "cpu_count", return_value=32):
            # CONTROL on the same box: with the agent REMOVED it really does
            # raise, so the pane cap below is the denied agent still being
            # seen and not a census that never worked.
            shutil.rmtree(os.path.join(self.proc, "90007"))
            self.assertEqual(gate.suite_cap(self.proc), 8)
            self.pane(90007)
            with mock.patch("builtins.open", denied):
                self.assertEqual(gate._agent_pane_pids(self.proc), [90007])
                self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_a_build_host_full_of_protected_STRANGERS_still_raises(self):
        """THE REGRESSION THAT LANDED, as a test rather than a story.

        A build host is mostly other people's processes, and the rejected cut
        counted every unreadable environ as a pane — one live host reported 434
        "panes", its sibling 487, and neither ever left cap 2. The feature landed
        and did nothing. None of these strangers is an agent by comm/argv, so
        none of them may hold the cap down."""
        self.paneless()
        real_open = open
        for pid in range(90100, 90140):
            d = os.path.join(self.proc, str(pid))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "comm"), "w") as f:
                f.write("sshd-session\n")
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(b"PATH=/usr/bin\0")

        def denied(path, *a, **kw):
            p = str(path)
            if "/901" in p and p.endswith("environ"):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *a, **kw)

        with mock.patch.object(os, "cpu_count", return_value=32):
            with mock.patch("builtins.open", denied):
                self.assertEqual(gate._agent_pane_pids(self.proc), [])
                self.assertEqual(gate.suite_cap(self.proc), 8)
            # CONTROL: one REAL agent among the same 40 strangers pins it, so
            # the raise above is an empty census and not a dead one.
            self.pane(90150)
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_gate_does_not_re_derive_what_beacons_already_owns(self):
        """The existence-sweep arm. Three defects came from gate hand-rolling
        a /proc scan that `beacons` already owned; this pins the delegation so
        a fourth cannot arrive by someone re-inlining it."""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(gate._agent_pane_pids)))
        fn = tree.body[0]
        # STRIP THE DOCSTRING FIRST. It NAMES the old scan in order to explain
        # why it is gone, so matching raw source made this arm fail on its own
        # history note — an anti-rot check has to read the CODE.
        body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                               and isinstance(fn.body[0].value, ast.Constant)
                               and isinstance(fn.body[0].value.value, str)) \
            else fn.body
        code = "\n".join(ast.dump(n) for n in body)
        self.assertIn("agent_index", code)          # it really delegates
        self.assertNotIn("HELM_CHAT_NAME", code,
                         "gate is scanning environs again — that is the "
                         "duplicate census, and it shipped three holes")

    def test_the_refusal_names_THIS_hosts_cap_and_which_kind_of_host(self):
        """A refusal quoting a fleet-wide number sends its reader hunting a
        fleet-wide occupant that does not exist."""
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("this host's whole-suite cap is 2", err)
        self.assertIn("carries agent panes", err)
        self.assertNotIn("the fleet whole-suite cap", err)

    def test_a_build_host_admits_past_the_old_fleet_cap(self):
        """THE POINT OF THE WHOLE ROW, asserted as behaviour rather than as
        a returned integer: two suites already running no longer refuse the
        third on a box with nothing to lose."""
        self.paneless()
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        with mock.patch.object(os, "cpu_count", return_value=32):
            self.assertIsNone(gate._admit_suite())
        # control on the SAME two occupants: put one pane back and the
        # identical box refuses, so this is the cap moving and not the
        # census miscounting.
        self.pane(90004)
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", err)


class AdmissionTest(CapBase):
    def test_the_third_run_is_refused_and_the_runners_are_named(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", err)
        for token in ("cap is 2", "pid 101", "pid 102", "/lane/one",
                      "/lane/two"):
            self.assertIn(token, err)

    def test_the_cap_refusal_names_the_repair_path(self):
        """A refusal that names cause but no next step strands its reader
        (honest-refusals law: cause + fix + override-or-why-not). The honest
        repair is WAIT — a slot frees itself when a named run finishes — and
        the honest override note is that none exists: the cap counts
        processes, so no env can argue with it."""
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("Retry when one of those named runs finishes", err)
        self.assertIn("No override exists", err)

    def test_the_second_run_is_admitted_and_holds_an_intent(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.launcher(500)
        position = {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
                    "holder": "seat-b"}
        self.assertIsNone(gate._admit_suite(position))
        rows = gate._admissions_load(gate._admissions_path())
        self.assertEqual([r["position"] for r in rows], ["feedfacefeedface"])
        self.assertIsNone(gate._admission_release("feedfacefeedface"))
        self.assertEqual(gate._admissions_load(gate._admissions_path()), [])

    def test_a_live_pending_intent_holds_a_slot_before_its_suite_exists(self):
        # One suite RUNNING plus one admission whose suite has not spawned
        # yet: the box is committed to two, and the third must refuse — this
        # is the two-launchers-read-N-together race, closed by the ledger.
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.launcher(500)
        self.intent("feedfacefeedface", 500)
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("suite starting", err)
        self.assertIn("seat-a", err)

    def test_a_dead_launchers_intent_self_clears(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.intent("feedfacefeedface", 500)   # pid 500 never planted
        # Positive control: the row IS on disk before admission scrubs it.
        self.assertEqual([r["position"] for r in gate._admissions_load(
            gate._admissions_path())], ["feedfacefeedface"])
        self.assertIsNone(gate._admit_suite())
        self.assertEqual(gate._admissions_load(gate._admissions_path()), [])

    def test_a_running_suite_covers_its_own_intent_counting_once(self):
        self.plant(101, SUITE_ARGV, cgroup="helm-gate-feedfacefeedface",
                   cwd="/lane/one")
        self.launcher(500)
        self.intent("feedfacefeedface", 500)
        # Positive controls: the suite IS counted and carries the intent's
        # own position, so a pass below is coverage, not an empty census.
        rows = gate.suite_census(self.proc)
        self.assertEqual([r["position"] for r in rows],
                         ["feedfacefeedface"])
        self.assertIsNone(gate._admit_suite())

    def test_an_unreadable_census_refuses_never_admits(self):
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "no-such-proc")
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("cannot count running suites", err)


class PressureFloorTest(CapBase):
    def test_a_stalling_box_refuses_admission_and_names_the_reading(self):
        self.psi(12.34)
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("12.34%", err)
        self.assertIn("REFUSED", err)

    def test_the_psi_refusal_names_the_repair_path(self):
        """Same law as the cap refusal: the honest next step is re-run once
        the 10s window clears (and WHERE to watch it fall), and the honest
        override note is that none exists — the floor is the kernel's own
        starvation reading, not a policy a variable can waive."""
        self.psi(12.34)
        err = gate._admit_suite()
        self.assertIsNotNone(err)
        self.assertIn("Re-run once the pressure clears", err)
        self.assertIn("/proc/pressure/memory", err)
        self.assertIn("No override exists", err)

    def test_a_quiet_box_admits(self):
        self.psi(2.00)
        # Positive control: the pressure file was READ, not missing.
        self.assertEqual(gate._psi_some_avg10(self.proc), 2.00)
        self.launcher(500)
        self.assertIsNone(gate._admit_suite(
            {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
             "holder": "seat-b"}))
        # The admitted run's EFFECT exists: its intent row is on disk.
        self.assertEqual([r["position"] for r in gate._admissions_load(
            gate._admissions_path())], ["feedfacefeedface"])

    def test_a_box_without_psi_keeps_its_gate(self):
        # Positive control pair: no pressure file reads as None, and the
        # admitted run still leaves its positive effect (the intent row).
        self.assertIsNone(gate._psi_some_avg10(self.proc))
        self.launcher(500)
        self.assertIsNone(gate._admit_suite(
            {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
             "holder": "seat-b"}))
        self.assertEqual([r["position"] for r in gate._admissions_load(
            gate._admissions_path())], ["feedfacefeedface"])


class RunRefusalTest(CapBase):
    """gate.run itself: the refusal reaches the caller in words, releases
    the FIFO position, and leaves no intent behind."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=self.repo,
                       check=True)
        subprocess.run(("git", "config", "user.email", "cap@test"),
                       cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "cap test"),
                       cwd=self.repo, check=True)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("one\n")
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "first"), cwd=self.repo,
                       check=True)

    def saturate(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")

    def test_a_queued_run_is_refused_released_and_spawns_nothing(self):
        self.saturate()
        # Positive control: the queue snapshot CAN surface a position, so
        # the emptiness asserted below is a release, not a blind reader.
        state, probe, perr = seats.gate_queue_enqueue(
            self.repo, "seat-probe", pid=os.getpid())
        self.assertEqual((state, perr), ("QUEUED", None))
        rows, snap_err = seats.gate_queue_snapshot(self.repo)
        self.assertEqual([r["id"] for r in rows], [probe["id"]])
        seats.gate_queue_finish(self.repo, probe["id"])
        row, err = gate.run(repo=self.repo)
        self.assertIsNone(row)
        self.assertIn("cap is 2", err)
        self.assertIn("pid 101", err)
        rows, snap_err = seats.gate_queue_snapshot(self.repo)
        self.assertIsNone(snap_err, snap_err)
        self.assertEqual(rows, [])
        self.assertEqual(gate._admissions_load(gate._admissions_path()), [])

    def test_a_custom_discovery_command_cannot_bypass_the_cap(self):
        self.saturate()
        row, err = gate.run(repo=self.repo,
                            argv=["python3", "-m", "unittest", "discover"])
        self.assertIsNone(row)
        self.assertIn("cap is 2", err)

    def test_a_custom_targeted_command_stays_outside_the_cap(self):
        self.saturate()
        row, err = gate.run(repo=self.repo, argv=[
            "python3", "-c",
            "import sys; print('Ran 1 test in 0.0s\\n\\nOK', "
            "file=sys.stderr)"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")


class RefusedAdmissionLeavesNoMarker(CapBase):
    """The seam between the process cap and the in-flight marker.

    These two guards were built in different lanes and NEITHER suite could see
    the other: the cap tests call `_admit_suite()` directly and never reach
    `run()`, and the in-flight tests never refuse an admission. The rebase put
    both into one function and the ORDER is the whole of it — open the marker
    before admission and a refused run leaks one, and the next honest gate in
    that repo reads the orphan as a commit racing its own gate and refuses a
    receipt that was fine.

    A composition is its own unit of correctness: two guards each correct per
    spec can jointly guarantee a hole."""

    def _repo(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            subprocess.run(list(cmd), cwd=repo, capture_output=True, timeout=30)
        with open(os.path.join(repo, "README"), "w") as f:
            f.write("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True,
                       timeout=30)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo,
                       capture_output=True, timeout=30)
        return repo

    def _markers(self, repo):
        d = gate.inflight_dir(repo)
        try:
            return sorted(os.listdir(d)) if d else []
        except OSError:
            return []

    def test_a_refused_admission_leaves_no_inflight_marker(self):
        repo = self._repo()
        # POSITIVE CONTROL FIRST, unconditional and on the SAME observable:
        # the marker directory is live and a real open is visible in it. An
        # emptiness assertion below is worthless without this.
        nonce = gate._inflight_open(repo)
        self.assertEqual(len(self._markers(repo)), 1)
        gate._inflight_close(repo, nonce)
        self.assertEqual(self._markers(repo), [])

        # cap is 2: two planted suite processes make the third refuse
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, SUITE_ARGV, cwd="/lane/two")
        row, err = gate.run(repo=repo)
        self.assertIsNone(row)
        self.assertIn("REFUSED", err or "")
        self.assertEqual(
            self._markers(repo), [],
            "a run refused at admission left an in-flight marker behind; the "
            "next gate in this repo will read it as a commit racing its own "
            "run and refuse an honest receipt")


if __name__ == "__main__":
    unittest.main()
