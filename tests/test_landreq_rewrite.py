#!/usr/bin/env python3
"""Surviving a history rewrite — content identity as the durable key.

THE PROBLEM: helm binds proofs to git SHAs, and a SHA is content-addressed over
HISTORY. A rewrite (filter-repo, a squash-root, a rebased trunk) changes every
recorded tip at once and every receipt dangles. Proven here on a real rewrite:
the tip changes, the patch-id does not.

THE FIX WAS ALREADY HALF-BUILT, which is the interesting part. `_patch_id` has
computed a rebase/ff-STABLE content identity since the rebase-instability work,
and `land_record` has stored it on every receipt — but the index was keyed by
tip alone, so the durable identity sat beside the fragile one and was never the
thing looked up. Not a missing primitive: a primitive wired as evidence instead
of as a key.

TWO INVARIANTS THESE PIN, and the second matters more:

  1. A receipt is findable by CONTENT when its tip no longer resolves.
  2. A migration NEVER moves an attested tip. dispatches is deliberately
     hardened against that — `retarget` survives in replay for already-written
     rows only, its own comment says "there is no shipping verb", and the module
     header lists retarget among the operations not supported. A migration that
     could re-point what a verdict attested could forge one, so translations
     land in a SIDECAR and the attested tip stays exactly as reviewed.
"""
import json
import os
import subprocess
import tempfile
import shutil
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import landreq  # noqa: E402


def _git(cwd, *args, **kw):
    env = dict(os.environ, **kw.pop("env", {}))
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          text=True, env=env)


class RewriteBase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="helm-test-rw-")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "old@example.invalid")
        _git(self.repo, "config", "user.name", "old")
        open(os.path.join(self.repo, "f"), "w").write("one\n")
        _git(self.repo, "add", "f")
        _git(self.repo, "commit", "-qm", "one")
        open(os.path.join(self.repo, "f"), "a").write("two\n")
        _git(self.repo, "add", "f")
        _git(self.repo, "commit", "-qm", "two")
        self.gitdir = os.path.join(self.repo, ".git")
        self.old = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def rewrite(self):
        """Change every sha without changing any content."""
        _git(self.repo, "filter-branch", "-f", "--env-filter",
             "export GIT_AUTHOR_EMAIL=new@example.invalid "
             "GIT_COMMITTER_EMAIL=new@example.invalid", "--", "--all")
        return _git(self.repo, "rev-parse", "HEAD").stdout.strip()


class ContentIdentityTest(RewriteBase):
    def test_the_tip_changes_and_the_patch_id_does_not(self):
        """The whole thesis. If this ever fails, content identity is not a
        durable key and everything built on it is unsound."""
        before = landreq._patch_id(self.gitdir, self.old)
        new = self.rewrite()
        after = landreq._patch_id(self.gitdir, new)
        self.assertNotEqual(self.old, new, "a rewrite must change the sha")
        self.assertTrue(before, "a patch-id must be computable")
        self.assertEqual(before, after,
                         "content identity must survive the rewrite")

    def test_a_receipt_is_found_by_content_when_its_tip_is_gone(self):
        pid = landreq._patch_id(self.gitdir, self.old)
        new = self.rewrite()
        receipt = {"id": self.old, "patch_id": pid}
        with mock.patch.object(landreq, "_receipts_by_tip", return_value={}), \
                mock.patch.object(landreq, "_receipts_by_patch",
                                  return_value={pid: [receipt]}), \
                mock.patch.object(landreq, "_validate_receipt",
                                  side_effect=lambda r, t: (r, None)):
            state, row, why, how = landreq.receipt_for_content(self.gitdir, new)
        self.assertEqual(how, "patch", "it must resolve by content identity")
        self.assertEqual(row["id"], self.old,
                         "the receipt recorded under the OLD tip is the hit")

    def test_the_tip_path_still_wins_when_it_resolves(self):
        """Content identity is the FALLBACK. A tip that still resolves must not
        pay for a patch-id computation or change which row is authoritative."""
        receipt = {"id": self.old}
        with mock.patch.object(landreq, "_receipt_for",
                               return_value=(landreq.R_LOCAL, receipt, None)), \
                mock.patch.object(landreq, "_patch_id") as pid:
            state, row, why, how = landreq.receipt_for_content(
                self.gitdir, self.old, by_tip={})
        self.assertEqual(how, "tip")
        pid.assert_not_called()

    def test_a_patch_index_that_could_not_be_READ_is_not_an_absent_receipt(self):
        """The content path has the same two failure modes as the tip path, and
        for the same reason they must not collapse into one another: a CORRUPT
        row is a receipt that exists and is bad, an unreadable index is helm
        never having looked. Reachable because these are two separate reads of
        the same file — the tip read can succeed and the patch read fail."""
        for index, want in ((landreq._receipts_by_patch(), landreq.R_NONE),
                            ({landreq._RECEIPT_LEDGER_ERROR: "corrupt ledger line 1 x"},
                             landreq.R_REJECTED),
                            ({landreq._RECEIPT_LEDGER_UNREADABLE: "OSError: EIO"},
                             landreq.R_UNREADABLE)):
            state, _row, _why, _how = landreq.receipt_for_content(
                self.gitdir, self.old, by_tip={}, by_patch=index)
            self.assertEqual(state, want, index)

    def test_no_patch_id_degrades_to_the_tip_verdict_never_to_a_refusal(self):
        """Fail-open: a missing stable identity must never turn a found receipt
        into a refusal, nor a NONE into something louder."""
        with mock.patch.object(landreq, "_receipts_by_tip", return_value={}), \
                mock.patch.object(landreq, "_patch_id", return_value=None):
            state, row, why, how = landreq.receipt_for_content(self.gitdir, self.old)
        self.assertEqual(state, landreq.R_NONE)
        self.assertIsNone(how)


class CommitMapTest(RewriteBase):
    def _map(self, body):
        p = os.path.join(self.repo, "commit-map")
        open(p, "w").write(body)
        return p

    def test_a_dropped_commit_is_never_applied_as_a_translation(self):
        """filter-repo maps a DROPPED commit to forty zeros. Applying that
        would point a ref at nothing while looking migrated — strictly worse
        than leaving it dangling."""
        p = self._map("old new\n%s %s\n" % (self.old, "0" * 40))
        m, err = landreq.read_commit_map(p)
        self.assertIsNone(m)
        self.assertIn("dropped-commit", err)

    def test_a_real_pair_is_read_and_the_header_ignored(self):
        new = "b" * 40
        p = self._map("old                                      new\n%s %s\n"
                      % (self.old, new))
        m, err = landreq.read_commit_map(p)
        self.assertIsNone(err)
        self.assertEqual(m, {self.old: new})

    def test_migrate_leaves_unmapped_refs_alone_rather_than_guessing(self):
        p = self._map("old new\n%s %s\n" % ("a" * 40, "b" * 40))
        with mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": "c" * 40}}):
            r, err = landreq.migrate_refs(p, repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(r["translations"], [])
        self.assertEqual(r["unmapped"], 1, "unmapped is counted, never guessed")

    def test_an_abbreviated_ref_matches_its_UNIQUE_full_sha_in_the_map(self):
        """Ledgers record abbreviations; filter-repo's map is keyed by full
        shas. Without prefix matching every abbreviated ref reads as unmapped
        and is silently left behind — a migration that looks complete and is
        not. (Caught by a mutation that this suite did not kill.)"""
        full, new = "a" * 40, "b" * 40
        p = self._map("old new\n%s %s\n" % (full, new))
        with mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": full[:12]}}):
            r, err = landreq.migrate_refs(p, repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(len(r["translations"]), 1)
        self.assertEqual(r["translations"][0]["new"], new)

    def test_an_AMBIGUOUS_abbreviation_is_refused_not_guessed(self):
        """The negative control for the line above. Two full shas sharing the
        prefix must leave the ref alone — picking one would forge a binding,
        and prefix matching without this is strictly dangerous."""
        p = self._map("old new\n%s %s\n%s %s\n"
                      % ("a" * 40, "b" * 40, "a" * 39 + "c", "d" * 40))
        with mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": "a" * 12}}):
            r, err = landreq.migrate_refs(p, repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(r["translations"], [], "ambiguity must never resolve")
        self.assertEqual(r["unmapped"], 1)

    def test_dry_run_writes_nothing(self):
        new = "b" * 40
        p = self._map("old new\n%s %s\n" % (self.old, new))
        with mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": self.old}}):
            r, err = landreq.migrate_refs(p, repo=self.repo)
        self.assertIsNone(err)
        self.assertEqual(len(r["translations"]), 1)
        self.assertFalse(r["applied"])
        self.assertNotIn("sidecar", r, "a dry run must not record anything")

    def test_apply_records_a_sidecar_and_does_not_touch_the_ledger(self):
        """THE INVARIANT THAT MATTERS. dispatches is hardened so nothing can
        move an attested tip; a migration that could would be able to forge a
        review. The translation is recorded beside the ledger, never in it."""
        new = "b" * 40
        p = self._map("old new\n%s %s\n" % (self.old, new))
        home = tempfile.mkdtemp(prefix="helm-test-rw-home-")
        rows = {"d1": {"id": "d1", "reviewed_tip": self.old}}
        try:
            with mock.patch.dict(os.environ, {"HELM_HOME": home}), \
                    mock.patch.object(landreq, "project", return_value=({}, None)), \
                    mock.patch.object(landreq.dispatches, "rows", return_value=rows):
                r, err = landreq.migrate_refs(p, repo=self.repo, apply=True)
                self.assertIsNone(err)
                self.assertEqual(r["written"], 1)
                self.assertTrue(os.path.exists(r["sidecar"]))
                rec = json.loads(open(r["sidecar"]).read().splitlines()[0])
                self.assertEqual((rec["old"], rec["new"]), (self.old, new))
                # the ledger row is untouched
                self.assertEqual(rows["d1"]["reviewed_tip"], self.old,
                                 "the attested tip must never be rewritten")
        finally:
            shutil.rmtree(home, ignore_errors=True)


def _has_filter_repo():
    try:
        p = subprocess.run(["git", "filter-repo", "--version"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


class SidecarBase(unittest.TestCase):
    """A helm home of our own, so the sidecar under test is the one we wrote
    and never the operator's real migration history."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="helm-test-chain-home-")
        self.sidecar = os.path.join(self.home, "_global", ".state",
                                    "ref-migrations.jsonl")
        os.makedirs(os.path.dirname(self.sidecar), exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"HELM_HOME": self.home})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def record(self, *pairs):
        with open(self.sidecar, "a", encoding="utf-8") as f:
            for old, new in pairs:
                f.write(json.dumps({"old": old, "new": new}) + "\n")


class ChainedRewriteTest(SidecarBase):
    """TWO filter-repo passes, real maps, real shas.

    THE BUG, measured live: pass 1 recorded original->A; pass 2 was run from
    the rewritten state, so its map is keyed by A while the ledgers still hold
    the originals; the run reported `0 commit ids translatable, 539 left
    alone`. A migration that ran, reported cleanly, and moved nothing. The
    chain had to be composed by hand, which is the work this now does.

    Hand-written maps cover the edge cases below; this one is real because the
    hand-written ones never would have shown the shape — the second map being
    keyed by the first pass's OUTPUT is a fact about filter-repo, not a fact
    anyone would have thought to fixture."""

    @classmethod
    def setUpClass(cls):
        if not _has_filter_repo():
            raise unittest.SkipTest("git filter-repo is not installed")

    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp(prefix="helm-test-chain-")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        for i in ("one", "two", "three"):
            open(os.path.join(self.repo, "f"), "a").write(i + "\n")
            _git(self.repo, "add", "f")
            _git(self.repo, "commit", "-qm", i)
        self.original = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        super().tearDown()

    def _pass(self, prefix, name):
        """One filter-repo run; returns (saved commit-map path, new HEAD).

        The filter-repo STATE is wiped between passes on purpose. Left in
        place, filter-repo composes the chain itself and the next map comes
        back keyed by the ORIGINALS — which is the shape that already worked.
        Its own documented workflow is a fresh clone per pass, and a fresh
        clone has no state; that is the shape that broke."""
        p = subprocess.run(
            ["git", "-C", self.repo, "filter-repo", "--force",
             "--message-callback", "return b'%s' + message" % prefix],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        saved = os.path.join(self.home, name)
        shutil.copyfile(
            os.path.join(self.repo, ".git", "filter-repo", "commit-map"), saved)
        shutil.rmtree(os.path.join(self.repo, ".git", "filter-repo"),
                      ignore_errors=True)
        return saved, _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def _migrate(self, cmap, tip):
        with mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": tip}}):
            return landreq.migrate_refs(cmap, repo=self.repo, apply=True)

    def test_the_second_pass_map_is_keyed_by_the_first_passs_output(self):
        """The premise of the whole fix, asserted rather than assumed. If this
        ever stops being true, composition is solving a problem that no longer
        exists and the direct lookup was always enough."""
        map1, a = self._pass("P1-", "map1")
        map2, _b = self._pass("P2-", "map2")
        m1, err1 = landreq.read_commit_map(map1)
        m2, err2 = landreq.read_commit_map(map2)
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual([o for o in m1 if o in m2], [],
                         "no original survives into the second map")
        self.assertIn(a, m2, "the second map is keyed by the first's output")

    def test_a_two_pass_chain_composes_the_original_to_the_newest_sha(self):
        map1, a = self._pass("P1-", "map1")
        r1, err = self._migrate(map1, self.original)
        self.assertIsNone(err)
        self.assertEqual([t["new"] for t in r1["translations"]], [a])
        self.assertEqual(r1["chained"], 0, "pass 1 needs no composition")

        map2, b = self._pass("P2-", "map2")
        r2, err = self._migrate(map2, self.original)
        self.assertIsNone(err)
        self.assertEqual(r2["unmapped"], 0,
                         "the original must not read as unmapped a second time")
        self.assertEqual(len(r2["translations"]), 1)
        self.assertEqual(r2["translations"][0]["old"], self.original)
        self.assertEqual(r2["translations"][0]["new"], b,
                         "the recorded translation is original -> NEWEST")
        self.assertEqual(r2["chained"], 1, "and it was reached by composition")
        self.assertNotEqual(a, b)

    def test_the_composed_sha_is_a_commit_that_actually_exists(self):
        """The end of a composed chain has to be a real tip in the rewritten
        repo, not merely a string that appeared in a file."""
        map1, _a = self._pass("P1-", "map1")
        self._migrate(map1, self.original)
        map2, _b = self._pass("P2-", "map2")
        r2, _err = self._migrate(map2, self.original)
        composed = r2["translations"][0]["new"]
        self.assertEqual(
            landreq._batch_exists(os.path.join(self.repo, ".git"), [composed]),
            {composed: True})

    def test_the_sidecar_answers_in_ONE_hop_after_composition(self):
        """Both rows are on disk — original->A from pass 1 and original->B
        appended by pass 2 — and the reader must hand back B. Verified rather
        than reasoned about: this is the only thing making the sidecar a
        one-hop answer, and `lr refs` reports whatever it says."""
        map1, a = self._pass("P1-", "map1")
        self._migrate(map1, self.original)
        map2, b = self._pass("P2-", "map2")
        self._migrate(map2, self.original)
        rows = [json.loads(l) for l in
                open(self.sidecar).read().splitlines() if l.strip()]
        self.assertEqual([r["new"] for r in rows], [a, b],
                         "append-only: the composed row lands last")
        self.assertEqual(landreq.ref_translations()[self.original], b,
                         "and the last row is the one read back")

    def test_a_third_pass_composes_from_the_newest_hop_not_the_stale_head(self):
        map1, _a = self._pass("P1-", "map1")
        self._migrate(map1, self.original)
        map2, _b = self._pass("P2-", "map2")
        self._migrate(map2, self.original)
        map3, c = self._pass("P3-", "map3")
        r3, err = self._migrate(map3, self.original)
        self.assertIsNone(err)
        self.assertEqual(r3["translations"][0]["new"], c)
        self.assertEqual(r3["chained"], 1)
        self.assertEqual(landreq.ref_translations()[self.original], c)

    def test_an_unchanged_commit_is_not_recorded_as_having_moved(self):
        """filter-repo lists every commit it walked, so a run that changed one
        message still emits `X X` for the untouched ancestors. Recording that
        tells `lr refs` a dangling ref moved to where it already was, and
        leaves a self-map for the chain walk to trip over."""
        p = subprocess.run(
            ["git", "-C", self.repo, "filter-repo", "--force",
             "--message-callback",
             "return message.replace(b'three', b'THREE')"],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        cmap = os.path.join(self.repo, ".git", "filter-repo", "commit-map")
        raw = [l.split() for l in open(cmap).read().splitlines()]
        self.assertTrue([x for x in raw if len(x) == 2 and x[0] == x[1]],
                        "the fixture must actually contain identity rows")
        m, err = landreq.read_commit_map(cmap)
        self.assertIsNone(err)
        self.assertEqual([o for o in m if m[o] == o], [],
                         "an unchanged commit is not a translation")


class ChainCompositionTest(SidecarBase):
    """The refusals, on hand-written chains — the shapes a real rewrite will
    not produce on demand but an append-only text file can always contain."""

    def _migrate(self, map_body, tip):
        """A hand-written map against a mocked repo — these cases are about the
        chain, and a real working tree would only add a way to fail."""
        p = os.path.join(self.home, "cm")
        open(p, "w").write(map_body)
        with mock.patch.object(landreq.dispatches, "_repo_info",
                               return_value={"repo": "/repo",
                                             "repo_id": "/repo/.git"}), \
                mock.patch.object(landreq, "project", return_value=({}, None)), \
                mock.patch.object(landreq.dispatches, "rows", return_value={
                    "d1": {"id": "d1", "reviewed_tip": tip}}):
            return landreq.migrate_refs(p, repo="/repo")

    def test_a_cycle_refuses_rather_than_spinning(self):
        self.assertIsNone(landreq._chain_end(
            "a" * 40, {"a" * 40: "b" * 40, "b" * 40: "a" * 40}))

    def test_a_cycle_is_DETECTED_not_merely_outlasted_by_the_bound(self):
        """The bound alone would also stop — after _MAX_CHAIN_HOPS pointless
        lookups, and only because that constant happens to be small. The
        revisit check is what makes the refusal a fact about the DATA rather
        than a fact about the constant, so it is asserted on its own."""
        class Counting(dict):
            reads = 0

            def get(self, key, default=None):
                Counting.reads += 1
                return dict.get(self, key, default)

        t = Counting({"a" * 40: "b" * 40, "b" * 40: "a" * 40})
        self.assertIsNone(landreq._chain_end("a" * 40, t))
        self.assertLessEqual(Counting.reads, 3,
                             "a cycle must be caught, not outlasted")

    def test_an_UNRECORDED_sha_is_None_not_itself(self):
        """None means DO NOT COMPOSE. Handing back the input would send the
        caller to look the same sha up in the same map a second time and call
        the identical miss an answer."""
        self.assertIsNone(landreq._chain_end("a" * 40, {}))

    def test_a_cycle_in_the_SIDECAR_leaves_the_ref_alone(self):
        """The refusal seen from outside: a poisoned sidecar must cost the ref
        a translation, never the run."""
        self.record(("a" * 40, "b" * 40), ("b" * 40, "a" * 40))
        r, err = self._migrate("old new\n%s %s\n" % ("b" * 40, "c" * 40),
                               "a" * 40)
        self.assertIsNone(err)
        self.assertEqual(r["translations"], [])
        self.assertEqual((r["unmapped"], r["chained"]), (1, 0))

    def test_a_sha_translated_to_ITSELF_refuses(self):
        self.assertIsNone(landreq._chain_end("a" * 40, {"a" * 40: "a" * 40}))

    def test_a_chain_longer_than_the_bound_refuses_instead_of_running_on(self):
        """Bounded, not merely cycle-checked: an acyclic chain longer than any
        real rewrite history is a shape we do not understand, and this audit
        must stop rather than walk it."""
        n = landreq._MAX_CHAIN_HOPS
        long_chain = {"%040x" % i: "%040x" % (i + 1) for i in range(n + 2)}
        self.assertIsNone(landreq._chain_end("%040x" % 0, long_chain))
        short = {"%040x" % i: "%040x" % (i + 1) for i in range(n - 2)}
        self.assertEqual(landreq._chain_end("%040x" % 0, short),
                         "%040x" % (n - 2), "a chain within the bound resolves")

    def test_the_end_of_a_chain_is_the_last_recorded_sha(self):
        self.assertEqual(
            landreq._chain_end("a" * 40, {"a" * 40: "b" * 40, "b" * 40: "c" * 40}),
            "c" * 40)

    def test_an_AMBIGUOUS_hop_is_refused_not_read_as_the_end_of_the_chain(self):
        """The distinction _map_lookup exists to preserve. A fork collapsed
        into 'nothing further recorded' would translate the ref from wherever
        the walk happened to stop — a sha we cannot show it ever reached."""
        forked = {"a" * 40: "b" * 40, "a" * 39 + "f": "c" * 40}
        self.assertIsNone(landreq._chain_end("a" * 12, forked))
        self.assertEqual(landreq._map_lookup(forked, "a" * 12), (None, True))

    def test_an_ambiguous_DIRECT_lookup_is_not_routed_around_by_the_chain(self):
        """When the map itself forks on the ref, composition must not offer a
        second opinion. Routing around a fork is still choosing which commit
        the ref meant.

        The chain here is deliberately WORKING — it ends on a sha the map does
        translate — so that dropping the ambiguity check produces a confident
        wrong answer rather than the same refusal by another road. A control
        whose second path also fails proves nothing about which path was
        taken."""
        self.record(("a" * 12, "e" * 40))
        r, err = self._migrate(
            "old new\n%s %s\n%s %s\n%s %s\n"
            % ("a" * 40, "b" * 40, "a" * 39 + "f", "c" * 40,
               "e" * 40, "d" * 40), "a" * 12)
        self.assertIsNone(err)
        self.assertEqual(r["translations"], [],
                         "an ambiguous ref is refused, not composed around")
        self.assertEqual((r["unmapped"], r["chained"]), (1, 0))

    def test_a_chain_that_ends_outside_the_map_is_left_alone(self):
        self.record(("a" * 40, "b" * 40))
        r, err = self._migrate("old new\n%s %s\n" % ("e" * 40, "f" * 40),
                               "a" * 40)
        self.assertIsNone(err)
        self.assertEqual(r["translations"], [])
        self.assertEqual((r["unmapped"], r["chained"]), (1, 0))

    def test_a_DROPPED_commit_cannot_re_enter_through_the_sidecar(self):
        """read_commit_map refuses forty zeros on the way in; this is the other
        door. A sidecar row is append-only text that predates any guard, and a
        chain ending at 000…0 would translate a proof to nothing."""
        self.record(("a" * 40, "0" * 40))
        self.assertEqual(landreq.ref_translations(), {})
        self.assertIsNone(landreq._chain_end("a" * 40, landreq.ref_translations()),
                          "with the row refused there is no chain to follow")

    def test_a_self_translating_ROW_is_not_read_back_as_a_chain(self):
        self.record(("a" * 40, "a" * 40))
        self.assertEqual(landreq.ref_translations(), {})

    def test_a_real_translation_is_still_read_back(self):
        """The positive control for the two refusals above — a guard that
        drops everything reports the same empty dict as a working one."""
        self.record(("a" * 40, "b" * 40))
        self.assertEqual(landreq.ref_translations(), {"a" * 40: "b" * 40})


class UnchangedCommitMapTest(unittest.TestCase):
    def test_an_all_unchanged_map_is_refused_with_a_reason(self):
        d = tempfile.mkdtemp(prefix="helm-test-cm-")
        try:
            p = os.path.join(d, "cm")
            open(p, "w").write("old new\n%s %s\n" % ("a" * 40, "a" * 40))
            m, err = landreq.read_commit_map(p)
            self.assertIsNone(m)
            self.assertIn("1 unchanged", err)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
