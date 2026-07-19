import contextlib
import io
import os
import re
import shutil
import tempfile
import unittest

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import drain, home, pk  # noqa: E402


def _mem_entry(name, etype, description, body="the full body\nwith detail\n"):
    return ("---\nname: %s\ndescription: \"%s\"\nmetadata:\n  node_type: memory\n"
            "  type: %s\n  originSessionId: sess-1\n---\n\n%s"
            % (name[:-3], description, etype, body))


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.mem = tempfile.mkdtemp(prefix="helm-test-mem-")
        write = lambda n, t: pk.atomic_write(os.path.join(self.mem, n), t)
        write("feedback-short-dms.md", _mem_entry(
            "feedback-short-dms.md", "feedback", "DMs must be short and direct"))
        write("reference-cool-tool.md", _mem_entry(
            "reference-cool-tool.md", "reference", "a tool worth keeping"))
        write("proj-meldproj-vision.md", _mem_entry(
            "proj-meldproj-vision.md", "project", "the meldproj roadmap"))
        write("proj-unknown-thing.md", _mem_entry(
            "proj-unknown-thing.md", "project", "no registry match here"))
        write("random-note.md", _mem_entry("random-note.md", "", "just a note"))
        write("prem-old-truth.md", _mem_entry("prem-old-truth.md", "prior", "x"))
        write("prior-old-truth.md", _mem_entry("prior-old-truth.md", "prior", "x"))
        write("prem-lonely.md", _mem_entry("prem-lonely.md", "prior", "no twin"))
        write("lex-term.md", _mem_entry("lex-term.md", "lexicon", "typed already"))
        pk.atomic_write(os.path.join(self.mem, "MEMORY.md"),
                        "# idx\n- [short dms](feedback-short-dms.md) hook\n")
        # a registry with one project so route-project resolves
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "meldproj": {"name": "meldproj", "path": "/x", "kind": "git"}}})

    def tearDown(self):
        shutil.rmtree(self.mem, ignore_errors=True)

    def _plan(self):
        return {a["src"]: a for a in drain.classify(self.mem)}

    def test_classify(self):
        plan = self._plan()
        self.assertEqual(plan["feedback-short-dms.md"]["op"], "retype")
        self.assertEqual(plan["feedback-short-dms.md"]["dst"], "prior-short-dms.md")
        self.assertEqual(plan["reference-cool-tool.md"]["dst"], "ref-cool-tool.md")
        self.assertEqual(plan["proj-meldproj-vision.md"]["op"], "route-project")
        self.assertEqual(plan["proj-meldproj-vision.md"]["project"], "meldproj")
        self.assertEqual(plan["proj-unknown-thing.md"]["op"], "keep")
        self.assertEqual(plan["prem-old-truth.md"]["op"], "sweep-dup")
        self.assertNotIn("prem-lonely.md", plan)   # un-twinned prem stays governed
        self.assertNotIn("lex-term.md", plan)      # already typed
        self.assertEqual(plan["random-note.md"]["op"], "keep")

    def test_apply_retype_preserves_body_and_seeds_evidence(self):
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertGreaterEqual(receipt["applied"], 3)
        newp = os.path.join(self.mem, "prior-short-dms.md")
        self.assertTrue(os.path.isfile(newp))
        with open(newp) as fh:
            raw = fh.read()
        self.assertIn("the full body", raw)             # body preserved verbatim
        self.assertIn("type: prior", raw)
        self.assertIn("confidence: 0.90", raw)
        self.assertIn("drained_from: feedback-short-dms.md", raw)
        self.assertIn('"type":"stated"', raw)           # evidence seeded
        self.assertFalse(os.path.exists(os.path.join(self.mem, "feedback-short-dms.md")))
        # rollback net holds the original
        net = receipt["net"]
        self.assertTrue(os.path.isfile(os.path.join(net, "feedback-short-dms.md")))
        self.assertTrue(os.path.isfile(os.path.join(net, "RECEIPT.json")))
        # the index re-pointed
        with open(os.path.join(self.mem, "MEMORY.md")) as fh:
            idx = fh.read()
        self.assertIn("(prior-short-dms.md)", idx)

    def test_route_project_lands_in_journal(self):
        drain.apply(drain.classify(self.mem), self.mem)
        dst = os.path.join(home.project_dir("meldproj"), "journal",
                           "proj-meldproj-vision.md")
        self.assertTrue(os.path.isfile(dst))
        self.assertFalse(os.path.exists(os.path.join(self.mem, "proj-meldproj-vision.md")))

    def test_sweep_dups_gated(self):
        drain.apply(drain.classify(self.mem), self.mem)               # no flag
        self.assertTrue(os.path.exists(os.path.join(self.mem, "prem-old-truth.md")))
        drain.apply(drain.classify(self.mem), self.mem, sweep_dups=True)
        self.assertFalse(os.path.exists(os.path.join(self.mem, "prem-old-truth.md")))
        self.assertTrue(os.path.exists(os.path.join(self.mem, "prior-old-truth.md")))

    def test_dry_run_mutates_nothing(self):
        before = sorted(os.listdir(self.mem))
        drain.classify(self.mem)
        self.assertEqual(before, sorted(os.listdir(self.mem)))

    def test_limit(self):
        receipt = drain.apply(drain.classify(self.mem), self.mem, limit=1)
        self.assertEqual(receipt["applied"], 1)


class KeywordsFromTest(unittest.TestCase):
    """The fixed derivation: FULL statement, distinctive words (>= 5 chars,
    non-generic), frequency-then-length ranked, cap 8 — the old slug+80-chars
    version minted weak generic keywords for the drained cohort."""

    def test_distinctive_words_chosen_generics_excluded(self):
        kw = drain._keywords_from(
            "short-dms", "agent DMs must build toward short direct messages").split(",")
        self.assertIn("short", kw)
        self.assertIn("direct", kw)
        self.assertIn("messages", kw)
        for g in ("build", "agent", "must"):  # generic or < 5 chars
            self.assertNotIn(g, kw)

    def test_full_statement_not_first_80_chars(self):
        stmt = "x " * 50 + "quorumward is the load bearing word"
        self.assertIn("quorumward", drain._keywords_from("slugword", stmt).split(","))

    def test_repeated_topic_word_ranks_first_cap_8(self):
        stmt = ("checkpoint early checkpoint often checkpoint always; "
                "alpha1 beta22 gamma333 delta4444 epsilon5 zetas66 etaxx77 thetas888")
        kw = drain._keywords_from("", stmt).split(",")
        self.assertEqual(kw[0], "checkpoint")  # freq 3 beats every singleton
        self.assertEqual(len(kw), 8)           # capped

    def test_empty_statement_falls_back_to_slug(self):
        self.assertEqual(drain._keywords_from("atomic-write", ""), "atomic,write")
        self.assertEqual(drain._keywords_from("a-b", ""), "")


class RekeyTest(unittest.TestCase):
    """drain --rekey: the one-time drained-cohort keyword migration — in-place
    two-line edit, evidence receipt as the idempotence marker, dry-run default."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rekey-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "adopted")
        os.makedirs(self.mem)
        os.environ["HELM_ADOPTED_DIR"] = self.mem
        # a real drained prior, minted by drain.apply itself
        pk.atomic_write(os.path.join(self.mem, "feedback-atomic-writes.md"), _mem_entry(
            "feedback-atomic-writes.md", "feedback",
            "always use atomic writes so a torn checkpoint never lands"))
        pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
        drain.apply(drain.classify(self.mem), self.mem)
        self.path = os.path.join(self.mem, "prior-atomic-writes.md")
        # simulate the drained cohort: overwrite with the OLD derivation's
        # weak keywords (drain now writes the fixed set at drain time)
        raw = re.sub(r"^(  keywords:).*$", r"\1 use,atomic,writes,torn",
                     self.read(self.path), count=1, flags=re.M)
        pk.atomic_write(self.path, raw)
        # a NON-drained store prior: rekey must never touch it
        from helm import store
        self.other = store.write_prior({"id": "hand-authored", "statement":
                                        "authored by hand", "confidence": 0.8,
                                        "keywords": "handkw"})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_dry_run_writes_nothing(self):
        before = self.read(self.path)
        r = drain.rekey()
        self.assertEqual((r["drained"], r["already"], r["rekeyed"]), (1, 0, 0))
        self.assertEqual(len(r["changes"]), 1)
        self.assertEqual(self.read(self.path), before)

    def test_apply_rekeys_in_place_preserving_untouched_bytes(self):
        before = self.read(self.path)
        r = drain.rekey(apply=True)
        self.assertEqual(r["rekeyed"], 1)
        after = self.read(self.path)
        # exactly two lines changed: evidence_log + keywords; every other
        # line survives byte-identical, in order
        diff = [(b, a) for b, a in zip(before.splitlines(), after.splitlines())
                if b != a]
        self.assertEqual([b.split(":")[0] for b, _ in diff],
                         ["  evidence_log", "  keywords"])
        kws = next(a for _, a in diff if a.startswith("  keywords:")) \
            .split(":", 1)[1].strip().split(",")
        self.assertEqual(kws[0], "atomic")  # slug + statement, freq 2
        self.assertIn("checkpoint", kws)
        self.assertNotIn("torn", kws)       # < 5 chars — no longer minted
        self.assertNotIn("use", kws)        # generic stays out
        self.assertIn('"type":"rekeyed"', after)  # the receipt landed
        # body + provenance untouched
        self.assertIn("the full body", after)
        self.assertIn("drained_from: feedback-atomic-writes.md", after)
        # the non-drained prior is untouched
        self.assertNotIn("rekeyed", self.read(self.other))

    def test_idempotent_second_run_skips_via_marker(self):
        drain.rekey(apply=True)
        first = self.read(self.path)
        r = drain.rekey(apply=True)
        self.assertEqual((r["drained"], r["already"], r["rekeyed"]), (1, 1, 0))
        self.assertEqual(self.read(self.path), first)

    def test_cmd_flag_dry_run_default(self):
        before = self.read(self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.cmd_drain(["--rekey"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("1 to rekey", out.getvalue())
        self.assertEqual(self.read(self.path), before)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.cmd_drain(["--rekey", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("REKEYED 1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
