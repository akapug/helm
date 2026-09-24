#!/usr/bin/env python3
"""Refuse a bounded chronology vocabulary in public-bound source prose.

The four rule labels below are the complete promise: ``review chronology``,
``named internal reviewer``, ``dated internal chronology``, and ``superseded
implementation``. A clean result means no phrase matching those compiled rules
was added; it does not certify the absence of every possible chronology.

The published tree should explain the property that holds, the failure mode it
prevents, and the mechanism that enforces it. Review chronology, dated local
measurements, and superseded implementation behaviour are evidence for the
private engineering record; they are not self-contained rationale for a reader
of the source.

The pre-commit rung judges only prose ADDED under ``helm/`` and ``tests/``.
A line the same staged set REMOVES, byte for byte, from another
public-bound file is MOVED rather than added: those bytes were already
published under the boundary, so relocating them publishes nothing and
the destination is not their first appearance. The match is exact -- an
edited line is new prose wearing a familiar shape and is judged.
Python comments and docstrings are parsed as prose. JavaScript, CSS, and HTML
assets contribute comment blocks whose marker begins the physical line. String
and fixture data are not prose and are never searched. A pure rename from
outside the public boundary scans the whole destination because unchanged bytes
are newly published there.

Historical records are exempt by FILE IDENTITY, never by wording: ``docs/``,
``journal/``, and ``helm/journal/``. Existing source debt is reported by
``--all`` but does not block an unrelated commit; the staged door refuses only
newly added history.

Stdlib-only, with no helm import. The hook executes an installed snapshot beside
its hardened conflict-marker sibling, so a committing lane cannot edit its own
judge.
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize


_HISTORICAL = ("docs/", "journal/", "helm/journal/")
_WORLD = ("helm/", "tests/")

_COUNT = (r"one|two|three|four|five|six|seven|eight|nine|ten|"
          r"several|successive|separate|multiple|many|\d+")
_RULES = (
    ("review chronology", re.compile(
        r"\b(?:%s)\s+(?:successive\s+|separate\s+)?review\s+findings?\b|"
        r"\breview\s+(?:round|finding)s?\b|"
        r"\b(?:%s)\s+(?:successive\s+|separate\s+)?reviews?\b|"
        r"\bfailed\s+(?:(?:%s|the|a|an|this|that|same|opposite|new)\s+){0,4}reviews?\b|"
        r"\bcost\s+(?:a|the)\s+reviewer\s+(?:a\s+)?turn\b|"
        r"\breviewer(?:'s)?\s+(?:finding|found|caught|flagged|reported|asked)\b"
        % (_COUNT, _COUNT, _COUNT), re.IGNORECASE)),
    ("named internal reviewer", re.compile(
        r"@(?:codex(?:-\d+)?|kimi)\b|"
        r"\b(?:codex-\d+|helm-claude-\d+)(?:'s)?[^.\n]{0,32}"
        r"\b(?:review|finding|round|flagged|caught|reported|asked)\b|"
        r"\b(?:review|finding|round|flagged|caught|reported|asked)\b"
        r"[^.\n]{0,32}\b(?:codex-\d+|helm-claude-\d+)\b|"
        r"@[a-z0-9][\w-]*(?:'s)?\s+"
        r"(?:finding|review|round|MED|LOW|HIGH)\b|"
        r"\bcross-family\s+review\s+"
        r"(?:measured|found|caught|flagged|reported)\b",
        re.IGNORECASE)),
    ("dated internal chronology", re.compile(
        r"\b20\d{2}-\d{2}-\d{2}\b", re.IGNORECASE)),
    ("superseded implementation", re.compile(
        r"\b(?:an?\s+|the\s+|this\s+)?"
        r"(?:earlier|previous|prior|old)\s+"
        r"(?:(?:version|draft|implementation|cure|attempt)s?|fix(?:es)?)\b|"
        r"\b(?:an?\s+|the\s+|this\s+)?"
        r"first(?:\s+(?:two|three|\d+))?\s+"
        r"(?:(?:version|draft|implementation|cure|attempt)s?|fix)\b|"
        r"\b(?:an?|the|this|that|these|those|my|our|your|their|its|his|her|"
        r"[a-z0-9_-]+['’]s)\s+first(?:\s+(?:two|three|\d+))?\s+"
        r"fix(?:es)?\b|"
        r"\b(?:used\s+to|formerly)\b",
        re.IGNORECASE)),
)


def _git(root, *args, stdin=None):
    return subprocess.run(("git",) + args, cwd=root, input=stdin,
                          capture_output=True, timeout=60)


def _sibling(name):
    """An installed snapshot neighbour, or None on a broken installation."""
    if __package__:
        try:
            return __import__("helm." + name, fromlist=[name])
        except Exception:                                      # noqa: BLE001
            return None
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, name + ".py")):
        return None
    try:
        mod = __import__(name)
    except Exception:                                          # noqa: BLE001
        return None
    got = os.path.dirname(os.path.abspath(getattr(mod, "__file__", "") or ""))
    return mod if got == here else None


def _root():
    done = _git(os.getcwd(), "rev-parse", "--show-toplevel")
    if done.returncode != 0:
        return None
    return os.fsdecode(done.stdout.strip())


def _path(raw):
    return os.fsdecode(raw).replace(os.sep, "/")


def historical(rel):
    """True only for a declared historical-document path identity."""
    rel = rel.lstrip("./")
    return any(rel == p[:-1] or rel.startswith(p) for p in _HISTORICAL)


def public_bound(rel):
    rel = rel.lstrip("./")
    return not historical(rel) and any(rel.startswith(p) for p in _WORLD)


def _source_kind(rel):
    lower = rel.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith((".js", ".js.part")):
        return "slash"
    if lower.endswith(".css.part"):
        return "slash"
    if lower.endswith(".html.part"):
        return "html"
    return None


def _group(rows):
    """Adjacent (line, prose) rows become one searchable paragraph."""
    groups = []
    for line, text in sorted(rows):
        if groups and line == groups[-1][1] + 1:
            groups[-1][1] = line
            groups[-1][2].append(text)
        else:
            groups.append([line, line, [text]])
    return [(first, "\n".join(parts)) for first, _last, parts in groups]


def python_prose(source):
    """[(first_line, text)] for comments and real Python docstrings."""
    tree = ast.parse(source)
    comments = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comments.append((tok.start[0], tok.string[1:].strip()))

    docs = []
    owners = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef))]
    for owner in owners:
        if not owner.body:
            continue
        expr = owner.body[0]
        if not (isinstance(expr, ast.Expr)
                and isinstance(expr.value, ast.Constant)
                and isinstance(expr.value.value, str)):
            continue
        docs.append((expr.value.lineno, expr.value.value))
    return _group(comments) + docs


def marked_prose(source, kind):
    """Comment paragraphs for source formats without a stdlib parser.

    Only a marker at the first non-whitespace byte opens prose. This narrow
    lexical promise cannot mistake an executable string containing ``//`` or
    ``/*`` for narration.
    """
    rows, block = [], None
    for line, raw in enumerate(source.splitlines(), 1):
        text = raw.lstrip()
        if block:
            end = text.find(block)
            body = text if end < 0 else text[:end]
            rows.append((line, body.lstrip("* ").strip()))
            if end >= 0:
                block = None
            continue
        if kind == "html" and text.startswith("<!--"):
            body = text[4:]
            end = body.find("-->")
            rows.append((line, (body if end < 0 else body[:end]).strip()))
            block = None if end >= 0 else "-->"
        elif text.startswith("//"):
            rows.append((line, text[2:].strip()))
        elif text.startswith("/*"):
            body = text[2:]
            end = body.find("*/")
            rows.append((line, (body if end < 0 else body[:end]).strip()))
            block = None if end >= 0 else "*/"
    return _group(rows)


def _overlaps(first, last, ranges):
    if ranges is None:
        return True
    return any(lo <= last and first <= hi for lo, hi in ranges)


def scan_source(rel, blob, ranges=None, moved=None):
    """[(path, line, category, excerpt)] in the selected source prose.

    `moved` is the line texts this staged set removed from other public-bound
    files. A match whose every physical line is one of them relocated inside
    the boundary and is not this commit's prose. A match spanning even one
    line that did not travel is judged whole, so the exemption can never be
    borrowed by new text sharing a passage with old.
    """
    kind = _source_kind(rel)
    if kind is None or b"\0" in blob[:8192]:
        return []
    source = blob.decode("utf-8")
    physical = source.splitlines()

    def travelled(first, last):
        if not moved or first < 1 or last > len(physical):
            return False
        return all(physical[n - 1] in moved for n in range(first, last + 1))

    chunks = python_prose(source) if kind == "python" else marked_prose(source, kind)
    found, seen = [], set()
    for start, text in chunks:
        for category, rule in _RULES:
            for match in rule.finditer(text):
                first = start + text[:match.start()].count("\n")
                last = start + text[:match.end()].count("\n")
                if not _overlaps(first, last, ranges):
                    continue
                if travelled(first, last):
                    continue
                key = (first, last, category)
                if key in seen:
                    continue
                seen.add(key)
                excerpt = " ".join(match.group(0).split())[:120]
                found.append((rel, first, category, excerpt))
    return sorted(found)


def _index_tree(root):
    done = _git(root, "write-tree")
    return done.stdout.strip() if done.returncode == 0 else None


def _moved_lines(root):
    """Line texts this staged set REMOVES from public-bound files.

    THE EXEMPTION IS PER LINE AND EXACT. A destination line is spared only
    when the same staged set deletes an identical line from a file that was
    ALREADY public-bound, so the bytes are relocating inside the boundary
    rather than entering it. `public_bound` excludes the historical paths, so
    prose leaving ``docs/`` for ``helm/`` is a first publication and stays
    judged -- the promise the rename sentence above already makes.

    NONE MEANS UNKNOWN AND EXEMPTS NOTHING. A diff that could not be read
    must never widen what passes; the caller then judges every added line,
    which is this rung's behaviour without the exemption.
    """
    done = _git(root, "diff", "--cached", "--no-renames", "-U0", "--no-color")
    if done.returncode != 0:
        return None
    moved, rel = set(), None
    for raw in done.stdout.splitlines():
        if raw.startswith(b"--- "):
            old = raw[4:]
            if old == b"/dev/null":
                rel = None
            else:
                rel = _path(old[2:] if old.startswith(b"a/") else old)
        elif raw.startswith(b"+++"):
            continue
        elif raw.startswith(b"-"):
            if rel is not None and public_bound(rel):
                try:
                    moved.add(raw[1:].decode("utf-8"))
                except UnicodeDecodeError:
                    continue
    return moved


def _entered_boundary(status, old, rel):
    if not status or status[0] not in ("R", "C") or old is None:
        return False
    return public_bound(rel) and not public_bound(_path(old))


def scan_staged(root):
    """(findings, error, missing_seam) for the exact captured staged set."""
    cm = _sibling("conflict_marker")
    if cm is None:
        return None, None, "conflict_marker"
    before = _index_tree(root)
    if before is None:
        return None, "the staged index could not be captured", None
    try:
        entries = cm.staged_entries(root)
        blobs = cm._index_blobs(root)
    except Exception as exc:                                   # noqa: BLE001
        return None, "the staged set could not be read (%s)" % exc, None
    moved = _moved_lines(root)
    found = []
    for status, old, raw in entries:
        rel = _path(raw)
        if not public_bound(rel) or _source_kind(rel) is None:
            continue
        mode, oid = blobs.get(raw, (None, None))
        if mode == b"160000":
            continue
        if oid is None:
            return None, "no stage-0 blob for %s" % rel, None
        done = cm._git(root, "cat-file", "blob", oid)
        if done[0] != 0:
            return None, "staged blob unreadable for %s" % rel, None
        try:
            ranges = cm._added_ranges(root, status, old, raw)
            if _entered_boundary(status, old, rel):
                ranges = None
            found += scan_source(rel, done[1], ranges, moved)
        except (SyntaxError, tokenize.TokenError, UnicodeDecodeError,
                IndentationError, ValueError, RuntimeError, OSError,
                subprocess.TimeoutExpired) as exc:
            return None, "staged source at %s could not be scanned (%s)" % (
                rel, exc), None
    if _index_tree(root) != before:
        return None, "the staged index changed while it was being scanned", None
    return sorted(found), None, None


def scan_population(root, ref="HEAD"):
    """All findings in one committed tree, including pre-existing debt."""
    listed = _git(root, "ls-tree", "-rz", "--name-only", ref, "--",
                  "helm", "tests", "docs", "journal")
    if listed.returncode != 0:
        raise RuntimeError("tree population could not be listed")
    found = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = _path(raw)
        if not public_bound(rel) or _source_kind(rel) is None:
            continue
        got = _git(root, "show", ref + ":" + rel)
        if got.returncode != 0:
            raise RuntimeError("tree blob unreadable for %s" % rel)
        try:
            found += scan_source(rel, got.stdout)
        except (SyntaxError, tokenize.TokenError, UnicodeDecodeError,
                IndentationError, ValueError) as exc:
            raise RuntimeError("source at %s could not be parsed (%s)" % (
                rel, exc))
    return sorted(found)


def _print(found):
    for rel, line, category, excerpt in found:
        print("[helm world-prose]   %s:%d — %s: %s" % (
            rel, line, category, excerpt), file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in (["--staged"], ["--all"]):
        print("usage: world_prose_guard.py --staged|--all", file=sys.stderr)
        return 2
    root = _root()
    if root is None:
        print("[helm world-prose] REFUSED: not inside a git work tree",
              file=sys.stderr)
        return 2
    if argv == ["--all"]:
        try:
            found = scan_population(root)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            print("[helm world-prose] REFUSED: population scan failed — %s" % exc,
                  file=sys.stderr)
            return 2
    else:
        found, error, missing = scan_staged(root)
        if missing:
            print("[helm world-prose] WARNING: hardened scanner seam missing: "
                  "%s — staged prose scan SKIPPED; reinstall the guard" % missing,
                  file=sys.stderr)
            return 0
        if error:
            print("[helm world-prose] REFUSED: staged prose scan is UNKNOWN — %s"
                  % error, file=sys.stderr)
            return 2
    if not found:
        return 0
    print("[helm world-prose] REFUSED: %d passage(s) matching the bounded "
          "internal-history vocabulary in public-bound source prose:"
          % len(found), file=sys.stderr)
    _print(found)
    print("[helm world-prose] state the current property and failure mode; keep "
          "review chronology in docs/ or the journal. One-commit owner override: "
          "HELM_WORLD_PROSE_SKIP=1", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
