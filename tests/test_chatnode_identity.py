#!/usr/bin/env python3
"""helm chat node — the node's identity survives a reboot; its ledger does not.

WHY THIS EXISTS: on 2026-07-28 the owner said "i see old data restored in chat,
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
import time
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
        # a fee-loop build: `init` is its only mint, and any other verb (the
        # rebased build's `genesis`) is a usage error
        body += '[ "$1" = init ] || exit 2\n'
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

    def test_a_rotated_key_replaces_the_snapshot_and_the_old_one_is_kept(self):
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        self.plant(self.cave, "node.key", b"N" * 32)
        saved, _ = chatnode.snapshot_identity(self.cave)
        self.assertEqual(saved, ["node.key"])
        with open(os.path.join(chatnode.identity_dir(), "node.key"), "rb") as f:
            self.assertEqual(f.read(), b"N" * 32)
        parent = os.path.dirname(chatnode.identity_dir())
        self.assertEqual(len([d for d in os.listdir(parent)
                              if ".replaced-" in d]), 1)


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



# THE FILES THE REAL CEREMONY WRITES, from `run_genesis` in node/src/genesis.rs
# at the rebased dregg tip: node-0.key (the raw 32-byte seed, reused verbatim
# under --reuse-validator-keys-from), node-0.env, the three well/faucet keys,
# one key per demo agent, genesis.json and the .devnet marker. `init` runs the
# same code into the data dir and renames node-0.key to node.key. Measured on
# a real mint: both lists are exactly these files.
CEREMONY_FILES = ("genesis.json", ".devnet", "node-0.key", "node-0.env",
                  "faucet.key", "fee-well.key", "issuer-well.key",
                  "agent-alice.key", "agent-bob.key", "agent-carol.key")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "chatnode_rebased_genesis")


def _real(name):
    """A REAL minted descriptor (see faucet-seed.json's provenance)."""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _faucet_seed():
    import json
    return bytes.fromhex(json.loads(_real("faucet-seed.json"))["faucet_seed_hex"])


def _supply_cell(genesis_text):
    """The cell the real mint funded with the 1,000,000 faucet supply — read
    from the descriptor by balance, independently of the code under test,
    which finds it by the faucet key's public key."""
    import json
    cells = [c["id"] for c in json.loads(genesis_text)["initial_cells"]
             if c.get("balance") == 1000000]
    assert len(cells) == 1, cells
    return cells[0]


class RebasedNodeFixture(IdentityBase):
    """A FIXTURE node binary, never the real one: it answers `genesis --help`
    with the successor flag, and its `genesis` and `init` install a REAL
    minted descriptor (tests/fixtures/chatnode_rebased_genesis) plus stand-in
    bytes for the keys the patch never reads. `faucet` chooses the faucet.key
    it writes: the real seed, none, or one no cell carries."""

    def rebased_node(self, faucet="real", rekey=False, genesis="genesis.json",
                     hold=None):
        """`hold`, a path prefix: the ceremony touches `<hold>.started`, then
        waits (at most 10 s) for `<hold>.release` before minting — a prepare
        caught mid-mint, for arms that race a second one against it."""
        tpl = os.path.join(self.tmp, "tpl-%s-%s" % (faucet, genesis))
        if not os.path.isdir(tpl):
            os.makedirs(tpl)
            with open(os.path.join(tpl, "genesis.json"), "w",
                      encoding="utf-8") as f:
                f.write(_real(genesis))
            for name in CEREMONY_FILES:
                if name not in ("genesis.json", "node-0.key", "faucet.key"):
                    with open(os.path.join(tpl, name), "wb") as f:
                        f.write(b"minted " + name.encode())
            seed = {"real": _faucet_seed(), "stranger": b"\x07" * 32}.get(faucet)
            if seed:
                with open(os.path.join(tpl, "faucet.key"), "wb") as f:
                    f.write(seed)
        p = os.path.join(self.tmp, "fake-rebased-node-%s-%s-%s-%s"
                         % (faucet, rekey, genesis, bool(hold)))
        body = r"""#!/bin/sh
echo "$@" >> "%(log)s"
verb="$1"; shift
if [ "$verb" = genesis ] && [ "$1" = --help ]; then
  echo "      --reuse-validator-keys-from <DIR>"; exit 0; fi
keys=""; out=""; dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reuse-validator-keys-from) shift; keys="$1";;
    --output) shift; out="$1";;
    --data-dir) shift; dir="$1";;
  esac; shift
done
mint() { mkdir -p "$1" && cp -rp "%(tpl)s"/. "$1"/; }
if [ "$verb" = genesis ]; then
  if [ -n "%(hold)s" ]; then
    : > "%(hold)s.started"; i=0
    while [ ! -e "%(hold)s.release" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
  fi
  mint "$out"
  if [ -n "%(rekey)s" ]; then printf 'REKEYED-DIFFERENT-32-BYTE-SEED!!' > "$out/node-0.key"
  else cp "$keys/node-0.key" "$out/node-0.key"; fi
  exit 0; fi
if [ "$verb" = init ]; then
  [ -f "$dir/genesis.json" ] && { echo "already a chain"; exit 0; }
  mint "$dir"; printf 'FRESH-INIT-SEED-32-BYTES-XXXXXXX' > "$dir/node.key"
  exit 0; fi
exit 2
""" % {"log": os.path.join(self.tmp, "calls.log"), "tpl": tpl,
       "rekey": "1" if rekey else "", "hold": hold or ""}
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p

    def calls(self):
        try:
            with open(os.path.join(self.tmp, "calls.log")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def genesis(self, where):
        import json
        with open(os.path.join(where, "genesis.json")) as f:
            return json.load(f)

    def key_in(self, where):
        try:
            with open(os.path.join(where, "node.key"), "rb") as f:
                return f.read()
        except OSError:
            return None

    def fee_loop_snapshot(self):
        """What the live fee-loop node's snapshot holds: its key, and the
        agent key and seed it minted for itself at run time."""
        self.plant(self.cave, "node.key", b"K" * 32)
        self.plant(self.cave, "agent-alice.key", b"OLD-ALICE")
        self.plant(self.cave, "starbridge-seed.json", b'{"old": true}')
        self.plant(self.cave, "dregg.redb", b"LEDGER")
        chatnode.snapshot_identity(self.cave)
        shutil.rmtree(self.cave)

    def fresh_world(self, tag):
        """A new HELM_HOME and cave, for an arm that loops over scenarios."""
        root = os.path.join(self.tmp, tag)
        os.makedirs(root)
        os.environ["HELM_HOME"] = os.path.join(root, "helm")
        self.cave = os.path.join(root, "cave")


class DescriptorSnapshotTest(RebasedNodeFixture):
    def test_the_whole_descriptor_is_snapshotted_and_never_the_store(self):
        for name in CEREMONY_FILES:
            self.plant(self.cave, name, b"x-" + name.encode())
        self.plant(self.cave, "dregg.redb", b"LEDGER" * 100)
        saved, err = chatnode.snapshot_identity(self.cave)
        self.assertIsNone(err)
        self.assertEqual(sorted(saved), sorted(CEREMONY_FILES))
        self.assertNotIn("dregg.redb", os.listdir(chatnode.identity_dir()))

    def test_a_genesis_snapshot_restores_whole_so_the_node_can_start(self):
        """Restoring node.key alone into a rebased node's dir gives a node
        that exits 1: `blocklace requires consensus_genesis_unix_seconds`."""
        for name in ("node.key",) + CEREMONY_FILES[:2]:
            self.plant(self.cave, name, b"x-" + name.encode())
        chatnode.snapshot_identity(self.cave)
        shutil.rmtree(self.cave)
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err)
        self.assertIn("restored", msg)
        self.assertEqual(sorted(os.listdir(self.cave)),
                         sorted(("node.key", "genesis.json", ".devnet")))
        self.assertNotIn("genesis --validators", "\n".join(self.calls()),
                         "a genesis snapshot must never be re-minted")


class SuccessorCeremonyTest(RebasedNodeFixture):
    def test_a_fee_loop_identity_meets_a_rebased_node_with_the_ceremony(self):
        self.fee_loop_snapshot()
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err, err)
        self.assertIn("successor ceremony", msg)
        # the SAME key, now the committee
        self.assertEqual(self.key_in(self.cave), b"K" * 32)
        self.assertFalse(os.path.exists(os.path.join(self.cave, "node-0.key")))
        self.assertTrue(os.path.isfile(os.path.join(self.cave, "genesis.json")))
        call = [c for c in self.calls() if c.startswith("genesis --validators")]
        self.assertEqual(len(call), 1)
        self.assertIn("--validators 1 --reuse-validator-keys-from", call[0])

    def test_the_old_nodes_agent_key_and_seed_never_enter_a_genesis_dir(self):
        self.fee_loop_snapshot()
        chatnode.prepare(self.cave, binary=self.rebased_node())
        with open(os.path.join(self.cave, "agent-alice.key"), "rb") as f:
            self.assertEqual(f.read(), b"minted agent-alice.key")
        self.assertFalse(os.path.exists(
            os.path.join(self.cave, "starbridge-seed.json")))
        # and the new snapshot is the new descriptor, the old one archived
        snap = chatnode.identity_dir()
        self.assertNotIn("starbridge-seed.json", os.listdir(snap))
        self.assertIn("genesis.json", os.listdir(snap))
        parent = os.path.dirname(snap)
        old = [d for d in os.listdir(parent) if ".pre-genesis-" in d]
        self.assertEqual(len(old), 1)
        with open(os.path.join(parent, old[0], "starbridge-seed.json"), "rb") as f:
            self.assertEqual(f.read(), b'{"old": true}')

    def test_the_real_genesis_is_patched_before_the_first_run(self):
        self.fee_loop_snapshot()
        chatnode.prepare(self.cave, binary=self.rebased_node())
        g = self.genesis(self.cave)
        self.assertIs(g["coordination_fee_exempt"], True)
        self.assertEqual(g["fee_well"], _supply_cell(_real("genesis.json")))
        self.assertTrue(g["fee_well"].startswith("b6a5b3f11d943606"))

    def test_a_ceremony_that_rekeys_is_refused(self):
        self.fee_loop_snapshot()
        msg, err = chatnode.prepare(self.cave,
                                    binary=self.rebased_node(rekey=True))
        self.assertIsNone(msg)
        self.assertIn("did not keep the node key", err)
        self.assertIsNone(self.key_in(self.cave))
        self.assertEqual(self.key_in(chatnode.identity_dir()), b"K" * 32)

    def test_a_fee_loop_binary_keeps_restoring_its_genesis_less_identity(self):
        """The rollback path: the old binary has no successor ceremony, so
        its identity comes back exactly as before."""
        self.fee_loop_snapshot()
        msg, err = chatnode.prepare(self.cave, binary=self.fake_dregg())
        self.assertIsNone(err)
        self.assertIn("restored", msg)
        self.assertEqual(sorted(os.listdir(self.cave)),
                         ["agent-alice.key", "node.key", "starbridge-seed.json"])

    def test_the_ceremony_names_its_rollback(self):
        self.fee_loop_snapshot()
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err)
        parent = os.path.dirname(chatnode.identity_dir())
        old = [d for d in os.listdir(parent) if ".pre-genesis-" in d]
        self.assertIn("ROLLBACK", msg)
        self.assertIn("`mv %s %s`" % (os.path.join(parent, old[0]),
                                      chatnode.identity_dir()), msg)


class GenesisMintTest(RebasedNodeFixture):
    def test_nothing_to_restore_inits_patches_and_snapshots(self):
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err, err)
        self.assertIn("snapshotted", msg)
        g = self.genesis(self.cave)
        self.assertEqual(g["fee_well"], _supply_cell(_real("genesis.json")))
        self.assertIn("genesis.json", os.listdir(chatnode.identity_dir()))

    def test_a_genesis_whose_faucet_key_no_cell_carries_is_refused_and_never_installed(self):
        """A refused patch must not leave a minted dir behind: systemd
        restarts the unit, and prepare would take a non-empty dir for a live
        one and start the node on the unpatched genesis."""
        msg, err = chatnode.prepare(self.cave,
                                    binary=self.rebased_node(faucet="stranger"))
        self.assertIsNone(msg)
        self.assertIn("no single initial cell", err)
        self.assertEqual(chatnode.descriptor_files(self.cave), [])
        # the restart mints again rather than calling a half-made dir live
        again, err2 = chatnode.prepare(
            self.cave, binary=self.rebased_node(faucet="stranger"))
        self.assertIsNone(again)
        self.assertIn("no single initial cell", err2)
        self.assertEqual(len([c for c in self.calls() if c.startswith("init")]), 2)

    def test_a_genesis_with_no_faucet_key_beside_it_is_refused(self):
        msg, err = chatnode.prepare(self.cave,
                                    binary=self.rebased_node(faucet="none"))
        self.assertIsNone(msg)
        self.assertIn("no readable faucet.key", err)

    def test_the_mint_gets_minutes_not_one(self):
        self.assertGreaterEqual(chatnode.MINT_TIMEOUT_S, 600)


class FaucetCellTest(IdentityBase):
    """The fee well points at the cell faucet.key CONTROLS, read from the
    minted descriptor — never a constant. Measured on real descriptors: one
    faucet key, and two different faucet cells across two builds."""

    def test_the_public_key_derivation_is_rfc8032(self):
        # RFC 8032 section 7.1, TEST 1
        self.assertEqual(chatnode.ed25519_public_key(bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )).hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")

    def _patched(self, genesis_name):
        import json
        d = os.path.join(self.tmp, "fixture-" + genesis_name.split(".")[0])
        self.plant(d, "genesis.json", _real(genesis_name).encode())
        self.plant(d, "faucet.key", _faucet_seed())
        self.assertIsNone(chatnode.patch_genesis(os.path.join(d, "genesis.json")))
        with open(os.path.join(d, "genesis.json")) as f:
            return json.load(f)

    def test_the_rebased_builds_real_genesis_gets_its_faucet_cell(self):
        g = self._patched("genesis.json")
        self.assertEqual(g["fee_well"], _supply_cell(_real("genesis.json")))
        self.assertTrue(g["fee_well"].startswith("b6a5b3f11d943606"))
        self.assertIs(g["coordination_fee_exempt"], True)

    def test_an_older_builds_real_genesis_gets_ITS_faucet_cell(self):
        """The same faucet key is cell 4a8882bb... in this build's genesis;
        a constant would refuse it, or worse, point elsewhere."""
        g = self._patched("older-build-genesis.json")
        self.assertEqual(g["fee_well"],
                         _supply_cell(_real("older-build-genesis.json")))
        self.assertTrue(g["fee_well"].startswith("4a8882bb17c23b3e"))


class KeyLossTest(RebasedNodeFixture):
    """THE ONLY COPY OF THE VALIDATOR KEY MUST SURVIVE ANY FAILED INSTALL.

    Measured by review on the previous tip: the ceremony succeeded, the move
    into the data dir failed on the ninth of ten files (ENOSPC), the data dir
    was left with genesis.json and no node.key, the scratch copy was deleted,
    the restart called the dir live, and a rebased node given a genesis and
    no key GENERATES one. Here the install fails at EVERY position, with the
    node key first and last, and the key must still be where prepare finds
    it: the restart yields a data dir holding the original key."""

    def _inject(self, pos, key_last):
        """Patches that fail the pos-th file written into the DATA DIR (or
        its staging sibling), or the final rename when pos is past the end.
        They cover this tip's per-file install and the previous tip's
        per-file move alike."""
        from unittest import mock
        import errno
        cave = self.cave
        count = [0]
        orig_file = getattr(chatnode, "_install_file", None)
        orig_move = chatnode.shutil.move
        orig_order = getattr(chatnode, "_install_order", None)

        def into_cave(dst):
            parent, base = os.path.split(os.path.dirname(os.path.abspath(dst)))
            here = os.path.join(parent, base)
            return here == cave or base.startswith(
                os.path.basename(cave) + ".mint-")

        def hit(dst):
            if not into_cave(dst):
                return False
            count[0] += 1
            return count[0] - 1 == pos

        def install_file(src, dst):
            if hit(dst):
                raise OSError(errno.ENOSPC, "injected", dst)
            return orig_file(src, dst)

        def move(src, dst, *a, **k):
            if hit(dst):
                raise OSError(errno.ENOSPC, "injected", dst)
            return orig_move(src, dst, *a, **k)

        def swap_in(stage, data_dir):
            if pos == len(CEREMONY_FILES):
                raise OSError(errno.ENOSPC, "injected rename", data_dir)
            os.rename(stage, data_dir)

        def order(names, key):
            rest = [n for n in names if n != key]
            return rest + [n for n in names if n == key]
        patches = [mock.patch.object(chatnode, "_install_file", install_file,
                                     create=True),
                   mock.patch.object(chatnode.shutil, "move", move),
                   mock.patch.object(chatnode, "_swap_in", swap_in, create=True)]
        if key_last and orig_order:
            patches.append(mock.patch.object(chatnode, "_install_order", order))
        return patches

    def test_an_install_that_fails_at_any_position_never_loses_the_key(self):
        import contextlib
        for key_last in (False, True):
            for pos in range(len(CEREMONY_FILES) + 1):
                with self.subTest(key_last=key_last, pos=pos):
                    self.fresh_world("w-%s-%d" % (key_last, pos))
                    self.fee_loop_snapshot()
                    node = self.rebased_node()
                    with contextlib.ExitStack() as stack:
                        for p in self._inject(pos, key_last):
                            stack.enter_context(p)
                        _msg, err = chatnode.prepare(self.cave, binary=node)
                    self.assertIsNotNone(err, "the injected failure fired")
                    # never a genesis without its key in the data dir
                    left = chatnode.descriptor_files(self.cave)
                    self.assertTrue(not left or "node.key" in left, left)
                    # the systemd restart, with nothing failing
                    msg2, err2 = chatnode.prepare(self.cave, binary=node)
                    self.assertIsNone(err2, err2)
                    self.assertEqual(self.key_in(self.cave), b"K" * 32, msg2)
                    self.assertEqual(self.key_in(chatnode.identity_dir()),
                                     b"K" * 32)

    def test_a_failed_install_leaves_the_data_dir_untouched(self):
        """Fence 1 alone: the data dir is absent or empty after ANY install
        failure — the atomic rename, not the later fences, guarantees it."""
        import contextlib
        for pos in (0, 5, len(CEREMONY_FILES) - 1, len(CEREMONY_FILES)):
            with self.subTest(pos=pos):
                self.fresh_world("u-%d" % pos)
                self.fee_loop_snapshot()
                with contextlib.ExitStack() as stack:
                    for p in self._inject(pos, False):
                        stack.enter_context(p)
                    chatnode.prepare(self.cave, binary=self.rebased_node())
                self.assertEqual(chatnode.descriptor_files(self.cave), [])

    def test_a_keyless_data_dir_is_moved_aside_never_called_live(self):
        """Fence: a dir holding a genesis and no node.key is what a rebased
        node turns into a stranger. prepare sets it aside and restores."""
        self.fee_loop_snapshot()
        node = self.rebased_node()
        self.assertIsNone(chatnode.prepare(self.cave, binary=node)[1])
        os.unlink(os.path.join(self.cave, "node.key"))     # the partial dir
        msg, err = chatnode.prepare(self.cave, binary=node)
        self.assertIsNone(err, err)
        self.assertNotIn("untouched", msg)
        self.assertIn("aside", msg)
        self.assertEqual(self.key_in(self.cave), b"K" * 32)
        parent = os.path.dirname(self.cave)
        self.assertEqual(len([d for d in os.listdir(parent)
                              if d.startswith("cave.partial-")]), 1)

    def test_a_keyless_snapshot_is_refused_never_restored_as_the_same_node(self):
        """Fence, measured by review on this tip: a snapshot holding the
        descriptor but no node.key was installed whole and reported as "the
        same node across the reboot" — the keyless data dir the fence above
        exists to catch, made by prepare itself one step after that fence
        ran. A rebased node started on it GENERATES a key (node/src/state.rs,
        first run), and the next `up` snapshotted the stranger: with no saved
        node.key to compare, neither the DISAGREES gate nor the
        archive-before-overwrite could fire."""
        node = self.rebased_node()
        self.assertIsNone(chatnode.prepare(self.cave, binary=node)[1])
        os.unlink(os.path.join(chatnode.identity_dir(), "node.key"))
        shutil.rmtree(self.cave)                                # the reboot
        msg, err = chatnode.prepare(self.cave, binary=node)
        self.assertIsNone(msg)
        self.assertIn("no node.key", err)
        self.assertEqual(chatnode.descriptor_files(self.cave), [],
                         "nothing is installed for a node to start on")
        # and no new identity is minted over it either
        self.assertEqual(len([c for c in self.calls() if c.startswith("init")]), 1)
        restored, rerr = chatnode.restore_identity(self.cave)
        self.assertEqual(restored, [])
        self.assertIn("no node.key", rerr)

    def test_a_differing_key_never_silently_replaces_the_snapshot(self):
        """Fence: snapshot_identity archives the snapshot before a different
        node.key is written over it."""
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        self.plant(self.cave, "node.key", b"S" * 32)          # a stranger
        _saved, err = chatnode.snapshot_identity(self.cave)
        self.assertIsNone(err)
        parent = os.path.dirname(chatnode.identity_dir())
        kept = [d for d in os.listdir(parent) if ".replaced-" in d]
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.key_in(os.path.join(parent, kept[0])), b"K" * 32)


class SnapshotSwapTest(RebasedNodeFixture):
    def test_a_failed_swap_after_the_archive_rename_leaves_a_snapshot(self):
        """Measured by review: the old snapshot was renamed to its archive,
        the new one failed to write, and no snapshot existed; the next boot
        minted a new identity. The swap now rolls the archive back."""
        from unittest import mock
        import errno
        self.fee_loop_snapshot()
        snap = chatnode.identity_dir()
        real_replace = os.replace

        def replace(src, dst):
            if os.path.abspath(dst) == os.path.abspath(snap) \
                    and not os.path.basename(src).startswith(
                        os.path.basename(snap) + ".pre-genesis-"):
                raise OSError(errno.ENOSPC, "injected", dst)
            return real_replace(src, dst)
        # the os.replace injection is the whole failure: prepare no longer
        # calls snapshot_identity after a mint (_replace_snapshot does that
        # work), so a double on it fires nowhere
        with mock.patch.object(chatnode.os, "replace", replace):
            _msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNotNone(err)
        self.assertEqual(self.key_in(snap), b"K" * 32)
        shutil.rmtree(self.cave, ignore_errors=True)            # the reboot
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err, err)
        self.assertEqual(self.key_in(self.cave), b"K" * 32, msg)

    def test_an_interrupted_swap_is_finished_not_replaced_by_a_new_key(self):
        """Killed between the two renames: `.new` is complete and there is
        no snapshot. prepare finishes the swap."""
        self.plant(self.cave, "node.key", b"K" * 32)
        self.plant(self.cave, "genesis.json", _real("genesis.json").encode())
        chatnode.snapshot_identity(self.cave)
        snap = chatnode.identity_dir()
        os.rename(snap, snap + ".new")
        shutil.rmtree(self.cave)
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err, err)
        self.assertEqual(self.key_in(self.cave), b"K" * 32, msg)
        self.assertNotIn("init", "\n".join(self.calls()))

    def test_no_snapshot_but_an_archived_key_refuses_to_mint(self):
        """A snapshot gone and a pre-genesis archive holding the key: a fresh
        init would bury that key under a new identity. Refuse, name it."""
        self.plant(self.cave, "node.key", b"K" * 32)
        chatnode.snapshot_identity(self.cave)
        snap = chatnode.identity_dir()
        os.rename(snap, snap + ".pre-genesis-1")
        shutil.rmtree(self.cave)
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(msg)
        self.assertIn(snap + ".pre-genesis-1", err)
        self.assertIsNone(self.key_in(self.cave))
        self.assertNotIn("init", "\n".join(self.calls()))


class PrepareLockTest(RebasedNodeFixture):
    """ONE PREPARE AT A TIME (ruled cure B1, task/2961). Two at once — the
    unit's ExecStartPre and a hand run — can each mint, and leave the data
    dir on one genesis and the snapshot on the other. A second prepare
    refuses at once, naming the lock; it never waits."""

    def lock(self):
        return os.path.join(os.path.dirname(chatnode.identity_dir()),
                            "chat-node-prepare.lock")

    def test_a_prepare_while_the_lock_is_held_refuses_and_names_it(self):
        import fcntl
        self.fee_loop_snapshot()
        os.makedirs(os.path.dirname(self.lock()), exist_ok=True)
        with open(self.lock(), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            t0 = time.time()
            msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
            self.assertLess(time.time() - t0, 5, "waited on the lock")
        self.assertIsNone(msg)
        self.assertIn(self.lock(), err)
        self.assertIn("refusing", err)
        self.assertEqual(self.calls(), [], "a refused prepare minted")
        self.assertFalse(os.path.exists(self.cave))
        # the positive control on the same door: released, it runs
        msg, err = chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertIsNone(err, err)
        self.assertIn("successor ceremony", msg)

    def test_a_second_prepare_racing_a_mint_refuses_and_the_first_completes(self):
        """The first prepare is mid-ceremony (its fixture paused); a second
        one — the hand run — must refuse, and the pair must end with the data
        dir and the snapshot on ONE genesis, minted once."""
        import threading
        self.fee_loop_snapshot()
        hold = os.path.join(self.tmp, "hold")
        node = self.rebased_node(hold=hold)
        first = {}
        t = threading.Thread(target=lambda: first.update(
            r=chatnode.prepare(self.cave, binary=node)))
        t.start()
        deadline = time.time() + 10
        while not os.path.exists(hold + ".started") and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(hold + ".started"), "the first never minted")
        msg2, err2 = chatnode.prepare(self.cave, binary=node)
        open(hold + ".release", "w").close()
        t.join(20)
        self.assertIsNone(msg2)
        self.assertIn(self.lock(), err2)
        msg1, err1 = first["r"]
        self.assertIsNone(err1, err1)
        self.assertEqual(len([c for c in self.calls()
                              if c.startswith("genesis --validators")]), 1)
        with open(os.path.join(self.cave, "genesis.json"), "rb") as a, \
                open(os.path.join(chatnode.identity_dir(), "genesis.json"),
                     "rb") as b:
            self.assertEqual(a.read(), b.read())


class PrepareHygieneTest(RebasedNodeFixture):
    def test_stale_mint_scratch_is_swept_and_fresh_scratch_is_kept(self):
        import time as _time
        parent = os.path.dirname(chatnode.identity_dir())
        old = os.path.join(parent, "chat-node-mint-old")
        fresh = os.path.join(parent, "chat-node-mint-fresh")
        stage = os.path.join(os.path.dirname(self.cave), "cave.mint-old")
        for d in (old, fresh, stage):
            self.plant(d, "node-0.key", b"K" * 32)
        long_ago = _time.time() - 10 * 3600
        for d in (old, stage):
            os.utime(d, (long_ago, long_ago))
        chatnode.prepare(self.cave, binary=self.rebased_node())
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(stage))
        self.assertTrue(os.path.exists(fresh))

    def test_an_unreadable_snapshot_key_is_a_prepare_failure_not_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        import contextlib
        import io
        self.fee_loop_snapshot()
        key = os.path.join(chatnode.identity_dir(), "node.key")
        os.chmod(key, 0)          # tearDown's rmtree needs only the dir's mode
        err_out = io.StringIO()
        with contextlib.redirect_stderr(err_out), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = chatnode.cmd_node(["prepare", "--data-dir", self.cave,
                                    "--bin", self.rebased_node()])
        self.assertEqual(rc, 1)
        self.assertIn(chatnode.PREPARE_FAILED, err_out.getvalue())


class UnitBinaryTest(IdentityBase):
    def test_prepare_is_told_the_binary_the_unit_runs(self):
        """systemd carries no HELM_CHAT_NODE_BIN, so an override chosen at
        `up` reached ExecStart and never the mint in ExecStartPre."""
        text = chatnode.unit_text("/opt/dregg-node-rebased")
        pre = [ln for ln in text.splitlines() if ln.startswith("ExecStartPre=")]
        self.assertEqual(len(pre), 1)
        self.assertTrue(pre[0].endswith("--bin /opt/dregg-node-rebased"), pre)

    def test_the_start_timeout_outlasts_the_mint_and_its_probe(self):
        """systemd's SIGTERM runs no cleanup; python's timeout does. The unit
        must give the mint AND the `genesis --help` probe their whole budget."""
        text = chatnode.unit_text("/opt/dregg-node-rebased")
        got = [int(ln.split("=", 1)[1]) for ln in text.splitlines()
               if ln.startswith("TimeoutStartSec=")]
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0], chatnode.MINT_TIMEOUT_S
                           + getattr(chatnode, "PROBE_TIMEOUT_S", 30))

    def test_the_prepare_verb_passes_bin_through(self):  # noqa: ORPHANED_MOCK — cmd_node reaches prepare through the _VERBS dispatch table, which the walker does not follow; rc 0 and the recorded arguments prove the double fired
        seen = {}

        def fake(data_dir, binary=None):
            seen.update(data_dir=data_dir, binary=binary)
            return "ok", None
        from unittest import mock
        with mock.patch.object(chatnode, "prepare", fake):
            rc = chatnode.cmd_node(["prepare", "--data-dir", self.cave,
                                    "--bin", "/opt/n"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"data_dir": self.cave, "binary": "/opt/n"})

if __name__ == "__main__":
    unittest.main()
