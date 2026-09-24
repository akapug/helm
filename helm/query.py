"""helm query — the single authoritative query facade for ledger and landedness state (Step 5, #163).

Unifies the duplicated status derivations across landreq, dispatches, cli, envtidy,
foldcheck, gate, landgate, docref_guard, and web_land_model behind ONE facade.

FACADE CONTRACT:
  - query_is_open(row) -> bool: True if status is open or held (unified _not_closed).
  - query_is_unheld_open(row) -> bool: True if status is strictly "open".
  - query_is_held(row) -> bool: True if status is strictly "held".
  - query_did_land(gitdir, tip, ref, mode="landed_ever") -> bool or None:
      Delegates to Step 2 predicates (ancestry / patch_identity / landed_ever).
      content_presence is DELIBERATELY absent — see ALLOWED_LANDED_MODES.
  - query_is_stalled(row, now=None) -> bool: True if row is stalled, False if owner_gated/retired.
  - query_is_owner_gated(row) -> bool: True if row hold is explicitly owner-gated.
"""

from . import dispatches, landreq

# CONTENT_PRESENCE IS NOT HERE, AND ITS ABSENCE IS THE HONEST ANSWER.
# It was allowlisted with NO implementation anywhere in this codebase, so
# getattr(landreq, "content_presence") returned None and the call fell through
# to the deprecated landreq._landed — asking for the ONE revert-aware fact
# silently returned the predicate that cannot see a revert.
#
# landreq.landed_ever's own docstring settles why it cannot simply be written
# here: net tree presence — "are these bytes on trunk RIGHT NOW" — is not
# answerable from ancestry OR patch-identity, "and nothing else here answers it
# either". Implementing it needs a tree-content probe and a land-then-revert arm
# against REAL git, which is its own lane. task/958's gap stays DECLARED rather
# than filled by a name that resolves to something weaker.
ALLOWED_LANDED_MODES = ("ancestry", "patch_identity", "landed_ever")


def query_is_retired_admin(row):
    """Was this row administratively retired.

    A SEPARATE FIELD, NOT A STATUS WORD, and that is the whole reason the
    readers below had to change. Retirement replay PRESERVES `status` — an
    OPEN row that is retired stays status=open and gains `retired_admin` —
    because the row is append-only and its status is a historical fact about
    what the parties did, not a claim about whether anyone still owes it.
    """
    if not isinstance(row, dict):
        return False
    return bool(row.get("retired_admin"))


def query_is_open(row):
    """Is a dispatch or landreq row an active obligation (OPEN or HELD).

    Unifies dispatches._open and dispatches._not_closed into one canonical rule:
    a held row is an active open obligation, not closed.

    RETIRED IS TERMINAL, AND READING STATUS ALONE MISSED IT (verified
    against the target modules: a retired OPEN child returned
    query_is_open=True). Retirement preserves `status` and only adds
    `retired_admin`, so every reader keyed on the status word alone kept
    retired rows live — `dispatches.owed` and `open_rows` went on feeding
    wake, work-offer and stalebot with obligations the door had just cleared,
    and the count the verb promises to clear never cleared.

    So the canonical rule is status AND not-retired. It is canonical
    precisely so this cannot be fixed in one caller and missed in the others,
    which is the shape that produced the bug: the status test was correct and
    duplicated, and the retirement field arrived after it.
    """
    if not isinstance(row, dict):
        return False
    if query_is_retired_admin(row):
        return False
    status = str(row.get("status") or "").strip().lower()
    return status in ("open", "held")


def query_is_unheld_open(row):
    """Is a row strictly OPEN (not held and not closed)."""
    if not isinstance(row, dict):
        return False
    if query_is_retired_admin(row):
        return False          # retired is terminal, whatever the status word
    status = str(row.get("status") or "").strip().lower()
    return status == "open"


def query_is_held(row):
    """Is a row strictly HELD."""
    if not isinstance(row, dict):
        return False
    if query_is_retired_admin(row):
        return False          # retired is terminal, whatever the status word
    status = str(row.get("status") or "").strip().lower()
    return status == "held"


def query_is_owner_gated(row):
    """Is a row explicitly held on an owner decision."""
    if not isinstance(row, dict):
        return False
    return bool(row.get("owner_gated"))


def query_is_stalled(row, now=None):
    """Is a row STALLED (past threshold, non-terminal, NOT owner-gated)."""
    if not isinstance(row, dict):
        return False
    if query_is_owner_gated(row):
        return False
    if query_is_retired_admin(row):
        return False          # retired is terminal, whatever the status word
    status = str(row.get("status") or "").strip().lower()
    if status in ("closed", "cancelled", "landed", "superseded", "withdrawn", "abandoned"):
        return False
    return bool(row.get("stalled"))


def query_did_land(gitdir, tip, ref, mode="landed_ever"):
    """Did `tip` land on `ref` in `gitdir` under specified predicate `mode`.

    `mode` MUST be one of `ALLOWED_LANDED_MODES` (`ancestry`, `patch_identity`,
    `landed_ever`). Arbitrary callable invocation is refused, and an allowlisted
    mode with no predicate RAISES rather than substituting a weaker one.

    THERE IS NO content_presence. Net tree presence — are these bytes on `ref`
    RIGHT NOW — is not derivable from ancestry or patch-identity, and nothing in
    this codebase answers it; landreq.landed_ever's docstring says so outright.
    Every mode here is therefore a HISTORICAL fact, and a caller that needs a
    current one has no predicate to ask for (task/958, declared not filled).
    """
    if mode not in ALLOWED_LANDED_MODES:
        raise ValueError("mode %r not in ALLOWED_LANDED_MODES %r" % (mode, ALLOWED_LANDED_MODES))
    fn = getattr(landreq, mode, None)
    if fn is None:
        # NO SILENT FALL-THROUGH. This branch used to call landreq._landed, so
        # an allowlisted-but-missing predicate answered with a DIFFERENT and
        # weaker fact under the requested name. A mode that passes the allowlist
        # and has no implementation is a programming error in this module, not a
        # runtime condition a caller can handle — and the whole point of a
        # facade is that the name you ask for is the predicate you get.
        raise AssertionError(
            "mode %r is allowlisted but landreq has no such predicate — the "
            "facade must not substitute a weaker one" % (mode,))
    return fn(gitdir, tip, ref)
