#!/usr/bin/env python3
"""Stale claim visibility and release — defect #115 fix.

A claimed room (worktree) was a mutex only its holder could see or release.
Other seats could not even probe whether the holder was dead. The fix:

  1. `seats.claim_holder_liveness()` probes whether a claim holder is
     demonstrably alive/dead/unknown by checking recorded sessions against
     /proc and the claude process census.

  2. `seats.release_stale()` releases a claim whose holder is provably
     dead — the safety is in the liveness proof, not a token a dead
     process can never produce.

  3. `claims_list()` now carries a `stale` field so every seat sees a
     dead holder's claim on every surface (the roster footer, `helm chat
     claims`, the web ledger).

  4. `helm work list` prints a STALE claims section naming the dead
     holder and the release command (`helm work release <lane> --stale`).

  5. `helm work release <lane> --stale` releases a dead holder's claim,
     unlocks the worktree, and leaves room/branch cleanup for `helm work gc`.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import pk, seats, web_ui_loader, work  # noqa: E402

ENV_KEYS = ("HELM_PROC",
            "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR",
            "HELM_PRIVATE_NEEDLES", "HELM_SCRATCH_GC")

ME = "alice"
THEM = "bob"


class StaleBase(unittest.TestCase):
    """Hermetic: tmp HELM_HOME + HELM_CHAT_DIR, scratch repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-stale-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        # HELM_HOME ALONE DOES NOT ISOLATE THE STORE. The adopted root is
        # HOME-derived by design (home.claude_memory_dir_for), so a test
        # that only sets HELM_HOME still reads and WRITES the operator's
        # real memory — store/__init__.py names HELM_ADOPTED_DIR as the
        # override in the same paragraph. Nothing in this file touches the
        # store today; this is set so that the first test that does cannot
        # quietly pollute it (fleet-wide correction, 2026-08-04).
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # DETERMINISTIC /proc FOR THE WHOLE CLASS. The secondary scan searches
        # every same-uid process's cmdline and environ for the fixture sid, so
        # against the REAL /proc any probe command that ever mentioned it —
        # including one typed in this same session minutes earlier — answers
        # "live" and the test measures the box. Hit three times tonight before
        # I pinned it here. A test that needs the real table sets HELM_PROC
        # back explicitly.
        os.makedirs(os.path.join(self.tmp, "proc"), exist_ok=True)
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(
            self.tmp, "no-needles-configured.txt")
        os.environ["HELM_CHAT_NAME"] = ME
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self._sh(*cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        self._sh("git", "add", "-A")
        r = self._sh("git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sh(self, *args):
        return subprocess.run(list(args), cwd=self.root, capture_output=True,
                            text=True, timeout=30)

    def work(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def claim_lane(self, lane, seat=ME, ttl=None):
        args = ["claim", lane, "--seat", seat]
        if ttl is not None:
            args += ["--ttl", str(ttl)]
        rc, out, err = self.work(*args)
        self.assertEqual(rc, 0, err)
        return out.strip().split("\t")[2]

    def stored_claim(self, resource):
        """The raw claim row from .claims.json, after sweep."""
        from helm import pk
        raw = pk.read_json(seats.claims_path(), {}) or {}
        return {r: v for r, v in raw.items()
                if r == "_fence" or (isinstance(v, dict)
                                     and v.get("exp_mono", 0) > seats._now_mono())
                }.get(resource)


class ClaimHolderLivenessTest(StaleBase):
    """Unit tests for claim_holder_liveness — the probe behind stale detection."""

    def test_no_session_returns_unknown(self):
        liveness, reason = seats.claim_holder_liveness("no-one")
        self.assertEqual(liveness, "unknown")
        self.assertIn("no session", reason)

    def test_known_dead_session_returns_stale(self):
        """A UUID session that no process carries is dead."""
        sid = "deadbeef-0000-0000-0000-000000000000"
        liveness, reason = seats.claim_holder_liveness("someone", session=sid)
        self.assertEqual(liveness, "stale")
        self.assertIn(sid[:8], reason)

    def test_live_session_returns_live(self):
        """A session known to live_sids is live."""
        from helm import session as sess_mod
        with mock.patch.object(sess_mod, "live_sids",
                               return_value={"11111111-0000-0000-0000-000000000000":
                                             [12345]}) as _m:
            liveness, reason = seats.claim_holder_liveness(
                "someone", session="11111111-0000-0000-0000-000000000000")
        self.assertEqual(liveness, "live")

    def test_unreadable_proc_returns_unknown_on_no_session(self):
        with mock.patch("os.listdir", side_effect=OSError("boom")):
            liveness, reason = seats.claim_holder_liveness("someone")
        self.assertEqual(liveness, "unknown")

    def test_the_three_return_values_are_live_stale_unknown(self):
        """The three-state contract: live, stale, unknown."""
        for holder in ("alice", "bob", "carol"):
            liveness, _reason = seats.claim_holder_liveness(holder)
            self.assertIn(liveness, ("live", "stale", "unknown"))


class StaleReleaseTest(StaleBase):
    """Integration tests for claim_holder_liveness + release_stale."""

    def _dead_session(self):
        """A UUID that is guaranteed not to be in any live process."""
        return "00000000-0000-0000-0000-000000000000"

    def test_release_stale_on_no_claim_refuses_cleanly(self):
        ok, msg = seats.release_stale("worktree:proj:no-such", ME)
        self.assertFalse(ok)
        self.assertIn("not claimed", msg)

    def test_release_stale_on_live_holder_refuses(self):
        """A claim with a live session must never be releasable via --stale."""
        sid = "11111111-0000-0000-0000-000000000000"
        from helm import session as sess_mod
        with mock.patch.object(sess_mod, "live_sids",
                               return_value={sid: [99999]}):
            seats.claim("worktree:proj:live-test", THEM, ttl=600,
                       session=sid)
            ok, msg = seats.release_stale("worktree:proj:live-test", ME)
            self.assertFalse(ok)
            self.assertIn("live", msg)
            self.assertIn("stale release refused", msg)
            # The claim must survive the refused release
            self.assertIsNotNone(self.stored_claim("worktree:proj:live-test"))

    def test_release_stale_on_unknown_holder_refuses(self):
        """Unknown liveness must never authorize release — uncertain proof
        must never destroy a live holder's claim."""
        seats.claim("worktree:proj:unknown-test", THEM, ttl=600)
        # No session recorded -> unknown
        ok, msg = seats.release_stale("worktree:proj:unknown-test", ME)
        self.assertFalse(ok)
        self.assertIn("unknown", msg)
        self.assertIsNotNone(self.stored_claim("worktree:proj:unknown-test"))

    def test_release_stale_on_dead_holder_succeeds(self):
        """The happy path: a dead session, claim released."""
        seats.claim("worktree:proj:dead-test", THEM, ttl=600,
                   session=self._dead_session())
        ok, msg = seats.release_stale("worktree:proj:dead-test", ME)
        self.assertTrue(ok, msg)
        self.assertIn("released", msg)
        self.assertIn("stale", msg)
        self.assertIsNone(self.stored_claim("worktree:proj:dead-test"))

    def test_release_stale_ignores_token(self):
        """A stale release must NOT require a lease token — the holder is dead
        and cannot produce one. That is the whole point."""
        seats.claim("worktree:proj:dead-token", THEM, ttl=600,
                   session=self._dead_session())
        # We DELIBERATELY don't use any lease token
        ok, msg = seats.release_stale("worktree:proj:dead-token", ME)
        self.assertTrue(ok, msg)
        self.assertIsNone(self.stored_claim("worktree:proj:dead-token"))


class ClaimsListStaleFieldTest(StaleBase):
    """The `stale` field in claims_list — surfaces dead-holder visibility."""

    def _dead_session(self):
        return "00000000-0000-0000-0000-000000000000"

    def test_live_claim_is_not_stale(self):
        seats.claim("worktree:proj:alive", ME, ttl=600)
        rows = seats.claims_list()
        alice = [r for r in rows if r["resource"] == "worktree:proj:alive"]
        self.assertEqual(len(alice), 1)
        self.assertFalse(alice[0]["stale"])

    def test_dead_session_claim_is_stale(self):
        seats.claim("worktree:proj:dead", THEM, ttl=600,
                   session=self._dead_session())
        rows = seats.claims_list()
        dead = [r for r in rows if r["resource"] == "worktree:proj:dead"]
        self.assertEqual(len(dead), 1)
        self.assertTrue(dead[0]["stale"])

    def test_stale_field_is_present_on_all_rows(self):
        seats.claim("worktree:proj:a", ME, ttl=600)
        seats.claim("worktree:proj:b", THEM, ttl=600,
                   session=self._dead_session())
        for row in seats.claims_list():
            self.assertIsInstance(row.get("stale"), bool)

    def test_json_claims_includes_stale(self):
        seats.claim("worktree:proj:dead-json", THEM, ttl=600,
                   session=self._dead_session())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats._cmd_claims(["--json"])
        rows = json.loads(out.getvalue())
        dead = [r for r in rows if r["resource"] == "worktree:proj:dead-json"]
        self.assertEqual(len(dead), 1)
        self.assertTrue(dead[0]["stale"])

    def test_human_claims_table_still_works(self):
        seats.claim("worktree:proj:claims-test", ME, ttl=600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats._cmd_claims([])
        self.assertEqual(rc, 0)
        self.assertIn("worktree:proj:claims-test", out.getvalue())


class WorkListStaleDisplayTest(StaleBase):
    """`helm work list` surfaces stale claims for ALL seats to see."""

    def _dead_session(self):
        return "00000000-0000-0000-0000-000000000000"

    def test_list_shows_stale_section_when_a_claim_is_stale(self):
        self.claim_lane("alive", seat=ME)
        # Bob claims, then "dies" — we simulate by using a dead session
        seats.claim(work.resource(self.root, "dead-lane"), THEM, ttl=600,
                   session=self._dead_session())
        # Create the worktree directly since claim() from bob's seat won't
        # create it in this fixture (bob isn't the ambient seat)
        path = work.lane_path(self.root, "dead-lane")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-lane"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("STALE claims", out)
        self.assertIn("dead-lane", out)
        self.assertIn(THEM, out)

    def test_list_prints_the_release_command(self):
        seats.claim(work.resource(self.root, "dead-lane"), THEM, ttl=600,
                   session=self._dead_session())
        path = work.lane_path(self.root, "dead-lane")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-lane"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("--stale", out)
        self.assertIn("dead-lane --stale", out)

    def test_list_board_includes_stale_field(self):
        seats.claim(work.resource(self.root, "dead-lane"), THEM, ttl=600,
                   session=self._dead_session())
        path = work.lane_path(self.root, "dead-lane")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-lane"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        rows = work.list_rows(self.root)
        dead_row = [r for r in rows if r["lane"] == "dead-lane"]
        self.assertEqual(len(dead_row), 1)
        self.assertTrue(dead_row[0]["stale"])

    def test_no_stale_claims_means_no_stale_section(self):
        self.claim_lane("alive", seat=ME)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("STALE claims", out)


class WorkReleaseStaleTest(StaleBase):
    """`helm work release <lane> --stale` end-to-end."""

    def _dead_session(self):
        return "00000000-0000-0000-0000-000000000000"

    def test_stale_flag_releases_claim_and_unlocks_worktree(self):
        seats.claim(work.resource(self.root, "dead-room"), THEM, ttl=600,
                   session=self._dead_session())
        path = work.lane_path(self.root, "dead-room")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-room"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        rc, out, err = self.work("release", "dead-room", "--stale")
        self.assertEqual(rc, 0, err)
        self.assertIn("released", out + err)
        self.assertIn("stale", out + err)
        self.assertIn("unlocked", out + err)
        self.assertIsNone(self.stored_claim(work.resource(self.root, "dead-room")))

    def test_stale_flag_refuses_live_holder(self):
        sid = "11111111-0000-0000-0000-000000000000"
        from helm import session as sess_mod
        with mock.patch.object(sess_mod, "live_sids",
                               return_value={sid: [99999]}):
            seats.claim(work.resource(self.root, "alive-room"), THEM, ttl=600,
                       session=sid)
            path = work.lane_path(self.root, "alive-room")
            subprocess.run(["git", "worktree", "add", "-q", "-b",
                            work.lane_branch("alive-room"), path, "main"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
            rc, out, err = self.work("release", "alive-room", "--stale")
            self.assertEqual(rc, 1)
            self.assertIn("live", err)
            self.assertIn("stale release refused", err)

    def test_stale_flag_with_no_lane_shows_usage(self):
        rc, out, err = self.work("release", "--stale")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_stale_release_does_not_remove_room(self):
        """Stale release only surrenders the claim and unlocks. Room/branch
        cleanup is left for `helm work gc`."""
        seats.claim(work.resource(self.root, "dead-room"), THEM, ttl=600,
                   session=self._dead_session())
        path = work.lane_path(self.root, "dead-room")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-room"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        rc, out, err = self.work("release", "dead-room", "--stale")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path),
                       "room must survive stale release (gc handles cleanup)")

    def test_other_seat_can_claim_after_stale_release(self):
        """After stale release, a different seat can claim the freed lane."""
        seats.claim(work.resource(self.root, "dead-room"), THEM, ttl=600,
                   session=self._dead_session())
        path = work.lane_path(self.root, "dead-room")
        subprocess.run(["git", "worktree", "add", "-q", "-b",
                        work.lane_branch("dead-room"), path, "main"],
                       cwd=self.root, capture_output=True, text=True,
                       timeout=30,
                       env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.work("release", "dead-room", "--stale")
        # Now Alice (ME) claims the freed lane
        rc, out, err = self.work("claim", "dead-room", "--seat", ME)
        self.assertEqual(rc, 0, err)


class FramingHonestTest(StaleBase):
    """The stale release surface never claims to be a security mechanism."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with open(os.path.join(self.REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def test_stale_release_doc_names_the_liveness_safety(self):
        doc = seats.release_stale.__doc__
        self.assertIn("demonstrably dead", doc)
        self.assertIn("never on", doc)
        self.assertIn("live", doc)

    def test_claim_holder_liveness_doc_includes_the_three_states(self):
        doc = seats.claim_holder_liveness.__doc__
        self.assertIn("live", doc)
        self.assertIn("stale", doc)
        self.assertIn("unknown", doc)
        self.assertIn("actionable negative", doc)

    def test_claims_list_stale_field_is_boolean(self):
        for row in seats.claims_list():
            self.assertIsInstance(row["stale"], bool)


class StaleIsRENDEREDNotOnlyComputed(StaleBase):
    """The claim row a human reads must SAY stale, not merely carry a flag.

    This lane's own headline is "surface stale claims for all seats", and the
    verdict landed in `claims_list()` where no renderer read it: `helm chat
    claims` and the roster's `claim:` lines printed holder, remaining and fence
    exactly as before, so a DEAD holder's lock was still indistinguishable from
    a working seat's. The existing coverage asserted `row["stale"]` is a bool —
    true, and about the data rather than the surface. A computed-and-discarded
    verdict is the same defect the fleet keeps finding one layer down: the tool
    knows and does not say."""

    def _dead_claim(self, lane="dead-lane"):
        """A claim whose holder session is provably not running."""
        sid = "00000000-0000-0000-0000-000000000000"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw
                   if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key], session=sid)
        pk.write_json(seats.claims_path(), raw)
        return key

    def _render(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats._cmd_claims(list(args))
        return out.getvalue()

    def test_a_dead_holders_claim_row_SAYS_stale_and_names_the_remedy(self):
        key = self._dead_claim()
        rows = {c["resource"]: c for c in seats.claims_list()}
        self.assertTrue(rows[key]["stale"], "fixture did not produce a stale "
                                            "claim; nothing below is tested")
        text = self._render()
        self.assertIn(key, text)                      # the row is rendered
        self.assertIn("STALE", text)                  # and it SAYS so
        self.assertIn("--stale", text)                # remedy rides with it

    def test_the_ROSTER_says_it_too_not_only_the_claims_verb(self):
        """`helm chat seats` is the surface the fleet actually reads, and it
        renders claim rows through a SECOND code path. Mutation caught this:
        stripping the marker from `claims` failed a test, stripping it from the
        roster failed NOTHING — I had added it to two renderers and covered
        one, which is the same computed-but-not-surfaced gap this class exists
        to close, one layer up and in my own work."""
        seats.write_roster(ME)          # the roster prints nothing with no seats
        key = self._dead_claim("dead-roster-lane")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats.cmd("seats", [])
        text = out.getvalue()
        self.assertIn(key, text)                      # the row is rendered...
        self.assertIn("STALE", text)                  # ...and it SAYS so
        self.assertIn("--stale", text)

    def test_a_LIVE_holders_claim_row_says_nothing_of_the_kind(self):
        """The control that makes the assertion above mean something: an
        unconditional marker would satisfy it while telling every reader their
        working seat is dead."""
        self.claim_lane("live-lane")
        rows = {c["resource"]: c for c in seats.claims_list()}
        live = [r for r, c in rows.items() if not c["stale"]]
        self.assertTrue(live, "no live claim to contrast against")
        text = self._render()
        self.assertIn(live[0], text)                  # rendered...
        self.assertNotIn("STALE", text)               # ...and unmarked


class UnreadableCensusRefusesRelease(StaleBase):
    """An unreadable session census must answer UNKNOWN, never STALE.

    @codex-2 on review, with a deterministic probe: `live_sids()` raising was
    caught into `live = {}`, so "I could not look" and "I looked and it is
    absent" became the same value — and the absence then fell through to
    "stale", which AUTHORIZES another seat to release a LIVE holder's claim.
    The docstring on claim_holder_liveness already said unreadable evidence is
    unknown; only the code disagreed. This is the third state exiting a
    predicate and landing in the actionable bucket, on a safety path."""

    def _unreadable_proc(self):
        """The REAL failure contract, not a mocked raise.

        `live_sids()` does NOT raise when /proc cannot be enumerated:
        `_proc_claude_rows` catches OSError and answers an empty list, so the
        failure reaches every caller as {} — indistinguishable from "nothing
        is running". A test that mocks live_sids to RAISE proves a shape
        production deliberately suppresses, which is precisely how the first
        fix passed its own tests while the safety path stayed open
        (@codex-2, second review round). Pointing session.PROC at a path that
        cannot be listed reproduces what actually happens."""
        from helm import session as sess_mod
        import contextlib as _ctx

        @_ctx.contextmanager
        def _both():
            # TWO KNOBS, because two different scans read /proc under two
            # different names: session.PROC drives the census, and HELM_PROC
            # drives `_sessions_with_a_process`. Isolating one leaves the other
            # reading the REAL /proc, where any process that ever carried this
            # sid on its cmdline or environ — including a probe script from
            # earlier in the same session — answers "live" and the test measures
            # the box instead of the code.
            empty = os.path.join(self.tmp, "empty-proc")
            os.makedirs(empty, exist_ok=True)
            prior = os.environ.get("HELM_PROC")
            os.environ["HELM_PROC"] = empty
            try:
                with mock.patch.object(sess_mod, "PROC",
                                       os.path.join(self.tmp, "no-such-proc")):
                    yield
            finally:
                if prior is None:
                    os.environ.pop("HELM_PROC", None)
                else:
                    os.environ["HELM_PROC"] = prior
        return _both()

    def test_an_unreadable_census_is_unknown_not_stale(self):
        sid = "00000000-0000-0000-0000-000000000000"
        with self._unreadable_proc():
            liveness, reason = seats.claim_holder_liveness("someone",
                                                           session=sid)
        self.assertEqual(liveness, "unknown", reason)
        self.assertIn("UNREADABLE", reason)
        # POSITIVE CONTROL on the same call, unconditional: with the census
        # READABLE and the same dead sid, the very same probe says stale — so
        # the "unknown" above is the unreadable census and not a probe that
        # answers unknown for everything.
        liveness, _reason = seats.claim_holder_liveness("someone", session=sid)
        self.assertEqual(liveness, "stale")

    def test_an_unreadable_census_REFUSES_the_release(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: with the census READABLE the same release goes through and the same claim disappears, so the survival above measured the refusal. Each stored_claim() call is a fresh producer by the rung's own provenance rule, so no control it can credit exists for a before/after on one row.
        """The consequence, asserted at the verb rather than the probe: this
        is the arm that would have let a live seat lose its lock."""
        lane = "guarded-lane"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key],
                        session="00000000-0000-0000-0000-000000000000")
        pk.write_json(seats.claims_path(), raw)
        with self._unreadable_proc():
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertFalse(ok, msg)
        self.assertIsNotNone(self.stored_claim(key))   # the claim SURVIVES
        # POSITIVE CONTROL: readable census, same dead sid -> the release goes
        # through, so the refusal above measured the unreadable census.
        ok, msg = seats.release_stale(key, "another-seat")
        self.assertTrue(ok, msg)
        self.assertIsNone(self.stored_claim(key))


class SharedStatusLineSaysStale(StaleBase):
    """The seat DOING-line is the widest claim surface there is.

    It reaches bare `helm chat status`, the roster/presence JSON, the web
    doing-cells and the dashboard tooltips — and it rendered a dead holder's
    lock as "working lane/X". @codex-2 found it AFTER I had fixed the two
    narrower surfaces and called the class closed, which is the same
    computed-but-not-surfaced shape one more time: the field was on the claim
    and this renderer did not read it."""

    def test_a_dead_holders_doing_line_does_not_say_working(self):
        seats.write_roster(ME, home_room="main")
        lane = "dead-doing-lane"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key],
                        session="00000000-0000-0000-0000-000000000000")
        pk.write_json(seats.claims_path(), raw)
        rep = seats.roster_report("main")
        rows = [s for s in rep["seats"] if s.get("seat") == ME]
        self.assertTrue(rows, "the seat is not on the roster; nothing tested")
        line, source = str(rows[0].get("line") or ""), rows[0].get("source")
        self.assertEqual(source, "claim",
                         "the row is not rendering the CLAIM tier, so this "
                         "asserts nothing about a claim: %r" % line)
        self.assertIn(lane, line)           # it IS rendering this claim...
        self.assertNotIn("working", line)   # ...and does not call it work
        self.assertIn("STALE", line)


class WebFooterRendersStale(unittest.TestCase):
    """The dashboard footer is the OWNER's surface and it dropped the field.

    Python cannot execute the JS, so this pins the SOURCE the way
    test_web.py's JS-classifier check does: the roster JSON already carries
    `stale` per claim, and the footer that renders those claims must read it.
    @codex-2 named this one on review — the fourth renderer of the same field,
    after the CLI claims verb, the roster claim lines and the seat doing-line.

    A source pin is weaker than an executed test and is written down as such.
    What it CAN do is fail the day someone rewrites the footer without the
    field, which is exactly how the first three surfaces lost it."""

    def test_the_claim_footer_reads_the_stale_field(self):
        src = web_ui_loader.read_text()
        # POSITIVE CONTROL, unconditional: the footer we mean is in this file.
        self.assertIn('$("#rosterclaims").innerHTML', src)
        i = src.index('$("#rosterclaims").innerHTML')
        footer = src[i:i + 600]
        self.assertIn("c.liveness", footer,
                      "the roster claim footer renders holder/remaining and "
                      "ignores claims[].liveness, so the owner's own dashboard "
                      "shows a dead or unprovable hold exactly like a live one")
        self.assertIn("STALE", footer)
        # THE THIRD STATE TOO. Pinning only STALE is what let `unknown` render
        # as healthy on every surface after `stale` was surfaced — the same
        # collapse one value over, which is the whole meld.
        self.assertIn("UNKNOWN", footer)


class RowLevelUnknownRefusesRelease(StaleBase):
    """Per-ROW uncertainty, not just global completeness.

    @codex-2, third round. `live_sids` answers a DIFFERENT question and its
    omissions have the opposite safety polarity: it deliberately drops a row
    whose identity is unproven, because for the double-open check an unproven
    identity must not manufacture a false POSITIVE. For a destructive absence
    the same omission is a false NEGATIVE — and a false negative deletes a live
    seat's claim. `open_pids` is the primitive built for this direction ("live
    pids holding, OR CONSERVATIVELY CAPABLE OF HOLDING, sid ... so law 1 fails
    closed").

    These craft the census RETURN, which is the real contract — the function
    answers a dict with these keys, it does not raise. Round two's tests mocked
    an exception production never throws; that mistake is not repeated here."""

    SID = "00000000-0000-0000-0000-000000000000"

    def _census(self, rows, **flags):
        base = {"rows": rows, "listing_failed": False,
                "who_failed": False, "census_partial": False}
        base.update(flags)
        from helm import session as sess_mod
        return mock.patch.object(sess_mod, "_proc_claude_census",
                                 return_value=base)

    def _row(self, **over):
        # A ROW THE REAL CENSUS WOULD PRODUCE. Measured 2026-08-04: all 12 live
        # rows on this box carry identity="declared", a trusted root and no
        # uncertainty marker. A fixture missing those is not a lean row, it is
        # an UNATTRIBUTED one — which the contract says can never exclude a
        # holder, so every verdict built on it is "unknown" and the arms below
        # would be testing the fixture rather than the code.
        row = {"pid": 12345, "session": "", "resume": "",
               "identity": "declared", "root": "/trusted/config/root",
               "who_context_mismatch": False,
               "possible_sessions": [], "probe_failed": False,
               "who_probe_failed": False, "child": False}
        row.update(over)
        return row

    def _empty_proc(self):
        empty = os.path.join(self.tmp, "empty-proc-rows")
        os.makedirs(empty, exist_ok=True)
        return mock.patch.dict(os.environ, {"HELM_PROC": empty})

    def test_a_MERELY_POSSIBLE_holder_is_unknown_not_stale(self):
        rows = [self._row(possible_sessions=[self.SID])]
        with self._empty_proc(), self._census(rows):
            liveness, reason = seats.claim_holder_liveness("someone",
                                                           session=self.SID)
        self.assertEqual(liveness, "unknown", reason)
        self.assertIn("CAPABLE", reason)
        # POSITIVE CONTROL on the same call: the SAME complete census with the
        # candidate removed answers stale, so "unknown" above is the candidate
        # row and not a probe that refuses everything.
        with self._empty_proc(), self._census([self._row()]):
            liveness, _reason = seats.claim_holder_liveness("someone",
                                                            session=self.SID)
        self.assertEqual(liveness, "stale")

    def test_a_probe_failed_ROW_is_unknown_not_stale(self):
        for gap in ("probe_failed", "who_probe_failed"):
            with self._empty_proc(), self._census([self._row(**{gap: True})]):
                liveness, reason = seats.claim_holder_liveness(
                    "someone", session=self.SID)
            self.assertEqual(liveness, "unknown", "%s: %s" % (gap, reason))
        # POSITIVE CONTROL: with neither gap set, the same shape is stale.
        with self._empty_proc(), self._census([self._row()]):
            liveness, _reason = seats.claim_holder_liveness("someone",
                                                            session=self.SID)
        self.assertEqual(liveness, "stale")

    def test_a_possible_holder_REFUSES_the_release(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: with the candidate row removed the SAME release goes through and the SAME claim disappears
        lane = "candidate-lane"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key], session=self.SID)
        pk.write_json(seats.claims_path(), raw)
        rows = [self._row(possible_sessions=[self.SID])]
        with self._empty_proc(), self._census(rows):
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertFalse(ok, msg)
        self.assertIsNotNone(self.stored_claim(key))    # the claim SURVIVES
        with self._empty_proc(), self._census([self._row()]):
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertTrue(ok, msg)
        self.assertIsNone(self.stored_claim(key))


class UnreadableProcessScanRefusesRelease(StaleBase):
    """The SECONDARY process scan can fail too, and failing is not absence.

    @codex-2, fourth round. `_sessions_with_a_process` does a bare os.listdir
    on HELM_PROC. A missing or unreadable root raised FileNotFoundError out of
    a function whose entire contract is three states — so claims_list, the
    roster, the web read and release_stale all crashed rather than answering
    unknown.

    THE HELPER THAT COULD NOT CATCH THIS IS THE POINT. My earlier isolation
    pointed HELM_PROC at an EXISTING EMPTY DIRECTORY, which proves READABLE
    ABSENCE — the scan ran and found nothing. That is a different fact from the
    scan never running, and only the second one is dangerous. A fixture that
    can only express the safe case cannot test the unsafe one."""

    SID = "00000000-0000-0000-0000-000000000000"

    def _missing_proc(self):
        """A root that does NOT exist — os.listdir raises, it does not
        answer empty."""
        gone = os.path.join(self.tmp, "definitely-no-such-proc-root")
        self.assertFalse(os.path.exists(gone))   # the fixture's own control
        return mock.patch.dict(os.environ, {"HELM_PROC": gone})

    def _complete_empty_census(self):
        from helm import session as sess_mod
        return mock.patch.object(
            sess_mod, "_proc_claude_census",
            return_value={"rows": [], "listing_failed": False,
                          "who_failed": False, "census_partial": False})

    def test_an_unreadable_process_scan_is_unknown_not_a_traceback(self):
        with self._complete_empty_census(), self._missing_proc():
            liveness, reason = seats.claim_holder_liveness("someone",
                                                           session=self.SID)
        self.assertEqual(liveness, "unknown", reason)
        self.assertIn("could not be READ", reason)
        # POSITIVE CONTROL on the same call: with a READABLE (empty) root and
        # the same complete-empty census, the very same probe says stale — so
        # "unknown" above is the unreadable scan, not a blanket refusal.
        readable = os.path.join(self.tmp, "readable-empty-proc")
        os.makedirs(readable, exist_ok=True)
        with self._complete_empty_census(), \
                mock.patch.dict(os.environ, {"HELM_PROC": readable}):
            liveness, _reason = seats.claim_holder_liveness("someone",
                                                            session=self.SID)
        self.assertEqual(liveness, "stale")

    def test_an_unreadable_process_scan_REFUSES_the_release(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: with a READABLE root the same release goes through and the same claim disappears
        lane = "unreadable-scan-lane"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key], session=self.SID)
        pk.write_json(seats.claims_path(), raw)
        with self._complete_empty_census(), self._missing_proc():
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertFalse(ok, msg)
        self.assertIsNotNone(self.stored_claim(key))     # the claim SURVIVES
        readable = os.path.join(self.tmp, "readable-empty-proc-2")
        os.makedirs(readable, exist_ok=True)
        with self._complete_empty_census(), \
                mock.patch.dict(os.environ, {"HELM_PROC": readable}):
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertTrue(ok, msg)
        self.assertIsNone(self.stored_claim(key))

    def test_the_SURFACES_survive_an_unreadable_scan(self):
        """A SMOKE TEST, and it does NOT discriminate this fix — said out loud
        because I nearly shipped it as if it did.

        I wrote it believing it covered the crash reaching claims_list and the
        roster. Mutation says otherwise: with the guard REMOVED this arm still
        passes, because both surfaces are fail-open and swallow the exception
        per row. So it pins that these entry points stay callable, which is
        worth something, and it proves NOTHING about the unreadable-scan
        contract — the two arms above are what do that.

        Left in with an honest label rather than deleted, because a smoke test
        that says what it is beats one that reads like coverage. Fifth time
        tonight I authored a check whose set excluded its own case, and this
        one was inside the commit fixing that very class."""
        seats.write_roster(ME, home_room="main")
        self.claim_lane("surface-lane")
        with self._complete_empty_census(), self._missing_proc():
            rows = seats.claims_list()          # used to raise
            rep = seats.roster_report("main")   # used to raise
        self.assertTrue(rows, "no claim rendered; nothing was tested")
        self.assertTrue(rep.get("seats"))


class OneSnapshotClassifiesTheWholeTable(StaleBase):
    """@codex-2 meld matrix, banked mutant 7: per-row census call count and the
    contradictory same-session snapshot.

    `claims_list` calls the classifier once per row. When the classifier read
    the census ITSELF, N claims meant N /proc walks — and worse than the cost,
    two rows could be classified from DIFFERENT readings, so the same session
    could render live on one line and stale on the next. A verdict that depends
    on where in the loop you were reached is not a verdict."""

    def test_the_census_is_read_ONCE_for_the_whole_table(self):
        from helm import session as sess_mod
        for lane in ("a-lane", "b-lane", "c-lane"):
            self.claim_lane(lane)
        # THE CLAIMS MUST CARRY SESSIONS OR THE CLASSIFIER RETURNS BEFORE IT
        # EVER READS A CENSUS, and the count below is 1 because nothing looked
        # — not because one snapshot served three rows. Mutation caught that:
        # removing the snapshot pass-through left this test green.
        raw = pk.read_json(seats.claims_path(), {}) or {}
        for r, v in list(raw.items()):
            if isinstance(v, dict) and r != "_fence":
                raw[r] = dict(v, session="0000000%d-0000-0000-0000-000000000000"
                              % (len(r) % 10))
        pk.write_json(seats.claims_path(), raw)
        real = sess_mod._proc_claude_census
        calls = []

        def counted():
            calls.append(1)
            return real()

        with mock.patch.object(sess_mod, "_proc_claude_census",
                               side_effect=counted):
            rows = seats.claims_list()
        self.assertGreaterEqual(len(rows), 3, "fewer claims than planted; the "
                                              "count below would prove nothing")
        # AND THEY REACHED THE CENSUS PATH: a row with no session is classified
        # without ever reading one, so a count of 1 over sessionless rows means
        # nothing looked rather than one snapshot served all.
        self.assertGreaterEqual(len(calls), 1,
                                "no census was read at all — these rows never "
                                "exercised the snapshot path")
        self.assertEqual(len(calls), 1,
                         "the census was read %d times for %d claims — per-row "
                         "probing is back, and with it the chance that two rows "
                         "disagree about one session"
                         % (len(calls), len(rows)))

    def test_two_claims_on_ONE_session_cannot_disagree(self):
        sid = "00000000-0000-0000-0000-000000000000"
        for lane in ("twin-one", "twin-two"):
            self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        for r, v in list(raw.items()):
            if isinstance(v, dict) and "twin-" in r:
                raw[r] = dict(v, session=sid)
        pk.write_json(seats.claims_path(), raw)
        rows = [c for c in seats.claims_list() if "twin-" in c["resource"]]
        self.assertEqual(len(rows), 2, "both twins must be present")
        self.assertEqual(len({c["liveness"] for c in rows}), 1,
                         "two claims on ONE session got different verdicts in "
                         "the same render: %r" % [c["liveness"] for c in rows])


class UnknownIsRENDEREDDistinctly(StaleBase):
    """@codex-2 banked mutant 6: renderer ignores state.

    Surfacing STALE while leaving UNKNOWN blank is the SAME collapse one value
    over — an unclassifiable claim reads exactly like a healthy one. Every
    human surface must say so, and none may offer `--stale` for a state the
    verb itself refuses."""

    def _unknown_claim(self, lane="unknowable-lane"):
        """A claim whose liveness cannot be proven either way: a session that
        is neither in the census nor provably absent."""
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key],
                        session="00000000-0000-0000-0000-000000000000")
        pk.write_json(seats.claims_path(), raw)
        gone = os.path.join(self.tmp, "no-such-proc-for-unknown")
        self.assertFalse(os.path.exists(gone))
        return key, mock.patch.dict(os.environ, {"HELM_PROC": gone})

    def test_the_claims_verb_says_UNKNOWN_and_offers_no_remedy(self):
        key, unreadable = self._unknown_claim()
        out, err = io.StringIO(), io.StringIO()
        with unreadable, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            seats._cmd_claims([])
        text = out.getvalue()
        self.assertIn(key, text)              # the row IS rendered...
        self.assertIn("UNKNOWN", text)        # ...and named unknown
        self.assertNotIn("--stale", text)     # and NOT offered a release
        self.assertNotIn("STALE —", text)

    def test_the_doing_line_does_not_call_an_unknown_hold_working(self):
        seats.write_roster(ME, home_room="main")
        key, unreadable = self._unknown_claim("unknown-doing-lane")
        with unreadable:
            rep = seats.roster_report("main")
        rows = [s for s in rep["seats"] if s.get("seat") == ME]
        self.assertTrue(rows, "the seat is not on the roster; nothing tested")
        line = str(rows[0].get("line") or "")
        self.assertEqual(rows[0].get("source"), "claim", line)
        self.assertIn("UNVERIFIED", line)
        self.assertNotIn("working", line)


class ProducerShapesCannotCertifyDeath(StaleBase):
    """@codex-2's meld FIX: three PRODUCER shapes that still turned an unknown
    into a definite STALE. The meld fixed the consumers; these are where the
    data is MADE."""

    SID = "00000000-0000-0000-0000-000000000000"

    def _census(self, value):
        from helm import session as sess_mod
        return mock.patch.object(sess_mod, "_proc_claude_census",
                                 return_value=value)

    def _good_row(self, **over):
        row = {"pid": 1, "session": "", "resume": "", "identity": "declared",
               "root": "/trusted", "who_context_mismatch": False,
               "possible_sessions": [], "probe_failed": False,
               "who_probe_failed": False, "child": False}
        row.update(over)
        return row

    def _complete(self, rows):
        return {"rows": rows, "listing_failed": False,
                "who_failed": False, "census_partial": False}

    def test_A_a_malformed_census_cannot_certify_an_empty_estate(self):
        """`or {}` laundered a missing or shapeless result into a COMPLETE
        negative: no rows, no failure flags, therefore "nothing is running",
        therefore stale."""
        for bogus in ({}, None, {"rows": []}, {"rows": [], "who_failed": False},
                      "not-a-census"):
            with self._census(bogus):
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", "%r -> %s (%s)"
                             % (bogus, state, why))
        # POSITIVE CONTROL, unconditional: a WELL-FORMED complete census with
        # no holder does reach stale, so the refusals above are the schema and
        # not a probe that refuses everything.
        with self._census(self._complete([self._good_row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "stale")

    def test_B_an_unattributed_live_row_cannot_be_excluded(self):
        """A live Claude process nobody could attribute may be holding the very
        session we are about to declare dead. `probe_failed|who_probe_failed`
        was a hand-picked pair; identity and config-trust are the contract."""
        for gap in ({"identity": "unknown"},
                    {"who_context_mismatch": True},
                    {"root": None},
                    {"probe_failed": True},
                    {"who_probe_failed": True}):
            rows = [self._good_row(**gap)]
            with self._census(self._complete(rows)):
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", "%r -> %s (%s)" % (gap, state, why))
        with self._census(self._complete([self._good_row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "stale")

    def test_C_an_unreadable_pid_is_not_an_absent_one(self):
        """A readable proc root containing a same-uid pid whose cmdline AND
        environ cannot be opened: the walk never examined that process, so its
        silence is not evidence."""
        proc = os.path.join(self.tmp, "proc-with-a-locked-pid")
        pid_dir = os.path.join(proc, "4242")
        os.makedirs(pid_dir, exist_ok=True)
        for leaf in ("cmdline", "environ"):
            f = os.path.join(pid_dir, leaf)
            with open(f, "wb") as fh:
                fh.write(b"")
            os.chmod(f, 0)
        if os.access(os.path.join(pid_dir, "cmdline"), os.R_OK):
            self.skipTest("running as root: chmod 0 does not deny a read")
        with mock.patch.dict(os.environ, {"HELM_PROC": proc}), \
                self._census(self._complete([self._good_row()])):
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "unknown", why)
        # POSITIVE CONTROL: make the same pid READABLE and the same walk
        # reaches stale — so "unknown" measured the denied read.
        for leaf in ("cmdline", "environ"):
            os.chmod(os.path.join(pid_dir, leaf), 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": proc}), \
                self._census(self._complete([self._good_row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "stale")


class TheUncertaintySetIsPinnedToTheContract(unittest.TestCase):
    """THE ANTI-SPIRAL DEVICE, and the reason this is one pass instead of a
    sixth round.

    Every earlier round decided row completeness from a HAND-PICKED pair of
    booleans. @opus-integrator named why that generates rounds: a hand-picked
    set is right for the cases you thought of, and each case you did not is a
    new defect. So the set is no longer hand-picked — it is PINNED against the
    census row schema. A marker added upstream fails HERE, loudly, instead of
    silently widening the set of claims helm is willing to delete."""

    def test_every_uncertainty_marker_the_census_declares_is_consulted(self):  # noqa: VACUOUS_ASSERTION — unconditional positive control four lines above: assertIn('probe_failed', declared) proves the AST scan found the census row literal, so an empty `declared` cannot satisfy the subset check trivially. `missing` is derived from `declared` through a set difference and the rung cannot follow provenance across it.
        import ast as _ast
        import inspect as _inspect
        from helm import session as sess_mod
        src = _inspect.getsource(sess_mod)
        # every key a census row dict declares whose NAME marks an uncertainty
        declared = set()
        for node in _ast.walk(_ast.parse(src)):
            if not isinstance(node, _ast.Dict):
                continue
            keys = [k.value for k in node.keys
                    if isinstance(k, _ast.Constant) and isinstance(k.value, str)]
            if "identity" not in keys and "probe_failed" not in keys:
                continue                      # not a census row literal
            declared |= {k for k in keys
                         if k.endswith("_failed") or k.endswith("_mismatch")}
        # POSITIVE CONTROL, unconditional: the scan found the row literal at
        # all. An empty `declared` would satisfy the subset check trivially.
        self.assertIn("probe_failed", declared,
                      "the census row literal was not found; this test is "
                      "scanning the wrong thing and proves nothing")
        missing = sorted(declared - set(seats._ROW_UNCERTAIN))
        self.assertEqual(
            missing, [],
            "the census row declares uncertainty marker(s) %s that "
            "seats._ROW_UNCERTAIN does not consult. A row carrying one would "
            "be treated as ATTRIBUTED and could authorize deleting a live "
            "seat's claim. Add it to _ROW_UNCERTAIN — or, if it genuinely is "
            "not an uncertainty, say so here." % missing)


class SurvivingProducerCases(StaleBase):
    """@codex-2's three surviving cases of the SETTLED invariant — not a new
    class, the same one at values I had not enumerated. Two of them are the
    hand-picked-set defect committed INSIDE the anti-spiral device."""

    SID = "aaaaaaaa-0000-0000-0000-000000000000"

    def _census(self, value):
        from helm import session as sess_mod
        return mock.patch.object(sess_mod, "_proc_claude_census",
                                 return_value=value)

    def _row(self, **over):
        row = {"pid": 1, "session": "", "resume": "", "identity": "declared",
               "root": "/trusted", "who_context_mismatch": False,
               "possible_sessions": [], "probe_failed": False,
               "who_probe_failed": False, "child": False}
        row.update(over)
        return row

    def _ok(self, rows):
        return {"rows": rows, "listing_failed": False,
                "who_failed": False, "census_partial": False}

    def test_A_flags_PRESENT_but_None_are_not_answers(self):
        """Key presence is not value validity: `not None` reads as False, so a
        census that knows nothing certified itself complete."""
        nulls = {"rows": [], "listing_failed": None,
                 "who_failed": None, "census_partial": None}
        with self._census(nulls):
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "unknown", why)
        # POSITIVE CONTROL on the same call: real booleans reach stale.
        with self._census(self._ok([self._row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "stale")

    def test_B_an_identity_OUTSIDE_the_producer_enum_is_not_attributed(self):
        """A denylist passed `identity="garbage"`; the producer declares an
        ENUM and anything outside it is a row we do not understand."""
        # UNCONDITIONAL POSITIVE CONTROL FIRST: a plain good row reaches
        # stale, so every "unknown" below is attributable to the identity
        # value and not to a probe that refuses everything.
        with self._census(self._ok([self._row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "stale")
        for bogus in ("garbage", "LIVE", 0, [], "Declared"):
            with self._census(self._ok([self._row(identity=bogus)])):
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", "%r -> %s (%s)"
                             % (bogus, state, why))
        for good in ("declared", "resume", "who"):
            with self._census(self._ok([self._row(identity=good)])):
                state, _why = seats.claim_holder_liveness("someone",
                                                          session=self.SID)
            self.assertEqual(state, "stale", good)

    def test_C_a_denied_environ_can_hold_the_only_copy_of_the_sid(self):
        """Readable cmdline + EACCES environ: the walk saw the process and did
        NOT see the file that carries the sid. My earlier "neither leaf" rule
        called that examined, and a claim could be deleted on the strength of
        a file nobody opened."""
        proc = os.path.join(self.tmp, "proc-denied-environ")
        pid = os.path.join(proc, "777")
        os.makedirs(pid, exist_ok=True)
        with open(os.path.join(pid, "cmdline"), "wb") as fh:
            fh.write(b"some-unrelated-process\0")      # readable, no sid
        env = os.path.join(pid, "environ")
        with open(env, "wb") as fh:
            fh.write(("CLAUDE_CODE_SESSION_ID=%s\0" % self.SID).encode())
        os.chmod(env, 0)
        if os.access(env, os.R_OK):
            self.skipTest("running as root: chmod 0 does not deny a read")
        with mock.patch.dict(os.environ, {"HELM_PROC": proc}), \
                self._census(self._ok([self._row()])):
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "unknown", why)
        # POSITIVE CONTROL: make the environ readable and the SAME walk finds
        # the sid — so "unknown" was the denial, and the file really did hold
        # the only copy.
        os.chmod(env, 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": proc}), \
                self._census(self._ok([self._row()])):
            state, _why = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(state, "live")


class TheIdentityEnumIsPinnedToTheProducer(unittest.TestCase):
    """The marker set was pinned and the IDENTITY set was not — one function
    apart. @codex-2 walked straight through the gap with identity="garbage".
    Both are now pinned against session.py, so a producer that grows a fourth
    identity fails HERE instead of silently widening what helm will delete."""

    def test_the_enum_matches_what_the_census_can_emit(self):
        import ast as _ast
        import inspect as _inspect
        from helm import session as sess_mod
        emitted = set()
        for node in _ast.walk(_ast.parse(_inspect.getsource(sess_mod))):
            if not isinstance(node, _ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if not (isinstance(k, _ast.Constant) and k.value == "identity"):
                    continue
                emitted |= {c.value for c in _ast.walk(v)
                            if isinstance(c, _ast.Constant)
                            and isinstance(c.value, str)}
        # POSITIVE CONTROL, unconditional: the scan found the producer's own
        # ladder. An empty set would satisfy the equality trivially.
        self.assertIn("declared", emitted,
                      "the identity ladder was not found; this test is "
                      "scanning the wrong thing and proves nothing")
        self.assertEqual(
            emitted - set(seats._IDENTITY_ENUM), set(),
            "session.py can emit identity value(s) %s that seats._IDENTITY_ENUM "
            "does not know. An unknown value currently falls to NOT-attributed "
            "(safe), but the enum should say so explicitly rather than by "
            "accident." % sorted(emitted - set(seats._IDENTITY_ENUM)))


class TheWorkBoardCarriesTheThreeStates(StaleBase):
    """@codex-2's non-blocking finding, and it survived my first mutation pass
    because I fixed the surface without testing it — the same shape as every
    other miss tonight.

    `list_rows` read the holder from one snapshot and the classification from
    a SECOND, later one, and kept only `stale` — so UNKNOWN flattened back into
    healthy on the one surface that lists lanes."""

    def test_a_board_row_carries_liveness_not_just_a_stale_bool(self):
        from helm.work import _gc
        lane = "board-lane"
        self.claim_lane(lane)
        rows = _gc.list_rows(self.root)
        mine = [r for r in rows if r["lane"] == lane]
        self.assertTrue(mine, "the lane is not on the board; nothing tested")
        # ASSERT THE VALUE, NOT THE KEY. My first cut checked that "liveness"
        # was PRESENT — and `"liveness": None` still has the key, so the
        # mutation that flattens every row survived. Third presence-not-value
        # assertion I have written in this lane.
        res = [c for c in seats.claims_list()
               if c["resource"].endswith(lane)]
        self.assertTrue(res, "the claim vanished; nothing to compare against")
        self.assertEqual(mine[0].get("liveness"), res[0]["liveness"],
                         "the board row's verdict does not match the claim's: "
                         "UNKNOWN renders identically to a healthy claim")
        self.assertIn(res[0]["liveness"], ("live", "stale", "unknown"))

    def test_the_board_reads_holder_and_liveness_from_ONE_snapshot(self):
        """Two reads let a row show a holder the classification never saw."""
        from helm.work import _gc
        import inspect, textwrap
        src = textwrap.dedent(inspect.getsource(_gc.list_rows))
        # POSITIVE CONTROL, unconditional: we are reading the right function.
        self.assertIn("liveness_by", src)
        # READ THE CODE, NOT THE PROSE. My first cut asserted `_live()` was
        # absent from the source text — and it appears in the COMMENT that
        # explains what the function used to do, so the pin failed on its own
        # explanation. AST: count real CALLS.
        import ast as _ast
        tree = _ast.parse(src)
        calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)]

        def _name(fn):
            return fn.attr if isinstance(fn, _ast.Attribute) else getattr(
                fn, "id", "")
        claim_reads = sum(1 for c in calls if _name(c.func) == "claims_list")
        live_reads = sum(1 for c in calls if _name(c.func) == "_live")
        self.assertEqual(claim_reads, 1,
                         "list_rows calls claims_list %d times; the holder and "
                         "the verdict must come from ONE read, or a row can "
                         "show a holder its classification never saw"
                         % claim_reads)
        self.assertEqual(live_reads, 0,
                         "the separate holder snapshot is back")


if __name__ == "__main__":
    unittest.main()
