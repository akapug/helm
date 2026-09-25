#!/usr/bin/env python3
"""ONE identity layer: what a NAME may do, and what needs an ADMITTED ACTOR.

task/994. The predecessor on this lane added one admission helper and migrated
six call sites; six bypasses were measured AROUND it and its own
suite exercised the HELPER instead of the production writers, so every bypass
stayed green. This suite is built the opposite way round.

THREE RULES IT ENFORCES ON ITSELF, each from a measured failure of the last
attempt:

  1. INVOKE THE REAL WRITER, never the door. Every arm below drives a
     production verb — `chat.cmd_chat`, `work.cmd_work`, `seats.dm`,
     `seats.ack`, `seats.deliver`, `_finalize_work_offer` — not the resolver
     they happen to share.

  2. ASSERT THE STORE, not the return code. A verb that refuses with rc 2 while
     still appending passes an rc assertion and fails the actual contract, so
     every refusal arm hashes the CONTENT of every file under every store root
     before and after. The roots are derived FROM THE PRODUCTION ACCESSORS
     (`actors.store_path`, `seats.roster_path`, `seats.claims_path`,
     `chat.chat_dir`) rather than from an env var this file guesses at: the
     predecessor's instrument walked HELM_HOME while its own positive control
     wrote `.roster.json` under HELM_CHAT_DIR — a SIBLING — so the anti-vacuity
     control was itself vacuous and said so, out loud, at 0 not greater than 0.

  3. EVERY ABSENCE ARM CARRIES A POSITIVE CONTROL ON THE SAME OBSERVABLE, in
     the same arm, through the same accessor. An arm that proves "nothing was
     written" while the instrument was blind proves nothing at all.

AND THE THING THE PREDECESSOR COULD NOT DO AT ALL: a census
(`UsesClosureTest`) that walks the AST of every module in helm/ and requires
each call to a bare string resolver to be REGISTERED with a classification. A
new call site cannot appear without somebody deciding whether it renders,
addresses, or crosses an actuator. Enumerating call sites by hand is what
failed; enumerating them mechanically and refusing an unclassified one is the
closure.
"""
import ast
import contextlib
import errno
import hashlib
import io
import json
import os
from unittest import mock
import time
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
from tests import _tmphome  # noqa: E402
_tmp_home(prefix="helm-test-idlayer-", var="HELM_HOME")

try:
    from helm import actors  # noqa: E402
except ImportError as _no_layer:            # pragma: no cover — trunk only
    # COLLECTABLE IN A TREE WHERE ITS SUBJECT IS ABSENT. The gate's base check
    # replays this lane's tests against TRUNK to prove a failure is not already
    # trunk's — and a bare `from helm import actors` made that reference run
    # die on `unittest.loader._FailedTest`, so the base check came back UNKNOWN
    # and could attribute nothing. A module-level SkipTest is the honest answer:
    # this file's subject does not exist there, so there is nothing to run, and
    # saying so is different from failing to load.
    raise unittest.SkipTest("helm.actors is absent — the identity layer this "
                            "suite is about does not exist in this tree "
                            "(expected on trunk, before the lane lands)")

from helm import beacons, chat, home, pk, seats, seats_rename  # noqa: E402
from helm.seats_identity import resolve_identity  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL",
            "HELM_ACTORS", "MELD_ACTORS", "HELM_CHAT_NAME", "HELM_CHAT_DELIVER",
            "HELM_SCRATCH_GC",
            "HELM_CACHE_DIR", "HELM_CHAT_LOG", "MELD_CHAT_DIR",
            "HELM_CHAT_ROOM",   # set in setUp — tests/test_env_hygiene checks
            "HELM_STOP_GUARD", "HELM_STOP_GUARD_WHISPER",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

# Synthetic only — tests/ is public-bound and a real seat name in a suite ABOUT
# identity leakage is refused by the seat-name guard, as it should be.
A, B, C = "seat-a", "seat-b", "seat-c"
SID = "sid-aaaa-bbbb"


class LayerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-idlayer-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""     # no signed transport
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)
        chat._ensure_dir()

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------------
    # the durable-mutation instrument
    # ------------------------------------------------------------------
    def _roots(self):
        """Every directory a writer under test actually uses, named by the
        PRODUCTION ACCESSOR rather than by an env var this file guesses.

        This is the whole fix for the predecessor's vacuous control. Its
        instrument walked `os.environ["HELM_HOME"]`; `seats.roster_path()` is
        `chat.chat_dir()/.roster.json`, and the fixture pointed HELM_CHAT_DIR
        at a SIBLING of HELM_HOME — so a real roster write moved nothing the
        counter could see, and every zero-mutation assertion beside it was
        true for the wrong reason."""
        return sorted({os.path.dirname(actors.store_path()),
                       os.path.dirname(seats.roster_path()),
                       os.path.dirname(seats.claims_path()),
                       chat.chat_dir(),
                       os.environ["HELM_HOME"]})

    def snapshot(self):
        """{path: sha256(content)} across every root.

        CONTENT, not size or mtime: an event-sourced store hides a rejected
        write from a value assertion, and a same-length in-place edit hides
        from a byte count. Hashing the bytes catches an appended line, a
        rewritten row and a truncation alike."""
        out = {}
        for root in self._roots():
            for base, _dirs, files in os.walk(root):
                for f in files:
                    p = os.path.join(base, f)
                    try:
                        with open(p, "rb") as fh:
                            out[p] = hashlib.sha256(fh.read()).hexdigest()
                    except OSError:
                        out[p] = "unreadable"
        return out

    def assertUnmoved(self, before, why):
        after = self.snapshot()
        moved = {p for p in set(before) | set(after)
                 if before.get(p) != after.get(p)}
        self.assertEqual(moved, set(), "%s — these files moved: %s"
                         % (why, sorted(moved)))

    def assertMoved(self, before, why, counting_actor_store=False):
        """A positive control must prove THE WRITER wrote, not that SOMETHING
        did. Every successful `resolve_actor` stamps the actor store, so a
        naive before != after would go green on the resolution alone — the
        measurement bound to the neighbouring claim. The actor store is
        excluded unless an arm is deliberately measuring it."""
        after = self.snapshot()
        store = actors.store_path()
        moved = {p for p in set(before) | set(after)
                 if before.get(p) != after.get(p)
                 and (counting_actor_store or p != store)}
        self.assertNotEqual(moved, set(), why)

    def assertWrote(self, before, path, why):
        """The narrowest form: THIS accessor's file changed."""
        after = self.snapshot()
        self.assertNotEqual(before.get(path), after.get(path),
                            "%s (%s)" % (why, path))

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def declare(self, seat, corroborate=True):
        """Declare `seat`, and by default CORROBORATE it the way a join does.

        A declared name alone is not an identity — an ACT door requires a
        session the roster resolves back to the same seat. Almost every arm
        here declares as SCAFFOLDING, to obtain an actor so it can test
        something else, and those arms want a seat that can act.

        `corroborate=False` is for the arms whose SUBJECT is the corroboration
        itself: they need a declared name with nothing behind it, which is the
        state this fixture otherwise exists to prevent."""
        os.environ["HELM_CHAT_NAME"] = seat
        if corroborate:
            sid = "sess-declared-%s" % seat.casefold()
            os.environ["CLAUDE_CODE_SESSION_ID"] = sid
            self.roster(seat, sid)

    def roster(self, seat, session):
        seats.write_roster(seat, session=session, cwd=self.tmp)

    def session(self, sid=SID):
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        return sid

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chat.cmd_chat(list(args))
        return rc, out.getvalue(), err.getvalue()


class InstrumentTest(LayerBase):
    """THE MUST-HIT CONTROLS. If any of these fails, every absence assertion in
    this file is vacuous — which is exactly what happened to the predecessor,
    and its control is the reason we know."""

    def test_the_instrument_sees_a_write_through_EVERY_accessor(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the whole file; its assertion is assertWrote, a presence check, not an absence
        for label, path, write in (
                ("roster", seats.roster_path(),
                 lambda: seats.write_roster(A, session=SID, cwd=self.tmp)),
                ("claims", seats.claims_path(),
                 lambda: seats.claim("res:probe", A, ttl=60)),
                ("chat room", chat.room_path("main"),
                 lambda: chat.post("probe", "main", who=B)),
                ("actor store", actors.store_path(),
                 lambda: actors.bind(C, (("declared", "x"),))[0]),
        ):
            before = self.snapshot()
            write()
            self.assertWrote(before, path,
                             "the instrument cannot see a %s write — every "
                             "absence arm in this file would be vacuous"
                             % label)

    def test_the_roster_lives_OUTSIDE_helm_home(self):  # noqa: VACUOUS_ASSERTION — a GEOMETRY pin, not an observable — assertIn on _roots() is its own positive half
        """The exact geometry that made the predecessor's control vacuous,
        pinned so a future instrument cannot forget it."""
        self.assertFalse(
            seats.roster_path().startswith(os.environ["HELM_HOME"] + os.sep),
            "the roster is under the CHAT dir, a sibling of HELM_HOME — an "
            "instrument that walks only HELM_HOME cannot see a roster write")
        self.assertIn(os.path.dirname(seats.roster_path()), self._roots())


class ResolveActorTest(LayerBase):
    """The four refusals, in the order the resolver applies them."""

    def test_DERIVED_is_refused(self):
        actor, err = actors.resolve_actor("sid-nobody", self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("DERIVED", err)

    def test_DECLARED_alone_is_NOT_admitted_it_needs_its_roster_row(self):
        """A DECLARED NAME IS A VALUE ANY PROCESS CAN EXPORT.

        This arm asserted the opposite, and asserting it is what made the hole
        a contract rather than a gap: a declared name plus a session no roster
        had ever seen was admitted to ACT. The name becomes evidence only once
        the roster resolves that session to the SAME seat, so the admission
        moved behind `roster()` and the bare case pins the refusal.

        BOTH HALVES STAY. Without the second the cure could be "refuse every
        declared identity", which passes the first half and takes the fleet
        down."""
        _a0, err0 = actors.resolve_actor("sid-nobody", self.tmp, act="claim")
        self.assertTrue(err0, "control: this call shape must be able to refuse")
        self.declare(A, corroborate=False)
        actor, err = actors.resolve_actor("sid-nobody", self.tmp, act="claim")
        self.assertIsNone(actor, "a bare declared name was admitted to act")
        self.assertIn("nothing corroborates it", err)

        self.roster(A, SID)
        actor, err = actors.resolve_actor(SID, self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)

    def test_ROSTERED_is_admitted(self):
        _a0, err0 = actors.resolve_actor("sid-bound", self.tmp, act="claim")
        self.assertTrue(err0, "control: unbound refuses before the roster write")
        self.roster(B, "sid-bound")
        actor, err = actors.resolve_actor("sid-bound", self.tmp, act="claim")
        self.assertIsNone(err)
        self.assertEqual(actor.canonical_name, B)

    def test_DISAGREEMENT_is_refused_which_the_predecessor_omitted(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the assertIsNotNone on identity_disagreement above: it proves the fixture really produced a dispute, so the refusal below discriminates
        """The 2026-08-02 P0, reintroduced by its own fix: the first attempt's
        door never called `identity_disagreement`, which lives in the same
        file, so a declared name plus a roster row naming somebody else was
        admitted as DECLARED."""
        self.roster(B, "sid-bound")
        self.declare(A)                      # env says A, roster says B
        self.assertIsNotNone(seats.identity_disagreement("sid-bound"),
                             "fixture must actually produce a dispute")
        actor, err = actors.resolve_actor("sid-bound", self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("disputed", err)

    def test_MALFORMED_is_refused_not_downgraded_to_derived(self):
        """`own_name()` swallows SeatNameError to None, which falls through to
        the DERIVED floor and MINTS a name — a hostile env var quietly became
        an anonymous actor instead of an error."""
        os.environ["HELM_CHAT_NAME"] = "seat\x1b[2Ja"
        actor, err = actors.resolve_actor("sid-x", self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("not a legitimate seat identifier", err)

    def test_an_assertion_cannot_RESCUE_a_derived_identity(self):
        _state, derived = resolve_identity("sid-nobody", self.tmp)
        actor, err = actors.resolve_actor("sid-nobody", self.tmp,
                                          asserted=derived, act="claim")
        self.assertIsNone(actor)
        self.assertIn("DERIVED", err,
                      "the DERIVED refusal must precede the assertion, or "
                      "asserting the minted name rescues it")

    def test_seat_asserts_and_never_selects(self):
        self.declare(A)
        self.roster(A, "sid-x")  # declared names need their roster row
        actor, err = actors.resolve_actor("sid-x", self.tmp, asserted=B,
                                          act="claim")
        self.assertIsNone(actor)
        self.assertIn("never selects", err)
        actor, err = actors.resolve_actor("sid-x", self.tmp, asserted="SEAT-A",
                                          act="claim")
        self.assertIsNone(err, "the assertion is casefold-equivalent")
        self.assertEqual(actor.canonical_name, A)


class CapabilityIsOpaqueTest(LayerBase):
    """A capability that behaved like its own name would be silently
    substitutable for the string it replaces — the substitution the type
    exists to make impossible."""

    def actor(self, seat=A):
        self.declare(seat)
        # THIS HELPER NAMES ITS OWN SESSION, so it rosters that one too: the
        # sid `declare` binds is not the sid this call presents, and a roster
        # row for a session nobody passes corroborates nothing.
        self.roster(seat, "sid-x")
        a, err = actors.resolve_actor("sid-x", self.tmp, act="act")
        self.assertIsNone(err)
        return a

    @staticmethod
    def legacy_journal(source=A, target=B, **evidence):
        old, new = seats._seat_key(source), seats._seat_key(target)
        row = {"v": 1, "old": old, "new": new,
               "old_lane": chat.DM_PREFIX + old,
               "new_lane": chat.DM_PREFIX + new,
               "moves": [], "receipt_ids": []}
        row.update(evidence)
        return row

    def test_a_raw_string_cannot_satisfy_the_gate(self):
        name, err = actors.actor_name(A, act="claim a lease")
        self.assertIsNone(name)
        self.assertIn("raw seat name", err)
        name, err = actors.actor_name(self.actor(), act="claim a lease")
        self.assertEqual(name, A, "control: the gate must admit a capability")

    def test_the_capability_CANNOT_BE_CONSTRUCTED_DIRECTLY(self):  # noqa: VACUOUS_ASSERTION — the second half is an unconditional positive control: the real mint returns a working capability in the same arm
        """EVERYTHING ELSE ABOUT THIS TYPE IS WALKED AROUND BY CALLING IT.

        Read-only fields, a tagged `__str__`, never `==` a string, and
        `actor_name()` refusing a raw string BY TYPE are each real — and each
        of them is defeated by `AdmittedActor('not-an-id', 'seat-a')`, which
        mints authority without passing the admission that makes the type mean
        anything. The class docstring claimed "no public constructor
        contract"; that was a guarantee's form without its substance."""
        # REFLECTIVELY, on purpose. The construction census below forbids a
        # SOURCE-VISIBLE `AdmittedActor(...)` call anywhere outside the layer,
        # and this arm must not be the one exception that hollows it out — a
        # deliberate forgery attempt should read as deliberate.
        forge = getattr(actors, "AdmittedActor")
        with self.assertRaises(actors.ActorRefused) as caught:
            forge("not-an-id", A)
        self.assertIn("cannot be constructed", str(caught.exception))
        # POSITIVE CONTROL on the same constructor: the LAYER's own mint still
        # produces a working capability, so the refusal above discriminates
        # rather than breaking the type.
        self.declare(A)
        self.roster(A, "sid-x")  # declared names need their roster row
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)

    def test_a_stale_proof_cannot_be_constructed_directly_either(self):  # noqa: VACUOUS_ASSERTION — the prove_stale call at the end is an unconditional positive control on the same constructor
        """Same law, same reason: `prove_stale` is documented as the ONLY
        mint, and a directly-constructed proof would let an "unknown" liveness
        verdict release a live holder's lease."""
        forge = getattr(actors, "StaleReleaseProof")   # see the arm above
        with self.assertRaises(actors.ActorRefused):
            forge("res:x", B, "invented")
        proof, err = actors.prove_stale("res:x", B, "stale", "holder is dead")
        self.assertIsNone(err)
        self.assertEqual(proof.holder, B)

    def test_it_does_not_stringify_to_its_own_name(self):
        a = self.actor()
        self.assertNotEqual(str(a), A)
        self.assertIn("AdmittedActor", str(a))

    def test_it_is_never_equal_to_a_string(self):  # noqa: VACUOUS_ASSERTION — self.actor() asserts err is None first, so the capability provably exists before the inequality is read
        self.assertNotEqual(self.actor(), A)

    def test_it_is_immutable(self):  # noqa: VACUOUS_ASSERTION — self.actor() asserts err is None first; assertRaises is a presence assertion about the raise
        a = self.actor()
        with self.assertRaises(AttributeError):
            a._canonical_name = B

    def test_lineage_never_vanishes_which_the_roster_cannot_express(self):
        """`seats_roster.rename` is `r[new] = r.pop(seat)` — a dict key swap
        with no lineage, which is why a renamed seat's beacon keeps listening
        on a name the roster no longer holds."""
        a = self.actor()
        ok, msg = actors.rename(A, B)
        self.assertTrue(ok, msg)
        row = actors.lookup(A)
        self.assertIsNotNone(row, "the OLD name must still resolve")
        self.assertEqual(row["actor_id"], a.actor_id, "same actor, new label")
        self.assertEqual(row["canonical_name"], B)
        self.assertIn(A, row["aliases"])

    def test_the_record_is_STABLE_across_repeated_resolutions(self):  # noqa: VACUOUS_ASSERTION — the second half is an unconditional positive control asserting the store DOES move for a new actor
        """`resolve_actor` runs at every delivery boundary. A record that
        rewrote itself on each call would be a log, and would turn one identity
        question into a JSON write per tool call."""
        self.actor(A)
        first = self.snapshot().get(actors.store_path())
        for _ in range(10):
            actors.resolve_actor("sid-x", self.tmp)
        self.assertEqual(self.snapshot().get(actors.store_path()), first,
                         "the actor record churns on every resolution")
        self.declare(B)
        actor, err = actors.resolve_actor(os.environ["CLAUDE_CODE_SESSION_ID"],
                                          self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, B)
        self.assertNotEqual(self.snapshot().get(actors.store_path()), first,
                            "control: a NEW actor must still be recorded")

    def test_rename_advances_only_the_committed_actor_revision(self):  # noqa: ORPHANED_MOCK — idempotent rename intentionally never probes store writability
        before = self.actor()
        self.assertEqual(before.version, 1)
        ok, msg = actors.rename(A, B)
        self.assertTrue(ok, msg)
        row = actors.lookup(A)
        self.assertEqual(row["actor_id"], before.actor_id)
        self.assertEqual(row["revision"], 2)
        self.assertIn(A, row["aliases"])
        self.assertEqual(before.version, 1, "the older capability stays immutable")
        self.declare(B, corroborate=False)
        self.roster(B, "sid-b")
        current, err = actors.resolve_actor("sid-b", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(current.actor_id, before.actor_id)
        self.assertEqual(current.version, 2)
        committed = self.snapshot().get(actors.store_path())
        with mock.patch.object(actors, "_store_writable", return_value=False):
            ok, msg = actors.rename(A, B)
        self.assertTrue(ok, msg)
        actors.resolve_actor("sid-b", self.tmp)
        self.assertEqual(self.snapshot().get(actors.store_path()), committed,
                         "retries and resolutions must not churn the store")
        ok, msg = actors.rename(B, C)
        self.assertTrue(ok, msg)
        self.assertEqual(actors.lookup(A)["revision"], 3)

    def test_idempotent_rename_requires_the_target_index(self):
        self.actor()
        ok, msg = actors.rename(A, B)
        self.assertTrue(ok, msg)
        d, unavailable = actors.read_store()
        self.assertIsNone(unavailable, unavailable)
        d["by_name"].pop(B.casefold())
        actors.pk.write_json(actors.store_path(), d)
        damaged = self.snapshot().get(actors.store_path())

        ok, msg = actors.rename(A, B)
        self.assertTrue(ok, msg)
        repaired = actors.lookup(B)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["revision"], 3)
        self.assertNotEqual(self.snapshot().get(actors.store_path()), damaged)

    def test_concurrent_rename_and_bind_cannot_lose_either_record(self):
        self.actor(A)
        arrived, attempting, second_read, release = (
            threading.Event() for _ in range(4))
        actual_read, actual_lock = actors.read_store, actors._store_lock
        outcome = {}

        def read():
            state = actual_read()
            if threading.current_thread().name == "rename-writer":
                arrived.set()
                if not release.wait(3):
                    raise TimeoutError("test never released first writer")
            elif threading.current_thread().name == "bind-writer":
                if attempting.is_set():
                    second_read.set()
            return state

        @contextlib.contextmanager
        def lock():
            if threading.current_thread().name == "bind-writer":
                attempting.set()  # after the unlocked probe, before acquisition
            with actual_lock():
                yield

        def rename():
            outcome["rename"] = actors.rename(A, B)

        def bind():
            outcome["bind"] = actors.bind(C)

        with mock.patch.object(actors, "read_store", side_effect=read), \
                mock.patch.object(actors, "_store_lock", side_effect=lock):
            first = threading.Thread(target=rename, name="rename-writer")
            second = threading.Thread(target=bind, name="bind-writer")
            first.start()
            try:
                self.assertTrue(arrived.wait(3), "positive control: rename read")
                second.start()
                self.assertTrue(attempting.wait(3),
                                "positive control: bind reached the writer lock")
                self.assertFalse(second_read.wait(.2),
                                 "bind read a stale snapshot while rename held the lock")
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_read.is_set(),
                        "positive control: bind eventually read inside the lock")
        self.assertTrue(outcome["rename"][0], outcome["rename"])
        self.assertIsNone(outcome["bind"][1], outcome["bind"])
        self.assertEqual(actors.lookup(A)["revision"], 2)
        self.assertEqual(actors.lookup(B)["actor_id"], actors.lookup(A)["actor_id"])
        self.assertEqual(actors.lookup(C)["revision"], 1)

    def test_failed_first_bind_refuses_uncommitted_admission(self):
        self.declare(A)
        self.roster(A, "sid-x")
        original = actors.pk.write_json
        with mock.patch.object(actors.pk, "write_json", side_effect=OSError("full")):
            actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(actor)
        self.assertIn("not writable", err)
        self.assertIsNone(actors.lookup(A), "positive control: no actor committed")
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(err, err)
        self.assertIsNotNone(actor, "positive control: persistence can succeed")
        self.assertEqual(actor.version, 1)
        self.assertIs(actors.pk.write_json, original)

    def test_known_canonical_and_alias_binds_never_probe_rename_journal(self):
        known, err = actors.bind(A)
        self.assertIsNone(err, err)
        with mock.patch.object(actors, "_store_lock",
                               side_effect=AssertionError("known bind locked")), \
                mock.patch.object(
                    actors, "_split_configuration_refusal",
                    side_effect=AssertionError("known bind read split guard")), \
                mock.patch.object(
                    seats_rename, "pending_rename_admission_refusal",
                    side_effect=AssertionError("known bind read journal")):
            again, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(again["actor_id"], known["actor_id"])

        ok, note = actors.rename(A, B)
        self.assertTrue(ok, note)
        with mock.patch.object(
                actors, "_split_configuration_refusal",
                side_effect=AssertionError("known alias read split guard")), \
                mock.patch.object(
                    seats_rename, "pending_rename_admission_refusal",
                    side_effect=AssertionError("known alias read journal")):
            alias, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(alias["actor_id"], known["actor_id"])
        self.assertEqual(alias["canonical_name"], B)

    def test_first_admission_reads_pending_rename_under_actor_lock(self):  # noqa: VACUOUS_ASSERTION — returned Actor and observed held lock are positive controls
        actual_lock = actors._store_lock
        held = []

        @contextlib.contextmanager
        def observed_lock():
            with actual_lock():
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

        def observed_probe(name):
            self.assertEqual(name, A)
            self.assertEqual(held, [True],
                             "rename journal was read outside the Actor lock")
            return None

        with mock.patch.object(actors, "_store_lock",
                               side_effect=observed_lock), \
                mock.patch.object(
                    seats_rename, "pending_rename_admission_refusal",
                    side_effect=observed_probe):
            actor, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(actor["canonical_name"], A)

    def test_parseable_key_only_legacy_journal_fences_source_and_target(self):  # noqa: VACUOUS_ASSERTION — unrelated and post-clear admissions are positive controls
        pk.write_json(seats_rename.rename_journal_path(), self.legacy_journal())

        for name, role in ((A, "source name"), (B, "target name")):
            actor, err = actors.bind(name)
            self.assertIsNone(actor)
            self.assertIn("pending seat rename", err)
            self.assertIn(role, err)
            self.assertIn("`helm chat seats`", err)
        unrelated, err = actors.bind(C)
        self.assertIsNone(err, err)
        self.assertEqual(unrelated["canonical_name"], C)
        self.assertIsNone(actors.lookup(A))
        self.assertIsNone(actors.lookup(B))

        self.assertTrue(seats_rename._clear_journal())
        admitted, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], A)

    def test_legacy_roster_labels_do_not_reserve_a_colliding_name(self):  # noqa: VACUOUS_ASSERTION — the exact collision and unrelated admission are positive controls
        source = "a.--.-.---..--.-----z"
        unrelated = "a---...--.-.---...--z"
        target = "seat-target"
        collision = "a-------------------z-51f3df11"
        self.assertEqual(seats._seat_key(source), collision,
                         "control: hostile source no longer has the collision")
        self.assertEqual(seats._seat_key(unrelated), collision,
                         "control: hostile unrelated name no longer collides")
        other = {"session_id": "sid-unrelated"}
        row = self.legacy_journal(
            source, target,
            roster_before={source: {"session_id": "sid-source"},
                           unrelated: other},
            roster_after={target: {"session_id": "sid-source"},
                          unrelated: other})
        pk.write_json(seats_rename.rename_journal_path(), row)

        reserved = ((source, "source name"),
                    (source.swapcase(), "source name"),
                    (target, "target name"),
                    (target.swapcase(), "target name"))
        for name, role in reserved:
            actor, err = actors.bind(name)
            self.assertIsNone(actor)
            self.assertIn("pending seat rename", err)
            self.assertIn(role, err)
        admitted, err = actors.bind(unrelated)
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], unrelated)
        self.assertIsNone(actors.lookup(source))
        self.assertIsNone(actors.lookup(target))

    def test_damaged_legacy_roster_evidence_fails_closed(self):  # noqa: VACUOUS_ASSERTION — post-clear first admission is the unconditional positive control
        damaged = {
            "partial": {"roster_before": {A: {}}},
            "malformed": {"roster_before": {A: {}},
                          "roster_after": [B]},
            "ambiguous": {"roster_before": {A: {}, C: {}},
                          "roster_after": {B: {}, "seat-d": {}}},
            "key-mismatch": {"roster_before": {C: {}},
                             "roster_after": {B: {}}},
        }
        for label, evidence in damaged.items():
            with self.subTest(label=label):
                pk.write_json(seats_rename.rename_journal_path(),
                              self.legacy_journal(**evidence))
                before = self.snapshot()
                actor, err = actors.bind("seat-unrelated")
                self.assertIsNone(actor)
                self.assertIn("does not safely identify", err)
                self.assertIn("first admission is refused", err)
                self.assertUnmoved(before, "%s roster evidence admitted an actor"
                                   % label)
        self.assertTrue(seats_rename._clear_journal())
        admitted, err = actors.bind("seat-unrelated")
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], "seat-unrelated")

    def test_json_escaped_surrogate_roster_name_fails_closed(self):  # noqa: VACUOUS_ASSERTION — escaped surrogate and post-clear admission are unconditional positive controls
        malformed = "\ud800"
        snapshots = {
            "source": {"roster_before": {malformed: {}},
                       "roster_after": {B: {}}},
            "target": {"roster_before": {A: {}},
                       "roster_after": {malformed: {}}},
        }
        for label, evidence in snapshots.items():
            with self.subTest(label=label):
                encoded = json.dumps(self.legacy_journal(**evidence),
                                     ensure_ascii=True)
                self.assertIn("\\ud800", encoded,
                              "control: fixture did not preserve the surrogate")
                pk.atomic_write(seats_rename.rename_journal_path(), encoded)
                before = self.snapshot()
                actor, err = actors.bind("seat-unrelated")
                self.assertIsNone(actor)
                self.assertIn("does not safely identify", err)
                self.assertIn("first admission is refused", err)
                self.assertUnmoved(before, "%s surrogate admitted an actor"
                                   % label)
        self.assertTrue(seats_rename._clear_journal())
        admitted, err = actors.bind("seat-unrelated")
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], "seat-unrelated")

    def test_pending_rename_reservation_uses_only_the_current_chat_root(self):
        store = actors.store_path()
        pk.write_json(
            seats_rename.rename_journal_path(),
            self.legacy_journal(
                roster_before={A: {"session_id": "sid-source"}},
                roster_after={B: {"session_id": "sid-source"}}))
        blocked, err = actors.bind(A)
        self.assertIsNone(blocked)
        self.assertIn("pending seat rename", err)

        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "other-chat")
        chat._ensure_dir()
        self.assertEqual(actors.store_path(), store,
                         "control: both chat roots must share one Actor store")
        admitted, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], A)

    def test_unknown_journal_names_fail_closed_only_for_first_admission(self):
        known, err = actors.bind(A)
        self.assertIsNone(err, err)
        with open(actors.store_path(), "rb") as fh:
            committed = fh.read()
        pk.atomic_write(seats_rename.rename_journal_path(), "{not-json")

        blocked, err = actors.bind(B)
        self.assertIsNone(blocked)
        self.assertIn("pending seat rename journal is unreadable", err)
        self.assertIn("source and target are UNKNOWN", err)
        self.assertIn("`helm chat seats`", err)
        again, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(again["actor_id"], known["actor_id"])
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), committed)

        self.assertTrue(seats_rename._clear_journal())
        with mock.patch.object(seats_rename, "_journal",
                               side_effect=OSError("permission denied")):
            blocked, err = actors.bind(C)
        self.assertIsNone(blocked)
        self.assertIn("pending seat rename journal is unreadable", err)
        self.assertIn("source and target are UNKNOWN", err)
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), committed)

        admitted, err = actors.bind(B)
        self.assertIsNone(err, err)
        self.assertEqual(admitted["canonical_name"], B)

    def test_failed_rename_cannot_publish_a_revision(self):
        actor = self.actor(A)
        before = self.snapshot().get(actors.store_path())
        with mock.patch.object(actors.pk, "write_json", side_effect=OSError("full")):
            ok, note = actors.rename(A, B)
        self.assertFalse(ok, note)
        self.assertEqual(self.snapshot().get(actors.store_path()), before)
        self.assertEqual(actors.lookup(A)["revision"], actor.version)
        self.assertIsNone(actors.lookup(B))
        ok, note = actors.rename(A, B)
        self.assertTrue(ok, note)
        self.assertEqual(actors.lookup(B)["revision"], actor.version + 1)

    def test_a_rename_may_not_merge_two_identities(self):
        self.actor(A)
        actors.bind(B, (("declared", "x"),))
        before = self.snapshot().get(actors.store_path())
        ok, err = actors.rename(A, B)
        self.assertFalse(ok)
        self.assertIn("different actor", err)
        self.assertEqual(self.snapshot().get(actors.store_path()), before)
        self.assertEqual(actors.lookup(A)["revision"], 1)
        ok, msg = actors.rename(A, C)
        self.assertTrue(ok, msg)
        self.assertEqual(actors.lookup(A)["revision"], 2)


class SplitIdentityConfigurationTest(LayerBase):
    """An explicit chat estate may not mint against the shared Actor store."""

    PATH_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
                 "HELM_ACTORS", "MELD_ACTORS")

    def clear_paths(self):
        for key in self.PATH_KEYS:
            os.environ.pop(key, None)

    def test_both_aliases_refuse_absent_or_shared_actor_override_then_admit_redirect(self):  # noqa: VACUOUS_ASSERTION — the literal HELM/MELD pair always runs and each case ends with a committed Actor-store positive control
        shared_home = os.path.join(self.tmp, "shared-home")
        for chat_key, actor_key in (("HELM_CHAT_DIR", "HELM_ACTORS"),
                                    ("MELD_CHAT_DIR", "MELD_ACTORS")):
            with self.subTest(chat_key=chat_key):
                self.clear_paths()
                os.environ["HELM_HOME"] = shared_home
                chat_root = os.path.join(self.tmp, chat_key.lower())
                os.environ[chat_key] = chat_root
                with mock.patch.object(home, "default_home",
                                       return_value=shared_home):
                    shared_store = actors.store_path()
                    for label, explicit in (("absent", False),
                                            ("explicit-shared", True)):
                        with self.subTest(chat_key=chat_key, actor=label):
                            if explicit:
                                os.environ[actor_key] = shared_store
                            else:
                                os.environ.pop(actor_key, None)
                            row, err = actors.bind("%s-%s" %
                                                   (chat_key.lower(), label))
                            self.assertIsNone(row)
                            self.assertIn("split identity configuration", err)
                            self.assertIn(os.path.abspath(chat_root), err)
                            self.assertIn(os.path.abspath(shared_store), err)
                            self.assertIn(actor_key, err)
                            self.assertFalse(os.path.exists(shared_store),
                                             "a refused admission wrote the shared store")

                    redirected = os.path.join(
                        self.tmp, actor_key.lower(), "actors.json")
                    os.environ[actor_key] = redirected
                    row, err = actors.bind(chat_key.lower() + "-redirected")
                    self.assertIsNone(err, err)
                    self.assertEqual(row["canonical_name"],
                                     chat_key.lower() + "-redirected")
                    self.assertEqual(actors.store_path(), redirected)
                    self.assertTrue(os.path.exists(redirected),
                                    "control: paired configuration did not commit")

    def test_default_redirected_home_and_explicit_chat_with_redirected_home_admit(self):  # noqa: VACUOUS_ASSERTION — the literal three-origin matrix always runs and every origin commits its own Actor store
        shared_home = os.path.join(self.tmp, "default-home")
        redirected = os.path.join(self.tmp, "redirected-home")
        explicit_chat = os.path.join(self.tmp, "explicit-chat")
        cases = (
            ("default", shared_home, None),
            ("redirected-home", redirected, None),
            ("explicit-chat-redirected-home", redirected, explicit_chat),
        )
        for label, root, explicit in cases:
            with self.subTest(origin=label):
                self.clear_paths()
                os.environ["HELM_HOME"] = root
                if explicit:
                    os.environ["HELM_CHAT_DIR"] = explicit
                with mock.patch.object(home, "default_home",
                                       return_value=shared_home), \
                        mock.patch.object(chat, "DEFAULT_DIR",
                                          os.path.join(self.tmp, "shared-chat")):
                    row, err = actors.bind("seat-" + label)
                    self.assertIsNone(err, err)
                    self.assertEqual(row["canonical_name"], "seat-" + label)
                    expected = os.path.join(root, home.GLOBAL, ".state",
                                            "actors.json")
                    self.assertEqual(actors.store_path(), expected)
                    self.assertTrue(os.path.exists(expected),
                                    "control: admitted origin did not commit")

    def test_explicit_default_chat_exact_or_symlink_is_shared_and_helm_precedence_wins(self):  # noqa: VACUOUS_ASSERTION — the literal admitted matrix commits three Actors before the HELM-wins-split refusal is checked
        shared_home = os.path.join(self.tmp, "same-chat-home")
        shared_chat = os.path.join(self.tmp, "same-chat")
        linked_chat = os.path.join(self.tmp, "same-chat-link")
        split_chat = os.path.join(self.tmp, "other-chat")
        os.makedirs(shared_chat)
        os.symlink(shared_chat, linked_chat)
        admitted = (
            ("helm-exact", {"HELM_CHAT_DIR": shared_chat}, shared_chat, False),
            ("meld-symlink", {"MELD_CHAT_DIR": linked_chat}, linked_chat, True),
            ("helm-preferred", {"HELM_CHAT_DIR": shared_chat,
                                "MELD_CHAT_DIR": split_chat}, shared_chat, False),
        )
        with mock.patch.object(home, "default_home", return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", shared_chat):
            for label, overrides, selected, symlinked in admitted:
                with self.subTest(case=label):
                    self.clear_paths()
                    os.environ["HELM_HOME"] = shared_home
                    name = "seat-" + label
                    if symlinked:
                        # Known canonical binds are the read-only hot path: seed
                        # under the real default, then select its symlink and
                        # prove the secure journal door is never consulted.
                        row, err = actors.bind(name)
                        self.assertIsNone(err, err)
                    os.environ.update(overrides)
                    self.assertEqual(chat.chat_dir(), selected,
                                     "control: env precedence chose another path")
                    row, err = actors.bind(name)
                    self.assertIsNone(err, err)
                    self.assertEqual(row["canonical_name"], name)

            store = actors.store_path()
            with open(store, "rb") as fh:
                before = fh.read()
            self.clear_paths()
            os.environ.update({"HELM_HOME": shared_home,
                               "HELM_CHAT_DIR": split_chat,
                               "MELD_CHAT_DIR": shared_chat})
            self.assertEqual(chat.chat_dir(), split_chat,
                             "control: HELM_CHAT_DIR must outrank MELD_CHAT_DIR")
            row, err = actors.bind("seat-helm-split")
            self.assertIsNone(row)
            self.assertIn("split identity configuration", err)
            self.assertIn("HELM_ACTORS", err)
            with open(store, "rb") as fh:
                self.assertEqual(fh.read(), before,
                                 "HELM-preferred split admission changed store")

    def test_symlinked_explicit_chat_root_refuses_first_bind_with_real_target_remedy(self):  # noqa: VACUOUS_ASSERTION — the real-directory retry commits the Actor after both refusal surfaces and the pre-refusal snapshot proves the instrument can see that commit
        shared_home = os.path.join(self.tmp, "symlink-home")
        target = os.path.join(self.tmp, "symlink-chat-target")
        linked = os.path.join(self.tmp, "symlink-chat-selected")
        os.makedirs(target)
        os.symlink(target, linked)
        self.clear_paths()
        os.environ.update({"HELM_HOME": shared_home,
                           "HELM_CHAT_DIR": linked})
        with mock.patch.object(home, "default_home", return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", target):
            self.assertTrue(os.path.islink(linked),
                            "control: selected chat root is not a symlink")
            self.assertEqual(os.path.realpath(linked), target,
                             "control: remedy target is not the linked directory")
            before = self.snapshot()
            row, err = actors.bind(A)
            self.assertIsNone(row)
            self.assertIn("explicit chat root", err)
            self.assertIn("symlink", err)
            self.assertIn(repr(linked), err)
            self.assertIn("Set HELM_CHAT_DIR=%r" % target, err)
            self.assertIn("real directory target", err)
            self.assertIn("first admission remains refused", err)
            self.assertUnmoved(before,
                               "symlink refusal mutated an Actor or chat file")
            self.assertFalse(os.path.exists(actors.store_path()),
                             "refused first admission wrote the Actor store")
            self.assertIsNone(actors.lookup(A))

            rc, out, command_err = self.cli("seats")
            self.assertEqual(rc, 1,
                             "control: helm chat seats no longer reports the "
                             "secure-open refusal")
            self.assertEqual(out, "")
            self.assertIn("interrupted rename is not recoverable", command_err)

            # The remedy changes ONLY the selected override. No automatic
            # realpath retry occurred above; the operator's explicit target is
            # what makes this second first-admission attempt admissible.
            os.environ["HELM_CHAT_DIR"] = target
            admitted, err = actors.bind(A)
            self.assertIsNone(err, err)
            self.assertEqual(admitted["canonical_name"], A)
            self.assertTrue(os.path.isfile(actors.store_path()),
                            "real-target retry did not commit the Actor")

    def test_unresolvable_symlink_keeps_generic_refusal_despite_normalized_target(self):  # noqa: VACUOUS_ASSERTION — the Actor-store absence and real-path controls distinguish a false remedy from successful admission
        shared_home = os.path.join(self.tmp, "unresolvable-home")
        target = os.path.join(self.tmp, "unresolvable-real")
        os.makedirs(target)
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")
        for intermediate in (blocker, os.path.join(self.tmp, "absent")):
            with self.subTest(intermediate=intermediate):
                linked = os.path.join(self.tmp, "unresolvable-link")
                if os.path.lexists(linked):
                    os.unlink(linked)
                os.symlink(os.path.join(intermediate, "..", "unresolvable-real"),
                           linked)
                self.clear_paths()
                os.environ.update({"HELM_HOME": shared_home,
                                   "HELM_CHAT_DIR": linked})
                with mock.patch.object(home, "default_home",
                                       return_value=shared_home), \
                        mock.patch.object(chat, "DEFAULT_DIR", target):
                    self.assertEqual(os.path.realpath(linked), target,
                                     "control: non-strict resolution did not hide "
                                     "the invalid intermediate component")
                    with self.assertRaises(OSError):
                        os.stat(linked)
                    before = self.snapshot()
                    row, err = actors.bind(A)
                    self.assertIsNone(row)
                    self.assertIn("pending seat rename journal is unreadable", err)
                    self.assertNotIn("real directory target", err)
                    self.assertNotIn("Set HELM_CHAT_DIR", err)
                    self.assertUnmoved(before,
                                       "invalid symlink refusal mutated identity")
                    self.assertFalse(os.path.exists(actors.store_path()))

    def test_eloop_secure_open_shape_gets_the_same_proven_symlink_remedy(self):  # noqa: VACUOUS_ASSERTION — real lstat and absent Actor store prove the diagnostic did not trust the synthetic errno alone
        shared_home = os.path.join(self.tmp, "eloop-home")
        target = os.path.join(self.tmp, "eloop-chat-target")
        linked = os.path.join(self.tmp, "eloop-chat-selected")
        os.makedirs(target)
        os.symlink(target, linked)
        self.clear_paths()
        os.environ.update({"HELM_HOME": shared_home,
                           "HELM_CHAT_DIR": linked})

        @contextlib.contextmanager
        def eloop_scope():
            raise OSError(errno.ELOOP, "final component is a symlink")
            yield  # pragma: no cover — makes this a context manager generator

        with mock.patch.object(home, "default_home", return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", target), \
                mock.patch.object(seats_rename, "_rename_scope",
                                  side_effect=eloop_scope):
            row, err = actors.bind(A)
            self.assertIsNone(row)
            self.assertIn(repr(linked), err)
            self.assertIn("Set HELM_CHAT_DIR=%r" % target, err)
            self.assertIn("symlink", err)
            self.assertFalse(os.path.exists(actors.store_path()),
                             "ELOOP diagnosis admitted or wrote an Actor")

    def test_ordinary_non_directory_chat_root_keeps_generic_journal_recovery_text(self):  # noqa: VACUOUS_ASSERTION — lstat-positive regular file and unchanged snapshot are the controls for the absent symlink diagnosis
        shared_home = os.path.join(self.tmp, "ordinary-home")
        ordinary = os.path.join(self.tmp, "ordinary-chat-file")
        with open(ordinary, "w", encoding="utf-8") as fh:
            fh.write("not a directory\n")
        self.clear_paths()
        os.environ.update({"HELM_HOME": shared_home,
                           "HELM_CHAT_DIR": ordinary})
        with mock.patch.object(home, "default_home", return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", ordinary):
            self.assertTrue(os.path.isfile(ordinary),
                            "control: ordinary root is not a regular file")
            self.assertFalse(os.path.islink(ordinary),
                             "control: ordinary root unexpectedly is a symlink")
            before = self.snapshot()
            row, err = actors.bind(A)
            self.assertIsNone(row)
            self.assertIn("pending seat rename journal is unreadable", err)
            self.assertIn("Run `helm chat seats`", err)
            self.assertNotIn("symlink", err.lower())
            self.assertNotIn("Set HELM_CHAT_DIR", err)
            self.assertUnmoved(before,
                               "ordinary-file refusal mutated an Actor file")

    def test_malformed_journal_keeps_generic_recovery_text_not_symlink_remedy(self):  # noqa: VACUOUS_ASSERTION — malformed bytes and unchanged snapshot are positive controls on the generic failure path
        shared_home = os.path.join(self.tmp, "malformed-home")
        root = os.path.join(self.tmp, "malformed-chat")
        os.makedirs(root)
        self.clear_paths()
        os.environ.update({"HELM_HOME": shared_home,
                           "HELM_CHAT_DIR": root})
        with mock.patch.object(home, "default_home", return_value=shared_home), \
                mock.patch.object(chat, "DEFAULT_DIR", root):
            pk.atomic_write(seats_rename.rename_journal_path(), "{not-json")
            before = self.snapshot()
            row, err = actors.bind(A)
            self.assertIsNone(row)
            self.assertIn("pending seat rename journal is unreadable", err)
            self.assertIn("Run `helm chat seats`", err)
            self.assertNotIn("symlink", err.lower())
            self.assertNotIn("Set HELM_CHAT_DIR", err)
            self.assertUnmoved(before,
                               "malformed-journal refusal mutated an Actor file")

    def test_new_generation_readmission_is_guarded_but_existing_store_can_be_redirected(self):  # noqa: VACUOUS_ASSERTION — the copied-store retry is an unconditional committed new-generation positive control
        first, err = actors.bind(A)
        self.assertIsNone(err, err)
        ok, note = actors.rename(A, B)
        self.assertTrue(ok, note)
        self.roster(A, "sid-new-generation")
        shared_home = os.environ["HELM_HOME"]
        shared_store = actors.store_path()
        with open(shared_store, "rb") as fh:
            before = fh.read()
        with mock.patch.object(home, "default_home", return_value=shared_home):
            row, err = actors.bind(A)
            self.assertIsNone(row)
            self.assertIn("first Actor admission", err)
            self.assertIn(shared_store, err)
        with open(shared_store, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "refused re-admission changed the shared store")

        redirected = os.path.join(self.tmp, "generation", "actors.json")
        os.makedirs(os.path.dirname(redirected))
        shutil.copyfile(shared_store, redirected)
        os.environ["HELM_ACTORS"] = redirected
        with mock.patch.object(home, "default_home", return_value=shared_home):
            second, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(second["canonical_name"], A)
        self.assertNotEqual(second["actor_id"], first["actor_id"])
        self.assertEqual(actors.store_path(), redirected)
        with open(redirected, "rb") as fh:
            self.assertNotEqual(fh.read(), before,
                                "control: redirected re-admission did not commit")


class StoreHasThreeStatesTest(LayerBase):
    """ABSENT is a legitimate empty. MALFORMED and UNREADABLE are REFUSALS.

    Collapsing them is the well-formed-certification-of-an-empty-world class:
    the input is not unreadable, it is READABLE AND FALSE, so every careful
    consumer downstream is carefully wrong. Here it can MINT AN IDENTITY —
    `by_name` is the only thing keeping a retired alias pointed at its actor,
    so an empty-looking store turns every known seat into a first sighting and
    re-mints the alias as a NEW actor."""

    def corrupt(self, body):
        path = actors.store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_ABSENT_is_a_legitimate_empty_that_still_mints(self):  # noqa: VACUOUS_ASSERTION — assertIsNone on the refusal is paired with an unconditional presence assertion that a real capability came back
        self.assertFalse(os.path.exists(actors.store_path()))
        self.declare(A)
        self.roster(A, "sid-x")  # declared names need their roster row
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)

    def test_MALFORMED_refuses_and_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — the store is removed at the end and the SAME call is asserted to return the SAME actor_id: an unconditional positive control on the exact observable
        self.declare(A)
        self.roster(A, "sid-x")  # declared names need their roster row
        actor, _e = actors.resolve_actor("sid-x", self.tmp)   # mint it honestly
        self.assertIsNotNone(actor)
        real_id = actor.actor_id
        self.corrupt("{ this is not json")
        before = self.snapshot()
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(actor, "a corrupt store minted an actor anyway")
        self.assertIn("MALFORMED", err)
        self.assertUnmoved(before, "a refused resolution rewrote the store")
        # THE ACTUAL HAZARD, stated as an assertion: with the corrupt store
        # read as empty, this same call would have derived a FRESH record for
        # a name the real store already knows.
        os.remove(actors.store_path())
        again, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(err, err)
        self.assertEqual(again.actor_id, real_id,
                         "control: the same name must resolve to the same id")

    def test_a_STORE_SHAPED_LIKE_SOMETHING_ELSE_refuses(self):
        self.declare(A)
        self.corrupt('{"v": 1, "actors": "not-a-mapping"}')
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(actor)
        self.assertIn("not a store", err)

    def test_UNREADABLE_refuses_rather_than_reading_as_empty(self):
        self.declare(A)
        path = self.corrupt('{"v": 1, "actors": {}, "by_name": {}}')
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        if os.access(path, os.R_OK):        # running as root: no such state
            self.skipTest("this uid can read a 0-mode file")
        actor, err = actors.resolve_actor("sid-x", self.tmp)
        self.assertIsNone(actor)
        self.assertIn("UNREADABLE", err)

    def test_a_corrupt_store_refuses_SPEECH_too(self):  # noqa: VACUOUS_ASSERTION — the post after the store is removed is an unconditional positive control on the same verb
        """`resolve_speaker` drops UNRESOLVED and nothing else. The hazard here
        is the STORE, not the tier of the act.

        The seat is DECLARED deliberately: an env-less process short-circuits
        at UNRESOLVED and never reaches the store at all, so an undeclared
        version of this arm would pass without ever consulting the thing it
        claims to be about."""
        self.declare(A)
        self.corrupt("}{")
        before = self.snapshot()
        rc, _out, err = self.cli("post", "hello")
        self.assertEqual(rc, 2, "speech minted against a corrupt store")
        self.assertIn("MALFORMED", err)
        self.assertUnmoved(before, "a refused post still wrote")
        os.remove(actors.store_path())
        rc, _out, err = self.cli("post", "hello")
        self.assertEqual(rc, 0, err)        # control: the same verb, no store

    def test_lookup_is_a_VIEW_and_says_so(self):
        """It collapses absent and unavailable to None, in the shape
        `council.registry` has beside `council.read` — so nothing that MINTS
        may use it. The mints take the two-tuple."""
        self.corrupt("nonsense")
        self.assertIsNone(actors.lookup(A))
        store, unavailable = actors.read_store()
        self.assertIsNone(store)
        self.assertTrue(unavailable, "read_store must report what lookup hides")


class TheIndexIsRequiredNotDefaultedTest(LayerBase):
    """`.get("by_name", {})` MADE A MISSING INDEX AND AN EMPTY ONE THE SAME
    ANSWER, and they are opposite facts.

    An empty index is a store with no names yet — mintable. A missing index is
    a store whose index was LOST — and minting against it re-mints a retired
    alias as a NEW actor, forking the lineage the index exists to preserve.
    The store-level three states (absent / malformed / unreadable) were the
    same collapse one level up; this is it INSIDE a parsed file."""

    AID = "1" * 8

    def plant(self, **over):
        """A RENAMED actor: canonical seat-b, alias seat-a, one id. Exactly the
        state `actors.rename` produces, which is the state whose whole point is
        that the old name keeps resolving."""
        d = {"v": 1,
             "actors": {self.AID: {"actor_id": self.AID, "canonical_name": B,
                                   "aliases": [A], "created": "t", "v": 1}},
             "by_name": {A.casefold(): self.AID, B.casefold(): self.AID}}
        d.update(over)
        for k, v in list(over.items()):
            if v is None:
                del d[k]
        path = actors.store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(d, fh)
        return path

    def test_legacy_revision_and_malformed_revision_are_distinct(self):
        self.plant()                     # legacy row has no revision
        row, err = actors.bind(B)
        self.assertIsNone(err, err)
        self.assertEqual(row.get("revision", 1), 1)
        before = self.snapshot().get(actors.store_path())
        self.assertIsNotNone(before, "positive control: legacy store exists")
        for malformed in (0, -1, True, "2", None):
            with self.subTest(malformed=malformed):
                self.plant(actors={self.AID: {
                    "actor_id": self.AID, "canonical_name": B,
                    "aliases": [A], "revision": malformed}})
                row, err = actors.bind(B)
                self.assertIsNone(row)
                self.assertIn("invalid revision", err)
        self.plant()
        ok, note = actors.rename(B, C)
        self.assertTrue(ok, note)
        self.assertEqual(actors.lookup(A)["revision"], 2)

    def test_present_and_missing_index_are_OPPOSITE_outcomes(self):  # noqa: VACUOUS_ASSERTION — the FIRST half is the unconditional positive control and it is the whole design of the arm: index PRESENT must resolve the alias to its own actor, so an always-refuse build fails here before the absence assertion is ever read
        """THE DISCRIMINATING PAIR, in one arm on purpose. Same store, same
        `bind`, ONE KEY present or absent — and it has to come out both ways,
        because "no new actor was minted" passes on a build that mints nothing
        for any reason, and "the alias resolved" passes on a build that never
        refuses. Neither an always-refuse nor an always-mint build survives."""
        self.plant()                                  # index PRESENT
        row, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertEqual(row["actor_id"], self.AID,
                         "the old alias must resolve to its own actor")

        before = self.snapshot()
        self.plant(by_name=None)                      # index MISSING
        after_plant = self.snapshot()
        self.assertNotEqual(before, after_plant, "control: the plant wrote")
        row, err = actors.bind(A)
        self.assertIsNone(row, "a lost index minted a SECOND actor for one seat")
        self.assertIn("MISSING index", err)
        self.assertUnmoved(after_plant,
                           "a refused bind still rewrote the store — the fork "
                           "is durable the moment it is written")

    def test_an_EMPTY_index_is_STILL_MINTABLE(self):
        """The other half of the distinction: empty is a legitimate state, and
        a cure that refused it would have traded one collapse for another."""
        self.plant(actors={}, by_name={})
        row, err = actors.bind(A)
        self.assertIsNone(err, err)
        self.assertTrue(row["actor_id"], "an empty store must still mint")

    def test_a_row_without_its_ALIASES_LIST_refuses(self):  # noqa: VACUOUS_ASSERTION — the honest plant in the sibling arms is the control; this asserts a refusal MESSAGE, a presence
        """`aliases` IS the lineage. Defaulted to `[]` at the append site, a
        row that lost it silently discards every name the actor answered to."""
        self.plant(actors={self.AID: {"actor_id": self.AID,
                                      "canonical_name": B, "v": 1}})
        _store, unavailable = actors.read_store()
        self.assertIsNotNone(unavailable)
        self.assertIn("IS the lineage", unavailable)

    def test_an_index_naming_an_actor_the_store_lacks_refuses(self):  # noqa: VACUOUS_ASSERTION — same class as the arm above; asserts a refusal message rather than an absence
        """The same fork by another route: `bind` would build a fresh row under
        that id and the renamed record it named would be gone."""
        self.plant(actors={})
        _store, unavailable = actors.read_store()
        self.assertIsNotNone(unavailable)
        self.assertIn("does not hold", unavailable)


class DistinctCapabilitiesTest(LayerBase):
    """System and recovery authorities are NOT identity, and holding a name
    must not reach them."""

    def test_autoclaim_refuses_a_raw_name_and_an_unsanctioned_kind(self):  # noqa: VACUOUS_ASSERTION — the last statement mints a real capability and asserts its actor name — an unconditional positive control on the same constructor
        self.declare(A)
        self.roster(A, "sid-x")  # declared names need their roster row
        actor, _e = actors.resolve_actor("sid-x", self.tmp)
        with self.assertRaises(actors.ActorRefused):
            actors.AutoClaimCapability(A, "build", "r1", ("build",))
        with self.assertRaises(actors.ActorRefused):
            actors.AutoClaimCapability(actor, "deploy", "r1", ("build",))
        cap = actors.AutoClaimCapability(actor, "build", "r1", ("build",))
        self.assertEqual(cap.actor.canonical_name, A)

    def test_a_stale_proof_mints_ONLY_from_a_stale_verdict(self):  # noqa: VACUOUS_ASSERTION — the stale mint after the loop runs unconditionally and asserts proof.holder
        for verdict in ("live", "unknown", "", None):
            proof, err = actors.prove_stale("res:x", B, verdict, "why")
            self.assertIsNone(proof, "%r must never authorize a release — "
                                     "missing evidence is not evidence of "
                                     "death" % (verdict,))
            self.assertTrue(err)
        proof, err = actors.prove_stale("res:x", B, "stale", "holder is dead")
        self.assertIsNone(err)
        self.assertEqual(proof.holder, B)

    def test_a_system_lease_holder_cannot_be_read_as_a_seat(self):  # noqa: VACUOUS_ASSERTION — the second half mints a real capability unconditionally and asserts its holder prefix
        with self.assertRaises(actors.ActorRefused):
            actors.SystemLeaseCapability("not-a-subsystem")
        cap = actors.SystemLeaseCapability("gate", "abcdef1234")
        self.assertTrue(cap.holder.startswith("system:gate:"))
        self.assertNotIn(cap.holder, (A, B, C))


class ProductionWritersRefuseDerivedTest(LayerBase):
    """THE MATRIX. Each arm drives the REAL verb and asserts the stores did not
    move; each carries its positive control in the same arm."""

    def _refuses(self, argv, needle="DERIVED"):
        before = self.snapshot()
        rc, _out, err = self.cli(*argv)
        self.assertEqual(rc, 2, "expected a refusal from %r, got %r"
                         % (argv, err or _out))
        self.assertIn(needle, err)
        self.assertUnmoved(before, "%r refused but still wrote" % (argv,))

    def _admits(self, argv):
        before = self.snapshot()
        rc, out, err = self.cli(*argv)
        self.assertEqual(rc, 0, err)
        self.assertMoved(before, "control: %r must be able to write" % (argv,))
        return out

    def test_SPEECH_is_admitted_but_the_ACT_beside_it_is_not(self):  # noqa: VACUOUS_ASSERTION — the first half is an unconditional positive control: the same process, in the same state, writes a room row
        """THE SPLIT, in one process state. A plain room post is SPEECH — the
        row is read, and nothing about it says another party did, owes or saw
        anything, which is precisely what `seats.auto_name` exists to let a
        fresh join do. `post --dm` in the SAME breath opens an obligation in
        one seat's private lane, and that is an ACT.

        Refusing both was measured wrong: it broke a pinned arm whose subject
        is that chat IO survives a deleted cwd. Admitting both is the defect
        this lane exists to close. The line is what the row MEANS."""
        self._admits(["post", "hello"])          # speech, on the auto-name floor
        before = self.snapshot()
        rc, _out, err = self.cli("post", "--dm", B, "private word")
        self.assertEqual(rc, 1, "a DM under a minted name must not land")
        self.assertIn("DERIVED", err)
        self.assertUnmoved(before, "the refused DM still wrote a row")

    def test_react_is_speech_too_and_the_declared_case_still_writes(self):  # noqa: VACUOUS_ASSERTION — both halves are positive controls asserting the store MOVED
        self._admits(["post", "target"])         # env-less: the speech floor
        self._admits(["react", "1", ":fire:"])   # env-less: still speech
        self.declare(B)
        self._admits(["react", "1", ":fire:"])   # declared: unchanged

    def test_cli_dm_refuses_then_a_declared_seat_writes(self):  # noqa: VACUOUS_ASSERTION — _admits() is the unconditional positive control and asserts the store MOVED on the same observable
        self._refuses(["dm", B, "private word"])
        self.declare(A)
        self._admits(["dm", B, "private word"])

    def test_a_MALFORMED_name_cannot_speak_either(self):  # noqa: VACUOUS_ASSERTION — the declared-seat post at the end is an unconditional positive control on the same observable
        """The speech door drops exactly ONE refusal. A hostile
        HELM_CHAT_NAME is no name at any tier — it must not reach a room row
        by falling through to the derived floor."""
        os.environ["HELM_CHAT_NAME"] = "seat\x1b[2Ja"
        before = self.snapshot()
        rc, _out, err = self.cli("post", "hello")
        self.assertEqual(rc, 2)
        self.assertIn("not a legitimate seat identifier", err)
        self.assertUnmoved(before, "a hostile name reached a durable row")
        self.declare(A)
        self._admits(["post", "hello"])

    def test_cli_claim_refuses_then_a_declared_seat_writes(self):  # noqa: VACUOUS_ASSERTION — _admits() is the unconditional positive control and asserts the store MOVED on the same observable
        self._refuses(["claim", "res:lane"])
        self.declare(A)
        self._admits(["claim", "res:lane", "--ttl", "60"])

    def test_cli_claim_uncorroborated_refuses_without_binding_then_writes(self):  # noqa: VACUOUS_ASSERTION — the same real claim command writes both Actor and claim stores after only the roster/session fact changes
        self.declare(A, corroborate=False)
        refused_before = self.snapshot()
        rc, _out, err = self.cli("claim", "res:uncorroborated", "--ttl", "60")
        self.assertEqual(rc, 2, err)
        self.assertIn("nothing corroborates it", err)
        self.assertUnmoved(refused_before,
                           "the refused real claim committed durable state")
        self.assertFalse(os.path.exists(actors.store_path()))
        self.assertFalse(os.path.exists(seats.claims_path()))

        self.session(SID)
        self.roster(A, SID)
        admitted_before = self.snapshot()
        rc, out, err = self.cli("claim", "res:uncorroborated", "--ttl", "60")
        self.assertEqual(rc, 0, err)
        self.assertIn("claimed by %s" % A, out)
        self.assertWrote(admitted_before, actors.store_path(),
                         "the admitted claim did not bind its Actor")
        self.assertWrote(admitted_before, seats.claims_path(),
                         "the admitted claim did not write its lease")

    def test_release_is_authorized_by_the_TOKEN_not_by_a_name(self):  # noqa: VACUOUS_ASSERTION — both halves are presence assertions: one release lands and the ledger empties, the other is refused with the ledger still holding the row
        """CLAIM assigns responsibility, so it needs an admitted actor. RELEASE
        SPENDS A NONCE only the holder was handed — `_binding_ok` requires
        {lease, holder, granting session} together, so presenting the token IS
        the proof, exactly as a liveness proof is for a dead holder.

        Requiring an actor on top broke the instructions-are-runnable law: the
        stop-guard prints `release … --seat <holder>` to a seat whose roster
        alias differs from its declared name, and that seat could no longer
        follow its own guard."""
        self.declare(A)
        out = self._admits(["claim", "res:lane", "--ttl", "60"])
        lease = out.split("lease ")[1].split(",")[0]
        del os.environ["HELM_CHAT_NAME"]
        rc, _o, err = self.cli("release", "res:lane", "--lease", "0" * 16,
                               "--seat", A)
        self.assertNotEqual(rc, 0, "a WRONG token released the lease")
        self.assertTrue(seats.claims_list(), "the row must still be held")
        del err
        rc, _o, err = self.cli("release", "res:lane", "--lease", lease,
                               "--seat", A)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seats.claims_list(), [],
                         "the right token must release without a declared name")

    def test_status_WITHHOLDS_attribution_rather_than_refusing_the_write(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a presence assertion on the roster row, and the declared half is an unconditional positive control
        """`by=` is RECORDED (status_by, post parity), so a minted or disputed
        name there credits a stranger — but refusing the whole verb is a
        SELF-LOCKOUT, and NoSelfLockoutTest exists because the fleet burned on
        exactly that shape. So the write lands and the ATTRIBUTION does not."""
        self.roster(A, SID)
        rc, out, err = self.cli("status", "--seat", A, "on the identity lane")
        self.assertEqual(rc, 0, err)
        row = seats.roster()[A]
        self.assertEqual(row["status"], "on the identity lane")
        self.assertNotIn("status_by", row,
                         "an unadmitted writer claimed the annotation")
        self.declare(B)                      # control: a declared writer IS
        rc, _out, err = self.cli("status", "--seat", A, "annotated")
        self.assertEqual(rc, 0, err)         # credited, on the same observable
        self.assertEqual(seats.roster()[A].get("status_by"), B)
        del out

    def test_cli_ack_refuses_under_a_derived_identity(self):  # noqa: VACUOUS_ASSERTION — seats.dm above runs unconditionally and asserts err is None — the row being acked provably exists
        self.declare(A, corroborate=False)
        row, err = seats.dm(B, "please review", who=A)
        self.assertIsNone(err, err)
        del os.environ["HELM_CHAT_NAME"]
        self._refuses(["ack", row["id"], "done"])

    def test_programmatic_dm_refuses_ambient_and_admits_on_behalf_of(self):  # noqa: VACUOUS_ASSERTION — the on-behalf-of DM at the end is the unconditional positive control and asserts the store MOVED
        before = self.snapshot()
        row, err = seats.dm(B, "ambient", session="sid-nobody")
        self.assertIsNone(row)
        self.assertIn("DERIVED", err)
        self.assertUnmoved(before, "a refused DM still wrote a row")
        row, err = seats.dm(B, "on behalf of", who=A)
        self.assertIsNone(err, err)
        self.assertMoved(before, "control: an on-behalf-of DM must write")

    def test_programmatic_ack_refuses_ambient(self):  # noqa: VACUOUS_ASSERTION — the on-behalf-of ack at the end is the unconditional positive control and asserts the store MOVED
        row, err = seats.dm(B, "please review", who=A)
        self.assertIsNone(err, err)
        before = self.snapshot()
        res, err = seats.ack(row["id"], "done", session="sid-nobody")
        self.assertIsNone(res)
        self.assertIn("DERIVED", err)
        self.assertUnmoved(before, "a refused ack still moved a store")
        res, err = seats.ack(row["id"], "done", who=B)
        self.assertIsNone(err, err)
        self.assertMoved(before, "control: an on-behalf-of ack must write")

    def test_delivery_is_the_BIND_path_so_DERIVED_still_self_heals(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a presence assertion (a roster row appears, a cursor moves), not an absence
        """DELIVERY IS WHERE AN UNROSTERED SESSION ACQUIRES A NAME, so
        refusing DERIVED here is a LOCKOUT LOOP: a session that never joined
        could never become rostered and therefore could never stop being
        DERIVED. And a derived delivery cannot drain a stranger — `auto_name`
        dedupes against the roster, so the minted name provably is not a live
        seat's. The theft vector was the DISPUTE, and that still refuses (the
        arm below)."""
        self.declare(A)
        self._admits(["post", "@%s ping" % B])
        del os.environ["HELM_CHAT_NAME"]
        before = self.snapshot()
        seats.deliver(session="sid-fresh", room="main", cwd=self.tmp,
                      emit=lambda _line: None)
        self.assertMoved(before, "the self-heal did not bind an unknown "
                                 "session — that is the lockout loop")

    def test_delivery_moves_NOTHING_under_a_disputed_identity(self):  # noqa: VACUOUS_ASSERTION — the second half is an unconditional positive control: the same call with the sources in agreement moves the store
        """The 2026-08-02 theft vector: an inherited HELM_CHAT_NAME pointing
        at somebody else's row, consuming that seat's inbox."""
        from helm import session as helm_session
        self.roster(B, "sid-bound")
        self.declare(B, corroborate=False)
        # SOMEBODY ELSE'S ROW IS A LIVE ONE: the claim-jump asks the claude
        # census whether the rostered session is still held, so the arm plants
        # its holder rather than reading the host's process table.
        held = mock.patch.object(helm_session, "_proc_claude_census",
                                 return_value={
                                     "rows": [{"pid": 4242,
                                               "session": "sid-bound"}],
                                     "listing_failed": False,
                                     "who_failed": False,
                                     "census_partial": False})
        held.start()
        self.addCleanup(held.stop)
        before = self.snapshot()
        got = seats.deliver(session="sid-other", room="main", cwd=self.tmp)
        self.assertIsNone(got, "delivery ran under a contested identity")
        self.assertUnmoved(before, "a disputed delivery moved a cursor or a "
                                   "presence beat")
        seats.deliver(session="sid-bound", room="main", cwd=self.tmp,
                      emit=lambda _line: None)
        self.assertMoved(before, "control: the same call in agreement must run")

    def test_catchup_apply_cannot_park_ANOTHER_seats_backlog(self):  # noqa: VACUOUS_ASSERTION — _admits() posts a real addressed row first, so there is provably a backlog to park
        """`--seat <victim> --apply` from a pane that declares no name. The
        predecessor's two guards had a hole between them: `_assert_own_seat` is
        fail-open by contract, so it passed the victim through, and the DERIVED
        check sat behind `if not seat` and never ran."""
        self.declare(A, corroborate=False)
        self._admits(["post", "@%s a real ask" % B])
        del os.environ["HELM_CHAT_NAME"]
        before = self.snapshot()
        rc, _o, err = self.cli("catchup", "--seat", B, "--apply")
        self.assertEqual(rc, 2, "parking a victim's backlog must refuse")
        self.assertIn("DERIVED", err)
        self.assertUnmoved(before, "catchup refused but moved a cursor — the "
                                   "victim's backlog reads as seen")

    def test_catchup_apply_cannot_be_ASSERTED_into_another_seat(self):  # noqa: VACUOUS_ASSERTION — _admits() posts a real addressed row first, so there is provably a backlog to park
        self.declare(A)
        self._admits(["post", "@%s a real ask" % B])
        before = self.snapshot()
        rc, _o, err = self.cli("catchup", "--seat", B, "--apply")
        self.assertEqual(rc, 2)
        self.assertIn("never selects", err)
        self.assertUnmoved(before, "an asserted victim still had rows parked")


class ActingForAnotherSeatIsTypedTest(LayerBase):
    """`wait --seat <victim>` from an UNNAMED process armed that seat's beacon
    and drained its cursor — silently, and by `_assert_own_seat`'s own
    contract, which refused the named case and waved through the unnamed one.

    The contract that pinned the old behaviour is RETIRED by cross-family
    ruling; acting for another seat is now a typed, STATED capability."""

    def test_the_unnamed_process_is_REFUSED_and_beats_nothing(self):  # noqa: VACUOUS_ASSERTION — its positive controls are the two sibling arms below, which run the SAME verb in the same fixture and assert it succeeds and beats
        self.roster(B, "sid-bound")
        before = self.snapshot()
        rc, _out, err = self.cli("wait", "--seat", B, "--timeout", "0.01")
        self.assertEqual(rc, 2)
        self.assertIn("--on-behalf", err, "the refusal must name the way in")
        self.assertUnmoved(before, "the refused wait moved a cursor or a beat")

    def test_the_STATED_form_is_accepted_and_says_so_out_loud(self):  # noqa: VACUOUS_ASSERTION — asserts rc and the presence of a loud line, both presence assertions
        self.roster(B, "sid-bound")
        rc, _out, err = self.cli("wait", "--seat", B, "--on-behalf",
                                 "--timeout", "0.01")
        self.assertIn(rc, (0, 1), err)
        self.assertIn("ON BEHALF OF", err,
                      "silence is what made the fail-open dangerous")

    def test_declaring_the_seat_you_name_is_unchanged(self):  # noqa: VACUOUS_ASSERTION — the ordinary-case control for the two arms above; asserts an rc, not an absence
        self.roster(A, "sid-bound")
        self.declare(A)
        rc, _out, err = self.cli("wait", "--seat", A, "--timeout", "0.01")
        self.assertIn(rc, (0, 1), err)
        self.assertNotIn("--on-behalf", err)

    def test_a_DECLARED_process_still_cannot_name_another_seat(self):  # noqa: VACUOUS_ASSERTION — the loop body asserts a presence (rc 2 plus the refusal text), and the sibling arm above proves the same verb succeeds when the seat is the process's own
        """The half that was always right stays right — and `--on-behalf` must
        not become a way around it. A process that HAS an identity is not
        acting on behalf of anyone; it is lying about who it is."""
        self.declare(A)
        for argv in (["wait", "--seat", B, "--timeout", "0.01"],
                     ["wait", "--seat", B, "--on-behalf", "--timeout", "0.01"]):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("cannot receive for another seat", err)

    def test_the_capability_covers_ONE_subject(self):
        cap, err = actors.grant_on_behalf("beacon", B, stated=True)
        self.assertIsNone(err)
        self.assertTrue(cap.covers(B))
        self.assertTrue(cap.covers(B.upper()), "seat addressing is casefolded")
        self.assertFalse(cap.covers(C), "it authorized a seat it never named")

    def test_an_unsanctioned_subsystem_gets_no_capability(self):  # noqa: VACUOUS_ASSERTION — the sanctioned mint at the end is an unconditional positive control on the same call
        cap, err = actors.grant_on_behalf("anything", B, stated=True)
        self.assertIsNone(cap)
        self.assertIn("not a subsystem", err)
        cap, err = actors.grant_on_behalf("beacon", B, stated=True)
        self.assertIsNotNone(cap, err)


class NoIdentityOverguardTest(LayerBase):
    """The regression the predecessor shipped: admission ran BEFORE verb
    dispatch, so read-only verbs refused for an operator with no name. These
    arms are the tripwire for doing it again."""

    def work(self, *args):
        from helm import work
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = work.cmd_work(list(args) + ["--repo", self.repo])
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        LayerBase.setUp(self)
        import subprocess
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for cmd in (["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "t@example.invalid"],
                    ["git", "config", "user.name", "t"],
                    ["git", "commit", "-q", "--allow-empty", "-m", "root"]):
            subprocess.run(cmd, cwd=self.repo, check=True,
                           capture_output=True)

    def test_read_only_work_verbs_run_with_NO_identity_at_all(self):  # noqa: VACUOUS_ASSERTION — its discriminating control is the sibling arm below, which runs the MUTATING verb in the identical state and asserts rc 2
        for verb in ("list", "gc"):
            rc, _out, err = self.work(verb)
            self.assertNotEqual(
                rc, 2, "helm work %s refused an env-less operator: %s\n"
                       "A read-only verb takes no identity, and denying it for "
                       "lacking one is the overguard this lane already shipped "
                       "once." % (verb, err))

    def test_the_lease_verb_in_the_SAME_state_does_refuse(self):
        """The discrimination that makes the arm above meaningful: identical
        process, identical absence of a name, and the MUTATING verb refuses."""
        rc, _out, err = self.work("claim", "a-lane")
        self.assertEqual(rc, 2, "a lane lease must require an admitted actor")
        self.assertIn("DERIVED", err)

    def test_stale_release_takes_no_identity(self):  # noqa: VACUOUS_ASSERTION — seats.claim above runs unconditionally and puts a real row in the ledger for release_stale to answer about
        """Recovery authority is a proof about the DEAD HOLDER, so an env-less
        operator unwedging a fleet must not be refused for having no name."""
        seats.claim("res:dead", C, ttl=600, session="sid-ghost")
        ok, msg = seats.release_stale("res:dead", "")
        self.assertIsInstance(ok, bool)
        self.assertNotIn("DERIVED", msg,
                         "stale release must never ask who the caller is")


class AutoClaimNeedsMoreThanANameTest(LayerBase):
    """The ACTUATOR-CROSSING case. `seats_stop_guard` composes warn text and
    passes the seat two hops down to a rung that calls `claim()` — a lease.
    The crossing is a property of the CALL GRAPH; nothing at the call site
    looks like acquisition."""

    def test_no_actor_means_no_lease_but_the_row_is_still_surfaced(self):  # noqa: VACUOUS_ASSERTION — the sibling arm below passes a real capability path and the file-wide InstrumentTest proves the snapshot can move
        from helm import seats_work_offer as offer
        before = self.snapshot()
        got = offer._finalize_work_offer("sid-nobody", A, None, "line",
                                         actor=None)
        self.assertIsNone(got)
        self.assertUnmoved(before, "a work offer with no admitted actor took a "
                                   "lease anyway")

    def test_a_raw_name_cannot_be_passed_where_the_capability_goes(self):  # noqa: VACUOUS_ASSERTION — the assertion is on claims_list() membership, a real ledger read, and InstrumentTest proves the claims accessor moves the snapshot
        """The return value is NOT the assertion here. An offer whose landing
        state is UNKNOWN answers with a REPORT ("verify before starting; no
        lease was claimed"), which is correct and non-None — so the contract is
        the LEDGER: no lease, and no `autoclaim:` fingerprint."""
        from helm import seats_work_offer as offer
        offer_row = ("r1", "label", "take", True, "build", {})
        before = self.snapshot()
        got = offer._finalize_work_offer("sid-x", A, offer_row, "line",
                                         actor=A)
        self.assertNotIn("dispatch:r1",
                         {c["resource"] for c in seats.claims_list()},
                         "a raw seat name reached claim() through the "
                         "work-offer rung")
        if got:
            self.assertFalse(got[0].startswith("autoclaim:"),
                             "a raw name minted an auto-claim fingerprint")
        self.assertUnmoved(before, "a raw name moved the claims ledger")


class UsesClosureTest(unittest.TestCase):
    """THE CENSUS. Every call in helm/ to a resolver that returns a BARE SEAT
    NAME must be registered here with a classification.

    Enumerating call sites by hand is precisely what failed: the predecessor
    listed twelve, migrated six, and a review found six more it had
    not thought of. So this arm enumerates them MECHANICALLY and fails on any
    site the registry does not know — a new one cannot appear without somebody
    deciding whether it renders, addresses, or crosses an actuator.

    RENDER    the answer is displayed or logged and goes no further.
    ADDRESS   the answer names a THIRD PARTY (a row's source session, a
              recipient), which an admission door must never be pointed at or
              resolving a stranger's row starts failing.
    ACTUATOR  the answer reaches a durable mutation. NONE may be registered
              here: an actuator resolves through helm.actors, so a site that
              needs this classification is a defect, not an entry.
    """

    RESOLVERS = ("derive_seat", "acting_seat", "whoname")

    # THE LAYER ITSELF IS NOT A CALLER OF THE LAYER. seats_identity DEFINES
    # these three and actors composes them; registering their own internal
    # calls would be registering the implementation against itself.
    EXEMPT = ("helm/seats_identity.py", "helm/actors.py")

    # (module, resolver, enclosing def) -> (classification, why).
    #
    # RENDER   the answer is displayed or logged and goes no further.
    # ADDRESS  the answer names a THIRD PARTY. An admission door must never be
    #          pointed here or resolving a stranger's row starts failing.
    # BIND     the site where a minted name legitimately BECOMES an identity.
    #          Exactly one exists and naming it is better than hiding it in
    #          RENDER, because it is the only place DERIVED is the input.
    # ACTUATOR forbidden — see the arm below. An actuator resolves through
    #          helm.actors, so needing this value is a defect, not an entry.
    REGISTRY = {
        ("helm/chat.py", "whoname", "post"): (
            "RENDER", "the PROGRAMMATIC floor only. The CLI leg resolves an "
            "actor through chat._seat_actor and refuses; what survives here is "
            "the in-process path seats.auto_name was built for — a join hook "
            "naming a session it is in the act of binding. A refusal here "
            "would refuse the row that creates the identity."),
        ("helm/chat.py", "whoname", "react"): (
            "RENDER", "same programmatic floor as post; the CLI leg above it "
            "resolves an actor through chat._seat_actor and refuses."),
        ("helm/chat.py", "whoname", "cmd_chat"): (
            "ADDRESS", "names the DM ROOM to read — a mailbox, not an actor; "
            "the signing branches of this dispatcher go through _seat_actor."),
        ("helm/fleetnotes.py", "whoname", "_actor"): (
            "RENDER", "a note's identity is its KEY; `by` is a display label "
            "saying who left it, in the same name the room knows."),
        ("helm/gateimport.py", "acting_seat", "_self_actor"): (
            "RENDER", "the one site where the MINTED name is the right answer, "
            "and the repo pins it: importer shells declare no HELM_CHAT_NAME, "
            "so requiring an admitted actor would put the OS user back into a "
            "provenance field or empty it for the ordinary case. Routing this "
            "through the layer was tried and reverted — see "
            "tests/test_gate_import.ActorResolutionTest."),
        ("helm/idle_dispatch.py", "derive_seat", "scan"): (
            "ADDRESS", "resolves a ROW's own source session to a name — a "
            "third party. Pointing an admission door here breaks it."),
        ("helm/landgate.py", "acting_seat", "cmd_landgate"): (
            "RENDER", "landgate asks its five clauses and lands nothing — the "
            "name appears in the answer it prints and reaches no store."),
        ("helm/meld.py", "derive_seat", "_self_seat"): (
            "RENDER", "names a meld participant; membership is policed by the "
            "seed's own invited-set allowlist, which a minted name fails."),
        ("helm/multiplayer.py", "whoname", "actor_name"): (
            "RENDER", "the multiplayer surface's display actor, overridable by "
            "HELM_MULTIPLAYER_ACTOR — a presence label in a cave, not an "
            "authority over anything another party reads as testimony."),
        ("helm/seats_ack.py", "derive_seat", "pending"): (
            "RENDER", "LISTS pending rows and mutates nothing."),
        ("helm/seats_delivery.py", "acting_seat", "_own_delivery_seat"): (
            "BIND", "the SPEECH floor under the delivery door. Delivery is "
            "where an unrostered session acquires a roster row at all, so "
            "refusing DERIVED is a lockout loop; `auto_name` dedupes against "
            "the roster, so the minted name is provably not a live seat's. "
            "MALFORMED and DISPUTED still refuse above it."),
        ("helm/seats_delegation.py", "acting_seat", "_record_posttool_delegation"): (
            "BIND", "same floor: this records THIS process's own work in a "
            "room whose claim was already bound, and refusing would DELETE "
            "the evidence rather than misattribute it."),
        ("helm/work/_cli.py", "acting_seat", "cmd_work"): (
            "ADDRESS", "`helm work release` names the HOLDER ROW to release. "
            "The authority there is the lease token, not a name — only the "
            "holder was handed the nonce — so requiring an actor on top "
            "refused a seat following the exact command its own guard "
            "printed."),
        ("helm/seats_claims.py", "acting_seat", "own_leases"): (
            "ADDRESS", "filters the ledger to rows a name holds — a read."),
        ("helm/seats_cli.py", "acting_seat", "_cmd_claims"): (
            "RENDER", "`helm chat claims` prints the live lease board and "
            "joins in the tokens for rows THIS name holds; a minted name "
            "matches no row, so it renders an empty own-lease column."),
        ("helm/seats_cli.py", "acting_seat", "cmd"): (
            "RENDER", "seat labels on read surfaces; every mutating branch of "
            "this dispatcher resolves through helm.actors instead."),
        ("helm/seats_join.py", "acting_seat", "join"): (
            "BIND", "THE mint. A join is where a derived name becomes a "
            "rostered identity — refusing DERIVED here would refuse every "
            "first join, which is the case auto_name was built for. The "
            "disagreement law still refuses above it, and --seat is compared "
            "against resolve_identity rather than against one source."),
        ("helm/seats_join.py", "acting_seat", "wait"): (
            "RENDER", "the resolved name only labels the wait; arming the "
            "beacon and consuming the inbox go through "
            "_beacon_identity_refusal, which resolves an actor when the "
            "caller did not supply a seat."),
        ("helm/seats_stop_guard.py", "derive_seat", "_stop_guard"): (
            "RENDER", "warn text. The ACTUATOR two hops down (autoclaim) "
            "takes a separately resolved actor, never this name."),
        ("helm/seats_stop_signals.py", "acting_seat", "_dispatch_candidate"): (
            "RENDER", "whisper text for an overdue dispatch."),
    }

    def _sites(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        found = []
        for base, dirs, files in os.walk(os.path.join(root, "helm")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                p = os.path.join(base, f)
                rel = os.path.relpath(p, root)
                if rel in self.EXEMPT:
                    continue
                try:
                    tree = ast.parse(open(p, encoding="utf-8").read())
                except SyntaxError:            # pragma: no cover
                    continue

                def walk(node, enclosing):
                    for child in ast.iter_child_nodes(node):
                        name = enclosing
                        if isinstance(child, (ast.FunctionDef,
                                              ast.AsyncFunctionDef)):
                            name = child.name
                        if isinstance(child, ast.Call):
                            fn = child.func
                            attr = getattr(fn, "attr", None) \
                                or getattr(fn, "id", None)
                            if attr in self.RESOLVERS:
                                found.append((rel, attr, name, child.lineno))
                        walk(child, name)

                walk(tree, "<module>")
        return found

    def test_the_census_finds_the_sites_it_must(self):
        """MUST-HIT: a census that found nothing would pass the arm below
        vacuously. Seed it with sites known to exist."""
        sites = {(m, r, fn) for m, r, fn, _ln in self._sites()}
        for must in (("helm/idle_dispatch.py", "derive_seat", "scan"),
                     ("helm/chat.py", "whoname", "post")):
            self.assertIn(must, sites,
                          "the census cannot see a site that is definitely "
                          "there — it is measuring the wrong thing")
        self.assertGreater(len(sites), 5)

    def test_every_bare_resolver_call_is_CLASSIFIED(self):  # noqa: VACUOUS_ASSERTION — its must-hit control is the arm above, which asserts the census FINDS two sites known to exist
        unregistered = sorted({(m, r, fn) for m, r, fn, _ln in self._sites()}
                              - set(self.REGISTRY))
        self.assertEqual(
            unregistered, [],
            "these calls return a BARE SEAT NAME and nobody has classified "
            "them. A name is render/addressing material — `derive_seat` hands "
            "one back for a stranger it just minted. If the answer reaches a "
            "durable mutation, route it through helm.actors.resolve_actor; if "
            "it only renders or addresses a third party, add it to REGISTRY "
            "with that classification and say why in the commit.\n  %s"
            % "\n  ".join(map(repr, unregistered)))

    def test_every_entry_says_WHY(self):  # noqa: VACUOUS_ASSERTION — REGISTRY is a module-level literal that is never empty; the arm above asserts the census matches it
        """A classification with no reason is a decision nobody can review —
        and reviewing these is the whole point of the registry."""
        for key, val in self.REGISTRY.items():
            self.assertEqual(len(val), 2, key)
            self.assertIn(val[0], ("RENDER", "ADDRESS", "BIND"), key)
            self.assertGreater(len(val[1]), 30,
                               "%r has no real justification" % (key,))

    # The capability types. Naming the class is not the offence — a docstring,
    # an isinstance check and a type annotation all name it. CALLING it is.
    MINTS = ("AdmittedActor", "StaleReleaseProof", "OnBehalfCapability")

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="helm-test-forge-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _constructions_in(self, root, exempt=()):
        """Every direct CALL of a capability class under `root`.

        `AdmittedActor(...)` is the whole of the forgery: read-only fields, a
        tagged `__str__`, never `==` a string, and `actor_name()` refusing a
        raw string by type are all walked around by anyone who simply calls
        the class. Its own docstring claimed "no public constructor contract",
        which is a guarantee's form without its substance — the exact thing a
        census exists to make impossible to write.

        Parameterised on `root` so the arm below can hand it a tree it BUILT
        and require a nonzero. A walker only ever exercised against a tree
        that should be clean is a walker whose zero nobody has tested."""
        found = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in ("__pycache__", ".git", ".fab-artifacts")]
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(base, f)
                rel = os.path.relpath(path, root)
                if rel in exempt:
                    continue
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except (SyntaxError, OSError):    # pragma: no cover
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name in self.MINTS:
                        found.append((rel, name, node.lineno))
        return found

    def _constructions(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # the layer IS the mint; everywhere else calling it is the forgery
        return self._constructions_in(root,
                                      exempt=(os.path.join("helm", "actors.py"),))

    def test_the_construction_census_can_SEE_a_call(self):  # noqa: VACUOUS_ASSERTION — this arm IS the must-hit for the one below; its assertion is a presence assertion over a file it writes itself
        """MUST-HIT, and it is a real one: this arm PLANTS a forging call in a
        temp tree and requires the walker to find it. Without this, the arm
        below could pass because the walker looks in the wrong place, matches
        the wrong node type, or skips the wrong directory — a green that means
        "I found nothing" rather than "there is nothing"."""
        planted = os.path.join(self.tmpdir, "forge_probe.py")
        with open(planted, "w") as fh:
            fh.write("from helm.actors import AdmittedActor\n"
                     "x = AdmittedActor('not-an-id', 'seat-a')\n")
        hits = [h for h in self._constructions_in(self.tmpdir)
                if h[0].endswith("forge_probe.py")]
        self.assertEqual(len(hits), 1,
                         "the walker cannot see a call it was handed — every "
                         "zero it reports is meaningless")
        self.assertEqual(hits[0][1], "AdmittedActor")

    def test_NOBODY_OUTSIDE_THE_LAYER_CONSTRUCTS_A_CAPABILITY(self):  # noqa: VACUOUS_ASSERTION — its must-hit is the planted-call arm above, which proves the walker reports a nonzero when a call exists
        """The forgery census. A capability that anyone can construct is not a
        capability — it is a struct with a strongly-worded docstring."""
        forged = sorted(self._constructions())
        self.assertEqual(
            forged, [],
            "these call a capability class directly, which mints authority "
            "without passing the admission that makes it mean anything. Use "
            "helm.actors.resolve_actor / resolve_speaker; a fixture that "
            "needs one for a negative arm should assert the mint REFUSES it."
            "\n  %s" % "\n  ".join(map(repr, forged)))

    def test_no_site_is_registered_as_an_ACTUATOR(self):  # noqa: VACUOUS_ASSERTION — the registry is proven non-empty and correctly-shaped by the two arms above
        """An actuator resolves through the identity layer. A registry entry
        claiming otherwise is a defect that has been written down instead of
        fixed."""
        self.assertEqual(
            [k for k, v in self.REGISTRY.items() if v[0] == "ACTUATOR"], [])


class InheritedPinThroughRenameAliasTest(LayerBase):
    """task/2338 — the actor door admits a retained session whose declared
    name is a LIVE rename alias, AS the renamed seat, and says so; the same
    pin with no alias or an expired one is the TAKEOVER dispute it always was."""

    def _renamed(self):
        self.declare(A)                       # HELM_CHAT_NAME=A, rostered
        sid = os.environ["CLAUDE_CODE_SESSION_ID"]
        first, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(err)                # mint A's record BEFORE the
        self.assertEqual(first.canonical_name, A)   # rename: lineage is the
        self.first_id = first.actor_id
        ok, msg = seats.rename_seat(A, B)     # store's claim, not the roster's
        self.assertTrue(ok, msg)              # the row is B now; env still A
        return sid

    def test_declared_old_name_is_admitted_as_the_new_seat(self):
        sid = self._renamed()
        actor, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(err)
        self.assertEqual(actor.canonical_name, B)
        self.assertEqual(actor.actor_id, self.first_id, "one actor, relabelled")
        self.assertTrue(actor.is_named(A), "lineage: the old name still names it")
        kinds = dict(actor.evidence)
        self.assertIn("declared-alias", kinds)
        self.assertIn("HELM_CHAT_NAME=%s is a rename alias of %s until" % (A, B),
                      kinds["declared-alias"])
        # --seat asserts against the RESOLVED name, either spelling
        for asserted in (A, B):
            actor, err = actors.resolve_actor(sid, self.tmp, asserted=asserted,
                                              act="claim")
            self.assertIsNone(err, asserted)
        _actor, err = actors.resolve_actor(sid, self.tmp, asserted=C, act="claim")
        self.assertIn("cannot claim as another seat", err)

    def test_an_expired_or_absent_alias_is_the_takeover_dispute(self):
        sid = self._renamed()
        r = seats.roster()
        r[B][seats.RENAME_ALIAS_FIELD]["until"] = pk.epoch_ts(time.time() - 1)
        pk.write_json(seats.roster_path(), r)
        actor, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("disputed identity", err)
        self.assertIn("declares %r but session" % A, err)
        # CONTROL: a rename with no window never admitted the pin at all
        self.declare(C)
        ok, msg = seats.rename_seat(C, "seat-d", alias_hours=0)
        self.assertTrue(ok, msg)
        actor, err = actors.resolve_actor(os.environ["CLAUDE_CODE_SESSION_ID"],
                                          self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("disputed identity", err)


class ReAdmittedNameGetsItsOwnActorTest(LayerBase):
    """CL99 (store half): an exact roster key beats a retired alias
    in the actor store exactly as in the roster. After A -> B relabels A's
    record, a fresh session lawfully re-admitted as A must resolve to its
    OWN actor, never B's; the renamed seat keeps its record; the retained
    A pin on the old sid is a stranger again."""

    def _renamed(self):
        self.declare(A)
        sid = os.environ["CLAUDE_CODE_SESSION_ID"]
        first, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(err)
        self.first_id = first.actor_id
        ok, msg = seats.rename_seat(A, B)
        self.assertTrue(ok, msg)
        return sid

    def test_an_exact_old_row_gets_its_own_actor_not_the_renamed_one(self):
        """CL99 (store half): where an exact old row exists (owner
        re-admit or post-window), bind(A) must not hand back the record it
        relabelled to B. Construct that world and resolve A's own sid."""
        sid = self._renamed()
        os.environ["HELM_CHAT_NAME"] = A
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-reuse"
        r = seats.roster()
        r[B][seats.RENAME_ALIAS_FIELD]["until"] = pk.epoch_ts(time.time() - 1)
        pk.write_json(seats.roster_path(), r)     # window closed: A is a name
        seats.write_roster(A, session="sid-reuse", cwd=self.tmp)
        occupant, err = actors.resolve_actor("sid-reuse", self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual(occupant.canonical_name, A)
        self.assertNotEqual(occupant.actor_id, self.first_id,
                            "the occupant was handed the renamed seat's actor")
        self.assertFalse(occupant.is_named(B))
        again, err = actors.resolve_actor("sid-reuse", self.tmp, act="claim")
        self.assertEqual((again and again.actor_id), occupant.actor_id, "stable")
        os.environ["HELM_CHAT_NAME"] = B
        renamed, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(err)
        self.assertEqual((renamed.canonical_name, renamed.actor_id),
                         (B, self.first_id))
        self.assertTrue(renamed.is_named(A), "lineage kept on the record")

    def test_an_unreadable_roster_at_bind_refuses_never_another_generations_actor(self):
        """With A's record relabelled to B and an exact A row present, a read
        failure at the ONE reread that decides exact-key-or-alias must not
        answer False and hand the occupant B's actor: UNKNOWN refuses, and
        readable again binds the occupant's own actor."""
        self._renamed()
        os.environ["HELM_CHAT_NAME"] = A
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-reuse"
        r = seats.roster()
        r[B][seats.RENAME_ALIAS_FIELD]["until"] = pk.epoch_ts(time.time() - 1)
        pk.write_json(seats.roster_path(), r)
        seats.write_roster(A, session="sid-reuse", cwd=self.tmp)
        # THE FAILURE IS INJECTED BEFORE THE FIRST OCCUPANT BIND: a successful
        # bind rewrites by_name[A] to the occupant's own record, after which
        # the canonical-name early return never consults the roster again
        # so the arm must fail BEFORE it. The spy proves the checked reader ran.
        from helm import seats_roster
        calls = []

        def failing():
            calls.append(1)
            return {}, "EIO on the reread"
        with mock.patch.object(seats_roster, "roster_checked", failing):
            row, err = actors.bind(A, ())
        self.assertEqual(len(calls), 1, "the checked reader was not consulted")
        self.assertIsNone(row, "a read failure became permission")
        self.assertIn("UNKNOWN", err)
        self.assertIn("roster unreadable", err)
        self.assertIn(self.first_id[:8], err)     # names the retired record
        store, _un = actors.read_store()
        self.assertEqual(store["by_name"].get(A.casefold()), self.first_id,
                         "a refusal must not rewrite the index")
        row, err = actors.bind(A, ())            # readable again: the occupant
        self.assertIsNone(err)
        self.assertEqual(row["canonical_name"], A)
        self.assertNotEqual(row["actor_id"], self.first_id)

    def test_during_the_window_the_old_pin_is_the_renamed_seat(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(err) is corroborated by the canonical_name/actor_id equality on the SAME resolve two lines down
        """The reserved-name law: while the window is open a process declaring
        A IS the renamed seat B (its actor), never a stranger with A's name."""
        sid = self._renamed()
        os.environ["HELM_CHAT_NAME"] = A          # env still A, alias live
        actor, err = actors.resolve_actor(sid, self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual((actor.canonical_name, actor.actor_id),
                         (B, self.first_id))

    def test_preflight_refuses_a_merge_before_the_roster_commit(self):  # noqa: VACUOUS_ASSERTION — the refusals are paired with the RELABEL and NO_RECORD proceeds (assertTrue ok) later in the same arm, over the same door
        """The three answers must not collapse: CONFLICT and UNAVAILABLE are
        refusals with different sentences; NO_RECORD and RELABEL proceed."""
        self.declare(B)
        _b, err = actors.resolve_actor(os.environ["CLAUDE_CODE_SESSION_ID"],
                                       self.tmp, act="claim")
        self.assertIsNone(err)                     # B has its own record
        r = seats.roster()
        r.pop(B)                                   # ...but no roster row
        pk.write_json(seats.roster_path(), r)
        self.declare(A)
        _a, err = actors.resolve_actor(os.environ["CLAUDE_CODE_SESSION_ID"],
                                       self.tmp, act="claim")
        self.assertIsNone(err)
        ok, msg = seats.rename_seat(A, B)
        self.assertFalse(ok)
        self.assertIn("already names a different actor", msg)
        self.assertIn(A, seats.roster())
        self.assertNotIn(B, seats.roster())        # nothing was published
        ok, msg = seats.rename_seat(A, B, dry_run=True)
        self.assertFalse(ok)                       # the dry-run says so too
        self.assertIn("already names a different actor", msg)
        # UNAVAILABLE is a different sentence from NO_RECORD
        store = actors.store_path()        # captured NOW: tearDown restores
        os.chmod(store, 0)                 # HELM_HOME before cleanups run
        self.addCleanup(os.chmod, store, 0o600)
        ok, msg = seats.rename_seat(A, C)
        self.assertFalse(ok)
        self.assertIn("actor store unreadable", msg)
        self.assertNotIn("no actor record", msg)
        os.chmod(actors.store_path(), 0o600)
        ok, msg = seats.rename_seat(A, C)          # RELABEL proceeds
        self.assertTrue(ok, msg)
        self.assertIn("is now %r" % C, msg)
        # NO_RECORD proceeds and says so
        os.environ.pop("HELM_CHAT_NAME", None)
        self.roster("seat-d", "sid-d")
        ok, msg = seats.rename_seat("seat-d", "seat-e")
        self.assertTrue(ok, msg)
        self.assertIn("no actor record for", msg)


class UncorroboratedDeclaredNameTest(LayerBase):
    """A DECLARED NAME IS A VALUE ANY PROCESS CAN EXPORT, SO ON ITS OWN IT IS
    NOT EVIDENCE.

    The disagreement check is what makes a declared name mean something, and it
    works by COMPARING two sources. With no rostered session it has one, so it
    returns nothing -- which is byte-identical to the two sources agreeing.
    Reading that silence as corroboration is how a bare `HELM_CHAT_NAME` became
    sufficient to ACT.

    THE SPEECH DOOR MUST NOT MOVE, and that is not a nicety: the owed-push and
    stale bot units set a name and UNSET the session vars on purpose, precisely
    so their declared identity is not a DISPUTE against an inherited session.
    They only ever post, and they reach chat through the on-behalf-of string
    branch rather than this pass. A refusal that reached them would silence the
    fleet's own reporting.
    """

    def test_a_declared_name_with_NO_session_cannot_act(self):  # noqa: VACUOUS_ASSERTION — the corroborated same-name arm below unconditionally writes the Actor store; this arm distinguishes refusal by requiring that exact file remain absent
        self.declare(A, corroborate=False)
        before = self.snapshot()
        self.assertFalse(os.path.exists(actors.store_path()),
                         "control: the refused name was already an Actor")
        actor, err = actors.resolve_actor(None, self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("nothing corroborates it", err)
        self.assertIn("no session was presented", err)
        self.assertUnmoved(before, "an uncorroborated name committed an Actor")
        self.assertFalse(os.path.exists(actors.store_path()))

    def test_an_unrelated_Actor_store_stays_byte_identical_until_corroborated(self):  # noqa: VACUOUS_ASSERTION — the same name commits into the existing store after only its roster corroboration changes
        unrelated, err = actors.bind(B)
        self.assertIsNone(err, err)
        self.assertEqual(unrelated["canonical_name"], B)
        with open(actors.store_path(), "rb") as fh:
            committed = fh.read()
        self.declare(A, corroborate=False)

        actor, err = actors.resolve_actor(None, self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("nothing corroborates it", err)
        with open(actors.store_path(), "rb") as fh:
            self.assertEqual(fh.read(), committed,
                             "the refused name changed unrelated Actor authority")
        self.assertIsNone(actors.lookup(A))

        self.roster(A, SID)
        before = self.snapshot()
        actor, err = actors.resolve_actor(SID, self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)
        self.assertWrote(before, actors.store_path(),
                         "the corroborated name did not join the Actor store")
        self.assertEqual(actors.lookup(B)["actor_id"], unrelated["actor_id"])

    def test_a_declared_name_with_an_UNROSTERED_session_cannot_act(self):  # noqa: VACUOUS_ASSERTION — the corroborated same-name arm below unconditionally writes the Actor store; this arm varies only whether the presented session has a roster fact
        """THE ONE-VARIABLE BYPASS. A guard that asks only whether a session
        STRING is present is satisfied by exporting one more variable -- the
        same move it exists to stop."""
        self.declare(A, corroborate=False)
        before = self.snapshot()
        actor, err = actors.resolve_actor("sid-nobody-ever-rostered", self.tmp,
                                          act="claim")
        self.assertIsNone(actor)
        self.assertIn("not on the roster", err)
        self.assertUnmoved(before, "an unrostered session committed an Actor")
        self.assertFalse(os.path.exists(actors.store_path()))

    def test_actor_store_failure_still_outranks_UNCORROBORATED(self):  # noqa: VACUOUS_ASSERTION — the malformed store is unconditionally present before resolution and the neighbouring absent-store arm proves the opposite precedence input
        self.declare(A, corroborate=False)
        path = actors.store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not-json")
        before = self.snapshot()
        actor, err = actors.resolve_actor(None, self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("MALFORMED", err)
        self.assertNotIn("nothing corroborates it", err)
        self.assertUnmoved(before, "the precedence refusal rewrote its store")

    def test_the_corroboration_is_not_read_from_a_source_that_reads_the_NAME(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the assertEqual above it: resolve_identity must ECHO the declared name, which proves the circularity is live before the refusal is asserted
        """THE SELF-ANSWERING TRAP: `resolve_identity` consults
        HELM_CHAT_NAME itself, so comparing ITS answer against the declared
        name compares the name to itself and admits everything.

        The first assertion proves the circularity is real rather than
        theoretical; the second proves the door does not stand on it."""
        self.declare(A, corroborate=False)
        state, name = resolve_identity("sid-nobody-ever-rostered", self.tmp)
        self.assertEqual((state, name), ("declared", A),
                         "if this stops echoing the declared name, the trap "
                         "this arm guards has moved and the arm needs rewriting")
        actor, _err = actors.resolve_actor("sid-nobody-ever-rostered", self.tmp,
                                           act="claim")
        self.assertIsNone(actor,
                          "the door admitted on an answer derived from the "
                          "very value it was checking")

    def test_a_declared_name_WITH_its_rostered_session_still_acts(self):  # noqa: VACUOUS_ASSERTION — this arm includes both outcomes on actors.store_path: refusal leaves it absent, then roster corroboration unconditionally writes it
        """THE CONTROL THAT MATTERS MOST: without it the cure could be
        "refuse every declared identity", which passes all three arms above
        and takes the fleet down."""
        self.declare(A, corroborate=False)
        refused_before = self.snapshot()
        _a0, err0 = actors.resolve_actor(SID, self.tmp, act="claim")
        self.assertTrue(err0, "control: this shape must refuse before the "
                              "roster write, or the arm proves nothing")
        self.assertUnmoved(refused_before,
                           "the refusal pre-created the positive Actor")
        self.assertFalse(os.path.exists(actors.store_path()))
        self.roster(A, SID)
        admitted_before = self.snapshot()
        actor, err = actors.resolve_actor(SID, self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)
        self.assertWrote(admitted_before, actors.store_path(),
                         "the corroborated admission did not commit its Actor")

    def test_first_admission_rechecks_corroboration_at_the_commit_seam(self):
        self.declare(A)
        session = os.environ["CLAUDE_CODE_SESSION_ID"]
        from helm import seats_roster
        real_lookup = seats_roster.seat_for_session
        calls = []

        def withdraw_after_check(sid):
            calls.append(sid)
            hit = real_lookup(sid)
            if len(calls) == 1:
                roster = seats.roster()
                roster[A].pop("session", None)
                roster[A].pop("sessions", None)
                pk.write_json(seats.roster_path(), roster)
            return hit

        with mock.patch.object(seats_roster, "seat_for_session",
                               side_effect=withdraw_after_check):
            actor, err = actors.resolve_actor(session, self.tmp, act="claim")
        self.assertEqual(calls, [session, session],
                         "the commit seam did not re-read corroboration")
        self.assertIsNone(actor)
        self.assertIn("nothing corroborates it", err)
        self.assertFalse(os.path.exists(actors.store_path()),
                         "withdrawn corroboration still committed an Actor")

        self.roster(A, session)
        before = self.snapshot()
        actor, err = actors.resolve_actor(session, self.tmp, act="claim")
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)
        self.assertWrote(before, actors.store_path(),
                         "the restored corroboration did not commit its Actor")

    def test_a_ROSTERED_session_naming_someone_else_is_still_DISPUTED(self):
        """Ordering: the new refusal must sit BEHIND the disagreement check, so
        the case with a better message keeps it."""
        self.roster(B, SID)
        self.declare(A, corroborate=False)
        actor, err = actors.resolve_actor(SID, self.tmp, act="claim")
        self.assertIsNone(actor)
        self.assertIn("disputed", err)
        self.assertNotIn("nothing corroborates it", err)

    def test_SPEECH_is_untouched_because_the_bots_live_in_this_shape(self):  # noqa: VACUOUS_ASSERTION — the second half is the unconditional positive control on the same door: a corroborated speaker resolves AS itself, so the None above is not the door returning None to everything
        """The bot identities declare a name and unset their session on
        purpose. Speech drops this refusal exactly as it drops DERIVED."""
        self.declare(A, corroborate=False)
        refused_before = self.snapshot()
        speaker, err = actors.resolve_speaker(None, self.tmp, act="post")
        self.assertIsNone(err)
        self.assertIsNone(speaker, "an uncorroborated name speaks on the "
                                   "ambient floor, it does not carry the name")
        self.assertUnmoved(refused_before,
                           "ambient speech committed an uncorroborated Actor")
        self.assertFalse(os.path.exists(actors.store_path()))
        self.roster(A, SID)
        admitted_before = self.snapshot()
        ok, okerr = actors.resolve_speaker(SID, self.tmp, act="post")
        self.assertIsNone(okerr)
        self.assertEqual(ok.canonical_name, A,
                         "control: a corroborated speaker still speaks AS "
                         "itself, so the door is not simply returning None")
        self.assertWrote(admitted_before, actors.store_path(),
                         "the corroborated speaker did not commit its Actor")

    def test_the_bot_units_really_are_this_shape(self):
        """The population this refusal must not reach, read from the units
        themselves rather than assumed. If a unit ever stops unsetting its
        session vars, the reasoning above stops holding and this reddens."""
        import glob
        roots = os.path.dirname(os.path.dirname(os.path.abspath(actors.__file__)))
        found = {}
        for path in sorted(glob.glob(os.path.join(roots, "helm", "*.py"))):
            with io.open(path, encoding="utf-8") as fh:
                text = fh.read()
            if "Environment=HELM_CHAT_NAME=" in text:
                found[os.path.basename(path)] = "UnsetEnvironment=" in text
        self.assertTrue(found, "no bot unit was found at all; this arm is "
                               "reading the wrong tree and proves nothing")
        self.assertEqual(sorted(k for k, v in found.items() if not v), [],
                         "a unit declares a seat name without unsetting the "
                         "session vars, so its declared name is a DISPUTE and "
                         "the speech-door reasoning no longer covers it: %s"
                         % found)


if __name__ == "__main__":
    unittest.main()


class DeclaringHelperCasefoldTest(LayerBase):
    """THE FIXTURE HELPERS OWE PRODUCTION'S IDENTITY RELATION, NOT PYTHON'S.

    `write_roster` keeps ONE canonical key per casefold-equivalent name and
    RAISES on case-variant duplicates; `identity_disagreement` compares a
    declared name against its bound one with casefold; `recipient_matches` is
    the exact-canonical-token relation the routing layer already uses. A
    helper that looks a seat up by exact spelling can therefore miss the row
    that already exists, abandon the session THAT ROW GRANTS, and insert a
    SECOND key for one identity — a state the production writer refuses, which
    fails the fixture before it reaches its subject.

    THE HELPER STAYS BINDING-ONLY. `write_roster` would also maintain session
    history, incarnation, home, cursors, joined and last_seen, and publish
    authority — the joined/listed state the undo exists to avoid changing. So
    these arms pin the ONE key and the canonical row, never a whole-row
    restore.
    """

    SEAT, SID = "kimi", "s" * 32

    def _rows(self):
        return pk.read_json(seats.roster_path(), {}) or {}

    def _canon(self, name=None):
        want = str(name or self.SEAT).casefold()
        return [k for k in self._rows() if str(k).casefold() == want]

    def _seed_bound_row(self):
        seats.write_roster(self.SEAT, session=self.SID, cwd=self.tmp)

    # ---------------- the adoption half ----------------

    def test_a_case_variant_declaration_keeps_the_GRANTING_session(self):  # noqa: VACUOUS_ASSERTION — the first assertEqual is the unconditional positive control on the same observable: the EXACT spelling must adopt the same session, so a helper that adopted nothing fails there before the case-variant assertion is reached
        """B1a. `_own_session` adopts the ambient session only when the roster
        says it belongs to this seat, and "belongs" is the casefold relation.
        Rejecting it mints a replacement, and the release door then refuses
        with "release needs the granting session" — the exact failure this
        helper was written to end, reappearing on the spelling axis."""
        self._seed_bound_row()
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        self.assertEqual(_tmphome._own_session(self.SEAT), self.SID,
                         "control: the exact spelling must adopt it, or this "
                         "arm cannot tell case from a broken adoption")
        self.assertEqual(_tmphome._own_session("Kimi"), self.SID)

    def test_declaring_preserves_the_presented_session_inside_the_block(self):
        """B1. THE WHOLE PATH, not the helper in isolation: roster key `kimi`,
        declared spelling `Kimi`, and the presented session S already bound to
        `kimi`. S must still be the session INSIDE the block, the roster must
        hold one canonical row, and a real roster writer must still be
        reachable — the last of those is what the duplicate-key OSError
        takes away."""
        self._seed_bound_row()
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        with _tmphome.declaring(["--seat", "Kimi"]):
            self.assertEqual(os.environ.get("CLAUDE_CODE_SESSION_ID"),
                             self.SID,
                             "the helper replaced the presented session")
            self.assertEqual(self._canon(), [self.SEAT],
                             "one identity, one row: %r" % (sorted(self._rows()),))
            seats.write_roster("Kimi", cwd=self.tmp)   # must not raise
            self.assertEqual(self._canon(), [self.SEAT])

    # ---------------- the seeding half ----------------

    def test_corroborate_seeds_the_REQUESTED_session_under_the_existing_key(self):  # noqa: VACUOUS_ASSERTION — the arm's own second assertion IS the unconditional positive control — an equality on the VALUE the helper was asked to write, which a no-op helper fails
        """B2. THE POSITIVE CONTROL A NO-OP HELPER FAILS. Asserting the key
        set and later writeability alone is satisfied by a helper that returns
        without doing anything, so the value it was asked to bind is the
        assertion that carries the property."""
        self._seed_bound_row()
        other = "t" * 32
        undo = _tmphome.corroborate("Kimi", other)
        self.addCleanup(undo)
        self.assertEqual(self._canon(), [self.SEAT],
                         "the helper must keep the roster's existing spelling "
                         "as the key: %r" % (sorted(self._rows()),))
        self.assertEqual(self._rows()[self.SEAT].get("session"), other,
                         "the requested session was not seeded")

    def test_an_absent_row_is_seeded_under_the_spelling_given(self):  # noqa: VACUOUS_ASSERTION — the assertion is an equality on the session VALUE written, not an absence; a helper that seeded nothing fails it
        """B3. With nothing to match, the caller's spelling IS the canonical
        key — the arm that stops the lookup from being read as "only ever edit
        an existing row"."""
        undo = _tmphome.corroborate("newseat", self.SID)
        self.addCleanup(undo)
        self.assertEqual(self._rows().get("newseat", {}).get("session"),
                         self.SID)

    def test_an_AMBIGUOUS_roster_is_declined_and_nothing_is_seeded(self):
        """B4. Production refuses a roster holding two case-variant rows and
        sends the operator to `seat rename`. A fixture that picked one would
        bind onto whichever row it happened to sort to, and a fixture that
        added a third would make the repair worse."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({"kimi": {"home": "a"},
                                          "Kimi": {"home": "b"}}))
        undo = _tmphome.corroborate("KIMI", self.SID)
        self.addCleanup(undo)
        rows = self._rows()
        self.assertEqual(sorted(rows), ["Kimi", "kimi"],
                         "declining must not add a third row")
        self.assertNotIn("session", rows["kimi"])
        self.assertNotIn("session", rows["Kimi"])

    # ---------------- the undo half ----------------

    def test_undo_keeps_what_the_block_added(self):
        """B5. The block between seed and undo is the CLI call, and it
        legitimately writes to this seat's own row — a `join` creates the real
        row with its cursor and home. Restoring the row wholesale deleted
        that."""
        self._seed_bound_row()
        undo = _tmphome.corroborate("Kimi", "t" * 32)
        rows = self._rows()
        rows[self.SEAT]["home"] = "/somewhere"
        pk.atomic_write(seats.roster_path(), json.dumps(rows))
        undo()
        after = self._rows()[self.SEAT]
        self.assertEqual(after.get("home"), "/somewhere")
        self.assertEqual(after.get("session"), self.SID,
                         "the prior session must come back, not vanish")

    def test_undo_leaves_a_session_the_block_replaced(self):  # noqa: VACUOUS_ASSERTION — the assertion is an equality on the block's OWN written value, not an absence; an undo that reverted it fails here
        """B6. If the block wrote its own session, that is the block's answer
        and it stands — the undo only puts back what it still owns."""
        self._seed_bound_row()
        undo = _tmphome.corroborate("Kimi", "t" * 32)
        rows = self._rows()
        rows[self.SEAT]["session"] = "u" * 32
        pk.atomic_write(seats.roster_path(), json.dumps(rows))
        undo()
        self.assertEqual(self._rows()[self.SEAT].get("session"), "u" * 32)

    def test_undo_restores_a_PRESENT_NULL_session_as_present(self):  # noqa: VACUOUS_ASSERTION — the assertEqual before undo() is the unconditional positive control — it proves the helper actually SEEDED, so an undo of nothing cannot pass this arm
        """PRESENCE AND VALUE ARE TWO FACTS. A row can carry `session: null`
        — the key there, holding no sid — and an undo that decides by VALUE
        alone reads that as absent and REMOVES the key, handing the block back
        a different row from the one the helper found. The contract is to
        restore what was there, both halves of it."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({self.SEAT: {"session": None,
                                                     "home": "/h"}}))
        undo = _tmphome.corroborate(self.SEAT, self.SID)
        self.assertEqual(self._rows()[self.SEAT].get("session"), self.SID,
                         "control: the helper must actually seed, or this arm "
                         "tests an undo of nothing")
        undo()
        row = self._rows()[self.SEAT]
        self.assertIn("session", row, "a PRESENT null was removed")
        self.assertIsNone(row["session"])
        self.assertEqual(row.get("home"), "/h")

    def test_undo_removes_a_session_key_that_was_ABSENT(self):  # noqa: VACUOUS_ASSERTION — same unconditional control: the seeded session is asserted present before undo(), so the absence afterwards is a change this arm caused rather than a state it inherited
        """The other half of the same fact: a row with NO session key gets it
        removed again, not set to null. The two rows are different and the
        undo has to tell them apart."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({self.SEAT: {"home": "/h"}}))
        undo = _tmphome.corroborate(self.SEAT, self.SID)
        self.assertEqual(self._rows()[self.SEAT].get("session"), self.SID,
                         "control: the helper must actually seed")
        undo()
        row = self._rows()[self.SEAT]
        self.assertNotIn("session", row)
        self.assertEqual(row.get("home"), "/h")

    def test_undo_does_not_resurrect_a_row_the_block_removed(self):  # noqa: VACUOUS_ASSERTION — the absence IS the property; its positive pole is the seeding arm above, which proves this helper does create the row under the same call
        """B7. A removed row is a decision, and re-creating it would hand the
        next assertion a seat the block deliberately pruned."""
        undo = _tmphome.corroborate("ghost", self.SID)
        rows = self._rows()
        rows.pop("ghost", None)
        pk.atomic_write(seats.roster_path(), json.dumps(rows))
        undo()
        self.assertNotIn("ghost", self._rows())


class _PinnedCensus:
    """THE AMBIENT REACH OF THE BEACON PATH, PINNED TO PLANTED ROOTS.

    `LayerBase` pins HELM_HOME and the chat dir, and neither of those is the
    process census. A `wait --follow` arm reaches, on the real host:

      * `beacons._proc_root` -> /proc, so `_scan` enumerates every process on
        the box and `attributable` reads their argv and environ;
      * `beacons.live_sessions` -> `sessions.live_sids()`, and
        `beacons.holder_records` -> `sessions.cred_homes()`, which is
        HOME-relative (`~/.claude`, `~/.claude-homes/*`, `~/.helm/_global/seats`)
        and therefore reads the OWNER'S REAL CREDENTIAL HOMES and the session
        JSON in them;
      * `beacons.stop_superseded` -> `os.kill`, on pids chosen from that scan.

    So the fixture is pinned at those four seams and nowhere else. HELM_PROC
    points at an EMPTY planted tree (a listable directory with no pids, which is
    "nothing running", not "unlistable"), HOME at a planted one, the two census
    functions answer synthetically, and the SIGNALLING boundary becomes a
    recorder — which is also what lets an arm assert that no signal was sent,
    a thing no arm could do while the call was real.

    WHAT IS DELIBERATELY *NOT* PINNED, because it is the subject: the roster is
    the real planted file through the shipped writer, the admission pass is
    real, and `register` is the REAL writer wrapped in a recorder, so the
    registry row is genuinely written and the observable is the row, not the
    mock."""

    @contextlib.contextmanager
    def _pinned(self):
        """Yields {"registered": [(seat, phase, wrote)], "signals": [seat]}."""
        log = {"registered": [], "signals": []}
        proc = os.path.join(self.tmp, "proc")
        home_root = os.path.join(self.tmp, "home")
        for d in (proc, home_root):
            os.makedirs(d, exist_ok=True)
        prior_proc = os.environ.get("HELM_PROC")
        prior_home = os.environ.get("HOME")
        os.environ["HELM_PROC"] = proc
        os.environ["HOME"] = home_root
        real_register = beacons.register

        def pinned_register(seat, **kw):
            row = real_register(seat, **kw)
            log["registered"].append((seat, kw.get("phase") or "active",
                                      bool(row)))
            return row

        def pinned_stop(seat, **kw):
            log["signals"].append(seat)
            return {"stopped": [], "kept": [], "pruned": []}

        try:
            with mock.patch.object(beacons, "register", pinned_register), \
                    mock.patch.object(beacons, "stop_superseded", pinned_stop), \
                    mock.patch.object(beacons, "live_sessions", lambda: {}), \
                    mock.patch.object(beacons, "holder_records", lambda: {}):
                yield log
        finally:
            for key, was in (("HELM_PROC", prior_proc), ("HOME", prior_home)):
                if was is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = was

    def _phases(self, log):
        return [phase for _seat, phase, _wrote in log["registered"]]


class SelfIdentityFromTheRosterTest(_PinnedCensus, LayerBase):
    """A PROCESS WITH NO HELM_CHAT_NAME IS NOT A PROCESS WITH NO IDENTITY.

    The owner's framing: a team USING helm must never need to know how helm is
    made, and wherever a verb assumes the helm checkout or a helm seat, that is
    the bug. The measured instance: a project pane opened by hand
    (`clientproj-claude`) is named correctly by helm's own SessionStart join
    hook — through `auto_name`, project + family — and written to the roster,
    with its session id bound to exactly one row. Then every CONSUMING door
    asked the env instead of that roster and refused it: `helm chat wait
    --seat clientproj-claude` answered "--seat 'clientproj-claude' names
    ANOTHER seat and this process declares no identity of its own, so it has
    proven nothing".

    So the roster IS the identity source when nothing is declared, and the
    derivation lives in ONE place (`actors.self_identity`, the identity layer
    itself) that every consuming door
    reads. It refuses, NAMING THE COUNT, when zero or more than one row
    matches — the exactly-one-match rule `sessions.resume_identity_env` has
    carried since it was written ("a guessed name is incident 1"), applied at
    the identity door that never adopted it.
    """

    SID_ONE = "sid-one-row-aaaa"

    def _bind(self, seat, sid):
        self.roster(seat, sid)
        return sid

    def _fd2(self, call):
        """(stderr bytes, return value) — captured at FILE DESCRIPTOR 2.

        `_warn_once` writes with `os.write(2, ...)` deliberately (a warning
        that a buffered sink swallows is not a warning), so
        `redirect_stderr` — a swap of the Python-level `sys.stderr` object —
        sees NOTHING and an arm built on it asserts the absence of its own
        blind spot. This dups the real fd."""
        with tempfile.TemporaryFile() as fh:
            saved = os.dup(2)
            try:
                os.dup2(fh.fileno(), 2)
                got = call()
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            fh.seek(0)
            return fh.read().decode("utf-8", "replace"), got

    # ---------------- (a) exactly one row ----------------

    def test_one_matching_row_IS_this_processes_identity(self):
        sid = self._bind(A, self.SID_ONE)
        self.assertEqual(seats.own_name(), None,
                         "fixture: this process must declare nothing")
        self.assertEqual(seats.seats_for_session(sid), [A],
                         "control: the roster must really bind this session")
        state, name, why = actors.self_identity(sid)
        self.assertEqual((state, name, why), (seats.ROSTERED, A, None))

    def test_the_consuming_door_admits_the_derived_seat(self):  # noqa: VACUOUS_ASSERTION — assertIsNone(err) is the ADMISSION; its discriminating controls are the zero-match and two-match arms below, which drive the identical call in the same fixture shape and assert it refuses
        """`_assert_own_seat` is the door `chat wait|deliver|catchup` share.
        RED before the cure: it read `own_name()` and nothing else."""
        sid = self._bind(A, self.SID_ONE)
        seat, err = seats._assert_own_seat(A, session=sid)
        self.assertIsNone(err, err)
        self.assertEqual(seat, A)

    def test_chat_wait_on_the_derived_seat_PROCEEDS(self):  # noqa: VACUOUS_ASSERTION — the control is test_zero_matches_keeps_the_doors_refusal_and_says_why, which runs the SAME shipped CLI and asserts rc 2 plus the refusal text
        """THE OWNER-VISIBLE OBSERVABLE, driven through the shipped CLI."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        rc, _out, err = self.cli("wait", "--seat", A, "--timeout", "0.01")
        self.assertNotIn("has proven nothing", err)
        self.assertNotIn("--on-behalf", err)
        self.assertIn(rc, (0, 1), err)

    def test_the_roster_spelling_wins_over_argv_casing(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertion is an EQUALITY on the returned spelling; the mismatch arm proves a name the roster does not bind is refused by the same call
        """Seat identity treats `Kimi` and `kimi` as one address; letting argv
        casing become the actuator key would split one seat's beacon
        election — the same reason the DECLARED branch returns `own`."""
        sid = self._bind(A, self.SID_ONE)
        seat, err = seats._assert_own_seat(A.upper(), session=sid)
        self.assertIsNone(err, err)
        self.assertEqual(seat, A)

    # ---------------- (b) zero rows ----------------

    def test_zero_matches_refuses_and_NAMES_ZERO(self):  # noqa: VACUOUS_ASSERTION — the refusal IS the property, and it is asserted positively (assertIn on the count and the sid); the one-row arm above is the unconditional control that this same call answers ROSTERED when a row exists
        self.assertEqual(seats.seats_for_session("sid-nobody"), [],
                         "fixture: nothing may bind this session")
        state, name, why = actors.self_identity("sid-nobody")
        self.assertIsNone(state)
        self.assertIsNone(name)
        self.assertIn("0 matches", why)
        self.assertIn("sid-nobo", why)

    def test_zero_matches_keeps_the_doors_refusal_and_says_why(self):  # noqa: VACUOUS_ASSERTION — assertUnmoved's positive pole is the instrument test at the top of this file, which proves the snapshot sees a write through every accessor
        before = self.snapshot()
        seat, err = seats._assert_own_seat(A, session="sid-nobody")
        self.assertIsNone(seat)
        self.assertIn("--on-behalf", err, "the refusal must name the way in")
        self.assertIn("0 matches", err)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-nobody"
        rc, _out, cli_err = self.cli("wait", "--seat", A, "--timeout", "0.01")
        self.assertEqual(rc, 2, cli_err)
        self.assertIn("0 matches", cli_err)
        self.assertUnmoved(before, "a refused wait moved a cursor or a beat")

    def test_no_session_at_all_still_refuses(self):
        """The pinned retired contract stays retired: an un-named process with
        nothing to derive FROM may not name any seat."""
        seat, err = seats._assert_own_seat(A, session=None)
        self.assertIsNone(seat)
        self.assertIn("--on-behalf", err)
        self.assertIn("0 roster matches", err)

    # ---------------- (c) two rows ----------------

    def _two_rows(self, sid="sid-two-rows-bbbb"):
        """A ROSTER FILE HOLDING ONE SID IN TWO ROWS — written as a FILE, on
        purpose, and this is the one fixture here that does not go through
        `write_roster`.

        MEASURED WHILE WRITING THIS ARM: today's writer actively HEALS the
        state. Binding a sid that another row already holds prints "session %s
        now belongs to 'seat-a' ONLY — cleared it from 'seat-b'" and drops it
        from the other row, current binding and `sessions` history alike, so
        `write_roster` cannot produce two hits at all. The state is still
        reachable — a hand-edited roster, a partial write, an older helm, a
        concurrent writer — and `seats_for_session`'s own contract calls more
        than one hit "an inconsistency, not a tie to break silently", while
        `seat_for_session` resolves it BY BINDING AGE and warns. An identity
        door must not inherit that tie-break, so the refusal is owed whether or
        not today's writer can reach the state.

        The resolver still reads the real roster through its real accessor —
        the file at `seats.roster_path()`, never a dict handed to it — and the
        control in each arm asserts the shipped resolver really sees two."""
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({
            A: {"session": sid, "sessions": [sid], "cwd": self.tmp},
            B: {"session": "sid-b-moved-on", "sessions": ["sid-b-moved-on",
                                                          sid],
                "cwd": self.tmp}}))
        return sid

    def test_two_matches_refuses_and_NAMES_TWO(self):
        sid = self._two_rows()
        self.assertEqual(sorted(seats.seats_for_session(sid)), [A, B],
                         "fixture: two rows must really hold this session")
        state, name, why = actors.self_identity(sid)
        self.assertIsNone(state)
        self.assertIsNone(name)
        self.assertIn("2 roster rows", why)
        self.assertIn(A, why)
        self.assertIn(B, why)
        self.assertIn("disown", why, "a refusal owes the repair")

    def test_two_matches_refuses_the_consuming_door(self):  # noqa: VACUOUS_ASSERTION — the control is test_the_consuming_door_admits_the_derived_seat: one row admits, two refuse, same call
        sid = self._two_rows()
        before = self.snapshot()
        seat, err = seats._assert_own_seat(A, session=sid)
        self.assertIsNone(seat, "an ambiguous binding picked a seat by age")
        self.assertIn("2 roster rows", err)
        self.assertUnmoved(before, "a refused assertion moved state")

    HOSTILE_PREFIX = "seat-\x1b[31m"

    def _hostile_second_row(self, tail="b", sid="sid-hostile-cccc"):
        """A VALID row beside a row whose KEY carries an ESC sequence.

        Written as a FILE for the same reason `_two_rows` is: the join seam
        does not validate a seat key, so this state is reachable in production
        (a hand-edited roster, a hostile HELM_CHAT_NAME at join time), while
        today's writer heals a duplicate sid and cannot produce two hits."""
        hostile = self.HOSTILE_PREFIX + tail
        path = seats.roster_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({
            A: {"session": sid, "sessions": [sid], "cwd": self.tmp},
            hostile: {"session": sid, "sessions": [sid], "cwd": self.tmp}}))
        return sid, hostile

    def test_the_ambiguity_refusal_LAUNDERS_every_candidate_key(self):  # noqa: VACUOUS_ASSERTION — the escape-text absence is bounded by THREE unconditional positives on the same observable (the refusal names two rows, carries the laundered spelling seat-[31mb, and carries seat-a byte-identical), and the substitution control in the commit message inverts it
        """THE EMISSION ARM THE COUNT-PIN OWES.

        `self_identity` is the roster's SECOND consumer in actors.py, and its
        ambiguity refusal is the one place in this module where roster KEYS are
        RENDERED — straight to an operator's terminal, in the sentence they read
        while deciding which row to disown. Every candidate goes through
        `seats_common._seat_label` first.

        WHAT IS AND IS NOT LOAD-BEARING, MEASURED RATHER THAN ASSUMED. The
        refusal wraps each name in `repr()`, and repr ALREADY escapes an ESC
        byte — so "no raw ESC reaches the terminal" is true with the laundering
        REMOVED and is therefore not a property this arm can assert. Two things
        are only true WITH it, and they are what is asserted: the operator sees
        the seat's readable spelling rather than repr's `\\x1b` escape text,
        and a key of any length is CLIPPED to `SEAT_BYTES`, so one roster row
        cannot flood the sentence the operator has to read.

        CONTROLS. (1) The hostile row really is in the roster the SHIPPED
        resolver read — asserted through `seats_for_session` before the call, so
        the absence of the escape text is laundering and not a row that never
        arrived. (2) The legitimate key survives BYTE-IDENTICAL, so a cure that
        scrubbed names wholesale goes red. BLAST RADIUS: both arms read one
        refusal string from one call, write only their own roster file in their
        own tmp home, and share no state with the arms above."""
        sid, hostile = self._hostile_second_row()
        self.assertEqual(sorted(seats.seats_for_session(sid)),
                         sorted([A, hostile]),
                         "fixture: the shipped resolver must really see the "
                         "hostile row, or this arm is about nothing")
        state, name, why = actors.self_identity(sid)
        self.assertIsNone(state)
        self.assertIsNone(name)
        self.assertIn("2 roster rows", why)
        self.assertNotIn("\\x1b", why,
                         "the operator was shown repr's escape text instead of "
                         "the laundered spelling — the key never laundered")
        self.assertIn("seat-[31mb", why,
                      "the hostile candidate vanished instead of laundering — "
                      "an operator cannot disown a row helm will not name")
        self.assertIn(A, why, "laundering is lossy: a legitimate seat key must "
                              "survive byte-identical")

    def test_one_hostile_roster_key_cannot_FLOOD_the_refusal(self):
        """THE BOUND, on the same door. `_seat_label` clips to SEAT_BYTES, so a
        400-character roster key is truncated rather than pushing the repair
        sentence off the operator's screen.

        CONTROL BLAST RADIUS: one thing changes from the arm above — the hostile
        key's length. Same fixture shape, same call, same refusal."""
        sid, hostile = self._hostile_second_row(tail="b" * 400)
        self.assertIn(hostile, seats.seats_for_session(sid),
                      "fixture: the long hostile row must really be a hit")
        why = actors.self_identity(sid)[2]
        self.assertNotIn("b" * 400, why, "an unbounded roster key flooded the "
                                         "refusal an operator must read")
        self.assertLess(len(why), 400,
                        "the refusal grew with the roster key: %d chars"
                        % len(why))
        self.assertIn("disown", why, "the clip ate the repair")

    # ---------------- (d) the env still wins ----------------

    def test_an_EXPLICIT_env_name_still_wins(self):  # noqa: VACUOUS_ASSERTION — every assertion here is an equality on a resolved name, and the roster row bound to the SAME session names a DIFFERENT seat, so a cure that preferred the roster fails on the value
        """THE MUST-HIT CONTROL. The roster becomes the source only when
        nothing is declared; a declared name is never displaced by it. The
        inversion is a production failure: with the roster outranking a declared
        name, one row's seat NAME hijacks every process touching its session."""
        sid = self._bind(B, self.SID_ONE)      # the roster says seat-b
        os.environ["HELM_CHAT_NAME"] = A       # the process says seat-a
        state, name, _why = actors.self_identity(sid)
        self.assertEqual((state, name), (seats.DECLARED, A),
                         "the roster displaced a DECLARED identity")
        seat, _err = seats._assert_own_seat(A, session=sid)
        self.assertEqual(seat, A, "the door displaced a DECLARED identity")

    # ---------------- (e) env vs row mismatch is REPORTED ----------------

    def test_a_mismatch_between_the_env_and_the_matched_row_is_REPORTED(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive controls are the identity_disagreement assertion (the fixture really produces a dispute) and the three assertIn checks on the captured fd-2 text
        """NEVER SILENTLY OVERRIDDEN, IN EITHER DIRECTION. Both orders have
        failed in production — roster-first hijacks a process, env-first drains
        another seat's inbox — so the law is agreement: the env wins the NAME
        and the disagreement is said out loud at the door that consumes."""
        sid = self._bind(B, self.SID_ONE)
        os.environ["HELM_CHAT_NAME"] = A
        self.assertIsNotNone(seats.identity_disagreement(sid),
                             "fixture must actually produce a dispute")
        seats._FOREIGN_WARNED.clear()     # one line per key per PROCESS
        said = self._fd2(lambda: seats._assert_own_seat(A, session=sid))
        seat, err = said[1]
        said = said[0]
        self.assertEqual((seat, err), (A, None))
        self.assertIn(A, said)
        self.assertIn(B, said)
        self.assertIn("DISPUTE", said)

    def test_a_derived_claim_that_names_ANOTHER_seat_is_refused_with_both(self):
        sid = self._bind(B, self.SID_ONE)
        seat, err = seats._assert_own_seat(A, session=sid)
        self.assertIsNone(seat)
        self.assertIn(A, err)
        self.assertIn(B, err)
        self.assertIn("--on-behalf", err)

    # ---------------- the admission layer is not switched off ----------------

    def test_the_BEACON_shape_is_admitted_and_the_bogus_one_refused(self):  # noqa: VACUOUS_ASSERTION — the second half IS the control: the same --follow call with an unbound session must refuse with rc 2 and name the count
        """The flag that switched admission off lived on the --follow path, so
        the beacon shape is where the cure has to be measured: `wait --seat S
        --follow` from an env-less pane whose session the roster binds to S must
        arm, and the same call whose session the roster binds to NOBODY must
        still be refused. The refusal rides STDOUT here, deliberately — a
        Monitor pipe reads stdout, so a refusal on stderr wakes nobody.

        PINNED: unpinned, this arm reaches the real host — /proc, the owner's
        real credential homes through a HOME-relative glob, and a live
        `os.kill` — while claiming to be about a planted roster alone.
        `_PinnedCensus` binds
        those four seams to planted roots; the roster, the admission pass and
        the registry WRITE stay real, which is where the observable comes
        from."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        with self._pinned() as log:
            rc, out, err = self.cli("wait", "--seat", A, "--follow",
                                    "--timeout", "0.01")
            self.assertNotIn("REFUSING", out + err, "%s|%s" % (out, err))
            self.assertEqual(rc, 0, err)
            self.assertIn("active", self._phases(log),
                          "the admitted follow registered no beacon — the "
                          "refusal assertion below would be vacuous")
            del log["registered"][:]
            # THE FIRST CALL MUST NOT HAVE SIGNALLED EITHER, so it is
            # asserted and not cleared: clearing the recorder here would grant
            # the admitted call a free stop. `beacons.arm`'s default reap
            # SIGTERMs an incumbent the census cannot prove with nobody asking
            # for --replace; reaping is replacement, and this derived call has
            # authority for neither.
            self.assertEqual(log["signals"], [],
                             "the admitted derived follow reaped a waiter on "
                             "the ORDINARY path, with no --replace")
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-nobody"
            rc2, out2, err2 = self.cli("wait", "--seat", A, "--follow",
                                       "--timeout", "0.01")
            self.assertEqual(rc2, 2, "control: an unbound session must refuse")
            self.assertIn("0 matches", err2)
            self.assertEqual(log["registered"], [],
                             "a refused follow still claimed the wake route")
            self.assertEqual(log["signals"], [],
                             "a refused follow still signalled a waiter")

    # ---------------- (f) the roster read is ALL-OR-NOTHING ----------------

    def test_an_UNREADABLE_roster_is_UNKNOWN_and_a_MISSING_one_is_ZERO(self):
        """THE DISCRIMINATING PAIR, in one arm.

        `seats_for_session` reads the FAIL-OPEN `roster()`, which answers `{}`
        for a file it cannot parse — so a corrupt roster counted ZERO hits and
        this door said "the roster has never seen this session; run `helm chat
        join`". A false zero with the one repair guaranteed to be wrong: a join
        WRITES into the file helm has just failed to parse.

        BOTH HALVES ARE REQUIRED. A cure that answered UNKNOWN for everything
        passes the first half and takes the genuine zero's repair away —
        `helm chat join` is exactly right for a session nobody has bound.
        CONTROL BLAST RADIUS: the second half changes ONE thing, the roster file
        going from unparseable to absent; same session, same call, same door."""
        sid = self._bind(A, self.SID_ONE)
        pk.atomic_write(seats.roster_path(), "{ this is not json")
        self.assertEqual(seats.roster_checked(), ({}, True),
                         "fixture: the tri-state door must call this a FAILED "
                         "probe, or this arm is about nothing")
        state, name, why = actors.self_identity(sid)
        self.assertEqual(state, actors.ROSTER_UNKNOWN)
        self.assertIsNone(name, "a name was resolved out of an unreadable file")
        self.assertIn("UNKNOWN", why)
        self.assertNotIn("0 matches", why, "a failed read was counted as zero")
        self.assertNotIn("never seen", why)
        seat, err = seats._assert_own_seat(A, session=sid)
        self.assertIsNone(seat)
        self.assertIn("UNKNOWN", err, "the door reported the false zero too")
        self.assertNotIn("0 matches", err)

        os.remove(seats.roster_path())      # MISSING is a PROVEN empty roster
        self.assertEqual(seats.roster_checked(), ({}, False),
                         "control: a missing roster is not a failed probe")
        state2, _n2, why2 = actors.self_identity(sid)
        self.assertIsNone(state2, "a MISSING roster was reported as UNKNOWN")
        self.assertIn("0 matches", why2)
        self.assertIn("helm chat join", why2, "the real zero lost its repair")

    # ---------------- (g) EVERY consuming route is admitted ----------------

    def _bad_actor_store(self):
        """A VALID roster beside an actor store that cannot be minted against.

        THE INPUT THAT ISOLATES ADMISSION. The roster still binds this session
        to exactly one row, so the derivation SUCCEEDS and the only thing left
        that can refuse is `resolve_actor`'s store rung — which is the rung a
        missing admission call silences. `by_name` is ABSENT rather than empty
        on purpose: `_store_defect` treats a missing index as damage and an
        empty one as legitimately mintable."""
        path = actors.store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps({"v": 1, "actors": {}}))
        _store, unavailable = actors.read_store()
        self.assertIn("MISSING index", unavailable or "",
                      "fixture: the store must really be unmintable")
        return path

    def test_SINGLE_SHOT_wait_is_admitted_not_waved_through(self):  # noqa: VACUOUS_ASSERTION — the absence (`ping-two` not delivered) is controlled by the FIRST half of the same arm, which drives the identical CLI call on the identical fixture and asserts the row DOES come out; the rung cannot see that because the two halves are two producer identities
        """Admission checked on the --follow leg alone leaves THIS shape — one
        delivery, no beacon — reaching `deliver_any` with nothing checked at
        all, because the door hands back a name it never admitted.

        POSITIVE FIRST so the negative cannot be vacuous: the pending row must
        really come out. CONTROL BLAST RADIUS: between the halves exactly one
        thing changes, the actor store's `by_name` key. Same roster, same
        session, same argv, same pending-row shape."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        # THE SEAT ARRIVES FIRST, on an EMPTY room. A seat's cursor is created
        # at the room's tail, so a row posted before the cursor exists is
        # deliberately behind it and no build delivers it — an arm that posted
        # first would assert an absence that is true for the wrong reason.
        self.cli("deliver", "--seat", A)
        chat.post("@%s ping-one" % A, "main", who=B)
        rc, out, err = self.cli("wait", "--seat", A, "--timeout", "0.5")
        self.assertEqual(rc, 0, err)
        self.assertIn("ping-one", out, "the healthy delivery left no artifact")

        chat.post("@%s ping-two" % A, "main", who=B)
        self._bad_actor_store()
        before = self.snapshot()
        rc2, out2, err2 = self.cli("wait", "--seat", A, "--timeout", "0.5")
        self.assertEqual(rc2, 2, out2)
        self.assertNotIn("ping-two", out2,
                         "an unadmitted process consumed the seat's row")
        self.assertIn("MISSING index", err2)
        self.assertUnmoved(before, "a refused single-shot wait moved state")

    def test_ORDINARY_deliver_is_admitted_not_waved_through(self):  # noqa: VACUOUS_ASSERTION — the absence is controlled by the SECOND half, which removes the damaged store and asserts the row is delivered and the cursor commits (assertMoved); two halves, two producer identities, one observable
        """`deliver` is FAIL-OPEN by contract — it must never hold a tool
        boundary — so rc is not the observable here. THE ROW IS: healthy, it
        comes out and the cursor commits; unadmitted, neither happens.

        CONTROL BLAST RADIUS: the second half removes the damaged actor store
        and changes nothing else, so the delivered row and the cursor commit
        prove the instrument was not simply blind."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        self.cli("deliver", "--seat", A)      # arrive on an EMPTY room first
        chat.post("@%s deliver-one" % A, "main", who=B)
        self._bad_actor_store()
        before = self.snapshot()
        _rc, out, err = self.cli("deliver", "--seat", A)
        self.assertNotIn("deliver-one", out,
                         "an unadmitted process drained the seat's inbox")
        self.assertIn("MISSING index", err)
        self.assertUnmoved(before, "a refused deliver moved a cursor")

        os.remove(actors.store_path())     # the ONLY thing that changes
        _rc2, out2, err2 = self.cli("deliver", "--seat", A)
        self.assertIn("deliver-one", out2, err2)
        self.assertMoved(before, "control: the healthy delivery must commit a "
                                 "cursor, or the absence above is vacuous")

    def test_a_MALFORMED_declaration_does_not_fall_through_to_the_roster(self):
        """`own_name()` SWALLOWS a hostile HELM_CHAT_NAME to None, and that is
        exactly the input the new derived branch receives: a valid roster row
        then named the process and the door handed the name back, so a C0/bidi
        payload in an env var became a rostered seat. The admission pass refuses
        it at MALFORMED, which is the refusal that exists for this."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["HELM_CHAT_NAME"] = "seat-\x1b[31ma"
        self.assertIsNone(seats.own_name(),
                          "fixture: the hostile name must be swallowed to None")
        self.assertEqual(seats.seats_for_session(sid), [A],
                         "control: the roster must still bind this session, so "
                         "the refusal can only come from admission")
        seat, err = seats._assert_own_seat(A, session=sid)
        self.assertIsNone(seat, "a hostile env var became a rostered seat")
        self.assertIn("not a legitimate seat identifier", err)

    def test_the_CLI_refuses_at_ADMISSION_on_a_VALID_one_row_identity(self):
        """THE CONTROL THE EARLIER ADMISSION ARM COULD NOT BE.

        Its negative was an UNBOUND session, which fails at DERIVATION — before
        admission is even reached — so it went green whether or not the
        admission call existed. This one derives SUCCESSFULLY (one row, valid,
        its spelling returned) and is then refused at the store rung, the only
        rung a removed admission call silences. The positive half proves healthy
        actuation on the identical fixture, so neither an always-refuse nor an
        always-admit build survives it."""
        sid = self._bind(A, self.SID_ONE)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        self.assertEqual(actors.self_identity(sid)[:2], (seats.ROSTERED, A),
                         "fixture: the identity must DERIVE, so the refusal "
                         "below can only be admission's")
        self._bad_actor_store()
        with self._pinned() as log:
            rc, out, err = self.cli("wait", "--seat", A, "--follow",
                                    "--timeout", "0.01")
            self.assertEqual(rc, 2, out)
            self.assertIn("MISSING index", err)
            self.assertEqual(log["registered"], [],
                             "a process refused at admission armed a beacon")
            self.assertEqual(log["signals"], [])

            os.remove(actors.store_path())     # the ONLY thing that changes
            rc2, _out2, err2 = self.cli("wait", "--seat", A, "--follow",
                                        "--timeout", "0.01")
            self.assertEqual(rc2, 0, err2)
            self.assertIn("active", self._phases(log),
                          "control: the healthy call must really arm, or the "
                          "absence above is vacuous")

    def test_a_derived_claim_still_goes_through_the_admission_pass(self):
        """The hole the flag left: `_beacon_identity_refusal(ambient_seat=not
        claimed)` skipped `actors.resolve_actor` exactly when a seat was NAMED,
        so the one door that could consult the roster was off whenever --seat
        was given. A roster-DERIVED claim is a resolution, not a proof, so it
        owes the admission pass — and the pass admits it (ROSTERED)."""
        sid = self._bind(A, self.SID_ONE)
        actor, err = actors.resolve_actor(sid, self.tmp, asserted=A,
                                         act="arm a beacon")
        self.assertIsNone(err, err)
        self.assertEqual(actor.canonical_name, A)
        _a0, err0 = actors.resolve_actor("sid-nobody", self.tmp, asserted=A,
                                         act="arm a beacon")
        self.assertTrue(err0, "control: this call shape must be able to refuse")


class RosterDerivedReplacementTest(_PinnedCensus, LayerBase):
    """--REPLACE IS THE ONE CONSUMING SHAPE THAT REACHES ANOTHER PROCESS.

    Everything else a nameless pane does with a derived identity it does to its
    OWN inbox. `wait --seat S --follow --replace` skips beacons' incumbent-
    idempotence rung, calls `stop_superseded`, SIGTERMs the live waiter serving
    S, and registers itself as the wake route in its place.

    THE MEASURED HOLE: a session id is INHERITED. Every nameless child a seat's
    pane spawns carries CLAUDE_CODE_SESSION_ID unchanged, resolves the same
    unique roster row, and was therefore admitted to rotate its own parent's
    beacon — killing the attributable waiter and diverting the wake/inbox route
    to itself. `beacons.attributable` proving the TARGET is that seat's beacon
    is not evidence about the CALLER: a /proc proof about the victim says
    nothing about the process holding the knife.

    So rotation needs POSITIVE caller ownership — a DECLARED identity naming the
    seat, or a STATED --on-behalf grant — and the arms below are PAIRS, because
    "nothing was rotated" is satisfied by a build that can never rotate at all.
    """

    SID_ONE = "sid-one-row-aaaa"

    def _bind(self, seat=A, sid=None):
        sid = sid or self.SID_ONE
        self.roster(seat, sid)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        return sid

    def test_a_DERIVED_claim_may_not_ROTATE_and_a_DECLARED_one_may(self):
        """CONTROL BLAST RADIUS: the recorder replaces `stop_superseded` for the
        duration of this arm only, so the single behaviour it can mask is the
        SIGTERM itself — the election, the incumbent probe, the registry write
        and the whole refusal path still run for real. The second half FIRES the
        recorder, which is what makes the first half's silence a measurement
        rather than a build that can never signal."""
        self._bind()
        with self._pinned() as log:
            rc, _out, err = self.cli("wait", "--seat", A, "--follow",
                                     "--replace", "--timeout", "0.01")
            self.assertEqual(rc, 2, err)
            self.assertIn("--replace needs independent proof", err)
            self.assertIn("INHERITED", err, "the refusal owes the REASON")
            self.assertIn("--on-behalf", err, "a refusal owes the way in")
            self.assertEqual(log["signals"], [],
                             "a nameless child rotated its parent's beacon")
            self.assertEqual(log["registered"], [],
                             "a refused rotation still claimed the wake route")

            os.environ["HELM_CHAT_NAME"] = A     # the SAME call, DECLARED
            rc2, _out2, err2 = self.cli("wait", "--seat", A, "--follow",
                                        "--replace", "--timeout", "0.01")
            self.assertEqual(rc2, 0, err2)
            self.assertEqual(log["signals"], [A],
                             "control: a seat's OWN rotation must still stop "
                             "its superseded waiter — a recorder that cannot "
                             "fire makes the first half vacuous")
            self.assertIn("active", self._phases(log),
                          "control: the rotation must register the route")

    def test_a_STATED_on_behalf_grant_MAY_rotate_and_says_so(self):
        """The second positive grant. A supervisor rotating a beacon for a seat
        it is standing up is exactly on-behalf-of and exactly not "I am that
        seat" — so it is allowed, and it is LOUD."""
        self._bind()
        with self._pinned() as log:
            rc, _out, err = self.cli("wait", "--seat", A, "--follow",
                                     "--replace", "--on-behalf",
                                     "--timeout", "0.01")
            self.assertEqual(rc, 0, err)
            self.assertIn("ON BEHALF OF", err, "a stated grant must be loud")
            self.assertEqual(log["signals"], [A],
                             "the typed grant did not reach the rotation")

    def test_the_NAMELESS_pane_still_ARMS_without_replace(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertion is the PRESENCE one (an `active` registry row really written by the real `register`); the assertNotIn on REFUSING is the secondary half
        """WHAT THE GATE MUST NOT COST, and the reason it is scoped to
        --replace: the legitimate project pane — no HELM_CHAT_NAME, one roster
        row written by helm's own join hook — still arms its own beacon and
        consumes its own inbox, because that act reaches no other process."""
        self._bind()
        with self._pinned() as log:
            rc, out, err = self.cli("wait", "--seat", A, "--follow",
                                    "--timeout", "0.01")
            self.assertEqual(rc, 0, err)
            self.assertNotIn("REFUSING", out + err, "%s|%s" % (out, err))
            self.assertIn("active", self._phases(log),
                          "the derived seat armed nothing — the legitimate "
                          "nameless pane lost its beacon")
            self.assertTrue(all(wrote for _s, _p, wrote in log["registered"]),
                            "a registry write failed, so the observable above "
                            "proves nothing about the row")
            self.assertEqual(log["signals"], [],
                             "the nameless pane's ordinary arm entered the "
                             "stop pass — reaping is replacement, and this "
                             "call has no authority for either")

    # ---------------- the DEFAULT reap is the same act ----------------

    INCUMBENT_PID = 424242

    def _incumbent(self, sid, seat=A):
        """A committed registry row for ANOTHER pid serving this seat, written
        by the SHIPPED writer so the row is the real artifact.

        The planted /proc tree is EMPTY, so `attributable` can prove nothing
        about that pid and `_one_live_incumbent` returns None — which is
        precisely the UNKNOWN/ghost/duplicate state that fell PAST beacons'
        idempotence rung into `stop_superseded`. On the owner's host that pid is
        his live pane's waiter."""
        row = beacons.register(seat, session=sid, pid=self.INCUMBENT_PID)
        self.assertTrue(row, "fixture: the incumbent row must really be written")
        self.assertIn(self.INCUMBENT_PID, beacons._rows_by_pid(seat),
                      "fixture: the incumbent must be visible to the election")
        return row

    def test_a_DERIVED_arm_WITHOUT_replace_never_REAPS_the_incumbent(self):
        """THE HOLE THE --replace GATE LEFT OPEN. Authority was checked only
        when --replace was present, but `beacons.arm` defaults `reap=True`, and
        on that default an incumbent the census cannot prove falls past the
        idempotence rung straight into `stop_superseded`. So a nameless child
        carrying the inherited session id, asking for nothing but its own inbox,
        still SIGTERMed its parent's attributable waiter and took the wake route
        — the whole of --replace, with the asking left out.

        CONTROL BLAST RADIUS: the third leg calls `beacons.arm` directly with
        the PRE-CURE argument (reap=True, replace=False) on the same planted
        fixture, so the only thing that differs from the first leg is the one
        value this cure computes — and it FIRES the recorder, which is what
        makes the first leg's silence a measurement rather than a fixture that
        could never signal. The recorder's reach is `stop_superseded` alone:
        the election, the incumbent probe and the real registry write all run.
        """
        sid = self._bind()
        with self._pinned() as log:
            self._incumbent(sid)
            log["registered"].clear()        # the plant is fixture, not result
            rc, out, err = self.cli("wait", "--seat", A, "--follow",
                                    "--timeout", "0.01")
            self.assertEqual(rc, 0, err)
            self.assertEqual(log["signals"], [],
                             "a nameless child reaped its parent's waiter "
                             "through the DEFAULT reap, with no --replace")
            self.assertIn(self.INCUMBENT_PID, beacons._rows_by_pid(A),
                          "the incumbent's registry row was taken away")
            self.assertIn("active", self._phases(log),
                          "the legitimate nameless pane lost its own beacon")
            self.assertIn("ALONGSIDE", err, "%s|%s" % (out, err))
            self.assertIn("--replace", err, "the note owes the way in")

            os.environ["HELM_CHAT_NAME"] = A        # PAIRED POSITIVE
            rc2, _out2, err2 = self.cli("wait", "--seat", A, "--follow",
                                        "--replace", "--timeout", "0.01")
            self.assertEqual(rc2, 0, err2)
            self.assertEqual(log["signals"], [A],
                             "a DECLARED seat's explicit rotation must still "
                             "stop its superseded waiter — one grant, one stop")

            os.environ.pop("HELM_CHAT_NAME", None)  # CONTROL: the old default
            log["signals"].clear()
            beacons.arm(A, session=sid, replace=False, reap=True,
                        waiter=beacons.requested_waiter_spec("main"))
            self.assertEqual(log["signals"], [A],
                             "control: the pre-cure default (reap=True on the "
                             "derived path) must fire the recorder, or the "
                             "first leg proves nothing")

    def test_arm_WITHOUT_authority_arms_ALONGSIDE_and_names_the_way_in(self):
        """THE ROOT, ASKED DIRECTLY: `reap` is the authority argument. With it
        false the arm stops nobody — and it does not go blind either. An
        unprovable holder, the reaping path's fall-through into SIGTERM, is here
        a note carrying the two grants, beside a registry row that really got
        written, so the nameless pane keeps its wake path and the incumbent
        keeps its own.

        CONTROL BLAST RADIUS: the second leg is the identical call with
        reap=True — the single argument under test — over the same planted rows,
        and it reaches the stop pass. Nothing else in the fixture moves."""
        sid = self._bind()
        spec = beacons.requested_waiter_spec("main")
        with self._pinned() as log:
            self._incumbent(sid)
            log["registered"].clear()
            report = beacons.arm(A, session=sid, replace=False, reap=False,
                                 waiter=spec)
            self.assertEqual(log["signals"], [],
                             "reap=False stopped a waiter anyway")
            self.assertIsNone(report.get("error"), report.get("error"))
            self.assertTrue(report["registered"],
                            "a beacon that may not reap still owes its row")
            self.assertIn("ALONGSIDE", report["alongside"] or "")
            self.assertIn(str(self.INCUMBENT_PID), report["alongside"])
            self.assertIn("--on-behalf", report["alongside"])
            self.assertIn("HELM_CHAT_NAME", report["alongside"])
            self.assertIn(self.INCUMBENT_PID, beacons._rows_by_pid(A),
                          "the unreapable incumbent lost its row anyway")

            report2 = beacons.arm(A, session=sid, replace=False, reap=True,
                                  waiter=spec)
            self.assertEqual(log["signals"], [A],
                             "control: reap=True over the identical rows must "
                             "reach the stop pass, or the arm above is vacuous")
            self.assertIsNone(report2["alongside"],
                              "the reaping path must not advertise a posture "
                              "it did not take")

    def test_the_authority_door_refuses_a_LOOKUP_and_admits_PROOF(self):
        """The door under the CLI, asked directly, so the three outcomes read
        side by side: a successful roster lookup is refused, a declared name
        that does NOT match is refused, and a declared name that matches is
        admitted in the CALLER'S own spelling."""
        sid = self._bind()
        self.assertEqual(actors.self_identity(sid)[:2], (seats.ROSTERED, A),
                         "fixture: the lookup must SUCCEED, so the refusal is "
                         "about authority and not about resolution")
        name, err = actors.replacement_authority(A, session=sid)
        self.assertIsNone(name)
        self.assertIn("INHERITED", err)

        os.environ["HELM_CHAT_NAME"] = B
        name2, err2 = actors.replacement_authority(A, session=sid)
        self.assertIsNone(name2, "a declared seat rotated ANOTHER seat")
        self.assertIn("this process is", err2)

        os.environ["HELM_CHAT_NAME"] = A.upper()
        name3, err3 = actors.replacement_authority(A, session=sid)
        self.assertIsNone(err3, err3)
        self.assertEqual(name3, A.upper(),
                         "the DECLARED spelling is the caller's own")
