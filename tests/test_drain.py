import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

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
        # a GENUINELY typed lexicon (term + definition) — drain-v2 leaves it
        # governed; the old fixture (name+description only) was never real-typed
        pk.atomic_write(os.path.join(self.mem, "lex-term.md"),
                        "---\nname: lex-term\ndescription: \"lexicon: term = defined\"\n"
                        "metadata:\n  node_type: memory\n  type: lexicon\n  term: term\n"
                        "  definition: a defined word\n---\nbody\n")
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
        # drain-v2: an un-twinned prem- bulk file is now an upgrade candidate;
        # "no twin" is too terse (<2 specific keywords) so it stays episodic
        self.assertEqual(plan["prem-lonely.md"]["op"], "keep")
        self.assertIn("specific keyword", plan["prem-lonely.md"]["why"])
        self.assertNotIn("lex-term.md", plan)      # genuinely typed -> governed
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


class AliasRoutingTest(unittest.TestCase):
    """The registry alias map fixes unroutable global project entries: a short
    or old handle (built-in buildr->buildr-private-beta, mc->mission-control,
    plus authored per-project aliases) routes to the canonical project."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-alias-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "buildr-private-beta": {"name": "buildr-private-beta", "path": "/x/b",
                                    "kind": "git", "sessions": {}},
            "mission-control": {"name": "mission-control", "path": "/x/mc",
                                "kind": "git", "sessions": {}, "aliases": ["mc"]}}})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _classify(self):
        return {a["src"]: a for a in drain.classify(self.mem)}

    def test_builtin_alias_routes_by_description(self):
        pk.atomic_write(os.path.join(self.mem, "proj-note.md"),
                        _mem_entry("proj-note.md", "project", "the buildr roadmap and vision"))
        a = self._classify()["proj-note.md"]
        self.assertEqual((a["op"], a["project"]), ("route-project", "buildr-private-beta"))

    def test_authored_short_alias_routes_by_filename(self):
        # 'mc' is 2 chars — filename prefix only (a 2-char word wallpapers the corpus)
        pk.atomic_write(os.path.join(self.mem, "mc-standup.md"),
                        _mem_entry("mc-standup.md", "project", "the standup notes"))
        a = self._classify()["mc-standup.md"]
        self.assertEqual((a["op"], a["project"]), ("route-project", "mission-control"))

    def test_short_alias_never_wallpapers_by_description(self):
        # 'mc' appearing as a description word must NOT route (len < 6 guard)
        pk.atomic_write(os.path.join(self.mem, "random-thing.md"),
                        _mem_entry("random-thing.md", "project", "the mc was loud"))
        self.assertEqual(self._classify()["random-thing.md"]["op"], "keep")


class DrainProjectTest(unittest.TestCase):
    """drain --project P drains the project's OWN claude memory dir with the
    identical gauntlet. Hermetic: the claude memdir resolver is patched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-drainproj-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.projmem = os.path.join(self.tmp, "projmem")
        os.makedirs(self.projmem)
        pk.atomic_write(os.path.join(self.projmem, "feedback-x.md"),
                        _mem_entry("feedback-x.md", "feedback", "an x rule to keep"))
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "polyana": {"name": "polyana", "path": "/dev/polyana", "kind": "git",
                        "sessions": {}}}})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self):
        return mock.patch.object(
            home, "claude_memory_dir_for",
            side_effect=lambda p: self.projmem if p == "/dev/polyana" else "/nonexistent-xyz")

    def test_drain_project_scans_project_memdir(self):
        with self._patch():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = drain.cmd_drain(["--project", "polyana"])
        self.assertEqual(rc, 0)
        self.assertIn("project polyana", out.getvalue())
        self.assertIn("retype", out.getvalue())
        self.assertIn("DRY-RUN", out.getvalue())

    def test_drain_project_apply_routes_in_place(self):
        with self._patch():
            plan = drain.classify(self.projmem)
            receipt = drain.apply(plan, self.projmem)
        self.assertGreaterEqual(receipt["applied"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.projmem, "prior-x.md")))

    def test_unknown_project_fails_open(self):
        with self._patch():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = drain.cmd_drain(["--project", "ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("no claude memory dir", out.getvalue())


class PromoteTest(unittest.TestCase):
    """helm promote: the episodic->durable funnel. USER-role only, length +
    marker gated, deduped, capped, incremental via (mtime,size) cache; hits land
    as drain-intake candidates the existing gauntlet then routes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-promote-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CACHE_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "adopted")
        os.makedirs(self.mem)
        os.environ["HELM_ADOPTED_DIR"] = self.mem
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.tx = os.path.join(self.tmp, "tx")
        os.makedirs(self.tx)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jsonl(self, name, lines):
        with open(os.path.join(self.tx, name), "w", encoding="utf-8") as f:
            for d in lines:
                f.write(json.dumps(d) + "\n")

    def _user(self, text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def _asst(self, text):
        return {"type": "assistant", "message": {"role": "assistant", "content": text}}

    def test_user_markers_only_land_as_candidates(self):
        self._jsonl("11111111-1111-1111-1111-111111111111.jsonl", [
            self._user("From now on always squelch the flimflam before deploy"),
            self._asst("Sure, from now on I will always do that"),   # assistant: ignored
            self._user("what's the weather"),                        # no marker: ignored
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "always ran the tool"}]}},  # tool_result: ignored
        ])
        r = drain.promote(since_days=30, apply=False, roots=[self.tx])
        self.assertEqual(r["found"], 1)
        self.assertEqual(r["actions"][0]["proposed_type"], "prior")
        self.assertIn("flimflam", r["actions"][0]["slug"])
        self.assertEqual(r["written"], 0)                            # dry-run
        self.assertEqual(os.listdir(self.mem), [])

    def test_apply_writes_intake_then_drain_routes_to_prior(self):
        self._jsonl("22222222-2222-2222-2222-222222222222.jsonl", [
            self._user("Remember this: the quorumward gate must be signed before ship"),
        ])
        r = drain.promote(since_days=30, apply=True, roots=[self.tx])
        self.assertEqual(r["written"], 1)
        cand = [n for n in os.listdir(self.mem) if n.startswith("feedback-promoted-")]
        self.assertEqual(len(cand), 1)
        with open(os.path.join(self.mem, cand[0])) as f:
            raw = f.read()
        self.assertIn("proposed_type: prior", raw)
        self.assertIn("origin_line: 1", raw)
        self.assertIn("capture_confidence: 0.50", raw)
        self.assertIn("type: feedback", raw)   # drain-routable
        # the existing gauntlet routes it to a typed prior
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertGreaterEqual(receipt["applied"], 1)
        newp = [n for n in os.listdir(self.mem) if n.startswith("prior-promoted-")]
        self.assertEqual(len(newp), 1)

    def test_length_guard_skips_task_prompts(self):
        self._jsonl("33333333-3333-3333-3333-333333333333.jsonl", [
            self._user("always " + "x" * 700),   # long -> a task prompt, not a rule
        ])
        self.assertEqual(drain.promote(since_days=30, roots=[self.tx])["found"], 0)

    def test_dedupe_against_existing_prior(self):
        from helm import store
        store.write_prior({"id": "quorumward-gate",
                           "statement": "the quorumward gate must be signed before ship",
                           "confidence": 0.9})
        self._jsonl("44444444-4444-4444-4444-444444444444.jsonl", [
            self._user("Always the quorumward gate must be signed before ship please"),
        ])
        self.assertEqual(drain.promote(since_days=30, roots=[self.tx])["found"], 0)

    def test_incremental_mtime_size_cache_and_cap(self):
        self._jsonl("55555555-5555-5555-5555-555555555555.jsonl", [
            self._user("From now on prefer the alpha1 path over beta2"),
            self._user("Never touch the gamma3 store directly"),
        ])
        r = drain.promote(since_days=30, cap=1, apply=True, roots=[self.tx])
        self.assertEqual(r["found"], 1)          # cap honored
        # the cap stopped mid-file, so it is NOT cached -> re-run sees the rest
        r2 = drain.promote(since_days=30, cap=5, apply=True, roots=[self.tx])
        self.assertEqual(r2["scanned"], 1)
        self.assertEqual(r2["found"], 1)         # the second rule (first already an intake dup)
        # a third run: file fully processed + cached -> skipped
        r3 = drain.promote(since_days=30, cap=5, apply=True, roots=[self.tx])
        self.assertEqual(r3["skipped_cache"], 1)
        self.assertEqual(r3["found"], 0)
        self.assertEqual(drain.pending_promotions(self.mem), 2)

    def test_cmd_dry_run_default(self):
        self._jsonl("66666666-6666-6666-6666-666666666666.jsonl", [
            self._user("From now on the widgetron must idle at 40hz"),
        ])
        # cmd_promote's default roots are ~/.claude; force them at the tmp tx
        orig = drain.promote
        with mock.patch.object(drain, "promote",
                               side_effect=lambda **kw: orig(**{**kw, "roots": [self.tx]})):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = drain.cmd_promote([])
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertIn("widgetron", out.getvalue())


def _bulk(name, description, mtype="project", body="the full body\nwith detail\n"):
    """A typed-PREFIX file with NO typed fields (name+description only) — the
    bulk-memory shape that falls back to episodic (the 194 dark files)."""
    return ("---\nname: %s\ndescription: \"%s\"\nmetadata:\n  node_type: memory\n"
            "  type: %s\n---\n\n%s" % (name[:-3], description, mtype, body))


class HermeticMemBase(unittest.TestCase):
    """Fresh HELM_HOME + intake dir per test — the upgrade/edge-case harness."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-upgrade-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem)
        os.environ["HELM_ADOPTED_DIR"] = self.mem
        pk.write_json(home.registry_path(), {"version": 1, "projects": {}})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, name, text):
        pk.atomic_write(os.path.join(self.mem, name), text)

    def _plan(self):
        return {a["src"]: a for a in drain.classify(self.mem)}


class UpgradeTest(HermeticMemBase):
    """drain-v2 upgrade: typed-prefix files that fell back to episodic get real
    typed frontmatter (prem-/prior- -> prior 0.9 jit; lex- -> lexicon), body
    verbatim, archive-first net + receipt. The >=2-specific-keyword gate holds."""

    def test_prem_bulk_upgrades_to_prior_at_0_9(self):
        self._w("prem-scrub-docs.md",
                _bulk("prem-scrub-docs.md", "scrub internal planning documents before every push"))
        act = self._plan()["prem-scrub-docs.md"]
        self.assertEqual((act["op"], act["to_type"], act["dst"]),
                         ("upgrade", "prior", "prior-scrub-docs.md"))
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertGreaterEqual(receipt["applied"], 1)
        newp = os.path.join(self.mem, "prior-scrub-docs.md")
        with open(newp) as f:
            raw = f.read()
        self.assertIn("type: prior", raw)
        self.assertIn("confidence: 0.90", raw)          # 0.9, NOT 1.0
        self.assertIn("class: prior", raw)
        self.assertIn("upgraded from typed-prefix bulk memory", raw)
        self.assertIn("the full body", raw)             # body verbatim
        self.assertIn("upgraded_from: prem-scrub-docs.md", raw)
        self.assertFalse(os.path.exists(os.path.join(self.mem, "prem-scrub-docs.md")))
        # it was DARK before; now it resolves + fires
        from helm import store
        e = self.one_store(store.load_all(), "scrub-docs")
        self.assertEqual((e["type"], e["load_class"]), ("prior", "jit"))
        self.assertAlmostEqual(e["confidence"], 0.9)
        self.assertTrue(store.resolve_prompt("scrub the planning documents"))

    def one_store(self, es, eid):
        hits = [e for e in es if e["id"] == eid]
        assert len(hits) == 1, [x["id"] for x in es]
        return hits[0]

    def test_lex_bulk_upgrades_to_real_lexicon(self):
        self._w("lex-quorumward.md",
                _bulk("lex-quorumward.md", "quorumward means toward a signed quorum gate"))
        act = self._plan()["lex-quorumward.md"]
        self.assertEqual((act["op"], act["to_type"], act["dst"]),
                         ("upgrade", "lexicon", "lex-quorumward.md"))    # in-place
        drain.apply(drain.classify(self.mem), self.mem)
        with open(os.path.join(self.mem, "lex-quorumward.md")) as f:
            raw = f.read()
        self.assertIn("type: lexicon", raw)
        self.assertIn("term: quorumward", raw)
        self.assertIn("definition: quorumward means toward a signed quorum gate", raw)
        from helm import store
        e = self.one_store(store.load_all(types=("lexicon",)), "quorumward")
        self.assertEqual(e["type"], "lexicon")

    def test_prior_bulk_upgrades_in_place(self):
        self._w("prior-widget-idle.md",
                _bulk("prior-widget-idle.md", "the widgetron must idle at forty hertz baseline"))
        act = self._plan()["prior-widget-idle.md"]
        self.assertEqual((act["op"], act["dst"]), ("upgrade", "prior-widget-idle.md"))
        drain.apply(drain.classify(self.mem), self.mem)
        with open(os.path.join(self.mem, "prior-widget-idle.md")) as f:
            self.assertIn("confidence: 0.90", f.read())

    def test_two_keyword_gate_keeps_terse_episodic(self):
        self._w("prem-go.md", _bulk("prem-go.md", "go fast"))   # <2 specific kw
        act = self._plan()["prem-go.md"]
        self.assertEqual(act["op"], "keep")
        self.assertIn("specific keyword", act["why"])
        # nothing upgraded on apply
        drain.apply(drain.classify(self.mem), self.mem)
        self.assertTrue(os.path.exists(os.path.join(self.mem, "prem-go.md")))

    def test_real_typed_entry_never_upgraded(self):
        from helm import store
        store.write_prior({"id": "already-real", "statement": "a real typed prior",
                           "confidence": 0.8, "keywords": "realkw"}, root_dir=self.mem)
        self.assertNotIn("prior-already-real.md", self._plan())  # governed, skipped

    def test_dry_run_mutates_nothing(self):
        self._w("prem-scrub-docs.md",
                _bulk("prem-scrub-docs.md", "scrub internal planning documents before push"))
        before = sorted(os.listdir(self.mem))
        drain.classify(self.mem)
        self.assertEqual(before, sorted(os.listdir(self.mem)))

    def test_upgrade_netted_and_receipted(self):
        self._w("prem-scrub-docs.md",
                _bulk("prem-scrub-docs.md", "scrub internal planning documents before push"))
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        net = receipt["net"]
        self.assertTrue(os.path.isfile(os.path.join(net, "prem-scrub-docs.md")))
        self.assertTrue(os.path.isfile(os.path.join(net, "RECEIPT.json")))


def _typed_prior(name, statement):
    """A REAL typed prior (id + statement parse) — the curated entry the
    conflict guards must never clobber."""
    pid = name[len("prior-"):-3]
    return ("---\nname: %s\ndescription: \"prior: %s\"\nmetadata:\n"
            "  node_type: memory\n  type: prior\n  id: %s\n  statement: %s\n"
            "  confidence: 0.8\n  status: live\n  keywords: curated\n"
            "  source: human\n---\n\ncurated body\n"
            % (name[:-3], pid, pid, statement))


class RetypeEdgeCaseTest(HermeticMemBase):
    """The conflict/collision guards around retype + upgrade: an existing
    curated entry or a sibling action's destination downgrades to a surfaced
    'conflict' — never a silent overwrite — and even a hand-crafted plan that
    DOES overwrite must leave the old bytes recoverable in the net."""

    def test_existing_dst_conflicts_and_apply_skips_it(self):
        self._w("feedback-x.md", _mem_entry(
            "feedback-x.md", "feedback", "the new opinion about x"))
        self._w("prior-x.md", _typed_prior("prior-x.md", "the curated truth"))
        act = self._plan()["feedback-x.md"]
        self.assertEqual((act["op"], act["dst"]), ("conflict", "prior-x.md"))
        self.assertIn("already exists", act["why"])
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertNotIn("conflict", {a["op"] for a in receipt.get("actions", [])})
        with open(os.path.join(self.mem, "prior-x.md")) as fh:
            self.assertIn("the curated truth", fh.read())  # curated entry intact
        self.assertTrue(os.path.isfile(os.path.join(self.mem, "feedback-x.md")))

    def test_two_intake_files_same_dst_second_conflicts(self):
        self._w("feedback-y.md", _mem_entry(
            "feedback-y.md", "feedback", "first claimant of the y slug"))
        self._w("feedback_y.md", _mem_entry(
            "feedback_y.md", "feedback", "second claimant of the y slug"))
        plan = self._plan()
        self.assertEqual(plan["feedback-y.md"]["op"], "retype")
        act = plan["feedback_y.md"]
        self.assertEqual((act["op"], act["dst"]), ("conflict", "prior-y.md"))
        self.assertIn("also feedback-y.md", act["why"])

    def test_upgrade_rename_conflicts_with_existing_prior(self):
        # slug differs from the literal twin (Old-Way -> old-way), so the
        # sweep-dup branch misses and the upgrade collision guard must catch
        self._w("prem-Old-Way.md", _bulk(
            "prem-Old-Way.md", "deploys always batch at the slice boundary"))
        self._w("prior-old-way.md", _typed_prior(
            "prior-old-way.md", "the curated boundary rule"))
        act = self._plan()["prem-Old-Way.md"]
        self.assertEqual((act["op"], act["dst"]), ("conflict", "prior-old-way.md"))
        self.assertIn("already exists", act["why"])

    def test_upgrade_and_retype_collision_on_same_dst(self):
        self._w("feedback-z.md", _mem_entry(
            "feedback-z.md", "feedback", "the retype claimant of slug z"))
        self._w("prem-z.md", _bulk(
            "prem-z.md", "the bulk-upgrade claimant of exactly slug z"))
        plan = self._plan()
        self.assertEqual(plan["feedback-z.md"]["op"], "retype")
        act = plan["prem-z.md"]
        self.assertEqual((act["op"], act["dst"]), ("conflict", "prior-z.md"))
        self.assertIn("also feedback-z.md", act["why"])

    def test_handcrafted_overwrite_netted_under_overwritten_subdir(self):
        # classify blocks the common case; a hand-crafted plan (or a race)
        # that overwrites must net the old dst — under overwritten/, so a src
        # literally named overwritten-<dst> can never collide with the label
        self._w("overwritten-prior-b.md", _mem_entry(
            "overwritten-prior-b.md", "feedback", "awkwardly named source"))
        self._w("prior-b.md", _typed_prior("prior-b.md", "the about-to-be-replaced"))
        plan = [{"op": "retype", "src": "overwritten-prior-b.md",
                 "dst": "prior-b.md", "id": "b", "to_type": "prior",
                 "statement": "awkwardly named source", "origin": ""}]
        receipt = drain.apply(plan, self.mem)
        net = receipt["net"]
        self.assertTrue(os.path.isfile(os.path.join(net, "overwritten-prior-b.md")))
        with open(os.path.join(net, "overwritten", "prior-b.md")) as fh:
            self.assertIn("the about-to-be-replaced", fh.read())  # recoverable
        with open(os.path.join(self.mem, "prior-b.md")) as fh:
            self.assertIn("awkwardly named source", fh.read())    # new dst live

    def test_retype_text_without_frontmatter_preserves_raw_as_body(self):
        raw = "no frontmatter here\njust two lines of prose\n"
        p = os.path.join(self.mem, "feedback-bare.md")
        pk.atomic_write(p, raw)
        act = {"op": "retype", "src": "feedback-bare.md", "dst": "prior-bare.md",
               "id": "bare", "statement": "a bare note", "origin": "",
               "to_type": "prior"}
        text = drain._retype_text(p, act, pk.now_ts())
        self.assertIn(raw, text)               # the whole raw survives as body
        self.assertIn("type: prior", text)

    def test_empty_description_typed_prefix_kept_with_reason(self):
        self._w("prem-mute.md", _bulk("prem-mute.md", ""))
        act = self._plan()["prem-mute.md"]
        self.assertEqual(act["op"], "keep")
        self.assertIn("no description to upgrade", act["why"])


class ExpireCandidatesTest(unittest.TestCase):
    """drain --expire-candidates: the operator-visible candidate DECAY leg —
    unconfirmed candidates older than N days archived + removed on --apply,
    dry-run default, age-gated (no-timestamp candidates never expire)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-expcand-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "adopted")
        os.makedirs(self.mem)
        os.environ["HELM_ADOPTED_DIR"] = self.mem

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cand(self, term, updated_ts):
        from helm import store, home
        store.write_lexicon({"term": term, "definition": "d", "status": "candidate",
                             "source": "inferred", "updated_ts": updated_ts},
                            root_dir=os.path.join(home.global_dir(), "lexicon"))

    def test_dry_run_then_apply_age_gated(self):
        from helm import store
        self._cand("stale-cand", "2026-01-01T00:00:00Z")   # ancient -> expires
        self._cand("fresh-cand", pk.now_ts())              # today -> kept
        self._cand("undated-cand", "")                     # no ts -> never expires
        # a live lexicon is never a candidate and never touched
        store.write_lexicon({"term": "live-term", "definition": "d"})
        r = drain.expire_candidates(days=14, apply=False)
        self.assertEqual(r["found"], 1)
        self.assertEqual(r["ids"], ["stale-cand"])
        self.assertEqual(r["expired"], 0)   # dry-run mutates nothing
        self.assertEqual({e["id"] for e in store.candidates()},
                         {"stale-cand", "fresh-cand", "undated-cand"})
        r = drain.expire_candidates(days=14, apply=True)
        self.assertEqual(r["expired"], 1)
        self.assertEqual({e["id"] for e in store.candidates()},
                         {"fresh-cand", "undated-cand"})
        # the net + receipt exist and the expired file is recoverable
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "lex-stale-cand.md")))
        self.assertTrue(os.path.isfile(os.path.join(r["net"], "RECEIPT.json")))
        # the live term survived
        self.assertEqual([e["id"] for e in store.load_all()], ["live-term"])

    def test_cmd_dry_run_default(self):
        self._cand("stale-cand", "2026-01-01T00:00:00Z")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.cmd_drain(["--expire-candidates"])
        self.assertEqual(rc, 0)
        self.assertIn("1 unconfirmed candidate older than 14d", out.getvalue())
        self.assertIn("DRY-RUN", out.getvalue())
        from helm import store
        self.assertEqual(len(store.candidates()), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drain.cmd_drain(["--expire-candidates", "--days", "5", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("PRUNED 1 candidate", out.getvalue())
        self.assertEqual(len(store.candidates()), 0)


if __name__ == "__main__":
    unittest.main()
