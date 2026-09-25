"""Prospective compose/close contract, with synthetic stores and real Git.

The gate child is simulated by the existing serial-process fixture; receipt
production, authority binding, verdict production, compose, locked close and
replay are real. These tests make no claim that a suite actually ran in a
fixture. Owner specimens are independently literal, not generated from the
production guard list.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import _tmphome
from helm import (compose_contract as contract, dispatches, eventledger, gate,
                  foldcompose, home, landreq, pk, proxywatch, registry, seats, seats_common, store,
                  verdicts)
from tests._gate_receipt import serial_process
from tests._verdict import native_author


# Independent expected owners: removing a production owner must fail its hostile
# arm, even if somebody edits the production list and its explanatory comments.
OWNER_SPECIMENS = (
    "helm/compose_contract.py", "helm/work/_guard.py", "helm/work/_cli.py",
    "helm/nevertrack.py", "helm/vacuous_assertion.py", "helm/hostpath_guard.py",
    "helm/inflight_gate.py", "helm/hardcode.py", "helm/docref_guard.py",
    "helm/conflict_marker.py", "helm/world_prose_guard.py", "helm/seatname_guard.py",
    "helm/trailer_rung.py", "helm/lane_discipline.py", "helm/hooks.py",
    "helm/cred/_common.py", "helm/cred/cli.py", "helm/record.py",
    "helm/hookrun.py", "helm/foldcompose.py", "helm/foldcheck.py",
    "helm/injection_schema.py", "helm/inject/_ledger.py", "helm/seat_ledger.py",
    "helm/eventledger.py", "helm/dispatches.py", "helm/dispatches_close.py",
    "helm/dispatches_cli.py", "helm/landreq.py", "helm/landreq_close.py",
    "helm/landreq_cli.py",
    "helm/verdicts.py", "helm/verdict_tier.py", "helm/store/policy_history.py",
    "helm/store/load.py", "helm/seats_roster.py", "helm/seats_common.py",
    "helm/gate.py", "helm/gateauthority.py", "helm/gateimport.py", "helm/fabgate.py",
    "helm/landgate.py", "helm/rowstate.py", "helm/rowworld.py", "helm/vcs.py",
    "helm/actors.py", "helm/whoami.py", "helm/mcpd.py", "helm/tasks.py",
    "helm/store/write.py", "helm/premise/_chain.py", "helm/work/_claims.py",
    "helm/seats_claims.py", "helm/seat_lifecycle.py", "helm/seat_exit_owner.py",
    "helm/seat_reassign.py", "helm/seats_rename.py",
)


def brief(base, tip, effects="reversible"):
    return ("helm-compose-land/1 base=" + base + " tip=" + tip
            + " bounded=1 effects=" + effects + "\n"
            "Review the bounded text change and its effects.")


def evidence(body):
    digest = hashlib.blake2b(body.encode("utf-8"), digest_size=16).hexdigest()
    return ("helm-compose-land/1 brief=" + digest
            + " pass=complete findings=0 effects=confirmed")


def specimen():
    body = brief("a" * 40, "b" * 40)
    root = {"id": "1" * 32, "tip": "a" * 40, "kind": "build"}
    row = {"id": "2" * 32, "kind": "review", "status": "verdict",
           "tip": "b" * 40, "reviewed_tip": "b" * 40,
           "chain_root": root["id"], "polarity": "concur", "basis": "measured",
           "message_body": body,
           "message_hash": hashlib.blake2b(body.encode(), digest_size=16).hexdigest(),
           "verdict_ref": evidence(body)}
    return row, {root["id"]: root, row["id"]: row}


class HermeticCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="helm-compose-contract-")
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": str(self.tmp / "home"),
            "HELM_ADOPTED_DIR": str(self.tmp / "adopted"),
            "HELM_CHAT_DIR": str(self.tmp / "chat"),
            "HELM_CHAT_NODE_URL": "", "HELM_CHAT_NAME": "author-fixture",
            "HELM_CHAT_ROOM": "fixture", "HELM_PROC": str(self.tmp / "proc"),
            contract.WRITER_ENV: "1",
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
                    "HELM_CELL_BIN", "HELM_CELL_PROFILE"):
            os.environ.pop(key, None)
        (self.tmp / "adopted").mkdir()
        (self.tmp / "proc").mkdir()
        # ADMISSION ON A FIXTURE BOX: HELM_PROC never reached it (task/1740).
        from tests._tmphome import pin_admission
        pin_admission(self, proc=str(self.tmp / "proc"))


class ParserAndEffectTest(HermeticCase):
    def test_exact_positive_statement_and_native_hash(self):
        row, rows = specimen()
        got, err = contract.parse(row, rows)
        self.assertEqual(got, {"base": "a" * 40, "tip": "b" * 40,
                               "brief": row["message_hash"], "effects": ["reversible"]})
        self.assertIsNone(err)
        self.assertLessEqual(len(row["verdict_ref"]), 256)
        clean = contract._diff_records(b"M\0docs/example.txt\0")
        self.assertEqual(contract.effects(got["effects"], clean), ("REVERSIBLE", None))

    def test_absence_ambiguity_truncation_and_positive_zero_are_not_authority(self):
        row, rows = specimen()
        self.assertEqual(contract.parse(row, rows)[0]["tip"], "b" * 40)
        mutations = (
            {"message_body": None}, {"message_body": ""}, {"message_body": 7},
            {"message_body": row["message_body"] + dispatches.BODY_TRUNCATED_MARK},
            {"message_body": row["message_body"] + "\nhelm-compose-land/1"},
            {"message_body": row["message_body"] + "\ud800"},
            {"message_hash": "0" * 32}, {"kind": "build"}, {"status": "open"},
            {"polarity": "approve"}, {"basis": None}, {"basis": "inferred"},
            {"tip": "c" * 40}, {"reviewed_tip": "c" * 40}, {"chain_root": None},
            {"verdict_ref": row["verdict_ref"].replace(" findings=0", "")},
            {"verdict_ref": row["verdict_ref"].replace("findings=0", "findings=1")},
            {"verdict_ref": row["verdict_ref"].replace("pass=complete", "pass=partial")},
            {"verdict_ref": row["verdict_ref"].replace("effects=confirmed", "effects=unknown")},
            {"verdict_ref": row["verdict_ref"] + " findings=0"},
            {"exit_answer": "worse-than-main"}, {"worse_than_main_paths": ["x"]},
        )
        for change in mutations:
            with self.subTest(change=repr(change)):
                bad = dict(row, **change)
                got, why = contract.parse(bad, dict(rows, **{bad["id"]: bad}))
                self.assertIsNone(got)
                self.assertIn("UNKNOWN", why)
        old = dict(row)
        del old["message_body"]
        self.assertIn("unavailable", contract.parse(old, rows)[1])
        for unreadable in (None, [], "unavailable"):
            self.assertIn("snapshot unavailable", contract.parse(row, unreadable)[1])

    def test_resigned_bad_grammar_still_refuses(self):
        row, rows = specimen()
        for body in (
                row["message_body"].replace("bounded=1", "bounded=0"),
                row["message_body"].replace("effects=reversible", "effects=reversible,reversible"),
                row["message_body"].replace("effects=reversible", "effects=novel"),
                row["message_body"].split("\n")[0],
                "prefix " + row["message_body"],
                row["message_body"].replace("a" * 40, "a" * 12)):
            with self.subTest(body=body):
                bad = dict(row, message_body=body, verdict_ref=evidence(body),
                           message_hash=hashlib.blake2b(body.encode(), digest_size=16).hexdigest())
                self.assertIn("UNKNOWN", contract.parse(bad, rows)[1])

    def test_each_literal_owner_vetoes_clean_declared_effects(self):
        self.assertEqual(set(OWNER_SPECIMENS), set(contract.PROTECTED_OWNERS))
        clean = [{"status": "M", "paths": ["docs/example.txt"]}]
        self.assertEqual(contract.effects(["reversible"], clean), ("REVERSIBLE", None))
        for owner in OWNER_SPECIMENS:
            for status, paths in (("M", [owner]), ("D", [owner]), ("T", [owner]),
                                  ("R100", [owner, "docs/moved.txt"]),
                                  ("R099", ["docs/moved.txt", owner]),
                                  ("C050", [owner, "docs/copied.txt"])):
                with self.subTest(owner=owner, status=status, paths=paths):
                    state, why = contract.effects(["reversible"], [{"status": status, "paths": paths}])
                    self.assertEqual(state, "CONTRACT")
                    self.assertIn(owner, why)

    def test_no_protected_owner_is_listed_twice(self):
        """A REGISTRY MERGED BY UNION CAN CARRY ONE ENTRY TWICE, and a tuple
        does not object. Membership still answers right, which is why nothing
        notices; but the next reader counts entries, and a removal that
        deletes one copy leaves the path protected by the other."""
        def twice(owners):
            return sorted({o for o in owners if owners.count(o) > 1})
        self.assertEqual(["a"], twice(["a", "b", "a"]),
                         "the control: this detector does find a repeat")
        self.assertEqual([], twice(list(contract.PROTECTED_OWNERS)))

    def test_a_protected_owner_DECLARED_SATELLITES_are_protected_too(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control IS present on the same walk (assertTrue(declared) refuses a pass that found no satellite at all), but `declared` is built in a loop and this rung credits a control only from the same CALL's other channels, which a loop-accumulated dict cannot be
        """A PROTECTED OWNER THAT SHEDS A SATELLITE TAKES ITS PROTECTION WITH
        IT UNLESS SOMEBODY REMEMBERS, and this arm is instead of remembering.

        `PROTECTED_OWNERS` names exact FILES, so the protection is bound to a
        path and not to the code inside it. The 1 MiB never-track ceiling
        forces protected modules to split -- that is not hypothetical, it is
        the reason `landreq` has satellites at all -- and when it does, the
        code that earned the entry MOVES while the entry keeps reading as
        though it still covered that code. Nothing refused, nothing warned:
        a compose-land could carry the close ladder through bounded review
        while the list still said `helm/landreq.py` was protected.

        THE OWNER TABLE IS THE ONLY DECLARATION OF WHERE THE CODE WENT, and
        the retired-name rung already reads it for exactly this reason, so
        asking it here keeps both guards deriving from one fact. A module that
        declares no satellites contributes nothing, which is why this is safe
        to run over the whole list.
        """
        import ast
        import io
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        declared = {}
        for owner in contract.PROTECTED_OWNERS:
            path = os.path.join(root, owner)
            if not os.path.exists(path):
                continue
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (SyntaxError, ValueError):
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "_OWNER_NAMES"
                           for t in node.targets):
                    continue
                for pair in getattr(node.value, "elts", ()):
                    parts = getattr(pair, "elts", ())
                    if parts and isinstance(parts[0], ast.Constant):
                        satellite = "%s/%s.py" % (os.path.dirname(owner),
                                                  parts[0].value)
                        declared[satellite] = owner
        # THE UNCONDITIONAL POSITIVE CONTROL, on the same walk: a pass that
        # found no satellite at all would satisfy the absence below while
        # proving nothing, and that is precisely what this walk returns if the
        # owner-table shape ever changes under it.
        self.assertTrue(declared,
                        "no protected owner declares a satellite, so this "
                        "arm proved nothing — check the _OWNER_NAMES shape")
        unprotected = sorted(s for s in declared
                             if s not in set(contract.PROTECTED_OWNERS))
        self.assertEqual([], unprotected,
                         "these files hold code moved out of a PROTECTED "
                         "owner and are not protected themselves: %s"
                         % "; ".join("%s (from %s)" % (s, declared[s])
                                     for s in unprotected))

    def test_declared_effects_independently_veto_clean_diff(self):
        clean = [{"status": "M", "paths": ["docs/example.txt"]}]
        self.assertEqual(contract.effects(["reversible"], clean)[0], "REVERSIBLE")
        for effect in ("schema", "ledger-version", "hooks", "cross-seat",
                       "administrative-terminal", "ledger-mutation"):
            with self.subTest(effect=effect):
                self.assertEqual(contract.effects([effect], clean)[0], "CONTRACT")
                self.assertEqual(contract.effects(["reversible", effect], clean)[0], "CONTRACT")
        for declared in (None, [], ["unknown"], ["novel"], ["reversible", "reversible"], [1]):
            self.assertEqual(contract.effects(declared, clean)[0], "UNKNOWN")

    def test_no_glob_or_whitespace_path_guessing(self):
        raw = b"M\0docs/helm/work/_guard.py\0A\0docs/space and\nnewline.txt\0"
        parsed = contract._diff_records(raw)
        self.assertEqual(parsed[1]["paths"], ["docs/space and\nnewline.txt"])
        self.assertEqual(contract.effects(["reversible"], parsed), ("REVERSIBLE", None))
        for raw in (b"", b"M\0x", b"Q\0x\0", b"R100\0old\0", b"C101\0a\0b\0",
                    b"M\0\xff\0", b"M\0../x\0", b"M\0/x\0", b"M\0a//b\0",
                    b"M\0a\\b\0", b"M\0x\0garbage\0"):
            with self.subTest(raw=raw):
                self.assertIsNone(contract._diff_records(raw))
        for diff in (None, [], [None], [{"status": "M", "paths": "x"}],
                     [{"status": "M", "paths": ["x"], "extra": True}]):
            self.assertEqual(contract.effects(["reversible"], diff)[0], "UNKNOWN")

    def test_manifest_bound_duplicate_keys_and_standing_version(self):
        path = self.tmp / "manifest.json"
        good = '{"compose_land_version":1,"dry_run":false}'
        path.write_text(good, encoding="utf-8")
        self.assertEqual(contract.read_manifest(path), (json.loads(good), None))
        for text in ('{"compose_land_version":1,"compose_land_version":1,"dry_run":false}',
                     '{"compose_land_version":true,"dry_run":false}',
                     '{"compose_land_version":1,"dry_run":true}',
                     '[' * 2000, ' ' * (1024 * 1024 + 1)):
            path.write_text(text, encoding="utf-8")
            self.assertIsNone(contract.read_manifest(path)[0])
        self.assertIn("unreadable", contract.read_manifest(self.tmp / "missing")[1])

    def test_all_writer_doors_default_off_and_global_concur_unchanged(self):
        os.environ.pop(contract.WRITER_ENV)
        self.assertIn("disabled", contract.writer_error())
        out, why = landreq.close("2" * 32, "landed", compose_manifest={}, compose_gate="gate:" + "c" * 16)
        self.assertIsNone(out)
        self.assertIn("disabled", why)
        out, why = dispatches._record_close_proven("2" * 32, "landed", "b" * 40, close_proof_version=3)
        self.assertIsNone(out)
        self.assertIn("disabled", why)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = landreq.cmd_lr(["compose", "2" * 32, "--bounded-concur"])
        self.assertEqual(rc, 1)
        self.assertIn("disabled", stderr.getvalue())
        self.assertNotIn("concur", verdicts.WORK_POLARITIES)
        self.assertNotIn("concur", dispatches._CLOSE_POLARITY["landed"])
        self.assertNotEqual(landreq.VERDICT_STATE.get("concur"), "READY")


class LifecycleTest(HermeticCase):
    def setUp(self):
        super().setUp()
        self.repo = str(self.tmp / "repo")
        Path(self.repo).mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.invalid")
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. An adopter project declares its own instead.
        _tmphome.helm_tree(self, self.repo)
        self.base = self.commit("base.txt", "base\n")
        self.trunk = self.commit("trunk.txt", "unrelated trunk\n")
        self.git("checkout", "-q", "-b", "lane/fixture", self.base)
        self.tip = self.commit("docs/example.txt", "bounded text\n")
        self.git("checkout", "-q", "main")
        _tmphome.pin_dispatch_home(self, self.repo)
        registry.save({"version": 1, "projects": {"fixture": {"path": self.repo}}})
        activation, err = foldcompose.activate("fixture", self.repo, self.base)
        self.assertIsNone(err, err)
        self.assertTrue(Path(activation).is_file())
        live = mock.patch.object(proxywatch, "_live_seats", return_value=(set(), None, {}))
        live.start()
        self.addCleanup(live.stop)
        self.addCleanup(landreq._GATE_INDEX_MEMO.clear)
        self.root = dispatches.add("builder-fixture", "fixture", ref=self.base,
                                   repo=self.repo, kind="build", new_work=True,
                                   force=True, notify=False)
        self.assertIsNotNone(self.root)

    def git(self, *args, cwd=None):
        proc = subprocess.run(["git", "-C", cwd or self.repo, *args],
                              text=True, capture_output=True, check=True)
        return proc.stdout.strip()

    def commit(self, path, text):
        file = Path(self.repo) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        self.git("add", "--", path)
        self.git("commit", "-qm", "fixture change")
        return self.git("rev-parse", "HEAD")

    def review(self, *, tip=None, body=None, statement=None, basis="measured", polarity="concur"):
        tip = tip or self.tip
        body = body if body is not None else brief(self.base, tip)
        row, err, _sent = dispatches.send(
            "review-fixture", "fixture", body, tip, repo=self.repo,
            kind="review", supersedes=self.root["id"], force=True, sign=False)
        self.assertIsNone(err, err)
        self.assertIsNotNone(row)
        with native_author(self), mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "review-fixture"}):
            verdict, err = dispatches.mark_verdict(
                row["id"], tip, statement or evidence(body), polarity=polarity,
                basis=basis, bind_author=True)
        self.assertIsNone(err, err)
        self.assertEqual(verdict["verdict_version"], 4)
        return verdict

    def peer(self, scope, **kw):
        return self.review(body=brief(self.base, self.tip) + "\n" + scope, **kw)

    def pending_peer(self):
        row, err, _sent = dispatches.send(
            "pending-review-fixture", "fixture", "Review the same exact tip.",
            self.tip, repo=self.repo, kind="review", supersedes=self.root["id"],
            force=True, sign=False)
        self.assertIsNone(err, err)
        self.assertIsNotNone(row)
        self.assertEqual(dispatches.bound_tip(row), self.tip)
        self.assertFalse(row.get("verdict_ref"))
        return row

    def assert_discharge_refuses(self, row, note, review):
        history = self.events()
        for preview in (True, False):
            out, err = landreq.close(row["id"], "discharged", evidence=note,
                                     dry_run=preview, fan_out=False)
            self.assertIsNone(out)
            self.assertIn("close --reason discharged refused:", err)
            self.assertEqual(self.events(), history)
        # Bypass the ladder as an independent hostile caller: the lock must
        # refuse the same proposed authority without appending an event.
        out, err = dispatches._record_close_proven(
            row["id"], "discharged", None, evidence=note,
            discharging_id=review["id"], discharging_tip=review["reviewed_tip"],
            discharge_tier=dispatches.DISCHARGE_TIER_CHAIN)
        self.assertIsNone(out)
        self.assertIn("discharge refused", err)
        self.assertEqual(self.events(), history)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertFalse(rows[row["id"]].get("close_reason"))

    def assert_discharge_preview_and_live(self, row, approved):
        note = "landed review " + approved["id"]
        history = self.events()
        preview, err = landreq.close(row["id"], "discharged", evidence=note,
                                     dry_run=True, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(preview["discharging_id"], approved["id"])
        self.assertEqual(self.events(), history)
        closed, err = landreq.close(row["id"], "discharged", evidence=note,
                                    fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(closed["close_reason"], "discharged")
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(rows[row["id"]]["discharging_id"], approved["id"])
        self.assertGreater(len(self.events()), len(history))

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landreq.cmd_lr(list(args))
        return rc, out.getvalue(), err.getvalue()

    def compose(self, row, *args):
        rc, text, err = self.command("compose", row["id"], "--bounded-concur", "--json", *args)
        self.assertEqual(rc, 0, err + text)
        manifest = json.loads(text)
        self.assertEqual(manifest["compose_land_version"], 1)
        self.assertTrue(manifest["members"][0]["carries"])
        path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        if manifest["dry_run"]:
            self.assertFalse(path.exists())
        else:
            self.assertEqual(json.loads(path.read_text()), manifest)
        return manifest

    def landed(self, row):
        manifest = self.compose(row)
        with serial_process(ran=3):
            receipt, err = gate.run(repo=manifest["room"])
        self.assertIsNone(err, err)
        self.assertEqual(receipt["tree"], manifest["composed_tree"])
        self.git("merge", "--ff-only", manifest["composed_tip"])
        return manifest, "gate:" + receipt["id"]

    def close(self, row, manifest, receipt, **kw):
        return landreq.close(row["id"], "landed", trunk="main", live=True,
                             compose_manifest=manifest, compose_gate=receipt, **kw)

    def events(self):
        return eventledger.events(dispatches.ledger_path())

    def assert_fold(self, manifest, receipt=None, succeeds=True):
        proof = foldcompose.composition_proof(
            self.repo, "fixture", manifest["result_tip"], gate_ref=receipt)
        self.assertTrue(proof, proof)
        self.assertEqual(all(ok is True for _name, ok, _why in proof), succeeds, proof)
        return proof

    def mixed_multicommit(self):
        self.git("checkout", "-q", "lane/fixture")
        self.tip = self.commit("docs/second.txt", "second bounded commit\n")
        self.git("checkout", "-q", "main")
        bounded = self.review()
        self.git("checkout", "-q", "-b", "lane/ordinary", self.base)
        self.commit("docs/ordinary.txt", "ordinary first\n")
        tip = self.commit("docs/ordinary-second.txt", "ordinary second\n")
        with serial_process(ran=3):
            receipt, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "main")
        ordinary = self.review(tip=tip, body="Review both ordinary commits.",
                               statement="reviewed gate:" + receipt["id"], polarity="approve")
        manifest = self.compose(bounded, ordinary["id"])
        self.assertEqual(len(manifest["cars"]), 4)
        self.assertEqual([c["row"] for c in manifest["cars"]],
                         [bounded["id"], bounded["id"], ordinary["id"], ordinary["id"]])
        with serial_process(ran=3):
            whole, err = gate.run(repo=manifest["room"])
        self.assertIsNone(err, err)
        return bounded, ordinary, manifest, "gate:" + whole["id"]

    def test_canonical_mixed_multicommit_fold_and_real_five_rung_route(self):
        bounded, ordinary, manifest, receipt = self.mixed_multicommit()
        path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        self.assertEqual(json.loads(path.read_text()), manifest)
        self.assertFalse(Path(manifest["room"] + ".manifest.json").exists())
        self.assertIn("pending", manifest["fold_authority"])
        before = self.events()
        pending = self.assert_fold(manifest, succeeds=False)
        self.assertEqual([(name, ok) for name, ok, _why in pending if ok is not True],
                         [("bounded-suite", False)])
        with mock.patch.object(foldcompose, "_review_snapshot", wraps=foldcompose._review_snapshot) as snap, \
                mock.patch.object(foldcompose, "_reviewed_covers", wraps=foldcompose._reviewed_covers) as covers:
            self.assert_fold(manifest, receipt)
        self.assertEqual(snap.call_count, 1)
        self.assertEqual(covers.call_count, 2)
        for call in covers.call_args_list:
            self.assertEqual(call.args[1], ordinary["id"])
        self.assertIs(covers.call_args_list[0].args[3], covers.call_args_list[1].args[3])
        # Both ancestry rungs require a FRESH fetch, not a hand-set tracking
        # ref. The origin is another temporary local repo: no network service
        # or weakened rung is involved, and --no-fetch remains a refusal.
        origin = str(self.tmp / "origin.git")
        self.git("init", "--bare", "-q", origin)
        self.git("remote", "add", "origin", origin)
        self.git("push", "-q", "origin", manifest["result_tip"] + ":refs/heads/main")
        rc, text, err = self.command("foldcheck", manifest["result_tip"], "--repo", manifest["room"],
                                     "--gate", receipt, "--no-fetch")
        self.assertEqual(rc, 1, text + err)
        self.assertIn("ff-able", text)
        self.assertIn("origin-has-it", text)
        self.assertIn("NOT PROVEN", text)
        rc, text, err = self.command("foldcheck", manifest["result_tip"], "--repo", manifest["room"],
                                     "--gate", receipt)
        self.assertEqual(rc, 0, text + err)
        self.assertIn("composition proof PASSED", text)
        dirty = Path(manifest["room"]) / "untracked.txt"
        dirty.write_text("dirty room must still refuse\n")
        rc, text, err = self.command("foldcheck", manifest["result_tip"], "--repo", manifest["room"],
                                     "--gate", receipt)
        self.assertEqual(rc, 1, text + err)
        self.assertIn("head-clean", text)
        dirty.unlink()
        os.environ.pop(contract.WRITER_ENV)
        self.assert_fold(manifest, receipt)  # readers precede writer enablement
        self.assertIsNone(foldcompose.write_manifest("fixture", manifest))
        self.assertEqual(self.events(), before)
        os.environ[contract.WRITER_ENV] = "1"
        self.git("merge", "--ff-only", manifest["result_tip"])
        closed, err = self.close(bounded, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(closed["close_proof_version"], 3)

    def assert_fold_close_refuses(self, row, manifest, receipt, ordinary_id):
        history = self.events()
        proof = self.assert_fold(manifest, receipt, succeeds=False)
        self.assertTrue(any(name == "reviewed:" + ordinary_id and ok is not True
                            for name, ok, _why in proof), proof)
        self.assertTrue(all(ok is True for name, ok, _why in proof
                            if not name.startswith("reviewed:")), proof)
        for preview in (True, False):
            out, why = self.close(row, manifest, receipt, dry_run=preview, fan_out=False)
            self.assertIsNone(out)
            self.assertIn("reviewed:" + ordinary_id, why)
            self.assertEqual(self.events(), history)
        # The locked writer must use the raw rows AND accepted verdict history
        # it already captured under lock, never another snapshot via the helper.
        with mock.patch.object(foldcompose, "_review_snapshot",
                               side_effect=AssertionError("second locked snapshot")):
            out, why = dispatches._record_close_proven(
                row["id"], "landed", row["reviewed_tip"], close_proof_version=3,
                closing_repo_id=row["repo_id"], closing_trunk_ref="refs/heads/main",
                closing_trunk_sha=manifest["result_tip"], proof_mode="patch-equivalent",
                delivery_class=landreq.DELIVERY_CLI, compose_manifest=manifest,
                compose_gate=receipt)
        self.assertIsNone(out)
        self.assertIn("reviewed:" + ordinary_id, why)
        self.assertEqual(self.events(), history)

    def test_mixed_ordinary_authority_refuses_equally_at_fold_preview_and_locked_close(self):
        bounded, ordinary, manifest, receipt = self.mixed_multicommit()
        bare = self.review(tip=ordinary["reviewed_tip"], body="Agreement, not approval.",
                           statement="Read the artifact.", polarity="concur")
        # A genuine APPROVE with a genuine receipt, but for a noncovering tip.
        with serial_process(ran=3):
            other_gate, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        noncover = self.review(tip=self.trunk, body="Review unrelated trunk.",
                              statement="reviewed gate:" + other_gate["id"], polarity="approve")
        self.git("merge", "--ff-only", manifest["result_tip"])
        path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        self.assert_fold(manifest, receipt)
        preview, err = self.close(bounded, manifest, receipt, dry_run=True, fan_out=False)
        self.assertIsNone(err, err)
        self.assertIsNotNone(preview)
        for label, rid in (("absent", "0" * 32), ("nonreview", self.root["id"]),
                           ("bare-concur", bare["id"]), ("noncovering-approve", noncover["id"])):
            bad = copy.deepcopy(manifest)
            for car in bad["cars"]:
                if car["row"] == ordinary["id"]:
                    car["row"] = rid
            path.write_text(json.dumps(bad))
            with self.subTest(authority=label):
                self.assert_fold_close_refuses(bounded, bad, receipt, rid)
            path.write_text(json.dumps(manifest))
            self.assert_fold(manifest, receipt)
        # Corrupt the ordinary review's ACTUAL canonical receipt, retaining its
        # id. Integrity filtering, not a mocked authority result, must refuse.
        receipts_path = Path(gate.receipts_path())
        original = receipts_path.read_bytes()
        entries = [json.loads(line) for line in original.splitlines() if line]
        target = ordinary["gate"].removeprefix("gate:")
        hits = [entry for entry in entries if entry.get("id") == target]
        self.assertEqual(len(hits), 1)
        hits[0]["tree"] = "0" * 40
        receipts_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
        self.assertIsNotNone(gate.by_id(target)[1])
        self.assertIsNone(gate.by_id(receipt[5:])[1])
        self.assert_fold_close_refuses(bounded, manifest, receipt, ordinary["id"])
        receipts_path.write_bytes(original)
        self.assert_fold(manifest, receipt)
        before_rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        with mock.patch.object(foldcompose, "_review_snapshot",
                               side_effect=AssertionError("second locked snapshot")):
            closed, err = dispatches._record_close_proven(
                bounded["id"], "landed", bounded["reviewed_tip"], close_proof_version=3,
                closing_repo_id=bounded["repo_id"], closing_trunk_ref="refs/heads/main",
                closing_trunk_sha=manifest["result_tip"], proof_mode="patch-equivalent",
                delivery_class=landreq.DELIVERY_CLI, compose_manifest=manifest, compose_gate=receipt)
        self.assertIsNone(err, err)
        self.assertEqual(closed["close_reason"], "landed")
        event = [e for e in self.events() if e.get("event") == "close" and e["id"] == bounded["id"]][-1]
        # Historical replay does not retrofit today's manifest/ordinary review
        # availability into the recorded event's existing proof contract.
        os.environ.pop(contract.WRITER_ENV)
        path.unlink()
        with mock.patch.object(foldcompose, "record_proof", side_effect=AssertionError("live authority during replay")):
            self.assertIsNone(dispatches._close_event_error(event, before_rows[bounded["id"]], before_rows))
            replayed = dispatches._apply(copy.deepcopy(before_rows[bounded["id"]]), event, before_rows)
        self.assertEqual(replayed["close_reason"], "landed")

    def test_project_root_worktree_and_subdirectory_share_canonical_manifests(self):
        linked = str(self.tmp / "linked")
        self.git("worktree", "add", "--detach", linked, "main")
        subdir = Path(self.repo) / "existing-subdirectory"
        subdir.mkdir()
        for polarity in ("approve", "concur"):
            for i, where in enumerate((self.repo, linked, str(subdir))):
                label = polarity + "-" + str(i)
                self.git("checkout", "-q", "-b", "lane/" + label, self.base)
                tip = self.commit("docs/" + label + ".txt", label + "\n")
                if polarity == "approve":
                    with serial_process(ran=3):
                        source_gate, err = gate.run(repo=self.repo)
                    self.assertIsNone(err, err)
                    row = self.review(tip=tip, body="Review ordinary alias control.",
                                      statement="reviewed gate:" + source_gate["id"], polarity="approve")
                else:
                    row = self.review(tip=tip)
                self.git("checkout", "-q", "main")
                flags = ("--bounded-concur",) if polarity == "concur" else ()
                rc, text, err = self.command("compose", row["id"], "--repo", where,
                                             "--trunk", "main", "--json", *flags)
                self.assertEqual(rc, 0, text + err)
                output = json.loads(text)
                record = foldcompose.read_manifest("fixture", output["composed_tip"])
                self.assertIsNotNone(record)
                self.assertEqual(record["cars"][0]["row"], row["id"])
                self.assertEqual(Path(output["room"]).parent, Path(self.repo + "-wt") / "compose")
                with serial_process(ran=3):
                    whole, err = gate.run(repo=output["room"])
                self.assertIsNone(err, err)
                token = "gate:" + whole["id"]
                for alias in (self.repo, linked, str(subdir)):
                    proof = foldcompose.composition_proof(alias, "fixture", record["result_tip"], gate_ref=token)
                    self.assertTrue(proof)
                    self.assertTrue(all(ok is True for _n, ok, _why in proof), proof)
                self.git("merge", "--ff-only", record["result_tip"])
                extra = {"compose_manifest": record, "compose_gate": token} if polarity == "concur" else {}
                closed, err = landreq.close(row["id"], "landed", repo=where, trunk="main",
                                            live=True, fan_out=False, **extra)
                self.assertIsNone(err, err)
                self.assertEqual(closed["close_reason"], "landed")

    def test_unknown_registry_refuses_and_unregistered_ordinary_stays_unscoped(self):
        bounded, ordinary, manifest, _receipt = self.mixed_multicommit()
        # Free the first composition room; no close or main merge occurred.
        self.git("worktree", "remove", manifest["room"])
        canonical_path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        canonical_path.unlink()
        regpath = Path(home.registry_path())
        original = regpath.read_bytes()
        regpath.write_text("{unreadable")
        history = self.events()
        for row, flags in ((ordinary, ()), (bounded, ("--bounded-concur",))):
            rc, text, err = self.command("compose", row["id"], "--repo", self.repo, "--json", *flags)
            self.assertEqual(rc, 1, text + err)
            self.assertIn("registry is UNKNOWN", text)
            self.assertEqual(self.events(), history)
        regpath.write_bytes(original)
        registry.save({"version": 1, "projects": {}})
        rc, text, err = self.command("compose", bounded["id"], "--bounded-concur", "--repo", self.repo, "--json")
        self.assertEqual(rc, 1, text + err)
        self.assertIn("registered project", text)
        rc, text, err = self.command("compose", ordinary["id"], "--repo", self.repo, "--json")
        self.assertEqual(rc, 0, text + err)
        output = json.loads(text)
        self.assertNotIn("compose_land_version", output)
        self.assertIsNone(foldcompose.read_manifest("fixture", output["composed_tip"]))
        self.assertEqual(foldcompose.project_state(self.repo), ("unregistered", None))

    def test_canonical_hostile_car_census_and_unmarked_concur_refuse(self):
        _bounded, _ordinary, manifest, receipt = self.mixed_multicommit()
        self.assert_fold(manifest, receipt)
        path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        mutations = (
            lambda m: m["cars"].pop(0),
            lambda m: m["cars"].reverse(),
            lambda m: m["cars"].insert(0, copy.deepcopy(m["cars"][0])),
            lambda m: m["cars"][0].update(source_commit=self.base),
            lambda m: m["cars"][0].update(result_commit=m["cars"][1]["result_commit"]),
            lambda m: m.update(bounded_rows=m["bounded_rows"] * 2),
            lambda m: m.update(bounded_rows=[]),
            lambda m: m.update(compose_land_version=True),
            lambda m: m.update(compose_land_version=2),
            lambda m: m.update(dry_run=True),
            lambda m: (m.pop("compose_land_version"), m.pop("bounded_rows")),
        )
        for mutate in mutations:
            bad = copy.deepcopy(manifest)
            mutate(bad)
            path.write_text(json.dumps(bad))
            with self.subTest(cars=bad["cars"], bounded=bad.get("bounded_rows")):
                self.assert_fold(manifest, receipt, succeeds=False)
        path.write_text(json.dumps(manifest))
        self.assert_fold(manifest, receipt)
        # Replay-valid PARTIAL group: dropping the first car AND moving the
        # composition base makes replay pass. Only complete bounded coverage
        # catches the omitted, reviewed first commit.
        partial = copy.deepcopy(manifest)
        partial["base"] = partial["cars"].pop(0)["result_commit"]
        self.assertTrue(all(ok is True for _n, ok, _d in foldcompose.replay_chain(
            self.repo, partial["cars"], partial["base"], partial["result_tip"])))
        path.write_text(json.dumps(partial))
        proof = self.assert_fold(manifest, receipt, succeeds=False)
        self.assertTrue(any("entire immutable" in why for _n, _ok, why in proof), proof)
        path.write_text(json.dumps(manifest))
        self.assert_fold(manifest, receipt)

    def test_absent_malformed_canonical_manifest_cannot_be_replaced_by_output(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        self.assert_fold(manifest, receipt)
        path = Path(foldcompose.manifest_path("fixture", manifest["result_tip"]))
        original = path.read_text()
        history = self.events()
        for text in (None, "{", "[]", "[" * 2000,
                     original[:-1] + ', "result_tip": "' + manifest["result_tip"] + '"}'):
            if text is None:
                path.unlink()
            else:
                path.write_text(text)
            self.assert_fold(manifest, receipt, succeeds=False)
            out, why = self.close(row, manifest, receipt, fan_out=False)
            self.assertIsNone(out)
            self.assertIn("canonical", why)
            self.assertEqual(self.events(), history)
            path.write_text(original)
        self.assert_fold(manifest, receipt)
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")

    def test_replay_valid_protected_revert_and_declared_effects_still_veto_fold(self):
        # Source=result is a legitimate replay identity, not a mock of replay.
        # A green whole-tree receipt and a clean NET diff cannot erase an
        # intermediate protected edit, nor override the independent declaration.
        for protected in (False, True):
            lane = "lane/hostile-" + str(protected)
            self.git("checkout", "-q", "-b", lane, self.base)
            self.commit("docs/safe.txt", "reversible looking net diff\n")
            if protected:
                self.commit("helm/foldcompose.py", "protected owner edit\n")
                self.git("rm", "-q", "helm/foldcompose.py")
                self.git("commit", "-qm", "revert owner edit")
            tip = self.git("rev-parse", "HEAD")
            body = brief(self.base, tip, effects="reversible" if protected else "hooks")
            row = self.review(tip=tip, body=body)
            sources = self.git("rev-list", "--reverse", self.base + ".." + tip).splitlines()
            record = {"result_tip": tip, "base": self.base, "compose_land_version": 1,
                      "dry_run": False, "bounded_rows": [row["id"]],
                      "cars": [{"row": row["id"], "source_commit": sha, "result_commit": sha}
                               for sha in sources]}
            self.assertEqual(self.git("diff", "--name-only", self.base, tip), "docs/safe.txt")
            replay = foldcompose.replay_chain(self.repo, record["cars"], self.base, tip)
            self.assertTrue(replay)
            self.assertTrue(all(ok is True for _n, ok, _d in replay), replay)
            self.assertIsNotNone(foldcompose.write_manifest("fixture", record))
            with serial_process(ran=3):
                receipt, err = gate.run(repo=self.repo)
            self.assertIsNone(err, err)
            proof = self.assert_fold(record, "gate:" + receipt["id"], succeeds=False)
            self.assertTrue(any("CONTRACT" in why for _n, _ok, why in proof), proof)
            self.git("checkout", "-q", "main")

    def snapshot_over_base(self):
        """A REAL fab dirty-tree snapshot whose range is exactly one commit.

        Built with `commit-tree` from a scratch index over `base`, never by
        writing fab's subject onto an ordinary commit: the object under test
        must be the one the producer actually mints, or the arm proves only
        that this door reads a string somebody typed.
        """
        index = str(self.tmp / "boundedsnapindex")
        env = dict(os.environ, GIT_INDEX_FILE=index)
        subprocess.run(["git", "-C", self.repo, "read-tree", self.tip],
                       check=True, env=env)
        tree = subprocess.run(["git", "-C", self.repo, "write-tree"],
                              check=True, capture_output=True, text=True,
                              env=env).stdout.strip()
        message = str(self.tmp / "boundedsnapmsg")
        Path(message).write_text(
            "fab snapshot (tracked+untracked) of %s+dirty\n" % self.base[:9],
            encoding="utf-8")
        tip = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-p", self.base,
             "-F", message], check=True, capture_output=True,
            text=True).stdout.strip()
        self.assertEqual(len(tip), 40)
        self.assertEqual(self.git("branch", "--contains", tip), "",
                         "the fixture snapshot is on a branch, so it is not "
                         "the dangling object this arm is about")
        self.assertEqual(
            self.git("rev-list", "--reverse", self.base + ".." + tip), tip,
            "the snapshot must be the whole bounded range, one commit")
        return tip

    def test_a_bounded_member_reviewed_at_a_dirty_tree_snapshot_is_refused(self):  # noqa: VACUOUS_ASSERTION — the ordinary bounded positive runs first through the same proof_of helper and asserts True on the same observable, so the False below cannot be an arm that never reached the branch
        """The bounded path SKIPS _reviewed_covers, so its door must be its own.

        `record_proof` `continue`s past every bounded member before reaching
        the per-car `_reviewed_covers` call, so the snapshot refusal that
        guards an ordinary car cannot guard a bounded one: without a refusal of
        its own, a bounded member is admitted with an unconditional True
        whatever its reviewed object is. A complete immutable range establishes
        that the CARS are covered; it says nothing about whether the reviewed
        tip is a commit of anyone's work.

        THE ROW IS MINTED THE WAY A PRE-DOOR ROW WAS. `dispatches` refuses a
        snapshot at append time now, and this door exists exactly for the rows
        that predate it — both real instances were already on the ledger when
        the class was found. The append guard is suspended for the WRITE only,
        never for the proof under test, and the brief, verdict, compose and
        gate are the real ones.
        """
        # NEITHER HALF MERGES TO TRUNK, and that is load-bearing rather than
        # thrift. `landed()` fast-forwards main onto the composed tip, which
        # puts the reviewed CONTENT on trunk — so the second row derives
        # LANDED and `compose` excludes it with "bounded CONCUR requires a
        # live REVIEWED row", and the arm never reaches the branch it is
        # about. record_proof asks nothing of trunk, so compose plus the
        # composed-tree gate is the whole input either half needs.
        def proof_of(row):
            manifest = self.compose(row)
            with serial_process(ran=3):
                receipt, err = gate.run(repo=manifest["room"])
            self.assertIsNone(err, err)
            checks, members = foldcompose.record_proof(
                self.repo, manifest, gate_ref="gate:" + receipt["id"])
            self.assertEqual(
                list(members or {}), [row["id"]],
                "%s is not a bounded member, so this arm never reached the "
                "branch it is about" % row["id"])
            named = [c for c in checks
                     if c[0] == "bounded-review:" + row["id"]]
            self.assertEqual(len(named), 1, checks)
            return checks, named[0]

        # ORDINARY BOUNDED POSITIVE FIRST, through this same entrypoint, so a
        # False below is about the reviewed tip and not about bounded plumbing.
        checks, named = proof_of(self.review())
        self.assertIs(named[1], True, named)
        self.assertTrue(all(ok is True for _n, ok, _w in checks), checks)

        snapshot = self.snapshot_over_base()
        with mock.patch.object(dispatches, "snapshot_tip_refusal",
                               lambda *_a, **_kw: None):
            snap_row = self.review(tip=snapshot, body=brief(self.base, snapshot))
        self.assertEqual(snap_row["reviewed_tip"], snapshot)
        _checks, named = proof_of(snap_row)
        self.assertIs(named[1], False, named)
        self.assertIn("COMMIT BEFORE GATING", named[2])
        self.assertIn(snapshot[:12], named[2])

    def test_scoped_fold_cannot_spend_suite_for_another_tree(self):
        row = self.review()
        manifest = self.compose(row)
        with serial_process(ran=3):
            other, err = gate.run(repo=self.repo)
            whole, whole_err = gate.run(repo=manifest["room"])
        self.assertIsNone(err, err)
        self.assertIsNone(whole_err, whole_err)
        self.assertNotEqual(other["tree"], manifest["composed_tree"])
        self.assert_fold(manifest, "gate:" + other["id"], succeeds=False)
        self.assert_fold(manifest, "gate:" + whole["id"])

    def test_activation_unknown_and_inactive_never_write_bounded_manifest(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        self.assert_fold(manifest, receipt)
        activation = Path(foldcompose.activation_path("fixture"))
        original = activation.read_text()
        activation.unlink()  # surviving latch means UNKNOWN, not inactive
        proof = self.assert_fold(manifest, receipt, succeeds=False)
        self.assertEqual(proof[0][:2], ("activation", None))
        out, why = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(out)
        self.assertIn("activation", why)
        activation.write_text(original)
        self.assert_fold(manifest, receipt)
        # New prospective lane, to reach activation admission before a pick.
        self.git("checkout", "-q", "-b", "lane/new", self.base)
        tip = self.commit("docs/new.txt", "new bounded content\n")
        self.git("checkout", "-q", "main")
        new = self.review(tip=tip)
        for unknown in (True, False):
            activation.unlink(missing_ok=True)
            if not unknown:
                Path(foldcompose.activation_latch_path("fixture")).unlink()
            rc, text, err = self.command("compose", new["id"], "--bounded-concur", "--json")
            self.assertEqual(rc, 1, text + err)
            self.assertIn("active composition proof", text)
        # Ordinary activation semantics remain unchanged: no stage when never active.
        self.assertIsNone(foldcompose.composition_proof(self.repo, "fixture", manifest["result_tip"]))

    def test_real_compose_locked_close_replay_and_retry(self):
        row = self.review()
        projected, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(projected["state"], "REVIEWED")
        rc, text, _err = self.command("compose", row["id"], "--json")
        self.assertEqual(rc, 1)
        self.assertIn("REVIEWED", text)
        manifest, receipt = self.landed(row)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        before = copy.deepcopy(rows[row["id"]])
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        self.assertEqual(out["polarity"], "concur")
        event = [e for e in self.events() if e.get("event") == "close" and e["id"] == row["id"]][-1]
        self.assertEqual(event["close_proof_version"], 3)
        self.assertIsNone(event["close_delivery_restart"])
        self.assertEqual(event["compose_land_proof"]["gate"], receipt[5:])
        os.environ.pop(contract.WRITER_ENV)
        self.assertIsNone(dispatches._close_event_error(event, before, rows))
        replayed = dispatches._apply(copy.deepcopy(before), event, rows)
        self.assertEqual(replayed["close_reason"], "landed")
        os.environ[contract.WRITER_ENV] = "1"
        history = self.events()
        retry, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(retry["close_reason"], "landed")
        self.assertEqual(self.events(), history)
        direct, err = dispatches._record_close_proven(
            row["id"], "landed", row["reviewed_tip"], close_proof_version=3,
            closing_repo_id=event["closing_repo_id"], closing_trunk_ref=event["closing_trunk_ref"],
            closing_trunk_sha=event["closing_trunk_sha"], proof_mode=event["close_proof_mode"],
            translated_tip=event.get("translated_tip"), delivery_class=event["close_delivery_class"],
            compose_manifest=manifest, compose_gate=receipt)
        self.assertIsNone(err, err)
        self.assertEqual(direct["close_reason"], "landed")
        self.assertEqual(self.events(), history)

    def test_composition_capture_and_replay_carry_the_rows_own_checkout(self):
        """F1's composition leg. The coordinate has to travel the WHOLE way —
        dispatch, verdict, bind, and then both composition doors — or the last
        reader is back to resolving a checkout question through a shared admin
        dir. `capture` writes the landing proof and `proof_error` re-validates it
        on replay; both asked the gate door about the repository alone.

        THE COORDINATE IS READ OFF THE STANDING ROW, NOT ADDED TO THE PROOF: the
        proof is compared byte for byte against every recorded landing, so a new
        key there would refuse every proof written before it.

        THE SPY WRAPS THE REAL DOOR; every binding still has to succeed for the
        close to land, so nothing here is measured against a stubbed answer.
        """
        row = self.review()
        manifest, receipt = self.landed(row)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        standing = rows[row["id"]]
        checkout = standing.get("repo_root")
        # CONTROL ON THE INPUT: the row carries a checkout AND a repository, and
        # they are different strings — otherwise "it passed the checkout" and "it
        # passed the repository" would be one observation.
        self.assertTrue(checkout)
        self.assertNotEqual(checkout, standing.get("repo_id"))
        before = copy.deepcopy(standing)
        seen = []
        real = gate.bind

        def spy(*args, **kw):
            seen.append(kw.get("consuming_repo"))
            return real(*args, **kw)

        with mock.patch.object(gate, "bind", spy):
            out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        # MUST-HIT FIRST: the capture really reached the gate door, so the
        # agreement below is about calls that happened.
        self.assertTrue(seen, "the locked close never bound a gate")
        self.assertEqual(set(seen), {checkout})
        # AND REPLAY ASKS THE SAME QUESTION. `_close_event_error` is the reader
        # that re-validates a recorded landing, and it runs OUTSIDE the writer.
        event = [e for e in self.events()
                 if e.get("event") == "close" and e["id"] == row["id"]][-1]
        writer = os.environ.pop(contract.WRITER_ENV)
        del seen[:]
        try:
            with mock.patch.object(gate, "bind", spy):
                self.assertIsNone(dispatches._close_event_error(event, before,
                                                                rows))
        finally:
            os.environ[contract.WRITER_ENV] = writer
        self.assertTrue(seen, "replay never bound the recorded gate")
        self.assertEqual(set(seen), {checkout})

    def test_moving_trunk_retry_keeps_original_scoped_pin(self):
        row = self.review()
        peer = self.peer("Independent peer awaiting a retried sweep.")
        hostile = self.peer("No completed pass.", statement="read the patch")
        manifest, receipt = self.landed(row)
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        pin = out["closing_trunk_sha"]
        moved = self.commit("later.txt", "later unrelated tree\n")
        self.assertNotEqual(moved, pin)
        self.assert_fold(manifest, receipt)
        history = self.events()
        again, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(again["closing_trunk_sha"], pin)
        direct, err = dispatches._record_close_proven(
            row["id"], "landed", self.tip, close_proof_version=3,
            closing_repo_id=out["closing_repo_id"], closing_trunk_ref=out["closing_trunk_ref"],
            closing_trunk_sha=moved, proof_mode=out["close_proof_mode"],
            translated_tip=out.get("translated_tip"), delivery_class=out["close_delivery_class"],
            compose_manifest=manifest, compose_gate=receipt)
        self.assertIsNone(err, err)
        self.assertEqual(direct["closing_trunk_sha"], pin)
        bad, why = self.close(row, dict(manifest, composed_tree="0" * 40), receipt, fan_out=False)
        self.assertIsNone(bad)
        self.assertIn("tree", why)
        self.assertEqual(self.events(), history)
        for source_id, candidate_pin in ((peer["id"], pin), (row["id"], moved)):
            refused, why = dispatches._record_close_proven(
                peer["id"], "landed", self.tip, close_proof_version=3,
                closing_repo_id=out["closing_repo_id"], closing_trunk_ref=out["closing_trunk_ref"],
                closing_trunk_sha=candidate_pin, proof_mode=out["close_proof_mode"],
                translated_tip=out.get("translated_tip"), delivery_class=out["close_delivery_class"],
                compose_manifest=manifest, compose_gate=receipt, compose_landed_by=source_id)
            self.assertIsNone(refused)
            self.assertIn("recorded landing", why)
            self.assertEqual(self.events(), history)
        preview, err = self.close(row, manifest, receipt, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], preview["would_close_siblings"])
        self.assertIn(hostile["id"], preview["would_leave_open_siblings"])
        self.assertEqual(self.events(), history)
        swept, err = self.close(row, manifest, receipt)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], {p["id"] for p in swept["closed_siblings"]})
        self.assertIn(hostile["id"], {p["id"] for p in swept["sibling_refusals"]})
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(rows[peer["id"]]["closing_trunk_sha"], pin)
        self.assertEqual(rows[peer["id"]]["compose_land_proof"]["pinned"], pin)
        self.assertNotEqual(rows[peer["id"]]["compose_land_proof"]["contract"]["brief"],
                            rows[row["id"]]["compose_land_proof"]["contract"]["brief"])
        self.assertFalse(rows[hostile["id"]].get("close_reason"))
        self.assertEqual(sum(e.get("event") == "close" and e["id"] == row["id"]
                             for e in self.events()), 1)

    def test_compose_dry_run_is_not_a_standing_close_artifact(self):
        row = self.review()
        history = self.events()
        preview = self.compose(row, "--dry-run")
        self.assertIs(preview["dry_run"], True)
        self.assertEqual(self.events(), history)
        manifest, receipt = self.landed(row)
        out, why = self.close(row, preview, receipt, fan_out=False)
        self.assertIsNone(out)
        self.assertIn("standing", why)
        good, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(good["close_reason"], "landed")

    def test_paired_cli_flags_and_actual_manifest_path(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        history = self.events()
        for args in (("--reason", "landed", "--compose-gate", receipt),
                     ("--reason", "landed", "--compose-manifest", "missing.json"),
                     ("--reason", "withdrawn", "--compose-manifest", "missing.json", "--compose-gate", receipt)):
            rc, _text, err = self.command("close", row["id"], *args)
            self.assertEqual(rc, 2)
            self.assertIn("together", err)
            self.assertEqual(self.events(), history)
        rc, text, err = self.command(
            "close", row["id"], "--reason", "landed", "--trunk", "main", "--live",
            "--compose-manifest", foldcompose.manifest_path("fixture", manifest["result_tip"]),
            "--compose-gate", receipt, "--json")
        self.assertEqual(rc, 0, err + text)
        self.assertEqual(json.loads(text)["close_reason"], "landed")

    def test_mixed_ordinary_approve_keeps_its_existing_close_proof(self):
        bounded = self.review()
        self.git("checkout", "-q", "-b", "lane/ordinary", self.base)
        tip = self.commit("docs/ordinary.txt", "ordinary approved work\n")
        with serial_process(ran=3):
            receipt, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "main")
        ordinary = self.review(tip=tip, body="Review ordinary work.",
                               statement="reviewed gate:" + receipt["id"], polarity="approve")
        rc, text, err = self.command("compose", bounded["id"], ordinary["id"],
                                      "--bounded-concur", "--json")
        self.assertEqual(rc, 0, err + text)
        manifest = json.loads(text)
        self.assertEqual(len(manifest["members"]), 2)
        self.assertIn("compose_land", manifest["members"][0])
        self.assertNotIn("compose_land", manifest["members"][1])
        with serial_process(ran=3):
            whole, err = gate.run(repo=manifest["room"])
        self.assertIsNone(err, err)
        self.git("merge", "--ff-only", manifest["composed_tip"])
        scoped, err = self.close(bounded, manifest, "gate:" + whole["id"], fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(scoped["close_proof_version"], 3)
        plain, err = landreq.close(ordinary["id"], "landed", trunk="main", live=True, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(plain["close_reason"], "landed")
        event = [e for e in self.events() if e.get("event") == "close" and e["id"] == ordinary["id"]][-1]
        self.assertEqual(event["close_proof_version"], 1)
        self.assertNotIn("compose_land_proof", event)
        self.assertNotIn("compose_land_proof", plain)

    def test_hostile_reanchored_actual_close_events_stay_inert(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        before = copy.deepcopy(rows[row["id"]])
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        event = [e for e in self.events() if e.get("event") == "close" and e["id"] == row["id"]][-1]
        def broken_chain(p):
            p["source"][0]["parent"] = "0" * 40
        def changed_newline(p):
            p["carried"][0]["identity"][2] = "0" * 64
        mutations = (
            lambda p: p.update(diff=[{"status": "M", "paths": ["helm/work/_guard.py"]}]),
            lambda p: p.update(diff=None), lambda p: p.update(gate="0" * 16),
            lambda p: p.update(composed_tree="0" * 40),
            lambda p: p.update(contract=dict(p["contract"], brief="0" * 32)),
            lambda p: p.update(member=dict(p["member"], source_base="0" * 40)),
            lambda p: p.update(extra=True), broken_chain, changed_newline,
        )
        for mutate in mutations:
            bad = copy.deepcopy(event)
            mutate(bad["compose_land_proof"])
            bad["compose_land_anchor"] = dispatches._proof_anchor("compose-land-v1", bad["compose_land_proof"])
            with self.subTest(proof=bad["compose_land_proof"]):
                self.assertTrue(dispatches._close_event_error(bad, before, rows))
                self.assertEqual(dispatches._apply(copy.deepcopy(before), bad, rows), before)
        for version in (1, 2, 4, True):
            bad = dict(event, close_proof_version=version)
            self.assertTrue(dispatches._close_event_error(bad, before, rows))
            self.assertEqual(dispatches._apply(copy.deepcopy(before), bad, rows), before)

    def test_same_tip_independent_admission_dry_run_and_retry_sweep(self):
        row = self.review()
        peer = self.peer("A second independently bounded pass.")
        old = self.peer("Historical ordinary concurrence.", statement="read the patch")
        findings = self.peer("Findings remain.", statement="bounded findings=1")
        inferred = self.peer("Inferred rather than measured.", basis="inferred")
        manifest, receipt = self.landed(row)
        history = self.events()
        dry, err = self.close(row, manifest, receipt, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], dry["would_close_siblings"])
        for bad in (old, findings, inferred):
            self.assertIn(bad["id"], dry["would_leave_open_siblings"])
        self.assertEqual(self.events(), history)
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        history = self.events()
        dry, err = self.close(row, manifest, receipt, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], dry["would_close_siblings"])
        self.assertEqual(self.events(), history)
        out, err = self.close(row, manifest, receipt)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], {r["id"] for r in out["closed_siblings"]})
        self.assertTrue({old["id"], findings["id"], inferred["id"]} <=
                        {r["id"] for r in out["sibling_refusals"]})
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(rows[peer["id"]]["close_reason"], "landed")
        self.assertFalse(rows[self.root["id"]].get("close_reason"))
        history = self.events()
        again, err = self.close(row, manifest, receipt)
        self.assertIsNone(err, err)
        self.assertEqual(again["close_reason"], "landed")
        self.assertEqual(self.events(), history)

    def test_same_tip_build_preview_requires_real_approve_discharge(self):
        row = self.review()
        peer = self.pending_peer()
        build = self.root  # the review names this BUILD through supersedes
        unbound = dispatches.add("build-peer-fixture", "fixture", ref=self.tip,
                                 repo=self.repo, kind="build", new_work=True,
                                 force=True, notify=False)
        self.assertIsNotNone(unbound)
        self.assertIsNone(dispatches.bound_tip(unbound))
        self.assertEqual(dispatches.bound_tip(peer), self.tip)
        manifest, receipt = self.landed(row)
        history = self.events()
        preview, err = self.close(row, manifest, receipt, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], preview["would_leave_open_siblings"])
        self.assertNotIn(peer["id"], preview["would_close_siblings"])
        # A BUILD ref names a base, even when its bytes equal this reviewed
        # tip. Neither a naked BUILD nor a chain parent is a same-tip peer.
        for candidate in (build, unbound):
            self.assertNotIn(candidate["id"], preview["would_close_siblings"])
            self.assertNotIn(candidate["id"], preview["would_leave_open_siblings"])
        self.assertEqual(self.events(), history)
        out, err = self.close(row, manifest, receipt)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], {p["id"] for p in out["sibling_refusals"]})
        self.assert_discharge_refuses(build, "CONCUR is not BUILD discharge authority", row)
        self.assert_discharge_refuses(unbound, "a matching BUILD base is not a review", row)
        # APPROVE is necessary but not enough: retain the original reviewed
        # commit as an actual trunk ancestor before expecting BUILD discharge.
        self.git("merge", "--no-ff", "-m", "retain reviewed ancestry", self.tip)
        self.assertIn(self.tip, self.git("rev-list", "main").splitlines())
        self.git("checkout", "-q", "lane/fixture")
        with serial_process(ran=3):
            approved_gate, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "main")
        approved = self.peer("Ordinary approved pass.", polarity="approve",
                             statement="reviewed gate:" + approved_gate["id"])
        history = self.events()
        preview, err = landreq.close(approved["id"], "landed", trunk="main",
                                     live=True, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], preview["would_close_siblings"])
        self.assertEqual(self.events(), history)
        out, err = landreq.close(approved["id"], "landed", trunk="main", live=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], {p["id"] for p in out["closed_siblings"]})
        self.assert_discharge_preview_and_live(build, approved)
        self.assert_discharge_refuses(unbound, "no recorded review relation", approved)

    def test_patch_equivalent_approve_leaves_build_open_in_preview_and_live(self):
        self.git("checkout", "-q", "lane/fixture")
        with serial_process(ran=3):
            receipt, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "main")
        approved = self.review(polarity="approve", body="Ordinary review.",
                               statement="reviewed gate:" + receipt["id"])
        build = self.root
        peer = self.pending_peer()
        self.assertEqual(approved["supersedes"], build["id"])
        self.assertIsNone(dispatches.bound_tip(build))
        rc, text, err = self.command("compose", approved["id"], "--json")
        self.assertEqual(rc, 0, err + text)
        manifest = json.loads(text)
        self.git("merge", "--ff-only", manifest["composed_tip"])
        history = self.events()
        self.assertFalse(any(e.get("event") == "close" for e in history))
        self.assertNotIn(self.tip, self.git("rev-list", "main").splitlines())
        preview, err = landreq.close(approved["id"], "landed", trunk="main",
                                     live=True, dry_run=True)
        self.assertIsNone(err, err)
        self.assertEqual(preview["proof_mode"], "patch-equivalent")
        self.assertIn(peer["id"], preview["would_leave_open_siblings"])
        self.assertNotIn(peer["id"], preview["would_close_siblings"])
        self.assertNotIn(build["id"], preview["would_leave_open_siblings"])
        self.assertNotIn(build["id"], preview["would_close_siblings"])
        self.assertEqual(self.events(), history)
        out, err = landreq.close(approved["id"], "landed", trunk="main", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        self.assertEqual(out["close_proof_mode"], "patch-equivalent")
        self.assertIn(peer["id"], {p["id"] for p in out["sibling_refusals"]})
        # A real chain successor now has a real LANDED close, but not literal
        # reviewed ancestry. Both preview and locked BUILD discharge refuse.
        self.assert_discharge_refuses(
            build, "patch-equivalent approval is not ancestry", approved)
        history = self.events()
        retry, err = landreq.close(approved["id"], "landed", trunk="main",
                                   live=True, dry_run=True)
        self.assertIsNone(err, err)
        self.assertIn(peer["id"], retry["would_leave_open_siblings"])
        self.assertEqual(self.events(), history)

    def test_ancestor_approve_allows_build_discharge_in_preview_and_live(self):
        self.git("checkout", "-q", "lane/fixture")
        with serial_process(ran=3):
            receipt, err = gate.run(repo=self.repo)
        self.assertIsNone(err, err)
        self.git("checkout", "-q", "main")
        approved = self.review(polarity="approve", body="Ordinary ancestor review.",
                               statement="reviewed gate:" + receipt["id"])
        build = self.root
        peer = self.pending_peer()
        self.assertEqual(approved["supersedes"], build["id"])
        self.assertIsNone(dispatches.bound_tip(build))
        self.git("merge", "--no-ff", "-m", "retain reviewed ancestry", self.tip)
        self.assertIn(self.tip, self.git("rev-list", "main").splitlines())
        history = self.events()
        self.assertFalse(any(e.get("event") == "close" for e in history))
        preview, err = landreq.close(approved["id"], "landed", trunk="main",
                                     live=True, dry_run=True)
        self.assertIsNone(err, err)
        self.assertEqual(preview["proof_mode"], "ancestor")
        self.assertIn(peer["id"], preview["would_close_siblings"])
        self.assertNotIn(build["id"], preview["would_close_siblings"])
        self.assertNotIn(build["id"], preview["would_leave_open_siblings"])
        self.assertEqual(self.events(), history)
        out, err = landreq.close(approved["id"], "landed", trunk="main", live=True)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_proof_mode"], "ancestor")
        self.assertIn(peer["id"], {p["id"] for p in out["closed_siblings"]})
        # The BUILD's recorded chain, not its base ref, owns this door. Its
        # real discharge preview and locked write agree after the review lands.
        self.assert_discharge_preview_and_live(build, approved)

    def test_protected_edit_then_revert_is_not_a_clean_range(self):
        self.git("checkout", "-q", "lane/fixture")
        self.commit("helm/work/_guard.py", "changed protected hook\n")
        self.git("rm", "-q", "--", "helm/work/_guard.py")
        self.git("commit", "-qm", "revert protected hook")
        tip = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        self.assertEqual(self.git("diff", "--name-only", self.base, tip), "docs/example.txt")
        observed = contract.measure_diff(self.repo, self.base, tip)
        self.assertEqual(contract.effects(["reversible"], observed)[0], "CONTRACT")
        self.assertIn({"status": "D", "paths": ["helm/work/_guard.py"]}, observed)
        row = self.review(tip=tip)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        proof, why = contract.admission(rows[row["id"]], rows, self.repo)
        self.assertIsNone(proof)
        self.assertIn("helm/work/_guard.py", why)
        history = self.events()
        rc, text, _err = self.command("compose", row["id"], "--bounded-concur", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("helm/work/_guard.py", text)
        self.assertEqual(self.events(), history)

    def test_diff_failure_is_unknown_and_the_locked_writer_remeasures(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        history = self.events()
        with mock.patch.object(contract, "measure_diff", return_value=None):
            out, why = dispatches._record_close_proven(
                row["id"], "landed", self.tip, close_proof_version=3,
                closing_repo_id=os.path.realpath(self.repo + "/.git"),
                closing_trunk_ref="refs/heads/main", closing_trunk_sha=manifest["composed_tip"],
                proof_mode="patch-equivalent", delivery_class=landreq.DELIVERY_CLI,
                compose_manifest=dict(manifest, diff=[]), compose_gate=receipt)
        self.assertIsNone(out)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(self.events(), history)
        self.assertEqual(contract.effects(["reversible"], contract.measure_diff(self.repo, self.base, self.tip))[0], "REVERSIBLE")
        backend = contract.vcs.backend(self.repo)
        for result in ((1, b"M\0docs/example.txt\0", b"failure"),
                       (0, b"Q\0docs/example.txt\0", b"")):
            with mock.patch.object(backend, "run", return_value=result), \
                    mock.patch.object(contract.vcs, "backend", return_value=backend), \
                    mock.patch.object(contract, "_text", return_value=self.tip + " " + self.base):
                self.assertIsNone(contract.measure_diff(self.repo, self.base, self.tip))

    def test_untiered_historical_concurrence_is_not_reclassified(self):
        body = brief(self.base, self.tip)
        row, err, _sent = dispatches.send(
            "review-fixture", "fixture", body, self.tip, repo=self.repo,
            kind="review", supersedes=self.root["id"], force=True, sign=False)
        self.assertIsNone(err, err)
        old, err = dispatches.mark_verdict(row["id"], self.tip, evidence(body),
                                           polarity="concur", basis="measured", bind_author=False)
        self.assertIsNone(err, err)
        self.assertEqual(old["status"], "verdict")
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertIsNone(contract.parse(rows[row["id"]], rows)[1])
        admitted, why = contract.admission(rows[row["id"]], rows, self.repo)
        self.assertIsNone(admitted)
        self.assertIn("PRE-TIER", why)
        history = self.events()
        rc, text, _err = self.command("compose", row["id"], "--bounded-concur", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("PRE-TIER", text)
        self.assertEqual(self.events(), history)

    def test_scope_read_error_and_merge_or_empty_ranges_refuse(self):
        measured = contract.measure_diff(self.repo, self.base, self.tip)
        self.assertEqual(contract.effects(["reversible"], measured)[0], "REVERSIBLE")
        for malformed in (None, 1111111111111111111111111111111111111111, True):
            self.assertIsNone(contract.measure_diff(self.repo, malformed, self.tip))
        with mock.patch.object(contract.vcs, "backend", side_effect=OSError("unreadable git")):
            self.assertIsNone(contract.measure_diff(self.repo, self.base, self.tip))
        for parents in (self.tip + " " + self.base + " " + self.trunk,
                        self.tip + " " + "0" * 40):
            with mock.patch.object(contract, "_text", return_value=parents):
                self.assertIsNone(contract.measure_diff(self.repo, self.base, self.tip))
        self.git("checkout", "-q", "lane/fixture")
        self.git("commit", "--allow-empty", "-qm", "empty range member")
        tip = self.git("rev-parse", "HEAD")
        self.assertIsNone(contract.measure_diff(self.repo, self.base, tip))

    def test_manifest_gate_and_tree_refusals_leave_history_unchanged(self):
        row = self.review()
        manifest, receipt = self.landed(row)
        history = self.events()
        for bad, token in ((dict(manifest, composed_tree="0" * 40), receipt),
                           (dict(manifest, dry_run=True), receipt),
                           (dict(manifest, members=[]), receipt),
                           (manifest, "gate:" + "0" * 16)):
            out, why = self.close(row, bad, token, fan_out=False)
            self.assertIsNone(out)
            self.assertTrue(why)
            self.assertEqual(self.events(), history)
        good, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(good["close_reason"], "landed")

    def test_actual_focused_receipt_cannot_close_the_composed_tree(self):
        # Focus needs a Python-only bounded diff and an actual import consumer.
        # All files precede the new BUILD base except the reviewed value edit.
        self.commit("pkg/__init__.py", "")
        self.commit("pkg/value.py", "VALUE = 1\n")
        self.commit("tests/__init__.py", "")
        # An independent test is part of the base, not the reviewed diff.
        # Without it the closure is 1/1: correctly a whole-suite queue request,
        # never a focused receipt. This must remain a proper 1/2 subset.
        self.commit("tests/test_independent.py",
            "import unittest\n"
            "class IndependentTest(unittest.TestCase):\n"
            "    def test_independent(self):\n"
            "        self.assertEqual(2 + 2, 4)\n")
        self.base = self.commit("tests/test_value.py",
            "import unittest\nfrom pkg import value\n"
            "class ValueTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(value.VALUE, 2)\n")
        self.root = dispatches.add("python-builder-fixture", "fixture", ref=self.base,
                                   repo=self.repo, kind="build", new_work=True,
                                   force=True, notify=False)
        self.assertIsNotNone(self.root)
        self.git("checkout", "-q", "-b", "lane/focused-fixture")
        self.tip = self.commit("pkg/value.py", "VALUE = 2\n")
        self.git("checkout", "-q", "main")
        row = self.review()
        manifest = self.compose(row)
        footer = ("test_value (tests.test_value.ValueTest.test_value) ... ok\n"
                  "\nRan 1 test in 0.0s\n\nOK\n")
        # Only child execution is simulated; scope planning, v6 receipt
        # production, canonical artifact lookup and BOTH bind needs are real.
        with mock.patch.object(gate, "_queued_process", return_value=("", footer, 0, None)):
            focused, err = gate.run(repo=manifest["room"], focus=True)
        self.assertIsNone(err, err)
        self.assertEqual(focused["status"], "OK")
        self.assertFalse(focused["suite"])
        self.assertEqual(focused["focus"]["selected"], ["tests.test_value"])
        self.assertEqual(focused["focus"]["universe"], 2)
        self.assertEqual(focused["tree"], manifest["composed_tree"])
        token = "gate:" + focused["id"]
        state, ident, why = gate.bind(token, manifest["composed_tip"],
                                      repo_id=row["repo_id"], need=gate.NEED_FOCUSED)
        self.assertEqual(state, "VERIFIED", why)
        self.assertEqual(ident, focused["id"])
        state, _ident, why = gate.bind(token, manifest["composed_tip"],
                                       repo_id=row["repo_id"], need=gate.NEED_SUITE)
        self.assertNotEqual(state, "VERIFIED")
        self.assertTrue(why)
        proof = self.assert_fold(manifest, token, succeeds=False)
        self.assertEqual([(n, ok) for n, ok, _d in proof if ok is not True],
                         [("bounded-suite", False)])
        with serial_process(ran=2):
            whole, err = gate.run(repo=manifest["room"])
        self.assertIsNone(err, err)
        self.assertEqual(whole["tree"], focused["tree"])
        self.assert_fold(manifest, "gate:" + whole["id"])
        self.git("merge", "--ff-only", manifest["composed_tip"])
        history = self.events()
        out, why = self.close(row, manifest, token, fan_out=False)
        self.assertIsNone(out)
        self.assertIn("whole composed-tree", why)
        self.assertEqual(self.events(), history)
        out, err = self.close(row, manifest, "gate:" + whole["id"], fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")

    def test_a_scoped_close_re_reads_the_rows_checkout_from_the_filesystem(self):  # noqa: VACUOUS_ASSERTION — the refused close (out is None, no event appended, W absent on disk) is bracketed by unconditional positives on the SAME observables: the memo is asserted to still hold W's key and to still answer the repository before the refusal, the refusal text is asserted to name UNKNOWN and W, and the CONTROL at the end drives the identical close with W back and asserts close_reason == "landed"
        """Round nine (B), at the scoped-close caller shape. The row's own
        checkout travels into the close (the arm above proves that) and the
        close validated it through a per-process memo: a linked worktree W
        REMOVED after the memo warmed still answered its repository from the
        entry, so the close admitted a receipt for a tree that no longer
        existed. The checkout is validated against the FILESYSTEM at the moment
        of the act; the memo is re-read, never trusted.

        ONE ROW, ONE CLOSE CALL, ONE CHANGE ON DISK: the same close refuses
        with W gone and lands with W back — and the memo is kept WARM across
        both, which is what makes the refusal the filesystem's and not the
        memo's. The row is DISPATCHED FROM W, the way the world mints such a
        row, so `repo_root` is W by the shipped producer and not by an edit.
        """
        from helm import vcs
        w = os.path.realpath(str(self.tmp / "lane-w"))
        self.git("worktree", "add", "-q", "--detach", w, self.base)
        body = brief(self.base, self.tip)
        row, err, _sent = dispatches.send(
            "review-fixture", "fixture", body, self.tip, repo=w,
            kind="review", supersedes=self.root["id"], force=True, sign=False)
        self.assertIsNone(err, err)
        # CONTROL ON THE INPUT: the shipped producer recorded W as the row's
        # checkout, beside the shared repository.
        self.assertEqual(row.get("repo_root"), w)
        self.assertEqual(row.get("repo_id"), dispatches._repo_info(self.repo)["repo_id"])
        with native_author(self), mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "review-fixture"}):
            verdict, err = dispatches.mark_verdict(
                row["id"], self.tip, evidence(body), polarity="concur",
                basis="measured", bind_author=True)
        self.assertIsNone(err, err)
        manifest, receipt = self.landed(verdict)
        # WARM THE MEMO through the seam the door reads, then remove W.
        common = gate._repository_of(self.repo)
        self.assertEqual(gate._repository_of(w), common)
        key = (type(vcs.backend(w)), os.path.realpath(w))
        self.assertIn(key, vcs._COMMON_DIR)
        self.git("worktree", "remove", "--force", w)
        # CONTROL ON THE PREMISE: the memo is STILL warm and still answers the
        # repository for the removed tree — a close trusting it would land.
        self.assertFalse(os.path.exists(w))
        self.assertIn(key, vcs._COMMON_DIR)
        self.assertEqual(gate._repository_of(w), common)
        history = self.events()
        out, why = self.close(verdict, manifest, receipt, fan_out=False)
        self.assertIsNone(out)
        self.assertIn("UNKNOWN", why)
        self.assertIn(w, why)
        self.assertEqual(self.events(), history, "a refusal appended a close")
        # THE LIVE-W CONTROL, SAME ROW, SAME CALL: W back on disk, memo untouched.
        self.git("worktree", "prune")
        self.git("worktree", "add", "-q", "--detach", w, self.base)
        out, err = self.close(verdict, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")

    def test_record_time_authority_survives_real_rename_and_retirement(self):
        store.write_prior({"id": "fixture-approval-tier", "statement": "Fixture final approval tier.",
                           "confidence": 1.0, "stated_ts": "2026-07-29T00:00:00Z", "source": "human",
                           "policy_kind": "approval-tier", "policy_members": ["seat:review-fixture"],
                           "policy_reason": "independent review"},
                          root_dir=os.path.join(home.global_dir(), "premises"))
        row = self.review()
        self.assertEqual(dispatches.approval_tier_for_verdict(row), ("ok", None))
        manifest, receipt = self.landed(row)
        rows, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        proof, err = contract.capture(rows[row["id"]], rows, row["repo_id"],
                                      manifest["composed_tip"], manifest, receipt)
        self.assertIsNone(err, err)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "review-fixture"}):
            seats.write_roster("review-fixture", session="verdict-author-session",
                               runtime={"family": "claude", "backend": "native"}, presence_beat=False)
            renamed, why = seats.rename_seat("review-fixture", "renamed-fixture", whole_row=True)
        self.assertTrue(renamed, why)
        self.assertIn("renamed-fixture", seats.roster())
        self.assertIsNone(contract.proof_error(proof, rows[row["id"]], rows))
        with seats._flocked(seats.roster_path() + ".lock"):
            roster = seats_common.roster_for_write()
            del roster["renamed-fixture"]
            pk.write_json(seats.roster_path(), roster)
        retired, err = store.retire("fixture-approval-tier", "2026-09-09T00:00:00Z", "fixture replaced")
        self.assertIsNone(err, err)
        self.assertIsNotNone(retired)
        with mock.patch.object(seats, "roster", side_effect=AssertionError("live roster")), \
                mock.patch.object(store, "load_certain_policy", side_effect=AssertionError("live policy")), \
                mock.patch.object(dispatches, "_approval_identity_family_evidence", side_effect=AssertionError("live runtime")):
            self.assertIsNone(contract.proof_error(proof, rows[row["id"]], rows))
            self.assert_fold(manifest, receipt)
        out, err = self.close(row, manifest, receipt, fan_out=False)
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        replay, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        self.assertEqual(replay[row["id"]]["close_reason"], "landed")
        self.assertEqual(replay[row["id"]]["recipient"], "review-fixture")


class FixtureIsolationTest(unittest.TestCase):
    def test_lifecycle_cleanup_restores_environment_and_seams_after_exception(self):  # noqa: VACUOUS_ASSERTION — this arm's positive controls must observe the fixture MID-FLIGHT, so they necessarily sit inside the try that drives its failure path: HELM_HOME set to case.tmp/home by setUp, and the HELM_FIXTURE_CLEANUP_CONTROL key this arm adds, whose removal the united key sets below would name. No POST-cleanup control may name a key at all, because tests/__init__.py:189-195 pops HELM_HOME in a MELD_HOME-only environment -- requiring one was task/2370's regression
        """Exercise this module's real setup/cleanup, including a failed child seam."""
        environment = dict(os.environ)
        home_resolver = dispatches.home_repo_id
        live_seats = proxywatch._live_seats
        queued = gate._queued_process
        suite = gate.SUITE
        case = LifecycleTest("test_real_compose_locked_close_replay_and_retry")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            try:
                case.setUp()
                self.assertEqual(os.environ["HELM_HOME"], str(case.tmp / "home"))
                self.assertIsNot(dispatches.home_repo_id, home_resolver)
                self.assertIsNot(proxywatch._live_seats, live_seats)
                self.assertTrue(case.tmp.is_dir())
                with serial_process(ran=3):
                    self.assertIsNot(gate._queued_process, queued)
                    control = mock.patch.dict(
                        os.environ, {"HELM_FIXTURE_CLEANUP_CONTROL": "temporary"})
                    control.start()
                    case.addCleanup(control.stop)
                    self.assertEqual(os.environ["HELM_FIXTURE_CLEANUP_CONTROL"], "temporary")
                    raise RuntimeError("fixture failure")
            finally:
                self.assertTrue(case.doCleanups())
        # THE KEYS THAT MOVED, NEVER THE MAPPING (task/2370). The old form
        # compared two full copies of this process's environment, so a
        # cleanup defect printed every ambient value — real credentials on a
        # build node — into the report and the fab artifact. Key sets united,
        # so an added, a removed and a changed key all still fail here.
        now = dict(os.environ)
        # ABSENCE IS A RESTORABLE STATE, so nothing here may require a key to
        # be PRESENT afterwards. tests/__init__.py:189-195 pops HELM_HOME when
        # MELD_HOME carries the operator's choice (planted at line 235), so an
        # `assertIn("HELM_HOME", ...)` postcondition fails on a legitimate
        # MELD_HOME-only environment that the old dict equality accepted. What
        # proves this arm is not vacuous is already above and does not depend
        # on the ambient environment: the pre-cleanup assertion that setUp put
        # case.tmp/"home" in HELM_HOME, and HELM_FIXTURE_CLEANUP_CONTROL, a key
        # this arm itself adds and whose removal the united key sets below see.
        self.assertEqual([k for k in sorted(set(now) | set(environment))
                          if now.get(k) != environment.get(k)], [],
                         "cleanup left these environment KEYS unrestored")
        self.assertIs(dispatches.home_repo_id, home_resolver)
        self.assertIs(proxywatch._live_seats, live_seats)
        self.assertIs(gate._queued_process, queued)
        self.assertEqual(gate.SUITE, suite)
        self.assertFalse(case.tmp.exists())


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
