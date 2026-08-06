#!/usr/bin/env python3
"""The conflict-marker rung — REFUSE a commit that stages merge-conflict
marker lines, before they become history.

WHY THE RUNG EXISTS (2026-08-01, measured). A committed tests/test_vcs.py
carried a whole conflict block through commit + push + review; a later
SyntaxError was the only thing that noticed, and only because the markers sat
OUTSIDE a string. The same block inside a docstring or a markdown file parses,
imports, and greens every suite we have — between `git add` and permanence no
instrument sees the SHAPE. This rung is that instrument: the pre-commit hook
runs it against the STAGED set and it refuses the commit naming file:line.

THE SHAPE, not the idea of it. Git writes markers at column 0, N glyphs, then
a space and a label — or nothing at all after the Nth glyph in a hand-mangled
leftover, so the arm takes space-OR-end-of-line:

    <<<<<<< ours          (indented here on purpose: markers are a COLUMN-0
    ||||||| base           shape, so an indented lookalike — including these
    =======                doc lines — is not a marker and never fires)
    >>>>>>> theirs

N is the per-path `conflict-marker-size` ATTRIBUTE, exactly as real git's
merge machinery honors it — resolved from BOTH attribute sources, default 7
each: the INDEX (`check-attr --cached`, the tree being committed) AND the
working tree (plain `check-attr`, the source git's merge actually reads when
it WRITES markers). The two drift in both directions — an untracked
.gitattributes sets 8 while the index says nothing, or the index carries 8
while the worktree copy moved on — and a rung bound to only one source
blesses every marker the other source shaped. A line is a marker at EITHER
resolved length (deduped when they agree); over-catching is the safe
direction for a block-only law, and the neighbor-gated equals arm keeps the
false-positive risk bounded. A rung pinned to seven would bless every marker
git actually writes in a path that configures eight, and refuse none.
Four arms: N×'<', N×'>' and the diff3 base N×'|' are violations on sight; an
(N+1)th glyph or a leading space disqualifies the line.

THE `=======` DECISION (pinned by this module's tests, both directions). A
lonely N-equals line is byte-identical to a Markdown setext-H1 underline and
to an RST section underline over an N-character title — "Section" is seven
characters, so restricting the arm to exactly-N equals would NOT save
RST/markdown and was rejected. The arm is NEIGHBOR-GATED instead: it fires
only when the line sits inside a bracket — an unclosed N×'<' opener above it
and an N×'>' closer below it in the same staged blob. Accepted, documented
trade-off: a lone separator whose opener and closer were both hand-deleted is
invisible to this arm — but that line is exactly the one nothing can
distinguish from a heading underline, and a rung that refuses every setext
heading gets switched off within a day (helm/hardcode.py carries this
estate's scar on that). Inside a REAL bracket a heading underline can be
over-reported as a separator; the opener and closer already refuse that
commit, so the over-report costs nothing.

THE COMMIT'S DIFF IS WHAT BLOCKS, NEVER THE FILE (nevertrack's measured
lesson, bug class `guard-refuses-what-it-cannot-fix`): a marker line ADDED by
this commit is a violation; a marker already in HEAD's copy is a NOTE on
every commit that touches the file — refusing a commit cannot remove what
history already holds, it only teaches the skip flag. A new path adds every
line; a binary-looking diff over a text blob treats every line as added
(over-blocks, never under-sees). The diff is read RENAME-AWARE (`-M`,
pinned on): a pure rename of a marker-carrying file adds no lines, so its
inherited markers stay a note — refusing the one operation that cannot even
edit the content would only teach the skip flag.

FOUR MEASUREMENT LAWS, each one a way a scanner lies:
  * BYTES, NEVER A VIEW. Staged content is read by index OID (`ls-files
    --stage` -> `cat-file blob <oid>`): never the worktree file (the commit
    takes the index, and the two differ in both directions), never a
    textconv-filtered diff (`--no-textconv` on every content diff — a filter
    that hides marker lines must not hide them from the rung).
  * PATHS ARE LITERAL BYTES. Enumeration is `-z`; every per-path diff uses
    `:(literal)` pathspec magic. A file NAMED '*.py' is scanned, never
    glob-expanded into its neighbors' diffs — the hunk parser has no per-file
    attribution, so an expanded neighbor's added range would be blamed on the
    wrong path. Invalid-UTF8 names are data, not a crash; decoding happens
    only at the display edge, with replacement.
  * TRI-STATE. Staged content that cannot be READ refuses as UNKNOWN (a
    RuntimeError -> exit 2): an unscannable blob silently skipped would make
    the one commit that most needs scanning the one that passes.
  * NUL-carrying blobs are skipped outright: markers are a LINE construct and
    git's merge machinery never writes textual markers into a binary merge.
    CRLF files keep the shape — each line is judged with its trailing '\\r'
    stripped.

Legitimate marker-bearing content (a doc ABOUT conflict markers) uses the
one-commit owner override: HELM_CONFLICT_MARKER_SKIP=1.

Stdlib-only, no helm imports: the hook executes an installed SNAPSHOT of this
file as `python3 <snapshot> --staged` with cwd at the committing work tree's
top, same as nevertrack.
"""
import os
import re
import subprocess
import sys

_KIND = {"<": "ours-side opener", "|": "diff3 base marker",
         "=": "separator (bracketed by markers in this file)",
         ">": "theirs-side closer"}

_PRE_EXISTING = (" — ALREADY IN HEAD, so this commit did not stage it and is "
                 "NOT blocked. It needs its own cleanup commit; until then "
                 "this line prints on every commit that touches the file.")

_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEFAULT_SIZE = 7
_GITLINK = b"160000"


def _git(root, *args, stdin=None):
    """Bytes in, bytes out, always: a path is filesystem data, not UTF-8
    prose. Decoding happens only at the display edge, with replacement."""
    p = subprocess.run(("git",) + args, capture_output=True, cwd=root,
                       timeout=60, input=stdin)
    return p.returncode, p.stdout, p.stderr


_ESC = {0x5C: "\\\\"}
_ESC.update((i, "\\x%02x" % i) for i in list(range(0x20)) + [0x7F])
_ESC.update({0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r"})


def _show(raw):
    """The single display edge — and the single escape edge: a pathname is
    attacker-shaped data, and one embedded LF would forge whole diagnostic
    lines. Controls (and the backslash, so escapes stay unambiguous) render
    repr-style; every diagnostic stays exactly one line."""
    return (raw or b"").decode("utf-8", "replace").translate(_ESC)


def staged_entries(root):
    """[(status, old_path, path)] for every path the NEXT commit would create
    or rewrite; deletions leave nothing behind to carry a marker. `-M` is
    pinned on — config must not decide whether a pure rename reads as adding
    every line — and `-z` keeps names literal bytes, never glob patterns."""
    rc, out, err = _git(root, "diff", "--cached", "--name-status", "-z", "-M",
                        "--no-ext-diff", "--diff-filter=ACMRT")
    if rc != 0:
        raise RuntimeError("git diff --cached failed: %s" % _show(err.strip()))
    fields = out.split(b"\0")
    entries, i = [], 0
    while i < len(fields) and fields[i]:
        two = fields[i][:1] in (b"R", b"C")
        if i + (3 if two else 2) > len(fields):
            raise RuntimeError("unparseable --name-status record at %r"
                               % fields[i])
        entries.append((fields[i][:1].decode("ascii", "replace"),
                        fields[i + 1] if two else None,
                        fields[i + 2] if two else fields[i + 1]))
        i += 3 if two else 2
    return entries


def _index_blobs(root):
    """path -> (mode, oid) at stage 0, from `ls-files --stage`: an OID lookup
    has no pathspec to glob-expand and no filter to look through."""
    rc, out, err = _git(root, "ls-files", "-z", "--stage")
    if rc != 0:
        raise RuntimeError("git ls-files --stage failed: %s"
                           % _show(err.strip()))
    blobs = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _tab, path = rec.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) == 3 and parts[2] == b"0":
            blobs[path] = (parts[0], parts[1])
    return blobs


def _attr_sizes(root, paths, cached):
    """ONE source's resolution: path -> marker length per the INDEX
    (`--cached`) or the working tree (plain), default 7 where the attribute
    is unspecified. One batched `--stdin -z` call; stdin paths are literal
    pathnames."""
    sizes = dict.fromkeys(paths, _DEFAULT_SIZE)
    args = ("check-attr",) + (("--cached",) if cached else ()) + \
        ("-z", "--stdin", "conflict-marker-size")
    rc, out, err = _git(root, *args, stdin=b"\0".join(paths) + b"\0")
    if rc != 0:
        raise RuntimeError("git check-attr failed: %s" % _show(err.strip()))
    fields = out.split(b"\0")
    for i in range(0, len(fields) - 2, 3):
        try:
            n = int(fields[i + 2])
        except ValueError:
            continue               # unspecified/unset/set: the default holds
        if n > 0:
            sizes[fields[i]] = n
    return sizes


def marker_sizes(root, paths):
    """path -> the marker run lengthS real git could have written there: the
    `conflict-marker-size` attribute resolved from BOTH sources — the index
    (the tree being committed) and the working tree (what the merge machinery
    read when it WROTE the markers). The two drift in both directions; a line
    matching EITHER resolved length is a marker (module docstring owns the
    over-catch reasoning). Deduped: agreement yields one length."""
    if not paths:
        return {}
    index = _attr_sizes(root, paths, True)
    work = _attr_sizes(root, paths, False)
    return {p: frozenset((index[p], work[p])) for p in paths}


def _added_ranges(root, status, old, path):
    """Inclusive new-side line ranges this commit ADDS, or None when EVERY
    line is this commit's: a new path, or a change git calls binary — no line
    structure to attribute, so the caller over-blocks rather than under-sees.
    A pure rename yields NO ranges — its markers are inherited. `:(literal)`
    is load-bearing: the bare path of a file NAMED '*.py' glob-expands, and
    this hunk parser has no per-file attribution, so a neighbor's added range
    would be blamed on this path. `--no-textconv` is load-bearing the same
    way: a textconv filter that strips marker lines would erase exactly the
    added ranges that must refuse. A git failure RAISES: an empty answer here
    would read as "adds nothing"."""
    if status == "A":
        return None
    spec = [b":(literal)" + p for p in ((old, path) if old else (path,))]
    rc, out, err = _git(root, "diff", "--cached", "-U0", "--no-color",
                        "--no-ext-diff", "--no-textconv", "-M", "--", *spec)
    if rc != 0:
        raise RuntimeError("git diff --cached failed for %s: %s"
                           % (_show(path), _show(err.strip())))
    ranges = []
    for line in out.split(b"\n"):
        # only the binary notice can start with these bytes: content lines
        # in a -U0 diff always carry a +/-/space/backslash prefix
        if line.startswith(b"Binary files ") or line.startswith(b"GIT binary patch"):
            return None
        m = _HUNK.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        if count:
            ranges.append((start, start + count - 1))
    return ranges


def _marker(line, sizes):
    """('<'|'='|'>'|'|', matched_size) when the line IS a conflict-marker
    line shape at ANY of this path's resolved marker sizes, else None.
    Column 0, exactly `size` glyphs, then space or end of line; the equals
    separator is the exact lonely run only. At most one size can match — a
    glyph run's length is exact — so the pair is deterministic."""
    ch = line[:1]
    for size in sizes:
        if ch == b"=":
            if line == b"=" * size:
                return "=", size
        elif ch in (b"<", b">", b"|") and line[:size] == ch * size \
                and line[size:size + 1] in (b"", b" "):
            return ch.decode(), size
    return None


def scan_lines(blob, sizes=frozenset({_DEFAULT_SIZE})):
    """-> sorted [(lineno, kind, size)] conflict-marker lines in one text
    blob; `sizes` is the path's set of resolved marker lengths and `size` is
    the length the line actually matched.

    Opener/closer/diff3-base fire on sight. The equals separator only counts
    once BRACKETED: recorded while an opener is open above it, reported only
    when a closer arrives below it — the module docstring owns the
    RST/markdown reasoning. A second opener restarts the bracket. The bracket
    gate spans sizes (an 8-opener can bracket a 7-separator): over-reporting
    inside a REAL bracket costs nothing, the opener already refuses."""
    hits, pending, opened = [], [], False
    for i, raw in enumerate(blob.split(b"\n"), 1):
        line = raw.rstrip(b"\r")     # CRLF files still carry the shape
        hit = _marker(line, sizes)
        if hit is None:
            continue
        kind, size = hit
        if kind == "<":
            hits.append((i, kind, size))
            opened, pending = True, []
        elif kind == "=":
            if opened:
                pending.append((i, size))
        elif kind == ">":
            hits.append((i, kind, size))
            hits += [(j, "=", n) for j, n in pending]
            opened, pending = False, []
        else:
            hits.append((i, kind, size))
    return sorted(hits)


def scan_staged(root):
    """-> (violations, notes): violations BLOCK the commit; notes are printed
    and let it through (a marker HEAD already carries is named, never blamed
    on this commit). UNKNOWN staged content RAISES — the tri-state law: a
    blob that cannot be read refuses the scan, never slips through it."""
    violations, notes = [], []
    entries = staged_entries(root)
    if not entries:
        return violations, notes
    blobs = _index_blobs(root)
    sizes = marker_sizes(root, [e[2] for e in entries])
    for status, old, path in entries:
        mode, oid = blobs.get(path, (None, None))
        if mode == _GITLINK:
            continue                 # a gitlink names a commit, not lines
        if oid is None:
            raise RuntimeError("no stage-0 index entry for staged path %s — "
                               "UNKNOWN staged content refuses" % _show(path))
        rc, blob, err = _git(root, "cat-file", "blob", oid)
        if rc != 0:
            raise RuntimeError("cat-file %s (%s) failed: %s — UNKNOWN staged "
                               "content refuses, never skips"
                               % (_show(oid), _show(path), _show(err.strip())))
        if b"\0" in blob[:8192]:
            continue                 # not line-text; see module docstring
        hits = scan_lines(blob, sizes.get(path, frozenset({_DEFAULT_SIZE})))
        if not hits:
            continue
        ranges = _added_ranges(root, status, old, path)
        for lineno, kind, size in hits:
            added = ranges is None or any(lo <= lineno <= hi
                                          for lo, hi in ranges)
            row = "%s:%d — merge-conflict %s '%s'" % (
                _show(path), lineno, _KIND[kind], kind * size)
            if added:
                violations.append(row + ", ADDED BY THIS COMMIT")
            else:
                notes.append(row + _PRE_EXISTING)
    return violations, notes


def main(argv=None):
    """The pre-commit entry: scan the staged set of the repo at cwd, refuse
    on any added marker line. Exit 0 clean, 1 on violations, 2 when the scan
    itself cannot run (fail CLOSED — an unscannable commit is not a scanned
    one)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--staged"]:
        sys.stderr.write("usage: conflict_marker.py --staged  (run by the "
                         "helm pre-commit guard; cwd must be inside the "
                         "repo)\n")
        return 2
    rc, out, _err = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        sys.stderr.write("[helm conflict-marker] REFUSED: not inside a git "
                         "work tree — cannot scan the staged set\n")
        return 2
    try:
        violations, notes = scan_staged(out.strip())
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write("[helm conflict-marker] REFUSED: staged-set scan "
                         "failed — %s\n" % exc)
        return 2
    w = sys.stderr.write
    for note in notes:
        w("[helm conflict-marker] %s\n" % note)
    if not violations:
        return 0
    w("[helm conflict-marker] REFUSED: this commit stages merge-conflict "
      "marker lines:\n")
    for v in violations:
        w("[helm conflict-marker]   %s\n" % v)
    w("[helm conflict-marker] delete the marker lines (finish the merge), "
      "then re-add; `git restore --staged <path>` unstages.\n")
    w("[helm conflict-marker] false positive (a doc ABOUT markers)? owner "
      "override for one commit: HELM_CONFLICT_MARKER_SKIP=1\n")
    w("[helm conflict-marker] scanner: helm/conflict_marker.py; the "
      "docstring owns the equals-separator decision.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
