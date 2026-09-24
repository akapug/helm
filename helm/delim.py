#!/usr/bin/env python3
"""The pipe-delimited capture grammar — ONE parser, four callers.

`helm store add`, `helm premise`, `helm reflex add` and `helm mentor` all take
their body as a single argument split on `|`. Every one of them did it with
the same line and therefore the same hole:

    parts = [p.strip() for p in " ".join(args).split("|")]
    if len(parts) < 2: refuse

That guard only catches TOO FEW fields. A pipe inside a statement produces
MORE fields, so it never fired — and the surplus did not append, it CASCADED:
the statement truncated at the first pipe, its tail promoted to keywords,
keywords promoted to domain, and domain dropped off the end. No message, exit
0. `helm premise` then digested the truncated head into the attestation chain
at confidence 1.00, so the SIGNED payload was the fragment. Measured at HEAD
on a live run; the same shape ate an integrator a2a message the same night
through a different delimiter, which is what surfaced the class.

WHAT THIS FIXES:
  - `\\|` is a literal pipe, so a field containing one is EXPRESSIBLE. That
    is the root defect under every symptom — the old grammar had no way to
    say what it meant, so authors could only lose the text.
  - more fields than the grammar accepts is REFUSED, never dropped.
  - the refusal PRINTS THE PARSE, because the operator's next question is
    always "then what did you think I said".

WHAT IT DOES NOT FIX, and cannot: a stray pipe that lands WITHIN the arity in
a type-valid slot is syntactically identical to a deliberate full-arity call.
`premise 'id | stmt with | a pipe'` and an intentional keywords="a pipe" are
the same string, and no validator distinguishes them. Only the escape helps
there, and only for an author who knows to reach for it. This guard is
partial BY CONSTRUCTION — written here so nobody downstream cites it as
total, which is the failure mode that put this module in the repo.
"""

_MAX_ECHO = 72


def fields(text):
    """Split on UNESCAPED pipes. Total — never raises, every string maps.

    `\\|` is the ONE escape and yields a literal pipe. Every other backslash
    is DATA and survives untouched: captured prose carries regexes, Windows
    paths and TeX, and making an author double every backslash to write a
    sentence would trade a rare bug for a constant tax. The cost is that a
    literal backslash-then-pipe cannot be written; that is a real limit of a
    narrow escape and it is documented rather than hidden.
    """
    out, cur, esc = [], [], False
    for ch in text:
        if esc:
            cur.append(ch if ch == "|" else "\\" + ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if esc:                     # a trailing lone backslash is data as well
        cur.append("\\")
    out.append("".join(cur))
    return out


def echo(f):
    """One field, clipped, for showing an operator what the parse produced.

    Public because every caller that refuses a body owes the same answer to
    the same next question — and an empty field must READ as empty rather
    than vanish into whitespace, which is how the cascade stayed invisible.
    """
    f = f.strip()
    if not f:
        return "(empty)"
    return f if len(f) <= _MAX_ECHO else f[:_MAX_ECHO - 1] + "…"


def refusal(raw, arity, grammar):
    """The message for an over-arity body. Names the count, shows the parse."""
    over = len(raw) - arity
    lines = ["%d fields, but this grammar takes at most %d — refusing instead "
             "of dropping the %d that %s not fit."
             % (len(raw), arity, over, "does" if over == 1 else "do"),
             "  grammar: " + grammar,
             "  what the pipes actually split:"]
    lines += ["    [%d] %s" % (i, echo(f)) for i, f in enumerate(raw)]
    lines.append("  a `|` that belongs INSIDE a field is escaped `\\|` — quote "
                 "the whole body so the shell hands helm the backslash.")
    return "\n".join(lines)


def split(text, arity, grammar):
    """(parts, refusal) — parts stripped, never longer than arity.

    A SHORT body is not this function's business: callers own their own
    required-field checks, which mean something different (a field the author
    never supplied, rather than one the delimiter ate).
    """
    raw = fields(text)
    if len(raw) > arity:
        return None, refusal(raw, arity, grammar)
    return [f.strip() for f in raw], None
