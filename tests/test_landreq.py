#!/usr/bin/env python3
"""helm lr — the LAND REQUEST lifecycle VIEW over the dispatch ledger + a git
trunk observation. Hermetic: HELM_HOME/HELM_CHAT_DIR are tmp dirs and every
git repo is minted in setUp; the real ledger and repos are never touched. No
second ledger is created — these tests assert the LR is a pure projection of
dispatch rows refined by an observation of where the reviewed tip actually is.
"""
import contextlib
import inspect
import io
import json
import itertools
import os
import re
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from helm import chat, dispatches, eventledger, gate, landreq, pk, \
    projscope, verdicts
from tests._gate_receipt import serial_process

# CLASSES THIS MODULE HANDED AWAY, read by `helm/retired_name_rung.py`.
#
# That rung judges a diff PER FILE: a `-` line removing a top-level class with
# no `+` line adding it back IN THE SAME FILE reads as a retirement, and a move
# between two files in one commit is exactly the shape a size ceiling forces.
# This table is the cure it offers, and it is not clearance by itself -- the
# rung also requires the named satellite to define the name at column zero, so
# it cannot vouch for a class nobody wrote.
#
# NO SETATTR REPUBLICATION HERE, AND THAT IS DELIBERATE. When a production
# module in `helm/` sheds a name its consumers still spell `owner.NAME`, so the
# owner must bind it back or every one of them dangles. A moved TEST CLASS has
# no such consumer -- measured, zero spellings of any name below anywhere
# outside its satellite -- and binding it back would be a live defect rather
# than a courtesy: `unittest` collects by walking module attributes, so each
# class would be found twice, once under each module, and every one of its arms
# would run twice under two names.
_OWNER_NAMES = (
    ("test_lr_compose", ("ComposeTest",)),
    ("test_lr_contrary", ("ContraryDischargeTest",
                          "ContraryDischargeArmsTest",
                          "ContraryProvenanceNamesItsEvidenceTest",
                          "ContraryProvenanceReachesTheOperatorTest",
                          "FixContraryCarrierDoorTest",
                          "ContraryHonoredOnEverySurfaceTest",
                          "ConfirmationRowIsNeverContraryTest")),
    ("test_lr_landing_store", ("LandingStoreBackfillTest",
                               "ProofAvailabilityIsItsOwnAnswerTest",
                               "LandingStoreServesTheRealProofTest")),
    ("test_lr_outsider_approval", ("OutsiderApprovalIsWhatMakesALaneREADYTest",)),
)

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

# A complete signed-send receipt, exactly the shape chat._sign_send returns.
SENT = {"sent": True, "turn_hash": "a" * 64, "receipt_hash": "b" * 64,
        "chain_index": 7}


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = landreq.cmd_lr(args)
    return rc, out.getvalue(), err.getvalue()


# THIS MODULE DOES NOT READ HOST LIVENESS, AND DECLARES IT HERE.
#
# Every dispatch write in this module reaches `dispatches.add` ->
# `_validate_recipient_usable` -> `seat_usability.seat_verdict`, which walks
# the WHOLE process table twice. Measured on the build node mid-run: 54,114
# pids, 0.677s a walk, and this module makes 608 tests pay it. That is 396s of
# the suite's wall clock spent consulting a host none of these tests assert on.
#
# THE COST IS THE SYMPTOM; THE DEFECT IS THAT WHAT THESE TESTS OBSERVED
# DEPENDED ON WHAT ELSE WAS RUNNING BESIDE THEM — including the other gate
# shards, which is why it grew with the fleet rather than with the code.
#
# MODULE SCOPE, NOT A BASE CLASS, AND THAT IS MEASURED. `LandReqBase` covers
# 40 classes; an AST census of this file finds 95, of which 32 inherit
# `unittest.TestCase` DIRECTLY. A setUp on the base would have left a third of
# the module reading the real host while looking fully wired — the same
# partway-wiring this fleet keeps getting bitten by. setUpModule covers every
# class regardless of base.
#
# PATCHING THE MODULE ATTRIBUTE, not passing `join(live_seats=...)`. That
# parameter is threaded into `_read_panes` only, while `_read_health` reaches
# the census independently through `proxywatch.health()` — so the kwarg
# removes about HALF the walk and reports success (measured: 0.89s -> 0.375s
# via the kwarg, 0.8188s -> 0.0001s patching the attribute). Filed separately.
#
# A test here that DOES need real liveness must stop the patch explicitly and
# say why; it must not quietly assert against this stand-in.
_LIVE_SEATS_PATCH = None

# CAPTURED AT IMPORT, NOT IN setUpModule, AND THE DIFFERENCE IS A REAL BUG I
# WROTE FIRST. The control below contrasts the bound attribute against the
# genuine function — necessary because on the build node the REAL census also
# returns an empty fleet (no claude runs there), so `names == set()` cannot
# tell the stand-in from the host and would pass with no patch at all.
#
# Capturing in setUpModule made that contrast unreliable across MODULES: this
# file is imported by test_lr_close (for LandReqBase), several modules install
# the same stand-in, and a setUpModule running while another module's patch was
# live would have captured the PATCHED function as "real" — after which the
# identity assertion compares a patch to itself and says nothing. Imports all
# happen during collection, before any setUpModule, so import time is the one
# moment nothing is patched.
from helm import proxywatch as _proxywatch_for_capture     # noqa: E402
_REAL_LIVE_SEATS = _proxywatch_for_capture._live_seats


_VIEW_FLAGS = ("--no-replace-objects",)


def git_verb(args):
    """The git VERB from a call's argv, past the view flags and any -c pair.

    A double that reads ``args[0]`` as the verb stops recognising every call
    the moment its caller asks for the object view, because the view flag now
    sits in front. It then answers "" to everything, which is indistinguishable
    from a repository that failed — a double that quietly stopped looking,
    wearing the costume of a broken repo. Reading the verb through this helper
    keeps a double honest across a view change instead of silently blind.
    """
    rest = [a for a in args]
    while rest and rest[0] in _VIEW_FLAGS:
        rest.pop(0)
    if len(rest) >= 2 and rest[0] == "-c":
        rest = rest[2:]
    return rest[0] if rest else ""


def setUpModule():
    global _LIVE_SEATS_PATCH
    from helm import proxywatch
    _LIVE_SEATS_PATCH = mock.patch.object(
        proxywatch, "_live_seats",
        # (names, blind) — a MEASURED empty fleet, never a blind one. `blind`
        # is the reason string from `cannot_look`; None means the census was
        # taken and saw no claude, which is a different claim from "could not
        # look" and consumers read them differently (`turn_state` reads absent
        # as `off`, which this fleet's vocabulary defines as deliberate).
        lambda: (set(), None, {}))
    _LIVE_SEATS_PATCH.start()


def tearDownModule():
    global _LIVE_SEATS_PATCH
    if _LIVE_SEATS_PATCH is not None:
        _LIVE_SEATS_PATCH.stop()
    # THE GLOBAL GOES BACK TO WHAT IMPORT LEFT: other modules import from this
    # one, so a stopped patcher left here is data they can reach (task/3039).
    _LIVE_SEATS_PATCH = None


class TheLivenessStandInIsInEffectTest(unittest.TestCase):
    """The control for the module hook above — it asserts the DOUBLE, not the
    speed.

    A fixture that silently stops taking is the failure this fleet repeats: the
    arms stay green, the module quietly reads the real host again, and the only
    symptom is that the suite gets slow again months later. This class inherits
    `unittest.TestCase` DIRECTLY, so it is one of the 32 a base-class fixture
    would have missed — if setUpModule ever narrows to a base, this goes red.
    """

    def test_the_census_is_the_STAND_IN_and_not_the_host(self):  # noqa: VACUOUS_ASSERTION — MUTATION-TESTED rather than argued: with the fixture these 2 arms run OK in 0.042s; with `_LIVE_SEATS_PATCH.start()` disabled they run in 0.815s and FAIL. The identity assertion IS the unconditional positive control — the value cannot be one, because the build node's REAL census also returns an empty fleet.
        from helm import proxywatch
        # POSITIVE CONTROL, UNCONDITIONAL, ON THE SAME OBSERVABLE: the bound
        # attribute must NOT be the function captured at module setup. This is
        # the assertion that actually fails if the patch stops taking; the
        # value assertions below cannot, because a build node with no claude
        # on it answers `set()` honestly.
        self.assertIsNotNone(_REAL_LIVE_SEATS,
                             "nothing was captured at import")
        self.assertIsNot(proxywatch._live_seats, _REAL_LIVE_SEATS,
                         "the liveness stand-in is NOT in effect: the bound "
                         "attribute is still the real census")
        names, blind, per_seat = proxywatch._live_seats()
        self.assertEqual(names, set(),
                         "the liveness stand-in is not in effect — this module "
                         "is reading the real host again")
        self.assertIsNone(blind,
                          "the stand-in must report a MEASURED empty fleet; a "
                          "blind reading is a different claim and consumers "
                          "act on it differently")
        self.assertEqual(per_seat, {},
                         "the stand-in must report no PER-SEAT doubt either; "
                         "a refused seat is not an empty fleet")

    def test_the_validator_answers_WITHOUT_walking_the_host(self):
        """The stand-in must reach the door that actually costs the time.

        Patching a module attribute only helps if the consumer resolves it at
        CALL time; a caller holding an early reference would bypass it, and
        nothing about the patch itself would say so.
        """
        from helm import proxywatch
        seen = []
        real = proxywatch._live_seats
        def _counting():
            seen.append(1)
            return real()
        with mock.patch.object(proxywatch, "_live_seats", _counting):
            ok, why, _note = dispatches._validate_recipient_usable(
                "seat-a", False)
        # POSITIVE CONTROL ON THE ROUTE, not on the answer: the validator must
        # actually reach the module attribute at CALL time. If it held an early
        # reference, this counter stays 0 and the stand-in above would be
        # decorative — patched, in effect, and bypassed.
        self.assertTrue(seen,
                        "the validator never resolved proxywatch._live_seats "
                        "at call time, so patching that attribute cannot be "
                        "what makes this module fast")
        self.assertIsInstance(ok, bool,
                              "the validator did not answer through the "
                              "stand-in: %r / %r" % (ok, why))


class LandReqBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lr-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.repo = os.path.join(self.tmp, "repo")
        # THE SAME FOUR COMMITS EVERY TIME, SO THEY ARE BUILT ONCE (task/3039).
        # Building them here cost about 21 git spawns per test across the 18
        # modules and about 1,900 tests on this base. Each test gets its own
        # copy of the process's template (`_built`), so nothing it writes
        # reaches the template or the next test.
        from tests._tmphome import cross_tree_gate, repo_from_template
        facts = repo_from_template("landreq-base", LandReqBase._built,
                                   self.repo)
        self.main, self.a, self.b, self.c, self.side = (
            facts[k] for k in ("main", "a", "b", "c", "side"))
        # THIS FIXTURE REPO STANDS IN FOR A TREE THAT SHIPS HELM, which is
        # the only tree whose whole-suite command helm may assume — see
        # `helm_tree`. The template carries the package root; the override
        # that gates it from outside is this case's own.
        cross_tree_gate(self)
        # THIS FIXTURE IS ITS OWN PROJECT. Without the pin the dispatch write
        # door refuses every row here as foreign — a true refusal that says
        # nothing about landing. Inherited by CloseBase and LrApiBase, so one
        # pin covers test_lr_close and test_web_lr too.
        from tests._tmphome import pin_dispatch_home, pin_live_seats
        self._real_home_repo_id = pin_dispatch_home(self, self.repo)
        # THE STAND-IN RIDES THE BASE TOO (task/3039). setUpModule above covers
        # this module only; the other 17 modules built on this base run under
        # their own module, and their rows walked the host's process table.
        pin_live_seats(self)
        # Generated lifecycle verdicts use the current author/tier producer via
        # self.mark_verdict below. Only receipt capability is disabled here:
        # these synthetic repositories have no suite to gate. Explicit frozen
        # historical events bypass that helper and remain nonauthorizing.
        # THIS WHOLE MODULE TESTS THE PRE-GATE RECEIPT LIFECYCLE.
        #
        # An approve written by a GATE-CAPABLE writer must carry a minted
        # receipt to reach READY (dispatches.GATE_CAPS stamps the writer's
        # capability on the event; landreq._needs_gate reads it). These
        # fixtures record untokened approves and assert READY/stall behaviour,
        # and their subject is LANDING — whether a reviewed tip reaching trunk
        # is observed — not gating; their synthetic side branches have no suite
        # to run. They disable that capability, not record-time tier authority.
        #
        # PINNING IT HERE RATHER THAN AT EACH CALL SITE IS THE POINT. The
        # alternative was measured: after the enforcement went live the
        # two tests that were already red got fixed, and FIVE more were green only by
        # accident of when the suite ran. Per-call-site discipline cannot close
        # that class, because the next fixture someone adds has the same hole
        # and nothing asks them to think about it.
        #
        # PINNING THE WRITER, NOT A CLOCK, is the second correction: the first
        # version of this pinned a wall-time boundary, which made these tests'
        # correctness depend on the hour they ran. A writer capability is a
        # durable property of the row.
        #
        # The gate-capable contract (untokened approve stays REVIEWED, a bound
        # one reaches READY) is asserted in tests.test_gate.LandPathEnforcement.
        policy = mock.patch.object(dispatches, "GATE_CAPS", ())
        policy.start()
        self.addCleanup(policy.stop)
        self._generated_verdicts = {}

    def mark_verdict(self, *args, **kwargs):
        """Generate a current verdict; explicit author-mode calls stay literal.

        Not an authorization mock: the real producer captures the real isolated
        store, persists history, and runs the real validator. Direct frozen
        ledger fixtures never enter this helper.
        """
        if "bind_author" in kwargs:
            return dispatches.mark_verdict(*args, **kwargs)
        with self.verdict_author():
            row, err = dispatches.mark_verdict(*args, bind_author=True, **kwargs)
        if row is not None and err is None:
            self._generated_verdicts[row["id"]] = (args, dict(kwargs))
        return row, err

    def verdict_author(self, family="claude"):
        """One exact native author proof for CLI verdict fixtures."""
        from tests._verdict import native_author
        return native_author(self, family)

    def tearDown(self):
        landreq._GATE_INDEX_MEMO.clear()
        for key, value in self.prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _built(repo):
        """Build the fixture repository at `repo` and name its commits: trunk
        a-b-c on the default branch, and `side`, one commit off a, which is
        NOT on trunk until an integrator merges it (the READY/MERGED_LOCAL/
        LANDED axis rides on it). Run once per process; see setUp."""
        from tests._tmphome import plant_helm_root
        repo_ = _TemplateRepo(repo)
        repo_.git("init", "-q")
        repo_.git("config", "user.email", "test@example.com")
        repo_.git("config", "user.name", "Test")
        main = repo_.git("symbolic-ref", "--short", "HEAD")
        plant_helm_root(repo)
        a = repo_.commit("a")
        repo_.git("branch", "side", a)
        b = repo_.commit("b")
        c = repo_.commit("c")
        repo_.git("checkout", "-q", "side")
        side = repo_.commit("side", path="g")
        repo_.git("checkout", "-q", main)
        return {"main": main, "a": a, "b": b, "c": c, "side": side}

    def git(self, *args, cwd=None):
        p = subprocess.run(["git", "-C", cwd or self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def commit(self, text, path="state"):
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def gitdir(self):
        return os.path.realpath(os.path.join(self.repo, ".git"))

    def prune(self, sha, *refs):
        """Destroy `sha` for real: drop the named refs, expire every reflog,
        gc — then POSITIVELY assert the prune with a bare cat-file (rc 1),
        the MUST-HIT for the fixture itself. No cat-file mocking, ever.

        ON THE SHARED BASE because two suites now need one destroyed object:
        the close ladders (`stranded`) and the `reviewed-object-destroyed`
        retirement. A second copy of a prune recipe is a second thing that can
        stop actually pruning, and a fixture that only LOOKS pruned makes both
        suites green over an object that is still there.
        """
        for ref in refs:
            self.git("update-ref", "-d", ref)
        self.git("reflog", "expire", "--expire-unreachable=now", "--all")
        self.git("gc", "--prune=now", "--quiet")
        p = subprocess.run(["git", "--git-dir", self.gitdir(),
                            "cat-file", "-e", sha], capture_output=True)
        self.assertEqual(p.returncode, 1,
                         "the prune recipe MUST leave %s missing (rc 1), got "
                         "rc %d" % (sha[:12], p.returncode))

    def sidecar(self, old, new):
        """Record one ref translation exactly as migrate_refs --apply does."""
        path = os.path.join(landreq.home.global_dir(), ".state",
                            "ref-migrations.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dispatches.pk.now_ts(),
                                "repo": self.gitdir(), "source": "test",
                                "id": "x", "field": "reviewed_tip",
                                "old": old, "new": new}) + "\n")
        return path

    def content_equivalent_pair(self):
        """(reviewed tip NOT on trunk, carrier ON trunk) — the rung's REAL shape.

        A CARRIER EQUAL TO THE SOURCE CANNOT REPLAY, which is the trap every
        arm in this area fell into. Pointing the carrier at the reviewed tip
        makes the witness trivially self-consistent, and the write boundary
        replays a witness before recording it — replay requires the carrier be
        reachable from the PINNED TRUNK, and the reviewed tip is precisely the
        commit trunk does not carry. So "trivially valid" was the one witness
        guaranteed to be refused.

        This builds what trunk really looks like in the case the content rung
        exists for: the SAME added bytes on a DIFFERENT context. Same payload
        digest so the census matches, different patch-id so the stronger
        patch-equivalence rung does not catch it first, and the source delta
        applies cleanly onto the carrier's parent so application-equivalence
        holds.

        Lives on the BASE because test_lr_close's CloseBase extends it and
        needs the identical shape — two copies of a fixture is two fixtures.
        """
        self.git("checkout", "-q", self.main)
        self.commit("ctx-a", path="h")                    # trunk's context
        carrier = self.commit("CHANGE", path="h")         # trunk's carrier
        self.git("checkout", "-q", "-b", "ce-lane", self.a)
        self.commit("ctx-b", path="h")                    # a DIFFERENT context
        reviewed = self.commit("CHANGE", path="h")        # the same change
        self.git("checkout", "-q", self.main)
        return reviewed, carrier

    def add_origin(self):
        """Publish a trunk upstream so refs/remotes/origin/<trunk> exists and
        MERGED_LOCAL vs LANDED becomes observable."""
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", self.main)

    def dispatch(self, ref=None, **kw):
        # Independent work unless the test names a parent — see the same rule in
        # tests/test_dispatches.py. A land-loop fixture that stamped --new-work
        # on a superseding round would be asserting the defect.
        kw.setdefault("new_work", "supersedes" not in kw)
        # UNDELIVERED BY DEFAULT, which is what a land loop's FIRST stage means.
        # add() now marks a row delivered on its own mention, and delivery is
        # the very thing that moves the state OPEN -> AWAITING_REVIEW and the
        # debt integrator -> reviewer (landreq.OWED_BY). A fixture that posted a
        # mention would hand every walk-the-lifecycle arm a row that had already
        # left the stage it was written to observe. Arms that MEAN delivered
        # call _mark_delivered explicitly, as the delivered/stall arms already do.
        kw.setdefault("notify", False)
        row = dispatches.add("codex-3", kw.pop("lane", "lane/foo"),
                             ref=ref or self.side, repo=self.repo, **kw)
        self.assertIsNotNone(row)
        return row

    def age(self, rid, seconds, base=None):
        """Set the generated fixture clock, recreating verdicts at the producer.

        Never rehash an edited tier claim. Only verdicts generated by this
        fixture are recreated from their original call and controlled source.
        Direct historical verdict events keep their original bytes/timestamps.

        RETURNS THE INSTANT IT BACKDATED FROM, so a caller asserting an EXACT
        dwell can pin the projection's clock to the same one. The stamp is
        truncated to a whole second HERE while the projection reads the clock
        again when it RENDERS, so the dwell is `seconds` plus however many
        second boundaries crossed in between — usually none, and on a loaded
        node one. That produced a whole-suite red on a lane touching neither
        this file nor the projection (helm task/2232).

        `base` is an integer epoch and defaults to now. Its stamp is the value
        the old expression produced for every integer `seconds`, because
        subtracting an integer cannot change a fractional part.
        """
        base = int(time.time()) if base is None else int(base)
        path = dispatches.ledger_path()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(base - seconds))
        events = eventledger.events(path)
        generated = self._generated_verdicts.get(rid)
        position = next((i for i, row in enumerate(events)
                         if row.get("id") == rid and row.get("event") == "verdict"), None)
        def write(rows):
            with open(path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, separators=(",", ":")) + "\n")
        for row in events:
            if row.get("id") == rid and row.get("event") != "verdict":
                row["ts"] = stamp
        if generated and position is not None:
            # Rebuild only the synthetic pre-verdict state. Other work and any
            # later target transitions are restored after the real producer.
            write([row for i, row in enumerate(events) if row.get("id") != rid or i < position])
            args, kwargs = generated
            with mock.patch.object(dispatches.pk, "now_ts", return_value=stamp):
                row, err = self.mark_verdict(*args, **kwargs)
            self.assertIsNone(err, err)
            self.assertEqual(row["status"], "verdict")
            replacement = next(row for row in reversed(eventledger.events(path))
                               if row.get("id") == rid and row.get("event") == "verdict")
            events[position] = replacement
        write(events)
        return base


class _TemplateRepo(object):
    """LandReqBase's own `git` and `commit`, run on the template repository
    it builds once per process, so the template and a per-test build cannot
    differ in how a commit is made."""

    git = LandReqBase.git
    commit = LandReqBase.commit

    def __init__(self, repo):
        self.repo = repo


class AFailureChunkIsNotAGateReceiptTest(LandReqBase):
    """The receipt index skips a failure chunk, whose id is not a receipt id.

    ITS OWN CLASS, NEVER AN ARM ON `LandReqBase`. Every land-request suite
    subclasses that base for its fixture, so an arm written on the base runs
    once PER SUBCLASS under a new id each time: this one was collected 212
    times per whole-suite run to ask one question, and a regression in it
    would have read as 212 failures. The base carries fixtures only.
    """

    def test_failure_chunk_id_never_enters_the_gate_receipt_index(self):  # noqa: VACUOUS_ASSERTION — the computed chunk id and two real index reads prove the parser ran before asserting exclusion
        chunk = {"v": gate._FAILURE_CHUNK_VERSION,
                 "event": gate._FAILURE_CHUNK_EVENT,
                 "failures": [{"kind": "FAIL", "test": "tests.C.test_x"}]}
        chunk["id"] = gate._failure_chunk_id(chunk)
        os.makedirs(os.path.dirname(gate.receipts_path()), exist_ok=True)
        with open(gate.receipts_path(), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(chunk) + "\n")
        landreq._GATE_INDEX_MEMO.clear()
        self.assertEqual(landreq._gate_receipt_index(), {})
        self.assertNotIn(chunk["id"], landreq._gate_receipt_index())


class TheFixtureRepositoryIsCopiedFromATemplateTest(LandReqBase):
    """Every LandReqBase fixture gets its own copy of one repository built
    once per process (task/3039).

    MEASURED BEFORE: setUp spent about 21 git spawns per test building
    commits a, b, c and side, the same four commits every time, across the 18
    modules and about 1,900 tests that use this base (24% of lr_retire's
    profile). The copy is per test, so nothing one test writes reaches the
    template or the next test.
    """

    @contextlib.contextmanager
    def _second_fixture(self):
        """A second LandReqBase fixture in this same process, torn down when
        the block ends.

        INSIDE THE TEST BODY, NEVER AS THIS CASE'S CLEANUP. The second
        fixture's tearDown restores the environment it found, which is THIS
        case's. Registered with addCleanup it ran after this case's tearDown
        and put this case's HELM_HOME and HELM_CHAT_DIR back, naming a
        directory that tearDown had just removed: tests.test_dispatches then
        failed 11 arms on "claim lock is unavailable" in a whole-module run.
        tests/test_env_hygiene.py's DeletedTempPathTest is the arm for that."""
        case = LandReqBase("setUp")
        case.setUp()
        try:
            yield case
        finally:
            case.tearDown()
            case.doCleanups()

    def test_a_second_fixture_builds_no_repository(self):  # noqa: VACUOUS_ASSERTION — the counter is proven live under the same patch by the fixture's own commit, and the fixture's trunk is asserted
        built = []
        real = subprocess.run

        def counting(argv, *a, **kw):
            if list(argv[:1]) == ["git"] and ("init" in argv
                                             or "commit" in argv):
                built.append(argv)
            return real(argv, *a, **kw)

        with mock.patch.object(subprocess, "run", counting), \
                self._second_fixture() as other:
            by_setup = list(built)
            other.commit("counted")
            parent = other.git("rev-parse", "HEAD~1")
        self.assertEqual(len(built), 1,
                         "control: the counter sees a fixture's own commit")
        self.assertEqual(parent, other.c,
                         "control: the second fixture has its trunk")
        self.assertEqual(by_setup, [], "setUp built its repository again")

    def test_each_fixture_owns_its_copy(self):  # noqa: VACUOUS_ASSERTION — the empty status is controlled by the same `git status --porcelain` on this fixture's copy, asserted non-empty after its own write
        """The must-miss: a copy is never shared and never written back."""
        home = os.environ["HELM_HOME"]
        with self._second_fixture() as other:
            self.assertNotEqual(os.path.realpath(other.repo),
                                os.path.realpath(self.repo))
            self.assertEqual(
                (other.a, other.b, other.c, other.side, other.main),
                (self.a, self.b, self.c, self.side, self.main))
            mine = self.commit("only in this fixture")
            self.assertEqual(self.git("rev-parse", "HEAD"), mine)
            self.assertEqual(other.git("rev-parse", "HEAD"), other.c)
        with open(os.path.join(self.repo, "untracked"), "w") as fh:
            fh.write("only in this fixture\n")
        self.assertIn("untracked", self.git("status", "--porcelain"),
                      "control: status reports a change in a copy")
        with self._second_fixture() as third:
            self.assertEqual(third.git("rev-parse", "HEAD"), third.c,
                             "a test's commit reached the template")
            self.assertEqual(third.git("status", "--porcelain"), "",
                             "a test's file reached the template")
            self.assertEqual(os.environ.get("HELM_CROSS_TREE_GATE"), "1",
                             "control: the helm-tree mark still sets its "
                             "override")
        self.assertEqual(os.environ["HELM_HOME"], home,
                         "a second fixture did not give this case its "
                         "environment back")


class GeneratedVerdictClockTest(LandReqBase):
    def test_backdating_recreates_valid_authority_and_preserves_other_history(self):
        generated = self.dispatch()
        verdict, err = self.mark_verdict(
            generated["id"], self.side, "reviewed", polarity="approve")
        self.assertIsNone(err, err)
        original_anchor = verdict["verdict_tier_anchor"]
        other = self.dispatch()
        before = eventledger.events(dispatches.ledger_path())
        other_events = [e for e in before if e.get("id") == other["id"]]
        self.age(generated["id"], 7200)
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        verdict = current[generated["id"]]
        self.assertNotEqual(verdict["verdict_tier_anchor"], original_anchor)
        self.assertEqual(dispatches.approval_tier_for_verdict(verdict), ("none", None))
        self.assertEqual(verdict["verdict_version"], 4)
        after = eventledger.events(dispatches.ledger_path())
        self.assertEqual([e for e in after if e.get("id") == other["id"]], other_events)
        self.assertEqual([e["id"] for e in after], [e["id"] for e in before])
        lr, err = landreq.get(generated["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
        self.assertGreaterEqual(lr["dwell_s"], 7200)

    def test_backdating_does_not_rewrite_a_direct_historical_verdict(self):
        row = self.dispatch()
        event = {"v": 3, "event": "verdict", "seq": row["seq"] + 1,
                 "id": row["id"], "ts": "2026-08-01T00:00:00Z",
                 "reviewed_tip": self.side, "verdict_ref": "old review",
                 "polarity": "approve", "gate": "", "gate_caps": []}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), event))
        self.age(row["id"], 7200)
        verdicts = [e for e in eventledger.events(dispatches.ledger_path())
                    if e.get("id") == row["id"] and e.get("event") == "verdict"]
        self.assertEqual(verdicts, [event])
        current, err = dispatches.snapshot()
        self.assertIsNone(err, err)
        tier, why = dispatches.approval_tier_for_verdict(current[row["id"]])
        self.assertEqual(dispatches.tier_unknown_kind(tier), dispatches.TIER_PRE_TIER)
        self.assertIn("PRE-TIER", why)
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")


class EraStabilityTest(LandReqBase):
    """The audit, made permanent: THE CLOCK MUST NOT MOVE THESE TESTS.

    The bug this replaces was not "two tests are red". It was that the file's
    verdict timestamps came from the wall clock — `age()` backdates a row to
    `now - seconds` — so each fixture crossed GATE_BOUNDARY at its own moment
    and the suite would have gone red one test at a time over the following
    hours. Fixing the two that had already tripped is fixing instances of a
    class whose remaining members are indistinguishable from healthy.

    So this asserts the PROPERTY rather than the instances: with the base's
    pin in place, the same lifecycle holds under a clock far past the boundary.
    A future fixture inherits the pin automatically; if someone removes it,
    this is what says so.
    """

    def test_the_lifecycle_is_the_same_TEN_YEARS_FROM_NOW(self):
        """The clock must not move these tests. It used to: verdict stamps came
        from the wall clock (`age()` backdates to `now - seconds`), so each
        fixture crossed the old wall-time boundary at its own moment and the
        suite would have gone red one test at a time over the following hours.
        Nothing about the still-green ones said which kind they were."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        far = time.time() + 10 * 365 * 24 * 3600
        with mock.patch.object(time, "time", lambda: far):
            self.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertIsNone(lr["ungated"])

    def test_the_pin_is_what_makes_that_true_and_not_luck(self):
        """THE CONTROL. Without it the test above passes for a version where
        the gate check never fires at all — which is the failure mode the first
        `_needs_gate` actually shipped with (time.gmtime on an ISO string,
        False for every row, reading as enabled while enforcing nothing)."""
        self.assertEqual(dispatches.GATE_CAPS, ())            # the pin is on
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-2")
        current = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": current["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.side, "verdict_ref": "ok",
            "polarity": "approve", "gate": "",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}))
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIn("PRE-TIER", lr["ungated"])
        raw = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(landreq.gate_requirement(raw), "required")

        # Reach the gate boundary with current authority. A faulty binder is
        # deliberately injected: normal VERIFIED always supplies a token.
        control = self.dispatch(ref=self.side)
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(gate, "bind", return_value=(
                    "VERIFIED", "", "injected missing token")) as binding:
            got, why = self.mark_verdict(control["id"], self.side,
                                          "faulty binder control", polarity="approve")
        self.assertIsNone(why, why)
        binding.assert_called_once()
        self.assertEqual(dispatches.approval_tier_for_verdict(got), ("none", None))
        held, why = landreq.get(control["id"])
        self.assertIsNone(why, why)
        self.assertEqual(held["state"], "REVIEWED")
        self.assertIn("no minted gate receipt", held["ungated"])
        honest = self.dispatch(ref=self.side)
        with mock.patch.object(dispatches, "GATE_CAPS",
                               (dispatches.GATE_CAP_RECEIPT,)), \
                mock.patch.object(gate, "bind", return_value=(
                    "VERIFIED", "a" * 16, "test receipt")) as binding:
            got, why = self.mark_verdict(honest["id"], self.side,
                                          "honest binding control", polarity="approve")
        self.assertIsNone(why, why)
        binding.assert_called_once()
        self.assertEqual(landreq._approval_refusal(got), (None, "none"))
        self.assertEqual(landreq.get(honest["id"])[0]["state"], "READY")

    def test_a_declared_polarity_is_never_rendered_UNDECLARED(self):
        """The projection contradicted its own record. `lr show` printed
        "polarity UNDECLARED ... this row can never gain one" on EVERY REVIEWED
        row, guarded only on `closed_by_landing` and never on whether a polarity
        was actually present. So a row whose ledger polarity is `approve`
        rendered as having declared nothing — two lines under its own `verdict`
        line, which printed the approval in full. An integrator read the
        projection instead of the record and announced in the room that a
        reviewer's APPROVE carried no polarity; the retraction was public.

        The negative control is the sibling
        test_verdict_without_repo_binding_is_unobservable_never_a_false_stall,
        which builds a genuinely polarity-less row and still expects UNDECLARED.
        Both must hold, and that is the point: this asserts the line is
        SUPPRESSED when the ledger has a polarity, that one asserts it still
        FIRES when it does not. A fix that simply deleted the branch would go
        green here and red there."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-3")
        # The fixture is an APPROVAL-TIER demotion rather than a missing
        # receipt, for two reasons. It is the representative shape — 13 of the
        # 14 live rows carrying this defect are tier demotions and only 1 is a
        # missing receipt. And an ungated approve is no longer CONSTRUCTIBLE:
        # mark_verdict refuses it outright once the receipt capability is on,
        # so a fixture built that way records no verdict at all and the row
        # stays OPEN. Demotion at projection time is the durable seam.
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                return_value=("outside", "reviewer outside the permitted tier "
                              "(test)")):
            self.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            # The exact row the bug mis-rendered: REVIEWED, yet polarity IS set.
            self.assertEqual(lr["state"], "REVIEWED")
            self.assertEqual(lr["polarity"], "approve")
            text = run(["show", row["id"]])[1]
            # Assert the EFFECT, not the absence of a complaint: the declared
            # polarity must reach the surface, not merely go uncontradicted.
            self.assertIn("polarity  APPROVE", text)
            self.assertNotIn("UNDECLARED", text)
            # And the row still has to say why it is not READY — the tier,
            # never the verdict, which exists.
            self.assertIn("approval tier does not permit", text)

    def test_compact_list_reads_declared_polarity_from_the_dispatch_store(self):
        """The live #135 row carried canonical polarity=approve and
        ledger_refused=[verdict], yet compact `lr list` printed UNDECLARED because
        it inferred polarity from state=REVIEWED. Tier holds are the ordinary
        way a declared APPROVE becomes REVIEWED; the state is not the verdict.

        The refused historical event stays visible as a separate axis. This arm
        proves neither warning can overwrite the current accepted polarity."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-list-polarity")
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                return_value=("outside", "reviewer outside tier (test)")):
            self.mark_verdict(row["id"], self.side, "accepted approve",
                                    polarity="approve")
            # A later same-sequence duplicate is historical refusal evidence,
            # not the owner of current polarity.
            current = dispatches.snapshot()[0][row["id"]]
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": current["seq"],
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.side, "verdict_ref": "refused fix",
                "polarity": "fix", "gate": "", "gate_caps": []}))
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "REVIEWED")
            self.assertEqual(lr["polarity"], "approve")
            self.assertEqual(lr["polarity_source"], "dispatch-store")
            self.assertEqual(lr["ledger_refused"], ["verdict"])
            self.assertEqual(lr["owed_by"], "unknown (declared verdict held)")
            text = run(["list", "--all"])[1]
        self.assertIn("verdict polarity: dispatch store", text)
        self.assertIn("APPROVE from dispatch store", text)
        self.assertIn("dispatch fold REFUSED historical verdict event", text)
        self.assertNotIn("polarity UNDECLARED", text)


class LifecycleTest(LandReqBase):
    def test_open_awaiting_ready_landed_walk(self):
        row = self.dispatch()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err)
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["reviewer"], "codex-3")
        self.assertEqual(lr["review_sha"], self.side)
        self.assertFalse(lr["terminal"])

        dispatches._mark_delivered(row["id"], "post-1")
        self.assertEqual(landreq.get(row["id"])[0]["state"], "AWAITING_REVIEW")

        self.mark_verdict(row["id"], self.side, "review-post-9", polarity="approve")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "READY")      # reviewed tip not on trunk
        self.assertFalse(lr["landed"])

        self.git("merge", "--no-edit", "-q", "side")   # integrator lands it
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])
        self.assertTrue(lr["terminal"])

    def test_landed_needs_the_exact_reviewed_tip_not_just_any_trunk_move(self):
        # A verdict'd tip that never reaches trunk stays READY even as trunk
        # advances on unrelated work — landing is bound to THE reviewed commit.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.commit("more-trunk-work")             # trunk moves, side does not
        self.assertEqual(landreq.get(row["id"])[0]["state"], "READY")

    def test_merged_local_is_not_landed_until_the_push_is_observed(self):
        self.add_origin()                          # origin/main == b
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")   # local trunk past origin
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")
        self.assertTrue(lr["merged_local"])
        self.assertFalse(lr["landed"])
        self.assertFalse(lr["terminal"])

        self.git("push", "-q", "origin", self.main)    # push observed
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])

    def test_branch_and_author_bind_from_the_dispatch_send_row(self):
        # send delivers to the tmp chat lane (sign=False); the author is the
        # sender seat (HELM_CHAT_NAME), the reviewer is the recipient.
        row, why, sent = dispatches.send(
            "codex-3", "review", "review the branch tip", self.side,
            repo=self.repo, key="k1", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["author"], "integrator")   # HELM_CHAT_NAME seat
        self.assertEqual(lr["reviewer"], "codex-3")
        self.assertEqual(lr["state"], "AWAITING_REVIEW")   # delivery observed


class StallNamesTheOwnerTest(LandReqBase):
    """`lr stalls` printed the ROLE that owes a row and never the SEAT.

    MEASURED, twice: during a burndown that asked every
    seat for its own authored stalls, the listing answered ZERO while the
    reader had one, 13h50m old. A control found the only seat-looking strings in the whole
    output were a LANE NAME ("codex-refusal-ends-turn-reroute") whose author was
    a different seat — so the surface could not answer the question it was being
    read for, and answering it wrong looked exactly like answering it right."""

    def test_the_role_is_resolved_to_the_seat_that_holds_it(self):
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "author (kimi)")
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "reviewer", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "reviewer (ds4pro)")

    def test_a_role_with_no_single_holder_is_left_alone(self):
        """integrator/nobody/unknown name no seat, and inventing one would be
        this same defect pointing the other way."""
        # UNCONDITIONAL POSITIVE CONTROL, on the same observable and the same
        # inputs: the resolver DOES rewrite a role that has a holder. Without
        # this line every assertion below would also pass for a function that
        # returns its argument unchanged — which is exactly the bug this file
        # is about, one level up.
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": "kimi",
                                   "reviewer": "ds4pro"}),
            "author (kimi)")
        for role in ("integrator", "nobody", "nobody (undeclared)",
                     "unknown (declared verdict held)"):
            self.assertEqual(
                landreq._owed_by_whom({"owed_by": role, "author": "kimi",
                                       "reviewer": "ds4pro"}),
                role, role)

    def test_a_missing_seat_degrades_to_the_role_not_to_None(self):
        self.assertEqual(
            landreq._owed_by_whom({"owed_by": "author", "author": None}),
            "author")

    def test_ball_holder_is_the_one_server_side_display_answer(self):
        ordinary = {"owed_by": "author", "author": "kimi"}
        raw = {"contrary": True, "owed_by": "author", "author": "kimi"}
        missing = {}
        self.assertEqual(landreq.ball_holder(ordinary), ("author", "kimi"))
        self.assertEqual(landreq.ball_holder(raw), ("integrator", None))
        self.assertEqual(landreq.ball_holder(missing), ("unknown", None))
        for stamp in ("a", "b", "c"):
            row = {"contrary": True, "contrary_discharge": stamp,
                   "owed_by": "integrator"}
            self.assertEqual(landreq.ball_holder(row), ("nobody", None), stamp)
            self.assertEqual(landreq._owed_by_whom(row), "nobody", stamp)
        # fail-closed: a classifier uncertainty is still live debt
        unverified = {"contrary": True,
                      "contrary_discharge": "unverified",
                      "owed_by": "integrator"}
        self.assertEqual(landreq.ball_holder(unverified),
                         ("integrator", None))


class BuildVsReviewSurfaceTest(LandReqBase):
    """#176: `helm lr show` must distinguish BUILD vs REVIEW dispatches in
    state label, recipient role name, and timeline stage name.

    A BUILD row's recipient is a 'builder', not a 'reviewer', and its stage is
    'AWAITING_BUILD', not 'AWAITING_REVIEW'."""

    def test_build_row_surface_renders_builder_and_awaiting_build(self):
        row = self.dispatch(ref=self.side, lane="lane/test-build", kind="build")
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_BUILD")
        self.assertEqual(lr["owed_by"], "builder")
        self.assertEqual(landreq._owed_by_whom(lr), "builder (codex-3)")

        rendered = landreq._render_show(lr)
        self.assertIn("AWAITING_BUILD", rendered)
        self.assertNotIn("AWAITING_REVIEW", rendered)
        self.assertIn("author    integrator  ->  builder  codex-3", rendered)
        self.assertNotIn("reviewer codex-3", rendered)

    def test_review_row_surface_renders_reviewer_and_awaiting_review(self):
        row = self.dispatch(ref=self.side, lane="lane/test-review", kind="review")
        dispatches._mark_delivered(row["id"], "post-2")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")
        self.assertEqual(landreq._owed_by_whom(lr), "reviewer (codex-3)")

        rendered = landreq._render_show(lr)
        self.assertIn("AWAITING_REVIEW", rendered)
        self.assertNotIn("AWAITING_BUILD", rendered)
        self.assertIn("author    integrator  ->  reviewer codex-3", rendered)
        self.assertNotIn("builder  codex-3", rendered)
        self.assertIn("AWAITING_REVIEW", rendered)
        self.assertNotIn("AWAITING_BUILD", rendered)
        self.assertIn("author    integrator  ->  reviewer codex-3", rendered)
        self.assertNotIn("builder  codex-3", rendered)
        self.assertEqual(landreq._owed_by_whom({}), "unknown")

    def test_the_PRINTED_stall_line_names_the_seat(self):
        """The unit above proves the resolver; this proves it is WIRED. The
        defect was never in a helper — it was that the line never called one."""
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            landreq.cmd_lr(["stalls"])
        text = out.getvalue()
        # UNCONDITIONAL POSITIVE CONTROL: the row really is in this listing,
        # so a missing seat name means "not printed" and not "not stalled".
        self.assertIn(row["id"][:12], text)
        self.assertIn("owed by reviewer (%s)" % row["recipient"], text)

    def test_the_PRINTED_stall_line_SAYS_the_work_already_landed(self):
        """The unit arms prove the marker; THIS proves it is WIRED — the same
        reason test_the_PRINTED_stall_line_names_the_seat exists above it.

        A marker nothing calls is the built-not-wired shape this seat spent the
        night finding in other people's lanes, and the isolated arms would stay
        green while the print site never called it.
        """
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)

        from helm import seats_work_offer
        # UNCONDITIONAL POSITIVE CONTROL, first and on the same listing: with
        # the probe silent the row is present and carries NO landed marker, so
        # the sentence below is the wiring speaking rather than a listing that
        # prints it unconditionally.
        with mock.patch.object(seats_work_offer, "_offer_landing_state",
                               return_value=(None, None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                landreq.cmd_lr(["stalls"])
        quiet = out.getvalue()
        self.assertIn(row["id"][:12], quiet)
        self.assertNotIn("ALREADY ON TRUNK", quiet)

        with mock.patch.object(seats_work_offer, "_offer_landing_state",
                               return_value=(True, "d" * 40)):
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                landreq.cmd_lr(["stalls"])
        loud = out2.getvalue()
        self.assertIn(row["id"][:12], loud)
        self.assertIn("ALREADY ON TRUNK at " + "d" * 12, loud)
        # THE OWED-BY CLAUSE IS GONE FROM THE PRINTED LINE, not merely
        # contradicted further along it. The quiet run above proves the same
        # listing DOES carry "owed by reviewer" when nothing landed, so this
        # absence is the replacement working rather than a line that never
        # said it.
        self.assertIn("owed by reviewer", quiet)
        self.assertNotIn("owed by reviewer", loud)
        self.assertIn("owed by NOBODY", loud)

class StallTest(LandReqBase):
    def test_awaiting_review_stalls_at_the_dispatch_deadline(self):
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        self.age(row["id"], 3600)                  # far past the 60s deadline
        stalled, unavailable = landreq.stalls()
        self.assertIsNone(unavailable)
        self.assertEqual([lr["id"] for lr in stalled], [row["id"]])
        self.assertEqual(stalled[0]["state"], "AWAITING_REVIEW")
        self.assertTrue(stalled[0]["stalled"])

    def test_a_row_held_ON_THE_OWNER_is_not_a_machine_stall(self):
        """THE OWNER'S OWN QUEUE MUST NOT READ AS THE FLEET FAILING HIM.

        Measured: the owner's console showed seven in-flight rows, five
        STALLED, and three of those five were waiting on the OWNER's
        authorization — rendered as "owed by builder" and "owed by the
        integrator". A frozen board reads like a project to shut down. The
        row is not hidden and its dwell still runs; it simply stops being
        counted as a stage the machine blew.

        The PAIR is the point: same row, same dwell, same threshold, and the
        ONLY difference is who the hold names. Without the ordinary half this
        arm would pass on a projection that stopped counting stalls at all."""
        machine = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(machine["id"], "post-1")
        dispatches.mark_hold(machine["id"], "waiting on the build box")
        self.age(machine["id"], 3600)

        owner = self.dispatch(deadline_s=60, lane="lane/owner-gated")
        dispatches._mark_delivered(owner["id"], "post-2")
        dispatches.mark_hold(owner["id"], "needs a publication decision",
                             owner_gated=True)
        self.age(owner["id"], 3600)

        lr_machine = landreq.get(machine["id"])[0]
        lr_owner = landreq.get(owner["id"])[0]
        self.assertIs(lr_machine["owner_gated"], False)
        self.assertIs(lr_owner["owner_gated"], True)
        self.assertTrue(lr_machine["stalled"],
                        "a hold on a MACHINE dependency is still the fleet's "
                        "debt and must keep counting")
        self.assertFalse(lr_owner["stalled"],
                         "a row waiting on the owner is not a machine stall")

        stalled = [lr["id"] for lr in (landreq.stalls()[0] or [])]
        self.assertIn(machine["id"], stalled)
        self.assertNotIn(owner["id"], stalled,
                         "the stall list is what the console counts — an "
                         "owner-gated row in it is the defect this cures")

    def test_a_SOURCE_CLEAN_hold_stops_billing_the_reviewer_who_finished(self):  # noqa: VACUOUS_ASSERTION — the ordinary-hold half of the pair is the unconditional control on every observable asserted: it is stalled, owed by the reviewer, and present in the stall list
        """THE THIRD ANSWER ON THE HOLD AXIS, and the projection knew two.

        A source-clean hold is a structured claim that the reviewer read the
        delta and found nothing, and could not mint an approve only because
        one binds a whole-suite token the integrator's land gate produces. The
        stage stays AWAITING_REVIEW -- correctly, no verdict exists -- so
        `OWED_BY` kept answering "reviewer" for a review that was over.

        THE FAILURE MODE: rows read AWAITING_REVIEW (STALLED) for hours
        while held source-clean with the review finished, so anybody sweeping
        for stalls reads a queue of slow reviewers and chases the one seat
        that has already answered.

        THE PAIR IS THE POINT, exactly as in the owner-gated arm above: same
        row, same dwell, same threshold, and the only difference is which hold
        it carries. Without the ordinary half this would pass on a projection
        that had stopped counting stalls at all."""
        machine = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(machine["id"], "post-1")
        dispatches.mark_hold(machine["id"], "waiting on the build box")
        self.age(machine["id"], 3600)

        clean = self.dispatch(deadline_s=60, lane="lane/source-clean")
        dispatches._mark_delivered(clean["id"], "post-2")
        _out, why = dispatches.mark_hold(clean["id"], "awaiting the land gate",
                                         source_clean_tip=self.c)
        self.assertIsNone(why)
        self.age(clean["id"], 3600)

        lr_machine = landreq.get(machine["id"])[0]
        lr_clean = landreq.get(clean["id"])[0]
        # The projection CARRIES the third value; it was written by the
        # dispatch store and read by nobody.
        self.assertIsNone(lr_machine["source_clean_tip"])
        self.assertEqual(lr_clean["source_clean_tip"], self.c)

        self.assertTrue(lr_machine["stalled"],
                        "a hold on a MACHINE dependency is still the fleet's "
                        "debt and must keep counting")
        self.assertFalse(lr_clean["stalled"],
                         "the review is finished; billing this dwell names "
                         "the one seat that already did its part")

        # WHO OWES IT, which is the half that routes a human.
        self.assertEqual(lr_machine["owed_by"], "reviewer")
        self.assertEqual(lr_clean["owed_by"], "integrator")

        stalled = [lr["id"] for lr in (landreq.stalls()[0] or [])]
        self.assertIn(machine["id"], stalled)
        self.assertNotIn(clean["id"], stalled,
                         "the stall list is what an integrator sweeps — a "
                         "finished review in it is the defect this cures")

    def test_the_source_clean_row_SAYS_why_it_is_not_stalled(self):  # noqa: VACUOUS_ASSERTION — the positive assertions on the rendered line (SOURCE-CLEAN, the tip, INTEGRATOR) are unconditional and come first; the MUST-MISS on an ordinary hold is what makes them about the claim
        """Excluding the row from the count is half a cure. The STATE column
        still reads AWAITING_REVIEW with a growing dwell, so a row that merely
        stopped saying STALLED has gone from accusing the reviewer out loud to
        letting the reader supply the accusation themselves.

        This drives the RENDERED marks rather than the projection dict,
        because the marks are what a seat sweeping the board actually reads."""
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        _out, why = dispatches.mark_hold(row["id"], "awaiting the land gate",
                                         source_clean_tip=self.c)
        self.assertIsNone(why)
        self.age(row["id"], 3600)
        line = landreq._line(landreq.get(row["id"])[0])
        self.assertIn("SOURCE-CLEAN", line)
        self.assertIn(self.c[:12], line)
        self.assertIn("INTEGRATOR", line,
                      "the mark must NAME the holder: every reader of this "
                      "line is deciding whether the row is theirs")
        # MUST-MISS on the same observable: an ordinary hold does not acquire
        # the mark, so this is about the claim and not about the renderer.
        other = self.dispatch(deadline_s=60, lane="lane/ordinary")
        dispatches._mark_delivered(other["id"], "post-2")
        dispatches.mark_hold(other["id"], "waiting on the build box")
        self.age(other["id"], 3600)
        self.assertNotIn("SOURCE-CLEAN",
                         landreq._line(landreq.get(other["id"])[0]))

    def test_the_owner_row_SAYS_it_is_waiting_on_him_and_why(self):
        """Excluding the row from the count is half a cure — a row that stops
        alarming and says nothing has gone from mislabelled to invisible.
        This drives the RENDERED marks, not the projection dict, because the
        marks are what he reads."""
        row = self.dispatch(deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-1")
        dispatches.mark_hold(row["id"], "authorize the remote fab dogfood",
                             owner_gated=True)
        self.age(row["id"], 3600)
        lr = landreq.get(row["id"])[0]
        joined = landreq._line(lr)
        card = landreq.card(lr)
        self.assertEqual(landreq.ball_holder(lr), ("owner", None))
        self.assertIs(card["owner_gated"], True)
        self.assertEqual(card["hold_ts"], lr["hold_ts"])
        self.assertRegex(card["hold_ts"],
                         r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
        self.assertEqual(card["hold_reason"],
                         "authorize the remote fab dogfood")
        self.assertEqual((card["holder_role"], card["holder_seat"]),
                         ("owner", None))
        self.assertIn("WAITING ON THE OWNER", joined)
        self.assertIn("authorize the remote fab dogfood", joined,
                      "the dependency must ride with the banner — a holder "
                      "without a reason sends him to another tool to find "
                      "his own ask")
        self.assertNotIn("STALLED", joined,
                         "the two are mutually exclusive: a row cannot be "
                         "both his turn and a blown machine stage")

    def test_open_is_not_a_stallable_state(self):
        # OPEN = the dispatch was persisted but delivery was never confirmed.
        # The obligation was created but the reviewer was never told — billing
        # them for work they don't know exists is the fleet-stall root cause.
        # OPEN loops still appear in `lr list` as live obligations; they just
        # don't count as stalled because the integrator owes the notification.
        row = self.dispatch(deadline_s=60)
        self.age(row["id"], 3600)                   # far past the 60s deadline
        self.assertEqual(landreq.stalls()[0], [])    # not stallable
        # And the row says who owes it
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIsNone(lr["stall_threshold_s"])
        self.assertFalse(lr["stalled"])

    def test_ready_stalls_past_the_land_threshold(self):
        row = self.dispatch()
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        stalled = landreq.stalls()[0]
        self.assertEqual([lr["state"] for lr in stalled], ["READY"])

    def test_merged_local_stalls_past_its_cumulative_push_threshold(self):
        # A push loop sitting since its verdict past the MERGED_LOCAL threshold
        # IS a workflow gap — the local merge landed but the push never did.
        self.add_origin()                          # origin lacks the reviewed tip
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")   # local trunk only
        self.age(row["id"], landreq.LAND_STALL_S["MERGED_LOCAL"] + 60)
        stalled = landreq.stalls()[0]
        self.assertEqual([(lr["id"], lr["state"]) for lr in stalled],
                         [(row["id"], "MERGED_LOCAL")])
        self.assertTrue(stalled[0]["stalled"])

    def test_forward_progress_never_manufactures_a_stall(self):
        # A loop healthy in READY (dwell inside the land budget) must NOT flip
        # to STALLED the instant the integrator merges it locally. Thresholds
        # are cumulative from the verdict and MERGED_LOCAL >= READY, so the
        # merge clears the land-side wait, it does not invent a push stall.
        self.add_origin()
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] - 60)   # inside READY
        before = landreq.get(row["id"])[0]
        self.assertEqual(before["state"], "READY")
        self.assertFalse(before["stalled"])
        self.git("merge", "--no-edit", "-q", "side")             # forward progress
        after = landreq.get(row["id"])[0]
        self.assertEqual(after["state"], "MERGED_LOCAL")
        self.assertFalse(after["stalled"])                       # not a false alarm
        self.assertEqual(landreq.stalls()[0], [])

    def test_landed_and_young_loops_never_show_as_stalled(self):
        landed = self.dispatch(ref=self.side)
        self.mark_verdict(landed["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        self.age(landed["id"], 10 ** 6)            # ancient but terminal
        young = self.dispatch(ref=self.b, lane="lane/young")
        self.mark_verdict(young["id"], self.b, "ok",   # b IS on trunk
                                polarity="approve")
        self.assertEqual(landreq.stalls()[0], [])
        # the ancient landed loop is terminal, and b landed immediately.
        self.assertEqual(landreq.get(landed["id"])[0]["state"], "LANDED")
        self.assertEqual(landreq.get(young["id"])[0]["state"], "LANDED")

    def test_verdict_without_repo_binding_is_unobservable_never_a_false_stall(self):
        # The live-ledger bug this guards: an old verdict'd row with no repo_id
        # cannot have its landing observed, so it must NOT be asserted READY +
        # STALLED (it may well have landed a day ago). Unobservable, not a gap.
        #
        # Since verdict POLARITY landed, this legacy row is REVIEWED rather than
        # READY, and that is a strictly stronger form of the same guarantee: the
        # row never declared whether the review APPROVED, so no stall is billable
        # in either direction. Note the evidence prose here literally reads
        # "CLEAR; landed as merge x" — a keyword sniffer would happily call this
        # an approval, which is exactly the per-case inference this module
        # refuses to make.
        old = "2026-07-01T00:00:00Z"
        legacy = {"id": "4f65d90d", "ts": old, "recipient": "codex-orch",
                  "lane": "work-adopt", "ref": self.a, "tip": self.a,
                  "note": None, "deadline_s": 60, "source": "old",
                  "status": "verdict", "verdict_ref": "CLEAR; landed as merge x",
                  "reviewed_tip": self.a, "last_updated": old}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        lr = landreq.get("4f65d90d")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertFalse(lr["observable"])
        self.assertFalse(lr["stalled"])           # aged far past, still no gap
        self.assertEqual(landreq.stalls()[0], [])
        self.assertIn("polarity UNDECLARED", run(["list", "--all"])[1])
        self.assertIn("UNDECLARED", run(["show", "4f65d90d"])[1])

    def test_stalls_are_ordered_longest_first(self):
        older = self.dispatch(lane="lane/older")
        self.mark_verdict(older["id"], self.side, "ok", polarity="approve")
        self.age(older["id"], 7200)
        newer = self.dispatch(ref=self.side, lane="lane/newer")
        self.mark_verdict(newer["id"], self.side, "ok", polarity="approve")
        self.age(newer["id"], 3700)
        order = [lr["id"] for lr in landreq.stalls()[0]]
        self.assertEqual(order, [older["id"], newer["id"]])


class AlreadyOnTrunkTest(LandReqBase):
    """A ROW WHOSE WORK IS ALREADY IN HISTORY, still rendering as owed.

    `_will_observe_git` reads git only for a row whose status is
    verdict/closed, on a premise spelled twice in `landreq`: a row with no
    verdict yet cannot have landed, so "there is nothing that could have
    landed". That holds only while every land goes through helm's verdict
    door, and it is false for any repository helm does not gate -- an outside
    project, a human merge, a maintainer pushing straight to main. The
    pre-verdict row then keeps billing a reviewer who cannot act, and a
    whole-suite gate spent on it gates a tree already in history.

    EVERY ARM HERE IS A PAIR. `self.b` is on trunk and `self.side` is not, so
    each absence assertion sits beside an unconditional positive taken from
    the same projection through the same call -- without it these would pass
    on a projection that had stopped answering at all."""

    def _row(self, ref, lane):
        """One delivered, aged, PRE-VERDICT row projected from the board."""
        row = self.dispatch(ref=ref, lane=lane, deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-" + lane)
        self.age(row["id"], 3600)
        return landreq.get(row["id"])[0]

    def test_a_row_already_on_trunk_stops_billing_the_reviewer_who_cannot_act(self):  # noqa: VACUOUS_ASSERTION — the not-on-trunk half of the pair is the unconditional control on every observable asserted: it is answered False, it is stalled, and it carries no mark
        """THE STALL IS AN AGE AND IT READS AS A SLOW REVIEWER.

        Nobody can review work that is already in history, and the author has
        no rebase that would make it landable -- it landed. Leaving the stall
        up while adding the correction beside it publishes both, and the
        accusation is the one that routes attention."""
        contained = self._row(self.b, "lane/contained")
        loose = self._row(self.side, "lane/loose")

        self.assertIs(contained["trunk_contains_tip"], True,
                      "trunk contains this row's pinned tip and the board "
                      "must say so")
        self.assertEqual(contained["trunk_contains_proof"],
                         landreq.PROOF_ANCESTOR)
        self.assertIs(loose["trunk_contains_tip"], False,
                      "the control's tip is a real commit that is NOT on "
                      "trunk — PROVEN absent, which is a different answer "
                      "from unanswered")

        self.assertFalse(contained["stalled"],
                         "a row whose change is on trunk has no reviewer who "
                         "can act, so the stall clock is billing nobody")
        self.assertTrue(loose["stalled"],
                        "the ordinary half must keep counting, or this arm "
                        "would pass on a board that stopped counting stalls")

    def test_the_ALREADY_ON_TRUNK_mark_names_a_ledger_gap_not_a_slow_reviewer(self):
        """THE WORDS MATTER, because three different parties are one word apart.

        STALLED accuses the reviewer. A distance behind trunk accuses the
        author of owing a rebase. What is actually missing is a VERDICT: the
        change reached trunk without one, and the close door is right to
        refuse to stamp it landed because git cannot prove a review
        happened."""
        contained = landreq._line(self._row(self.b, "lane/marked"))
        loose = landreq._line(self._row(self.side, "lane/unmarked"))

        self.assertIn("ALREADY ON TRUNK", contained)
        self.assertIn("NO VERDICT", contained)
        self.assertNotIn("ALREADY ON TRUNK", loose,
                         "a row that is NOT on trunk must not carry the mark, "
                         "or the mark says nothing")

    def test_the_board_card_carries_ALREADY_ON_TRUNK_the_CLI_already_prints(self):  # noqa: VACUOUS_ASSERTION — the loose row's absent mark is paired with the contained row's PRESENT mark from the same renderer, and each card's field is asserted True / False by identity
        """ONE ROW, TWO SURFACES, ONE ANSWER. `helm lr list` prints ALREADY
        ON TRUNK for a pre-verdict row whose pinned tip trunk holds; the card
        `/api/lr` serves and the board's kanban join must carry the same
        field, or the owner's kanban draws that row under "review". Both
        copy it; neither recomputes it."""
        from helm import web_board
        contained = self._row(self.b, "lane/on-main")
        loose = self._row(self.side, "lane/waiting")
        self.assertIn("ALREADY ON TRUNK", landreq._line(contained))
        self.assertNotIn("ALREADY ON TRUNK", landreq._line(loose))
        cards = [landreq.card(contained), landreq.card(loose)]
        self.assertIs(cards[0]["trunk_contains_tip"], True)
        self.assertIs(cards[1]["trunk_contains_tip"], False,
                      "the control is a PROVEN no, not an unasked None")
        body = {"withheld": {"scope": "fixture"}, "loops": cards,
                "read_age_s": 1}
        _sec, joined = web_board._lands_join(lambda _qs: (body, 200))
        lanes = joined["fixture"]["lanes"]
        loops = {c["lane"]: c for c in lanes["loops"]}
        # THE CONTAINED ROW IS COUNTED ON THE ON-MAIN LINE, not drawn as a
        # card (task/2381); the loose row, a PROVEN no, is a live card
        self.assertEqual(sorted(loops), [loose["lane"]])
        self.assertIs(loops[loose["lane"]]["trunk_contains_tip"], False)
        self.assertEqual(lanes["on_main"]["lanes"], [contained["lane"]])
        self.assertEqual(lanes["on_main"]["count"], 1)

    def test_a_foreign_row_gets_no_git_leg_even_when_its_tip_IS_on_trunk(self):  # noqa: VACUOUS_ASSERTION — the owned row is the unconditional positive control on the SAME observable and the SAME tip: out['own']['trunk_contains_tip'] is asserted True in this arm, so a board answering nothing goes red here
        """AUTHORITY IS NOT INHERITED FROM THE ORACLE, so it is asserted here.

        A row with no carrier never reaches `_landing_proofs` today, so asking
        for every pre-verdict row reaches it for rows that had no git leg at
        all. `_landing_proofs` does not itself check provenance. THE TIP IS
        THE SAME CONTAINED COMMIT IN ALL THREE ROWS, so a leak shows up as
        True rather than as a difference that could be blamed on the sha."""
        base = self._row(self.b, "lane/authority")
        out = {}
        for name, extra in (("own", {}), ("foreign", {"foreign": True}),
                            ("unresolved", {"project_unresolved": True})):
            row = dict(base, id=name, terminal=False, observable=False, **extra)
            row.pop("trunk_contains_tip", None)
            row.pop("trunk_contains_proof", None)
            out[name] = row
        landreq._annotate_trunk_containment(out, gitdir=self.repo)

        self.assertIs(out["own"]["trunk_contains_tip"], True,
                      "the owned row is the control: same tip, and it IS "
                      "answered")
        self.assertIsNone(out["foreign"]["trunk_contains_tip"],
                          "a foreign row must get no git leg — running git in "
                          "a repository this board cannot call its own is an "
                          "authority claim, not a display choice")
        self.assertIsNone(out["unresolved"]["trunk_contains_tip"],
                          "project_unresolved is a REAL path the registry "
                          "cannot assign, and authority takes the strict "
                          "answer")

    def test_a_row_behind_trunk_says_so_and_a_row_ALREADY_on_it_never_does(self):
        """A DISTANCE AND AN AGE ACCUSE DIFFERENT PEOPLE, and containment wins.

        `STALLED` is how long a row has sat and reads as a slow reviewer.
        BEHIND is how far its pinned tip is from trunk and says the row cannot
        land whoever reviews it -- the land door is ff-only. A gate spent on a
        stale base does not fail, it congratulates you.

        AND AN ANCESTOR OF TRUNK IS ALSO BEHIND IT. This fixture makes that
        concrete: `self.b` is on trunk AND one commit behind its tip, so a
        distance rendered without asking containment first would bill the
        author for rebasing work that already landed. The contained row
        carrying NO distance is the whole routing claim, which is why both
        halves are asserted from the same projection."""
        loose = self._row(self.side, "lane/behind")
        contained = self._row(self.b, "lane/ahead")

        self.assertTrue(loose["tip_behind_trunk"],
                        "a pre-verdict row is never `observable`, so the one "
                        "number that says this cannot land was missing from "
                        "exactly the rows a reviewer is asked to spend a pass "
                        "on")
        self.assertIn("BEHIND trunk", landreq._line(loose))
        self.assertIsNone(
            loose["base_behind"],
            "and it must NOT be written into `base_behind`, whose EMPTINESS "
            "is load-bearing: that field is bound to `observable`, an "
            "unobservable READY row is pinned UNMEASURED rather than zero, "
            "and the STALE_BASE_BEHIND rung refuses on the number once it is "
            "there")

        self.assertIsNone(contained["tip_behind_trunk"],
                          "`self.b` IS behind trunk, and measuring that would "
                          "publish a rebase debt on work already in history")
        self.assertNotIn("BEHIND trunk", landreq._line(contained))
        self.assertIn("ALREADY ON TRUNK", landreq._line(contained),
                      "the contained row is the control: it renders, it just "
                      "renders the other fact")

    def test_a_distance_is_never_published_while_ALREADY_LANDED_is_unruled_out(self):
        """A REBASED LAND IS NOT_ANCESTOR, AND THAT IS NOT A NO.

        The two instruments disagree about a rebased land BY DESIGN and it is
        measured, not supposed: ancestry answers NOT_ANCESTOR for content that
        reached trunk under new object ids, while patch identity answers
        PATCH_EQUIVALENT. The containment walk therefore depends on the typed
        oracle to settle that case -- and it reaches that oracle only after the
        shared derive budget may already be spent, past which the oracle
        answers UNKNOWN.

        SO THE DANGEROUS ROW IS: distance measured, containment UNKNOWN. If the
        mark renders there, the board tells the author of ALREADY LANDED work
        to rebase it -- the exact wrong accusation this leg exists to stop,
        reintroduced one branch below where it was cured. Unknown is not a
        ruled-out, so it must render nothing.

        THE CONTROL IS THE SAME ROW THROUGH THE REAL ORACLE, where containment
        comes back a PROVEN absent and the mark DOES render -- otherwise this
        would pass on a renderer that had stopped printing distances at all."""
        base = self._row(self.side, "lane/unruled")
        real = landreq._landing_proofs(self.repo)

        def unknown_proof(_owner, _carrier):
            return landreq.PROOF_UNKNOWN
        unknown_proof.pin = real.pin

        def fresh(name):
            row = dict(base, id=name, terminal=False, observable=False)
            for k in ("trunk_contains_tip", "trunk_contains_proof",
                      "tip_behind_trunk"):
                row.pop(k, None)
            return row

        unknown, absent = fresh("unknown"), fresh("absent")
        landreq._annotate_trunk_containment(
            {"unknown": unknown}, gitdir=self.repo, proof=unknown_proof,
            cache={})
        landreq._annotate_trunk_containment(
            {"absent": absent}, gitdir=self.repo, cache={})

        self.assertIsNone(unknown["trunk_contains_tip"],
                          "an unanswered containment is None, never False")
        self.assertTrue(unknown["tip_behind_trunk"],
                        "and the distance IS measured on it — which is "
                        "exactly why the RENDER has to be the thing that "
                        "refuses")
        self.assertNotIn("BEHIND trunk", landreq._line(unknown),
                         "a rebased land looks like this, and telling its "
                         "author to rebase is the accusation this leg exists "
                         "to stop")

        self.assertIs(absent["trunk_contains_tip"], False,
                      "the control reaches a PROVEN absent through the real "
                      "oracle")
        self.assertIn("BEHIND trunk", landreq._line(absent),
                      "and there the distance is published, so this pair "
                      "cannot pass on a renderer that prints nothing")

    def test_provenance_is_read_off_the_LEDGER_row_not_the_projection(self):  # noqa: VACUOUS_ASSERTION — the owned row is the unconditional positive control on the same observable and the same tip: out['own']['trunk_contains_tip'] is asserted True in this arm
        """THE ORDERING HOLE, pinned so it cannot come back.

        `_lr` builds a fresh projection dict, so `foreign` and
        `project_unresolved` are re-attached to `out` in a loop that runs
        AFTER the annotators. An authority check reading only `lr` therefore
        tests a flag that is not set yet and admits every foreign row -- and
        the listing where that matters is exactly the disclosure listing,
        whose whole content is foreign rows. This arm hands the annotator
        rows with CLEAN projections and the provenance only on the ledger
        side, which is the shape production actually has at that instant."""
        base = self._row(self.b, "lane/provenance")
        out, eligible = {}, {}
        for name, src in (("own", {}), ("foreign", {"foreign": True}),
                          ("unresolved", {"project_unresolved": True})):
            row = dict(base, id=name, terminal=False, observable=False)
            row.pop("trunk_contains_tip", None)
            row.pop("trunk_contains_proof", None)
            row.pop("foreign", None)
            row.pop("project_unresolved", None)
            out[name] = row
            eligible[name] = src

        landreq._annotate_trunk_containment(out, eligible, gitdir=self.repo)

        self.assertIs(out["own"]["trunk_contains_tip"], True,
                      "the owned row is the control: identical projection, "
                      "identical tip, and it IS answered")
        self.assertIsNone(out["foreign"]["trunk_contains_tip"],
                          "the projection carries no marker yet, so the "
                          "ledger row is the only thing that can refuse this "
                          "— and it must")
        self.assertIsNone(out["unresolved"]["trunk_contains_tip"],
                          "same window, same refusal")

    def test_the_containment_annotator_asks_about_the_rows_own_tip(self):
        """PINS THE ORACLE'S PARAMETER SHAPE, which the annotator relies on.

        `proof_for` reads `repo_id` off the owner and `repo_id` plus
        `reviewed_tip` off the carrier, and nothing else. The annotator hands
        it the row's OWN pinned tip under that key, because `reviewed_tip` on
        the row itself means "the tip a reviewer actually read" and is None on
        exactly the pre-verdict population this exists for. If either key
        drifts, the oracle answers no-tip for every row and the cure goes
        quietly dead -- so the drift has to go red here instead."""
        lr = self._row(self.b, "lane/contract")
        repo_id = lr["repo_id"]
        proof = landreq._landing_proofs(self.repo)
        owner = {"repo_id": repo_id}

        self.assertEqual(
            proof(owner, {"repo_id": repo_id, "reviewed_tip": self.b}),
            landreq.PROOF_ANCESTOR,
            "the carrier's tip key is `reviewed_tip`")
        self.assertEqual(
            proof(owner, {"repo_id": repo_id}), landreq.PROOF_NO_TIP,
            "and a carrier without that key answers no-tip — which is what "
            "the whole board would get if the annotator spelled it wrong")
        self.assertTrue(
            landreq._sha(proof.pin(repo_id) or ""),
            "the oracle exposes its own trunk pin, so the cheap ancestry "
            "route asks about the SAME snapshot rather than resolving trunk "
            "a second time")

    def test_an_unanswerable_containment_is_never_reported_as_not_landed(self):  # noqa: VACUOUS_ASSERTION — the absent row is the unconditional control on the same observable: out['absent']['trunk_contains_tip'] is asserted to be exactly False, which an annotator that refused every row could not produce
        """TRI-STATE, AND None IS NOT False.

        A question that could not be answered must never be handed back as
        "this did not land" — that is the reassuring answer, and it is the one
        nobody re-checks. The control is a tip that IS answerable and IS
        absent, so this arm cannot pass by the annotator refusing everything."""
        # DERIVED, never spelled: a literal 40-hex run in this file reads to
        # the docref rung as a cited commit it cannot resolve.
        import hashlib
        missing = hashlib.sha1(b"a commit this fixture never made").hexdigest()

        base = self._row(self.side, "lane/tristate")
        out = {}
        for name, tip in (("absent", self.side), ("unanswerable", missing)):
            row = dict(base, id=name, terminal=False, observable=False,
                       pinned_tip=tip)
            row.pop("trunk_contains_tip", None)
            row.pop("trunk_contains_proof", None)
            out[name] = row
        landreq._annotate_trunk_containment(out, gitdir=self.repo)

        self.assertIs(out["absent"]["trunk_contains_tip"], False,
                      "a real commit that is not on trunk is PROVEN absent")
        self.assertIsNot(out["unanswerable"]["trunk_contains_tip"], False,
                         "a sha this repository has never seen is UNKNOWN, "
                         "and unknown must never be published as a no")


class BuildBaseIsNotALandTest(LandReqBase):
    """A BUILD ROW'S TIP IS THE BASE IT WAS SENT AGAINST, NOT ITS WORK.

    The owner read two lanes as LANDED the moment they were sent: each was a
    BUILD dispatched with `--ref` the trunk itself, so its pinned tip was
    already on trunk and the containment walk said "ALREADY ON TRUNK — this
    work is in history". No work existed yet. A build is building until its
    lane carries a tip of its own; only then can that WORK be on trunk, and
    the question is asked of the lane through `work.lanes_landed`, the
    producer `helm work list` prints.

    Every arm pairs the build row with the REVIEW row on the same pinned tip,
    which is contained — so an annotator that stopped answering fails too."""

    def _row(self, ref, lane, kind):
        row = self.dispatch(ref=ref, lane=lane, kind=kind, deadline_s=60)
        dispatches._mark_delivered(row["id"], "post-" + lane)
        self.age(row["id"], 3600)
        return row["id"]

    def _lane(self, lane, base, commits=(), merge=False):
        """A real lane branch under `lane/`, minted and committed on the way
        a seat does it, so its reflog tells authorship the way it does live."""
        branch = "lane/" + lane
        self.git("branch", branch, base)
        self.git("checkout", "-q", branch)
        tip = base
        for text in commits:
            tip = self.commit(text, path=lane)
        self.git("checkout", "-q", self.main)
        if merge:
            self.git("merge", "--no-edit", "-q", branch)
        return tip

    def get(self, rid):
        return landreq.get(rid)[0]

    def test_a_build_sent_against_trunk_itself_is_not_already_on_trunk(self):
        build = self._row(self.c, "sent-at-trunk", "build")
        review = self._row(self.c, "review-at-trunk", "review")
        b, r = self.get(build), self.get(review)
        self.assertEqual(b["state"], "AWAITING_BUILD")
        self.assertIsNot(b["trunk_contains_tip"], True,
                         "a build whose lane authored nothing was read as "
                         "work already in history")
        self.assertNotIn("ALREADY ON TRUNK", landreq._line(b))
        self.assertTrue(b["stalled"], "a build nobody started is a stall; "
                        "reading its base as landed silenced it")
        # THE CONTROL, same pinned tip: a REVIEW of that commit IS contained
        self.assertIs(r["trunk_contains_tip"], True)
        self.assertIn("ALREADY ON TRUNK", landreq._line(r))
        self.assertIs(landreq.card(r)["trunk_contains_tip"], True)
        self.assertIsNot(landreq.card(b)["trunk_contains_tip"], True)

    def test_a_build_sent_against_an_ancestor_of_trunk_is_not_landed_either(self):  # noqa: VACUOUS_ASSERTION — the review row on the SAME pinned tip is asserted `trunk_contains_tip` True by identity in this arm, and the build row's own state, stall and printed line are asserted present first
        build = self.get(self._row(self.b, "sent-at-ancestor", "build"))
        # THE POSITIVE CONTROL on the same row and line: it is the build,
        # projected and billed, so the absent mark below is a measurement
        self.assertEqual(build["state"], "AWAITING_BUILD")
        self.assertTrue(build["stalled"])
        self.assertIn("AWAITING_BUILD", landreq._line(build))
        self.assertIsNot(build["trunk_contains_tip"], True)
        self.assertNotIn("ALREADY ON TRUNK", landreq._line(build))
        self.assertIs(self.get(self._row(self.b, "ctl-ancestor", "review"))
                      ["trunk_contains_tip"], True)

    def test_a_lane_claimed_at_trunk_with_nothing_authored_is_still_building(self):
        build = self._row(self.c, "claimed-only", "build")
        self._lane("claimed-only", self.c)
        b = self.get(build)
        self.assertIs(b["trunk_contains_tip"], False,
                      "the lane was measured: it authored nothing, so none "
                      "of this build's work is on trunk")
        self.assertTrue(b["stalled"])

    def test_a_lane_tip_of_its_own_that_is_not_on_trunk_is_building(self):
        build = self._row(self.c, "built-open", "build")
        self._lane("built-open", self.c, commits=("work",))
        b = self.get(build)
        # UNANSWERED, NOT NO: the lane holds commits trunk lacks by object id,
        # and only patch identity — too dear to ask of every build row on
        # every projection — could say whether they landed rebased
        self.assertIsNone(b["trunk_contains_tip"])
        # THE POSITIVE CONTROL on the same line: the row is there and billed
        self.assertIn("AWAITING_BUILD", landreq._line(b))
        self.assertTrue(b["stalled"])
        self.assertNotIn("ALREADY ON TRUNK", landreq._line(b))

    def test_a_lane_tip_of_its_own_that_reached_trunk_IS_already_on_trunk(self):
        """THE OTHER HALF: once the lane carries work and that work is on
        trunk, the build row really is a ledger gap — work in history with no
        verdict — and says so exactly as a review row does."""
        build = self._row(self.c, "built-landed", "build")
        self._lane("built-landed", self.c, commits=("work",), merge=True)
        b = self.get(build)
        self.assertIs(b["trunk_contains_tip"], True)
        self.assertIn("ALREADY ON TRUNK", landreq._line(b))
        self.assertFalse(b["stalled"])

    def test_the_gone_rungs_are_the_census_own_words(self):  # noqa: VACUOUS_ASSERTION — every rung assertion is an exact equality on a verdict the census returned, and the readable-chain control asserts the PLACED reason on the same row before the polarity rung is driven
        """`FRONTIER_GONE_RUNGS` decides which UNCLASSIFIED rows the owner
        board folds away, so each of its words is driven through the census
        here: a reviewed commit git gc really pruned answers `object`, and a
        placed row whose chain polarity cannot be read answers `polarity`."""
        self.git("checkout", "-q", "-b", "doomed", self.a)
        doomed = self.commit("doomed", path="doomed")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=doomed, lane="pruned-away-lane")
        dispatches._mark_delivered(row["id"], "post-pruned")
        _out, err = self.mark_verdict(row["id"], doomed, "reviewed",
                                      polarity="fix")
        self.assertIsNone(err, err)
        self.git("branch", "-D", "doomed")
        self.prune(doomed)
        got = landreq.off_frontier_reason(self.get(row["id"]))
        self.assertEqual(got["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(got["rung"], "object")
        self.assertIn(got["rung"], landreq.FRONTIER_GONE_RUNGS)
        placed = self.commit("placed-on-trunk", path="placed")
        other = self.dispatch(ref=placed, lane="placed-away-lane")
        dispatches._mark_delivered(other["id"], "post-placed")
        _out, err = self.mark_verdict(other["id"], placed, "reviewed",
                                      polarity="fix")
        self.assertIsNone(err, err)
        lrs, _raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable)
        # THE CONTROL: with the chain readable the row is PLACED
        self.assertEqual(landreq.frontier_verdicts([lrs[other["id"]]],
                                                   lrs=lrs)[other["id"]]
                         ["reason"], landreq.OFF_FRONTIER_LANDED_ANCESTRY)
        with mock.patch.object(landreq, "_chain_polarity",
                               return_value=(None, None, "chain unreadable")):
            got = landreq.frontier_verdicts([lrs[other["id"]]],
                                            lrs=lrs)[other["id"]]
        self.assertEqual(got["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED)
        self.assertEqual(got["rung"], "polarity")
        self.assertEqual(set(landreq.FRONTIER_GONE_RUNGS),
                         {"object", "polarity"})

    def test_the_census_does_not_place_a_build_by_its_base_either(self):  # noqa: VACUOUS_ASSERTION — the verdicted review on the SAME pinned commit is asserted PLACED by exact reason in this arm, and the build's rung is an exact equality on a verdict the census returned
        """THE ANNOTATOR'S CURE HAS A SIBLING IN THE CENSUS. `off_frontier_reason`
        reads a row's dispatch ref when it has no reviewed tip, and for a
        BUILD that ref is the base it was sent against: a build sent at trunk
        whose builder has not yet claimed its lane — no branch, no room, no
        lease — walked the ladder to `landed-by-ancestry`, so the owner board
        folded a build nobody had started into "left over after landing" and
        `helm lr retire --off-frontier` offered to close it as landed. A build
        with no reviewed tip has no commit to place: UNCLASSIFIED at the `tip`
        rung, which the board keeps listed (`scheduler.collapse_class` None)
        and the retire door never takes. THE CONTROL is a VERDICTED review on
        the same commit, which IS placed by ancestry (an unverdicted review
        has no full-sha tip of its own and answers the `tip` rung too) — so a
        census that stopped placing anything fails here as well."""
        from helm import scheduler
        build = self.get(self._row(self.c, "sent-unclaimed", "build"))
        review = self._row(self.c, "review-unclaimed", "review")
        _out, err = self.mark_verdict(review, self.c, "reviewed",
                                      polarity="fix")
        self.assertIsNone(err, err)
        got = landreq.off_frontier_reason(build)
        self.assertEqual(got["reason"], landreq.OFF_FRONTIER_UNCLASSIFIED,
                         "a build was placed by the base it was sent "
                         "against: %r" % got)
        self.assertEqual(got["rung"], "tip")
        self.assertIsNone(scheduler.collapse_class(
            {"frontier": got["reason"], "frontier_rung": got["rung"]}),
            "the owner board folded a build nobody has started")
        ctl = landreq.off_frontier_reason(self.get(review))
        self.assertEqual(ctl["reason"], landreq.OFF_FRONTIER_LANDED_ANCESTRY,
                         ctl)

    # -- task/2381 round 2: patch identity for free --------------------------

    def _picked(self, lane):
        """A lane whose one authored commit lands on trunk by CHERRY-PICK, the
        way this repository lands: trunk carries the same patch under a new
        object id, so ancestry cannot see it. -> the lane's own tip."""
        tip = self._lane(lane, self.c, commits=("work " + lane,))
        # `-x` names the source commit, as a train's pick does: without a
        # message of its own, a pick made in the same second as the commit it
        # copies IS that commit, and trunk would hold it by ancestry
        self.git("cherry-pick", "-x", tip)
        return tip

    @staticmethod
    def _git_argv():
        """(argvs, patch): every git argv any caller spawns while the patch is
        active — landreq's own `_git`, the vcs backend `work` asks through,
        and anything else that reaches `subprocess.run`."""
        argvs, real = [], subprocess.run

        def spy(argv, *a, **kw):
            if isinstance(argv, (list, tuple)) and argv \
                    and os.path.basename(str(argv[0])) == "git":
                argvs.append([str(x) for x in argv])
            return real(argv, *a, **kw)
        return argvs, mock.patch.object(subprocess, "run", spy)

    def test_a_cherry_picked_build_lane_is_on_trunk_by_its_kept_patch_proof(self):
        """FINDING 4, RULED: `lanes_landed(content=False)` answers UNKNOWN for
        every lane this repository lands by cherry-pick (37 of the 92 live
        build lanes), because only patch identity can see a land under a new
        object id and that leg walks every trunk patch since the lane's base.
        The durable landing-proof ledger the census reads already holds that
        answer for every tip a review proved, so it is consulted before an
        UNKNOWN stands — and nothing on this path runs `git cherry`.

        THE CONTROL is a second build whose lane landed the SAME way with no
        proof kept: it stays UNKNOWN — None, never a no — carries no ALREADY
        ON TRUNK and no BEHIND, and its stall clock is left as it was."""
        build = self._row(self.c, "built-picked", "build")
        control = self._row(self.c, "picked-unproved", "build")
        tip = self._picked("built-picked")
        self._picked("picked-unproved")
        repo_id = self.get(build)["repo_id"]
        trunk = self.git("rev-parse", "HEAD")
        # THE LEDGER HOLDS WHAT A REVIEW OF THIS TIP PROVED, written through
        # its one door from a real derive against this trunk (MUST-HIT)
        self.assertEqual(landreq._landing_proof(repo_id, tip, trunk),
                         landreq.PROOF_PATCH_EQUIVALENT)
        argvs, spy = self._git_argv()
        with spy:
            b, c = self.get(build), self.get(control)
        self.assertIs(b["trunk_contains_tip"], True)
        self.assertEqual(b["trunk_contains_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertIn("ALREADY ON TRUNK", landreq._line(b))
        self.assertFalse(b["stalled"])
        self.assertIsNone(c["trunk_contains_tip"])
        self.assertIsNone(c["trunk_contains_proof"])
        self.assertTrue(c["stalled"])
        line = landreq._line(c)
        self.assertIn("AWAITING_BUILD", line)
        self.assertNotIn("ALREADY ON TRUNK", line)
        self.assertNotIn("BEHIND", line)
        self.assertGreater(len(argvs), 0, "MUST-HIT: the projection asked git")
        self.assertEqual([a for a in argvs if "cherry" in a], [],
                         "the build-lane path ran git cherry")

    def test_a_retired_cherry_picked_lane_is_answered_by_the_same_ledger(self):  # noqa: VACUOUS_ASSERTION — the None before the proof is the control for the True asserted by identity on the same row after it
        """A RETIRED lane keeps its tip under `refs/helm-retired/`, and a
        retired tip trunk holds only by patch identity is UNKNOWN to the cheap
        read exactly as a live one is."""
        from helm.work import _gc
        build = self._row(self.c, "retired-picked", "build")
        tip = self._picked("retired-picked")
        self.git("update-ref", _gc.RETIRED_NS + "lane/retired-picked", tip)
        self.git("branch", "-D", "lane/retired-picked")
        repo_id = self.get(build)["repo_id"]
        self.assertIsNone(self.get(build)["trunk_contains_tip"],
                          "THE CONTROL: before the proof is kept the retired "
                          "lane is UNKNOWN")
        self.assertEqual(landreq._landing_proof(repo_id, tip,
                                                self.git("rev-parse", "HEAD")),
                         landreq.PROOF_PATCH_EQUIVALENT)
        b = self.get(build)
        self.assertIs(b["trunk_contains_tip"], True)
        self.assertEqual(b["trunk_contains_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)

    def test_a_kept_ANCESTOR_proof_never_lands_a_lane_the_reflog_cannot_date(self):  # noqa: VACUOUS_ASSERTION — the cherry-picked control on the same projection is asserted True by identity, and the fixture's UNKNOWN lane is asserted by exact state first
        """THE LEDGER IS READ FOR PATCH IDENTITY ONLY. A lane at a trunk
        commit whose reflog is gone is UNKNOWN because landed and never
        started look the same there; a kept `ancestor` proof for that commit
        says only what the producer already measured, and reading it as a
        land is how a build sent at trunk read LANDED before anybody wrote a
        line. THE CONTROL on the same projection: the patch-identity proof of
        a cherry-picked lane IS taken."""
        build = self._row(self.b, "no-reflog", "build")
        picked = self._row(self.c, "picked-ctl", "build")
        self._lane("no-reflog", self.b)
        os.unlink(os.path.join(self.repo, ".git", "logs", "refs", "heads",
                               "lane", "no-reflog"))
        tip = self._picked("picked-ctl")
        repo_id = self.get(build)["repo_id"]
        trunk = self.git("rev-parse", "HEAD")
        self.assertEqual(landreq._landing_proof(repo_id, self.b, trunk),
                         landreq.PROOF_ANCESTOR)
        self.assertEqual(landreq._landing_proof(repo_id, tip, trunk),
                         landreq.PROOF_PATCH_EQUIVALENT)
        from helm import work
        root = os.path.dirname(repo_id.rstrip(os.sep))
        self.assertEqual(work.lanes_landed(root, ["no-reflog"], content=False)
                         ["no-reflog"]["state"], work.LANE_UNKNOWN,
                         "MUST-HIT: the fixture is the unreadable-reflog lane")
        self.assertIsNone(self.get(build)["trunk_contains_tip"],
                          "a kept ancestor proof landed a lane whose reflog "
                          "cannot say it authored anything")
        self.assertIs(self.get(picked)["trunk_contains_tip"], True)


class ProjectionScopeTest(LandReqBase):
    def test_refless_legacy_rows_are_not_land_loops(self):
        ts = "2026-07-01T00:00:00Z"
        legacy = {"id": "ce1e7dd0", "ts": ts, "recipient": "codex-3",
                  "lane": "legacy", "ref": self.a[:7], "note": None,
                  "deadline_s": 60, "source": "old", "status": "open",
                  "ack_ref": None, "verdict_ref": None, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), legacy))
        self.assertEqual(dispatches.rows()["ce1e7dd0"]["migration"],
                         "needs-redispatch")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertNotIn("ce1e7dd0", lrs)          # no exact tip => no land loop

    def test_unavailable_ledger_is_unknown_not_an_empty_board(self):
        # The dispatch fold reads its ledger as BYTES (task/2770): the failure
        # is planted at that door as well as at the row reader.
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")), \
                mock.patch.object(eventledger, "read_bytes",
                                  return_value=(None,
                                                "PermissionError: denied")):
            loops, unavailable = landreq.loops()
            self.assertIsNone(loops)
            self.assertIn("denied", unavailable)
            section = landreq.board_section()
            self.assertIn("denied", section["unavailable"])
            self.assertEqual(section["loops"], [])
            rc, _out, err = run(["list"])
            self.assertEqual(rc, 1)
            self.assertIn("UNKNOWN", err)

    def test_list_excludes_landed_by_default_all_includes_it(self):
        row = self.dispatch()
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        default, _ = landreq.loops()
        self.assertEqual(default, [])              # terminal, not in flight
        every, _ = landreq.loops(include_landed=True)
        self.assertEqual([lr["id"] for lr in every], [row["id"]])


class BoardSectionTest(LandReqBase):
    def test_board_section_exposes_loops_and_the_stalled_subset(self):
        stuck = self.dispatch(lane="lane/stuck")
        self.mark_verdict(stuck["id"], self.side, "ok", polarity="approve")
        self.age(stuck["id"], 9000)
        fresh = self.dispatch(ref=self.side, lane="lane/fresh")
        section = landreq.board_section()
        self.assertEqual(section["title"], "LAND LOOPS")
        self.assertIsNone(section["unavailable"])
        ids = {c["id"] for c in section["loops"]}
        self.assertEqual(ids, {stuck["id"], fresh["id"]})
        stalled_ids = {c["id"] for c in section["stalled"]}
        self.assertEqual(stalled_ids, {stuck["id"]})
        card = next(c for c in section["loops"] if c["id"] == stuck["id"])
        self.assertEqual(card["state"], "READY")
        self.assertEqual(len(card["review_sha"]), 12)   # clipped for the panel

    def test_board_derives_both_subsets_from_one_projection(self):
        self.dispatch()
        # The PROPERTY is one ledger read, not the NAME of the reader: the
        # topology needs rows project() drops (cancelled is transit), so both
        # subsets now come from project_raw — still ONE read. Counting the old
        # name would have gone green forever the day the mechanism moved.
        with mock.patch.object(landreq, "project_raw",
                               wraps=landreq.project_raw) as p, \
                mock.patch.object(landreq, "project",
                                  wraps=landreq.project) as legacy:
            landreq.board_section()
        self.assertEqual(p.call_count + legacy.call_count, 1)


class CmdTest(LandReqBase):
    def test_cli_list_show_stalls_round_trip(self):
        row = self.dispatch(lane="lane/round")
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], 9000)
        rc, out, err = run(["list"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("READY", out)
        self.assertIn("STALLED", out)
        rc, out, err = run(["stalls"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("workflow gap", out)
        self.assertIn(row["id"][:12], out)
        rc, out, err = run(["show", row["id"][:8]])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("LAND REQUEST", out)
        self.assertIn("timeline:", out)
        self.assertIn("reviewer codex-3", out)

    def test_stalls_derives_both_subsets_from_one_projection(self):
        self.dispatch()
        # Same property, same reason as the board pin above: ONE read, whoever
        # performs it. `stalls` derives the stalled AND unmeasurable subsets.
        with mock.patch.object(landreq, "project_raw",
                               wraps=landreq.project_raw) as p, \
                mock.patch.object(landreq, "project",
                                  wraps=landreq.project) as legacy:
            run(["stalls"])
        self.assertEqual(p.call_count + legacy.call_count, 1)

    def test_cli_empty_and_clean_states_are_honest(self):
        self.assertIn("no land loops", run(["list"])[1])
        self.assertIn("no stalled", run(["stalls"])[1])
        row = self.dispatch()                      # OPEN, young, not stalled
        self.assertIn("no stalled", run(["stalls"])[1])
        self.assertIn("OPEN", run(["list"])[1])

    def test_show_prefix_is_unique_or_refused(self):
        row = self.dispatch()
        _lr, err = landreq.get(row["id"][:10])
        self.assertIsNone(err)
        _lr, err = landreq.get("")
        self.assertIn("no such land request", err)
        rc, _out, err = run(["show", "zzzz"])
        self.assertEqual(rc, 1)
        self.assertIn("no such land request", err)

    def test_exact_short_id_does_not_outrank_a_longer_collision(self):
        short = {"id": ("de" * 20), "state": "OPEN"}
        longer = {"id": ("de" * 20) + "1" * 24, "state": "READY"}
        lrs = {short["id"]: short, longer["id"]: longer}
        with mock.patch.object(landreq, "project", return_value=(lrs, None)):
            row, err = landreq.get(("de" * 20))
            self.assertIsNone(row)
            self.assertIn("ambiguous land request id prefix", err)
            self.assertIs(landreq.get(longer["id"])[0], longer)
        with mock.patch.object(landreq, "project",
                               return_value=({short["id"]: short}, None)):
            self.assertIs(landreq.get(short["id"])[0], short)

    def test_bad_usage_is_rc2_without_traceback(self):
        for args in ([], ["bogus"], ["list", "--wat"], ["stalls", "x"],
                     ["show"]):
            rc, out, err = run(args)
            self.assertEqual(rc, 2, args)
            self.assertNotIn("Traceback", out + err)

    def test_cli_verb_is_wired(self):
        from helm import cli
        self.assertIn("lr", cli.VERBS)


class SelectiveProjectionTest(LandReqBase):
    @contextlib.contextmanager
    def projection_world(self, current, fold):
        events = {rid: [] for rid in current}
        attest = lambda rows: {row["id"]: {} for row in rows}
        with mock.patch.object(
                dispatches, "snapshot_and_events",
                return_value=(current, events, events, {}, None)), \
                mock.patch.object(dispatches, "gate_epoch", return_value=None), \
                mock.patch.object(dispatches, "attest_projections",
                                  side_effect=attest), \
                mock.patch.object(dispatches, "verdict_index",
                                  return_value=None), \
                mock.patch.object(landreq, "_receipts_by_tip",
                                  return_value={}), \
                mock.patch.object(landreq, "_consumed_confirmations",
                                  return_value={}), \
                mock.patch.object(landreq.store_load, "read_scope",
                                  side_effect=contextlib.nullcontext), \
                mock.patch.object(landreq, "_lr", side_effect=fold) as hydrate, \
                mock.patch.object(landreq, "_trunk_reach", return_value=set()), \
                mock.patch.object(landreq, "_annotate_succession"), \
                mock.patch.object(landreq, "_annotate_contrary_discharge"), \
                mock.patch.object(landreq, "_annotate_frontier_debt") as frontier:
            yield hydrate, frontier

    def raw(self, rid, parent=None, root=None, status="open"):
        return {"id": rid, "tip": self.side, "status": status,
                "supersedes": parent, "chain_root": root}

    def test_show_hydrates_only_the_selected_chain_from_the_full_snapshot(self):
        parent, cancelled, target, fork = (c * 32 for c in "abcd")
        current = {
            parent: self.raw(parent),
            cancelled: self.raw(cancelled, parent, parent, "cancelled"),
            target: self.raw(target, cancelled, parent),
            fork: self.raw(fork, parent, parent),
        }
        for i in range(1000):
            rid = "%032x" % (i + 16)
            current[rid] = self.raw(rid)
        wanted = {parent, target, fork}

        def fold(row, *_args, **_kw):
            if row["id"] not in wanted:
                raise RuntimeError("unrelated row hydrated")
            return {"id": row["id"]}

        with self.projection_world(current, fold) as (hydrate, frontier):
            row, err = landreq.get(target[:12])
            self.assertIsNone(err, err)
            self.assertEqual(row["id"], target)
            self.assertEqual({c.args[0]["id"] for c in hydrate.call_args_list},
                             wanted)
            # The cancelled middle is not hydrated, but the complete one-instant
            # snapshot still reaches topology/frontier annotation as transit.
            self.assertIs(frontier.call_args.args[1], current)
            self.assertEqual(len(current), 1004)

            # Full-board projection remains strict: the same unrelated failure
            # is still globally unavailable rather than silently omitted.
            hydrate.reset_mock()
            out, raw, unavailable = landreq.project_raw()
            self.assertEqual((out, raw), ({}, {}))
            self.assertIn("unrelated row hydrated", unavailable)

    def test_selector_ambiguity_refuses_before_any_row_is_hydrated(self):
        short = ("de" * 20)
        longer = short + "1" * 24
        current = {short: self.raw(short), longer: self.raw(longer)}
        with self.projection_world(
                current, lambda row, *_a, **_k: {"id": row["id"]}) \
                as (hydrate, _frontier):
            row, err = landreq.get(short)
        self.assertIsNone(row)
        self.assertIn("ambiguous land request id prefix", err)
        self.assertEqual(hydrate.call_count, 0)

    def test_close_routes_the_requested_id_into_selective_projection(self):
        rid = "a" * 32
        row = {"id": rid}
        with mock.patch.object(landreq, "project",
                               return_value=({rid: row}, None)) as projection, \
                mock.patch.object(landreq, "_close_ladder_withdrawn",
                                  return_value=({"would_close": rid}, None)):
            result, err = landreq.close(rid, "withdrawn", dry_run=True)
        self.assertIsNone(err, err)
        self.assertEqual(result["would_close"], rid)
        projection.assert_called_once_with(selector=rid)


class PolarityTest(LandReqBase):
    """A verdict's DIRECTION, which `status == "verdict"` could never carry.

    The bug these pin, live: five loops sat READY for over a day
    carrying SUPERSEDED, SUPERSEDED, "FIX (3 blockers)", "helm#210 FIX" and
    "helm#217 FIX" — not one an approval — and `stalls` billed all five as
    land-side workflow gaps, because READY was derived from a review having
    HAPPENED rather than from its having said YES.
    """

    def _legacy_undeclared(self, row, evidence="ok"):
        """Plant history the current writer is now required to refuse."""
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": row["tip"], "verdict_ref": evidence}))

    def test_a_fix_verdict_is_changes_requested_and_owed_by_the_author(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "3 blockers",
                                polarity="fix")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertEqual(lr["owed_by"], "author")
        self.assertFalse(lr["terminal"])       # the author still owes a re-dispatch

    def test_a_fix_verdict_never_reads_as_a_land_loop(self):
        """The precise inversion of the live bug: a rejection must not park in a
        land-side state where the LANDER is nagged for someone else's work."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.age(row["id"], 10 ** 6)
        lr = landreq.get(row["id"])[0]
        self.assertNotIn(lr["state"], ("READY", "MERGED_LOCAL", "LANDED"))
        self.assertNotEqual(lr["owed_by"], "lander")

    def test_a_supersede_verdict_is_terminal_and_never_stalls(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "replaced by e1cf126",
                                polarity="supersede")
        self.age(row["id"], 10 ** 6)           # ancient, and must still be quiet
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(landreq.stalls()[0], [])

    def test_an_undeclared_verdict_is_reviewed_and_carries_no_threshold(self):
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertIsNone(lr["stall_threshold_s"])
        self.assertFalse(lr["stalled"])

    def test_an_unknown_polarity_fails_closed_to_reviewed_never_ready(self):
        """THE MUTATION KILL. Replay is not a trust boundary: a hand-edited or
        future-versioned ledger row carrying an unrecognised polarity must read
        UNDECLARED, never as an approval. If `_replay_polarity` ever passed the
        raw value through, or `VERDICT_STATE.get` ever defaulted to READY, this
        row would arm the land clock on a verdict nobody can interpret."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("event") == "verdict":
                    ev["polarity"] = "definitely-approved-trust-me"
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])

    def test_write_time_refuses_an_unknown_polarity_outright(self):
        row = self.dispatch(ref=self.side)
        out, why = self.mark_verdict(row["id"], self.side, "ok",
                                           polarity="lgtm")
        self.assertIsNone(out)
        self.assertIn("polarity must be one of", why)
        # and the refusal is TOTAL — no half-written verdict behind it
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")

    def test_polarity_cannot_be_flipped_on_a_standing_verdict(self):
        """Terminal is immutable, and that includes a verdict's direction: the
        same tip and evidence with a DIFFERENT polarity is not an idempotent
        retry, it is an attempted rewrite of what the reviewer said."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "same", polarity="fix")
        out, why = self.mark_verdict(row["id"], self.side, "same",
                                           polarity="approve")
        self.assertIsNone(out)
        self.assertIn("already has a verdict", why)
        self.assertEqual(landreq.get(row["id"])[0]["state"],
                         "CHANGES_REQUESTED")

    def test_an_identical_verdict_including_polarity_is_still_idempotent(self):
        row = self.dispatch(ref=self.side)
        first, why = self.mark_verdict(row["id"], self.side, "same",
                                            polarity="approve")
        self.assertIsNone(why)
        again, why = self.mark_verdict(row["id"], self.side, "same",
                                            polarity="approve")
        self.assertIsNone(why)
        self.assertEqual(again["polarity"], first["polarity"])

    def test_an_undeclared_verdict_still_reports_trunk_FACTS(self):
        """Undeclared kills the land CLOCK, not git OBSERVATION. This is what
        preserved the 8 genuinely-true MERGED_LOCAL alarms when the polarity fix
        went in: trunk membership is a fact about the repo, not a reading of the
        verdict, so an undeclared row whose tip demonstrably reached trunk is
        honestly LANDED rather than hidden behind the refusal."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])

    def test_a_fix_verdict_still_observes_git_facts(self):
        """A FIX governs intent, not physical history. The verdict state remains
        CHANGES_REQUESTED while the separate Git facts expose contrary inclusion
        instead of suppressing or laundering it."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertTrue(lr["observable"] and lr["landed"])
        self.assertTrue(lr["contrary"])
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIn("CONTRARY", run(["list"])[1])
        self.assertIn("contrary", run(["show", row["id"]])[1])
        card = landreq.board_section()["loops"][0]
        self.assertTrue(card["contrary"] and card["landed"])
        self.assertEqual(card["owed_by"], "integrator")

    def test_a_contrary_local_merge_names_local_not_upstream_trunk(self):
        self.add_origin()
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertTrue(lr["contrary"] and lr["merged_local"])
        self.assertFalse(lr["landed"])
        shown = run(["show", row["id"]])[1]
        self.assertIn("MERGED_LOCAL on local trunk", shown)
        self.assertNotIn("MERGED_LOCAL on upstream trunk", shown)

    def test_a_superseded_tip_on_trunk_stays_visible_as_contrary(self):
        row = self.dispatch(ref=self.side, lane="lane/contrary")
        self.mark_verdict(row["id"], self.side, "replaced",
                                polarity="supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "SUPERSEDED")
        self.assertTrue(lr["contrary"] and lr["landed"])
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "integrator")
        self.assertIn(row["id"], [x["id"] for x in landreq.loops()[0]])
        self.assertIn("CONTRARY", run(["list"])[1])
        self.assertIn("owed by integrator", run(["show", row["id"]])[1])

    def test_stalls_reports_undeclared_rows_instead_of_dropping_them(self):
        """The HONEST REFUSAL. A loop excluded from stall accounting and also
        absent from the output reads as healthy — that silent-absence is how 4
        live loops stayed invisible for three days. Exclusion must be STATED."""
        row = self.dispatch(ref=self.side, lane="lane/undeclared")
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        rows, err = landreq.unmeasurable()
        self.assertIsNone(err)
        self.assertEqual([lr["id"] for lr, _why in rows], [row["id"]])
        self.assertEqual(landreq.stalls()[0], [])
        _rc, out, _err = run(["stalls"])
        self.assertIn("NOT stall-checked", out)
        # #142: the writer strips the lane/ prefix, so the surfaced row names
        # the stored bare spelling.
        self.assertIn("undeclared", out)

    def test_a_held_approve_is_not_reported_as_undeclared_polarity(self):
        """Second site of the `lr show` polarity defect, and the one that
        reached the OWNER CONSOLE. `_unmeasurable_rows` labelled EVERY
        non-terminal REVIEWED row "polarity UNDECLARED" with no check on
        whether a polarity existed. Measured on the live ledger: 14 of 37
        REVIEWED rows carry polarity `approve` — held by the approval tier or
        a missing receipt, not by any absence of a verdict — and the console
        told the owner all 37 were undeclared.

        Sited beside test_stalls_reports_undeclared_rows_instead_of_dropping_them,
        which builds a genuinely polarity-less row and still expects UNDECLARED.
        The pair is the point: this asserts the label is CORRECTED when a
        polarity exists, that one asserts it still FIRES when none does. Deleting
        the branch outright goes green here and red there.

        The row stays in the unmeasurable bucket on purpose — whether a held
        APPROVE should become stall-billable is a real question and a separate
        change. This fixes the false REASON, nothing else."""
        row = self.dispatch(ref=self.side, lane="lane/held-approve")
        # Approval-tier demotion, not a missing receipt — see the sibling test
        # in EraStabilityTest for why: an ungated approve is no longer
        # constructible, mark_verdict refuses it before any append.
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                return_value=("outside", "reviewer outside the permitted tier "
                              "(test)")):
            self.mark_verdict(row["id"], self.side, "ok",
                                    polarity="approve")
            self.age(row["id"], 10 ** 6)
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "REVIEWED")     # the shape at issue
            self.assertEqual(lr["polarity"], "approve")   # and it IS declared
            rows, err = landreq.unmeasurable()
            self.assertIsNone(err)
            why = dict((x["id"], w) for x, w in rows)[row["id"]]
            # Assert the EFFECT: the real polarity reaches the surface.
            self.assertIn("APPROVE", why)
            self.assertNotIn("UNDECLARED", why)

    def test_the_all_clear_line_never_hides_an_undeclared_row(self):
        """Lie by omission: with zero stalls and one unclassifiable loop, "every
        loop is inside its threshold" must not be the whole message."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        self.age(row["id"], 10 ** 6)
        _rc, out, _err = run(["stalls"])
        self.assertIn("inside its threshold", out)      # the all-clear fires...
        self.assertIn("NOT stall-checked", out)         # ...and does not stand alone

    def test_stalls_offers_no_remedy_the_ledger_would_refuse(self):
        """A remedy that cannot be followed is worse than none: a verdict is
        immutable, so `dispatch verdict` REFUSES an already-verdict'd row, and
        telling an operator to re-record one would fail the moment they tried."""
        row = self.dispatch(ref=self.side)
        self._legacy_undeclared(row)
        _rc, out, _err = run(["stalls"])
        self.assertIn("immutable", out)
        # and the ledger really does refuse, so the text is not merely cautious
        _out, why = self.mark_verdict(row["id"], self.side, "ok",
                                            polarity="approve")
        self.assertIn("already has a verdict", why)

    def test_the_timeline_records_the_verdicts_own_direction(self):
        """The history must not retell the lie the state machine just stopped
        telling: a FIX verdict's timeline entry is CHANGES_REQUESTED, not READY."""
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        states = [s["state"] for s in landreq.get(row["id"])[0]["timeline"]]
        self.assertEqual(states, ["OPEN", "AWAITING_REVIEW",
                                  "CHANGES_REQUESTED"])
        self.assertNotIn("READY", states)

    def test_every_state_names_who_owes_the_next_move(self):
        """A stall that cannot say whose turn it is cannot drive a poke, which is
        the whole point of open owner-ask 21350297."""
        for state in landreq.STAGE_ORDER:
            self.assertIn(state, landreq.OWED_BY, state)
        self.assertEqual(landreq.OWED_BY["CHANGES_REQUESTED"], "author")
        self.assertEqual(landreq.OWED_BY["READY"], "lander")
        self.assertEqual(landreq.OWED_BY["AWAITING_REVIEW"], "reviewer")

    def test_declared_polarity_survives_a_fresh_disk_replay(self):
        """It is ledger EVIDENCE, not a live-object field: a cold reader must
        reach the same verdict direction the writer recorded."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="fix")
        current, unavailable = dispatches.snapshot()   # re-reads from disk
        self.assertIsNone(unavailable)
        self.assertEqual(current[row["id"]]["polarity"], "fix")

    def test_cli_declares_polarity_by_flag_not_by_positional(self):
        """Evidence is a free-text tail, so polarity is a REQUIRED flag.
        Omitting it refuses before the immutable ledger gains a verdict."""
        from helm import dispatches as d
        row = self.dispatch(ref=self.side)
        rc = d.cmd_dispatch(
            ["verdict", row["id"], self.side, "--measured",
             "looks", "good"])
        self.assertEqual(rc, 2)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")
        with self.verdict_author():
            rc = d.cmd_dispatch(["verdict", row["id"], self.side,
                                 "--approve", "--measured", "looks", "good"])
        self.assertEqual(rc, 0)
        self.assertEqual(landreq.get(row["id"])[0]["polarity"], "approve")
        self.assertEqual(landreq.get(row["id"])[0]["verdict_ref"], "looks good")

    def test_cli_refuses_two_polarities_and_an_unknown_flag(self):
        from helm import dispatches as d
        row = self.dispatch(ref=self.side)
        for flags in (["--approve", "--fix"], ["--maybe"]):
            rc = d.cmd_dispatch(["verdict", row["id"], self.side, *flags,
                                 "--measured", "e"])
            self.assertEqual(rc, 2, flags)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "OPEN")


class WithdrawTerminalTest(LandReqBase):
    """A do-not-land FIX verdict gets a terminal state when Git proves the
    reviewed change is provably ABSENT from trunk — the mirror of discharge,
    for the row no superseding tip will ever exist to close."""

    def fix_row(self, polarity="fix", lane="lane/evals-0723", key="key-evals"):
        """A verdict on the divergent side tip, which is NOT on trunk —
        exactly the do-not-land shape withdraw retires.

        `lane`/`key` are parameters because a test that needs a SECOND row —
        a control at a different polarity, say — cannot mint one from the
        same pair: `dispatches.send` refuses the repeat as an
        already-recorded dispatch, which surfaces as an unrelated-looking
        assertion failure about delivery."""
        row, why, sent = dispatches.send(
            "seat-a", lane, "review evals-0723",
            self.side, repo=self.repo, key=key, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.mark_verdict(row["id"], self.side, "findings: keep untracked",
                                polarity=polarity)
        return row

    def test_withdraw_retires_the_debt_and_preserves_the_verdict(self):
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"][:12], "kept untracked as instructed")
        self.assertIsNone(why)
        # the FIX verdict is preserved, the debt is retired (via lr close)
        self.assertEqual(lr["state"], "CHANGES_REQUESTED")
        self.assertEqual(lr["polarity"], "fix")
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertTrue(lr["terminal"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertEqual(lr["close_evidence"], "kept untracked as instructed")
        # and it is OFF the operational board but still in --all
        self.assertNotIn(row["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(row["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertEqual(landreq.stalls()[0], [])
        # dwell is frozen at the withdraw, not still accruing
        frozen = lr["dwell_s"]
        self.assertEqual(landreq.get(row["id"], time.time() + 10000)[0]["dwell_s"],
                         frozen)
        # the compact board row and the show page both name the resolution
        line = landreq._line(lr)
        self.assertIn("WITHDRAWN", line)
        shown = landreq._render_show(lr)
        self.assertIn("CLOSED (WITHDRAWN)", shown)
        self.assertIn("withdrawn absent at", shown)

    def test_a_landed_reviewed_tip_cannot_be_withdrawn(self):
        """THE GATE, mutation-pinned: withdraw is only truthful when the change
        is ABSENT. A reviewed tip that IS on trunk makes withdraw a lie, and
        the refusal offers the door for work on trunk by verdict instead."""
        row = self.fix_row()
        self.git("merge", "--no-edit", "-q", "side")   # land the side tip
        lr, why = landreq.withdraw(row["id"], "claim it is not landed")
        self.assertIsNone(lr)
        self.assertIn("on trunk", why)
        # and the row is NOT withdrawn
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unknown_land_state_fails_closed(self):
        """If Git cannot prove absence (_landed None), withdraw refuses rather
        than guess — fail-closed, exactly as discharge is."""
        row = self.fix_row()
        # THE DOUBLE GOES ON THE FUNCTION THIS DOOR ACTUALLY CALLS. The proof
        # migrated from the lossy `_landed` wrapper to the typed
        # `_landing_proof`, and a double left on the old name is simply never
        # consulted — the real proof runs, the fixture's tip reads absent, and
        # the withdraw SUCCEEDS while the arm believes it is testing a refusal.
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            lr, why = landreq.withdraw(row["id"], "evidence")
        self.assertIsNone(lr)
        self.assertIn("could not prove", why)
        self.assertIn("FAIL-CLOSED", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_APPROVE_reaches_the_landing_proof_rather_than_a_polarity_wall(self):
        """Withdraw asks whether the WORK will land, not what the verdict said.
        An APPROVE whose lane conflicts with trunk is exactly as stuck as a
        FIX, and the answer must come from Git rather than from the sign."""
        # UNCONDITIONAL POSITIVE CONTROL ON THE SAME DOOR, and it is what
        # makes the absences below mean anything: an UNDECLARED row still
        # hits the polarity wall and says so. Without it, an empty `why` — or
        # a door that stopped refusing altogether — passes this arm.
        # THE POSITIVE FORM, because that is what the widening MEANS: this
        # fixture's side tip is NOT on trunk, so an APPROVE whose work is
        # provably absent now reaches the landing proof and RETIRES. Asserting
        # a refusal here would be asserting the law this lane replaced.
        row = self.fix_row(polarity="approve")
        lr, why = landreq.withdraw(row["id"], "evidence")
        self.assertIsNone(why, "an APPROVE with provably absent work was "
                               "refused: %s" % why)
        self.assertIsNotNone(lr)
        self.assertEqual("withdrawn", landreq.get(row["id"])[0]["close_reason"])

        # AND THE INVARIANT THE WIDENING MAY NOT TOUCH, driven on the same
        # door: once the SAME work is on trunk, the LANDING proof refuses the
        # same polarity. That is the discriminator — polarity stopped
        # deciding, landedness still does.
        landed = self.fix_row(polarity="approve", lane="lane/evals-0723-on",
                              key="key-evals-on")
        self.git("merge", "--no-edit", "-q", "side")
        lr2, why2 = landreq.withdraw(landed["id"], "evidence")
        self.assertIsNone(lr2, "a LANDED approve was withdrawn")
        self.assertIn("on trunk", why2)
        self.assertNotIn("withdrawable", why2,
                         "refused on POLARITY, so it never reached the proof")

    def test_an_UNDECLARED_polarity_is_still_refused(self):
        """The one case the widening deliberately leaves alone: no declared
        verdict anywhere, so there is no judgement to retire. Its negative
        control is `test_a_silent_chain_still_refuses_the_gate_did_not_loosen`,
        which is untouched."""
        row = self.fix_row(polarity=None)
        lr, why = landreq.withdraw(row["id"], "evidence")
        self.assertIsNone(lr)
        self.assertIn("withdrawable", why)

    def test_withdraw_needs_evidence(self):
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "")
        self.assertIsNone(lr)
        self.assertIn("evidence", why)

    def test_identical_retry_is_idempotent_and_a_conflict_is_refused(self):
        row = self.fix_row()
        lr1, why1 = landreq.withdraw(row["id"], "resolution A")
        self.assertIsNone(why1)
        lr2, why2 = landreq.withdraw(row["id"], "resolution A")
        self.assertIsNone(why2)                     # identical retry: OK
        self.assertEqual(lr2["close_reason"], "withdrawn")
        lr3, why3 = landreq.withdraw(row["id"], "resolution B")
        self.assertIsNone(lr3)                      # conflicting: refused
        self.assertIn("retired once", why3)

    def test_a_withdrawn_row_accepts_no_discharge_and_vice_versa(self):
        """A debt is retired once. withdraw-then-discharge and discharge-then-
        withdraw are both refused at the ledger boundary."""
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "withdrawn first")
        self.assertIsNone(why)
        fixed = self.commit("a later superseding tip", path="h")
        out, err = dispatches._record_discharge_proven(
            row["id"], self.side, fixed, row["id"], "superseded",
            "landed", "local")
        self.assertIsNone(out)
        self.assertIn("retired once", err)

    def test_a_later_land_re_exposes_a_withdrawn_row_as_contrary(self):
        """LIFECYCLE-WALK, and the trap named in the lane brief. A withdraw is
        truthful when recorded (Git proved absence then) but its premise is
        falsifiable: if the withdrawn change LATER lands, the row must not stay
        silently terminal — the live landed-despite-FIX fact re-exposes it as
        CONTRARY, owed by the integrator again. A withdraw is a resolution, not
        a veto on the future."""
        row = self.fix_row()
        lr, why = landreq.withdraw(row["id"], "absent at withdraw time")
        self.assertIsNone(why)
        self.assertTrue(lr["terminal"] and not lr["stalled"])
        # the change LANDS LATER — the withdraw's premise is now false
        self.git("merge", "--no-edit", "-q", "side")
        reland = landreq.get(row["id"])[0]
        self.assertTrue(reland["contrary"], "a later land must re-expose the row")
        self.assertFalse(reland["terminal"])
        self.assertEqual(reland["owed_by"], "integrator")
        # the close event stays as history even though the row is contrary
        self.assertEqual(reland["close_reason"], "withdrawn")
        self.assertTrue(reland["close_contradicted"])

    def test_the_owner_console_card_carries_the_withdraw_state(self):
        """OWNER SURFACE — the P2 from cross-family review. The operational
        effect (retire off the stalled list) was right, but board_section's card
        dropped the `withdrawn` key, so the owner saw only a row that stopped
        being red with no way to distinguish CORRECTLY-NEVER-LANDED from
        SUPERSEDED-BY-A-LATER-LAND — the very distinction the feature exists
        for. The card must carry it, and a contradicted row must say why it came
        back."""
        row = self.fix_row()
        landreq.withdraw(row["id"], "kept untracked")
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        # a withdrawn row is terminal, so it is NOT in the default loops — but
        # the key must survive on the row wherever it renders. Assert the card
        # SHAPE carries the key for a row that IS present (a contradicted one,
        # which un-retires back onto the board).
        self.git("merge", "--no-edit", "-q", "side")   # later land -> contradicted
        cards = {c["id"]: c for c in landreq.board_section()["loops"]}
        self.assertIn(row["id"], cards, "a contradicted row re-appears on the board")
        card = cards[row["id"]]
        self.assertEqual(card["close_reason"], "withdrawn")
        self.assertIn("close_contradicted", card)
        self.assertTrue(card["close_contradicted"])
        self.assertEqual(card["owed_by"], "integrator")


class UnobservedLandedLineTest(LandReqBase):
    """THE LINE THAT NAMED A CAUSE ITS OWN ROW REFUTES.

    `_render_show` had ONE sentence for every row whose git leg produced no
    answer: "UNOBSERVABLE — no repo binding or trunk to watch". That is two
    claims, and on a live READY row BOTH were false — its repo_id was present,
    `_observation_owned` was True, and `has_upstream` was True, which is only
    set from a RESOLVED upstream ref inside `_git_observe`. The real cause was
    a capped patch-index scan missing, which `_landing_proof` honestly answers
    `unknown`, `landed_ever` turns into None, and `_git_observe` converts into
    a blind return.

    This is the same defect a 2026-08-05 audit named in this file's own
    comment — "it named a cause its own report refutes, and sent readers
    hunting a binding that was present and correct" — cured then for rows
    carrying a close_reason and left standing for LIVE ones, where it cost
    three successive wrong diagnoses before anyone read the emitting branch.

    THE PAIR IS THE MEASUREMENT. Both rows are unobservable and neither is
    closed, so the OLD code gives them one identical sentence and cannot tell
    them apart. They differ in exactly one bit — whether git got far enough to
    resolve a trunk ref — and that bit is the whole difference between "asked
    and could not answer" and "never asked".
    """

    def _projected(self):
        """ONE REAL projected row, rendered twice by the arm below.

        Building the dict by hand would let the fixture invent a shape the
        projection never emits, which is how a render arm comes to test its
        author's model instead of the code. And it must be ONE row, not two:
        a second `dispatch()` on the same lane is correctly refused as a
        duplicate successor, which is a true refusal that says nothing about
        rendering.
        """
        row = self.dispatch()
        self.mark_verdict(row["id"], self.side, "content clean",
                                polarity="approve")
        lr, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        self.assertIsNotNone(lr)
        # THE BRANCH UNDER TEST IS GATED ON READY, so a row that never reached
        # it would render no `landed` line at all and the arm would compare two
        # identical pages for a reason that has nothing to do with the cure.
        # Measured the first time round: an OPEN row produced byte-identical
        # output and the assertion fired on a page with no `landed` in it.
        self.assertEqual(lr["state"], "READY", lr["state"])
        return lr

    def _shown(self, lr, **over):
        return landreq._render_show(
            dict(lr, observable=False, close_reason=None, **over))

    def test_asked_and_unanswerable_is_not_reported_as_never_asked(self):
        lr = self._projected()
        asked = self._shown(lr, has_upstream=True)
        unasked = self._shown(lr, has_upstream=False)
        self.assertNotEqual(
            asked, unasked,
            "one bit apart and the render is byte-identical — the two states "
            "still share a sentence and no reader can tell them apart")
        self.assertIn("NOT PROVEN", asked)
        self.assertIn("repo binding above is fine", asked,
                      "the row proves its binding is present; the line must "
                      "stop accusing it")
        self.assertNotIn("no repo binding", asked)
        self.assertIn("NOT OBSERVED", unasked)
        self.assertIn("no repo binding", unasked,
                      "control: when git really was not asked, saying so is "
                      "correct and must survive this cure")
        for shown in (asked, unasked):
            self.assertIn("never inferred landed", shown.lower(),
                          "neither state may be read as a landing verdict")


class RetiredHeadlineTest(LandReqBase):
    """THE LYING INSTRUMENT, measured on the live ledger: 61 of 718
    rows were `terminal`, `owed_by: nobody`, carrying a real retirement stamp
    and a reason — and `helm lr show` headlined every one of them a bare
    CHANGES_REQUESTED, because the retirement headline keyed on `close_reason`
    and the `withdraw`/`discharge` events do not write one. Specimen
    251d3c1a89bb (lane triage-committer-signal, withdrawn)
    read as its author's open debt at the top of its own page. The same count
    was the `--all` header's: `718 land loops in flight` over 653 retirements.
    """

    def fix_row(self, lane="lane/retired-headline", key="key-retired",
                polarity="fix"):
        row, why, sent = dispatches.send(
            "opus-integrator", lane, "review " + lane, self.side,
            repo=self.repo, key=key, sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.mark_verdict(row["id"], self.side, "findings: do not land",
                                polarity=polarity)
        return row

    def withdrawn_row(self, ref="superseded in-lane; never lands"):
        """A row retired by the `withdraw` EVENT — `withdrawn: True` with no
        `close_reason`, the exact shape all 15 live withdrawn specimens carry
        and the shape `_record_withdraw_proven` still writes today."""
        row = self.fix_row()
        out, err = dispatches._record_withdraw_proven(row["id"], self.side, ref)
        self.assertIsNone(err)
        self.assertTrue(out["withdrawn"])
        self.assertIsNone(out.get("close_reason"))
        return landreq.get(row["id"])[0]

    def test_a_withdrawn_row_headlines_as_retired_and_names_its_reason(self):
        lr = self.withdrawn_row("superseded in-lane by a539c3e67434 on trunk")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        shown = landreq._render_show(lr)
        headline = shown.splitlines()[0]
        self.assertIn("RETIRED (WITHDRAWN)", headline)
        # the verdict stays in the headline — the retirement names how the debt
        # ended, it does not overwrite what the reviewer said
        self.assertIn("CHANGES_REQUESTED", headline)
        # and the page carries WHY, verbatim, so a reader outside our history
        # can see what retired it
        self.assertIn("superseded in-lane by a539c3e67434 on trunk", shown)

    def test_a_withdrawn_row_is_never_headlined_as_landed(self):
        """DISCRIMINATION, the other half of the fix. A withdraw asserts the
        reviewed change is correctly ABSENT from trunk; reading it as a landing
        would trade one lie for another. It also holds no close event, so the
        `CLOSED (...)` spelling would assert a transition the ledger lacks."""
        headline = landreq._render_show(self.withdrawn_row()).splitlines()[0]
        # the positive control on the SAME observable: an empty headline would
        # satisfy both absences below and prove nothing
        self.assertIn("RETIRED (WITHDRAWN)", headline)
        self.assertNotIn("LANDED", headline)
        self.assertNotIn("CLOSED", headline)

    def test_a_discharged_row_headlines_as_retired_and_names_its_reason(self):
        """THE MAJORITY LEG, and a mutation this suite first let survive: 46 of
        the 61 lying rows were retired by `discharge`, not `withdraw` — the
        same `close_reason`-is-None shape, the same bare CHANGES_REQUESTED
        headline over a debt a later round already paid. Specimen 631d2220b189
        (lane pi-start-resume)."""
        row = self.fix_row(lane="lane/discharged-headline", key="key-disc")
        self.git("merge", "--no-edit", "-q", "side")   # the contrary land
        fixed = self.commit("the superseding round", path="h")
        out, err = dispatches._record_discharge_proven(
            row["id"], self.side, fixed, row["id"],
            "resolved by the r2 round", "landed", "local")
        self.assertIsNone(err, err)
        self.assertTrue(out["discharged"])
        self.assertIsNone(out.get("close_reason"))
        lr = landreq.get(row["id"])[0]
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        shown = landreq._render_show(lr)
        headline = shown.splitlines()[0]
        self.assertIn("RETIRED (DISCHARGED)", headline)
        self.assertIn("CHANGES_REQUESTED", headline)   # the verdict survives
        self.assertIn("resolved by the r2 round", shown)   # and WHY

    def test_a_landed_row_still_headlines_as_landed(self):
        """The discrimination control: the fix must not blur the retirements
        that already rendered honestly."""
        row = self.fix_row(lane="lane/landed-control", key="key-landed",
                           polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        sha = self.git("rev-parse", self.main)
        out, err = dispatches._record_close_proven(
            row["id"], "landed", self.side, evidence="landed on trunk",
            closing_repo_id=self.repo, closing_trunk_ref="refs/heads/" + self.main,
            closing_trunk_sha=sha, proof_mode="ancestor")
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")
        headline = landreq._render_show(
            landreq.get(row["id"])[0]).splitlines()[0]
        self.assertIn("CLOSED (LANDED)", headline)
        self.assertNotIn("RETIRED", headline)

    def test_a_live_changes_requested_row_headline_is_unchanged(self):
        """THE NEGATIVE CONTROL — the assertion that proves this did not just
        relabel every CHANGES_REQUESTED row. An un-retired FIX is real, live,
        author-owed debt and must keep reading exactly that way."""
        row = self.fix_row(lane="lane/live-debt", key="key-live")
        lr = landreq.get(row["id"])[0]
        self.assertFalse(lr["terminal"])
        self.assertEqual(lr["owed_by"], "author")
        headline = landreq._render_show(lr).splitlines()[0]
        self.assertIn("CHANGES_REQUESTED", headline)
        self.assertNotIn("RETIRED", headline)

    def test_a_contradicted_withdraw_loses_the_retired_headline(self):
        """The falsifiability clause, carried into the headline. A withdraw is
        truthful when recorded, not forever: once the withdrawn change lands,
        the row is live debt owed by the integrator again, and a headline still
        saying RETIRED would be the same lie pointed the other way."""
        lr = self.withdrawn_row()
        self.assertIn("RETIRED (WITHDRAWN)",
                      landreq._render_show(lr).splitlines()[0])
        self.git("merge", "--no-edit", "-q", "side")   # the premise falsified
        reland = landreq.get(lr["id"])[0]
        self.assertTrue(reland["withdraw_contradicted"])
        self.assertFalse(reland["terminal"])
        self.assertEqual(reland["owed_by"], "integrator")
        self.assertNotIn("RETIRED", landreq._render_show(reland).splitlines()[0])

    def test_the_all_header_does_not_count_retirements_as_in_flight(self):
        """THE SUMMARY LINE. `--all` carries the retirements, so counting the
        whole listing as `in flight` billed the fleet for debt nobody owed."""
        self.withdrawn_row()
        live = self.fix_row(lane="lane/still-owed", key="key-still-owed")
        rc, out, err = run(["list", "--all"])
        self.assertEqual(rc, 0, err)
        header = out.splitlines()[0]
        self.assertIn("1 RETIRED", header)
        self.assertIn("1 in flight", header)
        self.assertNotIn("2 land loops in flight", header)
        # and the DEFAULT listing, which filters terminals out, is untouched
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # startswith, not equality: trunk's header also carries a polarity
        # provenance suffix, and pinning the WHOLE line makes this test fail
        # for a reason that has nothing to do with retirement counting (it
        # did, on the rebase that merged the two headers). The claim under
        # test is the COUNT and the words "in flight", so assert those.
        default_header = out.splitlines()[0]
        self.assertTrue(
            default_header.startswith("helm lr — 1 land loop in flight"),
            default_header)
        self.assertNotIn("RETIRED", default_header)
        self.assertIn(live["id"][:12], out)


class ChainPolarityCloseTest(LandReqBase):
    """The close ladders' polarity gates read the CHAIN's declared polarity
    when the row's own field is silent — the bug class is a decision recorded
    in PROSE that the machine-readable gate cannot see. Live fixture:
    be5e82bbe0b5 sat REVIEWED 8d08h with verdict text "SUPERSEDED — guard not
    landing", polarity UNDECLARED, and every terminal door refusing for a
    different correct reason, while chained round b5c7a8df67df held an
    explicit SUPERSEDE about the same code.

    THE NEGATIVE CONTROLS ARE THE POINT: a chain-aware door must not become a
    door any chained row can open. Boundary under test (`_chain_polarity`):
    the chain speaks only when the row is silent; only a descendant verdict
    about the SAME reviewed code counts; conflict refuses and names the rows;
    every unreadable leg is UNKNOWN in words, never a close."""

    def undeclared_row(self, tip=None, lane="lane/silent"):
        """A verdict whose decision lives in PROSE only — polarity None. The
        current writer refuses this shape, which is exactly why the rows
        exist only as history: plant the legacy event verbatim."""
        tip = tip or self.side
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": tip,
            "verdict_ref": "SUPERSEDED in prose — not landing"}))
        return row

    def chained(self, parent, tip, polarity, lane="lane/silent-r2"):
        """A round dispatched --supersedes parent, reviewed at `tip`."""
        kid = self.dispatch(ref=tip, lane=lane, kind="review",
                            supersedes=parent["id"])
        if polarity:
            _out, why = self.mark_verdict(
                kid["id"], tip, "chain declares %s" % polarity,
                polarity=polarity)
            self.assertIsNone(why)
        else:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": kid["seq"] + 1,
                "id": kid["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": tip,
                "verdict_ref": "round happened, silent"}))
        return kid

    def sidecar(self, *pairs):
        state = os.path.join(landreq.home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "ref-migrations.jsonl"), "a",
                  encoding="utf-8") as f:
            for old, new in pairs:
                f.write(json.dumps({"old": old, "new": new}) + "\n")

    def test_chain_declared_supersede_withdraws_an_undeclared_row(self):
        """THE ACCEPTANCE SHAPE (be5e82bbe0b5): undeclared row, chained round
        declares SUPERSEDE at the same reviewed tip, work absent from trunk —
        the withdrawn door closes on the absence proof, dry-run first."""
        row = self.undeclared_row()
        kid = self.chained(row, self.side, "supersede")
        plan, why = landreq.close(row["id"], "withdrawn",
                                  evidence="owner declined in the verdict",
                                  dry_run=True)
        self.assertIsNone(why)
        self.assertEqual(plan["reason"], "withdrawn")
        self.assertEqual(plan["proof_mode"], "absent")
        self.assertEqual(plan["polarity"], "supersede")
        self.assertEqual(plan["polarity_via_chain"], kid["id"][:12])
        # dry-run appended nothing
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        lr, why = landreq.close(row["id"], "withdrawn",
                                evidence="owner declined in the verdict")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertIsNone(lr["polarity"], "the immutable verdict is untouched")
        # and it survives a fresh disk replay — the recorder admitted it
        self.assertEqual(landreq.get(row["id"])[0]["close_reason"], "withdrawn")

    def test_a_silent_chain_still_refuses_the_gate_did_not_loosen(self):
        """NEGATIVE CONTROL: no declared polarity anywhere — the undeclared
        row refuses exactly as before, in words that say the chain was read."""
        row = self.undeclared_row()
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_undeclared_chained_round_asserts_nothing(self):
        """NEGATIVE CONTROL: a chained child whose own polarity is UNDECLARED
        contributes no polarity — prose on the child is still prose."""
        row = self.undeclared_row()
        self.chained(row, self.side, None)
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_declaration_about_different_code_does_not_count(self):
        """NEGATIVE CONTROL — the door-any-chained-row-can-open shape: a
        chained SUPERSEDE at a DIFFERENT reviewed tip is a ruling about
        different code and must not open this row's withdrawn door."""
        row = self.undeclared_row()
        self.git("checkout", "-q", "side")
        other = self.commit("a different change entirely", path="g")
        self.git("checkout", "-q", self.main)
        self.chained(row, other, "supersede")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("no chained round declares", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_conflicting_chain_refuses_and_names_the_rows(self):
        """NEGATIVE CONTROL: an APPROVE and a SUPERSEDE about the same code
        on different children is a chain that disagrees with itself — refuse,
        and say WHICH rows conflict."""
        row = self.undeclared_row()
        yes = self.chained(row, self.side, "approve", lane="lane/silent-r2")
        no = self.chained(row, self.side, "supersede", lane="lane/silent-r3")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("CONFLICTING", why)
        self.assertIn(yes["id"][:12], why)
        self.assertIn(no["id"][:12], why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        # the conflict blocks the landed door identically — neither polarity
        # may be assumed for ANY gate on an ambiguous chain
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(lr)
        self.assertIn("CONFLICTING", why)

    def test_a_fold_that_CANNOT_witness_says_so_and_still_lands(self):
        """FAIL-OPEN MUST NOT MEAN SILENT.

        Receipting is fail-open by law and that is right — a land must never
        be blocked because a signer is unavailable. But fail-open was ALSO
        silent, and that is how 56 folds produced one receipt
        while the owner's LANDED card, fed only by the receipt index, sat
        frozen since 00:11Z. No predicate rejected those lands. There was no
        row, and nothing anywhere said so.

        BOTH HALVES ARE ASSERTED HERE because either alone is the bug: the
        land must still close (blocking it would be a worse defect than the
        one being fixed), AND the operator must be told."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        with mock.patch.object(landreq, "record_land",
                               return_value=(None, "signer unavailable")):
            lr, why = landreq.close(row["id"], "landed", live=True)

        # THE LAND STILL CLOSES — this is the half a loud warning must not cost.
        self.assertIsNone(why, "witnessing failure BLOCKED a land: %r" % (why,))
        self.assertIsNotNone(lr)
        self.assertEqual(lr["close_reason"], "landed")

        # ...AND IT IS NOT SILENT. The fact travels as DATA on the close's
        # own result, so every surface can render it — the CLI prints it,
        # --json carries it, and the owner's card can badge the row. A print
        # from inside the library would serve exactly one of those three.
        self.assertEqual(lr.get("unwitnessed"), "signer unavailable",
                         "the close did not report that it went unwitnessed, "
                         "which is the silence that froze the LANDED card")

    def test_a_fold_that_CAN_witness_is_quiet_and_records(self):
        """THE CONTROL, and without it the arm above passes on a build that
        shouts on EVERY land — which would be its own defect, and the kind
        that gets a warning tuned out within a day."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        seen = {}

        def witnessed(*a, **kw):
            seen["args"], seen["kw"] = a, kw
            return {"id": "receipt-1"}, None

        with mock.patch.object(landreq, "record_land", witnessed):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "landed")
        # THE FOLD ACTUALLY WITNESSED — the wiring is the point, so prove the
        # call happened rather than only that nothing was printed.
        self.assertIn("args", seen,
                      "the landed close never attempted a receipt at all; "
                      "witnessing is still a second verb somebody must "
                      "remember")
        self.assertIsNone(  # noqa: VACUOUS_ASSERTION — V1 kills this
            lr.get("unwitnessed"),
            "a SUCCESSFUL witness still reported the row as unwitnessed")

    def test_a_receipt_READ_crash_cannot_fail_a_land_already_recorded(self):
        """THE TWIN OF THE ARM BELOW, and the reason it was needed is the
        lesson. That arm patches record_land to raise — the crash site as it
        existed when the arm was written. When I delegated to the strict
        resolver I put a NEW read phase in front of the try, and an OSError
        from it propagated out and failed a close that was already durable.
        The old arm could not see it: it targeted the old raise site, which is
        the same blind spot as a mutation matrix that only mutates the
        function you just wrote.

        The exception boundary belongs around EVERY step that can raise, not
        around the step that raised when it was written."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        with mock.patch.object(landreq, "_existing_receipt",
                               side_effect=OSError("receipt read exploded")):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why, "a receipt-READ crash failed the land")
        # AND IT MUST REPORT AS UNKNOWN, NOT AS A NEGATIVE (the FIX
        # bound on ef0c543800a6). This arm proved the crash could not FAIL the
        # land and stopped there — so when the typed witness state was added on
        # the RETURN path, the RAISE path kept flattening a crashed READ into
        # `unwitnessed`, under the CLI line prescribing `helm lr land`. A
        # crashed read is the STRONGEST unknown available: we could not even
        # establish what the record says, which is precisely when minting a
        # second signed row is the worst move.
        self.assertEqual((lr or {}).get("id"), row["id"],
                         "this is not the closed row: %r" % (lr,))
        self.assertIn("UNKNOWN", lr.get("witness_unknown") or "",
                      "a crashed READ must report UNKNOWN in its typed field: "
                      "%r" % lr.get("witness_unknown"))
        self.assertEqual(lr.get("witness_state"), landreq.R_UNREADABLE,
                         "a read that could not complete is exactly the "
                         "unreadable state, not an untyped failure")
        self.assertIsNone(lr.get("unwitnessed"),
                          "the crash path serialized UNKNOWN as the NEGATIVE "
                          "— the same defect as the return path, through the "
                          "exception door: %r" % lr.get("unwitnessed"))
        self.assertEqual(lr["close_reason"], "landed")
        # THE CAUSE STILL TRAVELS — it just travels in the TYPED field now.
        # This assertion previously named `unwitnessed`, which is how the arm
        # pinned the collapse it was meant to guard: it proved the crash was
        # REPORTED and never asked WHICH state it was reported as.
        self.assertIn("receipt read exploded", lr.get("witness_unknown") or "",
                      "the read crash was swallowed instead of reported")

    def test_a_MINT_crash_after_R_NONE_degrades_to_an_honest_unwitnessed(self):
        """A CRASH IN THE MINT, NOT A CRASH IN WITNESSING GENERALLY (the FIX
        on row fea2dc76). This arm injects at `record_land`, which
        runs only AFTER `_existing_receipt` returned R_NONE — so absence IS
        established and there is genuinely no receipt: the honest report is
        `unwitnessed`, and the CLI's "mint it once the cause is cleared" is
        the right advice.

        ITS NAME AND DOCSTRING USED TO CLAIM THE GENERAL CASE — "a witnessing
        CRASH ... degrades to the same loud line, an unwitnessed land is
        unwitnessed whatever prevented it" — which is the model the split
        disproves and the exact sentence removed from _witness_land's own
        docstring one commit ago. A test whose NAME asserts the wrong contract
        is worse than a stale comment: the name is what the next author reads
        when deciding whether their change is covered, and this one said the
        two catches are interchangeable while the arm below tested only one.

        The twin, test_a_receipt_READ_crash..., covers the other side: a crash
        BEFORE absence is established is UNKNOWN, and minting is the worst
        move available. Neither arm alone is the contract; the pair is."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                               polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        def boom(*a, **kw):
            raise RuntimeError("dregg socket vanished")

        with mock.patch.object(landreq, "record_land", boom):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why, "a witnessing CRASH failed the land")
        self.assertEqual(lr["close_reason"], "landed")
        self.assertIn("dregg socket vanished", lr.get("unwitnessed") or "",
                      "the crash was swallowed instead of reported")

    def test_the_unwitnessed_fact_survives_BOTH_render_paths(self):
        """THE REASON IT IS DATA AND NOT A PRINT, pinned.

        A library that writes to a stream decides for every caller at once.
        My first version printed to stderr from inside the close, which broke
        eight CLI tests asserting a clean stderr — and a stdout print would
        have been worse, because it would land inside `--json` output and no
        parser survives that. The fact rides the close's own result, so each
        surface renders it: text prints a human line, --json carries a field,
        and the owner's derived LANDED card can badge the row off the same
        key.

        BOTH ARMS IN ONE TEST ON PURPOSE. They are one contract — "the fact
        is in the DATA" — and splitting them invites fixing one render while
        the other silently loses the field."""
        row = {"id": "abc123def456", "unwitnessed": "signer unavailable"}

        with mock.patch.object(landreq, "close", return_value=(row, None)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = landreq._cmd_close(["abc123def456", "--reason", "landed",
                                         "--live", "--json"])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())      # must PARSE, not just contain
        self.assertEqual(parsed.get("unwitnessed"), "signer unavailable",
                         "--json lost the witness state")

        with mock.patch.object(landreq, "close", return_value=(row, None)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = landreq._cmd_close(["abc123def456", "--reason", "landed",
                                         "--live"])
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("CLOSED (LANDED)", text, "the close line is gone")
        self.assertIn("NOT WITNESSED", text, "text mode lost the witness state")
        self.assertIn("signer unavailable", text, "the reason is not rendered")

        # CONTROL: a WITNESSED close carries neither, on either path.
        clean = {"id": "abc123def456"}
        with mock.patch.object(landreq, "close", return_value=(clean, None)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                landreq._cmd_close(["abc123def456", "--reason", "landed",
                                    "--live"])
        self.assertNotIn("NOT WITNESSED", buf.getvalue(),
                         "a witnessed close still printed the warning — the "
                         "shape that gets a warning tuned out in a day")

    def _already_witnessed(self, tip):
        """Stand the LOOKUP up as if `helm lr land` had already run.

        Patched at `_existing_receipt`, NOT at `_receipts_by_tip`: that index
        is read by the board projection too, and a hand-made row without a
        `payload` made the whole close fail on a KeyError from a surface this
        test is not about. Mock at the seam you are testing, not upstream of
        three other readers. `_existing_receipt`'s own three states are
        exercised directly in ExistingReceiptTest below.
        """
        return mock.patch.object(landreq, "_existing_receipt",
                                 return_value=({"id": str(tip)}, None, landreq.R_LOCAL))

    def test_a_manual_land_then_a_close_mints_ONE_receipt_not_two(self):
        """On the NORMAL lifecycle rather than a constructed one:
        `helm lr land` succeeds, then close(landed) runs and mints a SECOND
        receipt for the same reviewed tip. record_land call_count 2, duplicate
        signed rows, and the verb-split contract broken.

        ALREADY WITNESSED IS NOT UNWITNESSED."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        calls = {"n": 0}

        def counting(*a, **kw):
            calls["n"] += 1
            return {"id": "receipt-2"}, None

        with self._already_witnessed(self.side), \
                mock.patch.object(landreq, "record_land", counting):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why)
        self.assertEqual(calls["n"], 0,
                         "the close minted a SECOND receipt for a tip that "
                         "already had one — duplicate signed rows")
        self.assertIsNone(lr.get("unwitnessed"),
                          "an already-witnessed land was reported unwitnessed")

    def test_a_LATER_signer_failure_cannot_unwitness_an_existing_receipt(self):
        """THE FALSE NEGATIVE, and it is the worse half of the same bug: with
        a valid receipt already on file, a signer that fails on the SECOND
        attempt made the close report unwitnessed — telling an operator the
        audit trail has a hole that it does not have."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        def boom(*a, **kw):
            return None, "signer unavailable later"

        with self._already_witnessed(self.side), \
                mock.patch.object(landreq, "record_land", boom):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why)
        self.assertIsNone(lr.get("unwitnessed"),
                          "a later signer failure reported unwitnessed while "
                          "a valid receipt exists: %r" % lr.get("unwitnessed"))

    def test_an_UNREADABLE_index_is_UNKNOWN_and_mints_nothing(self):  # noqa: VACUOUS_ASSERTION — the absence here is assertIsNone(lr.get("unwitnessed")), and the rung wants a positive on that exact key, which cannot exist: the assertion's whole content is that the key is ABSENT. The vacuity it guards against is `lr` being empty or None, and that is refused unconditionally two lines earlier by assertEqual(lr.get("id"), row["id"]) — a real closed row, not a dict that satisfies every absence for free. Two further unconditional positives stand on the same object: assertIn("UNKNOWN", lr.get("witness_unknown")) and assertEqual(lr.get("witness_state"), R_UNREADABLE). Mutation-proven: collapsing the typed split so UNKNOWN falls back into `unwitnessed` fails this arm; restoring passes.
        """THE THIRD ANSWER. An index that could not be read does not mean
        'no receipt' — minting blind on unknown is how one land ends up with
        two rows, which _receipts_by_tip calls a conflict and refuses to
        resolve last-wins. So it mints nothing and says UNKNOWN, rather than
        either silently minting or silently claiming unwitnessed."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "review-post-9",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")

        calls = {"n": 0}

        def counting(*a, **kw):
            calls["n"] += 1
            return {"id": "r"}, None

        with mock.patch.object(
                landreq, "_existing_receipt",
                return_value=(None, "the land-receipt index could not be "
                                    "READ, so whether this land is already "
                                    "witnessed is UNKNOWN",
                              landreq.R_UNREADABLE)), \
                mock.patch.object(landreq, "record_land", counting):
            lr, why = landreq.close(row["id"], "landed", live=True)

        self.assertIsNone(why, "an unreadable index blocked a land")
        self.assertEqual(calls["n"], 0, "it minted blind on UNKNOWN")
        # UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE, before anything is
        # asserted ABSENT from it: `lr` must be the real closed row. An empty
        # dict satisfies every assertIsNone below for free.
        self.assertEqual((lr or {}).get("id"), row["id"],
                         "this is not the closed row: %r" % (lr,))
        # THIS ARM USED TO PIN THE DEFECT (row 141d46c5a6ab). It
        # asserted the word UNKNOWN appeared inside `unwitnessed` — which
        # accepted precisely the collapse: an UNKNOWN serialized into the
        # NEGATIVE field, under a CLI line that prescribes `helm lr land`.
        # A test that admits the wrong field is how the wrong field survives
        # a review, so the fix has to land here as well as in the source.
        self.assertIn("UNKNOWN", lr.get("witness_unknown") or "",
                      "an unreadable index must report as UNKNOWN in its own "
                      "typed field: %r" % lr.get("witness_unknown"))
        self.assertEqual(lr.get("witness_state"), landreq.R_UNREADABLE,
                         "the resolver state must survive to the caller, not "
                         "be flattened into a sentence")
        # AND NOTHING MAY CLAIM THE NEGATIVE. This is the assertion the whole
        # finding is about: `unwitnessed` asserts an absence that nobody
        # established, and its CLI text hands the operator the duplicate mint
        # as the remedy.
        self.assertIsNone(lr.get("unwitnessed"),
                          "UNKNOWN was serialized as unwitnessed — the state "
                          "that cannot tell you whether a receipt exists must "
                          "never claim that none does: %r"
                          % lr.get("unwitnessed"))

    def test_own_declared_polarity_outranks_the_chain(self):
        """NEGATIVE CONTROL: a chained SUPERSEDE cannot flip a row whose OWN
        polarity is APPROVE — the chain speaks only when the row is silent.

        PINNED ON THE RESOLVED POLARITY RATHER THAN ON A REFUSAL. Both APPROVE
        and SUPERSEDE now open this door, so a refusal can no longer tell them
        apart and an arm that watched for one would be asserting nothing. The
        dry run reports which polarity was used and whether the chain supplied
        it, which is the property this control was always about.
        """
        row = self.dispatch(ref=self.side, lane="lane/declared", kind="review")
        self.mark_verdict(row["id"], self.side, "clean", polarity="approve")
        self.chained(row, self.side, "supersede", lane="lane/declared-r2")
        out, why = landreq.close(row["id"], "withdrawn", evidence="please",
                                 dry_run=True)
        self.assertIsNone(why, "the row did not reach the landing proof: %s" % why)
        self.assertEqual("approve", out["polarity"],
                         "the chain's SUPERSEDE overrode the row's own APPROVE")
        self.assertIsNone(out["polarity_via_chain"],
                          "a row with its own polarity consulted the chain")

    def test_a_SILENT_row_does_take_its_polarity_from_the_chain(self):
        """The positive half of the control above, so "own outranks chain" is
        a comparison rather than a statement about a door that never consults
        the chain at all."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        out, why = landreq.close(row["id"], "withdrawn", evidence="please",
                                 dry_run=True)
        self.assertIsNone(why, "the silent row did not resolve: %s" % why)
        self.assertEqual("supersede", out["polarity"])
        self.assertTrue(out["polarity_via_chain"],
                        "the chain supplied the polarity but is not recorded")

    def test_landed_work_still_cannot_be_withdrawn_through_the_chain(self):
        """NEGATIVE CONTROL: withdrawn proves ABSENCE. A chain-declared
        SUPERSEDE over work that IS on trunk must still refuse — the polarity
        gate opened, the absence rung refuses."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("IS on trunk", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_sidecar_is_UNKNOWN_for_the_polarity_gate(self):
        """NEGATIVE CONTROL: the same-code set rides the translation sidecar;
        a chain whose sidecar cannot be read is a chain that cannot be fully
        read — UNKNOWN in words, never a close."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        with mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=({}, "permission denied")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertIn("permission denied", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_ledger_never_reaches_a_door(self):
        """NEGATIVE CONTROL: the chain lives in the projection; a projection
        that cannot be read refuses the whole close in words, one layer above
        every door."""
        row = self.undeclared_row()
        self.chained(row, self.side, "supersede")
        with mock.patch.object(landreq, "project",
                               return_value=({}, "ledger torn mid-read")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("ledger torn mid-read", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_superseded_door_walks_the_contrary_branch_on_chain_polarity(self):
        """The superseded door treats a chain-declared SUPERSEDE row exactly
        as an own-declared one: its landed change is CONTRARY debt
        (contrary_state records it), where the silent-chain reading would
        have refused with 'IS on trunk — use landed'."""
        first, why, sent = dispatches.send(
            "codex-3", "lane/prose-r1", "review prose-r1", self.side,
            repo=self.repo, key="key-prose-r1", sign=False, new_work=True)
        self.assertIsNone(why)
        self.assertTrue(sent)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": first["seq"] + 1,
            "id": first["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.side,
            "verdict_ref": "SUPERSEDED in prose — do not keep"}))
        # the chain declares the polarity the prose meant, at the same code
        decl, why, sent = dispatches.send(
            "codex-3", "lane/prose-r2", "declare polarity", self.side,
            repo=self.repo, key="key-prose-r2", sign=False,
            new_work=False, supersedes=first["id"])
        self.assertIsNone(why)
        _out, why = self.mark_verdict(
            decl["id"], self.side, "chain declares supersede",
            polarity="supersede")
        self.assertIsNone(why)
        # the contrary LANDS, then an approved superseding round replaces it
        self.git("merge", "--no-edit", "-q", "side")
        self.git("checkout", "-q", "side")
        fixed = self.commit("resolve findings", path="g")
        self.git("checkout", "-q", self.main)
        second, why, sent = dispatches.send(
            "codex-3", "lane/prose-r3", "superseding round", fixed,
            repo=self.repo, key="key-prose-r3", sign=False,
            new_work=False, supersedes=decl["id"])
        self.assertIsNone(why)
        _out, why = self.mark_verdict(second["id"], fixed,
                                            "re-probed clean",
                                            polarity="approve")
        self.assertIsNone(why)
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(first["id"], "superseded",
                                evidence="r3 approved", tip=fixed)
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "superseded")
        self.assertEqual(lr["contrary_state"], "landed",
                         "the contrary branch must have adjudicated this")
        self.assertEqual(lr["superseding_id"], second["id"])

    def test_chain_declared_contrary_blocks_the_landed_door(self):
        """The landed door learns the same identity: an undeclared row whose
        chain declares SUPERSEDE about its code is a CONTRARY on trunk, not a
        resolution — and the refusal names the declaring round. Control: the
        same row without the declaration closes landed."""
        row = self.undeclared_row()
        kid = self.chained(row, self.side, "supersede")
        self.git("merge", "--no-edit", "-q", "side")
        lr, why = landreq.close(row["id"], "landed", live=True)
        self.assertIsNone(lr)
        self.assertIn("chain-declared SUPERSEDE", why)
        self.assertIn(kid["id"][:12], why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])
        # POSITIVE CONTROL — an identical row with a silent chain closes
        control = self.undeclared_row(lane="lane/silent-control")
        self.chained(control, self.side, None, lane="lane/silent-control-r2")
        lr, why = landreq.close(control["id"], "landed", live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "landed")


class WithdrawTranslationTest(LandReqBase):
    """#79 — the withdrawn door follows REWRITE TRANSLATIONS, both walks of
    one lane: a tip pruned by a recorded rewrite proves absence through its
    live recorded identity, and a recorded identity that LANDED makes
    "absent" a lie the door must catch."""

    def fix_row(self, tip, lane="lane/rewrite"):
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        self.mark_verdict(row["id"], tip, "do not land",
                                polarity="fix")
        return row

    def pruned_tip(self):
        """A reviewed tip whose object a history rewrite destroyed."""
        self.git("checkout", "-q", "-b", "doomed", self.main)
        tip = self.commit("doomed evidence", path="doomed")
        row = self.fix_row(tip)
        self.git("checkout", "-q", self.main)
        self.git("branch", "-D", "doomed")
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        probe = subprocess.run(["git", "-C", self.repo, "cat-file", "-e",
                                tip + "^{commit}"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the fixture must really prune the object")
        return row, tip

    def sidecar(self, *pairs):
        state = os.path.join(landreq.home.global_dir(), ".state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "ref-migrations.jsonl"), "a",
                  encoding="utf-8") as f:
            for old, new in pairs:
                f.write(json.dumps({"old": old, "new": new}) + "\n")

    def test_a_pruned_tip_withdraws_through_its_recorded_translation(self):
        """The #79 acceptance walk: direct absence is UNKNOWN (object gone),
        the recorded live identity is provably absent — close, recording the
        translated proof."""
        row, old = self.pruned_tip()
        self.git("checkout", "-q", "side")
        new = self.commit("rewritten identity, still unlanded", path="g")
        self.git("checkout", "-q", self.main)
        self.sidecar((old, new))
        plan, why = landreq.close(row["id"], "withdrawn",
                                  evidence="rewrite kept it unlanded",
                                  dry_run=True)
        self.assertIsNone(why)
        self.assertEqual(plan["proof_mode"], "translated-absent")
        self.assertEqual(plan["translated_tip"], new)
        lr, why = landreq.close(row["id"], "withdrawn",
                                evidence="rewrite kept it unlanded")
        self.assertIsNone(why)
        self.assertEqual(lr["close_reason"], "withdrawn")
        self.assertEqual(lr["close_proof_mode"], "translated-absent")
        self.assertEqual(lr["translated_tip"], new)
        # and the recorded proof survives a fresh disk replay
        replayed = landreq.get(row["id"])[0]
        self.assertEqual(replayed["close_proof_mode"], "translated-absent")
        self.assertEqual(replayed["translated_tip"], new)

    def test_a_landed_translation_makes_absent_a_lie(self):
        """NEGATIVE CONTROL: the original tip reads absent directly, but its
        recorded translation IS on trunk — the work landed under its
        rewritten identity and the door must refuse."""
        row = self.fix_row(self.side)
        # the rewritten identity is a DIFFERENT patch cut from main (a
        # rewrite changes content), and IT lands; the original side tip
        # itself never does — direct absence would read False.
        self.git("checkout", "-q", "-b", "rewrite", self.main)
        new = self.commit("carried forward under a new identity", path="h")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-edit", "-q", "rewrite")   # translation lands
        self.sidecar((self.side, new))
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("recorded translation", why)
        self.assertIn("IS on trunk", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unprovable_translation_is_UNKNOWN(self):
        """NEGATIVE CONTROL: pruned tip, translation recorded to an object
        git cannot adjudicate — UNKNOWN in words, fail-closed."""
        row, old = self.pruned_tip()
        self.sidecar((old, "f" * 40))
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("recorded translation", why)
        self.assertIn("FAIL-CLOSED", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_an_unreadable_sidecar_refuses_absence_adjudication(self):
        """NEGATIVE CONTROL: an unreadable sidecar cannot say whether a
        translation exists — absence cannot be adjudicated."""
        row, _old = self.pruned_tip()
        with mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=({}, "disk error")):
            lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("unreadable", why)
        self.assertIn("disk error", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])

    def test_a_pruned_tip_with_no_translation_stays_UNKNOWN(self):
        """NEGATIVE CONTROL: the translation walk widens proof, never
        loosens it — no recorded translation leaves the original fail-closed
        refusal exactly as before."""
        row, _old = self.pruned_tip()
        lr, why = landreq.close(row["id"], "withdrawn", evidence="please")
        self.assertIsNone(lr)
        self.assertIn("could not prove", why)
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])


class AbandonTerminalTest(LandReqBase):
    """Evidence loss is terminal only when Git explicitly says MISSING."""

    def reviewed(self, tip=None, polarity="fix", lane="lane/ghost"):
        tip = tip or self.side
        row = self.dispatch(ref=tip, lane=lane, kind="review")
        self.mark_verdict(row["id"], tip, "reviewed", polarity=polarity)
        return row

    def ghost(self, polarity="fix", lane="lane/ghost", path="ghost"):
        # `path` exists so a test may build SEVERAL ghosts in one repo. Same
        # parent + same tree + same message inside one second is the same
        # COMMIT ID, so two default ghosts would silently be one object and
        # the second row would review a tip the first had already destroyed.
        self.git("checkout", "-q", "-b", "ghost-object", self.main)
        tip = self.commit("ghost evidence", path=path)
        row = self.reviewed(tip, polarity=polarity, lane=lane)
        self.git("checkout", "-q", self.main)
        self.git("branch", "-D", "ghost-object")
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        probe = subprocess.run(["git", "-C", self.repo, "cat-file", "-e",
                                tip + "^{commit}"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0,
                            "the positive arm must really remove the object")
        return row, tip

    def test_missing_commit_abandons_with_land_state_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, missing = self.ghost()
        lr, why = landreq.abandon(
            row["id"][:12], "history rewrite destroyed the reviewed object")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertTrue(lr["abandoned"] and lr["terminal"])
        self.assertEqual(lr["land_state"], "UNKNOWN")
        self.assertEqual(lr["polarity"], "fix")
        self.assertEqual(lr["reviewed_tip"], missing)
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertFalse(lr["stalled"] or lr["landed"] or lr["withdrawn"])
        self.assertNotIn(row["id"], [r["id"] for r in landreq.loops()[0]])
        self.assertIn(row["id"], [r["id"] for r in landreq.loops(True)[0]])
        self.assertIn("ABANDONED", landreq._line(lr))
        shown = landreq._render_show(lr)
        self.assertIn("LAND STATE UNKNOWN", shown)
        self.assertIn("history rewrite destroyed", shown)
        self.assertIn("structured-message-and-tag-scan v2 (none trunk mention)", shown)
        self.assertIn("branch    none via git-ref-and-ancestry v1", shown)
        self.assertIn("worktree  none via git-worktree-status v1", shown)

    def test_the_writer_and_the_replay_admit_the_SAME_polarities(self):  # noqa: VACUOUS_ASSERTION — the positive control is the UNCONDITIONAL assertIn("fix", accepted) after the loop, on the same observable the loop fills
        """THE DEFECT REPRODUCED AT THE EXACT TIP — pinned as the
        AGREEMENT it actually is, not as a fact about `concur`.

        `_apply`'s abandon arm requires a WORK polarity. The WRITER blocked
        only `supersede`, so `concur` passed HERE and was refused THERE:
        err=None, an event appended (history 2->3), the row still REVIEWED,
        and the CLI printing ABANDONED over it.

        THE DIVERGENCE IS WORSE THAN EITHER HALF ALONE. A refusal that leaves
        no event is safe. A silent acceptance that changes nothing is an
        operator being told a terminal write happened that did not — and the
        ledger keeping an event to prove them right.

        Walking EVERY polarity the vocabulary admits, instead of asserting
        that `concur` is refused, is what makes the polarity added tomorrow
        covered without editing this test. `supersede` is refused by an
        earlier door while the replay would admit it: writer-stricter-than-
        replay writes no event, so it is the SAFE direction of disagreement
        and the same assertions below hold for it unchanged."""
        accepted = []
        for polarity in verdicts.POLARITIES:
            with self.subTest(polarity=polarity):
                row, _gone = self.ghost(polarity=polarity,
                                        lane="lane/ghost-" + polarity,
                                        path="ghost-" + polarity)
                before = len(dispatches.history(row["id"]))
                _lr, why = landreq.abandon(row["id"][:12],
                                           "history rewrite destroyed it")
                state = landreq.get(row["id"])[0]["state"]
                grew = len(dispatches.history(row["id"])) - before
                if why is None:
                    accepted.append(polarity)
                    self.assertEqual(state, "ABANDONED")
                    self.assertEqual(grew, 1)
                else:
                    self.assertNotEqual(state, "ABANDONED")
                    self.assertEqual(
                        grew, 0,
                        "%s: the writer appended an event the replay ignores "
                        "— the exact divergence that printed ABANDONED over a "
                        "REVIEWED row" % polarity)
        # MUST-HIT CONTROL. Every assertion above is satisfied vacuously by a
        # writer that refuses EVERYTHING, so the loop proves nothing until it
        # is shown to admit real work — and to still exclude concur.
        self.assertIn("fix", accepted)
        self.assertNotIn("concur", accepted)

    def test_structured_trunk_lane_mention_blocks_abandon_without_calling_it_landed(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost(lane="lane/chatnode-posture-r2")
        self.git("commit", "--allow-empty", "-q", "-m",
                 "Merge branch 'chatnode-posture' — restore chat posture")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("structured trunk mention", why)
        self.assertIn("chatnode-posture", why)
        self.assertIn("correlation, not proven landed", why)
        self.assertNotIn("LAND STATE LANDED", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_landed_lane_family_tag_blocks_false_writeoff(self):  # noqa: VACUOUS_ASSERTION — lightweight and annotated tags bind real trunk ancestors and unchanged history
        cases = (("light", False, "gate/%s-r2"),
                 ("annotated", True, "gate/%s-r2"),
                 ("token", False, "archive/%s"))
        for label, annotated, tag_shape in cases:
            with self.subTest(tag=label):
                family = "tag-%s-family" % label
                row, _missing = self.ghost(lane="lane/" + family + "-r1")
                self.git("commit", "--allow-empty", "-q", "-m",
                         "repair arrived under paraphrased words")
                tag = tag_shape % family
                args = ["tag"]
                if annotated:
                    args.extend(("-a", tag, "-m", "review gate"))
                else:
                    args.append(tag)
                self.git(*args)
                before = len(dispatches.history(row["id"]))
                lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("landed family tag", why)
                self.assertIn(tag, why)
                self.assertIn("correlation, not proven landed", why)
                self.assertNotIn("LAND STATE LANDED", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_family_tag_not_on_trunk_does_not_claim_landing(self):  # noqa: VACUOUS_ASSERTION — trunk-ancestor positive control above binds this negative arm
        family = "tag-unlanded-family"
        row, _missing = self.ghost(lane="lane/" + family + "-r1")
        self.git("checkout", "-q", "-b", "tag-only", self.main)
        self.commit("tagged but not on trunk", path="tag-only")
        self.git("tag", "gate/" + family + "-r2")
        self.git("checkout", "-q", self.main)
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertEqual(lr["land_state"], "UNKNOWN")

    def test_unreadable_tag_scan_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — non-empty Git failure arm binds unchanged history
        row, _missing = self.ghost(lane="lane/unreadable-tag-family-r1")
        before = len(dispatches.history(row["id"]))
        real_git = landreq._git

        def fake_git(gitdir, *args, **kwargs):
            if args == ("for-each-ref", "--format=%(refname)", "refs/tags"):
                return subprocess.CompletedProcess(
                    args, 128, stdout="", stderr="fatal: refs unreadable")
            return real_git(gitdir, *args, **kwargs)

        with mock.patch.object(landreq, "_git", side_effect=fake_git):
            lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertIn("tag", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_lane_family_suffixes_widen_iteratively_for_refusal_only(self):
        self.assertEqual(
            landreq._lane_family_names("feature-review-r2"),
            ("feature-review-r2", "feature-review", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-re-review"),
                         ("feature-re-review", "feature-re", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-build"),
                         ("feature-build", "feature"))
        self.assertEqual(landreq._lane_family_names("feature-fix"),
                         ("feature-fix", "feature"))
        self.assertEqual(
            landreq._lane_family_names("lr-delivery-leg-contract"),
            ("lr-delivery-leg-contract", "lr-delivery-leg", "lr-delivery"))
        self.assertEqual(
            landreq._lane_family_names("lr-undeclared-terminal-xrev"),
            ("lr-undeclared-terminal-xrev", "lr-undeclared-terminal",
             "lr-undeclared"))
        self.assertEqual(landreq._lane_family_names("feature"), ("feature",))

    def test_every_tag_namespace_gets_the_same_suffix_tolerance(self):
        """A post-land finding, reproduced against tags that exist
        in THIS repo: suffix tolerance lived only in a special gate/ branch,
        and every other namespace fell to a boundary regex whose trailing
        lookahead rejects a FOLLOWING HYPHEN — the under-match direction,
        which for a block-only interlock is the direction that permits a
        wrongful abandon. One rule now: strip any <namespace>/ prefix, then
        startswith + separator boundary."""
        m = landreq._tag_matches_lane_family
        names = ("roster-read-cache",)
        # The three live MISSES from the finding, now matches:
        self.assertTrue(m("refs/tags/archive/roster-read-cache-64b0700", names))
        self.assertTrue(m("refs/tags/archive/roster-read-cache-v2", names))
        self.assertTrue(m("refs/tags/rescue/ds4pro-landreq-staged-2026-07-27",
                          ("ds4pro-landreq-staged",)))
        # The prior matches stay matches (no regression):
        self.assertTrue(m("refs/tags/rescue/codex-slice4-staged",
                          ("codex-slice4-staged",)))
        self.assertTrue(m("refs/tags/gate/argv-body-guard-r4",
                          ("argv-body-guard",)))
        # PRODUCTION NAMES, not a hand-built tuple — a review
        # caught the first version asserting run-ons miss against a fixture
        # production never builds: _lane_trunk_mention feeds
        # _lane_family_names(lane), whose STEM ("roster-read") catches the
        # run-on via startswith + separator ("-cachex" starts with a
        # separator). So under the real code path a run-on MATCHES — and that
        # is ACCEPTED refusal-only noise, written here deliberately: the leg
        # is block-only, a false refusal costs one human investigation, a
        # false permit is an irreversible write-off. A later author who reads
        # an over-match as a bug and tightens the boundary walks straight
        # back into the under-match this lane fixed.
        prod = landreq._lane_family_names("roster-read-cache")
        self.assertIn("roster-read", prod)          # the stem that decides
        self.assertTrue(m("refs/tags/archive/roster-read-cachex", prod))
        self.assertTrue(m("refs/tags/rescue/roster-read-cache2", prod))
        # The boundary claim, restated where it is TRUE: against the exact
        # lane name alone (no stem), a run-on still fails the separator rule.
        self.assertFalse(m("refs/tags/archive/roster-read-cachex", names))
        self.assertFalse(m("refs/tags/rescue/roster-read-cache2", names))
        # And an unrelated lane still misses entirely, production names too.
        self.assertFalse(m("refs/tags/archive/roster-read-cache-v2",
                           landreq._lane_family_names("lr-projection")))

    def test_one_family_vocabulary_feeds_both_consumers(self):
        """SINGLE FAMILY AUTHORITY. `_lane_stem` (the discharge ADMIT) and
        `_lane_family_names` (refusal-only abandonment evidence) both derive
        from _LANE_FAMILY_SUFFIXES. Two independently-authored suffix sets
        once sat in this one file and disagreed about -build, -re-review,
        -xrev and -v2 — a renamed lane was FAMILY to one side and a STRANGER
        to the other. Every vocabulary token must read as the SAME family on
        both sides. The stem is deliberately SHORT and single-segment
        ("feature", 7 chars): prefix expansion never yields it, so the
        family-names side of every assertion rides on the suffix vocabulary
        alone — the exact thing this test pins."""
        # UNCONDITIONAL positive control on -build, the token the two forked
        # sets historically disagreed about, before the vocabulary loop.
        self.assertEqual(landreq._lane_stem("feature-build"), "feature")
        self.assertIn("feature", landreq._lane_family_names("feature-build"))
        for suffix in ("re-review", "rereview", "review", "build", "fix",
                       "xrev", "retry", "redo", "r3", "v2"):
            lane = "feature-" + suffix
            self.assertEqual(landreq._lane_stem(lane), "feature", lane)
            self.assertIn("feature", landreq._lane_family_names(lane), lane)

    def test_renamed_lane_family_trunk_mention_blocks_false_writeoff(self):  # noqa: VACUOUS_ASSERTION — three live census shapes bind real rows and unchanged history
        cases = (
            ("lr-delivery-leg-review", "lr-delivery-leg", "r6"),
            ("lr-delivery-leg-contract", "lr-delivery-leg", "r7"),
            ("lr-undeclared-terminal-xrev", "lr-undeclared-terminal", "r3"),
        )
        for lane, family, round_name in cases:
            with self.subTest(lane=lane):
                row, _missing = self.ghost(lane="lane/" + lane)
                self.git("commit", "--allow-empty", "-q", "-m",
                         "merge: %s %s — landed family work" %
                         (family, round_name))
                before = len(dispatches.history(row["id"]))
                lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("AMBIGUOUS", why)
                self.assertIn(family, why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_incidental_short_stem_match_is_ambiguous_and_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost(lane="lane/delim-r1")
        self.git("commit", "--allow-empty", "-q", "-m",
                 "fix delimiter parsing in unrelated input")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("AMBIGUOUS", why)
        self.assertIn("delimiter", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_live_lane_branch_blocks_abandon_after_reviewed_tip_dies(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        lane = "withdraw-contradicted-exit"
        row, _missing = self.ghost(lane="lane/" + lane)
        self.git("checkout", "-q", "-b", "lane/" + lane, self.main)
        self.commit("live branch work", path="live-branch")
        self.git("checkout", "-q", self.main)
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("lane branch", why)
        self.assertIn("unlanded", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_renamed_lane_family_branch_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — live family branch binds a real row and unchanged history
        family = "renamed-live-work"
        row, _missing = self.ghost(lane="lane/" + family + "-contract")
        self.git("checkout", "-q", "-b", "lane/" + family, self.main)
        self.commit("live renamed-family work", path="renamed-live-branch")
        self.git("checkout", "-q", self.main)
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("lane branch", why)
        self.assertIn("unlanded", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_dirty_registered_worktree_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        lane = "dirty-ghost"
        row, _missing = self.ghost(lane="lane/" + lane)
        wt = os.path.join(self.tmp, "dirty-worktree")
        self.git("worktree", "add", "-q", "-b", "lane/" + lane, wt, self.main)
        with open(os.path.join(wt, "uncommitted"), "w", encoding="utf-8") as f:
            f.write("live dirt\n")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("dirty worktree", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_renamed_lane_family_dirty_worktree_blocks_abandon(self):  # noqa: VACUOUS_ASSERTION — registered family worktree binds a real row and unchanged history
        family = "renamed-dirty-work"
        row, _missing = self.ghost(lane="lane/" + family + "-xrev")
        wt = os.path.join(self.tmp, "opaque-renamed-family-worktree")
        self.git("worktree", "add", "-q", "-b", "lane/" + family,
                 wt, self.main)
        with open(os.path.join(wt, "uncommitted"), "w", encoding="utf-8") as f:
            f.write("live renamed-family dirt\n")
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(lr)
        self.assertIn("dirty worktree", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unreadable_lane_branch_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — three non-empty Git failure arms bind unchanged history
        real_git = landreq._git
        cases = (
            ("resolve", 128, "", None),
            ("identity", 0, "not-a-full-object-id\n", None),
            ("ancestry", 0, self.c + "\n", landreq.UNDETERMINED),
        )
        for name, returncode, stdout, relation in cases:
            with self.subTest(name=name):
                lane = "unreadable-branch-" + name
                row, _missing = self.ghost(lane="lane/" + lane)
                ref = "refs/heads/lane/" + lane
                before = len(dispatches.history(row["id"]))

                def fake_git(gitdir, *args, **kwargs):
                    if args == landreq._REF_ARGV + (ref,):
                        return subprocess.CompletedProcess(
                            args, returncode, stdout=stdout, stderr="fatal")
                    return real_git(gitdir, *args, **kwargs)

                ancestry = mock.patch.object(
                    landreq, "_ancestry", return_value=relation) \
                    if relation is not None else contextlib.nullcontext()
                with mock.patch.object(landreq, "_git", side_effect=fake_git), ancestry:
                    lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("UNKNOWN", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unreadable_registered_worktree_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — four non-empty Git failure arms bind unchanged history
        real_git = landreq._git
        cases = ("list", "missing-path", "status-missing", "status-error")
        for name in cases:
            with self.subTest(name=name):
                lane = "unreadable-worktree-" + name
                row, _missing = self.ghost(lane="lane/" + lane)
                path = os.path.join(self.tmp, "registered-" + name)
                if name in ("status-missing", "status-error"):
                    os.makedirs(path)
                porcelain = ("worktree %s\nbranch refs/heads/lane/%s\n\n"
                             % (path, lane))
                before = len(dispatches.history(row["id"]))

                def fake_git(gitdir, *args, **kwargs):
                    if args == ("worktree", "list", "--porcelain"):
                        if name == "list":
                            return subprocess.CompletedProcess(
                                args, 128, stdout="", stderr="fatal")
                        return subprocess.CompletedProcess(
                            args, 0, stdout=porcelain, stderr="")
                    return real_git(gitdir, *args, **kwargs)

                if name == "status-missing":
                    status = mock.patch.object(landreq, "_worktree_status",
                                               return_value=None)
                elif name == "status-error":
                    result = subprocess.CompletedProcess(
                        ("git", "status"), 128, stdout="", stderr="fatal")
                    status = mock.patch.object(landreq, "_worktree_status",
                                               return_value=result)
                else:
                    status = contextlib.nullcontext()
                with mock.patch.object(landreq, "_git", side_effect=fake_git), status:
                    lr, why = landreq.abandon(row["id"], "write off reviewed work")
                self.assertIsNone(lr)
                self.assertIn("UNKNOWN", why)
                self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_porcelain_branch_identity_ignores_a_dirty_lookalike_path(self):  # noqa: VACUOUS_ASSERTION — clean match plus dirty negative control bind exact branch identity
        lane = "identity-target"
        row, _missing = self.ghost(lane="lane/" + lane)
        matching = os.path.join(self.tmp, "opaque-matching-worktree")
        lookalike = os.path.join(self.tmp, lane)
        self.git("worktree", "add", "-q", "-b", "lane/" + lane,
                 matching, self.main)
        self.git("worktree", "add", "-q", "-b", "lane/unrelated-dirty",
                 lookalike, self.main)
        with open(os.path.join(lookalike, "uncommitted"), "w",
                  encoding="utf-8") as f:
            f.write("unrelated live dirt\n")
        lr, why = landreq.abandon(row["id"], "write off reviewed work")
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "ABANDONED")
        self.assertEqual(lr["abandon_worktree_state"], "clean")

    def test_existing_commit_refuses_and_names_the_honest_alternative(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row = self.reviewed()
        before = len(dispatches.history(row["id"]))
        lr, why = landreq.abandon(row["id"], "close inconvenient work")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertIn("helm lr withdraw", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)
        self.assertFalse(landreq.get(row["id"])[0].get("abandoned", False))

    def test_commit_exists_only_explicit_missing_is_FALSE(self):  # noqa: VACUOUS_ASSERTION — paired commit/missing controls bind both concrete outcomes
        sha = self.side
        cases = (
            (None, None),
            (subprocess.CompletedProcess(("git",), 128, stdout="", stderr="fatal"),
             None),
            (subprocess.CompletedProcess(
                ("git",), 0, stdout=sha + "^{commit} missing\n", stderr=""),
             False),
            (subprocess.CompletedProcess(
                ("git",), 0, stdout=sha + " commit 123\n", stderr=""), True),
        )
        for result, expected in cases:
            with self.subTest(result=result, expected=expected), \
                    mock.patch.object(landreq, "_git", return_value=result):
                self.assertIs(landreq._commit_exists(self.repo, sha), expected)

    def test_UNKNOWN_git_state_refuses_without_an_event(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost()
        before = len(dispatches.history(row["id"]))
        with mock.patch.object(landreq, "_commit_exists", return_value=None):
            lr, why = landreq.abandon(row["id"], "evidence unavailable")
        self.assertIsNone(lr)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_preflight_missing_but_mutation_boundary_exists_refuses(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        row, _missing = self.ghost()
        before = len(dispatches.history(row["id"]))
        with mock.patch.object(landreq, "_commit_exists",
                               side_effect=(False, True)) as probe:
            lr, why = landreq.abandon(row["id"], "history rewrite")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_repo_id_is_part_of_the_identity_not_the_callers_checkout(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.git("init", "-q", cwd=other)
        self.git("config", "user.email", "other@example.com", cwd=other)
        self.git("config", "user.name", "Other", cwd=other)
        with open(os.path.join(other, "base"), "w", encoding="utf-8") as f:
            f.write("base\n")
        self.git("add", "base", cwd=other)
        self.git("commit", "-q", "-m", "base", cwd=other)
        main = self.git("symbolic-ref", "--short", "HEAD", cwd=other)
        self.git("checkout", "-q", "-b", "side", cwd=other)
        with open(os.path.join(other, "only-there"), "w", encoding="utf-8") as f:
            f.write("live\n")
        self.git("add", "only-there", cwd=other)
        self.git("commit", "-q", "-m", "live cross-repo object", cwd=other)
        tip = self.git("rev-parse", "HEAD", cwd=other)
        self.git("checkout", "-q", main, cwd=other)
        # Minted as the OTHER repository's own helm — this arm's subject is
        # that repo_id is part of the row's identity, which needs the row.
        from tests._tmphome import dispatch_home
        with dispatch_home(other):
            row = dispatches.add("codex-3", "lane/cross-repo", ref=tip,
                                 repo=other, new_work=True, kind="review")
        self.mark_verdict(row["id"], tip, "reviewed", polarity="approve")
        lr, why = landreq.abandon(row["id"], "wrong-repo would call this missing")
        self.assertIsNone(lr)
        self.assertIn("exists", why)
        self.assertFalse(landreq.get(row["id"])[0].get("abandoned", False))

    def test_build_rows_and_bad_reasons_are_refused(self):  # noqa: VACUOUS_ASSERTION — paired positive control binds a real row/event
        build = self.dispatch(ref=self.side, lane="lane/build", kind="build")
        self.mark_verdict(build["id"], self.side, "historical bad shape",
                                polarity="fix")
        lr, why = landreq.abandon(build["id"], "not a review")
        self.assertIsNone(lr)
        self.assertIn("not a review row", why)
        row, _missing = self.ghost()
        for reason in ("", "two\nlines", "control\x00byte"):
            lr, why = landreq.abandon(row["id"], reason)
            self.assertIsNone(lr)
            self.assertIn("abandon reason", why)

    def test_cli_requires_named_reason_and_reports_write_off(self):
        row, _missing = self.ghost()
        rc, _out, err = run(["abandon", row["id"]])
        self.assertEqual(rc, 2)
        self.assertIn("--reason", err)
        rc, out, err = run(["abandon", row["id"][:12], "--reason",
                            "history rewrite destroyed the object"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("ABANDONED", out)
        self.assertIn("LAND STATE UNKNOWN", out)
        self.assertIn("WRITES OFF REVIEWED WORK", out)


class ReceiptBase(LandReqBase):
    """Shared harness for land-receipt replay. Signed emit is mocked (hermetic —
    no node, no binary), exactly as test_chat_v2 does."""

    def gitdir(self):
        return self.git("rev-parse", "--absolute-git-dir")

    def signed(self, info=None):
        """The room node is UP: emit returns a committed signing receipt."""
        return mock.patch.object(chat, "emit_coordination_turn",
                                 return_value=(dict(info or SENT), None))

    def down(self, code="node_unreachable", reason="chat node unreachable"):
        """The room node is DOWN: emit fails open with a structured diagnostic."""
        return mock.patch.object(chat, "emit_coordination_turn",
                                 return_value=(None, chat._diag(code, reason)))

    def record(self, tip, trunk_sha, lane="lane/foo", branch="seat/foo",
               patch_id=None, **kw):
        """Record one signed receipt, asserting it was actually recorded."""
        with self.signed():
            rec, err = landreq.record_land(
                lane, branch, tip, patch_id or "e" * 40, trunk_sha,
                repo_id=kw.pop("repo_id", self.gitdir()), **kw)
        self.assertIsNone(err)
        return rec

    def rows(self):
        """Every raw row currently in the durable index."""
        with open(landreq.receipts_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def write_rows(self, *rows):
        """Overwrite the index with exactly these raw rows — how a hostile or
        corrupt writer is simulated (the index is a plain local file)."""
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def landed_row(self, tip, rid="aabbccdd", lane="lane/pruned"):
        """A verdict'd dispatch row bound to `tip` — the loop a receipt speaks
        for. Stamped before the compat boundary so the snapshot row replays as
        verdict'd (the reduced core's own grammar)."""
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3", "lane": lane,
               "ref": tip, "tip": tip, "note": None, "deadline_s": 60,
               "source": "x", "status": "verdict", "verdict_ref": "CLEAR",
               "polarity": "approve", "reviewed_tip": tip,
               "repo_id": self.gitdir(),
               "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return rid

    def land_side(self):
        """Land `side` onto trunk with a real merge commit; return the new trunk."""
        self.git("merge", "-q", "--no-ff", "side", "-m", "land side")
        return self.git("rev-parse", self.main)


class LandReceiptTest(ReceiptBase):
    """The durable candidate receipt and its honest non-authority boundary."""

    def test_recorded_receipt_stays_diagnostic_when_git_evidence_is_gone(self):
        # Git cannot see the pruned object, so observation is UNAVAILABLE. The
        # local row records what the signer command returned, but replay cannot
        # verify that turn's payload; it must not manufacture LANDED.
        pruned = "0" * 40
        self.landed_row(pruned)

        lr = landreq.get("aabbccdd")[0]
        # This deliberately old snapshot carries no replayable verdict polarity,
        # so the verdict is REVIEWED/UNDECLARED rather than guessed APPROVED.
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["observable"])
        self.assertEqual(lr["receipt_state"], landreq.R_NONE)

        with self.signed() as emit:
            rec, err = landreq.record_land(
                "lane/pruned", "seat/pruned", pruned, "e" * 40, "d" * 40,
                repo_id=self.gitdir(), has_upstream=False)
        self.assertIsNone(err)
        self.assertEqual((rec["chain"], rec["turn"]), (7, "a" * 64))
        self.assertEqual(emit.call_args[0][0], landreq.LAND_TOPIC)
        # #142: record_land canonicalizes the lane BEFORE signing, so the
        # signed payload is the one over the stored bare spelling.
        self.assertEqual(emit.call_args[0][1],
                         landreq.land_payload("pruned", "seat/pruned",
                                              pruned, "e" * 40, "d" * 40))
        self.assertTrue(emit.call_args[0][1].startswith(landreq.LAND_TAG))

        lr = landreq.get("aabbccdd")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["landed"] or lr["terminal"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])
        shown = run(["show", "aabbccdd"])[1]
        self.assertIn("local-unverified", shown)
        self.assertNotIn("[recorded receipt]", shown)

    def test_receipt_lane_is_canonical_at_write_and_verbatim_at_replay(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls ARE the assertEquals on the same observables (stored row == ["foo"] and stored payload == the bare-spelling payload, both loud on an empty index); the assertNotEquals pin the as-written law
        """#142 at the RECEIPT layer: the WRITER canonicalizes
        the lane before signing/storing (`record_land`), so a new receipt
        stores the bare spelling and its payload was signed over that bare
        spelling. `land_record` itself joins fields VERBATIM — replay
        recomputes stored payloads through it, so canonicalizing there would
        judge historical prefixed rows under today's spelling (the live-ledger
        29/29 rebind failure)."""
        args = ("seat/foo", "1" * 40, "e" * 40, "2" * 40)
        # The as-written law: two spellings are two DIFFERENT records/payloads.
        self.assertNotEqual(landreq.land_record("lane/foo", *args),
                            landreq.land_record("foo", *args))
        self.assertNotEqual(landreq.land_payload("lane/foo", *args),
                            landreq.land_payload("foo", *args))
        # The fixture records under "lane/foo"; the stored row is bare and its
        # payload is the one signed over the BARE spelling.
        self.record("1" * 40, "2" * 40)
        self.assertEqual([r["lane"] for r in self.rows()], ["foo"])
        rec = self.rows()[0]
        self.assertEqual(rec["payload"],
                         landreq.land_payload("foo", rec["branch"],
                                              rec["reviewed_tip"],
                                              rec["patch_id"],
                                              rec["trunk_sha"]))

    def test_the_signed_payload_binds_every_field(self):
        # The payload is a tagged digest over the WHOLE record, so changing one
        # stored field without changing the stored payload is detectable. This is
        # local consistency, not proof that dregg's turn carried that payload.
        base = ("lane/a", "seat/a", "1" * 40, "e" * 40, "2" * 40)
        p = landreq.land_payload(*base)
        self.assertEqual(len(p), len(landreq.LAND_TAG) + 64)
        self.assertLess(len(p.encode("utf-8")), 104)
        for i in range(5):
            other = list(base)
            other[i] = other[i][:-1] + "f" if i else "lane/b"
            self.assertNotEqual(p, landreq.land_payload(*other))
        # The RS-join is injective only because a field may never CONTAIN the
        # separator — so that is the invariant actually enforced, at BOTH the
        # write and the replay boundary (a slid pair does collide as a digest:
        # `_field` is what makes it unreachable, and it is tested as such).
        self.assertEqual(landreq.land_payload("a\x1eb", "c", "1" * 40, None, "2" * 40),
                         landreq.land_payload("a", "b\x1ec", "1" * 40, None, "2" * 40))
        self.assertIsNone(landreq._field("a\x1eb"))           # refused as a field
        with self.signed():                                   # refused at write
            self.assertIn("printable line", landreq.record_land(
                "a\x1eb", "c", "1" * 40, None, "2" * 40)[1])
        rec = dict(schema=landreq.LAND_SCHEMA, topic=landreq.LAND_TOPIC,
                   id="1" * 40, reviewed_tip="1" * 40, lane="a\x1eb", branch="c",
                   patch_id=None, trunk_sha="2" * 40, turn="a" * 64,
                   receipt="b" * 64, chain=1,
                   payload=landreq.land_payload("a\x1eb", "c", "1" * 40, None,
                                                "2" * 40))
        self.assertIn("printable line",                       # refused at replay
                      landreq._validate_receipt(rec, "1" * 40)[1])

    def test_node_down_falls_open_to_git_observation(self):
        # FAIL-OPEN: an unreachable cave-node must NEVER block a land or crash
        # the recorder. Nothing is written and landing stays EXACTLY git-observed.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()

        with self.down():
            rec, why = landreq.record_land(
                "lane/foo", "seat/foo", self.side, "e" * 40, trunk,
                repo_id=self.gitdir(), has_upstream=False)
        self.assertIsNone(rec)                                      # no receipt
        self.assertIn("unreachable", why)                           # honest why
        self.assertFalse(os.path.exists(landreq.receipts_path()))    # no write

        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")     # git observed the merge
        self.assertTrue(lr["landed"])
        self.assertFalse(lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_NONE)

    def test_a_receipt_without_its_anchor_is_refused_not_written(self):
        # trunk_sha identifies the historical land event the turn claims. Even a
        # diagnostic candidate without that identity is malformed and is refused
        # before signing.
        with self.signed() as emit:
            rec, why = landreq.record_land("lane/x", "seat/x", "1" * 40,
                                           "e" * 40, None)
        self.assertIsNone(rec)
        self.assertIn("ACTUAL trunk sha", why)
        emit.assert_not_called()                    # never even signed
        self.assertFalse(os.path.exists(landreq.receipts_path()))
        # same for a tip that is not a full sha, and for a junk patch-id
        with self.signed():
            self.assertIn("FULL sha", landreq.record_land(
                "l", "b", "abc", None, "2" * 40)[1])
            self.assertIn("patch_id", landreq.record_land(
                "l", "b", "1" * 40, "patchid-x", "2" * 40)[1])

    def test_cli_land_verb_records_then_is_fail_open(self):
        # `helm lr land <id>` WITNESSES a land; it does NOT perform one. The
        # comment here used to call it "the integrator's ff-merge hook", which
        # is the exact misreading the verb's imperative name invites — and it
        # was written into the test that documents it. Nothing in this path
        # moves a ref; the merge is the integrator's own git work.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.land_side()
        with self.signed():
            rc, out, err = run(["land", row["id"]])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("receipt recorded", out)
        self.assertIn("NO MERGE PERFORMED", out,
                      "the success line must name the non-action — an "
                      "integrator scanning output sees `land` beside exit 0 "
                      "and concludes the lane landed")
        rec = self.rows()[0]
        self.assertEqual(rec["reviewed_tip"], self.side)
        self.assertEqual(rec["trunk_sha"], self.git("rev-parse", self.main))
        self.assertTrue(rec["patch_id"])            # correlation evidence rode
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        other = self.dispatch(ref=self.b, lane="lane/down")   # b is already trunk
        self.mark_verdict(other["id"], self.b, "ok",
                                polarity="approve")
        with self.down():
            rc, out, err = run(["land", other["id"]])
        self.assertEqual(rc, 0)                     # fail-open, land not blocked
        self.assertIn("fail-open", out)
        # BOTH exits must name the non-action, not just the happy one. This is
        # the branch an integrator actually hits when HELM_CELL_BIN is unset,
        # and its old wording ("land not blocked") read as though the land had
        # proceeded — the reading that cost a real integrator a false "landed"
        # report, caught only by checking origin/main afterwards.
        self.assertIn("NO MERGE PERFORMED", out)


class ReceiptAuthorityTest(ReceiptBase):
    """trunk_sha is the candidate event identity, never local authority.

    These cells preserve the correct historical semantics while proving that the
    local index cannot apply them until dregg payload binding is verifiable.
    """

    def test_a_cherry_pick_is_landed_by_patch_identity_not_the_receipt(self):
        # Cherry-picking lands the change while leaving the reviewed object
        # non-ancestral. Main's patch-identity fallback proves the land; adding a
        # local-unverified receipt changes only the diagnostic axis.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.git("cherry-pick", self.side)
        trunk = self.git("rev-parse", self.main)
        self.assertEqual(landreq._ancestry(self.gitdir(), self.side,
                                          "refs/heads/" + self.main),
                         landreq.NOT_ANCESTOR)
        before = landreq.get(row["id"])[0]
        self.assertEqual(before["state"], "LANDED")
        self.assertTrue(before["landed"])

        self.record(self.side, trunk)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"])
        self.assertFalse(lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])

    def test_a_real_revert_stays_landed_it_is_a_later_separate_event(self):
        # A real `git revert` ADDS a commit; it does not retract the land. Git's
        # historical ancestry proves the land independently of the local receipt.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        self.git("revert", "--no-edit", "-m", "1", trunk)
        self.assertNotEqual(self.git("rev-parse", self.main), trunk)   # trunk moved
        self.assertEqual(landreq._ancestry(self.gitdir(), self.side,
                                          "refs/heads/" + self.main),
                         landreq.ANCESTOR)       # the tip is STILL in history
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")  # the land happened; revert is later
        self.assertFalse(lr["receipt"])           # Git, not the local row, proved it
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_a_history_rewrite_contradicts_the_candidate_receipt(self):
        # A rewrite that drops the claimed trunk out of history contradicts even
        # the local candidate. Live Git governs throughout; the receipt state
        # changes only to preserve the useful diagnostic.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")

        self.git("reset", "-q", "--hard", "HEAD~1")      # the land left history
        self.assertEqual(landreq._ancestry(self.gitdir(), trunk,
                                          "refs/heads/" + self.main),
                         landreq.NOT_ANCESTOR)
        lr = landreq.get(row["id"])[0]
        self.assertNotEqual(lr["state"], "LANDED")
        self.assertFalse(lr["landed"])
        self.assertEqual(lr["receipt_state"], landreq.R_CONTRADICTED)
        self.assertIn("left trunk's history", lr["receipt_reason"])

    def test_live_git_still_observes_the_push_with_a_local_receipt_present(self):
        # Removing local receipt authority must not break the real feature: Git
        # still distinguishes a local merge from the later upstream push.
        self.add_origin()                                  # origin lacks the land
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk, has_upstream=True, upstream=False)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")      # local only
        self.assertTrue(lr["merged_local"])
        self.assertFalse(lr["landed"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

        self.git("push", "-q", "origin", self.main)        # the push happens
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")


class ReceiptValidationBase(ReceiptBase):
    """`valid_row` records one genuine receipt for a real land, the baseline
    every receipt arm corrupts, and `assert_receipt_diagnostic` asserts the
    land stays LANDED while the receipt axis reports the named state.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def valid_row(self):
        """One genuinely recorded receipt for a REAL land, as the baseline."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "LANDED")
        return row["id"], self.rows()[0]

    def assert_receipt_diagnostic(self, rid, state, needle=None):
        lr = landreq.get(rid)[0]
        # Receipt contamination is diagnostic only: it neither manufactures a
        # land nor vetoes the independent, readable Git fact established by
        # valid_row(). The two evidence axes must stay orthogonal.
        self.assertEqual(lr["state"], "LANDED")
        self.assertTrue(lr["landed"] and lr["observable"])
        self.assertFalse(lr["stalled"])
        self.assertEqual(lr["receipt_state"], state)
        if needle:
            self.assertIn(needle, lr["receipt_reason"])
        shown = run(["show", rid])[1]
        self.assertIn("LANDED", shown)
        self.assertIn(state, shown)
        # SCOPED TO THE TWO AXES THIS ASSERTS. A bare substring over the whole
        # render also caught the DWELL line, which legitimately reads UNKNOWN
        # here: a git-observed landing is terminal and carries no closure
        # instant anywhere, so its age was never measured. The claim being made
        # is that receipt corruption manufactures no UNKNOWN in the LAND state
        # or in the receipt diagnostic — so it is asserted on those lines.
        for line in shown.splitlines():
            if line.startswith(("  landed", "  receipt", "LAND REQUEST")):
                self.assertNotIn("UNKNOWN", line)
        self.assertEqual(len([line for line in shown.splitlines()
                              if line.startswith("  receipt   ")]), 1)
        self.assertIn("LANDED", run(["list", "--all"])[1])
        return lr


class ReceiptValidationTest(ReceiptValidationBase):
    """Nothing in the index is trusted, and nothing in it may veto live Git.

    A wrong topic, forged field, or junk transport shape stays visible on the
    receipt-diagnostic axis while an independently observed land remains LANDED.
    Receipt corruption can never manufacture authority in either direction.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses ReceiptValidationBase."""

    def test_a_wrong_topic_row_is_rejected(self):
        rid, rec = self.valid_row()
        self.write_rows(dict(rec, topic="helm.chat"))
        self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "topic is not helm.land")

    def test_forged_fields_that_no_longer_rebind_are_rejected(self):
        # the heart of the binding: keep the SIGNED payload, swap the fields it
        # was signed over. Each forgery must fail to rebind.
        rid, rec = self.valid_row()
        for field, value in (("trunk_sha", "9" * 40), ("patch_id", "b" * 40),
                             ("lane", "lane/other"), ("branch", "seat/other")):
            self.write_rows(dict(rec, **{field: value}))
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "does not rebind")

    def test_a_historical_prefixed_receipt_still_rebinds_as_written(self):  # noqa: VACUOUS_ASSERTION — the positive controls are the assertEquals on ok[lane] (None would raise loudly) and on receipt_state against a baseline pinned NotEqual REJECTED above
        """#142: 29 of the live ledger's 117 signed receipts
        store a lane/-prefixed spelling whose payload was SIGNED over those
        bytes. Replay must validate each stored payload under the spelling
        that was actually signed — recomputing through the write-time
        canonicalizer rejected all 29 live rows ("payload does not rebind
        these fields"). The refactor must superset the dumb version: an old
        row keeps exactly the meaning it was written with."""
        rid, rec = self.valid_row()
        baseline = landreq.get(rid)[0]["receipt_state"]
        self.assertNotEqual(baseline, landreq.R_REJECTED)   # live control
        # The row AS THE OLD WRITER WROTE IT: prefixed lane, payload signed
        # over that prefixed spelling. The historical bytes are FROZEN HERE,
        # never recomputed through land_record/land_payload — a fixture that
        # routes through the current builder would mutate in lockstep with
        # the exact regression it exists to catch (the round-1 mutation
        # matrix proved that: re-adding the strip survived the recomputing
        # version of this test).
        import hashlib
        raw = landreq._RS.join([landreq.LAND_SCHEMA, "lane/" + rec["lane"],
                                rec["branch"], rec["reviewed_tip"],
                                rec["patch_id"], rec["trunk_sha"]])
        frozen = landreq.LAND_TAG + hashlib.blake2b(
            raw.encode("utf-8"), digest_size=32).hexdigest()
        hist = dict(rec, lane="lane/" + rec["lane"], payload=frozen)
        ok, why = landreq._validate_receipt(dict(hist), rec["reviewed_tip"])
        self.assertIsNone(why)
        self.assertEqual(ok["lane"], "lane/" + rec["lane"])
        self.write_rows(hist)
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["receipt_state"], baseline)     # not degraded

    def test_a_forged_reviewed_tip_cannot_speak_for_another_loop(self):
        rid, rec = self.valid_row()
        self.write_rows(dict(rec, reviewed_tip="9" * 40))   # id still the old tip
        self.assert_receipt_diagnostic(rid, landreq.R_REJECTED, "not bound to this row's")

    def test_junk_transport_evidence_is_rejected(self):
        rid, rec = self.valid_row()
        for junk in ({"turn": "junk"}, {"receipt": "b" * 63}, {"chain": "7"},
                     {"chain": -1}, {"chain": True}, {"turn": None}):
            self.write_rows(dict(rec, **junk))
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED,
                                "no committed signing receipt")

    def test_an_unschemad_or_malformed_row_is_rejected(self):
        rid, rec = self.valid_row()
        for bad in (dict(rec, schema="helm.land/1"), dict(rec, schema=None),
                    dict(rec, trunk_sha=("de" * 20)), dict(rec, lane="a\x1eb"),
                    dict(rec, lane="x" * 300), dict(rec, payload=None)):
            self.write_rows(bad)
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED)

    def test_two_receipts_binding_different_lands_to_one_tip_conflict(self):
        rid, rec = self.valid_row()
        second = dict(rec, trunk_sha="9" * 40)
        second["payload"] = landreq.land_payload(
            rec["lane"], rec["branch"], rec["reviewed_tip"], rec["patch_id"],
            "9" * 40)                        # internally consistent, but a RIVAL
        self.write_rows(rec, second)
        self.assert_receipt_diagnostic(rid, landreq.R_CONFLICT, "DIFFERENT lands")

    def test_a_valid_receipt_beside_an_invalid_one_conflicts(self):
        # appending garbage next to a good row must not let the good row be
        # cherry-picked: a contaminated index is refused, not resolved
        rid, rec = self.valid_row()
        self.write_rows(rec, dict(rec, topic="helm.chat"))
        self.assert_receipt_diagnostic(rid, landreq.R_CONFLICT, "both a valid and an invalid")

    def test_an_identical_duplicate_row_is_idempotent_not_a_conflict(self):
        # re-recording the SAME land (a retried `lr land`) binds identically
        rid, rec = self.valid_row()
        self.write_rows(rec, dict(rec))
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["state"], "LANDED")       # Git independently sees it
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_complete_malformed_physical_siblings_poison_strict_replay(self):
        cases = {
            "malformed": b'{not json}\n',
            "idless": b'{}\n',
            "oversize": b'{"id":"x","pad":"' +
                        b'x' * eventledger.MAX_EVENT_BYTES + b'"}\n',
        }
        for name, raw in cases.items():
            rid, _rec = self.valid_row()
            with open(landreq.receipts_path(), "ab") as f:
                f.write(raw)
            self.assert_receipt_diagnostic(rid, landreq.R_REJECTED,
                                "corrupt ledger line")
            os.remove(landreq.receipts_path())

    def test_an_unterminated_final_tail_is_outside_the_durable_boundary(self):
        rid, _rec = self.valid_row()
        with open(landreq.receipts_path(), "ab") as f:
            f.write(b'{not durable yet')
        lr = landreq.get(rid)[0]
        self.assertEqual(lr["state"], "LANDED")       # Git still proves the land
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_record_land_refuses_to_write_a_row_that_fails_its_own_replay(self):
        # what is written always replays: an incomplete signer response cannot
        # become a row that would later be rejected
        with mock.patch.object(chat, "emit_coordination_turn",
                               return_value=({"sent": True, "turn_hash": "a" * 64,
                                              "receipt_hash": "b" * 64,
                                              "chain_index": None}, None)):
            rec, why = landreq.record_land("l", "b", "1" * 40, None, "2" * 40)
        self.assertIsNone(rec)
        self.assertIn("fails its own replay", why)
        self.assertFalse(os.path.exists(landreq.receipts_path()))


class AncestryTriStateTest(ReceiptBase):
    """Ancestry is tri-state, while landedness has a patch-identity fallback."""

    def unanswerable(self, rc=128, hang=False, patch=False):
        """Make ancestry, and optionally patch identity, unanswerable while the
        rest of Git stays real. This separates a fallback answer from merely
        having attempted the fallback."""
        real = landreq._git

        def fake(gitdir, *args, **kw):
            ancestry = args[:2] == ("merge-base", "--is-ancestor")
            # PATCH IDENTITY IS A PROPERTY, NOT A COMMAND. This modelled
            # "patch identity cannot answer" as "cherry is broken", which held
            # while cherry was the only route to that fact. It is no longer:
            # the derive now answers the same question from a batched upstream
            # patch-id index (git show | git patch-id) because asking cherry
            # per row recomputed the whole symmetric difference 1301 times in
            # one inject. Breaking only cherry left the second route working,
            # so the fixture stopped achieving its own stated intent and the
            # arm silently became "one route is broken" instead of "the fact
            # is unavailable". Both routes are cut here.
            patch_probe = patch and args[:1] in (
                ("cherry",), ("show",), ("patch-id",), ("log",))
            if ancestry or patch_probe:
                if hang:
                    return None                 # a timeout/OSError
                return subprocess.CompletedProcess(
                    args, rc, "", "fatal: Not a valid object name")
            return real(gitdir, *args, **kw)
        return mock.patch.object(landreq, "_git", side_effect=fake)

    def test_the_three_cells_are_distinct(self):
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                         landreq.ANCESTOR)              # rc 0
        self.assertEqual(landreq._ancestry(gitdir, self.side, trunk),
                         landreq.NOT_ANCESTOR)          # rc 1
        with self.unanswerable(rc=128):
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.UNDETERMINED)      # rc 128
        with self.unanswerable(hang=True):
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.UNDETERMINED)      # timeout / OSError
        # a missing gitdir/commit/ref is undeterminable, never a negative
        self.assertEqual(landreq._ancestry(gitdir, self.b, None),
                         landreq.UNDETERMINED)
        self.assertEqual(landreq._ancestry(None, self.b, trunk),
                         landreq.UNDETERMINED)

    def test_unanswerable_ancestry_falls_through_to_patch_identity(self):
        # rc128/timeout makes reachability unknown, not the whole landing question.
        # The readable per-commit patch comparison still proves this side commit is
        # absent, so READY remains observable and billable.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        for cell in (dict(rc=128), dict(hang=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertTrue(lr["observable"])
                self.assertEqual(lr["state"], "READY")
                self.assertTrue(lr["stalled"])

    def test_unanswerable_ancestry_and_patch_identity_stays_unobservable(self):
        # A fallback ATTEMPT is not a fallback ANSWER. If neither reachability nor
        # patch identity can answer, folding the pair into False manufactures a
        # READY stall from evidence Git never gave.
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        for cell in (dict(rc=128, patch=True),
                     dict(hang=True, patch=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertEqual(lr["state"], "READY")
                self.assertFalse(lr["observable"])
                self.assertFalse(lr["stalled"])
                self.assertEqual(landreq.stalls()[0], [])

    def test_patch_identity_not_local_receipt_proves_land_when_ancestry_is_unknown(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        # Land under a different object id so the independent patch-identity
        # fallback has an actual '-' answer even while ancestry is unavailable.
        self.git("cherry-pick", self.side)
        trunk = self.git("rev-parse", self.main)
        self.record(self.side, trunk)
        for cell in (dict(rc=128), dict(hang=True)):
            with self.unanswerable(**cell):
                lr = landreq.get(row["id"])[0]
                self.assertEqual(lr["state"], "LANDED")
                self.assertTrue(lr["observable"] and lr["landed"])
                self.assertFalse(lr["receipt"])
                self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)

    def test_an_unreadable_index_is_the_git_floor_never_worse(self):
        # the index is an ENHANCEMENT: if it cannot be read at all, observation
        # is exactly today's git behavior (it can never manufacture a LANDED).
        # It is also no longer SILENT: the index answers a marker naming the
        # failure, so the diagnostic says "could not look" instead of "none".
        row = self.dispatch(ref=self.side)
        self.mark_verdict(
            row["id"], self.side, "ok", polarity="approve")
        self.land_side()
        with mock.patch.object(eventledger, "checked_events",
                               return_value=([], "PermissionError: denied")):
            index = landreq._receipts_by_tip()
            self.assertEqual(list(index.values()), ["PermissionError: denied"])
            self.assertEqual(landreq._receipt_for(self.side, index)[0],
                             landreq.R_UNREADABLE)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")        # git saw the merge
        self.assertFalse(lr["receipt"])


class UnsignedFieldMayNotStrengthenTest(ReceiptBase):
    """No field outside the stored payload may affect lifecycle state."""

    def _rec_with(self, **changes):
        tip = "a" * 40
        self.record(tip, self.git("rev-parse", "HEAD"))
        row = dict(self.rows()[-1], **changes)
        self.write_rows(row)
        return tip, landreq._observe(
            self.gitdir(), tip, {}, receipts=landreq._receipts_by_tip())

    def test_flipping_either_unsigned_field_changes_no_observation(self):
        keys = ("observable", "local", "upstream", "has_upstream", "receipt")
        for field in ("upstream", "has_upstream"):
            _tip, a = self._rec_with(**{field: False})
            _tip, b = self._rec_with(**{field: True})
            self.assertEqual({k: a[k] for k in keys},
                             {k: b[k] for k in keys}, field)
            self.assertEqual(a["receipt_state"], landreq.R_LOCAL)
            self.assertIn("payload binding unavailable", a["receipt_reason"])

    def test_live_git_upstream_ancestry_still_grants_landed(self):
        self.add_origin()
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        trunk = self.land_side()
        self.record(self.side, trunk, has_upstream=True, upstream=False)
        self.assertEqual(landreq.get(row["id"])[0]["state"], "MERGED_LOCAL")
        self.git("push", "-q", "origin", self.main)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "LANDED")
        self.assertFalse(lr["receipt"])             # authority came from Git


class ReceiptStateNameIsHonestTest(ReceiptBase):
    """Local self-consistency must be named honestly and remain non-authority."""

    def test_the_wire_value_cannot_be_mistaken_for_attestation(self):
        self.assertEqual(landreq.R_LOCAL, "local-unverified")
        self.assertFalse(hasattr(landreq, "R_VALID"),
                         "the old misleading name must be GONE, not aliased "
                         "beside its successor")

    def test_a_handwritten_self_consistent_row_cannot_manufacture_landed(self):
        tip = "0" * 40
        self.landed_row(tip)
        trunk = self.git("rev-parse", self.main)
        row = {"id": tip, "schema": landreq.LAND_SCHEMA,
               "topic": landreq.LAND_TOPIC,
               "payload": landreq.land_payload(
                   "lane/pruned", "seat/pruned", tip, "e" * 40, trunk),
               "reviewed_tip": tip, "lane": "lane/pruned",
               "branch": "seat/pruned", "patch_id": "e" * 40,
               "trunk_sha": trunk, "repo_id": self.gitdir(),
               "has_upstream": False, "upstream": False,
               "turn": "a" * 64, "receipt": "b" * 64, "chain": 7,
               "profile": "", "ts": "2026-07-01T00:00:00Z"}
        self.write_rows(row)
        lr = landreq.get("aabbccdd")[0]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["landed"] or lr["receipt"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])

    def test_local_receipt_cannot_erase_a_repo_less_legacy_loop(self):
        tip, rid = "0" * 40, "aabbccdd"
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3",
               "lane": "lane/pruned", "ref": tip, "tip": tip,
               "note": None, "deadline_s": 60, "source": "old",
               "status": "verdict", "verdict_ref": "CLEAR",
               "reviewed_tip": tip, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        self.record(tip, self.git("rev-parse", self.main), repo_id=None)

        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable)
        self.assertIn(rid, lrs)
        lr = lrs[rid]
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertFalse(lr["observable"] or lr["landed"])
        self.assertEqual(lr["receipt_state"], landreq.R_LOCAL)
        self.assertIn("payload binding unavailable", lr["receipt_reason"])


class ReceiptFieldShapesTest(unittest.TestCase):
    """Blockers (4) and (5) — two validators that had stopped
    validating."""

    def test_impossible_sha_lengths_are_refused(self):
        for n in (40, 64):
            self.assertTrue(landreq._SHA.match("a" * n), "%d must be valid" % n)
        for n in (39, 41, 50, 63, 65):
            self.assertIsNone(landreq._SHA.match("a" * n),
                              "%d is not a git object name" % n)

    def test_a_lone_surrogate_is_refused_not_raised(self):
        """It passed the field check and then made the payload's UTF-8 encode
        RAISE, upstream of every fail-open, so record_land CRASHED instead of
        refusing. A validator whose rejection path is an exception in someone
        else's frame is not a validator."""
        self.assertIsNone(landreq._field("lane\ud800name"))
        self.assertEqual(landreq._field("ordinary/lane"), "ordinary/lane")


class CherryPickedLandTest(LandReqBase):
    """Landed-ness must be read from the PATCH, not from reachability alone.

    The integrator's stated rule is to land the single gated commit onto
    current trunk rather than merge the branch — landing less than was gated
    is safe, landing more never is. That means the landed change carries a
    NEW sha and the reviewed one stays reachable from nothing, so an
    ancestry-only test reports the loop as still awaiting a land that already
    happened. Forever: no later event makes the old sha reachable.

    It is the same defect as deriving READY from "a verdict exists", one
    state further along — a terminal fact inferred from a proxy that does not
    carry it. Measured on this repo: delim was gated at bc3ef8e,
    landed as 964f06f, and `helm lr` showed the loop READY while the patch
    was demonstrably on trunk.
    """

    def gitdir(self):
        """What the ledger stores as repo_id: `rev-parse --git-common-dir`,
        NOT the worktree root. Passing the root makes every git call fail and
        every answer read False — which is how the first cut of these tests
        "proved" the fix while actually exercising nothing."""
        return os.path.realpath(os.path.join(self.repo, ".git"))

    def cherry_pick_onto_trunk(self, sha):
        """Land it the way the integrator actually lands: a new sha, same patch."""
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", sha)
        landed = self.git("rev-parse", "HEAD")
        self.assertNotEqual(landed, sha, "the whole point is a DIFFERENT sha")
        return landed

    def test_a_cherry_picked_land_is_SEEN(self):
        from helm import landreq
        self.cherry_pick_onto_trunk(self.side)
        self.assertFalse(
            landreq._is_ancestor(self.gitdir(), self.side, self.main),
            "precondition: ancestry must MISS it, or this proves nothing")
        self.assertTrue(landreq._landed(self.gitdir(), self.side, self.main))

    def test_the_loop_leaves_READY_once_its_patch_is_on_trunk(self):
        """End to end through the lifecycle, which is where it actually bit."""
        from helm import landreq
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "approved",
                                polarity="approve")
        self.cherry_pick_onto_trunk(self.side)
        lr = landreq.get(row["id"])[0]
        self.assertNotEqual(lr["state"], "READY",
                            "a landed loop parked in READY is the zombie")
        # no origin in this fixture, so local trunk IS the end of the road
        self.assertEqual(lr["state"], "LANDED")

    def test_with_an_upstream_it_reads_MERGED_LOCAL_not_READY(self):
        """The same land, one repo shape over: on local trunk but not yet
        pushed. Still not READY — READY means nobody has landed it at all,
        and saying that about landed work sends the lander after their own
        finished job."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "approved",
                                polarity="approve")
        self.add_origin()
        self.cherry_pick_onto_trunk(self.side)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "MERGED_LOCAL")

    def test_a_plain_merge_still_reads_landed_via_the_FAST_path(self):
        """Ancestry stays decisive and cheap when it is true; the patch-id
        compare is only a fallback, never a replacement."""
        from helm import landreq
        self.git("checkout", "-q", self.main)
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assertTrue(landreq._is_ancestor(self.gitdir(), self.side, self.main))
        self.assertTrue(landreq._landed(self.gitdir(), self.side, self.main))

    def test_an_UNLANDED_commit_is_still_not_landed(self):
        """The dangerous direction. A landed-ness check that over-matches is
        worse than one that under-matches: it retires a live obligation."""
        from helm import landreq
        self.assertFalse(landreq._landed(self.gitdir(), self.side, self.main))

    def test_a_DIFFERENT_change_is_never_mistaken_for_this_one(self):
        """Patch-id equality is the claim; make sure it is really equality
        and not 'something landed around then'."""
        from helm import landreq
        self.git("checkout", "-q", self.main)
        self.commit("an unrelated trunk change", path="unrelated")
        self.assertFalse(landreq._landed(self.gitdir(), self.side, self.main))

    def test_an_unreadable_object_never_INVENTS_a_land(self):
        """Unobservable stays unknown: it may neither close the obligation nor
        manufacture a billable negative."""
        self.assertIsNone(landreq._landed(
            self.gitdir(), "0" * 40, self.main))
        self.assertIsNone(landreq._landed(
            self.gitdir(), "not-a-sha", self.main))


class RangeWalkAmbiguityTest(LandReqBase):
    """`git cherry` answers about a RANGE; landed-ness is asked per COMMIT.

    Raised by the integrator while gating the fix, as the specific way it
    could be right in intent and wrong in implementation: `git cherry <ref>
    <tip>` lists every commit from the merge-base forward and its FIRST LINE
    IS THE OLDEST, so an implementation that reads line one answers about a
    different commit than the one it was asked about — and would do it
    silently, with a plausible boolean.

    Their own confirmation hit exactly this: `git cherry HEAD 7ad25f9` over a
    15-commit stack reported 2 landed and 13 not, an aggregate that answers
    nothing about 7ad25f9 itself.

    The fix scans for the line whose sha matches the resolved commit. This
    pins that, because the reasoning is not visible from the call site and
    the next person to touch the parse will not have this conversation.
    """

    def stack_with_only_the_middle_landed(self):
        self.git("checkout", "-q", "-b", "stack", self.main)
        shas = {}
        for name in ("first", "middle", "last"):
            shas[name] = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        self.git("cherry-pick", shas["middle"])
        return shas

    def test_the_answer_is_per_COMMIT_not_per_range(self):
        shas = self.stack_with_only_the_middle_landed()
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        # the precondition that makes this a real test: the first line of the
        # cherry output is NOT the commit we ask about
        out = subprocess.run(
            ["git", "--git-dir", gitdir, "cherry", self.main, shas["last"]],
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertGreater(len(out), 1, "need a multi-commit range")
        self.assertIn(shas["first"], out[0], "first line must be the OLDEST")

        self.assertFalse(landreq._landed(gitdir, shas["first"], self.main))
        self.assertTrue(landreq._landed(gitdir, shas["middle"], self.main))
        self.assertFalse(landreq._landed(gitdir, shas["last"], self.main))

    def test_reading_line_one_would_FAIL_this(self):
        """Names the mutation the test exists to kill, so a future 'simplify'
        of the parse into `out[0].startswith("-")` cannot pass quietly."""
        shas = self.stack_with_only_the_middle_landed()
        gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        out = subprocess.run(
            ["git", "--git-dir", gitdir, "cherry", self.main, shas["last"]],
            capture_output=True, text=True, check=True).stdout.splitlines()
        line_one_says = out[0].split()[0] == "-"
        self.assertFalse(line_one_says)                       # it says '+'
        self.assertTrue(landreq._landed(gitdir, shas["middle"], self.main))
        self.assertNotEqual(line_one_says,
                            landreq._landed(gitdir, shas["middle"], self.main))


class UndeclaredCloseLandedTest(LandReqBase):
    """Proof-based terminal closure preserves the missing verdict direction."""

    def reviewed(self, ref=None):
        row = self.dispatch(ref=ref or self.b)
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": row["seq"] + 1,
            "id": row["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": ref or self.b, "verdict_ref": "legacy review"}))
        return row

    def legacy_unbound(self, tip=None, rid="aabbccdd"):
        tip = tip or self.b
        ts = "2026-07-01T00:00:00Z"
        row = {"id": rid, "ts": ts, "recipient": "codex-3",
               "lane": "lane/legacy", "ref": tip, "tip": tip,
               "note": None, "deadline_s": 60, "source": "old",
               "status": "verdict", "verdict_ref": "reviewed",
               "reviewed_tip": tip, "last_updated": ts}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), row))
        return rid

    def test_ancestor_close_preserves_undeclared_verdict_and_retires_loop(self):
        row = self.reviewed()
        lr, why = landreq.close_landed(
            row["id"], trunk="refs/heads/" + self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["state"], "REVIEWED")
        self.assertIsNone(lr["polarity"])
        self.assertEqual(lr["close_reason"], "landed")
        self.assertTrue(lr["terminal"])
        self.assertEqual(lr["close_proof_mode"], "ancestor")
        self.assertEqual(lr["timeline"][-1]["state"], "CLOSED_LANDED")
        self.assertEqual(landreq.loops()[0], [])
        self.assertEqual(landreq.unmeasurable()[0], [])

    def test_patch_equivalent_close_records_the_proof_mode(self):
        row = self.reviewed(ref=self.side)
        self.git("cherry-pick", self.side)
        self.assertFalse(landreq._is_ancestor(
            os.path.realpath(os.path.join(self.repo, ".git")),
            self.side, "refs/heads/" + self.main))
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["close_proof_mode"], "patch-equivalent")
        self.assertTrue(lr["terminal"])

    def test_absent_or_unknown_change_refuses_without_appending(self):
        row = self.reviewed(ref=self.side)
        before = len(dispatches.history(row["id"]))
        _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("neither an ancestor", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)
        with mock.patch.object(landreq, "_landing_proof",
                               return_value="unknown"):
            _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("could not prove", why)
        self.assertEqual(len(dispatches.history(row["id"])), before)

    def test_unbound_legacy_row_requires_explicit_repo_then_records_it(self):
        rid = self.legacy_unbound()
        _lr, why = landreq.close_landed(rid, trunk=self.main, live=True)
        self.assertIn("pass --repo", why)
        lr, why = landreq.close_landed(
            rid, repo=self.repo, trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["closing_repo_id"],
                         os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertIsNone(lr["repo_id"])

    def test_bound_row_rejects_a_different_repo_override(self):
        row = self.reviewed()
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        _lr, why = landreq.close_landed(
            row["id"], repo=other, trunk=self.main, live=True)
        self.assertIn("refusing cross-repository proof", why)

    def test_declared_or_open_rows_refuse_close(self):
        # the alias delegates to close --reason landed, whose domain admits
        # approve rows (proof-refused here: the tip never landed) and refuses
        # FIX/SUPERSEDE rows as contrary debt
        row = self.dispatch(lane="lane/approve")
        self.mark_verdict(row["id"], self.side, "review",
                                polarity="approve")
        _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIn("neither an ancestor", why)
        for polarity in ("fix", "supersede"):
            row = self.dispatch(lane="lane/" + polarity)
            self.mark_verdict(row["id"], self.side, "review",
                                    polarity=polarity)
            _lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
            self.assertIn("CONTRARY, not a resolution", why)
        open_row = self.dispatch(lane="lane/open")
        _lr, why = landreq.close_landed(open_row["id"], trunk=self.main, live=True)
        self.assertIn("has no verdict", why)

    def test_trunk_is_explicit_named_and_frozen(self):
        row = self.reviewed()
        _lr, why = landreq.close_landed(row["id"], trunk=self.b, live=True)
        self.assertIn("not an object id", why)
        lr, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(lr["closing_trunk_ref"], "refs/heads/" + self.main)
        self.assertEqual(lr["closing_trunk_sha"],
                         self.git("rev-parse", self.main))
        # an UNBOUND row still requires the explicit trunk (a bound one may
        # use the discharge trio through close --reason landed)
        rid = self.legacy_unbound(tip=self.c, rid="ddeeff00")
        _lr, why = landreq.close_landed(rid, repo=self.repo, live=True)
        self.assertIn("--trunk is required", why)

    def test_historical_close_survives_trunk_movement_without_reobserving_git(self):
        row = self.reviewed()
        first, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        anchor = first["closing_trunk_sha"]
        self.commit("trunk moved")
        again, why = landreq.close_landed(row["id"], trunk=self.main, live=True)
        self.assertIsNone(why)
        self.assertEqual(again["closing_trunk_sha"], anchor)
        # BOUND TO THE GIT READ ITSELF, NOT TO A FUNCTION NAME. The claim is
        # that a historical closure consults no live repository, and `_git` is
        # the one place this module spawns one — so no future route into the
        # same subprocess can satisfy this by being called something else.
        #
        # `_observe` IS called for such a row now, with git=False. It carries
        # the land-receipt diagnostic as well as the git observation, and that
        # half is a lookup in an index project() has already read; skipping the
        # whole function to avoid the git half is what made an UNREADABLE
        # receipt ledger render as "land receipt: none" on exactly these rows.
        with mock.patch.object(landreq, "_git") as git, \
                mock.patch.object(landreq, "_git_observe") as observe:
            replayed = landreq.get(row["id"])[0]
        git.assert_not_called()
        observe.assert_not_called()
        self.assertEqual(replayed["close_reason"], "landed")
        self.assertTrue(replayed["terminal"])
        self.assertEqual(replayed["closing_trunk_sha"], anchor)

    def test_surfaces_never_imply_approval(self):
        row = self.reviewed()
        landreq.close_landed(row["id"], trunk=self.main, live=True)
        lr = landreq.get(row["id"])[0]
        text = landreq._render_show(lr)
        self.assertIn("REVIEWED (UNDECLARED) — CLOSED (LANDED)", text)
        self.assertNotIn("APPROVED", text)
        self.assertIn(lr, landreq.loops(include_landed=True)[0])
        self.assertNotIn(lr, landreq.loops()[0])
        self.assertEqual(landreq._unmeasurable_rows({lr["id"]: lr}), [])

    def test_cli_close_landed_json_and_usage(self):
        row = self.reviewed()
        rc, out, err = run(["close-landed", row["id"][:12], "--trunk",
                            self.main, "--live", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated: use helm lr close --reason landed", err)
        self.assertEqual(json.loads(out)["close_reason"], "landed")
        for args in (["close-landed", row["id"]],
                     ["close-landed", row["id"], "--trunk"],
                     ["close-landed", row["id"], "--trunk", self.main,
                      "--trunk", self.main],
                     ["close-landed", row["id"], "--wat", "x",
                      "--trunk", self.main]):
            self.assertEqual(run(args)[0], 2)


class OwedByTest(LandReqBase):
    """The fleet-stall root cause: OPEN billed the REVIEWER, not the
    INTEGRATOR — a seat was billed for work it didn't know existed."""

    def test_open_is_owed_by_the_integrator_not_the_reviewer(self):
        """An obligation that was created but whose delivery was never confirmed
        is the INTEGRATOR's to push through, never the reviewer's to chase."""
        row = self.dispatch()
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertEqual(lr["owed_by"], "integrator")

    def test_awaiting_review_is_owed_by_the_reviewer(self):
        """Once delivery IS observed, the reviewer knows. That is when their
        clock starts and the obligation transfers."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")

    def test_open_is_not_assertable_as_a_stall(self):
        """OPEN has no billable clock — you cannot stall on work you don't know
        exists. The integrator's silence is the failure, but the reviewer's
        bill is the symptom."""
        row = self.dispatch(deadline_s=60)
        self.age(row["id"], 7200)
        lr = landreq.get(row["id"])[0]
        self.assertFalse(lr["stalled"])
        self.assertIsNone(lr["stall_threshold_s"])

    def test_a_verdict_disputed_polarity_is_owed_by_the_author(self):
        """A FIX verdict returns the obligation to the AUTHOR, not the reviewer."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        self.mark_verdict(row["id"], self.side, "needs work",
                                polarity="fix")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["owed_by"], "author")

    def test_every_owed_by_state_names_someone(self):
        for state, owed in landreq.OWED_BY.items():
            self.assertTrue(owed, "%s -> %r" % (state, owed))

    def test_open_rows_are_not_in_the_stalls_subset(self):
        """OPEN rows are live obligations, not stalled ones."""
        row = self.dispatch()
        self.age(row["id"], 7200)
        stalled = landreq.stalls()[0]
        self.assertEqual(stalled, [])

    def test_forward_progress_out_of_open_is_owed_by_the_reviewer(self):
        """Delivery observed → the obligation transfers from integrator to
        reviewer. The row never passes through a state where nobody cares."""
        row = self.dispatch()
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertEqual(lr["owed_by"], "reviewer")

    def test_dispatch_send_posts_a_public_at_mention(self):
        """The notification leg: a dispatch at-mentions the reviewer in main,
        so the beacon wakes on the mention. The fleet-stall root cause was
        that nothing told the reviewer a review was owed."""
        from unittest import mock
        patched = mock.patch.object(chat, "post", return_value={
            "id": "test-post", "ts": "2020-01-01T00:00:00Z"})
        with patched as fake_post:
            row = self.dispatch()
        # The DM goes to the private lane via seats.dm; the @mention goes to
        # main via chat.post in dispatches.send(). Both happen in send(), not
        # in add(), so check that they are reachable.
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["id"])


class NotifyPersistenceTest(LandReqBase):
    """Three-state persistence: told / never-told / told-but-delivery-failed.
    The r4 P0: _record_notify_failed was 100% inert (called pk.write_json with
    a nonexistent append parameter, swallowed the TypeError silently). The
    test that proved "green" checked for no exception — which tests the handler,
    not the durability. These tests assert the OBSERVABLE EFFECT."""

    def test_never_told_OPEN_has_no_notify_failed_marker(self):
        """A fresh dispatch that was never sent has no notify record."""
        row = dispatches.add("codex-3", "lane/fresh", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        self.assertIsNotNone(row)
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "OPEN")
        self.assertIsNone(lr["notify_failed"])

    def test_send_succeeds_no_notify_failed_marker(self):
        """When notification succeeds, no failure marker is written."""
        row = dispatches.add("codex-3", "lane/sent", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertIsNone(lr["notify_failed"])

    def test_notify_failed_is_persisted_and_readable(self):
        """When notification fails, the event is DURABLE and visible in the
        lifecycle. This is the test r3 couldn't pass — _record_notify_failed
        was silently inert."""
        row = dispatches.add("codex-3", "lane/failed", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row["id"], "test failure")
        lr = landreq.get(row["id"])[0]
        self.assertIsNotNone(lr["notify_failed"],
                             "notify_failed must be a dict, not None — "
                             "the durability leg must WORK")
        self.assertEqual(lr["notify_failed"]["id"], row["id"])
        self.assertEqual(lr["notify_failed"]["event"], "notify-failed")
        self.assertEqual(lr["notify_failed"]["reason"], "test failure")

    def test_notify_failed_does_not_leak_to_other_rows(self):
        """The marker is keyed by row id — a different dispatch never sees it."""
        row_a = dispatches.add("codex-3", "lane/a", ref=self.side,
                               repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row_a["id"], "failed for a")
        row_b = dispatches.add("codex-3", "lane/b", ref=self.side,
                               repo=self.repo, notify=False, new_work=True)
        lr_b = landreq.get(row_b["id"])[0]
        self.assertIsNone(lr_b["notify_failed"],
                          "an unrelated dispatch must never read another's "
                          "notify-failed marker")

    def test_delivered_rows_dont_read_notify_failed(self):
        """Once delivery is observed, the notify-failed status is moot —
        the DM got through regardless."""
        row = dispatches.add("codex-3", "lane/delivered", ref=self.side,
                             repo=self.repo, notify=False, new_work=True)
        dispatches._record_notify_failed(row["id"], "was failing")
        dispatches._mark_delivered(row["id"], "post-1")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr["state"], "AWAITING_REVIEW")
        self.assertIsNone(lr["notify_failed"])


LAND_DEL_TEST = 6    # tracked files in the helper's HEAD fixture


class LandDeletionAckTest(LandReqBase):
    """A merge that deletes TRACKED files from a remote REFUSES by default.
    --ack-deletions is the explicit authorization. UNKNOWN fails safe."""

    def setUp(self):
        super().setUp()
        self.gitdir = os.path.realpath(os.path.join(self.repo, ".git"))
        # Plant tracked files so HEAD has a known tracked set
        for i in range(LAND_DEL_TEST):
            path = os.path.join(self.repo, "tracked_%d.md" % i)
            with open(path, "w") as f:
                f.write("tracked file %d\n" % i)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "adding tracked files")
        self.head = self.git("rev-parse", "HEAD")
        self.trunk = "refs/heads/" + self.main

    def rcommit(self, text, files_to_delete=(), branch=None):
        """Commit deletion of named tracked files, optionally on a branch."""
        if branch:
            self.git("checkout", "-b", branch)
        for f in files_to_delete:
            os.unlink(os.path.join(self.repo, f))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def test_no_deletion_returns_empty(self):
        """A merge that deletes nothing returns empty (not an error)."""
        from helm import landreq
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, self.head)
        self.assertIsNone(err)
        self.assertEqual(paths, [])

    def test_tracked_deletions_are_detected(self):
        """Three tracked files deleted in the merge range."""
        from helm import landreq
        tip = self.rcommit("delete 3 tracked", ("tracked_0.md",
                                                "tracked_1.md",
                                                "tracked_2.md"),
                           branch="side-del-3")
        self.git("checkout", self.main)
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, tip)
        self.assertIsNone(err)
        self.assertEqual(len(paths), 3)
        for p in paths:
            self.assertIn("tracked_", p)

    def test_untracked_deletions_are_not_counted(self):
        """An untracked file deleted is not in ls-files -> not counted."""
        from helm import landreq
        # Delete a tracked file so diff is non-empty
        tip = self.rcommit("delete 1 tracked", ("tracked_5.md",),
                           branch="side-del-untracked")
        self.git("checkout", self.main)
        paths, err = landreq._tracked_deletions(
            self.gitdir, self.trunk, tip)
        self.assertIsNone(err)
        # Only tracked_5.md should appear
        self.assertEqual(len(paths), 1)
        self.assertIn("tracked_5.md", paths[0])

    def test_missing_repo_returns_error_not_empty(self):
        """UNKNOWN fails SAFE — missing gitdir returns err, not empty."""
        from helm import landreq
        paths, err = landreq._tracked_deletions(
            "/nonexistent/path", "main", "sha")
        self.assertIsNotNone(err)
        self.assertIsNone(paths)

    def test_deletion_refusal_text(self):
        """Count, paths, flag, and CURATIVE message all present."""
        from helm import landreq
        r = landreq._deletion_refusal(
            ["docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md"])
        self.assertIn("deletes 4 TRACKED files", r)
        self.assertIn("docs/a.md", r)
        self.assertIn("+1 more TRACKED file", r)
        self.assertIn("--ack-deletions", r)

    def test_the_flag_the_refusal_demands_is_accepted_by_the_parser(self):
        """The refusal's own cure must be reachable THROUGH THE CLI. For as
        long as --ack-deletions was missing from guard_tail's flags tuple,
        `helm lr land <id>` refused with rc 1 telling the operator to pass
        --ack-deletions, and `helm lr land <id> --ack-deletions` returned
        rc 2 as an unknown argument — the cure the refusal named was
        unpronounceable, so a deletion-bearing merge could never be
        witnessed from the CLI at all. Every prior test here called the
        helpers directly, so the CLI seam carried no pin.

        Found by a read-only code-ground audit of the terminal paths."""
        tip = self.rcommit("delete 1 tracked", ("tracked_3.md",),
                           branch="side-del-cli")
        self.git("checkout", self.main)
        row = self.dispatch(ref=tip)
        self.mark_verdict(row["id"], tip, "ok", polarity="approve")
        # Without the flag: the refusal fires and NAMES the cure.
        rc, out, err = run(["land", row["id"]])
        text = out + err
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("--ack-deletions", text)
        # With the flag: the parser must ACCEPT it (the bug returned rc 2
        # usage-error here) and the land proceeds to the witness path, which
        # is fail-open with no signer in this environment.
        rc, out, err = run(["land", row["id"], "--ack-deletions"])
        text = out + err
        self.assertNotEqual(rc, 2, "parser rejected the refusal's own cure: "
                            + text)
        self.assertEqual(rc, 0, text)
        self.assertIn("NO MERGE PERFORMED", text)
        self.assertNotIn("REFUSED", text)

    def test_deletion_refusal_plural_is_correct(self):
        """One file: 'deletes 1 TRACKED file' (no plural)."""
        from helm import landreq
        r = landreq._deletion_refusal(["one.md"])
        self.assertIn("deletes 1 TRACKED file", r)
        self.assertNotIn("files", r.split("deletes")[1])
        self.assertNotIn("+", r)


class ChainFoldingTest(unittest.TestCase):
    """The projection accounted per ROW while the unit of work is the CHAIN.

    Three consumers had the same defect. `_stalled_rows` billed a superseded
    round's dwell to an author who had nothing to do — 21 of 51 live stalled
    rows were predecessors, rolling up to ten real debts, and no
    close ladder could retire them. `_unmeasurable_rows` read only the head's
    empty gate while a successor carried a verified one at the same reviewed
    tip. The primary list later repeated the per-row mistake, showing seven
    relieved predecessors as fresh integrator debt. One validated walk over the
    supersedes edge answers all three."""

    def row(self, rid, **kw):
        lr = {"id": rid, "stalled": False, "terminal": False, "dwell_s": 60,
              "state": "CHANGES_REQUESTED", "supersedes": None, "polarity": None,
              "ungated": None, "gate": "", "reviewed_tip": None,
              "owed_by": "author"}
        lr.update(kw)
        return lr

    def board(self, *rows, **kw):
        """(projected lrs, RAW ledger view). The forest is built from the RAW
        rows because project() drops cancelled ones while the chain grammar
        permits a successor of a cancelled parent — so a fixture that only
        ever hands over `lrs` cannot express the transit case at all."""
        lrs = {r["id"]: r for r in rows if not r.get("_cancelled")}
        # REPLAY-LEGAL BY CONSTRUCTION. A real snapshot row carries id and v
        # and has already been through dispatches._replay_chain, so chain_root
        # is exactly id | None | CHAIN_UNKNOWN — never "", never blank, never
        # a non-str. A fixture that can build those shapes invites arms for a
        # world production cannot produce.
        raw = {r["id"]: {"id": r["id"],
                         "v": r.get("v", 3),
                         "supersedes": r.get("supersedes"),
                         "chain_root": r.get("chain_root", "ROOT"),
                         "status": "cancelled" if r.get("_cancelled") else "open"}
               for r in rows}
        raw.update(kw.get("raw_extra") or {})
        return lrs, raw

    def stalled_ids(self, board):
        lrs, raw = board
        return sorted(r["id"] for r in landreq._stalled_rows(lrs, raw))

    def loop_ids(self, board, include_landed=False):
        lrs, raw = board
        return sorted(r["id"] for r in
                      landreq._loop_rows(lrs, raw, include_landed))

    # ---- site 0: the primary board ------------------------------------

    def test_default_loops_show_the_carrying_frontier_not_its_predecessor(self):  # noqa: VACUOUS_ASSERTION — exact non-empty [child] output proves the classifier ran while simultaneously excluding the parent; an always-empty filter fails
        parent = self.row("p", state="CHANGES_REQUESTED", owed_by="integrator")
        child = self.row("c", supersedes="p", state="READY", owed_by="lander")
        board = self.board(parent, child)
        self.assertEqual(self.loop_ids(board), ["c"])
        # Historical mode retains both exact rows and their independent facts.
        self.assertEqual(self.loop_ids(board, include_landed=True), ["c", "p"])
        self.assertFalse(parent["terminal"])
        self.assertNotIn("close_reason", parent)

    def test_a_landed_successor_removes_the_predecessor_not_a_bystander(self):
        parent = self.row("p", owed_by="integrator")
        landed = self.row("c", supersedes="p", state="LANDED", terminal=True)
        other = self.row("bystander")
        self.assertEqual(self.loop_ids(self.board(parent, landed, other)),
                         ["bystander"])

    def test_a_CONTRARY_row_is_not_relieved_by_a_merely_LIVE_successor(self):
        """An alarm is not debt. `contrary` says changes were requested and the
        change reached trunk ANYWAY; a live successor means somebody is holding
        the work, which un-lands nothing. Folding it trades a standing safety
        alarm for an intention — and the alarm is the whole reason the row is
        loud."""
        parent = self.row("p", contrary=True, owed_by="integrator")
        child = self.row("c", supersedes="p", state="AWAITING_BUILD")
        self.assertEqual(self.loop_ids(self.board(parent, child)), ["c", "p"])
        # THE CONTROL, same topology minus the alarm: ordinary debt still folds,
        # so this is a carve-out and not a disabled classifier.
        plain = self.row("p", owed_by="integrator")
        self.assertEqual(self.loop_ids(self.board(plain, child)), ["c"])

    def test_a_CONTRARY_row_IS_relieved_once_a_descendant_LANDS(self):
        """The alarm's subject is trunk, so trunk resolves it. A descendant
        that actually landed carries the contrary condition away; anything
        short of landing does not."""
        parent = self.row("p", contrary=True, owed_by="integrator")
        landed = self.row("c", supersedes="p", state="LANDED", terminal=True)
        other = self.row("bystander")
        self.assertEqual(self.loop_ids(self.board(parent, landed, other)),
                         ["bystander"])

    def test_CONTRARY_relief_is_found_THROUGH_a_live_intermediate_round(self):
        """The landed-relief rule reads the whole SUBTREE, not just the
        immediate child: a contrary row whose GRANDCHILD landed is resolved,
        even though the round between them is still open. The intermediate
        round folds too, on the ordinary rule — the bystander is the control
        that proves the classifier still emitted rows rather than collapsing
        to an empty answer."""
        parent = self.row("p", contrary=True, owed_by="integrator")
        middle = self.row("m", supersedes="p", state="AWAITING_BUILD")
        landed = self.row("g", supersedes="m", state="LANDED", terminal=True)
        other = self.row("bystander")
        self.assertEqual(
            self.loop_ids(self.board(parent, middle, landed, other)),
            ["bystander"])

    def test_every_dead_branch_leaves_the_nearest_unresolved_ancestor(self):
        parent = self.row("p")
        abandoned = self.row("a", supersedes="p", state="ABANDONED",
                             terminal=True)
        stranded = self.row("s", supersedes="p", state="REVIEWED",
                            terminal=True, close_reason="stranded")
        self.assertEqual(
            self.loop_ids(self.board(parent, abandoned, stranded)), ["p"])

    def test_default_loops_traverse_cancelled_transit_to_the_live_frontier(self):
        parent = self.row("p")
        cancelled = self.row("x", supersedes="p", _cancelled=True)
        grandchild = self.row("g", supersedes="x")
        self.assertEqual(
            self.loop_ids(self.board(parent, cancelled, grandchild)), ["g"])

    def test_loop_topology_failure_reaches_the_public_surface(self):
        a = self.row("a", supersedes="b")
        b = self.row("b", supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            rows, unavailable = landreq.loops()
        self.assertIsNone(rows)
        self.assertIn("cycle", unavailable)

    def test_board_section_uses_the_same_frontier_as_the_list(self):
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", stalled=True)
        lrs, raw = self.board(parent, child)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)), \
                mock.patch.object(landreq, "card", side_effect=dict):
            section = landreq.board_section()
        self.assertIsNone(section["unavailable"])
        self.assertEqual([row["id"] for row in section["loops"]], ["c"])
        self.assertEqual([row["id"] for row in section["stalled"]], ["c"])

    # ---- site 1: the stall census -------------------------------------

    def test_a_superseded_round_is_not_billed_to_its_author(self):
        parent = self.row("p", stalled=True, dwell_s=9000)
        child = self.row("c", stalled=True, dwell_s=100, supersedes="p")
        got = self.stalled_ids(self.board(parent, child))
        # the POSITIVE half is the point: the debt MOVED to the successor, it
        # was not written off. A cure that emptied both rows would pass an
        # assertion that only said "p is gone".
        self.assertEqual(got, ["c"])

    def test_a_six_round_chain_bills_one_debt_not_six(self):
        rows = [self.row("r0", stalled=True, dwell_s=9000)]
        for i in range(1, 6):
            rows.append(self.row("r%d" % i, stalled=True, dwell_s=9000 - i,
                                 supersedes="r%d" % (i - 1)))
        self.assertEqual(self.stalled_ids(self.board(*rows)), ["r5"])

    def test_a_LANDED_successor_absorbs_the_debt(self):
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="LANDED", terminal=True)
        # BYSTANDER: an unrelated stalled row that must SURVIVE. Without it the
        # assertion below is satisfied just as well by a census that returns
        # nothing at all, which is the failure this whole class is about.
        other = self.row("bystander", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, child, other)),
                         ["bystander"])

    def test_a_terminal_close_absorbs_even_when_the_STATE_does_not_say_so(self):
        """The topology refutation of d9adb643, the exact two shapes.

        A row's projected STATE does not track its CLOSURE: close_reason=landed
        can still render REVIEWED, and close_reason=superseded can still render
        CHANGES_REQUESTED. Both are terminal and both carried the parent's debt
        away, but neither appears in the LANDED/SUPERSEDED state tuple the first
        version compared against — so the parent stayed billed for a debt its
        successor had already discharged. His probes returned [p]; [] is right.

        I keyed on a PROJECTION of the fact instead of the fact, which is the
        error this whole lane exists to correct, committed one layer over."""
        shapes = (("landed", "REVIEWED"), ("superseded", "CHANGES_REQUESTED"))
        self.assertEqual(len(shapes), 2)
        for reason, state in shapes:
            parent = self.row("p", stalled=True)
            child = self.row("c", supersedes="p", state=state, terminal=True,
                             close_reason=reason)
            other = self.row("bystander", stalled=True)
            self.assertEqual(
                self.stalled_ids(self.board(parent, child, other)),
                ["bystander"],
                "close_reason=%s rendering %s left the parent billed"
                % (reason, state))

    def test_a_STRANDED_close_carries_nothing_either(self):
        """The other side of the same fact, and the reason this is a reason-set
        rather than 'terminal absorbs': `stranded` ENDS work without moving it,
        exactly like abandoned. A predicate that absorbed on terminality alone
        would retire a debt nobody took."""
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="REVIEWED", terminal=True,
                         close_reason="stranded")
        self.assertEqual(self.stalled_ids(self.board(parent, child)), ["p"])

    def test_an_ABANDONED_successor_carries_NOTHING(self):
        """The one direction where staying stalled is the honest answer: a
        successor written off moved no debt anywhere, so relieving the parent
        would retire an obligation nobody ever took."""
        parent = self.row("p", stalled=True)
        child = self.row("c", supersedes="p", state="ABANDONED", terminal=True)
        self.assertEqual(self.stalled_ids(self.board(parent, child)), ["p"])

    def test_an_unchained_stalled_row_is_untouched(self):
        """The control. Every assertion above is about rows being REMOVED, and
        a walk that removed everything would satisfy all of them."""
        lone = self.row("lone", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(lone)), ["lone"])

    # ---- topology: the forest, not the first edge -----------------------

    def test_an_abandoned_SIBLING_does_not_resurrect_a_carried_parent(self):
        """A fork does NOT require every branch to absorb. My meld proposal
        said it did; review refuted it: an abandoned sibling does not mint a
        second copy of the parent's debt, so if any branch carries, billing
        the parent too is DOUBLE BILLING."""
        parent = self.row("p", stalled=True)
        dead = self.row("d", supersedes="p", state="ABANDONED", terminal=True)
        live = self.row("L", supersedes="p", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, dead, live)),
                         ["L"])

    def test_when_EVERY_branch_dies_the_nearest_ancestor_keeps_its_debt(self):
        parent = self.row("p", stalled=True)
        d1 = self.row("d1", supersedes="p", state="ABANDONED", terminal=True)
        d2 = self.row("d2", supersedes="p", state="REVIEWED", terminal=True,
                      close_reason="stranded")
        self.assertEqual(self.stalled_ids(self.board(parent, d1, d2)), ["p"])

    def test_two_live_fork_leaves_are_two_obligations_and_no_ancestor(self):
        parent = self.row("p", stalled=True)
        a = self.row("a", supersedes="p", stalled=True)
        b = self.row("b", supersedes="p", stalled=True)
        self.assertEqual(self.stalled_ids(self.board(parent, a, b)), ["a", "b"])

    def test_a_CANCELLED_middle_still_connects_its_live_descendant(self):
        """project() drops status=cancelled while the chain grammar permits a
        successor of a cancelled parent, so a projection-only graph severed
        p -> cancelled -> live and the live carrier stopped absorbing. The
        cancelled node is TRANSIT: never billable, always traversable."""
        parent = self.row("p", stalled=True)
        gone = self.row("x", supersedes="p", _cancelled=True)
        live = self.row("g", supersedes="x", stalled=True)
        got = self.stalled_ids(self.board(parent, gone, live))
        self.assertEqual(got, ["g"])          # not ["p", "g"], not ["p"]

    def test_measurement_reaches_an_APPROVE_GRANDCHILD_behind_a_FIX(self):  # noqa: VACUOUS_ASSERTION — the assertIsNotNone IS the claim; the FIX-middle-alone assertIsNone below is the control on the same observable
        """Traversal continues THROUGH a gated FIX middle: the middle answers
        nothing itself, and the grandchild at the same tip answers everything."""
        parent = self.held("p")
        middle = self.row("m", supersedes="p", polarity="fix",
                          gate="deadbeefdeadbeef", reviewed_tip="81aed834")
        grand = self.row("g", supersedes="m", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        self.assertIsNotNone(self.through(parent, middle, grand))
        # control on the same observable: the FIX middle ALONE answers nothing,
        # so the yes above came from the grandchild and not from reachability.
        self.assertIsNone(self.through(parent, middle))

    def test_a_CYCLE_makes_the_WHOLE_surface_unavailable(self):
        """Never a partial list: "this row still bills" and "the board is
        unavailable" are two different external answers, and a topology we
        cannot trust must not be able to SUPPRESS a debt."""
        a = self.row("a", stalled=True, supersedes="b")
        b = self.row("b", stalled=True, supersedes="a")
        lrs, raw = self.board(a, b)
        # control: a HEALTHY board of the same shape does not raise, so the
        # raises below are about the cycle and not about the fixture.
        ok_lrs, ok_raw = self.board(self.row("h", stalled=True))
        self.assertEqual([r["id"] for r in
                          landreq._stalled_rows(ok_lrs, ok_raw)], ["h"])
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._unmeasurable_rows(lrs, raw)

    def test_a_ROOT_MISMATCH_makes_the_WHOLE_surface_unavailable(self):  # noqa: VACUOUS_ASSERTION — absence of a fold IS the claim; the same-root control above asserts the fold works
        parent = self.row("p", stalled=True, chain_root="ROOT-A")
        child = self.row("c", supersedes="p", stalled=True,
                         chain_root="ROOT-B")
        lrs, raw = self.board(parent, child)
        # control: the SAME two rows sharing one root fold normally.
        same = self.board(self.row("p", stalled=True),
                          self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(same), ["c"])
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)

    def test_an_ABSENT_chain_root_on_an_edge_is_UNKNOWN_not_a_pass(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the both-roots-present fold asserted first is the unconditional control
        """Refuting 9e9b4bb: a v3 child that names a parent and omits
        chain_root replayed as None, and the validation required BOTH roots
        truthy before comparing — so the edge skipped validation entirely and
        the fold SUPPRESSED the parent with unavailable=None. Unknown identity
        on a load-bearing edge is not a pass.

        My fixture defaulted chain_root on every row, so 201 tests could not
        see it: the shape under test was one the board could not express."""
        # Control FIRST, unconditionally: both roots present and equal folds
        # normally, so the raises below are about the ABSENCE and not about a
        # fixture shape that never folds at all.
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        for missing in ("child", "parent"):
            parent = self.row("p", stalled=True,
                              chain_root=None if missing == "parent" else "ROOT")
            child = self.row("c", supersedes="p", stalled=True,
                             chain_root=None if missing == "child" else "ROOT")
            lrs, raw = self.board(parent, child)
            with self.assertRaises(landreq._ChainUntrustworthy,
                                   msg="absent root on the %s passed" % missing):
                landreq._stalled_rows(lrs, raw)
    def test_a_ROOT_parent_with_no_chain_root_roots_its_child_and_folds(self):
        """Refuting 1c2aa62: THE ENDPOINTS ARE NOT SYMMETRIC. A parent
        that names no parent ROOTS ITS OWN CHAIN, and the writer roots its
        child at parent.id — so an absent chain_root there is normal. My
        symmetric rule refused that edge and darkened the WHOLE board for an
        ordinary continuation: worse than the hole it closed, because it
        refused valid work.

        398 of 628 live rows are this shape, and only 6 are legacy — which is
        why the rule keys on TOPOLOGY (names no parent) and not on schema
        version. My first cut branched on v and the census refuted it."""
        schemas = (1, 3)
        self.assertEqual(len(schemas), 2)
        for schema in schemas:
            parent = self.row("p", stalled=True, v=schema, chain_root=None)
            child = self.row("c", supersedes="p", stalled=True, chain_root="p")
            self.assertEqual(self.stalled_ids(self.board(parent, child)), ["c"],
                             "v=%s root-parent was refused" % schema)

    def test_a_parent_that_is_ITSELF_a_continuation_must_carry_a_root(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the folds-normally board asserted first is the unconditional control
        """The other half of the same rule: a row that names a parent AND
        carries no root has no self-rooting excuse, whatever its version."""
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        # The named grandparent is deliberately ABSENT from the board: with it
        # present, the g->p edge refuses FIRST and assertRaises passes without
        # the clause under test ever deciding. My first two versions both did
        # that — same trap, one level out each time.
        parent = self.row("p", supersedes="absent-g", stalled=True,
                          chain_root=None)
        # child root EQUALS parent.id, so a mismatch cannot be the reason it
        # refuses — only the missing-identity clause can. My first version used
        # a different root and the arm passed for the wrong reason: a mutation
        # letting a continuation self-root SURVIVED it.
        child = self.row("c", supersedes="p", stalled=True, chain_root="p")
        lrs, raw = self.board(parent, child)
        with self.assertRaises(landreq._ChainUntrustworthy):
            landreq._stalled_rows(lrs, raw)

    def test_CHAIN_UNKNOWN_is_never_identity_on_either_side(self):  # noqa: VACUOUS_ASSERTION — the REFUSAL is the claim; the folds-normally board asserted first is the unconditional control
        """Two rows both reading UNKNOWN are two INDEPENDENTLY CORRUPTED rows.
        Their equality is agreement that they are broken, not proof they share
        a chain — and the truthy sentinel made == say otherwise.

        Not a live shape: zero rows on the 628-row ledger carry CHAIN_UNKNOWN
        today. The arm pins it before production produces it."""
        ok = self.board(self.row("p", stalled=True),
                        self.row("c", supersedes="p", stalled=True))
        self.assertEqual(self.stalled_ids(ok), ["c"])
        U = dispatches.CHAIN_UNKNOWN
        for proot, croot in ((U, U), (U, "ROOT"), ("ROOT", U)):
            parent = self.row("p", stalled=True, chain_root=proot)
            child = self.row("c", supersedes="p", stalled=True, chain_root=croot)
            lrs, raw = self.board(parent, child)
            with self.assertRaises(landreq._ChainUntrustworthy,
                                   msg="parent=%r child=%r passed" % (proot, croot)):
                landreq._stalled_rows(lrs, raw)

    def test_the_untrustworthy_surface_reaches_the_VERB_as_unavailable(self):
        """The exception is internal; what a caller sees must be an honest
        unavailable, never a short row list that reads like a clean board."""
        a = self.row("a", stalled=True, supersedes="b")
        b = self.row("b", stalled=True, supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(landreq, "project_raw",
                               return_value=(lrs, raw, None)):
            rows, unavailable = landreq.stalls()
        self.assertIsNone(rows)
        self.assertIn("cycle", unavailable)

    # ---- site 2: the unmeasurable census -------------------------------

    def through(self, *rows):
        """_measurable_through for row "p" over a board — the predicate under
        test, bound directly. Asserting these through the unmeasurable COLUMN
        would now measure FRONTIER OWNERSHIP instead: a live descendant moves
        its parent's hold whether or not it clears it, so the parent leaves
        the column either way and the assertion would pass for the wrong
        reason (blocker 2)."""
        lrs, raw = self.board(*rows)
        kids, node, err = landreq._chain_forest(raw, lrs)
        self.assertIsNone(err)
        return landreq._measurable_through(lrs["p"], kids, node)


    def held(self, rid, **kw):
        return self.row(rid, state="REVIEWED", polarity="approve",
                        ungated="approved with no minted gate receipt",
                        reviewed_tip="81aed834", **kw)

    def unmeasurable_ids(self, board):
        lrs, raw = board
        return sorted(r["id"] for r, _why in
                      landreq._unmeasurable_rows(lrs, raw))

    def test_a_gated_child_at_the_SAME_tip_answers_the_hold(self):
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        # BYSTANDER, same reason as the stall site: an empty column is also
        # what a classifier that stopped classifying returns.
        other = self.held("bystander")
        self.assertEqual(
            self.unmeasurable_ids(self.board(parent, child, other)),
            ["bystander"])
        self.assertIsNotNone(self.through(parent, child))

    def test_a_child_gated_at_a_DIFFERENT_tip_proves_nothing(self):
        """A gate binds a tree. A successor gated on other code is a
        measurement of other code, and accepting it would let any later green
        launder this row's missing receipt."""
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="READY", polarity="approve",
                         gate="3caeb50c42f1c790", reviewed_tip=("de" * 20))
        self.assertIsNone(self.through(parent, child))

    def test_a_same_tip_FIX_carrying_a_gate_does_NOT_rescue_its_parent(self):  # noqa: VACUOUS_ASSERTION — the None IS the claim; the approve-polarity control below proves the predicate says yes
        """Gate d9adb643. The predicate read `kid.get("polarity")`
        for TRUTH, so ANY declared polarity satisfied it — and a FIX carrying
        a perfectly valid gate at the very same tip rescued a parent held for
        want of an APPROVAL. A fix verdict authorizes no land; it is the
        opposite of the thing the parent is missing. mark_verdict refuses an
        ungated approve for exactly this reason, and truthiness walked around
        that refusal one layer up.

        Every fixture I wrote gave the child polarity="approve", so the clause
        that decided the result was never the clause under test — the same
        miss as this class's M4."""
        bad_polarities = ("fix", "supersede")
        # Pinned before the loop: an empty tuple asserts nothing, and this arm
        # exists BECAUSE a clause was never reached. It must not itself become
        # a test whose body never runs.
        self.assertEqual(len(bad_polarities), 2)
        for bad in bad_polarities:
            parent = self.held("p")
            child = self.row("c", supersedes="p", state="CHANGES_REQUESTED",
                             polarity=bad, gate="3caeb50c42f1c790",
                             reviewed_tip="81aed834")
            self.assertIsNone(
                self.through(parent, child),
                "a %s verdict rescued a parent held for an APPROVAL" % bad)
        # control: flip ONLY the polarity to approve and the same shape answers
        good = self.row("c", supersedes="p", state="READY", polarity="approve",
                        gate="3caeb50c42f1c790", reviewed_tip="81aed834")
        self.assertIsNotNone(self.through(self.held("p"), good))

    def test_a_child_that_is_ITSELF_ungated_answers_nothing(self):
        """THE CHILD CARRIES A TOKEN AND IS STILL REFUSED — a tier refusal, not
        a missing receipt. Written that way deliberately: my first version gave
        the child no gate at all, so it was rejected by the token clause and the
        `ungated` clause was never reached. Deleting that clause left this test
        GREEN (mutation M4 survived). A gate token is not an approval; `ungated`
        is what says the approval stands, and only a child whose OWN approval
        was refused can prove this clause carries weight."""
        parent = self.held("p")
        child = self.row("c", supersedes="p", state="REVIEWED",
                         polarity="approve", gate="3caeb50c42f1c790",
                         reviewed_tip="81aed834",
                         ungated="approval tier not satisfied")
        self.assertIsNone(self.through(parent, child))
        # and the HOLD still moves: the child is the live frontier, so it is
        # the row that owes, not its parent.
        self.assertEqual(self.unmeasurable_ids(self.board(parent, child)), ["c"])

    def test_an_unchained_held_row_stays_unmeasurable(self):
        """The control for this site: the walk must not swallow a row that has
        no chain to be answered by. Asserts the REASON too — a row surfaced
        under the wrong explanation is the defect this classifier already
        shipped once, when every REVIEWED row was labelled UNDECLARED."""
        _lrs, _raw = self.board(self.held("p"))
        rows = landreq._unmeasurable_rows(_lrs, _raw)
        self.assertEqual([r["id"] for r, _why in rows], ["p"])
        self.assertIn("APPROVE, held", rows[0][1])


class StallsSayTheTipMoved(LandReqBase):
    """A verdict binds a BASE as well as a TIP: when the lane's head advances
    after the review, the verdict may be answering a question that no longer
    exists — and the stalls surface is exactly where a reviewer looks for
    what they still owe. Measured: trunk moved 45+ times in one
    night and the integrator hand-computed "did the tip move under this
    review" before every land because the listing would not say it.

    The marker is three-dot counted (reviewed...head), never two-dot — the
    #91 trap — and SILENT whenever it cannot measure, because an absent
    marker must never read as "the tip did not move"."""

    def _stalled_ready_row(self, lane="lane/probe"):
        # ref_branch binds the UNIQUE local branch pointing at the tip
        # (dispatches._unique_local_tip_branch: exactly one, else None), so
        # the reviewed tip must be a commit that exists ONLY on the lane
        # branch — self.side is on `side` too, which would bind None.
        self.git("checkout", "-q", "-b", lane, self.side)
        tip = self.commit("lane work", path="lanefile")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(lane=lane, ref=tip)
        self.assertEqual(dispatches.rows()[row["id"]].get("ref_branch"),
                         "refs/heads/" + lane,
                         "fixture law: the row must bind the LANE branch")
        self.mark_verdict(row["id"], tip, "ok",
                                polarity="approve")
        self.age(row["id"], landreq.LAND_STALL_S["READY"] + 60)
        return row

    def test_a_moved_tip_is_NAMED_with_its_three_dot_count(self):
        row = self._stalled_ready_row()
        # POSITIVE CONTROL FIRST: the row IS stalled, so the silence/print
        # below is the marker acting, not an empty listing.
        stalled = landreq.stalls()[0]
        self.assertEqual([lr["id"] for lr in stalled], [row["id"]])
        # advance the lane two commits past the reviewed tip
        self.git("checkout", "-q", "lane/probe")
        self.commit("m1"); head = self.commit("m2")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +2 since review)", out)

    def test_an_unmoved_tip_says_nothing(self):
        row = self._stalled_ready_row()
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)   # the row IS listed
        self.assertNotIn("MOVED", out)

    def test_a_rebased_lane_is_not_phantom_inflated(self):  # noqa: VACUOUS_ASSERTION — the assertNotEqual(three, two) two lines above IS the unconditional positive control: the fixture proves the two count forms disagree before the marker's choice between them is asserted
        """The #91 trap in its exact form: three-dot counts from the
        merge-base, and a REBASED lane shares no history with its old tip —
        the symmetric form then counts the whole fork (trunk's five plus
        the lane's one) as "moved", when the reviewer has one unreviewed
        commit. Two-dot from the reviewed commit counts what the reviewer
        has not seen."""
        row = self._stalled_ready_row()
        reviewed = dispatches.rows()[row["id"]]["reviewed_tip"]
        # trunk advances five; the lane is REBASED onto it (old tip
        # dropped, its work replayed as one new commit).
        for i in range(5):
            self.commit("trunk %d\n" % i, path="trunkfile")
        self.git("checkout", "-q", "-B", "lane/probe", self.main)
        self.commit("the replayed lane work", path="lanefile")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        # CONTROL, unconditional: the two forms genuinely differ here —
        # three-dot counts the whole fork, two-dot counts the one commit.
        three = self.git("rev-list", "--count", reviewed + "..." + head)
        two = self.git("rev-list", "--count", reviewed + ".." + head)
        self.assertNotEqual(three, two,
                            "fixture law: three-dot and two-dot must "
                            "disagree on a rebased lane, or the assertion "
                            "below proves nothing about which the marker "
                            "uses")
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +%s since review)" % two, out)

    def test_a_merged_lane_counts_its_own_and_the_merge(self):
        """The other direction of the same rule: a lane that merged trunk
        after the review DID change under the reviewer — the merge and the
        trunk commits it carried in are unreviewed code ahead of the
        verdict, and they count."""
        row = self._stalled_ready_row()
        reviewed = dispatches.rows()[row["id"]]["reviewed_tip"]
        for i in range(3):
            self.commit("trunk %d\n" % i, path="trunkfile")
        self.git("checkout", "-q", "lane/probe")
        self.git("merge", "-q", "--no-edit", self.main)
        self.commit("the one lane commit", path="lane2")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.main)
        self.git("branch", "-f", "lane/probe", head)
        expected = self.git("rev-list", "--count", "--right-only",
                            reviewed + "..." + head)
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn("(tip MOVED +%s since review)" % expected, out)

    def test_a_missing_branch_is_silence_never_a_claim(self):
        row = self._stalled_ready_row()
        # no lane/probe ref at all: the marker cannot measure, and
        # cannot-measure must not render as "not moved" nor as noise.
        rc, out, err = run(["stalls"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"][:12], out)
        self.assertNotIn("MOVED", out)


class TheListHeaderStatesTheFiledPopulation(LandReqBase):
    """`helm lr list` and the web card's filed strip (/api/lr `filed`) read
    one record; a header that carried less of the split than the card would
    let the two surfaces disagree about it (the finding on the first
    cut: the CLI printed only the total). Both headers print the WHOLE strip
    through the same owner (`filed_split` + `filed_line`) the card's body
    rides. The empty board is where it bites most: "no land loops in flight"
    over a populated ledger needs the population beside it, or the absence
    claim reads as an empty RECORD."""

    # `open`, not `in flight` — task/324. The strip counts what is ON THE
    # BOOKS (every non-terminal filed row except REVIEWED); "in flight" is
    # the HEADER's word for the chain-folded live count, and one noun may
    # not name two predicates on one surface. This regex is the reason the
    # rename could not be a display-only patch: it pins the pasted text.
    #
    # AND THAT BUCKET IS NOW PRINTED AS TWO TERMS (task/2381): the rows on the
    # LIVE FRONTIER lead, and the residue whose lane branch is gone follows
    # under its own word, because the single number was read as work owed when
    # most of it was debris. The arms below are about the PARTITION — that
    # every filed row lands in exactly one bucket — so `split` re-adds the
    # residue and hands back the same six numbers they were written for. The
    # frontier/residue arms of the split live in tests.test_lr_retire, on the
    # predicate that decides which side a row is on.
    # THE UNPLACEABLE DISCLOSURE RIDES INSIDE THE FRONTIER TERM and is
    # optional here on purpose: it is a SUBSET of the frontier count, not a
    # seventh bucket, so the partition arms below are unchanged by it — but a
    # regex that could not see it would fail to find the strip at all on any
    # board carrying one, which is most of them.
    _STRIP = re.compile(
        r"filed (\d+) all-time · (\d+) open on the live frontier"
        r"(?: \(incl\. \d+ unclassified[^)]*\))? · (\d+) held"
        r" · (\d+) underived · (\d+) landed · (\d+) closed · (\d+) non-loop")
    _RESIDUE = re.compile(r" · (\d+) OFF-FRONTIER")

    def split(self, out):
        """(total, [buckets]) parsed from the rendered strip — the assertion
        runs on what the owner PASTES, not on the dict behind it.

        `buckets[0]` is the WHOLE open bucket: the printed frontier count plus
        the printed residue, which is silent at zero."""
        m = self._STRIP.search(out)
        self.assertIsNotNone(m, "no filed strip in: %r" % out)
        total, *buckets = (int(g) for g in m.groups())
        residue = self._RESIDUE.search(out)
        buckets[0] += int(residue.group(1)) if residue else 0
        return total, buckets

    def off_trunk(self, name):
        """A tip on its own branch, NOT on trunk — so merging `side` for the
        landed arm cannot silently land another arm's row too."""
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        return tip

    def test_the_populated_header_carries_the_whole_split(self):  # noqa: VACUOUS_ASSERTION — split() asserts the strip EXISTS in the output (assertIsNotNone on the regex match) before the exact-tuple equality; an empty out fails inside the helper
        self.dispatch()
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        total, buckets = self.split(out.splitlines()[0])
        # [open, held, underived, landed, closed, non_loop]
        self.assertEqual((total, buckets), (1, [1, 0, 0, 0, 0, 0]))

    def test_the_empty_board_still_states_the_population(self):  # noqa: VACUOUS_ASSERTION — paired positive controls on the same out (the stamped absence line AND the parsed strip) bind a real render; an empty out fails both
        row = self.dispatch()
        _row, err = dispatches.mark_cancel(row["id"], "moot")
        self.assertIsNone(err)
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # positive control on the same output: the board really is empty
        self.assertIn("no land loops in flight", out)
        total, buckets = self.split(out)
        self.assertEqual((total, buckets), (1, [0, 0, 0, 0, 0, 1]))

    def test_the_header_split_sums_to_its_own_total(self):  # noqa: VACUOUS_ASSERTION — split() asserts the strip EXISTS (assertIsNotNone on the match) and the exact non-zero tuple (3, [1,0,1,0,1]) is the unconditional positive control; the sum line is the self-audit on top
        """The self-checking header: a bucket that leaked rows would break
        the printed arithmetic, so the header carries its own audit."""
        self.dispatch(lane="lane/sum-open", ref=self.off_trunk("sum-open"))
        landed = self.dispatch(lane="lane/sum-landed", ref=self.side)
        self.mark_verdict(landed["id"], self.side, "ok",
                                polarity="approve")
        self.git("merge", "--no-edit", "-q", "side")
        moot = self.dispatch(lane="lane/sum-moot",
                             ref=self.off_trunk("sum-moot"))
        _row, err = dispatches.mark_cancel(moot["id"], "moot")
        self.assertIsNone(err)
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        total, buckets = self.split(out.splitlines()[0])
        self.assertEqual((total, buckets), (3, [1, 0, 0, 1, 0, 1]))
        self.assertEqual(total, sum(buckets),
                         "the printed split no longer sums to its own total")


class AnUnderivedLandingIsItsOwnBucket(LandReqBase):
    """A row whose landedness was never DERIVED is not work, and counting it
    as work is how a board reports a backlog that is partly its own unfinished
    reading.

    THE HONESTY SURVIVED FIVE LAYERS AND DIED AT THE SIXTH. `_landing_proof`
    answers "unknown" on an expired derive budget and says in its own comment
    that running out of time is not evidence the patch is missing;
    `landed_ever` turns that into None; `_git_observe` returns blind; the row
    carries land_state UNKNOWN. Then `filed_split` bucketed on `terminal` and
    `state` alone and put it in `open` or `held` -- the two words a reader acts
    on.

    THE CAUSE IS THE WHOLE DISTINCTION, and keying on `observable` alone gets
    it WRONG: helm deliberately reads no trunk for a row with no verdict yet
    (`_will_observe_git`), because nothing could have landed, and those rows
    are unobservable while being perfectly ordinary open work. Measured on the
    live ledger the day this was written, the three causes separate cleanly:
    597 underived (REVIEWED 574, CHANGES_REQUESTED 19, READY 4), 6 not-asked
    (OPEN 1, AWAITING_REVIEW 3, AWAITING_BUILD 2), 28 no-trunk-or-tip.
    """

    def off_trunk(self, name):
        """A tip on its own branch, NOT on trunk, so no other arm's merge can
        land this one by accident."""
        self.git("checkout", "-q", "-b", name, self.a)
        tip = self.commit(name, path=name)
        self.git("checkout", "-q", self.main)
        return tip

    def test_the_board_names_the_cause_it_MEASURED_not_a_git_failure(self):
        """Four causes, and both surfaces printed one sentence for all four.

        The terminal said a bare "landing unobservable" and the browser said
        "git could not be read for this repo" — which is TRUE of exactly one
        cause and is a false accusation against the repository for the other
        three. UNDERIVED is the one the owner kept hitting: git was never
        asked, because the reader ran out of the budget it declared, so the
        row is undetermined and nothing was found wrong with it.
        """
        self._verdicted("lane/budget-sentence", "budget-sentence")
        # POSITIVE CONTROL: with budget the row is observable, so the mark is
        # ABSENT — a sentence that always prints is a sentence nobody reads.
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed 1 all-time", out)      # the board really rendered
        self.assertNotIn("landing unobservable", out)

        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("landing unobservable — BUDGET EXPIRED", out)
        self.assertNotIn("git could not be read", out,
                         "a spent budget was reported as a git failure")

    def test_the_browser_and_the_terminal_share_ONE_observe_vocabulary(self):
        """Two renderers of one datum, and only one of them is Python.

        `helm/web_ui/scripts/00-core.js.part` is not imported by the module
        that owns these words, so nothing but an arm can keep the board the
        owner reads and the terminal an agent reads from describing one row
        differently — which is the divergence this whole lane is about.
        """
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(landreq.__file__)))
        path = os.path.join(root, "helm", "web_ui", "scripts", "00-core.js.part")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        block = re.search(r"const LR_OBSERVE_REASON = \{(.*?)\n\};",
                          source, re.S)
        self.assertTrue(block, "the browser's reason map is gone or renamed")
        pairs = dict(re.findall(r'"([^"]+)":\s*"((?:[^"\\]|\\.)*)"',
                                block.group(1)))
        self.assertTrue(pairs, "the reason map parsed EMPTY, so an equality "
                               "against it would pass on nothing")
        self.assertEqual(pairs, dict(landreq.OBSERVE_REASON))
        # AND THE BROWSER ACTUALLY CALLS IT: a map nothing reads is not a
        # renderer, and the hardcoded sentence must be gone from this file.
        self.assertIn("lrObserveReason(c.observe_why)", source)
        self.assertNotIn(
            'unobservable — git could not be read for this repo', source,
            "the collapsed sentence is still hardcoded in the browser")

    def test_an_unrecognised_cause_is_never_given_a_name(self):
        """A word this build does not know is where inventing one is worst."""
        self.assertEqual(landreq.observe_reason("a-future-word"),
                         "reason unrecorded")
        self.assertEqual(landreq.observe_reason(None), "reason unrecorded")
        self.assertIn("BUDGET EXPIRED",
                      landreq.observe_reason(landreq.OBSERVE_UNDERIVED))

    def test_a_seeded_budget_replaces_the_module_default_for_that_projection(self):
        """The number is the CALLER'S when the caller said one.

        `_DERIVE_BUDGET_S` was sized for a 10s inject turn and charged to
        every reader alike, including the board reader that serves a stale
        body while it rebuilds and therefore most needed to finish.
        """
        from helm import projscope
        # POSITIVE CONTROL FIRST: with the module default at zero the door
        # really does bite, so the False below is the seed and not a budget
        # that never fired.
        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            with projscope.scope():
                self.assertTrue(landreq._derive_expired())
            with projscope.scope():
                landreq.arm_derive_budget(3600)
                self.assertFalse(landreq._derive_expired())
            # AND THE SEED IS PER PROJECTION, never latched into the process:
            # a later scope that seeds nothing is back on the default.
            with projscope.scope():
                self.assertTrue(landreq._derive_expired())

    def test_the_seed_is_SOFT_and_never_arms_a_raising_projscope_deadline(self):
        """The two channels look interchangeable and are opposites.

        `landreq._git` calls `projscope.spend_or_raise` on every memo lookup,
        so a projscope deadline turns a spent budget into an EXCEPTION out of
        `project_raw` instead of rows that read UNDERIVED. The derive budget's
        whole contract is that running out produces an honest UNKNOWN, so
        seeding one must leave the hard channel untouched.
        """
        from helm import projscope
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: a REAL
        # projscope deadline makes `remaining()` answer a number and
        # `spend_or_raise` refuse, so the Nones below are the soft seed and
        # not a projscope that never speaks.
        with projscope.scope(deadline=time.monotonic() - 1):
            self.assertIsNotNone(projscope.remaining())
            with self.assertRaises(projscope.Expired):
                projscope.spend_or_raise("fixture spend")
        with projscope.scope():
            landreq.arm_derive_budget(0.0)
            self.assertTrue(landreq._derive_expired())
            self.assertIsNone(projscope.remaining(),
                              "the soft seed armed a HARD projscope deadline; "
                              "every _git spend in the projection now raises")
            # The hard door is the one that must still be open.
            self.assertIsNone(projscope.spend_or_raise("fixture spend"))

    def test_the_first_seeder_in_a_projection_wins(self):
        """A second seeder must not be able to extend a bound in force."""
        from helm import projscope
        with projscope.scope():
            first = landreq.arm_derive_budget(0.0)
            self.assertEqual(landreq.arm_derive_budget(3600), first)
            self.assertTrue(landreq._derive_expired())

    def _verdicted(self, lane, name):
        """A row past its verdict -- the population helm DOES ask git about."""
        tip = self.off_trunk(name)
        row = self.dispatch(lane=lane, ref=tip)
        self.mark_verdict(row["id"], tip, "ok", polarity="approve")
        return row

    def test_a_SPENT_budget_makes_a_verdicted_row_underived_not_held(self):  # noqa: VACUOUS_ASSERTION — the flagged zero is the BASELINE half of a two-budget comparison, not a product law; its positive control is spent["underived"] == 1 on the same key of the same function, and the rung cannot link them because they are two filed_split CALLS by design — the same row read under two budgets IS the arm, and folding it into one call would delete the thing under test
        """THE SAME ROW UNDER BOTH BUDGETS, which is the only design that shows
        the bucket follows the CAUSE rather than the row. Nothing about the row
        changes between the two reads: one has budget and answers held, the
        other has none and answers underived.

        AND A PRE-VERDICT ROW RIDES ALONG AS THE DISCRIMINATOR. It is
        unobservable under BOTH budgets -- helm never asks about it -- so a
        bucket keyed on `observable` would swallow it in both reads. It must
        stay `open` in both, and that is what separates "could not finish"
        from "had no reason to look"."""
        self._verdicted("lane/underived-verdicted", "underived-verdicted")
        self.dispatch(lane="lane/underived-pending",
                      ref=self.off_trunk("underived-pending"))

        lrs, raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        # A SET, NOT A SORT: None is one of the values under test (it is what
        # an OBSERVABLE row carries) and python refuses to order it beside a
        # string. The membership checks below are the whole use anyway.
        whys = {lr.get("observe_why")
                for lr in lrs.values() if not lr["terminal"]}
        # THE FIXTURE MUST REALLY BUILD BOTH POPULATIONS, asserted before any
        # bucket is read, or the counts below are about nothing. On the CAUSE
        # and not on the state label: which state a verdicted row lands in
        # (READY, REVIEWED) is this fixture's business and not this arm's,
        # while the causes ARE the subject.
        self.assertIn(landreq.OBSERVE_NOT_ASKED, whys, sorted(map(str, whys)))
        self.assertIn(None, whys,
                      "no row was OBSERVABLE, so the discriminator below "
                      "cannot show the bucket following the cause: %r" % (whys,))
        with_budget = landreq.filed_split(lrs, raw)
        self.assertEqual(with_budget["underived"], 0,
                         "a board that CAN derive has nothing underived: %r"
                         % (with_budget,))

        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            lrs2, raw2, unavailable = landreq.project_raw()
            self.assertIsNone(unavailable, unavailable)
            spent = landreq.filed_split(lrs2, raw2)
        self.assertEqual(spent["underived"], 1,
                         "a spent budget did not reach the bucket: %r"
                         % (spent,))
        self.assertEqual(spent["open"], 1,
                         "the PRE-VERDICT row must stay open under a spent "
                         "budget -- helm never asked about it, which is not "
                         "the same as could not finish: %r" % (spent,))
        for filed in (with_budget, spent):
            self.assertEqual(
                filed["total"],
                filed["open"] + filed["held"] + filed["underived"]
                + filed["landed"] + filed["closed"] + filed["non_loop"],
                "the split no longer partitions the ledger: %r" % (filed,))

    def test_the_header_calls_its_count_an_UPPER_BOUND_only_when_it_is_one(self):
        """THE MUST-MISS IS THE HALF THAT MATTERS. A header that always warns
        is a header nobody reads, so the disclosure must be ABSENT on a board
        helm fully derived -- and the same run asserts the strip rendered, so
        an empty output cannot satisfy the absence."""
        self._verdicted("lane/bound-verdicted", "bound-verdicted")
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed 1 all-time", out)      # the board really rendered
        self.assertNotIn("UNDERIVED", out,
                         "a fully derived board claimed an upper bound")
        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("UNDERIVED", out,
                      "the header hid that its own count is an upper bound")
        self.assertIn("UPPER BOUND", out)

    def test_the_headers_underived_term_names_the_subset_it_counted(self):
        """ONE NOUN MAY NOT NAME TWO PREDICATES ON ONE SURFACE -- the law
        `TheListHeaderStatesTheFiledPopulation` already states for `open`
        against `in flight`, and this is the third row of that shape in this
        header. `filed_line` prints the ALL-TIME filed underived population on
        the SAME rendered line the header prints the IN-FLIGHT subset on:
        unqualified, one cold read of the live board said "529 UNDERIVED" and
        "1309 underived" about forty words apart, with nothing telling the
        reader which question either answered.

        WHAT THIS ARM PINS IS THE QUALIFIER AND NOT THAT DIVERGENCE, stated
        because the two are easy to confuse. In a one-project fixture the two
        counts coincide, and building an honored chain to separate them would
        be an arm about chain folding wearing this one's name. So: the header
        clause must carry its own scope, and the must-miss is the unqualified
        spelling, which is what shipped."""
        self._verdicted("lane/scoped-underived", "scoped-underived")
        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        head = out.splitlines()[0]
        # positive control on the SAME line: both terms really rendered
        self.assertIn("1 IN-FLIGHT UNDERIVED", head)
        self.assertIn("1 underived", head)
        self.assertNotIn("1 UNDERIVED", head,
                         "the header count is readable as the filed "
                         "population one clause away: %r" % (head,))

    def test_the_header_does_not_promise_the_bound_CONVERGES(self):
        """THE REFUTED MECHANISM, KEPT OUT BY NAME. The first cut said the
        count "shrinks as the proof ledger fills", which promises the bound
        accumulates downward as proofs are stored. The carry-forward that
        would accumulate it cannot fire at all: ZERO of 4,444 ancestry pairs
        carry either member among the proof ledger's 83 distinct trunks,
        because the ledger's only writer spends its budget on succession
        pairs. Until task/2259 seeds those pairs a land retires the warm-up,
        so the only claim the header may make is the one it can keep. NOT
        "falls each time the board is read" either: that quantifies over
        every read, and the read after a land is one of them."""
        self._verdicted("lane/no-converge", "no-converge")
        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", 0.0):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # positive control: the disclosure rendered at all
        self.assertIn("UPPER BOUND", out)
        self.assertIn("an UPPER BOUND that re-reading lowers", out)
        self.assertNotIn("as the proof ledger fills", out,
                         "the header promises an accumulation the empty "
                         "pair ledger cannot deliver: %r" % (out,))


class AListingStatesWhenItWasRead(LandReqBase):
    """A listing that carries no read instant is a snapshot that silently
    re-anchors to whenever it is quoted next.

    THE ABSENCE CLAIM IS THE ONE THAT BITES. "no land loops in flight" pasted
    into a room reads as a standing fact about the board, and that is the
    reading an integrator acts on by standing down — hours after it stopped
    being true. Every row already prints a RELATIVE dwell; a relative age with
    no origin cannot be re-derived by the reader."""

    _STAMP = re.compile(r"read (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")

    def test_the_EMPTY_listing_stamps_its_absence_claim(self):
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("no land loops in flight", out)
        self.assertRegex(out, self._STAMP,
                         "an absence claim with no read instant reads as a "
                         "standing fact about the board")

    def test_the_POPULATED_listing_stamps_its_header(self):
        row = self.dispatch()             # one row -> the populated header
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        # POSITIVE CONTROL FIRST, unconditional and on the same output: the
        # listing really rendered the row. Without it, an empty `out` would
        # satisfy both assertions below.
        self.assertIn("land loop", out)
        self.assertIn(str(row["id"])[:12] if isinstance(row, dict) else "", out)
        self.assertNotIn("no land loops in flight", out)
        self.assertRegex(out, self._STAMP)

    def _instants_seen_by(self, verb):
        """The `now` each producer actually RECEIVED on one run of the verb.

        RUNTIME, NOT SOURCE TEXT. These two guards used to grep cmd_lr's
        source, which is the brittleness that produced the EOF-slice defect
        one round earlier — and a text pin cannot see a caller that passes the
        WRONG instant, only one that omits the token. Spying the producers
        answers the actual question: did both receive the SAME bound value,
        or did one sample its own clock (arriving as None)?  (Ruled in the
        convergence meld: do NOT keep the source-text guards.)"""
        seen = {}
        real_project, real_stamp = landreq.project_raw, dispatches._read_stamp

        def spy_project(now=None, *a, **k):
            seen["project_raw"] = now
            return real_project(now, *a, **k)

        def spy_stamp(now=None, *a, **k):
            seen.setdefault("read_stamp", []).append(now)
            return real_stamp(now, *a, **k)

        with mock.patch.object(landreq, "project_raw", spy_project):
            with mock.patch.object(dispatches, "_read_stamp", spy_stamp):
                run([verb])
        return seen

    def test_list_uses_ONE_instant_for_the_stamp_AND_the_projection(self):
        seen = self._instants_seen_by("list")
        # POSITIVE CONTROL: both producers must have been CALLED, or the
        # equality below compares two absent keys and proves nothing.
        self.assertIn("project_raw", seen, "lr list never called project_raw")
        self.assertIn("read_stamp", seen, "lr list never stamped its read")
        self.assertIsNotNone(seen["project_raw"],
                             "project_raw received now=None, so it sampled "
                             "its OWN clock and the header instant is not the "
                             "one that classified the rows")
        for got in seen["read_stamp"]:
            self.assertEqual(got, seen["project_raw"],
                             "the stamp was taken at %r while the projection "
                             "used %r — one verb, two instants"
                             % (got, seen["project_raw"]))

    def test_stalls_uses_ONE_instant_for_every_section(self):
        seen = self._instants_seen_by("stalls")
        self.assertIn("project_raw", seen, "lr stalls never called project_raw")
        self.assertIn("read_stamp", seen, "lr stalls never stamped its read")
        self.assertIsNotNone(seen["project_raw"],
                             "project_raw sampled its own clock in stalls")
        for got in seen["read_stamp"]:
            self.assertEqual(got, seen["project_raw"],
                             "stalls stamped at %r while classifying at %r"
                             % (got, seen["project_raw"]))

    def test_the_stamp_spells_the_instant_the_way_a_ROW_does(self):
        """It is quoted into chat beside row timestamps; a second spelling is
        one more thing for a reader to mis-compare."""
        from helm import dispatches as d
        self.assertEqual(d._read_stamp(0), "1970-01-01T00:00:00Z")
        # POSITIVE CONTROL, unconditional: the helper tracks its argument, so
        # the equality above is a format check and not a constant.
        self.assertNotEqual(d._read_stamp(0), d._read_stamp(86400))


if __name__ == "__main__":
    unittest.main()


class StoredPatchRescueTest(unittest.TestCase):
    """THE OWNER SPOTTED THIS: the home LANDED card read "? UNKNOWN" six times
    for changes that are demonstrably on trunk.

    `_landing_proof` answers about a TIP and both its rungs need that tip
    readable. Land receipts outlive their tips — the integrator lands a
    rebased/cherry-picked commit, the gated object becomes unreachable, git
    prunes it. Measured on this repo's real receipts: all six had
    BOTH anchors dead, 12 of 12 objects unreadable. The receipt's STORED
    patch_id, written while the object was alive, is the only identity left
    that can answer — and all six resolve through it, at trunk depths
    505-579."""

    def test_a_cached_rescue_skips_the_WALK_but_still_re_checks_trunk(self):
        """The cache buys the 600-commit WALK, not blind trust. It first said
        "never walked" and asserted git was never called at all — that claim
        died when it was pointed out a cached hit survives a trunk RESET. A
        patch-id is immutable; TRUNK IS NOT. So a hit now costs exactly ONE
        ancestry call and the walk is still what is saved (7.9ms/commit
        measured, ~4.8s for 600)."""
        from unittest import mock
        pid = "b38be8050f42ca4bd0241c97111d4de88af98c67"
        ok = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/repo/.git\t" + pid: "205f06df1320aa"}), \
                mock.patch.object(landreq, "_git", return_value=ok) as git, \
                _hash_faces(lambda *_a, **_k: pid), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", pid, "refs/remotes/origin/main")
        self.assertEqual(sha, "205f06df1320aa")
        self.assertIn("content identity", why)
        idx.assert_not_called()                       # the WALK is skipped
        self.assertEqual(git.call_args[0][1], "merge-base")   # one re-check

    def test_a_POISONED_cache_entry_is_refused_even_though_it_is_on_trunk(self):
        """The leg that was missed: ancestry proves the cached
        sha is still ON trunk and says NOTHING about whether it still carries
        the patch-id it was cached UNDER.

        Their probe, one repo: key=(gitdir, pid_A) -> sha_B, where B IS on
        trunk but patch_id(B) != pid_A. My re-check passed it, and the card
        then told the reader "this receipt's patch-id matches B" when it did
        not. I validated the answer's LIVENESS and never its CORRECTNESS."""
        from unittest import mock
        pid_a = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid_a: "shaBshaB"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                _hash_faces(lambda *_a, **_k: "b" * 40):
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid_a, "main", index={}, why=False)
        self.assertIsNone(sha)                       # NOT returned as a match
        self.assertIn("no longer carries this receipt's patch-id", why)

    def test_a_cached_hit_that_cannot_be_re_hashed_is_UNKNOWN(self):
        """The third state of the same leg: unreadable is not a match and not
        a mismatch. Without this arm the fix could treat None as 'differs' and
        silently re-derive on every read of an unreadable repo."""
        from unittest import mock
        pid = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid: "somesha"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                _hash_faces(lambda *_a, **_k: None):
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid, "main", index={}, why=False)
        self.assertIsNone(sha)
        self.assertIn("UNKNOWN, not a match", why)

    def test_a_GENUINE_cached_hit_still_answers(self):
        """POSITIVE CONTROL. Both new legs pass and the hit is returned —
        without this the two arms above pass on a version that refuses every
        cached entry and destroys the cache entirely."""
        from unittest import mock
        pid = "a" * 40
        on_trunk = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/r/.git\t" + pid: "goodsha"}), \
                mock.patch.object(landreq, "_git", return_value=on_trunk), \
                _hash_faces(lambda *_a, **_k: pid), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            sha, why = landreq._stored_patch_on_trunk(
                "/r/.git", pid, "main", index={}, why=False)
        self.assertEqual(sha, "goodsha")
        self.assertIn("content identity", why)
        idx.assert_not_called()                      # the WALK is still saved

    def test_a_cached_hit_whose_commit_left_trunk_is_REFUSED(self):
        """The claim: "after trunk reset a cached hit remains true." It does not."""
        from unittest import mock
        pid = "b" * 40
        gone = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(landreq.pk, "read_json",
                               return_value={"/repo/.git\t" + pid: "deadbeefcafe"}), \
                mock.patch.object(landreq, "_git", return_value=gone):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", pid, "main", index={}, why=False)
        self.assertIsNone(sha)
        self.assertIn("no longer on this trunk", why)

    def test_a_breached_cap_is_UNKNOWN_and_never_absent(self):
        """A cap that skips commits cannot prove a patch is not among them.
        The live receipts sit at depth 505-579, so a small cap reported every
        one of them as unresolvable — the exact shape that must not read as
        'proven absent'."""
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", "a" * 40, "refs/remotes/origin/main",
                index={}, why=True)
        self.assertIsNone(sha)
        self.assertIn("UNKNOWN, not absent", why)

    def test_an_unfound_patch_under_a_complete_scan_is_not_a_false_hit(self):
        """POSITIVE CONTROL on the negative: an uncapped scan that genuinely
        does not hold the patch returns nothing and claims nothing. Without
        this the two arms above could both pass on an always-hit stub."""
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            sha, why = landreq._stored_patch_on_trunk(
                "/repo/.git", "b" * 40, "refs/remotes/origin/main",
                index={"c" * 40: ("de" * 20)}, why=False)
        self.assertIsNone(sha)
        self.assertIsNone(why)

    def test_a_malformed_stored_patch_id_is_refused_not_guessed(self):
        from unittest import mock
        with mock.patch.object(landreq.pk, "read_json", return_value={}):
            for bad in ("", None, "not-hex", "zz" * 20):
                with self.subTest(pid=bad):
                    self.assertEqual(
                        landreq._stored_patch_on_trunk(
                            "/repo/.git", bad, "refs/remotes/origin/main",
                            index={}, why=True),
                        (None, None))


class ForgedReceiptCannotClaimALandingTest(unittest.TestCase):
    """The bound FIX on caa6f7a, closed by construction.

    THE REPRO: a hand-built accepted receipt carrying a DEAD reviewed tip plus
    ANY unrelated LIVE patch-id returned on_trunk=true and seeded the cache —
    the receipt asserting its own landing. helm/landreq.py:40 has always said
    the stored patch_id is "correlation evidence" and that "lifecycle
    landedness derives INDEPENDENTLY FROM LIVE GIT", and line 23 says
    "NEVER authority". The first cut of the rescue made correlation into
    authority EIGHT LINES below the sentence forbidding it, and every one of
    its four arms tested that the rescue WORKED rather than whether it SHOULD.

    The cure is not a forgery check — there is nothing to check against, the
    tip is gone. It is that a receipt may never reach `on_trunk` at all."""

    def test_a_receipt_can_never_set_on_trunk(self):
        """The whole finding in one assertion: whatever the receipt carries,
        on_trunk is Git's field. A forged id changes what the card CORRELATES
        to and can never change what it CLAIMS."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "forged", "repo_id": "/r/.git",
               "reviewed_tip": "d" * 40, "patch_id": "f" * 40,
               "trunk_sha": "e" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="unknown"), \
                mock.patch.object(landreq, "_stored_patch_on_trunk",
                                  return_value=("live0badc0de", "correlated")):
            out, _un = landreq.verified_lands(limit=4)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["on_trunk"])          # NOT True. Ever.
        self.assertEqual(out[0]["correlates_to"], "live0badc0de")
        self.assertIsNone(out[0]["how"])               # `how` is Git's too

    def test_a_REAL_git_proof_still_sets_on_trunk(self):
        """POSITIVE CONTROL. Without it the arm above passes on a version that
        hard-codes on_trunk=None and destroys the field's meaning."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "real", "repo_id": "/r/.git",
               "reviewed_tip": "a" * 40, "patch_id": "b" * 40,
               "trunk_sha": "c" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="ancestor"):
            out, _un = landreq.verified_lands(limit=4)
        self.assertIs(out[0]["on_trunk"], True)
        self.assertEqual(out[0]["how"], "ancestor")

    def test_a_row_with_no_usable_patch_id_never_builds_the_index(self):
        """Finding 3: a valid patch_id=None still walked 600 commits.
        Proven by never-called, not by timing."""
        from unittest import mock
        row = {"topic": "helm.land", "lane": "noid", "repo_id": "/r/.git",
               "reviewed_tip": "a" * 40, "patch_id": None, "trunk_sha": "c" * 40}
        with mock.patch.object(landreq, "_receipt_rows",
                               return_value=([row], None)), \
                mock.patch.object(landreq, "_trunk_refs",
                                  return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_landing_proof",
                                  return_value="unknown"), \
                mock.patch.object(landreq, "_stored_patch_index") as idx:
            landreq.verified_lands(limit=4)
        idx.assert_not_called()

    def test_the_cache_is_keyed_by_repo_so_B_cannot_inherit_A(self):
        """Finding 2: estate-global cache keyed on patch-id alone let
        repo B inherit repo A's foreign sha."""
        from unittest import mock
        pid = "a" * 40
        stored = {"/repoA/.git\t" + pid: "aaaaaaaaaaaa"}
        ok = mock.Mock(returncode=0, stdout="")
        with mock.patch.object(landreq.pk, "read_json", return_value=stored), \
                mock.patch.object(landreq, "_git", return_value=ok), \
                _hash_faces(lambda *_a, **_k: pid):
            hit, _ = landreq._stored_patch_on_trunk("/repoA/.git", pid, "main")
            miss, _ = landreq._stored_patch_on_trunk(
                "/repoB/.git", pid, "main", index={}, why=False)
        self.assertEqual(hit and hit[:12], "aaaaaaaaaaaa")   # A still hits
        self.assertIsNone(miss)                              # B does NOT


class LandsCarryChainIdentityTest(LandReqBase):
    """A land receipt gets the CHAIN it belongs to, so the owner's card can
    fold a lane's rounds into one LAND without guessing identity from a label.

    THE GUESS THESE REPLACE deleted a landing: keying the card's groups on
    (lane, trunk_sha) merged two unrelated lands — lane labels are reused
    across many chain roots — and labelled one of them a round of the other.
    So identity is DECLARED here or it is absent, and absent means the surface
    renders the receipt alone."""

    def test_index_reads_the_raw_snapshot_not_the_projection(self):  # noqa: VACUOUS_ASSERTION — assert_not_called IS the claim; the unconditional control is idx.get(tip)==ROOT1 plus cheap.assert_called_once() on the line above, and mutating the read back to project_raw turns 4 arms RED
        """THE PERF ARM, and it pins a regression I measured rather than
        imagined: chain_root is a RAW ledger field, but the first cut read it
        through project_raw() — 26.4s vs 0.034s on this ledger, turning an
        0.89s owner card into a 24.8s one behind a 12s client deadline. A
        card that never renders is worse than a repetitive one."""
        snap = ({"d1": {"chain_root": "ROOT1", "reviewed_tip": "a" * 40}}, None)
        with mock.patch.object(landreq, "project_raw") as proj, \
                mock.patch.object(landreq.dispatches, "snapshot",
                                  return_value=snap) as cheap:
            idx = landreq._chain_root_index()
        # POSITIVE CONTROL FIRST: the index really ran and really indexed.
        # Without it, an early raise would satisfy assert_not_called and this
        # arm would pass while measuring nothing.
        self.assertEqual(idx.get("a" * 40), "ROOT1")
        cheap.assert_called_once()
        proj.assert_not_called()

    def test_declared_chain_reaches_the_receipt_row(self):
        """A receipt whose ledger row declares a chain_root carries it out."""
        tip = "a" * 40
        snap = ({"d1": {"chain_root": "ROOT1", "reviewed_tip": tip,
                        "ref": "lane/x"}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get(tip), "ROOT1")

    def test_undeclared_chain_is_absent_never_invented(self):
        """A row with no chain_root gets NO entry — it is not a chain of one.
        Downstream that renders the receipt alone, which is the honest state."""
        bare, chained = "b" * 40, "c" * 40
        snap = ({"d1": {"chain_root": None, "reviewed_tip": bare},
                 "d2": {"chain_root": "ROOT2", "reviewed_tip": chained}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        # The DECLARED row in the same snapshot is the control: it proves the
        # walk reached these rows at all, so the absence below is a verdict
        # about `bare` and not about an index that never got built.
        self.assertEqual(idx.get(chained), "ROOT2")
        self.assertNotIn(bare, idx)

    def test_a_build_rows_ref_is_a_BASE_and_never_indexed_as_a_tip(self):
        """`ref` means two different things and the row does not say which: on
        a --kind build it is the BASE dispatched FROM, on a --kind review it is
        the reviewed TIP. Indexing it blindly made every build row started from
        trunk claim that trunk sha — ca318bb5812f, a real landing, was claimed
        by FIVE roots: its own review row plus four lanes that merely began
        there. The review row is the unconditional control, so an absent build
        entry is a verdict and not an index that never got built."""
        base = "9" * 40
        snap = ({"b1": {"chain_root": "BUILDROOT", "kind": "build", "ref": base},
                 "r1": {"chain_root": "REVIEWROOT", "kind": "review",
                        "ref": "8" * 40}}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get("8" * 40), "REVIEWROOT")   # control: ref DOES
        self.assertNotIn(base, idx)                          # ... but not on a build

    def test_a_tip_under_two_roots_is_ambiguous_and_answers_absent(self):
        """One tip declared under SEVERAL chain roots is not a first-wins race:
        flipping insertion order flipped the answer. An ambiguous root is not a
        DECLARED root, so it answers None and the receipt renders alone."""
        shared, lone = "7" * 40, "6" * 40
        snap = ({"a": {"chain_root": "R_A", "kind": "review", "reviewed_tip": shared},
                 "b": {"chain_root": "R_B", "kind": "review", "reviewed_tip": shared},
                 "c": {"chain_root": "R_C", "kind": "review", "reviewed_tip": lone}},
                None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=snap):
            idx = landreq._chain_root_index()
        self.assertEqual(idx.get(lone), "R_C")   # control: unambiguous still resolves
        self.assertNotIn(shared, idx)
        # and the answer does not depend on which row the dict yields first
        flipped = ({"b": snap[0]["b"], "a": snap[0]["a"], "c": snap[0]["c"]}, None)
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=flipped):
            self.assertNotIn(shared, landreq._chain_root_index())

    def test_unavailable_ledger_fails_open_to_empty(self):
        """Every failure answers {} — each receipt then stands alone and every
        land stays visible. This index is never the reason the card cannot
        render, and it never guesses identity to stay useful."""
        good = ({"d1": {"chain_root": "ROOT3", "reviewed_tip": "d" * 40}}, None)
        # POSITIVE CONTROL on the same observable: a HEALTHY ledger yields a
        # NON-empty index. Both {} assertions below are otherwise satisfied by
        # a function that can only ever return {}.
        with mock.patch.object(landreq.dispatches, "snapshot", return_value=good):
            self.assertEqual(landreq._chain_root_index(), {"d" * 40: "ROOT3"})
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=({}, "ledger unreadable")):
            self.assertEqual(landreq._chain_root_index(), {})
        with mock.patch.object(landreq.dispatches, "snapshot",
                               side_effect=OSError("boom")):
            self.assertEqual(landreq._chain_root_index(), {})

    def test_verified_lands_carries_chain_root_and_none_is_a_value(self):  # noqa: VACUOUS_ASSERTION — the loop is guarded by an UNCONDITIONAL assertTrue(rows) that fires before it; a receipt is planted above precisely because the empty fixture made this arm vacuous once and the rung caught it
        """The field is always present on the row — None is the fail-open
        answer, never a missing key the renderer has to guess about."""
        # PLANT A RECEIPT. The fixture's HELM_HOME is empty, so without this
        # `rows` is [] and the loop below asserts NOTHING — which is exactly
        # how this arm first passed. helm's own vacuous-assertion rung flagged
        # it and the control proved the rung right.
        path = landreq.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "topic": "helm.land", "schema": landreq.LAND_SCHEMA,
                "id": "e" * 40, "reviewed_tip": "e" * 40, "lane": "lane/planted",
                "branch": "lane/planted", "patch_id": None,
                "trunk_sha": "f" * 40, "repo_id": "/nonexistent/.git",
                "ts": "2026-08-01T00:00:00Z"}) + "\n")
        with mock.patch.object(landreq, "_chain_root_index", return_value={}):
            rows, unavailable = landreq.verified_lands(5)
        self.assertIsNone(unavailable)
        # UNCONDITIONAL: an empty `rows` would make the loop below assert
        # nothing at all, which is the shape this arm is meant to refuse.
        self.assertTrue(rows, "no receipts read — the loop would be vacuous")
        for r in rows:
            self.assertIn("chain_root", r)
            self.assertIsNone(r["chain_root"])


class UnknownGateCapsNamesItsCauseTest(unittest.TestCase):
    """requirement=="unknown" has TWO causes and they need TWO sentences.

    #111/#119, measured: an integrator read "repair the ledger
    row" on a row that was INTACT and spent a whole diagnosis on it. The
    actual defect was three days upstream — a seat home that had not rebased,
    whose `./bin/helm` runs the package beside it and stamps no gate_caps at
    all. 34 post-epoch verdicts carried no stamp; every one came from the
    four seats with stale homes.
    """

    def test_absent_blames_the_writer_and_unreadable_blames_the_row(self):
        absent = landreq._unknown_gate_caps_why({"polarity": "approve"})
        unreadable = landreq._unknown_gate_caps_why({"gate_caps": None})

        # they must not be the same sentence — the collapse IS the defect
        self.assertNotEqual(absent, unreadable)

        # ABSENT: name the writer, exonerate the row, give the check to run
        self.assertIn("stamped no gate_caps", absent)
        self.assertIn("INTACT", absent)
        self.assertIn("grep -c gate_caps", absent)
        self.assertNotIn("repair the row", absent.replace(
            "do not repair the row", ""))

        # UNREADABLE: the ledger IS the defect here, so keep that instruction
        self.assertIn("present but unreadable", unreadable)
        self.assertIn("repair the ledger row", unreadable)

    def test_the_split_reaches_the_refusal_an_integrator_actually_reads(self):
        """The helper is not the surface — _approval_refusal is."""
        row = {"polarity": "approve", "recipient": "helm-claude-2",
               "verdict_ref": "x", "gate": ""}
        with mock.patch.object(landreq, "gate_requirement",
                               return_value="unknown"), \
                mock.patch.object(
                    landreq.dispatches, "approval_tier_for_verdict",
                    return_value=("inside", "")):
            why, _tier = landreq._approval_refusal(row)
        self.assertIn("stamped no gate_caps", why)
        # POSITIVE CONTROL on the same observable: the unreadable shape still
        # reaches the OTHER sentence through the identical call path, so a
        # helper that returned one constant would redden here.
        row2 = dict(row, gate_caps=None)
        with mock.patch.object(landreq, "gate_requirement",
                               return_value="unknown"), \
                mock.patch.object(
                    landreq.dispatches, "approval_tier_for_verdict",
                    return_value=("inside", "")):
            why2, _t2 = landreq._approval_refusal(row2)
        self.assertIn("present but unreadable", why2)


class VanishedObjectProofTest(LandReqBase):
    """A GIT OBJECT THAT DOES NOT EXIST IS THE STRONGEST EVIDENCE OF ABSENCE,
    AND THE LADDER USED TO SCORE IT AS THE WEAKEST.

    `_landing_proof` returned `unknown` for a missing object while the subsumed
    door requires `absent`, so helm's two oldest rows — 4fdd32fcf664 at 7.9d
    and b970911edbe6 at 7.8d, audited — were permanently unclosable
    with every other clause passing: chain matched, cross-family held, and
    their cure 77e4021415e7 was a proven ancestor of main.

    THE TRAP THIS CLASS EXISTS TO KEEP SHUT is the obvious fix. Flipping
    unknown->absent on a LOCAL miss is the same bug pointing the other way: the
    object can live on a remote or in a reflog, and a confident false `absent`
    starts closing rows whose work never landed — strictly worse than
    refusing. So every arm below is really one rule: unanimous silence from
    every reachable source converts, and any probe that could not LOOK
    refuses.

    AND THE LADDER IS NOT WHERE IT CONVERTS. Routing the conversion through
    `_landing_proof` handed its `absent` to every door that reads the ladder,
    `withdrawn` among them, which records "not on trunk" with no confirmation
    to bound the residual. The ladder now answers `unknown` for an object it
    cannot resolve, and the subsumed door asks `_vanished_absence` by name;
    `UnreadableTipIsUnknownNotAbsentTest` below holds the ladder's side."""

    def _remote(self):
        """A reachable origin, so the remote probe can actually answer."""
        bare = os.path.join(self.tmp, "origin.git")
        if not os.path.isdir(bare):
            subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        # idempotent: one arm points origin at a dead URL first, then asks for
        # a live one, and `remote add` on an existing name exits 3.
        subprocess.run(["git", "-C", self.repo, "remote", "remove", "origin"],
                       capture_output=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "-f", "origin", self.main)
        return bare

    @property
    def gitdir(self):
        return os.path.join(self.repo, ".git")

    def test_an_object_no_reachable_source_holds_is_absent_not_unknown(self):
        """THE FOUNDING CASE. Nothing anywhere has it, so it is gone."""
        self._remote()
        missing = "0" * 40
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "absent")
        # …and end-to-end through the reader the subsumed door actually calls
        # for its original. A helper that were right while nothing reached it
        # would close nothing.
        self.assertEqual(landreq._vanished_absence(self.gitdir, missing),
                         "absent")

    def test_the_remote_publishing_it_refuses_because_it_is_not_vanished(self):
        """A LOCAL MISS IS NOT ABSENCE — the object is on origin.

        THE FIXTURE HAS TO ISOLATE THIS CLAUSE AND THE FIRST ONE DID NOT. It
        asked about `main`'s own HEAD, which is in the LOCAL REFLOG, so
        deleting the remote arm entirely left this test green — the reflog arm
        caught it one clause later and the mutation SURVIVED. The sha here is
        therefore built in a SEPARATE clone and pushed, so it exists on origin
        and has never been in this repo's reflog: the remote arm is the only
        one that can answer."""
        bare = self._remote()
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", bare, other], check=True)
        subprocess.run(["git", "-C", other, "config",
                        "user.email", "o@example.com"], check=True)
        subprocess.run(["git", "-C", other, "config", "user.name", "O"],
                       check=True)
        with open(os.path.join(other, "elsewhere"), "w") as fh:
            fh.write("made in another clone\n")
        subprocess.run(["git", "-C", other, "add", "-A"], check=True)
        subprocess.run(["git", "-C", other, "commit", "-q", "-m", "elsewhere"],
                       check=True)
        subprocess.run(["git", "-C", other, "push", "-q", "origin",
                        "HEAD:refs/heads/elsewhere"], check=True)
        on_remote = subprocess.run(
            ["git", "-C", other, "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        # THE THREE CONTROLS THAT MAKE THIS FIXTURE SINGLE-CLAUSE, each
        # unconditional and on the same observables the predicate reads.
        absent_locally = subprocess.run(
            ["git", "--git-dir", self.gitdir, "cat-file", "-e",
             on_remote + "^{commit}"], capture_output=True).returncode
        self.assertTrue(absent_locally, "must not be in the local object DB")
        reflog = subprocess.run(
            ["git", "--git-dir", self.gitdir, "reflog", "--all",
             "--no-abbrev", "--format=%H %gd %gs"],
            capture_output=True, text=True).stdout
        self.assertNotIn(on_remote, reflog,
                         "must not be in the reflog, or the reflog arm "
                         "answers and this test stops testing the remote")
        remote_refs = subprocess.run(
            ["git", "--git-dir", self.gitdir, "ls-remote", "origin"],
            capture_output=True, text=True).stdout
        self.assertIn(on_remote, remote_refs, "but origin DOES publish it")
        self.assertEqual(landreq._vanished_proof(self.gitdir, on_remote),
                         "unknown")

    def test_a_reflog_holding_it_refuses_because_that_is_a_repair_job(self):
        """IT EXISTED HERE ONCE. The object DB losing it is corruption to
        repair, not evidence the work never happened."""
        self._remote()
        # a commit that is reachable from NO ref but IS in the reflog: make it,
        # then move the branch back off it.
        before = self.git("rev-parse", "HEAD")
        orphan = self.commit("orphaned", path="orphan")
        self.git("reset", "-q", "--hard", before)
        reflog = subprocess.run(
            ["git", "--git-dir", self.gitdir, "reflog", "--all",
             "--no-abbrev", "--format=%H %gd %gs"],
            capture_output=True, text=True).stdout
        self.assertIn(orphan, reflog)
        self.assertEqual(landreq._vanished_proof(self.gitdir, orphan),
                         "unknown")

    def test_a_probe_that_could_not_look_never_answers_absent(self):
        """A FAILED PROBE IS NOT A NEGATIVE RESULT — the rule the fleet
        re-derived on five surfaces."""
        missing = "0" * 40
        # NO REMOTE CONFIGURED: ls-remote cannot answer, so absence is unproven.
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "unknown")
        # AN UNREACHABLE REMOTE: the probe errors rather than missing.
        self.git("remote", "add", "origin", "https://127.0.0.1:1/nope.git")
        self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                         "unknown")
        # GIT ITSELF NOT RUNNING (timeout/OSError -> _git returns None).
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "origin.git"))
        with mock.patch.object(landreq, "_git", return_value=None):
            self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                             "unknown")
        # AND THE REFLOG LEG SEPARATELY: the remote answers, the reflog does
        # not. Without this arm a reflog failure would ride the remote's
        # success straight to `absent`.
        self._remote()
        real = landreq._git

        def reflog_broken(gitdir, *args, **kw):
            if args and args[0] == "reflog":
                return None
            return real(gitdir, *args, **kw)

        with mock.patch.object(landreq, "_git", side_effect=reflog_broken):
            self.assertEqual(landreq._vanished_proof(self.gitdir, missing),
                             "unknown")

    def test_something_that_is_not_a_full_sha_is_not_an_object_lookup(self):
        """There is nothing to look up, so there is nothing to prove absent."""
        self._remote()
        self.assertEqual(landreq._vanished_proof(self.gitdir, "abc"),
                         "unknown")
        self.assertEqual(landreq._vanished_proof(self.gitdir, ""), "unknown")
        self.assertEqual(landreq._vanished_proof(self.gitdir, self.main),
                         "unknown")

    def test_a_present_object_still_reaches_the_patch_id_comparison(self):
        """THE REGRESSION GUARD. `_vanished_proof` is reached ONLY on a missing
        object; every existing answer must be untouched, or this fix bought two
        rows by breaking five hundred."""
        self._remote()
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.b, self.main), "ancestor")
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.side, self.main),
            "absent")
        self.git("cherry-pick", self.side)
        self.assertEqual(
            landreq._landing_proof(self.gitdir, self.side, self.main),
            "patch-equivalent")


class UnreadableTipIsUnknownNotAbsentTest(LandReqBase):
    """AN OBJECT THIS CLONE CANNOT RESOLVE IS UNREADABLE, NOT ABSENT.

    `absent` is the population of the `withdrawn` door, which retires a row by
    recording that its work is NOT on trunk. A ladder that routes a tip
    `rev-parse` cannot resolve through `_vanished_proof` lets
    remote-and-reflog silence publish ABSENT, so a lane commit gc has pruned,
    or one never fetched into this clone, closes a live row with a false
    negative. Measured read-only on the live ledger, on the defect: 330 live
    in-repo rows carried an unreadable reviewed tip, 274 of them had no kept
    proof to answer first, and the ladder answered `absent` for every one of
    the 274.

    EVERY FIXTURE HERE HAS A REACHABLE ORIGIN, and that is what makes the arms
    bite. Without one `_vanished_proof` cannot look and answers `unknown`
    whatever the ladder does, so an arm on a remote-less repository passes on
    the defect unchanged — which is how the existing pruned-tip arms stayed
    green over it. Each arm asserts that precondition, on the pruned object
    itself, before it asserts anything about the ladder."""

    def _origin(self):
        """A reachable origin carrying trunk, so the vanished pass can LOOK."""
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", self.main)
        self.git("fetch", "-q", "origin")

    def _pruned(self):
        """A reviewed-tip-shaped commit gc has destroyed, in a repository
        whose every source is silent about it — the old ladder's ABSENT."""
        self.git("checkout", "-q", "-b", "doomed", self.main)
        doomed = self.commit("work gc will destroy", path="doomed")
        self.git("checkout", "-q", self.main)
        self.prune(doomed, "refs/heads/doomed")
        self._origin()
        self.assertEqual(
            landreq._vanished_proof(self.gitdir(), doomed), "absent",
            "precondition: the vanished pass must answer ABSENT for this "
            "object, or the arm cannot tell the cure from the defect")
        return doomed

    def test_a_pruned_tip_is_unknown_and_a_readable_off_trunk_tip_is_absent(self):
        doomed = self._pruned()
        gitdir, trunk = self.gitdir(), self.git("rev-parse", self.main)
        # THE POSITIVE CONTROL, in the same repository and the same call
        # shape: a READABLE tip that is provably not on trunk still answers
        # ABSENT. A cure that answered unknown for everything would disable
        # the withdrawn door and pass every negative below unchanged.
        self.assertEqual(
            landreq._derive_landing_proof(gitdir, self.side, trunk), "absent")
        self.assertEqual(
            landreq._landing_proof(gitdir, self.side, trunk), "absent")
        self.assertIs(landreq.landed_ever(gitdir, self.side, trunk), False)
        # THE CLAIM, through the ladder and through the door that wraps it.
        self.assertEqual(
            landreq._derive_landing_proof(gitdir, doomed, trunk), "unknown")
        self.assertEqual(
            landreq._landing_proof(gitdir, doomed, trunk), "unknown")
        # and the tri-state wrapper projects it as UNMEASURED, never False
        self.assertIsNone(landreq.landed_ever(gitdir, doomed, trunk))

    def test_the_vanished_reader_converts_only_an_object_git_cannot_see(self):
        """`_vanished_absence` is the subsumed door's question, and a tip that
        resolves is never it — its absence is the ladder's to measure."""
        doomed = self._pruned()
        gitdir = self.gitdir()
        self.assertEqual(landreq._vanished_absence(gitdir, doomed), "absent")
        # a resolving tip is never its question, even one that IS off trunk
        self.assertEqual(landreq._vanished_absence(gitdir, self.side),
                         "unknown")
        self.assertEqual(landreq._vanished_absence(gitdir, "abc"), "unknown")
        # and it inherits the bound it wraps: a remote it cannot ask refuses
        self.git("remote", "set-url", "origin",
                 os.path.join(self.tmp, "nowhere.git"))
        self.assertEqual(landreq._vanished_absence(gitdir, doomed), "unknown")

    def test_a_kept_memo_answers_for_a_pruned_tip_and_no_live_read_does(self):
        """A MEMO IS NOT A LIVE READING, in either direction. A positive kept
        while the object was readable still answers after gc takes it — that
        is what the durable ledger is for — and the live ladder, asked the
        same question, says it cannot see the object rather than inventing
        an answer of either sign."""
        gitdir = self.gitdir()
        self.git("cherry-pick", self.side)
        trunk = self.git("rev-parse", self.main)
        self.assertEqual(landreq._landing_proof(gitdir, self.side, trunk),
                         "patch-equivalent")
        self.assertEqual(landreq._kept_landing_proof(gitdir, self.side, trunk),
                         "patch-equivalent", "the memo was never written")
        self.prune(self.side, "refs/heads/side")
        self._origin()
        self.assertEqual(landreq._vanished_proof(gitdir, self.side), "absent",
                         "precondition: the old ladder's ABSENT input")
        self.assertEqual(landreq._landing_proof(gitdir, self.side, trunk),
                         "patch-equivalent")
        self.assertEqual(
            landreq._derive_landing_proof(gitdir, self.side, trunk), "unknown")


class ReadyRungTest(unittest.TestCase):
    """READY says "this may be merged" for rows a land door will refuse, and it
    said it three measured ways. This names WHICH rung bites.

    Measured on the live board when this landed: of 131 READY rows, 81 were
    PRE-V4, 34 had no gate token at all, 12 were STALE-BASE, 4 were SELF-REVIEW
    and ZERO were clean. A word true of every row carries no information, which
    is the whole defect -- not that READY was wrong, but that it was unfalsifiable.

    THE FIRST TEST BELOW IS THE ONE THAT MATTERS. "every row fails a rung" is
    also exactly what a structurally broken predicate produces, so a suite
    without a reachable-plain-READY control cannot tell a real finding from a
    function that only knows one answer.
    """

    TRUNK = "8a5ae7e2cac5" + "0" * 28
    OK = {"tok": {"v": "4", "host": "a-host", "head": TRUNK}}
    ROW = {"state": "READY", "author": "alpha", "reviewer": "beta",
           "gate": "tok"}

    _DEFAULT = object()   # so an EXPLICIT None (unreadable trunk) is testable

    def word(self, row=None, index=_DEFAULT):
        return landreq.ready_word(
            dict(self.ROW, **(row or {})),
            index=self.OK if index is self._DEFAULT else index)

    def test_a_clean_row_is_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """THE CONTROL, and it is unconditional: every other assertion in this
        class is a negative, and negatives are worthless if the function cannot
        produce the positive."""
        self.assertEqual(self.word(), "READY")

    def test_an_unmarked_SELF_REVIEW_is_named(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """reviewer == author: nobody independent looked. First in severity
        order because no receipt can compensate for it."""
        self.assertEqual(self.word({"reviewer": "alpha"}),
                         "READY-SELF-REVIEW")

    def test_the_rung_reads_THE_shared_rule_not_a_private_copy(self):  # noqa: VACUOUS_ASSERTION — the line above the patch is the positive control on the same observable
        """The comparison is NAMED, not inlined, so one reader cannot drift
        from another. This arm forces the predicate off and the rung must go
        with it — a rung keeping its own inline copy stays SELF-REVIEW under
        the patch and fails here, which is the two-surfaces-drift defect
        regrowing."""
        self.assertEqual(self.word({"reviewer": "alpha"}),
                         "READY-SELF-REVIEW")   # control: rule reachable
        with mock.patch.object(landreq, "self_reviewed",
                               return_value=False) as pred:
            self.assertNotEqual(self.word({"reviewer": "alpha"}),
                                "READY-SELF-REVIEW")
        self.assertTrue(pred.called, "the rung never consulted the named "
                        "predicate — the assertion above passed for some "
                        "other reason")

    def test_RECEIPT_VERSION_is_deliberately_NOT_a_rung(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """81 of 131 live READY rows carry a hostless pre-v4 receipt, and NONE
        of them is flagged. The land door's landability check is a regex on the
        TOKEN STRING -- it never opens the receipt record, so it cannot refuse
        on version, host or binding, and binding is verified at `dispatch
        verdict` WRITE time instead. The door would land all 81, so READY does
        not overstate for them; flagging them would assert a consequence that
        does not exist, which is this surface's own disease pointed backwards.

        This test exists to keep it that way: a future reader who notices
        hostless receipts and "fixes" them into a rung must fail here first."""
        old = {"tok": {"v": "1", "head": self.TRUNK}}          # no host at all
        self.assertEqual(self.word(index=old), "READY",
                         "receipt version became a landability rung")

    def test_an_IN_FLIGHT_GATED_LANE_renders_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — asserts plain READY, a positive, on the same observable
        """THE PIN FOR THE ONE THAT REACHED TRUNK. A lane's gate receipt
        attests THE LANE'S OWN TIP, and a lane tip is not reachable from trunk
        until it LANDS -- that is what "in flight" means. A reachability rung
        therefore flags every properly-gated in-flight lane, which is the whole
        audience this board serves; it rendered a minutes-old, host-bound,
        already-approved row as defective, and it did that ON TRUNK because
        fixtures cannot hold a distribution.

        So: a receipt whose head is nowhere near trunk is NOT a defect. If a
        future reader adds a RECEIPT-REACHABILITY rung, this fails first.

        The filed-as-its-own-row sequel (task/266) landed staleness on a
        DIFFERENT measure, and this pin is what keeps the two apart: the
        STALE-BASE rung reads `base_behind` -- trunk commits the row's history
        LACKS, counted from live git by the projection -- which is small for
        every in-flight lane and enormous for a dead one. It never opens the
        receipt, so a receipt whose head is unreachable stays exactly as
        non-defective as this test demands."""
        lane_tip = "b" * 40                    # a lane head, never on trunk
        self.assertEqual(
            self.word(index={"tok": {"v": "4", "host": "h", "head": lane_tip}}),
            "READY",
            "an in-flight gated lane was flagged; reachability is not a rung")

    def test_every_OTHER_state_is_returned_UNTOUCHED(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """DISPLAY TRUTH ONLY. This layer never refuses and never rewrites the
        state word other consumers key on -- STAGE_ORDER, OWED_BY and
        LAND_STALL_S all index "READY", so rewriting it would drop rows out of
        stall accounting. Enforcement stays in the land door."""
        for state in ("OPEN", "AWAITING_REVIEW", "REVIEWED", "LANDED",
                      "CHANGES_REQUESTED", "SUPERSEDED"):
            self.assertEqual(self.word({"state": state}), state)
        self.assertIsNone(landreq.ready_rung(dict(self.ROW, state="OPEN")))

    def test_a_dead_base_is_named_STALE_BASE(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """task/266: a READY row whose base lacks STALE_BASE_BEHIND trunk
        commits is named, AT the boundary — `>=` mutated to `>` fails here,
        and so does a drifted constant, because the fixture reads the real
        one rather than pinning a copy of it."""
        self.assertEqual(self.word({"base_behind": landreq.STALE_BASE_BEHIND}),
                         "READY-STALE-BASE")

    def test_one_commit_under_the_bar_is_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """The other half of the boundary: routine drift is the normal state
        of every in-flight row (76 of 96 measured) and must never be named
        as the defect — that mistake is the rejected reachability rung."""
        self.assertEqual(
            self.word({"base_behind": landreq.STALE_BASE_BEHIND - 1}), "READY")

    def test_an_unmeasured_drift_NEVER_accuses(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (plain READY) on the same observable, which is the whole claim
        """UNKNOWN is a real answer and it is not STALE: an absent field (a
        row projected by older code), an explicit None (rev-list declined),
        and bool-typed junk (True IS an int, and must not read as "1 commit
        behind") all stay silent. A rung minted from a value nobody measured
        would be the confident-verdict-over-no-reading defect this whole
        surface exists to cure."""
        self.assertEqual(self.word({"base_behind": None}), "READY")
        self.assertEqual(self.word({"base_behind": True}), "READY")
        self.assertEqual(self.word({"base_behind": "1333"}), "READY")
        # The bool arm needs a bar True could actually clear, or the guard
        # under test is unreachable and its mutant immortal: with the real
        # 120, True >= 120 is False and dropping `not isinstance(bool)`
        # changes nothing this test can see.
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 1):
            self.assertEqual(self.word({"base_behind": True}), "READY")

    def test_a_stronger_rung_OUTRANKS_a_dead_base(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_row_is_PLAIN_READY is the unconditional positive control on the SAME observable (ready_word); every negative here is meaningless without it and it is asserted first in the class
        """Severity order: a dead base never masks a missing reviewer or an
        unverifiable receipt — those bite at the door, staleness bites at the
        compose leg. The marks column still carries the number either way."""
        vast = 10 ** 6
        self.assertEqual(self.word({"reviewer": "alpha", "base_behind": vast}),
                         "READY-SELF-REVIEW")
        self.assertEqual(self.word({"gate": "", "base_behind": vast}),
                         "READY-UNVERIFIED")


class LandInstructionWordTest(unittest.TestCase):
    """THE WORD A LAND INSTRUCTION MAY CARRY, and the ONE place that knows
    every value of it.

    `dispatches._verdict_land_nudge` built its DM from verdict POLARITY
    ALONE, so every approve got "ready: helm lr land <id>" — for a
    self-reviewed row, for a row hundreds of commits behind trunk, and for a
    row that had already closed. The cure is not a
    branch per state in the notifier; it is that the notifier asks THIS
    function and prescribes only on plain "READY". Every state therefore has
    to be minted here, beside the ladder that already knows them — which is
    what these arms pin.

    THE FIRST ARM IS THE ONE THAT MATTERS. A function that answered
    "not-READY" to everything would satisfy every negative below, so the
    reachable plain-READY control is asserted first and unconditionally."""

    TRUNK = "8a5ae7e2cac5" + "0" * 28
    OK = {"tok": {"v": "4", "host": "a-host", "head": TRUNK}}
    ROW = {"state": "READY", "author": "alpha", "reviewer": "beta",
           "gate": "tok"}

    def word(self, row=None, index=None):
        return landreq.land_instruction_word(
            dict(self.ROW, **(row or {})), index=index or self.OK)

    def test_a_clean_live_row_is_PLAIN_READY(self):  # noqa: VACUOUS_ASSERTION — this IS the unconditional positive control for the class
        """THE CONTROL. The nudge prescribes `helm lr land` on exactly this
        answer and on no other, so a function that could not produce it would
        withhold the instruction from every healthy row — the G2
        nothing-wakes-the-lander defect rebuilt inside its own cure."""
        self.assertEqual(self.word(), "READY")

    def test_every_rung_the_ladder_can_name_reaches_this_word(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """One arm per rung, because the notifier will carry whichever comes
        back and a rung that never reaches this function is a state the DM
        silently calls ready."""
        for patch, expect in (
                ({"reviewer": "alpha"}, "READY-SELF-REVIEW"),
                ({"gate": ""}, "READY-UNVERIFIED"),
                ({"gate": "no-such-token"}, "READY-UNVERIFIED"),
                ({"base_behind": landreq.STALE_BASE_BEHIND},
                 "READY-STALE-BASE")):
            with self.subTest(patch=patch):
                self.assertEqual(self.word(patch), expect)

    def test_CONTESTED_reaches_this_word_too(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """The contest rung needs the join, so it gets its own arm rather than
        a fixture the table above cannot express."""
        with mock.patch.object(landreq, "contest_report",
                               return_value=(True, 1, None)) as rep:
            self.assertEqual(self.word(), "READY-CONTESTED")
        self.assertTrue(rep.called, "the contest join was never consulted — "
                        "the word above came from somewhere else")

    def test_a_TERMINAL_row_is_NEVER_plain_READY(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """THE ARM `ready_word` CANNOT SERVE, and the reason this function
        exists beside it. A terminal row keeps its STORED state — retired
        rows commonly hold a stored state of READY — and `ready_word`
        answers the BOARD's question, so it returns "READY" for them by
        design (no live rung reaches a row with no door left). Asked "may I
        tell someone to MERGE this", a closed row is the loudest NO there is,
        so this must name the terminal instead."""
        for patch, expect in (
                ({"terminal": True, "closed_by_landing": True},
                 "CLOSED_BY_LANDING"),
                ({"terminal": True, "abandoned": True}, "ABANDONED"),
                ({"terminal": True, "withdrawn": True}, "WITHDRAWN"),
                ({"terminal": True, "discharged": True}, "DISCHARGED"),
                ({"terminal": True, "close_reason": "landed"},
                 "CLOSED_LANDED"),
                ({"terminal": True, "close_reason": "delivered-report"},
                 "CLOSED_DELIVERED_REPORT")):
            with self.subTest(patch=patch):
                row = dict(self.ROW, **patch)
                self.assertEqual(landreq.ready_word(row, index=self.OK),
                                 "READY",
                                 "fixture drift: this arm is only meaningful "
                                 "while ready_word still says READY here")
                self.assertEqual(self.word(patch), expect)

    def test_a_terminal_row_with_no_annotation_still_never_says_READY(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """A git-OBSERVED landing carries no closure stamp anywhere, so it is
        terminal with nothing to name it by. It falls back to its own state,
        which for such a row is already the truth."""
        self.assertEqual(self.word({"terminal": True, "state": "LANDED"}),
                         "LANDED")

    def test_a_NON_READY_state_comes_back_untouched(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """A live shape, not a hypothesis: an approve-verdicted row can
        project REVIEWED (approval tier unresolved), and the polarity-only
        line would have told it "ready:"."""
        self.assertEqual(self.word({"state": "REVIEWED"}), "REVIEWED")

    def test_it_DELEGATES_to_ready_word_rather_than_re_deriving(self):  # noqa: VACUOUS_ASSERTION — test_a_clean_live_row_is_PLAIN_READY is the unconditional positive control on the SAME observable
        """THE COUPLING PIN. The whole cure is that the ladder is consulted
        once and its answer carried; a private re-derivation here would drift
        from the board exactly as the notifier's own copy did. Forcing
        ready_word to an invented value must change this answer — and the
        invented value proves nothing is enumerated on the way through."""
        with mock.patch.object(landreq, "ready_word",
                               return_value="READY-NO-SUCH-RUNG") as rw:
            self.assertEqual(self.word(), "READY-NO-SUCH-RUNG")
        self.assertTrue(rw.called, "ready_word was never called — this "
                        "function re-derived the answer itself")


class LandInstructionTest(unittest.TestCase):
    """`land_instruction`: the ONE oracle a land nudge reads, and what it says
    when it cannot read anything. A reading that could not be taken must never
    come back as READY — that would be the defect wearing its own cure."""

    # Two rows of the SAME work chain, deliberately in states no receipt
    # ledger is consulted for — this class pins WHICH row is answered for and
    # what happens when nothing can be read, never the ladder's own rungs
    # (LandInstructionWordTest above owns those).
    CHAIN = {"a" * 32: {"id": "a" * 32, "state": "AWAITING_REVIEW"},
             "b" * 32: {"id": "b" * 32, "state": "OPEN"}}

    def test_it_returns_the_projected_word_for_THIS_row(self):
        with mock.patch.object(landreq, "project",
                               return_value=(self.CHAIN, None)) as proj:
            self.assertEqual(landreq.land_instruction("a" * 32),
                             ("AWAITING_REVIEW", None))
            self.assertEqual(landreq.land_instruction("b" * 32),
                             ("OPEN", None))
        self.assertEqual(proj.call_count, 2, "the projection double was "
                         "bypassed — these answers came from the live board")

    def test_a_unique_PREFIX_resolves_back_out_of_the_chain(self):
        """The projection answers with the selector's whole work CHAIN, so the
        row has to be picked back out of it."""
        with mock.patch.object(landreq, "project",
                               return_value=(self.CHAIN, None)):
            self.assertEqual(landreq.land_instruction("bbbbbbbb"),
                             ("OPEN", None))

    def test_an_unreadable_ledger_is_UNVERIFIED_and_says_why(self):
        with mock.patch.object(landreq, "project",
                               return_value=({}, "ledger unreadable")):
            word, why = landreq.land_instruction("a" * 32)
        self.assertEqual(word, "UNVERIFIED")
        self.assertEqual(why, "ledger unreadable")

    def test_a_RAISING_projection_never_escapes_and_never_says_READY(self):
        """The caller is a delivery leg running after a DURABLE verdict. An
        exception here would cost the wake the notifier exists to send, and a
        fallback to the old prescription would rebuild the defect."""
        with mock.patch.object(landreq, "project",
                               side_effect=RuntimeError("boom")):
            word, why = landreq.land_instruction("a" * 32)
        self.assertEqual(word, "UNVERIFIED")
        self.assertIn("boom", why)

    def test_a_row_the_projection_does_not_hold_is_UNVERIFIED(self):
        with mock.patch.object(landreq, "project", return_value=({}, None)):
            word, why = landreq.land_instruction("c" * 32)
        self.assertEqual(word, "UNVERIFIED")
        self.assertIn("c" * 32, why)


class BaseStateTest(unittest.TestCase):
    """base_state: LANDED / UNLANDED-CURRENT / UNLANDED-STALE / UNKNOWN — the
    task/266 predicate, judged from the row the projection already built.

    THE LADDER IS RESPECTED BY CONSTRUCTION: `landed`/`merged_local` are
    `_landing_proof`'s verdict (ancestry, then patch identity), so this
    function never accuses a row whose sha was merely rewritten by a rebase —
    the census measured 131 of 153 resolvable non-ancestor approves as
    landed-by-rebase, and calling those stale sends someone to
    re-land work already on trunk. The end-to-end arm of that claim lives in
    StaleBaseProjectionTest.test_landed_by_rebase_reads_LANDED_never_stale."""

    def test_landed_wins_over_everything(self):
        """A landed row is LANDED whatever the drift fields say — staleness is
        a property of WAITING work."""
        self.assertEqual(landreq.base_state(
            {"landed": True, "observable": True, "base_behind": 10 ** 6}),
            "LANDED")
        self.assertEqual(landreq.base_state(
            {"merged_local": True, "observable": True, "base_behind": 10 ** 6}),
            "LANDED")

    def test_a_blind_row_is_UNKNOWN_even_with_a_number(self):
        """`observable` is the projection's own "I actually looked" bit; a
        behind-count on a row helm could not observe is a value nothing
        vouches for, and judging it would let a stamped field outrank the
        reading it is supposed to summarise."""
        self.assertEqual(landreq.base_state(
            {"observable": False, "base_behind": 10 ** 6}), "UNKNOWN")

    def test_an_unmeasured_count_is_UNKNOWN_never_current(self):
        """None means UNMEASURED. Rendering it CURRENT would be the exact
        inversion of the task/266 defect: an instrument that could not read
        reporting health."""
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": None}), "UNKNOWN")
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": True}), "UNKNOWN")

    def test_the_boundary_is_the_constant_exactly(self):
        self.assertEqual(landreq.base_state(
            {"observable": True, "base_behind": landreq.STALE_BASE_BEHIND}),
            "UNLANDED-STALE")
        self.assertEqual(landreq.base_state(
            {"observable": True,
             "base_behind": landreq.STALE_BASE_BEHIND - 1}),
            "UNLANDED-CURRENT")

    def test_stale_at_parameterises_the_policy_knob(self):
        """A caller with its own bar gets its own verdicts; the default stays
        the measured constant rather than a copy of it."""
        row = {"observable": True, "base_behind": 5}
        self.assertEqual(landreq.base_state(row, stale_at=5), "UNLANDED-STALE")
        self.assertEqual(landreq.base_state(row, stale_at=6),
                         "UNLANDED-CURRENT")


class StaleBaseProjectionTest(LandReqBase):
    """task/266 end to end: the projection MEASURES a READY row's base drift
    from live git, the predicate judges it, and every reader surface (list
    word, list mark, show detail) says the same thing — one computation, one
    field, no second census.

    The fixture geometry the base mints: `side` is cut from commit `a`, trunk
    then gains `b` and `c` — so a fresh READY row on `side` is exactly TWO
    commits behind, which is the MUST-NOT-FLAG control every scan here is
    seeded with. The MUST-FLAG control advances trunk past a lowered bar."""

    def _ready(self):
        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-sb")
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        return row

    def _receipted(self):
        """Current authority over a seeded, content-valid legacy receipt.

        The receipt reader remains real; only the mint/bind seam is supplied.
        Tier capture uses the real producer, not a retargeted historical event.
        """
        receipt = {
            "v": 1, "event": "gate", "ts": dispatches.pk.now_ts(),
            "head": self.side,
            "tree": self.git("rev-parse", "%s^{tree}" % self.side),
            "dirty": False, "repo_id": self.repo,
            "interpreter": {"name": "cpython", "language": "3.14.4",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 1, "skipped": 0, "rc": 0,
        }
        token = gate._receipt_id(receipt)
        receipt["id"] = token
        self.assertTrue(eventledger.append(gate.receipts_path(), receipt))
        admitted, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(skipped, 0)
        self.assertEqual([rec["id"] for rec in admitted], [token])
        self.assertIn(token, landreq._gate_receipt_index())

        row = self.dispatch(ref=self.side)
        dispatches._mark_delivered(row["id"], "post-sb2")
        with mock.patch.object(gate, "bind",
                               return_value=("VERIFIED", token, "test receipt")):
            got, why = self.mark_verdict(row["id"], self.side, "ok",
                                          polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(dispatches.approval_tier_for_verdict(got),
                         ("none", None))
        return row

    def test_a_ready_row_carries_its_measured_drift(self):  # noqa: VACUOUS_ASSERTION — test_a_dead_base_names_the_rung_on_every_reader_surface is the unconditional positive control on the SAME observables (the list mark and the rung); this is their must-not-flag half
        """The MUST-NOT-FLAG control: routine drift is counted (2, from the
        fixture geometry — a number, not a guess), judged CURRENT, and no
        surface breathes the word stale."""
        row = self._ready()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["base_behind"], 2)
        self.assertEqual(lr["base_state"], "UNLANDED-CURRENT")
        self.assertNotEqual(landreq.ready_rung(lr), "STALE-BASE")
        self.assertNotIn("commits behind trunk", run(["list"])[1])

    def test_a_dead_base_names_the_rung_on_every_reader_surface(self):
        """The MUST-FLAG control, walked through every surface a human or an
        integrator actually reads: the projected fields, the state word, the
        list line with its number, and the show detail. The bar is lowered to
        the fixture's scale via the module constant — the same knob
        production reads — so this also pins that the rung and the predicate
        consult the CONSTANT and not a copy of it."""
        row = self._receipted()
        self.commit("d")                       # behind: 2 -> 3
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 3):
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertEqual(lr["base_behind"], 3)
            self.assertEqual(lr["base_state"], "UNLANDED-STALE")
            self.assertEqual(landreq.ready_word(lr), "READY-STALE-BASE")
            listing = run(["list"])[1]
            self.assertIn("READY-STALE-BASE", listing)
            self.assertIn("base 3 commits behind trunk", listing)
            shown = run(["show", row["id"]])[1]
            self.assertIn("base is 3 commits behind trunk", shown)
            self.assertIn("STALE", shown)

    def test_landed_by_rebase_reads_LANDED_never_stale(self):
        """THE LADDER-RESPECT CONTROL, the census's own worst case: the work
        reached trunk under a REWRITTEN sha, so ancestry says NO — asserted,
        not assumed — and only patch identity says LANDED. A staleness rung
        that consulted ancestry alone would accuse this row of being a
        zombie while its content sits on trunk; 131 of the 153 resolvable
        non-ancestor approves in the live ledger are this exact shape
        ."""
        row = self._ready()
        self.git("cherry-pick", self.side)     # lands the CONTENT, new sha
        gitdir = os.path.join(self.repo, ".git")
        self.assertEqual(landreq._ancestry(gitdir, self.side, self.main),
                         landreq.NOT_ANCESTOR,
                         "fixture must exercise the patch-identity rung")
        with mock.patch.object(landreq, "STALE_BASE_BEHIND", 1):
            lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "LANDED")
        self.assertEqual(lr["base_state"], "LANDED")
        self.assertIsNone(lr["base_behind"])   # drift is a property of WAITING

    def test_drift_is_counted_against_the_LANDING_target(self):
        """Upstream trunk when one exists — the SAME leg the row's landedness
        is judged on. Counting drift against a different trunk than the land
        verdict reads would let one row read LANDED on one ref and stale
        against another, which is a two-census disagreement inside a single
        row. Local main advances past origin here, so the two legs disagree
        by exactly one commit and the assertion can tell which was read."""
        self.add_origin()
        row = self._ready()
        self.commit("d")            # local main gains d; origin/main does not
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "READY")
        self.assertEqual(lr["base_behind"], 2)     # vs origin/main — not 3

    def test_base_behind_declines_to_answer_rather_than_guessing(self):
        """The None arms, with their positive control last: an unresolvable
        tip and a repo with no trunk both answer None — UNMEASURED — because
        a drift count nothing measured must never render as a number (least
        of all 0, which reads as freshly-cut)."""
        gitdir = os.path.join(self.repo, ".git")
        self.assertIsNone(landreq._base_behind(gitdir, "f" * 40, {}))
        self.assertIsNone(landreq._base_behind(
            os.path.join(self.tmp, "nowhere", ".git"), self.side, {}))
        self.assertEqual(landreq._base_behind(gitdir, self.side, {}), 2)

    def test_a_terminal_row_prints_no_drift_even_with_a_frozen_READY_word(self):
        """A closed-as-landed row FREEZES the verdict word READY in its
        headline, and a terminal row stops re-reading git by design — so the
        show surface used to print "drift UNMEASURED" on it forever: an
        eternal shrug on a discharged row (live dogfood find).
        Staleness is a property of WAITING work; the drift line must gate on
        non-terminal, not on the frozen word. Self-controlled: the SAME row
        shows its drift line while live, and drops it at closure."""
        row = self._ready()
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertIn("drift", landreq._render_show(lr))     # live control
        self.git("cherry-pick", self.side)     # the proof close_landed needs
        closed, why = landreq.close_landed(row["id"], trunk=self.main,
                                           live=True)
        self.assertIsNone(why, why)
        self.assertTrue(closed["terminal"])
        shown = landreq._render_show(landreq.get(row["id"])[0])
        headline = shown.splitlines()[0]
        self.assertIn("READY", headline)       # the frozen word, asserted —
        self.assertIn("CLOSED (LANDED)", shown)  # this IS the dogfood shape
        self.assertNotIn("drift", shown)

    def test_an_unobservable_ready_row_is_UNKNOWN_and_says_UNMEASURED(self):
        """A READY row whose landing helm cannot observe cannot have its
        drift measured either, and the show surface must say so rather than
        render like a current row — 'I could not look' printed as 'nothing
        to see' is this module's oldest recurring defect, and staleness must
        not re-introduce it. The blindness is injected at the observation
        seam (`_git_observe` answering its own blind shape), which is what a
        vanished repo or an rc-128 ancestry actually produces there; the
        CONTROL half is the sibling test above, where the same fixture with
        a live repo measures 2."""
        row = self._ready()
        with mock.patch.object(landreq, "_git_observe",
                               return_value=dict(landreq._UNOBSERVED)):
            lr, err = landreq.get(row["id"])
            self.assertIsNone(err, err)
            self.assertEqual(lr["state"], "READY")
            self.assertFalse(lr["observable"])
            self.assertIsNone(lr["base_behind"])
            self.assertEqual(lr["base_state"], "UNKNOWN")
            self.assertIn("UNMEASURED", run(["show", row["id"]])[1])


class CloseReasonRegisterParityTest(unittest.TestCase):
    """THE REGISTER: CLOSE_CLI_REASONS is the one truth, and every help
    surface that enumerates close reasons must carry ALL of it in order.
    Before this pin the three surfaces held three different lists (VERBS.md
    seven, landreq USAGE seven, cli.py eight) — a reader was told a door does
    not exist depending on which help they read, while the code accepted nine.
    """

    # `--reason TEXT` (abandon) is uppercase and never matches; only the
    # lowercase pipe-joined enumeration is a register.
    _LIST = re.compile(r"--reason ([a-z][a-z-]*(?:\|[a-z][a-z-]*)+)")

    def _register(self, text, label):
        m = self._LIST.search(text)
        self.assertIsNotNone(m, "%s carries no --reason enumeration" % label)
        return tuple(m.group(1).split("|"))

    def test_usage_string_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — _register's assertIsNotNone is the unconditional positive control: the enumeration must EXIST before the equality means anything
        self.assertEqual(self._register(landreq.USAGE, "landreq.USAGE"),
                         landreq.CLOSE_CLI_REASONS)

    def test_cli_verb_help_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — _register's assertIsNotNone is the unconditional positive control: the enumeration must EXIST before the equality means anything
        from helm import cli
        self.assertEqual(
            self._register(cli._VERB_HELP["lr"], "cli._VERB_HELP['lr']"),
            landreq.CLOSE_CLI_REASONS)

    def test_verbs_md_lists_every_close_reason(self):  # noqa: VACUOUS_ASSERTION — the MUST-HIT assertTrue(registers) is the unconditional positive control; zero matches fails loudly instead of passing empty
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "VERBS.md")
        with open(path, encoding="utf-8") as fh:
            registers = self._LIST.findall(fh.read())
        # MUST-HIT: the lr close signature is a register; zero matches means
        # the probe broke, not that the doc is clean.
        self.assertTrue(registers, "docs/VERBS.md carries no --reason "
                                   "enumeration — probe or doc broke")
        for found in registers:
            self.assertEqual(tuple(found.split("|")),
                             landreq.CLOSE_CLI_REASONS,
                             "a docs/VERBS.md close-reason list drifted from "
                             "CLOSE_CLI_REASONS")


class SuccessionProofContextTest(unittest.TestCase):
    """The succession door proves one related carrier in its own repository.

    These are small geometry arms rather than another Git fixture: each one
    isolates a trust boundary that the old ambient `origin/main` probe crossed.
    """

    ROOT = "work-root"
    REPO = "/repo/.git"
    TARGET = "a" * 40
    CARRIER = "b" * 40
    PIN = "c" * 40

    def rows(self, **target_kw):
        target = {"id": "target", "chain_root": self.ROOT,
                  "reviewed_tip": self.TARGET, "polarity": "fix",
                  "repo_id": self.REPO, "contrary": True, "terminal": False}
        target.update(target_kw)
        carrier = {"id": "carrier", "chain_root": self.ROOT,
                   "reviewed_tip": self.CARRIER, "polarity": "approve",
                   "repo_id": self.REPO}
        return target, carrier

    def pinned(self, state):
        """A proof context whose repository resolves to one explicit trunk."""
        from helm import vcs
        backend = mock.Mock()
        backend.landed_state.return_value = state
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            landreq, "_trunk_refs",
            return_value=("refs/heads/release", None)))
        stack.enter_context(mock.patch.object(
            landreq, "_origin_configured", return_value=False))
        git = stack.enter_context(mock.patch.object(
            landreq, "_git", return_value=mock.Mock(
                returncode=0, stdout=self.PIN + "\n")))
        stack.enter_context(mock.patch.object(vcs, "backend", return_value=backend))
        return stack, landreq._landing_proofs(), backend, git

    def test_a_cross_repository_carrier_cannot_retire_the_target(self):  # noqa: VACUOUS_ASSERTION — backend.assert_not_called is the forbidden-I/O observable; same-repository positive proofs run in this class.
        target, carrier = self.rows()
        carrier["repo_id"] = "/other/.git"
        proof = landreq._landing_proofs()
        with mock.patch("helm.vcs.backend") as backend:
            self.assertEqual(proof(target, carrier), landreq.PROOF_CROSS_REPO)
        backend.assert_not_called()
        carrier["supersedes"] = target["id"]
        state, evidence = landreq._succession(
            target, {self.ROOT: [target, carrier]}, set(), {}, set(),
            {target["id"]: target, carrier["id"]: carrier}, proof)
        self.assertEqual(state, landreq.SUCCESSION_HELD)
        self.assertIsNone(evidence)

    def test_patch_identity_on_a_descendant_does_not_prove_target_history(self):  # noqa: VACUOUS_ASSERTION — the next test is the unconditional target-ancestor positive on this exact geometry.
        target, carrier = self.rows()
        landed = mock.Mock(side_effect=lambda _owner, row:
                           landreq.PROOF_PATCH_EQUIVALENT
                           if row is carrier else landreq.PROOF_ABSENT)
        state, evidence = landreq._succession(
            target, {self.ROOT: [target, carrier]}, set(),
            {(self.TARGET, self.CARRIER): True}, set(),
            {target["id"]: target, carrier["id"]: carrier}, landed)
        self.assertEqual(state, landreq.SUCCESSION_HELD)
        self.assertIsNone(evidence)
        self.assertEqual(landed.call_args_list,
                         [mock.call(target, carrier), mock.call(target, target)])

    def test_patch_landed_carrier_keeps_its_own_proof_after_target_check(self):  # noqa: VACUOUS_ASSERTION — MOVED plus patch-equivalent carrier evidence is the unconditional positive observable.
        target, carrier = self.rows()
        landed = mock.Mock(side_effect=lambda _owner, row:
                           landreq.PROOF_PATCH_EQUIVALENT
                           if row is carrier else landreq.PROOF_ANCESTOR)
        state, evidence = landreq._succession(
            target, {self.ROOT: [target, carrier]}, set(),
            {(self.TARGET, self.CARRIER): True}, set(),
            {target["id"]: target, carrier["id"]: carrier}, landed)
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(evidence["landed_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)

    def test_non_cwd_tip_ancestry_is_measured_in_the_rows_repository(self):  # noqa: VACUOUS_ASSERTION — the spy path and the resulting MOVED state are both asserted.
        target, carrier = self.rows()
        pair = (self.TARGET, self.CARRIER)
        out = {target["id"]: target}
        current = {target["id"]: target, carrier["id"]: carrier}
        seen = []

        def learn(root, pairs):
            seen.append((root, set(pairs)))
            return {p: True for p in pairs}

        with mock.patch.object(landreq, "_ancestry_ledger", return_value={}), \
                mock.patch.object(landreq, "_ancestry_append", side_effect=learn):
            landreq._annotate_succession(
                out, current, set(), gitdir="/ambient/repo",
                landed=mock.Mock(return_value=landreq.PROOF_ANCESTOR))
        self.assertTrue(seen)
        self.assertEqual(seen[0], ("/repo", {pair}))
        self.assertEqual(target["succession_state"], landreq.SUCCESSION_MOVED)

    def test_live_contrary_pairs_spend_the_ancestry_budget_first(self):  # noqa: VACUOUS_ASSERTION — append order is the contract, so the recorded first batch is the direct observable.
        early, early_carrier = self.rows(contrary=False)
        early["id"], early_carrier["id"] = "early", "early-carrier"
        early["chain_root"] = early_carrier["chain_root"] = "early-root"
        early["reviewed_tip"], early_carrier["reviewed_tip"] = \
            "1" * 40, "2" * 40
        urgent, urgent_carrier = self.rows()
        urgent["id"], urgent_carrier["id"] = "urgent", "urgent-carrier"
        urgent["chain_root"] = urgent_carrier["chain_root"] = "urgent-root"
        urgent["reviewed_tip"], urgent_carrier["reviewed_tip"] = \
            "3" * 40, "4" * 40
        out = {early["id"]: early, urgent["id"]: urgent}
        current = {r["id"]: r for r in
                   (early, early_carrier, urgent, urgent_carrier)}
        batches = []

        def learn(_root, pairs):
            batches.append(set(pairs))
            return {p: False for p in pairs}

        with mock.patch.object(landreq, "ANCESTRY_BUDGET", 1), \
                mock.patch.object(landreq, "_ancestry_ledger", return_value={}), \
                mock.patch.object(landreq, "_ancestry_append", side_effect=learn):
            landreq._annotate_succession(
                out, current, set(), landed=mock.Mock(
                    return_value=landreq.PROOF_UNKNOWN))
        self.assertTrue(batches)
        self.assertEqual(batches[0],
                         {(urgent["reviewed_tip"],
                           urgent_carrier["reviewed_tip"])})

    def test_an_unrelated_siblings_unreadable_repo_cannot_poison_HELD(self):  # noqa: VACUOUS_ASSERTION — assert_not_called is the relation-before-I/O contract; related positive carriers run in this class.
        target, carrier = self.rows()
        landed = mock.Mock(return_value=landreq.PROOF_UNKNOWN)
        state, evidence = landreq._succession(
            target, {self.ROOT: [target, carrier]}, set(),
            {(self.TARGET, self.CARRIER): False}, set(),
            {target["id"]: target, carrier["id"]: carrier}, landed)
        self.assertEqual(state, landreq.SUCCESSION_HELD)
        self.assertIsNone(evidence)
        landed.assert_not_called()       # relation is decided before repository I/O

    def test_a_cross_chain_supersedes_path_is_UNKNOWN_and_names_the_cause(self):
        target, carrier = self.rows()
        middle = {"id": "foreign-middle", "chain_root": "other-root",
                  "reviewed_tip": "d" * 40, "repo_id": self.REPO,
                  "supersedes": target["id"]}
        carrier["supersedes"] = middle["id"]
        current = {r["id"]: r for r in (target, middle, carrier)}
        out = {target["id"]: target}
        with mock.patch.object(landreq, "_ancestry_ledger", return_value={
                (self.TARGET, self.CARRIER): False}):
            landreq._annotate_succession(
                out, current, set(), landed=mock.Mock(
                    return_value=landreq.PROOF_ANCESTOR))
        self.assertEqual(target["succession_state"], landreq.SUCCESSION_UNKNOWN)
        self.assertEqual(target["succession_unknown_reason"],
                         "supersedes chain is malformed or unreadable")
        landreq._annotate_contrary_discharge(out)
        self.assertEqual(target["contrary_discharge"], "unverified")

    def test_the_actual_trunk_object_is_pinned_once_for_the_projection(self):
        from helm import vcs
        stack, proof, backend, git = self.pinned(vcs.ANCESTOR)
        target, carrier = self.rows()
        other = dict(carrier, id="other", reviewed_tip="e" * 40)
        with stack:
            self.assertEqual(proof(target, carrier), landreq.PROOF_ANCESTOR)
            self.assertEqual(proof(target, other), landreq.PROOF_ANCESTOR)
        self.assertEqual(git.call_count, 1)
        self.assertEqual(git.call_args.args[-1], "refs/heads/release^{commit}")
        self.assertEqual(
            [c.args for c in backend.landed_state.call_args_list],
            [("/repo", self.CARRIER, self.PIN),
             ("/repo", other["reviewed_tip"], self.PIN)])

    def test_a_transient_UNKNOWN_landing_answer_is_not_memoized(self):
        from helm import vcs
        stack, proof, backend, _git = self.pinned(None)
        backend.landed_state.side_effect = (None, vcs.ANCESTOR)
        target, carrier = self.rows()
        with stack:
            self.assertEqual(proof(target, carrier), landreq.PROOF_UNKNOWN)
            self.assertEqual(proof(target, carrier), landreq.PROOF_ANCESTOR)
        self.assertEqual(backend.landed_state.call_count, 2)

    def test_a_raising_trunk_resolver_is_NO_PIN_not_a_projection_crash(self):  # noqa: VACUOUS_ASSERTION — the following retry tests are unconditional readable-pin positives for the same oracle.
        target, carrier = self.rows()
        with mock.patch.object(landreq, "_trunk_refs",
                               side_effect=OSError("repo disappeared")):
            proof = landreq._landing_proofs()
            self.assertEqual(proof(target, carrier), landreq.PROOF_NO_PIN)

    def test_a_failed_pin_probe_is_forgotten_beneath_projection_memoization(self):
        from helm import vcs
        target, carrier = self.rows()
        failed = mock.Mock(returncode=1, stdout="")
        found = mock.Mock(returncode=0, stdout=self.PIN + "\n")
        backend = mock.Mock()
        backend.landed_state.return_value = vcs.ANCESTOR
        with mock.patch.object(landreq, "_trunk_refs",
                               return_value=("refs/heads/release", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_git_spawn",
                                  side_effect=(failed, found)) as spawn, \
                mock.patch.object(vcs, "backend", return_value=backend), \
                projscope.scope():
            proof = landreq._landing_proofs()
            self.assertEqual(proof(target, carrier), landreq.PROOF_NO_PIN)
            self.assertEqual(proof(target, carrier), landreq.PROOF_ANCESTOR)
        self.assertEqual(spawn.call_count, 2)

    def test_a_transient_missing_pin_is_retried_not_cached(self):
        from helm import vcs
        target, carrier = self.rows()
        backend = mock.Mock()
        backend.landed_state.return_value = vcs.ANCESTOR
        resolved = (None, None, "refs/heads/release", None)
        with mock.patch.object(landreq, "_resolve_ref", side_effect=resolved) as ref, \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_git", return_value=mock.Mock(
                    returncode=0, stdout=self.PIN + "\n")), \
                mock.patch.object(vcs, "backend", return_value=backend):
            proof = landreq._landing_proofs()
            self.assertEqual(proof(target, carrier), landreq.PROOF_NO_PIN)
            self.assertEqual(proof(target, carrier), landreq.PROOF_ANCESTOR)
        self.assertEqual(ref.call_count, 4)
        backend.landed_state.assert_called_once_with(
            "/repo", self.CARRIER, self.PIN)

    def test_a_vanished_object_stays_UNKNOWN_never_measured_absence(self):
        stack, proof, backend, _git = self.pinned(None)
        target, carrier = self.rows()
        with stack:
            self.assertEqual(proof(target, carrier), landreq.PROOF_UNKNOWN)
        backend.landed_state.assert_called_once_with(
            "/repo", self.CARRIER, self.PIN)

    def test_contrary_rendering_reuses_the_stamped_succession_result(self):  # noqa: VACUOUS_ASSERTION — MOVED/b is the positive result and the unchanged proof-call count is the reuse contract.
        target, carrier = self.rows()
        carrier["supersedes"] = target["id"]
        current = {target["id"]: target, carrier["id"]: carrier}
        out = {target["id"]: target}
        landed = mock.Mock(return_value=landreq.PROOF_ANCESTOR)
        landreq._annotate_succession(out, current, set(), landed=landed)
        calls = landed.call_count
        self.assertEqual(target["succession_state"], landreq.SUCCESSION_MOVED)
        landreq._annotate_contrary_discharge(out)
        self.assertEqual(target["contrary_discharge"], "b")
        self.assertEqual(landed.call_count, calls,
                         "contrary rendering ran a second proof snapshot")


class InFlightIsONENumberOnBOTHFrontEndsTest(LandReqBase):
    """The lane's own defect one surface down.

    This lane renamed `filed_split`'s bucket so "in flight" named one
    predicate WITHIN a surface. It did not check ACROSS them — and the
    browser had partitioned honored rows out of its in-flight count
    ("superseded, if verified, should just be like another type of
    closed" — the owner) while `helm lr list` counted them IN. Same disease,
    same word, one layer out: a reader comparing the console to the CLI on a
    board with honored rows saw two numbers and no way to tell which lied.

    `inflight_rows` is now the single owner, so these pin the two front-ends
    to it rather than to each other's text."""

    def board(self):
        """Two real projected rows, one stamped honored. The stamp is set in
        memory on a REAL row — the idiom ContraryHonoredOnEverySurfaceTest
        already uses — because the discharge annotator needs a landed cure
        carrier that has nothing to do with what is being measured here."""
        self.dispatch(lane="lane/moving")
        self.dispatch(lane="lane/also-moving")
        lrs, unavailable = landreq.project()
        self.assertIsNone(unavailable, unavailable)
        rows = sorted(lrs.values(), key=lambda r: r["id"])
        self.assertEqual(len(rows), 2)              # MUST-HIT: the board exists
        rows[0]["contrary"] = True
        rows[0]["contrary_discharge"] = "c"
        # MUST-HIT: the fixture really is honored, so a parity assertion below
        # cannot pass by comparing two empty sets.
        self.assertTrue(landreq.honored_display(rows[0]))
        self.assertFalse(landreq.honored_display(rows[1]))
        return rows

    def test_the_browsers_in_flight_rows_are_the_CLIs_in_flight_rows(self):
        """THE guard. The browser's count is `loops.filter(c => !c.honored)`
        over card() payloads; the CLI's is inflight_rows(). Same rows, by id,
        or one of the two surfaces is lying to the owner."""
        rows = self.board()
        browser = [c["id"] for c in (landreq.card(lr) for lr in rows)
                   if not c["honored"]]
        cli = [lr["id"] for lr in landreq.inflight_rows(rows)]
        self.assertEqual(browser, cli)
        self.assertEqual(len(cli), 1)     # and it actually excluded the row

    def test_the_header_NAMES_the_honored_row_it_stopped_counting(self):
        """A number that shrinks with no explanation is its own bug report.
        The rows are still LISTED, so the header says all three counts and
        the arithmetic is checkable on its face."""
        rows = self.board()
        with mock.patch.object(landreq, "_loop_rows", return_value=rows):
            rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        head = out.splitlines()[0]
        self.assertIn("2 land loops", head)          # both still listed
        self.assertIn("1 honored", head)             # and named
        self.assertIn("1 in flight", head)
        self.assertNotIn("2 in flight", head)

    def test_a_board_with_NO_honored_rows_reads_exactly_as_before(self):
        """The negative control: this must not add a clause to the header
        every fleet member has been reading all night."""
        self.dispatch(lane="lane/plain")
        rc, out, err = run(["list"])
        self.assertEqual(rc, 0, err)
        head = out.splitlines()[0]
        self.assertIn("1 land loop in flight", head)
        self.assertNotIn("honored", head)


class LaneSpellingJoinTest(LandReqBase):
    """#142: every read-side identity join must understand
    BOTH historical spellings and every role suffix, while replay and
    verification stay byte-true to what was stored or signed."""

    def test_liveness_stem_shares_the_canonical_role_vocabulary(self):
        """Finding 2: `_stem` (the ref/worktree liveness joins behind
        rebind/stranded/out-of-scope) knew only -rN/-dHEX, so feature-review
        and feature-build read as strangers while `_lane_stem` called them
        family — and stranded is irreversible, so the refusal-only probe must
        never under-match live work."""
        self.assertEqual(landreq._stem("lane/feature-review"), "feature")
        self.assertEqual(landreq._stem("refs/heads/lane/feature-re-review"),
                         "feature")
        self.assertEqual(landreq._stem("feature-build-r2"), "feature")
        self.assertEqual(landreq._stem("wt/feature-d1234abcd"), "feature")
        self.assertTrue(landreq._stems_match(
            landreq._stem("lane/feature-review"),
            landreq._stem("feature-build")))
        # Round 3 (codex): `_stem` slices the ORIGINAL case for evidence, so
        # the match itself must casefold — canonical identity already calls
        # these one family, and a case-sensitive liveness compare called
        # them strangers.
        self.assertTrue(landreq._stems_match(
            landreq._stem("LANE/Feature-review"),
            landreq._stem("feature-build")))
        # Negative control: a genuinely different family stays a stranger.
        self.assertFalse(landreq._stems_match(
            landreq._stem("lane/gauge-review"),
            landreq._stem("ledger-build")))

    def test_moved_tip_marker_probes_the_stripped_lane_branch(self):  # noqa: VACUOUS_ASSERTION — the assertIn positive control runs once per member of a two-element literal tuple; the loop cannot be empty
        """Finding 5: refs/heads/lane/ + the RAW stored lane yields
        lane/lane/foo for a historical prefixed row, so the marker silently
        never spoke for exactly the rows most likely to have moved. The
        branch namespace already carries lane/."""
        self.git("checkout", "-q", "-b", "lane/foo", self.main)
        reviewed = self.commit("reviewed here", path="mv1")
        self.commit("moved beyond review", path="mv2")
        self.git("checkout", "-q", self.main)
        for spelling in ("lane/foo", "foo"):
            marker = landreq._moved_tip_marker(
                {"reviewed_tip": reviewed, "lane": spelling}, None, self.repo)
            self.assertIn("MOVED +1", marker,
                          "marker silent for lane spelling %r" % spelling)

    def test_out_of_scope_refuses_when_the_family_lives_under_another_case(self):
        """Round 3 (codex), THE DOOR ITSELF, not helper equality: a live
        family ref spelled LANE/Feature-review guards a feature-build row —
        canonical identity calls them ONE family, and the case-sensitive
        liveness compare called them strangers, so the irreversible
        out-of-scope door would have closed a row whose work is live on
        disk. The door must refuse and write nothing."""
        row = self.dispatch(lane="feature-build")
        self.git("branch", "lane/Feature-review", self.side)
        # The control posture from CloseOutOfScopeTest: no discharger, so
        # nothing upstream of the liveness block refuses for its own reason.
        with mock.patch.object(dispatches, "discharging_row",
                               return_value=(None, None, "nothing records it")):
            rc, _out, err = run(["close", row["id"][:12], "--reason",
                                 "out-of-scope", "--evidence",
                                 "case-crossing probe"])
        self.assertEqual(rc, 1)
        self.assertIn("live at", err)
        self.assertIn("lane/Feature-review", err,
                      "the refusal must NAME the ref as it is spelled")
        snap = dispatches.snapshot()[0][row["id"]]
        self.assertEqual(snap["status"], "open", "nothing may be written")


class SuccessionIsNotContraryOnlyTest(unittest.TestCase):
    """contrary_discharge is scoped to CONTRARY rows — `rows = [l for l in
    out.values() if l.get("contrary")]` — so a row DISCHARGED BY SUCCESSION
    that was never contrary gets no stamp and no surface can tell.

    MEASURED ON THE LIVE BOARD: six in-flight rows sat stalled and
    owed_by=author for up to FIVE AND A HALF DAYS. Every one had been cured on
    a successor and APPROVED by a second reviewer. Nothing was ever going to
    notice them, because the only machinery that could was reading a different
    class of row.

    THE THIRD STATE IS THE POINT. A row with no chain_root cannot be answered
    by any chain-based rule, and the five-day row is exactly that one. Calling
    it "not discharged" is a confident negative over a skipped case."""

    # THE FIXTURES CARRY entered_ts, BECAUSE THE REAL ROWS DO. An LR projection
    # row has no `ts` field at all — measured, zero of 1042 — and the first cut
    # of the ordering rule keyed on `ts`, so every real row became unorderable
    # and 405 MOVED collapsed to zero while these fixtures stayed green. A
    # fixture that invents a field tests a schema the projection does not have.
    T0 = "2026-01-01T00:00:00Z"
    T1 = "2026-02-01T00:00:00Z"
    T2 = "2026-03-01T00:00:00Z"

    def _chain(self, rows):
        chain = {}
        for r in rows:
            root = str(r.get("chain_root") or "")
            if root:
                chain.setdefault(root, []).append(r)
        return chain

    def test_a_citing_approve_on_trunk_reads_MOVED(self):
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        state, carrier = landreq.succession_facts(
            me, self._chain([me, sib]), {"bbb"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(carrier["relation"], "supersedes-ancestry")

    def test_a_citing_approve_that_never_LANDED_does_not_move(self):  # noqa: VACUOUS_ASSERTION — the same citing row is asserted MOVED with its tip in reach before HELD with reach empty.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        rows = [me, sib]
        self.assertEqual(
            landreq.succession_state(me, self._chain(rows), {"bbb"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain(rows), set()),
            landreq.SUCCESSION_HELD)

    def test_a_NON_approve_citation_does_not_move_the_chain(self):  # noqa: VACUOUS_ASSERTION — the same citation is asserted MOVED after flipping only polarity to approve before FIX is asserted HELD.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "fix", "supersedes": "me"}
        self.assertEqual(
            landreq.succession_state(
                me, self._chain([me, dict(sib, polarity="approve")]), {"bbb"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"bbb"}),
            landreq.SUCCESSION_HELD)

    def test_a_ROOTLESS_row_is_UNKNOWN_and_never_HELD(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual(..., SUCCESSION_MOVED) on a ROOTED row with the same sibling and reach set runs before the loop.
        """The five-day row. No chain root means no chain-based rule can
        answer, and answering "held" anyway is the confident negative that let
        it alarm for days with nobody able to see why."""
        # MUST-HIT CONTROL: the SAME sibling and reach set move a ROOTED row,
        # so UNKNOWN below is about the missing root and nothing else.
        rooted = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
                  "entered_ts": self.T0}
        control_sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                       "polarity": "approve", "supersedes": "me"}
        self.assertEqual(
            landreq.succession_state(rooted, self._chain([rooted, control_sib]),
                                     {"bbb"}),
            landreq.SUCCESSION_MOVED)
        for root in (None, "", dispatches.CHAIN_UNKNOWN):
            with self.subTest(root=root):
                me = {"id": "me", "chain_root": root, "reviewed_tip": "aaa",
                      "entered_ts": self.T0}
                sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                       "polarity": "approve", "entered_ts": self.T1}
                chain = self._chain([me, sib])
                self.assertEqual(
                    landreq.succession_state(me, chain, {"bbb"}),
                    landreq.SUCCESSION_UNKNOWN)
                reasons = set()
                self.assertEqual(
                    landreq._succession(me, chain, {"bbb"}, reasons=reasons)[0],
                    landreq.SUCCESSION_UNKNOWN)
                self.assertEqual(reasons, {"identity"})
                landreq._annotate_succession(
                    {me["id"]: me}, {me["id"]: me, sib["id"]: sib}, {"bbb"},
                    landed=mock.Mock(return_value=landreq.PROOF_ANCESTOR))
                self.assertEqual(me["succession_unknown_reason"],
                                 "chain identity is missing or unreadable")

    def test_a_row_is_not_its_own_successor(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertEqual(..., SUCCESSION_MOVED) with a real sibling on the same approved tip runs first, so HELD is the identity skip and not a dead walk.
        """A single-row chain has nobody to carry it. Without the identity
        skip a row approving its own tip would report its own chain moved."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "polarity": "approve", "entered_ts": self.T0}
        # MUST-HIT CONTROL: add a REAL sibling with the same approved tip and
        # it moves, so HELD below is the identity skip and not a dead walk.
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "aaa",
               "polarity": "approve", "entered_ts": self.T1}
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"aaa"}),
            landreq.SUCCESSION_MOVED)
        self.assertEqual(
            landreq.succession_state(me, self._chain([me]), {"aaa"}),
            landreq.SUCCESSION_HELD)

    def test_supersedes_ancestry_carries_regardless_of_timestamp_order(self):
        target = {"id": "target", "chain_root": "R", "reviewed_tip": "aaa",
                  "entered_ts": self.T2}
        middle = {"id": "middle", "chain_root": "R", "reviewed_tip": "bbb",
                  "polarity": "fix", "supersedes": "target",
                  "entered_ts": self.T1}
        carrier = {"id": "carrier", "chain_root": "R", "reviewed_tip": "ccc",
                   "polarity": "approve", "supersedes": "middle",
                   "entered_ts": self.T0}
        rows = [target, middle, carrier]
        state, evidence = landreq.succession_facts(
            target, self._chain(rows), {"ccc"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(evidence["id"], "carrier")
        self.assertEqual(evidence["relation"], "supersedes-ancestry")

    def test_tip_lineage_is_tri_state_and_measured(self):
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa"}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve"}
        rows = [me, sib]
        unseen = set()
        self.assertEqual(
            landreq.succession_facts(
                me, self._chain(rows), {"bbb"}, {}, unseen)[0],
            landreq.SUCCESSION_UNKNOWN)
        self.assertEqual(unseen, {("aaa", "bbb")})
        state, carrier = landreq.succession_facts(
            me, self._chain(rows), {"bbb"}, {("aaa", "bbb"): True})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertEqual(carrier["relation"], "tip-descendant")
        self.assertEqual(
            landreq.succession_state(
                me, self._chain(rows), {"bbb"}, {("aaa", "bbb"): False}),
            landreq.SUCCESSION_HELD)

    def test_a_later_UNRELATED_approve_does_not_carry(self):  # noqa: VACUOUS_ASSERTION — the same approve is asserted MOVED when it cites this row before the unrelated citation is asserted HELD with carrier None.
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "other",
               "entered_ts": self.T2}
        direct = dict(sib, supersedes="me")
        self.assertEqual(
            landreq.succession_state(
                me, self._chain([me, direct]), {"bbb"},
                {("aaa", "bbb"): False}),
            landreq.SUCCESSION_MOVED)
        current = {"me": me, "sib": sib,
                   "other": {"id": "other", "chain_root": "R"}}
        state, carrier = landreq.succession_facts(
            me, self._chain(current.values()), {"bbb"},
            {("aaa", "bbb"): False}, current=current)
        self.assertEqual(state, landreq.SUCCESSION_HELD)
        self.assertIsNone(carrier)

    def test_the_carrier_EVIDENCE_rides_with_the_state_from_one_walk(self):
        """A close verb must record WHICH row carried the work and at WHAT tip.
        Computing that separately would be a second chain walk that has to
        agree with the first forever — the exact shape this lane removes one
        layer down, so the primitive returns both."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
               "polarity": "approve", "supersedes": "me"}
        state, carrier = landreq.succession_facts(
            me, self._chain([me, sib]), {"bbb"})
        self.assertEqual(state, landreq.SUCCESSION_MOVED)
        self.assertIsNotNone(carrier, "MOVED named no carrier")
        self.assertEqual(carrier["id"], "sib")
        self.assertEqual(carrier["reviewed_tip"], "bbb")
        self.assertEqual(carrier["relation"], "supersedes-ancestry")
        # ...and succession_state reads the SAME walk rather than redoing it.
        self.assertEqual(
            landreq.succession_state(me, self._chain([me, sib]), {"bbb"}),
            state)

    def test_only_MOVED_names_a_carrier(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone on a MOVED carrier runs before the loop, using the same fixture with the polarity flipped, so the Nones below cannot pass on a walk that never names anyone.
        """HELD has none by definition and UNKNOWN cannot name one without
        inventing it — a close verb that read a carrier off either would be
        recording a fact nobody established."""
        me = {"id": "me", "chain_root": "R", "reviewed_tip": "aaa",
              "entered_ts": self.T0}
        held_sib = {"id": "sib", "chain_root": "R", "reviewed_tip": "bbb",
                    "polarity": "fix", "supersedes": "me"}
        # MUST-HIT CONTROL: the same fixture with an approve DOES name one.
        moved_state, moved_carrier = landreq.succession_facts(
            me, self._chain([me, dict(held_sib, polarity="approve")]), {"bbb"})
        self.assertEqual(moved_state, landreq.SUCCESSION_MOVED)
        self.assertIsNotNone(moved_carrier)
        for lr, chain_rows in ((me, [me, held_sib]),
                               ({"id": "me", "chain_root": None,
                                 "entered_ts": self.T0}, [me])):
            with self.subTest(row=lr.get("chain_root")):
                _st, carrier = landreq.succession_facts(
                    lr, self._chain(chain_rows), {"bbb"})
                self.assertIsNone(carrier, "a non-MOVED state named a carrier")

    def test_the_stall_alarm_yields_to_MOVED_but_not_to_HELD(self):
        """Display only, and the two directions together are the whole claim.
        A stall nobody can unstall is noise; a stall someone still owes is the
        alarm doing its job."""
        base = {"id": "x", "state": "CHANGES_REQUESTED", "stalled": True,
                "contrary": False, "polarity": "fix", "lane": "lane/x",
                "branch": "lane/x", "review_sha": "a" * 12, "author": "a",
                "reviewer": "r", "dwell_s": 60, "land_state": "ABSENT",
                "landed": False, "observable": True}
        moved = dict(base, succession_state=landreq.SUCCESSION_MOVED)
        held = dict(base, succession_state=landreq.SUCCESSION_HELD)
        unknown = dict(base, succession_state=landreq.SUCCESSION_UNKNOWN)
        def as_text(lr):
            out = landreq._line(lr)
            return out if isinstance(out, str) else " ".join(out)
        self.assertNotIn("STALLED", as_text(moved),
                         "a chain that moved on still alarmed")
        self.assertIn("STALLED", as_text(held),
                      "a genuinely held row stopped alarming")
        # ...and the unknowable one says so rather than asserting either.
        txt = as_text(unknown)
        self.assertIn("STALLED", txt)
        self.assertIn("succession UNKNOWN", txt,
                      "an unanswerable row rendered as a confident stall")


class FrontierDebtTest(unittest.TestCase):
    """Mechanism A bills the FRONTIER, never the parent.

    The first draft narrowed `_absorbs_debt` so a live carrier stopped
    absorbing unless its own tip reached trunk. Measured on the live board,
    5 of 5 parents that un-suppressed had their live carrier ALSO on the
    board, so every one rendered the same chain twice — the exact thing
    `_loop_rows` forbids. So board membership is untouched and the fact moves
    to the row a reader can act on."""

    row = ChainFoldingTest.row
    board = ChainFoldingTest.board
    loop_ids = ChainFoldingTest.loop_ids

    def _vcs(self, landed_by_tip):
        """Patch the three real seams the annotation reads, nothing more."""
        class _BE:
            def landed_state(_s, root, tip, ref, **kw):
                return landed_by_tip.get(tip, "not-ancestor")
        from helm import vcs
        p1 = mock.patch.object(vcs, "backend", lambda root: _BE())
        p2 = mock.patch.object(landreq, "_trunk_refs",
                               lambda gd, cache: ("main", "origin/main"))
        p3 = mock.patch.object(landreq, "_origin_configured", lambda gd: True)
        # the repo can resolve its own trunk object; the no-pin arm below turns
        # this OFF deliberately, so it is a seam and not a blanket stub.
        p4 = mock.patch.object(landreq, "_git", return_value=mock.Mock(
            returncode=0, stdout="f" * 40 + "\n"))
        for p in (p1, p2, p3, p4):
            p.start(); self.addCleanup(p.stop)

    def _annotate(self, *rows, **landed):
        lrs, raw = self.board(*rows)
        self._vcs(landed.get("landed_by_tip") or {})
        landreq._annotate_frontier_debt(lrs, raw)
        return lrs, raw

    def _live(self, rid, tip, **kw):
        return self.row(rid, reviewed_tip=tip, repo_id="/r/.git", **kw)

    def _owed(self, lr):
        """The ids a row is billed for — the dicts carry the proofs."""
        return [d["id"] for d in lr["frontier_debt"]]

    def _proofs(self, lr):
        return [d["carrier_discharge_proof"] for d in lr["frontier_debt"]]

    # ---- the primitive's own contract ----------------------------------

    def test_descend_STOPS_at_a_hit_and_does_not_read_past_it(self):
        """Pinned as a contract of `_descend`, not of today's callers.

        A mutation removing the stop broke NO caller test, because no current
        question can observe it — a board row cannot have a carrying
        descendant, since such a descendant would relieve it and a relieved
        row is not on the board (measured 0 of 16 live). So the guarantee is
        asserted here directly, and a fourth question added later inherits it
        deliberately rather than by luck."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, raw = self.board(a, b, c)
        kids, node, err = landreq._chain_forest(raw, lrs)
        self.assertIsNone(err)
        # every descendant qualifies, so a walk that reads past a hit returns
        # BOTH while a stopping walk returns only the nearest.
        hits = landreq._descend(a["id"], kids, node, lambda _c, _r: True)
        self.assertEqual(hits, ["b"])
        # MUST-HIT control on the same call: a predicate that never fires
        # reaches the whole subtree, so the ["b"] above is the STOP and not an
        # inability to walk.
        none = landreq._descend(a["id"], kids, node, lambda _c, _r: False)
        self.assertEqual(none, [])
        seen = landreq._descend(a["id"], kids, node,
                               lambda cid, _r: cid == "c")
        self.assertEqual(seen, ["c"])

    # ---- the load-bearing property -------------------------------------

    def test_board_membership_is_UNTOUCHED_by_the_annotation(self):
        """The whole ruling. If this fails the mechanism double-bills."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        before = self.loop_ids(self.board(a, b, c))
        # POSITIVE CONTROL FIRST, on the observable being compared: the board
        # is NON-EMPTY, so the equality below cannot be satisfied by two empty
        # lists from a projection that fell over.
        self.assertEqual(before, ["c"])
        lrs, raw = self._annotate(a, b, c)
        after = self.loop_ids((lrs, raw))
        self.assertEqual(before, after)
        # and the annotation DID bill on this same fixture, so "membership
        # unchanged" is not the trivial truth of a no-op function.
        self.assertEqual(self._owed(lrs["c"]), ["a", "b"])

    def test_the_frontier_carries_its_TRANSITIVELY_suppressed_ancestors(self):
        """a <- b <- c, nothing landed. b is itself suppressed, so billing the
        immediate carrier would leave the only visible row (c) reporting ONE
        owed round out of two."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, _raw = self._annotate(a, b, c)
        self.assertEqual(self._owed(lrs["c"]), ["a", "b"])
        self.assertEqual(self._owed(lrs["a"]), [])
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_debt_NEVER_lands_on_a_row_the_board_will_not_render(self):
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, raw = self._annotate(a, b, c)
        visible = set(self.loop_ids((lrs, raw)))
        billed = {k for k, v in lrs.items() if v.get("frontier_debt")}
        self.assertTrue(billed, "no debt billed; the assertion below is vacuous")
        self.assertEqual(billed - visible, set(),
                         "a debt was parked on a row nobody can see")

    # ---- the git gate, both polarities ---------------------------------

    def test_an_ANCESTOR_carrier_relieves_and_bills_nobody(self):
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # POSITIVE CONTROL FIRST: the identical board with the carrier NOT
        # landed DOES bill, so the empty result below is about ancestry.
        base, _b = self._annotate(a, b, landed_by_tip={})
        self.assertEqual(self._owed(base["b"]), ["a"])
        lrs, _raw = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_PATCH_EQUIVALENT_relieves_exactly_as_ancestor_does(self):
        """The corrected rule. This repo cherry-picks lands, so the gated
        commit stays reachable from nothing while its content IS on trunk;
        refusing patch-equivalence would bill every cherry-picked land
        forever."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # POSITIVE CONTROL FIRST, same board and same call: with the carrier
        # NOT landed this DOES bill, so the empty result below is about
        # patch-equivalence and not a dead predicate.
        base, _b = self._annotate(a, b, landed_by_tip={})
        self.assertEqual(self._owed(base["b"]), ["a"])
        lrs, _raw = self._annotate(a, b,
                                   landed_by_tip={"tb": "patch-equivalent"})
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        self.assertEqual(self._owed(lrs["b"]), [])

    def test_a_DEEPER_landed_descendant_discharges_the_ancestor(self):
        """a <- b <- c where c landed. b landed nothing, but the DEBT is
        discharged, so billing b's frontier for a would over-count — measured
        15 of 20 over the live board before this walk continued past b."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        # POSITIVE CONTROL FIRST: with NOTHING landed the same three-row
        # chain bills, so the empty result below is about the deeper land.
        base, _b = self._annotate(a, b, c, landed_by_tip={})
        self.assertEqual(self._owed(base["c"]), ["a", "b"])
        lrs, _raw = self._annotate(a, b, c,
                                   landed_by_tip={"tc": "ancestor"})
        self.assertEqual(sorted(lrs), ["a", "b", "c"])   # a REAL board
        self.assertEqual(self._owed(lrs["c"]), [])

    # ---- the eight proof values, which must NOT collapse ----------------

    def test_CROSS_REPO_is_its_own_proof_and_never_suppresses(self):
        """repo_id carries FOUR distinct values in the live ledger, so this is
        today's data, not a hypothetical. One repository's trunk proves nothing
        about another's, and the reader must be able to tell "it is somewhere
        else" from "git could not read it" — that fact decides whether the debt
        is even ours to bill."""
        a = self.row("a", reviewed_tip="ta", repo_id="/r/.git")
        b = self.row("b", reviewed_tip="tb", repo_id="/OTHER/.git",
                     supersedes="a")
        # the carrier's tip WOULD read ancestor if anyone asked its own trunk;
        # the point is that nobody may.
        lrs, _raw = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(self._owed(lrs["b"]), ["a"])
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_CROSS_REPO])
        self.assertIsNone(lrs["a"]["carrier_discharge"])

    def test_NO_TIP_and_NO_REPO_are_distinct_from_unknown(self):
        a = self.row("a", reviewed_tip="ta", repo_id="/r/.git")
        b = self.row("b", repo_id="/r/.git", supersedes="a")     # no tip
        lrs, _raw = self._annotate(a, b)
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_NO_TIP])
        c = self.row("c", reviewed_tip="tc")                     # no repo_id
        d = self.row("d", supersedes="c")
        lrs2, _r2 = self._annotate(c, d)
        self.assertEqual(self._proofs(lrs2["d"]), [landreq.PROOF_NO_REPO])

    def test_NO_PIN_when_the_repo_cannot_prove_its_own_trunk_object(self):  # noqa: VACUOUS_ASSERTION — the same test first proves the readable-trunk relief control.
        """The positive control `_close_ladder_resolved` already applies at its
        rung 0, reused rather than restated: a repository that cannot prove its
        own trunk object relieves nothing."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        # MUST-HIT FIRST: with the trunk object provable, this board relieves.
        base, _b = self._annotate(a, b, landed_by_tip={"tb": "ancestor"})
        self.assertEqual(self._owed(base["b"]), [])
        lrs, raw = self.board(a, b)
        self._vcs({"tb": "ancestor"})
        with mock.patch.object(landreq, "_git", return_value=mock.Mock(
                returncode=1, stdout="")):
            landreq._annotate_frontier_debt(lrs, raw)
        self.assertEqual(self._proofs(lrs["b"]), [landreq.PROOF_NO_PIN])

    def test_PATCH_EQUIVALENT_is_RECORDED_never_flattened_to_ancestor(self):
        """Relief that cannot say WHICH proof carried it is not falsifiable —
        a later reader would have to re-run git to tell object-identity relief
        from delta-identity relief."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        lrs, _raw = self._annotate(a, b,
                                   landed_by_tip={"tb": "patch-equivalent"})
        # the proof lives on the row being CARRIED (a), because it answers
        # "did a's carrier discharge a" — b is the carrier, not the carried.
        self.assertEqual(lrs["a"]["carrier_discharge_proof"],
                         landreq.PROOF_PATCH_EQUIVALENT)
        self.assertNotEqual(lrs["a"]["carrier_discharge_proof"],
                            landreq.PROOF_ANCESTOR)
        self.assertIs(lrs["a"]["carrier_discharge"], True)
        # and a row NOBODY carries reads None rather than a fabricated value
        self.assertIsNone(lrs["b"]["carrier_discharge_proof"])
        self.assertIsNone(lrs["b"]["carrier_discharge"])

    def test_ONE_mark_per_frontier_not_one_per_ancestor(self):
        """A wall of per-ancestor marks on one row is the attention-budget
        failure in a different costume."""
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        c = self._live("c", "tc", supersedes="b")
        lrs, _raw = self._annotate(a, b, c)
        mark = lrs["c"]["frontier_debt_mark"]
        self.assertEqual(mark.count("THIS ROW CARRIES"), 1)
        self.assertIn("2 UNDISCHARGED ROUND(S)", mark)
        self.assertIn("still live debt", mark)
        # and every owed round is NAMED in that one mark, with its proof
        self.assertIn("a (", mark)
        self.assertIn("b (", mark)
        self.assertEqual(lrs["a"]["frontier_debt_mark"], "")

    # ---- fail-closed ----------------------------------------------------

    def test_an_UNTRUSTWORTHY_CHAIN_reads_unknown_never_an_empty_list(self):
        """"I checked and this frontier owes nothing" and "I could not check"
        are different answers; a caller must not be handed the reassuring one
        by default."""
        a = self._live("a", "ta", supersedes="b")
        b = self._live("b", "tb", supersedes="a")          # a cycle
        self._vcs({})
        # POSITIVE CONTROL FIRST, same call: a TRUSTWORTHY chain reads KNOWN
        # and bills, so the UNKNOWN below is about the cycle rather than about
        # a function that has stopped answering.
        c = self._live("c", "tc")
        d = self._live("d", "td", supersedes="c")
        lrs2, raw2 = self.board(c, d)
        landreq._annotate_frontier_debt(lrs2, raw2)
        self.assertEqual(lrs2["d"]["frontier_debt_state"],
                         landreq.FRONTIER_DEBT_KNOWN)
        self.assertEqual(self._owed(lrs2["d"]), ["c"])
        lrs, raw = self.board(a, b)
        landreq._annotate_frontier_debt(lrs, raw)
        self.assertEqual(sorted(lrs), ["a", "b"])   # lrs is a REAL board
        for r in lrs.values():
            self.assertEqual(r["frontier_debt_state"],
                             landreq.FRONTIER_DEBT_UNKNOWN)

    def test_a_RAISING_backend_is_unknown_not_a_silent_relief(self):
        from helm import vcs
        class _Boom:
            def landed_state(_s, *a, **k):
                raise OSError("planted")
        a = self._live("a", "ta")
        b = self._live("b", "tb", supersedes="a")
        lrs, raw = self.board(a, b)
        with mock.patch.object(vcs, "backend", lambda root: _Boom()), \
             mock.patch.object(landreq, "_trunk_refs",
                               lambda gd, cache: ("main", "origin/main")), \
             mock.patch.object(landreq, "_origin_configured", lambda gd: True):
            landreq._annotate_frontier_debt(lrs, raw)
        # a broken probe must not read as "landed" and quietly relieve
        self.assertEqual(self._owed(lrs["b"]), ["a"])


class StallsSayItAlreadyLandedTest(unittest.TestCase):
    """task/320 — the stall surface billed a named seat for 11h42m of delay on
    a row whose work was already on trunk.

    THE MISFIRE, from the integrator's transcript:
        652d9794afbe AWAITING_BUILD  parked-dispatch-rebi  11h42m  (STALLED)
          (>= 4h00m in AWAITING_BUILD, owed by builder (helm-claude))
    AWAITING_BUILD and a dwell clock are both claims about the LEDGER; neither
    asks whether the work exists.

    A MARKER, NOT A FILTER — an earlier cure dropped landed rows inside
    dispatches.owed() and took 18 gate failures, because the work-offer rung
    wants that same row to SURFACE so the seat CLOSES it. Speaking here changes
    what the reader is told and removes nothing.
    """

    ROW = {"id": "a" * 32, "kind": "review", "reviewed_tip": "f" * 40}

    def _probe(self, **kw):
        from helm import seats_work_offer
        return mock.patch.object(seats_work_offer, "_offer_landing_state", **kw)

    BUILD = {"id": "b" * 32, "kind": "build", "lane": "parked-widget-lane-x",
             "seq": 10}

    def _offchain(self, recurs, hits, row=None):
        """Drive offchain_landing with the two facts it consults."""
        def fake_git(gitdir, *args):
            # THE DOUBLE MUST BE AS WEAK AS GIT, NOT STRONGER (the review's
            # review of this very harness). It used to answer `grep in hits`
            # — EXACT SET MEMBERSHIP — which is the dashed-token boundary
            # production did not have, so the fixture implemented the fix and
            # the suite verified the FIXTURE. It now does what
            # `--fixed-strings` does: a SUBSTRING match, returning the real
            # `%H%x1e%B%x1f` record so the boundary filter under test is the
            # thing that decides.
            grep = next((a[len("--grep="):] for a in args
                         if a.startswith("--grep=")), "")
            out = "".join("%s\x1efold: %s at abc\n\x1f" % ("c" * 40, hit)
                          for hit in hits if grep and grep in hit)
            return types.SimpleNamespace(returncode=0, stdout=out)
        with mock.patch.object(landreq, "_lane_recurs_later",
                               return_value=recurs), \
                mock.patch.object(landreq, "_close_repo",
                                  return_value=("/repo", None)), \
                mock.patch.object(landreq, "_resolve_ref", return_value="main"), \
                mock.patch.object(landreq, "_git", fake_git):
            r = dict(row or self.BUILD)
            # COMPOSED EXACTLY AS PRODUCTION DOES (task/644 split the
            # detector into a structured probe plus a renderer so
            # stalebot can consume the finding without re-implementing
            # it). The arms still assert the RENDERED contract, so a
            # split that silently changed the sentence would redden.
            found = landreq.offchain_landing(r)
            return landreq._offchain_line(
                *found, rid=str(r.get("id") or "")) if found else ""

    def test_a_build_row_whose_STEM_is_cited_on_trunk_says_it_may_have_landed(self):  # noqa: VACUOUS_ASSERTION — the rung reads the assertEqual(miss, "") as an absence with no positive on the same observable. It has three, all on the SAME fixture and all unconditional: the exact-label hit and the stem hit both assert MAY HAVE LANDED OFF-CHAIN plus the durable row id, and they run through the identical _offchain() harness the miss uses — so a miss that came from an inert probe would take those two down with it. Mutation-proven: dropping the stem walk fails this arm.
        """task/430, and the arm is the specimen. Row 652d9794's lane label is
        `parked-dispatch-rebind-orphaned-tip` while trunk cites
        `dispatch-rebind-orphaned-tip` — ZERO hits on the label, ONE on the
        stem, because the orphan-disposition sweep RENAMED the label when it
        parked the row. A detector for "a renamed continuation is invisible to
        every same-lane rule" was defeated by a rename helm performs itself.

        BOTH CONTROLS, and the negative one is the whole contract: a MISS is
        UNKNOWN and must render NOTHING, never "not landed"."""
        miss = self._offchain(recurs=False, hits=set())
        self.assertEqual(miss, "", "a MISS must be silent, never 'not landed'")

        exact = self._offchain(recurs=False, hits={"parked-widget-lane-x"})
        self.assertIn("MAY HAVE LANDED OFF-CHAIN", exact)
        self.assertIn("cites lane 'parked-widget-lane-x'", exact)
        self.assertIn("b" * 12, exact, "the durable ROW id must be cited")

        stem = self._offchain(recurs=False, hits={"widget-lane-x"})
        self.assertIn("cites lane STEM 'widget-lane-x'", stem,
                      "a stem hit must not be reported as a label hit")
        self.assertIn("b" * 12, stem)

    def test_a_lane_that_RECURS_or_is_UNREADABLE_stays_silent(self):
        """A live chain is owed to somebody, and an unreadable ledger is not
        evidence of a dead lane — both must render nothing even when trunk
        cites the lane, or the rung tells a working seat their job is done."""
        # UNCONDITIONAL POSITIVE ON THE SAME FIXTURE FIRST: with recurs
        # FALSE this exact row and these exact hits DO produce a marker, so
        # every silence below is attributable to the recurs value rather than
        # to a probe that never fires.
        self.assertIn("MAY HAVE LANDED OFF-CHAIN",
                      self._offchain(recurs=False,
                                     hits={"parked-widget-lane-x"}))
        for recurs, why in ((True, "lane recurs in a later dispatch"),
                            (None, "ledger UNREADABLE")):
            with self.subTest(recurs=why):
                self.assertEqual(
                    self._offchain(recurs=recurs,
                                   hits={"parked-widget-lane-x"}), "", why)
        review = dict(self.BUILD, kind="review")
        self.assertEqual(
            self._offchain(recurs=False, hits={"parked-widget-lane-x"},
                           row=review), "",
            "a REVIEW row's ref is its reviewed tip; this rung is for BUILDs")

    def test_a_row_proven_on_trunk_SAYS_SO(self):
        # POSITIVE CONTROL FIRST, same row and same observable: with the probe
        # answering ABSENT the marker is silent, so the sentence below is the
        # landing answer rather than a marker that always fires.
        with self._probe(return_value=(False, "d" * 40)):
            self.assertEqual(landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD"), "")
        with self._probe(return_value=(True, "d" * 40)):
            out = landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD")
        self.assertIn("ALREADY ON TRUNK at " + "d" * 12, out)
        self.assertIn("CLOSING, not building", out)
        # THE DEBT IS REPLACED, NOT ARGUED WITH (the first finding):
        # the tail that stands in for the owed-by clause must say the row is
        # owed by nobody, or the routing claim survives its own correction.
        self.assertIn("owed by NOBODY", out)

    def test_the_VERB_comes_from_the_STATE_not_from_a_guess(self):
        """The second finding: the first cut said "not building" on
        every landed row, including AWAITING_REVIEW ones, naming an activity
        nobody was doing. OWED_BY already keys the ROLE off state; the verb
        has to come from the same place."""
        row = dict(self.ROW)
        with self._probe(return_value=(True, "d" * 40)):
            build = landreq._landed_marker(row, "AWAITING_BUILD")
            review = landreq._landed_marker(row, "AWAITING_REVIEW")
            other = landreq._landed_marker(row, "OPEN")
        self.assertIn("not building", build)
        self.assertNotIn("not reviewing", build)
        self.assertIn("not reviewing", review)
        self.assertNotIn("not building", review)
        # A state with no activity word says NEITHER rather than guessing one.
        self.assertNotIn("not building", other)
        self.assertNotIn("not reviewing", other)
        self.assertIn("needs CLOSING", other)

    def test_UNKNOWN_is_SILENT_never_a_denial(self):
        """The same tri-state law _moved_tip_marker states: an absent marker
        must never read as 'this did not land'."""
        row = dict(self.ROW)
        # UNCONDITIONAL POSITIVE CONTROL on the same row: the marker CAN fire
        # here, so the silence below is UNKNOWN being honoured rather than a
        # row this marker could never have spoken about.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK", landreq._landed_marker(row, "AWAITING_BUILD"))
        with self._probe(return_value=(None, None)):
            self.assertEqual(landreq._landed_marker(row, "AWAITING_BUILD"), "")

    def test_a_probe_that_RAISES_is_SILENT(self):
        """A marker that cannot measure says nothing. git being down is not
        evidence either way, and it must never become an accusation."""
        row = dict(self.ROW)
        # UNCONDITIONAL POSITIVE CONTROL on the same row, first.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK", landreq._landed_marker(row, "AWAITING_BUILD"))
        with self._probe(side_effect=RuntimeError("git unavailable")):
            self.assertEqual(landreq._landed_marker(row, "AWAITING_BUILD"), "")

    def test_a_BUILD_row_is_never_marked_on_its_BASE(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls are in-arm and deliberate: the forced-LANDED probe proves the marker CAN fire for this exact row, and the reached2 counter proves the same _close_repo patch DOES record a call for a row that legitimately reaches git, so both the empty string and the empty counter discriminate
        """THE TRAP THIS IS SHAPED AROUND, run against the REAL landing
        function with no mock so the short-circuit is proved, not assumed.

        A BUILD row's ref is the base the work was dispatched FROM. Measured
        on the live ledger, 48 of the 52 resolvable BUILD refs are
        ALREADY ANCESTORS OF TRUNK, so a raw landing probe would stamp this
        marker on nearly every AWAITING_BUILD row ever written — the defect
        installed as its own cure. A BUILD with no reviewed_tip must return
        UNKNOWN before any git call.
        """
        build = {"id": "b" * 32, "kind": "build", "ref": "f" * 40}
        # UNCONDITIONAL POSITIVE CONTROL on THIS EXACT ROW: with the probe
        # forced to LANDED the marker does fire for it, so the silence below
        # is the BUILD rule declining to answer and not a row shape the marker
        # rejects for some unrelated reason.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK",
                          landreq._landed_marker(build, "AWAITING_BUILD"))
        # AND IT MUST PROVE THE GIT CLAIM, NOT JUST THE ANSWER (the review's
        # third finding). An empty return is also what a BUILD row that DID
        # reach git and found nothing produces, so the silence proves nothing
        # on its own — the repo resolution has to be COUNTED.
        #
        # A RAISING DETONATOR WOULD BE INERT HERE AND I SHIPPED ONE FIRST:
        # _landed_marker catches Exception by law, and AssertionError IS an
        # Exception, so a side_effect that raises is SWALLOWED and the arm
        # passes whether or not git was reached. Counting is the only
        # instrument that survives a fail-open callee.
        reached = []
        with mock.patch.object(landreq, "_close_repo",
                               side_effect=lambda *a, **k:
                               reached.append(1) or (None, "err")):
            self.assertEqual(landreq._landed_marker(build, "AWAITING_BUILD"), "")
        self.assertEqual(reached, [], "a BUILD row with no reviewed_tip "
                                      "resolved a repo — the short-circuit is gone")
        # POSITIVE CONTROL ON THE COUNTER ITSELF: the same patch DOES record a
        # call for a row that legitimately reaches git, so the empty list above
        # is a short-circuit and not a counter that never fires.
        reached2 = []
        with mock.patch.object(landreq, "_close_repo",
                               side_effect=lambda *a, **k:
                               reached2.append(1) or (None, "err")):
            landreq._landed_marker({"id": "c" * 32, "kind": "build",
                                    "reviewed_tip": "f" * 40}, "AWAITING_BUILD")
        self.assertEqual(len(reached2), 1)

    def test_a_missing_store_row_is_SILENT(self):
        """store_rows.get(id) returns None for a row the dispatch store never
        held; that is an unmeasured row, not a landed one."""
        # UNCONDITIONAL POSITIVE CONTROL: a real dict DOES produce a marker
        # under the same probe, so the two silences below are the non-dict
        # guard and not a globally mute marker.
        with self._probe(return_value=(True, "d" * 40)):
            self.assertIn("ALREADY ON TRUNK",
                          landreq._landed_marker(dict(self.ROW), "AWAITING_BUILD"))
            self.assertEqual(landreq._landed_marker(None, "AWAITING_BUILD"), "")
            self.assertEqual(landreq._landed_marker("not-a-dict", "AWAITING_BUILD"), "")


class BatchedAncestryTest(ReceiptBase):
    """Two listings replace ~4400 per-pair spawns, with the tri-state intact.

    cProfiled: `lr list` spent 140 of 155 seconds in per-(tip, ref)
    `merge-base --is-ancestor` / cherry subprocesses. The batch answers from a
    reachable-set and an object-set; these arms pin that the ANSWERS are
    byte-identical to the per-pair path and that the sets actually replace the
    spawns — a green that only re-proved the answers would let a regression
    quietly reintroduce the spawn-per-row wall."""

    def test_batched_cells_match_per_pair_inside_a_scope(self):  # noqa: VACUOUS_ASSERTION — three exact tri-state equalities are the unconditional positive controls
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        with projscope.scope():
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.ANCESTOR)
            self.assertEqual(landreq._ancestry(gitdir, self.side, trunk),
                             landreq.NOT_ANCESTOR)
            # a well-formed sha in NEITHER listing is merge-base's rc-128 cell:
            # vanished is never folded into "not an ancestor"
            self.assertEqual(landreq._ancestry(gitdir, "d" * 40, trunk),
                             landreq.UNDETERMINED)

    @staticmethod
    def _counting_spawn(real, record):
        """One forwarding implementation for the performance and view arms."""
        def counting(gd, args, input_text=None, env=None):
            record(args, env)
            # Both arms exercise THIS forwarding, not lookalike closures that
            # can disagree about the environment while each stays green.
            return real(gd, args, input_text, env=env)
        return counting

    def test_the_listings_replace_the_per_pair_spawns(self):
        # THE PERF CLAIM ITSELF, as a spawn count. MUST-HIT control: outside a
        # scope the same question still spawns merge-base, so the zero inside
        # the scope is a measurement and not a broken counter.
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        spawned = []
        counting = self._counting_spawn(
            landreq._git_spawn,
            lambda args, env: spawned.append(tuple(args[:2])))

        with mock.patch.object(landreq, "_git_spawn", side_effect=counting):
            with projscope.scope():
                for tip in (self.b, self.side, self.b, self.side, "d" * 40):
                    landreq._ancestry(gitdir, tip, trunk)
                self.assertNotIn(("merge-base", "--is-ancestor"), spawned)
                listings = [a for a in spawned
                            if a[0] in ("rev-list", "cat-file")]
                self.assertEqual(len(listings), 2)
            spawned.clear()
            landreq._ancestry(gitdir, self.b, trunk)
            self.assertIn(("merge-base", "--is-ancestor"), spawned)

    def test_the_original_closure_forwards_env_and_the_memo_keeps_views_apart(self):
        """The performance spy asks identical argv under two explicit views.

        The shared factory keeps the forwarding under test identical to the
        performance arm's. Three asks in one memo scope must spawn twice and
        preserve the two different answers, not just record different envs.
        """
        gitdir = self.gitdir()
        child = self.git("rev-parse", "HEAD")
        parent = self.git("rev-parse", "HEAD^")
        info = os.path.join(gitdir, "info")
        os.makedirs(info, exist_ok=True)
        graft = os.path.join(info, "grafts")
        with io.open(graft, "w") as fh:
            fh.write(child + "\n")

        seen = []
        counting = self._counting_spawn(
            landreq._git_spawn,
            lambda args, env: seen.append((tuple(args), dict(env or {}))))
        grafted_env = {"GIT_GRAFT_FILE": graft}
        original_env = {"GIT_GRAFT_FILE": os.devnull}
        argv = ("--no-replace-objects", "rev-list", "--parents", "-1", child)
        with mock.patch.object(landreq, "_git_spawn", side_effect=counting), \
                projscope.scope():
            grafted = landreq._git(gitdir, *argv, env=grafted_env)
            original = landreq._git(gitdir, *argv, env=original_env)
            repeat = landreq._git(gitdir, *argv, env=grafted_env)

        self.assertIsNotNone(grafted)
        self.assertIsNotNone(original)
        self.assertEqual((grafted.returncode, original.returncode), (0, 0))
        # Exact records prove both views spawned, with the SAME argv; the
        # third ask must reuse the first answer rather than spend another git.
        self.assertEqual(seen, [(argv, grafted_env), (argv, original_env)])
        self.assertIs(repeat, grafted)
        # Behaviour, not merely captured env: dropping the overlay in the
        # shared spy makes these answers equal and loses the original parent.
        self.assertEqual(grafted.stdout.split(), [child])
        self.assertEqual(original.stdout.split(), [child, parent])

    def test_the_counting_wrapper_carries_the_two_graft_views_apart(self):  # noqa: VACUOUS_ASSERTION — the object-view call list is asserted NON-EMPTY before any absence, and both patch-id answers are exact equalities
        """THE NAMED MUST-HIT: env is FORWARDED, and the forwarding is
        MEASURED rather than merely present.

        A spy that accepts env and drops it runs the real spawn under a
        different view of the repository than production asked for, and every
        arm over it stays green because the double still runs. So this arm
        goes through the same wrapper and asks a question whose ANSWER DIFFERS
        BETWEEN THE TWO VIEWS: with a legacy graft installed, the object view
        still sees the child's parent and the ambient view does not.
        """
        gitdir = self.gitdir()
        child = self.git("rev-parse", "HEAD")
        info = os.path.join(gitdir, "info")
        os.makedirs(info, exist_ok=True)
        with io.open(os.path.join(info, "grafts"), "w") as fh:
            fh.write(child + "\n")

        seen = []
        real = landreq._git_spawn

        def counting(gd, args, input_text=None, env=None):
            seen.append((tuple(args), tuple(sorted((env or {}).items()))))
            return real(gd, args, input_text, env=env)

        with mock.patch.object(landreq, "_git_spawn", side_effect=counting):
            grafted = landreq._measured_patch_id(gitdir, child)

        # THE OBJECT VIEW REACHED THE SPAWN. Asserted non-empty first, so the
        # per-call checks below are over a population that exists.
        object_view = [a for a, env in seen
                       if "--no-replace-objects" in a
                       and ("GIT_GRAFT_FILE", os.devnull) in env]
        self.assertTrue(object_view,
                        "no call reached the spawn under the object view — "
                        "the wrapper swallowed env")

        # AND THE ANSWER IS THE ONE THAT VIEW GIVES. Without the graft the
        # same call must produce the SAME id; a wrapper that dropped env would
        # have answered under the graft and produced the root-shaped one.
        os.remove(os.path.join(info, "grafts"))
        plain = landreq._measured_patch_id(gitdir, child)
        self.assertEqual(grafted, plain,
                         "the answer moved with the graft, so the forwarded "
                         "view did not reach git")
        self.assertEqual(plain[0], landreq.HASHED)

    def test_a_failed_listing_falls_back_and_is_not_sticky(self):  # noqa: VACUOUS_ASSERTION — both branches assert exact ANCESTOR/NOT_ANCESTOR equalities
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        real = landreq._git

        def failing(gd, *args, **kw):
            if args[:1] == ("rev-list",):
                return subprocess.CompletedProcess(args, 128, "", "boom")
            return real(gd, *args, **kw)

        with projscope.scope():
            with mock.patch.object(landreq, "_git", side_effect=failing):
                # the batch abstains; per-pair merge-base still answers
                self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                                 landreq.ANCESTOR)
            # forget() kept the key free: the listing recovers IN THIS SCOPE
            self.assertEqual(landreq._ancestry(gitdir, self.side, trunk),
                             landreq.NOT_ANCESTOR)

    def test_an_empty_listing_is_abstention_not_a_negative(self):  # noqa: VACUOUS_ASSERTION — the ANCESTOR equality is the positive control that the fallback answered
        # The both-ways mutation for the set model: an empty reachable-set
        # must abstain (fall back to the spawn), never read every tip as
        # NOT_ANCESTOR — the false-negative direction a landedness guard is
        # trusted-while-blind in.
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        real = landreq._git

        def empty(gd, *args, **kw):
            if args[:1] == ("rev-list",):
                return subprocess.CompletedProcess(args, 0, "", "")
            return real(gd, *args, **kw)

        with projscope.scope(), \
                mock.patch.object(landreq, "_git", side_effect=empty):
            self.assertEqual(landreq._ancestry(gitdir, self.b, trunk),
                             landreq.ANCESTOR)

    def test_a_short_ref_tip_still_answers_per_pair(self):  # noqa: VACUOUS_ASSERTION — the ANCESTOR equality is the positive control on the fallback path
        # Only full shas can use set membership; a branch NAME as the commit
        # falls back to the spawn path rather than reading as absent.
        gitdir, trunk = self.gitdir(), "refs/heads/" + self.main
        with projscope.scope():
            self.assertEqual(landreq._ancestry(gitdir, self.main, trunk),
                             landreq.ANCESTOR)


class ExistingReceiptTest(ReceiptValidationBase):
    """_existing_receipt's mapping from the STRICT RESOLVER's states.

    Subclasses ReceiptValidationBase to inherit valid_row()/write_rows(), so
    the CONFLICT cases below are built from REAL rows and resolved by the real
    `_receipt_for` — not from a mock agreeing with my own branch. The
    resolver's own semantics are ReceiptValidationTest's business; what is
    under test here is which of its answers may suppress a mint.

    ONLY R_LOCAL SUPPRESSES. ONLY R_NONE MINTS. Everything else is a
    NON-ABSENCE: the record says something, and what it says is not 'no
    receipt', so minting into it cannot improve it."""

    def test_a_unanimous_valid_set_is_witnessed(self):
        rid, rec = self.valid_row()
        self.write_rows(rec)
        row, why, _state = landreq._existing_receipt(rec["reviewed_tip"])
        self.assertIsNotNone(row, "a unanimous R_LOCAL set was not witnessed")
        self.assertIsNone(why)

    def test_NO_row_is_absent_and_mints(self):
        """valid_row() WRITES its row, so the absence case needs a tip nothing
        was ever recorded for — asking about the fixture's own tip finds the
        fixture."""
        rid, rec = self.valid_row()
        self.write_rows(rec)
        row, why, _state = landreq._existing_receipt("c" * 40)   # never recorded
        self.assertIsNone(row)
        self.assertIsNone(why, "an index that was READ and holds no row for "
                               "this tip is ABSENCE, so it mints")

    def test_a_valid_row_with_an_INVALID_SIBLING_does_not_witness(self):
        """The mixed-sibling case. The first implementation scanned for
        the first row passing _validate_receipt and returned it — laundering
        a contaminated index into 'witnessed'. The resolver calls this
        R_CONFLICT and refuses to cherry-pick the good row."""
        rid, rec = self.valid_row()
        self.write_rows(rec, dict(rec, topic="helm.chat"))
        row, why, _state = landreq._existing_receipt(rec["reviewed_tip"])
        self.assertIsNone(row, "a contaminated index was read as witnessed")
        self.assertIn("DISAGREE", why or "",
                      "the conflict was not named to the operator: %r" % why)

    def test_two_DIVERGENT_valid_rows_do_not_witness(self):
        """The divergent-valid case: two individually-valid rows binding
        DIFFERENT lands to one tip. A first-match scan returns whichever was
        appended first; the resolver calls it R_CONFLICT."""
        rid, rec = self.valid_row()
        second = dict(rec, trunk_sha="9" * 40)
        second["payload"] = landreq.land_payload(
            rec["lane"], rec["branch"], rec["reviewed_tip"], rec["patch_id"],
            "9" * 40)
        self.write_rows(rec, second)
        row, why, _state = landreq._existing_receipt(rec["reviewed_tip"])
        self.assertIsNone(row, "two rival lands resolved to one 'witnessed'")
        self.assertIn("DISAGREE", why or "")

    def test_a_REJECTED_row_is_not_absence(self):
        rid, rec = self.valid_row()
        self.write_rows(dict(rec, schema="helm.land/1"))
        row, why, _state = landreq._existing_receipt(rec["reviewed_tip"])
        self.assertIsNone(row)
        self.assertIn("FAILS strict replay", why or "",
                      "a corrupt record was treated as absence and would be "
                      "minted beside: %r" % why)

    def test_an_UNREADABLE_index_is_UNKNOWN_not_absent(self):
        """THE DISTINCTION THE WHOLE CURE RESTS ON: treating 'could not look'
        as 'no receipt' mints blind and produces the duplicate this change
        exists to stop."""
        # THE SHAPE THE PRODUCER ACTUALLY EMITS, read rather than guessed —
        # it took me three tries. _receipts_by_tip returns a DICT KEYED BY the
        # sentinel, {_RECEIPT_LEDGER_UNREADABLE: reason}, and _ledger_failure
        # tests `marker in index`. A bare "UNREADABLE" string and the bare
        # sentinel both feed shapes production never emits: the first silently
        # skips the branch, the second raises. A fixture proves the code
        # handles the input you gave it, never that the input occurs.
        with mock.patch.object(
                landreq, "_receipts_by_tip",
                return_value={landreq._RECEIPT_LEDGER_UNREADABLE:
                              "receipts ledger unreadable"}):
            row, why, _state = landreq._existing_receipt("a" * 40)
        self.assertIsNone(row)
        self.assertIn("UNKNOWN", why or "",
                      "an unreadable index did not say UNKNOWN: %r" % why)



class WarmLrReadTest(unittest.TestCase):
    """task/444 — `helm lr list` reads the warm projection instead of
    cold-replaying. MEASURED on the live instance: 18,621 ms cold, 3 ms warm.

    Every arm here drives the real `landreq.warm_lr_body` with a stubbed
    transport, because the property under test is the ACCEPTANCE RULE, not
    urllib."""

    FRESH = {"loops": [], "filed": {}, "read_ts": 1000.0,
             "read_age_s": 60, "ledger_age_s": 600}

    def setUp(self):
        """CLEAR THE SUITE'S HOME OVERRIDE, because it is load-bearing here.

        Importing this module points HELM_HOME at a temp dir so no test can
        touch the real ledger — and `warm_lr_body` refuses outright under an
        overridden home, since a running `helm web` resolved ITS ledger from
        ITS own environment. That guard is exactly what keeps the suite from
        reaching a live server, so these arms clear the variable to reach the
        reader at all, and the one arm ABOUT the override sets it back."""
        self._home = mock.patch.dict(os.environ, {}, clear=False)
        self._home.start()
        self.addCleanup(self._home.stop)
        for var in ("HELM_HOME", "MELD_HOME"):
            os.environ.pop(var, None)

    def _serve(self, body):
        """Stub urlopen with a context manager yielding `body` as JSON."""
        class _Resp:
            def __init__(self, payload):
                self._p = json.dumps(payload).encode("utf-8")
            def read(self, *a):
                return self._p
            def __enter__(self):
                return io.BytesIO(self._p)
            def __exit__(self, *e):
                return False
        import urllib.request
        return mock.patch.object(urllib.request, "urlopen",
                                 return_value=_Resp(body))

    def test_a_projection_that_saw_the_LEDGER_is_usable_however_old(self):
        """THE CORRECTION MEASUREMENT FORCED. My first cut gated on WALL AGE
        against the model's 120s cap, which reads sensibly and asks the wrong
        question: an idle `helm web` serves a projection that ages
        monotonically (measured 199s -> 247s over 48s) because nothing has
        written to the ledger, so the accelerator never fired at all.

        Freshness is about the INPUT. A projection computed AFTER the newest
        ledger write is CURRENT however long ago that was."""
        old_but_current = dict(self.FRESH, read_age_s=900, ledger_age_s=4000)
        with self._serve(old_but_current):
            body, why = landreq.warm_lr_body()
        self.assertIsNotNone(body, why)
        self.assertIsNone(why)

    def test_a_LEDGER_WRITE_AFTER_the_projection_refuses_however_young(self):
        """The other direction, and the pair is the point: sixty seconds old
        over a ledger written ten seconds ago is STALE despite being younger
        than anything the wall-clock gate would have rejected."""
        young_but_stale = dict(self.FRESH, read_age_s=60, ledger_age_s=10)
        with self._serve(young_but_stale):
            body, why = landreq.warm_lr_body()
        self.assertIsNone(body)
        self.assertIn("ledger changed", why)
        # UNCONDITIONAL POSITIVE CONTROL on the same call: the fresh fixture
        # is accepted, so this refusal discriminates rather than describing a
        # reader that refuses everything.
        with self._serve(self.FRESH):
            ok, _ = landreq.warm_lr_body()
        self.assertIsNotNone(ok)

    def test_a_TWO_HOUR_stale_projection_refuses_even_over_a_quiet_ledger(self):
        """The exact-tip probe (dispatch ee48cea6): raising the web
        serve-stale cap 120->600 silently widened this backstop 1h->5h
        through the inherited `_LR_HARD_TTL_S * 30`, and age=7200 over
        ledger_age=8200 rendered WARM — a two-hour-stale projection bypassing
        cold replay under a contract that promises one genuinely quiet hour.
        The backstop is now PINNED at 3600 and owes its own arm: the input-
        freshness gate PASSES here (the ledger is older than the projection,
        so the projection saw it), which leaves the backstop as the ONLY
        refusal that can fire."""
        two_hours_stale = dict(self.FRESH, read_age_s=7200, ledger_age_s=8200)
        with self._serve(two_hours_stale):
            body, why = landreq.warm_lr_body()
        self.assertIsNone(body)
        self.assertIn("replaying to be sure", why)
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: just UNDER
        # the hour, the same shape (ledger older than projection) is ACCEPTED
        # and returns ITS fields — so the refusal above is the backstop
        # discriminating by age, not the wall-clock gate creeping back in and
        # refusing every old-but-current projection (the defect
        # this reader was cured of).
        just_under = dict(self.FRESH, read_age_s=3599, ledger_age_s=8200)
        with self._serve(just_under):
            body, why = landreq.warm_lr_body()
        self.assertIsNotNone(body, why)
        self.assertEqual(body["read_age_s"], 3599)

    def test_every_refusal_path_NAMES_ITSELF(self):
        """An accelerator that silently does nothing is indistinguishable from
        a broken one — the operator waits the full cold price with no sign it
        was tried. Each refusal carries a reason the caller prints."""
        cases = [
            ({"warming": True}, "still building"),
            (dict(self.FRESH, unavailable="ledgers unreadable"), "unreadable"),
            (dict(self.FRESH, loops="not-a-list"), "no loops"),
            ({k: v for k, v in self.FRESH.items() if k != "read_age_s"},
             "did not state its age"),
            ({k: v for k, v in self.FRESH.items() if k != "ledger_age_s"},
             "did not state its ledger age"),
        ]
        for body, expect in cases:
            with self.subTest(expect=expect):
                with self._serve(body):
                    got, why = landreq.warm_lr_body()
                self.assertIsNone(got)
                self.assertIn(expect, why)
        # UNCONDITIONAL POSITIVE CONTROL: the same reader accepts a good body
        # and returns ITS fields, asserted against a literal. `assertIsNotNone`
        # alone is itself an absence assertion and proves nothing about a
        # reader stuck on some third answer.
        with self._serve(self.FRESH):
            self.assertEqual(landreq.warm_lr_body()[0]["read_age_s"], 60)

    def test_NOT_APPLICABLE_refusals_are_SILENT_not_reasons(self):
        """The two ordinary cases carry NO reason, and that distinction is the
        point: `no fast path here` is not a fault worth a line on every
        invocation, while `the fast path is present and unhealthy` is. A
        successful command stays quiet — the header already distinguishes a
        warm answer by saying WARM.

        Both arms are real: most boxes run no web surface at all, and a CLI
        under an overridden HELM_HOME is asking about a DIFFERENT ledger than
        any running instance serves."""
        # UNCONDITIONAL POSITIVE CONTROL FIRST, deliberately: a present-but-
        # unhealthy instance DOES carry a reason, so the silences below are a
        # deliberate distinction and not a reader that never speaks. It runs
        # BEFORE any os.environ patching so its result cannot depend on the
        # order these blocks happen to execute in — which is exactly how it
        # failed the first time I wrote it.
        with self._serve({"warming": True}):
            body, why = landreq.warm_lr_body()
        self.assertIsNone(body)
        self.assertIn("still building", why)

        import urllib.request
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            body, why = landreq.warm_lr_body()
        self.assertIsNone(body)
        self.assertIsNone(why, "a dead instance printed a line every run")

        with mock.patch.dict(os.environ, {"HELM_HOME": "/tmp/somewhere-else"}):
            body, why = landreq.warm_lr_body()
        self.assertIsNone(body)
        self.assertIsNone(why, "an overridden home printed a line every run")

    def test_a_payload_this_BUILD_CANNOT_RENDER_falls_back_and_prints_NOTHING(self):
        """THE CURE FOR THE RED GATE, and the property is partial output.

        `helm web` is a LONG-LIVED process of unknown vintage: the one running
        on this box predates this lane and serves cards without `terminal`, so
        the header's `inflight_rows` raised KeyError MID-PRINT and the
        accelerator became the new failure mode task/444 forbids.

        A key-list check at the door would drift out of sync with the renderer
        the first time the renderer read one more field. Proving it RENDERS
        cannot drift, because the proof IS the renderer — so the warm branch
        formats into a buffer and NOTHING reaches the terminal unless the
        whole listing formatted."""
        old_vintage = [{"id": "0123456789ab", "state": "READY",
                        "lane": "x", "stalled": False}]   # no `terminal`
        filed = {"total": 0, "open": 0, "held": 0, "landed": 0,
                 "closed": 0, "non_loop": 0, "in_flight": 0}
        buf = io.StringIO()
        # THE KEY IS PINNED. A bare assertRaises(KeyError) would pass on a
        # KeyError about anything at all — my first cut passed on a missing
        # `total` in the filed stub and proved nothing about vintage.
        with self.assertRaises(KeyError) as caught:
            with contextlib.redirect_stdout(buf):
                landreq._print_loop_list(old_vintage, filed, "STAMP")
        self.assertEqual(caught.exception.args[0], "terminal")
        # THE PROPERTY: the renderer raised BEFORE the header, so a caller
        # buffering it emits nothing at all — no half-printed board.
        self.assertEqual(buf.getvalue(), "")
        # UNCONDITIONAL POSITIVE CONTROL on the same renderer: a CURRENT-
        # vintage row renders and produces output, so the emptiness above is
        # the vintage and not a renderer that never writes.
        current = [dict(old_vintage[0], terminal=False, observable=True,
                        dwell_s=1, dwell_known=True, review_sha=None,
                        contrary=False, contrary_state=None, honored=False,
                        attest_state=None, attest_detail=None,
                        polarity=None, polarity_source=None, land_state=None)]
        ok = io.StringIO()
        with contextlib.redirect_stdout(ok):
            landreq._print_loop_list(current, filed, "STAMP")
        self.assertIn("helm lr", ok.getvalue())

    def test_card_carries_TERMINAL_so_one_predicate_serves_both_surfaces(self):
        """`inflight_rows` reads `terminal`, and `card()` dropped it — so the
        warm header could not do its honored/in-flight accounting. This file's
        own list header records what happens when the two surfaces compute
        that count separately: "one word still named two numbers". The datum
        is supplied at the PRODUCER so both keep asking one predicate."""
        import inspect
        src = inspect.getsource(landreq.card)
        self.assertIn('"terminal": lr["terminal"]', src)


class WarmJsonVintageFallbackTest(unittest.TestCase):
    """task/1821 — a warm body WITHOUT the raw-rows field must fall through
    to the cold replay, NAMED on stderr, never answered with an invented
    empty list. Absence has two producers that must read identically here:
    a running `helm web` older than the --json warm path, and a builder
    that refused the chain and REMOVED the field (web_land._lr_project)."""

    BODY = {"loops": [], "read_ts": 1000.0, "read_age_s": 5,
            "ledger_age_s": 600, "filed": {},
            "withheld": {"scope": "p", "scope_repo": "r", "scope_why": None,
                         "foreign": 0, "unresolved": 0, "by_project": {}}}

    def test_a_body_without_rows_falls_back_cold_and_says_so(self):
        with mock.patch.object(landreq, "warm_lr_body",
                               return_value=(dict(self.BODY), None)), \
             mock.patch.object(landreq, "board_scope",
                               return_value={"project": None, "repo_id": None,
                                             "why": "test scope"}), \
             mock.patch.object(landreq, "project_raw",
                               return_value=({}, {},
                                             "COLD PATH REACHED")) as cold:
            rc, out, err = run(["list", "--json"])
        # The fallback is NAMED, and the cold path actually ran — the
        # sentinel unavailable-reason proves project_raw was consulted
        # rather than the warm branch inventing an answer.
        self.assertIn("predates the --json warm path", err)
        self.assertTrue(cold.called, "the cold replay never ran")
        self.assertIn("COLD PATH REACHED", err)
        self.assertEqual(out.strip(), "", "rows were invented for a body "
                         "that carries none: %r" % out[:200])

    def test_a_body_WITH_rows_serves_warm_and_never_touches_the_ledger(self):
        """UNCONDITIONAL POSITIVE CONTROL for the arm above, and the perf
        property itself: a warm-served --json consults no ledger at all."""
        good = dict(self.BODY, rows=[{"id": "x" * 12, "state": "OPEN"}])
        def boom(*a, **k):
            raise AssertionError("cold path consulted on a warm-served --json")
        with mock.patch.object(landreq, "warm_lr_body",
                               return_value=(good, None)), \
             mock.patch.object(landreq, "project_raw", side_effect=boom):
            rc, out, err = run(["list", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), good["rows"])
        self.assertIn("WARM projection, computed 5s ago", err)


class ContestedTipTest(unittest.TestCase):
    """A LANE CAN READ READY ON ONE REVIEWER'S APPROVE WHILE ANOTHER REVIEWER'S
    FIX BINDS THE SAME TIP.

    Measured, and it nearly landed a proven data-loss defect: lr
    the todos-sweep lr row read READY on an APPROVE of tip that lane's reviewed tip while a T1
    cross-family read of THE SAME 40-HEX TIP returned FIX with a destructive
    TOCTOU. Both verdicts real, both binding that tip, and the land surface
    showed only the approve — the two reviews sat on DIFFERENT CHAINS and this
    projection consults the row's OWN verdict. The refusal was never stale and
    never lost; there was nowhere to put a second opinion.

    A TIP IS A CONTENT IDENTITY, so the join is on the tip and never the chain.
    """

    TIP = "e5d1add7b03f" + "0" * 28
    OK = {"tok": {"v": "4", "host": "a-host", "head": TIP}}
    ROW = {"state": "READY", "author": "alpha", "reviewer": "beta",
           "gate": "tok", "id": "own-row", "reviewed_tip": TIP}

    def contest(self, verdicts, lanes=None):
        """(verdicts, lanes) exactly as `contest_index` returns them."""
        return ({self.TIP: verdicts},
                {self.TIP: lanes if lanes is not None else {"a", "b"}})

    def word(self, verdicts, lanes=None, row=None):
        return landreq.ready_word(dict(self.ROW, **(row or {})),
                                  index=self.OK,
                                  contest=self.contest(verdicts, lanes))

    def test_a_clean_tip_is_PLAIN_READY(self):
        """THE CONTROL, unconditional and first: every assertion below is a
        negative, and a negative is worthless if the function cannot produce
        the positive. An approve from another chain is NOT a contest."""
        got = self.word([("approve", "beta", "own-row"),
                         ("approve", "gamma", "other-row")])
        self.assertEqual(got, "READY")

    def test_a_FIX_on_the_same_tip_from_another_chain_CONTESTS(self):
        """THE INCIDENT. The approve made it READY; the fix is on a row this
        projection never consults."""
        got = self.word([("approve", "beta", "own-row"),
                         ("fix", "codex", "other-chain")])
        self.assertEqual(got, "READY-CONTESTED")

    def test_a_SUPERSEDE_contests_too_but_CONCUR_does_not(self):
        """supersede refuses landing; CONCUR authorizes nothing and objects to
        nothing, so it must not gate. UNDECLARED is not a refusal either — an
        absent polarity is not a negative."""
        sup = self.word([("approve", "beta", "own-row"),
                         ("supersede", "codex", "x")])
        self.assertEqual(sup, "READY-CONTESTED")
        con = self.word([("approve", "beta", "own-row"),
                         ("concur", "codex", "x")])
        self.assertEqual(con, "READY")
        und = self.word([("approve", "beta", "own-row"),
                         ("", "codex", "x")])
        self.assertEqual(und, "READY")

    def test_a_row_NEVER_contests_itself(self):
        """If the row's own verdict counted, every READY row would contest
        itself the moment its polarity was anything but approve — and a rung
        true of every row carries no information, which is the defect this
        whole surface exists to cure."""
        got = self.word([("fix", "beta", "own-row")])
        self.assertEqual(got, "READY")

    def test_a_SHARED_BASE_is_reported_but_never_REFUSED(self):
        """THE FALSE POSITIVE I MEASURED BEFORE SHIPPING. Of 20 live tips
        carrying an approve and a refusal, 5 span 3+ lanes — one has FIFTEEN
        unrelated `*-sub-*` fan-out lanes at one base, where a single FIX would
        make fourteen strangers read CONTESTED. Refusing all 20 is 25% false,
        and a land surface that cries contested at unrelated work is muted in a
        day. The REPORT stays wide so nothing is hidden; only the REFUSAL
        narrows."""
        verdicts = [("approve", "beta", "own-row"), ("fix", "codex", "x")]
        many = {"lane-a", "lane-b", "lane-c", "lane-d"}
        self.assertEqual(self.word(verdicts, lanes=many), "READY")
        hits, span, err = landreq.contest_report(
            dict(self.ROW), index=self.contest(verdicts, many))
        self.assertIsNone(err)
        self.assertEqual(span, 4)
        self.assertEqual([h[0] for h in hits], ["fix"],
                         "the refusal is still REPORTED, just not gating")

    def test_an_UNREADABLE_ledger_is_UNVERIFIED_and_never_a_pass(self):
        """"No other verdict binds this tip" and "I could not look" are the
        same bytes to a reader, and only one of them is safe to land on."""
        def boom():
            return {}, {}, "dispatch ledger unreadable: boom"
        real = landreq.contest_index
        landreq.contest_index = boom
        self.addCleanup(setattr, landreq, "contest_index", real)
        self.assertEqual(
            landreq.ready_word(dict(self.ROW), index=self.OK),
            "READY-UNVERIFIED")

    def test_a_row_with_NO_reviewed_tip_has_nothing_to_join_on(self):
        """Absence of a tip is not a refusal — and it must not cost a ledger
        read either, which is why the guard returns before the index is built."""
        got = self.word([("fix", "codex", "x")], row={"reviewed_tip": None})
        self.assertEqual(got, "READY")


class ContestedTipRealCallerTest(LandReqBase):
    """The FIX, and it is the gap every other arm in this file had.

    THE FIRST SEVEN ARMS ALL INJECT the contest index, so none exercised the
    path production takes: `list` and `compose` call `ready_word(lr)` with NO
    index at all. A fixture proves the code handles the input you gave it,
    never that the real caller mints that shape — so a per-row ledger refold
    sat behind seven green tests and the "wide report" was rendered by nobody.

    HERMETIC, like the rest of this file: a SYNTHETIC ledger in a tmp HELM_HOME.
    My first draft read the LIVE ledger and failed for the right reason — this
    suite isolates HELM_HOME by design, so a live-data arm is environment
    coupling wearing a measurement's clothes."""

    TIP = "d4d1328d7da0" + "0" * 28
    # The receipt index is FAKED exactly as ContestedTipTest fakes it: this
    # class isolates the CONTEST path, and an unresolvable gate token would
    # make every row read UNVERIFIED before the contest rung is ever reached.
    # My first draft passed {} and BOTH arms failed — including the control,
    # which is how a control earns its place.
    OK = {"tok": {"v": "4", "host": "a-host", "head": TIP}}

    def seed(self, *rows):
        from helm import eventledger
        for r in rows:
            self.assertTrue(eventledger.append(dispatches.ledger_path(), r))
        landreq._CONTEST_MEMO.clear()

    def row(self, rid, polarity, lane, recipient="peer"):
        return {"v": 3, "event": "dispatch", "id": rid, "lane": lane,
                "kind": "review", "recipient": recipient, "sender": "someone",
                "reviewed_tip": self.TIP, "polarity": polarity,
                "ts": "2026-01-01T00:00:00Z", "status": "verdict"}

    def line_row(self, **over):
        """A row carrying every key `_line` reads, extracted from the function
        rather than discovered one KeyError at a time — shared by every arm
        that renders a whole line, so the next arm about ONE column does not
        re-transcribe the other twenty-four keys."""
        lr = dict.fromkeys(
            ("abandon_reason", "abandoned", "attest_state", "base_behind",
             "base_state", "chain_root", "close_contradicted", "close_reason",
             "closed_by_landing", "closed_ts_impossible", "consumed_by",
             "contrary", "contrary_discharge", "contrary_state", "discharged",
             "landing_proof_mode", "ledger_refused", "observable", "polarity",
             "polarity_source", "receipt_state", "stalled", "succession_state",
             "superseding_tip", "withdrawn"))
        lr.update({"state": "READY", "author": "a", "reviewer": "b",
                   "gate": "tok", "id": "own-row", "reviewed_tip": self.TIP,
                   "lane": "a-lane", "dwell_s": 0, "dwell_known": True,
                   "review_sha": self.TIP, "observable": True})
        lr.update(over)
        return lr

    def stub_index(self, verdicts, lanes):
        """Replace the ONE reader, so the arm measures whether the UNTHREADED
        caller reaches it — which is exactly the finding. The first draft
        hand-wrote ledger rows and the index came back EMPTY: the real producer
        emits a `dispatch` event and a `verdict` event SEPARATELY, and my
        fixture fused them. A fixture proves the code handles the input you
        gave it, never that production mints that shape — the same law this
        whole class exists for, caught on me while writing it."""
        real = landreq.contest_index
        landreq.contest_index = lambda: ({self.TIP: verdicts},
                                         {self.TIP: lanes}, None)
        self.addCleanup(setattr, landreq, "contest_index", real)

    def test_the_UNTHREADED_caller_still_discriminates(self):
        """`ready_word(lr)` with NO contest argument — exactly what `list` and
        `compose` do, and exactly the shape that was broken: they never passed
        an index, so every READY row refolded the ledger and all seven earlier
        arms hid it by injecting one."""
        self.stub_index([("approve", "beta", "own-row"),
                         ("fix", "codex", "other-chain")], {"lane-one", "lane-two"})
        lr = {"state": "READY", "author": "a", "reviewer": "b",
              "gate": "tok", "id": "own-row", "reviewed_tip": self.TIP}
        self.assertEqual(landreq.ready_word(lr, index=self.OK),
                         "READY-CONTESTED")

    def test_a_clean_tip_is_PLAIN_READY_on_the_same_unthreaded_path(self):
        """THE CONTROL on the same unthreaded path: without a refusal the same
        call is plain READY, so the assertion above is the fix and not the
        plumbing. It earned its place already — when the gate token could not
        resolve, BOTH arms failed and the control is what said so."""
        self.stub_index([("approve", "beta", "own-row"),
                         ("approve", "gamma", "other")], {"lane-one", "lane-two"})
        lr = {"state": "READY", "author": "a", "reviewer": "b",
              "gate": "tok", "id": "own-row", "reviewed_tip": self.TIP}
        self.assertEqual(landreq.ready_word(lr, index=self.OK), "READY")

    def test_a_SHARED_BASE_refusal_is_actually_RENDERED_not_just_computed(self):
        """The SECOND finding, and it is the sharper one. The REFUSAL was
        narrowed to a lane span <= 2 and claimed the REPORT stayed wide — but no
        renderer called `contest_report`, so a refusal on a busy tip was
        computed correctly and shown to NOBODY. A narrowing that hides what it
        declines to gate is not a narrowing; it is the silence this rung exists
        to end. The row now carries `?N`."""
        self.stub_index([("approve", "beta", "own-row"),
                         ("fix", "codex", "other-chain")],
                        {"l1", "l2", "l3", "l4"})          # span 4 -> not gated
        # `_line` resolves the gate token through the REAL receipt index, which
        # is empty in a hermetic home — so neutralise that path too, or the row
        # reads UNVERIFIED and never reaches the contest mark this arm is about.
        real_gi = landreq._gate_receipt_index
        landreq._gate_receipt_index = lambda: self.OK
        self.addCleanup(setattr, landreq, "_gate_receipt_index", real_gi)
        lr = self.line_row()
        # CONTROL FIRST: it is NOT gated — the word stays plain READY.
        self.assertEqual(landreq.ready_word(lr, index=self.OK), "READY")
        rendered = landreq._line(lr)
        self.assertIn("?1", rendered,
                      "a refusal too wide to GATE must still be SEEN")

    def test_the_MEMO_IS_USED_measured_by_counting_ledger_folds(self):
        """MUTATION-TESTED: remove the memo-hit return and ALL FOUR arms
        of the first version still passed. Asserting
        that the memo POPULATES never asserts that it is USED, and a suite that
        survives deleting the thing it tests reports green for a behaviour it
        never exercised — worse than no test, because it retires the question.

        THE RECIPE: seed a REAL dispatch event plus a later
        verdict event, wrap and COUNT snapshot_with_verdicts, call the
        UNTHREADED ready_word repeatedly, assert ONE fold with a populated
        contest. Counting folds is the only assertion the mutation cannot
        survive — and it needs the real two-event shape, which is the fixture
        error made twice before it was named."""
        approving = self.dispatch(ref=self.side, lane="lane/one")
        refusing = self.dispatch(ref=self.side, lane="lane/two")
        for row, polarity in ((approving, "approve"), (refusing, "fix")):
            current = dispatches.snapshot()[0][row["id"]]
            self.assertTrue(eventledger.append(dispatches.ledger_path(), {
                "v": 3, "event": "verdict", "seq": current["seq"] + 1,
                "id": row["id"], "ts": dispatches.pk.now_ts(),
                "reviewed_tip": self.side, "verdict_ref": "seeded",
                "polarity": polarity, "gate": "", "gate_caps": []}))
        landreq._CONTEST_MEMO.clear()

        folds = []
        real = dispatches.snapshot_with_verdicts
        dispatches.snapshot_with_verdicts = lambda: (folds.append(1), real())[1]
        self.addCleanup(setattr, dispatches, "snapshot_with_verdicts", real)
        real_gi = landreq._gate_receipt_index
        landreq._gate_receipt_index = lambda: {
            "tok": {"v": "4", "host": "a-host", "head": self.side}}
        self.addCleanup(setattr, landreq, "_gate_receipt_index", real_gi)

        lr = {"state": "READY", "author": "a", "reviewer": "b", "gate": "tok",
              "id": approving["id"], "reviewed_tip": self.side}
        words = [landreq.ready_word(lr) for _ in range(25)]      # UNTHREADED
        # POSITIVE FIRST: the contest is populated, so the fold count below is
        # about a read that FOUND something, not an empty short-circuit.
        self.assertEqual(set(words), {"READY-CONTESTED"},
                         "the seeded fix must contest the seeded approve")
        self.assertEqual(len(folds), 1,
                         "25 unthreaded ready_word calls folded the ledger %d "
                         "times — the memo is not being used" % len(folds))

    def test_the_MEASURED_combination_STALE_BASE_plus_span4_FIX_still_renders(self):
        """The round-three finding (gate:e19f67f0bba81887): the wide
        report rendered ONLY when the word was plain READY. Measured
        READY-STALE-BASE with a span-4 FIX — `contest_report` returned the
        refusal and `_line` emitted no ?1, so the promised wide report
        disappeared exactly when a SECOND rung also bit. A report gated on the
        healthiest word is not a report: the whole point of `?N` is that a
        refusal too wide to gate must still be SEEN, and that obligation does
        not lapse because the row has other problems too."""
        self.stub_index([("approve", "beta", "own-row"),
                         ("fix", "codex", "other-chain")],
                        {"l1", "l2", "l3", "l4"})          # span 4 -> not gated
        real_gi = landreq._gate_receipt_index
        landreq._gate_receipt_index = lambda: self.OK
        self.addCleanup(setattr, landreq, "_gate_receipt_index", real_gi)
        rendered = landreq._line(
            self.line_row(base_state="UNLANDED-STALE", base_behind=725))
        # CONTROL on the same render: the second rung really bit, so this row
        # took exactly the non-plain-READY path the old guard silenced.
        self.assertIn("READY-STALE-BASE", rendered)
        self.assertIn("?1", rendered,
                      "the wide report must render beside EVERY word, not "
                      "only plain READY")

    def test_a_CHANGES_REQUESTED_row_renders_the_wide_report_too(self):
        """EVERY word includes the refusal states. A CHANGES_REQUESTED row on
        a tip some OTHER chain also refused owes the reader that count — the
        author cures against ONE reviewer's findings, and a second refusal on
        the same tip is exactly the cross-chain fact this join exists to
        surface. `ready_word` returns non-READY states untouched, so only the
        renderer can carry the mark here."""
        self.stub_index([("fix", "codex", "other-chain")], {"l1", "l2", "l3"})
        rendered = landreq._line(self.line_row(state="CHANGES_REQUESTED"))
        self.assertIn("CHANGES_REQUESTED", rendered)       # the word survives
        self.assertIn("?1", rendered,
                      "a refusal word must not hide the OTHER chain's refusal")

    def test_the_mark_is_ABSENT_only_when_nothing_refuses(self):
        """The absence assertion, carrying its positive control on the SAME
        observable: the same row renders ?1 while a refusal is in the index,
        then the refusal is replaced by an approve and the mark must vanish —
        so the absence below is the join discriminating, never the mark being
        broken. (dwell_known is True in `line_row`, so no other '?' can reach
        this line and the substring is unambiguous.)"""
        real_gi = landreq._gate_receipt_index
        landreq._gate_receipt_index = lambda: self.OK
        self.addCleanup(setattr, landreq, "_gate_receipt_index", real_gi)
        lr = self.line_row(base_state="UNLANDED-STALE", base_behind=725)
        self.stub_index([("approve", "beta", "own-row"),
                         ("fix", "codex", "other-chain")],
                        {"l1", "l2", "l3", "l4"})
        self.assertIn(" ?1", landreq._line(lr))            # positive control
        self.stub_index([("approve", "beta", "own-row"),
                         ("approve", "gamma", "other")], {"l1", "l2"})
        self.assertNotIn(" ?", landreq._line(lr),
                         "an uncontested tip must carry no contest mark")

    def test_a_LANDED_rows_line_is_a_RECORD_the_live_index_cannot_annotate(self):
        """The round-four finding (gate:c2e3444cd037fe7b), measured by an
        independent fab probe: a LANDED row rendered `LANDED ... ?1`. The
        contest index is the CURRENT ledger, so the mark beside a terminal row
        is not history — it is a LIVE annotation retroactively applied to a
        closed record, and a FIX filed today re-words a row that landed last
        week. Re-measured against the live ledger at feb8b9cc before curing:
        54 terminal rows wore a mark, 617 LANDED rows were exposed to one.

        The positive control is on the SAME observable — the same index entry
        renders beside a NONTERMINAL row through the same `_line` — so the
        absence below is the terminal boundary discriminating, never the mark
        being broken."""
        self.stub_index([("fix", "codex", "other-chain")], {"l1", "l2"})
        self.assertIn(" ?1", landreq._line(self.line_row()))   # control: live
        rendered = landreq._line(self.line_row(state="LANDED", terminal=True))
        self.assertIn("LANDED", rendered)                  # the word survives
        self.assertNotIn(" ?", rendered,
                         "a terminal row is a record — live contest data "
                         "must not annotate closed history")

    def test_a_closed_READY_rows_stored_word_survives_a_live_refusal(self):
        """The second half of the same finding: `terminal` is the boundary,
        not the state WORD — the projection mints closed rows whose stored
        state is READY (closed-by-landing keeps the verdict state; measured
        104 live at feb8b9cc, FIVE reading READY-CONTESTED off refusals filed
        after they closed). A terminal stored-READY row must keep its stored
        word and carry no mark, however the live index moves.

        Both observables carry their own positive control on the same index
        entry: while the row is LIVE, the same refusal both re-words it and
        marks it — so the two absences below are the boundary, not a dead
        join."""
        self.stub_index([("fix", "codex", "other-chain")], {"l1", "l2"})
        live, closed = self.line_row(), self.line_row(terminal=True)
        self.assertEqual(landreq.ready_word(live, index=self.OK),
                         "READY-CONTESTED")                # control: word
        self.assertIn(" ?1", landreq._line(live))          # control: mark
        self.assertEqual(landreq.ready_word(closed, index=self.OK), "READY",
                         "a closed row's stored word must not be re-judged "
                         "by verdicts filed after it closed")
        self.assertNotIn(" ?", landreq._line(closed),
                         "a closed stored-READY row is a record too")

    def test_NO_live_rung_reaches_a_terminal_row_not_only_the_contest_one(self):
        """`ready_word` is scoped to nonterminal rows AS A WHOLE (the verdict's
        words: "Scope report and ready_word to nonterminal rows"), because
        every rung reads a live instrument — the contest join, the receipt
        ledger, `base_behind` — and a closed row's word must not change when
        an instrument does. Measured at feb8b9cc: 95 terminal stored-READY
        rows read READY-UNVERIFIED and two READY-SELF-REVIEW. This arm is
        also what keeps the `ready_rung` guard load-bearing under mutation:
        the contest door answers empty for terminal rows on its own, so only
        a non-contest rung can prove the ladder itself is gated."""
        self.stub_index([], set())         # no contest — isolate the ladder
        # controls: while LIVE, the same rows do wear their rungs.
        self.assertEqual(landreq.ready_word(self.line_row(gate="")),
                         "READY-UNVERIFIED")
        self.assertEqual(
            landreq.ready_word(self.line_row(author="a", reviewer="a"),
                               index=self.OK),
            "READY-SELF-REVIEW")
        self.assertEqual(landreq.ready_word(
            self.line_row(gate="", terminal=True)), "READY")
        self.assertEqual(landreq.ready_word(
            self.line_row(author="a", reviewer="a", terminal=True),
            index=self.OK), "READY")


class SpanCountsWorkNotLaneTest(LandReqBase):
    """THE CONTEST SPAN COUNTS DISTINCT WORK (chain_root), NOT LANE LABELS.

    task/734, found on the 186275c0 confirmation (gate ef44d754).
    The shared-base narrowing asks "how many distinct pieces of work cite this
    tip" and refuses only a small span (<= CONTEST_WORK_SPAN); a large span is
    a SHARED BASE a stranger's FIX must not gate. The identity was the
    renamable LANE LABEL, so the count lied two ways, and this is the same
    "renamed continuation is invisible to every same-lane rule" class that
    motivated --supersedes:

      (a) one reviewing chain that CONTINUES under a renamed lane counted as
          MANY, inflating its span past the shared-base line, so a binding FIX
          escaped in silence — the contest went quiet.
      (b) two UNRELATED chains that shared a lane label collapsed to ONE,
          deflating a genuine shared base under the line, so a FIX on one
          stranger FALSELY contested another.

    These arms run the REAL `contest_index` over a seeded hermetic ledger —
    injecting the (verdicts, works) pair would bypass the very construction
    under test (the mistake the seven earlier contest arms all made, named in
    `ContestedTipRealCallerTest`). Each asserts the rendered marker
    `ready_word` returns — the EFFECT a reader sees — and each carries a
    positive control on that SAME observable and the SAME real index: a
    genuine two-chain contest on another tip that must fire, so an arm's
    expectation is a real (mis)count and not a harness that can never say
    CONTESTED. Mutation-proven: revert the keying in `contest_index` from
    chain_root back to `lane` and BOTH arms redden while both controls stay
    green."""

    def OK(self):
        # The gate rung only checks `isinstance(index.get(token), dict)`; a
        # plausible receipt keeps the fixture honest. The contest rung bites
        # BEFORE this, so a CONTESTED row never consults it.
        return {"tok": {"v": "4", "host": "h", "head": self.side}}

    def _review(self, lane, polarity, tip, supersedes=None):
        """Seed a real reviewing row: a `dispatch` event (new work, or a
        continuation that INHERITS its parent's chain_root) plus a SEPARATE
        `verdict` event carrying the polarity and reviewed tip — the two-event
        shape production actually mints (fusing them makes `contest_index`
        come back empty; see the sibling class's `stub_index` note)."""
        kw = {"lane": lane}
        if supersedes:
            kw["supersedes"] = supersedes
        # The dispatch ref IS the reviewed tip: the verdict fold refuses a
        # `reviewed_tip` that diverges from the row's own tip (it folds to a
        # None polarity), so a review of `tip` must be dispatched against it.
        row = self.dispatch(ref=tip, **kw)
        cur = dispatches.snapshot()[0][row["id"]]
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "v": 3, "event": "verdict", "seq": cur["seq"] + 1, "id": row["id"],
            "ts": dispatches.pk.now_ts(), "reviewed_tip": tip,
            "verdict_ref": "seeded", "polarity": polarity,
            "gate": "", "gate_caps": []}))
        return row

    def _live(self, row, tip):
        """The land-surface view of an approving row: READY and gated, wearing
        its REAL id + chain_root so its own approve is excluded from the join
        and its chain is one of the works the span counts."""
        return {"state": "READY", "author": "a", "reviewer": "b",
                "gate": "tok", "id": row["id"],
                "chain_root": row.get("chain_root"), "reviewed_tip": tip}

    def _corrupt_chain(self, rid):
        """Plant a PRESENT malformed root in the real seeded ledger.

        Replay must type it as CHAIN_UNKNOWN; editing the stored first event is
        the only honest way to exercise that state because the dispatch writer
        correctly refuses malformed chain ids at its trust boundary.
        """
        path = dispatches.ledger_path()
        events = eventledger.events(path)
        hit = False
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                if ev.get("id") == rid and ev.get("event") == "dispatch":
                    ev["chain_root"] = "not-a-dispatch-id"
                    hit = True
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self.assertTrue(hit, "positive control: the seeded dispatch was corrupted")

    def test_a_renamed_continuation_of_a_FIX_still_contests(self):
        """ARM (a): a reviewing chain files a FIX, then CONTINUES the SAME
        work under a renamed lane. Keyed on the label the one chain wears two
        names, its span reads 3 (author + two review lanes), crosses the
        shared-base line, and the FIX escapes as PLAIN READY. Keyed on
        chain_root the continuation is one work, the span is 2, and the FIX
        still bites. RED at trunk (label -> "READY"), GREEN on the cure."""
        author = self._review("author-lane", "approve", self.side)      # live
        fix_v1 = self._review("review-v1", "fix", self.side)            # the FIX
        self._review("review-v2", "fix", self.side,                     # renamed
                     supersedes=fix_v1["id"])
        # CONTROL on the SAME observable + SAME real index, on another tip: a
        # genuine two-chain second opinion that MUST contest, so the arm below
        # is a real suppression and not a join that can never say CONTESTED.
        ctl = self._review("ctl-approve", "approve", self.c)
        self._review("ctl-fix", "fix", self.c)
        landreq._CONTEST_MEMO.clear()

        self.assertEqual(
            landreq.ready_word(self._live(ctl, self.c), index=self.OK()),
            "READY-CONTESTED",
            "positive control: a genuine two-chain contest must fire")
        self.assertEqual(
            landreq.ready_word(self._live(author, self.side), index=self.OK()),
            "READY-CONTESTED",
            "a FIX whose work continued under a renamed lane must STILL "
            "contest — one chain_root is one piece of work, however many "
            "lane labels it wore")

    def test_two_chains_sharing_a_lane_label_do_NOT_contest(self):
        """ARM (b): three UNRELATED chains cite one tip — a genuine shared
        base — and two happen to wear the SAME lane label. Keyed on the label
        the two collapse to one, the span reads 2, and a FIX on one stranger
        falsely gates another as READY-CONTESTED. Keyed on chain_root the span
        is 3, above the shared-base line, and the stranger stays PLAIN READY.
        RED at trunk (label -> "READY-CONTESTED"), GREEN on the cure."""
        landing = self._review("landing", "approve", self.side)         # live
        self._review("sub", "fix", self.side)                           # stranger
        self._review("sub", "approve", self.side)          # shares the label
        # CONTROL on the SAME observable + SAME real index: a genuine
        # two-chain contest on another tip must STILL fire after the cure, so
        # the absence below is the span discriminating and not contesting
        # being globally disabled.
        ctl = self._review("ctl-approve", "approve", self.c)
        self._review("ctl-fix", "fix", self.c)
        landreq._CONTEST_MEMO.clear()

        self.assertEqual(
            landreq.ready_word(self._live(ctl, self.c), index=self.OK()),
            "READY-CONTESTED",
            "positive control: a genuine two-chain contest must still fire")
        self.assertEqual(
            landreq.ready_word(self._live(landing, self.side),
                               index=self.OK()),
            "READY",
            "three distinct chains at one tip are a SHARED BASE; a shared "
            "lane label must not collapse them into a false contest against "
            "a stranger")

    def test_malformed_chain_roots_each_name_their_own_work(self):  # noqa: VACUOUS_ASSERTION — replay-sentinel and genuine-contest positive controls precede the READY absence assertion on the same real index
        """Two unreadable roots are two works, never one shared UNKNOWN work.

        A PRESENT malformed chain_root replays as the truthy CHAIN_UNKNOWN
        sentinel. Grouping directly on that sentinel collapses every corrupted
        row at a tip into one apparent work, deflating this real three-work
        shared base into a false two-work contest against the landing row.
        """
        landing = self._review("landing", "approve", self.side)
        broken_fix = self._review("broken-fix", "fix", self.side)
        broken_other = self._review("broken-other", "approve", self.side)
        self._corrupt_chain(broken_fix["id"])
        self._corrupt_chain(broken_other["id"])
        # CONTROL on the same real contest_index: a genuine two-work contest at
        # another tip must still fire, so READY below means the malformed roots
        # stayed distinct rather than the join becoming blind.
        ctl = self._review("ctl-approve", "approve", self.c)
        self._review("ctl-fix", "fix", self.c)
        landreq._CONTEST_MEMO.clear()
        replayed = dispatches.snapshot()[0]
        self.assertEqual(replayed[broken_fix["id"]]["chain_root"],
                         dispatches.CHAIN_UNKNOWN)
        self.assertEqual(replayed[broken_other["id"]]["chain_root"],
                         dispatches.CHAIN_UNKNOWN)

        self.assertEqual(
            landreq.ready_word(self._live(ctl, self.c), index=self.OK()),
            "READY-CONTESTED",
            "positive control: a genuine two-work contest must still fire")
        self.assertEqual(
            landreq.ready_word(self._live(landing, self.side), index=self.OK()),
            "READY",
            "two malformed roots are two distinct pieces of work; the truthy "
            "UNKNOWN sentinel must not collapse them")


class OffchainDetectorRealGitTest(unittest.TestCase):
    """The three reproduced blockers on tip 31372c9b, armed against
    REAL GIT rather than a double.

    THE REVIEW'S SHARPEST LINE IS WHY THIS CLASS EXISTS: "the fakes use exact
    set membership and therefore prove a stronger oracle than production."
    The existing harness answers a grep by asking whether the pattern is IN A
    SET — which is precisely the boundary-respecting behaviour production did
    NOT have. The double implemented the fix, so the suite verified the
    FIXTURE and every arm stayed green over a substring bug."""

    def _repo(self, *subjects):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_EMAIL="t@t")

        def git(*a):
            done = subprocess.run(("git",) + a, cwd=tmp, capture_output=True,
                                  text=True, env=env)
            self.assertEqual(done.returncode, 0, (a, done.stderr))
            return done.stdout.strip()

        git("init", "-q", "-b", "main")
        for n, subject in enumerate(subjects):
            with open(os.path.join(tmp, "f%d.txt" % n), "w") as fh:
                fh.write("x\n")
            git("add", "-A")
            git("commit", "-q", "-m", subject)
        self.worktree = tmp
        # THE PROBE TAKES A GITDIR, not a worktree — `_close_repo` hands it
        # one in production, and passing the worktree makes every git call
        # exit 128 with empty output, which reads exactly like "no citation
        # found". A fixture that fails this way turns every arm green.
        return os.path.join(tmp, ".git")

    def _find(self, repo, lane, trunk="main"):
        row = {"id": "b" * 32, "kind": "build", "lane": lane,
               "ts": "2026-08-08T00:00:00Z"}
        with mock.patch.object(landreq, "_lane_recurs_later",
                               return_value=False):
            return landreq.offchain_landing(row, gitdir=repo, trunk=trunk)

    def test_BLOCKER_3_a_longer_label_does_not_match_a_shorter_one(self):
        """`--fixed-strings --grep` is a SUBSTRING match, so `widget-lane-x`
        hits `widget-lane-x-extra`. Git has no dashed-token boundary and `\\b`
        cannot supply one, because a hyphen IS a word boundary to every regex
        flavour — so the boundary is applied to the message git returns."""
        repo = self._repo("fold: widget-lane-x-extra at abc (dispatch aaaa1111)")
        # CONTROL, unconditional and on the SAME repo: the whole token DOES
        # match, so the miss below is the boundary and not an inert probe.
        self.assertTrue(self._find(repo, "widget-lane-x-extra"))
        self.assertIsNone(self._find(repo, "widget-lane-x"))

    def test_BLOCKER_3_a_stem_walk_does_not_match_an_unrelated_prefix(self):
        """A stem is a PREFIX by construction, so it needs the boundary MORE
        than a full label does, not less."""
        repo = self._repo("fold: dispatch-rebind-orphaned-tip at abc")
        self.assertTrue(self._find(repo, "parked-dispatch-rebind-orphaned-tip"))
        self.assertIsNone(self._find(repo, "parked-dispatch-rebind-other-tip"))

    def test_BLOCKER_1_the_callers_trunk_is_the_probe_trunk(self):
        """The probe resolved its OWN local ref while its caller had already
        derived one. A citation on a LOCAL-only branch could mint a supersede
        proposal that no other clone can see."""
        repo = self._repo("root", "fold: widget-lane-q at abc")
        subprocess.run(("git", "branch", "other", "HEAD"), cwd=self.worktree,
                       capture_output=True)
        subprocess.run(("git", "reset", "-q", "--hard", "HEAD~1"),
                       cwd=self.worktree, capture_output=True)
        # main no longer carries the citation; `other` does. The ANSWER MUST
        # FOLLOW THE TRUNK THE CALLER NAMED.
        self.assertIsNone(self._find(repo, "widget-lane-q", trunk="main"))
        self.assertTrue(self._find(repo, "widget-lane-q", trunk="other"))

    def test_BLOCKER_2_recurrence_orders_by_time_not_by_per_row_seq(self):
        """`seq` counts ONE row's lifecycle events, so two rows created a week
        apart both start at 0 and "did the lane recur LATER" read False — on a
        live chain, which is what authorizes the terminal proposal."""
        # UNCONDITIONAL: both rows really parse to instants, so a False
        # below cannot come from an unreadable fixture.
        early = {"id": "a" * 32, "lane": "shared", "seq": 0,
                 "ts": "2026-08-01T00:00:00Z"}
        later = {"id": "c" * 32, "lane": "shared", "seq": 0,
                 "ts": "2026-08-08T00:00:00Z"}
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=({early["id"]: early,
                                              later["id"]: later}, None)):
            self.assertGreater(landreq._row_instant(early) or 0, 0)
            self.assertGreater(landreq._row_instant(later) or 0, 0)
            self.assertIs(landreq._lane_recurs_later(early), True)
            self.assertIs(landreq._lane_recurs_later(later), False)

    def test_BLOCKER_2d_the_verdict_does_not_depend_on_DICT_ORDER(self):
        """The open uncertainty on 2a2bd605 — "should ONE malformed
        timestamp silence a lane's recurrence rung forever?" — answered by
        measurement rather than by ruling.

        The early `return None` did something worse than silence the rung: it
        made the verdict depend on the order `snapshot()` happened to yield.
        The same lane, holding one undatable sibling and one genuinely-later
        sibling, answered None or True according to which came out of the dict
        first. So the answer is NO, one malformed stamp must not silence it —
        a sibling we CAN date and that IS later PROVES the chain is live, and
        an unreadable row is missing evidence, never evidence against it.

        This arm asserts over EVERY PERMUTATION, because a single ordering is
        exactly what hid the defect.

        LOAD-BEARING MUTATION: in helm/landreq.py `_lane_recurs_later`, change
        the undatable rung from `unreadable = True; continue` back to
        `return None`.
          python3 -m unittest tests.test_landreq.OffchainDetectorRealGitTest\
.test_BLOCKER_2d_the_verdict_does_not_depend_on_DICT_ORDER
          -> AssertionError: Items in the first set but not the second:
             None
        It preserves every older observable: the equal-stamp, undatable,
        later and noncanonical arms all still pass under it, because each
        uses an ordering under which the early return gives the same answer.
        """
        mine = {"id": "a" * 32, "lane": "shared", "ts": "2026-08-01T00:00:00Z"}
        later = {"id": "b" * 32, "lane": "shared", "ts": "2026-08-02T00:00:00Z"}
        junk = {"id": "c" * 32, "lane": "shared", "ts": "not-a-stamp"}
        early = {"id": "e" * 32, "lane": "shared", "ts": "2026-07-01T00:00:00Z"}

        def over_every_order(*siblings):
            seen = set()
            for perm in itertools.permutations(siblings):
                snap = {mine["id"]: mine}
                for r in perm:
                    snap[r["id"]] = r
                with mock.patch.object(landreq.dispatches, "snapshot",
                                       return_value=(snap, None)):
                    seen.add(landreq._lane_recurs_later(mine))
            return seen

        # MUST-HIT: a lone later sibling is a recurrence at all.
        self.assertEqual(over_every_order(later), {True})
        # A DATED, LATER SIBLING OUTRANKS AN UNREADABLE ONE — the finding.
        self.assertEqual(over_every_order(later, junk), {True})
        # And with nothing later proved, an unreadable sibling withholds the
        # False that would authorize a terminal.
        self.assertEqual(over_every_order(early, junk), {None})
        # while a lane whose every sibling is EARLIER really does not recur.
        self.assertEqual(over_every_order(early), {False})

    def test_BLOCKER_2b_EQUAL_stamps_cannot_order_so_they_are_UNKNOWN(self):
        """The ledger stamps at SECOND resolution, so two
        rows filed in the same second are genuinely UNORDERED — and "not
        later" is not the honest reading, because this predicate's False
        AUTHORIZES A TERMINAL on a possibly-live chain.

        LOAD-BEARING MUTATION: in helm/landreq.py `_lane_recurs_later`,
        delete the `if theirs == mine: return None` rung.
          python3 -m unittest tests.test_landreq.OffchainDetectorRealGitTest\
.test_BLOCKER_2b_EQUAL_stamps_cannot_order_so_they_are_UNKNOWN
          -> AssertionError: False is not None
        It preserves every older observable: the datable/later/undatable arms
        all still pass under it, so only this assertion can kill it."""
        mine = {"id": "a" * 32, "lane": "shared", "ts": "2026-08-01T00:00:00Z"}
        twin = {"id": "c" * 32, "lane": "shared", "ts": "2026-08-01T00:00:00Z"}
        # CONTROL, unconditional: both stamps really parse, so the None below
        # is the EQUALITY and not an unreadable fixture.
        self.assertEqual(landreq._row_instant(mine), landreq._row_instant(twin))
        self.assertGreater(landreq._row_instant(mine) or 0, 0)
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=({mine["id"]: mine,
                                              twin["id"]: twin}, None)):
            self.assertIsNone(landreq._lane_recurs_later(mine))

    def test_BLOCKER_2c_a_NONCANONICAL_stamp_is_not_an_instant(self):
        """`_row_instant` stripped whitespace and accepted
        stamps that `dispatches._valid_ts` REJECTS, so a row the ledger holds
        to be unstamped got a confident instant here. Two readers of one field
        must not disagree about which values are real — and the one that can
        END a row defers to the one that admits it.

        LOAD-BEARING MUTATION: in helm/landreq.py `_row_instant`, replace the
        `if not dispatches._valid_ts(stamp): return None` rung with the local
        parse it displaced — `stamp = stamp.strip()`.
          python3 -m unittest tests.test_landreq.OffchainDetectorRealGitTest\
.test_BLOCKER_2c_a_NONCANONICAL_stamp_is_not_an_instant
          -> AssertionError: 1785542400 is not None : \' 2026-08-01T00:00:00Z \'
        MEASURED, both halves: it reddens ONLY this arm — the datable, later,
        equal-stamp and undatable arms all still pass under it."""
        canonical = {"id": "a" * 32, "lane": "s", "ts": "2026-08-01T00:00:00Z"}
        self.assertGreater(landreq._row_instant(canonical) or 0, 0)  # MUST-HIT
        for bad in (" 2026-08-01T00:00:00Z ", "2026-08-01T00:00:00",
                    "2026-08-01 00:00:00Z", ""):
            self.assertIsNone(landreq._row_instant({"ts": bad}), repr(bad))
            self.assertFalse(landreq.dispatches._valid_ts(bad), repr(bad))

    def test_BLOCKER_3b_an_UPPERCASE_or_unicode_suffix_is_not_whole(self):
        """Reproduced against real git: `widget-lane-x` came
        back from `widget-lane-xExtra` and the post-filter passed it as EXACT,
        because the boundary class was lowercase ASCII only. A lane label is
        lowercase by convention; the MESSAGE git hands back is arbitrary text,
        so the boundary must reject ANY adjacent alphanumeric.

        LOAD-BEARING MUTATION: narrow `_LANE_CHAR` back to `[a-z0-9-]`.
          python3 -m unittest tests.test_landreq.OffchainDetectorRealGitTest\
.test_BLOCKER_3b_an_UPPERCASE_or_unicode_suffix_is_not_whole
          -> AssertionError: True is not false  (the xExtra case)"""
        cites = landreq._cites_whole_token
        # MUST-HIT first: the whole token still binds.
        self.assertTrue(cites("fold: widget-lane-x at abc", "widget-lane-x"))
        for text in ("fold: widget-lane-xExtra at abc",
                     "fold: widget-lane-xEXTRA at abc",
                     "fold: widget-lane-x9 at abc",
                     "fold: widget-lane-x\u042f at abc",
                     "fold: Xwidget-lane-x at abc"):
            self.assertFalse(cites(text, "widget-lane-x"), text)

    def test_BLOCKER_2_an_undatable_sibling_is_unreadable_not_absent(self):
        """This predicate's False AUTHORIZES a terminal, so an unreadable
        sibling must not read as "no later recurrence"."""
        mine = {"id": "a" * 32, "lane": "shared", "ts": "2026-08-01T00:00:00Z"}
        blind = {"id": "c" * 32, "lane": "shared", "ts": "not-a-stamp"}
        # CONTROL on the same observable: mine IS datable, so the None
        # below is the blind sibling and not an undatable subject.
        self.assertGreater(landreq._row_instant(mine) or 0, 0)
        self.assertIsNone(landreq._row_instant(blind))
        with mock.patch.object(landreq.dispatches, "snapshot",
                               return_value=({mine["id"]: mine,
                                              blind["id"]: blind}, None)):
            self.assertIsNone(landreq._lane_recurs_later(mine))


class AbsentStillClimbsToTheTranslationRungTest(unittest.TestCase):
    """The landing ladder is ORDERED, and the order IS the guarantee:

        ancestry > patch-identity > recorded translation

    A recorded translation is a MECHANICAL identity map written by
    `migrate_refs --apply`, which Git then proves. It is the rung that sees a
    rewritten sha, and it is exactly the case where Git alone answers ABSENT.
    So "absent" is not a reason to stop climbing, and a rung that cannot be
    READ is not a rung that answered no.

    THIS CLASS WAS ALSO CALLED `LandingProofLadderOrderTest` (1bf63c006), and so
    is the one further down this file (c48e96a03, which added the content rung a
    day later). Python binds the second and DISCARDS the first, so from
until this rename both arms below ran ZERO times on any gate —
    the tests existed, read as protection, and were never executed once.
    `tests/test_suite_collection.py` now censuses the shape so it cannot recur
    silently.

    THE TWO CLASSES STAY SEPARATE RATHER THAN MERGING, though they drive the
    same `_close_landed_proof`. Their `_proof` helpers disagree deliberately:
    the later one STUBS the content rung so it can assert the rung was never
    CONSULTED, while this one leaves the content rung live and reads the refusal
    MESSAGE the whole ladder composes. One helper cannot serve both, so merging
    would quietly delete whichever premise lost the merge.
    """

    def _proof(self, landing, table=(None, None)):
        with mock.patch.object(landreq, "_landing_proof",
                               return_value=landing), \
             mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=table):
            return landreq._close_landed_proof("/x/.git", "a" * 40, "refs/x",
                                               "b" * 40)

    def test_ABSENT_still_consults_the_recorded_translation_map(self):
        """LOAD-BEARING MUTATION: restore the early return, so
        `_landing_proof == "absent"` refuses before the map is read.
          python3 -m unittest tests.test_landreq\\
.AbsentStillClimbsToTheTranslationRungTest\\
.test_ABSENT_still_consults_the_recorded_translation_map
          -> AssertionError: 'sidecar is unreadable' not found in 'Git proved
             aaaaaaaaaaaa is neither an ancestor ... landing closure NOT
             recorded'
        The refusal MESSAGE is the assertion: both versions refuse, and only
        one of them got as far as the map."""
        # MUST-HIT: the same door on a placeable tip returns the proof itself,
        # so the refusals below are the ladder and not a broken stub.
        mode, _t, _w, err = self._proof("ancestor")
        self.assertEqual(mode, "ancestor")
        self.assertIsNone(err)

        for landing in ("absent", "unknown"):
            _m, _t, _w, err = self._proof(landing, table=({}, "permission denied"))
            self.assertIn("sidecar is unreadable", err or "", landing)

    def test_an_UNREADABLE_map_refuses_rather_than_reading_as_absence(self):
        """A map that cannot be read is an unreadable MEASUREMENT. Treating it
        as "no translation recorded" would let an unreadable stronger rung be
        silently replaced by whatever comes after it.

        LOAD-BEARING MUTATION: return `({}, None)` from
        `_ref_translations_checked` instead of propagating the error.
          -> AssertionError: 'sidecar is unreadable' not found in '... and no
             recorded translation names it; landing closure NOT recorded'"""
        _m, _t, _w, readable = self._proof("unknown", table=({}, None))
        self.assertIn("no recorded translation names it", readable or "")

        _m, _t, _w, unreadable = self._proof("unknown", table=({}, "EACCES"))
        self.assertIn("sidecar is unreadable", unreadable or "")
        self.assertNotEqual(readable, unreadable,
                            "unreadable and absent must not read alike")


class IsolatedEstateTest(unittest.TestCase):
    """A bare TestCase that reaches the landing ladder now touches DURABLE
    state and needs an estate of its own.

    `_landing_proof` KEEPS its positive answers and `_landed_index` KEEPS its
    trunk window, both under `home.global_dir()`. Two things follow that no
    arm here used to have to think about. An unisolated arm writes into the
    operator's real estate; and, measured, every arm in these classes shares
    the same synthetic ("/g", "de"*20, "origin/main") triple, so the FIRST
    arm's answer became a free short-circuit for every later one and the
    spawn spies they are built on went empty. The ladder was fine; the arms
    were reading a ledger the previous arm wrote.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="helm-test-lr-estate-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        prior = os.environ.get("HELM_HOME")

        def restore():
            if prior is None:
                os.environ.pop("HELM_HOME", None)
            else:
                os.environ["HELM_HOME"] = prior
            landreq._LAND_PROOF_MEMO.clear()
            landreq._TRUNK_INDEX_MEMO.clear()

        self.addCleanup(restore)
        os.environ["HELM_HOME"] = os.path.join(tmp, "helm")
        # The memos key on a FILE's (size, mtime); a previous test's estate
        # leaves them warm, so clearing is the difference between reading this
        # arm's ledger and the last one's.
        landreq._LAND_PROOF_MEMO.clear()
        landreq._TRUNK_INDEX_MEMO.clear()


class SplitTheWordLandedTest(IsolatedEstateTest):
    """Step 2 of the ledger-architecture council: LANDED is three
    facts that die differently, and every NO in that council turned on the
    collapse.

    `_landed` returned True for both `ancestor` and `patch-equivalent`. Only
    the first is monotonic: a REVERT is ff-only and permitted, leaves ancestry
    untouched, and makes content-presence false. So a record of landed is a
    record of whichever fact its writer happened to mean.
    """

    PROOFS = ("ancestor", "patch-equivalent", "absent", "unknown")

    def _table(self, proof):
        """Mock BOTH primitives, because the three predicates no longer share
        one input — and that divergence IS the cure, not an accident.

        ancestry() now maps `_ancestry` DIRECTLY: routing it through
        `_landing_proof` meant an unreadable `git cherry` could erase an
        already-proved ancestry answer. So a table that mocked only
        `_landing_proof` would leave the ancestry column measuring NOTHING —
        vacuous in exactly the way that looks green.
        """
        anc = {"ancestor": landreq.ANCESTOR,
               "patch-equivalent": landreq.NOT_ANCESTOR,
               "absent": landreq.NOT_ANCESTOR,
               "unknown": landreq.UNDETERMINED}[proof]
        return (mock.patch.object(landreq, "_landing_proof",
                                  return_value=proof),
                mock.patch.object(landreq, "_ancestry", return_value=anc))

    def test_each_predicate_answers_its_OWN_question(self):
        """The table IS the contract. Each row is a proof outcome and each
        column a predicate; if any two columns were identical the split would
        be decorative."""
        want = {
            #  proof            ancestry  patch_identity  landed_ever
            "ancestor":        (True,     False,          True),
            "patch-equivalent": (False,   True,           True),
            "absent":          (False,    False,          False),
            "unknown":         (None,     None,           None),
        }
        for proof, (anc, pid, pres) in want.items():
            mp, ma = self._table(proof)
            with mp, ma:
                self.assertIs(landreq.ancestry("/g", "t", "r"), anc, proof)
                self.assertIs(landreq.patch_identity("/g", "t", "r"), pid, proof)
                self.assertIs(landreq.landed_ever("/g", "t", "r"), pres, proof)
        # THE DISCRIMINATION ITSELF: no two predicates agree on every proof.
        # Without this, three predicates that happened to be synonyms would
        # pass every row above.
        cols = {name: tuple(want[p][i] for p in self.PROOFS)
                for i, name in enumerate(("ancestry", "patch_identity",
                                          "landed_ever"))}
        self.assertEqual(len(set(cols.values())), 3,
                         "two predicates answer identically, so the split "
                         "distinguishes nothing: %r" % (cols,))

    def test_ancestry_survives_an_UNREADABLE_patch_identity_probe(self):
        """THE ARM THAT ACTUALLY GUARDS THE CURE, and the table above does not.

        My regression table mocks _landing_proof and _ancestry to ALIGNED
        outcomes — so reverting ancestry() to the old `_landing_proof` path
        leaves every row returning its expected value and the suite green while
        the defect returns. Aligning the mocks is precisely what makes the
        table unable to discriminate. A reviewer caught that; it is the second
        time on this lane that curing a vacuity created one.

        THE FALSIFIER IS THE DIVERGENT STATE: _ancestry has decisively answered
        NOT_ANCESTOR while _landing_proof says 'unknown' because the LATER git
        cherry probe is unreadable. The old implementation returns None here —
        a broken patch-identity instrument erasing a proved reachability
        answer. The cure must return False, and must not consult
        _landing_proof at all."""
        called = []

        def must_not_be_called(*a, **k):
            called.append(a)
            return "unknown"

        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR), \
             mock.patch.object(landreq, "_landing_proof",
                               side_effect=must_not_be_called):
            got = landreq.ancestry("/g", "t", "r")
        self.assertIs(got, False,
                      "an unreadable patch-identity probe erased a proved "
                      "ancestry answer — this is the predecessor's defect")
        self.assertEqual(called, [],
                         "ancestry() consulted _landing_proof; a broken "
                         "instrument for a DIFFERENT question can still reach it")

        # UNCONDITIONAL POSITIVE CONTROL on the same divergent shape: with
        # _ancestry saying ANCESTOR and _landing_proof still unreadable,
        # ancestry() must answer True — so the False above is the ancestry
        # primitive being read, not a function that returns False regardless.
        called[:] = []
        with mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.ANCESTOR), \
             mock.patch.object(landreq, "_landing_proof",
                               side_effect=must_not_be_called):
            self.assertIs(landreq.ancestry("/g", "t", "r"), True)
        self.assertEqual(called, [])

    def test_ancestor_and_patch_equivalent_DIVERGE_which_is_the_whole_point(self):
        """The one row that mattered to the council. _landed said True to
        both; a revert kills one and not the other."""
        mp, ma = self._table("ancestor")
        with mp, ma:
            self.assertIs(landreq.ancestry("/g", "t", "r"), True)
            self.assertIs(landreq.patch_identity("/g", "t", "r"), False)
        mp, ma = self._table("patch-equivalent")
        with mp, ma:
            self.assertIs(landreq.ancestry("/g", "t", "r"), False)
            self.assertIs(landreq.patch_identity("/g", "t", "r"), True)

    def test_the_deprecated_wrapper_is_UNCHANGED_for_all_thirty_callers(self):
        """30 live callers still use _landed. Introducing the predicates must
        not move it by one answer, or this step breaks the board it exists to
        make legible. Old contract: ancestor/patch-equivalent -> True,
        absent -> False, unknown -> None."""
        historical = {"ancestor": True, "patch-equivalent": True,
                      "absent": False, "unknown": None}
        for proof, expected in historical.items():
            mp, ma = self._table(proof)
            with mp, ma:
                self.assertIs(landreq._landed("/g", "t", "r"), expected, proof)

    def test_one_proof_call_per_predicate(self):
        """Each predicate asks git ONCE. Three calls would triple the spawn
        count on the owner's P0 path and could disagree with themselves
        mid-answer — my first draft did exactly that."""
        for fn in (landreq.patch_identity, landreq.landed_ever,
                   landreq._landed):
            calls = []

            def counting(*a, **k):
                calls.append(a)
                return "ancestor"

            with mock.patch.object(landreq, "_landing_proof",
                                   side_effect=counting):
                fn("/g", "t", "r")
            self.assertEqual(len(calls), 1,
                             "%s asked git %d times" % (fn.__name__, len(calls)))


class ContentEquivalentRungTest(IsolatedEstateTest):
    """task/777's fourth rung, armed against REAL GIT.

    The rung ANSWERS BY SEARCHING TRUNK, so a double that answers by set
    membership would prove a stronger oracle than production — the same trap
    named for the off-chain detector. Every arm here builds commits.
    """

    def _repo(self):
        tmp = tempfile.mkdtemp(prefix="helm-test-ce-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_EMAIL="t@t")

        def git(*a):
            done = subprocess.run(("git",) + a, cwd=tmp, capture_output=True,
                                  text=True, env=env)
            self.assertEqual(done.returncode, 0, (a, done.stderr))
            return done.stdout.strip()

        git("init", "-q", "-b", "main")
        self.git = git
        # PRODUCTION HANDS THESE READERS A GITDIR (`_close_repo` returns one),
        # and passing a WORKTREE is the trap this file already documents: it
        # makes _landing_proof answer "unknown" for a commit that IS an
        # ancestor — measured — so a control built on it proves nothing and
        # every arm goes green. The arms use the gitdir for that reason.
        self.gitdir = os.path.join(tmp, ".git")
        return tmp

    def _write(self, tmp, name, text):
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(text)

    def _commit(self, tmp, name, text, msg):
        self._write(tmp, name, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def test_the_SAME_CHANGE_on_a_different_base_resolves_its_carrier(self):
        """The rung's whole reason: identical added and removed bytes, moved
        CONTEXT, and Git calling it absent. db87bcd4's reviewed tip and trunk
        98582d17 differ by exactly 3 of 98 context lines.

        LOAD-BEARING MUTATION: in `_content_equivalent_carrier`, compare
        `other[0] == source[0]` (patch-id) instead of `other[1] == source[1]`.
          python3 -m unittest tests.test_landreq.ContentEquivalentRungTest\\
.test_the_SAME_CHANGE_on_a_different_base_resolves_its_carrier
          -> AssertionError: None != '<carrier sha>'
        That mutation makes this rung patch-identity with extra steps, which is
        the tier ABOVE it and would never have reached here."""
        tmp = self._repo()
        self._commit(tmp, "seed.txt", "seed\n", "root")
        root = self.git("rev-parse", "HEAD")
        self._commit(tmp, "f.txt", "one\nctx-a\n", "base a")
        carrier = self._commit(tmp, "f.txt", "one\nctx-a\nCHANGE\n",
                               "carrier on trunk")
        trunk = carrier

        self.git("checkout", "-q", "-b", "lane", root)
        self._commit(tmp, "f.txt", "one\nctx-b\n", "base b")
        source = self._commit(tmp, "f.txt", "one\nctx-b\nCHANGE\n", "the lane")

        # CONTROL: Git itself cannot place it — that is why this rung exists.
        # BOTH "absent" and "unknown" are honest entries here and the rung
        # handles both; asserting only "absent" pins a stronger property than
        # the contract has, and this fixture really does yield "unknown"
        # (measured) because a two-commit history gives `git cherry` nothing
        # to work with. The claim that matters is that Git could not place it.
        self.assertIn(landreq._landing_proof(self.gitdir, source, trunk),
                      ("absent", "unknown"))

        # UNCONDITIONAL POSITIVE ON THE SAME READER: the carrier really is on
        # trunk by ancestry. Without it every assertion here is a name against
        # another name or against a tuple of strings, none of which proves
        # landreq answered at all — and the whole arm would pass if the module
        # returned None for everything.
        self.assertEqual(landreq._landing_proof(self.gitdir, carrier, trunk),
                         "ancestor")

        found, why = landreq._content_equivalent_carrier(self.gitdir, source,
                                                         trunk, trunk)
        self.assertIsNone(why)
        self.assertEqual(found, carrier)

    def test_a_SHARED_key_is_refused_and_never_picked(self):
        """Revert-then-reapply puts one change on trunk twice. Content offers
        no basis for choosing, and choosing anyway is the failure this rung
        exists to avoid.

        LOAD-BEARING MUTATION: delete the `if len(hits) > 1` rung so it
        returns hits[0].
          -> AssertionError: '<sha>' is not None
        It preserves the older observable: the single-carrier arm above still
        passes under it, because with one hit hits[0] IS the answer."""
        tmp = self._repo()
        self._commit(tmp, "f.txt", "one\n", "base")
        base = self.git("rev-parse", "HEAD")
        self._commit(tmp, "f.txt", "one\nREPEATED\n", "apply")
        self._commit(tmp, "f.txt", "one\n", "revert")
        self._commit(tmp, "f.txt", "one\nREPEATED\n", "reapply")
        trunk = self.git("rev-parse", "HEAD")

        self.git("checkout", "-q", "-b", "lane", base)
        source = self._commit(tmp, "f.txt", "one\nREPEATED\n", "the lane")

        # MUST-HIT: two trunk commits really do share this content.
        found, why = landreq._content_equivalent_carrier(self.gitdir, source,
                                                         trunk, trunk)
        self.assertIsNone(found)
        self.assertIn("cannot choose between them", why or "")

    def test_a_MERGE_is_refused_by_PARENT_COUNT_not_by_the_empty_sentinel(self):
        """A merge emits no diff, so the identity falls to EMPTY. Catching it
        THERE would work by accident and bucket every merge with every
        genuinely-empty commit — a collision class, not a refusal.

        LOAD-BEARING MUTATION: delete the parent-count rung.
          -> AssertionError: 'a merge has no single delta to compare' not
             found in 'the source commit has no readable content identity'
        The message is the assertion: both refuse, and only one says WHY."""
        tmp = self._repo()
        self._commit(tmp, "f.txt", "one\n", "base")
        base = self.git("rev-parse", "HEAD")
        self._commit(tmp, "f.txt", "one\nmain\n", "main side")
        self.git("checkout", "-q", "-b", "side", base)
        self._commit(tmp, "g.txt", "branch\n", "branch side")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "side", "-m", "merge")
        merge = self.git("rev-parse", "HEAD")

        # MUST-HIT: it really is a merge, so the refusal is about parentage.
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1",
                                      merge).split()), 3)
        found, why = landreq._content_equivalent_carrier(self.gitdir, merge, merge,
                                                         merge)
        self.assertIsNone(found)
        self.assertIn("a merge has no single delta to compare", why or "")

    def test_same_payload_at_a_DIFFERENT_OCCURRENCE_is_not_a_carrier(self):
        """Payload equality narrows candidates; application location admits.

        Both commits replace the same bytes in the same path, but at different
        duplicate occurrences. A content-only key aliases them even though the
        reviewed change did not land. Replaying the reviewed delta onto the
        candidate parent must produce the candidate tree before it may carry.
        """
        tmp = self._repo()
        original = "old\n" + "context\n" * 10 + "old\n"
        self._commit(tmp, "f.txt", original, "base")
        base = self.git("rev-parse", "HEAD")
        carrier = self._commit(
            tmp, "f.txt", original.rsplit("old\n", 1)[0] + "new\n",
            "change the second occurrence")
        trunk = carrier

        self.git("checkout", "-q", "-b", "lane", base)
        source = self._commit(
            tmp, "f.txt", original.replace("old\n", "new\n", 1),
            "change the first occurrence")

        source_id = landreq._commit_content_identity(self.gitdir, source)
        carrier_id = landreq._commit_content_identity(self.gitdir, carrier)
        self.assertNotEqual(source_id[0], carrier_id[0])       # MUST-HIT
        self.assertEqual(source_id[1:], carrier_id[1:])        # the collision
        self.assertEqual(landreq._landing_proof(self.gitdir, source, trunk),
                         "absent")

        found, why = landreq._content_equivalent_carrier(
            self.gitdir, source, trunk, trunk)
        self.assertIsNone(found,
                          "same payload at another occurrence is not landed")
        self.assertIn("application-equivalent", why or "")

    def test_one_unreadable_candidate_withholds_uniqueness(self):
        """A partial trunk census cannot prove that one readable hit is unique.

        EMPTY is outside a non-empty source's domain. An unreadable identity is
        UNKNOWN: it may be a second carrier, so silently skipping it turns a
        shared key into a false unique match.
        """
        tmp = self._repo()
        self._commit(tmp, "f.txt", "base\n", "base")
        base = self.git("rev-parse", "HEAD")
        carrier = self._commit(tmp, "f.txt", "base\nMATCH\n", "carrier")
        blind = self._commit(tmp, "f.txt", "base\nMATCH\nother\n", "blind")
        trunk = blind

        self.git("checkout", "-q", "-b", "lane", base)
        source = self._commit(tmp, "f.txt", "base\nMATCH\n", "source")
        real_identity = landreq._commit_content_identity

        def identity(root, sha):
            if sha == blind:
                return None
            return real_identity(root, sha)

        self.assertEqual(self.git("cat-file", "-t", blind), "commit")
        with mock.patch.object(landreq, "_commit_content_identity", identity):
            found, why = landreq._content_equivalent_carrier(
                self.gitdir, source, trunk, trunk)
        self.assertIsNone(found,
                          "an unreadable candidate makes uniqueness unknown")
        self.assertIn("unreadable", why or "")
        self.assertNotEqual(carrier, blind)                    # non-vacuous set

    def test_replay_refuses_a_carrier_outside_the_pinned_trunk(self):
        """Matching payload proves sameness; ancestry proves landedness."""
        tmp = self._repo()
        self._commit(tmp, "f.txt", "base\n", "base")
        pinned = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "lane", pinned)
        source = self._commit(tmp, "f.txt", "base\nCHANGE\n", "source")
        identity = landreq._commit_content_identity(self.gitdir, source)
        witness = landreq._content_equivalent_witness(
            self.gitdir, source, source, "refs/heads/main", pinned,
            identity, 1)
        self.assertIsNotNone(witness)
        ancestry = subprocess.run(
            ("git", "merge-base", "--is-ancestor", source, pinned),
            cwd=tmp, capture_output=True)
        self.assertEqual(ancestry.returncode, 1)                # MUST-HIT

        replayed, why = landreq._content_equivalent_replay(
            self.gitdir, witness)
        self.assertIs(replayed, False)
        self.assertIn("pinned trunk", why or "")


    def test_a_carrier_on_a_MERGED_SIDE_is_not_hidden_by_path_simplification(self):
        """The census finding, built rather than argued.

        A path-limited `rev-list` applies DEFAULT HISTORY SIMPLIFICATION: when
        a merge is TREESAME to one parent for the given paths, git prunes the
        OTHER side entirely. A content-equivalent carrier living on that side
        is REACHABLE FROM THE PIN and invisible to the census — and the damage
        is not a miss, it is a false GRANT: the survivor count drops to one and
        the uniqueness check hands out a proof that history does not support.

        The topology is the one that actually reproduces it (measured with
        plain git before this arm was written): main and a side branch reach
        the SAME content independently, so the merge is TREESAME to its first
        parent and the side is pruned.

        LOAD-BEARING MUTATION: drop `--full-history` from the census rev-list
        in `_content_equivalent_carrier`.
          python3 -m unittest tests.test_landreq.ContentEquivalentRungTest\
.test_a_carrier_on_a_MERGED_SIDE_is_not_hidden_by_path_simplification
          -> the rung RETURNS the twin as a unique carrier instead of refusing,
             so the assertion on `found` reddens. Note which way that fails:
             without the flag the rung is CONFIDENT and WRONG, which is the
             only failure mode this whole rung exists to prevent.
        """
        tmp = self._repo()
        self._commit(tmp, "seed.txt", "seed\n", "root")
        base = self._commit(tmp, "f.txt", "one\n", "base")

        # The carrier lives on a side branch...
        self.git("checkout", "-q", "-b", "side", base)
        carrier = self._commit(tmp, "f.txt", "one\nCHANGE\n", "carrier on side")

        # ...while main reaches the SAME content on its own, which is what
        # makes the merge TREESAME to the first parent.
        self.git("checkout", "-q", "main")
        twin = self._commit(tmp, "f.txt", "one\nCHANGE\n", "same content on main")
        self.git("merge", "--no-ff", "-q", "side", "-m", "merge side")
        trunk = self.git("rev-parse", "HEAD")

        # The reviewed tip: the same change, never merged.
        self.git("checkout", "-q", "-b", "lane", base)
        reviewed = self._commit(tmp, "f.txt", "one\nCHANGE\n", "same change on a lane")

        # THE PREMISE, ASSERTED — without these three this arm could pass on a
        # repository where nothing was ever hidden, which is the failure mode
        # of every simplification test that does not check its own topology.
        self.git("merge-base", "--is-ancestor", carrier, trunk)   # MUST-HIT: no raise
        simplified = self.git("rev-list", trunk, "--", "f.txt").split()
        self.assertIn(twin, simplified)
        self.assertNotIn(carrier, simplified,
                         "default simplification did NOT prune the side, so "
                         "this arm is not testing what it says it is")
        self.assertIn(carrier,
                      self.git("rev-list", "--full-history", trunk,
                               "--", "f.txt").split())

        # TWO trunk commits carry this content, so the rung must refuse rather
        # than choose. Under the pruned walk it sees ONE and grants a proof.
        found, why = landreq._content_equivalent_carrier(
            self.gitdir, reviewed, "main", trunk)
        self.assertIsNone(found, "granted a carrier on a census that could not "
                                 "see the whole history")
        self.assertIn("cannot choose between them", why)


class ContentRungPrecedenceTest(LandReqBase):
    """task/777's REFUSAL CONTROLS: the new door must not weaken an old gate.

    Measured: 15 of the 19 first-customer rows carry FIX
    polarity, and the ladder refuses them as CONTRARY before any content
    proof. That refusal is the rung WORKING — a FIX verdict says the reviewer
    required changes, and the reviewed tip reaching trunk does not discharge
    findings. These arms pin that, because a rung that could rescue a FIX row
    would close rows whose review debt is unpaid.
    """

    def _spy(self):
        """A carrier finder that would ALWAYS succeed, recording each call.

        The strength of these arms is that the stub says YES. If the ladder
        still refuses, it refused WITHOUT ASKING — which is precedence, not
        luck. An arm that stubbed a MISS would pass for the wrong reason.
        """
        calls = []

        def finder(*a, **kw):
            calls.append(a[1] if len(a) > 1 else None)
            return ("c" * 40, None)

        return calls, finder

    def test_a_FIX_row_never_even_ASKS_the_content_rung(self):  # noqa: VACUOUS_ASSERTION — the spy list is an INTENTIONAL zero and no positive is possible on it: the arm's claim IS that the rung was never called. The load-bearing assertion is assertIn('CONTRARY', err), which the mutation reddens; the docstring states that the call count is corroborating rather than load-bearing under this fixture
        """LOAD-BEARING MUTATION, and the measured message is not the one I
        first wrote. Disable the ORDINARY door's polarity gate — line ~6389,
        `if False and polarity in ("fix", "supersede")`. THE SAME GATE TEXT
        APPEARS THREE TIMES in this module (5445, 6389, 6960), so a string
        replace silently hits the wrong door and this arm stays green under a
        mutation that never applied; anchor on the line number.
          python3 -m unittest tests.test_landreq.ContentRungPrecedenceTest\
.test_a_FIX_row_never_even_ASKS_the_content_rung
          -> AssertionError: 'CONTRARY' not found in '--repo ... is not a
             readable Git working tree'

        STATED LIMIT, because this arm would otherwise imply more than it
        proves: the CALL-COUNT assertion is CORROBORATING, not load-bearing.
        With the gate disabled this fixture's row dies one rung later at
        `_close_repo` — its tmp dir is not a working tree — so the content
        rung goes unreached EITHER WAY and `calls == []` holds whether or not
        precedence survived. What the mutation genuinely proves is that the
        CONTRARY refusal is what answers a FIX row. Making the call count
        falsifiable needs a fixture whose repo the ladder accepts."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "FIX", polarity="fix")
        lr = landreq.get(row["id"])[0]
        # MUST-HIT: the row really carries a FIX, so the refusal below is
        # about polarity and not about a malformed fixture.
        self.assertEqual(lr.get("polarity"), "fix")

        calls, finder = self._spy()
        with mock.patch.object(landreq, "_content_equivalent_carrier", finder):
            out, err = landreq._close_ladder_landed(
                lr, "e", self.tmp, self.main, True, live=True)
        # THE CALL COUNT ASSERTS FIRST, deliberately. If it ran last, any
        # mutation that let a FIX row reach the rung would trip the verdict
        # assertion above it and this arm would never demonstrate the claim
        # its own docstring makes.
        self.assertEqual(calls, [],
                         "a FIX row must be refused WITHOUT asking the rung")
        self.assertIsNone(out)
        self.assertIn("CONTRARY", err or "")

    def test_a_SUPERSEDE_row_never_even_ASKS_the_content_rung(self):  # noqa: VACUOUS_ASSERTION — same intentional zero as the FIX sibling; the polarity assertion above it is the unconditional positive and assertIsNone(out) is the refusal this pins
        """The sibling polarity, pinned for the same reason: a supersede
        verdict is its own terminal and a content carrier cannot re-open it."""
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "replaced",
                                polarity="supersede")
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr.get("polarity"), "supersede")   # MUST-HIT

        calls, finder = self._spy()
        with mock.patch.object(landreq, "_content_equivalent_carrier", finder):
            out, err = landreq._close_ladder_landed(
                lr, "e", self.tmp, self.main, True, live=True)
        self.assertEqual(calls, [])
        self.assertIsNone(out)


class ContentWitnessDurabilityTest(unittest.TestCase):
    """The FIX on task/777: the proof was computed, passed, and DROPPED.

    `dispatches._record_close_proven` copies ONLY keys admitted by the close
    schemas, and both the ordinary `landed` tuple and _BUILD_LANDED_EVENT_FIELDS
    omitted the witness — so `proof_mode=content-equivalent` could close with
    nothing durable for `_content_equivalent_replay` to re-check, violating the
    rung's own "a hit is not a closure".

    ALL FIVE OF THE RUNG'S ARMS STAYED GREEN OVER THAT BROKEN WIRE, which is
    the lesson: they tested the SEARCH and nobody tested the RECORD. This class
    asserts the schemas, because the schema IS the wire.
    """

    def test_BOTH_close_schemas_admit_the_content_witness(self):
        """LOAD-BEARING MUTATION: drop either name from either collection.
          python3 -m unittest tests.test_landreq.ContentWitnessDurabilityTest\\
.test_BOTH_close_schemas_admit_the_content_witness
          -> AssertionError: 'content_witness' not found in (...)

        This is the cheapest arm that could have caught the original defect,
        and it is cheap precisely because it does not need a close at all: the
        writer's filter is a data structure, so the bug is a membership
        question. An end-to-end close arm would also catch it and would have
        cost a fixture nobody wrote — which is why nobody wrote it."""
        landed = dispatches._CLOSE_STATE_FIELDS["landed"]
        build = dispatches._BUILD_LANDED_EVENT_FIELDS
        # MUST-HIT: these collections really are populated, so a membership
        # failure below is an omission and not an empty schema.
        self.assertIn("close_proof_mode", landed)
        self.assertIn("close_proof_mode", build)

        # THE ORDINARY SCHEMA IS A MEMBERSHIP TUPLE: a name absent here is
        # dropped by the writer's copy.
        for name in ("content_witness", "content_witness_anchor"):
            self.assertIn(name, landed, "ordinary landed door drops %s" % name)

        # THE BUILD SCHEMA IS AN EXACT SET and a standing guard asserts
        # `set(event) == _BUILD_LANDED_EVENT_FIELDS`, so listing the witness
        # there would make it REQUIRED of every build close — measured, that
        # reddened 9 arms in tests/test_lr_close. Admission is by SUBTRACTION
        # at the enforcement points instead, so what this pins is the
        # exemption set, not membership.
        self.assertEqual(dispatches._CONTENT_PROOF_FIELDS,
                         {"content_witness", "content_witness_anchor"})
        for name in dispatches._CONTENT_PROOF_FIELDS:
            self.assertNotIn(name, build,
                             "%s must stay OUT of the exact-set schema" % name)


class ContentCloseLabelTest(unittest.TestCase):
    """The weakest mechanical rung must say so on the surface.

    A reader seeing a bare LANDED cannot tell ancestry from a context-erased
    content match, and those are not the same claim — the second ignores
    context by construction. LANDED_REWRITTEN already exists for exactly this
    reason on the translation rung.
    """

    def test_a_content_equivalent_close_does_NOT_display_as_plain_LANDED(self):
        """LOAD-BEARING MUTATION: delete the CONTENT_EQUIVALENT arm from
        `_close_label`.
          python3 -m unittest tests.test_landreq.ContentCloseLabelTest\\
.test_a_content_equivalent_close_does_NOT_display_as_plain_LANDED
          -> AssertionError: 'LANDED' != 'LANDED_CONTENT'
        It preserves every older observable: ancestry still reads LANDED and
        the translation rung still reads LANDED_REWRITTEN under it, so only
        this assertion can kill it."""
        label = landreq._close_label
        # MUST-HIT CONTROLS FIRST, both older observables, so a difference
        # below is this rung and not a broken labeller.
        self.assertEqual(label({"close_reason": "landed",
                                "close_proof_mode": "ancestor"}), "LANDED")
        self.assertEqual(label({"close_reason": "landed",
                                "close_proof_mode": "translated-ancestor"}),
                         "LANDED_REWRITTEN")
        self.assertEqual(label({"close_reason": "landed",
                                "close_proof_mode": landreq.CONTENT_EQUIVALENT}),
                         "LANDED_CONTENT")


class ContentWitnessReplaysAtWriteTest(unittest.TestCase):
    """The ruling: the witness must replay AT THE WRITE
    BOUNDARY, because that is where both commits are in hand and where a bad
    witness must be REFUSED rather than recorded.

    A proof nobody can re-check is worse than no proof: the row then LOOKS
    proven, which is the state this whole rung exists to stop happening by
    accident.
    """

    def _proof(self, replay_result):
        """Drive `_close_landed_content` with the replay stubbed."""
        with mock.patch.object(landreq, "_content_equivalent_carrier",
                               return_value=("c" * 40, None)), \
             mock.patch.object(landreq, "_commit_content_identity",
                               return_value=("p", "d", "n")), \
             mock.patch.object(landreq, "_content_equivalent_witness",
                               return_value={"v": 1}), \
             mock.patch.object(landreq, "_content_equivalent_replay",
                               return_value=replay_result):
            return landreq._close_landed_content(
                "/nonexistent/.git", "a" * 40, "refs/x", "b" * 40, "git said")

    def test_a_witness_that_does_not_replay_is_REFUSED_at_write(self):
        """LOAD-BEARING MUTATION: delete the `if replayed is not True` rung.
          python3 -m unittest tests.test_landreq.ContentWitnessReplaysAtWriteTest\\
.test_a_witness_that_does_not_replay_is_REFUSED_at_write
          -> AssertionError: 'content-equivalent' is not None
        i.e. the close SUCCEEDS on a witness that cannot re-derive its own
        claim, which is exactly the state the ruling forbids."""
        # UNCONDITIONAL POSITIVE CONTROL, same door, same stubs: a witness
        # that DOES replay still closes. Without it this arm would pass under
        # a mutation that refused everything.
        mode, carrier, witness, err = self._proof((True, None))
        self.assertEqual(mode, landreq.CONTENT_EQUIVALENT)
        self.assertIsNone(err)
        self.assertEqual(witness, {"v": 1})

        # A MEASURED CONTRADICTION at write -> refuse.
        mode, carrier, witness, err = self._proof((False, "tree moved"))
        self.assertIsNone(mode)
        self.assertIn("does not replay at write", err or "")
        self.assertIn("tree moved", err or "")

    def test_an_UNREADABLE_replay_also_refuses_AT_WRITE(self):
        """The asymmetry that matters, and it is deliberate: at WRITE, None is
        a refusal — the witness was built from these objects seconds ago, so
        failing to re-read them means the measurement is broken right now. At
        READ, None must NOT be False, because a clone that later pruned an
        object has an unreadable measurement and not a disproven proof.
        Same helper, opposite handling, because the question differs."""
        mode, carrier, witness, err = self._proof((None, "does not resolve"))
        self.assertIsNone(mode)
        self.assertIn("does not replay at write", err or "")


class ContentWitnessSurvivesThePersistTest(LandReqBase):
    """The acceptance edge: PRE-WRITE REPLAY CANNOT DETECT SCHEMA
    FILTERING.

    The close-time re-check runs BEFORE the write, so it says nothing about
    whether the record survived `_record_close_proven`'s schema filter — which
    is precisely the defect that shipped: witness computed, passed, dropped,
    and all five rung arms green over it. This class re-reads the PERSISTED
    row and asserts the proof is still there, then re-derives from what was
    read rather than from what was sent.
    """

    def _witness(self):
        """(gitdir, witness, reviewed tip) over a REAL pair, so replay answers True.

        This used to pass self.side as BOTH source and carrier and call that a
        real witness. It was real and it could never replay: the write
        boundary requires the carrier be reachable from the pinned trunk, and
        self.side is the commit trunk does not have. The arm's last assertion
        — that the re-read witness still replays — was therefore asserting
        something the fixture made impossible.
        """
        gitdir = os.path.join(self.repo, ".git")
        reviewed, carrier = self.content_equivalent_pair()
        ident = landreq._commit_content_identity(gitdir, reviewed)
        w = landreq._content_equivalent_witness(
            gitdir, reviewed, carrier, "refs/heads/" + self.main,
            self.git("rev-parse", self.main), ident, 1)
        self.assertIsNotNone(w, "the fixture must build a real witness")
        return gitdir, w, reviewed

    def test_the_persisted_row_still_carries_the_witness_and_anchor(self):
        """LOAD-BEARING MUTATION: drop "content_witness" from the landed tuple
        in dispatches._CLOSE_STATE_FIELDS.
          python3 -m unittest tests.test_landreq\\
.ContentWitnessSurvivesThePersistTest\\
.test_the_persisted_row_still_carries_the_witness_and_anchor
          -> AssertionError: None is not an instance of <class 'dict'>
        The schema arm elsewhere asserts MEMBERSHIP; this asserts the record
        actually SURVIVES a close, which is a different fact — a schema can
        name a key the assembly never emits."""
        gitdir, w, reviewed = self._witness()
        anchor = dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, w)
        row = self.dispatch(ref=reviewed)
        self.mark_verdict(row["id"], reviewed, "ok", polarity="approve")
        out, err = dispatches._record_close_proven(
            row["id"], "landed", reviewed, evidence="content carried",
            closing_repo_id=self.repo,
            closing_trunk_ref="refs/heads/" + self.main,
            closing_trunk_sha=self.git("rev-parse", self.main),
            proof_mode=landreq.CONTENT_EQUIVALENT,
            content_witness=w, content_witness_anchor=anchor)
        # POSITIVE ON THE WRITER'S OWN RESULT: `out` and `err` are two
        # channels of one call, and asserting only the absence of an error
        # would hold for a writer that returned (None, None) — which is
        # exactly what a refused close looks like from here.
        self.assertIsNone(err, err)
        self.assertEqual(out["close_reason"], "landed")

        # RE-READ THE PERSISTED ROW — not `out`, and not what I sent.
        lr = landreq.get(row["id"])[0]
        self.assertEqual(lr.get("close_proof_mode"),
                         landreq.CONTENT_EQUIVALENT)   # MUST-HIT
        stored = lr.get("content_witness")
        self.assertIsInstance(stored, dict)
        self.assertEqual(lr.get("content_witness_anchor"), anchor)

        # RECOMPUTE FROM WHAT WAS READ, so a witness mangled in transit fails.
        self.assertEqual(
            dispatches._proof_anchor(landreq.CONTENT_ALGORITHM, stored),
            anchor)

        # AND THE RE-READ WITNESS MUST STILL REPLAY.
        self.assertEqual(landreq._content_equivalent_replay(gitdir, stored),
                         (True, None))


class LandingProofLadderOrderTest(unittest.TestCase):
    """The landing ladder is ORDERED, and the order is the guarantee:

        ancestry > patch-identity > recorded translation > content equivalence

    A recorded translation is a MECHANICAL identity map that Git then proves.
    Content equivalence erases context and proves a strictly weaker property.
    So a weaker rung must never be reached by stepping over a stronger one —
    not when the stronger one says no, and above all not when it cannot be
    read at all.
    """

    def _proof(self, landing, table=(None, None), carrier=("c" * 40, None)):
        """Drive the shared proof owner with each rung stubbed, recording
        whether the content rung was consulted at all."""
        consulted = []

        def content(*a, **kw):
            consulted.append(True)
            return carrier

        with mock.patch.object(landreq, "_landing_proof",
                               return_value=landing), \
             mock.patch.object(landreq, "_ref_translations_checked",
                               return_value=table), \
             mock.patch.object(landreq, "_content_equivalent_carrier", content), \
             mock.patch.object(landreq, "_commit_content_identity",
                               return_value=("p", "d", "n")), \
             mock.patch.object(landreq, "_content_equivalent_witness",
                               return_value={"v": 1}), \
             mock.patch.object(landreq, "_content_equivalent_replay",
                               return_value=(True, None)):
            out = landreq._close_landed_proof("/x/.git", "a" * 40, "refs/x",
                                              "b" * 40)
        return out, consulted

    def test_an_UNREADABLE_translation_map_fails_closed_on_BOTH_entry_states(self):
        """A map that cannot be READ is an unreadable measurement, not an
        absence — and stepping past it to a weaker rung would let an
        unreadable stronger proof be replaced by a readable weaker one.

        LOAD-BEARING MUTATION: run `_ref_translations_checked()` only when the
        direct proof is "unknown", sending "absent" straight to content.
          python3 -m unittest tests.test_landreq.LandingProofLadderOrderTest\\
.test_an_UNREADABLE_translation_map_fails_closed_on_BOTH_entry_states
          -> AssertionError: Lists differ: [True] != []
        The verdict alone cannot catch that reorder — both versions refuse or
        close somewhere — so the CONSULTED list is the assertion."""
        for landing in ("absent", "unknown"):
            (mode, _t, _w, err), consulted = self._proof(
                landing, table=({}, "permission denied"))
            self.assertIsNone(mode, landing)
            self.assertIn("sidecar is unreadable", err or "", landing)
            self.assertEqual(consulted, [],
                             "%s must not reach the content rung when the "
                             "stronger rung is unreadable" % landing)

    def test_a_RECORDED_TRANSLATION_outranks_content_on_BOTH_entry_states(self):
        """A rewritten sha with a recorded mapping is a STRONGER proof and must
        win, so a recorded rewrite is never downgraded to the weaker mode.

        LOAD-BEARING MUTATION: same reorder as above.
          -> AssertionError: 'content-equivalent' != 'translated-ancestor'"""
        table = ({"a" * 40: "d" * 40}, None)
        with mock.patch.object(landreq, "_sha", return_value=True):
            for landing in ("absent", "unknown"):
                calls = {"n": 0}

                def proof(_gitdir, sha, _ref):
                    # the reviewed tip is unplaceable; its TRANSLATION is not
                    calls["n"] += 1
                    return landing if sha == "a" * 40 else "ancestor"

                consulted = []
                with mock.patch.object(landreq, "_landing_proof", proof), \
                     mock.patch.object(landreq, "_ref_translations_checked",
                                       return_value=table), \
                     mock.patch.object(
                         landreq, "_content_equivalent_carrier",
                         lambda *a, **k: (consulted.append(True),
                                          ("c" * 40, None))[1]):
                    mode, translated, _w, err = landreq._close_landed_proof(
                        "/x/.git", "a" * 40, "refs/x", "b" * 40)
                self.assertIsNone(err, landing)
                self.assertEqual(mode, "translated-ancestor", landing)
                self.assertEqual(translated, "d" * 40, landing)
                self.assertEqual(consulted, [], landing)


class ProjectionAsksOneRefTwiceTest(IsolatedEstateTest):
    """Slice 3: the same patch question, asked under two names for one commit.

    THE ARM COUNTS SPAWNS, NOT ANSWERS, and that is the whole point. The two
    ref spellings always AGREED — `refs/heads/main` and `refs/remotes/origin/main`
    resolve to the same commit on a synced checkout, so `git cherry` returned
    identical output both times. A test asserting the answers match passes on
    the unfixed code. The defect was never a wrong answer; it was paying twice
    for a right one, and only an argv count can see that.

    Measured on trunk with a git-argv shim over one `lr list --all`:
    3908 spawns, 548 `cherry refs/heads/main <SHA>` and 547
    `cherry refs/remotes/origin/main <SHA>`, 547 shas asked under BOTH — 14% of
    every git call the projection makes.
    """

    def setUp(self):
        # CHAIN, never replace: the estate isolation lives in the base and a
        # silent override would put this class's arms back on the operator's
        # real ledger.
        super().setUp()
        import shutil
        import subprocess
        import tempfile
        self.root = tempfile.mkdtemp(prefix="lr-refnorm-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        g = lambda *a: subprocess.run(["git", "-C", self.root] + list(a),
                                      capture_output=True, text=True)
        g("init", "-q", ".")
        g("config", "user.email", "r@refnorm.local")
        g("config", "user.name", "refnorm")
        with open(os.path.join(self.root, "f.py"), "w") as f:
            f.write("# base\n")
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        # Both trunk spellings, same commit — the ordinary state of a synced
        # checkout, and the state in which the duplicate is pure waste.
        head = g("rev-parse", "HEAD").stdout.strip()
        g("update-ref", "refs/heads/main", head)
        g("update-ref", "refs/remotes/origin/main", head)
        # THE LANE TIP CARRIES NO DIFF OF ITS OWN, and that is what routes
        # this fixture to the rung it is about. `_landing_proof` answers from
        # the trunk INDEX whenever the index is non-empty and the tip has its
        # own patch-id, and only falls through to `git cherry` otherwise. An
        # empty tip has no id, so the cherry rung is the rung that answers --
        # which is the rung these two arms measure.
        #
        # It used to reach cherry for a different and accidental reason: this
        # repository's trunk WAS its root commit, roots were unhashable, and
        # the index therefore came back empty. That was a defect standing in
        # for a fixture. A root's content is now measured like any other
        # commit, so the empty index is gone and the route has to be real.
        g("commit", "-q", "--allow-empty", "-m", "lane")
        self.tip = g("rev-parse", "HEAD").stdout.strip()
        self.gitdir = os.path.join(self.root, ".git")
        self.local, self.upstream = "refs/heads/main", "refs/remotes/origin/main"

    def _spy(self):
        """Record every real spawn, below the memo, and return the log."""
        seen = []
        original = landreq._git_spawn

        def spy(gitdir, args, input_text, env=None):
            seen.append(tuple(args))
            return original(gitdir, args, input_text, env)

        landreq._git_spawn = spy
        self.addCleanup(setattr, landreq, "_git_spawn", original)
        return seen

    def test_two_spellings_of_one_trunk_cost_ONE_cherry(self):
        seen = self._spy()
        with projscope.scope():
            a = landreq._landing_proof(self.gitdir, self.tip, self.local)
            b = landreq._landing_proof(self.gitdir, self.tip, self.upstream)
        cherries = [c for c in seen if c and c[0] == "cherry"]
        # CONTROL: the door really did run and really did reach cherry. Without
        # this, a _landing_proof that returned early on some unrelated path
        # would satisfy the count assertion below by doing nothing at all.
        self.assertGreaterEqual(len(cherries), 1,
                                "no cherry spawn at all — the arm measured a "
                                "door that never opened: %r" % (seen,))
        self.assertEqual(a, b,
                         "two names for one commit disagreed, which would make "
                         "this a correctness bug rather than a spawn one")
        self.assertEqual(len(cherries), 1,
                         "the same patch question was spawned once per ref "
                         "SPELLING rather than once per commit: %r" % cherries)

    def test_the_cherry_argv_names_a_sha_not_a_ref(self):
        """The mechanism, asserted directly: whatever else changes, the memo key
        can only collapse if the argv stops carrying the ref NAME."""
        seen = self._spy()
        with projscope.scope():
            landreq._landing_proof(self.gitdir, self.tip, self.local)
        cherries = [c for c in seen if c and c[0] == "cherry"]
        self.assertEqual(len(cherries), 1)
        upstream_arg = cherries[0][1]
        self.assertNotIn("refs/", upstream_arg,
                         "cherry still names a ref: %r" % (cherries[0],))
        self.assertEqual(len(upstream_arg), 40,
                         "cherry's upstream is not a full sha: %r" % upstream_arg)


class DryRunResultContractTest(unittest.TestCase):
    """The CLI spends only a normalized close result, in text or JSON."""

    rid = "ce5e3991566f0000"

    def invoke(self, result, *extra):
        args = ["close", self.rid[:12], "--reason", "carried",
                "--evidence", "carried by its successor", *extra]
        with mock.patch.object(landreq, "close",
                               return_value=(result, None)) as close:
            got = run(args)
        return got, close

    def test_a_dry_run_refuses_an_unnormalized_success(self):
        (rc, out, err), close = self.invoke(
            {"id": self.rid, "close_reason": "carried"}, "--dry-run")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("does not bind this dry run", err)
        self.assertIs(close.call_args.kwargs["dry_run"], True)

    def test_normalized_dry_run_is_unambiguous_in_text_and_json(self):  # noqa: VACUOUS_ASSERTION — rc and exact JSON are unconditional controls; the loop pins each required text marker
        result = {"id": self.rid, "dry_run": True, "reason": "carried",
                  "idempotent": False, "would_append": True}
        (rc, out, err), close = self.invoke(result, "--dry-run")
        self.assertEqual((rc, err), (0, ""))
        for marker in ("[dry-run]", self.rid[:12], "WOULD close",
                       "--reason carried", "nothing appended"):
            self.assertIn(marker, out)
        self.assertNotIn("CLOSED (", out)
        self.assertIs(close.call_args.kwargs["dry_run"], True)

        (rc, out, err), close = self.invoke(result, "--dry-run", "--json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), result)
        self.assertIs(close.call_args.kwargs["dry_run"], True)

    def test_an_idempotent_dry_run_reports_the_standing_terminal(self):  # noqa: VACUOUS_ASSERTION — rc and the forbidden WOULD-close marker are unconditional controls; the loop pins each standing-state marker
        result = {"id": self.rid, "dry_run": True, "reason": "carried",
                  "idempotent": True, "would_append": False,
                  "close_proof_mode": "ancestor",
                  "close_delivery_class": landreq.DELIVERY_PROCESS,
                  "close_delivery_restart": "the web unit"}
        (rc, out, err), close = self.invoke(result, "--dry-run")
        self.assertEqual((rc, err), (0, ""))
        for marker in ("[dry-run]", self.rid[:12], "ALREADY closed",
                       "--reason carried", "proof ancestor",
                       "LIVE OPEN LOOP", "the web unit", "no append needed"):
            self.assertIn(marker, out)
        self.assertNotIn("WOULD close", out)
        self.assertIs(close.call_args.kwargs["dry_run"], True)

    def test_a_live_close_rejects_a_dry_result(self):
        result = {"id": self.rid, "dry_run": True, "reason": "carried"}
        (rc, out, err), close = self.invoke(result)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("dry-run result for a live close", err)
        self.assertIs(close.call_args.kwargs["dry_run"], False)

    def test_a_live_close_keeps_the_terminal_identity(self):  # noqa: VACUOUS_ASSERTION — exact non-empty stdout and clean stderr jointly pin the successful live terminal
        result = {"id": self.rid, "close_reason": "carried"}
        (rc, out, err), close = self.invoke(result)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "helm lr: %s CLOSED (CARRIED)\n" % self.rid[:12])
        self.assertIs(close.call_args.kwargs["dry_run"], False)


class BatchExistenceTest(unittest.TestCase):
    """The batched existence probe: 946 per-row `cat-file -e` spawns became one
    `--batch-check` per repository. These arms pin the two ways that batching
    stopped answering the question the per-row probe asked.
    """

    def test_a_BLOB_is_present_BARE_and_NOT_a_commit_THROUGH_THE_CACHE(self):  # noqa: VACUOUS_ASSERTION — every absence claim here has an unconditional positive control on the same observable: the blob is asserted PRESENT bare before any peel claim, and a real commit answers True through the same armed cache with _git disarmed
        """BOTH POLARITIES, AND THE ANSWER MUST COME FROM THE CACHE.

        The batch sends `<sha>^{commit}` only, so its answers are about
        COMMIT-NESS. A reader taking them as bare existence calls a present
        blob ABSENT — the defect that deleted the bare wrapper.

        MY FIRST VERSION OF THIS ARM PROVED NONE OF THAT. It passed a fresh
        `{}` as the cache, so `_objexist_map` returned {} and every call took
        the per-sha `cat-file -e` FALLBACK; the batch maps it computed were
        never installed under the real key. Deleting the cache-hit branch
        entirely would have left it GREEN, which means the 946-spawns-to-one
        optimisation was unguarded at the exact seam it exists to change. A
        reviewer measured that. So this version ARMS the cache the way the
        prefetch does and then DISARMS `_git`, so a fallback cannot answer."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_EMAIL="t@t")
        tmp = tempfile.mkdtemp(prefix="helm-test-polarity-")
        self.addCleanup(shutil.rmtree, tmp, True)

        def git(*a, **kw):
            return subprocess.run(("git",) + a, cwd=tmp, capture_output=True,
                                  text=True, env=env, **kw)

        self.assertEqual(git("init", "-q", "-b", "main").returncode, 0)
        blob = git("hash-object", "-w", "--stdin", input="hello\n").stdout.strip()
        self.assertTrue(blob, "no blob was written, so nothing below is tested")
        with open(os.path.join(tmp, "f.txt"), "w") as fh:
            fh.write("x\n")
        git("add", "-A")
        git("commit", "-qm", "c")
        head = git("rev-parse", "HEAD").stdout.strip()
        self.assertTrue(head)
        gitdir = os.path.join(tmp, ".git")

        # BARE: the blob really is present. Asserted BEFORE any peel claim, so
        # a failure here voids the test instead of passing it.
        self.assertTrue(landreq._object_exists(gitdir, blob),
                        "the blob is not present; the polarity test is void")

        # ARM THE CACHE THE WAY THE PREFETCH DOES, then buy it on first need.
        cache = {("objexist-pending", gitdir): [blob, head]}
        self.assertFalse(landreq._commit_exists_cached(gitdir, blob, cache),
                         "a blob answered TRUE to the commit question")
        self.assertIn(("objexist", gitdir), cache,
                      "the batch was never installed, so the cache branch "
                      "below cannot be the thing under test")

        # DISARM THE SPAWN. From here a fallback CANNOT answer, so anything
        # returned came out of the batch. This is what makes the assertions
        # above and below statements about the cache rather than about git.
        def refuse(*a, **kw):
            raise AssertionError("fell through to a per-sha spawn: the cached "
                                 "answer was not used")

        with mock.patch.object(landreq, "_git", side_effect=refuse):
            self.assertFalse(landreq._commit_exists_cached(gitdir, blob, cache))
            # UNCONDITIONAL POSITIVE CONTROL, same armed cache, same disarmed
            # git: a real COMMIT answers True. Without it the two False
            # results above would also pass if the cache answered False to
            # everything.
            self.assertTrue(landreq._commit_exists_cached(gitdir, head, cache),
                            "the cache cannot answer TRUE for a real commit")

    def test_the_FALLBACK_path_still_asks_the_peel_question_PER_SHA(self):  # noqa: VACUOUS_ASSERTION — real commit/blob positive and negative controls plus captured peel argv prove both fallback polarities and the exact question
        """CURING A WEAK ARM DELETED THE COVERAGE IT ACCIDENTALLY PROVIDED.

        The predecessor passed a fresh {} and was therefore weak evidence about
        the CACHE — the finding that replaced it. But those same calls were the
        ONLY arms driving the live MISS/FALLBACK branch, and production takes
        that branch whenever there is no prefetch, the spawn fails or returns
        nonzero, the output is misaligned, or a line is malformed. So the
        accelerator's failure path — the one that matters most, because it runs
        exactly when the optimisation is broken — went from weakly covered to
        not covered at all.

        A regression to a default False, to no spawn, or to a BARE `<sha>`
        would stay green and would mark a valid commit unobservable precisely
        when the batch is down."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                   GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_EMAIL="t@t")
        tmp = tempfile.mkdtemp(prefix="helm-test-fallback-")
        self.addCleanup(shutil.rmtree, tmp, True)

        def git(*a, **kw):
            return subprocess.run(("git",) + a, cwd=tmp, capture_output=True,
                                  text=True, env=env, **kw)

        self.assertEqual(git("init", "-q", "-b", "main").returncode, 0)
        blob = git("hash-object", "-w", "--stdin", input="hello\n").stdout.strip()
        with open(os.path.join(tmp, "f.txt"), "w") as fh:
            fh.write("x\n")
        git("add", "-A")
        git("commit", "-qm", "c")
        head = git("rev-parse", "HEAD").stdout.strip()
        self.assertTrue(blob and head)
        gitdir = os.path.join(tmp, ".git")

        real_git = landreq._git
        seen = []

        def spy(g, *a, **kw):
            seen.append(a)
            return real_git(g, *a, **kw)

        # CASE 1 — NO PREFETCH AT ALL: the map is empty, so both calls must
        # take the per-sha probe and both polarities must still be right.
        with mock.patch.object(landreq, "_git", side_effect=spy):
            self.assertTrue(landreq._commit_exists_cached(gitdir, head, {}))
            self.assertFalse(landreq._commit_exists_cached(gitdir, blob, {}))
        peels = [a for a in seen if a and str(a[-1]).endswith("^{commit}")]
        self.assertTrue(peels,
                        "the fallback never asked the PEEL question — a bare "
                        "<sha> would call a blob a commit")
        self.assertTrue(any(str(a[-1]).startswith(head) for a in peels),
                        "the peel was not asked for the commit under test")

        # CASE 2 — THE BATCH FAILS. _objexist_map caches {} on failure, so
        # every later row falls through. Same two polarities must hold.
        seen[:] = []
        cache = {("objexist-pending", gitdir): [blob, head]}
        with mock.patch.object(landreq, "_object_exists_batch", return_value=None):
            with mock.patch.object(landreq, "_git", side_effect=spy):
                self.assertTrue(landreq._commit_exists_cached(gitdir, head, cache))
                self.assertFalse(landreq._commit_exists_cached(gitdir, blob, cache))
        failed = cache.get(("objexist", gitdir))
        self.assertEqual(failed, {},
                         "a failed batch must cache an empty map so it is not "
                         "re-bought once per row")
        self.assertIsInstance(
            failed, landreq._ObjectExistenceBatchFailure,
            "batch failure was erased into ordinary emptiness, so restart "
            "witnessing cannot distinguish unreadable from an empty result")
        self.assertTrue([a for a in seen if str(a[-1]).endswith("^{commit}")],
                        "a failed batch did not fall through to the per-sha probe")

    def test_the_batch_asks_the_PEEL_question_not_an_advertised_type(self):
        """`cat-file -e <sha>^{commit}` and "batch-check says type is commit"
        are not equivalent in EITHER polarity. An ANNOTATED TAG peels to a
        commit but types `tag`, so a type check calls a readable row blind. A
        truncated loose commit advertised `commit 169` while the peel failed
        rc=128, so a type check turned UNOBSERVABLE into observable-with-
        corrupt-state and let lifecycle reasoning run on a broken object — the
        dangerous direction, because it MANUFACTURES an observation."""
        sent = {}

        def fake_git(gitdir, *args, input_text=None, env=None):
            sent["args"] = args
            sent["stdin"] = input_text
            lines = [l for l in (input_text or "").split("\n") if l]
            out = []
            for l in lines:
                # A VALID 40-HEX PEELED OID, deliberately DIFFERENT from the
                # sha we asked about — that is the annotated-tag case this arm
                # exists for. It used to be `deadbeef`, a short oid the parser
                # accepted back when any three tokens meant commit.
                out.append(("%s commit 42" % ("c" * 40)) if l.startswith("a")
                           else "%s missing" % l)
            return types.SimpleNamespace(returncode=0, stdout="\n".join(out) + "\n")

        with mock.patch.object(landreq, "_git", fake_git):
            got = landreq._object_exists_batch("/g", ["a" * 40, "b" * 40])
        self.assertIn("--batch-check", sent["args"])
        self.assertTrue(all(l.endswith("^{commit}")
                            for l in sent["stdin"].split("\n") if l),
                        "the batch asked about bare shas, not the peel")
        # ANSWERS ARE ZIPPED TO QUESTIONS: a resolved line reports the PEELED
        # oid (for a tag, the commit — NOT the sha we asked about), while a
        # refused line echoes the expression. Reading the first field as a key
        # mis-maps exactly the annotated-tag case.
        self.assertEqual(got["a" * 40], (True, "commit"))
        self.assertEqual(got["b" * 40], (False, ""))

    def test_a_misaligned_batch_is_UNKNOWN_not_a_partial_map(self):
        """If answers cannot be zipped to questions there is no key to trust,
        and a partial map would silently answer for some rows and not others."""
        def short(gitdir, *args, input_text=None, env=None):
            return types.SimpleNamespace(returncode=0, stdout="deadbeef commit 42\n")
        with mock.patch.object(landreq, "_git", short):
            self.assertIsNone(
                landreq._object_exists_batch("/g", ["a" * 40, "b" * 40]))
        # POSITIVE CONTROL: an ALIGNED, WELL-FORMED reply does answer.
        # This control used to send `deadbeef commit 42` and assert truth —
        # an INVALID SHORT OID. A reviewer measured that it entrenched the
        # very defect it was meant to control: the parser accepted any three
        # tokens, so the arm proved the parser would believe garbage.
        def aligned(gitdir, *args, input_text=None, env=None):
            n = len([l for l in (input_text or "").split("\n") if l])
            good = "%s commit 42" % ("c" * 40)
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join([good] * n) + "\n")
        with mock.patch.object(landreq, "_git", aligned):
            self.assertTrue(landreq._object_exists_batch("/g", ["a" * 40, "b" * 40]))

    def test_a_MALFORMED_success_line_answers_NOTHING(self):
        """`garbage blob nope` is three tokens and used to manufacture
        (True, "commit"). Protocol corruption is exactly when a confident
        answer is most dangerous, so the key is OMITTED and the caller falls
        back to its own per-sha probe. Refusing costs one spawn; answering
        wrongly costs a verdict."""
        asked = ["a" * 40, "b" * 40]

        def junk(gitdir, *args, input_text=None, env=None):
            n = len([l for l in (input_text or "").split("\n") if l])
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join(["garbage blob nope"] * n) + "\n")
        with mock.patch.object(landreq, "_git", junk):
            got = landreq._object_exists_batch("/g", asked)
        self.assertEqual(got, {}, "malformed lines manufactured an answer")
        # UNCONDITIONAL POSITIVE CONTROL on the same helper and shape: a
        # well-formed line DOES answer, so the empty map above is about the
        # grammar and not about a helper that never answers.
        def good(gitdir, *args, input_text=None, env=None):
            n = len([l for l in (input_text or "").split("\n") if l])
            line = "%s commit 7" % ("d" * 40)
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join([line] * n) + "\n")
        with mock.patch.object(landreq, "_git", good):
            got2 = landreq._object_exists_batch("/g", asked)
        self.assertEqual(sorted(got2), sorted(asked))
        self.assertTrue(all(v == (True, "commit") for v in got2.values()))

    def test_a_FOREIGN_refusal_is_not_zipped_onto_this_sha(self):
        """git echoes the QUERY it could not resolve. Accepting any line whose
        LAST token is missing/ambiguous zipped a SHIFTED or FOREIGN answer
        onto this sha and cached it as DEFINITE ABSENCE — the batch would
        report a live commit gone because another row's line landed here."""
        asked = ["a" * 40, "b" * 40]

        def foreign(gitdir, *args, input_text=None, env=None):
            n = len([l for l in (input_text or "").split("\n") if l])
            other = "%s^{commit} missing" % ("f" * 40)   # not either asked sha
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join([other] * n) + "\n")
        with mock.patch.object(landreq, "_git", foreign):
            got = landreq._object_exists_batch("/g", asked)
        self.assertEqual(got, {}, "a foreign refusal was cached as absence")
        # UNCONDITIONAL POSITIVE CONTROL: the seat's OWN refusal, echoed
        # exactly, IS accepted — so the empty map is about identity, not about
        # refusals being ignored wholesale.
        def mine(gitdir, *args, input_text=None, env=None):
            asks = [l for l in (input_text or "").split("\n") if l]
            return types.SimpleNamespace(
                returncode=0,
                stdout="\n".join("%s missing" % a for a in asks) + "\n")
        with mock.patch.object(landreq, "_git", mine):
            got2 = landreq._object_exists_batch("/g", asked)
        self.assertEqual(sorted(got2), sorted(asked))
        self.assertTrue(all(v == (False, "") for v in got2.values()))

    def test_only_tips_the_projection_will_ASK_about_are_armed(self):
        """The prefetch was REPOSITORY-lazy where the invariant is
        ROW-OBSERVATION-lazy: it armed every eligible same-repo tip, so one
        live row's batch carried monotonic historical closures whose own `_lr`
        path passes git=False. That violates no-reobserve, and in a partial or
        promisor repository it can lazy-fetch or stall on dormant objects."""
        g = "/tmp/some/repo/.git"
        rows = [
            {"repo_id": g, "tip": "a" * 40, "status": "closed",
             "close_reason": "landed"},                      # monotonic
            {"repo_id": g, "tip": "b" * 40, "status": "verdict",
             "close_reason": "fix"},                         # live
            {"repo_id": g, "tip": "c" * 40, "status": "open"},
            {"repo_id": g, "tip": "d" * 40, "status": "closed",
             "abandoned": True, "close_reason": "fix"},
        ]
        cache = {}
        landreq._prefetch_object_existence(rows, cache)
        armed = cache.get(("objexist-pending", g), [])
        # THE POSITIVE CONTROL IS FIRST ON PURPOSE: an earlier version of this
        # check passed because NOTHING was armed — the fixture rows carried no
        # repo_id — so every "is not armed" assertion held for the wrong reason.
        self.assertIn("b" * 40, armed, "the live tip was not armed")
        self.assertEqual(len(armed), 1)
        self.assertNotIn("a" * 40, armed, "a monotonic closure was armed")
        self.assertNotIn("c" * 40, armed)
        self.assertNotIn("d" * 40, armed)

    def test_the_arming_predicate_is_the_one_the_projection_uses(self):
        """Two callers need the same answer, and computing it twice is how the
        batch came to disagree with the projection about which rows are asked.
        """
        self.assertTrue(landreq._will_observe_git(
            {"tip": "b" * 40, "status": "verdict", "close_reason": "fix"}))
        for row in ({"tip": "a" * 40, "status": "closed", "close_reason": "landed"},
                    {"tip": "c" * 40, "status": "open"},
                    {"status": "verdict", "close_reason": "fix"},
                    {"tip": "d" * 40, "status": "closed", "abandoned": True,
                     "close_reason": "fix"}):
            self.assertFalse(landreq._will_observe_git(row), row)

    def test_an_UNOWNED_row_is_neither_observed_nor_ARMED(self):
        """The rebase merge, pinned. `_observation_owned` reached trunk as a
        conjunct of the inline `git=` expression that this lane replaced with
        one door, so the rebase presented a MERGE as a CHOICE — and taking
        either side alone deletes a live rule.

        THE PREFETCH IS WHY THIS IS AN AUTHORITY BUG AND NOT A TIDINESS ONE.
        It groups armed tips BY `repo_id` and buys one `cat-file --batch-check`
        per gitdir, so an unowned row in the armed list is a git process
        spawned inside a repository this board has no standing to read — the
        exact class trunk's `_observation_owned` commit exists to stop, arriving
        by a NEW route that its own call-site guard cannot see.

        LOAD-BEARING MUTATION: delete the `_observation_owned` clause from
        `_will_observe_git`.
          -> AssertionError: a foreign row was ARMED for a batch spawn"""
        g = "/tmp/not/ours/.git"
        live = {"repo_id": g, "tip": "b" * 40, "status": "verdict",
                "close_reason": "fix"}
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, and it is the same dict with
        # one key added below — so an arming path that broke for an unrelated
        # reason cannot let the two absence assertions pass vacuously.
        self.assertTrue(landreq._will_observe_git(live))
        cache = {}
        landreq._prefetch_object_existence([live], cache)
        self.assertIn("b" * 40, cache.get(("objexist-pending", g), []),
                      "the owned control was not armed — the absence "
                      "assertions below would prove nothing")
        for mark in ("foreign", "project_unresolved"):
            row = dict(live, **{mark: True})
            self.assertFalse(landreq._will_observe_git(row), mark)
            cache = {}
            landreq._prefetch_object_existence([row], cache)
            self.assertEqual(cache.get(("objexist-pending", g), []), [],
                             "a %s row was ARMED for a batch spawn inside a "
                             "repository this board does not own" % mark)


class BatchIsSharedAcrossRowsTest(LandReqBase):
    """THE BATCHING CLAIM, DRIVEN THROUGH THE PRODUCTION DOOR.

    The lane that introduced the batch landed on one claim: git observation is
    bought ONCE PER REPOSITORY instead of once per sha.

    THE TWO SETS WERE DISJOINT, and that is the measurement rather than an
    impression. Ten arms across tests/ drove `project_raw` for real before this
    one (the mocked call sites are not drivers — one of them, the raw-snapshot
    index arm, was named to me as a driver and is not); NOT ONE of them so much
    as mentions `_prefetch_object_existence`, `_objexist_map` or
    `_object_exists_batch`, and NOT ONE of the arms that do mention them calls
    `project_raw` — those reach the primitives directly or hand them fabricated
    rows. So nothing exercised the claim through the entry point callers use,
    and A REGRESSION TO PER-SHA RESOLUTION WOULD HAVE KEPT EVERY EXISTING TEST
    GREEN. The reviewer raised exactly that; the lane landed with it uncured.

    A CALL COUNT IS NOT THE OBSERVABLE, which is why this arm does not use one
    alone. `assert_called_once` on the batch is satisfied by a batch carrying
    ONE of two tips while the other row quietly spawns its own `cat-file -e` —
    the half-regression, and the likeliest one. What a second batch cannot slip
    past is IDENTITY: the second row must be handed the SAME map OBJECT the
    first row bought, that one batch must carry BOTH tips, and the per-sha door
    must be SHUT while both rows are answered.
    """

    def two_observable_rows(self):
        """Two land loops in ONE repository, DISTINCT off-trunk tips, each in a
        state whose projection genuinely ASKS git about its tip.

        Both halves are asserted here rather than assumed: a fixture whose rows
        never reach `_will_observe_git`, or whose tips collide, would satisfy
        every claim below while measuring nothing."""
        self.git("checkout", "-q", "-b", "side-two", self.a)
        second = self.commit("side-two", path="i")
        self.git("checkout", "-q", self.main)
        rows = []
        for n, (lane, tip) in enumerate((("lane/batch-one", self.side),
                                         ("lane/batch-two", second))):
            row = self.dispatch(lane=lane, ref=tip)
            dispatches._mark_delivered(row["id"], "post-batch-%d" % n)
            self.mark_verdict(row["id"], tip, "ok", polarity="approve")
            rows.append((row["id"], tip))
        current = dispatches.snapshot()[0]
        self.assertEqual(len({tip for _rid, tip in rows}), 2,
                         "one tip twice is not two rows sharing a batch")
        self.assertEqual(len({current[rid]["repo_id"] for rid, _t in rows}), 1,
                         "the two rows name different repositories, so they "
                         "were never eligible for ONE batch")
        for rid, _tip in rows:
            self.assertTrue(landreq._will_observe_git(current[rid]),
                            "%s will not ask git at all, so it can neither "
                            "share a batch nor fall back" % rid[:12])
        return rows

    def test_project_raw_answers_BOTH_rows_from_ONE_shared_batch(self):
        """ARM THE PER-SHA FALLBACK, THEN DISARM IT.

        Phase 1 breaks the batch spawn and measures that BOTH rows really do
        take the per-sha `cat-file -e <sha>^{commit}` path. That is what makes
        phase 2 a trap and not a decoration: an arm that never armed the
        fallback proves nothing about it, because a disarmed door nothing walks
        through is silent either way.

        Phase 2 restores the batch and shuts that door. Every clause is then a
        statement about the batch: the two rows still project, one batch was
        bought, it carried both tips, and the second row read the FIRST row's
        map object rather than an equal one.

        LOAD-BEARING MUTATION: delete the `_prefetch_object_existence` call in
        `project_raw`, or arm per row instead of per repo.
          -> AssertionError: project_raw resolved a tip PER SHA
        """
        rows = self.two_observable_rows()
        tips = [tip for _rid, tip in rows]
        gitdir = dispatches._repo_info(self.repo)["repo_id"]
        peel = tuple(t + "^{commit}" for t in tips)
        real_git = landreq._git

        # PHASE 1 — ARM THE FALLBACK. With the batch spawn reporting FAILURE
        # `_objexist_map` caches {} and every row must buy its own probe.
        asked = []

        def spy(gd, *args, **kw):
            asked.append(args)
            return real_git(gd, *args, **kw)

        with mock.patch.object(landreq, "_object_exists_batch",
                               lambda gd, shas: None), \
                mock.patch.object(landreq, "_git", spy):
            armed, _armed_raw, armed_unavailable = landreq.project_raw()
        per_sha = {a[2] for a in asked
                   if a[:2] == ("cat-file", "-e") and len(a) > 2}
        self.assertIsNone(armed_unavailable, armed_unavailable)
        self.assertEqual(sorted(armed), sorted(rid for rid, _t in rows),
                         "the projection did not carry both rows, so the "
                         "per-sha measurement below is about something else")
        self.assertEqual(sorted(expr for expr in peel if expr in per_sha),
                         sorted(peel),
                         "with the batch DOWN a row did not fall through to a "
                         "per-sha probe — that door is not on this path, so "
                         "shutting it in phase 2 would prove nothing")

        # PHASE 2 — DISARM IT. A per-sha probe for either tip now records the
        # violation and raises. `project_raw` converts a row's exception into
        # `unavailable`, so the recorded list is what NAMES the defect and the
        # unavailable check is its second wall.
        fellback, maps, batches = [], [], []
        real_map, real_batch = landreq._objexist_map, landreq._object_exists_batch

        def refuse(gd, *args, **kw):
            if args[:2] == ("cat-file", "-e") and len(args) > 2 \
                    and args[2] in peel:
                fellback.append(args[2])
                raise AssertionError("per-sha resolution: " + args[2])
            return real_git(gd, *args, **kw)

        def watch_map(gd, cache):
            got = real_map(gd, cache)
            maps.append((gd, cache, got))
            return got

        def watch_batch(gd, shas):
            batches.append((gd, list(shas)))
            return real_batch(gd, shas)

        with mock.patch.object(landreq, "_git", refuse), \
                mock.patch.object(landreq, "_objexist_map", watch_map), \
                mock.patch.object(landreq, "_object_exists_batch", watch_batch):
            out, _raw, unavailable = landreq.project_raw()

        # THE PROJECTION STILL ANSWERED, WITH THE PER-SHA DOOR SHUT. Asserted
        # before any claim about the batch: rows that failed to project would
        # satisfy "no fallback fired" for the wrong reason entirely.
        self.assertEqual(fellback, [],   # noqa: VACUOUS_ASSERTION — the product law under test is that NO per-sha spawn occurs; its positive control is the same observable measured NON-empty in phase 1 above, plus the commit answers read out of the shared map below
                         "project_raw resolved a tip PER SHA: %s" % fellback)
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(sorted(out), sorted(rid for rid, _t in rows))
        for rid, _tip in rows:
            self.assertTrue(out[rid]["observable"],
                            "%s came out UNOBSERVABLE, so its git leg was "
                            "never answered by anything" % rid[:12])

        # ONE BATCH, AND IT CARRIES BOTH TIPS. The membership half is the
        # load-bearing one: a single batch holding one tip while the other row
        # spawned its own probe satisfies `assert_called_once` exactly.
        self.assertEqual(len(batches), 1,
                         "%d batches were bought for ONE repository" % len(batches))
        self.assertEqual(batches[0][0], gitdir)
        self.assertEqual(sorted(batches[0][1]), sorted(tips),
                         "the one batch did not carry both rows' tips")

        # AND THE SECOND ROW READ THE FIRST ROW'S MAP OBJECT, not an equal one.
        # Two batches produce two DISTINCT dicts; so does a dropped prefetch,
        # which hands every caller a fresh `{}`. Identity is what neither can
        # imitate, and the commit answers make it a snapshot that ANSWERED
        # rather than an empty dict shared by two rows that learned nothing.
        self.assertGreaterEqual(len(maps), 2,
                                "fewer than two rows consulted the batch")
        self.assertEqual(len({id(c) for _g, c, _m in maps}), 1,
                         "the rows were handed DIFFERENT caches — nothing was "
                         "shared, whatever the call count says")
        self.assertEqual(len({id(m) for _g, _c, m in maps}), 1,
                         "the rows were handed DIFFERENT maps, so the second "
                         "did not read the snapshot the first bought")
        self.assertEqual(sorted(tip for tip in tips
                                if maps[0][2].get(tip) == (True, "commit")),
                         sorted(tips),
                         "the shared snapshot holds no COMMIT answer for both "
                         "tips, so it is not what served these rows")


class LandInstructionBoundaryTest(unittest.TestCase):
    """The nudge's boundary is the WHOLE body, and its prefix is bounded.

    land_instruction runs on a delivery leg after a durable verdict, so an
    escape does not merely fail the caller -- it costs the wake the function
    exists to send.
    """

    def test_a_row_that_is_not_an_object_does_not_escape(self):
        """A receipt can be valid JSON and not a dict: it satisfies the
        parser and raises on .get, one line past a try that wrapped only the
        projection."""
        from helm import landreq

        def hostile(now=None, selector=None):
            return {"abc123": ["not", "an", "object"]}, None

        with mock.patch.object(landreq, "project", hostile):
            word, why = landreq.land_instruction("abc123")
        self.assertEqual("UNVERIFIED", word)
        self.assertIn("raised", str(why),
                      "the reason must name what happened, not be empty")

    def test_a_healthy_row_still_mints_its_word(self):
        """UNCONDITIONAL POSITIVE CONTROL: a widened try that swallowed
        everything would satisfy the arm above while breaking the feature."""
        from helm import landreq
        row = {"id": "abc123", "state": "CHANGES_REQUESTED",
               "terminal": False, "author": "a", "reviewer": "b"}

        def healthy(now=None, selector=None):
            return {"abc123": row}, None

        with mock.patch.object(landreq, "project", healthy):
            word, why = landreq.land_instruction("abc123")
        # A NON-READY LIFECYCLE STATE COMES BACK AS ITSELF: the widened try
        # must not swallow the body into a blanket UNVERIFIED, which would
        # satisfy the hostile-row arm above while destroying the feature.
        self.assertEqual("CHANGES_REQUESTED", word,
                         "the widened try swallowed a healthy row")
        self.assertIsNone(why, "a row that resolved has no failure reason")

    def test_a_full_id_under_a_different_dict_key_still_resolves(self):
        """MUST-HIT for the bound: the projection is keyed by its OWN
        convention, so a COMPLETE id can miss the dict lookup and still name
        exactly one row. A bound that loses this is worse than no bound."""
        from helm import landreq
        rows = {"chain-root-key": {"id": "abc123def456",
                                   "state": "CHANGES_REQUESTED",
                                   "terminal": False,
                                   "author": "a", "reviewer": "b"}}
        with mock.patch.object(landreq, "project",
                               lambda now=None, selector=None: (rows, None)):
            word, why = landreq.land_instruction("abc123def456")
        self.assertEqual("CHANGES_REQUESTED", word,
                         "an exact id was lost to a keying detail: %s" % why)

    def test_an_equal_length_id_does_not_ride_the_prefix_arm(self):
        """THE WIDENING THE BOUND EXISTS TO STOP. Real ids are one width, so
        a key that is not an exact match must not match a same-width row."""
        from helm import landreq
        rows = {"k": {"id": "abc123def456", "state": "READY",
                      "terminal": False, "author": "a", "reviewer": "b",
                      "observable": True}}
        with mock.patch.object(landreq, "project",
                               lambda now=None, selector=None: (rows, None)):
            word, why = landreq.land_instruction("abc123def999")
        self.assertEqual("UNVERIFIED", word)
        self.assertIn("not a land loop", str(why))

    def test_a_genuine_prefix_still_resolves(self):
        """POSITIVE CONTROL on the same observable: the bound must not refuse
        every non-exact key, or the arm above passes for the wrong reason."""
        from helm import landreq
        rows = {"k": {"id": "abc123def456", "state": "CHANGES_REQUESTED",
                      "terminal": False, "author": "a", "reviewer": "b"}}
        with mock.patch.object(landreq, "project",
                               lambda now=None, selector=None: (rows, None)):
            word, _why = landreq.land_instruction("abc123")
        self.assertEqual("CHANGES_REQUESTED", word,
                         "a genuine prefix stopped resolving")


class UnobservableIsARungTest(unittest.TestCase):
    """A row helm could not observe never mints plain READY.

    The discriminator needs a row that is otherwise FULLY ready — a live
    receipt for its token and a current base — because every other rung also
    answers UNVERIFIED. Against a half-built row this arm would pass with the
    rung deleted, which is exactly what a first version of it did.
    """

    TOKEN = "gate:" + "0" * 16
    BASE = {"state": "READY", "terminal": False, "author": "a",
            "reviewer": "b", "gate": TOKEN, "base_behind": 0}

    def _rung(self, lr):
        from helm import landreq
        with mock.patch.object(landreq, "contest_report",
                               lambda *a, **k: (False, None, None)), \
                mock.patch.object(landreq, "_gate_receipt_index",
                                  lambda: {self.TOKEN: {"status": "OK"}}):
            return landreq.ready_rung(lr)

    def test_an_otherwise_READY_row_mints_plain_READY(self):  # noqa: VACUOUS_ASSERTION — assertIsNone IS this class's positive control: it asserts the fixture reaches the BOTTOM of the ladder, which is the only thing that makes the UNVERIFIED arm beside it meaningful
        """UNCONDITIONAL POSITIVE CONTROL, and the arm below is meaningless
        without it: this fixture must reach the BOTTOM of the ladder."""
        self.assertIsNone(self._rung(dict(self.BASE, observable=True)),
                          "the fixture never reaches plain READY, so an "
                          "UNVERIFIED below proves nothing about this rung")

    def test_the_same_row_unobservable_is_UNVERIFIED(self):  # noqa: VACUOUS_ASSERTION — its unconditional structural control is test_an_otherwise_READY_row_mints_plain_READY above, same fixture, one field changed; mutation-verified: deleting the rung reddens this arm
        """One field changed against the control above."""
        self.assertEqual("UNVERIFIED",
                         self._rung(dict(self.BASE, observable=False)))

    def test_an_ABSENT_observable_key_is_NOT_a_refusal(self):
        """MISSING EVIDENCE IS NOT EVIDENCE AGAINST, and this arm exists
        because the first version of this rung got it backwards.

        `not lr.get("observable")` refuses on an ABSENT key as loudly as on a
        measured False — so every caller that never carried the field, which
        is every row built outside the projection, silently became
        READY-UNVERIFIED. Measured: 26 whole-suite failures, all of them
        existing arms asserting plain READY.

        The projection ALWAYS stamps the field (landreq.py builds it into the
        row), so refusing on the measured False loses nothing real.
        """
        lr = dict(self.BASE)
        lr.pop("observable", None)
        self.assertIsNone(self._rung(lr),
                          "an absent observable key was read as a refusal")

    def test_the_projection_stamps_the_field_this_rung_reads(self):
        """MUST-HIT beside the arm above: refusing only on a measured False
        is safe ONLY because the producer always stamps it. If that stops
        being true, this rung goes quiet and nothing else would say so."""
        from helm import landreq
        import inspect
        src = inspect.getsource(landreq)
        self.assertIn('"observable": observable', src,
                      "the projection no longer stamps observable, so an "
                      "is-False rung can no longer see an unobservable row")

    def test_store_facts_still_refuse_an_unobservable_row(self):
        """THE LADDER ORDER IS THE POINT: SELF-REVIEW needs no repository, so
        it must still fire on a row helm could not observe."""
        from helm import landreq
        lr = dict(self.BASE, observable=False, author="same", reviewer="same")
        self.assertEqual("SELF-REVIEW", landreq.ready_rung(lr),
                         "an unobservable row lost its store-fact refusal")


class LandNudgeCommandTest(unittest.TestCase):
    """The command is part of the instruction, and it must match the DOOR.

    Prescribing a bare `helm lr land` for deletion-bearing work sends the
    reader at a door that instantly refuses.
    """

    ROW = {"id": "abc123def456", "state": "READY", "terminal": False,
           "author": "a", "reviewer": "b", "observable": True,
           "repo_id": "/tmp/does-not-matter", "reviewed_tip": "f" * 40}

    def _parts(self, deletions):
        from helm import landreq
        with mock.patch.object(landreq, "project",
                               lambda now=None, selector=None: (
                                   {"abc123def456": dict(self.ROW)}, None)), \
                mock.patch.object(landreq, "_resolve_ref",
                                  lambda *a, **k: "trunkref"), \
                mock.patch.object(landreq, "_tracked_deletions", deletions):
            return landreq.land_nudge_instruction("abc123def456")

    def test_the_deletion_probe_runs_INSIDE_the_pinned_projection(self):
        """SNAPSHOT IDENTITY, WHICH A CALL COUNT CANNOT SEE.

        Counting project() calls was never the property. The deletion state
        is read with GIT, and a git read taken after the projection's scope
        has closed samples the repository at a LATER instant than the word
        did — so the word could describe the row at T1 while the command
        described the tree at T2. The count was one the whole time.

        Inside a projection scope, an identical question is answered from one
        memo, so asserting the probe runs while that scope is ACTIVE is what
        pins both halves to the same reading.
        """
        from helm import landreq, projscope
        seen = {}

        def spy(gitdir, trunk, tip):
            seen["active"] = projscope.active()
            return [], None

        with mock.patch.object(
                landreq, "project",
                lambda now=None, selector=None: ({"abc123def456": dict(self.ROW)},
                                                 None)), \
                mock.patch.object(landreq, "_resolve_ref",
                                  lambda *a, **k: "trunkref"), \
                mock.patch.object(landreq, "_tracked_deletions", spy):
            word, _why, cmd = landreq.land_nudge_instruction("abc123def456")
        self.assertTrue(seen.get("active"),
                        "the deletion probe ran OUTSIDE the pinned "
                        "projection, so the command can describe a later "
                        "tree than the word")
        # POSITIVE CONTROL on the same observable: the probe must actually
        # have run and the door must still mint a real instruction, or the
        # assertion above passes for a door that never probed at all.
        self.assertIn("active", seen, "the deletion probe never ran")
        self.assertTrue(word and cmd, "the door produced no instruction")

    def test_projscope_is_NOT_active_outside_the_door(self):
        """MUST-MISS for the arm above: `active()` must be able to answer
        False, or asserting True proves nothing about where the probe ran."""
        from helm import projscope
        self.assertFalse(projscope.active())

    def test_ONE_projection_per_wake(self):
        """ONE BOUNDARY MEANS ONE PROJECTION, and this is invisible to every
        output assertion.

        Asking the ladder for the word and projecting AGAIN for the command
        returns a perfectly good triple — while costing the wake two
        selected-chain projections and letting it pair a word read at T1 with
        deletion state read at T2. The row can change between them and
        nothing in the result says so. Only counting the calls sees it.
        """
        from helm import landreq
        calls = []

        def counting(now=None, selector=None):
            calls.append(selector)
            return {"abc123def456": dict(self.ROW)}, None

        with mock.patch.object(landreq, "project", counting), \
                mock.patch.object(landreq, "_resolve_ref",
                                  lambda *a, **k: "trunkref"), \
                mock.patch.object(landreq, "_tracked_deletions",
                                  lambda *a, **k: ([], None)):
            word, _why, cmd = landreq.land_nudge_instruction("abc123def456")
        self.assertEqual(1, len(calls),
                         "the wake projected %d times; the word and the "
                         "command must come from ONE reading" % len(calls))
        # POSITIVE CONTROL: one projection must still produce a real triple,
        # or this arm would pass for a door that projected zero times and
        # returned nothing useful.
        self.assertTrue(word and cmd, "one projection produced no instruction")

    def test_the_command_grows_the_flag_when_the_door_will_demand_it(self):
        _w, _y, cmd = self._parts(lambda *a, **k: (["a/deleted.py"], None))
        self.assertIn("--ack-deletions", cmd,
                      "deletion-bearing work was prescribed a command the "
                      "door refuses")

    def test_no_deletions_leaves_the_bare_command(self):
        """UNCONDITIONAL POSITIVE CONTROL: a MEASURED-empty deletion set must
        keep the real word and the bare command, or the degradations above
        pass while every instruction is UNVERIFIED."""
        word, why, cmd = self._parts(lambda *a, **k: ([], None))
        self.assertNotIn("--ack-deletions", cmd)
        self.assertIn("helm lr land", cmd, "the command vanished entirely")
        self.assertNotEqual("UNVERIFIED", word,
                            "a measured-empty scan degraded the word")
        self.assertIsNone(why)

    def test_an_UNMEASURABLE_deletion_set_degrades_the_WORD_too(self):
        """Unknown must not become a confident flag — and it must not become
        a silent bare command either.

        THE LAND DOOR REFUSES when it cannot compute the deletion set. So a
        nudge that drops the flag while its word still reads plainly READY
        prescribes a command the door will reject, which is the exact defect
        this door exists to close, one layer down.
        """
        word, why, cmd = self._parts(lambda *a, **k: (None, "git failed"))
        self.assertNotIn("--ack-deletions", cmd)
        self.assertEqual("UNVERIFIED", word,
                         "the word stayed confident over an unscannable "
                         "deletion set the door would refuse")
        self.assertIn("git failed", str(why),
                      "the reason must name what could not be measured")

    def test_a_RAISING_deletion_scan_degrades_the_word_too(self):
        def boom(*a, **k):
            raise RuntimeError("git exploded")
        word, why, cmd = self._parts(boom)
        self.assertEqual("UNVERIFIED", word)
        self.assertIn("raised", str(why))
        self.assertIn("helm lr land", cmd)

    def test_a_RAISING_measure_never_escapes(self):
        """It runs on a delivery leg; an exception here costs the wake."""
        def boom(*a, **k):
            raise RuntimeError("git exploded")
        _w, _y, cmd = self._parts(boom)
        self.assertIn("helm lr land", cmd)


class ChainProofPrunedTipCloseBase(LandReqBase):
    """A chain whose reviewed tip a history rewrite pruned: `_pruned_chain`
    builds it under a landed frontier, `_prune` removes the object for real,
    and `_authorize` records the author-bound APPROVE on one row.

    THIS CLASS HOLDS NO `test_*` METHOD, and that is its contract. unittest
    collects every inherited `test_*` again under each subclass's own id, so
    an arm written here runs once per subclass. A class that needs this
    fixture subclasses THIS class; its arms go in the subclass."""

    def _prune(self, sha):
        """ACTUALLY prune the object, rather than fabricate an absent sha.

        The first version of this fixture invented a 40-hex that was not an
        object — and the DISPATCH WRITER refused the row, correctly: you
        cannot mint a review of a commit that never existed. That refusal is
        the fixture telling the truth about the world. A real pruned tip was
        REVIEWED WHILE IT EXISTED and lost its object later, so the fixture
        has to do the same thing in the same order.
        """
        self.git("reflog", "expire", "--expire=now", "--expire-unreachable=now",
                 "--all")
        self.git("gc", "--prune=now", "--quiet")
        gitdir = os.path.join(self.repo, ".git")
        self.assertIs(landreq._object_exists(gitdir, sha), False,
                      "gc did not prune %s — the fixture is not reproducing "
                      "the condition under test" % sha[:12])

    def _authorize(self, kid, ftip=None):
        """Record the current author-bound APPROVE directly on `kid`.

        Split out of `_pruned_chain` so an arm can build an UNAUTHORIZED chain,
        assert the refusal, and then authorize the SAME chain as its control —
        one clause changed, one observable, no second fixture whose difference
        could be doing the work.
        """
        if ftip is None:
            rows, _v, _u = dispatches.snapshot_with_verdicts()
            ftip = rows[kid["id"]].get("reviewed_tip") \
                or rows[kid["id"]].get("ref")
        # Supply only the gate-binding seam. Retargeting a donor's event is
        # invalid now that its authority binds id, polarity, gate and clock.
        with mock.patch.object(dispatches.gate, "bind", return_value=(
                "VERIFIED", "cafebabe12345678", "test receipt")):
            got, why = self.mark_verdict(
                kid["id"], ftip, "reviewed frontier", polarity="approve")
        self.assertIsNone(why, why)
        self.assertEqual(dispatches.approval_tier_for_verdict(got),
                         ("none", None))
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(rows[kid["id"]].get("polarity"), "approve")
        self.assertEqual(rows[kid["id"]].get("gate"), "cafebabe12345678")
        self.assertEqual(dispatches.approval_tier_for_verdict(rows[kid["id"]]),
                         ("none", None))

    def _pruned_chain(self, with_approve=True, frontier_lands=True):
        """A row reviewed at a tip that is LATER pruned, under a landed frontier."""
        self.git("checkout", "-q", "-b", "doomed", self.a)
        doomed = self.commit("doomed", path="h")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=doomed, lane="lane/pruned", kind="review")
        _o, why = self.mark_verdict(row["id"], doomed,
                                          "findings", polarity="fix")
        self.assertIsNone(why)
        ftip = self.a if frontier_lands else self.side
        kid = self.dispatch(ref=ftip, lane="lane/pruned-r2", kind="review",
                            supersedes=row["id"])
        if with_approve:
            # Record on the actual frontier through the current producer.
            # The gate mint/bind is a seam here, not a real suite claim; the
            # real minted-approve subclass owns that integration control.
            # No copied event or rehashed authority can authorize this kid.
            self._authorize(kid, ftip)
        # THE PRUNE HAPPENS LAST, after every row that needed the object to
        # exist has been written — which is the real chronology too.
        self.git("branch", "-q", "-D", "doomed")
        self._prune(doomed)
        self.doomed = doomed
        return row, kid


class ChainProofPrunedTipCloseTest(ChainProofPrunedTipCloseBase):
    """The terminal for a chain a history rewrite pruned.

    THE CLASS THIS EXISTS FOR WAS MEASURED TWICE INDEPENDENTLY: eleven rows
    rendering CHANGES_REQUESTED forever because every measuring door refused,
    each for a correct and different reason. `landed` cannot compute a proof
    from an absent object; `carried` is fail-closed on an unaskable carriage;
    `subsumed` and `resolved` need a confirmation bound into the chain;
    `withdrawn` would record the work as ABSENT when it is not; `out-of-scope`
    is blocked by declared resolution debt.

    SO THE NEGATIVE CONTROLS ARE THE POINT, exactly as they are for the
    chain-polarity doors: a reason that closes rows nothing else can must not
    become a reason that closes ANYTHING. Every arm below names an input this
    door has to REJECT, and the positive is last so a door that said yes to
    everything could not reach it green.

    A subclass of THIS class runs every arm below again under its own id,
    which is right only for one that changes the fixture those arms read. A
    class that only needs the fixture subclasses
    ChainProofPrunedTipCloseBase."""

    def test_a_tip_that_still_EXISTS_is_refused(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the ok_plan close on the lines above: it drives the SAME door with a pruned chain and asserts a plan comes back, proving chain-proof can return one at all before this arm asserts it does not for a present tip. The rung cannot see the pairing because the control binds a different name, not because it is absent
        """MUST-MISS, and the load-bearing one: a row whose object is present
        belongs to a measuring door. If this passed, chain-proof would become
        a way to skip `carried`'s proof rather than a terminal for chains that
        cannot reach it."""
        # CONTROL FIRST AND UNCONDITIONAL, on the same observable: prove this
        # door can return a plan AT ALL. Without it, a chain-proof that refused
        # every input would satisfy the assertIsNone below perfectly — which is
        # the failure mode a must-miss exists to rule out, not to share.
        ok_row, _kid = self._pruned_chain()
        ok_plan, ok_why = landreq.close(
            ok_row["id"], "chain-proof", evidence="pruned by a rewrite",
            repo=self.repo, trunk=self.main, dry_run=True)
        self.assertIsNone(ok_why, ok_why)
        self.assertIsNotNone(ok_plan, "the door refuses everything")

        row = self.dispatch(ref=self.side, lane="lane/present", kind="review")
        # THE ROW NEEDS A VERDICT OR IT HAS NO reviewed_tip, and the door then
        # refuses for the WRONG reason — "cites no full reviewed tip" instead
        # of "not pruned". A must-miss that trips an EARLIER clause proves
        # nothing about the clause it names.
        _o, vwhy = self.mark_verdict(row["id"], self.side, "findings",
                                           polarity="fix")
        self.assertIsNone(vwhy, vwhy)
        plan, why = landreq.close(row["id"], "chain-proof",
                                  evidence="probe", repo=self.repo,
                                  trunk=self.main, dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("not pruned", why)

    def test_no_evidence_is_refused(self):
        row, _kid = self._pruned_chain()
        plan, why = landreq.close(row["id"], "chain-proof", repo=self.repo,
                                  trunk=self.main, dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("needs evidence", why)

    def test_a_chain_with_NO_APPROVE_is_refused(self):
        """MUST-MISS: the human leg is CHECKED, not assumed. Its content is a
        judgement this door makes no use of; its existence is the ledger fact
        that a cross-family reviewer looked."""
        row, _kid = self._pruned_chain(with_approve=False)
        plan, why = landreq.close(row["id"], "chain-proof",
                                  evidence="pruned by a rewrite",
                                  repo=self.repo, trunk=self.main,
                                  dry_run=True)
        self.assertIsNone(plan)
        # THE REASON CHANGED WITH THE CONTRACT, and the new one is truer. Under
        # the old door "no approve exists on this chain" was its own condition,
        # searched for chain-wide. Under authority transfer there is nothing to
        # search: a chain whose frontier was never reviewed has an OPEN
        # frontier, and an unreviewed frontier authorizes nothing. This arm
        # keeps its value as the GIT-BACKED version of that refusal — a real
        # pruned chain rather than a synthetic row map.
        self.assertIn("OPEN", why)
        self.assertIn("authorizes nothing", why)

    def test_a_frontier_that_did_NOT_land_is_refused(self):
        """MUST-MISS: without the frontier's own content on trunk the chain has
        nothing to lend its members, and the whole inference collapses."""
        row, _kid = self._pruned_chain(frontier_lands=False)
        plan, why = landreq.close(row["id"], "chain-proof",
                                  evidence="pruned by a rewrite",
                                  repo=self.repo, trunk=self.main,
                                  dry_run=True)
        self.assertIsNone(plan)
        self.assertIn("does not land", why)

    def test_the_pruned_chain_closes_AND_NEVER_READS_AS_CARRIAGE(self):
        """The positive, last. It asserts the RENDERING as hard as the close:
        `carried` answers whether THIS row's diff reached trunk, and that
        question died with the base object. A terminal that let a reader
        mistake one for the other would be worse than the open rows."""
        row, kid = self._pruned_chain()
        plan, why = landreq.close(row["id"], "chain-proof",
                                  evidence="objects pruned by a history "
                                           "rewrite; frontier landed",
                                  repo=self.repo, trunk=self.main,
                                  dry_run=True)
        self.assertIsNone(why, why)
        self.assertEqual(plan["reason"], "chain-proof")
        self.assertEqual(plan["proof_mode"], "chain-proof")
        self.assertNotEqual(plan["proof_mode"], "carried")
        self.assertEqual(plan["pruned_tip"], self.doomed)
        self.assertEqual(plan["frontier_id"], kid["id"])
        self.assertIn(plan["frontier_landing"], ("ancestor", "patch-equivalent"))
        # dry-run appended nothing — the same contract every other door keeps
        self.assertIsNone(landreq.get(row["id"])[0]["close_reason"])


class PreTierAttestationClosesOnlyThePrunedObligation(LandReqBase):
    """A current terminal act must not upgrade its historical APPROVE.

    Use real Git pruning, the real close writer and cold ledger replay. The
    frontier is a deliberately historical v3 event, never a rewritten v4 proof.
    """
    _prune = ChainProofPrunedTipCloseBase._prune
    _pruned_chain = ChainProofPrunedTipCloseBase._pruned_chain

    def test_attested_terminal_leaves_historical_approval_nonauthorizing(self):
        row, kid = self._pruned_chain(with_approve=False)
        current = dispatches.snapshot()[0][kid["id"]]
        historical = {
            "v": 3, "event": "verdict", "seq": current["seq"] + 1,
            "id": kid["id"], "ts": dispatches.pk.now_ts(),
            "reviewed_tip": self.a, "verdict_ref": "historical review",
            "polarity": "approve", "gate": "cafebabe12345678",
            "gate_caps": [dispatches.GATE_CAP_RECEIPT]}
        self.assertTrue(eventledger.append(dispatches.ledger_path(), historical))
        before = eventledger.events(dispatches.ledger_path())
        frontier = dispatches.snapshot()[0][kid["id"]]
        tier, why = dispatches.approval_tier_for_verdict(frontier)
        self.assertEqual(tier, "unknown")
        self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
        refusal, _tier = landreq._approval_refusal(frontier)
        self.assertIn("PRE-TIER", refusal)
        options = dict(evidence="objects pruned; frontier content already on trunk",
                       repo=self.repo, trunk=self.main)
        refused, why = landreq.close(row["id"], "chain-proof", **options)
        self.assertIsNone(refused)
        self.assertIn("attest", why)
        self.assertEqual(eventledger.events(dispatches.ledger_path()), before)

        closed, why = landreq.close(
            row["id"], "chain-proof", attester="seat-a",
            attest="read the historical frontier verdict and its recorded gate",
            **options)
        self.assertIsNone(why, why)
        self.assertEqual(closed["close_reason"], "chain-proof")
        self.assertEqual(closed["close_proof_mode"], "chain-proof")
        after = eventledger.events(dispatches.ledger_path())
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]["event"], "close")
        self.assertEqual(after[-1]["chain_tier_state"], "unknown")
        self.assertEqual(after[-1]["chain_attestation"]["attester"], "seat-a")
        rows, _verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(rows[kid["id"]], frontier)
        self.assertEqual(rows[row["id"]]["close_reason"], "chain-proof")
        self.assertEqual(rows[row["id"]]["close_proof_mode"], "chain-proof")
        tier, why = dispatches.approval_tier_for_verdict(rows[kid["id"]])
        self.assertEqual(tier.kind, dispatches.TIER_PRE_TIER)
        self.assertIn("PRE-TIER", landreq._approval_refusal(rows[kid["id"]])[0])
        projected, why = landreq.get(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(projected["close_reason"], "chain-proof")
        self.assertEqual(projected["state"], "SUPERSEDED")
        self.assertNotEqual(projected["close_proof_mode"], "carried")
        self.assertFalse(projected["landed"])


class ChainProofActuallyAPPENDSAndReplaysTheSameProof(ChainProofPrunedTipCloseBase):
    """The writer seam, which five dry-run arms could not see.

    A REVIEWER MEASURED THE GAP: `lr close --reason chain-proof` could never
    have bound however good its proof was, because the reason was missing from
    dispatches.CLOSE_REASONS — the LADDER decides whether a close is PROVABLE
    and that tuple decides whether the write is ADMISSIBLE AT ALL. Every arm
    above stopped at the ladder, so the door was bolted and the suite was
    green. These drive the real append and the real replay.

    The fixture records the actual frontier through mark_verdict, including
    real author-bound tier capture. Only gate binding is supplied at a seam;
    the real-mint subclass covers receipt provenance separately.
    """

    def test_a_NON_DRY_RUN_close_APPENDS_and_replay_reconstructs_it(self):
        row, kid = self._pruned_chain()
        before = len(list(eventledger.events(dispatches.ledger_path())))
        out, why = landreq.close(row["id"], "chain-proof",
                                 evidence="objects pruned by a history "
                                          "rewrite; frontier landed",
                                 repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        # THE APPEND IS COUNTED IN THE FILE, not inferred from a return value.
        # A door that returned a plan and wrote nothing would satisfy every
        # assertion that reads its own output.
        after = len(list(eventledger.events(dispatches.ledger_path())))
        self.assertEqual(after, before + 1, "the close appended no event")
        closed = landreq.get(row["id"])[0]
        self.assertEqual(closed["close_reason"], "chain-proof")
        self.assertEqual(closed["close_proof_mode"], "chain-proof")
        self.assertNotEqual(closed["close_proof_mode"], "carried")
        # REPLAY RECONSTRUCTS THE SAME CLOSE from the recorded events alone —
        # which is the property that makes the proof durable rather than a
        # verdict that happened to be true at write time.
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        self.assertEqual(rows[row["id"]].get("close_reason"), "chain-proof")
        self.assertEqual(rows[row["id"]].get("close_proof_mode"), "chain-proof")

    def test_a_SECOND_close_appends_NOTHING(self):
        """The idempotence half: a door that appends twice launders one proof
        into two, and the ledger's own count is the only witness."""
        row, _kid = self._pruned_chain()
        landreq.close(row["id"], "chain-proof", evidence="first",
                      repo=self.repo, trunk=self.main)
        mid = len(list(eventledger.events(dispatches.ledger_path())))
        landreq.close(row["id"], "chain-proof", evidence="second",
                      repo=self.repo, trunk=self.main)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), mid, "a second close appended")

    def test_a_FRONTIER_THAT_STOPS_LANDING_between_dry_and_write_APPENDS_NOTHING(self):
        """THE SNAPSHOT-CHANGED-AFTER-PREFLIGHT CASE. A dry run that proved a
        close is not a licence to write it later: the door must re-derive from
        the locked snapshot at append, or a proof measured against one world
        commits against another."""
        row, _kid = self._pruned_chain()
        plan, why = landreq.close(row["id"], "chain-proof", evidence="preflight",
                                  repo=self.repo, trunk=self.main, dry_run=True)
        self.assertIsNone(why, why)
        self.assertIsNotNone(plan)          # the preflight really did prove it
        # THE WORLD MOVES, AND THE MUTATION IS UNAMBIGUOUS. My first cut
        # branched the new trunk at `side` and the close SUCCEEDED — because
        # reasoning about which fixture commit reaches which is exactly the
        # kind of premise that quietly makes a negative arm vacuous. An ORPHAN
        # branch shares no history with anything, so the frontier's content
        # cannot be there by ancestry or by patch identity, and no topology
        # argument is needed to know it.
        self.git("checkout", "-q", "--orphan", "moved-trunk")
        self.git("rm", "-rq", "--cached", ".")
        self.commit("unrelated", path="unrelated")
        before = len(list(eventledger.events(dispatches.ledger_path())))
        out, why2 = landreq.close(row["id"], "chain-proof", evidence="write",
                                  repo=self.repo, trunk="moved-trunk")
        self.assertIsNone(out, "closed against a trunk the frontier never reached")
        self.assertIsNotNone(why2)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before, "a refused close still appended")


class ChainProofPathIsREWALKEDNeverREAD(ChainProofPrunedTipCloseBase):
    """THE WRITER/REPLAY AUTHORITY BOUNDARY (row 9f09ff4b858e).

    THE LADDER IS NOT IN THE REPLAY PATH AT ALL, and that is the whole defect.
    `_close_ladder_chain_proof` derives `chain_path` from a snapshot it read
    OUTSIDE the lock, hands it to the writer as a kwarg, and from there nothing
    ever looked at it again: `_close_event_error` had no chain-proof arm, so
    the reason fell through to the chain's default — ACCEPT. `carried`'s own
    comment predicted this by name one branch above the hole.

    WHAT THAT COSTS, and both halves are reachable without any tampering with
    the file's integrity: a next-seq close event naming supersedes edges this
    ledger does not have BINDS, and a close whose chain has since forked or
    lost its authorizing approve REPLAYS TERMINAL FOREVER. A row closed on a
    path nobody can re-walk is exactly the thing this door exists to be an
    alternative to.

    SO EVERY ARM HERE PAIRS ITS REFUSAL WITH THE HONEST EVENT, unconditionally
    and on the same observable. A validator that refused every hand-built event
    would satisfy an `assertIsNone(close_reason)` perfectly, which is the
    failure mode a must-miss exists to rule out rather than to share.
    """

    def _hand_close(self, row, kid, chain_path, **over):
        """Capture legitimate authority before assembling a close event."""
        rows, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        bundle, err = landreq._chain_write_authority(
            rows[kid["id"]], rows, verdicts)
        self.assertIsNone(err, err)
        bundle.update(over)
        return self._close_event(row, kid, chain_path, **bundle)

    def _close_event(self, row, kid, chain_path, **over):
        """Low-level event assembly, deliberately independent of authorization.

        Honest callers supply the writer's capture through _hand_close;
        hostile callers can supply a false claim to exercise replay itself.
        """
        rows, _v, _u = dispatches.snapshot_with_verdicts()
        state, frontier = rows[row["id"]], rows[kid["id"]]
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _target, err = landreq._close_trunk(
            lr, gitdir, self.main)
        self.assertIsNone(err, err)
        event = {"v": 3, "event": "close", "id": row["id"],
                 "seq": state["seq"] + 1, "ts": "2026-08-27T10:00:00Z",
                 "close_reason": "chain-proof", "close_proof_version": 1,
                 "reviewed_tip": state["reviewed_tip"],
                 "close_evidence": "objects pruned by a history rewrite",
                 "closing_repo_id": gitdir,
                 "closing_trunk_ref": trunk_ref,
                 "closing_trunk_sha": pinned,
                 "carried_base": (frontier.get("reviewed_tip")
                                  or frontier.get("ref")),
                 "carried_tip": kid["id"],
                 "chain_path": chain_path,
                 # THE CENSUS TRAVELS WITH THE PATH. A hand-built event that
                 # omits it is not a forgery this arm is testing — it is a
                 # fixture that stopped matching what the writer emits, and
                 # replay says so in those words.
                 "chain_fork_census": {row["id"]: list(chain_path)},
                 # THE WRITER STAMPS THIS; a hand-built event has to supply it,
                 # because appending straight to the ledger is exactly the door
                 # these arms are testing.
                 "chain_census_cutoff": len(list(eventledger.events(
                     dispatches.ledger_path()))),
                 "close_proof_mode": "chain-proof"}
        event.update(over)
        return event

    def _append(self, event):
        path = dispatches.ledger_path()
        with eventledger.locked(path) as held:
            self.assertTrue(held, "could not lock the ledger to append")
            self.assertTrue(eventledger.append_unlocked(path, event),
                            "the ledger refused the append")

    def _close_reason(self, rid):
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        return rows[rid].get("close_reason")

    def test_a_FORGED_chain_path_is_INERT_and_the_HONEST_one_BINDS(self):
        """THE MUST-MISS FOR BLOCKER ONE. The forged event differs from the
        honest one in ONE clause — the recorded supersedes path — and every
        other field is the door's own derivation. If the path is never
        re-walked, both bind and nothing can tell them apart; that was the
        shipped state."""
        row, kid = self._pruned_chain()
        self._append(self._hand_close(row, kid, chain_path=["f" * 32]))
        self.assertIsNone(
            self._close_reason(row["id"]),
            "a close naming supersedes edges this ledger does not have closed "
            "the row TERMINAL — the path was recorded and never re-walked")
        # CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: one clause back to
        # the truth and the identical hand-built event binds.
        self._append(self._hand_close(row, kid, chain_path=[kid["id"]]))
        self.assertEqual(self._close_reason(row["id"]), "chain-proof")

    def test_a_TRUNCATED_chain_path_is_INERT_even_though_the_ENDPOINTS_agree(self):
        """ENDPOINT EQUALITY IS NOT PATH EQUALITY, and this is why the edges
        are compared rather than the pair. An empty path claims the frontier
        carried the row with no supersession between them — the endpoints
        match, the transfer it describes never happened."""
        row, kid = self._pruned_chain()
        self._append(self._hand_close(row, kid, chain_path=[]))
        self.assertIsNone(self._close_reason(row["id"]),
                          "a close recording NO edges bound on its endpoints")
        self._append(self._hand_close(row, kid, chain_path=[kid["id"]]))
        self.assertEqual(self._close_reason(row["id"]), "chain-proof")

    def test_a_close_over_an_UNAUTHORIZED_frontier_is_INERT_on_replay(self):
        """THE STALE HALF. The ladder refuses a chain whose frontier never
        carried a gate-backed APPROVE — but the ladder is not what replay runs.
        Only this arm stands between a hand-appended event and a row closed
        terminal on authority that was never there."""
        row, kid = self._pruned_chain(with_approve=False)
        rows, verdicts, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable, unavailable)
        frontier = rows[kid["id"]]
        self.assertEqual(frontier["status"].lower(), "open")
        tier, _why = dispatches.approval_tier_for_verdict(frontier)
        self.assertEqual(dispatches.tier_unknown_kind(tier),
                         dispatches.TIER_PRE_TIER)
        bundle, why = landreq._chain_write_authority(frontier, rows, verdicts)
        self.assertIsNone(bundle)
        self.assertIn("PRE-TIER", why)
        # HOSTILE APPEND, NOT A WRITER CAPTURE: claim a measured tier the OPEN
        # frontier never earned. A structurally matching content anchor is not
        # authorization. No verdict is changed and no attestation is supplied;
        # replay must still enforce the independent frontier lifecycle check.
        requirement = landreq.gate_requirement(
            frontier, index=dispatches.verdict_index(verdicts, kid["id"]),
            epoch=dispatches.gate_epoch(rows, verdicts))
        self._append(self._close_event(
            row, kid, chain_path=[kid["id"]], chain_tier_state="none",
            chain_gate_requirement=requirement, chain_attestation=None,
            chain_authority_anchor=landreq._chain_authority_anchor(
                frontier, "none", requirement, None)))
        self.assertIsNone(
            self._close_reason(row["id"]),
            "the row closed on an OPEN frontier — replay accepted a transfer "
            "the ladder would have refused")
        # CONTROL: the SAME row and replay accessor, now with a legitimate
        # writer capture after the frontier becomes a gate-backed APPROVE.
        self._authorize(kid)
        self._append(self._hand_close(row, kid, chain_path=[kid["id"]]))
        self.assertEqual(self._close_reason(row["id"]), "chain-proof")

    def test_the_WRITER_refuses_a_forged_path_under_the_LOCK(self):  # noqa: VACUOUS_ASSERTION — the control is the second `write([kid["id"]])` at the end of this method: the SAME door, the SAME row, one clause true, asserting a bound close and a chain-proof terminal. The rung cannot credit it because every call in a binding RHS mints a fresh producer identity by design, so a paired refusal/control on one door is structurally invisible to it — not absent
        """The other half of the pair. The writer takes the path as a KWARG
        from a caller that read an unlocked snapshot, so the locked re-walk is
        the only thing that can catch a path that was true then and is not
        true now — Q3's invariant, recomputed at append and not assumed."""
        row, kid = self._pruned_chain()
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _t, err = landreq._close_trunk(lr, gitdir, self.main)
        self.assertIsNone(err, err)
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        ftip = (rows[kid["id"]].get("reviewed_tip")
                or rows[kid["id"]].get("ref"))

        bundle, berr = landreq._chain_write_authority(
            rows[kid["id"]], rows, verdicts)
        self.assertIsNone(berr, berr)

        def write(chain_path):
            return dispatches._record_close_proven(
                row["id"], "chain-proof", lr["reviewed_tip"],
                evidence="objects pruned by a history rewrite",
                closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
                closing_trunk_sha=pinned, carried_base=ftip,
                carried_tip=kid["id"], proof_mode="chain-proof",
                chain_path=chain_path,
                chain_fork_census={row["id"]: [kid["id"]]}, **bundle)

        before = len(list(eventledger.events(dispatches.ledger_path())))
        out, why = write(["f" * 32])
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before,
            "a refused close still appended")
        # CONTROL, SAME CALL, ONE CLAUSE TRUE: the re-derived path binds.
        out, why = write([kid["id"]])
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        self.assertEqual(self._close_reason(row["id"]), "chain-proof")

    def test_the_RETRY_reconciles_and_a_DIFFERENT_close_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and first: `first` must be a real non-None close before either retry is attempted, and the identical retry must return a row with close_reason chain-proof. Only the THIRD call asserts absence, and it is a different producer than the two positives above it
        """THE IDEMPOTENCE ARM, WHICH USED TO MEASURE ONLY THE COUNT.

        Its predecessor closed twice with DIFFERENT evidence and asserted only
        that nothing appended — which is what a REFUSAL looks like too, so it
        was green on a door with no retry rung at all. A retry and a refusal
        are opposite outcomes and the count cannot separate them: assert the
        ANSWER."""
        row, _kid = self._pruned_chain()
        first, why = landreq.close(row["id"], "chain-proof", evidence="first",
                                   repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(first)
        mid = len(list(eventledger.events(dispatches.ledger_path())))
        # SAME EVIDENCE = the same close. It RECONCILES: row back, no error.
        again, why = landreq.close(row["id"], "chain-proof", evidence="first",
                                   repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(again, "an identical retry did not reconcile")
        self.assertEqual(again.get("close_reason"), "chain-proof")
        # DIFFERENT EVIDENCE = a different closure. A row is retired ONCE.
        other, why = landreq.close(row["id"], "chain-proof", evidence="second",
                                   repo=self.repo, trunk=self.main)
        self.assertIsNone(other)
        self.assertIsNotNone(why, "a DIFFERENT close was accepted in silence")
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), mid,
            "a second close appended")

    def test_the_WRITER_reconciles_an_identical_retry(self):  # noqa: VACUOUS_ASSERTION — two unconditional positives run before the absence: the first write must bind and the identical retry must reconcile to a non-None row. Each `write(...)` is a fresh producer to the rung, so the pairing cannot be credited automatically
        """The writer's OWN retry rung, reached without the ladder's
        pre-emptive one. `_close_idempotent` had no chain-proof case, so an
        identical re-run of a proven close read as a DIFFERENT closure and took
        the retired-once refusal — a caller whose first write succeeded but
        whose answer was lost could never reconcile."""
        row, kid = self._pruned_chain()
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _t, err = landreq._close_trunk(lr, gitdir, self.main)
        self.assertIsNone(err, err)
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        ftip = (rows[kid["id"]].get("reviewed_tip")
                or rows[kid["id"]].get("ref"))

        bundle, berr = landreq._chain_write_authority(
            rows[kid["id"]], rows, verdicts)
        self.assertIsNone(berr, berr)

        def write(evidence):
            return dispatches._record_close_proven(
                row["id"], "chain-proof", lr["reviewed_tip"],
                evidence=evidence, closing_repo_id=gitdir,
                closing_trunk_ref=trunk_ref, closing_trunk_sha=pinned,
                carried_base=ftip, carried_tip=kid["id"],
                proof_mode="chain-proof", chain_path=[kid["id"]],
                chain_fork_census={row["id"]: [kid["id"]]}, **bundle)

        out, why = write("pruned by a rewrite")
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        mid = len(list(eventledger.events(dispatches.ledger_path())))
        out, why = write("pruned by a rewrite")
        self.assertIsNone(why, why)
        self.assertIsNotNone(out, "an identical retry did not reconcile")
        # AND THE MUST-MISS FOR THE RETRY RUNG: a DIFFERENT annotation is not
        # a retry, and idempotence that admits it is not idempotence.
        out, why = write("a different story")
        self.assertIsNone(out)
        self.assertIsNotNone(why)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), mid,
            "a retry or a refusal appended a second terminal")


class ChainProofValidatesBeforeItReconciles(ChainProofPrunedTipCloseBase):
    """ROUND 3: a retry was RECONCILED before it was VALIDATED.

    `_record_close_proven` consulted `_close_idempotent` — an IDENTITY test,
    not a validator — as the FIRST thing a retry met. So a payload the open-row
    door refuses came back SUCCESS purely because the row was already retired:
    the same bytes got two different answers depending on lifecycle position.
    The mode was invisible to the identity tuple because ROUND 2 had removed it
    there, to satisfy the rule that forbids a mode-inspecting function from
    deciding a proof question without reaching `_content_proof_pair_error`.

    The cure threads between them: `_close_mode_error` validates the mode and
    REACHES that rule itself, and the writer calls it BEFORE the retired fork.
    """

    def _close(self, row, **over):
        """One real chain-proof close through the production ladder."""
        kw = dict(evidence="objects pruned by a history rewrite",
                  repo=self.repo, trunk=self.main)
        kw.update(over)
        return landreq.close(row["id"], "chain-proof", **kw)

    def _writer(self, row, kid, **over):
        """The writer door directly, so the RETRY path is what is under test
        rather than the ladder's pre-checks."""
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _t, err = landreq._close_trunk(lr, gitdir, self.main)
        self.assertIsNone(err, err)
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        frontier = rows[kid["id"]]
        ftip = frontier.get("reviewed_tip") or frontier.get("ref")
        # NO ATTESTATION HERE: this fixture's tier resolves MEASURED (the
        # isolated home declares no policy), and the door refuses an
        # attestation nothing asked for. Attesting by reflex is exactly what
        # would make the record meaningless on the row where it matters.
        bundle, why = landreq._chain_write_authority(
            frontier, rows, verdicts)
        self.assertIsNone(why, why)
        kw = dict(evidence="objects pruned by a history rewrite",
                  closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
                  closing_trunk_sha=pinned, carried_base=ftip,
                  carried_tip=kid["id"], proof_mode="chain-proof",
                  chain_path=[kid["id"]],
                  chain_fork_census={row["id"]: [kid["id"]]}, **bundle)
        kw.update(over)
        return dispatches._record_close_proven(
            row["id"], "chain-proof", lr["reviewed_tip"], **kw)

    def test_a_RETIRED_retry_with_a_BAD_MODE_is_REFUSED_not_reconciled(self):  # noqa: VACUOUS_ASSERTION — the honest close is asserted non-None before any bad mode is tried, and the honest retry is re-asserted after — control on both sides of the refusal
        """THE MUST-MISS A REVIEW NAMED. Close once honestly, then retry the
        SAME payload with mode None and with mode 'carried'. Before the cure
        both returned SUCCESS, because the identity tuple never looked at the
        mode and it answered first."""
        row, kid = self._pruned_chain()
        out, why = self._writer(row, kid)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out, "the honest close did not bind")
        before = len(list(eventledger.events(dispatches.ledger_path())))
        for mode in (None, "carried"):
            out, why = self._writer(row, kid, proof_mode=mode)
            self.assertIsNone(out, "mode %r reconciled into a success" % mode)
            self.assertIsNotNone(why)
            self.assertIn("proof mode", why)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before,
            "a refused retry appended")
        # CONTROL, UNCONDITIONAL, SAME DOOR: the honest mode still reconciles,
        # so this is validation discriminating and not idempotence broken.
        out, why = self._writer(row, kid)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out, "the honest retry stopped reconciling")

    def test_the_SAME_payload_answers_the_SAME_way_open_or_retired(self):
        """The invariant underneath the finding, stated directly: lifecycle
        position must not change the verdict on a payload. A bad mode is
        refused on a fresh row and on a retired one, by the same rung."""
        fresh, kid = self._pruned_chain()
        out, open_why = self._writer(fresh, kid, proof_mode="carried")
        self.assertIsNone(out)
        out, why = self._writer(fresh, kid)          # now retire it honestly
        self.assertIsNone(why, why)
        out, retired_why = self._writer(fresh, kid, proof_mode="carried")
        self.assertIsNone(out)
        self.assertEqual(open_why, retired_why,
                         "the open row and the retired row gave different "
                         "answers to identical bytes")

    def test_a_retry_against_a_CHANGED_TRUNK_is_REFUSED(self):  # noqa: VACUOUS_ASSERTION — the honest close binds unconditionally first and the original trunk is re-asserted to reconcile at the end
        """The second finding. The ladder short-circuited a retry on
        closing_repo_id + evidence and returned BEFORE `_close_trunk` ran, so
        the same evidence against another trunk reconciled clean. There is now
        one retry identity and it is the writer's."""
        row, _kid = self._pruned_chain()
        out, why = self._close(row)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        before = len(list(eventledger.events(dispatches.ledger_path())))
        self.git("branch", "-q", "other-trunk", self.main)
        out, why = self._close(row, trunk="other-trunk")
        self.assertIsNone(out, "a retry against another trunk reconciled")
        self.assertIsNotNone(why)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before)
        # CONTROL: the ORIGINAL trunk still reconciles to the standing row.
        out, why = self._close(row)
        self.assertIsNone(why, why)
        self.assertEqual(out.get("close_reason"), "chain-proof")

    def test_a_nonexistent_trunk_REFUSES_instead_of_reconciling(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the honest `self._close(row)` on the first two lines, which must bind before any trunk is varied
        """The sharper half of the same hole: the shortcut returned success
        before anything asked whether the named trunk existed at all."""
        row, _kid = self._pruned_chain()
        out, why = self._close(row)
        self.assertIsNone(why, why)
        out, why = self._close(row, trunk="no-such-trunk-anywhere")
        self.assertIsNone(out, "a nonexistent trunk reconciled")
        self.assertIsNotNone(why)

    def test_the_PROOF_TUPLE_survives_the_READ(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the honest close above, asserted non-None, plus the positive equality on chain_path/carried_tip in the body
        """The third finding. The edges and endpoints survived the
        WRITE and were dropped by the PROJECTION, so `helm lr show` could not
        show which supersedes links carried the close — and the ladder's retry
        check could not see them either, which is why it compared the two
        fields it could see and ignored the trunk."""
        row, kid = self._pruned_chain()
        out, why = self._close(row)
        self.assertIsNone(why, why)
        lr, _err = landreq.get(row["id"])
        self.assertEqual(lr.get("chain_path"), [kid["id"]],
                         "the projection dropped the supersedes edges")
        self.assertEqual(lr.get("carried_tip"), kid["id"])
        self.assertTrue(lr.get("carried_base"))
        self.assertIn(lr.get("chain_tier_state"), ("none", "ok", "unknown"))


class UnmeasurableTierNeedsAnExplicitAttestation(unittest.TestCase):
    """RULING ONE, AND IT REVERSES MY OWN ROUND-2 CALL.

    I had admitted an UNMEASURABLE approval tier silently, reasoning that
    refusing on absence downgrades every aged-out legitimate row. That premise
    is true and it is NOT a licence: REFUSING on absence and AUTHORIZING on
    absence are different acts, and a security boundary may only do the first
    by default. Capability absence is not proof, and the 183-row
    cost I measured is an argument for building a DOOR, never for leaving the
    wall open.

    So the door is EXPLICIT, RECORDED, and belongs to the DISCHARGING SEAT —
    the integrator or the row's issuer, never the human owner, because an
    outcome the owner already locked must not be re-gated on him.
    """

    def setUp(self):
        self.rows = {
            "T": {"id": "T", "status": "verdict", "polarity": "fix",
                  "recipient": "codex", "close_reason": "resolved",
                  "reviewed_tip": "a" * 40},
            "F": {"id": "F", "status": "verdict", "polarity": "approve",
                  "recipient": "codex", "supersedes": "T",
                  "gate": "cafebabe12345678",
                  "gate_caps": [dispatches.GATE_CAP_RECEIPT],
                  "reviewed_tip": "c" * 40},
        }
        self.verdicts = {"T": (10, "a" * 32), "F": (20, "c" * 32)}

    def _authority(self, tier, **kw):
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               lambda row: tier):
            return landreq._chain_write_authority(
                self.rows["F"], self.rows, self.verdicts, **kw)

    def test_an_UNKNOWN_tier_WITHOUT_an_attestation_REFUSES(self):
        """THE REVERSAL, ASSERTED. This is the exact input my round-2 cure
        admitted."""
        bundle, why = self._authority(("unknown", "no runtime family evidence"))
        self.assertIsNone(bundle, "an unmeasurable tier was minted into "
                                  "authority with nobody accountable")
        self.assertIn("attest", why)

    def test_an_UNKNOWN_tier_WITH_an_attestation_ADMITS_and_records_WHO(self):
        """CONTROL AND CURE IN ONE: the aged row is not condemned either — it
        is admitted by a recorded act naming the seat and what it read."""
        bundle, why = self._authority(
            ("unknown", "no runtime family evidence"),
            attester="seat-a",
            attest="read the frontier verdict and its gate receipt")
        self.assertIsNone(why, why)
        self.assertEqual(bundle["chain_tier_state"], "unknown")
        self.assertEqual(bundle["chain_attestation"]["attester"],
                         "seat-a")
        self.assertIn("gate receipt", bundle["chain_attestation"]["evidence"])

    def test_UNKNOWN_has_THREE_faces_and_only_LEGACY_ABSENCE_is_attestable(self):  # noqa: VACUOUS_ASSERTION — FACE 1 is the unconditional positive control and runs first — it must ADMIT before either refusal is asserted; the rung cannot credit it because each _authority call is a fresh producer
        """A late precision, and it is the sharpest thing in the
        round. "The tier could not be evaluated" collapses three different
        worlds: a verdict written BEFORE the author stamp existed, a row that
        CLAIMS author evidence which does not read, and a policy file nobody
        can parse. Only the first is a gap a reader can close by looking at the
        row. Attesting over the other two would let one seat's reading paper
        over a contradiction or a corrupt prior — which is the laundering this
        door replaced, wearing a signature.
        """
        tier = ("unknown", "no runtime family evidence")
        attesting = dict(attester="seat-a", attest="I read the row")

        # FACE 1 — LEGACY ABSENCE: attestable, and the control for the rest.
        bundle, why = self._authority(tier, **attesting)
        self.assertIsNone(why, why)
        self.assertIsNotNone(bundle["chain_attestation"])

        # FACE 2 — MALFORMED ROW PROOF: the row makes a CLAIM that fails.
        claimed = dict(self.rows)
        claimed["F"] = dict(self.rows["F"],
                            **{dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS[0]:
                               "a-session-with-no-matching-proof"})
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               lambda row: tier):
            bundle, why = landreq._chain_write_authority(
                claimed["F"], claimed, self.verdicts, **attesting)
        self.assertIsNone(bundle, "a malformed author claim was attested away")
        self.assertIn("CONTRADICTION", why)

        # FACE 3 — UNREADABLE POLICY: the world, not the row.
        with mock.patch.object(landreq, "_approval_policy_readable",
                               lambda repo_id=None: False):
            bundle, why = self._authority(tier, **attesting)
        self.assertIsNone(bundle, "an unreadable approval-tier policy was "
                                  "attested away")
        self.assertIn("policy", why)

    def test_TRUNKS_KIND_VOCABULARY_decides_attestability(self):
        """THE COMPOSE SEAM, PINNED. Trunk's tier-unknown KINDS (c014b1e2) and
        my structural three-way split reached this door from different chains
        and express overlapping distinctions. The union: trunk's vocabulary
        decides where it CLASSIFIES, my probes derive the answer where nobody
        measured a kind, and nothing either side could say is lost.

        TWO OF THESE I DID NOT HAVE, and they are why the kind has to win:
        TRANSIENT means a LIVE step failed and one re-read may answer, so
        signing for it certifies a question nobody has asked; UNNAMED means the
        reviewer is not one canonical seat, so there is no subject to attest
        about. My structural test saw neither — both would have fallen through
        to ATTESTABLE, which is the quiet kind of wrong.
        """
        attesting = dict(attester="seat-a", attest="read the frontier verdict")
        for kind in (dispatches.TIER_TRANSIENT, dispatches.TIER_UNNAMED,
                     dispatches.TIER_DAMAGED):
            bundle, why = self._authority(
                dispatches._tier_unknown(kind, "because"), **attesting)
            self.assertIsNone(bundle, "%s was attested away" % kind)
            self.assertIsNotNone(why)
        # DARK is the one attestable kind — the legacy absence my own split
        # named. UNCONDITIONAL POSITIVE CONTROL: without it every refusal above
        # is satisfied by a door that admits nothing.
        bundle, why = self._authority(
            dispatches._tier_unknown(dispatches.TIER_DARK, "nothing stored"),
            **attesting)
        self.assertIsNone(why, why)
        self.assertIsNotNone(bundle["chain_attestation"])

    def test_an_UNCLASSIFIED_unknown_still_gets_MY_structural_split(self):
        """`tier_unknown_kind` never returns None for an unknown — it returns
        UNCLASSIFIED. Gating my probes on `kind is None` made them DEAD CODE,
        and blanket-refusing UNCLASSIFIED would have deleted the three-way
        split outright. Both are silent losses; this arm is what makes them
        loud."""
        plain = ("unknown", "nobody measured the kind")
        attesting = dict(attester="seat-a", attest="read the frontier verdict")
        bundle, why = self._authority(plain, **attesting)
        self.assertIsNone(why, why)
        self.assertIsNotNone(bundle["chain_attestation"],
                             "the structural split is dead code")
        # MUST-MISS on the same observable: a row that CLAIMS author evidence
        # it cannot back is a contradiction, unclassified or not.
        claimed = dict(self.rows)
        claimed["F"] = dict(self.rows["F"],
                            **{dispatches.VERDICT_AUTHOR_EVIDENCE_FIELDS[0]:
                               "a-session-with-no-matching-proof"})
        with mock.patch.object(dispatches, "approval_tier_for_verdict",
                               lambda row: plain):
            bundle, why = landreq._chain_write_authority(
                claimed["F"], claimed, self.verdicts, **attesting)
        self.assertIsNone(bundle)
        self.assertIn("CONTRADICTION", why)

    def test_an_OUTSIDE_tier_is_refused_EVEN_WITH_an_attestation(self):
        """THE MUST-MISS FOR THE DOOR. An attestation admits an UNMEASURABLE
        tier; it may never overrule a MEASURED violation, or the door becomes
        a way to launder exactly what the tier exists to refuse."""
        bundle, why = self._authority(
            ("outside", "@codex is outside the tier"),
            attester="seat-a", attest="I say it is fine")
        self.assertIsNone(bundle)
        self.assertIn("MEASURED", why)

    def test_a_MEASURED_tier_REFUSES_a_pointless_attestation(self):
        """An attestation on a tier that was measurable records a judgement
        nothing asked for — and a door that accepts one everywhere teaches
        operators to attach it by reflex, which is how it stops meaning
        anything on the row where it matters."""
        bundle, why = self._authority(("ok", None), attester="seat-a",
                                      attest="belt and braces")
        self.assertIsNone(bundle)
        self.assertIn("MEASURED", why)

    def test_a_measured_tier_admits_with_NO_attestation(self):
        """CONTROL: the ordinary path is untouched by any of this.

        UNROLLED DELIBERATELY. Both readings used to be asserted inside a
        `for`, and an assertion whose execution is optional cannot be the
        control for anything — if the tuple were ever emptied the loop would
        pass having checked nothing.
        """
        ok_bundle, why = self._authority(("ok", None))
        self.assertIsNone(why, why)
        self.assertIsNone(ok_bundle["chain_attestation"])
        self.assertTrue(ok_bundle["chain_authority_anchor"])
        none_bundle, why = self._authority(("none", "no policy"))
        self.assertIsNone(why, why)
        self.assertIsNone(none_bundle["chain_attestation"])
        self.assertTrue(none_bundle["chain_authority_anchor"])


class VerdictProducerAndReplayAreOneReader(LandReqBase):
    """The owner-schema cure, and it is a SEAM test.

    `mark_verdict` returned a row it rebuilt BY HAND while replay derived the
    same row through `_apply`. Two independent readers of one event, free to
    agree today and diverge the moment a field is added — which is the mirror
    image of the stale-rename that took every verdict down last round.

    THE ASSERTION IS AN EQUALITY OVER ONE ROW THROUGH BOTH PATHS, deliberately,
    not two separate expectations that happen to match. Two assertions can both
    be updated to a new wrong answer; an equality cannot.
    """

    def test_the_IMMEDIATE_result_IS_the_REPLAY_projection(self):
        """THE SEAM, AS AN EQUALITY OVER ONE ROW THROUGH BOTH PATHS.

        Not two expectations that happen to match — two expectations can both
        be updated to the same wrong answer. This takes the row `mark_verdict`
        RETURNS and the row a COLD REPLAY of the ledger produces for that id,
        and asserts they are the same mapping. A field the producer invents,
        drops, or shapes differently (tuple where replay says list) fails here
        and nowhere else, which is the whole point: it is the only assertion
        that can see both readers at once.
        """
        row = self.dispatch(ref=self.side, lane="lane/parity", kind="review")
        out, why = self.mark_verdict(
            row["id"], self.side, "findings for the parity seam",
            polarity="fix", basis="measured")
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        replayed, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        cold = replayed[row["id"]]
        # `announce` is the CLI's own courtesy line, computed after the
        # projection and never recorded — the one key replay cannot have.
        immediate = {k: v for k, v in out.items() if k != "announce"}
        self.assertEqual(immediate, dict(cold),
                         "the row mark_verdict returned is not the row a cold "
                         "replay produces — two readers of one event")
        # MUST-HIT on the same observable: an equality between two EMPTY or
        # two default rows would pass while proving nothing.
        self.assertEqual(cold.get("polarity"), "fix")
        self.assertEqual(cold.get("basis"), "measured")
        self.assertEqual(cold.get("status"), "verdict")


class ALaterForkNeitherDissolvesNorEvadesTheTerminal(ChainProofPrunedTipCloseBase):
    """THE RULING THAT RESOLVED A CONFLICT BETWEEN TWO OTHER RULINGS.

    "Later successor forks are invisible" and "a later change must not
    resurrect an append-only terminal" pull opposite ways: re-deriving
    uniqueness on replay would let anyone dissolve a closed row by opening a
    parallel round, and ignoring forks entirely would let one be minted later
    to claim a different authority path. A review's resolution, melded
    before this was written: UNIQUENESS IS A WRITE-TIME FINDING, RECORDED-EDGE
    INTEGRITY IS A REPLAY ONE, and a sibling found INSIDE the captured cutoff
    is tampering rather than news.
    """

    def _closed(self):
        row, kid = self._pruned_chain()
        out, why = landreq.close(row["id"], "chain-proof",
                                 evidence="objects pruned by a rewrite",
                                 repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        return row, kid

    def test_a_fork_appended_AFTER_the_close_does_NOT_dissolve_it(self):  # noqa: VACUOUS_ASSERTION — the observable is a PRESENT close_reason, not an absence — _closed() asserts the close bound before the fork is minted
        """THE MUST-MISS FOR THE WHOLE RULING. Close honestly, then mint a
        SECOND successor of the same parent — the shape a fresh cure round
        creates every day. The terminal must survive a cold replay."""
        row, _kid = self._closed()
        self.dispatch(ref=self.side, lane="lane/pruned-fork", kind="review",
                      supersedes=row["id"])
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        self.assertEqual(rows[row["id"]].get("close_reason"), "chain-proof",
                         "a later parallel round dissolved a terminal that "
                         "was true when it was written")

    def test_a_census_naming_TWO_successors_is_REFUSED_as_tampering(self):
        """The other side: a capture the write-time walk could NOT have
        produced. That walk refuses any node with more than one successor, so a
        census recording two is corruption and must surface a refusal rather
        than a silent pass or a silent reopen."""
        row, kid = self._pruned_chain()
        rows, _v, _u = dispatches.snapshot_with_verdicts()
        bad = landreq.chain_path_intact(
            row["id"], rows, [kid["id"]],
            {row["id"]: [kid["id"], "f" * 32]}, cutoff=1, position=2)
        self.assertIsNotNone(bad)
        self.assertIn("more than one", bad)
        # CONTROL, UNCONDITIONAL, SAME DOOR: the honest census passes.
        self.assertIsNone(landreq.chain_path_intact(
            row["id"], rows, [kid["id"]], {row["id"]: [kid["id"]]},
            cutoff=1, position=2), "the intact check refuses everything")

    def test_a_sibling_appended_BEFORE_the_lock_is_REFUSED_not_dated_LATER(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the honest write at the end of this method, same door and same row, which must BIND after the hostile one is refused
        """THE ROUND-4 TOCTOU. The ladder walked an UNLOCKED
        snapshot and handed the census to the writer as a kwarg. A sibling
        appended in that window is in the ledger AT CLOSE TIME, but the census
        — taken before it existed — recorded one child, and replay read the
        sibling as LATER and let the terminal stand. A fork silently inheriting
        a closed row's authority path.

        This drives the post-window state directly: a STALE census claiming one
        child while the ledger already holds two.
        """
        row, kid = self._pruned_chain()
        # THE SIBLING LANDS BEFORE THE WRITE, which is the whole window.
        sib = self.dispatch(ref=self.side, lane="lane/pruned-window",
                            kind="review", supersedes=row["id"])
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _t, err = landreq._close_trunk(lr, gitdir, self.main)
        self.assertIsNone(err, err)
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        ftip = rows[kid["id"]].get("reviewed_tip") or rows[kid["id"]].get("ref")
        bundle, berr = landreq._chain_write_authority(
            rows[kid["id"]], rows, verdicts)
        self.assertIsNone(berr, berr)
        before = len(list(eventledger.events(dispatches.ledger_path())))
        out, why = dispatches._record_close_proven(
            row["id"], "chain-proof", lr["reviewed_tip"],
            evidence="objects pruned by a history rewrite",
            closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
            closing_trunk_sha=pinned, carried_base=ftip,
            carried_tip=kid["id"], proof_mode="chain-proof",
            chain_path=[kid["id"]],
            # THE STALE CAPTURE: taken before `sib` existed.
            chain_fork_census={row["id"]: [kid["id"]]}, **bundle)
        self.assertIsNone(out, "a pre-lock sibling was dated as a later fork")
        self.assertIsNotNone(why)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before,
            "a refused close still appended")
        # The refusal fires at the census-coherence rung: the writer RE-DERIVED
        # under the lock, found two children, and a census that could only have
        # been produced by a walk enforcing uniqueness cannot name two. That is
        # the sibling being seen — which is the whole finding.
        self.assertIn("successors", why)
        self.assertIsNotNone(dispatches.snapshot()[0][sib["id"]],
                             "the sibling never reached the ledger, so this "
                             "arm never drove the window it names")

    def test_the_RECORDED_census_is_the_LOCKS_and_not_the_CALLERS(self):
        """THE MUTATION PROOF for the same finding, stated as a property rather
        than a scenario: whatever census the caller hands in, the census that
        LANDS is the one re-derived under the lock, and it carries a cutoff.

        RESTORE THE PRE-LOCK CAPTURE — make the writer trust the kwarg — and
        this goes red on the first assertion, because the bogus node would
        survive into the record. A scenario arm can be satisfied by refusing
        everything; this one pins what a SUCCESSFUL close writes.
        """
        row, kid = self._pruned_chain()
        lr, _why = landreq.get(row["id"])
        gitdir, err = landreq._close_repo(lr, self.repo)
        self.assertIsNone(err, err)
        trunk_ref, pinned, _t, err = landreq._close_trunk(lr, gitdir, self.main)
        self.assertIsNone(err, err)
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        ftip = rows[kid["id"]].get("reviewed_tip") or rows[kid["id"]].get("ref")
        bundle, berr = landreq._chain_write_authority(
            rows[kid["id"]], rows, verdicts)
        self.assertIsNone(berr, berr)
        out, why = dispatches._record_close_proven(
            row["id"], "chain-proof", lr["reviewed_tip"],
            evidence="objects pruned by a history rewrite",
            closing_repo_id=gitdir, closing_trunk_ref=trunk_ref,
            closing_trunk_sha=pinned, carried_base=ftip,
            carried_tip=kid["id"], proof_mode="chain-proof",
            chain_path=[kid["id"]],
            # A CENSUS THE CALLER INVENTED, naming a node that is not on the
            # path at all. Nothing about it may reach the ledger.
            chain_fork_census={"a-node-the-caller-made-up": ["and-its-child"]},
            **bundle)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out, "the honest close did not bind")
        recorded = landreq.get(row["id"])[0]
        self.assertEqual(recorded.get("chain_fork_census"),
                         {row["id"]: [kid["id"]]},
                         "the caller's census reached the ledger — the capture "
                         "is still outside the lock")
        self.assertIsInstance(recorded.get("chain_census_cutoff"), int,
                              "the close records no cutoff, so a sibling can "
                              "only be dated by re-derivation")
        self.assertGreater(recorded["chain_census_cutoff"], 0)

    def test_a_MISSING_recorded_edge_is_REFUSED(self):
        """The edges ARE the proof: one that is no longer in the ledger means
        the recorded path cannot be walked, whatever else still agrees."""
        row, kid = self._pruned_chain()
        rows, _v, _u = dispatches.snapshot_with_verdicts()
        why = landreq.chain_path_intact(
            row["id"], rows, ["f" * 32], {row["id"]: ["f" * 32]},
            cutoff=1, position=2)
        self.assertIsNotNone(why)
        self.assertIn("not in this ledger", why)


class ReplayNeverReasksMutablePolicy(ChainProofPrunedTipCloseBase):
    """RULING TWO, ARMED: the policy-drift replay arm.

    A chain-proof close captures the approval tier it resolved AT WRITE TIME.
    If replay re-asked the live policy, an approval-tier edit made afterwards
    would silently reopen — or silently bless — an append-only terminal. The
    tier is MUTABLE; the ledger is not; and only one of them gets a vote after
    the fact.
    """

    def test_a_TIER_POLICY_CHANGE_after_the_close_leaves_the_terminal_alone(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the honest close asserted non-None before the policy is moved, on the same row this then re-reads
        """MEASURED BOTH SIDES OF THE DRIFT, on one row: close under a
        permissive reading, replay under a hostile one."""
        row, _kid = self._pruned_chain()
        out, why = landreq.close(row["id"], "chain-proof",
                                 evidence="objects pruned by a rewrite",
                                 repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        # THE WORLD MOVES: every tier reading now says OUTSIDE. Under a live
        # re-derivation this row would stop replaying closed.
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                lambda r: ("outside", "policy changed after the close")):
            rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        self.assertEqual(rows[row["id"]].get("close_reason"), "chain-proof",
                         "a later approval-tier edit dissolved a recorded "
                         "terminal — replay re-asked mutable policy")

    def test_the_captured_authority_must_BIND_its_own_fields(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the assertEqual(honest, recorded) immediately above the must-miss — the anchor must MATCH for its mismatch to mean anything
        """The structural half that replaces the live re-derivation: tamper
        with the captured tier and the anchor stops matching. Replay cannot
        re-ask the policy, so binding is the only honesty left to check."""
        row, kid = self._pruned_chain()
        rows, verdicts, _u = dispatches.snapshot_with_verdicts()
        frontier = rows[kid["id"]]
        bundle, why = landreq._chain_write_authority(frontier, rows, verdicts)
        self.assertIsNone(why, why)
        honest = landreq._chain_authority_anchor(
            frontier, bundle["chain_tier_state"],
            bundle["chain_gate_requirement"], bundle["chain_attestation"])
        self.assertEqual(honest, bundle["chain_authority_anchor"])
        # MUST-MISS: one clause of the captured decision changed, same anchor.
        forged = landreq._chain_authority_anchor(
            frontier, "ok", bundle["chain_gate_requirement"],
            bundle["chain_attestation"])
        self.assertNotEqual(forged, bundle["chain_authority_anchor"],
                            "the anchor does not bind the tier state, so a "
                            "captured decision could be edited freely")


class ChainProofOverARealMintedApprove(ChainProofPrunedTipCloseBase):
    """PRODUCER TO CONSUMER through a real gate.run mint.

    The base chain-proof arms use the real verdict producer and retained tier
    capture, with gate binding supplied at a seam. This arm additionally mints
    a genuine NEED_SUITE receipt through gate.run; it proves receipt provenance,
    not merely that a supplied binding reaches the close consumer. Only queued
    child execution is replaced: the receipt retains the canonical serial gate
    argv and interpreter required by binding.
    """

    def setUp(self):
        super().setUp()
        # The mint is a real whole-suite gate.run: admission on a fixture
        # box, never this node's live cap (task/1740).
        from tests._tmphome import pin_admission
        pin_admission(self)

    def _minted_chain(self):
        self.git("checkout", "-q", "-b", "doomed", self.a)
        doomed = self.commit("doomed", path="h")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=doomed, lane="lane/minted", kind="review")
        _o, why = self.mark_verdict(row["id"], doomed, "findings",
                                          polarity="fix")
        self.assertIsNone(why, why)
        kid = self.dispatch(ref=self.a, lane="lane/minted-r2", kind="review",
                            supersedes=row["id"])
        self.git("checkout", "-q", self.a)
        with serial_process():
            receipt, gerr = gate.run(repo=self.repo)
        self.git("checkout", "-q", self.main)
        self.assertIsNone(gerr, gerr)
        _o, why = self.mark_verdict(
            kid["id"], self.a, gate.evidence_line(receipt), polarity="approve")
        self.assertIsNone(why, why)
        self.git("branch", "-q", "-D", "doomed")
        self._prune(doomed)
        self.doomed = doomed
        return row, kid

    def test_a_REAL_minted_approve_emits_the_shape_the_consumer_reads(self):
        """THE SEAM ITSELF. The seeded arms assume production records polarity
        approve WITH a non-empty gate on the frontier row; this asserts that a
        verified NEED_SUITE receipt actually produces it."""
        _row, kid = self._minted_chain()
        rows, _v, unavailable = dispatches.snapshot_with_verdicts()
        self.assertIsNone(unavailable)
        frontier = rows[kid["id"]]
        self.assertEqual(frontier.get("polarity"), "approve")
        self.assertTrue((frontier.get("gate") or "").strip(),
                        "a verified NEED_SUITE approve recorded no gate token, "
                        "so every seeded arm is testing a shape production "
                        "does not emit")

    def test_chain_proof_closes_over_the_real_mint_and_replays(self):
        row, kid = self._minted_chain()
        before = len(list(eventledger.events(dispatches.ledger_path())))
        out, why = landreq.close(row["id"], "chain-proof",
                                 evidence="objects pruned; authorizing "
                                          "descendant landed",
                                 repo=self.repo, trunk=self.main)
        self.assertIsNone(why, why)
        self.assertIsNotNone(out)
        self.assertEqual(len(list(eventledger.events(
            dispatches.ledger_path()))), before + 1)
        rows, _v, _u = dispatches.snapshot_with_verdicts()
        self.assertEqual(rows[row["id"]].get("close_reason"), "chain-proof")
        self.assertEqual(rows[row["id"]].get("carried_tip"), kid["id"])


class EveryCloseReasonIsAdmissibleAtTheWRITER(unittest.TestCase):
    """The register the CLI does not own, and the one my arms could not see.

    A reason can be spelled correctly in landreq.CLOSE_CLI_REASONS, documented,
    rendered in every usage string, and exercised by a full ladder of arms —
    and STILL be refused by dispatches.CLOSE_REASONS, because the ladder
    decides whether a close is PROVABLE and that tuple decides whether the
    write is ADMISSIBLE AT ALL. `chain-proof` shipped in exactly that state:
    eleven reasons on the CLI, nine at the writer, every ladder arm green, and
    the live actuator returning invalid. A cross-family reviewer found it by
    driving the production path instead of the ladder.
    """

    #: `out-of-scope` cancels a moot row through the CANCEL boundary and never
    #: reaches the close writer, so its absence is deliberate. Naming it here
    #: rather than skipping the check keeps the NEXT omission visible.
    CANCEL_ROUTED = ("out-of-scope",)

    def test_every_CLI_reason_is_accepted_by_the_writer(self):
        from helm import dispatches
        missing = [r for r in landreq.CLOSE_CLI_REASONS
                   if r not in dispatches.CLOSE_REASONS
                   and r not in self.CANCEL_ROUTED]
        self.assertEqual(missing, [], (
            "these close reasons are offered by the CLI and REFUSED by the "
            "writer, so `lr close --reason <r>` cannot bind however good its "
            "proof is: %s. Add them to dispatches.CLOSE_REASONS, or add them "
            "to CANCEL_ROUTED here if they genuinely route elsewhere."
            % ", ".join(missing)))

    def test_the_exception_is_REAL_and_not_a_way_to_silence_this(self):
        """CONTROL. If out-of-scope ever becomes writer-admissible, this arm
        goes red and the exemption must be deleted — an allowlist nobody
        re-checks is how the next gap hides behind this one."""
        from helm import dispatches
        for reason in self.CANCEL_ROUTED:
            self.assertIn(reason, landreq.CLOSE_CLI_REASONS,
                          "%s is exempted here but is not a CLI reason at "
                          "all — the exemption is stale" % reason)
            self.assertNotIn(reason, dispatches.CLOSE_REASONS,
                             "%s is now accepted by the writer, so its "
                             "exemption is a lie" % reason)

    def test_the_writer_offers_nothing_the_CLI_cannot_ask_for(self):
        """The other direction: a writer-only reason is a door with no handle,
        and it would sit undetected because no CLI surface would name it."""
        from helm import dispatches
        orphan = [r for r in dispatches.CLOSE_REASONS
                  if r not in landreq.CLOSE_CLI_REASONS]
        self.assertEqual(orphan, [])

    def test_every_writer_reason_has_a_TERMINAL_STATE(self):  # noqa: VACUOUS_ASSERTION — the must-hit controls are unconditional and immediately below: both registers must be non-empty and must both carry `landed`, so an empty-set difference cannot come from an empty register
        """THE REGISTER NOBODY PINNED, found while curing the chain-proof
        authority boundary. `rowstate._CLOSE_TERMINAL` decides what word an
        operator READS on a closed row, and a reason missing from it does not
        refuse — `rowstate` simply falls past the table and the close renders
        as something else. `carried` is pinned by name in test_lr_close; every
        other reason, `chain-proof` included, was pinned by nothing."""
        from helm import dispatches, rowstate
        # MUST-HIT FIRST: an empty register on either side would make the set
        # difference empty and this arm green while proving nothing.
        self.assertIn("landed", dispatches.CLOSE_REASONS)
        self.assertIn("landed", rowstate._CLOSE_TERMINAL)
        missing = sorted(set(dispatches.CLOSE_REASONS)
                         - set(rowstate._CLOSE_TERMINAL))
        self.assertEqual(missing, [], (
            "these writer-admissible close reasons have no terminal state, so "
            "a row closed with one renders by fallthrough: %s"
            % ", ".join(missing)))

    def test_chain_proof_renders_SUPERSEDED_and_never_LANDED(self):  # noqa: VACUOUS_ASSERTION — the control is `carried` still rendering LANDED in the same method, which is what stops this passing because every reason reads SUPERSEDED
        """THE POSTURE THE REASON EXISTS TO HOLD, and rowstate's own comment
        claimed arms already asserted it — they did not, in any file.

        chain-proof proves the FRONTIER's content reached trunk and that this
        row sits beneath it. It does NOT prove this row's own diff landed; that
        question died with the pruned object and no verb can resurrect it.
        LANDED is a claim about THIS row's work, so rendering it here would let
        the terminal state say exactly what the door refuses to say."""
        from helm import dispatches, rowstate
        self.assertEqual(rowstate._CLOSE_TERMINAL["chain-proof"],
                         rowstate.SUPERSEDED)
        self.assertNotEqual(rowstate._CLOSE_TERMINAL["chain-proof"],
                            rowstate.LANDED)
        # CONTROL, so this cannot pass because every reason reads SUPERSEDED:
        # the sibling whose proof IS carriage still renders LANDED.
        self.assertEqual(rowstate._CLOSE_TERMINAL["carried"], rowstate.LANDED)
        self.assertIn("chain-proof", dispatches._CLOSE_POLARITY)
        self.assertIn("chain-proof", dispatches._CLOSE_STATE_FIELDS)
        self.assertIn("chain_path",
                      dispatches._CLOSE_STATE_FIELDS["chain-proof"])


class ChainProofTransfersAUTHORITYNeverLaundersDebt(unittest.TestCase):
    """The contract after a reviewer falsified the original one.

    THE ORIGINAL CLAIM WAS "frontier landed, therefore member landed", and a
    cross-family reviewer constructed the counter-example: root legacy APPROVE
    landed A, child FIX doomed B, an OPEN unreviewed frontier back at A, prune
    B. The door reported would-append and closed the child while the frontier
    stayed OPEN — laundering unresolved FIX debt, which is strictly worse than
    leaving the row red. That repro is the primary negative below and is kept
    verbatim.

    THE NARROWED CONTRACT is authority transfer along ONE explicit supersedes
    path: a terminal, gate-authorized APPROVE that is a strict graph descendant
    carried the target's obligations, and its content is on trunk. Every
    negative here is a ONE-CLAUSE mutation of the positive, so each names the
    single condition it removes rather than failing for a reason the arm cannot
    distinguish.
    """

    def setUp(self):
        # THE TIER IS A LIVE POLICY READ and this class mints no HELM_HOME of
        # its own, so an operator with an `approval-tier` prior declared would
        # get different answers here than a fab node does — a fixture green
        # wherever I stand and red on a checkout. Pinned to the NO-POLICY
        # answer, which is what an isolated home resolves to anyway. The arm
        # that proves this door is CONSULTED overrides the pin, and the
        # end-to-end proof through the real resolver lives in
        # ChainProofPrunedTipCloseTest, which does isolate HELM_HOME.
        tier = mock.patch.object(dispatches, "approval_tier_for_verdict",
                                 lambda row: ("none", None))
        tier.start()
        self.addCleanup(tier.stop)

    def _chain(self, **over):
        """The positive: one linear path T -> M -> F, all debt resolved, F a
        terminal gate-backed APPROVE. Mutations pass one overriding clause."""
        rows = {
            "T": {"id": "T", "status": "verdict", "polarity": "fix",
                  "recipient": "codex", "close_reason": "resolved",
                  "reviewed_tip": "a" * 40},
            "M": {"id": "M", "status": "verdict", "polarity": "fix",
                  "recipient": "codex", "supersedes": "T",
                  "close_reason": "resolved", "reviewed_tip": "b" * 40},
            "F": {"id": "F", "status": "verdict", "polarity": "approve",
                  "recipient": "codex", "supersedes": "M",
                  "gate": "cafebabe12345678",
                  "gate_caps": [dispatches.GATE_CAP_RECEIPT],
                  "reviewed_tip": "c" * 40},
        }
        for rid, patch in over.items():
            if patch is None:
                rows.pop(rid, None)
            else:
                rows[rid].update(patch)
        return rows

    def _verdicts(self, **over):
        """THE CHRONOLOGY, WHICH IS A SEPARATE INPUT FROM THE ROWS.

        `_fold` keys verdict ORDER on the ledger append index and no folded row
        carries it, so an arm that can only mutate `rows` cannot move a verdict
        in time. That is why ten arms could all be green while an APPROVE
        recorded BEFORE the FIX it claims to carry walked straight through.
        """
        out = {"T": (10, "a" * 32), "M": (20, "b" * 32), "F": (30, "c" * 32)}
        for rid, value in over.items():
            if value is None:
                out.pop(rid, None)
            else:
                out[rid] = value
        return out

    def test_the_positive_resolves_to_the_gate_backed_descendant(self):
        """CONTROL, and it must come first: if this cannot pass, every refusal
        below is a door that refuses everything."""
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), self._verdicts())
        self.assertIsNone(why)
        self.assertEqual(frontier["id"], "F")

    # ------------------------------------------------------------------
    # BLOCKER TWO (row 9f09ff4b858e): "_chain_authority treats ANY
    # nonempty gate as authorization, bypassing the canonical tier/gate_caps/
    # epoch checks." Every arm below names an input the OLD shape ADMITTED.
    # ------------------------------------------------------------------

    def test_a_NONEMPTY_but_MALFORMED_gate_token_refuses(self):
        """THE MUST-MISS FOR BLOCKER TWO. `gate: "gate-pending"` is nonempty,
        so a truthiness test calls it authorization; it is not a minted receipt
        id and `helm gate verify` could never resolve it. If the only thing a
        forger needs is a non-blank string, the gate rung is decoration."""
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(F={"gate": "gate-pending"}), self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("gate", why)

    def test_an_UNREADABLE_gate_caps_STAMP_refuses(self):
        """`gate_caps: None` is PRESENT and unreadable — `gate_requirement`'s
        third state, which exists precisely so a corrupt byte cannot launder an
        ungated approve. Reading only the token skips that question entirely."""
        # CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: a door that
        # refused every input would satisfy the refusal below perfectly.
        self.assertEqual(landreq._chain_authority(
            "T", self._chain(), self._verdicts())[0]["id"], "F",
            "the door refuses everything — the refusal below proves nothing")
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(F={"gate_caps": None}), self._verdicts())
        self.assertIsNone(frontier)
        self.assertIsNotNone(why)

    def test_the_TOPOLOGY_walk_no_longer_asks_the_TIER_at_all(self):
        """RULING 2 MOVED THIS QUESTION, and the arm follows it rather than
        pretending the old location still owns it.

        `_chain_authority` runs on REPLAY, so it may only re-derive what the
        ledger already fixed. A hostile tier reading must therefore change
        NOTHING here — the tier is decided once at the writer and travels as a
        capture. The refusal itself is asserted where it now lives, in
        `UnmeasurableTierNeedsAnExplicitAttestation`.
        """
        with mock.patch.object(
                dispatches, "approval_tier_for_verdict",
                lambda row: ("outside", "hostile reading")):
            frontier, why, _proof = landreq._chain_authority(
                "T", self._chain(), self._verdicts())
        self.assertIsNone(why, why)
        self.assertEqual(frontier["id"], "F",
                         "the replay-side walk consulted mutable tier policy, "
                         "so a policy edit could dissolve a terminal")

    #: The refusal `approval_tier_for_verdict` returns for a row that carries
    #: no author stamp at all. DERIVED from the production wording rather than
    #: transcribed, so a change at the producer cannot leave these arms
    #: silently testing a sentence nobody emits any more.
    def test_an_APPROVE_that_PREDATES_the_TARGET_refuses(self):
        """THE MUST-MISS FOR BLOCKER THREE. The whole claim is that a later
        approve CARRIED this row's obligations. An approve recorded BEFORE the
        FIX existed carried nothing — it cannot have judged work that had not
        been verdicted yet. The topology is identical, so no shape check here
        can see it; only the append order can."""
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), self._verdicts(F=(5, "c" * 32)))
        self.assertIsNone(frontier)
        self.assertIn("predates", why)

    def test_an_APPROVE_that_PREDATES_AN_INTERMEDIATE_debt_row_refuses(self):
        """The sibling: the approve post-dates the target and still predates a
        FIX further down the path it claims to have carried."""
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), self._verdicts(F=(15, "c" * 32)))
        self.assertIsNone(frontier)
        self.assertIn("predates", why)

    def test_a_DEBT_row_with_NO_recorded_verdict_index_refuses(self):
        """FAIL CLOSED ON UNASKABLE, never on absence read as innocence: a
        FIX-verdicted row the fold never accepted cannot be ordered against the
        approve, so the carrying claim is unmeasurable rather than true."""
        # CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: a door that
        # refused every input would satisfy the refusal below perfectly.
        self.assertEqual(landreq._chain_authority(
            "T", self._chain(), self._verdicts())[0]["id"], "F",
            "the door refuses everything — the refusal below proves nothing")
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), self._verdicts(M=None))
        self.assertIsNone(frontier)
        self.assertIsNotNone(why)

    def test_a_FRONTIER_with_NO_recorded_verdict_index_refuses(self):
        """The frontier's own position is what every comparison is against."""
        # CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: a door that
        # refused every input would satisfy the refusal below perfectly.
        self.assertEqual(landreq._chain_authority(
            "T", self._chain(), self._verdicts())[0]["id"], "F",
            "the door refuses everything — the refusal below proves nothing")
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), self._verdicts(F=None))
        self.assertIsNone(frontier)
        self.assertIsNotNone(why)

    def test_the_chronology_input_is_REQUIRED_not_optional(self):
        """A caller that cannot supply the verdict order gets a REFUSAL, never
        a silently unordered pass — the shape that let blocker three exist."""
        # CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE: a door that
        # refused every input would satisfy the refusal below perfectly.
        self.assertEqual(landreq._chain_authority(
            "T", self._chain(), self._verdicts())[0]["id"], "F",
            "the door refuses everything — the refusal below proves nothing")
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(), None)
        self.assertIsNone(frontier)
        self.assertIsNotNone(why)

    def test_THE_LAUNDERING_REPRO_refuses(self):
        """VERBATIM from the reviewer: an old APPROVE, a doomed FIX beneath it,
        and an OPEN frontier. The original shape closed the child here."""
        rows = {
            "A": {"id": "A", "status": "verdict", "polarity": "approve",
                  "gate": "cafebabe12345678", "reviewed_tip": "a" * 40},
            "B": {"id": "B", "status": "verdict", "polarity": "fix",
                  "supersedes": "A", "reviewed_tip": "b" * 40},
            "C": {"id": "C", "status": "open", "supersedes": "B",
                  "reviewed_tip": "c" * 40},
        }
        frontier, why, _proof = landreq._chain_authority(
            "B", rows, {"A": (10, "a" * 32), "B": (20, "b" * 32)})
        self.assertIsNone(frontier)
        self.assertIn("OPEN", why)

    def test_an_OPEN_frontier_refuses(self):
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(F={"status": "open"}), self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("OPEN", why)

    def test_an_UNGATED_approve_refuses(self):
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(F={"gate": ""}), self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("gate", why)

    def test_a_NON_APPROVE_frontier_refuses(self):
        frontier, why, _proof = landreq._chain_authority(
            "T", self._chain(F={"polarity": "fix"}), self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("approve", why)

    def test_an_UNRESOLVED_FIX_on_the_path_refuses(self):
        """The invariant: chain-proof may never close beneath open debt."""
        rows = self._chain()
        rows["M"].pop("close_reason")
        frontier, why, _proof = landreq._chain_authority(
            "T", rows, self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("unresolved", why)

    def test_a_SECOND_frontier_refuses(self):
        rows = self._chain()
        rows["F2"] = {"id": "F2", "status": "verdict", "polarity": "approve",
                      "supersedes": "M", "gate": "cafebabe12345678",
                      "reviewed_tip": "d" * 40}
        frontier, why, _proof = landreq._chain_authority(
            "T", rows, self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("successors", why)

    def test_a_BROKEN_supersedes_link_refuses(self):
        """With M no longer superseding T, F is a COUSIN — same chain, no
        descent. This is the case chain_root membership could not tell apart."""
        rows = self._chain()
        rows["M"].pop("supersedes")
        frontier, why, _proof = landreq._chain_authority(
            "T", rows, self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("no successor", why)

    def test_an_APPROVE_on_an_ANCESTOR_transfers_nothing(self):
        """The approve sits ABOVE the target, so nothing downstream carries it.
        The predecessor's chain-wide search accepted exactly this."""
        rows = {
            "A": {"id": "A", "status": "verdict", "polarity": "approve",
                  "gate": "cafebabe12345678", "reviewed_tip": "a" * 40},
            "T": {"id": "T", "status": "verdict", "polarity": "fix",
                  "supersedes": "A", "reviewed_tip": "b" * 40},
        }
        frontier, why, _proof = landreq._chain_authority(
            "T", rows, self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("no successor", why)

    def test_a_SUPERSEDES_CYCLE_refuses_rather_than_looping(self):
        rows = self._chain()
        rows["T"]["supersedes"] = "F"
        frontier, why, _proof = landreq._chain_authority(
            "T", rows, self._verdicts())
        self.assertIsNone(frontier)
        self.assertIn("cycle", why)


class TheDeriveBoundsItsAggregateTest(IsolatedEstateTest):
    """Every git spawn under the landing derive was individually fast and
    individually bounded; the derive over N rows had no bound at all. Measured
    on the live ledger: 1301 cherry spawns in one inject, ~50ms each, ~65s of
    git against a 10s hook budget. Per-item correctness is never the standard
    — something has to bound the aggregate.
    """

    def test_the_batch_path_asks_the_index_not_one_cherry_per_row(self):
        calls = []

        def fake_git(gitdir, *args, input_text=None, env=None):
            calls.append(git_verb(args))
            out = {"rev-parse": "abc123\n", "show": "diff\n",
                   "patch-id": "pid-of-tip sha\n"}.get(git_verb(args), "")
            return types.SimpleNamespace(returncode=0, stdout=out)

        with mock.patch.object(landreq, "_git", fake_git), \
             mock.patch.object(landreq, "_landed_index",
                               return_value=({"pid-of-tip": "trunksha"}, False)), \
             mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
            got = landreq._landing_proof("/g", ("de" * 20), "origin/main")
        self.assertEqual(got, "patch-equivalent")
        # POSITIVE CONTROL ON `calls` ITSELF, which is the observable the
        # absence below is about. Asserting the verdict constrains `got`, a
        # different observable — and an empty `calls` satisfies "cherry was
        # not spawned" perfectly while proving the batch path never ran.
        self.assertEqual(calls, ["rev-parse", "show", "patch-id"],
                         "the batch path did not run the spawns it answers "
                         "from, so the absence below is about a path that "
                         "never executed")
        self.assertNotIn("cherry", calls,
                         "the index answered and cherry was spawned anyway, "
                         "which is the per-row cost this exists to remove")

    def test_a_CAPPED_miss_is_UNKNOWN_and_never_ABSENT(self):
        """THE ONE THAT MATTERS. A bounded scan that did not find the patch
        has not proven the patch is not on trunk — it has proven the window
        was too small. Reporting that as ABSENT turns 'I could not see' into
        'it did not land' on a surface people read as truth."""
        def fake_git(gitdir, *args, input_text=None, env=None):
            out = {"rev-parse": "abc123\n", "show": "diff\n",
                   "patch-id": "unmatched-pid sha\n"}.get(git_verb(args), "")
            return types.SimpleNamespace(returncode=0, stdout=out)

        # A DISTINCT TIP PER PROBE, and this is load-bearing rather than
        # tidiness: `_landing_proof` KEEPS a positive answer for its exact
        # (repo, tip, trunk) triple, so re-asking under one tip with a
        # different mocked index is answered from the FIRST probe's kept
        # proof and never reaches the ladder at all. The subject here is
        # capped-ness, so each probe gets its own tip and the ledger has
        # nothing to say about any of them.
        # POSITIVE CONTROL: the SAME ladder, same fixture, answers
        # patch-equivalent on a HIT. Without it every row below is satisfied
        # by a ladder that returns a non-answer for every input.
        with mock.patch.object(landreq, "_git", fake_git), \
             mock.patch.object(landreq, "_landed_index",
                               return_value=({"unmatched-pid": "x"}, False)), \
             mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
            self.assertEqual(
                landreq._landing_proof("/g", ("ab" * 20), "origin/main"),
                "patch-equivalent",
                "the ladder cannot answer at all under this fixture, so the "
                "verdicts below are not about capped-ness")
        for tip, (capped, expected) in zip(("cd" * 20, "ef" * 20),
                                           ((True, "unknown"),
                                            (False, "absent"))):
            with self.subTest(why=capped):
                with mock.patch.object(landreq, "_git", fake_git), \
                     mock.patch.object(landreq, "_landed_index",
                                       return_value=({"other": "x"}, capped)), \
                     mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
                    got = landreq._landing_proof("/g", tip, "origin/main")
                self.assertEqual(got, expected)

    def test_the_tip_id_is_derived_by_git_and_never_read_off_the_row(self):  # noqa: VACUOUS_ASSERTION — the piped-diff equality is the unconditional positive control; the view assertions loop over a list the assertTrue above proves non-empty
        """A stored patch_id is correlation evidence: a hand-built receipt
        carrying a dead tip plus any unrelated live id once claimed its own
        landing. The id compared must come from THIS tip, now."""
        seen = []

        def fake_git(gitdir, *args, input_text=None, env=None):
            seen.append((git_verb(args), input_text, tuple(args),
                         tuple(sorted((env or {}).items()))))
            out = {"rev-parse": "abc123\n", "show": "REALDIFF\n",
                   "patch-id": "derived-pid sha\n"}.get(git_verb(args), "")
            return types.SimpleNamespace(returncode=0, stdout=out)

        with mock.patch.object(landreq, "_git", fake_git), \
             mock.patch.object(landreq, "_landed_index",
                               return_value=({"derived-pid": "t"}, False)), \
             mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
            landreq._landing_proof("/g", ("de" * 20), "origin/main")
        piped = [r[1] for r in seen if r[0] == "patch-id"]
        self.assertEqual(piped, ["REALDIFF\n"],
                         "patch-id was not fed this tip's own diff")
        # AND THE RENDERING RAN UNDER THE OBJECT VIEW. A `git show` resolved
        # through a replace ref renders the REPLACEMENT's content, so an id
        # derived from an ambient render is a true patch-id of the wrong
        # commit — the failure this arm's whole subject (derive it from THIS
        # tip, now) is about, one layer down.
        shows = [r for r in seen if r[0] == "show"]
        self.assertTrue(shows, "the tip was never rendered")
        for _, _, argv, env in shows:
            self.assertIn("--no-replace-objects", argv,
                          "the tip was rendered through replace refs")
            self.assertEqual(dict(env).get("GIT_GRAFT_FILE"), os.devnull,
                             "the tip was rendered under the ambient grafts")
        # MUST-HIT CONTROL on the same recorder: the patch-id spawn does NOT
        # carry the view, because it hashes the text it is handed and a view
        # flag there would be cargo. So the assertions above measure a
        # deliberate placement, not a recorder that stamps everything.
        pids = [r for r in seen if r[0] == "patch-id"]
        self.assertTrue(pids)
        for _, _, argv, env in pids:
            self.assertNotIn("--no-replace-objects", argv)
            self.assertEqual(env, ())

    def test_an_unbuildable_index_falls_back_rather_than_guessing(self):
        """CONTROL. If the index is unavailable the ladder must return to the
        per-row path, not answer from nothing — every arm above passes if the
        batch simply swallowed the question."""
        calls = []

        def fake_git(gitdir, *args, input_text=None, env=None):
            calls.append(args[0])
            out = {"rev-parse": "abc123\n"}.get(args[0], "")
            if args[0] == "cherry":
                out = "- %s\n" % ("de" * 20)
            return types.SimpleNamespace(returncode=0, stdout=out)

        with mock.patch.object(landreq, "_git", fake_git), \
             mock.patch.object(landreq, "_landed_index",
                               return_value=(None, False)), \
             mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
            got = landreq._landing_proof("/g", ("de" * 20), "origin/main")
        self.assertIn("cherry", calls,
                      "an unbuildable index skipped the fallback entirely")
        self.assertEqual(got, "patch-equivalent")


class AmbientLandingExpiryPropagationTest(IsolatedEstateTest):
    """A cooperative Stop expiry is control flow, never local UNKNOWN."""

    def test_patch_index_expiry_escapes_the_fallback(self):  # noqa: VACUOUS_ASSERTION — planted Expired is the positive control
        """THE PROPERTY IS UNCHANGED; ONLY THE FUNCTION ON THE PATH MOVED.

        This arm planted its Expired in `_stored_patch_index` because that was
        what `_landed_index` called. This lane made the index DURABLE AND
        EXTENDED rather than rebuilt, so `_trunk_index_extend` is the builder
        now and a plant in the old one is a plant beside the path — the arm
        would have gone green-by-vacuity if the assertion had been weaker, and
        went red here instead, which is the right way round.

        THE INVARIANT IT PINS MATTERS MORE AGAINST THE EXTENDING INDEX, not
        less: the extend spends a budget deliberately and yields, so an
        Expired swallowed by `_landed_index`'s blanket clause would report NO
        INDEX for a walk that was working when the clock ran out.
        """
        expired = projscope.Expired("patch index")
        with mock.patch.object(landreq, "_trunk_index_extend",
                               side_effect=expired):
            with self.assertRaises(projscope.Expired):
                landreq._landed_index("/git", "trunk")
        # THE CONTROL, on the same call: without a planted expiry the same
        # builder answers rather than raising, so the raise above is the
        # plant escaping and not `_landed_index` refusing everything.
        with mock.patch.object(landreq, "_trunk_index_extend",
                               return_value=({"pid": "sha"}, False)):
            self.assertEqual(landreq._landed_index("/git", "trunk"),
                             ({"pid": "sha"}, False))

    def test_ancestry_expiry_stops_before_later_pairs(self):  # noqa: VACUOUS_ASSERTION — one backend call proves the planted expiry path ran
        backend = mock.Mock()
        backend.text.side_effect = projscope.Expired("ancestry")
        with mock.patch.object(landreq.vcs, "backend", return_value=backend):
            with self.assertRaises(projscope.Expired):
                landreq._ancestry_append(
                    "/git", {("older-a", "newer-a"),
                              ("older-b", "newer-b")})
        self.assertEqual(backend.text.call_count, 1,
                         "ancestry continued to later pairs after expiry")

    def test_trunk_ref_expiry_escapes_the_pin_fallback(self):  # noqa: VACUOUS_ASSERTION — planted Expired is the positive control
        proof = landreq._landing_proofs()
        carrier = {"repo_id": "/git/.git", "reviewed_tip": "a" * 40}
        with mock.patch.object(landreq, "_trunk_refs",
                               side_effect=projscope.Expired("trunk refs")):
            with self.assertRaises(projscope.Expired):
                proof({}, carrier)

    def test_resolved_ref_expiry_escapes_the_pin_fallback(self):  # noqa: VACUOUS_ASSERTION — planted Expired is the positive control
        proof = landreq._landing_proofs()
        carrier = {"repo_id": "/git/.git", "reviewed_tip": "a" * 40}
        with mock.patch.object(landreq, "_trunk_refs",
                               return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(
                    landreq, "_resolved_ref",
                    side_effect=projscope.Expired("resolved ref")):
            with self.assertRaises(projscope.Expired):
                proof({}, carrier)

    def test_landed_state_expiry_escapes_the_proof_fallback(self):  # noqa: VACUOUS_ASSERTION — backend call proves the planted expiry path ran
        proof = landreq._landing_proofs()
        carrier = {"repo_id": "/git/.git", "reviewed_tip": "a" * 40}
        backend = mock.Mock()
        backend.landed_state.side_effect = projscope.Expired("landed state")
        with mock.patch.object(landreq, "_trunk_refs",
                               return_value=("refs/heads/main", None)), \
                mock.patch.object(landreq, "_origin_configured",
                                  return_value=False), \
                mock.patch.object(landreq, "_resolved_ref",
                                  return_value="b" * 40), \
                mock.patch.object(landreq, "_derive_expired",
                                  return_value=False), \
                mock.patch.object(landreq.vcs, "backend",
                                  return_value=backend):
            with self.assertRaises(projscope.Expired):
                proof({}, carrier)
        backend.landed_state.assert_called_once()


class TheDeriveBudgetIsArmedAtTheLayerTest(IsolatedEstateTest):
    """Round one bounded the derive loop in `verified_lands` — the caller I
    had found — and the next capture blew 9.03s entering through
    `landed_ever`, a caller the bound never covered. A bound at a caller is
    only as complete as the caller census, so the budget moved to the door
    every path passes.

    ROUND TWO'S FIRST ATTEMPT AT THAT DOOR WAS PROCESS-WIDE AND BROKE 82
    TESTS: a deadline armed on first entry expires four seconds into any
    long-running process and answers UNKNOWN for everything after. The unit
    that needs bounding is ONE PROJECTION, not one process.
    """

    def _expired_scope(self):
        """A scope whose budget is already spent, without sleeping for it."""
        return mock.patch.object(
            landreq.projscope, "memo",
            side_effect=lambda key, compute: (
                time.monotonic() - 1
                if key == ("landreq._derive_deadline",) else compute()))

    def _spy_git(self, spawns):
        def fake_git(gitdir, *args, input_text=None, env=None):
            spawns.append(args[0])
            out = {"rev-parse": "abc123\n", "show": "d\n",
                   "patch-id": "pid sha\n"}.get(args[0], "")
            return types.SimpleNamespace(returncode=0, stdout=out)
        return fake_git

    # SPLIT INTO TWO ARMS AT ROUND FOUR, deliberately. One method was proving
    # two properties — that an expired budget SPENDS NOTHING and that it
    # ANSWERS UNKNOWN — so each property's control sat next to the other
    # property's assertion, and the arm kept reading as instrumentation-only
    # no matter which control I added. Three patches did not fix that; the
    # shape did. One observable per method, its own control beside it.

    def test_an_expired_budget_SPENDS_no_git_through_landed_ever(self):  # noqa: VACUOUS_ASSERTION — the observable here IS the instrumentation, deliberately: the property is 'the door precedes the work', and the only witness to work-NOT-done is the spawn spy. `fresh_spawns` is an unconditional positive control on that same spy under a fresh budget; the rung cannot credit it because it is a different BINDING, and it rejects spy-only positives by design
        """THE PATH THAT ACTUALLY BLEW: round one's bound was real and this
        entry point walked straight past it. Observable: the spawn list."""
        spawns = []
        with self._expired_scope(), \
             mock.patch.object(landreq, "_git", self._spy_git(spawns)):
            landreq.landed_ever("/g", ("de" * 20), "origin/main")
        expired_spawns = list(spawns)

        spawns.clear()
        with mock.patch.object(landreq, "_git", self._spy_git(spawns)):
            landreq.landed_ever("/g", ("de" * 20), "origin/main")
        fresh_spawns = list(spawns)

        # The control is the SAME observable under a fresh budget: without it
        # an empty expired list is satisfied by a fixture that never ran.
        self.assertTrue(fresh_spawns,
                        "the unexpired path spends no git either, so the "
                        "emptiness below is about a fixture that never ran")
        self.assertEqual(expired_spawns, [],
                         "an expired budget still spent git, so the door is "
                         "not in front of the work")

    def test_an_expired_budget_ANSWERS_unknown_through_landed_ever(self):  # noqa: VACUOUS_ASSERTION — `fresh` is an unconditional positive control on the verdict, asserted BEFORE the absence and from the same call under a fresh budget; the rung cannot credit it because expired and fresh are separate bindings
        """Same entry point, the other property. Observable: the verdict."""
        spawns = []
        with self._expired_scope(), \
             mock.patch.object(landreq, "_git", self._spy_git(spawns)):
            expired = landreq.landed_ever("/g", ("de" * 20), "origin/main")
        with mock.patch.object(landreq, "_git", self._spy_git(spawns)):
            fresh = landreq.landed_ever("/g", ("de" * 20), "origin/main")

        # Control on the verdict itself: the ladder must be able to produce a
        # real answer here, or "no answer when expired" is about a fixture
        # that can never answer at all.
        self.assertIsNotNone(fresh,
                             "the unexpired path produced no verdict, so the "
                             "non-answer below proves nothing about the door")
        self.assertIn(expired, (None, "unknown", False),
                      "an expired budget produced a verdict: %r" % (expired,))

    def test_a_fresh_budget_still_answers(self):
        """THE CONTROL, and the one the 82-failure round would have failed:
        every arm here passes if the layer simply refused everything, which
        is a worse outage than the one it cures."""
        def fake_git(gitdir, *args, input_text=None, env=None):
            out = {"rev-parse": "abc123\n", "show": "d\n",
                   "patch-id": "pid sha\n"}.get(git_verb(args), "")
            return types.SimpleNamespace(returncode=0, stdout=out)

        with mock.patch.object(landreq, "_git", fake_git), \
             mock.patch.object(landreq, "_landed_index",
                               return_value=({"pid": "t"}, False)), \
             mock.patch.object(landreq, "_ancestry",
                               return_value=landreq.NOT_ANCESTOR):
            got = landreq._landing_proof("/g", ("de" * 20), "origin/main")
        self.assertEqual(got, "patch-equivalent",
                         "a fresh budget refused a provable land")

    def _local_carrier_refs(self):
        """A carrier the arm can always build, in ANY checkout — INCLUDING fab.

        HISTORY, because this fixture has now been wrong in two different ways
        and both were about borrowing someone else's refs. The first cut bound
        `merge-base HEAD origin/main` behind a skipTest; that was replaced with
        HEAD~1 of the AMBIENT CHECKOUT to remove the remote dependency, and a
        skip was rejected outright — "a skip is a green that proves nothing",
        and this gate already reports skipped=17, so one more is
        indistinguishable from the others.

        THE SECOND ORIGIN DEPENDENCY WAS STILL THERE, one level down, and it
        made these arms fail on every fab run (task/1924). `proof_for` resolves
        trunk through `landreq._pin`, which reads the checkout's OWN refs — so
        the arms inherited whatever ref layout they happened to stand in.
        MEASURED, same probe both hosts: locally the checkout has an origin and
        `_trunk_refs` answers ('refs/heads/main', 'refs/remotes/origin/main');
        on fab the run happens in a worktree of a BARE MIRROR with NO origin
        and NO refs/heads/main, so `_trunk_refs` answers (None, None), `_pin`
        returns None, and `proof_for` answers PROOF_NO_PIN. The arms' own
        must-hit reported that correctly for weeks.

        SO THE FIXTURE BUILDS ITS OWN REPO, which keeps every property the
        rewrite above argued for and drops the one that was never intended.
        Still a REAL repo with REAL refs and a deterministic ancestor; still no
        skip. And it is now STRONGER than the ambient version: two commits on
        `main` make the carrier tip a genuine ANCESTOR of the pinned trunk, so
        these arms exercise PROOF_ANCESTOR rather than whatever the surrounding
        checkout happened to yield. MEASURED: a repo built exactly this way
        resolves identically on this box and on fab.
        """
        root = tempfile.mkdtemp(prefix="carrier-trunk-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        def git(*args):
            return subprocess.run(["git", "-C", root, *args],
                                  capture_output=True, text=True).stdout.strip()

        git("init", "-q", "-b", "main")
        git("config", "user.email", "fixture@helm.test")
        git("config", "user.name", "fixture")
        for n in ("base", "tip"):
            with open(os.path.join(root, n), "w") as fh:
                fh.write(n)
            git("add", n)
            git("commit", "-q", "-m", n)
        gitdir, base = git("rev-parse", "--absolute-git-dir"), git("rev-parse", "HEAD~1")
        # A SECOND CARRIER THE CONTROL NEVER TOUCHES. Both arms below derive
        # one carrier as their positive control, and a derived answer is now
        # KEPT durably — read back above the budget door on purpose, because a
        # kept verdict costs no spawn. So the budget question has to be asked
        # about a carrier nothing has answered yet, or it measures the ledger.
        head = git("rev-parse", "HEAD")

        # MUST-HIT, WIDENED TO THE THING THAT ACTUALLY BROKE. The old one
        # checked only that a gitdir and a base exist, which was TRUE on fab
        # while the trunk was unresolvable — so it could not see the failure it
        # was written to guard. A carrier the annotator cannot pin is not a
        # carrier these arms can test.
        self.assertTrue(gitdir and base,
                        "MUST-HIT: no git checkout here, so these arms are "
                        "not exercising proof_for at all")
        self.assertTrue(
            landreq._trunk_refs(gitdir, {})[0],
            "MUST-HIT: this fixture's own repo has no resolvable trunk, so "
            "proof_for would answer NO-PIN and every assertion below would be "
            "about a fixture rather than about the budget door")
        self.assertNotEqual(head, base,
                            "MUST-HIT: the control carrier and the budget "
                            "carrier are the same object, so a kept answer "
                            "would satisfy the budget question")
        return gitdir, base, head

    # THE SECOND DOOR, added when the round-two derive cure landed and inject
    # STILL blew 30s: the faulthandler stack named a caller the derive budget
    # never covered — board_section -> _annotate_succession -> proof_for ->
    # vcs.landed_state, one git per carrier. proof_for now shares the SAME
    # projection deadline. Real repo, because _pin resolves the trunk through
    # the checkout's own refs and a landed ancestor is deterministic there.

    def test_the_succession_proof_honors_the_budget_function(self):  # noqa: VACUOUS_ASSERTION — `live` is an unconditional positive control on the SAME observable (the typed proof) asserted BEFORE the expired case, from the same carrier; the rung cannot credit it because live and expired are separate bindings, exactly as the landed_ever arms above
        """One observable, the typed proof, under both budget states.

        A carrier whose reviewed_tip is an ancestor of the pinned trunk MUST
        answer PROOF_ANCESTOR when the budget is live. The SAME carrier MUST
        answer PROOF_UNKNOWN once the budget is spent — that is the guard, and
        it degrades to the state SUCCESSION_UNKNOWN already names 'carrier
        landing proof is unreadable'. Removing the guard makes the expired
        case compute the real ancestor (reddens); always-unknown makes the
        live case lose it (reddens). Both mutants die on this one arm.
        """
        gitdir, base, head = self._local_carrier_refs()
        carrier = {"repo_id": gitdir, "reviewed_tip": base}
        # THE BUDGET CARRIER IS THE ONE NOTHING HAS ANSWERED. A derived answer
        # is kept durably and read back ABOVE this door, so asking the budget
        # question about the control's own carrier would measure the ledger
        # (which is its sibling arm's subject) instead of the door.
        undecided = {"repo_id": gitdir, "reviewed_tip": head}

        with mock.patch.object(landreq, "_derive_expired", return_value=False):
            pf = landreq._landing_proofs(gitdir)
            live = pf({}, carrier)
        with mock.patch.object(landreq, "_derive_expired", return_value=True):
            pf = landreq._landing_proofs(gitdir)
            expired = pf({}, undecided)

        self.assertNotIn(live, (landreq.PROOF_UNKNOWN, landreq.PROOF_NO_PIN,
                                landreq.PROOF_NO_REPO, landreq.PROOF_NO_TIP),
                         "MUST-HIT: this checkout could not answer the carrier "
                         "at all (got %r), so the expired non-answer proves "
                         "nothing about the door" % (live,))
        self.assertEqual(expired, landreq.PROOF_UNKNOWN,
                         "an expired budget still computed a succession "
                         "proof: %r" % (expired,))

    def test_the_succession_proof_SHARES_the_budget_landing_proof_spent(self):
        """THE DISCRIMINATING ARM (a review of the first cut).

        Patching `_derive_expired` proves proof_for respects A budget function
        — it would pass identically against a PRIVATE budget, which is the
        design under question. So this arm patches nothing: it spends the
        REAL projection deadline through the OTHER door and then asks this one.

        One projscope. `_landing_proof` (door one) arms the shared deadline;
        real time passes it; `proof_for` (door two) must then find it spent.
        A private budget would arm fresh at its own first call and answer
        PROOF_ANCESTOR here — so that mutation reddens this and only this arm.
        """
        gitdir, base, head = self._local_carrier_refs()
        carrier = {"repo_id": gitdir, "reviewed_tip": base}
        undecided = {"repo_id": gitdir, "reviewed_tip": head}

        budget = 0.2
        with mock.patch.object(landreq, "_DERIVE_BUDGET_S", budget):
            # CONTROL first: a fresh scope, nothing spent, same carrier. If
            # this is not ANCESTOR the arm below proves nothing.
            with landreq.projscope.scope():
                live = landreq._landing_proofs(gitdir)({}, carrier)

            # Now the real thing: door one spends the shared deadline.
            with landreq.projscope.scope():
                landreq._landing_proof(gitdir, base, "origin/main")
                time.sleep(budget + 0.05)     # the SHARED deadline is now past
                self.assertTrue(landreq._derive_expired(),
                                "MUST-HIT: the shared deadline did not expire, "
                                "so the assertion below is not about sharing")
                shared = landreq._landing_proofs(gitdir)({}, undecided)

        self.assertNotIn(live, (landreq.PROOF_UNKNOWN, landreq.PROOF_NO_PIN,
                                landreq.PROOF_NO_REPO, landreq.PROOF_NO_TIP),
                         "MUST-HIT: this checkout could not answer the carrier "
                         "at all (got %r), so the UNKNOWN below is about a "
                         "broken fixture, not the shared budget" % (live,))
        self.assertEqual(shared, landreq.PROOF_UNKNOWN,
                         "proof_for answered %r after _landing_proof spent the "
                         "projection budget — it is NOT sharing that budget, it "
                         "has its own" % (shared,))

    def test_OUTSIDE_a_projection_the_budget_never_latches(self):
        """The 82-failure mode, pinned. With no scope the memo computes fresh
        each call, so a long-running process can never inherit a spent budget
        from something that ran before it."""
        self.assertFalse(landreq.projscope.active(),
                         "MUST-HIT: a scope is active here, so this arm is "
                         "not testing the no-scope path it names")
        for _ in range(3):
            self.assertFalse(landreq._derive_expired(),
                             "the budget latched outside a projection")
        # POSITIVE CONTROL on the same observable: _derive_expired CAN return
        # True. Without it this arm passes against a budget that never expires
        # anywhere, which would be the outage uncured rather than the
        # latch-free property it claims.
        with self._expired_scope():
            self.assertTrue(landreq._derive_expired(),
                            "_derive_expired never returns True, so the "
                            "False results above prove nothing")

    def test_every_spending_call_sits_behind_the_door(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNotNone/assertTrue must-hits above the ordering check ARE its unconditional positive controls: they fail loudly if the parse found no function, no guard, or no spending call, so the assertLess can never pass on an empty census
        """THE CENSUS, with a must-hit — the arm that would have caught round
        one. It fails when a new entry point reaches the git-spending rungs
        without passing the budget."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(landreq))
        def _fn(name):
            got = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == name), None)
            self.assertIsNotNone(got, "MUST-HIT: %s was not found in the "
                                      "parsed source, so this census read "
                                      "nothing and its pass means nothing"
                                      % name)
            return got

        def _spends(fn):
            return [n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", "") in ("_git", "_ancestry",
                                                      "_tip_patch_id")]

        # THE CENSUS NOW READS TWO FUNCTIONS BECAUSE THE LADDER IS TWO, and
        # this is a STRICTER claim than the one it replaces. `_landing_proof`
        # is the door: it reads the durable ledger, delegates, and records —
        # and it must spend NOTHING itself, which is exactly why its kept read
        # is allowed to sit above the budget. `_derive_landing_proof` is the
        # ladder, and every spending call in it still sits behind the guard.
        door = _fn("_landing_proof")
        self.assertEqual(_spends(door), [],
                         "_landing_proof spends git directly — the door must "
                         "delegate, or its pre-budget ledger read becomes a "
                         "way past the bound")
        delegates = [n for n in ast.walk(door)
                     if isinstance(n, ast.Call)
                     and getattr(n.func, "id", "") == "_derive_landing_proof"]
        self.assertTrue(delegates, "MUST-HIT: the door no longer calls the "
                                   "ladder, so the census below is about a "
                                   "function nothing reaches")
        ladder = _fn("_derive_landing_proof")
        guard = next((n for n in ast.walk(ladder)
                      if isinstance(n, ast.Call)
                      and getattr(n.func, "id", "") == "_derive_expired"), None)
        self.assertIsNotNone(guard,
                             "the landing ladder no longer consults the "
                             "derive budget — the door moved back to the "
                             "callers")
        spends = _spends(ladder)
        self.assertTrue(spends, "MUST-HIT: no git-spending call found inside "
                                "_derive_landing_proof, so the ordering "
                                "assertion below has nothing to order")
        self.assertLess(guard.lineno, min(spends),
                        "the budget check does not precede every spending "
                        "call, so an expired burst still pays git")


# THE GIT-ANSWER DOUBLES AND THE RESCUE-STORE FIXTURE BELOW ARE SHARED WITH
# `tests/test_lr_landing_store.py`, which imports them from here rather than
# copying them. They stay in this module on purpose: one definition of what a
# faked git answer looks like, so the two files cannot come to disagree about
# it, and the importer cannot import back into this one.
class _Ok(object):
    """A git result the module's own helper shape accepts."""

    returncode = 0

    def __init__(self, out):
        self.stdout = out


class _Fail(object):
    """A git call that RAN and FAILED -- distinct from `_git` returning None,
    which is a call that could not run at all. Both are UNKNOWN to a caller
    that decides on the answer, and a fixture that only ever produces one of
    them cannot show whether a cure covers both."""

    returncode = 128

    def __init__(self, out=""):
        self.stdout = out


class _Missed(object):
    """A git call that RAN, LOOKED, and did not resolve the name -- rc 1.

    THE SIGN IS ONE BIT AND THE FACT IS TWO. MEASURED through `rev-parse
    --verify --quiet <x>^{commit}`: an absent-but-valid sha and a malformed
    name BOTH give rc 1 with empty stdout and empty stderr, while a broken or
    absent repository gives rc 128 with `fatal:` on stderr. Only rc 1 is git
    reporting on the OBJECT; 128 is git reporting on itself. `_Fail` above is
    the 128 face and this is the 1 face, kept as separate types so a fixture
    cannot silently stand in for the other."""

    returncode = 1

    def __init__(self, out=""):
        self.stdout = out


_EMPTY = object()   # a fixture saying "git looked and the range was empty"


@contextlib.contextmanager
def _hash_faces(projection):
    """Double BOTH hash faces from ONE description of the repository.

    `_patch_id` IS `_measured_patch_id(...)[1]`, so a fixture that replaces
    one and leaves the other real describes a module that cannot exist. The
    arm still passes -- it exercises the face it replaced -- while production
    reads the face it never mentioned, and the answer comes from a REAL git
    call against a fake gitdir. MEASURED when one consumer was routed to the
    richer face: FIFTEEN arms in this file doubled only the projection and
    reached that consumer, and only FOUR went red. The other eleven kept
    passing against a hash face they never meant to call, which is the same
    absence wearing a green suite.

    `projection` is the double as it was always written -- called with the
    gitdir and sha (and the trunk ref, if it takes one) and answering a
    patch-id, None for "git could not answer", or `_EMPTY` for "git answered
    and the range holds no diff". Both faces are then built from that one
    answer, so a fixture cannot make them disagree even by trying.

    THE DERIVATION RUNS THE OPPOSITE WAY FROM PRODUCTION AND THAT IS SAID
    OUT LOUD RATHER THAN GLOSSED. Production computes the rich answer and
    projects it; this builds the rich answer back up from the projection,
    which is only possible because the projection is lossy in exactly ONE
    place -- it cannot tell EMPTY_RANGE from UNMEASURED. `_EMPTY` is the
    spelling for the case it loses, and an arm that needs the distinction to
    be the SUBJECT rather than the setup doubles `_measured_patch_id`
    directly instead of coming through here."""
    import inspect
    try:
        takes = len(inspect.signature(projection).parameters)
    except (TypeError, ValueError):
        takes = 3

    def measured(gitdir, sha, trunk_ref=None):
        got = (projection(gitdir, sha, trunk_ref) if takes >= 3
               else projection(gitdir, sha))
        if got is _EMPTY:
            return landreq.EMPTY_RANGE, None
        return (landreq.HASHED, got) if got else (landreq.UNMEASURED, None)

    def fail_open(gitdir, sha, trunk_ref=None):
        return measured(gitdir, sha, trunk_ref)[1]

    with mock.patch.object(landreq, "_measured_patch_id", measured), \
            mock.patch.object(landreq, "_patch_id", fail_open):
        yield


@contextlib.contextmanager
def _tmp_rescue_store():
    """Point the durable landing store at a throwaway file. -> its path.

    THE STORE IS THE REAL ESTATE'S, and it now takes a LOCK beside itself, so
    an arm that patches only `pk.read_json` still opens a lock file in the
    operator's home and still lets a whole-file write escape the fixture.
    Redirect the PATH and both follow it.
    """
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "land-rescue.json")
    try:
        with mock.patch.object(landreq, "_rescue_cache_path",
                               lambda: path):
            yield path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class LandreqGitAmbientBudgetTest(unittest.TestCase):
    def test_an_expired_scope_refuses_a_warm_git_memo_hit(self):  # noqa: VACUOUS_ASSERTION — the first successful call is the must-hit control and spawn.call_count pins refusal before the warm hit
        answer = subprocess.CompletedProcess([], 0, "tip\n", "")
        with mock.patch.object(landreq.subprocess, "run", return_value=answer) as spawn, \
                projscope.scope():
            self.assertIs(landreq._git("/repo.git", "rev-parse", "HEAD"), answer)
            with projscope.scope(deadline=0):
                with self.assertRaises(projscope.Expired):
                    landreq._git("/repo.git", "rev-parse", "HEAD")
        self.assertEqual(spawn.call_count, 1)

    def test_ambient_timeout_raises_but_the_ordinary_five_second_timeout_does_not(self):  # noqa: VACUOUS_ASSERTION — the unscoped None control proves the same timeout remains ordinary before the ambient case raises
        planted = subprocess.TimeoutExpired("git", 5)
        with mock.patch.object(landreq.subprocess, "run", side_effect=planted):
            self.assertIsNone(landreq._git("/repo.git", "status"))
        with mock.patch.object(landreq.subprocess, "run", side_effect=planted), \
                projscope.scope(deadline=time.monotonic() + 1):
            with self.assertRaises(projscope.Expired):
                landreq._git("/repo.git", "status")


# Shared by reference with `tests/test_lr_outsider_approval.py`.
def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TipPatchIdBatchTest(LandReqBase):
    """THE PATCH-IDENTITY RUNG IS BOUGHT ONCE PER PROJECTION, NOT ONCE PER ROW.

    WHAT WAS MEASURED, and it is the whole reason this class exists. A four
    second `strace -c` on the live `helm web` process showed 44 vfork and 440
    execve — about eleven subprocess spawns a second, continuously, on a box
    whose per-toolcall hooks have a two second budget. Instrumenting the same
    projection at `landreq._git_spawn` named them: 663 spawns in one
    `/api/lr` build, of which 626 were `_object_view(... "show" ...)` plus
    `git patch-id` — TWO processes for every one of 313 rows, inside
    `_derive_landing_proof`'s patch-identity rung. That is the git-per-row
    shape task/1824 bounded for the inject hook, running here in the web
    process' own rebuild loop where nothing had bounded it.

    The cure is the shape `_prefetch_object_existence` already uses one door
    over: arm the tips at `project_raw`, buy them in a small constant of
    spawns on first need, answer every row out of that map.
    """

    def side_commits(self, n):
        """`n` DISTINCT commits that are not reachable from trunk.

        Off-trunk is the fixture's load-bearing half: a tip that IS an ancestor
        of trunk is settled by `_ancestry` and never reaches the rung these
        arms are about, so a fixture built on trunk would measure nothing and
        pass."""
        self.git("checkout", "-q", "-b", "patchid-side", self.a)
        tips = [self.commit("patchid-%d" % i, path="p%d" % i) for i in range(n)]
        self.git("checkout", "-q", self.main)
        self.assertEqual(len(set(tips)), n, "the fixture minted a tip twice")
        gitdir = self.gitdir()
        trunk = self.git("rev-parse", self.main)
        for tip in tips:
            self.assertNotEqual(
                landreq._ancestry(gitdir, tip, trunk), landreq.ANCESTOR,
                "a fixture tip IS on trunk, so ancestry settles it and the "
                "patch-identity rung is never reached")
        return gitdir, trunk, tips

    def rows_for(self, gitdir, tips):
        """Raw rows in the shape `_prefetch_tip_patch_ids` reads, each one in a
        state the projection genuinely asks git about."""
        rows = [{"repo_id": gitdir, "tip": tip, "status": "verdict",
                 "close_reason": "fix"} for tip in tips]
        for row in rows:
            self.assertTrue(landreq._will_observe_git(row),
                            "a fixture row will not ask git at all")
        return rows

    def spawn_census(self, gitdir, trunk, tips, arm=None):
        """Drive `_landing_proof` for every tip inside ONE projection scope and
        return (answers, the argv of every git spawn).

        THE SEAM IS `_git_spawn`, THE BARE PROCESS, and not `_git` — `_git` is
        memoised, so counting it would count questions and the subject here is
        PROCESSES. `_landed_index` is supplied because it is this rung's
        upstream INPUT and not its subject: an empty index sends the ladder to
        `git cherry` instead, which is a different rung with a different cost
        and would make both phases measure the same nothing."""
        spawned = []
        real = landreq._git_spawn

        def spy(gd, args, input_text, env=None):
            spawned.append(tuple(args))
            return real(gd, args, input_text, env)

        answers, cache = {}, {}
        with mock.patch.object(landreq, "_git_spawn", spy), \
                mock.patch.object(landreq, "_landed_index",
                                  lambda *a, **k: ({"f" * 40: "e" * 40}, None)), \
                projscope.scope():
            if arm is not None:
                # THE PRODUCTION WIRING, IN ORDER: `project_raw` arms, and the
                # first row that reaches `_git_observe` buys. Arming without
                # the buy would leave the batch unbought and measure the
                # per-row path under a name that says otherwise.
                landreq._prefetch_object_existence(arm, cache)
                landreq._prefetch_tip_patch_ids(arm, cache)
                landreq._prime_tip_patch_ids(gitdir, cache)
            for tip in tips:
                answers[tip] = landreq._landing_proof(gitdir, tip, trunk)
        return answers, spawned

    @staticmethod
    def renders(spawned):
        """Every `git show` spawn, batched or per-row."""
        return [a for a in spawned if "show" in a]

    def test_the_rung_spawns_TWO_PROCESSES_PER_ROW_without_the_batch(self):
        """PHASE ONE OF THE RED-THEN-GREEN, AND IT IS A MEASUREMENT.

        With nothing armed, every row buys its own render and its own hash.
        This is the arm that makes the next test a trap rather than a
        decoration: a green "no per-row spawn" over a path nothing walks is
        silent either way, and the number here is what says the path is real.
        """
        gitdir, trunk, tips = self.side_commits(6)
        answers, spawned = self.spawn_census(gitdir, trunk, tips)
        self.assertEqual(len(self.renders(spawned)), len(tips),
                         "the rung did not render once per row, so the storm "
                         "this lane cures is not on this path")
        self.assertEqual(len([a for a in spawned if a[:1] == ("patch-id",)]),
                         len(tips))
        self.assertEqual(sorted(set(answers.values())), ["absent"],
                         "the fixture rows did not reach a patch-identity "
                         "answer at all: %r" % (answers,))

    def test_the_armed_batch_answers_EVERY_row_in_a_small_CONSTANT(self):  # noqa: VACUOUS_ASSERTION — the empty list is the per-tip render, and the SAME observable is measured NON-empty and equal to len(tips) on the unarmed census inside this arm, unconditionally, before the zero is read
        """PHASE TWO. Same tips, same ladder, same seam — the prefetch armed.

        THREE CLAUSES, AND THE THIRD IS THE ONE A HALF-REGRESSION CANNOT PASS.
        A render count that merely DROPPED is satisfied by a batch carrying
        some tips while the rest quietly spawn for themselves; so the per-row
        door is measured SHUT (no render names a single tip), and the answers
        are asserted IDENTICAL to the ones phase one derived per row.

        LOAD-BEARING MUTATION: delete the `_prefetch_tip_patch_ids` call in
        `project_raw`, or return `{}` from `_patchid_map`.
          -> AssertionError: the rung rendered once per row
        """
        gitdir, trunk, tips = self.side_commits(6)
        rows = self.rows_for(gitdir, tips)
        per_row, unarmed = self.spawn_census(gitdir, trunk, tips)
        batched, spawned = self.spawn_census(gitdir, trunk, tips, arm=rows)

        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, IN THIS ARM, and it is
        # unconditional: the single-tip render this test measures ABSENT is
        # measured PRESENT on the unarmed run one line above. Without it the
        # emptiness below is satisfied by a census that recorded nothing.
        self.assertEqual(len([a for a in self.renders(unarmed) if len(a) == 4]),
                         len(tips),
                         "the unarmed run did not render per tip, so this "
                         "test's absence claim is about a path nothing walks")
        renders = self.renders(spawned)
        self.assertEqual(len(renders), 1,
                         "the rung rendered once per row: %d renders for %d "
                         "tips" % (len(renders), len(tips)))
        self.assertEqual(sorted(t for t in tips if t in renders[0]),
                         sorted(tips),
                         "the one render did not carry every tip, so the rows "
                         "it missed spawned for themselves")
        # THE PER-ROW DOOR IS SHUT, measured as the absence of any render
        # naming ONE tip — the exact shape a half-regression takes.
        self.assertEqual([a for a in renders if len(a) == 4], [],
                         "a tip was rendered on its own beside the batch")
        self.assertEqual(batched, per_row,
                         "the batch changed an answer the per-row ladder gave")

    def test_a_tip_this_repository_DOES_NOT_HAVE_cannot_kill_the_batch(self):  # noqa: VACUOUS_ASSERTION — the absent sha is asserted PRESENT in the armed list first, unconditionally, so its absence from the render is the filter emptying it and not an arming path that never carried it
        """`git show` IS ALL-OR-NOTHING: one unresolvable name exits 128 with
        `fatal: bad object` and the whole render is lost.

        MEASURED, and it is why the existence filter is in the batch rather
        than trusted to the rows: on the live board 345 of 1954 observable tips
        name objects the repository does not have — rows whose own per-row path
        never reaches git because `_git_observe` proves existence first. The
        first cut of this batch armed them, every render failed, and every row
        fell back to its two spawns while the code read as though it had
        batched.

        LOAD-BEARING MUTATION: drop the `_object_exists_batch` filter from
        `_tip_patch_ids_batch`.
          -> AssertionError: one absent tip killed the batch
        """
        gitdir, trunk, tips = self.side_commits(4)
        absent = "b" * 40
        self.assertNotEqual(
            self.spawn_census(gitdir, trunk, [absent])[0][absent],
            "patch-equivalent",
            "the fixture's absent sha resolves, so it arms nothing")
        rows = self.rows_for(gitdir, tips + [absent])
        clean, _ = self.spawn_census(gitdir, trunk, tips, arm=self.rows_for(gitdir, tips))
        # THE POSITIVE CONTROL: the absent tip really is ARMED, so the render
        # that does not name it is a render the FILTER emptied and not an
        # arming path that quietly dropped it upstream.
        armed = {}
        with projscope.scope():
            landreq._prefetch_tip_patch_ids(rows, armed)
        self.assertIn(absent, armed.get((landreq._PATCHID_PENDING, gitdir), []),
                      "the absent tip was never armed, so the render below "
                      "was never at risk and proves nothing")
        poisoned, spawned = self.spawn_census(gitdir, trunk, tips, arm=rows)
        renders = self.renders(spawned)
        self.assertEqual(len(renders), 1,
                         "one absent tip killed the batch: %d renders"
                         % len(renders))
        self.assertNotIn(absent, renders[0],
                         "the absent tip reached the render, which is the "
                         "call git refuses outright")
        self.assertEqual(poisoned, clean)

    def test_a_tip_the_DURABLE_PROOF_LEDGER_covers_is_not_armed(self):
        """The batch renders about 20 KB of diff per commit, so arming a tip
        whose answer is already kept buys megabytes for a question nobody asks:
        `_landing_proof` reads the kept ledger ABOVE the budget door and
        returns without deriving.

        MEASURED on the live board: 1954 observable tips, 1351 of them covered
        by the ledger, 603 left — and all 314 the projection actually asked
        about were inside those 603, none outside. Arming all 1954 rendered 32
        MB, which `_git`'s five second bound refuses outright, so the batch
        answered NOTHING and every row spawned for itself.

        LOAD-BEARING MUTATION: drop the `ledger.get(...)` clause from
        `_prefetch_tip_patch_ids`.
          -> AssertionError: a tip the ledger already answers was armed
        """
        gitdir, trunk, tips = self.side_commits(3)
        kept, rest = tips[0], tips[1:]
        rows = self.rows_for(gitdir, tips)
        cache = {}
        with projscope.scope():
            landreq._keep_landing_proof(gitdir, kept, trunk, "patch-equivalent")
            self.assertEqual(landreq._kept_landing_proof(gitdir, kept, trunk),
                             "patch-equivalent",
                             "the fixture did not actually keep a proof, so "
                             "the absence below would prove nothing")
            landreq._prefetch_tip_patch_ids(rows, cache)
            armed = cache.get((landreq._PATCHID_PENDING, gitdir), [])
        self.assertEqual(sorted(armed), sorted(rest),
                         "a tip the ledger already answers was armed")

    def test_project_raw_ARMS_AND_BUYS_the_batch_for_the_rows_it_projects(self):
        """THE PRODUCTION DOOR, BOTH HALVES, because arming and buying are two
        load-bearing lines in two different functions and a unit test of the
        helpers can see NEITHER removed.

        MEASURED WHILE WRITING THIS: with only the arming half driven here,
        deleting `_prime_tip_patch_ids(gitdir, cache)` from `_git_observe` left
        every arm in this class GREEN — the census helper primed by hand, so it
        measured a wiring the projection no longer had. That is
        `_prefetch_object_existence`'s own history repeating: its batching claim
        held in ten arms while NOT ONE of them drove `project_raw`, so a
        regression to per-sha resolution would have kept every one green.

        LOAD-BEARING MUTATION: delete either call — the arm in `project_raw` or
        the buy in `_git_observe`.
          -> AssertionError: project_raw did not arm ... / did not buy ..."""
        self.git("checkout", "-q", "-b", "armed-side", self.a)
        tip = self.commit("armed", path="q")
        self.git("checkout", "-q", self.main)
        row = self.dispatch(lane="lane/armed", ref=tip)
        dispatches._mark_delivered(row["id"], "armed-note")
        self.mark_verdict(row["id"], tip, "ok", polarity="approve")
        seen, bought = [], []
        real = landreq._prefetch_tip_patch_ids
        real_batch = landreq._tip_patch_ids_batch

        def spy(rows, cache):
            rows = list(rows or ())
            seen.append([(r or {}).get("tip") for r in rows])
            return real(rows, cache)

        def spy_batch(gitdir, cache):
            armed = list(cache.get((landreq._PATCHID_PENDING, gitdir), ()))
            got = real_batch(gitdir, cache)
            bought.append((gitdir, armed, got))
            return got

        with mock.patch.object(landreq, "_prefetch_tip_patch_ids", spy), \
                mock.patch.object(landreq, "_tip_patch_ids_batch", spy_batch):
            out, _raw, unavailable = landreq.project_raw()
        self.assertIsNone(unavailable, unavailable)
        self.assertIn(row["id"], out,
                      "the row did not project, so nothing here is about it")
        self.assertTrue(out[row["id"]]["observable"],
                        "the row came out UNOBSERVABLE, so it never reached "
                        "`_git_observe` and neither claim below is about it")
        self.assertEqual(len(seen), 1,
                         "project_raw did not arm the patch-id batch exactly "
                         "once: %d calls" % len(seen))
        self.assertIn(tip, seen[0],
                      "project_raw armed the batch without the tip it is "
                      "about to ask about")
        # AND THE BUY, which is the half a hand-primed census cannot see.
        self.assertEqual(len(bought), 1,
                         "project_raw did not buy the armed batch exactly "
                         "once: %d buys" % len(bought))
        self.assertEqual(bought[0][0], self.gitdir())
        self.assertIn(tip, bought[0][1],
                      "the buy did not carry the armed tip")
        self.assertIsInstance(bought[0][2], dict)


class ListReadRunsInsideOneScope(ReceiptBase):
    """`helm lr list` is ONE read, so it asks git each question once.

    A scope that starts in the MIDDLE of the read covers neither end of it.
    `project_raw` enters `projscope.scope()` around its row loop, so the
    ledger fold ABOVE that scope and the per-row render BELOW it — which
    reaches a second fold through `independent_review` ->
    `chain_contributor_index` -> `_ledger_fold` — each buy their own answers
    to questions the projection has already paid for unless something wider
    holds them. Measured on the live ledger pinned to a frozen record with no
    scope on the verb: two folds per listing, NEITHER of them inside one.
    """

    def _row_that_prints(self):
        row = self.dispatch(ref=self.side)
        self.mark_verdict(row["id"], self.side, "ok", polarity="approve")
        return row

    def test_both_ends_of_a_listing_run_inside_a_scope(self):
        """THE HEAD FOLD AND THE TAIL RENDER, which are the two ends the old
        scope sat between.

        `dispatches.snapshot_and_events` is the fold `project_raw` performs
        BEFORE entering its own scope; `landreq._line` is the per-row render
        the listing reaches AFTER that scope has dropped, and it is where the
        second fold hangs. Reading `projscope.active()` at both says the whole
        listing is one memo window rather than three.
        """
        row = self._row_that_prints()
        head, tail = [], []
        real_fold = dispatches.snapshot_and_events
        real_line = landreq._line

        def fold_spy(*a, **kw):
            head.append(projscope.active())
            return real_fold(*a, **kw)

        def line_spy(*a, **kw):
            tail.append(projscope.active())
            return real_line(*a, **kw)

        with mock.patch.object(dispatches, "snapshot_and_events", fold_spy), \
                mock.patch.object(landreq, "_line", line_spy):
            rc, out, err = run(["list", "--cold"])
        # THE MUST-HIT FIRST: a spy that never fired would make every claim
        # below true about nothing at all.
        self.assertEqual(rc, 0, out + err)
        self.assertIn(row["id"][:12], out,
                      "the row never printed, so the tail spy saw no render "
                      "and this test is about an empty board")
        self.assertTrue(head, "snapshot_and_events was never called")
        self.assertTrue(tail, "_line was never called")
        self.assertEqual([True] * len(head), head,
                         "the ledger fold at the head of the listing ran "
                         "OUTSIDE a scope %d time(s)" % head.count(False))
        self.assertEqual([True] * len(tail), tail,
                         "the per-row render at the tail of the listing ran "
                         "OUTSIDE a scope %d time(s)" % tail.count(False))
        # THE SAME OBSERVABLE, BOTH POLES, IN THIS METHOD. `projscope.active()`
        # returning True everywhere would satisfy the assertions above whether
        # or not the listing opens anything, so this reads it where the answer
        # must be False and where it must be True.
        self.assertFalse(projscope.active(),
                         "a scope outlived the listing that opened it")
        with projscope.scope():
            self.assertTrue(projscope.active())
        self.assertFalse(projscope.active())

    def test_the_listing_is_scoped_and_a_write_verb_is_handed_no_scope(self):
        """THE CONTROL, and it is why the scope keys on `_SCOPED_READ_VERBS`
        rather than simply wrapping the dispatch.

        Every verb of `helm lr` comes through one dispatch and several of them
        WRITE. `projscope`'s module docstring makes "outside a scope nothing
        is cached" the property the land door is safe by, so a memo stretched
        over the function would let a land be authorised by a question asked
        before it started. Both poles of one observable ride one door apiece
        here: `project_raw` — the listing's projection — is now entered with a
        scope already open, and `_cmd_land` is entered with none.

        THE WRITE VERB'S OWN READ IS NOT WHAT THIS CLAIMS. `_cmd_land`
        projects, and `project_raw` opens its own scope around that
        projection, so SOME git under a land has always been memoised and
        still is. The property is that nothing ABOVE the write verb opens one
        for it, which is what this reads at the verb's own door.
        """
        row = self._row_that_prints()
        self.land_side()
        read_door, write_door = [], []
        real_project, real_land = landreq.project_raw, landreq._cmd_land

        phase = {"verb": None}

        def project_spy(*a, **kw):
            # ONLY THE LISTING'S PROJECTION. `_cmd_land` projects too, and its
            # read is deliberately NOT the subject here — recording it would
            # make this arm fail on the very behaviour the docstring above
            # says is unchanged.
            if phase["verb"] == "list":
                read_door.append(projscope.active())
            return real_project(*a, **kw)

        def land_spy(*a, **kw):
            write_door.append(projscope.active())
            return real_land(*a, **kw)

        with mock.patch.object(landreq, "project_raw", project_spy), \
                mock.patch.object(landreq, "_cmd_land", land_spy):
            phase["verb"] = "list"
            rc, out, err = run(["list", "--cold"])
            self.assertEqual(rc, 0, out + err)
            phase["verb"] = "land"
            with self.signed():
                rc, out, err = run(["land", row["id"]])
            self.assertEqual(rc, 0, out + err)
            self.assertIn("receipt recorded", out,
                          "the land verb did not reach its write, so this "
                          "says nothing about a write path")
            phase["verb"] = None
        # THE MUST-HIT ON BOTH DOORS: an empty list is a claim about nothing,
        # and the read door is the positive control proving this observable
        # can read True at all.
        self.assertTrue(read_door, "the listing never reached project_raw")
        self.assertTrue(write_door, "the land verb never reached _cmd_land, "
                        "so nothing here is about a write path")
        self.assertEqual([True] * len(read_door), read_door,
                         "the listing's own projection ran OUTSIDE the "
                         "listing's scope %d time(s)" % read_door.count(False))
        self.assertEqual([False] * len(write_door), write_door,
                         "`cmd_lr` handed a WRITE verb an OPEN memo scope %d "
                         "time(s) — a land may never be answered from a "
                         "question asked before it started"
                         % write_door.count(True))

    def test_the_census_keeps_its_own_derive_window(self):  # noqa: VACUOUS_ASSERTION — the positive control on the SAME
    # predicate is `spent`: a deadline seeded in the past, read through
    # `_derive_expired` and asserted True unconditionally below, which
    # the rung cannot see because the read happens inside the `with`
    # that makes it possible at all.
        """A SCOPE IS AN OPERATION AND NEVER A CLOCK.

        The derive budget rides `projscope.memo` under one key and
        `arm_derive_budget` is first-seeder-wins, so one memo window over the
        whole listing handed `filed_split`'s walk a deadline the row loop had
        seeded and spent long before. Measured board against board on a frozen
        record, that moved a row out of OFF-FRONTIER and into UNCLASSIFIED.
        So the listing evicts the seed and arms the board's own number for
        everything after the projection, and what `filed_split` reads is a
        deadline in ITS future rather than the row loop's past.
        """
        self._row_that_prints()
        seen = []
        real_split = landreq.filed_split

        def split_spy(*a, **kw):
            seen.append(projscope.remaining())
            seen.append(landreq._derive_expired())
            return real_split(*a, **kw)

        with mock.patch.object(landreq, "filed_split", split_spy):
            rc, out, err = run(["list", "--cold"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("filed", out,
                      "the census line never rendered, so the walk this arm "
                      "is about did not reach the board")
        self.assertTrue(seen, "filed_split was never called")
        # THE POSITIVE CONTROL FIRST, on the same predicate and unconditional:
        # a deadline already behind us must read EXPIRED, or the claim below
        # is true of a predicate that never says True at all.
        with projscope.scope():
            landreq.arm_derive_budget(-1.0)
            spent = landreq._derive_expired()
        self.assertIs(spent, True,
                      "_derive_expired never answers True, so it cannot "
                      "witness a spent budget for the census either")
        self.assertIs(seen[1], False,
                      "the census walk began on a SPENT derive budget — it is "
                      "reading the row loop's window, not its own")
