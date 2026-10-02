#!/usr/bin/env python3
"""helm chat node — consensus-posture mirroring and honest start failures.

WHY THIS EXISTS: the chat node had never once started on the dev machine. Not a
regression, not a flake — the generated unit could not possibly work, and the two
bugs that hid it were both in helm.

dregg refuses to boot when the verified Lean executor archive is absent: it will
not "serve as if verified while running the un-verified executor". The escape
hatch belongs to the operator, and on this machine the operator had already
opened it — for the TEAM cave node, in a systemd drop-in, which is the standard
place to state machine-local posture without editing a shipped unit. helm's unit
was the same binary on the same machine and carried no Environment= lines, so it
restart-looped forever.

And helm reported that as "unit started but the API never answered". A timeout is
true of every possible cause, so it pointed nowhere; meanwhile the service had
already printed the exact remedy. Relaying a subordinate's precise diagnosis is
not optional for a provisioner that owns it.

The invariant these tests pin, in both directions: helm MIRRORS the posture the
operator's team node RUNS WITH and NEVER mints one. Each flag that loosens
dregg's verification is written on every `up`: the value the running peer was
started with, or EMPTY, which dregg reads as refusal, when the peer runs
without it or its environment cannot be proven. Writing nothing is not
refusal: our unit inherits the user manager's environment, so a bypass the
manager exports reached our node (task/3432 round 2, helm-codex's F1).
"""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chatnode  # noqa: E402
# How systemd 259 reads an Environment= line (an oracle pinned to measured
# lines, independent of the module under test).
from tests.test_timerhealth import _environment_set  # noqa: E402


# THE POSTURE FLAGS dregg's own gates read (emberian/dregg c93404d81): the
# boot refusal's escape hatch (node/src/lib.rs:2218), the unaudited-PQ bypass
# (dregg-pq/src/audit.rs:98), and the switch that revokes it (:104, applied
# at :166-171).
UNVERIFIED = "DREGG_ALLOW_UNVERIFIED_CONSENSUS"
UNAUDITED = "DREGG_ALLOW_UNAUDITED_PQ"
REQUIRE_LEAN = "DREGG_REQUIRE_LEAN"
# DREGG_ NAMES dregg defines that are no posture: tokens, a bearer, keys, a
# seed, a DSN, and a trusted-proxy list (emberian/dregg a31590c37; meta-claude
# listed them on 907400b780f). Each one mirrored while the filter was the
# prefix.
DREGG_SECRETS = ("DREGG_ADMIN_TOKEN", "DREGG_API_TOKEN", "DREGG_NODE_BEARER",
                 "DREGG_LLM_API_KEY", "DREGG_PAY_SEED", "DREGG_DEVNET_KEY",
                 "DREGG_ROOT_KEY", "DREGG_PG_DSN", "DREGG_TRUSTED_PROXIES")
# A VALUE ONLY THE PEER'S ENVIRONMENT HOLDS: no output, reason or file may
# carry it.
SECRET = "fixture-secret-value"


# HOW dregg READS EACH FLAG: an oracle independent of the module under test,
# written from emberian/dregg c93404d81. The escape hatch is on for exactly
# 1, true, TRUE, on or ON (node/src/lib.rs:4048; node/src/blocklace_sync.rs
# :3895 reads it the same way). The unaudited-PQ bypass is on for exactly "1"
# (dregg-pq/src/audit.rs:111), unless DREGG_REQUIRE_LEAN, trimmed, is 1,
# true, on or yes (audit.rs:123-128, blocklace_sync.rs:3940), which revokes
# it (audit.rs:166-171). So unset and EMPTY are refusal for both bypasses.
def _dregg_runs_unverified(env):
    return env.get(UNVERIFIED) in ("1", "true", "TRUE", "on", "ON")


def _dregg_requires_lean(env):
    return env.get(REQUIRE_LEAN, "").strip() in ("1", "true", "on", "yes")


def _dregg_runs_unaudited_pq(env):
    return env.get(UNAUDITED) == "1" and not _dregg_requires_lean(env)


def _effective(dropin, manager=()):
    """The posture environment our unit's node starts with: the user
    manager's own `manager` first, which a user unit inherits, then each
    Environment= line of our posture drop-in's text `dropin` (None when
    there is none), read as systemd 259 reads it (systemd.exec(5): what
    Environment= sets overrides what the manager passes)."""
    env = dict(manager)
    env.update(_environment_set(dropin or ""))
    return env


# THE PEER AS THE KERNEL SHOWS IT. Its main process in a /proc of the arm's
# own: /proc/<pid>/environ holds each entry and a NUL after it, and
# /proc/<pid>/cgroup, on cgroup v2, one "0::" line naming the process's
# control group, which for a user unit runs through the user's manager,
# user@<uid>.service, its slice and the unit.
PEER_PID = 4242
PEER_ASKED = ["show", chatnode.PEER_UNIT, "-p", "LoadState", "-p", "MainPID"]


def _environ(entries):
    """/proc/<pid>/environ's bytes: each entry (str or bytes), NUL-ended."""
    return b"".join((e.encode("utf-8") if isinstance(e, str) else e) + b"\0"
                    for e in entries)


def _cgroup(*path, uid=None):
    """/proc/<pid>/cgroup of a process of this user's manager: its one
    cgroup v2 line, through user@<uid>.service and then `path` (by default
    app.slice and the peer's unit, where a user unit's process lives)."""
    uid = os.getuid() if uid is None else uid
    return "0::/user.slice/user-%d.slice/user@%d.service/%s\n" % (
        uid, uid, "/".join(path or ("app.slice", chatnode.PEER_UNIT)))


class PostureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-posture-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # HOME COMES BACK IN A CLEANUP, NOT tearDown: cleanups run after
        # tearDown, the last registered first, so an arm's own later patch of
        # the environment (fake_user_systemd) is undone before this one.
        home = mock.patch.dict(os.environ, {"HOME": self.tmp})
        home.start()
        self.addCleanup(home.stop)
        self.units = os.path.join(self.tmp, ".config", "systemd", "user")
        os.makedirs(self.units)
        # THE PEER UNIT AS systemctl DESCRIBES IT: one line per property, and
        # only the properties a reader asks for (systemctl prints no other,
        # measured). The mirror asks for two; the others say what the unit
        # declares, which the mirror no longer reads. Every arm answers here,
        # through the runner the reader shares with the timer census, and
        # never reaches the host's systemctl. `answer` replaces the whole
        # answer. By default there is no such unit.
        from helm import timerhealth
        self.props = {"LoadState": "not-found", "MainPID": "0",
                      "NeedDaemonReload": "no", "Environment": "",
                      "UnsetEnvironment": "", "PAMName": ""}
        self.answer, self.asked = None, []
        self.peer_patch = mock.patch.object(timerhealth, "_systemctl",
                                            self.peer_systemctl)
        self.peer_patch.start()
        self.addCleanup(self.peer_patch.stop)
        # THE KERNEL THE READER ASKS is this /proc, which holds only what an
        # arm plants (peer_runs): no arm reads a real process. create=True
        # lets an arm run on a module that reads no /proc at all; one that
        # read the host's would find no process of the peer's unit at
        # PEER_PID, and every arm expecting a proven posture fails there.
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        proc = mock.patch.object(chatnode, "PROC", self.proc, create=True)
        proc.start()
        self.addCleanup(proc.stop)

    def peer_systemctl(self, argv, timeout=10):
        self.asked.append(list(argv))
        if self.answer is not None:
            return self.answer
        asked = {b for a, b in zip(argv, argv[1:]) if a == "-p"}
        return 0, "".join("%s=%s\n" % (k, v) for k, v in self.props.items()
                          if k in asked)

    def peer_runs(self, env=(), raw=None, cgroup=None, pid=PEER_PID, **unit):
        """The peer unit loaded, its main process `pid` started with `env`
        (NAME=value entries; `raw` is the environ's bytes instead), in the
        peer's control group unless `cgroup` is another's. `unit` sets what
        systemctl says the unit declares (Environment=...). Returns the
        process's /proc directory."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(_environ(env) if raw is None else raw)
        with open(os.path.join(d, "cgroup"), "w", encoding="utf-8") as f:
            f.write(_cgroup() if cgroup is None else cgroup)
        self.props.update(LoadState="loaded", MainPID=str(pid), **unit)
        return d

    def node_bin(self, name="dregg-node"):
        """A node binary `up` can hash and record; the unit never runs it.
        `up` writes that record into the chat-node state, so only an arm
        whose HELM_HOME is its own (BootBase) may drive `up`."""
        b = os.path.join(self.tmp, name)
        with open(b, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(b, 0o755)
        return mock.patch.object(chatnode, "bin_resolution", return_value={
            "path": b, "source": "env", "reason": None})

    def grants(self):
        """(the posture assignments proven, sorted; why none)."""
        posture, why = chatnode.running_posture()
        return sorted(a for _s, a in posture), why

    def mirror(self):
        """The text `up` writes into our unit's posture drop-in."""
        p = chatnode.write_posture_dropin(chatnode.running_posture()[0])
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def assert_neutralised(self, text):
        """Our drop-in's `text` sets both loosening flags EMPTY and leaves
        DREGG_REQUIRE_LEAN alone: a bypass the user manager exports never
        reaches our node, and a tightening it exports does."""
        for manager in ({UNVERIFIED: "1", UNAUDITED: "1"},
                        {UNVERIFIED: "1", UNAUDITED: "1", REQUIRE_LEAN: "1"}):
            env = _effective(text, manager)
            self.assertEqual((env[UNVERIFIED], env[UNAUDITED]), ("", ""),
                             "a loosening flag is not set empty: %r" % text)
            self.assertFalse(_dregg_runs_unverified(env))
            self.assertFalse(_dregg_runs_unaudited_pq(env))
            self.assertEqual(env.get(REQUIRE_LEAN), manager.get(REQUIRE_LEAN),
                             "DREGG_REQUIRE_LEAN was written: %r" % text)


class RunningPostureTest(PostureBase):
    """THE MIRROR READS THE RUNNING PEER (task/3432 round 2).

    Every earlier reader rebuilt the peer's environment from what systemd
    reports about its unit: its Environment= (task/3423), then its
    EnvironmentFile= and PAMName= (round 1). Each rebuild missed a source
    systemd applies when it starts the unit, and helm-codex's gap check found
    more (F2-F4: a newline in an env file's path, a file that changes after
    the read, an ExecStart= wrapper). The one account of what the peer runs
    with is its main process's own environment. So the mirror asks systemd
    for the unit's LoadState and MainPID, shows that the process is the
    peer's, reads /proc/<pid>/environ and keeps the posture flags alone; and
    when it cannot, it proves nothing and says why in fixed words."""

    def test_the_flags_the_peer_runs_with_are_mirrored_exactly(self):
        self.peer_runs([UNVERIFIED + "=1", "PATH=/usr/bin",
                        "DREGG_ADMIN_TOKEN=" + SECRET, UNAUDITED + "=1"])
        posture, why = chatnode.running_posture()
        self.assertIsNone(why)
        self.assertEqual(sorted(a for _s, a in posture),
                         [UNAUDITED + "=1", UNVERIFIED + "=1"])
        self.assertEqual({s for s, _a in posture},
                         {chatnode.POSTURE_SOURCE % chatnode.PEER_UNIT})
        self.assertEqual(self.asked, [PEER_ASKED],
                         "one show, in the scope of timerhealth's runner")
        env = _effective(self.mirror())
        self.assertTrue(_dregg_runs_unverified(env))
        self.assertTrue(_dregg_runs_unaudited_pq(env))

    def test_it_asks_the_user_manager_the_peer_runs_under(self):
        """dregg-cave.service is a USER unit: the mirror asks `systemctl
        --user`, through timerhealth's runner, and an answer it cannot parse
        (this fake prints nothing) proves nothing."""
        from tests._tmphome import fake_user_systemd
        self.peer_patch.stop()
        fake = fake_user_systemd(self)
        self.assertEqual(chatnode.running_posture(),
                         ([], chatnode.POSTURE_WHY["unparsed"]))
        self.assertEqual(fake.calls(), [["--user"] + PEER_ASKED])

    def test_the_posture_names_are_the_flags_dreggs_gates_read(self):
        """ONE SET, and each name is one dregg's own startup gates read: the
        boot refusal's escape hatch, the unaudited-PQ bypass and the switch
        that revokes that bypass. The first two LOOSEN, and are the two the
        drop-in writes on every `up`. No token, key, seed, DSN or proxy list
        is among them."""
        self.assertEqual(chatnode.POSTURE_NAMES,
                         frozenset((UNVERIFIED, UNAUDITED, REQUIRE_LEAN)))
        self.assertEqual(chatnode.LOOSENING, (UNVERIFIED, UNAUDITED))
        self.assertEqual(chatnode.POSTURE_NAMES & {UNAUDITED, *DREGG_SECRETS},
                         {UNAUDITED}, "a flag in, every secret out")

    def test_no_dregg_secret_is_ever_mirrored(self):  # noqa: VACUOUS_ASSERTION — the loop is over the non-empty literal DREGG_SECRETS; every row first asserts the posture flag beside the secret mirrored (its positive control on the same environment), then the secret's absence
        """Each DREGG_ name dregg defines for a token, a bearer, a key, a
        seed, a DSN or a trusted-proxy list, beside a posture flag and alone:
        the flag mirrors, the secret never does, and a secret alone proves
        a peer running with no posture flag."""
        self.assertTrue(DREGG_SECRETS)
        for name in DREGG_SECRETS:
            with self.subTest(name):
                self.peer_runs([name + "=" + SECRET, UNAUDITED + "=1"])
                self.assertEqual(self.grants(), ([UNAUDITED + "=1"], None))
                body = self.mirror()
                self.assertIn("\nEnvironment=%s=1\n" % UNAUDITED, body)
                self.assertNotIn(name, body)
                self.assertNotIn(SECRET, body)
                self.peer_runs([name + "=" + SECRET])
                self.assertEqual(self.grants(), ([], None))
                body = self.mirror()
                self.assert_neutralised(body)
                self.assertNotIn(name, body)
                self.assertNotIn(SECRET, body)

    def test_a_revoked_pq_bypass_is_mirrored_revoked(self):
        """DREGG_REQUIRE_LEAN=1 revokes DREGG_ALLOW_UNAUDITED_PQ=1
        (dregg-pq/src/audit.rs:166-171). A mirror that carried the bypass
        without its revocation would hand our node the opt-out the operator
        withdrew from theirs."""
        self.peer_runs([UNAUDITED + "=1", REQUIRE_LEAN + "=1"])
        self.assertEqual(self.grants(),
                         ([UNAUDITED + "=1", REQUIRE_LEAN + "=1"], None))
        env = _effective(self.mirror())
        self.assertEqual(env[REQUIRE_LEAN], "1")
        self.assertFalse(_dregg_runs_unaudited_pq(env))

    def test_every_value_is_mirrored_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal table; every row asserts the exact grant and the exact line written for it
        """The mirror writes each value through env_assignment: quoted when
        systemd would split or drop it, "%" doubled so our unit expands no
        specifier, and a multi-byte character (NBSP, which Python's strip
        would lose) kept. An empty value is the refusal it is."""
        for value, line in (
                ("one two", '"%s=one two"'),
                ("a\"b\\c`d$e'f", '"%s=a\\"b\\\\c`d$e\'f"'),
                ("café ", "%s=café "),
                ("100%", '"%s=100%%%%"'),
                ("", "%s=")):
            with self.subTest(value):
                self.peer_runs([UNAUDITED + "=" + value])
                self.assertEqual(self.grants(),
                                 ([UNAUDITED + "=" + value], None))
                self.assertIn("\nEnvironment=%s\n" % (line % UNAUDITED),
                              self.mirror())

    def test_a_posture_value_no_line_can_carry_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple; every row asserts the exact empty posture, its exact fixed reason and the neutralised drop-in, and the secret row after it is its control
        """A posture value holding a control character, or bytes that are
        not UTF-8, is one no Environment= line helm writes carries exactly:
        nothing is proven, since a partial grant is a guess at what the rest
        meant. A secret's value is not mirrored, so it blocks nothing."""
        for label, entry in (("a tab", UNAUDITED + "=1\t"),
                             ("a control character", UNAUDITED + "=1\x01"),
                             ("bytes that are not UTF-8",
                              UNAUDITED.encode("ascii") + b"=1\xff")):
            with self.subTest(label):
                self.peer_runs([UNVERIFIED + "=1", entry])
                self.assertEqual(chatnode.running_posture(),
                                 ([], chatnode.POSTURE_WHY["unwritable"]))
                self.assert_neutralised(self.mirror())
        self.peer_runs(["DREGG_ADMIN_TOKEN=a\tb", b"DISCORD_TOKEN=\xff",
                        UNAUDITED + "=1"])
        self.assertEqual(self.grants(), ([UNAUDITED + "=1"], None))

    def test_an_entry_ends_at_a_nul_alone(self):
        """The kernel ends each entry with a NUL, and getenv(3) matches a
        name only from an entry's start up to its first "=". So a flag's name
        after a newline or a U+2028 inside another entry's value, one without
        "=", one with a blank before or after its name, and a longer name
        ending in a flag's are no flag."""
        self.peer_runs(["OTHER=x\n%s=1" % UNVERIFIED,
                        "OTHER2=x %s=1" % UNVERIFIED, UNVERIFIED,
                        " %s=1" % UNVERIFIED, "%s =1" % UNVERIFIED,
                        "X_%s=1" % UNVERIFIED, UNAUDITED + "=1"])
        self.assertEqual(self.grants(), ([UNAUDITED + "=1"], None))

    def test_a_flag_set_twice_proves_nothing(self):
        """An environment can hold one name twice, and which one a reader
        takes is its own semantics: nothing is proven."""
        self.peer_runs([UNAUDITED + "=1", UNVERIFIED + "=1"])
        self.assertEqual(self.grants(),
                         ([UNAUDITED + "=1", UNVERIFIED + "=1"], None),
                         "the control: each once proves both")
        self.peer_runs([UNAUDITED + "=", UNVERIFIED + "=1", UNAUDITED + "=1"])
        posture, why = chatnode.running_posture()
        self.assertEqual(why, chatnode.POSTURE_WHY["twice"])
        self.assertEqual(posture, [])
        self.assert_neutralised(self.mirror())

    def test_a_peer_systemd_cannot_describe_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal table after its control (the same running peer, answered as systemctl answers, proves both flags); every row asserts the exact empty posture, its exact fixed reason and the neutralised drop-in
        """FAIL CLOSED: an answer the reader cannot trust proves nothing, and
        the reason is one of the fixed sentences, never systemctl's text. The
        peer runs with both flags throughout."""
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
        self.assertEqual(self.grants(),
                         ([UNAUDITED + "=1", UNVERIFIED + "=1"], None),
                         "the control: systemctl's own answer proves both")
        good = "LoadState=loaded\nMainPID=%d\n" % PEER_PID
        pid = "MainPID=%d" % PEER_PID
        cases = (
            ("no systemctl", (None, ""), "unrun"),
            ("show failed", (1, good), "refused"),
            ("no MainPID line", (0, "LoadState=loaded\n"), "unparsed"),
            ("MainPID twice", (0, good + pid + "\n"), "unparsed"),
            ("a line of another property", (0, good + "Id=x.service\n"),
             "unparsed"),
            ("a line without an equals sign", (0, good + "MainPID\n"),
             "unparsed"),
            ("no final newline", (0, good[:-1]), "unparsed"),
            ("nothing", (0, ""), "unparsed"),
            ("a negative pid", (0, good.replace(pid, "MainPID=-1")),
             "unparsed"),
            ("a pid with a suffix", (0, good.replace(pid, pid + "x")),
             "unparsed"),
            ("an empty pid", (0, good.replace(pid, "MainPID=")), "unparsed"),
            ("digits that are not ASCII",
             (0, good.replace(pid, "MainPID=٤٢")), "unparsed"),
            ("no such unit", (0, "LoadState=not-found\nMainPID=0\n"),
             "absent"),
            ("masked", (0, good.replace("loaded", "masked")), "unloaded"),
        )
        for label, answer, why in cases:
            with self.subTest(label):
                self.answer = answer
                self.assertEqual(chatnode.running_posture(),
                                 ([], chatnode.POSTURE_WHY[why]))
                self.assert_neutralised(self.mirror())

    def test_a_stopped_peer_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the control asserts the exact grants of the same running peer; the loop is over a non-empty literal tuple, and every row asserts the exact empty posture, its fixed reason and the neutralised drop-in
        """A peer with no main process (MainPID=0, what systemd reports for
        a unit that is not running) runs with nothing to read, and a main
        process that has exited since systemd named it is gone."""
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
        self.assertEqual(self.grants(),
                         ([UNAUDITED + "=1", UNVERIFIED + "=1"], None),
                         "the control")
        for label, pid in (("MainPID=0", "0"),
                           ("its main process has exited",
                            str(PEER_PID + 1))):
            with self.subTest(label):
                self.props["MainPID"] = pid
                self.assertEqual(chatnode.running_posture(),
                                 ([], chatnode.POSTURE_WHY["stopped"]))
                self.assert_neutralised(self.mirror())

    def test_a_process_not_shown_to_be_the_peers_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the controls first assert a proven posture for the peer's own control group, a sub-cgroup of it and a nested slice; every row of the non-empty literal table then asserts the exact empty posture and fixed reason, no secret in it, and the neutralised drop-in
        """The pid systemd names is read only when the kernel shows it is
        the peer's: a process of this user (its /proc entry's owner) in the
        control group of the peer's unit under this user's manager, or a
        sub-cgroup of it. Anything else, a process of another unit, the
        system manager's unit of the same name, another user's, a v1-only
        or doubled cgroup record, or a deleted control group, is not shown
        to be the peer."""
        uid = os.getuid()
        for label, cgroup in (
                ("its own control group", _cgroup()),
                ("a sub-cgroup of its own",
                 _cgroup("app.slice", chatnode.PEER_UNIT, "payload")),
                ("a nested slice",
                 _cgroup("app.slice", "app-dregg.slice", chatnode.PEER_UNIT))):
            with self.subTest("control: " + label):
                self.peer_runs([UNVERIFIED + "=1"], cgroup=cgroup)
                self.assertEqual(self.grants(), ([UNVERIFIED + "=1"], None))
        env = [UNVERIFIED + "=1", UNAUDITED + "=1", "DREGG_ADMIN_TOKEN=" + SECRET]
        cases = (
            ("another unit", _cgroup("app.slice", "other.service")),
            ("the system manager's unit of that name",
             "0::/system.slice/%s\n" % chatnode.PEER_UNIT),
            ("another user's manager", _cgroup(uid=uid + 1)),
            ("a sub-cgroup of another unit, named like the peer",
             _cgroup("app.slice", "other.service", chatnode.PEER_UNIT)),
            ("a cgroup v1 record alone",
             "1:name=systemd:" + _cgroup()[len("0::"):]),
            ("two unified records", _cgroup() + _cgroup()),
            ("a deleted control group",
             _cgroup().replace("\n", " (deleted)\n")),
            ("no record", ""),
        )
        self.assertTrue(cases)
        for label, cgroup in cases:
            with self.subTest(label):
                self.peer_runs(env, cgroup=cgroup)
                posture, why = chatnode.running_posture()
                self.assertEqual((posture, why),
                                 ([], chatnode.POSTURE_WHY["stranger"]))
                self.assertNotIn(SECRET, why)
                self.assert_neutralised(self.mirror())
        with self.subTest("no cgroup file"):
            d = self.peer_runs(env)
            os.remove(os.path.join(d, "cgroup"))
            self.assertEqual(chatnode.running_posture(),
                             ([], chatnode.POSTURE_WHY["stranger"]))
        with self.subTest("another user's process"):
            self.peer_runs(env, cgroup=_cgroup(uid=uid + 1))
            with mock.patch.object(chatnode.os, "getuid",
                                   return_value=uid + 1):
                self.assertEqual(chatnode.running_posture(),
                                 ([], chatnode.POSTURE_WHY["stranger"]))

    def test_an_environment_that_cannot_be_read_proves_nothing(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty table; every row first asserts its own control (the same process, readable, proves the flag), then the exact empty posture, the fixed reason and the neutralised drop-in
        """What the peer runs with is unknown when its environ cannot be
        read: missing, not a file, one this user may not read, or empty, as
        the kernel shows a process that is exiting."""
        def empty(p):
            open(p, "wb").close()

        def directory(p):
            os.remove(p)
            os.mkdir(p)
        cases = [("empty", empty), ("missing", os.remove),
                 ("a directory", directory)]
        if os.geteuid():
            cases.append(("not permitted", lambda p: os.chmod(p, 0)))
        self.assertTrue(cases)
        for i, (label, spoil) in enumerate(cases):
            with self.subTest(label):
                d = self.peer_runs([UNVERIFIED + "=1"], pid=PEER_PID + 10 + i)
                self.assertEqual(self.grants(), ([UNVERIFIED + "=1"], None),
                                 "the control")
                spoil(os.path.join(d, "environ"))
                self.assertEqual(chatnode.running_posture(),
                                 ([], chatnode.POSTURE_WHY["unreadable"]))
                self.assert_neutralised(self.mirror())


class MirrorTest(PostureBase):
    """THE DROP-IN IS WRITTEN ON EVERY `up` AND NEVER REMOVED (task/3432
    round 2). Our unit inherits the user manager's environment, so no
    drop-in, or one without a loosening flag, lets a bypass the manager
    exports through. Each loosening flag is therefore always written: the
    value the running peer has, else empty."""

    def read(self, p):
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def test_nothing_proven_writes_both_loosening_flags_empty(self):
        """THE LOAD-BEARING ONE. helm must never be the layer that opts a
        node out of verification, and writing nothing does not keep it out:
        an empty value is dregg's refusal, and it overrides what the manager
        exports."""
        p = chatnode.write_posture_dropin([])
        body = self.read(p)
        self.assertEqual([ln for ln in body.splitlines()
                          if not ln.startswith("#")],
                         ["[Service]", "Environment=%s=" % UNVERIFIED,
                          "Environment=%s=" % UNAUDITED])
        self.assert_neutralised(body)
        self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")
        self.assertEqual(os.listdir(os.path.dirname(p)),
                         ["10-helm-posture.conf"],
                         "no temporary file is left beside it")

    def test_F1_a_manager_exported_bypass_never_reaches_our_node(self):
        """helm-codex's F1 on 3b7a8f92ad9: a peer that runs without a
        bypass wrote no drop-in, and our node took the manager's
        DREGG_ALLOW_UNAUDITED_PQ=1. Nor does a flag the peer runs with carry
        the other one in."""
        self.peer_runs(["PATH=/usr/bin", "DREGG_ADMIN_TOKEN=" + SECRET])
        body = self.mirror()
        self.assertIn("\nEnvironment=%s=\n" % UNAUDITED, body)
        self.assert_neutralised(body)
        self.peer_runs([UNVERIFIED + "=1"])
        env = _effective(self.mirror(), {UNAUDITED: "1"})
        self.assertTrue(_dregg_runs_unverified(env))
        self.assertEqual(env[UNAUDITED], "")
        self.assertFalse(_dregg_runs_unaudited_pq(env))

    def test_a_tightening_is_written_when_it_runs_and_never_blanked(self):
        """DREGG_REQUIRE_LEAN only tightens. The peer running with it is
        mirrored; the peer running without it writes no line, so one the
        manager exports still reaches our node and still revokes the PQ
        bypass."""
        self.peer_runs([UNAUDITED + "=1", REQUIRE_LEAN + "=1"])
        env = _effective(self.mirror())
        self.assertEqual(env[REQUIRE_LEAN], "1")
        self.assertFalse(_dregg_runs_unaudited_pq(env))
        self.peer_runs([UNAUDITED + "=1"])
        env = _effective(self.mirror(), {REQUIRE_LEAN: "1"})
        self.assertEqual(env[REQUIRE_LEAN], "1", "a tightening was blanked")
        self.assertFalse(_dregg_runs_unaudited_pq(env))

    def test_mirrors_the_flags_and_names_its_source(self):
        self.peer_runs([UNVERIFIED + "=1"])
        p = chatnode.write_posture_dropin(chatnode.running_posture()[0])
        body = self.read(p)
        self.assertIn("\nEnvironment=%s=1\n" % UNVERIFIED, body)
        self.assertIn("[Service]", body)
        self.assertIn(chatnode.POSTURE_SOURCE % chatnode.PEER_UNIT, body,
                      "a mirrored opt-out must cite where it came from")
        self.assertIn("MIRRORED, not decided here", body)
        self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")

    def test_rewriting_replaces_rather_than_appends(self):
        self.peer_runs([UNVERIFIED + "=1"])
        self.mirror()
        self.peer_runs([UNAUDITED + "=2"])
        body = self.mirror()
        self.assertIn("\nEnvironment=%s=2\n" % UNAUDITED, body)
        self.assertIn("\nEnvironment=%s=\n" % UNVERIFIED, body,
                      "a withdrawn value must not survive in the mirror")
        self.assertNotIn("%s=1" % UNVERIFIED, body)

    def test_the_writer_carries_only_posture_names_whoever_calls_it(self):
        """BY CONSTRUCTION, not by the reader alone: the writer is handed a
        list, and a DREGG_ name in it that is no posture flag is never
        written, whoever built the list. A list holding none writes the
        loosening flags empty."""
        src = "a caller's list"
        alone = self.read(chatnode.write_posture_dropin(
            [(src, "DREGG_ADMIN_TOKEN=" + SECRET)]))
        self.assertIn("\nEnvironment=%s=\n" % UNAUDITED, alone)
        self.assertNotIn(SECRET, alone)
        self.assert_neutralised(alone)
        body = self.read(chatnode.write_posture_dropin(
            [(src, "DREGG_ADMIN_TOKEN=" + SECRET), (src, UNAUDITED + "=1"),
             (src, "DREGG_TRUSTED_PROXIES=0.0.0.0/0")]))
        self.assertIn("\nEnvironment=%s=1\n" % UNAUDITED, body)
        for leak in ("DREGG_ADMIN_TOKEN", SECRET, "DREGG_TRUSTED_PROXIES",
                     "0.0.0.0"):
            with self.subTest(leak):
                self.assertNotIn(leak, body)

    def test_a_doubled_or_unwritable_list_writes_the_flags_empty(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple; every row asserts the neutralised drop-in
        """A list naming one flag twice, or holding a value no line carries
        exactly, is a guess at what the peer meant: the loosening flags are
        written empty and no tightening is written from it."""
        src = "a caller's list"
        for posture in ([(src, UNAUDITED + "=1"), (src, UNAUDITED + "=0")],
                        [(src, UNAUDITED + "=1\t")],
                        [(src, UNVERIFIED + "=1"),
                         (src, REQUIRE_LEAN + "=1\x01")]):
            with self.subTest(posture):
                self.assert_neutralised(self.read(
                    chatnode.write_posture_dropin(posture)))

    def test_a_withdrawn_or_unproven_grant_is_written_empty_never_deleted(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple; every row asserts its control (the mirror grants both), then the same path returned, still on disk, and neutralised
        """A grant must not outlive its source (kimi's HOLE 1), and a
        deleted mirror lets our unit inherit the manager's environment, so
        withdrawing it never deletes the file. Every way the grant ends
        (the peer runs without it, stops, is gone, cannot be asked, or its
        process is not shown to be the peer's) rewrites the same file with
        both loosening flags empty."""
        gone = (
            ("it runs without them now",
             lambda: self.peer_runs(["PATH=/usr/bin"])),
            ("it is stopped", lambda: self.props.update(MainPID="0")),
            ("no such unit",
             lambda: self.props.update(LoadState="not-found", MainPID="0")),
            ("systemctl cannot be run",
             lambda: setattr(self, "answer", (None, ""))),
            ("its process is another unit's",
             lambda: self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"],
                                    cgroup=_cgroup("app.slice",
                                                   "other.service"))),
        )
        for label, withdraw in gone:
            with self.subTest(label):
                self.answer = None
                self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
                p = chatnode.write_posture_dropin(
                    chatnode.running_posture()[0])
                self.assertTrue(_dregg_runs_unaudited_pq(
                    _effective(self.read(p))), "the control")
                withdraw()
                self.assertEqual(chatnode.write_posture_dropin(
                    chatnode.running_posture()[0]), p)
                self.assertTrue(os.path.exists(p),
                                "removed: our unit inherits the manager's "
                                "environment")
                self.assert_neutralised(self.read(p))

    def test_the_generated_comment_says_what_the_code_does(self):
        """A false guarantee inside a generated file is worse than no
        comment: it is what a future reader checks INSTEAD of the code. The
        file is rewritten, never deleted, and says so."""
        self.peer_runs([UNVERIFIED + "=1"])
        comment = "\n".join(ln for ln in self.mirror().splitlines()
                            if ln.startswith("#"))
        self.assertIn("EMPTY", comment)
        self.assertIn("never deleted", comment)
        self.assertNotIn("DELETES", comment)
        self.assertNotIn("becomes empty", comment)

    def test_the_mirror_targets_our_unit_not_the_peers(self):
        self.peer_runs([UNVERIFIED + "=1"])
        p = chatnode.write_posture_dropin(chatnode.running_posture()[0])
        self.assertIn(chatnode.UNIT + ".d", p)
        self.assertNotIn(chatnode.PEER_UNIT + ".d", p,
                         "never write back into the operator's own declaration")


class FailureRelayTest(PostureBase):
    REFUSAL = ("2026-07-25T15:17:51Z ERROR dregg_node: REFUSING TO START: "
               "`dregg_lean_ffi::lean_available()` is false ... To deliberately "
               "run an un-verified node, set DREGG_ALLOW_UNVERIFIED_CONSENSUS=1.")

    def _journal(self, stdout):
        return mock.patch("helm.chatnode.subprocess.run",
                          return_value=mock.Mock(stdout=stdout, returncode=0))

    def test_the_services_own_words_are_returned(self):
        with self._journal("Started helm-chat-node.service\n" + self.REFUSAL + "\n"):
            self.assertEqual(chatnode.last_failure(), self.REFUSAL)

    def test_the_newest_error_wins(self):
        with self._journal("x ERROR old thing\ny ERROR newest thing\n"):
            self.assertIn("newest", chatnode.last_failure())

    def test_a_clean_journal_is_None_never_a_fabricated_reason(self):
        with self._journal("Started helm-chat-node.service\nlistening on 8898\n"):
            self.assertIsNone(chatnode.last_failure())

    def test_journalctl_missing_degrades_to_None(self):
        with mock.patch("helm.chatnode.subprocess.run", side_effect=OSError("nope")):
            self.assertIsNone(chatnode.last_failure())

    @unittest.skipIf(_sys.flags.utf8_mode,
                     "UTF-8 mode reads every child as UTF-8 whatever the locale")
    def test_the_services_words_are_read_as_utf8_in_any_locale(self):  # noqa: VACUOUS_ASSERTION — the loop is over two literal locales; every row first asserts the simulated locale mis-reads the same fakes (its control), then each reader's exact result
        """dregg's refusal carries an em dash (node/src/lib.rs), and
        journalctl and systemctl print UTF-8 whatever the locale. chatnode's
        readers decoded with the locale: a Latin-1 locale relayed the dash as
        three characters, and an ASCII one raised out of `up`. They read
        UTF-8 (task/3423, the same class as timerhealth._systemctl), and a
        byte that is not UTF-8 is replaced, never raised: each answer is
        read for words, and none is mirrored anywhere."""
        import locale
        from tests._tmphome import fake_user_systemd
        said = (self.REFUSAL.replace("is false ...", "is false — ...")
                + " é")
        self.assertIn("—", said)
        fake = fake_user_systemd(self)
        bindir = os.path.dirname(fake.systemctl)
        for name in ("journalctl", "systemctl"):
            with open(os.path.join(bindir, name), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf '%s\\n'\n" % "".join(
                    "\\%03o" % b for b in said.encode("utf-8")))
            os.chmod(os.path.join(bindir, name), 0o755)
        for encoding in ("iso-8859-1", "ascii"):
            with self.subTest(encoding), mock.patch.object(
                    locale, "getencoding", return_value=encoding):
                try:
                    control = subprocess.run(
                        [os.path.join(bindir, "journalctl")],
                        capture_output=True, text=True).stdout
                except UnicodeDecodeError:
                    control = ""
                self.assertNotIn("—", control,
                                 "the control: this locale mis-reads")
                self.assertEqual(chatnode.last_failure(), said)
                self.assertEqual(chatnode._systemctl("show", chatnode.UNIT),
                                 (0, said))



# A real line from a rebased node's journal as `journalctl -o cat` returns it:
# the node colours its log whether or not stdout is a terminal, and the journal
# keeps the escapes (measured on this host's helm-chat-node journal).
ESC = "\x1b"
EPOCH_REFUSAL = (
    ESC + "[2m2026-09-24T03:55:38.604428Z" + ESC + "[0m " + ESC + "[31mERROR"
    + ESC + "[0m " + ESC + "[2mdregg_node" + ESC + "[0m" + ESC + "[2m:" + ESC
    + "[0m failed to initialize node state: failed to open store: integrity "
    "error: populated store has no canonical state schema epoch; refusing to "
    "reinterpret pre-v11 fields roots / pre-v3 ledger roots (re-genesis "
    "required)")


def _show(active, sub, restarts=0, result="", invocation="",
          started_ago=None, pid=None):
    """`systemctl show` output. `started_ago` seconds sets the main process
    start (ExecMainStartTimestampMonotonic, CLOCK_MONOTONIC microseconds);
    `pid` its MainPID."""
    started = (int((time.monotonic() - started_ago) * 1e6)
               if started_ago is not None else 0)
    return (0, "ActiveState=%s\nSubState=%s\nNRestarts=%d\nResult=%s\n"
               "InvocationID=%s\nExecMainStartTimestampMonotonic=%d%s"
            % (active, sub, restarts, result, invocation, started,
               "\nMainPID=%d" % pid if pid else ""))


# HUNG IS MEASURED, NEVER AGED (task/3891): a flat main-thread window, past
# the boot wait. The arms below that pin the hung words give the unit a main
# process and stand in for the /proc measurement that
# tests/test_chatnode_booting.py drives on a planted /proc.
NODE_PID = 4242
FLAT = {"cpu_s": 4868.0, "delta_s": 0.0, "window_s": 45.0, "progress": "flat"}


@contextlib.contextmanager
def _measured_flat():
    with mock.patch.object(chatnode, "cpu_progress",
                           side_effect=lambda pid: dict(FLAT)), \
            mock.patch.object(chatnode, "api_listener", return_value="none"):
        yield


RUN_ID = "3a5e9072fdb646a5b04f327152efb149"
NONFATAL_SWAP_ERROR = (
    "2026-09-24T03:30:44.553574Z ERROR dregg::lean_shadow::producer: THE SWAP "
    "authority inversion: verified Lean executor (AUTHORITATIVE) and the "
    "demoted Rust reference DISAGREE on a covered turn")


class BootBase(PostureBase):
    """HOME and HELM_HOME in tmp, the boot-wait knob owned, and a systemctl
    stand-in whose `show` answer each arm sets."""

    KEYS = ("HELM_HOME", "HELM_CHAT_NODE_BOOT_WAIT_S",
            "MELD_CHAT_NODE_BOOT_WAIT_S", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL")

    def setUp(self):
        super().setUp()
        self.prior = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.shows = [_show("active", "running")]
        self.calls = []
        # the timeout each daemon-reload `up` passed (task/4204), recorded
        # separately from `self.calls` so its shape stays positional-only
        self.reload_timeouts = []

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def systemctl(self, *args, timeout=30):
        self.calls.append(args)
        if args == ("daemon-reload",):
            # `up`'s daemon-reload passes its own bound (task/4204); record it
            self.reload_timeouts.append(timeout)
        if args[:1] == ("show",) and "--property=DropInPaths" in args:
            # `up`'s drop-in census (chatnode.unit_dropins), not a read of
            # the unit's state: it takes no answer from the scripted `shows`
            return 0, "DropInPaths="
        if args and args[0] == "show":
            return self.shows.pop(0) if len(self.shows) > 1 else self.shows[0]
        if args and args[0] == "is-active":
            return 0, "active"
        return 0, ""

    def journal(self, stdout):
        return mock.patch("helm.chatnode.subprocess.run",
                          return_value=mock.Mock(stdout=stdout, returncode=0))


class BootWaitTest(BootBase):
    def test_the_default_outlasts_a_rebased_nodes_measured_boot(self):
        """150.2-154.2 s to the API on a fast host; a slower one takes longer."""
        self.assertEqual(chatnode.boot_wait_s(), 600)
        self.assertGreater(chatnode.boot_wait_s(), 155)

    def test_the_knob_is_read_and_a_typo_falls_back_to_the_default(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "45"
        self.assertEqual(chatnode.boot_wait_s(), 45.0)
        for bad in ("abc", "-3", "0"):
            os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = bad
            self.assertEqual(chatnode.boot_wait_s(), 600, bad)

    def test_an_exited_unit_ends_the_wait_at_once(self):
        """A failing fast-booting node must not cost the whole ten minutes:
        the wait ends as soon as systemd says the process is gone."""
        self.shows = [_show("activating", "auto-restart", 1)]
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=4)
        self.assertEqual(outcome, "exited")
        self.assertEqual(what["sub"], "auto-restart")
        self.assertLess(time.time() - t0, 3)

    def test_a_restart_during_the_wait_is_an_exit_even_if_it_is_running_again(self):
        self.shows = [_show("active", "running", 0), _show("active", "running", 1)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=6)
        self.assertEqual(outcome, "exited")

    def test_a_start_still_queued_is_not_an_exit(self):
        """`up` starts the unit with --no-block, so the first reads can see
        it inactive/dead before its start job runs; then prepare
        (start-pre) and the node itself. None of that is an exit."""
        self.shows = [_show("inactive", "dead"), _show("inactive", "dead"),
                      _show("activating", "start-pre"),
                      _show("active", "running")]
        answers = iter([None] * 6)
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json",
                                  side_effect=lambda *a, **k: next(answers, [])):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=30)
        self.assertEqual(outcome, "up")

    def test_a_unit_left_failed_by_an_earlier_crash_loop_is_not_this_starts_exit(self):
        """With --no-block the first reads still show the LAST run's
        `failed`; this start has begun only when the unit is active or its
        InvocationID changes. Measured by review: this read as `exited` in
        0.0 s and relayed the old journal as the new failure."""
        old_run, new_run = "a" * 32, "b" * 32
        self.shows = [_show("failed", "failed", 5, "exit-code", old_run),
                      _show("failed", "failed", 5, "exit-code", old_run),
                      _show("activating", "start-pre", 0, "", new_run),
                      _show("active", "running", 0, "", new_run)]
        answers = iter([None] * 6)
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json",
                                  side_effect=lambda *a, **k: next(answers, [])):
            outcome, _what = chatnode.wait_boot("http://127.0.0.1:8898",
                                                unit=chatnode.UNIT, seconds=30)
        self.assertEqual(outcome, "up")

    def test_a_new_run_that_fails_inside_the_grace_is_an_exit_at_once(self):
        old_run, new_run = "a" * 32, "b" * 32
        self.shows = [_show("failed", "failed", 5, "exit-code", old_run),
                      _show("failed", "failed", 0, "exit-code", new_run)]
        t0 = time.time()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=30)
        self.assertEqual((outcome, what["invocation"]), ("exited", new_run))
        self.assertLess(time.time() - t0, chatnode.START_GRACE_S)

    def test_up_never_snapshots_a_stranger_over_the_saved_key(self):
        """Measured by review: after a partial install, the node generated a
        random key; the next `up` snapshotted it over the only copy of the
        real one. identity_state already said matched=False and `up` never
        asked. The doubles bind both identity reads to a temp cave, never the
        live default data dir."""
        cave = os.path.join(self.tmp, "cave")
        os.makedirs(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"K" * 32)
        real_snap, real_state = chatnode.snapshot_identity, chatnode.identity_state
        real_snap(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"S" * 32)                     # the stranger it generated
        provisioned = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                self.node_bin("n"), \
                mock.patch.object(chatnode.cell, "get_json", return_value=[]), \
                mock.patch.object(chatnode, "snapshot_identity",
                                  lambda *_a, **_k: real_snap(cave)), \
                mock.patch.object(chatnode, "identity_state",
                                  lambda *_a, **_k: real_state(cave)), \
                mock.patch.object(chatnode, "provision",
                                  lambda *a, **k: provisioned.append(a) or
                                  (None, "not reached")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["up"])
        self.assertEqual(rc, 1)
        self.assertIn("DISAGREES", err.getvalue())
        self.assertEqual(provisioned, [])
        with open(os.path.join(chatnode.identity_dir(), "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32)

    def test_up_clears_a_failed_unit_before_starting_it(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1"
        self._up()
        verbs = [c[0] for c in self.calls if c and c[0] != "show"]
        self.assertIn("reset-failed", verbs)
        self.assertLess(verbs.index("reset-failed"), verbs.index("enable"))

    def test_a_unit_that_never_starts_is_an_exit_after_the_grace(self):
        self.shows = [_show("inactive", "dead")]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode, "START_GRACE_S", 1), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, what = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=20)
        self.assertEqual((outcome, what["active"]), ("exited", "inactive"))

    def test_a_live_process_past_the_wait_is_initializing_not_failed(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None):
            outcome, secs = chatnode.wait_boot("http://127.0.0.1:8898",
                                               unit=chatnode.UNIT, seconds=1)
        self.assertEqual(outcome, "initializing")
        self.assertGreaterEqual(secs, 1)

    def test_an_answering_api_is_up_and_wait_api_stays_a_bool(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=[]):
            self.assertEqual(chatnode.wait_boot(
                "http://127.0.0.1:8898", unit=chatnode.UNIT, seconds=1)[0], "up")
            self.assertIs(chatnode.wait_api("http://127.0.0.1:8898",
                                            seconds=1), True)
        with mock.patch.object(chatnode.cell, "get_json", return_value=None):
            self.assertIs(chatnode.wait_api("http://127.0.0.1:8898",
                                            seconds=0.2), False)

    def _up(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                self.node_bin(), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["up"])
        return rc, out.getvalue(), err.getvalue()

    def test_up_reports_a_slow_boot_as_initializing_verified_runtime(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1"
        rc, out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertIn("initializing verified runtime", out)
        self.assertIn("still initializing verified runtime", err)
        self.assertIn("has not failed", err)
        self.assertNotIn("did not come up", err)
        self.assertNotIn("exited", err)

    def test_up_against_a_hung_node_says_hung_at_once_in_status_words(self):  # noqa: VACUOUS_ASSERTION — the absent "still initializing" is read on the same `err` that must carry "it is hung, not booting"
        """`up` re-run on a node that has been running two hours without
        its API: `enable --now` does not restart it, so wait_boot must see
        the old run's clock and say `hung` at the first look — the same line
        `helm chat node status` prints — never wait out the boot wait and
        call it "still initializing" while status says restart it."""
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "8"
        self.shows = [_show("active", "running", 0, "success", RUN_ID,
                            started_ago=7200, pid=NODE_PID)]
        t0 = time.time()
        with _measured_flat():
            rc, _out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertLess(time.time() - t0, 5, "waited out the boot wait")
        self.assertNotIn("still initializing", err)
        status = io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                _measured_flat(), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(status):
            chatnode.cmd_node(["status"])
        hung = [ln for ln in status.getvalue().splitlines()
                if "NOT INITIALIZING" in ln]
        self.assertEqual(len(hung), 1, status.getvalue())
        # the same line word for word; only the running seconds may tick
        import re
        norm = lambda t: re.sub(r"running \d+s", "running Ns", t.strip())
        self.assertIn(norm(hung[0]), [norm(ln) for ln in err.splitlines()])
        self.assertIn("it is hung, not booting", err)
        self.assertIn("past the 600s boot wait", err)

    def test_up_relays_a_refusal_at_once_with_its_cure(self):
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "30"
        # `up`'s door reads the stopped unit (task/3891), `up` clears it
        # (reset-failed) and queues the start; the new run — a new
        # InvocationID — refuses and fails
        self.shows = [_show("inactive", "dead"), _show("inactive", "dead"),
                      _show("failed", "failed", 1, "exit-code", "b" * 32)]
        t0 = time.time()
        with self.journal("Started helm-chat-node.service\n" + EPOCH_REFUSAL + "\n"):
            rc, _out, err = self._up()
        self.assertEqual(rc, 1)
        self.assertLess(time.time() - t0, 10, "waited out the clock on a dead node")
        self.assertIn("the service said:", err)
        self.assertIn("no canonical state schema epoch", err)
        self.assertIn("cure: re-genesis: archive the data dir and keep the "
                      "identity", err)
        self.assertNotIn(ESC, err)

    def test_a_refusal_carries_the_readers_own_reason_for_no_mirror(self):  # noqa: VACUOUS_ASSERTION — the loop is over two literal peers; every row asserts rc 1, the relayed refusal and the exact reason sentence in the same stderr
        """meta-claude's B, its second half: `up` held the posture reader's
        reason in `why`, reused the name for the journal line, and for any
        empty posture then said "no DREGG_* posture is declared", right after
        printing that the peer's posture could not be read. A refusal naming
        the escape hatch carries the reader's actual reason; only a peer
        proven to run without the flags reads as running without them."""
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "30"
        for peer, said in (
                (lambda: self.props.update(LoadState="loaded", MainPID="0"),
                 "no posture was proven for %s: %s, so both are set empty "
                 "here" % (chatnode.PEER_UNIT,
                           chatnode.POSTURE_WHY["stopped"])),
                (lambda: self.peer_runs(["DREGG_ADMIN_TOKEN=" + SECRET]),
                 "the running %s does not run with %s or %s, so both are set "
                 "empty here" % (chatnode.PEER_UNIT, UNVERIFIED, UNAUDITED))):
            with self.subTest(said):
                peer()
                # the first read is `up`'s door (task/3891)
                self.shows = [_show("inactive", "dead"),
                              _show("inactive", "dead"),
                              _show("failed", "failed", 1, "exit-code",
                                    "b" * 32)]
                with self.journal("Started helm-chat-node.service\n"
                                  + FailureRelayTest.REFUSAL + "\n"):
                    rc, _out, err = self._up()
                self.assertEqual(rc, 1)
                self.assertIn("the service said:", err)
                self.assertIn(said, err)
                self.assertNotIn("no DREGG_* posture is declared", err)
                self.assertNotIn(SECRET, err)

class RefusalRemedyTest(BootBase):
    LINES = {
        "store-epoch": [
            "canonical state schema epoch 25 is incompatible with required "
            "epoch 26; re-genesis is required",
            "populated store has no canonical state schema epoch; refusing to "
            "reinterpret pre-v11 fields roots / pre-v3 ledger roots (re-genesis "
            "required)"],
        "clock-policy": [
            "ERROR dregg_node: blocklace requires consensus_genesis_unix_seconds "
            "+ consensus_time_mode in the shared genesis.json; refusing an "
            "implicit clock policy"],
        "pq-identity": [
            "[client-sign] error: node refused the turn: required post-quantum "
            "signer identity is neither Cell-committed nor independently "
            "enrolled for migration"],
        "swap-veto": [
            "[client-sign] error: node refused the turn: rejected: verified Lean "
            "executor vetoed the commit (THE SWAP strict mode): the legacy Rust "
            "executor accepted this turn but the verified kernel rejected it"],
    }

    def test_each_known_refusal_is_classified_by_the_nodes_own_words(self):
        for kind, lines in self.LINES.items():
            for line in lines:
                hit = chatnode.classify_refusal(line)
                self.assertIsNotNone(hit, line)
                self.assertEqual(hit["kind"], kind, line)
        self.assertIsNone(chatnode.classify_refusal(
            "ERROR dregg_node: something nobody has a cure for"))

    def test_the_regenesis_cure_keeps_the_identity_it_has(self):
        cave = os.path.join(self.tmp, "cave")
        os.makedirs(cave)
        with open(os.path.join(cave, "node.key"), "wb") as f:
            f.write(b"k" * 32)
        chatnode.snapshot_identity(cave)
        cure = chatnode.refusal_remedy(self.LINES["store-epoch"][1], data_dir=cave)
        self.assertIn("archive the data dir and keep the identity", cure)
        self.assertIn("mv %s %s.pre-regenesis-" % (cave, cave), cure)
        self.assertIn("prepare restores node.key from %s"
                      % chatnode.identity_dir(), cure)
        self.assertNotIn("NO identity snapshot", cure)

    def test_the_regenesis_cure_warns_when_no_identity_is_snapshotted(self):
        cure = chatnode.refusal_remedy(self.LINES["store-epoch"][0],
                                       data_dir="/dev/shm/x")
        self.assertIn("NO identity snapshot exists", cure)
        self.assertIn("node.key", cure)

    def test_the_cave_nodes_disk_copy_is_named_only_for_the_cave_node(self):
        """dregg-cave-restore copies ~/.local/share/dregg-cave/data back into
        /dev/shm/dregg-cave on every boot, so archiving only the cave node's
        tmpfs store brings the old-epoch store straight back. The chat node's
        cure must not tell the operator to archive the cave node's store —
        that directory is the rollback path's — and says it is untouched."""
        line = self.LINES["store-epoch"][0]
        disk = os.path.join(self.tmp, ".local", "share", "dregg-cave", "data")
        os.makedirs(disk)
        chat_cure = chatnode.refusal_remedy(line)
        self.assertNotIn("dregg-cave-restore", chat_cure)
        self.assertIn("untouched by this", chat_cure)
        cave_cure = chatnode.refusal_remedy(line, data_dir="/dev/shm/dregg-cave")
        self.assertIn("archive %s too" % disk, cave_cure)
        self.assertIn("dregg-cave-restore", cave_cure)

    def test_the_genesis_cure_is_the_successor_ceremony_that_keeps_the_key(self):
        cure = chatnode.refusal_remedy(self.LINES["clock-policy"][0])
        self.assertIn("successor ceremony", cure)
        self.assertIn("prepare mints the chain descriptor around the SAME key",
                      cure)

    def test_a_signer_side_refusal_records_its_cure_not_inspect_and_retry(self):
        from helm import chat
        d = chat._diag("send_failed", self.LINES["pq-identity"][0])
        self.assertIn("claimable stub", d["remediation"])
        d = chat._diag("send_failed", self.LINES["swap-veto"][0])
        self.assertIn("federation id", d["remediation"])
        d = chat._diag("send_failed", "node refused the turn: rate limited")
        self.assertIn("inspect `helm chat node status`", d["remediation"])


class ColouredJournalTest(BootBase):
    def test_a_coloured_error_line_is_found_and_uncoloured(self):
        """The real journal line carries escapes around ERROR, so it never
        contained " ERROR " and was never relayed."""
        with self.journal("Started helm-chat-node.service\n" + EPOCH_REFUSAL + "\n"):
            got = chatnode.last_failure()
        self.assertIsNotNone(got)
        self.assertIn("ERROR dregg_node: failed to initialize node state", got)
        self.assertNotIn(ESC, got)


class UnreachableDiagnosisTest(BootBase):
    """`helm chat node status` and `helm doctor` read one diagnosis."""

    def _status(self):
        out = io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(out):
            rc = chatnode.cmd_node(["status"])
        return rc, out.getvalue()

    def _doctor(self):
        from helm import cell, chat, doctor
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(cell, "bin_status",
                                  return_value={"configured": False,
                                                "usable": False}):
            return doctor.check_chat_node()

    def test_a_running_node_without_its_api_is_initializing_not_unreachable(self):
        rc, out = self._status()
        self.assertEqual(rc, 1)
        self.assertIn("INITIALIZING VERIFIED RUNTIME", out)
        self.assertNotIn("UNREACHABLE", out)
        rows = self._doctor()
        self.assertTrue(any(level == "WARN" and "INITIALIZING VERIFIED RUNTIME"
                            in text for level, text in rows), rows)

    def test_a_refused_node_names_the_refusal_and_its_cure(self):
        self.shows = [_show("failed", "failed", 5, "exit-code", RUN_ID)]
        with self.journal(EPOCH_REFUSAL + "\n"):
            _rc, out = self._status()
            rows = self._doctor()
        self.assertIn("REFUSED TO START", out)
        self.assertIn("cure: re-genesis", out)
        hit = [t for level, t in rows if level == "FAIL"]
        self.assertTrue(hit and "cure: re-genesis" in hit[0], rows)

    def test_a_url_that_is_not_this_units_is_never_diagnosed_from_its_journal(self):  # noqa: VACUOUS_ASSERTION — the spy is installed on the same observable, and test_this_units_url_asks_systemd_once is its positive control: the identical spy records one ("show", UNIT) call when the URL is this unit's
        """The spy is INSTALLED here, so an empty call list means systemd was
        never asked; the mirror arm below proves the same spy records a call
        when the URL is this unit's."""
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8899")
        self.assertEqual(d["state"], "unknown")
        self.assertEqual(self.calls, [])

    def test_this_units_url_asks_systemd_once(self):
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d["state"], "initializing")
        self.assertEqual([c[:2] for c in self.calls], [("show", chatnode.UNIT)])

    def run_journal(self, current, older):
        """A journal that answers per scope: this run's lines when asked by
        _SYSTEMD_INVOCATION_ID, every line of the unit when asked by -u."""
        seen = []

        def run(argv, **_kw):
            seen.append(argv)
            scoped = any(a.startswith("_SYSTEMD_INVOCATION_ID=") for a in argv)
            return mock.Mock(returncode=0,
                             stdout=current if scoped else older + current)
        return mock.patch("helm.chatnode.subprocess.run", side_effect=run), seen

    def test_a_clean_stop_is_down_never_an_earlier_runs_refusal(self):
        """`helm chat node down` leaves inactive/dead with Result=success. An
        older run's store-epoch ERROR, twelve lines back in the journal, is
        not this stop's cause: status must not print REFUSED with a cure
        that moves the data dir aside, and doctor must not FAIL."""
        self.shows = [_show("inactive", "dead", 0, "success", RUN_ID)]
        older = EPOCH_REFUSAL + "\n" + "INFO dregg_node: served a turn\n" * 12
        # and THIS run logged an ERROR that has a cure but did not end it — a
        # running node reports THE SWAP authority inversion at ERROR and
        # keeps serving (measured on a scratch node). Result=success, not
        # the absence of an error line, is what says the stop was clean.
        current = (NONFATAL_SWAP_ERROR + "\n"
                   + "INFO dregg_node: HTTP server shut down gracefully\n")
        patch, _seen = self.run_journal(current, older)
        with patch:
            _rc, out = self._status()
            rows = self._doctor()
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("REFUSED", out)
        self.assertNotIn("re-genesis", out)
        self.assertEqual([t for level, t in rows if level == "FAIL"], [])
        self.assertTrue(any(level == "WARN" and "UNREACHABLE" in t
                            for level, t in rows), rows)

    def test_the_journal_is_read_for_the_run_that_exited(self):
        """A crash in THIS run is relayed from this run's lines; an older
        run's refusal further back never is."""
        self.shows = [_show("failed", "failed", 5, "exit-code", RUN_ID)]
        now_line = ("2026-09-24T04:04:40.680720Z ERROR dregg_node: failed to "
                    "bind 127.0.0.1:8898: address already in use")
        patch, seen = self.run_journal(now_line + "\n", EPOCH_REFUSAL + "\n")
        with patch:
            _rc, out = self._status()
        self.assertIn("the service said: " + now_line, out)
        self.assertNotIn("re-genesis", out)
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + RUN_ID, seen[0])
        self.assertNotIn("-u", seen[0])

    def test_a_node_running_past_the_boot_wait_is_hung_not_initializing(self):
        self.shows = [_show("active", "running", 0, "success", RUN_ID,
                            started_ago=7200, pid=NODE_PID)]
        with _measured_flat():
            _rc, out = self._status()
            rows = self._doctor()
        self.assertIn("NOT INITIALIZING", out)
        self.assertNotIn("INITIALIZING VERIFIED RUNTIME", out)
        self.assertIn("hung", out)
        self.assertTrue(any(level == "FAIL" and "NOT INITIALIZING" in t
                            for level, t in rows), rows)

    def test_a_running_node_that_restarted_before_is_initializing(self):
        """NRestarts counts every restart since the unit was loaded; a node
        that crashed yesterday and is booting now has not exited now."""
        self.shows = [_show("active", "running", 3, "success", RUN_ID,
                            started_ago=20)]
        with self.journal(EPOCH_REFUSAL + "\n"), \
                mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d["state"], "initializing")

    def _diagnosis(self, *show, **kw):
        self.shows = [_show(*show, **kw)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            return chatnode.boot_diagnosis("http://127.0.0.1:8898")

    def test_a_lowered_boot_wait_never_makes_a_booting_node_hung(self):
        """HELM_CHAT_NODE_BOOT_WAIT_S is `up`'s patience, and the fee-loop
        build answers in under a second, so 30 is a reasonable setting. It
        must not become the hung threshold: a rebased node 45 s into its
        150 s Lean init is booting, and doctor FAILing it (with status
        saying `down` and `up`) would kill a healthy boot. The threshold is
        the wait floored at its default; raising the wait raises it."""
        os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "30"
        booting = ("active", "running", 0, "success", RUN_ID)
        self.assertEqual(chatnode.hung_after_s(), 600)
        with _measured_flat():
            # a main thread flat over its window is still booting inside
            # the floor; only past it does the flat window read hung
            self.assertEqual(self._diagnosis(*booting, started_ago=45,
                                             pid=NODE_PID)["state"],
                             "booting")
            self.assertEqual(self._diagnosis(*booting, started_ago=599,
                                             pid=NODE_PID)["state"],
                             "booting")
            d = self._diagnosis(*booting, started_ago=601, pid=NODE_PID)
            self.assertEqual(d["state"], "hung")
            self.assertIn("600s boot wait", d["line"])
            os.environ["HELM_CHAT_NODE_BOOT_WAIT_S"] = "1200"
            self.assertEqual(chatnode.hung_after_s(), 1200)
            self.assertEqual(self._diagnosis(*booting, started_ago=601,
                                             pid=NODE_PID)["state"],
                             "booting")
            self.assertEqual(self._diagnosis(*booting, started_ago=1201,
                                             pid=NODE_PID)["state"],
                             "hung")

    def test_a_node_being_stopped_is_down_not_hung(self):
        """`systemctl stop` on a node that served for two hours leaves it
        `deactivating` for up to TimeoutStopSec. Its process has run far
        past the boot wait without (now) answering, but a stop in flight is
        not a hung boot: down, no FAIL, no `down` and `up` advice."""
        self.shows = [_show("deactivating", "stop-sigterm", 0, "success",
                            RUN_ID, started_ago=7200)]
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            d = chatnode.boot_diagnosis("http://127.0.0.1:8898")
        self.assertEqual(d, {"state": "down", "line": None, "remedy": None})
        _rc, out = self._status()
        rows = self._doctor()
        self.assertIn("UNREACHABLE", out)
        self.assertNotIn("hung", out)
        self.assertTrue(any(level == "WARN" and "UNREACHABLE" in t
                            for level, t in rows), rows)
        self.assertEqual([t for level, t in rows if level == "FAIL"], [])


class PriorityTest(BootBase):
    """THE NODE YIELDS THE CPU TO THE OWNER'S PANES, and `up` keeps it so.

    Measured on the owner's laptop while his typing lagged: load
    38.7, CPU pressure some=55-63%, the node at 303% CPU proving signed chat
    turns. A runtime CPUWeight=20 and renice +10 brought
    pressure to 32% within a minute and were lost at the next restart; the
    drop-in `up` writes is what outlives one. Proofs attach after a turn
    commits, so the priority delays proofs, never delivery.

    AND IT BOUNDS THE PROVER (task/3851). A signed send cost the node about
    25 CPU-seconds in bursts of 7-12 cores, from a 24-thread rayon pool, in
    app.slice at weight 100 against the fleet's 25. The same drop-in now
    holds RAYON_NUM_THREADS, DREGG_PROVE_WORKERS=1 and background.slice."""

    def dropin(self):
        return os.path.join(chatnode._dropin_dir(chatnode.UNIT),
                            chatnode.PRIORITY_DROPIN)

    def body(self):
        with open(self.dropin(), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def keys(body):
        return [ln.split("=", 1)[0] for ln in body.splitlines()
                if "=" in ln and not ln.startswith("#")]

    def up(self):
        """`up` as far as the boot wait, which reads as still initializing:
        the drop-ins and the daemon-reload are behind it by then. Returns
        stdout and, per daemon-reload, whether the drop-in was on disk."""
        loaded = []

        def systemctl(*args, timeout=30):
            if args[:1] == ("daemon-reload",):
                loaded.append(os.path.exists(self.dropin()))
            return self.systemctl(*args, timeout=timeout)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", systemctl), \
                self.node_bin(), \
                mock.patch.object(chatnode, "wait_boot",
                                  return_value=("initializing", 1)), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            chatnode.cmd_node(["up"])
        self.err = err.getvalue()
        return out.getvalue(), loaded

    def test_up_writes_the_priority_even_with_no_posture_declared(self):
        out, loaded = self.up()
        body = self.body()
        self.assertIn("[Service]", body.splitlines())
        self.assertEqual(self.keys(body), ["CPUWeight", "Nice", "Slice",
                                           "Environment", "Environment"],
                         "exactly these keys, and no IOWeight")
        self.assertIn("CPUWeight=%d" % chatnode.CPU_WEIGHT, body.splitlines())
        self.assertIn("Nice=%d" % chatnode.NICE, body.splitlines())
        self.assertIn("Slice=background.slice", body.splitlines())
        self.assertEqual(_environment_set(body), {
            "RAYON_NUM_THREADS": str(chatnode.prove_threads()),
            "DREGG_PROVE_WORKERS": "1"})
        self.assertLess(chatnode.CPU_WEIGHT, 100,
                        "systemd weighs every sibling 100 by default")
        self.assertGreater(chatnode.NICE, 0)
        self.assertTrue(any(ln.startswith("#") and "delivery" in ln
                            for ln in body.splitlines()),
                        "the file must say why it delays proofs, not turns")
        self.assertEqual(loaded, [False, True],
                         "first reload is before drop-in write (False), "
                         "second reload is after (True)")
        self.assertIn(chatnode.PRIORITY_DROPIN, out)

    def test_rerunning_up_leaves_one_identical_file(self):
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
        self.up()
        first = self.body()
        self.assertIn("[Service]", first.splitlines())
        out, _loaded = self.up()
        self.assertIn("recorded", out, "the second `up` ran and printed")
        self.assertEqual(self.body(), first)
        self.assertEqual(sorted(os.listdir(chatnode._dropin_dir(chatnode.UNIT))),
                         ["10-helm-posture.conf", chatnode.PRIORITY_DROPIN],
                         "one of each, the posture one written every `up`")
        self.assertNotIn(chatnode.PRIORITY_DROPIN, out,
                         "an unchanged drop-in is not news")

    def test_the_dropin_is_0600_like_its_sibling(self):
        self.peer_runs([UNVERIFIED + "=1"])
        self.up()
        d = chatnode._dropin_dir(chatnode.UNIT)
        self.assertEqual(sorted(os.listdir(d)),
                         ["10-helm-posture.conf", chatnode.PRIORITY_DROPIN])
        for name in os.listdir(d):
            self.assertEqual(oct(os.stat(os.path.join(d, name)).st_mode)[-3:],
                             "600", name)

    def test_an_edited_or_loosened_dropin_is_put_back(self):
        self.up()
        want = self.body()
        self.assertIn("[Service]", want.splitlines())
        with open(self.dropin(), "w", encoding="utf-8") as f:
            f.write("[Service]\nCPUWeight=100\n")
        out, _loaded = self.up()
        self.assertEqual(self.body(), want)
        self.assertIn(chatnode.PRIORITY_DROPIN, out)
        os.chmod(self.dropin(), 0o644)          # the same bytes, loosened
        self.up()
        self.assertEqual(oct(os.stat(self.dropin()).st_mode)[-3:], "600")

    def test_the_prover_bound_follows_the_online_cpus(self):
        """RAYON_NUM_THREADS is max(2, online // 6), from the CPUs the
        kernel counts online. Never nproc, which a cgroup CPU quota shrinks:
        it printed 8 on the owner's 24-CPU laptop."""
        self.assertEqual([chatnode.prove_threads(n) for n in (1, 8, 12, 24, 96)],
                         [2, 2, 2, 4, 16])
        real = os.sysconf
        asked = []

        def sysconf(name):
            asked.append(name)
            return 48 if name == "SC_NPROCESSORS_ONLN" else real(name)
        with mock.patch.object(chatnode.os, "sysconf", side_effect=sysconf):
            out, _loaded = self.up()
        self.assertIn("SC_NPROCESSORS_ONLN", asked)
        self.assertEqual(_environment_set(self.body()), {
            "RAYON_NUM_THREADS": "8", "DREGG_PROVE_WORKERS": "1"})
        self.assertIn("CPUWeight=20 Nice=10 Slice=background.slice "
                      "RAYON_NUM_THREADS=8 DREGG_PROVE_WORKERS=1", out)
        self.assertIn("next start", out)

    def test_a_dropin_that_is_not_helms_is_named_and_never_written(self):  # noqa: VACUOUS_ASSERTION — the quiet files' absence sits beside straight-line assertIn on the same out and self.err, which name the 30- and 05- files
        """F3's local cure was a hand-made 30-prove-bound.conf. `up` never
        writes a drop-in it did not write, not even its mode: it names each
        one that sets a directive 20-helm-priority.conf sets, and says which
        of the two systemd applies. One that sets none of them, or sets one
        outside [Service], is not news, and helm's own two are never named."""
        d = chatnode._dropin_dir(chatnode.UNIT)
        os.makedirs(d, exist_ok=True)
        planted = {
            "30-prove-bound.conf": "[Service]\nEnvironment=RAYON_NUM_THREADS=12"
                                   "\nSlice=app.slice\n",
            "05-early.conf": "[Service]\nNice=0\n",
            "40-memory.conf": "[Service]\nMemoryHigh=8G\n",
            "45-unit.conf": "[Unit]\nDescription=CPUWeight=1 in prose\n",
        }
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
        for name, text in planted.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(os.path.join(d, name), 0o644)
        out, _loaded = self.up()
        out2, _loaded = self.up()
        for name, text in planted.items():
            with open(os.path.join(d, name), encoding="utf-8") as f:
                self.assertEqual(f.read(), text, name)
            self.assertEqual(oct(os.stat(os.path.join(d, name)).st_mode)[-3:],
                             "644", name)
        self.assertEqual(sorted(os.listdir(d)),
                         sorted(list(planted) + list(chatnode.HELM_DROPINS)))
        self.assertIn(
            "30-prove-bound.conf is not helm's and also sets Slice, "
            "Environment; it sorts after 20-helm-priority.conf, so systemd "
            "applies its values over helm's. helm never writes it", self.err)
        self.assertIn(
            "05-early.conf is not helm's and also sets Nice; it sorts before "
            "20-helm-priority.conf, so helm's values win", out)
        self.assertIn("05-early.conf", out2, "named on every `up`")
        for quiet in ("40-memory.conf", "45-unit.conf"):
            self.assertNotIn(quiet, out + self.err)
        for own in chatnode.HELM_DROPINS:
            self.assertNotIn(own + " is not helm's", out + out2 + self.err)
        self.assertEqual(_environment_set(self.body())["RAYON_NUM_THREADS"],
                         str(chatnode.prove_threads()),
                         "helm's own file keeps helm's value beside it")

    def test_up_says_why_it_proved_nothing_and_keeps_the_previous_mirror(self):
        """`up` reloads first, then reads the peer, then reloads again after
        writing drop-ins. A peer that is not running has no environment to
        read: `up` says so in the fixed words, and keeps the mirror an earlier
        `up` wrote (a failed probe must never overwrite the posture, which a
        successful probe that read the running node's values alone may write).
        A flag the peer runs without is named the same way beside the one it
        mirrors."""
        self.peer_runs([UNVERIFIED + "=1"])
        out, _loaded = self.up()
        self.assertIn("helm chat node: mirrored 1 posture flag from the "
                      "running %s; 10-helm-posture.conf sets %s empty, which "
                      "dregg reads as refusal" % (chatnode.PEER_UNIT,
                                                  UNAUDITED), out)
        self.props["MainPID"] = "0"
        out, _loaded = self.up()
        self.assertIn("helm chat node: %s — %s; keeping "
                      "the existing 10-helm-posture.conf unchanged"
                      % (chatnode.PEER_UNIT, chatnode.POSTURE_WHY["stopped"]),
                      out)
        self.assertEqual(sorted(os.listdir(chatnode._dropin_dir(chatnode.UNIT))),
                         ["10-helm-posture.conf", chatnode.PRIORITY_DROPIN])
        self.assertIn(PEER_ASKED, self.asked)

    def test_up_says_a_peer_running_with_no_posture_flag_runs_with_none(self):
        """A peer that runs with a DREGG_ secret and no posture flag has
        nothing the mirror carries: `up` says the running peer runs with no
        posture flag, never that it has no DREGG_ variable, which it has,
        and never names the secret."""
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"])
        out, _loaded = self.up()
        self.assertIn("helm chat node: mirrored 2 posture flags from the "
                      "running %s; 10-helm-posture.conf written"
                      % chatnode.PEER_UNIT, out)
        self.peer_runs(["DREGG_ADMIN_TOKEN=" + SECRET])
        out, _loaded = self.up()
        self.assertIn("helm chat node: the running %s runs with no posture "
                      "flag; 10-helm-posture.conf sets %s and %s empty, which "
                      "dregg reads as refusal" % (chatnode.PEER_UNIT,
                                                  UNVERIFIED, UNAUDITED), out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("DREGG_ADMIN_TOKEN", out)

    def test_up_adds_no_restart_to_the_reload_it_already_does(self):
        self.up()
        verbs = {c[0] for c in self.calls if c}
        self.assertIn("daemon-reload", verbs)
        self.assertEqual(verbs & {"restart", "try-restart", "reload-or-restart",
                                  "stop", "kill"}, set())

    def test_up_reloads_before_reading_peer_and_again_after_dropins(self):
        """The first daemon-reload runs BEFORE posture reading so that
        pending unit changes are applied when `running_posture` queries the
        peer (task/3433: first `up` sees NeedDaemonReload=yes on its own
        unit and fails closed).  A second reload runs AFTER writing the
        posture and priority drop-ins so they are loaded into the start
        that follows."""
        self.peer_runs([UNVERIFIED + "=1"])
        reload_positions = []
        reload_timeouts = []
        posture_asked = None

        def track_chatnode_systemctl(*args, timeout=10):
            nonlocal posture_asked
            if args[:1] == ("daemon-reload",):
                reload_positions.append(len(self.calls))
                reload_timeouts.append(timeout)
            return self.systemctl(*args, timeout=timeout)

        def track_timerhealth_systemctl(argv, timeout=10):
            nonlocal posture_asked
            if list(argv)[:2] == ["show", chatnode.PEER_UNIT]:
                posture_asked = len(self.calls)
            return self.systemctl(argv, timeout)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", track_chatnode_systemctl), \
                mock.patch("helm.timerhealth._systemctl",
                           track_timerhealth_systemctl), \
                self.node_bin(), \
                mock.patch.object(chatnode, "wait_boot",
                                  return_value=("initializing", 1)), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            chatnode.cmd_node(["up"])
        self.assertEqual(len(reload_positions), 2,
                         "exactly two daemon-reloads: %s" % reload_positions)
        self.assertIsNotNone(posture_asked)
        self.assertLess(reload_positions[0], posture_asked,
                        "first reload must precede peer posture read")
        self.assertLess(posture_asked, reload_positions[1],
                        "posture read must precede second reload")
        self.assertEqual(reload_timeouts, [chatnode.RELOAD_TIMEOUT_S] * 2,
                         "both daemon-reloads carry the reload bound, not the "
                         "30 s default — the second reload loads the drop-in "
                         "`up` just wrote and must not time out on ~7k units")
        self.assertGreaterEqual(chatnode.RELOAD_TIMEOUT_S, 120,
                                "the reload bound is not sized for the measured "
                                "reload")

    def status(self, weight, nice, running, slice_="background.slice",
               env=None, group=None, dropins=""):
        """`status` over a unit systemd describes with these values. The
        main process's nice is `running`; None is a stopped node, MainPID 0,
        and getpriority then answers 0 — what the kernel says for PID 0, the
        CALLER — so a read that asks it anyway prints a nice it must not.
        `env` is the unit's Environment as systemctl prints it (by default
        the prover bound beside another variable), `group` the running
        process's control group (by default in `slice_`; empty when
        stopped), `dropins` the DropInPaths line."""
        if env is None:
            env = ("RUST_LOG=info,dregg_node::blocklace_sync=warn "
                   "RAYON_NUM_THREADS=%d DREGG_PROVE_WORKERS=1"
                   % chatnode.prove_threads())
        if group is None:
            group = "" if running is None else (
                "/user.slice/user-1000.slice/user@1000.service/%s/%s"
                % (slice_, chatnode.UNIT))
        asked = []

        def systemctl(*args, timeout=30):
            if any(a.startswith("--property=CPUWeight") for a in args):
                asked.extend(args)
                return 0, ("CPUWeight=%s\nNice=%s\nSlice=%s\nEnvironment=%s\n"
                           "ControlGroup=%s\nDropInPaths=%s\nMainPID=%d" % (
                               weight, nice, slice_, env, group, dropins,
                               0 if running is None else 4242))
            return self.systemctl(*args, timeout=timeout)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", systemctl), \
                mock.patch.object(chatnode.os, "getpriority",
                                  return_value=running or 0), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["status"])
        return rc, out.getvalue(), err.getvalue(), asked

    def test_status_reports_the_priority_the_unit_runs_with(self):
        rc, out, err, asked = self.status(20, 10, 10)
        line = [ln for ln in out.splitlines() if "CPUWeight" in ln]
        self.assertEqual(len(line), 1, out)
        self.assertIn("yields the CPU to interactive work — CPUWeight 20, "
                      "Nice 10, Slice background.slice, RAYON_NUM_THREADS %d, "
                      "DREGG_PROVE_WORKERS 1, main process nice 10"
                      % chatnode.prove_threads(), line[0])
        self.assertNotIn("CPUWeight", err)
        self.up()
        props = [a for a in asked if a.startswith("--property=")][0]
        self.assertLessEqual(set(self.keys(self.body())),
                             set(props.split("=", 1)[1].split(",")),
                             "status must read every key the drop-in sets")
        self.assertEqual(self.status("[not set]", 0, 0)[0], rc,
                         "the priority line informs; it never fails status")

    def test_status_says_when_the_unit_does_not_yield(self):
        _rc, out, err, _asked = self.status("[not set]", 0, 0)
        self.assertNotIn("CPUWeight", out)
        self.assertIn("does NOT yield", err)
        self.assertIn("CPUWeight unset, Nice 0", err)
        self.assertIn("helm chat node up", err)

    def test_status_says_when_the_running_process_predates_the_nice(self):
        _rc, _out, err, _asked = self.status(20, 10, 0)
        self.assertIn("main process nice 0", err)
        self.assertIn("next start", err)

    def test_a_stopped_node_is_judged_by_its_unit_alone(self):
        _rc, out, err, _asked = self.status(20, 10, None)
        self.assertIn("DREGG_PROVE_WORKERS 1, main process nice -", out)
        self.assertNotIn("CPUWeight", err)

    def test_a_running_node_in_its_old_slice_takes_the_bound_at_next_start(self):
        """`up` reloads systemd, which then holds Slice=background.slice, but
        a process keeps the control group, the environment and the nice it
        was started with. Its slice says which it runs with."""
        old = ("/user.slice/user-1000.slice/user@1000.service/app.slice/%s"
               % chatnode.UNIT)
        _rc, out, err, _asked = self.status(20, 10, 10, group=old)
        self.assertNotIn("CPUWeight", out)
        self.assertIn("yields the CPU by weight, not yet by slice", err)
        self.assertIn("main process nice 10 in app.slice", err)
        self.assertIn("its slice, prover bound and Nice=10 at its next start",
                      err)

    def test_status_says_when_the_prover_is_not_bound(self):
        _rc, out, err, _asked = self.status(20, 10, 10, env="RUST_LOG=info",
                                            slice_="app.slice")
        self.assertNotIn("CPUWeight", out)
        self.assertIn("does NOT yield", err)
        self.assertIn("Slice app.slice, RAYON_NUM_THREADS unset, "
                      "DREGG_PROVE_WORKERS unset", err)
        self.assertIn("RAYON_NUM_THREADS=%d DREGG_PROVE_WORKERS=1. Fix: helm "
                      "chat node up" % chatnode.prove_threads(), err)

    def test_status_reads_only_the_prover_names_from_the_environment(self):
        """The unit's Environment can hold a token. status prints the two
        values it judges, and nothing else from that line."""
        env = ('DREGG_ADMIN_TOKEN=%s "NOTE=a b" RAYON_NUM_THREADS=%d '
               'DREGG_PROVE_WORKERS=1' % (SECRET, chatnode.prove_threads()))
        _rc, out, err, _asked = self.status(20, 10, 10, env=env)
        self.assertIn("yields the CPU to interactive work", out)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn("DREGG_ADMIN_TOKEN", out + err)
        _rc, out, err, _asked = self.status(
            20, 10, 10, env="RAYON_NUM_THREADS=%s DREGG_PROVE_WORKERS=1"
            % SECRET)
        self.assertIn("RAYON_NUM_THREADS (a value not shown)", err)
        self.assertNotIn(SECRET, out + err)

    def test_status_names_a_later_dropin_that_is_not_helms(self):
        """When the values are wrong because someone else's drop-in sorts
        after helm's, `up` would change nothing: the line names that file,
        from systemd's own list of the unit's drop-ins, in any directory."""
        d = os.path.join(self.tmp, "user.control", chatnode.UNIT + ".d")
        os.makedirs(d)
        theirs = os.path.join(d, "50-Slice.conf")
        with open(theirs, "w", encoding="utf-8") as f:
            f.write("[Service]\nSlice=app.slice\n")
        ours = os.path.join(chatnode._dropin_dir(chatnode.UNIT),
                            chatnode.PRIORITY_DROPIN)
        _rc, _out, err, _asked = self.status(
            20, 10, 10, slice_="app.slice", dropins="%s %s" % (ours, theirs))
        self.assertIn("does NOT yield", err)
        self.assertIn("50-Slice.conf is not helm's and also sets Slice; it "
                      "sorts after 20-helm-priority.conf, so systemd applies "
                      "its values over helm's", err)
        self.assertNotIn("Fix: helm chat node up", err)

    def later_dropin(self, name, text):
        """A drop-in systemd reads from user.control, where `systemctl
        set-property` writes: (helm's own file's path, that file's path)."""
        d = os.path.join(self.tmp, "user.control", chatnode.UNIT + ".d")
        os.makedirs(d, exist_ok=True)
        theirs = os.path.join(d, name)
        with open(theirs, "w", encoding="utf-8") as f:
            f.write(text)
        return (os.path.join(chatnode._dropin_dir(chatnode.UNIT),
                             chatnode.PRIORITY_DROPIN), theirs)

    def test_a_later_dropin_is_blamed_only_for_a_value_it_sets(self):
        """Measured on the owner's laptop: a `set-property --runtime`
        50-CPUWeight.conf holds CPUWeight=20, helm's own value, while the
        unit had no Slice or prover bound yet. status named that file and
        dropped the cure, `up`. A later file is named only when it sets a
        directive whose value is wrong, and a wrong value no later file sets
        still needs `up`."""
        ours, theirs = self.later_dropin("50-CPUWeight.conf",
                                         "[Service]\nCPUWeight=20\n")
        _rc, _out, err, _asked = self.status(
            20, 10, 10, slice_="app.slice", env="RUST_LOG=info",
            dropins="%s %s" % (ours, theirs))
        self.assertIn("does NOT yield", err)
        self.assertIn("Fix: helm chat node up", err)
        self.assertNotIn("50-CPUWeight.conf", err)
        ours, theirs = self.later_dropin("60-Slice.conf",
                                         "[Service]\nSlice=app.slice\n")
        _rc, _out, err, _asked = self.status(
            20, 10, 10, slice_="app.slice", env="RUST_LOG=info",
            dropins="%s %s" % (ours, theirs))
        self.assertIn("60-Slice.conf is not helm's and also sets Slice; it "
                      "sorts after", err)
        self.assertIn("Fix: helm chat node up", err,
                      "the prover bound is still unset, and only `up` sets it")

    def test_up_names_a_later_dropin_in_another_directory(self):
        """`up` weighs every drop-in systemd reads, not only the files in
        helm's own directory: `systemctl set-property` writes its
        50-<Property>.conf into user.control, which sorts after
        20-helm-priority.conf and wins."""
        ours, theirs = self.later_dropin("50-CPUWeight.conf",
                                         "[Service]\nCPUWeight=100\n")
        real = self.systemctl

        def systemctl(*args, timeout=30):
            if args[:1] == ("show",) and "--property=DropInPaths" in args:
                return 0, "DropInPaths=%s %s" % (ours, theirs)
            return real(*args, timeout=timeout)
        self.systemctl = systemctl
        _out, _loaded = self.up()
        self.assertIn("50-CPUWeight.conf is not helm's and also sets "
                      "CPUWeight; it sorts after 20-helm-priority.conf, so "
                      "systemd applies its values over helm's", self.err)
        with open(theirs, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[Service]\nCPUWeight=100\n",
                             "helm never writes it")

    def test_a_systemd_that_does_not_describe_the_unit_prints_nothing(self):
        out = io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                mock.patch.object(chatnode.cell, "get_json", return_value=None), \
                mock.patch("helm.chat.transport_status",
                           return_value={"mode": "unsigned"}), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            chatnode.cmd_node(["status"])
        self.assertIn("helm chat node: unit " + chatnode.UNIT, out.getvalue())
        self.assertNotIn("CPUWeight", out.getvalue())
        self.assertIn(("show", chatnode.UNIT,
                       "--property=CPUWeight,Nice,Slice,Environment,"
                       "ControlGroup,DropInPaths,MainPID"), self.calls,
                      "the silence must be systemd's, not a skipped read")


class RunningPeerUpTest(BootBase):
    """`up` WRITES WHAT THE RUNNING PEER PROVES AND WRITES THE REST EMPTY
    (task/3432 round 2), through the verb itself. The peer unit's own
    declarations (what systemctl reports for its Environment=) are beside
    each arm, so a reader that rebuilt the environment from them is judged
    by the same arm: it wrote no drop-in when it found no flag or could not
    answer, and our unit then inherited what the user manager exports."""

    def up(self):
        """`up` as far as the boot wait: (stdout, stderr, the posture
        drop-in's text, or None when there is none)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", self.systemctl), \
                self.node_bin(), \
                mock.patch.object(chatnode, "wait_boot",
                                  return_value=("initializing", 1)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            chatnode.cmd_node(["up"])
        p = os.path.join(chatnode._dropin_dir(chatnode.UNIT),
                         "10-helm-posture.conf")
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            text = None
        return out.getvalue(), err.getvalue(), text

    def test_F1_a_manager_exported_bypass_is_written_empty(self):
        """helm-codex's F1 on 3b7a8f92ad9: the peer runs with neither
        loosening flag and declares none, so the mirror wrote nothing, and
        our unit inherits the manager's DREGG_ALLOW_UNAUDITED_PQ=1. `up`
        writes both loosening flags empty."""
        self.peer_runs(["PATH=/usr/bin", "DREGG_ADMIN_TOKEN=" + SECRET])
        _out, _err, text = self.up()
        self.assert_neutralised(text)
        self.assertIn("\nEnvironment=%s=\n" % UNAUDITED, text)

    def test_a_reader_failure_keeps_the_previous_drop_in(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple; every row asserts its control (the mirror `up` wrote grants both), then the drop-in still on disk and byte-identical
        """task/4204: `up` wrote the empty mirror on EVERY run, so a probe that
        failed mid a ~36 s daemon-reload overwrote the peer's good bypass with
        both flags empty — the refusal the node had cleared. A probe that fails
        or times out proves nothing and must not write its empty result; it
        keeps the previous drop-in byte-identical and says why. The peer
        declares both flags throughout and runs with both until the failure."""
        declared = {"Environment": "%s=1 %s=1" % (UNVERIFIED, UNAUDITED)}
        failures = (
            ("systemctl cannot be run",
             lambda: setattr(self, "answer", (None, ""))),
            ("show exits non-zero", lambda: setattr(self, "answer", (1, ""))),
            ("an answer the reader cannot parse",
             lambda: setattr(self, "answer", (0, "garbage\n"))),
            ("no such unit",
             lambda: self.props.update(LoadState="not-found", MainPID="0")),
            ("it is stopped", lambda: self.props.update(MainPID="0")),
            ("its process is another unit's",
             lambda: self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"],
                                    cgroup=_cgroup("app.slice",
                                                   "other.service"),
                                    **declared)),
            ("its environment cannot be read",
             lambda: self.peer_runs(raw=b"", **declared)),
        )
        self.assertTrue(failures)
        for label, fail in failures:
            with self.subTest(label):
                self.answer = None
                self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"],
                               **declared)
                _out, err, text = self.up()
                self.assertTrue(_dregg_runs_unaudited_pq(_effective(text)),
                                "the control: the running peer's bypass "
                                "is mirrored")
                before = text
                fail()
                _out, err, text = self.up()
                self.assertIsNotNone(text, "the drop-in was removed")
                self.assertEqual(text, before,
                                 "the probe wrote its empty result over the "
                                 "good mirror")
                self.assertIn("keeping the existing", _out)
                # the peer's real bypass is still set here, not blanked
                self.assertTrue(_dregg_runs_unaudited_pq(_effective(text)))

    def test_a_systemctl_timeout_is_an_explicit_unknown_not_an_empty_write(self):  # noqa: VACUOUS_ASSERTION — the timeout case asserts rc None; the success case asserts rc 0 with a captured body, neither reaching a real systemctl
        """task/4204's reload timed past the probe's 30 s bound: _systemctl
        reports that as rc None, the explicit "unknown" — never rc 0 (success)
        and never an empty posture another layer could write as a real
        reading. `up` writes its empty result on any non-success probe, so a
        timeout must read as "keep", never "write empty"."""
        with mock.patch("helm.chatnode.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("systemctl", 1)):
            rc, out = chatnode._systemctl("show", "x", timeout=1)
        self.assertIsNone(rc)
        self.assertIn("unavailable", out)
        with mock.patch("helm.chatnode.subprocess.run",
                        return_value=mock.Mock(stdout="Loaded=loaded\n",
                                               stderr="", returncode=0)):
            rc, out = chatnode._systemctl("show", "x", timeout=30)
        self.assertEqual(rc, 0)
        self.assertIn("Loaded=loaded", out)

    def test_the_mirror_follows_the_running_process_not_its_unit(self):
        """An env-file revocation: the unit declares both bypasses (its
        Environment= says 1), and its process runs without the PQ bypass and
        with DREGG_REQUIRE_LEAN=1, as an EnvironmentFile=, UnsetEnvironment=
        or an ExecStart= wrapper leaves it. `up` writes what the process
        runs with."""
        self.peer_runs([UNVERIFIED + "=1", REQUIRE_LEAN + "=1"],
                       Environment="%s=1 %s=1" % (UNVERIFIED, UNAUDITED))
        _out, _err, text = self.up()
        env = _effective(text, {UNAUDITED: "1"})
        self.assertEqual(env[UNAUDITED], "",
                         "mirrored a bypass the running peer does not have")
        self.assertEqual(env.get(REQUIRE_LEAN), "1",
                         "dropped a tightening the running peer has")
        self.assertEqual(env[UNVERIFIED], "1")
        self.assertFalse(_dregg_runs_unaudited_pq(env))

    def test_a_peer_running_with_both_bypasses_mirrors_both(self):
        """The control: the peer declares both and runs with both, and our
        node runs with both, as the team node does (the two posture names
        a running team node was measured with)."""
        self.peer_runs([UNVERIFIED + "=1", UNAUDITED + "=1"],
                       Environment="%s=1 %s=1" % (UNVERIFIED, UNAUDITED))
        out, _err, text = self.up()
        env = _effective(text)
        self.assertTrue(_dregg_runs_unverified(env))
        self.assertTrue(_dregg_runs_unaudited_pq(env))
        self.assertIn("mirrored 2 posture flags from", out)

    def test_no_secret_the_peer_runs_with_reaches_any_output(self):  # noqa: VACUOUS_ASSERTION — the loop is over a non-empty literal tuple of peers; every row asserts that none of the non-empty literal leaks reaches stdout, stderr or the drop-in
        """The peer's environment holds dregg's tokens and whatever else it
        was started with. Whether `up` mirrors, finds a posture value it
        cannot write, or finds a process that is not the peer's, none of it
        reaches its output, its reasons or the drop-in."""
        secrets = ["DREGG_ADMIN_TOKEN=" + SECRET, "DISCORD_TOKEN=" + SECRET]
        peers = (("mirrored", [UNAUDITED + "=1"] + secrets, None),
                 ("unwritable", [UNAUDITED + "=1\t"] + secrets, None),
                 ("not the peer", [UNAUDITED + "=1"] + secrets,
                  _cgroup("app.slice", "other.service")))
        self.assertTrue(peers)
        for label, env, cgroup in peers:
            with self.subTest(label):
                self.peer_runs(env, cgroup=cgroup)
                out, err, text = self.up()
                for leak in (SECRET, "DREGG_ADMIN_TOKEN", "DISCORD_TOKEN"):
                    self.assertNotIn(leak, out + err + (text or ""))

    def test_ups_daemon_reload_passes_its_own_reload_timeout(self):
        """task/4204: the measured cutover timed daemon-reload at 29-36 s over
        ~7k units, past the probe's 30 s bound, which is what failed the probe
        mid-reload. `up`'s daemon-reload passes its own 120 s bound, not the
        probe's 30 s, so the probe survives the reload that preceded it."""
        self.peer_runs([UNVERIFIED + "=1"])
        with mock.patch.object(chatnode, "_systemctl", self.systemctl):
            self.up()
        self.assertTrue(self.reload_timeouts)
        self.assertEqual(self.reload_timeouts[0], chatnode.RELOAD_TIMEOUT_S)
        self.assertGreaterEqual(chatnode.RELOAD_TIMEOUT_S, 120,
                                "the probe-bound reload timeout is not sized "
                                "for the measured reload")


if __name__ == "__main__":
    unittest.main()
