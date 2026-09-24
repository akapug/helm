#!/usr/bin/env python3
"""Warn when a staged write persists a value this code cut, and says nothing.

THE CLASS. A producer bounds a value -- a slice, a ``*_CAP``/``*_MAX``/
``*_LIMIT`` constant, a ``textwrap.shorten`` -- and then the cut copy is
written or published as though it were the whole thing. Every reader
downstream then holds a value that is indistinguishable from a complete one,
because the loss left no trace in what was written. The store already forbids
both halves of this shape: an absence and an all-clear may not share a
representation, and a bound is either REFUSED over or MARKED in the value it
produces. Neither rule can fire at the moment somebody writes the slice, which
is why this is a rung and not a premise.

THE RULE, exactly as implemented.

    A BOUNDED WRITE is an expression that shortens a value: a subscript whose
    slice has a constant upper bound of at least ``SMALL_BOUND``, or an upper
    bound named ``*_CAP``, ``*_MAX`` or ``*_LIMIT``; a tail slice of at least
    that many items; or a call to ``textwrap.shorten``. A slice of the first
    one, two or three items is a FIELD EXTRACTION -- a status byte, a marker
    character -- and loses nothing. A slice of an expression that names an
    IDENTITY (``_IDENTIFIER``: an id, a sha, a digest) is a conventional
    identifier prefix and is not a cut either; both carve-outs are by the
    shape of the sliced expression, never by its surroundings.

    It FLOWS INTO A WRITE when the bounded expression is an argument of a
    persisting or publishing call, or when it is bound to a name (directly, or
    nested inside a dict/list/tuple literal) and that name reaches such a call
    within ``SPAN`` statements of the binding, counted over the enclosing
    function's statements in source order, descending into ``if``/``with``/
    ``try``/loop bodies but never into a nested function.

    A WRITE is a call whose callee's terminal name is in ``WRITE_CALLS``
    (``dump``/``dumps``, the ``write`` family, ``write_json``,
    ``atomic_write``, ``post``, ``publish``, ``broadcast``), or an
    ``append``/``extend`` onto a receiver whose own name says it is a durable
    sequence (``LEDGER_NAMES``). A ``print`` is NOT a write: a column width, a
    table cell and a log line are display, and treating them as records is what
    makes a rung like this get switched off. The journal case is still caught,
    because a journal row is serialized and appended, not printed.

    A MARK is a string in the SAME function that names the cut -- it contains
    ``truncat``, ``elid``, ``clip``, ``shorten``, ``omitted``, an ellipsis, a
    ``%d of`` / ``of %d`` pair, a byte/character count beside a format
    placeholder, or the cap's own identifier -- or a ``raise`` in that function whose subtree names the
    cap. A marked cut and a refusal over the bound are both cures, so neither
    is reported.

THE HALF THIS RUNG DOES NOT SEE, said here rather than left for a reader to
discover. The sibling shape is ABSENCE SHARING A REPRESENTATION WITH AN
ALL-CLEAR: an empty collection that is both "nothing was wrong" and "nothing
was examined", returned as the success value. It is the same defect from the
other end -- a loss with no mark -- and it is not detectable by the rule above,
because its evidence is what the function DID NOT record, not a bound it
applied. helm task/2760 carries it.

WARN-ONLY, on a measurement rather than a caution. Classified by hand over the
whole committed ``helm/`` population, 13 of 21 findings were true unmarked
losses; 6 rendered an identity or a label short inside a human-readable line
and 2 cut nothing at all. Those last two classes are not separable from a lossy
cut by shape alone, so this rung reports and never refuses, and its banner
carries the rate. A deliberate cut names its reason on the
slicing line:

    row["summary"] = str(summary)[:CAP]  # noqa: SILENT_CAP — the cap is a
                                         # display width; the full value is
                                         # stored in the row beside it

The reason is required: a bare ``# noqa: SILENT_CAP`` is not an escape and
still reports. Exemptions are by FILE IDENTITY, never by wording -- ``tests/``,
``docs/``, ``journal/`` and ``helm/journal/`` are outside the scan.

``--staged`` judges Python ADDED under ``helm/``; ``--all`` reports the whole
population, existing debt included, and blocks nothing either way.

Stdlib-only, with no helm import. The installer snapshots these bytes beside
the shared pre-commit hook, where no helm package exists to import, so a
committing lane cannot edit the judge of its own staged set.
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize

TAG = "[helm silent-cap]"
SKIP_ENV = "HELM_SILENT_CAP_SKIP"

#: Judged tree. A path outside this prefix is not this rung's subject.
POLICED = ("helm/",)
#: Exempt by FILE IDENTITY. A journal records history and a test builds a
#: fixture; a cut there loses nothing a reader is entitled to.
EXEMPT = ("tests/", "docs/", "journal/", "helm/journal/")

#: How far a cut value may travel and still be attributed to the write it
#: reaches. The instances behind this rung bind the cut into a row and write
#: it within two statements; a `with open(...)` plus a serialize step is three.
#: Eight leaves room for that shape without spanning an unrelated block.
SPAN = 8

#: An upper bound spelled as a name is a cap when it is SHOUTED and ends in
#: one of these -- MESSAGE_BODY_CAP, EVENTS_MAX, LINE_LIMIT.
_CAP_NAME = re.compile(r"(?:^|_)(?:CAP|MAX|LIMIT)$")

#: A CONSTANT BOUND BELOW THIS IS A FIELD EXTRACTION, NOT A CUT. `fields[i][:1]`
#: reads a status byte and `line[:2]` reads a marker; nothing is lost, because
#: the value was never longer. The cost is stated rather than hidden: a genuine
#: top-N selection of fewer than four items is invisible to this rung.
SMALL_BOUND = 4

#: AN IDENTIFIER PREFIX IS NOT A CUT. A 12-character commit id and an 8-character
#: digest are this tree's conventional spelling of an identity, the full value
#: is recoverable from the same system, and every reader already knows a short
#: hex string is a prefix. Matched on the spellings inside the SLICED VALUE, so
#: only the sliced expression -- never its surroundings -- can grant this.
_IDENTIFIER = re.compile(
    r"(?:^|_)(?:id|ids|sid|sha|digest|hexdigest|hash|oid|uuid|guid|token|"
    r"fingerprint|checksum)$", re.IGNORECASE)

#: The terminal name of a call that persists or publishes its arguments.
#: `print` is deliberately absent; see the module docstring.
WRITE_CALLS = frozenset((
    "dump", "dumps",
    "write", "writelines", "write_text", "write_bytes",
    "write_json", "atomic_write", "append_json",
    "post", "publish", "broadcast",
))
#: `append` is a write only onto a receiver that IS a durable sequence. The
#: receiver is matched on its own spelling because a bare `.append` onto a
#: local working list is the single most common shape in this tree and is not
#: a write at all. `lines` and `log` are DELIBERATELY ABSENT: in this tree
#: they name a local display buffer that gets joined and printed, and treating
#: a rendering as a record is the false positive that gets a rung switched off.
LEDGER_NAMES = re.compile(
    r"(?:^|_)(?:ledger|journal|events?|rows|records?|entries)$",
    re.IGNORECASE)

#: A string that names the cut. `bytes`/`chars` count only alongside a format
#: placeholder, because the bare word is ambient in a tree that moves bytes.
_MARK_WORDS = re.compile(
    r"truncat|elid|clipp?ed|shorten|omitted|abbreviat|\bcut\b|"
    r"…|\.\.\.|\[\.\.\.\]", re.IGNORECASE)
#: A FORMAT PLACEHOLDER, not any braces: `{v, ts, actor}` in a docstring is a
#: set, and reading it as a field made a cut with no mark read as marked.
_FIELD = r"(?:%[sdi]|\{\}|\{[A-Za-z_][\w.\[\]]*(?::[^{}]*)?\})"
_MARK_COUNT = re.compile(
    _FIELD + r"[^\n]{0,16}\b(?:byte|char|line|item|row)s?\b|"
    r"\b(?:byte|char|line|item|row)s?\b[^\n]{0,16}" + _FIELD + r"|"
    r"%d\s+of\b|\bof\s+%d", re.IGNORECASE)

_ESCAPE = re.compile(r"^#\s*noqa:\s*SILENT_CAP\s+(?:—|-{1,2})\s*(\S.*)$")
_BARE_ESCAPE = re.compile(r"^#\s*noqa:\s*SILENT_CAP\s*$")

_CURE = "mark the cut in the written value, or refuse over the bound"

#: WARN, NEVER BLOCK, and the reason is a measurement rather than a caution.
#: Classified by hand over the whole committed `helm/` population: of 21
#: findings, 13 were true unmarked losses, 6 were an identifier or label
#: rendered short inside a human-readable line, and 2 were not cuts at all.
#: A rung that refuses at that rate walls a lane on its own judgement calls,
#: and a rung that walls honest work teaches its own bypass.
POLARITY = "WARN only"
MEASURED = ("13 of the 21 findings in the classification that set this "
            "polarity were true unmarked losses; 6 rendered an identity and "
            "2 cut nothing")

# THE CLASSIFIED POPULATION behind that rate: the hits of `--all` over the
# committed helm/ tree at the pass that set the polarity, classified by hand.
# This block is the one copy; a doc page that restated it would be a second
# reader of one measurement.
#
# THE RATE IS A PROPERTY OF THAT PASS, NOT A RUNNING COUNT OF THE TREE. Every
# row in the TRUE class below now carries its cut inside the value it writes,
# so `--all` over this tree reports the DISPLAY-ONLY and NOT-A-CUT rows and
# nothing else. The classification stays because it is the evidence for
# WARN-only, and it is what a later reader needs to judge whether the
# polarity is still right; `tests/test_silent_cap.py` holds each cured writer
# as a production negative control, so a reverted cure is named by a red arm
# rather than by this comment going quietly stale.
#
#   TRUE -- content lost, persisted, nothing in the written value said so
#   (13). Each of these is CURED; the shape of each cure is in its own commit.
#     handoff.py:654      a handoff front-matter description, written to file
#     injectpack.py:308   a premise gloss, cut into a served row
#     nouncensus.py:150   a censused source line, cut into a census row
#     pk.py:278           the journal receipt summary, appended to the ledger
#     record.py:393       the swallow log's `where`
#     record.py:399       the swallow log's exception message
#     reflex.py:183       a reflex front-matter description, written to file
#     resumeturn.py:2387  a wake payload's reason
#     resumeturn.py:2393  the same reason, cut again for the pipe bound
#     resumeturn.py:2394  the SERIALIZED payload, cut mid-JSON
#     web_core.py:51      a served entries list, cut at ENTRY_CAP
#     web_core.py:76      a served review statement, cut at REVIEW_STMT_CAP
#     whoami.py:206       a whoami front-matter description, written to file
#
#   DISPLAY-ONLY -- a short identity or label inside a human-readable line,
#   where the full value is recoverable from the same system (6)
#     dispatches.py:7576  a reviewed tip, rendered short in a chat line
#     foldcompose.py:589  two commit ids in a verdict sentence
#     foldcompose.py:600  two more in the sibling sentence
#     injectbudget.py:723 a session id used as a seat label
#
#   ALREADY MARKED -- a cure this rule does not recognise (0)
#
#   NOT A CUT (2)
#     providers.py:666    a fixed-width timestamp read for a comparison; the
#                         written value is the whole line, not the slice
#     resumeturn.py:2386  a sanity bound on a short display name, never reached
#
# The two carve-outs the rule DOES make -- an identifier prefix, and a bound
# under SMALL_BOUND -- were derived from this same pass and removed eleven
# further hits before it: status-byte reads, hash prefixes and top-N picks.

#: The rung's own must-hit control. An analyzer that reports nothing over this
#: is broken, and silence from a broken analyzer is not a clean bill.
_CONTROL = """\
LIMIT = 200


def store(summary, path):
    row = {"summary": str(summary)[:LIMIT]}
    with open(path, "a") as fh:
        fh.write(dumps(row))
"""
#: The negative half of the control: the same shape, cured. An analyzer that
#: reports this one is reporting the cure and would train the tree off itself.
_CONTROL_CURED = """\
LIMIT = 200


def store(summary, path):
    text = str(summary)
    kept = text[:LIMIT]
    if len(text) > LIMIT:
        kept += " [truncated: %d of %d chars]" % (LIMIT, len(text))
    row = {"summary": kept}
    with open(path, "a") as fh:
        fh.write(dumps(row))
"""


def policed(rel):
    """Is this repo-relative path this rung's subject?"""
    rel = rel.replace(os.sep, "/").lstrip("./")
    if not rel.endswith(".py"):
        return False
    if any(rel == e.rstrip("/") or rel.startswith(e) for e in EXEMPT):
        return False
    return any(rel.startswith(p) for p in POLICED)


def _git(root, *args):
    return subprocess.run(("git", "-c", "core.quotePath=false") + args,
                          cwd=root, capture_output=True, timeout=60)


def _root(start=None):
    done = _git(start or os.getcwd(), "rev-parse", "--show-toplevel")
    if done.returncode != 0:
        return None
    return os.fsdecode(done.stdout.strip())


def _short(text, limit=72):
    """This module's own bounded write, MARKED -- the rule it enforces.

    A finding's excerpt is a rendering for a human, not a record, and it is
    still marked: a reader must never be able to mistake a cut expression for
    the whole one while reading a report about cut values.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return "%s… (%d of %d chars)" % (text[:limit], limit, len(text))


# ---------------------------------------------------------------- the rule

def _terminal(call):
    """The callee's last name: `f.write` -> 'write', `json.dumps` -> 'dumps'."""
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


def _receiver_name(call):
    """The spelling the call hangs off: `rows.append(x)` -> 'rows'."""
    fn = call.func
    if not isinstance(fn, ast.Attribute):
        return ""
    base = fn.value
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def is_write(call):
    """Does this call persist or publish what it is handed?"""
    name = _terminal(call)
    if name in WRITE_CALLS:
        return True
    if name in ("append", "extend"):
        return bool(LEDGER_NAMES.search(_receiver_name(call)))
    return False


def _cap_name(node):
    """The cap identifier an upper bound names, or ''."""
    name = ""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    if name and name == name.upper() and _CAP_NAME.search(name):
        return name
    return ""


def _identity_head(value):
    """The spelling of what is being sliced, or '' when it has none.

    THE HEAD, NOT THE SUBTREE. `(e["id"] + " - " + e["steer"])[:170]` names an
    id somewhere inside it and is still a CONCATENATION -- content, cut. Only
    the expression the slice is taken OF can vouch for itself, so a BinOp is
    never an identity and an `or` default reads through to its first operand.
    """
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) \
            and value.values:
        return _identity_head(value.values[0])
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Subscript) and \
            isinstance(value.slice, ast.Constant) and \
            isinstance(value.slice.value, str):
        return value.slice.value
    if isinstance(value, ast.Call):
        name = _terminal(value)
        if _IDENTIFIER.search(name or ""):
            return name
        if name in ("get", "pop") and value.args and \
                isinstance(value.args[0], ast.Constant) and \
                isinstance(value.args[0].value, str):
            return value.args[0].value
        return name
    return ""


def _identifier_slice(value):
    """Does the sliced expression name an identity rather than content?"""
    head = _identity_head(value)
    return bool(head) and bool(_IDENTIFIER.search(head))


def _negative_constant(node):
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int)
            and not isinstance(node.operand.value, bool))


def bound_of(node):
    """('constant'|'<CAPNAME>'|'shorten', node) when this expression cuts a
    value, else None.

    A FULL SLICE IS NOT A CUT. `x[:]`, `x[i:]` and `x[a:b]` against two names
    carry no bound this rung can name, and a step-only slice reorders rather
    than shortens.
    """
    if isinstance(node, ast.Call) and _terminal(node) == "shorten":
        return "shorten"
    if not isinstance(node, ast.Subscript):
        return None
    sl = node.slice
    if not isinstance(sl, ast.Slice):
        return None
    if _identifier_slice(node.value):
        return None
    if sl.upper is not None:
        low_ok = sl.lower is None or (isinstance(sl.lower, ast.Constant)
                                      and sl.lower.value == 0)
        if not low_ok:
            return None
        if isinstance(sl.upper, ast.Constant) and \
                isinstance(sl.upper.value, int) and \
                not isinstance(sl.upper.value, bool) and \
                sl.upper.value >= SMALL_BOUND:
            return "constant"
        named = _cap_name(sl.upper)
        return named or None
    # a tail slice: x[-N:] keeps the end and drops the head
    if sl.lower is not None and sl.upper is None and \
            _negative_constant(sl.lower) and \
            -sl.lower.operand.value >= SMALL_BOUND:
        return "constant"
    return None


_NESTED_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _statements(body):
    """Every statement under `body` in source order, nested bodies included.

    A write inside `with open(...)` or a `try` is still the write the cut
    reaches; a flat scan of the function's top level cannot see it, and the
    shape that made this rung necessary is exactly that one. A NESTED
    FUNCTION IS NOT DESCENDED INTO: its statements are its own scope and it
    is analyzed in its own right, so folding them in here would let an outer
    binding vouch for an inner write that cannot see it.
    """
    out = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, _NESTED_SCOPE):
            continue
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                out += _statements([n for n in inner
                                    if isinstance(n, ast.stmt)])
        for handler in getattr(stmt, "handlers", None) or ():
            if isinstance(handler, ast.ExceptHandler):
                out += _statements(handler.body)
    return out


def _own_nodes(stmt):
    """Every expression node this statement owns, stopping at a nested
    statement -- so a `with` block does not absorb the writes in its body and
    widen an unrelated binding's reach over them."""
    out, stack = [], [stmt]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.excepthandler)):
                continue
            out.append(child)
            stack.append(child)
    return out


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _targets(stmt):
    """The names a binding statement writes to, root name for a subscript."""
    out = set()
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                out.add(node.id)
    return out


def _value_of(stmt):
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return stmt.value
    return None


def _docstring_node(fn):
    """The function's docstring expression, which is PROSE and never a mark.

    A sentence explaining the function is not written beside the value; a
    docstring that happens to spell a shape the mark vocabulary recognises
    would clear the warning for a cut that still ships unmarked.
    """
    first = fn.body[0] if fn.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        return first.value
    return None


def _marks(fn, cap):
    """Does this function name the cut it makes?"""
    doc = _docstring_node(fn)
    for node in ast.walk(fn):
        if node is doc:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _MARK_WORDS.search(node.value) or _MARK_COUNT.search(node.value):
                return True
            if cap and cap not in ("constant", "shorten") and \
                    cap in node.value:
                return True
    if cap and cap not in ("constant", "shorten"):
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise) and cap in _names_in(node):
                return True
    return False


def _functions(tree):
    """Every function in the module, innermost owning its own statements."""
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _writes_in(stmt):
    return [n for n in _own_nodes(stmt)
            if isinstance(n, ast.Call) and is_write(n)]


def _analyze_function(fn, rel, escapes, bare, report):
    """Report every unmarked bounded write this function persists."""
    stmts = _statements(fn.body)
    for i, stmt in enumerate(stmts):
        for call in _writes_in(stmt):
            for node in [call] + list(ast.walk(call)):
                cap = bound_of(node)
                if cap:
                    report(fn, rel, node, cap, call, escapes, bare)
        value = _value_of(stmt)
        if value is None:
            continue
        cuts = [(n, bound_of(n)) for n in [value] + list(ast.walk(value))]
        cuts = [(n, c) for n, c in cuts if c]
        if not cuts:
            continue
        carried = _targets(stmt)
        if not carried:
            continue
        for later in stmts[i + 1:i + 1 + SPAN]:
            for call in _writes_in(later):
                if not (_names_in(call) & carried):
                    continue
                for node, cap in cuts:
                    report(fn, rel, node, cap, call, escapes, bare)


def analyze_source(source, rel):
    """[(rel, line, cap_expr, cap_name, write_expr, escaped_without_reason)]"""
    tree = ast.parse(source, filename=rel)
    escapes, bare = _escapes(source)
    found, seen = [], set()

    def report(fn, path, node, cap, call, esc, bare_lines):
        line = getattr(node, "lineno", 0)
        if line in esc:
            return
        if _marks(fn, cap):
            return
        key = (line, getattr(node, "col_offset", -1))
        if key in seen:
            return
        seen.add(key)
        found.append((path, line, _short(_unparse(node)),
                      "" if cap in ("constant", "shorten") else cap,
                      _short(_unparse(call)), line in bare_lines))

    for fn in _functions(tree):
        _analyze_function(fn, rel, escapes, bare, report)
    return sorted(found, key=lambda row: (row[0], row[1]))


def _unparse(node):
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):                       # noqa: BLE001
        return "<line %s>" % getattr(node, "lineno", "?")


def _escapes(source):
    """({line: reason}, {lines carrying a REASONLESS escape})."""
    reasoned, bare = {}, set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            got = _ESCAPE.match(tok.string)
            if got:
                reasoned[tok.start[0]] = got.group(1)
            elif _BARE_ESCAPE.match(tok.string):
                bare.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return reasoned, bare


# ------------------------------------------------------------- the doors

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _staged_paths(root):
    done = _git(root, "diff", "--cached", "--name-only", "-z", "--no-ext-diff",
                "--diff-filter=ACMRT")
    if done.returncode != 0:
        raise RuntimeError("git diff --cached failed: %s"
                           % done.stderr.decode("utf-8", "replace").strip())
    return [p.decode("utf-8", "surrogateescape")
            for p in done.stdout.split(b"\0") if p and policed(
                p.decode("utf-8", "surrogateescape"))]


def _staged_blob(root, rel):
    done = _git(root, "cat-file", "blob", ":0:" + rel)
    return done.stdout if done.returncode == 0 else None


def _added_ranges(root, rel):
    """Inclusive new-side line ranges this commit adds to `rel`."""
    # ALGORITHM PINNED -- helm/conflict_marker.py owns the measurement: myers
    # re-adds identical text to shorten a big deletion's edit script, so an
    # unchanged line reads as this commit's. Histogram does not, and the pin
    # also neutralises an ambient `diff.algorithm`.
    done = _git(root, "-c", "diff.algorithm=histogram",
                "diff", "--cached", "-U0", "--no-ext-diff",
                "--no-color", "--", ":(literal)" + rel)
    if done.returncode != 0:
        raise RuntimeError("git diff --cached -U0 failed for %s" % rel)
    out = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        got = _HUNK.match(line)
        if not got:
            continue
        start = int(got.group(1))
        count = int(got.group(2) or 1)
        if count:
            out.append((start, start + count - 1))
    return out


def _intersects(line, ranges):
    return any(lo <= line <= hi for lo, hi in ranges)


def scan_staged(root):
    """(findings, issues) over Python this commit ADDS under helm/."""
    findings, issues = [], []
    for rel in _staged_paths(root):
        blob = _staged_blob(root, rel)
        if blob is None:
            issues.append("%s: staged entry is not a readable blob" % rel)
            continue
        try:
            source = blob.decode("utf-8")
        except UnicodeDecodeError:
            issues.append("%s: staged Python is not UTF-8" % rel)
            continue
        try:
            ranges = _added_ranges(root, rel)
        except RuntimeError as exc:
            issues.append("%s: added lines unreadable (%s)" % (rel, exc))
            continue
        if not ranges:
            continue
        try:
            rows = analyze_source(source, rel)
        except (SyntaxError, ValueError) as exc:
            issues.append("%s: staged Python cannot be parsed (%s)"
                          % (rel, exc))
            continue
        findings += [r for r in rows if _intersects(r[1], ranges)]
    return findings, issues


def scan_population(root, ref="HEAD"):
    """Every finding in one committed tree, standing debt included."""
    listed = _git(root, "ls-tree", "-rz", "--name-only", ref, "--", "helm")
    if listed.returncode != 0:
        raise RuntimeError("tree population could not be listed")
    findings, issues = [], []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        if not policed(rel):
            continue
        got = _git(root, "show", ref + ":" + rel)
        if got.returncode != 0:
            issues.append("%s: blob unreadable at %s" % (rel, ref))
            continue
        try:
            findings += analyze_source(got.stdout.decode("utf-8"), rel)
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            issues.append("%s: cannot be parsed (%s)" % (rel, exc))
    return findings, issues


def control_ok():
    """The analyzer sees its planted cut, and does NOT see the cured one."""
    try:
        hit = analyze_source(_CONTROL, "control.py")
        cured = analyze_source(_CONTROL_CURED, "control_cured.py")
    except (SyntaxError, ValueError):                          # noqa: BLE001
        return False
    return bool(hit) and not cured


def _print(findings):
    for rel, line, cut, cap, write, bare in findings:
        print("%s   %s:%d  %s%s  ->  %s" %
              (TAG, rel, line, cut,
               "  [cap %s]" % cap if cap else "", write), file=sys.stderr)
        if bare:
            print("%s     (a bare `# noqa: SILENT_CAP` is not an escape — the "
                  "reason IS the audit trail)" % TAG, file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    modes = [f for f in ("--staged", "--all") if f in argv]
    for flag in modes:
        argv.remove(flag)
    mode = modes[0] if len(modes) == 1 else None
    root = "."
    if "--repo" in argv:
        at = argv.index("--repo")
        root = argv[at + 1] if at + 1 < len(argv) else "."
        del argv[at:at + 2]
    if mode is None or argv:
        print("usage: silent_cap.py --staged|--all [--repo PATH]",
              file=sys.stderr)
        return 2
    resolved = _root(root if os.path.isdir(root) else None)
    if resolved is None:
        print("%s not inside a git work tree — scan SKIPPED" % TAG,
              file=sys.stderr)
        return 0
    if not control_ok():
        print("%s CLASSIFIER CONTROL FAILED — the analyzer cannot see its "
              "planted unmarked cut, or reports the marked one; scan "
              "SKIPPED, and silence here is not a clean bill" % TAG,
              file=sys.stderr)
        return 0
    try:
        if mode == "--all":
            findings, issues = scan_population(resolved)
        else:
            findings, issues = scan_staged(resolved)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print("%s scan UNMEASURED (%s: %s) — WARN-only, commit allowed"
              % (TAG, type(exc).__name__, exc), file=sys.stderr)
        return 0
    for issue in issues:
        print("%s scan UNKNOWN: %s" % (TAG, issue), file=sys.stderr)
    if not findings:
        return 0
    print("%s %d bounded write%s reach%s a persisting call with no mark:"
          % (TAG, len(findings), "s"[:len(findings) != 1],
             "" if len(findings) != 1 else "es"), file=sys.stderr)
    _print(findings)
    print("%s %s. %s (%s)%s. A deliberate cut names its reason: "
          "`# noqa: SILENT_CAP — <reason>` on the slicing line."
          % (TAG, _CURE, POLARITY, MEASURED,
             "; existing debt, nothing blocked" if mode == "--all" else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
