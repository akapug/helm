#!/usr/bin/env python3
"""helm chat node — the node's identity survives a reboot; its ledger does not.

WHY THIS EXISTS: the owner said "i see old data restored in chat,
but i don't see new dregg turns happening... did we forget to build that into
helm?" We had. The chat node's data dir is tmpfs by law (one-cave-per-team keeps
the cave RAM-hot, a2a-ram-only-disk-log-after keeps the send/read path off
disk), and /dev/shm is wiped on every boot — so every reboot minted a NEW node
key and the team's node came back as a stranger wearing the room's name.

The distinction these tests pin is the whole design: a node has a LEDGER and an
IDENTITY, and only the ledger is data. dregg.redb is the RAM-hot room and it is
SUPPOSED to evaporate. node.key is 32 bytes saying which node this is, and it
belongs on disk beside the passphrase and the unit file that are already there.
Persist the identity, let the room forget. So the first test below is the one
that matters most: the snapshot must contain the key and must NOT contain the
ledger.

Two defects in the previous fix are pinned here as regressions, both measured
against the real binary rather than reasoned about:

  * `test -d <dir> || init` re-keyed the node on every boot. It stopped the
    crash loop (47 restarts on this host, ~1827 fleet-wide) and silently traded
    it for a continuity break.
  * `dregg-cave-node init` NO-OPS ON AN EXISTING-BUT-EMPTY DIR AND EXITS 0,
    creating nothing. Anything that pre-creates the data dir therefore yields a
    KEYLESS cave while every exit code reports success — and `_up` pre-created
    it, one line before starting the unit. FakeDregg below reproduces exactly
    that behaviour, so test_an_existing_empty_dir_still_gets_a_key fails
    against the old ordering.
"""
import os
import shutil
import stat
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chatnode  # noqa: E402


class IdentityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-identity-")
        self.prior = {k: os.environ.get(k) for k in ("HOME", "HELM_HOME")}
        os.environ["HOME"] = self.tmp
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.cave = os.path.join(self.tmp, "cave")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, where, name, blob):
        os.makedirs(where, mode=0o700, exist_ok=True)
        p = os.path.join(where, name)
        with open(p, "wb") as f:
            f.write(blob)
        return p

    def fake_dregg(self, writes_key=True):
        """A stand-in for `dregg-cave-node init` with its MEASURED behaviour:
        an existing dir (empty or not) is left alone with rc 0; otherwise the
        dir and node.key are created. writes_key=False models a binary that
        reports success and produces nothing."""
        p = os.path.join(self.tmp, "fake-dregg-%s" % writes_key)
        body = "#!/bin/sh\n"
        body += 'd=""\n'
        body += 'while [ $# -gt 0 ]; do\n'
        body += '  if [ "$1" = "--data-dir" ]; then shift; d="$1"; fi\n'
        body += '  shift\n'
        body += 'done\n'
        body += '[ -d "$d" ] && { echo "Data directory already exists: $d"; exit 0; }\n'
        body += 'mkdir -p "$d"\n'
        if writes_key:
            body += 'printf %s "FAKEKEY-MINTED-FRESH-32-BYTES--" > "$d/node.key"\n'
        body += 'exit 0\n'
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p


class SnapshotTest(IdentityBase):
    def test_the_snapshot_takes_the_identity_and_never_the_ledger(self):
        """The RAM-only law in one assertion: keys leave the cave, data never
        does. A snapshot that swept up dregg.redb would put the room's
        messages on disk and break the topology this fix exists to preserve."""
        self.plant(self.cave, "node.key", b"K" * 32)
        self.plant(self.cave, "starbridge-seed.json", b'{"seed":1}')
        self.plant(self.cave, "dregg.redb", b"LEDGER" * 100)
        saved, err = chatnode.snapshot_identity(self.cave)
        self.assertIsNone(err)
        self.assertIn("node.key", saved)
        d = chatnode.identity_dir()
        self.assertTrue(os.path.isfile(os.path.join(d, "node.key")))
        self.assertTrue(os.path.isfile(os.path.join(d, "starbridge-seed.json")))
        self.assertFalse(os.path.exists(os.path.join(d, "dregg.redb")),
                         "the ledger must never be copied to disk")

    def test_snapshotted_key_material_is_0600(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        mode = os.stat(os.path.join(chatnode.identity_dir(), "node.key")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_an_unchanged_identity_is_not_rewritten(self):
        """Idempotent so `node up` can call it every time: the second pass
        reports nothing saved rather than churning the file."""
        self.plant(self.cave, "node.key", b"K" * 32)
        first, _ = chatnode.snapshot_identity(self.cave)
        second, err = chatnode.snapshot_identity(self.cave)
        self.assertIsNone(err)
        self.assertEqual(first, ["node.key"])
        self.assertEqual(second, [])

    def test_a_rotated_key_replaces_the_snapshot(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        self.plant(self.cave, "node.key", b"N" * 32)
        saved, _ = chatnode.snapshot_identity(self.cave)
        self.assertEqual(saved, ["node.key"])
        with open(os.path.join(chatnode.identity_dir(), "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"N" * 32)


class RestoreTest(IdentityBase):
    def test_the_identity_comes_back_into_an_absent_cave(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        shutil.rmtree(self.cave)                      # the reboot
        restored, err = chatnode.restore_identity(self.cave)
        self.assertIsNone(err)
        self.assertEqual(restored, ["node.key"])
        with open(os.path.join(self.cave, "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32)

    def test_a_live_caves_own_key_outranks_the_snapshot(self):
        """A restore must never re-key something that is running. The cave is
        the authority while it exists; the snapshot only speaks for a cave
        that is gone."""
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        self.plant(self.cave, "node.key", b"LIVE-AND-DIFFERENT-32-BYTES-XXXX")
        restored, err = chatnode.restore_identity(self.cave)
        self.assertIsNone(err)
        self.assertEqual(restored, [])
        with open(os.path.join(self.cave, "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"LIVE-AND-DIFFERENT-32-BYTES-XXXX")

    def test_no_snapshot_restores_nothing_and_is_not_an_error(self):
        restored, err = chatnode.restore_identity(self.cave)
        self.assertIsNone(err)
        self.assertEqual(restored, [])


class PrepareTest(IdentityBase):
    def test_a_live_cave_is_left_alone(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        self.plant(self.cave, "dregg.redb", b"LEDGER")
        msg, err = chatnode.prepare(self.cave, binary=self.fake_dregg())
        self.assertIsNone(err)
        self.assertIn("untouched", msg)
        with open(os.path.join(self.cave, "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32)

    def test_a_reboot_restores_the_same_node_rather_than_minting_one(self):
        """The owner-visible bug, end to end: cave wiped, identity returns."""
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        shutil.rmtree(self.cave)                      # /dev/shm after a boot
        msg, err = chatnode.prepare(self.cave, binary=self.fake_dregg())
        self.assertIsNone(err)
        self.assertIn("restored", msg)
        with open(os.path.join(self.cave, "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"K" * 32,
                             "the restored node must be the SAME node")

    def test_an_existing_empty_dir_still_gets_a_key(self):
        """REGRESSION for the silent-success trap. `init` no-ops on a dir that
        already exists — even an empty one — and still exits 0, so the old
        `test -d || init` ordering left a keyless cave that `run` refuses
        while every exit code claimed success. prepare() must own the dir's
        existence and produce a key regardless of who got there first."""
        os.makedirs(self.cave, mode=0o700)            # someone pre-created it
        self.assertEqual(os.listdir(self.cave), [])
        msg, err = chatnode.prepare(self.cave, binary=self.fake_dregg())
        self.assertIsNone(err, "prepare must not fail on a pre-created dir")
        self.assertTrue(os.path.isfile(os.path.join(self.cave, "node.key")),
                        "an empty dir must not survive prepare unkeyed")

    def test_a_fresh_mint_is_snapshotted_so_the_next_boot_is_continuous(self):
        msg, err = chatnode.prepare(self.cave, binary=self.fake_dregg())
        self.assertIsNone(err)
        self.assertIn("snapshotted", msg)
        snap = os.path.join(chatnode.identity_dir(), "node.key")
        self.assertTrue(os.path.isfile(snap))
        with open(snap, "rb") as f:
            snapped = f.read()
        with open(os.path.join(self.cave, "node.key"), "rb") as f:
            self.assertEqual(f.read(), snapped)

    def test_a_binary_that_reports_success_but_writes_no_key_is_refused(self):
        """Never start a keyless node on someone else's rc 0. The whole class
        of bug here is a success code standing in for a discharged
        obligation."""
        msg, err = chatnode.prepare(self.cave,
                                    binary=self.fake_dregg(writes_key=False))
        self.assertIsNone(msg)
        self.assertIn("no node.key", err)

    def test_a_missing_binary_with_nothing_to_restore_is_loud(self):
        msg, err = chatnode.prepare(self.cave, binary=os.path.join(
            self.tmp, "does-not-exist"))
        self.assertIsNone(msg)
        self.assertTrue(err)


class IdentityStateTest(IdentityBase):
    def test_a_snapshot_matching_the_live_cave_reports_matched(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        st = chatnode.identity_state(self.cave)
        self.assertEqual(st["saved"], ["node.key"])
        self.assertTrue(st["matched"])

    def test_a_snapshot_that_disagrees_with_the_live_cave_is_unmatched(self):
        """The one state that looks healthy and is not: a node is serving,
        a snapshot exists, and they are different nodes — so the next reboot
        would quietly restore a stranger."""
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        self.plant(self.cave, "node.key", b"DRIFTED-DIFFERENT-KEY-32-BYTES!!")
        st = chatnode.identity_state(self.cave)
        self.assertTrue(st["saved"])
        self.assertFalse(st["matched"])

    def test_no_snapshot_reports_nothing_saved(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        st = chatnode.identity_state(self.cave)
        self.assertEqual(st["saved"], [])


class UnitTextTest(IdentityBase):
    def test_the_unit_runs_the_prepare_verb_not_a_shell_conditional(self):
        text = chatnode.unit_text("/usr/bin/dregg-cave-node")
        self.assertIn("chat node prepare", text)
        self.assertNotIn("test -d", text,
                         "the shell conditional could neither restore an "
                         "identity nor survive an empty dir")

    def test_a_failed_prepare_is_fatal(self):
        """No `-` prefix: a node that cannot prove which node it is must not
        start. A stranger serving the team's room is worse than no room."""
        text = chatnode.unit_text("/usr/bin/dregg-cave-node")
        self.assertNotIn("ExecStartPre=-", text)

    def test_the_unit_names_a_stable_helm_rather_than_a_worktree(self):
        text = chatnode.unit_text("/usr/bin/dregg-cave-node")
        self.assertIn(chatnode.helm_bin(), text)


if __name__ == "__main__":
    unittest.main()
