"""Who holds the proxy port — and what helm is allowed to SAY about it.

The defect these pin (measured live 2026-07-29): `_up` refused a pre-bound port
with "a listener that is not <seat>'s proxy … someone else's socket", a
conclusion drawn from `_port_open`, a bool that knows only THAT something
listens. The listener was the seat's OWN healthy proxy, unrecorded because its
pidfile named a dead pid. The refusal was also a closed loop — `seat spawn`
sent the operator to `seat up`, which hit the same refusal — so a healthy proxy
was declared unrecoverable by a remedy that could never succeed.

Hermetic: every /proc here is a synthetic tree under the test's own tmp dir,
every pid is invented, and no proxy, socket or process is ever touched.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests import _tmphome  # noqa: E402 — relative import broke discovery

_tmphome.home()

from helm import seat                                          # noqa: E402
# the MODULE, never the class: binding `SeatTest` into this namespace would
# make unittest collect and re-run all 60 of its tests here.
from tests import test_seat as _seatfx                        # noqa: E402

_TCP_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
               "tm->when retrnsmt   uid  timeout inode")

# The keys this suite SETS, and therefore the keys it must restore. Named as a
# tuple rather than popped inline because that is the shape every sibling suite
# uses, and `tests/test_env_hygiene.py` reads exactly this shape to prove the
# file cleans up after itself.
ENV_KEYS = ("HELM_PROC",)


class _Proc:
    """A synthetic /proc: net/tcp, net/tcp6, and per-pid cmdline/cwd/fd."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "net"), exist_ok=True)
        self.rows = {"tcp": [], "tcp6": []}
        self._flush()

    def socket_row(self, port, inode, state="0A", v6=False):
        table = "tcp6" if v6 else "tcp"
        laddr = "0" * 31 + "1" if v6 else "0100007F"
        self.rows[table].append(
            "%4d: %s:%04X 00000000:0000 %s 00000000:00000000 00:00000000 "
            "00000000  1000        0 %d 2 0000000000000000 100 0 0 10 0"
            % (len(self.rows[table]), laddr, port, state, inode))
        self._flush()

    def _flush(self):
        for name, rows in self.rows.items():
            with open(os.path.join(self.root, "net", name), "w") as f:
                f.write("\n".join([_TCP_HEADER] + rows) + "\n")

    def process(self, pid, argv=None, inodes=(), cwd=None, fd_mode=None):
        d = os.path.join(self.root, str(pid))
        fd = os.path.join(d, "fd")
        os.makedirs(fd, exist_ok=True)
        if argv is not None:
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        if cwd is not None:
            os.symlink(cwd, os.path.join(d, "cwd"))
        for i, ino in enumerate(inodes):
            os.symlink("socket:[%d]" % ino, os.path.join(fd, str(i + 3)))
        if fd_mode is not None:
            os.chmod(fd, fd_mode)
        return d


class PortListenerTest(unittest.TestCase):
    """`_port_listener` — the /proc resolution, on a fake tree."""

    PORT = 8360

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-portowner-")
        self.proc = _Proc(os.path.join(self.tmp, "proc"))
        self.root = self.proc.root
        # HELM_PROC is SET by a test below and this tmp tree is rmtree'd in
        # tearDown, so without this the variable outlives the directory it
        # names: every later reader in the process would resolve a DELETED
        # proc root and find no processes — an absence that reads exactly like
        # a true negative. Five sibling suites already pop/restore this key;
        # this file set it without joining the convention.
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.ident = mock.patch.object(seat, "_pid_identity",
                                       return_value="proc:4242")
        self.ident.start()
        self.addCleanup(self.ident.stop)

    def tearDown(self):
        for key, value in self.env_prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v4_listener_resolves_to_its_pid_argv_and_config(self):
        self.proc.socket_row(self.PORT, 700100)
        self.proc.process(4242, argv=["/opt/cli-proxy-api", "-config",
                                      "/seats/ds4pro/config.yaml"],
                          inodes=(700100,))
        who = seat._port_listener(self.PORT, self.root)
        self.assertEqual(who["pid"], 4242)
        self.assertEqual(who["config"], "/seats/ds4pro/config.yaml")
        self.assertEqual(who["identity"], "proc:4242")
        self.assertEqual(who["argv"][0], "/opt/cli-proxy-api")

    def test_a_listener_that_only_appears_in_tcp6_is_still_found(self):
        # a dual-stack bind lands ONLY in net/tcp6; a reader that consults
        # net/tcp alone is blind to it and reports the port unowned.
        self.proc.socket_row(self.PORT, 700200, v6=True)
        self.proc.process(5151, argv=["/opt/cli-proxy-api", "-config",
                                      "/seats/kimi/config.yaml"],
                          inodes=(700200,))
        who = seat._port_listener(self.PORT, self.root)
        self.assertIsNotNone(who, "a tcp6-only listener was not resolved")
        self.assertEqual(who["pid"], 5151)

    def test_an_established_row_on_the_same_local_port_is_never_collected(self):
        # the proxy's own accepted connections carry the SAME local port in
        # state 01. Collecting those inodes would let the scan name a CLIENT
        # as the owner of the bind.
        self.proc.socket_row(self.PORT, 700300)                  # LISTEN
        self.proc.socket_row(self.PORT, 700301, state="01")      # ESTABLISHED
        self.assertEqual(seat._listen_inodes(self.PORT, self.root), [700300])

    def test_a_different_ports_listener_is_not_collected(self):
        self.proc.socket_row(self.PORT + 1, 700400)
        self.assertEqual(seat._listen_inodes(self.PORT, self.root), [])
        self.assertIsNone(seat._port_listener(self.PORT, self.root))

    @unittest.skipIf(os.getuid() == 0, "root reads every fd table")
    def test_an_unreadable_fd_table_is_unknown_never_a_wrong_owner(self):
        # THE branch this whole fix exists for, at the lowest layer: a
        # PermissionError on another user's fd dir is not evidence about the
        # socket, and it must not (a) escape as an exception or (b) let the
        # scan settle on the decoy that happens to be readable.
        holder = self.proc.process(6001, argv=["/opt/cli-proxy-api"],
                                   inodes=(700500,))
        self.addCleanup(os.chmod, os.path.join(holder, "fd"), 0o700)
        os.chmod(os.path.join(holder, "fd"), 0o000)
        self.proc.process(6002, argv=["/bin/other"], inodes=(999999,))
        self.assertIsNone(seat._pid_holding_socket([700500], self.root))

    def test_a_listener_whose_cmdline_cannot_be_read_is_unidentified(self):
        # exited mid-scan: the fd link is still in the dirents, /cmdline is
        # gone. Half an identification is not an identification.
        self.proc.socket_row(self.PORT, 700600)
        self.proc.process(7001, argv=None, inodes=(700600,))
        self.assertEqual(seat._pid_holding_socket([700600], self.root), 7001)
        self.assertIsNone(seat._port_listener(self.PORT, self.root))

    def test_exact_listener_census_keeps_an_unreadable_owner_as_unknown(self):
        self.proc.socket_row(self.PORT, 700601)
        self.proc.process(7002, argv=None, inodes=(700601,))
        self.assertEqual(seat._port_listeners(self.PORT, self.root, exact=True),
                         [{"pid": 7002, "identity": None, "argv": None,
                           "config": None}])

    def test_exact_census_keeps_an_inode_with_no_readable_holder_unknown(self):
        self.proc.socket_row(self.PORT, 700602)
        self.proc.socket_row(self.PORT, 700603)
        self.proc.process(7003, argv=["cli-proxy-api"], inodes=(700602,))
        self.assertEqual(seat._port_listeners(self.PORT, self.root, exact=True),
                         [{"pid": None, "identity": None, "argv": None,
                           "config": None},
                          {"pid": 7003, "identity": "proc:4242",
                           "argv": ["cli-proxy-api"], "config": None}])

    def test_a_relative_config_is_anchored_to_the_processes_own_cwd(self):
        # resolving it against the CALLER's cwd answers a question nobody
        # asked and can invent a match (or miss ours).
        theirs = os.path.join(self.tmp, "their-cwd")
        os.makedirs(theirs)
        self.proc.socket_row(self.PORT, 700700)
        self.proc.process(8001, argv=["cli-proxy-api", "-config", "config.yaml"],
                          inodes=(700700,), cwd=theirs)
        who = seat._port_listener(self.PORT, self.root)
        self.assertEqual(who["config"], os.path.join(theirs, "config.yaml"))

    def test_a_relative_config_with_no_readable_cwd_is_no_claim(self):
        self.proc.socket_row(self.PORT, 700800)
        self.proc.process(8002, argv=["cli-proxy-api", "-config", "config.yaml"],
                          inodes=(700800,))
        who = seat._port_listener(self.PORT, self.root)
        self.assertIsNone(who["config"])
        self.assertEqual(who["pid"], 8002)

    def test_the_equals_form_of_the_config_flag_is_read(self):
        self.proc.socket_row(self.PORT, 700900)
        self.proc.process(8003, argv=["cli-proxy-api",
                                      "--config=/seats/grok/config.yaml"],
                          inodes=(700900,))
        who = seat._port_listener(self.PORT, self.root)
        self.assertEqual(who["config"], "/seats/grok/config.yaml")

    def test_a_process_with_no_config_flag_is_identified_with_config_none(self):
        self.proc.socket_row(self.PORT, 701000)
        self.proc.process(8004, argv=["/usr/bin/python3", "-m", "http.server"],
                          inodes=(701000,))
        who = seat._port_listener(self.PORT, self.root)
        self.assertEqual(who["pid"], 8004)
        self.assertIsNone(who["config"])


class UpPortDecisionTest(unittest.TestCase):
    """`_up`'s pre-bound-port branch: adopt ours, name a stranger, say UNKNOWN.

    Borrows SeatTest's hermetic fixtures (tmp HELM_HOME, fake codex homes).
    """

    setUp = _seatfx.SeatTest.setUp
    tearDown = _seatfx.SeatTest.tearDown
    _plant = _seatfx.SeatTest._plant
    _add = _seatfx.SeatTest._add

    def _seat(self, name="codex"):
        """A minted seat, family or INSTANCE. The two layouts differ — a family
        seat's proxy home is seats/<family>/, an instance's is
        seats/<family>/instances/<seat>/ — and only `_proxy_home` knows which."""
        self._plant("home-a")
        self.assertEqual(self._add()[0], 0)
        if name != "codex":
            seat._mint_instance_proxy("codex", name)
        self.seat_name = name
        self.cfgd = seat._proxy_home("codex", name)
        self.pidfile = os.path.join(self.cfgd, "proxy.pid")
        return os.path.join(self.cfgd, "config.yaml")

    def _up(self, listener, name=None):
        """Run `_up` with the port held and `listener` as the resolved owner."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch.object(seat, "_port_listener",
                                  return_value=listener), \
                mock.patch.object(seat, "_proxy_bin",
                                  return_value="/opt/cli-proxy-api"), \
                mock.patch.object(seat.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat._up("codex", seat=name or self.seat_name)
        return rc, err.getvalue(), popen

    # -- branch 1: it is OURS -> adopt -------------------------------------
    def test_the_seats_own_unrecorded_proxy_is_adopted_not_refused(self):
        # THE measured symptom: a stale pidfile naming a DEAD pid, while the
        # seat's own healthy proxy holds the port. Old code: "someone else's
        # socket", forever. New: adopt it.
        cfg = self._seat()
        seat._write_private(self.pidfile, "1791124 proc:stale\n")
        rc, err, popen = self._up({"pid": 756429, "identity": "proc:9931",
                                   "argv": ["/opt/cli-proxy-api", "-config", cfg],
                                   "config": cfg})
        self.assertEqual(rc, 0, err)
        with open(self.pidfile) as f:
            self.assertEqual(f.read(), "756429 proc:9931\n")
        self.assertIn("ADOPTED", err)
        self.assertIn("756429", err)
        popen.assert_not_called()      # never spawn a second proxy over ours

    def test_an_adopted_record_authenticates_and_still_defeats_pid_reuse(self):
        # adoption is only worth anything if the record it writes is the one
        # `_running_pid` accepts — otherwise the seat stays "proxy down".
        cfg = self._seat()
        rc, err, _ = self._up({"pid": 756429, "identity": "proc:9931",
                               "argv": ["/opt/cli-proxy-api", "-config", cfg],
                               "config": cfg})
        self.assertEqual(rc, 0, err)
        with mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:9931"):
            self.assertEqual(seat._running_pid("codex", "codex"), 756429)
        with mock.patch.object(seat, "_pid_alive", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:1"):
            self.assertIsNone(seat._running_pid("codex", "codex"))

    # -- branch 1, the OTHER layout: an INSTANCE seat ----------------------
    def test_an_instance_seats_own_proxy_is_adopted_from_its_nested_home(self):
        # SECOND confirmed instance of the defect (codex-2 / 8319, 2026-07-29):
        # same shape, different LAYOUT. A family seat's config is
        # seats/<family>/config.yaml; an instance's is
        # seats/<family>/instances/<seat>/config.yaml. Any expected-path
        # RECONSTRUCTION that assumes the flat form silently fails to match for
        # every instance seat and falls through to a refusal — the fix would
        # look like it worked while half the fleet stayed broken. The expected
        # path must come from `_proxy_home`, and this is what proves it does.
        cfg = self._seat("codex-2")
        self.assertIn(os.path.join("instances", "codex-2"), cfg)
        seat._write_private(self.pidfile, "1791124 proc:stale\n")
        rc, err, popen = self._up({"pid": 677249, "identity": "proc:9932",
                                   "argv": ["/opt/cli-proxy-api", "-config", cfg],
                                   "config": cfg})
        self.assertEqual(rc, 0, err)
        with open(self.pidfile) as f:
            self.assertEqual(f.read(), "677249 proc:9932\n")
        self.assertIn("ADOPTED", err)
        popen.assert_not_called()

    def test_a_sibling_instances_proxy_is_a_stranger_to_the_family_seat(self):
        # the inverse of the layout trap: the two homes must not collapse into
        # each other. codex-2's proxy on the family seat's port is FOREIGN, and
        # adopting it would hand a seat a proxy serving another seat's creds.
        self._seat("codex")
        theirs = os.path.join(seat._proxy_home("codex", "codex-2"), "config.yaml")
        rc, err, _ = self._up({"pid": 677249, "identity": "proc:9932",
                               "argv": ["/opt/cli-proxy-api", "-config", theirs],
                               "config": theirs})
        self.assertEqual(rc, 1)
        self.assertIn("NOT codex's proxy", err)
        self.assertIn("677249", err)
        self.assertFalse(os.path.exists(self.pidfile))

    # -- branch 2: it is SOMEONE ELSE'S -> refuse, and NAME them -----------
    def test_a_foreign_listener_is_refused_and_its_owner_is_named(self):
        self._seat()
        theirs = "/seats/ds4pro/config.yaml"
        rc, err, popen = self._up({"pid": 99001, "identity": "proc:7",
                                   "argv": ["/opt/cli-proxy-api", "-config",
                                            theirs],
                                   "config": theirs})
        self.assertEqual(rc, 1)
        self.assertIn("99001", err)          # the ACTUAL owner, not "someone"
        self.assertIn(theirs, err)
        self.assertIn("NOT codex's proxy", err)
        self.assertFalse(os.path.exists(self.pidfile))
        popen.assert_not_called()

    def test_an_unrelated_listener_with_no_config_is_refused_by_argv(self):
        self._seat()
        rc, err, _ = self._up({"pid": 99002, "identity": "proc:8",
                               "argv": ["/usr/bin/python3", "-m", "http.server"],
                               "config": None})
        self.assertEqual(rc, 1)
        self.assertIn("99002", err)
        self.assertIn("http.server", err)
        self.assertFalse(os.path.exists(self.pidfile))

    # -- branch 3: UNKNOWN -> refuse, and say so --------------------------
    def test_an_unidentifiable_listener_is_refused_as_unknown_not_as_foreign(self):
        # THE point of the fix. "I could not read /proc" must never be
        # rendered as "this belongs to someone else".
        self._seat()
        rc, err, popen = self._up(None)
        self.assertEqual(rc, 1)
        self.assertIn("COULD NOT IDENTIFY", err)
        self.assertIn("NOT a claim that it is foreign", err)
        self.assertNotIn("NOT codex's proxy", err)
        self.assertNotIn("another process's socket", err)
        self.assertFalse(os.path.exists(self.pidfile))
        popen.assert_not_called()

    def test_ours_but_unverifiable_birth_identity_is_refused_unrecorded(self):
        # an identity we cannot capture cannot be authenticated later, so a
        # pidfile carrying it would be a record helm can never act on.
        cfg = self._seat()
        rc, err, _ = self._up({"pid": 756429, "identity": None,
                               "argv": ["/opt/cli-proxy-api", "-config", cfg],
                               "config": cfg})
        self.assertEqual(rc, 1)
        self.assertIn("could not capture its birth identity", err)
        self.assertNotIn("NOT codex's proxy", err)
        self.assertFalse(os.path.exists(self.pidfile))

    # -- the closed loop, end to end ---------------------------------------
    def _closed_loop(self, name):
        """`helm seat up <name>` on the measured symptom, through the REAL
        /proc reader (no `_port_listener` mock): stale pidfile naming a dead
        pid, the seat's own proxy on the port. `helm seat spawn` sent the
        operator to this command and it refused, forever."""
        cfg = self._seat(name)
        seat._write_private(self.pidfile, "1791124 proc:stale\n")
        proc = _Proc(os.path.join(self.tmp, "proc"))
        proc.socket_row(seat._instance_port("codex", name), 701100)
        proc.process(4242, argv=["/opt/cli-proxy-api", "-config", cfg],
                     inodes=(701100,))
        os.environ["HELM_PROC"] = proc.root
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(seat, "_port_open", return_value=True), \
                mock.patch.object(seat, "_pid_identity", return_value="proc:77"), \
                mock.patch.object(seat, "_pid_alive",
                                  lambda pid: pid == 4242), \
                mock.patch.object(seat, "_proxy_bin",
                                  return_value="/opt/cli-proxy-api"), \
                mock.patch.object(seat.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(["up", name])
            self.assertEqual(rc, 0, err.getvalue())
            # and the seat now reads UP: the record authenticates against the
            # live process, which is what `helm seat list` renders off.
            self.assertEqual(seat._running_pid("codex", name), 4242)
        with open(self.pidfile) as f:
            self.assertEqual(f.read(), "4242 proc:77\n")
        popen.assert_not_called()

    def test_helm_seat_up_recovers_a_family_seat_ds4pro_shape(self):
        self._closed_loop("codex")

    def test_helm_seat_up_recovers_an_instance_seat_codex2_shape(self):
        # the second confirmed instance, end to end: `helm seat up codex-2`
        # routes through `_split_seat` to the instance's own nested proxy home.
        self._closed_loop("codex-2")


if __name__ == "__main__":
    unittest.main()
