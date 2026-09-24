#!/usr/bin/env python3
"""The UNTESTED-COMPOSITION stop-guard rung: two verified halves, no arm across.

OWNER REQUEST, 2026-08-23, verbatim: "Two green halves with an untested
composition — this seems to be the biggest failure mode across projects, please
/learn globally to prevent that via whispers and or stophooks." The whisper half
is the store's `composition-untested-on-split` reflex. This file is the stop
half, and it exists because a reflex fires on LANGUAGE: it can only reach a seat
that already said the word "split". The expensive shape is the one nobody named.

THE SIGNAL WAS CHOSEN BY KILLING TWO CANDIDATES WITH MEASUREMENTS, and half this
file is the controls that keep the survivor honest:

  `helm chat claims` — read live 2026-08-23 and carrying ONE row, the reading
    seat's own lease. An advisory claim needs somebody to declare, which is the
    same dependency that makes the whisper insufficient.
  `work.lane_overlaps` + the claim ledger — i.e. HELM BOOKKEEPING. Killed by
    measuring it against the project that ASKED for the rung: THE SIBLING PROJECT has
    zero helm lane rooms, 0 of 2,422 dispatches, 0 of 2,425 gate receipts, 0 of
    43 land requests, 0 work claims. The rung would have fired for helm and
    never for it. It also missed helm's OWN seat rooms, because `lane_rows`
    only matches direct children of `<root>-wt/` and five of the seven occupied
    rooms on this box live at `helm-wt/seats/<seat>`.

So the signal is GIT plus /proc — the worktree registry, the processes standing
in those rooms, the branches' own diffs — with helm bookkeeping ADDED where it
exists rather than required. Each remaining clause has a must-miss arm here plus
the positive control that proves the arm discriminates rather than measuring an
empty fixture:

  BOTH HALVES VERIFIED — a gate receipt where the repo mints them, else the
    git-only reading (own commits + a clean room). An unfinished half is not a
    half. The tier is per REPO, so a helm lane that has not gated stays NOT
    green rather than falling through to the weaker test.
  A DIFFERENT HOLDER — two rooms driven by one seat are one seat's problem.
  A CODE FILE — a prose collision is a text merge, not a composition.
  A CONFLICT IS STILL A SEAM. An earlier cut filtered conflicts out as "git
    will be loud", which is true about the text and silent about the behaviour;
    every code-overlapping pair in the sibling project conflicts, on branches ~830
    commits divergent, so the filter made the rung unable to fire for the
    project that asked for it. The merge outcome now picks the MESSAGE only.
"""
import contextlib
import datetime
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

from tests._tmphome import declaring as _tmp_declaring  # noqa: E402

from helm import dispatches, eventledger, gate, gateimport, seats, vcs, work  # noqa: E402
# IMPORTED UNDER ITS OWN NAME, not reached through `seats`: the commit point is
# this module's, and a test that pokes it through a re-export would still pass
# if the re-export vanished.
from helm import seats_stop_guard  # noqa: E402
from helm import seats_stop_seam  # noqa: E402
from helm import seats_stop_signals  # noqa: E402
from helm.work import _gc as _work_gc  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "HELM_ADOPTED_DIR",
            "HELM_SCRATCH_GC", "HELM_PRIVATE_NEEDLES",
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_SEAM", "HELM_STOP_GUARD_CLAIMS",
            "HELM_STOP_GUARD_WHISPER", "HELM_STOP_GUARD_WIRING",
            "HELM_STOP_GUARD_NDP", "HELM_STOP_GUARD_SPIRAL",
            "HELM_STOP_GUARD_INDEX", "HELM_STOP_GUARD_INBOX",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")

# Wide enough that two branches can edit opposite ends and merge in silence, or
# the same line and conflict. Both outcomes are under test, so the fixture has
# to be able to produce either on demand — see FixtureTest.
SEED = "".join("line %02d\n" % i for i in range(60))


def _sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=60)


class SeamBase(unittest.TestCase):
    def setUp(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seam-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        os.environ["HELM_PRIVATE_NEEDLES"] = os.path.join(
            self.tmp, "no-needles-configured.txt")
        # EVERY OTHER RUNG IS OFF, and each for a reason that would otherwise
        # make an assertion here mean something else. The CLAIMS rung blocks on
        # the very lane leases this fixture must mint; the whisper reads the
        # same world and soft-holds; the wiring rung derives its repo from
        # helm's own __file__ and is not hermetic; the index/scratch legs touch
        # the operator's real estate. The beacon rung needs no switch — it fires
        # only for a seat named by HELM_CHAT_NAME, left unset here.
        for k in ("CLAIMS", "WHISPER", "WIRING", "NDP", "SPIRAL", "INDEX",
                  "INBOX"):
            os.environ["HELM_STOP_GUARD_" + k] = "0"
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@t"),
                    ("git", "config", "user.name", "t")):
            self.assertEqual(_sh(self.root, *cmd).returncode, 0)
        for name in ("shared.py", "other.py", "notes.md"):
            with open(os.path.join(self.root, name), "w") as f:
                f.write(SEED)
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "commit", "-q", "-m", "seed")
        self.assertEqual(r.returncode, 0, r.stderr)
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)

    def tearDown(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── fixture verbs ────────────────────────────────────────────────────
    def work(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with _tmp_declaring(args), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.root])
        return rc, out.getvalue(), err.getvalue()

    def path(self, lane):
        return os.path.join(self.root + "-wt", lane)

    def claim(self, lane, seat):
        rc, _o, err = self.work("claim", lane, "--seat", seat)
        self.assertEqual(rc, 0, err)
        return self.path(lane)

    def room(self, lane):
        """A registered room with NO lease — the parked/unclaimed shape.

        HELM_WORK_INTEGRATOR=1 because the ref-guard refuses a raw
        `worktree add -b` by design; the fixture stands in for the integrator
        and says so rather than weakening the guard to let a test through."""
        path = self.path(lane)
        r = subprocess.run(["git", "worktree", "add", "-q", "-b",
                            "lane/" + lane, path, "main"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=60,
                           env=dict(os.environ, HELM_WORK_INTEGRATOR="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        return path

    def edit(self, lane, name="shared.py", at=1, message=None):
        """Rewrite ONE line of `name` in the room and commit -> the sha."""
        path = self.path(lane)
        target = os.path.join(path, name)
        with open(target) as f:
            lines = f.readlines()
        lines[at] = "%s touched %s\n" % (lane, at)
        with open(target, "w") as f:
            f.writelines(lines)
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", message or ("%s edits %s" % (lane, name)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.tip(lane)

    def soil(self, lane, name="other.py"):
        """Leave uncommitted bytes — a half still in flight."""
        with open(os.path.join(self.path(lane), name), "a") as f:
            f.write("# work in progress\n")

    def trailer(self, lane, body):
        """An empty commit carrying a trailer — the discharge's real shape."""
        path = self.path(lane)
        r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-q", "--allow-empty", "-m", "seam note", "-m", body)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.tip(lane)

    def reset(self, lane, sha):
        """Rewind a lane's branch to `sha`. THE ISOLATION VERB.

        A matrix over commit BODIES cannot share one branch. Every reader that
        judges a body reads `trunk..branch`, so a body committed in row two is
        still behind row seven, and row seven's verdict is then row two's. Each
        row must start from the same base and carry its OWN commit only."""
        r = _sh(self.path(lane), "git", "reset", "--hard", sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tip(lane), sha, "the rewind did not take")
        return sha

    def own_log(self, lane, trunk="main"):
        """The message bodies of `lane`'s OWN commits — what a body-reading
        discharge sees, and therefore what an isolation claim is measured on."""
        r = _sh(self.root, "git", "log", "--format=%B",
                "%s..lane/%s" % (trunk, lane))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def tip(self, lane):
        r = _sh(self.root, "git", "rev-parse", "lane/" + lane)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def merged_tree(self, lane_a, lane_b):
        r = _sh(self.root, "git", "merge-tree", "--write-tree",
                "lane/" + lane_a, "lane/" + lane_b)
        return r.returncode, r.stdout.strip().split("\n")[0]

    def roster(self, name, cwd):
        """One roster row placing `name` in `cwd` — helm's OWN seat surface.

        The shape is the real one (`helm chat seats` writes `cwd` per row;
        measured on the live roster 2026-08-23, all 15 seats carry it including
        the claude-native family that exports no HELM_CHAT_NAME)."""
        from helm import pk as _pk, seats as _s
        rows = _pk.read_json(_s.roster_path(), {}) or {}
        rows[name] = dict(rows.get(name) or {}, cwd=cwd)
        os.makedirs(os.path.dirname(_s.roster_path()), exist_ok=True)
        _pk.atomic_write(_s.roster_path(), json.dumps(rows))
        return rows

    def receipt(self, head=None, tree=None, status="OK", append=True,
                **spoil):
        """One gate receipt, id computed by gate's OWN content-hash.

        Hand-writing the id would be a forgery `gate.receipts()` drops on the
        floor — silently, since a dropped row and an absent row read the same.
        `test_the_planted_receipts_are_actually_readable` is the must-hit that
        keeps this fixture honest if the grammar ever moves."""
        row = {"v": 5, "event": "gate", "ts": "2026-08-23T00:00:00Z",
               "head": head or ("0" * 40), "tree": tree or ("f" * 40),
               "dirty": False, "head_after": head or ("0" * 40),
               "tree_after": tree or ("f" * 40), "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.14",
                               "language": "3.14", "executable": "/x"},
               "argv": ["python3"] + list(gate.SUITE), "suite": True,
               "status": status, "ran": 1, "skipped": 0, "rc": 0,
               "executed": True, "repo_id": self.root, "failures": [],
               "failures_unreadable": False, "base_check": None,
               "host": {"node": "n", "system": "Linux", "release": "r",
                        "id": "i"},
               "label": None, "elapsed": 1.0, "wall": 1.0, "detail": ""}
        # SPOIL BEFORE THE ID, or the row is a forgery `gate.receipts()` drops
        # on the floor — and a dropped row reads exactly like an absent one.
        row.update(spoil)
        row["id"] = gate._receipt_id(row)
        if append:
            self.assertTrue(eventledger.append(gate.receipts_path(), row))
        return row

    def tree_of(self, lane):
        r = _sh(self.root, "git", "rev-parse", "lane/%s^{tree}" % lane)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def green(self, lane):
        """Mark a lane's CURRENT TREE as having passed a whole-suite gate.

        THE TREE IS THE POINT, not the head: the rung asks whether this exact
        content was tested, so a fixture that planted only a head would have
        gone on passing while the production reader had stopped looking there —
        the stale-fixture shape this file's own header warns about."""
        return self.receipt(head=self.tip(lane), tree=self.tree_of(lane))

    # ── the world under test ─────────────────────────────────────────────
    def two_green_halves(self, code="shared.py", merges=True):
        """alpha (mine, seat s1) and beta (peer, seat s2): both live by lease,
        both having authored `code`, both carrying a gate receipt, merging
        cleanly unless asked to collide."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", code, at=1)
        self.edit("beta", code, at=1 if not merges else 50)
        self.green("alpha")
        self.green("beta")

    def two_banked_halves(self, code="shared.py", merges=True):
        """The SAME shape in a repo that mints NO gate receipts — the sibling project's
        world. Greenness falls to the git-only reading: own commits, room
        clean."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", code, at=1)
        self.edit("beta", code, at=1 if not merges else 50)

    def compose_train(self, name, *lanes, seat="s3", green=True,
                      repo_id=None):
        """A LIVE room whose branch carries every named lane's commits REBASED
        onto the trunk — the integrator's compose train, and the exact shape
        ancestry cannot see.

        `cherry-pick` is the mechanism because it is what a rebase does to each
        commit: the same patch under a different object id. So
        `merge-base --is-ancestor` answers NO about a train that demonstrably
        carries the lane, and `git cherry` answers YES — which is why clause 7
        asks `landed_state` rather than either one alone."""
        path = self.claim(name, seat)
        # THE TRAIN'S OWN COMMIT COMES FIRST, AND THE ORDER IS THE WHOLE
        # FIXTURE. A commit's sha is its content — tree, parent, message,
        # author and committer INCLUDING THE SECOND THEY WERE STAMPED IN — so
        # a cherry-pick of one lane commit onto the SAME parent `main`, with
        # the same message and the same `t <t@t>` identity, produces the
        # IDENTICAL OBJECT whenever both commits land inside one second. The
        # train then has the lane's own commit as an ancestor and every arm
        # here measures the wrong shape: `merge-base --is-ancestor` starts
        # answering YES, so the pair's merge-base becomes the lane's own tip,
        # its authored diff against that base is EMPTY, and the pair the arm
        # is about never forms. THAT IS A RACE ON THE WALL CLOCK: the same
        # source was green on one gate and red on the next, twice, with no
        # change between them. Committing the train's own prose first gives
        # every cherry-pick a parent no lane commit has, which makes the
        # rebased-sibling shape a property of the fixture rather than of how
        # fast the box was.
        #
        # IT IS ALSO THE TRUTHFUL SHAPE FOR THE OTHER REASON THIS FIXTURE
        # LEARNED THE EXPENSIVE WAY. Greenness is keyed on the TREE, so a
        # train carrying exactly one lane's single edit would hold that LANE'S
        # tree byte-for-byte, and greening the train would silently green the
        # lane. A real train carries several lanes plus the integrator's own
        # composition. The commit edits PROSE, which `_SEAM_CODE` excludes, so
        # it adds no code overlap of its own.
        with open(os.path.join(path, "notes.md"), "a") as f:
            f.write("compose %s\n" % name)
        _sh(path, "git", "add", "-A")
        r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", "compose %s" % name)
        self.assertEqual(r.returncode, 0, r.stderr)
        for lane in lanes:
            r = _sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "cherry-pick", "main..lane/" + lane)
            self.assertEqual(r.returncode, 0, r.stderr)
        # THE PREMISE, MEASURED HERE RATHER THAN ASSUMED BY EACH ARM. Every
        # caller depends on the train being a REBASED SIBLING of the lanes it
        # carries — patch-identical and object-different — because that is the
        # shape ancestry cannot see and clause 7 exists for. Asserting it in
        # the helper means a future change to the composition cannot quietly
        # turn these arms into tests of the sha-identity path that already
        # worked, which is exactly what the clock race did.
        for lane in lanes:
            r = _sh(self.root, "git", "merge-base", "--is-ancestor",
                    "lane/" + lane, "lane/" + name)
            # rc 1 IS THE ANSWER "NOT AN ANCESTOR". rc 128 is git FAILING —
            # a bad ref, a broken repo — and `!= 0` accepts it as evidence of
            # non-ancestry, which is an unreadable result and a negative
            # result sharing one value at the exact place the fixture's whole
            # premise is established.
            self.assertEqual(r.returncode, 1, r.stderr)
            # AND THE POSITIVE HALF, THROUGH THE INSTRUMENT THE RUNG ITSELF
            # ASKS. Non-ancestry alone is also what an EMPTY train looks like;
            # the shape these arms are about is patch-identical AND
            # object-different, so the containment must be measured rather
            # than assumed from a cherry-pick that returned 0.
            self.assertIn(
                vcs.backend(self.root).landed_state(
                    self.root, "lane/" + lane, "lane/" + name),
                _work_gc.RETIRABLE,
                "the train does not carry lane/%s by patch identity, so it "
                "is not a compose train at all" % lane)
        self.assertNotEqual(self.tree_of(name), self.tree_of(lanes[0]),
                            "the train's tree still equals its lane's, so a "
                            "receipt on either would green both")
        if green:
            # `repo_id` NAMING A PATH THAT DOES NOT EXIST makes the receipt
            # UNPLACEABLE: it ARMS a half (the tree lands in `trees`) and
            # cannot DISCHARGE (it never reaches `local`). That is the only
            # fixture that holds containment TRUE while the container is not
            # a same-repo green.
            self.receipt(head=self.tip(name), tree=self.tree_of(name),
                         **({"repo_id": repo_id} if repo_id else {}))
        return path

    def guard(self, seat="s1", session="sess-1", cwd=None, emitted=True):
        """One stop, THROUGH THE EXIT THE CLI WOULD HAVE TAKEN. -> (blocks, warns)

        `seats_cli` picks the exit by whether there are blocks: blocks print
        and return 2 with every advisory suppressed, otherwise the warns print
        and it returns 0. BOTH exits commit the latches whose text they
        actually put on the stream, so this reproduces that rule rather than
        approximating it — a fixture that latched on a different rule from the
        production exit is how the disclosure defect survived a cure.

        `emitted=False` models the third exit, the one where nothing is
        printed at all: the guard raised after this rung ran, `seats_cli`
        prints THE GUARD COULD NOT RUN and returns 0. Nothing was seen, so
        nothing may be spent. Calling the CLI itself would drag the whole hook
        payload in to observe one commit."""
        blocks, warns = seats.stop_guard(session=session, room="main",
                                         seat=seat,
                                         cwd=cwd or self.path("alpha"))
        self.emitted = ""
        if not emitted:
            pass                        # nothing reached a reader
        elif blocks:
            self.emitted = "\n".join(blocks)
            seats_stop_seam.commit_disclosures(self.emitted)
        else:
            self.emitted = "\n".join(warns)
            seats_stop_seam.commit_disclosures(self.emitted)
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        return blocks, warns

    def text(self, seat="s1", session="sess-1", cwd=None):
        blocks, warns = self.guard(seat, session, cwd)
        return "\n".join(blocks), "\n".join(warns)


class FixtureTest(SeamBase):
    """The fixture's own controls. A rung tested through a fixture that cannot
    express its inputs proves nothing about the rung."""

    def test_the_planted_receipts_are_actually_readable(self):
        self.claim("alpha", "s1")
        head = self.edit("alpha")
        self.green("alpha")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0, "gate dropped the planted receipt")
        self.assertEqual([r["head"] for r in rows], [head])
        trees, local, err, _warn = _work_gc.green_receipts(self.root)
        self.assertIsNone(err)
        self.assertIn(self.tree_of("alpha"), trees)
        # THE FIXTURE'S RECEIPTS MUST BE PLACEABLE IN THIS REPOSITORY, or every
        # discharge arm below would be measuring the unplaceable path instead
        # of the one production takes.
        self.assertIn(self.tree_of("alpha"), local,
                      "the planted receipt does not resolve to this repo")

    def test_beacon_and_lease_rungs_share_one_dispatch_snapshot(self):
        self.two_green_halves()
        os.environ["HELM_CHAT_NAME"] = "s1"
        self.roster("s1", self.path("alpha"))
        real = dispatches.snapshot
        obligation = seats_stop_signals._beacon_obligation
        with mock.patch.object(seats_stop_signals, "beacon_procs",
                               return_value=([], None)), \
                mock.patch.object(seats_stop_signals, "_beacon_obligation",
                                  wraps=obligation) as beacon_obligation, \
                mock.patch.object(dispatches, "snapshot", wraps=real) as snapshot:
            blocks, _warns = self.guard()
        self.assertTrue(blocks, "control: the lease and seam ladder must have run")
        beacon_obligation.assert_called_once_with("s1", mock.ANY)
        self.assertIsNotNone(beacon_obligation.call_args.args[1],
                             "control: beacon must consume the shared snapshot")
        self.assertEqual(snapshot.call_count, 1)

    def test_the_two_branches_merge_cleanly_or_conflict_on_demand(self):  # noqa: VACUOUS_ASSERTION — clean and conflicting are two FIXTURE states, asserted across a setUp boundary
        """THE TRAP THIS FLEET KEEPS HITTING is a fixture whose two states
        cannot actually differ. Clean-vs-conflicting now selects the MESSAGE
        rather than membership, so the fixture's ability to produce both is
        asserted here rather than assumed by every arm below."""
        self.two_green_halves(merges=True)
        rc_clean, tree = self.merged_tree("alpha", "beta")
        self.assertEqual(rc_clean, 0, "the clean fixture did not merge cleanly")
        self.assertRegex(tree, r"\A[0-9a-f]{40,64}\Z")
        self.tearDown()
        self.setUp()
        self.two_green_halves(merges=False)
        rc_conflict, _t = self.merged_tree("alpha", "beta")
        self.assertNotEqual(rc_conflict, 0,
                            "the conflicting fixture merged cleanly")

    def test_a_claimed_room_reads_LIVE_and_an_unclaimed_one_does_not(self):
        """The liveness union's lease leg, pinned on its own. Every arm below
        that expects a peer to be live rides this."""
        self.claim("alpha", "s1")
        self.room("beta")
        rooms, err, degraded = _work_gc.seam_rooms(self.root)
        self.assertIsNone(err)
        self.assertEqual(degraded, [], "the healthy census reported a hole")
        rooms = {r["path"]: r for r in rooms}
        self.assertTrue(rooms[self.path("alpha")]["live"])
        self.assertEqual(rooms[self.path("alpha")]["why"], "lease")
        self.assertEqual(rooms[self.path("alpha")]["holder"], "s1")
        self.assertFalse(rooms[self.path("beta")]["live"])


class PredicateTest(SeamBase):
    """`work.seam_candidates` — the signal on its own, so a guard-level pass
    can never be mistaken for a working detector."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def test_two_green_halves_on_one_code_file_is_a_seam(self):
        self.two_green_halves()
        rows = self.rows()
        self.assertEqual([(r["branch"], r["peer_branch"]) for r in rows],
                         [("lane/alpha", "lane/beta")])
        self.assertEqual(rows[0]["files"], ["shared.py"])
        self.assertEqual(rows[0]["peer_holder"], "s2")
        self.assertTrue(rows[0]["merges"])
        self.assertEqual(rows[0]["tree"], self.merged_tree("alpha", "beta")[1])

    def test_a_CONFLICTING_pair_is_STILL_a_seam_with_no_composed_tree(self):
        """THE CORRECTION THAT SENT THIS DESIGN BACK ONCE. An earlier cut
        filtered conflicts out as "git will be loud", which is true about the
        TEXT and silent about the BEHAVIOUR — a hand-resolved merge is an
        untested composition by construction. Measured cost of the filter:
        every code-overlapping pair in the sibling project conflicts (4 of 4, branches
        ~830 commits divergent), so the rung could not fire for the project
        whose incidents are the entire rationale."""
        self.two_green_halves(merges=False)
        rows = self.rows()
        self.assertEqual([(r["branch"], r["peer_branch"]) for r in rows],
                         [("lane/alpha", "lane/beta")])
        self.assertFalse(rows[0]["merges"], "a conflict claimed a clean merge")
        self.assertIsNone(rows[0]["tree"],
                          "a conflicted merge-tree was read as the composition")

    def test_a_receipt_at_a_CONFLICTED_merge_tree_cannot_discharge(self):
        """`git merge-tree` prints a tree for a conflicted merge too, and
        reading it as the composition would let a conflict discharge itself
        with a receipt at a tree full of conflict markers. rc 0 is the only
        clean answer."""
        self.two_green_halves(merges=False)
        rc, tree = self.merged_tree("alpha", "beta")
        self.assertNotEqual(rc, 0, "precondition: this pair must conflict")
        self.assertRegex(tree, r"\A[0-9a-f]{40,64}\Z",
                         "precondition: git must still print a tree here")
        self.receipt(head="c" * 40, tree=tree)
        self.assertTrue(self.rows(),
                        "a receipt at a CONFLICTED tree discharged the seam")

    # ── the verified-half clause, both tiers ──────────────────────────────
    def test_an_UNGATED_peer_is_an_unfinished_half_not_a_green_one(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.assertEqual(self.rows(), [], "an ungated peer was billed green")
        # POSITIVE CONTROL on the same observable: gating that very peer, with
        # nothing else changed, produces the seam. Without this the silence
        # above is equally satisfied by a predicate that never fires.
        self.green("beta")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_an_UNGATED_self_is_not_a_green_half_either(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("beta")
        self.assertEqual(self.rows(), [], "an ungated own half was billed")
        self.green("alpha")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_a_RED_receipt_is_not_a_green_half(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        """Status is read, not assumed. A FAILED run on the peer's tip is a
        receipt at that head and proves the opposite of what the rung needs."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        beta_tip = self.edit("beta", at=50)
        self.green("alpha")
        beta_tree = self.tree_of("beta")
        self.receipt(head=beta_tip, tree=beta_tree, status="FAILED")
        self.assertEqual(self.rows(), [], "a FAILED receipt was read as green")
        self.receipt(head=beta_tip, tree=beta_tree, status="OK")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_in_a_repo_with_NO_receipts_there_is_NO_SEAM_and_no_block(self):
        """THE INVERSE OF WHAT STOOD HERE, and the cost of dropping the tier.

        This arm used to assert that two BANKED halves — commits of their own,
        clean rooms — are a seam in a repo that mints no receipts. A review
        killed that: "banked" is evidence that two halves EXIST, never evidence
        about a COMPOSITION, so blocking on it was laundering by tier inside the
        rung written to complain about laundering.

        The consequence is REAL AND NOT FREE, which is why it gets an arm rather
        than a deletion: this rung now says NOTHING in a repo that cannot mint a
        receipt. It is silent there instead of blocking with a cure that proves
        nothing. If a durable accepted obligation ever lands (task/1378), this
        arm is the one that must change.
        """
        self.two_banked_halves()
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path("alpha")}, holder="s1")
        self.assertIsNone(err, "a no-receipt repo must be SILENT, not UNKNOWN")
        self.assertEqual(rows, [],
                         "banked halves were still billed as a seam")
        # THE POSITIVE CONTROL, AND IT IS ONE VARIABLE. helm's own
        # vacuous-assertion rung caught this arm asserting only an ABSENCE:
        # `seam_candidates` returning [] unconditionally would have satisfied
        # it. `two_green_halves` differs from `two_banked_halves` by EXACTLY
        # these two calls, so planting the receipts and nothing else must flip
        # the answer — which proves the emptiness above came from the missing
        # evidence rather than from a rung that finds nothing at all. This is
        # also the ARM PREDICATE stated as a test: both half trees carrying
        # admissible evidence is what makes a seam visible.
        self.green("alpha")
        self.green("beta")
        rows2, err2, _warn2 = _work_gc.seam_candidates(
            self.root, {self.path("alpha")}, holder="s1")
        self.assertIsNone(err2)
        self.assertEqual([r["peer_branch"] for r in rows2], ["lane/beta"],
                         "planting a receipt on both halves did not arm the "
                         "rung — the empty result above proves nothing")

    def test_a_DIRTY_room_is_still_green_when_the_repo_MINTS_receipts(self):
        """THE TIER MUST NOT LEAK. Where receipts exist they are the authority,
        and a room that is dirty AFTER its gate is still a gated half — using
        cleanliness there would make the rung answer a different question in
        the two worlds while claiming one meaning."""
        self.two_green_halves()
        self.soil("beta")
        # ASSERTED ON THE ROW EXISTING, not on a `green_by` tier label: the
        # label is gone with the tier, but the PROPERTY is the same and still
        # load-bearing — where a receipt exists it is the authority, and a room
        # dirty AFTER its gate is still a gated half.
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"],
                         "a room dirty after its gate stopped being green")

    def test_a_repo_that_mints_receipts_does_NOT_fall_back_for_an_ungated_lane(self):  # noqa: VACUOUS_ASSERTION — the control is the sibling banked-tier arm; here the fixture is IDENTICAL except for one receipt, which is the whole discriminator
        """The other half of the leak, and the one that would quietly disarm
        the green clause: an ungated helm lane must read NOT GREEN, never fall
        through to `banked` because it happens to be clean."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")                     # the repo now mints receipts
        self.assertEqual(self.rows(), [],
                         "an ungated but clean peer fell through to `banked`")

    # ── liveness and holder ───────────────────────────────────────────────
    def test_an_UNHELD_UNOCCUPIED_room_is_nobody_composing_against_you(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.claim("alpha", "s1")
        self.room("beta")                       # registered, NOT leased
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(), [], "an unheld room was billed live")
        # POSITIVE CONTROL on the same observable: leasing that very room, with
        # not one byte of its branch changed, produces the seam.
        self.claim("beta", "s2")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_a_room_with_a_LIVE_PROCESS_and_no_lease_is_a_live_half(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        """THE LEG THAT MAKES THIS WORK OUTSIDE HELM. THE SIBLING PROJECT has no claim
        ledger rows at all, so occupancy is the only liveness producer there —
        measured 2026-08-23: 2 of its 6 worktrees occupied, 7 of helm's 89."""
        self.claim("alpha", "s1")
        self.room("beta")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(), [], "precondition: unoccupied is silent")
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({self.path("beta"): ["4242"]},
                                             True)):
            rows = self.rows()
        self.assertEqual([r["peer_branch"] for r in rows], ["lane/beta"])
        self.assertEqual(rows[0]["peer_why"], "occupied")

    def test_both_rooms_driven_by_ME_is_not_a_seam(self):  # noqa: VACUOUS_ASSERTION — the control varies the HOLDER argument, so it is a second call by construction — one call cannot be read under two holders
        """One seat holding both halves can see both. helm's own stop guard
        documents an orchestrator holding eight lane leases at once; blocking
        it against itself is the false positive that gets a rung switched
        off."""
        self.claim("alpha", "s1")
        self.claim("beta", "s1")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(holder="s1"), [])
        # POSITIVE CONTROL: the same two rooms read by a DIFFERENT seat DO
        # seam, so the silence above is the holder clause, not an inert fixture.
        self.assertEqual([r["peer_branch"] for r in self.rows(holder="s9")],
                         ["lane/beta"])

    # ── the overlap clause ────────────────────────────────────────────────
    def test_a_PROSE_collision_is_a_text_merge_not_a_composition(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.two_green_halves(code="notes.md")
        self.assertEqual(self.rows(), [], "a markdown collision was billed")
        # POSITIVE CONTROL: the same two seats colliding in a .py file DO seam.
        self.edit("alpha", "shared.py", at=1)
        self.edit("beta", "shared.py", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual([r["files"] for r in self.rows()], [["shared.py"]])

    def test_two_branches_in_DIFFERENT_files_are_not_this_rungs_business(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here; reachability is proven by the named sibling arm rather than inside this one (test_two_green_halves_on_one_code_file_is_a_seam)
        """File overlap is the available proxy and it is stated as one. Two
        branches that never touch a shared path give this rung nothing to key
        on, and inventing a seam there would be the tuned guess the whole
        ladder refuses."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", "shared.py", at=1)
        self.edit("beta", "other.py", at=1)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(), [])

    # ── the discharge, both halves ───────────────────────────────────────
    def test_a_receipt_at_the_COMPOSED_TREE_discharges_the_seam(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        """THE MEASURED DISCHARGE, and it is a set membership rather than a
        story: `git merge-tree --write-tree` yields the exact tree the two
        halves make together, and a receipt records the tree it ran against.
        Observed already in production — 3 of the 40 clean-merging green pairs
        on the 2026-08-23 helm board carried one."""
        self.two_green_halves()
        self.assertTrue(self.rows(), "precondition: the seam must exist first")
        rc, tree = self.merged_tree("alpha", "beta")
        self.assertEqual(rc, 0)
        self.receipt(head="c" * 40, tree=tree)
        self.assertEqual(self.rows(), [],
                         "an arm run against the composed tree did not clear")

    def test_a_receipt_at_SOME_OTHER_tree_discharges_nothing(self):
        """The control on the clause above: greenness anywhere is not greenness
        at the composition. Without this, `tree in trees` could be widened to
        "any receipt exists" and stay green."""
        self.two_green_halves()
        self.receipt(head="c" * 40, tree="d" * 40)
        self.assertEqual([r["peer_branch"] for r in self.rows()],
                         ["lane/beta"],
                         "an unrelated receipt cleared the seam")

    def test_a_TRAIN_that_CARRIES_my_lane_by_patch_identity_discharges_it(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on `rows()` for this exact fixture is the FIRST assertion of test_a_TRAIN_carrying_my_lane_with_NO_RECEIPT_discharges_nothing: same lane, same train shape, an UNPLACEABLE receipt, and it requires the row to be PRESENT. Reproducing it here would make this arm a copy of that one — the emptiness below is the receipt becoming placeable, not a fixture that cannot produce a row.
        """THE FLEET BLOCKER, task/2300. Measured: five collisions on one
        seat, every one of them against a compose train that ALREADY CARRIED
        the colliding lanes. The composition those rows named had been composed
        AND gated by the integrator; the rung cannot see it because a train
        carries the lane REBASED, so ancestry says no and `merge-tree` composes
        a tree nobody ever ran. That seat's stops were refused for work a green
        train had already measured, with no discharge it could perform.

        THE CONTROL IS THE ARM BELOW and it differs in ONE fact: which lane the
        train carries. Same rooms, same leases, same receipts, same shared
        file."""
        self.claim("alpha", "s1")
        self.edit("alpha", "shared.py", at=1)
        self.green("alpha")
        # THE PRECONDITION THIS ARM DEPENDS ON — the train is NOT an ancestor
        # of the lane, so nothing here is discharged by the sha-identity path
        # that already worked — IS ASSERTED BY `compose_train` FOR EVERY
        # CALLER. It stood here as a second copy until the fixture's clock
        # race proved the rule belongs at the one door that composes trains:
        # a copy in one arm left every other arm free to measure the wrong
        # shape, which is what happened.
        train = self.compose_train("compose-train-155", "alpha")
        self.assertEqual(self.rows(), [],
                         "a green train carrying this very lane did not "
                         "discharge the seam it composes")
        self.assertTrue(os.path.isdir(train))

    def _one_second_train(self, stamp, train_stamp=None):
        """One whole fixture with EVERY git commit stamped at `stamp`.

        `train_stamp` DRIFTS THE TRAIN'S COMMITS ONLY, and exists so the
        precondition below can be shown to BITE. An arm that asserts a clock
        was frozen is worth exactly what its check can refuse, and nothing
        else here can make that check fail.

        THE CLOCK IS INJECTED AT GIT'S OWN ENVIRONMENT, NOT AT PYTHON'S TIME.
        `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` are the values that enter
        the commit object, so pinning them is the only way to make "the same
        second" a fact rather than a hope — patching `time` would leave git
        reading the real clock and the arm would pass by luck, exactly as the
        fixture did before this."""
        frozen = dict(os.environ, GIT_AUTHOR_DATE=stamp,
                      GIT_COMMITTER_DATE=stamp)
        with mock.patch.dict(os.environ, frozen, clear=True):
            self.claim("alpha", "s1")
            self.edit("alpha", "shared.py", at=1)
            self.green("alpha")
            # THE DRIFT, WHEN A CALLER ASKS FOR ONE: the lane's commit keeps
            # `stamp` and the train's take another, so the two land in
            # DIFFERENT seconds of the SAME DAY — the exact case a
            # day-resolution check waves through. Applied as a SECOND patched
            # dict rather than a bare assignment, because a bare
            # `os.environ[k] = v` is what the env-hygiene scanner reads as a
            # variable this module sets and never restores; `patch.dict`
            # restores it and says so in a form that pass can see.
            ts = train_stamp or stamp
            with mock.patch.dict(os.environ,
                                 dict(os.environ, GIT_AUTHOR_DATE=ts,
                                      GIT_COMMITTER_DATE=ts), clear=True):
                self.compose_train("compose-train-152", "alpha",
                                   repo_id="/nonexistent-origin-for-this-arm")
            # THE PRECONDITION THIS ARM OWNS, AND IT IS THE SECOND RATHER
            # THAN THE DAY. A `startswith(day)` check passes for two commits
            # made anywhere in the same twenty-four hours, which is not the
            # property this arm is named for and is satisfied by an unfrozen
            # clock on any single run.
            #
            # COMPARED AS EPOCH SECONDS, WHICH IS THE ONLY SPELLING NOBODY
            # ARGUES ABOUT. Two earlier cuts of this line were each wrong in a
            # way that looked like a broken cure rather than a broken check.
            # First `startswith(day)`, which an unfrozen clock satisfies. Then
            # a hand-built `stamp.replace(" +0000", "+00:00")` compared against
            # `%aI`, which renders a +0000 offset as `Z` — so the arm failed
            # while the property held. Parsing the ISO form instead walks into
            # a trap THIS TREE ALREADY DOCUMENTS at tests/test_doctor.py's
            # PY3.9/3.10 arm: `datetime.fromisoformat` REJECTS a trailing `Z`
            # until 3.11, and the floor here is 3.9 with CI running 3.9
            # through 3.13, so a green run on the gate's 3.14 would have said
            # nothing about the versions that matter. `%at` and `%ct` are
            # integer seconds on every version, and the expectation is derived
            # from the same timezone-aware parse of the stamp this fixture
            # SET. Requiring two values also requires both lines to EXIST, so
            # a format that stopped emitting one cannot pass either.
            want = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S %z")
            epoch = str(int(want.timestamp()))
            for ref in ("lane/alpha", "lane/compose-train-152"):
                r = _sh(self.root, "git", "log", "-1", "--format=%at%n%ct",
                        ref)
                self.assertEqual(r.returncode, 0, r.stderr)
                lines = r.stdout.splitlines()
                self.assertEqual(len(lines), 2,
                                 "git rendered %d timestamps for %s, not two"
                                 % (len(lines), ref))
                self.assertEqual(lines, [epoch, epoch],
                                 "the clock was not frozen on %s" % ref)
            self.assertNotEqual(self.tip("alpha"),
                                self.tip("compose-train-152"),
                                "one second produced one object")
            self.assertNotEqual(self.tree_of("alpha"),
                                self.tree_of("compose-train-152"))
            self.assertIn("lane/compose-train-152",
                          [r["peer_branch"] for r in self.rows()],
                          "the train composed inside one second lost the row "
                          "the rung must still raise")

    def test_a_train_composed_INSIDE_ONE_SECOND_is_still_a_rebased_sibling(  # noqa: VACUOUS_ASSERTION — every assertion lives one frame down in `_one_second_train`, which the walker does not follow: it pins the frozen stamp on BOTH commits, then distinct tips, distinct trees and the surviving row. Inlining them would make these two arms copies of each other, and the second timestamp is the whole point of the pair.
            self):
        """THE RACE THAT REDDENED THIS MODULE THREE GATES RUNNING, MADE
        DETERMINISTIC.

        A commit's sha is its content, and that content includes the second it
        was stamped in. While `compose_train` cherry-picked a lane's commit
        onto `main` — the lane's OWN parent — with the same message and the
        same `t <t@t>` identity, git produced the IDENTICAL OBJECT whenever
        both commits landed inside one second: the train then CONTAINED the
        lane by ancestry, the pair's merge-base became the lane's own tip, the
        lane's authored diff against that base was EMPTY, and the pair never
        formed. Same source, green on one run and red on the next.

        `compose_train` asserts the rebased-sibling shape for every caller;
        this arm is what makes that assertion meet the adversarial input,
        rather than whatever second the box happened to be in."""
        self._one_second_train("2026-01-02T03:04:05 +0000")

    def test_the_one_second_train_holds_at_a_SECOND_fixed_timestamp(self):  # noqa: VACUOUS_ASSERTION — every assertion lives one frame down in `_one_second_train`, which the walker does not follow: it pins the frozen stamp on BOTH commits, then distinct tips, distinct trees and the surviving row. Inlining them would make these two arms copies of each other, and the second timestamp is the whole point of the pair.
        """ONE FIXED SECOND IS ONE SAMPLE OF A PROPERTY THAT MUST HOLD FOR
        EVERY SECOND, and a single stamp can agree with a cure by accident —
        a value that happens to differ in one field. The second timestamp is
        in a different year, month, day, hour, minute and second, so a cure
        that depended on any one of them fails here and passes above."""
        self._one_second_train("2019-11-12T13:14:15 +0000")

    def test_the_frozen_clock_CHECK_refuses_a_one_second_drift_in_one_day(
            self):
        """THE RETAINED NEGATIVE CONTROL FOR THE PRECONDITION ITSELF, and it
        is here because the check it guards was wrong in exactly this way.

        The first cut asserted `line.startswith(day)`, which two commits made
        anywhere in the same twenty-four hours satisfy — including an
        UNFROZEN clock on any single run. So the arm that exists to prove the
        second was frozen proved only that the day was, and NOTHING COULD
        HAVE TOLD ME: every positive run passed either way. A check that
        cannot be made to fail is not a check, so this arm makes it fail, at
        the one-second resolution the positives claim, by drifting the
        TRAIN'S git committer environment one second forward INSIDE THE SAME
        DAY and requiring the specific refusal.

        THE POSITIVE IS ITS SIBLING ARMS, unchanged and running the same
        helper with no drift. Discrimination is measured here rather than
        inferred from a mutation nobody ran."""
        with self.assertRaises(AssertionError) as caught:
            self._one_second_train("2026-01-02T03:04:05 +0000",
                                   train_stamp="2026-01-02T03:04:06 +0000")
        # THE WHOLE SENTENCE, NAMING THE TRAIN. The drift is applied to the
        # TRAIN'S commits, so the TRAIN'S assertion is the one that must fire;
        # matching the prefix alone would also accept the LANE's copy of the
        # same message, which would mean the fixture broke somewhere upstream
        # and this control passed on a failure it is not about.
        self.assertIn("the clock was not frozen on lane/compose-train-152",
                      str(caught.exception),
                      "the fixture failed for some reason OTHER than the "
                      "train's drift, so this control proves nothing about "
                      "the check")

    def test_a_GREEN_TRAIN_that_does_NOT_carry_my_lane_is_STILL_a_seam(self):
        """The control on the clause above. Without it, clause 7 could be
        widened to "any green live peer discharges" and stay green."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", "shared.py", at=1)
        self.edit("beta", "shared.py", at=50)
        self.green("alpha")
        self.compose_train("compose-train-154", "beta")
        pairs = [(r["branch"], r["peer_branch"]) for r in self.rows()]
        self.assertIn(("lane/alpha", "lane/compose-train-154"), pairs,
                      "a train carrying somebody ELSE's lane discharged mine")

    def test_a_TRAIN_carrying_my_lane_with_NO_RECEIPT_discharges_nothing(self):
        """CONTAINMENT ALONE IS NOT THE DISCHARGE — the container's CURRENT
        tree carrying an admissible receipt is. A train that has been composed
        but not yet gated is the case this separates: the composition exists
        and nothing has run it, which is precisely what the rung blocks on.

        THE TRAIN'S RECEIPT IS UNPLACEABLE, which is the only fixture that
        holds containment TRUE while the container is not a same-repo green:
        its `repo_id` names a path that does not exist, so the tree ARMS the
        half and can never END a block. An UNGATED train would not do — it
        fails `_green_half`, the pair never forms, and the row's absence would
        prove nothing about clause 7."""
        self.claim("alpha", "s1")
        self.edit("alpha", "shared.py", at=1)
        self.green("alpha")
        # The unplaceable train must produce a row before the placeable
        # control is added. Adding that control clears only its own pair.
        self.compose_train("compose-train-152", "alpha",
                           repo_id="/nonexistent-origin-for-this-arm")
        self.assertIn("lane/compose-train-152",
                      [r["peer_branch"] for r in self.rows()],
                      "a container whose receipt cannot be placed in THIS "
                      "repository discharged the seam it composes")
        # THE POSITIVE ON THIS SAME FIXTURE, AND IT RUNS SECOND FOR THAT
        # REASON: a SAME-REPO green train carrying this lane DOES discharge
        # it, so the row above is about where the receipt can be placed and
        # not about containment failing here.
        #
        # THE UNPLACEABLE TRAIN'S ROW SURVIVES, AND THAT IS THE ASSERTION.
        # A discharge is per PAIR, never per lane: the placeable train clears
        # the pair IT composes and says nothing about the pair the unplaceable
        # one composes, which is still a composition nobody can prove was run
        # in this repository. Asserting an EMPTY list here would have demanded
        # that one green train retire every other train's untested
        # composition, which is the laundering clause 7 exists to refuse.
        self.compose_train("compose-train-153", "alpha")
        self.assertEqual(["lane/compose-train-152"],
                         [r["peer_branch"] for r in self.rows()],
                         "a same-repo green train carrying this lane left its "
                         "OWN pair standing, or cleared a pair it does not "
                         "compose")

    def test_a_RED_run_on_the_composed_tree_discharges_nothing(self):
        self.two_green_halves()
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree, status="FAILED")
        self.assertEqual([r["peer_branch"] for r in self.rows()],
                         ["lane/beta"],
                         "a FAILED composed run was read as an arm that passed")

    def test_a_Seam_trailer_DISCHARGES_NOTHING_now(self):
        """THE DELETION, PROVEN — and this arm is why the six trailer arms that
        stood here are gone rather than quietly dropped.

        Trailer discharge was killed in review: PROSE CANNOT PROVE
        TESTING OR ACCOUNTABLE TRANSFER. A bare `Seam: peer` cleared the
        block; the same token plus three filler words cleared it; the
        next iteration would have been whitespace, because every cure was
        SHAPE-based where the property is CONTENT-based.

        The six deleted arms asserted the OPPOSITE of this one — that a
        well-formed trailer discharges — so they could not be repaired, only
        removed. Removing arms is how a suite is made green by weakening it, so
        the deletion owes exactly this: the strongest trailer any of them used,
        now proving it clears nothing. If someone re-adds trailer discharge,
        this arm goes red.
        """
        self.two_green_halves()
        self.assertTrue(self.rows(), "precondition: the seam must exist first")
        # the STRONGEST form the deleted arms accepted: exact token, exact peer,
        # a named owner and a stated when.
        self.trailer("alpha",
                     "Seam: lane/beta — s1 composes and gates before land")
        self.assertEqual([r["peer_branch"] for r in self.rows()],
                         ["lane/beta"],
                         "a commit trailer still discharged the seam")

    # ── UNKNOWN ───────────────────────────────────────────────────────────
    def test_an_unreadable_receipt_ledger_is_UNKNOWN_never_an_all_clear(self):
        self.two_green_halves()
        with mock.patch.object(gate, "receipts",
                               return_value=([], "checksum mismatch", 0)):
            rows, err, _warn = _work_gc.seam_candidates(
                self.root, {self.path("alpha")}, holder="s1")
        self.assertEqual(rows, [])
        self.assertIn("UNKNOWN", err)

class HolderTest(SeamBase):
    """WHO holds a room — the tri-state, and the leg order that survives helm's
    own liveness premise.

    `seat-liveness-is-environ-not-cwd-and-not-comm` is explicit that reading
    HELM_CHAT_NAME from /proc "cannot see a claude-native seat AT ANY INSTANT,
    because those seats never declare that variable", and an environ-only
    holder would have been that defect. Measured on the live board 2026-08-23:
    this lane's own room shows four occupying pids and ZERO declared names —
    the seat running the measurement is exactly the seat the probe cannot see —
    and it resolves anyway, from its LEASE. These arms pin that order."""

    def rooms(self):
        rooms, err, _degraded = _work_gc.seam_rooms(self.root)
        self.assertIsNone(err)
        return {r["path"]: r for r in rooms}

    def test_the_LEASE_names_a_holder_no_environ_scan_could_find(self):  # noqa: VACUOUS_ASSERTION — the empty `seats` and the resolved holder are two fields of ONE bound row, so the positive control is the very next line on the same observable
        """THE PREMISE'S OWN HOLE, SEEDED WITH THE FAMILY THAT FALLS IN IT. No
        roster row, no declared name — exactly a claude-native seat — and the
        holder resolves because the lease leg runs first."""
        self.claim("alpha", "s1")
        r = self.rooms()[self.path("alpha")]
        self.assertEqual(r["seats"], [],
                         "precondition: nothing declares a seat in this room")
        self.assertEqual((r["holder"], r["holder_why"]), ("s1", "lease"))

    def test_the_ROSTER_names_a_holder_when_no_lease_does(self):  # noqa: VACUOUS_ASSERTION — the assertion IS the positive one — a named holder and a named leg; there is no absence here for a control to answer
        """helm's own seat surface, which the premise says outranks any process
        scan — and the only leg that covers the claude-native family."""
        self.room("beta")
        self.roster("bruce", self.path("beta"))
        r = self.rooms()[self.path("beta")]
        self.assertEqual((r["holder"], r["holder_why"]), ("bruce", "roster"))

    def test_a_roster_row_OUTSIDE_every_room_names_nobody(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and immediately below: the same seat moved INSIDE the room resolves, which needs a second call by construction
        """Longest-prefix, and a seat sitting in $HOME (two real ones do)
        belongs to no room. Without this the roster leg would attribute every
        homeless seat to whichever room happened to sort first."""
        self.room("beta")
        self.roster("bruce", self.tmp)
        r = self.rooms()[self.path("beta")]
        self.assertEqual(r["seats"], [])
        self.assertEqual((r["holder"], r["holder_why"]), (None, "unknown"))
        # POSITIVE CONTROL: the same seat moved INSIDE the room does resolve,
        # so the silence above is the prefix test and not a dead roster read.
        self.roster("bruce", self.path("beta"))
        self.assertEqual(self.rooms()[self.path("beta")]["holder"], "bruce")

    def test_a_roster_row_in_a_NESTED_room_belongs_to_the_INNER_one(self):
        """The shared checkout contains every lane room by path, so a
        shortest-prefix match would attribute all of them to the checkout and
        make every seat a co-tenant of everything."""
        self.room("beta")
        self.roster("bruce", os.path.join(self.path("beta"), "sub", "dir"))
        rooms = self.rooms()
        self.assertEqual(rooms[self.path("beta")]["seats"], ["bruce"])
        self.assertEqual(rooms[self.root]["seats"], [])

    def test_TWO_seats_in_ONE_room_is_SHARED_not_unknown(self):  # noqa: VACUOUS_ASSERTION — the None here is asserted BESIDE a populated seats list and a distinct holder_why, so it is not an empty observable
        """THE TRI-STATE, and the reason it exists. `holder=None` had two
        opposite causes and an earlier cut collapsed them: this lane's room
        declares NOTHING (the claude-native hole) while helm's SHARED CHECKOUT
        declares three at once. Absence and disagreement are different facts
        and the second is a signal in its own right."""
        self.room("beta")
        self.roster("bruce", self.path("beta"))
        self.roster("bella", self.path("beta"))
        r = self.rooms()[self.path("beta")]
        self.assertEqual(r["seats"], ["bella", "bruce"])
        self.assertIsNone(r["holder"], "two seats cannot elect one holder")
        self.assertEqual(r["holder_why"], "shared")

    def test_a_room_nothing_can_speak_for_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the control is the SHARED and LEASE arms above, which prove the same field carries other values
        self.room("beta")
        r = self.rooms()[self.path("beta")]
        self.assertEqual((r["holder"], r["holder_why"], r["seats"]),
                         (None, "unknown", []))


class ExemptionTest(SeamBase):
    """An UNKNOWN holder must not buy the same-seat exemption."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def test_an_UNKNOWN_peer_holder_does_NOT_buy_the_exemption(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — rostering the peer as me is the state change the pair straddles, and one call cannot be read under both
        """Skipping a peer is an EXCLUSION, and helm's liveness premise states
        the rule for those: "exclusion ... must be earned by a POSITIVE signal
        ..., never by silence or by inequality." So a peer helm cannot name is
        KEPT and the rung stays wide where it cannot tell. Cost of keeping: one
        dischargeable block on a seam that may be one seat's own. Cost of
        dropping: silence on the exact case the rung exists for — and only the
        second failure is invisible."""
        self.claim("alpha", "s1")
        self.room("beta")                    # registered, unleased, unrostered
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({self.path("beta"): ["4242"]},
                                             True)):
            rows = self.rows(holder="s1")
            self.assertEqual([r["peer_holder"] for r in rows], [None],
                             "precondition: the peer's holder must be UNKNOWN")
            self.assertEqual([r["peer_branch"] for r in rows], ["lane/beta"])
        # THE CONTROL, and it moved from the roster to the LEASE deliberately:
        # a roster row is no longer allowed to exempt anything (a stale one was
        # dropping live peers), so the control uses the present-tense signal
        # that IS allowed to. Same fixture otherwise, so the block above stays
        # the unknown-never-exempts rule and not a case that could never be
        # exempted at all.
        self.work("claim", "beta", "--seat", "s1")
        self.assertEqual(self.rows(holder="s1"), [])

    def test_a_SHARED_peer_room_does_not_buy_the_exemption_either(self):  # noqa: VACUOUS_ASSERTION — same shape: dropping the co-tenant is the state change, so the exempting control is necessarily a second call
        """Two seats in the peer's room means no single seat holds it — which
        is emphatically not proof that I do. Even when *I* am one of the two:
        my presence in a crowded room does not make the room's other half mine.

        The room is made live by OCCUPANCY, not by the roster: a roster row
        never makes a room live (rows outlive their processes), so the mock is
        what supplies the positive present-tense signal the peer needs."""
        self.claim("alpha", "s1")
        self.room("beta")
        self.roster("s1", self.path("beta"))
        self.roster("someone-else", self.path("beta"))
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({self.path("beta"): ["4242"]},
                                             True)):
            rows = self.rows(holder="s1")
            self.assertEqual([r["peer_holder"] for r in rows], [None],
                             "precondition: a shared room elects no holder")
            self.assertEqual([r["peer_branch"] for r in rows], ["lane/beta"])
        # THE CONTROL, on the lease for the same reason as its sibling: a
        # roster-elected holder may NAME but never EXCLUDE, so the exemption is
        # demonstrated with the one signal permitted to grant it.
        self.work("claim", "beta", "--seat", "s1")
        self.assertEqual(self.rows(holder="s1"), [])

    def test_a_KNOWN_matching_holder_still_exempts(self):  # noqa: VACUOUS_ASSERTION — the control varies the HOLDER argument, so it is a second call by construction — one call cannot be read under two holders
        """The control on both arms above: the exemption is not dead code."""
        self.claim("alpha", "s1")
        self.claim("beta", "s1")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(holder="s1"), [])
        self.assertEqual([r["peer_branch"] for r in self.rows(holder="s9")],
                         ["lane/beta"])


class BlindSpotTest(SeamBase):
    """The disclosure the rung owes the case it CANNOT see.

    This rung keys on the worktree, so two seats sharing one room produce no
    comparable pair and are invisible to it by construction — and helm's own
    shared checkout is exactly that room: the roster puts four seats
    (helm-claude, helm-claude-2, helm-claude-3, opus-integrator) inside it.
    Acting on that belongs to another mechanism; SAYING it belongs here,
    because a can-tell-nothing must never read as a clean bill."""

    def share(self, lane="alpha", who=("ann", "bob")):
        path = self.claim(lane, "s1")
        for name in who:
            self.roster(name, path)
        return path

    def test_a_shared_room_WARNS_that_the_rung_is_blind_inside_it(self):  # noqa: VACUOUS_ASSERTION — the four assertIn lines on the same joined warn ARE the positive control; the one assertEqual is the not-a-block law on a different channel
        room = self.share()
        blocks, warns = self.guard(cwd=room)
        joined = "\n".join(warns)
        self.assertIn("SEAM RUNG BLIND SPOT", joined)
        self.assertIn("2 seats", joined)
        self.assertIn("ann", joined)
        self.assertIn("bob", joined)
        self.assertIn("not a clean bill", joined)
        self.assertEqual([b for b in blocks if "BLIND SPOT" in b], [],
                         "a blind spot is not evidence of a defect; it may "
                         "never wall a turn")

    def test_a_room_with_ONE_seat_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the control is the two-seat arm above, and the second half here is an unconditional positive on the same observable
        """The must-miss. Every ordinary lane room has one occupant, so a rung
        that fired here would be on every stop of every seat."""
        room = self.share(who=("ann",))
        self.assertNotIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]))
        # POSITIVE CONTROL: a second seat arriving in that same room speaks.
        self.roster("bob", room)
        self.assertIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]))

    def test_a_shared_room_that_is_NOT_MINE_says_nothing(self):  # noqa: VACUOUS_ASSERTION — the control is the same arm's second half, standing IN the shared room
        """Scoped to the room the stopping seat is standing in. Somebody else's
        crowded room is not this seat's blind spot, and disclosing every one of
        them would be the wallpaper the ladder refuses."""
        mine = self.claim("alpha", "s1")
        theirs = self.claim("beta", "s2")
        for name in ("ann", "bob"):
            self.roster(name, theirs)
        self.assertNotIn("BLIND SPOT", "\n".join(self.guard(cwd=mine)[1]))
        # CONTROL: standing in that same crowded room DOES disclose it.
        self.assertIn("BLIND SPOT",
                      "\n".join(self.guard(seat="s2", session="s-b",
                                           cwd=theirs)[1]))

    def test_it_speaks_ONCE_per_arrangement_and_a_new_cotenant_re_arms_it(self):
        room = self.share()
        self.assertIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]))
        for _ in range(3):
            self.assertNotIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]),
                             "the same arrangement spoke twice")
        self.roster("cass", room)
        joined = "\n".join(self.guard(cwd=room)[1])
        self.assertIn("BLIND SPOT", joined)
        self.assertIn("3 seats", joined)

    def test_a_SUPPRESSED_disclosure_is_still_owed_at_the_next_stop(self):
        """THE BUG THIS RUNG SHIPPED, and it needed two correct decisions.

        `seats_cli` suppresses advisories on the exit-2 branch — Claude Code
        renders every exit-2 stop-hook emission as a red "Stop hook error:",
        and the owner watched that line all evening on 2026-07-31 — and
        justifies it by promising the advisory is RECOMPUTED on the re-stop.
        The disclosure used to latch while CONSTRUCTING itself, so a stop where
        another rung blocked spent the arrangement on a line that exit never
        printed, and the re-stop matched the latch and said nothing FOREVER.
        Neither decision was wrong alone.

        So: a stop that did not emit must leave the arrangement OWED, and the
        next stop that does emit must speak. The `emitted=False` guard is the
        exit-2 branch; the sibling arm above is the positive control that a
        committed arrangement goes quiet, so this is not merely 'it always
        speaks'."""
        room = self.share()
        for _ in range(3):            # three blocked stops in a row
            blocked = "\n".join(self.guard(cwd=room, emitted=False)[1])
            self.assertIn("BLIND SPOT", blocked,
                          "a suppressed stop consumed the arrangement")
        self.assertIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]),
                      "the arrangement was never disclosed on an ALLOW exit")
        self.assertNotIn("BLIND SPOT", "\n".join(self.guard(cwd=room)[1]),
                         "it kept speaking after a real emission latched it")

    def test_the_latch_is_ABSENT_until_the_line_is_emitted(self):
        """The mechanism under the behaviour, asserted on the FILE.

        The arm above reads the rendered text, which cannot distinguish 'not
        latched' from 'latched somewhere else'. This one names the artifact:
        after building the line the latch must not exist, and after the commit
        it must."""
        room = self.share()
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        blocks, warns = seats.stop_guard(session="sess-1", room="main",
                                         seat="s1", cwd=room)
        self.assertIn("BLIND SPOT", "\n".join(warns), "fixture never disclosed")
        armed = list(seats_stop_seam._PENDING_DISCLOSURES)
        self.assertEqual(len(armed), 1, "expected exactly one armed latch")
        path, _fp, text, armed_seat, armed_session, incarnation = armed[0]
        self.assertEqual((armed_seat, armed_session), ("s1", "sess-1"))
        self.assertTrue(incarnation)
        self.assertIn("BLIND SPOT", text,
                      "the arming carries the text it must be seen in")
        self.assertFalse(os.path.exists(path),
                         "the latch was written while BUILDING the line")
        self.assertEqual(seats_stop_seam.commit_disclosures("\n".join(warns)),
                         1)
        self.assertTrue(os.path.exists(path), "commit did not write the latch")
        self.assertEqual(seats_stop_seam.commit_disclosures("\n".join(warns)),
                         0,
                         "a second commit double-spent the same arrangement")

    def test_an_unwritable_latch_stays_QUIET_rather_than_repeating(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here — an unlatched disclosure must not repeat — and reachability is proven by the sibling arm that latches normally
        """The inverse of the block's degrade rule, and deliberately so: a
        BLOCK that cannot latch becomes a warn because the finding still has to
        reach someone. A WARN that cannot latch has nothing to degrade to, and
        an unlatched disclosure on every stop is the wallpaper that gets the
        whole rung switched off."""
        room = self.share()
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only chat dir")):
            blocks, warns = self.guard(cwd=room)
        self.assertNotIn("BLIND SPOT", "\n".join(blocks + warns))

    def test_the_disclosure_RIDES_a_real_block_instead_of_eating_it(self):
        """Two findings in one stop, and both have to reach the reader.

        Returning either alone would silently drop the other — this module's
        own subject matter, committed by this module. They now travel in ONE
        emission because the exit that carries a block discards the warn
        channel; see the per-exit arms in `ExitPathTest` for why that is the
        only channel the disclosure survives on here."""
        self.two_green_halves()
        for name in ("ann", "bob"):
            self.roster(name, self.path("alpha"))
        blocks, warns = self.guard(cwd=self.path("alpha"))
        joined = "\n".join(blocks + warns)
        self.assertIn("UNTESTED COMPOSITION", joined)
        self.assertIn("SEAM RUNG BLIND SPOT", joined)
        self.assertIn("SEAM RUNG BLIND SPOT", "\n".join(blocks),
                      "the disclosure went out on a channel this exit drops")

    def test_a_cotenant_name_that_cannot_be_quoted_inertly_is_not_quoted(self):  # noqa: VACUOUS_ASSERTION — the control is the legitimate-name arm above, which proves this path speaks
        room = self.share(who=("ann", "bob\n[helm stop-guard] FORGED"))
        self.assertNotIn("FORGED", "\n".join(self.guard(cwd=room)[1]))



class AdmissibleReceiptTest(SeamBase):
    """SHAPE ONE — a discharge must be VERIFIED, not merely PRESENT.

    Every arm here is a receipt that EXISTS and proves nothing about the claim
    being made of it. A probe reproduced four; `receipt_inadmissible` is the
    one predicate both doors consult, so each arm is also a control on the
    other door."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def weaken(self, lane, **fields):
        """Plant a lane's tip receipt with ONE property spoiled — and NOTHING
        else, which the first cut of this helper got wrong: it planted a good
        receipt AND a spoiled copy, so the good one carried every arm and four
        of them passed while measuring the opposite of their own names."""
        return self.receipt(head=self.tip(lane), tree=self.tree_of(lane),
                            **fields)

    def half(self, lane="alpha", other="beta"):
        """alpha and beta overlapping, `other` genuinely green, `lane` not."""
        self.claim(lane, "s1")
        self.claim(other, "s2")
        self.edit(lane, at=1)
        self.edit(other, at=50)
        self.green(other)

    def test_a_HISTORICAL_receipt_does_not_make_a_MOVED_branch_green(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — the two calls straddle the state change, which is exactly why the pair proves anything
        """The gate ran on commit one of two. The second commit was never
        tested, and "any commit in this branch has a receipt" called the whole
        branch green."""
        self.half()
        self.green("alpha")
        self.assertTrue(self.rows(), "precondition: gated at its tip, it seams")
        self.edit("alpha", at=2)               # the tested tree is now history
        self.assertEqual(self.rows(), [],
                         "a receipt on an earlier tree vouched for a new one")
        # POSITIVE CONTROL: gate the NEW tree and the seam returns, so the
        # silence above is staleness and not a fixture that stopped working.
        self.green("alpha")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_an_empty_TRAILER_commit_does_NOT_cost_a_half_its_greenness(self):  # noqa: VACUOUS_ASSERTION — the two preconditions above are unconditional positives on the same tip, and the final assertion is itself the positive (a named peer, not a count)
        """THE REASON THE KEY IS A TREE AND NOT A HEAD. The discharge this rung
        prescribes is an empty commit, which moves the head and leaves the tree
        alone — so a head-keyed staleness test would have made TAKING THE CURE
        look like losing the greenness, and the rung would have gone quiet for
        the wrong reason and called it a discharge."""
        self.two_green_halves()
        head_before = self.tip("alpha")
        self.trailer("alpha", "Seam: lane/nobody — unrelated note about work")
        self.assertNotEqual(self.tip("alpha"), head_before,
                            "precondition: the empty commit moved the head")
        self.assertEqual(self.tree_of("alpha"),
                         _sh(self.root, "git", "rev-parse",
                             head_before + "^{tree}").stdout.strip(),
                         "precondition: and left the tree alone")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"])

    def test_a_FOCUSED_receipt_is_inadmissible(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — the two calls straddle the state change, which is exactly why the pair proves anything
        """A focused claim is its SCOPE, not its tree — which is why a focused
        receipt cannot authorize a land, and equally cannot stand for "this
        half passed everything"."""
        self.half()
        self.weaken("alpha", suite=False, focus={"selected": ["tests.x"]})
        self.assertEqual(self.rows(), [], "a focused receipt was read as green")
        self.green("alpha")
        self.assertTrue(self.rows(), "a whole-suite receipt must still count")

    def test_a_DIRTY_receipt_is_inadmissible(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — the two calls straddle the state change, which is exactly why the pair proves anything
        self.half()
        self.weaken("alpha", dirty=True, dirty_after=True)
        self.assertEqual(self.rows(), [], "a dirty-tree receipt was read green")
        self.green("alpha")
        self.assertTrue(self.rows())

    def test_a_receipt_whose_tree_MOVED_under_the_run_is_inadmissible(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — the two calls straddle the state change, which is exactly why the pair proves anything
        """The clean bracket. If the tree changed while the suite ran, the
        recorded tree is not the tree that was tested."""
        self.half()
        self.weaken("alpha", tree_after="9" * 40, head_after="9" * 40)
        self.assertEqual(self.rows(), [], "a moved-tree receipt was read green")
        self.green("alpha")
        self.assertTrue(self.rows())

    def test_the_same_admissibility_guards_the_DISCHARGE_door(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts the seam SURVIVES (a positive), and the unconditional control after the loop is an admissible receipt that clears it
        """ONE PREDICATE, TWO DOORS. A weak receipt at the COMPOSED tree must
        no more discharge the seam than a weak one at a branch tip makes a half
        green — findings 1 and 6 were the same defect at the two doors."""
        self.two_green_halves()
        _rc, tree = self.merged_tree("alpha", "beta")
        for spoil in ({"suite": False, "focus": {"selected": []}},
                      {"dirty": True, "dirty_after": True},
                      {"tree_after": "9" * 40, "head_after": "9" * 40}):
            with self.subTest(**spoil):
                self.receipt(head="c" * 40, tree=tree, **spoil)
                self.assertTrue(self.rows(),
                                "a weak receipt at the composed tree cleared "
                                "the seam: %s" % spoil)
        # POSITIVE CONTROL: an ADMISSIBLE receipt at that same tree discharges.
        self.receipt(head="c" * 40, tree=tree)
        self.assertEqual(self.rows(), [])

    # ── WEAK: a receipt that exists and measured less than it claims ──────
    def test_a_receipt_that_executed_ZERO_tests_is_not_a_green_half(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL: the same fixture regated with a real count is the unconditional positive below
        """The purest vacuity available to a receipt: a whole-suite run that
        passed and ran nothing. `status OK` plus `suite true` said everything
        passed; `ran` said the set was empty, and nobody asked it."""
        self.half()
        self.weaken("alpha", ran=0)
        self.assertEqual(self.rows(), [],
                         "a run that executed nothing was read as green")
        self.green("alpha")
        self.assertTrue(self.rows(), "a real count does not green the half")

    def test_a_receipt_whose_ARGV_names_one_module_is_not_whole_suite(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL: the discovery-shaped argv on the next lines is the unconditional positive
        """`suite: true` is a CLAIM and the argv is the RUN. Nothing compared
        them, so a scoped command wearing the whole-suite flag was read as
        "everything passed" — a weak receipt laundered by one boolean."""
        self.half()
        self.weaken("alpha",
                    argv=["python3", "-m", "unittest", "tests.test_one"])
        self.assertEqual(self.rows(), [],
                         "a one-module run wore the whole-suite word")
        self.green("alpha")
        self.assertTrue(self.rows())

    def test_helms_OWN_gaterunner_discovery_is_still_a_whole_suite_run(self):
        """The must-hit on the clause above. Stored ledgers hold honest
        whole-suite receipts minted through the legacy
        `-m helm.gaterunner discover` as well as `-m unittest`, and the
        anchored form of gate's parser refuses them. A clause that also refused
        those would have been a silent narrowing dressed as a cure."""
        self.half()
        self.weaken("alpha", argv=["/usr/bin/python3.14", "-m",
                                   "helm.gaterunner", "discover", "-s",
                                   "tests", "-t", "."])
        self.assertEqual([r["peer_branch"] for r in self.rows()],
                         ["lane/beta"],
                         "a gaterunner discovery was refused as scoped")

    # ── the stored-suite RUNNER SET: two modules, and nothing else ────────
    def doors(self, argv):
        """What a receipt carrying `argv` DOES at the rung's two doors.

        -> (armed, honest, discharged), each a WORD rather than a boolean, so
        a wrong reading prints as itself:
          armed       "armed" | "not armed"
          honest      the peer branches the rung names once BOTH halves carry
                      an honest receipt — the must-hit, handed back rather
                      than asserted here so every caller states it in its own
                      body and no arm inherits a control it cannot see
          discharged  "discharged" | "not discharged"

        BOTH DOORS ON ONE ARGV, because a command module the rung reads as
        whole-suite can make a half green AND end a block, and a predicate
        that merely returns False proves neither. The reading is the rung's
        own rows: at door one alpha's only receipt carries `argv`, so a seam
        appears exactly when that receipt armed the half; at door two both
        halves are honestly green and a receipt carrying `argv` sits at the
        composed tree, so the seam disappears exactly when it discharged."""
        self.half()                       # beta green, alpha not
        self.weaken("alpha", argv=list(argv))
        armed = "armed" if self.rows() else "not armed"
        self.green("alpha")
        honest = [r["peer_branch"] for r in self.rows()]
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree, argv=list(argv))
        return (armed, honest,
                "discharged" if not self.rows() else "not discharged")

    def test_a_stored_unittest_discovery_ARMS_and_DISCHARGES_the_rung(self):
        """POSITIVE CONTROL ONE on the runner set. `-m unittest` discovery is
        the command the gate itself spawns, so a restriction that lost it
        would have made every honest receipt in the fleet unreadable."""
        verdict = self.doors(["/usr/bin/python3.14", "-m", "unittest",
                              "discover", "-s", "tests", "-t", "."])
        self.assertEqual(verdict, ("armed", ["lane/beta"], "discharged"))

    def test_a_stored_gaterunner_discovery_ARMS_and_DISCHARGES_the_rung(self):
        """POSITIVE CONTROL TWO. Stored ledgers hold whole-suite receipts
        minted through the legacy `-m helm.gaterunner discover`, accepted and
        never produced (gate._STORED_SUITE_RUNNERS); the runner set is two
        modules and this is the second."""
        verdict = self.doors(["/usr/bin/python3.14", "-m", "helm.gaterunner",
                              "discover", "-s", "tests", "-t", "."])
        self.assertEqual(verdict, ("armed", ["lane/beta"], "discharged"))

    def test_an_ARBITRARY_command_module_NEITHER_arms_NOR_discharges(self):
        """THE RESTRICTION ITSELF. Asking only whether the recorded command
        named explicit test ids admitted `python3 -m <anything> discover`, so
        a receipt whose argv names a module that runs no tests at all — or
        runs something else entirely — read as a whole-suite verification and
        could arm a half or end a block. A whole-suite CLAIM is only as good
        as the runner that could have produced it, so the set of command
        modules a stored receipt may name is closed: unittest, or the legacy
        gaterunner.

        The middle term is this arm's own must-hit: between the two doors the
        fixture DOES seam and names the peer, so neither refusal is a lane
        that had gone quiet."""
        verdict = self.doors(["python3", "-m", "not.a.test.runner",
                              "discover"])
        self.assertEqual(verdict,
                         ("not armed", ["lane/beta"], "not discharged"))
        # AND THE DISCHARGE DOOR ITSELF STILL OPENS: an admissible receipt at
        # that SAME composed tree ends the block, so the survival above is the
        # module name and not a door that had stopped opening. Read as ONE
        # before/after pair, because the named peer is what makes the silence
        # after it mean something.
        before = [r["peer_branch"] for r in self.rows()]
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree)
        self.assertEqual((before, [r["peer_branch"] for r in self.rows()]),
                         (["lane/beta"], []),
                         "the composed-tree receipt stopped discharging")

    def test_a_receipt_that_says_it_did_not_EXECUTE_is_inadmissible(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL: the same row with executed True is the unconditional positive below
        """The imported v5 shape carries the runner's own admission. Only
        those rows have the field, so it is asked ONLY when present — a
        version that never spoke is not a version that confessed."""
        self.half()
        self.weaken("alpha", executed=False)
        self.assertEqual(self.rows(), [])
        self.green("alpha")
        self.assertTrue(self.rows())

    # ── the THREE states a canonical field can be in ─────────────────────
    def test_a_receipt_MISSING_a_canonical_field_is_refused_BY_NAME(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST — the intact row is ADMITTED — so the loop cannot be satisfied by a predicate that refuses everything
        """STATE ONE: ABSENT. `row.get("dirty")` on a row with no `dirty` key
        is None, which is falsy, which passed the cleanliness test — so a row
        that never recorded whether the worktree was clean was read as a row
        recording it CLEAN. Same shape at `rc`. The reader was consuming its
        own default and calling the result evidence."""
        base = self.receipt(append=False)
        # UNCONDITIONAL CONTROL FIRST. Without it a predicate that refused
        # every row would satisfy every subTest below, and the arm would be
        # measuring a broken guard rather than a cured one.
        self.assertIsNone(gate.row_refusal(base, about="deadbeefdead"),
                          "the INTACT row is refused — the loop below cannot "
                          "discriminate")
        for field in ("id", "head", "tree", "dirty", "status", "rc"):
            with self.subTest(field=field):
                row = {k: v for k, v in base.items() if k != field}
                refusal = gate.row_refusal(row, about="deadbeefdead")
                self.assertIsNotNone(
                    refusal, "a receipt with no %r field was admitted" % field)
                self.assertIn(repr(field), refusal,
                              "the refusal does not name the missing field")

    def test_a_receipt_whose_field_is_PRESENT_and_BAD_is_refused(self):  # noqa: VACUOUS_ASSERTION — same shape: the intact row is admitted unconditionally before the loop, which is the positive on this accessor
        """STATE TWO: PRESENT-AND-BAD. The same fields, carried and wrong. Two
        of these were already refused and two were not; all four must be, and
        by a DIFFERENT sentence from the absent case, or the two states have
        one representation again."""
        base = self.receipt(append=False)
        self.assertIsNone(gate.row_refusal(base, about="deadbeefdead"),
                          "the INTACT row is refused — the loop below cannot "
                          "discriminate")
        for tag, spoil in (("dirty", {"dirty": True}),
                           ("dirty_after", {"dirty_after": True}),
                           ("rc-nonzero", {"rc": 7}),
                           ("rc-null-on-an-OK-row", {"rc": None}),
                           ("moved", {"tree_after": "9" * 40})):
            with self.subTest(tag):
                refusal = gate.row_refusal(dict(base, **spoil),
                                           about="deadbeefdead")
                self.assertIsNotNone(refusal, "%s was admitted" % tag)
                self.assertNotIn("carries no", refusal,
                                 "a present-but-bad field was reported as "
                                 "missing — two states, one sentence")

    def test_a_receipt_with_every_canonical_field_GOOD_is_admitted(self):  # noqa: VACUOUS_ASSERTION — an admission IS a None here, and both accessors are asked unconditionally about the SAME row with one byte changed and DO speak
        """STATE THREE: PRESENT-AND-GOOD, and it is the arm that keeps the two
        above from being a predicate that refuses everything. Without it, a
        cure that returned a refusal unconditionally would pass both."""
        good = self.receipt(append=False)
        self.assertIsNone(gate.row_refusal(good, about="deadbeefdead"))
        self.assertIsNone(_work_gc.receipt_inadmissible(good))
        # AND THE SAME TWO ACCESSORS DO SPEAK, unconditionally, on this very
        # row with one byte changed — so the two silences above are an
        # admission and not a predicate that has stopped answering.
        self.assertIsNotNone(gate.row_refusal(dict(good, dirty=True),
                                              about="deadbeefdead"))
        self.assertIsNotNone(_work_gc.receipt_inadmissible(
            dict(good, ran=0)))


class ReceiptProvenanceTest(SeamBase):
    """SHAPE TWO — evidence about ANOTHER repository is not weak evidence
    about this one.

    The gate receipt ledger is GLOBAL: one file for every repository on the
    box, and measured on the live 2,439-row ledger it holds 898 distinct
    `repo_id` values of which THREE resolve to this repository. Nothing read
    that field, so a row minted anywhere could green a half here and — worse —
    end a block here. The tier probe that used to sit at this door asked
    `cat-file` whether a tree object resolved, which a shared object store
    answers YES to for reasons that have nothing to do with provenance.

    Three states, and the two doors take them differently: see
    `green_receipts` for why one filter cannot serve both."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def elsewhere(self):
        """A SECOND REAL REPOSITORY on disk — not a made-up path, because a
        path that does not exist is the UNPLACEABLE state and would measure a
        different clause than the one this arm names."""
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        self.assertEqual(_sh(other, "git", "init", "-q", "-b",
                             "main", ".").returncode, 0)
        return other

    def mine(self):
        from helm import rowworld
        ident, err = rowworld.repo_identity(self.root)
        self.assertIsNone(err)
        return ident

    def test_a_receipt_minted_HERE_may_arm_and_may_discharge(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL and unconditional — assertIs(True) and assertTrue(rows) run before the composed receipt is planted, on the same two accessors
        """STATE ONE: PROVABLY THIS REPOSITORY. A peer WORKTREE counts — the
        identity is the common git dir, which every worktree shares — and this
        is also the control that keeps the two arms below from passing on a
        filter that refuses everything."""
        self.two_green_halves()
        row = self.receipt(repo_id=self.path("beta"), append=False)
        self.assertIs(_work_gc.receipt_repo_scope(row, self.mine(), {}), True)
        self.assertTrue(self.rows(), "a same-repo half stopped arming")
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree, repo_id=self.path("beta"))
        self.assertEqual(self.rows(), [],
                         "a same-repo composed receipt did not discharge")

    def test_import_binding_turns_reaped_origin_into_local_discharge_authority(self):
        gone = os.path.join(self.tmp, "remote-reaped-scratch")
        head = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        tree = _sh(self.root, "git", "rev-parse", "HEAD^{tree}").stdout.strip()
        row = self.receipt(head=head, tree=tree, repo_id=gone, append=False)
        self.assertTrue(eventledger.append(gate.receipts_path(), row))
        trees, local, err, _warn = _work_gc.green_receipts(self.root)
        self.assertIsNone(err)
        self.assertIn(tree, trees)
        self.assertNotIn(tree, local)

        artifact = os.path.join(self.tmp, "remote-receipt.jsonl")
        with open(artifact, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        got, verdict, err = gateimport.import_receipt(artifact, self.root)
        self.assertEqual((got["id"], verdict, err),
                         (row["id"], "imported", None))
        self.assertTrue(eventledger.append(
            gateimport.imports_path(),
            {"event": "gate-import", "ts": "2026-08-26T00:00:00Z",
             "receipt": "legacy-malformed", "repo": "bad\0path"}))
        trees, local, err, warning = _work_gc.green_receipts(
            self.root,
            binding_budget_s=gateimport.BINDING_CANDIDATE_BUDGET_S)
        self.assertIsNone(err)
        self.assertIn("legacy placement is UNKNOWN", warning or "")
        self.assertIn(tree, local,
                      "legacy UNKNOWN erased canonical discharge authority")

    def test_a_receipt_from_ANOTHER_repository_neither_arms_nor_discharges(self):
        """STATE TWO: PROVABLY ELSEWHERE. Not weak evidence — evidence about a
        different repository, which is why it is in NEITHER set rather than in
        the permissive one."""
        other = self.elsewhere()
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        for lane in ("alpha", "beta"):
            self.receipt(head=self.tip(lane), tree=self.tree_of(lane),
                         repo_id=other)
        row = self.receipt(repo_id=other, append=False)
        self.assertIs(_work_gc.receipt_repo_scope(row, self.mine(), {}), False)
        trees, local, err, _warn = _work_gc.green_receipts(self.root)
        self.assertIsNone(err)
        self.assertNotIn(self.tree_of("alpha"), trees,
                         "a foreign receipt armed a half here")
        self.assertNotIn(self.tree_of("alpha"), local)
        # POSITIVE CONTROL on the identical fixture: the same two receipts
        # stamped with THIS repo do arm, so the silence above is the repo
        # filter and not a fixture that never greened anything.
        for lane in ("alpha", "beta"):
            self.receipt(head=self.tip(lane), tree=self.tree_of(lane))
        trees, local, err, _warn = _work_gc.green_receipts(self.root)
        self.assertIn(self.tree_of("alpha"), trees)

    def test_an_UNPLACEABLE_receipt_ARMS_but_may_never_DISCHARGE(self):  # noqa: VACUOUS_ASSERTION — every absence here is answered by an unconditional positive on the SAME accessor: the tree IS in `trees`, the seam DOES survive, and the placeable receipt at the end discharges
        """STATE THREE: THE ROOM IS GONE. 2,404 of the ledger's 2,439 rows are
        here, because a reaped worktree takes its path with it — so this state
        is the POPULATION, not a corner.

        The two doors take it opposite ways ON PURPOSE. Arming on it keeps a
        half in the picture, and an exclusion in this rung must be earned by a
        positive signal rather than by a room having been tidied up.
        Discharging on it would END a block on evidence nobody can place,
        which is the laundering this whole shape is about."""
        gone = os.path.join(self.tmp, "reaped-room")
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        for lane in ("alpha", "beta"):
            self.receipt(head=self.tip(lane), tree=self.tree_of(lane),
                         repo_id=gone)
        row = self.receipt(repo_id=gone, append=False)
        self.assertIsNone(_work_gc.receipt_repo_scope(row, self.mine(), {}))
        trees, local, err, _warn = _work_gc.green_receipts(self.root)
        self.assertIsNone(err)
        self.assertIn(self.tree_of("alpha"), trees,
                      "a reaped room's receipt stopped arming its half")
        self.assertNotIn(self.tree_of("alpha"), local)
        self.assertTrue(self.rows(), "precondition: the seam must exist")
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree, repo_id=gone)
        self.assertTrue(self.rows(),
                        "an unplaceable receipt at the composed tree ENDED "
                        "the block")
        # POSITIVE CONTROL: the same tree, a placeable receipt, discharges.
        self.receipt(head="d" * 40, tree=tree)
        self.assertEqual(self.rows(), [])

    def test_a_repository_with_no_resolvable_identity_reads_UNKNOWN(self):
        """The scope question's own could-not-tell. Without an identity for
        THIS repository nothing can be placed, so the answer is UNKNOWN and
        audible rather than a permissive default that admits everything."""
        self.two_green_halves()
        trees, local, err, _warn = _work_gc.green_receipts(None)
        self.assertEqual((trees, local), (frozenset(), frozenset()))
        self.assertIn("UNKNOWN", err or "")


class CouldNotTellTest(SeamBase):
    """SHAPE THREE — an error must not read as an answer.

    The rung already refused to let an UNKNOWN HOLDER buy an exclusion. These
    are the inputs that FEED the holder and the peer list, where the same rule
    was not applied: a registry read that fails, a /proc walk that errors, and a
    roster row that is stale are three flavours of I COULD NOT TELL, and each
    silently deleted peers."""

    def rows(self, lane="alpha", holder="s1"):
        return _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)

    def test_stop_path_bounds_the_historical_import_repository_scan(self):
        self.two_green_halves()
        real = gateimport.binding_candidate_ids
        with mock.patch.object(gateimport, "binding_candidate_ids",
                               wraps=real) as candidates:
            rows, err, _warn = self.rows()
        self.assertIsNone(err)
        self.assertTrue(rows, "the bounded control must still find the real seam")
        candidates.assert_called_once_with(
            self.root, budget_s=gateimport.BINDING_CANDIDATE_BUDGET_S)

    def test_legacy_expiry_preserves_measured_seam_and_surfaces_UNKNOWN(self):
        self.two_green_halves()
        self.assertTrue(self.rows()[0], "control: the real seam is block-worthy")
        reason = ("historical import repository census exceeded 1.000s; "
                  "legacy placement is UNKNOWN")
        with mock.patch.object(gateimport, "binding_candidate_ids",
                               return_value=(frozenset(), None, reason)):
            blocks, _warns = self.guard()
        composition = [b for b in blocks if "COMPOSITION" in b]
        self.assertTrue(composition,
                        "legacy UNKNOWN erased an independently measured seam")
        self.assertIn("canonical bindings were measured", composition[0])
        self.assertIn("legacy import placement is UNKNOWN", composition[0])

    def test_an_unreadable_REGISTRY_is_UNKNOWN_not_an_empty_board(self):
        self.two_green_halves()
        self.assertTrue(self.rows()[0], "precondition: a seam exists")
        with mock.patch.object(_work_gc, "worktrees",
                               side_effect=OSError("registry unreadable")):
            rows, err, _warn = self.rows()
        self.assertEqual(rows, [])
        self.assertIn("could not be read", err or "")

    def test_a_failed_PROC_census_is_UNKNOWN_not_an_empty_room(self):
        """Occupancy is the ONLY liveness producer outside helm's own ledger, so
        in a repo with no leases this failure erases the whole rung while
        looking like a clean board."""
        self.two_green_halves()
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({}, False)):
            rows, err, _warn = self.rows()
        self.assertEqual(rows, [])
        self.assertIn("occupancy census", err or "")
        # and the RAISING flavour takes the same branch
        with mock.patch.object(_work_gc, "_occupants_many",
                               side_effect=OSError("proc walk failed")):
            rows2, err2, _warn2 = self.rows()
        self.assertEqual(rows2, [])
        self.assertIn("occupancy census", err2 or "")

    def test_the_could_not_tell_reaches_the_seat_as_a_WARN(self):
        """An err that never leaves the predicate is the same silence one layer
        up. It must arrive, and it must not block."""
        self.two_green_halves()
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({}, False)):
            blocks, warns = self.guard()
        self.assertEqual([b for b in blocks if "COMPOSITION" in b], [])
        self.assertIn("UNKNOWN, not absent", "\n".join(warns))

    def test_an_unreadable_LEASE_ledger_is_UNKNOWN_not_an_empty_board(self):
        """The one input read OUTSIDE the guarded region. A raise here left
        `seam_rooms` through the stop gate's blanket handler, which renders as
        a clean allow — so the ledger that supplies the delegated-build
        liveness leg could fail and the stop looked examined."""
        self.two_green_halves()
        self.assertTrue(self.rows()[0], "precondition: a seam exists")
        with mock.patch.object(_work_gc, "_live",
                               side_effect=OSError("ledger unreadable")):
            rooms, err, degraded = _work_gc.seam_rooms(self.root)
            self.assertEqual((rooms, degraded), ([], []))
            self.assertIn("lease ledger", err or "")
            blocks, warns = self.guard()
        self.assertEqual([b for b in blocks if "COMPOSITION" in b], [])
        self.assertIn("UNKNOWN, not absent", "\n".join(warns),
                      "an unreadable lease ledger passed as a clean stop")

    def test_a_TOTALLY_blind_proc_pass_is_UNKNOWN_not_an_empty_room(self):
        """`census_complete` answered only "could /proc be LISTED", which is
        the smaller half. Every per-pid cwd read can fail — a hidepid mount, a
        container — and the census then reported NOBODY IS ANYWHERE using the
        same two values a genuinely empty board produces."""
        from helm.work import _lanes
        real = os.path.realpath

        def blind(path, *a, **k):
            p = str(path)
            if p.startswith("/proc/") and p.endswith("/cwd"):
                raise OSError(13, "Permission denied")
            return real(p, *a, **k)
        with mock.patch("helm.work._lanes.os.path.realpath", side_effect=blind):
            occ, complete = _lanes._occupants_many([self.root])
        self.assertFalse(complete,
                         "a /proc pass that read NO process reported complete")
        self.assertEqual(occ, {self.root: []})
        # MUST-HIT: the unmocked pass on this very box reads at least this
        # process's own cwd, so the branch above is reachable only under the
        # failure and the assertion is not measuring a broken instrument.
        _occ2, complete2 = _lanes._occupants_many([self.root])
        self.assertTrue(complete2, "the healthy /proc pass reported blind")

    def test_a_partially_blind_ENVIRON_read_degrades_and_says_so(self):
        """A pid whose environ will not open and a pid that opened and declared
        nothing both contributed nothing, so "I could not read these
        processes" and "these are not seats" were one observable. The
        difference decides whether a one-name room is unshared or merely
        unread — and an unread room reads as a clean bill."""
        self.claim("alpha", "s1")
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({self.path("alpha"): ["4242"]},
                                             True)):
            rooms, err, degraded = _work_gc.seam_rooms(self.root)
        self.assertIsNone(err)
        self.assertTrue(degraded, "an unreadable occupant reported nothing")
        self.assertIn("FLOOR", "\n".join(degraded))
        names, unread = _work_gc._occupant_seats(["4242"])
        self.assertEqual((names, unread), ([], 1),
                         "the unreadable pid was counted as a declaration")

    def test_a_healthy_census_reports_NO_degradation(self):  # noqa: VACUOUS_ASSERTION — the empty `degraded` IS the product law — the third state — and it is asserted beside an unconditional assertTrue(rooms) on the same call
        """THE THIRD STATE, and the arm that stops the two above from passing
        on a census that always complains. Absent, unreadable and genuinely
        clean are three answers; two of them would collapse without this."""
        self.two_green_halves()
        rooms, err, degraded = _work_gc.seam_rooms(self.root)
        self.assertIsNone(err)
        self.assertEqual(degraded, [])
        self.assertTrue(rooms, "the healthy census found no rooms at all")
        self.assertNotIn("PARTIAL room census", "\n".join(self.guard()[1]))

    def test_an_unreadable_ROSTER_degrades_rather_than_narrowing(self):
        """It failed to a bare `{}`, byte-identical to a roster read perfectly
        that named nobody — so an unreadable roster silently NARROWED every
        room's seat set. Narrow is the dangerous direction: the blind-spot
        disclosure fires on a room having MORE than one seat, so the
        instrument's own failure rendered as a cleaner world."""
        room = self.claim("alpha", "s1")
        for name in ("ann", "bob"):
            self.roster(name, room)
        rooms, err, degraded = _work_gc.seam_rooms(self.root)
        self.assertEqual(degraded, [], "precondition: a healthy roster read")
        self.assertEqual({r["path"]: r for r in rooms}[room]["seats"],
                         ["ann", "bob"])
        # THE PRODUCER'S OWN ERR, measured on the producer. `pk.read_json` is
        # ONE module object shared by every roster reader in helm, so a mock on
        # it reaches the stop ladder's identity resolution too — scope it to
        # this call rather than wrapping a whole stop in it.
        real_read, roster = _work_gc.pk.read_json, _work_gc.seats.roster_path()

        def only_roster(path, *a, **k):
            if os.path.abspath(str(path)) == os.path.abspath(roster):
                raise OSError("roster unreadable")
            return real_read(path, *a, **k)
        with mock.patch.object(_work_gc.pk, "read_json",
                               side_effect=only_roster):
            named, roster_err = _work_gc._rostered_seats([room])
            rooms2, err2, degraded2 = _work_gc.seam_rooms(self.root)
        self.assertEqual(named, {}, "an unreadable roster named somebody")
        self.assertIn("UNKNOWN, not absent", roster_err or "")
        self.assertIsNone(err2, "an unreadable roster is not fatal")
        self.assertEqual({r["path"]: r for r in rooms2}[room]["seats"], [])
        self.assertTrue(degraded2, "the narrowing was silent")
        self.assertIn("UNKNOWN, not absent", "\n".join(degraded2))

    def test_a_degraded_census_REACHES_the_seat_and_never_blocks(self):  # noqa: VACUOUS_ASSERTION — the not-a-block assertEqual is a different channel from the two unconditional assertIn lines above it, which are the positive control
        """A degradation that stops inside the predicate is the same silence
        one layer up. It must arrive, and — like every could-not-tell here —
        it may not wall a turn: nothing about an incomplete census is evidence
        that a composition is untested."""
        room = self.claim("alpha", "s1")
        with mock.patch.object(
                _work_gc, "_rostered_seats",
                return_value=({}, "the seat roster could not be read "
                                  "(OSError) — the seats in each room are "
                                  "UNKNOWN, not absent")):
            blocks, warns = self.guard(cwd=room)
        joined = "\n".join(blocks + warns)
        self.assertIn("PARTIAL room census", joined)
        self.assertIn("floor", joined)
        self.assertEqual([b for b in blocks if "PARTIAL room census" in b], [],
                         "a census hole is not evidence of a defect and may "
                         "never wall a turn")

    def test_a_STALE_roster_row_cannot_buy_the_same_seat_exemption(self):
        """Finding 4. A roster row outlives its process, so a stale row
        naming this seat inside a peer's room was enough to drop that peer —
        an exclusion earned by an artifact, not by a positive signal. The roster
        may NAME a holder and may never EXCLUDE one."""
        self.claim("alpha", "s1")
        self.room("beta")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        with mock.patch.object(_work_gc, "_occupants_many",
                               return_value=({self.path("beta"): ["4242"]},
                                             True)):
            self.assertEqual([r["peer_branch"] for r in self.rows()[0]],
                             ["lane/beta"], "precondition: the peer seams")
            self.roster("s1", self.path("beta"))
            rows, err, _warn = self.rows()
        self.assertIsNone(err)
        self.assertEqual([r["peer_branch"] for r in rows], ["lane/beta"],
                         "a stale roster row dropped a live peer")

    def test_the_roster_still_NAMES_the_holder_it_may_not_exclude_on(self):  # noqa: VACUOUS_ASSERTION — the assertion is a named holder and a named leg — a positive; there is no absence here for a control to answer
        """The control that keeps the split honest: attribution accepts all
        three legs, so the arm above is about the EXEMPTION and not about the
        roster leg having been removed."""
        self.room("beta")
        self.roster("bruce", self.path("beta"))
        rooms, err, _degraded = _work_gc.seam_rooms(self.root)
        self.assertIsNone(err)
        row = {r["path"]: r for r in rooms}[self.path("beta")]
        self.assertEqual((row["holder"], row["holder_why"]),
                         ("bruce", "roster"))

    def test_a_LEASE_holder_still_exempts_because_a_lease_is_present_tense(self):  # noqa: VACUOUS_ASSERTION — the control varies the HOLDER argument, so it is a second call by construction — one call cannot be read under two holders
        self.claim("alpha", "s1")
        self.claim("beta", "s1")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        self.assertEqual(self.rows(holder="s1")[0], [])
        self.assertEqual([r["peer_branch"] for r in self.rows(holder="s9")[0]],
                         ["lane/beta"])


class SwallowTest(SeamBase):
    """SHAPE TWO's sibling — finding 5: the disclosure was eaten by the
    measurement's own failure handler, which is the exact defect class this
    module is about, committed inside this module."""

    def test_a_RAISING_predicate_no_longer_eats_the_blind_spot_line(self):
        room = self.claim("alpha", "s1")
        for name in ("ann", "bob"):
            self.roster(name, room)
        with mock.patch.object(_work_gc, "seam_candidates",
                               side_effect=RuntimeError("boom")):
            blocks, warns = self.guard(cwd=room)
        self.assertEqual(blocks, [], "a raising predicate must never block")
        self.assertIn("BLIND SPOT", "\n".join(warns),
                      "the predicate's failure discarded the disclosure — and "
                      "a failed instrument is exactly when a seat needs to be "
                      "told where it cannot look")



class LandedHalfTest(SeamBase):
    """A branch whose work is ALREADY ON TRUNK is not a half.

    MEASURED FALSE POSITIVE, 2026-08-24, and the rung fired it on its own lane:
    `seat/codex-3` carried exactly one commit ahead of trunk, two weeks old, and
    `git cherry origin/main seat/codex-3` printed it with a MINUS — helm's own
    notation for already-on-trunk BY PATCH IDENTITY. Almost nothing lands under
    the sha its author wrote, because the integrator rebases and gates the
    rebased tree, so ancestry alone answers a truthful NO about landed work and
    the branch looks live forever.

    IT SCALES, WHICH IS WHY IT IS NOT ONE BAD ROW. The fleet keeps 74 worktrees
    and most carry branches whose work landed long ago. If a landed half counts
    as a half, this rung's noise grows with the number of unreaped rooms — the
    exact way a correct guard gets muted and then ignored."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def overlapping_pair(self):
        """alpha and beta: two live green halves sharing an authored file."""
        self.claim("alpha", "s1")
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")

    def land_by_patch_identity(self, lane="beta"):
        """Put the lane's CONTENT on trunk under a DIFFERENT sha — the shape our
        protocol actually produces, and the one ancestry cannot see.

        TRUNK MUST MOVE FIRST, and the arm that taught me so was my own
        precondition. Cherry-picking a lane commit onto a trunk still sitting at
        that commit's own PARENT reproduces every input to the sha — same tree,
        same parent, same message, same dates — so git hands back the IDENTICAL
        object and the fixture is testing ANCESTRY while claiming to test patch
        identity. Three arms failed on the precondition rather than passing
        vacuously, which is the whole reason it is there.

        Advancing trunk by one unrelated commit first is also the honest shape:
        work lands rebased onto a trunk that has moved, which is exactly why
        almost nothing arrives under the sha its author wrote."""
        sha = self.tip(lane)
        with open(os.path.join(self.root, "trunk-moved-on.txt"), "w") as f:
            f.write("an unrelated landing, so the rebase target differs\n")
        _sh(self.root, "git", "add", "-A")
        r = _sh(self.root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", "unrelated trunk commit")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _sh(self.root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "cherry-pick", sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        landed = _sh(self.root, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(sha, landed,
                            "precondition: the landed commit must be a "
                            "DIFFERENT object, or this arm is testing ancestry")
        return sha

    def test_a_half_landed_by_PATCH_IDENTITY_is_not_a_half(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL — the landing happens BETWEEN the two calls, which is exactly what the arm is about
        self.overlapping_pair()
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"],
                         "precondition: the pair seams while beta is unlanded")
        self.land_by_patch_identity("beta")
        self.assertEqual(_work_gc._merge_state(self.root, "lane/beta"),
                         vcs.PATCH_EQUIVALENT,
                         "precondition: helm must read this as patch-equivalent")
        self.assertEqual(self.rows(), [],
                         "a landed branch on an unreaped room counted as a half")

    def test_a_half_landed_by_ANCESTRY_is_not_a_half(self):  # noqa: VACUOUS_ASSERTION — the two preconditions are unconditional positives (the merge succeeded, helm reads ANCESTOR); the sibling patch-identity arm carries the seams-while-unlanded control on the identical fixture
        """The easy half of the same question, and the one a naive predicate
        already got right — kept so a cure that only handled patch identity
        cannot quietly drop it."""
        self.overlapping_pair()
        r = _sh(self.root, "git", "-c", "user.name=t", "-c", "user.email=t@t",
                "merge", "--no-ff", "-q", "-m", "land beta", "lane/beta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_work_gc._merge_state(self.root, "lane/beta"),
                         vcs.ANCESTOR, "precondition: reachable by ancestry")
        self.assertEqual(self.rows(), [])

    def test_MY_OWN_landed_room_stops_being_a_half_too(self):  # noqa: VACUOUS_ASSERTION — same fixture as its sibling above, which asserts the pair DOES seam while unlanded; here only which side lands differs
        """Symmetric by construction, and worth pinning because the two sides
        are separate call sites: if MY work has landed there is nothing of mine
        left to compose, whoever the peer is."""
        self.overlapping_pair()
        self.land_by_patch_identity("alpha")
        self.assertEqual(_work_gc._merge_state(self.root, "lane/alpha"),
                         vcs.PATCH_EQUIVALENT)
        self.assertEqual(self.rows(), [])

    def test_a_PARTLY_landed_stack_is_STILL_a_half(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the three above it — its assertion is a named peer, not an absence
        """THE CONTROL THAT KEEPS THE CURE FROM EATING THE RUNG. Landing SOME of
        a stack is exactly the state that must keep its branch, and the
        instrument agrees: one unlanded commit makes `_merge_state` answer
        NOT_ANCESTOR. Without this arm the exclusion could be widened to "any
        landed commit anywhere" and every arm above would stay green."""
        self.overlapping_pair()
        self.land_by_patch_identity("beta")            # commit one lands
        self.edit("beta", "shared.py", at=51)          # commit two does not
        self.green("beta")
        self.assertEqual(_work_gc._merge_state(self.root, "lane/beta"),
                         vcs.NOT_ANCESTOR,
                         "precondition: a mixed stack is not contained")
        self.assertEqual([r["peer_branch"] for r in self.rows()], ["lane/beta"],
                         "a mixed stack lost its half")

    def test_an_UNKNOWN_containment_KEEPS_the_room(self):
        """`_merged`'s own contract: True is PROOF of containment, False covers a
        clean negative AND an unreadable one. So an unreadable branch stays a
        candidate half — exclusion earned by a positive signal, never by
        silence, which is the same rule this rung applies to holders and to
        every input that feeds them."""
        self.overlapping_pair()
        with mock.patch.object(_work_gc, "_merge_state",
                               return_value=vcs.UNKNOWN):
            self.assertEqual([r["peer_branch"] for r in self.rows()],
                             ["lane/beta"], "UNKNOWN was spent as LANDED")



class GateTest(SeamBase):
    """The stop-guard rung itself: what actually reaches the stopping seat."""

    def test_a_real_seam_BLOCKS_with_its_ONE_discharge_quoted(self):
        self.two_green_halves()
        block, _warn = self.text()
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertIn("lane/alpha", block)
        self.assertIn("lane/beta", block)
        self.assertIn("shared.py", block)
        self.assertIn("s2", block)
        self.assertIn("merge CLEANLY", block)
        room = self.path("alpha")
        # the measured discharge, copy-pasteable, aimed at the real room
        self.assertIn("git -C %s merge --no-ff lane/beta && fab gate --repo %s"
                      % (room, room), block)
        # the declared discharge, with the exact trailer token the reader wants
        self.assertIn("HELM_STOP_GUARD_SEAM=0", block)

    def test_a_CONFLICTING_seam_blocks_WITHOUT_offering_a_merge_command(self):
        """A conflicting pair has no composed tree, so the merge+gate command
        would send a seat into a resolution it cannot finish here AND would not
        clear the block when it came back. Name the real next step instead."""
        self.two_green_halves(merges=False)
        block, _warn = self.text()
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertIn("CONFLICT", block)
        self.assertIn("hand-resolved merge", block)
        # THE ARM'S ACTUAL SUBJECT, and I had this BACKWARDS for one run. A
        # blanket replace dropped an assertIn("merge --no-ff") here — into the
        # arm whose whole point is that a conflicting pair is offered NO merge
        # command, because there is no composed tree to gate and the seat
        # cannot finish a resolution from a stop hook. I replaced a line by
        # PATTERN without checking which arm owned it; the clean-merge arm
        # above already pins the command and is where that assertion belongs.
        self.assertNotIn("merge --no-ff", block,
                         "a conflicting seam was offered a merge command it "
                         "cannot complete from here")
        self.assertNotIn("fab gate --repo", block,
                         "offered a gate on a tree that cannot be built")

    def test_the_evidence_word_is_GREEN_and_never_BANKED(self):
        """One evidence word, because there is one tier — and the old branch
        was an INVERSION, not dead prose.

        `proof` chose between GREEN and BANKED by reading `green_by`, a field
        the tier removal deleted. So the ternary rendered BANKED on EVERY seam,
        telling every reader its repo mints no gate receipts at exactly the
        moment an admissible receipt on both half trees became MANDATORY for
        the row to exist. Only a rendered-message arm can catch that: a symbol
        sweep sees the dead field, never a reader still branching on its
        absence.

        REWRITTEN WHOLE rather than patched again. Two earlier partial edits to
        this arm left contradictory remnants — an assertNotIn("halves are
        GREEN") against the new assertIn, and an assertNotIn("fab gate --repo")
        that was right for the no-receipt fixture and false for this one — plus
        a docstring still describing the deleted subject. A third patch would
        have been a fourth contradiction.

        The no-receipt-repo-is-silent property is deliberately NOT here: it
        belongs to test_in_a_repo_with_NO_receipts_there_is_NO_SEAM_and_no_block,
        which carries a one-variable control on the same accessor.
        """
        self.two_green_halves()
        block, _warn = self.text()
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertIn("halves are GREEN", block)
        self.assertIn("admissible gate receipt", block)
        # THE TIER VOCABULARY MUST BE GONE FROM THE RENDER, not merely from the
        # predicate — that gap is what shipped the inversion.
        self.assertNotIn("BANKED", block)
        self.assertNotIn("mints no gate receipts", block)
        self.assertNotIn("strongest evidence available", block)

    def test_a_LONE_room_with_no_peer_stops_freely(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here; reachability is proven by the named sibling arm rather than inside this one (test_a_real_seam_BLOCKS_with_both_discharges_quoted)
        """THE MUST-MISS. One seat, one room, nobody else live. Measured on the
        real board 2026-08-23: 7 live rooms in helm and 2 in the sibling project, and the
        rung fired on ZERO of them."""
        self.claim("alpha", "s1")
        self.edit("alpha")
        self.green("alpha")
        block, warn = self.text()
        self.assertNotIn("UNTESTED COMPOSITION", block)
        self.assertNotIn("UNTESTED COMPOSITION", warn)

    def test_a_seat_standing_OUTSIDE_every_room_is_not_party_to_a_seam(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here; reachability is proven by the named sibling arm rather than inside this one (test_the_cwd_alone_makes_a_seat_party_to_a_seam)
        """`mine` comes from the seat's cwd (plus its leases). A process
        stopping outside the repo's worktrees holds no half."""
        self.two_green_halves()
        block, _warn = self.text(seat="s9", session="s-out", cwd=self.tmp)
        self.assertNotIn("UNTESTED COMPOSITION", block)

    def test_the_cwd_alone_makes_a_seat_party_to_a_seam(self):
        """THE LEG THAT MAKES THIS WORK OUTSIDE HELM: no lease of my own, no
        roster row, just a process standing in a room. that project's seats look
        exactly like this."""
        self.claim("alpha", "s1")           # alpha leased by somebody else
        self.claim("beta", "s2")
        self.edit("alpha", at=1)
        self.edit("beta", at=50)
        self.green("alpha")
        self.green("beta")
        block, _warn = self.text(seat="s9", session="s-cwd",
                                 cwd=self.path("alpha"))
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertIn("lane/beta", block)

    def test_the_block_rides_the_guards_exit_2(self):
        """Arbiter shape: a block is a GATE, and it reaches the seat as rc 2 on
        the real CLI leg, not merely as a returned list."""
        self.two_green_halves()
        rc, err = _cli(self.path("alpha"), seat="s1", session="sess-cli")
        self.assertEqual(rc, 2)
        self.assertIn("UNTESTED COMPOSITION", err)

    def test_the_composed_tree_receipt_ends_the_block(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.two_green_halves()
        self.assertIn("UNTESTED COMPOSITION", self.text(session="s-a")[0])
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree)
        block, warn = self.text(session="s-b")
        self.assertNotIn("UNTESTED COMPOSITION", block)
        self.assertNotIn("UNTESTED COMPOSITION", warn)

    # ── non-wedging ───────────────────────────────────────────────────────
    def test_it_blocks_ONCE_and_a_re_stop_on_the_same_seam_passes(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.two_green_halves()
        self.assertIn("UNTESTED COMPOSITION", self.text()[0])
        for _ in range(5):
            self.assertNotIn("UNTESTED COMPOSITION", self.text()[0],
                             "the same seam blocked twice")

    def test_a_NEW_peer_re_arms_the_block_exactly_once(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        """The latch keys on the seam SET, so a third room arriving on the same
        file is a new fact and deserves another block — while the peer's next
        commit, which moves the tips and not the seams, does not."""
        self.two_green_halves()
        self.assertIn("UNTESTED COMPOSITION", self.text()[0])
        self.assertNotIn("UNTESTED COMPOSITION", self.text()[0])
        self.claim("gamma", "s3")
        self.edit("gamma", "shared.py", at=30)
        self.green("gamma")
        block = self.text()[0]
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertIn("further seam", block)
        self.assertNotIn("UNTESTED COMPOSITION", self.text()[0])

    def test_a_MOVED_half_re_arms_the_block_because_it_is_a_NEW_composition(self):  # noqa: VACUOUS_ASSERTION — every assertion here is temporal by design: the arm is ABOUT the sequence latch, move, re-arm, compress, which no single call can express
        """A DEAD PREMISE, INVERTED RATHER THAN DELETED. This arm used to assert
        the OPPOSITE — that a peer's new commit must not re-arm — and defended
        it as sparing the seat "a nag about somebody else's keyboard". A probe
        measured what that actually bought: when the peer's half moves, the
        composition is a DIFFERENT composition, nothing has ever run it, and the
        pair-keyed latch suppressed exactly the re-arm the latch exists to
        permit. The old assertion was the defect, so the arm now pins the cure.

        The latch still compresses a genuine re-stop — the arm below this one —
        so this is not a return to nagging: it is the difference between the
        same seam and a new one."""
        self.two_green_halves()
        self.assertIn("UNTESTED COMPOSITION", self.text()[0])
        self.assertNotIn("UNTESTED COMPOSITION", self.text()[0],
                         "precondition: the SAME composition must compress")
        self.edit("beta", "shared.py", at=51)
        self.green("beta")
        self.assertIn("UNTESTED COMPOSITION", self.text()[0],
                      "a MOVED half left the block latched shut")
        self.assertNotIn("UNTESTED COMPOSITION", self.text()[0],
                         "and the new composition compresses in its turn")

    def test_an_unwritable_latch_degrades_to_a_warn_never_a_wall(self):
        """A gate that cannot remember is a gate that blocks every stop
        forever. It gives up the block and keeps the message."""
        self.two_green_halves()
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only chat dir")):
            blocks, warns = self.guard()
        self.assertEqual([b for b in blocks if "UNTESTED COMPOSITION" in b], [])
        self.assertIn("UNTESTED COMPOSITION", "\n".join(warns))

    def test_stop_hook_active_short_circuits_the_rung(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here; reachability is proven by the named sibling arm rather than inside this one (test_a_real_seam_BLOCKS_with_both_discharges_quoted)
        self.two_green_halves()
        blocks, _warns = seats.stop_guard(session="s-x", room="main",
                                          seat="s1", stop_active=True,
                                          cwd=self.path("alpha"))
        self.assertEqual(blocks, [])

    # ── fail-open ─────────────────────────────────────────────────────────
    def test_a_raising_predicate_never_wedges_the_stop(self):  # noqa: VACUOUS_ASSERTION — absence IS the product law here; reachability is proven by the named sibling arm rather than inside this one (test_the_block_rides_the_guards_exit_2 proves rc 2 is reachable)
        self.two_green_halves()
        with mock.patch.object(_work_gc, "seam_candidates",
                               side_effect=RuntimeError("boom")):
            rc, err = _cli(self.path("alpha"), seat="s1", session="s-boom")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("UNTESTED COMPOSITION", err)

    def test_an_unreadable_receipt_ledger_WARNS_and_never_blocks(self):
        """UNKNOWN seams are not zero seams — the spiral rung's own correction,
        applied at birth here rather than after a seat runs ten rounds inside
        the silence."""
        self.two_green_halves()
        with mock.patch.object(gate, "receipts",
                               return_value=([], "checksum mismatch", 0)):
            blocks, warns = self.guard()
        self.assertEqual([b for b in blocks if "COMPOSITION" in b], [])
        self.assertIn("UNKNOWN, not absent", "\n".join(warns))

    def test_a_branch_name_that_cannot_be_quoted_inertly_is_not_quoted(self):  # noqa: VACUOUS_ASSERTION — the control is a SECOND mock return value, so it cannot share a call with the hostile one; it is unconditional and immediately below
        """The message's value is a PASTEABLE command, so laundering a branch at
        the sink would produce a silently WRONG command. Validate at the seam
        (the beacon block's pattern) and stay silent on anything that does not
        clear it.

        THE CLAIM IS LOAD-BEARING, not scenery: without a room of my own the
        gate returns before it ever calls the predicate, and BOTH halves of
        this arm would pass while measuring an early exit."""
        self.claim("alpha", "s1")
        hostile = {"path": self.path("alpha"), "branch": "lane/alpha",
                   "peer_path": self.path("beta"),
                   "peer_branch": 'lane/beta" ; rm -rf /', "peer_holder": "s2",
                   "peer_why": "lease",
                   "files": ["shared.py"], "tree": "e" * 40, "merges": True}
        with mock.patch.object(_work_gc, "seam_candidates",
                               return_value=([hostile], None, None)):
            blocks, warns = self.guard()
        self.assertNotIn("rm -rf", "\n".join(blocks + warns))
        self.assertNotIn("UNTESTED COMPOSITION", "\n".join(blocks + warns))
        # THE CONTROL: the same path with a legitimate name DOES speak, so the
        # silence above is the validator and not a dead code path.
        with mock.patch.object(_work_gc, "seam_candidates",
                               return_value=([dict(hostile, peer_branch="lane/beta")],
                                             None, None)):
            self.assertIn("UNTESTED COMPOSITION", self.text()[0])

    # ── kill switches ─────────────────────────────────────────────────────
    def test_the_named_kill_switch_disables_only_this_rung(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL, and the rung is right that it is not the same observable: the two calls straddle the state change, which is exactly why the pair proves anything
        self.two_green_halves()
        os.environ["HELM_STOP_GUARD_SEAM"] = "0"
        try:
            self.assertEqual(self.guard()[0], [])
        finally:
            os.environ.pop("HELM_STOP_GUARD_SEAM")
        self.assertIn("UNTESTED COMPOSITION", self.text()[0])   # …and back on

    def test_the_global_kill_switch_covers_it_too(self):  # noqa: VACUOUS_ASSERTION — the unconditional control runs FIRST and the kill-switch read is a second call by construction
        self.two_green_halves()
        # UNCONDITIONAL CONTROL FIRST: without it, `guard() == ([], [])`
        # under the kill switch is equally satisfied by a fixture that
        # never produced a seam, and the arm would pass measuring
        # nothing at all.
        self.assertIn("UNTESTED COMPOSITION", self.text()[0])
        os.environ["HELM_STOP_GUARD"] = "0"
        try:
            self.assertEqual(self.guard(), ([], []))
        finally:
            os.environ.pop("HELM_STOP_GUARD")


class ExitPathTest(SeamBase):
    """SHAPE FOUR — a finding must survive the exit the stop actually takes.

    `seats_cli` has three exits and this rung has business on all of them:
    rc 2 prints blocks and DISCARDS every advisory, rc 0 prints the advisories,
    and the guard-could-not-run branch prints neither. A once-per-arrangement
    latch spent on an exit that showed nothing is silent forever after, and a
    finding routed onto a channel an exit drops never existed for the reader.

    ONE ARM PER EXIT CODE, THROUGH THE REAL CLI where the exit code is the
    observable. A returned list cannot tell these apart — it is the same list
    on every branch — which is exactly how the first cure of this defect
    passed review while the warning was still being lost."""

    def shared_seam(self):
        """A stop that has BOTH findings at once: a real cross-room seam and a
        co-tenanted room. The refusal exit is only reachable with a block, so
        the disclosure can only be observed there beside one."""
        self.two_green_halves()
        for name in ("ann", "bob"):
            self.roster(name, self.path("alpha"))
        return self.path("alpha")

    def shared_no_seam(self):
        """A co-tenanted room with NO cross-room seam — this rung BLIND and
        silent. One lane, so there is no peer room to compare against and the
        rung has no block of its own; the disclosure is everything it says."""
        room = self.claim("alpha", "s1")
        self.edit("alpha", at=1)
        self.green("alpha")
        for name in ("ann", "bob"):
            self.roster(name, room)
        return room

    def test_the_blind_spot_survives_a_DIFFERENT_rungs_refusal(self):
        """THE SIBLING EXIT, and the one the first cure did not reach. Putting
        the disclosure IN this rung's block covers the stop where THIS rung
        refuses. It does nothing for the stop where this rung is blind, has no
        block to ride, and some OTHER rung refuses — the warn channel is
        dropped by the refusal exit no matter whose refusal it is, so the seat
        was told nothing about the one place the instrument cannot look.

        A disclosure that is TRUE must reach the seat regardless of which rung,
        if any, blocks. Which rung refuses is not a property of the finding.

        THE OTHER RUNG IS A DOUBLE AT ITS REAL CALL SITE. The ladder's own
        `_ndp_gate` is made to refuse, one rung BELOW this one, so the block
        travels the production path from `stop_guard` through the CLI's
        refusal exit; only the verdict is supplied, and the verdict is the
        variable this arm varies. Standing up a second subsystem's real
        preconditions would test that subsystem, and the first attempt did
        exactly that: it enabled the claims rung, got a clean allow, and was
        red on its own precondition rather than on the finding."""
        room = self.shared_no_seam()
        elsewhere = "[helm stop-guard] A DIFFERENT RUNG REFUSES THIS STOP."
        with mock.patch.object(seats_stop_guard, "_ndp_gate",
                               return_value=(elsewhere, None)):
            rc, err = _cli(room, seat="s1", session="sess-other-rung")
            self.assertEqual(rc, 2, "precondition: the stop must REFUSE")
            self.assertIn(elsewhere, err,
                          "the double never took — no other rung refused, so "
                          "the exit under test was not taken")
            self.assertNotIn("UNTESTED COMPOSITION", err,
                             "this rung refused — the sibling case is not "
                             "being measured")
            self.assertIn("SEAM RUNG BLIND SPOT", err,
                          "the disclosure died with the warn channel because "
                          "somebody else blocked")
            # AND IT IS SPENT BY BEING SEEN, not by being built: the same
            # arrangement on the next stop is silent, so surviving this exit
            # was not bought with per-stop wallpaper.
            rc2, err2 = _cli(room, seat="s1", session="sess-other-rung")
            self.assertIn(elsewhere, err2,
                          "the re-stop took a DIFFERENT exit — the silence "
                          "below would be that, not the latch")
            self.assertEqual(rc2, 2)
            self.assertNotIn("SEAM RUNG BLIND SPOT", err2,
                             "the arrangement spoke twice after being emitted")

    def test_a_disclosure_a_BLOCK_ALREADY_CARRIES_is_not_said_twice(self):
        """The cost side of the door, on the stop where BOTH cures apply. When
        this rung refuses, the disclosure is already folded into its block; a
        delivery rule that also printed every armed disclosure beside the
        blockers would say it twice in one refusal — and doubling a line is
        how a true disclosure starts reading as noise."""
        room = self.shared_seam()
        rc, err = _cli(room, seat="s1", session="sess-dup")
        self.assertEqual(rc, 2, "precondition: this stop must refuse")
        self.assertEqual(err.count("SEAM RUNG BLIND SPOT"), 1,
                         "the disclosure was printed beside a block that "
                         "already carried it")

    def test_the_blind_spot_reaches_a_stop_that_NOTHING_blocks(self):
        """THE OTHER POLE, on the same fixture with one variable changed: no
        rung refuses at all. The disclosure has to arrive on the advisory
        channel here, and this arm is what keeps its sibling from passing on a
        cure that simply prints the line into every refusal it can find."""
        room = self.shared_no_seam()
        rc, err = _cli(room, seat="s1", session="sess-nothing-blocks")
        self.assertEqual(rc, 0, "precondition: nothing may refuse this stop")
        self.assertIn("SEAM RUNG BLIND SPOT", err,
                      "the disclosure was lost on the allow exit")

    def test_the_blind_spot_line_survives_the_REFUSAL_exit(self):
        """rc 2. The disclosure used to ride the WARN channel, which this exit
        drops by design — the owner watched an evening of red "Stop hook
        error:" lines in 2026-07-31 and the suppression is right. The channel
        was wrong: a rung whose finding must survive this exit puts it IN the
        block rather than beside one."""
        room = self.shared_seam()
        rc, err = _cli(room, seat="s1", session="sess-rc2")
        self.assertEqual(rc, 2, "precondition: this stop must refuse")
        self.assertIn("UNTESTED COMPOSITION", err)
        self.assertIn("SEAM RUNG BLIND SPOT", err,
                      "the blind-spot disclosure was lost on exit 2")

    def test_the_blind_spot_line_survives_the_ALLOW_exit(self):
        """rc 0, and the arm that keeps its sibling from passing on a cure that
        merely prints the line everywhere. Same fixture, one variable changed:
        the seam is discharged, so there is no block and the disclosure has to
        arrive on the advisory channel instead."""
        room = self.shared_seam()
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree)
        rc, err = _cli(room, seat="s1", session="sess-rc0")
        self.assertEqual(rc, 0, "precondition: this stop must allow")
        self.assertNotIn("UNTESTED COMPOSITION", err)
        self.assertIn("SEAM RUNG BLIND SPOT", err,
                      "the blind-spot disclosure was lost on exit 0")

    def test_a_REFUSED_stop_spends_the_arrangement_it_actually_showed(self):  # noqa: VACUOUS_ASSERTION — the control is TEMPORAL by construction — the first _cli call asserts the line IS emitted, and one call cannot straddle spent and unspent
        """The latch follows the emission, both ways. The line went out on
        exit 2, so it is spent there and the next stop on the same arrangement
        does NOT repeat it — otherwise "survives exit 2" would have been bought
        with per-stop wallpaper."""
        room = self.shared_seam()
        rc, err = _cli(room, seat="s1", session="sess-spend")
        self.assertEqual(rc, 2)
        self.assertIn("SEAM RUNG BLIND SPOT", err)
        _rc2, err2 = _cli(room, seat="s1", session="sess-spend")
        self.assertNotIn("SEAM RUNG BLIND SPOT", err2,
                         "the arrangement spoke twice after being emitted")

    def test_a_stop_that_EMITTED_NOTHING_spends_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive is the final assertIn — the arrangement still speaks — and the absent latch files are the product law this arm is about
        """THE THIRD EXIT. `seats_cli` catches a raising guard, prints THE
        GUARD COULD NOT RUN and returns 0 — nothing this rung built reached
        anybody. Process death used to do this cleanup, which is not a
        mechanism: the queue outlives one stop inside a long-running process,
        and a later unrelated emission banked a fingerprint for a line nobody
        had printed. That is the original defect through a second door."""
        room = self.shared_seam()
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        blocks, warns = seats.stop_guard(session="sess-quiet", room="main",
                                         seat="s1", cwd=room)
        self.assertTrue(seats_stop_seam._PENDING_DISCLOSURES,
                        "precondition: the stop armed something")
        paths = [row[0] for row in seats_stop_seam._PENDING_DISCLOSURES]
        # A LATER, UNRELATED emission in the SAME process must not bank them.
        seats_stop_seam.emit_warns(["[helm stop-guard] an unrelated advisory"],
                                   stream=io.StringIO())
        for path in paths:
            self.assertFalse(os.path.exists(path),
                             "an unshown line's latch was banked by a "
                             "different emission")
        # AND THE ARRANGEMENT IS STILL OWED: the next stop that emits speaks.
        rc, err = _cli(room, seat="s1", session="sess-quiet")
        self.assertIn("SEAM RUNG BLIND SPOT", err,
                      "the unshown disclosure was lost rather than re-armed")

    def test_a_BLOCK_nobody_emitted_leaves_the_composition_ARMED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positives are the two assertIn lines on the built blocks; the absent latch and the compressed re-stop are the product law
        """ITEM FOUR AT THE MECHANISM. The seam latch was written the moment
        the sentence was BUILT, so a stop whose block never reached a reader
        still spent the fingerprint — and the re-stop matched it and said
        nothing. A latch that fires before its line is read is a guard that
        fires once and then blesses everything after it."""
        self.two_green_halves()
        seats_stop_seam._PENDING_DISCLOSURES.clear()
        block, _warn = seats_stop_seam._seam_gate("sess-armed", "main", "s1",
                                                  cwd=self.path("alpha"))
        self.assertIn("UNTESTED COMPOSITION", block or "")
        latch = seats_stop_seam._stop_fp_path(
            "main", "s1", "sess-armed", kind=seats_stop_seam.SEAM_LATCH)
        self.assertFalse(os.path.exists(latch),
                         "the block latched while being BUILT")
        seats_stop_seam._PENDING_DISCLOSURES.clear()   # nothing was emitted
        block2, _w2 = seats_stop_seam._seam_gate("sess-armed", "main", "s1",
                                                 cwd=self.path("alpha"))
        self.assertIn("UNTESTED COMPOSITION", block2 or "",
                      "an unemitted block blessed the composition")
        # POSITIVE CONTROL: emitted, the same composition compresses.
        rc, err = _cli(self.path("alpha"), seat="s1", session="sess-armed2")
        self.assertEqual(rc, 2)
        _rc2, err2 = _cli(self.path("alpha"), seat="s1", session="sess-armed2")
        self.assertNotIn("UNTESTED COMPOSITION", err2,
                         "an EMITTED block failed to compress the re-stop")


class TrailerVacuityTest(SeamBase):
    """SHAPE FIVE — a form standing in for a discharged obligation.

    A `Seam: <branch>` commit trailer used to clear this block. First a
    BARE trailer cleared it; the cure added a word-count floor and then a
    trailer padded with three filler words cleared it; the next step would have
    been whitespace or a near-miss token, because every cure was SHAPE-based
    where the property is CONTENT-based. The discharge is gone entirely, and
    this class is what the deletion owes: the strongest form each iteration
    accepted, proven to clear nothing, plus the padded-filler form that
    defeated the second cure specifically.

    TESTING ONLY AN EMPTY TRAILER WOULD RE-CREATE THE EXACT HOLE — an empty
    string fails every shape check ever written here, so an arm built on one
    passes under a rung that still discharges on any well-formed trailer."""

    def rows(self, lane="alpha", holder="s1"):
        rows, err, _warn = _work_gc.seam_candidates(
            self.root, {self.path(lane)}, holder=holder)
        self.assertIsNone(err)
        return rows

    def test_no_Seam_trailer_of_ANY_shape_discharges_the_seam(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts a NAMED peer (a positive), and the unconditional control after the loop is the composed-tree receipt clearing the same fixture
        """RIGHT SHAPE, WRONG SUBSTANCE, one form per round of the old cure.
        Each is committed onto the lane and the tree is REGATED afterwards, so
        the half stays green and the only thing under test is the trailer —
        without that the seam would vanish for the unrelated reason that a new
        commit moved the tree off its receipt.

        EVERY ROW IS JUDGED ALONE, and this matrix did not start that way. All
        seven bodies were appended to ONE branch while the reader they are
        aimed at — any discharge that judges a commit MESSAGE — reads
        `trunk..branch`. So from row three onward each verdict was earned by
        row two's commit still sitting in the history behind it, and a matrix
        that reported seven refusals was measuring one. The rewind is the
        FIRST statement of each row rather than the last, because a row that
        fails takes the rest of its `subTest` body with it: cleaning up
        afterwards is exactly the placement that stops running the moment the
        matrix starts finding things.

        The one-trailer count inside the loop is that isolation stated as an
        assertion, so this arm reports a contaminated history as a failure of
        its own rather than as a confident row about a body it never tested.

        WHAT EACH ROW IS SENSITIVE TO, measured by restoring the deleted
        discharge over this fixture: THREE rows move — the three whose bodies
        clear a three-word floor after the peer name — and the other four do
        not, because that floor refused those bodies too. Sharing a branch had
        made SIX move and read as seven independent refusals. The four are
        aimed at the looser discharge a next round would have written: a bare
        token, punctuation, an oid, a second trailer line. A matrix carrying
        only the three would go quiet the moment someone rewired one of those,
        which is the hole this class exists to hold open."""
        self.two_green_halves()
        self.assertTrue(self.rows(), "precondition: the seam must exist first")
        base = self.tip("alpha")
        # A REAL OID, AND THE COMPOSED ONE. The oid row used to carry forty
        # zeros, which resolves to nothing — so it proved only that a trailer
        # naming a NON-EXISTENT object clears nothing, and any future cure that
        # accepted a trailer naming a real object would have passed it while
        # this row applauded. The strongest available token is the composed
        # tree itself: even a seat that names, correctly and provably, the
        # exact tree this seam composes to has not RUN anything against it.
        _rc, composed = self.merged_tree("alpha", "beta")
        self.assertEqual(_sh(self.root, "git", "cat-file", "-t",
                             composed).stdout.strip(), "tree",
                         "the oid row's token must RESOLVE, or the row is "
                         "back to testing a non-existent object")
        forms = (
            ("bare", "Seam: lane/beta"),
            ("three filler words", "Seam: lane/beta aaa bbb ccc"),
            ("padded to length", "Seam: lane/beta " + ("filler " * 40)),
            ("named owner and a when",
             "Seam: lane/beta — s1 composes and gates before land"),
            ("punctuation padding", "Seam: lane/beta - - — — ..."),
            ("the RESOLVING oid of the composed tree",
             "Seam: lane/beta " + composed),
            # NAMED FOR ITS BODY. This row was labelled "the peer named twice"
            # while `lane/beta` appears in it exactly ONCE — the label named a
            # form the body never carried, so the row could not have been
            # measuring it. What it does carry is a multi-line trailer BLOCK,
            # which is the form a cure that counted trailer keys would accept.
            ("extra Seam-* keys beside it",
             "Seam: lane/beta\nSeam-Owner: s1\nSeam-Tested: yes"),
        )
        for tag, body in forms:
            with self.subTest(tag):
                self.reset("alpha", base)      # this row's ONLY commit follows
                self.trailer("alpha", body)
                self.green("alpha")
                self.assertEqual(self.own_log("alpha").count("Seam:"), 1,
                                 "ISOLATION: %r is being judged with another "
                                 "form's commit behind it" % tag)
                self.assertEqual([r["peer_branch"] for r in self.rows()],
                                 ["lane/beta"],
                                 "a %s trailer discharged the seam" % tag)
        # POSITIVE CONTROL on the same fixture: the ONE real discharge still
        # works, so the seven silences above are the trailer and not a lane
        # that had stopped seaming. From the BASE, so the control is not read
        # off whatever the last row happened to leave behind.
        self.reset("alpha", base)
        _rc, tree = self.merged_tree("alpha", "beta")
        self.receipt(head="c" * 40, tree=tree)
        self.assertEqual(self.rows(), [],
                         "the composed-tree receipt stopped discharging")

    def test_the_trailer_SHAPE_VOCABULARY_is_gone_from_the_module(self):
        """The residue is the re-wiring bait. A word-shape regex and a
        word-count floor sat unreferenced beside the deleted discharge — a
        ready-made vocabulary for "a well-formed trailer" that the next reader
        finds and rewires. There is no shape a seat can type."""
        for name in ("_SEAM_WORD", "SEAM_OWNER_WORDS"):
            self.assertFalse(hasattr(_work_gc, name),
                             "%s survived the discharge it belonged to" % name)
        # MUST-HIT: a constant that IS still live, so the sweep above is not
        # measuring a module it failed to import.
        self.assertTrue(hasattr(_work_gc, "_SEAM_CODE"))

    def test_the_block_no_longer_OFFERS_a_trailer_as_a_cure(self):
        """A cure a guard prints is a promise. Leaving the trailer in the
        remediation would have been worse than the original bug: helm would
        instruct an operator to write a line that clears nothing, and they
        would write it, re-stop, and find the block still there."""
        self.two_green_halves()
        block = "\n".join(self.guard()[0])
        self.assertIn("UNTESTED COMPOSITION", block)
        self.assertNotIn("Seam:", block)
        self.assertIn("fab gate", block)


def _cli(cwd, seat, session, room="main"):
    """The real verb leg, FD-free: (rc, stderr)."""
    import types
    payload = json.dumps({"session_id": session, "cwd": cwd}).encode()
    err = io.StringIO()
    fake = types.SimpleNamespace(buffer=io.BytesIO(payload))
    with mock.patch.object(sys, "stdin", fake), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(err):
        rc = seats.cmd("stop-guard", ["--hook-json", "--seat", seat], room)
    return rc, err.getvalue()


if __name__ == "__main__":
    unittest.main()
