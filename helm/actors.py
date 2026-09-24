#!/usr/bin/env python3
"""helm actors — THE identity layer: one place that decides who may ACT.

WHY A NEW LAYER AND NOT ANOTHER GUARD. The first attempt at this — frozen on
this lane as a worked negative — added ONE admission helper and migrated six
call sites. Six live bypasses were measured around
it, and every one had the same shape: the helper was a door, and the callers
were free to walk past it, because what they needed — a seat NAME — was still
obtainable as a bare string from four other functions (`derive_seat`,
`acting_seat`, `chat.whoname`, `seat_for_session or derive`). Guarding a door
in a wall with four other doors is a per-case fix wearing a whole-object
costume. The owner's ruling: "identity should live in one layer, the identity
layer, and all else should hook into it."

SO THE CURE IS A TYPE, NOT A CHECK. `AdmittedActor` is an OPAQUE capability.
It is what a caller must PRESENT in order to act, it can only be obtained from
`resolve_actor` (which refuses malformed, disputed, DERIVED and mis-asserted
identities in one pass), and it deliberately does NOT render as a seat name —
`str()` on one gives `<AdmittedActor seat-a #<id8>>`, so a caller who
smuggles the capability into a durable row without asking for
`.canonical_name` writes something obviously wrong instead of something
plausibly right. A raw seat string cannot be mistaken for it and cannot be
coerced into it.

WHAT IS *NOT* AN IDENTITY. Two acts that look like identity and are not:
  - AUTOCLAIM — a seat handing ITSELF a dispatch lease. It needs an actor AND
    a policy decision about the kind; `AutoClaimCapability` carries both, so
    a stop-guard rung cannot reach `claim()` merely by having a name.
  - STALE RELEASE — breaking a dead holder's lease. The authority there is a
    LIVENESS PROOF about somebody else, never the caller's own name;
    `StaleReleaseProof` can only be minted from a "stale" verdict, so an
    "unknown" verdict cannot be laundered into a release by a caller who
    happens to be well-named. This is also why stale release takes NO identity
    and must stay usable by an env-less operator.
  - A LEASE HELD BY MACHINERY — the gate queue. Its holder must be
    DISTINGUISHABLE, because the queue orders waiters and people read its
    lines, but there is no seat to attribute it to when an operator runs the
    gate from a shell that declares no name. `SystemLeaseCapability` yields
    `system:gate:<sid8>`, which cannot be read as a seat, and mints only for a
    subsystem in a closed set — the alternative was `acting_seat`'s fall
    through to `derive_seat`, which enqueued under a MINTED name so "GATE
    QUEUED #3 @helm-fable" was indistinguishable from the real seat of that
    name holding the fleet's gate.

THE RECORD LIVES HERE, NOT IN THE ROSTER. Measured, not assumed:
`roster_path()` is `chat.chat_dir()/.roster.json` (helm/seats_common.py) — the
roster is owned by the CHAT layer, so identity built on it is identity owned by
chat. And rename is `r[new] = r.pop(seat)` (helm/seats_roster.py) — a dict key
swap with no lineage and no merge path, which is why after a rename the roster
row moves while the seat's beacon keeps listening on the old name (`helm
beacons` shows new=DEAF, old=live-armed). A record that cannot express "this is
the same actor under a new label" cannot express lifecycle at all.

So the actor record is: `actor_id` (identity, immutable), `canonical_name` (a
mutable LABEL), `aliases` (every name this actor has ever answered to — they
never vanish), plus evidence and a version. The roster stays exactly as it is
and becomes a PROJECTION of this record; building that projection, and the
rename/restore/transfer lifecycle it enables, is task/1300 and is deliberately
not here. This module is the seed the projection will read.
"""

import contextlib
import fcntl
import json
import os

from . import home, pathenv, pk

ACTOR_STORE_VERSION = 1

# The refusal reasons, NAMED and RETURNED (see _resolve), because "no actor"
# has four causes: they need four different fixes from whoever reads the error,
# and the SPEECH door has to drop exactly one of them without branching on
# message text — a guard a reworded sentence would silently open.
MALFORMED = "malformed"      # the declared name is hostile (home.SeatNameError)
DISPUTED = "disputed"        # declared vs rostered disagree — the 2026-08-02 P0
UNRESOLVED = "unresolved"    # DERIVED: a name was MINTED, nobody declared it
MISASSERTED = "misasserted"  # --seat named a seat this process is not
FORGED = "forged"            # a capability was CONSTRUCTED instead of minted
UNAVAILABLE = "unavailable"  # the actor store could not be read — NOT empty
UNCORROBORATED = "uncorroborated"  # DECLARED with no session to check it against

# THE FOURTH IDENTITY STATE, and it lives HERE rather than beside DECLARED /
# ROSTERED / DERIVED in seats_identity because those three are what
# `resolve_identity` can answer and this one is what only `self_identity` can:
# it describes the STORE, not the process. The roster could not be READ, so
# whether a row binds the session is UNKNOWN — and that is never a zero. A real
# zero's repair is `helm chat join`; offering it for a roster helm cannot parse
# tells the owner to WRITE into the broken file. Opposite actions, so they are
# not allowed to share a name.
ROSTER_UNKNOWN = "roster-unknown"


# THE MINT TOKEN. A capability whose class anyone can call is not a capability
# — it is a struct with a strongly-worded docstring, and this module shipped
# exactly that: read-only fields, a tagged `__str__`, `!=` any string, and
# `actor_name()` refusing a raw string BY TYPE, every one of them walked around
# by `AdmittedActor('not-an-id', 'seat-a')`. The class even claimed "no public
# constructor contract" in its own docstring, which is the form of a guarantee
# without its substance.
#
# FOOTGUN SCOPE, HONESTLY, in the register `chat._seat_actor` already uses: a
# determined same-process caller can import `_MINT` and forge anyway, exactly
# as it can export HELM_CHAT_NAME. What this closes is the ACCIDENTAL mint and
# the casual one — a helper that "just needs an actor here", a fixture that
# builds one to get past a signature. Those are how a capability quietly
# becomes a struct, and tests/test_identity_layer's construction census makes
# the deliberate version impossible to land without saying so out loud.
_MINT = object()


class ActorRefused(ValueError):
    """A refusal raised rather than returned, for the paths that cannot carry
    an `err` out (a property, a comprehension, a callback). Carries `reason`,
    one of the four named above, so a handler can branch on the CAUSE instead
    of matching message text."""

    def __init__(self, reason, message):
        ValueError.__init__(self, message)
        self.reason = reason


class AdmittedActor(object):
    """THE capability. Holding one is the authorization to act as its actor.

    Opaque on purpose, and now ENFORCED rather than asserted: the constructor
    demands a module-private mint token, so `resolve_actor`/`resolve_speaker`
    are the only ways to obtain one. Its fields are read-only, and `str()`/`repr()` give a
    tagged form rather than the bare name — a capability that stringified to
    `seat-a` would be silently substitutable for the raw string it replaced,
    which is precisely the substitution this type exists to make impossible.
    Callers that need the label ask for `.canonical_name` and that asking is
    the audit trail.

    THE SHAPE IS THE SEED FOR THE LIFECYCLE WORK (task/1300) and must be
    adoptable there unchanged: `actor_id` is identity and never changes;
    `canonical_name` is a label and may; `aliases` is lineage and only ever
    grows; `evidence` records WHY this resolution was believed; `version` is
    the committed per-actor revision the record was read at (not the store
    format version), so a projection can tell a current record from an
    unpropagated one."""

    __slots__ = ("_actor_id", "_canonical_name", "_aliases", "_evidence",
                 "_version")

    def __init__(self, actor_id, canonical_name, aliases=(), evidence=(),
                 version=1, *, mint=None):
        # KEYWORD-ONLY AND LAST, so a forgery written in the obvious shape —
        # `AdmittedActor('not-an-id', 'seat-a')` — reaches THIS refusal instead
        # of a TypeError about argument counts. The error a developer sees has
        # to name the actual problem; "missing 1 required positional argument"
        # invites them to add one.
        if mint is not _MINT:
            raise ActorRefused(
                FORGED,
                "AdmittedActor cannot be constructed directly — it is MINTED "
                "by helm.actors.resolve_actor / resolve_speaker, which is "
                "what makes holding one mean the admission actually happened. "
                "A caller that needs an actor should resolve one; a test that "
                "needs a negative should assert the mint REFUSES.")
        object.__setattr__(self, "_actor_id", str(actor_id))
        object.__setattr__(self, "_canonical_name", str(canonical_name))
        object.__setattr__(self, "_aliases", tuple(str(a) for a in aliases))
        object.__setattr__(self, "_evidence", tuple(evidence))
        object.__setattr__(self, "_version", int(version))

    # Immutable: a capability whose name could be reassigned after admission
    # is a capability for whoever holds the last reference, not for the actor.
    def __setattr__(self, name, value):
        raise AttributeError("AdmittedActor is immutable (%s)" % name)

    def __delattr__(self, name):
        raise AttributeError("AdmittedActor is immutable (%s)" % name)

    @property
    def actor_id(self):
        return self._actor_id

    @property
    def canonical_name(self):
        return self._canonical_name

    @property
    def aliases(self):
        return self._aliases

    @property
    def evidence(self):
        return self._evidence

    @property
    def version(self):
        return self._version

    def __eq__(self, other):
        # NEVER equal to a string. `actor == "seat-a"` must be False, or a
        # caller could authorize on a name comparison and think it checked a
        # capability.
        if not isinstance(other, AdmittedActor):
            return NotImplemented
        return self._actor_id == other._actor_id

    def __ne__(self, other):
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    def __hash__(self):
        return hash(("AdmittedActor", self._actor_id))

    def __repr__(self):
        return "<AdmittedActor %s #%s>" % (self._canonical_name,
                                           self._actor_id[:8])

    __str__ = __repr__

    def is_named(self, name):
        """True when `name` addresses this actor — its canonical name OR any
        name it has ever answered to. Casefolded, because seat identity treats
        `Kimi` and `kimi` as one address everywhere else."""
        cf = str(name or "").casefold()
        return bool(cf) and (cf == self._canonical_name.casefold()
                             or cf in tuple(a.casefold() for a in self._aliases))


class AutoClaimCapability(object):
    """A seat handing ITSELF work — a DIFFERENT authority from acting as
    itself, and deliberately not obtainable from an `AdmittedActor` alone.

    The stop-guard's bottom rung reaches `claim()` through two hops of what
    looks like warn text (`seats_stop_guard` composes a warning, passes the
    seat to `_stop_whisper`, which finalizes an offer, which claims a lease).
    That call graph is the reason a name is not enough here: the policy — WHICH
    KINDS a seat may hand itself — is a decision made by the offer layer, and
    binding it into the capability means the actuator cannot be reached by a
    caller who merely resolved an identity."""

    __slots__ = ("_actor", "_kind", "_ref")

    def __init__(self, actor, kind, ref, allowed):
        if not isinstance(actor, AdmittedActor):
            raise ActorRefused(
                UNRESOLVED,
                "auto-claim needs an admitted actor, not %r"
                % (type(actor).__name__,))
        # THE POLICY IS CHECKED HERE, not merely carried. `allowed` is the
        # offer layer's own per-kind decision passed in, so this capability
        # cannot be minted for a kind that layer never sanctioned — including
        # None, the honest UNKNOWN of a historical row, and junk a hand-planted
        # ledger row could carry. A capability that stored the kind without
        # testing it would be a comment claiming a guarantee.
        if kind not in tuple(allowed or ()):
            raise ActorRefused(
                MISASSERTED,
                "auto-claim refused: %r is not a kind a seat may hand itself "
                "(allowed: %s)" % (kind, ", ".join(map(str, allowed or ())) or "none"))
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_kind", str(kind))
        object.__setattr__(self, "_ref", str(ref))

    def __setattr__(self, name, value):
        raise AttributeError("AutoClaimCapability is immutable (%s)" % name)

    @property
    def actor(self):
        return self._actor

    @property
    def kind(self):
        return self._kind

    @property
    def ref(self):
        return self._ref

    def __repr__(self):
        return "<AutoClaim %s %s:%s>" % (self._actor.canonical_name,
                                         self._kind, self._ref)


# The subsystems that may hold a lease WITHOUT being a seat. A closed literal,
# not an open string: the whole value of a typed authority is that an arbitrary
# caller cannot mint one by naming itself something plausible.
SYSTEM_LEASE_SUBSYSTEMS = ("gate",)


class SystemLeaseCapability(object):
    """A lease held by MACHINERY, not by a seat — the third authority.

    The gate queue is the case. Its holder must be DISTINGUISHABLE (the queue
    orders waiters and its lines are read by people), but there is no seat to
    attribute it to when an operator runs `helm gate` from a shell that
    declares no name — and refusing there would be the read-only-verb
    regression again, on a verb whose whole job is to run the suite.

    What must never happen is what `acting_seat` did: fall through to
    `derive_seat` and enqueue under a MINTED name, so "GATE QUEUED #3
    @helm-fable" is indistinguishable from the real seat of that name holding
    the fleet's gate. This capability's holder is `system:<subsystem>:<sid8>`,
    which cannot be read as a seat, and it mints only for a subsystem in the
    closed set above — a caller cannot name itself into system authority."""

    __slots__ = ("_subsystem", "_session")

    def __init__(self, subsystem, session=None):
        if subsystem not in SYSTEM_LEASE_SUBSYSTEMS:
            raise ActorRefused(
                MISASSERTED,
                "%r is not a subsystem that may hold a lease without a seat "
                "(allowed: %s)" % (subsystem, ", ".join(SYSTEM_LEASE_SUBSYSTEMS)))
        object.__setattr__(self, "_subsystem", str(subsystem))
        object.__setattr__(self, "_session", str(session or ""))

    def __setattr__(self, name, value):
        raise AttributeError("SystemLeaseCapability is immutable (%s)" % name)

    @property
    def subsystem(self):
        return self._subsystem

    @property
    def holder(self):
        """The name this capability may write into a holder field."""
        sid = self._session[:8] if self._session else "%d" % os.getpid()
        return "system:%s:%s" % (self._subsystem, sid)

    def __repr__(self):
        return "<SystemLease %s>" % self.holder


# The subsystems that may act FOR another seat. Closed, like the lease one.
ON_BEHALF_SUBSYSTEMS = ("beacon", "delivery")


class OnBehalfCapability(object):
    """Authority to arm or consume for ANOTHER seat — the fourth authority.

    THE HOLE IT CLOSES was a fail-open predicate, and the shape is worth
    naming because it is the same one twice in this lane: `_assert_own_seat`
    refuses `--seat <other>` when this process DECLARES a name, and returns
    the claimed seat untouched when it declares none. So `helm chat wait
    --seat <victim>` from an unnamed shell armed the victim's beacon and
    drained its cursor — SILENTLY, and by the guard's own contract. A guard
    that is strict about the named case and open about the unnamed one is a
    guard aimed at the wrong half: the unnamed process is the one that has
    proven nothing.

    IT IS NOT AN IDENTITY AND MUST NOT BE ONE. The legitimate case is a
    supervisor or launcher arming a beacon for a seat it is standing up, which
    is exactly on-behalf-of and exactly not "I am that seat". So this carries
    the SUBJECT it may act for, never a name for the actor, and the operator
    has to say so: `--on-behalf` is the stated intent, and the default is
    refusal. The value is not that a flag is unforgeable — a same-uid caller
    can type it, as it can export HELM_CHAT_NAME — it is that draining another
    seat's inbox stops being something that happens by accident and starts
    being something somebody wrote down."""

    __slots__ = ("_subsystem", "_subject")

    def __init__(self, subsystem, subject, *, mint=None):
        if mint is not _MINT:      # keyword-only: see AdmittedActor.__init__
            raise ActorRefused(
                FORGED,
                "OnBehalfCapability cannot be constructed directly — it is "
                "MINTED by helm.actors.grant_on_behalf, which requires the "
                "operator to have STATED the intent.")
        object.__setattr__(self, "_subsystem", str(subsystem))
        object.__setattr__(self, "_subject", str(subject))

    def __setattr__(self, name, value):
        raise AttributeError("OnBehalfCapability is immutable (%s)" % name)

    @property
    def subsystem(self):
        return self._subsystem

    @property
    def subject(self):
        return self._subject

    def covers(self, seat):
        """True when this capability authorizes acting for `seat`. Casefolded,
        because seat identity treats `Kimi` and `kimi` as one address
        everywhere else and letting argv casing decide would split the very
        beacon election this is about."""
        cf = str(seat or "").casefold()
        return bool(cf) and cf == self._subject.casefold()

    def __repr__(self):
        return "<OnBehalf %s for %s>" % (self._subsystem, self._subject)


def grant_on_behalf(subsystem, subject, stated=False):
    """(capability, err) — the ONLY mint for OnBehalfCapability.

    `stated` is the operator's explicit declaration (`--on-behalf`). Without
    it there is no capability and the caller refuses: silence is what made the
    old fail-open dangerous, so the default here is a refusal that NAMES the
    flag rather than a quiet grant."""
    if subsystem not in ON_BEHALF_SUBSYSTEMS:
        return None, ("%r is not a subsystem that may act for another seat "
                      "(allowed: %s)"
                      % (subsystem, ", ".join(ON_BEHALF_SUBSYSTEMS)))
    subject = str(subject or "").strip()
    if not subject:
        return None, "acting on behalf of nobody is not an authority"
    if not stated:
        return None, (
            "--seat %r names ANOTHER seat and this process declares no "
            "identity of its own, so it has proven nothing. Arming or draining "
            "that seat's inbox is an ON-BEHALF-OF act: say so with "
            "--on-behalf, export HELM_CHAT_NAME=%s if you ARE that seat, or "
            "read `helm chat seats` to inspect without consuming."
            % (subject, subject))
    return OnBehalfCapability(subsystem, subject, mint=_MINT), None


class StaleReleaseProof(object):
    """Authority to break SOMEBODY ELSE's lease, and it is a proof about THEM.

    Minted only by `prove_stale`, only from a verdict of exactly "stale". An
    "unknown" verdict is the case that must never release: it is missing
    evidence, not evidence of death. Because the authority is the proof and not
    the caller, stale release correctly takes NO identity — an env-less
    operator recovering a wedged fleet must not be refused for having no
    HELM_CHAT_NAME."""

    __slots__ = ("_resource", "_holder", "_why")

    def __init__(self, resource, holder, why, *, mint=None):
        if mint is not _MINT:      # keyword-only: see AdmittedActor.__init__
            raise ActorRefused(
                FORGED,
                "StaleReleaseProof cannot be constructed directly — it is "
                "MINTED by helm.actors.prove_stale, which refuses any verdict "
                "but 'stale'. A directly-built proof would let 'unknown' "
                "(missing evidence, not evidence of death) release a live "
                "holder's lease.")
        object.__setattr__(self, "_resource", str(resource))
        object.__setattr__(self, "_holder", str(holder))
        object.__setattr__(self, "_why", str(why))

    def __setattr__(self, name, value):
        raise AttributeError("StaleReleaseProof is immutable (%s)" % name)

    @property
    def resource(self):
        return self._resource

    @property
    def holder(self):
        return self._holder

    @property
    def why(self):
        return self._why

    def __repr__(self):
        return "<StaleReleaseProof %s held-by %s>" % (self._resource,
                                                      self._holder)


def prove_stale(resource, holder, liveness, why):
    """(proof, err) — the ONLY mint for a StaleReleaseProof.

    `liveness` is `claim_holder_liveness`'s verdict verbatim. Anything but
    "stale" returns no proof, so the refusal lives in the mint rather than in
    each caller's `if` — the shape that let three different callers each
    re-derive "is unknown safe here?" and one of them get it wrong."""
    if liveness != "stale":
        return None, ("%s holder %s is %s — %s; stale release refused"
                      % (resource, holder, liveness, why))
    return StaleReleaseProof(resource, holder, why, mint=_MINT), None


# --------------------------------------------------------------------------
# the actor store — the RECORD identity is read from, which the roster will
# become a projection OF (task/1300). Under HELM_HOME, not the chat dir: see
# this module's docstring for why the ownership had to move.
# --------------------------------------------------------------------------

def store_path():
    """The actor record. HELM_ACTORS overrides for probes and fixtures."""
    override = home.env("ACTORS")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(home.global_dir(), ".state", "actors.json")


def _shared_store_path():
    """The machine-shared Actor authority selected by the default Helm root."""
    return os.path.join(home.default_home(), home.GLOBAL, ".state", "actors.json")


def _selected_env_key(name):
    """The declared path spelling whose value ``home.env(name)`` selected."""
    suffix = "_" + name
    for key in pathenv.IDENTITY_PATH_ENV_KEYS:
        if key.endswith(suffix) and os.environ.get(key) is not None:
            return key
    return None


def _split_configuration_refusal(action):
    """Refuse a new identity edge when chat moved but Actor authority did not.

    An explicit chat root creates a separate roster / claims / rename-journal
    estate.  Minting or relabelling against the machine-shared Actor file while
    that estate is elsewhere forks one identity across two roots.  Existing
    canonical names and retired aliases never call this helper: they remain the
    read-only hot path.  A redirected Helm home or any genuinely redirected
    Actor override is paired enough and is admitted.
    """
    from . import chat
    chat_path, origin = home.surface_origin(
        "CHAT_DIR", "helm-chat", chat.DEFAULT_DIR)
    if origin != home.EXPLICIT:
        return None
    # EXPLICIT NAMES PROVENANCE, NOT SEPARATION. An operator may spell the
    # production bus directly (or through a symlink), and home.surface_origin's
    # own contract says that is still production. Compare the path chat_dir()
    # actually returns against the default surface this host actually selects;
    # only a different real destination creates a second identity estate.
    default_chat = home.default_surface(chat.DEFAULT_DIR)
    if os.path.realpath(chat_path) == os.path.realpath(default_chat):
        return None
    actor_path = store_path()
    shared = _shared_store_path()
    if os.path.realpath(actor_path) != os.path.realpath(shared):
        return None
    actor_key = _selected_env_key("ACTORS")
    if actor_key is None:
        chat_key = _selected_env_key("CHAT_DIR")
        if chat_key is None:
            raise RuntimeError("explicit chat path has no declared env spelling")
        actor_key = chat_key.split("_", 1)[0] + "_ACTORS"
    resolved_chat = os.path.abspath(os.path.expanduser(chat_path))
    resolved_actor = os.path.abspath(os.path.expanduser(actor_path))
    return ("refusing %s under split identity configuration: chat root resolves "
            "to %s, but Actor store resolves to the shared authority %s. Set %s "
            "to a redirected Actor store for this chat estate."
            % (action, resolved_chat, resolved_actor, actor_key))


def _empty():
    return {"v": ACTOR_STORE_VERSION, "actors": {}, "by_name": {}}


@contextlib.contextmanager
def _store_lock():
    """Serialize whole-store writes on its stable directory, not replaced JSON.

    A sibling lock file would itself mutate the store on a refused first bind;
    locking the directory keeps a failed read or write free of new files.
    """
    path = os.path.dirname(store_path())
    os.makedirs(path, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_store():
    """(store, unavailable) — THREE STATES, and collapsing them is the bug.

    ABSENT is a legitimate empty: every fresh box, every new HELM_HOME, the
    first actor ever seen. It may be minted into and written.

    MALFORMED and UNREADABLE are REFUSALS. A store that exists but does not
    parse, or one this process cannot read, is not an empty world — it is a
    world this process cannot see. Answering `{}` there is a WELL-FORMED
    CERTIFICATION OF AN EMPTY WORLD, and downstream code that is careful about
    every value it receives will still be wrong, because the input was not
    unreadable: it was readable and false.

    THE STAKES HERE ARE A MINTED IDENTITY. `by_name` is what makes an old
    alias keep resolving to the same actor. Read it as empty and the next
    `bind` sees an unknown name, derives a fresh record, and RE-MINTS a
    retired alias as a NEW actor — the corrupt store becoming the authority
    for a lineage it just erased. So the caller is told it could not look, and
    the layer neither mints nor writes until it can.

    Repair is deliberately trivial and lossless: DELETE the file. Absent is a
    legitimate empty and `_actor_id_for` derives ids from names, so the same
    seats resolve to the same ids on the next pass."""
    path = store_path()
    try:
        with pk.open_regular(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _empty(), None                    # ABSENT — a legitimate empty
    except OSError as e:                         # UNREADABLE
        return None, ("the actor store is UNREADABLE (%s: %s) — refusing to "
                      "treat that as an empty world, because minting into one "
                      "would re-mint retired aliases as new actors. Fix the "
                      "permissions, or delete %s (absent is a legitimate "
                      "empty and ids derive from names, so nothing is lost)."
                      % (type(e).__name__, e, path))
    try:
        d = json.loads(raw)
    except ValueError as e:                      # MALFORMED
        return None, ("the actor store is MALFORMED (%s) — refusing to treat "
                      "that as an empty world. Delete %s; absent is a "
                      "legitimate empty and ids derive from names, so the "
                      "same seats resolve to the same ids on the next pass."
                      % (e, path))
    defect = _store_defect(d)
    if defect:
        return None, ("the actor store parsed but is not a store (%s) — "
                      "refusing to mint against it. Delete %s to start clean; "
                      "absent is a legitimate empty and ids derive from names."
                      % (defect, path))
    # `v` IS THE ONE DEFAULT LEFT AND IT IS DELIBERATE: a store written before
    # the field existed is a STORE, not damage, and the floor is the version
    # this code implements. Nothing about lineage hangs on it.
    d.setdefault("v", ACTOR_STORE_VERSION)
    return d, None


def _store_defect(d):
    """Why `d` is not a store, or None — and every check here exists because a
    MISSING key and an EMPTY one are opposite facts.

    THE DEFECT THIS FUNCTION WAS BORN FROM: the shape test used to read
    `isinstance(d.get("by_name", {}), dict)`, and THE DEFAULT WAS THE HOLE. A
    store whose index was LOST substitutes `{}`, `{}` is a dict, validation
    passes — and `bind` then re-mints a retired alias as a NEW actor, forking
    the lineage the index exists to preserve. An empty index is a store with no
    names yet and is mintable; a missing index is a store that lost its index
    and must refuse. `.get(k, <container>)` cannot tell those apart, so the
    accessors below take the keys directly and this door is what makes that
    safe.

    ROWS ARE CHECKED FOR THE SAME REASON. `aliases` IS the lineage: a row
    missing it, defaulted to `[]` at the append site, silently discards every
    name the actor has answered to. Extra keys are fine — required keys are
    required, the schema stays open for task/1300 to grow into.

    THE INDEX MUST POINT AT ACTORS THAT EXIST, because an entry pointing into
    nothing is the same fork by another route: `bind` would build a fresh row
    under that id and the renamed record it named would be gone."""
    if not isinstance(d, dict):
        return "top level is %s, not a mapping" % type(d).__name__
    for key in ("actors", "by_name"):
        if key not in d:
            return "no %s index — a MISSING index is not an empty one" % key
        if not isinstance(d[key], dict):
            return "%s is %s, not a mapping" % (key, type(d[key]).__name__)
    for aid, row in d["actors"].items():
        if not isinstance(row, dict):
            return "actor %s is %s, not a mapping" % (aid, type(row).__name__)
        for key in ("actor_id", "canonical_name"):
            if not isinstance(row.get(key), str) or not row[key]:
                return "actor %s has no %s" % (aid, key)
        if not isinstance(row.get("aliases"), list):
            return ("actor %s has no aliases list — that field IS the lineage"
                    % aid)
        if "revision" in row and (type(row["revision"]) is not int
                                  or row["revision"] < 1):
            return "actor %s has invalid revision" % aid
    missing = [n for n, aid in d["by_name"].items() if aid not in d["actors"]]
    if missing:
        return ("the index names %d actor(s) the store does not hold (%s)"
                % (len(missing), ", ".join(sorted(missing)[:3])))
    return None


def lookup(name):
    """The record `name` addresses — canonical name OR alias — or None.

    A CONVENIENCE VIEW, in the shape `council.registry` has beside
    `council.read`: it collapses "no such actor" and "could not look" to None
    and is therefore for callers that only need to display or probe. Nothing
    that MINTS may use it — `bind` and `_resolve` take the two-tuple, because
    for them those two answers demand opposite behaviour.

    Alias lookup is the whole point: a renamed seat's OLD name must keep
    resolving to the same actor, which is what the roster's
    `r[new] = r.pop(seat)` cannot do."""
    cf = str(name or "").casefold()
    if not cf:
        return None
    d, unavailable = read_store()
    if unavailable:
        return None
    aid = d["by_name"].get(cf)
    row = d["actors"].get(aid) if aid else None
    return row if isinstance(row, dict) else None


def _actor_id_for(name, after=None):
    """A stable id for a first sighting. Derived from the name so a store lost
    to a wipe re-mints the SAME id for the same seat — the alternative
    (os.urandom) makes every reseed a new actor and turns lineage into noise
    exactly when it is most needed. `after` is the id of the record this
    name is being minted AWAY from (a re-admitted name whose old record now
    belongs to the seat it was renamed to): same determinism, distinct id."""
    import hashlib
    seed = "helm-actor:" + str(name).casefold()
    if after:
        seed += "#after:" + str(after)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _own_roster_key(name):
    """(is_exact_key, unknown) — whether `name` is an EXACT roster key right
    now. A roster this process cannot read answers (False, why): UNKNOWN is
    preserved and the caller REFUSES, because "keep the retired record" is
    not a conservative default here — it hands another generation's actor
    to a re-admitted name on one transient read failure. Read through the
    checked reader, never the fail-open one."""
    try:
        from . import seats_common, seats_roster
        rows, failed = seats_roster.roster_checked()
    except Exception as exc:              # noqa: BLE001
        return False, "roster unreadable (%s)" % type(exc).__name__
    if failed:
        return False, "roster unreadable (%s)" % failed
    return bool(seats_common.canonical_keys(name, rows)), None


_UNCOMMITTED_ADMISSION = object()


def bind(name, evidence=()):
    """The record for `name`, minting a first sighting. Returns the record.

    WRITES ONLY WHEN THE RECORD CHANGES — a new actor, or a name this actor has
    not answered to before. `resolve_actor` runs at EVERY delivery boundary, so
    a `seen` timestamp refreshed on each call would turn one identity question
    into a JSON read plus a JSON write per tool call, and would make the store
    churn on a file whose whole value is being STABLE. Liveness already has an
    owner (the roster's presence beat); this is the identity RECORD, and a
    record that rewrites itself constantly is a log.

    A new admission is proven only after the record commits. An existing
    canonical name or retired alias needs no write; a failed first sighting
    cannot return a revision-bearing capability from an uncommitted row.
    A STORE IT COULD NOT READ returns (None, why), never a minted actor."""
    return _bind(name, evidence, commit=True)


def _bind(name, evidence, commit, admit=None):
    """The shared read/fence pass behind bind and Actor resolution.

    ``commit=False`` still takes the Actor lock and runs every first-admission
    fence in its existing order, but returns ``_UNCOMMITTED_ADMISSION`` instead
    of changing the Actor record.  A corroborated resolver then repeats this
    locked pass with ``admit``: the callback re-reads the roster immediately
    before a return or write, after store/generation/split/rename revalidation,
    so neither the preflight nor its corroboration becomes stale commit
    authority. Public ``bind`` supplies no callback and keeps its contract.
    """
    cf = str(name).casefold()
    d, unavailable = read_store()
    if unavailable:
        return None, unavailable
    aid = d["by_name"].get(cf)
    row = d["actors"].get(aid) if aid else None
    if isinstance(row, dict) and row["canonical_name"].casefold() == cf \
            and admit is None:
        return row, None                  # read-only hot path, no writer lock
    try:
        with _store_lock():
            d, unavailable = read_store()
            if unavailable:
                return None, unavailable
            aid = d["by_name"].get(cf)
            row = d["actors"].get(aid) if aid else None
            if isinstance(row, dict) and row["canonical_name"].casefold() == cf:
                refused = admit() if admit is not None else None
                return (None, refused) if refused is not None else (row, None)
            if isinstance(row, dict):
                exact, unknown = _own_roster_key(name)
                if unknown:
                    return None, ("cannot bind %r: it is a retired alias of actor "
                                  "#%s and the %s, so whether it is its own seat "
                                  "again is UNKNOWN" % (name, aid[:8], unknown))
                if not exact:
                    refused = admit() if admit is not None else None
                    return (None, refused) if refused is not None else (row, None)
                # An exact roster key beats a retired alias: a re-admission
                # starts another actor; the earlier one's lineage remains.
                after = aid
            else:
                after = None
            refusal = _split_configuration_refusal("first Actor admission")
            if refusal:
                return None, refusal
            # ONLY A FIRST ADMISSION consults rename state, and only after the
            # locked re-read proved this is not a known canonical name or alias.
            # The helper takes the journal mutex after this actor lock and never
            # attempts recovery, preserving roster -> actor -> journal order.
            from . import seats_rename
            refusal = seats_rename.pending_rename_admission_refusal(name)
            if refusal:
                return None, refusal
            if not commit:
                return _UNCOMMITTED_ADMISSION, None
            refused = admit() if admit is not None else None
            if refused is not None:
                return None, refused
            aid = (_actor_id_for(name, after=after) if after
                   else aid or _actor_id_for(name))
            row = d["actors"].get(aid)
            if not isinstance(row, dict):
                row = {"actor_id": aid, "canonical_name": str(name), "aliases": [],
                       "created": pk.now_ts(), "v": ACTOR_STORE_VERSION,
                       "revision": 1, "evidence": [list(e) for e in evidence]}
                d["actors"][aid] = row
            elif str(name) not in row["aliases"] \
                    and cf != row["canonical_name"].casefold():
                row["aliases"].append(str(name))
                row["revision"] = row.get("revision", 1) + 1
            d["by_name"][cf] = aid
            pk.write_json(store_path(), d)
            return row, None
    except OSError as exc:
        return None, "actor store is not writable (%s)" % type(exc).__name__


NO_RECORD, RELABEL, CONFLICT = "no-record", "relabel", "conflict"
SPLIT_CONFIGURATION = "split-configuration"


def _store_writable():
    """Can this process create or replace the store? A directory that does
    not exist yet is a legitimate empty (the first write makes it), so the
    question is asked of the NEAREST EXISTING ancestor."""
    path = os.path.dirname(store_path()) or "."
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return os.access(path, os.W_OK)


def _rename_decision(d, old, new):
    """Preflight plus the exact actor epoch a recoverable rename may commit."""
    oldcf, newcf = str(old or "").casefold(), str(new or "").casefold()
    aid, holder = d["by_name"].get(oldcf), d["by_name"].get(newcf)
    if aid and holder and holder != aid:
        return False, ("%r already names a different actor (#%s) — a rename "
                       "may not merge two identities" % (new, holder[:8])), \
            CONFLICT, None
    if not aid:
        return True, ("no actor record for %r; the first admission as %r "
                      "mints one" % (old, new)), NO_RECORD, None
    row = d["actors"][aid]
    expected = {"actor_id": aid, "revision": row.get("revision", 1),
                "canonical_name": row["canonical_name"],
                "source_name": str(old), "target_name": str(new),
                "target_indexed": holder == aid}
    return True, "actor #%s will be relabelled %r (%r stays its alias)" % (
        aid[:8], new, old), RELABEL, expected


def rename_preflight(old, new):
    """(ok, note, reason) — what `rename` WOULD do, decided BEFORE a roster
    commit so the roster never publishes a rename the store cannot honour.

    THREE ANSWERS THAT MUST NOT COLLAPSE (task/2338): NO_RECORD (old
    was never an actor; nothing to relabel, the first admission under `new`
    mints), RELABEL (old's record will be relabelled and keep its lineage),
    CONFLICT (`new` already names a DIFFERENT actor — a rename may not merge
    two identities, and a best-effort call after the roster commit could not
    promise the lineage the commit implied). UNAVAILABLE — an unreadable or
    unwritable authority — is a refusal too, and a different sentence from
    NO_RECORD: "no prior record" is a fact about the world, "could not read"
    is a fact about this process."""
    refusal = _split_configuration_refusal("seat rename")
    if refusal:
        return False, refusal, SPLIT_CONFIGURATION
    d, unavailable = read_store()
    if unavailable:
        return False, "actor store unreadable — " + str(unavailable), UNAVAILABLE
    if not _store_writable():
        return False, ("actor store directory %s is not writable"
                       % os.path.dirname(store_path())), UNAVAILABLE
    ok, note, reason, _expected = _rename_decision(d, old, new)
    return ok, note, reason


def rename_expectation(old, new):
    """Preflight plus the actor epoch to journal. Caller holds `_store_lock`."""
    refusal = _split_configuration_refusal("seat rename")
    if refusal:
        return False, refusal, SPLIT_CONFIGURATION, None
    d, unavailable = read_store()
    if unavailable:
        return (False, "actor store unreadable — " + str(unavailable),
                UNAVAILABLE, None)
    if not _store_writable():
        return False, ("actor store directory %s is not writable"
                       % os.path.dirname(store_path())), UNAVAILABLE, None
    return _rename_decision(d, old, new)


def _rename_expected_state(d, expected, forward):
    """(`before`|`after`, note) only for the exact journalled actor epoch."""
    required = ("actor_id", "revision", "canonical_name", "source_name",
                "target_name")
    if not isinstance(expected, dict) or any(key not in expected
                                              for key in required):
        return None, "actor rename expectation is malformed"
    aid, revision = expected["actor_id"], expected["revision"]
    canonical = expected["canonical_name"]
    source, target = expected["source_name"], expected["target_name"]
    if not all(isinstance(value, str) and value
               for value in (aid, canonical, source, target)) \
            or type(revision) is not int or revision < 1:
        return None, "actor rename expectation is malformed"
    row = d["actors"].get(aid)
    if not isinstance(row, dict):
        return None, "expected actor #%s is absent" % aid[:8]
    source_holder = d["by_name"].get(source.casefold())
    target_holder = d["by_name"].get(target.casefold())
    if target_holder and target_holder != aid:
        return None, ("%r now names a different actor (#%s); replay refused"
                      % (target, target_holder[:8]))
    current_revision = row.get("revision", 1)
    before = (source_holder == aid and current_revision == revision
              and row["canonical_name"] == canonical)
    alias_kept = (canonical.casefold() == target.casefold()
                  or canonical in row["aliases"])
    after = (source_holder == aid and target_holder == aid
             and current_revision == revision + 1
             and row["canonical_name"] == target and alias_kept)
    if before:
        return "before", None
    if forward and after:
        return "after", None
    return None, ("actor #%s diverged from the journalled %r revision %d; "
                  "replay refused" % (aid[:8], canonical, revision))


def replay_rename(expected, forward=True, apply=True, *, _locked=False):
    """Replay one journalled actor leg without merging or overwriting drift.

    Forward replay accepts only the exact pre-state or its exact committed
    successor. Rollback accepts only the pre-state because the transaction
    never writes the actor before roster publication. The caller may validate
    with ``apply=False`` before moving any other estate, then apply under the
    same actor lock after the roster/state leg is committed.
    """
    try:
        with contextlib.nullcontext() if _locked else _store_lock():
            d, unavailable = read_store()
            if unavailable:
                return False, unavailable
            state, err = _rename_expected_state(d, expected, forward)
            if not state:
                return False, err
            aid = expected["actor_id"]
            if state == "after":
                return True, "%r is now %r (actor #%s; %r still resolves)" % (
                    expected["source_name"], expected["target_name"], aid[:8],
                    expected["source_name"])
            if not forward or not apply:
                return True, "actor #%s rename is ready" % aid[:8]
            row = d["actors"][aid]
            prior, target = row["canonical_name"], expected["target_name"]
            if prior not in row["aliases"] \
                    and prior.casefold() != target.casefold():
                row["aliases"].append(prior)
            row["canonical_name"] = target
            row["revision"] = expected["revision"] + 1
            row["renamed"] = pk.now_ts()
            d["by_name"][target.casefold()] = aid
            pk.write_json(store_path(), d)
            return True, "%r is now %r (actor #%s; %r still resolves)" % (
                expected["source_name"], target, aid[:8],
                expected["source_name"])
    except OSError:
        return False, "actor store is not writable"


def rename(old, new, *, _locked=False):
    """(ok, err) — relabel an actor, KEEPING its identity and its old name.

    The seed of the lifecycle task/1300 will build on, and the reason the
    record could not stay in the roster: `r[new] = r.pop(seat)` destroys the
    old key, so nothing downstream (a beacon still armed on the old name, a
    dispatch addressed to it, a lease it holds) can be followed to the seat it
    now belongs to. Here the actor_id is unchanged and `old` keeps resolving.
    """
    if not str(old or "").casefold() or not str(new or "").casefold():
        return False, "rename needs both an old and a new name"
    try:
        with contextlib.nullcontext() if _locked else _store_lock():
            d, unavailable = read_store()
            if unavailable:
                return False, unavailable
            ok, note, reason, expected = _rename_decision(d, old, new)
            if not ok:
                return False, note
            if reason == NO_RECORD:
                return False, "no actor record for %r" % old
            if expected["canonical_name"] == str(new) \
                    and expected["target_indexed"]:
                return True, "%r is already %r (actor #%s)" % (
                    old, new, expected["actor_id"][:8])
            return replay_rename(expected, _locked=True)
    except OSError:
        return False, "actor store is not writable"


# --------------------------------------------------------------------------
# resolution — the ONE pass every actuator hooks into
# --------------------------------------------------------------------------

def resolve_speaker(session=None, cwd=None, asserted=None, act="speak"):
    """(actor_or_None, err) — admission for SPEECH, where DERIVED is allowed.

    THE SPLIT THIS FUNCTION MAKES IS THE ONE THE WHOLE LANE IS ABOUT, so it is
    worth stating precisely. A plain room post is SPEECH: the row is read, and
    nothing about it says another party did, owes, or saw anything.
    `seats.auto_name` exists for exactly this — a meaningful project+family
    name so an un-named join can say its first word — and the repo pins that
    behaviour in several arms, including one whose entire subject is that chat
    IO survives a deleted cwd. Refusing there does not close a hole; it
    silences a new agent and breaks an unrelated property.

    An ACT is different and keeps `resolve_actor`: a DM opens an obligation, an
    ack discharges one, a lease assigns responsibility, a cursor says a backlog
    was seen, a verdict binds member identity. Those write a name ANOTHER PARTY
    reads as testimony.

    So the refusals this door drops are UNRESOLVED and UNCORROBORATED — the
    two that are about a name having no backing rather than a name being
    WRONG. The bot identities (owed-push, the stale sweep) are the
    uncorroborated shape by construction: their units set HELM_CHAT_NAME and
    unset the session vars precisely so their declared name is not a DISPUTE,
    and they only ever post. Malformed, disputed,
    mis-asserted and UNAVAILABLE still refuse — a hostile name is no name, a contested author
    must not sign, and `--seat` never selects. And `(None, None)` means
    "speak on the ambient floor", which is what routes a `post --dm` down to
    `seats.dm`, where AMBIENT refuses. Speech that turns out to be an act is
    caught by the act's own door, not by this one."""
    actor, err, reason = _resolve(session, cwd, asserted, act)
    if reason in (UNRESOLVED, UNCORROBORATED):
        return None, None
    return actor, err


def self_identity(session=None, cwd=None):
    """(state, name, why) — "WHO AM I", asked ONCE, for the doors that consume.

    DECLARED as ever; else the ROSTER when exactly ONE row binds `session`;
    else (None, None, why) with the COUNT named, because zero and many are
    different repairs (declare/join vs `helm chat seat disown`). A roster that
    cannot be READ is a FOURTH answer — (ROSTER_UNKNOWN, None, why) — and never
    a zero: see the acquisition note in the body.

    WHY THE ROSTER IS AN IDENTITY SOURCE AT ALL. helm helps agent teams build
    ANY project, and a verb that assumes a helm seat is a bug. A project pane
    opened by hand declares no HELM_CHAT_NAME, and helm's OWN SessionStart join
    hook has already NAMED it — `auto_name` = project + family — and written
    the row with its session id. A consuming door that reads only the env then
    answers "--seat <that seat> names ANOTHER seat and this process declares no
    identity of its own", which is helm refusing a seat helm itself named. The
    fact is in hand; the failure is a door keeping its own copy of the
    question.

    IT LIVES HERE, in the layer whose whole purpose is that identity is decided
    in ONE place, and it is NOT an authorization: it returns a NAME, so a
    caller that ACTS still needs `resolve_actor`'s opaque capability. What it
    adds to `resolve_identity` is the AMBIGUITY arm: that function's roster rung
    is `seat_for_session`, which picks the first hit BY BINDING AGE and warns —
    right for a display, a coin flip for an act — so this one reads the LIST
    and refuses a tie it cannot break. Exactly-one is not a new rule either:
    `sessions.resume_identity_env` carries it ("Ambiguous (multi-row) or
    unknown sids pass nothing ... a guessed name is incident 1").

    THE ENV STAYS FIRST, AND NEITHER SOURCE IS EVER PREFERRED SILENTLY. Both
    orders have failed in production: the roster outranking a declared name
    lets one row's seat NAME hijack every process that touches its session, and
    an inherited declared name drains another seat's inbox. So a DECLARED name
    is never displaced here and `identity_disagreement` owns the mismatch.
    DERIVED is never an identity: `resolve_identity`'s last rung MINTS a
    stranger that passes every `if seat:`, so this stops at the roster.
    """
    from . import seats_common as _common
    from . import seats_identity as _ident
    from . import seats_roster as _roster
    own = _common.own_name()
    if own:
        return _ident.DECLARED, own, None
    if not session:
        return None, None, (
            "this process declares no HELM_CHAT_NAME and presented no session "
            "id, so nothing binds it to a seat (0 roster matches) — export "
            "HELM_CHAT_NAME=<seat>, or bind this session with `helm chat join`")
    # ACQUISITION IS ALL-OR-NOTHING AND IT HAPPENS BEFORE ANY COUNT.
    # `seats_for_session` reads the FAIL-OPEN `roster()`, which answers `{}` for
    # an unreadable, malformed or wrong-shaped file and for an unresolvable
    # roster PATH — so an identity door built on it counted 0 hits and said
    # "the roster has never seen this session; run `helm chat join`". That is a
    # false zero with the wrong repair attached: the file is the broken thing,
    # and a join would write INTO it. `roster_checked` is the tri-state door
    # that separates a MISSING roster (a proven empty one) from a failed probe,
    # and it fails CLOSED, so cannot-look can never be read as nobody-home.
    rows, failed = _roster.roster_checked()
    if failed:
        try:
            where = _common.roster_path()
        except Exception:                    # noqa: BLE001 — an unresolvable
            where = "<unresolvable roster path>"   # path IS one of the failures
        return ROSTER_UNKNOWN, None, (
            "the roster itself could not be READ (unreadable, malformed or "
            "wrong-shaped state at %s), so whether a row binds session %.8s is "
            "UNKNOWN — this is 'cannot look', never 'no row binds this "
            "session'. Repair the FILE (`helm chat seats` reads it; a join "
            "would write into the state helm just failed to parse), or declare "
            "the identity outright with HELM_CHAT_NAME=<seat>."
            % (where, str(session)))
    index, _holders = _roster.roster_indexes(rows)
    hits = [h for h in (_roster.seats_for_session_in(index, session) or []) if h]
    if len(hits) == 1:
        return _ident.ROSTERED, hits[0], None
    if not hits:
        return None, None, (
            "no roster row binds session %.8s (0 matches) — this process "
            "declares no HELM_CHAT_NAME and the roster has never seen this "
            "session; run `helm chat join` or export HELM_CHAT_NAME=<seat>"
            % str(session))
    return None, None, (
        "session %.8s is bound to %d roster rows (%s) — an identity is never "
        "guessed from an ambiguous binding; repair it with `helm chat seat "
        "disown <wrong-seat> %.8s`"
        % (str(session), len(hits),
           ", ".join(repr(_common._seat_label(h)) for h in hits),
           str(session)))


def admit_rostered(claimed, derived, session=None):
    """(name, err) — a ROSTER-DERIVED claim, CHECKED before it becomes a name.

    THE ONE DOOR EVERY CONSUMING ROUTE SHARES. `seats_identity._assert_own_seat`
    is what `helm chat wait` (single-shot and --follow), `deliver`, the
    PostToolUse prepare leg and `catchup` all call, and a name it hands back is
    treated as proven by every one of them. Checking a derived name on the
    --follow leg alone (`_beacon_identity_refusal`) therefore leaves the other
    routes admitting it with nothing checked at all: a valid roster row beside
    an UNREADABLE ACTOR STORE, or a HELM_CHAT_NAME carrying a C0/bidi payload
    that `own_name()` swallows to None, both reach a presence write and a cursor
    commit. Four call sites owing one rule is a shape problem, so the rule is
    applied once, at the door.

    WHAT COMES BACK IS THE ROSTER ROW'S SPELLING, not argv's, for the same
    reason the declared branch returns `own`: seat identity is casefold-exact,
    and letting argv casing become the actuator key would split one seat's
    beacon election. The ADMITTED ACTOR is deliberately dropped — this door's
    contract is a NAME, and a caller that ACTS resolves its own capability.
    What the pass is called for here is its REFUSALS.

    AND IT IS NOT REPLACEMENT AUTHORITY. `resolve_actor` answers "may this
    process act as this seat"; rotating another process's beacon is a different
    question, and `replacement_authority` below is what answers it."""
    _actor, err = resolve_actor(session, asserted=claimed,
                                act="consume seat %s's inbox" % derived)
    return (None, "helm chat: " + err) if err else (derived, None)


# A SUBAGENT IS NOT ITS SEAT, AND NOTHING THE SUBAGENT CARRIES SAYS SO. It
# inherits the pane's whole environ (HELM_CHAT_NAME included), its session id
# and its transcript path, so every door below reads it as the seat and
# `replacement_authority` admits its --replace under DECLARED. Measured on a
# live seat (task/2542): a compacted background subagent re-armed the seat's
# beacon with --replace, Claude Code routed every wake line to that subagent,
# and the seat went deaf when it ended while `helm beacons` read covered.
#
# ONLY THE HARNESS KNOWS WHICH AGENT IS ACTING, and it says so in one field.
# Read from the bytes of Claude Code 2.1.270: the hook input builder sets
# agent_id = toolUseContext.agentId, every subagent context gets a minted
# agentId, and the main thread's context has none, so the key is absent. That
# holds for PreToolUse. SessionStart is built without a tool context and
# carries no agent_id from a subagent either, so the SessionStart doors keyed
# on it are the DOCUMENTED contract ("present only when the hook fires inside
# a subagent"), unmeasured for that event.
SIDECHAIN_RULE = ("never arm, replace or stop a helm chat wait beacon; "
                  "report to your parent")


def sidechain_agent(payload):
    """The subagent id a hook payload carries, or None for the main thread.

    A missing, empty or non-string agent_id is None: missing evidence is not
    evidence of a sidechain, so a main-thread payload takes exactly the path
    it took before this door existed."""
    agent = payload.get("agent_id") if isinstance(payload, dict) else None
    return agent.strip() if isinstance(agent, str) and agent.strip() else None


def _seat_phrase(seat):
    from . import seats_common as _common
    seat = seat or _common.own_name()
    return "seat '%s'" % seat if seat else "this seat"


def sidechain_notice(seat=None):
    """The one sentence a sidechain hears where a seat hears its banner."""
    return ("[helm chat] you are a subagent inside %s, sharing its session; "
            "%s." % (_seat_phrase(seat), SIDECHAIN_RULE))


def sidechain_beacon_refusal(seat=None):
    """Why a subagent's beacon arm is refused, and what to do instead.

    THE ESCAPE IS THE PART THAT MATTERS and it stays whole, because this rung
    refuses a MENTION with the act — a subagent writing ABOUT the beacon is
    refused exactly like one arming it, and without the escape that reader is
    stuck. What went is the folding-semantics paragraph: how the rung reads
    the text is an editor's concern, and the module docstring above carries
    it. The refusal is 576 characters shorter and refuses the same acts."""
    who = _seat_phrase(seat)
    return ("a Monitor armed in a subagent wakes the subagent, not %s, and "
            "leaves the seat deaf once you end — the seat's beacon is the "
            "main conversation's to arm. You are a subagent: %s. Only writing "
            "ABOUT it? This rung reads the words and no shell, so say it "
            "without the flag word, or write it with a tool that is not a "
            "shell." % (who, SIDECHAIN_RULE))


def replacement_authority(claimed, on_behalf=None, session=None):
    """(name, err) — may THIS CALLER rotate `claimed`'s beacon? POSITIVE only.

    ROTATION IS THE ONE CONSUMING SHAPE THAT REACHES ANOTHER PROCESS.
    `helm chat wait --seat S --follow --replace` skips beacons' incumbent-
    idempotence rung, calls `beacons.stop_superseded`, SIGTERMs the live waiter
    serving S, and registers itself as the wake route in its place. Everything
    else a nameless pane may do it does to its OWN inbox; this one takes a route
    away from a process that had proven it.

    SO A LOOKUP IS NEVER AUTHORITY FOR IT, and that is the whole function.
    Deriving an identity from "the roster binds my session id to exactly one
    row" is right as a RESOLUTION and worthless as CALLER ownership, because a
    session id is INHERITED: every nameless child a seat's pane spawns carries
    CLAUDE_CODE_SESSION_ID unchanged, resolves the same unique row, and could
    therefore rotate its own parent's beacon and divert the wake/inbox route to
    itself. `beacons.attributable` proving the TARGET is that seat's beacon does
    not make the CALLER the seat — a /proc proof about the victim is not
    evidence about the process holding the knife.

    TWO POSITIVE GRANTS, and nothing else counts:

      DECLARED     the caller's OWN HELM_CHAT_NAME (or a live rename alias of
                   it) names the seat it is rotating. Inheritable too — but an
                   INHERITED declared name is the takeover class, which
                   `identity_disagreement` and `resolve_actor` already refuse in
                   their own right, so it is a CHECKED credential and not a
                   lookup any descendant can reproduce.
      ON-BEHALF    a typed `OnBehalfCapability` covering the seat, minted from a
                   STATED `--on-behalf`. A supervisor rotating a beacon for a
                   seat it is standing up says so out loud.

    WHAT THIS DELIBERATELY DOES NOT COST: the genuine nameless project pane.
    `wait --seat S --follow` with NO --replace arms its own beacon and consumes
    its own inbox exactly as intended — a derived identity is enough for that,
    because that act reaches no other process. Only rotation is gated, and the
    refusal names which of the two grants to bring.

    AND THE REAP IS ROTATION, so this one door answers BOTH questions. The
    caller asks it for every `--follow` arm, not only for `--replace`: a
    positive answer becomes `beacons.arm(reap=True)` and a refusal becomes
    `reap=False`, because `arm`'s default stop pass SIGTERMs an incumbent the
    census cannot prove — UNKNOWN, ghost or duplicate — which is the same act
    with the asking left out. A refused caller is not refused its beacon; it
    arms alongside and beacons names the way in."""
    from . import seats_common as _common
    own = _common.own_name()
    if not claimed:
        return None, ("helm chat wait: --replace requires --seat S so the "
                      "process proves whose beacon it may rotate")
    if own:
        if str(claimed).casefold() == str(own).casefold() \
                or str(_common.live_alias(claimed)[0] or "").casefold() \
                == str(own).casefold():
            return own, None
        return None, ("helm chat wait: --replace cannot rotate seat %r's "
                      "beacon: this process is %r. Rotation stops the "
                      "incumbent waiter and takes over its wake route, so it "
                      "is only ever a seat's own act — or a STATED "
                      "--on-behalf." % (claimed, own))
    if isinstance(on_behalf, OnBehalfCapability) and on_behalf.covers(claimed):
        return claimed, None
    _state, derived, _why = self_identity(session)
    bound = ("its session is bound to that seat on the roster, and a session "
             "id is INHERITED by every child this pane spawns — so that "
             "binding is a lookup any descendant can reproduce, not proof of "
             "who is calling"
             if derived and str(derived).casefold() == str(claimed).casefold()
             else "it has no declared identity of its own")
    return None, (
        "helm chat wait: --replace needs independent proof that this process "
        "IS %r and it has none: %s. Rotation SIGTERMs the live waiter serving "
        "that seat and registers this process as the wake route in its place. "
        "Either export HELM_CHAT_NAME=%s in the process that rotates, or state "
        "the grant with --on-behalf. Waiting WITHOUT --replace needs neither "
        "and still works — this pane arms its own beacon."
        % (claimed, bound, claimed))


def resolve_actor(session=None, cwd=None, asserted=None, act="act"):
    """(AdmittedActor, None) or (None, err) — the single admission pass.

    FOUR REFUSALS, IN THIS ORDER, and the order is load-bearing:

      MALFORMED   the declared name carries ESC / C0-C1 / bidi overrides.
                  `own_name()` swallows this to None, which then falls through
                  to the DERIVED floor and MINTS a name — a hostile env var
                  quietly became an anonymous actor instead of an error.
      DISPUTED    `identity_disagreement` — both sources answered and they
                  disagree (TAKEOVER or CLAIM-JUMP). The frozen first attempt
                  omitted this, so HELM_CHAT_NAME=seat-a with the session
                  rostered to seat-b was admitted as DECLARED seat-a: the
                  2026-08-02 P0 reintroduced by its own fix.
      UNRESOLVED  DERIVED — nobody declared, nobody rostered, a name was
                  minted from session+cwd. A stranger that passes every check.
      UNCORROBORATED  a name WAS declared and no session exists to check it
                  against. The disagreement pass returns nothing whether the
                  two sources agree or only one answered, so its silence is
                  not evidence. Dropped for SPEECH, because the bot identities
                  unset their session on purpose and only ever speak.
      MISASSERTED `--seat` named someone else. LAST, so asserting the derived
                  name cannot rescue an unresolvable identity.

    `asserted` is an ASSERTION, never a selector: a caller may state who it
    believes it is and be refused for being wrong; it may never NAME a seat
    into being. `act` shapes the message so a refusal says which boundary
    refused."""
    return _resolve(session, cwd, asserted, act)[:2]


def resolve_actor_reason(session=None, cwd=None, asserted=None, act="act"):
    """(AdmittedActor, err, reason) — `resolve_actor`, plus WHICH refusal fired.

    FOR A DOOR THAT MUST TREAT THE REFUSALS DIFFERENTLY. `resolve_speaker`
    already needs this and reaches `_resolve` from inside the module; a door in
    another module needs the same thing and must not have to guess the reason
    by re-reading the environment. Guessing there is precisely the trap this
    layer exists to close: HELM_CHAT_NAME is what the refusal is ABOUT, so
    consulting it to interpret the refusal is asking the suspect to confirm the
    alibi. The reason is a constant on this module, so a reworded message
    cannot silently open a caller's branch either.
    """
    return _resolve(session, cwd, asserted, act)


def _uncorroborated_message(act, declared, session):
    return (
        "refusing to %s as %r: this identity is DECLARED and nothing "
        "corroborates it — %s. A declared name is a value any process "
        "can export, so it is evidence only when a harness session the "
        "roster knows resolves to the same seat. Run from a seat that "
        "carries its own session, or bind one with `helm chat join`."
        % (act, declared,
           "no session was presented" if not session
           else "its session is not on the roster"))


def _resolve(session=None, cwd=None, asserted=None, act="act"):
    """(actor, err, reason) — the pass both doors share.

    The REASON is returned, not just the message, because the speech door has
    to drop exactly one refusal and no other. Branching on message text would
    be a guard that a reworded sentence silently opens."""
    from . import seats_identity as _ident
    from . import seats_roster as _roster

    try:
        declared = home.chat_name()
    except home.SeatNameError as e:
        return None, ("refusing to %s: HELM_CHAT_NAME is not a legitimate seat "
                      "identifier (%s). A hostile name is no name — it must not "
                      "become an actor by falling through to the derived floor."
                      % (act, e)), MALFORMED

    dis = _ident.identity_disagreement(session)
    if dis:
        own, bound = dis
        return None, ("refusing to %s under a disputed identity: %s"
                      % (act, _ident._dispute_sentence(own, bound, session))), \
            DISPUTED

    from . import seats_common as _common
    if declared:
        # THE DECLARED NAME IS READ THROUGH THE SAME SEAM EVERY OTHER DOOR
        # READS: a HELM_CHAT_NAME that is a live rename alias admits the
        # process AS the renamed row, and the evidence says so — the actor
        # that comes back is the new seat's, never a second identity minted
        # under the old spelling.
        name, alias = _common.declared_name()
        state = _ident.DECLARED
        if alias:
            evidence = (("declared-alias",
                         "HELM_CHAT_NAME=%s is a rename alias of %s until %s"
                         % (alias[0], name, pk.epoch_ts(alias[1]))),)
        else:
            name = declared
            evidence = (("declared", "HELM_CHAT_NAME"),)
    else:
        state, name = _ident.resolve_identity(session, cwd)
        evidence = (("rostered", str(session or "")),)

    if state == _ident.DERIVED:
        return None, (
            "refusing to %s as %r: this identity is DERIVED (minted from "
            "session+cwd), not declared and not rostered. A derived name is a "
            "stranger that passes every check — declare one with "
            "HELM_CHAT_NAME, or bind this session with `helm chat join`."
            % (act, name)), UNRESOLVED

    if asserted:
        claimed = str(asserted).strip()
        # THE SEAT'S OWN LIVE ALIAS IS THE SEAT: `--seat <old>` from the
        # renamed process inside the window asserts the identity it
        # resolves to, spelled the way its environ still spells it.
        alias_of = _common.live_alias(claimed)[0] if claimed else None
        if claimed.casefold() != str(name).casefold() \
                and str(alias_of or "").casefold() != str(name).casefold():
            return None, (
                "--seat %r cannot %s as another seat: this session resolves to "
                "%r (%s). --seat asserts an identity, it never selects one."
                % (claimed, act, name, state)), MISASSERTED

    # THE LAST REFUSAL, and it is about the AUTHORITY rather than the caller:
    # an unreadable or malformed store cannot be minted against, because
    # `by_name` is the only thing keeping a retired alias pointed at its actor
    # and an empty-looking one turns every known seat into a first sighting.
    # Refused for SPEECH too — `resolve_speaker` drops only the refusals about
    # a name having no BACKING (UNRESOLVED, UNCORROBORATED) — because the
    # hazard is the store, not the tier of the act. THAT IS WHY THE
    # CORROBORATION CHECK SITS BELOW THIS ONE and not above it: it is dropped
    # for speech, so running it first let an unreadable store mint a speaker.
    row, unavailable = _bind(name, evidence, commit=False)
    if unavailable:
        return None, "refusing to %s: %s" % (act, unavailable), UNAVAILABLE

    # A DECLARED NAME WITH NO SESSION HAS NOTHING BEHIND IT, AND THE
    # DISAGREEMENT CHECK ABOVE CANNOT SAY SO. `identity_disagreement`
    # answers by COMPARING two sources; with no session it has one, so it
    # returns nothing — which is byte-identical to the two sources agreeing.
    # Silence there means "not contradicted", never "corroborated", and
    # reading it as the second is how a declared name became sufficient on
    # its own.
    #
    # MEASURED ACROSS THE LIVE FLEET, which is what makes this safe to
    # require rather than merely correct to want: every live seat carries
    # CLAUDE_CODE_SESSION_ID (proxy families included — they run under the
    # harness with a proxy backend) and every one of them resolves through
    # the roster to its own declared name. The single process group that
    # did NOT was a seat with no roster row and no agent process left, only
    # orphaned children, whose session belonged to a different name
    # entirely — which is the confusion this refusal exists to surface, not
    # a cost of it.
    #
    # THE BOTS ARE THAT SHAPE ON PURPOSE AND MUST NOT BREAK. The owed-push
    # and stale units set HELM_CHAT_NAME and UnsetEnvironment the session
    # vars deliberately, because an inherited session would make their
    # declared name a DISPUTE. They are SPEAKERS: they reach chat through
    # the on-behalf-of string branch of `attributed`, never through this
    # pass. So this refusal is dropped at the speech door, exactly as
    # UNRESOLVED is, and bites only where an ACT is being authorized.
    # AND THE SOURCE MUST BE THE ROSTER, NOT `resolve_identity`, WHICH
    # READS HELM_CHAT_NAME ITSELF. Comparing that function's answer to the
    # declared name compares the name to ITSELF, so it admits everything —
    # including a session id invented on the spot. `seat_for_session` is
    # the roster map alone, and it cannot be fed the value it is
    # checking.
    #
    # AND THE QUESTION IS "IS THIS SESSION ROSTERED AT ALL", because the
    # case where it is rostered to somebody ELSE is already refused above
    # as DISPUTED, with a better message. What is left for this branch is
    # the silence: no session, or a session no roster has ever seen.
    # ONLY THE DECLARED TIER. A ROSTERED identity was resolved BY its session
    # in the first place, so it is corroborated by construction and asking
    # again would answer yes; guarding on the tier keeps this refusal's
    # message true rather than merely its outcome right.
    def corroboration_refusal():
        current = _roster.seat_for_session(session) if session else None
        if current and str(current).casefold() == str(name).casefold():
            return None
        if state == _ident.DECLARED:
            if current:
                return ActorRefused(
                    DISPUTED,
                    "refusing to %s under a disputed identity: %s"
                    % (act, _ident._dispute_sentence(declared, current, session)))
            return ActorRefused(
                UNCORROBORATED,
                _uncorroborated_message(act, declared, session))
        return ActorRefused(
            UNAVAILABLE,
            "refusing to %s as %r: the roster binding that selected this "
            "ROSTERED identity changed during Actor admission (now %r). Retry "
            "after the roster is stable." % (act, name, current))

    refused = corroboration_refusal()
    if refused is not None:
        return None, str(refused), refused.reason

    # A FIRST ADMISSION HAS ONLY BEEN PREFLIGHTED. Repeat the locked pass with
    # the corroboration callback: it re-reads the roster at the commit seam,
    # after every store/generation/fence fact, so neither half of the plan can
    # go stale and mint an Actor by itself.
    if row is _UNCOMMITTED_ADMISSION:
        row, unavailable = _bind(name, evidence, commit=True,
                                 admit=corroboration_refusal)
        if isinstance(unavailable, ActorRefused):
            return None, str(unavailable), unavailable.reason
        if unavailable:
            return None, "refusing to %s: %s" % (act, unavailable), UNAVAILABLE

    # EVERY KEY HERE IS INDEXED, NOT `.get`-WITH-A-DEFAULT: `_store_defect`
    # guarantees actor_id/canonical_name/aliases and validates any explicit
    # revision. A legacy row without a revision starts at 1 without a write;
    # `v` is the store FORMAT and must not stand in for its lifecycle revision.
    return AdmittedActor(row["actor_id"], row["canonical_name"],
                         aliases=row["aliases"], evidence=evidence,
                         version=row.get("revision", 1),
                         mint=_MINT), None, None


def actor_name(actor, act="act"):
    """(name, err) — THE gate every actor-attributed mutation calls.

    A raw seat string is REFUSED here, and that refusal is the whole design: a
    string is render material and addressing material, and the four functions
    that hand one out (`derive_seat`, `acting_seat`, `chat.whoname`,
    `seat_for_session`) all answer for a stranger just as readily as for a
    seat. Only `resolve_actor` can produce the capability, so the only way to
    satisfy this gate is to have passed that pass."""
    if isinstance(actor, AdmittedActor):
        return actor.canonical_name, None
    if actor is None:
        return None, ("refusing to %s: no admitted actor was presented. "
                      "Resolve one with helm.actors.resolve_actor()." % act)
    return None, (
        "refusing to %s: %r is a raw seat name, not an admitted actor. A name "
        "is render/addressing material — it is what `derive_seat` hands back "
        "for a stranger it just minted. Authorization comes from "
        "helm.actors.resolve_actor(), which refuses malformed, disputed and "
        "derived identities." % (act, actor))


def attributed(who, session=None, cwd=None, act="act"):
    """(name, err) — THE one definition of "who is this row/act attributed to",
    shared by every actor-attributed writer so none of them re-derives it.

    Three inputs, three different authorities, and conflating them is the whole
    defect class:

      None            AMBIENT — "whoever I am". This is the case that used to
                      fall through `who or seat_for_session(session) or
                      derive_seat(session)` and MINT a stranger. It now
                      requires an admitted actor and REFUSES without one.
      AdmittedActor   AUTHORIZED — the capability was obtained by passing
                      `resolve_actor`, so malformed / disputed / derived /
                      mis-asserted were all refused upstream. This is what the
                      CLI door (`chat._seat_actor`) now hands down, which is
                      how the CLI leg stopped being a bypass: its `who` can no
                      longer BE a derived name.
      str             ON-BEHALF-OF — an explicit named party, the authority the
                      claims lane already models (a handed lease records the
                      RECEIVER's holder and the CALLER's session). A bot
                      identity, a web/TUI surface naming itself, a fixture
                      staging a peer. It is deliberately still accepted, and
                      deliberately NOT reachable from an ambient resolution:
                      no production caller may compute one from `derive_seat`,
                      `acting_seat` or `whoname` — tests/test_identity_layer.py
                      holds the census that enforces it.
    """
    if isinstance(who, AdmittedActor):
        return who.canonical_name, None
    if who is None:
        actor, err = resolve_actor(session, cwd, act=act)
        return (None, err) if err else (actor.canonical_name, None)
    name = str(who).strip()
    return (name, None) if name else (
        None, "refusing to %s: an empty name is not an actor" % act)


def name_of(actor):
    """The label, or None — for RENDER only, and never an authorization. Takes
    a capability or a string, because a display surface legitimately gets both
    (a row's recorded author is a string forever)."""
    if isinstance(actor, AdmittedActor):
        return actor.canonical_name
    return str(actor) if actor else None
