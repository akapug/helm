"""The exact command a substitution refusal of the argv guard admits.

The argv guard (`helm.chat.argv_guard`) refuses a backtick or a `$(` that
the shell would run inside a message body: a chat or dispatch body, a task
or store text, a `git commit -m` message, verdict evidence. A route named in
general terms leaves the seat to rebuild its own command by hand, and a
seat that rebuilds it wrong meets the same refusal again.

This module builds the corrected command from the refused one, so the
refusal can print it whole:

  * a backtick or `$(` the guard refuses gets one backslash in front of it,
    which makes it literal in a double-quoted word, in an unquoted heredoc
    body and in a bare word alike. Nothing else changes: a `$NAME` still
    expands, and every other word stays where it was;
  * a double-quoted word that is all one quoted document read back by cat,
    `"$(cat <<'EOF' ... EOF )"`, keeps its document. A verb that reads its
    body on stdin (chat post, reply and dm, dispatch send) takes the
    document as its stdin heredoc; any other verb takes it through a
    variable set first and passed as `"$helm_body"`.

The result is checked against the guard itself, so a corrected command is
only ever printed when the guard admits it, and against the guard's shell
reader, so it is only printed when bash splits it into the same commands
and the same words per command as the original: a backslash in front of a
`$(` whose substitution holds double quotes would otherwise turn those
quotes into word and command breaks. Anything this reader cannot settle
returns None, and the refusal then prints its general route alone.
"""
import bisect
import itertools
import re

#: past this many characters the refusal names the route and prints no copy
CORRECTED_CAP = 12000
#: the prefix of the line a refusal ends with: the command to paste. It is
#: `helm.review_done.CORRECTED`, spelled here because this module loads on
#: the hook's refusal path and that one loads the dispatch ledger with it
#: (tests.test_argv_guard_cure_3945 holds the two equal)
CORRECTED = "corrected: "

# A WHOLE double-quoted word that is one quoted document read back by cat.
# The operator is kept as typed (`<<` or `<<-`), and so is the tag.
#
# THE TERMINATOR IS THE TAG LINE EXACTLY, as bash reads it (task/3945): a
# newline, the tag, a newline, with NO trailing blanks. A tag line with a
# trailing space is not a terminator to bash, and accepting one as the end of
# the document ended the copy early and ran a body line as a command. Leading
# tabs are captured (group 5) and allowed only for `<<-`; `corrected_command`
# rejects a tab-led tag under plain `<<`, which bash does not strip.
_CAT_DOCUMENT = re.compile(
    r"\"\$\([ \t]*cat[ \t]+(<<-?)[ \t]*(['\"])([A-Za-z_][\w.-]*)\2[ \t]*\n"
    r"(.*?)\n(\t*)\3\n[ \t\n]*\)\"", re.DOTALL)
# the start of such a word, matched or not: escaping its `$(` would send the
# document's own spelling, so a word that starts this way and does not match
# the whole shape above gets no copy
_CAT_OPENING = re.compile(r"\"\$\([ \t]*cat[ \t]+<<")
# the verbs that read their body on stdin when no body word is given
_STDIN_VERBS = re.compile(r"\bchat\s+(?:post|reply|dm)\b|\bdispatch\s+send\b")
_CONTINUATION = re.compile(r"\\\n")
_BODY_NAME = "helm_body"


def corrected_command(command):
    """The command the guard admits with every refused substitution made
    literal, or None when there is none to print."""
    from . import chat
    command = command or ""
    if len(command) > CORRECTED_CAP:
        return None
    # The guard reads the command with its backslash-newlines joined, as
    # bash does before it parses anything. Its offsets are mapped back to
    # the text as typed, so a continuation inside a quoted document is kept.
    gone = [m.start() - 2 * k
            for k, m in enumerate(_CONTINUATION.finditer(command))]
    joined = _CONTINUATION.sub("", command)

    def typed(at):
        return at + 2 * bisect.bisect_right(gone, at)

    found = {at: None for at, _cure, _why
             in chat._unquoted_heredoc_hazards(joined)}
    lines = joined.split("\n")
    kept = chat._kept_lines(joined)
    starts = list(itertools.accumulate([0] + [len(x) + 1 for x in lines]))
    excised = "\n".join(lines[k] for k in kept)
    cut = list(itertools.accumulate([0] + [len(lines[k]) + 1 for k in kept]))

    def joined_at(at):
        j = bisect.bisect_right(cut, at) - 1
        return starts[kept[j]] + at - cut[j]

    for start, end in chat._shell_segment_spans(excised):
        found.update((joined_at(start + at), verb) for at, _cure, _why, verb
                     in chat._segment_hazards(excised[start:end]))
    if not found:
        return None
    escapes, documents = set(), []
    for at, verb in sorted(found.items()):
        if at and _CAT_OPENING.match(joined, at - 1):
            doc = _CAT_DOCUMENT.match(joined, at - 1)
            # a tab-led tag is a terminator only under `<<-`; under plain `<<`
            # bash strips nothing, so the match ended on a line bash reads as
            # body and the copy would be wrong — print none (task/3945)
            if doc is None or (doc.group(5) and doc.group(1) != "<<-"):
                return None
            documents.append((doc, verb))
        else:
            escapes.add(at)
    spans = [(doc.start(), doc.end()) for doc, _verb in documents]
    escapes = {at for at in escapes
               if not any(s <= at < e for s, e in spans)}
    edits = [(typed(at), typed(at), "\\") for at in escapes]
    # each document as a plain stand-in word, and as the copy spells it
    stand_in, spelled, prefix = [], [], []
    # the command with EVERY document replaced by a plain word settles for
    # the shell reader (a `$(cat <<TAG)` inside quotes does not), so a heredoc
    # the reader then finds is the verb's own stdin (task/3945)
    other_heredoc = _outer_heredoc(_apply(command, [
        (typed(doc.start()), typed(doc.end() - 1) + 1, '"$H"')
        for doc, _verb in documents]))
    for n, (doc, verb) in enumerate(documents, 1):
        start, end = typed(doc.start()), typed(doc.end() - 1) + 1
        stand_in.append((start, end, '"$H"'))
        body = command[typed(doc.start(4)):typed(doc.end(4) - 1) + 1] \
            if doc.end(4) > doc.start(4) else ""
        opener = "%s'%s'" % (doc.group(1), doc.group(3))
        document = "%s\n%s\n%s" % (opener, body, doc.group(3))
        if verb and _STDIN_VERBS.search(verb) and len(documents) == 1:
            # the move takes the verb's stdin; if the command line ALREADY
            # opens a heredoc of its own the move would post the wrong body,
            # so print no copy at all (task/3945)
            if other_heredoc:
                return None
            spelled.append((start, end, ("stdin", opener, body,
                                         doc.group(3))))
        else:
            name = _BODY_NAME + ("" if n == 1 else str(n))
            prefix.append(("%s=$(cat %s\n)\n" % (name, document),
                           "%s=H\n" % name))
            spelled.append((start, end, '"$%s"' % name))
    out = "".join(p for p, _s in prefix) + _apply(command, edits + spelled)
    if out == command or chat.argv_guard(out) is not None:
        return None
    # THE COPY KEEPS BASH'S READING (task/3945). A backslash makes a `$(`
    # literal, but the double quotes INSIDE that substitution then close
    # and reopen the word instead: `"$(echo "; touch x; echo ")"` becomes
    # three commands, and `"$(git log --format="%h %s")"` two words. So the
    # copy must split into the same commands and the same words per
    # command as the original, read by the guard's own shell reader with
    # each document as one plain word; anything it cannot settle gets none.
    try:
        same = _shape(_apply(command, stand_in)) == _shape(
            _apply(command, edits + stand_in)) and _shape(out) == _shape(
            "".join(s for _p, s in prefix) + _apply(command, edits + [
                (s, e, "" if isinstance(f, tuple) else f)
                for s, e, f in spelled]))
    except chat._Unsettled:
        return None
    return out if same else None


def _outer_heredoc(command):
    """Whether `command` opens a heredoc the shell attaches to a command line
    — the verb's own stdin (task/3945). Called on the command with its
    cat-documents already replaced by a plain word, which the shell reader can
    settle (a `$(cat <<TAG)` inside quotes it cannot); any heredoc it then
    finds is an outer one, and moving a document to the verb's stdin would
    clobber it. The reader cannot settle even the stand-in: treat a heredoc as
    present, so no copy is printed."""
    from . import chat
    try:
        reader = chat._ShellReader(command)
        reader.read()
    except chat._Unsettled:
        return True
    return bool(reader.bodies)


def _shape(text):
    """The words of each simple command bash reads in `text`, with its
    place in its pipeline, by the guard's shell reader
    (`helm.chat._ShellReader`); raises `_Unsettled`. A command of no words
    (the reader's mark for a separator, a group or a heredoc's line end)
    runs nothing and is left out, so a document moved to a stdin heredoc
    reads as the word it was."""
    from . import chat
    return [(len(cmd.words), cmd.stage) for cmd in chat._simple_commands(
        chat._ShellReader(text).read()) if cmd.words]


def _apply(text, edits):
    """`text` with each ``(start, end, fill)`` of `edits` made, the last
    first. A tuple fill moves a document to a stdin heredoc on its line."""
    for start, end, fill in sorted(edits, key=lambda e: e[0], reverse=True):
        if isinstance(fill, tuple):
            _kind, opener, body, tag = fill
            line_end = text.find("\n", end)
            line_end = len(text) if line_end < 0 else line_end
            text = (text[:start] + opener + text[end:line_end] + "\n" + body
                    + "\n" + tag + text[line_end:])
        else:
            text = text[:start] + fill + text[end:]
    return text


def corrected_tail(command):
    """The lines a substitution refusal appends: the corrected command, or
    nothing. A defect here costs the copy and never the refusal."""
    try:
        fixed = corrected_command(command)
    except Exception:
        return ""
    if fixed is None:
        return ""
    return ("\nBelow is the same command with every refused substitution "
            "made literal, ready to run as is. To run one on purpose, set a "
            "variable first and pass the variable.\n" + CORRECTED + fixed)
