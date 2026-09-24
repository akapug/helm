"""The two durable landing ledgers: kept positive proofs, and the trunk cover.

Both exist because the projection's derive budget is a REAL bound and the work
behind it was neither bounded nor kept. Every arm here asserts an observable —
a file's contents, a returned word, or the exact absence of a git spawn — and
never that some call did not raise.

The invariants these arms pin are the contract agreed before the code was
written: full-object-id keys proved readable, one trunk per cover, a frontier
set rather than two ends, measured-never-latched completeness, whole-record
validation, and a budget observed by every enumeration.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import landreq                                     # noqa: E402
from helm import projscope                                   # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NAME", "HELM_CHAT_NODE_URL")


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-landledger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore_env)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        # The memos key on (size, mtime) of files that a previous test in this
        # process may have written under a DIFFERENT home. Clearing them here
        # is not hygiene, it is the difference between reading this test's
        # ledger and reading the last one's.
        landreq._LAND_PROOF_MEMO.clear()
        landreq._CARRIER_PROOF_MEMO.clear()
        landreq._TRUNK_INDEX_MEMO.clear()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.gitdir = os.path.join(self.repo, ".git")

    def _restore_env(self):
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        landreq._LAND_PROOF_MEMO.clear()
        landreq._CARRIER_PROOF_MEMO.clear()
        landreq._TRUNK_INDEX_MEMO.clear()

    def git(self, *args):
        done = subprocess.run(("git",) + args, cwd=self.repo,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0,
                         "git %s failed: %s" % (" ".join(args), done.stderr))
        return done.stdout.strip()

    def commit(self, name=None, body="x", empty=False):
        if empty:
            self.git("commit", "-q", "--allow-empty", "-m", "empty")
        else:
            path = os.path.join(self.repo, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            self.git("add", name)
            self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def proofs(self):
        path = landreq.land_proofs_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def cover(self):
        path = landreq.trunk_patch_index_path()
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get(self.gitdir) or {}

    def write_cover(self, record):
        os.makedirs(os.path.dirname(landreq.trunk_patch_index_path()),
                    exist_ok=True)
        with open(landreq.trunk_patch_index_path(), "w",
                  encoding="utf-8") as fh:
            json.dump({self.gitdir: record}, fh)
        landreq._TRUNK_INDEX_MEMO.clear()


class KeptProofTest(LedgerBase):
    def test_a_kept_positive_answers_with_NO_git_spawn_at_all(self):
        first = self.commit("a.txt")
        second = self.commit("b.txt")
        spy = []
        real = landreq._git

        def counting(gitdir, *args, **kw):
            spy.append(args[0] if args else "?")
            return real(gitdir, *args, **kw)

        # THE MUST-HIT: with an empty ledger the same question SPAWNS. Without
        # this the zero below would be satisfied by a function that answers
        # nothing at all.
        with mock.patch.object(landreq, "_git", counting):
            cold = landreq._landing_proof(self.gitdir, first, second)
        self.assertEqual(cold, "ancestor")
        self.assertTrue(spy, "the cold answer spawned no git at all — the "
                             "zero-spawn assertion below would be vacuous")
        self.assertEqual([r for r in self.proofs()
                          if r.get("tip") == first
                          and r.get("trunk") == second],
                         [{"proof": "ancestor", "repo": self.gitdir,
                           "tip": first, "trunk": second}])

        landreq._LAND_PROOF_MEMO.clear()
        del spy[:]

        def forbidden(*_a, **_k):
            raise AssertionError("a kept proof must spawn no git")

        with mock.patch.object(landreq, "_git", forbidden):
            warm = landreq._landing_proof(self.gitdir, first, second)
        self.assertEqual(warm, "ancestor")

    def test_the_kept_read_sits_ABOVE_the_derive_budget_door(self):
        first = self.commit("a.txt")
        second = self.commit("b.txt")
        self.assertEqual(landreq._landing_proof(self.gitdir, first, second),
                         "ancestor")
        landreq._LAND_PROOF_MEMO.clear()
        third = self.commit("c.txt")
        with mock.patch.object(landreq, "_derive_expired", lambda: True):
            kept = landreq._landing_proof(self.gitdir, first, second)
            # THE CONTROL, and it is what makes the line above mean anything:
            # an UNKEPT question under the same expired budget answers unknown.
            unkept = landreq._landing_proof(self.gitdir, third, second)
        self.assertEqual(kept, "ancestor")
        self.assertEqual(unkept, "unknown")

    def test_only_the_POSITIVES_are_kept(self):
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "side", base)
        off = self.commit("side.txt", body="only on the side")
        self.git("checkout", "-q", "-")
        landed = self.commit("b.txt")
        self.assertEqual(landreq._landing_proof(self.gitdir, off, landed),
                         "absent")
        landreq._LAND_PROOF_MEMO.clear()
        with mock.patch.object(landreq, "_derive_expired", lambda: True):
            self.assertEqual(landreq._landing_proof(self.gitdir, off, landed),
                             "unknown")
        self.assertEqual([r for r in self.proofs() if r.get("tip") == off], [],
                         "an absent or unknown answer was written to the "
                         "ledger — both are weather and must be re-derived")
        # THE POSITIVE CONTROL on the same ledger file, same call, same pass:
        # the writer is live, it simply refuses these two words.
        landreq._LAND_PROOF_MEMO.clear()
        self.assertEqual(landreq._landing_proof(self.gitdir, base, landed),
                         "ancestor")
        self.assertEqual([r.get("proof") for r in self.proofs()
                          if r.get("tip") == base], ["ancestor"])

    def test_a_REF_NAME_is_never_kept_and_never_read(self):  # noqa: VACUOUS_ASSERTION — the empty-proofs assertion is followed in the same pass by an unconditional control writing and reading a real object-id record from the same file
        first = self.commit("a.txt")
        self.commit("b.txt")
        branch = self.git("symbolic-ref", "--short", "HEAD")
        self.assertEqual(landreq._landing_proof(self.gitdir, first, branch),
                         "ancestor")
        self.assertEqual(self.proofs(), [],
                         "a proof was filed under a REF NAME, which moves — "
                         "the key must be an object id or it is not identity")
        # AND THE READER REFUSES ONE even if a line somehow exists: a
        # hand-written record naming the branch answers nothing.
        os.makedirs(os.path.dirname(landreq.land_proofs_path()), exist_ok=True)
        with open(landreq.land_proofs_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": branch,
                                 "proof": "ancestor"}) + "\n")
        landreq._LAND_PROOF_MEMO.clear()
        self.assertIsNone(landreq._kept_landing_proof(self.gitdir, first,
                                                      branch))
        # THE CONTROL, same file and same call shape, with a real object id.
        head = self.git("rev-parse", "HEAD")
        with open(landreq.land_proofs_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": head, "proof": "ancestor"}) + "\n")
        landreq._LAND_PROOF_MEMO.clear()
        self.assertEqual(landreq._kept_landing_proof(self.gitdir, first, head),
                         "ancestor")

    def test_a_key_that_does_not_RESOLVE_here_is_never_kept(self):  # noqa: VACUOUS_ASSERTION — the empty-proofs assertion is followed in the same pass by an unconditional control that DOES write, on the same file and the same call
        first = self.commit("a.txt")
        head = self.commit("b.txt")
        landreq._keep_landing_proof(self.gitdir, "0" * 40, head, "ancestor")
        self.assertEqual(self.proofs(), [],
                         "a proof was filed under an id this repository "
                         "cannot resolve")
        # THE CONTROL: the identical call with two resolvable ids DOES write.
        landreq._LAND_PROOF_MEMO.clear()
        landreq._keep_landing_proof(self.gitdir, first, head, "ancestor")
        self.assertEqual([r.get("tip") for r in self.proofs()], [first])

    def test_a_line_naming_an_UNKEPT_word_is_dropped_by_the_reader(self):
        first = self.commit("a.txt")
        second = self.commit("b.txt")
        os.makedirs(os.path.dirname(landreq.land_proofs_path()), exist_ok=True)
        with open(landreq.land_proofs_path(), "w", encoding="utf-8") as fh:
            for proof in ("absent", "unknown", "landed", ""):
                fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                     "trunk": second, "proof": proof}) + "\n")
            fh.write("{ this line is torn\n")
            fh.write(json.dumps([1, 2, 3]) + "\n")
            fh.write(json.dumps({"repo": self.gitdir, "tip": second,
                                 "trunk": second,
                                 "proof": "patch-equivalent"}) + "\n")
        landreq._LAND_PROOF_MEMO.clear()
        self.assertIsNone(landreq._kept_landing_proof(self.gitdir, first,
                                                      second))
        # THE MUST-HIT: the reader parsed the file at all, and neither a torn
        # line nor a JSON list blinded it to the good line beneath.
        self.assertEqual(landreq._kept_landing_proof(self.gitdir, second,
                                                     second),
                         "patch-equivalent")

    def test_a_proof_CARRIES_FORWARD_only_when_the_pair_ledger_says_so(self):
        first = self.commit("a.txt")
        old_trunk = self.commit("b.txt")
        self.assertEqual(landreq._landing_proof(self.gitdir, first, old_trunk),
                         "ancestor")
        new_trunk = self.commit("c.txt")
        landreq._LAND_PROOF_MEMO.clear()
        # WITHOUT the pair, there is no carry: the hop is READ, never computed,
        # so an unrecorded ancestry simply yields no free answer.
        self.assertIsNone(landreq._kept_landing_proof(self.gitdir, first,
                                                      new_trunk))
        with mock.patch.object(landreq, "_ancestry_ledger",
                               lambda: {(old_trunk, new_trunk): True}):
            self.assertEqual(landreq._kept_landing_proof(self.gitdir, first,
                                                         new_trunk),
                             "ancestor")
            # AND A RECORDED *NON*-ANCESTOR CARRIES NOTHING — the force-push
            # case the monotonicity argument names as its own falsifier.
            with mock.patch.object(landreq, "_ancestry_ledger",
                                   lambda: {(old_trunk, new_trunk): False}):
                self.assertIsNone(landreq._kept_landing_proof(
                    self.gitdir, first, new_trunk))


    def pairs(self):
        path = landreq.ancestry_pairs_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_the_WRITER_teaches_the_pairs_the_carry_forward_asks_for(self):
        """THE CARRY-FORWARD WAS BUILT AND WIRED TO NOTHING, and the arm above
        cannot see that because it MOCKS the pair ledger. Mocking supplies the
        one input the real world never produced: the ledger's only writer is
        `_ancestry_append`, whose only caller spends its budget on SUCCESSION
        pairs, so a trunk-to-trunk pair was never written and the reader could
        not hit. Measured on the live stores before this cure: 4,553 pairs
        against 83 distinct trunks in the proof ledger, with ZERO carrying
        either member among those 83.

        SO THIS ARM MOCKS NOTHING. It drives the real writer and then asks the
        real reader, which is the only shape that can tell a wired feature
        from an unwired one."""
        first = self.commit("a.txt")
        old_trunk = self.commit("b.txt")
        self.assertEqual(landreq._landing_proof(self.gitdir, first, old_trunk),
                         "ancestor",
                         "the fixture did not produce a proof to carry")
        new_trunk = self.commit("c.txt")
        landreq._LAND_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        # THE DEFECT: the hop is read and never computed, so with no writer
        # the proof one commit back is unreachable.
        self.assertIsNone(landreq._kept_landing_proof(self.gitdir, first,
                                                      new_trunk),
                          "the carry-forward answered with no pair recorded, "
                          "so this arm is not measuring the writer")
        landreq._teach_trunk_pairs(self.gitdir, new_trunk)
        landreq._ANCESTRY_MEMO.clear()
        # THE MUST-HIT: the writer actually wrote. Without this, a teach wired
        # to nothing and a store that was already converged are the same
        # silence -- which is the exact failure this arm exists to catch.
        self.assertEqual([(r.get("older"), r.get("newer"), r.get("anc"))
                          for r in self.pairs()],
                         [(old_trunk, new_trunk, True)],
                         "the teach wrote no pair, so the answer below would "
                         "say nothing about it")
        self.assertEqual(landreq._kept_landing_proof(self.gitdir, first,
                                                     new_trunk),
                         "ancestor",
                         "the pair is recorded and the carry-forward still "
                         "does not hit")

    def test_the_teach_SPENDS_A_BOUNDED_PURSE(self):
        """A CONVERGING BACKFILL IS LEGITIMATE AND AN UNBOUNDED WALK IS NOT --
        `_ancestry_ledger`'s own docstring draws that line. So more unseen
        trunks than the cap must leave the remainder UNSEEN rather than
        spending more, and a later pass picks them up."""
        first = self.commit("a.txt")
        olds = [self.commit("t%d.txt" % i) for i in range(4)]
        for old in olds:
            landreq._LAND_PROOF_MEMO.clear()
            landreq._keep_landing_proof(self.gitdir, first, old, "ancestor")
        new_trunk = self.commit("head.txt")
        landreq._LAND_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        with mock.patch.object(landreq, "TRUNK_PAIR_BUDGET", 2):
            landreq._teach_trunk_pairs(self.gitdir, new_trunk)
        self.assertEqual(len(self.pairs()), 2,
                         "the teach spent more than its cap: %r"
                         % (self.pairs(),))
        # AND THE REMAINDER IS STILL UNSEEN, so the next pass converges rather
        # than the budget silently freezing them as UNVERIFIED.
        landreq._ANCESTRY_MEMO.clear()
        with mock.patch.object(landreq, "TRUNK_PAIR_BUDGET", 99):
            landreq._teach_trunk_pairs(self.gitdir, new_trunk)
        self.assertEqual(len(self.pairs()), 4,
                         "a second pass did not finish the remainder: %r"
                         % (self.pairs(),))

    def test_an_UNDECIDABLE_pair_is_not_written(self):
        """`_ancestry_append`'s own contract, asserted here rather than
        assumed, because this caller is what now feeds it ids read out of a
        durable file rather than ids it just resolved. An unreadable answer
        must stay UNVERIFIED and be retried, never frozen into the ledger."""
        first = self.commit("a.txt")
        new_trunk = self.commit("b.txt")
        absent = "0" * 40
        os.makedirs(os.path.dirname(landreq.land_proofs_path()), exist_ok=True)
        with open(landreq.land_proofs_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": absent,
                                 "proof": "ancestor"}) + "\n")
        landreq._LAND_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        landreq._teach_trunk_pairs(self.gitdir, new_trunk)
        self.assertEqual(self.pairs(), [],
                         "a pair git could not decide was frozen into the "
                         "ledger: %r" % (self.pairs(),))
        # THE CONTROL, same call and same file, with a trunk that RESOLVES.
        real_old = first
        with open(landreq.land_proofs_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": real_old,
                                 "proof": "ancestor"}) + "\n")
        landreq._LAND_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        landreq._teach_trunk_pairs(self.gitdir, new_trunk)
        self.assertEqual([(r.get("older"), r.get("newer"))
                          for r in self.pairs()],
                         [(real_old, new_trunk)],
                         "the teach writes nothing at all, so the refusal "
                         "above proves nothing about undecidability")


class CarrierProofLedgerTest(LedgerBase):
    """The durable answers behind `proof_for`, the board's most expensive read.

    MEASURED before this ledger existed, one full projection over the live
    ledger: `vcs.landed_state` was 2209 calls at about 48ms — 105.8s of a
    116.6s pass — against 4.1s for every `landreq._git` spawn put together.
    The answers lived in a dict on the projection, so they died with the pass
    and with every `helm web` restart, and the board's top CPU consumer paid
    them again on each rebuild.
    """

    def carrier_proofs(self):
        path = landreq.carrier_proofs_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def rows(self, tip):
        row = {"repo_id": self.gitdir, "reviewed_tip": tip}
        return row, dict(row)

    def spy(self):
        """Count REAL `landed_state` calls without replacing the answer.

        The double wraps the shipped backend method rather than returning a
        verdict of its own: an arm that asserts a cache hit while feeding the
        cache its own invented answer proves only that its double was called.
        """
        from helm import vcs
        backend = type(vcs.backend(self.repo))
        real = backend.landed_state
        calls = []

        # PATCHED ON THE CLASS AND THEREFORE BOUND: `proof_for` builds its own
        # backend instance, so an instance patch would miss it — and a
        # replacement that forgets `self` shifts every argument by one, the
        # real call raises, and `proof_for`'s blanket except turns it into
        # UNKNOWN. That failure reads exactly like a cache miss.
        def counted(self, root, tip, ref, *a, **kw):
            calls.append((root, tip, ref))
            return real(self, root, tip, ref, *a, **kw)

        return calls, mock.patch.object(backend, "landed_state", counted)

    def test_a_carrier_proof_outlives_the_projection_that_derived_it(self):  # noqa: VACUOUS_ASSERTION — the empty second-pass call list has its unconditional positive control in the SAME arm, len(calls) == 1 on the first pass through the same spy on the same method; the rung cannot link them because the two passes are separate bindings, which is exactly what a before/after cache arm is
        """The whole point: the SECOND projection spawns nothing for this row.

        Both halves run the real `_landing_proofs` closure and the real
        backend. The first pass is the unconditional positive control — a
        second pass that spawns nothing proves a cache only if the first one
        is observed to spawn.
        """
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        tip = self.commit("b.txt")
        self.git("checkout", "-q", "-")
        owner, carrier = self.rows(tip)

        calls, patch = self.spy()
        with patch, projscope.scope():
            first = landreq._landing_proofs()(owner, carrier)
        self.assertEqual(first, landreq.PROOF_ABSENT, first)
        self.assertEqual(len(calls), 1,
                         "the first pass did not reach the real derivation, "
                         "so an empty second pass would prove nothing")
        self.assertEqual(
            [(r["tip"], r["proof"]) for r in self.carrier_proofs()],
            [(tip, landreq.PROOF_ABSENT)], self.carrier_proofs())

        calls, patch = self.spy()
        with patch, projscope.scope():
            second = landreq._landing_proofs()(owner, carrier)
        self.assertEqual(second, first)
        self.assertEqual(calls, [],
                         "the kept answer did not reach the reader: %r" % (calls,))

    def test_the_kept_answer_is_read_ABOVE_the_spent_budget(self):
        """A kept verdict costs no spawn, so a spent budget must not hide it.

        Charging a free answer against a bound meant for git is how the rows
        past the cutoff — the ones the ledger exists to serve — went on
        reading UNKNOWN.
        """
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        tip = self.commit("b.txt")
        self.git("checkout", "-q", "-")
        owner, carrier = self.rows(tip)
        with projscope.scope():
            self.assertEqual(landreq._landing_proofs()(owner, carrier),
                             landreq.PROOF_ABSENT)
        self.assertTrue(self.carrier_proofs())

        # POSITIVE CONTROL on the same spent budget: a row with NO kept answer
        # really does degrade, so the hit below is the ledger and not a budget
        # that failed to bite. IT IS COMMITTED ON A SECOND LANE, never on
        # trunk: committing on trunk MOVES the pin, which mints a new key for
        # the kept row too and makes this arm fail for the one reason it is
        # not about.
        self.git("checkout", "-q", "-b", "lane-two", base)
        other = self.commit("c.txt")
        self.git("checkout", "-q", "-")
        cold_owner, cold_carrier = self.rows(other)
        with mock.patch.object(landreq, "_derive_expired", lambda: True), \
                projscope.scope():
            proof = landreq._landing_proofs()
            self.assertEqual(proof(cold_owner, cold_carrier),
                             landreq.PROOF_UNKNOWN)
            self.assertEqual(proof(owner, carrier), landreq.PROOF_ABSENT)

    def test_UNKNOWN_is_never_written(self):
        """UNKNOWN is weather. Keeping it makes a timeout permanent truth."""
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        tip = self.commit("b.txt")
        self.git("checkout", "-q", "-")
        owner, carrier = self.rows(tip)
        with mock.patch.object(landreq, "_derive_expired", lambda: True), \
                projscope.scope():
            self.assertEqual(landreq._landing_proofs()(owner, carrier),
                             landreq.PROOF_UNKNOWN)
        self.assertEqual(self.carrier_proofs(), [])
        # AND THE WRITER REFUSES IT DIRECTLY, so the absence above is the law
        # and not merely a path that happened not to call the writer.
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tip, base,
                                        landreq.PROOF_UNKNOWN)
            self.assertEqual(self.carrier_proofs(), [])
            landreq._keep_carrier_proof(self.gitdir, tip, base,
                                        landreq.PROOF_ABSENT)
        self.assertEqual(len(self.carrier_proofs()), 1,
                         "the writer refuses everything, so the refusal above "
                         "says nothing about UNKNOWN")

    def commits(self, n):
        """n real commits on a lane, plus the trunk base. Real objects only:
        the writer refuses a repository it cannot see, and an arm that fed it
        synthetic ids would measure a refusal rather than the append path."""
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "many", base)
        tips = [self.commit("b%d.txt" % i) for i in range(n)]
        self.git("checkout", "-q", "-")
        return base, tips

    def parses(self):
        """Count ledger LINE parses, which is the amplification's own unit.

        The writer read the whole ledger to dedupe, appended, and the append moved (size, mtime_ns) — so
        the next lookup reparsed the entire retained file. Twelve new answers
        against 50k retained rows read 109.2 MB and cost 1.428s. Counting
        parses rather than timing makes the growth a fact rather than a
        measurement of this box.
        """
        calls = []
        real = json.loads

        def counted(text, *a, **kw):
            calls.append(1)
            return real(text, *a, **kw)

        return calls, mock.patch.object(landreq.json, "loads", counted)

    def test_appending_an_answer_does_not_reparse_the_whole_ledger(self):
        """N appends cost N parses, never N-squared.

        The memo is kept VALID across our own append — we know exactly what we
        wrote — instead of being invalidated and rebuilt from the file.
        """
        base, tips = self.commits(12)
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tips[0], base,
                                        landreq.PROOF_ABSENT)
        landreq._carrier_proof_ledger()          # warm the reader once
        calls, patch = self.parses()
        with patch, projscope.scope():
            for tip in tips[1:]:
                landreq._keep_carrier_proof(self.gitdir, tip, base,
                                            landreq.PROOF_ABSENT)
        # MUST-HIT: the writes really landed, or zero parses is trivially true.
        self.assertEqual(len(self.carrier_proofs()), 12, self.carrier_proofs())
        # ELEVEN APPENDS OVER A GROWING LEDGER. Reparsing would cost the sum
        # 1+2+...+11 = 66 line parses; keeping the memo costs none at all,
        # because nothing re-reads the file.
        self.assertLessEqual(len(calls), 11,
                             "%d line parses for 11 appends — the ledger is "
                             "being re-read per write" % len(calls))
        # AND THE MEMO STILL DESCRIBES THE FILE, which is what makes the next
        # reader cheap rather than merely making this arm's counter small.
        self.assertEqual(
            landreq._CARRIER_PROOF_MEMO.get("key"),
            landreq._ledger_key(landreq.carrier_proofs_path()),
            "the memo no longer describes the file, so the next reader "
            "reparses and the parse count above measured only this arm")
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, tips[-1], base),
            landreq.PROOF_ABSENT)

    def test_an_INTERLEAVED_writer_invalidates_the_memo_instead_of_being_lost(self):
        """Keeping the memo is only safe while the file is ours alone.

        If another process appended between our read and our write, our cached
        dict is genuinely missing their line. Silently keeping it would turn
        their write into a duplicate append on our next pass — so a size that
        is not exactly ours must clear.
        """
        base, tips = self.commits(3)
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tips[0], base,
                                        landreq.PROOF_ABSENT)
        landreq._carrier_proof_ledger()
        # ANOTHER WRITER, out of band, exactly as a second process would.
        foreign = {"repo": self.gitdir, "tip": tips[1], "trunk": base,
                   "proof": landreq.PROOF_ANCESTOR}
        with open(landreq.carrier_proofs_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(foreign, sort_keys=True) + "\n")
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tips[2], base,
                                        landreq.PROOF_ABSENT)
        # THE FOREIGN ANSWER SURVIVES AND IS READABLE. A writer that kept its
        # stale dict would have re-keyed the memo over the top of it.
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, tips[1], base),
            landreq.PROOF_ANCESTOR,
            "the interleaved writer's answer is invisible — the memo was "
            "kept across a write that was not ours")
        self.assertEqual(len(self.carrier_proofs()), 3, self.carrier_proofs())

    def test_the_commit_level_ledger_gets_the_SAME_cure(self):
        """The sibling writer has the identical shape and predates this one.

        It is lower volume today, which is exactly why nobody measured it; the
        carrier ledger is what made the class expensive enough to see. One
        helper, so the two cannot drift.
        """
        base, tips = self.commits(8)
        with projscope.scope():
            landreq._keep_landing_proof(self.gitdir, tips[0], base, "ancestor")
        landreq._land_proof_ledger()
        calls, patch = self.parses()
        with patch, projscope.scope():
            for tip in tips[1:]:
                landreq._keep_landing_proof(self.gitdir, tip, base, "ancestor")
        self.assertEqual(len(self.proofs()), 8, self.proofs())
        self.assertLessEqual(len(calls), 7,
                             "%d line parses for 7 appends — the commit-level "
                             "ledger is still re-read per write" % len(calls))
        self.assertEqual(
            landreq._kept_landing_proof(self.gitdir, tips[-1], base), "ancestor")

    def test_a_REPLACED_file_with_the_same_size_and_mtime_is_not_the_same_file(self):
        """Identity, not only contents-shaped metadata.

        An atomic rewrite-and-rename is the ordinary way to compact or repair
        an append-only ledger, and it is exactly the moment a stale cached map
        would be trusted. (size, mtime_ns) alone calls the replacement
        identical; the inode does not.
        """
        base, tips = self.commits(2)
        path = landreq.carrier_proofs_path()
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tips[0], base,
                                        landreq.PROOF_ANCESTOR)
        first = landreq._carrier_proof_ledger()
        self.assertEqual(len(first), 1, first)
        st = os.stat(path)
        # A DIFFERENT FILE carrying a DIFFERENT answer for the SAME key, padded
        # to the same byte count and with the mtime restored to the nanosecond.
        # Trailing space before the newline is JSON-legal and the reader strips
        # it, so the padding changes the bytes and not the record.
        other = dict(json.loads(open(path, encoding="utf-8").read().strip()),
                     proof=landreq.PROOF_ABSENT)
        body = json.dumps(other, sort_keys=True)
        pad = st.st_size - len(body.encode("utf-8")) - 1
        self.assertGreaterEqual(
            pad, 0, "MUST-HIT: the replacement cannot be padded DOWN to the "
                    "original size, so this arm is not testing a same-size "
                    "rewrite at all")
        line = body + " " * pad + "\n"
        self.assertEqual(len(line.encode("utf-8")), st.st_size,
                         "MUST-HIT: the replacement is a different SIZE, so "
                         "this arm would pass on size alone and prove nothing")
        swap = path + ".swap"
        with open(swap, "w", encoding="utf-8") as fh:
            fh.write(line)
        os.utime(swap, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(swap, path)
        after = os.stat(path)
        self.assertNotEqual(after.st_ino, st.st_ino,
                            "MUST-HIT: the replacement reused the inode, so "
                            "there is no identity change to detect")
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (st.st_size, st.st_mtime_ns),
                         "MUST-HIT: size or mtime moved, so the OLD key would "
                         "have caught this without the inode")
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, tips[0], base),
            landreq.PROOF_ABSENT,
            "the reader served its map of the REPLACED file — a same-size, "
            "same-mtime rewrite was treated as the same contents")

    def test_a_TORN_last_line_is_sealed_instead_of_being_fused_with(self):
        """A crashed writer leaves a line with no newline, and an append onto
        it FUSES both records into one unparseable line — losing the record we
        were writing as well as the one already there."""
        base, tips = self.commits(2)
        path = landreq.carrier_proofs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"repo": "%s", "tip": "%s", "trunk"' % (self.gitdir, tips[0]))
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tips[1], base,
                                        landreq.PROOF_ABSENT)
        # OUR record survives on its own line and is readable.
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, tips[1], base),
            landreq.PROOF_ABSENT,
            "our record was fused onto the torn line and lost")
        with open(path, encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2, lines)

    def test_a_ref_NAME_is_refused_at_both_doors(self):
        """A name is not an identity: it moves, and the proof would be reused.

        Both doors refuse independently, because several callers still carry
        trunk NAMES down this path and a census of them is not a guard.
        """
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        sha = self.commit("b.txt")
        self.git("checkout", "-q", "-")
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, sha, "main",
                                        landreq.PROOF_ABSENT)
            landreq._keep_carrier_proof(self.gitdir, "HEAD", sha,
                                        landreq.PROOF_ABSENT)
            self.assertEqual(self.carrier_proofs(), [])
            # POSITIVE CONTROL: the SAME writer takes the same two objects
            # spelled as full shas, so the two refusals above are about the
            # NAME and not about a writer that refuses everything.
            landreq._keep_carrier_proof(self.gitdir, sha, base,
                                        landreq.PROOF_ABSENT)
        self.assertEqual(len(self.carrier_proofs()), 1, self.carrier_proofs())
        # The READER refuses one that reached the file some other way.
        os.makedirs(os.path.dirname(landreq.carrier_proofs_path()), exist_ok=True)
        with open(landreq.carrier_proofs_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": sha,
                                 "trunk": "main",
                                 "proof": landreq.PROOF_ABSENT}) + "\n")
            fh.write(json.dumps({"repo": self.gitdir, "tip": sha,
                                 "trunk": "d" * 40,
                                 "proof": landreq.PROOF_ABSENT}) + "\n")
        landreq._CARRIER_PROOF_MEMO.clear()
        self.assertIsNone(landreq._kept_carrier_proof(self.gitdir, sha, "main"))
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, sha, "d" * 40),
            landreq.PROOF_ABSENT,
            "the reader dropped everything, so the refusal above is not "
            "about the ref name")

    def test_a_torn_line_and_an_unknown_word_never_blind_the_rest(self):
        good = {"repo": self.gitdir, "tip": "e" * 40, "trunk": "f" * 40,
                "proof": landreq.PROOF_PATCH_EQUIVALENT}
        os.makedirs(os.path.dirname(landreq.carrier_proofs_path()), exist_ok=True)
        with open(landreq.carrier_proofs_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"repo": self.gitdir, "tip": "e" * 40,
                                 "trunk": "0" * 40,
                                 "proof": "a-future-word"}) + "\n")
            fh.write("[]\n")
            fh.write(json.dumps(good) + "\n")
        landreq._CARRIER_PROOF_MEMO.clear()
        self.assertEqual(
            landreq._kept_carrier_proof(self.gitdir, "e" * 40, "f" * 40),
            landreq.PROOF_PATCH_EQUIVALENT)
        self.assertIsNone(
            landreq._kept_carrier_proof(self.gitdir, "e" * 40, "0" * 40),
            "a word this build does not keep became an answer by being present")

    def test_an_unreadable_ledger_reads_as_EMPTY_and_never_raises(self):
        """An unreadable ledger is EMPTY, never an exception out of a reader.

        The decode is inside the handler that falls back: iterating the file is
        where UTF-8 is decoded, so one invalid byte raises from the loop and a
        handler catching only OSError would let it escape.
        """
        os.makedirs(os.path.dirname(landreq.carrier_proofs_path()), exist_ok=True)
        good = {"repo": self.gitdir, "tip": "e" * 40, "trunk": "f" * 40,
                "proof": landreq.PROOF_ABSENT}
        # UNCONDITIONAL POSITIVE CONTROL on the same reader and the same path:
        # a readable ledger really does produce an entry, so the empty result
        # below is the invalid byte and not a reader that answers nothing.
        with open(landreq.carrier_proofs_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(good) + "\n")
        landreq._CARRIER_PROOF_MEMO.clear()
        self.assertEqual(landreq._carrier_proof_ledger(),
                         {(self.gitdir, "e" * 40, "f" * 40): landreq.PROOF_ABSENT})
        with open(landreq.carrier_proofs_path(), "wb") as fh:
            fh.write(b'{"repo": "x", "tip": "\xff\xfe", "trunk": "y"}\n')
        landreq._CARRIER_PROOF_MEMO.clear()
        self.assertEqual(landreq._carrier_proof_ledger(), {})

    def test_the_two_ledgers_are_SEPARATE_FILES_for_separate_questions(self):
        """One key shape, two predicates, and sharing a file would be silent.

        `_landing_proof` asks whether ONE COMMIT's change reached trunk;
        `proof_for` asks it of a whole CARRIER CHAIN, where one unlanded
        commit is decisive against the whole. The keys are identical in shape,
        so a shared file would answer one question with the other's verdict
        under a key that matched exactly.
        """
        self.assertNotEqual(landreq.carrier_proofs_path(),
                            landreq.land_proofs_path())
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        tip = self.commit("b.txt")
        self.git("checkout", "-q", "-")
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tip, base,
                                        landreq.PROOF_ABSENT)
        # MUST-HIT ON THE WRITER, then the separation: the carrier write
        # landed and the commit-level ledger did not grow an `absent` from it.
        self.assertEqual(len(self.carrier_proofs()), 1)
        self.assertEqual(
            [r["proof"] for r in self.proofs()
             if r.get("proof") == landreq.PROOF_ABSENT], [],
            "an absent carrier verdict reached the commit-level ledger")
        # AND THE COMMIT-LEVEL READER CANNOT SEE THE CARRIER VERDICT, while
        # its own writer still works on the same pair — so the None below is
        # the separation and not a reader that answers nothing.
        self.assertIsNone(landreq._kept_landing_proof(self.gitdir, tip, base))
        with projscope.scope():
            landreq._keep_landing_proof(self.gitdir, tip, base, "ancestor")
        self.assertEqual(landreq._kept_landing_proof(self.gitdir, tip, base),
                         "ancestor")


class CarrierProofCarriesForwardTest(LedgerBase):
    """A RELIEVING CARRIER VERDICT SURVIVES A FAST-FORWARD; A NEGATIVE DOES NOT.

    Trunk moves on every land and a moved trunk mints a new key for every row,
    so without a carry the whole warm-up retires at each land — and the
    verdicts it retires are the expensive ones: a relieving answer is the one
    that runs the whole patch-identity ladder instead of exiting at the first
    unlanded commit.

    The carry is sound in one direction only. Present at an ancestor implies
    present at every descendant, because a fast-forward only SHRINKS the range
    the verdict was proved over; absent at an ancestor implies nothing at all.
    """

    def pairs(self):
        path = landreq.ancestry_pairs_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def lane(self, name, base):
        self.git("checkout", "-q", "-b", name, base)
        tip = self.commit(name + ".txt")
        self.git("checkout", "-q", "-")
        return tip

    def test_a_RELIEVING_verdict_carries_forward_and_ABSENT_does_not(self):
        """LOAD-BEARING MUTATION: drop the carried-forward loop in
        `_kept_carrier_proof`.
          -> AssertionError: a relieving verdict did not survive the land
        """
        base = self.commit("a.txt")
        landed, unlanded = self.lane("landed", base), self.lane("open", base)
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, landed, base,
                                        landreq.PROOF_PATCH_EQUIVALENT)
            landreq._keep_carrier_proof(self.gitdir, unlanded, base,
                                        landreq.PROOF_ABSENT)
        moved = self.commit("c.txt")            # trunk fast-forwards past base
        landreq._CARRIER_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        # THE EXACT KEY ANSWERS BOTH, which is what makes every absence below
        # a statement about the CARRY rather than about an empty ledger.
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, landed,
                                                     base),
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, unlanded,
                                                     base),
                         landreq.PROOF_ABSENT)
        # THE HOP IS READ AND NEVER COMPUTED: with no pair recorded there is
        # no carry, so the arm below measures the writer and not a coincidence.
        self.assertIsNone(landreq._kept_carrier_proof(self.gitdir, landed,
                                                      moved))
        landreq._teach_trunk_pairs(self.gitdir, moved)
        landreq._ANCESTRY_MEMO.clear()
        self.assertEqual([(r.get("older"), r.get("newer"), r.get("anc"))
                          for r in self.pairs()], [(base, moved, True)],
                         "the teach wrote no pair for the CARRIER ledger's "
                         "trunk, so the answer below would say nothing")
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, landed,
                                                     moved),
                         landreq.PROOF_PATCH_EQUIVALENT,
                         "a relieving verdict did not survive the land")
        # THE DIRECTION THAT MUST NOT CARRY, over the same recorded hop.
        self.assertIsNone(landreq._kept_carrier_proof(self.gitdir, unlanded,
                                                      moved),
                          "an ABSENT verdict was carried onto a trunk it was "
                          "never proved against")

    def test_a_REWRITTEN_trunk_carries_nothing(self):
        """The carry's one falsifier is a history rewrite, and it is not a
        silent hazard: the older trunk stops being an ancestor, the recorded
        pair says so, and the entry simply stops matching."""
        base = self.commit("a.txt")
        landed = self.lane("landed", base)
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, landed, base,
                                        landreq.PROOF_ANCESTOR)
        self.git("checkout", "-q", "--orphan", "rewritten")
        self.git("rm", "-q", "-rf", ".")
        rewritten = self.commit("d.txt")
        landreq._CARRIER_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        landreq._teach_trunk_pairs(self.gitdir, rewritten)
        landreq._ANCESTRY_MEMO.clear()
        self.assertEqual([(r.get("older"), r.get("newer"), r.get("anc"))
                          for r in self.pairs()], [(base, rewritten, False)],
                         "the teach decided no pair, so the refusal below is "
                         "about an empty ledger rather than about the rewrite")
        self.assertIsNone(landreq._kept_carrier_proof(self.gitdir, landed,
                                                      rewritten))
        # THE CONTROL: on a trunk the base DOES reach, the same entry carries.
        self.git("checkout", "-q", "-")
        moved = self.commit("c.txt")
        landreq._teach_trunk_pairs(self.gitdir, moved)
        landreq._ANCESTRY_MEMO.clear()
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, landed,
                                                     moved),
                         landreq.PROOF_ANCESTOR)


    def test_a_carried_PATCH_EQUIVALENT_can_be_the_weaker_word(self):
        """WHAT THE CARRY DOES NOT PROMISE, pinned so nobody re-derives it.

        RELIEF carries; the WORD does not. A verdict taken as
        `patch-equivalent` against trunk `t` still relieves at a descendant
        `ref`, but if the move brought the tip OBJECT itself onto trunk, the
        live ladder would now print `ancestor` and the carry prints the older
        word. It is still true of the three ids — the patch is in trunk's
        history — and it is not the strongest word git would give.

        This arm exists so the gap is a measured fact with a reproduction
        rather than a sentence in a docstring, and so that closing it (by
        asking the free ancestry batch before the carry) has a red arm to
        turn green.

        LOAD-BEARING MUTATION: make the carry refuse `patch-equivalent`.
          -> AssertionError: the carry stopped serving a relieving verdict
        """
        from helm import vcs                   # the live ladder, as control
        base = self.commit("a.txt")
        self.git("checkout", "-q", "-b", "lane", base)
        tip = self.commit("work.txt", body="carried")
        self.git("checkout", "-q", "-")
        # TRUNK MOVES FIRST, AND THAT IS NOT DECORATION. Replaying the tip
        # onto its OWN parent reproduces every input to the commit hash —
        # tree, parent, author, message, and the preserved author date — so
        # `cherry-pick` hands back the SAME OBJECT and there is no replay to
        # measure. One unrelated commit ahead of it makes the replay a
        # different object, which is the state this arm is about.
        self.commit("ahead.txt", body="trunk moved")
        # THE LAND THIS REPOSITORY ACTUALLY PERFORMS: the patch is replayed
        # onto trunk under a different object, so ancestry says no and patch
        # identity says yes.
        self.git("cherry-pick", tip)
        replayed = self.git("rev-parse", "HEAD")
        backend = vcs.backend(self.repo)
        self.assertEqual(backend.landed_state(self.repo, tip, replayed),
                         vcs.PATCH_EQUIVALENT,
                         "the fixture did not build the state this arm is "
                         "about, so everything below would be vacuous")
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tip, replayed,
                                        landreq.PROOF_PATCH_EQUIVALENT)
        # AND THEN A MERGE PUTS THE REVIEWED OBJECT ITSELF ON TRUNK.
        self.git("merge", "-q", "--no-ff", "-m", "merge lane", "lane")
        merged = self.git("rev-parse", "HEAD")
        self.assertEqual(backend.landed_state(self.repo, tip, merged),
                         vcs.ANCESTOR,
                         "the second half of the fixture did not take")
        landreq._CARRIER_PROOF_MEMO.clear()
        landreq._ANCESTRY_MEMO.clear()
        landreq._teach_trunk_pairs(self.gitdir, merged)
        landreq._ANCESTRY_MEMO.clear()
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, tip,
                                                     merged),
                         landreq.PROOF_PATCH_EQUIVALENT,
                         "the carry stopped serving a relieving verdict")

    def test_a_proof_kept_THIS_PASS_carries_without_a_re_read(self):
        """The writer's own append must reach the relieving index too.

        `_append_kept` re-keys the memo to the POST-append stat, so an index
        the writer does not update is not repaired by a later read either: it
        is stamped current while missing the line just written. The FIRST
        write cannot show it — there is no memo to keep, so the writer clears
        and the next read rebuilds — which is why this arm writes twice and
        asks about the second tip.

        LOAD-BEARING MUTATION: drop the relieving half of `_carrier_insert`.
          -> AssertionError: a proof kept in this pass did not carry
        """
        base = self.commit("a.txt")
        first, second = self.lane("first", base), self.lane("second", base)
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, first, base,
                                        landreq.PROOF_PATCH_EQUIVALENT)
            # THE REBUILD THAT PUTS A MEMO IN HAND, so the write below takes
            # the append path this arm is about rather than the clearing one.
            landreq._carrier_proof_ledger()
            held = landreq._CARRIER_PROOF_MEMO.get("key")
            landreq._keep_carrier_proof(self.gitdir, second, base,
                                        landreq.PROOF_PATCH_EQUIVALENT)
        self.assertNotEqual(landreq._CARRIER_PROOF_MEMO.get("key"), held,
                            "the memo was cleared rather than kept over the "
                            "append, so this arm would measure a re-read")
        moved = self.commit("c.txt")
        landreq._teach_trunk_pairs(self.gitdir, moved)
        landreq._ANCESTRY_MEMO.clear()
        # THE CONTROL: the tip written before the memo existed carries, so a
        # failure below is about the writer's index and not about the teach.
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, first,
                                                     moved),
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertEqual(landreq._kept_carrier_proof(self.gitdir, second,
                                                     moved),
                         landreq.PROOF_PATCH_EQUIVALENT,
                         "a proof kept in this pass did not carry")


class TrunkCoverTest(LedgerBase):
    def test_a_commit_with_NO_PATCH_is_covered_rather_than_blocking(self):  # noqa: VACUOUS_ASSERTION — the empty-frontier assertion is paired with unconditional positives on the same record: newest at the tip, every commit seen, ids non-empty
        root = self.commit("a.txt")
        self.commit(empty=True)
        top = self.commit("b.txt")
        middle = self.git("rev-parse", top + "^")
        # THE STATE, NOT ITS PROJECTION: an empty commit is EMPTY_RANGE — git
        # answered and there is no diff — which is a different fact from
        # UNMEASURED, and collapsing the two to None is what let an
        # unmeasurable commit settle as covered.
        self.assertEqual(landreq._measured_patch_id(self.gitdir, middle),
                         (landreq.EMPTY_RANGE, None))
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
        rec = self.cover()
        self.assertEqual(rec.get("newest"), top)
        self.assertEqual(rec.get("frontier"), [],
                         "the walk stopped at the patchless commit — a commit "
                         "git ANSWERED about must be covered even when it "
                         "yields no patch id")
        self.assertEqual(sorted(rec.get("seen") or []),
                         sorted([root, middle, top]))
        self.assertFalse(incomplete)
        self.assertTrue(ids)

    def test_a_MERGE_covers_BOTH_parents_before_it_completes(self):  # noqa: VACUOUS_ASSERTION — the empty-frontier assertion is paired with unconditional positives on the same record: both parents seen and both their patch ids present
        root = self.commit("root.txt")
        self.git("checkout", "-q", "-b", "left", root)
        left = self.commit("left.txt", body="left only")
        self.git("checkout", "-q", "-b", "right", root)
        right = self.commit("right.txt", body="right only")
        self.git("merge", "--no-edit", "-q", "left")
        top = self.git("rev-parse", "HEAD")
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
        rec = self.cover()
        self.assertFalse(incomplete, "a merge cover declared itself complete "
                                     "with a frontier still open")
        self.assertEqual(rec.get("frontier"), [])
        seen = set(rec.get("seen") or ())
        # BOTH SIDES, and this is the counterexample a single oldest cursor
        # cannot survive: stop inside the merge and one parent's history is
        # never walked, while the cover calls itself complete.
        self.assertIn(left, seen)
        self.assertIn(right, seen)
        self.assertIn(root, seen)
        self.assertIn(landreq._patch_id(self.gitdir, left), ids)
        self.assertIn(landreq._patch_id(self.gitdir, right), ids)

    def test_a_git_that_did_NOT_answer_leaves_the_cover_INCOMPLETE(self):  # noqa: VACUOUS_ASSERTION — the empty-record assertion is followed, in the same pass on the same repository and the same file, by an unconditional control asserting the record IS written and complete once git can answer
        self.commit("a.txt")
        self.commit("b.txt")
        top = self.commit("c.txt")

        with mock.patch.object(landreq, "_commit_parents",
                               lambda *_a, **_k: None):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
        self.assertTrue(incomplete)
        self.assertFalse(ids)
        self.assertEqual(self.cover(), {},
                         "an unreadable first commit wrote a cover record")
        # THE CONTROL, ON THE SAME OBSERVABLE: unblinded, the identical call
        # writes a record and completes.
        landreq._TRUNK_INDEX_MEMO.clear()
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
        self.assertTrue(ids)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("newest"), top)

    def test_incomplete_stays_TRUE_until_the_frontier_EMPTIES(self):  # noqa: VACUOUS_ASSERTION — the assertFalse(incomplete) that ends the loop is paired with unconditional positives on the same cover: ids non-empty, frontier empty, newest at the tip, and every commit seen
        shas = [self.commit("f%d.txt" % i) for i in range(6)]
        top = shas[-1]
        with mock.patch.object(landreq, "TRUNK_PID_BACKFILL", 1):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
            self.assertTrue(incomplete,
                            "a cover with an open frontier reported complete")
            self.assertTrue(self.cover().get("frontier"))
            landreq._TRUNK_INDEX_MEMO.clear()
            seen = len(self.cover().get("seen") or ())
            for _ in range(12):
                ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
                landreq._TRUNK_INDEX_MEMO.clear()
                grew = len(self.cover().get("seen") or ())
                self.assertGreaterEqual(grew, seen,
                                        "the cover SHRANK between passes")
                seen = grew
                if not incomplete:
                    break
        self.assertFalse(incomplete, "the backfill never emptied the frontier")
        self.assertTrue(ids, "a complete cover holds no ids at all")
        self.assertEqual(self.cover().get("frontier"), [])
        self.assertEqual(self.cover().get("newest"), top)
        self.assertEqual(len(self.cover().get("seen") or ()), len(shas))

    def test_the_front_extends_to_a_MOVED_trunk_without_rehashing(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn on the hash spy is paired with an unconditional assertIn on that same spy, so an empty spy fails the arm rather than passing it
        root = self.commit("a.txt")
        settled = self.commit("b.txt")
        landreq._trunk_index_extend(self.gitdir, settled)
        landreq._TRUNK_INDEX_MEMO.clear()
        before = dict(self.cover().get("ids") or {})
        self.assertEqual(sorted(self.cover().get("seen") or ()),
                         sorted([root, settled]))

        moved = self.commit("c.txt")
        spy = []
        real = landreq._measured_patch_id

        def counting(gitdir, sha, *a, **k):
            spy.append(sha)
            return real(gitdir, sha, *a, **k)

        with mock.patch.object(landreq, "_measured_patch_id", counting):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, moved)
        self.assertEqual(self.cover().get("newest"), moved)
        self.assertFalse(incomplete)
        self.assertTrue(set(before).issubset(set(ids)),
                        "extending the front dropped ids the cover already had")
        self.assertNotIn(settled, spy,
                         "a commit already covered was re-hashed — the cover "
                         "is being rebuilt rather than extended")
        self.assertNotIn(root, spy)
        self.assertIn(moved, spy)

    def test_a_cover_is_REBUILT_for_a_trunk_it_does_not_describe(self):  # noqa: VACUOUS_ASSERTION — the assertNotIn carries a must-hit proving the id WAS in the cover before the rebuild, and is followed by unconditional positives on the same record: ids non-empty, seen exactly the two ancestors, newest at the requested trunk
        # `first` must NOT be the repository root: a root contributes no id
        # under this instrument, so a cover rebuilt at one is legitimately
        # empty and the arm would be asserting about nothing.
        root = self.commit("root.txt")
        first = self.commit("a.txt")
        ahead = self.commit("b.txt")
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, ahead)
        self.assertFalse(incomplete)
        ahead_id = landreq._patch_id(self.gitdir, ahead)
        self.assertIn(ahead_id, ids,
                      "MUST-HIT: the cover never held the id whose absence "
                      "the rest of this arm is about")
        landreq._TRUNK_INDEX_MEMO.clear()

        # THE DEFECT THIS PINS, and it is the one the superseded close door
        # turned into a wrong terminal: a cover built at `ahead` holds an id
        # for a commit `first` does not contain, and `rev-list first..ahead`
        # asked the other way round is EMPTY, so nothing in the walk itself
        # notices. Only an ancestry answer separates behind from descendant.
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, first)
        self.assertFalse(incomplete)
        self.assertNotIn(ahead_id, ids,
                         "the cover answered about a trunk it does not "
                         "describe, carrying a foreign patch id into it")
        self.assertTrue(ids, "the rebuilt cover holds nothing at all")
        self.assertEqual(sorted(self.cover().get("seen") or ()),
                         sorted([root, first]))
        self.assertEqual(self.cover().get("newest"), first)

    def test_a_rival_write_yields_NO_index_for_that_pass(self):  # noqa: VACUOUS_ASSERTION — the None-index assertion is followed in the same pass by an unconditional control, no rival, that returns ids and writes the record
        first = self.commit("a.txt")
        head = self.commit("b.txt")
        landreq._trunk_index_extend(self.gitdir, head)
        landreq._TRUNK_INDEX_MEMO.clear()
        mine = dict(self.cover())
        self.assertTrue(mine.get("ids"))

        # A RIVAL PASS LANDS BETWEEN THE READ AND THE WRITE, carrying an id
        # this repository never produced and a frontier of its own. The losing
        # side must hand back NOTHING: the stored record may describe another
        # target, and returning it would let that record answer a question it
        # was never valid for — the forbidden union arriving through the write
        # path instead of the read one.
        rival_id = "f" * 40
        rival = {"newest": head, "frontier": [first], "seen": [head],
                 "ids": dict(mine["ids"], **{rival_id: head})}
        reads = []

        def read_once_then_rival():
            reads.append(1)
            return {self.gitdir: dict(mine) if len(reads) == 1
                    else dict(rival)}

        moved = self.commit("c.txt")
        with mock.patch.object(landreq, "_trunk_index_read",
                               read_once_then_rival):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, moved)
        self.assertGreater(len(reads), 1,
                           "the write never re-read, so the rival never "
                           "existed for this arm")
        self.assertIsNone(ids, "the losing side answered from a cover it did "
                               "not write")
        self.assertTrue(incomplete)
        # THE CONTROL, same call shape with no rival: the extension IS kept
        # and the pass answers.
        landreq._TRUNK_INDEX_MEMO.clear()
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, moved)
        self.assertTrue(ids)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("newest"), moved)

    def test_a_MERGE_is_hashed_and_never_settled_by_a_default_SILENCE(self):  # noqa: VACUOUS_ASSERTION — the default-diff-tree silence IS the trap being named and carries its own must-hit; the arm then asserts unconditionally that the merge patch id is present
        root = self.commit("root.txt")
        self.git("checkout", "-q", "-b", "left", root)
        self.commit("left.txt", body="left only")
        self.git("checkout", "-q", "-b", "right", root)
        self.commit("right.txt", body="right only")
        self.git("merge", "--no-edit", "-q", "left")
        merge = self.git("rev-parse", "HEAD")
        # THE MUST-HIT THAT NAMES THE TRAP: a DEFAULT diff-tree prints nothing
        # for this merge, so a probe asking that question would settle it as
        # patchless while its first-parent patch — the one `_patch_id` and the
        # lookup both use — is real and non-empty.
        silent = subprocess.run(("git", "diff-tree", "--no-commit-id",
                                 "--name-only", "-r", merge),
                                cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(silent.returncode, 0)
        self.assertEqual(silent.stdout.strip(), "",
                         "this git does NOT suppress the merge diff, so the "
                         "arm below is not about the trap it names")
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, merge)
        self.assertFalse(incomplete)
        self.assertIn(landreq._patch_id(self.gitdir, merge), ids,
                      "the merge was settled without its patch being measured")

    def test_a_ROOT_is_HASHED_by_the_root_aware_instrument(self):  # noqa: VACUOUS_ASSERTION — the empty-frontier assertion is paired with unconditional positives on the same cover: the measured state is HASHED, the id is non-empty, the root is the only seen entry, and that id is asserted present in the returned index
        root = self.commit("only.txt")
        # THE MUST-HIT, AND THE PROPERTY IT PINS IS THE WHOLE POINT: the
        # root-aware instrument hashes a parentless commit against the empty
        # tree, so a root carries a real id exactly like any other commit.
        # `_patch_id` ANSWERS HERE TOO — it routes to the same root-aware
        # helper and hands back the real id — so nothing about a root is
        # unreachable. What the cover needs from this call is the STATE, not
        # the id: HASHED and EMPTY_RANGE are different settlements and only
        # the tri-state carries the difference.
        self.assertEqual(landreq._patch_id(self.gitdir, root),
                         landreq._measured_patch_id(self.gitdir, root)[1],
                         "the two spellings disagree about a root, so the "
                         "comment above is describing a tree that is not this "
                         "one")
        state, pid = landreq._measured_patch_id(self.gitdir, root)
        self.assertEqual(state, landreq.HASHED)
        self.assertTrue(pid)
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, root)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("frontier"), [])
        self.assertEqual(self.cover().get("seen"), [root])
        self.assertIn(pid, ids, "the root's measured id never reached the cover")

    def test_an_UNMEASURED_patch_never_settles_a_commit(self):  # noqa: VACUOUS_ASSERTION — the empty-record assertion is followed in the same pass by an unconditional control with the real instrument that writes the record and completes
        head = self.commit("a.txt")
        with mock.patch.object(landreq, "_measured_patch_id",
                               lambda *_a, **_k: (landreq.UNMEASURED, None)):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertTrue(incomplete)
        self.assertFalse(ids)
        self.assertEqual(self.cover(), {},
                         "a commit whose patch could not be measured settled "
                         "as covered, which is how a miss becomes a false "
                         "ABSENT one layer along")
        # THE CONTROL on the same observable: the identical call with the real
        # instrument writes the record and completes.
        landreq._TRUNK_INDEX_MEMO.clear()
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertTrue(ids)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("newest"), head)

    def test_an_INTERRUPTED_forward_walk_stores_no_cover_it_did_not_close(self):  # noqa: VACUOUS_ASSERTION — the sibling-id absence carries a must-hit proving that id WAS in the earlier cover, and the interrupted call's refusal is asserted unconditionally before the later lookup
        root = self.commit("root.txt")
        self.git("checkout", "-q", "-b", "left", root)
        left = self.commit("left.txt", body="left only")
        self.git("checkout", "-q", "-b", "right", root)
        right = self.commit("right.txt", body="right only")
        self.git("merge", "--no-edit", "-q", "left")
        merge = self.git("rev-parse", "HEAD")

        # A COVER THAT REACHED ONLY THE LEFT CHILD.
        self.git("checkout", "-q", "left")
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, left)
        self.assertFalse(incomplete)
        left_id = landreq._patch_id(self.gitdir, left)
        self.assertIn(left_id, ids, "MUST-HIT: the left id never entered the "
                                    "cover, so its absence below is free")
        landreq._TRUNK_INDEX_MEMO.clear()

        # NOW THE TRUNK IS THE MERGE, and the walk is cut off after ONE commit
        # so it covers the right child and never arrives at the merge.
        calls = []

        def expired_after_two():
            calls.append(1)
            return len(calls) > 2

        with mock.patch.object(landreq, "_derive_expired", expired_after_two):
            got, incomplete = landreq._trunk_index_extend(self.gitdir, merge)
        self.assertIsNone(got)
        self.assertTrue(incomplete)
        landreq._TRUNK_INDEX_MEMO.clear()

        # AND THE LATER QUESTION IS THE ONE THAT MATTERS, not the refusal
        # above: asking about the RIGHT child must not be answered out of a
        # record whose newest was retargeted mid-walk while the left child's
        # ids stayed in it. `right` does not contain `left`.
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, right)
        # UNCONDITIONAL, both branches. Answering NOTHING is the safe
        # direction, so an arm that only checks the ids WHEN there are ids can
        # pass by never producing any — and then it proves nothing about the
        # state it exists to pin. Either the cover answers about `right` and
        # carries no sibling id, or it answers nothing and stores no sibling
        # evidence; there is no third outcome this arm will accept.
        self.assertNotIn(left, self.cover().get("seen") or (),
                         "a sibling's commit was left in the cover for a "
                         "trunk that does not contain it")
        self.assertNotIn(left_id, (self.cover().get("ids") or {}),
                         "a sibling's patch id was left in the stored cover")
        if ids is None:
            self.assertTrue(incomplete)
        else:
            self.assertNotIn(left_id, ids,
                             "a sibling's patch id answered about a trunk "
                             "that does not contain it")

    def test_a_TRUNCATED_commit_header_settles_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty-record assertion is followed in the same pass by an unconditional control, unblinded, that parses the header and writes the record
        head = self.commit("a.txt")
        real = landreq._git

        def truncated(gitdir, *args, **kw):
            got = real(gitdir, *args, **kw)
            # `_object_view` puts its isolation flags FIRST, so the verb is
            # not args[0] any more — matching on position would
            # silently never fire and the arm would pass on a mock
            # that never intercepted anything.
            if "cat-file" in args and "commit" in args:
                # The header block arrives WITHOUT its blank delimiter, which
                # is exactly what a short read looks like — and a prefix with
                # no parent line in it is not a parentless commit.
                text = (got.stdout or "").partition("\n\n")[0]
                return type(got)(args=got.args, returncode=0,
                                 stdout=text, stderr=got.stderr)
            return got

        with mock.patch.object(landreq, "_git", truncated):
            self.assertIsNone(landreq._commit_parents(self.gitdir, head))
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertTrue(incomplete)
        self.assertFalse(ids)
        self.assertEqual(self.cover(), {})
        # THE CONTROL on the same observable, unblinded and in the same pass.
        landreq._TRUNK_INDEX_MEMO.clear()
        self.assertEqual(landreq._commit_parents(self.gitdir, head), [])
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("newest"), head)

    def test_the_backfill_YIELDS_when_the_budget_is_spent(self):  # noqa: VACUOUS_ASSERTION — the open-frontier assertion is paired, in the same pass on the same repository, with an unconditional control that empties that same frontier once the budget is fresh
        shas = [self.commit("f%d.txt" % i) for i in range(6)]
        top = shas[-1]
        calls = []

        def expired_after_two():
            calls.append(1)
            return len(calls) > 2

        with mock.patch.object(landreq, "_derive_expired", expired_after_two):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
        self.assertTrue(incomplete,
                        "a cover cut short by the budget said complete")
        self.assertTrue(ids, "the interrupted pass banked nothing at all")
        self.assertTrue(self.cover().get("frontier"),
                        "the walk ran past the spent budget to the end")
        # THE CONTROL: the identical call with a budget that never expires
        # empties the frontier, so the stop above was the BUDGET and not the
        # walk being unable to continue.
        landreq._TRUNK_INDEX_MEMO.clear()
        for _ in range(12):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, top)
            landreq._TRUNK_INDEX_MEMO.clear()
            if not incomplete:
                break
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("frontier"), [])


class MalformedRecordTest(LedgerBase):
    def test_a_record_that_does_not_VALIDATE_is_dropped_whole(self):  # noqa: VACUOUS_ASSERTION — the must-hit above the loop reads the well-formed record back unconditionally, so an empty result from the reader fails the arm rather than passing it
        head = self.commit("a.txt")
        good = {"newest": head, "frontier": [], "seen": [head],
                "ids": {"a" * 40: head}}
        # THE MUST-HIT FIRST: the well-formed record IS read, so every refusal
        # below is about the shape and not about a reader that reads nothing.
        self.write_cover(good)
        self.assertEqual(landreq._trunk_index_read().get(self.gitdir, {})
                         .get("newest"), head)
        for label, bad in (
                ("a list where a dict belongs", []),
                ("null", None),
                ("no newest", {"frontier": [], "seen": [], "ids": {}}),
                ("a ref name as newest", dict(good, newest="main")),
                ("a frontier that is not a list", dict(good, frontier="x")),
                ("a non-sha in the frontier", dict(good, frontier=["main"])),
                ("a non-sha in seen", dict(good, seen=["nope"])),
                ("ids that is not a dict", dict(good, ids=[])),
                ("a non-hex id key", dict(good, ids={"zz": head})),
                ("a non-sha id value", dict(good, ids={"a" * 40: "main"})),
        ):
            with self.subTest(shape=label):
                self.write_cover(bad)
                self.assertEqual(landreq._trunk_index_read(), {},
                                 "a record shaped %r was believed" % label)

    def test_a_PRESENT_NONCOMMIT_is_not_an_admissible_key(self):  # noqa: VACUOUS_ASSERTION — the empty-proofs assertion carries a must-hit proving the tree object is PRESENT, and is followed by an unconditional control that does write
        head = self.commit("a.txt")
        tree = self.git("rev-parse", "HEAD^{tree}")
        # THE MUST-HIT: this object really is present, so the refusal below is
        # about it not being a COMMIT and not about it being missing.
        self.assertIs(landreq._object_exists(self.gitdir, tree), True)
        landreq._keep_landing_proof(self.gitdir, tree, head, "ancestor")
        self.assertEqual(self.proofs(), [],
                         "a TREE was admitted as a landing-proof key — bare "
                         "existence is not the commit-readability promised")
        # THE CONTROL: the identical call with two commits DOES write.
        landreq._LAND_PROOF_MEMO.clear()
        first = self.git("rev-parse", "HEAD")
        landreq._keep_landing_proof(self.gitdir, first, head, "ancestor")
        self.assertEqual([r.get("tip") for r in self.proofs()], [first])

    def test_a_SHORT_id_cannot_enter_the_cover(self):  # noqa: VACUOUS_ASSERTION — the empty-read assertion is preceded by an unconditional must-hit reading the well-formed record back from the same file
        head = self.commit("a.txt")
        good = {"newest": head, "frontier": [], "seen": [head],
                "ids": {"a" * 40: head}}
        self.write_cover(good)
        self.assertTrue(landreq._trunk_index_read().get(self.gitdir),
                        "MUST-HIT: the well-formed record was not read, so "
                        "the refusal below is about nothing")
        # An abbreviated id is a key nothing git emits can ever match, while
        # still counting toward a cover that calls itself COMPLETE and so
        # licenses ABSENT.
        self.write_cover(dict(good, ids={"abcdef12": head}))
        self.assertEqual(landreq._trunk_index_read(), {})

    def test_an_UNDECODABLE_proof_ledger_reads_as_EMPTY(self):  # noqa: VACUOUS_ASSERTION — the empty-ledger assertion is followed in the same pass by an unconditional control reading a real proof back from the same path
        first = self.commit("a.txt")
        head = self.commit("b.txt")
        os.makedirs(os.path.dirname(landreq.land_proofs_path()), exist_ok=True)
        with open(landreq.land_proofs_path(), "wb") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": head,
                                 "proof": "ancestor"}).encode() + b"\n")
            fh.write(b"\xff\xfe not utf-8 at all\n")
        landreq._LAND_PROOF_MEMO.clear()
        self.assertEqual(landreq._land_proof_ledger(), {},
                         "an undecodable byte escaped the fallback instead of "
                         "reading as an empty ledger")
        # THE CONTROL: the same file without the bad byte IS read.
        landreq._LAND_PROOF_MEMO.clear()
        with open(landreq.land_proofs_path(), "wb") as fh:
            fh.write(json.dumps({"repo": self.gitdir, "tip": first,
                                 "trunk": head,
                                 "proof": "ancestor"}).encode() + b"\n")
        self.assertEqual(landreq._kept_landing_proof(self.gitdir, first, head),
                         "ancestor")

    def test_a_BARE_parent_line_refuses_the_whole_read(self):  # noqa: VACUOUS_ASSERTION — the None assertion is followed in the same pass by an unconditional control parsing the same header unblinded
        head = self.commit("a.txt")
        real = landreq._git

        def mangled(gitdir, *args, **kw):
            got = real(gitdir, *args, **kw)
            # `_object_view` puts its isolation flags FIRST, so the verb is
            # not args[0] any more — matching on position would
            # silently never fire and the arm would pass on a mock
            # that never intercepted anything.
            if "cat-file" in args and "commit" in args:
                header, sep, rest = (got.stdout or "").partition("\n\n")
                return type(got)(args=got.args, returncode=0,
                                 stdout=header + "\nparent" + sep + rest,
                                 stderr=got.stderr)
            return got

        with mock.patch.object(landreq, "_git", mangled):
            self.assertIsNone(landreq._commit_parents(self.gitdir, head),
                              "a parent line with no id was read as evidence")
        # THE CONTROL, same call unblinded and in the same pass.
        self.assertEqual(landreq._commit_parents(self.gitdir, head), [])

    def test_a_TREE_LINE_THAT_IS_NOT_AN_ID_refuses_the_whole_read(self):  # noqa: VACUOUS_ASSERTION — the None assertion is followed in the same pass by an unconditional control parsing the same header unblinded
        head = self.commit("a.txt")
        real = landreq._git

        def mangled(gitdir, *args, **kw):
            got = real(gitdir, *args, **kw)
            # `_object_view` puts its isolation flags FIRST, so the verb is
            # not args[0] any more — matching on position would
            # silently never fire and the arm would pass on a mock
            # that never intercepted anything.
            if "cat-file" in args and "commit" in args:
                header, sep, rest = (got.stdout or "").partition("\n\n")
                lines = header.splitlines()
                lines[0] = "tree not-an-object-id"
                return type(got)(args=got.args, returncode=0,
                                 stdout="\n".join(lines) + sep + rest,
                                 stderr=got.stderr)
            return got

        with mock.patch.object(landreq, "_git", mangled):
            self.assertIsNone(landreq._commit_parents(self.gitdir, head),
                              "a header whose tree line is nonsense was "
                              "accepted as a commit header")
        self.assertEqual(landreq._commit_parents(self.gitdir, head), [])

    def test_a_HASHED_state_with_an_UNUSABLE_id_settles_nothing(self):  # noqa: VACUOUS_ASSERTION — the empty-record assertion is followed in the same pass by an unconditional control with the real instrument that writes the record and completes
        head = self.commit("a.txt")
        # The parser labels ANY non-empty first output token HASHED — that is
        # its own contract and right for its other callers. An id this reader
        # cannot use is not a smaller answer, it is NO answer, and omitting it
        # while marking the commit covered is how a trunk one of whose commits
        # was never indexed still empties its frontier and licenses ABSENT.
        with mock.patch.object(landreq, "_measured_patch_id",
                               lambda *_a, **_k: (landreq.HASHED, "abcdef12")):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertTrue(incomplete)
        self.assertFalse(ids)
        self.assertEqual(self.cover(), {},
                         "a commit whose id could not be used settled anyway")
        # THE CONTROL on the same observable: the real instrument writes the
        # record and completes, so the refusal above is about the id and not
        # about a cover that never works.
        landreq._TRUNK_INDEX_MEMO.clear()
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, head)
        self.assertTrue(ids)
        self.assertFalse(incomplete)
        self.assertEqual(self.cover().get("newest"), head)

    def test_an_UNUSABLE_id_leaves_the_answer_UNKNOWN_beside_a_valid_one(self):  # noqa: VACUOUS_ASSERTION — every assertion here is positive or comparative: a valid id IS present in the same index, a covered subject receives patch-equivalent from that same incomplete cover, the incomplete flag is asserted true, and the same absent tip answers ABSENT under a complete cover as the control
        """THE COUNTERSIGNED COUNTEREXAMPLE, whole rather than in outline.

        One commit measures cleanly and enters the index; a second answers
        HASHED with an id this reader cannot use. The index is therefore
        NON-EMPTY and its frontier could otherwise empty, which is exactly the
        shape where an omission turns into a false ABSENT: a tip whose patch is
        genuinely not on this trunk must still answer UNKNOWN, because one
        commit of that trunk was never indexed at all.

        AND THE ANSWER-SIDE HALF, ASKED OF THE SAME COVER: incompleteness
        downgrades a MISS and must not downgrade a HIT, so a non-ancestor
        subject whose patch the cover DOES hold still receives the real
        positive. Asserting that the good id reached the index is a claim
        about the INDEX; this is the claim about the ANSWER.
        """
        first = self.commit("a.txt")
        second = self.commit("b.txt")
        self.git("checkout", "-q", "-b", "side", first)
        off = self.commit("side.txt", body="never on the trunk")
        # A COPY OF THE COVERED COMMIT, OFF THE TRUNK. Same diff, so the same
        # patch id, and NOT an ancestor of the trunk — which is what makes it
        # a production POSITIVE subject: an answer about it can only come from
        # the index, never from ancestry, so the positive below is the index's
        # answer rather than a rung above it.
        self.git("cherry-pick", second)
        copied = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-")

        real = landreq._measured_patch_id
        good = real(self.gitdir, second)[1]
        self.assertTrue(good, "MUST-HIT: the valid commit has no id, so the "
                              "non-empty index below is not what it claims")
        self.assertEqual(real(self.gitdir, copied)[1], good,
                         "MUST-HIT: the off-trunk copy does not carry the "
                         "covered commit's patch id, so the positive below "
                         "would prove nothing about the index")
        self.assertNotEqual(copied, second)

        # THE POISONED COMMIT IS THE PARENT, NOT THE TIP, and that is what
        # makes the mixed state reachable at all: the walk covers the trunk
        # first, so poisoning the trunk refuses before anything is indexed and
        # the index is empty rather than partial. The dangerous shape is a
        # NON-EMPTY index whose frontier could still empty.
        def unusable_for_first(gitdir, sha, *a, **k):
            if sha == first:
                return (landreq.HASHED, "abcdef12")
            return real(gitdir, sha, *a, **k)

        with mock.patch.object(landreq, "_measured_patch_id",
                               unusable_for_first):
            ids, incomplete = landreq._trunk_index_extend(self.gitdir, second)
            self.assertTrue(incomplete,
                            "a cover holding an unindexed commit called "
                            "itself complete")
            landreq._LAND_PROOF_MEMO.clear()
            answer = landreq._landing_proof(self.gitdir, off, second)
            after_unknown = self.proofs()
            # THE PRODUCTION POSITIVE, ASKED OF THE SAME INCOMPLETE COVER.
            # Incompleteness downgrades a MISS, and must not downgrade a HIT:
            # a subject the index does cover still gets the real answer while
            # a commit of that trunk remains unindexed. Nothing is unblinded
            # and nothing is rebuilt between the two questions — one cover,
            # two subjects, two different answers.
            positive = landreq._landing_proof(self.gitdir, copied, second)
            after_positive = self.proofs()
        # THE INDEX IS NON-EMPTY: the valid id really did land in it, so the
        # UNKNOWN below is about the unusable commit and not about an index
        # that holds nothing.
        stored = self.cover().get("ids") or {}
        self.assertIn(good, stored)
        self.assertNotIn("abcdef12", stored)
        self.assertEqual(answer, "unknown",
                         "a trunk one of whose commits was never indexed "
                         "answered ABSENT for a tip it cannot speak about")
        self.assertEqual(after_unknown, [],
                         "an unknown answer was written to the proof ledger")
        self.assertEqual(positive, "patch-equivalent",
                         "a subject the cover DOES hold lost its positive "
                         "because a different commit of the trunk was "
                         "unindexed")
        # A POSITIVE IS KEPT, and this is the second half of the same
        # asymmetry: the unknown above wrote nothing, this one is durable.
        self.assertEqual([(r.get("tip"), r.get("proof"))
                          for r in after_positive],
                         [(copied, "patch-equivalent")])

        # THE INDEPENDENT POSITIVE, same repository and same absent tip: once
        # every commit measures, the cover completes and the SAME question
        # becomes a real ABSENT. Without this the UNKNOWN above could be a
        # ladder that never answers anything.
        landreq._TRUNK_INDEX_MEMO.clear()
        landreq._LAND_PROOF_MEMO.clear()
        ids, incomplete = landreq._trunk_index_extend(self.gitdir, second)
        self.assertFalse(incomplete)
        self.assertEqual(landreq._landing_proof(self.gitdir, off, second),
                         "absent")


class TrunkArgumentIsAnAddressTest(LedgerBase):
    """A trunk may be named by a REF, and the cover is still keyed on a commit.

    WHAT A REFUSED COVER COSTS IS NOT ONE SENTENCE, and both flat readings
    are wrong: "it renders UNKNOWN" and "it only costs speed".
    `_derive_landing_proof` guards its index block with `if index:`, so a
    (None, True) cover falls past it and the consumer TRIES `git cherry`.
    A USABLE cherry answer settles the row exactly as the index would have —
    measured against one subject that reached trunk by cherry-pick, ancestry
    NOT_ANCESTOR so the patch rung is the one talking:

        index AVAILABLE (len 3, why False) -> "patch-equivalent"
        index REFUSED   ((None, True))     -> "patch-equivalent"

    An UNAVAILABLE or UNANSWERABLE fallback yields UNKNOWN instead, and an
    ambient `Expired` unwinds rather than answering at all. There is no
    universal spawn count either: the fallback shares the argv memo, and a
    spent budget never reaches a spawn.

    Which is why the arms below assert the INDEX itself rather than the
    verdict it feeds: the verdict is the same word on both sides wherever the
    fallback stays affordable, so it cannot see this failure at all.
    """

    def _two(self):
        first = self.commit("a.txt")
        second = self.commit("b.txt")
        return first, second

    def test_a_REF_NAME_builds_the_same_index_as_its_own_sha(self):
        _first, second = self._two()
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        by_sha, why_sha = landreq._landed_index(self.gitdir, second)
        landreq._TRUNK_INDEX_MEMO.clear()
        by_name, why_name = landreq._landed_index(self.gitdir, branch)
        landreq._TRUNK_INDEX_MEMO.clear()
        by_full, why_full = landreq._landed_index(self.gitdir,
                                                  "refs/heads/" + branch)
        self.assertTrue(by_sha, "the sha spelling built no index at all")
        self.assertEqual(by_name, by_sha,
                         "a ref name built a different index from its own sha")
        self.assertEqual(by_full, by_sha,
                         "the fully qualified ref built a different index")
        self.assertFalse(why_sha)
        self.assertFalse(why_name)
        self.assertFalse(why_full)

    def test_a_name_git_cannot_resolve_is_still_INCOMPLETE(self):  # noqa: VACUOUS_ASSERTION — the resolvable name on the same repo builds a real index unconditionally on the last lines
        """An unresolvable trunk is a trunk nothing can be proved against, so
        it stays (None, incomplete) — resolving a name must not become
        admitting one."""
        _first, second = self._two()
        index, why = landreq._landed_index(self.gitdir, "no-such-branch")
        self.assertIsNone(index)
        self.assertTrue(why)
        landreq._TRUNK_INDEX_MEMO.clear()
        index, why = landreq._landed_index(
            self.gitdir, self.git("rev-parse", "--abbrev-ref", "HEAD"))
        self.assertTrue(index)
        self.assertFalse(why)

    def test_the_cover_is_keyed_on_the_COMMIT_a_name_resolves_to(self):  # noqa: VACUOUS_ASSERTION — the assertEqual on the stored newest sha is an unconditional positive on the same record, so the assertNotIn is about the name and not about an empty cover
        """Resolving must not put the NAME into the durable record: a name is
        an address for a commit and the cover's one-trunk identity is a
        commit."""
        _first, second = self._two()
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        landreq._landed_index(self.gitdir, branch)
        cover = self.cover()
        self.assertEqual(cover.get("newest"), second)
        self.assertNotIn(branch, repr(cover))


class TrunkResolutionBoundaryTest(LedgerBase):
    """THE ONE PLACE A NAME BECOMES A COMMIT, and every way that can fail.

    The three arms above prove the direct named path warm and cold. They do
    not reach the BOUNDARY, and that boundary is where a resolver earns the
    right to exist: `_sha` accepts any forty hex characters, and a ref can
    name a TREE or a BLOB whose id is forty hex characters. MEASURED on git
    2.53.0 against a tag pointing straight at a tree:

        rev-parse --verify --quiet treeref            rc 0  <tree sha>
        rev-parse --verify --quiet treeref^{commit}   rc 1  expected commit
                                                            type, but the
                                                            object dereferences
                                                            to tree type

    So the peel is not decoration: without `^{commit}` this builder would take
    a tree id as a trunk, walk it as a commit, and every failure downstream
    would be about the walk rather than about the address. The refusals below
    are the shapes the resolver's own answer can take, and each of them must
    leave the DURABLE COVER exactly as it found it — a builder that cannot
    resolve its trunk has learned nothing, and learning nothing must not cost
    anything already known.
    """

    # THE CANONICAL ARGV, NOT A COPY OF IT. A literal here is a second
    # spelling of the thing under test: when landreq respelled its ref
    # reads, this spy silently stopped matching and the planted answer
    # never reached the code, which reads as the code being wrong.
    _RESOLVE = landreq._REF_ARGV

    def _cover_bytes(self):
        path = landreq.trunk_patch_index_path()
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def _plant(self, answer, spy=None):
        """Replace ONLY the trunk-resolving spawn; everything else is real.

        Keyed on the exact argv the builder issues — the three flags plus a
        peeled name — because `_measured_patch_id` and the ancestry probe
        also spawn `rev-parse`, and a plant that caught those would be
        measuring a different function. `spy`, when given, collects the verb
        of every OTHER spawn the builder makes, which is how the arms below
        assert that a refusal costs nothing.
        """
        real = landreq._git

        def fake(gitdir, *args, **kwargs):
            # EVERY INDEX DERIVED FROM THE CONSTANT, none written down. The
            # literal 3/4/args[3] here were arithmetic over the OLD argv
            # length, so pointing _RESOLVE at the canonical tuple silently
            # made this match nothing — a second spelling of the argv's SIZE
            # is the same defect as a second spelling of its TOKENS.
            if (args[:len(self._RESOLVE)] == self._RESOLVE
                    and len(args) == len(self._RESOLVE) + 1
                    and str(args[-1]).endswith("^{commit}")):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            if spy is not None:
                spy.append(args[0])
            return real(gitdir, *args, **kwargs)

        return mock.patch.object(landreq, "_git", fake)

    def test_a_ref_naming_a_TREE_or_a_BLOB_is_refused_and_a_TAG_is_not(self):  # noqa: VACUOUS_ASSERTION — the annotated-tag must-hit above the loop asserts a non-empty index unconditionally on the same observable, so the assertIsNone is about the peel and not about an inert repository
        """The peel is the whole discriminator, so both sides are asserted."""
        self.commit("a.txt")
        second = self.commit("b.txt")
        tree = self.git("rev-parse", "HEAD^{tree}")
        blob = self.git("rev-parse", "HEAD:b.txt")
        self.git("tag", "treeref", tree)
        self.git("tag", "blobref", blob)
        self.git("tag", "-a", "-m", "annotated", "tagref", second)

        # THE MUST-HIT FIRST, so a refusal below is a refusal and not an
        # inert repository: an ANNOTATED tag is two peels away from a commit
        # and still resolves, so the cover it builds is the sha's own.
        by_sha, why_sha = landreq._landed_index(self.gitdir, second)
        self.assertTrue(by_sha, "the sha spelling built no index at all")
        self.assertFalse(why_sha)
        landreq._TRUNK_INDEX_MEMO.clear()
        by_tag, why_tag = landreq._landed_index(self.gitdir, "tagref")
        self.assertEqual(by_tag, by_sha,
                         "an annotated tag on the trunk commit built a "
                         "different index from the commit it names")
        self.assertFalse(why_tag)

        kept = self._cover_bytes()
        self.assertIsNotNone(kept, "no cover was written to compare against")
        for name, kind in (("treeref", "tree"), ("blobref", "blob")):
            with self.subTest(ref=name):
                # `_sha` ACCEPTS what this ref resolves to without the peel:
                # asserting that here is what makes the refusal a measurement
                # of the peel rather than of the name's spelling.
                bare = self.git("rev-parse", "--verify", "--quiet", name)
                self.assertTrue(landreq._sha(bare),
                                "a %s ref did not resolve to a sha-shaped id, "
                                "so this arm no longer tests the peel" % kind)
                landreq._TRUNK_INDEX_MEMO.clear()
                index, why = landreq._landed_index(self.gitdir, name)
                self.assertIsNone(index,
                                  "a ref naming a %s was admitted as a trunk"
                                  % kind)
                self.assertTrue(why)
                self.assertEqual(self._cover_bytes(), kept,
                                 "a refused %s ref rewrote the durable cover"
                                 % kind)

    def test_the_PEEL_PREMISE_holds_in_git_itself(self):
        """GIT'S OWN BOUNDARY, and this arm is PREMISE evidence, not coverage.

        It calls its own peel rather than the builder's, so no mutation of
        this module can redden it — which is exactly what it is for. The cure
        rests on an assumption about git that nothing else here would notice
        changing. The arm that observes THE BUILDER doing the peel is the one
        below it, and the two are not substitutes.

        THE BARE SPELLING IS ASSERTED TOO, and it is the half that makes the
        peel load-bearing rather than decorative: without `^{commit}` the same
        ref resolves rc 0 to forty hex characters that `_sha` ACCEPTS, so a
        resolver spelled that way would take a TREE id as a trunk and walk it
        as a commit.
        """
        self.commit("a.txt")
        second = self.commit("b.txt")
        tree = self.git("rev-parse", "HEAD^{tree}")
        blob = self.git("rev-parse", "HEAD:b.txt")
        self.git("tag", "treeref", tree)
        self.git("tag", "blobref", blob)
        self.git("tag", "-a", "-m", "annotated", "tagref", second)

        def peel(name, suffix="^{commit}"):
            return landreq._git(self.gitdir, "rev-parse", "--verify",
                                "--quiet", name + suffix)

        # THE COMMIT SIDE, unconditional and first: an annotated tag peels
        # through to the commit it names. Without this the refusals below
        # could be a repository in which nothing resolves at all.
        got = peel("tagref")
        self.assertIsNotNone(got, "the resolving spawn did not run")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((got.stdout or "").strip(), second,
                         "an annotated tag peeled to something other than the "
                         "commit it names")

        for name, kind in (("treeref", "tree"), ("blobref", "blob")):
            with self.subTest(ref=name):
                bare = peel(name, "")
                self.assertEqual(bare.returncode, 0,
                                 "a %s ref did not resolve at all" % kind)
                self.assertTrue(landreq._sha((bare.stdout or "").strip()),
                                "a %s ref resolved to something `_sha` would "
                                "reject, so the peel is not what refuses it"
                                % kind)
                peeled = peel(name)
                self.assertNotEqual(peeled.returncode, 0,
                                    "`^{commit}` accepted a %s, so a %s id "
                                    "can enter the cover as a trunk" % (kind,
                                                                        kind))
                self.assertEqual((peeled.stdout or "").strip(), "",
                                 "the refused peel still printed an id")

    def test_the_BUILDER_peels_and_then_STOPS_traced_through_real_git(self):
        """The required control: what `_landed_index` ITSELF does at the
        boundary, traced through a FORWARDING spy over real git.

        THE TRACE IS THE OBSERVABLE, not the returned tuple. A builder that
        refused a non-commit ref for some unrelated reason returns the same
        (None, True), so the tuple alone cannot say the PEEL is what refused
        it. This wraps `landreq._git` so every call runs for real and is
        RECORDED, then asserts three things about one call to
        `_landed_index(<tree ref>)`: the commit-peel was issued, it FAILED,
        and NOTHING downstream ran — no `merge-base`, no `rev-list`, no
        object read, so the durable cover is never even opened.

        The commit-side positive uses the same trace on the same repository,
        so a refusal below cannot be a repository in which nothing works.
        """
        self.commit("a.txt")
        second = self.commit("b.txt")
        self.git("tag", "treeref", self.git("rev-parse", "HEAD^{tree}"))
        self.git("tag", "-a", "-m", "annotated", "tagref", second)

        def traced(trunk):
            calls = []
            real = landreq._git

            def spy(gitdir, *args, **kwargs):
                calls.append(tuple(str(a) for a in args))
                return real(gitdir, *args, **kwargs)

            landreq._TRUNK_INDEX_MEMO.clear()
            with mock.patch.object(landreq, "_git", spy):
                got = landreq._landed_index(self.gitdir, trunk)
            return got, calls

        (index, why), calls = traced("treeref")
        self.assertIsNone(index, "a tree ref was admitted as a trunk")
        self.assertTrue(why)
        # DERIVED FROM THE CONSTANT AT BOTH ENDS, never from its length on the
        # day this was written. The sibling filter above was cured for exactly
        # this and THIS ONE WAS NOT — a second surface answering the same
        # question kept the old shape, and `_RESOLVE` growing from three
        # elements to four made `c[:3] == self._RESOLVE` unsatisfiable, so the
        # peel the builder really did issue counted as zero.
        peels = [c for c in calls
                 if c[:len(self._RESOLVE)] == self._RESOLVE
                 and len(c) == len(self._RESOLVE) + 1
                 and str(c[-1]).endswith("^{commit}")]
        self.assertEqual(len(peels), 1,
                         "the builder did not issue exactly one commit-peel: "
                         "%r" % (calls,))
        self.assertTrue(str(peels[0][-1]).startswith("treeref"),
                        "the peel was issued against something else: %r"
                        % (peels[0],))
        # THE PEEL ITSELF FAILED — re-run the recorded argv so the assertion
        # is about git's answer to THIS call, not about a fixture's.
        answer = landreq._git(self.gitdir, *peels[0])
        self.assertNotEqual(answer.returncode, 0,
                            "the peel the builder issued SUCCEEDED, so what "
                            "refused the tree ref was something else")
        self.assertEqual(calls, peels,
                         "the builder kept working after the peel refused: "
                         "%r" % ([c for c in calls if c not in peels],))

        # POSITIVE, SAME TRACE, SAME REPOSITORY: an annotated tag peels and
        # the builder DOES go on to do downstream work.
        (tag_index, tag_why), tag_calls = traced("tagref")
        self.assertTrue(tag_index, "an annotated tag built no index at all")
        self.assertFalse(tag_why)
        self.assertGreater(len(tag_calls), 1,
                           "the builder did no work beyond the peel for a "
                           "resolvable trunk, so 'it stopped' means nothing")

    def test_every_resolver_REFUSAL_SHAPE_preserves_the_prior_cover(self):  # noqa: VACUOUS_ASSERTION — the real cover is built unconditionally before the plants and rebuilt unconditionally after them, both on the same observable the refusals return None for
        """Four answers the resolver can give, none of which is a resolution.

        The nonzero-with-valid-stdout shape is the one worth naming: git
        prints nothing useful on a refusal, but a caller that read `stdout`
        before `returncode` would find forty hex characters sitting there and
        take them. The returncode is checked FIRST for that reason.

        AND THE SPAWN COUNT IS THE ASSERTION THAT DISCRIMINATES, not the
        returned word. MEASURED: with the post-strip sha test removed, a
        blank and a malformed resolution BOTH still return (None, True) and
        both still leave the cover untouched — they simply reach that answer
        one `merge-base` later, through the divergence probe, on a trunk that
        is an empty string. Asserting only the word therefore proves nothing
        about that guard, which is the whole reason this lane exists: the
        cost of a bad address is measured in spawns.
        """
        self.commit("a.txt")
        second = self.commit("b.txt")
        built, why_built = landreq._landed_index(self.gitdir, second)
        self.assertTrue(built, "the arm needs a real cover to preserve")
        self.assertFalse(why_built)
        kept = self._cover_bytes()
        self.assertIsNotNone(kept)

        shapes = (
            ("the spawn never ran", None),
            ("nonzero carrying a valid sha",
             subprocess.CompletedProcess((), 1, stdout=second, stderr="")),
            ("zero with a blank stdout",
             subprocess.CompletedProcess((), 0, stdout="", stderr="")),
            ("zero with a malformed stdout",
             subprocess.CompletedProcess((), 0, stdout="not-a-sha\n",
                                         stderr="")),
        )
        for label, answer in shapes:
            with self.subTest(shape=label):
                landreq._TRUNK_INDEX_MEMO.clear()
                spy = []
                with self._plant(answer, spy):
                    index, why = landreq._landed_index(self.gitdir, "a-name")
                self.assertIsNone(index,
                                  "%s was treated as a resolution" % label)
                self.assertTrue(why, "%s did not report incompleteness"
                                     % label)
                self.assertEqual(self._cover_bytes(), kept,
                                 "%s rewrote the durable cover" % label)
                self.assertEqual(spy, [],
                                 "%s spent git after the resolver had "
                                 "already refused: %s" % (label, spy))

        # THE CONTROL, and it is about the STORE rather than the resolver:
        # after four refusals the same repository still builds its index, so
        # what the refusals refused was the address and not the cover.
        landreq._TRUNK_INDEX_MEMO.clear()
        again, why_again = landreq._landed_index(self.gitdir, second)
        self.assertEqual(again, built)
        self.assertFalse(why_again)

    def test_an_EXPIRED_from_the_RESOLVER_propagates_it_is_not_a_refusal(self):  # noqa: VACUOUS_ASSERTION — the no-plant control below asserts the same name builds a real index unconditionally, so the raise is about the plant and not about an unresolvable name
        """Expiry is not a failed resolution and must not wear its clothes.

        `_git` spends the projection budget before it spawns, so the resolver
        is a place `Expired` can be raised — and `_landed_index` re-raises it
        past the blanket clause precisely so a caller cannot read "no index"
        for a walk that was working when the clock ran out.
        """
        self.commit("a.txt")
        self.commit("b.txt")
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD")

        landreq._TRUNK_INDEX_MEMO.clear()
        with self._plant(projscope.Expired("budget spent before the peel")):
            with self.assertRaises(projscope.Expired):
                landreq._landed_index(self.gitdir, branch)

        # NO-PLANT CONTROL: the same call, same name, same repository. Without
        # it the raise above could be a name this repository never resolves.
        landreq._TRUNK_INDEX_MEMO.clear()
        index, why = landreq._landed_index(self.gitdir, branch)
        self.assertTrue(index, "the unplanted name built no index at all")
        self.assertFalse(why)


class FrontierProofAsksAncestryFirstTest(LedgerBase):
    """`best_proof` takes ANCESTOR from the free batch before the carry speaks.

    RELIEF carries and the WORD does not: a verdict taken as
    `patch-equivalent` against an older trunk still relieves at a descendant,
    but if the move brought the reviewed OBJECT itself onto trunk the true
    word there is `ancestor`. `carrier_discharge_proof` is read as the proof
    that discharged the row, so the weaker word must not be served while trunk
    contains the object. The three arms are the same board over one real
    repository, differing only in what the ancestry batch can say about the
    carrier's tip: ANCESTOR, NOT_ANCESTOR, and a sha this repository does not
    have.

    The carry is NOT disabled by the gate and the second arm is the control
    that says so — it is the spawn relief the ledger exists for, and only the
    published word changes.
    """

    def world(self):
        """One repository holding all three carrier states at one trunk.

        TRUNK MOVES BEFORE THE REPLAY, and that is not decoration: replaying a
        tip onto its OWN parent reproduces every input to the commit hash, so
        `cherry-pick` hands back the same object and there is no replay to
        measure. Then a merge puts the reviewed object itself on trunk, which
        is the state the whole class is about."""
        base = self.commit("a.txt")
        self.git("branch", "-M", "main")             # LOCAL_TRUNK, explicitly
        self.git("checkout", "-q", "-b", "lane", base)
        merged_tip = self.commit("work.txt", body="carried")
        self.git("checkout", "-q", "-b", "side", base)
        stranded = self.commit("side.txt", body="never lands")
        self.git("checkout", "-q", "main")
        self.commit("ahead.txt", body="trunk moved")
        self.git("cherry-pick", merged_tip)
        self.git("merge", "-q", "--no-ff", "-m", "merge lane", "lane")
        trunk = self.git("rev-parse", "HEAD")
        return merged_tip, stranded, trunk

    def board(self, tip):
        """a <- b, where b is the live carrier reviewed at `tip`."""
        def row(rid, **kw):
            lr = {"id": rid, "stalled": False, "terminal": False,
                  "dwell_s": 60, "state": "CHANGES_REQUESTED",
                  "supersedes": None, "polarity": None, "ungated": None,
                  "gate": "", "reviewed_tip": None, "owed_by": "author",
                  "repo_id": self.gitdir}
            lr.update(kw)
            return lr
        lrs = {"a": row("a", reviewed_tip="a" * 40),
               "b": row("b", reviewed_tip=tip, supersedes="a")}
        raw = {rid: {"id": rid, "v": 3, "supersedes": lr["supersedes"],
                     "chain_root": "a", "status": "open"}
               for rid, lr in lrs.items()}
        return lrs, raw

    def annotate(self, tip):
        """The proof word `best_proof` publishes for the carried row."""
        lrs, raw = self.board(tip)
        landreq._annotate_frontier_debt(
            lrs, raw, proof=landreq._landing_proofs(gitdir=self.repo,
                                                    cache={}))
        return lrs["a"]["carrier_discharge_proof"]

    def test_a_carried_PATCH_EQUIVALENT_never_shadows_a_true_ancestor(self):
        """The gap task/2796 measured, closed and asserted end to end.

        LOAD-BEARING MUTATION: delete the `_ancestry_word(...) or` leg in
        `best_proof`.
          -> AssertionError: the carried word shadowed a true ancestor
        """
        tip, _stranded, trunk = self.world()
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, tip, trunk,
                                        landreq.PROOF_PATCH_EQUIVALENT)
            landreq._CARRIER_PROOF_MEMO.clear()
            # FIXTURE CONTROLS, both directions, before the claim: the ledger
            # really holds the WEAKER word, and the batch really answers
            # ANCESTOR for the same pair. Without both, the assertion below
            # could pass over an empty ledger or a batch that cannot see.
            self.assertEqual(
                landreq._kept_carrier_proof(self.gitdir, tip, trunk),
                landreq.PROOF_PATCH_EQUIVALENT,
                "the carry does not hold the weaker word this arm is about")
            self.assertEqual(
                landreq._batched_ancestry(self.gitdir, tip, trunk),
                landreq.ANCESTOR,
                "the batch cannot answer, so the gate has nothing to take")
            self.assertEqual(self.annotate(tip), landreq.PROOF_ANCESTOR,
                             "the carried word shadowed a true ancestor")

    def test_a_NOT_ANCESTOR_carrier_is_still_served_by_the_carry(self):
        """The gate narrows the WORD, it does not disable the ledger.

        LOAD-BEARING MUTATION: make `_ancestry_word` return PROOF_ANCESTOR
        whenever the batch does not say NOT_ANCESTOR.
          -> AssertionError: a non-ancestor carrier was called an ancestor
        """
        _tip, stranded, trunk = self.world()
        with projscope.scope():
            landreq._keep_carrier_proof(self.gitdir, stranded, trunk,
                                        landreq.PROOF_PATCH_EQUIVALENT)
            landreq._CARRIER_PROOF_MEMO.clear()
            self.assertEqual(
                landreq._batched_ancestry(self.gitdir, stranded, trunk),
                landreq.NOT_ANCESTOR,
                "this tip is not the present-but-unreachable object the arm "
                "needs, so the carry below is not the thing being tested")
            self.assertEqual(self.annotate(stranded),
                             landreq.PROOF_PATCH_EQUIVALENT,
                             "a non-ancestor carrier was called an ancestor")

    def test_an_UNANSWERABLE_batch_consults_the_oracle_and_invents_nothing(self):
        """UNDETERMINED is the vanished-object case and never an ancestor.

        LOAD-BEARING MUTATION: make `_ancestry_word` return PROOF_ANCESTOR on
        anything but NOT_ANCESTOR.
          -> AssertionError: an unanswerable batch manufactured an ancestor
        """
        _tip, _stranded, trunk = self.world()
        absent = "c" * 40
        with projscope.scope():
            self.assertEqual(
                landreq._batched_ancestry(self.gitdir, absent, trunk),
                landreq.UNDETERMINED,
                "this repository has the object, so the arm measures nothing")
            # MUST-HIT CONTROL on the same call shape: the tip that IS on
            # trunk reaches PROOF_ANCESTOR here, so the word below is the
            # refusal and not an annotator that stopped answering.
            self.assertEqual(self.annotate(_tip), landreq.PROOF_ANCESTOR)
            self.assertEqual(self.annotate(absent), landreq.PROOF_UNKNOWN,
                             "an unanswerable batch manufactured an ancestor")


if __name__ == "__main__":
    unittest.main()
