#!/usr/bin/env python3
"""The cross-process fact table — `helm.gitfacts`.

WHAT THESE ARMS ARE DEFENDING. This cache has no ttl, no stamp and no
invalidation, so every safety property it has comes from its ADMISSION RULE:
only a question whose every operand — bare word or option value — is a full
object id, asked with the history rewriters pinned off AND the repository
selection scrubbed, whose exit code is an ANSWER for that particular verb, and
keyed to the git that produced it. Every arm below makes one clause of that
rule false and asserts the refusal, and each is paired with a positive control
differing in exactly that clause — a refusal arm that passes because the cache
is broken everywhere proves nothing.

AND A REFUSAL ARM MUST REFUSE FOR ITS OWN REASON. The first build of this
module was defended by arms that all passed while four defects were live,
because each named a clause and then exercised a question the VERB table would
have refused anyway. So every admission arm here is written on a verb that IS
in `_ANSWERS`: the clause under test is then the only thing that can be doing
the refusing.

WHAT THE FIXTURES ARE. The classes at the bottom build REAL repositories and
ask through the REAL seam, because the defects this module was cured of are
not visible to an invented argv: that a conflicted `merge-tree` writes objects
and prints a tree id, that `diff --raw` is reordered by ambient config, and
that an object can stop existing under a path that did not move, are all facts
about git that a fabricated stdout cannot contradict.

NO ARM SHARES A TABLE. A durable cache is a cross-test channel by construction:
an arm that writes an entry another arm reads makes the second a replay of the
first, and the order dependency is invisible until a single method is run alone.
Each test therefore gets its own root.
"""
import contextlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helm import dispatches, doctor, foldckpt, gc, gitfacts, projscope, vcs

# The overlay every helm read path runs under (`rowworld._scrubbed_env`): the
# graft file pinned at /dev/null, replacement objects switched off, and every
# repository-selection variable REMOVED — a `None` value is the seam's spelling
# for "unset this one".
SCRUB = {name: None for name in dispatches._GIT_SELECTION_ENV}
PINNED = dict(SCRUB, GIT_GRAFT_FILE=os.devnull, GIT_NO_REPLACE_OBJECTS="1")
# The same history view WITHOUT the selection scrub — `landreq._object_view`'s
# overlay, which pins both rewriters and leaves GIT_DIR alone.
REWRITERS_ONLY = {"GIT_GRAFT_FILE": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1"}

A = "a" * 40
B = "b" * 40


def _git(repo, *args, **kw):
    """One real git call against a fixture repository, with the ambient user
    config pinned away so a developer's `~/.gitconfig` cannot author the
    fixture."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull)
    env.update(kw.pop("env", {}))
    out = subprocess.run(("git",) + args, cwd=repo, capture_output=True,
                         env=env, **kw)
    return out.stdout.decode().strip()


class RootTest(unittest.TestCase):
    """Every arm on its own table (see the module docstring)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-gitfacts-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patch = mock.patch.object(gitfacts, "_root", lambda: self.root)
        patch.start()
        self.addCleanup(patch.stop)
        # TWO REAL REPOSITORIES, because the admission rule reads the
        # FILESYSTEM: it refuses a repository it cannot prove is unshallowed,
        # so an invented path is refused before any clause under test gets a
        # chance to refuse it, and every arm below would pass for that reason.
        self.here = self.empty_repo()
        self.there = self.empty_repo()

    def empty_repo(self):
        path = tempfile.mkdtemp(prefix="helm-test-gitfacts-repo-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        _git(path, "init", "-q", "-b", "main")
        return path

    def entries(self):
        return sum(len(f) for _d, _s, f in os.walk(self.root))

    def admit(self):
        """Offer ONE known-good question to this arm's own table.

        Every refusal arm calls this and then asserts the count went UP, on
        the same observable it asserted was zero — because "nothing was
        stored" is equally what a table that is broken, misrooted or switched
        off reports, and a refusal arm passing for that reason proves nothing
        about the rule it names. The assertion stays in the arm rather than
        here so the positive and the absence constrain one observable in one
        method, which is the only shape that can fail together."""
        gitfacts.record(self.there, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")


class AdmissionTest(RootTest):
    """WHICH QUESTIONS MAY BE STORED AT ALL."""

    def test_a_question_whose_operands_are_object_ids_round_trips(self):
        """The positive control every refusal below is measured against."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        self.assertEqual(
            gitfacts.lookup(self.here, ("merge-base", "--is-ancestor", A, B), PINNED),
            (1, b"", b""),
            "an immutable question, pinned, scrubbed, with an exit code that "
            "IS the answer, must be served from the table")

    def test_a_ref_operand_of_an_admitted_verb_is_never_stored(self):
        """A ref is a fact about NOW. `origin/main` answers differently after
        every fetch, and the whole basis for caching without invalidation is
        that the answer cannot move.

        ASKED ON AN ADMITTED VERB on purpose: the same clause written against
        `rev-parse` would pass on a table that has no `rev-parse` row at all,
        and prove nothing about refs."""
        argv = ("cherry", "refs/remotes/origin/main", A)
        gitfacts.record(self.here, argv, PINNED, 0, b"+ %s\n" % A.encode(), b"")
        self.assertEqual(self.entries(), 0)
        self.assertIsNone(gitfacts.lookup(self.here, argv, PINNED))
        self.admit()
        self.assertEqual(
            gitfacts.lookup(self.there, ("cherry", A, B), PINNED),
            (0, b"+ x\n", b""),
            "control: the same table DOES serve an admitted answer on the "
            "same verb, so the miss above is this clause refusing and not a "
            "dead table")
        self.assertEqual(self.entries(), 1,
                         "and the COUNT moved too, so the zero above is this "
                         "clause refusing and not a table that cannot write")

    def test_a_verb_with_no_operand_is_never_stored(self):
        """A bare verb names no object because it asks about the CHECKOUT or
        about HEAD — the same class as a ref, one step further out. `cherry`
        with no argument reads `HEAD`'s upstream, which is a fact about now."""
        gitfacts.record(self.here, ("cherry",), PINNED, 0, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer on the same verb, so the zero above is this "
                         "clause refusing and not a table that cannot write")

    def test_an_abbreviated_id_is_not_an_object_id(self):
        """A short id is a PREFIX, and a prefix resolves against the object
        store as it stands — a repository that later grows a second object
        with that prefix makes the same string a different question."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A[:12], B),
                        PINNED, 1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")

    def test_an_end_of_options_read_is_refused_by_that_word(self):
        """WHAT `_NEVER` COVERS, and this arm is named for exactly that.

        `--end-of-options` is NOT what keeps the patch-tip proof off this
        table, so this arm does not claim it is. The proof spells that word on
        its `rev-parse` reads, which this table refuses for their verb whatever
        the word does, and not on the `merge-base --is-ancestor` it asks — the
        one read the table admits. The argv below is therefore an argv no
        caller in this tree emits, and it proves one thing only: the word
        itself refuses. The rule that covers the proof is `_declined`, and the
        two arms after this one measure it on the overlay the proof builds.
        """
        argv = ("cherry", "--end-of-options", A, B)
        gitfacts.record(self.here, argv, PINNED, 0, b"+ x\n", b"")
        self.assertEqual(self.entries(), 0)
        # CONTROL: the same question WITHOUT that word is admitted, so the
        # refusal above is attributable to the word and not to the shape.
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(self.entries(), 1)

    def test_a_read_declared_uncached_is_never_stored(self):
        """The declaration a re-measuring caller puts on its overlay.

        THE CONTROL DIFFERS IN THE CLAUSE AND IN NOTHING ELSE: the same
        repository, the same argv and the SAME overlay, stored once the clause
        is switched off. A control that dropped the key instead would differ
        in the key too, and the zero would be as well explained by a miss."""
        argv = ("merge-base", "--is-ancestor", A, B)
        declared = dict(PINNED, **{gitfacts.UNCACHED: "1"})
        gitfacts.record(self.here, argv, declared, 0, b"", b"")
        self.assertEqual(self.entries(), 0)
        with mock.patch.object(gitfacts, "_declined", lambda env: False):
            gitfacts.record(self.here, argv, declared, 0, b"", b"")
        self.assertEqual(self.entries(), 1,
                         "control: that identical question under that "
                         "identical overlay IS stored with this clause off, "
                         "so the zero above is the declaration and not the "
                         "verb, the overlay or a table that cannot write")

    def test_a_read_declared_uncached_is_never_served(self):
        """BOTH DOORS, because refusing to write is not refusing to read.

        An entry written before a caller started declaring itself — or by any
        other process sharing the root — is still on disk, so the serve door
        has to refuse on its own account."""
        argv = ("merge-base", "--is-ancestor", A, B)
        declared = dict(PINNED, **{gitfacts.UNCACHED: "1"})
        with mock.patch.object(gitfacts, "_declined", lambda env: False):
            gitfacts.record(self.here, argv, declared, 0, b"out\n", b"")
            self.assertEqual(
                gitfacts.lookup(self.here, argv, declared), (0, b"out\n", b""),
                "control: the entry for THIS key exists and IS served with "
                "the clause off, so the miss below is the declaration and not "
                "an entry that was never written or keyed elsewhere")
        self.assertIsNone(gitfacts.lookup(self.here, argv, declared))
        self.assertEqual(self.entries(), 1,
                         "and the refusal is a refusal to ANSWER: the entry "
                         "it declined to serve is still on disk")


class OptionValueTest(RootTest):
    """AN OPTION'S VALUE IS AN OPERAND — the clause that reading only bare
    words missed entirely."""

    def test_a_ref_glued_to_an_option_is_refused_and_an_id_is_not(self):  # noqa: VACUOUS_ASSERTION — the must-hit is the same gate on the identical argv with only the option VALUE changed, asserted EQUAL to the admitted codes; two calls are two producers by construction
        """THE GATE, MEASURED APART FROM THE TABLE.

        `merge-tree --write-tree --merge-base=<committish> A B` is the argv
        this tree actually issues, and `--merge-base=refs/remotes/origin/main`
        is a REF that moves under a key that would never notice. The verb is
        admitted again in exactly this shape (`_VIEWED`, task/3056, with the
        view in its key), so the gate is the only thing left to refuse the
        ref, and the same argv carrying an object id instead is the must-hit
        that proves the gate admits the shape it is supposed to. (Before
        task/3056 the verb was patched back into `_ANSWERS` for this arm.)"""
        ref = ("merge-tree", "--write-tree",
               "--merge-base=refs/remotes/origin/main", A, B)
        oid = ("merge-tree", "--write-tree", "--merge-base=" + A, A, B)
        self.assertIsNone(gitfacts._answers_for(list(ref)),
                          "an option value naming a ref is an operand "
                          "about NOW")
        self.assertEqual(gitfacts._answers_for(list(oid)), gitfacts._VIEWED[1],
                         "must-hit: the identical argv whose option value "
                         "IS an object id is admitted, so the refusal "
                         "above is the VALUE and not the option, the verb "
                         "or the shape")

    def test_a_non_id_option_value_on_an_admitted_verb_is_refused(self):
        """`cherry --abbrev=7 A B` is a real spelling of a real git option
        whose value is not an object id. The rule is a refusal of everything
        that is not an id rather than a list of the options that take a name,
        because that list is per-verb and moves between git releases."""
        gitfacts.record(self.here, ("cherry", "--abbrev=7", A, B), PINNED,
                        0, b"+ x\n", b"")
        self.assertEqual(self.entries(), 0)
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(self.entries(), 1,
                         "control: the same question without the glued value "
                         "is stored, so the zero above is the value and not "
                         "the verb")

    def test_a_bare_flag_is_still_admitted(self):
        """The rule reads VALUES, not hyphens: the argv spelling of the
        replacement pin carries no `=` and must survive it."""
        argv = ("--no-replace-objects", "merge-base", "--is-ancestor", A, B)
        self.assertEqual(gitfacts._answers_for(list(argv)), (0, 1))


class HistoryViewTest(RootTest):
    """A REWRITER DEFEATS THE ENTIRE ARGUMENT, so it is a precondition."""

    def test_an_unpinned_read_is_never_stored(self):
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), None,
                        1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")

    def test_replacement_objects_left_on_is_never_stored(self):
        """The graft file alone is not enough: `refs/replace/<oid>` swaps one
        object for another at every lookup, which is exactly the property
        being cached."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B),
                        dict(SCRUB, GIT_GRAFT_FILE=os.devnull), 1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")

    def test_a_graft_file_left_on_is_never_stored(self):
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B),
                        dict(SCRUB, GIT_NO_REPLACE_OBJECTS="1"), 1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")

    def test_the_argv_spelling_of_no_replace_also_counts(self):
        """`landreq._object_view` pins replacement on the ARGV; refusing that
        spelling would exclude a caller that is just as safe as the env one —
        once it also scrubs the selection variables."""
        argv = ("--no-replace-objects", "merge-base", "--is-ancestor", A, B)
        env = dict(SCRUB, GIT_GRAFT_FILE=os.devnull)
        gitfacts.record(self.here, argv, env, 1, b"", b"")
        self.assertEqual(gitfacts.lookup(self.here, argv, env), (1, b"", b""))


class SelectionScrubTest(RootTest):
    """THE REPOSITORY MUST BE THE ONE IN THE KEY, and no argv can pin that."""

    def test_an_unscrubbed_selection_environment_is_never_stored(self):
        """`landreq._object_view`'s overlay: both rewriters pinned, nothing
        scrubbed. Ambient `GIT_DIR` or `GIT_ALTERNATE_OBJECT_DIRECTORIES` then
        selects a repository from OUTSIDE the key, and `git -C <path>` does not
        beat them. Today's safety is one caller's habit; this makes it the
        module's precondition."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B),
                        REWRITERS_ONLY, 1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.assertIsNone(gitfacts.lookup(
            self.here, ("merge-base", "--is-ancestor", A, B), REWRITERS_ONLY))
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        self.assertEqual(
            gitfacts.lookup(self.here, ("merge-base", "--is-ancestor", A, B),
                            PINNED), (1, b"", b""),
            "control: the IDENTICAL question under the scrubbed overlay is "
            "stored AND SERVED, so the miss above is the scrub and not the "
            "question")
        self.assertEqual(self.entries(), 1,
                         "and the count moved with it")

    def test_one_missing_selection_variable_is_enough_to_refuse(self):
        """The clause is ALL of them. A scrub that forgot one variable would
        still be honoured by git for that one."""
        for name in dispatches._GIT_SELECTION_ENV:
            partial = {k: v for k, v in PINNED.items() if k != name}
            gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B),
                            partial, 1, b"", b"")
            self.assertEqual(self.entries(), 0, "dropping %s must refuse" % name)
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        self.assertEqual(self.entries(), 1,
                         "control: the complete scrub stores, so every zero "
                         "above is the missing variable")

    def test_a_selection_variable_left_set_is_refused(self):
        """PRESENT-AND-EMPTY is the assertion, not merely absent: `{}`.get
        answers None for a key nobody scrubbed, and a dict that only OMITS
        `GIT_DIR` removes nothing from the child."""
        loud = dict(PINNED, GIT_DIR="/somewhere/else/.git")
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), loud,
                        1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")


class GitVersionTest(RootTest):
    """THE BINARY IS AN OPERAND OF EVERY ANSWER IT PRODUCED."""

    def setUp(self):
        super().setUp()
        self.addCleanup(gitfacts._VERSION.clear)

    def pin(self, value):
        gitfacts._VERSION[:] = [value]

    def test_a_different_git_is_a_different_key(self):
        self.pin(b"git version 2.53.0")
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.pin(b"git version 2.39.5")
        self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED),
                          "an entry written by one git must not answer for "
                          "another: merge-ort and patch identity have both "
                          "been revised across releases")
        self.pin(b"git version 2.53.0")
        self.assertEqual(gitfacts.lookup(self.here, ("cherry", A, B), PINNED),
                         (0, b"+ x\n", b""),
                         "control: the SAME entry is served under the version "
                         "that wrote it, so the miss above is the version and "
                         "not the write")

    def test_a_git_that_cannot_be_read_refuses_the_table(self):
        """An answer whose producer cannot be named is not stored under a
        guess."""
        self.pin(None)
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(self.entries(), 0)
        self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED))
        self.pin(b"git version 2.53.0")
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(gitfacts.lookup(self.here, ("cherry", A, B), PINNED),
                         (0, b"+ x\n", b""),
                         "control: with a readable version the identical "
                         "write lands and is SERVED, so the miss above is the "
                         "unreadable version and not a dead table")
        self.assertEqual(self.entries(), 1,
                         "and the count moved with it")

    def test_the_version_is_resolved_once_per_process(self):
        """A probe per lookup would be the spawn this module exists to
        remove."""
        gitfacts._VERSION.clear()
        with mock.patch.object(gitfacts, "_resolve_version",
                               return_value=b"git version 9.9.9") as probe:
            for _ in range(5):
                gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
                gitfacts.lookup(self.here, ("cherry", A, B), PINNED)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.entries(), 1,
                         "control: those calls really did reach the table, so "
                         "the single probe is caching and not a dead path")


class ExitCodeTest(RootTest):
    """WHICH EXIT CODES ARE ANSWERS — per verb, never one global rule."""

    def test_a_not_an_ancestor_answer_is_stored(self):
        """rc 1 from `merge-base --is-ancestor` IS the answer, and on a fleet
        that lands rebased it is the answer almost every time: 127 of 127 on
        the ledger this module was measured against."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        self.assertEqual(
            gitfacts.lookup(self.here, ("merge-base", "--is-ancestor", A, B), PINNED),
            (1, b"", b""))

    def test_an_unreadable_operand_is_never_stored(self):
        """rc 128 is what BOTH admitted verbs return when an operand is not in
        the odb, and the next fetch can end that state. It is nobody's
        answer."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        128, b"", b"fatal: Not a valid object name\n")
        self.assertEqual(self.entries(), 0)
        # CONTROL: the same verb's rc 1 IS an answer and is stored, so the
        # refusal is about the exit code and not about `merge-base`.
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", B, A), PINNED,
                        1, b"", b"")
        self.assertEqual(self.entries(), 1)

    def test_a_spawn_failure_is_never_stored(self):
        """rc -1 is `_spawn`'s spelling for any spawn trouble — a fact about
        the moment, not about the subject."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        -1, b"", b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")


class EntryTest(RootTest):
    """THE STORED BYTES, and every way reading them can go wrong."""

    def test_stdout_and_stderr_both_survive(self):
        """A caller that reads stderr on an answered call must see what it
        would have seen from the spawn."""
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ %s\n" % B.encode(),
                        b"warning: something\n")
        self.assertEqual(gitfacts.lookup(self.here, ("cherry", A, B), PINNED),
                         (0, b"+ %s\n" % B.encode(), b"warning: something\n"))

    def test_output_bytes_are_not_decoded_or_stripped(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is `assertNotEqual(raw, raw.strip())` on the SAME bytes: the input demonstrably HAS whitespace to lose, so the equality above it can fail
        """`_git_bytes` exists because `text` strips, and a stripped patch
        hashes differently. A table between them must not re-introduce it."""
        raw = b"  \t x \r\n\n"
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, raw, b"")
        self.assertEqual(gitfacts.lookup(self.here, ("cherry", A, B), PINNED)[1],
                         raw)
        self.assertNotEqual(raw, raw.strip(),
                            "control: this input HAS whitespace to lose, so "
                            "the equality above can fail")

    def test_a_half_written_entry_is_a_miss_not_a_crash(self):
        """A writer killed mid-write must cost a spawn, never a traceback in
        a read path."""
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ deadbeef\n", b"")
        for base, _dirs, names in os.walk(self.root):
            for name in names:
                path = os.path.join(base, name)
                with open(path, "rb") as f:
                    blob = f.read()
                with open(path, "wb") as f:
                    f.write(blob[:len(blob) - 4])
        self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED))
        self.admit()
        self.assertEqual(
            gitfacts.lookup(self.there, ("cherry", A, B), PINNED),
            (0, b"+ x\n", b""),
            "control: the same table DOES serve an admitted answer, so the "
            "miss above is this clause refusing and not a dead table")

    def test_garbage_in_the_header_is_a_miss(self):
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
        for base, _dirs, names in os.walk(self.root):
            for name in names:
                with open(os.path.join(base, name), "wb") as f:
                    f.write(b"not a header at all\nx")
        self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED))
        self.admit()
        self.assertEqual(
            gitfacts.lookup(self.there, ("cherry", A, B), PINNED),
            (0, b"+ x\n", b""),
            "control: the same table DOES serve an admitted answer, so the "
            "miss above is this clause refusing and not a dead table")

    def test_a_derivation_version_bump_orphans_every_entry(self):
        """The version is in the KEY and in the value, so a changed encoding
        cannot be read under the old meaning by either route."""
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
        with mock.patch.object(gitfacts, "_V", gitfacts._V + 1):
            self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED))
        self.assertEqual(gitfacts.lookup(self.here, ("cherry", A, B), PINNED),
                         (0, b"x", b""))

    def test_an_oversized_answer_is_not_stored(self):
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0,
                        b"x" * (gitfacts._MAX_BYTES + 1), b"")
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table DOES store an admitted "
                         "answer, so the zero above is this clause refusing "
                         "and not a table that cannot write")

    def test_two_repository_paths_do_not_share_an_answer(self):
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        self.assertIsNone(gitfacts.lookup(
            self.there, ("merge-base", "--is-ancestor", A, B), PINNED))
        self.assertEqual(
            gitfacts.lookup(self.here, ("merge-base", "--is-ancestor", A, B),
                            PINNED),
            (1, b"", b""),
            "control: the entry exists under the repository that wrote it, so "
            "the miss above is the repository and not the write")

    def test_a_different_overlay_is_a_different_question(self):
        """The env is part of the key: two callers asking one argv under
        different views are asking different questions. This says nothing
        about AMBIENT config, which is not in the overlay at all — that hazard
        is what the verb table answers, and `AmbientStateTest` measures."""
        gitfacts.record(self.here, ("merge-base", "--is-ancestor", A, B), PINNED,
                        1, b"", b"")
        other = dict(PINNED, GIT_CONFIG_GLOBAL="/dev/null")
        self.assertIsNone(gitfacts.lookup(
            self.here, ("merge-base", "--is-ancestor", A, B), other))
        self.assertEqual(
            gitfacts.lookup(self.here, ("merge-base", "--is-ancestor", A, B), PINNED),
            (1, b"", b""),
            "control: the SAME entry is served under the overlay it was "
            "stored with, so the miss above is the overlay and not the write")

    def test_disable_turns_both_halves_off(self):
        """A caller that must observe the uncached world gets the uncached
        world — and no entry is left behind for the next reader either."""
        gitfacts.disable()
        self.addCleanup(gitfacts.enable)
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
        self.assertEqual(self.entries(), 0)
        gitfacts.enable()
        self.admit()
        self.assertEqual(
            gitfacts.lookup(self.there, ("cherry", A, B), PINNED),
            (0, b"+ x\n", b""),
            "control: with the table ON the same write lands and is served, "
            "so the refusals here are `disable` and not a dead table")
        self.assertEqual(self.entries(), 1,
                         "and the COUNT moved, so the zero above is `disable` "
                         "and not a table that cannot write")
        gitfacts.disable()
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
        gitfacts.disable()
        self.assertIsNone(gitfacts.lookup(self.here, ("cherry", A, B), PINNED))


class RealRepoTest(RootTest):
    """A REAL repository, a REAL git, and the REAL seam."""

    def setUp(self):
        super().setUp()
        self.repo = self.build(self.empty_repo())
        self.first = _git(self.repo, "rev-list", "--max-count=1", "HEAD~1")
        self.head = _git(self.repo, "rev-list", "--max-count=1", "HEAD")
        self.backend = vcs.backend(self.repo)

    def build(self, repo, mark=""):
        """`mark` IS NOT DECORATION. A git commit id is a function of its tree,
        message, author and TIMESTAMP, so rebuilding this same fixture at the
        same path in the same second reproduces the IDENTICAL shas — measured,
        and it silently turned "a second repository" into a byte-for-byte copy
        of the first. The marker is what makes the two histories unrelated."""
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        for name in ("one", "two"):
            with open(os.path.join(repo, name + ".txt"), "w") as f:
                f.write(name + mark)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", name + mark)
        return repo

    def spawns(self):
        return mock.patch.object(vcs.GitVcs, "_spawn",
                                 autospec=True,
                                 side_effect=vcs.GitVcs._spawn)

    def run_git(self, *argv, **kw):
        return self.backend.run(kw.pop("repo", self.repo), *argv, env=PINNED)


class SeamTest(RealRepoTest):
    """THE END-TO-END CLAIM: the SECOND identical read forks NOTHING and
    returns what the first returned.

    Asserted by counting spawns, because the observable that matters is the
    process that did not start — a returned value alone is satisfied by a
    cache that never engaged."""

    def test_the_second_identical_read_forks_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is `assertEqual(warm, cold)` on the SAME call plus the cold-rc assertion above it: a table that never engaged cannot produce a zero spawn count AND the cold answer
        argv = ("merge-base", "--is-ancestor", self.first, self.head)
        cold = self.run_git(*argv)
        self.assertEqual(cold[0], 0, "the control: HEAD~1 IS an ancestor")
        with self.spawns() as spawn:
            warm = self.run_git(*argv)
        self.assertEqual(spawn.call_count, 0,
                         "a stored immutable answer must not start git")
        self.assertEqual(warm, cold)

    def test_a_ref_question_forks_every_time(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the one above it, and its own control is the cherry verdict assertion on the same returned call
        """The must-hit control for the arm above: the same seam, the same
        repository, the same ADMITTED VERB, a question about NOW — and it
        still pays the spawn."""
        argv = ("cherry", "refs/heads/main", self.first)
        first = self.run_git(*argv)
        self.assertEqual(first[0], 0, "the control: cherry answered")
        with self.spawns() as spawn:
            again = self.run_git(*argv)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(again, first)

    def test_a_call_carrying_stdin_is_not_offered_to_the_table(self):
        """`git patch-id` reads CONTENT, and the table keys on argv alone —
        offering it would answer one patch's question with another's."""
        with self.spawns() as spawn:
            self.backend.run(self.repo, "patch-id", "--verbatim",
                             env=PINNED, stdin=b"")
            self.backend.run(self.repo, "patch-id", "--verbatim",
                             env=PINNED, stdin=b"")
        self.assertEqual(spawn.call_count, 2)

    def test_the_answer_served_is_the_answer_git_gave(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is `assertTrue(cold[1].strip())` — cherry really said something, so the equality below can fail
        """Not only the exit code: `cherry` writes its verdict on stdout, so a
        table that dropped or truncated output would still satisfy an arm that
        only checked rc."""
        argv = ("cherry", self.first, self.head)
        cold = self.run_git(*argv)
        self.assertTrue(cold[1].strip(), "the control: cherry said something")
        with self.spawns() as spawn:
            warm = self.run_git(*argv)
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(warm, cold)


class ObjectDatabaseTest(RealRepoTest):
    """EXISTENCE IS A PROPERTY OF AN OBJECT DATABASE, and the two ways that
    database moves under a path that did not."""

    def doom(self):
        """A commit made unreachable, then pruned. This is the shape a fleet
        that lands REBASED produces by itself: a reviewed tip is reachable
        from nothing once its lane is rebased away, and it is exactly what gc
        collects."""
        _git(self.repo, "checkout", "-q", "-b", "doomed")
        with open(os.path.join(self.repo, "doomed.txt"), "w") as f:
            f.write("doomed")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "doomed")
        sha = _git(self.repo, "rev-list", "--max-count=1", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-qD", "doomed")
        return sha

    def prune(self):
        _git(self.repo, "reflog", "expire", "--expire=now",
             "--expire-unreachable=now", "--all")
        _git(self.repo, "gc", "--prune=now", "-q")

    def test_a_pruned_object_is_not_answered_from_the_table(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the pre-prune `assertEqual(self.run_git(*existence)[0], 0)` on the SAME call, plus `assertEqual(served[0], 0)` proving the table is still live at the moment of the re-measure
        """THE MEASURED FAILURE PATH. `dispatches` guards carriage with `git
        cat-file -e <tip>^{commit}` and refuses when the object "is not in"
        the repository. A stored rc 0 makes that guard PASS over an odb that
        no longer holds the tip, and a proof then measures a repository other
        than the one it names."""
        sha = self.doom()
        existence = ("cat-file", "-e", sha + "^{commit}")
        self.assertEqual(self.run_git(*existence)[0], 0,
                         "the control: the object IS here before the prune")
        ancestry = ("merge-base", "--is-ancestor", self.first, self.head)
        self.assertEqual(self.run_git(*ancestry)[0], 0)
        self.prune()
        with self.spawns() as spawn:
            gone = self.run_git(*existence)
            served = self.run_git(*ancestry)
        self.assertNotEqual(gone[0], 0,
                            "the seam must report the object git reports: it "
                            "is no longer in this object database")
        self.assertGreaterEqual(spawn.call_count, 1,
                                "and it must have ASKED git, because an "
                                "existence claim is never stored")
        self.assertEqual(served[0], 0,
                         "control: an admitted answer about the same "
                         "repository is still served from the table, so the "
                         "re-measure above is this rule and not a dead cache")

    def test_a_path_reused_by_another_repository_serves_no_existence_claim(self):  # noqa: VACUOUS_ASSERTION — two unconditional controls on the same observables: the pre-reuse `existence` call asserted 0, and the closing `assertEqual(live, stored)` proves the answer that DID cross is the one live git gives
        """ONE PATH, TWO REPOSITORIES OVER TIME — the hazard two distinct
        paths cannot show.

        The key names a repository by PATH, so a second repository at that
        path meets the first one's entries. What the table holds is the only
        reason that is survivable: an answer about the OBJECT GRAPH two fixed
        ids root is the same answer wherever those objects are, and this arm
        proves it by putting the objects back and asking the live git. What
        must NOT survive is a claim that the objects are HERE, and that is
        re-measured."""
        mirror = tempfile.mkdtemp(prefix="helm-test-gitfacts-mirror-")
        self.addCleanup(shutil.rmtree, mirror, ignore_errors=True)
        _git(self.repo, "clone", "-q", "--mirror", ".", mirror)
        ancestry = ("merge-base", "--is-ancestor", self.first, self.head)
        stored = self.run_git(*ancestry)
        self.assertEqual(stored[0], 0, "the control: HEAD~1 IS an ancestor")
        existence = ("cat-file", "-e", self.head + "^{commit}")
        self.assertEqual(self.run_git(*existence)[0], 0,
                         "the control: the tip IS in the first repository")

        for name in os.listdir(self.repo):
            path = os.path.join(self.repo, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
        # A DIFFERENT repository, same path.
        _git(self.repo, "init", "-q", "-b", "main")
        self.build(self.repo, mark=" in the second repository")
        self.assertNotEqual(
            _git(self.repo, "rev-list", "--max-count=1", "HEAD"), self.head,
            "the control: the second repository really is a different one")

        with self.spawns() as spawn:
            crossed = self.run_git(*existence)
        self.assertNotEqual(crossed[0], 0,
                            "no existence claim may cross: the tip of the "
                            "FIRST repository is not in the SECOND")
        self.assertGreaterEqual(spawn.call_count, 1)

        _git(self.repo, "fetch", "-q", mirror, "+refs/heads/*:refs/old/*")
        gitfacts.disable()
        self.addCleanup(gitfacts.enable)
        live = self.run_git(*ancestry)
        self.assertEqual(
            live, stored,
            "and the answer that DID cross is the answer this git gives once "
            "the objects are present again — a fact about the two ids, not "
            "about either repository")


class AmbientStateTest(RealRepoTest):
    """NOTHING AMBIENT CONFIG CAN MOVE IS STORED — measured by moving it."""

    def build(self, repo, mark=""):
        super().build(repo, mark)
        _git(repo, "checkout", "-q", "-b", "left")
        self.write(repo, "LEFT\nb\nc\n")
        _git(repo, "commit", "-q", "-am", "left")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "checkout", "-q", "-b", "right", "main")
        self.write(repo, "RIGHT\nb\nc\n")
        _git(repo, "commit", "-q", "-am", "right")
        _git(repo, "checkout", "-q", "main")
        return repo

    def write(self, repo, text):
        with open(os.path.join(repo, "one.txt"), "w") as f:
            f.write(text)
        _git(repo, "add", "-A")

    def attributes(self, text):
        info = os.path.join(self.repo, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w") as f:
            f.write(text)

    def test_a_merge_tree_answer_moves_with_an_attributes_file(self):
        """THE STANDING RULING, MEASURED. `dispatches._carriage_replay_witness`
        says merge-tree's answer "IS NOT A FUNCTION OF THE IDS IT WAS HANDED".
        The first build of this table stored its rc 1 anyway. Here the same
        two ids answer 1 and then 0, moved by a file behind a pathname."""
        left = _git(self.repo, "rev-list", "--max-count=1", "left")
        right = _git(self.repo, "rev-list", "--max-count=1", "right")
        argv = ("merge-tree", "--write-tree", left, right)
        conflict = self.run_git(*argv)
        self.assertEqual(conflict[0], 1, "the control: these two DO conflict")
        self.attributes("one.txt merge=union\n")
        with self.spawns() as spawn:
            resolved = self.run_git(*argv)
        self.assertEqual(resolved[0], 0,
                         "the must-hit: an attributes file moved the answer "
                         "while every operand id stood still")
        self.assertTrue(resolved[1].strip(),
                        "and that run SAID something — the union merge printed "
                        "its tree — so the exit code above belongs to a run "
                        "that produced a result rather than to an empty one")
        self.assertGreaterEqual(spawn.call_count, 1,
                                "and git was ASKED again, so no merge-tree "
                                "answer was served from the table")
        self.assertEqual(self.entries(), 0,
                         "and nothing about merge-tree was written either")
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: this arm's own table DOES take a write, so "
                         "the zero above is merge-tree being refused and not "
                         "a table that cannot write")

    def test_a_conflicted_merge_tree_writes_objects_and_prints_a_tree(self):  # noqa: VACUOUS_ASSERTION — nothing here asserts an absence: the controls are `cat-file -t` returning "tree" for the printed id and the loose-object count strictly increasing, both unconditional
        """THE REFUTED JUSTIFICATION. The rc 1 half was admitted because a
        conflicted merge-tree "produces nothing". It prints a real tree id and
        leaves loose objects behind, exactly as the success half does."""
        left = _git(self.repo, "rev-list", "--max-count=1", "left")
        right = _git(self.repo, "rev-list", "--max-count=1", "right")
        loose = os.path.join(self.repo, ".git", "objects")
        before = sum(len(f) for _d, _s, f in os.walk(loose))
        rc, out, _err = self.run_git("merge-tree", "--write-tree", left, right)
        self.assertEqual(rc, 1)
        printed = out.decode().split("\n")[0].strip()
        self.assertEqual(_git(self.repo, "cat-file", "-t", printed), "tree",
                         "the conflicted run printed a real tree object")
        self.assertGreater(sum(len(f) for _d, _s, f in os.walk(loose)), before,
                           "and wrote objects into the odb while doing it")

    def test_a_diff_raw_answer_moves_with_ambient_config(self):
        """`diff --raw` is a RENDERING. `diff.orderFile` reorders its lines,
        and it does so through the exact argv this tree's one caller issues —
        which is why no argv pinning could have rescued the entry."""
        for name in ("one.txt", "two.txt"):
            with open(os.path.join(self.repo, name), "w") as fh:
                fh.write("moved")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "both")
        both = _git(self.repo, "rev-list", "--max-count=1", "HEAD")
        order = os.path.join(self.repo, "order")
        with open(order, "w") as fh:
            fh.write("two.txt\none.txt\n")
        argv = ("diff", "--raw", "-z", "--abbrev=40", "--no-renames",
                self.head, both)
        plain = self.run_git(*argv)
        self.assertEqual(
            sorted(f for f in plain[1].split(b"\0") if f.endswith(b".txt")),
            [b"one.txt", b"two.txt"],
            "the control: this diff names TWO paths, so there is an order for "
            "config to move — a one-path diff would satisfy the must-hit "
            "below by never being able to fail it")
        _git(self.repo, "config", "diff.orderFile", order)
        with self.spawns() as spawn:
            reordered = self.run_git(*argv)
        self.assertNotEqual(reordered[1], plain[1],
                            "the must-hit: ambient config moved the bytes "
                            "while every operand id stood still")
        self.assertGreaterEqual(spawn.call_count, 1)
        self.assertEqual(self.entries(), 0,
                         "and no diff answer was written to the table")
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: this arm's own table DOES take a write, so "
                         "the zero above is `diff` being refused and not a "
                         "table that cannot write")

    def test_the_admitted_verbs_do_not_move_under_the_same_state(self):
        """THE CONTROL FOR THIS WHOLE CLASS. The same hostile ambient state
        that moved merge-tree and diff leaves both admitted verbs' answers
        byte-identical — which is the property the table rests on, and the
        reason these two are the two that stayed."""
        left = _git(self.repo, "rev-list", "--max-count=1", "left")
        argv_cherry = ("cherry", self.head, left)
        argv_anc = ("merge-base", "--is-ancestor", self.first, left)
        gitfacts.disable()
        self.addCleanup(gitfacts.enable)
        before = (self.run_git(*argv_cherry), self.run_git(*argv_anc))
        self.attributes("* -diff\n* merge=union\n")
        for name, value in (("diff.renames", "false"), ("core.abbrev", "12"),
                            ("diff.algorithm", "patience"),
                            ("diff.ignoreSubmodules", "all"),
                            ("diff.noprefix", "true"),
                            ("core.quotePath", "false")):
            _git(self.repo, "config", name, value)
        after = (self.run_git(*argv_cherry), self.run_git(*argv_anc))
        self.assertTrue(before[0][1].strip(),
                        "the control: cherry really said something, so the "
                        "equality below can fail")
        self.assertEqual(after, before)


class MergeViewTest(RealRepoTest):
    """task/3056: ONE merge-tree form, stored with the VIEW in its key.

    `AmbientStateTest` measured what moves a merge while every id stands
    still: an attributes file, `merge.renames`, `diff.renames`. Each of them is
    inside `foldckpt.fingerprint`, so the replay witness's exact form --
    `merge-tree --write-tree --merge-base=<id> <id> <id>` -- is admitted with
    that fingerprint in its key, asked at a git directory, inside a scope. The
    arms below hold the store to the ruling: the answer it serves is the one a
    fresh spawn gives, byte for byte; a store that is present but unreadable,
    corrupt or missing is a MISS; a view that moved is a different key; and
    nothing outside the one form, the git directory and a scope is stored."""

    def build(self, repo, mark=""):
        super().build(repo, mark)
        _git(repo, "checkout", "-q", "-b", "left")
        self.write(repo, "one.txt", "LEFT\nb\nc\n")
        _git(repo, "commit", "-q", "-am", "left")
        _git(repo, "checkout", "-q", "-b", "right", "main")
        self.write(repo, "one.txt", "RIGHT\nb\nc\n")
        _git(repo, "commit", "-q", "-am", "right")
        _git(repo, "checkout", "-q", "-b", "other", "main")
        self.write(repo, "two.txt", "OTHER\n")
        _git(repo, "commit", "-q", "-am", "other")
        _git(repo, "checkout", "-q", "main")
        return repo

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)
        _git(repo, "add", "-A")

    def argv(self, *sides):
        """The replay witness's exact form over this fixture's commits."""
        ids = [_git(self.repo, "rev-parse", side) for side in sides]
        return ("merge-tree", "--write-tree",
                "--merge-base=" + _git(self.repo, "rev-parse", "main")) \
            + tuple(ids)

    def gitdir(self):
        return os.path.join(self.repo, ".git")

    def ask(self, argv, where=None, scoped=True):
        """(rc, stdout, stderr, merge processes) for one read through the seam."""
        with self.spawns() as spawn:
            if scoped:
                with projscope.scope():
                    got = self.backend.run(where or self.gitdir(), *argv,
                                           env=PINNED)
            else:
                got = self.backend.run(where or self.gitdir(), *argv,
                                       env=PINNED)
        merges = [c for c in spawn.call_args_list
                  if tuple(c.args[2])[:1] == ("merge-tree",)]
        return got + (len(merges),)

    def fresh(self, argv):
        """What git answers with the table switched off."""
        gitfacts.disable()
        try:
            return self.backend.run(self.gitdir(), *argv, env=PINNED)
        finally:
            gitfacts.enable()

    def test_the_answer_served_is_a_fresh_spawn_byte_for_byte(self):  # noqa: VACUOUS_ASSERTION — the loop is a fixed two-case tuple and the closing unconditional entries()==2 proves both cases ran and stored; every served zero sits beside that case's own cold (rc, 1 merge) on the same instrument
        """A conflict (rc 1, the answer most carried closes get) and a clean
        merge (rc 0), each asked in one scope and served in the next."""
        for sides, rc in ((("left", "right"), 1), (("left", "other"), 0)):
            with self.subTest(sides=sides):
                argv = self.argv(*sides)
                cold = self.ask(argv)
                self.assertEqual((cold[0], cold[3]), (rc, 1),
                                 "the control: the first scope ran the merge "
                                 "and got the answer the fixture was built for")
                warm = self.ask(argv)
                self.assertEqual(warm[3], 0,
                                 "a later scope re-ran a merge the table holds")
                spawned = self.fresh(argv)
                self.assertTrue(spawned[1].strip(), "git printed a tree")
                self.assertEqual(warm[:2], spawned[:2],
                                 "the served (rc, stdout) is not git's")
                self.assertEqual(cold[:2], spawned[:2])
        self.assertEqual(self.entries(), 2)

    def test_a_present_but_unreadable_or_corrupt_or_missing_store_is_a_miss(self):  # noqa: VACUOUS_ASSERTION — the served zero is the POSITIVE control here (the entry is live), bracketed by the cold run's one merge on the same instrument; each damage case asserts one merge and git's own (rc, stdout)
        argv = self.argv("left", "right")
        cold = self.ask(argv)
        self.assertEqual(cold[3], 1)
        entry, = gitfacts.entry_paths()
        with open(entry, "rb") as f:
            good = f.read()
        # THE UNCONDITIONAL POSITIVE CONTROL: this entry IS served.
        self.assertEqual(self.ask(argv)[3], 0)

        def damaged(how):
            got = self.ask(argv)
            self.assertEqual(got[3], 1, "%s was served instead of re-asked"
                                        % how)
            self.assertEqual(got[:2], cold[:2],
                             "and the re-asked answer is git's own")

        if os.geteuid() != 0:                  # root reads a mode-000 file
            os.chmod(entry, 0)
            damaged("a present entry that cannot be read")
            os.chmod(entry, 0o644)
            self.assertEqual(self.ask(argv)[3], 0, "control: readable again")
        os.unlink(entry)
        os.mkdir(entry)                        # present, and not a file
        damaged("an entry that is a directory")
        os.rmdir(entry)
        for how, blob in (("a garbage header", b"not a header\n" + good),
                          ("a non-answer exit code",
                           good.replace(b" 1 ", b" 128 ", 1)),
                          ("stderr on a merge answer", good + b"hint: x\n")):
            with open(entry, "wb") as f:
                f.write(blob)
            damaged(how)
        shutil.rmtree(self.root)
        damaged("a table that is not there")

    def test_a_view_that_moved_is_a_different_key(self):
        """The mover `AmbientStateTest` measured, through the exact form the
        table now admits: the same ids, an attributes line, a new answer --
        and the old answer served again once the line is gone."""
        argv = self.argv("left", "right")
        conflict = self.ask(argv)
        self.assertEqual(conflict[0], 1, "the control: these two conflict")
        info = os.path.join(self.gitdir(), "info")
        os.makedirs(info, exist_ok=True)
        attributes = os.path.join(info, "attributes")
        with open(attributes, "w") as f:
            f.write("one.txt merge=union\n")
        union = self.ask(argv)
        self.assertEqual((union[0], union[3]), (0, 1),
                         "the moved view was served the old answer")
        os.unlink(attributes)
        back = self.ask(argv)
        self.assertEqual((back[0], back[3]), (1, 0),
                         "the unmoved view's own entry was not served")
        self.assertEqual(self.entries(), 2)

    def test_only_the_one_form_at_a_git_directory_inside_a_scope(self):
        argv = self.argv("left", "right")
        left, right, trunk = argv[3], argv[4], argv[2].split("=", 1)[1]
        for why, question, where, scoped in (
                ("outside a scope", argv, None, False),
                ("inside a work tree", argv, self.repo, True),
                ("with no merge base", ("merge-tree", "--write-tree", left,
                                        right), None, True),
                ("with a glued option", ("merge-tree", "--write-tree",
                                         "--merge-base=" + trunk, "-Xours",
                                         left), None, True),
                ("on a ref", argv[:3] + ("left", right), None, True)):
            with self.subTest(why=why):
                self.ask(question, where=where, scoped=scoped)
                self.assertEqual(self.entries(), 0, why)
        # THE CONTROL, on the same table: the admitted form IS stored.
        self.ask(argv)
        self.assertEqual(self.entries(), 1)


class ShallowTest(RootTest):
    """THE THIRD REWRITER, THE ONE WITH NO OVERLAY.

    `seats_stop_budget` rules that a durable memo of the ancestry/patch-identity
    verdict cannot be made correct without a view held immutable, and names a
    shallow boundary beside `refs/replace` and `info/grafts` as something that
    "reinterpret[s] the same immutable ids without changing one of them". The
    other two are pinned off by the overlay. This one cannot be, because the
    parent objects are absent rather than hidden, so it is refused."""

    def setUp(self):
        super().setUp()
        self.complete = self.empty_repo()
        _git(self.complete, "config", "user.email", "t@t")
        _git(self.complete, "config", "user.name", "t")
        for n in range(5):
            with open(os.path.join(self.complete, "f.txt"), "a") as fh:
                fh.write("line %d\n" % n)
            _git(self.complete, "add", "-A")
            _git(self.complete, "commit", "-q", "-m", "c%d" % n)
        self.head = _git(self.complete, "rev-list", "--max-count=1", "HEAD")
        self.mid = _git(self.complete, "rev-list", "--max-count=1", "HEAD~2")
        _git(self.complete, "branch", "mid", self.mid)
        self.shallow = tempfile.mkdtemp(prefix="helm-test-gitfacts-shallow-")
        self.addCleanup(shutil.rmtree, self.shallow, ignore_errors=True)
        shutil.rmtree(self.shallow)
        # `--depth` needs a TRANSPORT: a plain path clone is a local copy and
        # would hand over the whole history whatever depth was asked for.
        _git(self.complete, "clone", "-q", "--depth=1", "--no-local",
             "file://" + self.complete, self.shallow)
        # A SECOND shallow root, so both operands are PRESENT and only the
        # path between them is missing — an absent operand would answer 128,
        # which is nobody's answer and would be refused for that reason
        # instead of this one.
        _git(self.shallow, "fetch", "-q", "--depth=1", "origin",
             "mid:refs/heads/mid")

    def ask(self, repo):
        return vcs.backend(repo).run(
            repo, "merge-base", "--is-ancestor", self.mid, self.head,
            env=PINNED)

    def test_a_shallow_repository_is_neither_stored_nor_served(self):  # noqa: VACUOUS_ASSERTION — the unconditional controls are `assertEqual(complete[0], 0)` and `assertEqual(self.entries(), 1)` on the SAME observables before the boundary is introduced, plus `cat-file -t` proving both operands are present
        complete = self.ask(self.complete)
        self.assertEqual(complete[0], 0,
                         "the control: in the WHOLE history it is an ancestor")
        self.assertEqual(self.entries(), 1,
                         "and the complete repository's answer IS stored, so "
                         "every zero below is the boundary and not a dead "
                         "table")
        self.assertEqual(
            [_git(self.shallow, "cat-file", "-t", sha)
             for sha in (self.mid, self.head)], ["commit", "commit"],
            "the control: both operands are PRESENT in the shallow clone — "
            "asked with `-t`, which SAYS something, because `-e` writes "
            "nothing at all whether the object is there or not — so what "
            "follows is the boundary and not a missing object")
        truncated = self.ask(self.shallow)
        self.assertEqual(truncated[0], 1,
                         "the must-hit: the SAME two ids answer differently "
                         "across a shallow boundary, with neither id changed")
        self.assertEqual(self.entries(), 1,
                         "and that answer was not added to the table")
        with mock.patch.object(vcs.GitVcs, "_spawn", autospec=True,
                               side_effect=vcs.GitVcs._spawn) as spawn:
            again = self.ask(self.shallow)
        self.assertEqual(again, truncated)
        self.assertGreaterEqual(spawn.call_count, 1,
                                "and it is re-asked every time rather than "
                                "served from anywhere")

    def test_a_relocated_shallow_file_is_refused(self):
        """`GIT_SHALLOW_FILE` moves the boundary from outside the key, and no
        caller scrubs it."""
        moved = dict(PINNED, GIT_SHALLOW_FILE="/somewhere/shallow")
        gitfacts.record(self.complete, ("cherry", A, B), moved, 0, b"x", b"")
        self.assertEqual(self.entries(), 0)
        gitfacts.record(self.complete, ("cherry", A, B), PINNED, 0, b"x", b"")
        self.assertEqual(self.entries(), 1,
                         "control: the identical write without that variable "
                         "lands, so the zero above is the variable")

    def test_a_layout_that_cannot_be_resolved_is_refused(self):
        """Absence of a readable `shallow` file is not evidence of a complete
        history when the directory is not a repository this can read at all."""
        stranger = tempfile.mkdtemp(prefix="helm-test-gitfacts-nonrepo-")
        self.addCleanup(shutil.rmtree, stranger, ignore_errors=True)
        gitfacts.record(stranger, ("cherry", A, B), PINNED, 0, b"x", b"")
        self.assertEqual(self.entries(), 0)
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"x", b"")
        self.assertEqual(self.entries(), 1,
                         "control: the identical write against a readable "
                         "repository lands, so the zero above is the layout")

    def test_a_linked_worktree_resolves_to_its_shared_repository(self):
        """A lane worktree's `.git` is a FILE naming a gitdir whose `commondir`
        points at the repository the boundary belongs to. Resolving only the
        first hop would read a directory that never holds `shallow`."""
        linked = os.path.join(self.complete, "..", "linked-%d" % os.getpid())
        linked = os.path.normpath(linked)
        self.addCleanup(shutil.rmtree, linked, ignore_errors=True)
        _git(self.complete, "worktree", "add", "-q", linked, "-b", "side")
        common = gitfacts._common_dir(linked)
        self.assertEqual(common,
                         os.path.normpath(os.path.join(self.complete, ".git")),
                         "the linked worktree must resolve to the shared "
                         "repository, not to its own gitdir")
        self.assertTrue(gitfacts._complete(linked, PINNED),
                        "control: and that shared repository is complete, so "
                        "the resolution above is readable and not merely "
                        "non-None")


class CeilingTest(RootTest):
    """THE EXIT ARC: this table evicts, and eviction is a DISK decision.

    Every arm here runs the retention plane gc declares for this store —
    `gitfacts.entry_paths` as the finder, `gc._measure_count` as the axis,
    `gc._reap` as the actuator — over THIS arm's own root and nothing else.
    `gc.scan()` measures every declared stream against the LIVE estate and
    `gc --apply` reaps every reapable one; running either from here would make
    this suite a fleet reaper, which is a mistake the gc suite has already
    paid for once (24,001 live chat cursors). So the census is narrowed to one
    row and every other part of the plane is gc's own code."""

    def policy(self):
        return next(p for p in gc.POLICIES if p["stream"] == "gitfacts")

    def sweep(self):
        """gc's own measurement and gc's own reaper over this root.
        -> (items, bytes) actually removed."""
        policy = self.policy()
        found = policy["find"]()
        _budget, _used, victims, _size = gc._measure_count(
            found, policy, time.time())
        _lines, items, reaped = gc._reap(
            {"policy": policy, "victims": victims, "act": policy["act"],
             "claims": {}, "now": time.time()})
        return items, reaped

    def fill(self, count, repo=None):
        """`count` distinct admitted answers, newest last."""
        for i in range(count):
            gitfacts.record(repo or self.here,
                            ("cherry", A, "%040x" % i), PINNED, 0, b"+ x\n", b"")

    def test_the_real_ceiling_is_reachable_and_the_sweep_prunes_exactly_to_it(self):
        """THE MUST-HIT CONTROL for every arm below: the ceiling is a number
        this store can actually reach, and reaching it is what makes the
        eviction observable at all.

        It is run at the REAL `MAX_ENTRIES` rather than a patched-small one on
        purpose. An arm that lowers the ceiling to eight proves the axis
        arithmetic and says nothing about whether 4096 entries can exist — and
        a ceiling nothing ever reaches is a ceiling that has never been
        tested. Writing them costs about a quarter of a second."""
        over = 12
        self.fill(gitfacts.MAX_ENTRIES + over)
        self.assertEqual(self.entries(), gitfacts.MAX_ENTRIES + over,
                         "the control: the store really holds more entries "
                         "than its ceiling admits, so there is something to "
                         "evict")
        items, reaped = self.sweep()
        self.assertEqual(items, over, "the sweep must remove exactly the "
                                      "entries past the ceiling")
        self.assertGreater(reaped, 0, "and account for their bytes")
        self.assertEqual(self.entries(), gitfacts.MAX_ENTRIES,
                         "and prune TO the ceiling, not past it and not to it "
                         "approximately")

    def test_a_table_under_its_ceiling_loses_nothing(self):
        """The other polarity of the same axis, on the same observable: the
        sweep is not a flush. A size budget would have been — `gc._measure_size`
        makes the WHOLE stream a victim the moment the total is over — and that
        is the concrete reason this row is on the count axis."""
        self.fill(64)
        self.assertEqual(self.entries(), 64)
        items, reaped = self.sweep()
        self.assertEqual((items, reaped), (0, 0))
        self.assertEqual(self.entries(), 64,
                         "a table inside its budget must come out of a sweep "
                         "byte-for-byte unchanged")
        with mock.patch.dict(self.policy(), {"count": 60}):
            items, reaped = self.sweep()
        self.assertEqual((items, self.entries()), (4, 60),
                         "the unconditional control on the SAME sweep: drop "
                         "the ceiling under the same table and it reaps, so "
                         "the zero above is a budget decision and not a "
                         "reaper that cannot remove anything")
        self.assertGreater(reaped, 0)

    def write(self, i):
        """Record question `i` and return the one path that record created."""
        before = set(gitfacts.entry_paths())
        gitfacts.record(self.here, ("cherry", A, "%040x" % i), PINNED,
                        0, b"+ x\n", b"")
        (path,) = set(gitfacts.entry_paths()) - before
        return path

    def aged(self, path, seconds):
        """Set one entry's recency `seconds` into the past, to the ns."""
        stamp = time.time_ns() - int(seconds * 1e9)
        os.utime(path, ns=(stamp, stamp))
        return stamp

    def read_then_sweep(self, touch):
        """ONE WORLD, run twice by the arm below with only the touch differing.

        The entry a reader asks for was WRITTEN FIRST, two hours ago; the three
        nobody reads were written after it. One hit, then gc's own sweep down
        to a single entry. -> (served, items, read survives, [each unread
        survives, oldest first])"""
        for path in gitfacts.entry_paths():
            os.unlink(path)
        read = self.write(0)
        unread = [self.write(i) for i in (1, 2, 3)]
        self.aged(read, 7200)
        for age, path in zip((3600, 3500, 3400), unread):
            self.aged(path, age)
        # THE TOUCHING WORLD PATCHES NOTHING, so it runs the code as shipped.
        # `create=True` lets the other world run on a tree that predates the
        # refresh, where it is the same read; if the refresh is ever renamed,
        # that world stops being disabled and its eviction assertion goes red.
        with contextlib.nullcontext() if touch else mock.patch.object(
                gitfacts, "_used", lambda *_a: None, create=True):
            served = gitfacts.lookup(self.here, ("cherry", A, "%040x" % 0),
                                     PINNED)
        with mock.patch.dict(self.policy(), {"count": 1}):
            items, _reaped = self.sweep()
        return (served, items, os.path.exists(read),
                [os.path.exists(path) for path in unread])

    def test_the_least_recently_USED_is_what_goes(self):
        """LAST USE, AND SAID TO BE LAST USE (task/3056). The order is the
        file's mtime, which `record` sets and a served hit refreshes, so an
        entry that is still being asked for survives a sweep that removes
        entries written after it which nobody reads. That is the recorded-trunk
        generation's case: its questions are the OLDEST writes in the table and
        are asked again after every land.

        TWO WORLDS, IDENTICAL BUT FOR THE TOUCH, on one observable — whether
        each path is still on disk. Without the refresh the same hit leaves the
        entry to be evicted first, by write time, and the newest unread entry is
        the survivor: the must-differ control. So what keeps the read entry is
        the refresh, not the sweep, the fixture or the ceiling.

        THE READ ENTRY IS QUESTION 0 AND ITS PATH IS WHEREVER ITS HASH PUTS IT,
        so a pruner that ordered by PATH instead of recency keeps one fixed path
        in both worlds, and the two worlds expect two different survivors."""
        answer = (0, b"+ x\n", b"")
        served, items, kept, unread = self.read_then_sweep(touch=False)
        self.assertEqual(served, answer, "the control: the hit was served")
        self.assertEqual(items, 3)
        self.assertFalse(kept, "the must-differ control: without the refresh "
                               "the entry written first is evicted first, "
                               "however recently it was read")
        self.assertEqual(unread, [False, False, True],
                         "the positive control, on the SAME observable: the "
                         "newest write is what survives by write time")

        served, items, kept, unread = self.read_then_sweep(touch=True)
        self.assertEqual(served, answer, "the control: the hit was served")
        self.assertEqual(items, 3)
        self.assertTrue(kept, "an entry that was just read was evicted ahead "
                              "of three written after it that nobody reads")
        self.assertEqual(unread, [False, False, False],
                         "and the entries nobody reads are what went")

    def test_a_hit_inside_the_interval_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the unchanged mtime is bracketed on the SAME observable, os.stat(path).st_mtime_ns, by the assertGreater after the past-the-interval hit, and both lookups are asserted served
        """THE THROTTLE. A hit is a ~20µs read, so a refresh on every one would
        be a write on every read; an entry refreshed within `_TOUCH_S` is left
        exactly as it is. The control is the same entry, the same read and the
        same instrument, past the interval: there the recency does move."""
        path = self.write(0)
        argv = ("cherry", A, "%040x" % 0)
        inside = self.aged(path, gitfacts._TOUCH_S - 60)
        self.assertIsNotNone(gitfacts.lookup(self.here, argv, PINNED))
        self.assertEqual(os.stat(path).st_mtime_ns, inside,
                         "a hit inside the interval rewrote the entry")
        outside = self.aged(path, gitfacts._TOUCH_S + 60)
        before = time.time_ns()
        self.assertIsNotNone(gitfacts.lookup(self.here, argv, PINNED))
        moved = os.stat(path).st_mtime_ns
        self.assertGreater(moved, outside,
                           "the control, on the SAME observable: a hit past "
                           "the interval refreshes the entry")
        self.assertGreaterEqual(moved, before - 10 ** 9,
                                "and refreshes it to NOW, not to some other "
                                "time")

    def test_a_refresh_that_fails_still_serves_the_answer(self):
        """A pruner may unlink the entry between the read and its refresh, and
        a store may be readable and not writable. Either way the answer already
        read is served and nothing is raised into the read."""
        path = self.write(0)
        self.aged(path, gitfacts._TOUCH_S + 60)
        with mock.patch("os.utime",
                        side_effect=FileNotFoundError(path)) as utime:
            got = gitfacts.lookup(self.here, ("cherry", A, "%040x" % 0),
                                  PINNED)
        self.assertEqual(utime.call_count, 1,
                         "the control: the refresh was attempted, so the "
                         "answer below survived a failing write")
        self.assertEqual(got, (0, b"+ x\n", b""))

    def test_a_stray_temp_file_is_counted_and_evicted(self):
        """`record` writes a temp beside the entry and renames over it; a
        process killed between the two leaves the temp behind and nothing in
        this tree has ever removed one. The census counts the STORE, so the
        residue is bounded by the same ceiling as an answer."""
        self.fill(2)
        shard = os.path.dirname(sorted(gitfacts.entry_paths())[0])
        orphan = os.path.join(shard, "deadbeef.1234.abcd.tmp")
        with open(orphan, "wb") as handle:
            handle.write(b"half a\n")
        stamp = time.time() - 7200
        os.utime(orphan, (stamp, stamp))
        self.assertIn(orphan, gitfacts.entry_paths(),
                      "a temp file occupies a block like any other file, so "
                      "the ceiling must see it")
        answers = [p for p in gitfacts.entry_paths() if p != orphan]
        with mock.patch.dict(self.policy(), {"count": 2}):
            items, _reaped = self.sweep()
        self.assertEqual(items, 1)
        self.assertTrue(all(os.path.exists(p) for p in answers),
                        "the control, on the SAME observable: both real "
                        "answers are still at their paths")
        self.assertFalse(os.path.exists(orphan),
                         "and the residue is what the ceiling evicted")


class PrunedEntryTest(RealRepoTest):
    """A PRUNED ENTRY COSTS ONE GIT SPAWN AND NOTHING ELSE — the whole
    argument for bounding this store by capacity rather than by a clock,
    asserted on both halves of the effect: the file is gone, and the answer
    that comes back after it is gone is the one git gives."""

    def test_a_pruned_entry_is_re_derived_and_the_answer_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — both absences carry an unconditional positive control on their own observable IN THIS METHOD: `assertEqual(spawn.call_count, 1)` after the prune against the `0` before it, and `assertTrue(all(os.path.exists(p) ...))` over the survivors against the `assertFalse(os.path.exists(entry))` beside it
        argv = ("merge-base", "--is-ancestor", self.first, self.head)
        before = set(gitfacts.entry_paths())
        cold = self.run_git(*argv)
        self.assertEqual(cold[0], 0, "the control: HEAD~1 IS an ancestor")
        born = set(gitfacts.entry_paths()) - before
        self.assertEqual(len(born), 1, "the read stored exactly one entry")
        entry = born.pop()
        with self.spawns() as spawn:
            self.assertEqual(self.run_git(*argv), cold)
        self.assertEqual(spawn.call_count, 0,
                         "the control: before the prune this answer is served "
                         "from the table, so the spawn counted below is the "
                         "eviction's and not the seam's")

        stamp = time.time() - 3600
        os.utime(entry, (stamp, stamp))
        for i in range(gitfacts.MAX_ENTRIES):
            gitfacts.record(self.repo, ("cherry", A, "%040x" % i), PINNED,
                            0, b"+ x\n", b"")
        policy = next(p for p in gc.POLICIES if p["stream"] == "gitfacts")
        found = policy["find"]()
        _b, _u, victims, _s = gc._measure_count(found, policy, time.time())
        self.assertEqual(victims, [entry],
                         "the oldest entry, and only it, is past the ceiling")
        gc._reap({"policy": policy, "victims": victims, "act": policy["act"],
                  "claims": {}, "now": time.time()})

        self.assertTrue(all(os.path.exists(p) for p in gitfacts.entry_paths()),
                        "the control, on the SAME observable: every surviving "
                        "entry is still at its path, so the absence below is "
                        "one eviction")
        self.assertFalse(os.path.exists(entry),
                         "THE EFFECT, half one: the entry is off the disk")
        with self.spawns() as spawn:
            after = self.run_git(*argv)
        self.assertEqual(spawn.call_count, 1,
                         "and the read paid the one git spawn the uncached "
                         "world pays — which is the entire cost of eviction")
        self.assertEqual(after, cold,
                         "THE EFFECT, half two: and git's answer is the same "
                         "answer, byte for byte, because the operands are the "
                         "same immutable objects they always were")

    def test_the_prune_is_not_on_the_read_path(self):
        """THE PRUNE MUST NEVER RAISE INTO A READ, and the strongest form of
        that is that a read never calls it. The census is wired to raise; a
        cold read, a warm read and a write all run through it unchanged, so
        the two paths are proven disjoint rather than merely proven quiet.

        The second half is the race the code claims is safe: an entry unlinked
        under a reader is a MISS, not an exception."""
        argv = ("cherry", self.first, self.head)
        cold = self.run_git(*argv)
        self.assertTrue(cold[1].strip(),
                        "the control: cherry really answered, so the equality "
                        "below has something to be wrong about")
        boom = mock.patch.object(gitfacts, "entry_paths",
                                 side_effect=AssertionError("read path"))
        with boom:
            self.assertEqual(self.run_git(*argv), cold,
                             "a warm read must not consult the retention "
                             "plane")
            gitfacts.record(self.repo, ("cherry", A, "b" * 40), PINNED,
                            0, b"+ x\n", b"")
            self.assertEqual(
                gitfacts.lookup(self.repo, ("cherry", A, "b" * 40), PINNED),
                (0, b"+ x\n", b""),
                "and neither must a write, nor the read that follows it")

        for path in gitfacts.entry_paths():
            os.unlink(path)
        self.assertIsNone(gitfacts.lookup(self.repo, argv, PINNED),
                          "an entry that went away under the reader is a "
                          "miss")
        self.assertEqual(self.run_git(*argv), cold,
                         "control: and the seam answers anyway, by spawning")


class CensusTest(RootTest):
    """`entry_paths` is the retention plane's only view of this store, so what
    it does with a store it cannot read is a correctness question and not an
    ergonomic one."""

    def test_a_store_that_was_never_written_is_legitimately_empty(self):
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertEqual(gitfacts.entry_paths(), [])
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(len(gitfacts.entry_paths()), 1,
                         "control: the same census counts an entry the moment "
                         "one exists, so the empty above is an empty store "
                         "and not a broken reader")

    def test_an_unreadable_store_raises_instead_of_reporting_empty(self):
        """gc's own law, learned on the cursor reaper: a finder that hands
        back [] for "I could not look" is printed inside `helm gc`'s
        in-budget line, and the ceiling silently stops existing. gc turns a
        raise into a loud ERR row."""
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        self.assertEqual(len(gitfacts.entry_paths()), 1,
                         "the control: this store is readable and holds one "
                         "entry right now")
        with mock.patch("os.listdir", side_effect=PermissionError(13, "nope")):
            with self.assertRaises(OSError):
                gitfacts.entry_paths()

    def test_the_gc_row_declares_the_store_s_own_ceiling(self):
        """Two copies of one number drift and the drift is invisible: the
        policy's budget IS `gitfacts.MAX_ENTRIES`, not a literal beside it."""
        policy = next(p for p in gc.POLICIES if p["stream"] == "gitfacts")
        self.assertEqual(policy["count"], gitfacts.MAX_ENTRIES)
        self.assertEqual(policy["cls"], "exhaust",
                         "only class exhaust is ever reapable")
        self.assertEqual(policy["act"], "prune")
        self.assertEqual([k for k in ("size", "age", "count") if k in policy],
                         ["count"],
                         "COUNT and not age: an age budget on a store whose "
                         "answers cannot expire is a ttl in a costume")


class DoctorLineTest(RootTest):
    """THE SURFACE. A bounded store nobody can see is still the class that let
    ~/.remember rot, and `helm gc` prints a stream only once it is already
    over budget — so the healthy reading needs its own line.

    The arms live in this file rather than beside doctor's other checks
    because the observable is this store, and this is the file that knows how
    to build one."""

    def test_the_line_counts_the_entries_and_the_bytes(self):
        payload = b"+ " + b"x" * 200 + b"\n"
        for i in range(3):
            gitfacts.record(self.here, ("cherry", A, "%040x" % i), PINNED,
                            0, payload, b"")
        level, msg = doctor.check_gitfacts_table()[0]
        self.assertEqual(level, "OK")
        self.assertIn("3 entries", msg)
        self.assertIn("ceiling %d" % gitfacts.MAX_ENTRIES, msg)
        self.assertIn("0.6KB", msg,
                      "the bytes are the store's own, measured: three entries "
                      "of a 203-byte answer plus their headers")

    def test_twice_the_ceiling_warns_that_the_sweep_is_not_running(self):
        """Over the ceiling is NORMAL between hourly sweeps; twice it is the
        claim that the drain has stopped. Run against a lowered ceiling
        because the assertion is about the threshold, not about how many
        entries this store can hold — the arm that proves THAT is
        `CeilingTest.test_the_real_ceiling_is_reachable_and_the_sweep_prunes_exactly_to_it`."""
        with mock.patch.object(gitfacts, "MAX_ENTRIES", 4):
            for i in range(5):
                gitfacts.record(self.here, ("cherry", A, "%040x" % i), PINNED,
                                0, b"+ x\n", b"")
            level, msg = doctor.check_gitfacts_table()[0]
            self.assertEqual((level, "5 entries" in msg), ("OK", True),
                             "the control: one over the ceiling is the normal "
                             "state between sweeps and must not WARN")
            for i in range(5, 9):
                gitfacts.record(self.here, ("cherry", A, "%040x" % i), PINNED,
                                0, b"+ x\n", b"")
            level, msg = doctor.check_gitfacts_table()[0]
        self.assertEqual(level, "WARN")
        self.assertIn("9 entries", msg)
        self.assertIn("helm gc --apply", msg,
                      "and the line names the drain that fixes it")

    def test_an_unreadable_store_warns_rather_than_raising(self):
        """A check that raises takes the whole `helm doctor` pass with it."""
        gitfacts.record(self.here, ("cherry", A, B), PINNED, 0, b"+ x\n", b"")
        level, msg = doctor.check_gitfacts_table()[0]
        self.assertEqual(level, "OK",
                         "the control: a readable store is an OK line")
        with mock.patch.object(gitfacts, "entry_paths",
                               side_effect=PermissionError(13, "nope")):
            level, msg = doctor.check_gitfacts_table()[0]
        self.assertEqual(level, "WARN")
        self.assertIn("unreadable", msg)

    def test_the_check_is_wired_into_the_doctor_pass(self):
        """A check nobody runs is the unbounded store all over again."""
        self.assertIn("check_gitfacts_table", doctor.CHECKS)


class RelativePathTest(RootTest):
    """task/3105 — A RELATIVE PATH IS NOT A REPOSITORY. This table outlives
    the process, so two processes started in two directories share it, and a
    key that carries the relative string names a different repository in
    each. "Immutable" holds for one repository; the key must name which.

    Two parents each hold a real repository at the relative path `repo`, so
    the shallow check admits the question in both and the key is the only
    thing left to refuse it."""

    def setUp(self):
        super().setUp()
        self.parents = []
        for _ in range(2):
            parent = os.path.realpath(
                tempfile.mkdtemp(prefix="helm-test-gitfacts-parent-"))
            self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
            _git(parent, "init", "-q", "-b", "main", "repo")
            self.parents.append(parent)
        self.addCleanup(os.chdir, os.getcwd())

    def test_a_relative_path_is_never_answered_from_another_directory(self):
        """ARM (b): recorded under `repo` from one parent, asked under `repo`
        from the other — as two helm processes in two directories would."""
        argv = ("merge-base", "--is-ancestor", A, B)
        here, there = self.parents
        os.chdir(here)
        gitfacts.record("repo", argv, PINNED, 0, b"", b"")
        served = gitfacts.lookup("repo", argv, PINNED)
        os.chdir(there)
        elsewhere = gitfacts.lookup("repo", argv, PINNED)
        self.assertEqual(served, (0, b"", b""),
                         "control: the entry is served where it was written")
        self.assertIsNone(elsewhere, "an answer written for one repository "
                                     "was served for another")
        self.assertEqual(self.entries(), 1)

    def test_every_spelling_of_one_repository_is_one_entry(self):  # noqa: VACUOUS_ASSERTION — the equality names the stored non-empty answer for every spelling, and the entry count is an exact 1
        """ARM (c), THE CONTROL: the key names the directory, it does not
        switch the table off. The relative, absolute, bytes and `PathLike`
        spellings of one repository are served the one entry a relative
        spelling wrote."""
        argv = ("cherry", A, B)
        here, _there = self.parents
        os.chdir(here)
        gitfacts.record("repo", argv, PINNED, 0, b"+ x\n", b"")
        spellings = {"relative": "repo",
                     "absolute": os.path.join(here, "repo"),
                     "bytes": b"repo",
                     "PathLike": pathlib.Path("repo")}
        got = {name: gitfacts.lookup(where, argv, PINNED)
               for name, where in spellings.items()}
        self.assertEqual(got, dict.fromkeys(spellings, (0, b"+ x\n", b"")))
        self.assertEqual(self.entries(), 1)

    def test_no_path_is_a_miss_and_never_a_raise(self):  # noqa: VACUOUS_ASSERTION — the absence IS the contract for a cwd that names no repository; `admit` then moves the same entry count to an exact 1
        """A cwd of None names no repository: nothing is stored under it and
        nothing is served for it, and neither door raises into the read."""
        argv = ("merge-base", "--is-ancestor", A, B)
        gitfacts.record(None, argv, PINNED, 0, b"", b"")
        self.assertIsNone(gitfacts.lookup(None, argv, PINNED))
        self.assertEqual(self.entries(), 0)
        self.admit()
        self.assertEqual(self.entries(), 1,
                         "control: the same table stores an answer for a "
                         "repository it can name")


if __name__ == "__main__":
    unittest.main()
