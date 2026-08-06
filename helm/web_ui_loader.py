"""Assemble Helm's self-contained browser page from ordered source sections.

The browser still receives one HTML document with one ``<style>`` and one
classic ``<script>`` scope.  The split is only a source-ownership boundary:
the manifest and every fragment are read on each request, preserving the old
``web_ui.html`` hot-template lifetime while Python remains import-frozen.
"""
import os


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(PACKAGE_DIR, "web_ui")
MANIFEST_PATH = os.path.join(UI_DIR, "manifest.txt")


def fragment_paths(manifest_path=MANIFEST_PATH, root=None):
    """Return the manifest's validated, ordered fragment paths.

    Entries are exact POSIX-relative paths.  Refusing whitespace, duplicates,
    traversal and symlink escapes keeps the manifest an inventory rather than a
    second filesystem language.  It is deliberately read every call: adding or
    reordering a section must become live with the same next-request semantics
    the former monolith had.
    """
    root = os.path.abspath(root or os.path.dirname(manifest_path))
    real_root = os.path.realpath(root)
    paths = []
    seen = set()
    with open(manifest_path, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            entry = raw.rstrip("\n")
            if not entry or entry.startswith("#"):
                continue
            if entry != entry.strip() or "\\" in entry:
                raise ValueError("web UI manifest line %d is not an exact path" % number)
            parts = entry.split("/")
            if os.path.isabs(entry) or any(p in ("", ".", "..") for p in parts):
                raise ValueError("web UI manifest line %d escapes its root" % number)
            if entry in seen:
                raise ValueError("web UI manifest repeats %s" % entry)
            seen.add(entry)
            path = os.path.abspath(os.path.join(root, *parts))
            try:
                inside = os.path.commonpath((real_root, os.path.realpath(path))) \
                    == real_root
            except ValueError:
                inside = False
            if not inside:
                raise ValueError("web UI manifest line %d escapes its root" % number)
            paths.append(path)
    if not paths:
        raise ValueError("web UI manifest has no fragments")
    return tuple(paths)


def read_bytes(manifest_path=MANIFEST_PATH, root=None):
    """Return the exact page bytes; no separator or newline is invented."""
    parts = []
    for path in fragment_paths(manifest_path, root):
        with open(path, "rb") as handle:
            parts.append(handle.read())
    return b"".join(parts)


def read_text(manifest_path=MANIFEST_PATH, root=None):
    """The assembled source for tests that extract production JavaScript."""
    return read_bytes(manifest_path, root).decode("utf-8")
