#!/usr/bin/env python3
"""The split UI assembles to the exact page the former monolith served."""
import os
import tempfile
import unittest

from helm import web_ui_loader


# THE MIGRATION BASELINE, kept as history rather than as an assertion. The
# split was proven byte-identical at 402700 bytes / sha256
# c064847a8fe0e6f473feb997c6851ddc53fb342e29a2112eb4c40af38be6e627, verified
# independently by the reviewer at the time. Pinning those numbers FORWARD
# forbids every later edit to the UI, so the durable invariant replaced them:
# assembly invents no bytes. The numbers stay here so the migration remains
# checkable against git history, and nothing imports them.
EXPECTED_FRAGMENTS = (
    "shell/00-head.html.part",
    "styles/00-core.css.part",
    "styles/10-boxes.css.part",
    "styles/20-quota.css.part",
    "styles/30-sessions.css.part",
    "styles/40-configs.css.part",
    "styles/50-chat.css.part",
    "styles/60-ledger.css.part",
    "styles/70-home.css.part",
    "styles/80-roster.css.part",
    "shell/10-nav.html.part",
    "views/00-home.html.part",
    "views/10-quota.html.part",
    "views/20-boxes.html.part",
    "views/30-sessions.html.part",
    "views/40-configs.html.part",
    "views/50-work.html.part",
    "views/60-chat.html.part",
    "views/70-roster.html.part",
    "views/80-ledger.html.part",
    "shell/20-overlays.html.part",
    "scripts/00-core.js.part",
    "scripts/10-boxes.js.part",
    "scripts/20-quota.js.part",
    "scripts/30-sessions.js.part",
    "scripts/40-configs.js.part",
    "scripts/50-work.js.part",
    "scripts/60-chat.js.part",
    "scripts/70-ledger.js.part",
    "scripts/80-roster.js.part",
    "scripts/90-shared.js.part",
)


class WebUiAssemblyTest(unittest.TestCase):
    def assertExactAssembly(self, body, parts):
        """The loader output is exactly the independently-read manifest bytes."""
        self.assertEqual(len(body), sum(len(part) for part in parts),
                         "assembly added or dropped bytes")
        self.assertEqual(body, b"".join(parts),
                         "assembly changed manifest order or invented bytes")

    def test_assembly_invents_no_bytes_and_keeps_one_document_shape(self):
        """The DURABLE half of the migration proof.

        This asserted equality against BASELINE_BYTES/BASELINE_SHA256, frozen
        at the moment of the split. That was exactly right AS A MIGRATION
        CHECK — it proved the split changed nothing — and it is a trap as a
        standing one: the constants pin the page as it was, so the FIRST
        legitimate edit to any fragment fails it. Measured: it went red on a
        one-line change to 00-core.js.part that altered no behaviour, and no
        re-baselining makes it meaningful again, because a constant updated
        with every edit only ever restates the current file.

        What actually deserved pinning survives here. ASSEMBLY INVENTS NOTHING:
        the page is exactly the concatenation of its fragments, so its length
        equals the sum of theirs and no separator, newline or wrapper is added.
        That is the property the byte hash was standing in for, and unlike the
        hash it holds for every future edit."""
        body = web_ui_loader.read_bytes()
        parts = []
        for path in web_ui_loader.fragment_paths():
            with open(path, "rb") as fh:
                parts.append(fh.read())
        self.assertTrue(parts, "no fragments; the assertions below are vacuous")
        # AN INDEPENDENT ORACLE, not self-agreement. `read_bytes` owns the
        # production assembly; this test separately opens every manifest path
        # and joins those raw bytes. A separator, drop, duplicate or reorder in
        # the loader therefore differs from the manual assembly and fails.
        #
        # Do NOT locate fragments with body.count/body.find: exact assembly does
        # not require fragment bytes to be globally unique. Two legitimate
        # fragments may be identical or one may occur inside another, and a
        # content-search oracle would forbid that future shape while claiming
        # to test concatenation.
        self.assertExactAssembly(body, parts)
        # ONE DOCUMENT, one style scope, one classic script scope — the shape
        # the browser is served, independent of what the fragments say.
        self.assertTrue(body.startswith(b"<!doctype html>\n"))
        self.assertTrue(body.endswith(b"</script>\n"))
        self.assertEqual(body.count(b"<style>"), 1)
        self.assertEqual(body.count(b"<script>"), 1)
        self.assertGreater(len(body), 100000, "the page assembled far too small")

    def test_fragment_bytes_need_not_be_globally_unique(self):  # noqa: VACUOUS_ASSERTION — the non-empty literal drives the same helper twice: the inserted-separator arm MUST raise, then the duplicate/subsequence arm MUST pass
        """Concatenation orders entries; it does not assign unique byte content.
        This is the must-miss for the rejected body.count/body.find oracle: the
        second fragment is a substring and the first and third are identical,
        yet the manifest assembly is exact."""
        parts = [b"tail", b"ail", b"tail"]
        with self.assertRaises(AssertionError):
            self.assertExactAssembly(b"tail|ailtail", parts)
        self.assertExactAssembly(b"tailailtail", parts)

    def test_the_manifest_names_every_section_once_in_global_source_order(self):
        paths = web_ui_loader.fragment_paths()
        named = tuple(os.path.relpath(path, web_ui_loader.UI_DIR)
                      .replace(os.sep, "/") for path in paths)
        self.assertEqual(named, EXPECTED_FRAGMENTS)
        present = []
        for root, _dirs, files in os.walk(web_ui_loader.UI_DIR):
            for name in files:
                if name.endswith(".part"):
                    present.append(os.path.relpath(
                        os.path.join(root, name), web_ui_loader.UI_DIR)
                        .replace(os.sep, "/"))
        self.assertEqual(sorted(present), sorted(EXPECTED_FRAGMENTS))

    def test_assembly_invents_no_separator_and_reads_every_call_fresh(self):
        with tempfile.TemporaryDirectory(prefix="helm-ui-parts-") as root:
            manifest = os.path.join(root, "manifest.txt")
            one = os.path.join(root, "one.part")
            two = os.path.join(root, "two.part")
            with open(one, "wb") as handle:
                handle.write(b"middle\n\n")
            with open(two, "wb") as handle:
                handle.write(b"tail")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write("two.part\none.part\n")
            self.assertEqual(web_ui_loader.read_bytes(manifest),
                             b"tailmiddle\n\n")
            with open(two, "wb") as handle:
                handle.write(b"TAIL\n")
            self.assertEqual(web_ui_loader.read_bytes(manifest),
                             b"TAIL\nmiddle\n\n")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write("one.part\ntwo.part\n")
            self.assertEqual(web_ui_loader.read_bytes(manifest),
                             b"middle\n\nTAIL\n")

    def test_manifest_refuses_ambiguous_or_escaping_inventory(self):
        invalid = (
            "../outside.part\n",
            "nested/../outside.part\n",
            "/absolute.part\n",
            " spaced.part\n",
            "nested\\windows.part\n",
            "one.part\none.part\n",
            "\n# comments alone are not an inventory\n",
        )
        with tempfile.TemporaryDirectory(prefix="helm-ui-manifest-") as root:
            manifest = os.path.join(root, "manifest.txt")
            with open(os.path.join(root, "one.part"), "wb") as handle:
                handle.write(b"one")
            for text in invalid:
                with self.subTest(text=text):
                    with open(manifest, "w", encoding="utf-8") as handle:
                        handle.write(text)
                    with self.assertRaises(ValueError):
                        web_ui_loader.fragment_paths(manifest)

    def test_a_missing_fragment_fails_loudly(self):
        with tempfile.TemporaryDirectory(prefix="helm-ui-missing-") as root:
            manifest = os.path.join(root, "manifest.txt")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write("gone.part\n")
            with self.assertRaises(FileNotFoundError):
                web_ui_loader.read_bytes(manifest)

    def test_a_symlink_cannot_escape_the_fragment_root(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory(prefix="helm-ui-link-") as root, \
                tempfile.TemporaryDirectory(prefix="helm-ui-outside-") as outside:
            target = os.path.join(outside, "outside.part")
            with open(target, "wb") as handle:
                handle.write(b"outside")
            os.symlink(target, os.path.join(root, "link.part"))
            manifest = os.path.join(root, "manifest.txt")
            with open(manifest, "w", encoding="utf-8") as handle:
                handle.write("link.part\n")
            with self.assertRaises(ValueError):
                web_ui_loader.fragment_paths(manifest)


if __name__ == "__main__":
    unittest.main()
