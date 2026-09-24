#!/usr/bin/env python3
"""A proxy that is UP can still be wrong, and "UP" was all we reported.

A long-running proxy holds the binary and config it started with, so a landed
change is inert until restart. Empty-200 has at least two subclasses sharing one
signature: ds4pro's drops cleared on restart and MAY involve stale inputs, while
kimi's high-rate defect-hunting drops persist on a fresh proxy and track task
shape. This feature promises only what identity proves: whether the running
process's recorded launch inputs still match disk. It never promises that a
restart cures completion drops.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import seat, seat_proxy  # noqa: E402


class FakeProcess:
    pid = 4242
    returncode = None

    def poll(self):
        return None

    def kill(self):
        self.returncode = -9


class StaleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-stale-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.tmp
        self.d = seat.seat_dir("kimi")
        os.makedirs(self.d, exist_ok=True)
        self.cfg = os.path.join(self.d, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write(seat._config_yaml_key(
                8318, "test-token", "moonshot",
                "https://api.moonshot.ai/v1", "kimi-k3", "test-api-key",
                "kimi-k3"))
        self.bin = os.path.join(self.tmp, "cli-proxy-api")
        with open(self.bin, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.bin, 0o700)

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def launch_record(self, family="kimi", seat_name=None):
        cfg = os.path.join(seat._proxy_home(family, seat_name), "config.yaml")
        return {"pid": 4242, "identity": "proc:7",
                "launch": seat._proxy_launch_inputs(cfg, self.bin)}


class ProxyIdentityTest(StaleBase):
    def test_matching_launch_inputs_are_current(self):
        self.assertEqual(seat.proxy_drift("kimi", record=self.launch_record()),
                         (seat.PROXY_CURRENT, None))

    def test_changed_config_is_stale_with_valid_family_remedy(self):
        rec = self.launch_record()
        with open(self.cfg, "a") as f:
            f.write("new-setting: true\n")
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_STALE)
        self.assertIn("its config", detail)
        self.assertIn("helm seat down kimi && helm seat up kimi", detail)
        self.assertNotIn("empty-200", detail)

    def test_changed_binary_is_stale(self):
        rec = self.launch_record()
        with open(self.bin, "ab") as f:
            f.write(b"-v2")
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_STALE)
        self.assertIn("proxy binary", detail)

    def test_same_size_in_place_binary_deploy_is_stale_by_mtime(self):
        rec = self.launch_record()
        before = rec["launch"]["binary"]
        with open(self.bin, "r+b") as f:
            f.write(b"proxy-v2")
        st = os.stat(self.bin)
        os.utime(self.bin, ns=(st.st_atime_ns, before["mtime_ns"] + 1_000_000_000))
        after = seat._binary_identity(self.bin)
        self.assertEqual({k: v for k, v in before.items() if k != "mtime_ns"},
                         {k: v for k, v in after.items() if k != "mtime_ns"})
        self.assertNotEqual(before["mtime_ns"], after["mtime_ns"])
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_STALE)
        self.assertIn("proxy binary", detail)

    def test_symlink_retarget_is_stale(self):
        target_v1 = os.path.join(self.tmp, "proxy-v1")
        target_v2 = os.path.join(self.tmp, "proxy-v2")
        link = os.path.join(self.tmp, "proxy-current")
        os.replace(self.bin, target_v1)
        with open(target_v2, "wb") as f:
            f.write(b"proxy-v2")
        os.symlink(target_v1, link)
        rec = {"pid": 4242, "identity": "proc:7",
               "launch": seat._proxy_launch_inputs(self.cfg, link)}
        replacement = link + ".new"
        os.symlink(target_v2, replacement)
        os.replace(replacement, link)
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_STALE)
        self.assertIn("proxy binary", detail)

    def test_identical_instance_remint_is_still_current(self):
        seat._mint_instance_proxy("codex", "codex-2")
        rec = self.launch_record("codex", "codex-2")
        seat._mint_instance_proxy("codex", "codex-2")
        self.assertEqual(seat.proxy_drift("codex", "codex-2", rec),
                         (seat.PROXY_CURRENT, None),
                         "rewriting byte-identical config is not drift")

    def test_unreadable_required_config_is_unknown(self):
        rec = self.launch_record()
        with mock.patch.object(seat, "_config_digest",
                               side_effect=PermissionError("denied")):
            status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertTrue(status, "unknown must never collapse to false/current")
        self.assertIn("cannot read required config", detail)

    def test_missing_required_config_is_unknown(self):
        rec = self.launch_record()
        os.remove(self.cfg)
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertIn("cannot read required config", detail)

    def test_missing_recorded_binary_is_unknown(self):
        rec = self.launch_record()
        os.remove(self.bin)
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertIn("cannot read recorded proxy binary", detail)

    def test_malformed_binary_identity_is_unknown_not_crash_or_stale(self):
        rec = self.launch_record()
        rec["launch"]["binary"]["source"] = 7
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertIn("launch inputs are malformed", detail)

    def test_incomplete_binary_identity_is_unknown_not_stale(self):
        rec = self.launch_record()
        rec["launch"]["binary"] = {"source": self.bin, "path": self.bin}
        status, detail = seat.proxy_drift("kimi", record=rec)
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertIn("launch inputs are malformed", detail)

    def test_legacy_live_record_is_unknown_until_one_restart(self):
        status, detail = seat.proxy_drift(
            "kimi", record={"pid": 4242, "identity": "proc:7", "launch": None})
        self.assertEqual(status, seat.PROXY_UNKNOWN)
        self.assertIn("launch inputs were not recorded", detail)

    def test_down_proxy_is_current_and_needs_no_binary(self):
        with mock.patch.object(seat, "_running_pid_rec", return_value=None), \
             mock.patch.object(seat, "_proxy_bin",
                               side_effect=AssertionError("must not resolve")):
            self.assertEqual(seat.proxy_drift("kimi"),
                             (seat.PROXY_CURRENT, None))

    def test_pid_record_round_trips_launch_inputs(self):
        launch = self.launch_record()["launch"]
        body = "4242 proc:7 %s\n" % seat._encode_launch_inputs(launch)
        seat._write_private(os.path.join(self.d, "proxy.pid"), body)
        self.assertEqual(seat._proxy_pid_record("kimi")["launch"], launch)

    def test_pid_record_rejects_incomplete_binary_identity(self):
        launch = self.launch_record()["launch"]
        launch["binary"].pop("mtime_ns")
        body = "4242 proc:7 %s\n" % seat._encode_launch_inputs(launch)
        seat._write_private(os.path.join(self.d, "proxy.pid"), body)
        self.assertIsNone(seat._proxy_pid_record("kimi")["launch"])

    def test_up_records_the_inputs_given_to_popen(self):
        proc = FakeProcess()
        with mock.patch.object(seat, "_running_pid", return_value=None), \
             mock.patch.object(seat, "_proxy_bin", return_value=self.bin), \
             mock.patch.object(seat_proxy, "_proxy_binary_ready",
                               return_value=(True, None)), \
             mock.patch.object(seat, "_port_open",
                               side_effect=[False, False, True, True]), \
             mock.patch.object(seat, "_pid_identity", return_value="proc:7"), \
             mock.patch.object(seat.subprocess, "Popen", return_value=proc) as popen:
            self.assertEqual(seat._up("kimi", quiet=True), 0)
        rec = seat._proxy_pid_record("kimi")
        self.assertEqual(rec["launch"], seat._proxy_launch_inputs(self.cfg, self.bin))
        self.assertEqual(popen.call_args.args[0],
                         [self.bin, "-config", self.cfg])


class ProxyStatusIntegrationTest(StaleBase):
    def test_family_row_surfaces_suffix_detail_and_valid_remedy(self):
        rec = self.launch_record()
        with open(self.cfg, "a") as f:
            f.write("changed: true\n")
        with mock.patch.object(seat, "_running_pid_rec", return_value=rec), \
             mock.patch.object(seat, "_port_open", return_value=True):
            row = seat._seat_row("kimi")
        self.assertIn("proxy UP pid 4242 port 8318 ⚠ STALE", row)
        self.assertIn("⚠ kimi: STALE", row)
        self.assertIn("helm seat down kimi && helm seat up kimi", row)

    def test_instance_row_targets_the_instance_and_not_the_family(self):
        seat._mint_instance_proxy("codex", "codex-2")
        rec = self.launch_record("codex", "codex-2")
        cfg = os.path.join(seat._proxy_home("codex", "codex-2"), "config.yaml")
        with open(cfg, "a") as f:
            f.write("changed: true\n")

        def record(_family, seat_name=None):
            return rec if seat_name == "codex-2" else None

        with mock.patch.object(seat, "_running_pid_rec", side_effect=record), \
             mock.patch.object(seat, "_port_open", return_value=True):
            row = seat._seat_row("codex")
        self.assertIn("codex-2 proxy UP pid 4242 port 8319 ⚠ STALE", row)
        self.assertIn("helm seat down codex-2 && helm seat up codex-2", row)
        self.assertNotIn("helm seat down codex && helm seat up codex`", row)

    def test_unknown_inputs_are_visible_not_fresh(self):
        rec = self.launch_record()
        os.remove(self.cfg)
        with mock.patch.object(seat, "_running_pid_rec", return_value=rec), \
             mock.patch.object(seat, "_port_open", return_value=True):
            row = seat._seat_row("kimi")
        self.assertIn("⚠ drift UNKNOWN", row)
        self.assertIn("UNKNOWN — cannot read required config", row)


if __name__ == "__main__":
    unittest.main()
