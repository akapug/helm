#!/usr/bin/env python3
"""webserve tests — the record each `helm web` leaves about itself, and the
doctor check that counts them.

THE PROPERTY UNDER TEST IS A SET OF DISTINCTIONS, not a level. "nothing is
serving here", "a server died without tidying up" and "the registry could not
be read" are three different answers, and any two of them collapsing is how a
reader concludes the host is quiet from an instrument that could not look. So
each arm pins the finding AND its sentence, and every arm that asserts an
absence carries an unconditional positive control that the instrument spoke.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import doctor, home, pk, webserve


def levels(results, level):
    return [msg for lvl, msg in results if lvl == level]


class WebServeBase(unittest.TestCase):
    #: Whether this class's arms should be blind to the real process table.
    #: True for everything that asserts about the REGISTRY; the arms that test
    #: the /proc sweep itself set it False, because patching the sweep out from
    #: under them would leave them measuring the mock.
    BLIND = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.envp = mock.patch.dict(
            os.environ, {"HELM_HOME": os.path.join(self.tmp.name, "helm-home")})
        self.envp.start()
        self.addCleanup(self.envp.stop)
        self.assertTrue(home.helm_home().startswith(self.tmp.name))
        if self.BLIND:
            self.blind_to_the_real_box()

    def blind_to_the_real_box(self):
        """THE HOST THIS SUITE RUNS ON MAY BE SERVING A CONSOLE, and `live`
        reads the real process table on purpose. An arm that asserts "no
        servers" would then pass or fail on whether the owner happened to have
        his console open, which is a property of the afternoon and not of the
        code. Arms that want the process table say so by patching it."""
        patch = mock.patch.object(webserve, "observed", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def plant(self, port, pid=424242, **fields):
        """A record written straight to disk, so an arm can name a server this
        process is not."""
        rec = {"schema": webserve.SCHEMA, "port": port, "pid": pid,
               "pid_start": "99", "started": 1000.0,
               "cwd": "/srv/trees/wt/some-lane", "root": None}
        rec.update(fields)
        os.makedirs(webserve.dir_path(), exist_ok=True)
        pk.atomic_write(webserve.path(port), json.dumps(rec), mode=0o600)
        return rec


class RegisterForgetTest(WebServeBase):

    def test_round_trip(self):
        self.assertTrue(webserve.register(7999, root="/r"))
        st = webserve.live()
        self.assertEqual(
            [7999], [x["port"] for x in st["servers"]],
            "a server that registered itself is not in the live list")
        self.assertEqual(os.getpid(), st["servers"][0]["pid"])
        self.assertTrue(webserve.forget(7999))
        self.assertEqual(
            [], webserve.live()["servers"],
            "the record survived the forget that reported success")

    def test_forget_of_an_absent_record_is_success(self):
        """The caller's goal is the absence, so already-absent is not a
        failure — a server that unregistered twice must not report a fault."""
        self.assertTrue(webserve.forget(7998))


class LiveTest(WebServeBase):

    def test_a_dead_server_is_stale_not_live(self):
        """A file outlives a SIGKILLed server, so presence is never liveness."""
        self.plant(7001, pid=424242)          # a pid that is not running
        # POSITIVE CONTROL, same observable, unconditional: this process IS
        # reported, so an empty `servers` below is about the planted record and
        # not about a reader that returns nothing whatever it is given.
        self.assertTrue(webserve.register(7002))
        st = webserve.live()
        self.assertEqual(
            [7002], [x["port"] for x in st["servers"]],
            "the dead server was reported as running, or the live one was not")
        self.assertEqual(1, st["stale"], "the dead record was not counted")

    def test_an_unparsable_record_is_counted_never_skipped(self):
        """A registry that cannot read a file must not answer as though the
        server it could not read does not exist."""
        os.makedirs(webserve.dir_path(), exist_ok=True)
        pk.atomic_write(webserve.path(7003), "{ this is not json",
                        mode=0o600)
        self.assertTrue(webserve.register(7004))     # positive control
        st = webserve.live()
        self.assertEqual([7004], [x["port"] for x in st["servers"]])
        self.assertEqual(
            1, st["unreadable"],
            "the unparsable record was silently skipped, which would let a "
            "running server disappear from the count")


class DoctorWebServersTest(WebServeBase):

    def test_no_servers_is_not_a_fault(self):
        out = doctor.check_web_servers()
        self.assertEqual(
            [], levels(out, doctor.FAIL),
            "a host serving no console was reported as a fault")
        # POSITIVE CONTROL: the check spoke at all.
        self.assertTrue(out, "the check returned nothing, so the arm above "
                             "asserts the absence of a FAIL in an empty list")
        self.assertIn("none recorded", out[0][1])

    def test_one_server_is_the_expected_shape(self):
        self.assertTrue(webserve.register(7005))
        out = doctor.check_web_servers()
        self.assertEqual([], levels(out, doctor.FAIL))
        self.assertTrue(out)
        self.assertIn("one running", out[0][1])

    def test_two_servers_is_a_fault_that_names_the_cost(self):
        """The finding the whole module exists for. It must be a FAIL, name
        every port, and say what the extras cost — a WARN about a second
        console is a line nobody acts on."""
        self.assertTrue(webserve.register(7006))
        self.plant(7007, pid=os.getpid(), pid_start=None)
        bad = levels(doctor.check_web_servers(), doctor.FAIL)
        self.assertEqual(1, len(bad), "two live servers did not produce "
                                      "exactly one FAIL: %r" % (bad,))
        self.assertIn("7006", bad[0])
        self.assertIn("7007", bad[0])
        self.assertIn("slows every seat", bad[0])


class ObservedTest(WebServeBase):
    BLIND = False               # these arms ARE the sweep; see WebServeBase
    """The /proc sweep, over a SYNTHETIC process tree.

    This is what makes the check work on the day it lands instead of after
    every server has been restarted — the servers that pegged the box were
    already running and had never registered.
    """

    def proc(self, entries):
        """A fake /proc. `entries` is {pid: (argv_list, cwd)}."""
        root = os.path.join(self.tmp.name, "proc")
        for pid, (argv, cwd) in entries.items():
            d = os.path.join(root, str(pid))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "cmdline"), "wb") as handle:
                handle.write(b"\0".join(a.encode() for a in argv) + b"\0")
            target = os.path.join(self.tmp.name, "cwd-%d" % pid)
            os.makedirs(target, exist_ok=True)
            link = os.path.join(d, "cwd")
            if not os.path.islink(link):
                os.symlink(target, link)
        os.makedirs(os.path.join(root, "not-a-pid"), exist_ok=True)
        return root

    def test_it_finds_a_server_and_its_port(self):
        root = self.proc({4242: (["/usr/bin/python3", "/srv/bin/helm",
                                  "web", "--port", "7433"], "/srv/somewhere")})
        found = webserve.observed(proc_dir=root)
        self.assertEqual([4242], sorted(found),
                         "the sweep did not find a running `helm web`")
        self.assertEqual(7433, found[4242]["port"])

    def test_it_does_not_match_the_words_alone(self):
        """A grep for the phrase, or an editor holding the file, is not a
        server. `pgrep -f` would report every one of these — including the
        shell running the pgrep, whose argv carries the pattern just typed."""
        root = self.proc({
            11: (["grep", "-rn", "helm web", "/srv/trees"], "/srv/somewhere"),
            12: (["bash", "-c", "helm web --port 9999 # in a comment"], "/x"),
            13: (["vim", "helm/web_server.py"], "/srv/somewhere"),
            14: (["/usr/bin/python3", "/srv/bin/helm", "doctor"],
                 "/srv/somewhere"),
        })
        # POSITIVE CONTROL, unconditional and on the same observable: a REAL
        # server in the same tree is found, so the empty result below is about
        # these four argvs and not about a matcher that never matches.
        root2 = self.proc({15: (["/srv/bin/helm", "web"], "/srv/somewhere")})
        self.assertIn(15, webserve.observed(proc_dir=root2),
                      "the matcher found no server at all, so the refusals "
                      "below prove nothing")
        found = webserve.observed(proc_dir=root2)
        self.assertEqual(
            [15], sorted(found),
            "something that merely mentions `helm web` was counted as a "
            "running server: %r" % (found,))
        self.assertIsNone(found[15]["port"], "a server with no --port in argv "
                                             "must report None, not a guess")


class UnregisteredServerTest(WebServeBase):

    def test_a_running_server_that_never_registered_still_counts(self):
        """The shape that made this module necessary. Every server on the box
        that night predated the registry, so a check that trusted the registry
        alone would have reported a quiet host at load 34."""
        patch = mock.patch.object(
            webserve, "observed",
            return_value={9001: {"pid": 9001, "cwd": "/srv/somewhere", "port": 7433}})
        patch.start()
        self.addCleanup(patch.stop)
        st = webserve.live()
        self.assertEqual([7433], [x["port"] for x in st["servers"]],
                         "a running but unregistered server was not counted")
        self.assertEqual(1, st["unregistered"])
        self.assertFalse(st["servers"][0]["registered"])

    def test_the_rung_names_it_unregistered_and_does_not_invent_an_age(self):
        patch = mock.patch.object(
            webserve, "observed",
            return_value={9001: {"pid": 9001, "cwd": "/srv/somewhere", "port": 7433}})
        patch.start()
        self.addCleanup(patch.stop)
        out = doctor.check_web_servers()
        self.assertTrue(out)
        self.assertIn("UNREGISTERED", out[0][1])
        self.assertIn("start time unknown", out[0][1])


class AdHocWebServersTest(WebServeBase):
    """task/3715: `helm doctor` lists every ad hoc `helm web` (any one that is
    not the owner's helm-web unit) with its port, RSS, age and the tree it
    serves. An ad hoc server has no memory bound of its own: one left polling
    from a single open tab reached 4.5 GB in about 54 minutes.

    A SYNTHETIC /proc, so the arms say what the doctor makes of each process
    rather than what happens to be running on the host."""
    BLIND = False               # these arms feed the sweep; see WebServeBase

    UNIT_CGROUP = ("/user.slice/user-1000.slice/user@1000.service/app.slice/"
                   "helm-web.service")
    SEAT_CGROUP = "/agents.slice/agents-seat.slice/run-u7.scope"
    UPTIME_S = 10000.0

    def proc(self, entries):
        """A fake /proc: `entries` is {pid: {argv, cgroup, rss_kb, swap_kb,
        age_s}}, plus the uptime every age is measured against."""
        root = os.path.join(self.tmp.name, "proc")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "uptime"), "w") as fh:
            fh.write("%.2f 0.00\n" % self.UPTIME_S)
        hz = os.sysconf("SC_CLK_TCK")
        for pid, e in entries.items():
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "cmdline"), "wb") as fh:
                fh.write(b"\0".join(a.encode() for a in e["argv"]) + b"\0")
            cwd = os.path.join(self.tmp.name, "cwd-%d" % pid)
            os.makedirs(cwd)
            os.symlink(cwd, os.path.join(d, "cwd"))
            with open(os.path.join(d, "status"), "w") as fh:
                fh.write("Name:\tpython3\nVmRSS:\t%d kB\nVmSwap:\t%d kB\n"
                         % (e["rss_kb"], e.get("swap_kb", 0)))
            with open(os.path.join(d, "cgroup"), "w") as fh:
                fh.write("0::%s\n" % e["cgroup"])
            start = int((self.UPTIME_S - e["age_s"]) * hz)
            with open(os.path.join(d, "stat"), "w") as fh:
                fh.write("%d (python3) S %s %d 0 0\n"
                         % (pid, " ".join(["1"] * 18), start))
        return root

    def tree(self, name):
        """A checkout the way a lane worktree looks: <tree>/bin/helm beside
        its <tree>/helm package."""
        t = os.path.join(self.tmp.name, "trees", name)
        for sub in ("bin", "helm"):
            os.makedirs(os.path.join(t, sub))
        for rel in (("bin", "helm"), ("helm", "__init__.py")):
            with open(os.path.join(t, *rel), "w") as fh:
                fh.write("\n")
        return os.path.realpath(t)

    def console(self):
        return {5001: {"argv": ["/usr/bin/python3",
                                os.path.join(self.tree("main"), "bin", "helm"),
                                "web", "--port", "7433"],
                       "cgroup": self.UNIT_CGROUP, "rss_kb": 1200 * 1024,
                       "swap_kb": 300 * 1024, "age_s": 1800}}

    def adhoc_line(self, out):
        lines = [(lvl, msg) for lvl, msg in out
                 if msg.startswith("ad hoc web servers:")]
        self.assertEqual(len(lines), 1, "no single ad hoc line in %r" % (out,))
        return lines[0]

    def test_no_ad_hoc_server_says_none(self):
        out = doctor.check_web_servers(proc_dir=self.proc(self.console()))
        # POSITIVE CONTROL: the sweep saw the console, so "none" below is a
        # reading about ad hoc servers and not a sweep that saw nothing.
        self.assertIn("port 7433", out[0][1])
        lvl, msg = self.adhoc_line(out)
        self.assertEqual(lvl, doctor.OK)
        self.assertIn("ad hoc web servers: none", msg)
        self.assertEqual([], levels(out, doctor.FAIL) + levels(out, doctor.WARN))

    def test_an_empty_host_says_none_too(self):
        out = doctor.check_web_servers(proc_dir=self.proc({}))
        self.assertIn("none recorded", out[0][1])
        self.assertIn("ad hoc web servers: none", self.adhoc_line(out)[1])

    def test_one_ad_hoc_server_names_its_port_rss_age_and_tree(self):
        lane = self.tree("wt-lane")
        entries = self.console()
        entries[5002] = {"argv": ["/usr/bin/python3",
                                  os.path.join(lane, "bin", "helm"), "web",
                                  "--port", "7488"],
                         "cgroup": self.SEAT_CGROUP,
                         "rss_kb": int(4.5 * 1024 * 1024), "age_s": 54 * 60}
        out = doctor.check_web_servers(proc_dir=self.proc(entries))
        lvl, msg = self.adhoc_line(out)
        self.assertEqual(lvl, doctor.WARN)
        self.assertIn("port 7488", msg)   # the control for the absence below
        for fact in ("pid 5002", "RSS 4.5 GiB", "up 54m", "tree " + lane):
            self.assertIn(fact, msg)
        self.assertNotIn("7433", msg, "the owner's unit was listed as ad hoc")


class RegisteredTreeTest(WebServeBase):

    def test_a_registered_server_records_the_tree_it_serves(self):
        self.assertTrue(webserve.register(7011))
        mine = [x for x in webserve.live()["servers"] if x["port"] == 7011]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].get("tree"), os.path.dirname(
            os.path.dirname(os.path.abspath(webserve.__file__))))
        self.assertIsInstance(mine[0].get("rss_bytes"), int)
