"""Record-time approval policy inputs and their deterministic interpretation.

The trusted store history owns policy observations; dispatch verdicts reference
those versions. Content anchors bind inputs, not authenticate a hostile rewrite
of both sources. This evaluator reads no current roster, runtime, clock or store.
"""
import re


_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
from .store.policy_history import POLICY_FIELDS


#: The rule an admission gives a reader's same-family reads. FAMILY is every
#: admission a record could carry before a policy named a model: a same-family
#: read never counts toward the rules that ask for two families. NON-AUTHOR is
#: a reader a `model:` selector admits (the owner's ruling that Opus seats are
#: in the upper tier): its same-family read counts when the reader wrote none
#: of the work (`landreq.non_author_error`).
RULE_FAMILY = "family"
RULE_NON_AUTHOR = "non-author"


def evaluate(policy, recipient, families):
    """Interpret a captured certain policy; unknown inputs never permit.

    THE GRAMMAR EVERY v1 AND v2 TIER RECORD WAS DERIVED UNDER: `seat:` and
    `family:` selectors, and anything else is malformed. It stays exactly
    that, so a record minted before `model:` existed replays as it did."""
    state, why, _rule = _admit(policy, recipient, families, None, False)
    return state, why


def admit(policy, recipient, families, model):
    """(state, why, rule) — `evaluate` with the `model:` selector.

    `model:<id>` admits the reader whose recorded runtime model is that id
    (case folded, a `[1m]`-style window suffix dropped, a provider prefix
    ignored), and gives it the NON-AUTHOR rule; a seat or family admission
    gives the FAMILY rule. A reader with no recorded model, or another
    model, is never admitted by a `model:` selector: it keeps whatever rule
    the rest of the policy gives it."""
    return _admit(policy, recipient, families, model, True)


def admit_window(policy, recipient, family, models):
    """(state, why) — `admit` for EVERY model one read may have been made
    on: a native seat's own answer and each other model its subagents named
    in the window before the read (`native_turn.native_turn_candidates`).

    ONE model is its own admission, under any rule. SEVERAL are admitted only
    when the ambiguity cannot change the answer: each model of `family`
    (`window_family`), and each "ok" by the FAMILY rule. A `model:` selector
    admits one id, so when the ids disagree, which one read decides, and the
    window cannot say."""
    several = len(models) > 1
    for model in models:
        if several and window_family(model, family) != family:
            return "outside", ("the window before the read named %s, which "
                               "is not a %s model" % (model, family))
        state, why, rule = admit(policy, recipient, {family}, model)
        if state != "ok":
            return state, why
        if several and rule != RULE_FAMILY:
            return "outside", ("a per-model rule admits %s, and the window "
                               "also named %s" % (model, ", ".join(
                                   m for m in models if m != model)))
    return "ok", None


#: Model ids helm's catalog does not carry that a NATIVE Claude seat's own
#: window was measured naming, read as the claude family for APPROVAL only
#: (`window_family`). Routing never reads this table: the proxy-routing
#: catalog is `seat_catalog`, and an id here routes nowhere new. A new id is
#: added only with measured evidence, a native seat's transcript that names
#: it, never because its spelling looks like a Claude id; every other
#: uncatalogued id fails closed. `claude-opus-4-8` is the id a Workflow
#: agent of a native Opus seat writes in `subagents/workflows/`.
NATIVE_WINDOW_MODELS = frozenset({"claude-opus-4-8"})


def window_family(model, family):
    """The family of one model a native seat's window named, or None.

    A model helm catalogues answers with its catalogued family
    (`dispatches._model_family`). An uncatalogued id is the claude family
    only when the seat's family is claude and the id is one
    `NATIVE_WINDOW_MODELS` names. Any other id is unknown, whatever its
    spelling: `claude-foo` is what an invented model looks like."""
    from .dispatches import _model_family, _model_key
    found = _model_family(model)
    if found:
        return found
    return "claude" if family == "claude" \
        and _model_key(model) in NATIVE_WINDOW_MODELS else None


def _model_keys(model):
    """The spellings one recorded model id answers to."""
    from .dispatches import _model_key
    key = _model_key(model)
    return {key, key.rsplit("/", 1)[-1]} - {""}


def _admit(policy, recipient, families, model, models):
    from . import seats
    from .dispatches import TIER_DAMAGED, TIER_DARK, TIER_UNNAMED, _tier_unknown

    def unknown(kind, text):
        return _tier_unknown(kind, text) + (RULE_FAMILY,)
    canonical, err = seats._canonical_recipient(recipient)
    if err:
        return unknown(TIER_UNNAMED, err)
    if policy is None:
        return "none", None, RULE_FAMILY
    if not isinstance(policy, dict) or set(policy) != set(POLICY_FIELDS):
        return unknown(TIER_DAMAGED, "recorded approval policy is malformed")
    if not isinstance(policy["id"], str) or not policy["id"] \
            or policy["class"] != "certain" \
            or policy["_policy_confidence_valid"] is not True \
            or policy["_policy_source_valid"] is not True \
            or not isinstance(policy["policy_kind"], str) \
            or policy["policy_kind"].strip().casefold() != "approval-tier":
        return unknown(TIER_DAMAGED, "recorded approval policy is not certain and human-sourced")
    reason, members = policy["policy_reason"], policy["policy_members"]
    if not isinstance(reason, str) or not reason.strip() \
            or not isinstance(members, list) or not members:
        return unknown(TIER_DAMAGED, "recorded approval policy lacks reason or members")
    heads = ("seat", "family", "model") if models else ("seat", "family")
    chosen = {head: set() for head in heads}
    for selector in members:
        if not isinstance(selector, str):
            return unknown(TIER_DAMAGED, "recorded approval policy has malformed selector")
        head, sep, token = selector.strip().partition(":")
        if sep != ":" or head not in heads or not _TOKEN.fullmatch(token):
            return unknown(TIER_DAMAGED, "recorded approval policy has malformed selector")
        chosen[head].add(token if head == "family" else token.casefold())
    exact, selected = chosen["seat"], chosen["family"]
    if any(ord(c) < 32 or ord(c) == 127 for c in reason):
        return unknown(TIER_DAMAGED, "recorded approval policy contains control characters")
    if models and model and _model_keys(model) & chosen["model"]:
        return "ok", None, RULE_NON_AUTHOR
    if canonical in exact:
        return "ok", None, RULE_FAMILY
    if selected:
        if not families:
            return unknown(TIER_DARK, "no immutable verdict-time runtime family evidence")
        if not isinstance(families, (set, frozenset)) or len(families) != 1 \
                or any(not isinstance(f, str) or not _TOKEN.fullmatch(f) for f in families):
            return unknown(TIER_DAMAGED, "conflicting or malformed recorded runtime families")
        if next(iter(families)) in selected:
            return "ok", None, RULE_FAMILY
    return ("outside", "@%s is outside the recorded approval tier; reason: %s; "
            "source prior: %s; valid set: %s" %
            (canonical, reason, policy["id"], ", ".join(sorted(set(members)))),
            RULE_FAMILY)
