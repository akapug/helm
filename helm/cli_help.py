"""The verb help text, and the one-line root listing cut from it.

`helm <verb> --help` (and `helm help <verb>`) prints that verb's entry in
`_VERB_HELP` whole. `helm --help` prints ONE line per verb, derived from that
same entry: the verb, its usage head compacted (the subverbs and their
positional <args>, with every bracketed group dropped but an optional subverb
group, and the whole head one optional group when the verb runs bare), then
` — ` and the first sentence of its description, so the listing holds no text
of its own that could drift from the full help. A model that runs `helm
--help` first pays for the verbs and their subverbs, not for every verb's
contract.

Only the help and refusal paths import this module. The table is most of the
CLI's source bytes and an ordinary verb never reads it, so `cli` imports it
where it prints it. `cli._VERB_HELP` is a module attribute resolved on first
read, and it is this same table for every reader that spells it through `cli`.
"""
import os
import re

#: The widest listing line, in columns (the dash is one). A verb's usage head
#: and its first sentence share what the verb's name leaves of it.
LINE_WIDTH = 120

#: The most columns a usage head takes, so every line keeps room for what the
#: verb does after the subverbs it takes.
HEAD_WIDTH = 56

#: The mark on a head or a sentence cut short.
CUT_MARK = "..."

HEADER = ("helm — the steering station for you and your agent fleet\n\n"
          "usage: helm <verb> [args]\n")

CLOSING = "helm <verb> --help, or helm help <verb>, prints that verb's full usage."

HELP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "help")


def _load_verb_help():
    """The verb help table, one file per verb, read back at import.

    `helm/help/<verb>.txt` holds that verb's help string exactly, followed by
    one newline; the key is the file name without `.txt` and the value is the
    file's text with exactly ONE trailing newline removed, read as utf-8. The
    one shared dict literal is gone: a new verb adds a file, so parallel lanes no
    longer conflict on a single table (task/3845's one-file-per-change, applied
    to the help text). Key order does not matter: every reader looks a verb up
    by name."""
    table = {}
    for name in sorted(os.listdir(HELP_DIR)):
        if not name.endswith(".txt"):
            continue
        with open(os.path.join(HELP_DIR, name), encoding="utf-8") as fh:
            text = fh.read()
        if text.endswith("\n"):
            text = text[:-1]
        table[name[:-4]] = text
    return table


_VERB_HELP = _load_verb_help()


_OPEN, _CLOSE = "([{", ")]}"

#: The spans a usage head never splits or cuts inside, by their closing mark:
#: a <placeholder>, whose `|` is one argument's choice (`<ISO-8601|epoch-ms>`),
#: and a "quoted" argument, whose ` | ` is typed (`"<id> | <steer>"`).
_SPANS = {"<": ">", '"': '"'}

#: A subverb: a lowercase word such as `pull` or `vendor-reset`. A flag, a
#: <placeholder>, an UPPER metavariable and a quoted argument are not.
_SUBVERB = re.compile(r"[a-z][a-z0-9-]*\Z")

#: The abbreviations whose full stop ends no sentence (`incl. the ...`).
_ABBREVIATION = re.compile(r"\b(?:incl|e\.g|i\.e|cf|vs|viz|approx|resp|esp)\Z")


def _outside_brackets(text, mark):
    """The index of the first `mark` in `text` that no bracket encloses, or
    -1. A usage group such as `(in helm's own tree ... — ...)` holds dashes
    and full stops that end neither the usage nor the sentence."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(depth - 1, 0)
        elif not depth and text.startswith(mark, i):
            return i
    return -1


def entry(name, fn):
    """The full help text for one verb: its `_VERB_HELP` entry, else the first
    line of its handler's docstring, else its bare name."""
    return _VERB_HELP.get(name) or (fn.__doc__ or name).strip().split("\n")[0]


def _parts(text):
    """One full entry split at its first ` — ` that no bracket encloses (else
    its first at all): (usage, description). An entry with none is all
    description."""
    at = _outside_brackets(text, " — ")
    if at < 0:
        at = text.find(" — ")
    return (text[:at], text[at + len(" — "):]) if at >= 0 else ("", text)


def _end(text, i):
    """The index just past the group or span that opens at `text[i]`: a
    bracket group ends at its matching bracket (unclosed, at the end), a
    <placeholder> or a "quoted" argument at its closing mark (unclosed, it is
    no span and ends right after its one character)."""
    if text[i] in _SPANS:
        return text.find(_SPANS[text[i]], i + 1) + 1 or i + 1
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] in _OPEN) - (text[j] in _CLOSE)
        if not depth:
            return j + 1
    return len(text)


def _alternatives(usage):
    """`usage` split at each `|` between two forms: one outside every bracket
    group, placeholder and quoted argument, and not the typed pipe between two
    placeholders (`add <type> <id> | <stmt>` passes one pipe-delimited
    argument)."""
    alts, start, i = [], 0, 0
    while i < len(usage):
        if usage[i] in _OPEN or usage[i] in _SPANS:
            i = _end(usage, i)
            continue
        if usage[i] == "|" and not (usage.endswith("> ", 0, i)
                                    and usage.startswith(" <", i + 1)):
            alts.append(usage[start:i])
            start = i + 1
        i += 1
    return alts + [usage[start:]]


def _compact(alt):
    """One usage alternative less its bracketed groups (`[--flag X]`, a
    `(...)` note, and the `...` that repeats one), spaces collapsed. An
    OPTIONAL SUBVERB group stays, compacted the same way inside: a `[...]`
    whose every alternative begins with a subverb, standing where a subverb
    would, after nothing but subverbs (`lifecycle [show|record]`)."""
    out, grouped, i = [], False, 0
    while i < len(alt):
        end = _end(alt, i) if alt[i] in _OPEN or alt[i] in _SPANS else i + 1
        if alt[i] in _OPEN:
            kept = (alt[i] == "[" and not grouped
                    and all(map(_SUBVERB.match, "".join(out).split()))
                    and _subverbs(alt[i + 1:end - 1]))
            if kept:
                out.append("[" + kept + "]")
            elif alt.startswith("...", end):
                end += len("...")
            grouped = True
        elif alt[i] == '"' and end > i + 1:
            out.append('"%s"' % _compact(alt[i + 1:end - 1]))
        else:
            out.append(alt[i:end])
        i = end
    return " ".join("".join(out).split())


def _subverbs(group):
    """The inside of a `[...]` group compacted, when every alternative in it
    begins with a subverb (`show|record`), else empty."""
    alts = [_compact(alt) for alt in _alternatives(group)]
    ok = all(alt and _SUBVERB.match(alt.split(" ")[0]) for alt in alts)
    return "|".join(alts) if ok else ""


def _whole_group(alt):
    return alt.startswith("[") and _end(alt, 0) == len(alt)


def _cut(text, width):
    """`text` whole when it fits `width`, else cut with CUT_MARK at the last
    space or `|` that no <placeholder> or "quoted" argument encloses, so a cut
    never splits an argument."""
    if len(text) <= width:
        return text
    room, at, i = width - len(CUT_MARK), 0, 0
    while i <= room:
        if text[i] in _SPANS:
            i = _end(text, i)
            continue
        if text[i] in " |":
            at = i
        i += 1
    return text[:at or room].rstrip(" |,;:(.") + CUT_MARK


def usage_head(name, text, width=HEAD_WIDTH):
    """The compact usage head of the verb `name`, cut from its full entry
    `text`: each form of the usage before ` — ` compacted (less its bracketed
    groups but an optional subverb group, less the verb's own name where it
    spells it), the emptied forms dropped. What stays is the subverbs and
    their positional arguments. When the verb runs BARE (a form empties, or
    is only an optional subverb group), the whole head is optional:
    `ship [pull|hosts]`. A pipe typed inside an argument keeps its spaces
    (`add <type> <id> | <stmt>`). Cut at `width`, the cut closing any group
    it leaves open. A verb that takes no subverb or argument has no head."""
    alts = [_compact(alt) for alt in _alternatives(_parts(text)[0])]
    alts = [alt[len(name):].lstrip() if alt.split(" ")[0] == name else alt
            for alt in alts]
    bare = any(not alt or _whole_group(alt) for alt in alts)
    head = "|".join(alt[1:-1] if _whole_group(alt) else alt
                    for alt in alts if alt)
    head = "[%s]" % head if head and bare else head
    for room in range(width, 0, -1):
        cut = _cut(head, room)
        shut = cut.count("[") - cut.count("]")
        if len(cut) + shut <= width:
            return cut + "]" * shut
    return ""


def summary(text):
    """The first sentence of an entry's description, without its closing
    punctuation. A full stop inside brackets or after an abbreviation
    (`incl.`) ends no sentence."""
    desc, at = _parts(text)[1], 0
    while True:
        stop = _outside_brackets(desc[at:], ". ")
        if stop < 0:
            return desc.rstrip(" .;:,")
        at += stop
        if not _ABBREVIATION.search(desc[:at]):
            return desc[:at].rstrip(" .;:,")
        at += len(". ")


def listing_line(name, text):
    """One verb's root-listing line: the verb, its usage head, then ` — ` and
    its first sentence cut to what LINE_WIDTH leaves."""
    lead = "  " + " ".join(filter(None, (name, usage_head(name, text))))
    return lead + " — " + _cut(summary(text), LINE_WIDTH - len(lead) - 3)


def root_help(verbs):
    """The whole `helm --help` text for the dispatch table `verbs` (name ->
    handler): the header, one line per verb in table order, and the closing
    line that points at the full text."""
    lines = [listing_line(name, entry(name, fn)) for name, fn in verbs.items()]
    return "\n".join([HEADER] + lines + ["", CLOSING])
