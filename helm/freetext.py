"""THE RULE FOR ARGV THAT BECOMES A RECORD, spelled once for every verb.

THE DEFECT, and it is the same one wherever a verb joins a trailing tail.
`argv` hands the verb a list of strings. If one of them starts with `--` and
the verb does not implement it, joining the tail makes that token
INDISTINGUISHABLE from the text -- and the verb then prints its ordinary
success line, so the author reads "noted", "sent", "closed" and walks away.
Measured on two surfaces: 24 comment bodies on 15 rows lost behind three
different flags over two months, and three DMs whose entire body was the single
word `--body-stdin`, each reported as delivered while a recipient acted on the
absence.

ONE SCANNER AND ONE TEACHER, BECAUSE THE COPY IS THE BUG. Each surface that
discovered this hole cured it locally, so the rule exists several times with
several refusal texts, and the verb written next inherits the confidence of the
cured sibling without the protection. The scan and the sentence that teaches
the escape live here; a caller supplies only its own vocabulary.

SCOPE IS THE LEADING OPTION POSITIONS, NEVER THE BODY. Scanning a whole body
made ordinary prose about flags unsendable ("please use --force carefully" was
refused). The walk stops at the first token that is not flag-shaped, and a bare
`--` ends options POSIX-style so a record that genuinely BEGINS with a
flag-shaped token can still be written deliberately. Flag-shape is
`-{1,2}<letter>`, so "-" bullets, "->" arrows and em-dashes are body.

BOTH HALVES SHIP TOGETHER OR NEITHER DOES. A refusal without an escape turns a
corrupted record into a command with no way through, which is why every refusal
here names `--` in the same breath.
"""

import re
import sys

# -{1,2} followed by a LETTER. A leading "-" bullet, an "->" arrow and an
# em-dash are text that happens to start with a dash, and refusing those makes
# ordinary prose unwritable.
FLAG = re.compile(r"^-{1,2}[A-Za-z]")

# What a token at a leading option position turned out to be.
FLAG_SHAPED = "flag"     # a flag the verb did not consume
ESCAPE = "escape"        # a bare `--`: the caller means the rest literally
BODY = "body"            # the record starts here
EXHAUSTED = "exhausted"  # nothing left to look at


def scan(words, start=0):
    """(kind, index) for the first token at a LEADING option position.

    PURE, so the two call conventions can share it. A verb that keeps parsing
    in place wants the index to delete an escape at; a verb that joins a tail
    wants to know where the record begins. Both ask the same question and it is
    answered here once."""
    k = start
    while k < len(words):
        token = words[k]
        if token == "--":
            return ESCAPE, k
        if not FLAG.match(token):
            return BODY, k
        return FLAG_SHAPED, k
    return EXHAUSTED, k


# Flags that do not exist anywhere but that callers reach for, with the sentence
# that answers the question they were actually asking. Keyed by the flag rather
# than by the verb: an operator who tries `--body` on one verb tries it on the
# next, and the answer does not change with the surface.
_SHARED_HINTS = {
    "--body-stdin": "there is no body flag: give the text as arguments, or "
                    "give NO text and pipe it on stdin (a quoted heredoc "
                    "delimiter keeps backticks and $ literal).",
    "--stdin": "there is no body flag: give the text as arguments, or give NO "
               "text and pipe it on stdin (a quoted heredoc delimiter keeps "
               "backticks and $ literal).",
    "--body": "there is no body flag: give the text as arguments.",
    "--message": "there is no body flag: give the text as arguments.",
    "--msg": "there is no body flag: give the text as arguments.",
    "--text": "there is no text flag: give the text as arguments.",
    "--reason": "there is no reason flag: give the reason as arguments.",
    "--note": "this verb takes the note as its trailing arguments, not as a "
              "flag.",
}


def refuse(command, label, flag, what, known=(), usage=None, hints=None,
           out=None):
    """Print why `flag` is not the record, teach the escape, and return 2.

    `what` names the tail in the VERB'S OWN vocabulary -- "comment text", "a
    close reason", "the message" -- because a refusal that cannot say what it
    wanted is a refusal the reader has to guess at."""
    out = sys.stderr if out is None else out
    print("%s %s: %r is not %s — this verb does not implement it, so it would "
          "have become the record itself and the text you meant would be "
          "corrupted or dropped." % (command, label, flag, what), file=out)
    if known:
        print("  %s accepts: %s" % (label, ", ".join(known)), file=out)
    hint = (hints or {}).get(flag) or _SHARED_HINTS.get(flag)
    # A HINT MAY BE SEVERAL LINES. The chat `--help` refusal is the case that
    # proves it: its usage line and its stdin/heredoc line ARE the cure text a
    # confused caller learns from, and a single-line hint silently dropped both
    # when this rule was first shared.
    for line in ((hint,) if isinstance(hint, str) else (hint or ())):
        print("  " + line, file=out)
    if usage:
        print("  usage: %s %s %s" % (command, label, usage), file=out)
    print("  to write a record that STARTS with a flag-shaped token, put `--` "
          "before it.", file=out)
    return 2


def tail(command, label, words, what, known=(), usage=None, hints=None,
         out=None):
    """(text, None) or (None, rc) -- the joined free-text tail, or a refusal.

    THE ARITY IS THE CALLER'S. An empty tail returns `(None, None)`: whether a
    verb may be called with no text at all is a question about that verb, and
    answering it here would make every caller's own emptiness check dead code
    that nobody notices has stopped running."""
    if not words:
        return None, None
    kind, at = scan(words)
    if kind == ESCAPE:
        rest = words[at + 1:]
        if not rest:
            print("%s %s needs %s after `--`" % (command, label, what),
                  file=sys.stderr if out is None else out)
            return None, 2
        return " ".join(rest), None
    if kind == FLAG_SHAPED:
        return None, refuse(command, label, words[at], what, known, usage,
                            hints, out)
    return " ".join(words), None


def refuse_leading_flags(args, start, known, label, usage, command,
                         what="the message", hints=None, out=None):
    """rc 2 if a flag-shaped LEADING token survived flag parsing, else None.

    FOR VERBS THAT KEEP PARSING IN PLACE. `args` is mutated: a bare `--` is
    consumed so the caller's own join sees only the record. `start` is where
    the options begin -- 1 for `post <text...>`, but 1 past the RECIPIENT for
    `dm <seat> <text...>`, which is the positional that made `dm` stop policing
    after one token in the first place."""
    kind, at = scan(args, start)
    if kind == ESCAPE:
        del args[at]
        return None
    if kind == FLAG_SHAPED:
        return refuse(command, label, args[at], what, known, usage, hints, out)
    return None
