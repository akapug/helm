"""One-instant projection memo — `helm.projscope`.

The behaviour under test is a CONTRACT, not an optimisation: a projection that
asks the same question twice must be told the same thing, and nothing that
writes may ever be answered from an older question. Both halves are asserted
here by counting real calls, never by asserting that nothing complained.
"""
import contextlib
import threading
import time
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

    def test_an_env_overlay_is_part_of_the_question_not_a_bypass(self):
        """REPLACES `..._is_a_write_signal_and_is_never_memoised`, whose
        premise was refuted by measurement and whose fixture invented its
        input.

        THAT ARM SAID "a call that has to say who it is is not a read" and
        demonstrated it on `git log -1` under a `HELM_REF_GUARD` overlay — a
        pairing that exists nowhere in this tree. The real ref-guard bit is
        `HELM_WORK_CLAIM` and it rides `worktree add` (helm/harness.py), a
        WRITE verb, so what keeps it out of the memo is the verb allowlist,
        which `test_a_verb_outside_the_allowlist_always_spawns` proves
        directly and which this change does not touch.

        WHAT ACTUALLY ARRIVES WITH AN `env` IS A CONSTANT READ VIEW.
        `rowworld._git` hands EVERY read in that module `_scrubbed_env()` —
        repository selection removed, replacement objects and grafts pinned
        off — and `obligation` and `gateimport` do the same with their own
        constant scrubs. MEASURED over one cold `helm lr list`:
        2587 of 4089 git spawns came through this door and NONE of them could
        be memoised, 265 of them one `rev-parse --is-shallow-repository`
        asked once per row. So the overlay belongs in the KEY, which is the
        law `landreq._git_key` already states.

        THREE ASSERTIONS, because the key must be neither too narrow nor too
        wide, and the third is the control that keeps the safety claim real.
        """
        with mock.patch.object(vcs.GitVcs, "_spawn",
                               return_value=(0, b"", b"")) as spawn:
            # ONE overlay, asked twice -> ONE spawn. The read this cure is for.
            with projscope.scope():
                self.be.run("/root", "log", "-1", env={"GIT_DIR": None})
                self.be.run("/root", "log", "-1", env={"GIT_DIR": None})
            counted = {}
            counted["one overlay twice"] = spawn.call_count
            # TWO overlays, one argv -> TWO spawns. A key that dropped the env
            # would answer the second question with the first repository's
            # reading, which is the failure `_git_key` names.
            spawn.reset_mock()
            with projscope.scope():
                self.be.run("/root", "log", "-1", env={"GIT_DIR": None})
                self.be.run("/root", "log", "-1", env={"GIT_DIR": "/elsewhere"})
            counted["two overlays"] = spawn.call_count
            # AND "REMOVE THIS VARIABLE" IS NOT "LEAVE IT ALONE": a None value
            # is the seam's spelling for unset, and no-overlay is a third
            # question again.
            spawn.reset_mock()
            with projscope.scope():
                self.be.run("/root", "log", "-1", env={"GIT_DIR": None})
                self.be.run("/root", "log", "-1")
            counted["unset versus absent"] = spawn.call_count
            # THE WRITE VERB IS UNMOVED, and it is the control: without it a
            # memo that had quietly started answering writes would pass every
            # assertion above.
            spawn.reset_mock()
            with projscope.scope():
                self.be.run("/root", "worktree", "add", "/p",
                            env={"HELM_WORK_CLAIM": "1"})
                self.be.run("/root", "worktree", "add", "/p",
                            env={"HELM_WORK_CLAIM": "1"})
            counted["write verb"] = spawn.call_count
        # ONE unconditional assertion over the whole table, the shape the
        # allowlist arm beside this one uses: a scope that never opened would
        # make every row read 2 and the first row is what refuses that.
        self.assertEqual(counted, {"one overlay twice": 1, "two overlays": 2,
                                   "unset versus absent": 2, "write verb": 2})

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
    """Live advisory resolution is memoised per seat and repository.

    Verdict reads use retained authority instead; the interleaved controls below
    ensure an advisory cache entry cannot authorize historical evidence gaps.
    """

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

    def test_historical_verdicts_never_resolve_live_even_inside_a_scope(self):
        """Historical absence is not permission to consult today's authority.

        Repeated reads stay pre-tier. The advisory positive control proves the
        live resolver remains reachable, but cannot repair historical rows.
        """
        row = {"recipient": "kimi", "repo_id": "/r/.git"}
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", "")) as live:
            with projscope.scope():
                for _ in range(20):
                    tier, why = dispatches.approval_tier_for_verdict(dict(row))
                    self.assertEqual(tier, "unknown")
                    self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
                    self.assertIn("PRE-TIER", why)
                live.assert_not_called()
                self.assertEqual(dispatches.approval_tier("kimi", repo="/r/.git"),
                                 ("ok", ""))
                live.assert_called_once()

    def test_runtime_evidence_alone_cannot_restore_a_historical_tier(self):
        """Both absent-author and valid-runtime-only history remain pre-tier.

        Runtime validity is asserted first, so refusal cannot be credited to a
        broken envelope. Neither input may consult live policy or family state.
        """
        auth = {"v": 5, "identity": "reviewer", "roster_identity": "reviewer",
                "session": "session-a",
                "runtime": {"family": "claude", "backend": "native"},
                "runtime_verified": True}
        ev = dispatches._verdict_author_runtime_evidence(
            "reviewer", "session-a", "claude", auth)
        anchor_ = dispatches._verdict_author_runtime_anchor(ev)
        # PRECONDITION, ASSERTED: if the envelope ever stops validating, this
        # arm would silently take the incomplete-proof path and count ZERO
        # instead of failing — which is exactly how a naive fixture passes
        # for the wrong reason.
        self.assertIsNone(dispatches._verdict_author_runtime_error(
            ev, "reviewer", "session-a", anchor_))
        hist = {"recipient": "reviewer", "repo_id": "/r/.git"}
        full = dict(hist, verdict_author_session="session-a",
                    verdict_author_runtime_evidence=ev,
                    verdict_author_runtime_anchor=anchor_)

        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", "")) as live:
            with projscope.scope():
                for row in (hist, full):
                    with self.subTest(author_evidence=row is full):
                        tier, why = dispatches.approval_tier_for_verdict(dict(row))
                        self.assertEqual(tier, "unknown")
                        self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
                        self.assertIn("PRE-TIER", why)
                live.assert_not_called()
                self.assertEqual(dispatches.approval_tier("reviewer", repo="/r/.git"),
                                 ("ok", ""))
                live.assert_called_once()

    def test_live_advisory_memo_cannot_answer_a_historical_verdict(self):
        """Populate the live memo first, then interleave verdict and advisory.

        Shared recipient/repo cannot let a cached live OK authorize history.
        Reverse order is covered by the historical-first controls above.
        """
        hist = {"recipient": "kimi", "repo_id": "/r/.git"}
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               return_value=("ok", "live advisory")) as live:
            with projscope.scope():
                for _ in range(2):
                    self.assertEqual(dispatches.approval_tier("kimi", repo="/r/.git"),
                                     ("ok", "live advisory"))
                    tier, why = dispatches.approval_tier_for_verdict(dict(hist))
                    self.assertEqual(tier, "unknown")
                    self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
                    self.assertIn("PRE-TIER", why)
        live.assert_called_once()

    def test_a_different_families_selector_is_a_different_question(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertEqual(b, k("kimi", "/r/.git", {"claude"}, "kimi")) checks repeatability on the same key observable as the inequality arms. Those inequalities reject a constant key; the explicit hash checks reject unhashable keys. This is key-builder coverage, not a mutation probe through the memo door.
        """The key builder separates selectors and produces hashable keys.

        This arm compares keys directly; it neither counts computations nor
        mutates the key builder. The production advisory caller uses the live
        family sentinel, while verdict readers no longer enter this memo.
        Consequently this test does not claim a reachable pair of memo callers
        differing only in families or canonical_recipient.

        A bare family set must be normalised: projscope.memo computes rather
        than caches when its key is unhashable. The explicit hash and frozenset
        assertions below protect that builder property."""
        k = dispatches._approval_tier_key
        a = k("kimi", "/r/.git", None, "kimi")
        b = k("kimi", "/r/.git", {"claude"}, "kimi")
        c = k("kimi", "/other/.git", {"claude"}, "kimi")
        self.assertNotEqual(a, b)          # families None vs a selector
        self.assertNotEqual(b, c)          # repo still separates
        # HASHABILITY IS ASSERTED, NOT EXERCISED. This was three bare hash()
        # calls relying on TypeError to fail the test — load-bearing, but it
        # reads as dead code and the first reader to tidy it deletes the
        # guarantee without a single test going red. That is the same
        # silently-lost-protection shape this lane is about.
        for key in (a, b, c):
            with self.subTest(key=key):
                try:
                    hash(key)
                except TypeError:
                    self.fail("key is UNHASHABLE, so projscope.memo degrades "
                              "to computing and the memo caches nothing: %r"
                              % (key,))
        self.assertIsInstance(b[3], frozenset)
        # and the positive half: the same question really is the same key
        self.assertEqual(b, k("kimi", "/r/.git", {"claude"}, "kimi"))

    def test_every_write_path_still_measures_live(self):  # noqa: VACUOUS_ASSERTION — three distinct advisory answers come back under one key; an incorrectly active memo would return the first tuple three times
        """Direct live advisory calls outside a scope each recompute.

        This arm invokes approval_tier, not a land or close writer. It does not
        claim those consumers re-evaluate recorded verdict authority live.
        """
        self.assertFalse(projscope.active())
        with mock.patch.object(dispatches, "_approval_tier_uncached",
                               side_effect=[("ok", "a"), ("outside", "b"),
                                            ("unknown", "c")]) as live:
            answers = [dispatches.approval_tier("kimi", repo="/r/.git")
                       for _ in range(3)]
        # THE ADVISORY CHANGED THREE TIMES UNDER ONE KEY and the caller saw all
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


class AmbientBudgetTest(unittest.TestCase):
    """THE BUDGET PRIMITIVE ITSELF, with no consumer in the picture.

    Every arm here reaches `projscope` and nothing else. A budget wired into
    a caller is tested where that caller lives; what these arms own is the
    contract every such caller will rely on — that None and "no time left" are
    different answers, that a nested scope can tighten and never loosen, and
    that running out of time RAISES rather than returning something a caller
    can read as a decision.
    """

    def test_no_budget_means_no_deadline_no_cap_and_no_refusal(self):
        """AN UNBUDGETED SCOPE MUST BEHAVE EXACTLY AS IT DID BEFORE BUDGETS.

        This is the arm that keeps the feature from being a tax on every
        existing caller: a scope opened without a deadline answers None
        everywhere and refuses nothing.
        """
        self.assertIsNone(projscope.deadline(), "a budget leaked in from "
                                                "outside any scope")
        with projscope.scope():
            self.assertIsNone(projscope.deadline())
            self.assertIsNone(projscope.remaining())
            self.assertIsNone(projscope.spend_or_raise("anything"))

    def test_a_live_budget_reports_the_time_it_has_left(self):
        later = time.monotonic() + 30.0
        with projscope.scope(deadline=later):
            self.assertEqual(projscope.deadline(), later)
            left = projscope.remaining()
            self.assertIsNotNone(left, "a set budget reported None, which "
                                       "every caller reads as UNLIMITED")
            self.assertGreater(left, 25.0)
            self.assertLessEqual(left, 30.0)
            # THE SPEND DOOR AND `remaining()` MUST AGREE. They read the
            # same clock a moment apart, so they are compared with a
            # tolerance rather than for equality; what must not happen is one
            # of them answering None while the other answers a number.
            spend = projscope.spend_or_raise("a read")
            self.assertIsNotNone(
                spend, "the spend door answered None on a LIVE budget, which "
                       "every caller reads as unlimited")
            self.assertAlmostEqual(
                spend, left, delta=1.0,
                msg="the spend door and `remaining()` disagree about the "
                    "same budget")

    def test_an_expired_budget_raises_instead_of_answering(self):
        """A RAISE CANNOT BE COLLAPSED, AND THAT IS THE ENTIRE DESIGN.

        Every value this could have returned — None, False, 0, an empty
        result — is readable by some caller as a DECIDED answer: `p is not
        None and p.returncode == 0` turns an out-of-time spawn into a failed
        one, and a failed one into "no". A raise propagates until something
        that HAS an UNKNOWN in its vocabulary catches it.
        """
        with projscope.scope(deadline=time.monotonic() - 1.0):
            left = projscope.remaining()
            self.assertIsNotNone(
                left, "an EXPIRED budget reported None, which is the exact "
                      "collapse this primitive exists to prevent: None means "
                      "spend freely")
            self.assertLessEqual(left, 0.0)
            with self.assertRaises(projscope.Expired) as caught:
                projscope.spend_or_raise("the trunk pin")
            self.assertIn(
                "the trunk pin", str(caught.exception),
                "the exception does not name the stage that did not run, so "
                "a caller catching it can only say that SOMETHING did not")

    def test_a_nested_scope_tightens_and_can_never_buy_time(self):
        """AN INNER SCOPE MUST NOT BE ABLE TO EXTEND ITS CALLER'S BUDGET.

        Both directions are asserted on the same outer budget, because an
        implementation that simply always takes the inner value passes the
        tighten half alone.
        """
        outer = time.monotonic() + 20.0
        with projscope.scope(deadline=outer):
            with projscope.scope(deadline=outer - 15.0):
                self.assertAlmostEqual(projscope.deadline(), outer - 15.0,
                                       msg="a SOONER inner budget did not win")
            self.assertEqual(projscope.deadline(), outer,
                             "leaving the inner scope did not restore the "
                             "outer budget")
            with projscope.scope(deadline=outer + 600.0):
                self.assertEqual(
                    projscope.deadline(), outer,
                    "a LATER inner budget bought time its caller did not "
                    "have, which makes the outer budget unenforceable")
            with projscope.scope():
                self.assertEqual(
                    projscope.deadline(), outer,
                    "an inner scope with NO budget of its own escaped its "
                    "caller's")

    def test_the_budget_does_not_outlive_its_outermost_scope(self):
        """A LEAKED DEADLINE REFUSES WORK NOBODY BUDGETED.

        The cache is cleared when the outermost scope closes; the budget must
        go with it, or the next unbudgeted projection in this thread inherits
        an expired instant and starts raising.
        """
        with projscope.scope(deadline=time.monotonic() - 1.0):
            self.assertIsNotNone(projscope.deadline())
        self.assertIsNone(projscope.deadline(),
                          "the budget outlived its scope")
        with projscope.scope():
            self.assertIsNone(
                projscope.remaining(),
                "a later UNBUDGETED scope inherited the previous one's "
                "deadline")
            self.assertIsNone(projscope.spend_or_raise("unbudgeted work"))

        # AND THE RESET IS NOT THE POP. The clear at depth zero RUNS on every
        # outermost exit; what it does there is NOTHING, because balanced
        # enter/exit pairs have already emptied the stack. It has an effect
        # only when depth and the stack have DRIFTED, and NO NATURAL PRODUCER
        # OF THAT DRIFT IS CLAIMED HERE:
        # a skipped inner `__exit__` leaves depth at 1 after the remaining
        # exit, so the reset is not reached on that path either, and a thread
        # whose state predates budgets gets an EMPTY list rather than a
        # surplus entry. What this arm pins is DEFENSIVE CLEANUP UNDER
        # DELIBERATELY INDUCED PRIVATE-STATE INCONSISTENCY: the stack is
        # corrupted here by hand, and the outermost exit must repair it
        # rather than leave a live deadline behind for the next projection in
        # this thread.
        stale = time.monotonic() - 1.0
        with projscope.scope(deadline=stale):
            # ONE UNIT OF DRIFT, induced directly on the private state: the
            # scope pushed its own entry and this is a second one the exit
            # will not account for. The drift is induced on a BUDGETED scope
            # because the residue must be a real deadline -- with an
            # unbudgeted one it is None, and None is what a correct repair
            # also leaves, so the arm could not tell them apart.
            projscope._state().deadlines.append(stale)
            self.assertIsNotNone(
                projscope.deadline(),
                "MUST-HIT: the induced residue is not visible, so this case "
                "would pass against a scope that repairs nothing")
        self.assertIsNone(
            projscope.deadline(),
            "a deadline stack left longer than its scope depth survived the "
            "outermost exit, so the next unbudgeted projection in this "
            "thread inherits an expired budget")

    def test_an_exception_still_restores_the_outer_budget(self):
        """THE FAILURE PATH IS WHERE A STACK DISCIPLINE ACTUALLY GETS TESTED.

        `__exit__` runs on the way out of a raise too, and an implementation
        that pops only on the happy path leaves the inner budget in force for
        the rest of the outer scope — tighter than asked, silently.
        """
        outer = time.monotonic() + 20.0
        with projscope.scope(deadline=outer):
            with contextlib.suppress(RuntimeError):
                with projscope.scope(deadline=outer - 15.0):
                    raise RuntimeError("the work failed")
            self.assertEqual(
                projscope.deadline(), outer,
                "a scope exited by an exception left its budget on the stack")

    def test_the_budget_is_per_thread(self):
        """ONE THREAD'S DEADLINE IS NOT ANOTHER'S.

        Scopes are thread-local by construction and the budget rides in the
        same state; a shared stack would let a background projection refuse
        work in a thread that never opened a budget.
        """
        seen = []

        def other():
            seen.append(projscope.deadline())
            with projscope.scope():
                seen.append(projscope.remaining())

        with projscope.scope(deadline=time.monotonic() + 20.0):
            self.assertIsNotNone(projscope.deadline())
            t = threading.Thread(target=other)
            t.start()
            t.join()
            # MUST-HIT on the same observable: this thread still has its
            # budget after the other thread ran, so the Nones below are the
            # other thread's answer and not a budget that quietly ended.
            self.assertIsNotNone(
                projscope.deadline(),
                "MUST-HIT: this thread lost its own budget, so the assertions "
                "about the other thread prove nothing")
        self.assertEqual(seen, [None, None],
                         "a budget crossed a thread boundary: %r" % (seen,))


if __name__ == "__main__":
    unittest.main()
