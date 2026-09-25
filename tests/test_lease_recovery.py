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
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _release  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests._tmphome import corroborate as _tmp_corroborate  # noqa: E402
from tests._tmphome import session_for as _tmp_session_for  # noqa: E402

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
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
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
        # THIS FIXTURE'S OWN PROJECT IS ITS TEMP REPO. Every dispatch these
        # arms write binds self.root, and the write door refuses a ref whose
        # repository is not this project's — so without the pin `dispatch_to`
        # returns a TRUE refusal that says nothing about leases, which is what
        # the whole class is actually about.
        from tests._tmphome import pin_dispatch_home
        self._real_home_repo_id = pin_dispatch_home(self, self.root)
        # THE STOP GUARD READS A RESIDENT'S FACTS; this stands in one that is
        # exactly up to date at every stop (tests/_stopfacts.py).
        from tests._stopfacts import always_fresh
        self.fresh_resident = always_fresh(self)

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
        # See tests/_tmphome.declaring: a lane lease requires a declared or
        # rostered actor, and a hermetic fixture is otherwise DERIVED.
        with _tmp_declaring(args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
        # EXPIRED AT BIRTH through the lane door itself: the CLI refuses a
        # zero --ttl, because a caller typing it would hold nothing.
        rc, line = work.claim(self.root, "brief", ME, ttl=0)
        self.assertEqual(rc, 0, line)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARDED brief", out)
        self.assertNotIn("(yours)", out)
        self.assertIsNone({r["lane"]: r for r in
                           work.list_rows(self.root)}["brief"]["lease"])


class ChatClaimTtlTest(LeaseRecoveryBase):
    """`helm chat claim --ttl` reads the forms `helm work claim` reads, through
    the one reader both verbs share: seconds, bare or with ONE unit suffix.

    THE SAME CRASH, ONE VERB OVER: converting the TTL with a bare `int()`
    raises ValueError on `4h`, and the traceback is the verb's whole answer.
    The accepting arms read the stored lease as well as the printed line, since
    the printed length is the verb echoing its own variable."""

    RES = "port:ttl"

    def claim(self, *args):
        argv = [self.RES, "--seat", ME] + list(args)
        with _tmp_declaring(argv):
            return self.chat("claim", argv)

    def stored_left(self):
        with open(seats.claims_path(), encoding="utf-8") as f:
            return json.load(f)[self.RES]["exp_wall"] - time.time()

    def test_4h_is_four_hours(self):  # noqa: VACUOUS_ASSERTION — the printed length and the stored seconds left are both pinned by exact values
        rc, out, err = self.claim("--ttl", "4h")
        self.assertEqual(0, rc, err)
        self.assertIn("for 14400s", out)
        self.assertAlmostEqual(14400, self.stored_left(), delta=120)

    def test_a_bare_number_is_still_seconds(self):  # noqa: VACUOUS_ASSERTION — the printed length and the stored seconds left are both pinned by exact values
        """CONTROL: the spelling that always worked keeps its meaning."""
        rc, out, err = self.claim("--ttl", "3600")
        self.assertEqual(0, rc, err)
        self.assertIn("for 3600s", out)
        self.assertAlmostEqual(3600, self.stored_left(), delta=120)

    def test_4x_is_refused_rc_2_with_the_forms_on_one_line(self):  # noqa: VACUOUS_ASSERTION — no lease row is the product law; the refusal text is the positive control on the same call
        rc, out, err = self.claim("--ttl", "4x")
        self.assertEqual(2, rc, err)
        self.assertEqual("", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(1, len(err.strip().splitlines()),
                         "the refusal is not ONE stderr line: %r" % err)
        self.assertIn("--ttl '4x'", err)
        self.assertIn("s, m, h or d", err)
        for example in ("3600s", "90m", "4h", "1d"):
            self.assertIn(example, err)
        self.assertNotIn(self.RES, [c["resource"] for c in seats.claims_list()])


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
        omitted = _release.omitted("docs/ORCA_SEAM_AUDIT.md")
        if omitted:
            self.skipTest(omitted)
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
        # AND THE ROSTER MUST AGREE. Exporting a session and a name is half an
        # identity: what makes the pair evidence is a roster row resolving
        # SESSION back to ME, and every act door below (auto-claim, release)
        # now requires it. The undo runs with the fixture so no arm inherits a
        # join it did not make.
        self.addCleanup(_tmp_corroborate(ME, SESSION))
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
        # A DIFFERENT SEAT IS A DIFFERENT SESSION TOO. Swapping only the name
        # while keeping ME's session exported does not model another seat — it
        # models a DISPUTE, which the identity layer refuses, and the arms
        # below would then be measuring that refusal instead of the hand-off.
        dispatcher_sid = _tmp_session_for(DISPATCHER)
        undo = _tmp_corroborate(DISPATCHER, dispatcher_sid)
        os.environ["HELM_CHAT_NAME"] = DISPATCHER
        os.environ["CLAUDE_CODE_SESSION_ID"] = dispatcher_sid
        try:
            row, why, _sent = dispatches.send(
                seat, lane, "please build it", ref=self.tip, repo=self.root,
                kind="build", new_work=True, sign=False)
        finally:
            os.environ["HELM_CHAT_NAME"] = ME
            os.environ["CLAUDE_CODE_SESSION_ID"] = SESSION
            undo()
        self.assertIsNotNone(row, why)
        self.assertEqual(row.get("delivery"), "observed")
        return row["id"][:8]

    def autoclaim(self, seat=ME):
        """Drive the REAL actuator after pinning its independent landedness
        seam to proven-absent. These tests own lease mint/release mechanics,
        not the Git predicate that decides whether START is truthful.

        THE ACTOR IS PART OF THE ACTUATOR'S SIGNATURE NOW (task/994). Taking a
        lease requires an AdmittedActor, and `stop_guard` resolves one before
        calling this — so a fixture that calls `_stop_whisper` directly has to
        resolve one too, or it is driving a different function than production
        drives. The assertion on the resolution is deliberate: if this fixture
        ever stops having an admissible identity, that must fail here and say
        so, not silently stop minting and leave the arms below asserting about
        an actuator that never ran."""
        from helm import actors
        actor, err = actors.resolve_actor(SESSION, self.root,
                                          act="auto-claim work")
        # NOT asserted: one arm below deliberately stages a DISPUTED identity,
        # where the correct actor is None and the correct outcome is no lease.
        # Asserting admission here would have made that arm unwritable.
        with mock.patch.object(seats, "_offer_landing_state",
                               return_value=(False, self.tip)):
            return seats._stop_whisper(SESSION, "main", seat, [], False,
                                       cwd=self.root, actor=actor)

    def lease_block(self, detail=False):
        """The LEASE rung's block, selected by PROPERTY, never by a header
        literal: a header is prose and prose is a per-lane decision (the
        stop-hook concision lane rewrote it; the wording is not the
        contract). What makes a block the LEASE block is that it carries a
        per-lease line — a release command or the token-absence sentence —
        which is the thing these arms exist to pin, and the shape no other
        rung's block can wear. A block that shows the SAME claims without
        that line fails the count below as loudly as a missing one.

        A LANE LINE WEARS NEITHER SHAPE ANY MORE: no room read earns a lane
        release command, so its line is advice alone. An arm about a lane
        lease therefore holds a non-room lease in the same stop, which is
        also its positive control. `detail` is the long form
        `helm chat stop-guard --detail` prints — rendering only.
        """
        blocks, _warns = seats.stop_guard(session=SESSION, room="main",
                                          seat=ME, cwd=self.root,
                                          detail=detail)
        hit = [b for b in blocks if self._is_lease_block(b)]
        self.assertEqual(len(hit), 1, blocks)
        return hit[0]

    @staticmethod
    def _is_lease_block(block):
        # A lease line is `  <resource> <ttl> — <command-or-absence>`. The
        # separator and the two right-hand shapes are the contract; the
        # header above them is not named anywhere here.
        for line in block.splitlines():
            if "—" not in line:
                continue
            tail = line.split("—", 1)[1]
            if "--lease " in tail or "no lease token on this row" in tail:
                return True
        return False

    @staticmethod
    def _printed_command(block, resource):
        """The command the block prints for one resource, split off its
        advice WITHOUT pinning the framing around it. The contract is the
        pair the renderer publishes — a line naming the resource, and on it,
        after the em-dash, a runnable command — not the arrow, spacing or
        trailing-marker shape any one rendering chose. What is extracted is
        handed to run_printed, whose helm-verb routing is the loud half: a
        split that picked up advice fails there on a verb that does not
        exist."""
        import re
        for line in block.splitlines():
            if resource in line and "—" in line:
                tail = line.split("—", 1)[1].strip()
                m = re.match(r"(helm\s+\S+.*?)(?:\s+—\s+.*)?$", tail)
                return m.group(1)
        raise AssertionError("no line for %s in:\n%s" % (resource, block))

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

    def test_the_printed_command_actually_releases_the_lease(self):  # noqa: VACUOUS_ASSERTION — `before` is an unconditional positive on the same ledger the assertNotIn reads: the claim provably existed, so its absence afterwards is a release and not an empty fixture
        """The effect, not the absence of a complaint: the exact string the
        guard printed is run through the real CLI and the row is GONE."""
        rid8 = self.dispatch_to(ME)
        self.autoclaim()
        block = self.lease_block()
        # THE LINE CARRIES A COMMAND AND THEN ITS ADVICE: a dispatch claim
        # states what the ROW became, and the sentence RIDES the command
        # rather than replacing it, because releasing the claim strands
        # nothing while this line is the only printing of the lease token.
        # (A lane lease's advice REPLACES its command instead — no room read
        # earns one — so this is now the one advice-bearing command line.)
        cmd = self._printed_command(block, "dispatch:" + rid8)
        # MUST-HIT, wording-free: the line must carry ADVICE AFTER the
        # command (a second em-dash segment), or the split this arm rehearses
        # is against a line with nothing to split off.
        line = [l for l in block.splitlines() if "dispatch:" + rid8 in l][0]
        self.assertGreaterEqual(line.count("—"), 2,
                                "no advice rides the command: " + line)
        before = self.claims_file()
        rc, out, err = self.run_printed(cmd)
        self.assertEqual(rc, 0, err)
        self.assertIn("dispatch:%s released" % rid8, out)
        # UNCONDITIONAL POSITIVE ON THE SAME LEDGER the assertNotIn reads:
        # the claim WAS there before the printed command ran, so an absent
        # row cannot be mistaken for a release that never happened.
        self.assertIn("dispatch:" + rid8, before,
                      "MUST-HIT: the claim existed before the release")
        self.assertNotIn("dispatch:" + rid8, self.claims_file())

    def test_a_lane_lease_is_sent_to_the_work_door_never_chat_release(self):  # noqa: VACUOUS_ASSERTION — each absence reads a line or block whose same-call positive is asserted first: the lane line must be FOUND carrying its unfinished-work claim, and the port line in the SAME block must print `helm chat release port:9 --lease <tok>`, so the chat verb and the flag provably render there and their absence from the lane line is routing, not a mute sermon
        """`helm chat release` would drop a lane lease and leave the worktree
        git-LOCKED and the branch untriaged — followable, and wrong. WHICH
        DOOR a lane lease is sent to is the property this arm owns.

        WHETHER a command is printed is a different property, and for a lane
        the answer is never: the four room reads see artefacts, never
        presence, so no branch can justify a release command. So the verb is
        asked where the guard still names a door for a lane — the long form
        points at `helm work list`, the census that carries this lane's token
        — and the lane line is held to its withholding contract beside it: it
        carries its unfinished-work claim and no `--lease`. The whether-leg
        has its own arms in StopGuardRoomUnfinishedTest.

        THE ROOM CARRIES AN UNLANDED COMMIT, so the reads find work and the
        findings branch speaks; it is the only branch that names a door. Not
        uncommitted dirt: `work release` REFUSES a dirty room outright, and
        the last leg follows the pointer through the real door.

        A `port:` LEASE RIDES THE SAME STOP as the control. It strands
        nothing, so it keeps its exact `helm chat release` command, which
        puts the chat verb and the `--lease` flag in the very block whose
        lane line must carry neither."""
        import re
        lease = self.claim_lane("roomy", seat=ME)
        room = os.path.join(self.root + "-wt", "roomy")
        subprocess.run(["git", "-C", room, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "lane work"], check=True, capture_output=True,
                       timeout=30)           # a MEASURED room: helm can speak
        self.rebind_to_this_session("worktree:proj:roomy")
        ok, msg, port_lease = seats.claim("port:9", ME, ttl=600,
                                          session=SESSION)
        self.assertTrue(ok, msg)
        verb = re.compile(r"helm\s+([a-z][a-z-]*)\s+([a-z][a-z-]*)")

        # THE STOP A SEAT READS. The lane line names what is bound and no
        # command; the chat verb in this block belongs to the port lease.
        brief = self.lease_block()
        port_b = [l for l in brief.splitlines() if "port:9" in l]
        lane_b = [l for l in brief.splitlines() if "worktree:proj:roomy" in l]
        self.assertEqual((len(port_b), len(lane_b)), (1, 1), brief)
        self.assertIn("helm chat release port:9 --lease " + port_lease,
                      port_b[0])
        self.assertRegex(lane_b[0].lower(), r"not landed|unfinished|unproven")
        self.assertNotIn("--lease", lane_b[0])
        self.assertNotIn("helm chat", lane_b[0])

        # THE LONG FORM, where the door is named. THE VERB IS THE CONTRACT,
        # not the sentence around it: every `helm <family> <verb>` the lane
        # line names is read out, and the same extractor must SEE `chat
        # release` on the port line of this block, or a lane line that named
        # no verb at all would pass the family check below.
        full = self.lease_block(detail=True)
        port_f = [l for l in full.splitlines() if "port:9" in l]
        lane_f = [l for l in full.splitlines() if "worktree:proj:roomy" in l]
        self.assertEqual((len(port_f), len(lane_f)), (1, 1), full)
        self.assertIn(("chat", "release"), verb.findall(port_f[0]))
        named = verb.findall(lane_f[0])
        self.assertIn(("work", "list"), named, lane_f[0])
        self.assertNotIn("chat", [family for family, _v in named], lane_f[0])
        self.assertRegex(lane_f[0].lower(), r"not landed|unfinished|unproven")
        self.assertNotIn("--lease", lane_f[0])
        self.assertNotIn("helm chat release worktree:proj:roomy", full)

        # FOLLOW THE POINTER through the real CLIs. The census the line names
        # must carry THIS lane's token, and that token must discharge the
        # lease at the work door — which releases a lane in exactly this
        # unlanded shape and keeps its room and branch. The test composes the
        # release itself, because the guard deliberately stops at the census.
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        rows = [l for l in out.splitlines() if " roomy " in l and "lease=" in l]
        self.assertEqual(len(rows), 1, out)
        carried = rows[0].split("lease=", 1)[1].split()[0]
        self.assertEqual(carried, lease)
        rc, out, err = self.work("release", "roomy", "--seat", ME,
                                 "--lease", carried)
        self.assertEqual(rc, 0, err)
        self.assertIn("worktree:proj:roomy released", "\n".join([out, err]))

    def test_the_holder_is_named_when_this_process_answers_to_another_name(self):  # noqa: VACUOUS_ASSERTION — the planted claim and the lease_block/run_printed assertions below are unconditional positive controls on the same ledger the assertIsNone reads
        """The guard resolves its seat ROSTER-first (`seat_for_session`) while
        `helm chat claims` hands tokens back OWN-NAME-first (`acting_seat`).
        A session the roster remembers under a stale alias therefore mints
        under that alias and reads an EMPTY lease column — the strand
        reopening by a different door. The block must close it by naming the
        holder, which `claims_list` already publishes."""
        from helm import pk
        alias = "stale-alias"
        # THE SHAPE IS PLANTED BEFORE THE HAND-OFF, and both halves are load-
        # bearing. A recipient with no roster row is not addressable at all —
        # the dispatch door refuses it before this arm's subject is reached —
        # and the session must be remembered under the ALIAS and NOT under ME,
        # or it resolves to two seats and `seat_for_session` reports the
        # ambiguity instead of the alias. Together they are the state this arm
        # is about: a session the roster knows by a name this process no longer
        # answers to.
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault(alias, {})["session"] = SESSION
        if r.get(ME, {}).get("session") == SESSION:
            r[ME].pop("session", None)
        pk.write_json(seats.roster_path(), r)
        rid8 = self.dispatch_to(alias)
        guard_seat = seats.seat_for_session(SESSION)
        self.assertEqual(guard_seat, alias)          # diverges from own_name
        self.assertEqual(seats.own_name(), ME)
        # THE ACTUATOR NO LONGER MINTS INTO THIS STATE — a lease is an act, and
        # a disputed identity has no admitted actor, so auto-claim declines.
        self.assertIsNone(self.autoclaim(seat=alias),
                          "a disputed identity must not mint a lease")
        # But a lease minted BEFORE that law still exists on live boxes, and
        # recovery is precisely about those: plant the shape a pre-fix seat
        # left behind and prove the surface still names its holder.
        ok, _m, _l = seats.claim("dispatch:" + rid8, alias, session=SESSION)
        self.assertTrue(ok)
        self.assertEqual(seats.own_leases(), {})     # unreachable by name
        block = self.lease_block()
        lease = self.stored("dispatch:" + rid8)["lease"]
        self.assertIn("--lease %s --seat %s" % (lease, alias), block)
        rc, out, err = self.run_printed(
            self._printed_command(block, "dispatch:" + rid8))
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

    def test_a_row_with_no_token_says_so_instead_of_inventing_one(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the same observable is two lines down: the row's own line must be FOUND (len == 1) before any absence on it is read, so a block that never rendered the row reddens the count, not the assertNotIn
        self.assertIsNone(seats.release_hint("port:x", {"holder": ME}))
        from helm import pk
        seats.claim("port:tokenless", ME, ttl=600, session=SESSION)
        c = self.claims_file()
        c["port:tokenless"]["lease"] = ""
        pk.write_json(seats.claims_path(), c)
        block = self.lease_block()
        # PROPERTY, NOT SPELLING: the row must SAY the token is absent (the
        # selector's absence-branch must have fired for this resource), and
        # nothing may invent a command for it. The sentence's wording is the
        # lane's to choose; "no lease token" is the phrase the renderer uses
        # today and is matched as a fragment, not a sentence.
        lines = [l for l in block.splitlines() if "port:tokenless" in l]
        self.assertEqual(len(lines), 1, block)
        self.assertIn("no lease token", lines[0])
        self.assertNotIn("--lease ", block)

    def test_a_metacharacter_lane_pastes_as_one_argument(self):
        """The hint is copy-pasteable BY CONTRACT, and scrub+clip never made
        a hostile lane name paste-safe: unquoted, `lane;rm -rf ~` pastes as
        a second command. Every interpolated value is quoted at the single
        authority; a clean value stays bare (the quoting must not turn the
        common hint into noise)."""
        import shlex
        hint = seats.release_hint(
            "worktree:helm:evil lane;echo pwned",
            {"lease": "aa11", "holder": ME})
        self.assertIsNotNone(hint)
        toks = shlex.split(hint)
        self.assertEqual(toks[:3], ["helm", "work", "release"])
        self.assertIn("evil lane;echo pwned", toks)   # ONE argv token
        clean = seats.release_hint(
            "worktree:helm:plain-lane", {"lease": "aa11", "holder": ME})
        self.assertIn(" plain-lane ", clean + " ")    # bare, unquoted

    def test_a_hostile_holder_and_resource_quote_too(self):
        import shlex
        hint = seats.release_hint(
            "port:has space$(boom)",
            {"lease": "bb22", "holder": "who ami"})
        toks = shlex.split(hint)
        self.assertIn("port:has space$(boom)", toks)
        self.assertIn("who ami", toks)

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
        """THE HALF I SAID WAS UNFIXABLE, and a review refuted me.

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

    def test_multiple_claims_share_one_roster_snapshot(self):
        """Claim count may not multiply full roster parses."""
        # THE DOUBLE GOES WHERE THE CODE READS THE NAME. The claims rung
        # left _stop_guard for seats_stop_claims and took
        # roster_acquired with it, so a double planted on the old
        # module is never consulted — patch.object would raise, and a
        # patch that did NOT raise would be worse: a spy nothing calls
        # proves an absence rather than a behaviour.
        from helm import seats_stop_claims as claims_impl
        for i in range(4):
            self._hand("gatelock:helm:%d" % i, holder=self.OTHER,
                       session=SESSION, holder_session=OTHER_SESSION)
        real = claims_impl.roster_acquired
        with mock.patch.object(claims_impl, "roster_acquired",
                               wraps=real) as roster_read, \
                mock.patch.dict(os.environ, {"HELM_STOP_GUARD_NDP": "0"}):
            blocks, warns = self._guard()
        roster_read.assert_called_once_with()
        self.assertFalse(blocks, blocks)
        self.assertEqual(len([w for w in warns if "not yours to release" in w]),
                         4, warns)

    def test_ambiguous_snapshot_keeps_current_first_and_warns_once(self):
        """Claims reuse canonical ordering and its loud ambiguity verdict."""
        from helm import pk, seats_roster
        # THE DOUBLE GOES WHERE THE CODE READS THE NAME. The claims rung
        # left _stop_guard for seats_stop_claims and took
        # roster_acquired with it, so a double planted on the old
        # module is never consulted — patch.object would raise, and a
        # patch that did NOT raise would be worse: a spy nothing calls
        # proves an absence rather than a behaviour.
        from helm import seats_stop_claims as claims_impl
        pk.write_json(seats.roster_path(), {
            "z-current": {"session": SESSION},
            "a-current": {"session": SESSION},
            "0-history": {"session": "historical-now",
                          "sessions": [SESSION]},
            "\u202ehistory-hostile": {"session": "hostile-now",
                                        "sessions": [SESSION]},
            self.OTHER: {"session": OTHER_SESSION},
        })
        self._hand("gatelock:helm", holder=self.OTHER, session=SESSION)
        real = claims_impl.roster_acquired
        with mock.patch.object(claims_impl, "roster_acquired",
                               wraps=real) as roster_read, \
                mock.patch.object(seats_roster, "_warn_once") as warnings:
            blocks, warns = self._guard()
        roster_read.assert_called_once_with()
        self.assertTrue(warnings.call_args_list,
                        "the ambiguous snapshot never reached its warning")
        keys = {call.args[0] for call in warnings.call_args_list}
        lines = {call.args[1] for call in warnings.call_args_list}
        self.assertEqual(keys, {"ambiguous-session:%s" % SESSION})
        self.assertEqual(len(lines), 1, warnings.call_args_list)
        line = lines.pop()
        self.assertLess(line.index("'a-current'"), line.index("'z-current'"))
        self.assertLess(line.index("'z-current'"), line.index("'0-history'"))
        self.assertIn("'history-hostile'", line)
        self.assertNotIn("\u202e", line)
        self.assertIn("resolving to 'a-current'", line)
        self.assertFalse(blocks, blocks)
        self.assertEqual(len([w for w in warns if "not yours to release" in w]),
                         1, warns)

    def test_an_unreadable_roster_is_UNKNOWN_and_offers_no_command(self):
        """Unreadable ownership stays UNKNOWN and never suggests revocation."""
        # THE DOUBLE GOES WHERE THE CODE READS THE NAME. The claims rung
        # left _stop_guard for seats_stop_claims and took
        # roster_acquired with it, so a double planted on the old
        # module is never consulted — patch.object would raise, and a
        # patch that did NOT raise would be worse: a spy nothing calls
        # proves an absence rather than a behaviour.
        from helm import seats_stop_claims as claims_impl
        self._hand("gatelock:helm", holder=self.OTHER, session=SESSION,
                   holder_session=OTHER_SESSION)

        # The INBOX rung reads the roster far upstream (stop_guard ->
        # _pending_all -> seat_scope -> roster), so switch it off to prove the
        # claims rung's own tri-state snapshot rather than an earlier failure.
        os.environ["HELM_STOP_GUARD_INBOX"] = "0"
        try:
            with mock.patch.object(claims_impl, "roster_acquired",
                                   return_value=({}, True)) as roster_read:
                blocks, warns = self._guard()
        finally:
            os.environ.pop("HELM_STOP_GUARD_INBOX", None)
        roster_read.assert_called_once_with()
        said = [w for w in warns if "gatelock:helm" in w]
        self.assertEqual(len(said), 1, warns)
        self.assertIn("UNKNOWN", said[0])
        self.assertNotIn("--lease", said[0])
        self.assertNotIn("gatelock:helm", "\n".join(blocks))


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def tearDownModule():
    """A stop that armed a surviving disclosure and never emitted it leaves
    the text queued for whatever refuses next in this process, which is
    another module's stop; drain it the way an interrupted response does
    (task/3039: the slice runner's data audit named it)."""
    from helm import seats_stop_seam
    seats_stop_seam.fallback_lines(())


if __name__ == "__main__":
    unittest.main()
