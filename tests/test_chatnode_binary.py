#!/usr/bin/env python3
"""helm chat node — `up` remembers the binary it installed.

WHY THIS EXISTS: a chat node moved to a rebased chain is brought up with
HELM_CHAT_NODE_BIN naming the new binary. Nothing recorded that choice. The
unit is re-rendered from the default resolution on every `up`, and that
resolution was HELM_CHAT_NODE_BIN, else `dregg-cave-node` on PATH, else
~/.local/bin/dregg-cave-node. So the first bare `helm chat node up` after the
move put the OLD binary back in the unit, against the NEW chain's data. A bare
`up` is exactly what status and doctor print as the fix, and seats follow
printed remedies. The old binary cannot simply be overwritten either: another
node (the team cave) runs it against its own store.

The contract pinned here: `up` records the binary it installs (absolute path
and sha256) in the 0600 chat-node state; the default resolution prefers that
record; a record that cannot be honoured (the file gone, its bytes changed,
the state unreadable) REFUSES and names the fix, and never falls back; with
nothing recorded, today's fallback is unchanged.
"""
import contextlib
import hashlib
import io
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import cell, chat, chatnode, doctor  # noqa: E402

FIX = "HELM_CHAT_NODE_BIN=<binary> helm chat node up"


class BinaryBase(unittest.TestCase):
    """HOME, HELM_HOME and PATH all in tmp: the fallback reads PATH and
    ~/.local/bin, and a build host may well have a dregg-cave-node on both."""

    KEYS = ("HOME", "HELM_HOME", "PATH", "HELM_CHAT_NODE_BIN",
            "MELD_CHAT_NODE_BIN")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-nodebin-")
        self.prior = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = self.tmp
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.on_path = os.path.join(self.tmp, "path")
        os.makedirs(self.on_path)
        os.environ["PATH"] = self.on_path

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def binary(self, where, body="rebased"):
        os.makedirs(os.path.dirname(where), exist_ok=True)
        with open(where, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n# %s\nexit 0\n" % body)
        os.chmod(where, 0o755)
        return where

    def rebased(self):
        return self.binary(os.path.join(self.tmp, "bin", "dregg-node-rebased"))

    def old_on_path(self):
        """The binary every fallback finds: the team cave's."""
        return self.binary(os.path.join(self.on_path, "dregg-cave-node"), "old")

    def up(self):
        """`helm chat node up`, with systemd and the boot wait stood in: the
        node reads as still initializing, so `up` stops before provisioning,
        after it wrote the unit."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl", return_value=(0, "")), \
                mock.patch.object(chatnode, "wait_boot",
                                  return_value=("initializing", 1)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chatnode.cmd_node(["up"])
        return rc, out.getvalue(), err.getvalue()

    def unit_execstart(self):
        with open(chatnode.unit_path(), encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines()
                    if ln.startswith("ExecStart=")][0]

    def cut_over(self):
        """The cutover: `up` with the variable naming the rebased binary, and
        the old one still on PATH. Returns the rebased binary's path."""
        b = self.rebased()
        self.old_on_path()
        os.environ["HELM_CHAT_NODE_BIN"] = b
        self.up()
        del os.environ["HELM_CHAT_NODE_BIN"]
        # THE POSITIVE CONTROL for every refusal below: before anything is
        # broken, the bare resolution names the recorded binary, so a None
        # after the break is the break's doing.
        self.assertEqual(chatnode.bin_path(), b)
        return b


class UpRecordsTheBinary(BinaryBase):
    def test_up_with_the_variable_records_the_binary_and_its_sha256(self):
        b = self.rebased()
        os.environ["HELM_CHAT_NODE_BIN"] = b
        rc, out, _err = self.up()
        self.assertEqual(rc, 1)                     # initializing, not a fault
        with open(b, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(chatnode.state().get("binary"),
                         {"path": b, "sha256": sha})
        self.assertEqual(stat.S_IMODE(os.stat(chatnode.state_path()).st_mode),
                         0o600)
        self.assertEqual(self.unit_execstart().split()[0], "ExecStart=" + b)
        self.assertIn("recorded", out)

    def test_a_later_bare_resolution_returns_the_recorded_binary(self):
        b = self.cut_over()
        self.assertEqual(chatnode.bin_path(), b)
        self.assertEqual(chatnode.bin_resolution()["source"], "recorded")
        # and a bare `up` keeps it in the unit, never the PATH's old binary
        self.up()
        self.assertEqual(self.unit_execstart().split()[0], "ExecStart=" + b)

    def test_the_variable_still_wins_over_the_record(self):
        self.cut_over()
        other = self.binary(os.path.join(self.tmp, "bin", "next"), "next")
        os.environ["HELM_CHAT_NODE_BIN"] = other
        self.assertEqual(chatnode.bin_resolution(),
                         {"path": other, "source": "env", "reason": None})


class ARecordThatCannotBeHonouredRefuses(BinaryBase):
    def assert_refused(self, *words):
        res = chatnode.bin_resolution()
        self.assertIsNone(res["path"], res)
        self.assertIsNone(chatnode.bin_path())
        self.assertEqual(res["source"], "recorded")
        for w in words + (FIX,):
            self.assertIn(w, res["reason"])
        return res

    def test_a_missing_recorded_binary_refuses_and_never_falls_back(self):
        b = self.cut_over()
        before = self.unit_execstart()
        os.unlink(b)
        self.assert_refused(b, "MISSING")
        rc, _out, err = self.up()
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", err)
        self.assertIn(FIX, err)
        self.assertEqual(self.unit_execstart(), before,
                         "a refused `up` must not re-render the unit")

    def test_a_changed_sha256_refuses(self):
        b = self.cut_over()
        self.binary(b, "a different build of the same name")
        self.assert_refused(b, "CHANGED")
        rc, _out, err = self.up()
        self.assertEqual(rc, 1)
        self.assertIn("CHANGED", err)

    def test_an_unreadable_state_file_refuses_rather_than_read_as_nothing(self):
        self.cut_over()
        with open(chatnode.state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assert_refused(chatnode.state_path())

    def test_a_malformed_record_refuses(self):
        self.cut_over()
        st = chatnode.state()
        st["binary"] = {"path": "relative/dregg-node", "sha256": "x"}
        chatnode.write_state(st)
        self.assert_refused("malformed")


class NothingRecordedKeepsTodaysFallback(BinaryBase):
    """The control: with no record, resolution is exactly what it was."""

    def test_no_record_resolves_dregg_cave_node_on_path(self):
        old = self.old_on_path()
        self.assertEqual(chatnode.bin_path(), old)

    def test_no_record_and_nothing_on_path_resolves_the_known_install(self):
        known = self.binary(os.path.join(self.tmp, ".local", "bin",
                                         "dregg-cave-node"), "old")
        self.assertEqual(chatnode.bin_path(), known)

    def test_no_record_and_no_binary_is_none(self):  # noqa: VACUOUS_ASSERTION — the two arms above are the positive control on the same bin_path; None is the contract here
        self.assertIsNone(chatnode.bin_path())


class TheReadersNameTheRecordedBinary(BinaryBase):
    def test_node_staleness_reads_the_recorded_binary(self):
        b = self.cut_over()
        seen = []
        with mock.patch.object(cell, "_staleness",
                               side_effect=lambda what, path, *a: seen.append(
                                   path) or {"state": "current"}):
            cell.node_staleness()
        self.assertEqual(seen, [b])

    def test_node_staleness_is_unknown_with_the_refusal_when_it_is_gone(self):
        b = self.cut_over()
        os.unlink(b)
        row = cell.node_staleness()
        self.assertEqual(row["state"], "unknown")
        self.assertIn("MISSING", row["reason"])

    def status(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(chatnode, "_systemctl",
                               return_value=(0, "active")), \
                mock.patch.object(chat, "node_url", return_value="http://node"), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(cell, "get_json", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            chatnode.cmd_node(["status"])
        return out.getvalue(), err.getvalue()

    def doctor(self):
        with mock.patch.object(chat, "node_url", return_value="http://node"), \
                mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(chat, "transport_status",
                                  return_value={"mode": "unsigned"}), \
                mock.patch.object(chatnode, "boot_diagnosis",
                                  return_value={"state": "stopped"}), \
                mock.patch.object(cell, "bin_status",
                                  return_value={"configured": False,
                                                "usable": False}):
            return doctor.check_chat_node()

    def test_status_and_doctor_name_the_binary_and_where_it_came_from(self):
        b = self.cut_over()
        line = "node binary %s (recorded by `helm chat node up`)" % b
        out, _err = self.status()
        self.assertIn(line, out)
        self.assertIn((doctor.OK, "chat " + line), self.doctor())

    def test_status_and_doctor_name_the_fallback_as_the_fallback(self):
        old = self.old_on_path()
        out, _err = self.status()
        self.assertIn("node binary %s (fallback (nothing recorded)" % old, out)

    def test_status_and_doctor_fail_a_refused_record(self):
        b = self.cut_over()
        os.unlink(b)
        _out, err = self.status()
        self.assertIn("node binary REFUSED", err)
        self.assertIn(FIX, err)
        fails = [t for lvl, t in self.doctor() if lvl == doctor.FAIL]
        self.assertTrue(any("REFUSED" in t and b in t and FIX in t
                            for t in fails), fails)


if __name__ == "__main__":
    unittest.main()
