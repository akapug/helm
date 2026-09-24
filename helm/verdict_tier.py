"""Record-time approval policy inputs and their deterministic interpretation.

The trusted store history owns policy observations; dispatch verdicts reference
those versions. Content anchors bind inputs, not authenticate a hostile rewrite
of both sources. This evaluator reads no current roster, runtime, clock or store.
"""
import re


_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
from .store.policy_history import POLICY_FIELDS


def evaluate(policy, recipient, families):
    """Interpret a captured certain policy; unknown inputs never permit."""
    from . import seats
    from .dispatches import TIER_DAMAGED, TIER_DARK, TIER_UNNAMED, _tier_unknown
    canonical, err = seats._canonical_recipient(recipient)
    if err:
        return _tier_unknown(TIER_UNNAMED, err)
    if policy is None:
        return "none", None
    if not isinstance(policy, dict) or set(policy) != set(POLICY_FIELDS):
        return _tier_unknown(TIER_DAMAGED, "recorded approval policy is malformed")
    if not isinstance(policy["id"], str) or not policy["id"] \
            or policy["class"] != "certain" \
            or policy["_policy_confidence_valid"] is not True \
            or policy["_policy_source_valid"] is not True \
            or not isinstance(policy["policy_kind"], str) \
            or policy["policy_kind"].strip().casefold() != "approval-tier":
        return _tier_unknown(TIER_DAMAGED, "recorded approval policy is not certain and human-sourced")
    reason, members = policy["policy_reason"], policy["policy_members"]
    if not isinstance(reason, str) or not reason.strip() \
            or not isinstance(members, list) or not members:
        return _tier_unknown(TIER_DAMAGED, "recorded approval policy lacks reason or members")
    exact, selected = set(), set()
    for selector in members:
        if not isinstance(selector, str):
            return _tier_unknown(TIER_DAMAGED, "recorded approval policy has malformed selector")
        head, sep, token = selector.strip().partition(":")
        if sep != ":" or head not in ("seat", "family") or not _TOKEN.fullmatch(token):
            return _tier_unknown(TIER_DAMAGED, "recorded approval policy has malformed selector")
        (exact if head == "seat" else selected).add(token.casefold() if head == "seat" else token)
    if any(ord(c) < 32 or ord(c) == 127 for c in reason):
        return _tier_unknown(TIER_DAMAGED, "recorded approval policy contains control characters")
    if canonical in exact:
        return "ok", None
    if selected:
        if not families:
            return _tier_unknown(TIER_DARK, "no immutable verdict-time runtime family evidence")
        if not isinstance(families, (set, frozenset)) or len(families) != 1 \
                or any(not isinstance(f, str) or not _TOKEN.fullmatch(f) for f in families):
            return _tier_unknown(TIER_DAMAGED, "conflicting or malformed recorded runtime families")
        if next(iter(families)) in selected:
            return "ok", None
    return "outside", ("@%s is outside the recorded approval tier; reason: %s; "
                       "source prior: %s; valid set: %s" %
                       (canonical, reason, policy["id"], ", ".join(sorted(set(members)))))
