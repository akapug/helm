"""WHERE A BEACON CAME FROM, ASKED OF THE PRODUCER INSTEAD OF THE TEXT.

THE PROBLEM THIS REPLACES. A seat's wake route is one `helm chat wait
--follow` beacon. When a SUBAGENT arms, replaces or stops it, the seat takes
that subagent's beacon as its own and goes deaf while every surface reads
covered. Telling the two apart is the whole job, and ten rounds of reading the
transcript could not do it, for a reason that is a property and not a bug:

    A SESSION JOIN CANNOT CLASSIFY MAIN VERSUS SIDECHAIN. Both inherit the
    parent session id (seats_delegation.py already records this), the registry
    row's `waiter` dict carries BEHAVIOUR rather than ORIGIN, and the union of
    every field across every live row holds no agent_id, no isSidechain, no
    origin and no parent. The only evidence the reader could reach was TEXT
    THE SUBAGENT CONTROLS.

So this module does not read. It carries a STAMP minted by a producer that
knows its own origin, handed to the waiter at the moment it registers, and
recorded causally into that exact registration row. The transcript is not
consulted and no shell grammar is modelled.

THE FOUR PROPERTIES THE STAMP MUST HAVE, and each one is a refusal here:

  NONCE-KEYED     the env carries an opaque nonce, never the answer, so a
                  copied variable names a stamp rather than asserting one
  TOOL_USE-BOUND  a stamp names the exact tool call it was minted for, so it
                  cannot drift onto a different call in the same session
  SINGLE-USE      reading it CONSUMES it; the second reader gets UNKNOWN, so a
                  replayed nonce can never mint a second proven origin
  FAIL-CLOSED     missing, stale, malformed, conflicting or already-consumed
                  all answer UNKNOWN. UNKNOWN IS NOT AN ANSWER and is never
                  presumed main -- a beacon with no stamp is unattributed, and
                  an unattributed beacon must never be credited to the seat.

THE ERROR CLASSES CHANGE, WHICH IS THE POINT. The producer still has to
recognise "is this command a beacon arm" to know what to stamp, and that is a
parse. But a parse that MISSES an arm yields no stamp, which is UNKNOWN; and a
parse that stamps a NON-arm yields a stamp no registration ever consumes,
because no waiter registers. Neither error can produce a FALSE PROVEN, and a
false proven -- a seat credited with a beacon it does not have -- is the only
failure the census exists to prevent.

WHAT IS NOT CLAIMED HERE. This module is the CONSUMER half. It is indifferent
to how the stamp reaches the process -- argv, environment, or anything else --
so that the producer can be built against a measurement rather than a guess.
Nothing in this file asserts that any particular producer exists yet.
"""

import json
import os
import string
import time

from . import home, pk

#: A beacon armed by the session's own main turn loop. The seat's wake route.
ORIGIN_MAIN = "main"
#: A beacon armed from a sidechain -- an Agent subagent's tool call. This is
#: the misrouting the census exists to name.
ORIGIN_SUBAGENT = "subagent"
#: NOT AN ANSWER. Every refusal below returns this, and every consumer must
#: treat it as "nobody has established where this came from", never as main.
ORIGIN_UNKNOWN = "unknown"

ORIGINS = frozenset((ORIGIN_MAIN, ORIGIN_SUBAGENT))

#: The environment variable carrying the NONCE -- never the origin itself.
#: A process that copies this variable copies a NAME; the answer lives in a
#: stamp file that reading destroys.
STAMP_ENV = "HELM_BEACON_ORIGIN_STAMP"

#: A stamp is minted immediately before the waiter it describes is spawned, so
#: the honest window is seconds, not minutes. Beyond it the stamp may describe
#: a different call entirely and is refused as stale. The number is generous
#: against a loaded box and still far short of a second arm of the same seat.
STAMP_TTL_S = 120.0

_STAMP_SUBDIR = "beacon-stamps"

#: 16 random bytes rendered as hex. The alphabet is DERIVED rather than
#: spelled out: a literal run of sixteen hex characters in this file reads to
#: helm's docref rung as a cited commit it cannot resolve, and refuses the
#: commit. Deriving it also means the check and the minting agree by
#: construction instead of by two copies staying in step.
NONCE_BYTES = 16
NONCE_LEN = NONCE_BYTES * 2
_HEX = frozenset(string.hexdigits.lower())


def stamp_dir():
    return os.path.join(home.global_dir(), ".state", _STAMP_SUBDIR)


def _stamp_path(nonce):
    return os.path.join(stamp_dir(), "%s.json" % nonce)


def valid_nonce(nonce):
    """A nonce is a bare lowercase hex word of a fixed length.

    THIS IS A PATH GUARD BEFORE IT IS A FORMAT CHECK. The nonce arrives in an
    environment variable and is joined into a filesystem path, so anything
    holding a separator, a dot segment or a null must never reach `open`. The
    narrow alphabet refuses all three by construction rather than by
    enumerating what to strip."""
    if not isinstance(nonce, str) or len(nonce) != NONCE_LEN:
        return False
    return all(c in _HEX for c in nonce)


def mint(origin, tool_use_id, session=None, now=None, nonce=None):
    """Write a stamp and return its nonce, or None if it could not be written.

    THE CALLER IS THE PRODUCER AND MUST KNOW ITS OWN ORIGIN. This function
    asserts nothing about where `origin` came from; it refuses a value that is
    not one of the two real answers, so a producer that cannot tell simply does
    not mint and the waiter answers UNKNOWN.

    `nonce` exists for tests that need a deterministic path. Production passes
    None and gets a fresh one, because a caller-chosen nonce is a caller-chosen
    filename and the single-use property rests on that name being unguessable.
    """
    if origin not in ORIGINS or not tool_use_id:
        return None
    if nonce is None:
        nonce = os.urandom(NONCE_BYTES).hex()
    if not valid_nonce(nonce):
        return None
    row = {"origin": origin,
           "tool_use_id": str(tool_use_id),
           "session": session or home.session_id(),
           "minted": float(now if now is not None else time.time()),
           "minter_pid": os.getpid()}
    try:
        os.makedirs(stamp_dir(), exist_ok=True)
        pk.write_json(_stamp_path(nonce), row)
    except OSError:
        return None
    return nonce


def _discard(path):
    """Best-effort removal for a stamp whose read ALREADY failed.

    Distinct from the unlink inside `consume`: there the removal is the
    single-use guarantee and its failure has to be reported, while here the
    caller is returning a refusal whatever happens, so a stamp that cannot be
    removed changes no answer and only waits for `reap`."""
    try:
        os.unlink(path)
    except OSError:
        pass


def consume(nonce, session=None, now=None):
    """(stamp, reason) -- read a stamp ONCE and destroy it.

    Returns ({origin, tool_use_id, session, minted}, None) on success, or
    (None, reason) for every refusal. The reason is a short machine token so a
    census can aggregate WHY it is unattributed without holding the values.

    THE UNLINK COMES BEFORE THE PARSE, deliberately. If the file is removed
    first, two racing readers cannot both succeed whatever either then does
    with the bytes; if the parse came first, a malformed stamp would be read
    twice and a valid one could be consumed by both. Single-use is a property
    of the REMOVAL, so the removal is what must be atomic."""
    if not nonce:
        return None, "absent"
    if not valid_nonce(nonce):
        return None, "malformed_nonce"
    path = _stamp_path(nonce)
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            raw = f.read(64 * 1024)
    except FileNotFoundError:
        # Already consumed, never minted, or minted under another home. All
        # three are the same answer to the only question being asked.
        return None, "consumed_or_absent"
    except (OSError, ValueError):
        _discard(path)
        return None, "unreadable"
    # THE REMOVAL IS THE GUARANTEE, SO A FAILED REMOVAL IS A REFUSAL.
    # Swallowing this left the stamp on disk after a successful read, and a
    # later reader could consume it again -- single-use silently degraded to
    # best-effort exactly when the filesystem is behaving adversarially, which
    # is the only time it matters. FAIL-CLOSED is this module's stated law and
    # it applies to its own housekeeping: if the stamp cannot be destroyed,
    # nobody may be credited with it, because a stamp that outlives one read
    # is a second PROVEN waiting to happen.
    try:
        os.unlink(path)
    except OSError:
        return None, "unconsumable"
    try:
        row = json.loads(raw)
    except ValueError:
        return None, "malformed"
    if not isinstance(row, dict) or row.get("origin") not in ORIGINS:
        return None, "malformed"
    if not row.get("tool_use_id"):
        return None, "unbound"
    minted = row.get("minted")
    if not isinstance(minted, (int, float)):
        return None, "malformed"
    age = (now if now is not None else time.time()) - float(minted)
    if age < 0 or age > STAMP_TTL_S:
        return None, "stale"
    want = session if session is not None else home.session_id()
    if want and row.get("session") and row["session"] != want:
        # The stamp is real and belongs to a DIFFERENT conversation. That is a
        # conflict rather than an absence, and it is named separately so a
        # census can tell a missing producer from a crossed one.
        return None, "session_conflict"
    return row, None


def from_env(env=None, session=None, now=None):
    """(stamp, reason) for THIS process, consuming the nonce in its own
    environment. The single call a waiter makes at registration."""
    src = os.environ if env is None else env
    return consume(src.get(STAMP_ENV), session=session, now=now)


def row_fields(stamp, reason):
    """The registry-row fields for a consumed stamp, or for its absence.

    ALWAYS RETURNS AN ORIGIN, and it is UNKNOWN whenever anything at all went
    wrong. A row that simply omitted the field on failure would be
    indistinguishable from a row written by a helm that predates this module,
    and those two must not read alike: the first is a beacon whose origin was
    asked and not established, the second is a beacon nobody asked about."""
    if not stamp:
        return {"origin": ORIGIN_UNKNOWN, "origin_reason": reason or "absent"}
    return {"origin": stamp["origin"],
            "origin_reason": None,
            "origin_tool_use": stamp["tool_use_id"],
            "origin_minted": stamp.get("minted")}


def reap(now=None, ttl=None):
    """Remove stamps past their TTL; returns how many went.

    A MINTED STAMP THAT IS NEVER CONSUMED IS THE NORMAL CASE, not an error: the
    producer stamps a command that may not register a waiter at all, which is
    exactly the fail-closed direction. Those files must not accumulate, and
    they are never load-bearing once stale, so the reaper needs no lock and an
    unreadable entry is skipped rather than raised."""
    horizon = (now if now is not None else time.time()) - float(
        STAMP_TTL_S if ttl is None else ttl)
    gone = 0
    try:
        names = os.listdir(stamp_dir())
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(stamp_dir(), name)
        try:
            if os.stat(path).st_mtime > horizon:
                continue
            os.unlink(path)
            gone += 1
        except OSError:
            continue
    return gone
