#!/usr/bin/env python3
"""`helm lr refs` — the recorded proofs that no longer resolve.

WHY THIS EXISTS: helm binds its proofs to git SHAs, and a SHA is
content-addressed over HISTORY. Rewrite history — git filter-repo, a
squash-root, a rebased trunk — and every stored ref dangles at once. A land
request still names its reviewed tip, a verdict still names what it attested,
and not one of them can be verified again. Nothing detected that, so the first
symptom would have been a gate quietly unable to prove something it had
already proven.

THE TESTS ARE MOSTLY NEGATIVE CONTROLS, because writing this audit produced
THREE false-positive classes in a row, none caught by reading the code and all
caught by disbelieving a number:

  * iterating a {id: row} mapping yields id STRINGS — the audit inspected
    nothing and would have reported a clean bill over zero rows;
  * `cat-file --batch-check` echoes the RESOLVED FULL sha for a valid
    abbreviation and only echoes the input verbatim for `<input> missing`, so
    keying results by name lost every abbreviated ref that resolved — 119
    false dangles on a repository whose history had never been rewritten;
  * `delivery_ref` is delivery EVIDENCE (a chat row id, 12 hex) and is
    indistinguishable by shape from an abbreviated commit — 78 more.

So the load-bearing assertion is that a repository with intact history reports
ZERO, paired with a positive control proving the audit can still say otherwise.
A zero that cannot detect a real dangle is worth nothing.
"""
import os
import subprocess
import tempfile
import shutil
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import landreq  # noqa: E402

IMPOSSIBLE = "deadbeef" * 5          # 40 hex, cannot exist


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


class RefsAuditTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="helm-test-refs-")
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        open(os.path.join(self.repo, "f"), "w").write("x")
        _git(self.repo, "add", "f")
        _git(self.repo, "commit", "-qm", "one")
        self.sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _audit(self, lrs=None, drows=None):
        with mock.patch.object(landreq, "project", return_value=(lrs or {}, None)), \
                mock.patch.object(landreq.dispatches, "rows",
                                  return_value=(drows or {})):
            return landreq.dangling_refs(repo=self.repo)

    def test_a_resolvable_ref_is_never_reported_dangling(self):
        r, err = self._audit(drows={"d1": {"id": "d1", "reviewed_tip": self.sha}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [])
        self.assertEqual(r["unknown"], 0)

    def test_an_ABBREVIATED_resolvable_ref_is_not_dangling(self):
        """batch-check answers an abbreviation with the RESOLVED FULL sha, so a
        name-keyed lookup loses it. 119 false dangles came from exactly this."""
        r, err = self._audit(drows={"d1": {"id": "d1", "tip": self.sha[:12]}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [], "an abbreviation that resolves is not dangling")

    def test_an_impossible_commit_IS_reported(self):
        """The positive control. Without it a zero proves nothing."""
        r, err = self._audit(drows={"d1": {"id": "d1", "reviewed_tip": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1)
        self.assertEqual(r["dangling"][0]["field"], "reviewed_tip")
        self.assertEqual(r["by_field"], {"reviewed_tip": 1})

    def test_delivery_evidence_is_not_audited_as_a_commit(self):
        """delivery_ref holds a chat row id — 12 hex, shaped exactly like an
        abbreviated commit, and it will NEVER resolve. Auditing it produced 78
        permanent false dangles."""
        r, err = self._audit(drows={"d1": {"id": "d1", "delivery_ref": "983c8debb093"}})
        self.assertIsNone(err)
        self.assertEqual(r["dangling"], [])
        self.assertEqual(r["checked"], 0, "it must not even be collected")

    def test_rows_are_read_from_the_mapping_VALUES(self):
        """Both ledgers hand back {id: row}. Walking the mapping yields id
        strings, which have no fields — the audit would inspect nothing and
        report a clean bill over zero rows."""
        r, err = self._audit(drows={"abcdef01": {"id": "abcdef01",
                                                 "reviewed_tip": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1,
                         "the row's fields must be inspected, not its key")

    def test_lr_rows_are_audited_too_not_only_dispatches(self):
        r, err = self._audit(lrs={"x": {"id": "x", "review_sha": IMPOSSIBLE}})
        self.assertIsNone(err)
        self.assertEqual(len(r["dangling"]), 1)
        self.assertEqual(r["dangling"][0]["source"], "lr")

    def test_an_unreadable_ledger_reports_a_note_never_a_clean_bill(self):
        """A pass whose input was missing reports the opposite of the truth."""
        with mock.patch.object(landreq, "project", return_value=({}, "ledger gone")), \
                mock.patch.object(landreq.dispatches, "rows", return_value={}):
            r, err = landreq.dangling_refs(repo=self.repo)
        self.assertIsNone(err)
        self.assertIn("nothing was checked", r["note"])
        self.assertEqual(r["checked"], 0)

    def test_git_that_cannot_be_asked_is_UNKNOWN_not_resolved(self):
        """Tri-state: a repo we cannot query must never read as 'all resolve'."""
        with mock.patch.object(landreq, "_git", return_value=None):
            out = landreq._batch_exists("/nonexistent.git", [self.sha])
        self.assertIsNone(out, "unaskable git is UNKNOWN, never True")

    def test_a_non_repo_path_is_refused(self):
        r, err = landreq.dangling_refs(repo=tempfile.gettempdir() + "/definitely-not-a-repo")
        self.assertIsNone(r)
        self.assertIn("not a readable Git working tree", err)



class RefReadArgvSpellingTest(unittest.TestCase):
    """Every ref read in landreq goes through ONE argv, so the next one does too.

    THE DEFECT THIS CLOSES IS THE POPULATION, NOT THE MEMBERS. `rev-parse
    --verify --quiet <ref>` without `--end-of-options` reports an EXISTING
    option-shaped ref as ABSENT — rc 1 and empty, a silent false negative
    rather than a misread. task/1060 counted eleven such sites; by the time it
    was picked up there were fifteen, and a live lane was adding a sixteenth.
    Nothing was preventing number sixteen: the PEEL half had a shared constant
    and four callers inheriting it, and the plain-resolve half had none, so
    every new caller chose its own spelling. Respelling N sites is correct on
    the day it lands and wrong the week after; this arm is the part that lasts.

    SCOPE, STATED BECAUSE A SCANNER'S REACH IS NARROWER THAN THE PROPERTY,
    AND STATED ON BOTH AXES because a reader who sees one scope declared
    assumes the other was considered. BY FILE: this reads helm/landreq.py
    only, so a hand-rolled spelling in another module is invisible here. BY
    CONSTRUCT: it reads every string constant in a statement regardless of
    which node carries it, so there is no shape of literal argv it cannot
    see — but it reads LITERALS IN ONE STATEMENT, and two things sit outside
    that. Tokens assembled at RUN TIME ("rev" + "-parse", a name imported
    from elsewhere, a format string) are outside it and always will be. So is
    an argv whose literals are SPLIT ACROSS STATEMENTS — `_A = ("rev-parse",)`
    then `_C = _A + ("--verify", "--quiet")` — because no single statement
    spells both tokens. That second one is deliberately not chased: a reader
    who has to break the argv across two statements to get past this arm has
    already said what they are doing.
    """

    def _handrolled(self, src):
        """Statements that spell a rev-parse ref-read argv, EXCEPT _REF_ARGV's own.

        Deliberately AST and not text: a source-text scan is defeated by
        reformatting, and the census for this row was wrong twice because a
        grep cannot see tokens split across lines or spelled `-q`.

        AND DELIBERATELY NOT A LIST OF NODE TYPES. A scan that asks `is it a
        Call, and are the tokens in node.args or node.keywords` or `is it an
        Assign, and is node.value a Tuple or a List` is making a guess about
        WHERE a caller will put the tokens, and every guess the caller does
        not share is a silent clean verdict about a shape nobody checked. The
        sibling arm below names eight such shapes and none of them is exotic;
        `f(g, *("rev-parse", "--verify", "--quiet"), ref)` is simply how a
        person splats a literal argv, and it is a Starred node where such a
        scan looks for a Tuple.

        So this asks the question the row is actually about — DOES THIS
        STATEMENT SPELL THE TOKENS — and reads every string constant beneath
        the statement without caring which node carries it. There is nothing
        left to enumerate, so there is no shape left to miss.

        SCOPED TO THE STATEMENT, NOT THE SUBTREE, and that is the whole of the
        design. Walking a statement wholesale would make a FunctionDef carry
        every literal in its body, so one hand-rolled call would flag the
        function containing it, the class containing that, and the module —
        true, useless, and impossible to act on. Descent stops at the first
        nested statement because a nested statement is its own unit and gets
        its own verdict.
        """
        import ast as _ast

        def strings_of(stmt):
            out, stack = [], list(_ast.iter_child_nodes(stmt))
            while stack:
                n = stack.pop()
                if isinstance(n, _ast.stmt):    # its own unit, its own verdict
                    continue
                if isinstance(n, _ast.Constant) and isinstance(n.value, str):
                    out.append(n.value)
                stack.extend(_ast.iter_child_nodes(n))
            return out

        def targets(stmt):
            # Assign carries `targets`, AnnAssign and AugAssign carry `target`.
            # Reading both by NAME rather than by node type is the same move as
            # the scan above: the exemption must not depend on which spelling
            # of assignment the definition happens to use.
            #
            # A TUPLE TARGET CARRIES NO `.id`, so `_REF_ARGV, _X = (...), 1`
            # is FLAGGED rather than exempted. Left that way on purpose: the
            # arm below deliberately treats a tuple-target assignment as a
            # RIVAL shape, so exempting one here would make the exemption and
            # the rival rule disagree about the same node. Nobody defines the
            # constant that way, and a definition that reached for that
            # spelling would be worth a second look anyway.
            out = []
            for t in (list(getattr(stmt, "targets", []))
                      + [getattr(stmt, "target", None)]):
                if t is not None:
                    out.append(getattr(t, "id", None))
            return out

        hits = []
        for stmt in (n for n in _ast.walk(_ast.parse(src))
                     if isinstance(n, _ast.stmt)):
            vals = strings_of(stmt)
            if "rev-parse" not in vals:
                continue
            if not ("--verify" in vals or "-q" in vals):
                continue
            if "_REF_ARGV" in targets(stmt):
                continue
            hits.append(stmt.lineno)
        return sorted(hits)

    def test_the_scanner_sees_every_shape_a_caller_can_write(self):
        # THE SHAPES THE REAL MODULE AND ITS NEIGHBOURS ACTUALLY USE. This
        # table is no longer the scanner's coverage claim — the scan
        # enumerates nothing, so coverage is not a list any more and the
        # sibling arm records what enumerating it cost. These stay because
        # they are the shapes a reader of THIS file will recognise: a
        # subprocess LIST is how a caller writes a git call when they are not
        # reaching for this module's helper, and a RIVAL CONSTANT is not a
        # near-miss of this row's defect but the defect itself.
        # UNCONDITIONAL, ahead of the table: a positive control inside the
        # loop does not run if the table is ever emptied, and the arm would
        # then pass by iterating nothing.
        plainly_hand_rolled = 'f(g, "rev-parse", "--verify", ref)'
        self.assertTrue(
            self._handrolled(plainly_hand_rolled),
            "the scanner sees nothing at all, so every case below is vacuous")
        for label, snippet in (
                ("positional splat",
                 'f(g, "rev-parse", "--verify", "--quiet", ref)'),
                ("tuple handed to a memo-key builder",
                 'k(r, ("rev-parse", "--verify", "--quiet", n))'),
                ("-q, the short flag with the order reversed",
                 'f(g, "rev-parse", "-q", "--verify", ref)'),
                ("keyword argument",
                 'f(g, argv=("rev-parse", "--verify", "--quiet", ref))'),
                ("subprocess LIST",
                 'run(["git", "rev-parse", "--verify", "--quiet", ref])'),
                ("a RIVAL CONSTANT beside _REF_ARGV",
                 '_MY_ARGV = ("rev-parse", "--verify", "--quiet")'),
        ):
            self.assertTrue(self._handrolled(snippet),
                            "the scanner cannot see %s, so its clean verdict "
                            "on landreq.py does not cover that shape" % label)

    def test_the_eight_shapes_a_node_type_list_could_not_see(self):
        # THE SHAPES A NODE-TYPE LIST CANNOT REACH. Each names a place a
        # caller can put the tokens that is neither `node.args` on a Call nor
        # a Tuple/List on an Assign, which is the natural pair of clauses to
        # reach for. They are named cases rather than a count because a count
        # cannot tell a later reader WHICH guess a node-type scan would make
        # wrong.
        #
        # This is not a list to maintain. The scan enumerates nothing, so the
        # table cannot go stale by a caller inventing a ninth shape — it
        # documents what an enumeration would cost, and it stays green for
        # free.
        #
        # UNCONDITIONAL, AHEAD OF THE TABLE, and bound to a NAME so the rung
        # can see it: a positive control that lives inside the loop does not
        # run if the table is ever emptied, and this arm would then pass by
        # iterating nothing — the failure mode it exists to detect.
        plainly_hand_rolled = 'f(g, "rev-parse", "--verify", ref)'
        self.assertTrue(
            self._handrolled(plainly_hand_rolled),
            "the scanner sees nothing at all, so every shape below is vacuous")
        for label, snippet in (
                ("an ANNOTATED rival constant — AnnAssign, not Assign",
                 '_MY: tuple = ("rev-parse", "--verify", "--quiet")'),
                ("a STARRED inline tuple — Starred, not Tuple, in node.args",
                 'f(g, *("rev-parse", "--verify", "--quiet"), ref)'),
                ("a DEFAULT ARGUMENT value — arguments, not a Call at all",
                 'def f(g, ref, a=("rev-parse", "--verify", "--quiet")):\n'
                 '    return a'),
                ("a RETURNED literal — Return, not Assign",
                 'def f():\n    return ("rev-parse", "--verify", "--quiet")'),
                ("a DICT value — Dict, which the one-level unwrap skipped",
                 '_A = {"ref": ("rev-parse", "--verify", "--quiet")}'),
                ("a BinOp concatenation — neither operand is the whole argv",
                 '_M = ("rev-parse", "--verify") + ("--quiet",)'),
                ("a TUPLE-TARGET assignment — targets carry no .id",
                 '_A, _B = ("rev-parse", "--verify", "--quiet"), 1'),
                ("a list nested one level inside a list",
                 'run([["git", "rev-parse", "--verify", ref]])'),
        ):
            self.assertTrue(
                self._handrolled(snippet),
                "the scanner cannot see %s, so its clean verdict on "
                "landreq.py does not cover that shape" % label)

    def test_the_scan_stops_at_the_nested_statement(self):
        # THE ONE PROPERTY THE WHOLE-OBJECT READ COULD HAVE COST US, and the
        # reason descent stops at a nested statement. Walking the subtree
        # wholesale would make the enclosing def and the enclosing class each
        # carry the literals of the call inside them, so a single hand-rolled
        # call would report three hits at three line numbers — true, useless,
        # and impossible to act on, because two of the three name a line the
        # author must not change.
        nested = ('class C:\n'
                  '    def f(self, g, ref):\n'
                  '        return _git(g, "rev-parse", "--verify", ref)\n')
        hits = self._handrolled(nested)
        self.assertEqual(
            1, len(hits),
            "a hand-rolled call reported %d hits; the enclosing def and class "
            "are carrying their body's literals, which makes every finding "
            "point at a line the author cannot fix" % len(hits))
        self.assertEqual(
            3, hits[0],
            "the hit must name the CALL's line, not the def or the class")

    def test_the_definition_is_excluded_by_name_not_by_luck(self):
        # MINIMAL PAIR ON THE NAME. _REF_ARGV's own definition carries exactly
        # the literals being hunted, so it must be exempted deliberately. The
        # rival is the same text with only the NAME changed, which is both the
        # positive control and the real hazard: a second constant beside the
        # first is the failure this row exists to prevent.
        real = ('_REF_ARGV = ("rev-parse", "--verify", "--quiet",'
                ' "--end-of-options")')
        rival = real.replace("_REF_ARGV", "_MY_ARGV")
        self.assertTrue(
            self._handrolled(rival),
            "a rival constant is not flagged, so the exemption below is not a "
            "name check — it is the scanner failing to see Assign at all")
        self.assertEqual([], self._handrolled(real))

    def test_a_rev_parse_that_reads_no_ref_is_left_alone(self):
        # MINIMAL PAIR ON THE FLAG. Only ref READS are in scope; a rev-parse
        # asking for a path or a git-dir is a different question and must not
        # be refused.
        reads_a_ref = 'f(g, "rev-parse", "--verify", ref)'
        reads_no_ref = reads_a_ref.replace('"--verify", ref', '"--show-toplevel"')
        self.assertTrue(
            self._handrolled(reads_a_ref),
            "the scanner is blind, so its silence on the non-ref form says "
            "nothing about scoping")
        self.assertEqual([], self._handrolled(reads_no_ref))

    def test_the_scanner_can_see_a_handrolled_call(self):
        # MUST-HIT. A zero from a blind scanner is worth nothing, and this
        # file's own history is three false-clean classes in a row. Both
        # shapes the real code uses are seeded: the splatted call and the
        # tuple handed to a memo-key builder.
        seeded = self._handrolled(
            'def f(g, ref):\n'
            '    return _git(g, "rev-parse", "--verify", "--quiet", ref)\n')
        self.assertEqual(1, len(seeded),
                         "the scanner cannot see a hand-rolled splatted call, "
                         "so its verdict on the real module means nothing")
        seeded_tuple = self._handrolled(
            'def f(r, name):\n'
            '    return _git_key(r, ("rev-parse", "--verify", "--quiet", name))\n')
        self.assertEqual(1, len(seeded_tuple),
                         "the scanner misses the TUPLE form, which is exactly "
                         "the shape the projscope eviction key is built in")

    def test_the_scanner_passes_a_call_that_uses_the_constant(self):
        # MUST-MISS, with its positive control IN THE SAME ARM rather than in
        # a sibling: a control one method away can be deleted or skipped on
        # its own, and then this assertion goes vacuous with nothing to say so.
        # A MINIMAL PAIR: the two sources differ in exactly the token under
        # test, so a silence on the constant form cannot be explained by
        # anything else about the snippet. The hand-rolled arm is DERIVED from
        # the constant one for that reason -- two independently written
        # snippets would differ in ways neither assertion controls.
        using_constant = ('def f(g, ref):\n'
                          '    return _git(g, *_REF_ARGV, ref)\n')
        hand_rolled = using_constant.replace(
            "*_REF_ARGV", '"rev-parse", "--verify", "--quiet"')
        self.assertTrue(
            self._handrolled(hand_rolled),
            "the scanner cannot see a hand-rolled call, so its silence on the "
            "constant form is not evidence of anything")
        self.assertEqual([], self._handrolled(using_constant))

    def test_no_ref_read_in_landreq_hand_rolls_its_argv(self):
        src = open(landreq.__file__, encoding="utf-8").read()
        # THE UNCONDITIONAL POSITIVE CONTROL, on the SAME observable and the
        # SAME source text: inject one hand-rolled call into the real module's
        # source and require the scan to find exactly it. An empty result from
        # a scanner that cannot parse this file, or that silently walked
        # nothing, is indistinguishable from a clean module without this.
        seeded = src + (
            '\n\ndef _vacuity_control(g, ref):\n'
            '    return _git(g, "rev-parse", "--verify", "--quiet", ref)\n')
        self.assertEqual(
            1, len(self._handrolled(seeded)),
            "the scan found no hand-rolled call in landreq.py WITH one "
            "appended, so it cannot see them at all and the clean verdict "
            "below is vacuous")
        self.assertEqual(
            [], self._handrolled(src),
            "a ref read in landreq.py builds its own rev-parse argv instead "
            "of using _REF_ARGV. Without --end-of-options an EXISTING "
            "option-shaped ref reads as ABSENT (rc 1, empty), and the caller "
            "cannot tell that from a ref that is genuinely gone. Use "
            "*_REF_ARGV, or _REF_ARGV + (ref,) where a tuple is wanted.")

    def test_the_constant_keeps_the_order_that_makes_it_work(self):
        # The ORDER is the whole mechanism and it is not cosmetic: placed
        # AFTER --end-of-options, --quiet is itself a revision and the call
        # dies 128. An edit that sorted these tokens would leave every caller
        # above still "using the constant" and break all of them at once.
        self.assertEqual(
            ("rev-parse", "--verify", "--quiet", "--end-of-options"),
            landreq._REF_ARGV)
if __name__ == "__main__":
    unittest.main()
