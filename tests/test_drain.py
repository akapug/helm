import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import drain, eventledger, home, pk, projscope, registry  # noqa: E402


def _mem_entry(name, etype, description, body="the full body\nwith detail\n"):
    return ("---\nname: %s\ndescription: \"%s\"\nmetadata:\n  node_type: memory\n"
            "  type: %s\n  originSessionId: sess-1\n---\n\n%s"
            % (name[:-3], description, etype, body))


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.mem = tempfile.mkdtemp(prefix="helm-test-mem-")
        self.adopted_prior = os.environ.get("HELM_ADOPTED_DIR")
        os.environ["HELM_ADOPTED_DIR"] = self.mem
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
        if self.adopted_prior is None:
            os.environ.pop("HELM_ADOPTED_DIR", None)
        else:
            os.environ["HELM_ADOPTED_DIR"] = self.adopted_prior
        shutil.rmtree(self.mem, ignore_errors=True)

    def _plan(self):
        return {a["src"]: a for a in drain.classify(self.mem)}

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

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

    def test_repoint_index_writes_only_under_the_projection_lock(self):
        idx = os.path.join(self.mem, "MEMORY.md")
        real_lock = eventledger.locked
        real_write = pk.atomic_write
        held = []
        locks = []

        @contextlib.contextmanager
        def tracked(path):
            locks.append(path)
            with real_lock(path) as acquired:
                held.append(acquired)
                try:
                    yield acquired
                finally:
                    held.pop()

        def write(path, content):
            if path == idx:
                self.assertEqual(held, [True])
            return real_write(path, content)

        with mock.patch.object(drain.eventledger, "locked", side_effect=tracked), \
             mock.patch.object(pk, "atomic_write", side_effect=write):
            n = drain._repoint_index(
                self.mem, {"feedback-short-dms.md": "prior-short-dms.md"})
        self.assertEqual((n, locks), (1, [idx]))
        with open(idx) as f:
            self.assertIn("(prior-short-dms.md)", f.read())

    def test_apply_expiry_before_projection_lock_mutates_nothing(self):  # noqa: VACUOUS_ASSERTION — the same action succeeds below and proves every mutation observable
        src = os.path.join(self.mem, "feedback-short-dms.md")
        dst = os.path.join(self.mem, "prior-short-dms.md")
        idx = os.path.join(self.mem, "MEMORY.md")
        before_src = self._read(src)
        before_idx = self._read(idx)
        action = [self._plan()["feedback-short-dms.md"]]

        @contextlib.contextmanager
        def expired(path):
            self.assertEqual(path, idx)
            raise projscope.Expired("projection lock expired")
            yield  # pragma: no cover — makes this a contextmanager generator

        with mock.patch.object(drain.eventledger, "locked", side_effect=expired):
            with self.assertRaises(projscope.Expired):
                drain.apply(action, self.mem)
        self.assertEqual(self._read(src), before_src)
        self.assertEqual(self._read(idx), before_idx)
        self.assertFalse(os.path.exists(dst))
        receipts = [name for root, _dirs, files in os.walk(self.mem)
                    for name in files if name == "RECEIPT.json"]
        self.assertEqual(receipts, [])

        receipt = drain.apply(action, self.mem)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.isfile(dst))
        self.assertTrue(os.path.isfile(os.path.join(receipt["net"], "RECEIPT.json")))
        self.assertIn("(prior-short-dms.md)",
                      self._read(idx))

    def test_apply_refused_projection_lock_mutates_nothing(self):  # noqa: VACUOUS_ASSERTION — the same action succeeds below and proves every mutation observable
        src = os.path.join(self.mem, "feedback-short-dms.md")
        dst = os.path.join(self.mem, "prior-short-dms.md")
        idx = os.path.join(self.mem, "MEMORY.md")
        before_src = self._read(src)
        before_idx = self._read(idx)
        action = [self._plan()["feedback-short-dms.md"]]

        @contextlib.contextmanager
        def refused(path):
            self.assertEqual(path, idx)
            yield False

        with mock.patch.object(drain.eventledger, "locked", side_effect=refused):
            with self.assertRaisesRegex(RuntimeError, "nothing mutated"):
                drain.apply(action, self.mem)
        self.assertEqual(self._read(src), before_src)
        self.assertEqual(self._read(idx), before_idx)
        self.assertFalse(os.path.exists(dst))
        receipts = [name for root, _dirs, files in os.walk(self.mem)
                    for name in files if name == "RECEIPT.json"]
        self.assertEqual(receipts, [])

        receipt = drain.apply(action, self.mem)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.isfile(dst))
        self.assertTrue(os.path.isfile(os.path.join(receipt["net"], "RECEIPT.json")))
        self.assertIn("(prior-short-dms.md)",
                      self._read(idx))

    def test_apply_holds_projection_lock_through_receipt(self):
        action = [self._plan()["feedback-short-dms.md"]]
        idx = os.path.join(self.mem, "MEMORY.md")
        held = []
        mutations = []
        write = pk.atomic_write
        write_json = pk.write_json
        remove = os.remove

        @contextlib.contextmanager
        def tracked(path):
            self.assertEqual(path, idx)
            held.append(True)
            try:
                yield True
            finally:
                held.pop()

        def tracked_write(path, text):
            if path.startswith(self.mem):
                self.assertEqual(held, [True])
                mutations.append(path)
            return write(path, text)

        def tracked_json(path, row):
            if path.endswith("RECEIPT.json"):
                self.assertEqual(held, [True])
                mutations.append(path)
            return write_json(path, row)

        def tracked_remove(path):
            self.assertEqual(held, [True])
            mutations.append(path)
            return remove(path)

        with mock.patch.object(drain.eventledger, "locked", side_effect=tracked), \
                mock.patch.object(drain.pk, "atomic_write",
                                  side_effect=tracked_write), \
                mock.patch.object(drain.pk, "write_json",
                                  side_effect=tracked_json), \
                mock.patch.object(drain.os, "remove",
                                  side_effect=tracked_remove):
            receipt = drain.apply(action, self.mem)
        self.assertIn(os.path.join(self.mem, "prior-short-dms.md"), mutations)
        self.assertIn(os.path.join(self.mem, "feedback-short-dms.md"), mutations)
        self.assertIn(idx, mutations)
        self.assertIn(os.path.join(receipt["net"], "RECEIPT.json"), mutations)

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
    """Short and old handles route through the shipped alias map or an authored
    per-project alias, without duplicating private canonical labels in fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-alias-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME"),
                          "HELM_PROJECT_ALIASES": os.environ.get("HELM_PROJECT_ALIASES")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem)
        # The built-in alias map is config-driven and EMPTY by default: the
        # shipped tree carries no site-specific names, only the mechanism.
        os.environ.pop("HELM_PROJECT_ALIASES", None)
        self.assertEqual(drain._builtin_aliases(), {})
        os.environ["HELM_PROJECT_ALIASES"] = "oldhandle=alpha-project"
        self.assertEqual(drain._builtin_aliases(), {"oldhandle": "alpha-project"})
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha-project": {"name": "alpha-project", "path": "/x/a",
                              "kind": "git", "sessions": {}},
            "beta-project": {"name": "beta-project", "path": "/x/b",
                             "kind": "git", "sessions": {}, "aliases": ["bx"]}}})

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
                        _mem_entry("proj-note.md", "project", "the oldhandle roadmap and vision"))
        a = self._classify()["proj-note.md"]
        self.assertEqual((a["op"], a["project"]),
                         ("route-project", "alpha-project"))

    def test_authored_short_alias_routes_by_filename(self):
        # 'bx' is 2 chars — filename prefix only (a 2-char word wallpapers the corpus)
        pk.atomic_write(os.path.join(self.mem, "bx-standup.md"),
                        _mem_entry("bx-standup.md", "project", "the standup notes"))
        a = self._classify()["bx-standup.md"]
        self.assertEqual((a["op"], a["project"]), ("route-project", "beta-project"))

    def test_short_alias_never_wallpapers_by_description(self):
        # 'bx' appearing as a description word must NOT route (len < 6 guard)
        pk.atomic_write(os.path.join(self.mem, "random-thing.md"),
                        _mem_entry("random-thing.md", "project", "the bx was loud"))
        self.assertEqual(self._classify()["random-thing.md"]["op"], "keep")


class AliasPrecedenceTest(unittest.TestCase):
    """Review #1: alias matching is LONGEST-HANDLE-FIRST — never env insertion
    order. --apply copies to the matched project then DELETES the intake, so an
    order-dependent match is destructive, not cosmetic. Equal lengths tie-break
    alphabetically; on a duplicate handle the authored definition beats the env."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-aliasprec-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME"),
                          "HELM_PROJECT_ALIASES": os.environ.get("HELM_PROJECT_ALIASES")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mem = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem)
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha-project": {"name": "alpha-project", "path": "/x/a",
                              "kind": "git", "sessions": {}},
            "beta-project": {"name": "beta-project", "path": "/x/b",
                             "kind": "git", "sessions": {}}}})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _routes(self, filename, env, description="no matching words here"):
        """The project `filename` routes to under `env` — hermetic per call."""
        os.environ["HELM_PROJECT_ALIASES"] = env
        p = os.path.join(self.mem, filename)
        pk.atomic_write(p, _mem_entry(filename, "project", description))
        try:
            a = {x["src"]: x for x in drain.classify(self.mem)}[filename]
            return a.get("project") if a["op"] == "route-project" else None
        finally:
            os.remove(p)

    def test_overlapping_handles_route_most_specific_both_env_orders(self):
        # old-api-roadmap.md must reach `old-api`, never `old`, whichever side
        # of the comma the operator listed the more specific handle
        for env in ("old=alpha-project,old-api=beta-project",
                    "old-api=beta-project,old=alpha-project"):
            self.assertEqual(self._routes("old-api-roadmap.md", env),
                             "beta-project", env)
            # the short handle still owns files that are ONLY its own
            self.assertEqual(self._routes("old-notes.md", env),
                             "alpha-project", env)

    def test_equal_length_description_handles_tiebreak_alphabetical(self):
        # two >=6-char handles of EQUAL length both in the description: the
        # alphabetically first handle decides, not the env listing order
        desc = "the mmmmmm and aaaaaa are both named"
        for env in ("mmmmmm=beta-project,aaaaaa=alpha-project",
                    "aaaaaa=alpha-project,mmmmmm=beta-project"):
            self.assertEqual(self._routes("plain-note.md", env, desc),
                             "alpha-project", env)

    def test_duplicate_handle_authored_beats_env_loudly(self):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha-project": {"name": "alpha-project", "path": "/x/a", "kind": "git"},
            "beta-project": {"name": "beta-project", "path": "/x/b", "kind": "git",
                             "aliases": ["shared"]}}})
        os.environ["HELM_PROJECT_ALIASES"] = "shared=alpha-project"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m, refused = drain._alias_map(registry.load())
        self.assertIsNone(refused)
        self.assertEqual(m["shared"], "beta-project")   # the record is closer truth
        # the warning names the ACTUAL source of each side: the authored
        # winner by project, the loser as the ENV var (never another project)
        self.assertIn("authored (beta-project) overrides HELM_PROJECT_ALIASES "
                      "(alpha-project)", err.getvalue())


class AliasConfigValidationTest(unittest.TestCase):
    """Review #2: HELM_PROJECT_ALIASES is operator-authored ROUTING config
    feeding a copy-then-delete — malformed entries, duplicates, unknown or
    case-mismatched targets, and wrong-shape authored fields all surface on
    stderr, never degrade into a silent 'no registry match'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-aliascfg-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME"),
                          "HELM_PROJECT_ALIASES": os.environ.get("HELM_PROJECT_ALIASES")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "alpha-project": {"name": "alpha-project", "path": "/x/a",
                              "kind": "git", "sessions": {}}}})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, env):
        os.environ["HELM_PROJECT_ALIASES"] = env
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return drain._builtin_aliases(), err.getvalue()

    def test_malformed_entries_rejected_loudly(self):
        # no '=', empty handle, empty target, extra '=' (partition used to keep
        # 'alpha-project=typo' as the target) — each rejected with its own line
        m, err = self._parse("broken,=alpha-project,name=,old=alpha-project=typo")
        self.assertEqual(m, {})                        # nothing salvaged by guesswork
        self.assertEqual(err.count("malformed entry"), 4)

    def test_blank_segments_are_absence_not_malformation(self):
        m, err = self._parse("old=alpha-project, ,")   # trailing comma / spaces
        self.assertEqual(m, {"old": "alpha-project"})
        self.assertEqual(err, "")

    def test_whitespace_stripped_handle_lowercased(self):
        m, err = self._parse(" OLD = alpha-project ")
        self.assertEqual(m, {"old": "alpha-project"})
        self.assertEqual(err, "")

    def test_duplicate_env_handle_first_wins_loudly(self):
        m, err = self._parse("old=alpha-project,old=beta-project")
        self.assertEqual(m, {"old": "alpha-project"})  # FIRST definition kept
        self.assertIn("duplicate handle", err)

    def test_env_target_must_name_registry_project_exactly(self):
        os.environ["HELM_PROJECT_ALIASES"] = "gone=ghost-project,cased=Alpha-Project"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m, _ = drain._alias_map(registry.load())
        self.assertEqual(m, {})                        # both dropped, both loud
        self.assertIn("names no registry project", err.getvalue())
        self.assertIn("case mismatch", err.getvalue())
        self.assertIn("'alpha-project'", err.getvalue())  # the registry spelling, named

    def test_authored_string_aliases_never_iterate_char_by_char(self):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "beta-project": {"name": "beta-project", "path": "/x/b", "kind": "git",
                             "aliases": "bx"}}})       # wrong shape: bare string
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m, _ = drain._alias_map(registry.load())
        self.assertEqual(m, {"bx": "beta-project"})    # ONE alias, not {'b','x'}
        self.assertIn("string, not a list", err.getvalue())

    def test_authored_nonlist_shape_ignored_loudly(self):
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "beta-project": {"name": "beta-project", "path": "/x/b", "kind": "git",
                             "aliases": 7}}})          # not a string, not a list
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m, _ = drain._alias_map(registry.load())
        self.assertEqual(m, {})
        self.assertIn("not a list", err.getvalue())


class AliasDeterminismTest(unittest.TestCase):
    """The converged determinism model (CD/codex-3/OI meld): the alias map is
    SOURCE-TRACKED per handle. Authored-authored same-handle conflicts REFUSE
    the handle under EVERY registry order (a winner picked by iteration order
    would copy-then-DELETE the intake file — unrecoverable; a refusal only
    leaves it in intake). An UNREADABLE authored source refuses ALIAS routing
    outright — no env-only degrade, because a partial map routes deletions
    with half the authority while every surface reports success. And
    longest-handle-first ranks the ONE MERGED map, so a prefix pair straddling
    sources still routes the specific handle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-aliasdet-")
        self.env_prior = {"HELM_HOME": os.environ.get("HELM_HOME"),
                          "HELM_PROJECT_ALIASES": os.environ.get("HELM_PROJECT_ALIASES")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ.pop("HELM_PROJECT_ALIASES", None)
        self.mem = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self, order, authored=()):
        """BOTH registry layers written in `order` (dict order IS file order —
        the axis the determinism tests vary): registry.json projections plus
        registry-authored.json entries (path-stamped so load() merges them).
        authored = ((project, [aliases...]), ...)."""
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            n: {"name": n, "path": "/x/" + n, "kind": "git", "sessions": {}}
            for n in order}})
        pk.write_json(home.authored_path(), {"version": 1, "projects": {
            n: {"path": "/x/" + n, "aliases": list(al)} for n, al in authored}})

    def _classify(self, filename, description="no matching words here"):
        """(action, stderr) for one intake file — hermetic per call."""
        p = os.path.join(self.mem, filename)
        pk.atomic_write(p, _mem_entry(filename, "project", description))
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                a = {x["src"]: x for x in drain.classify(self.mem)}[filename]
            return a, err.getvalue()
        finally:
            os.remove(p)

    def test_authored_authored_same_handle_refused_both_registry_orders(self):
        # two RECORDS both author `shared`: the handle routes NOTHING and both
        # projects are named — IDENTICALLY under both registry file orders
        # (the determinism proof: no picked winner exists to flip)
        decl = {"beta-project": ["shared", "solo"], "gamma-project": ["shared"]}
        for order in (("beta-project", "gamma-project"),
                      ("gamma-project", "beta-project")):
            self._registry(order, tuple((n, decl[n]) for n in order))
            act, err = self._classify("shared-notes.md")
            self.assertEqual(act["op"], "keep", order)          # refused, not routed
            self.assertIn("AMBIGUOUS", err)
            self.assertIn("beta-project AND gamma-project", err)  # both named, sorted
            self.assertIn("REFUSED", err)
            # the poison is per-HANDLE: beta's uncontested alias still routes
            act, _ = self._classify("solo-notes.md")
            self.assertEqual((act["op"], act.get("project")),
                             ("route-project", "beta-project"), order)

    def test_ambiguous_handle_suppresses_env_definition_too(self):
        # when the two CLOSER authorities disagree, authority is not
        # determinable — the env definition must not resurrect the handle
        self._registry(("alpha-project", "beta-project", "gamma-project"),
                       (("beta-project", ["shared"]), ("gamma-project", ["shared"])))
        os.environ["HELM_PROJECT_ALIASES"] = "shared=alpha-project"
        act, err = self._classify("shared-notes.md")
        self.assertEqual(act["op"], "keep")
        self.assertIn("AMBIGUOUS", err)
        self.assertIn("suppressed", err)                # the env side, named as such

    def test_authored_conflict_receipt_never_blames_env(self):
        # the FALSE-ATTRIBUTION fix: with no env at all, the old message said
        # 'overrides HELM_PROJECT_ALIASES (...)' about a mapping that came
        # from ANOTHER AUTHORED project — the receipt must name both projects
        # and never mint a silent winner
        self._registry(("beta-project", "gamma-project"),
                       (("beta-project", ["shared"]), ("gamma-project", ["shared"])))
        _, err = self._classify("shared-notes.md")
        self.assertIn("beta-project", err)
        self.assertIn("gamma-project", err)
        self.assertNotIn("HELM_PROJECT_ALIASES", err)
        self.assertNotIn("overrides", err)

    def test_same_target_duplicate_is_silent_and_routes(self):
        # env and a record AGREEING (and a record repeating itself) is not a
        # conflict — no warning, and the handle routes normally
        self._registry(("beta-project",),
                       (("beta-project", ["shared", "shared"]),))
        os.environ["HELM_PROJECT_ALIASES"] = "shared=beta-project"
        act, err = self._classify("shared-notes.md")
        self.assertEqual((act["op"], act["project"]),
                         ("route-project", "beta-project"))
        self.assertEqual(err, "")

    def test_unreadable_authored_source_refuses_alias_routing(self):
        # a half-written registry-authored.json: alias routing REFUSES for the
        # whole drain (env-alias entries STAY in intake and are reported; no
        # env-only fallback), canonical routing is untouched, and stderr names
        # the failed source and why
        self._registry(("alpha-project", "beta-project"))
        os.environ["HELM_PROJECT_ALIASES"] = "oldhandle=alpha-project"
        pk.atomic_write(home.authored_path(), '{"version": 1, "projects": {')
        pk.atomic_write(os.path.join(self.mem, "oldhandle-note.md"),
                        _mem_entry("oldhandle-note.md", "project", "the notes"))
        pk.atomic_write(os.path.join(self.mem, "beta-project-plan.md"),
                        _mem_entry("beta-project-plan.md", "project", "the plan"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            plan = {a["src"]: a for a in drain.classify(self.mem)}
        act = plan["oldhandle-note.md"]
        self.assertEqual(act["op"], "keep")                    # NOT routed by env alone
        self.assertIn("alias routing refused", act["why"])     # held AND reported
        self.assertEqual(plan["beta-project-plan.md"]["op"], "route-project")
        self.assertIn("ALIAS ROUTING REFUSED", err.getvalue())
        self.assertIn(home.authored_path(), err.getvalue())    # the failed source
        self.assertIn("unparseable JSON", err.getvalue())      # ...and why
        # --apply under the refusal: the alias-routable entry survives in
        # intake (recoverable), the canonical one still routes
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            drain.apply(drain.classify(self.mem), self.mem)
        self.assertTrue(os.path.isfile(os.path.join(self.mem, "oldhandle-note.md")))
        self.assertFalse(os.path.exists(os.path.join(self.mem, "beta-project-plan.md")))

    @unittest.skipIf(os.geteuid() == 0, "chmod-based denial is a no-op as root")
    def test_permission_unreadable_authored_source_refuses(self):
        # same refusal for the EXISTS-but-unreadable state — and the drain
        # survives to say so (registry's corruption net used to crash on the
        # backup copy it cannot read)
        self._registry(("alpha-project",))
        os.environ["HELM_PROJECT_ALIASES"] = "oldhandle=alpha-project"
        os.chmod(home.authored_path(), 0)
        try:
            act, err = self._classify("oldhandle-note.md")
        finally:
            os.chmod(home.authored_path(), 0o600)
        self.assertEqual(act["op"], "keep")
        self.assertIn("alias routing refused", act["why"])
        self.assertIn("ALIAS ROUTING REFUSED", err)
        self.assertIn("unreadable", err)

    def test_absent_authored_file_is_empty_layer_not_refusal(self):
        # no file at all = a legitimately empty authored layer: env aliases
        # route normally and nothing is announced
        self._registry(("alpha-project",))
        os.remove(home.authored_path())
        os.environ["HELM_PROJECT_ALIASES"] = "oldhandle=alpha-project"
        act, err = self._classify("oldhandle-note.md")
        self.assertEqual((act["op"], act["project"]),
                         ("route-project", "alpha-project"))
        self.assertEqual(err, "")

    def test_cross_source_prefix_pair_ranks_on_the_merged_map(self):
        # `old` / `old-api` STRADDLE sources, both directions: the specific
        # handle wins regardless of WHICH source holds it — any per-source
        # sort-before-merge would consult one source's ranking first and send
        # old-api-roadmap.md through the short handle
        cases = (("old=alpha-project", (("beta-project", ["old-api"]),)),
                 ("old-api=beta-project", (("alpha-project", ["old"]),)))
        for env, authored in cases:
            self._registry(("alpha-project", "beta-project"), authored)
            os.environ["HELM_PROJECT_ALIASES"] = env
            act, err = self._classify("old-api-roadmap.md")
            self.assertEqual((act["op"], act.get("project")),
                             ("route-project", "beta-project"), env)
            self.assertEqual(err, "", env)                     # no conflict here
            act, _ = self._classify("old-notes.md")
            self.assertEqual((act["op"], act.get("project")),
                             ("route-project", "alpha-project"), env)


class DrainProjectTest(unittest.TestCase):
    """drain --project P drains the project's OWN claude memory dir with the
    identical gauntlet. Hermetic: the claude memdir resolver is patched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-drainproj-")
        self.env_prior = {k: os.environ.get(k)
                          for k in ("HELM_HOME", "HELM_ADOPTED_DIR")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.projmem = os.path.join(self.tmp, "projmem")
        os.makedirs(self.projmem)
        os.environ["HELM_ADOPTED_DIR"] = self.projmem
        pk.atomic_write(os.path.join(self.projmem, "feedback-x.md"),
                        _mem_entry("feedback-x.md", "feedback",
                                   "signed checkpoints protect widgetron recovery"))
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "demo-project": {"name": "demo-project", "path": "/dev/demo-project", "kind": "git",
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
            side_effect=lambda p: self.projmem if p == "/dev/demo-project" else "/nonexistent-xyz")

    def test_drain_project_scans_project_memdir(self):
        with self._patch():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = drain.cmd_drain(["--project", "demo-project"])
        self.assertEqual(rc, 0)
        self.assertIn("project demo-project", out.getvalue())
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

    def test_refusals_are_surfaced_not_swallowed(self):
        """`found 0` and `found 43` looked equally healthy while the old gate
        ran. The receipt names the layer, and the CLI prints it."""
        self._jsonl("77777777-7777-7777-7777-777777777777.jsonl", [
            self._user("from now on the widgetron idles at 40hz between runs"),
            self._user("the export is idle again as always bc it is so fast haha"),
            self._user("<task-notification> always ship it </task-notification>"),
        ])
        r = drain.promote(since_days=30, roots=[self.tx])
        self.assertEqual(r["found"], 1)                    # the real rule landed
        self.assertEqual(r["refused"], {"banter": 1, "markup": 1})
        orig = drain.promote
        with mock.patch.object(drain, "promote",
                               side_effect=lambda **kw: orig(**{**kw, "roots": [self.tx]})):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                drain.cmd_promote([])
        self.assertIn("refused:", out.getvalue())
        self.assertIn("banter=1", out.getvalue())


_CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "promote-corpus.json")


def _corpus():
    with open(_CORPUS_PATH, encoding="utf-8") as f:
        return json.load(f)


class PromoteGauntletCorpusTest(unittest.TestCase):
    """The acceptance that decides the gauntlet: one stand-in for each of the
    43 messages the old substring gate minted into the typed store, in the same
    order and under the label the cleanup that retired them gave. 40 junk, 3
    genuine. The originals were verbatim owner messages naming private projects
    and seats, so each stand-in is SYNTHETIC text built to take its original's
    exact path through every gauntlet layer: the same length class against both
    length bounds and the 170 and 400 cuts, the same structural refusals, the
    same markers, and the same verdict for every clause that carries one.

    A gauntlet that refuses everything scores perfectly on the junk and is
    worthless, so the 3 genuine entries are asserted INDIVIDUALLY as positive
    controls — they are the point. The junk side is asserted as a floor, not an
    exact count, so a later tightening can improve it without editing a test;
    the control side is exact."""

    CONTROLS = ("always-integrate-upstream-parser",
                "quick-misleading-schema-fixes",
                "checklist-installer-whatever-boringly")

    # the two labelled-junk messages this gauntlet deliberately accepts, each
    # with the reason it is a defensible disagreement rather than a miss
    KNOWN_ACCEPTS = {
        # "...never clear your scratch state mid-task, it throws away everything
        # you have loaded" — a real directive. The cleanup retired its original
        # because the 170-char truncation cut it inside "[CORRECTION" (the
        # stand-in is cut at the same place), so it could only ever inject the
        # owner's disappointment. _STATEMENT_CAP now carries the directive,
        # which is exactly the defect this lane fixed; refusing it would be
        # refusing a rule for a reason that no longer exists.
        "clear-suggestion-dashboard-workspace",
        # "the test and live databases should always match" — textbook `should
        # always`. Refusing it means refusing `should always`, which positive
        # control #1 ("we should always track the newest tag") is built on.
        "databases-importer-remove-legacy",
    }

    def test_positive_controls_are_kept(self):
        by_slug = {r["slug"]: r for r in _corpus()}
        self.assertEqual(sorted(r["slug"] for r in _corpus()
                                if r["label"] == "keep"), sorted(self.CONTROLS))
        # named individually and unconditionally — these three ARE the acceptance
        self.assertEqual(drain.promote_verdict(
            by_slug["always-integrate-upstream-parser"]["text"])[0], "always")
        self.assertEqual(drain.promote_verdict(
            by_slug["quick-misleading-schema-fixes"]["text"])[0], "never")
        self.assertEqual(drain.promote_verdict(
            by_slug["checklist-installer-whatever-boringly"]["text"])[0],
            "always")
        for slug in self.CONTROLS:
            self.assertEqual(by_slug[slug]["label"], "keep", slug)
            marker, why = drain.promote_verdict(by_slug[slug]["text"])
            self.assertIsNone(why, "positive control %s was REFUSED (%s)"
                              % (slug, why))
            self.assertTrue(marker)

    def test_junk_cohort_is_refused(self):
        junk = [r for r in _corpus() if r["label"] == "refuse"]
        self.assertEqual(len(junk), 40)
        accepted = [r["slug"] for r in junk
                    if drain.promote_verdict(r["text"])[0]]
        self.assertEqual(set(accepted) - self.KNOWN_ACCEPTS, set(),
                         "new false-accepts against the corpus")
        # MEASURED 38/40 at the time of writing; the floor guards the direction
        self.assertGreaterEqual(len(junk) - len(accepted), 38)

    def test_the_five_task_notifications_are_refused_as_markup(self):
        """FIVE of the 43 originals were verbatim <task-notification> XML, so
        five stand-ins are too. Markup is a hard refusal, so this holds even
        when a payload has no marker at all."""
        xml = [r for r in _corpus() if "<task-notification>" in r["text"]
               or "</task-id>" in r["text"]]
        self.assertEqual(len(xml), 5)
        for r in xml:
            self.assertEqual(drain.promote_verdict(r["text"])[1], "markup", r["slug"])

    def test_every_corpus_message_passed_the_OLD_substring_gate(self):
        """The corpus is only evidence if the old gate really did accept it.
        Reproduces the pre-fix rule: bare substring, no word boundary."""
        old = ("from now on", "remember this", "remember that", "make it a rule",
               "going forward", "the rule is", "always ", "never ")
        corpus = _corpus()
        self.assertEqual(len(corpus), 43)
        self.assertTrue(any(m in corpus[0]["text"].lower() for m in old))
        for r in corpus:
            low = r["text"].lower()
            self.assertTrue(any(m in low for m in old), r["slug"])


class PromoteGauntletUnitTest(unittest.TestCase):
    """Each gauntlet layer, one behaviour at a time."""

    def test_marker_is_word_matched_not_substring(self):
        """3 of the 43 were minted ONLY because "whenever" contains "never "."""
        self.assertIsNone(drain._promote_marker(
            "go on, and pause the watcher whenever it suits you, what is left to do"))
        self.assertEqual(drain._promote_marker(
            "the gate must never be skipped before a ship"), "never")

    def test_describing_a_marker_is_not_directing_with_one(self):
        report = "i never got the benchmark numbers you promised me last night ok"
        rule = "the release gate should never be skipped before a ship"
        self.assertEqual(drain.promote_verdict(report)[1], "marker-not-directive")
        self.assertIsNone(drain.promote_verdict(rule)[1])

    def test_past_tense_and_subordinate_clauses_are_not_directives(self):
        # the SAME words in directive position must still be kept — otherwise
        # this test would pass just as well against a gauntlet that refuses all
        self.assertEqual(drain.promote_verdict(
            "the status page should always be updated before a handoff")[0],
            "always")
        for text in ("that seat was always the larger model and you steered it anyway",
                     "keep the status page updated so the dashboard is always clear",
                     "find out why you never ran the browser checks at all"):
            self.assertEqual(drain.promote_verdict(text)[1],
                             "marker-not-directive", text)

    def test_a_directive_addressed_outward_is_not_an_i_report(self):
        """The owner's most natural directive phrasing puts the pronoun FIRST:
        "i want you to always X", "i need you to never Y", "we have to always
        Z". An I-report rule with a loose word gap, and a past-tense rule that
        did not except the "have to" modal, refused all three."""
        self.assertEqual(drain.promote_verdict(
            "i want you to always sign the widgetron before ship")[0], "always")
        for text in ("i want you to always sign the widgetron before ship",
                     "i need you to never touch the gamma store directly",
                     "we have to always sign the widgetron before we ship"):
            marker, why = drain.promote_verdict(text)
            self.assertIsNone(why, "%s refused as %s" % (text, why))
            self.assertIn(marker, ("always", "never"))
        # the genuine self-reports they must not drag back in
        for text in ("i never got the benchmark numbers you promised me last night",
                     "im just always anxious that the numbers keep improving"):
            self.assertEqual(drain.promote_verdict(text)[1],
                             "marker-not-directive", text)

    def test_structural_refusals(self):
        clean = "the release gate should never be skipped before a ship"
        self.assertEqual(drain.promote_verdict(clean)[0], "never")  # all layers
        cases = {
            "markup": "<task-notification> " + clean + " </task-notification>",
            "attachment": "the seat was always the larger model here [Image #12] look at it",
            "chat-log": "[7:42 AM]user (ops): we should always keep one printer on",
            "banter": "the export is idle again as always bc it's so fast haha ok",
            "fleet-traffic": "resume: never sit idle, report to the lead at pane:w9:p1",
            "fragment": "..." + clean,
            "too-short": "always!",
            "too-long": clean + " " + "x" * 700,
        }
        for want, text in cases.items():
            self.assertEqual(drain.promote_verdict(text)[1], want, want)

    def test_explicit_capture_phrasings_need_no_directive_shape(self):
        """"from now on"/"remember this" ARE the speech act; only the two bare
        English words have to earn it."""
        marker, why = drain.promote_verdict(
            "from now on the widgetron idles at 40hz between runs")
        self.assertIsNone(why)
        self.assertEqual(marker, "from now on")

    def test_statement_cap_equals_the_injection_line_cap(self):
        """The mint-time cap and the fire-time cap are the same number on
        purpose: truncating below what the lane would carry is loss for
        nothing. This test is the drift guard between the two modules."""
        from helm.inject._common import LINE_CAP
        self.assertEqual(drain._STATEMENT_CAP, LINE_CAP)
        # and the effect: a 300-char directive survives to the statement whole,
        # where the old 170-char description path cut it to a fragment
        long_rule = ("the release gate should never be skipped before a ship, "
                     + "and the reason matters " * 11)[:300]
        self.assertEqual(len(long_rule), 300)
        act = {"statement": long_rule, "id": "x", "to_type": "prior",
               "dst": "prior-x.md", "src": "feedback-x.md", "origin": ""}
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("---\nname: feedback-x\n---\n\nbody\n")
            src = f.name
        out = drain._retype_text(src, act, "2026-08-03T00:00:00Z")
        os.unlink(src)
        self.assertIn("  statement: " + long_rule, out)

    def test_injected_prompts_are_not_owner_speech(self):
        self.assertTrue(drain._injected_prompt({"isMeta": True}))
        self.assertTrue(drain._injected_prompt({"promptSource": "system"}))
        self.assertTrue(drain._injected_prompt(
            {"origin": {"kind": "task-notification"}}))
        self.assertFalse(drain._injected_prompt(
            {"promptSource": "typed", "origin": {"kind": "human"}}))
        self.assertFalse(drain._injected_prompt({"promptSource": "queued"}))
        self.assertFalse(drain._injected_prompt({}))   # old transcripts: fail-open


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
        self.assertIn("  keywords: ", raw)          # guarded derivation reached output
        from helm import store
        e = self.one_store(store.load_all(types=("lexicon",)), "quorumward")
        self.assertEqual(e["type"], "lexicon")
        self.assertTrue(e["keywords"])

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


class DrainMintGuardTest(HermeticMemBase):
    """#204: semantic drain mints cross the shared store guard transactionally."""

    def _prior(self, eid, keywords):
        from helm import store
        return store.write_prior({
            "id": eid, "statement": "seed " + eid, "confidence": 0.8,
            "keywords": keywords, "source": "human", "stated_ts": pk.now_ts(),
            "last_updated": pk.now_ts()}, root_dir=self.mem)

    def _feedback(self, name, statement):
        self._w(name, _mem_entry(name, "feedback", statement))

    def test_existing_duplicate_refuses_and_source_stays(self):
        self._prior("widgetron-recovery-law",
                    "widgetron,checkpoint,freezes,recovery,rollback")
        src = "feedback-widgetron-recovery-two.md"
        self._feedback(src, "widgetron checkpoint freezes during recovery rollback")
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertEqual(receipt["applied"], 0)
        self.assertEqual(len(receipt["refused"]), 1)
        self.assertIn("widgetron-recovery-law", receipt["refused"][0]["why"])
        self.assertTrue(os.path.isfile(os.path.join(self.mem, src)))
        self.assertFalse(os.path.exists(
            os.path.join(self.mem, "prior-widgetron-recovery-two.md")))

    def test_same_batch_duplicate_sees_earlier_staged_entry_one_corpus_read(self):
        from helm import store
        first = "feedback-alpha-recovery.md"
        second = "feedback-beta-recovery.md"
        statement = "widgetron checkpoint freezes during signed recovery"
        self._feedback(first, statement)
        self._feedback(second, statement)
        with mock.patch.object(store, "load_all", wraps=store.load_all) as load:
            receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(receipt["applied"], 1)
        self.assertEqual([a["src"] for a in receipt["refused"]], [second])
        self.assertFalse(os.path.exists(os.path.join(self.mem, first)))
        self.assertTrue(os.path.isfile(os.path.join(self.mem, second)))
        self.assertTrue(os.path.isfile(
            os.path.join(self.mem, "prior-alpha-recovery.md")))

    def test_exact_predecessor_exclusion_avoids_self_df_inflation(self):
        self._prior("infra-one", "infrastructure,seedone")
        self._prior("infra-two", "infrastructure,seedtwo")
        path = self._prior("self-map", "infrastructure")
        plan = [{"op": "upgrade", "src": os.path.basename(path),
                 "dst": os.path.basename(path), "id": "self-map",
                 "to_type": "prior", "statement": "self mapped infrastructure",
                 "keywords": "infrastructure", "origin": ""}]
        receipt = drain.apply(plan, self.mem)
        self.assertEqual((receipt["applied"], receipt["refused"]), (1, []))
        with open(path, encoding="utf-8") as f:
            self.assertIn("  keywords: infrastructure", f.read())

    def test_predecessor_exclusion_retains_all_other_df_siblings(self):  # noqa: VACUOUS_ASSERTION — three seeded siblings and source bytes control refusal
        for n in ("one", "two", "three"):
            self._prior("infra-" + n, "infrastructure,seed" + n)
        path = self._prior("self-map", "infrastructure")
        with open(path, "rb") as f:
            before = f.read()
        plan = [{"op": "upgrade", "src": os.path.basename(path),
                 "dst": os.path.basename(path), "id": "self-map",
                 "to_type": "prior", "statement": "self mapped infrastructure",
                 "keywords": "infrastructure", "origin": ""}]
        receipt = drain.apply(plan, self.mem)
        self.assertEqual(receipt["applied"], 0)
        self.assertIn("3 live entries", receipt["refused"][0]["why"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_staged_replacement_does_not_double_count_its_predecessor(self):
        self._prior("infra-sibling", "infrastructure,seedone")
        path = self._prior("self-map", "infrastructure")
        src = "feedback-new-map.md"
        self._feedback(src, "new mapping with useful provenance")
        plan = [
            {"op": "upgrade", "src": os.path.basename(path),
             "dst": os.path.basename(path), "id": "self-map",
             "to_type": "prior", "statement": "self mapped infrastructure",
             "keywords": "infrastructure", "origin": ""},
            {"op": "retype", "src": src, "dst": "prior-new-map.md",
             "id": "new-map", "to_type": "prior", "statement": "new mapping",
             "keywords": "infrastructure", "origin": ""},
        ]
        receipt = drain.apply(plan, self.mem)
        self.assertEqual((receipt["applied"], receipt["refused"]), (2, []))
        self.assertTrue(os.path.isfile(os.path.join(self.mem, "prior-new-map.md")))

    def test_staged_overwrite_replaces_existing_destination_in_corpus(self):
        self._prior("infra-sibling", "infrastructure,seedone")
        self._prior("self-map", "infrastructure")
        replace = "feedback-replace.md"
        following = "feedback-following.md"
        self._feedback(replace, "replacement with useful provenance")
        self._feedback(following, "following mint with useful provenance")
        plan = [
            {"op": "retype", "src": replace, "dst": "prior-self-map.md",
             "id": "self-map", "to_type": "prior", "statement": "replacement",
             "keywords": "infrastructure", "origin": ""},
            {"op": "retype", "src": following, "dst": "prior-following.md",
             "id": "following", "to_type": "prior", "statement": "following",
             "keywords": "infrastructure", "origin": ""},
        ]
        receipt = drain.apply(plan, self.mem)
        self.assertEqual((receipt["applied"], receipt["refused"]), (2, []))
        self.assertTrue(os.path.isfile(os.path.join(self.mem, "prior-following.md")))

    def test_empty_and_salad_refuse_without_mutating_sources(self):  # noqa: VACUOUS_ASSERTION — byte snapshots positively control each refused source
        empty = "feedback-x.md"
        self._feedback(empty, "go now")
        with open(os.path.join(self.mem, empty), "rb") as f:
            before = f.read()
        receipt = drain.apply(drain.classify(self.mem), self.mem)
        self.assertIn("NO keywords", receipt["refused"][0]["why"])
        with open(os.path.join(self.mem, empty), "rb") as f:
            self.assertEqual(f.read(), before)

        salad = "feedback-salad.md"
        self._feedback(salad, "a statement with useful provenance")
        path = os.path.join(self.mem, salad)
        with open(path, "rb") as f:
            before = f.read()
        plan = [{"op": "retype", "src": salad, "dst": "prior-salad.md",
                 "id": "salad", "to_type": "prior", "statement": "a statement",
                 "keywords": "one giant comma missing keyword cell", "origin": ""}]
        receipt = drain.apply(plan, self.mem)
        self.assertIn("comma-less cell", receipt["refused"][0]["why"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_override_receipt_follows_successful_write(self):
        from helm import store
        self._prior("widgetron-recovery-law",
                    "widgetron,checkpoint,freezes,recovery,rollback")
        src = "feedback-widgetron-recovery-two.md"
        dst = "prior-widgetron-recovery-two.md"
        self._feedback(src, "widgetron checkpoint freezes during recovery rollback")
        calls = []
        write = pk.atomic_write
        record = store.record_mint_events

        def ordered_write(path, text):
            if os.path.basename(path) == dst:
                calls.append("write")
            return write(path, text)

        def ordered_record(events):
            if events:
                calls.append("event")
            return record(events)

        with mock.patch.object(drain.pk, "atomic_write", side_effect=ordered_write), \
                mock.patch.object(store, "record_mint_events",
                                  side_effect=ordered_record):
            receipt = drain.apply(drain.classify(self.mem), self.mem, force_new=True)
        self.assertEqual(receipt["applied"], 1)
        self.assertEqual(calls[:2], ["write", "event"])
        self.assertTrue(any(r.get("verb") == "store.dup_override"
                            and r.get("target") == "widgetron-recovery-two"
                            for r in pk.read_events(20)))

    def test_writer_failure_records_no_duplicate_override(self):  # noqa: VACUOUS_ASSERTION — surviving source positively controls the failed destination/event
        from helm import store
        self._prior("widgetron-recovery-law",
                    "widgetron,checkpoint,freezes,recovery,rollback")
        src = "feedback-widgetron-recovery-two.md"
        dst = "prior-widgetron-recovery-two.md"
        self._feedback(src, "widgetron checkpoint freezes during recovery rollback")
        write = pk.atomic_write

        def fail_destination(path, text):
            if os.path.basename(path) == dst:
                raise OSError("writer failed")
            return write(path, text)

        with mock.patch.object(drain.pk, "atomic_write", side_effect=fail_destination), \
                mock.patch.object(store, "record_mint_events") as record:
            with self.assertRaisesRegex(OSError, "writer failed"):
                drain.apply(drain.classify(self.mem), self.mem, force_new=True)
        record.assert_not_called()
        self.assertTrue(os.path.isfile(os.path.join(self.mem, src)))
        self.assertFalse(os.path.exists(os.path.join(self.mem, dst)))
        self.assertFalse(any(r.get("verb") == "store.dup_override"
                             for r in pk.read_events(20)))

    def test_dry_run_surfaces_refusal_and_force_override_mutates_nothing(self):  # noqa: VACUOUS_ASSERTION — refusal output and stable directory listing control no-write claims
        self._prior("widgetron-recovery-law",
                    "widgetron,checkpoint,freezes,recovery,rollback")
        src = "feedback-widgetron-recovery-two.md"
        self._feedback(src, "widgetron checkpoint freezes during recovery rollback")
        before = sorted(os.listdir(self.mem))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(drain.cmd_drain([]), 0)
        self.assertIn("refuse", out.getvalue())
        self.assertEqual(sorted(os.listdir(self.mem)), before)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(drain.cmd_drain(["--force-new"]), 0)
        self.assertIn("DRY-RUN", out.getvalue())
        self.assertEqual(sorted(os.listdir(self.mem)), before)
        self.assertFalse(any(r.get("verb") == "store.dup_override"
                             for r in pk.read_events(20)))

    def test_guarded_heuristic_trigger_is_written_to_staged_output(self):
        src = "feedback-heuristic.md"
        self._feedback(src, "inspect frozen pane state before relaunch")
        plan = [{"op": "retype", "src": src, "dst": "heuristic-frozen-pane.md",
                 "id": "frozen-pane", "to_type": "heuristic",
                 "statement": "inspect frozen pane state before relaunch",
                 "trigger": "pane freezes on plan prompt, relaunch check",
                 "origin": ""}]
        receipt = drain.apply(plan, self.mem)
        self.assertEqual(receipt["applied"], 1)
        with open(os.path.join(self.mem, "heuristic-frozen-pane.md"),
                  encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("type: heuristic", raw)
        self.assertIn("  trigger: pane freezes on plan prompt, relaunch check,", raw)
        self.assertIn("pane freezes", raw)
        from helm import store
        e = next(e for e in store.load_all(types=("heuristic",))
                 if e["id"] == "frozen-pane")
        self.assertEqual(e["keywords"], e["trigger"])
        self.assertIn("plan prompt", store._kw_list(e["trigger"]))


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
