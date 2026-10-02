#!/usr/bin/env python3
"""The landing protocol is stated ONCE, and it is the one helm runs (task/4050).

THE CONFLICT THIS CLOSES. Two documents told an author and an integrator to do
opposite things with the same reviewed commit. AGENTS.md's authority list said
"never amend or rebase a SHA that was posted for review"; the launch, dispatch
and doc surfaces told the integrator to "rebase the chain" onto current trunk.
An integrator who believed the second sentence rewrites the very commit the
first sentence protects, and the review that was bound to it authorizes a tree
that no longer exists.

WHAT IS ACTUALLY TRUE, read off the code rather than off either sentence:
  * helm/trainblame.py:22 — `helm train` "merges each car with `merge --no-ff`",
    and the reviewed sha stays a parent, so the current train proves a land
    by ancestry. Patch identity handles historical rebases/cherry-picks whose
    original commit objects do not reach trunk;
  * helm/foldcheck.py:346 — "landed — there is nothing to rebase onto and
    nothing left to gate".
So the chain is MERGED, never rebased, and the one thing an author must not do
to a reviewed commit is the thing the old sentence told the integrator to do.

THE RULE, in one breath: work from the recorded base and do not refresh
speculatively; a reviewed commit is immutable, so compose NEW commits on top of
it; the integrator picks the landing order, merges the chain at those exact
shas, and gates that composed tree once; trunk moves by fast-forward. A commit
that was NEVER submitted for review may be rebased freely, because nothing is
bound to it yet.

THIS MODULE IS THE GUARD, not a second copy of the prose: it reads the real
sentences out of the real files and refuses the one that tells a writer to
rebase a chain, and it refuses a doc that promises author-side rebase while the
landing mechanism it describes is a merge. A doc guard that pinned the whole
paragraph would go stale on the next honest edit and teach everyone to delete
guards; this one pins the CLAIM."""
import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: (path, what the file is) for every surface that states the protocol.
SURFACES = (
    ("AGENTS.md", "the authority list a seat reads before its first turn"),
    ("docs/VERBS.md", "the verb reference"),
    ("docs/NEW_AGENT_GUIDE.md", "the onboarding guide"),
    ("helm/harness.py", "the home-drift note an author actually reads"),
    ("helm/foldcheck.py", "the land proof's own statement of how work lands"),
)

#: The code hints a reviewer's patch is delivered through. These are not prose
#: a reader goes looking for: they arrive in an obligation line or a verdict.
HINTS = ("helm/dispatches.py", "helm/dispatches_cli.py")


def _py_sources():
    """EVERY python module under helm/, found rather than listed.

    A HAND-LISTED SET OF SURFACES IS THE DEFECT THIS GUARD EXISTS TO CATCH,
    one level up: a list of five names never reads helm/work/_gc.py, so the
    guard goes green over a document it never looked at. The doors are derived
    from the system being guarded, not from the author's memory of it."""
    out = []
    for base, dirs, files in os.walk(os.path.join(ROOT, "helm")):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.relpath(os.path.join(base, name), ROOT))
    return sorted(out)

#: THE FORBIDDEN CLAIM: a writer being told to rebase work that was reviewed.
#: It is the INSTRUCTION that is wrong, never the word. Sentences that DESCRIBE
#: a rebase are true and must survive — helm's land proofs exist precisely
#: because lanes were rebased in the past ("a cure cherry-picked onto a rebased
#: lane is carried", "a reaper that cannot see it keeps every rebased lane
#: forever"), and a guard that banned the word would delete those proofs and
#: teach everyone to delete guards. So this matches an imperative or a
#: prescriptive auxiliary in FRONT of the verb: "rebase the lane", "rebase onto
#: it", "the integrator rebases the chain", "you should rebase".
REBASE_THE_WORK = re.compile(
    r"(?:\b(?:do not|don't|never|must|should|shall|please|then|now)\s+"
    r"|\b(?:integrator|lane owner|author|owner|reviewer)\s+"
    r"|\brebase\s+(?:the|your|its|this)\b)"
    r"[^.\n]{0,60}?\brebase(?:s|d)?\s+(?:the\s+|your\s+|its\s+|this\s+)?"
    r"(?:chain|lane|reviewed\s+(?:sha|commit|tip))",
    re.I)

#: The imperative spelled at the start of a clause, which the lookbehind above
#: cannot see ("Rebase the lane onto it or cherry-pick it").
REBASE_IMPERATIVE = re.compile(
    r"(?:^|[.;:]\s+|\bthen\s+|\bor\s+)rebase\s+(?:the\s+|your\s+|its\s+)?"
    r"(?:chain|lane)", re.I | re.M)

#: A stale assertion about HOW the work lands is also a protocol instruction,
#: even if it never literally says "rebase the lane". The old verb reference
#: claimed the gate binds a post-rebase tree and a PATCH recipient owes a
#: rebase; neither was detected by the imperative-only guard.
REBASE_DRIFT = re.compile(
    r"\bpost-rebase tree\b|\b(?:lane|chain) owes (?:is )?(?:the )?rebase\b"
    r"|\b(?:helm|our protocol|integrator) lands work rebased\b"
    r"|\bmerges?\s+(?:(?:the|each)\s+)?(?:chain|lane|car)\b"
    r"[^.]{0,150}?\b(?:landed commit carries a different|"
    r"commit on trunk is patch-identical and object-different|"
    r"almost nothing arrives under)\b",
    re.I)

#: The sentence that must exist: a reviewed commit is immutable.
IMMUTABLE = re.compile(
    r"never amend\s+or\s+rebase|reviewed shas are immutable", re.I)


def _text(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _sentences(text):
    """Sentence-ish spans, so a refusal can quote the line that says it."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _forbidden(text):
    # A prohibition is the rule, not a recommendation to rewrite an artifact.
    # Strip only the negated clause, not the rest of the sentence: a later
    # affirmative rebase instruction in the same sentence must still fail.
    text = re.sub(r"\b(?:never|do not|don't|does not)\s+rebase(?:s|d)?\s+"
                  r"(?:(?:the|your|its|this)\s+)?(?:chain|lane)\b",
                  "", text, flags=re.I)
    return (REBASE_THE_WORK.search(text) or REBASE_IMPERATIVE.search(text)
            or REBASE_DRIFT.search(text))


def _literal_strings(text):
    """Python joins adjacent literals across source lines at parse time."""
    return (node.value for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and isinstance(node.value, str))


class TheLandingProtocolIsStatedOnceTest(unittest.TestCase):

    def test_no_surface_tells_a_writer_to_rebase_the_work(self):  # noqa: VACUOUS_ASSERTION — the contract IS an absence, and the rung cannot see through the scan loop; the unconditional control is the assertGreaterEqual on the derived source set below, which runs before any offender can be collected
        offenders = []
        self.assertGreaterEqual(len(_py_sources()), 100,
                                "the scan found almost no modules — an empty "
                                "offender list would mean nothing")
        for name, _what in list(SURFACES) + [(h, "hint") for h in HINTS] \
                + [(p, "module") for p in _py_sources()]:
            text = _text(name)
            self.assertTrue(text.strip(), "%s read back empty" % name)
            spans = _sentences(text)
            if name.endswith(".py"):
                spans += [s for literal in _literal_strings(text)
                          for s in _sentences(literal)]
            for s in spans:
                if _forbidden(s):
                    offenders.append("%s: %s" % (name, " ".join(s.split())))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_counterfactual_old_instructions_fail_but_history_is_allowed(self):
        # Real claims previously in the changed docs and source. These
        # controls fail if the scanner becomes a vacuous literal-word search.
        old = (
            "the integrator rebases the chain onto current trunk",
            "rebase the lane onto it or cherry-pick it",
            "(ii) gate binds the post-rebase tree",
            "what the lane owes is the rebase",
            "helm lands work REBASED, so the commit has another sha",
            "the integrator merges the chain onto trunk, so the landed commit carries a different object id",
        )
        for claim in old:
            with self.subTest(claim=claim):
                self.assertTrue(_forbidden(claim), claim)
        for history in (
                "a cure cherry-picked onto a rebased lane is carried",
                "the 2026 rebase carried content under different shas",
                "never amend or rebase a SHA posted for review",
                "never rebase the lane after review"):
            with self.subTest(history=history):
                self.assertFalse(_forbidden(history), history)
        # The source scan must parse joined string literals rather than
        # missing a production hint split over two quoted lines.
        joined = 'note = ("the integrator re" "bases the chain")'
        self.assertTrue(any(_forbidden(s) for s in _literal_strings(joined)))

    def test_the_protocol_vocabulary_really_is_in_the_tree(self):
        """The arm above is only meaningful if the sentences it forbids would
        have been FOUND: the protocol's own words must still be present, or a
        tree that dropped the subject entirely would read as clean."""
        both = 0
        for name, _what in list(SURFACES) + [(h, "hint") for h in HINTS]:
            if "rebase" in _text(name).lower():
                both += 1
        self.assertGreaterEqual(both, 4, "the protocol surfaces lost their "
                                "own vocabulary — check they still exist")

    def test_every_module_in_the_tree_really_was_scanned(self):
        """The set is DERIVED, not remembered. A guard that quietly stopped
        finding files would pass the arm above while looking at nothing."""
        found = _py_sources()
        self.assertGreater(len(found), 100, found[:5])
        self.assertIn("helm/work/_gc.py", found)
        self.assertIn("helm/harness.py", found)
        self.assertIn("helm/foldcheck.py", found)

    def test_the_authority_list_still_forbids_rewriting_a_reviewed_commit(self):
        # THE POSITIVE CONTROL for the arm above: this file really does carry
        # the rule, so "no rebase-the-chain sentence" is a statement about a
        # rule that EXISTS rather than about a file that failed to load.
        text = _text("AGENTS.md")
        self.assertTrue(IMMUTABLE.search(text), text[:200])
        self.assertIn("posted for review", text)

    def test_the_landing_mechanism_the_docs_name_is_a_merge(self):
        """A doc that promises author-side rebasing while the tool merges is
        the conflict in one sentence. The tool is the authority: `helm train`
        merges each car at its exact sha (`merge --no-ff`), which is why a
        land is provable by ancestry."""
        import helm.trainblame as trainblame
        self.assertIn("merge --no-ff", trainblame.__doc__ or "")
        merge = _text("helm/landwindow.py")
        self.assertIn('"merge", "--no-ff"', merge)
        self.assertIn("got[1:] != [before, tip]", merge,
                      "train must verify its second parent is the reviewed tip")
        verbs = _text("docs/VERBS.md")
        self.assertIn("merge --no-ff", verbs)

    def test_a_never_submitted_commit_is_still_rebasable(self):
        """The rule is not "never rebase". A working commit nobody reviewed
        has nothing bound to it, and forbidding its rebase would make the
        immutable-artifact rule into a rule against tidying your own branch.
        AGENTS.md must keep saying which commits the protection covers."""
        text = _text("AGENTS.md")
        self.assertIn("never amend", text)
        self.assertIn("posted for review", text)


if __name__ == "__main__":
    unittest.main()
