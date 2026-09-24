#!/usr/bin/env python3
"""WHO BEGAN THIS TURN, AND WHICH OF ITS BYTES SAY ANYTHING.

A prompt is not always typed. Measured over 1,923 inject turns joined to their
prompts (41h): 54% were chat wakes delivered as a Monitor event,
26% were a background task's completion notice, and 17% were typed. Both
machine kinds arrive in ONE harness envelope, `<task-notification>`, and most
of that envelope is FIXED TEXT the harness writes on every delivery: the ids,
the output path, the status, a summary naming the Monitor or the command, a
note about how agents re-notify, token usage, and the sentence about
PushNotification. 80% of JIT fires matched text of that machine kind, so a
store entry keyed on "monitor", "output" or "wake" fired on every wake the
fleet received, whatever the wake said.

THIS MODULE ANSWERS TWO QUESTIONS AND DECIDES NOTHING:

  * `is_notice(prompt)` — was this turn begun by the harness rather than
    typed? The stall counter asks it (record.turn_open): a turn a wake began
    is not a turn a seat chose to spend.
  * `substance(prompt)` — the prompt with that fixed text set aside: the
    result body of a finished agent and the text of the event (a chat row, a
    watcher's line) are KEPT, because they are what the wake is about; the
    envelope is dropped. The JIT keyword match and the prompt reflex regexes
    read this (inject), never the raw envelope.

A TYPED PROMPT COMES BACK BYTE-IDENTICAL. Only a prompt that BEGINS with the
envelope is a notice; a typed prompt that quotes one mid-text is still typed,
so nothing a person wrote is ever rewritten here. That identity is the control
the whole change is measured against.

THE BODIES SUBSTANCE KEEPS CARRY FIXED TEXT TOO (task/2978). A finished
agent whose report travelled another way has a <result> of one harness
sentence ("This agent's report was delivered to you as a message from ...",
or "This agent has not reported yet: ..."), and a Monitor that printed too
fast gets a harness line of its own ("[N events suppressed — ..."). MEASURED
on 1,284 joined turns (E2 audit): 90, 27 and 14 turns carried only these, and
13.2% of JIT bytes landed on turns with no substance at all. Each such line
is dropped where it starts a line; a line that QUOTES one mid-sentence is
someone's words and stays.

A SUBAGENT'S HAND-BACK IS A SECOND ENVELOPE. It arrives as a leading
`<agent-message from="...">` (50 of the 1,284 turns): the harness's frame
paragraph ("[Subagent hand-back] The text below is the final report of a
subagent ... The report follows:") and then the report, indented two spaces.
The frame is the same bytes on every hand-back and is dropped; the report is
the substance, dedented. A plain agent message has no frame and keeps its
body. Only a LEADING envelope is parsed, and text after its close is kept as
it came, the same law as the notice envelope. is_notice stays the
task-notification question alone: the stall counter that asks it was
measured on that envelope, and a hand-back turn is left to that lane.

A NOTICE CAN MEAN SOMETHING FIXED. A Monitor expiry is the fleet's check-in
tick, and its substance is empty by the rule above. notice_kinds says which
fixed kinds a leading envelope carries (today one: MONITOR_EXPIRED), so the
rule about that kind can reach the seat by the kind itself (inject routes a
kind to the store entries that declare it), never by whichever entry happens
to carry the word "expired".

Pure string handling, stdlib `re` only: this runs on every turn inside the
UserPromptSubmit hook, and a parse that could raise would cost the seat its
whole injection, so every reader below degrades to the input unchanged.
"""
import re
import textwrap

NOTICE_OPEN = "<task-notification>"
NOTICE_CLOSE = "</task-notification>"
HANDBACK_OPEN = "<agent-message"

#: The one fixed notice kind a route can name today (see notice_kinds).
MONITOR_EXPIRED = "monitor-expired"

# THE TWO BODIES THAT CARRY A WAKE'S CONTENT. Everything else inside the
# envelope is written by the harness on every delivery.
_KEEP = re.compile(r"<(result|event)>(.*?)(?:</\1>|\Z)", re.S)

# helm's own chat wake line, rendered by seats_delivery.deliver_any:
#   [helm chat #room → seat @ts] author: text (+N waiting — helm chat read ...)
# The bracketed header and the waiting tail are fixed; author and text stay.
_WAKE_HEAD = re.compile(r"^\[helm chat(?: reaction)?(?: dm| #\S+)? → [^\]]*\]\s*")
_WAKE_TAIL = re.compile(r"\s*\(\+\d+ waiting — helm chat read[^)]*\)\s*$")

# Whole event lines that are fixed notices, never content: the beacon's own
# `[helm chat] ...` lines (more pending, orphaned, degraded), the harness's
# Monitor expiry line, and its rate-limit line.
_FIXED_LINE = re.compile(
    r"^(?:\[helm chat\] |\[Monitor expired after |\[\d+ events? suppressed — )")
_EXPIRED_LINE = re.compile(r"^\[Monitor expired after ", re.M)

# Whole RESULT lines the harness writes when a finished agent's report took
# another road (task/2978), anchored at the line start.
_FIXED_RESULT = re.compile(
    r"^(?:This agent's report was delivered to you as a message from "
    r"|This agent has not reported yet: )", re.M)

# The leading hand-back envelope and its frame paragraph (task/2978).
_HANDBACK = re.compile(
    r"\A\s*<agent-message\b[^>]*>(.*?)(?:</agent-message>|\Z)", re.S)
_HANDBACK_FRAME = re.compile(
    r"\A\s*\[Subagent hand-back\] .*?The report follows:[^\n]*\n?", re.S)


def is_notice(prompt):
    """True when the harness, not a person, began this turn.

    Decided on the first bytes only: the envelope opens the prompt or the
    prompt is typed. Never raises."""
    return isinstance(prompt, str) and prompt.lstrip().startswith(NOTICE_OPEN)


def _is_handback(prompt):
    """Does the prompt BEGIN with a subagent hand-back envelope?"""
    return isinstance(prompt, str) and prompt.lstrip().startswith(HANDBACK_OPEN)


def _handback(prompt):
    """A leading hand-back (up to its first close tag) reduced to its report,
    then whatever followed it, verbatim as substance keeps a notice's tail."""
    m = _HANDBACK.match(prompt)
    body = m.group(1)
    frame = _HANDBACK_FRAME.match(body)
    if frame:
        body = textwrap.dedent(body[frame.end():])
    rest = prompt[m.end():].lstrip("\r\n")
    return "\n".join(p for p in (body.strip(), rest) if p.strip())


def _result_text(body):
    """A result body with the harness's fixed result lines dropped; any other
    body comes back as it came."""
    if not _FIXED_RESULT.search(body):
        return body
    return "\n".join(line for line in body.split("\n")
                     if not _FIXED_RESULT.match(line))


def notice_kinds(prompt):
    """The fixed notice kinds the LEADING envelope(s) carry, as a frozenset:
    MONITOR_EXPIRED when an event holds the harness's expiry line. A typed
    prompt, a hand-back, or any parse trouble has none. Never raises."""
    if not is_notice(prompt):
        return frozenset()
    try:
        kinds = set()
        for body in _envelopes(prompt)[0]:
            for k in _KEEP.finditer(body):
                if k.group(1) == "event" and _EXPIRED_LINE.search(k.group(2)):
                    kinds.add(MONITOR_EXPIRED)
        return frozenset(kinds)
    except Exception:                          # noqa: BLE001 — fail open
        return frozenset()


def _event_text(body):
    """An event body with its fixed lines dropped and each chat wake line cut
    to author and text."""
    out = []
    for line in body.splitlines():
        if _FIXED_LINE.match(line):
            continue
        line = _WAKE_TAIL.sub("", _WAKE_HEAD.sub("", line))
        if line.strip():
            out.append(line)
    return "\n".join(out)


def _kept(body):
    """One envelope's body reduced to its result and event text, each with
    the harness's fixed lines set aside (task/2978)."""
    parts = []
    for m in _KEEP.finditer(body):
        text = _result_text(m.group(2)) if m.group(1) == "result" \
            else _event_text(m.group(2))
        if text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _envelopes(prompt):
    """(bodies, rest): the bodies of the envelopes the prompt OPENS with, read
    one at a time from the front, each up to its first close tag (or the end
    of the text), and the remainder after the last of them, untouched. The
    one walk substance and notice_kinds both read."""
    bodies, rest = [], prompt
    while rest.lstrip().startswith(NOTICE_OPEN):
        rest = rest.lstrip()[len(NOTICE_OPEN):]
        end = rest.find(NOTICE_CLOSE)
        body, rest = (rest, "") if end < 0 else \
            (rest[:end], rest[end + len(NOTICE_CLOSE):])
        bodies.append(body)
    return bodies, rest


def substance(prompt):
    """The prompt with its LEADING notice envelopes reduced to their kept
    bodies, and whatever follows them kept verbatim.

    ONLY THE ENVELOPES THE PROMPT OPENS WITH ARE THE HARNESS'S. They are read
    one at a time from the front, each up to its first close tag (or the end
    of the text: a truncated notice is still a notice); the first byte that
    is not another envelope ends the walk, and everything from there on is a
    message queued behind the notice, returned exactly as written, even when
    it quotes the tag or a whole envelope. A leading subagent hand-back is
    reduced to its report the same way (task/2978). A typed prompt, a
    non-string, or any parse trouble returns the input unchanged."""
    if _is_handback(prompt):
        try:
            return _handback(prompt)
        except Exception:                      # noqa: BLE001 — fail open
            return prompt
    if not is_notice(prompt):
        return prompt
    try:
        bodies, rest = _envelopes(prompt)
        parts = [kept for kept in (_kept(b) for b in bodies) if kept]
        rest = rest.lstrip("\r\n")
        if rest.strip():
            parts.append(rest)
        return "\n".join(parts)
    except Exception:                          # noqa: BLE001 — fail open
        return prompt
