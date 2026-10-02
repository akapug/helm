"""RED tests for helm.taskverify (task/3747 slice A).

The module helm/taskverify.py and the `verify`/`show` wiring do not exist yet,
so the first pass FAILS at import. The arms are written to the brief's exact
shape; when the module and the verb land, the same arms are the green bar.
The git binary is the only VCS dependency (the spec names
`git rev-parse --verify ... ^{commit}`), so temp repos are built with it via
subprocess.

The API is `tasks.verify(token, verdict, trunk, evidence, by=None, link=None,
path=None)` -> (row, err); the CLI is
`helm task verify <id> <verdict> --trunk <sha> --evidence <one line> [--link <ref>]`.
The `verified.trunk` stored is the FULL sha resolved in the task's project
repo, not the raw input.
"""
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

from .test_tasks import CliBase, TasksBase

from helm import tasks


def _git_root(path):
    """A git repo rooted at `path` -> its commit sha (via the git binary)."""
    env = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    subprocess.check_call(["git", "init", path], env=env)
    with open(os.path.join(path, "r.txt"), "w", encoding="utf-8") as fh:
        fh.write("r\n")
    subprocess.check_call(["git", "-C", path, "add", "r.txt"], env=env)
    subprocess.check_call(["git", "-C", path, "commit", "-m", "seed"],
                          env=env)
    out = subprocess.check_output(
        ["git", "-C", path, "rev-parse", "HEAD"]).decode().strip()
    return out


class StampTest(TasksBase):
    """stamp(): row fields, CAP, and the two refusals the spec names."""

    def setUp(self):
        super().setUp()
        from helm import taskverify
        self.tv = taskverify

    def _row(self):
        return dict(id="task/1000", ts=1.0, last_updated=1.0,
                    title="t", status="open", owner=None, note=None,
                    refs=[], source=None, origin=None, closed_reason=None,
                    comments=[])

    def test_stamp_appends_the_fields(self):
        row = self._row()
        out = self.tv.stamp(row, "LIVE", "abc123",
                            "measured at trunk abc123", "seat-a",
                            link="#3747", now=1760000000.0)
        self.assertIn("verified", out)
        self.assertEqual(len(out["verified"]), 1)
        s = out["verified"][0]
        self.assertEqual(s["verified_at"], 1760000000.0)
        self.assertEqual(s["verified_by"], "seat-a")
        self.assertEqual(s["trunk"], "abc123")
        self.assertEqual(s["verdict"], "LIVE")
        self.assertEqual(s["evidence"], "measured at trunk abc123")
        self.assertEqual(s["link"], "#3747")
        # The input row IS the returned row (documented: returns the row).
        self.assertIs(out, row)

    def test_link_defaults_to_none(self):
        out = self.tv.stamp(self._row(), "LIVE", "abc123",
                            "e", "seat-a")
        self.assertIsNone(out["verified"][0]["link"])

    def test_cap_keeps_the_newest_five(self):
        # Seven stamps -> the newest FIVE survive, oldest (e0) and second-oldest
        # (e1) dropped. That is the contract: a re-verified row is still read
        # once, and the five most recent are what a triage needs. (Stamping e0
        # through e6 leaves e2..e6, not e3..e7.)
        row = self._row()
        for i in range(7):
            self.tv.stamp(row, "LIVE", "abc123", "e%d" % i,
                          "seat-a", now=float(i))
        kept = [s["evidence"] for s in row["verified"]]
        self.assertEqual(kept, ["e2", "e3", "e4", "e5", "e6"])

    def test_refuses_a_verdict_outside_the_vocabulary(self):
        row = self._row()
        for bad in ("KEEP", "CLOSE-FIXED", "live", "LIVE ", "x"):
            with self.assertRaises(self.tv.REFUSE):
                self.tv.stamp(row, bad, "abc123", "e", "seat-a")
            self.assertNotIn("verified", row)

    def test_refuses_empty_evidence(self):
        row = self._row()
        for bad in ("", "   ", "\n"):
            with self.assertRaises(self.tv.REFUSE):
                self.tv.stamp(row, "LIVE", "abc123", bad, "seat-a")
            self.assertNotIn("verified", row)

    def test_refuses_multiline_evidence(self):
        row = self._row()
        with self.assertRaises(self.tv.REFUSE):
            self.tv.stamp(row, "LIVE", "abc123",
                          "first line\nsecond line", "seat-a")
        with self.assertRaises(self.tv.REFUSE):
            self.tv.stamp(row, "LIVE", "abc123",
                          "first line\nsecond\ntab", "seat-a")


class ResolveTrunkTest(TasksBase):
    """resolve_trunk(): short sha -> full sha in a temp git repo, else a clear
    error."""

    def setUp(self):
        super().setUp()
        from helm import taskverify
        self.tv = taskverify
        self.sha = _git_root(self.tmp)

    def test_short_sha_resolves_to_full(self):
        out = self.tv.resolve_trunk(self.tmp, self.sha[:8])
        self.assertEqual(out, self.sha)
        self.assertEqual(len(out), 40)
        self.assertRegex(out, re.compile(r"^[0-9a-f]{40}$"))

    def test_full_sha_passes_through(self):
        self.assertEqual(self.tv.resolve_trunk(self.tmp, self.sha), self.sha)

    def test_unknown_sha_raises(self):
        with self.assertRaises(self.tv.REFUSE):
            self.tv.resolve_trunk(self.tmp, "deadbeef" * 5)

    def test_unknown_repo_raises(self):
        with self.assertRaises(self.tv.REFUSE):
            self.tv.resolve_trunk(tempfile.mkdtemp(), self.sha[:8])


class ShowLineTest(TasksBase):
    """show_line(): the exact newest-stamp line, None when there is no stamp.

    Exact shape (brief, verbatim):
      verified <verdict> by <by> at <YYYY-MM-DD HH:MMZ>, trunk <sha>: <evidence>
    plus ' (<link>)' only when a link is present."""

    def setUp(self):
        super().setUp()
        from helm import taskverify
        self.tv = taskverify

    def _row(self, verified=None):
        row = dict(id="task/1000", ts=1.0, title="t", status="open")
        row["verified"] = list(verified) if verified is not None else None
        return row

    def test_none_when_no_stamps(self):
        # The positive control on this observable: a row THAT HAS a stamp does
        # return a line. That is what makes the `None` asserts below measure an
        # ABSENCE (and a distinct shape) rather than a default — a writer that
        # returned `None` for every row would make the asserts below pass while
        # breaking this one.
        stamped = self._row(verified=[
            dict(verified_at=1.0, verified_by="a", trunk="t",
                 verdict="LIVE", evidence="e")])
        self.assertIsNotNone(self.tv.show_line(stamped))
        # No stamp is three shapes: the key absent, an empty list, or a
        # `verified` that is explicitly None. `show_line` must read all three
        # as "no verdict recorded".
        empty = dict(id="task/1000", ts=1.0, title="t", status="open",
                     verified=[])
        self.assertIsNone(self.tv.show_line(empty))
        self.assertIsNone(self.tv.show_line(self._row(verified=None)))

    def test_one_stamp_rendered(self):
        row = self._row(verified=[
            dict(verified_at=1760000000.0, verified_by="qwen27",
                 trunk="9ae70c6e055", verdict="LIVE",
                 evidence="measured at trunk 9ae70c6e055", link="#3747")])
        # `when` is derived from the SAME strftime the module must use, so
        # the assertion pins the FORMAT (UTC, no seconds, "at <ts>, trunk <sha>:"
        # layout) rather than a memorized epoch. A module that formats the
        # timestamp any other way (seconds, local time, ISO) fails here.
        when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(1760000000.0))
        expected = ("verified LIVE by qwen27 at %s, trunk 9ae70c6e055: "
                    "measured at trunk 9ae70c6e055 (#3747)" % when)
        self.assertEqual(self.tv.show_line(row), expected)

    def test_newest_stamp_wins(self):
        row = self._row(verified=[
            dict(verified_at=1.0, verified_by="a", trunk="t1",
                 verdict="STALE", evidence="old", link=None),
            dict(verified_at=2.0, verified_by="qwen27", trunk="t2",
                 verdict="NARROW", evidence="new", link="#1")])
        line = self.tv.show_line(row)
        self.assertIn("NARROW", line)
        self.assertIn("new", line)
        self.assertNotIn("old", line)
        self.assertNotIn("STALE", line)

    def test_no_link_suffix(self):
        row = self._row(verified=[
            dict(verified_at=1.0, verified_by="a", trunk="t",
                 verdict="FIXED", evidence="e", link=None)])
        line = self.tv.show_line(row)
        self.assertNotIn("()", line)
        self.assertNotIn("(None)", line)

    def test_missing_or_nonnumeric_verified_at_renders_question_mark(self):
        # A malformed stamp (missing `verified_at`, or one that is not a
        # number) must render "at ?" rather than crash `show_line`, so `helm
        # task show` never tracebacks on a row a writer corrupted.
        row = self._row(verified=[
            dict(verified_by="a", trunk="t", verdict="LIVE", evidence="e")])
        self.assertIn("at ?", self.tv.show_line(row))
        row = self._row(verified=[
            dict(verified_at="not-a-number", verified_by="a", trunk="t",
                 verdict="LIVE", evidence="e")])
        self.assertIn("at ?", self.tv.show_line(row))


class CliRoundTripTest(CliBase):
    """One CLI round trip on the temp task store: verify, then show prints the
    line. Also a direct-API arm for each of the three API refusals."""

    def setUp(self):
        super().setUp()
        from helm import taskverify
        self.tv = taskverify
        from helm import home
        self.gdir = home.global_dir()
        os.makedirs(self.gdir, exist_ok=True)
        # Seed a row through the API (what `helm task add` files). The temp
        # task store lives under self.tmp via tasks.ledger_path()
        # (TasksBase.setUp), so verify and show read/write the same store.
        row, err = tasks.add("verify target", "seat-a", project="fixproj")
        self.assertEqual(err, None)
        self.base_row = dict(row)
        self.id = row["id"]
        self._seed_registry()

    def _seed_registry(self):
        """Register a project whose path is the temp git repo fixproj-repo,
        so --trunk can resolve against a real git repo (the spec: resolve in
        the task's project repo)."""
        self.repo_path = os.path.join(self.tmp, "fixproj-repo")
        self.sha = _git_root(self.repo_path)
        with open(os.path.join(self.gdir, "registry.json"), "w",
                   encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": {
                "fixproj": {"name": "fixproj", "path": self.repo_path},
                "otherproj": {"name": "otherproj",
                               "path": self.tmp}}}, fh)

    def _capture_stdout(self, fn, *args, **kw):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            fn(*args, **kw)
        finally:
            sys.stdout = old
        return buf.getvalue()

    def test_verify_then_show_prints_the_line(self):
        # 1) verify via the API (what `helm task verify` calls): the short
        # sha in, the FULL sha stored.
        row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "measured at trunk", "seat-a")
        self.assertEqual(err, None)
        self.assertIn("verified", row)
        self.assertEqual(row["verified"][0]["trunk"], self.sha)
        self.assertEqual(row["verified"][0]["verified_by"], "seat-a")
        self.assertEqual(row["verified"][0]["verdict"], "LIVE")
        # 2) show via the CLI; the line must render the verdict, the seat and
        # the resolved trunk.
        text = self._capture_stdout(tasks.cmd_task, ["show", self.id])
        self.assertIn("verified LIVE", text)
        self.assertIn("by seat-a", text)
        self.assertIn("trunk %s" % self.sha, text)
        self.assertIn("measured at trunk", text)

    def test_cli_verify_then_show(self):
        # The verb itself, not only the API: its flags, its author (the seat
        # the CLI acts as) and its exit code.
        rc, out, err = self.cli("verify", self.id, "LIVE", "--trunk",
                                self.sha[:8], "--evidence", "read at trunk")
        self.assertEqual(rc, 0, err)
        self.assertIn("stamped LIVE", out)
        stamp = tasks.rows(path=tasks.ledger_path())[self.id]["verified"][-1]
        self.assertEqual(stamp["trunk"], self.sha)
        self.assertEqual(stamp["verified_by"], "seat-a")
        rc, out, err = self.cli("show", self.id)
        self.assertEqual(rc, 0, err)
        self.assertIn("verified LIVE by seat-a", out)

    def test_cli_verify_refuses_a_stray_word_and_stamps_nothing(self):
        rc, _out, err = self.cli("verify", self.id, "LIVE", "--trunk",
                                 self.sha[:8], "--evidence", "e", "extra")
        self.assertEqual(rc, 2)
        self.assertIn("unknown token", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_cli_refuses_a_bad_verdict_and_stamps_nothing(self):
        # A verdict outside the vocabulary is a typo that must refuse BEFORE a
        # stamp is written; the CLI exits 2 and nothing lands.
        rc, _out, err = self.cli("verify", self.id, "KEEP",
                                 "--trunk", self.sha[:8], "--evidence", "e")
        self.assertEqual(rc, 2, err)
        self.assertIn("verdict must be one of", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_cli_refuses_a_missing_trunk_and_stamps_nothing(self):
        # A verdict with no --trunk names no trunk the reader read, so it is
        # refused before a stamp; nothing may land with a guessed trunk.
        rc, _out, err = self.cli("verify", self.id, "LIVE",
                                 "--evidence", "read at trunk")
        self.assertEqual(rc, 2, err)
        self.assertIn("--trunk", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_cli_refuses_a_missing_evidence_and_stamps_nothing(self):
        rc, _out, err = self.cli("verify", self.id, "LIVE",
                                 "--trunk", self.sha[:8])
        self.assertEqual(rc, 2, err)
        self.assertIn("--evidence", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_api_refuses_bad_verdict_and_ledger_stays_unstamped(self):
        _row, err = tasks.verify(self.id, "KEEP", self.sha[:8],
                                "e", "seat-a")
        self.assertIsNotNone(err)
        # THE ROW THE VERB READ, read back from the ledger file it would commit
        # through — not the returned copy (a bug that left the returned copy
        # stamped would still pass a test that only inspects `row` while the
        # real row carries the lie).
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_unregistered_project_refusal_names_the_cause(self):
        row, err = tasks.add("elsewhere", "seat-a", project="nosuchproj")
        self.assertEqual(err, None)
        _row, err = tasks.verify(row["id"], "LIVE", self.sha[:8], "e", "seat-a")
        self.assertIn("project 'nosuchproj' is not registered", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[row["id"]])

    def test_unreadable_registry_is_named_not_read_as_unregistered(self):
        with open(os.path.join(self.gdir, "registry.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        _row, err = tasks.verify(self.id, "LIVE", self.sha[:8], "e", "seat-a")
        self.assertIn("registry cannot be read", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_api_refuses_unknown_sha_and_ledger_stays_unstamped(self):
        # An unresolvable trunk is refused in the project repo BEFORE a stamp,
        # and the refusal keeps the row exactly as the reader read it — no
        # verified entry, no guessed trunk.
        _row, err = tasks.verify(self.id, "LIVE", "deadbeef" * 5, "e", "seat-a")
        self.assertIsNotNone(err)
        self.assertIn("does not name a commit", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_cli_refuses_unknown_sha_and_stamps_nothing(self):
        rc, _out, err = self.cli("verify", self.id, "LIVE",
                                 "--trunk", "deadbeef" * 5, "--evidence", "e")
        self.assertEqual(rc, 2, err)
        self.assertIn("does not name a commit", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_api_refuses_empty_evidence_and_ledger_stays_unstamped(self):
        _row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "   ", "seat-a")
        self.assertIsNotNone(err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])


class Round2FindingsTest(CliBase):
    """The five findings from the fresh Opus read (round 2, AFTER stalebot).
    F1 valueless --link, F2 evidence/link caps, F3 registry name-lookup."""

    _fresh = itertools.count()

    def setUp(self):
        super().setUp()
        from helm import home
        self.gdir = home.global_dir()
        os.makedirs(self.gdir, exist_ok=True)
        self.repo_path = os.path.join(self.tmp, "f3-repo")
        self.sha = _git_root(self.repo_path)
        row, err = tasks.add("round2 row %d" % next(self._fresh),
                             self.SEAT, project="fixproj")
        self.assertEqual(err, None)
        self.id = row["id"]
        self._seed_registry()

    def _seed_registry(self):
        # "keyname" is the KEY, "renamed" is the registered NAME the row's
        # project field carries. Both must map to the same real git repo.
        with open(os.path.join(self.gdir, "registry.json"), "w",
                   encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": {
                "fixproj": {"name": "fixproj", "path": self.repo_path},
                "keyname": {"name": "renamed",
                            "path": self.repo_path},
                "pathonly": {"path": self.tmp}}}, fh)

    def _fresh_row(self):
        """A new unverified row in scope, so each arm measures its own row —
        a valueless refusal must leave ITS row untouched, and a row already
        stamped by an earlier arm would make the 'not stamped' assert see the
        stale stamp, not the refusal. The title is unique per arm so the
        duplicate-verdict guard (task add's 100%-overlap refusal) never fires
        on a second arm in the same test."""
        row, err = tasks.add("f1 valueless row %d" % next(self._fresh),
                             self.SEAT, project="fixproj")
        self.assertEqual(err, None)
        return row["id"]

    def test_f1_valueless_link_refuses_and_stamps_nothing(self):
        # --link with no value is a typo, not a default: _take deletes a
        # trailing valueless flag while returning None, so without the guard
        # `--link` would vanish and link=None would be stamped. The fresh row
        # is the point — a valueless refusal must leave ITS row untouched; a
        # row already stamped by a prior arm would make the 'not stamped'
        # assert see a stale stamp, not the refusal.
        # Positive control: --link omitted stamps link=None.
        c1 = self._fresh_row()
        rc1, _out1, _err1 = self.cli("verify", c1, "LIVE",
                                      "--trunk", self.sha[:8],
                                      "--evidence", "e")
        self.assertEqual(rc1, 0, _err1)
        self.assertEqual(
            tasks.rows(path=tasks.ledger_path())[c1]["verified"][-1]["link"],
            None)
        # The valueless flag must refuse, name --link, and stamp nothing.
        row = self._fresh_row()
        rc, _out, err = self.cli("verify", row, "LIVE",
                                "--trunk", self.sha[:8],
                                "--evidence", "e", "--link")
        self.assertEqual(rc, 2, err)
        self.assertIn("--link", err)
        self.assertIn("VALUE", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[row])

    def test_f1_valueless_trunk_refuses_and_stamps_nothing(self):
        row = self._fresh_row()
        rc, _out, err = self.cli("verify", row, "LIVE",
                                "--trunk", "--evidence", "e")
        self.assertEqual(rc, 2, err)
        self.assertIn("--trunk", err)
        self.assertIn("VALUE", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[row])

    def test_f1_valueless_evidence_refuses_and_stamps_nothing(self):
        row = self._fresh_row()
        rc, _out, err = self.cli("verify", row, "LIVE",
                                "--trunk", self.sha[:8], "--evidence")
        self.assertEqual(rc, 2, err)
        self.assertIn("--evidence", err)
        self.assertIn("VALUE", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[row])

    def test_f2_refuses_evidence_over_the_comment_cap(self):
        import helm.tasks as tasks_mod
        max_chars = tasks_mod.COMMENT_TEXT_MAX
        row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "x" * (max_chars + 1), "seat-a")
        self.assertIsNotNone(err)
        self.assertIn(str(max_chars), err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_f2_accepts_evidence_exactly_at_the_comment_cap(self):
        import helm.tasks as tasks_mod
        max_chars = tasks_mod.COMMENT_TEXT_MAX
        row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "x" * max_chars, "seat-a")
        self.assertEqual(err, None)
        self.assertEqual(row["verified"][0]["evidence"], "x" * max_chars)

    def test_f2_refuses_link_over_512(self):
        row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "e", "seat-a", link="a" * 513)
        self.assertIsNotNone(err)
        self.assertIn("512", err)
        self.assertNotIn("verified",
                        tasks.rows(path=tasks.ledger_path())[self.id])

    def test_f2_accepts_link_exactly_512(self):
        row, err = tasks.verify(self.id, "LIVE", self.sha[:8],
                                "e", "seat-a", link="a" * 512)
        self.assertEqual(err, None)
        self.assertEqual(row["verified"][0]["link"], "a" * 512)

    def test_f3_resolves_by_the_registered_name_not_the_key(self):
        # F3: the row carries the project NAME ("renamed"), but the registry
        # key is "keyname". The old projects.get(name) read the key and would
        # find nothing. The fix matches on rec.get("name") or key, so the
        # row scoped to the NAME finds its repo and the trunk resolves against
        # it.
        row, err = tasks.add("name-scope row", self.SEAT, project="renamed")
        self.assertEqual(err, None)
        _row, err = tasks.verify(row["id"], "LIVE", self.sha[:8],
                                  "e", "seat-a")
        self.assertEqual(err, None)
        self.assertEqual(_row["verified"][0]["trunk"], self.sha)


if __name__ == "__main__":
    unittest.main()
