#!/usr/bin/env python3
"""The lane-discipline pre-commit rung, script-driven and END TO END.

THE GAP THIS PROVES CLOSED (2026-08-03, measured): two commits went straight
to local `main` in the shared checkout — no lane, no gate, no verdict, no fold
— and two OTHER seats' lanes were then cut from that main and silently
inherited the unreviewed change. Nothing in the estate could see it: the
reference-transaction guard admits an ordinary fast-forward of `main`, and no
content rung asks WHERE a commit is being made.

BOTH DIRECTIONS OR IT IS NOT A GUARD. A rung tested only on what it blocks is
how you ship one that blocks everything, and the thing it must never block is
THE FOLD — the one correct path onto trunk. So the admit direction is tested
harder than the refuse direction here: a clean `git merge --no-ff` fold, a
CONFLICTED fold finished by `git commit`, an ordinary lane-worktree commit,
a detached shared checkout, an off-base branch, a solo repo, and the root
commit all have to land through the REAL installed hook.

Hermetic: git global/system config nulled, HELM_HOME sandboxed, landlock off,
synthetic needles planted so the composed hook's never-track leg has a real
(quiet) configuration.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import lane_discipline, pk, seats, wiring, work  # noqa: E402
from helm.work import _guard  # noqa: E402

MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "helm", "lane_discipline.py")

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
            "HELM_PRIVATE_NEEDLES", "HELM_NEVER_TRACK_SKIP",
            "HELM_CONFLICT_MARKER_SKIP", "HELM_LANE_DISCIPLINE_SKIP",
            "HELM_WORK_CLAIM", "HELM_WORK_INTEGRATOR", "HELM_LANDLOCK")

# `git worktree add` trips the reference-transaction guard's branch-creation
# arm once the rail is installed; a room birth is exactly what HELM_WORK_CLAIM
# declares, and using it here keeps the rung's OWN hatch out of the fixture.
ROOM_ENV = {"HELM_WORK_CLAIM": "1"}


class RungBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lane-rung-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_LANDLOCK"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        needles = os.path.join(self.tmp, "needles.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-lane-rung\n")
        os.environ["HELM_PRIVATE_NEEDLES"] = needles
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(self.sh(self.root, *cmd).returncode, 0)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, cwd, *args, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run(list(args), cwd=cwd, capture_output=True,
                              text=True, timeout=60, env=merged)

    def seed(self, body="seed\n"):
        """The repo's first commit — every venue test needs a trunk to have
        departed from, and the root commit has its own exemption."""
        self.write(self.root, "README", body)
        self.sh(self.root, "git", "add", "-A")
        r = self.sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)

    def write(self, cwd, rel, body):
        full = os.path.join(cwd, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(body)

    def stage(self, rel, body, cwd=None):
        cwd = cwd or self.root
        self.write(cwd, rel, body)
        r = self.sh(cwd, "git", "add", "--", ":(literal)" + rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def room(self, name="alpha"):
        """A second registered worktree on its own lane branch — the estate
        the shared checkout is shared WITH. Returns its path."""
        path = os.path.join(self.tmp, "rooms", name)
        r = self.sh(self.root, "git", "worktree", "add", "-q", "-b",
                    "lane/" + name, path, "main", env=ROOM_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def rung(self, cwd=None, env=None):
        """The rung EXACTLY as the hook runs it: a plain script, cwd in the
        target work tree, no helm package anywhere near the interpreter."""
        return self.sh(cwd or self.root, sys.executable, MODULE, "--staged",
                       env=env)

    def head(self, cwd=None):
        return self.sh(cwd or self.root, "git", "rev-parse", "HEAD").stdout.strip()

    def commit(self, msg="c", cwd=None, env=None):
        return self.sh(cwd or self.root, "git", "commit", "-m", msg,
                       env=env)


class ScriptVenueTest(RungBase):
    """The rung as a script, against real git state. No hook installed."""

    def test_originating_on_main_in_the_shared_checkout_is_refused(self):
        """THE MUST-CATCH — the exact incident: an originated commit on trunk
        in the tree every lane is cut from, with rooms already registered."""
        self.seed()
        self.room()
        self.stage("pkg/mod.py", "X = 1\n")
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("[helm lane-discipline] REFUSED", r.stderr)
        self.assertIn("originating work on 'main' in the shared checkout",
                      r.stderr)
        self.assertIn("helm work claim", r.stderr,
                      "the refusal must name the exact command that fixes it")
        self.assertIn("HELM_WORK_INTEGRATOR=1", r.stderr,
                      "the escape hatch must be documented in the refusal")
        self.assertIn("HELM_LANE_DISCIPLINE_SKIP=1", r.stderr)
        self.assertIn("INHERITS", r.stderr,
                      "the refusal teaches the failure MODE, not just the rule")

    def test_a_lane_worktree_commit_is_admitted(self):
        """THE MUST-PASS, and the hot path: every seat working correctly is
        here, so this arm is what keeps the rung from costing the fleet."""
        self.seed()
        path = self.room()
        self.stage("pkg/mod.py", "X = 1\n", cwd=path)
        r = self.rung(cwd=path)
        self.assertIn(MODULE, r.args)      # the measurement is bound to THIS rung
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        state, why = lane_discipline.verdict(path, environ={})
        self.assertIn("lane worktree", why)
        self.assertIn(state, (lane_discipline.ADMIT,))

    def test_a_conflicted_fold_carries_merge_head_and_is_admitted(self):
        """THE FOLD DOOR, through a REAL conflict rather than a planted file.
        A clean `git merge` never reaches a pre-commit rung at all; the one
        way a genuine fold arrives here is a conflict finished by `git
        commit`, and MERGE_HEAD is git's own record that the content already
        exists on another ref."""
        self.seed()
        path = self.room()
        self.stage("shared.txt", "lane side\n", cwd=path)
        lane = self.commit("lane work", cwd=path)
        self.assertIn("lane work", lane.stdout)
        self.stage("shared.txt", "main side\n")
        trunk = self.commit("trunk work", env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn("trunk work", trunk.stdout)
        merge = self.sh(self.root, "git", "merge", "--no-ff", "lane/alpha",
                        "-m", "fold: alpha")
        self.assertNotEqual(merge.returncode, 0, "the fixture must CONFLICT")
        marker = lane_discipline.integration_in_progress(
            os.path.join(self.root, ".git"))
        self.assertIn("fold door", marker)
        self.stage("shared.txt", "resolved\n")
        r = self.rung()
        self.assertIn(MODULE, r.args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)

    def test_the_integrator_declaration_admits_and_names_itself(self):
        """The escape hatch is a DECLARATION, and the rung says so rather than
        pretending it proved anything."""
        self.seed()
        self.room()
        self.stage("docs/note.md", "note\n")
        r = self.rung(env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn(MODULE, r.args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        state, why = lane_discipline.verdict(
            self.root, environ={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn("declares the integrator", why)
        self.assertIn(state, (lane_discipline.ADMIT,))

    def test_a_solo_repo_with_no_estate_is_admitted(self):
        """ADOPTION ARM: `helm work install-guard` in a repo with one worktree
        must not brick `git commit`. There is no lane that could inherit the
        commit, so there is no harm this rung exists to prevent."""
        self.seed()
        self.stage("pkg/mod.py", "X = 1\n")
        r = self.rung()
        self.assertIn(MODULE, r.args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        state, why = lane_discipline.verdict(self.root, environ={})
        self.assertIn("one worktree", why)
        self.assertIn(state, (lane_discipline.ADMIT,))

    def test_off_base_and_detached_shared_checkouts_are_another_guard_s_law(self):
        """Narrow beats broad: HEAD departure in the shared checkout belongs
        to the reference-transaction and post-checkout guards, and a rung that
        also claimed it would refuse work those guards already handle."""
        self.seed()
        self.room()
        self.sh(self.root, "git", "checkout", "-q", "-b", "side",
                env={"HELM_WORK_INTEGRATOR": "1"})
        self.stage("pkg/mod.py", "X = 1\n")
        off = self.rung()
        self.assertIn(MODULE, off.args)
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertNotIn("REFUSED", off.stderr)
        self.assertIn("not the base branch",
                      lane_discipline.verdict(self.root, environ={})[1])
        self.sh(self.root, "git", "checkout", "-q", "--detach",
                env={"HELM_WORK_INTEGRATOR": "1"})
        detached = self.rung()
        self.assertIn(MODULE, detached.args)
        self.assertEqual(detached.returncode, 0, detached.stderr)
        self.assertNotIn("REFUSED", detached.stderr)
        self.assertIn("detached", lane_discipline.verdict(self.root, environ={})[1])

    def test_the_root_commit_is_admitted_because_there_is_no_trunk_yet(self):
        """A repo's FIRST commit has no trunk to protect and no lane that
        could exist — refusing it would break every repo the guard is
        installed into before it has any history."""
        self.stage("README", "first\n")
        r = self.rung()
        self.assertIn(MODULE, r.args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        state, why = lane_discipline.verdict(self.root, environ={})
        self.assertIn("root commit", why)
        self.assertIn(state, (lane_discipline.ADMIT,))

    def test_an_unmeasurable_venue_fails_closed(self):
        """UNKNOWN is not admitted: what lands on a shared trunk is not
        recoverable by a retry, so a venue the rung cannot read refuses."""
        outside = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(outside)
        r = self.rung(cwd=outside)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("cannot be measured", r.stderr)

    def test_a_wrong_invocation_refuses_with_usage(self):
        self.seed()
        r = self.sh(self.root, sys.executable, MODULE)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("usage: lane_discipline.py --staged", r.stderr)


class RefusalPrescribesTheRunnableClaimFormTest(RungBase):
    """The instructions-are-runnable law, bound both ways (its test file's
    own pattern): prove the printed cure's flag REALLY binds, prove the form
    it replaced really did not, and prove the refusal prints the binding
    form. The old cure said `helm work claim <lane-name> <your-seat-name>` —
    work/_cli.py reads the seat from --seat (or derives one) and never looks
    at a second positional, so following it verbatim EXITED 0 and leased the
    room to a different name: the worst shape in the class, a dry run that
    reads like success."""

    def work_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def holders(self):
        rows = pk.read_json(seats.claims_path(), {}) or {}
        return {r: v.get("holder") for r, v in rows.items()
                if isinstance(v, dict)}

    def test_a_positional_seat_parses_binds_nothing_and_exits_zero(self):
        self.seed()
        rc, _out, err = self.work_cmd("claim", "lane-a", "imposter-seat")
        self.assertEqual(rc, 0, err)          # it "succeeds" — that is the trap
        holders = self.holders()
        self.assertTrue(holders, "no lease minted at all — fixture broken")
        self.assertNotIn("imposter-seat", holders.values())

    def test_the_seat_flag_binds_and_the_refusal_prescribes_it(self):
        self.seed()
        rc, _out, err = self.work_cmd("claim", "lane-b", "--seat", "real-seat")
        self.assertEqual(rc, 0, err)
        self.assertIn("real-seat", self.holders().values())
        text = "\n".join(lane_discipline._refusal(self.root, "detail"))
        self.assertIn("helm work claim <lane-name> --seat <your-seat-name>",
                      text)
        self.assertNotIn("claim <lane-name> <your-seat-name>", text)


class HookWiringTest(RungBase):
    """The rung through the REAL composed hook `helm work install-guard`
    writes — the only place its exit status actually stops a commit."""

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def install(self):
        rc, out, err = self.cli("install-guard", "--apply")
        self.assertEqual(rc, 0, err)
        return out

    def test_install_snapshots_the_lane_rung_beside_the_hook(self):
        self.seed()
        out = self.install()
        self.assertIn("HELM_LANE_DISCIPLINE_SKIP=1", out,
                      "the install summary must teach the rung's own skip")
        assets = _guard._scanner_assets(self.root)
        installed = next(p for p in assets if p.endswith("lane_discipline.py"))
        with open(installed, "rb") as got, open(MODULE, "rb") as want:
            snapshot, source = got.read(), want.read()
        self.assertTrue(snapshot)
        self.assertEqual(snapshot, source,
                         "installed snapshot must be the source, byte-for-byte")
        hook = work.hook_path(self.root, "pre-commit")
        with open(hook) as f:
            body = f.read()
        self.assertIn("lane-discipline", body)
        self.assertIn(installed, body)
        self.assertEqual(body.count('python3 "$lane_discipline" --staged'), 1,
                         "duplicate hook invocations repeat every refusal")
        self.assertNotIn(MODULE, body,
                         "a source lane path is editable and disposable")

    def test_an_ad_hoc_commit_on_main_is_refused_end_to_end(self):
        """THE INCIDENT, reproduced through the installed hook."""
        self.seed()
        self.install()
        self.room()
        before = self.head()
        self.stage("pkg/adhoc.py", "X = 1\n")
        r = self.commit()
        self.assertIn("[helm lane-discipline] REFUSED", r.stderr)
        self.assertNotEqual(r.returncode, 0, "the commit MUST be refused")
        after = self.head()
        self.assertTrue(after)
        self.assertEqual(after, before, "history did not move")
        status = self.sh(self.root, "git", "status", "--porcelain").stdout
        self.assertIn("A  pkg/adhoc.py", status,
                      "refusal leaves the stage intact for the move to a lane")

    def test_a_clean_fold_merge_lands_through_the_installed_hook(self):
        """THE ARM THAT MATTERS MOST. A blanket refusal would break the one
        workflow that is supposed to work, and it would be overridden within
        the hour. A clean fold takes NO env at all."""
        self.seed()
        self.install()
        path = self.room()
        self.stage("pkg/lanework.py", "X = 1\n", cwd=path)
        lane = self.commit("lane work", cwd=path)
        self.assertIn("lane work", lane.stdout)
        before = self.head()
        r = self.sh(self.root, "git", "merge", "--no-ff", "lane/alpha",
                    "-m", "fold: alpha at deadbeef (kimi APPROVE gate:cafe)")
        self.assertIn("Merge made", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        after = self.head()
        self.assertTrue(after)
        self.assertNotEqual(after, before, "the fold DID land")
        log = self.sh(self.root, "git", "log", "-1", "--format=%s").stdout
        self.assertIn("fold: alpha", log)

    def test_a_conflicted_fold_lands_through_the_installed_hook(self):
        """The one shape where a genuine fold DOES reach pre-commit: a merge
        that stopped on a conflict and is finished by `git commit`. MERGE_HEAD
        admits it with no env."""
        self.seed()
        self.install()
        path = self.room()
        self.stage("shared.txt", "lane side\n", cwd=path)
        lane = self.commit("lane work", cwd=path)
        self.assertIn("lane work", lane.stdout)
        self.stage("shared.txt", "main side\n")
        trunk = self.commit("trunk", env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn("trunk", trunk.stdout)
        before = self.head()
        merge = self.sh(self.root, "git", "merge", "--no-ff", "lane/alpha",
                        "-m", "fold: alpha")
        self.assertNotEqual(merge.returncode, 0, "the fixture must CONFLICT")
        self.stage("shared.txt", "resolved\n")
        r = self.commit("fold: alpha (conflict resolved)")
        self.assertIn("fold: alpha (conflict resolved)", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        after = self.head()
        self.assertTrue(after)
        self.assertNotEqual(after, before, "the conflicted fold DID land")

    def test_a_squash_fold_lands_through_the_installed_hook(self):  # noqa: VACUOUS_ASSERTION — positive controls: SQUASH_MSG present, the fold lands, head moves; the MERGE_HEAD absence IS the reviewed shape
        """kimi's review: `git merge --squash` writes NO MERGE_HEAD — it sets
        SQUASH_MSG and leaves the commit to the user, whose `git commit`
        reaches pre-commit with NO marker, so an unguarded rung refused a
        correct squash-fold (proven against real git). SQUASH_MSG is now a
        marker, and the squash fold is admitted with no env."""
        self.seed()
        self.install()
        path = self.room()
        self.stage("pkg/mod.py", "X = 1\n", cwd=path)
        lane = self.commit("lane work", cwd=path)
        self.assertIn("lane work", lane.stdout)
        self.stage("other.txt", "trunk side\n")
        trunk = self.commit("trunk", env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn("trunk", trunk.stdout)
        before = self.head()
        squash = self.sh(self.root, "git", "merge", "--squash", "lane/alpha")
        self.assertEqual(squash.returncode, 0, squash.stderr)
        # the fixture must present the shape the review found: SQUASH_MSG
        # set, MERGE_HEAD absent — or the admit below proves nothing
        self.assertTrue(os.path.exists(os.path.join(self.root, ".git",
                                                    "SQUASH_MSG")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".git",
                                                     "MERGE_HEAD")))
        r = self.commit("fold: alpha (squash)")
        self.assertIn("fold: alpha (squash)", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REFUSED", r.stderr)
        after = self.head()
        self.assertTrue(after)
        self.assertNotEqual(after, before, "the squash fold DID land")

    def test_a_lane_commit_lands_through_the_installed_hook(self):
        """Every seat's ordinary day, through the shared hook the lane
        inherits: the rung must be invisible here."""
        self.seed()
        self.install()
        path = self.room()
        before = self.head(cwd=path)
        self.stage("pkg/mod.py", "X = 1\n", cwd=path)
        r = self.commit("lane work", cwd=path)
        self.assertIn("lane work", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("lane-discipline", r.stderr,
                         "a correct commit must not even hear from the rung")
        after = self.head(cwd=path)
        self.assertTrue(after)
        self.assertNotEqual(after, before)

    def test_both_hatches_are_one_commit_and_grant_no_standing_exemption(self):
        self.seed()
        self.install()
        self.room()
        self.stage("docs/a.md", "a\n")
        declared = self.commit("via the integrator declaration",
                               env={"HELM_WORK_INTEGRATOR": "1"})
        self.assertIn("via the integrator declaration", declared.stdout)
        self.assertEqual(declared.returncode, 0, declared.stderr)
        self.stage("docs/b.md", "b\n")
        skipped = self.commit("via this rung's own skip",
                              env={"HELM_LANE_DISCIPLINE_SKIP": "1"})
        self.assertIn("via this rung's own skip", skipped.stdout)
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.stage("docs/c.md", "c\n")
        r = self.commit()
        self.assertIn("[helm lane-discipline] REFUSED", r.stderr)
        self.assertNotEqual(r.returncode, 0, "no standing exemption")

    def test_this_rung_s_skip_does_not_disarm_the_conflict_marker_rung(self):
        """Each rung's skip isolates to itself — the v3 law. This rung runs
        FIRST, so a skip that `exit 0`ed the hook would silently switch off
        every content rung beneath it."""
        self.seed()
        self.install()
        self.room()
        block = "\n".join(["<" * 7 + " HEAD", "ours", "=" * 7, "theirs",
                           ">" * 7 + " lane/x", ""])
        self.stage("docs/conflicted.md", block)
        r = self.commit(env={"HELM_LANE_DISCIPLINE_SKIP": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm conflict-marker] REFUSED", r.stderr,
                      "the conflict rung must still fire under this rung's skip")

    def test_a_missing_snapshot_fails_open_but_loud(self):
        """A deleted snapshot must not brick every commit in the estate — but
        the skip has to announce itself, or the estate believes it is guarded
        when it is not."""
        self.seed()
        self.install()
        self.room()
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("lane_discipline.py"))
        os.unlink(installed)
        self.stage("docs/clean.md", "clean\n")
        r = self.commit("clean commit under a missing snapshot")
        self.assertIn("[helm lane-discipline] WARNING: rung missing", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install-guard --apply", r.stderr)

    def test_wiring_census_proves_the_installed_rung_and_names_an_absent_one(self):
        """The ALLOWED exemption is a claim; the actuator census is its proof.
        Both directions, or the obligation is prose."""
        self.seed()
        self.install()
        hooks = os.path.dirname(work.hook_path(self.root, "pre-commit"))
        empty_units = os.path.join(self.tmp, "no-units")
        os.makedirs(empty_units)
        got = wiring.actuator_census(hook_dir=hooks, unit_dir=empty_units,
                                     crontab="", settings_paths=[])
        self.assertIn("lane-discipline-pre-commit", got["wired"])
        self.assertNotIn("lane-discipline-pre-commit", got["missing"])
        bare = os.path.join(self.tmp, "bare-hooks")
        os.makedirs(bare)
        with open(os.path.join(bare, "pre-commit"), "w") as f:
            f.write('#!/bin/sh\nscanner=/opt/helm/helm/nevertrack.py\n'
                    'exec python3 "$scanner" --staged\n')
        os.chmod(os.path.join(bare, "pre-commit"), 0o755)
        got = wiring.actuator_census(hook_dir=bare, unit_dir=empty_units,
                                     crontab="", settings_paths=[])
        self.assertIn("lane-discipline-pre-commit", got["missing"])


if __name__ == "__main__":
    unittest.main()
