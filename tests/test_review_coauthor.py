#!/usr/bin/env python3
"""The review procedure recognizes both fix modes on every surface.

THE RULING (owner): "these codex models are just as good as claude models and
should be equal counterparts even if a claude happens to be the integrator, I
think our system has evolved to be too restrictive because the original
efficient meld based cross family review procedures we tried to put in place
were all based on finding the most efficient path to the maximal quality code,
maybe we can be creative with how things are modified to accommodate such as
having multiple authors on a given task or file edit." And on HOW: "this isn't
the case where you're just adding new things, you should be updating the old
things that were restrictive or wrong."

SO THIS FILE IS A MUST-MISS SUITE, not a must-hit one. The failure it catches
is the one that actually happens to a sweep: the new sentence is added and the
old restrictive sentence is left standing one paragraph away, so a reader
meeting the old one first never reaches the new one. Each removed sentence is
listed VERBATIM below with the file it was removed from; a surface that grows
the old wording back goes red.

EVERY MUST-MISS IS PAIRED WITH A POSITIVE CONTROL from the same file, because a
must-miss suite over a file that was DELETED, RENAMED or emptied passes
vacuously and reports a sweep that reached nothing. The control is a sentence
the rewrite put there, so it proves this test read the rewritten file.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import dispatches, gitfacts, landreq, vcs  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: path -> (must-miss sentences removed from it, one must-hit control)
SURFACES = {
    "AGENTS.md": (
        ("- **Reviewed SHAs are immutable** — fix on top with a new commit; "
         "never amend\n  or rebase a SHA that was posted for review.\n- **The "
         "store is the shared memory**",),
        "MELD-DIFF posts the exact fix as a diff",
    ),
    "README.md": (
        ("is better, but because a blind spot is a property of a shared "
         "frame.\n- **Minted whole-suite gates**",),
        "equal counterparts, not a writing tier and a witnessing",
    ),
    "docs/NEW_AGENT_GUIDE.md": (
        ("exact reviewed tip — evidence is not clearance.\n- **The landing "
         "bar is ONE approval-tier APPROVE**",),
        "Reviewing? Patch the mechanical findings yourself.",
    ),
    "docs/VERBS.md": (
        ("A FIX verdict tells an author to cure.",),
        "the PATCH reviewer's committed cure, recorded",
    ),
    "helm/lane_discipline.py": (
        ("gate the whole suite -> a cross-family reviewer\nAPPROVEs carrying "
         "the gate token -> the integrator FOLDS it to main.",),
        "A lane may\ntherefore carry SEVERAL AUTHORS",
    ),
    "helm/dispatches.py": (
        ("# A FIX verdict says the AUTHOR owes a cure",),
        "--patch-tip belongs to --fix",
    ),
    "agents/claudecode/skills/reviewer-implements-own-findings/SKILL.md": (
        ("Do not turn hot context into a\ncold handoff unless an exception "
         "applies.",
         "Route elsewhere when the patch is broad",
         "comms send --from pane:"),
        "This skill is the review procedure, not an exception to it.",
    ),
    "agents/claudecode/skills/build/SKILL.md": (
        ("- **Author != reviewer.** A non-trivial change gets a cross-family "
         "refutation before it lands; the\n  refuter looks for the substrate "
         "rule the change violates. A bare \"looks good\" is not review.\n",),
        "The refuter follows the row's REVIEW FIX MODE",
    ),
    "agents/claudecode/skills/fix/SKILL.md": (
        ("catches earlier and costs no more.)\n\n## Red flags",),
        "follow the row's REVIEW FIX MODE for a mechanical cure",
    ),
    "agents/claudecode/skills/devops/SKILL.md": (
        ("Bounded findings can be fixed in-pass by the reviewer.",
         "- `reviewer-implements-own-findings` for bounded review fixes."),
        "Credit the actual authors on close.",
    ),
    "agents/claudecode/skills/fleet-maintenance/SKILL.md": (
        ("fleet's merges on one family's remaining budget.\n\n## 7.",),
        "not a writer tier and a witness tier",
    ),
    # The SubagentStart brief, which every build-capable helm subagent is
    # handed before its first step, by the owner's ruling. It never taught the
    # witness-only form, so it has nothing to must-miss; it is listed so a
    # sweep of this procedure reaches the one surface a review AGENT is
    # guaranteed to read. The control is the brief's own sentence (BRIEF, at
    # most 560 bytes, tests/test_hook_budgets.py), not the long form's.
    "helm/saguide.py": (
        (),
        "REVIEW FIX MODE: PATCH commits cure off exact tip",
    ),
}


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class EveryTaughtSurfaceCarriesTheCoAuthorProcedureTest(unittest.TestCase):
    def test_the_witness_only_sentences_are_gone_from_every_surface(self):  # noqa: VACUOUS_ASSERTION — absence IS the claim, and the arm ends on an unconditional EXACT count of the sentences swept plus a floor on that count, so an emptied SURFACES table fails here rather than passing green
        swept = 0
        for rel, (gone, _control) in SURFACES.items():
            text = read(rel)
            for sentence in gone:
                with self.subTest(surface=rel, sentence=sentence[:48]):
                    self.assertNotIn(
                        sentence, text,
                        "%s still teaches the witness-only procedure: the "
                        "owner's ruling was to UPDATE the restrictive wording, "
                        "not to add a second procedure beside it" % rel)
                swept += 1
        self.assertEqual(swept, sum(len(g) for g, _c in SURFACES.values()))
        self.assertGreaterEqual(swept, 14, "the must-miss set shrank")

    def test_each_surface_carries_the_rewrite_that_replaced_it(self):
        """THE CONTROL. Without it, deleting any of these files turns the
        must-miss arm green while the fleet loses the procedure entirely."""
        seen = 0
        for rel, (_gone, control) in SURFACES.items():
            with self.subTest(surface=rel):
                self.assertIn(control, read(rel),
                              "%s lost the rewritten procedure" % rel)
            seen += 1
        self.assertEqual(seen, len(SURFACES))
        self.assertGreaterEqual(seen, 11, "the swept surface set shrank")

    def test_the_anchor_skill_carries_the_whole_procedure_in_one_place(self):
        """The owner's instruction was to sharpen the existing anchor rather
        than add a parallel document, so every clause of the procedure has to
        be findable in that ONE file."""
        text = read("agents/claudecode/skills/"
                    "reviewer-implements-own-findings/SKILL.md")
        self.assertIn("# Reviewer Implements Own Findings", text)
        seen, clauses = 0, ("EITHER model family",
                       "REVIEW FIX MODE",
                       "commit the cure off the EXACT reviewed tip",
                       "git -C <repo> fetch --no-write-fetch-head <scratch>/wt",
                       "do not push",
                       "--patch-tip",
                       "MELD-DIFF: do not commit the mechanical cure",
                       "--no-patch-because",
                       "the ledger records each author",
                       "Design findings go to a meld.",
                       "wrote none of that tip")
        for clause in clauses:
            with self.subTest(clause=clause):
                self.assertIn(clause, text)
            seen += 1
        self.assertEqual(seen, len(clauses), "the clause sweep did not run")

    def test_public_reference_names_both_cure_modes(self):
        self.assertIn("PATCH commits it, while MELD-DIFF gives the author an exact diff",
                      " ".join(read("README.md").split()))
        verbs = read("docs/VERBS.md")
        self.assertIn("In MELD-DIFF, the reviewer\n  posts the exact mechanical cure",
                      verbs)
        self.assertIn("Only a new eligible round at round 3 or later receives",
                      verbs)
        self.assertIn("A review whose original full brief is recoverable",
                      verbs)
        # The verb help moved to one file per verb (task/3918); dispatch's
        # help is helm/help/dispatch.txt, the text `helm dispatch --help` prints.
        help_text = read("helm/help/dispatch.txt")
        self.assertIn("a recoverable full brief travels with the move",
                      help_text)
        self.assertIn("in MELD-DIFF the reviewer posts the exact diff",
                      help_text)
        self.assertNotIn("the DM body does NOT travel", help_text)


class LandedCloseCreditsEveryAuthorTest(unittest.TestCase):
    """`chain_credits` + `credit_line` — the derivation and the threshold.

    Unit arms on purpose: the printed line is one branch over these two, and a
    whole close ladder would measure git, the trunk pin and the delivery
    declaration rather than the credit question.
    """

    def test_a_reviewer_patch_makes_the_lane_two_authored(self):
        lr = {"id": "r1", "chain_root": "r1", "author": "seat-a",
              "reviewer": "seat-b", "patch_tip": "f" * 40,
              "patch_author": "seat-b"}
        self.assertEqual(landreq.chain_credits(lr), ["seat-a", "seat-b"])
        self.assertIn("seat-a", landreq.credit_line(
            landreq.chain_credits(lr)))
        self.assertIn("seat-b", landreq.credit_line(
            landreq.chain_credits(lr)))

    def test_an_earlier_round_of_the_same_chain_is_credited_too(self):
        """The cure carrying a reviewer's commit is usually an EARLIER round
        than the one that lands, so a row-local read would credit seat-e."""
        landing = {"id": "r2", "chain_root": "r1", "author": "seat-a",
                   "reviewer": "seat-b"}
        rows = {"r1": {"id": "r1", "chain_root": "r1", "author": "seat-a",
                       "reviewer": "seat-c", "patch_tip": "a" * 40,
                       "patch_author": "seat-c"},
                "r2": landing,
                # A different chain in the same projection must not be credited.
                "z9": {"id": "z9", "chain_root": "z9", "author": "seat-d",
                       "patch_tip": "b" * 40, "patch_author": "seat-e"}}
        self.assertEqual(landreq.chain_credits(landing, rows),
                         ["seat-a", "seat-c"])

    def test_one_author_prints_no_line_and_blanks_are_never_credited(self):
        solo = {"id": "r3", "author": "seat-a", "reviewer": "seat-b"}
        self.assertEqual(landreq.chain_credits(solo), ["seat-a"])
        self.assertIsNone(landreq.credit_line(landreq.chain_credits(solo)))
        self.assertIsNone(landreq.credit_line([]))
        self.assertIsNone(landreq.credit_line(["seat-a", "", "   ", None]))

    def test_a_patch_tip_with_no_recorded_author_falls_back_to_the_reviewer(self):
        lr = {"id": "r4", "author": "seat-a", "reviewer": "seat-b",
              "patch_tip": "c" * 40}
        self.assertEqual(landreq.chain_credits(lr), ["seat-a", "seat-b"])

    def test_a_row_with_no_patch_tip_never_credits_its_reviewer(self):
        """MUST-MISS: reading a review is not authorship, and a credit list
        that names every reviewer is a credit list seat-e believes."""
        lr = {"id": "r5", "author": "seat-a", "reviewer": "seat-b"}
        self.assertEqual(landreq.chain_credits(lr), ["seat-a"],
                         "the positive half: the lane author IS credited")
        self.assertNotIn("seat-b", landreq.chain_credits(lr))


class PatchTipImmutableRepositoryProofTest(unittest.TestCase):
    """Real synthetic Git graphs; no ledger, credentials or live repository.

    Ordinary Git must affirm each rewritten-graph attack before the proof
    refuses it. A real descendant remains the positive control in that same
    repository, so refusing every patch cannot satisfy these arms.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        # The host's git, wherever it keeps it (a build host may keep it
        # outside os.defpath's /bin:/usr/bin), and nothing else of its PATH.
        git_dir = os.path.dirname(shutil.which("git") or "/usr/bin/git")
        env = {"PATH": os.pathsep.join((git_dir, os.defpath)),
               "HOME": self.tmp,
               "HELM_HOME": os.path.join(self.tmp, "helm"),
               "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_AUTHOR_NAME": "Synthetic Author",
               "GIT_AUTHOR_EMAIL": "author@example.invalid",
               "GIT_COMMITTER_NAME": "Synthetic Author",
               "GIT_COMMITTER_EMAIL": "author@example.invalid"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = os.path.join(self.tmp, "owner")
        self.git("init", "-q", self.repo, cwd=self.tmp)
        self.git("commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        self.reviewed = self.commit(self.base, "reviewed")
        self.patch = self.commit(self.reviewed, "cure")
        self.sibling = self.commit(self.base, "other branch")
        self.git("update-ref", "refs/heads/reviewed", self.reviewed)
        self.common = os.path.realpath(os.path.join(self.repo, ".git"))
        # THE ROW NAMES A LANE WITH NO LOCAL BRANCH, the common case: every arm
        # below that does not make refs/heads/lane/x also proves a missing lane
        # branch is no refusal.
        self.row = {"repo_root": self.repo, "repo_id": self.common,
                    "lane": "x"}

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", "-C", cwd or self.repo, *args],
                              input="", capture_output=True, text=True,
                              timeout=10, check=check)

    def commit(self, parent, message, cwd=None):
        return self.git("commit-tree", self.tree, "-p", parent, "-m", message,
                        cwd=cwd).stdout.strip()

    def proof(self, patch=None, row=None):
        return dispatches._patch_tip_ancestry(
            self.row if row is None else row, self.reviewed,
            self.patch if patch is None else patch)

    def test_real_descendant_passes_and_real_sibling_refuses(self):
        self.assertEqual(self.proof(), None)
        self.assertEqual(self.git("rev-parse", self.sibling + "^{commit}")
                         .stdout.strip(), self.sibling)
        self.assertIn("does not descend", self.proof(self.sibling))

    def test_a_cure_committed_in_a_shared_clone_is_proven_once_fetched(self):
        """The path a reader with no room takes (skill
        reviewer-implements-own-findings, step 3). A commit made in a `git
        clone --shared` clone lives in the clone's own object store, so the
        repo cannot resolve it and the refusal names the fetch. After `git -C
        <repo> fetch --no-write-fetch-head <clone> <sha>`, which moves no ref
        and leaves FETCH_HEAD unwritten, the same id is proven."""
        clone = os.path.join(self.tmp, "reader-clone")
        self.git("clone", "-q", "--shared", self.repo, clone, cwd=self.tmp)
        self.git("checkout", "-q", "--detach", self.reviewed, cwd=clone)
        self.git("commit", "--allow-empty", "-qm", "reader cure", cwd=clone)
        cure = self.git("rev-parse", "HEAD", cwd=clone).stdout.strip()
        refs = self.git("for-each-ref").stdout
        refused = self.proof(cure)
        self.assertIn("does not resolve to a commit", refused)
        self.assertIn("fetch --no-write-fetch-head <clone> %s" % cure, refused)
        self.git("fetch", "-q", "--no-write-fetch-head", clone, cure)
        self.assertEqual(self.git("for-each-ref").stdout, refs)
        self.assertFalse(os.path.exists(os.path.join(self.common, "FETCH_HEAD")))
        self.assertEqual(self.proof(cure), None)

    def test_legacy_graft_cannot_make_a_sibling_a_cure(self):
        graft = os.path.join(self.common, "info", "grafts")
        with open(graft, "w", encoding="utf-8") as f:
            f.write("%s %s\n" % (self.sibling, self.reviewed))
        self.assertEqual(self.git("merge-base", "--is-ancestor",
                                  self.reviewed, self.sibling).returncode, 0)
        self.assertIn("does not descend", self.proof(self.sibling))
        self.assertEqual(self.proof(), None)

    def test_replacement_cannot_make_a_sibling_a_cure(self):
        self.git("replace", "--graft", self.sibling, self.reviewed)
        self.assertEqual(self.git("merge-base", "--is-ancestor",
                                  self.reviewed, self.sibling).returncode, 0)
        self.assertIn("does not descend", self.proof(self.sibling))
        self.assertEqual(self.proof(), None)

    def test_shallow_history_is_unknown_not_a_negative_ancestry_claim(self):
        self.assertEqual(self.proof(), None)
        shallow = os.path.join(self.common, "shallow")
        with open(shallow, "w", encoding="utf-8") as f:
            f.write(self.patch + "\n")
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository")
                         .stdout.strip(), "true")
        self.assertIn("shallow or its completeness", self.proof())
        os.unlink(shallow)
        self.assertEqual(self.proof(), None)

    def test_missing_parent_object_is_unmeasured_not_non_descendant(self):
        self.assertEqual(self.proof(), None)
        os.unlink(os.path.join(self.common, "objects", self.reviewed[:2],
                               self.reviewed[2:]))
        self.assertEqual(self.git("cat-file", "-t", self.patch).stdout.strip(),
                         "commit")
        self.assertIn("ancestry could not be measured", self.proof())

    def test_the_authors_successor_on_its_own_lane_branch_is_no_patch(self):
        """Every commit carries the one git identity, so a tip that descends
        from the reviewed one may be the AUTHOR'S next commit as easily as a
        reviewer's cure. The lane's own branch holding it says which: a
        reviewer's cure lives off the lane until the author agrees and
        fast-forwards. The control is a cure off the same reviewed tip that
        the lane does not hold, and the same patch before the branch
        existed."""
        self.assertEqual(self.proof(), None,
                         "a lane with no local branch cannot hold the tip")
        self.git("update-ref", "refs/heads/lane/x", self.patch)
        refused = self.proof()
        self.assertIn("that tip is on the lane's own branch", refused)
        self.assertIn("refs/heads/lane/x", refused)
        self.assertIn("the author fast-forwards after agreeing", refused)
        cure = self.commit(self.reviewed, "the reviewer's cure")
        self.assertEqual(self.proof(cure), None)
        self.assertIn("that tip is on the lane's own branch",
                      self.proof(self.reviewed),
                      "the reviewed tip itself is on the lane, and is no cure")

    def test_a_prefixed_lane_is_the_lanes_own_and_the_send_ref_is_not(self):
        """A historical row spells its lane `lane/x`: that is still the lane's
        own branch. The branch the send's ref resolved through (`ref_branch`)
        is not: a review is sent at a branch its reader then commits its cure
        on, so that branch holding the tip says nothing about who wrote it."""
        self.git("update-ref", "refs/heads/lane/x", self.patch)
        self.assertIn("that tip is on the lane's own branch",
                      self.proof(row=dict(self.row, lane="lane/x")))
        self.git("update-ref", "refs/heads/feature", self.patch)
        sent = dict(self.row, lane="y", ref_branch="refs/heads/feature")
        self.assertEqual(self.git("merge-base", "--is-ancestor", self.patch,
                                  "refs/heads/feature").returncode, 0,
                         "fixture premise: the send's branch holds the cure")
        self.assertEqual(self.proof(row=sent), None)

    def test_an_unreadable_lane_branch_refuses_unknown(self):  # noqa: VACUOUS_ASSERTION — the None control is the same proof on the same readable lane, and the arm ends on the refusal's exact words and ref name
        """A lane branch that exists but whose head cannot be read is not a
        branch that lacks the tip: the proof names what it could not read."""
        head = self.commit(self.reviewed, "the author's successor")
        self.git("update-ref", "refs/heads/lane/x", head)
        self.assertEqual(self.proof(), None,
                         "the control: a readable lane that lacks the cure")
        os.unlink(os.path.join(self.common, "objects", head[:2], head[2:]))
        unknown = self.proof()
        self.assertIn("could not be read", unknown)
        self.assertIn("refs/heads/lane/x", unknown)

    def test_every_read_in_this_proof_declares_itself_uncached(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is `assertGreaterEqual(len(declared), 2)` on the SAME list the equality below reads, and it runs FIRST: a spy that observed nothing, or a proof that stopped declaring, cannot reach two declared envs, so `len(declared) == len(seen)` cannot be satisfied by two zeroes
        """THE RULE BEHIND THE ARM ABOVE, asserted where it can be forgotten.

        The arm above measures one verdict. This one measures the property
        that stops the next one: the cross-process fact table ADMITS
        `merge-base --is-ancestor <oid> <oid>` asked under a pinned, scrubbed
        overlay — the projection asks that identical shape and IS answered
        from it — so nothing about this proof's question refuses it. Only the
        caller's declaration does, and a declaration each call must repeat is
        one the next call added here would not carry. That is how the word
        `gitfacts._NEVER` holds came to guard nothing: the proof spells it on
        its `rev-parse` reads and not on the one read the table admits.

        The env is read off the REAL call rather than rebuilt here, so a
        second overlay assembled somewhere in this path is visible."""
        seen = []
        real = vcs.GitVcs.run

        def spy(backend, cwd, *args, **kw):
            seen.append(kw.get("env") or {})
            return real(backend, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "run", autospec=True,
                               side_effect=spy):
            self.assertEqual(self.proof(), None)
        declared = [env for env in seen if gitfacts.UNCACHED in env]
        self.assertGreaterEqual(
            len(declared), 2,
            "the positive control on the same list: this proof really does "
            "read git through the seam, so an empty difference below is not "
            "a spy that observed nothing")
        self.assertEqual(len(declared), len(seen))

    def test_reused_checkout_cannot_prove_a_patch_from_a_foreign_clone(self):
        checkout = os.path.join(self.tmp, "checkout")
        self.git("worktree", "add", "--detach", checkout, self.reviewed)
        row = dict(self.row, repo_root=checkout)
        self.assertEqual(self.proof(row=row), None)
        self.git("worktree", "remove", checkout)
        self.git("clone", "--no-local", "-q", self.repo, checkout,
                 cwd=self.tmp)
        foreign = self.commit(self.reviewed, "foreign cure", cwd=checkout)
        self.assertEqual(self.git("merge-base", "--is-ancestor", self.reviewed,
                                  foreign, cwd=checkout).returncode, 0)
        self.assertNotEqual(self.git("cat-file", "-e", foreign,
                                     check=False).returncode, 0)
        self.assertIn("refusing cross-repository proof",
                      self.proof(foreign, row))
        self.assertEqual(self.proof(), None)

    def test_linked_worktree_and_bound_gitdir_share_the_owner_identity(self):
        checkout = os.path.join(self.tmp, "linked")
        self.git("worktree", "add", "--detach", checkout, self.reviewed)
        common = self.git("rev-parse", "--path-format=absolute",
                          "--git-common-dir", cwd=checkout).stdout.strip()
        self.assertEqual(os.path.realpath(common), self.common)
        linked = dict(self.row, repo_root=checkout)
        self.assertIn("does not descend", self.proof(self.sibling, linked))
        self.assertEqual(self.proof(row=linked), None)
        self.assertEqual(self.proof(row={"repo_id": self.common}), None)

    def test_checkout_without_recorded_identity_cannot_supply_a_proof(self):
        missing = self.proof(row={"repo_root": self.repo})
        self.assertIn("no repository identity (repo_id)", missing)
        gone = self.proof(row=dict(self.row, repo_root=os.path.join(
            self.tmp, "gone")))
        self.assertIn("repository or checkout path is unavailable", gone)
        self.assertNotEqual(missing, gone)
        self.assertIn("repository identity (repo_id) is invalid",
                      self.proof(row=dict(self.row, repo_id="relative")))
        self.assertEqual(self.proof(), None)

    def test_repoint_after_identity_check_still_reads_the_bound_repository(self):
        """Swap a checkout symlink AFTER real Git returned its identity.

        Both object reads must stay in the owner: reject a foreign-only cure,
        accept an owner-only cure. Removing `repo = bound` reverses both arms.
        No Git result is fabricated; the wrapper only times the path swap.
        """
        foreign_repo = os.path.join(self.tmp, "foreign")
        self.git("clone", "--no-local", "-q", self.repo, foreign_repo,
                 cwd=self.tmp)
        foreign = self.commit(self.reviewed, "foreign-only cure", cwd=foreign_repo)
        self.assertEqual(self.git("merge-base", "--is-ancestor", self.reviewed,
                                  foreign, cwd=foreign_repo).returncode, 0)
        self.assertNotEqual(self.git("cat-file", "-e", foreign,
                                     check=False).returncode, 0)
        self.assertNotEqual(self.git("cat-file", "-e", self.patch,
                                     cwd=foreign_repo, check=False).returncode, 0)
        checkout = os.path.join(self.tmp, "selected")
        row = dict(self.row, repo_root=checkout)
        real_run = subprocess.run
        swaps = []

        def repoint(argv, **kwargs):
            result = real_run(argv, **kwargs)
            if argv == ["git", "-C", checkout, "rev-parse",
                        "--path-format=absolute", "--git-common-dir"]:
                self.assertEqual(result.returncode, 0)
                self.assertEqual(os.path.realpath(os.fsdecode(result.stdout).strip()),
                                 self.common)
                os.unlink(checkout)
                os.symlink(foreign_repo, checkout)
                swaps.append(os.fsdecode(result.stdout).strip())
            return result

        def repointed_proof(patch):
            if os.path.lexists(checkout):
                os.unlink(checkout)
            os.symlink(self.repo, checkout)
            with mock.patch.object(dispatches.subprocess, "run", repoint):
                answer = self.proof(patch, row)
            self.assertEqual(os.path.realpath(checkout), foreign_repo)
            return answer

        self.assertIn("does not resolve to a commit", repointed_proof(foreign))
        self.assertEqual(repointed_proof(self.patch), None)
        self.assertEqual(len(swaps), 2)

    def test_replacement_cannot_hide_real_lane_movement(self):
        branch = "refs/heads/lane"
        self.git("update-ref", branch, self.patch)
        row = dict(self.row, ref_branch=branch)
        self.assertEqual(dispatches._lane_movement(row, self.reviewed),
                         (branch, self.patch))
        self.git("replace", self.patch, self.reviewed)
        self.assertEqual(self.git("rev-parse", branch).stdout.strip(), self.patch)
        self.assertEqual(self.git("merge-base", "--is-ancestor", self.reviewed,
                                  self.patch, check=False).returncode, 1)
        self.assertNotIn(self.reviewed,
                         self.git("rev-list", "-g", branch).stdout.splitlines())
        self.assertEqual(dispatches._lane_movement(row, self.reviewed),
                         (branch, self.patch))
        self.assertIsNone(dispatches._lane_movement(row, self.patch))

    def test_legacy_graft_cannot_hide_real_lane_movement(self):
        branch = "refs/heads/lane"
        self.git("update-ref", branch, self.patch)
        row = dict(self.row, ref_branch=branch)
        self.assertEqual(dispatches._lane_movement(row, self.reviewed),
                         (branch, self.patch))
        with open(os.path.join(self.common, "info", "grafts"), "w",
                  encoding="utf-8") as f:
            f.write("%s %s\n" % (self.patch, self.base))
        self.assertEqual(self.git("rev-parse", branch).stdout.strip(), self.patch)
        self.assertEqual(self.git("merge-base", "--is-ancestor", self.reviewed,
                                  self.patch, check=False).returncode, 1)
        self.assertNotIn(self.reviewed,
                         self.git("rev-list", "-g", branch).stdout.splitlines())
        self.assertEqual(dispatches._lane_movement(row, self.reviewed),
                         (branch, self.patch))
        self.assertIsNone(dispatches._lane_movement(row, self.patch))

    def test_symlinked_common_dir_keeps_bare_gitdir_support(self):
        alias = os.path.join(self.tmp, "common-alias")
        os.symlink(self.common, alias)
        row = {"repo_id": alias}
        self.assertEqual(self.proof(row=row), None)
        self.assertIn("does not descend", self.proof(self.sibling, row))

    def test_unknown_canonical_identity_is_not_a_match(self):
        self.assertEqual(self.proof(), None)
        for values in ((None, self.common), (self.common, None), (None, None)):
            with self.subTest(values=values):
                with mock.patch.object(dispatches, "_real", side_effect=values) as real:
                    self.assertIn("identity could not be measured", self.proof())
                self.assertEqual(real.call_args_list,
                                 [mock.call(self.common), mock.call(self.common)])
        self.assertEqual(self.proof(), None)


if __name__ == "__main__":
    unittest.main()
