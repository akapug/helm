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

from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402

from helm import pk, seats, seats_claims, web_ui_loader, work  # noqa: E402

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
        # THE CENSUS AND THE `helm who` RUNG READ THE FIXTURE TOO. HELM_PROC
        # moves only the secondary scan: the session census lists
        # session.PROC and the who rung lists who.PROC, and both were the
        # node's real /proc. A gate node running a claude process nobody could
        # attribute made that census INCOMPLETE, so every dead-holder arm in
        # this file read "unknown" there and "stale" on a quiet node (19 arms
        # red on one gate node and green on the other, from one tree). The
        # fixture /proc is empty, so the real census answers complete with no
        # process in it. An arm about an incomplete census builds one: the
        # `_census` helpers below, or TheNodesProcessesAreNotEvidence.
        from helm import session as sess_mod, who
        for mod in (sess_mod, who):
            pin = mock.patch.object(mod, "PROC",
                                    os.path.join(self.tmp, "proc"))
            pin.start()
            self.addCleanup(pin.stop)
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
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
        # See tests/_tmphome.declaring. `--stale` is deliberately NOT covered
        # by this: stale release takes no identity at all (its authority is a
        # StaleReleaseProof about the DEAD holder), so those arms keep running
        # exactly as an env-less operator would run them.
        with _tmp_declaring(args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def live_holder(self, seat=ME):
        """Make THIS fixture's own claims read LIVE, and return the session.

        A CLAIM'S LIVENESS IS ITS SESSION'S. `claim_holder_liveness` answers
        UNKNOWN when no session was recorded and STALE when the recorded one is
        in no live process — and a fixture cannot put a session into a real
        process's environ, so a claim this suite mints under a declared
        identity is provably dead by construction. That is correct about the
        world and wrong about the arm: an arm whose subject is a LIVE holder's
        row must state that premise instead of inheriting it from whether the
        fixture happened to record a session at all.

        NARROW ON PURPOSE — only this seat's session is named live, so an arm
        that needs a live holder AND a dead one beside it still gets both.
        """
        from helm import session as sess_mod
        sid = _tmp_session_for(seat)
        p = mock.patch.object(sess_mod, "live_sids",
                              side_effect=lambda rows=None: {sid: [os.getpid()]})
        p.start()
        self.addCleanup(p.stop)
        return sid

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
        self.live_holder()
        self.claim_lane("alive", seat=ME)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OUTPUT: the lane IS
        # rendered. Without it a render that printed nothing at all — an empty
        # list, a refused verb, a claim that never landed — satisfies the
        # absence below, and the arm would be measuring silence.
        self.assertIn("alive", out)
        self.assertNotIn("STALE claims", out)


class TheBoardReadsTheLanesOwnClaimRow(StaleBase):
    """`helm work list` takes a lane's holder, TTL and liveness from the claim
    row stored under the lane's own resource, and from no other row
    (task/2528).

    THE DEFECT. `claim` stores a resource as given, so `worktree:proj:alive `
    is a row of its own beside `worktree:proj:alive`, and both publish as
    `worktree:proj:alive`. The board keyed holder, TTL and liveness on the
    published resource, so the padded row, which sorts last, wrote over the
    lane's row. The board then printed another seat's name and TTL beside the
    caller's own token and "(yours)", and it filed the live lane under STALE
    when the padded row's holder was dead. The padded row does not hold the
    lane: a claim on the lane's resource never reads it.

    THE DESIGN THESE ARMS PIN. The board reads only the rows whose stored
    resource is the published one (`resource_exact`). A row whose HOLDER the
    scrub changed still holds its lane and still shows."""

    DEAD = "00000000-0000-0000-0000-000000000000"

    def unheld_room(self, lane):
        """A registered lane room whose own claim has expired."""
        # EXPIRED AT BIRTH through the lane door itself: the CLI refuses a
        # zero --ttl, because a caller typing it would hold nothing.
        rc, line = work.claim(self.root, lane, ME, ttl=0)
        self.assertEqual(rc, 0, line)
        return work.resource(self.root, lane)

    def dead_claim(self, resource, holder=THEM):
        ok, msg, _lease = seats.claim(resource, holder, ttl=600,
                                      session=self.DEAD)
        self.assertTrue(ok, msg)

    def board(self, lane):
        return {r["lane"]: r for r in work.list_rows(self.root)}[lane]

    def test_a_padded_row_beside_my_lane_does_not_speak_for_it(self):
        self.live_holder()
        mine = self.claim_lane("alive", seat=ME)
        res = work.resource(self.root, "alive")
        self.dead_claim(res + " ")
        # MUST-HIT: both rows publish the lane's resource, and only the padded
        # one is stale.
        self.assertEqual(sorted((c["resource"], c["holder_id"], c["liveness"])
                                for c in seats.claims_list()),
                         [(res, ME, "live"), (res, THEM, "stale")])
        row = self.board("alive")
        self.assertEqual([row["holder"], row["lease"], row["liveness"],
                          row["stale"]], [ME, mine, "live", False])
        self.assertGreater(row["remaining"], 600, "the TTL is the padded row's")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        line = [x for x in out.splitlines() if "GUARDED alive" in x]
        self.assertEqual(len(line), 1, out)
        self.assertRegex(line[0], r"GUARDED alive  %s \d+s left" % ME)
        self.assertIn("lease=%s (yours)" % mine, line[0])
        self.assertNotIn("STALE claims", out)
        self.assertNotIn("(was %s)" % THEM, out)

    def test_a_padded_row_alone_leaves_the_lane_unheld(self):
        res = self.unheld_room("brief")
        self.dead_claim(res + " ")
        # MUST-HIT: the one live row publishes the lane's resource and is stale.
        self.assertEqual([(c["resource"], c["holder_id"], c["stale"])
                          for c in seats.claims_list()], [(res, THEM, True)])
        row = self.board("brief")
        self.assertEqual([row["holder"], row["remaining"], row["lease"],
                          row["liveness"], row["stale"]],
                         [None, None, None, None, False])
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARDED brief", out)
        self.assertNotIn("STALE claims", out)
        # AND THE BOARD IS RIGHT ABOUT THE WORLD: the lane's own resource is
        # free to claim while the padded row is live.
        ok, msg, _lease = seats.claim(res, ME, ttl=60)
        self.assertTrue(ok, msg)

    def test_a_lane_regranted_between_the_reads_keeps_its_token_off_the_new_holder(self):
        """task/2541 (review on task/2528): the board read the caller's
        tokens, then the claim rows, as two reads. A lane released and
        claimed by another seat between them showed the new holder's name
        and TTL beside the caller's released token and "(yours)". Both come
        from one read now, so the holder and the token are the same row's."""
        self.live_holder()
        mine = self.claim_lane("alive", seat=ME)
        res = work.resource(self.root, "alive")
        stored = self.stored_claim(res)
        real, fired = pk.read_json, []

        def read_json(path, *args, **kwargs):
            value = real(path, *args, **kwargs)
            if not fired and path == seats.claims_path():
                fired.append(True)
                ok, msg = seats.release(res, ME, lease=mine,
                                        session=stored.get("session"))
                self.assertTrue(ok, msg)
                ok, msg, _lease = seats.claim(res, THEM, ttl=600)
                self.assertTrue(ok, msg)
            return value
        with mock.patch.object(pk, "read_json", side_effect=read_json):
            row = self.board("alive")
        # MUST-HIT: the regrant ran inside the board's reads.
        self.assertEqual(len(fired), 1)
        self.assertEqual([c["holder_id"] for c in seats.claims_list()], [THEM])
        self.assertEqual([row["holder"], row["lease"]], [ME, mine])

    def test_a_padded_holder_on_the_lanes_own_resource_still_holds_it(self):
        """CONTROL: exactness of the RESOURCE decides, never of the holder."""
        res = self.unheld_room("held")
        self.dead_claim(res, holder=THEM + " ")
        # MUST-HIT: the stored resource is the lane's; the stored holder is not
        # the published one.
        self.assertEqual([(c["resource"], c["holder_id"], c["holder_exact"],
                           c["stale"]) for c in seats.claims_list()],
                         [(res, THEM, False, True)])
        row = self.board("held")
        self.assertEqual([row["holder"], row["lease"], row["stale"]],
                         [THEM, None, True])
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("helm work release held --stale  (was %s)" % THEM, out)


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
        self.live_holder()
        self.claim_lane("live-lane")
        rows = {c["resource"]: c for c in seats.claims_list()}
        live = [r for r, c in rows.items() if not c["stale"]]
        self.assertTrue(live, "no live claim to contrast against")
        text = self._render()
        self.assertIn(live[0], text)                  # rendered...
        self.assertNotIn("STALE", text)               # ...and unmarked


class UnreadableCensusRefusesRelease(StaleBase):
    """An unreadable session census must answer UNKNOWN, never STALE.

    A review, with a deterministic probe: `live_sids()` raising was
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
        (a second pass). Pointing session.PROC at a path that
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
        # The refusal names WHICH census input failed — here the listing.
        self.assertIn("INCOMPLETE (the /proc listing failed)", reason)
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
    lock as "working lane/X". A review found it AFTER I had fixed the two
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
    A review named this one — the fourth renderer of the same field,
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

    Third round. `live_sids` answers a DIFFERENT question and its
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

    Fourth round. `_sessions_with_a_process` does a bare os.listdir
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
        # THE SENTENCE NAMES THE READING: HELM_PROC is set here, so blaming it
        # is true — and the errno says it was a listing that failed.
        self.assertIn("could not be LISTED (ENOENT)", reason)
        self.assertIn("(HELM_PROC)", reason)
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


class TheScanSaysWhichReadingHappened(StaleBase):
    """One boolean held four readings of a process table, and the refusal
    printed one sentence for all of them: "could not be READ (HELM_PROC
    unreadable), so nothing was looked at".

    Measured on a desktop with HELM_PROC UNSET: the scan listed /proc, read
    hundreds of same-uid processes, and was incomplete only because a few
    non-dumpable ones (ssh-agent, systemd --user, sandboxed renderers) close
    their environ to their own user. A one-liner that typed the sid into its
    own argv then found ITSELF, so the snapshot read hits AND unreadable at
    once and looked like a contradiction.

    The verdicts do not move — a partial walk still cannot prove a death, and
    a hit still proves life. What moves is that each reading is its own state
    and the sentence names the one that happened."""

    SID = "cccccccc-0000-0000-0000-000000000000"

    def _census(self, rows=None, **flags):
        from helm import session as sess_mod
        value = {"rows": [self._row()] if rows is None else rows,
                 "listing_failed": False, "who_failed": False,
                 "census_partial": False}
        value.update(flags)
        return mock.patch.object(sess_mod, "_proc_claude_census",
                                 return_value=value)

    def _row(self, **over):
        row = {"pid": 1, "session": "", "resume": "", "identity": "declared",
               "root": "/trusted", "who_context_mismatch": False,
               "possible_sessions": [], "probe_failed": False,
               "who_probe_failed": False, "child": False}
        row.update(over)
        return row

    def _proc(self, name, carrier=None, denied=True):
        """A fixture table: pid 100 readable (carrying the sid in its cmdline
        when `carrier`), pid 200 with a readable cmdline and an environ that
        is denied when `denied`. Returns (root, the denied environ path)."""
        root = os.path.join(self.tmp, name)
        for pid, argv in (("100", b"worker\0" + (carrier or b"")),
                          ("200", b"ssh-agent\0-D\0")):
            os.makedirs(os.path.join(root, pid), exist_ok=True)
            with open(os.path.join(root, pid, "cmdline"), "wb") as fh:
                fh.write(argv)
            with open(os.path.join(root, pid, "environ"), "wb") as fh:
                fh.write(b"HOME=/nowhere\0")
        env = os.path.join(root, "200", "environ")
        if denied:
            os.chmod(env, 0)
            if os.access(env, os.R_OK):
                self.skipTest("running as root: chmod 0 does not deny a read")
        return root, env

    @contextlib.contextmanager
    def _no_proc_env(self):
        """HELM_PROC and MELD_PROC both unset — the owner's environment. A
        context, not a cleanup: cleanups run AFTER tearDown has restored the
        environment, so a patch.dict stopped there would put this fixture's
        HELM_HOME back into the process for every later test."""
        with mock.patch.dict(os.environ):
            os.environ.pop("HELM_PROC", None)
            os.environ.pop("MELD_PROC", None)
            yield

    def test_the_producer_keeps_the_three_facts_apart(self):
        from helm import seats_common as sc
        gone = os.path.join(self.tmp, "no-such-proc-root")
        with mock.patch.dict(os.environ, {"HELM_PROC": gone}):
            unlisted = sc.process_sid_reading({self.SID})
            pair = sc.process_sid_scan({self.SID})     # no longer raises
        self.assertEqual((unlisted["state"], unlisted["error"],
                          unlisted["source"], unlisted["walked"]),
                         (sc.SCAN_UNLISTED, "ENOENT", "HELM_PROC", 0))
        self.assertEqual(pair, (set(), False))
        root, env = self._proc("proc-partial")
        with mock.patch.dict(os.environ, {"HELM_PROC": root}):
            partial = sc.process_sid_reading({self.SID})
        self.assertEqual(partial["state"], sc.SCAN_PARTIAL)
        self.assertEqual(partial["walked"], 2)          # the table WAS walked
        self.assertEqual(partial["unread"],
                         [("200", None, "environ", "EACCES")])
        # POSITIVE CONTROL: the same table with the environ readable is WHOLE,
        # so PARTIAL above measured the one denied leaf and nothing else.
        os.chmod(env, 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": root}):
            whole = sc.process_sid_reading({self.SID})
        self.assertEqual((whole["state"], whole["unread"], whole["walked"]),
                         (sc.SCAN_WHOLE, [], 2))

    def test_a_hit_in_a_partial_walk_is_LIVE(self):
        """The owner's exact snapshot — hits AND not-complete — is a coherent
        pair: a process we READ carries the sid, and some other process we
        could not read. The hit stands; the partial walk only bounds the
        negative."""
        root, env = self._proc("proc-hit", carrier=self.SID.encode())
        from helm import seats_common as sc
        with mock.patch.dict(os.environ, {"HELM_PROC": root}):
            pair = sc.process_sid_scan({self.SID})
            with self._census():
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
        self.assertEqual(pair, ({self.SID}, False))
        self.assertEqual(state, "live", why)
        self.assertIn("(pid 100)", why)               # the evidence is named
        # CONTROL 1: drop the carrier, keep the denial -> UNKNOWN, not stale.
        root2, env2 = self._proc("proc-no-hit")
        with mock.patch.dict(os.environ, {"HELM_PROC": root2}), self._census():
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "unknown", why)
        # CONTROL 2: make the denied leaf readable -> the SAME walk proves
        # death, so "unknown" above was the partial read and nothing else.
        os.chmod(env2, 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": root2}), self._census():
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "stale", why)

    def test_no_reading_but_WHOLE_can_reach_stale(self):
        """THE SAFETY MATRIX. Every non-whole reading, under a census that
        would otherwise certify death, must stay UNKNOWN."""
        root, env = self._proc("proc-matrix")
        readings = {
            "unlisted": mock.patch.dict(
                os.environ, {"HELM_PROC": os.path.join(self.tmp, "gone")}),
            "partial": mock.patch.dict(os.environ, {"HELM_PROC": root}),
            "raised": mock.patch.object(seats_claims, "process_sid_reading",
                                        side_effect=RuntimeError("boom")),
        }
        got = {}
        for name, reading in readings.items():
            with reading, self._census():
                got[name] = seats.claim_holder_liveness("someone",
                                                        session=self.SID)[0]
        self.assertEqual(got, {"unlisted": "unknown", "partial": "unknown",
                               "raised": "unknown"})
        os.chmod(env, 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": root}), self._census():
            state, why = seats.claim_holder_liveness("someone",
                                                     session=self.SID)
        self.assertEqual(state, "stale", why)

    def test_a_partial_walk_REFUSES_the_release(self):  # noqa: VACUOUS_ASSERTION — in-test positive control below: with the denied leaf made readable the same release goes through and the same claim disappears
        lane = "partial-scan-lane"
        self.claim_lane(lane)
        raw = pk.read_json(seats.claims_path(), {}) or {}
        key = next(r for r in raw if isinstance(raw[r], dict) and lane in r)
        raw[key] = dict(raw[key], session=self.SID)
        pk.write_json(seats.claims_path(), raw)
        root, env = self._proc("proc-release")
        with mock.patch.dict(os.environ, {"HELM_PROC": root}), self._census():
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertFalse(ok, msg)
        self.assertIn("PARTIAL", msg)
        self.assertIsNotNone(self.stored_claim(key))     # the claim SURVIVES
        os.chmod(env, 0o600)
        with mock.patch.dict(os.environ, {"HELM_PROC": root}), self._census():
            ok, msg = seats.release_stale(key, "another-seat")
        self.assertTrue(ok, msg)
        self.assertIsNone(self.stored_claim(key))

    def test_the_refusal_names_the_input_that_failed(self):
        from helm import seats_common as sc
        with self._no_proc_env():
            # UNLISTED, HELM_PROC UNSET: the default root is what failed, and
            # the sentence must not send the reader to a variable not set.
            gone = os.path.join(self.tmp, "default-root-gone")
            with mock.patch.object(sc, "PROC_DEFAULT", gone), self._census():
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", why)
            self.assertIn("%s (the default root) could not be LISTED (ENOENT)"
                          % gone, why)
            self.assertNotIn("HELM_PROC", why)
            # CONTROL: the SAME missing root named by HELM_PROC says so — the
            # label follows the input; it is not a constant respelled.
            with mock.patch.dict(os.environ, {"HELM_PROC": gone}), \
                    self._census():
                _state, why = seats.claim_holder_liveness("someone",
                                                          session=self.SID)
            self.assertIn("%s (HELM_PROC) could not be LISTED" % gone, why)
            # PARTIAL, HELM_PROC UNSET: the walk happened, so "nothing was
            # looked at" would be false; the sentence names pid, leaf, errno.
            root, _env = self._proc("proc-default-partial")
            with mock.patch.object(sc, "PROC_DEFAULT", root), self._census():
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", why)
            self.assertIn("was PARTIAL: 1 of 2 same-uid processes", why)
            self.assertIn("pid 200 ? environ:EACCES", why)
            self.assertNotIn("nothing was looked at", why)
            self.assertNotIn("HELM_PROC", why)
            # RAISED: the exception type, not a guessed cause.
            with mock.patch.object(seats_claims, "process_sid_reading",
                                   side_effect=RuntimeError("boom")), \
                    self._census():
                state, why = seats.claim_holder_liveness("someone",
                                                         session=self.SID)
            self.assertEqual(state, "unknown", why)
            self.assertIn("raised RuntimeError", why)

    def test_the_census_refusal_names_its_gap(self):
        """One rung down, the same defect: "the session census is UNREADABLE"
        for a census that listed /proc fine and found a live claude process
        nobody could attribute."""
        root, _env = self._proc("proc-clean", denied=False)
        with mock.patch.dict(os.environ, {"HELM_PROC": root}):
            with self._census(rows=[self._row(pid=4321, identity="unknown")]):
                loose = seats.claim_holder_liveness("someone",
                                                    session=self.SID)
            with self._census(who_failed=True):
                who = seats.claim_holder_liveness("someone", session=self.SID)
            with self._census(census_partial=True):
                part = seats.claim_holder_liveness("someone", session=self.SID)
            with self._census():                       # POSITIVE CONTROL
                control = seats.claim_holder_liveness("someone",
                                                      session=self.SID)
        self.assertEqual(control[0], "stale", control[1])
        self.assertEqual((loose[0], who[0], part[0]),
                         ("unknown", "unknown", "unknown"))
        self.assertIn("the session census is INCOMPLETE (1 live claude "
                      "process with no attributable session (pid 4321))",
                      loose[1])
        self.assertIn("INCOMPLETE (the `helm who` rung was never probed)",
                      who[1])
        self.assertIn("INCOMPLETE (a process could not be probed far enough "
                      "to tell whether it is claude)", part[1])
        self.assertNotIn("UNREADABLE", loose[1] + who[1] + part[1])

    def test_a_sid_typed_into_the_asking_command_is_the_question(self):
        """The real /proc, on purpose: the annotation only exists for pids in
        our own namespace. A child that names a FRESH sid in its argv finds
        itself; the same child reading the sid from stdin does not."""
        import uuid
        sid = str(uuid.uuid4())
        prog = ("import sys; sys.path.insert(0, %r)\n"
                "from unittest import mock\n"
                "from helm import seats_claims, session\n"
                "sid = sys.argv[1] if len(sys.argv) > 1 "
                "else sys.stdin.read()\n"
                "with mock.patch.object(session, '_proc_claude_census', "
                "return_value={'rows': [], 'listing_failed': False, "
                "'who_failed': False, 'census_partial': False}):\n"
                "    print('\\t'.join(seats_claims.claim_holder_liveness("
                "'x', session=sid.strip())))\n"
                % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {k: v for k, v in os.environ.items()
               if k not in ("HELM_PROC", "MELD_PROC")}
        typed = subprocess.run([sys.executable, "-c", prog, sid], env=env,
                               capture_output=True, text=True, timeout=60)
        piped = subprocess.run([sys.executable, "-c", prog], input=sid,
                               env=env, capture_output=True, text=True,
                               timeout=60)
        self.assertEqual(typed.returncode, 0, typed.stderr)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        state, why = typed.stdout.strip().split("\t", 1)
        self.assertEqual(state, "live", why)
        self.assertIn("the command asking or its parent", why)
        # CONTROL: the same question without the sid in argv finds no
        # carrier, so the annotation above measured the typed argv.
        state, why = piped.stdout.strip().split("\t", 1)
        self.assertNotEqual(state, "live", why)
        self.assertNotIn("the command asking", why)


class OneSnapshotClassifiesTheWholeTable(StaleBase):
    """Meld matrix, banked mutant 7: per-row census call count and the
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
    """Banked mutant 6: renderer ignores state.

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
    """The meld FIX: three PRODUCER shapes that still turned an unknown
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
    booleans. The integrator named why that generates rounds: a hand-picked
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
    """The three surviving cases of the SETTLED invariant — not a new
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
    apart. A review walked straight through the gap with identity="garbage".
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
    """A non-blocking finding, and it survived my first mutation pass
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


class AnUnlistedHolderIsNAMEDNotLeftToTheReader(StaleBase):
    """The roster printed a seat list and then printed claims held
    by seats the list did not contain — two answers on ONE screen.

    Measured: `helm chat roster` listed five seats, then named two OTHER
    seats holding worktree leases with live fences and time remaining.
    Both halves were honest. The screen was not, and the seat list is the half
    that looked complete, so a reader scanning it concluded those seats were
    gone. `dispatch send` consults that same list, which is why it refused to
    route work to two demonstrably working reviewers inside one hour.

    The write path underneath is the other half: a roster row is created
    ONLY by an explicit `helm chat join` (seats_join.join has exactly one
    caller), and nothing in the tree ever repairs a missing one. Until that is
    settled, the surface must at minimum refuse to state the contradiction
    silently — which is what this class holds."""

    def _roster_render_all(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats.cmd("seats", ["--all"], "main")
        return out.getvalue()

    def _roster_render(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats.cmd("seats", [])
        return out.getvalue()

    def _claims_render(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            seats._cmd_claims([])
        return out.getvalue()

    def test_a_holder_with_no_roster_row_is_MARKED_on_the_claims_verb(self):
        self.claim_lane("unlisted-lane")       # ME holds it and never joined
        text = self._claims_render()
        self.assertIn("unlisted-lane", text,
                      "the claim row did not render; nothing below is tested")
        self.assertIn("NO SEAT ROW", text)

    def test_the_ROSTER_API_carries_listedness_not_only_the_TERMINALS(self):
        """THE THIRD CONSUMER, AND A REVIEWER HAD TO FIND IT BECAUSE BOTH
        TERMINAL ARMS WERE GREEN.

        Two renderers assert the mark above. The roster API — what the owner's
        WEB roster actually reads — answered without listedness at all, so the
        dashboard kept printing a claims footer naming holders its own seat
        table did not list, with nothing on the page saying so. That is this
        lane's exact defect, surviving on the surface the owner is most likely
        to be looking at, while the CLI it does not use was cured.

        The pair below is the whole point: the SAME claim, the SAME call, and
        only the roster changes. Without the pole, an arm asserting "unlisted"
        could pass against a field that says "unlisted" unconditionally."""
        from helm import web_roster
        self.claim_lane("api-unlisted-lane")     # ME holds it and never joined
        web_roster._ROSTER_REP_CACHE.clear()     # the report is TTL-cached

        def api_claim():
            """The claim row as the ROSTER API serves it.

            Matched on the resource SUFFIX, because a lane claim is stored
            under a namespaced resource — `worktree:<project>:<lane>`, not the
            bare lane name. The first draft looked up the bare name, found
            nothing, and its must-hit assertion caught that rather than letting
            the listedness check below pass over an empty dict."""
            rep = web_roster._roster_cached("main")
            for c in (rep.get("claims") or []):
                if str(c.get("resource") or "").endswith("api-unlisted-lane"):
                    return c
            return None

        got = api_claim()
        self.assertIsNotNone(got, "the claim never reached the API at all; "
                                  "nothing below is tested")
        self.assertEqual(got.get("listedness"), "unlisted",
                         "the roster API served a claim with no listedness — "
                         "the web surface cannot draw what it is not sent")

        # THE POLE: give the holder a roster row and the same claim reads
        # listed, which proves the field is COMPUTED rather than pinned.
        pk.write_json(seats.roster_path(), {ME: {"session": "s"}})
        web_roster._ROSTER_REP_CACHE.clear()
        again = api_claim()
        self.assertIsNotNone(again, "the claim vanished from the API between "
                                    "the two reads")
        self.assertEqual(again.get("listedness"), "listed",
                         "a holder WITH a roster row still read unlisted, so "
                         "the assertion above proves nothing")

    def test_the_ROSTER_marks_it_too_and_COUNTS_it(self):
        """The second renderer, because covering one is this module's own
        recorded defect (see test_the_ROSTER_says_it_too_not_only_the_claims_
        verb): stripping the marker from `claims` failed a test and stripping
        it from the roster failed nothing. The count is a separate assertion
        from the mark — the per-line mark serves a reader scanning CLAIMS, and
        the summary serves the reader scanning the SEAT LIST, who is the one
        the incident actually misled."""
        seats.write_roster("bob")     # a seat list that does NOT contain ME
        self.claim_lane("unlisted-lane")
        text = self._roster_render()
        self.assertIn("unlisted-lane", text,
                      "the claim row did not render; nothing below is tested")
        self.assertIn("NO SEAT ROW", text)
        self.assertIn("NOT in the seat list", text)
        self.assertIn(ME, text)       # and it NAMES the holder, not just a count

    def test_a_LISTED_holders_claim_row_says_nothing_of_the_kind(self):
        """The control that makes the two above mean something: a marker that
        fired unconditionally would satisfy both while telling every reader
        their own joined seat is unreachable. Discrimination, not presence —
        the same fixture differing only by whether the holder joined.
        """      # noqa: VACUOUS_ASSERTION — positive control is assertIn("listed-lane"): the row IS rendered, and only the marker is absent
        seats.write_roster(ME)                 # ME is on the roster this time
        self.claim_lane("listed-lane")
        text = self._claims_render()
        self.assertIn("listed-lane", text,
                      "the claim row did not render; the absence below would "
                      "be vacuous")
        self.assertNotIn("NO SEAT ROW", text)

    def test_a_REAL_malformed_roster_reads_UNKNOWN_not_unlisted(self):
        """The unreadable state, built with BYTES rather than a mock.

        seats_common.roster() is pk.read_json, which SWALLOWS malformed,
        missing and wrong-shaped state alike and returns {} — it never
        raises. So an unreadable roster makes every holder read `unlisted`
        unless the predicate consults roster_checked, and a fixture that
        mocks an EXCEPTION tests a path production cannot reach. Writing real
        garbage to the real path is what makes the assertion mean something:
        the state arrives through the same door production would use."""
        self.claim_lane("unreadable-lane")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")     # a state production can produce
        state = seats_claims.claim_holder_listedness({"holder": ME})
        text = self._claims_render()
        self.assertEqual(state, "unknown",
                         "a malformed roster read as a fact about the holder")
        self.assertIn("ROSTER UNREADABLE", text)
        self.assertNotIn("NO SEAT ROW", text)   # never accuse on a blind read

    def test_a_HOSTILE_roster_KEY_never_reaches_either_render(self):  # noqa: VACUOUS_ASSERTION — four unconditional must-hits (both renders show the claim row AND fire the marker) plus assertIn(HOSTILE, planted) proving the key IS in the roster this code reads; the positive control is necessarily on a DIFFERENT container than the absences, because the whole claim is that the key does not travel from the roster to the screen
        """EXECUTING THE ALLOWLIST REASON INSTEAD OF ASSERTING IT.

        This module is registered in the display-launder tripwire as
        INTERNAL-MATCHING-ONLY, on the claim that no roster KEY can reach a
        sink from here: the predicate returns one of three fixed words, the
        marker returns constant sentences, and the holder name printed beside
        them comes from the CLAIM row, laundered by the caller. That reason is
        the entire value of the allowlist entry — a false one is a lie inside
        a security guard, which is worse than the red gate it replaced.

        A roster key is attacker-supplied text: it is whatever some process
        exported as its chat name. So the claim is a CANNOT-HAPPEN, and the
        rule for those is to construct the thing and watch it not appear.
        The must-hit control is what stops this being vacuous — the claim row
        has to actually RENDER, and the marker has to actually FIRE, before
        the key's absence means anything at all.
        """
        HOSTILE = "zz-ev\x1b[2Jil-\u202eseat"
        self.claim_lane("hostile-key-lane")          # held by ME, unlisted
        pk.write_json(seats.roster_path(),
                      {HOSTILE: {"session": "sid-hostile"}})

        # POSITIVE CONTROL ON THE ABSENT OBSERVABLE ITSELF, which the guard
        # asked for and was right to: if write_json had silently failed, the
        # key would be missing from the output for a reason that has nothing
        # to do with laundering, and every assertNotIn below would pass over
        # a fixture that never planted anything.
        planted = pk.read_json(seats.roster_path(), {}) or {}
        self.assertIn(HOSTILE, planted,
                      "the hostile key is not in the roster this code reads; "
                      "its absence downstream would prove nothing")
        with open(seats.roster_path(), encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("\\u001b", raw.replace("\x1b", "\\u001b"),
                      "the planted key carries no ESC; the escape-absence "
                      "assertions below would be vacuous")

        claims_text, roster_text = self._claims_render(), self._roster_render()
        # UNCONDITIONAL MUST-HITS, outside any loop, because an absence
        # assertion whose positive control sits behind a loop is only as
        # honest as the loop running — and a reader cannot see that at a
        # glance. Both renders must show the row AND fire the marker before
        # any of the absences below are evidence of laundering rather than
        # evidence of an empty screen.
        self.assertIn("hostile-key-lane", claims_text)
        self.assertIn("NO SEAT ROW", claims_text)
        self.assertIn("hostile-key-lane", roster_text)
        self.assertIn("NO SEAT ROW", roster_text)

        for label, text in (("claims", claims_text), ("roster", roster_text)):
            # THE CLAIM ITSELF: the key is in the roster this code just read,
            # and it must not have travelled from there onto the screen.
            self.assertNotIn(HOSTILE, text,
                             "%s render emitted a raw roster KEY" % label)
            self.assertNotIn("\x1b", text, "%s render emitted ESC" % label)
            self.assertNotIn("\u202e", text,
                             "%s render emitted a bidi override" % label)

    def test_a_long_holder_survives_the_DISPLAY_CLIP_through_production(self):
        """DRIVE THE ENTRY POINT, NOT THE PREDICATE — this arm exists because
        its sibling below did the opposite and passed over a live defect.

        claims_list publishes a `holder` that is scrubbed AND clipped to 40
        with an ellipsis. Calling claim_holder_listedness directly hands it the
        RAW name and answers `listed`; production hands it the truncation and
        answers `unmeasurable`, so both human surfaces said HOLDER UNREADABLE
        about a seat with an exact roster row. Identity and its rendering are
        two fields now: `holder_id` is scrubbed but never clipped, because
        laundering is a safety property and truncation is a layout one.

        Everything here goes through claims_list and the rendered surface. A
        predicate-level assertion cannot see this class at all."""
        LONG = "seat-" + "x" * 45                  # 50 chars, a valid token
        pk.write_json(seats.roster_path(), {LONG: {"session": "s"}})
        self.claim_lane("clip-lane", seat=LONG)

        rows = [c for c in seats_claims.claims_list()
                if "clip-lane" in c["resource"]]
        self.assertEqual(len(rows), 1, "fixture produced no claim row")
        row = rows[0]
        self.assertLess(len(row["holder"]), len(LONG),
                        "the display value is not clipped; this arm would "
                        "pass for the wrong reason")
        self.assertEqual(row["holder_id"], LONG)   # identity survives intact
        self.assertEqual(seats_claims.claim_holder_listedness(row), "listed",
                         "production accused a listed seat")

        text = self._claims_render()
        self.assertIn("clip-lane", text)           # must-hit: it rendered
        self.assertNotIn("HOLDER UNREADABLE", text)
        # and the display is STILL clipped — the cure must not widen the column
        self.assertNotIn(LONG, text)

    def test_a_holder_we_cannot_COMPARE_is_never_accused_of_being_unlisted(self):
        """recipient_matches answers False for TWO different reasons — the
        names differ, or the name could not be canonicalised at all — and
        reading the second as the first ACCUSES a holder we merely failed to
        compare. Measured by a reviewer: a holder longer than the seat-token
        cap is IN the roster and read `unlisted`.

        Same collapse as the fail-open roster, one level down, and the same
        cure: say what could not be done. A missing, empty or non-string
        holder is malformed for the same reason and used to return `listed`,
        which silently suppressed the marker on exactly the rows most likely
        to be broken.

        One table, because the property is DISCRIMINATION: the states must
        differ across these inputs, not merely be non-None on each."""
        LONG = "seat-" + "x" * 60          # over the seat-token cap
        pk.write_json(seats.roster_path(),
                      {LONG: {"session": "s"}, ME: {"session": "a"}})
        listedness = seats_claims.claim_holder_listedness
        self.assertEqual(listedness({"holder": ME}), "listed")
        self.assertEqual(listedness({"holder": "nobody-here"}), "unlisted")
        self.assertEqual(listedness({"holder": LONG}), "unmeasurable",
                         "a holder that is IN the roster but uncomparable "
                         "was accused of being absent")
        for malformed in ({}, {"holder": ""}, {"holder": "   "},
                          {"holder": 42}, {"holder": None}):
            self.assertEqual(listedness(malformed), "unmeasurable",
                             "malformed holder %r did not say so" % malformed)
        self.assertIn("HOLDER UNREADABLE",
                      seats_claims.claim_unlisted_mark({"holder": LONG}))

    def test_the_two_marks_on_one_line_never_contradict_each_other(self):
        """Liveness and listedness are different axes decided by different
        probes, and both render on the SAME LINE. The listedness sentence used
        to read "holder is live enough to hold this lease", so a stale holder
        printed "holder is dead" and "holder is live" side by side — the
        self-contradicting screen this marker exists to end, reproduced inside
        the cure for it.

        A mark may state only what its own probe decided."""
        pk.write_json(seats.roster_path(), {})
        marks = seats_claims.claim_marks
        stale = marks({"holder": "ghost", "liveness": "stale"})
        self.assertIn("STALE", stale)          # must-hit: both marks present
        self.assertIn("NO SEAT ROW", stale)
        self.assertNotIn("is live", stale)     # ...and they do not disagree
        self.assertNotIn("live enough", stale)

    def test_an_EMPTY_roster_does_not_hide_a_live_claim(self):
        """"No seats" is a fact about the ROSTER, never about the work.

        The seats render early-returned "no seats yet" before reaching the
        claims section, so a held lease vanished from the surface exactly when
        the seat list was emptiest — which is precisely when every holder is
        unlisted and the contradiction this render exists to name is at its
        widest. The must-miss below keeps the short-circuit for a board that
        genuinely has nothing on it."""
        self.claim_lane("zero-roster-lane")
        pk.write_json(seats.roster_path(), {})
        text = self._roster_render()
        self.assertIn("zero-roster-lane", text,
                      "an empty roster hid a live claim")
        self.assertIn("NO SEAT ROW", text)

        os.remove(seats.claims_path())          # nothing at all on the board
        self.assertIn("no seats yet", self._roster_render(),
                      "the short-circuit should survive for a truly empty "
                      "board; removing it entirely is the mirror regression")

    def test_the_render_acquires_the_roster_a_PINNED_number_of_times(self):
        """COUNT THE READS, because a comment claiming one has failed four
        times on this axis. The prose said "ONE ROSTER READ FOR THE WHOLE
        RENDER" while sitting INSIDE the per-claim loop; a later version took
        membership from one read and the failed bit from another. Both shipped
        green, and both let two rows on one screen describe two different
        rosters — the contradiction this surface exists to name.

        A render may consult the roster once. That is not a style rule: two
        reads mean two moments, and the whole marker is a claim about ONE."""
        self.claim_lane("read-count-a")
        self.claim_lane("read-count-b")          # two claims, so per-claim reads show
        seats.write_roster(THEM)

        import helm.seats_cli as cli
        import helm.seats_report as report

        def reads(render):
            """(text, acquisition count) — counting EVERY binding.

            The first version of this arm patched ONE name and reported 1
            while a second reader went uncounted entirely, so it certified the
            property it was written to protect while that property was false.

            BOTH DOORS ARE STILL COUNTED, and they are now different
            FUNCTIONS rather than two bindings of one: the claims verb takes
            `roster_checked` (module-scope in seats_cli) and the report takes
            `roster_acquired` (module-scope in seats_report). Patching the
            defining module reaches neither, which is why each is patched where
            it is BOUND. A count that follows only one of them is how this arm
            was wrong the first time.
            """
            calls = []
            # THE REPORT'S DOOR IS roster_acquired NOW, NOT roster. Counting
            # the old name after the port went through would have measured
            # ZERO reads and passed forever — a counter blind to the function
            # under test reads exactly like a render that stopped touching the
            # roster. The pin exists to catch an EXTRA acquisition, and a pin
            # bound to a name nothing calls cannot catch anything.
            real_checked, real_acq = cli.roster_checked, report.roster_acquired
            def c1(*a, **k):
                calls.append("checked")
                return real_checked(*a, **k)
            def c2(*a, **k):
                calls.append("acquired")
                return real_acq(*a, **k)
            cli.roster_checked, report.roster_acquired = c1, c2
            try:
                return render(), len(calls)
            finally:
                cli.roster_checked, report.roster_acquired = real_checked, real_acq

        claims_text, claims_reads = reads(self._claims_render)
        roster_text, roster_reads = reads(self._roster_render)
        self.assertIn("read-count-a", claims_text)
        self.assertIn("read-count-a", roster_text)
        # EXACTLY ONE, AND THE PORT THIS PIN WAS WAITING FOR IS WHAT MADE IT
        # ONE. The number was 2 with a comment saying it "must become 1 the
        # moment the port lands"; roster_acquired landed, roster_report now
        # answers the seat list AND the verdict from a single read, and the
        # marks take the listedness stamped on the row instead of asking again.
        # The old pin did its whole job: it named the number it would become,
        # and it reddened on the commit that changed it rather than passing
        # quietly at a stale 2.
        #
        # ONE IS ALSO THE PROPERTY, NOT JUST THE COUNT. Two acquisitions is
        # what let a render list a seat above and call the same holder unlisted
        # below — the contradiction this lane is named for. A THIRD read is a
        # regression and so is a SECOND.
        self.assertEqual(roster_reads, 1,
                         "the seats render acquired the roster %d times; ONE "
                         "is the property — a second read is what let two "
                         "halves of one screen describe two rosters"
                         % roster_reads)
        self.assertLessEqual(claims_reads, 1,
                             "the claims render acquired the roster %d times"
                             % claims_reads)

    def test_a_FAILED_read_narrates_the_whole_screen_not_half(self):
        """ONE ACQUISITION IS NOT ONE STORY, and this is where four rounds of
        half-fixes finally ended.

        When validation fails the two views legitimately DIFFER — raw still
        holds the rows, checked is empty — so the seat list could name a seat
        while every claim beside it read ROSTER UNREADABLE. Same contradiction
        as the original defect, in its last hiding place: not two reads, but
        ONE read narrated two ways.

        A failed probe must therefore reach BOTH halves. The must-miss keeps
        the banner off a healthy roster, because a warning that always fires
        is not a warning."""
        self.claim_lane("coherence-lane")
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({ME: {"session": "s"}, "bad": {"sessions": [1, 2]}}, f)
        text = self._roster_render_all()
        self.assertIn("coherence-lane", text)            # must-hit: it rendered
        self.assertIn(ME, text)                          # ...and DOES list the seat
        self.assertIn("DID NOT VALIDATE", text,
                      "the seat list named a seat while the claims beside it "
                      "said the roster was unreadable, and nothing on screen "
                      "reconciled the two")
        self.assertIn("ROSTER UNREADABLE", text)

        # MUST-MISS: a healthy roster carries no such banner
        pk.write_json(seats.roster_path(), {ME: {"session": "s"}})
        healthy = self._roster_render_all()
        self.assertIn(ME, healthy)
        self.assertNotIn("DID NOT VALIDATE", healthy)

    def test_a_FAILED_read_yields_NO_ROWS_so_listedness_cannot_be_positive(self):
        """THE READER IS ALL-OR-NOTHING, AND THIS PINS THAT TO THE MARKER.

        What stood here asserted the asymmetry "positive evidence survives a
        failed read": a holder FOUND among the rows that passed validation is
        listed even when the file failed, while NOT finding one proves nothing.
        The rule is sound and the arm was not, which a reviewer caught: it built
        the snapshot BY HAND as ({ME: ...}, True) — rows present, read failed —
        and roster_checked cannot produce that pair. One invalid row fails the
        WHOLE file and returns {}. The fixture was proving a property of itself,
        and it passed for exactly that reason.

        SO THIS ARM DRIVES THE REAL READER AND ASSERTS THE COUPLING INSTEAD.
        The first assertion is the load-bearing one: a failed read returns NO
        rows, which is WHY the positive branch is unreachable and why the
        marker answers `unknown` rather than accusing anyone. Should the reader
        ever return the rows that PASSED validation, this arm goes red at that
        first line and points straight at the branch in claim_holder_listedness
        that must come back with it. A test that fails when a capability is
        ADDED is the right alarm here, because the marker's logic depends on
        the absence of that capability."""
        listedness = seats_claims.claim_holder_listedness
        # MALFORMED, through the real reader — not a tuple I typed.
        # The chat dir is created lazily by the first WRITER, and this arm
        # writes the roster by hand before anything else has touched it, so it
        # must make the directory itself rather than assume a previous test
        # left one behind.
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            f.write('{"' + ME + '": {"session": 12345}}')   # session must be str
        rows, failed = seats.roster_checked()
        self.assertTrue(failed, "a wrong-shaped row did not fail the read")
        self.assertEqual(rows, {},
                         "roster_checked returned SURVIVING rows on a failed "
                         "read — it is no longer all-or-nothing, so restore "
                         "the positive-evidence branch in "
                         "claim_holder_listedness that this arm licenses")
        self.assertEqual(listedness({"holder": ME}, snap=(rows, failed)),
                         "unknown",
                         "a holder was judged from a read that returned nothing")

        # THE POSITIVE POLE, same holder, same call, only the file differs: a
        # VALID roster still says listed, so `unknown` above measured the
        # failure and not a marker that simply never fires.
        pk.write_json(seats.roster_path(), {ME: {"session": "s"}})
        good = seats.roster_checked()
        self.assertFalse(good[1])
        self.assertEqual(listedness({"holder": ME}, snap=good), "listed")
        self.assertEqual(listedness({"holder": "nobody"}, snap=good), "unlisted")

    def test_a_MISSING_roster_is_proven_empty_and_still_says_unlisted(self):
        """The control that keeps the fix from over-refusing, and it is the
        distinction roster_checked exists to draw: MISSING is a proven-empty
        roster (failed False), UNREADABLE is a failed probe (failed True).
        Collapsing them would be the mirror error — every genuinely unlisted
        holder would render UNKNOWN and the marker would never fire at all,
        which is a silent regression a green suite would not show."""
        self.claim_lane("missing-roster-lane")
        try:
            os.unlink(seats.roster_path())
        except FileNotFoundError:
            pass
        self.assertEqual(
            seats_claims.claim_holder_listedness({"holder": ME}), "unlisted",
            "a missing roster is PROVEN empty; refusing here would silence "
            "the marker for every real gap")



class TheNodesProcessesAreNotEvidence(StaleBase):
    """A dead holder's claim reads STALE from the fixture's own empty /proc,
    whatever the node running the suite has in its process table; and it reads
    UNKNOWN when the arm PLANTS the shape a gate node really had, a live
    claude process nobody could attribute (two of them on the build host). The planted
    census is the same contract the real one answers."""

    DEAD = "00000000-0000-0000-0000-000000000000"

    def _unattributed_census(self):
        from helm import session as sess_mod
        row = {"pid": 3959719, "session": "", "resume": "",
               "identity": "unknown", "root": None,
               "who_context_mismatch": False, "possible_sessions": [],
               "probe_failed": False, "who_probe_failed": False,
               "child": False}
        return mock.patch.object(
            sess_mod, "_proc_claude_census",
            return_value={"rows": [row], "listing_failed": False,
                          "who_failed": False, "census_partial": False})

    def _row(self, resource):
        rows = [r for r in seats.claims_list() if r["resource"] == resource]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_an_unattributed_node_process_is_unknown_only_when_planted(self):
        from helm import session as sess_mod, who
        key = "worktree:proj:dead-on-any-node"
        seats.claim(key, THEM, ttl=600, session=self.DEAD)
        self.assertEqual((sess_mod.PROC, who.PROC),
                         (os.environ["HELM_PROC"],) * 2,
                         "the census and the who rung list the fixture /proc")
        self.assertEqual(seats_claims.liveness_snapshot({self.DEAD})[
            "complete"], True, "the fixture's census is complete")
        self.assertEqual((self._row(key)["liveness"], self._row(key)["stale"]),
                         ("stale", True))
        with self._unattributed_census():
            planted = self._row(key)
            state, why = seats.claim_holder_liveness(THEM, session=self.DEAD)
            ok, msg = seats.release_stale(key, ME)
        self.assertEqual((planted["liveness"], planted["stale"]),
                         ("unknown", False))
        self.assertEqual(state, "unknown")
        self.assertIn("1 live claude process with no attributable session "
                      "(pid 3959719)", why)
        self.assertFalse(ok, msg)
        self.assertIsNotNone(self.stored_claim(key), "an unknown holder keeps "
                             "the claim")
        ok, msg = seats.release_stale(key, ME)
        self.assertTrue(ok, "control: the fixture's census proves the death "
                            "and the same release goes through: %s" % msg)


if __name__ == "__main__":
    unittest.main()
