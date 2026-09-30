#!/usr/bin/env python3
"""The provider families the seat catalog declares, as candidate rows for
:mod:`helm.accounts`.

WHY THE SEED WAS NOT ENOUGH. `accounts.seed_rows` fills an inventory from what
helm MEASURES, and a family helm cannot measure is invisible to it — which is
every key-mode family on this host. The owner then reads a card that looks
complete and is not, and the fleet goes on guessing about exactly the accounts
nobody wrote down. The catalog already knows those families exist: it holds the
port, the mode, the provider and the key env for each one. So the roster is
DERIVED FROM THE CATALOG rather than typed here, and a family added to the
catalog tomorrow appears on his card with no edit to this file.

NOTHING IS INVENTED AND NOTHING IS PRICED. A candidate carries the vendor and
the reach the catalog can prove and leaves plan, price, billing and allowance
empty for the owner — the seed's standing law, because a derived guess
presented as his own words is worse than a blank he can fill. Every row is
UNCONFIRMED and says where it came from.

A POOL PROVIDER IS ITS OWN ACCOUNT, AND THE FAMILY FRONTING THEM IS NOT. A
family in pool mode fronts several upstream accounts, each with its own key and
its own bill (`pool_providers`); a single row for the family would hide all but
one of them, so every pool provider is its own candidate. The family itself is
then a ROUTE and no longer a bill, and a row for it as well is a subscription
the owner does not hold — he read `ds4pro` back to us sitting beside `deepseek`,
which it pools, and said the two were one DeepSeek account. So a family that
declares pool providers mints rows for THEM and none for itself. A family that
declares none is still its own candidate: that is the ordinary case, and the
only thing such a row could mean.

A GPU ON THE OPERATOR'S OWN BOX IS NOT AN ACCOUNT EITHER. A pool row served
from his own hardware has no key and no bill, so it mints no row, and a family
served only from such rows mints none.

WHO IS BILLED IS THE CATALOG'S ONE READING, `seat.billing_accounts`: each
(account, group) it names is a candidate, the account as its id and the group
as its vendor. Fleet › credit joins its Families card to the accounts table on
the same reading (task/3461), so a row this seeder mints is a group that card
can link; a vendor worked out here a second way was how a fresh home got
`cursor-bridge` and `opencode-go` rows the card could not match.

A MODEL IS NOT AN ACCOUNT. The catalog also names models it routes to — some of
them vendor-branded — and a row for each of those would invent subscriptions
the owner does not hold. Only families and their pool providers become rows.

ORCA SEES ACCOUNTS THE CREDENTIAL HOMES HIDE. The measured seed can only find a
login some home on this host is logged into, and it reads the home's NAME for
it; when a home's name lies, the account it really holds is counted under the
wrong login and the account it is named for is counted not at all. The owner
found this the only way anyone could — by reading his own card and missing a
plan he pays for. Orca keeps its own claude account store and states each
login outright, so it answers exactly the question a misnamed home makes
unanswerable. It is read for IDENTITIES ONLY: an email, never a token.
"""
import os

from . import pk

#: What a candidate says about itself until the owner says otherwise.
SEEDED_FROM = "helm's seat catalog"
#: …and what a row found only in Orca's account store says.
SEEDED_FROM_ORCA = "Orca's claude account store"
UNKNOWN_PLAN = "unknown plan"


def _catalog_families():
    """The catalog's family table, or {} where it cannot be read.

    NEVER A RAISE. The declared inventory is readable on a host with no catalog
    at all, and a seeder that exploded on one would take the owner's whole card
    with it — `accounts._measured` states the same law for the quota provider.
    """
    try:
        # THROUGH THE FACADE. helm.seat is the door every consumer of the
        # catalog is meant to use; reaching past it into seat_catalog is what
        # tests/test_seat_facade_injection.py refuses, and this module is a
        # leaf that has no reason to know the impl module's name.
        from . import seat
        table = getattr(seat, "FAMILIES", None)
        return table if isinstance(table, dict) else {}
    except Exception:                       # noqa: BLE001 — the roster is optional
        return {}


def _billing(family, table):
    """`seat.billing_accounts` for one family of `table`, or None where it
    cannot be read — never a raise, `_catalog_families`' law."""
    try:
        from . import seat
        return seat.billing_accounts(family, table)
    except Exception:                       # noqa: BLE001 — the roster is optional
        return None


def candidates(not_described_for, not_described_not, families=None):
    """[{row}] — one candidate per account `seat.billing_accounts` names: a
    family with no pool, or each pool provider off the operator's own box.

    The two placeholder sentences are PASSED IN rather than imported, so this
    module stays a leaf of accounts.py instead of a cycle with it and there is
    still exactly one spelling of "nobody has described this yet" in the tree.

    The rows are NOT validated here and NOT written here: `accounts.seed` owns
    the one door every writer uses, and a second validator in front of it is a
    second opinion about what is legal."""
    table = _catalog_families() if families is None else families
    if not isinstance(table, dict):
        return []
    rows, seen = [], set()

    def _add(account_id, vendor, reach):
        if not account_id or account_id in seen:
            return
        seen.add(account_id)
        rows.append({
            "id": account_id,
            "vendor": vendor,
            "plan": UNKNOWN_PLAN,
            "count": 1,
            "good_for": not_described_for,
            "not_for": not_described_not,
            "reach": reach,
            "seeded_from": SEEDED_FROM,
        })

    for family in sorted(table):
        spec = table[family] if isinstance(table[family], dict) else {}
        reach = ("the %s seats, through their %s pool" % (family, family)
                 if spec.get("pool_providers") else "the %s seats" % family)
        for account, vendor in _billing(family, table) or ():
            _add(account, vendor, reach)
    return rows


def orca_identities():
    """[{"email": str}] — the logins Orca's claude account store holds.

    NEVER A RAISE AND NEVER A TOKEN. The store may be absent (no Orca on this
    host), unreadable, or hold a directory whose identity file is missing — all
    three are the same answer here, "nothing to add", because a seeder that
    exploded on one would take the owner's whole card with it, which is
    `_catalog_families`' law and `accounts._measured`'s. Only `emailAddress` is
    read out of each account: the credential sitting beside it in that same
    directory is none of this module's business."""
    try:
        from .cred import orca
        root = orca.orca_accounts_root()
        names = sorted(os.listdir(root))
    except Exception:                       # noqa: BLE001 — the source is optional
        return []
    out, seen = [], set()
    for name in names:
        try:
            doc = pk.read_json(os.path.join(root, name, orca.ORCA_AUTH_DIR,
                                            orca.ORCA_ACCOUNT_JSON), default=None)
        except Exception:                   # noqa: BLE001 — one bad dir, not the store
            continue
        email = (doc or {}).get("emailAddress") if isinstance(doc, dict) else None
        if not isinstance(email, str):
            continue
        email = email.strip()
        key = email.lower()
        if not email or key in seen:
            continue
        seen.add(key)
        out.append({"email": email})
    return out

