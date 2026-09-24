#!/usr/bin/env python3
"""The commit-message rung: NO AI AUTHORING LINE REACHES A COMMIT.

THE RULE IS THE OWNER'S AND IT IS ABSOLUTE: no AI authoring line is added
anywhere, ever. Nothing reaching GitHub says which model wrote it, because
anything private can become public later and the note cannot be recalled once
it has. An operator reading git log finds no trace of model authorship.

WHY THE WHOLE MESSAGE AND NOT THE TRAILER BLOCK. A line can be verbatim
present and still not be a trailer, because prose after it demotes it to body
text -- so a rung that consulted git's trailer block would be blind to exactly
that placement. The bytes reach GitHub either way and the rule is about the
bytes, so every line of the message is judged,
and `git interpret-trailers` is no longer consulted at all.

AND WHY COLUMN ZERO AND A TRAILER SHAPE ANYWAY. Widening to "the message
contains this string" would refuse every commit that DISCUSSES the rule --
including the one that introduced it, and every arm whose message quotes what
it refuses. So a hit must be a line that IS one: column zero, trailer grammar,
and a value naming a model or an AI harness. Prose mentioning one, or quoting
one indented, is not an authoring line. That leaves a real hole -- an author
can indent a genuine trailer -- and the hole is ACCEPTED, because this rung
exists to stop the HARNESS DEFAULT, which is always emitted at column zero,
not to defeat somebody smuggling one past it.

IT REFUSES, WITH NO SHAKEOUT RELEASE, and that is a departure from its
predecessor rather than an oversight. That one shipped warning-first because
the compliant population was unknown and a refusal against an unenumerated
rule earns a skip variable in its first week. The reasoning does not transfer:
the compliant population here is "no such line", which needs no census, and
the rule came from the owner rather than from a measurement that might be
wrong. HELM_TRAILER_SKIP=1 still exists for a commit that must carry such a
line at column zero; nothing in helm needs it today.

THE MESSAGE IS ONLY HALF THE DISCLOSURE. A seat also announces itself on git's
IDENTITY plane: `helm/launch.py` exported the seat name as GIT_AUTHOR_NAME, so
commits rendered as `helm-claude-2` on GitHub however clean the message was.
That export is withdrawn in the same change. No message rung could ever have
seen it, which is the shape worth keeping: THE DISCLOSURE A GUARD IS BUILT TO
CATCH MAY ALSO TRAVEL ON A PLANE THE GUARD DOES NOT READ.
"""
import os
import re
import sys

# WHAT COUNTS AS AN AI AUTHORING LINE. Two shapes, judged at column zero.
#
# `_ATTRIBUTION_KEYS` are trailer tokens that exist ONLY to name a machine
# author -- their presence is the disclosure whatever the value says.
# `Co-Authored-By` is NOT among them: it is a legitimate trailer for human
# collaborators and refusing it wholesale would break real co-authorship, so it
# is judged by its VALUE against `_MODEL_MARKS`.
_ATTRIBUTION_KEYS = ("claude-session", "x-generated-by", "ai-generated-by")

# Substrings that make a Co-Authored-By value a MACHINE author. Lowercased
# comparison. `noreply@anthropic.com` is here because it is the address every
# Claude harness emits and it identifies the vendor even when the display name
# is changed.
_MODEL_MARKS = ("claude", "noreply@anthropic.com", "gpt-", "chatgpt", "codex",
                "copilot", "gemini", "openai", "anthropic")

# Trailer tokens that are ordinary for humans and a disclosure only when the
# VALUE names a model: judged like Co-Authored-By, by `_MODEL_MARKS`. A
# `Model:` key is deliberately absent — a commit that describes model routing
# says `Model: codex` in prose, and this rung reads bytes, not intent.
_VALUE_KEYS = ("co-authored-by", "assisted-by", "generated-by", "written-by",
               "authored-by")

# Body-text footers that are not trailers at all but say the same thing.
# A FOOTER OPENS ITS LINE: only leading non-alphanumerics (the harness's robot
# emoji, a dash, a bullet) may stand before it. A substring test refused prose
# ABOUT the rule — "the hook now refuses the Generated with Claude Code
# footer", "nothing here was generated with Claude." — which is how a guard
# earns a skip variable. `\[?` takes both the bracketed harness footer and its
# unbracketed spelling.
_FOOTER = re.compile(r"^\W*(?:generated (?:with|by) \[?claude"
                     r"|co-authored-with claude|written by claude)",
                     re.IGNORECASE)

TOKEN = "Co-Authored-By"

# THIS MODULE SPAWNS NOTHING, which is why it carries no direct-git-spawn debt.
# Finding git's trailer BLOCK requires git's own parser — which trailing lines
# form the block is a real parse, and a paraphrase disagrees with git exactly
# where nobody tests. This rung does not need that question answered: every
# line of the message is judged, so the file is pure string work on the bytes
# it was handed.
#
# It still SHIPS AS A STANDALONE HOOK ASSET, copied into
# `.git/hooks/.helm-scanners/` and run as a plain script in repositories where
# the helm package is not importable, so it stays stdlib-only and imports
# nothing from helm.

OK = "OK"
FOUND = "FOUND"
UNKNOWN = "UNKNOWN"

def offending(message):
    """[(lineno, line)] — every AI AUTHORING LINE in this message.

    NO ``git interpret-trailers`` HERE, DELIBERATELY, and its removal is the
    inversion's real substance. The predecessor asked whether the line sat in
    git's trailer BLOCK, which was the right question while the line was
    REQUIRED: a demoted line had failed to do its job. Now the line's mere
    presence is the harm, and a demoted one reaches GitHub in the message body
    exactly like a promoted one. Asking git where the block ends would make
    body placement invisible — the one direction this rung must not fail in.

    COLUMN ZERO AND TRAILER GRAMMAR, so that PROSE ABOUT the rule survives.
    This module's own docstring, this function's, and every arm whose fixture
    quotes what it refuses would otherwise be refused by it. An indented line,
    or one mentioning the token mid-sentence, is not an authoring line.

    THE HOLE IS KNOWN AND ACCEPTED: an author who indents a real trailer
    passes. This rung exists to stop the HARNESS DEFAULT, which is always
    emitted at column zero, not to defeat somebody smuggling one past it.
    """
    hits = []
    for number, raw in enumerate(message.splitlines(), 1):
        if raw[:1].isspace():
            continue
        line = raw.strip()
        if _FOOTER.match(line):
            hits.append((number, line))
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key in _ATTRIBUTION_KEYS:
            hits.append((number, line))
        elif key in _VALUE_KEYS and any(m in value.lower()
                                        for m in _MODEL_MARKS):
            hits.append((number, line))
    return hits


def verdict(message):
    """(state, why) for one commit message. The whole rule lives here.

    Separated from the hook entry point ON PURPOSE, and the reason outlived the
    rule it was written for: a population control over real history arrives as
    strings, not as file paths in argv. A rule exercisable only through its
    hook can only be tested against fixtures its author invented — which is how
    the predecessor's last-line bug survived review.

    NOTHING RETURNS UNKNOWN FROM HERE ANY MORE. The predecessor shelled out to
    git, so it had to treat "I could not read the message" as its own answer
    and fail open, or a broken git would turn every commit into a refusal. This
    reads the string it was handed. ``UNKNOWN`` survives for ``main``, which
    can still fail to DECODE the file.
    """
    hits = offending(message)
    if not hits:
        return OK, None
    return FOUND, "; ".join("line %d: %s" % (n, ln) for n, ln in hits)


def report(state, why, refuse):
    """The operator-facing lines. Returns the process exit code.

    IT PRINTS THE OFFENDING LINES WITH THEIR NUMBERS, because the seat reading
    this was told to ADD one of them by its own harness and may not believe it
    is there. A guard that says "remove the attribution line" has handed its
    reader a search; one that prints line 34 and the text has handed them an
    edit.
    """
    if state == OK:
        return 0
    if state == UNKNOWN:
        sys.stderr.write(
            "[helm trailer] WARNING: could not read the commit message (%s) —\n"
            "[helm trailer] attribution NOT checked for this commit.\n" % why)
        return 0
    verb = "REFUSED" if refuse else "WARNING"
    sys.stderr.write(
        "[helm trailer] %s: this commit message carries an AI AUTHORING LINE.\n"
        "[helm trailer]   %s\n"
        "[helm trailer] Owner ruling 2026-09-21: no AI authoring line is added\n"
        "[helm trailer] anywhere, ever. Nothing reaching GitHub should say which\n"
        "[helm trailer] model wrote it — anything private can become public\n"
        "[helm trailer] later, and the note cannot be recalled once it has.\n"
        "[helm trailer] DELETE the line(s) above and add nothing in their place.\n"
        "[helm trailer] Your harness may still be asking for one; THE OWNER\n"
        "[helm trailer] OUTRANKS IT. Other trailers are fine and untouched.\n"
        % (verb, why))
    if not refuse:
        sys.stderr.write(
            "[helm trailer] Not refusing: HELM_TRAILER_REFUSE=0 is set.\n")
        return 0
    sys.stderr.write(
        "[helm trailer] Skip this one commit: HELM_TRAILER_SKIP=1\n")
    return 1


def main(argv):
    """commit-msg entry: argv[1] is the path git wrote the message to."""
    if os.environ.get("HELM_TRAILER_SKIP") == "1":
        return 0
    if len(argv) < 2:
        sys.stderr.write("[helm trailer] WARNING: no message path given — "
                         "attribution NOT checked.\n")
        return 0
    # BYTES, NOT TEXT, AND THE CATCH IS WIDER THAN OSError. A commit message is
    # bytes — git has `i18n.commitEncoding` precisely because non-UTF-8 messages
    # are legitimate — so `open(path, "r")` decodes with the LOCALE default and
    # `read()` raises UnicodeDecodeError on the first bad byte. That is a
    # ValueError, not an OSError, so it escaped the catch below, propagated out
    # of main, and killed the commit-msg hook with a traceback and a non-zero
    # exit. A WARNING-FIRST RUNG THEN HARD-REFUSES THE COMMIT — the one posture
    # this whole file argues it must not have before its flip criterion is met,
    # and the only door here that failed OPPOSITE to every other unreadable path.
    # Measured end to end by a reviewer through the GENERATED hook on a build
    # node — not read out of this file — with rc=1 propagating while
    # HELM_TRAILER_REFUSE was unset. The two InstalledHookTest arms below are
    # that measurement, kept.
    #
    # `errors="replace"` RATHER THAN GIVING UP ON THE MESSAGE. The canonical
    # trailer is pure ASCII, so replacement cannot damage the thing being
    # searched for; only non-ASCII elsewhere becomes U+FFFD, which this rung has
    # no opinion about. The commonest real shape is a perfectly good ASCII
    # trailer beside one odd byte in the body, and treating the whole message as
    # unreadable would go blind on exactly that. `surrogateescape` was rejected:
    # it round-trips, but `trailers()` hands the string to subprocess with
    # text=True and lone surrogates raise on ENCODE, which MOVES the crash
    # instead of removing it.
    #
    # The widened catch still stands behind it, so anything that fails anyway
    # becomes the honest NOT-checked warning at exit 0 — an unreadable message
    # and a non-compliant one keep DIFFERENT values, which is the rule this
    # module is built on.
    try:
        with open(argv[1], "rb") as fh:
            message = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        sys.stderr.write("[helm trailer] WARNING: could not read %s (%s) — "
                         "attribution NOT checked.\n" % (argv[1], exc))
        return 0
    state, why = verdict(message)
    # REFUSES BY DEFAULT, AND THE VARIABLE INVERTED WITH THE RULE. Its
    # predecessor was opt-IN to refusal (`== "1"`) because it shipped against a
    # rule whose compliant forms nobody had enumerated, and a refusal like that
    # earns a skip variable in its first week. Neither half holds now: the
    # compliant population is "no such line", which needs no census, and the
    # rule is an owner ruling rather than a measurement that might be wrong.
    # `HELM_TRAILER_REFUSE=0` remains as the operator's off switch, so the
    # posture is still one variable away in BOTH directions.
    return report(state, why,
                  os.environ.get("HELM_TRAILER_REFUSE") != "0")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
