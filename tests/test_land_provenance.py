"""A LAND TAKES ONLY A RECEIPT HELM CAN PROVE IT RAN (task/3066).

THE MEASURED DEFECT. `helm gate import` (gateimport.import_receipt) checks an
artifact's schema, content id, head and tree, and every one of those checks is
a computation the artifact's submitter can run too. So a v4 FAILED receipt
with its status flipped to OK and its id recomputed imported and bound every
land door (a probe on trunk), and so did a v10 OK reminted as v4 (a review).
Until task/3066 every train land went through that door.

THE LAW. A land door asks, after every content clause, which AUTHENTICATED
door placed the receipt in the repository being landed, and reads the answer
only from the rows those doors write (`gateimport.land_provenance`):

  (a) a LOCAL MINT record, written by `gate._mint_result` alone;
  (b) a FAB COMPLETION helm built from its own observation of the Fab job,
      whose artifact_sha256 is the artifact the import binding recorded;
  (c) a ROUTED CUSTODY row from gateroute's challenge-framed session.

A generic import still binds every lane-level purpose (a review's APPROVE, a
lane tip), and never a land.

EVERY ARM IS A PAIR. Each forge arm asserts the lane-level door still binds
the same receipt (the forge is a real, placed receipt, not a malformed row
refused for some other reason), and each accepted door is asserted through
every land door, so a door that refused everything could not pass them.
The plants the report names were run against these arms: dropping the
provenance question turns both forge arms red, and accepting a mismatched
artifact_sha256 turns its arm red.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import calendar
import time
import unittest
import uuid
from unittest import mock

from tests import _tmphome
from tests._gate_receipt import serial_process
from tests import test_fabgate as fabfix
from tests import test_gate_focus as _focus
from helm import (fabgate, foldcheck, foldcompose, gate, gateimport,
                  gatewindow, landgate, vcs)

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL", "HELM_PROC",
            "HELM_CROSS_TREE_GATE", "HELM_NO_TREE_WARNING", "GIT_DIR",
            "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "FAB_ID",
            "FAB_GATE_GENERATION", "FAB_GATE_LABEL", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")
CURE = "a land needs a receipt helm can prove it ran"
WINDOW = "helm gate window launch"
INTERPRETER = {"name": "cpython", "version": "3.13.7", "language": "3.13.7",
               "executable": "/usr/bin/python3.13"}
RUNNER = {"format": "fab-gate-runner-v1",
          "argv": ["-m", "helm", "gate", "run", "--repo", "."],
          "wrapper_version": "c" * 64,
          "cgroup": "systemd-user-generation-cgroup-v1"}


def _git(repo, *args):
    proc = subprocess.run(("git", "-C", repo) + args, capture_output=True,
                          text=True, timeout=30)
    if proc.returncode:
        raise AssertionError("git %r: %s" % (args, proc.stderr))
    return proc.stdout.strip()


class ProvenanceBase(unittest.TestCase):
    # THE FLIP IS FORWARD-ONLY: until its activation is recorded every land
    # door answers as it did. These arms are about the flip itself, so they
    # activate it at an instant before anything reaches the ledger; the
    # forward-only arms place it themselves.
    ACTIVATE_AT = "2000-01-01T00:00:00Z"

    def setUp(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="helm-test-prov-"))
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore_env)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "provenance-fixture"
        os.environ["HELM_NO_TREE_WARNING"] = "1"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "prov@test")
        _git(self.repo, "config", "user.name", "prov test")
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("one\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-qm", "one")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        if self.ACTIVATE_AT:
            self.activate(self.ACTIVATE_AT)

    def activate(self, ts):
        record, err = gateimport.activate(ts=ts)
        self.assertIsNone(err, err)
        self.assertEqual(record["ts"], ts)
        return record

    def tearDown(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None

    def _restore_env(self):
        for key, value in self.prior.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    # -- receipts and artifacts ------------------------------------------

    def receipt(self, **over):
        """A v4 whole-suite receipt as a Fab node mints one: its repo_id is
        the node's worktree, which names nothing on this box."""
        row = {"v": 4, "event": "gate", "ts": "2026-09-25T01:24:13Z",
               "repo_id": "/mnt/fab-hot/wt/" + "a" * 64,
               "head": self.head, "tree": self.tree, "dirty": False,
               "head_after": self.head, "tree_after": self.tree,
               "dirty_after": False, "interpreter": dict(INTERPRETER),
               "host": {"node": "snoozy", "system": "Linux", "release": "1",
                        "id": "ab" * 8},
               "argv": [INTERPRETER["executable"]] + list(fabgate.WHOLE_ARGV),
               "suite": True, "label": None, "rc": 0, "wall": 1200.0,
               "status": "OK", "ran": 20, "skipped": 0, "detail": "",
               "elapsed": 1190.0, "failures": [],
               "failures_unreadable": False, "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    def artifact(self, row, name=None, sort_keys=False):
        path = os.path.join(self.tmp, name or ("artifact-%s.jsonl" % row["id"]))
        with open(path, "w") as fh:
            fh.write(json.dumps(row, sort_keys=sort_keys) + "\n")
        return path

    def generic_import(self, row):
        imported, verdict, err = gateimport.import_receipt(
            self.artifact(row), self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(verdict, "imported")
        self.assertEqual(imported["id"], row["id"])
        return row

    def flipped(self):
        """The status-flip forge: an honest red, edited green, id recomputed."""
        red = self.receipt(
            status="FAILED", rc=1, detail="failures=1",
            failures=[{"kind": "FAIL", "test": "tests.test_x.T.test_y",
                       "traceback": "File \"tests/test_x.py\", line 3"}],
            base_check={"verdict": "LANE_OWNED", "reason": "lane"})
        forged = copy.deepcopy(red)
        forged.update(status="OK", rc=0, detail="", failures=[],
                      failures_unreadable=False, base_check=None)
        forged["id"] = gate._receipt_id(forged)
        self.assertNotEqual(forged["id"], red["id"])
        return forged

    def reminted(self):
        """The review's remint: a sliced (v10) OK re-shaped as a serial v4,
        so the KIND question that refuses v10 at a land no longer sees it."""
        sliced = self.receipt(v=gate.SLICE_VERSION,
                              slice_authority={"workers": 2, "leaks": 0})
        forged = copy.deepcopy(sliced)
        forged.pop("slice_authority")
        forged["v"] = 4
        forged["argv"] = [INTERPRETER["executable"]] + list(fabgate.WHOLE_ARGV)
        forged["id"] = gate._receipt_id(forged)
        self.assertIsNone(gate.land_refusal(forged),
                          "the remint must defeat the kind question, or this "
                          "arm would be about the kind and not provenance")
        self.assertIsNotNone(gate.land_refusal(sliced))
        return forged

    # -- the doors ---------------------------------------------------------

    def lane_level(self, row):
        """Every lane-level door, which a generic import still satisfies."""
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head,
                                     repo_id=self.repo)
        self.assertEqual(state, "VERIFIED", why)
        rung = foldcheck.gate_authority(self.repo, self.head,
                                        "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.PASS, rung.discriminator)

    def land_doors(self, row):
        """{door: (admitted, why)} across every door that authorizes a land."""
        handle = "gate:" + row["id"]
        state, _rid, bind_why = gate.bind(
            gate.evidence_line(row), self.head, repo_id=self.repo,
            need=gate.NEED_LAND)
        common = vcs.backend(self.repo).common_dir(self.repo)
        state_common, _rid, common_why = gate.bind(
            gate.evidence_line(row), self.head, repo_id=common,
            consuming_repo=self.repo, need=gate.NEED_LAND)
        rung = foldcheck._tree_matches_gate(
            vcs.backend(self.repo), self.repo, self.head, handle, land=True)
        clause, clause_why = landgate.gate_binds_tree(
            row["id"], None, repo=self.repo, tip=self.head)
        bounded, bounded_why = foldcompose.bounded_gate(
            self.repo, self.head, handle)
        return {
            "bind NEED_LAND": (state == "VERIFIED", bind_why),
            "bind NEED_LAND (common dir)": (state_common == "VERIFIED",
                                            common_why),
            "foldcheck tree-vs-gate": (rung.verdict == foldcheck.PASS,
                                       rung.discriminator),
            "landgate clause (ii)": (clause == landgate.OK, clause_why),
            "foldcompose bounded_gate": (bounded is True, bounded_why),
        }

    def assert_every_land_door_refuses(self, row, needle=CURE):
        for door, (admitted, why) in self.land_doors(row).items():
            with self.subTest(door=door):
                self.assertFalse(admitted, "%s admitted: %s" % (door, why))
                self.assertIn(needle, why)
                self.assertIn(row["id"], why)

    def assert_every_land_door_admits(self, row, door_name):
        for door, (admitted, why) in self.land_doors(row).items():
            with self.subTest(door=door):
                self.assertTrue(admitted, "%s refused: %s" % (door, why))
        proven, why = gate.land_provenance(row, self.repo)
        self.assertIs(proven, True, why)
        self.assertIn(door_name, why)


class GenericForgeArms(ProvenanceBase):
    """The two measured forges, each through the generic door."""

    def test_the_status_flip_forge_binds_lane_level_and_refuses_every_land(self):  # noqa: VACUOUS_ASSERTION — lane_level() asserts VERIFIED and PASS on the same forged receipt before every land refusal, and the cure text is asserted positively
        forged = self.generic_import(self.flipped())
        self.lane_level(forged)
        self.assert_every_land_door_refuses(forged)
        proven, why = gate.land_provenance(forged, self.repo)
        self.assertIs(proven, False, why)
        self.assertIn("generic import door only", why)
        # The cure names the door that proves a run, and says whose word a
        # generic import is.
        _admitted, why = self.land_doors(forged)["bind NEED_LAND"]
        self.assertIn(WINDOW, why)
        self.assertIn("the artifact's own word", why)

    def test_the_remint_forge_binds_lane_level_and_refuses_every_land(self):  # noqa: VACUOUS_ASSERTION — lane_level() asserts VERIFIED and PASS on the same reminted receipt, and reminted() asserts the remint defeats the kind question
        forged = self.generic_import(self.reminted())
        self.lane_level(forged)
        self.assert_every_land_door_refuses(forged)

    def test_no_import_door_writes_a_local_mint_record(self):  # noqa: VACUOUS_ASSERTION — generic_import() asserts the import positively; LocalMintArms reads the same ledger non-empty after a real mint
        self.generic_import(self.flipped())
        rows, err = gateimport._door_rows(gateimport.mints_path(),
                                          gateimport._mint_err, "mints")
        self.assertIsNone(err, err)
        self.assertEqual(rows, [])

    def test_a_declared_block_this_repository_never_declared_is_the_flips(self):
        """A `suite_command` block is the receipt's claim about itself; with
        no authored declaration of that command for the landed repository it
        is no declared scope, and the refusal names what would make it one."""
        declared = {"source": "registry", "argv": ["bash", "scripts/gate.sh"],
                    "protocol": "exit", "project": "adopter"}
        row = self.receipt(argv=["bash", "scripts/gate.sh"],
                           suite_command=declared)
        refusal = gate.land_provenance_refusal(row, self.repo)
        self.assertIn("declares itself", refusal)
        self.assertIn("not a declared-command land", refusal)
        self.assertNotIn(WINDOW, refusal)

    def test_a_land_that_names_no_repository_is_UNKNOWN_never_a_pass(self):
        """The row's own repo_id is a field of the artifact, so a land that
        names no repository has none to ask about, even when that repo_id
        names this very checkout and the repository-authority spend passes."""
        row = self.generic_import(self.receipt(repo_id=self.repo))
        proven, why = gate.land_provenance(row, None)
        self.assertIsNone(proven)
        self.assertIn("none named", why)
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head)
        self.assertEqual(state, "VERIFIED", why)
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head,
                                     need=gate.NEED_LAND)
        self.assertEqual(state, "REFUSED")
        self.assertIn("provenance UNKNOWN", why)
        self.assertIn("none named", why)


class DeclaredCommandArms(ProvenanceBase):
    """OPTION (D): a project that declares its own gate command has no
    authenticated remote door yet, so its land binds as it did and says so,
    `provenance: unauthenticated-no-door`. The scope is read from the
    landed repository's own authored declaration, never from the receipt."""

    COMMAND = ["bash", "scripts/gate.sh"]

    def setUp(self):
        super().setUp()
        self.declare(self.COMMAND, "exit")

    def declare(self, command, protocol):
        from helm import home
        d = home.global_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "registry.json"), "w") as fh:
            json.dump({"version": 1, "projects": {"adopter": {
                "name": "adopter", "path": self.repo, "kind": "git",
                "status": "active", "sessions": {}}}}, fh)
        with open(os.path.join(d, "registry-authored.json"), "w") as fh:
            json.dump({"version": 1, "projects": {"adopter": {
                "path": self.repo, gate.DECLARED_GATE_FIELD: {
                    "command": list(command), "protocol": protocol}}}}, fh)

    def declared(self, command=None, protocol="exit", **over):
        """An adopter-shaped receipt: the declared command, run whole on a Fab
        node, its exit status the verdict (receipt 4d9da2bb7d34c800's shape)."""
        command = list(command or self.COMMAND)
        fields = dict(argv=command, suite_command={
            "source": "registry", "argv": command, "protocol": protocol,
            "project": "adopter"})
        if protocol == "exit":
            fields.update(ran=None, skipped=None, elapsed=None,
                          failures_unreadable=True,
                          detail="bash exited 0; the declared `exit` protocol "
                                 "reads that status as the verdict")
        fields.update(over)
        return self.receipt(**fields)

    def test_an_adopter_shaped_generic_receipt_binds_at_every_land_door_and_says_so(self):  # noqa: VACUOUS_ASSERTION — every door is asserted True and its success text asserted to carry the no-door marker
        row = self.generic_import(self.declared())
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head,
                                     repo_id=self.repo)
        self.assertEqual(state, "VERIFIED", why)
        for door, (admitted, why) in self.land_doors(row).items():
            with self.subTest(door=door):
                self.assertTrue(admitted, "%s refused: %s" % (door, why))
                self.assertIn("provenance: unauthenticated-no-door", why)
        proven, why = gate.land_provenance(row, self.repo)
        self.assertIs(proven, True, why)
        self.assertIn(gate.LAND_NO_DOOR, why)
        # CONTROL on the same receipt: the authenticated reader alone still
        # says no door placed it; the carve-out is a scope, not a door.
        self.assertIs(gateimport.land_provenance(row, self.repo)[0], False)

    def test_a_declared_block_forging_helms_runner_argv_is_helms_suite(self):  # noqa: VACUOUS_ASSERTION — the lane-level rung is asserted PASS on the same receipt, and the refusal's reason and cure are asserted positively
        """The repository even DECLARES helm's runner command here, the
        strongest form of the forgery: the scope is read off the argv against
        helm's runner, so it is helm's suite and the flip applies."""
        runner = [INTERPRETER["executable"]] + list(fabgate.WHOLE_ARGV)
        self.declare(runner, "unittest")
        row = self.generic_import(self.declared(command=runner,
                                                protocol="unittest"))
        rung = foldcheck.gate_authority(self.repo, self.head,
                                        "gate:" + row["id"])
        self.assertEqual(rung.verdict, foldcheck.PASS, rung.discriminator)
        for door, (admitted, why) in self.land_doors(row).items():
            with self.subTest(door=door):
                self.assertFalse(admitted, "%s admitted: %s" % (door, why))
                self.assertIn(row["id"], why)
        why = gate.land_provenance_refusal(row, self.repo)
        self.assertIn("runs helm's own suite runner", why)
        self.assertIn(CURE, why)

    def test_a_declared_block_for_a_command_this_repository_does_not_declare_refuses(self):
        row = self.generic_import(self.declared())
        self.declare(["bash", "scripts/other.sh"], "exit")
        rung = foldcheck._tree_matches_gate(
            vcs.backend(self.repo), self.repo, self.head, "gate:" + row["id"],
            land=True)
        self.assertEqual(rung.verdict, foldcheck.REFUSE, rung.discriminator)
        self.assertIn("not a declared-command land", rung.discriminator)


class FabCompletionArms(ProvenanceBase):
    """(b): the durable door, driven through fabgate's own reconcile."""

    def completed(self, **over):
        builder = fabfix.FakeBuilder()
        job, err = fabgate.request(self.repo, self.tree, "whole",
                                   dict(INTERPRETER), dict(RUNNER),
                                   queue_timeout=600, execution_timeout=3600)
        self.assertIsNone(err, err)
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err, err)
        handle = admitted["handle"]
        row = self.receipt(host={"node": handle["host"], "system": "Linux",
                                 "release": "1", "id": "ab" * 8}, **over)
        artifact = self.artifact(row)
        builder.complete(admitted["request"], artifact, receipt=row["id"])
        return builder, admitted, row, artifact

    def test_a_valid_completion_authority_binds_at_every_land_door(self):  # noqa: VACUOUS_ASSERTION — assert_every_land_door_admits() asserts True at every door and names the Fab door
        builder, admitted, row, _artifact = self.completed()
        result, err = fabgate.reconcile(builder, admitted["request"],
                                        admitted["handle"], self.repo)
        self.assertIsNone(err, err)
        self.assertEqual((result["receipt"], result["verdict"]),
                         (row["id"], "imported"))
        self.lane_level(row)
        self.assert_every_land_door_admits(row, gateimport.LAND_FAB)

    def test_an_authority_whose_digest_is_not_the_artifact_refuses_at_import(self):  # noqa: VACUOUS_ASSERTION — the digest refusal text and the bind refusal are asserted positively; the valid-authority arm binds the same fixture
        builder, admitted, row, artifact = self.completed()
        authority = fabfix.FabGateBase.authority(
            self, admitted["request"], admitted["handle"], artifact)
        self.assertEqual(authority["artifact_sha256"], "d" * 64)
        imported, _verdict, err = gateimport.import_fab_receipt(
            artifact, self.repo, authority)
        self.assertIsNone(imported)
        self.assertIn("digest disagrees", err)
        state, _rid, why = gate.bind(gate.evidence_line(row), self.head,
                                     repo_id=self.repo, need=gate.NEED_LAND)
        self.assertEqual(state, "REFUSED")
        self.assertIn("no minted gate receipt", why)

    def test_a_completion_for_another_artifact_than_the_import_refuses_at_land(self):  # noqa: VACUOUS_ASSERTION — the completion import and the lane-level binding are asserted positively before every land refusal
        """The receipt was placed by a generic import of artifact A; a Fab
        completion then arrives whose authority names artifact B (the same
        receipt, other bytes). The completion row is self-consistent, and the
        land must still refuse: its artifact_sha256 is not the artifact whose
        import placed the receipt."""
        builder, admitted, row, artifact = self.completed()
        self.generic_import(row)
        other = self.artifact(row, name="other.jsonl", sort_keys=True)
        with open(other, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        with open(artifact, "rb") as fh:
            self.assertNotEqual(digest, hashlib.sha256(fh.read()).hexdigest())
        authority = fabfix.FabGateBase.authority(
            self, admitted["request"], admitted["handle"], other)
        authority.update(artifact_sha256=digest, sha=self.head)
        imported, _verdict, err = gateimport.import_fab_receipt(
            other, self.repo, authority)
        self.assertIsNone(err, err)
        self.assertEqual(imported["id"], row["id"])
        rows, err = gateimport._fab_completion_rows()
        self.assertIsNone(err, err)
        self.assertEqual([r["receipt"] for r in rows], [row["id"]])
        self.lane_level(row)
        self.assert_every_land_door_refuses(row, needle="artifact_sha256")

    def test_a_completion_in_another_repository_does_not_authorize_this_one(self):  # noqa: VACUOUS_ASSERTION — the same receipt is asserted True in the repository it was imported for
        builder, admitted, row, _artifact = self.completed()
        result, err = fabgate.reconcile(builder, admitted["request"],
                                        admitted["handle"], self.repo)
        self.assertIsNone(err, err)
        other = os.path.join(self.tmp, "other")
        _git(self.tmp, "clone", "-q", self.repo, other)
        proven, why = gate.land_provenance(row, other)
        self.assertIs(proven, False, why)
        self.assertIs(gate.land_provenance(row, self.repo)[0], True)


class RoutedCustodyArms(ProvenanceBase):
    """(c): gateroute's challenge-framed whole-suite custody."""

    def custody(self, row):
        return {"receipt": copy.deepcopy(row), "head": row["head"],
                "tree": row["tree"], "node": row["host"]["node"],
                "challenge": uuid.uuid4().hex}

    def test_a_routed_whole_suite_binds_at_every_land_door(self):  # noqa: VACUOUS_ASSERTION — assert_every_land_door_admits() asserts True at every door and names the routed door
        row = self.receipt()
        imported, verdict, err = gateimport.import_routed_suite(
            self.artifact(row), self.repo, self.custody(row))
        self.assertIsNone(err, err)
        self.assertEqual((imported["id"], verdict), (row["id"], "imported"))
        self.assert_every_land_door_admits(row, gateimport.LAND_ROUTED)

    def test_custody_for_a_different_receipt_object_imports_nothing(self):
        row = self.receipt()
        custody = self.custody(self.receipt(ran=21))
        imported, _verdict, err = gateimport.import_routed_suite(
            self.artifact(row), self.repo, custody)
        self.assertIsNone(imported)
        self.assertIn("differs from the receipt object", err)
        rows, err = gateimport._door_rows(gateimport.route_custodies_path(),
                                          gateimport._route_custody_err, "c")
        self.assertEqual((rows, err), ([], None))


class LocalMintArms(ProvenanceBase):
    """(a): helm's own runner, through the real `gate.run` mint."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        _tmphome.pin_admission(self, proc=os.environ["HELM_PROC"])
        _tmphome.helm_tree(self, self.repo)
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "tests", "__init__.py"), "w"):
            pass
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "ships helm")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

    def mint(self, label=None):
        with serial_process(ran=3):
            row, err = gate.run(repo=self.repo, label=label)
        self.assertIsNone(err, err)
        self.assertEqual((row["status"], row["v"]), ("OK", 4), row)
        return row

    def test_a_locally_minted_serial_OK_binds_at_every_land_door(self):
        row = self.mint()
        rows, err = gateimport._door_rows(gateimport.mints_path(),
                                          gateimport._mint_err, "mints")
        self.assertIsNone(err, err)
        self.assertEqual([(r["receipt"], r["head"], r["tree"]) for r in rows],
                         [(row["id"], self.head, self.tree)])
        self.assert_every_land_door_admits(row, gateimport.LAND_LOCAL)

    def test_a_mint_record_with_its_content_edited_does_not_count(self):
        row = self.mint()
        path = gateimport.mints_path()
        with open(path) as fh:
            record = json.loads(fh.readline())
        record["receipt"] = "f" * 16
        with open(path, "w") as fh:
            fh.write(json.dumps(record) + "\n")
        proven, why = gate.land_provenance(row, self.repo)
        self.assertIsNone(proven)
        self.assertIn("content id does not resolve", why)


class ForwardOnlyArms(ProvenanceBase):
    """OI 02:21Z: the flip governs NEW land decisions and never re-reads the
    proof of a land that already happened. The instant is the recorded
    ACTIVATION; a receipt helm's own ledger held then, placed in this
    repository before it, is judged by the rule in force at its placement."""

    ACTIVATE_AT = None

    def after(self, ts):
        """One second after a helm-written timestamp."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) + 1))

    def fold(self, row):
        """Everything a land door says about this receipt at this tip."""
        rungs = foldcheck.check(self.repo, self.head, gate_ref="gate:"
                                + row["id"], fetch=False)
        return {"foldcheck": foldcheck.report(rungs),
                "landgate": landgate.gate_binds_tree(
                    row["id"], None, repo=self.repo, tip=self.head),
                "bind": gate.bind(gate.evidence_line(row), self.head,
                                  repo_id=self.repo, need=gate.NEED_LAND)}

    def binding_ts(self, row, repo=None):
        rows, err = gateimport._binding_rows()
        self.assertIsNone(err, err)
        identity = gateimport._repo_identity(repo or self.repo)
        return [b["ts"] for b in rows if b["receipt"] == row["id"]
                and b["importing_repo"] == identity][0]

    def test_a_land_that_happened_on_a_classic_receipt_reads_exactly_as_before(self):  # noqa: VACUOUS_ASSERTION — the before/after answers are asserted EQUAL and the before answer asserted to PASS; the new-receipt control refuses on the same doors
        classic = self.generic_import(self.receipt())
        before = self.fold(classic)
        self.assertEqual(before["bind"][0], "VERIFIED", before["bind"])
        self.assertIn("tree-vs-gate", "\n".join(before["foldcheck"]))
        self.assertEqual(before["landgate"][0], landgate.OK)
        record = self.activate(self.after(self.binding_ts(classic)))
        self.assertIn(classic["id"], record["receipts"])
        self.assertEqual(self.fold(classic), before)
        # CONTROL, deciding NOW: a classic receipt placed after the flip, on
        # the same doors, answers the flip.
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("two\n")
        _git(self.repo, "commit", "-qam", "two")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        fresh = self.generic_import(self.receipt())
        self.assert_every_land_door_refuses(fresh)

    def test_an_old_receipt_imported_here_after_the_flip_answers_the_flip(self):  # noqa: VACUOUS_ASSERTION — the same receipt is asserted EQUAL to (True, '') in the repository it was placed in before the flip
        classic = self.generic_import(self.receipt())
        self.activate(self.after(self.binding_ts(classic)))
        other = os.path.join(self.tmp, "other")
        _git(self.tmp, "clone", "-q", self.repo, other)
        with mock.patch.object(gateimport.pk, "now_ts",
                               return_value="2099-06-01T00:00:00Z"):
            imported, _verdict, err = gateimport.import_receipt(
                self.artifact(classic), other)
        self.assertIsNone(err, err)
        proven, why = gate.land_provenance(classic, other)
        self.assertIs(proven, False, why)
        self.assertIn("after the flip's activation", why)
        self.assertEqual(gate.land_provenance(classic, self.repo), (True, ""))

    def test_a_dormant_flip_answers_as_before_and_a_torn_one_enforces(self):
        forged = self.generic_import(self.flipped())
        self.assertEqual(gate.land_provenance(forged, self.repo), (True, ""))
        state, _record, _why = gateimport.activation_state()
        self.assertEqual(state, gateimport.FLIP_INACTIVE)
        # ONE HALF WRITTEN: never read as inactive, and never a pass.
        from helm.foldcompose import _write_once
        _record_path, latch_path = gateimport.activation_paths()
        self.assertTrue(_write_once(latch_path, {
            "format": gateimport.ACTIVATION_FORMAT,
            "ts": "2000-01-01T00:00:00Z",
            "receipts_sha256": gateimport._ids_digest([])}))
        state, _record, _why = gateimport.activation_state()
        self.assertEqual(state, gateimport.FLIP_UNKNOWN)
        proven, why = gate.land_provenance(forged, self.repo)
        self.assertIsNone(proven)
        self.assertIn("UNKNOWN", why)
        self.assert_every_land_door_refuses(forged)

    def test_only_the_integrator_activates_and_only_once(self):
        with mock.patch.dict(os.environ, {"HELM_WORK_INTEGRATOR": ""}):
            self.assertEqual(gateimport.cmd_provenance(["--activate"]), 2)
        self.assertEqual(gateimport.activation_state()[0],
                         gateimport.FLIP_INACTIVE)
        with mock.patch.dict(os.environ, {"HELM_WORK_INTEGRATOR": "1"}):
            self.assertEqual(gateimport.cmd_provenance(["--activate"]), 0)
            self.assertEqual(gateimport.cmd_provenance(["--activate"]), 1)
        self.assertEqual(gateimport.activation_state()[0],
                         gateimport.FLIP_ACTIVE)


class ForwardOnlyRowArms(_focus.FocusBase):
    """The landed/closed state of a row whose land went through a classic
    receipt is the same before and after the flip is activated."""

    def _dispatch(self, tip):
        """The review row `FocusVerdictArms` opens, for this arm's approve."""
        from helm import dispatches
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)
        row, err = dispatches.add("reviewer", "a-lane", tip, kind="review",
                                  repo=self.repo, notify=False, _reason=True,
                                  new_work=True)
        self.assertIsNone(err, err)
        return row

    def test_a_rows_state_and_its_fold_are_unchanged_by_the_activation(self):  # noqa: VACUOUS_ASSERTION — the approve, the row's projected state and the fold are asserted before, then asserted EQUAL after the activation
        from helm import dispatches, landreq
        tree = self._git("rev-parse", "HEAD^{tree}")
        ident = gate.interpreter()
        row = {"v": 4, "event": "gate", "ts": "2026-09-24T23:00:00Z",
               "repo_id": "/mnt/fab-hot/wt/" + "b" * 64, "head": self.head,
               "tree": tree, "dirty": False, "head_after": self.head,
               "tree_after": tree, "dirty_after": False, "interpreter": ident,
               "host": gate.host(),
               "argv": [ident["executable"]] + list(fabgate.WHOLE_ARGV),
               "suite": True, "label": None, "rc": 0, "wall": 41.2,
               "status": "OK", "ran": 9, "skipped": 0, "detail": "",
               "elapsed": 41.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row["id"] = gate._receipt_id(row)
        artifact = os.path.join(self.tmp, "classic.jsonl")
        with open(artifact, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        _got, verdict, err = gateimport.import_receipt(artifact, self.repo)
        self.assertIsNone(err, err)
        d = self._dispatch(self.head)
        got, err = dispatches.mark_verdict(
            d["id"], self.head, gate.evidence_line(row), "approve")
        self.assertIsNone(err, err)

        def seen():
            lrs, unavailable = landreq.project()
            self.assertIsNone(unavailable, unavailable)
            lr = lrs[d["id"]]
            rungs = foldcheck.check(self.repo, self.head,
                                    gate_ref="gate:" + row["id"], fetch=False)
            return (lr.get("state"), landreq.ready_rung(lr),
                    foldcheck.report(rungs))

        before = seen()
        record, err = gateimport.activate(ts="2099-01-01T00:00:00Z")
        self.assertIsNone(err, err)
        self.assertIn(row["id"], record["receipts"])
        self.assertEqual(seen(), before)


class LaunchLabelArms(ProvenanceBase):
    """LABEL (task/3066 defect 2): `--label train200` rides the job to the
    node and the receipt names it, when the Fab that answers says it takes
    `gate submit --label`."""

    def measure(self, options):
        event = {"v": 2, "event": "gate-measure", "host": "snoozy",
                 "interpreter": dict(INTERPRETER), "runner": dict(RUNNER),
                 "route_preimage": "f" * 64}
        if options is not None:
            event["submit_options"] = options
        return json.dumps(event) + "\n"

    def identity(self, options, label="train200"):
        request = {"project": "p", "room": self.repo, "head": self.head,
                   "trunk": self.head, "label": label}
        return gatewindow.job_identity(
            request, fab=lambda argv, timeout=None: (
                0, self.measure(options), ""))

    def test_the_label_rides_the_submit_only_when_fab_takes_it(self):  # noqa: VACUOUS_ASSERTION — the forwarded argv tail and label are asserted EQUAL first; the absence is the control on the same argv
        identity, err = self.identity(["--label"])
        self.assertIsNone(err, err)
        self.assertEqual(identity["label"], "train200")
        argv = gatewindow.submit_argv(self.repo, identity["request"],
                                      label=identity["label"])
        self.assertEqual(argv[-2:], ["--label", "train200"])
        # A Fab that takes no such option would refuse the WHOLE submit, so
        # the door leaves the label on its own record rather than lose the
        # suite; the key is the same either way (the label is no part of it).
        bare, err = self.identity(None)
        self.assertIsNone(err, err)
        self.assertIsNone(bare["label"])
        self.assertNotIn("--label", gatewindow.submit_argv(
            self.repo, bare["request"], label=bare["label"]))
        self.assertEqual(bare["key"], identity["key"])

    def test_only_a_durable_job_reads_the_forwarded_label(self):
        self.assertEqual(gate.fab_job_label({
            "FAB_GATE_GENERATION": "run-x", "FAB_GATE_LABEL": " train200\n"}),
            "train200")
        self.assertIsNone(gate.fab_job_label({"FAB_GATE_LABEL": "train200"}))
        self.assertIsNone(gate.fab_job_label({"FAB_GATE_GENERATION": "run-x"}))

    def test_the_launch_label_is_the_label_the_node_mints(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the minted label are asserted EQUAL on both runs
        """End to end on the node's side: the label the window forwarded
        arrives as Fab's environment, `helm gate run` (no --label, inside a
        durable job) mints it, and the receipt the hub imports names it."""
        identity, err = self.identity(["--label"])
        self.assertIsNone(err, err)
        argv = gatewindow.submit_argv(self.repo, identity["request"],
                                      label=identity["label"])
        forwarded = argv[argv.index("--label") + 1]
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        _tmphome.pin_admission(self, proc=os.environ["HELM_PROC"])
        _tmphome.helm_tree(self, self.repo)
        _git(self.repo, "commit", "-qm", "ships helm")
        os.environ.update({"FAB_ID": "gate-node-run",
                           "FAB_GATE_GENERATION": "run-" + "a" * 32,
                           "FAB_GATE_LABEL": forwarded})
        minted = {}
        real_run = gate.run

        def run(**kwargs):
            with serial_process(ran=2):
                row, err = real_run(**kwargs)
            minted["row"] = row
            return row, err

        with mock.patch.object(gate, "run", side_effect=run):
            rc = gate._cmd_run(["--repo", self.repo, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(minted["row"]["label"], "train200")
        # An explicit --label still wins over the forwarded one (on a new
        # tree: a green tree never runs its whole suite twice).
        with open(os.path.join(self.repo, "f.txt"), "a") as fh:
            fh.write("two\n")
        _git(self.repo, "commit", "-qam", "two")
        with mock.patch.object(gate, "run", side_effect=run):
            rc = gate._cmd_run(["--repo", self.repo, "--label", "mine",
                                "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(minted["row"]["label"], "mine")


class StandingArms(ProvenanceBase):
    """STANDING (task/3066 defect 3): a durable-door receipt's standing is
    measured in the repository it was imported for, not in the reader's cwd
    and never at the node's worktree path its repo_id names."""

    def test_standing_resolves_in_the_repository_the_receipt_was_placed_in(self):
        row = self.generic_import(self.receipt())
        elsewhere = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(elsewhere)
        with mock.patch.object(os, "getcwd", return_value=elsewhere):
            got = gate.trunk_standing(row)
        self.assertEqual(got["state"], vcs.ANCESTOR, got)
        self.assertIn(self.repo, got["reason"])
        # CONTROL on the same observable: a receipt no door placed anywhere
        # is still read in the cwd, where nothing answers.
        stray = self.receipt(ran=99)
        with mock.patch.object(os, "getcwd", return_value=elsewhere):
            got = gate.trunk_standing(stray)
        self.assertEqual(got["state"], vcs.UNKNOWN, got)

    def test_a_cwd_inside_the_placed_repository_answers_for_itself(self):
        row = self.generic_import(self.receipt())
        root, placed = gate.standing_root(row, cwd=self.repo)
        self.assertEqual((root, placed), (self.repo, None))


def setUpModule():
    # ForwardOnlyRowArms writes a dispatch row, and every row a test writes
    # asks the live-seat census: a measured empty fleet stands in for it.
    _tmphome.pin_live_seats()


if __name__ == "__main__":
    unittest.main()
