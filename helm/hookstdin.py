"""What a hook payload may weigh, and why an unreadable one is not empty.

A tool payload holds whole files: an Edit's `tool_response` carries the edited
file's full original text. A 64 KiB cap on the read cut every Edit of a large
module mid-document, the parse failed, and the payload read as `{}`, with no
session id and no agent id. The delivery hook then ran session-less on the
SEAT-level cursor, which session-keyed consumers never advance, and
re-delivered rows hours old. It did so into subagents as readily as into the
lead, because the subagent fence reads `agent_id`.

So the bound is the payload's, and a read that got bytes it could not parse
says so in `_UNPARSED`: `{}` from an unreadable payload tells nothing about
which thread fired, and a consumer that spends rows must not treat it as the
lead's. The readers themselves stay in helm/seats_cli.py, the one door its
verbs call.
"""
import json

#: The most a hook payload may carry, set for the payload and not a buffer.
HOOK_STDIN_MAX = 64 * 1024 * 1024
#: Why the last hook read was unreadable (empty, cut, or not an object), or ""
#: when it parsed. A list, so the reader and its consumers share one slot.
_UNPARSED = [""]


def _parsed(raw):
    """The payload dict, recording why a read did not parse.

    AN EMPTY PAYLOAD IS UNREAD TOO. The harness always sends a document, so
    empty stdin is a hook invocation that carries no thread and no session,
    and a delivery that took it for the lead would spend the SEAT-level
    cursor exactly as a truncated payload did."""
    if not raw or not bytes(raw).strip():
        _UNPARSED[0] = "the payload was empty"
        return {}
    try:
        d = json.loads(raw)
    except Exception:                        # noqa: BLE001
        _UNPARSED[0] = "%d bytes did not parse" % len(raw)
        return {}
    if not isinstance(d, dict):
        _UNPARSED[0] = "%d bytes are not a JSON object" % len(raw)
        return {}
    return d
