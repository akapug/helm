#!/usr/bin/env python3
"""Arms for /api/owed — the owner's burn-down surface (task/955).

THE REASON THESE ARMS INJECT A FAKE `helm.obligation`: on this trunk the module
does not exist, so a suite that only called the endpoint would exercise ONLY the
unavailable branch and report itself green having never rendered a single row.
That is the vacuous pass this codebase keeps paying for — the arm must drive the
POPULATED path too, and the only honest way to do that before the source lands
is to supply one.
"""
import inspect
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web_cache, web_owed  # noqa: E402

# THE CACHE KEY THIS ENDPOINT OWNS. /api/owed is served through
# `web_cache._cached_swr`, so a second call inside the floor answers from the
# FIRST call's body — and an arm that installs a new fixture and then reads a
# previous arm's answer is green about nothing. Every class below therefore
# starts from an empty cache, and leaves one behind.
_OWED_KEY = "owed"


def _clear_owed_cache():
    """Return this key to the state a fresh process is in.

    `_qinflight` IS POPPED AND ITS EVENT SET, never popped alone: a waiter
    blocked on an event nobody will ever set waits the full `_cached` timeout,
    and the arm that pays for it is whichever one runs next."""
    web_cache._qstate.pop(_OWED_KEY, None)
    web_cache._qcold.pop(_OWED_KEY, None)
    web_cache._qfresh.pop(_OWED_KEY, None)
    web_cache._qverified.pop(_OWED_KEY, None)
    web_cache._qrestored.discard(_OWED_KEY)
    event = web_cache._qinflight.pop(_OWED_KEY, None)
    if event is not None:
        event.set()


class _FreshBurnDown(unittest.TestCase):
    """Every arm below drives a BUILD, so every arm starts from an EMPTY
    cache and leaves one. Without this the second arm in a class reads the
    first one's body through a fixture it never saw."""

    def setUp(self):
        _clear_owed_cache()
        self.addCleanup(_clear_owed_cache)


class _FakeObligation:
    """Stands in for helm.obligation. Returns whatever the arm hands it.

    THE SIGNATURE IS PRODUCTION'S, and the double was POORER than production
    here in exactly the way this file has already been burned by twice. The
    real `unanswered_fixes(rows=None, unavailable=None)` has always accepted a
    ledger reading its caller already took; a double that accepted none made
    the endpoint's one-fold wiring unexpressible — the call would have raised
    TypeError, the broad except would have turned it into "unavailable", and
    four arms would have failed for a reason unrelated to what they test.

    `seen` RECORDS WHAT IT WAS HANDED, because "the endpoint folded once" is a
    claim about the ARGUMENT and not about the answer.
    """

    def __init__(self, items=(), forks=(), unreadable=None, raises=None):
        self._r = (list(items), list(forks), unreadable)
        self._raises = raises
        self.seen = []

    def unanswered_fixes(self, rows=None, unavailable=None):
        self.seen.append((rows, unavailable))
        if self._raises is not None:
            raise self._raises
        return self._r


class _Installed:
    """Install a fake helm.obligation for the duration of a block.

    RESTORES WHATEVER WAS THERE, including nothing. A test that leaves a
    synthetic module in sys.modules poisons every later test in the process,
    and the hub forbids local suites so I would not see it here.
    """

    def __init__(self, fake):
        self.fake = fake
        try:
            from helm import obligation as real
        except ImportError:
            real = None
        self.real = real

    def __enter__(self):
        import helm
        self.pkg = helm
        self.had = "helm.obligation" in sys.modules
        self.prev = sys.modules.get("helm.obligation")
        # THE PACKAGE ATTRIBUTE, NOT ONLY sys.modules — and this is the whole
        # reason this class exists rather than a one-line patch. `from . import
        # obligation` resolves through the PACKAGE ATTRIBUTE once the real
        # module has been imported, so a fake installed only in sys.modules is
        # silently ignored and the endpoint reads the live ledger instead.
        #
        # It worked before the module existed and broke the moment it landed,
        # which is the worst possible trigger: these arms were green for hours
        # while proving nothing they claimed, and only the fold exposed it.
        self.had_attr = hasattr(helm, "obligation")
        self.prev_attr = getattr(helm, "obligation", None)
        mod = types.ModuleType("helm.obligation")
        mod.unanswered_fixes = self.fake.unanswered_fixes
        # CARRY THE REAL MODULE'S CONSTANTS. The endpoint reads
        # obligation.UNDECLARED_VERDICT to split undeclared out of owed; a fake
        # without it raises AttributeError, the broad except turns that into
        # "unavailable", and four arms fail for a reason unrelated to what they
        # test.
        #
        # THIS IS THE SAME FIXTURE-VS-PRODUCTION LESSON AS THE `to` FIELD, in
        # the opposite direction. There the double was RICHER than production
        # and hid a defect; here it was POORER and invented one. A double has
        # to match the real thing in BOTH directions, which is why the schema
        # is copied rather than hand-listed.
        if self.real is not None:
            for name in dir(self.real):
                if name.isupper() and not name.startswith("_"):
                    setattr(mod, name, getattr(self.real, name))
            # AND THE HELPERS THE OTHER BUCKET CALLS THROUGH THIS MODULE.
            # _cured_rows resolves each repository with
            # obligation._root_for_repo, so a double that omits it makes the
            # CURED half unavailable whenever a test fakes the OWED half —
            # which is precisely the coupling the independence arm exists to
            # disprove, re-created by the fixture rather than by the code.
            #
            # Worth stating plainly: the two buckets DO share this module now.
            # They are independent in their FAILURE MODES, which is what the
            # arm asserts, not in their imports.
            for name in ("_root_for_repo",):
                if hasattr(self.real, name):
                    setattr(mod, name, getattr(self.real, name))
        sys.modules["helm.obligation"] = mod
        helm.obligation = mod
        return self

    def __exit__(self, *exc):
        if self.had:
            sys.modules["helm.obligation"] = self.prev
        else:
            sys.modules.pop("helm.obligation", None)
        if self.had_attr:
            self.pkg.obligation = self.prev_attr
        else:
            try:
                del self.pkg.obligation
            except AttributeError:
                pass
        return False


# THE REPOSITORY THE CURED FIXTURES LIVE IN. One literal, referenced by both
# _entry's default and CuredBucketTest._REPO, because the endpoint now DECIDES
# results on repo_id — a fixture whose entry disagreed with its snapshot would
# render an empty bucket that looks exactly like a genuinely empty one.
_REPO_ID = "/w/one/.git"


def _row(row="abc123", lane="lane-x", seat="a-seat"):
    return {"row": row, "lane": lane, "owed_seat": seat,
            "owed_since": "2026-08-01T00:00:00Z", "kind": "UNANSWERED_FIX",
            "repo_id": "/w/some-repo/.git", "what": "a thing"}


class OwedSurfaceTest(unittest.TestCase):
    """THE THREE STATES, each asserted as ITSELF rather than as not-the-others.

    THESE ARMS DRIVE `_owed_build`, THE BODY, AND NOT `_api_owed`, THE
    ENDPOINT, and the split is deliberate rather than incidental. /api/owed is
    served through a serve-stale cache, so a second call inside the floor
    answers with the FIRST call's body — and an arm that installs a new fixture
    and then reads a previous one's answer is green about nothing. Measured
    while this landed: the empty-ledger arm read `1 != 0` because the positive
    control two lines above it was still in the cache. What these arms are
    about is the BODY, so they ask the function that builds one; the serving
    behaviour has its own class at the foot of this file.
    """

    def test_a_populated_ledger_RENDERS_ITS_ROWS(self):
        """The positive control for every absence assertion below: if this
        cannot render a row, no refusal in this file means anything."""
        with _Installed(_FakeObligation(items=[_row(), _row(row="def456")])):
            out = web_owed._owed_build()
        self.assertFalse(out.get("unavailable"), out.get("why"))
        self.assertEqual(out["total"], 2)
        self.assertEqual([r["row"] for r in out["rows"]], ["abc123", "def456"])
        self.assertEqual(out["rows"][0]["lane"], "lane-x")
        self.assertEqual(out["rows"][0]["seat"], "a-seat")

    def test_an_ABSENT_module_is_UNAVAILABLE_not_an_empty_backlog(self):
        """The land-order state. This is the branch that runs on real trunk
        today, and the distinction it protects is the whole point: an owner
        who reads 'nothing owed' when the answer is 'cannot look' stops
        burning down a backlog that is still there."""
        import helm
        had = "helm.obligation" in sys.modules
        prev = sys.modules.pop("helm.obligation", None)
        # THE PACKAGE ATTRIBUTE TOO — correct hygiene, but it does NOT make
        # this branch reachable and an earlier version of this comment claimed
        # it did. `from . import obligation` RE-IMPORTS FROM DISK when the
        # attribute is missing, so on any tree carrying helm/obligation.py the
        # import succeeds and this arm SKIPS. Measured: the gate's skipped
        # count went 17 -> 18 the moment the module landed.
        #
        # The branch is defensive code with no reachable arm here. It stays
        # because it costs nothing and answers correctly if the module ever
        # goes missing — but it is NOT tested, and saying so is the point.
        had_attr = hasattr(helm, "obligation")
        prev_attr = getattr(helm, "obligation", None)
        if had_attr:
            del helm.obligation
        try:
            out = web_owed._owed_build()
        finally:
            if had:
                sys.modules["helm.obligation"] = prev
            if had_attr:
                helm.obligation = prev_attr
        # If obligation IS importable in this tree the branch cannot be
        # reached, and saying so is honest where skipping silently is not.
        if not out.get("unavailable"):
            self.skipTest("helm.obligation is importable here; the "
                          "absent-module branch is unreachable in this tree")
        self.assertIn("not on this trunk", out["why"])
        self.assertEqual(out.get("rows", []), [])

    def test_an_UNREADABLE_ledger_is_UNAVAILABLE_not_an_empty_backlog(self):
        with _Installed(_FakeObligation(unreadable="the ledger did not open")):
            out = web_owed._owed_build()
        self.assertTrue(out["unavailable"])
        self.assertIn("did not open", out["why"])

    def test_a_RAISING_ledger_degrades_instead_of_500ing(self):
        """A surface that throws is worse than one that says it cannot look:
        the owner gets a blank panel and no sentence explaining it."""
        with _Installed(_FakeObligation(raises=RuntimeError("boom"))):
            out = web_owed._owed_build()
        self.assertTrue(out["unavailable"])
        self.assertIn("boom", out["why"])

    def test_a_GENUINELY_EMPTY_ledger_is_EMPTY_and_says_so(self):
        """The state that must NOT be confused with unavailable, asserted from
        the other side: zero rows, but available."""
        # POSITIVE CONTROL FIRST, on the same observable: the SAME call shape
        # with one row present must produce that row. Without it, an endpoint
        # that returned an empty list unconditionally satisfies everything
        # below — which is exactly what an empty-vs-unavailable arm cannot
        # afford to miss.
        with _Installed(_FakeObligation(items=[_row()])):
            populated = web_owed._owed_build()
        self.assertEqual(populated["total"], 1)
        self.assertEqual(len(populated["rows"]), 1)

        with _Installed(_FakeObligation(items=[])):
            out = web_owed._owed_build()
        self.assertFalse(out.get("unavailable"), out.get("why"))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["rows"], [])

    def test_TRUNCATION_IS_REPORTED_never_silent(self):
        """A capped list that does not say it was capped reads as the whole
        backlog, and the owner would burn down a number that was never the
        number."""
        many = [_row(row="r%04d" % i) for i in range(web_owed.ROW_CAP + 25)]
        with _Installed(_FakeObligation(items=many)):
            out = web_owed._owed_build()
        self.assertEqual(out["total"], web_owed.ROW_CAP + 25)
        self.assertEqual(out["shown"], web_owed.ROW_CAP)
        self.assertEqual(out["truncated"], 25)
        self.assertEqual(len(out["rows"]), web_owed.ROW_CAP)

    def test_EXCLUDED_FORKS_travel_with_the_rows(self):
        """The owed verb and the dispatch triage verb disagree about forked-subtree
        descendants. The owner is shown that a judgement was applied rather
        than discovering it later from a different verb."""
        with _Installed(_FakeObligation(items=[_row()], forks=[1, 2, 3])):
            out = web_owed._owed_build()
        self.assertEqual(out["forks_excluded"], 3)
        self.assertEqual(out["total"], 1)


class OwedIsWiredTest(_FreshBurnDown):
    """The ENDPOINT, not just the function — a handler nothing routes to is a
    surface the owner cannot open, which is this row's entire complaint."""

    def test_the_route_is_registered_and_reaches_this_handler(self):
        from helm import web
        self.assertIn("/api/owed", web.API)
        self.assertEqual(web.API["/api/owed"].__name__, "_api_owed")

    def test_the_route_answers_without_raising(self):  # noqa: VACUOUS_ASSERTION — the positive control is the dict-shape assertion on the same call; a handler that raised would never reach it
        """THE REAL HANDLER ON THE REAL LEDGER, and it may now answer a THIRD
        shape. Since the burn-down is served through the serve-stale cache, a
        COLD call can legitimately come back `{"warming": true}` — the build
        has not finished inside the cold wait — and that body carries no
        `unavailable` key at all. Demanding one would fail on a healthy
        endpoint doing exactly what it was changed to do.

        THE BUILD IS DRAINED BEFORE THIS ARM RETURNS. `_cached_swr` leaves a
        REAL background fold running when it answers warming, and a later arm
        that installs a fixture would otherwise join that in-flight build and
        read the LIVE ledger's answer through its own double. Waiting here is
        what keeps the cache from becoming a cross-test channel.
        """
        from helm import web
        try:
            out = web.API["/api/owed"]()
        finally:
            event = web_cache._qinflight.get(_OWED_KEY)
            if event is not None:
                event.wait(300)
        self.assertIsInstance(out, dict)
        self.assertTrue("unavailable" in out or out.get("warming"),
                        "the burn-down answered a body naming NO state — "
                        "neither readable, unreadable, nor warming: %r"
                        % sorted(out))


if __name__ == "__main__":
    unittest.main()


class CuredBucketTest(unittest.TestCase):
    """THE SECOND OBLIGATION TYPE (task/955 slice two).

    Patches dispatches' own functions rather than installing a fake module,
    because `from . import dispatches` reads the PACKAGE ATTRIBUTE and a fake
    in sys.modules is silently bypassed — the failure that made six arms in
    this file green while they read the live system.
    """

    def _entry(self, rid="a1b2c3d4e5f6", lane="lane-x", seat="a-seat",
               branch="lane/x", tip="0123456789ab", ahead=3, repo=_REPO_ID):
        """REAL FIELD NAMES ONLY. The first version of this fixture invented
        `to`, which no dispatch row carries — so every arm passed while all
        eight production cards rendered "unassigned", and the gate could not
        see it because the fixture supplied its own reality."""
        return ({"id": rid, "lane": lane, "sender": seat, "recipient": "a-rev",
                 "ts": "2026-08-01T00:00:00Z", "repo_id": repo},
                (branch, tip, ahead))

    def test_the_FIXTURE_USES_FIELDS_REAL_ROWS_ACTUALLY_CARRY(self):
        """THE ARM THAT WOULD HAVE CAUGHT IT, and it checks the fixture rather
        than the code — because the defect was that my fixture was richer than
        production. A test double that invents a field cannot fail on the field
        being absent, which is the one thing that mattered here.

        Pinned against the LIVE schema: every key this fixture claims must
        exist on a real dispatch row.
        """
        # PINNED AGAINST A LITERAL, NOT ONLY THE LIVE LEDGER. The first
        # version of this arm read dispatches.snapshot() and SKIPPED when no
        # ledger was readable — which is the fab, where it matters most. An arm
        # that skips on the fab gate is an arm that does not exist there, and
        # this file already carries one branch I documented as unreachable; two would be
        # a pattern rather than an accident.
        #
        # These are the fields a dispatch row actually carries that this
        # fixture depends on, read off a live row at cure time.
        KNOWN_ROW_FIELDS = {
            "id", "lane", "sender", "recipient", "recipient_display", "ts",
            "kind", "status", "tip", "ref", "reviewed_tip", "supersedes",
            "chain_root", "repo_id", "verdict_ref", "delivery", "note",
            "polarity", "gate", "seq", "v", "event", "source", "migration",
            "deadline_s", "message_hash", "operation_key", "delivery_ref",
        }
        fixture_row, _meta = self._entry()

        # UNCONDITIONAL POSITIVE CONTROL, and this arm needs one more than most:
        # its whole claim is an EMPTY difference, which is exactly what a
        # comparison that cannot discriminate also produces. Prove the check
        # CATCHES an invented field before believing it found none — the
        # original defect was a fixture carrying `to`, so that is the probe.
        planted = dict(fixture_row)
        planted["to"] = "a-seat"
        self.assertEqual(sorted(set(planted) - KNOWN_ROW_FIELDS), ["to"],
                         "this check cannot detect an invented field, so its "
                         "empty result below means nothing")

        invented = set(fixture_row) - KNOWN_ROW_FIELDS
        self.assertEqual(invented, set(),
                         "the fixture invents field(s) no real row carries, so "
                         "every arm using it is blind to production: %r"
                         % sorted(invented))

        # AND CROSS-CHECK THE LITERAL AGAINST THE LIVE LEDGER WHEN THERE IS
        # ONE, so the literal cannot rot into its own fiction. This half may
        # legitimately not run; the half above always does.
        from helm import dispatches
        snap, unavailable = dispatches.snapshot()
        if unavailable or not snap:
            return
        real_keys = set()
        for row in snap.values():
            if isinstance(row, dict):
                real_keys |= set(row.keys())
        self.assertIn("sender", real_keys)
        self.assertIn("recipient", real_keys)
        stale = (KNOWN_ROW_FIELDS & set(fixture_row)) - real_keys
        self.assertEqual(stale, set(),
                         "the literal above lists field(s) the live ledger no "
                         "longer carries: %r" % sorted(stale))

    def test_the_FIXTURE_CARRIES_EVERY_FIELD_PRODUCTION_READS(self):
        """THE CONVERSE OF THE ARM ABOVE, and the gap it left cost a red gate.

        That arm pins ONE direction: no field the fixture invents. It cannot
        fail on a field production READS that the fixture OMITS — and that is
        the defect that actually landed. When the endpoint began deciding
        results on repo_id, this fixture had no repo_id, every entry was
        filtered out, and four arms read `0 != N` on code that was correct.

        Two faces of one lesson, and the sign flipped between them: the `to`
        incident was a fixture RICHER than production, this one POORER. An arm
        per direction, because neither can see the other's failure.

        DERIVED FROM PRODUCTION SOURCE, NOT HAND-LISTED. A hand-written list is
        the thing that just failed — it records what the author remembered on
        the day. Reading `row.get("...")` out of the module means a field added
        to the endpoint tomorrow fails here rather than silently emptying a
        bucket.
        """
        import re
        src = inspect.getsource(web_owed)

        # THE DERIVATION'S PREMISE, ASSERTED. This reads `row.get(...)` and
        # calls the result "what a cured ENTRY must carry" — which is only true
        # while `row` names an entry and nothing else. It stopped being true
        # once: a successor map bound `row` to a LEDGER row, the derivation
        # picked up `supersedes`, and this arm demanded a field no cured entry
        # has. Adding it to the fixture would have been the `to` bug again, so
        # the code names ledger rows `ledger_row` and this pins that naming.
        self.assertNotIn(
            "for row in snap.values()", src,
            "`row` is bound to a LEDGER row somewhere in web_owed, so the "
            "read-set derived below is a mix of two different object shapes "
            "and the assertion it feeds is meaningless — name ledger rows "
            "`ledger_row`")

        reads = set(re.findall(r'\brow\.get\("([a-z_]+)"', src))

        # MUST-HIT BEFORE TRUSTING THE SET. This arm's claim is an empty
        # difference, and a regex that matches nothing produces exactly that —
        # a green result meaning "I looked for nothing and found it all". If
        # the endpoint stops spelling its reads `row.get(...)`, this must fail
        # loudly rather than pass vacuously.
        self.assertIn("id", reads,
                      "the derivation found no cured-entry id read, so it is no "
                      "longer reading production and its result below is "
                      "meaningless")
        self.assertGreaterEqual(len(reads), 4, sorted(reads))

        fixture_row, _meta = self._entry()
        missing = reads - set(fixture_row)
        self.assertEqual(missing, set(),
                         "production reads field(s) this fixture does not "
                         "carry, so every arm using it tests a row real code "
                         "would handle differently: %r" % sorted(missing))

    # A DEFAULT SNAPSHOT WITH ONE RESOLVABLE REPO. It used to default to EMPTY,
    # which was harmless while the endpoint ignored the snapshot and became a
    # silent zero the moment it started grouping repos from it — four arms went
    # red at once on a change that was correct. Same lesson as the invented
    # `to` field, third face: a double must track what production READS, not
    # only what it returns.
    _REPO = _REPO_ID

    def _patch(self, snap=(None, None), cured=((), None)):
        from unittest import mock
        from helm import dispatches, obligation
        default = {"r1": {"id": "r1", "repo_id": self._REPO, "lane": "l"}}
        rows = snap[0] if snap[0] is not None else default
        entries, err = cured
        facts = {"eligible": len(entries), "scanned_repos": 1,
                 "blind_repos": 0, "blind_rows": 0,
                 "ambiguous": int(bool(err and "ambiguous" in str(err)))}
        problems = [err] if err else []
        return (mock.patch.object(dispatches, "snapshot",
                                  lambda: (rows, snap[1])),
                mock.patch.object(dispatches, "cured_by_repo",
                                  lambda *a, **k: (list(entries), problems, facts)),
                mock.patch.object(obligation, "_root_for_repo",
                                  lambda r, carried=None: carried or (
                                      "/w/one" if r == self._REPO else None)))

    def test_a_populated_cured_bucket_RENDERS_ITS_ROWS(self):
        a, b, c3 = self._patch(cured=([self._entry(), self._entry(rid="ffffffffffff")], None))
        with a, b, c3:
            out = web_owed._owed_build()
        c = out["cured"]
        self.assertFalse(c["unavailable"], c.get("why"))
        self.assertEqual(c["total"], 2)
        self.assertEqual([r["row"] for r in c["rows"]],
                         ["a1b2c3d4e5f6", "ffffffffffff"])
        self.assertEqual(c["rows"][0]["ahead"], 3)
        self.assertEqual(c["rows"][0]["branch"], "lane/x")
        # THE SEAT MUST RENDER. Eight production cards read "unassigned"
        # because this was never asserted and the fixture hid the cause.
        self.assertEqual(c["rows"][0]["seat"], "a-seat")
        self.assertEqual(c["rows"][0]["reviewer"], "a-rev")

    def test_a_BROKEN_cured_WALK_is_PARTIAL_not_a_clean_empty_bucket(self):
        """A per-repo diagnostic survives without erasing rows from other repos."""
        a, b, c3 = self._patch(cured=([], "git could not answer"))
        with a, b, c3:
            out = web_owed._owed_build()
        c = out["cured"]
        self.assertFalse(c["unavailable"])
        self.assertTrue(c["partial"])
        self.assertIn("git could not answer", c["why"])

    def test_an_UNREADABLE_dispatch_ledger_is_UNAVAILABLE(self):
        a, b, c3 = self._patch(snap=({}, "the ledger did not open"))
        with a, b, c3:
            out = web_owed._owed_build()
        self.assertTrue(out["cured"]["unavailable"])
        self.assertIn("did not open", out["cured"]["why"])

    def test_a_GENUINELY_EMPTY_cured_bucket_is_EMPTY_not_unavailable(self):
        # POSITIVE CONTROL FIRST on the same observable: one entry renders.
        a, b, c3 = self._patch(cured=([self._entry()], None))
        with a, b, c3:
            populated = web_owed._owed_build()["cured"]
        self.assertEqual(populated["total"], 1)

        a, b, c3 = self._patch(cured=([], None))
        with a, b, c3:
            out = web_owed._owed_build()["cured"]
        self.assertFalse(out["unavailable"], out.get("why"))
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["rows"], [])

    def test_THE_BUCKETS_ARE_INDEPENDENT_when_the_owed_half_goes_blind(self):
        """THE PROPERTY I NEARLY SHIPPED BROKEN. My first wiring called the
        cured renderer AFTER the owed half's early return, so an owed source
        that could not answer silently blanked a cured bucket that could.

        Two questions over two sources: an owner who loses the unanswered-FIX
        list must still see the rows whose authors cured and never handed back.
        """
        from unittest import mock
        a, b, c3 = self._patch(cured=([self._entry()], None))
        with a, b, c3, _Installed(_FakeObligation(unreadable="owed side is blind")):
            out = web_owed._owed_build()
        self.assertTrue(out["unavailable"], "the owed half should be blind here")
        self.assertFalse(out["cured"]["unavailable"],
                         "the cured bucket went blind because the OWED half "
                         "did — the two are coupled")
        self.assertEqual(out["cured"]["total"], 1)


class UndeclaredIsNotOwedTest(unittest.TestCase):
    """The post-land FIX on lane 23, as arms.

    cmd_owed defines an UNDECLARED verdict as NEITHER owed nor clear — a third
    state that exists because the two-way reading is what made 26 of these
    vanish from a burn-down whose whole job is completeness. The first version
    of this surface counted them inside "owed", which put them back in the
    opposite direction: the owner read 116 debts where 91 were owed and 26 were
    unanswerable.

    IT OVER-REPORTED IN THE SAFE DIRECTION, which is why it would have
    survived. Nobody is alarmed by a backlog that looks too long.
    """

    def _item(self, kind, row="r1", lane="lane-a"):
        return {"kind": kind, "row": row, "lane": lane, "owed_seat": "s",
                "owed_since": "2026-08-01T00:00:00Z", "what": "x"}

    def _run(self, items):
        # _Installed copies the real module's constants onto the double, so
        # nothing is hand-wired here — see its __enter__ for why that matters
        # in BOTH directions.
        with _Installed(_FakeObligation(items=items)):
            return web_owed._owed_build()

    def test_an_UNDECLARED_row_is_NOT_counted_as_owed(self):
        from helm import obligation as real
        out = self._run([
            self._item(real.UNANSWERED_FIX, row="fix1"),
            self._item(real.UNANSWERED_FIX, row="fix2"),
            self._item(real.UNDECLARED_VERDICT, row="und1"),
        ])
        # POSITIVE CONTROL FIRST: the fix rows ARE owed, so a zero below is
        # discrimination and not an endpoint that counts nothing.
        self.assertEqual(out["total"], 2, "the unanswered-fix rows were lost")
        self.assertEqual([r["row"] for r in out["rows"]], ["fix1", "fix2"])
        self.assertNotIn("und1", [r["row"] for r in out["rows"]],
                         "an UNDECLARED row was rendered as an owed debt")

    def test_UNDECLARED_rows_get_their_OWN_bucket(self):
        from helm import obligation as real
        out = self._run([
            self._item(real.UNANSWERED_FIX, row="fix1"),
            self._item(real.UNDECLARED_VERDICT, row="und1"),
            self._item(real.UNDECLARED_VERDICT, row="und2"),
        ])
        u = out["undeclared"]
        self.assertEqual(u["total"], 2)
        self.assertEqual([r["row"] for r in u["rows"]], ["und1", "und2"])

    def test_the_two_buckets_PARTITION_the_source_losing_nothing(self):
        """A split that drops rows is worse than the merge it replaced — the
        owner would read a SHORTER backlog than exists, which is the unsafe
        direction the original bug happened to avoid."""
        from helm import obligation as real
        items = ([self._item(real.UNANSWERED_FIX, row="f%d" % i) for i in range(5)]
                 + [self._item(real.UNDECLARED_VERDICT, row="u%d" % i) for i in range(3)])
        out = self._run(items)
        self.assertEqual(out["total"] + out["undeclared"]["total"], len(items),
                         "the split lost or duplicated rows")
        self.assertEqual(out["total"], 5)
        self.assertEqual(out["undeclared"]["total"], 3)


class CuredBucketDoesNotDependOnCwdTest(unittest.TestCase):
    """THE DEFECT THAT REACHED THE OWNER'S CONSOLE, as an arm.

    helm-web runs as a systemd --user service whose cwd is the operator's home
    directory rather than a checkout. The original control called
    cured_unwitnessed on the live ledger without a root and expected an error.
    Rows now carry repo_root, so the endpoint can legitimately answer from
    outside a checkout and the old direct call no longer proves its placement
    mechanism is reachable.

    This arm plants one real cured candidate in a synthetic repository. The
    rooted and rootless calls differ ONLY by that row field, under the same
    non-repository cwd; the endpoint claim then runs on the intact synthetic
    snapshot rather than depending on whichever rows happen to be live.
    """

    def test_the_cured_bucket_resolves_from_a_NON_REPO_cwd(self):
        import shutil
        import subprocess
        import tempfile
        from unittest import mock
        from helm import dispatches

        tmp = tempfile.mkdtemp(prefix="not-a-repo-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = os.path.join(tmp, "repo")
        outside = os.path.join(tmp, "outside")
        gitdir = os.path.join(tmp, "elsewhere.git")
        os.makedirs(outside)
        subprocess.run(["git", "init", "-q", "--separate-git-dir", gitdir,
                        repo], check=True, capture_output=True)

        def git(*args):
            p = subprocess.run(["git", "-C", repo, *args], check=True,
                               capture_output=True, text=True)
            return p.stdout.strip()

        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        state = os.path.join(repo, "state")
        with open(state, "w", encoding="utf-8") as f:
            f.write("base\n")
        git("add", "state")
        git("commit", "-q", "-m", "base")
        trunk = git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "side")
        with open(state, "a", encoding="utf-8") as f:
            f.write("reviewed\n")
        git("commit", "-qam", "reviewed")
        reviewed = git("rev-parse", "HEAD")
        with open(state, "a", encoding="utf-8") as f:
            f.write("cure\n")
        git("commit", "-qam", "cure")
        cure = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", trunk)

        row = {"id": "rooted-cure", "lane": "lane-x",
               "sender": "author", "recipient": "reviewer",
               "polarity": "fix", "reviewed_tip": reviewed,
               "status": "verdict", "ts": "2026-01-01T00:00:00Z",
               "repo_id": gitdir, "repo_root": repo}
        snap = {row["id"]: row}
        here = os.getcwd()
        try:
            os.chdir(outside)
            # POSITIVE CONTROL FIRST: production _api_owed consumes the intact
            # persisted row field and finds this exact cure without a resolver
            # patch or any checkout in the process cwd.
            with _Installed(_FakeObligation()), \
                    mock.patch.object(dispatches, "snapshot",
                                      return_value=(snap, None)):
                out = web_owed._owed_build()
            cured = out["cured"]
            self.assertFalse(cured["unavailable"], cured.get("why"))
            self.assertEqual(cured["total"], 1)
            self.assertEqual(cured["rows"][0]["tip"], cure[:12])

            # NEGATIVE CONTROL: remove ONLY the authoritative persisted field
            # from the same candidate. Its separate gitdir cannot be inverted to
            # a checkout, so the same endpoint must report the row unplaceable.
            rootless_row = dict(row)
            rootless_row.pop("repo_root")
            rootless = {rootless_row["id"]: rootless_row}
            with _Installed(_FakeObligation()), \
                    mock.patch.object(dispatches, "snapshot",
                                      return_value=(rootless, None)):
                without_root = web_owed._owed_build()
        finally:
            os.chdir(here)

        self.assertEqual(without_root["cured"]["measured"], 0)
        self.assertEqual(without_root["cured"]["total"], 1)
        self.assertEqual(without_root["cured"]["unplaceable"], 1)
        self.assertEqual(out["cured"]["rows"][0]["row"], row["id"][:12])


class CuredBucketScansEveryRepoTest(unittest.TestCase):
    """The ledger is GLOBAL and the scan was rooted only in helm.

    Measured on the live ledger: rows from four repositories. Handing the whole
    snapshot to a scan rooted in one of them returns 0 rows and err=None for
    every other repo — SILENTLY ABSENT, which is the single answer this surface
    exists to never give. The cure walks branches, so a row whose branches live
    in another checkout is invisible rather than erroring.
    """

    def test_rows_are_scanned_PER_REPOSITORY_and_unplaceable_ones_are_COUNTED(self):
        from unittest import mock
        from helm import dispatches, obligation

        # POLARITY IS PART OF THE SNAPSHOT SHAPE, not decoration. unplaceable
        # counts only fix-polarity rows — the finding, that a count of 7
        # was really 1, because a row with no fix polarity could never have been
        # a cure candidate and inflates the number the owner is asked to burn
        # down. A snapshot double without polarity therefore reports 0
        # unplaceable no matter how many repos it fails to resolve.
        #
        # This is a DIFFERENT double from the cured-entry one that
        # test_the_FIXTURE_CARRIES_EVERY_FIELD_PRODUCTION_READS pins: that arm
        # derives `row.get(...)` reads off the cured entries, while polarity is
        # read from SNAPSHOT rows. Two objects, two shapes, and the pin on one
        # says nothing about the other.
        snap = {
            "a": {"id": "a", "repo_id": "/w/one/.git", "lane": "l1",
                  "polarity": "fix", "reviewed_tip": "a" * 40},
            "b": {"id": "b", "repo_id": "/w/two/.git", "lane": "l2",
                  "polarity": "fix", "reviewed_tip": "b" * 40},
            # AND reviewed_tip, because unplaceable now admits on the
            # classifier's index-free clauses rather than polarity alone
            # (second round): a fix row with nothing cured is not a
            # candidate for anything. Without this field the count is 0 and the
            # arm below would be measuring the new predicate's refusal rather
            # than the placement it is about.
            "c": {"id": "c", "repo_id": "", "lane": "l3", "polarity": "fix",
                  "reviewed_tip": "0123456789abcdef0123456789abcdef01234567"},
        }
        seen_roots = []

        def fake_cured(s, ids=None, index=None, root=None, trunk=None):
            seen_roots.append(root)
            # ONE CURED ROW PER REPO, so a dropped repo shows up as a lost row —
            # and the row is BUILT FROM THE SNAPSHOT rather than invented, which
            # is what the real cured_unwitnessed does. That matters now that the
            # endpoint decides results on repo_id: a hand-written row carries
            # whichever repo the fixture author remembered, while a row drawn
            # from `s` carries the one the scan was actually asked about. The
            # invented version returned rows with NO repo_id at all and every
            # arm here read 0 — a fixture poorer than production, the same
            # lesson as the invented `to` field with its sign flipped.
            rid = (ids or ["?"])[0]
            row = dict(s.get(rid) or {"id": rid})
            row.setdefault("lane", "L")
            row.update({"sender": "s", "recipient": "r",
                        "ts": "2026-08-01T00:00:00Z"})
            return ([(row, ("br", "0123456789ab", 1))], None)

        real_by_repo = dispatches.cured_by_repo
        with mock.patch.object(dispatches, "snapshot", lambda: (snap, None)), \
                mock.patch.object(dispatches, "cured_unwitnessed", fake_cured), \
                mock.patch("helm.landreq._close_trunk",
                           side_effect=lambda _row, _repo, _trunk:
                                  ("refs/heads/main", "f" * 40, "local", None)), \
                mock.patch.object(dispatches, "cured_by_repo",
                                  side_effect=lambda s: real_by_repo(s)), \
                mock.patch.object(obligation, "_root_for_repo",
                                  lambda r, carried=None: carried or (
                                      "/w/one" if r == "/w/one/.git"
                                      else ("/w/two" if r == "/w/two/.git"
                                            else None))):
            out = web_owed._owed_build()

        cured = out["cured"]
        # BOTH placeable repos were scanned — a single-root scan would show one.
        self.assertEqual(sorted(seen_roots), ["/w/one", "/w/two"],
                         "the scan did not run per repository")
        self.assertEqual(cured["measured"], 2, "a repository's rows were dropped")
        self.assertEqual(cured["total"], 3,
                         "the total must include the unplaceable candidate")
        # AND THE UNRESOLVABLE ONE IS COUNTED, not silently omitted.
        self.assertEqual(cured["unplaceable"], 1,
                         "a row whose repo could not be resolved vanished "
                         "instead of being reported")

    def test_a_STALE_carried_root_does_not_hide_a_live_sibling_checkout(self):  # noqa: VACUOUS_ASSERTION — the spy's ordered candidates and the exact cured call together prove the stale root was rejected and the live sibling selected
        from unittest import mock
        from helm import dispatches, obligation

        snap = {
            "old": {"id": "old", "repo_id": "/shared.git",
                    "repo_root": "/deleted", "polarity": "fix",
                    "reviewed_tip": "a" * 40},
            "live": {"id": "live", "repo_id": "/shared.git",
                     "repo_root": "/live", "polarity": "fix",
                     "reviewed_tip": "b" * 40},
        }
        tried = []

        def root(_repo, carried=None):
            tried.append(carried)
            return carried if carried == "/live" else None

        seen = []

        def cured(_snap, ids=None, root=None, **_kw):
            seen.append((tuple(ids or ()), root))
            return [], None

        with mock.patch.object(dispatches, "snapshot",
                               return_value=(snap, None)), \
                mock.patch.object(obligation, "_root_for_repo", root), \
                mock.patch("helm.landreq._close_trunk",
                           return_value=("refs/heads/main", "f" * 40,
                                         "local", None)), \
                mock.patch.object(dispatches, "cured_unwitnessed", cured):
            web_owed._owed_build()

        self.assertEqual(tried, ["/deleted", "/live"])
        self.assertEqual(seen, [(('live', 'old'), "/live")])

    def test_partial_diagnostics_preserve_that_repos_rows_and_later_repos(self):
        from unittest import mock
        from helm import dispatches, obligation

        snap = {
            "a": {"id": "a", "repo_id": "/a/.git", "polarity": "fix",
                  "reviewed_tip": "a" * 40},
            "b": {"id": "b", "repo_id": "/b/.git", "polarity": "fix",
                  "reviewed_tip": "b" * 40},
        }

        def entry(row, tip):
            return [(dict(row, sender="s", recipient="r", lane="l", ts="t"),
                     ("branch", tip, 1))]

        def cured(s, ids=None, root=None, **_kw):
            rid = ids[0]
            if root == "/a":
                return entry(s[rid], "1" * 40), "ambiguous live cure carriers"
            return entry(s[rid], "2" * 40), None

        with mock.patch.object(dispatches, "snapshot", return_value=(snap, None)), \
                mock.patch.object(obligation, "_root_for_repo",
                                  side_effect=lambda r, carried=None:
                                  "/a" if r == "/a/.git" else "/b"), \
                mock.patch("helm.landreq._close_trunk",
                           side_effect=lambda _row, repo, _trunk:
                           ("refs/heads/main", "f" * 40, "local", None)), \
                mock.patch.object(dispatches, "cured_unwitnessed", cured):
            out = web_owed._owed_build()["cured"]

        self.assertFalse(out["unavailable"])
        self.assertTrue(out["partial"])
        self.assertIn("ambiguous live cure carriers", out["why"])
        self.assertEqual(sorted(row["row"] for row in out["rows"]), ["a", "b"])
        self.assertEqual(out["unplaceable"], 0)

    def test_UNPLACEABLE_ADMITS_THE_SAME_POPULATION_THE_CLASSIFIER_WOULD(self):
        """PARITY AS A TEST, because it was asked for twice and the second
        time the prose already claimed it.

        A row we cannot place is UNKNOWN, not absent — but only if it could
        have been a cure at all. cure_state owns that judgement and cannot be
        called without an index (given None it answers CURE_UNKNOWN on its
        first line, before reading anything), so dispatches.cure_candidate
        carries its INDEX-FREE clauses. This arm is what keeps the two honest.
        """
        from helm import dispatches

        TIP = "0123456789abcdef0123456789abcdef01234567"

        # THE DIRECTION THAT MATTERS: anything the classifier calls
        # CURE_AWAITING must be admitted by the predicate. If it were not, a
        # repo going unresolvable would DROP a row the placeable path counts.
        row = {"id": "r", "polarity": "fix", "reviewed_tip": TIP}
        snap = {"r": row}
        git_index = {TIP: ("lane/x", "f" * 40)}
        state, _where = dispatches.cure_state(row, git_index, ())
        self.assertEqual(state, dispatches.CURE_AWAITING)
        self.assertTrue(dispatches.cure_candidate(row, snap))

        self.assertFalse(dispatches.cure_candidate(
            {"id": "r", "polarity": "fix"}, snap))
        self.assertFalse(dispatches.cure_candidate(
            {"id": "r", "polarity": "approve", "reviewed_tip": TIP}, snap))
        retired = dict(row, withdrawn=True)
        self.assertFalse(dispatches.cure_candidate(retired, {"r": retired}))

        # CANCELLED is pass-through, not a holder. A live grandchild beyond it
        # does hold the debt; raw successor presence cannot answer either case.
        dead = {"id": "dead", "status": "cancelled", "supersedes": "r",
                "chain_root": "r"}
        self.assertTrue(dispatches.cure_candidate(row, {"r": row, "dead": dead}))
        live = {"id": "live", "status": "open", "supersedes": "dead",
                "chain_root": "r"}
        self.assertFalse(dispatches.cure_candidate(
            row, {"r": row, "dead": dead, "live": live}))


class BurnDownIsServedStaleTest(_FreshBurnDown):
    """THE FLAP, AS ARMS. Measured on the owner's box: GET /api/owed took about
    a minute, the card fetches it with an eight second deadline every
    forty-five seconds, so the burn-down ALWAYS printed a timeout — and each
    abandoned fetch left a fold running, so one open tab kept the web process
    folding the ledger permanently and every other endpoint slowed down behind
    it.

    Two cures, two halves of this class: ONE FOLD PER BUILD (one reading of the
    ledger serves both buckets, where a reading per bucket costs a second fold
    and lets the two halves describe two instants), and the build is served
    through the serve-stale cache /api/lr already uses.
    """

    _SNAP = {"a": {"id": "a", "repo_id": "", "lane": "lane/a",
                   "status": "open", "polarity": "fix"}}

    def _counting_snapshot(self, result=None):
        """(patcher, calls) — `dispatches.snapshot` with a tally.

        The tally is the whole instrument: "one fold per build" is a claim
        about how many times the ledger was READ, which no property of the
        answer can express."""
        from unittest import mock
        from helm import dispatches
        calls = []

        def snapshot():
            calls.append(1)
            return result if result is not None else (dict(self._SNAP), None)

        return mock.patch.object(dispatches, "snapshot", snapshot), calls

    def test_ONE_BUILD_TAKES_ONE_LEDGER_READING(self):
        """THE DEFECT THIS LANE EXISTS FOR, on the only observable that can
        show it. `_cured_rows` folded the ledger and `obligation.
        unanswered_fixes` folded it again, so every response paid twice for
        the same eleven megabytes — and the two buckets rendered side by side
        could describe two different instants.

        THE REAL obligation MODULE IS USED HERE ON PURPOSE. A double cannot
        fold anything, so the second read would be invisible to it; the module
        that actually takes the second reading has to be the one running.

        COUNTERFACTUAL: restoring `obligation.unanswered_fixes()` with no
        arguments makes this two.
        """
        patcher, calls = self._counting_snapshot()
        with patcher:
            body = web_owed._owed_build()
        # POSITIVE CONTROL: the build ran and produced a real body. A build
        # that folded ZERO times would also satisfy "not two".
        self.assertEqual(len(calls), 1,
                         "the burn-down folded the dispatch ledger %d times "
                         "for ONE body" % len(calls))
        self.assertFalse(body.get("unavailable"), body.get("why"))
        self.assertIn("cured", body)

    def test_THE_FOLD_RUNS_INSIDE_ONE_PROJECTION_SCOPE(self):
        """The build opens `projscope.scope()` around its one ledger reading.
        Measured on a frozen ledger: without the scope the same build pays 498
        more git spawns and 14 s more wall, and the cured rows' branch, tip and
        ahead counts are resolved against reads taken tens of seconds apart
        inside one body. Nothing about the ANSWER shows that, so the arm asks
        the scope itself, from inside the fold.

        COUNTERFACTUAL: replacing `with projscope.scope():` with `if True:`
        makes the reading inside the fold False.
        """
        from unittest import mock
        from helm import dispatches, projscope
        seen = []

        def snapshot():
            seen.append(projscope.active())
            return dict(self._SNAP), None

        # CONTROL: the instrument can read False. No scope is open out here,
        # so a True below is the build's doing and not the fixture's.
        self.assertFalse(projscope.active())
        with mock.patch.object(dispatches, "snapshot", snapshot):
            body = web_owed._owed_build()
        self.assertFalse(body.get("unavailable"), body.get("why"))
        self.assertEqual(seen, [True],
                         "the burn-down folded the ledger outside a "
                         "projection scope (or not exactly once): %r" % seen)
        self.assertFalse(projscope.active(), "the build left its scope open")

    def test_THE_SAME_ROWS_REACH_BOTH_BUCKETS(self):
        """The other face of one fold: not just ONE read, but the SAME read on
        both sides. Two reads that happened to agree would pass the count arm
        if someone re-derived the second one from a cache."""
        fake = _FakeObligation(items=[_row()])
        patcher, calls = self._counting_snapshot()
        with patcher, _Installed(fake):
            web_owed._owed_build()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(fake.seen), 1,
                         "the owed half was not asked exactly once")
        rows, unavailable = fake.seen[0]
        self.assertIsNone(unavailable)
        self.assertEqual(rows, self._SNAP,
                         "the owed half was handed %r rather than the rows "
                         "the build read" % (rows,))

    def test_A_POLL_INSIDE_THE_FLOOR_IS_SERVED_not_rebuilt(self):
        """THE OWNER-VISIBLE HALF. Before the cache, every poll blocked on a
        whole fold and the card aborted at eight seconds — and the next poll
        started another one. Three polls must cost ONE fold."""
        patcher, calls = self._counting_snapshot()
        with patcher:
            first = web_owed._api_owed()
            second = web_owed._api_owed()
            third = web_owed._api_owed()
        self.assertEqual(len(calls), 1,
                         "three polls cost %d folds — the endpoint is not "
                         "being served from the cache" % len(calls))
        # UNCONDITIONAL POSITIVE CONTROL: the served body is a real burn-down.
        # One fold for three polls is also what an endpoint that answered
        # nothing at all would produce.
        self.assertFalse(first.get("unavailable"), first.get("why"))
        self.assertIn("cured", first)
        self.assertFalse(second.get("unavailable"), second.get("why"))
        self.assertIn("cured", second)
        self.assertFalse(third.get("unavailable"), third.get("why"))
        self.assertIn("cured", third)

    def test_THE_CACHE_IS_ASKED_FOR_EVERY_TERM_THE_DESIGN_NEEDS(self):
        """A SPY ON THE CALL, because three of these arguments have no other
        observable. Dropping `fingerprint`/`unchanged_max` costs three
        thousand git spawns per floor over a ledger nobody wrote to and
        changes no answer; dropping `cold_body` turns a cold start back into
        "✗ CANNOT SEE the burn-down". Neither fails any arm above."""
        from unittest import mock
        seen = {}
        real = web_cache._cached_swr

        def spy(key, ttl, hard_ttl, fn, **kw):
            seen.update(kw)
            seen.update({"key": key, "ttl": ttl, "hard_ttl": hard_ttl,
                         "fn": fn})
            return real(key, ttl, hard_ttl, fn, **kw)

        patcher, _calls = self._counting_snapshot()
        with patcher, mock.patch.object(web_cache, "_cached_swr", spy):
            web_owed._api_owed()
        self.assertEqual(seen.get("key"), _OWED_KEY)
        self.assertEqual(seen.get("ttl"), web_owed._OWED_FLOOR_S)
        self.assertEqual(seen.get("hard_ttl"), web_owed._OWED_HARD_TTL_S)
        self.assertIs(seen.get("fn"), web_owed._owed_build)
        self.assertEqual(seen.get("cold_body"), {"warming": True})
        self.assertEqual(seen.get("cold_wait"), web_owed._OWED_COLD_WAIT_S)
        self.assertIs(seen.get("fingerprint"), web_owed._owed_fingerprint)
        self.assertEqual(seen.get("unchanged_max"),
                         web_owed._OWED_UNCHANGED_MAX_S)

    def test_A_COLD_READ_ANSWERS_WARMING_rather_than_an_alarm(self):  # noqa: ORPHANED_MOCK — `_owed_build` is HANDED to `_cached_swr` as a value and never called by name, so no name-following walker can reach it; `started.wait(5)` below is the unconditional proof that the double actually fired
        """WARMING AND UNREADABLE ARE DIFFERENT FACTS, and only the server can
        tell them apart: the ledger is perfectly readable, it has not been
        COMPUTED. Saying "cannot see the burn-down" on every view after a
        restart is a false claim about the record.

        The cold wait is shortened here so the arm costs a fifth of a second
        instead of the production wait; the production NUMBER is pinned
        against the card's fetch deadline by its own arm below."""
        from unittest import mock
        started = threading.Event()
        release = threading.Event()

        def slow_build():
            started.set()
            release.wait(30)
            return {"unavailable": False, "rows": [], "total": 0,
                    "cured": {"unavailable": False, "rows": [], "total": 0},
                    "read_ts": time.time()}

        with mock.patch.object(web_owed, "_owed_build", slow_build), \
                mock.patch.object(web_owed, "_OWED_COLD_WAIT_S", 0.2):
            try:
                body = web_owed._api_owed()
            finally:
                release.set()
                event = web_cache._qinflight.get(_OWED_KEY)
                if event is not None:
                    event.wait(30)
        self.assertTrue(started.wait(5), "no build was started at all")
        self.assertTrue(body.get("warming"),
                        "a cold burn-down answered %r instead of warming"
                        % sorted(body))
        self.assertNotIn("unavailable", body,
                         "a body nothing has read yet claims a READ state")
        self.assertIsNone(body.get("read_age_s"),
                          "a body that was never read carries an age")

    def test_THE_COLD_WAIT_FITS_INSIDE_THE_CARDS_FETCH_DEADLINE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the must-hit `len(found) == 1`: the card's deadline is READ off the page and its absence fails the arm before any comparison
        """THE TWO NUMBERS ARE ONE DECISION AND THEY LIVE IN TWO FILES. A cold
        wait longer than the deadline aborts the very fetch it exists to let
        finish, and the card prints the timeout again with a warming body
        sitting unread behind it."""
        import re
        from helm import web_ui_loader
        found = re.findall(r'j\("/api/owed",\s*(\d+)\)',
                           web_ui_loader.read_text())
        # THE MUST-HIT. A renamed helper or a moved literal would leave this
        # arm comparing against nothing and passing.
        self.assertEqual(len(found), 1,
                         "the burn-down's fetch deadline could not be read "
                         "off the page at all: %r" % (found,))
        deadline_s = int(found[0]) / 1000.0
        self.assertLess(web_owed._OWED_COLD_WAIT_S, deadline_s,
                        "the cold wait (%ss) outlives the card's fetch "
                        "deadline (%ss)" % (web_owed._OWED_COLD_WAIT_S,
                                            deadline_s))

    def test_THE_FLOOR_OUTLASTS_A_BUILD_and_the_caps_are_ordered(self):
        """WHY THESE NUMBERS ARE THESE NUMBERS, as relations rather than as
        prose that cannot go red.

        A floor BELOW the build duration is not a floor: the entry is already
        older than it the moment the build that filled it finishes, so the
        next poll kicks another and the fold runs forever — the state the
        owner's open tab actually produced. The build was measured at about
        forty seconds once the double fold was removed.

        The unchanged cap belongs BELOW the hard ttl by more than one build,
        or a key declining to rebuild ages out of the STALE regime into the
        BLOCKING one — which is the flap, reached by the option that exists to
        avoid it.
        """
        measured_build_s = 40      # the wall clock of one fold, measured
        # UNCONDITIONAL POSITIVE CONTROL on every observable below: each number
        # exists and is a positive count of seconds. A constant renamed away
        # would otherwise reach the comparisons as an AttributeError rather
        # than as a stated claim, and one set to None would make every
        # inequality a crash nobody reads as this arm's subject.
        self.assertGreater(web_owed._OWED_COLD_WAIT_S, 0)
        self.assertGreater(web_owed._OWED_FLOOR_S, 0)
        self.assertGreater(web_owed._OWED_UNCHANGED_MAX_S, 0)
        self.assertGreater(web_owed._OWED_HARD_TTL_S, 0)
        self.assertGreater(web_owed._OWED_FLOOR_S, 3 * measured_build_s,
                           "the floor is short enough that the fold is "
                           "permanently running")
        self.assertLess(web_owed._OWED_COLD_WAIT_S, web_owed._OWED_FLOOR_S)
        self.assertLess(web_owed._OWED_FLOOR_S,
                        web_owed._OWED_UNCHANGED_MAX_S)
        self.assertLess(web_owed._OWED_UNCHANGED_MAX_S + measured_build_s,
                        web_owed._OWED_HARD_TTL_S,
                        "an unmoved fingerprint can hold the body until the "
                        "hard cap, which puts the next rebuild on the "
                        "BLOCKING path")

    def test_A_REFUSAL_IS_SERVED_AS_A_REFUSAL_never_as_an_empty_backlog(self):
        """THE ONE THING THIS ENDPOINT MAY NEVER DO. A cache that stored an
        unreadable fold as `{"rows": [], "total": 0}` would put "nothing owed"
        on the owner's screen for a whole floor, at the exact moment we lost
        the ability to say."""
        patcher, calls = self._counting_snapshot(
            result=({}, "the ledger did not open"))
        with patcher:
            first = web_owed._api_owed()
            second = web_owed._api_owed()
        self.assertEqual(len(calls), 1)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the refusal is
        # a real body naming a real reason, not an endpoint that answered
        # nothing (which would satisfy every absence claim in the loop).
        self.assertIn("did not open", first.get("why") or "")
        self.assertIn("cured", first)
        self.assertTrue(first.get("unavailable"),
                        "an unreadable ledger was served as %r" % sorted(first))
        self.assertEqual(first.get("rows", []), [])
        self.assertTrue(first["cured"]["unavailable"])
        # AND THE SECOND POLL, WHICH IS SERVED FROM THE CACHE: the refusal is
        # what the entry holds, so the floor cannot turn it into a clean board.
        self.assertTrue(second.get("unavailable"),
                        "an unreadable ledger was served as %r" % sorted(second))
        self.assertIn("did not open", second.get("why") or "")
        self.assertEqual(second.get("rows", []), [])
        self.assertTrue(second["cured"]["unavailable"])
        # AND THE ENTRY ITSELF IS THE REFUSAL — a cache holding a healthy body
        # behind a refusal would serve the healthy one to the next reader.
        entry = web_cache._qstate.get(_OWED_KEY)
        self.assertIsNotNone(entry, "the refusal was never cached at all")
        self.assertTrue(entry[1].get("unavailable"))

    def test_A_BUILD_THAT_RAISES_IS_A_NAMED_STATE_never_an_exception(self):
        """What `_owed_build` returns is what gets CACHED, so it may not
        raise: an escaped exception leaves no entry to replace and the next
        reader pays a whole fold to learn the same thing."""
        from unittest import mock
        from helm import dispatches

        def boom():
            raise RuntimeError("the ledger exploded")

        with mock.patch.object(dispatches, "snapshot", boom):
            body = web_owed._owed_build()
        self.assertTrue(body.get("unavailable"))
        self.assertIn("exploded", body.get("why") or "")
        self.assertTrue(body["cured"]["unavailable"],
                        "the cured bucket rendered a confident zero over a "
                        "fold that never happened")

    def test_THE_AGE_IS_THE_BODYS_OWN_not_the_entrys(self):
        """A restored or long-lived body can be far older than the moment it
        entered this cache, and the card's whole honesty rests on the age
        being the READ's. The two numbers are deliberately different here, so
        an implementation that stamped the cache entry's age would read 100
        where the truth is 900."""
        now = time.time()
        web_cache._qstate[_OWED_KEY] = (now - 100, {
            "unavailable": False, "rows": [], "total": 0,
            "read_ts": now - 900,
            "cured": {"unavailable": False, "rows": [], "total": 0}})
        body = web_owed._api_owed()
        self.assertGreaterEqual(body["read_age_s"], 880)
        self.assertLessEqual(body["read_age_s"], 940)
        # THE CONTROL FOR THE ABSENCE BELOW, on the same field: the cached
        # body DOES carry `read_ts`. Without it, an endpoint that dropped the
        # field everywhere — or a cache holding a body that never had one —
        # would satisfy the assertion by having nothing to remove.
        self.assertIn("read_ts", web_cache._qstate[_OWED_KEY][1])
        self.assertNotIn("read_ts", body,
                         "the raw build instant reaches the browser, where a "
                         "clock comparison can silently misreport it")
        # THE CACHED BODY IS NOT MUTATED BY THE RESPONSE. Stamping in place
        # would put one reader's age into every later reader's body.
        self.assertNotIn("read_age_s", web_cache._qstate[_OWED_KEY][1])

    def test_A_CONFIRMED_READING_AGES_FROM_THE_CONFIRMATION_not_the_build(self):
        """The same split /api/lr makes: a fingerprint match that confirmed the
        ledgers unmoved is a read of them, so `read_age_s` counts from it while
        `projected_age_s` keeps the build's age. The arm above is this one's
        control — no confirmation, and both ages read the build's 900."""
        now = time.time()
        entry_ts = now - 100
        web_cache._qstate[_OWED_KEY] = (entry_ts, {
            "unavailable": False, "rows": [], "total": 0,
            "read_ts": now - 900,
            "cured": {"unavailable": False, "rows": [], "total": 0}})
        web_cache._qverified[_OWED_KEY] = (entry_ts, now - 5)
        body = web_owed._api_owed()
        self.assertLessEqual(body["read_age_s"], 6)
        self.assertGreaterEqual(body["projected_age_s"], 880)
        # a stamp bound to a DIFFERENT entry does not speak for this one
        web_cache._qverified[_OWED_KEY] = (entry_ts - 1, now - 5)
        body = web_owed._api_owed()
        self.assertGreaterEqual(body["read_age_s"], 880)

    def test_A_BODY_THAT_CANNOT_BE_DATED_GETS_NO_AGE_never_a_zero(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the FIRST half of this arm: the same field, through the same call, on a body that CAN be dated, asserted to be an int before the None is claimed
        """A fabricated zero on a freshness line is the worst available lie:
        it says "just read" about a body nothing can date."""
        now = time.time()
        healthy = {"unavailable": False, "rows": [], "total": 0,
                   "cured": {"unavailable": False, "rows": [], "total": 0}}
        # UNCONDITIONAL POSITIVE CONTROL FIRST, on the same field through the
        # same call: a body that CAN be dated gets a number. An endpoint that
        # never stamped an age at all would satisfy the None below.
        web_cache._qstate[_OWED_KEY] = (now - 10,
                                        dict(healthy, read_ts=now - 10))
        dated = web_owed._api_owed()
        self.assertIsInstance(dated["read_age_s"], int)

        web_cache._qstate[_OWED_KEY] = (time.time() - 10, dict(healthy))
        body = web_owed._api_owed()
        self.assertIsNone(body["read_age_s"])

    def test_THE_FINGERPRINT_MOVES_ON_A_CHMOD_and_holds_on_a_still_file(self):
        """NOT THE MTIME MEMO THE LAND CARD KILLED, asserted on the exact
        input that memo could not see: `chmod 000` makes the ledger unreadable
        while moving NEITHER its mtime NOR its size, so an mtime-keyed term
        serves the last healthy board forever over a record nobody can read.

        AND THE STILL CASE IS THE CONTROL. A term that changed on every call
        would pass the chmod half while never holding a body at all.
        """
        import shutil
        import tempfile
        from unittest import mock
        from helm import dispatches

        tmp = tempfile.mkdtemp(prefix="owed-fingerprint-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ledger = os.path.join(tmp, "dispatches.jsonl")
        epoch = os.path.join(tmp, "gate-epoch.json")
        for path in (ledger, epoch):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}\n")

        with mock.patch.object(dispatches, "ledger_path", lambda: ledger), \
                mock.patch.object(dispatches, "epoch_path", lambda: epoch):
            first = web_owed._owed_fingerprint()
            still = web_owed._owed_fingerprint()
            self.assertTrue(first, "the fingerprint named no input at all")
            self.assertEqual(first, still,
                             "an unchanged read-set produces a MOVING term, "
                             "so no body can ever be held")
            os.chmod(ledger, 0o000)
            self.addCleanup(os.chmod, ledger, 0o600)
            chmodded = web_owed._owed_fingerprint()
            self.assertNotEqual(
                first, chmodded,
                "a chmod that makes the ledger unreadable does not move the "
                "term, so the last healthy burn-down would be served forever")
            # A FRESH BASELINE, BECAUSE RESTORING THE MODE MOVES THE LEDGER
            # AGAIN. Comparing the epoch case against `first` measured the
            # LEDGER's second ctime change and called it the epoch marker: a
            # true reading bound to a claim it does not support, and it left
            # a mutation that dropped the epoch path from the term entirely
            # GREEN (measured). The baseline is retaken after every touch to
            # the ledger, so the only thing that moves below is the marker.
            os.chmod(ledger, 0o600)
            before_epoch = web_owed._owed_fingerprint()
            self.assertEqual(before_epoch, web_owed._owed_fingerprint(),
                             "the baseline for the epoch case is itself "
                             "moving, so the comparison below means nothing")
            with open(epoch, "w", encoding="utf-8") as fh:
                fh.write('{"v": 2}\n')
            self.assertNotEqual(
                before_epoch, web_owed._owed_fingerprint(),
                "the gate epoch marker is read on nearly every folded row and "
                "the term cannot see it move")


# THE SLOWEST FRESH-PROCESS FIRST BUILD MEASURED ON THE OWNER'S BOX. Four
# separate processes, each timing `_owed_build` as the FIRST call it ever
# made: 8.85s, 9.81s, 10.24s — plus one 42.09s outlier taken while the box
# carried a load average near 16 on 8 cores. The typical figure is what the
# cold wait is set against; the outlier is what `warming` and the card's
# retry exist for, because no wait worth having holds a card blank for 42s.
# A warm in-process rebuild is FASTER (5.7-9.9s) and is the wrong number to
# size this against: the build a cold read waits for is always a first one.
_MEASURED_COLD_BUILD_S = 10.24


class ColdWaitIsSizedAgainstTheBuildTest(unittest.TestCase):
    """THE DEFECT, measured: `cold_wait` was 5s and the build it waits for
    takes 8.9-10.2s, so the FIRST view of the burn-down after any restart was
    GUARANTEED to answer `{"warming": true}`. The owner paid five seconds to
    be told nothing was ready, every single time, and the body he wanted
    landed in the cache seconds after he stopped looking at it.

    A WAIT SHORTER THAN ITS OWN FILL IS THE TTL TRAP ONE STATE EARLIER — the
    same shape as a 46s /api/ready under a 30s ttl and a 6s roster build under
    a 2.5s one. Both sides of the sandwich are pinned: this arm holds the
    floor, `test_THE_COLD_WAIT_FITS_INSIDE_THE_CARDS_FETCH_DEADLINE` holds the
    ceiling, and between them the number cannot drift back to a guess."""

    def test_the_cold_wait_outlasts_the_build_it_exists_to_wait_for(self):
        self.assertGreater(
            web_owed._OWED_COLD_WAIT_S, _MEASURED_COLD_BUILD_S,
            "the cold wait (%ss) is shorter than the measured cold build "
            "(%ss), so the first view after every restart is a guaranteed "
            "placeholder" % (web_owed._OWED_COLD_WAIT_S,
                             _MEASURED_COLD_BUILD_S))

    def test_the_card_RE_ASKS_after_warming_instead_of_waiting_a_full_poll(self):  # noqa: VACUOUS_ASSERTION — the unconditional must-hit is `assertIn("obdWarming", page)`: the warming renderer is read off the same page text before any retry claim is made
        """THE PROMISE THE PLACEHOLDER MAKES. `obdWarming` tells the owner
        "this card refreshes itself when it is" — and the only thing that
        refreshed it was the 45-SECOND timer, so a build finishing at ten
        seconds sat unread for another thirty-five. The retry is what makes
        that sentence true, and it is bounded so a build that never succeeds
        reaches him as a failure rather than as a spinner."""
        from helm import web_ui_loader
        page = web_ui_loader.read_text()
        # THE MUST-HIT, unconditional and on the same observable: the warming
        # renderer itself must be on this page, or the arm below is searching
        # a page that never had the feature and passing on its absence.
        self.assertIn("obdWarming", page,
                      "the burn-down's warming renderer is not on the "
                      "assembled page at all, so this arm reads nothing")
        self.assertIn("OBD_WARM_TRIES", page,
                      "a warming answer is never re-asked, so the card waits "
                      "a full 45s poll for a body that already landed")
        self.assertIn("d.warming && OBD_WARM_TRIES", page,
                      "the retry does not key off the warming answer")
