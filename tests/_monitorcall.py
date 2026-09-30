"""Read a rendered Monitor call back into the tool input it spells (task/3435).

helm prints an arming instruction as `Monitor(key: <json>, ...)`: the tool's
name, then each field as its key and ONE JSON value. A seat copies it field
for field, so the test that matters is the one the seat performs: every value
reads back through json as exactly one value, with nothing left between the
values but the separator. This reader does that and nothing more, and raises
on anything else, so a call it returns is a call a seat can copy whole.
"""
import json
import re

_HEAD = "Monitor("
_KEY = re.compile(r"([a-z_]+): ")


def monitor_input(call):
    """{field: value} for the one Monitor call `call` is, or ValueError."""
    if not (call.startswith(_HEAD) and call.endswith(")")):
        raise ValueError("not one Monitor call: %r" % call)
    decode, at, end, out = json.JSONDecoder().raw_decode, len(_HEAD), \
        len(call) - 1, {}
    while True:
        key = _KEY.match(call, at)
        if key is None or key.group(1) in out:
            raise ValueError("no fresh field at %d of %r" % (at, call))
        out[key.group(1)], at = decode(call, key.end())
        if at == end:
            return out
        if not call.startswith(", ", at):
            raise ValueError("no separator at %d of %r" % (at, call))
        at += 2


def last_call(text):
    """The Monitor call a message ends with: its last indented line."""
    return text.rsplit("\n  ", 1)[-1]
