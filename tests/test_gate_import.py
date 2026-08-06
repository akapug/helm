"""gate import — the strict seam for remotely-minted receipts.

Every arm here fails exactly one clause of the import ladder, and the fixture
diverges on precisely the field that clause reads — a receipt built by the
REAL minting grammar (gate._receipt_id over a real commit in a real scratch
repo), then perturbed one field at a time. The happy path is proven by
reading the DESTINATION ledgers back, never the return value alone."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import eventledger, gate, gateimport, pk

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo] + list(args),
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise AssertionError("git %s: %s" % (args, p.stderr))
    return p.stdout.strip()


class ImportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gate-import-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "import-test-seat"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", ".")
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("content\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")

    def tearDown(self):
        for key in ENV_KEYS:
            prior = self.prior.get(key)
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, **over):
        """A receipt the REAL grammar would mint: id computed by the same
        function the gate uses, over this scratch repo's real head/tree."""
        row = {"v": 3, "event": "gate", "ts": pk.now_ts(),
               "repo_id": "/remote/fab/wt/some-lane-abc12345",
               "head": self.head, "tree": self.tree, "dirty": False,
               "head_after": self.head, "tree_after": self.tree,
               "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.14.6",
                               "language": "3.14.6",
                               "executable": "/usr/bin/python3.14"},
               "argv": ["/usr/bin/python3.14", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 12.5,
               "status": "OK", "ran": 100, "skipped": 1, "detail": "",
               "elapsed": 12.0, "failures": [], "failures_unreadable": False,
               "base_check": None}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        return row

    def _artifact(self, *rows):
        path = os.path.join(self.tmp, "gate-receipts.jsonl")
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    def _import(self, artifact, **kw):
        return gateimport.import_receipt(artifact, self.repo, **kw)

    def _ledger(self):
        try:
            with open(gate.receipts_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def _provenance(self):
        try:
            with open(gateimport.imports_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []


class HappyPathTest(ImportBase):
    def test_a_genuine_row_appends_verbatim_and_audits_its_origin(self):
        row = self._row()
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(err)
        self.assertEqual(verdict, "imported")
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        # VERBATIM: the stored row is byte-equivalent, id included — an
        # import that "improves" the row breaks its content identity.
        self.assertEqual(gateimport._canonical(stored[0]),
                         gateimport._canonical(row))
        prov = self._provenance()
        self.assertEqual(len(prov), 1)
        self.assertEqual(prov[0]["receipt"], row["id"])
        self.assertEqual(prov[0]["artifact"], os.path.abspath(self._artifact(row)))
        self.assertEqual(prov[0]["repo"], self.repo)
        self.assertEqual(prov[0]["actor"], "import-test-seat")
        # origin is READ, never typed or parsed-into-existence: repo = the
        # receipt's own repo_id; run and host stay ABSENT (a parsed tmp
        # dirname read back as a "run" is the same rumour class as an
        # inferred host — presence control: origin_repo is the real field).
        self.assertEqual(prov[0]["origin_repo"], row["repo_id"])
        self.assertNotIn("origin_host", prov[0])  # noqa: VACUOUS_ASSERTION — receipt/artifact/repo/actor controls above prove the provenance row is real and populated
        self.assertNotIn("origin_run", prov[0])  # noqa: VACUOUS_ASSERTION — same populated-row controls; absence means unknown origin was not invented

    def test_the_receipt_ledger_itself_never_records_the_import(self):
        # Distinguishable lives in the PROVENANCE ledger; the receipt row
        # stays exactly what the minting machine wrote, so its id resolves.
        row = self._row()
        self._import(self._artifact(row))
        stored = self._ledger()
        self.assertEqual(stored[0]["id"], row["id"])   # positive control:
        # the row IS there — so the absence below is about content, not
        # about an empty ledger.
        self.assertNotIn("imported", json.dumps(stored[0]))  # noqa: VACUOUS_ASSERTION — presence control three lines up: stored[0]["id"] == row["id"] proves the row exists before this absence is read


class OrderingTest(ImportBase):
    def test_provenance_is_durable_before_the_receipt_append(self):  # noqa: VACUOUS_ASSERTION — the append-path order assertion is an unconditional positive control; the empty-ledger comparison is the presence control for the preceding assertion
        row = self._row()
        real, paths = eventledger.append, []

        def append(path, event):
            paths.append(path)
            return real(path, event)

        with mock.patch.object(gateimport.eventledger, "append",
                               side_effect=append):
            _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(paths, [gateimport.imports_path(), gate.receipts_path()])
        self.assertEqual(self._provenance()[0]["receipt"], row["id"])
        self.assertEqual(self._ledger()[0]["id"], row["id"])

    def test_receipt_append_failure_leaves_only_the_harmless_provenance_orphan(self):  # noqa: VACUOUS_ASSERTION — the provenance receipt-id control proves the first append landed before receipt-ledger absence is asserted
        row = self._row()
        real, paths = eventledger.append, []

        def append(path, event):
            paths.append(path)
            return False if path == gate.receipts_path() else real(path, event)

        with mock.patch.object(gateimport.eventledger, "append",
                               side_effect=append):
            got, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(got)
        self.assertIsNone(verdict)
        self.assertIn("provenance appended but receipt append FAILED", err)
        self.assertEqual(paths, [gateimport.imports_path(), gate.receipts_path()])
        self.assertEqual(self._provenance()[0]["receipt"], row["id"])
        self.assertEqual(self._ledger(), [])


class CliShapeTest(ImportBase):
    def test_help_prints_usage_without_reading_or_importing(self):  # noqa: VACUOUS_ASSERTION — exact usage output positively proves the help arm fired before destination-ledger absence is asserted
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gateimport.cmd_import(["--help"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), gateimport.IMPORT_USAGE)
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])

    def test_origin_host_and_run_flags_are_not_an_import_surface(self):  # noqa: VACUOUS_ASSERTION — the unknown-argument refusal and empty destination ledger positively prove the obsolete flag was parsed and rejected
        row = self._row()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gateimport.cmd_import([
                self._artifact(row), "--repo", self.repo,
                "--host", "inferred-host", "--run", "inferred-run"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--host'", err.getvalue())
        self.assertEqual(self._ledger(), [])
        self.assertEqual(self._provenance(), [])


class ActorResolutionTest(ImportBase):
    def test_actor_is_the_seat_from_the_identity_law_not_the_os_user(self):
        # importer shells often declare no HELM_CHAT_NAME; the identity law
        # (acting_seat) still resolves a seat via its floor — the OS user is
        # the fallback of last resort, never the value.
        row = self._row()
        with mock.patch("helm.gateimport.seats.acting_seat",
                        return_value="floor-resolved-seat"):
            self._import(self._artifact(row))
        self.assertEqual(self._provenance()[0]["actor"], "floor-resolved-seat")

    def test_actor_is_absent_when_the_identity_law_answers_nothing(self):
        row = self._row()
        with mock.patch("helm.gateimport.seats.acting_seat",
                        return_value=None):
            self._import(self._artifact(row))
        provenance = self._provenance()[0]
        self.assertEqual(provenance["receipt"], row["id"])
        self.assertNotIn("actor", provenance)  # noqa: VACUOUS_ASSERTION — receipt identity above proves the provenance row exists; absence means no process location was laundered into actor identity


class TamperTest(ImportBase):
    def test_an_edited_status_stops_resolving(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the honest row imports and the ledger flips [] -> 1 on the same observable
        row = self._row()
        row["status"] = "OK" if row["status"] != "OK" else "FAILED"
        # id deliberately NOT recomputed: this is the pasted-verdict shape.
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("content id mismatch", err)
        self.assertEqual(self._ledger(), [])
        # positive control on the same observable: the HONEST row imports,
        # so the emptiness above measured the refusal, not a broken pipeline.
        _, verdict, err = self._import(self._artifact(self._row()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_a_pasted_base_check_stops_resolving(self):
        row = self._row(status="FAILED", rc=1,
                        failures=[{"kind": "FAIL", "test": "t.x",
                                   "traceback": "tb"}])
        row["base_check"] = {"verdict": "STALE_BASE"}
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("content id mismatch", err)


class SchemaTest(ImportBase):
    def test_an_unknown_version_is_refused_not_half_validated(self):
        # 5, NOT 4. This arm named v4 as its unknown example, which made it a
        # test of TODAY'S BOUNDARY rather than of the refusal: the moment v4
        # became known it would have gone green off the id clause instead,
        # while testing nothing about versions at all
        # (same-refusal-different-gate). The id is now valid for its shape, so
        # only the version can be what stops it.
        row = self._row()
        row["v"] = 5
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("version", err)

    def test_a_row_missing_minted_keys_refuses(self):
        row = self._row()
        del row["interpreter"]
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("missing minted keys", err)
        self.assertIn("interpreter", err)

    def test_a_row_carrying_foreign_keys_refuses(self):
        row = self._row()
        row["imported_by"] = "someone"
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("keys no gate mints", err)

    def test_a_non_gate_event_refuses(self):
        row = self._row()
        row["event"] = "land"
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("not 'gate'", err)


class HostBoundVersionTest(ImportBase):
    """v4 receipts name their HOST, and every READER learns them here — one
    commit before anything mints one.

    WHY THE HALVES SHIP APART. The minting side and the import side share NOT
    ONE FILE, so every changed-file overlap check clears the pair while the
    composition is broken. Ship the writer first and a fab-minted receipt is
    refused loudly at `gate import` and, worse, SILENTLY SKIPPED by
    `gate.receipts()` — whose integrity filter recomputes the id, gets a
    different answer under a pre-v4 reader, and drops the row. `by_id` then
    reports "no minted gate receipt" about a receipt physically present in the
    ledger, and advises re-running the gate, which mints another unreadable
    one. Measured live: 685 rows read, skipped 1, a
    reviewer's APPROVE unable to bind, three functions traced to find out why.

    `host` is REQUIRED at v4 and FOREIGN below it, never merely optional: it is
    bound into the receipt id, so a v4 row without one and a v3 row with one
    both describe a minting that never happened."""

    _HOST = {"node": "box-a", "system": "Linux",
             "release": "6.17.0-40-generic", "id": "0123456789abcdef"}

    def _v4(self, **over):
        # The host block is written LITERALLY, never taken from a gate.host()
        # helper — this tree has none, and that is the property under test.
        return self._row(v=4, host=dict(self._HOST), **over)

    def test_a_v4_host_bound_receipt_imports(self):
        row = self._v4()
        _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        stored = self._ledger()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], row["id"])
        self.assertEqual(stored[0]["v"], 4)
        # The host block survives VERBATIM. An import that dropped it would
        # still satisfy "a row landed", and the receipt would no longer name
        # the machine that is the entire point of the version.
        self.assertEqual(stored[0]["host"], self._HOST)

    def test_a_v4_receipt_without_a_host_refuses(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: restoring the host imports through the SAME path and the ledger [] -> 1
        row = self._v4()
        del row["host"]
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("missing minted keys", err)
        self.assertIn("host", err)
        self.assertEqual(self._ledger(), [])
        _, verdict, err = self._import(self._artifact(self._v4()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_a_v3_receipt_carrying_a_host_refuses(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: dropping the host imports through the SAME path and the ledger [] -> 1
        row = self._row(host=dict(self._HOST))
        row["id"] = gate._receipt_id(row)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("keys no gate mints", err)
        self.assertIn("host", err)
        self.assertEqual(self._ledger(), [])
        _, verdict, err = self._import(self._artifact(self._row()))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)

    def test_v3_still_imports_beside_v4(self):
        """The positive control for both refusals, on the SAME seam: the
        version gate still passes everything it always passed."""
        three, four = self._row(), self._v4()
        _, verdict, err = self._import(self._artifact(three))
        self.assertEqual((verdict, err), ("imported", None))
        _, verdict, err = self._import(self._artifact(four))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual({r["v"] for r in self._ledger()}, {3, 4})

    def test_the_host_is_bound_into_the_id_not_merely_carried(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the UNEDITED row imports and the ledger flips [] -> 1 on the same observable. Each _receipt_id() call is a fresh producer by the rung's own provenance rule, so no control it can credit exists for a hash-binding test.
        """Edit the node and the receipt must stop resolving — the same
        tamper-evidence every other bound field gets. A `host` that rode along
        unbound would let a fab receipt be re-labelled with another box's name
        and still verify."""
        row = self._v4()
        original = gate._receipt_id(row)
        moved = dict(row, host=dict(self._HOST, node="box-b"))
        self.assertNotEqual(gate._receipt_id(moved), original)
        # and the ledger refuses the edited row rather than storing it
        moved["id"] = original
        _, verdict, err = self._import(self._artifact(moved))
        self.assertIsNone(verdict)
        self.assertEqual(self._ledger(), [])
        # POSITIVE CONTROL on the same path: unedited, it imports.
        _, verdict, err = self._import(self._artifact(row))
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)


    def test_v4_keeps_every_earlier_versions_bindings(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the FIRST line of the body: an unedited copy recomputes to the stored id. The rung cannot credit it because each _receipt_id() call mints a separate producer identity.
        """A version bump must GROW the id, never re-cut it.

        `_receipt_id` gates the failure identities on `v in (2,3,4)` and the
        base check on `v in (3,4)`. Written as `== 2` / `== 3` they would look
        equally correct and would silently stop binding those fields for v4 —
        an edited failure list or a pasted STALE_BASE would resolve fine on a
        v4 receipt while still being caught on a v3 one. The file's comment
        names that hazard; this asserts it, because a mutation reverting the
        tuples passed the entire suite before this test existed."""
        base = self._v4()
        # POSITIVE CONTROL FIRST, unconditional: an UNEDITED copy recomputes to
        # exactly the stored id, so every inequality below is a real difference
        # and not a helper that returns junk for anything handed to it.
        self.assertEqual(gate._receipt_id(dict(base)), base["id"])
        for field, edited in (("failures", [{"id": "tests.forged.Case.test_x"}]),
                              ("failures_unreadable", True),
                              ("base_check", {"verdict": "STALE_BASE"})):
            moved = dict(base)
            moved[field] = edited
            self.assertNotEqual(
                gate._receipt_id(moved), gate._receipt_id(base),
                "%s is not bound into a v4 receipt id, so editing it leaves "
                "the receipt resolving — the version bump dropped a binding "
                "an earlier version had" % field)


class ReaderBeforeWriterTest(ImportBase):
    """THIS TREE READS v4 AND MUST NOT MINT IT.

    The whole value of landing the reader alone is that no v4 receipt exists
    until every helm can verify one. A well-meaning edit that flips
    `_mint_result` here would restore the exact silent-skip this commit
    prevents, and every other test in this file would stay green while it
    happened — they all construct their rows by hand."""

    def test_the_writer_flipped_only_after_every_reader_landed(self):
        """THE GUARD DID ITS JOB AND IS NOW INVERTED, deliberately.

        Its previous form asserted `"v": 3` and refused to let the writer flip
        in the same tree that taught the reader. It fired on exactly the change
        it was written for — this commit — which is what forced the flip to be
        its own reviewed land against a trunk that can already read v4. The
        assertion turns over rather than being deleted, because the property
        worth keeping is not "we mint v3", it is that MINT AND READER AGREE."""
        import inspect
        source = inspect.getsource(gate._mint_result)
        self.assertIn('"v": 4', source)
        self.assertNotIn('"v": 3', source)
        # and the reader that must already understand it is HERE, on trunk,
        # not merely promised by this lane
        self.assertIn(4, gateimport.KNOWN_VERSIONS)
        self.assertEqual(gateimport.VERSION_KEYS.get(4), frozenset(("host",)))

    def test_v3_receipt_ids_are_untouched_by_the_v4_grammar(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control is the first assertion: the v3 row recomputes to its own stored id before anything is compared against it.
        """The one property that makes landing this safe: every id already in
        the wild must recompute to exactly what it did before. A v4 branch that
        perturbed the v3 payload would invalidate every stored receipt at
        once."""
        row = self._row()
        self.assertEqual(row["id"], gate._receipt_id(row))
        # a v3 row is unaffected by the host grammar even when the key exists
        # on some OTHER row in the same ledger
        self.assertEqual(gate._receipt_id(dict(row)), row["id"])
        # and the v4 grammar really is reachable — otherwise the equality above
        # proves only that nothing ran
        four = self._row(v=4, host=dict(HostBoundVersionTest._HOST))
        self.assertNotEqual(gate._receipt_id(four), row["id"])


class RepoResolutionTest(ImportBase):
    def test_a_head_this_repo_never_saw_refuses(self):
        row = self._row(head="deadbeef" * 5)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("does not resolve", err)

    def test_a_tree_the_cited_head_cannot_produce_refuses(self):
        # head is real, tree is another commit's — the receipt-about-a-tree-
        # nobody-can-check-out shape, including every dirty-run receipt.
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("advanced\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "advance")
        other_tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        row = self._row(tree=other_tree)
        _, verdict, err = self._import(self._artifact(row))
        self.assertIsNone(verdict)
        self.assertIn("tree mismatch", err)


class IdempotencyTest(ImportBase):
    def test_the_same_import_twice_is_a_no_op_not_a_second_row(self):
        row = self._row()
        art = self._artifact(row)
        self._import(art)
        _, verdict, err = self._import(art)
        self.assertIsNone(err)
        self.assertEqual(verdict, "duplicate")
        self.assertEqual(len(self._ledger()), 1)
        # And no second provenance row: a no-op that audits itself as an
        # import would double-count origins.
        self.assertEqual(len(self._provenance()), 1)

    def test_an_edited_advisory_field_is_caught_at_the_conflict_clause(self):
        # detail/wall/label are ADVISORY: the content id deliberately does
        # not bind them ("every field a reader would RELY on"), so editing
        # one passes clause 3 — and the stored-content comparison is the
        # clause that catches it. Two guards, different fields; this arm
        # proves the second exists.
        row = self._row()
        self._import(self._artifact(row))
        conflict = dict(row)
        conflict["detail"] = "edited after storage"
        _, verdict, err = self._import(self._artifact(conflict))
        self.assertIsNone(verdict)
        self.assertIn("DIFFERENT content", err)
        self.assertEqual(len(self._ledger()), 1)

    def test_a_planted_ledger_row_under_the_same_id_refuses(self):
        # The ledger already holds a row wearing this id with OTHER hashed
        # content (only reachable by hand-editing the ledger — the exact
        # shape this verb exists to end). The import must refuse
        # both directions rather than trust either copy.
        stored = self._row()
        planted = dict(stored)
        planted["ran"] = 999
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w") as f:
            f.write(json.dumps(planted) + "\n")
        _, verdict, err = self._import(self._artifact(stored))
        self.assertIsNone(verdict)
        self.assertIn("DIFFERENT content", err)


class ArtifactShapeTest(ImportBase):
    def test_a_multi_row_artifact_requires_id(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: the same call with --id flips to ("imported", None)
        # rows must differ on a HASHED field or they share an id (label and
        # same-second ts are not bound — measured while building this arm).
        a, b = self._row(), self._row(ran=200)
        self.assertNotEqual(a["id"], b["id"])
        _, verdict, err = self._import(self._artifact(a, b))
        self.assertIsNone(verdict)
        self.assertIn("--id", err)
        # positive control: naming the row flips the same call to success.
        _, verdict, err = self._import(self._artifact(a, b), want_id=a["id"])
        self.assertEqual((verdict, err), ("imported", None))

    def test_id_selects_the_named_row_from_a_multi_row_artifact(self):
        a, b = self._row(), self._row(ran=200)
        _, verdict, err = self._import(self._artifact(a, b),
                                       want_id=b["id"])
        self.assertIsNone(err)
        self.assertEqual(verdict, "imported")
        self.assertEqual(self._ledger()[0]["ran"], 200)

    def test_a_repeated_id_inside_one_artifact_refuses_even_with_id(self):
        a = self._row()
        _, verdict, err = self._import(self._artifact(a, dict(a)),
                                       want_id=a["id"])
        self.assertIsNone(verdict)
        self.assertIn("appears 2 times", err)

    def test_a_torn_artifact_contributes_nothing(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: mending the tear flips the same artifact to ("imported", None) and the ledger [] -> 1
        row = self._row()
        path = self._artifact(row)
        with open(path, "a") as f:
            f.write('{"torn": tru')
        _, verdict, err = self._import(path)
        self.assertIsNone(verdict)
        self.assertIn("not JSON", err)
        self.assertEqual(self._ledger(), [])
        # positive control: mend the tear and the SAME artifact imports —
        # the emptiness above was the refusal, not a dead write path.
        with open(path, "w") as f:
            f.write(json.dumps(row) + "\n")
        _, verdict, err = self._import(path)
        self.assertEqual((verdict, err), ("imported", None))
        self.assertEqual(len(self._ledger()), 1)


if __name__ == "__main__":
    unittest.main()
