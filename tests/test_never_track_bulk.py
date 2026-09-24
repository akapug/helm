#!/usr/bin/env python3
"""The bulk-data leg of the never-track scanner — the leak no needle and no
enumerated path can see.

THE CLASS. A customer's content export, a database dump, a browser-session
snapshot, an archive, a subscriber list: data that is not the project's
source. It is nobody's needle (the people in it are not the owner), it sits
at no enumerated path (it lands wherever a tool wrote it), and once it is in
history the cure is a rewrite of a shared remote and a fresh clone for every
collaborator. So the leg judges SHAPE with no configuration at all: a blob
larger than source ever is, content that declares itself an export or a dump
or an archive, or a text carrying a crowd of e-mail addresses.

THE SPLIT the needle scan already makes is kept: what THIS commit adds is a
refusal; what HEAD already carries is a note naming the scrub it needs. The
override is a tracked declaration in .gitattributes, so the decision to keep
a large or export-shaped file is in the diff a reviewer reads.

Hermetic: scratch repos, synthetic content, no needles configured (the leg
must work with nothing configured — that is its whole point).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import nevertrack  # noqa: E402

WXR = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
       b'<rss version="2.0" xmlns:wp="http://wordpress.org/export/1.2/">\n'
       b'<channel><title>Example</title><wp:wxr_version>1.2</wp:wxr_version>\n'
       b'<item><title>Hello</title></item></channel></rss>\n')
SQL_DUMP = (b"-- MySQL dump 10.13  Distrib 8.0\n--\n"
            b"CREATE TABLE `wp_users` (`ID` bigint);\n"
            b"INSERT INTO `wp_users` VALUES (1);\n")
ZIP_MAGIC = b"PK\x03\x04" + b"\0" * 64


def addresses(n, domain="corp-%d.io"):
    """n distinct addresses. The default domain is NOT reserved, so these are
    what the bulk leg must count; it is built at run time so this file holds
    no literal address. Pass a reserved `domain` for the synthetic kind."""
    return "\n".join(("person%03d@" + domain) % (i, i % 7)
                     for i in range(n)).encode() + b"\n"


class BulkLegBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-bulk-")
        self.prior = {k: os.environ.get(k) for k in
                      ("HELM_PRIVATE_NEEDLES", "GIT_CONFIG_GLOBAL",
                       "GIT_CONFIG_SYSTEM")}
        needles = os.path.join(self.tmp, "needles.txt")
        open(needles, "w").close()          # NOTHING configured, on purpose
        os.environ["HELM_PRIVATE_NEEDLES"] = needles
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for cmd in (("git", "init", "-q", "-b", "main"),
                    ("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            self.sh(*cmd)
        self.write("README", b"seed\n")
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-q", "-m", "seed")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args):
        p = subprocess.run(list(args), cwd=self.root, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def write(self, rel, blob):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(blob)

    def stage(self, rel, blob):
        self.write(rel, blob)
        self.sh("git", "add", "--", rel)

    def commit_unscanned(self, rel, blob):
        """Land content in HEAD the way a leak already in history got there:
        no hook, no scan — the state the leg must report and not block."""
        self.stage(rel, blob)
        self.sh("git", "commit", "-q", "-m", "landed")

    def scan(self):
        return nevertrack.scan_staged(self.root)


class ShapesAreRefused(BulkLegBase):
    def test_a_blob_over_the_ceiling_is_refused_and_names_the_ceiling(self):
        self.stage("dump/big.bin", os.urandom(nevertrack.BULK_CEILING + 1))
        violations, _notes = self.scan()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("ceiling", violations[0])
        self.assertIn("ADDED BY THIS COMMIT", violations[0])

    def test_a_blob_at_the_ceiling_passes_and_one_byte_more_does_not(self):
        self.stage("fixture.bin", b"a" * nevertrack.BULK_CEILING)
        self.assertEqual(self.scan()[0], [])
        self.stage("fixture.bin", b"a" * (nevertrack.BULK_CEILING + 1))
        self.assertEqual(len(self.scan()[0]), 1)     # positive control

    def test_a_tiny_wordpress_export_is_refused_by_its_content(self):
        self.stage("uploads/site.txt", WXR)     # renamed .txt: still an export
        violations, _ = self.scan()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("WordPress export", violations[0])

    def test_a_database_dump_is_refused_by_its_content(self):
        self.stage("backup.sql", SQL_DUMP)
        violations, _ = self.scan()
        self.assertTrue(any("database dump" in v for v in violations), violations)

    def test_an_archive_is_refused_by_its_magic(self):
        self.stage("export.zip", ZIP_MAGIC)
        violations, _ = self.scan()
        self.assertTrue(any("archive" in v for v in violations), violations)

    def test_a_browser_session_snapshot_is_refused_by_its_path(self):
        self.stage(".playwright-mcp/page.yml", b"- role: document\n")
        violations, _ = self.scan()
        self.assertTrue(any("snapshot" in v for v in violations), violations)

    def test_a_crowd_of_addresses_is_refused_and_a_few_are_not(self):
        self.stage("docs/authors.md", addresses(nevertrack.BULK_ADDRESSES))
        violations, _ = self.scan()
        self.assertTrue(any("e-mail addresses" in v for v in violations),
                        violations)
        self.sh("git", "reset", "-q")
        self.stage("docs/authors.md", addresses(3))
        self.assertEqual(self.scan()[0], [])
        self.assertEqual(nevertrack.address_count(addresses(3)), 3)   # the scan saw them

    def test_the_address_count_is_distinct_and_case_folded(self):
        text = b"\n".join([b"Same@Example.test", b"same@example.test"] * 40)
        self.assertEqual(nevertrack.address_count(text), 1)

    def test_an_ordinary_xml_config_is_not_an_export(self):
        self.stage("pom.xml", b'<?xml version="1.0"?><project><name>x</name></project>')
        self.assertEqual(self.scan()[0], [])
        self.stage("pom.xml", WXR)                    # positive control: same path, export content
        self.assertEqual(len(self.scan()[0]), 1)


class HistoryIsReportedNotBlocked(BulkLegBase):
    def test_a_large_blob_already_in_head_is_a_note_not_a_refusal(self):
        big = os.urandom(nevertrack.BULK_CEILING + 1)
        self.commit_unscanned("legacy/export.bin", big)
        self.stage("legacy/export.bin", big + b"x")     # touched, still big
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("legacy/export.bin" in n and "ALREADY IN HEAD" in n
                            for n in notes), notes)

    def test_growing_a_small_head_file_past_the_ceiling_is_the_leak(self):
        self.commit_unscanned("data.bin", b"small\n")
        self.stage("data.bin", os.urandom(nevertrack.BULK_CEILING + 1))
        violations, _ = self.scan()
        self.assertTrue(any("data.bin" in v and "ceiling" in v
                            for v in violations), violations)

    def test_an_export_already_in_head_is_a_note_when_touched(self):
        self.commit_unscanned("site.wxr", WXR)
        self.stage("site.wxr", WXR + b"<!-- edited -->\n")
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("site.wxr" in n and "export" in n for n in notes),
                        notes)

    def test_a_deletion_is_nothing_to_scan_but_a_re_add_is(self):
        big = os.urandom(nevertrack.BULK_CEILING + 1)
        self.commit_unscanned("gone.bin", big)
        self.sh("git", "rm", "-q", "--", "gone.bin")
        self.assertEqual(self.scan()[0], [])
        self.sh("git", "commit", "-q", "-m", "removed")
        self.stage("gone.bin", big)                   # positive control: back as a NEW path
        self.assertEqual(len(self.scan()[0]), 1)


class TheOverrideIsTracked(BulkLegBase):
    def test_a_gitattributes_declaration_turns_the_refusal_into_a_note(self):
        self.stage(".gitattributes", b"fixtures/golden.wxr helm-bulk=ok\n")
        self.stage("fixtures/golden.wxr", WXR)
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("fixtures/golden.wxr" in n and "helm-bulk=ok" in n
                            for n in notes), notes)

    def test_the_declaration_is_per_path_not_global(self):
        self.stage(".gitattributes", b"fixtures/golden.wxr helm-bulk=ok\n")
        self.stage("uploads/customer.wxr", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                        violations)


class TheRefusalTeachesTheRemedy(BulkLegBase):
    def test_main_prints_the_scrub_law_on_a_bulk_refusal(self):
        self.stage("customer.wxr", WXR)
        p = subprocess.run([sys.executable, nevertrack.__file__, "--staged"],
                           cwd=self.root, capture_output=True, text=True,
                           timeout=60, env=dict(os.environ))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("untracking is NOT a scrub", p.stderr)
        self.assertIn("helm-bulk=ok", p.stderr)
        self.assertIn("customer.wxr", p.stderr)


class TheDeclarationMustBeTracked(BulkLegBase):
    """The exemption is honoured only from a STAGED .gitattributes — the
    place a reviewer reads. git also consults an unstaged edit of that file,
    `.git/info/attributes` and a global attributes file, and none of those
    is in the diff, so none of them may exempt anything."""

    def test_an_unstaged_gitattributes_edit_does_not_exempt(self):
        self.write(".gitattributes", b"uploads/customer.wxr helm-bulk=ok\n")   # NOT staged
        self.stage("uploads/customer.wxr", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                        violations)

    def test_info_attributes_does_not_exempt(self):
        info = os.path.join(self.root, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w") as f:
            f.write("uploads/customer.wxr helm-bulk=ok\n")
        self.stage("uploads/customer.wxr", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                        violations)

    def test_a_global_attributes_file_does_not_exempt(self):
        g = os.path.join(self.tmp, "global-attributes")
        with open(g, "w") as f:
            f.write("uploads/customer.wxr helm-bulk=ok\n")
        self.sh("git", "config", "core.attributesFile", g)
        self.stage("uploads/customer.wxr", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                        violations)

    def test_an_unrelated_staged_rule_does_not_lend_its_authority(self):
        """The exact counter-case to a presence check: a staged rule for SOME
        OTHER path carries the attribute name, while the rule that actually
        wins for this path lives in info/attributes. The staged authority
        must decide on its own, and it says nothing about this path."""
        self.stage(".gitattributes", b"fixtures/other.bin helm-bulk=ok\n")
        info = os.path.join(self.root, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w") as f:
            f.write("uploads/customer.wxr helm-bulk=ok\n")
        self.stage("uploads/customer.wxr", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                        violations)
        self.stage(".gitattributes", b"uploads/customer.wxr helm-bulk=ok\n")   # positive control
        self.assertEqual(self.scan()[0], [])

    def test_an_explicit_GIT_COMMON_DIR_does_not_reach_the_sandbox(self):
        """Repository-selection authority in the environment: with
        GIT_COMMON_DIR set (to the repo's own common dir, a valid state) a
        sandbox that inherited it would read the real info/attributes."""
        self.stage(".gitattributes", b"fixtures/other.bin helm-bulk=ok\n")
        info = os.path.join(self.root, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w") as f:
            f.write("uploads/customer.wxr helm-bulk=ok\n")
        self.stage("uploads/customer.wxr", WXR)
        prior = os.environ.get("GIT_COMMON_DIR")
        os.environ["GIT_COMMON_DIR"] = os.path.join(self.root, ".git")
        try:
            violations, _ = self.scan()
            self.assertTrue(any("uploads/customer.wxr" in v for v in violations),
                            violations)
            self.stage(".gitattributes", b"uploads/customer.wxr helm-bulk=ok\n")
            self.assertEqual(self.scan()[0], [])        # positive control
        finally:
            if prior is None:
                os.environ.pop("GIT_COMMON_DIR", None)
            else:
                os.environ["GIT_COMMON_DIR"] = prior

    def test_a_sha256_repository_can_still_declare_an_exemption(self):
        """The sandbox carries the real repo's object format; without it the
        index is unreadable and a legitimate fixture would be refused."""
        root = os.path.join(self.tmp, "sha256")
        os.makedirs(root)
        p = subprocess.run(["git", "init", "-q", "-b", "main",
                            "--object-format=sha256"], cwd=root,
                           capture_output=True, text=True)
        if p.returncode != 0:
            self.skipTest("this git cannot init a sha256 repository")
        for cmd in (("git", "config", "user.email", "t@example.com"),
                    ("git", "config", "user.name", "t")):
            subprocess.run(cmd, cwd=root, check=True)
        os.makedirs(os.path.join(root, "fixtures"))
        with open(os.path.join(root, ".gitattributes"), "wb") as f:
            f.write(b"fixtures/golden.wxr helm-bulk=ok\n")
        with open(os.path.join(root, "fixtures", "golden.wxr"), "wb") as f:
            f.write(WXR)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        violations, notes = nevertrack.scan_staged(root)
        self.assertEqual(violations, [], notes)
        self.assertFalse(any("could not decide" in n for n in notes), notes)
        self.assertTrue(any("fixtures/golden.wxr" in n and "helm-bulk=ok" in n
                            for n in notes), notes)

    def test_a_staged_declaration_in_a_subdirectory_exempts_its_subtree(self):
        self.stage("fixtures/.gitattributes", b"golden.wxr helm-bulk=ok\n")
        self.stage("fixtures/golden.wxr", WXR)
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("fixtures/golden.wxr" in n for n in notes), notes)


class TheShapeIsJudgedAgainstHead(BulkLegBase):
    def test_an_export_written_over_an_ordinary_file_is_added(self):
        """A pathname already in HEAD does not launder the content: HEAD's
        site.xml was an ordinary config file, this commit makes it an export."""
        self.commit_unscanned("config/site.xml",
                              b'<?xml version="1.0"?><site><name>x</name></site>\n')
        self.stage("config/site.xml", WXR)
        violations, _ = self.scan()
        self.assertTrue(any("config/site.xml" in v and "export" in v
                            for v in violations), violations)

    def test_an_export_that_head_already_had_stays_a_note(self):
        self.commit_unscanned("legacy.xml", WXR)
        self.stage("legacy.xml", WXR + b"<!-- touched -->\n")
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("legacy.xml" in n for n in notes), notes)


class ThePathIsScannedBeforeTheBlob(BulkLegBase):
    def test_a_gitlink_at_a_needle_path_is_still_a_path_hit(self):
        """A submodule entry has no blob to read; its NAME is still scanned."""
        needles = os.path.join(self.tmp, "needles2.txt")
        with open(needles, "w") as f:
            f.write("zz-synthetic-omega\n")
        os.environ["HELM_PRIVATE_NEEDLES"] = needles
        sha = self.sh("git", "rev-parse", "HEAD").strip()
        self.sh("git", "update-index", "--add", "--cacheinfo",
                "160000,%s,vendor/zz-synthetic-omega" % sha)
        violations, notes = self.scan()
        self.assertTrue(any("vendor/zz-synthetic-omega" in v and "PATH" in v
                            for v in violations), violations)
        self.assertTrue(any("not a readable blob" in n for n in notes), notes)


class TheAddressThresholdIsPerCommit(BulkLegBase):
    def test_a_list_split_over_two_files_is_one_list(self):
        half = nevertrack.BULK_ADDRESSES // 2
        self.stage("a.md", addresses(half))
        self.stage("b.md", "\n".join("other%03d@corp-%d.io" % (i, i % 5)
                                    for i in range(half)).encode())
        violations, _ = self.scan()
        hits = [v for v in violations if "e-mail addresses" in v]
        self.assertEqual(len(hits), 1, violations)
        self.assertIn("a.md", hits[0]); self.assertIn("b.md", hits[0])
        self.assertIn("across 2 file(s)", hits[0])

    def test_duplicates_across_files_do_not_overcount(self):
        n = nevertrack.BULK_ADDRESSES - 1
        self.stage("a.md", addresses(n))
        self.stage("b.md", addresses(n))          # the SAME addresses again
        self.assertEqual([v for v in self.scan()[0] if "e-mail" in v], [])
        self.stage("c.md", ("one-more@corp-%d.io\n" % 9).encode())  # positive control: +1 distinct
        self.assertEqual(len([v for v in self.scan()[0] if "e-mail" in v]), 1)

    def test_a_declared_file_contributes_no_addresses(self):
        self.stage(".gitattributes", b"fixtures/people.csv helm-bulk=ok\n")
        self.stage("fixtures/people.csv", addresses(nevertrack.BULK_ADDRESSES))
        self.assertEqual([v for v in self.scan()[0] if "e-mail" in v], [])


class ReservedDomainsAreNotAnAddressList(BulkLegBase):
    """RFC 2606 / RFC 6761 reserve example.com/.net/.org and the .example,
    .test, .invalid and .localhost names so that tests can write an address
    that reaches nobody. A commit full of them is a fixture doing what the
    leak law asks, not a client export, so they do not count toward
    BULK_ADDRESSES. Every arm pairs the pass with the refusal it must keep."""

    RESERVED = ("example.com", "example.net", "example.org",
                "mail.example.com", "x.example", "y.test", "z.invalid",
                "host.localhost")

    def reserved(self, n):
        return "\n".join("person%03d@%s" % (i, self.RESERVED[i % len(self.RESERVED)])
                         for i in range(n)).encode() + b"\n"

    def crowd(self):
        return [v for v in self.scan()[0] if "e-mail addresses" in v]

    def test_a_crowd_of_reserved_addresses_passes(self):
        n = nevertrack.BULK_ADDRESSES + 5
        self.stage("tests/fixtures.py", self.reserved(n))
        self.assertEqual(self.crowd(), [])
        # the scan SAW every one of them; they are simply not a crowd
        self.assertEqual(nevertrack.address_count(self.reserved(n)), n)

    def test_the_same_crowd_at_real_domains_is_still_refused(self):
        n = nevertrack.BULK_ADDRESSES + 5
        self.stage("tests/fixtures.py", addresses(n))
        hits = self.crowd()
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("adds %d distinct" % n, hits[0])

    def test_a_mixed_commit_counts_only_the_real_ones(self):
        real = nevertrack.BULK_ADDRESSES - 1
        self.stage("a.md", addresses(real))
        self.stage("b.md", self.reserved(40))
        self.assertEqual(self.crowd(), [], "19 real + 40 reserved is 19")
        self.stage("c.md", ("one-more@corp-%d.io\n" % 9).encode())
        hits = self.crowd()
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("adds %d distinct" % nevertrack.BULK_ADDRESSES, hits[0])

    def test_look_alike_domains_are_not_reserved(self):
        look = ("myexample.com", "example.com.co", "example.co", "test.io",
                "example-test.io", "invalid.org", "notexample.org")
        text = "\n".join("person%03d@%s" % (i, look[i % len(look)])
                         for i in range(nevertrack.BULK_ADDRESSES)).encode()
        self.stage("a.md", text)
        self.assertEqual(len(self.crowd()), 1)
        for d in look:
            self.assertFalse(nevertrack.reserved_address(("a@" + d).encode()), d)
        for d in self.RESERVED + ("EXAMPLE.COM", "x.Example", "y.test."):
            self.assertTrue(nevertrack.reserved_address(("a@" + d).encode()), d)


class AQuotingSourceFileIsDeclaredNotInferred(BulkLegBase):
    """A self-declaration marker fires wherever it sits: quote characters
    occur inside the raw formats themselves (an XML attribute, an SQL
    comment), so no scanner can tell a parser test that quotes a header
    from an export wearing a source suffix. The ONE door is the STAGED
    declaration `<path> helm-bulk=quotes`,
    which a reviewer sees in the diff, and it waives ONLY the two
    self-declaration shapes: the ceiling, the magic bytes and the address
    count still judge the declared file."""

    FIXTURE = (b'const WXR = `<rss xmlns:wp="http://wordpress.org/export/1.2/">'
               b'<wp:wxr_version>1.2</wp:wxr_version></rss>`;\n')

    def test_a_test_file_quoting_the_wxr_header_is_refused_until_declared(self):
        self.stage("test/import/fixtures.test.ts", self.FIXTURE)
        self.assertTrue(any("fixtures.test.ts" in v and "export" in v for v in self.scan()[0]))
        self.stage(".gitattributes", b"test/import/fixtures.test.ts helm-bulk=quotes\n")
        violations, notes = self.scan()
        self.assertEqual(violations, [])
        self.assertTrue(any("fixtures.test.ts" in n and "helm-bulk=quotes" in n for n in notes), notes)

    def test_quotes_waives_only_the_self_declaration(self):
        self.stage(".gitattributes", b"test/** helm-bulk=quotes\n")
        self.stage("test/big.ts", os.urandom(nevertrack.BULK_CEILING + 1))
        self.assertTrue(any("big.ts" in v and "ceiling" in v for v in self.scan()[0]))
        self.sh("git", "reset", "-q")
        self.stage(".gitattributes", b"test/** helm-bulk=quotes\n")
        self.stage("test/vendor.ts", ZIP_MAGIC)
        self.assertTrue(any("vendor.ts" in v and "archive" in v for v in self.scan()[0]))
        self.sh("git", "reset", "-q")
        self.stage(".gitattributes", b"test/** helm-bulk=quotes\n")
        self.stage("test/people.ts", self.FIXTURE + addresses(nevertrack.BULK_ADDRESSES))
        violations, _ = self.scan()
        self.assertTrue(any("e-mail" in v for v in violations), violations)
        self.assertFalse(any("export" in v for v in violations), violations)
        self.sh("git", "reset", "-q")
        self.stage(".gitattributes", b"test/** helm-bulk=ok\n")            # ok waives everything
        self.stage("test/people.ts", self.FIXTURE + addresses(nevertrack.BULK_ADDRESSES))
        self.assertEqual(self.scan()[0], [])

    def test_quotes_does_not_hide_a_shape_it_cannot_waive(self):
        """The r5 witness: a browser snapshot whose text quotes the WXR
        marker, under a quotes declaration. The self-declaration is waived;
        the snapshot is still a snapshot, and the archive magic under a
        header is still an archive."""
        self.stage(".gitattributes", b".playwright-mcp/** helm-bulk=quotes\ntest/** helm-bulk=quotes\n")
        self.stage(".playwright-mcp/page.yml", b'- text: "wp:wxr_version"\n')
        violations, notes = self.scan()
        self.assertTrue(any("page.yml" in v and "snapshot" in v for v in violations), (violations, notes))
        self.assertFalse(any("export" in v for v in violations), violations)
        self.sh("git", "reset", "-q")
        self.stage(".gitattributes", b"test/** helm-bulk=quotes\n")
        self.stage("test/blob.ts", b"BZh" + b"\0" * 32 + b"wp:wxr_version")
        self.assertTrue(any("blob.ts" in v and "archive" in v for v in self.scan()[0]))

    def test_an_unknown_value_declares_nothing_and_says_so(self):
        self.stage(".gitattributes", b"test/f.ts helm-bulk=maybe\n")
        self.stage("test/f.ts", self.FIXTURE)
        violations, notes = self.scan()
        self.assertTrue(any("f.ts" in v and "export" in v for v in violations))
        self.assertTrue(any("helm-bulk=maybe" in n and "not a value" in n for n in notes), notes)

    def test_no_suffix_and_no_prolog_hides_a_raw_export(self):
        """Every witness against INFERRING quotation from the bytes: a
        renamed export; one behind a comment, a DOCTYPE, a DOCTYPE with an
        internal subset, a processing instruction; one whose only marker is
        inside an XML attribute quote (alternate namespace prefix); a dump
        opening with PRAGMA, PRAGMA split by a newline, SAVEPOINT, a banner
        inside an SQL comment with paired quotes, a real mysqldump head; a
        serialized export inside JSON or YAML; a README replaced by an
        export. No suffix and no byte shape earns any of them anything."""
        rss = b'<rss xmlns:wp="http://wordpress.org/export/1.2/"><channel><wp:wxr_version>1.2</wp:wxr_version></channel></rss>\n'
        self.stage("uploads/customer.ts", WXR)                         # unconditional positive control
        self.assertTrue(any("uploads/customer.ts" in v and "export" in v for v in self.scan()[0]))
        for rel, blob in (
                ("uploads/customer.ts", WXR),
                ("uploads/customer.ts", b"<!-- export -->\n" + rss),
                ("uploads/customer.ts", b"<!DOCTYPE rss>\n" + rss),
                ("uploads/customer.ts", b'<!DOCTYPE rss [<!ENTITY label "Example">]>\n' + rss),
                ("uploads/customer.ts", b'<?xml version="1.0"?>\n<?pi x?>\n' + rss),
                ("uploads/customer.ts", b'<rss xmlns:w="http://wordpress.org/export/1.2/"><channel><w:wxr_version>1.2</w:wxr_version></channel></rss>\n'),
                ("uploads/customer.ts", b"const W = `" + rss + b"x" * nevertrack._SNIFF),
                ("exports/site.json", b'{"export": "' + WXR.replace(b'"', b'\\"').replace(b"\n", b"") + b'"}\n'),
                ("exports/site.yaml", b"export: |\n  " + WXR.replace(b"\n", b"\n  ") + b"\n"),
                ("README.md", WXR), ("uploads/export", WXR), ("uploads/export.txt", WXR)):
            self.sh("git", "reset", "-q")
            self.stage(rel, blob)
            violations, _ = self.scan()
            self.assertTrue(any(rel in v and "export" in v for v in violations), (rel, blob[:60], violations))
        dump = b"CREATE TABLE t (id int);\nINSERT INTO t VALUES (1);\n"
        for rel, blob in (
                ("uploads/customer.py", b"\xef\xbb\xbf\n" + SQL_DUMP),
                ("seed/customer.py", b"PRAGMA foreign_keys=OFF;\n" + dump),
                ("seed/customer.py", b"PRAGMA\nforeign_keys=OFF;\n" + dump),
                ("seed/customer.py", b"SAVEPOINT a;\n" + dump),
                ("seed/customer.py", b'-- "-- MySQL dump"\ncreate table t (id integer);\ninsert into t values (1);\n'),
                ("seed/customer.py", b"-- MySQL dump 10.13  Distrib 8.0\n--\n/*!40101 SET NAMES utf8mb4 */;\n" + dump),
                ("tools/restore.py", b'BANNER = "-- MySQL dump 10.13"\nSQL = "CREATE TABLE t (id int); INSERT INTO t VALUES (1);"\n'),
                ("tools/migrate.sh", b"psql <<EOF\n" + dump + b"EOF\n")):
            self.sh("git", "reset", "-q")
            self.stage(rel, blob)
            violations, _ = self.scan()
            self.assertTrue(any(rel in v and "dump" in v for v in violations), (rel, blob[:60], violations))



if __name__ == "__main__":
    unittest.main()
