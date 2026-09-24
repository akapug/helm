#!/usr/bin/env python3
"""Open flags refuse missing protection instead of silently becoming zero."""
import ast
import errno
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock

from helm import eventledger, openflags, session
from helm.configs import _io as config_io
from helm import todos


def _silent_fallbacks(source, label):
    found = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call) or len(node.args) < 3:
            continue
        fn = node.func
        if not isinstance(fn, ast.Name) or fn.id != "getattr":
            continue
        owner, name, default = node.args[:3]
        if (isinstance(owner, ast.Name) and owner.id == "os"
                and isinstance(name, ast.Constant)
                and isinstance(name.value, str) and name.value.startswith("O_")
                and isinstance(default, ast.Constant) and default.value == 0):
            found.append("%s:%d %s" % (label, node.lineno, name.value))
    return found


class MissingOpenCapabilitiesRefuse(unittest.TestCase):
    def test_each_required_flag_refuses_when_the_runtime_lacks_it(self):  # noqa: VACUOUS_ASSERTION — each subtest first proves the same flag is present in the returned mask before removing it and asserting refusal
        for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
            present = types.SimpleNamespace(O_RDONLY=os.O_RDONLY,
                                            **{name: 1 << 20})
            with self.subTest(flag=name), mock.patch("helm.openflags.os", present):
                self.assertTrue(openflags.flags(os.O_RDONLY, name) & (1 << 20),
                                "positive control: the required flag was ignored")
            missing = types.SimpleNamespace(O_RDONLY=os.O_RDONLY)
            with mock.patch("helm.openflags.os", missing):
                with self.assertRaisesRegex(OSError, name):
                    openflags.flags(os.O_RDONLY, name)

    def test_all_required_flags_are_reported(self):
        fake = types.SimpleNamespace(O_RDONLY=os.O_RDONLY)
        with mock.patch("helm.openflags.os", fake):
            with self.assertRaises(OSError) as raised:
                openflags.flags(os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW")
        self.assertIn("O_DIRECTORY", str(raised.exception))
        self.assertIn("O_NOFOLLOW", str(raised.exception))

    def test_cloexec_is_the_only_deliberate_zero_fallback(self):  # noqa: VACUOUS_ASSERTION — the positive half first proves O_CLOEXEC is added before the missing-runtime fallback is asserted
        present = types.SimpleNamespace(O_RDONLY=os.O_RDONLY, O_CLOEXEC=1 << 20)
        with mock.patch("helm.openflags.os", present):
            self.assertTrue(openflags.flags(os.O_RDONLY, cloexec=True) & (1 << 20),
                            "positive control: O_CLOEXEC was not added")
        missing = types.SimpleNamespace(O_RDONLY=os.O_RDONLY)
        with mock.patch("helm.openflags.os", missing):
            self.assertEqual(openflags.flags(os.O_RDONLY, cloexec=True),
                             os.O_RDONLY)


class ProductionDoorsRefuseWithoutProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-openflags-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)

    def test_eventledger_read_refuses_without_nofollow(self):
        path = os.path.join(self.tmp, "events.jsonl")
        # A COMPLETE EVENT ROW NEEDS A NON-EMPTY `id`, and this fixture did not
        # have one. `checked_rows` drops any row that is not "an object with a
        # non-empty id", so `{"v":1}` was never a row production would return
        # and the CONTROL below — the one proving the reader works before the
        # mock strips O_NOFOLLOW — asserted a value the ledger cannot produce.
        # Measured on trunk as well as here: both return ([], None), so this was
        # the arm inventing a shape, never a regression in the lane.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"id":"e1","v":1}\n')
        self.assertEqual(eventledger.checked_events(path),
                         ([{"id": "e1", "v": 1}], None))
        fake = types.SimpleNamespace()
        with mock.patch("helm.openflags.os", fake):
            rows, unavailable = eventledger.checked_events(path)
        self.assertEqual(rows, [])
        self.assertIn("O_NOFOLLOW", unavailable)

    def test_directory_fsync_refuses_without_directory_flag(self):  # noqa: VACUOUS_ASSERTION — the real production fsync succeeds first on the same directory, then the missing capability must refuse
        eventledger._fsync_dir(self.tmp)
        fake = types.SimpleNamespace(O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 1 << 20))
        with mock.patch("helm.openflags.os", fake):
            with self.assertRaisesRegex(OSError, "O_DIRECTORY"):
                eventledger._fsync_dir(self.tmp)

    def test_config_snapshot_refuses_without_nofollow(self):
        path = os.path.join(self.tmp, "config")
        with open(path, "wb") as fh:
            fh.write(b"value")
        dfd = os.open(self.tmp, os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        self.assertEqual(config_io._snapshot_at(dfd, "config")["data"], b"value")
        fake = types.SimpleNamespace(O_DIRECTORY=getattr(os, "O_DIRECTORY", 1 << 20))
        with mock.patch("helm.openflags.os", fake):
            with self.assertRaises(config_io._ConfigIOError) as raised:
                config_io._snapshot_at(dfd, "config")
        self.assertEqual(raised.exception.code, "open")

    def test_todo_member_refuses_without_nofollow(self):  # noqa: VACUOUS_ASSERTION — the same production door opens the planted member before the missing capability makes it return None
        path = os.path.join(self.tmp, "member")
        with open(path, "wb") as fh:
            fh.write(b"value")
        dfd = os.open(self.tmp, os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        fd = todos._open_member(dfd, "member")
        self.assertIsNotNone(fd, "positive control: the real door did not open")
        os.close(fd)
        with mock.patch("helm.openflags.os", types.SimpleNamespace()):
            self.assertIsNone(todos._open_member(dfd, "member"))

    def test_session_record_refuses_without_nonblock(self):
        sessions = os.path.join(self.tmp, "sessions")
        os.mkdir(sessions, 0o700)
        pid, start = 123, "456"
        sid = "00000000-0000-4000-8000-000000000001"
        path = os.path.join(sessions, "%d.json" % pid)
        with open(path, "w", encoding="utf-8") as fh:
            __import__("json").dump(
                {"pid": pid, "procStart": start, "sessionId": sid}, fh)
        os.chmod(path, 0o600)
        self.assertEqual(session._read_session_record(
            self.tmp, pid, os.getuid(), start), (sid, "record-ok"))
        fake = types.SimpleNamespace(
            O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 1 << 20))
        with mock.patch("helm.openflags.os", fake):
            self.assertEqual(session._read_session_record(
                self.tmp, pid, os.getuid(), start),
                (None, "record-unreadable"))


class DurableMakedirsCapabilities(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="helm-durable-openflags-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_missing_flags_report_capability_not_hostile_path(self):
        target = os.path.join(self.tmp, "normal", "nested")
        todos._makedirs_durable(target)
        self.assertTrue(os.path.isdir(target),
                        "positive control: real nested creation failed")
        before = os.stat(target)
        todos._makedirs_durable(target)
        self.assertEqual(os.stat(target).st_ino, before.st_ino)
        for name in ("O_DIRECTORY", "O_NOFOLLOW"):
            fake = types.SimpleNamespace(**{
                n: getattr(os, n) for n in ("O_DIRECTORY", "O_NOFOLLOW")
                if n != name})
            with self.subTest(flag=name), mock.patch("helm.openflags.os", fake):
                with self.assertRaises(OSError) as raised:
                    todos._makedirs_durable(target)
                self.assertEqual(raised.exception.errno, errno.ENOTSUP)
                self.assertEqual(raised.exception.strerror,
                                 "required open flag unavailable: " + name)

    def test_no_components_need_no_protection_flags(self):
        with mock.patch.object(openflags, "flags",
                               wraps=openflags.flags) as resolve:
            todos._makedirs_durable(self.tmp)
        resolve.assert_called_once_with(os.O_RDONLY, "O_DIRECTORY", "O_NOFOLLOW")
        with mock.patch("helm.openflags.os", types.SimpleNamespace()), \
                mock.patch.object(openflags, "flags",
                                  wraps=openflags.flags) as resolve:
            self.assertIsNone(todos._makedirs_durable(os.sep))
        resolve.assert_not_called()


class NoOpenProtectionSilentlyDisappears(unittest.TestCase):
    def test_production_has_no_getattr_O_flag_defaulting_to_zero(self):  # noqa: VACUOUS_ASSERTION — the detector first finds a planted default-zero control before asserting the production census is empty
        control = _silent_fallbacks(
            'flags = getattr(os, "O_NOFOLLOW", 0)\n', "control.py")
        self.assertEqual(control, ["control.py:1 O_NOFOLLOW"],
                         "the census cannot detect the defect it claims to ban")
        root = pathlib.Path(__file__).resolve().parents[1] / "helm"
        found = []
        for path in root.rglob("*.py"):
            found.extend(_silent_fallbacks(
                path.read_text(encoding="utf-8"), str(path.relative_to(root.parent))))
        source = (root / "openflags.py").read_text(encoding="utf-8")
        cloexec_line = next(
            n for n, line in enumerate(source.splitlines(), 1)
            if line.strip() == 'value |= getattr(os, "O_CLOEXEC", 0)')
        expected = ["helm/openflags.py:%d O_CLOEXEC" % cloexec_line]
        self.assertEqual(found, expected,
                         "silent open-flag fallbacks changed:\n" +
                         "\n".join(found))


if __name__ == "__main__":
    unittest.main()
