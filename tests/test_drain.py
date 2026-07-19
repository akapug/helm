import json
import os
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


if __name__ == "__main__":
    unittest.main()
