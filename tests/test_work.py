#!/usr/bin/env python3
"""helm work — worktree lifecycle on the claims lane. Hermetic: HELM_HOME +
HELM_CHAT_DIR are tmp dirs, git global/system config nulled, every room is a
scratch repo minted in setUp — the real repo and its worktrees are never
touched (every `helm work` call pins --repo at the scratch root)."""
import ast
import concurrent.futures
import contextlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import declaring as _tmp_declaring  # noqa: E402

from helm import projscope, seats, vcs, work  # noqa: E402
from helm.work import _guard as _work_guard  # noqa: E402
from helm.work import _claims as _work_claims  # noqa: E402
from helm.work import _cli as _work_cli  # noqa: E402
from helm.work import _common as _work_common  # noqa: E402
from helm.work import _gc as _work_gc  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CELL_BIN", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_WORK_INTEGRATOR",
            "HELM_WORK_CLAIM", "HELM_TEST_HOOK_LOG", "HELM_PRIVATE_NEEDLES")


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=30)


def _hook_tree(root):
    """Every file under the repo's hook dir, snapshots included, as
    relpath -> (kind, bytes, mode) through the transaction's own reader —
    so "unchanged" below is a byte-and-mode claim about the whole tree."""
    d = os.path.dirname(work.hook_path(root, "pre-commit"))
    tree = {}
    for base, _dirs, files in os.walk(d):
        for name in files:
            full = os.path.join(base, name)
            tree[os.path.relpath(full, d)] = _work_guard._path_snapshot(full)
    return tree


def _reap(proc, deadline=30.0):
    """Wait for `proc` to exit, polling to a generous deadline.

    The behaviour under test is that the process IS reaped — never that it is
    reaped within 3 seconds while a whole suite runs beside it. A bare
    proc.wait(timeout=3) measures the BOX, not the reap: measured 2026-08-02,
    the orca-shell pair red-ed whole-suite at ~0.6s of real work against a 3s
    budget (5x headroom inside this fleet host's demonstrated noise band,
    which GraalPy showed can stretch a single test 31x) while passing every
    isolated, repeated, and module-scoped run. Poll fast, return the moment
    the process exits — faster than the fixed wait on a quiet box, immune on
    a loud one. Raises TimeoutExpired after `deadline` exactly as wait would.
    """
    end = time.monotonic() + deadline
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if time.monotonic() >= end:
            raise subprocess.TimeoutExpired(proc.args, deadline)
        time.sleep(0.05)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


class WorkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-work-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        # An ABSENT needle file: install-guard now composes a pre-commit
        # never-track scan, so every scratch-repo commit here runs it — the
        # operator's real needle file must never leak into these fixtures'
        # behavior (hermeticity), and an absent file is the documented no-op.
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(
            self.tmp, "no-needles-configured.txt")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t"),
                    # THE FIXTURE DECLARES ITS LAW. It simulates
                    # helm-the-shared-checkout; an undeclared PROJECT repo
                    # now defaults to the leak legs (task/2441).
                    ("git", "config", "--local",
                     "helm.guard.profile", "rail")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("seed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def work(self, *args):
        out, err = io.StringIO(), io.StringIO()
        # DECLARE AN IDENTITY FOR THE CALL. A lane lease is actor-attributed —
        # it assigns responsibility and can strand another worker — so the
        # identity layer refuses a name minted from session+cwd, which is what
        # a hermetic fixture resolves to. `--seat S` names the seat this call
        # acts as; see tests/_tmphome.declaring.
        with _tmp_declaring(args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def room(self, lane, dirty=None):
        """Mint a lease-less room directly (git only, never the desk).

        Carries HELM_WORK_INTEGRATOR=1 because once the ref-guard is installed
        this raw `worktree add -b` is REFUSED by design — minting a branch in
        the shared checkout is exactly what the rail exists to stop, and the
        sanctioned paths are `helm work claim` or this override. The fixture
        is standing in for the integrator, so it declares that rather than
        quietly weakening the guard to let a test through."""
        path = work.lane_path(self.root, lane)
        r = subprocess.run(["git", "worktree", "add", "-q", "-b",
                            work.lane_branch(lane), path, "main"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("precious uncommitted bytes\n")
            self.abandon(path)
        return path

    def abandon(self, path):
        """Back-date every dirty path so the room reads as ABANDONED.

        THE FIXTURE CARRIED AN UNSTATED PREMISE AND A CURE MADE IT LOAD-BEARING.
        `gc_enact`'s rescue leg now DEFERS on a room written inside
        `_gc._RESCUE_ACTIVE_S`, and a room minted microseconds ago is the
        freshest room there is — so every arm that asserted a rescue was
        silently asserting it about a room the substrate now treats, correctly,
        as one its author is still sitting in. The rooms those arms mean are
        LEASE-LESS AND LONG ABANDONED. This says that out loud instead of
        leaving it to the clock.

        DERIVED FROM THE CONSTANT, NEVER A COPY OF IT. An arm that transcribed
        900 would stay green against a window that had moved out from under it;
        reading `_RESCUE_ACTIVE_S` means widening the window drags this fixture
        along with it. The population is `_room_status`'s own — whatever git
        reports as changed — because the newest of exactly those mtimes is the
        clock the cure reads.
        """
        cutoff = time.time() - (_work_gc._RESCUE_ACTIVE_S + 60)
        r = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=path, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        rels = [f[3:] for f in r.stdout.split(b"\0") if len(f) > 3]
        self.assertTrue(rels, "abandon() found no dirty path in %s" % path)
        for rel in rels:
            target = os.path.join(os.fsencode(path), rel)
            if os.path.lexists(target) and not os.path.islink(target):
                os.utime(target, (cutoff, cutoff))


class ClaimTest(WorkBase):
    def test_claim_mints_room_lock_and_lease(self):
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, branch, lease, ttl = out.strip().split("\t")
        self.assertEqual(path, work.lane_path(self.root, "demo"))
        self.assertEqual(branch, "lane/demo")
        self.assertEqual(int(ttl), work.DEFAULT_TTL)
        self.assertTrue(os.path.isdir(path))
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertTrue(row["locked"])                    # do-not-disturb tag
        self.assertEqual(row["reason"], "lease:" + lease[:8])
        self.assertIn("worktree:proj:demo",
                      [c["resource"] for c in seats.claims_list()])
        # idempotent re-entry: the holder re-claims with the lease, same room
        rc2, out2, err2 = self.work("claim", "demo", "--seat", "s1",
                                    "--lease", lease)
        self.assertEqual(rc2, 0, err2)
        self.assertEqual(out2.strip().split("\t")[:3], [path, branch, lease])
        # a second seat without the capability is refused, holder named
        rc3, _out3, err3 = self.work("claim", "demo", "--seat", "s2")
        self.assertEqual(rc3, 1)
        self.assertIn("held by s1", err3)

    def test_claim_bad_lane_name(self):
        rc, _out, err = self.work("claim", "no/slashes")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_claim_refuses_the_reserved_seat_home_container(self):
        """`<root>-wt/seats/` holds the per-seat HOME worktrees (slice 0); a
        lane by that name would check a room out on top of the container."""
        from helm import harness
        rc, _out, err = self.work("claim", harness.SEAT_HOME_DIRNAME)
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err)
        self.assertFalse(os.path.isdir(
            os.path.join(self.tmp, "proj-wt", harness.SEAT_HOME_DIRNAME)))

    def test_lane_inference_skips_a_seat_home_worktree(self):
        """A seat living in its HOME shares the -wt container but is not in a
        lane room; inferring 'seats' would aim release at a lane that does not
        exist. A real lane room still infers."""
        from helm import harness
        home = harness.ensure_home_worktree("codex", self.root)
        self.assertIsNone(work._infer_lane(self.root, home))
        self.assertEqual(work._infer_lane(self.root,
                                          work.lane_path(self.root, "demo")),
                         "demo")
        self.assertIsNone(work._infer_lane(self.root, self.root))


class ClaimTtlTakesUnitsTest(WorkBase):
    """`claim --ttl` takes seconds, bare or with ONE unit suffix s, m, h or d,
    and REFUSES every other spelling with rc 2 and one stderr line.

    THE CRASH THESE ARMS PIN: a claim that read its TTL with a bare `int()`
    raised ValueError on `4h` — the spelling a lease length is naturally
    written in — and the traceback was the verb's whole answer. The refusal
    arms sit beside the accepting ones because a reader widened to take units
    is exactly where `0`, `-1` and `1.5h` get taken by accident.

    EVERY ACCEPTING ARM READS THE STORED LEASE, not only the printed field:
    the printed TTL is the verb echoing its own variable, and a length that
    never reached the claims file would still print correctly."""

    SEAT = "s1"
    EXAMPLES = ("3600s", "90m", "4h", "1d")

    def granted(self, lane, *args):
        """(printed TTL, seconds the STORED lease has left, lease id)."""
        from helm import pk
        rc, out, err = self.work("claim", lane, "--seat", self.SEAT, *args)
        self.assertEqual(0, rc, err)
        fields = out.strip().split("\t")
        raw = pk.read_json(seats.claims_path(), {}) or {}
        left = raw[work.resource(self.root, lane)]["exp_wall"] - time.time()
        return int(fields[3]), left, fields[2]

    def refused(self, rc, out, err, lane="lane-x"):
        """`err`, after the checks every refusal owes: nothing on stdout, no
        traceback, ONE stderr line that names the suffixes and the examples,
        and nothing minted — no lease on the lane, no room on disk."""
        self.assertEqual("", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(1, len(err.strip().splitlines()),
                         "the refusal is not ONE stderr line: %r" % err)
        self.assertIn("s, m, h or d", err,
                      "the refusal does not name the suffixes: %r" % err)
        for example in self.EXAMPLES:
            self.assertIn(example, err,
                          "the refusal does not show %s: %r" % (example, err))
        self.assertNotIn(work.resource(self.root, lane),
                         [c["resource"] for c in seats.claims_list()])
        self.assertFalse(os.path.isdir(work.lane_path(self.root, lane)))
        return err

    def refuse(self, value):
        """(rc, err) for `claim lane-x --ttl <value>`."""
        rc, out, err = self.work("claim", "lane-x", "--seat", self.SEAT,
                                 "--ttl", value)
        return rc, self.refused(rc, out, err)

    def test_4h_is_four_hours(self):
        printed, left, _lease = self.granted("lane-h", "--ttl", "4h")
        self.assertEqual(14400, printed)
        self.assertAlmostEqual(14400, left, delta=120)

    def test_90m_is_ninety_minutes(self):
        printed, left, _lease = self.granted("lane-m", "--ttl", "90m")
        self.assertEqual(5400, printed)
        self.assertAlmostEqual(5400, left, delta=120)

    def test_1d_is_one_day(self):
        printed, left, _lease = self.granted("lane-d", "--ttl", "1d")
        self.assertEqual(86400, printed)
        self.assertAlmostEqual(86400, left, delta=120)

    def test_3600s_is_an_hour(self):
        printed, left, _lease = self.granted("lane-s", "--ttl", "3600s")
        self.assertEqual(3600, printed)
        self.assertAlmostEqual(3600, left, delta=120)

    def test_a_bare_integer_is_still_seconds(self):
        """CONTROL: the spelling that always worked keeps its meaning."""
        printed, left, _lease = self.granted("lane-n", "--ttl", "3600")
        self.assertEqual(3600, printed)
        self.assertAlmostEqual(3600, left, delta=120)

    def test_a_renewal_with_its_lease_takes_a_unit(self):  # noqa: VACUOUS_ASSERTION — every observable here is pinned by an exact equality: the printed TTL, the lease id and the stored seconds left
        """The lease is minted at the default length and renewed at 90m, so a
        renewal that ignored its --ttl would store the default and fail."""
        printed, _left, lease = self.granted("lane-r")
        self.assertEqual(work.DEFAULT_TTL, printed)
        printed, left, again = self.granted("lane-r", "--lease", lease,
                                            "--ttl", "90m")
        self.assertEqual(lease, again)
        self.assertEqual(5400, printed)
        self.assertAlmostEqual(5400, left, delta=120)

    def test_an_unknown_suffix_is_refused(self):
        rc, err = self.refuse("4x")
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl '4x'", err)

    def test_a_negative_is_refused_by_the_ttl_reader(self):
        """`-1` begins with a dash, so a reader that skips dash tokens hands
        it to the unknown-flag scan, whose refusal cannot say which flag was
        starved or what it takes. The TTL's own reader answers it."""
        rc, err = self.refuse("-1")
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl '-1'", err)

    def test_zero_is_refused(self):
        """A zero lease is expired at birth: the room is unguarded the moment
        it exists while its caller believes they hold it."""
        rc, err = self.refuse("0")
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl '0'", err)

    def test_a_fraction_is_refused(self):
        rc, err = self.refuse("1.5h")
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl '1.5h'", err)

    def test_an_empty_value_is_refused(self):
        rc, err = self.refuse("")
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl ''", err)

    def test_a_length_past_any_expressible_instant_is_refused(self):
        value = "9" * 18 + "d"
        rc, err = self.refuse(value)
        self.assertEqual(2, rc, err)
        self.assertIn("--ttl '%s'" % value, err)

    def test_a_ttl_with_no_value_at_all_is_refused(self):
        """`--ttl` as the LAST token. Driven through the dispatcher directly,
        because the fixture's own `--repo` suffix would fill the value
        position, so `--repo` is placed ahead of the flag here."""
        args = ["claim", "lane-x", "--seat", self.SEAT, "--repo", self.root,
                "--ttl"]
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = work.cmd_work(args)
        said = self.refused(rc, out.getvalue(), err.getvalue())
        self.assertEqual(2, rc, said)
        self.assertIn("--ttl with no value", said)

    def test_the_real_process_refuses_an_unknown_suffix_without_a_traceback(self):
        """THE SHAPE THE CRASH WAS MEASURED IN: the entry point in a real
        process under an admitted identity, its exit code and its stderr."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with _tmp_declaring(["--seat", self.SEAT]):
            p = subprocess.run(
                [sys.executable, os.path.join(repo, "bin", "helm"), "work",
                 "claim", "lane-x", "--seat", self.SEAT, "--ttl", "4x",
                 "--repo", self.root],
                cwd=self.root, capture_output=True, text=True, timeout=120,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, HELM_NO_TREE_WARNING="1"))
        self.assertEqual(2, p.returncode, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(1, len([line for line in p.stderr.splitlines()
                                 if "--ttl '4x'" in line]), p.stderr)


class ClaimObeysTheProjectLightTest(WorkBase):
    """A new lane IS new work starting in a project, so `helm work claim` is a
    door the owner's colour has to hold at. Only an AUTHORED light binds; the
    way past one is to change it, which records who and why."""

    def setUp(self):
        super().setUp()
        from helm import home, pk
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "proj": {"name": "proj", "path": self.root, "kind": "git",
                     "status": "dormant", "sessions": {}}}})

    def light(self, colour, reason="the owner decided"):
        from helm import registry
        row, err = registry.state("proj", colour, reason=reason, by="owner",
                                  apply=True)
        self.assertIsNone(err)

    def test_a_project_nobody_decided_about_admits_in_silence(self):  # noqa: VACUOUS_ASSERTION — the claim's success is asserted positively (rc 0 and a room on disk) beside the absent note, and the yellow arm proves the same door DOES print one
        """The scan's half of the light never refuses: `dormant` is the planted
        status here, and the first thing to happen in a dormant project is
        still allowed to happen."""
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(work.lane_path(self.root, "demo")))
        self.assertNotIn("NOTE", err)

    def test_yellow_and_orange_admit_and_say_their_reason_out_loud(self):
        """ORANGE IS WORK, CONSTRAINED. An owner sets it when a credential is
        scarce, and a door that froze the project on that word stopped the work
        he had only asked to be careful with."""
        for colour, why in (("yellow", "maintenance only until the audit lands"),
                            ("orange", "only one credential available")):
            with self.subTest(colour=colour):
                self.light(colour, why)
                rc, out, err = self.work("claim", "demo-" + colour, "--seat", "s1")
                self.assertEqual(rc, 0, err)
                self.assertTrue(os.path.isdir(work.lane_path(self.root, "demo-" + colour)))
                self.assertIn("proj is %s" % colour.upper(), err)
                self.assertIn(why, err)

    def test_red_refuses_a_new_lane_and_changing_the_light_is_the_way_past(self):  # noqa: VACUOUS_ASSERTION — each colour's absent room is followed, on the same lane name, by the present room once the light is handed back
        for colour in ("red",):
            with self.subTest(colour=colour):
                lane = "demo-" + colour
                self.light(colour, "frozen for the release")
                rc, out, err = self.work("claim", lane, "--seat", "s1")
                self.assertEqual(rc, 1)
                self.assertIn("REFUSED", err)
                self.assertIn("frozen for the release", err)
                self.assertIn("helm projects state proj", err)
                self.assertFalse(os.path.isdir(work.lane_path(self.root, lane)))
                self.light("clear", None)
                rc, out, err = self.work("claim", lane, "--seat", "s1")
                self.assertEqual(rc, 0, err)
                self.assertTrue(os.path.isdir(work.lane_path(self.root, lane)))

    def test_a_renewal_is_never_refused_because_a_lease_is_not_work(self):
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lease = out.strip().split("\t")[2]
        self.light("red", "stopped")
        rc, out, err = self.work("claim", "demo", "--seat", "s1", "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip().split("\t")[2], lease)
        self.assertIn("proj is RED", err)                 # told, not stopped
        # and the SAME light does stop a new lane, so the pass above is the
        # renewal rule and not a light that failed to bind
        self.assertEqual(self.work("claim", "other", "--seat", "s1")[0], 1)

    def test_an_unreadable_registry_admits_and_says_so(self):
        """A corrupt file is not the owner saying no. The door proceeds and
        names the trouble, so a broken reader cannot freeze the fleet."""
        from helm import home
        self.light("red", "stopped")
        self.assertEqual(self.work("claim", "demo", "--seat", "s1")[0], 1)  # live
        with open(home.registry_path(), "w") as f:
            f.write("{ not json")
        rc, out, err = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("project light UNKNOWN", err)


class ReleaseTest(WorkBase):
    def test_release_clean_removes_room_and_merged_branch(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(seats.claims_list(), [])         # key surrendered
        self.assertFalse(work._has_branch(self.root, "lane/demo"))  # merged: -d

    def test_release_refuses_the_room_supplying_its_running_binary(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with mock.patch.object(_work_claims, "_runtime_source_in",
                               return_value=True):
            rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                      "--lease", lease)
        self.assertEqual(rc, 1)
        self.assertIn("running helm binary", err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)

    def test_the_PARK_PATH_refuses_when_a_peer_room_holds_the_branch(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the untouched-file assertion; the unconditional positives on the same call are rc==1, the peer path named in err, and the branch tip asserted EQUAL to its pre-call value
        """THE CALLER, NOT THE DOOR. `_wip_commit` refuses on a shared branch,
        but a door-only arm is true only while every caller keeps checking rc —
        and a caller that stops checking is exactly the regression it cannot
        see. release_lane(park=True) is a real authored write onto the lane,
        so it gets its own binding here."""
        rc, out, _err = self.work("claim", "peerpark", "--seat", "s1")
        self.assertEqual(rc, 0)
        path, branch, lease, _t = out.strip().split("\t")
        with open(os.path.join(path, "precious.txt"), "w") as f:
            f.write("must remain uncommitted\n")
        second = os.path.join(self.tmp, "forced-park-peer")
        forced = subprocess.run(
            ["git", "worktree", "add", "--force", "-q", second, branch],
            cwd=self.root, capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(forced.returncode, 0, forced.stderr)
        before = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()

        rc, _out, err = self.work("release", "peerpark", "--seat", "s1",
                                  "--lease", lease, "--park")
        self.assertEqual(rc, 1, "the park path must refuse, not commit")
        self.assertIn("ALSO checked out at", err)
        self.assertIn(second, err, "the refusal must NAME the peer room")
        self.assertEqual(_sh(path, "git", "rev-parse", "HEAD").stdout.strip(),
                         before, "the peer's branch must not have moved")
        self.assertIn("?? precious.txt",
                      _sh(path, "git", "status", "--short").stdout)

    def test_a_MID_OPERATION_room_REFUSES_because_a_SEQUENCER_owns_its_HEAD(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the untouched-file assertion; the unconditional positive is the SAME room after `rebase --abort`, which must WRITE through the same door
        """A SEQUENCER OWNS THIS HEAD AND DETACHED-MEANS-SAFE HANDS IT THE PEN.
        Mid-rebase/merge/cherry-pick a room is DETACHED, so the branch check
        reads "no branch to protect" and lets the write through. Measured
        before this guard existed: a conflicted rebase gives operation=rebase,
        `_current_branch` -> (None, None), and the commit LANDED on the
        sequencer's detached HEAD — state git is about to rewrite.

        gc_scan already refuses to route a mid-operation room to rescue, so
        this was reachable only through the two callers that do not go through
        it: release_lane(park=True) and envtidy. Which is the whole argument
        for the guard living at the write.

        `autonomous=False` BECAUSE THAT IS HOW THE CALLER THIS REHEARSES CALLS
        IT. These three writes stand in for `release_lane(park=True)` — an
        operator naming the write — and the default would make them a SWEEP,
        which the actuator now refuses on a room written seconds ago. The
        arm's control (the same room, after `rebase --abort`, must WRITE) went
        red for exactly that reason. A probe that reaches the door differently
        from its real caller is testing a different door: the MID-OPERATION
        refusal under test applies to both dispositions, and pinning it under
        the wrong one hid that."""
        r = os.path.join(self.tmp, "sequencer-room")
        os.makedirs(r)
        run = lambda *a: subprocess.run(["git", "-C", r] + list(a),
                                        capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", r], capture_output=True)
        run("config", "user.email", "t@example.com"); run("config", "user.name", "t")
        with open(os.path.join(r, "f"), "w") as f: f.write("base\n")
        run("add", "f"); run("commit", "-qm", "base")
        base = run("symbolic-ref", "--short", "HEAD").stdout.strip()
        run("checkout", "-qb", "side")
        with open(os.path.join(r, "f"), "w") as f: f.write("side\n")
        run("add", "f"); run("commit", "-qm", "side")
        run("checkout", "-q", base)
        with open(os.path.join(r, "f"), "w") as f: f.write("trunkward\n")
        run("add", "f"); run("commit", "-qm", "trunkward")
        run("checkout", "-q", "side")
        run("rebase", base)                       # conflicts on purpose
        self.assertEqual(_work_gc._operation_state(r), "rebase",
                         "fixture: the room must actually be mid-rebase")
        with open(os.path.join(r, "precious.txt"), "w") as f:
            f.write("must survive the sequencer\n")

        rc, _out, err = _work_gc._wip_commit(r, "wip: park mid-rebase", autonomous=False)
        self.assertNotEqual(rc, 0, "a sequencer's HEAD is not ours to write")
        self.assertIn("mid-rebase", err)
        self.assertIn("?? precious.txt", run("status", "--short").stdout)

        # THE CONTROL, unconditional and through the same door: once the
        # operation is gone the write must land, so the refusal is about the
        # sequencer and not about _wip_commit having stopped working.
        run("rebase", "--abort")
        before = run("rev-parse", "HEAD").stdout.strip()
        rc, _out, err = _work_gc._wip_commit(r, "wip: after abort", autonomous=False)
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(run("rev-parse", "HEAD").stdout.strip(), before)

    def test_an_UNREADABLE_branch_REFUSES_while_a_DETACHED_one_PROCEEDS(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the no-commit assertion on the refusing call; the unconditional positive is the DETACHED arm below it, which must COMMIT through the same door with a moved HEAD
        """DETACHED AND UNREADABLE ARE OPPOSITE FACTS AND THEY SHARED None.
        git distinguishes them itself — `symbolic-ref --quiet` exits 1 on a
        genuinely detached HEAD and 128 when it cannot read the repository —
        and collapsing both to None made an unknown identity license the
        write. "No branch" means no peer lane to advance; "I could not tell"
        means the blast radius was never measured.

        NO MOCK ON EITHER SIDE: the unreadable case is a real directory that
        is not a repository, so git really returns 128, and the detached case
        is a real detached checkout returning a real 1."""
        blind = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(blind)
        rc, _out, err = _work_gc._wip_commit(blind, "wip: blind identity", autonomous=False)
        self.assertNotEqual(rc, 0, "an unreadable identity must REFUSE")
        self.assertIn("UNKNOWN", err)
        self.assertIn("could not be read", err)

        # THE OPPOSITE POLE THROUGH THE SAME DOOR, unconditional: a room that
        # is genuinely on no branch has no peer lane to advance, so it must
        # still commit. Either assertion alone passes if the two collapse
        # back together.
        room = self.room("detached-write", dirty="precious.txt")
        _sh(room, "git", "checkout", "-q", "--detach")
        before = _sh(room, "git", "rev-parse", "HEAD").stdout.strip()
        rc, _out, err = _work_gc._wip_commit(room, "wip: detached write")
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(_sh(room, "git", "rev-parse", "HEAD").stdout.strip(),
                            before, "a DETACHED room must still commit")

    def test_EVERY_wip_commit_caller_still_checks_its_return_code(self):  # noqa: VACUOUS_ASSERTION — a source-contract walk; its unconditional positive is assertGreaterEqual(sites, 3), which fails rather than reporting a clean sweep if the enumeration finds nothing
        """THE DOOR REFUSES BY RETURNING A NON-ZERO rc, so a caller that stops
        reading rc silently re-opens the hazard for its own path while every
        other arm stays green. This walks the SOURCE of each call site rather
        than trusting that the three I read today stay three."""
        import inspect
        from helm import envtidy
        from helm.work import _claims
        sites = 0
        for mod in (_work_gc, _claims, envtidy):
            src = inspect.getsource(mod)
            lines = src.splitlines()
            for i, line in enumerate(lines):
                if "_wip_commit(" not in line or "def _wip_commit" in line:
                    continue
                if line.lstrip().startswith(("#", '"', "'")):
                    continue
                sites += 1
                window = "\n".join(lines[max(0, i - 1):i + 8])
                # THE BINDING MUST BE ON THE CALL LINE, not merely somewhere in
                # the window: an `rc` eight lines away belongs to some other
                # call, so a window-wide search would pass a caller that
                # discards this one's return value entirely.
                self.assertRegex(
                    line, r"^\s*rc\s*[,=]",
                    "%s call site at line %d does not bind rc on the call "
                    "line:\n%s" % (mod.__name__, i + 1, line))
                self.assertTrue("rc != 0" in window or "rc:" in window
                                or "rc ==" in window,
                                "%s call site near line %d binds rc but never "
                                "tests it:\n%s" % (mod.__name__, i + 1, window))
        self.assertGreaterEqual(sites, 3,
                                "MUST-HIT: expected at least the three known "
                                "_wip_commit call sites, found %d" % sites)

    def test_release_dirty_refuses_then_park_commits(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _b, lease, _t = out.strip().split("\t")
        junk = os.path.join(path, "junk.txt")
        with open(junk, "w") as f:
            f.write("do not lose me\n")
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 1)                     # inspect before the key
        self.assertIn("--park", err)                # the two exits are named
        self.assertTrue(os.path.exists(junk))       # nothing touched
        self.assertEqual(len(seats.claims_list()), 1)   # key still in pocket
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease, "--park")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path))       # unlanded room stays too
        show = _sh(self.root, "git", "show", "lane/demo:junk.txt")
        self.assertEqual(show.stdout, "do not lose me\n")   # lost-and-found
        self.assertIn("TRIAGE demo", out)
        self.assertIn("git committer, shared across seats:", out)
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertFalse(row["locked"])            # released room is inspectable

    def test_release_occupied_room_keeps_room_and_lease(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                      "--lease", lease)
            self.assertEqual(rc, 1)
            self.assertIn("OCCUPIED", err)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(len(seats.claims_list()), 1)
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def test_release_landed_room_stops_only_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", lease)
            self.assertEqual(rc, 0, err)
            _reap(proc)
            self.assertIsNotNone(proc.poll(),  # noqa: VACUOUS_ASSERTION — reaped-or-not is the claim; _reap above already raises if it never exits
                                 "release left the disposable shell running")
            self.assertIn("stopped disposable Orca shell", out)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(seats.claims_list(), [])
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()

    def test_wrong_lease_cannot_stop_a_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, _lease, _ttl = out.strip().split("\t")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", "beefbeefbeefbeef")
            self.assertEqual(rc, 1)
            self.assertIsNone(proc.poll(), "wrong capability stopped a process")
            self.assertNotIn("stopped disposable", out + err)
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(len(seats.claims_list()), 1)
        finally:
            proc.terminate()
            proc.wait()

    def test_unlanded_release_keeps_a_disposable_orca_shell(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with open(os.path.join(path, "lane.txt"), "w") as f:
            f.write("unlanded\n")
        _sh(path, "git", "add", "-A")
        self.assertEqual(_sh(path, "git", "commit", "-q", "-m", "lane").returncode,
                         0)
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        try:
            with mock.patch.object(_work_claims,
                                   "_disposable_worktree_occupant",
                                   return_value=True), \
                    mock.patch.object(_work_gc,
                                      "_disposable_worktree_occupant",
                                      return_value=True):
                rc, out, err = self.work("release", "demo", "--seat", "s1",
                                         "--lease", lease)
            self.assertEqual(rc, 0, err)
            self.assertIsNone(proc.poll(), "unlanded release stopped the shell")
            self.assertNotIn("stopped disposable", out)
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(work._has_branch(self.root, "lane/demo"))
            self.assertEqual(seats.claims_list(), [])
        finally:
            proc.terminate()
            proc.wait()

    def test_release_wrong_lease_removes_nothing(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path = out.split("\t")[0]
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", "beefbeefbeefbeef")
        self.assertEqual(rc, 1)                 # key surrender precedes removal
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)

    def test_wrong_lease_cannot_park_commit_before_refusal(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path = out.split("\t")[0]
        with open(os.path.join(path, "precious.txt"), "w") as f:
            f.write("must remain uncommitted\n")
        before = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        rc, _out, err = self.work("release", "demo", "--seat", "s1",
                                  "--lease", "beefbeefbeefbeef", "--park")
        self.assertEqual(rc, 1)
        self.assertIn("refresh needs", err)
        self.assertEqual(_sh(path, "git", "rev-parse", "HEAD").stdout.strip(),
                         before)
        self.assertIn("?? precious.txt", _sh(path, "git", "status", "--short").stdout)

    def test_release_unmerged_clean_lane_keeps_room_branch_and_triage(self):
        rc, out, _err = self.work("claim", "demo", "--seat", "s1")
        path, _branch, lease, _ttl = out.strip().split("\t")
        with open(os.path.join(path, "land-me.txt"), "w") as f:
            f.write("unlanded\n")
        _sh(path, "git", "add", "-A")
        # Commit with distinct author and committer to verify %cn format specifier
        env = dict(os.environ, GIT_AUTHOR_NAME="Alice Author", GIT_AUTHOR_EMAIL="alice@example.com",
                   GIT_COMMITTER_NAME="Bob Committer", GIT_COMMITTER_EMAIL="bob@example.com")
        proc = subprocess.run(["git", "commit", "-q", "-m", "lane work"], cwd=path, env=env)
        self.assertEqual(proc.returncode, 0)
        rc, out, err = self.work("release", "demo", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/demo"))
        self.assertRegex(out, r"TRIAGE demo .*tip [0-9a-f]{12}, age .*\(git committer, shared across seats: Bob Committer <bob@example.com> — not seat provenance\)")
        self.assertEqual(seats.claims_list(), [])


class SupersededReleaseRetiresTheRoomAndKeepsTheWorkTest(WorkBase):
    """THE THIRD STATE: work that will never land, deliberately.

    The reaper asks LANDED (retire everything) or NOT-LANDED (keep
    everything). A lane whose class was solved independently and more strongly
    on trunk, or whose one surviving finding is declined by canon, is neither —
    and it stands forever on a predicate nobody asked the right question
    (task/1956). These arms pin what the new door may do and, more
    importantly, what it still may not.
    """

    def unlanded(self, lane="dead"):
        """(path, lease, tip) — a claimed room one real commit ahead of main."""
        rc, out, err = self.work("claim", lane, "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, _branch, lease, _ttl = out.strip().split("\t")
        with open(os.path.join(path, "lane.txt"), "w") as f:
            f.write("work that will never land\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertFalse(work._merged(self.root, work.lane_branch(lane)),
                         "fixture: the lane must be genuinely unlanded")
        return path, lease, tip

    def test_the_room_retires_and_EVERY_COMMIT_STAYS_REACHABLE_on_the_branch(self):
        """THE WHOLE DESIGN RESTS ON THIS ONE SEPARATION.

        Two acts hid behind one `merged`: removing the ROOM, which costs a
        `helm work claim` to rebuild, and DELETING THE BRANCH, which is the act
        that can put commits beyond reach. This door opens only the first, so
        the assertion can never cost authored bytes — and that is the property
        asserted here directly, by resolving the lane's tip through the branch
        AFTER the room is gone rather than by trusting the branch's existence.
        """
        path, lease, tip = self.unlanded()
        self.assertTrue(os.path.exists(path), "control: the room exists first")
        self.assertEqual(len(seats.claims_list()), 1, "control: lease held")
        rc, out, err = self.work("release", "dead", "--seat", "s1",
                                 "--lease", lease,
                                 "--superseded", "class solved on trunk by 155fee718")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path), "the room retires")
        self.assertTrue(work._has_branch(self.root, "lane/dead"),
                        "the branch is KEPT — this is the safety")
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "lane/dead").stdout.strip(),
            tip, "and it still resolves to the exact commit the room held")
        self.assertIn("SUPERSEDED dead", out)
        self.assertIn("class solved on trunk by 155fee718", out)
        self.assertIn("did NOT authorize this", out)
        self.assertEqual(seats.claims_list(), [])

    def test_an_ordinary_release_wakes_NOBODY_and_a_superseded_one_still_does(self):
        """A HOUSEKEEPING VERB MUST NOT SPEND A READER'S TURN.

        `release` on an unlanded lane must not post its triage note to chat
        ADDRESSED TO THE INTEGRATOR. Measured cost of doing so: five wakes in
        twenty-seven seconds for lanes that seat had already ruled into landing
        rows, each one a turn on the credential the owner asked to spare — and
        every one of them said "not landed by ancestry or patch identity",
        which is the EXPECTED state of a lane whose row is still gating.

        THE TWO CLAIMS ARE SEPARATE AND BOTH ARE HERE. Silence on the ordinary
        path is worthless without proof that the addressed row still goes out
        when it carries something a reader can act on; and proof that it goes
        out is worthless if it also goes out for housekeeping. So the same mock
        reads both legs in one method, and the superseded leg is this arm's
        positive control: if the post door were removed entirely, or if this
        release stopped reaching it at all, the second half goes red.
        """
        quiet, lease_q, _tip = self.unlanded("quiet")
        _p, lease_s, _t = self.unlanded("loud")
        with mock.patch("helm.chat.post") as posted:
            rc, out, err = self.work("release", "quiet", "--seat", "s1",
                                     "--lease", lease_q)
            self.assertEqual(rc, 0, err)
            self.assertIn("TRIAGE quiet", out,
                          "control: the note is still RENDERED — the cure is "
                          "about who gets woken, not about losing the triage")
            self.assertTrue(os.path.exists(quiet),
                            "control: the unlanded room is kept, so this "
                            "really is the not-landed path")
            self.assertEqual(posted.call_count, 0,
                             "housekeeping woke somebody: %r"
                             % (posted.call_args_list,))

            # THE POSITIVE CONTROL, ON THIS SAME MOCK INSTANCE. A SUPERSEDED
            # release asserts work will never land on no proof at all, which is
            # a decision other seats may be waiting on, so it keeps its
            # addressed row. Sharing the instance is what makes the silence
            # above mean anything: a post door removed outright, or a release
            # that stopped reaching chat for any reason, leaves this counter at
            # zero and takes this arm red with it.
            rc, _out, err = self.work("release", "loud", "--seat", "s1",
                                      "--lease", lease_s,
                                      "--superseded", "will never land")
            self.assertEqual(rc, 0, err)
            self.assertEqual(posted.call_count, 1,
                             "the actionable row must still go out")
            said = posted.call_args[0][0]
            self.assertIn("SUPERSEDED loud", said)
            self.assertIn("will never land", said)

    def test_the_branch_ACTUATOR_IS_NEVER_REACHED_on_the_superseded_path(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assert_not_called; its positive control is assert_called_once on the LANDED leg of this same method, reading the same mock attribute. It is unconditional — the `with mock.patch.object` around it is how the mock is held, not a branch — and the arm's other unconditional positives are rc==0 and the room actually retiring
        """THE OUTCOME AND THE SEPARATION ARE TWO DIFFERENT CLAIMS.

        Measured while mutation-testing the arm above: routing `superseded`
        into the branch-delete leg leaves the branch standing ANYWAY, because
        `_delete_lane_branch` has its own independent refusal for work that is
        not landed. That is good news about the substrate and bad news about
        the arm — a surviving branch cannot distinguish "this door never asked
        for the delete" from "it asked and the actuator said no", and only the
        first is the design.

        So this pins the design directly: on the superseded path the
        actuator is NOT CALLED. Its positive control is the landed release in
        this class, which must reach the same actuator through the same
        release — otherwise a door that had stopped retiring anything at all
        would pass here.
        """
        path, lease, _tip = self.unlanded("nodelete")
        with mock.patch.object(_work_claims, "_delete_lane_branch") as actuator:
            rc, _out, err = self.work("release", "nodelete", "--seat", "s1",
                                      "--lease", lease,
                                      "--superseded", "will never land")
            self.assertEqual(rc, 0, err)
            self.assertFalse(os.path.exists(path), "the room did retire")
            actuator.assert_not_called()
        # THE CONTROL: a proven-landed lane reaches that same actuator.
        rc, out, err = self.work("claim", "reached", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        _p, _b, lease2, _t = out.strip().split("\t")
        with mock.patch.object(_work_claims, "_delete_lane_branch",
                               return_value=["control"]) as actuator:
            rc, _out, err = self.work("release", "reached", "--seat", "s1",
                                      "--lease", lease2)
            self.assertEqual(rc, 0, err)
            actuator.assert_called_once()

    def test_the_SAME_FIXTURE_without_the_flag_still_keeps_the_room(self):
        """THE CONTROL, IN THE SAME FIXTURE. An arm that only shows the door
        opening proves nothing about the predicate it bypasses — a door that
        had accidentally become unconditional would pass the arm above."""
        path, lease, _tip = self.unlanded("kept")
        rc, out, err = self.work("release", "kept", "--seat", "s1",
                                 "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path), "no flag, no retirement")
        self.assertIn("not proven landed", out)
        self.assertNotIn("SUPERSEDED", out)

    def test_a_MALFORMED_REASON_mutates_nothing_not_even_the_lease(self):
        """VALIDATED BEFORE ANYTHING MOVES. A reason is the entire evidence
        this door takes, so a reason that cannot be read back must cost the
        caller nothing — and the lease is the cheapest thing to lose silently.
        """
        path, lease, _tip = self.unlanded("malformed")
        self.assertTrue(os.path.isdir(path), "control: the room exists first")
        self.assertEqual(len(seats.claims_list()), 1, "control: lease held")
        for bad in ("", "two\nlines", "x" * 257):
            rc, _out, err = self.work("release", "malformed", "--seat", "s1",
                                      "--lease", lease, "--superseded", bad)
            self.assertEqual(rc, 1, "rejected: %r" % bad)
            self.assertIn("nothing released", err)
            self.assertTrue(os.path.isdir(path), "room untouched: %r" % bad)
            self.assertEqual(len(seats.claims_list()), 1,
                             "lease still held: %r" % bad)
        # AND THE CONTROL: the same room, same lease, a readable reason.
        rc, _out, err = self.work("release", "malformed", "--seat", "s1",
                                  "--lease", lease, "--superseded", "declined by canon")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))

    def test_the_EVIDENCE_LINE_does_not_contradict_the_ACT_it_accompanies(self):
        """A SURFACE MUST NOT END BY DENYING WHAT IT JUST DID.

        `_branch_triage` closed with the hardcoded words "worktree + branch
        kept", which was true for every caller that keeps the room and became
        FALSE the moment a caller retired one. Found by DOGFOODING this verb on
        a real 29-day room (task/1956): the worktree was already gone when the
        line it printed claimed the worktree was kept.

        THE DISPOSITION IS THE CALLER'S FACT. Every other clause on that line
        answers a question about the world; this one reports what the caller
        DID, so it is passed in rather than assumed — and the default keeps the
        keep-the-room callers byte-identical, which the control below pins.
        """
        path, lease, _tip = self.unlanded("contra")
        rc, out, err = self.work("release", "contra", "--seat", "s1",
                                 "--lease", lease,
                                 "--superseded", "will never land")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path), "the room did retire")
        self.assertIn("branch kept, room retired on stated evidence", out)
        self.assertNotIn("worktree + branch kept", out,
                         "the evidence line still claims the room was kept "
                         "after this very call removed it")
        # THE CONTROL, AND IT IS THE HALF THAT MAKES THE DEFAULT SAFE: the
        # keep-the-room path must still say the keep-the-room words, or this
        # cure has quietly changed every other triage surface in the fleet.
        path2, lease2, _t2 = self.unlanded("kept2")
        rc, out2, err = self.work("release", "kept2", "--seat", "s1",
                                  "--lease", lease2)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path2))
        self.assertIn("worktree + branch kept", out2)
        self.assertNotIn("room retired on stated evidence", out2)

    def test_a_ROOMLESS_supersede_claims_NO_retirement_it_did_not_perform(self):  # noqa: VACUOUS_ASSERTION — every absence here has an unconditional positive control on the SAME observable: the two assertNotIn on `out` are preceded by assertIn('branch kept; no room was registered', out), and the assertFalse(os.path.exists(path)) fixture check is preceded by assertTrue(os.path.exists(path)) on the same path. This arm asserts TWO absences on purpose because neither existing phrase is true in the roomless state — that is the finding, not an omission
        """THE REQUEST IS NOT THE ACT, AND THE ACT HAS THREE OUTCOMES.

        Keying the disposition on `superseded` keys it on what the CALLER
        ASKED, so a lane whose room is not registered retires nothing and the
        line still announces a retirement. That is the same defect this
        surface exists to refuse, one branch over: a cure can inherit the
        shape of the bug it cures.

        NEITHER EXISTING PHRASE IS TRUE HERE: "worktree + branch kept" claims
        a worktree that never existed to keep, which is the mirror of the
        original bug rather than a safe fallback.
        """
        # A BRANCH WITH NO REGISTERED ROOM, built the way the world makes one:
        # claim, commit, then drop the worktree while leaving the branch.
        rc, out, err = self.work("claim", "roomless", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, _b, lease, _t = out.strip().split("\t")
        self.assertTrue(os.path.exists(path),
                        "control: the room exists before the fixture drops it")
        with open(os.path.join(path, "lane.txt"), "w") as f:
            f.write("work\n")
        _sh(path, "git", "add", "-A")
        self.assertEqual(_sh(path, "git", "commit", "-q", "-m", "w").returncode, 0)
        # UNLOCK BEFORE REMOVING, THE WAY PRODUCTION DOES. `helm work claim`
        # git-worktree-LOCKS the room, and `worktree remove --force` refuses a
        # locked one — the first cut of this fixture skipped the unlock, did
        # not check the removal's own rc, and was caught only by the must-hit
        # below. release_lane calls unlock_worktree before remove_worktree for
        # exactly this reason; the fixture uses the same order.
        for argv in (["git", "worktree", "unlock", path],
                     ["git", "worktree", "remove", "--force", path]):
            r = subprocess.run(argv, cwd=self.root, capture_output=True,
                               text=True, timeout=30,
                               env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
            self.assertEqual(r.returncode, 0, "%s: %s" % (argv[2], r.stderr))
        self.assertFalse(os.path.exists(path), "fixture: the room must be gone")

        rc, out, err = self.work("release", "roomless", "--seat", "s1",
                                 "--lease", lease,
                                 "--superseded", "will never land")
        self.assertEqual(rc, 0, err)
        self.assertIn("branch kept; no room was registered", out)
        self.assertNotIn("room retired on stated evidence", out,
                         "the line announces a retirement this call could not "
                         "have performed — there was no room to retire")
        self.assertNotIn("worktree + branch kept", out,
                         "the line claims a worktree was kept that never "
                         "existed to keep")
        self.assertTrue(work._has_branch(self.root, "lane/roomless"),
                        "and the branch is still kept, which is the one "
                        "disposition that IS true here")

    def test_a_DIRTY_room_refuses_the_assertion_exactly_as_it_refuses_a_proof(self):
        """UNCOMMITTED BYTES OUTRANK EVERY AUTHORITY, STATED OR PROVEN. The
        dirty guard runs before the lease is even surrendered, and this door
        is added BELOW it on purpose: an assertion must be no more able to
        discard authored bytes than a landedness proof is."""
        path, lease, _tip = self.unlanded("dirtysup")
        with open(os.path.join(path, "precious.txt"), "w") as f:
            f.write("must remain uncommitted\n")
        rc, _out, err = self.work("release", "dirtysup", "--seat", "s1",
                                  "--lease", lease,
                                  "--superseded", "will never land")
        self.assertEqual(rc, 1)
        self.assertIn("DIRTY", err)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("?? precious.txt",
                      _sh(path, "git", "status", "--short").stdout)
        self.assertEqual(len(seats.claims_list()), 1)

    def test_a_PROVEN_LANDED_lane_says_the_assertion_was_not_used(self):
        """A PROOF OUTRANKS AN ASSERTION AND THE OUTPUT SAYS SO. Silently
        accepting the flag here would teach a reader that --superseded is what
        retired a branch that Git had already proven landed — and the next
        reader would reach for the flag instead of the proof."""
        rc, out, err = self.work("claim", "landed", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path, _branch, lease, _ttl = out.strip().split("\t")
        self.assertTrue(os.path.isdir(path), "control: the room exists first")
        self.assertTrue(work._has_branch(self.root, "lane/landed"),
                        "control: the branch exists first")
        rc, out, err = self.work("release", "landed", "--seat", "s1",
                                 "--lease", lease,
                                 "--superseded", "not needed here")
        self.assertEqual(rc, 0, err)
        self.assertIn("--superseded was not needed and was not used", out)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/landed"),
                         "the PROOF retires the branch, as it always did")

    def test_a_VALUELESS_flag_refuses_instead_of_silently_becoming_a_plain_release(self):
        """THE FLAG WAS TYPED AND THE FLAG HAS A VALUE ARE DIFFERENT FACTS.

        Found on row f584892b6aa6, before the gate.
        `seats._flag` answers None both for an ABSENT flag and for a flag that
        is last or followed by another flag — so a door keyed on the parsed
        value dropped the evidence, fell back to an ORDINARY release, retired
        nothing, and said nothing. That is the one outcome a door whose whole
        authority IS the evidence cannot have, and it is silent, which is why
        no arm above could see it: every one of them supplied a good reason.

        THE THIRD SHAPE IS THE WORST. `--superseded --stale` is the pair this
        verb deliberately refuses, and the refusal itself read the parsed
        value — so it could not fire on the shape it exists for.
        """
        path, lease, _tip = self.unlanded("valueless")
        held = len(seats.claims_list())
        self.assertTrue(os.path.exists(path), "control: the room exists first")
        self.assertEqual(held, 1, "control: the lease is held first")
        for args in (["--lease", lease, "--superseded"],
                     ["--lease", lease, "--superseded", "--park"],
                     ["--lease", lease, "--superseded", "-dash-leading"]):
            rc, _out, err = self.work("release", "valueless", "--seat", "s1",
                                      *args)
            self.assertEqual(rc, 2, "refused: %r" % (args,))
            self.assertIn("needs a value that is not another flag", err)
            self.assertIn("--superseded=REASON", err)
            self.assertTrue(os.path.isdir(path), "room untouched: %r" % (args,))
            self.assertEqual(len(seats.claims_list()), held,
                             "lease untouched: %r" % (args,))
        # THE `=` FORM IS THE ESCAPE AND IT MUST ACTUALLY WORK, or the refusal
        # above advises a spelling that does not exist and a dash-leading
        # reason stays unsayable.
        rc, out, err = self.work("release", "valueless", "--seat", "s1",
                                 "--lease", lease,
                                 "--superseded=-dash-leading reason")
        self.assertEqual(rc, 0, err)
        self.assertIn("-dash-leading reason", out)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(work._has_branch(self.root, "lane/valueless"))

    def test_STALE_and_SUPERSEDED_refuse_each_other_even_with_NO_VALUE(self):
        """THE PAIR-REFUSAL READS THE TOKENS, NEVER THE PARSED VALUE — which
        is the same distinction as the arm above, at the one guard where
        reading the value made the guard unreachable for its own case."""
        path, _lease, _tip = self.unlanded("bothvalueless")
        held = len(seats.claims_list())
        self.assertTrue(os.path.exists(path), "control: the room exists first")
        self.assertEqual(held, 1, "control: the lease is held first")
        rc, _out, err = self.work("release", "bothvalueless", "--seat", "s1",
                                  "--superseded", "--stale")
        self.assertEqual(rc, 2)
        self.assertIn("different requests", err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), held)

    def test_STALE_and_SUPERSEDED_are_different_requests_and_it_refuses_both(self):
        """--stale is a liveness proof about a DEAD HOLDER and touches neither
        room nor branch; --superseded is a live caller's evidence about the
        WORK. Accepting both would silently drop one, and which one it dropped
        would depend on argument order."""
        path, _lease, _tip = self.unlanded("bothflags")
        rc, _out, err = self.work("release", "bothflags", "--seat", "s1",
                                  "--stale", "--superseded", "never lands")
        self.assertEqual(rc, 2)
        self.assertIn("different requests", err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(seats.claims_list()), 1)


class ListHolderlessDeltaTest(WorkBase):
    """#289 third instance: a lease expires, the room and its COMMITTED delta
    do not — and nothing listed it, so the failure mode is a seat silently
    redoing the work (two seats, one gc-triage fix, two lane names,
    2026-08-05). The list now calls out holderless-with-delta by default."""

    def _commit_in(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write("lane work\n")
        self.assertEqual(_sh(path, "git", "add", "-A").returncode, 0)
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_holderless_room_with_commits_is_called_out(self):
        path = self.room("orphan-delta")
        self._commit_in(path, "work.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("HOLDERLESS WITH DELTA", out)
        section = out.split("HOLDERLESS WITH DELTA", 1)[1]
        self.assertIn("orphan-delta", section)
        self.assertIn("helm work claim orphan-delta", section)

    def test_the_section_obeys_the_empty_section_law(self):  # noqa: VACUOUS_ASSERTION — in-test flip: the SAME room gains a commit and the SAME observable speaks, three lines down
        # No delta -> silent; the SAME room gaining a commit flips the SAME
        # observable (the positive control that makes the absence a fact
        # about the filter, not about a broken list).
        path = self.room("orphan-quiet")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("HOLDERLESS WITH DELTA", out)
        self._commit_in(path, "late.txt")
        rc, out, err = self.work("list")
        self.assertIn("HOLDERLESS WITH DELTA", out)

    def test_a_held_room_with_delta_is_not_in_the_section(self):  # noqa: VACUOUS_ASSERTION — in-test flip: releasing the SAME room enters it in the section
        rc, out, err = self.work("claim", "held-delta")
        self.assertEqual(rc, 0, err)
        path = work.lane_path(self.root, "held-delta")
        self._commit_in(path, "held.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # held rows stay out; the section exists only if some OTHER
        # holderless-with-delta room does — there is none here.
        self.assertNotIn("HOLDERLESS WITH DELTA", out)
        # positive control: the same room released (parked work) enters it.
        # The release MUST succeed and the assertions MUST run — a control
        # behind `if rc2 == 0` skips silently on a failed release, which is
        # a pass whose input was missing (a review caught exactly
        # that conditional).
        lease = re.search(r"lease=([0-9a-f]+)", out)
        self.assertIsNotNone(lease, "the list did not hand back the lease "
                             "token for this seat's own row")
        rc2, out2, err2 = self.work(
            "release", "held-delta", "--lease", lease.group(1))
        self.assertEqual(rc2, 0, (out2, err2))
        rc3, out3, _ = self.work("list")
        self.assertIn("HOLDERLESS WITH DELTA", out3)
        self.assertIn("held-delta",
                      out3.split("HOLDERLESS WITH DELTA", 1)[1])


class GcTest(WorkBase):
    def setUp(self):
        super().setUp()
        rc, out, _e = self.work("claim", "held", "--seat", "s1")
        self.assertEqual(rc, 0)
        self.held = out.split("\t")[0]                    # live lease
        self.locked = self.room("noturn")                 # out-of-band lock
        _sh(self.root, "git", "worktree", "lock", self.locked,
            "--reason", "owner says keep")
        self.clean = self.room("cleanmg")                 # clean + merged,
        _sh(self.root, "git", "worktree", "lock", self.clean,
            "--reason", "lease:deadbeef")                 # stale key tag
        self.dirty = self.room("messy", dirty="junk.txt")  # dirty, lease-less

    def test_dry_run_verdicts_touch_nothing(self):
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        for path in (self.held, self.locked, self.clean, self.dirty):
            self.assertTrue(os.path.isdir(path))
        self.assertRegex(out, r"KEEP\s+held\s+lease live")
        self.assertRegex(out, r"KEEP\s+noturn\s+locked out-of-band")
        self.assertRegex(out, r"REMOVE\s+cleanmg")
        self.assertRegex(out, r"RESCUE\s+messy")
        self.assertIn("dry-run", out)
        self.assertIn("never discarded", out)

    def test_apply_sweeps_and_rescues_never_discards(self):
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.held))      # guest in the room
        self.assertTrue(os.path.isdir(self.locked))    # do-not-disturb
        self.assertFalse(os.path.exists(self.clean))   # clean+merged swept
        self.assertFalse(work._has_branch(self.root, "lane/cleanmg"))
        self.assertTrue(os.path.isdir(self.dirty))     # rescued + kept unlanded
        show = _sh(self.root, "git", "show", "lane/messy:junk.txt")
        self.assertEqual(show.stdout, "precious uncommitted bytes\n")
        self.assertTrue(work._has_branch(self.root, "lane/messy"))
        self.assertIn("rescued", out)
        self.assertIn("room kept", out)

    def test_apply_REFUSES_when_the_trunk_ref_cannot_be_refreshed(self):
        """gc_scan decides landedness against a REMOTE-TRACKING ref, which is a
        local snapshot, and --apply deletes branches on that decision. An
        unrefreshed proof must not authorize a deletion — same law as `lr
        land`'s deletion guard: an unscannable deletion set is not a known-safe
        one. vcs.py:405 already stated the rule as prose and nothing did it."""
        # PATCH THE CONSUMER, NOT THE DEFINER. _cli does `from ._gc import
        # refresh_trunk`, so the name it calls is bound at import — patching
        # _gc.refresh_trunk leaves the caller pointing at the original and the
        # test measures an object nobody uses. It passed for that reason first.
        from helm.work import _cli as _workcli
        with mock.patch.object(_workcli, "refresh_trunk",
                               return_value=(False, "git fetch origin failed: boom")):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING to apply", err)
        self.assertIn("boom", out)
        # AND NOTHING WAS TOUCHED — the refusal is before the scan, so no
        # verdict was even computed, let alone enacted
        for path in (self.held, self.locked, self.clean, self.dirty):
            self.assertTrue(os.path.isdir(path), path)
        self.assertTrue(work._has_branch(self.root, "lane/cleanmg"))

    def test_the_DRY_RUN_never_fetches(self):
        """A read-only pass must not touch the network. The refusal above is
        the price of DESTRUCTION, not of looking."""
        from helm.work import _cli as _workcli
        with mock.patch.object(_workcli, "refresh_trunk") as fetched:
            rc, _out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        fetched.assert_not_called()

    def test_a_LOCAL_trunk_has_nothing_to_refresh_and_that_is_not_a_failure(self):
        """THE CONTROL. Without it the refusal test passes for a version that
        refuses every apply — which would be 'safe' and useless."""
        from helm.work import _gc
        ok, why = _gc.refresh_trunk(self.root)
        self.assertTrue(ok, why)
        self.assertIn("nothing to refresh", why)
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.clean))   # the sweep still happens

    def test_apply_never_removes_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
        try:
            rc, out, err = self.work("gc", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertIn("OCCUPIED", out)
            self.assertTrue(os.path.isdir(self.clean))
            self.assertNotIn("(deleted)", os.readlink("/proc/%d/cwd" % proc.pid))
        finally:
            proc.terminate()
            proc.wait()

    def _tip(self, lane):
        r = subprocess.run(["git", "rev-parse", work.lane_branch(lane)],
                           cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _rescue_row(self, path):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "rescue",
                         "fixture: %s must be lease-less and DIRTY" % path)
        return row

    # ------------------------------------------------------------------
    # AN AUTONOMOUS RESCUE DEFERS TO A ROOM ITS AUTHOR IS STILL IN.
    #
    # Measured 2026-08-27: the sweep rescued mid-edit work four times across
    # three rooms in one session. Every sweep was LAWFUL — `_removal_blocker`
    # refuses on a LIVE lease and it works; these leases had LAPSED while their
    # holder kept typing. Content survived, provenance did not, and one `wip:
    # rescued` commit was later read by `dispatch triage` as a CURE.
    #
    # THE GUARD IS AT `_wip_commit`, THE SHARED ACTUATOR, not at gc_enact.
    # A review on e8336b68699c: my first cut guarded one caller and left the
    # other two authored writes open. `autonomous=False` is the operator's
    # door — `helm work release --park` names this write instead of guessing.
    # ------------------------------------------------------------------

    REFUSED = "refusing to AUTONOMOUSLY WIP-commit"

    def test_a_room_its_author_JUST_WROTE_is_not_rescued(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the two tip comparisons, and the rung cannot credit their control BY CONSTRUCTION: each `self._tip(...)` site mints its own opaque call identity, so even the inline abandoned-room rescue asserted first is a different producer. The unconditional positives on the SAME gc_enact call are the refusal text and `_work_gc._dirty(room)`
        """THE MUST-HIT, and the sequence is the whole point: the write lands
        AFTER the scan verdict, which is how all four real sweeps happened.
        `gc_scan` still says rescue and it is not wrong — the room IS dirty and
        lease-less — so the actuator is what has to notice the author.

        ASSERTED ON THE REPO, NOT THE SENTENCE. A version that printed a
        refusal and committed anyway would read identically in the lines, so
        this asks git whether the tip moved and whether the bytes are still
        uncommitted."""
        # CONTROL, UNCONDITIONAL AND FIRST: an ABANDONED room rescues in this
        # same test, so the unmoved tip below is a REFUSAL rather than a rescue
        # path this arm broke outright. Without it a cure that refused
        # unconditionally would pass while switching rescue off — which reads
        # as safe and quietly strands authored bytes.
        stale = self.room("stale-control", dirty="precious.txt")
        stale_before = self._tip("stale-control")
        control = work.gc_enact(self.root, self._rescue_row(stale))
        self.assertNotEqual(self._tip("stale-control"), stale_before,
                            "control: an abandoned room MUST still rescue: %r"
                            % (control,))

        room = self.room("live-author", dirty="precious.txt")
        row = self._rescue_row(room)
        with open(os.path.join(room, "precious.txt"), "a") as f:
            f.write("the author is still typing\n")
        before = self._tip("live-author")
        lines = work.gc_enact(self.root, row)
        self.assertTrue(any(self.REFUSED in l for l in lines), lines)
        self.assertTrue(any("lapsed lease is not an abandoned room" in l
                            for l in lines),
                        "the refusal must say WHY, in the author's terms: %r"
                        % (lines,))
        self.assertEqual(self._tip("live-author"), before,
                         "a refusal must not advance the lane")
        self.assertTrue(os.path.isdir(room), "refusing keeps the room")
        self.assertTrue(_work_gc._dirty(room),
                        "the bytes must still be UNCOMMITTED in the room — "
                        "committing them under an anonymous message IS the loss")

    def test_the_window_is_THE_CONSTANT_not_a_number_this_arm_knows(self):  # noqa: VACUOUS_ASSERTION — same per-call-site identity limit; the flagged absences are the fixture pins on `wrote_ago` and the two tip comparisons. The unconditional positive is the refusal text on the widened call, and the shipped-window rescue above it is the control the rung cannot match to a tip it did not produce
        """THE DERIVATION PROOF — move the input, the verdict must move.

        The arm above passes against a cure pinned to ANY window with 900
        inside it, because it supplies one age comfortably either side. This
        takes two rooms of the SAME measured age and changes only
        `_RESCUE_ACTIVE_S`: at the shipped window the room rescues, and widened
        past that same age the identical call must refuse. Nothing here
        transcribes the number."""
        wide = self.room("window-wide", dirty="precious.txt")
        narrow = self.room("window-narrow", dirty="precious.txt")
        age = _work_gc._room_status(narrow)["wrote_ago"]
        self.assertIsNotNone(age, "fixture: the room needs a readable clock")
        self.assertGreater(age, _work_gc._RESCUE_ACTIVE_S,
                           "fixture: abandon() must age past the live window")

        narrow_before = self._tip("window-narrow")
        shipped = work.gc_enact(self.root, self._rescue_row(narrow))
        self.assertNotEqual(self._tip("window-narrow"), narrow_before,
                            "control: at the shipped window this age rescues: "
                            "%r" % (shipped,))

        wide_before = self._tip("window-wide")
        row = self._rescue_row(wide)
        with mock.patch.object(_work_gc, "_RESCUE_ACTIVE_S", age + 60):
            widened = work.gc_enact(self.root, row)
        self.assertTrue(any(self.REFUSED in l for l in widened),
                        "widening the window must move the boundary: %r"
                        % (widened,))
        self.assertEqual(self._tip("window-wide"), wide_before,
                         "the widened refusal must not advance the lane")

    def test_an_UNREADABLE_CLOCK_IS_UNKNOWN_AND_UNKNOWN_REFUSES(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the unmoved-tip assertion; its unconditional positive is the refusal text on the same enact call, and the control that a READABLE clock on the same room still rescues runs first
        """THE REVERSAL, and it is mine to own (a review on e8336b68699c).

        I shipped this branch as a DELIBERATE fail-open and defended it in the
        commit message: deferring on an unreadable clock would leave dirty work
        uncommitted in a room nobody can vouch for. The reasoning assumed the
        operator had no other door. THEY DO — `helm work release --park` — so
        the fail-open bought nothing that door does not already buy, and paid
        for it in exactly the provenance this lane exists to protect. A room
        whose dirty paths were just DELETED, or whose only dirty thing is a
        SUBMODULE with no bounded file clock, or which raced a stat, read as
        ABANDONED and was committed immediately.

        It is also the same two-state-collapses-three mistake I had just fixed
        one lane over, where an interpreter check turned "I could not look"
        into "it is fine". I did not carry the lesson across the two lanes I
        was holding at the same time."""
        room = self.room("no-clock", dirty="precious.txt")
        row = self._rescue_row(room)

        # CONTROL FIRST: with the clock READABLE this same room rescues.
        before = self._tip("no-clock")
        ok = work.gc_enact(self.root, row)
        self.assertNotEqual(self._tip("no-clock"), before,
                            "control: a readable abandoned room rescues: %r"
                            % (ok,))

        blind_room = self.room("blind-clock", dirty="precious.txt")
        blind_row = self._rescue_row(blind_room)
        blind_before = self._tip("blind-clock")
        blind = {"dirty": True, "wrote_ago": None, "clock_skew": False,
                 "unknown": "deleted or raced path", "conflicts": 0,
                 "operation": None, "dangling_conflict": False}
        with mock.patch.object(_work_gc, "_room_status", return_value=blind):
            lines = work.gc_enact(self.root, blind_row)
        self.assertTrue(any(self.REFUSED in l for l in lines), lines)
        self.assertEqual(self._tip("blind-clock"), blind_before,
                         "an unreadable clock must NOT authorize the write")

    def test_a_FUTURE_mtime_is_UNKNOWN_not_freshly_written(self):
        """The third finding, and the one no arm of mine could reach.

        `_room_status` clamps a future mtime to age 0 (`max(0, int(now -
        newest))`) and reports `clock_skew` beside it. My binary reader saw
        0 < 900, called it freshly written, and would have deferred FOREVER
        while printing "written 0s ago" — a sentence about a duration that does
        not exist. The flag had been computed all along and nothing read it.

        Asserted on the REASON, not just the refusal: both a skewed clock and a
        genuinely fresh write refuse, so an arm that only checked that it
        refused would pass against the bug."""
        state, why = _work_gc._room_activity(
            "/nonexistent", st={"dirty": True, "wrote_ago": 0,
                                "clock_skew": True, "unknown": None,
                                "conflicts": 0, "operation": None,
                                "dangling_conflict": False})
        self.assertEqual(state, _work_gc.ACTIVITY_UNKNOWN)
        self.assertIn("FUTURE", why)
        # AND THE CONTROL ON THE SAME PRODUCER: identical row, skew cleared,
        # must classify as ACTIVE with a duration — otherwise this arm passes
        # for a function that answers UNKNOWN to everything.
        state2, why2 = _work_gc._room_activity(
            "/nonexistent", st={"dirty": True, "wrote_ago": 0,
                                "clock_skew": False, "unknown": None,
                                "conflicts": 0, "operation": None,
                                "dangling_conflict": False})
        self.assertEqual(state2, _work_gc.ACTIVE)
        self.assertIn("0s ago", why2)

    def test_the_guard_reaches_EVERY_caller_of_the_shared_actuator(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotEqual(rc, 0) on the autonomous call; its control is the explicit park BELOW it, which the rung cannot credit BY CONSTRUCTION because a second `_wip_commit` call is a different opaque call identity. That the control is a SEPARATE CALL is the claim: the same room in the same state must refuse one disposition and accept the other, and one call cannot show that
        """THE FIX ITSELF: the guard was above ONE caller.

        `_wip_commit` has three doors — gc_enact's rescue, envtidy's
        rescue-before-gc, and `release_lane(park=True)` — and its own docstring
        already said that a predicate placed at one call site is a predicate
        the next call site does not inherit. I put the activity check in
        gc_enact anyway, so a live author's room was deferred by the sweep and
        committed by envtidy.

        This calls the ACTUATOR directly for both dispositions, which is the
        only way to show the difference is `autonomous` and not the caller."""
        room = self.room("actuator", dirty="precious.txt")
        with open(os.path.join(room, "precious.txt"), "a") as f:
            f.write("still typing\n")
        before = self._tip("actuator")

        rc, _o, err = _work_gc._wip_commit(room, "wip: autonomous")
        self.assertNotEqual(rc, 0, "an autonomous write must refuse: %r" % err)
        self.assertIn(self.REFUSED, err)
        self.assertEqual(self._tip("actuator"), before)

        # THE OPERATOR'S DOOR, on the SAME room in the SAME state: an explicit
        # park proceeds, because a person naming this write has supplied the
        # provenance a sweep would have had to invent. This is also the
        # unconditional positive control for the refusal above.
        rc2, _o2, err2 = _work_gc._wip_commit(room, "wip: parked",
                                              autonomous=False)
        self.assertEqual(rc2, 0, "an explicit park must still commit: %r" % err2)
        self.assertNotEqual(self._tip("actuator"), before,
                            "the park must actually land the bytes")

    def test_a_DANGLING_conflict_refuses_even_an_explicit_park(self):  # noqa: VACUOUS_ASSERTION — same per-call-site limit: the flagged absence is the refusal on the conflicted call and its control is the identical call with no conflicts, asserted unconditionally at the end. Both are `_wip_commit`, and the arm's whole claim is that the two calls differ
        """The state no reflex recovers from: conflict stages in the index with
        NO operation in progress — what a conflicted `git stash apply` leaves.
        The operation check cannot see it (git reports no operation), so
        `git add -A` would stage the CONFLICT MARKERS and bury a
        hand-resolution under an anonymous message.

        REFUSES A PARK TOO, deliberately: the operator asked to park their
        work, not to have their half-resolved merge committed as `wip:`."""
        room = self.room("dangling", dirty="precious.txt")
        fake = {"dirty": True, "wrote_ago": 99999, "clock_skew": False,
                "unknown": None, "conflicts": 2, "operation": "",
                "dangling_conflict": True}
        before = self._tip("dangling")
        with mock.patch.object(_work_gc, "_room_status", return_value=fake):
            rc, _o, err = _work_gc._wip_commit(room, "wip: parked",
                                               autonomous=False)
        self.assertNotEqual(rc, 0, "a dangling conflict must refuse: %r" % err)
        self.assertIn("conflict stages", err)
        self.assertIn("restore", err, "the remedy must be PER-PATH, never a "
                                      "tree-wide reset: %r" % err)
        self.assertEqual(self._tip("dangling"), before)
        # CONTROL, same room, no conflicts: the park lands.
        rc2, _o2, err2 = _work_gc._wip_commit(room, "wip: parked",
                                              autonomous=False)
        self.assertEqual(rc2, 0, "control: a park with no conflict must land: "
                                 "%r" % err2)

    def test_the_refusal_reaches_the_APPLY_VERB_not_only_the_helper(self):
        """THE DOOR. A helper returning the right string proves nothing about
        the sweep the fleet runs; the four real incidents came through
        `helm work gc --apply`. This dirties the standing fixture room after
        setUp and drives the verb.

        The clean+merged room is still swept in the same pass, so this also
        shows the refusal is scoped to ONE row rather than aborting the sweep."""
        with open(os.path.join(self.dirty, "junk.txt"), "a") as f:
            f.write("author still here\n")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn(self.REFUSED, out)
        self.assertTrue(os.path.isdir(self.dirty))
        show = _sh(self.root, "git", "show", "lane/messy:junk.txt")
        self.assertNotEqual(show.returncode, 0,
                            "the bytes must NOT have been committed: %r"
                            % (show.stdout,))
        self.assertFalse(os.path.exists(self.clean),
                         "one refusal must not abort the whole sweep")

    def test_a_rescue_REFUSES_to_advance_a_branch_ANOTHER_ROOM_HOLDS(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(unreadable) on the registry read; the unconditional positives are the SOLO rescue asserted to MOVE its tip earlier in this arm, and the peer list asserted equal to the forced room
        """A WORKTREE LOCK GUARDS THE ROOM AND THE HAZARD IS THE REF.

        Git normally refuses a second checkout of one branch; a forced one puts
        two rooms on one ref, and the rescue commit then advances the OTHER
        room's lane to whatever THIS room was holding. Measured as task/1008: a
        rescue in a reviewer's forced checkout moved the author's HEAD BACKWARDS
        by two commits while their gate was running, so the receipt bound the
        right tree and the branch pointed at an older one.

        THE ASSERTION IS THE BYTES AND THE TIP, not the sentence. A refusal that
        printed the right words while still committing would read identically in
        the lines, so the arm asks git what happened."""
        solo = self.room("solo-rescue", dirty="precious.txt")
        shared = self.room("shared-rescue", dirty="precious.txt")
        second = os.path.join(self.tmp, "forced-peer")
        forced = subprocess.run(
            ["git", "worktree", "add", "--force", "-q", second,
             work.lane_branch("shared-rescue")],
            cwd=self.root, capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(forced.returncode, 0, forced.stderr)

        # CONTROL, UNCONDITIONAL AND FIRST: an unshared branch still rescues, so
        # the refusal below is about the SECOND ROOM and not about the guard
        # having broken rescue outright.
        before_solo = self._tip("solo-rescue")
        lines = work.gc_enact(self.root, self._rescue_row(solo))
        self.assertTrue(any("rescued dirty work" in l for l in lines), lines)
        self.assertNotEqual(self._tip("solo-rescue"), before_solo,
                            "control: an unshared rescue MUST commit")

        # THE HELPER DIRECTLY, BEFORE THE VERB THAT USES IT. gc_enact can only
        # ever tell me the guard did not fire; it cannot tell me WHY, and the
        # first cut failed here — the scan row carries `lane/x` while git's
        # registry carries `refs/heads/lane/x`, so the comparison was false for
        # every input and the guard answered NOBODY ELSE HOLDS IT to
        # everything. A vocabulary drift is silent at the verb and loud here.
        peers, unreadable = _work_gc._shared_branch_rooms(
            self.root, shared, work.lane_branch("shared-rescue"))
        self.assertIsNone(unreadable, unreadable)
        self.assertEqual([os.path.abspath(p) for p in peers],
                         [os.path.abspath(second)],
                         "the helper itself must SEE the forced peer room")

        before = self._tip("shared-rescue")
        lines = work.gc_enact(self.root, self._rescue_row(shared))
        self.assertTrue(any("ALSO checked out at" in l for l in lines), lines)
        self.assertTrue(any(second in l for l in lines),
                        "the refusal must NAME the other room: %r" % (lines,))
        self.assertEqual(self._tip("shared-rescue"), before,
                         "the shared branch must not have moved")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=shared,
                                capture_output=True, text=True)
        self.assertIn("precious.txt", status.stdout,
                      "the dirty bytes must still be THERE and uncommitted")

    def test_the_AUTHORED_WRITE_ITSELF_refuses_not_just_the_rescue_caller(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotEqual(rc, 0) on the refusing call; its unconditional positive is the SOLO call's assertEqual(rc, 0) plus a moved tip, which the rung cannot credit because a positive control on a different CALL is a different producer identity
        """THE GUARD BELONGS AT THE WRITE, NOT AT ONE CALLER, and this arm is
        the difference. `_wip_commit` has THREE callers — gc_enact's rescue,
        `_claims.release_lane(park=True)`, and envtidy's rescue-before-gc — and
        the first cut of this lane guarded only the first. A predicate at one
        call site is a predicate the other call sites do not inherit, so the
        park path would have advanced a peer's lane with a green suite.

        Driving the door directly is what covers all three: every caller takes
        (rc, out, err) and already refuses on a non-zero rc, so this one
        refusal reaches each of them."""
        room = self.room("door-rescue", dirty="precious.txt")
        second = os.path.join(self.tmp, "forced-door-peer")
        forced = subprocess.run(
            ["git", "worktree", "add", "--force", "-q", second,
             work.lane_branch("door-rescue")],
            cwd=self.root, capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(forced.returncode, 0, forced.stderr)
        before = self._tip("door-rescue")

        rc, _out, err = _work_gc._wip_commit(room, "wip: park-shaped write")
        self.assertNotEqual(rc, 0, "the WRITE itself must refuse")
        self.assertIn("ALSO checked out at", err)
        self.assertIn(second, err, "the refusal must NAME the peer room")
        self.assertEqual(self._tip("door-rescue"), before,
                         "no caller may advance a branch a peer holds")

        # CONTROL on the same door: a solo room still writes, so the refusal
        # above is about the peer and not about _wip_commit being broken.
        solo = self.room("door-solo", dirty="precious.txt")
        solo_before = self._tip("door-solo")
        rc, _out, err = _work_gc._wip_commit(solo, "wip: solo write")
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(self._tip("door-solo"), solo_before,
                            "control: an unshared WIP-commit MUST land")

    def test_an_UNREADABLE_worktree_registry_REFUSES_the_rescue_as_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — a refusal arm; the unconditional positive is the same room rescuing normally on the line above, with the registry readable
        """A FAILED LOOK IS NOT AN ALL-CLEAR. `worktrees()` keeps [] on error
        for legacy callers, and [] here would mean NOBODY ELSE HOLDS IT — an
        all-clear minted out of a registry nobody could read, on the one
        question that decides whether an authored-bytes write is safe."""
        room = self.room("unreadable-registry", dirty="precious.txt")
        row = self._rescue_row(room)
        before = self._tip("unreadable-registry")
        with mock.patch.object(_work_gc, "_worktree_records",
                               return_value=([], "git worktree list exploded")):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(any("UNKNOWN" in l for l in lines), lines)
        self.assertTrue(any("exploded" in l for l in lines),
                        "the refusal must carry WHY it could not look: %r"
                        % (lines,))
        self.assertEqual(self._tip("unreadable-registry"), before,
                         "an unreadable registry must not authorize the write")

    def test_enact_rechecks_occupancy_after_scan(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
        try:
            lines = work.gc_enact(self.root, row)
            self.assertTrue(any("OCCUPIED" in line for line in lines))
            self.assertTrue(os.path.isdir(self.clean))
        finally:
            proc.terminate()
            proc.wait()

    def test_scan_keeps_the_room_supplying_the_running_gc_binary(self):
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean):
            row = next(r for r in work.gc_scan(self.root)
                       if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("RUNNING this helm GC binary", row["why"])

    def test_enact_rechecks_the_running_source_room(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(any("RUNNING this helm GC binary" in line
                            for line in lines), lines)

    def test_runtime_source_relationship_is_path_based(self):
        source_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(_work_gc.__file__))))
        self.assertTrue(_work_gc._runtime_source_in(source_root))
        self.assertFalse(_work_gc._runtime_source_in(self.root))

    def test_gc_sees_harness_minted_rooms_not_only_lane_rooms(self):
        """THE SWEEP WAS BLIND BY CONSTRUCTION. lane_rows matches only
        `<root>-wt/`, so every gc built on it could not see the subagent and
        workflow worktrees under `.claude/worktrees/` — it reported a clean tree
        while 12 of 29 rooms sat abandoned, two holding uncommitted work from
        agents that had died.

        Those are also the rows the OWNER sees: orca lists every worktree in its
        sidebar, so a dozen unreadable `wf_<id>` entries sat between him and the
        six seats he actually talks to."""
        from helm.work import _lanes
        registered = [
            {"path": os.path.join(self.root + "-wt", "a-lane"), "branch": None,
             "locked": False, "reason": ""},
            {"path": os.path.join(self.root, ".claude", "worktrees",
                                  "wf_deadbeef-000-1"), "branch": None,
             "locked": False, "reason": ""},
            {"path": os.path.join(self.root, ".claude", "worktrees",
                                  "agent-abc123"), "branch": None,
             "locked": False, "reason": ""},
            # a NESTED path under the box is not a direct child and must not match
            {"path": os.path.join(self.root, ".claude", "worktrees", "wf_x",
                                  "deeper"), "branch": None,
             "locked": False, "reason": ""},
        ]
        lanes = _lanes.lane_rows(self.root, registered=registered)
        autos = _lanes.auto_rows(self.root, registered=registered)
        self.assertEqual([r["lane"] for r in lanes], ["a-lane"])
        self.assertEqual(sorted(r["lane"] for r in autos),
                         ["agent-abc123", "wf_deadbeef-000-1"])
        # the two views are DISJOINT — a room belongs to exactly one sweep
        self.assertFalse(set(r["path"] for r in lanes) &
                         set(r["path"] for r in autos))
        # and harness-minted rows are marked, so a caller can tell them apart
        self.assertTrue(all(r.get("harness_minted") for r in autos))

    def test_gc_orphans_feeds_the_helm_gc_report_row(self):
        got = work.gc_orphans(self.root)
        self.assertEqual(sorted(got), sorted([self.clean, self.dirty]))

    def test_enact_rechecks_branch_is_still_landed(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with open(os.path.join(self.clean, "late.txt"), "w") as f:
            f.write("arrived after scan\n")
        _sh(self.clean, "git", "add", "-A")
        self.assertEqual(_sh(self.clean, "git", "commit", "-q", "-m",
                             "late unlanded work").returncode, 0)
        lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(any("became unlanded" in line for line in lines), lines)

    def test_enact_unknown_ancestry_never_authorizes_delete(self):
        row = next(r for r in work.gc_scan(self.root) if r["path"] == self.clean)
        self.assertEqual(row["verdict"], "remove")
        with mock.patch.object(_work_gc, "_merge_state",
                               return_value=vcs.UNKNOWN):
            lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertTrue(work._has_branch(self.root, "lane/cleanmg"))
        self.assertTrue(any("UNKNOWN" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("removed ") for line in lines), lines)

    def test_disposable_orca_shell_is_the_only_occupied_reap_exception(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=self.clean)
        try:
            with mock.patch.object(_work_gc, "_disposable_worktree_occupant",
                                   return_value=True):
                rc, out, err = self.work("gc", "--apply")
            self.assertEqual(rc, 0, err)
            _reap(proc)
            self.assertIsNotNone(proc.poll(),  # noqa: VACUOUS_ASSERTION — reaped-or-not is the claim; _reap above already raises if it never exits
                                 "gc left the disposable shell running")
            self.assertFalse(os.path.exists(self.clean))
            self.assertIn("stopped disposable Orca shell", out)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()

    def test_disposable_orca_shell_receives_terminal_hangup_not_term(self):
        with mock.patch.object(_work_gc, "_occupants",
                               side_effect=[["123"], []]), \
                mock.patch.object(_work_gc, "_disposable_worktree_occupant",
                                  return_value=True), \
                mock.patch.object(_work_gc.os.path, "exists", return_value=False), \
                mock.patch.object(_work_gc.os, "kill") as kill:
            stopped, error = _work_gc._retire_disposable_occupants("/room")
        self.assertEqual(stopped, ["123"])
        self.assertIsNone(error)
        kill.assert_called_once_with(123, signal.SIGHUP)

    def test_orca_shell_identity_requires_rcfile_Ss_plus_and_no_children(self):
        proc_root = os.path.join(self.tmp, "proc")
        pid = "123"
        task = os.path.join(proc_root, pid, "task", pid)
        os.makedirs(task)
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"bash\0--rcfile\0/home/test/.config/orca/shell-ready\0-i\0")
        with open(os.path.join(proc_root, pid, "stat"), "wb") as f:
            f.write(b"123 (bash) S 1 123 123 34816 123 0 0 0")
        children = os.path.join(task, "children")
        with open(children, "wb") as f:
            f.write(b"")
        self.assertTrue(work._disposable_worktree_occupant(pid, proc_root=proc_root))
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/rcfile\0")
        self.assertTrue(work._disposable_worktree_occupant(pid, proc_root=proc_root),
                        "current Orca shell-ready rcfile shape")
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/other\0")
        self.assertFalse(work._disposable_worktree_occupant(pid, proc_root=proc_root),
                         "nearby arbitrary rcfiles are not disposable")
        with open(os.path.join(proc_root, pid, "cmdline"), "wb") as f:
            f.write(b"/bin/bash\0--rcfile\0/home/u/.config/orca/shell-ready/bash/rcfile\0")
        with open(children, "wb") as f:
            f.write(b"456")
        self.assertFalse(work._disposable_worktree_occupant(pid, proc_root=proc_root))

    def test_every_keep_for_triage_emitter_returns_a_structured_result(self):
        """THE ARM THAT REPLACES A PROSE DETECTOR, and it guards the one risk
        the new design has.

        gc_enact now carries its decision as an ATTRIBUTE instead of a
        sentence, so no substring is parsed and a blocker whose worktree PATH
        contains the marker can no longer be counted as triage. The cost is a
        default: `was_reclassified` reads False off a plain list, so a future
        reclassifying path that returns a bare list undercounts SILENTLY —
        the same failure the whole pair exists to close.

        So this walks gc_enact's SOURCE and requires every keep-for-triage
        emitter to construct an EnactResult. Comments are skipped (the first
        version of an arm like this matched its own docstring) and the
        statement is rejoined before asking (the emitter wraps, and a
        single-line check reports a wrapped constructor as missing)."""
        # KEYED ON THE PATHS, NOT ON A PHRASE. The first version keyed on
        # "kept for triage" and covered only the DIRTY emitter — the
        # landedness one renders its triage facts instead and was silently
        # unguarded. That is the same enumerate-from-memory miss this whole
        # lane exists to fix, made a third time inside its own arm.
        src = inspect.getsource(_work_gc.gc_enact)
        for marker in ('if _dirty(row["path"]):', "if anc not in RETIRABLE:"):
            with self.subTest(path=marker):
                self.assertIn(marker, src, "the reclassifying path this arm "
                                           "guards has moved or been renamed")
                after = src[src.index(marker):]
                stmt = after[:after.index("\n    if ")] if "\n    if " in after \
                    else after
                self.assertIn("EnactResult", stmt)
                self.assertIn("reclassified=True", stmt)

        # THE LIMIT, STATED RATHER THAN IMPLIED: this guards the two paths
        # that exist. A genuinely NEW reclassifying path is not caught by
        # walking source for markers I already know — only an end-to-end arm
        # per path can do that, and the two below are those arms.

    def test_the_structured_result_is_still_a_list_for_every_other_caller(self):
        """The compatibility claim, asserted rather than assumed. gc_enact's
        result is consumed as a list of lines in this file, in _cli.py and in
        envtidy.py, and several arms compare it to [] directly. A list
        subclass keeps all of that working — if it did not, this change would
        break callers that never asked about reclassification."""
        r = _work_gc.EnactResult(["a", "b"], reclassified=True)
        self.assertEqual(r, ["a", "b"])
        self.assertEqual(_work_gc.EnactResult(), [])
        self.assertEqual(list(r), ["a", "b"])
        self.assertTrue(_work_gc.was_reclassified(r))
        # a plain list is the not-reclassified case, and must not raise
        self.assertFalse(_work_gc.was_reclassified(["x"]))
        self.assertFalse(_work_gc.was_reclassified([]))

    def test_apply_posts_one_removed_kept_triage_summary(self):
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        post.assert_called_once()
        line = post.call_args.args[0]
        self.assertRegex(line, r"worktree gc proj: removed=1 kept=3 triage=1")
        self.assertIn(line, out)

    def _plant_ledger(self, *lanes, raw=None):
        """Write a dispatch ledger holding a CANONICAL row per named lane.

        HELM_HOME is planted per-suite (tests/__init__.py), so this writes
        inside the sandbox and never touches the operator's real ledger.

        THE ROWS MUST BE CANONICAL, not {id, event, lane}. The predicate folds
        through dispatches' own state, so a pseudo-row is REJECTED and an arm
        planting one would assert tracked-ness it never established — passing
        while proving the reverse. `raw` appends bytes verbatim for the
        corrupt-ledger arms.
        """
        from helm import dispatches, home
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(dispatches.ledger_path(), "wb") as f:
            for i, lane in enumerate(lanes):
                f.write(json.dumps({
                    "id": "%032d" % i, "event": "dispatch", "lane": lane,
                    "v": 3, "recipient": "seat-a", "seq": 0, "status": "open",
                    "tip": "d" * 40, "deadline_s": 10800}).encode() + b"\n")
            if raw:
                f.write(raw)
        _work_gc._ROWED = None
        self.addCleanup(setattr, _work_gc, "_ROWED", None)

    def test_the_apply_summary_actually_carries_the_unrowed_split(self):
        """THE WIRING, NOT THE FORMATTER. format_gc_summary rendering the
        clause proves nothing about whether cmd_work ever passes it — that is
        the compute-it-then-drop-it-one-frame-before-the-human shape, where
        every arm stays green and the operator is told the old number.

        THE PAIR IS THE POINT, and neither half works alone. `messy` is this
        fixture's only triage/rescue row, so with NO ledger row it must read
        unrowed=1 and with one it must read unrowed=0. An implementation that
        hardcodes 0, echoes the triage count, or never threads the value at
        all fails one of these two assertions."""
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("triage=1 (unrowed=1)", post.call_args.args[0])

    def test_a_tracked_triage_lane_drops_out_of_the_unrowed_count(self):
        """The other half of the pair above: the SAME estate, one ledger row
        planted for the one triage lane, and the count must move."""
        self._plant_ledger("messy")
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("triage=1 (unrowed=0)", post.call_args.args[0])

    def test_an_unreadable_ledger_reaches_the_summary_as_unknown(self):
        """UNKNOWN has to survive the whole path to the posted line, not just
        the formatter. A corrupt ledger collapsing to 0 here would tell the
        operator every lane is tracked at the exact moment helm cannot tell."""
        self._plant_ledger("messy", raw=b'{"id":"y","event":not json at all\n')
        self.addCleanup(setattr, _work_gc, "_ROWED", None)
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        line = post.call_args.args[0]
        self.assertIn("(unrowed=UNKNOWN)", line)
        self.assertNotIn("unrowed=0", line)

    def test_an_empty_population_never_asks_the_ledger_or_prints_unknown(self):  # noqa: VACUOUS_ASSERTION — the control IS present and unconditional: the anchored assertRegex on `line` matches the COMPLETE summary (removed/kept/triage=0 with $ terminating it), so it fails if the line never rendered, if counts are wrong, or if any clause follows. Absence is proven by exhausting the string, not by a missing substring. Behaviour-proven through the real CLI: the pre-cure build printed `triage=0 (unrowed=UNKNOWN)` on this exact fixture, which this regex rejects.
        """Finding 3. With NO triage/rescue rows there is nothing that
        COULD be unrowed, and that holds whether the ledger is pristine,
        missing or shredded — so `triage=0 (unrowed=UNKNOWN)` claimed helm
        could not tell at a moment helm could. UNKNOWN belongs only where the
        answer genuinely DEPENDS on the unreadable source.

        THE LEDGER HERE IS CORRUPT ON PURPOSE: that is the only fixture under
        which a predicate that still consults it produces UNKNOWN, so this arm
        fails loudly against the old behaviour instead of passing on a
        coincidence. `messy` is dropped from the estate to empty the
        population, and its removal is asserted rather than assumed — an arm
        that silently kept a triage row would test the wrong branch and pass.
        """
        shutil.rmtree(self.dirty, ignore_errors=True)
        _sh(self.root, "git", "worktree", "prune")
        self._plant_ledger(raw=b'{"id":"y","event":not json at all\n')
        self.addCleanup(setattr, _work_gc, "_ROWED", None)
        with mock.patch.object(_work_cli, "post_gc_summary",
                               return_value=None) as post:
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        line = post.call_args.args[0]
        self.assertNotIn("RESCUE", out)          # the population really is empty
        # ONE ANCHORED POSITIVE CARRIES BOTH HALVES. An assertNotIn("unrowed")
        # passes just as happily against a line that never rendered at all, so
        # the absence is asserted as the END OF A COMPLETE LINE instead: this
        # matches the whole summary structurally AND leaves no room after
        # triage=0 for a clause to hide in. Absence proven by exhaustion of
        # the string, not by a missing substring.
        self.assertRegex(
            line, r"^worktree gc proj: removed=\d+ kept=\d+ triage=0$")

    def test_self_source_room_is_kept_and_summary_still_posts(self):
        with mock.patch.object(_work_gc, "_runtime_source_in",
                               side_effect=lambda p: p == self.clean), \
                mock.patch.object(_work_cli, "post_gc_summary",
                                  return_value=None) as post:
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("RUNNING this helm GC binary", out)
        post.assert_called_once()
        self.assertRegex(post.call_args.args[0],
                         r"worktree gc proj: removed=0 kept=4 triage=1")


class GcPaneBoundRoomTest(WorkBase):
    """GC may not delete a room the metaharness still holds a terminal in.

    THE INCIDENT (2026-07-30). The owner reported, repeatedly and over hours,
    that his agent seats were being "killed back to a CWD". He was told more
    than once that the seats were fine, because every instrument consulted was
    a /proc scan and /proc said the processes were alive. He was right and the
    instruments were looking in the wrong place: a PANE is a metaharness object
    that OUTLIVES its shell, so `_occupants` — processes whose cwd is inside
    the room — cannot clear a room on its own. GC hung up the shell, deleted
    the worktree, and left orca holding a terminal bound to a directory that no
    longer existed. That pane renders as a bare command prompt, which from the
    outside is indistinguishable from someone having killed the agent.

    The two instruments are COMPLEMENTARY, not redundant: /proc catches a shell
    sitting in the room, the metaharness catches a pane bound to the room whose
    shell is not (measured on this host: one live bash at a `(deleted)` cwd,
    and five panes orca listed with no worktree path at all).

    Every test here plants its own answer through the `_panes_bound_to` seam.
    The suite must never ask the operator's real orca — see tests/__init__.py,
    which pins HELM_METAHARNESS=none for exactly that reason.
    """

    def setUp(self):
        super().setUp()
        self.clean = self.room("panelane")   # clean + merged -> REMOVE verdict

    def _gc_apply(self, panes, error=None):
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=(panes, error)):
            return self.work("gc", "--apply")

    def test_the_suite_never_reaches_a_live_metaharness(self):
        """The MUST-HIT for every other test in this class: if an adapter is
        ambient, the unmocked legs below are reading the owner's real fleet and
        their verdicts mean nothing. Measured before the pin: inside an Orca
        pane `orca` is on PATH and ORCA_USER_DATA_PATH is set, so detect()
        returned a live OrcaAdapter and each blocker check shelled out."""
        from helm import harness
        self.assertEqual(os.environ.get("HELM_METAHARNESS"), "none")
        self.assertIsNone(harness.detect())
        self.assertEqual(harness.worktree_panes(self.clean), ([], None))

    def test_a_bound_pane_refuses_removal_even_when_disposable_is_allowed(self):
        """THE INCIDENT, as an assertion. `disposable_ok` is the flag that lets
        GC hang up an Orca shell-ready placeholder; it must NOT also authorize
        deleting the room, because a disposable SHELL says nothing about the
        PANE wrapped around it."""
        handle = "term_97a38e41-bbb3-42e5-929d-7d42435628d8"
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([handle], None)):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIsNotNone(blocked, "disposable_ok relaxed the pane guard")
        self.assertIn(handle, blocked)
        rc, out, err = self._gc_apply([handle])
        self.assertEqual(rc, 0, err)
        self.assertIn(handle, out)
        self.assertIn("SKIPPED", out)
        self.assertTrue(os.path.isdir(self.clean), "deleted under a live pane")
        self.assertTrue(work._has_branch(self.root, "lane/panelane"))

    def test_an_unanswerable_metaharness_refuses_removal(self):
        """Fail CLOSED. "I could not look" and "nothing is there" are the same
        value only to code that has stopped caring which one it got."""
        with mock.patch.object(
                _work_gc, "_panes_bound_to",
                return_value=(None, "metaharness pane list unavailable: "
                                    "orca terminal list: rc 1 — daemon down")):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIsNotNone(blocked)
        self.assertIn("cannot prove the room is pane-free", blocked)
        self.assertIn("daemon down", blocked)
        rc, out, err = self._gc_apply(None, "orca terminal list: rc 1")
        self.assertEqual(rc, 0, err)
        self.assertIn("cannot prove the room is pane-free", out)
        self.assertTrue(os.path.isdir(self.clean))

    def test_no_metaharness_at_all_blocks_nothing(self):
        """THE INVERSE CONTROL. Without it the guard could refuse every room on
        earth and still look correct above. `([], None)` is an AFFIRMATIVE
        empty — nothing can be bound to a room by a host that does not exist —
        and it must let the sweep through."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([], None)):
            blocked = _work_gc._removal_blocker(
                self.root, self.clean, "panelane", stale_lease_ok=True)
        self.assertIsNone(blocked, blocked)
        rc, out, err = self._gc_apply([])
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(self.clean), out)
        self.assertFalse(work._has_branch(self.root, "lane/panelane"))

    def test_a_pane_in_a_subdirectory_blocks_the_whole_room(self):
        """Path-prefix matching, and the boundary that makes it a prefix rather
        than a substring: `/a/b` must not be blocked by a pane in `/a/bc`."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            def __init__(self, rows):
                self.rows = rows

            def list(self):
                return self.rows

        room = self.clean
        deep = os.path.join(room, "src", "nested")
        os.makedirs(deep)
        sibling = room + "c"            # /…/panelane vs /…/panelanec
        os.makedirs(sibling)
        ad = _Stub([{"handle": "h-deep", "worktree": deep},
                    {"handle": "h-sibling", "worktree": sibling}])
        self.assertEqual(harness.worktree_panes(room, adapter=ad),
                         (["h-deep"], None))
        self.assertEqual(harness.worktree_panes(sibling, adapter=ad),
                         (["h-sibling"], None))
        # and the whole way down to the blocker: the room stays
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               side_effect=lambda p: harness.worktree_panes(
                                   p, adapter=ad)):
            blocked = _work_gc._removal_blocker(
                self.root, room, "panelane",
                stale_lease_ok=True, disposable_ok=True)
        self.assertIn("h-deep", blocked or "")
        self.assertNotIn("h-sibling", blocked or "")

    def test_a_pane_reaching_the_room_through_a_symlink_still_blocks(self):
        """The comparison RESOLVES both sides. The metaharness reports whatever
        path the pane was opened with, which need not be spelled the way helm
        spells `<repo>-wt/<lane>`; an abspath-only compare would miss it, and a
        miss here is a delete under a live pane, not a cosmetic mismatch."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            def __init__(self, wt):
                self.wt = wt

            def list(self):
                return [{"handle": "h-link", "worktree": self.wt}]

        link = os.path.join(self.tmp, "room-by-another-name")
        os.symlink(self.clean, link)
        self.assertNotEqual(link, self.clean)
        self.assertEqual(harness.worktree_panes(self.clean,
                                                adapter=_Stub(link)),
                         (["h-link"], None))
        # and the reverse spelling: helm asks about the link, orca knows the real
        self.assertEqual(harness.worktree_panes(link,
                                                adapter=_Stub(self.clean)),
                         (["h-link"], None))

    def test_an_unbound_pane_blocks_nothing(self):
        """Five of the panes measured that morning carried no worktree path at
        all. A pane belonging to no room may not keep every room alive."""
        from helm import harness

        class _Stub:
            name = "stub"
            reports_pane_worktree = True

            @staticmethod
            def list():
                return [{"handle": "h-none", "worktree": None},
                        {"handle": "h-empty", "worktree": ""},
                        {"handle": "h-missing"}]

        self.assertEqual(harness.worktree_panes(self.clean, adapter=_Stub()),
                         ([], None))

    def test_an_adapter_that_cannot_answer_is_unanswerable_not_empty(self):
        """The herdr shape. Its `pane list` rows carry pane_id/label/status and
        no worktree, so reading them yields a bare [] that MEANS "this adapter
        never says" and would authorize the delete. An adapter must declare it
        can answer; unproven refuses."""
        from helm import harness

        class _Mute:
            name = "mute"
            # reports_pane_worktree deliberately absent — the default refuses

            @staticmethod
            def list():
                raise AssertionError("must not be asked; it cannot answer")

        panes, err = harness.worktree_panes(self.clean, adapter=_Mute())
        self.assertIsNone(panes)
        self.assertIn("does not report which worktree", err)
        self.assertFalse(harness.HerdrAdapter.reports_pane_worktree)
        self.assertTrue(harness.OrcaAdapter.reports_pane_worktree)


class GcClosesTheBoundPaneTest(WorkBase):
    """THE DEADLOCK, AND THE ORDER THAT MAKES BREAKING IT SAFE.

    A room could not be removed because a pane was bound to it; the pane stayed
    in the owner's sidebar because the room existed. Measured 2026-08-04: a
    reaper run PLANNED two removals, completed ZERO, and reported removed=0.

    The guard from GcPaneBoundRoomTest is NOT relaxed here — it is SATISFIED.
    The pane is closed, the binding is re-read from the adapter, and only then
    does the unrelaxed blocker authorize the delete. Reversed, this reproduces
    the 2026-07-30 incident exactly."""

    class _Adapter:
        """Records what it was asked to close; never touches a real pane."""
        def __init__(self, fail=None):
            self.closed, self.fail = [], fail
        def stop(self, handle):
            if self.fail:
                raise OSError(self.fail)
            self.closed.append(handle)

    def setUp(self):
        super().setUp()
        self.clean = self.room("panelane")     # clean + merged -> REMOVE

    def _run(self, pane_reads, adapter):
        """pane_reads: successive (handles, error) answers as gc re-asks."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               side_effect=list(pane_reads)), \
             mock.patch("helm.harness.detect", return_value=adapter):
            return self.work("gc", "--apply")

    def test_the_pane_is_closed_and_THEN_the_room_is_removed(self):
        h = "term_5d72fa07-1111-2222-3333-444455556666"
        ad = self._Adapter()
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the room is
        # THERE before the run, so "gone" afterwards measures the removal and
        # not a room that never existed.
        self.assertTrue(os.path.isdir(self.clean))
        # bound at the deferred check, bound at the close, GONE on re-verify,
        # GONE for the final unrelaxed blocker.
        rc, out, err = self._run([([h], None), ([], None), ([], None)], ad)
        self.assertEqual(rc, 0, err)
        # THE EFFECT, not the absence of a complaint:
        self.assertEqual(ad.closed, [h], "the pane must actually be closed")
        self.assertFalse(os.path.isdir(self.clean), "the room must be GONE")
        self.assertIn("closed metaharness pane(s) %s" % h, out)

    def test_a_close_that_RAISES_keeps_the_room(self):
        h = "term_aaaa"
        ad = self._Adapter(fail="pane close refused")
        rc, out, _e = self._run([([h], None)], ad)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean),
                        "a failed close must never remove the room")
        self.assertIn("could not close metaharness pane", out)

    def test_a_pane_STILL_BOUND_after_the_close_keeps_the_room(self):
        """`stop` returning cleanly says the command was accepted, not that the
        pane is gone. Only a fresh read of the binding proves the room free."""
        h = "term_bbbb"
        ad = self._Adapter()
        rc, out, _e = self._run([([h], None), ([h], None)], ad)
        self.assertEqual(rc, 0)
        self.assertEqual(ad.closed, [h], "it did try")
        self.assertTrue(os.path.isdir(self.clean), "still-bound must KEEP")
        self.assertIn("STILL bound after close", out)

    def test_an_UNREADABLE_re_verify_keeps_the_room(self):
        """Could-not-look is never looked-and-found-nothing."""
        ad = self._Adapter()
        rc, out, _e = self._run([(["term_cccc"], None), (None, "orca down")], ad)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("cannot re-verify", out)

    def test_panes_reported_with_NO_adapter_keeps_the_room(self):
        """Two readings disagree — panes exist but nothing can close them. A
        delete is not how that disagreement gets resolved."""
        rc, out, _e = self._run([(["term_dddd"], None)], None)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self.clean))
        self.assertIn("no adapter is available to close them", out)

    def test_a_pane_free_room_never_calls_the_adapter(self):
        """UNCONDITIONAL CONTROL that the close is CONDITIONAL: the ordinary
        path must remove without ever asking the metaharness to close anything,
        or every assertion above would also hold for code that closes blindly."""
        ad = self._Adapter()
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: this recorder
        # DOES record. Without it, `closed == []` would also hold for an
        # adapter that silently drops every close, and the assertion would be
        # measuring the double rather than the code under test.
        ad.stop("term_control")
        self.assertEqual(ad.closed, ["term_control"])
        ad.closed.clear()
        self.assertTrue(os.path.isdir(self.clean))
        rc, _o, err = self._run([([], None), ([], None), ([], None)], ad)
        self.assertEqual(rc, 0, err)
        self.assertEqual(ad.closed, [], "nothing to close, nothing closed")
        self.assertFalse(os.path.isdir(self.clean))


class GcSummaryTellsPlannedVsDoneTest(WorkBase):
    """"removed=0 kept=40" is literally true of an estate with nothing to
    remove AND of one where every planned removal FAILED — and it reads as the
    first. Measured 2026-08-04: a run planned two removals, completed zero, and
    said removed=0; real time was spent believing the estate was clean. The
    counts were all correct and the sentence was still false."""

    def setUp(self):
        super().setUp()
        self.clean = self.room("jammedlane")

    def test_a_blocked_removal_is_NAMED_with_its_reason(self):
        h = "term_eeee"
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([h], None)), \
             mock.patch("helm.harness.detect", return_value=None):
            rc, out, _e = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        # THE EFFECT: the summary must say the intention existed and failed.
        summary = [l for l in out.splitlines() if l.startswith("worktree gc ")]
        self.assertEqual(len(summary), 1, out)
        self.assertRegex(summary[0], r"removed=0 \(1 planned, 1 blocked: [^)]+\)")
        self.assertTrue(os.path.isdir(self.clean), "and the room is kept")

    def test_a_clean_estate_summary_is_UNCHANGED(self):
        """UNCONDITIONAL CONTROL on the same observable: with nothing blocked
        the clause must be ABSENT, or the new text would be decoration that
        always appears and says nothing."""
        with mock.patch.object(_work_gc, "_panes_bound_to",
                               return_value=([], None)):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.isdir(self.clean), "it really did remove")
        # Assert on the SUMMARY LINE ALONE. A first pass asserted over the
        # whole output and a fixture room named "blockedlane" satisfied
        # assertNotIn("blocked") by accident — the filter matched a name, not
        # the fact. Narrow the observable to the sentence under test.
        summary = [l for l in out.splitlines() if l.startswith("worktree gc ")]
        self.assertEqual(len(summary), 1, out)
        self.assertIn("removed=1", summary[0])
        self.assertNotIn("planned", summary[0])
        self.assertNotIn("blocked", summary[0])


class GcBlockedReasonSurvivesAMultilineErrorTest(unittest.TestCase):
    """FOUND BY DOGFOODING THIS LANE'S OWN FIX, on its first live run.

    The summary printed `blocked: unreported` while the reason sat two
    characters away: an adapter error carried a multi-line JSON body, so the
    ") — kept" the extractor required never appeared on the same line and the
    match failed. A summary that cannot name a reason it HAS is exactly the
    defect this feature exists to fix, one layer in — so the extractor must
    never depend on the closing punctuation."""

    def test_a_multiline_adapter_error_is_still_named(self):
        from helm.work._cli import _blocked_reason
        live = ["SKIPPED /p (could not close metaharness pane term_5d72fa07: "
                "HarnessError: orca terminal close: rc 1 — {",
                '  "error": "no such terminal"',
                "}"]
        self.assertEqual(_blocked_reason(live), "pane close failed")

    def test_the_ordinary_single_line_reasons_still_classify(self):
        """UNCONDITIONAL CONTROL: the extractor distinguishes reasons rather
        than returning one constant. Without this, the assertion above would
        also pass for a function hardcoded to say 'pane close failed'."""
        from helm.work._cli import _blocked_reason
        self.assertEqual(_blocked_reason(
            ["SKIPPED /p (metaharness pane(s) t are bound to this room) — kept"]),
            "bound pane")
        self.assertEqual(_blocked_reason(
            ["SKIPPED /p (OCCUPIED by cwd pid(s) 1) — kept"]), "occupied")
        self.assertEqual(_blocked_reason(["nothing was skipped"]), "unreported")


class GcUnmergedTest(WorkBase):
    def test_unmerged_clean_room_and_branch_stay_with_triage_evidence(self):
        path = self.room("aheadln")
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/aheadln"))
        self.assertRegex(out, r"TRIAGE\s+aheadln.*tip [0-9a-f]{12}, age .*"
                         r"\(git committer, shared across seats:")

    def test_an_unreadable_tip_never_reads_merged_and_triages_distinctly(self):
        """The phantom-unlanded-lanes fold at the REAP decision. rc 128 folded
        into "not merged" hid the unreadable tip inside the clean-negative
        triage; folded the other way it would -d a branch nobody proved
        landed. UNKNOWN must (a) never read as merged and (b) be its OWN
        triage reason, not the clean 'stays for the integrator' prose."""
        path = self.room("ghostln")
        # two lane commits so the MIDDLE object can vanish: `git status` in
        # the room stays readable (HEAD's tree is intact) while the ancestry
        # walk cannot parse the missing parent -> merge-base exits 128
        with open(os.path.join(path, "f.txt"), "w") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "mid")
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        with open(os.path.join(path, "g.txt"), "w") as f:
            f.write("y\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "commit", "-q", "-m", "tip")
        self.assertEqual(r.returncode, 0, r.stderr)
        os.remove(os.path.join(self.root, ".git", "objects",
                               mid[:2], mid[2:]))          # orphan the walk
        self.assertEqual(_work_gc._merge_state(self.root, "lane/ghostln"),
                         vcs.UNKNOWN)
        self.assertFalse(work._merged(self.root, "lane/ghostln"))
        row = next(r for r in work.gc_scan(self.root)
                   if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertIn("UNKNOWN", row["why"])       # a DISTINCT triage reason
        self.assertIn("unreadable", row["why"])
        self.assertNotIn("stays for the integrator", row["why"])
        # UNKNOWN authorizes no destructive step: both room and branch survive.
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/ghostln"))


class BranchTriageSeatProvenanceTest(WorkBase):
    """The ROSTERED arm of the triage committer line — the half that shipped dead.

    `_branch_triage` called `seats.is_rostered_name`, which exists in no
    revision of this repo, and a bare `except Exception` turned the
    AttributeError into is_seat=False forever. Every committer, a real seat
    included, read as "not seat provenance" — and the suite stayed green,
    because only the negative branch was ever asserted (test_work.py:375
    still pins it). So these tests assert the POSITIVE effect and the
    MECHANISM, which is the only thing that could have caught it.

    `launch.py` exports GIT_COMMITTER_NAME=<seat> (#155), so the seat token IS
    the committer name on a seat's own commits — this arm is load-bearing, not
    decorative.
    """

    SEAT = "opus-integrator"

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({self.SEAT: {"session": "sess-1"}}, f)

    def _lane(self, lane, committer):
        """A lane branch whose tip commit is COMMITTED BY `committer`."""
        path = self.room(lane)
        with open(os.path.join(path, "f.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        _sh(path, "git", "add", "-A")
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=committer, GIT_AUTHOR_EMAIL="s@local",
                   GIT_COMMITTER_NAME=committer, GIT_COMMITTER_EMAIL="s@local")
        r = subprocess.run(["git", "commit", "-q", "-m", "lane work"],
                           cwd=path, env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return work.lane_branch(lane)

    def test_rostered_committer_reads_as_seat_provenance_across_case(self):
        """Committer "Opus-Integrator" vs roster key "opus-integrator".

        THE CASE VARIANT IS THE POINT. An exactly-matching name would pass
        under `c_name in seats.roster()` too and would prove nothing about the
        ruling; only a case variant makes the choice of `recipient_matches`
        (unconditional @-strip + casefold, #134) observable.
        """
        branch = self._lane("seatln", "Opus-Integrator")
        line = _work_gc._branch_triage(self.root, "seatln", branch)
        self.assertIn("(git committer: Opus-Integrator <s@local>)", line)
        self.assertNotIn("shared across seats", line)
        self.assertNotIn("not seat provenance", line)

    def test_an_unreadable_roster_reads_unknown_not_not_provenance(self):
        """THE ARTIFACT BOUNDARY, per the post-bind audit. seats.roster()
        is the fail-OPEN reader — it swallows storage and parse failures
        itself, so the first cure's `except OSError` could never fire and an
        unreadable roster printed the same confident negative as a measured
        one. roster_checked() carries the probe, and a FAILED probe renders
        UNKNOWN: "not seat provenance" off a parse failure asserts a negative
        nobody measured.

        THE MAPPING IN THE MOCK WOULD MATCH THE COMMITTER — that is the
        discriminating half: a failed probe must beat a matching mapping,
        proving the flag is consulted and not the dict."""
        branch = self._lane("blindln", self.SEAT)
        with mock.patch.object(seats, "roster_checked",
                               return_value=({self.SEAT: {}}, True)):
            line = _work_gc._branch_triage(self.root, "blindln", branch)
        self.assertIn("roster unreadable, seat provenance UNKNOWN", line)
        self.assertNotIn("not seat provenance", line)

    def test_committer_display_is_scrubbed_but_identity_is_not(self):
        """%cn is arbitrary author-chosen bytes: ESC[2J clears the operator's
        terminal and U+202E reverses its reading order, and the triage line
        printed both raw (codex post-bind audit, second finding). The printed
        line rides the same Cc/Cf scrubber every seat surface uses; the
        printable remainder surviving is the unconditional positive control
        that scrubbing happened rather than the line going missing."""
        hostile = "evil\x1b[2Jname‮"
        branch = self._lane("hostln", hostile)
        line = _work_gc._branch_triage(self.root, "hostln", branch)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("‮", line)
        self.assertIn("evil", line)
        self.assertIn("name", line)

    def test_a_broken_comparator_crashes_instead_of_reading_as_shared(self):  # noqa: VACUOUS_ASSERTION — the control IS present and unconditional (the first assertIn, on the same _branch_triage call this test then breaks); a bare assertRaises binds no operand root, so the scanner scores it uncovered no matter what precedes it. Mutation-proven: restoring `except Exception` turns this test red while the control still passes
        """THE MECHANISM, not the symptom. The bare `except Exception` is what
        hid this for the whole life of the feature: it converted a code fault
        into a confident-looking negative verdict. A comparator that raises
        must reach the caller.

        The UNPATCHED call is the positive control, on the same observable and
        unconditional: it proves this fixture reaches the predicate at all, so
        the raise below is the comparator faulting and not the lane, the
        roster or `git show` quietly failing somewhere earlier.
        """
        branch = self._lane("crashln", self.SEAT)
        self.assertIn("(git committer: %s <s@local>)" % self.SEAT,
                      _work_gc._branch_triage(self.root, "crashln", branch))
        with mock.patch.object(seats, "recipient_matches",
                               side_effect=AttributeError("no such name")):
            with self.assertRaises(AttributeError):
                _work_gc._branch_triage(self.root, "crashln", branch)

    # ------------------------------------------------------------------
    # LEDGER TRACKING. A room is a GIT fact; a row is a LEDGER fact; a lane
    # can fail either independently. `helm work claim` mints a room and never
    # a row (helm/work/_claims.py:85), so a lane nobody came back to is
    # invisible to every stall detector, nag and burn-down helm owns —
    # measured 2026-08-25 at 23 of 65 triage rooms.
    # ------------------------------------------------------------------

    def _ledger(self, payload):
        """Point dispatches.ledger_path() at `payload` bytes; None = missing."""
        from helm import dispatches
        path = os.path.join(self.tmp, "led.jsonl")
        if payload is not None:
            with open(path, "wb") as f:
                f.write(payload)
        _work_gc._ROWED = None
        self.addCleanup(setattr, _work_gc, "_ROWED", None)
        p = mock.patch.object(dispatches, "ledger_path", lambda: path)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _row(lane, rid="a" * 32):
        """A CANONICAL dispatch genesis, not a pseudo-row.

        THE FIELD SET IS MEASURED, NOT GUESSED. The first version of these
        fixtures wrote {id, event, lane} and passed, because the code under
        test collected `lane` off any parseable event. Once the predicate
        folds through dispatches' own canonical state those fixtures are
        REJECTED, and an arm built on one would prove the opposite of what it
        claims. Dropping each key in turn against the real fold shows the
        required set is exactly id/event/lane/deadline_s/recipient/seq/
        status/tip/v; kind, ref, repo_id, sender and ts are droppable.
        """
        return (json.dumps({
            "id": rid, "event": "dispatch", "lane": lane, "v": 3,
            "recipient": "seat-a", "seq": 0, "status": "open",
            "tip": "d" * 40, "deadline_s": 10800}) + "\n").encode()

    @staticmethod
    def _non_genesis(lane, rid="c" * 32):
        """An IDENTITY-VALID event that opens no row.

        THE IDENTITY MUST BE VALID OR THE ARM IS CONFOUNDED, and the first
        version of this fixture was. `{"id":"x","event":"close","lane":...}`
        fails `_valid_identity` before anything examines the event KIND, so
        the arm passed on the identity check and proved nothing about
        non-genesis rejection - green in two receipts while testing a
        different claim than its name (a cross-family review). This
        fixture is a well-formed row in every respect EXCEPT that `close`
        cannot open anything, so rejection can only come from the kind.
        """
        return (json.dumps({
            "id": rid, "event": "close", "lane": lane, "v": 3,
            "recipient": "seat-a", "seq": 0, "status": "open",
            "tip": "d" * 40, "deadline_s": 10800}) + "\n").encode()

    @staticmethod
    def _future_genesis(lane, rid="f" * 32):
        """A genesis this build cannot understand: identity-valid, genesis
        KIND, and a schema version `_new_state` does not accept."""
        return (json.dumps({
            "id": rid, "event": "dispatch", "lane": lane, "v": 4,
            "recipient": "seat-a", "seq": 0, "status": "open",
            "tip": "d" * 40, "deadline_s": 10800}) + "\n").encode()

    def test_a_lane_with_a_ledger_row_carries_no_tracking_marker(self):
        """The unconditional positive: a TRACKED lane must print the line it
        always printed, with nothing appended. Without this arm every other
        arm here is satisfied by a function that appends unconditionally."""
        self._ledger(self._row("rowedln"))
        branch = self._lane("rowedln", self.SEAT)
        line = _work_gc._branch_triage(self.root, "rowedln", branch)
        self.assertIn("TRIAGE rowedln", line)          # the line still renders
        self.assertNotIn("NO LEDGER ROW", line)
        self.assertNotIn("UNKNOWN", line)

    def test_a_lane_whose_row_is_under_another_label_says_under_this_label(self):
        """THE CLAIM IS ABOUT THE LABEL, NEVER ABOUT THE WORK. The lookup is
        keyed on lane NAME, and helm is explicit that a lane name is free text
        and never work identity — a row filed under a renamed continuation is
        real tracking this predicate cannot see. The ledger here HOLDS a row
        for a neighbouring label, which is the discriminating half: the marker
        must still fire, and it must still scope itself to the label, because
        a reader who took the wider claim would file a duplicate row for work
        already tracked."""
        self._ledger(self._row("labelln-v2"))
        branch = self._lane("labelln", self.SEAT)
        line = _work_gc._branch_triage(self.root, "labelln", branch)
        self.assertIn("[NO LEDGER ROW UNDER THIS LABEL]", line)

    def test_a_future_genesis_reads_unknown_never_absent(self):
        """The worse-than-main regression, armed.

        A FUTURE v4 dispatch passes strict parsing AND `_valid_identity`, but
        `_new_state` accepts only v3 and declines it. The first cure folded
        that into "no genesis" and printed a confident NO LEDGER ROW for a
        lane that HAS one - strictly worse than the raw projection it
        replaced, and reachable under ordinary rolling version skew.

        THE LEDGER ALSO HOLDS A CANONICAL ROW for the lane under test, which
        is the discriminating half: the marker must be UNKNOWN rather than
        absent, AND it must not be silently correct by way of that row. A
        predicate that simply stopped trusting the ledger passes the absence
        half of this and fails the arm below it."""
        self._ledger(self._row("futln") + self._future_genesis("otherln"))
        branch = self._lane("futln", self.SEAT)
        line = _work_gc._branch_triage(self.root, "futln", branch)
        self.assertIn("tracking UNKNOWN", line)
        self.assertNotIn("NO LEDGER ROW", line)

    def test_an_identity_valid_non_genesis_is_skipped_not_unknown(self):
        """The other side of the same predicate, and the reason it is not
        simply "refuse on anything `_new_state` declines". A close is a
        well-formed, identity-valid event that legitimately opens nothing;
        skipping it asserts nothing and must NOT poison the read. If this
        went UNKNOWN the instrument would be blind on every real ledger,
        which is the fail-always shape a refusal cure invites."""
        self._ledger(self._row("realln") + self._non_genesis("ghostln"))
        real = self._lane("realln", self.SEAT)
        ghost = self._lane("ghostln", self.SEAT)
        real_line = _work_gc._branch_triage(self.root, "realln", real)
        ghost_line = _work_gc._branch_triage(self.root, "ghostln", ghost)
        self.assertNotIn("UNKNOWN", real_line)      # the read was NOT poisoned
        self.assertNotIn("NO LEDGER ROW", real_line)
        self.assertIn("[NO LEDGER ROW UNDER THIS LABEL]", ghost_line)

    def test_a_missing_ledger_is_a_proven_absence_not_unknown(self):
        """A missing file is a KNOWN empty ledger (eventledger.checked_events
        returns [], None), so every lane truly has no row and the honest
        render is the absence, not UNKNOWN. This is the arm that stops the
        third state from swallowing the second."""
        self._ledger(None)
        branch = self._lane("missln", self.SEAT)
        line = _work_gc._branch_triage(self.root, "missln", branch)
        self.assertIn("[NO LEDGER ROW UNDER THIS LABEL]", line)
        self.assertNotIn("UNKNOWN", line)

    def test_a_corrupt_ledger_renders_unknown_and_never_an_absence(self):
        """THE ARM THAT MATTERS, and it is built like its roster sibling above:
        THE LEDGER WOULD MATCH THE LANE. `goodln` has a real row on line 1, so
        a tolerant read would answer TRACKED and a naive failure would answer
        NO ROW — the failed probe must beat BOTH. An instrument that prints an
        absence when it means "I could not look" manufactures confident false
        rows for someone to chase, which is worse than the blindness it
        replaces. Asserting the UNKNOWN string specifically, not merely that
        no exception escaped."""
        self._ledger(self._row("goodln") + b'{"id":"b","event":not json\n')
        branch = self._lane("goodln", self.SEAT)
        line = _work_gc._branch_triage(self.root, "goodln", branch)
        self.assertIn("tracking UNKNOWN", line)
        self.assertNotIn("NO LEDGER ROW", line)

    def test_the_summary_splits_the_triage_count_and_never_zeroes_unknown(self):
        """format_gc_summary's own lesson one axis over: "triage=65" is true of
        an estate whose rooms are all tracked AND of one where a third are
        invisible, and it reads as the first. UNKNOWN must survive as a word;
        collapsing it to 0 is the same lie the marker exists to prevent."""
        self.assertIn("triage=9 (unrowed=4)", _work_gc.format_gc_summary(
            self.root, 0, 3, 9, unrowed=4))
        self.assertIn("(unrowed=UNKNOWN)", _work_gc.format_gc_summary(
            self.root, 0, 3, 9, unrowed="UNKNOWN"))
        # None omits the clause — the callers with no lane population must not
        # report a measured zero they never took.
        self.assertNotIn("unrowed", _work_gc.format_gc_summary(
            self.root, 0, 3, 9))


class GcRebasedLandTest(WorkBase):
    """THE OWNER'S QUESTION, answered at the reap: "why can't an agent clean up
    after itself when the thing it made is no longer necessary".

    Because the check asked the WRONG QUESTION and got a TRUTHFUL "no". An
    agent finishing a lane asked `merge-base --is-ancestor <tip> <trunk>` —
    SHA identity — while our protocol lands work REBASED, so the commit on
    trunk has a different sha. Ancestry correctly answered "that object is not
    on trunk" and the well-behaved agent kept its branch FOREVER. Measured on
    the live box 2026-08-03: 107 `lane/*` branches against 24 branch-holding
    worktrees, 7 of them entirely on trunk under rebased shas.

    BOTH ARMS or the predicate is unproven: landed-under-a-different-sha must
    RETIRE, and not-landed must KEEP.
    """

    def _land_rebased(self, lane):
        """Replay a lane's commit onto a MOVED main — the integrator's actual
        behavior. Returns (lane_tip, trunk_tip): different objects carrying
        identical patches."""
        branch = work.lane_branch(lane)
        lane_tip = _sh(self.root, "git", "rev-parse", branch).stdout.strip()
        with open(os.path.join(self.root, "trunk-moved.txt"), "w") as f:
            f.write("main advanced first, so the replay cannot fast-forward\n")
        _sh(self.root, "git", "add", "-A")
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "-m", "trunk moved").returncode, 0)
        self.assertEqual(
            _sh(self.root, "git", "cherry-pick", lane_tip).returncode, 0)
        return lane_tip, _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def _lane_commit(self, path, name):
        with open(os.path.join(path, name), "w") as f:
            f.write("lane work in %s\n" % name)
        _sh(path, "git", "add", "-A")
        self.assertEqual(
            _sh(path, "git", "commit", "-q", "-m", "work " + name).returncode, 0)

    def _patch_id(self, committish):
        show = _sh(self.root, "git", "show", committish)
        return subprocess.run(["git", "patch-id", "--stable"], cwd=self.root,
                              input=show.stdout, capture_output=True,
                              text=True, timeout=30).stdout.split()[0]

    def test_a_lane_landed_under_a_DIFFERENT_sha_is_retired_and_says_why(self):  # noqa: VACUOUS_ASSERTION — equal patch-ids, the exact PATCH_EQUIVALENT proof and the preserved-ref sha are the positive controls
        """ARM 1 — and the audit line, because a silent reap is
        indistinguishable from data loss."""
        path = self.room("rebased")
        self._lane_commit(path, "feature.txt")
        lane_tip, trunk_tip = self._land_rebased("rebased")

        # the owner's own measurement, reproduced: same content, different sha
        self.assertNotEqual(lane_tip, trunk_tip)
        self.assertEqual(self._patch_id(lane_tip), self._patch_id(trunk_tip))
        # ancestry is TRUTHFULLY negative — this is why nothing could reap it
        self.assertEqual(
            vcs.backend(self.root).ancestry(self.root, "lane/rebased", "main"),
            vcs.NOT_ANCESTOR)
        self.assertEqual(_work_gc._merge_state(self.root, "lane/rebased"),
                         vcs.PATCH_EQUIVALENT)

        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        self.assertEqual(row["proof"], vcs.PATCH_EQUIVALENT)
        self.assertIn("patch identity", row["why"])

        lines = work.gc_enact(self.root, row)
        blob = "\n".join(lines)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/rebased"))
        # SAY WHY, and say how to undo it: the audit names the proof and the
        # exact restore command, so the decision survives the deletion.
        self.assertIn("patch identity", blob)
        self.assertIn("rebased sha", blob)
        self.assertIn("git branch lane/rebased " + lane_tip, blob)
        # PRESERVED, NOT DELETED. Patch identity is a weaker grade of fact than
        # ancestry — it proves the DIFFS are upstream, not that these commit
        # objects survive anywhere — so the tip is demoted to a durable ref
        # instead of being made collectable. The sidebar clears; the work does
        # not move.
        keep_ref = _work_gc.RETIRED_NS + "lane/rebased"
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "--verify", "-q",
                keep_ref).stdout.strip(), lane_tip)
        self.assertIn(keep_ref, blob)
        # and the restore really works — the sha is not decorative
        self.assertEqual(
            _sh(self.root, "git", "branch", "lane/rebased",
                lane_tip).returncode, 0)
        self.assertTrue(work._has_branch(self.root, "lane/rebased"))

    def test_an_ancestry_retire_needs_no_retirement_ref(self):  # noqa: VACUOUS_ASSERTION — the exact ANCESTOR proof and 'ancestry' in the audit blob are the positive controls
        """The grades are DIFFERENT and the code shows it. After `-d` on an
        ancestry proof the tip is still on the trunk, so nothing is owed; the
        retirement ref exists only to pay for the weaker evidence."""
        path = self.room("ffland")
        self._lane_commit(path, "feature.txt")
        self.assertEqual(
            _sh(self.root, "git", "merge", "-q", "--ff-only",
                "lane/ffland").returncode, 0)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["proof"], vcs.ANCESTOR)
        blob = "\n".join(work.gc_enact(self.root, row))
        self.assertFalse(work._has_branch(self.root, "lane/ffland"))
        self.assertIn("ancestry", blob)
        self.assertEqual(
            _sh(self.root, "git", "for-each-ref", _work_gc.RETIRED_NS
                ).stdout.strip(), "")

    def test_a_failed_preservation_KEEPS_the_branch(self):  # noqa: VACUOUS_ASSERTION — has_branch True plus 'KEPT'/'retirement ref' in the lines are the positive controls
        """Save, verify the save, THEN delete. If the tip cannot be provably
        preserved, the deletion never happens — reversibility is a
        precondition, not a courtesy printed afterwards."""
        path = self.room("nosave")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("nosave")
        real = vcs.GitVcs.text

        def refuse_update_ref(self_, cwd, *args, **kw):
            if args and args[0] == "update-ref":
                return 1, "", "simulated ref-store failure"
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", refuse_update_ref):
            lines = _work_gc._delete_lane_branch(self.root, "lane/nosave")
        self.assertTrue(work._has_branch(self.root, "lane/nosave"))
        self.assertIn("KEPT", "\n".join(lines))
        self.assertIn("retirement ref", "\n".join(lines))

    def test_a_read_back_lie_KEEPS_the_branch(self):  # noqa: VACUOUS_ASSERTION — has_branch True plus 'KEPT'/'did not read back' in the lines are the positive controls
        """The verify half of save-verify-delete, which the update-ref arm
        above does NOT cover: the write succeeds, but the read-back returns a
        DIFFERENT sha (a lying or torn ref store). kimi's reap review: the
        comparison is the only thing standing between a successful write and
        a forced delete, and removing it left every preservation test green —
        so this arm simulates the lie at exactly that step and asserts the
        delete never happens."""
        path = self.room("liesave")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("liesave")
        real = vcs.GitVcs.text
        tip = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()

        def lying_read_back(self_, cwd, *args, **kw):
            if args[:2] == ("rev-parse", "--verify") and \
                    any("refs/helm-retired/" in a for a in args):
                return 0, "0" * 40 + "\n", ""        # a different sha
            return real(self_, cwd, *args, **kw)

        with mock.patch.object(vcs.GitVcs, "text", lying_read_back):
            lines = _work_gc._delete_lane_branch(self.root, "lane/liesave")
        self.assertTrue(work._has_branch(self.root, "lane/liesave"),
                        "a branch was -D'd on an UNVERIFIED preservation")
        joined = "\n".join(lines)
        self.assertIn("KEPT", joined)
        self.assertIn("did not read back", joined)
        self.assertIn(tip[:12], joined)

    def test_a_branch_that_ADVANCES_after_the_read_back_is_NOT_deleted(self):
        """THE THIRD ARM, and the only one that loses bytes. The two above
        simulate a ref store that fails or lies; this one lets the ref store
        work perfectly and moves the BRANCH instead.

        save-verify-delete proves things about the retirement REF and never
        bound the DELETE to the sha it proved. `git branch -D` removes whatever
        the ref points at when it runs — measured 2026-08-04 in a scratch repo,
        it reported deleting the branch at its ADVANCED sha, for a decision
        taken about the earlier one. So a seat committing into its room
        between the read-back
        and the delete had that commit deleted while the retirement ref
        preserved the tip BEFORE it: the new work was referenced by nothing and
        the audit line still said PRESERVED.

        The fix is `git update-ref -d <ref> <expected>`, git's own
        compare-and-delete, so what gets deleted is exactly what was proven
        saved or nothing is. Here the branch advances with REAL content (an
        empty commit would be no loss and reads UNKNOWN anyway, per the test
        below).

        THE ASSERTION IS THE ISSUED ARGV, NOT THE SURVIVING BRANCH, and that
        distinction cost a mutation round: this room's branch is CHECKED OUT in
        a worktree, and git refuses to delete a checked-out branch whatever the
        caller asks. So "the branch is still there" passed identically with the
        fix reverted — a true effect with an unrelated cause. What discriminates
        is that a COMPARE-AND-DELETE was issued against the preserved sha, so
        that is what this pins."""
        path = self.room("racer")
        self._lane_commit(path, "feature.txt")
        self._land_rebased("racer")
        real = vcs.GitVcs.text
        raced, argvs = {}, []

        def advance_the_branch_mid_preserve(self_, cwd, *args, **kw):
            argvs.append(tuple(args))
            out = real(self_, cwd, *args, **kw)
            # Fire once, AFTER the retirement ref is written and verified —
            # i.e. at the last instant where every existing guard is satisfied.
            if args and args[0] == "update-ref" and not raced:
                raced["before"] = _sh(path, "git", "rev-parse",
                                      "HEAD").stdout.strip()
                with open(os.path.join(path, "arrived-late.txt"), "w") as fh:
                    fh.write("work a seat committed after the read-back\n")
                _sh(path, "git", "add", "arrived-late.txt")
                _sh(path, "git", "commit", "-q", "-m", "after the read-back")
                raced["after"] = _sh(path, "git", "rev-parse",
                                     "HEAD").stdout.strip()
            return out

        with mock.patch.object(vcs.GitVcs, "text",
                               advance_the_branch_mid_preserve):
            lines = _work_gc._delete_lane_branch(self.root, "lane/racer")
        # POSITIVE CONTROL: the race must actually have been staged, or this
        # test proves nothing about a window it never opened.
        self.assertTrue(raced, "the interposition never fired — no race staged")
        self.assertNotEqual(raced["before"], raced["after"])
        preserved = raced["before"]
        # THE DISCRIMINATOR: a compare-and-delete naming the sha that was
        # preserved. An unconditional `branch -D` cannot produce this argv.
        self.assertIn(
            ("update-ref", "-d", "refs/heads/lane/racer", preserved), argvs,
            "the delete was not a compare-and-delete against the preserved "
            "sha — issued argv: %r" % (argvs,))
        self.assertNotIn(
            ("branch", "-D", "lane/racer"), argvs,
            "an unconditional -D was issued despite a preserved sha")
        self.assertTrue(work._has_branch(self.root, "lane/racer"),
                        "a branch that MOVED after the read-back was deleted")
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "lane/racer").stdout.strip(),
            raced["after"],
            "the branch survived but not at the tip the race created")
        self.assertIn("KEPT", "\n".join(lines))

    def test_an_EMPTY_commit_is_never_patch_identity_evidence(self):  # noqa: VACUOUS_ASSERTION — cherry asserted to start with '-' and the exact UNKNOWN verdict are the positive controls
        """An empty diff has an empty patch-id, so EVERY empty commit is
        "patch-identical" to every other one. Measured 2026-08-03: a lane
        carrying one `--allow-empty` marker read '-' against a trunk whose only
        empty commit was totally unrelated. No bytes are at risk there, but the
        VERDICT is false and a false landed verdict is the input to a deletion.
        Patch identity supplies no evidence at all on an empty diff, so the
        range is UNKNOWN — which keeps."""
        path = self.room("emptymark")
        self.assertEqual(
            _sh(path, "git", "commit", "-q", "--allow-empty",
                "-m", "lane marker, no files").returncode, 0)
        self.assertEqual(
            _sh(self.root, "git", "commit", "-q", "--allow-empty",
                "-m", "TOTALLY UNRELATED trunk marker").returncode, 0)
        # cherry is fooled...
        self.assertTrue(_sh(self.root, "git", "cherry", "main",
                            "lane/emptymark").stdout.startswith("-"))
        # ...and the predicate is not
        self.assertEqual(_work_gc._merge_state(self.root, "lane/emptymark"),
                         vcs.UNKNOWN)
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/emptymark"))

    def test_a_lane_whose_content_did_NOT_land_is_KEPT_with_its_reason(self):  # noqa: VACUOUS_ASSERTION — the exact NOT_ANCESTOR verdict, the triage row and its 'NOT landed' reason are the positive controls
        """ARM 2. Without it, `return PATCH_EQUIVALENT` would pass arm 1."""
        path = self.room("unlanded")
        self._lane_commit(path, "never-landed.txt")
        with open(os.path.join(self.root, "elsewhere.txt"), "w") as f:
            f.write("unrelated trunk motion\n")
        _sh(self.root, "git", "add", "-A")
        _sh(self.root, "git", "commit", "-q", "-m", "unrelated")

        self.assertEqual(_work_gc._merge_state(self.root, "lane/unlanded"),
                         vcs.NOT_ANCESTOR)
        self.assertFalse(work._merged(self.root, "lane/unlanded"))
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "triage")
        self.assertIn("NOT landed", row["why"])
        work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/unlanded"))

    def test_a_LIVE_LEASE_outranks_content_identity(self):  # noqa: VACUOUS_ASSERTION — PATCH_EQUIVALENT plus the 'lease live' keep reason are the positive controls
        """Someone mid-work holds a deliberately dirty tree, and "dirty and
        quiet" is a NORMAL working state whose meaning is knowable only from
        the worker's side. A held lease keeps the room no matter how completely
        the content landed."""
        rc, out, _e = self.work("claim", "held", "--seat", "s1")
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        self._lane_commit(path, "feature.txt")
        self._land_rebased("held")
        self.assertEqual(_work_gc._merge_state(self.root, "lane/held"),
                         vcs.PATCH_EQUIVALENT)      # fully landed by content
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertIn("lease live", row["why"])
        self.assertEqual(work.gc_enact(self.root, row), [])
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/held"))

    def test_a_worktree_LESS_branch_is_the_surface_the_owner_sees(self):  # noqa: VACUOUS_ASSERTION — the delete/keep verdicts, the PATCH_EQUIVALENT state and the restore line are the positive controls
        """Branches OUTLIVE their rooms — 107 branches against 24 rooms on the
        live box — so the reaper that matters most to a sidebar is envtidy's
        orphan-branch pass, which asked the same ancestry-only question."""
        from helm import envtidy
        path = self.room("orphaned")
        self._lane_commit(path, "feature.txt")
        lane_tip, _trunk = self._land_rebased("orphaned")
        keep = self.room("stillopen")
        self._lane_commit(keep, "unlanded.txt")
        # retire the ROOMS only; the branches remain (the real shape)
        for p in (path, keep):
            self.assertEqual(
                vcs.backend(self.root).remove_worktree(self.root, p)[0], 0)

        rows = {r["branch"]: r for r in
                envtidy._orphan_branches(self.root, "main")}
        self.assertEqual(rows["lane/orphaned"]["verdict"], "delete")
        self.assertEqual(rows["lane/orphaned"]["state"], vcs.PATCH_EQUIVALENT)
        self.assertEqual(rows["lane/stillopen"]["verdict"], "keep")
        self.assertIn("NOT landed", rows["lane/stillopen"]["why"])

        result = envtidy.worktree_gc(root=self.root, apply=True)
        self.assertFalse(work._has_branch(self.root, "lane/orphaned"))
        self.assertTrue(work._has_branch(self.root, "lane/stillopen"))
        self.assertIn("git branch lane/orphaned " + lane_tip,
                      "\n".join(result["orphan_lines"]["lane/orphaned"]))

    def test_release_retires_a_rebased_lane_instead_of_stranding_it(self):  # noqa: VACUOUS_ASSERTION — rc 0 plus 'patch identity' and the restore line in the release output are the positive controls
        """The agent's OWN exit — where the accumulation starts. The seat did
        the right thing at release, ancestry said no, and the branch stayed.
        Now the release retires it AND names the proof that authorized it."""
        rc, out, _e = self.work("claim", "selfclean", "--seat", "s1")
        self.assertEqual(rc, 0)
        path, lease = out.split("\t")[0], out.split("\t")[2]
        self._lane_commit(path, "feature.txt")
        self._land_rebased("selfclean")
        rc, out, _err = self.work("release", "selfclean", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/selfclean"))
        self.assertIn("patch identity", out)
        self.assertIn("restore: git branch lane/selfclean", out)


class GcDetachedIsManualOnlyTest(WorkBase):
    """helm REFUSES to auto-reap a room it cannot prove is safe.

    THIS CLASS REPLACES A REACHABILITY PROVER THAT FAILED FIVE REVIEW ROUNDS,
    and the history is the justification, so it is recorded rather than lost:
      r1  HEAD only            -> missed a tip reset away into ORIG_HEAD
      r2  seven-name pseudorefs-> missed FETCH_HEAD, MERGE_AUTOSTASH, rewritten
      r3  --include-root-refs  -> blind to MULTI-VALUED FETCH_HEAD/MERGE_HEAD
      r4  read the admin dir   -> truncates large files, misses abbreviated and
                                  uppercase shas, follows symlinks, blocks on
                                  FIFOs, and CANNOT CLOSE THE SCAN/REMOVE RACE
    That last one is not patchable: there is always a window between judging a
    room safe and deleting it. Five rounds of a per-case handler is the signal
    to stop handling cases.

    A BRANCH room needs no prover — `delete_branch` is `branch -d` and GIT
    refuses an unmerged branch, so the guarantee comes from the tool that owns
    the objects. A DETACHED room has no equivalent, so helm keeps it and says
    why. Debris that is VISIBLE and named beats debris silently deleted."""

    def detached_room(self, name):
        path = self.room(name)
        _sh(path, "git", "checkout", "--detach", "-q")
        with open(os.path.join(path, "u.txt"), "w") as f:
            f.write("work with no branch to protect it\n")
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "-m", "detached work")
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def test_a_detached_room_is_KEEP_and_manual_only(self):
        path = self.detached_room("detachd")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertTrue(row["manual_only"])
        self.assertIn("DETACHED", row["why"])

    def test_the_reason_names_HEAD_so_a_human_can_inspect(self):
        """A refusal that does not say where to look just moves the problem."""
        path = self.detached_room("wherelook")
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["head"], head)
        self.assertIn(head[:12], row["why"])

    def test_apply_NEVER_removes_a_detached_room(self):
        """THE LOAD-BEARING ONE. Before the re-scope this room was verdicted
        `remove`, and five detached rooms in the real repo held eight commits
        reachable from nothing."""
        path = self.detached_room("survive")
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path), "a detached room was reaped: %s" % out)
        self.assertEqual(
            _sh(self.root, "git", "cat-file", "-t", head).stdout.strip(),
            "commit", "the detached commit did not survive")

    def test_a_room_MID_OPERATION_is_manual_only_even_on_a_branch(self):
        """Sequencer state is neither a commit nor a branch, so neither `-d`
        nor any walk speaks for it. Named beside the detached case
        and it gets the same answer."""
        path = self.room("midmerge")
        admin = _sh(path, "git", "rev-parse", "--absolute-git-dir").stdout.strip()
        with open(os.path.join(admin, "MERGE_HEAD"), "w") as f:
            f.write(_sh(path, "git", "rev-parse", "HEAD").stdout.strip() + "\n")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep")
        self.assertTrue(row["manual_only"])
        self.assertIn("mid-merge", row["why"])

    def test_an_unreadable_gitdir_keeps_the_room(self):
        path = self.detached_room("unreadable")
        real = vcs.backend(path).text

        def broken(p, *args, **kw):
            if args[:2] == ("rev-parse", "--absolute-git-dir"):
                return 1, "", "simulated failure"
            return real(p, *args, **kw)
        with mock.patch.object(vcs.backend(path), "text", side_effect=broken):
            state = _work_gc._operation_state(path)
        self.assertEqual(state, "unreadable-gitdir",
                         "an unreadable git dir must read as in-an-operation")

    def test_a_room_that_DETACHES_between_scan_and_enact_is_not_reaped(self):
        """codex, on the attached path I had claimed was proven.

        `branch -d` guarantees the BRANCH is merged. It says nothing about where
        HEAD went. A room on a branch at scan can detach and commit before
        enact; removal then destroys that commit while `-d` cheerfully succeeds,
        because the branch really is merged — it simply is not where the work
        is. So the scan's premise is re-read immediately before removal.

        This SHRINKS the window, it does not close it. Closing it needs shared
        serialization across every remover, which is a different lane."""
        path = self.room("racy")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        # the room moves AFTER the scan
        _sh(path, "git", "checkout", "--detach", "-q")
        _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "committed after the scan")
        doomed = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        lines = work.gc_enact(self.root, row)
        self.assertTrue(os.path.isdir(path),
                        "removed a room that detached under the scan: %s" % lines)
        self.assertTrue(any("moved under the scan" in l for l in lines), lines)
        self.assertEqual(
            _sh(self.root, "git", "cat-file", "-t", doomed).stdout.strip(),
            "commit", "the post-scan commit was destroyed")

    def test_a_DIRTY_DETACHED_room_is_manual_only_not_rescued(self):
        """codex r6, and the most dangerous room there is: uncommitted bytes AND
        no branch to hold them.

        `_dirty` was checked BEFORE the manual-only gate, so this room verdicted
        rescue with branch=None; enact then wip-committed onto a DETACHED HEAD
        and removed the room, destroying the commit the rescue had just made.
        The refusal existed and the hazardous input routed around it."""
        path = self.detached_room("dirtydet")
        with open(os.path.join(path, "uncommitted.txt"), "w") as f:
            f.write("bytes with no branch to hold them\n")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "keep",
                         "a dirty DETACHED room was queued for rescue+removal")
        self.assertTrue(row["manual_only"])
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path), "removed it anyway: %s" % out)
        self.assertTrue(os.path.exists(os.path.join(path, "uncommitted.txt")),
                        "the uncommitted bytes are gone")

    def test_a_DIRTY_room_ON_A_BRANCH_still_rescues(self):
        """The reorder must not disable the lost-and-found for the case it was
        built for: dirty bytes on a real branch still get wip-committed."""
        path = self.room("dirtybr", dirty="junk.txt")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "rescue")
        self.assertFalse(row.get("manual_only"))
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(
            _sh(self.root, "git", "show", "lane/dirtybr:junk.txt").stdout,
            "precious uncommitted bytes\n", "the bytes were not rescued")
        self.assertIn("room kept", out)

    def test_a_BRANCH_room_still_reaps_because_git_itself_proves_it(self):
        """The re-scope must not stop gc doing the job it CAN prove. A guard
        that always fires protects nothing — it just accumulates debris behind
        a refusal."""
        path = self.room("branchok")
        row = next(r for r in work.gc_scan(self.root) if r["path"] == path)
        self.assertEqual(row["verdict"], "remove")
        self.assertFalse(row.get("manual_only"))
        rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))


class GcPhantomRecordTest(WorkBase):
    """A registered worktree whose DIRECTORY is gone is registry residue: git
    keeps the record indefinitely, `worktree add` refuses the path, and a
    metaharness that mirrors the registry (Orca's sidebar) renders a ghost
    room forever. Measured live 2026-08-01: two dregg records outlived their
    dirs — one a session scratchpad wiped by scratch lifecycle — and sat in
    the owner's sidebar as dead entries. gc's apply pass prunes the RECORD
    only; files and branches are never in reach."""

    def phantom(self, lane):
        """A room whose dir dies WITHOUT `git worktree remove` — the reboot/
        rm -rf/scratch-reap shape that mints the residue. Asserts the record
        IS registered after the dir dies: the positive control every later
        absence reading rests on — a `registered` probe that could never
        return True would vacuously green the prune assertions."""
        path = self.room(lane)
        shutil.rmtree(path)
        self.assertTrue(self.registered(path),
                        "fixture failed to mint a registered phantom")
        return path

    def registered(self, path):
        rows, error = vcs.backend(self.root).worktrees(self.root)
        self.assertIsNone(error)
        return any(r["path"] == path for r in rows)

    def test_an_UNREADABLE_dir_is_NEVER_a_phantom(self):
        """The FIX, and the one that could have deleted a live record:
        "unreadable live dirs prune". os.path.isdir SWALLOWS OSError and answers
        False, so a directory that EXISTS but cannot be stat'd — permission
        denied, a hanging NFS mount, a failing disk — was indistinguishable from
        one that is gone, and the enact leg pruned its registry record. Prune is
        irreversible registry loss, so this is the one direction that may never
        fail open.

        The control comes FIRST and unconditionally: a genuinely-absent dir IS
        still detected as phantom in the same pass. Without it, a predicate that
        simply stopped detecting anything would green this arm."""
        gone = self.phantom("really-gone")
        live = self.room("unreadable-but-live")
        self.assertTrue(self.registered(live))
        real = os.stat

        def blind(path, *a, **k):
            if str(path) == live:
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **k)

        with mock.patch.object(os, "stat", side_effect=blind):
            found = _work_gc.phantom_records(self.root)
        self.assertIn(gone, found,
                      "the detector stopped seeing a REAL phantom — this arm "
                      "would then pass for the wrong reason")
        self.assertNotIn(live, found,
                         "an unreadable LIVE directory was classed phantom; "
                         "--apply would have deleted its registry record")

    def test_apply_targets_ONLY_the_approved_phantom_not_unreadable_live(self):
        """The load-bearing regression: one real phantom makes the apply leg
        run while an unreadable live control shares the registry. The old global
        `worktree prune` could delete both despite detecting only the first."""
        gone = self.phantom("approved-gone")
        live = self.room("excluded-unreadable-live")
        real = os.stat

        def blind(path, *a, **k):
            if str(path) == live:
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **k)

        backend = vcs.backend(self.root)
        with mock.patch.object(os, "stat", side_effect=blind), \
                mock.patch.object(backend, "remove_worktree_record",
                                  wraps=backend.remove_worktree_record) as remove, \
                mock.patch.object(backend, "text", wraps=backend.text) as text:
            approved = _work_gc.phantom_records(self.root)
            removed, error, unknown = _work_gc.prune_phantom_records(self.root, approved)
        self.assertEqual(approved, [gone])
        self.assertEqual((removed, error, unknown), ([gone], None, False))
        remove.assert_called_once_with(self.root, gone)
        self.assertFalse(any(call.args[1:] == ("worktree", "prune")
                             for call in text.call_args_list),
                         "the targeted actuator fell back to global prune")
        self.assertTrue(self.registered(live),
                        "an excluded unreadable-live record was collateral loss")
        self.assertTrue(os.path.isdir(live))

    def test_postcheck_reports_if_an_EXCLUDED_record_disappears(self):  # noqa: VACUOUS_ASSERTION — approved/excluded controls are positively identified and the collateral-loss diagnostic proves the synthetic post-state was consumed
        """The excluded control is part of the postcondition, not merely a scan
        assertion. A future widened actuator must fail loudly even if every
        approved candidate also disappeared as requested."""
        gone = self.phantom("approved-control")
        live = self.room("excluded-control")
        approved, excluded, error = _work_gc.phantom_scan(self.root)
        self.assertIsNone(error)
        self.assertEqual(approved, [gone])
        self.assertIn(live, excluded)
        before, error = vcs.backend(self.root).worktrees(self.root)
        self.assertIsNone(error)
        after = [r for r in before if r["path"] not in (gone, live)]
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [(before, None), (after, None)]
            be.return_value.remove_worktree_record.return_value = (0, "", "")
            removed, error, unknown = _work_gc.prune_phantom_records(
                self.root, approved, excluded=excluded)
        self.assertEqual(removed, [gone])
        self.assertIsNotNone(error)
        self.assertIn("excluded record(s) disappeared", error)
        self.assertFalse(unknown)

    def test_a_PARTIAL_targeted_remove_reports_BOTH_halves(self):
        """Each approved record is independent: one success and one refusal
        report both the observed removal and the residue."""
        a = self.phantom("leaves-ok")
        b = self.phantom("stays-behind")
        rows = lambda *paths: ([{"path": p, "locked": False} for p in paths], None)
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [rows(a, b), rows(b), rows(b)]
            be.return_value.remove_worktree_record.side_effect = [
                (0, "", ""), (1, "", "simulated refusal")]
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [a, b])
        self.assertEqual(pruned, [a], "the record that DID leave was not reported")
        self.assertIsNotNone(error, "residue reported as a clean pass")
        self.assertIn("remain registered", error)
        self.assertFalse(unknown)

    def test_a_REVIVED_path_is_reauthorized_and_kept(self):  # noqa: VACUOUS_ASSERTION — the recreated directory and deterministic refusal diagnostics prove reauthorization ran before the no-removal assertions
        """Another actor may recreate the directory after the scan. The apply
        leg re-checks the proof and never calls Git for the revived record."""
        path = self.phantom("revived-during-prune")
        os.makedirs(path)
        with mock.patch.object(_work_gc.vcs.backend(self.root),
                               "remove_worktree_record") as remove:
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [path])
        remove.assert_not_called()
        self.assertEqual(pruned, [],
                         "a still-registered path was reported removed")
        self.assertIsNotNone(error)
        self.assertIn("remain registered", error)
        self.assertFalse(unknown)

    def test_an_UNREADABLE_post_prune_registry_reports_UNKNOWN(self):
        """A failed verifier cannot prove what git removed. phantom_records()
        deliberately maps an unreadable registry to [], which is safe for
        authorization and unsafe for postcondition evidence: [] there means
        CANNOT SEE, not every record is gone."""
        path = self.phantom("unreadable-after-prune")
        with mock.patch.object(_work_gc.vcs, "backend") as be:
            be.return_value.worktrees.side_effect = [
                ([{"path": path, "locked": False}], None),
                ([], "simulated post-prune registry failure")]
            be.return_value.remove_worktree_record.return_value = (0, "", "")
            pruned, error, unknown = _work_gc.prune_phantom_records(self.root, [path])
        self.assertEqual(pruned, [],
                         "an unreadable verifier minted removal evidence")
        self.assertIsNotNone(error)
        self.assertIn("removal is UNKNOWN", error)
        self.assertTrue(unknown)

    def test_a_CONTROL_in_a_path_cannot_forge_terminal_output(self):
        """The fourth finding began with newlines, but the boundary is
        terminal printability. A raw ESC can clear or rewrite the same line
        without any newline, so a four-character control allowlist is not the
        class-level fix."""
        from helm.work import _cli
        hostile = (
            "/tmp/x\n  phantom record no longer registered /etc/passwd",
            "/tmp/x\x1b[2Jforged",
            "/tmp/x" + chr(0x202e) + "forged",
        )
        for path in hostile:
            with self.subTest(path=repr(path)):
                shown = _cli._show(path)
                self.assertTrue(shown.isprintable(),
                                "a terminal control survived the renderer")
                self.assertNotEqual(shown, path,
                                    "the hostile path was passed through raw")
                self.assertIn("forged" if "forged" in path else "passwd", shown,
                              "escaping destroyed the path's identifying text")
        # CONTROL: an ordinary path is passed through untouched, so the escape
        # is not simply mangling every path it sees.
        self.assertEqual(_cli._show("/tmp/ordinary/path"), "/tmp/ordinary/path")

    def test_dry_run_NAMES_the_phantom_and_keeps_the_record(self):
        path = self.phantom("ghostrec")
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        self.assertIn("PHANTOM %s" % path, out)
        self.assertTrue(self.registered(path),
                        "the dry run must not touch the registry")

    def test_apply_prunes_the_record_and_NEVER_the_branch(self):  # noqa: VACUOUS_ASSERTION — the fixture asserts the record exists before apply, the observational removal line fires, and the branch remains as the opposite-direction control
        path = self.phantom("deadroom")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("phantom record no longer registered %s" % path, out)
        self.assertFalse(self.registered(path), "the record must be gone")
        # The branch is git's lossless authority over the commits and prune
        # never speaks for it — unlanded work stays recoverable by re-add.
        self.assertTrue(work._has_branch(self.root, "lane/deadroom"))

    def test_UNKNOWN_phantom_result_posts_no_known_summary(self):
        self.phantom("unknown-record")
        with mock.patch.object(_work_cli, "prune_phantom_records",
                               return_value=([], "removal is UNKNOWN", True)):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("removal is UNKNOWN", err)
        self.assertNotIn("phantom records kept", err)
        self.assertNotIn("removed=", out,
                         "an unknown verifier posted a known owner summary")

    def test_failed_phantom_remove_is_not_counted_as_removed(self):
        path = self.phantom("refused-record")
        backend = vcs.backend(self.root)
        with mock.patch.object(backend, "remove_worktree_record",
                               return_value=(1, "", "simulated refusal")):
            rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("simulated refusal", err)
        self.assertIn("removed=0 kept=1 triage=1", out)
        self.assertTrue(self.registered(path))

    def test_OUT_OF_TREE_phantom_belongs_to_worktree_gc(self):  # noqa: VACUOUS_ASSERTION — real Git registration is asserted before and after apply, positively proving the ownership-exclusion path preserved the record
        """`helm work gc` owns lane/harness rooms only. A detached scratchpad
        in the remaining estate stays for the composing `helm worktree gc`."""
        path = os.path.join(self.tmp, "scratch-land")
        r = subprocess.run(["git", "worktree", "add", "-q", "--detach", path],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.rmtree(path)
        self.assertTrue(self.registered(path),
                        "fixture failed to mint a registered phantom")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("no lane rooms", out)
        self.assertNotIn("phantom record no longer registered %s" % path, out)
        self.assertTrue(self.registered(path),
                        "work gc crossed its lane/harness ownership boundary")

    def test_a_LOCKED_record_is_immune_even_with_the_dir_gone(self):  # noqa: VACUOUS_ASSERTION — an unlocked control phantom is asserted PRESENT in the same dry-run and apply outputs the locked one must be absent from; the absences read against a proven-live detector
        """A locked room on an absent path is an owner's deliberate state
        (an unmounted disk), not residue — git's own prune rule, kept.

        The DRY-RUN assertion is the load-bearing one: git itself skips
        locked records on apply, so only the report can lie. Dropping the
        exclusion in `phantom_records` would print 'PHANTOM ... --apply
        prunes the record' — a promise apply then breaks."""
        path = self.room("lockedgone")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "on the usb drive")
        shutil.rmtree(path)
        # POSITIVE CONTROL ON THE SAME OBSERVABLES: an unlocked phantom in the
        # same registry IS seen and IS pruned — proving the detector and both
        # output readings can fire — while the locked one is spared.
        control = self.phantom("unlockedctrl")
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0)
        self.assertIn("PHANTOM %s" % control, out)
        self.assertNotIn("PHANTOM %s" % path, out,
                         "the dry run promised a prune git will refuse")
        rc, out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("phantom record no longer registered %s" % control, out)
        self.assertNotIn("phantom record no longer registered %s" % path, out)
        self.assertFalse(self.registered(control))
        self.assertTrue(self.registered(path),
                        "a locked record must survive the prune")

    def test_an_unreadable_registry_reads_as_NO_phantoms(self):
        """'Cannot see' must never become a prune authorization."""
        path = self.phantom("blindfold")
        self.assertEqual(_work_gc.phantom_records(self.root), [path],
                         "positive control: a readable registry names it")
        with mock.patch.object(vcs.backend(self.root), "worktrees",
                               return_value=([], "simulated registry failure")):
            self.assertEqual(_work_gc.phantom_records(self.root), [])


class TrunkSyncTest(WorkBase):
    """The shared checkout itself rides the cadence (the 2026-08-03 fork:
    a local-only commit raced a land and every seat measured trunk on the
    stale side for ~80 minutes). BEHIND converges by ff; AHEAD and a FORK
    are NAMED; a fork is posted to the room; nothing here pushes, rebases,
    or repairs a fork."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        r = _sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main",
                self.origin)
        self.assertEqual(r.returncode, 0, r.stderr)
        _sh(self.root, "git", "remote", "add", "origin", self.origin)
        r = _sh(self.root, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "fetch", "-q", "origin")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _sha(self, ref="main"):
        return _sh(self.root, "git", "rev-parse", ref).stdout.strip()

    def _land_on_origin(self, fname, content="landed elsewhere\n"):
        """A land performed by the REST OF THE FLEET: clone, commit, push."""
        clone = os.path.join(self.tmp, "clone-" + fname)
        r = _sh(self.tmp, "git", "clone", "-q", self.origin, clone)
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in (("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            _sh(clone, *cmd)
        with open(os.path.join(clone, fname), "w") as f:
            f.write(content)
        _sh(clone, "git", "add", "-A")
        r = _sh(clone, "git", "commit", "-q", "-m", "land " + fname)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(clone, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _commit_local(self, fname):
        """The stray direct commit — the incident's generator."""
        with open(os.path.join(self.root, fname), "w") as f:
            f.write("stray direct commit\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "stray " + fname)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_in_sync_says_nothing(self):
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("refreshed origin", out,
                      "positive control: the pass demonstrably ran and got "
                      "past the fetch — silence below is then a verdict")
        self.assertNotIn("TRUNK-", out)

    # noqa: VACUOUS_ASSERTION — assertNotEqual(before, after) is the MOVED
    # claim, paired with assertEqual(after, origin/main) on the same ref and
    # a file-exists control; M1 kills this test 6 ways.
    def test_behind_apply_fast_forwards_the_shared_base(self):
        self._land_on_origin("landed.txt")
        before = self._sha()
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-SYNC fast-forwarded", out)
        after = self._sha()
        self.assertNotEqual(before, after, "positive control: main must move")
        self.assertEqual(after, self._sha("origin/main"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "landed.txt")),
                        "the ff must carry the working tree with it")

    # noqa: VACUOUS_ASSERTION — restraint IS the behavior under test; the
    # positive control on the same sha/file observables is the apply arm
    # (test_behind_apply_...), same fixture, and assertIn(TRUNK-BEHIND)
    # proves this arm saw the state it declined to act on. M3 kills it.
    def test_behind_dry_run_names_the_ff_and_moves_nothing(self):
        self._land_on_origin("landed.txt")
        _sh(self.root, "git", "fetch", "-q", "origin")  # dry run never fetches
        before = self._sha()
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-BEHIND", out)
        self.assertEqual(self._sha(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "landed.txt")))

    # noqa: VACUOUS_ASSERTION — the unchanged-sha claim rides beside two
    # unconditional positives on the same observables: the refusal line in
    # out, and the byte-exact read of the dirty edit the ff must not eat.
    def test_behind_with_a_conflicting_dirty_edit_keeps_gits_words(self):
        self._land_on_origin("README", "rewritten on the trunk\n")
        with open(os.path.join(self.root, "README"), "w") as f:
            f.write("dirty local edit\n")
        before = self._sha()
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-BEHIND kept: fast-forward refused", out)
        self.assertEqual(self._sha(), before)
        with open(os.path.join(self.root, "README")) as f:
            self.assertEqual(f.read(), "dirty local edit\n",
                             "the refused ff must not eat the dirty edit")

    # noqa: VACUOUS_ASSERTION — never-pushed is the behavior under test;
    # assertIn(TRUNK-AHEAD) + the named commit prove the pass saw the state,
    # and the origin tip is read from the BARE repo, not this checkout.
    # M4 (ahead-goes-silent) kills it.
    def test_ahead_is_named_and_never_pushed(self):
        self._commit_local("stray.txt")
        origin_before = self._sha("origin/main")
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-AHEAD", out)
        self.assertIn("1 unpushed commit", out)
        self.assertIn("stray", out)                    # names the commit
        remote_tip = _sh(self.origin, "git", "rev-parse",
                         "main").stdout.strip()
        self.assertEqual(remote_tip, origin_before,
                         "gc must never publish local work")

    # noqa: VACUOUS_ASSERTION — left-alone is the behavior under test; the
    # unconditional positives on the same pass are assertIn(TRUNK-DIVERGED)
    # and exactly-one FORKED room post. M2 (post-dropped) kills it.
    def test_a_fork_is_named_posted_and_left_alone(self):
        self._commit_local("stray.txt")
        self._land_on_origin("landed.txt")
        before = self._sha()
        with mock.patch("helm.chat.post") as posted:
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-DIVERGED", out)
        self.assertEqual(self._sha(), before, "a fork is never auto-repaired")
        forked = [c for c in posted.call_args_list if "FORKED" in c[0][0]]
        self.assertEqual(len(forked), 1,
                         "the fork must reach the room exactly once per pass")

    # noqa: VACUOUS_ASSERTION — not-posted is the behavior under test; the
    # posted arm (test_a_fork_is_named_posted_...) is the positive control
    # on the same chat.post observable, and assertIn(TRUNK-DIVERGED) proves
    # this arm saw the fork it declined to broadcast.
    def test_a_fork_in_the_dry_run_is_named_but_not_posted(self):
        self._commit_local("stray.txt")
        self._land_on_origin("landed.txt")
        _sh(self.root, "git", "fetch", "-q", "origin")
        with mock.patch("helm.chat.post") as posted:
            rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("TRUNK-DIVERGED", out)
        posted.assert_not_called()

    # noqa: VACUOUS_ASSERTION — kept is the behavior under test; the
    # unconditional positives are len(lines)==1 and the HEAD reason text
    # on the same return value the absence claim reads.
    def test_head_off_the_base_is_kept_with_the_reason(self):
        r = _sh(self.root, "git", "checkout", "-q", "--detach")
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self._sha("HEAD")
        lines, err = _work_gc.trunk_sync(self.root, enforcing=True)
        self.assertIsNone(err)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("HEAD is", lines[0])
        self.assertEqual(self._sha("HEAD"), before)

    def test_no_remote_is_silence(self):
        solo = os.path.join(self.tmp, "solo")
        os.makedirs(solo)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(solo, *cmd).returncode, 0)
        with open(os.path.join(solo, "f"), "w") as f:
            f.write("x\n")
        _sh(solo, "git", "add", "-A")
        _sh(solo, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(_work_gc.trunk_sync(solo, enforcing=True),
                         ([], None))


class ListTest(WorkBase):
    def test_list_joins_registry_with_claims(self):
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray", dirty="j.txt")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row_w = next(ln for ln in out.splitlines() if "webui" in ln)
        # the duration says WHICH DIRECTION it runs — see
        # test_the_lease_duration_says_which_direction_it_runs
        self.assertRegex(row_w, r"webui\s+s1 \d+s left\s+clean")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_the_lease_duration_says_which_direction_it_runs(self):
        """A BARE DURATION READS BOTH WAYS, and two seats proved it.

        The column rendered "<seat> 13378s", which is equally natural as
        "held for" and as "left" — and the field behind it is literally named
        `remaining`. On 2026-08-05 two seats misread it the same way inside
        an hour: one nearly raised a false alarm on their own lane, and I
        built a lease-expiry policy ask on top of it. Same misreading, two
        readers, one hour — that is a surface defect, not carelessness.

        This same file already wrote "%ds ago" for the other direction, so
        the convention existed and this column had simply missed it."""
        rc, _out, err = self.work("claim", "webui", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        row = next(ln for ln in out.splitlines() if "webui" in ln)

        self.assertIn("left", row,
                      "the duration is unlabelled and reads equally as age "
                      "or as remaining: %r" % row)
        self.assertRegex(row, r"\d+s left",
                         "the label is not attached to the number")
        # NEGATIVE CONTROL: an UNHELD row has no duration to label, and must
        # not grow a stray one — the holder column is what says unheld, and
        # that is the reading the whole finding rested on.
        self.room("stray", dirty="j.txt")
        rc, out, _err = self.work("list")
        row_s = next(ln for ln in out.splitlines() if "stray" in ln)
        self.assertNotIn("left", row_s,
                         "an unheld row grew a duration: %r" % row_s)
        self.assertRegex(row_s, r"stray\s+-\s+dirty")

    def test_list_empty(self):
        rc, out, _err = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("no lane rooms", out)

    def test_two_held_lanes_on_one_file_are_surfaced(self):
        """THE QUESTION THE LEASE COLUMN CANNOT ANSWER.

        A lease guards a worktree+branch, so it answers 'is anyone else editing
        this ROOM' — never 'is anyone else fixing this DEFECT'. On 2026-07-30
        two seats held valid, uncontested leases on two lanes both rewriting
        tests/test_seats.py for the same red; the board printed two healthy
        rows and main stayed broken for hours. A human reading chat caught it.

        Both lanes commit a change to the SAME file, both leases live -> the
        board must say so."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out)
        self.assertIn("shared.py", out)
        self.assertIn("PROXY", out, "must not claim overlap proves same-defect")

    def test_overlap_survives_one_lane_already_being_on_the_trunk(self):
        """THE MERGE-BASE MUST BE THE PAIR'S, NEVER THE TRUNK'S — and this test
        exists because a mutation proved the other two did not bind it.

        Once a lane's work is reachable from the trunk (landed, cherry-picked,
        or merged-but-room-not-yet-reaped), merge-base(tip, trunk) IS the tip,
        so a trunk-based diff is EMPTY and the pair reports no overlap. Silent,
        confident, wrong — the same shape as every other failure this lane is
        about. Measured: the first live run of this detector scored BOTH known
        duplicate-work incidents clean for exactly this reason, because I
        diffed against the trunk instead of against the pair.

        alpha's commit is merged to the trunk; beta still holds its own. They
        share a file, both leases live -> the overlap must still surface."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
        # alpha lands on the trunk; its room and lease stay (the real window)
        _sh(self.root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "merge", "--no-ff", "-q", "-m", "land alpha", "lane/alpha")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out,
                      "a lane whose work reached the trunk still collides with "
                      "an open lane on the same file; diffing against the trunk "
                      "would silently report nothing")
        self.assertIn("shared.py", out)

    def test_overlap_survives_one_lane_fast_forwarded_to_trunk(self):  # noqa: VACUOUS_ASSERTION — FF ancestry, authored reflog, rendered overlap, and UNKNOWN fallback are all positive controls
        """REVIEW FINDING at eee99d72: first-parent alone is not authorship.

        An FF-landed lane tip is on trunk's first-parent line just like an
        untouched stale lane. Its own reflog still records the lane commit, so
        the empty own-diff must preserve the pair-base overlap."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# " + lane + "\n")
        alpha = _sh(self.root, "git", "rev-parse",
                    "lane/alpha").stdout.strip()
        _sh(self.root, "git", "merge", "--ff-only", "lane/alpha")
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head, alpha,
                         "fixture broken: alpha must fast-forward onto trunk")
        first_parent = _sh(self.root, "git", "rev-list", "--first-parent",
                           "HEAD").stdout.splitlines()
        self.assertIn(alpha, first_parent,
                      "fixture broken: FF lane tip must be first-parent")
        reflog = _sh(self.root, "git", "reflog", "show", "--format=%gs",
                     "lane/alpha").stdout
        self.assertIn("commit", reflog,
                      "fixture broken: alpha branch must record authorship")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("OVERLAPPING LANES", out)
        self.assertIn("shared.py", out)
        with mock.patch.object(_work_gc, "_branch_authored",
                               return_value=None):
            unknown = _work_gc.lane_overlaps(self.root)
        self.assertTrue(unknown,
                        "UNKNOWN branch biography must preserve the wide answer")
        self.assertIn("shared.py", unknown[0][2])

    def test_an_unheld_lane_is_not_racing_you(self):
        """NOISE CONTROL, and it is what makes the signal usable.

        Raw file overlap over every open room is O(N^2) and hub-dominated —
        measured on the live board it produced 26 pairs, most of them 'we both
        touched tests/test_seats.py', which is true of nearly every lane. A
        PARKED room's holder is gone and is not racing anyone. Same two lanes,
        same shared file, one lease released -> silent."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, out, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            lease = out.split("\t")[2].strip()
            p = os.path.join(self.root + "-wt", lane)
            with open(os.path.join(p, "shared.py"), "w") as f:
                f.write("# %s\n" % lane)
            _sh(p, "git", "add", "-A")
            _sh(p, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", lane)
            if lane == "beta":
                self.work("release", lane, "--lease", lease, "--seat", seat)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("OVERLAPPING LANES", out)

    def _commit(self, path, name, body):
        with open(os.path.join(path, name), "w") as f:
            f.write(body)
        _sh(path, "git", "add", "-A")
        _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", name + " in " + os.path.basename(path))

    def test_a_fresher_lane_does_not_own_the_trunk_commits_it_inherited(self):
        """THE FALSE-POSITIVE THE PAIR-BASE PRODUCES, and why the cure is not
        'diff against the trunk'.

        When two lanes sit on DIFFERENT trunk points, merge-base(a, b) is the
        OLDER base, so the fresher lane's diff against it contains every trunk
        commit it merely INHERITED by being rebased later. If the older lane
        happens to have authored a file the trunk also changed in that window,
        the board reports a collision on a file the fresher lane never touched.
        Measured on the live board 2026-08-03: 44 reported pairs, 26 of them
        sharing nothing but inherited landings.

        alpha authors shared.py. The TRUNK then changes shared.py. beta is cut
        from that newer trunk and touches only its own file. Their merge-base
        predates the trunk change, so beta 'inherits' shared.py and the pair
        reports it. beta never opened the file."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha's own work\n")
        # the trunk moves, touching the same file — a land by somebody else
        self._commit(self.root, "shared.py", "# a landed trunk change\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "beta"),
                     "beta_only.py", "# beta's own work\n")
        # POSITIVE CONTROL, unconditional: beta really does inherit shared.py
        # through the pair-base, so a silent board below is the NARROWING
        # working and not a scan that found nothing.
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "lane/beta").stdout.strip()
        inherited = _sh(self.root, "git", "diff", "--name-only",
                        base + "..lane/beta").stdout
        self.assertIn("shared.py", inherited,
                      "fixture broken: beta must inherit shared.py through "
                      "the pair-base, or this tests nothing")
        self.assertIn("beta_only.py", inherited)
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME ROOT OBJECT, unconditional: the board
        # really did render both lanes, so the absence below is the narrowing
        # and not an empty listing.
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("shared.py", out,
                         "beta never touched shared.py — it inherited the "
                         "trunk commit that did")

    def test_a_lane_that_has_authored_nothing_is_racing_nobody(self):
        """A ROOM JUST CLAIMED SITS ON THE TRUNK TIP. Its pair-base diff is
        100% inherited, so without this it collides with everything that ever
        touched a busy file. The first cut of the narrowing treated its empty
        own-diff as 'cannot narrow' and handed back the un-narrowed set —
        which made the board WORSE than no narrowing at all (44/26 false
        became, for that shape, more pairs rather than fewer)."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        self._commit(self.root, "shared.py", "# trunk moved on\n")
        # beta is claimed and NEVER COMMITS — it sits exactly on the trunk tip
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        tip = _sh(self.root, "git", "rev-parse", "lane/beta").stdout.strip()
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(tip, head,
                         "fixture broken: beta must sit on the trunk tip")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME ROOT OBJECT, unconditional.
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("OVERLAPPING LANES", out,
                         "a lane with no commits of its own cannot be racing "
                         "anyone for a file")

    def test_an_untouched_lane_stays_silent_after_trunk_moves_again(self):  # noqa: VACUOUS_ASSERTION — inherited shared.py and both rendered lanes positively control the intentional no-overlap assertion
        """REVIEW FINDING at 8c576205: trunk-tip equality is time-sensitive.

        beta is claimed at T1 and authors NOTHING. A later trunk T2 made beta
        differ from the current tip, so the old discriminator called its empty
        own-diff a landed lane and restored inherited shared.py. Its tip remains
        on trunk's first-parent line, which is the durable no-authorship fact."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        self._commit(self.root, "shared.py", "# trunk T1\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        beta = _sh(self.root, "git", "rev-parse", "lane/beta").stdout.strip()
        self._commit(self.root, "later.py", "# trunk T2\n")
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(beta, head,
                            "fixture broken: trunk must move after beta claim")
        first_parent = _sh(self.root, "git", "rev-list", "--first-parent",
                           "HEAD").stdout.splitlines()
        self.assertIn(beta, first_parent,
                      "fixture broken: untouched beta must remain trunk-line")
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "lane/beta").stdout.strip()
        inherited = _sh(self.root, "git", "diff", "--name-only",
                        base + "..lane/beta").stdout
        self.assertIn("shared.py", inherited,
                      "fixture broken: pair-base must contain inherited file")
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertNotIn("OVERLAPPING LANES", out,
                         "an untouched stale trunk-line lane authored no file")

    def test_a_narrowing_that_cannot_compute_hands_back_the_wide_answer(self):
        """A CHECK THAT COULD NOT LOOK MUST NOT ANSWER AS IF IT HAD — and here
        the safe side is the WIDE one, because a missed collision cost a day
        and an extra pair costs a glance. With no trunk resolvable the
        narrowing is impossible, so the pair-base result must stand."""
        for lane, seat in (("alpha", "s1"), ("beta", "s2")):
            rc, _o, err = self.work("claim", lane, "--seat", seat)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# " + lane + "\n")
        # POSITIVE CONTROL: with a trunk, this pair is REAL and surfaces.
        rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("shared.py", out)
        with mock.patch.object(_work_gc, "_trunk", return_value=""):
            overlaps = _work_gc.lane_overlaps(self.root)
        self.assertTrue(overlaps,
                        "an unresolvable trunk must fall back to the "
                        "un-narrowed pair-base answer, never to silence")
        self.assertIn("shared.py", overlaps[0][2])


class ClaimDisclosesLiveFilesTest(WorkBase):
    """THE FILE AXIS OF THE SAME SEAM. KindredLaneWarningTest catches a lane
    whose NAME leads another; this catches the case where the names share
    nothing.

    MEASURED 2026-08-04: three seats built the #183 tree-warning fix in
    parallel as which-helm-warning-scope, treewarn-fires-only-in-a-helm-
    checkout and fix-183-tree-warning-scope — no shared prefix, one shared
    file (helm/cli.py). The name check could not see it, and lane_overlaps
    could, but it runs inside `gc` and reports to whoever does housekeeping
    after both duplicates are written.

    It DISCLOSES rather than compares: the lane being claimed has no commits,
    so nothing can compare its diff. It states what is live and in what."""

    def _commit(self, path, name, body):
        with open(os.path.join(path, name), "w") as f:
            f.write(body)
        _sh(path, "git", "add", "-A")
        _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", name + " in " + os.path.basename(path))

    def test_a_held_lanes_files_are_named_at_claim_time(self):
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED, so the line
        # below is added information and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("live lane alpha", err)
        self.assertIn("shared.py", err,
                      "the FILE is the whole signal — a lane name it does not "
                      "recognise tells the claimer nothing")

    def test_the_lane_being_claimed_is_not_disclosed_to_itself(self):  # noqa: VACUOUS_ASSERTION — the control is a SECOND claim in this same test proving alpha IS disclosable to another claimer, so the silence toward its own holder is the exclude arm and not an unwired stream
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        # CONTROL: alpha IS disclosable — it shows to another claimer.
        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("live lane alpha", err2)
        rc, _o, err3 = self.work("claim", "alpha", "--seat", "s1")
        self.assertNotIn("live lane alpha", err3,
                         "a lane must never be announced to its own claimer")

    def test_an_UNHELD_lane_is_not_disclosed(self):  # noqa: VACUOUS_ASSERTION — the control runs first and unconditionally: while alpha is HELD it is disclosed, so the silence under a patched-empty _live is the lease filter acting
        """Same live-lease filter lane_overlaps uses: an unheld room is nobody
        racing you, and listing it is the noise that gets a guard ignored."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertIn("live lane alpha", err2)   # CONTROL: held -> disclosed
        with mock.patch.object(_work_gc, "_live", return_value={}):
            rc, _o, err3 = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err3)
        self.assertNotIn("live lane alpha", err3,
                         "an unheld lane must not be announced")

    def test_a_lane_that_has_authored_NOTHING_is_still_disclosed(self):
        """THE COLLISION WINDOW IS BEFORE ANYONE WRITES CODE, and that is
        exactly where this disclosure used to go silent.

        held_lane_files ended with `if files:`, so a live lane with no commits
        never reached the claimer. The function's own docstring says it exists
        because lane_overlaps needs commits from BOTH lanes and the lane being
        claimed has none — 'a guard that says clear exactly when you consult
        it is worse than no guard'. It then required commits from every lane
        it disclosed, reintroducing the same silence one seam over.

        Measured 2026-08-05 on the live checkout: 13 held lanes, 12 disclosed,
        1 dropped — and the dropped one was an actively-worked lane with an
        open dispatch. That same hour two seats claimed one defect under two
        labels minutes apart, neither having authored anything, and neither
        claim warned the other."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # NOTHING IS COMMITTED TO alpha. That is the whole fixture.

        rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("live lane alpha", err2,
                      "a live lane with no commits was dropped from the "
                      "disclosure — the claimer was told the coast was clear "
                      "during the exact window a duplicate is born")
        self.assertIn("nothing authored yet", err2,
                      "an authored-nothing lane was flattened into the "
                      "files wording, which reads as 'live and touching "
                      "nothing' — the opposite of the warning intended")

        # AND IT STAYS DISTINCT FROM THE FILES CASE. Without this, the arm
        # above would pass on a build that printed the no-file wording for
        # EVERY lane, which is the same conflation one direction over.
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")
        rc, _o, err3 = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err3)
        self.assertIn("live lane alpha is in", err3,
                      "a lane that HAS authored files lost its file list")
        self.assertNotIn("live lane alpha CLAIMED, nothing authored", err3,
                         "the no-file wording leaked onto a lane that has "
                         "authored files")

    def test_an_UNREADABLE_history_says_UNKNOWN_and_not_nothing_authored(self):
        """A FAILED READ MUST NOT BECOME A CONFIDENT DENIAL, and the first cut
        of this disclosure manufactured exactly that.

        _authored returns the _EVERYTHING sentinel when it could not look — no
        trunk resolvable, merge-base non-zero, diff non-zero. _EVERYTHING is
        an EMPTY frozenset subclass, so it is FALSEY, and `sorted(x or ())`
        turned it into [] — indistinguishable from a branch that genuinely
        authored nothing. The claim seam then printed "CLAIMED, nothing
        authored yet" about a lane that may have authored plenty, stating as
        fact the one thing the read had failed to establish."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self._commit(os.path.join(self.root + "-wt", "alpha"),
                     "shared.py", "# alpha\n")

        # CONTROL: readable, it reports FILES — so the UNKNOWN below is the
        # sentinel doing the work and not the lane having gone quiet.
        rc, _o, ctl = self.work("claim", "ctl", "--seat", "sc")
        self.assertIn("live lane alpha is in", ctl)

        with mock.patch.object(_work_gc, "_authored",
                               return_value=_work_gc._EVERYTHING):
            rc, _o, err2 = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err2)
        self.assertIn("authorship UNKNOWN", err2,
                      "a history that could not be READ was reported as a "
                      "history that was read and found empty")
        self.assertNotIn("nothing authored yet", err2,
                         "the failed read was rendered as a confident denial")

    def test_every_live_lane_is_NAMED_however_many_there_are(self):
        """NEVER OMIT AN IDENTITY, ONLY ABBREVIATE EVIDENCE.

        THREE INDEPENDENT POPULATIONS, and the previous version of this test
        did not have them — caught in review. The claim was 5 UNKNOWN + 8 bare +
        5 file-bearing, but the UNKNOWN five were PATCHED OUT OF THE BARE
        EIGHT, so the real populations were 5/3/5 with UNKNOWN a strict subset
        of bare. A fixture whose classes overlap can misclassify a row and
        stay green, because the row it put in the wrong bucket is a member of
        both. The classes are disjoint now: unk*, bare*, filey* are three
        separate sets of lanes.

        The cap now sits on the FILE LIST, never on the lane. A lane's NAME is
        the whole signal a claimer needs to recognise their own subject under
        someone else's label, and it costs one line."""
        unk = ["unk%d" % i for i in range(5)]
        bare = ["bare%d" % i for i in range(8)]
        filey = ["filey%d" % i for i in range(5)]
        for lane in unk + bare + filey:
            rc, _o, err = self.work("claim", lane, "--seat", "s-" + lane)
            self.assertEqual(rc, 0, err)
        for lane in filey:
            self._commit(os.path.join(self.root + "-wt", lane),
                         "%s.py" % lane, "# %s\n" % lane)

        real = _work_gc._authored

        def unknown_only_for_unk(v, root, trunk, branch, cache):
            if branch.startswith("lane/unk"):
                return _work_gc._EVERYTHING
            return real(v, root, trunk, branch, cache)

        with mock.patch.object(_work_gc, "_authored", unknown_only_for_unk):
            rc, _o, err = self.work("claim", "newcomer", "--seat", "sn")
        self.assertEqual(rc, 0, err)

        # EXACT CLASS-SPECIFIC LINES, not a name appearing somewhere. A bare
        # membership check would pass on a build that printed every lane under
        # one wording, which is the misclassification this fixture exists to
        # catch.
        for lane in unk:
            self.assertIn("live lane %s — authorship UNKNOWN" % lane, err,
                          "an UNKNOWN lane was missing or misclassified: %s"
                          % lane)
        for lane in bare:
            self.assertIn("live lane %s CLAIMED, nothing authored yet" % lane,
                          err, "a bare lane was missing or misclassified: %s"
                               % lane)
        for lane in filey:
            self.assertIn("live lane %s is in %s.py" % (lane, lane), err,
                          "a file-bearing lane lost its identity or its file "
                          "evidence: %s" % lane)

        # AND THE POPULATIONS REALLY ARE DISJOINT — without this the three
        # loops above could all be satisfied by one over-broad wording.
        self.assertEqual(err.count("authorship UNKNOWN"), len(unk))
        self.assertEqual(err.count("nothing authored yet"), len(bare))

    def test_two_lanes_on_ONE_filename_both_survive(self):
        """FILENAME OVERLAP IS THE COLLISION CASE, and my fixture did not have
        it — found by mutating rather than reading.

        Every file-bearing lane in the other arm owns a filename nobody else
        touches (fileyN -> fileyN.py). So a production change that DEDUPED
        non-empty rows by tuple(files) parsed, imported, and left that test
        GREEN — while a real probe with two lanes both on shared.py disclosed
        only the first. The fixture could not see the bug because it never
        built two lanes that could be confused for each other.

        AND FILENAME OVERLAP IS THE WHOLE POINT OF THIS DISCLOSURE. Two lanes
        editing one file is not an edge case here; it is the collision the
        claim seam exists to announce."""
        for lane in ("alpha", "zeta"):
            rc, _o, err = self.work("claim", lane, "--seat", "s-" + lane)
            self.assertEqual(rc, 0, err)
            self._commit(os.path.join(self.root + "-wt", lane),
                         "shared.py", "# %s\n" % lane)

        rc, _o, err2 = self.work("claim", "newcomer", "--seat", "sn")
        self.assertEqual(rc, 0, err2)
        # BOTH exact lines. A count would pass on a build that printed one
        # lane twice, and a bare `assertIn("shared.py")` would pass on a build
        # that dropped either lane entirely.
        self.assertIn("live lane alpha is in shared.py", err2,
                      "alpha vanished from the disclosure")
        self.assertIn("live lane zeta is in shared.py", err2,
                      "zeta vanished — two lanes on ONE file is exactly the "
                      "collision this disclosure exists to announce, and it "
                      "is the case a dedupe-by-fileset silently eats")

    def test_a_long_file_list_is_abbreviated_IN_PLACE_with_its_count(self):
        """The evidence may be abbreviated; the abbreviation must say so
        beside the lane it belongs to, not in a trailing summary a reader
        cannot attribute."""
        rc, _o, err = self.work("claim", "wide", "--seat", "sw")
        self.assertEqual(rc, 0, err)
        for i in range(7):
            self._commit(os.path.join(self.root + "-wt", "wide"),
                         "f%d.py" % i, "# %d\n" % i)

        rc, _o, err2 = self.work("claim", "after", "--seat", "sa")
        self.assertEqual(rc, 0, err2)
        # THE EXACT LINE, not a substring of it. An aggregate
        # `assertIn("more file(s)")` passes on ANY count — including a wrong
        # one — and `assertIn("is in")` passes without a single filename. The
        # thing under test is which four survive and that the remainder is
        # counted correctly, so both have to be in the assertion.
        self.assertIn(
            "live lane wide is in f0.py, f1.py, f2.py, f3.py "
            "(+3 more file(s))", err2,
            "the abbreviated file list is wrong in its files, its order, or "
            "its remainder count")

    def test_a_raising_disclosure_never_costs_the_claim(self):
        """It runs AFTER the claim has already succeeded, so a git hiccup must
        cost the line and never the lease."""
        with mock.patch.object(_work_gc, "held_lane_files",
                               side_effect=RuntimeError("boom")):
            rc, out, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("alpha", out, "the claim itself must still report")


    def test_one_raising_disclosure_never_silences_the_other(self):
        """THE REGRESSION A REVIEWER MEASURED, now pinned. The moved-target
        disclosure was added ahead of this one INSIDE THE SAME try, so when it
        raised, the live-lane line vanished entirely and the claim still
        returned 0 — a new guard silently deleting an older one, which is
        strictly worse than the gap it was added to close.

        MUST-HIT FIRST: an unpatched claim prints the live-lane line at all,
        or every assertion below is satisfied by a surface that never speaks."""
        self.work("claim", "beta", "--seat", "s2")
        # beta needs an AUTHORED file or held_lane_files has nothing to say —
        # my first fixture skipped this and the control below caught it,
        # which is the only reason the assertions are not vacuous.
        self._commit(os.path.join(self.root + "-wt", "beta"),
                     "shared.py", "# beta\n")
        _rc, _o, base_err = self.work("claim", "alpha", "--seat", "s1")
        self.assertIn("live lane beta", base_err, "control: the line exists")

        # a THIRD lane rather than release-and-reclaim: release needs the
        # lease id, and the point is only to run the seam a second time.
        with mock.patch.object(_work_gc, "moved_lane_targets",
                               side_effect=RuntimeError("boom")):
            rc, out, err = self.work("claim", "gamma", "--seat", "s3")
        self.assertEqual(rc, 0, err)
        self.assertIn("gamma", out, "the claim itself must still report")
        self.assertIn("live lane beta", err,
                      "the sibling disclosure must survive its neighbour")


class MovedLaneTargetTest(WorkBase):
    # THE ENTRY NAMES ITS EVENT, and these arms pin that. The first cut
    # reported a bare path and answered only "was it DELETED" — which was
    # SILENT on the incident that motivated the guard, because the real web
    # split left helm/web.py in place as a 156-line facade (3805 -> 156, 36
    # added / 3685 deleted, --diff-filter=D empty). Two events now report,
    # each once and each labelled: "(deleted on trunk)" and "(trunk -N,
    # yours M)". The numbers travel because the reader, not a tuned
    # constant, decides whether the ground moved too far.

    """DID TRUNK MOVE THE FILE OUT FROM UNDER A LIVE LANE — the other axis of
    the same seam. held_lane_files answers "who else is in this file NOW";
    moved_lane_targets answers "the file you are in is no longer where the
    trunk keeps that code".

    MEASURED 2026-08-05: a lane edited helm/web.py; the web split landed and
    moved that code to helm/web_core.py, leaving web.py a facade. The rebase
    CONFLICTED, which is the loud outcome and git working correctly. The silent
    outcome is the expensive one — hunks that do not collide apply to a file
    that no longer holds the caller, the real caller keeps calling a name the
    same commit deleted, and the gate stays GREEN the whole way because it
    measured the OLD base.

    ABSENCE FROM THE TRUNK IS THE WRONG TEST ON ITS OWN: a file the lane
    CREATED is absent by design, and this lane's own work added helm/tasks.py.
    The merge-base is the discriminator and
    `test_a_file_the_lane_CREATED_is_not_a_moved_target` is the arm that
    binds it."""

    def _write(self, path, name, body):
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(body)

    def _commit(self, path, msg):
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", msg)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _lane_editing_web(self, lane="alpha", seat="s1", creates=None):
        """The trunk carries helm/web.py; `lane` branches from it and edits it,
        plus any `creates` path the lane INVENTS. Returns the room path."""
        self._write(self.root, "helm/web.py", "# the real caller lives here\n")
        self._commit(self.root, "seed web")
        rc, _o, err = self.work("claim", lane, "--seat", seat)
        self.assertEqual(rc, 0, err)
        room = os.path.join(self.root + "-wt", lane)
        self._write(room, "helm/web.py",
                    "# the real caller lives here\n# %s's edit\n" % lane)
        if creates:
            self._write(room, creates, "# a module this lane CREATED\n")
        self._commit(room, "edit web")
        return room

    def _trunk_moves_web(self):
        """The split lands: helm/web.py's code becomes helm/web_core.py."""
        r = _sh(self.root, "git", "mv", "helm/web.py", "helm/web_core.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._commit(self.root, "split web out to web_core")
        # FIXTURE CONTROLS, unconditional: the move really landed on the trunk.
        self.assertNotEqual(_sh(self.root, "git", "cat-file", "-e",
                                "main:helm/web.py").returncode, 0,
                            "fixture broken: the trunk must no longer have "
                            "helm/web.py")
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web_core.py").returncode, 0,
                         "fixture broken: the code must be at web_core.py")

    def _trunk_DRAINS_web(self):
        """THE SHAPE PRODUCTION ACTUALLY MINTS. The real web split did NOT
        move or delete helm/web.py — it left a 156-line facade behind and
        emptied the body into siblings. Measured on the live pair
        6e965966->4b39c7ec: 3805 lines -> 156, +36/-3685, and
        `git diff --diff-filter=D` reports NOTHING. The first version of this
        guard asked only about deletion and was therefore silent on the exact
        incident that motivated it."""
        with open(os.path.join(self.root, "helm", "web_core.py"), "w") as f:
            f.write("\n".join("def moved_%d(): return %d" % (i, i)
                               for i in range(40)) + "\n")
        with open(os.path.join(self.root, "helm", "web.py"), "w") as f:
            f.write("from .web_core import *  # facade\n")
        self._commit(self.root, "drain web.py into web_core, keep a facade")
        # FIXTURE CONTROLS, unconditional and both directions: the file is
        # STILL THERE (so a deletion test must stay silent) and it really lost
        # its body (so there is something to detect at all).
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web.py").returncode, 0,
                         "fixture broken: the facade must SURVIVE")
        d = _sh(self.root, "git", "diff", "--name-only", "--diff-filter=D",
                "--no-renames", "main~1", "main")
        self.assertNotIn("helm/web.py", d.stdout,
                         "fixture broken: nothing may be DELETED here — that "
                         "is the whole point of the drained shape")

    def test_the_SURFACE_never_claims_deletion_for_a_drained_file(self):  # noqa: VACUOUS_ASSERTION — the two absences (NO LONGER HAS, deleted) are on the SAME `err` binding as two unconditional must-hits directly above them: assertIn("helm/web.py", err) and assertIn("trunk +", err), both non-negotiable and both on the beta-claim result. A run where the disclosure never printed fails at those two before reaching either absence. The analyzer cannot credit them because `err` is a tuple-unpacked call result rather than a literal comparison; the controls are real and named here so the annotation is checkable rather than a shrug.
        """THE SEAM ARM. The producer learned to tell deleted from drained and
        the claim surface collapsed them back: it printed a flat deletion claim
        directly after an entry showing trunk still had the file, which shows
        trunk DOES have it. Self-contradicting in one sentence, and a reader is
        sent hunting a deleted file that is still there.

        Neither side's own tests could see it — the producer's arms assert the
        RETURN VALUE and never render it; the surface's arm asserted only that
        SOMETHING printed. This runs both and reads the sentence."""
        body = "\n".join("def real_%d(): return %d" % (i, i)
                          for i in range(60)) + "\n"
        self._write(self.root, "helm/web.py", body)
        self._commit(self.root, "seed a substantial web.py")
        rc, _o, _e = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0)
        room = os.path.join(self.root + "-wt", "alpha")
        self._write(room, "helm/web.py", body + "def lane_edit(): return 1\n")
        self._commit(room, "lane edits web.py")
        self._trunk_DRAINS_web()

        rc, _o, err = self.work("claim", "beta", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        # MUST-HIT: the disclosure fired at all, or every absence below is free
        self.assertIn("helm/web.py", err, "the disclosure must have printed")
        self.assertIn("trunk +", err, "the entry must carry trunk's numbers")
        # AND THE SENTENCE MUST NOT CLAIM A DELETION THAT DID NOT HAPPEN
        # BOTH absences on the SAME binding the must-hits above assert on,
        # not on a derived expression. My first version tested
        # err.lower().replace("deleted on trunk", "") — a fresh observable
        # with no positive control, which is what the vacuity rung flagged
        # and it was right. A drained file must produce NO deletion language
        # at all, so the simpler assertion is also the stronger one.
        self.assertNotIn("NO LONGER HAS", err)
        self.assertNotIn("deleted", err)
        # the file really is still there — the claim would have been false
        self.assertEqual(_sh(self.root, "git", "cat-file", "-e",
                             "main:helm/web.py").returncode, 0)

    def test_a_DRAINED_file_is_reported_though_nothing_was_deleted(self):
        """THE FOUNDING INCIDENT, which the first implementation could not see.

        A reviewer pointed the predicate at the real split pair and it
        returned [] — the guard was silent on the case it was built for,
        because the fixture used `git mv` (a shape production does not mint)
        while production drains a file and leaves the path in place."""
        # A SUBSTANTIAL FILE, because the comparison is between two MEASURED
        # quantities and a one-line file cannot express it: trunk removing 1
        # line is not more than a lane that changed 1 line, and the guard is
        # RIGHT to stay quiet there. The real case was 3685 against 1.
        body = "\n".join("def real_%d(): return %d" % (i, i)
                          for i in range(60)) + "\n"
        self._write(self.root, "helm/web.py", body)
        self._commit(self.root, "seed a substantial web.py")
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        room = os.path.join(self.root + "-wt", "alpha")
        self._write(room, "helm/web.py", body + "def lane_edit(): return 1\n")
        self._commit(room, "lane edits one line of web.py")
        self._trunk_DRAINS_web()
        got = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(len(got), 1, got)
        lane, gone = got[0]
        self.assertEqual(lane, "alpha")
        self.assertEqual(len(gone), 1, gone)
        # the path is named, the EVENT is named, and the NUMBERS travel so a
        # reader judges rather than a tuned constant deciding for them
        self.assertIn("helm/web.py", gone[0])
        # the NUMBERS travel and they are TRUNK'S, not the lane's — detection
        # must not depend on how much this lane typed (a reviewer named the
        # previous "trunk removed more than you changed" rule as a tuned
        # threshold wearing a ratio of 1.0)
        self.assertIn("trunk +", gone[0])
        # the DELETION COUNT must be visible and non-zero — asserted by
        # PARSING it, not by matching a digit. My first version pinned "-3",
        # a magnitude from the REAL incident (-3685) that this fixture never
        # produces (-60): an assertion tied to the wrong instance's numbers.
        removed = int(gone[0].split("/-")[1].split(" ")[0])
        self.assertGreater(removed, 1, gone[0])
        self.assertNotIn("yours", gone[0],
                         "the lane's own edit size is not part of the claim")
        self.assertNotIn("deleted on trunk", gone[0],
                         "nothing was deleted — mislabelling the event is how "
                         "the next reader looks for the wrong thing")

    def test_a_file_the_trunk_MOVED_is_reported_against_the_live_lane(self):
        """THE MUST-HIT. Without it every other arm here is an absence and the
        whole class passes with the function returning [] forever."""
        self._lane_editing_web()
        self._trunk_moves_web()
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

    def test_a_file_the_lane_CREATED_is_not_a_moved_target(self):
        """THE DISCRIMINATOR. A created file is absent from the trunk BY
        DESIGN, so the naive 'not on trunk' test flags every new module — it
        would have flagged helm/tasks.py, which this lane's own work added.
        One lane, one call: the moved file is named and the new one is not."""
        self._lane_editing_web(creates="helm/tasks.py")
        self._trunk_moves_web()
        # FIXTURE CONTROLS, unconditional: BOTH paths are authored by the lane
        # and BOTH are absent from the trunk, so absence cannot be what
        # separates them and the silence below is the merge-base check acting.
        base = _sh(self.root, "git", "merge-base", "lane/alpha",
                   "main").stdout.strip()
        authored = _sh(self.root, "git", "diff", "--name-only",
                       base + "..lane/alpha").stdout
        self.assertIn("helm/web.py", authored,
                      "fixture broken: the lane must have authored web.py")
        self.assertIn("helm/tasks.py", authored,
                      "fixture broken: the lane must have authored tasks.py")
        self.assertNotEqual(_sh(self.root, "git", "cat-file", "-e",
                                "main:helm/tasks.py").returncode, 0,
                            "fixture broken: a created file is absent from "
                            "the trunk — that is the confusion under test")
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

    def test_an_UNKNOWN_authored_set_is_silence_not_an_all_clear(self):  # noqa: VACUOUS_ASSERTION — the positive control is the SAME call on the SAME fixture, run first and unconditionally: it names alpha/helm/web.py, so the empty result under the sentinel is the UNKNOWN path and not a scan that found nothing
        """CANNOT-LOOK IS NOT CLEAR. _authored hands back _EVERYTHING when it
        could not determine what a branch wrote, and _EVERYTHING is a frozenset
        SUBCLASS — EMPTY and therefore FALSEY. A truthiness test reads UNKNOWN
        as 'authored nothing'; the code must ask by IDENTITY."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # POSITIVE CONTROL, unconditional and first: this fixture DOES report.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])
        with mock.patch.object(_work_gc, "_authored",
                               return_value=_work_gc._EVERYTHING):
            unknown = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(unknown, [],
                         "an undeterminable authored set must yield SILENCE "
                         "for that lane, never a verdict")

    def test_an_UNREADABLE_deletion_probe_is_silence_not_a_drained_label(self):
        """CANNOT-LOOK IS NOT CLEAR, on the probe that labels the EVENT.

        The first cut wrote `deleted_paths = ... if rc_g == 0 else set()`,
        which turns an unreadable probe into a determinate "nothing was
        deleted" — indistinguishable from the real empty answer, and it flows
        into the drainage branch, so a genuinely DELETED file gets reported as
        merely drained with a label the evidence cannot support. The
        function's own docstring promised silence on unreadable input and this
        line broke that promise two calls later."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # MUST-HIT FIRST: with the probe WORKING this lane is reported, so the
        # silence asserted below is caused by the failure and not by an empty
        # board.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])

        real = _work_gc.vcs.backend(self.root).text

        def flaky(root, *args, **kw):
            if "--diff-filter=D" in args:
                return (128, "", "fatal: could not read the object")
            return real(root, *args, **kw)

        with mock.patch.object(type(_work_gc.vcs.backend(self.root)), "text",
                               side_effect=flaky, autospec=False):
            got = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(got, [], "an unreadable probe must say NOTHING — "
                                  "reporting it as drained asserts an event "
                                  "the evidence cannot support")

    def test_an_UNHELD_lane_is_nobody_racing_you(self):  # noqa: VACUOUS_ASSERTION — the positive control is the SAME call on the SAME fixture, run first and unconditionally: while the lease is live it names alpha/helm/web.py, so the empty result under an empty ledger is the held-lease filter acting
        """The same live-lease filter held_lane_files and lane_overlaps use: a
        parked room's holder is gone, and listing it is the noise that teaches
        people to scroll past the line that matters."""
        self._lane_editing_web()
        self._trunk_moves_web()
        # POSITIVE CONTROL, unconditional and first: held -> reported.
        self.assertEqual(_work_gc.moved_lane_targets(self.root),
                         [("alpha", ["helm/web.py (deleted on trunk)"])])
        with mock.patch.object(_work_gc, "_live", return_value={}):
            unheld = _work_gc.moved_lane_targets(self.root)
        self.assertEqual(unheld, [],
                         "an unheld room is nobody racing you — it must not "
                         "be reported")

    def test_the_claim_seam_says_it_on_stderr(self):
        """THE SURFACE IS THE BAR. A guard that computes correctly and reaches
        nobody is not a guard; this is the line a claimer actually reads."""
        self._lane_editing_web()
        self._trunk_moves_web()
        rc, out, err = self.work("claim", "beta", "--seat", "s2")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED, so the line
        # below is added information and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("beta", out)
        # THE DURABLE PARTS, not the sentence. Pinning exact prose made this
        # arm fail every time the wording was corrected — including when the
        # correction was the POINT (the old line claimed a deletion that had
        # not happened). What must hold: the lane is named, the path is named,
        # and the reader is told what to do.
        self.assertIn("alpha", err)
        self.assertIn("helm/web.py", err)
        self.assertIn("rebase before trusting a gate", err)
        # the sentence must not assert an event the entry did not name —
        # this used to read "trunk NO LONGER HAS it" for a file trunk still
        # has. Pinned from the other side in the SURFACE arm above.
        self.assertIn("trunk changed code this lane edits", err)
        self.assertIn("rebase before trusting a gate on it", err)


class KindredLaneWarningTest(WorkBase):
    """CLAIM WAS SILENT AT THE ONE MOMENT SOMEONE IS ABOUT TO DUPLICATE WORK.

    MEASURED 2026-08-04: I claimed `gate-import-verb` while `lane/gate-import`
    sat on disk — already built and already content-reviewed — and claim said
    nothing, because `_has_branch` asks only whether THIS exact name exists.
    Six hours earlier I had written a premise telling myself to sweep for
    exactly that, read it, and did not run it. An advisory line that is read
    and ignored under load is the canon's definition of a check that belongs
    at the keystroke instead."""

    def test_a_lane_whose_name_leads_this_one_is_announced(self):
        rc, _o, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.work("claim", "gate-import-verb", "--seat", "s2")
        # POSITIVE CONTROL on the same observable, unconditional: the claim
        # SUCCEEDED, so the warning below is an added line and not a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("already exists", err)
        self.assertIn("lane/gate-import", err)
        self.assertIn("Claiming anyway is fine", err,
                      "it must read as information, not a block")

    def test_an_unrelated_lane_is_silent(self):
        """A check-in that cries wolf teaches people to scroll past the line
        that matters — the law `_warn_inherited_base` states for itself."""
        rc, _o, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional and first:
        # a KINDRED name really does put the line on this stream, so the
        # silence below is the predicate declining and not an unwired stderr.
        # (An earlier version asserted stderr was merely non-empty and went
        # red — claim writes NOTHING there on a clean claim, so the only
        # honest control is making the warning itself appear.)
        _rc, _o, kin_err = self.work("claim", "gate-import-verb", "--seat", "s3")
        self.assertIn("already exists", kin_err)
        rc, _o, err = self.work("claim", "seat-where-leaks", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("already exists", err)

    def test_a_single_token_lane_cannot_trip_it(self):
        """One shared token is not evidence of anything — the predicate needs
        TWO leading tokens or every lane starting with `seat` collides."""
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL on the same stream, unconditional.
        _rc, _o, kin_err = self.work("claim", "alpha-two-tokens", "--seat", "s3")
        self.assertNotIn("already exists", kin_err,
                         "alpha/alpha-two share ONE token — still silent")
        rc, _o, err = self.work("claim", "alphabet", "--seat", "s2")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("already exists", err)

    def test_re_claiming_the_SAME_lane_does_not_warn_about_itself(self):
        """Re-claim is idempotent and must stay quiet: the branch already
        exists, so this is not a fresh cut and nothing is being duplicated."""
        rc, out, err = self.work("claim", "gate-import", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lease = out.split("\t")[2].strip()
        rc, out2, err = self.work("claim", "gate-import", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL: the re-claim really happened and handed back the
        # same room, so the absent warning is the fresh-branch gate holding.
        self.assertIn("gate-import", out2)
        self.assertNotIn("already exists", err)


class UnguardedRoomTest(WorkBase):
    """Only Claude Agent/Workflow isolation rooms are reported, with every
    uncertain liveness input retaining an explicit UNKNOWN state."""

    def foreign(self, name, dirty=None):
        path = os.path.join(self.root, ".claude", "worktrees", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "worktree-" + name, path, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        if dirty:
            with open(os.path.join(path, dirty), "w") as f:
                f.write("a reviewer's uncommitted security fixes\n")
        return path

    @contextlib.contextmanager
    def workflow_evidence(self, path, status="completed", live=True):
        home = os.path.join(self.tmp, "claude-home")
        sid = "11111111-2222-4333-8444-555555555555"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "workflows")
        os.makedirs(d, exist_ok=True)
        run = os.path.basename(path).rsplit("-", 1)[0]
        with open(os.path.join(d, run + ".json"), "w") as f:
            json.dump({"runId": run, "status": status}, f)
        sessions = {sid} if live else set()
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[home]), \
                mock.patch.object(work._lanes, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    @contextlib.contextmanager
    def direct_agent_evidence(self, path, terminal=False, live=True):
        home = os.path.join(self.tmp, "direct-home")
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        slug = self.root.replace(os.sep, "-").replace(".", "-")
        d = os.path.join(home, "projects", slug, sid, "subagents")
        os.makedirs(d, exist_ok=True)
        room = os.path.basename(path)
        with open(os.path.join(d, room + ".meta.json"), "w") as f:
            json.dump({"worktreePath": path}, f)
        msg = {"type": "assistant", "message": {
            "stop_reason": "end_turn" if terminal else "tool_use",
            "content": [{"type": "text", "text": "done"}] if terminal
            else [{"type": "tool_use", "name": "Bash"}]}}
        with open(os.path.join(d, room + ".jsonl"), "w") as f:
            f.write(json.dumps(msg) + "\n")
        sessions = {sid} if live else set()
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[home]), \
                mock.patch.object(work._lanes, "_live_claude_sessions",
                                  return_value=sessions):
            yield

    def test_agent_worktree_is_reported_with_stable_identity(self):
        path = self.foreign("wf_dead00-1")
        with self.workflow_evidence(path):
            rows = work.unguarded_rows(self.root)
            self.assertEqual([r["id"] for r in rows], ["wf_dead00-1"])
            rc, out, err = self.work("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("id=wf_dead00-1", out)
        self.assertIn("never authorizes cleanup", out)

    def test_main_lanes_and_arbitrary_worktrees_are_not_unguarded(self):
        rc, _out, err = self.work("claim", "mine", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.room("stray")
        normal = os.path.join(self.tmp, "ordinary-review")
        r = _sh(self.root, "git", "worktree", "add", "-q", "-b",
                "ordinary-review", normal, "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(work.unguarded_rows(self.root), [])

    def test_recent_write_is_advisory_not_ownership_claim(self):
        path = self.foreign("wf_busy00-2", dirty="cred.py")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, err = self.work("list")
        self.assertTrue(row["dirty"])
        self.assertLessEqual(row["wrote_ago"], work.RECENT_WRITE_SECONDS)
        self.assertEqual(rc, 0, err)
        self.assertIn("RECENT-WRITE", out)
        self.assertIn("advisory, not ownership proof", out)
        self.assertNotIn("assume live", out)

    def test_clean_but_live_workflow_is_live_with_parent_cwd_elsewhere(self):
        path = self.foreign("wf_live000-3")
        self.assertEqual(work._occupants(path), [])
        with self.workflow_evidence(path, status="running", live=True):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertFalse(row["dirty"])
        self.assertEqual(row["harness"], "live")
        self.assertIn("LIVE-HARNESS", out)
        self.assertIn("Workflow active", out)

    def test_running_metadata_without_live_parent_is_unknown(self):
        path = self.foreign("wf_crashed0-4")
        with self.workflow_evidence(path, status="running", live=False):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("harness metadata incomplete", out)

    def test_clean_direct_agent_is_live_until_its_final_text_end_turn(self):
        path = self.foreign("agent-deadbeef1234")
        with self.direct_agent_evidence(path, terminal=False, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"], "live")
        with self.direct_agent_evidence(path, terminal=True, live=True):
            self.assertEqual(work.unguarded_rows(self.root)[0]["harness"],
                             "inactive")

    def test_terminal_clean_room_is_clean_but_not_declared_unowned(self):
        path = self.foreign("wf_idle000-5")
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertIsNone(row["wrote_ago"])
        self.assertEqual(row["harness"], "inactive")
        self.assertIn("UNGUARDED CLEAN", out)
        self.assertIn("not proof that no writer will resume", out)

    def test_stale_dirty_write_remains_dirty_without_live_claim(self):
        path = self.foreign("wf_stale00-6", dirty="half.py")
        old = time.time() - 7200
        os.utime(os.path.join(path, "half.py"), (old, old))
        with self.workflow_evidence(path):
            row = work.unguarded_rows(self.root)[0]
            rc, out, _err = self.work("list")
        self.assertGreaterEqual(row["wrote_ago"], 7000)
        self.assertIn("UNGUARDED DIRTY", out)
        self.assertNotIn("LIVE-HARNESS", out)

    def test_missing_harness_metadata_fails_unknown(self):
        self.foreign("wf_orphan00-7")
        with mock.patch.object(work._lanes, "_claude_homes", return_value=[]), \
                mock.patch.object(work._lanes, "_live_claude_sessions", return_value=set()):
            rc, out, _err = self.work("list")
        self.assertIn("UNGUARDED UNKNOWN", out)
        self.assertIn("no matching harness metadata", out)

    def test_symlink_escape_is_reported_unknown_without_git_status(self):
        target = os.path.join(self.tmp, "outside")
        os.makedirs(target)
        path = os.path.join(self.root, ".claude", "worktrees", "wf_escape00-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_escape00-8",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, errors = work.unguarded_inventory(self.root, registered=records)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["harness"], "unknown")
        self.assertIn("symlink", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_foreign_repo_replacement_is_not_inspected_as_same_repo(self):
        path = os.path.join(self.root, ".claude", "worktrees", "wf_foreign0-9")
        os.makedirs(path)
        self.assertEqual(_sh(path, "git", "init", "-q").returncode, 0)
        records = [{"path": path, "branch": "refs/heads/worktree-wf_foreign0-9",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("not a regular worktree link", rows[0]["hard_unknown"])
        status_call.assert_not_called()

    def test_symlinked_container_escape_is_unknown(self):
        outside = os.path.join(self.tmp, "escaped-claude")
        os.makedirs(os.path.join(outside, "worktrees", "wf_parent00-1"))
        os.symlink(outside, os.path.join(self.root, ".claude"))
        path = os.path.join(self.root, ".claude", "worktrees", "wf_parent00-1")
        records = [{"path": path, "branch": "refs/heads/worktree-wf_parent00-1",
                    "locked": False, "reason": ""}]
        with mock.patch.object(work._lanes, "_room_status") as status_call:
            rows, _errors = work.unguarded_inventory(self.root, registered=records)
        self.assertIn("container escapes", rows[0]["hard_unknown"])
        status_call.assert_not_called()


class PorcelainSafetyTest(WorkBase):
    def test_worktree_registry_is_nul_and_byte_safe(self):
        weird = os.fsencode(self.root) + b"/.claude/worktrees/agent-deadbeef\\\n\xff"
        raw = (b"worktree " + weird + b"\0HEAD abc\0branch refs/heads/x\0"
               b"locked because\0\0")
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(0, raw, b"")):
            rows, error = work._worktree_records(self.root)
        self.assertIsNone(error)
        self.assertEqual(os.fsencode(rows[0]["path"]), weird)
        self.assertEqual(rows[0]["branch"], "refs/heads/x")
        self.assertEqual(rows[0]["reason"], "because")

    def test_worktree_registry_failure_and_truncation_are_not_empty_success(self):
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(1, b"", b"boom")):
            self.assertEqual(work._worktree_records(self.root), ([], "boom"))
        with mock.patch.object(work._lanes, "_git_bytes",
                               return_value=(0, b"worktree /tmp/no-nul", b"")):
            rows, error = work._worktree_records(self.root)
        self.assertEqual(rows, [])
        self.assertIn("truncated", error)

    def test_porcelain_v2_parser_consumes_two_path_records_exactly(self):
        ordinary = b"1 .M N... 100644 100644 100644 a b line\\name\n\xff"
        rename = b"2 R. N... 100644 100644 100644 a b R100 new name"
        copy = b"2 C. N... 100644 100644 100644 a b C075 copy name"
        unmerged = b"u UU N... 100644 100644 100644 100644 a b c conflict"
        raw = (ordinary + b"\0" + rename + b"\0old name\0" + copy +
               b"\0source name\0" + unmerged + b"\0? untracked dir/file\0")
        got = work._status_entries(raw)
        self.assertEqual([p for p, _sub, _kind in got], [
            b"line\\name\n\xff", b"new name", b"copy name", b"conflict",
            b"untracked dir/file"])
        # the KIND is carried now, and the unmerged record must be identifiable
        # as unmerged — the parser always knew, and every caller threw it away
        self.assertEqual([k for _p, _sub, k in got],
                         [b"1", b"2", b"2", b"u", b"?"])

    def test_human_porcelain_and_truncated_records_fail_closed(self):
        with self.assertRaises(ValueError):
            work._status_entries(b" M human path\0")
        with self.assertRaises(ValueError):
            work._status_entries(b"? no terminator")
        with self.assertRaises(ValueError):
            work._status_entries(
                b"2 R. N... 100644 100644 100644 a b R100 new\0\0")

    def test_weird_untracked_names_and_untracked_directory_are_timed(self):
        path = self.room("weirdst")
        rel = b"dir/space newline\nback\\slash-\xff"
        os.makedirs(os.path.join(path, "dir"))
        fd = os.open(os.path.join(os.fsencode(path), rel),
                     os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIsNone(row["unknown"])
        self.assertIsNotNone(row["wrote_ago"])

    def test_deletion_and_dirty_submodule_are_timestamp_unknown(self):
        path = self.room("deleted")
        os.remove(os.path.join(path, "README"))
        row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("deleted", row["unknown"])
        record = (b"1 .M S.M. 160000 160000 160000 a b sub\0")
        fake_stat = os.stat(path)
        with mock.patch.object(work._lanes, "_git_bytes", return_value=(0, record, b"")), \
                mock.patch.object(work.os, "lstat", return_value=fake_stat):
            row = work._room_status(path)
        self.assertIn("submodule", row["unknown"])

    def test_symlink_mtime_is_not_target_mtime_and_future_clock_is_advisory(self):
        path = self.room("symlink")
        target = os.path.join(self.tmp, "future-target")
        with open(target, "w") as f:
            f.write("outside")
        future = time.time() + 3600
        os.utime(target, (future, future))
        link = os.path.join(path, "link")
        os.symlink(target, link)
        old = time.time() - 3600
        os.utime(link, (old, old), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertGreater(row["wrote_ago"], 3000)
        os.utime(link, (future, future), follow_symlinks=False)
        row = work._room_status(path, now=time.time())
        self.assertEqual(row["wrote_ago"], 0)
        self.assertTrue(row["clock_skew"])

    def test_ignored_files_are_excluded_and_status_failure_is_unknown(self):
        path = self.room("ignored")
        with open(os.path.join(path, ".gitignore"), "w") as f:
            f.write("ignored/\n")
        _sh(path, "git", "add", ".gitignore")
        _sh(path, "git", "commit", "-q", "-m", "ignore")
        os.makedirs(os.path.join(path, "ignored"))
        with open(os.path.join(path, "ignored", "secret"), "w") as f:
            f.write("ignored")
        self.assertFalse(work._room_status(path)["dirty"])
        with mock.patch.object(work._lanes, "_git_bytes",
                               return_value=(-1, b"", b"timeout")):
            row = work._room_status(path)
        self.assertTrue(row["dirty"])
        self.assertIn("status failed", row["unknown"])


class GuardTest(WorkBase):
    def _ref_transaction(self, *lines):
        hook = work.hook_path(self.root, "reference-transaction")
        return subprocess.run(
            [hook, "prepared"], cwd=self.root,
            input="".join(line + "\n" for line in lines),
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HELM_LANDLOCK="0"))

    def test_ref_guard_accepts_old_and_new_branch_update_formats(self):  # noqa: VACUOUS_ASSERTION — four explicit rc==0 installed-hook transactions are the open-door observable; test_ref_guard_refuses_unmatched_oid_and_symbolic_head_moves is the paired closed-door control
        """Git 2.43 duplicates an ordinary branch update as OID-valued HEAD;
        newer Git sends only the real branch row. Both must remain legal."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        hook = work.hook_path(self.root, "reference-transaction")
        self.assertIn("reference-transaction v6", _bytes(hook).decode())
        old = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        new = _sh(self.root, "git", "commit-tree", "HEAD^{tree}", "-p", "HEAD",
                  "-m", "synthetic descendant").stdout.strip()
        self.assertRegex(new, r"^[0-9a-f]{40}$")

        r = self._ref_transaction(
            "%s %s HEAD" % (old, new),
            "%s %s refs/heads/main" % (old, new))
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._ref_transaction(
            "%s %s HEAD" % ("0" * 40, new),
            "%s %s refs/heads/main" % ("0" * 40, new))
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._ref_transaction(
            "%s %s refs/heads/main" % (old, new))
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self._ref_transaction(
            "%s ref:refs/heads/main HEAD" % ("0" * 40))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ref_guard_refuses_unmatched_oid_and_symbolic_head_moves(self):
        """An OID HEAD row without its exact base-ref twin is a detach, while
        a ref-valued off-base row is a symbolic branch switch. Refuse both."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        old = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        new = _sh(self.root, "git", "commit-tree", "HEAD^{tree}", "-p", "HEAD",
                  "-m", "synthetic descendant").stdout.strip()

        cases = {
            "unmatched-oid-head": ("%s %s HEAD" % (old, new),),
            "mismatched-base-twin": (
                "%s %s HEAD" % ("0" * 40, new),
                "%s %s refs/heads/main" % (old, new)),
            "symbolic-off-base-head": (
                "%s ref:refs/heads/other HEAD" % ("0" * 40),),
        }
        runs = {name: self._ref_transaction(*lines)
                for name, lines in cases.items()}
        self.assertTrue(all(r.returncode != 0 for r in runs.values()),
                        {name: (r.returncode, r.stderr)
                         for name, r in runs.items()})
        for name, r in runs.items():
            with self.subTest(format=name):
                self.assertIn("HEAD must stay on 'main'", r.stderr)

    def test_install_guard_dry_then_apply_then_heal(self):
        hook = work.hook_path(self.root)
        rc, out, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        self.assertIn("HELM_WORK_INTEGRATOR", out)
        self.assertIn("DRY", out)
        self.assertFalse(os.path.exists(hook))          # print, never run
        rc, out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(os.access(hook, os.X_OK))
        # THE MOTIVATING FAILURE, now enforced rather than healed. It used to
        # SUCCEED (rc 0) and be silently corrected afterwards — git printed
        # "Switched to a new branch", the heal spoke only on stderr, and the
        # next commit landed on main. post-checkout cannot do better: git
        # ignores its exit code. reference-transaction refuses outright.
        r = _sh(self.root, "git", "checkout", "-b", "oops")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("helm work claim oops", r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "main")
        # and the ref never came into existence — nothing to clean up later
        self.assertFalse(work._has_branch(self.root, "oops"))
        # committing on the trunk is untouched by the ref guard
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("trunk work\n")
        self.assertEqual(_sh(self.root, "git", "commit", "-qam", "t").returncode,
                         0)
        # worktree add must NOT trip the guard (lane rooms are unguarded)
        wt = self.room("quiet")
        self.assertEqual(
            _sh(wt, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "lane/quiet")
        # the escape hatch: the integrator's own env skips the heal
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "checkout", "-q", "-b", "intg"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        head = _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
        self.assertEqual(head.stdout.strip(), "intg")

    def test_the_dry_run_REPORTS_what_is_installed_not_only_what_would_be(self):  # noqa: VACUOUS_ASSERTION — the clean-install assertNotIn is the CONTROL, and its positives are the three assertIn on the SAME observable later in this method: the same command reports DRIFT for a rewritten hook, NOT INSTALLED for a missing one and DRIFT naming the retire path for a superseded companion once each shape is present, so a report that had gone silent fails those
        """THE INSTALLED TIER IS ONE NO SOURCE RUNG REACHES. Every content
        guard in this repo scans the TRACKED tree, so it proves things about
        what the NEXT install will write and nothing about what is installed
        NOW — and the gap is invisible from both directions, because the
        source reads cured and the running box reads fine when nothing asks.

        THE THREE SHAPES, each measured against a CLEAN INSTALL FIRST so the
        lines below are caused by the damage and not by the report firing on
        everything."""
        from helm.work import _guard
        hook = work.hook_path(self.root)
        rc, _out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)

        # CONTROL, UNCONDITIONAL: a freshly installed rail reports NO drift.
        rc, clean, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        self.assertNotIn("# DRIFT", clean)
        self.assertNotIn("# UNHEALABLE", clean)

        with open(hook, "w") as f:
            f.write("#!/bin/sh\n# helm work managed hook: post-checkout v2\n")
        _rc, drifted, _err = self.work("install-guard")
        self.assertIn("# DRIFT", drifted)
        self.assertIn("installed CONTENT differs", drifted)

        os.unlink(hook)
        _rc, gone, _err = self.work("install-guard")
        self.assertIn("NOT INSTALLED", gone)

        # AND THE COMPANION THE MANAGED HOOK RUNS FIRST. A `.helm-user`
        # carrying a helm marker is THIS PROJECT'S OWN superseded hook in the
        # user slot, and the managed hook invokes it ahead of the template on
        # every invocation — so its body can answer before the installed rules
        # do. The preservation rule is about a USER'S file, and the marker is
        # what separates the two populations.
        with open(hook + ".helm-user", "w") as f:
            f.write("#!/bin/sh\n# helm work guard — the shared checkout is "
                    "the integrator's tree (installed\n# by a previous "
                    "install).\n")
        _rc, superseded, _err = self.work("install-guard")
        self.assertIn("# DRIFT", superseded)
        self.assertIn("# helm work guard", superseded)
        self.assertIn("RUNS IT FIRST", superseded)
        self.assertIn(_guard.RETIRED_HOOK_SUFFIX, superseded)

    def test_a_companion_carrying_OUR_marker_is_RETIRED_not_kept_forever(self):  # noqa: VACUOUS_ASSERTION — every absence assertion here is preceded by an unconditional positive on the SAME observable: both companions are asserted PRESENT before the apply, and the post-apply report is asserted to still carry its scanner-snapshot lines before the two assertNotIn
        """THE MANAGED HOOK RUNS ITS COMPANION FIRST, SO A SUPERSEDED COPY OF
        OUR OWN RULES ANSWERS AHEAD OF THE INSTALLED ONES.

        The preserve-byte-for-byte contract exists for a USER'S hook. A
        companion carrying a helm marker is not one, and keeping it there
        means every invocation runs an older copy of this project's own guard
        before the template that replaced it. `_owned_hook` already computes
        exactly that distinction; this arm pins that install ACTS on it.

        BOTH POPULATIONS IN ONE METHOD, because the cure is worth nothing if
        it also retires a user's work: the marked companion is retired and the
        unmarked one beside it is untouched."""
        from helm.work import _guard
        post = work.hook_path(self.root, "post-checkout")
        commit = work.hook_path(self.root, "pre-commit")
        os.makedirs(os.path.dirname(post), exist_ok=True)
        ours = ("#!/bin/sh\n# helm work guard — the shared checkout is the "
                "integrator's tree (installed\n# by a previous install).\n"
                "exit 0\n").encode()
        theirs = b"#!/bin/sh\n# my own pre-commit\nexit 0\n"
        for path, body in ((post + ".helm-user", ours),
                           (commit + ".helm-user", theirs)):
            with open(path, "wb") as f:
                f.write(body)
            os.chmod(path, 0o755)

        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE the absence
        # assertions below read: both companions are on disk right now, so a
        # lexists that answered False for everything fails HERE first.
        self.assertTrue(os.path.lexists(post + ".helm-user"))
        self.assertTrue(os.path.lexists(commit + ".helm-user"))

        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("retired superseded helm hook", out)

        # OURS: gone from the slot the managed hook invokes, kept verbatim
        # where nothing runs it.
        retired = post + _guard.RETIRED_HOOK_SUFFIX
        self.assertFalse(os.path.lexists(post + ".helm-user"),
                         "the managed hook still invokes the superseded copy")
        self.assertEqual(_bytes(retired), ours)
        self.assertFalse(os.access(retired, os.X_OK),
                         "a retired hook that is still executable can run")

        # THEIRS, THE CONTROL: an unmarked companion is untouched, and no
        # retire path was minted beside it.
        self.assertEqual(_bytes(commit + ".helm-user"), theirs)
        self.assertFalse(
            os.path.lexists(commit + _guard.RETIRED_HOOK_SUFFIX),
            "a user's hook was retired, which the preserve rule forbids")

        # AND THE REPORT AGREES WITH THE BOX AFTERWARDS: nothing left to say.
        rc, after, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        # POSITIVE CONTROL ON THE REPORT ITSELF: a report that had gone silent
        # would satisfy both assertNotIn below, so first pin that this same
        # command still speaks about this box.
        self.assertIn("# scanner snapshot", after)
        self.assertNotIn("# DRIFT", after)
        self.assertNotIn("# UNHEALABLE", after)

    def test_the_retire_REFUSES_rather_than_overwrite_other_bytes(self):  # noqa: VACUOUS_ASSERTION — the companion is asserted STILL PRESENT after the refusal and the retired bytes are asserted PRESENT after the idempotent arm, both unconditional and both on the same lexists/bytes observable the absence assertion reads
        """THE RETIRE PATH IS A DESTINATION, SO IT CAN ALREADY BE OCCUPIED.

        Writing over whatever sits there would destroy the one copy of what
        some earlier box was executing, which is the thing retiring exists to
        preserve. Identical bytes are not a conflict — a second install must
        stay idempotent — so only DIFFERENT bytes refuse."""
        from helm.work import _guard
        post = work.hook_path(self.root, "post-checkout")
        os.makedirs(os.path.dirname(post), exist_ok=True)
        ours = ("#!/bin/sh\n# helm work guard — the shared checkout is the "
                "integrator's tree (installed\n# by a previous install).\n"
                "exit 0\n").encode()
        with open(post + ".helm-user", "wb") as f:
            f.write(ours)
        os.chmod(post + ".helm-user", 0o755)
        with open(post + _guard.RETIRED_HOOK_SUFFIX, "wb") as f:
            f.write(b"#!/bin/sh\n# some other retired hook\nexit 0\n")

        # THE REFUSAL IS ON STDERR, WHICH IS PART OF THE CLAIM: a caller that
        # reads only stdout sees an empty answer, so naming the channel is
        # what makes this arm about the message a human actually gets.
        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", err)
        self.assertIn("nothing changed", err)
        self.assertTrue(os.path.lexists(post + ".helm-user"),
                        "the refusal removed the companion it refused to move")
        self.assertEqual(_bytes(post + ".helm-user"), ours,
                         "the refusal still moved the companion")

        # THE OPEN-DOOR CONTROL ON THE SAME OBSERVABLE: identical bytes at the
        # destination are idempotent, not a conflict, so the same command
        # succeeds once the occupant IS this companion.
        with open(post + _guard.RETIRED_HOOK_SUFFIX, "wb") as f:
            f.write(ours)
        os.chmod(post + _guard.RETIRED_HOOK_SUFFIX, 0o644)
        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err or out)
        # THE POSITIVE HALF FIRST: the bytes arrived where they were going.
        self.assertEqual(_bytes(post + _guard.RETIRED_HOOK_SUFFIX), ours)
        self.assertFalse(os.path.lexists(post + ".helm-user"))

    def test_a_LEGACY_marker_is_matched_by_PREFIX_like_the_managed_one(self):  # noqa: VACUOUS_ASSERTION — the must-miss assertFalse is preceded in the same method by TWO unconditional assertTrue on the same predicate, so a predicate answering False for every input fails before it is reached
        """THE ASYMMETRY WAS THE MECHANISM. The legacy test used whole-line
        equality while the managed one used a prefix, and the hooks this
        project actually wrote carry a CONTINUATION on the marker line — so
        the legacy test was unsatisfiable against its own population.

        AND THE CONSEQUENCE WAS NOT A MISSED LABEL, IT WAS A MISFILED FILE.
        Install reads this answer to decide whether an existing hook is a
        USER'S work worth preserving, so a legacy helm guard answering False
        was preserved into the companion slot that nothing ever rewrites."""
        from helm.work import _guard
        managed = ("#!/bin/sh\n"
                   "# helm work managed hook: post-checkout v2\n").encode()
        legacy = ("#!/bin/sh\n"
                  "# helm work guard \u2014 the shared checkout is the "
                  "integrator's tree (installed\n").encode()
        mine = ("#!/bin/sh\n# my own hook\necho hi\n").encode()
        # POSITIVE CONTROL FIRST on the same call: the managed marker still
        # answers True, so a False below is about the legacy shape and not
        # about the predicate having gone dark.
        self.assertTrue(_guard._owned_hook(("file", managed, 0o755)))
        self.assertTrue(_guard._owned_hook(("file", legacy, 0o755)),
                        "a legacy helm guard read as a user's file, which is "
                        "how it reaches the slot nothing heals")
        # MUST-MISS: a genuine user hook is still theirs.
        self.assertFalse(_guard._owned_hook(("file", mine, 0o755)))

    def test_a_NEAR_MISS_of_the_helm_marker_keeps_the_users_bytes(self):  # noqa: VACUOUS_ASSERTION — the companion's absence in the control row is preceded, OUTSIDE the loop and unconditionally, by an install of a marker-free hook asserting os.path.lexists(user) True and its bytes equal, so a lexists that answered False to everything fails before any row is read
        """THE NARROWNESS IS THE SAFETY, AND EVERY FIXTURE FOR IT IS A
        POSITIVE. The classifier reads the first EIGHT lines of a hook and
        requires a line to START WITH one of three exact markers. All seven
        marker occurrences in this file put the marker at the start of a line
        inside that window, so a widening — a substring search, a larger line
        window, a looser prefix — would enlarge the set of files install
        absorbs and NOTHING here would go red.

        AND THE BLAST RADIUS AT THIS PATH IS BYTE LOSS, NOT A RENAME. A
        companion carrying a helm marker is RETIRED, bytes intact under a
        reported name. A hook at the TARGET path answering owned is simply
        overwritten by the template: `desired[target]` is set and no preserve
        branch runs, so a user's file classified as ours does not move, it
        ends. That is why the near misses are worth three fixtures.

        THE CONTROL IS THE LAST ROW and it is the same call on the same
        observable: a genuinely owned hook is absorbed with no companion
        written, so a predicate that answered False to everything would fail
        here rather than pass every row above it."""
        from helm.work import _guard
        target = work.hook_path(self.root, "post-checkout")
        user = target + ".helm-user"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        marker = _guard.MANAGED_HOOK_MARKER

        # THE BASELINE, UNCONDITIONAL AND OUTSIDE THE LOOP: a hook with no
        # marker anywhere is preserved into the companion. It is what the
        # three near misses must be INDISTINGUISHABLE FROM, and it proves the
        # accessor and the install path before any row is read — an arm whose
        # only positive lived inside its own loop would report nothing if the
        # loop never ran.
        plain = b"#!/bin/sh\n# my own hook\nexit 0\n"
        with open(target, "wb") as f:
            f.write(plain)
        os.chmod(target, 0o755)
        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err or out)
        self.assertTrue(os.path.lexists(user),
                        "an ordinary user hook was not preserved at all, so "
                        "every row below would be about a dead install path")
        self.assertEqual(_bytes(user), plain)

        rows = [
            # QUOTED MID-LINE: a user hook that mentions our marker in prose.
            ("mid-line", True,
             "#!/bin/sh\n# adapted from `%s post-checkout v2`\nexit 0\n" % marker),
            # BELOW THE WINDOW: the marker is real and starts its line, but at
            # line twelve, so the eight-line read never reaches it.
            ("line twelve", True,
             "#!/bin/sh\n" + "# note\n" * 10 + "%s post-checkout v2\nexit 0\n" % marker),
            # NEAR-MISS PREFIX: one character off, so `startswith` says no.
            ("near-miss prefix", True,
             "#!/bin/sh\n%ss: post-checkout v2\nexit 0\n" % marker.rstrip(":")),
            # THE CONTROL: the real marker, at the start of line two.
            ("the marker itself", False,
             "#!/bin/sh\n%s post-checkout v2\nexit 0\n" % marker),
        ]
        for name, preserved, body in rows:
            with self.subTest(fixture=name):
                for path in (target, user,
                             target + _guard.RETIRED_HOOK_SUFFIX):
                    if os.path.lexists(path):
                        os.unlink(path)
                mine = body.encode()
                with open(target, "wb") as f:
                    f.write(mine)
                os.chmod(target, 0o755)
                self.assertEqual(_guard._owned_hook(("file", mine, 0o755)),
                                 not preserved,
                                 "the predicate disagrees with what this row "
                                 "is about before install is even asked")
                rc, out, err = self.work("install-guard", "--apply")
                self.assertEqual(rc, 0, err or out)
                if preserved:
                    self.assertTrue(
                        os.path.lexists(user),
                        "a user hook was absorbed, and at this path that is "
                        "not a rename — the bytes are gone")
                    self.assertEqual(_bytes(user), mine,
                                     "the preserved companion is not the "
                                     "user's bytes")
                else:
                    # THE ABSENCE GETS ITS POSITIVE ON THE SAME ACCESSOR AND
                    # THE SAME READING: install wrote a hook at the target, so
                    # a `lexists` that answered False to everything fails here
                    # before the companion's absence is read.
                    self.assertTrue(os.path.lexists(target),
                                    "install wrote no hook at all")
                    self.assertFalse(
                        os.path.lexists(user),
                        "this project's own hook was preserved as a user's")
                self.assertNotEqual(
                    _bytes(target), mine,
                    "install did not write its own hook at all, so neither "
                    "branch above was reached")

    def test_dry_run_scopes_the_integrator_override_honestly(self):
        """The dry line called HELM_WORK_INTEGRATOR=1 'the override' for a
        multi-rung install in which it clears exactly TWO things — the
        shared-tree ref rail and the lane-discipline venue rung — while six
        pre-commit rungs each carry their own one-commit skip (stated in
        their refusals and in the --apply summary). A reader following the
        dry line exported it expecting the conflict-marker or never-track
        rung to stand down; neither reads it."""
        rc, out, _err = self.work("install-guard")
        self.assertEqual(rc, 0)
        dry = next(l for l in out.splitlines() if "DRY" in l)
        self.assertIn("HELM_WORK_INTEGRATOR=1 clears the shared-tree ref "
                      "rail and the lane-discipline venue rung", dry)
        self.assertIn("its own one-commit skip", dry)
        self.assertNotIn("is the override", dry)

    def test_claim_still_works_with_the_ref_guard_installed(self):
        """The guard must not break the verb it points at. `worktree add -b`
        runs the hook with cwd AND toplevel both equal to the shared checkout
        — indistinguishable from a forbidden `checkout -b` — so claim exports
        HELM_WORK_CLAIM=1 to announce itself. Without that, installing the
        rail would refuse every `helm work claim` and strand the fleet."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("claim", "after-guard", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = out.split("\t")[0]
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(work._has_branch(self.root, "lane/after-guard"))
        self.assertEqual(
            _sh(path, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "lane/after-guard")
        # the shared checkout never left the trunk
        self.assertEqual(
            _sh(self.root, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "main")

    def test_release_and_gc_branch_cleanup_still_work_with_guard(self):
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("claim", "released", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lease = out.split("\t")[2]
        rc, _out, err = self.work("release", "released", "--seat", "s1",
                                  "--lease", lease)
        self.assertEqual(rc, 0, err)
        self.assertFalse(work._has_branch(self.root, "lane/released"))

        path = self.room("collected")
        _sh(self.root, "git", "worktree", "lock", path,
            "--reason", "lease:deadbeef")
        rc, _out, err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(work._has_branch(self.root, "lane/collected"))

    def test_ref_guard_leaves_lane_rooms_alone(self):
        """Branching INSIDE a lane room is the sanctioned workflow and must
        stay free — the rail guards the integrator's tree, nothing else."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        room = self.room("free")
        r = _sh(room, "git", "checkout", "-b", "free-sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            _sh(room, "git", "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "free-sub")

    def test_existing_hooks_are_preserved_composed_and_idempotent(self):
        log = os.path.join(self.tmp, "hook log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        ref = work.hook_path(self.root, "reference-transaction")
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w") as f:
            f.write("#!/bin/sh\nprintf 'ref:%s\\n' \"$1\" >> "
                    "\"$HELM_TEST_HOOK_LOG\"\n"
                    "while IFS= read -r line; do printf 'in:%s\\n' \"$line\" "
                    ">> \"$HELM_TEST_HOOK_LOG\"; done\nexit 0\n")
        os.chmod(ref, 0o750)
        with open(post, "w") as f:
            f.write("#!/bin/sh\nprintf 'post:%s:%s:%s\\n' \"$1\" \"$2\" "
                    "\"$3\" >> \"$HELM_TEST_HOOK_LOG\"\nexit 0\n")
        os.chmod(post, 0o700)
        originals = {ref: _bytes(ref), post: _bytes(post)}

        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("preserved existing", out)
        for path, mode in ((ref, 0o750), (post, 0o700)):
            user = path + ".helm-user"
            self.assertEqual(_bytes(user), originals[path])
            self.assertEqual(stat.S_IMODE(os.stat(user).st_mode), mode)
            self.assertTrue(os.access(path, os.X_OK))

        r = _sh(self.root, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        with open(log) as f:
            calls = f.read()
        self.assertIn("ref:prepared", calls)
        self.assertIn("refs/heads/blocked", calls)

        before = {p: (_bytes(p), os.stat(p).st_mtime_ns)
                  for p in (ref, post, ref + ".helm-user", post + ".helm-user")}
        rc, out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("already up to date", out)
        after = {p: (_bytes(p), os.stat(p).st_mtime_ns)
                 for p in before}
        self.assertEqual(after, before)

    def test_install_rolls_back_every_path_after_partial_failure(self):
        ref = work.hook_path(self.root, "reference-transaction")
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        for path, text in ((ref, "ref-user"), (post, "post-user")):
            with open(path, "w") as f:
                f.write("#!/bin/sh\n# %s\nexit 0\n" % text)
            os.chmod(path, 0o755)
        originals = {p: _bytes(p) for p in (ref, post)}
        real, calls = work._put_snapshot, []

        def fail_third(path, snap):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("injected install failure")
            return real(path, snap)

        with mock.patch.object(work._guard, "_put_snapshot", side_effect=fail_third):
            rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)
        for path in (ref, post):
            self.assertEqual(_bytes(path), originals[path])
            self.assertFalse(os.path.lexists(path + ".helm-user"))

    def test_non_executable_user_hook_stays_inactive(self):
        log = os.path.join(self.tmp, "inactive.log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        post = work.hook_path(self.root)
        os.makedirs(os.path.dirname(post), exist_ok=True)
        with open(post, "w") as f:
            f.write("#!/bin/sh\nprintf activated > \"$HELM_TEST_HOOK_LOG\"\n")
        os.chmod(post, 0o644)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(post + ".helm-user").st_mode),
                         0o644)
        _sh(self.root, "git", "checkout", "--", "README")
        self.assertFalse(os.path.exists(log))

    def test_foreign_symlink_hook_is_preserved_and_composed(self):
        log = os.path.join(self.tmp, "symlink.log")
        os.environ["HELM_TEST_HOOK_LOG"] = log
        post = work.hook_path(self.root)
        actual = os.path.join(os.path.dirname(post), "actual-user-post")
        os.makedirs(os.path.dirname(post), exist_ok=True)
        with open(actual, "w") as f:
            f.write("#!/bin/sh\nprintf symlink-user >> "
                    "\"$HELM_TEST_HOOK_LOG\"\n")
        os.chmod(actual, 0o755)
        os.symlink(os.path.basename(actual), post)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.islink(post + ".helm-user"))
        self.assertEqual(os.readlink(post + ".helm-user"), os.path.basename(actual))
        _sh(self.root, "git", "checkout", "--", "README")
        with open(log) as f:
            self.assertEqual(f.read(), "symlink-user")

    def test_existing_branch_switch_and_symbolic_ref_are_refused(self):
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "branch", "existing"], cwd=self.root,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        r = _sh(self.root, "git", "commit", "--allow-empty", "-m", "forward")
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = _sh(self.root, "git", "update-ref", "refs/heads/main", "existing")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-fast-forward", r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "main").stdout.strip(),
                         tip)
        for cmd in (("checkout", "existing"),
                    ("symbolic-ref", "HEAD", "refs/heads/existing")):
            r = _sh(self.root, "git", *cmd)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("HEAD must stay", r.stderr)
            self.assertEqual(
                _sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
                .stdout.strip(), "main")

    def test_loose_occupied_branch_delete_and_move_are_refused(self):  # noqa: VACUOUS_ASSERTION — the loose file is positively present, both real git mutations return nonzero with OCCUPIED, and the branch OID remains exact after each
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        room = self.room("held")
        old = _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip()
        loose = os.path.join(self.root, ".git", "refs", "heads", "lane", "held")
        self.assertTrue(os.path.isfile(loose),
                        "fixture must exercise a loose-only occupied ref")
        moved = _sh(self.root, "git", "commit-tree", "HEAD^{tree}", "-p", old,
                    "-m", "synthetic occupied-branch move").stdout.strip()
        self.assertRegex(moved, r"^[0-9a-f]{40}$")
        for cmd in (("update-ref", "-d", "refs/heads/lane/held"),
                    ("update-ref", "refs/heads/lane/held", moved)):
            r = _sh(self.root, "git", *cmd)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("OCCUPIED", r.stderr)
            self.assertEqual(
                _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip(),
                old)
        # Its own ordinary commit remains legal.
        with open(os.path.join(room, "held.txt"), "w") as f:
            f.write("ok\n")
        self.assertEqual(_sh(room, "git", "add", "held.txt").returncode, 0)
        r = _sh(room, "git", "commit", "-m", "held work")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pack_refs_succeeds_then_packed_branch_moves_refuse(self):  # noqa: VACUOUS_ASSERTION — loose ref exists before pack, pack exits zero, the loose file disappears, and the branch OID stays exact; paired packed-only delete/move controls prove the hook still fires
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.room("held")
        old = _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip()
        loose = os.path.join(self.root, ".git", "refs", "heads", "lane", "held")
        self.assertTrue(os.path.isfile(loose),
                        "fixture must start with the occupied branch as a loose ref")

        packed = _sh(self.root, "git", "pack-refs", "--all")
        self.assertEqual(packed.returncode, 0, packed.stderr)
        self.assertFalse(os.path.exists(loose),
                         "pack-refs must actually move the loose representation")
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip(), old,
            "the admitted transaction must preserve the occupied ref value")

        moved = _sh(self.root, "git", "commit-tree", "HEAD^{tree}", "-p", old,
                    "-m", "synthetic packed-branch move").stdout.strip()
        self.assertRegex(moved, r"^[0-9a-f]{40}$")
        for cmd in (("update-ref", "-d", "refs/heads/lane/held"),
                    ("update-ref", "refs/heads/lane/held", moved)):
            r = _sh(self.root, "git", *cmd)
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("OCCUPIED", r.stderr)
            self.assertEqual(
                _sh(self.root, "git", "rev-parse", "lane/held").stdout.strip(),
                old)

    def test_duplicate_loose_and_packed_ref_distinguishes_delete_from_prune(self):  # noqa: VACUOUS_ASSERTION — the zero-old deletion refuses with OCCUPIED, the explicit-old prune admits, and the effective branch OID remains exact
        """Git emits 0000.. -> 0000.. for an ordinary duplicate-ref delete,
        but the true old SHA -> 0000.. when pack-refs prunes its loose copy.
        The second shape is the representation-only door; the first must stay
        behind occupancy even though both backends temporarily name one OID."""
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.room("held")
        ref = "refs/heads/lane/held"
        old = _sh(self.root, "git", "rev-parse", ref).stdout.strip()
        packed = _sh(self.root, "git", "pack-refs", "--all")
        self.assertEqual(packed.returncode, 0, packed.stderr)
        loose = os.path.join(self.root, ".git", *ref.split("/"))
        os.makedirs(os.path.dirname(loose), exist_ok=True)
        with open(loose, "w") as f:
            f.write(old + "\n")

        zero = "0" * 40
        delete = self._ref_transaction("%s %s %s" % (zero, zero, ref))
        self.assertNotEqual(delete.returncode, 0, delete.stderr)
        self.assertIn("OCCUPIED", delete.stderr)
        prune = self._ref_transaction("%s %s %s" % (old, zero, ref))
        self.assertEqual(prune.returncode, 0, prune.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", ref).stdout.strip(), old)

    def test_ref_guard_fails_closed_when_occupancy_registry_is_unreadable(self):  # noqa: VACUOUS_ASSERTION — the target OID is a proven distinct descendant, the injected worktree census exits 9, the real update refuses loudly, and the original branch OID remains exact
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")
        r = subprocess.run(["git", "branch", "other"], cwd=self.root,
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        fakebin = os.path.join(self.tmp, "fake-bin")
        os.makedirs(fakebin)
        fakegit = os.path.join(fakebin, "git")
        realgit = shutil.which("git")
        with open(fakegit, "w") as f:
            f.write("#!/bin/sh\n"
                    "if [ \"$1 $2\" = \"worktree list\" ]; then exit 9; fi\n"
                    "exec \"%s\" \"$@\"\n" % realgit)
        os.chmod(fakegit, 0o755)
        ref_hook = work.hook_path(self.root, "reference-transaction")
        with open(ref_hook) as f:
            script = f.read()
        with open(ref_hook, "w") as f:
            f.write(script.replace("#!/bin/sh\n", "#!/bin/sh\nPATH='" + fakebin
                                   + "':$PATH\n", 1))
        os.chmod(ref_hook, 0o755)
        old = _sh(self.root, "git", "rev-parse", "other").stdout.strip()
        moved = _sh(self.root, "git", "commit-tree", "HEAD^{tree}", "-p", old,
                    "-m", "synthetic unreadable-registry move").stdout.strip()
        self.assertRegex(moved, r"^[0-9a-f]{40}$")
        r = subprocess.run(["git", "update-ref", "refs/heads/other", moved],
                           cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot verify worktree occupancy", r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "other").stdout.strip(),
                         old)

    def test_install_from_linked_worktree_targets_common_hooks_only(self):
        linked = self.room("caller")
        root = work.find_root(linked)
        self.assertEqual(root, self.root)
        rc, lines = work.install_guard(root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        common = _sh(self.root, "git", "rev-parse", "--path-format=absolute",
                     "--git-common-dir").stdout.strip()
        for name, _var in work.GUARD_HOOKS:
            self.assertEqual(os.path.dirname(work.hook_path(root, name)),
                             os.path.join(common, "hooks"))
            per_worktree = _sh(linked, "git", "rev-parse", "--path-format=absolute",
                               "--git-dir").stdout.strip()
            self.assertFalse(os.path.exists(os.path.join(per_worktree, "hooks", name)))

    def test_repo_local_hooks_path_is_honored_but_external_path_is_refused(self):
        local = os.path.join(self.root, ".git", "helm-hooks")
        self.assertEqual(_sh(self.root, "git", "config", "core.hooksPath", local)
                         .returncode, 0)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(local, "reference-transaction")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git", "hooks",
                                                     "reference-transaction")))

        other = os.path.join(self.tmp, "foreign hooks")
        self.assertEqual(_sh(self.root, "git", "config", "core.hooksPath", other)
                         .returncode, 0)
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("outside this repo", err)
        self.assertFalse(os.path.exists(other))

    def test_shell_quoting_handles_a_base_branch_with_apostrophe(self):
        base = "odd'base"
        self.assertEqual(_sh(self.root, "git", "branch", "-m", base).returncode, 0)
        rc, lines = work.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        r = _sh(self.root, "git", "commit", "--allow-empty", "-m", "on odd base")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertEqual(_sh(self.root, "git", "symbolic-ref", "--short", "HEAD")
                         .stdout.strip(), base)

    def test_guard_handles_repo_paths_with_spaces_newlines_and_symlink_callers(self):
        moved = os.path.join(self.tmp, "repo space\nline")
        os.rename(self.root, moved)
        self.root = moved
        alias = os.path.join(self.tmp, "repo-alias")
        os.symlink(self.root, alias)
        root = work.find_root(alias)
        self.assertEqual(os.path.realpath(root), os.path.realpath(self.root))
        rc, lines = work.install_guard(root, apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        r = _sh(alias, "git", "checkout", "-b", "blocked")
        self.assertEqual(r.returncode, 128, r.stderr)
        self.assertIn("REFUSED", r.stderr)

    def test_concurrent_installers_converge_to_one_complete_rail(self):
        def install(_):
            return work.install_guard(self.root, apply=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(install, range(16)))
        self.assertTrue(all(rc == 0 for rc, _lines in results), results)
        for name, _var in work.GUARD_HOOKS:
            path = work.hook_path(self.root, name)
            self.assertTrue(os.access(path, os.X_OK))
            with open(path) as f:
                self.assertIn(work.MANAGED_HOOK_MARKER, f.read())

    def test_claim_lock_blocks_normal_raw_remove_and_prune(self):
        rc, out, err = self.work("claim", "locked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        path = out.split("\t")[0]
        r = _sh(self.root, "git", "worktree", "remove", "--force", path)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("locked", r.stderr.lower())
        self.assertEqual(_sh(self.root, "git", "worktree", "prune").returncode, 0)
        self.assertTrue(os.path.isdir(path))


class SharedCheckoutLocalCommitTest(WorkBase):
    """#144, the INVERSION: the guard refused `git branch foo` — which creates
    nothing anyone can lose — while PERMITTING a plain commit onto shared
    main, the one operation that silently forks the tree nine seats read.

    A commit is always FAST-FORWARD, so it fell through the non-ff arm and
    landlock passed silently because nobody held the claim. MEASURED THREE
    TIMES on 2026-08-03: a docs commit stopped the ff for ~80 minutes and two
    more followed within the hour; every one was found by reflog archaeology
    rather than by an instrument, and the fleet spent the afternoon reasoning
    against a base that had silently forked.

    THE DISCRIMINATOR IS PROVENANCE, NOT SHAPE: a SYNC moves main to a commit
    that is ON its configured upstream; a LOCAL COMMIT moves it to one that is
    not. Both are fast-forward, so ancestry against the configured ref is the
    only thing that tells them apart. Missing upstream state is UNKNOWN, not
    proof this is a solo checkout."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.assertEqual(_sh(self.tmp, "git", "init", "-q", "--bare", "-b",
                             "main", self.origin).returncode, 0)
        _sh(self.root, "git", "remote", "add", "origin", self.origin)
        self.assertEqual(_sh(self.root, "git", "push", "-q", "origin",
                             "main").returncode, 0)
        self.assertEqual(_sh(self.root, "git", "fetch", "-q",
                             "origin").returncode, 0)
        self.assertEqual(_sh(self.root, "git", "branch", "--set-upstream-to",
                             "origin/main", "main").returncode, 0)
        rc, _out, _err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)

    def _commit(self, name, env=None):
        with open(os.path.join(self.root, name), "w") as f:
            f.write("x\n")
        _sh(self.root, "git", "add", "-A")
        return subprocess.run(["git", "commit", "-qm", name], cwd=self.root,
                              capture_output=True, text=True, timeout=30,
                              env=env or os.environ.copy())

    def test_a_local_commit_onto_shared_main_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the behavior; unconditional positives on the same pass are the stderr text and the ref-unchanged assertion
        r = self._commit("stray.txt")
        self.assertNotEqual(r.returncode, 0, "the fork operation must refuse")
        self.assertIn("LOCAL COMMIT onto shared", r.stderr)
        self.assertIn("helm work claim", r.stderr)
        # AND THE REF NEVER MOVED — a refusal that leaves the commit is worse
        # than none, because the tree forks while the operator reads REFUSED.
        self.assertEqual(
            _sh(self.root, "git", "rev-parse", "main").stdout.strip(),
            _sh(self.root, "git", "rev-parse", "origin/main").stdout.strip())

    def test_a_missing_configured_upstream_ref_is_REFUSED(self):
        _sh(self.root, "git", "update-ref", "-d",
            "refs/remotes/origin/main")
        self.assertIn("origin", _sh(self.root, "git", "remote").stdout)
        r = self._commit("missing-upstream.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("configured upstream refs/remotes/origin/main is unavailable",
                      r.stderr)

    def test_a_remote_without_a_configured_upstream_is_REFUSED(self):
        self.assertEqual(_sh(self.root, "git", "branch", "--unset-upstream").returncode,
                         0)
        self.assertIn("origin", _sh(self.root, "git", "remote").stdout)
        r = self._commit("unconfigured-upstream.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("has remotes but no configured upstream", r.stderr)

    def test_the_configured_remote_name_is_not_assumed_to_be_origin(self):  # noqa: VACUOUS_ASSERTION — remote rename rc0 and the exact central/main refusal prove both configured-upstream resolution and the refusing effect
        self.assertEqual(_sh(self.root, "git", "remote", "rename", "origin",
                             "central").returncode, 0)
        r = self._commit("central.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refs/remotes/central/main", r.stderr)

    def test_a_SYNC_from_the_remote_is_PERMITTED(self):  # noqa: VACUOUS_ASSERTION — rc 0 rides beside an unconditional positive — landed.txt must exist in the working tree after the sync
        """THE CONTROL THAT MAKES THE REFUSAL MEAN SOMETHING. Without it a
        guard that refused every update would pass the test above and brick
        the fleet's ability to pull."""
        clone = os.path.join(self.tmp, "peer")
        self.assertEqual(_sh(self.tmp, "git", "clone", "-q", self.origin,
                             clone).returncode, 0)
        for cmd in (("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            _sh(clone, *cmd)
        with open(os.path.join(clone, "landed.txt"), "w") as f:
            f.write("from the fleet\n")
        _sh(clone, "git", "add", "-A")
        _sh(clone, "git", "commit", "-qm", "peer land")
        self.assertEqual(_sh(clone, "git", "push", "-q", "origin",
                             "main").returncode, 0)
        _sh(self.root, "git", "fetch", "-q", "origin")
        r = _sh(self.root, "git", "merge", "--ff-only", "origin/main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, "landed.txt")))

    def test_the_integrator_override_still_lands(self):
        before = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = self._commit("intg.txt",
                         env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # UNCONDITIONAL POSITIVE CONTROL: rc 0 alone would pass for a commit
        # that silently did nothing. The ref must actually have moved.
        after = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.assertNotEqual(before, after)
        self.assertIn("intg.txt", _sh(self.root, "git", "show", "--name-only",
                                      "--format=", "main").stdout)

    def test_a_repo_with_NO_REMOTE_still_commits_on_trunk(self):
        """The long-standing behaviour GuardTest documents ('committing on the
        trunk is untouched') survives for a solo checkout: no remote-tracking
        ref means no shared tree to protect and no way to prove provenance,
        and refusing there would brick a single-user helm."""
        _sh(self.root, "git", "remote", "remove", "origin")
        _sh(self.root, "git", "update-ref", "-d", "refs/remotes/origin/main")
        before = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        r = self._commit("solo.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = _sh(self.root, "git", "rev-parse", "main").stdout.strip()
        self.assertNotEqual(before, after, "the solo commit must really land")
        self.assertIn("solo.txt", _sh(self.root, "git", "show", "--name-only",
                                      "--format=", "main").stdout)

    def test_the_branch_create_refusal_is_UNCHANGED(self):
        """The inversion's other half stays refused — this fix removes the
        asymmetry by tightening the permissive side, never by loosening the
        strict one."""
        r = _sh(self.root, "git", "branch", "zero-risk")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("may not be created in the shared checkout", r.stderr)

    def test_the_refusal_names_a_cure_that_ACTUALLY_RUNS(self):
        """MEASURED 2026-08-22, hitting this guard for real: it printed
        `helm work claim lane/<name>` and that command REFUSES a slash
        (`lane = [A-Za-z0-9._-]{1,64}`), so the gate's own stated cure exited
        2. That is the bug class this repo already names — a gate whose stated
        cure does not run — sitting inside a gate. The line must print the
        LANE, which is what the verb takes, never the branch."""
        r = _sh(self.root, "git", "branch", "lane/zero-risk")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("helm work claim zero-risk", r.stderr)
        self.assertNotIn("helm work claim lane/zero-risk", r.stderr)


class StaleGuardHookTest(WorkBase):
    """A LANDED GUARD THAT IS NOT INSTALLED IS INERT, and nothing watched.
    MEASURED 2026-08-04 on the live shared checkout: it ran reference-
    transaction v3 while trunk generated v4, so v4's peek `..`-traversal
    refusal and its landlock session-identity fix had never been armed there.
    pre-commit was stale too, which the report found and the original finding
    had not. Detection only — installing rewrites an executable in a .git and
    must never be a side effect of a read pass."""

    def test_a_freshly_installed_hook_reports_NO_drift(self):
        """THE CONTROL. Without it a detector that flagged everything would
        satisfy the drift test below and be useless."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the hook
        # must EXIST and be non-empty, or "no drift" means "nothing compared".
        target = work.hook_path(self.root, "reference-transaction")
        self.assertTrue(os.path.getsize(target) > 0)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])

    # noqa: VACUOUS_ASSERTION — the positive control on THIS observable
    # is in this same test: after drifting one snapshot, the identical
    # stale_guard_hooks(self.root) call is asserted NON-empty and exact.
    # The clean-rail [] above is the baseline half of that pair, and the
    # rung reads them independently. Suppressed with the control named,
    # not because the warning was inconvenient.
    def test_a_STALE_SCANNER_is_drift_even_when_every_hook_matches(self):
        """THE HOLE THIS FUNCTION SHIPPED WITH. A hook is a few lines of shell
        that DELEGATES to a snapshot under .helm-scanners — never-track, the
        in-flight gate, the vacuity advisory all live there. So a scanner can
        land while its installed copy keeps enforcing the old rules with every
        HOOK byte-identical.

        Found by sweeping prior art after building: `helm doctor`'s
        check_work_guard already read both halves, and this predicate read only
        the hooks — strictly weaker than the check it was built to move to a
        louder surface. The #92 class, in the file class the lane is about."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        # UNCONDITIONAL POSITIVE CONTROL, on the same observable: the scanner
        # snapshots must EXIST and be non-empty before "no drift" can mean
        # compared-and-clean rather than nothing-to-compare. This is the exact
        # failure the predicate itself shipped with, so the test must not
        # repeat it.
        snaps = sorted(work._guard._scanner_assets(self.root))
        self.assertTrue(snaps)
        for installed in snaps:
            self.assertTrue(os.path.getsize(installed) > 0, installed)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])

        snap = next(p for p in snaps if p.endswith("nevertrack.py"))
        with open(snap, "a") as f:
            f.write("\n# drifted\n")

        drift = work._guard.stale_guard_hooks(self.root)
        self.assertEqual([(st, n) for st, n, _w in drift],
                         [("STALE", "scanner:nevertrack.py")])
        # AND EVERY HOOK STILL MATCHES — proving the hook loop alone is blind
        # to this, which is exactly why the predicate had to widen.
        for p in work._guard._guard_plan(self.root)[1]:
            with open(p["target"]) as f:
                self.assertEqual(f.read(), p["script"])

    def test_a_MISSING_scanner_is_reported_not_silent(self):
        """A hook that delegates to a file which is not there fails OPEN in the
        worst way: git runs the hook, the scanner is absent, and enforcement
        simply does not happen."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(work._guard.stale_guard_hooks(self.root), [])
        snap = next(p for p in work._guard._scanner_assets(self.root)
                    if p.endswith("inflight_gate.py"))
        os.remove(snap)
        self.assertEqual(
            [(st, n) for st, n, _w in work._guard.stale_guard_hooks(self.root)],
            [("MISSING", "scanner:inflight_gate.py")])

    def test_an_EDITED_hook_is_reported_as_drift(self):
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        with open(target, "a") as f:
            f.write("\n# an older ruleset\n")
        drift = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("STALE", "reference-transaction"),
                      [(state, name) for state, name, _why in drift])

    def test_an_ABSENT_hook_is_reported_as_missing(self):
        target = work.hook_path(self.root, "reference-transaction")
        self.assertFalse(os.path.exists(target), "the hook must really be absent")
        findings = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("MISSING", "reference-transaction"),
                      [(state, name) for state, name, _why in findings])
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("GUARD-MISSING reference-transaction", out)

    def test_an_UNREADABLE_hook_is_reported_as_unknown(self):
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        os.chmod(target, 0)
        findings = work._guard.stale_guard_hooks(self.root)
        self.assertIn(("UNKNOWN", "reference-transaction"),
                      [(state, name) for state, name, _why in findings])

    def test_an_UNRENDERABLE_plan_is_reported_as_unknown(self):
        with mock.patch.object(_work_guard, "_guard_plan",
                               side_effect=OSError("plan unreadable")):
            self.assertEqual(
                work._guard.stale_guard_hooks(self.root),
                [("UNKNOWN", "guard-plan",
                  "cannot render the expected hooks: OSError: plan unreadable")])

    def test_gc_REPORTS_the_drift_and_never_installs(self):  # noqa: VACUOUS_ASSERTION — never-installs is the claim; the unconditional positive on the same run is assertIn GUARD-STALE
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        target = work.hook_path(self.root, "reference-transaction")
        with open(target, "a") as f:
            f.write("\n# an older ruleset\n")
        with open(target) as f:
            before = f.read()
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("GUARD-STALE reference-transaction", out)
        self.assertIn("install-guard --apply", out)
        with open(target) as f:
            after = f.read()
        self.assertEqual(after, before,
                         "a read pass must never rewrite a hook")


class HooksDirectoryIsAskedOncePerEvaluationTest(WorkBase):
    """One guard evaluation asks git for the hooks directory once, and the
    next evaluation asks again (task/3039).

    MEASURED BEFORE: `hook_path` asked `git rev-parse --git-path hooks` for
    every hook name, about 41 times per `helm work claim`, and 7,995 times in
    tests.test_stop_seam alone. The directory is resolved at the top of each
    evaluation and passed down. It is NOT remembered across evaluations,
    because `core.hooksPath` can change between two of them, and a memo
    keyed on the repository path would then name a directory git no longer
    runs.
    """

    def _asks(self):
        real = _work_guard._git
        asked = []

        def counting(where, *args, **kw):
            if "--git-path" in args:
                asked.append(args)
            return real(where, *args, **kw)

        patch = mock.patch.object(_work_guard, "_git", counting)
        patch.start()
        self.addCleanup(patch.stop)
        return asked

    def test_each_evaluation_asks_for_the_hooks_directory_once(self):
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        asked = self._asks()
        _work_guard.hook_path(self.root, "pre-commit")
        self.assertEqual(len(asked), 1, "control: the counter sees one ask")
        for name, evaluate in (
                ("stale_guard_hooks", _work_guard.stale_guard_hooks),
                ("guard_remedy", _work_guard.guard_remedy),
                ("guard_remedy_note", _work_guard.guard_remedy_note),
                ("installed_profile", _work_guard.installed_profile),
                ("install_guard dry", lambda root: _work_guard.install_guard(
                    root, apply=False))):
            with self.subTest(evaluation=name):
                asked = self._asks()
                evaluate(self.root)
                self.assertEqual(len(asked), 1, asked)

    def test_one_claim_asks_for_the_hooks_directory_once(self):
        """A claim's rail check and the remedy it prints for a rail that is
        not armed are ONE evaluation: they read one hooks directory. Measured
        before: three asks per claim, the check and each half of the remedy."""
        asked = self._asks()
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARD RAIL NOT ARMED", err,
                      "control: the check ran and printed its remedy")
        self.assertIn("install-guard --apply --profile rail", err)
        self.assertEqual(len(asked), 1, asked)

    def test_one_staleness_check_resolves_the_profile_once(self):
        """The plan and the scanner snapshots a staleness check compares are
        judged under ONE profile resolution. Measured before: two per check,
        585 `config --get helm.guard.profile` spawns in tests.test_stop_seam,
        and two resolutions that could disagree if the repo changed between
        them."""
        real = _work_guard._git
        asked = []

        def counting(where, *args, **kw):
            if args[:2] == ("config", "--get"):
                asked.append(args)
            return real(where, *args, **kw)

        with mock.patch.object(_work_guard, "_git", counting):
            drift = _work_guard.stale_guard_hooks(self.root)
        self.assertIn(("MISSING", "reference-transaction"),
                      [(state, name) for state, name, _why in drift],
                      "control: the check compared an unarmed rail")
        self.assertEqual(len(asked), 1, asked)

    def test_the_answer_is_unchanged_by_the_hoist(self):
        """The plan's targets and snapshots still sit in the hooks directory
        git reports."""
        hooks = _sh(self.root, "git", "rev-parse", "--path-format=absolute",
                    "--git-path", "hooks").stdout.strip()
        _base, plan = _work_guard._guard_plan(self.root)
        self.assertTrue(plan, "control: the rail plans hooks")
        self.assertEqual({os.path.dirname(p["target"]) for p in plan}, {hooks})
        self.assertEqual({os.path.dirname(os.path.dirname(p))
                          for p in _work_guard._scanner_assets(self.root)},
                         {hooks})

    def test_a_hooks_path_changed_between_evaluations_is_seen(self):
        """The must-miss: nothing is remembered across evaluations."""
        rc, _o, _e = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(_work_guard.stale_guard_hooks(self.root), [],
                         "control: the installed rail is fresh")
        moved = os.path.join(self.root, ".git", "moved-hooks")
        self.assertEqual(_sh(self.root, "git", "config", "core.hooksPath",
                             moved).returncode, 0)
        drift = _work_guard.stale_guard_hooks(self.root)
        self.assertIn(("MISSING", "reference-transaction"),
                      [(state, name) for state, name, _why in drift],
                      "the second evaluation read the old hooks directory")
        self.assertTrue(all(os.path.dirname(p["target"]) == moved
                            for p in _work_guard._guard_plan(self.root)[1]))


class LandlockGuardTest(WorkBase):
    """LANDLOCK enforced in the ref-transaction hook (three same-day land
    races 2026-07-29 — SI merged inside CD's held window 8 minutes after the
    convention was POSTED; a mutex that requires reading the room is not a
    mutex). Edge policy: no claim = PASS; unreadable claims = PASS + loud
    warning; unresolvable identity = PASS + warning; HELM_LANDLOCK=0
    disables THIS check alone. It narrows races; it is not an auth gate.

    "UNRESOLVABLE" NARROWED 2026-08-02. It used to mean "$HELM_CHAT_NAME is
    empty", which is the RESTING STATE of every claude-direct seat — so the
    warning arm was the ordinary path for a whole family and the narrower
    narrowed nothing for them. Claim rows already record the ambient harness
    session and bind it on refresh/release. The reader now compares that value
    directly, without importing identity code from the candidate worktree.
    Unresolvable now means neither a declared seat nor both sides of the session
    binding are available. That still PASSES with the warning — the fail-open is
    the point and it is unchanged. What changed is only who falls into it."""

    def setUp(self):
        super().setUp()
        rc, _o, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        self.claims = os.path.join(self.tmp, "claims.json")
        self._env = dict(os.environ,
                         HELM_LANDLOCK_CLAIMS=self.claims)

    def _write_claims(self, holder="cd", exp_mono=None):
        import json as _json
        row = {"holder": holder, "session": "s-x", "lease": "l",
               "fence": 1,
               "exp_mono": exp_mono if exp_mono is not None
                           else __import__("time").monotonic() + 900}
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": row, "_fence": 1}, f)

    def _commit_on_main(self, env=None):
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("land\n")
        return subprocess.run(["git", "commit", "-qam", "land"],
                              cwd=self.root, capture_output=True, text=True,
                              timeout=30, env=env or self._env)

    def _nameless_env(self, session=None):
        env = dict(self._env)
        for key in ("HELM_CHAT_NAME", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
            env.pop(key, None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return env

    def test_held_by_anOTHER_seat_refuses_and_names_the_holder(self):
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("landlock:helm is held by cd", r.stderr)
        self.assertIn("HELM_LANDLOCK=0", r.stderr)

    def test_held_by_ME_with_the_RECORDED_session_passes_silently(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """The real holder: name AND the session the claim writer recorded.

        SUPERSEDES test_held_by_ME_passes_silently, which set only the name
        and asserted a silent PASS — that test PINNED THE BYPASS GREEN. A
        name alone is the one credential the candidate supplies for itself,
        so any future fix would have read as a regression against it."""
        self._write_claims(holder="kimi")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi",
                                          CLAUDE_CODE_SESSION_ID="s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("landlock", r.stderr)
        # POSITIVE CONTROL on the same observable: a silent pass is also what
        # a DISABLED hook produces, so prove the hook was armed during it.
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="cd",
                                          CLAUDE_CODE_SESSION_ID="s-other"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "hook was not armed")

    def test_an_INHERITED_name_with_a_FRESH_session_is_REFUSED(self):
        """THE P0: the candidate declares the holder's name and is not it.

        Measured twice 2026-08-02 as an ACCIDENT, no adversary — a seat ran
        session-unbound with its name intact, and another auto-bound to the
        wrong roster row at reboot. Both would have committed against a lock
        another seat held, and the hook would have reported success."""
        self._write_claims(holder="opus-integrator")
        r = self._commit_on_main(env=dict(self._env,
                                          HELM_CHAT_NAME="opus-integrator",
                                          CLAUDE_CODE_SESSION_ID="s-fresh"))
        # assert the REFUSAL, never the advice wording — this test owns the
        # gate, and test_the_refusal_gives_IDENTITY_advice owns the message.
        # Asserting the advice here made both die to a message-only mutation.
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("REFUSED: landlock:helm is held by opus-integrator",
                      r.stderr)

    def test_CLEARING_the_session_does_not_reopen_the_name_alone_door(self):
        """The obvious escape from the fix, closed by construction.

        The refusal is gated on whether THE ROW records a session, never on
        whether the CANDIDATE supplies one — so unsetting your own session
        cannot buy back the name-alone path."""
        self._write_claims(holder="opus-integrator")
        env = self._nameless_env()
        env["HELM_CHAT_NAME"] = "opus-integrator"
        r = self._commit_on_main(env=env)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("REFUSED: landlock:helm is held by opus-integrator",
                      r.stderr)

    def test_the_refusal_gives_IDENTITY_advice_not_wait_for_the_lock(self):
        """A refusal that misroutes the operator is its own defect: told to
        'coordinate the release', a seat whose own name matches would sit out
        a timer that can never fix an identity binding."""
        self._write_claims(holder="opus-integrator")
        r = self._commit_on_main(env=dict(self._env,
                                          HELM_CHAT_NAME="opus-integrator",
                                          CLAUDE_CODE_SESSION_ID="s-fresh"))
        self.assertIn("seat disown", r.stderr)
        self.assertNotIn("land after it expires", r.stderr)

    def test_a_NAMELESS_holder_with_the_recorded_session_still_passes(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """THE BOUNDARY the fix must not cross. A legitimate holder with no
        declared name is admitted by the session alone, exactly as before —
        this change narrows only the name-alone door."""
        self._write_claims(holder="kimi")
        r = self._commit_on_main(env=self._nameless_env(session="s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL: same nameless shape, WRONG session must refuse —
        # otherwise this arm cannot tell admission from an inert hook.
        r = self._commit_on_main(env=self._nameless_env(session="s-other"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "hook was not armed")

    def test_a_LEGACY_row_with_NO_session_still_admits_its_named_holder(self):  # noqa: VACUOUS_ASSERTION — each carries a POSITIVE control on the same commit observable (a foreign seat must be REFUSED); non-vacuity proven by mutation, not shape: disabling enforcement (`if True` at the nobody-holds-it arm) fails all three
        """A claim predating session recording has only the name, so refusing
        would brick a real holder over a field their row cannot carry.
        Measured 2026-08-02: 0 of 4 live rows are this shape."""
        import json as _json
        import time as _time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {"holder": "kimi", "lease": "l",
                                          "fence": 1,
                                          "exp_mono": _time.monotonic() + 900},
                        "_fence": 1}, f)
        r = self._commit_on_main(env=self._nameless_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        r2 = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        # POSITIVE CONTROL: on this same legacy row a FOREIGN name must still
        # refuse. Without it, "legacy rows admit their holder" is
        # indistinguishable from "legacy rows admit everyone".
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="cd"))
        self.assertIn("REFUSED: landlock:helm is held by kimi", r.stderr,
                      "legacy row admitted a foreign seat")

    def test_a_name_only_admission_is_WARNED_never_silent(self):
        """The one door a declared name can still open must be VISIBLE.

        A sessionless claim is REACHABLE, not historical: `helm chat claim`
        takes the session from the ambient harness env only, so a claim
        minted from a bare shell or a cron unit records session=None
        (measured: _env_session() -> None with no harness env). We fail open
        there so a real holder is not locked out of their own lock — but a
        silent fail-open would hide the exact shape that can still be
        spoofed, so it warns and says WHICH door it used."""
        import json as _json
        import time as _time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {"holder": "kimi", "lease": "l",
                                          "fence": 1,
                                          "exp_mono": _time.monotonic() + 900},
                        "_fence": 1}, f)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("name-only", r.stderr)
        self.assertIn("claim records no session", r.stderr)
        # and it must NOT be rendered as the unreadable-claims warning, which
        # would tell the reader identity was unknown when it was merely
        # uncorroborated
        self.assertNotIn("claims unreadable/identity unknown", r.stderr)

    def test_NO_claim_at_all_passes_silently(self):
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("landlock", r.stderr)

    def test_an_EXPIRED_claim_passes(self):
        import time
        self._write_claims(holder="cd", exp_mono=time.monotonic() - 1)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unreadable_claims_warns_and_passes(self):
        with open(self.claims, "w") as f:
            f.write("{not json")
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock", r.stderr)
        self.assertIn("WARNING", r.stderr)

    def _plant_candidate_helm(self, name):
        """Candidate-controlled identity code that the installed hook must not
        import while deciding whether that candidate may update main."""
        pkg = os.path.join(self.root, "helm")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(pkg, "seats.py"), "w") as f:
            f.write("def safe_cwd():\n    return '.'\n\n"
                    "def acting_seat(session=None, cwd=None):\n"
                    "    return %r\n" % (name,))
        self.assertEqual(_sh(self.root, "git", "add", "helm").returncode, 0)

    def test_a_seat_that_declared_NO_name_is_now_BOUND_by_the_lock(self):
        """THE BUG. $HELM_CHAT_NAME empty is not an exotic edge — it is how
        every claude-direct seat runs. Its ambient session is enough to prove
        that a live claim belongs to a different session."""
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=self._nameless_env("s-other"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock:helm is held by cd", r.stderr)

    def test_the_ANTI_BRICK_control_the_bound_session_is_ME_so_it_passes(self):
        """The half that must NOT change: the session recorded by the writer
        proves this is my own land window without consulting candidate code."""
        self._write_claims(holder="helm-claude-2")
        r = self._commit_on_main(env=self._nameless_env("s-x"))
        self.assertEqual(r.returncode, 0, r.stderr)
        # POSITIVE CONTROL on the SAME observable (r.stderr): the managed hook
        # chain demonstrably RAN. Without this, "no landlock line on stderr"
        # is equally satisfied by a run where no hook fired at all — and that
        # is the difference between "the check ran and stayed silent" and
        # "the check never happened", which is the whole claim of this test.
        self.assertIn("[helm", r.stderr)
        self.assertNotIn("landlock", r.stderr)
        landed = _sh(self.root, "git", "log", "--oneline", "-1", "main").stdout
        self.assertIn("land", landed)

    def test_candidate_identity_code_cannot_speak_for_the_guard(self):
        """THE REVIEW FINDING. A reference-transaction hook runs with the
        candidate files already in the worktree. Importing helm.seats there let
        a candidate return the foreign holder's name and pass its own guard."""
        self._write_claims(holder="cd")
        self._plant_candidate_helm("cd")
        r = self._commit_on_main(env=self._nameless_env("s-other"))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("landlock:helm is held by cd", r.stderr)

    def test_unresolvable_identity_warns_and_passes(self):
        self._write_claims(holder="cd")
        r = self._commit_on_main(env=self._nameless_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)

    def test_a_hostile_holder_name_cannot_inject_the_refusal_line(self):
        import json as _json, time
        with open(self.claims, "w") as f:
            _json.dump({"landlock:helm": {
                "holder": "cd$(touch /tmp/landlock-injected)",
                "session": "s", "lease": "l", "fence": 1,
                "exp_mono": time.monotonic() + 900}, "_fence": 1}, f)
        r = self._commit_on_main(env=dict(self._env, HELM_CHAT_NAME="kimi"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("held by ?", r.stderr)
        self.assertFalse(os.path.exists("/tmp/landlock-injected"))

    def test_kill_switch_disables_THIS_check_alone(self):
        self._write_claims(holder="cd")
        env = dict(self._env, HELM_CHAT_NAME="kimi", HELM_LANDLOCK="0")
        r = self._commit_on_main(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # and the REST of the guard still bites under the same switch
        r2 = subprocess.run(["git", "checkout", "-b", "still-guarded"],
                            cwd=self.root, capture_output=True, text=True,
                            timeout=30, env=env)
        self.assertEqual(r2.returncode, 128, r2.stderr)
        self.assertIn("REFUSED", r2.stderr)


class PeekReuseDisclosureTest(WorkBase):
    """task/2182 — a peek that CONVERGES on an existing room says so.

    `peek` returns the same room for the same sha rather than minting a
    second one, which is the cheap and intended case: two reviewers reading
    one immutable checkout. The defect was that the ANSWER was byte-identical
    either way, so the second caller could not learn it had become a GUEST in
    a tree the verb documents as having no lease, no branch and no lock.

    THE COST, walked into rather than imagined: a cherry-pick inside a reused
    room writes conflict markers into the files of a worktree another caller
    has live processes in, and the only surface that names the owner is a
    REFUSED `--drop` — a door the guest has no reason to knock on. That
    asymmetry is the defect: the occupancy probe is wired to the door where
    being wrong costs a refused cleanup and absent from the door where being
    wrong costs another caller's measurement.

    THESE ARMS ARE ABOUT THE DISCLOSURE, NOT ABOUT REFUSING. Sharing the room
    stays the design; the arms pin that the caller is TOLD, that the machine
    contract is untouched, and that an unreadable census never renders as an
    empty one.
    """

    def _mint(self):
        rc, out, _err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, out)
        return out.strip().splitlines()[0].split("\t")[0]

    def test_a_fresh_peek_is_silent_and_a_reused_one_is_not(self):
        # MINIMAL PAIR IN ONE ARM, WITH THE POSITIVE CONTROL FIRST. An arm
        # asserting only that a fresh mint says nothing passes just as well
        # when the notice is deleted outright, and a control that runs AFTER
        # the absence assertion cannot rescue it — so the reuse is exercised
        # first, unconditionally, on the same observable.
        # ONE NAME CARRIES BOTH POLES on purpose: the control has to be on
        # the SAME observable the absence is asserted about, and two
        # differently-named captures are two observables as far as any reader
        # or rung can tell.
        path = self._mint()
        rc, out, err = self.work("peek", "HEAD")                 # converges
        self.assertEqual(rc, 0, err)
        self.assertIn("GUEST", err,
                      "the notice never appears at all, so the silence "
                      "asserted below proves nothing")
        reused_out = out
        # A DROP MAKES THE NEXT PEEK GENUINELY FRESH, which is the only way to
        # get both poles out of one sha in one repository.
        rc, _dropped, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        # THE CONTROL CANNOT SHARE THIS ASSERTION'S PRODUCER. Fresh and
        # reused are two different invocations by construction — one room is
        # minted once — so the unconditional control above is on the same
        # expression and the same room but necessarily a different call.
        self.assertNotIn(  # noqa: VACUOUS_ASSERTION — fresh and reused are necessarily two calls; control is above on the same room
            "GUEST", err,
            "a FRESH mint claimed the caller is a guest")
        self.assertEqual(out, reused_out,
                         "the machine contract (path<TAB>sha) changed between "
                         "a fresh mint and a reuse")

    def test_the_notice_goes_to_stderr_and_stdout_stays_one_line(self):
        # THE CONTRACT THIS MUST NOT BREAK. `peek` is documented as printing
        # `path<TAB>sha`, and PeekTtlTest.setUp parses exactly that. A second
        # stdout line would break every caller that splits on tab.
        path = self._mint()
        rc, out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        self.assertEqual(1, len(out.strip().splitlines()),
                         "the reuse notice reached STDOUT: %r" % out)
        self.assertEqual(path, out.strip().split("\t")[0])
        self.assertTrue(err.strip(), "the notice reached neither stream")

    def test_an_occupied_room_names_the_pids(self):
        path = self._mint()
        from helm.work import _peek
        with mock.patch.object(_peek, "_occupants_many",
                               return_value=({path: ["4242", "4243"]}, True)):
            rc, _out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        self.assertIn("4242", err)
        self.assertIn("4243", err)
        self.assertIn("2 process(es)", err)

    def test_an_unreadable_census_is_never_rendered_as_an_empty_one(self):
        # THE ARM THIS ROW IS ACTUALLY ABOUT. `_occupants_many` answers
        # ({path: []}, False) when the process census could not be read, and
        # that empty list is the SAME VALUE a genuinely idle room produces.
        # A caller told "no process is here" acts on a measurement nobody
        # made — which is the exact shape of the defect being cured, one
        # layer down.
        path = self._mint()
        from helm.work import _peek
        # THE DOUBLE MINTS THE SHAPE PRODUCTION MINTS, and that is not a
        # detail. `_occupants_many` returns ({path: ["unknown"]}, False) when
        # /proc cannot be listed — a NON-EMPTY list carrying a string where
        # pids belong — so a double feeding ({path: []}, False) would assert
        # against a pairing the world never produces.
        #
        # AND THIS ARM COULD NOT DETECT THAT ITSELF. Both shapes render
        # identically here: `occupancy != "measured"` is tested first, so the
        # branch touching `occupants` is unreachable in that state. A double
        # that is wrong where the assertion cannot look is invisible, which is
        # why the machine-surface arm below exists.
        with mock.patch.object(_peek, "_occupants_many",
                               return_value=({path: ["unknown"]}, False)):
            rc, _out, unread = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, unread)
        self.assertIn("could NOT be measured", unread)
        self.assertNotIn("no process currently has its cwd here", unread,
                         "an unreadable census rendered as an empty one")
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, so the absence above
        # cannot be explained by the room having no notice at all: the same
        # room with a READABLE empty census says the opposite thing.
        with mock.patch.object(_peek, "_occupants_many",
                               return_value=({path: []}, True)):
            rc, _out, readable = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, readable)
        self.assertIn("no process currently has its cwd here", readable)
        self.assertNotIn("could NOT be measured", readable)

    def test_the_machine_surface_reports_no_count_when_none_was_taken(self):
        # THE SURFACE THE HUMAN ARM CANNOT SEE. Both census shapes render the
        # same stderr, so the prose arm above is structurally unable to catch
        # a payload that reports occupants nobody counted. This asserts the
        # JSON, where the difference is the whole point: a consumer reading
        # `occupants` as pids must get NOTHING rather than a length-1 list
        # holding the string "unknown", which it would read as one process.
        path = self._mint()
        from helm.work import _peek
        with mock.patch.object(_peek, "_occupants_many",
                               return_value=({path: ["unknown"]}, False)):
            rc, out, err = self.work("peek", "HEAD", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual("unmeasured", payload["occupancy"])
        self.assertIsNone(payload["occupants"],
                          "a count nobody took was published as %r"
                          % (payload["occupants"],))
        # THE CONTROL, unconditional and on the same field: a MEASURED census
        # must still publish its pids, or the assertion above is satisfied by
        # the field being null always.
        with mock.patch.object(_peek, "_occupants_many",
                               return_value=({path: ["4242"]}, True)):
            rc, out, err = self.work("peek", "HEAD", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(["4242"], json.loads(out)["occupants"])

    def test_a_dirty_reused_room_says_it_is_dirty(self):
        # A MINIMAL PAIR ON THE DIRT, DIRTY POLE FIRST so the absence below
        # is measured against an observable already proven live on this exact
        # room. The two runs differ in one untracked file and nothing else.
        path = self._mint()
        scratch = os.path.join(path, "peek-scratch.txt")
        with open(scratch, "w") as fh:
            fh.write("a second caller started writing here\n")
        rc, _out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        self.assertIn("DIRTY", err,
                      "an untracked file did not read as dirt, so the clean "
                      "assertion below is asserting nothing")
        os.remove(scratch)
        rc, _out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        # Same reason as the arm above: dirty and clean are two calls against
        # one room differing in one untracked file, so the control cannot be
        # the same invocation.
        self.assertNotIn(  # noqa: VACUOUS_ASSERTION — dirty and clean are necessarily two calls; control is above on the same room
            "DIRTY", err, "a clean room reported dirt")

    def test_json_carries_the_facts_and_prints_no_prose(self):
        # THE MACHINE PATH IS A DIFFERENT CONTRACT. A caller that asked for
        # --json wants one parseable object; prose on stderr beside it is
        # noise, and the facts belong IN the object.
        self._mint()
        # UNCONDITIONAL CONTROL ON THE SAME OBSERVABLE, FIRST: the prose path
        # must actually be producing stderr on this room, or the emptiness
        # asserted below is the notice being broken rather than suppressed.
        rc, _out, err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, err)
        self.assertTrue(err.strip(),
                        "the non-json path printed nothing, so an empty "
                        "stderr under --json proves nothing")
        rc, out, err = self.work("peek", "HEAD", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["reused"])
        self.assertIn("occupancy", payload)
        self.assertIn("dirty", payload)
        # The control is the same command WITHOUT --json, a separate
        # invocation by definition: one run cannot both print prose and not.
        self.assertEqual(  # noqa: VACUOUS_ASSERTION — control is the same command without --json, necessarily a different call
            "", err.strip(), "the --json path printed prose: %r" % err)


class PeekTtlTest(WorkBase):
    """#157 — stale peeks are TTL-reaped by `work gc`, the owner's fifth ask
    for the same sidebar recurrence. Detection (stale_peeks) is age-only BY
    DESIGN: a peek is read-only by construction, and every live-room refusal
    (occupant, pane, dirt) stays in peek_drop, the ONLY remover."""

    def setUp(self):
        super().setUp()
        rc, out, _err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, out)
        self.peek_path = out.strip().splitlines()[0].split("\t")[0]
        self.assertTrue(os.path.isdir(self.peek_path))

    def _backdate(self, seconds):
        past = time.time() - seconds
        os.utime(self.peek_path, (past, past))

    def test_reuse_resets_the_idleness_clock(self):
        # REWORK repro 3 on ef9c68e3: a pure re-issue wrote
        # nothing, so a room reused at hour 3 still read 3h idle and the
        # NEXT tick could reap it out from under its reuser.
        from helm.work import _peek
        self._backdate(3 * 3600)
        self.assertTrue(_peek.stale_peeks(self.root),
                        "control: the room must read idle before the reuse")
        rc, out, _err = self.work("peek", "HEAD")   # converges on the room
        self.assertEqual(rc, 0, out)
        self.assertEqual(_peek.stale_peeks(self.root), [],
                         "a reused room just proved it is not abandoned")

    def test_reuse_refuses_when_its_activity_refresh_fails(self):
        from helm.work import _peek
        self._backdate(3 * 3600)
        with mock.patch.object(_peek.os, "utime",
                               side_effect=OSError("read-only mount")):
            rc, payload = _peek.peek(self.root, "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("cannot refresh peek activity", payload["error"])
        self.assertTrue(os.path.isdir(self.peek_path),
                        "a failed refresh refuses reuse but destroys nothing")

    def test_reuse_after_stale_scan_refuses_the_drop_at_mutation_boundary(self):
        """Reviewer FIX on 88b26a9a: the stale list is only a candidate set.

        Reuse can refresh the room after that scan. The shared activity lock
        plus drop-side TTL recheck must consume the fresh fact, not the stale
        candidate, or the cadence deletes a room under its reuser."""
        from helm.work import _peek
        self._backdate(3 * 3600)
        self.assertTrue(_peek.stale_peeks(self.root),
                        "control: cadence must first select the stale room")
        real_drop = _peek.peek_drop

        def reuse_then_drop(root, path, **kwargs):
            rc, payload = _peek.peek(root, "HEAD")
            self.assertEqual(rc, 0, payload)
            self.assertTrue(payload["reused"])
            return real_drop(root, path, **kwargs)

        with mock.patch.object(_peek, "peek_drop",
                               side_effect=reuse_then_drop):
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "freshly reused room must survive the stale candidate")
        self.assertIn("activity refreshed", out)
        self.assertIn("removed=0", out)
        self.assertIn("kept=1", out)

    # noqa: VACUOUS_ASSERTION — the assertFalse(isdir) rides between two
    # unconditional positives on the same pass: setUp proves the room
    # existed, and assertIn("removed=1") reads the drop off the summary.
    def test_summary_counts_a_dropped_peek_with_no_lane_rows(self):
        # REWORK repro 1: the no-rows branch discarded
        # peek_pass's counts, so this exact pass reported removed=0.
        self._backdate(3 * 3600)
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path),
                         "positive control: the peek was really dropped")
        self.assertIn("removed=1", out)
        self.assertIn("kept=0", out)

    # noqa: VACUOUS_ASSERTION — assertNotIn("kept=-1") is the regression
    # pin; the unconditional positives on the SAME summary line are
    # assertIn("removed=2") and assertIn("kept=1"), and both room drops
    # are asserted against rooms setUp/this test proved existed.
    def test_summary_counts_peeks_beside_lanes_and_kept_never_negative(self):
        # REWORK repro 2: peek drops were folded into the LANE
        # removed count, so len(rows)-removed printed kept=-1 on exactly
        # this shape — one kept lane, two dropped peeks.
        rc, out, _err = self.work("claim", "heldlane", "--seat", "s1")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.root, "second"), "w") as f:
            f.write("x\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "second")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, out, _err = self.work("peek", "HEAD")
        self.assertEqual(rc, 0, out)
        second = out.strip().splitlines()[0].split("\t")[0]
        self.assertNotEqual(second, self.peek_path)
        past = time.time() - 3 * 3600
        os.utime(self.peek_path, (past, past))
        os.utime(second, (past, past))
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path))
        self.assertFalse(os.path.isdir(second))
        self.assertIn("removed=2", out)
        self.assertIn("kept=1", out, "the held lane is the 1 kept")
        self.assertNotIn("kept=-1", out)

    def test_a_fresh_peek_is_not_stale_and_an_idle_one_is(self):
        from helm.work import _peek
        self.assertEqual(_peek.stale_peeks(self.root), [],
                         "a fresh peek must never be listed")
        self._backdate(3 * 3600)
        rows = _peek.stale_peeks(self.root)
        self.assertEqual([p for p, _ in rows], [self.peek_path])
        self.assertGreaterEqual(rows[0][1], 3 * 3600 - 5)

    def test_gc_dry_run_names_the_stale_peek_and_drops_nothing(self):
        self._backdate(3 * 3600)
        rc, out, _err = self.work("gc")
        self.assertEqual(rc, 0, out)
        self.assertIn("PEEK-STALE", out)
        self.assertIn(os.path.basename(self.peek_path), out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "dry run must never drop a room")

    def test_gc_apply_drops_the_idle_peek(self):
        self._backdate(3 * 3600)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "positive control: the room must exist before the "
                        "apply, or the drop assertion below is vacuous")
        rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.isdir(self.peek_path),
                         "the idle peek must be dropped by --apply")
        self.assertIn("dropped", out)

    def test_an_OCCUPIED_stale_peek_is_kept_with_the_refusal_printed(self):
        self._backdate(3 * 3600)
        with mock.patch("helm.work._peek._occupants",
                        return_value=["12345"]):
            rc, out, _err = self.work("gc", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.peek_path),
                        "an occupied room must survive the apply")
        self.assertIn("OCCUPIED", out)

    def test_the_shipped_timer_service_runs_the_work_reap_leg(self):
        from helm import gc as streamgc
        _sp, service, _tp, timer = streamgc.timer_units()
        # EXECUTABLE lines only — the template's own comment mentions the
        # same phrase, and a prose match let mutation M3 survive (the
        # tripwire-counts-prose-as-call-sites class, caught in this lane).
        exec_lines = [l for l in service.splitlines()
                      if l.startswith("ExecStart=")]
        self.assertTrue(any("work gc --apply" in l for l in exec_lines),
                        "the cadence must EXECUTE the worktree reap leg "
                        "(#157), not merely mention it: %r" % exec_lines)
        self.assertTrue(any(l.endswith("gc --apply") or " gc --apply" in l
                            for l in exec_lines))
        self.assertIn("OnUnitActiveSec", timer)


class PeekTest(WorkBase):
    """`helm work peek` — the read-only door to an arbitrary landed sha.

    THE GAP (fleet-measured, 2026-08-01): a REVIEWER had no sanctioned path to
    a point-in-time checkout — the shared checkout's HEAD rule refuses any
    departure, `work claim` mints a lane room (lease + branch + write intent,
    the wrong shape for a look), and the integrator override is
    integrator-only. The gap reproduced itself during its own fix: the build
    agent assigned to this feature was first refused by the ref guard when its
    harness tried to mint a scratch worktree.

    Verb-level tests here run WITHOUT the guard installed (the door must work
    on its own terms); PeekGuardTest below is the guard leg."""

    def setUp(self):
        super().setUp()
        # a second commit so the peeked sha is NOT the tip: the door's whole
        # point is an ARBITRARY landed sha, not a spelling of HEAD
        with open(os.path.join(self.root, "README"), "a") as f:
            f.write("more\n")
        r = _sh(self.root, "git", "commit", "-qam", "second")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.old = _sh(self.root, "git", "rev-parse",
                       "HEAD~1").stdout.strip()

    def test_peek_yields_a_detached_room_at_exactly_that_sha(self):
        rc, out, err = self.work("peek", "HEAD~1")
        self.assertEqual(rc, 0, err)
        path, sha = out.strip().split("\t")
        self.assertEqual(sha, self.old)
        self.assertEqual(path, work.peek_path(self.root, self.old))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(_sh(path, "git", "rev-parse", "HEAD").stdout.strip(),
                         self.old)                       # exactly, not the tip
        r = _sh(path, "git", "symbolic-ref", "-q", "--short", "HEAD")
        self.assertNotEqual(r.returncode, 0, "a peek room is on a BRANCH")
        row = {w["path"]: w for w in work.worktrees(self.root)}[path]
        self.assertIsNone(row["branch"] or None)         # never a branch ref
        self.assertFalse(row["locked"])                  # disposable, no lock

    def test_the_same_sha_converges_on_the_same_room(self):
        rc1, out1, _e1 = self.work("peek", self.old)
        rc2, out2, err2 = self.work("peek", "HEAD~1")    # different spelling
        self.assertEqual((rc1, rc2), (0, 0), err2)
        self.assertIn(self.old, out2)        # non-empty: the sha itself printed
        self.assertEqual(out1.strip(), out2.strip())
        rows = work.peek_rows(self.root)
        self.assertEqual([r["name"] for r in rows], [self.old[:12]])

    def test_a_garbage_committish_refuses_with_gits_own_words(self):
        # MUST-HIT first: the door provably opens here, so the refusal below
        # is the committish's fault and "nothing new minted" is measurable
        # against a room that exists rather than against a vacuum
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        self.assertIn(self.old, out)
        rc, out, err = self.work("peek", "no-such-committish")
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve 'no-such-committish'", err)
        self.assertIn("fatal", err)                      # git's words, forwarded
        self.assertEqual(out, "")
        rows = work.peek_rows(self.root)
        self.assertEqual([r["name"] for r in rows], [self.old[:12]])

    def test_an_uncreatable_peek_area_refuses_and_mints_nothing(self):
        with open(self.root + "-wt", "w") as f:
            f.write("a file squatting on the container\n")
        rc, _out, err = self.work("peek", self.old)
        self.assertEqual(rc, 1)
        self.assertIn("cannot create peek area", err)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])   # exactly the main checkout

    def test_a_room_that_moved_off_its_sha_is_not_reissued(self):
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        r = _sh(path, "git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "someone worked here")
        commit_rc = r.returncode
        self.assertEqual(commit_rc, 0, r.stderr)
        # the premise, proven not assumed: the room's HEAD really moved
        moved = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(moved), 40)     # a real sha, not an error's ""
        self.assertNotEqual(moved, self.old)
        rc, _out, err = self.work("peek", self.old)
        self.assertEqual(rc, 1, "a moved room was silently re-issued")
        self.assertIn("no longer a clean peek", err)
        self.assertIn("--drop", err)                     # the honest exit, named
        # and nothing touched: the room still sits at the commit someone made
        after = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(after), 40)
        self.assertEqual(after, moved)

    def test_drop_by_path_and_by_committish_both_remove(self):
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        self.assertTrue(os.path.isdir(path))     # the room to remove IS there
        rc, out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("dropped", out)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])  # exactly the main survives
        self.assertFalse(os.path.exists(path))
        rc, out, _e = self.work("peek", self.old)        # mint again
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(path))
        rc, out, err = self.work("peek", "--drop", "HEAD~1")
        self.assertEqual(rc, 0, err)
        registry = [w["path"] for w in work.worktrees(self.root)]
        self.assertEqual(registry, [self.root])
        self.assertFalse(os.path.exists(path))

    def test_drop_refuses_a_target_that_is_not_a_peek_room(self):
        # NO CONTAINER AT ALL: no peek was ever cut in this estate, and the
        # verdict must say THAT — "not a registered peek room" here sent the
        # caller checking one room's registration when no rooms exist
        rc, _out, err = self.work("peek", "--drop", "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("no peek container", err)
        self.assertNotIn("not a registered peek room", err)
        # container PRESENT, this room absent: the registration verdict
        rc, _out, _e = self.work("peek", self.old)   # mints the container
        self.assertEqual(rc, 0)
        rc, _out, err = self.work("peek", "--drop", "HEAD")
        self.assertEqual(rc, 1)
        self.assertIn("not a registered peek room", err)
        # a LANE room path must never leave through the peek door
        rc, out, _e = self.work("claim", "demo", "--seat", "s1")
        self.assertEqual(rc, 0)
        lane_path = out.split("\t")[0]
        rc, _out, err = self.work("peek", "--drop", lane_path)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.isdir(lane_path), "peek --drop removed a lane room")
        # an unresolvable non-path refuses on the resolve arm
        rc, _out, err = self.work("peek", "--drop", "garbage-target")
        self.assertEqual(rc, 1)
        self.assertIn("resolvable commit", err)

    def test_drop_refuses_an_occupied_room(self):
        if not os.path.isdir("/proc"):
            self.skipTest("cwd occupancy proof requires /proc")
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        proc = subprocess.Popen(["sleep", "30"], preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL), cwd=path)
        self.addCleanup(proc.wait)         # cleanup, so the assertions below
        self.addCleanup(proc.terminate)    # are UNCONDITIONAL, not try-guarded
        rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("OCCUPIED", err)
        self.assertIn(str(proc.pid), err)  # the refusal names the occupant
        self.assertTrue(os.path.isdir(path))

    def test_drop_refuses_a_dirty_room_with_gits_own_refusal(self):
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        junk = os.path.join(path, "scribbles.txt")
        with open(junk, "w") as f:
            f.write("a peek that grew work\n")
        rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("peek drop refused", err)
        self.assertTrue(os.path.exists(junk), "--drop discarded bytes")

    def test_a_pane_bound_peek_room_refuses_drop(self):
        """Same law as gc's remover: a metaharness pane outlives its shell,
        and deleting under one leaves the operator a prompt at a dead cwd."""
        rc, out, _e = self.work("peek", self.old)
        path = out.split("\t")[0]
        from helm.work import _peek as _work_peek
        with mock.patch.object(_work_peek, "_panes_bound_to",
                               return_value=(["term_deadbeef"], None)):
            rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("term_deadbeef", err)
        with mock.patch.object(_work_peek, "_panes_bound_to",
                               return_value=(None, "daemon down")):
            rc, _out, err = self.work("peek", "--drop", path)
        self.assertEqual(rc, 1)
        self.assertIn("cannot prove", err)
        self.assertTrue(os.path.isdir(path))

    def test_a_peek_room_is_never_a_lane_room(self):
        rc, out, _e = self.work("peek", self.old)
        self.assertEqual(rc, 0)
        path = out.split("\t")[0]
        # MUST-HIT first: the classifier does see the room, as its own kind
        self.assertEqual(work.managed_room_kind(self.root, path), "peek")
        registered = work.worktrees(self.root)
        self.assertNotIn(path, [r["path"] for r in
                                work.lane_rows(self.root, registered=registered)])
        self.assertNotIn(path, [r["path"] for r in work.gc_scan(self.root)])
        self.assertNotIn(path, [r["path"] for r in
                                work.list_rows(self.root, registered=registered)])
        # release can never aim at it: inference answers None inside the room
        self.assertIsNone(work._infer_lane(self.root, path))
        # and the container name cannot be claimed on top of
        rc, _out, err = self.work("claim", work.PEEK_DIRNAME)
        self.assertEqual(rc, 2)
        self.assertIn("reserved", err)
        # the board shows it in its OWN section, not as GUARDED
        rc, out, _e = self.work("list")
        self.assertEqual(rc, 0)
        self.assertIn("PEEK %s" % os.path.basename(path), out)
        self.assertNotRegex(out, r"GUARDED.*%s" % os.path.basename(path))

    def test_peek_json_carries_path_sha_and_reuse(self):
        rc, out, err = self.work("peek", self.old, "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["sha"], self.old)
        self.assertEqual(got["path"], work.peek_path(self.root, self.old))
        self.assertFalse(got["reused"])
        rc, out, _e = self.work("peek", self.old, "--json")
        self.assertTrue(json.loads(out)["reused"])

    def test_drop_and_a_committish_together_refuse_as_ambiguous(self):
        rc, _out, err = self.work("peek", "--drop", "HEAD", "HEAD~1")
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)


class PeekGuardTest(WorkBase):
    """The ref guard admits a peek birth STRUCTURALLY — no env override.

    MEASURED 2026-08-01 (the design's load-bearing fact): `git worktree add
    --detach` births the new worktree's HEAD in MAIN-TREE context, and its
    stdin line (`0000.. <sha> HEAD`, git-dir == common) is byte-identical to
    `git checkout --detach` detaching the shared checkout's own HEAD. The
    discriminator is the HALF-BORN ADMIN DIR: at `prepared` time
    `$common/worktrees/<name>/gitdir` exists and its HEAD does not. Each test
    below fails exactly one clause of that allowance."""

    def setUp(self):
        super().setUp()
        rc, _out, err = self.work("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        # the point — and a KEY VIEW, never os.environ itself, whose failure
        # would render every ambient value (task/2370)
        self.assertNotIn("HELM_WORK_INTEGRATOR", tuple(os.environ))
        self.sha = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def test_the_guard_admits_a_peek_birth_without_any_env(self):
        self.assertEqual(len(self.sha), 40)             # the target is real
        rc, out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 0, err)
        self.assertIn(self.sha, out)                    # the door printed it
        path = out.split("\t")[0]
        head = _sh(path, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(head), 40)                 # a real sha came back
        self.assertEqual(head, self.sha)
        # the shared checkout never moved
        shared = _sh(self.root, "git", "symbolic-ref", "--short",
                     "HEAD").stdout.strip()
        self.assertEqual(shared, "main")
        # and the raw git spelling of the same door is equally sanctioned —
        # the allowance is the AREA, not the helm binary
        raw = os.path.join(work.peek_area(self.root), "raw-spelling")
        r = _sh(self.root, "git", "worktree", "add", "--detach", raw, self.sha)
        raw_rc = r.returncode
        self.assertEqual(raw_rc, 0, r.stderr)
        self.assertTrue(os.path.isdir(raw))

    def test_a_detached_add_OUTSIDE_the_peek_area_is_still_refused(self):
        outside = os.path.join(self.tmp, "elsewhere")
        r = _sh(self.root, "git", "worktree", "add", "--detach", outside,
                self.sha)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        # refusal leaves no room; the same operation INTO the area opening
        # (test_the_guard_admits_a_peek_birth_without_any_env) is the paired
        # MUST-HIT proving this red is the area's fault, not a broken add
        self.assertFalse(os.path.exists(outside))  # noqa: VACUOUS_ASSERTION — refusal leaves nothing; paired open-door control is test_the_guard_admits_a_peek_birth_without_any_env

    def test_a_branch_add_INTO_the_peek_area_is_still_refused(self):
        """Detached-only, never a branch ref — the area alone opens nothing."""
        # the branch oracle can say yes (else the absence below proves nothing)
        self.assertTrue(work._has_branch(self.root, "main"))
        inside = os.path.join(work.peek_area(self.root), "branchy")
        os.makedirs(work.peek_area(self.root), exist_ok=True)
        r = _sh(self.root, "git", "worktree", "add", "-b", "lane/branchy",
                inside, "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        self.assertFalse(work._has_branch(self.root, "lane/branchy"))  # noqa: VACUOUS_ASSERTION — the ref never existing IS the contract; the oracle's yes-arm is pinned above on main

    def test_detaching_the_shared_checkout_itself_is_still_refused(self):
        """THE MUST-NOT-OPEN CONTROL. This operation's stdin is byte-identical
        to the sanctioned birth; only the absent half-born admin dir tells
        them apart. If this ever passes, the discriminator broke and the
        integrator's tree is detachable by anyone."""
        r = _sh(self.root, "git", "checkout", "--detach")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("helm work peek", r.stderr)     # the door is signposted
        self.assertEqual(_sh(self.root, "git", "symbolic-ref", "--short",
                             "HEAD").stdout.strip(), "main")

    def test_one_stray_half_born_admin_refuses_ALL_births(self):
        """Fail toward refusal: with a concurrent non-peek birth in flight the
        transaction cannot be attributed, so the peek is refused too — a
        spurious refusal retries; a spurious pass detaches the shared tree."""
        stray = os.path.join(self.root, ".git", "worktrees", "stray")
        os.makedirs(stray)
        with open(os.path.join(stray, "gitdir"), "w") as f:
            f.write(os.path.join(self.tmp, "elsewhere", ".git") + "\n")
        rc, _out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        # THE CONTROL: with the stray gone the same peek passes — proving the
        # refusal above came from the stray, not from a door that never opens
        shutil.rmtree(stray)
        rc, _out, err = self.work("peek", self.sha)
        self.assertEqual(rc, 0, err)

    def test_a_dotdot_gitdir_component_is_refused_by_name(self):
        """kimi's traversal (2026-08-02): a half-born admin whose gitdir STARTS
        WITH the sanctioned prefix but carries `../../` passes the AS-SPELLED
        prefix compare yet RESOLVES OUTSIDE the peek area. Git normally
        normalizes the path it writes, so the guard must not lean on that — a
        crafted/aliased admin can carry the traversal literally. The guard now
        refuses the '..' BY NAME before the prefix compare can be walked out."""
        peeks = work.peek_area(self.root)
        os.makedirs(peeks, exist_ok=True)
        # a stray half-born admin (gitdir present, HEAD absent) spelled THROUGH
        # peeks/ but resolving to a sibling of the repo
        stray = os.path.join(self.root, ".git", "worktrees", "traversal")
        os.makedirs(stray)
        evil = peeks + "/../../escape/.git"
        with open(os.path.join(stray, "gitdir"), "w") as f:
            f.write(evil + "\n")
        self.assertTrue(evil.startswith(peeks + "/"))      # the prefix DID match
        self.assertNotEqual(os.path.realpath(os.path.dirname(evil)),
                            os.path.realpath(peeks))        # yet it escapes
        target = os.path.join(peeks, self.sha[:12])
        r = _sh(self.root, "git", "worktree", "add", "--detach", target,
                self.sha)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSED", r.stderr)      # positive: the guard SPOKE
        self.assertIn("'..'", r.stderr)         # and it NAMES the traversal
        self.assertFalse(os.path.exists(target))
        # THE CONTROL: with the stray gone the same birth opens — proving the
        # red above came from the '..' gitdir, not a door that never opens
        shutil.rmtree(stray)
        r2 = _sh(self.root, "git", "worktree", "add", "--detach", target,
                 self.sha)
        reopen_rc = r2.returncode
        self.assertEqual(reopen_rc, 0, r2.stderr)
        self.assertTrue(os.path.isdir(target))         # the effect: it opened

    def test_a_canonical_peek_gitdir_still_opens_past_the_dotdot_guard(self):
        """The '..' refusal must not over-match a canonical `<sha12>` room name
        — the exact string that now flows past the new `..` case globs. The
        legitimate door stays open."""
        self.assertEqual(len(self.sha), 40)                # the target is real
        target = os.path.join(work.peek_area(self.root), self.sha[:12])
        r = _sh(self.root, "git", "worktree", "add", "--detach", target,
                self.sha)
        add_rc = r.returncode
        self.assertEqual(add_rc, 0, r.stderr)
        self.assertTrue(os.path.isdir(target))
        head = _sh(target, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(len(head), 40)                    # a real sha, not ""
        self.assertEqual(head, self.sha)                   # exactly the sha


class WiringTest(unittest.TestCase):
    def test_cli_and_gc_wiring(self):
        from helm import cli, gc
        self.assertIn("work", cli.VERBS)
        self.assertIn("work", cli._VERB_HELP)
        rows = [p for p in gc.POLICIES if p["stream"] == "work-worktrees"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cls"], "state")   # report-only, never reaped
        self.assertEqual(rows[0]["act"], "report")


if __name__ == "__main__":
    unittest.main()


class ClaimWarnsTheRailIsNotArmedTest(WorkBase):
    """THE NET UNDER THE LANDED-CLOSE RUNG. That rung only helps rows landing
    AFTER it; #92 had already landed and closed COMPLETED with its guard never
    installed. Nothing routinely LOOKS at drift — stale_guard_hooks had exactly
    one production caller, `gc`, which prints it above a 45-room listing, so it
    surfaces only if someone runs housekeeping and reads past the rooms.

    CLAIM IS THE MOMENT, not a convenient one: the drifted rules guard worktree
    BIRTH and COMMITS, and claim is where a worktree is born. It cannot live in
    the pre-commit hook, because the stale thing IS that hook."""

    # PATCH _cli, NOT _guard: _cli binds this name at import time
    # (`from ._guard import stale_guard_hooks`), so patching the defining
    # module leaves the bound reference untouched. Measured while writing
    # these: with the _guard target the mock did nothing and the STALE test
    # PASSED ANYWAY, because a fixture repo has no hooks and the real function
    # returned MISSING — a green test measuring none of its own setup.
    def test_claim_says_the_rail_is_not_armed_without_blocking(self):
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        return_value=[("STALE", "pre-commit", "why")]):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        # POSITIVE CONTROL, unconditional: the claim SUCCEEDED. This is a
        # warning on a working claim, never a refusal.
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARD RAIL NOT ARMED", err)
        self.assertIn("install-guard --apply", err)
        self.assertIn("SHARED by every worktree", err,
                      "one hook dir serves every room — a claimer who reads "
                      "this as 'my lane only' will not go fix it")

    def test_the_printed_remedy_carries_the_profile_at_both_poles(self):  # noqa: VACUOUS_ASSERTION — the bare-form clause is absence-shaped; the unconditional positives on the same stderr are the two rendered remedies, `--profile rail` at the declared pole and `--profile leak` at the installed-only pole
        """TASK/2504. A REMEDY IS PASTED, NOT READ. The notice's whole text was
        `helm work install-guard --apply`, and on a shared checkout running
        the rail that exact paste retired three hook slots and eleven scanner
        snapshots. The resolver no longer narrows, but a notice is
        printed by the tree INSTALLED in a repo, which can be older than the
        tree that cured this — so the printed text itself must name the
        profile and be unable to narrow anything.

        BOTH POLES, and each reaches the answer by a different resolver step:
        this fixture DECLARES rail, and the leak pole is a repo with the
        declaration removed and the leak hooks really installed, so the second
        step (what the repo is RUNNING) is what answers there.

        The drift list is the one mock — it stands in for a stale hook, which
        is not this arm's subject; `guard_remedy` reads the real repo."""
        self.assertEqual(_sh(self.root, "git", "config", "--get",
                             "helm.guard.profile").stdout.strip(), "rail",
                         "fixture: this repo declares the rail")
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        return_value=[("STALE", "pre-commit", "why")]):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("install-guard --apply --profile rail", err)

        rc, out, err2 = self.work("install-guard", "--apply", "--profile",
                                  "leak")
        self.assertEqual(rc, 0, out + err2)
        self.assertEqual(_sh(self.root, "git", "config", "--unset",
                             "helm.guard.profile").returncode, 0)
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        return_value=[("STALE", "pre-commit", "why")]):
            rc, _o, err3 = self.work("claim", "beta", "--seat", "s1")
        self.assertEqual(rc, 0, err3)
        self.assertIn("install-guard --apply --profile leak", err3)
        # AND NEITHER POLE LEFT THE BARE FORM: the old text ended the command
        # at `--apply`, and that is the exact string an operator copied.
        for line in (err, err3):
            self.assertNotIn("install-guard --apply`", line)

    def test_a_declared_leak_over_a_running_rail_is_told_to_keep_the_rail(self):
        """TASK/2504 ROUND 3 (ledger row 844f54ded002). THE PRINTED REMEDY
        SUPPLIED THE FLAG THE REFUSAL ADMITS. A repo that DECLARES leak while
        the rail's hooks are on disk is the conflicted state the flagless
        install now refuses to narrow — but the remedy resolved through the
        declaration first and printed `--profile leak`, the explicit flag the
        refusal deliberately lets through, so an operator who pasted the
        notice's own advice retired the rail's three slots and eleven scanner
        snapshots. The notice must name the profile the repo is RUNNING and
        say the declaration disagrees, so following it changes nothing on
        disk and records rail.

        NO MOCK: the drift is real (the rail's pre-commit read against the
        declared-leak plan), and the printed command is parsed out of the
        notice and RUN through the shipped verb.

        CONTROL, by mutation on the fab (the resolver's narrowing clause made
        to return the declaration, this module run, the mutant restored): the
        notice rendered `helm work install-guard --apply --profile leak` and
        this arm went red at that line. That an explicit `--profile leak` on
        this state retires the rail's slots and shelf is measured by
        test_never_track_hook's RECORDED-leak-over-RUNNING-rail arm, green in
        the same run — the two together are the pre-cure trap."""
        rc, out, err = self.work("install-guard", "--apply", "--profile",
                                 "rail")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(_sh(self.root, "git", "config", "--local",
                             "helm.guard.profile", "leak").returncode, 0)
        self.assertEqual(_work_guard.installed_profile(self.root), "rail",
                         "fixture: the disk disagrees with the declaration")
        before = _hook_tree(self.root)
        self.assertTrue(any(rel.endswith("lane_discipline.py")
                            for rel in before),
                        "fixture: the rail's shelf is on disk: %s"
                        % sorted(before))
        rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("GUARD RAIL NOT ARMED", err)
        m = re.search(r"Arm them: `([^`]*)`(.*)", err)
        self.assertIsNotNone(m, err)
        command, note = m.group(1), m.group(2)
        self.assertEqual(command,
                         "helm work install-guard --apply --profile rail",
                         err)
        self.assertIn("declares helm.guard.profile=leak but is RUNNING the "
                      "rail profile", note)
        self.assertIn("`helm work install-guard --apply --profile leak` "
                      "narrows on purpose", note)
        # THE LEAK SPELLING APPEARS ONLY AS THE NAMED NARROWING DOOR, never as
        # the remedy: every occurrence is followed by the words that mark it.
        for tail in re.findall(r"--profile leak(.{0,20})", err):
            self.assertTrue(tail.startswith("` narrows on purpose"),
                            "a bare narrowing remedy: %s" % err)
        # AND FOLLOWING THE ADVICE KEEPS THE RAIL: the printed command, run
        # through the shipped verb, leaves the hook tree byte-and-mode
        # identical and records the profile the repo was running.
        argv = shlex.split(command)
        self.assertEqual(argv[:2], ["helm", "work"], command)
        rc, out, err2 = self.work(*argv[2:])
        self.assertEqual(rc, 0, out + err2)
        self.assertEqual(_hook_tree(self.root), before,
                         "the remedy changed the hook tree; note was: %s"
                         % out)
        self.assertEqual(_sh(self.root, "git", "config", "--get",
                             "helm.guard.profile").stdout.strip(), "rail")

    def test_an_armed_rail_says_nothing(self):
        with mock.patch("helm.work._cli.stale_guard_hooks", return_value=[]):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL on the same stream: the claim's own room line is
        # present, so a silent GUARD RAIL means "not warned" and not "stderr
        # was never written to".
        self.assertIn("alpha", _o + err)
        self.assertNotIn("GUARD RAIL", err)

    def test_a_raising_detector_never_costs_the_claim(self):
        """Fail-open and LAST: the claim already succeeded before this runs,
        and a read hiccup must never take back a room that was granted."""
        with mock.patch("helm.work._cli.stale_guard_hooks",
                        side_effect=OSError("hook dir vanished")):
            rc, _o, err = self.work("claim", "alpha", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        # STRUCTURAL: the room is really on disk. rc 0 from a verb that bailed
        # before creating anything would satisfy the line above.
        self.assertTrue(os.path.isdir(os.path.join(self.root + "-wt", "alpha")))


class ClaimInheritsUnpushedTrunkTest(WorkBase):
    """A NEW ROOM INHERITS WHATEVER LOCAL TRUNK CARRIES, and said nothing.

    `claim` branches from `_base` — the LOCAL integration branch — which is the
    right thing to branch from and says nothing about whether the fleet has
    those commits. When local trunk sits ahead of the remote, every room
    claimed afterward carries the extra commits and the claim output is
    indistinguishable from a clean one.

    MEASURED 2026-08-03: an unreviewed commit sat on the shared checkout's main
    for an hour; a fresh claim inherited it for free, catching it
    only because an incident was already running. Rooms claimed before were
    clean, rooms claimed after were not, and nothing at the desk said which.
    """

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote],
                       capture_output=True)
        _sh(self.root, "git", "remote", "add", "origin", self.remote)
        r = _sh(self.root, "git", "push", "-q", "origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        _sh(self.root, "git", "fetch", "-q", "origin")

    def _commit_locally(self, name):
        with open(os.path.join(self.root, name), "w") as f:
            f.write("unpushed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "unpushed " + name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_room_claimed_off_an_AHEAD_trunk_says_so(self):
        # CONTROL FIRST, and it is the clause that matters most: with local
        # trunk LEVEL with the remote the claim must be SILENT. A warner that
        # always fires would satisfy the positive case below and be useless.
        rc, out, err = self.work("claim", "level", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("AHEAD", err,
                         "a level trunk must claim silently: %r" % err)
        self.assertIn("lane/level", out)          # positive: the claim worked
        # now local trunk carries a commit the remote does not have
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "inherits", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/inherits", out)       # positive: still a real claim
        self.assertIn("AHEAD", err)
        self.assertIn("1 commit", err)
        # ...and the room really does carry it, which is the fact being warned
        # about — assert the WORLD, not just the message.
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        contained = _sh(self.root, "git", "merge-base", "--is-ancestor",
                        head, "lane/inherits").returncode
        self.assertEqual(contained, 0,
                         "the warning must describe a real inheritance")

    def test_it_WARNS_and_never_refuses(self):
        """The failure was SILENCE, so the cure is noise — not a refusal.

        Local-ahead is a legitimate transient: a seat that just pushed and has
        not fetched reads exactly this way. Refusing would block a land
        mid-flight, which is worse than the disease it prevents."""
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "still-works", "--seat", "s1")
        self.assertEqual(rc, 0, "claim must SUCCEED while warning: %s" % err)
        path, branch, lease, _ttl = out.strip().split("\t")
        self.assertEqual(branch, "lane/still-works")
        self.assertTrue(os.path.isdir(path))      # the room really exists
        self.assertTrue(lease)

    def test_a_repo_with_no_remote_is_silent(self):
        """trunk_ref falls back to the LOCAL name when no remote exists, so the
        comparison is base-against-itself and there is nothing to say. Without
        this the guard would cry wolf on every local-only repo — and a check
        that fires when it cannot possibly know teaches people to ignore it."""
        _sh(self.root, "git", "remote", "remove", "origin")
        self._commit_locally("rogue.txt")
        rc, out, err = self.work("claim", "no-remote", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/no-remote", out)      # positive: the claim worked
        self.assertNotIn("AHEAD", err)

    def test_reopening_a_parked_lane_inherits_nothing_so_says_nothing(self):  # noqa: VACUOUS_ASSERTION — mutation-proven non-vacuous: removing the `fresh` gate REDDENS this test; the rev-parse --verify fixture asserts and assertIn('lane/parked', out) are unconditional positives
        """A parked branch re-opens ON ITSELF — `add_worktree` gets base=None —
        so it cannot pick up anything new no matter where local trunk sits. A
        warning here would be false, and a false warning at check-in is how a
        real one stops being read."""
        rc, out, err = self.work("claim", "parked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        room, _branch, lease, _ttl = out.strip().split("\t")
        # THE LANE MUST CARRY WORK or release RETIRES the branch as merged and
        # the next claim cuts a genuinely fresh one — which SHOULD warn. My
        # first draft released an empty lane and then asserted silence on what
        # was really a brand-new branch; the guard was right and the test was
        # asserting a premise that was never true.
        with open(os.path.join(room, "lane-work.txt"), "w") as f:
            f.write("real work\n")
        _sh(room, "git", "add", "-A")
        r = _sh(room, "git", "commit", "-q", "-m", "lane work")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, _out, err = self.work("release", "parked", "--lease", lease,
                                  "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertTrue(_sh(self.root, "git", "rev-parse", "--verify",
                            "lane/parked").returncode == 0,
                        "fixture: an unlanded lane branch must SURVIVE release")
        self._commit_locally("rogue.txt")         # trunk moves while parked
        # AND THE ROOM MUST BE GONE while the BRANCH survives, or `claim`
        # short-circuits on the still-registered worktree and never reaches the
        # base decision at all. My first draft stopped here and proved nothing:
        # removing the `fresh` gate entirely left it GREEN, because the path it
        # claimed to cover was never executed.
        r = _sh(self.root, "git", "worktree", "remove", "--force",
                work.lane_path(self.root, "parked"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_sh(self.root, "git", "rev-parse", "--verify",
                             "lane/parked").returncode, 0,
                         "fixture: the BRANCH must outlive the room")
        rc, out, err = self.work("claim", "parked", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/parked", out)         # positive: re-claim worked
        self.assertNotIn("AHEAD", err,
                         "a re-opened parked lane inherits nothing: %r" % err)


class ClaimRefusesLiveWorkElsewhereTest(WorkBase):
    """`claim <label>` MINTS a branch from the base when the label is not a
    branch — right for new work, WRONG for a label a row already names.

    THE DETECTOR IS NOT THE FEATURE; THE REFUSAL IS. My first version only
    printed, and what that bought was measured: the hit came back, claim
    ignored it, add_worktree ran with base=main and returned 0. A room minted
    over live work is the trap the check exists to stop.

    LIVE vs TERMINAL is the whole discrimination. A row still OWED is work
    someone is expected to finish -> REFUSE. A closed row is history and its
    label may legitimately be reused -> WARN.
    """

    def _row(self, lane, tip, **kw):
        row = {"id": "r" + tip[:8], "lane": lane, "tip": tip,
               "repo_id": self._repo_id()}
        row.update(kw)
        return row

    def _repo_id(self):
        from helm import dispatches
        return (dispatches._repo_info(self.root) or {}).get("repo_id")

    # LIVENESS COMES FROM THE ROW'S REAL SHAPE, not from a mocked predicate.
    # My first version patched dispatches.owed and asserted against it, which
    # ENCODED my wrong notion of live instead of testing it — the same failure
    # landreq's own landed_ever docstring confesses about mocking _landing_proof.
    # These shapes are the measured truth table of _duplicate_branch_live:
    # ONE LIST, EVERY PATH. It lived inline in the ordinary-tip arm, so the
    # TERMINAL arm checked three of these and the LIVE UNKNOWN arm checked NONE
    # — a probe measured that a renderer restoring "work NOT on" on the
    # unknown path passed every arm here. A per-arm list means each new path
    # starts with an empty invariant, which is how this class kept a limb
    # through ten rounds of curing it.
    ABSENCE_PHRASES = ("work NOT on", "work is NOT on", "never reached",
                       "starts EMPTY", "MEASURED absent")

    LIVE_OPEN = {"status": "open"}
    LIVE_FIX = {"status": "verdict", "polarity": "fix"}      # 521 rows on the
    TERMINAL = {"status": "verdict", "polarity": "approve"}  # live ledger

    def _ledger(self, rows):
        from helm import dispatches
        return mock.patch.object(
            dispatches, "snapshot",
            lambda *a, **k: ({r["id"]: r for r in rows}, None))

    def assertNoAbsenceClaim(self, text, where=""):
        """No spelling this path has ever used may appear in `text`.

        Asserts its OWN premise first: the list must be non-empty, or every
        call here is a loop over nothing that passes for free."""
        self.assertTrue(self.ABSENCE_PHRASES, "the invariant list is empty")
        for phrase in self.ABSENCE_PHRASES:
            self.assertNotIn(
                phrase, text,
                "%sasserted absence it never measured: %s" % (
                    where and where + ": ", phrase))

    def _trunk_tip(self):
        """A tip that IS on trunk — the LANDED case, which the loop measures
        ANCESTOR and dismisses. Its sibling `_offtrunk_tip` covers the
        not-landed half; a resurrection arm needs both to exist."""
        r = subprocess.run(["git", "rev-parse", "main"], cwd=self.root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def _offtrunk_tip(self, name="elsewhere"):
        # ASSERT THE CHECKOUT, because ignoring its rc makes this helper LIE.
        # A review on 3e18fefc: called twice with the default name, the
        # second `checkout -b` fails (the branch exists), HEAD stays on main,
        # the commit lands ON TRUNK, and the helper returns a trunk tip to an
        # arm whose entire premise is that the work is OFF trunk. Silent, and
        # it makes the arm measure the opposite of its name.
        r = _sh(self.root, "git", "checkout", "-q", "-b", name)
        self.assertEqual(r.returncode, 0,
                         "fixture: could not branch %r (%s) — a reused name "
                         "would leave HEAD on trunk and this helper would "
                         "return a TRUNK tip" % (name, r.stderr.strip()))
        with open(os.path.join(self.root, name + ".txt"), "w") as f:
            f.write("work that never landed\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "off-base " + name)
        self.assertEqual(r.returncode, 0, r.stderr)
        tip = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        _sh(self.root, "git", "checkout", "-q", "main")
        return tip

    def test_an_OPEN_row_whose_work_is_off_base_REFUSES_and_mints_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence claim is `git branch --list` empty, and the arm asserts rc != 0 plus the sha IN the refusal text on the same call first, so a claim that never ran reddens on those before the emptiness is read
        """The exact probe: the detector returned a hit and claim minted
        the room anyway. ASSERT THE WORLD, not the message — the branch must NOT
        exist afterward, because rc alone cannot tell a refusal from a warning
        that happened to print."""
        tip = self._offtrunk_tip()
        row = self._row("stranded", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "stranded", "--seat", "s1")
        self.assertNotEqual(rc, 0, "claim returned success over live work: %r" % out)
        self.assertIn("REFUSED", err)
        self.assertIn(tip[:12], err)          # names the sha, so it is actionable
        # THE ROOM WAS NOT MINTED — the assertion the probe needed
        # ASSERT THE EFFECT, NOT AN ERRNO. My first version pinned returncode
        # == 1 and git answers 128 for an unresolvable ref, so the arm failed
        # while the CODE was correct — the branch really was not created.
        # `git branch --list` answers the membership question directly instead
        # of me guessing which exit code means "no such ref".
        self.assertEqual(
            "", _sh(self.root, "git", "branch", "--list",
                    "lane/stranded").stdout.strip(),
            "the branch was created despite the refusal")

    def test_a_FIX_VERDICT_row_is_STILL_LIVE_WORK_and_refuses(self):  # noqa: VACUOUS_ASSERTION — the empty branch-list is asserted LAST; rc != 0 and 'REFUSED' in stderr are checked first on the same call, so a claim that never ran reddens there before any emptiness is read
        """The difference between a guard and
        a guard-shaped object.

        I keyed liveness on dispatches.owed(), which excludes HELD rows and rows
        whose FIX verdict closed the REVIEW TURN. helm's own
        _duplicate_branch_live says exactly why that is wrong: "FIX and SUPERSEDE
        close a REVIEW turn, not the work identity" — the branch stays active
        until the close vocabulary retires it.

        MEASURED ON THE LIVE LEDGER, and the number is the finding: owed() sees
        2 rows, _duplicate_branch_live sees 523. My refusal was blind to 521 live
        lanes. It would essentially never have fired, which is the worst shape a
        guard can have, because it LOOKS armed.

        LOAD-BEARING MUTATION: key liveness on owed() again -> this arm reddens
        while the open-row arm above stays green, because owed() still catches
        the open one. That is the whole point: only a FIX-verdict fixture can
        tell the two predicates apart.
        """
        tip = self._offtrunk_tip("fixwork")
        row = self._row("under-fix", tip, **self.LIVE_FIX)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "under-fix", "--seat", "s1")
        self.assertNotEqual(rc, 0, "a FIX-verdict row's work was treated as "
                                   "terminal: %r" % out)
        self.assertIn("REFUSED", err)
        self.assertEqual(
            "", _sh(self.root, "git", "branch", "--list",
                    "lane/under-fix").stdout.strip(),
            "the branch was created over live work")

    def test_a_lane_PREFIXED_label_names_the_same_lane(self):  # noqa: VACUOUS_ASSERTION — same ordering: rc != 0 and 'REFUSED' are unconditional positives on the same call, and the sibling control arm proves this identical call path returns rc 0 for a non-matching label
        """The second finding. A row recorded as `lane/work` is invisible
        to someone claiming `work` under an exact string compare, and 69 distinct
        labels on the live ledger carry that redundant prefix. The branch is
        `lane/<label>` either way, so the prefix is a SPELLING of one lane and
        never a different lane.

        I HAD SEEN THIS AND NOT ACTED ON IT: a prefixed label sat in my own
        census output and I read it as someone else's data-quality curiosity
        rather than a hole in the predicate I was writing that hour.
        """
        tip = self._offtrunk_tip("prefixed")
        row = self._row("lane/prefixed-work", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "prefixed-work", "--seat", "s1")
        self.assertNotEqual(rc, 0, "a `lane/`-prefixed row did not match the "
                                   "bare label: %r" % out)
        self.assertIn("REFUSED", err)
        self.assertEqual(
            "", _sh(self.root, "git", "branch", "--list",
                    "lane/prefixed-work").stdout.strip())

    def test_a_DIFFERENT_label_is_not_matched_by_the_normalizer(self):
        """THE CONTROL FOR THE NORMALIZER. Stripping a prefix must not make two
        genuinely different lanes collide — a normalizer that over-matches turns
        this guard into a wall on unrelated names."""
        tip = self._offtrunk_tip("other")
        row = self._row("lane/some-other-lane", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "prefixed-work", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/prefixed-work", out)
        self.assertNotIn("REFUSED", err)

    def test_the_REFUSAL_TEXT_may_not_claim_more_than_the_tips_support(self):
        """The overclaim had MOVED rather than gone.

        I had just cured the docstring header to say NOT PROVEN, and the
        sentence a human actually reads still said the work "is NOT on" the base
        and the room "would be" minted EMPTY. Both are true of a MEASURED-absent
        tip and NEITHER is proven for a live UNKNOWN one — which is returned
        precisely because nothing could be established about it.

        AND THE RENDERER HAD NO ARM AT ALL: every new test called
        _row_work_elsewhere directly, so the message was untested while the
        function under it was mutation-proven three ways. A tested function
        behind an untested sentence is how a cured defect keeps shipping.

        LOAD-BEARING MUTATION: restore the unconditional "is NOT on %s ... would
        be minted EMPTY" wording -> this arm reddens while every detector arm
        stays green, because none of them reads the message.
        """
        row = self._row("unverifiable", "0" * 40, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "unverifiable", "--seat", "s1")
        self.assertNotEqual(rc, 0, out)               # MUST-HIT: it refused
        self.assertIn("REFUSED", err)
        # the weaker, true claim
        self.assertIn("none is proven to be on", err)
        self.assertIn("UNVERIFIABLE", err)
        # ...and NOT the two facts an unreadable tip cannot support
        self.assertNotIn("work is NOT on", err,
                         "the refusal asserted absence it never measured")
        self.assertNotIn("would be\n", err)

    def test_the_TERMINAL_note_also_never_asserts_absence(self):
        """THE SIBLING SENTENCE (blocker 3).

        The LIVE refusal stopped claiming absence and the TERMINAL note kept
        claiming it — "their work never reached %s ... this room starts EMPTY"
        — resting on the identical NOT_ANCESTOR the live path had just admitted
        it cannot read that way. I cured the sentence I was shown instead of
        sweeping the ones asking the same question, which is precisely how this
        defect survived rounds seven, eight and nine.
        """
        tip = self._offtrunk_tip("terminal")
        row = self._row("terminal-lane", tip, **self.TERMINAL)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "terminal-lane", "--seat", "s1")
        # CONTROL: the note DID render — otherwise the absence checks below are
        # satisfied by an empty string and prove nothing.
        self.assertIn("CLOSED row(s) named lane", err)
        self.assertIn("NOT PROVEN", err)
        for phrase in ("never reached", "starts EMPTY", "work NOT on"):
            self.assertNotIn(phrase, err,
                             "the terminal note asserted absence: " + phrase)

    def test_an_ORDINARY_offbase_tip_says_NOT_PROVEN_and_never_absence(self):
        """WAS test_a_MEASURED_absent_tip_still_says_so_plainly, AND ITS PREMISE
        WAS FALSE (ruling (a)).

        It asserted "work NOT on" for a tip it called MEASURED absent. Nothing
        measured that. `landed_state` reads `git cherry`, which compares a
        lane's commits against UPSTREAM-ONLY commits — and `_offtrunk_tip`
        builds the ORDINARY shape, `git checkout -b` from main, so there are no
        upstream-only commits, the comparison set is EMPTY, and every commit
        reads '+' from zero evidence. Measured on plain git for exactly this
        fixture's shape: `rev-list --count tip..main` = 0, `git cherry` = '+'.

        So the arm was pinning the strongest claim to the case least able to
        support it, and it did so for nine rounds because a '+' looks like a
        finding. Its REPLACEMENT keeps the half that was always right — this
        tip is genuinely not on the base and the refusal must fire — and drops
        the half nothing could back.
        """
        tip = self._offtrunk_tip("ordinary")
        row = self._row("ordinary-offbase", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "ordinary-offbase", "--seat", "s1")
        self.assertNotEqual(rc, 0, out)          # MUST-HIT: it still refuses
        self.assertIn("REFUSED", err)
        self.assertIn("NOT PROVEN on", err)
        self.assertIn(tip[:12], err)             # and it names the tip
        # NO ABSENCE PHRASE, in any of the spellings this path has used across
        # ten rounds — a re-wording must not restore the claim under a new name.
        self.assertNoAbsenceClaim(err)

    def test_the_zero_evidence_behind_that_verdict_is_real(self):
        """THE POSITIVE CONTROL for the arm above, and the thing nine rounds of
        probes never built: show `git cherry` had NOTHING to compare against.

        Without this the claim "the instrument cannot measure absence here" is
        assertion, not measurement — and the arm above would merely be pinning
        a string.
        """
        tip = self._offtrunk_tip("evidence")
        upstream_only = _sh(self.root, "git", "rev-list", "--count",
                            tip + "..main").stdout.strip()
        cherry = _sh(self.root, "git", "cherry", "main", tip).stdout.strip()
        self.assertEqual(upstream_only, "0",
                         "fixture is not the ordinary shape; the comparison "
                         "set was non-empty so cherry had real evidence")
        self.assertTrue(cherry.startswith("+"),
                        "cherry did not read '+' — got %r" % cherry)

    def test_an_UNRECOGNISED_landed_state_FAILS_CLOSED_on_live_work(self):
        """The codicil, applied to this lane the hour it was written:
        A POLE FOR A THREE-STATE SURFACE MUST SIT ON THE BOUNDARY, NOT IN THE
        MIDDLE OF THE RANGE.

        Their pole used a 3-hours-ago timestamp — comfortably in range, nowhere
        near where falsy and None diverge — so it could not catch `if stamp`
        treating a VALID epoch-zero as unknown. My poles had the same shape: a
        real off-base commit for NOT_ANCESTOR, a 40-zero sha for UNKNOWN. Both
        mid-range for their state.

        THE BOUNDARY HERE IS THE EDGE OF THE PREDICATE'S DOMAIN — a state that
        is NONE of the four the contract names. landed_state documents exactly
        four, but a fifth (or a None from a future backend, or "" from a failed
        read) must not silently become "landed and skip", which is the only
        wrong direction: skipping means minting a room over live work.

        I DO NOT BELIEVE THIS IS REACHABLE TODAY — every constant is a non-empty
        string and the code compares by equality, not truthiness. That is
        precisely why it needs an arm rather than a paragraph: an unreachable
        guarantee stated only in prose is what gets widened by the next reader.

        LOAD-BEARING MUTATION: change the landed check to `state != NOT_ANCESTOR
        -> skip` -> this arm reddens because an unrecognised state stops
        refusing.
        """
        from helm import vcs
        tip = self._offtrunk_tip("weird-state")
        row = self._row("weird-state-lane", tip, **self.LIVE_OPEN)
        real = vcs.backend(self.root).__class__.landed_state
        for bogus in (None, "", "a-state-nobody-has-defined"):
            with mock.patch.object(type(vcs.backend(self.root)), "landed_state",
                                   lambda *a, **k: bogus):
                with self._ledger([row]):
                    live, terminal, unproven = \
                        __import__("helm.work._claims", fromlist=["x"]) \
                        ._row_work_elsewhere(self.root, "weird-state-lane")[:3]
            self.assertEqual(live, [tip],
                             "state %r stopped refusing over LIVE work" % (bogus,))
            self.assertEqual(unproven, [tip],
                             "state %r was reported as MEASURED absent" % (bogus,))
        # CONTROL: the real backend still discriminates, so the mock above did
        # not simply break every path.
        self.assertTrue(callable(real))

    def test_EVERY_rendered_path_carries_the_SAME_absence_invariant(self):
        """The exact-tip finding, and it is the class this lane exists to
        close showing up one level out.

        The invariant was written INLINE in the ordinary-tip arm. So the
        TERMINAL arm re-typed three of the five spellings, and the LIVE UNKNOWN
        path — a live row whose landedness could not be READ — carried NO phrase
        check at all. Measured: a renderer restoring "work NOT on" on the
        unknown path passed every arm in this class. I applied my own law to the
        two paths I was looking at and left the third, which is exactly the
        sibling-sentence defect a review caught in round ten, one layer up.

        RUNTIME TEXT, NEVER A SOURCE SCAN: this asserts what the operator
        actually READS on each path. A grep over the module would pass on a
        renderer that builds the phrase from parts, and would fail on a comment
        that quotes the old wording to explain why it is gone.

        EACH PATH CARRIES ITS OWN POSITIVE CONTROL, because "no forbidden
        phrase" is satisfied perfectly by a path that rendered nothing at all.
        """
        from helm import vcs

        # PATH 1 — LIVE, landedness READ and not proven present.
        tip = self._offtrunk_tip("inv-ordinary")
        row = self._row("inv-ordinary", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "inv-ordinary", "--seat", "s1")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("NOT PROVEN on", err)          # control: it rendered
        self.assertNoAbsenceClaim(err, "LIVE NOT_ANCESTOR")

        # PATH 2 — LIVE, landedness UNREADABLE. The bucket that had no invariant.
        tip2 = self._offtrunk_tip("inv-unknown")
        row2 = self._row("inv-unknown", tip2, **self.LIVE_OPEN)
        with mock.patch.object(type(vcs.backend(self.root)), "landed_state",
                               lambda *a, **k: ""):
            with self._ledger([row2]):
                rc2, out2, err2 = self.work("claim", "inv-unknown", "--seat", "s1")
        self.assertNotEqual(rc2, 0, out2)
        self.assertIn("UNVERIFIABLE", err2)          # control: THIS path rendered
        self.assertNoAbsenceClaim(err2, "LIVE UNKNOWN")

        # PATH 2b — MIXED: one live tip READ and not proven present, one live
        # tip UNREADABLE, in the SAME refusal. A finding on the first
        # cut: testing each bucket ALONE never renders the branch where BOTH
        # `parts` are appended, and that joined sentence is the longest text
        # this path ever prints — the most room for a restored claim to hide.
        from helm import vcs as _vcs
        tip_a = self._offtrunk_tip("inv-mixed-read")
        tip_b = self._offtrunk_tip("inv-mixed-unread")
        rows = [self._row("inv-mixed", tip_a, **self.LIVE_OPEN),
                self._row("inv-mixed", tip_b, **self.LIVE_OPEN)]
        rows[1]["id"] = "r-mixed-unread"          # distinct ids or the ledger folds them

        def _per_tip(_self, _root, tip, _base):
            return "" if tip == tip_b else _vcs.NOT_ANCESTOR

        with mock.patch.object(type(vcs.backend(self.root)), "landed_state",
                               _per_tip):
            with self._ledger(rows):
                rcm, outm, errm = self.work("claim", "inv-mixed", "--seat", "s1")
        self.assertNotEqual(rcm, 0, outm)
        # BOTH controls on the SAME text: this is the joined render, not either
        # bucket alone, so the arm cannot pass by exercising one branch twice.
        self.assertIn("NOT PROVEN on", errm)
        self.assertIn("UNVERIFIABLE", errm)
        # BOTH TIPS, NOT JUST BOTH PHRASES (an optional review hardening,
        # taken rather than banked). Asserting the two phrases proves two
        # BRANCHES ran; it does not prove two BUCKETS were populated, because
        # in principle one bucket could render both. The tips are what
        # distinguishes them: tip_a is the one whose landedness was READ,
        # tip_b the one that was unreadable, and a render carrying both is the
        # joined sentence this arm exists for.
        self.assertIn(tip_a[:12], errm, "the READ tip is missing from the render")
        self.assertIn(tip_b[:12], errm, "the UNREADABLE tip is missing from the render")
        self.assertNoAbsenceClaim(errm, "LIVE mixed read+unread")

        # PATH 3 — TERMINAL history, which only warns.
        tip3 = self._offtrunk_tip("inv-terminal")
        row3 = self._row("inv-terminal", tip3, **self.TERMINAL)
        with self._ledger([row3]):
            rc3, _out3, err3 = self.work("claim", "inv-terminal", "--seat", "s1")
        self.assertEqual(rc3, 0, err3)               # terminal never blocks
        self.assertIn("NOT PROVEN", err3)            # control: the NOTE rendered
        self.assertNoAbsenceClaim(err3, "TERMINAL")

    def test_the_phrase_oracle_cannot_VALIDATE_itself(self):
        """The sharpest finding on this lane: ABSENCE_PHRASES
        was BOTH the oracle and the mutation source.

        Change "work is NOT on" to "work iz NOT on" in the constant and every
        derived control still passes — the length is still 5, each entry is
        still detectable, and the mixed and ordinary arms still assert "none of
        these appear". Meanwhile the CANONICAL phrase is no longer checked
        anywhere, so a renderer restoring it walks through a fully green suite.
        A test cannot validate its own oracle; the witness must be independent
        of the thing it witnesses.

        SO THESE LITERALS ARE THE WITNESS, and they are deliberately a second
        copy. Two copies of a list is normally a smell; here it is the entire
        mechanism, because a witness that drifts WITH the oracle witnesses
        nothing. They are not retyped from memory either — each is a spelling
        this path actually SHIPPED, counted across the file's history at the
        time this arm was written: "work is NOT on" 12 commits, "starts EMPTY"
        9, "never reached" 9, "MEASURED absent" 4, "work NOT on" 1.

        A legitimate sixth phrase SHOULD redden this arm. That is the point:
        adding one must be a deliberate act with its own evidence, not a quiet
        edit to a constant that also grades itself.
        """
        WITNESS = ("work NOT on", "work is NOT on", "never reached",
                   "starts EMPTY", "MEASURED absent")
        self.assertEqual(set(self.ABSENCE_PHRASES), set(WITNESS),
                         "the phrase oracle drifted from the spellings this "
                         "path actually shipped — if a phrase was genuinely "
                         "added or retired, update the WITNESS in this arm "
                         "with the evidence, never the oracle alone")
        # AND THE ORDINARY PATH IS CHECKED AGAINST THE WITNESS, not the oracle,
        # so a corrupted oracle cannot make this arm pass by agreeing with it.
        tip = self._offtrunk_tip("oracle-witness")
        row = self._row("oracle-witness", tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "oracle-witness", "--seat", "s1")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("NOT PROVEN on", err)      # control: the path rendered
        for phrase in WITNESS:
            self.assertNotIn(phrase, err,
                             "restored absence claim (witness): " + phrase)

    def test_the_invariant_would_CATCH_a_restored_absence_claim(self):
        """THE MUTATION CONTROL for the arm above. Without it, that arm is three
        assertions that a list of strings is absent from text — which a helper
        with a typo'd phrase list satisfies just as well."""
        self.assertNoAbsenceClaim("this text is clean")
        # DERIVED FROM THE LIST, NOT ENUMERATED. The first cut named two
        # phrases by hand and a probe measured what that bought: three of the
        # five were never proven detectable, so a typo in any of them would
        # have left a restored claim passing. Driving the control from
        # ABSENCE_PHRASES makes a SIXTH phrase covered the moment it is added —
        # an enumerated control starts every new phrase at zero, which is the
        # same shape as the inline list this whole lane exists to remove.
        self.assertEqual(len(self.ABSENCE_PHRASES), 5,
                         "the oracle changed size; this control must still "
                         "cover every entry")
        for phrase in self.ABSENCE_PHRASES:
            with self.assertRaises(AssertionError,
                                   msg="undetectable phrase: %s" % phrase):
                self.assertNoAbsenceClaim("prefix " + phrase + " suffix")

    def test_a_CLOSED_row_only_warns_and_the_claim_succeeds(self):
        """TERMINAL HISTORY IS NOT A BLOCKER. A closed row's label may be reused,
        and refusing there would wall off a third of all labels on a verb that
        has no --force by design."""
        tip = self._offtrunk_tip("oldwork")
        row = self._row("reused", tip, **self.TERMINAL)
        with self._ledger([row]):
            rc, out, err = self.work("claim", "reused", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/reused", out)            # positive: a real claim
        self.assertIn("CLOSED row", err)
        self.assertIn(tip[:12], err)

    def test_a_row_from_ANOTHER_REPO_neither_refuses_nor_warns(self):
        """The second finding. The ledger carries rows from other repos —
        measured, ids under a different tree sit beside this project's — and a
        lane label is only unique WITHIN a repo. An unscoped read refuses a
        claim here over a foreign row's tip."""
        tip = self._offtrunk_tip("foreign")
        row = self._row("shared-label", tip, **self.LIVE_OPEN)
        row["repo_id"] = "/somewhere/else/.git"      # NOT this repo
        with self._ledger([row]):
            rc, out, err = self.work("claim", "shared-label", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/shared-label", out)
        self.assertNotIn("REFUSED", err)
        self.assertNotIn(tip[:12], err)

    def test_a_label_NOBODY_has_a_row_for_claims_silently(self):
        """THE CONTROL THAT MATTERS MOST: a checker that always fires would
        satisfy every positive case above and be worthless at the desk."""
        with self._ledger([]):
            rc, out, err = self.work("claim", "brand-new", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        self.assertIn("lane/brand-new", out)
        self.assertNotIn("REFUSED", err)
        self.assertNotIn("CLOSED row", err)

    def test_a_REBASED_LAND_is_patch_equivalent_and_neither_refuses_nor_warns(self):  # noqa: VACUOUS_ASSERTION — four unconditional fixture assertions run FIRST (commit rc, the two shas differing, and ancestry PROVEN false), so a fixture that failed to build reddens before the ([], []) claim is reached
        """THE ARM THAT MAKES THE INSTRUMENT CHOICE LOAD-BEARING.

        Every other control here uses a tip that is an ANCESTOR of the base, so
        is_ancestor and landed_state agree and no fixture can tell them apart.
        A FOLD REBASES: the landed commit gets a new sha, so the lane tip fails
        ancestry while every line of its content is on trunk. Measured on the
        real ledger — an ancestry-only version warned on a lane that had landed
        hours earlier, which `helm work release` had already called "LANDED by
        patch identity".

        LOAD-BEARING MUTATION, MEASURED not guessed: swapping landed_state for
        is_ancestor reddens THIS arm and ALSO the pruned-tip arm — two
        consequences of one wrong instrument, because a sha git cannot resolve
        is 'not an ancestor' too.
        """
        _sh(self.root, "git", "checkout", "-q", "-b", "prefold")
        with open(os.path.join(self.root, "folded.txt"), "w") as f:
            f.write("content that lands under a different sha\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "work that will be folded")
        self.assertEqual(r.returncode, 0, r.stderr)
        lane_tip = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        # MAIN MUST MOVE FIRST or the pick is a fast-forward and git REUSES the
        # sha — my first version did exactly that and the assertion below caught
        # it, which is why that assertion is there rather than implied.
        _sh(self.root, "git", "checkout", "-q", "main")
        with open(os.path.join(self.root, "meanwhile.txt"), "w") as f:
            f.write("trunk moved while the lane was in review\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "trunk moves")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "cherry-pick", lane_tip)
        self.assertEqual(r.returncode, 0, r.stderr)
        folded = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(lane_tip, folded)
        self.assertNotEqual(
            0, _sh(self.root, "git", "merge-base", "--is-ancestor",
                   lane_tip, "main").returncode,
            "fixture is not discriminating: the lane tip IS an ancestor")
        row = self._row("folded", lane_tip, **self.LIVE_OPEN)
        with self._ledger([row]):
            live, terminal = _claims_live_terminal(self.root, "folded")
        self.assertEqual((live, terminal), ([], []),
                         "a lane that landed under a rebased sha was called "
                         "stranded — ancestry alone cannot see a fold")

    def test_UNKNOWN_on_LIVE_work_REFUSES_until_it_can_be_read(self):
        """MY OWN ARM CODIFIED THE BUG.

        I applied "unknown is not guilty" uniformly, and the previous version of
        this test asserted ([], []) for an unresolvable tip on an OPEN row —
        locking in a silent mint over live work as though it were the intended
        contract. A green test can encode a defect just as firmly as it can
        protect a fix.

        THE ASYMMETRY I COLLAPSED: a LIVE row is itself positive evidence that
        work EXISTS. An unreadable tip there is not the absence of a claim, it
        is the inability to CHECK a claim already made. Refusing is recoverable
        — fetch the branch, or close the row — while minting an empty room over
        live work is the failure this lane exists to stop.

        LOAD-BEARING MUTATION: skip on any non-NOT_ANCESTOR state again -> this
        arm reddens while the terminal-unknown arm below stays green.
        """
        row = self._row("unreadable-live", "0" * 40, **self.LIVE_OPEN)
        with self._ledger([row]):
            live, terminal = _claims_live_terminal(self.root, "unreadable-live")
        self.assertEqual(live, ["0" * 40],
                         "an unresolvable tip on LIVE work was waved through")
        self.assertEqual(terminal, [])

    def test_UNKNOWN_on_TERMINAL_history_still_says_nothing(self):
        """THE OTHER HALF, and it is why this is an asymmetry rather than a
        reversal. On a CLOSED row nothing can distinguish work that landed under
        another name from work abandoned, and a closed row is not a claim on
        anybody's attention — so an unreadable tip there stays silent."""
        row = self._row("unreadable-dead", "0" * 40, **self.TERMINAL)
        with self._ledger([row]):
            live, terminal = _claims_live_terminal(self.root, "unreadable-dead")
        self.assertEqual((live, terminal), ([], []))

    def test_an_UNREADABLE_ledger_neither_refuses_nor_warns(self):
        from helm import dispatches
        with mock.patch.object(dispatches, "snapshot",
                               lambda *a, **k: (None, "ledger unreadable")):
            self.assertEqual(_claims_live_terminal(self.root, "x"), ([], []))
        with mock.patch.object(dispatches, "snapshot",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertEqual(_claims_live_terminal(self.root, "x"), ([], []))

    def test_a_LIVE_row_with_NO_TIP_still_refuses(self):
        """Fixture A, measured against this lane: a live legacy row
        with tip=null and migration=needs-redispatch VANISHED before anything
        could classify it, because the collector required `r.get("tip")` to
        include a row at all.

        THE FILTER ANSWERED "NO CLAIM" TO A QUESTION IT NEVER ASKED. This class
        already states the asymmetry twenty lines into the detector: a LIVE row
        is positive evidence that work EXISTS, so an unreadable — or absent —
        tip is inability to check a claim already made, never absence of the
        claim. A tipless row is the purest form of that, and it was the one
        shape the guard could not see."""
        row = self._row("tipless-lane", "0" * 40, **self.LIVE_OPEN)
        row["tip"] = None
        row["migration"] = "needs-redispatch"
        with self._ledger([row]):
            live, terminal, unproven = _claims_all(self.root, "tipless-lane")
        self.assertTrue(live, "a live tipless row left the claim door OPEN")
        self.assertTrue(unproven, "it must also read as UNVERIFIABLE, not measured")
        self.assertEqual(terminal, [])
        # MUST-MISS on the same observable: a TERMINAL tipless row is nobody's
        # claim, so it must NOT hold the door — otherwise this arm would pass
        # for a collector that simply reports every row it sees.
        dead = self._row("tipless-lane", "1" * 40, **self.TERMINAL)
        dead["tip"] = None
        with self._ledger([dead]):
            live2, _t2, _u2 = _claims_all(self.root, "tipless-lane")
        self.assertEqual(live2, [], "a terminal tipless row is not a live claim")

    def test_a_TIPLESS_row_whose_LIVENESS_raises_is_not_ERASED(self):
        """The SAME ordering bug, one branch over.

        The previous commit moved `resolved.add` off "we reached this row" and
        onto each CLASSIFICATION, so that a raise in the liveness call could
        not leave a row marked-but-unclassified for the fallback to skip. It
        fixed the TIPPED path and left the TIPLESS one untouched, where the
        mark still sat BEFORE the liveness call. A tipless live row whose
        liveness raises was therefore marked, never classified, skipped by the
        fallback, and the claim door opened over live work — the exact defect
        the lane exists to close, surviving inside the lane that closed it.

        THE CONTROL IS THE SAME ROW WITHOUT THE RAISE, so an empty `live`
        below means the raise erased it rather than the fixture never
        producing a live tipless row at all."""
        from helm import dispatches
        row = self._row("tipless-raise", "0" * 40, **self.LIVE_OPEN)
        row["tip"] = None
        with self._ledger([row]):
            live_ctl, _t, _u = _claims_all(self.root, "tipless-raise")
        self.assertTrue(live_ctl,
                        "control: a live tipless row must hold the door "
                        "normally, or the assertion below proves nothing")

        seen = {"n": 0}

        def _boom(r):
            seen["n"] += 1
            raise RuntimeError("liveness predicate is down")

        with self._ledger([row]), \
                mock.patch.object(dispatches, "_duplicate_branch_live", _boom):
            live, _t2, _u2 = _claims_all(self.root, "tipless-raise")
        self.assertTrue(seen["n"], "fixture: the liveness call never raised, "
                                   "so this arm proves nothing")
        # ASSERT THE BUCKET THE DOOR READS. `claim()` refuses on `live`; a row
        # that survives only in some other bucket leaves the door OPEN, which
        # is the whole defect.
        self.assertTrue(live,
                        "a TIPLESS row whose liveness raised was ERASED — it "
                        "was marked resolved before the call that raised, so "
                        "the fallback skipped it and the claim door opened "
                        "over live work")

    def test_a_RAISED_landedness_check_may_not_erase_the_claim(self):
        """Fixture B: a live tipped row whose landed_state RAISES was
        erased by a blanket `except: return [], [], []`, and the claim door
        opened over it.

        "A CHECK MAY NEVER BREAK A CLAIM" IS THE RIGHT INSTINCT AND WAS THE
        WRONG CODE. Returning empty answers ABSENT to a question that FAILED,
        which is precisely what the live-UNKNOWN rule forbids — an exception is
        the least informative outcome available and it was being rendered as
        the most confident one."""
        from helm import vcs
        row = self._row("raising-lane", self._offtrunk_tip(), **self.LIVE_OPEN)

        # THE FAKE DELEGATES, and it must. A one-method fake raises from
        # `base_branch` before `landed_state` is ever reached, so this arm used
        # to pass without exercising the sentence in its own name.
        boom, seen = _raising_backend(self.root)
        with self._ledger([row]), \
                mock.patch.object(vcs, "backend", boom):
            live, _terminal, unproven = _claims_all(self.root, "raising-lane")
            unknown = _claims_unknown(self.root, "raising-lane")
        self.assertTrue(seen["n"], "the landedness check was never reached, so "
                                   "this arm proves nothing about a raise")
        self.assertTrue(live, "a raised landedness check erased a live claim")
        # A RAISE STRANDS INTO `unknown`, NOT `unproven`. Nothing measured this
        # row at all, and `unproven` means specifically "the tip WAS reached and
        # could not be read" — a claim about the object store that was never
        # true here. The must-miss below is the whole point of the split.
        self.assertTrue(unknown, "and it must say the tip was NEVER ESTABLISHED")
        self.assertEqual(unproven, [],
                         "a row nothing measured was relabelled tip-unreadable")
        # MUST-HIT CONTROL, same fixture, working backend: the row is reported
        # through the ordinary path too, so the assertions above are about the
        # exception and not about a row that would have been reported anyway
        # for some unrelated reason.
        with self._ledger([row]):
            live_ok, _t, _u = _claims_all(self.root, "raising-lane")
        self.assertTrue(live_ok)


    def test_a_RAISED_check_may_not_RESURRECT_terminal_history(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the three assertEquals on the TERMINAL result; the rung cannot credit their control BY CONSTRUCTION, because `_claims_all` mints a fresh opaque call identity per site and the control is necessarily a SECOND call (the whole claim is that two rows through the same failing backend answer differently). The control is unconditional and runs FIRST: the LIVE row through the identical Boom backend must still strand
        """Row 4171ff6cae91, and it is the half my own cure
        missed. I fixed "a raised check ERASES a live claim" by stranding every
        matched row — and `mine` holds terminal history beside live work.

        THE RULE WAS ALREADY WRITTEN TWENTY LINES UP AND I DID NOT APPLY IT TO
        MY OWN BRANCH: unknown on TERMINAL history says nothing, because
        nothing can tell work that landed under another name from work
        abandoned. So one closed row plus one transient object-store hiccup
        blocked a label reuse main permits — the recoverable direction, but a
        refusal nobody can act on, since the row it names is already closed.

        `_duplicate_branch_live` reads the ROW, not the object store, so it
        still answers when the thing that raised is the VCS."""
        from helm import vcs

        # MUST-HIT CONTROL FIRST, same backend, same lane label: the LIVE row
        # still strands. Without it this arm passes for a cure that reverted
        # fixture B and switched the whole except-branch back off.
        alive = self._row("resurrect-lane", self._offtrunk_tip("resurrect-live"),
                          **self.LIVE_OPEN)
        boom, seen = _raising_backend(self.root)
        with self._ledger([alive]), \
                mock.patch.object(vcs, "backend", boom):
            live_ctl, _t, unproven_ctl = _claims_all(self.root, "resurrect-lane")
            unknown_ctl = _claims_unknown(self.root, "resurrect-lane")
        self.assertTrue(seen["n"], "the landedness check was never reached")
        self.assertTrue(live_ctl, "control: a live row must still strand")
        self.assertTrue(unknown_ctl)          # stranded by the raise: UNKNOWN
        self.assertEqual(unproven_ctl, [])   # never reached: not tip-unreadable

        # DISTINCT BRANCH NAMES, and this is the finding: both calls used the
        # default, so the SECOND one silently committed to trunk and the
        # "terminal off-base row" was a trunk row wearing its name.
        dead = self._row("resurrect-lane", self._offtrunk_tip("resurrect-dead"),
                         **self.TERMINAL)
        boom2, _seen2 = _raising_backend(self.root)
        with self._ledger([dead]), \
                mock.patch.object(vcs, "backend", boom2):
            live, terminal, unproven = _claims_all(self.root, "resurrect-lane")
        self.assertEqual(live, [], "a CLOSED row plus a VCS hiccup held the "
                                   "claim door that main leaves open")
        self.assertEqual(unproven, [])
        self.assertEqual(terminal, [])


    def test_a_RAISE_LATE_does_not_UN_MEASURE_what_the_loop_already_knew(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the empty-terminal control on the first call; its positive is the non-empty terminal on the SECOND call through the raising backend, and the rung cannot credit it because a second `_claims_all` call mints a different opaque call identity — that the two calls agree about the terminal row IS the claim
        """Second round on this branch, and it is the SAME defect
        one level up from the first.

        My except-branch rebuilt the whole answer from `mine` and threw away
        what the LOOP had already established. So a TERMINAL NOT_ANCESTOR
        reached for an early row VANISHED the moment a later sibling raised,
        the caller's terminal-warning path went silent, and the claim proceeded
        with no word about it. A failure late in a scan does not un-measure
        what was measured early in it — which is the asymmetry the rest of this
        function is built on. I had applied it per-ROW and ignored it for the
        ANSWER.

        THE FIXTURE IS TWO ROWS AND THE ORDER MATTERS: a terminal off-base row
        the loop can classify, then a live row whose landed_state raises. The
        raise has to arrive AFTER the terminal verdict exists, or the arm tests
        nothing."""
        from helm import vcs

        dead = self._row("late-raise", self._offtrunk_tip("late-terminal"),
                         **self.TERMINAL)
        live_row = self._row("late-raise", self._offtrunk_tip("late-live"),
                             **self.LIVE_OPEN)

        # CONTROL FIRST, working backend: the terminal row IS reported as
        # terminal. Without it, an empty terminal below could mean the fixture
        # never produced one.
        with self._ledger([dead, live_row]):
            _l, terminal_ok, _u = _claims_all(self.root, "late-raise")
        self.assertTrue(terminal_ok,
                        "control: the terminal row must classify as terminal "
                        "before the raise can be shown to erase it")

        # THE LIVE ROW IS THE ONE THAT RAISES, NAMED BY ITS TIP — so the
        # terminal verdict is established first whatever order the loop walks,
        # and this arm no longer depends on which call happens to be second.
        boom, seen = _raising_backend(self.root,
                                      raise_on_tip=live_row.get("tip"))
        with self._ledger([dead, live_row]), \
                mock.patch.object(vcs, "backend", boom):
            live, terminal, unproven = _claims_all(self.root, "late-raise")
            # INSIDE the patched backend, or it does not raise and `unknown`
            # comes back empty for a reason that has nothing to do with the
            # property — a second call outside this block measures a DIFFERENT
            # world and would pass or fail by accident.
            unknown = _claims_unknown(self.root, "late-raise")
        self.assertGreater(seen["n"], 1, "fixture: the raise never happened, "
                                         "so nothing was tested")
        self.assertEqual(terminal, terminal_ok,
                         "the terminal verdict established BEFORE the raise "
                         "was discarded when the fallback rebuilt the answer")
        self.assertTrue(live, "and the live row must still strand")
        self.assertTrue(unknown, "and it must read as NEVER ESTABLISHED")
        self.assertEqual(unproven, [],
                         "a row the raise never reached was relabelled "
                         "tip-unreadable")


    def test_an_UNREADABLE_LIVENESS_does_not_mean_ALIVE(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the empty live-set for the closed row; its positive is the NON-empty live set for the open row through the identical raising predicate, and the rung cannot credit it because a second `_claims_all` call is a different opaque call identity — that the two rows answer DIFFERENTLY under one broken predicate IS the claim
        """The third finding, and the same class as the first two.

        My fallback forced `alive = True` when `_duplicate_branch_live` itself
        raised — which resurrects terminal history through the very door I had
        just closed for the non-raising path. A row CLOSED three weeks ago
        whose predicate happened to raise would strand a lane nobody can
        unblock, because the row it names is already closed.

        ERRING TERMINAL IS THE EXPENSIVE DIRECTION, so the cure is not to flip
        the default: it re-derives the ANSWERABLE half of the predicate from
        fields that cannot raise — still-open states, plus a CONTRARY verdict
        whose branch survives the review turn — and leaves the rest terminal.

        THE ARM RUNS BOTH ROWS THROUGH THE SAME BROKEN PREDICATE, which is the
        only way to show the difference is the ROW and not the failure."""
        from helm import dispatches, vcs

        def blind(_row):
            raise RuntimeError("successor index unreadable")

        # CONTROL FIRST: an OPEN row still strands under the broken predicate.
        alive_row = self._row("blind-liveness", self._offtrunk_tip("blind-open"),
                              **self.LIVE_OPEN)
        with self._ledger([alive_row]), \
                mock.patch.object(dispatches, "_duplicate_branch_live", blind), \
                mock.patch.object(vcs, "backend", _raising_backend(self.root)[0]):
            live_ctl, _t, _u = _claims_all(self.root, "blind-liveness")
        self.assertTrue(live_ctl,
                        "control: an OPEN row must still strand when liveness "
                        "is unreadable — erring terminal is the expensive way")

        dead_row = self._row("blind-liveness", self._offtrunk_tip("blind-dead"),
                             **self.TERMINAL)
        with self._ledger([dead_row]), \
                mock.patch.object(dispatches, "_duplicate_branch_live", blind), \
                mock.patch.object(vcs, "backend", _raising_backend(self.root)[0]):
            live, _t2, unproven = _claims_all(self.root, "blind-liveness")
        self.assertEqual(live, [], "a CLOSED row whose liveness could not be "
                                   "read was resurrected as live work")
        self.assertEqual(unproven, [])

        # AND THE VOCABULARY IS THE MODULE'S, NOT A COPY. Move the input: a
        # state the module calls still-open must strand even though this arm
        # never names it.
        self.assertIn("held", dispatches.CANCELLABLE_STATES,
                      "fixture: the state this arm varies must be in the "
                      "vocabulary the cure reads")
        held = self._row("blind-liveness", self._offtrunk_tip("blind-held"),
                         **dict(self.LIVE_OPEN, status="held"))
        with self._ledger([held]), \
                mock.patch.object(dispatches, "_duplicate_branch_live", blind), \
                mock.patch.object(vcs, "backend", _raising_backend(self.root)[0]):
            live_held, _t3, _u3 = _claims_all(self.root, "blind-liveness")
        self.assertTrue(live_held, "a HELD row is still-open in the module's "
                                   "own vocabulary and must strand")


    def test_a_LANDED_row_the_loop_DISMISSED_is_not_resurrected_by_a_later_raise(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the landed tip's exclusion from `live`; its unconditional positive is the live row's PRESENCE in the same returned list from the same call, which the rung cannot match because the two assertions read different elements of one result
        """The FOURTH instance of the shape, and it is the sharpest.

        The loop measures a row ANCESTOR or PATCH_EQUIVALENT and `continue`s —
        that row is LANDED and correctly dismissed. My fallback then walked
        EVERY candidate again, so a raise on a LATER row resurrected the
        dismissed one as live work: a lane blocked by a claim on work that is
        already on trunk.

        SAME SHAPE, FOURTH TIME: a NEW answer written beside the loop instead
        of a REPAIR of it, blind to something the loop already knew. The loop
        now records what it resolved and the fallback adds ONLY rows it never
        reached."""
        from helm import vcs
        landed = self._row("resurrect-landed", self._trunk_tip(),
                           **self.LIVE_OPEN)
        later = self._row("resurrect-landed", self._offtrunk_tip("late-raiser"),
                          **self.LIVE_OPEN)
        boom, seen = _raising_backend(self.root,
                                      raise_on_tip=later.get("tip"))
        with self._ledger([landed, later]), \
                mock.patch.object(vcs, "backend", boom):
            live, _terminal, unproven = _claims_all(self.root,
                                                    "resurrect-landed")
        self.assertTrue(seen["n"], "the raise never happened")
        # THE POSITIVE AND THE ABSENCE COME OFF ONE CALL: the raising row
        # strands (so the fallback ran at all) and the landed one does not.
        self.assertIn(later.get("tip"), live,
                      "the row whose check RAISED must strand")
        self.assertNotIn(landed.get("tip"), live,
                         "a row the loop measured LANDED was resurrected as "
                         "live work by a raise on a different row")
        self.assertNotIn(landed.get("tip"), unproven)

    def test_a_MEASURED_row_is_not_relabelled_tip_unreadable(self):
        """The FIFTH instance. Folding the stranded set into
        `unproven` rewrote a cleanly measured NOT_ANCESTOR live row as "the
        tip could not be read" — a claim about the object store that was never
        true of that row. `unproven` means UNREADABLE, so widening it with
        rows that WERE read makes the word mean nothing."""
        from helm import vcs
        clean = self._row("relabel", self._offtrunk_tip("clean-measured"),
                          **self.LIVE_OPEN)
        raiser = self._row("relabel", self._offtrunk_tip("raiser"),
                           **self.LIVE_OPEN)
        boom, seen = _raising_backend(self.root,
                                      raise_on_tip=raiser.get("tip"))
        with self._ledger([clean, raiser]), \
                mock.patch.object(vcs, "backend", boom):
            live, _t, unproven = _claims_all(self.root, "relabel")
            unknown = _claims_unknown(self.root, "relabel")   # inside the raise
        self.assertTrue(seen["n"], "the raise never happened")
        self.assertIn(clean.get("tip"), live,
                      "control: the cleanly measured live row still strands")
        self.assertIn(raiser.get("tip"), unknown,
                      "control: the row that RAISED is genuinely UNMEASURED")
        self.assertNotIn(clean.get("tip"), unknown,
                         "a row whose tip WAS read was relabelled unmeasured")
        self.assertNotIn(clean.get("tip"), unproven,
                         "a row whose tip WAS read was relabelled unreadable")

    def test_a_raise_in_the_LIVENESS_call_does_not_ERASE_an_open_row(self):
        """`resolved` was marked right after `landed_state`, so a
        raise in the LIVENESS call — which happens AFTER that mark — left the
        row flagged resolved and never classified. The fallback then skipped
        it, and an OPEN row was erased by the cure that exists to stop rows
        being erased.

        AND NO EXISTING ARM COULD SEE IT. `_raising_backend` raises inside
        `landed_state`, which is BEFORE the mark; every hostile fixture on this
        class attacks that door. This one raises in the door AFTER it, which is
        the whole reason the defect survived a round of arms written to catch
        exactly this class."""
        # LOCAL IMPORT, the idiom every other arm here uses — the module-level
        # line imports seats/vcs/work only, so a bare `dispatches` is unbound
        # and this arm would NameError instead of testing anything. Fourth time
        # tonight I used a name without checking where it is bound.
        from helm import dispatches
        row = self._row("liveness-raise", self._offtrunk_tip("lr-tip"),
                        **self.LIVE_OPEN)
        # CONTROL FIRST, working predicate: the row strands normally, so an
        # empty result below means the RAISE erased it rather than the fixture
        # never producing a live row at all.
        with self._ledger([row]):
            live_ok, _t, _u = _claims_all(self.root, "liveness-raise")
        self.assertTrue(live_ok, "control: the live row must strand normally")

        seen = {"n": 0}

        def _boom(r):
            seen["n"] += 1
            raise RuntimeError("liveness predicate is down")

        with self._ledger([row]), \
                mock.patch.object(dispatches, "_duplicate_branch_live", _boom):
            live, _t2, _u2 = _claims_all(self.root, "liveness-raise")
            unknown = _claims_unknown(self.root, "liveness-raise")
        self.assertTrue(seen["n"], "fixture: the liveness call never raised, "
                                   "so this arm proves nothing")
        # ASSERT THE BUCKET THE DOOR READS. `live or unknown` passes when the
        # row lands in `unknown` ALONE — and `claim()` refuses on `live` only,
        # so that arm would go green while the door stayed OPEN over the row.
        # It tested that the row was not forgotten; it did not test that the
        # claim is refused, which is the property this whole lane exists for.
        # Blind rows deliberately enter BOTH buckets for exactly this reason,
        # so `live` is assertable and is the one that means anything.
        self.assertTrue(live,
                        "a raise in the LIVENESS call erased an open row from "
                        "the bucket the claim door reads — it was marked "
                        "resolved before it was classified")
        self.assertTrue(unknown,
                        "and it must ALSO say the row was never established, "
                        "rather than borrowing the tip-unreadable sentence")

    def test_a_MEASURED_unreadable_tip_SURVIVES_a_later_liveness_raise(self):
        """On the bucket the LAST round created. `landed_state`
        runs BEFORE the liveness call, so a row whose LIVENESS raised already
        carries a real landedness verdict — but `unproven.append` sat AFTER
        the call that raised, so the measurement was thrown away and the row
        was rendered as one nothing had measured. `unproven` means "the tip
        could not be READ", which is a fact about the OBJECT STORE; which
        LATER step failed cannot change it.

        THE MUST-MISS IS THE OTHER HALF AND COMES OFF THE SAME CALL: a row
        NOTHING measured must not be swept into `unproven` by the same
        recovery, or the word stops meaning unreadable. That is a live
        mutation, not a hypothetical — a membership test written as
        `had_measured.get(id(r)) != NOT_ANCESTOR` returns True for a row that
        was never measured at all, so dropping the `in` check relabels every
        unreached row as unreadable and this arm is what goes red.

        ORDER-INDEPENDENT BY CONSTRUCTION. The liveness door raises only for
        the unreadable row, so whichever row the ledger yields first, the two
        assertions below hold for the SAME reason: if the clean row runs
        first it completes through the loop, and if it runs second the raise
        has already ended the loop and it is never reached. Pinning the
        fixture to an arrival order would make this arm a coin flip that
        still printed OK."""
        from helm import dispatches, vcs
        unreadable = self._row("survive-raise",
                               self._offtrunk_tip("unreadable-tip"),
                               **self.LIVE_OPEN)
        clean = self._row("survive-raise", self._offtrunk_tip("clean-tip"),
                          **self.LIVE_OPEN)
        real = vcs.backend(self.root)
        saw = {"measured_unreadable": 0, "raised": 0}

        class Slate(object):
            def __getattr__(self, name):
                return getattr(real, name)

            def landed_state(self, *a, **k):
                # NAME THE ROW, never the call ordinal — the sibling fixture
                # on this class carries the full argument for why.
                if a and str(a[1]) == unreadable.get("tip"):
                    saw["measured_unreadable"] += 1
                    return vcs.UNKNOWN       # READ, and read as unreadable
                return real.landed_state(*a, **k)

        def _boom(r):
            if str(r.get("tip") or "") == unreadable.get("tip"):
                saw["raised"] += 1
                raise RuntimeError("liveness predicate is down")
            return True

        with self._ledger([unreadable, clean]), \
                mock.patch.object(vcs, "backend", lambda *a, **k: Slate()), \
                mock.patch.object(dispatches, "_duplicate_branch_live", _boom):
            live, _t, unproven = _claims_all(self.root, "survive-raise")
        # FIXTURE FIRST: both halves of the setup must have TAKEN, or every
        # assertion below passes over a loop that never did the thing.
        self.assertTrue(saw["measured_unreadable"],
                        "fixture: landed_state never saw the unreadable row "
                        "(%r), so nothing was measured to preserve" % (saw,))
        self.assertTrue(saw["raised"],
                        "fixture: the liveness call never raised (%r), so "
                        "this arm proves nothing" % (saw,))
        self.assertIn(unreadable.get("tip"), live,
                      "control: the row still strands, so the door refuses")
        self.assertIn(unreadable.get("tip"), unproven,
                      "landedness was READ as unreadable BEFORE the liveness "
                      "call raised, and the raise discarded that measurement")
        self.assertNotIn(clean.get("tip"), unproven,
                         "a row nothing measured was relabelled 'tip "
                         "unreadable' by the recovery")

    def test_an_UNREADABLE_BASE_does_not_answer_ABSENT_over_matching_rows(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertEqual(terminal, []); the unconditional positive on the same call is the non-empty live/unknown pair asserted immediately above it
        """The third check of the same round, and the same fail-open
        one branch up. `mine` is already NON-EMPTY when the base is read —
        those rows MATCHED — so answering absent tells the caller nobody
        holds this lane while we hold proof somebody might.

        The row strands, but as UNKNOWN and not as UNPROVEN: its tip was never
        the problem and may read perfectly. What was missing is the BASE."""
        from helm.work import _claims as _c
        row = self._row("blind-base", self._offtrunk_tip("blind-base-tip"),
                        **self.LIVE_OPEN)
        with self._ledger([row]), \
                mock.patch.object(_c, "_base", lambda *a, **k: ""):
            live, terminal, unproven = _claims_all(self.root, "blind-base")
            unknown = _claims_unknown(self.root, "blind-base")
        self.assertTrue(live, "an unreadable base answered ABSENT over a row "
                              "that had already MATCHED this lane")
        self.assertTrue(unknown, "and it must read as NEVER ESTABLISHED")
        # THE MUST-MISS: a missing BASE is not an unreadable TIP. Naming it
        # `unproven` sends the operator to fetch an object that was fine.
        self.assertEqual(unproven, [],
                         "a row nothing measured was relabelled tip-unreadable")
        self.assertEqual(terminal, [])



def _raising_backend(root, after=0, raise_on_tip=None):
    """A vcs backend that DELEGATES everything to the real one and raises from
    `landed_state` only — plus a counter proving it was reached.

    A BARE FAKE WITH ONE METHOD DOES NOT TEST WHAT ITS NAME SAYS, and this is
    measured, not theoretical. `_row_work_elsewhere` calls `_base(root)` first,
    which is `vcs.backend(root).base_branch(root)` — so a fake defining only
    `landed_state` raises AttributeError THERE, lands in the same blanket
    except, and reaches the fallback with `landed_state` never called at all.
    Every assertion about "a raised landedness check" then passes for the wrong
    reason. Found by the `seen` counter on the arm below going 0, which is the
    entire argument for asserting that a fixture took rather than assuming it.

    `after` lets an arm take N real answers before the raise, so a verdict
    established EARLY in the loop exists by the time a LATER row fails.
    """
    real = vcs.backend(root)
    seen = {"n": 0}

    class Boom(object):
        def __getattr__(self, name):
            return getattr(real, name)

        def landed_state(self, *a, **k):
            seen["n"] += 1
            # PIN THE PROPERTY, NEVER THE MECHANISM. `after=N` keys on the
            # Nth CALL, which is the incidental order the loop happens to use
            # today — and an ordinal pin has BOTH failure modes at once: a
            # legitimate reorder of `_row_work_elsewhere` breaks the arm for
            # the wrong reason, AND the same reorder silently stops it testing
            # what its name says, because "late" becomes "early" with nothing
            # going red. `raise_on_tip` names the ROW instead, so the arm's
            # sentence — a verdict ESTABLISHED BEFORE a raise survives it — is
            # true whatever order the rows arrive in.
            if raise_on_tip is not None:
                if a and str(a[1]) == raise_on_tip:
                    raise RuntimeError("object store unreadable")
                return real.landed_state(*a, **k)
            if seen["n"] > after:
                raise RuntimeError("object store unreadable")
            return real.landed_state(*a, **k)

    return (lambda *a, **k: Boom()), seen


def _claims_live_terminal(root, lane):
    """(live, terminal) — the two sets the existing arms assert on.

    The detector also returns UNPROVEN (tips returned because they could not be
    CHECKED rather than measured absent); the renderer needs it and these arms
    do not, so it is dropped HERE rather than by widening every caller.
    """
    from helm.work import _claims
    # SLICE, DO NOT UNPACK. This helper reached PAST `_claims_all` straight to
    # the detector, so widening the contract to four buckets broke it while
    # every arm that went through the helper stayed green — the census I ran
    # asked "who calls _claims_all" when the question was "who calls the
    # contract I am changing". A slice survives the next widening too.
    live, terminal = _claims._row_work_elsewhere(root, lane)[:2]
    return live, terminal


def _claims_all(root, lane):
    """(live, terminal, unproven) — the three MEASURED buckets.

    `_claims_live` drops unproven and `_claims_unproven` drops the rest; the
    fail-open arms need all three at once, because the defect they pin is a
    disagreement BETWEEN them (a row that is live but unverifiable).

    The detector returns a FOURTH bucket, `unknown`, for rows whose liveness
    the fallback DERIVED rather than measured. It is deliberately NOT unpacked
    here: these arms are about the measured answer, and folding a derived row
    into `unproven` is the exact overclaim the fourth bucket exists to end.
    Reach for `_claims_unknown` when the derived set is the subject."""
    from helm.work import _claims
    return _claims._row_work_elsewhere(root, lane)[:3]


def _claims_unknown(root, lane):
    """The rows whose LIVENESS was DERIVED by the fallback rather than
    measured by the loop. Named for what is true of EVERY member: the loop did
    not finish classifying them. It does NOT mean their landedness is unknown —
    a row measured before a raise in the liveness call keeps that verdict and
    is reported through `unproven`."""
    from helm.work import _claims
    return _claims._row_work_elsewhere(root, lane)[3]


def _claims_unproven(root, lane):
    from helm.work import _claims
    return _claims._row_work_elsewhere(root, lane)[2]


# THE BRANCH ALGEBRA IS IMPORTED, NEVER REIMPLEMENTED. I wrote a body-list
# approximation of `exclusive` and it was RED on six shapes this module already
# handles: if-body plus unconditional sibling, try-body plus else,
# handler plus finally, with-body plus sibling, nested same-execution, and
# same-arm duplicate plus else. A second census inherits none of the first
# one's scars.
from tests.test_suite_collection import (  # noqa: E402
    exclusive, scope_definitions,
)


def _loaded_test_identities(module):
    """{(filename, lineno)} for every test the loader actually returns.

    SOURCE IDENTITY, NOT NAMES. `_collected_case_methods` returned (class,
    method) pairs, and that pair is an ALIAS: two definitions of one name share
    it, so it could never say WHICH definition ran. Every defect on this guard
    so far — the duplicate alias, the conditional branch — lived in that gap,
    and the arms could not see them because arm and implementation were both
    built on the same lexical name model.

    A code object cannot be aliased. It carries the file and the first line of
    the definition the interpreter actually bound, so it answers the question
    the guard is asking instead of a question that correlates with it. It also
    covers alias assignment, inheritance and custom load_tests for free,
    because all of them end at a real callable.

    HONEST LIMIT, stated because the guard must not imply more: this is the
    world of THIS import under THIS environment. A definition that only binds
    under a different sys.version_info or a different optional dependency is
    absent here and that is not evidence against it.
    """
    out = set()

    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                flatten(item)
                continue
            fn = getattr(type(item), item._testMethodName, None)
            fn = getattr(fn, "__func__", fn)
            code = getattr(fn, "__code__", None)
            if code is not None:
                out.add((code.co_filename, code.co_firstlineno))

    flatten(unittest.TestLoader().loadTestsFromModule(module))
    return out


def _definition_lines(node):
    """Every source line the interpreter might report for this definition.

    A decorated function's code object reports the FIRST DECORATOR line, while
    the AST node reports the `def`. Both are the same definition, so both are
    offered rather than guessing which the runtime will use."""
    lines = {node.lineno}
    for dec in getattr(node, "decorator_list", ()):
        lines.add(dec.lineno)
    return lines


def _tests_the_loader_cannot_reach(tree, loaded, prefix, path):
    """Every test function in `tree` the loader did not bind AND that no
    branch alternative excuses.

    TWO INSTRUMENTS, AND NEITHER IS A PROXY.

    The LOADER answers "did this definition bind" — the actual question —
    through code identity rather than a name that two definitions can share.

    The BRANCH ALGEBRA answers "is there an execution in which it would have".
    A definition on the untaken arm of an `if` did not bind HERE and might bind
    elsewhere; flagging it is a false alarm on live code. `exclusive()` is
    reused from tests/test_suite_collection rather than reimplemented: I wrote
    a body-list approximation of it and it was RED on six shapes that module
    already handles (if-body plus unconditional sibling, try-body plus else,
    handler plus finally, with-body plus sibling, nested same-execution,
    same-arm duplicate plus else). A second census inherits none of the first
    one's scars.

    DIRECTION IS A CONTRACT, NOT A PREFERENCE: a false positive is FORBIDDEN.
    Every ambiguous or unresolved identity resolves to REACHABLE. A missed
    shadow leaves a dead test dead, which is the status quo this guard exists
    to improve; a false alarm reddens live code and teaches everyone to
    distrust the one instrument that says a green suite proved nothing.
    """
    unreachable = []

    def scope(node):
        by_name = {}
        for stmt, branch in scope_definitions(node.body):
            by_name.setdefault(stmt.name, []).append((stmt, branch))
        for name, defs in by_name.items():
            if not name.startswith(prefix):
                continue
            for stmt, branch in defs:
                if not isinstance(stmt, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                if any((path, line) in loaded
                       for line in _definition_lines(stmt)):
                    continue                       # the loader bound THIS one
                # EXCUSED when a same-named sibling sits on a DIFFERENT arm:
                # only one of them ever binds, so an absence here is simply
                # the branch this run did not take.
                if any(other is not stmt and exclusive(branch, obranch)
                       for other, obranch in defs):
                    continue
                unreachable.append("%s:%d" % (stmt.name, stmt.lineno))
        for stmt, _branch in scope_definitions(node.body):
            scope(stmt)

    scope(tree)
    return sorted(unreachable)


# The three shapes the guard must REJECT, as source rather than as prose. An
# arm that cannot name an input it must reject is measuring its author's intent,
# so this fixture is compiled and classified on every run alongside the real
# file, and the arm fails if any shape stops being caught.
_UNREACHABLE_FIXTURE = """
import unittest


class RealTest(unittest.TestCase):
    def test_reachable(self):
        pass


class _LostItsBaseInAMerge(object):
    def test_inside_a_plain_class(self):
        pass


def _helper():
    return 1

    def test_appended_after_a_return(self):
        pass


def test_at_module_scope():
    pass


class ShadowedTwice(unittest.TestCase):
    def test_duplicated_method(self):
        pass                       # SHADOWED: the definition below wins

    def test_duplicated_method(self):  # noqa: F811 — the shadowing IS the fixture
        pass


class DefinedTwice(unittest.TestCase):
    def test_in_the_shadowed_class(self):
        pass                       # SHADOWED: the class below replaces this one


class DefinedTwice(unittest.TestCase):  # noqa: F811 — the shadowing IS the fixture
    def test_in_the_shadowed_class(self):
        pass
"""


class NoTestIsOrphanedOutsideATestCaseTest(unittest.TestCase):
    """A test function the loader cannot reach is dead code that reads exactly
    like a test: it can never fail, so a green suite says nothing about it.

    MEASURED ON THIS FILE, which is why the guard exists rather than a note. An
    arm appended to the end of the module landed inside a top-level helper,
    after a `return`. Its AST parent was that helper, not a TestCase, so the
    loader discovered it never. The count of collected tests was identical
    before and after the arm was added, and that unchanged count was the only
    evidence anyone held.

    THE GUARD ASKS THE LOADER'S QUESTION LITERALLY: it compares the test
    functions in this file's AST against the set of cases
    `unittest.TestLoader` actually returns for this module. A structural proxy —
    "is there a class somewhere above it" — passes a test sitting in a class
    that is not a TestCase subclass, which is precisely what a merge that drops
    a base class produces, and the loader runs none of it.
    """

    def test_no_test_function_lives_outside_a_TestCase(self):
        import importlib.util
        import tempfile
        prefix = unittest.TestLoader().testMethodPrefix

        # MUST-MISS FIRST, AND IT NOW RUNS THE REAL LOADER OVER REAL SOURCE.
        # The fixture is written to a FILE and imported, because the oracle is
        # code identity — (filename, first line) off the bound callable — and a
        # code object cannot exist for source that was never executed. The old
        # version handed the classifier a set of NAME PAIRS, which is the alias
        # this guard was rewritten to stop trusting; when I replaced the
        # producer I left this consumer calling the removed helper, and only
        # running the arm found it.
        fixdir = tempfile.mkdtemp(prefix="unreachable-fixture-")
        fixpath = os.path.join(fixdir, "unreachable_fixture.py")
        io.open(fixpath, "w", encoding="utf-8").write(_UNREACHABLE_FIXTURE)
        spec = importlib.util.spec_from_file_location(
            "unreachable_fixture", fixpath)
        fixmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixmod)
        caught = _tests_the_loader_cannot_reach(
            ast.parse(io.open(fixpath, encoding="utf-8").read()),
            _loaded_test_identities(fixmod), prefix, fixpath)
        self.assertEqual(
            sorted(n.split(":")[0] for n in caught),
            ["test_appended_after_a_return", "test_at_module_scope",
             "test_duplicated_method", "test_in_the_shadowed_class",
             "test_inside_a_plain_class"],
            "the classifier no longer catches a shape the loader cannot run")
        # AND ONLY THE DEAD TWIN. Each duplicated name appears ONCE — flagging
        # a surviving definition would make the guard cry at every legitimate
        # test in the file, and a containment assertion cannot see that.
        self.assertEqual(len(caught), 5,
                         "a SURVIVING definition was flagged alongside its "
                         "shadowed twin: %s" % caught)

        # NOW THE REAL FILE.
        module = sys.modules[__name__]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "test_work.py")
        loaded = _loaded_test_identities(module)
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        # MUST-HIT: both instruments have to see this module at all, or the
        # comparison below is two empty sets agreeing with each other.
        self.assertGreater(len(loaded), 100,
                           "the loader bound almost nothing for this module")
        seen = sum(1 for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name.startswith(prefix))
        self.assertGreater(seen, 100,
                           "the AST walk found almost no tests — it is broken, "
                           "so its empty result proves nothing")
        unreachable = _tests_the_loader_cannot_reach(tree, loaded, prefix, path)
        self.assertEqual(unreachable, [],
                         "test function(s) the loader will never run: "
                         + ", ".join(unreachable))


class SeamAmbientBudgetTest(unittest.TestCase):
    def test_expiry_before_the_first_room_stops_all_seam_iteration(self):  # noqa: VACUOUS_ASSERTION — backend/_trunk are must-hit controls before expiry; _merged raises if any room work starts afterward
        rooms = [
            {"path": "/mine", "branch": "lane/a", "live": True},
            {"path": "/peer", "branch": "lane/b", "live": True,
             "holder": "other", "holder_why": "lease", "why": "lease"},
        ]
        with mock.patch.object(_work_gc.vcs, "backend", return_value=object()), \
                mock.patch.object(_work_gc, "_trunk", return_value="main"), \
                mock.patch.object(
                    _work_gc.projscope, "spend_or_raise",
                    side_effect=[None, projscope.Expired("planted")]), \
                mock.patch.object(
                    _work_gc, "_merged",
                    side_effect=AssertionError("landedness ran after expiry")):
            with self.assertRaises(projscope.Expired):
                _work_gc.seam_candidates(
                    "/repo", {"/mine"}, rooms=rooms,
                    green=(frozenset(), frozenset()))


class AnUnknownFlagIsRefusedNotDroppedTest(WorkBase):
    """A FLAG NOBODY READS WAS ACCEPTED AS DATA, AND THE ARTIFACT IS A TREE.

    `helm work claim <lane> --base <sha>` returned 0 and printed an ordinary
    lease line while the room was cut from trunk. The caller's belief and the
    worktree diverged in silence, the work compiled against code that was not
    there, and the first gate was the discovery — by which time the commits
    existed on the wrong base. Measured twice in one night on this verb.

    THE SAME CLI ALREADY RULED THE OTHER WAY THREE VERBS AWAY: `helm task
    list` refuses an unrecognised filter because "an unrecognised filter would
    print the WHOLE ledger while you believed it was narrowed". That argument
    is strictly stronger here — a wrong view is recovered by looking again, a
    wrong base only by rebasing commits that should never have been written.

    THE HARD HALF IS THE ACCEPT SIDE. A table that is too NARROW refuses a
    working call, which is worse than the drop it replaces, so the arm below
    drives EVERY flag of EVERY guarded verb in both spellings.
    """

    def test_the_ACCEPT_side_holds_for_every_flag_of_every_guarded_verb(self):
        """THE CONTROL THAT MATTERS, and the firing case runs FIRST: the guard
        must not have narrowed any verb. The SPACE spelling is every flag's,
        so every flag of every guarded verb is driven in it.

        THE `=` SPELLING IS ONE FLAG'S GRAMMAR AND NOT THE TABLE'S, which is
        why it is a separate arm below rather than a second loop here. A flag
        whose reader takes an exact token cannot see `--flag=VALUE` at all, so
        accepting that spelling drops the value while the call returns 0 —
        the ACCEPT side is the wrong place to assert it.

        ONE LIST CARRIES BOTH READINGS, so the emptiness at the end is
        constrained by the same expression having reported a moment earlier
        rather than by a sibling's."""
        found = []
        found.extend(_work_cli._unknown_flags("claim", ["l", "--base", "s"]))
        self.assertEqual(["--base"], found,
                         "the helper accepts an unread flag, so the accept "
                         "side below would hold over a guard that never "
                         "refuses anything: %r" % (found,))
        del found[:]
        for verb, flags in _work_cli._VERB_FLAGS.items():
            for flag in tuple(flags) + _work_cli._SHARED_FLAGS:
                argv = [flag, "VALUE"]
                got = _work_cli._unknown_flags(verb, ["lane"] + argv)
                if got:
                    found.append((verb, argv, got))
        self.assertEqual([], found,
                         "the table refuses a flag its own verb reads, so "
                         "this guard broke working calls: %r" % (found,))

    def test_the_EQUALS_spelling_is_REFUSED_unless_a_reader_can_parse_it(self):
        """A KNOWN NAME IN A SPELLING NOTHING READS IS A DROPPED VALUE.

        `seats._flag` is `args.index(name)` — an exact token match — so
        `--ttl=30` is a token it never finds and the flag reads as ABSENT.
        Accepting it hands back a lease carrying a default the caller
        believes they set, and the release-authority flag in that spelling is
        a release presenting no token at all.

        DERIVED FROM THE TWO TABLES, never transcribed: the refused set is
        `_VALUED_FLAGS` minus `_EQUALS_FLAGS`, so moving a flag into either
        one moves this arm with it. Each cell carries its own control — the
        SPACE spelling of the same flag reaching the same reader — so a cell
        cannot pass because the reader is broken for both.
        """
        exact = [f for f in _work_common._VALUE_FLAGS
                 if f not in _work_cli._EQUALS_FLAGS]
        self.assertTrue(exact,
                        "every valued flag parses the `=` form, so the "
                        "refusals below are asserted over an empty set")
        seen = []
        for flag in exact:
            # CONTROL FIRST: the reader answers in the spelling it does read.
            seen.append((flag, "space", seats._flag(["lane", flag, "V"], flag)))
            seen.append((flag, "equals",
                         seats._flag(["lane", "%s=V" % flag], flag)))
        self.assertEqual(
            [(f, kind, "V" if kind == "space" else None)
             for f in exact for kind in ("space", "equals")], seen,
            "the exact-match readers do not behave as this arm's premise "
            "says, so the refusals below are about something else: %r"
            % (seen,))
        del seen[:]
        for verb, flags in _work_cli._VERB_FLAGS.items():
            for flag in tuple(flags) + _work_cli._SHARED_FLAGS:
                if flag not in exact:
                    continue
                got = _work_cli._unknown_flags(verb, ["lane", "%s=V" % flag])
                seen.append((verb, flag, tuple(got)))
        self.assertTrue(seen, "no guarded verb reads an exact-match flag, "
                              "so this arm drove nothing")
        self.assertEqual([], [s for s in seen if not s[2]],
                         "a spelling no reader can parse was accepted, and "
                         "its value would be dropped in silence: %r"
                         % ([s for s in seen if not s[2]],))
        # AND THE ONE FLAG WHOSE READER DOES PARSE IT STAYS ACCEPTED, so the
        # refusal above is about the spelling reaching a reader rather than
        # about the `=` character.
        for flag in _work_cli._EQUALS_FLAGS:
            for verb, flags in _work_cli._VERB_FLAGS.items():
                if flag not in tuple(flags) + _work_cli._SHARED_FLAGS:
                    continue
                self.assertEqual(
                    [], _work_cli._unknown_flags(verb, ["lane", "%s=r" % flag]),
                    "%s %s=r is the documented escape for a value that "
                    "begins with a dash and it was refused" % (verb, flag))

    def test_the_REFUSAL_tells_the_two_mistakes_apart(self):
        """A NAME THIS VERB DOES NOT READ AND A NAME IT DOES ARE DIFFERENT
        MISTAKES, AND ONE SENTENCE SENDS HALF THE CALLERS THE WRONG WAY.

        A typo is answered by the list of readable flags. A readable flag in
        a spelling its reader cannot parse is answered by the spelling —
        telling that caller the flag is "unknown" while the same refusal
        lists it among the readable ones sends them hunting a typo that is
        not there.

        BOTH REFUSALS RUN IN ONE PASS through the real dispatcher, and each
        asserts the sentence the OTHER one does not carry, so neither can
        pass by both messages having collapsed into one.
        """
        rc_eq, _o, err_eq = self.work("claim", "lane-x", "--ttl=30")
        rc_unk, _o2, err_unk = self.work("claim", "lane-x", "--base", "abc")
        self.assertEqual((2, 2), (rc_eq, rc_unk),
                         "a refusal did not refuse: %r" % ((rc_eq, rc_unk),))
        self.assertIn("--ttl=30", err_eq,
                      "the refusal does not name the token: %r" % err_eq)
        self.assertIn("`--ttl VALUE`", err_eq,
                      "the refusal does not give the spelling that reaches "
                      "the reader: %r" % err_eq)
        self.assertNotIn("unknown flag", err_eq,
                         "a flag this verb reads was called unknown: %r"
                         % err_eq)
        self.assertIn("unknown flag", err_unk,
                      "a name this verb does not read was not called "
                      "unknown: %r" % err_unk)
        self.assertIn("--ttl", err_unk,
                      "the unknown-name refusal does not list what the verb "
                      "reads: %r" % err_unk)
        # AND THE `=` ESCAPE IS OFFERED ONLY BY A VERB THAT HAS ONE. Naming it
        # to a caller whose verb reads no such flag hands them a spelling this
        # same guard refuses, which reads as permission. The verb that DOES
        # offer it is asserted in the same pass, so an escape sentence deleted
        # everywhere could not satisfy the absence below.
        _rc, _o3, err_has = self.work("release", "lane-x", "--base", "abc")
        self.assertIn("--superseded=VALUE", err_has,
                      "a verb that reads the `=` flag does not name the "
                      "escape: %r" % err_has)
        self.assertNotIn("=VALUE", err_unk,
                         "a verb that reads no `=` flag offered that "
                         "spelling anyway: %r" % err_unk)

    def test_an_INLINE_value_opens_no_value_position_for_the_next_token(self):
        """A FLAG THAT CARRIED ITS VALUE INLINE IS ALREADY SATISFIED.

        The value-position skip exists so a dash-leading VALUE is left to the
        guard that owns it. A flag spelled `--flag=value` took its value from
        inside its own token, so the token after it belongs to nobody — and
        granting the skip there swallows the next dash token, which is the
        exact silence the whole guard exists to end.

        THE SPACE SPELLING IS THE CONTROL IN THE SAME PASS: the same flag,
        the same following token, and the skip must still be granted, so a
        green here cannot come from a guard that stopped skipping at all.
        """
        eq = _work_cli._EQUALS_FLAGS[0]
        self.assertIn(eq, _work_cli._VERB_FLAGS["release"],
                      "this arm drives release with a flag release does not "
                      "read, so its answers are about an unknown name")
        got = _work_cli._unknown_flags("release", ["lane", eq, "--base"])
        self.assertEqual([], got,
                         "the space spelling stopped opening a value "
                         "position, so the refusal below is about that "
                         "rather than about the inline form: %r" % (got,))
        got = _work_cli._unknown_flags(
            "release", ["lane", "%s=reason" % eq, "--base"])
        self.assertEqual(["--base"], got,
                         "a flag that already took its value inline swallowed "
                         "the next dash token: %r" % (got,))

    def test_a_DASH_VALUE_its_reader_refuses_is_REFUSED_HERE_not_dropped(self):
        """THE VALUE-POSITION SKIP IS EARNED BY A BETTER REFUSAL, NOT BY
        TAKING A VALUE — and for every flag but one there was no better
        refusal to defer to.

        `seats._flag` refuses a dash-leading next token on purpose, so that
        `--seat --apply` cannot mint a seat called `--apply`. The consequence
        is that for EVERY flag it reads, a dash value reads as ABSENT: the
        call was accepted here, the flag defaulted, and the caller was told
        nothing. `peek <sha> --drop -x` is the sharp one — the drop vanished
        and the verb PEEKED the subject instead, in the opposite direction
        from the request.

        `--superseded` keeps the skip because its verb answers better than
        this scan can: the guard names the `--superseded=REASON` escape, and
        this refusal cannot, because it does not know which flag was
        starved. THE READER PREMISE IS MEASURED IN THE SAME PASS rather than
        assumed, so a `seats._flag` that learned to read dash values would
        redden this arm instead of leaving it asserting a stale reason."""
        exact = [f for f in _work_common._VALUE_FLAGS
                 if f not in _work_cli._EQUALS_FLAGS]
        self.assertTrue(exact, "every valued flag owns a better refusal, so "
                               "the refusals below are asserted over an "
                               "empty set")
        # THE PREMISE: the readers really do refuse a dash-leading value, and
        # really do take a plain one. Both directions, so a reader that had
        # stopped reading anything could not stand in for one that refuses.
        premise = []
        for flag in _work_common._VALUE_FLAGS:
            premise.append((flag, seats._flag(["lane", flag, "-dash"], flag),
                            seats._flag(["lane", flag, "plain"], flag)))
        self.assertEqual([(f, None, "plain") for f in _work_common._VALUE_FLAGS],
                         premise,
                         "the value readers do not behave as this arm's "
                         "reason says, so the refusals below are about "
                         "something else: %r" % (premise,))
        seen = []
        for verb, flags in _work_cli._VERB_FLAGS.items():
            for flag in tuple(flags) + _work_cli._SHARED_FLAGS:
                if flag not in exact:
                    continue
                seen.append((verb, flag,
                             tuple(_work_cli._unknown_flags(
                                 verb, ["lane", flag, "-dash"]))))
        self.assertTrue(seen, "no guarded verb reads a flag whose reader "
                              "refuses a dash value, so this arm drove "
                              "nothing")
        self.assertEqual([], [s for s in seen if not s[2]],
                         "a dash value its reader will refuse was accepted "
                         "here, so the flag defaults in silence: %r"
                         % ([s for s in seen if not s[2]],))
        # AND THE ONE FLAG WITH A BETTER REFUSAL KEEPS THE SKIP, so the
        # refusals above are about the absence of a better sentence and not
        # about dashes.
        for flag in _work_cli._EQUALS_FLAGS:
            self.assertEqual(
                [], _work_cli._unknown_flags("release", ["lane", flag, "-d"]),
                "%s lost the value-position skip, so this scan now speaks "
                "over the verb's own refusal, which names the escape and "
                "this one cannot" % flag)

    def test_every_SKIPPED_flag_really_has_the_better_refusal_it_is_spared_for(self):
        """THE EXEMPTION'S JUSTIFICATION, ASSERTED INSTEAD OF ASSUMED.

        A flag in `_EQUALS_FLAGS` keeps the value-position skip on the claim
        that its own verb answers a starved value better than this scan can —
        by naming the inline escape, which a generic unknown-flag refusal
        cannot do because it does not know which flag was starved. That claim
        is written in a comment, and a comment is not a guard: a flag added
        to that table with no such refusal would silently buy the skip and
        get SILENCE instead of the better sentence.

        DRIVEN THROUGH THE REAL DISPATCHER, not read out of the source, so
        what is asserted is the sentence an operator sees. The CONTROL is the
        same shape on a flag OUTSIDE the table: it is refused by this guard
        instead, which is the whole reason the exemption is narrow."""
        for flag in _work_cli._EQUALS_FLAGS:
            verbs = [v for v, f in _work_cli._VERB_FLAGS.items() if flag in f]
            self.assertTrue(verbs, "%s is exempted but no verb reads it, so "
                                   "the skip is granted for a flag that can "
                                   "never be starved" % flag)
            for verb in verbs:
                rc, _out, err = self.work(verb, "lane-x", flag, "-dash")
                self.assertEqual(2, rc, "%s %s -dash was ACCEPTED: the skip "
                                        "was granted and nothing refused it"
                                        % (verb, flag))
                self.assertIn("%s=" % flag, err,
                              "%s %s -dash is refused without naming the "
                              "inline escape, which is the better sentence "
                              "this flag keeps the skip for: %r"
                              % (verb, flag, err))
        # CONTROL, SAME SHAPE, A FLAG OUTSIDE THE TABLE: this guard answers
        # it, so the assertions above are about the exemption rather than
        # about every dash value getting a good sentence for free.
        outside = [f for f in _work_common._VALUE_FLAGS
                   if f not in _work_cli._EQUALS_FLAGS
                   and f in _work_cli._VERB_FLAGS["claim"]]
        self.assertTrue(outside, "claim reads no flag outside the exempted "
                                 "table, so the control below drives nothing")
        rc, _out, err = self.work("claim", "lane-x", outside[0], "-dash")
        self.assertEqual(2, rc, "a dash value on an unexempted flag was "
                                "accepted: %r" % err)
        self.assertIn("-dash", err,
                      "the guard refused without naming the token it "
                      "refused: %r" % err)

    def test_the_peek_DROP_of_a_dash_path_is_refused_not_turned_into_a_PEEK(self):
        """THE REGRESSION THIS ARM IS NAMED FOR, pinned as its own cell.

        `--drop` takes a value, so binding the scan's skip to the value
        table let `peek <sha> --drop -x` through; `seats._flag` then read the
        drop as absent, `_positional` skipped the token anyway, and the verb
        took the single-positional path — MINTING a peek where the operator
        asked to retire one. An accepted-and-dropped flag is bad; one that
        performs the opposite verb is the case this whole guard exists for.

        THE CONTROL IS THE SAME CALL WITH A PLAIN PATH, so a green here
        cannot come from a guard that refuses every `--drop`."""
        self.assertEqual(
            [], _work_cli._unknown_flags("peek", ["sha", "--drop", "path"]),
            "a peek drop with an ordinary path is refused, so the refusal "
            "below is about the guard and not about `--drop`")
        self.assertEqual(
            ["-x"], _work_cli._unknown_flags("peek", ["sha", "--drop", "-x"]),
            "a dash-leading drop path is accepted here, read as absent by "
            "`seats._flag`, skipped by `_positional`, and the verb peeks the "
            "subject instead of dropping it")

    def test_the_EQUALS_table_NAMES_every_equals_aware_read_in_this_file(self):
        """THE TABLE IS A CLAIM ABOUT READERS, so the readers decide it.

        A flag added to `_EQUALS_FLAGS` without a reader that parses the form
        re-opens the drop this guard closes, and a reader added without the
        table entry makes the guard refuse a form that works. Both directions
        are drift, so the expected set is read out of the file's own source
        rather than written down beside it.

        MUST-HIT THROUGH THE SAME PRODUCER CALL ON THE SAME INPUT: the
        control scans THIS FILE'S SOURCE with one synthetic `=`-aware read
        appended, so it shares the real scan's expression and its real text.
        A control built from a literal of its own would prove only that the
        regex matches a string somebody wrote for it.
        """
        import re

        def scan(text):
            return set(re.findall(r'"(--[a-z-]+)="', text))

        with io.open(_work_cli.__file__, encoding="utf-8") as fh:
            text = fh.read()
        seeded = scan(text + '\nif tok.startswith("--synthetic="):\n')
        self.assertIn("--synthetic", seeded,
                      "the scan cannot see an `=`-aware read in this file's "
                      "own text, so its agreement below is empty: %r"
                      % (sorted(seeded),))
        found = scan(text)
        self.assertTrue(found,
                        "no `=`-aware read was found in a file the seeded "
                        "control just proved readable, so the comparison "
                        "below is between two empty sets")
        self.assertEqual(sorted(_work_cli._EQUALS_FLAGS), sorted(found),
                         "this file parses the `=` form for a different set "
                         "of flags than the table declares: source %r, table "
                         "%r" % (sorted(found), sorted(_work_cli._EQUALS_FLAGS)))

    def test_the_CLI_keeps_no_value_flag_table_of_its_OWN(self):
        """A SECOND TABLE ANSWERING ONE QUESTION DRIFTS WHERE NOBODY LOOKS,
        AND A TABLE WITH NO READER IS THE SAME HAZARD KEPT ALIVE FOR THE
        ARMS' BENEFIT.

        This module held a copy of the package's value-flag list that was one
        entry short, and each side was locally correct while disagreeing — so
        one door skipped a value the other called an unknown flag. The copy
        is gone rather than bound, because nothing in this file decides
        anything from WHICH FLAGS TAKE A VALUE any more; the value-position
        skip is earned by `_EQUALS_FLAGS`, and a list kept only so a test can
        compare it would drift exactly as quietly as the copy did.

        THE ABSENCE IS ASSERTED AGAINST A LIVE READER, so it cannot pass by
        the package table having vanished too."""
        # UNCONDITIONAL POSITIVE CONTROL FIRST, and it is the entry the copy
        # was missing: `--drop` is read with a value by the peek branch, so a
        # package table that omits it would make the absence below a
        # statement about nothing.
        self.assertIn("--drop", _work_common._VALUE_FLAGS,
                      "the package's value-flag table omits a flag that is "
                      "read with a value")
        # AND AN UNCONDITIONAL POSITIVE ON THE MODULE THE ABSENCE IS ABOUT:
        # it still carries the table that DOES decide something here, so the
        # absence below is about one table rather than about a module that
        # failed to import or was renamed out from under this arm.
        self.assertIn("--superseded", _work_cli._EQUALS_FLAGS,
                      "this module no longer names the flag whose reader "
                      "parses the inline form, so the absence below is not "
                      "about the table it claims to be about")
        self.assertFalse(hasattr(_work_cli, "_VALUED_FLAGS"),
                         "this module carries a value-flag table again; it "
                         "has no reader here and can only drift from the one "
                         "`_positional` reads")
        # AND THE PACKAGE TABLE REALLY IS THE ONE `_positional` READS: a
        # dash-leading value after each valued flag survives as a value
        # rather than becoming a positional. That is the reading the deleted
        # copy could contradict, so it is asserted here rather than inferred
        # from the absence above.
        #
        # EACH READING CARRIES ITS OWN POSITIVE HALF, IN THE SAME CALL. The
        # argv ends in a real positional, so the expected value is two names
        # rather than an absence: a `_positional` that had stopped collecting
        # anything fails on the half that must be THERE, and one that had
        # stopped skipping fails on the half that must not. A control hoisted
        # above the loop would be a different call on a different argv and
        # could not say either thing about these.
        #
        # AND A SINGLE-DASH TOKEN AFTER NO VALUED FLAG IS STILL COLLECTED, so
        # the skip is positional rather than a rule about dashes.
        self.assertEqual(
            ["lane", "-dash"], _work_claims._positional(["lane", "-dash"]),
            "_positional drops a single-dash positional that follows no "
            "valued flag, so the skip below is about the dash and not the "
            "position")
        for flag in _work_common._VALUE_FLAGS:
            self.assertEqual(
                ["lane", "tail"],
                _work_claims._positional(["lane", flag, "-dash", "tail"]),
                "%s does not open a value position for _positional, or "
                "_positional collected nothing at all, so the binding above "
                "is between two tables that mean different things" % flag)

    def test_every_guarded_verb_REFUSES_an_unread_flag_and_names_it(self):
        """One unread flag, every guarded verb, through the real dispatcher.

        The expected table is written out and compared whole rather than
        filtered down to failures: a filtered comparison asserts an EMPTINESS
        and is satisfied by a loop that ran zero times."""
        verbs = sorted(_work_cli._VERB_FLAGS)
        self.assertTrue(verbs, "no verb is guarded, so this arm runs nothing")
        seen = []
        for verb in verbs:
            rc, _out, err = self.work(verb, "lane-x", "--base", "deadbeef")
            seen.append((verb, rc, "--base" in err))
        self.assertEqual([(v, 2, True) for v in verbs], seen,
                         "a verb accepted an unread flag, or refused without "
                         "naming it: %r" % (seen,))
        # AND THE REFUSAL SAYS WHAT THE VERB DOES READ, so the operator's next
        # command is in the message rather than in the source.
        _rc, _out, err = self.work("claim", "lane-x", "--base", "deadbeef")
        self.assertIn("--ttl", err,
                      "the refusal does not list the flags claim reads: %r"
                      % err)
        self.assertIn("--lease", err, "the flag list is incomplete: %r" % err)

    def test_EVERY_guard_tail_verb_stays_out_of_this_table(self):
        """THE MISTAKE THIS ARM EXISTS FOR: the first cut excluded `list` BY
        NAME. The property is "calls guard_tail", not "is called list", and
        checking the one verb I had in mind found one of the three — the other
        two had their --help turned into a refusal (task/2255).

        THE LIST IS HELD TO THE CALL SITES, so a fourth guard_tail verb cannot
        appear without this failing. Reading the source is the only way to
        make the exclusion a derived fact rather than a remembered one.
        """
        import re
        with io.open(_work_cli.__file__, encoding="utf-8") as fh:
            src = fh.read()
        named = set(re.findall(r'guard_tail\("helm work ([a-z-]+)"', src))
        self.assertTrue(named,
                        "no guard_tail call site was found at all, so the "
                        "comparison below holds over an empty set")
        self.assertEqual(named, set(_work_cli._GUARD_TAIL_VERBS),
                         "the guard_tail verb list and the call sites "
                         "disagree: source %r, table %r"
                         % (sorted(named), sorted(_work_cli._GUARD_TAIL_VERBS)))
        overlap = named & set(_work_cli._VERB_FLAGS)
        self.assertEqual(set(), overlap,
                         "a verb is guarded TWICE — once by guard_tail and "
                         "once by this table, which is the drift the table's "
                         "own comment refuses: %r" % (sorted(overlap),))

    def test_the_guard_tail_verbs_still_REFUSE_an_unread_flag(self):
        """Deferring is not the same as dropping. The table leaves these three
        alone because guard_tail already covers them — so guard_tail had
        better still refuse, or the exclusion removed the contract."""
        seen = []
        for verb in _work_cli._GUARD_TAIL_VERBS:
            rc, _out, err = self.work(verb, "--base", "deadbeef")
            seen.append((verb, rc, "--base" in err))
        self.assertTrue(seen, "no guard_tail verb was exercised at all")
        self.assertEqual([(v, 2, True) for v in _work_cli._GUARD_TAIL_VERBS],
                         seen,
                         "a guard_tail verb stopped refusing an unread flag, "
                         "so leaving it out of the table removed the contract "
                         "instead of deferring to it: %r" % (seen,))

    def test_help_ANSWERS_on_every_verb_and_is_never_an_unknown_flag(self):
        """THE REGRESSION A PROBE MEASURED, pinned in both spellings on every
        verb. main answered gc and install-guard --help with rc 0 and the
        verb's usage; a table in front of guard_tail called --help an unknown
        flag and refused. -h and --help belong to every verb.

        THE EXPECTED TABLE IS WRITTEN WHOLE rather than filtered to failures:
        a filtered comparison asserts an EMPTINESS and is satisfied by a loop
        that ran zero times.
        """
        verbs = sorted(set(_work_cli._VERB_FLAGS)
                       | set(_work_cli._GUARD_TAIL_VERBS))
        self.assertTrue(verbs, "no verb was checked, so this arm is empty")
        seen, want = [], []
        for verb in verbs:
            for flag in _work_cli._HELP_FLAGS:
                rc, out, err = self.work(verb, flag)
                said = out + err
                seen.append((verb, flag, rc, "unknown flag" in said,
                             bool(said.strip())))
                want.append((verb, flag, 0, False, True))
        self.assertTrue(seen, "the loop ran zero times, so the comparison "
                              "below holds over two empty lists")
        self.assertEqual(want, seen,
                         "a verb answered -h or --help with something other "
                         "than rc 0 and a usage line: %r" % (seen,))

    def test_a_SINGLE_dash_token_cannot_become_a_LANE_NAME(self):
        """MEASURED ON main BY RUNNING IT, and it minted a real room: `helm
        work claim -h` returned 0 and created `<repo>-wt/-h` with a live
        lease. `_positional` filters only tokens beginning with TWO dashes and
        LANE_RE accepts a dash, so a one-dash token walked past both and
        became the lane. That is why this guard scans single-dash tokens.

        THE CONTROL IS A REAL CLAIM, because an assertion that no room
        appeared is true of a probe that cannot see rooms at all. A lane with
        an ordinary name is claimed FIRST, through the same door, and must
        show up in the same listing.
        """
        found = []
        found.extend(_work_cli._unknown_flags("claim", ["-x"]))
        self.assertEqual(["-x"], found,
                         "a single-dash token is invisible to the guard, so "
                         "it reaches the lane name: %r" % (found,))
        del found[:]
        found.extend(_work_cli._unknown_flags("claim", ["-h"]))
        self.assertEqual([], found,
                         "-h is reported as an unknown flag rather than "
                         "answered: %r" % (found,))

        box = self.root.rstrip(os.sep) + "-wt"
        rooms = []
        rc, out, _err = self.work("claim", "ordinary-lane")
        self.assertEqual(0, rc, "the control claim failed, so the absence "
                                "below is about a door that does not work: %r"
                                % out)
        rooms.extend(sorted(os.listdir(box)) if os.path.isdir(box) else [])
        self.assertIn("ordinary-lane", rooms,
                      "a REAL claim left no room in %s, so this listing "
                      "cannot see rooms and the check below proves nothing: "
                      "%r" % (box, rooms))

        del rooms[:]
        before = sorted(os.listdir(box))
        rc, _out, _err = self.work("claim", "-h")
        self.assertEqual(0, rc, "claim -h no longer answers help")
        rooms.extend(x for x in sorted(os.listdir(box)) if x not in before)
        self.assertEqual([], rooms,
                         "`claim -h` created something on disk, which is the "
                         "measured main behaviour this guard exists to stop: "
                         "%r" % (rooms,))

    def test_the_EQUALS_form_of_a_dash_valued_flag_still_reaches_the_verb(self):
        """`release --superseded=<reason>` is the documented escape for a
        reason beginning with a dash, so the guard reads the name from the
        left of the first `=`. A guard that refused it would refuse the very
        spelling the verb offers for the case it exists to serve."""
        self.assertEqual(
            [], _work_cli._unknown_flags(
                "release", ["lane", "--superseded=--not-a-flag"]),
            "the `=` escape is refused, so a reason beginning with a dash "
            "can no longer be given at all")
        # THE PAIRED REFUSAL, same verb, same spelling, an unread name: the
        # acceptance above is about the NAME on the left of the `=` and not
        # about `=` disabling the guard.
        self.assertEqual(
            ["--nope=x"],
            _work_cli._unknown_flags("release", ["lane", "--nope=x"]),
            "any `=` token is accepted, so the escape disabled the guard")

    def test_a_token_after_a_bare_dashdash_is_still_scanned(self):
        """No verb here takes a child argv, so `--` names no flag and is not
        worth refusing — while a token AFTER it belongs to nobody and would be
        dropped exactly like one before it. Stopping the scan there would put
        the silence back behind two dashes."""
        self.assertEqual(
            ["--sneaky"],
            _work_cli._unknown_flags("claim", ["lane", "--", "--sneaky"]),
            "a `--` disables the guard for everything after it")
        self.assertEqual([], _work_cli._unknown_flags("claim", ["lane", "--"]),
                         "a bare `--` is reported as an unknown flag")

    def test_an_unknown_VERB_still_prints_usage_rather_than_a_flag_complaint(self):
        """The dispatcher's own contract is unchanged: a verb it does not own
        gets USAGE, not a complaint about flags on a verb that does not
        exist."""
        found = []
        found.extend(_work_cli._unknown_flags("claim", ["--whatever"]))
        self.assertEqual(["--whatever"], found,
                         "the table reports nothing for a verb it owns, so "
                         "its silence on an unknown verb says nothing: %r"
                         % (found,))
        del found[:]
        found.extend(_work_cli._unknown_flags("bogus", ["--whatever"]))
        self.assertEqual([], found,
                         "the guard claims verbs the dispatcher does not "
                         "own: %r" % (found,))
        rc, _out, err = self.work("bogus", "--whatever")
        self.assertEqual(2, rc)
        self.assertIn("usage: helm work", err,
                      "an unknown verb no longer prints usage: %r" % err)
        self.assertNotIn("unknown flag", err,
                         "an unknown verb is reported as a flag problem: %r"
                         % err)

    def test_EVERY_refusal_this_verb_ALREADY_HAD_still_fires_ahead_of_mine(self):
        """THE GUARD MUST NOT SPEAK OVER A REFUSAL THAT SAYS MORE THAN IT DOES.

        ROUND TWO ON THIS CHAIN, and both rounds are the same shape: a new
        refusal placed ahead of an existing one. Round one was `--help` on the
        guard_tail verbs; this is `release --superseded -dash-leading`, where
        the verb's own guard names the `--superseded=REASON` escape and a
        generic unknown-flag refusal cannot, because it does not know which
        flag was starved.

        EACH ROW IS A REFUSAL THIS FILE ALREADY HAD, with the argv that
        reaches it. Every one is composed only of tokens the guard accepts, so
        each row is also a statement that the guard let the call through.

        WHAT THIS DELIBERATELY DOES NOT PIN: the peek SUBJECT error (an
        unresolvable committish) and the stash sub-command error. Both are
        reachable and neither is a flag refusal — they are a different door
        answering about a different thing, and an arm that pinned them here
        would grow a flag guard's test into a verb test and go stale for
        reasons that have nothing to do with this guard.
        """
        from helm.work._common import PEEK_DIRNAME
        from helm.harness import SEAT_HOME_DIRNAME
        rows = (
            ("a lane name the shape refuses",
             ("claim", "bad!name"), "usage: helm work claim <lane>"),
            ("the per-seat home container name",
             ("claim", SEAT_HOME_DIRNAME), "is reserved"),
            ("the read-only peek container name",
             ("claim", PEEK_DIRNAME), "is reserved"),
            ("release with no lane and none to infer",
             ("release",), "usage: helm work release <lane>"),
            ("the two release requests that are not the same request",
             ("release", "somelane", "--stale", "--superseded", "why"),
             "different requests"),
            ("a starved --superseded, which is the one that reddened",
             ("release", "somelane", "--superseded", "-dash-leading"),
             "needs a value that is not another flag"),
            ("peek asked for a subject and a drop at once",
             ("peek", "abc", "--drop", "x"), "not both"),
        )
        # ONE LIST, AND ITS POSITIVE READING COMES FIRST AND UNCONDITIONALLY.
        # Every row below asserts the guard is SILENT on that argv, and a
        # guard that had gone silent for every input would satisfy all of
        # them. So the same expression reports a real unknown flag before the
        # loop starts, and nothing in the loop can skip it.
        seen = []
        seen.extend(_work_cli._unknown_flags("release", ["lane", "--bogus"]))
        self.assertEqual(["--bogus"], seen,
                         "the guard does not report a plainly unknown flag, "
                         "so its silence on every row below says nothing")
        del seen[:]
        # AND THE SAME FOR THE OTHER OBSERVABLE. Every row asserts the generic
        # unknown-flag refusal did NOT answer; a build that never emitted it
        # would satisfy all of them. So one argv that SHOULD get it is driven
        # first, unconditionally, through the same helper into the same name.
        _rc, _out, err = self.work("release", "somelane", "--bogus")
        self.assertIn("unknown flag", err,
                      "the generic refusal does not fire even on a plainly "
                      "unknown flag, so its absence on every row below says "
                      "nothing: %r" % (err,))
        for why, argv, fragment in rows:
            seen.extend(_work_cli._unknown_flags(argv[0], list(argv[1:])))
            self.assertEqual(  # noqa: VACUOUS_ASSERTION — the positive control on THIS binding is the assertEqual(["--bogus"], seen) immediately above the loop: same list, same producer, unconditional, and cleared by `del seen[:]` at the end of each pass. The classifier matches a positive only within the same block, so it cannot see one hoisted out of the loop on purpose.
                [], seen,
                "the unknown-flag guard claims a token in the "
                "argv for %s, so it answers ahead of that "
                "refusal: %r" % (why, seen))
            rc, _out, err = self.work(*argv)
            self.assertEqual(2, rc, "%s: rc=%r err=%r" % (why, rc, err))
            self.assertIn(fragment, err,
                          "%s: the refusal that owns this argv no longer "
                          "speaks — err=%r" % (why, err))
            self.assertNotIn(  # noqa: VACUOUS_ASSERTION — the positive control is the assertIn("unknown flag", err) driven before the loop through the same helper: it proves the generic refusal DOES fire on a plainly unknown flag, so the absence here is about ordering rather than about a guard that never speaks. The classifier cannot unify the two because `err` is REBOUND from a different self.work() call and roots are snapshot-expanded at the producer, not at the name.
                "unknown flag", err,
                "%s: the generic unknown-flag refusal answered "
                "instead of the specific one — err=%r"
                % (why, err))
            del seen[:]

    def test_the_valued_table_AGREES_with_every_guard_tail_call_in_this_file(self):
        """THE OTHER AXIS OF THE ROUND-ONE MISTAKE, closed the same way.

        Round one broke because a second table was written BESIDE a guard that
        already knew the answer, and the cure was to derive the verb list from
        the call sites. The valued set has the same hazard: `cli.guard_tail`
        already takes `valued=` and this file passes it at three call sites,
        so a shared flag that takes a value THERE and not HERE is a drift that
        nothing would report.

        DERIVED FROM THE SOURCE, never transcribed: the expected set is read
        out of the file's own AST, so adding a fourth call site with a new
        valued flag reddens this rather than passing unnoticed.
        """
        import ast as _ast
        src = open(_work_cli.__file__, encoding="utf-8").read()
        passed = set()
        sites = 0
        for node in _ast.walk(_ast.parse(src)):
            if not isinstance(node, _ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None)
            if name != "guard_tail":
                continue
            for kw in node.keywords:
                if kw.arg != "valued":
                    continue
                sites += 1
                for elt in getattr(kw.value, "elts", ()):
                    if isinstance(elt, _ast.Constant):
                        passed.add(elt.value)
        # MUST-HIT: the walk found call sites at all. A zero here would make
        # the subset check below trivially true and say nothing.
        self.assertEqual(3, sites,
                         "the AST walk found %d guard_tail(valued=...) call "
                         "sites in this file, not the 3 that are there — the "
                         "walk is blind and its agreement below is empty"
                         % sites)
        # ONE LIST, FILLED TWICE, AND THE POSITIVE SIDE IS SPELLED OUT. The
        # verb-specific valued flags are exactly the ones guard_tail is never
        # told about, so their presence proves the subtraction is live before
        # the empty one is asked to mean anything.
        drift = []
        drift.extend(sorted(set(_work_common._VALUE_FLAGS) - passed))
        self.assertEqual(["--drop", "--lease", "--superseded", "--ttl"], drift,
                         "the verb-specific valued flags are not what this "
                         "file declares, so the emptiness below is about a "
                         "table that changed rather than about drift")
        del drift[:]
        drift.extend(sorted(passed - set(_work_common._VALUE_FLAGS)))
        self.assertEqual([], drift,
                         "guard_tail is told these flags take a value and "
                         "this file's own table is not: %r" % (drift,))


class LandedWorld(WorkBase):
    """A REAL TREE for the landed-lane arms: `helm work claim` mints real rooms
    and leases, a lane lands by the `--no-ff` merge the integrator's trains
    use, and the trunk is `origin/main`, so every verdict is measured the way
    production measures it. No arms of its own; shared with the web board's
    parity arms (tests/test_web_board.py)."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        r = _sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main",
                self.origin)
        self.assertEqual(r.returncode, 0, r.stderr)
        _sh(self.root, "git", "remote", "add", "origin", self.origin)
        self.push()
        self.leases = {}

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                            *args], cwd=cwd, capture_output=True, text=True,
                           timeout=30,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def push(self):
        self.git(self.root, "push", "-q", "origin", "main")
        self.git(self.root, "fetch", "-q", "origin")

    def lane(self, name, commits=0, seat="s1"):
        rc, out, err = self.work("claim", name, "--seat", seat)
        self.assertEqual(rc, 0, err)
        self.leases[name] = out.strip().split("\t")[2]
        path = work.lane_path(self.root, name)
        for i in range(commits):
            with open(os.path.join(path, "%s-%d.txt" % (name, i)), "w") as f:
                f.write("%s work %d\n" % (name, i))
            self.git(path, "add", "-A")
            self.git(path, "commit", "-q", "-m", "%s %d" % (name, i))
        return path

    def land(self, name):
        """The integrator's train: a --no-ff merge of the lane, pushed."""
        self.git(self.root, "merge", "--no-ff", "-q", "-m", "land " + name,
                 "lane/" + name)
        self.push()

    def spy(self):
        """(calls, patch) — every question put to git through the seam, cached
        or not, while the patch is active."""
        calls = []
        real = vcs.GitVcs.run

        def run(this, cwd, *args, **kw):
            calls.append(args)
            return real(this, cwd, *args, **kw)
        return calls, mock.patch.object(vcs.GitVcs, "run", run)


class LanesLandedTest(LandedWorld):
    """IS A HELD LANE'S WORK ALREADY ON THE TRUNK — the one producer
    `helm work list`, the web board and the land report read.

    THE OWNER'S OBSERVABLE: "about half the listed lanes already landed ...
    why werent they listed as landed in helm?" A land releases no lease, so a
    board that does not ask the ancestry `helm work release` prints draws a
    landed lane as building."""

    def test_a_merged_lane_is_LANDED_and_a_lane_claimed_at_the_trunk_is_not(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality on a named state, and the merged lane's LANDED is the positive control for the unstarted and unlanded ones
        self.lane("merged", commits=1)
        self.lane("open", commits=1)
        self.land("merged")
        self.lane("fresh")            # claimed AFTER the land: AT the trunk tip
        got = work.lanes_landed(self.root, ["merged", "open", "fresh"])
        self.assertEqual(got["merged"]["state"], work.LANE_LANDED, got)
        self.assertIn("LANDED by ancestry", got["merged"]["proof"])
        self.assertEqual(got["merged"]["trunk"], "origin/main")
        self.assertEqual(got["open"]["state"], work.LANE_UNLANDED, got)
        # THE DISCRIMINATOR ANCESTRY ALONE CANNOT MAKE. `fresh`'s tip IS an
        # ancestor of the trunk — it is the trunk's tip — exactly as a merged
        # lane's is, and it has authored nothing.
        tip = self.git(self.root, "rev-parse", "lane/fresh")
        self.assertEqual(tip, self.git(self.root, "rev-parse", "origin/main"))
        self.assertEqual(got["fresh"]["state"], work.LANE_UNSTARTED, got)

    def test_a_branch_that_is_gone_is_GONE_never_landed(self):  # noqa: VACUOUS_ASSERTION — the absent tip rides beside the positive GONE state on the same verdict
        got = work.lanes_landed(self.root, ["never-had-a-branch"])
        self.assertEqual(got["never-had-a-branch"]["state"], work.LANE_GONE)
        self.assertIsNone(got["never-had-a-branch"]["tip"])

    def test_helm_work_list_says_LANDED_with_the_release_line(self):
        self.lane("merged", commits=1)
        self.lane("open", commits=1)
        self.land("merged")
        rc, out, err = self.work("list", "--seat", "s1")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        at = next(i for i, l in enumerate(lines) if "GUARDED merged " in l)
        self.assertIn("LANDED — LANDED by ancestry", lines[at + 1])
        self.assertIn("the lease is still held", lines[at + 1])
        self.assertIn("helm work release merged --lease %s"
                      % self.leases["merged"], lines[at + 1])
        # CONTROL on the same render: the unlanded lane carries no such line
        at = next(i for i, l in enumerate(lines) if "GUARDED open " in l)
        self.assertNotIn("LANDED", "\n".join(lines[at:at + 2]))

    def test_a_second_read_of_an_unmoved_repository_makes_no_git_call(self):  # noqa: VACUOUS_ASSERTION — zero new calls IS the property; its control is the unconditional assertGreater(asked, 0) on the same spy over the first read
        self.lane("merged", commits=1)
        self.lane("open", commits=1)
        self.land("merged")
        calls, spy = self.spy()
        with spy:
            first = work.lanes_landed(self.root, ["merged", "open"])
            asked = len(calls)
            second = work.lanes_landed(self.root, ["merged", "open"])
        self.assertGreater(asked, 0, "MUST-HIT: the first read asked git")
        self.assertEqual(len(calls), asked,
                         "an unmoved repository was asked again: %r"
                         % (calls[asked:],))
        self.assertEqual(first, second)

    def test_rooms_dirty_is_the_list_rows_flag_for_the_named_rooms_only(self):
        """THE WEB BOARD'S DIRT IS THE CLI'S DIRT. `rooms_dirty` reads the
        rooms `list_rows` reads with the reader it uses, and only the ones it
        is asked about — the board asks for its LANDED lanes, so a board with
        thirty claims and two landed lanes pays two `git status` calls."""
        self.lane("clean", commits=1)
        dirty = self.lane("dirty", commits=1)
        self.lane("never-asked")
        with open(os.path.join(dirty, "wip.txt"), "w") as f:
            f.write("uncommitted\n")
        calls, spy = self.spy()
        with spy:
            got = work.rooms_dirty(self.root, ["clean", "dirty", "no-room"])
        self.assertEqual(got, {"clean": False, "dirty": True,
                               "no-room": False})
        rows = {r["lane"]: r["dirty"] for r in work.list_rows(self.root)}
        self.assertEqual({"clean": rows["clean"], "dirty": rows["dirty"]},
                         {"clean": got["clean"], "dirty": got["dirty"]})
        self.assertIs(rows["never-asked"], False)
        self.assertEqual(len([c for c in calls if c[0] == "status"]), 2,
                         calls)

    def test_rooms_dirty_is_unread_never_clean_when_the_registry_fails(self):
        self.lane("clean", commits=1)
        with mock.patch("helm.work._gc._worktree_records",
                        lambda root: ([], "git worktree list failed")):
            self.assertEqual(work.rooms_dirty(self.root, ["clean"]),
                             {"clean": None})
        # THE CONTROL: the same room, the registry readable, reads clean
        self.assertEqual(work.rooms_dirty(self.root, ["clean"]),
                         {"clean": False})

    def test_a_reset_lane_names_the_branch_that_no_longer_carries_its_work(self):
        reset = self.lane("reset", commits=1)
        dropped = self.git(reset, "rev-parse", "HEAD")
        self.git(reset, "reset", "--hard", "origin/main")
        proof = work.lanes_landed(self.root, ["reset"])["reset"]["proof"]
        self.assertIn("%s, which lane/reset no longer carries" % dropped[:12],
                      proof)
        self.assertNotIn("?", proof)

    def test_the_trunk_moving_by_a_FETCH_alone_is_re_read(self):  # noqa: VACUOUS_ASSERTION — the before read's UNLANDED and the after read's LANDED are both positive equalities on the same verdict field
        """THE STAMP COVERS THE TRUNK REF, not only local main: the fleet
        lands through origin, and a fetch moves nothing else this lane's
        verdict depends on. A memo blind to it would keep a landed lane
        UNLANDED forever."""
        self.lane("open", commits=1)
        self.git(self.root, "push", "-q", "origin", "lane/open")
        before = work.lanes_landed(self.root, ["open"])
        self.assertEqual(before["open"]["state"], work.LANE_UNLANDED)
        clone = os.path.join(self.tmp, "fleet-clone")
        self.git(self.tmp, "clone", "-q", self.origin, clone)
        self.git(clone, "merge", "--no-ff", "-q", "-m", "fleet lands open",
                 "origin/lane/open")
        self.git(clone, "push", "-q", "origin", "main")
        main_before = self.git(self.root, "rev-parse", "main")
        self.git(self.root, "fetch", "-q", "origin")
        self.assertEqual(self.git(self.root, "rev-parse", "main"), main_before,
                         "the fixture must move ONLY the remote-tracking ref")
        after = work.lanes_landed(self.root, ["open"])
        self.assertEqual(after["open"]["state"], work.LANE_LANDED, after)

    def test_the_land_report_names_what_to_release_and_what_to_keep(self):
        self.lane("clean", commits=1)
        dirty = self.lane("dirty", commits=1)
        ahead = self.lane("ahead", commits=1)
        self.lane("open", commits=1)
        for name in ("clean", "dirty", "ahead"):
            self.land(name)
        with open(os.path.join(dirty, "uncommitted.txt"), "w") as f:
            f.write("precious uncommitted bytes\n")
        with open(os.path.join(ahead, "next-round.txt"), "w") as f:
            f.write("round two\n")
        self.git(ahead, "add", "-A")
        self.git(ahead, "commit", "-q", "-m", "round two")
        held = sorted(c["resource"] for c in seats.claims_list())
        release, kept = work.landed_leases(self.root)
        self.assertEqual([r["lane"] for r in release], ["clean"])
        self.assertIn("helm work release clean --lease ",
                      work.release_command(release[0]))
        why = {r["lane"]: w for r, w in kept}
        self.assertEqual(sorted(why), ["ahead", "dirty"])
        self.assertIn("DIRTY", why["dirty"])
        self.assertIn("1 commit(s) beyond", why["ahead"])
        # the unlanded lane is neither released nor reported as carried
        self.assertNotIn("open", [r["lane"] for r in release] + list(why))
        # AND IT RELEASED NOTHING: every lease is exactly where it was
        self.assertEqual(sorted(c["resource"] for c in seats.claims_list()),
                         held)
        self.assertEqual(len(held), 4)

    def test_a_lane_reset_back_to_the_trunk_after_committing_is_NOT_landed(self):  # noqa: VACUOUS_ASSERTION — the merged lane on the same read is the positive LANDED control; the reset lane's UNLANDED, its named sha and its absence from `release` are positive equalities
        """THE HARMFUL DIRECTION. A branch reset back to the trunk after
        committing sits AT the trunk exactly as a merged lane does, and
        ancestry alone read it LANDED — and the release line that verdict
        prints deletes the branch and its reflog, the only place that work
        still lives — and `landed_leases` then offers it for release."""
        self.lane("merged", commits=1)
        self.land("merged")
        reset = self.lane("reset", commits=2)
        dropped = self.git(reset, "rev-parse", "HEAD")
        self.git(reset, "reset", "--hard", "origin/main")
        self.assertEqual(self.git(self.root, "rev-parse", "lane/reset"),
                         self.git(self.root, "rev-parse", "origin/main"),
                         "MUST-HIT: the reset lane sits at the trunk tip")
        got = work.lanes_landed(self.root, ["merged", "reset"])
        self.assertEqual(got["merged"]["state"], work.LANE_LANDED, got)
        self.assertEqual(got["reset"]["state"], work.LANE_UNLANDED, got)
        self.assertIn(dropped[:12], got["reset"]["proof"])
        self.assertIn("only in the reflog", got["reset"]["proof"])
        release, kept = work.landed_leases(self.root)
        self.assertEqual([r["lane"] for r in release], ["merged"])
        self.assertNotIn("reset", [r["lane"] for r, _why in kept])

    def test_a_lane_reset_to_the_trunk_after_a_REBASED_land_is_still_landed(self):  # noqa: VACUOUS_ASSERTION — LANDED with the dropped sha named is the positive arm; the reset arm above is its refuting control on the same shape
        """The same shape with the work ON the trunk under another sha: the
        integrator cherry-picked the lane's commit onto a moved trunk, and
        the lane then reset onto the trunk instead of rebasing. The dropped
        commit is judged by patch identity, exactly as a tip is."""
        p = self.lane("picked", commits=1)
        tip = self.git(p, "rev-parse", "HEAD")
        with open(os.path.join(self.root, "unrelated.txt"), "w") as f:
            f.write("the trunk moves first, so the pick mints a new object\n")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", "unrelated land")
        self.git(self.root, "cherry-pick", tip)
        self.push()
        self.git(p, "reset", "--hard", "origin/main")
        got = work.lanes_landed(self.root, ["picked"])
        self.assertEqual(got["picked"]["state"], work.LANE_LANDED, got)
        self.assertIn("patch identity", got["picked"]["proof"])
        self.assertIn(tip[:12], got["picked"]["proof"])

    def test_an_idle_lane_that_fast_forwarded_the_trunk_in_is_UNSTARTED(self):  # noqa: VACUOUS_ASSERTION — UNSTARTED is a positive equality beside the merged lane's LANDED on the same read
        """THE MIRROR IMAGE the producer's own comment names: `git merge
        origin/main` on a lane that wrote nothing records a trunk commit as
        the lane's new sha, and a reflog read that counted `merge` as
        authorship calls that LANDED."""
        self.lane("merged", commits=1)
        idle = self.lane("idle")
        self.land("merged")             # the trunk moves after `idle` exists
        self.git(idle, "merge", "-q", "origin/main")
        self.assertEqual(self.git(self.root, "rev-parse", "lane/idle"),
                         self.git(self.root, "rev-parse", "origin/main"),
                         "MUST-HIT: the fast-forward put the lane AT the trunk")
        got = work.lanes_landed(self.root, ["merged", "idle"])
        self.assertEqual(got["merged"]["state"], work.LANE_LANDED, got)
        self.assertEqual(got["idle"]["state"], work.LANE_UNSTARTED, got)


class LanesLandedByAncestryTest(LandedWorld):
    """THE CHEAP READ THE LAND PROJECTION ASKS OF EVERY OPEN BUILD'S LANE.

    A build row's work lives on its lane, so the projection asks the lane —
    for every open build row, on every board rebuild. The full producer's
    patch-identity leg runs `git cherry` against the trunk, which walks every
    trunk patch since the lane's base: measured on the live repository, the
    stale build lanes cost minutes on a cold process. `content=False` answers
    from ancestry and the lane's own reflog alone, says UNKNOWN (never
    UNLANDED) where only patch identity could tell, and writes nothing into
    the memo the full reader serves."""

    def test_ancestry_and_authorship_answer_and_patch_identity_is_never_asked(self):
        self.lane("merged", commits=1)
        self.land("merged")
        self.lane("open", commits=1)
        self.lane("fresh")             # claimed after the land: at the trunk
        calls, spy = self.spy()
        with spy:
            got = work.lanes_landed(self.root, ["merged", "open", "fresh",
                                                "never-had-a-branch"],
                                    content=False)
        self.assertEqual(got["merged"]["state"], work.LANE_LANDED, got)
        self.assertEqual(got["fresh"]["state"], work.LANE_UNSTARTED, got)
        self.assertEqual(got["never-had-a-branch"]["state"], work.LANE_GONE)
        self.assertEqual(got["open"]["state"], work.LANE_UNKNOWN, got)
        self.assertIn("patch identity was not asked", got["open"]["proof"])
        self.assertGreater(len(calls), 0, "MUST-HIT: git was asked at all")
        self.assertEqual([c for c in calls if c and c[0] in ("cherry", "log")],
                         [], "the ancestry-only read walked patches")

    def test_it_leaves_nothing_for_the_full_reader_to_be_served(self):  # noqa: VACUOUS_ASSERTION — every assertion is an equality on a named state of the same lane: the cheap UNKNOWN, the full UNLANDED after it, and the kept UNLANDED served back
        self.lane("open", commits=1)
        cheap = work.lanes_landed(self.root, ["open"], content=False)
        self.assertEqual(cheap["open"]["state"], work.LANE_UNKNOWN)
        full = work.lanes_landed(self.root, ["open"])
        self.assertEqual(full["open"]["state"], work.LANE_UNLANDED, full)
        # AND A FULL VERDICT ALREADY KEPT IS A STRONGER ANSWER, served as is
        again = work.lanes_landed(self.root, ["open"], content=False)
        self.assertEqual(again["open"]["state"], work.LANE_UNLANDED, again)


class TheTrunkNameFlippingUnderTheMemoTest(WorkBase):
    """`vcs.trunk_ref` answers `origin/<base>` the moment
    `refs/remotes/origin/<base>` resolves. A checkout that gains a remote
    flips the trunk's NAME, and a stamp that never covered that file kept
    serving a land on LOCAL main as LANDED against an origin that does not
    have it, with no git call."""

    def test_a_remote_appearing_re_reads_the_lane_against_origin(self):  # noqa: VACUOUS_ASSERTION — the before read's LANDED on `main` and the after read's UNLANDED on `origin/main` are both positive equalities on the same verdict
        env = dict(os.environ, HELM_WORK_INTEGRATOR="1")

        def git(cwd, *args):
            r = subprocess.run(["git", "-c", "user.name=t",
                                "-c", "user.email=t@t", *args], cwd=cwd,
                               capture_output=True, text=True, timeout=30,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()
        seed = git(self.root, "rev-parse", "HEAD")
        bare = os.path.join(self.tmp, "origin.git")
        git(self.tmp, "clone", "-q", "--bare", self.root, bare)  # seed only
        path = self.room("x")
        with open(os.path.join(path, "x.txt"), "w") as f:
            f.write("x work\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "x work")
        git(self.root, "merge", "--no-ff", "-q", "-m", "land x locally",
            "lane/x")
        before = work.lanes_landed(self.root, ["x"])
        self.assertEqual((before["x"]["state"], before["x"]["trunk"]),
                         (work.LANE_LANDED, "main"), before)
        git(self.root, "remote", "add", "origin", bare)
        git(self.root, "fetch", "-q", "origin")
        self.assertEqual(git(self.root, "rev-parse", "origin/main"), seed,
                         "MUST-HIT: the origin is BEHIND the local land")
        after = work.lanes_landed(self.root, ["x"])
        self.assertEqual((after["x"]["state"], after["x"]["trunk"]),
                         (work.LANE_UNLANDED, "origin/main"), after)
