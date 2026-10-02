"""Offline Devin proxy contract proposal (task/4072), not a runtime route.

No import from helm, bridge, Connect client, or credential source is needed. A
caller-supplied catalog and native-call ledger are assertions to validate, not
proof of vendor provenance, entitlement, actual serving, or tool-wire support.
"""
import json
import math
import re

# Exact selectors from a sanitized vendor catalog. These are proposed selectors,
# not verified routes, windows, prices, quotas or additional paid accounts.
# The catalog export is external non-source data and MUST NOT enter this repo.
ROUTE_IDENTITY = {"family": "devin", "provider": "devin-bridge",
                  "vendor": "windsurf", "economic_account": "windsurf"}
TRIAL_MODELS = {
    "devin-ds4pro": ("deepseek-v4-pro", "deepseek-v4-pro-high"),
    "devin-glm53": ("glm-5-3", "glm-5-3-high"),
    "devin-glm53flash": ("glm-5-3-flash", "glm-5-3-flash-high"),
    "devin-gemini38flash": ("gemini-3-8-flash", "gemini-3-8-flash-medium"),
    "devin-kimi": ("kimi-k3", "kimi-k3-high"),
    "devin-swe": ("swe-2", "swe-2-high"),
    "devin-ds41flash": ("deepseek-v4-1-flash", "deepseek-v4-1-flash-high"),
}
ADAPTIVE_TRIAL = "devin-adaptive"
# The reviewer aliases deliberately have NO hard-coded vendor UID. Their catalog
# UID and exact family UID must be independently supplied and vetted by a caller.
REVIEWER_FAMILIES = {"devin-sonnet": "sonnet", "devin-opus": "opus",
                     "devin-fable": "fable"}
REVIEWER_PURPOSE = "cross-family-reviewer"
_ALLOWED_FAMILIES = frozenset(family for family, _uid in TRIAL_MODELS.values())
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9.-]{0,127}$")
_TEXT_TOOL_CALL = re.compile(
    r'tool_calls_section_begin|<\s*(?:tool_call|function_call)\b|["\']tool_calls["\']\s*:',
    re.I)
_OPENAI_LINEAGE = re.compile(r"openai|gpt|o-series|\bo(?:1|3|4)(?:[-.]|\b)", re.I)
_MAX_MESSAGES = 128
_MAX_TOOLS = 128
_MAX_ARGUMENT_BYTES = 1 << 20


class ContractError(ValueError):
    """A static request/response is not admissible; never echo its contents."""


def _reviewer_selector(alias, purpose, vendor_families, model_uid, family_uid):
    """Validate an asserted catalog UID, not authorize a live reviewer route."""
    if purpose != REVIEWER_PURPOSE or not isinstance(vendor_families, list) \
            or not isinstance(model_uid, str) or not model_uid \
            or not isinstance(family_uid, str) or not family_uid:
        raise ContractError("reviewer selector needs purpose and exact catalog identity")
    rows = [row for row in vendor_families if isinstance(row, dict)
            and isinstance(row.get("variants"), list)
            for variant in row["variants"] if isinstance(variant, dict)
            and variant.get("model_uid") == model_uid]
    if len(rows) != 1 or rows[0].get("family_uid") != family_uid \
            or not isinstance(rows[0].get("family_label"), str):
        raise ContractError("reviewer catalog identity is missing or ambiguous")
    lineage = (family_uid + " " + rows[0]["family_label"]).casefold()
    family = REVIEWER_FAMILIES[alias]
    matched = {name for name in REVIEWER_FAMILIES.values()
               if re.search(r"\b" + name + r"\b", lineage)}
    if matched != {family} or _OPENAI_LINEAGE.search(lineage) \
            or (family in ("sonnet", "opus") and not re.search(
                r"\b(?:claude|anthropic)\b", lineage)):
        raise ContractError("reviewer catalog family lineage is not unique")
    return model_uid


def selector(alias, synthetic_trial=False, *, purpose=None, vendor_families=None,
             model_uid=None, family_uid=None):
    """Exact proposed selector; reviewer selection is purpose/candidate gated.

    The reviewer inputs MUST be independently vetted catalog facts, not values
    copied from an untrusted API request. A catalog match is not proof of account
    entitlement, actual served UID, reviewer independence, or launch approval.
    """
    if alias == ADAPTIVE_TRIAL and synthetic_trial is True and purpose is None:
        return "adaptive"
    if purpose in (None, "build") and isinstance(alias, str) \
            and alias in TRIAL_MODELS:
        return TRIAL_MODELS[alias][1]
    if isinstance(alias, str) and alias in REVIEWER_FAMILIES:
        return _reviewer_selector(alias, purpose, vendor_families, model_uid,
                                  family_uid)
    raise ContractError("unknown or unadmitted Devin model")


def served_model(alias, actual_uid, vendor_families, source):
    """Classify a candidate upstream actual UID, never the requested alias.

    Source is an assertion a bridge would have to independently verify. This
    pure function cannot verify provenance, serving or dispatch approval.
    Reviewer aliases stay excluded here: the reviewer selection proposal does
    not establish a live actual-UID/attestation pipeline or reviewer admission.
    """
    result = {"status": "unknown", "family": None, "actual_model_uid": None}
    if source != "upstream-connect" or not isinstance(actual_uid, str) \
            or not actual_uid or not isinstance(vendor_families, list):
        return result
    families = [row for row in vendor_families if isinstance(row, dict)
                and isinstance(row.get("variants"), list)
                and any(isinstance(v, dict) and v.get("model_uid") == actual_uid
                        for v in row["variants"])]
    if len(families) != 1 or not isinstance(families[0].get("family_uid"), str):
        return result
    family = families[0]["family_uid"]
    label = families[0].get("family_label")
    if not isinstance(label, str):
        return result
    result.update(family=family, actual_model_uid=actual_uid)
    lineage = (family + " " + label).casefold()
    if any(token in lineage for token in ("claude", "anthropic", "openai",
                                           "gpt", "o-series")) or re.search(
                                               r"\bo(?:1|3|4)(?:[-.]|\b)",
                                               lineage):
        result["status"] = "excluded"
    elif alias == ADAPTIVE_TRIAL:
        # A concrete safe UID cannot authorize a router that may choose Claude
        # or GPT on its next request.
        result["status"] = "trial" if family in _ALLOWED_FAMILIES else "unknown"
    elif isinstance(alias, str) and TRIAL_MODELS.get(alias) == (family, actual_uid):
        result["status"] = "catalog-match"
    return result


def _json_object(raw):
    """An actual JSON object with finite numbers, not Python's NaN extension."""
    def reject_constant(_value):
        raise ValueError("nonfinite JSON constant")

    def finite(value):
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
                return True
            except UnicodeEncodeError:
                return False
        if isinstance(value, dict):
            return all(finite(k) and finite(v) for k, v in value.items())
        if isinstance(value, list):
            return all(finite(v) for v in value)
        return True

    try:
        value = json.loads(raw, parse_constant=reject_constant)
        return isinstance(value, dict) and finite(value)
    except (ValueError, TypeError, RecursionError):
        return False


def _texts(content):
    """All text in message content, including each part of a content-part list.

    Reject unreadable or non-UTF-8 parts rather than let a tool-call imitation
    hide in one. The caller scans the contiguous rendering, not separate parts.
    """
    if content is None:
        return []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list) and all(
            isinstance(part, dict) and isinstance(part.get("text"), str)
            for part in content):
        texts = [part["text"] for part in content]
    else:
        raise ContractError("unsupported message content")
    try:
        for text in texts:
            text.encode("utf-8")
    except UnicodeEncodeError:
        raise ContractError("message content is not UTF-8 encodable") from None
    return texts


def validate_turn(request, issued_calls=None, *, purpose=None,
                  vendor_families=None, model_uid=None, family_uid=None):
    """Validate an OpenAI tool round trip BEFORE any proposed bridge egress.

    issued_calls maps upstream-native ID to the immutable (name, JSON args)
    descriptor decoded by a bridge, NOT a value copied from client history.
    Absent ledger fails closed. A trusted caller, never request fields, provides
    reviewer purpose and independently vetted catalog identity out of band.
    This pure function cannot enforce provenance or ledger custody.
    """
    if not isinstance(request, dict):
        raise ContractError("request is not an object")
    model = selector(request.get("model"), purpose=purpose,
                     vendor_families=vendor_families, model_uid=model_uid,
                     family_uid=family_uid)
    messages, tools = request.get("messages"), request.get("tools", [])
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ContractError("messages are missing or over limit")
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise ContractError("tool definitions are malformed or over limit")
    declared = set()
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ContractError("unsupported tool definition")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) \
                or not _TOOL_NAME.fullmatch(fn["name"]) \
                or not isinstance(fn.get("parameters"), dict) \
                or fn["name"] in declared:
            raise ContractError("unsupported tool definition")
        declared.add(fn["name"])
    if issued_calls is None:
        issued_calls = {}
    if not isinstance(issued_calls, dict) or len(issued_calls) > _MAX_TOOLS * _MAX_MESSAGES \
            or not all(isinstance(cid, str) and cid and isinstance(desc, tuple)
                       and len(desc) == 2 and all(isinstance(v, str) for v in desc)
                       for cid, desc in issued_calls.items()):
        raise ContractError("native tool-call ledger is invalid")
    pending, ids = set(), set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in (
                "system", "user", "assistant", "tool"):
            raise ContractError("unsupported message")
        role, calls = msg["role"], msg.get("tool_calls")
        if pending and role != "tool":
            raise ContractError("tool result missing before next message")
        # Content-part text renders contiguously; splitting a marker across
        # adjacent parts must not make it disappear from the refusal gate.
        if role == "assistant" and _TEXT_TOOL_CALL.search(
                "".join(_texts(msg.get("content")))):
            raise ContractError("text-emulated tool call is forbidden")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) \
                    or not calls or len(calls) > _MAX_TOOLS or pending:
                raise ContractError("tool calls are malformed or out of order")
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                cid = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call, dict) or call.get("type") != "function" \
                        or not isinstance(cid, str) or not cid or cid in ids \
                        or cid not in issued_calls \
                        or not isinstance(fn, dict) \
                        or not isinstance(fn.get("name"), str) \
                        or not _TOOL_NAME.fullmatch(fn["name"]) \
                        or fn["name"] not in declared \
                        or not isinstance(fn.get("arguments"), str) \
                        or issued_calls[cid] != (fn["name"], fn["arguments"]):
                    raise ContractError("tool call ID/name/arguments invalid")
                try:
                    argument_bytes = fn["arguments"].encode("utf-8")
                except UnicodeEncodeError:
                    raise ContractError("tool call arguments are not UTF-8 encodable") from None
                if len(argument_bytes) > _MAX_ARGUMENT_BYTES:
                    raise ContractError("tool call arguments exceed limit")
                if not _json_object(fn["arguments"]):
                    raise ContractError("tool call arguments must be a finite JSON object")
                ids.add(cid)
                pending.add(cid)
        if role == "tool":
            cid = msg.get("tool_call_id")
            if not isinstance(cid, str) or cid not in pending:
                raise ContractError("tool result has no matching call ID")
            pending.remove(cid)
    if pending:
        raise ContractError("incomplete tool round trip")
    return model
