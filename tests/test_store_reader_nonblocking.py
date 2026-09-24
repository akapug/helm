#!/usr/bin/env python3
"""A store or config reader answers at once when its path is not a regular file.

`open()` on a FIFO with no writer never returns. task/2530, 2536 and 2523
cured that for pk.read_json and the roster reader; task/2543 took an ast
census of every other `json.load` of a file helm opens by path and found 70
more readers that opened blocking, across 40-odd modules, plus private
`_read_json` helpers and whole-file `json.loads(f.read())` reads. They now
open through `pk.open_regular`, which adds O_NONBLOCK and refuses anything but
a regular file with `pk.NotRegularFile` (an OSError AND a ValueError, so each
reader answers it the way it already answers an unreadable or unparseable
file).

ONE ARM PER READER CONTRACT, NOT PER SITE: lenient defaults, tri-state
(value, error) readers, strict readers that raise, census readers that collect
what they could not read, whole-file reads, the os.open reader under
helm.work, and a credential store reader (synthetic JSON only, never a real
token). Each case plants a real FIFO at the path the shipped reader opens.

THREE ARMS PER CONTRACT, and the second is the one that tells the type check
apart from a parse failure: a non-blocking read of a writerless FIFO ends at
EOF and fails to parse anyway, so only a FIFO whose writer already queued
VALID JSON proves the reader refused the file type. The regular-file control
proves the same payload reads, so each refusal is not the only answer the
reader can give.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests._tmphome import own_env
from helm import (actors, chat, codexbudget, codexhomes, dispatches, drain,
                  envtidy, gate, harnesses, homes, inflight_gate, physics, pk,
                  providers, proxywatch, seat, seats_cursor, takeover,
                  tasksmirror, turnresponse)
from helm.work import _common as work_common

PAYLOAD = {"k": "v"}


def _within(test, fn, fifo, seconds=5):
    """fn() on a thread with a deadline. A reader blocked opening `fifo` is
    released by opening its write end, so a RED arm fails instead of wedging
    the suite. A reader that opened a fed FIFO blocking stays blocked in its
    read until the arm closes the writer, which the arm's `finally` does."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 — re-raised below
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        t.join(5)
        test.fail("blocked for %ss on the FIFO at %s" % (seconds, fifo))
    if "error" in box:
        raise box["error"]
    return box["value"]


def _outcome(fn):
    """("value", v) or ("raised", exc): a strict reader's refusal is a raise,
    and the deadline thread must hand it back rather than lose it."""
    try:
        return "value", fn()
    except Exception as e:              # noqa: BLE001 — the arm inspects it
        return "raised", e


class _Case:
    def __init__(self, name, path, call, refused, regular, payload=PAYLOAD):
        self.name, self.path, self.call = name, path, call
        self.refused, self.regular, self.payload = refused, regular, payload


class _ReaderContract:
    """Drives each case's shipped reader against a FIFO with no writer, a FIFO
    fed valid JSON, and a regular file holding the same JSON. A mixin, not a
    TestCase, so the loader never runs it without cases."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-nonblock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        own_env(self, "HELM_HOME", os.path.join(self.tmp, "home"))
        own_env(self, "HELM_CACHE_DIR", os.path.join(self.tmp, "cache"))
        own_env(self, "HELM_ACTORS", os.path.join(self.tmp, "actors.json"))

    def cases(self):
        raise NotImplementedError

    def _planted(self, case, fifo):
        """The case's real path holding a FIFO or the payload, removed after
        the subtest either way, since two cases may share one path."""
        path = case.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if fifo:
            os.mkfifo(path)
            self.assertTrue(stat.S_ISFIFO(os.stat(path).st_mode))
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(case.payload, f)
            self.assertTrue(stat.S_ISREG(os.stat(path).st_mode))
        return path

    def test_a_fifo_with_no_writer_is_refused_at_once(self):  # noqa: VACUOUS_ASSERTION — `answered` must equal every case name, a positive list; each refusal is asserted by the case against the regular answer the third arm proves
        cases, answered = self.cases(), []
        for case in cases:
            with self.subTest(reader=case.name):
                path = self._planted(case, fifo=True)
                try:
                    got = _within(self, lambda: _outcome(case.call), path)
                    answered.append(case.name)
                    case.refused(self, got, path)
                finally:
                    os.unlink(path)
        self.assertEqual(answered, [c.name for c in cases])

    def test_a_fifo_fed_valid_json_is_still_refused(self):  # noqa: VACUOUS_ASSERTION — `answered` must equal every case name, a positive list; the queued JSON is exactly the payload the regular-file arm reads back
        cases, answered = self.cases(), []
        for case in cases:
            with self.subTest(reader=case.name):
                path = self._planted(case, fifo=True)
                writer = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                try:
                    os.write(writer, json.dumps(case.payload).encode("utf-8"))
                    got = _within(self, lambda: _outcome(case.call), path)
                    answered.append(case.name)
                    case.refused(self, got, path)
                finally:
                    os.close(writer)
                    os.unlink(path)
        self.assertEqual(answered, [c.name for c in cases])

    def test_a_regular_file_with_the_same_json_still_reads(self):  # noqa: VACUOUS_ASSERTION — each case asserts its reader returned the planted payload, and `answered` must equal every case name
        cases, answered = self.cases(), []
        for case in cases:
            with self.subTest(reader=case.name):
                path = self._planted(case, fifo=False)
                try:
                    got = _outcome(case.call)
                    answered.append(case.name)
                    case.regular(self, got)
                finally:
                    os.unlink(path)
        self.assertEqual(answered, [c.name for c in cases])


def _names_the_type(test, text):
    test.assertIn("not a regular file", str(text))


class OpenRegularTest(unittest.TestCase):
    """The shared door itself."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-openreg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "store.json")

    def test_a_fifo_raises_not_regular_file_as_both_error_kinds(self):
        os.mkfifo(self.path)

        def read():
            with self.assertRaises(pk.NotRegularFile) as cm:
                pk.open_regular(self.path, encoding="utf-8")
            return cm.exception
        err = _within(self, read, self.path)
        self.assertIsInstance(err, OSError)
        self.assertIsInstance(err, ValueError)
        self.assertIn(self.path, str(err))
        _names_the_type(self, err)

    def test_a_fed_fifo_is_refused_in_text_and_binary_mode(self):
        os.mkfifo(self.path)
        writer = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        os.write(writer, b'{"k": "v"}')
        refused = []
        for args in (("r",), ("rb",)):
            with self.subTest(mode=args[0]):
                def read():
                    with self.assertRaises(pk.NotRegularFile):
                        pk.open_regular(self.path, *args)
                    return args[0]
                refused.append(_within(self, read, self.path))
        self.assertEqual(refused, ["r", "rb"])
        self.assertEqual(os.read(writer, 64), b'{"k": "v"}',
                         "the queued JSON was there to read all along")

    def test_regular_symlinked_missing_and_directory_paths_keep_open_answers(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"k": "v"}')
        with pk.open_regular(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), PAYLOAD)
        with pk.open_regular(self.path, "rb") as f:
            self.assertEqual(f.read(), b'{"k": "v"}')
        link = self.path + ".link"
        os.symlink(self.path, link)
        with pk.open_regular(link, encoding="utf-8") as f:
            self.assertEqual(json.load(f), PAYLOAD)
        with self.assertRaises(FileNotFoundError):
            pk.open_regular(os.path.join(self.tmp, "absent.json"))
        with self.assertRaises(IsADirectoryError):
            pk.open_regular(self.tmp)

    def test_a_refusal_closes_what_it_opened(self):  # noqa: VACUOUS_ASSERTION — the descriptor census is shown to count a held descriptor (before + 1) on the same observable
        os.mkfifo(self.path)
        # A WRITER IS HELD FIRST, so no open in the loop can block even if the
        # door lost O_NONBLOCK: this arm is about closing, and an unbounded
        # open of a writerless FIFO here would wedge the run instead of
        # failing it (the blocking arms go through `_within`).
        writer = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            with self.assertRaises(pk.NotRegularFile):
                pk.open_regular(self.path)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        # CONTROL: the census does count a descriptor this process holds.
        reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before + 1)

    def test_a_double_of_open_still_sees_the_path_and_the_callers_arguments(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{}")
        seen = []
        real = open

        def spy(path, *args, **kwargs):
            seen.append((path, args, sorted(kwargs)))
            return real(path, *args, **kwargs)
        with mock.patch("builtins.open", spy):
            with pk.open_regular(self.path, "rb") as f:
                body = f.read()
        self.assertEqual(body, b"{}")
        self.assertEqual(seen, [(self.path, ("rb",), ["opener"])])


class LenientReadersAnswerTheirDefaultTest(_ReaderContract, unittest.TestCase):
    """Readers that answer a default for any file they cannot read."""

    def cases(self):
        tmp = self.tmp

        def none_refused(test, got, path):
            test.assertEqual(got, ("value", None))

        def payload_read(test, got):
            test.assertEqual(got, ("value", PAYLOAD))
        out = [_Case("%s._read_json" % mod.__name__,
                     lambda mod=mod: os.path.join(tmp, mod.__name__ + ".json"),
                     lambda mod=mod: mod._read_json(
                         os.path.join(tmp, mod.__name__ + ".json")),
                     none_refused, payload_read)
               for mod in (codexhomes, homes, providers)]
        out.append(_Case(
            "harnesses._load_codex_cache", harnesses._codex_cache_path,
            harnesses._load_codex_cache,
            lambda test, got, path: test.assertEqual(got, ("value", {})),
            payload_read))
        budget = {"ts": time.time(), "rows": [{"home": "h"}]}
        out.append(_Case(
            "codexbudget.cached_budget", codexbudget.snapshot_path,
            codexbudget.cached_budget,
            lambda test, got, path: test.assertEqual(got, ("value", (None, None))),
            lambda test, got: test.assertEqual(got[1][0], [{"home": "h"}]),
            payload=budget))
        return out


class TriStateReadersReportAnErrorTest(_ReaderContract, unittest.TestCase):
    """Readers whose answer separates missing from unreadable."""

    def cases(self):
        tmp = self.tmp

        def err_names_type(index):
            def refused(test, got, path):
                test.assertEqual(got[0], "value")
                _names_the_type(test, got[1][index])
            return refused
        settings = os.path.join(tmp, "claude")
        return [
            _Case("proxywatch._read_watch_state", proxywatch._state_path,
                  proxywatch._read_watch_state, err_names_type(1),
                  lambda test, got: test.assertEqual(got, ("value", (PAYLOAD, None)))),
            _Case("envtidy._read_json", lambda: os.path.join(tmp, "e.json"),
                  lambda: envtidy._read_json(os.path.join(tmp, "e.json")),
                  lambda test, got, path: (
                      test.assertIsNone(got[1][0]), _names_the_type(test, got[1][1])),
                  lambda test, got: test.assertEqual(got, ("value", (PAYLOAD, None)))),
            _Case("physics._read_json", lambda: os.path.join(tmp, "p.json"),
                  lambda: physics._read_json(os.path.join(tmp, "p.json")),
                  lambda test, got, path: test.assertEqual(
                      got, ("value", (None, "%s: NotRegularFile" % path))),
                  lambda test, got: test.assertEqual(got, ("value", (PAYLOAD, None)))),
            _Case("dispatches._marker_file", dispatches.epoch_path,
                  dispatches._marker_file,
                  lambda test, got, path: test.assertEqual(got, ("value", (None, True))),
                  lambda test, got: test.assertEqual(got, ("value", (PAYLOAD, True)))),
            _Case("turnresponse.stop_hook_timeout_s",
                  lambda: os.path.join(settings, "settings.json"),
                  lambda: turnresponse.stop_hook_timeout_s(root=settings),
                  lambda test, got, path: (
                      test.assertIsNone(got[1][0]),
                      test.assertIn("settings.json: NotRegularFile", got[1][1])),
                  lambda test, got: test.assertEqual(
                      got, ("value", (turnresponse.DEFAULT_HOOK_TIMEOUT_S, None)))),
        ]


class StrictReadersRaiseTest(_ReaderContract, unittest.TestCase):
    """Readers that refuse by raising, each in its own error type."""

    def cases(self):
        tmp = self.tmp
        spawn_dir = os.path.join(tmp, "seat")

        def raised(kind, text):
            def refused(test, got, path):
                test.assertEqual(got[0], "raised", got)
                test.assertIsInstance(got[1], kind)
                test.assertIn(text, str(got[1]))
            return refused

        def payload_read(test, got):
            test.assertEqual(got, ("value", PAYLOAD))
        return [
            _Case("chat._read_event_receipts", lambda: os.path.join(tmp, "r.events.json"),
                  lambda: chat._read_event_receipts(os.path.join(tmp, "r.events.json")),
                  raised(ValueError, "unreadable"),
                  lambda test, got: test.assertEqual(got, ("value", {})), payload={}),
            _Case("seats_cursor._strict_json", lambda: os.path.join(tmp, "c.json"),
                  lambda: seats_cursor._strict_json(os.path.join(tmp, "c.json")),
                  raised(OSError, "unreadable"), payload_read),
            _Case("takeover._strict_spawn_record",
                  lambda: os.path.join(spawn_dir, "spawn.json"),
                  lambda: takeover._strict_spawn_record(spawn_dir),
                  raised(takeover.TakeoverRefused, "NotRegularFile"), payload_read),
            _Case("work._common._load_json_nofollow", lambda: os.path.join(tmp, "m.json"),
                  lambda: work_common._load_json_nofollow(os.path.join(tmp, "m.json")),
                  raised(OSError, "not a regular file"), payload_read),
        ]


class CensusReadersCountWhatTheyCouldNotReadTest(_ReaderContract, unittest.TestCase):
    """Readers that walk several files and report the ones they could not
    read, so a FIFO must land in that report rather than hang the walk."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for cmd in (["init", "-q"], ["config", "user.email", "t@e.example"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git"] + cmd, cwd=self.repo, capture_output=True,
                           timeout=30)

    def cases(self):
        tasks = os.path.join(self.tmp, "tasks")
        marker = {"pid": os.getpid(), "ts": "arm"}

        def census_refused(test, got, path):
            test.assertEqual(got[0], "value")
            test.assertEqual(got[1].state, gate.GATE_UNREADABLE)
            _names_the_type(test, got[1].reason)
        return [
            _Case("tasksmirror.read_tasks", lambda: os.path.join(tasks, "t1.json"),
                  lambda: tasksmirror.read_tasks(tasks),
                  lambda test, got, path: test.assertEqual(got, ("value", ([], [path]))),
                  lambda test, got: test.assertEqual(got, ("value", ([PAYLOAD], [])))),
            _Case("gate.inflight_census", lambda: gate.inflight_path(self.repo),
                  lambda: gate.inflight_census(self.repo), census_refused,
                  lambda test, got: test.assertEqual(
                      got[1][:2], (gate.GATE_LIVE, (os.getpid(), "arm"))),
                  payload=marker),
            _Case("inflight_gate.inflight", lambda: gate.inflight_path(self.repo),
                  lambda: inflight_gate.inflight(self.repo),
                  lambda test, got, path: test.assertEqual(got, ("value", None)),
                  lambda test, got: test.assertEqual(
                      got, ("value", (os.getpid(), "arm"))),
                  payload=marker),
        ]


class WholeFileReadersRefuseTest(_ReaderContract, unittest.TestCase):
    """Readers that read a store whole and parse it after the open."""

    def cases(self):
        return [
            _Case("actors.read_store", actors.store_path, actors.read_store,
                  lambda test, got, path: (
                      test.assertIsNone(got[1][0]),
                      test.assertIn("UNREADABLE (NotRegularFile", got[1][1])),
                  lambda test, got: test.assertEqual(
                      got, ("value", (actors._empty(), None))),
                  payload=actors._empty()),
            _Case("drain._authored_source_error", lambda: drain.home.authored_path(),
                  drain._authored_source_error,
                  lambda test, got, path: (
                      test.assertEqual(got[0], "value"),
                      test.assertTrue(str(got[1]).startswith("unreadable ("), got),
                      _names_the_type(test, got[1])),
                  lambda test, got: test.assertEqual(got, ("value", None)),
                  payload={"projects": {}}),
        ]


class CredentialStoreReaderRefusesTest(_ReaderContract, unittest.TestCase):
    """A tool credential store read by path. SYNTHETIC JSON ONLY: the key in
    the payload is a fixture string, never a token."""

    def setUp(self):
        super().setUp()
        store = os.path.join(self.tmp, "opencode-auth.json")
        old = seat.OPENCODE_AUTHSTORE
        seat.OPENCODE_AUTHSTORE = store
        self.addCleanup(setattr, seat, "OPENCODE_AUTHSTORE", old)
        self.store = store

    def cases(self):
        return [_Case(
            "seat._opencode_authstore_key", lambda: self.store,
            lambda: seat._opencode_authstore_key("synthetic-provider"),
            lambda test, got, path: (
                test.assertIsNone(got[1][0]),
                test.assertIn("unreadable", got[1][1]),
                _names_the_type(test, got[1][1])),
            lambda test, got: test.assertEqual(
                got, ("value", ("fixture-key-not-a-token", None))),
            payload={"synthetic-provider": {"type": "api",
                                            "key": "fixture-key-not-a-token"}})]


class ADirectScriptEntryPointStillRuns(unittest.TestCase):
    """A module that documents `python3 <file> ...` is RUN THAT WAY, and then
    it has no package parent: a module-level `from . import pk` raises
    ImportError before argv is even read. Both modules below carry such an
    entry point, so the import that reaches pk has to survive both ways in.

    The arms EXECUTE the shipped file by pathname, which is the only shape
    that sees this: every other test imports through the package, where a
    relative import is fine and the defect is invisible."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run_direct(self, relpath, argv, env=None):
        e = dict(os.environ)
        e.pop("PYTHONPATH", None)   # nothing may put the package on the path
        e.update(env or {})
        return subprocess.run(
            [sys.executable, os.path.join(self.ROOT, relpath)] + argv,
            capture_output=True, text=True, timeout=60, env=e,
            cwd=tempfile.gettempdir())

    def assertNotAnImportFailure(self, r, what):
        both = r.stdout + r.stderr
        self.assertNotIn("ImportError", both, what + ": " + both[-400:])
        self.assertNotIn("attempted relative import", both,
                         what + ": " + both[-400:])

    def test_physics_run_by_pathname_reaches_its_own_usage(self):
        r = self.run_direct("helm/physics.py", [])
        self.assertNotAnImportFailure(r, "physics.py by pathname")
        self.assertIn("usage: physics.py", r.stdout + r.stderr)

    def test_physics_run_by_pathname_produces_its_report(self):
        """PAST the usage line and INTO the reader: this one drives the branch
        that actually calls pk, over a real home holding a real settings file.

        IT ASSERTS THE LAYER, NEVER THE WINNING MODEL. A child process cannot
        be given this repo's MANAGED_DIRS, and a host that HAS a managed
        config legitimately outranks the user layer — so requiring the planted
        model in stdout would fail on a supported host for a reason that has
        nothing to do with the import. The user LAYER is host-independent: a
        managed config adds its own entry, it cannot remove this one. The
        winning-model contract is the isolated in-process twin below."""
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"model": "synthetic-model"}, f)
        r = self.run_direct("helm/physics.py", [home, "", "claude"])
        self.assertNotAnImportFailure(r, "physics.py report by pathname")
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        report = json.loads(r.stdout)
        planted = os.path.join(home, "settings.json")
        self.assertIn({"layer": "user", "path": planted},
                      report["settingsLayers"],
                      "the child read the planted user settings file")

    def test_the_planted_model_wins_when_no_managed_config_outranks_it(self):
        """THE TWIN the arm above hands its model claim to, isolated the way
        tests/test_physics_diff.py isolates: MANAGED_DIRS repointed at an empty
        directory, so the winning model is a fact about the planted file and
        not about this host."""
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"model": "synthetic-model"}, f)
        from helm import physics
        prior = physics.MANAGED_DIRS
        physics.MANAGED_DIRS = (os.path.join(home, "no-managed-here"),)
        self.addCleanup(setattr, physics, "MANAGED_DIRS", prior)
        report = physics.physics_report(home, None, "claude")
        self.assertEqual("synthetic-model",
                         report["settingsHighlights"]["model"])
        self.assertEqual("user", report["settingsHighlights"]["modelSource"])

    def test_catalog_run_by_pathname_seeds_from_a_prior_catalog(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        session = os.path.join(home, "synthetic-session.jsonl")
        with open(session, "w") as f:
            f.write("{}\n")
        prior = os.path.join(home, "prior-catalog.json")
        with open(prior, "w") as f:
            json.dump([{"path": session, "harness": "claude", "cwd": home,
                        "title": "synthetic", "bytes": 3, "msgs": 1,
                        "updated": "2026-09-15"}], f)
        r = self.run_direct("helm/catalog.py", ["seed", prior],
                            env={"HOME": home})
        self.assertNotAnImportFailure(r, "catalog.py by pathname")
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertIn("seeded 1 rows", r.stderr)

    def _catalog_scanner_fixture(self, matching):
        """A home with ONE discoverable session and a cache entry for it.

        THE CACHE IS ONLY REACHED ON THE SCANNER PATH, and only a MATCHING
        entry is a hit — so this fixture parameterises exactly the thing the
        arm must discriminate. HELM_CATALOG=scanner keeps `cv` out of it
        (a successful cv returns before the cache is ever loaded, and would
        also mean the child ran a real binary off this host), and
        HELM_CLAUDE_ROOTS plus HOME point discovery at the fixture instead of
        the operator's own sessions. The session is padded past the 200-byte
        floor the scanner skips below."""
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        root = os.path.join(home, "projects")
        os.makedirs(os.path.join(root, "proj"))
        session = os.path.join(root, "proj", "11111111-2222-3333-4444-555555555555.jsonl")
        with open(session, "w") as f:
            f.write(json.dumps({"type": "user", "pad": "x" * 400}) + "\n")
        st = os.stat(session)
        cache_dir = os.path.join(home, ".cache", "helm")
        os.makedirs(cache_dir)
        entry = {"mt": int(st.st_mtime), "mtns": st.st_mtime_ns,
                 "sz": st.st_size if matching else st.st_size + 1,
                 "row": {"h": "claude", "i": "cached", "c": "cached-cwd",
                         "b": "", "t": "CACHED-TITLE", "z": st.st_size,
                         "m": 1, "cr": "", "u": "2026-09-15", "p": session,
                         "cwd": "cached-cwd", "mt": int(st.st_mtime)}}
        with open(os.path.join(cache_dir, "catalog-cache.json"), "w") as f:
            json.dump({session: entry}, f)
        return self.run_direct("helm/catalog.py", [],
                               env={"HOME": home, "HELM_CATALOG": "scanner",
                                    "HELM_CLAUDE_ROOTS": root,
                                    "HELM_CODEX_ROOTS": os.path.join(home, "none")})

    def test_catalog_run_by_pathname_reads_its_cache_through_pk(self):
        """PAST the entry point and INTO the reader.

        A CACHE HIT IS THE ONLY THING THAT PROVES THE CACHE WAS READ, and the
        child's own stats line reports it: one discovered file rescanned ZERO
        times can only mean the entry was loaded and matched. rc 0 alone proves
        nothing here — an empty build, a cv that answered first, or a reader
        that returned {} all exit 0 with rows."""
        r = self._catalog_scanner_fixture(matching=True)
        self.assertNotAnImportFailure(r, "catalog.py build by pathname")
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertIn("'files': 1", r.stderr)
        self.assertIn("'rescanned': 0", r.stderr)

    def test_a_cache_entry_that_does_not_match_is_rescanned(self):
        """THE CONTROL that gives the hit its meaning: the same fixture with a
        size the file does not have. Without this twin, a reader that always
        answered {} and a scanner that skipped the file entirely would both
        pass the arm above on some other tree."""
        r = self._catalog_scanner_fixture(matching=False)
        self.assertNotAnImportFailure(r, "catalog.py build by pathname")
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertIn("'files': 1", r.stderr)
        self.assertIn("'rescanned': 1", r.stderr)

    def test_the_same_two_modules_still_import_through_the_package(self):
        """THE CONTROL. A cure that only fixed the script path would break the
        ordinary import, which is how every other caller reaches these."""
        from helm import catalog, physics
        self.assertTrue(callable(physics.physics_report))
        self.assertTrue(callable(catalog.seed_from))
        self.assertIs(physics._pk(), catalog._pk())


if __name__ == "__main__":
    unittest.main()
