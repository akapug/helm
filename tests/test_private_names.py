#!/usr/bin/env python3
"""The private-name pre-commit rung and its classifier advisory.

Every private name an arm stages is BUILT HERE AT RUN TIME from the one list
(helm/private_names.py), so this file carries none in the clear. The repos
are temporary; the classifier is a fake `helm` on PATH that records what it
was asked and answers from a script, so no arm reaches a network or a stream.
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import private_names                                     # noqa: E402
from helm.work import _guard                                       # noqa: E402

RUNG = os.path.abspath(private_names.__file__)
#: The two list entries the lane comments that motivated this rung named.
LEAKED = (private_names.NAMES[2], private_names.NAMES[6])
CLEAN = ("# keep the lock until the append lands, then release it\n"
         "X = 1\n")

FAKE_HELM = r'''#!%s
import json, os, sys, time
args = sys.argv[1:]
data = sys.stdin.read() if args[:1] == ["classify"] else ""
with open(os.environ["FAKE_HELM_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "stdin": data}) + "\n")
if args[:2] == ["classify", "--each-line"]:
    time.sleep(float(os.environ.get("FAKE_HELM_SLEEP") or 0))
    with open(os.environ["FAKE_HELM_REPLY"]) as f:
        sys.stdout.write(f.read())
'''


def spellings(name):
    """The lines a lane comment writes a project name in: a comment line in
    each spelling a person or a seat uses."""
    title = "-".join(p.title() for p in name.split("-"))
    return ["# the %s lane owns this retry" % name,
            "# see %s's notes before changing it" % title,
            "# %s_ROOT is read first" % name.upper().replace("-", "_"),
            "# the checkout under ~/dev/%s/ carries it" % name,
            "# measured by @%s-claude on the box" % name,
            "# %s: the room agreed" % name.upper()]


class RepoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-private-name-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"),
                     ("config", "--local", "helm.guard.profile", "rail")):
            self.assertEqual(self.git(*args).returncode, 0)
        self.stage("README", "seed\n")
        self.assertEqual(self.git("commit", "-qm", "seed").returncode, 0)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "fake-helm.jsonl")
        self.reply = os.path.join(self.tmp, "reply.jsonl")
        self.write_reply([])
        env = mock.patch.dict(os.environ, {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_HELM_LOG": self.log, "FAKE_HELM_REPLY": self.reply,
            "FAKE_HELM_SLEEP": "0"})
        env.start()
        self.addCleanup(env.stop)
        for key in ("HELM_PRIVATE_NAME_ADVISORY",
                    "HELM_PRIVATE_NAME_ADVISORY_LINES",
                    "HELM_PRIVATE_NAME_ADVISORY_MS"):
            os.environ.pop(key, None)
        self.install_fake_helm()

    def install_fake_helm(self):
        path = os.path.join(self.bin, "helm")
        with open(path, "w") as f:
            f.write(FAKE_HELM % sys.executable)
        os.chmod(path, 0o755)

    def write_reply(self, rows):
        with open(self.reply, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def git(self, *args, env=None):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True, timeout=90,
                              env=dict(os.environ, **(env or {})))

    def stage(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        r = self.git("add", "--", rel)
        self.assertEqual(r.returncode, 0, r.stderr)

    def refuse(self):
        err = io.StringIO()
        return private_names.refuse(self.root, err=err), err.getvalue()

    def advise(self):
        err = io.StringIO()
        return private_names.advise(self.root, err=err), err.getvalue()

    def rung(self, *args):
        return subprocess.run((sys.executable, RUNG) + (args or ("--staged",)),
                              cwd=self.root, capture_output=True, text=True,
                              timeout=90)


class RefusingRungTest(RepoBase):
    def test_each_spelling_of_the_leaked_names_is_refused(self):
        lines = [line for name in LEAKED for line in spellings(name)]
        self.stage("helm/lane.py", "\n".join(lines) + "\nX = 1\n")
        rc, err = self.refuse()
        self.assertEqual(rc, 1)
        refused = [row for row in err.splitlines() if "helm/lane.py:" in row]
        self.assertEqual(len(refused), len(lines),
                         "every spelling is its own refused line")
        for number, line in enumerate(lines, 1):
            self.assertIn("helm/lane.py:%d — private-names list entry %d"
                          % (number, 3 if number <= len(lines) // 2 else 7),
                          err)

    def test_every_listed_name_is_refused_in_every_public_bound_path(self):
        rels = ("helm/a.py", "docs/a.md", "tests/test_a.py", "agents/a.md",
                "bin/a", "scripts/a.sh", "README.md", "CHANGELOG.md")
        body = "".join("# the %s box\n" % n for n in private_names.NAMES)
        for rel in rels:
            self.stage(rel, body)
        rc, err = self.refuse()
        self.assertEqual(rc, 1)
        for rel in rels:
            for number in range(1, len(private_names.NAMES) + 1):
                self.assertIn("%s:%d — private-names list entry %d"
                              % (rel, number, number), err)

    def test_the_refusal_never_prints_the_name(self):
        name = LEAKED[0]
        self.stage("docs/leak.md", "the %s lane\n%s\n" % (name, name.upper()))
        rc, err = self.refuse()
        self.assertEqual(rc, 1)
        self.assertIn("docs/leak.md:1", err, "control: it did refuse")
        self.assertNotIn(name, err.lower())

    def test_a_clean_diff_passes_in_silence(self):
        self.stage("helm/clean.py", CLEAN)
        self.stage("docs/clean.md", "The lock is held until the write lands.\n")
        self.assertEqual(self.refuse(), (0, ""))
        # the same observable refuses one planted line
        self.stage("docs/clean.md", "The %s box holds the lock.\n" % LEAKED[1])
        self.assertEqual(self.refuse()[0], 1)

    def test_line_numbers_are_the_ones_git_counts(self):
        """A form feed or a lone CR inside a line is not a line break to git,
        so it must not shift the number the refusal names."""
        self.stage("docs/paged.md", "one\x0cstill one\rand still\n"
                   "the %s box\n" % LEAKED[0])
        rc, err = self.refuse()
        self.assertEqual(rc, 1)
        self.assertIn("docs/paged.md:2 —", err)

    def test_a_path_that_is_not_public_bound_is_not_judged(self):
        for rel in ("journal/notes.md", "notes/x.txt", ".github/x.yml"):
            self.stage(rel, "the %s box\n" % LEAKED[0])
        self.assertEqual(self.refuse(), (0, ""))
        self.assertFalse(private_names.public_bound("journal/notes.md"))
        self.assertTrue(private_names.public_bound("SECURITY.md"))

    def test_only_added_lines_are_judged(self):  # noqa: VACUOUS_ASSERTION — the same refuse() observable returns 1 once the judged line is edited, unconditionally
        self.stage("docs/old.md", "the %s box\nsecond line\n" % LEAKED[0])
        self.assertEqual(self.git("commit", "-qm", "debt",
                                  "--no-verify").returncode, 0)
        self.stage("docs/old.md", "the %s box\nsecond line, edited\n"
                   % LEAKED[0])
        self.assertEqual(self.refuse(), (0, ""),
                         "a line HEAD already carries is not this commit's")
        self.stage("docs/old.md", "the %s box, edited\nsecond line, edited\n"
                   % LEAKED[0])
        self.assertEqual(self.refuse()[0], 1, "an edited line is added")

    def test_a_rename_into_a_public_bound_path_adds_every_line(self):  # noqa: VACUOUS_ASSERTION — the refusal rc 1 and the path:line it names are unconditional positive controls
        self.stage("notes/draft.md", "the %s box\n" % LEAKED[1])
        self.assertEqual(self.git("commit", "-qm", "draft").returncode, 0)
        os.makedirs(os.path.join(self.root, "docs"))
        self.assertEqual(self.git("mv", "notes/draft.md",
                                  "docs/draft.md").returncode, 0)
        rc, err = self.refuse()
        self.assertEqual(rc, 1)
        self.assertIn("docs/draft.md:1", err)

    def test_a_staged_set_that_cannot_be_read_refuses_as_unknown(self):
        self.stage("helm/clean.py", CLEAN)
        with mock.patch("helm.conflict_marker._added_ranges",
                        side_effect=RuntimeError("range read failed")):
            rc, err = self.refuse()
        self.assertEqual(rc, 2)
        self.assertIn("UNKNOWN", err)

    def test_a_missing_seam_warns_and_stands_down(self):
        self.stage("docs/leak.md", "the %s box\n" % LEAKED[0])
        with mock.patch.object(private_names, "_sibling", return_value=None):
            rc, err = self.refuse()
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED", err)

    def test_each_refusal_is_journalled_as_a_hashed_positive(self):
        line = "# the %s lane owns this retry" % LEAKED[0]
        self.stage("helm/lane.py", line + "\n")
        self.assertEqual(self.refuse()[0], 1)
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["classify", "label", "--consumer",
                                        "private-name", "--label", "private"])
        self.assertEqual(call["stdin"], "%s 3\n" % private_names.line_hash(line))
        self.assertNotIn(LEAKED[0], call["stdin"])

    def test_the_script_door_exits_one_on_a_refusal(self):  # noqa: VACUOUS_ASSERTION — rc 1 with the REFUSED banner is the unconditional positive control beside the clean rc 0
        self.stage("helm/lane.py", "# the %s lane\n" % LEAKED[1])
        r = self.rung()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("[helm private-name] REFUSED", r.stderr)
        self.stage("helm/lane.py", CLEAN)
        self.assertEqual(self.rung().returncode, 0)


class AdvisoryTest(RepoBase):
    def row(self, line, private, label=None):
        return {"line": line, "outcome": "ok",
                "label": label or ("private" if private >= .5 else "clean"),
                "scores": {"private": private, "clean": 1 - private}}

    def stage_prose(self):
        self.stage("helm/mod.py", '"""Module prose about the retry window."""\n'
                   "# a comment about the lock that is held\n"
                   "MESSAGE = 'a string is data, never asked about'\n")
        self.stage("docs/guide.md", "A document line about the ledger.\n")

    def test_it_warns_at_the_threshold_highest_first_and_never_refuses(self):
        self.stage_prose()
        self.write_reply([self.row(1, 0.80), self.row(2, 0.40),
                          self.row(3, 0.97, label="clean")])
        rc, err = self.advise()
        self.assertEqual(rc, 0)
        self.assertIn("ADVISORY", err)
        warned = [row for row in err.splitlines() if "  0." in row]
        self.assertEqual(len(warned), 2, err)
        self.assertIn("0.97  helm/mod.py:2", warned[0],
                      "ranked by the private score, not by the label")
        self.assertIn("0.80  docs/guide.md:1", warned[1])
        self.assertIn("NAMES_HEX", err)
        (call,) = self.calls()
        self.assertEqual(call["argv"][:2], ["classify", "--each-line"])
        self.assertIn("@private-name", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--source") + 1],
                         "lane-diff")
        self.assertEqual(call["stdin"].splitlines(), [
            "A document line about the ledger.",
            "Module prose about the retry window.",
            "a comment about the lock that is held"])

    def test_it_is_silent_below_the_threshold(self):
        self.stage_prose()
        self.write_reply([self.row(1, 0.74), self.row(2, 0.2),
                          self.row(3, 0.5, label="private")])
        self.assertEqual(self.advise(), (0, ""))
        self.assertEqual(len(self.calls()), 1, "control: it did ask")

    def test_it_is_silent_when_the_stream_is_unreachable(self):  # noqa: VACUOUS_ASSERTION — each silence is paired with the fake's call log, which proves the leg ran and asked
        self.stage_prose()
        self.write_reply([{"line": n, "outcome": "unreachable",
                           "detail": "refused"} for n in (1, 2, 3)])
        self.assertEqual(self.advise(), (0, ""))
        self.assertEqual(len(self.calls()), 1, "control: it did ask")
        git_only = os.path.join(self.tmp, "git-only")
        os.makedirs(git_only)
        os.symlink(shutil.which("git"), os.path.join(git_only, "git"))
        self.write_reply([self.row(1, 0.99)])
        with mock.patch.dict(os.environ, {"PATH": git_only}):
            self.assertIsNone(shutil.which("helm"))
            self.assertEqual(self.advise(), (0, ""), "no helm on PATH")
        self.assertEqual(len(self.calls()), 1)

    def test_it_is_silent_when_the_answer_outlives_its_budget(self):
        self.stage_prose()
        self.write_reply([self.row(1, 0.99)])
        with mock.patch.dict(os.environ, {"FAKE_HELM_SLEEP": "5",
                                          "HELM_PRIVATE_NAME_ADVISORY_MS": "100"}), \
                mock.patch.object(private_names, "SPAWN_GRACE_S", 0.3):
            self.assertEqual(self.advise(), (0, ""))

    def test_the_off_switch_asks_nothing(self):  # noqa: VACUOUS_ASSERTION — the final arm re-runs the same leg with the switch unset and asserts the warning
        self.stage_prose()
        self.write_reply([self.row(1, 0.99)])
        with mock.patch.dict(os.environ, {"HELM_PRIVATE_NAME_ADVISORY": "0"}):
            self.assertEqual(self.advise(), (0, ""))
        self.assertEqual(self.calls(), [])
        self.assertIn("ADVISORY", self.advise()[1], "control: on, it warns")

    def test_the_line_cap_is_named_when_it_bites(self):
        self.stage_prose()
        self.write_reply([self.row(1, 0.9)])
        with mock.patch.dict(os.environ,
                             {"HELM_PRIVATE_NAME_ADVISORY_LINES": "2"}):
            _rc, err = self.advise()
        self.assertIn("asked about 2 of 3 candidate lines", err)
        self.assertEqual(len(self.calls()[0]["stdin"].splitlines()), 2)

    def test_a_line_the_list_already_refuses_is_not_asked_about(self):
        self.stage("docs/guide.md", "A document line about the ledger.\n"
                   "The %s box keeps it.\n" % LEAKED[0])
        self.write_reply([])
        self.advise()
        self.assertEqual(self.calls()[0]["stdin"],
                         "A document line about the ledger.\n")

    def test_prose_is_comments_docstrings_and_document_lines(self):
        src = ('"""Module docstring line one.\n\nSecond paragraph."""\n'
               "X = 'data, not prose'  # trailing comment\n"
               "def f():\n"
               "    '''Function docstring.'''\n"
               "    return 1\n")
        self.assertEqual(private_names.prose_lines("helm/m.py", src), {
            1: "Module docstring line one.", 2: "", 3: "Second paragraph.",
            4: "trailing comment", 6: "Function docstring."})
        self.assertEqual(private_names.prose_lines("bin/tool", "#!/bin/sh\n"
                                                   "echo hi\n# a note\n"),
                         {1: "!/bin/sh", 3: "a note"})
        self.assertEqual(private_names.prose_lines("docs/a.md", "x\n\ny\n"),
                         {1: "x", 3: "y"})


class HookWiringTest(RepoBase):
    SKIPS = {"HELM_WORK_INTEGRATOR": "1", "HELM_LANDLOCK": "0",
             "HELM_TRAILER_SKIP": "1",
             "HELM_LANE_DISCIPLINE_SKIP": "1", "HELM_INFLIGHT_GATE_SKIP": "1",
             "HELM_NEVER_TRACK_SKIP": "1", "HELM_DOCREF_SKIP": "1",
             "HELM_SEATNAME_SKIP": "1", "HELM_WORLD_PROSE_SKIP": "1",
             "HELM_RETIRED_NAME_SKIP": "1", "HELM_SPLIT_BUDGET_SKIP": "1"}

    def test_install_snapshots_the_rung_and_the_hook_refuses_a_leak(self):  # noqa: VACUOUS_ASSERTION — snapshot byte equality, the refused commit and the skipped commit are unconditional positive controls
        rc, _lines = _guard.install_guard(self.root, apply=True)
        self.assertEqual(rc, 0)
        installed = next(p for p in _guard._scanner_assets(self.root)
                         if p.endswith("/private_names.py"))
        with open(installed, "rb") as got, open(RUNG, "rb") as want:
            self.assertEqual(got.read(), want.read())
        self.assertEqual(stat.S_IMODE(os.stat(installed).st_mode) & 0o111, 0)
        with open(_guard.hook_path(self.root, "pre-commit")) as f:
            body = f.read()
        self.assertIn("pre-commit v8", body)
        self.assertEqual(body.count('python3 "$private_name" --staged || exit $?'),
                         1)
        self.assertEqual(body.count('python3 "$private_name" --advise || true'), 1)
        self.assertNotIn(RUNG, body)

        self.stage("helm/lane.py", "# the %s lane owns this\n" % LEAKED[0])
        r = self.git("commit", "-m", "leak", env=self.SKIPS)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[helm private-name] REFUSED", r.stderr)
        r = self.git("commit", "-m", "owner override",
                     env=dict(self.SKIPS, HELM_PRIVATE_NAME_SKIP="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_advisory_warns_through_the_hook_and_the_commit_lands(self):
        _guard.install_guard(self.root, apply=True)
        self.stage("docs/guide.md", "A document line about the ledger.\n")
        self.write_reply([{"line": 1, "outcome": "ok", "label": "private",
                           "scores": {"private": 0.91, "clean": 0.09}}])
        r = self.git("commit", "-m", "prose", env=self.SKIPS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[helm private-name] ADVISORY", r.stderr)
        self.assertIn("0.91  docs/guide.md:1", r.stderr)


if __name__ == "__main__":
    unittest.main()
