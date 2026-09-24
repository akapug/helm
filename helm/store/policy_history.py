"""Retained approval-policy versions, owned by the trusted store boundary.

These append-only events are source artifacts, not a projection or a second
editable policy. Only capture() creates them, from the store's validated policy
population. Retiring, replacing, or deleting a current prior never deletes a
version used by a verdict. There is deliberately no history migration or GC.
"""
import contextlib
import json
import os
import threading

from .. import eventledger, home, pk, projscope


POLICY_FIELDS = ("id", "class", "_policy_confidence_valid",
                 "_policy_source_valid", "policy_kind", "policy_members",
                 "policy_reason")
_LENS = threading.local()


def path():
    return os.path.join(home.global_dir(), "premises", "policy-versions.jsonl")


def _version(record):
    from ..dispatches import _proof_anchor
    return _proof_anchor("approval-policy-history-v1", record)


def index(rows, unavailable=None):
    """Index one checked source read; conflicting or unknown input is fatal."""
    if unavailable:
        return {}, "approval policy history unreadable: %s" % unavailable
    out = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "v", "event", "context", "policy"} \
                or type(row.get("v")) is not int or row["v"] != 1 \
                or row["event"] != "approval-policy-version" \
                or not isinstance(row["context"], dict):
            return {}, "approval policy history contains malformed evidence"
        record = {key: value for key, value in row.items() if key != "id"}
        if row["id"] != _version(record):
            return {}, "approval policy history content identity does not match"
        if row["id"] in out and out[row["id"]] != row:
            return {}, "approval policy history contains conflicting versions"
        out[row["id"]] = row
    return out, None


def snapshot():
    lens = getattr(_LENS, "read", None)
    if lens is not None:
        return lens()
    filename = path()
    return projscope.memo(("approval-policy-history", filename),
                          lambda: index(*eventledger.checked_events(filename, strict=True)))


@contextlib.contextmanager
def read_lens(read):
    before = getattr(_LENS, "read", None)
    _LENS.read = read
    try:
        yield
    finally:
        _LENS.read = before


def _quoted_metadata(value, limit):
    """Cap source locations before escaping terminal controls and Unicode."""
    return json.dumps(value[:limit], ensure_ascii=True) + (
        " (truncated)" if len(value) > limit else "")


def _syntax_diagnostic(exc):
    """Render only parser-owned metadata; arbitrary exception text is unsafe."""
    reason = pk.FrontmatterSyntaxError.REASONS.get(exc.reason, "invalid frontmatter syntax")
    location = _quoted_metadata(exc.path, 240)
    if type(exc.line) is int and 0 < exc.line <= 1_000_000_000:
        location += ":%d" % exc.line
    field = ""
    if exc.reason == "duplicate-field" and isinstance(exc.key, str):
        field = " (field %s)" % _quoted_metadata(exc.key, 64)
    return "approval policy read failed: %s: %s%s" % (location, reason, field)


def capture(repo, context):
    """Retain one validated observation before the verdict is appended.

    No-policy is an explicit observation of an empty trusted-store population,
    not null supplied by the verdict. Failed reads never mint that observation.
    The exact verdict context prevents borrowing another record's observation.
    """
    from .. import registry
    from ..inject._ledger import project_for_cwd
    from .load import InvalidPriorError, _certain_policy_from_hits, _policy_hits
    try:
        projects = registry.load(strict=True)["projects"]
        project = project_for_cwd(repo, projects=projects, strict=True)
        hits = _policy_hits("approval-tier", project=project, strict=True, projects=projects)
        if not isinstance(hits, list):
            return None, "approval policy population is unreadable"
        policy, why = _certain_policy_from_hits("approval-tier", hits)
    except pk.FrontmatterSyntaxError as exc:
        return None, _syntax_diagnostic(exc)
    except InvalidPriorError as exc:
        return None, "approval policy read failed: %s: %s" % (
            _quoted_metadata(exc.path, 240), InvalidPriorError.MESSAGE)
    except Exception as exc:  # noqa: BLE001 — unread policy cannot mint absence
        return None, "approval policy read failed: %s" % type(exc).__name__
    if why and hits:
        return None, why
    record = {"v": 1, "event": "approval-policy-version", "context": context,
              "policy": {key: policy.get(key) for key in POLICY_FIELDS} if policy else None}
    version = _version(record)
    record["id"] = version
    filename = path()
    with eventledger.locked(filename) as held:
        if not held:
            return None, "approval policy history is unwritable"
        known, err = index(*eventledger.checked_events(filename, strict=True))
        if err:
            return None, err
        if version not in known and not eventledger.append_unlocked(filename, record):
            return None, "approval policy version was not retained"
    projscope.forget(("approval-policy-history", filename))
    return {"version": version}, None


def resolve(reference, context):
    if not isinstance(reference, dict) or set(reference) != {"version"} \
            or not isinstance(reference["version"], str) or not reference["version"]:
        return None, "recorded policy reference is malformed"
    known, err = snapshot()
    if err:
        return None, err
    record = known.get(reference["version"])
    if record is None:
        return None, "retained approval policy version is missing"
    if record["context"] != context:
        return None, "retained approval policy version belongs to another verdict"
    return record, None
