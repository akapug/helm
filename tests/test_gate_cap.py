#!/usr/bin/env python3
"""The fleet whole-suite cap: admission counts PROCESSES, never lock-holders.

Concurrent whole-suite activity can coincide with resource pressure and loss
of agent panes on a pane host. The exact causal mechanism was not proven, and
Orca panes may span daemon generations rather than share one daemon. The cap is
TWO on a box that CARRIES PANES, and `PerHostCapTest` pins its per-host scope.
The existing gatelock cannot carry it: a bare
`python3 -m unittest discover`
never touches the lock. Every fixture here fabricates its own proc tree —
the guard under test is exactly the difference between what a process IS and
what it holds, so the tests hand it processes, not locks."""
import ast
import errno
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import gate, pk, seats
from tests import HostAdmissionAsked
from tests._tmphome import helm_tree, pin_admission

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_PROC",
            "HELM_GATE_SUITE_CAP",
            "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
            "CODEX_SESSION_ID")

SUITE_ARGV = ("python3", "-m", "unittest", "discover", "-s", "tests",
              "-t", ".")


class WorldNarrativeGuardTest(unittest.TestCase):
    """The public rationale keeps the mechanism, not the incident timeline."""

    def test_scoped_outage_narratives_stay_timeless(self):  # noqa: VACUOUS_ASSERTION — six exact bounded regions are positively anchored before their old incident details are asserted absent
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = (
            "helm/gate.py", "tests/test_gate_cap.py", "helm/notify.py",
            "helm/telegram.py", "docs/VERBS.md", "helm/cli.py",
        )
        texts = {}
        for path in paths:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                texts[path] = f.read()
        self.assertEqual(set(texts), set(paths))

        def between(path, start, end, occurrence="only"):
            """The text between two markers, REFUSING an ambiguous marker.

            `split(marker, 1)` silently takes the FIRST hit, and a marker that
            appears twice therefore selects a region nobody chose — which is
            how helm/cli.py's region came out SEVENTY-FIVE characters long, at
            a registry entry three hundred lines above the narrative it was
            supposed to bound, on trunk and on the WORLD car alike. The anchor
            then missed and the arm blamed the file. So ambiguity is now a
            LOUD failure with its own count, and a caller that genuinely wants
            the later pair says so by name.
            """
            text = texts[path]
            self.assertIn(start, text, path)
            self.assertIn(end, text, path)
            if occurrence == "only":
                self.assertEqual(
                    text.count(start), 1,
                    "%s: start marker %r occurs %d times — a region bounded by "
                    "an ambiguous marker is not the region you named"
                    % (path, start, text.count(start)))
                return text.split(start, 1)[1].split(end, 1)[0]
            self.assertEqual(occurrence, "last", occurrence)
            return text.rsplit(start, 1)[1].split(end, 1)[0]

        def class_body(path, name):
            """The class's real source, located by AST — never by markers.

            A marker-delimited region is defeated by THIS FILE, and it was:
            the guard's own source quotes "class PerHostCapTest" as an
            argument, so splitting the text on that literal lands INSIDE the
            guard and selects its own argument list. Measured on the composed
            tree before this cure: the region was TWENTY characters — the
            string '",\\n                "' — and its required anchor then
            missed. That failure reads as "the translated narrative is gone"
            when what actually happened is that nothing was scanned, which is
            the worse of the two failures because it accuses the wrong file.
            Asking the parser where the class is makes the guard unable to
            select itself.
            """
            for node in ast.parse(texts[path]).body:
                if isinstance(node, ast.ClassDef) and node.name == name:
                    seg = ast.get_source_segment(texts[path], node)
                    self.assertIsNotNone(seg, "%s: no source for %s" % (path, name))
                    return seg
            self.fail("%s defines no class %s" % (path, name))

        test_module = texts["tests/test_gate_cap.py"].split('"""', 2)[1]
        per_host = class_body("tests/test_gate_cap.py", "PerHostCapTest")
        # POSITIVE CONTROL on the SELECTION, not on the narrative: prove we are
        # holding the real class body before asking what it says. Without this
        # the guard cannot distinguish a scrubbed region from an empty one, and
        # the empty one is what a self-matching marker produces.
        self.assertTrue(per_host.startswith("class PerHostCapTest"), per_host[:80])
        self.assertIn("def test_the_derived_cap", per_host)
        self.assertIn("def test_a_paneless_refusal_does_not_claim_pane_context",
                      per_host)
        self.assertGreater(len(per_host), 2000, len(per_host))
        regions = {
            "helm/gate.py": (
                between("helm/gate.py",
                        "# ------------------------------------------------------------- fleet cap",
                        "PSI_SOME_FLOOR")
                + between("helm/gate.py", '"this host\'s whole-suite cap',
                          "_FAB_HANDOFF")
                + between("helm/gate.py",
                          'elif focus and len(plan["selected"]) * 2',
                          "capacity, admit_err = _admit_suite")
            ),
            "tests/test_gate_cap.py": test_module + per_host,
            "helm/notify.py": texts["helm/notify.py"].split('"""', 2)[1],
            "helm/telegram.py": texts["helm/telegram.py"].split('"""', 2)[1],
            "docs/VERBS.md": between(
                "docs/VERBS.md", "### `helm telegram", "\n### "),
            # LAST occurrence, named rather than defaulted: both markers appear
            # twice in cli.py — once in a short registry three hundred lines
            # above, once around the verb help text WORLD actually translated.
            # `split(...,1)` took the first pair and produced a SEVENTY-FIVE
            # character region, so the anchor missed and the arm blamed the file.
            "helm/cli.py": between(
                "helm/cli.py", '    "telegram":', '\n    "dispatch":',
                occurrence="last"),
        }
        # CONTROL: every bounded site must keep its translated topology or
        # uncertainty. This proves the scan saw the intended narrative.
        anchors = {
            # WORLD's causal clause about concurrent suites WAS an anchor here
            # and is not one any more. It was the single phrase in WORLD's
            # translation that
            # the runtime car deletes — runtime's whole contract is that the
            # refusal states facts and never a mechanism nobody proved — so on
            # the composed tree this must-hit could only ever fail, and it
            # would fail as "the scan saw nothing" rather than as the design
            # disagreement it actually is. The other five anchors are untouched
            # by runtime and two of them ("exact causal mechanism was not
            # proven", "may span daemon generations") exist ONLY because WORLD
            # translated this file, so the control stays a real must-hit rather
            # than a weakened one.
            "helm/gate.py": ("exact causal mechanism was not proven",
                             "may span daemon generations",
                             "admitted-", "suite limit",
                             "exact cause remains unproven"),
            "tests/test_gate_cap.py": (
                "exact causal mechanism was not proven",
                "multiple daemon generations"),
            "helm/notify.py": ("may span daemon generations",
                               "co-located reachability risk"),
            # The file says "rather than one daemon common to every pane"; the
            # anchor said "not one daemon...". WORLD's own tip fails this on
            # its own file, so the anchor was never a must-hit — it was a typo
            # with the authority of a control. Anchored on the clause both
            # spellings share, which is still positive evidence the translated
            # narrative is present.
            "helm/telegram.py": ("may span daemon generations",
                                 "one daemon common to every pane"),
            "docs/VERBS.md": ("may span daemon generations",
                              "no single daemon is assumed"),
            "helm/cli.py": ("may span daemon generations",
                            "not one daemon common to every pane"),
        }
        # NARRATIVE MATCHING IS WHITESPACE-INSENSITIVE, and both directions of
        # this check needed it. Prose in a docstring is WRAPPED, so a phrase
        # the author wrote as one sentence reaches this assertion split across
        # a newline: helm/telegram.py really does say "may span daemon
        # generations" and really did fail its own anchor, because the file
        # holds it as "may span daemon\ngenerations". That failure reads as
        # "the translated narrative is missing" when the narrative is right
        # there — the worst kind of red, because it accuses a correct file.
        # The FORBIDDEN half is the more important one: an incident string that
        # happens to wrap is exactly the one a scrub would miss, so matching
        # raw text made the guard weakest against the case it exists for.
        # Collapsing runs of whitespace to single spaces fixes both, and the
        # forbidden phrases are built by concatenation precisely so they exist
        # as contiguous strings at runtime while leaving nothing to grep for
        # in this file. The positive region controls above stay on the RAW
        # segment, because they assert STRUCTURE (this is the class body, it
        # is this long) rather than prose.
        def flat(text):
            return re.sub(r"\s+", " ", text)

        flat_regions = {p: flat(b) for p, b in regions.items()}
        for path, expected in anchors.items():
            for phrase in expected:
                self.assertIn(flat(phrase), flat_regions[path], path)

        forbidden = (
            "2026" + "-08-03", "03" + ":44",
            "all " + "seven panes", "seven " + "dead seats",
            "six " + "concurrent runs", "six " + "concurrent whole-suite",
            "seven-seat " + "outage", "four " + "hours nothing",
            "within " + "two seconds", "nine " + "minutes",
            "46 " + "agent processes", "87 " + "GB", "~720 " + "MB",
        )
        for path, body in flat_regions.items():
            self.assertTrue(body, path)
            for phrase in forbidden:
                self.assertNotIn(flat(phrase), body, path)


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
        self.admissions = os.path.join(self.tmp, "gate-admissions.json")
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

    def plant(self, pid, argv, cgroup=None, cwd=None, start=7, state="S",
              ppid=1):
        """One fabricated process: cmdline + stat (+ cgroup, + cwd). `ppid`
        goes into stat field 4 so the census can walk a spawned subtree."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in argv))
        fields = [state, str(ppid)] + ["0"] * 17 + [str(start)]
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
        pk.write_json(self.admissions, {"v": 1, "admissions": [
            {"position": position, "pid": pid, "starttime": start,
             "holder": holder, "ts": "2026-01-01T00:00:00Z"}]})

    def psi(self, avg10):
        d = os.path.join(self.proc, "pressure")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "memory"), "w") as f:
            f.write("some avg10=%.2f avg60=0.00 avg300=0.00 total=1\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
                    % avg10)

    def paneless(self):
        """This fixture box with its pane removed — a build host."""
        shutil.rmtree(os.path.join(self.proc, "90001"), ignore_errors=True)

    def admit(self, *args, **kwargs):
        """Return admission words while retaining the structured grant."""
        kwargs.setdefault("proc_dir", self.proc)
        kwargs.setdefault("admissions_path", self.admissions)
        self.capacity, err = gate._admit_suite(*args, **kwargs)
        return err

    def admission_rows(self):
        return gate._admissions_load(self.admissions)

    def use_fixture_admission_for_run(self):
        """Make gate.run use this test's explicit host-authority seams: the
        suite's one spelling of that, with this fixture's box and ledger."""
        pin_admission(self, proc=self.proc, ledger=self.admissions)


class HostAdmissionPathTest(CapBase):
    def test_every_helm_home_on_one_node_shares_one_admission_door(self):  # noqa: VACUOUS_ASSERTION — both node roots and both same-node paths are asserted present before their equality and separation are checked
        node_a = os.path.join(self.tmp, "snoozy-runtime")
        node_b = os.path.join(self.tmp, "drowsy-runtime")
        with mock.patch.object(gate.home, "ram_root", return_value=node_a):
            os.environ["HELM_HOME"] = os.path.join(self.tmp, "run-one")
            os.environ["HELM_PROC"] = os.path.join(self.tmp, "child-proc-one")
            first = gate._admissions_path()
            os.environ["HELM_HOME"] = os.path.join(self.tmp, "run-two")
            os.environ["HELM_PROC"] = os.path.join(self.tmp, "child-proc-two")
            second = gate._admissions_path()
        with mock.patch.object(gate.home, "ram_root", return_value=node_b):
            other_node = gate._admissions_path()
        self.assertEqual(first, second)
        self.assertEqual(os.path.dirname(first), node_a)
        self.assertEqual(os.path.dirname(other_node), node_b)
        self.assertNotEqual(first, other_node)
        self.assertNotIn("run-one", first)
        self.assertNotIn("child-proc", first)

    def test_admission_uses_host_proc_not_the_child_redirect(self):
        topology = {"cores": 32, "reason": "32 host-online CPUs"}
        census = mock.Mock(return_value=[])
        psi = mock.Mock(return_value=None)
        grant = mock.Mock(return_value={"cap": 16, "reason": "build host"})
        # CapBase deliberately exports HELM_PROC=self.proc. Production admission
        # must still sample the host, while an explicit proc_dir remains the
        # narrow test seam used by every fabricated-process test in this file.
        # THIS ARM IS ABOUT THE HOST PATH, so it says so to the suite's
        # tripwire; every read on that path is mocked above.
        with mock.patch.object(gate, "suite_census", census), \
                mock.patch.object(gate, "_psi_some_avg10", psi), \
                mock.patch.object(gate, "_capacity_grant", grant), \
                HostAdmissionAsked():
            capacity, err = gate._admit_suite(
                topology=topology, admissions_path=self.admissions)
        self.assertIsNone(err, err)
        self.assertEqual(capacity["cap"], 16)
        census.assert_called_once_with("/proc")
        psi.assert_called_once_with("/proc")
        grant.assert_called_once_with("/proc", topology)


class BuildHostSuiteCapTest(CapBase):
    def test_sysconf_discovers_host_online_cpus_and_rejects_bad_answers(self):  # noqa: VACUOUS_ASSERTION — the fixed table always runs a valid 32-core positive row before the malformed fail-closed rows
        cases = (
            ("valid", {"return_value": 32}, 32),
            ("zero", {"return_value": 0}, None),
            ("malformed", {"return_value": "32"}, None),
            ("unavailable", {"side_effect": ValueError("unknown")}, None),
        )
        for name, outcome, want in cases:
            with self.subTest(name=name), \
                    mock.patch.object(gate.os, "sysconf", **outcome) as probe:
                self.assertEqual(gate._online_cpu_count(), want)
                probe.assert_called_once_with("SC_NPROCESSORS_ONLN")

    def test_a_paneless_32_core_host_has_sixteen_slots(self):
        self.paneless()
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            capacity = gate.suite_capacity(self.proc)
        self.assertEqual(capacity, {
            "cap": 16,
            "reason": "paneless build host, 32 host-online CPUs",
        })
        self.assertEqual(gate.BUILD_HOST_SUITE_CAP_MAX, 16)
        self.assertEqual(gate._CORES_PER_SUITE, 2)

    def test_a_pane_host_remains_at_two(self):
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            capacity = gate.suite_capacity(self.proc)
        self.assertEqual(capacity, {
            "cap": 2,
            "reason": "carries agent panes",
        })
        self.assertEqual(gate.SUITE_CAP, 2)

    def test_unlistable_proc_fails_closed_to_two(self):
        missing = os.path.join(self.tmp, "missing-proc")
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            capacity = gate.suite_capacity(missing)
        self.assertEqual(capacity["cap"], 2)
        self.assertIn("process table is unreadable", capacity["reason"])

    def test_unavailable_topology_fails_closed_to_two(self):
        self.paneless()
        with mock.patch.object(gate, "_online_cpu_count", return_value=None):
            capacity = gate.suite_capacity(self.proc)
        self.assertEqual(capacity["cap"], 2)
        self.assertIn("paneless host", capacity["reason"])
        self.assertIn("unavailable", capacity["reason"])

    def test_memory_psi_over_floor_refuses_regardless_of_occupant_count(self):  # noqa: VACUOUS_ASSERTION — the fixed cases execute unconditional exact PSI refusals and the final call-list proves both branches ran
        topology = {"cores": 32, "reason": "32 host-online CPUs"}
        psi = mock.Mock(return_value=12.34)
        with mock.patch.object(gate, "_psi_some_avg10", psi):
            for count in (0, 2):
                with self.subTest(count=count):
                    if count:
                        self.plant(101, SUITE_ARGV, cwd="/lane/one")
                        self.plant(102, SUITE_ARGV, cwd="/lane/two")
                    err = self.admit(proc_dir=self.proc, topology=topology)
                    capacity = self.capacity
                    self.assertEqual(capacity["cap"], 2)
                    self.assertIn("PSI", err)
                    if count:
                        self.assertIn("whole-suite cap", err)
                    else:
                        self.assertNotIn("whole-suite cap", err)
        self.assertEqual(
            psi.call_args_list,
            [mock.call(self.proc), mock.call(self.proc)],
            "the PSI authority branch must execute for both occupant counts")
        self.assertEqual(gate.PSI_SOME_FLOOR, 10.0)

    def test_a_scalar_cap_of_two_does_not_invent_panes(self):
        self.paneless()
        topology = {
            "cores": None,
            "reason": "host online CPU count is unavailable",
        }
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, SUITE_ARGV, cwd="/lane/two")
        err = self.admit(proc_dir=self.proc, topology=topology)
        self.assertEqual(self.capacity["cap"], 2)
        self.assertIn("paneless host", err)
        self.assertNotIn("carries agent panes", err)

    def test_a_pane_starting_during_census_forces_the_final_grant_to_two(self):  # noqa: VACUOUS_ASSERTION — the exact capacity and called-once census are unconditional positive effects of the injected pane start
        self.paneless()
        topology = {"cores": 32, "reason": "32 host-online CPUs"}
        census = gate.suite_census

        def start_pane(proc_dir):
            rows = census(proc_dir)
            self.pane(90002)
            return rows

        with mock.patch.object(
                gate, "suite_census", side_effect=start_pane) as sampled:
            err = self.admit(proc_dir=self.proc, topology=topology)
        capacity = self.capacity
        self.assertIsNone(err, err)
        self.assertEqual(capacity, {
            "cap": 2,
            "reason": "carries agent panes",
        })
        sampled.assert_called_once_with(self.proc)


class SuiteShapeTest(CapBase):
    def test_the_gates_own_suite_argv_is_the_must_hit(self):
        # Seed the scan with a MUST-HIT: the exact argv gate.run spawns. If
        # this row ever reads False the census counts NOTHING and every
        # other assertion here is measuring an empty set.
        self.assertTrue(gate._suite_shaped(
            [os.path.realpath("/usr/bin/python3")] + list(gate.SUITE)))
        self.assertTrue(gate._suite_shaped(list(SUITE_ARGV)))

    def test_only_exact_serial_discovery_has_landing_authority(self):
        canonical = list(SUITE_ARGV)
        self.assertTrue(gate._suite_shaped(canonical))
        self.assertTrue(gate._suite_shaped(
            ["python3", "-u"] + canonical[1:]))
        for argv in (
                ["python3", "-m", "unittest"],
                ["python", "-m", "unittest", "discover"],
                ["/usr/bin/python3.14", "-munittest", "discover"],
                ["graalpy", "-m", "unittest", "discover", "-s", "tests"],
                ["python3", "-m", "unittest", "discover", "-s", "tests",
                 "-t", ".", "-k", "gate"],
                ["python3", "-m", "unittest", "discover", "-s", "tests",
                 "-t", ".", "-p", "test_gate*.py"]):
            with self.subTest(argv=argv):
                self.assertFalse(gate._suite_shaped(argv))

    def test_documented_discovery_spellings_consume_only_the_host_cap(self):  # noqa: VACUOUS_ASSERTION — the fixed command table classifies real suite roots, then two planted roots unconditionally produce a refusal
        commands = (
            ["python3", "-m", "unittest"],
            ["python", "-m", "unittest", "discover"],
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
            ["python3", "-m", "unittest", "-v"],
            ["python3", "-m", "unittest", "-k", "gate"],
            ["python3", "-m", "unittest", "discover", "-v", "-k", "gate"],
            ["python3", "-m", "unittest", "discover", "-k", "gate",
             "-kshard"],
            ["python3", "-m", "unittest", "discover", "-kgate"],
            ["python3", "-m", "unittest", "discover", "-stests", "-t."],
            ["python3", "-m", "unittest", "discover", "-ptest*.py"],
            ["python3", "-m", "unittest", "discover",
             "--start-directory=tests", "--top-level-directory=.",
             "--pattern=test*.py"],
            ["/usr/bin/python3.14", "-munittest", "discover", "-s", "tests"],
        )
        for argv in commands:
            with self.subTest(argv=argv):
                self.assertTrue(gate._suite_cap_shaped(argv))
                self.assertEqual(gate._suite_root_kind(argv), "suite")
                if argv != list(SUITE_ARGV):
                    self.assertFalse(gate._suite_shaped(argv))

        for argv in (
                ["python3", "-m", "unittest", "-k"],
                ["python3", "-m", "unittest", "-k", "-v"],
                ["python3", "-m", "unittest", "discover", "-stests",
                 "tests.test_gate"],
                ["python3", "-m", "unittest", "discover",
                 "--start-directory=tests/unit"],
                ["python3", "-m", "unittest", "discover", "-stests",
                 "--start-directory=tests"],
                ["python3", "-m", "unittest", "discover", "-p"],
                ["python3", "-m", "unittest", "discover", "-stests",
                 "-t.", "--top-level-directory=."]):
            with self.subTest(argv=argv):
                self.assertFalse(gate._suite_cap_shaped(argv))
                self.assertNotEqual(gate._suite_root_kind(argv), "suite")

        self.plant(121, commands[0])
        self.plant(122, commands[-1])
        self.assertIn("REFUSED", self.admit() or "")

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
        # WIDENED (task/923): a targeted run is now SEEN — it was invisible
        # before, which is how a 3.6G single-method run hid from every layer.
        self.plant(103, ("python3", "-m", "unittest", "tests.test_one"),
                   cwd="/lane/three")
        # a gatechild WRAPPER (script path first) is still NOT a root — its
        # real suite child is the root, so counting the wrapper double-counts.
        self.plant(104, ("python3", "/x/gatechild.py", "--guard", "--",
                         "python3", "-m", "unittest", "discover"))
        rows = gate.suite_census(self.proc)
        by = {r["pid"]: r for r in rows}
        self.assertEqual(sorted(by), [101, 102, 103])   # 104 stays invisible
        self.assertEqual(by[101]["kind"], "suite")
        self.assertEqual(by[102]["kind"], "suite")
        self.assertEqual(by[103]["kind"], "targeted")
        self.assertEqual(by[101]["position"], "aabbccdd11223344")
        self.assertEqual(by[101]["cwd"], "/lane/one")
        self.assertIsNone(by[102]["position"])
        self.assertIsNone(by[103]["position"])

    def test_shards_wrappers_and_nested_gates_keep_one_owner_identity(self):
        owner = "aabbccdd11223344"
        # Two sibling shard roots share the admitted owner's cgroup. A wrapper
        # is visible as cost under that owner but is not itself suite-shaped.
        self.plant(101, SUITE_ARGV, cgroup="helm-gate-" + owner,
                   cwd="/lane/owner")
        self.plant(102, ("python3", "-m", "unittest"),
                   cgroup="helm-gate-" + owner, ppid=700)
        self.plant(103, ("python3", "/x/gatechild.py", "--supervise"),
                   cgroup="helm-gate-" + owner, ppid=101)
        # A nested gate may acquire a new containment cgroup, but ancestry says
        # it is still work owned by 101, not a seventeenth admission.
        self.plant(104, ("python3", "-m", "unittest", "discover"),
                   cgroup="helm-gate-deadbeefdeadbeef", ppid=103)
        self.plant(105, ("python3", "-c", "work()"), ppid=104)
        # Polarity: a genuinely independent owner stays a separate row.
        self.plant(201, SUITE_ARGV, cgroup="helm-gate-0011223344556677",
                   cwd="/lane/other")

        rows = gate.suite_census(self.proc)
        self.assertEqual([row["owner"] for row in rows],
                         [owner, "0011223344556677"])
        first = rows[0]
        self.assertEqual(first["pid"], 101)
        self.assertEqual(first["position"], owner)
        self.assertEqual({child["pid"] for child in first["children"]},
                         {102, 103, 104, 105})
        self.assertEqual(rows[1]["pid"], 201)

    def test_an_unreadable_proc_tree_is_none_never_empty(self):
        # Positive control: the same call reads a REAL tree as non-empty,
        # so the None below is the unreadable tree and not a broken census.
        self.plant(101, SUITE_ARGV)
        self.assertEqual([r["pid"] for r in gate.suite_census(self.proc)],
                         [101])
        self.assertIsNone(gate.suite_census(
            os.path.join(self.tmp, "no-such-proc")))


class SuiteRootKindTest(CapBase):
    def test_the_classifier_names_every_root_shape_and_only_those(self):
        # MUST-HIT positive control: the canonical whole-tree run classifies,
        # so a miss below is a real gap and not the classifier saying None to
        # everything.
        self.assertEqual(gate._suite_root_kind(list(SUITE_ARGV)), "suite")
        for kind, argvs in (
            ("suite", (["python3", "-m", "unittest"],
                       ["python", "-m", "unittest", "discover"])),
            ("targeted", (["python3", "-m", "unittest", "tests.test_x"],
                          ["python3", "-m", "unittest",
                           "tests.test_x.Case.test_y"])),
            ("pytest", (["python3", "-m", "pytest", "tests/"],
                        ["python3", "-m", "py.test", "-k", "gate"])),
        ):
            for argv in argvs:
                self.assertEqual(gate._suite_root_kind(argv), kind, argv)
        # POLARITY control: a hook, inline code, the helm launcher, a shell
        # wrapper and empty argv are NONE — a root is a real python runner.
        for argv in (["python3", "hook.py"],
                     ["python3", "-c", "import x"],
                     ["python3", "-m", "helm", "gate", "run"],
                     ["bash", "-c", "python3 -m unittest"],
                     []):
            self.assertIsNone(gate._suite_root_kind(argv), argv)


class SubtreeVisibilityTest(CapBase):
    def test_a_test_spawned_child_is_visible_under_its_root(self):
        """THE LANE DEFECT (suite-routing-blind-to-test-spawned-child). A
        child a test spawns passes through no gate admission and resolves no
        PATH shim, so every layer above the census was blind to it. The
        census sees it because /proc records a process no matter how it
        launched — including the two shapes the map found real, a `-c` child
        and a script child, neither itself suite-shaped."""
        self.plant(200, SUITE_ARGV, cwd="/lane")
        self.plant(201, ("python3", "-c", "import heavy; heavy.run()"),
                   ppid=200)
        self.plant(202, ("python3", "sub.py", "spec"), ppid=201)  # grandchild
        rows = gate.suite_census(self.proc)
        root = next(r for r in rows if r["pid"] == 200)
        self.assertEqual({c["pid"] for c in root["children"]}, {201, 202})
        # MUTATION/POLARITY control: the walk attaches children to suite ROOTS
        # only — a bystander's subtree must not leak in. Drop the root-anchor
        # (walk every process) and this assertion reddens.
        self.plant(300, ("python3", "editor.py"))          # NOT a root
        self.plant(301, ("python3", "-c", "typing"), ppid=300)
        rows2 = gate.suite_census(self.proc)
        self.assertNotIn(300, [r["pid"] for r in rows2])
        self.assertTrue(all(301 not in {c["pid"] for c in r["children"]}
                            for r in rows2))


class WidenedAdmissionTest(CapBase):
    def test_targeted_runs_are_visible_but_do_not_count_toward_the_cap(self):  # noqa: VACUOUS_ASSERTION — the same fabricated census unconditionally proves all three targeted roots are visible before admission succeeds
        """A targeted run is SEEN (task/923) yet must not consume a cap slot:
        the cap is a proxy for concurrent DISCOVERY runs (gate.py:1211), and
        re-pricing it on targeted runs would refuse legitimate single-method
        work — the box's typeability is protected by cost/PSI, not by counting
        every narrow run as a whole suite."""
        self.plant(401, ("python3", "-m", "unittest", "t.A.test_a"))
        self.plant(402, ("python3", "-m", "unittest", "t.B.test_b"))
        self.plant(403, ("python3", "-m", "pytest", "x.py::C::test_c"))
        # CONTROL: the census DID see all three (non-empty, all widened kinds).
        rows = gate.suite_census(self.proc)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["kind"] in ("targeted", "pytest") for r in rows))
        # yet three of them — over the cap of 2 — do not block a real suite.
        self.launcher(500)
        self.assertIsNone(self.admit(
            {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
             "holder": "seat-b"}))

    def test_the_cap_refusal_hands_off_to_the_fab(self):
        """Refusal-plus-handoff (task/923): the door-slam that named no next
        step trained seats to go quiet; the refusal now names the fab."""
        self.plant(601, SUITE_ARGV, cwd="/lane/one")
        self.plant(602, ("python3", "-m", "unittest"), cwd="/lane/two")
        # CONTROL: two whole-tree runs ARE counted (cap is 2 on this pane box).
        self.assertEqual(gate.suite_cap(self.proc), 2)
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", err)
        # WORLD's requirement was that a PUBLIC refusal still explain itself
        # rather than only slam; runtime's was that it explain itself with
        # FACTS rather than a mechanism nobody proved. Composing the two cars
        # made those collide on one sentence. The factual contract satisfies
        # both: the refusal names the limit it hit, the runs occupying it, the
        # condition that clears it, and what the cap actually counts — with no
        # causal claim about pressure, and no incident narrative.
        self.assertIn("admitted-suite limit", err)
        self.assertIn("Retry when one of those named runs finishes", err)
        self.assertIn("it counts processes, not permission", err)
        self.assertNotIn("compound host " + "pressure", err)
        self.assertNotIn("2026" + "-08-03", err)
        self.assertNotIn("03" + ":44", err)
        self.assertNotIn("all " + "seven panes", err)
        self.assertIn("fab gate --repo", err)
        self.assertIn("fab build --repo", err)


class PerHostCapTest(CapBase):
    """The cap is PER-HOST, and the predicate is panes — never a hostname.

    Pane hosts use the conservative cap because they carry irreplaceable agent
    context. Paneless build hosts derive a larger cap because that casualty
    cannot occur there. Suite RSS measurements were far below build-host
    headroom. The pane-host outage correlated with concurrent suites and
    pressure, but did not prove an exact cause.

    A hostname allowlist would rot on the next host added or renamed, and it
    encodes the wrong fact: the casualty was irreplaceable agent context, not
    a machine's name. So these tests hand the predicate BOXES.
    """

    def test_ONE_canonical_agent_process_is_enough(self):
        """One pane is enough to put irreplaceable context at risk.

        Orca may serve panes across multiple daemon generations, so the pane
        predicate deliberately does not infer a single shared daemon. Detection
        uses comm/argv. A MELD_CHAT_NAME fixture override changes nothing and
        would only create false coverage of a second mechanism. The test name
        and fixture must describe the mechanism they exercise."""
        self.paneless()
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            # CONTROL on the same observable: this box DOES raise the cap
            # when empty, so the equality below is the agent being seen and
            # not suite_cap answering 2 to everything.
            self.assertEqual(gate.suite_cap(self.proc), 16)
            self.pane(90002)
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_the_derived_cap_is_bounded_at_both_ends(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the 256-core assertEqual(BUILD_HOST_SUITE_CAP_MAX) before the loop; the loop itself asserts EXACT caps, not >=
        """A huge box does not get an unbounded cap, and a tiny one never
        drops BELOW the ruled floor — the change may only ever raise."""
        self.paneless()
        with mock.patch.object(gate, "_online_cpu_count", return_value=256):
            self.assertEqual(gate.suite_cap(self.proc),
                             gate.BUILD_HOST_SUITE_CAP_MAX)
        # `assertGreaterEqual(cap, 2)` alone is satisfied by a suite_cap that
        # returns 2 forever, so each small box asserts its EXACT cap. The
        # ceiling above is the paired control that the value does move.
        for cores, want in ((None, 2), (0, 2), (1, 2), (2, 2), (4, 2),
                            (7, 3), (8, 4), (12, 6), (20, 10)):
            with self.subTest(cores=cores):
                with mock.patch.object(gate, "_online_cpu_count", return_value=cores):
                    self.assertEqual(gate.suite_cap(self.proc), want)

    def test_a_LEAKED_env_stamp_alone_does_not_make_a_pane_host(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the paneless assertion of BUILD_HOST_SUITE_CAP_MAX before any process is planted
        """REWRITTEN — the old version tested the OPPOSITE mechanism and only
        passed by accident.

        It was named `..._makes_a_build_host_STRICTER_never_looser` and argued
        that a leaked HELM_CHAT_NAME would pin a build host at 2 — conservative
        but wrong. It then called `self.pane()`, which now plants comm/argv
        `claude`, so it was planting a REAL agent and proving nothing about a
        leaked stamp. (a review caught that the name, the doc and the control
        had all drifted off the mechanism.)

        The truth under canonical identity is the opposite and better: an
        inherited stamp is IGNORED, because `is_agent` reads comm/argv, which
        do not propagate to children. That is what stopped this box being
        counted as 47 panes when it has ~12."""
        self.paneless()
        with mock.patch.object(gate, "_online_cpu_count", return_value=256):
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

    def test_a_HUGE_environment_can_no_longer_hide_a_pane_at_all(self):  # noqa: VACUOUS_ASSERTION — removing the planted pane unconditionally proves the same paneless box raises to 16 before restoring the pane and asserting the floor
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
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            # CONTROL: same box, agent removed, really does raise.
            shutil.rmtree(d)
            self.assertEqual(gate.suite_cap(self.proc), 16)
            self.pane(90007)
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(filler + b"HELM_CHAT_NAME=seat-a\0")
            self.assertEqual(gate._agent_pane_pids(self.proc), [90007])
            self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_an_AGENT_with_an_unreadable_environ_is_STILL_a_pane(self):
        """The original finding, now handled where it belongs.

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

        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            # CONTROL on the same box: with the agent REMOVED it really does
            # raise, so the pane cap below is the denied agent still being
            # seen and not a census that never worked.
            shutil.rmtree(os.path.join(self.proc, "90007"))
            self.assertEqual(gate.suite_cap(self.proc), 16)
            self.pane(90007)
            with mock.patch("builtins.open", denied):
                self.assertEqual(gate._agent_pane_pids(self.proc), [90007])
                self.assertEqual(gate.suite_cap(self.proc), gate.SUITE_CAP)

    def test_a_build_host_full_of_protected_STRANGERS_still_raises(self):
        """THE REGRESSION THAT LANDED, as a test rather than a story.

        A build host is mostly other people's processes, and the rejected cut
        counted every unreadable environ as a pane — one build host reported
        434 "panes", another 487, and neither ever left cap 2. The feature landed
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

        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            with mock.patch("builtins.open", denied):
                self.assertEqual(gate._agent_pane_pids(self.proc), [])
                self.assertEqual(gate.suite_cap(self.proc), 16)
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
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("this host's whole-suite cap is 2", err)
        self.assertIn("carries agent panes", err)
        self.assertIn("REFUSED at this host's admitted-suite limit", err)
        self.assertNotIn("the fleet whole-suite cap", err)
        # SPLIT ON PURPOSE, and the split is the whole point. These are the
        # incident literals the refusal must never carry, so the arm has to
        # name them — but the WORLD car scrubs this tree for public release,
        # and a scan cannot tell a literal QUOTED IN ORDER TO FORBID IT from
        # one that ships. Composing runtime (which added this control) with
        # WORLD (which never saw it) put exact incident strings back into a
        # tree WORLD had cleaned; neither author could have seen that, because
        # each car is correct alone. Concatenation keeps the negative control
        # at full strength while leaving no exact literal to find — the same
        # device test_the_cap_refusal_hands_off_to_the_fab already uses.
        for incident in ("2026" + "-08-03", "03" + ":44", "six " + "concurrent",
                         "kil" + "led", "terminal " + "daemon",
                         "PTY " + "daemon"):
            self.assertNotIn(incident, err)

    def test_a_paneless_refusal_does_not_claim_pane_context(self):
        self.paneless()
        for pid in range(101, 117):
            self.plant(pid, SUITE_ARGV, cwd="/lane/%d" % pid)
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("paneless build host", err)
        self.assertIn("admitted-suite limit", err)
        self.assertNotIn("pane context", err)

    def test_a_build_host_admits_past_the_old_fleet_cap(self):  # noqa: VACUOUS_ASSERTION — the identical two occupants refuse after the pane is restored, proving the earlier admission came from the paneless grant
        """THE POINT OF THE WHOLE ROW, asserted as behaviour rather than as
        a returned integer: two suites already running no longer refuse the
        third on a box with nothing to lose."""
        self.paneless()
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        with mock.patch.object(gate, "_online_cpu_count", return_value=32):
            self.assertIsNone(self.admit())
        # control on the SAME two occupants: put one pane back and the
        # identical box refuses, so this is the cap moving and not the
        # census miscounting.
        self.pane(90004)
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("REFUSED", err)


class AdmissionTest(CapBase):
    def test_the_third_run_is_refused_and_the_runners_are_named(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")
        err = self.admit()
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
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("Retry when one of those named runs finishes", err)
        self.assertIn("No override exists", err)

    def test_the_second_run_is_admitted_and_holds_an_intent(self):  # noqa: VACUOUS_ASSERTION — successful admission is followed by an unconditional positive assertion on its exact persisted intent row
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.launcher(500)
        position = {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
                    "holder": "seat-b"}
        self.assertIsNone(self.admit(position))
        rows = self.admission_rows()
        self.assertEqual([r["position"] for r in rows], ["feedfacefeedface"])
        self.assertIsNone(gate._admission_release(
            "feedfacefeedface", admissions_path=self.admissions))
        self.assertEqual(self.admission_rows(), [])

    def test_a_live_pending_intent_holds_a_slot_before_its_suite_exists(self):
        # One suite RUNNING plus one admission whose suite has not spawned
        # yet: the box is committed to two, and the third must refuse — this
        # is the two-launchers-read-N-together race, closed by the ledger.
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.launcher(500)
        self.intent("feedfacefeedface", 500)
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("suite starting", err)
        self.assertIn("seat-a", err)

    def test_a_dead_launchers_intent_self_clears(self):  # noqa: VACUOUS_ASSERTION — the row is unconditionally asserted present before admission and absent afterward on the same ledger
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.intent("feedfacefeedface", 500)   # pid 500 never planted
        # Positive control: the row IS on disk before admission scrubs it.
        self.assertEqual(
            [r["position"] for r in self.admission_rows()],
            ["feedfacefeedface"])
        self.assertIsNone(self.admit())
        self.assertEqual(self.admission_rows(), [])

    def test_a_bare_queued_owner_covers_its_live_ancestor_intent(self):  # noqa: VACUOUS_ASSERTION — the bare root is unconditionally proven positionless before its exact live launcher lets the third owner enter a cap-three host
        self.paneless()
        topology = {"cores": 6, "reason": "6 host-online CPUs"}
        self.launcher(500)
        self.plant(101, SUITE_ARGV, ppid=500)
        self.intent("feedfacefeedface", 500)
        self.plant(102, SUITE_ARGV)
        self.launcher(600)
        rows = gate.suite_census(self.proc)
        self.assertIsNone(next(row for row in rows
                               if row["pid"] == 101)["position"])

        third = {"id": "0011223344556677", "pid": 600, "starttime": 7,
                 "holder": "seat-third"}
        self.assertIsNone(self.admit(third, topology=topology))
        self.assertEqual(self.capacity["cap"], 3)
        self.assertEqual([row["position"] for row in self.admission_rows()],
                         ["feedfacefeedface", third["id"]])

    def test_a_running_suite_covers_its_own_intent_counting_once(self):  # noqa: VACUOUS_ASSERTION — the census unconditionally proves the suite carries the live intent position before admission succeeds
        self.plant(101, SUITE_ARGV, cgroup="helm-gate-feedfacefeedface",
                   cwd="/lane/one")
        self.launcher(500)
        self.intent("feedfacefeedface", 500)
        # Positive controls: the suite IS counted and carries the intent's
        # own position, so a pass below is coverage, not an empty census.
        rows = gate.suite_census(self.proc)
        self.assertEqual([r["position"] for r in rows],
                         ["feedfacefeedface"])
        self.assertIsNone(self.admit())

    def test_sixteen_owner_tokens_admit_and_the_seventeenth_waits(self):  # noqa: VACUOUS_ASSERTION — exact 16-row ledger and census are unconditional before the refused seventeenth and stale-owner replacement
        self.paneless()
        topology = {"cores": 32, "reason": "32 host-online CPUs"}
        positions = []
        for offset in range(16):
            pid = 500 + offset
            token = "%016x" % (offset + 1)
            self.launcher(pid)
            position = {"id": token, "pid": pid, "starttime": 7,
                        "holder": "seat-%d" % offset}
            self.assertIsNone(self.admit(position, topology=topology))
            positions.append(token)
        self.assertEqual(self.capacity["cap"], 16)
        self.assertEqual([row["position"] for row in self.admission_rows()],
                         positions)
        for offset, token in enumerate(positions):
            self.plant(1000 + offset, SUITE_ARGV,
                       cgroup="helm-gate-" + token, ppid=500 + offset)
        # One owner's sibling shard root and wrapper stay visible under that
        # token without turning the running-owner census into seventeen rows.
        self.plant(2000, ("python3", "-m", "unittest"),
                   cgroup="helm-gate-" + positions[0], ppid=700)
        self.plant(2001, ("python3", "/x/gatechild.py", "--supervise"),
                   ppid=1000)
        census = gate.suite_census(self.proc)
        self.assertEqual([row["owner"] for row in census], positions)
        self.assertEqual(len(census), 16)
        self.assertIn(2000,
                      {child["pid"] for child in census[0]["children"]})
        self.assertIn(2001,
                      {child["pid"] for child in census[0]["children"]})

        self.launcher(516)
        seventeenth = {"id": "%016x" % 17, "pid": 516, "starttime": 7,
                       "holder": "seat-16"}
        err = self.admit(seventeenth, topology=topology)
        self.assertIn("cap is 16", err or "")
        self.assertIn("paneless build host", err or "")
        self.assertIn("REFUSED at this host's admitted-suite limit", err or "")
        self.assertNotIn("agent pane", err or "")
        self.assertNotIn("daemon", err or "")
        self.assertIn("16 are already running", err or "")
        self.assertNotIn(seventeenth["id"],
                         [row["position"] for row in self.admission_rows()])

        # The ledger is process-owned, not TTL-owned. Once one exact pid/start
        # identity is stale, the same locked pass drops it and admits the waiter.
        for pid in (500, 1000, 2000, 2001):
            shutil.rmtree(os.path.join(self.proc, str(pid)))
        self.assertIsNone(self.admit(seventeenth, topology=topology))
        admitted = [row["position"] for row in self.admission_rows()]
        self.assertEqual(len(admitted), 16)
        self.assertNotIn(positions[0], admitted)
        self.assertIn(seventeenth["id"], admitted)

    def test_a_nested_launcher_inherits_a_full_hosts_owner_grant(self):  # noqa: VACUOUS_ASSERTION — the exact two-owner census and live duplicate intent are unconditional before inheritance empties the ledger
        first = "aabbccdd11223344"
        second = "0011223344556677"
        self.plant(101, SUITE_ARGV, cgroup="helm-gate-" + first)
        self.plant(102, ("python3", "/x/gatechild.py", "--supervise"),
                   ppid=101)
        self.plant(500, ("python3", "-m", "helm", "gate", "run"),
                   ppid=102)
        self.plant(201, SUITE_ARGV, cgroup="helm-gate-" + second)
        rows = gate.suite_census(self.proc)
        self.assertEqual([row["owner"] for row in rows], [first, second])
        self.assertIn(500, {child["pid"] for child in rows[0]["children"]})
        self.assertIn("REFUSED", self.admit() or "")

        # A pre-fix nested gate can leave a live duplicate intent. The first
        # owner-aware pass retires it rather than preserving a phantom slot.
        self.intent("deadbeefdeadbeef", 500)
        self.assertEqual(len(self.admission_rows()), 1)
        # Custom/focused suite-scale children have no FIFO position. Their
        # current launcher pid still resolves to the containing owner.
        with mock.patch.object(gate.os, "getpid", return_value=500):
            self.assertIsNone(self.admit())
        self.assertEqual(self.admission_rows(), [])
        # A queued nested gate carries the same launcher identity explicitly.
        nested = {"id": "deadbeefdeadbeef", "pid": 500, "starttime": 7,
                  "holder": "seat-nested"}
        self.assertIsNone(self.admit(nested))
        self.assertEqual(self.capacity["cap"], 2)
        self.assertEqual(self.admission_rows(), [])

    def test_a_nested_launcher_below_a_pending_owner_writes_no_intent(self):  # noqa: VACUOUS_ASSERTION — the exact outer intent and saturated two-owner state are unconditional before the nested admission leaves that ledger unchanged
        self.launcher(500)
        self.intent("feedfacefeedface", 500)
        self.plant(600, ("python3", "-m", "helm", "gate", "run"),
                   ppid=500)
        self.plant(201, SUITE_ARGV)
        self.assertIn("REFUSED", self.admit() or "")
        self.assertEqual([row["position"] for row in self.admission_rows()],
                         ["feedfacefeedface"])

        nested = {"id": "deadbeefdeadbeef", "pid": 600, "starttime": 7,
                  "holder": "seat-nested"}
        old = self.admission_rows() + [{
            "position": nested["id"], "pid": 600, "starttime": 7,
            "holder": nested["holder"], "ts": "2026-01-01T00:00:00Z",
        }]
        pk.write_json(self.admissions, {"v": 1, "admissions": old})
        self.assertEqual(len(self.admission_rows()), 2)
        self.assertIsNone(self.admit(nested))
        self.assertEqual([row["position"] for row in self.admission_rows()],
                         ["feedfacefeedface"])

    def test_an_unreadable_census_refuses_never_admits(self):
        missing = os.path.join(self.tmp, "no-such-proc")
        # Polarity against the production law: HELM_PROC points at the readable
        # fixture tree while the explicit host-authority seam points elsewhere.
        # A regression that honors the child redirect admits; only proc_dir
        # produces this refusal.
        os.environ["HELM_PROC"] = self.proc
        self.plant(101, SUITE_ARGV)
        self.assertEqual([row["pid"] for row in gate.suite_census()], [101])
        err = self.admit(proc_dir=missing)
        self.assertIsNotNone(err)
        self.assertEqual(self.capacity["cap"], 2)
        self.assertIn("process table is unreadable", self.capacity["reason"])
        self.assertIn("cannot count running suites", err)


class PressureFloorTest(CapBase):
    def test_a_stalling_box_refuses_admission_and_names_the_reading(self):
        self.psi(12.34)
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("12.34%", err)
        self.assertIn("REFUSED", err)

    def test_the_psi_refusal_names_the_repair_path(self):
        """Same law as the cap refusal: the honest next step is re-run once
        the 10s window clears (and WHERE to watch it fall), and the honest
        override note is that none exists — the floor is the kernel's own
        starvation reading, not a policy a variable can waive."""
        self.psi(12.34)
        err = self.admit()
        self.assertIsNotNone(err)
        self.assertIn("Re-run once the pressure clears", err)
        self.assertIn("/proc/pressure/memory", err)
        self.assertIn("No override exists", err)

    def test_a_quiet_box_admits(self):  # noqa: VACUOUS_ASSERTION — the real PSI value and the admitted intent's exact persisted row are both asserted unconditionally
        self.psi(2.00)
        # Positive control: the pressure file was READ, not missing.
        self.assertEqual(gate._psi_some_avg10(self.proc), 2.00)
        self.launcher(500)
        self.assertIsNone(self.admit(
            {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
             "holder": "seat-b"}))
        # The admitted run's EFFECT exists: its intent row is on disk.
        self.assertEqual(
            [r["position"] for r in self.admission_rows()],
            ["feedfacefeedface"])

    def test_a_box_without_psi_keeps_its_gate(self):  # noqa: VACUOUS_ASSERTION — successful admission leaves an exact persisted intent row, an unconditional positive effect independent of absent PSI
        # Positive control pair: no pressure file reads as None, and the
        # admitted run still leaves its positive effect (the intent row).
        self.assertIsNone(gate._psi_some_avg10(self.proc))
        self.launcher(500)
        self.assertIsNone(self.admit(
            {"id": "feedfacefeedface", "pid": 500, "starttime": 7,
             "holder": "seat-b"}))
        self.assertEqual(
            [r["position"] for r in self.admission_rows()],
            ["feedfacefeedface"])


class RunRefusalTest(CapBase):
    """gate.run itself: the refusal reaches the caller in words, releases
    the FIFO position, and leaves no intent behind."""

    def setUp(self):
        super().setUp()
        self.use_fixture_admission_for_run()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(("git", "init", "-q", "-b", "main"), cwd=self.repo,
                       check=True)
        subprocess.run(("git", "config", "user.email", "cap@test"),
                       cwd=self.repo, check=True)
        subprocess.run(("git", "config", "user.name", "cap test"),
                       cwd=self.repo, check=True)
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        helm_tree(self, self.repo)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("one\n")
        subprocess.run(("git", "add", "-A"), cwd=self.repo, check=True)
        subprocess.run(("git", "commit", "-qm", "first"), cwd=self.repo,
                       check=True)

    def saturate(self):
        self.plant(101, SUITE_ARGV, cwd="/lane/one")
        self.plant(102, ("python3", "-m", "unittest", "discover"),
                   cwd="/lane/two")

    def test_a_queued_run_is_refused_released_and_spawns_nothing(self):  # noqa: VACUOUS_ASSERTION — the queue position is asserted present before run and its exact post-refusal absence proves release rather than a blind reader
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
        self.assertEqual(self.admission_rows(), [])

    def test_a_custom_discovery_command_cannot_bypass_the_cap(self):
        self.saturate()
        row, err = gate.run(repo=self.repo,
                            argv=["python3", "-m", "unittest", "discover"])
        self.assertIsNone(row)
        self.assertIn("cap is 2", err)

    def test_a_custom_targeted_command_stays_outside_the_cap(self):  # noqa: VACUOUS_ASSERTION — the saturated census is real, while the admission mock must remain untouched and the child mock must receive an unconstrained inherited environment
        self.saturate()
        admit = mock.Mock(side_effect=AssertionError(
            "targeted custom commands must not enter suite admission"))
        child = mock.Mock(return_value=(
            "", "Ran 1 test in 0.0s\n\nOK", 0, None))
        with mock.patch.object(gate, "_admit_suite", admit), \
                mock.patch.object(gate, "_queued_process", child):
            row, err = gate.run(repo=self.repo, argv=[
                "python3", "-c", "print('diagnostic')"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "OK")
        admit.assert_not_called()
        self.assertNotIn(
            "HELM_GATE_SUITE_CAP", child.call_args.kwargs["env"])


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

    def setUp(self):
        super().setUp()
        self.use_fixture_admission_for_run()

    def _repo(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            subprocess.run(list(cmd), cwd=repo, capture_output=True, timeout=30)
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        helm_tree(self, repo)
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
