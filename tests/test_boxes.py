"""The box-inventory provider chain: rungs, consent ladder, and merge law."""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from helm import boxes
from helm import storage_matrix as matrix
from tests.test_no_private_names import hits_in


def _env(**extra):
    """A clean chain environment: no declared hosts, no consent, no ssh
    config, and an external CLI that resolves to nothing."""
    cleared = {"HELM_STORAGE_MATRIX_HOSTS": None, "HELM_BOXES_SSH_CONSENT": None,
               "HELM_BOXES_SSH_CONFIG": "/nonexistent/ssh-config",
               "HELM_BOXES_EXTERNAL_CLI": "/nonexistent/inventory-cli"}
    cleared.update(extra)
    keep = {k: v for k, v in cleared.items() if v is not None}
    drop = [k for k, v in cleared.items() if v is None]
    patcher = mock.patch.dict(os.environ, keep)
    patcher.start()
    for key in drop:
        os.environ.pop(key, None)
    return patcher


class LocalFloorTest(unittest.TestCase):
    def test_localhost_is_always_present_with_zero_config(self):
        patcher = _env()
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        self.assertEqual(len(value["nodes"]), 1)
        row = value["nodes"][0]
        self.assertEqual(row["probe_mode"], "local")
        self.assertEqual(row["provider"], "local")
        self.assertTrue(row["reachable"])
        self.assertTrue(row["host"])


class DeclaredTest(unittest.TestCase):
    def test_env_hosts_are_parsed_deduplicated_and_probeable(self):  # noqa: VACUOUS_ASSERTION — the assertEqual pins exactly two declared rows, so the loop cannot run zero times
        patcher = _env(HELM_STORAGE_MATRIX_HOSTS=" alpha, beta ,alpha,")
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        declared = [n for n in value["nodes"] if n.get("provider") == "declared"]
        self.assertEqual([n["host"] for n in declared], ["alpha", "beta"])
        for row in declared:  # naming a host IS the consent to probe it
            self.assertEqual(row["probe_mode"], "ssh")
            self.assertTrue(row["reachable"])
            self.assertEqual(row["ssh_host"], row["host"])

    def test_invalid_declared_host_refuses_before_any_ssh(self):  # noqa: VACUOUS_ASSERTION — the raised ValueError is the effect; the parse positive control is the test above
        patcher = _env(HELM_STORAGE_MATRIX_HOSTS="good,bad;host")
        self.addCleanup(patcher.stop)
        with self.assertRaisesRegex(ValueError, "invalid host"):
            boxes.inventory()

    def test_unset_env_means_none_not_empty(self):  # noqa: VACUOUS_ASSERTION — None-vs-empty IS the contract; the parsed-list positive control is the test above
        patcher = _env()
        self.addCleanup(patcher.stop)
        self.assertIsNone(boxes.declared_hosts())


class SshConfigTest(unittest.TestCase):
    def _config(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".sshconfig",
                                             delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def test_candidates_are_listed_unprobed_until_consent(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT sorted() equality precedes every negative scan
        path = self._config(
            "Host wildcard-* !negated pattern?\n"
            "Host boxa boxb  # trailing comment\n"
            "  HostName 192.0.2.1\n"
            "host boxa\n"          # duplicate + lowercase keyword
            "Match host boxa\n")
        patcher = _env(HELM_BOXES_SSH_CONFIG=path)
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        rows = {n["host"]: n for n in value["nodes"]
                if n.get("provider") == "ssh-config"}
        # MUST-HIT seed: the parse found the real Host names before we trust
        # any of the negative assertions below
        self.assertEqual(sorted(rows), ["boxa", "boxb"])
        for row in rows.values():
            self.assertEqual(row["probe_mode"], "candidate")
            self.assertFalse(row["reachable"])
            self.assertTrue(row["display_only"])
            self.assertIn("not probed", row["notes"])
            self.assertIn("HELM_BOXES_SSH_CONSENT", row["notes"])
        joined = json.dumps(value)
        self.assertNotIn("wildcard", joined)
        self.assertNotIn("negated", joined)
        self.assertNotIn("192.0.2.1", joined,
                         "only Host NAMES may be read from the ssh config")

    def test_consent_flag_gates_probing_per_host_and_for_all(self):
        path = self._config("Host boxa boxb\n")
        patcher = _env(HELM_BOXES_SSH_CONFIG=path,
                       HELM_BOXES_SSH_CONSENT="boxa")
        self.addCleanup(patcher.stop)
        value, _warning = boxes.inventory()
        rows = {n["host"]: n for n in value["nodes"]
                if n.get("provider") == "ssh-config"}
        self.assertTrue(rows["boxa"]["reachable"])
        self.assertEqual(rows["boxa"]["probe_mode"], "ssh")
        self.assertFalse(rows["boxb"]["reachable"])
        self.assertEqual(rows["boxb"]["probe_mode"], "candidate")
        with mock.patch.dict(os.environ, {"HELM_BOXES_SSH_CONSENT": "all"}):
            value, _warning = boxes.inventory()
        rows = {n["host"]: n for n in value["nodes"]
                if n.get("provider") == "ssh-config"}
        self.assertTrue(all(r["reachable"] for r in rows.values()))

    def test_declared_host_also_in_the_ssh_config_stays_probeable(self):
        """The common case: a declared alias is usually ALSO a Host entry.
        The unconsented candidate must contribute nothing — not display_only,
        not the consent note — to the row the declaration already consented."""
        path = self._config("Host alpha\n")
        patcher = _env(HELM_BOXES_SSH_CONFIG=path,
                       HELM_STORAGE_MATRIX_HOSTS="alpha")
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        rows = [n for n in value["nodes"] if n.get("host") == "alpha"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "declared")
        self.assertEqual(row["probe_mode"], "ssh")
        self.assertTrue(row["reachable"])
        self.assertNotIn("display_only", row)
        self.assertNotIn("notes", row)

    def test_missing_config_is_silent(self):
        patcher = _env(HELM_BOXES_SSH_CONFIG="/nonexistent/ssh-config")
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        # positive control on the same observable: the floor row is there,
        # so an empty ssh-config slice is a real absence, not a dead read
        self.assertEqual([n["provider"] for n in value["nodes"]], ["local"])


class ExternalInventoryTest(unittest.TestCase):
    def _binary(self, script):
        handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(script)
        handle.close()
        os.chmod(handle.name, os.stat(handle.name).st_mode | stat.S_IXUSR)
        return handle.name

    def test_absent_binary_degrades_silently(self):
        patcher = _env(HELM_BOXES_EXTERNAL_CLI="/nonexistent/inventory-cli")
        self.addCleanup(patcher.stop)
        rows, warning = boxes.ExternalInventoryProvider().boxes()
        self.assertEqual(rows, [])
        self.assertIsNone(warning)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        self.assertEqual(len(value["nodes"]), 1)  # the floor, nothing else

    def test_present_binary_rows_are_stamped_and_enrich_the_local_floor(self):
        payload = {"nodes": [
            {"host": "hub", "label": "Hub", "probe_mode": "local",
             "fab_hot_base": "/synthetic/hot"},
            {"host": "fast", "label": "Fast", "device_kind": "fab-node",
             "reachable": True, "ssh_host": "fast-alias", "inventory_order": 1},
        ]}
        binary = self._binary("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n"
                              % json.dumps(payload))
        patcher = _env(HELM_BOXES_EXTERNAL_CLI=binary)
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIsNone(warning)
        local = [n for n in value["nodes"] if n.get("probe_mode") == "local"]
        self.assertEqual(len(local), 1, "external local row must MERGE into "
                                        "the floor, never duplicate it")
        self.assertEqual(local[0]["host"], "hub")
        self.assertEqual(local[0]["fab_hot_base"], "/synthetic/hot")
        fast = next(n for n in value["nodes"] if n.get("host") == "fast")
        self.assertEqual(fast["provider"], "external")
        self.assertEqual(fast["device_kind"], "fab-node")

    def test_broken_binary_warns_instead_of_lying(self):
        binary = self._binary("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
        patcher = _env(HELM_BOXES_EXTERNAL_CLI=binary)
        self.addCleanup(patcher.stop)
        value, warning = boxes.inventory()
        self.assertIn("storage inventory failed", warning)
        self.assertIn("boom", warning)
        self.assertEqual(len(value["nodes"]), 1)  # the floor still stands

    def test_candidate_never_demotes_a_richer_rung(self):
        payload = {"nodes": [{"host": "fast", "label": "Fast",
                              "device_kind": "fab-node", "reachable": True,
                              "ssh_host": "fast-alias"}]}
        binary = self._binary("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n"
                              % json.dumps(payload))
        config = tempfile.NamedTemporaryFile("w", suffix=".sshconfig",
                                             delete=False)
        self.addCleanup(os.unlink, config.name)
        config.write("Host fast-alias othernew\n")
        config.close()
        patcher = _env(HELM_BOXES_EXTERNAL_CLI=binary,
                       HELM_BOXES_SSH_CONFIG=config.name)
        self.addCleanup(patcher.stop)
        value, _warning = boxes.inventory()
        fast = [n for n in value["nodes"] if n.get("ssh_host") == "fast-alias"]
        self.assertEqual(len(fast), 1, "the candidate must merge into the "
                                       "external row by ssh alias")
        self.assertTrue(fast[0]["reachable"])
        self.assertEqual(fast[0]["provider"], "external")
        self.assertEqual([n["host"] for n in value["nodes"]
                          if n.get("provider") == "ssh-config"], ["othernew"])


class MatrixConsumptionTest(unittest.TestCase):
    """storage_matrix consumes the chain instead of its old inline read."""

    def test_targets_surface_candidates_as_unprobed_rows_with_the_reason(self):  # noqa: VACUOUS_ASSERTION — the tier-pair equality above the loop is the positive control
        config = tempfile.NamedTemporaryFile("w", suffix=".sshconfig",
                                             delete=False)
        self.addCleanup(os.unlink, config.name)
        config.write("Host candidatebox\n")
        config.close()
        patcher = _env(HELM_BOXES_SSH_CONFIG=config.name)
        self.addCleanup(patcher.stop)
        with mock.patch.object(matrix, "_local_disk_target",
                               return_value=("/synthetic/local", "test disk")):
            rows, err = matrix.targets()
        self.assertIsNone(err)
        candidate = [r for r in rows if r["box"] == "candidatebox"]
        self.assertEqual([r["tier"] for r in candidate],
                         ["local-disk", "memory"])
        for row in candidate:
            self.assertFalse(row["reachable"])
            self.assertIn("not probed", row["unavailable_reason"])
            self.assertIn("HELM_BOXES_SSH_CONSENT", row["unavailable_reason"])

    def test_declared_host_unknown_to_any_richer_rung_is_probeable(self):
        patcher = _env(HELM_STORAGE_MATRIX_HOSTS="loneybox")
        self.addCleanup(patcher.stop)
        with mock.patch.object(matrix, "_local_disk_target",
                               return_value=("/synthetic/local", "test disk")):
            rows, err = matrix.targets()
        self.assertIsNone(err)
        declared = [r for r in rows if r["box"] == "loneybox"]
        self.assertEqual(len(declared), 2)
        memory = next(r for r in declared if r["tier"] == "memory")
        self.assertTrue(memory["reachable"])
        self.assertEqual(memory["ssh_host"], "loneybox")

    def test_declared_hosts_still_limit_the_remote_set(self):
        config = tempfile.NamedTemporaryFile("w", suffix=".sshconfig",
                                             delete=False)
        self.addCleanup(os.unlink, config.name)
        config.write("Host candidatebox\n")
        config.close()
        patcher = _env(HELM_BOXES_SSH_CONFIG=config.name,
                       HELM_STORAGE_MATRIX_HOSTS="onlybox")
        self.addCleanup(patcher.stop)
        with mock.patch.object(matrix, "_local_disk_target",
                               return_value=("/synthetic/local", "test disk")):
            rows, _err = matrix.targets()
        remote = {r["box"] for r in rows if r["transport"] == "ssh"}
        self.assertEqual(remote, {"onlybox"})

    def test_no_external_inventory_is_complete_not_partial(self):
        """Silent degradation all the way up: with only the floor, the
        snapshot status carries no inventory apology."""
        patcher = _env()
        self.addCleanup(patcher.stop)
        with mock.patch.object(matrix, "_local_disk_target",
                               return_value=("/synthetic/local", "test disk")):
            rows, err = matrix.targets()
        self.assertIsNone(err)
        # positive control: the floor measured both local tiers
        self.assertEqual([r["tier"] for r in rows], ["local-disk", "memory"])


class WorldWallTest(unittest.TestCase):
    def test_owner_facing_chain_prose_names_no_private_tools(self):  # noqa: VACUOUS_ASSERTION — the chain-name equality seeds the scan before any assertNotIn
        names = [p.name for p in boxes.chain()]
        # MUST-HIT seed: the surfaces under scan really are the chain's
        self.assertEqual(names, ["local", "external", "declared", "ssh-config"])
        for text in [boxes.CANDIDATE_NOTE] + names:
            for forbidden in ("fab ", "fab build", "fabric"):
                self.assertNotIn(forbidden, text,
                                 "user-facing chain string leaks %r: %r"
                                 % (forbidden, text))
            # the operator's private names, held as hex by the arm that owns
            # them, so this test carries none of them in the clear
            self.assertEqual(hits_in(text), [],
                             "user-facing chain string names a private "
                             "project: %r" % text)


if __name__ == "__main__":
    unittest.main()
