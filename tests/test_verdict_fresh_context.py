#!/usr/bin/env python3
"""A fresh-context Opus run's read reaches the ledger, on a door lane as on a
reversible one (the integrator's ruling, chat row 1693, widened by the
owner's ruling in room row 2104).

THE PROBLEM, MEASURED: `helm dispatch verdict --reviewer-model opus`
refused every Opus read of Claude-authored work ("IS the author's model", "the
same family"), while store prior review-independence-is-model-or-context-
scaled-by-reversibility already counts a fresh-context Opus read as a full
leg on a reversible Opus-authored lane. Each such read needed a second,
other-family read just to reach the ledger. Then the owner: "I think opus
seats should be in the upper tier, we are probably eating lots of tokens on
extra rounds". A door read needs ONE approval-tier read by a reader that is
not the author, and a different family is no longer required, so a door
lane records the verified read too, and the author's declared model does not
decide it.

ONE ARM PER BOUND OF THE RULING:
  (a) a reversible lane and each door class are admitted, recorded as
      `fresh-context`, and so is a Claude-sent row whose declared author
      model is another family's;
  (b) a run whose transcript is missing is refused, and so is a run that
      wrote a lane file, a fork, a run whose models are not Opus, a run
      whose transcript never names the reviewed tip (it read another lane),
      a run still in flight (no final answer, or a Workflow that did not
      complete), a run that began before the reviewed tip was committed (a
      builder that edited through the shell leaves no Write or Edit, and
      only when it began tells it from a reader; RunBeganAfterTipTest), and
      a lane whose changed files cannot be read; each holds on a door row
      too;
  (c) the run's own lineage: a run whose own line records a fork, or whose
      transcript opens by continuing a conversation, is refused; the session
      that SPAWNED it is no input, so a fresh subagent of the author's own
      session, or of the session that wrote the lane, is recorded (the
      owner's rule, "Sub agent of your own"; ReadingInstanceTest,
      task/3483 and task/3658);
  (d) the row records the model and `fresh-context run <id>`, and `helm lr
      show` prints it as "recorded (unattested)": a same-user process can
      plant a record, so no surface says the run was verified on disk.
Sonnet and Haiku never review, and gemini and local models stay input: the
arm is Opus's alone. The door CLASSIFIER keeps its falsifier (CL 85): a
planted guard lane, one that changes a hook's refusal in a mixed module,
must classify as a DOOR, and the arm that catches it must be load-bearing:
remove it and the planted lane reads reversible. A lane that rebinds what a
guard calls on an IMPORT line is a door too.
"""
import contextlib
import datetime
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (compose_contract, dispatches, landreq, pk,  # noqa: E402
                  review_door, rowworld, runrecord, seats, seats_join)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402
from tests._satellite_resolution import ledger_sources  # noqa: E402

OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
READER_SESSION = "11111111-2222-4333-8444-555555555555"
AUTHOR_SESSION = "66666666-7777-4888-8999-000000000000"
RECORDER_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RECIPIENT_SESSION = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
#: The review row's recipient, in the house fixture convention.
RECIPIENT = "seat-a"
#: A seat that wrote a round of the lane but did not send the row read, and
#: the one session its roster row records.
LANE_AUTHOR = "seat-b"
LANE_AUTHOR_SESSION = "cccccccc-eeee-4fff-8aaa-bbbbbbbbbbbb"
AGENT = "a0123456789abcdef"
WORKFLOW = "wf_0123abcd-4e5"
#: The tip the planted runs read, when a test names no other: a run's
#: transcript must name the reviewed tip, or it read some other lane. No
#: twelve hex of it recur in any planted id or session above (the agent id's
#: own digits once made a transcript "name" a tip it never read).
TIP = "d" * 40

#: What no surface may say of a fresh-context run: that it was verified or
#: proven on disk. Its record is unattested (see the arm that sweeps them).
OVERCLAIM = re.compile(r"(?i)\b(?:verif\w*|proven)\s+(?:\S+\s+){0,3}?on\s+"
                       r"disk\b")
#: The line every surface prints for a source-clean hold that rests on a
#: fresh-context read (task/3658), with its tip and holder.
HOLD_LINE = ("ADVISORY: the SOURCE-CLEAN hold at %s by @%s rests on the "
             "fresh-context read above; what it owes now is the integrator's "
             "land gate")
#: A paragraph about a model run's record names one of these.
RUN_WORDS = re.compile(r"(?i)fresh-context|reviewer[-_]run|runrecord"
                       r"|model run")
#: Every surface that describes a fresh-context run's record. A `helm/*.py`
#: entry names a MODULE and is read whole, with every satellite it declares:
#: see `surface_sources`.
SURFACES = ("helm/dispatches.py", "helm/runrecord.py", "helm/review_door.py",
            "helm/cli_help.py", "helm/obligation.py", "docs/VERBS.md",
            "agents/claudecode/skills/build/SKILL.md",
            "agents/claudecode/skills/reviewer-implements-own-findings/"
            "SKILL.md")


def surface_sources(root, rel):
    """[(path, text)] for one entry of SURFACES.

    A MODULE'S SURFACE IS EVERY FILE IT SPANS. `dispatches` hands whole
    questions to satellites (task/3407), and a sweep that opened
    `dispatches.py` alone stopped reading the spiral's own paragraph about a
    model run's read. `ledger_sources` follows `_OWNER_NAMES`, so the next
    split stays in scope; a module that declares none yields itself."""
    if rel.startswith("helm/") and rel.endswith(".py"):
        return ledger_sources(importlib.import_module(
            rel[:-len(".py")].replace("/", ".")))
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return [(os.path.join(root, rel), fh.read())]


def run_paragraphs(text):
    """The paragraphs of `text` about a model run, each with wrapped string
    literals and comment runs joined, so a phrase split across lines is
    still one phrase."""
    return [" ".join(re.sub(r"[\"']\s*\n\s*[\"']|\n\s*#:?", " ", p).split())
            for p in re.split(r"\n\s*\n", text) if RUN_WORDS.search(p)]


#: One planted lane per door class: (class, path, text). Each lane touches
#: nothing else, so the class it reads is the one this plant carries.
DOOR_PLANTS = (
    ("prod", "ops/ship.sh", "wrangler deploy --env production\n"),
    ("migration", "db/migrations/0042_widen.sql",
     "ALTER TABLE users ADD COLUMN nick TEXT;\n"),
    ("deletion", "helm/tidy.py",
     "import shutil\n\n\ndef tidy(path):\n    shutil.rmtree(path)\n"),
    ("money", "helm/ledger_pay.py", "PROVIDER = 'stripe'\n"),
    ("credentials", "helm/cred/extra.py", "NAME = 'extra'\n"),
    ("process-kill", "helm/ender.py",
     "import os\n\n\ndef end(pid):\n    os.kill(pid, 9)\n"),
    ("public-push", "helm/pub.py", "COMMAND = 'git push origin main'\n"),
    ("guard", "helm/hooks.py", "SPECS = ()\n"))


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit(repo, files, message, branch=None, start=None):
    """Commit `files` ({path: text}) on `branch` off `start`, and return to
    the default branch. Returns the new commit."""
    head = _git(repo, "symbolic-ref", "--short", "HEAD")
    if branch:
        _git(repo, "checkout", "-q", "-b", branch, start or head)
    try:
        for path, text in files.items():
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as stream:
                stream.write(text)
            _git(repo, "add", path)
        _git(repo, "commit", "-q", "-m", message)
        return _git(repo, "rev-parse", "HEAD")
    finally:
        if branch:
            _git(repo, "checkout", "-q", head)


def _iso(seconds):
    """An instant as the harness stamps a transcript line: ISO 8601, UTC,
    to the millisecond, ending in `Z`."""
    return datetime.datetime.fromtimestamp(
        seconds, datetime.timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")


def planted_at():
    """The `timestamp` a planted line carries when its test names none: one
    second after the instant it is planted. Bound (b) admits only a run that
    began in a LATER second than its tip was committed in (git keeps a
    commit's time to the second), and a test commits its tip moments before
    it plants the run that reads it, so the second is what keeps every
    planted run after every commit made before it."""
    return _iso(time.time() + 1)


def before_commit(repo, tip):
    """An instant an hour before `tip` was committed in `repo`, as git keeps
    that time: a run briefed then was alive while the lane was written."""
    return _iso(int(_git(repo, "show", "-s", "--format=%ct", tip)) - 3600)


#: A line's default stamp: `planted_at()`, taken when the line is written.
_PLANTED = object()


def _line(timestamp=_PLANTED, **entry):
    """One transcript line. Every line the harness writes carries its ISO
    `timestamp` (measured: runrecord.read), so a planted one does too:
    `planted_at()` unless the test names another, and none for None."""
    if timestamp is _PLANTED:
        timestamp = planted_at()
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return json.dumps(entry) + "\n"


def _assistant(session, model=OPUS, tools=(), stop=None, at=_PLANTED):
    """One assistant transcript line, with tool_use blocks (name, input). Its
    stop reason is the harness's: `tool_use` when it calls a tool, else
    `end_turn`, the final answer a finished run ends on (`stop` overrides).
    `at` is its timestamp (`_line`)."""
    return _line(type="assistant", sessionId=session, isSidechain=True,
                 timestamp=at,
                 message={"model": model, "role": "assistant", "content": [
                     {"type": "tool_use", "id": "t%d" % n, "name": name,
                      "input": given} for n, (name, given)
                     in enumerate(tools)] or [{"type": "text", "text": "ok"}],
                     "stop_reason": stop or ("tool_use" if tools
                                             else "end_turn")})


def _results(session, tools, at=_PLANTED):
    """The user line that answers an assistant line's tool calls."""
    return _line(type="user", sessionId=session, isSidechain=True,
                 timestamp=at, message={"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t%d" % n,
                      "content": "done"} for n in range(len(tools))]})


def _brief(session, tip, agent=None, at=_PLANTED):
    """The user line that briefs a run: it names the tip the run reads, as
    every real brief does (and every git tool result repeats). It is the
    run's first line, so its `at` is when the run began."""
    return _line(type="user", sessionId=session, agentId=agent,
                 isSidechain=True, timestamp=at,
                 message={"role": "user", "content":
                          "read the lane at %s" % tip})


def plant_agent(root, session, agent=AGENT, model=OPUS, tools=(),
                meta=None, lines=None, tip=TIP, at=_PLANTED):
    """An Agent tool run on disk, in the shape measured on the host: its
    brief, its tool calls and their results, and the final answer a finished
    run ends on, each line stamped `at` (`_line`)."""
    d = os.path.join(root, "-home-u-dev-proj", session, "subagents")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "agent-%s.jsonl" % agent), "w") as stream:
        stream.write(lines if lines is not None else (
            _brief(session, tip, agent, at=at)
            + (_assistant(session, model, tools, at=at)
               + _results(session, tools, at=at) if tools else "")
            + _assistant(session, model, at=at)))
    with open(os.path.join(d, "agent-%s.meta.json" % agent), "w") as stream:
        json.dump(meta or {"agentType": "general-purpose",
                           "description": "fresh read"}, stream)
    return os.path.join(d, "agent-%s.jsonl" % agent)


def plant_workflow(root, session, run=WORKFLOW, agents=("a" + "1" * 16,),
                   record=True, transcripts=None, model=OPUS, tip=TIP,
                   status="completed", at=_PLANTED):
    """A Workflow run on disk: its run record and each agent's transcript,
    each line stamped `at` (`_line`)."""
    base = os.path.join(root, "-home-u-dev-proj", session)
    wdir = os.path.join(base, "subagents", "workflows", run)
    os.makedirs(wdir, exist_ok=True)
    for agent in transcripts if transcripts is not None else agents:
        with open(os.path.join(wdir, "agent-%s.jsonl" % agent), "w") as s:
            s.write(_brief(session, tip, agent, at=at)
                    + _assistant(session, model, at=at))
        with open(os.path.join(wdir, "agent-%s.meta.json" % agent), "w") as s:
            json.dump({"agentType": "workflow-subagent", "model": "opus"}, s)
    if record:
        os.makedirs(os.path.join(base, "workflows"), exist_ok=True)
        with open(os.path.join(base, "workflows", run + ".json"), "w") as s:
            json.dump({"runId": run, "status": status,
                       "workflowProgress": [
                           {"type": "workflow_agent", "agentId": a,
                            "model": model} for a in agents]}, s)


class DoorClassifierTest(unittest.TestCase):
    """review_door.lane_doors over a real repository: which lanes are doors.

    `main` carries a mixed module (helm/chat.py) in the argv-guard's real
    shape: an ordinary function and its constant, a guard, and a helper named
    for what it parses that only the guard calls; a second mixed module
    (helm/steer.py) whose guard reaches what it imports. Plus a hook owner.
    Each lane is one commit off main."""

    CHAT = ("_WIDTH = 72\n\n\ndef render_line(text):\n"
            "    return text[:_WIDTH]\n\n\ndef _spelled_limit():\n"
            "    return 3\n\n\ndef argv_guard(command):\n"
            "    return len(command.split()) > _spelled_limit()\n")

    #: A second mixed module whose guard reaches what it IMPORTS: a module
    #: bound by `import`, a helper bound by `from ... import ... as`, a
    #: guarded import inside a module-level try, and a star import. Its
    #: ordinary function reaches only its own import.
    STEER = ("import shlex\n"
             "from textwrap import shorten\n"
             "\n"
             "from .limits import ceiling as _ceiling\n"
             "from .words import *\n"
             "\n"
             "try:\n"
             "    import fcntl as _lock\n"
             "except ImportError:\n"
             "    _lock = None\n"
             "\n\n"
             "def render(text):\n"
             "    return shorten(text, 40)\n"
             "\n\n"
             "def deny_command(command):\n"
             "    words = shlex.split(command)\n"
             "    return len(words) > _ceiling() and _lock is not None\n")
    #: The import lanes: (plant, the line it replaces, its replacement, the
    #: scope the door must name). Each rebinds what the guard calls, on an
    #: import line no function, class or assignment contains.
    IMPORT_PLANTS = (
        ("import-rebind", "from .limits import ceiling as _ceiling",
         "from .limits import loose as _ceiling", "_ceiling"),
        ("import-module", "import shlex\n", "import shlex_lax as shlex\n",
         "shlex"),
        ("import-guarded", "    import fcntl as _lock",
         "    import msvcrt as _lock", "_lock"),
        ("import-star", "from .words import *", "from .loose_words import *",
         "a star import"),
        ("import-shadow", "    return len(words) > _ceiling() and _lock is "
         "not None\n", "    return len(words) > _ceiling() and _lock is "
         "not None\n\n\nfrom .limits import loose as _ceiling\n",
         "_ceiling"))

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-doors-")
        cls.repo = repo = os.path.join(cls.tmp, "repo")
        os.makedirs(repo)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        cls.main = _commit(repo, {"helm/chat.py": cls.CHAT,
                                  "helm/steer.py": cls.STEER,
                                  "helm/hooks.py": "SPECS = None\n",
                                  "README.md": "fixture\n"}, "base")
        tips = {}
        tips["reversible"] = _commit(repo, {"helm/chat.py": cls.CHAT.replace(
            "_WIDTH = 72", "_WIDTH = 80")}, "wider lines", "l-reversible")
        # THE PLANTED GUARD LANES. No changed line carries a marker word. The
        # first changes the guard itself; the second changes a helper whose
        # name says nothing, which only the guard reaches: over chat.py's
        # real history that is the shape most argv-guard lanes take.
        tips["guard-scope"] = _commit(repo, {"helm/chat.py": cls.CHAT.replace(
            "split()) >", "split()) >=")}, "loosen", "l-guard-scope")
        tips["guard-reach"] = _commit(repo, {"helm/chat.py": cls.CHAT.replace(
            "return 3", "return 4")}, "loosen", "l-guard-reach")
        tips["unparseable"] = _commit(repo, {"helm/chat.py": cls.CHAT.replace(
            "_WIDTH = 72", "_WIDTH = (")}, "broken", "l-unparseable")
        tips["test-and-doc-only"] = _commit(repo, {
            "tests/test_ender.py": "import os\n\n\ndef test_guard_refuses():"
                                   "\n    os.kill(1, 0)\n",
            "docs/PUSH.md": "run git push origin main\n"}, "tests", "l-tests")
        for door, path, text in DOOR_PLANTS:
            tips[door] = _commit(repo, {path: text}, door, "l-" + door)
        for plant, before, after, _scope in cls.IMPORT_PLANTS:
            assert cls.STEER.count(before) == 1, before
            tips[plant] = _commit(repo, {"helm/steer.py": cls.STEER.replace(
                before, after)}, plant, "l-" + plant)
        # THE CONTROL: an import only the ordinary function reaches.
        tips["import-reversible"] = _commit(repo, {
            "helm/steer.py": cls.STEER.replace(
                "from textwrap import shorten",
                "from textwrap import wrap as shorten")}, "wrap", "l-imp-rev")
        cls.tips = tips

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def doors(self, plant, tip=None, **row):
        """The classifier's answer for one planted lane. The lane NAME is
        neutral (`lane/plant`), so only the arm under test can name a door."""
        row = dict({"id": "f" * 16, "lane": "lane/plant",
                    "tip": tip or self.tips[plant], "repo_root": self.repo},
                   **row)
        return review_door.lane_doors(row)

    def classes(self, plant, **kw):
        return {cls for cls, _why in self.doors(plant, **kw)["doors"]}

    def test_a_reversible_lane_has_no_door_and_names_its_paths(self):
        got = self.doors("reversible")
        self.assertEqual(got["doors"], [])
        self.assertEqual(got["paths"], ["helm/chat.py"])
        self.assertEqual((got["base"], got["tip"]),
                         (self.main, self.tips["reversible"]))

    def test_FALSIFIER_the_planted_guard_lanes_are_doors(self):  # noqa: VACUOUS_ASSERTION — each planted lane's door set is asserted EQUAL to {'guard'} and its evidence names the scope; the reversible lane's empty set is the control
        """CL 85: the classifier must tell a guard lane from a reversible
        one in the SAME module. All three lanes change one line of
        helm/chat.py; the two a guard reaches are doors, and each door names
        the scope it changed."""
        got = self.doors("guard-scope")
        self.assertEqual({c for c, _w in got["doors"]}, {"guard"})
        self.assertIn("changes argv_guard, which guards or refuses",
                      review_door.door_line(got["doors"]))
        got = self.doors("guard-reach")
        self.assertEqual({c for c, _w in got["doors"]}, {"guard"})
        self.assertIn("changes _spelled_limit, which argv_guard reaches",
                      review_door.door_line(got["doors"]))
        self.assertEqual(self.classes("reversible"), set())

    def test_FALSIFIER_the_scope_arm_is_what_catches_them(self):
        """A guard is unproven until it has failed: with the scope arm
        removed, both planted guard lanes read REVERSIBLE. So the arm above
        is load-bearing, and not an accident of another arm."""
        with mock.patch.object(review_door, "DOOR_SCOPES", ()):
            self.assertEqual(self.classes("guard-scope"), set())
            self.assertEqual(self.classes("guard-reach"), set())
        self.assertEqual(self.classes("guard-scope"), {"guard"})
        self.assertEqual(self.classes("guard-reach"), {"guard"})

    def test_FALSIFIER_an_import_the_guard_reaches_is_a_door(self):  # noqa: VACUOUS_ASSERTION — IMPORT_PLANTS is a fixed non-empty tuple; each lane's door set is asserted EQUAL to {'guard'} and its evidence names the rebound scope; the control lane's empty set is asserted unconditionally
        """The classifier hole the Fable read found: a lane that rebinds what
        a guard calls ON AN IMPORT LINE changed no function, class or
        assignment, so every scope arm missed it and the lane read
        REVERSIBLE. An import binding is a scope like any other: a guard
        that mentions the name it binds reaches the import statement, and a
        changed line inside that statement is the guard's door. That covers
        a rebound helper, a swapped module, a guarded import inside a
        module-level try, a star import (any name the guard mentions may be
        one it binds), and a new import that shadows the helper. An import
        only the ordinary function reaches stays reversible."""
        for plant, _before, _after, scope in self.IMPORT_PLANTS:
            with self.subTest(plant=plant):
                got = self.doors(plant)
                self.assertEqual({c for c, _w in got["doors"]}, {"guard"},
                                 got["doors"])
                self.assertIn("changes %s, which deny_command reaches"
                              % scope, review_door.door_line(got["doors"]))
        self.assertEqual(self.classes("import-reversible"), set())

    def test_FALSIFIER_the_scope_arm_is_what_catches_the_import_lanes(self):  # noqa: VACUOUS_ASSERTION — IMPORT_PLANTS is a fixed non-empty tuple; each lane is asserted EQUAL to set() with the arm removed and to {'guard'} with it
        """With the scope arm removed every import lane reads REVERSIBLE: no
        path, marker or phrase names them, so the import scopes are what
        make them doors."""
        for plant, _before, _after, _scope in self.IMPORT_PLANTS:
            with self.subTest(plant=plant):
                with mock.patch.object(review_door, "DOOR_SCOPES", ()):
                    self.assertEqual(self.classes(plant), set())
                self.assertEqual(self.classes(plant), {"guard"})

    def test_FALSIFIER_the_owner_arm_is_what_catches_a_hook_owner(self):
        self.assertEqual(self.classes("guard"), {"guard"})
        self.assertIn("helm/hooks.py", review_door.door_line(
            self.doors("guard")["doors"]))
        with mock.patch.object(compose_contract, "PROTECTED_OWNERS", ()):
            self.assertEqual(self.classes("guard"), set())

    def test_each_door_class_is_named(self):  # noqa: VACUOUS_ASSERTION — every plant in DOOR_PLANTS runs (a fixed, non-empty tuple) and each asserts its class and path present
        for door, path, _text in DOOR_PLANTS:
            with self.subTest(door=door):
                got = self.doors(door)
                self.assertEqual({c for c, _w in got["doors"]}, {door},
                                 got["doors"])
                self.assertIn(path, review_door.door_line(got["doors"]))

    def test_an_unparseable_changed_module_FAILS_CLOSED(self):
        got = self.doors("unparseable")
        self.assertEqual({c for c, _w in got["doors"]}, {"unknown"})
        self.assertIn("cannot be parsed", review_door.door_line(got["doors"]))

    def test_a_tip_already_on_trunk_FAILS_CLOSED(self):
        """No range off trunk and no build row: the base is never guessed."""
        got = self.doors("reversible", tip=self.main)
        self.assertEqual([c for c, _w in got["doors"]], ["unknown"])
        self.assertIn("base cannot be told", got["doors"][0][1])

    def test_a_row_with_no_repository_FAILS_CLOSED(self):
        got = self.doors("reversible", repo_root=None)
        self.assertEqual([c for c, _w in got["doors"]], ["unknown"])

    def test_tests_and_docs_carry_no_live_system(self):
        """Their markers (os.kill, git push, a guard-named test) are about no
        live system; the same markers in source are doors (the plants)."""
        self.assertEqual(self.classes("test-and-doc-only"), set())
        self.assertIn("process-kill", self.classes("process-kill"))

    def test_the_phrase_arm_reads_the_lane_and_its_brief(self):
        """Word tokens, as T0 reads them: a hyphen separates words."""
        self.assertEqual(self.classes("reversible",
                                      lane="lane/rotate-credentials-now"),
                         {"credentials"})
        self.assertEqual(self.classes(
            "reversible", message_body="then force push the branch"),
            {"public-push"})
        self.assertEqual(self.classes("reversible", lane="lane/wider-lines",
                                      message_body="wider lines, no more"),
                         set())

    def test_every_T0_phrase_is_a_door_phrase(self):
        self.assertTrue(set(review_door.IRREVERSIBLE_PHRASES)
                        <= set(review_door.DOOR_PHRASES))
        # and T0 itself did not widen: the build door keeps its own list
        self.assertEqual(review_door.irreversible_hits("send the payment"), [])
        self.assertEqual(review_door.irreversible_hits(
            "send the payment", review_door.DOOR_PHRASES), ["payment"])


class RunRecordTest(unittest.TestCase):
    """runrecord.locate/verify over a planted project root."""

    LANE = ["helm/x.py", "tests/test_x.py"]
    #: Where the lane lives: the shared checkout and the lane worktree.
    CHECKOUTS = ("/o", "/w/lane")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-runs-")
        self.root = os.path.join(self.tmp, "projects")
        os.makedirs(self.root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # WHEN THE TIP WAS COMMITTED IS GIT'S ANSWER (runrecord.tip_committed),
        # and this class's tips (TIP, "c" * 40) are no commit of any
        # repository, so that one read is answered here: a minute before the
        # test, earlier than every line it plants (planted_at).
        # RunBeganAfterTipTest asks git itself, over a real repository.
        patch = mock.patch.object(runrecord, "tip_committed",
                                  return_value=(int(time.time()) - 60, None))
        patch.start()
        self.addCleanup(patch.stop)

    def verify(self, run=AGENT, where=None, tip=TIP):
        return runrecord.verify(run, self.LANE, dispatches._OPUS, tip,
                                self.CHECKOUTS,
                                where=[self.root] if where is None else where)

    def test_a_run_whose_transcript_never_names_the_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively, and the same plant verifies against the tip it names
        """A clean Opus run that read SOME OTHER tip is not this lane's read:
        reused on another row, its record would say 'verified on disk' about
        a lane it never opened. The tip comes last, after every other bound,
        so each earlier refusal keeps its name."""
        plant_agent(self.root, READER_SESSION, tip="c" * 40)
        _got, err = self.verify()
        self.assertIn("(b) run %s" % AGENT, err)
        self.assertIn("names the reviewed tip %s" % TIP[:12], err)
        got, err = self.verify(tip="c" * 40)
        self.assertIsNone(err, err)
        self.assertEqual(got["session"], READER_SESSION)

    def test_a_clean_agent_run_verifies(self):  # noqa: VACUOUS_ASSERTION — err is None AND the verified record's kind, session and models are asserted equal to exact values
        plant_agent(self.root, READER_SESSION,
                    tools=[("Read", {"file_path": "/w/lane/helm/x.py"}),
                           ("Write", {"file_path": "/scratch/notes.md"})])
        got, err = self.verify()
        self.assertIsNone(err, err)
        self.assertEqual((got["kind"], got["session"], got["models"]),
                         ("agent", READER_SESSION, {OPUS}))

    def test_the_agent_prefix_names_the_same_run(self):  # noqa: VACUOUS_ASSERTION — err is None AND the session found is asserted equal to the planted one
        plant_agent(self.root, READER_SESSION)
        got, err = self.verify("agent-" + AGENT)
        self.assertIsNone(err, err)
        self.assertEqual(got["session"], READER_SESSION)

    def test_a_MISSING_transcript_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively
        plant_agent(self.root, READER_SESSION)
        _got, err = self.verify("a" + "e" * 16)
        self.assertIn("(b)", err)
        self.assertIn("does not exist", err)

    def test_a_malformed_run_id_is_refused(self):
        _got, err = self.verify("wf-7f3a")
        self.assertIn("neither an Agent run", err)

    def test_a_run_that_WROTE_a_lane_file_is_refused(self):  # noqa: VACUOUS_ASSERTION — four fixed tool plants, each asserting the refusal names the bound and the tool
        for tool, given in (("Edit", {"file_path": "/w/lane/helm/x.py"}),
                            ("Write", {"file_path": "/o/tests/test_x.py"}),
                            ("MultiEdit", {"file_path": "helm/x.py"}),
                            ("NotebookEdit", {}),):
            with self.subTest(tool=tool):
                shutil.rmtree(self.root)
                plant_agent(self.root, READER_SESSION, tools=[(tool, given)])
                _got, err = self.verify()
                self.assertIn("(b)", err)
                self.assertIn(tool, err)
                self.assertIn("lane's own files", err)

    def test_an_edit_counts_only_in_the_shared_checkout_or_the_lane_worktree(
            self):  # noqa: VACUOUS_ASSERTION — each placement is asserted True or False exactly, the private clone False beside the two lane checkouts True
        """A reader following the review procedure commits its cure in its
        OWN clone, off the reviewed tip. That edit is the procedure, not a
        write to the lane: only an edit in the shared checkout or the lane
        worktree changes the lane (integrator ruling, chat row 1915, finding
        4). An edit nobody can place still counts: a relative path naming a
        lane file, or no path at all."""
        lanes = ("/o/shared", "/w/lane")
        self.assertFalse(runrecord.touches("/home/r/clone/helm/x.py",
                                           self.LANE, lanes))
        self.assertFalse(runrecord.touches("/w/lane-cure/helm/x.py",
                                           self.LANE, lanes))
        self.assertTrue(runrecord.touches("/w/lane/helm/x.py", self.LANE,
                                          lanes))
        self.assertTrue(runrecord.touches("/o/shared/tests/test_x.py",
                                          self.LANE, lanes))
        self.assertFalse(runrecord.touches("/o/shared/helm/y.py", self.LANE,
                                           lanes))
        self.assertTrue(runrecord.touches("helm/x.py", self.LANE, lanes))
        self.assertTrue(runrecord.touches(None, self.LANE, lanes))

    def test_a_write_to_a_same_named_file_elsewhere_in_the_path_is_no_touch(
            self):
        self.assertFalse(runrecord.touches("/w/lane/helm/xx.py", self.LANE,
                                           self.CHECKOUTS))
        self.assertFalse(runrecord.touches("/w/lane/myhelm/x.py", self.LANE,
                                           self.CHECKOUTS))
        self.assertTrue(runrecord.touches("/w/lane/helm/x.py", self.LANE,
                                          self.CHECKOUTS))

    def test_the_SPAWNING_session_is_not_judged(self):  # noqa: VACUOUS_ASSERTION — err is None AND the session found is asserted equal to the author's
        """Was test_the_AUTHORS_session_is_refused; the owner's rule
        judges the reading instance, so the session that spawned
        it is no input (task/3658): a run under the author's session
        verifies."""
        plant_agent(self.root, AUTHOR_SESSION)
        got, err = self.verify()
        self.assertIsNone(err, err)
        self.assertEqual(got["session"], AUTHOR_SESSION)

    def test_a_run_whose_OWN_LINE_records_a_fork_is_refused(self):
        """(c) the reading instance's lineage (task/3658): a line of the run's
        own transcript that names the conversation it was forked from."""
        plant_agent(self.root, READER_SESSION, lines=_line(
            type="user", sessionId=READER_SESSION, isSidechain=True,
            forkedFrom={"sessionId": READER_SESSION, "messageUuid": "u-1"},
            message={"role": "user", "content": "read the lane at %s" % TIP})
            + _assistant(READER_SESSION))
        _got, err = self.verify()
        self.assertIn("(c) run %s" % AGENT, str(err))
        self.assertIn("forkedFrom", str(err))

    def test_a_run_that_BEGINS_by_continuing_a_conversation_is_refused(self):  # noqa: VACUOUS_ASSERTION — three fixed shapes, each refusal asserted present, positively
        """(c): a continuation line before any turn of the run's own carries a
        conversation this record does not hold, whoever wrote it: a resumed
        or compacted context from elsewhere."""
        for label, first in (
                ("summary", _line(type="summary", summary="earlier work",
                                  leafUuid="x")),
                ("isCompactSummary", _line(type="user",
                                           sessionId=READER_SESSION,
                                           isCompactSummary=True)),
                ("compact_boundary", _line(type="system",
                                           subtype="compact_boundary",
                                           sessionId=READER_SESSION))):
            with self.subTest(label):
                shutil.rmtree(self.root, ignore_errors=True)
                plant_agent(self.root, READER_SESSION, lines=first + _brief(
                    READER_SESSION, TIP, AGENT) + _assistant(READER_SESSION))
                _got, err = self.verify()
                self.assertIn("(c) run %s" % AGENT, str(err))
                self.assertIn("continues an earlier conversation", str(err))

    def test_a_run_that_COMPACTED_its_own_context_verifies(self):  # noqa: VACUOUS_ASSERTION — err is None AND the verified record's session is asserted equal to the planted one
        """The control, measured on the host: a compacted Agent transcript
        keeps every line before its boundary, so (b) reads all of it, and a
        compaction after the run's own turns holds only its own context."""
        call = [("Read", {"file_path": "/w/lane/helm/x.py"})]
        plant_agent(self.root, READER_SESSION, lines=(
            _brief(READER_SESSION, TIP, AGENT)
            + _assistant(READER_SESSION, tools=call)
            + _results(READER_SESSION, call)
            + _line(type="system", subtype="compact_boundary",
                    sessionId=READER_SESSION)
            + _line(type="user", sessionId=READER_SESSION,
                    isCompactSummary=True,
                    message={"role": "user", "content": "summary"})
            + _assistant(READER_SESSION)))
        got, err = self.verify()
        self.assertIsNone(err, err)
        self.assertEqual(got["session"], READER_SESSION)

    def test_a_FORK_is_refused_by_either_mark(self):
        plant_agent(self.root, READER_SESSION,
                    meta={"agentType": "fork", "isFork": True})
        _got, err = self.verify()
        self.assertIn("FORK", err)
        shutil.rmtree(self.root)
        plant_agent(self.root, READER_SESSION, lines=(
            _line(type="fork-context-ref", agentId=AGENT)
            + _assistant(READER_SESSION)))
        _got, err = self.verify()
        self.assertIn("FORK", err)

    def test_a_missing_meta_is_refused(self):
        path = plant_agent(self.root, READER_SESSION)
        os.remove(path[:-len(".jsonl")] + ".meta.json")
        _got, err = self.verify()
        self.assertIn("fork cannot be told", err)

    def test_a_model_that_is_not_opus_is_refused(self):
        plant_agent(self.root, READER_SESSION, model=FABLE)
        _got, err = self.verify()
        self.assertIn(FABLE, err)
        self.assertIn("contradicts", err)

    def test_a_malformed_line_is_refused(self):
        plant_agent(self.root, READER_SESSION,
                    lines=_assistant(READER_SESSION) + '{"type": "assist')
        _got, err = self.verify()
        self.assertIn("malformed line 2", err)

    def test_a_line_naming_another_session_is_refused(self):
        plant_agent(self.root, READER_SESSION,
                    lines=_assistant(READER_SESSION)
                    + _assistant(AUTHOR_SESSION))
        _got, err = self.verify()
        self.assertIn("names %s" % AUTHOR_SESSION, err)

    def test_a_clean_workflow_run_verifies(self):  # noqa: VACUOUS_ASSERTION — err is None AND the kind and transcript count are asserted equal to exact values
        plant_workflow(self.root, READER_SESSION,
                       agents=("a" + "1" * 16, "a" + "2" * 16))
        got, err = self.verify(WORKFLOW)
        self.assertIsNone(err, err)
        self.assertEqual((got["kind"], len(got["transcripts"])),
                         ("workflow", 2))

    def test_a_workflow_agent_with_no_transcript_is_refused(self):
        plant_workflow(self.root, READER_SESSION,
                       agents=("a" + "1" * 16, "a" + "2" * 16),
                       transcripts=("a" + "1" * 16,))
        _got, err = self.verify(WORKFLOW)
        self.assertIn("no transcript", err)

    def test_a_RUNNING_agent_run_is_refused_until_it_has_finished(self):  # noqa: VACUOUS_ASSERTION — three fixed in-flight shapes, each refusal asserted present, positively; the finished control is asserted to verify with the same brief
        """A run still working has not given its read yet: what it names now
        may not be what it concludes. Its transcript ends on a turn that is
        no final answer: a tool call awaiting its result, the result it has
        not answered yet, or an assistant line still writing (no stop
        reason). Each is refused as in flight (integrator ruling, chat row
        1915, finding 3). The same run with its final answer verifies."""
        call = [("Read", {"file_path": "/w/lane/helm/x.py"})]
        for shape, tail in (
                ("a tool call awaiting its result",
                 _assistant(READER_SESSION, tools=call)),
                ("a result it has not answered",
                 _assistant(READER_SESSION, tools=call)
                 + _results(READER_SESSION, call)),
                ("an answer still writing",
                 _assistant(READER_SESSION, stop="none")
                 .replace('"none"', "null"))):
            with self.subTest(shape=shape):
                shutil.rmtree(self.root)
                plant_agent(self.root, READER_SESSION, lines=_brief(
                    READER_SESSION, TIP, AGENT) + tail)
                _got, err = self.verify()
                self.assertIn("(b) run %s has not finished" % AGENT, str(err))
                self.assertIn("in flight", str(err))
        shutil.rmtree(self.root)
        plant_agent(self.root, READER_SESSION, tools=call)
        _got, err = self.verify()
        self.assertIsNone(err, err)

    def test_a_workflow_that_did_not_COMPLETE_is_refused(self):  # noqa: VACUOUS_ASSERTION — two fixed statuses, each refusal asserted to name the status, positively; the completed control is its own arm above
        """A Workflow's record is written when it ends, and it ends killed or
        failed as well as completed: only a completed one finished its
        read."""
        for status in ("killed", "failed"):
            with self.subTest(status=status):
                shutil.rmtree(self.root)
                plant_workflow(self.root, READER_SESSION, status=status)
                _got, err = self.verify(WORKFLOW)
                self.assertIn("(b) Workflow run %s ended %s" % (WORKFLOW,
                                                                 status),
                              str(err))

    def test_a_RUNNING_workflow_says_so(self):
        plant_workflow(self.root, READER_SESSION, record=False)
        _got, err = self.verify(WORKFLOW)
        self.assertIn("still running", err)

    def test_a_workflow_agent_id_names_its_workflow(self):
        plant_workflow(self.root, READER_SESSION)
        _got, err = self.verify("a" + "1" * 16)
        self.assertIn("name the Workflow run", err)
        self.assertIn(WORKFLOW, err)

    def test_two_distinct_records_are_ambiguous_and_one_linked_root_is_not(  # noqa: VACUOUS_ASSERTION — the linked root's session is asserted equal, and the distinct root's refusal is asserted present
            self):
        plant_agent(self.root, READER_SESSION)
        link = os.path.join(self.tmp, "linked")
        os.symlink(self.root, link)
        got, err = self.verify(where=[self.root, link])
        self.assertIsNone(err, err)
        self.assertEqual(got["session"], READER_SESSION)
        other = os.path.join(self.tmp, "other")
        plant_agent(other, READER_SESSION)
        _got, err = self.verify(where=[self.root, other])
        self.assertIn("2 distinct records", err)


class RunBeganAfterTipTest(unittest.TestCase):
    """Bound (b), when the run BEGAN (task/3658): a recorded run must have
    begun after the reviewed tip was committed.

    THE GAP IT CLOSES (a fresh reader's BLOCK on the lane): with the
    spawner bound gone, (b) is the only test that a run is not the lane's
    author, and its Write/Edit check sees only those four tools, on lane
    files, in the lane's checkouts. A builder subagent that edited through
    Bash (sed -i, a heredoc, python), or in a clone, and committed, left none
    of them, so it could be recorded as the fresh reader of its own work. A
    builder necessarily ran while the lane was written; a fresh reader starts
    after the tip exists. So the run's earliest transcript line is held
    against the tip's committer time, which git reads in the lane's
    checkouts: a real repository here, whose tip is committed at a fixed
    instant so each planted run sits before, in or after its second."""

    LANE = ["g"]
    #: The tip's committer time, and the planted instants around it.
    COMMITTED = "2026-09-29T10:00:00Z"
    BEFORE = "2026-09-29T09:00:00.000Z"
    IN_ITS_SECOND = "2026-09-29T10:00:00.900Z"
    AFTER = "2026-09-29T10:00:01.000Z"
    #: A replacement commit's time, earlier than BEFORE.
    REPLACED = "2026-09-29T08:00:00Z"
    #: The specimen's one edit: through the shell, so no Write or Edit.
    SHELL = [("Bash", {"command": "sed -i s/a/b/ g && git commit -qam cure"})]

    @classmethod
    def setUpClass(cls):
        """Two repositories: `repo` holds the tip, `other` another commit."""
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-began-")
        env = dict(os.environ, GIT_AUTHOR_DATE=cls.COMMITTED,
                   GIT_COMMITTER_DATE=cls.COMMITTED)
        for name in ("repo", "other"):
            repo = os.path.join(cls.tmp, name)
            os.makedirs(repo)
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "Test")
            with open(os.path.join(repo, "g"), "w", encoding="utf-8") as f:
                f.write(name + "\n")
            _git(repo, "add", "g")
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", name],
                           env=env, check=True, capture_output=True)
            setattr(cls, name, repo)
        cls.tip = _git(cls.repo, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="helm-test-began-runs-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def verify(self, checkouts=None, run=AGENT):
        return runrecord.verify(run, self.LANE, dispatches._OPUS, self.tip,
                                (self.repo,) if checkouts is None
                                else checkouts, where=[self.root])

    def builder(self, began):
        """THE SPECIMEN: briefed at `began`, it edits `g` through the shell
        and commits, and every later line is after the tip's commit."""
        plant_agent(self.root, READER_SESSION, lines=(
            _brief(READER_SESSION, self.tip, AGENT, at=began)
            + _assistant(READER_SESSION, tools=self.SHELL,
                         at="2026-09-29T10:00:30.000Z")
            + _results(READER_SESSION, self.SHELL,
                       at="2026-09-29T10:00:31.000Z")
            + _assistant(READER_SESSION, at="2026-09-29T10:01:00.000Z")))

    def test_a_run_that_began_BEFORE_the_tip_was_committed_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its exact words, positively
        """The builder's earliest line, not its last, decides: every line
        after its brief is later than the commit."""
        self.builder(self.BEFORE)
        _got, err = self.verify()
        self.assertIn("(b) run %s began at %s, before the reviewed tip %s was "
                      "committed at %s: a run alive while the lane was being "
                      "written may have written it"
                      % (AGENT, self.BEFORE, self.tip[:12], self.COMMITTED),
                      str(err))

    def test_the_CONTROL_the_same_run_begun_AFTER_the_tip_is_admitted(self):  # noqa: VACUOUS_ASSERTION — err is None AND the run's earliest instant is asserted equal to the planted one, in each checkout order
        """The FALSIFIER of the arm above: the same transcript, shell edit and
        all, begun in the second after the commit, is admitted. So its
        Write/Edit check never saw the shell edit, and when it began is what
        refuses the builder. git is asked in each checkout until one holds
        the tip: a first checkout that cannot date it does not refuse."""
        self.builder(self.AFTER)
        for checkouts in ((self.repo,), (self.other, self.repo)):
            with self.subTest(checkouts=checkouts):
                got, err = self.verify(checkouts)
                self.assertIsNone(err, err)
                self.assertEqual(got["began"][1], self.AFTER)

    def test_the_tips_time_is_git_s_in_the_first_checkout_holding_it(self):  # noqa: VACUOUS_ASSERTION — the answer is asserted EQUAL to the fixture's committer time, in epoch seconds
        committed = int(datetime.datetime.fromisoformat(
            self.COMMITTED.replace("Z", "+00:00")).timestamp())
        self.assertEqual(runrecord.tip_committed(self.tip, (self.other,
                                                            self.repo)),
                         (committed, None))

    def test_a_run_that_began_in_the_tips_own_SECOND_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its words, positively
        """git keeps a commit's time to the second, so a run that began in
        that second cannot be told to have begun after it."""
        plant_agent(self.root, READER_SESSION, tip=self.tip,
                    at=self.IN_ITS_SECOND)
        _got, err = self.verify()
        self.assertIn("(b) run %s began at %s, in the second the reviewed tip "
                      "%s was committed at %s" % (AGENT, self.IN_ITS_SECOND,
                                                  self.tip[:12],
                                                  self.COMMITTED), str(err))
        self.assertIn("may have written it", str(err))

    def test_a_run_with_NO_timestamp_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its words, positively
        plant_agent(self.root, READER_SESSION, tip=self.tip, at=None)
        _got, err = self.verify()
        self.assertIn("(b) run %s: no line of its transcript carries a "
                      "timestamp" % AGENT, str(err))

    def test_a_timestamp_that_names_NO_INSTANT_is_refused(self):  # noqa: VACUOUS_ASSERTION — three fixed shapes, each refusal asserted present, positively
        """A stamp with no zone, no date, or no string cannot be placed in
        time, so when the run began cannot be told from it."""
        for stamp in ("2026-09-29T11:00:00", "later", 1790000000):
            with self.subTest(stamp=stamp):
                shutil.rmtree(self.root)
                plant_agent(self.root, READER_SESSION, tip=self.tip,
                            at=stamp)
                _got, err = self.verify()
                self.assertIn("(b) run %s: its transcript" % AGENT, str(err))
                self.assertIn("line 1 carries a timestamp %r" % stamp,
                              str(err))

    def test_a_tip_NO_CHECKOUT_can_date_is_refused(self):  # noqa: VACUOUS_ASSERTION — each refusal is asserted to name its checkouts, positively
        """A checkout that does not hold the tip, one that does not exist,
        and no checkout at all: when the tip was committed is unknown."""
        plant_agent(self.root, READER_SESSION, tip=self.tip, at=self.AFTER)
        gone = os.path.join(self.tmp, "gone")
        for checkouts, named in (((self.other, gone), (self.other, gone)),
                                 ((), ("none given",))):
            with self.subTest(checkouts=checkouts):
                _got, err = self.verify(checkouts)
                self.assertIn("(b) run %s: no checkout of the lane can read "
                              "when the reviewed tip %s was committed"
                              % (AGENT, self.tip[:12]), str(err))
                for name in named:
                    self.assertIn(name, str(err))

    def test_a_WORKFLOW_whose_one_agent_began_before_the_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its words, and the all-after control is asserted admitted
        """A Workflow run began when its first agent did."""
        agents = ("a" + "1" * 16, "a" + "2" * 16)
        plant_workflow(self.root, READER_SESSION, agents=agents, tip=self.tip,
                       at=self.AFTER)
        got, err = self.verify(run=WORKFLOW)
        self.assertIsNone(err, err)
        with open(os.path.join(self.root, "-home-u-dev-proj", READER_SESSION,
                               "subagents", "workflows", WORKFLOW,
                               "agent-%s.jsonl" % agents[1]), "w") as stream:
            stream.write(_brief(READER_SESSION, self.tip, agents[1],
                                at=self.BEFORE)
                         + _assistant(READER_SESSION, at=self.AFTER))
        _got, err = self.verify(run=WORKFLOW)
        self.assertIn("(b) run %s began at %s, before the reviewed tip %s"
                      % (WORKFLOW, self.BEFORE, self.tip[:12]), str(err))

    def test_the_refusal_names_the_LINE_that_began_first(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted to name the early agent's transcript line, positively, and not the other agent's
        """A Workflow has one transcript per agent, so the refusal names the
        line that holds the earliest stamp, as a stamp that names no instant
        names its own line."""
        agents = ("a" + "1" * 16, "a" + "2" * 16)
        plant_workflow(self.root, READER_SESSION, agents=agents, tip=self.tip,
                       at=self.AFTER)
        early = os.path.join(self.root, "-home-u-dev-proj", READER_SESSION,
                             "subagents", "workflows", WORKFLOW,
                             "agent-%s.jsonl" % agents[1])
        with open(early, "w") as stream:
            stream.write(_brief(READER_SESSION, self.tip, agents[1],
                                at=self.BEFORE)
                         + _assistant(READER_SESSION, at=self.AFTER))
        _got, err = self.verify(run=WORKFLOW)
        self.assertIn("may have written it (its earliest line is %s line 1)"
                      % early, str(err))
        self.assertNotIn(agents[0], str(err))

    def test_a_REPLACE_ref_cannot_move_when_the_tip_was_committed(self):  # noqa: VACUOUS_ASSERTION — the replacement is asserted to move a plain read first, then the tip's time is asserted EQUAL to the fixture's and the early builder asserted refused by its words
        """git dates the tip OBJECT the row names, never a replacement: with
        `refs/replace/<tip>` naming a commit dated 08:00, a plain `git show`
        dates the tip two hours early, and a builder briefed at 09:00 would
        read as begun after it. helm's other authority reads switch
        replacement off the same way (rowworld._history_view_env)."""
        clone = tempfile.mkdtemp(prefix="helm-test-began-replaced-")
        self.addCleanup(shutil.rmtree, clone, ignore_errors=True)
        _git(clone, "clone", "-q", self.repo, ".")
        env = dict(os.environ, GIT_AUTHOR_NAME="Test",
                   GIT_AUTHOR_EMAIL="test@example.com",
                   GIT_COMMITTER_NAME="Test",
                   GIT_COMMITTER_EMAIL="test@example.com",
                   GIT_AUTHOR_DATE=self.REPLACED,
                   GIT_COMMITTER_DATE=self.REPLACED)
        early = subprocess.run(
            ["git", "-C", clone, "commit-tree", "-m", "early",
             self.tip + "^{tree}"], env=env, check=True, capture_output=True,
            text=True).stdout.strip()
        _git(clone, "replace", self.tip, early)

        def epoch(stamp):
            return int(datetime.datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).timestamp())
        self.assertEqual(int(_git(clone, "show", "-s", "--format=%ct",
                                  self.tip)), epoch(self.REPLACED))
        self.assertEqual(runrecord.tip_committed(self.tip, (clone,)),
                         (epoch(self.COMMITTED), None))
        self.builder(self.BEFORE)
        _got, err = self.verify((clone,))
        self.assertIn("(b) run %s began at %s, before the reviewed tip %s was "
                      "committed at %s" % (AGENT, self.BEFORE, self.tip[:12],
                                           self.COMMITTED), str(err))


class FreshOpusVerdictTest(_landreq.LandReqBase):
    """The verdict door end to end: `mark_verdict` and the real CLI.

    The row's sender is `integrator` (HELM_CHAT_NAME), who is also the
    recorder; its roster row records AUTHOR_SESSION, and this process runs
    as RECORDER_SESSION. The reader run is planted under READER_SESSION."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "claude-projects")
        os.makedirs(self.root)
        patch = mock.patch.object(runrecord, "roots",
                                  return_value=[self.root])
        patch.start()
        self.addCleanup(patch.stop)
        # ONE KEY, ONE OWNER: a patch.dict of the whole environment would put
        # this fixture's deleted HELM_HOME back after tearDown.
        from tests._tmphome import own_env
        own_env(self, "CLAUDE_CODE_SESSION_ID", RECORDER_SESSION)
        # The author's roster row: this process's session is its current
        # one, and AUTHOR_SESSION is an earlier session it ran.
        self.roster(RECORDER_SESSION, [AUTHOR_SESSION, RECORDER_SESSION])

    def roster(self, current, sessions):
        """The author's roster row, and the recipient's (a review row's
        recipient must have one)."""
        pk.write_json(seats.roster_path(), {
            "integrator": {"session": current, "sessions": sessions},
            RECIPIENT: {"session": RECIPIENT_SESSION,
                        "sessions": [RECIPIENT_SESSION]}})

    def row(self, ref=None, lane="lane/fresh"):
        """A review row sent by this process, with the add's refusal
        named when there is one."""
        row, why = dispatches.add(RECIPIENT, lane, ref=ref or self.side,
                                  repo=self.repo, kind="review",
                                  new_work=True, notify=False, _reason=True)
        self.assertIsNotNone(row, why)
        return row

    def record(self, row, run=AGENT, model="opus", polarity="concur"):
        return dispatches.mark_verdict(
            row["id"], row["tip"], "read clean", polarity=polarity,
            basis="measured", bind_author=True, reviewer_model=model,
            reviewer_run=run, author_model=OPUS)

    def reads(self, rid):
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        return list(current[rid].get("advisory_reads") or ())

    def door_row(self, door, path, text):
        """A lane carrying one plant, under a NEUTRAL name, so the door the
        refusal names comes from the plant and not from the lane's name."""
        n = [d for d, _p, _t in DOOR_PLANTS].index(door)
        tip = _commit(self.repo, {path: text}, "plant", "plant-%d" % n,
                      self.a)
        return self.row(ref=tip, lane="lane/plant-%d" % n)

    def test_a_REVERSIBLE_lane_records_the_fresh_context_read(self):
        """(a) and (d): admitted, recorded, and the row stays OWED."""
        plant_agent(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        out, err = self.record(row)
        self.assertIsNone(err, err)
        self.assertEqual(out["status"], "open")
        read = self.reads(row["id"])[-1]
        self.assertEqual(
            {k: read.get(k) for k in dispatches.REVIEWER_FIELDS},
            {"reviewer_model": "opus", "reviewer_run": AGENT,
             "author_model": OPUS, "author_model_source": "declared",
             "recorded_by": "integrator", "reviewer_family": "claude",
             "independence": "fresh-context"})

    def test_a_WORKFLOW_run_and_the_authors_own_model_id_are_admitted(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's independence is asserted equal to fresh-context
        plant_workflow(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        _out, err = self.record(row, run=WORKFLOW, model=OPUS)
        self.assertIsNone(err, err)
        self.assertEqual(self.reads(row["id"])[-1]["independence"],
                         "fresh-context")

    def test_lr_show_prints_the_model_and_the_fresh_context_run(self):  # noqa: VACUOUS_ASSERTION — rc 0 on both real CLI entry points, and both outputs asserted to carry the exact fresh-context line
        """(d): through the real CLI, and read back through `lr show`."""
        plant_agent(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(
                ["verdict", row["id"], row["tip"], "--concur", "--measured",
                 "--reviewer-model", "opus", "--reviewer-run", AGENT,
                 "--author-model", OPUS, "read", "clean"])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(landreq.cmd_lr(["show", row["id"]]), 0)
        # Was "stays OWED": the verb now also records the source-clean hold
        # the read carries (task/3658), and both surfaces say so.
        for text in (out.getvalue(), shown.getvalue()):
            self.assertIn("ADVISORY read by model opus (fresh-context run %s, "
                          "recorded (unattested): family claude)" % AGENT,
                          text)
            self.assertIn(HOLD_LINE % (self.side[:12], "integrator"), text)
            self.assertNotIn("stays OWED", text)
            self.assertIsNone(OVERCLAIM.search(" ".join(text.split())), text)

    def test_ONE_VERB_the_authors_own_fresh_read_holds_the_row_on_the_train(self):  # noqa: VACUOUS_ASSERTION — rc 0, the hold is asserted by value off the replayed ledger, and the train's and the close's predicates are asserted to admit it by value
        """THE AUTHOR'S ONE VERB (task/3658): the integrator SENT this row and
        wrote its lane (a chain with no build row names every sender). It
        spawns a fresh subagent of its own, and `helm dispatch verdict ...
        --reviewer-model opus --reviewer-run RUN` records the read AND the
        source-clean hold it carries, so the row rides `helm train` with no
        third seat. The control: the sender's own hold with no such read is
        refused as not the recipient's."""
        row = self.row()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("integrator", None)):
            out, why = dispatches.mark_hold(row["id"], "SOURCE-CLEAN: mine",
                                            source_clean_tip=self.side)
        self.assertIsNone(out)
        self.assertIn("only this row's recipient", str(why))
        plant_agent(self.root, RECORDER_SESSION, tip=self.side)
        said, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(
                ["verdict", row["id"], row["tip"], "--concur", "--measured",
                 "--reviewer-model", "opus", "--reviewer-run", AGENT,
                 "--author-model", OPUS, "read", "clean"])
        self.assertEqual(rc, 0, said.getvalue() + err.getvalue())
        state = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((state["status"], state["hold_actor"],
                          state["source_clean_tip"]),
                         ("held", "integrator", self.side))
        self.assertEqual(state["advisory_reads"][-1]["independence"],
                         "fresh-context")
        self.assertIsNone(landreq.source_clean_holder_error(state))
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(landreq.source_clean_car(lr), (self.side, None))
        self.assertIsNone(state.get("polarity"),
                          "a held read mints no APPROVE: the land gate does")

    def test_ONE_VERB_a_fresh_FIX_holds_nothing(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the row is asserted OPEN by value with the read recorded
        """Only a clean read is a source-clean claim: a fresh-context FIX is
        recorded and the row stays open."""
        row = self.row()
        plant_agent(self.root, READER_SESSION, tip=self.side)
        said, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(err):
            rc = dispatches.cmd_dispatch(
                ["verdict", row["id"], row["tip"], "--fix", "--measured",
                 "--worse-than-main", "g",
                 "--no-patch-because", "design: a meld owns it",
                 "--reviewer-model", "opus", "--reviewer-run", AGENT,
                 "--author-model", OPUS, "found", "one"])
        self.assertEqual(rc, 0, said.getvalue() + err.getvalue())
        state = dispatches.snapshot()[0][row["id"]]
        self.assertEqual((state["status"], state["advisory_reads"][-1][
            "polarity"]), ("open", "fix"))
        self.assertNotIn("source_clean_tip", state)

    def test_NO_SURFACE_says_the_run_was_verified_on_disk(self):  # noqa: VACUOUS_ASSERTION — each surface is asserted to EXIST and be non-empty before its absence is asserted, and the pattern is asserted to fire on the old wording
        """The record is not attested. A process running as the same user can
        run `claude -p` under a session no roster names, plant a transcript,
        or point HELM_CLAUDE_ROOTS at a directory it wrote, and no reader of
        the record can tell. So no surface may say the run was verified or
        proven on disk: the read is 'recorded (unattested)' (integrator
        ruling, chat row 1915, finding 2). Each paragraph that is about a
        model run is read, with wrapped string literals and comment runs
        joined first, so a phrase split across lines is still one phrase;
        a paragraph about something else (a hook file helm checks on disk)
        is not this path."""
        self.assertTrue(OVERCLAIM.search(
            "fresh-context run a1, verified on disk: family claude"))
        self.assertTrue(OVERCLAIM.search("whose run helm verifies on disk"))
        self.assertTrue(OVERCLAIM.search("until helm verifies the run on "
                                         "disk"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        swept = {}
        for rel in SURFACES:
            with self.subTest(surface=rel):
                about = []
                for path, text in surface_sources(root, rel):
                    self.assertTrue(text.strip(), path)
                    swept[os.path.relpath(path, root)] = run_paragraphs(text)
                    about += swept[os.path.relpath(path, root)]
                self.assertTrue(about, rel)
                for joined in about:
                    hit = OVERCLAIM.search(joined)
                    self.assertIsNone(hit, hit and joined[
                        max(0, hit.start() - 80):hit.end() + 40])
        # POSITIVE CONTROL, REACH: every satellite the ledger declares was
        # read, and the spiral's paragraph about a model run's read was
        # among the paragraphs swept.
        spiral = "helm/dispatches_spiral.py"
        self.assertLessEqual({"helm/%s.py" % satellite for satellite, _names
                              in dispatches._OWNER_NAMES}, set(swept))
        self.assertTrue(swept.get(spiral), "no run paragraph read in " + spiral)
        # POSITIVE CONTROL, PREDICATE: an overclaim written into that
        # satellite paragraph is caught by this same sweep.
        with open(os.path.join(root, spiral), encoding="utf-8") as fh:
            text = fh.read()
        planted = text.replace("model run's advisory read is an answer",
                               "model run's advisory read verified on disk is "
                               "an answer", 1)
        self.assertNotEqual(planted, text)
        self.assertTrue(any(OVERCLAIM.search(joined)
                            for joined in run_paragraphs(planted)))

    def assert_recorded(self, row, out, err, run=AGENT):
        """The verified read landed on this row as `fresh-context`, and the
        row stays owed like every model run's read."""
        self.assertIsNone(err, err)
        self.assertEqual(out["status"], "open")
        reads = self.reads(row["id"])
        self.assertEqual(len(reads), 1, reads)
        self.assertEqual((reads[0]["reviewer_run"], reads[0]["independence"],
                          reads[0]["reviewed_tip"]),
                         (run, "fresh-context", row["tip"]))

    def test_a_verified_read_BINDS_each_DOOR_class(self):  # noqa: VACUOUS_ASSERTION — DOOR_PLANTS is a fixed non-empty tuple; each row is asserted to BE its door by the classifier, then its read asserted recorded off the replayed ledger
        """(a) on a door (the owner's ruling, room row 2104):
        a door read needs ONE approval-tier read by a reader that is not the
        author, and a fresh-context Opus read is in the tier. So a verified
        run binds a door row as it binds a reversible one. Before the ruling
        each door refused it ("this lane is a DOOR"). Each door row gets its
        own run: a fresh-context run binds the first row that records it, and
        a second row cannot spend it."""
        for n, (door, path, text) in enumerate(DOOR_PLANTS):
            with self.subTest(door=door):
                row = self.door_row(door, path, text)
                self.assertIn(door, {c for c, _w in review_door.lane_doors(
                    row)["doors"]})
                run = "a%016d" % (n + 1)
                plant_agent(self.root, READER_SESSION, agent=run,
                            tip=row["tip"])
                out, err = self.record(row, run=run)
                self.assert_recorded(row, out, err, run=run)

    def test_the_PLANTED_guard_lane_BINDS_at_the_verdict_door(self):
        """The classifier's falsifier plant, at the door: what a hook
        refuses, changed in a helper only the guard reaches, with no marker
        on the changed line. It is still a guard door, and its verified read
        is recorded."""
        before = DoorClassifierTest.CHAT
        start = _commit(self.repo, {"helm/chat.py": before}, "guard on trunk")
        tip = _commit(self.repo, {"helm/chat.py": before.replace(
            "return 3", "return 4")}, "loosen", "guard-lane", start)
        row = self.row(ref=tip, lane="lane/loosen")
        self.assertIn("guard (helm/chat.py changes _spelled_limit, which "
                      "argv_guard reaches", review_door.door_line(
                          review_door.lane_doors(row)["doors"]))
        plant_agent(self.root, READER_SESSION, tip=tip)
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_an_OPUS_read_of_a_Claude_sent_row_BINDS_whatever_model_it_declares(self):  # noqa: VACUOUS_ASSERTION — the read is asserted recorded off the replayed ledger with exact fields; the declared model is asserted to be another family's first
        """The live refusal "opus vs a gpt author (the lane author is
        Claude)": the sender's runtime proves Claude and the recorder
        declared a GPT author model, so the Opus reader was the same family
        as one of them, and the arm stood aside because the author was not
        Claude alone. Independence here is by CONTEXT, which (b) and (c)
        prove, so the author's family does not decide it."""
        declared = "gpt-5.6-sol"
        self.assertEqual(dispatches._model_family(declared), "codex")
        plant_agent(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        with mock.patch.object(dispatches, "_runtime_families",
                               side_effect=lambda seat: {"claude"}
                               if seat == "integrator" else set()):
            out, err = dispatches.mark_verdict(
                row["id"], row["tip"], "read clean", polarity="concur",
                basis="measured", bind_author=True, reviewer_model="opus",
                reviewer_run=AGENT, author_model=declared)
        self.assert_recorded(row, out, err)
        self.assertEqual(self.reads(row["id"])[0]["author_model"], declared)

    def test_on_a_DOOR_row_every_bound_still_refuses(self):  # noqa: VACUOUS_ASSERTION — CASES is a fixed non-empty tuple; each refusal is asserted present, positively, and the ledger asserted empty; the verified control on the same door row is recorded last
        """MUST-MISS: the ruling admits the door, never a weaker read. On a
        money door, a run whose own line records a fork (c), a missing
        record, a run in flight, a run that edited the door's own file in the
        shared checkout, a run that began before the door's tip was
        committed, a fork, a run of another tip and a Fable transcript named
        as Opus (b), and Sonnet and Haiku, are each refused, and nothing is
        recorded. The same row then records the verified read.
        The author's two sessions left this list (task/3658): a fresh
        subagent of the author's own session is a reading instance too."""
        door, path, text = next(p for p in DOOR_PLANTS if p[0] == "money")
        row = self.door_row(door, path, text)
        tip = row["tip"]
        wrote = [("Edit", {"file_path": os.path.join(self.repo, path)})]
        call = [("Read", {"file_path": os.path.join(self.repo, path)})]
        cases = (
            ("a fork mark on its own line", lambda: plant_agent(
                self.root, READER_SESSION, lines=_line(
                    type="user", sessionId=READER_SESSION,
                    forkedFrom={"sessionId": AUTHOR_SESSION},
                    message={"role": "user", "content": tip})
                + _assistant(READER_SESSION)), "opus",
             "(c) run %s" % AGENT),
            ("missing record", lambda: None, "opus",
             "(b) no record of run %s" % AGENT),
            ("in flight", lambda: plant_agent(
                self.root, READER_SESSION, lines=_brief(
                    READER_SESSION, tip, AGENT) + _assistant(
                        READER_SESSION, tools=call)), "opus",
             "(b) run %s has not finished" % AGENT),
            ("wrote the door file", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip, tools=wrote), "opus",
             "made Edit to the lane's own files"),
            ("began before the tip", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip,
                at=before_commit(self.repo, tip)), "opus",
             "before the reviewed tip %s was committed" % tip[:12]),
            ("fork", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip,
                meta={"agentType": "fork"}), "opus", "FORK"),
            ("another tip", lambda: plant_agent(
                self.root, READER_SESSION, tip=self.side), "opus",
             "names the reviewed tip %s" % tip[:12]),
            ("fable named as opus", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip, model=FABLE), "opus",
             "contradicts the declared reader"),
            ("sonnet", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip), "sonnet",
             "never reviews anything"),
            ("haiku", lambda: plant_agent(
                self.root, READER_SESSION, tip=tip), "claude-haiku-4-5",
             "never reviews anything"))
        self.assertEqual(len(cases), 10)
        for name, plant, model, said in cases:
            with self.subTest(case=name):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                plant()
                out, err = self.record(row, model=model)
                self.assertIsNone(out)
                self.assertIn(said, str(err))
                self.assertEqual(self.reads(row["id"]), [])
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        plant_agent(self.root, READER_SESSION, tip=tip)
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_a_lane_whose_changed_files_cannot_be_read_still_FAILS_CLOSED(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively, and the ledger asserted empty
        """(b): no run can be shown to have left the lane unwritten when the
        lane's changed files cannot be told. A tip already on trunk has no
        range off it and no build row names its base."""
        row = self.row(ref=self.c, lane="lane/on-trunk")
        self.assertEqual(review_door.lane_doors(row)["paths"], [])
        plant_agent(self.root, READER_SESSION, tip=self.c)
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("(b) the lane's changed files cannot be read", str(err))
        self.assertIn("base cannot be told", str(err))
        self.assertEqual(self.reads(row["id"]), [])

    def test_GEMINI_and_LOCAL_models_stay_input(self):  # noqa: VACUOUS_ASSERTION — MODELS is a fixed non-empty tuple; each refusal is asserted present, positively, and never the fresh-context arm's; the ledger asserted empty
        """MUST-MISS: the arm is Opus's alone. A gemini or a local model
        reading its own family's work is refused as before, and the fresh-
        context arm never takes it up, even with a clean run on disk."""
        row = self.door_row(*next(p for p in DOOR_PLANTS if p[0] == "money"))
        for model in ("gemini-3.8-flash-high", "qwenlocal", "bonsai"):
            with self.subTest(model=model):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                plant_agent(self.root, READER_SESSION, tip=row["tip"],
                            model=model)
                out, err = dispatches.mark_verdict(
                    row["id"], row["tip"], "read clean", polarity="concur",
                    basis="measured", bind_author=True, reviewer_model=model,
                    reviewer_run=AGENT, author_model=model)
                self.assertIsNone(out)
                self.assertIn("IS the author's model", str(err))
                self.assertNotIn("A fresh-context Opus run is recorded only",
                                 str(err))
                self.assertEqual(self.reads(row["id"]), [])

    def test_a_MISSING_transcript_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively
        """(b)"""
        row = self.row()
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("(b) no record of run %s" % AGENT, err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_run_that_EDITED_a_lane_file_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present; the empty read list is read off the ledger a sibling arm proves a read lands on
        """(b): the fixture lane's one file is `g`."""
        plant_agent(self.root, READER_SESSION,
                    tools=[("Edit", {"file_path": os.path.join(self.repo,
                                                               "g")})])
        row = self.row()
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("made Edit to the lane's own files", err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_BUILDER_that_edited_through_the_SHELL_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its words with the ledger empty; the same transcript begun after the tip is asserted RECORDED off the replayed ledger
        """THE GAP (task/3658, a fresh reader's BLOCK): with the spawner bound
        gone, (b) alone tells a reader from the lane's builder, and its
        Write/Edit check sees no shell edit. This builder was briefed an hour
        before its tip, edited `g` with sed -i and committed it: no Write or
        Edit is on record, and it is refused because it began before the
        tip was committed. FALSIFIER: the same transcript begun after the
        tip is recorded, so when it began is what refuses it."""
        tip = _commit(self.repo, {"g": "the builder's cure\n"}, "cure",
                      "shell-lane", self.a)
        row = self.row(ref=tip, lane="lane/shell")
        shell = [("Bash", {"command": "sed -i s/side/cure/ %s && git commit "
                           "-qam cure" % os.path.join(self.repo, "g")})]

        def plant(began):
            shutil.rmtree(self.root)
            os.makedirs(self.root)
            plant_agent(self.root, READER_SESSION, lines=(
                _brief(READER_SESSION, tip, AGENT, at=began)
                + _assistant(READER_SESSION, tools=shell)
                + _results(READER_SESSION, shell)
                + _assistant(READER_SESSION)))
        plant(before_commit(self.repo, tip))
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("(b) run %s began at" % AGENT, str(err))
        self.assertIn("before the reviewed tip %s was committed" % tip[:12],
                      str(err))
        self.assertEqual(self.reads(row["id"]), [])
        plant(planted_at())
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_an_edit_in_a_reviewers_PRIVATE_CLONE_is_admitted(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's independence is asserted equal to fresh-context
        """(b), finding 4: the reader committed its cure to the lane's own
        file `g`, in a clone of its own off the reviewed tip, as the review
        procedure says. That is no write to the lane."""
        clone = os.path.join(self.tmp, "reader-clone")
        subprocess.run(["git", "clone", "-q", self.repo, clone], check=True,
                       capture_output=True)
        plant_agent(self.root, READER_SESSION, tip=self.side,
                    tools=[("Edit", {"file_path": os.path.join(clone, "g")})])
        row = self.row()
        _out, err = self.record(row)
        self.assertIsNone(err, err)
        self.assertEqual(self.reads(row["id"])[-1]["independence"],
                         "fresh-context")

    def lane_worktree(self):
        """The lane's own worktree: checked out on the branch the row's ref
        resolved through, in helm's room for it."""
        wt = os.path.join(self.tmp, "repo-wt", "side")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", wt,
                        "side"], check=True, capture_output=True)
        return wt

    def test_an_edit_in_the_LANE_WORKTREE_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively, and the ledger is asserted empty
        """(b), finding 4, the other side: an edit to `g` in the lane's own
        worktree changed the lane, so the run did not only read it."""
        wt = self.lane_worktree()
        plant_agent(self.root, READER_SESSION, tip=self.side,
                    tools=[("Edit", {"file_path": os.path.join(wt, "g")})])
        row = self.row()
        self.assertEqual(row.get("ref_branch"), "refs/heads/side")
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("made Edit to the lane's own files", str(err))
        self.assertIn(os.path.join(wt, "g"), str(err))
        self.assertEqual(self.reads(row["id"]), [])

    def test_FALSIFIER_the_lane_worktree_is_what_refuses_it(self):  # noqa: VACUOUS_ASSERTION — the same planted edit is asserted RECORDED when the worktree is dropped from the lane's checkouts, and refused above when it is not
        """A guard is unproven until it has failed: with the lane worktree
        dropped from where the lane lives, the same edit reads as a private
        clone's and is recorded. So the worktree arm is load-bearing."""
        wt = self.lane_worktree()
        plant_agent(self.root, READER_SESSION, tip=self.side,
                    tools=[("Edit", {"file_path": os.path.join(wt, "g")})])
        row = self.row()
        real = review_door.lane_checkouts

        rooms = os.path.realpath(os.path.join(self.tmp, "repo-wt"))

        def shared_only(*args, **kw):
            found, why = real(*args, **kw)
            return [p for p in found or () if not os.path.realpath(
                p).startswith(rooms)], why
        with mock.patch.object(review_door, "lane_checkouts", shared_only):
            _out, err = self.record(row)
        self.assertIsNone(err, err)
        self.assertEqual(self.reads(row["id"])[-1]["independence"],
                         "fresh-context")

    def test_a_run_spawned_by_the_RECIPIENTS_session_is_admitted(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's recorder and independence are asserted equal to exact values
        """The ordinary route: the row's recipient records its own Opus run
        on the author's row. Its session is not the author's."""
        plant_agent(self.root, RECIPIENT_SESSION, tip=self.side)
        row = self.row()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(RECIPIENT, None)):
            _out, err = self.record(row)
        self.assertIsNone(err, err)
        read = self.reads(row["id"])[-1]
        self.assertEqual((read["recorded_by"], read["independence"]),
                         (RECIPIENT, "fresh-context"))

    def test_a_run_still_IN_FLIGHT_is_refused_and_nothing_is_recorded(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively, and the ledger is asserted empty; the finished run is recorded by the (a) arm above
        """(b), at the verdict door: an Agent run whose transcript ends on a
        tool call is still working, so no verdict is recorded for it."""
        call = [("Read", {"file_path": os.path.join(self.repo, "g")})]
        plant_agent(self.root, READER_SESSION, lines=_brief(
            READER_SESSION, self.side, AGENT) + _assistant(READER_SESSION,
                                                           tools=call))
        row = self.row()
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("(b) run %s has not finished" % AGENT, str(err))
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_run_that_read_ANOTHER_tip_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted present, positively, and the ledger is asserted empty
        """(b): a clean Opus run whose transcript names some other tip is no
        read of this row's tip."""
        plant_agent(self.root, READER_SESSION, tip=self.a)
        row = self.row()
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("(b) run %s" % AGENT, err)
        self.assertIn("names the reviewed tip %s" % self.side[:12], err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_run_already_recorded_on_one_row_cannot_read_another(self):  # noqa: VACUOUS_ASSERTION — the first row positively records the run; the second is asserted to refuse it and remain empty
        """A transcript naming a tip is still one read, not a reusable token.

        Two rows may ask different questions about the same tree. Without a
        ledger-wide run binding, the same final answer records clean on both.
        """
        plant_agent(self.root, READER_SESSION, tip=self.side)
        first = self.row(lane="lane/first-question")
        second = self.row(lane="lane/second-question")
        _out, err = self.record(first)
        self.assertIsNone(err, err)
        out, err = self.record(second)
        self.assertIsNone(out)
        self.assertIn("already recorded on dispatch %s" % first["id"][:12],
                      str(err))
        out, err = self.record(second, run="agent-" + AGENT)
        self.assertIsNone(out)
        self.assertIn("already recorded on dispatch %s" % first["id"][:12],
                      str(err))
        self.assertEqual(self.reads(second["id"]), [])
        # Replay owns the same refusal. A hand-appended second v4 event cannot
        # bypass the writer's ledger-wide binding.
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable)
        before = current[second["id"]]
        read = self.reads(first["id"])[0]
        event = dict(read, v=4, event=dispatches.ADVISORY_READ_EVENT,
                     id=second["id"], seq=before["seq"] + 1)
        self.assertIs(dispatches._apply(before, event, current=current), before)

    def test_a_spent_run_is_refused_naming_what_to_take_instead(self):  # noqa: VACUOUS_ASSERTION — the first row positively records the run; the refusal is asserted to carry the remedy
        """Every refusal on this door names what to take instead
        (REVIEWER_FIELDS: "every refusal names `review_remedy`"), and a spent
        run's remedy starts with a NEW fresh-context run of this row's tip."""
        plant_agent(self.root, READER_SESSION, tip=self.side)
        first = self.row(lane="lane/first-question")
        second = self.row(lane="lane/second-question")
        _out, err = self.record(first)
        self.assertIsNone(err, err)
        out, err = self.record(second)
        self.assertIsNone(out)
        self.assertIn("record a NEW fresh-context run that read %s"
                      % second["tip"][:12], str(err))
        self.assertIn(dispatches.review_remedy(second["id"]), str(err))

    def test_a_RETIPPED_row_refuses_its_own_spent_run_by_name(self):  # noqa: VACUOUS_ASSERTION — the first record and the retip are asserted to succeed; the refusal is asserted to name the row
        """The same run on the SAME row at its new tip is a second spend of
        one answer too. The writer's binding let it past (the owner is this
        row) and the reducer then refused it with no name; the refusal names
        the row that holds the run, as it does for another row."""
        def lane_tip(base, name):
            self.git("checkout", "-q", "-b", name, base)
            with open(os.path.join(self.repo, "lane.txt"), "w",
                      encoding="utf-8") as f:
                f.write("the lane's own work\n")
            self.git("add", "lane.txt")
            self.git("commit", "-q", "-m", "lane work")
            tip = self.git("rev-parse", "HEAD")
            self.git("checkout", "-q", self.main)
            self.git("clean", "-qfd")
            return tip

        old, new = lane_tip(self.a, "lane-before"), lane_tip(self.c,
                                                             "lane-after")
        plant_agent(self.root, READER_SESSION, lines=(
            _brief(READER_SESSION, old, AGENT)
            + _brief(READER_SESSION, new, AGENT)
            + _assistant(READER_SESSION)))
        row = self.row(ref=old, lane="lane/retipped")
        _out, err = self.record(row)
        self.assertIsNone(err, err)
        moved, err = dispatches.retip(row["id"], new, reason="rebased",
                                      repo=self.repo, notify=False)
        self.assertIsNone(err, err)
        self.assertEqual(moved["tip"], new)
        out, err = self.record(moved)
        self.assertIsNone(out)
        self.assertIn("already recorded on dispatch %s" % row["id"][:12],
                      str(err))
        self.assertEqual([r["reviewed_tip"] for r in self.reads(row["id"])],
                         [old])

    def test_a_retry_in_the_OTHER_spelling_is_said_back(self):
        """A retry spelled agent-<id> is the same run (`_advisory_run_key`),
        so the writer reports the row as it stands, and the real CLI says
        that read back as it did the first time rather than printing
        nothing on rc 0."""
        plant_agent(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        for run in (AGENT, "agent-" + AGENT):
            with self.subTest(run=run):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = dispatches.cmd_dispatch(
                        ["verdict", row["id"], row["tip"], "--concur",
                         "--measured", "--reviewer-model", "opus",
                         "--reviewer-run", run, "--author-model", OPUS,
                         "read", "clean"])
                self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
                self.assertIn("ADVISORY read by model opus (fresh-context "
                              "run %s, recorded (unattested)" % AGENT,
                              out.getvalue())
        self.assertEqual(len(self.reads(row["id"])), 1)

    def test_a_fresh_subagent_of_the_AUTHORS_own_session_is_recorded(self):  # noqa: VACUOUS_ASSERTION — two fixed sessions, each read asserted recorded fresh-context off the replayed ledger
        """Was test_the_AUTHORS_own_session_is_refused: the owner's rule
        ("Sub agent of your own") judges the reading instance,
        so the author's roster session and this recorder's own session each
        spawn a counted reader (task/3658)."""
        for n, session in enumerate((AUTHOR_SESSION, RECORDER_SESSION)):
            with self.subTest(session=session):
                run = "a%016d" % (n + 11)
                plant_agent(self.root, session, agent=run, tip=self.side)
                row = self.row(lane="lane/own-" + session[:4])
                out, err = self.record(row, run=run)
                self.assert_recorded(row, out, err, run=run)

    def test_an_author_with_no_recorded_session_is_recorded(self):  # noqa: VACUOUS_ASSERTION — assert_recorded asserts err is None and the one read's run, independence and tip by value
        """Was ..._is_refused: the author's sessions are no input of the
        reading-instance test (task/3658), so an author the roster records no
        session for takes a verified read like any other."""
        plant_agent(self.root, READER_SESSION, tip=self.side)
        row = self.row()
        pk.write_json(seats.roster_path(), {RECIPIENT: {
            "session": RECIPIENT_SESSION}})
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "",
                                          "CLAUDE_SESSION_ID": "",
                                          "CODEX_SESSION_ID": ""}):
            out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def chain_row(self):
        """An INTEGRATOR-sent review of a lane LANE_AUTHOR wrote: LANE_AUTHOR
        sends the lane's first round, and the integrator sends the next one
        in the same chain (`supersedes`). That row's sender is not the
        lane's author. Each seat is rostered on its own session."""
        pk.write_json(seats.roster_path(), {
            "integrator": {"session": RECORDER_SESSION,
                           "sessions": [AUTHOR_SESSION, RECORDER_SESSION]},
            LANE_AUTHOR: {"session": LANE_AUTHOR_SESSION,
                          "sessions": [LANE_AUTHOR_SESSION]},
            RECIPIENT: {"session": RECIPIENT_SESSION,
                        "sessions": [RECIPIENT_SESSION]}})
        with mock.patch.dict(os.environ, {
                "HELM_CHAT_NAME": LANE_AUTHOR,
                "CLAUDE_CODE_SESSION_ID": LANE_AUTHOR_SESSION}):
            first = self.row(lane="lane/chain")
        row, why = dispatches.add(RECIPIENT, "lane/chain", ref=self.side,
                                  repo=self.repo, kind="review",
                                  supersedes=first["id"], notify=False,
                                  _reason=True)
        self.assertIsNotNone(row, why)
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        wrote, _approved, err = landreq.chain_contributors(
            row, index=landreq._contributor_chains(current, None)[0])
        self.assertIsNone(err, err)
        self.assertEqual(
            (first["sender"], row["sender"], row["chain_root"], set(wrote)),
            (LANE_AUTHOR, "integrator", first["id"],
             {LANE_AUTHOR, "integrator"}),
            "fixture premise: the chain records LANE_AUTHOR's round and the "
            "integrator sent the row read, or no arm here is about (c)")
        return row

    def test_a_run_from_a_LANE_AUTHORS_session_is_recorded_on_a_row_it_did_not_send(self):  # noqa: VACUOUS_ASSERTION — assert_recorded asserts err is None and the one read's run, independence and tip by value
        """Was ..._is_refused_...: the contractor premise gave way to the
        owner's rule ("as long as an agent's context is fresh, it
        counts"): a fresh run LANE_AUTHOR's session spawned is a reading
        instance that wrote none of the lane (task/3658)."""
        row = self.chain_row()
        plant_agent(self.root, LANE_AUTHOR_SESSION, tip=self.side)
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_on_that_chain_the_same_run_from_an_UNRELATED_session_binds(self):  # noqa: VACUOUS_ASSERTION — assert_recorded asserts err is None and exactly one read on the replayed ledger, with its run, independence and tip equal to exact values
        """The control: the same run shape on the same chain row, spawned
        from a session no author of the lane is recorded on, is recorded."""
        row = self.chain_row()
        plant_agent(self.root, READER_SESSION, tip=self.side)
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_on_that_chain_the_SENDERS_own_session_is_recorded(self):  # noqa: VACUOUS_ASSERTION — assert_recorded asserts err is None and the one read's run, independence and tip by value
        """Was ..._still_refuses: the sender's session is no input of the
        reading-instance test either (task/3658)."""
        row = self.chain_row()
        plant_agent(self.root, AUTHOR_SESSION, tip=self.side)
        out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_an_UNREADABLE_chain_is_no_input_of_the_verdict_door(self):  # noqa: VACUOUS_ASSERTION — assert_recorded asserts err is None and the one read's run, independence and tip by value
        """Was test_a_chain_whose_authors_CANNOT_BE_READ_refuses...: the
        verdict door no longer reads who wrote the chain (task/3658), so an
        unreadable chain refuses at the doors that do: the source-clean hold
        and the land (TheHoldDoorAsksWhoWroteCodeTest)."""
        row = self.chain_row()
        plant_agent(self.root, READER_SESSION, tip=self.side)
        with mock.patch.object(landreq, "chain_contributors", return_value=(
                frozenset(), (), "linked chain identity is UNKNOWN")):
            out, err = self.record(row)
        self.assert_recorded(row, out, err)

    def test_an_ADVISORY_read_alone_never_derives_APPROVED_resolved_or_subsumed(self):  # noqa: VACUOUS_ASSERTION — each read is asserted RECORDED fresh-context first; then each door is asserted to refuse with the ledger unchanged
        """The owner's ruling names SEATS ("opus seats should be in the
        upper tier"): a seat's verdict is an attested act of
        the seat, and a fresh-context run's record is unattested (task/2966),
        so it gains no land or close authority on any lane. A recorded read
        is no APPROVE the derivation counts, and no confirmation `subsumed`
        or `resolved` closes on."""
        original = self.row(lane="lane/advisory-subsumed")
        _out, err = self.mark_verdict(original["id"], original["tip"],
                                      "original verdict", polarity="approve")
        self.assertIsNone(err, err)
        later = _commit(self.repo, {"later.txt": "reimplemented\n"},
                        "reimplemented", "advisory-later")
        confirming, why = dispatches.add(
            RECIPIENT, "lane/advisory-subsumed", ref=later, repo=self.repo,
            kind="review", supersedes=original["id"], notify=False,
            _reason=True)
        self.assertIsNotNone(confirming, why)
        plant_agent(self.root, READER_SESSION, tip=later)
        out, err = self.record(confirming)
        self.assert_recorded(confirming, out, err)
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        self.assertIsNone(current[confirming["id"]].get("polarity"))
        self.assertNotIn(confirming["id"],
                         rowworld.non_author_approvals(current, {}))
        before = len(dispatches.history(original["id"]))
        out, err = landreq.close(original["id"], "subsumed",
                                 evidence=confirming["id"][:12],
                                 trunk=self.main)
        self.assertIsNone(out, err)
        self.assertEqual(len(dispatches.history(original["id"])), before)
        # resolved closes a SUPERSEDE whose own tip reached trunk.
        resolved = self.row(lane="lane/advisory-resolved")
        _out, err = self.mark_verdict(resolved["id"], resolved["tip"],
                                      "findings", polarity="supersede")
        self.assertIsNone(err, err)
        self.git("merge", "--no-edit", "-q", "side")
        tip = _commit(self.repo, {"confirmed.txt": "confirmed\n"},
                      "confirmed", "advisory-confirming")
        confirming, why = dispatches.add(
            RECIPIENT, "lane/advisory-confirming", ref=tip, repo=self.repo,
            kind="review", new_work=True, notify=False, _reason=True)
        self.assertIsNotNone(confirming, why)
        run = "a%016d" % 7
        plant_agent(self.root, READER_SESSION, agent=run, tip=tip)
        out, err = self.record(confirming, run=run)
        self.assert_recorded(confirming, out, err, run=run)
        before = len(dispatches.history(resolved["id"]))
        out, err = landreq.close(resolved["id"], "resolved",
                                 evidence=confirming["id"][:12])
        self.assertIsNone(out, err)
        self.assertEqual(len(dispatches.history(resolved["id"])), before)

    def test_a_FORK_is_refused(self):
        plant_agent(self.root, READER_SESSION, meta={"agentType": "fork"})
        out, err = self.record(self.row())
        self.assertIsNone(out)
        self.assertIn("FORK", err)

    def test_SONNET_HAIKU_and_FABLE_as_Opus_are_unchanged(self):  # noqa: VACUOUS_ASSERTION — the closing fable read is asserted RECORDED with independence fable, unconditionally
        """Sonnet and Haiku are refused before any run is read; a Fable
        transcript cannot be recorded as an Opus read; a Fable read of an
        Opus author stays the ordinary `fable` read."""
        plant_agent(self.root, READER_SESSION, model=FABLE)
        row = self.row()
        for model in ("sonnet", "claude-haiku-4-5"):
            with self.subTest(model=model):
                out, err = self.record(row, model=model)
                self.assertIsNone(out)
                self.assertIn("never reviews anything", err)
        out, err = self.record(row)
        self.assertIsNone(out)
        self.assertIn("contradicts the declared reader", err)
        out, err = self.record(row, model="fable")
        self.assertIsNone(err, err)
        self.assertEqual(self.reads(row["id"])[-1]["independence"], "fable")

    def test_APPROVE_is_still_refused(self):
        plant_agent(self.root, READER_SESSION)
        out, err = self.record(self.row(), polarity="approve")
        self.assertIsNone(out)
        self.assertIn("--concur", err)

    def test_the_reducer_refuses_a_fresh_context_read_by_another_family(self):
        """Replay refuses what the writer refuses: `fresh-context` belongs to
        an Opus model of the Claude family only."""
        state = {"tip": self.side}
        event = {"reviewed_tip": self.side, "polarity": "concur",
                 "ts": dispatches.pk.now_ts(), "verdict_ref": "read",
                 "reviewer_model": "opus", "reviewer_run": AGENT,
                 "author_model": OPUS, "author_model_source": "declared",
                 "recorded_by": "integrator", "reviewer_family": "claude",
                 "independence": "fresh-context"}
        record, err = dispatches._advisory_record(event, state)
        self.assertIsNone(err, err)
        self.assertEqual(record["independence"], "fresh-context")
        for key, value in (("reviewer_family", "codex"),
                           ("reviewer_model", "fable")):
            with self.subTest(key=key):
                _record, err = dispatches._advisory_record(
                    dict(event, **{key: value}), state)
                self.assertIn("independence fields", err)


#: The lane author of ReadingInstanceTest, the session that wrote its lane
#: (a previous day's context), and the fresh session it runs in today.
SEAT = "seat-s"
WRITING_SESSION = "12121212-3434-4565-8787-909090909090"
FRESH_SESSION = "34343434-5656-4787-8909-121212121212"
#: A session SEAT runs under a NEW id that continues WRITING_SESSION: a
#: `--resume --fork-session`, a new-id continuation, a pruned copy. It joins
#: under SEAT's name, so the roster records it as one of SEAT's sessions.
FORK_SESSION = "56565656-7878-4909-8121-343434343434"


def _transcript(session, *extra, **fields):
    """A seat session's own transcript: a user turn and an assistant turn
    that name only `session`, then `extra` lines."""
    return "".join((
        _line(type="user", sessionId=session, isSidechain=False,
              parentUuid=None, uuid="u-1", message={"role": "user",
                                                    "content": "hello"},
              **fields),
        _line(type="assistant", sessionId=session, isSidechain=False,
              parentUuid="u-1", uuid="u-2",
              message={"model": OPUS, "role": "assistant",
                       "content": [{"type": "text", "text": "hi"}],
                       "stop_reason": "end_turn"}))) + "".join(extra)


class ReadingInstanceTest(_landreq.LandReqBase):
    """INDEPENDENCE IS JUDGED PER READING INSTANCE (task/3483, task/3658),
    under the owner's rule: "as long as an agent's context is
    fresh, it counts. context is fresh" and "Sub agent of your own".

    THE LIVE CASE: SEAT built the lane and handed it back from
    WRITING_SESSION. SEAT is sent the lane to read by the integrator and
    spawns a fresh subagent. That run's read is recorded as the
    fresh-context read, and SEAT's source-clean hold on it stands.

    WHAT IS JUDGED IS THE RUN, never the session that spawned it: its own
    record (b), finished, no fork, naming the tip, no Write or Edit of the
    lane, and its own lineage (c), no fork mark and no conversation it
    continues before a turn of its own. So a run spawned from the session
    that wrote the lane, from a fork, resume, /clear or compaction of it,
    from a session with no start record or transcript, and on a brief the
    author wrote for its own lane, is each recorded (task/3658; before the
    cure bound (c) refused each by its spawner). What still refuses: a fork
    subagent, a run that edited a lane file, a run whose own line records a
    fork, and a run whose transcript opens by continuing a conversation."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "claude-projects")
        os.makedirs(self.root)
        patch = mock.patch.object(runrecord, "roots",
                                  return_value=[self.root])
        patch.start()
        self.addCleanup(patch.stop)
        from tests._tmphome import own_env
        own_env(self, "CLAUDE_CODE_SESSION_ID", RECORDER_SESSION)
        pk.write_json(seats.roster_path(), {
            "integrator": {"session": RECORDER_SESSION,
                           "sessions": [RECORDER_SESSION]},
            SEAT: {"session": FRESH_SESSION,
                   "sessions": [WRITING_SESSION, FORK_SESSION,
                                FRESH_SESSION]},
            RECIPIENT: {"session": RECIPIENT_SESSION,
                        "sessions": [RECIPIENT_SESSION]}})

    def send(self, sender, session, recipient, tip, kind="review",
             parent=None, lane="lane/instance"):
        """One row sent by `sender` from `session` ("" sends from no
        session, as a legacy or bare-CLI send did)."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": sender,
                                          "CLAUDE_CODE_SESSION_ID": session,
                                          "CLAUDE_SESSION_ID": session,
                                          "CODEX_SESSION_ID": session}):
            row, why = dispatches.add(
                recipient, lane, ref=tip, repo=self.repo, kind=kind,
                notify=False, new_work=parent is None,
                supersedes=parent["id"] if parent else None, force=True,
                _reason=True)
        self.assertIsNone(why, why)
        return row

    def lane(self, handback_session=WRITING_SESSION, lane="lane/instance"):
        """(handback, asked): the integrator sends SEAT the build, SEAT hands
        its tip back from `handback_session`, and the integrator sends SEAT
        that tip to read. The chain records SEAT as its one writer."""
        build = self.send("integrator", RECORDER_SESSION, SEAT, self.a,
                          kind="build", lane=lane)
        handback = self.send(SEAT, handback_session, RECIPIENT, self.side,
                             parent=build, lane=lane)
        asked = self.send("integrator", RECORDER_SESSION, SEAT, self.side,
                          parent=handback, lane=lane)
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        wrote, err = landreq.chain_authors(current[asked["id"]], current)
        self.assertIsNone(err, err)
        self.assertEqual((set(wrote), asked["sender"], asked["recipient"]),
                         ({SEAT}, "integrator", SEAT),
                         "fixture premise: SEAT is the lane's one writer and "
                         "the integrator wrote the brief SEAT answers")
        return handback, asked

    def started(self, session, source="startup", kind="interactive",
                after=True, transcript=None):
        """`session`'s SessionStart join recorded how it began: `source` and
        `kind`, an hour after (or before) now, which is when the fixture's
        rows were sent. Its own transcript is planted beside its runs."""
        from helm import sessionstart
        sessionstart.note_session_start(session, source, kind, ts=pk.epoch_ts(
            time.time() + (3600 if after else -3600)))
        if transcript is not False:
            base = os.path.join(self.root, "-home-u-dev-proj")
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, session + ".jsonl"), "w") as stream:
                stream.write(_transcript(session) if transcript is None
                             else transcript)

    def record(self, row, recorder=SEAT, session=FRESH_SESSION, run=AGENT):
        """`recorder`, running in `session`, records the planted run's read."""
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(recorder, None)), \
                mock.patch.dict(os.environ,
                                {"CLAUDE_CODE_SESSION_ID": session}):
            return dispatches.mark_verdict(
                row["id"], row["tip"], "read clean", polarity="concur",
                basis="measured", bind_author=True, reviewer_model="opus",
                reviewer_run=run, author_model=OPUS)

    def reads(self, rid):
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        return list(current[rid].get("advisory_reads") or ())

    def hold(self, row, actor=SEAT):
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=(actor, None)):
            return dispatches.mark_hold(row["id"], "SOURCE-CLEAN: read clean",
                                        source_clean_tip=self.side)

    def refused(self, row, out, err, *said):
        self.assertIsNone(out)
        for text in said:
            self.assertIn(text, str(err))
        self.assertEqual(self.reads(row["id"]), [])

    def admitted(self, row, out, err, run=AGENT):
        """The run's read is recorded fresh-context on `row`, and SEAT's
        source-clean hold on it stands at the close's own holder rung."""
        self.assertIsNone(err, err)
        read = self.reads(row["id"])[-1]
        self.assertEqual((read["reviewer_run"], read["recorded_by"],
                          read["independence"]),
                         (run, SEAT, "fresh-context"))
        if row["recipient"] == SEAT:
            held, why = self.hold(row)
            self.assertIsNone(why, why)
            state = dispatches.snapshot()[0][row["id"]]
            self.assertEqual((held["status"], state["hold_actor"]),
                             ("held", SEAT))
            self.assertIsNone(landreq.source_clean_holder_error(state))

    def test_LIVE_CASE_a_fresh_run_of_the_authoring_seat_is_recorded(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's run, recorder and independence are asserted equal to exact values
        """SEAT's fresh session spawned the run on the integrator's brief."""
        _handback, asked = self.lane()
        self.started(FRESH_SESSION)
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        out, err = self.record(asked)
        self.assertIsNone(err, err)
        self.assertEqual(out["status"], "open")
        read = self.reads(asked["id"])[-1]
        self.assertEqual((read["reviewer_run"], read["recorded_by"],
                          read["independence"]),
                         (AGENT, SEAT, "fresh-context"))

    def test_LIVE_CASE_the_source_clean_hold_on_that_read_stands(self):  # noqa: VACUOUS_ASSERTION — the hold is asserted HELD and stamped by value, and the close's own holder rung is asserted to agree
        """The hold door and the close's condition 1 read the same instance:
        SEAT's own clean claim rests on the fresh-context read it recorded at
        the held tip, so the hold stands and a land can close on it."""
        _handback, asked = self.lane()
        self.started(FRESH_SESSION)
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        _out, err = self.record(asked)
        self.assertIsNone(err, err)
        out, why = self.hold(asked)
        self.assertIsNone(why, why)
        state = dispatches.snapshot()[0][asked["id"]]
        self.assertEqual((out["status"], state["hold_actor"],
                          state["source_clean_tip"]),
                         ("held", SEAT, self.side))
        self.assertIsNone(landreq.source_clean_holder_error(state))

    def test_without_that_read_the_authors_hold_is_still_LANE_AUTHOR(self):
        """The control: the same hold with no fresh-context read on the row is
        the author's own claim, and refuses as it always did."""
        _handback, asked = self.lane()
        out, why = self.hold(asked)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", str(why))

    def test_THE_SPECIMEN_a_run_from_the_session_that_WROTE_the_lane_is_recorded(self):  # noqa: VACUOUS_ASSERTION — admitted() asserts err is None, the read by value, the hold HELD and the close's holder rung None
        """task/3658's specimen, which bound (c) refused ("that session wrote
        this lane"): the owner's rule judges the reading instance, and a
        fresh subagent of the writing session wrote none of the lane."""
        _handback, asked = self.lane()
        plant_agent(self.root, WRITING_SESSION, tip=self.side)
        out, err = self.record(asked, session=WRITING_SESSION)
        self.admitted(asked, out, err)

    def test_the_SPAWNER_is_no_input_whatever_its_lineage(self):  # noqa: VACUOUS_ASSERTION — CASES is a fixed non-empty tuple; each case's read is asserted recorded and its hold HELD by value
        """Each case below was a refusal of bound (c), which judged the
        session that spawned the run (task/3483); each is now recorded,
        because a non-fork subagent starts from its brief and holds none of
        its spawner's context (task/3658). One line per retired refusal."""
        fork = _transcript("{s}", forkedFrom={"sessionId": WRITING_SESSION,
                                             "messageUuid": "u-9"})
        cases = (
            # was test_a_run_from_a_FORK_of_the_writing_session_is_refused
            ("a resumed fork of the writing session", "resume", None),
            # was test_a_CONTINUATION_of_the_writing_session_is_refused
            ("a /clear continuation", "clear", None),
            # was test_a_COMPACTED_continuation_is_refused
            ("a compacted continuation", "compact", None),
            # was ..._whose_transcript_CARRIES_the_writing_session_is_refused
            ("a transcript naming the writing session", "startup",
             _transcript("{s}") + _transcript(WRITING_SESSION)),
            # was test_a_session_whose_transcript_records_a_FORK_is_refused
            ("a transcript recording a fork", "startup", fork),
            # was test_a_session_whose_START_IS_UNRECORDED_is_refused
            ("no start record", None, None),
            # was test_a_session_with_NO_TRANSCRIPT_is_refused
            ("no transcript", "startup", False),
            # was test_a_session_begun_BEFORE_the_hand_back_is_refused
            ("a start before the hand-back", "before", None),
            # was test_a_session_run_as_a_FORK_PROCESS_is_refused
            ("a fork process", "fork-kind", None),
            # was test_a_fresh_session_with_an_EMPTY_transcript_is_refused
            ("an empty transcript", "startup", ""),
            # was test_a_fresh_session_that_CONTINUES_a_summary_is_refused
            ("a transcript continuing a summary", "startup",
             _line(type="summary", summary="earlier", leafUuid="x")
             + _transcript("{s}")),
            # was test_a_cut_last_line_naming_the_writing_session_is_refused
            ("a cut last line naming the writing session", "startup",
             _transcript("{s}") + '{"type": "user", "sessionId": "%s", "me'
             % WRITING_SESSION))
        self.assertEqual(len(cases), 12)
        for n, (label, source, transcript) in enumerate(cases):
            with self.subTest(label):
                session = "%08d-5656-4787-8909-%012d" % (n + 1, n + 1)
                _handback, asked = self.lane(lane="lane/spawner-%d" % n)
                if source is not None:
                    self.started(
                        session,
                        source="startup" if source in ("before", "fork-kind")
                        else source,
                        kind="fork" if source == "fork-kind"
                        else "interactive", after=source != "before",
                        transcript=transcript if transcript is False
                        or transcript is None
                        else transcript.replace("{s}", session))
                run = "a%016d" % (n + 21)
                plant_agent(self.root, session, agent=run, tip=self.side)
                out, err = self.record(asked, session=session, run=run)
                self.admitted(asked, out, err, run=run)

    def test_a_FORK_subagent_from_the_fresh_session_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present, positively, and the ledger asserted empty
        _handback, asked = self.lane()
        self.started(FRESH_SESSION)
        plant_agent(self.root, FRESH_SESSION, tip=self.side,
                    meta={"agentType": "fork", "isFork": True})
        out, err = self.record(asked)
        self.refused(asked, out, err, "FORK")

    def test_a_run_whose_own_line_records_a_FORK_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted to name the bound and the mark, the ledger asserted empty and the hold asserted LANE AUTHOR
        """(c) the reading instance's own lineage: a fork of an author
        context under a fresh-looking meta still names where it came from."""
        _handback, asked = self.lane()
        plant_agent(self.root, FRESH_SESSION, lines=_line(
            type="user", sessionId=FRESH_SESSION, forkedFrom={
                "sessionId": WRITING_SESSION, "messageUuid": "u-9"},
            message={"role": "user", "content": self.side})
            + _assistant(FRESH_SESSION))
        out, err = self.record(asked)
        self.refused(asked, out, err, "(c) run %s" % AGENT, "forkedFrom")
        out, why = self.hold(asked)
        self.assertIsNone(out)
        self.assertIn("LANE AUTHOR", str(why))

    def test_a_run_that_EDITED_a_lane_file_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present, positively, and the ledger asserted empty
        _handback, asked = self.lane()
        self.started(FRESH_SESSION)
        plant_agent(self.root, FRESH_SESSION, tip=self.side,
                    tools=[("Edit", {"file_path": os.path.join(self.repo,
                                                               "g")})])
        out, err = self.record(asked)
        self.refused(asked, out, err, "made Edit to the lane's own files")

    def test_a_brief_the_AUTHOR_wrote_for_its_own_lane_is_recorded(self):  # noqa: VACUOUS_ASSERTION — admitted() asserts err is None and the read by value
        """Was ..._is_refused: the owner's rule drops the author-sent-brief
        exclusion (task/3658). SEAT's own hand-back row, recorded by SEAT as
        its sender, takes the read of a fresh subagent of SEAT's session."""
        handback, _asked = self.lane()
        self.started(FRESH_SESSION)
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        out, err = self.record(handback)
        self.admitted(handback, out, err)

    def test_a_writing_session_the_ledger_did_not_record_is_recorded(self):  # noqa: VACUOUS_ASSERTION — admitted() asserts err is None, the read by value and the hold HELD
        """Was ..._FAILS_CLOSED: which session wrote the lane is no input of
        the reading-instance test any more (task/3658)."""
        _handback, asked = self.lane(handback_session="")
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        out, err = self.record(asked)
        self.admitted(asked, out, err)

    def test_a_run_from_a_session_no_author_ran_is_recorded(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's independence is asserted equal to fresh-context
        """The control the old rule already admitted: the integrator, which
        wrote none of the lane, records a run from a session no author is
        recorded on, on the same chain."""
        _handback, asked = self.lane()
        plant_agent(self.root, READER_SESSION, tip=self.side)
        _out, err = self.record(asked, recorder="integrator",
                                session=RECORDER_SESSION)
        self.assertIsNone(err, err)
        self.assertEqual(self.reads(asked["id"])[-1]["independence"],
                         "fresh-context")

    def test_a_builder_session_that_EDITED_the_lane_spawns_an_ADMITTED_reader(self):  # noqa: VACUOUS_ASSERTION — err is None AND the replayed read's run and independence are asserted equal to exact values
        """The owner's ruling (2026-09-28 21:32 PDT): "I don't really think
        it's a problem if the same Master seat reviews cuz it would give a
        fresh context sub-agent the job". FRESH_SESSION wrote the lane itself
        and then spawned a fresh reader: the READING INSTANCE is fresh, so
        what its spawner did does not refuse it (the reader's own Edit still
        does, under (b))."""
        _handback, asked = self.lane()
        self.started(FRESH_SESSION, transcript=_transcript(FRESH_SESSION)
                     + _assistant(FRESH_SESSION, tools=[(
                         "Edit", {"file_path": os.path.join(self.repo,
                                                            "g")})]))
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        out, err = self.record(asked)
        self.assertIsNone(err, err)
        read = self.reads(asked["id"])[-1]
        self.assertEqual((read["reviewer_run"], read["independence"]),
                         (AGENT, "fresh-context"))

    def test_the_one_predicate_admits_the_instance_and_refuses_its_own_edit(self):  # noqa: VACUOUS_ASSERTION — both poles asserted by value on the predicate every door calls
        """`dispatches.reading_instance_is_fresh` is the one instance-level
        test: a fresh reader a lane-writing session spawned is fresh; the
        same reader whose OWN transcript edits the lane is not, and neither
        is the same reader begun before the lane's tip was committed."""
        from helm import review_door
        _handback, asked = self.lane()
        self.started(FRESH_SESSION, transcript=_transcript(FRESH_SESSION)
                     + _assistant(FRESH_SESSION, tools=[(
                         "Edit", {"file_path": os.path.join(self.repo,
                                                            "g")})]))
        current, _err = dispatches.snapshot()
        row = current[asked["id"]]
        lane = review_door.lane_doors(row, current)
        checkouts, err = review_door.lane_checkouts(row, current)
        self.assertIsNone(err, err)

        def ask():
            return dispatches.reading_instance_is_fresh(
                AGENT, lane["paths"], lane["tip"], checkouts)
        plant_agent(self.root, FRESH_SESSION, tip=self.side)
        self.assertEqual(ask(), (True, None))
        plant_agent(self.root, FRESH_SESSION, tip=self.side, tools=[(
            "Edit", {"file_path": os.path.join(self.repo, "g")})])
        fresh, why = ask()
        self.assertFalse(fresh)
        self.assertIn("made Edit to the lane's own files", why)
        plant_agent(self.root, FRESH_SESSION, tip=self.side,
                    at=before_commit(self.repo, self.side))
        fresh, why = ask()
        self.assertFalse(fresh)
        self.assertIn("before the reviewed tip %s was committed"
                      % self.side[:12], str(why))

    def test_an_UNREADABLE_run_record_is_refused_naming_it(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted to name the meta file, and the ledger asserted empty
        """UNKNOWN still refuses, naming the input: a run whose meta file
        cannot be read cannot be told from a fork."""
        _handback, asked = self.lane()
        path = plant_agent(self.root, FRESH_SESSION, tip=self.side)
        with open(path[:-len(".jsonl")] + ".meta.json", "w") as stream:
            stream.write("{not json")
        out, err = self.record(asked)
        self.refused(asked, out, err, "(b)", "meta file", "cannot be read")

    def test_a_session_start_is_recorded_ONCE_by_the_join(self):  # noqa: VACUOUS_ASSERTION — the record is asserted equal by value after a second start of the same id
        """The SessionStart join records how a session BEGAN, and a later
        start of the same id (a resume, a compaction) never rewrites it."""
        from helm import sessionstart
        with mock.patch.object(seats_join, "session_kind",
                               return_value="interactive"):
            seats_join.join(session=FRESH_SESSION, cwd=self.repo,
                            seat=SEAT, session_source="startup",
                            require_pane=False)
            seats_join.join(session=FRESH_SESSION, cwd=self.repo,
                            seat=SEAT, session_source="resume",
                            require_pane=False)
        got = sessionstart.session_start(FRESH_SESSION)
        self.assertEqual((got["session"], got["source"], got["kind"]),
                         (FRESH_SESSION, "startup", "interactive"))
        self.assertIsNone(sessionstart.session_start(FORK_SESSION))


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats). FreshOpusVerdictTest
    takes LandReqBase's plant; DoorClassifierTest and RunRecordTest write
    none but run in a module that does, which the census counts."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


if __name__ == "__main__":
    unittest.main()
