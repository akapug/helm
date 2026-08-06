#!/usr/bin/env python3
"""The holder can read back their OWN lease token.

Measured live 2026-07-29 while releasing a landed lane. `helm work release`
refuses without `--lease`, and NO read surface handed the holder their own
token: `helm chat claims` printed resource -> holder (TTL, fence), `helm work
list` printed lane/holder/TTL/state/path, and `helm chat claims --json`
silently ignored the flag and printed the human table anyway. The only route
left was reading `/dev/shm/helm-chat/.claims.json` by hand — which the
integrator did.

Why that refusal was exactly backwards. The token was described as "the
grant's capability", but every seat on this box runs as the same uid and can
read the whole ledger out of that file (mode 664 in a 0700 dir — the dir mode
stops another USER, not another SEAT). So the refusal stopped no thief, and
stranded the honest holder whose token died with a compaction: the lane stayed
locked for the rest of the TTL (measured 11868s = 3.3h) with the worktree
locked and the branch undeletable.

So: the surfaces that LIST the obligation now hand back what closing it
requires — for the calling seat's own rows only, never another holder's. There
is no security change here, and this file asserts none; it asserts that the
token STRING appears for a self-held row and is ABSENT for a foreign one, and
that a token read off a surface actually releases the lane.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import seats, work  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR",
            "HELM_PRIVATE_NEEDLES", "HELM_SCRATCH_GC")

ME = "lane-holder"       # the seat these tests act as
THEM = "other-seat"      # a different seat, whose token must never show up


class LeaseRecoveryBase(unittest.TestCase):
    """Hermetic: tmp HELM_HOME + HELM_CHAT_DIR, git config nulled, a scratch
    repo per test. Nothing here reads or writes the real estate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-leaserec-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(
            self.tmp, "no-needles-configured.txt")
        os.environ["HELM_CHAT_NAME"] = ME     # THE ambient identity seam
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

    def chat(self, verb, args=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), "main")
        return rc, out.getvalue(), err.getvalue()

    def claim_lane(self, lane, seat=ME):
        """-> lease id. Checks a lane in through the real desk."""
        rc, out, err = self.work("claim", lane, "--seat", seat)
        self.assertEqual(rc, 0, err)
        return out.strip().split("\t")[2]


class OwnLeasesTest(LeaseRecoveryBase):
    def test_own_row_yields_its_token_and_a_foreign_row_never_does(self):
        mine = self.claim_lane("mine", seat=ME)
        theirs = self.claim_lane("theirs", seat=THEM)
        self.assertNotEqual(mine, theirs)
        got = seats.own_leases()
        self.assertEqual(got, {"worktree:proj:mine": mine})
        # stated as an absence too: the other holder's token is not in ANY value
        self.assertNotIn(theirs, got.values())
        # and the same function answers for THEM when asked explicitly (this is
        # how a surface scopes to a row's holder, never how a caller names one)
        self.assertEqual(seats.own_leases(THEM), {"worktree:proj:theirs": theirs})

    def test_expired_row_is_swept_out_and_the_read_never_writes(self):
        seats.claim("port:1", ME, ttl=0)               # expired at birth
        live = seats.claim("port:2", ME, ttl=600)[2]
        before = os.stat(seats.claims_path())
        self.assertEqual(seats.own_leases(), {"port:2": live})
        after = os.stat(seats.claims_path())
        # claims_list owns the GC-on-read leg; a token lookup must not churn
        # the file that every claim/release contends on
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_holder_match_is_exact_so_a_printed_token_always_releases(self):
        """Case-folding here would print a token `release` then refuses:
        _binding_ok compares `seat != row['holder']` exactly. A surface that
        hands back an unusable token is worse than one that hands back
        nothing, so the match stays exact and this pins it."""
        lease = seats.claim("port:9", "Lane-Holder", ttl=600)[2]
        self.assertEqual(seats.own_leases(), {})        # ambient seat is ME
        self.assertNotIn(lease, seats.own_leases().values())
        ok, _msg = seats.release("port:9", ME, lease=lease)
        self.assertFalse(ok)                            # exactly why: ME != holder


class ChatClaimsSurfaceTest(LeaseRecoveryBase):
    def test_claims_prints_my_token_and_not_the_other_holders(self):
        mine = self.claim_lane("mine", seat=ME)
        theirs = self.claim_lane("theirs", seat=THEM)
        rc, out, err = self.chat("claims")
        self.assertEqual(rc, 0, err)
        self.assertIn(mine, out)                # THE effect: my token is there
        self.assertIn("(yours)", out)
        self.assertNotIn(theirs, out)           # and theirs is not
        # the row is still identifiable — the token rides an existing line
        self.assertIn("worktree:proj:mine -> " + ME, out)
        self.assertIn("worktree:proj:theirs -> " + THEM, out)

    def test_claims_json_carries_the_same_own_only_rule(self):
        mine = self.claim_lane("mine", seat=ME)
        theirs = self.claim_lane("theirs", seat=THEM)
        rc, out, err = self.chat("claims", ["--json"])
        self.assertEqual(rc, 0, err)
        rows = {r["resource"]: r for r in json.loads(out)}   # REAL json, or raise
        self.assertEqual(rows["worktree:proj:mine"]["lease"], mine)
        self.assertIsNone(rows["worktree:proj:theirs"]["lease"])
        self.assertNotIn(theirs, out)
        for key in ("resource", "holder", "remaining", "fence", "lease"):
            self.assertIn(key, rows["worktree:proj:mine"])

    def test_claims_json_on_an_empty_ledger_is_an_empty_array(self):
        rc, out, err = self.chat("claims", ["--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), [])   # parseable, not "no live claims"

    def test_an_unknown_flag_refuses_instead_of_being_dropped(self):
        """The measured second defect: `--json` was ACCEPTED AND DROPPED — the
        human table printed and rc was 0, so the caller could not tell. Every
        unrecognised flag now refuses; nothing is silently eaten."""
        self.claim_lane("mine", seat=ME)
        rc, out, err = self.chat("claims", ["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown arg '--bogus'", err)
        self.assertNotIn("worktree:proj:mine", out)   # no work ran under it
        # a name out of argv is not an identity here either
        rc, out, _e = self.chat("claims", ["--seat", THEM])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_claims_help_names_the_own_only_rule(self):
        rc, out, _err = self.chat("claims", ["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--json", out)


class WorkListSurfaceTest(LeaseRecoveryBase):
    def test_list_prints_my_token_and_not_the_other_holders(self):
        mine = self.claim_lane("mine", seat=ME)
        theirs = self.claim_lane("theirs", seat=THEM)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("lease=%s (yours)" % mine, out)
        self.assertNotIn(theirs, out)
        self.assertIn("GUARDED theirs", out)        # the row is still listed
        rows = {r["lane"]: r for r in
                work.list_rows(self.root)}          # the computed board too
        self.assertEqual(rows["mine"]["lease"], mine)
        self.assertIsNone(rows["theirs"]["lease"])

    def test_an_unheld_room_shows_no_token(self):
        """A room whose lease expired is holderless — there is no token to
        hand back, and the board must not invent one from a stale row."""
        rc, out, err = self.work("claim", "brief", "--seat", ME, "--ttl", "0")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARDED brief", out)
        self.assertNotIn("(yours)", out)
        self.assertIsNone({r["lane"]: r for r in
                           work.list_rows(self.root)}["brief"]["lease"])


class RefusalIsSelfRescuingTest(LeaseRecoveryBase):
    def test_release_refusal_names_the_surface_that_reprints_the_token(self):
        self.claim_lane("stranded", seat=ME)
        rc, _out, err = self.work("release", "stranded", "--seat", ME)
        self.assertEqual(rc, 1)
        self.assertIn("stays held", err)
        self.assertIn("the lease id", err)
        # the whole point: a route out, on the refusal line itself
        self.assertIn("helm chat claims", err)
        self.assertIn("helm work list", err)

    def test_a_holder_who_lost_the_token_recovers_it_and_releases(self):
        """The end-to-end strand, closed. The holder never keeps the value the
        claim printed — they read it off `helm work list`, exactly as a seat
        resuming after compaction would, and that string releases the lane."""
        self.claim_lane("stranded", seat=ME)          # value deliberately dropped
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        recovered = [tok.split("lease=")[1].split(" ")[0]
                     for tok in out.split() if tok.startswith("lease=")]
        self.assertEqual(len(recovered), 1, out)
        rc, out, err = self.work("release", "stranded", "--seat", ME,
                                 "--lease", recovered[0])
        self.assertEqual(rc, 0, err)
        self.assertIn("worktree:proj:stranded released", out)
        self.assertEqual(seats.own_leases(), {})

    def test_the_extend_refusal_carries_the_route_too(self):
        seats.claim("port:5", THEM, ttl=600)
        ok, msg, _l = seats.claim("port:5", THEM, ttl=600)   # no token supplied
        self.assertFalse(ok)
        self.assertIn("helm chat claims", msg)


class FramingIsHonestTest(LeaseRecoveryBase):
    """C: the lease is a CONFIRMATION TOKEN, not a capability and not a
    secret. Nothing about this lane improved security — a same-uid seat could
    always read `.claims.json` — so no surface may keep claiming it did."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with open(os.path.join(self.REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def _read_seats_package(self):
        """Every seats*.py, not helm/seats.py. The split moved the release
        path into helm/seats_claims.py and the wording invariant is about the
        SURFACE, never the filename it happened to live in."""
        d = os.path.join(self.REPO, "helm")
        names = sorted(f for f in os.listdir(d)
                       if f == "seats.py" or f.startswith("seats_"))
        self.assertGreaterEqual(len(names), 3, "the seats package scan found "
                                               "almost nothing: %s" % names)
        return "".join(self._read("helm", f) for f in names)

    def test_the_release_path_no_longer_calls_the_lease_a_capability(self):
        src = self._read_seats_package()
        self.assertNotIn("the grant's capability", src)
        self.assertNotIn("release capability", src)
        self.assertIn("CONFIRMATION TOKEN", src)

    def test_the_work_lane_docs_state_what_the_token_actually_proves(self):
        verbs = self._read("docs", "VERBS.md")
        self.assertIn("**lease id is a CONFIRMATION", verbs)
        self.assertIn("not a secret and not a capability", verbs)
        self.assertNotIn("lease id is the capability", verbs)
        # and it says out loud what the token does NOT do
        self.assertIn("It stops no *thief*", verbs)

    def test_the_orca_audit_no_longer_calls_the_nonce_secret(self):
        audit = self._read("docs", "ORCA_SEAM_AUDIT.md")
        self.assertNotIn("secret nonce", audit)
        self.assertIn("confirmation nonce", audit)

    def test_own_leases_documents_that_it_is_not_a_disclosure(self):
        doc = seats.own_leases.__doc__
        self.assertIn("discloses NOTHING", doc)
        self.assertIn("same uid", doc)


# ---------------------------------------------------------------------------
# The stop-guard must never demand an action its target cannot take.
# ---------------------------------------------------------------------------

SESSION = "11111111-2222-3333-4444-555555555555"
DISPATCHER = "dispatcher-seat"
OTHER_SESSION = "77777777-0000-0000-0000-000000000000"


class ActuatorLeaseBase(LeaseRecoveryBase):
    """The auto-claim lane: a dispatch is sent TO this seat by SOMEONE ELSE,
    the actuator claims `dispatch:<id8>` on the seat's behalf at stop time,
    and the stop-guard then addresses the seat about a lease it was handed no
    id for."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"], exist_ok=True)
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
        self.tip = self._sh("git", "rev-parse", "HEAD").stdout.strip()
        self.cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.cwd)
        super().tearDown()

    def dispatch_to(self, seat, lane="some-lane"):
        """-> id8. A DIFFERENT seat hands `seat` a build. Sending (not adding)
        marks delivery observed, which keeps the check-in rung — ranked above
        the actuator — quiet, exactly as a real delivered hand-off does."""
        from helm import dispatches
        os.environ["HELM_CHAT_NAME"] = DISPATCHER
        try:
            row, why, _sent = dispatches.send(
                seat, lane, "please build it", ref=self.tip, repo=self.root,
                kind="build", new_work=True, sign=False)
        finally:
            os.environ["HELM_CHAT_NAME"] = ME
        self.assertIsNotNone(row, why)
        self.assertEqual(row.get("delivery"), "observed")
        return row["id"][:8]

    def autoclaim(self, seat=ME):
        """Drive the REAL actuator after pinning its independent landedness
        seam to proven-absent. These tests own lease mint/release mechanics,
        not the Git predicate that decides whether START is truthful."""
        with mock.patch.object(seats, "_offer_landing_state",
                               return_value=(False, self.tip)):
            return seats._stop_whisper(SESSION, "main", seat, [], False,
                                       cwd=self.root)

    def lease_block(self):
        blocks, _warns = seats.stop_guard(session=SESSION, room="main",
                                          seat=ME, cwd=self.root)
        hit = [b for b in blocks if "claim lease(s) held by this session" in b]
        self.assertEqual(len(hit), 1, blocks)
        return hit[0]

    def claims_file(self):
        from helm import pk
        return pk.read_json(seats.claims_path(), {}) or {}

    def rebind_to_this_session(self, resource):
        """Bind an existing grant to the guard's session, the way a lease
        claimed by this seat in this session already is."""
        from helm import pk
        c = self.claims_file()
        c[resource]["session"] = SESSION
        pk.write_json(seats.claims_path(), c)

    def stored(self, resource):
        return self.claims_file()[resource]

    def run_printed(self, command):
        """Execute the command string the guard printed, through the REAL
        CLIs. Routing on the first two tokens is deliberate: a command naming
        a verb that does not exist fails HERE, which is the whole point of an
        instruction test."""
        import shlex
        parts = shlex.split(command)
        self.assertEqual(parts[0], "helm", command)
        if parts[1] == "chat":
            return self.chat(parts[2], parts[3:])
        self.assertEqual(parts[1], "work", command)
        return self.work(*parts[2:])


class GuardInstructionIsFollowableTest(ActuatorLeaseBase):
    def test_actuator_mints_a_lease_the_seat_is_never_handed(self):
        """THE REPRODUCTION, pinned so the defect cannot come back silently.
        The auto-claim is real, the lease is real, and the line the seat reads
        at mint time does not contain the token it will later be asked for."""
        rid8 = self.dispatch_to(ME)
        line = self.autoclaim()
        self.assertIn("auto-claimed dispatch %s" % rid8, line)
        row = self.stored("dispatch:" + rid8)
        self.assertEqual(row["holder"], ME)
        self.assertNotIn(row["lease"], line)      # never handed over at mint

    def test_the_block_carries_the_real_token_not_a_placeholder(self):
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        lease = self.stored("dispatch:" + rid8)["lease"]
        block = self.lease_block()
        self.assertIn("--lease " + lease, block)
        self.assertNotIn("--lease <id>", block)
        self.assertNotIn("<resource>", block)

    def test_the_printed_command_actually_releases_the_lease(self):
        """The effect, not the absence of a complaint: the exact string the
        guard printed is run through the real CLI and the row is GONE."""
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        block = self.lease_block()
        cmd = block.split("->")[1].strip()
        rc, out, err = self.run_printed(cmd)
        self.assertEqual(rc, 0, err)
        self.assertIn("dispatch:%s released" % rid8, out)
        self.assertNotIn("dispatch:" + rid8, self.claims_file())

    def test_a_lane_lease_is_sent_to_work_release_not_chat_release(self):
        """`helm chat release` would drop a lane lease and leave the worktree
        git-LOCKED and the branch untriaged — followable, and wrong.

        THE ROOM CARRIES A COMMIT FIRST, and that is #112's doing rather
        than a fixture convenience. A lane lease only carries a release
        command when helm MEASURED something in its room; an all-clean room
        now withholds the command entirely, because the four reads cannot
        see a subagent that has not written a file yet and that is the
        first minutes of every build a subagent runs. So the verb question
        this test exists to answer — work release, never chat release — is
        only ASKABLE on a measured room. The withheld case has its own arms
        in StopGuardRoomUnfinishedTest.

        AN UNLANDED COMMIT, not uncommitted dirt: `work release` REFUSES a
        dirty room outright ("two exits, no third"), so dirtying it would
        have made the printed command unrunnable and killed this test's
        real leg — that the command the guard prints actually releases."""
        lease = self.claim_lane("roomy", seat=ME)
        room = os.path.join(self.root + "-wt", "roomy")
        subprocess.run(["git", "-C", room, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "lane work"], check=True, capture_output=True,
                       timeout=30)           # a MEASURED room: helm can speak
        self.rebind_to_this_session("worktree:proj:roomy")
        block = self.lease_block()
        self.assertIn("NOT landed by ancestry or patch identity", block)
        self.assertIn("helm work release roomy --lease " + lease, block)
        self.assertNotIn("helm chat release worktree:proj:roomy", block)
        rc, out, err = self.run_printed(
            block.split("->")[1].strip().split("   —")[0].strip()
            + " --seat " + ME)
        self.assertEqual(rc, 0, err)
        self.assertIn("worktree:proj:roomy released", "\n".join([out, err]))

    def test_the_holder_is_named_when_this_process_answers_to_another_name(self):
        """The guard resolves its seat ROSTER-first (`seat_for_session`) while
        `helm chat claims` hands tokens back OWN-NAME-first (`acting_seat`).
        A session the roster remembers under a stale alias therefore mints
        under that alias and reads an EMPTY lease column — the strand
        reopening by a different door. The block must close it by naming the
        holder, which `claims_list` already publishes."""
        from helm import pk
        alias = "stale-alias"
        rid8 = self.dispatch_to(alias)
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault(alias, {})["session"] = SESSION
        pk.write_json(seats.roster_path(), r)
        guard_seat = seats.seat_for_session(SESSION)
        self.assertEqual(guard_seat, alias)          # diverges from own_name
        self.assertEqual(seats.own_name(), ME)
        self.autoclaim(seat=alias)
        self.assertEqual(seats.own_leases(), {})     # unreachable by name
        block = self.lease_block()
        lease = self.stored("dispatch:" + rid8)["lease"]
        self.assertIn("--lease %s --seat %s" % (lease, alias), block)
        rc, out, err = self.run_printed(block.split("->")[1].strip())
        self.assertEqual(rc, 0, err)
        self.assertIn("dispatch:%s released" % rid8, out)

    def test_seat_is_omitted_when_this_process_is_already_the_holder(self):
        """--seat is noise when the ambient identity already matches, and a
        blanket --seat would teach seats to pass an identity out of argv."""
        self.dispatch_to(ME)
        self.autoclaim()
        self.assertNotIn("--seat", self.lease_block())


class GuardInstructionRefusesTheWrongCallerTest(ActuatorLeaseBase):
    """THE NEGATIVE. Making release POSSIBLE for the true holder must not make
    it possible for anyone else — a release path any seat can invoke against
    any lease is a worse bug than the one being fixed."""

    def test_a_seat_that_does_not_hold_the_lease_is_still_refused(self):
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        res = "dispatch:" + rid8
        lease = self.stored(res)["lease"]
        ok, msg = seats.release(res, THEM, lease=lease, session=SESSION)
        self.assertFalse(ok)
        self.assertIn("the holding seat (%s)" % ME, msg)
        self.assertIn(res, self.claims_file())

    def test_a_wrong_token_from_the_right_seat_is_still_refused(self):
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        res = "dispatch:" + rid8
        ok, msg = seats.release(res, ME, lease="0" * 16, session=SESSION)
        self.assertFalse(ok)
        self.assertIn("the lease id", msg)
        self.assertIn(res, self.claims_file())

    def test_a_foreign_session_cannot_release_the_row(self):
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        res = "dispatch:" + rid8
        lease = self.stored(res)["lease"]
        ok, msg = seats.release(res, ME, lease=lease, session="deadbeef")
        self.assertFalse(ok)
        self.assertIn("the granting session", msg)
        self.assertIn(res, self.claims_file())

    def test_the_second_claimant_is_still_refused_by_name_and_ttl(self):
        """The collision guard itself — the thing that must not weaken."""
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        res = "dispatch:" + rid8
        ok, msg, lease = seats.claim(res, THEM, ttl=600)
        self.assertFalse(ok)
        self.assertIsNone(lease)
        self.assertIn("is held by %s for" % ME, msg)
        self.assertIn("s more", msg)

    def test_the_block_never_shows_a_lease_bound_to_another_session(self):
        """The block publishes tokens ONLY for rows it already matched on this
        session. A row belonging to a different session must stay invisible —
        both its existence and its token."""
        other = seats.claim("port:elsewhere", THEM, ttl=600,
                            session="99999999-0000-0000-0000-000000000000")[2]
        self.dispatch_to(ME)
        self.autoclaim()
        block = self.lease_block()
        self.assertNotIn(other, block)
        self.assertNotIn("port:elsewhere", block)


class GuardInstructionIsSafeToPrintTest(ActuatorLeaseBase):
    def test_a_hostile_resource_key_cannot_reach_the_terminal(self):
        """`claims_list` has scrubbed every published claim field for months;
        this block interpolated the RAW key, so the ONE surface a stopping
        seat always reads was the one that could still clear its terminal."""
        seats.claim("port:evil\x1b[2Jwiped", ME, ttl=600, session=SESSION)
        block = self.lease_block()
        self.assertNotIn("\x1b", block)
        self.assertIn("port:evil", block)

    def test_a_row_with_no_token_says_so_instead_of_inventing_one(self):
        self.assertIsNone(seats.release_hint("port:x", {"holder": ME}))
        from helm import pk
        seats.claim("port:tokenless", ME, ttl=600, session=SESSION)
        c = self.claims_file()
        c["port:tokenless"]["lease"] = ""
        pk.write_json(seats.claims_path(), c)
        block = self.lease_block()
        self.assertIn("records NO lease token", block)
        self.assertNotIn("--lease ", block)

    def test_the_hint_states_that_it_never_weakens_the_binding(self):
        doc = seats.release_hint.__doc__
        self.assertIn("NOTHING HERE WEAKENS THE COLLISION GUARD", doc)
        self.assertIn("_binding_ok", doc)



class HandedLeaseOwnershipTest(ActuatorLeaseBase):
    """THE CLAIM-ON-BEHALF SPLIT: `claim()` writes the RECEIVER's holder and
    the CALLER's session, so a handed lease has its identity split across two
    seats. The stop-guard selected leases on SESSION alone — the field
    `claim()`'s own docstring calls "display / extra binding — never an
    authorizer" — and that broke in BOTH directions at once.

    Measured live 2026-08-01, on the fleet's real gate mutex: gatelock:helm
    recorded holder "opus-integrator" with helm-claude's session, while
    opus-integrator was MID-SUITE under it. helm-claude's stop-guard printed
    `release gatelock:helm --lease <id> --seat opus-integrator` as the way to
    end their turn. A seat obeying its own guard would have killed a running
    whole-suite gate."""

    OTHER = "other-seat"

    def _register(self, seat_name, session):
        """Put a seat in the ROSTER under its own session — what every live
        seat looks like. The live roster carries 286 of these, each with a
        distinct session id, and that is the ONLY thing that distinguishes a
        genuine peer from a name this session claimed under. A fixture that
        skips it does not reproduce claim-on-behalf; it reproduces the alias
        case, which must keep blocking."""
        from helm import pk
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault(seat_name, {})["session"] = session
        pk.write_json(seats.roster_path(), r)

    def _hand(self, resource, holder, session, holder_session=None):
        """A grant whose holder and session can disagree — the shape
        claim-on-behalf produces. `holder_session` registers the holder as a
        real peer; omit it for the no-roster-evidence case."""
        from helm import pk
        if holder_session:
            self._register(holder, holder_session)
        c = pk.read_json(seats.claims_path(), {}) or {}
        c[resource] = {"holder": holder, "session": session,
                       "lease": "deadbeefcafe0001", "fence": 1,
                       "exp_mono": seats._now_mono() + 900,
                       "exp_wall": 0, "ts": "now"}
        pk.write_json(seats.claims_path(), c)

    def _guard(self):
        return seats.stop_guard(session=SESSION, room="main", seat=ME,
                                cwd=self.root)

    def test_the_GIVER_is_not_blocked_and_is_never_told_to_revoke(self):
        """The dangerous arm. Session is mine, holder is someone else."""
        self._hand("gatelock:helm", holder=self.OTHER, session=SESSION,
                   holder_session=OTHER_SESSION)
        blocks, warns = self._guard()
        joined = "\n".join(blocks)
        self.assertNotIn("gatelock:helm", joined)          # never blocks me
        self.assertNotIn("--seat %s" % self.OTHER, joined)  # never the cure
        said = [w for w in warns if "gatelock:helm" in w]
        self.assertEqual(len(said), 1, warns)              # said, not silent
        self.assertIn(self.OTHER, said[0])
        self.assertIn("not yours to release", said[0])

    def test_the_RECEIVER_of_a_handoff_is_blocked_by_their_own_guard(self):
        """THE HALF I SAID WAS UNFIXABLE, and @kimi refuted me on review.

        I compared seat->session — does the HOLDER's roster row name my
        session — and concluded the row could not tell a peer's hand-off from
        another session of my own seat. kimi tried the INVERSE: the roster
        maps session->seat, so `row.session` has exactly ONE owning seat
        (measured live: 292 seats carry a session, ZERO sessions are claimed
        by two). Owner != holder PROVES a hand-off, from the row, with no new
        field and no #66 dependency.

        Demonstrated live on me at 05:55: gatelock:helm read holder
        helm-claude-2 / session cf8ce076 (helm-claude's) while my own suite
        ran under it, and my guard listed one lease and not that one."""
        self._register(self.OTHER, OTHER_SESSION)
        self._hand("gatelock:helm", holder=ME, session=OTHER_SESSION)
        blocks, _warns = self._guard()
        hit = [b for b in blocks if "gatelock:helm" in b]
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("--lease deadbeefcafe0001", hit[0])
        # COMPOSITION with the renewing-lease hint: a handed gatelock's
        # renewer keeps it above the near-full-TTL floor forever, so the
        # renewed-check (scoped to leases THIS session minted) must never
        # swallow the receiver's release command — it did on the first
        # whole-suite gate of that lane, this exact test red.
        self.assertNotIn("RENEWED", hit[0])

    def test_my_own_other_session_is_NOT_a_handoff_and_still_passes(self):
        """THE CONTROL THAT MAKES THE ARM ABOVE SAFE, and the reason a bare
        holder comparison could never work: a lease MY OWN seat minted under
        a different session is not this turn's obligation.
        WorkOfferTest::test_new_head_after_a_take_offers_once pins it, and it
        went red when I tried to block on holder alone."""
        self._register(ME, "12121212-0000-0000-0000-000000000000")
        self._hand("dispatch:mine", holder=ME,
                   session="12121212-0000-0000-0000-000000000000")
        blocks, warns = self._guard()
        self.assertNotIn("dispatch:mine", "\n".join(blocks + warns))

    def test_an_ordinary_self_claimed_lease_still_blocks(self):
        """POSITIVE CONTROL. Without this the fix could be an always-allow and
        every arm above would still pass."""
        self._hand("worktree:helm:mine", holder=ME, session=SESSION)
        blocks, _warns = self._guard()
        hit = [b for b in blocks if "worktree:helm:mine" in b]
        self.assertEqual(len(hit), 1, blocks)

    def test_a_lease_belonging_to_neither_is_not_mentioned_at_all(self):
        """NEGATIVE CONTROL: the fix must not widen the guard into reporting
        the whole fleet's claims at every stop."""
        self._hand("worktree:helm:theirs", holder=self.OTHER,
                   session="88888888-0000-0000-0000-000000000000",
                   holder_session=OTHER_SESSION)
        blocks, warns = self._guard()
        self.assertNotIn("worktree:helm:theirs", "\n".join(blocks + warns))

    def test_an_unreadable_roster_is_UNKNOWN_and_offers_no_command(self):
        """The uncertainty arm. With the roster unreadable, an alias of MYSELF
        and a genuinely foreign holder are indistinguishable — and they want
        opposite actions. Allowing a stop on a lease that was ours costs a
        lease held a little longer; printing `release --seat <holder>` on one
        that was NOT ours kills another seat's live gate. So: UNKNOWN, allow,
        and offer nothing to run.

        The seam is NARROW on purpose: pk.read_json also reads the claims file
        this guard depends on, so a blanket mock breaks the guard before it
        reaches the branch under test and the arm skips instead of proving
        anything. Fail ONLY the roster read."""
        from unittest import mock
        self._hand("gatelock:helm", holder=self.OTHER, session=SESSION,
                   holder_session=OTHER_SESSION)
        roster = seats.roster_path()
        real = seats.pk.read_json

        def only_roster_fails(path, default=None, *a, **kw):
            if str(path) == str(roster):
                raise OSError("roster unreadable")
            return real(path, default, *a, **kw)

        # The INBOX rung reads the roster far upstream (stop_guard ->
        # _pending_all -> seat_scope -> roster), so with it on, an unreadable
        # roster kills the guard before the claims loop is ever reached and
        # this arm would prove nothing. HELM_STOP_GUARD_INBOX=0 is a
        # documented kill switch and is exactly the configuration in which the
        # branch under test is reachable.
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"
        try:
            with mock.patch.object(seats.pk, "read_json",
                                   side_effect=only_roster_fails):
                blocks, warns = self._guard()
        finally:
            os.environ.pop("HELM_STOP_GUARD_INBOX", None)
        said = [w for w in warns if "gatelock:helm" in w]
        self.assertEqual(len(said), 1, warns)
        self.assertIn("UNKNOWN", said[0])
        self.assertNotIn("--lease", said[0])
        self.assertNotIn("gatelock:helm", "\n".join(blocks))


if __name__ == "__main__":
    unittest.main()
