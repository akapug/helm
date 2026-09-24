"""Assemble Helm's self-contained browser page from ordered source sections.

The browser still receives one HTML document with one ``<style>`` and one
classic ``<script>`` scope.  The split is only a source-ownership boundary:
the manifest and every fragment are read on each request, preserving the old
``web_ui.html`` hot-template lifetime while Python remains import-frozen.
"""
import hashlib
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


def build_id(manifest_path=MANIFEST_PATH, root=None):
    """A short digest of the page this server would serve RIGHT NOW.

    THE TAB OUTLIVES THE BUILD. The owner keeps this console open for hours
    while the tree under it lands change after change; the page is assembled
    fresh per request and sent no-store, so the SERVER is always current and
    the tab is whatever was assembled when he opened it. Nothing told him the
    two had diverged — he read a card for hours that a newer build had already
    moved, and reported what an old page showed. This is the fact a page needs
    to notice that, and the content is the identity: a rebuild that changes
    nothing changes nothing here, and a revert back to an earlier page reads as
    that page rather than as a third one.

    It is derived, never stored, and computed from the same bytes `_ui` serves
    — before the per-process token is templated in, which is not part of the
    build and would otherwise mint a new id on every restart."""
    return hashlib.sha256(read_bytes(manifest_path, root)).hexdigest()[:12]
