"""One-instant projection memo — `helm.projscope`.

The behaviour under test is a CONTRACT, not an optimisation: a projection that
asks the same question twice must be told the same thing, and nothing that
writes may ever be answered from an older question. Both halves are asserted
here by counting real calls, never by asserting that nothing complained.
"""
import contextlib
import threading
import unittest
from unittest import mock

from helm import dispatches, landreq, projscope, vcs


class ScopeTest(unittest.TestCase):
    """The memo's own law: an OPERATION, never a clock."""

    def setUp(self):
        self.calls = 0

    def bump(self):
        self.calls += 1
        return "v%d" % self.calls

    def test_outside_a_scope_nothing_is_cached(self):  # noqa: VACUOUS_ASSERTION — the positive control is the RETURNED PAIR ('v1','v2'): two distinct answers can only exist if both computations really ran
        """The whole safety argument rests on this: every write path calls the
        same functions and opens no scope, so every write path still measures.
        """
        first = projscope.memo("k", self.bump)
        second = projscope.memo("k", self.bump)
        # POSITIVE CONTROL on the same observable: both calls really ran and
        # really produced DIFFERENT answers, so "not cached" is asserted by
        # what came back, not only by a counter.
        self.assertEqual((first, second), ("v1", "v2"))
        self.assertEqual(self.calls, 2)
        self.assertFalse(projscope.active())

    def test_inside_a_scope_one_answer_per_key(self):
        with projscope.scope():
            self.assertTrue(projscope.active())
            first = projscope.memo("k", self.bump)
            self.assertEqual(projscope.memo("k", self.bump), first)
            self.assertEqual(projscope.memo("k", self.bump), first)
        self.assertEqual(self.calls, 1)

    def test_a_none_answer_is_cached_like_any_other(self):  # noqa: VACUOUS_ASSERTION — None IS the observable under test; the control is that the computation ran EXACTLY once while both reads returned it
        """`.get(key) is not None` is the shape that quietly re-asks for every
        honest UNKNOWN — and UNKNOWN is the answer this codebase returns most
        under load. The sentinel is what makes the memo actually memoise."""
        def none_after_bump():
            self.bump()
            return None

        with projscope.scope():
            first = projscope.memo("k", none_after_bump)
            second = projscope.memo("k", none_after_bump)
        # The observable IS None, so the positive control is that a None came
        # back BOTH times AND that only one computation happened.
        self.assertEqual([first, second], [None, None])
        self.assertEqual(self.calls, 1)

    def test_leaving_the_scope_drops_the_cache(self):  # noqa: VACUOUS_ASSERTION — the control is the RETURNED PAIR ('v1','v2'); a cache that survived the exit would return ('v1','v1')
        with projscope.scope():
            first = projscope.memo("k", self.bump)
        with projscope.scope():
            second = projscope.memo("k", self.bump)
        self.assertEqual((first, second), ("v1", "v2"))
        self.assertEqual(self.calls, 2)

    def test_a_nested_scope_shares_the_outermost_cache_and_never_clears_it(self):  # noqa: VACUOUS_ASSERTION — the control is the THREE RETURNED VALUES all being 'v1'; an inner scope that cleared would return 'v2' after it
        """Re-entrancy is not a nicety: a caller must not have to know whether
        its callee also scopes, or the memo evaporates under composition."""
        with projscope.scope():
            outer = projscope.memo("k", self.bump)
            with projscope.scope():
                inner = projscope.memo("k", self.bump)
            after = projscope.memo("k", self.bump)  # inner exit must NOT clear
        self.assertEqual([outer, inner, after], ["v1", "v1", "v1"])
        self.assertEqual(self.calls, 1)

    def test_a_raise_is_never_memoised(self):
        """A transient failure frozen into the pass would turn one bad spawn
        into a board-wide UNKNOWN."""
        raised = 0
        with projscope.scope():
            for _ in range(2):
                try:
                    projscope.memo("k", self._boom)
                except ValueError:
                    raised += 1
            recovered = projscope.memo("k", self.bump)
        # Both attempts really raised (so neither was answered from a cache),
        # and the key was still free for the call that finally succeeded.
        self.assertEqual(raised, 2)
        self.assertEqual(recovered, "v1")
        self.assertEqual(self.calls, 1)

    @staticmethod
    def _boom():
        raise ValueError("transient")

    def test_an_unhashable_key_computes_rather_than_colliding(self):
        """`repo_id` is copied verbatim out of the ledger and can be a list —
        landreq's own comment records `["not", "a", "path"]` arriving here.
        Degrading to a spawn is slow; degrading to a shared key is wrong."""
        with projscope.scope():
            first = projscope.memo(["unhashable"], self.bump)
            second = projscope.memo(["unhashable"], self.bump)
            # POSITIVE CONTROL in the same scope: a hashable key IS memoised,
            # so the two answers above differ because the KEY was unhashable
            # and not because the scope silently failed to open.
            control = [projscope.memo("hashable", self.bump) for _ in range(2)]
        self.assertEqual((first, second), ("v1", "v2"))
        self.assertEqual(control, ["v3", "v3"])
        self.assertEqual(self.calls, 3)

    def test_the_cache_is_per_thread(self):
        """helm web is a ThreadingHTTPServer: a module-level dict lets one
        request's snapshot answer another's read, and one thread's exit clear
        the cache out from under a thread still inside its own scope."""
        seen = {}

        def worker(name):
            with projscope.scope():
                seen[name] = projscope.memo("shared", lambda: name)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen, {"a": "a", "b": "b"})


class GitSeamTest(unittest.TestCase):
    """`landreq._git` asks once per projection and always outside one."""

    def test_the_same_argv_spawns_once_inside_a_scope(self):
        with mock.patch.object(landreq, "_git_spawn",
                               return_value="P") as spawn:
            with projscope.scope():
                for _ in range(3):
                    self.assertEqual(
                        landreq._git("/r/.git", "merge-base",
                                     "--is-ancestor", "a", "b"), "P")
        self.assertEqual(spawn.call_count, 1)

    def test_a_different_argv_is_a_different_question(self):
        with mock.patch.object(landreq, "_git_spawn",
                               return_value="P") as spawn:
            with projscope.scope():
                landreq._git("/r/.git", "cat-file", "-e", "a")
                landreq._git("/r/.git", "cat-file", "-e", "b")
                landreq._git("/other/.git", "cat-file", "-e", "a")
        self.assertEqual(spawn.call_count, 3)

    def test_outside_a_scope_every_call_spawns(self):
        with mock.patch.object(landreq, "_git_spawn",
                               return_value="P") as spawn:
            for _ in range(3):
                landreq._git("/r/.git", "cat-file", "-e", "a")
        self.assertEqual(spawn.call_count, 3)

    def test_a_non_path_repo_binding_still_never_reaches_git(self):
        """`repo_id` is copied verbatim out of the ledger and never
        re-validated — landreq records `["not", "a", "path"]` arriving here."""
        with mock.patch.object(landreq, "_git_spawn",
                               return_value="P") as spawn:
            with projscope.scope():
                junk = landreq._git(["not", "a", "path"], "log")
                # POSITIVE CONTROL on the same observable, same scope: a REAL
                # gitdir does reach the spawn, so the None above is the
                # rejection and not a memo swallowing every call.
                real = landreq._git("/r/.git", "log")
        self.assertIsNone(junk)
        self.assertEqual(real, "P")
        self.assertEqual(spawn.call_count, 1)


class VcsSeamTest(unittest.TestCase):
    """The read allowlist is the safety boundary, and it is an ALLOWLIST."""

    def setUp(self):
        self.be = vcs.GitVcs()

    def test_a_read_verb_is_asked_once_per_scope(self):
        with mock.patch.object(vcs.GitVcs, "_spawn",
                               return_value=(0, b"", b"")) as spawn:
            with projscope.scope():
                for _ in range(4):
                    self.be.run("/root", "cherry", "refs/heads/main", "abc")
        self.assertEqual(spawn.call_count, 1)

    def test_a_verb_outside_the_allowlist_always_spawns(self):
        """`worktree` and `config` share a verb between their read and their
        mutating subcommands, so neither may be memoised on the verb alone."""
        counted = {}
        with mock.patch.object(vcs.GitVcs, "_spawn",
                               return_value=(0, b"", b"")) as spawn:
            for verb in ("worktree", "config", "update-ref", "branch",
                         "checkout", "cherry"):
                spawn.reset_mock()
                with projscope.scope():
                    self.be.run("/root", verb, "list")
                    self.be.run("/root", verb, "list")
                counted[verb] = spawn.call_count
        # ONE unconditional assertion over the whole table, INCLUDING the
        # allowlisted `cherry` as the positive control — without it a scope
        # that never opened would produce the same five 2s and read as a pass.
        self.assertEqual(counted, {"worktree": 2, "config": 2, "update-ref": 2,
                                   "branch": 2, "checkout": 2, "cherry": 1})

    def test_an_env_overlay_is_a_write_signal_and_is_never_memoised(self):
        """`env` carries the ref-guard bit that distinguishes the sanctioned
        branch creator from a forbidden one. A call that has to say who it is
        is not a read."""
        with mock.patch.object(vcs.GitVcs, "_spawn",
                               return_value=(0, b"", b"")) as spawn:
            with projscope.scope():
                self.be.run("/root", "log", "-1", env={"HELM_REF_GUARD": "1"})
                self.be.run("/root", "log", "-1", env={"HELM_REF_GUARD": "1"})
        self.assertEqual(spawn.call_count, 2)

    def test_distinct_stdin_is_a_distinct_question(self):
        """`patch-id` reads its whole answer off stdin — an argv-only key
        would hand one range's ids to another's."""
        with mock.patch.object(vcs.GitVcs, "_spawn",
                               return_value=(0, b"", b"")) as spawn:
            with projscope.scope():
                self.be.run("/root", "patch-id", "--verbatim", stdin=b"one")
                self.be.run("/root", "patch-id", "--verbatim", stdin=b"two")
                self.be.run("/root", "patch-id", "--verbatim", stdin=b"one")
        self.assertEqual(spawn.call_count, 2)


class ApprovalTierTest(unittest.TestCase):
    """Resolving a tier walks every fd in /proc and fires a network canary.
    It is per SEAT; it was being paid per ROW."""

    def test_one_resolution_per_recipient_and_repo(self):
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", "")) as live:
            with projscope.scope():
                for _ in range(20):
                    dispatches.approval_tier("kimi", repo="/r/.git")
                for _ in range(20):
                    dispatches.approval_tier("codex", repo="/r/.git")
                dispatches.approval_tier("kimi", repo="/other/.git")
        self.assertEqual(live.call_count, 3)

    def test_every_write_path_still_measures_live(self):  # noqa: VACUOUS_ASSERTION — the control is that THREE DIFFERENT verdicts came back under ONE key; a memo would have returned the first tuple three times
        """The land door, the close ladders and the send advisory open no
        scope. If this ever starts passing for the wrong reason, an out-of-tier
        APPROVE gets authorised off a stale reading."""
        self.assertFalse(projscope.active())
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=[("ok", "a"), ("outside", "b"),
                                            ("unknown", "c")]) as live:
            answers = [dispatches.approval_tier("kimi", repo="/r/.git")
                       for _ in range(3)]
        # THE VERDICT CHANGED THREE TIMES UNDER ONE KEY and the caller saw all
        # three. A counter alone would still pass if the door returned a
        # cached tuple while the body happened to be called elsewhere.
        self.assertEqual(answers, [("ok", "a"), ("outside", "b"),
                                   ("unknown", "c")])
        self.assertEqual(live.call_count, 3)


class ProjectionScopeTest(unittest.TestCase):
    """`project_raw` must hold the scope across the ANNOTATIONS too — they
    shell out to git exactly as the row loop does."""

    def test_the_annotations_run_inside_the_scope(self):
        seen = {}

        def record(name):
            def f(*_a, **_kw):
                seen[name] = projscope.active()
            return f

        with mock.patch.object(dispatches, "snapshot_and_events",
                               return_value=({}, {}, {}, {}, None)), \
                mock.patch.object(dispatches, "gate_epoch", return_value=None), \
                mock.patch.object(landreq, "_receipts_by_tip",
                                  return_value={}), \
                mock.patch.object(landreq, "_consumed_confirmations",
                                  return_value={}), \
                mock.patch.object(dispatches, "attest_projections",
                                  return_value={}), \
                mock.patch.object(landreq.store_load, "read_scope",
                                  side_effect=contextlib.nullcontext), \
                mock.patch.object(landreq, "_trunk_reach", return_value=set()), \
                mock.patch.object(landreq, "_annotate_succession",
                                  side_effect=record("succession")), \
                mock.patch.object(landreq, "_annotate_contrary_discharge",
                                  side_effect=record("contrary")), \
                mock.patch.object(landreq, "_annotate_frontier_debt",
                                  side_effect=record("frontier")):
            before = projscope.active()
            landreq.project_raw(0.0)
            after = projscope.active()
        # The three annotations shell out to git exactly as the row loop does,
        # so a scope that stopped at the loop would leave the whole frontier
        # pass unmemoised — and `before`/`after` are the positive control that
        # `active()` can read False at all, so three Trues mean something.
        self.assertEqual(seen, {"succession": True, "contrary": True,
                                "frontier": True})
        self.assertEqual((before, after), (False, False))


if __name__ == "__main__":
    unittest.main()
