#!/usr/bin/env python3
"""0.2 council item 2, cold start: the clean-home SUBPROCESS pin.

The historical defect (a review's repro): on a fresh HELM_HOME, `helm sync`
reported 0 projects and then `helm doctor` exited 1 with "projection ORPHANED"
— a valid zero-state machine failed the advertised health gate immediately
after the documented first command. Fixed across 27a74873b75adecaba0cb98a964f
92f26d8c005d (cold genesis is not corruption) -> a04c98b (lazy stamp) ->
d1b44b0 (genesis by register, not stamp) -> 77eb299 (exact source-free genesis
contracts). The council's demanded control, however, stayed unpinned: the one
prior probe (CleanHomeColdStartTest) set only HOME, so the subprocess still
saw this box's ambient estate — the live chat node on /dev/shm/helm-chat
(chat.DEFAULT_DIR is an absolute path that no amount of HOME-pointing moves)
and whatever rails are installed on the machine running the suite.

THIS file is the fully isolated control. Every helm env key is popped by
CONSTRUCTION (the subprocess env is built from scratch, never inherited) and
the leak vectors are each pinned to a tempdir: HELM_HOME, HELM_CHAT_DIR (off
/dev/shm), HELM_ADOPTED_DIR, HELM_CACHE_DIR, HELM_SCAN_ROOTS and
HELM_CONFIG_ROOTS (else both default to ~/dev — a LIVE projection source on
any dev box, which silently skips the whole orphan check), HELM_CHAT_NODE_URL
empty (no live node reachable), HOME itself a tempdir, and cwd a fresh
one-commit git repo. The verbs run as SUBPROCESSES of this tree's own
./bin/helm — an in-process call would inherit module state (frozen config
roots, cached homes) that the council's scenario never has.

Effect, not absence: the clean-home leg asserts the "exact clean genesis" OK
rows POSITIVELY, and the no-ORPHANED claim is paired with a seeded control —
the same estate with real data planted into the projection while every source
stays absent — where doctor MUST exit 1 and name ORPHANED. A detector that
never fires would prove nothing about a clean home.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

# The exact pop list the hermetic setUps share (tests/test_gate.py) — here the
# subprocess env is built from scratch, so absence is by construction; the
# invariant below asserts it stays that way if the fixture ever changes.
ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR", "HELM_CHAT_ROOM",
            "HELM_CHAT_NAME", "HELM_CELL_BIN", "HELM_CELL_PROFILE",
            "HELM_CHAT_NODE_URL", "HELM_VERDICT_ROOM",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

# Keys the fixture DOES set, every one aimed inside the tempdir (or explicitly
# empty). Anything in ENV_KEYS but not here must be absent from the child env.
OVERRIDDEN = {"HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
              "HELM_CHAT_ROOM", "HELM_CHAT_NODE_URL",
              # codex's review blocker 1: these three were SET by the fixture
              # but omitted from the invariant's audit, so aiming any of them
              # outside the tempdir left the invariant green — the exact
              # regrowth the test exists to prevent.
              "HELM_CACHE_DIR", "HELM_SCAN_ROOTS", "HELM_CONFIG_ROOTS"}

HELM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "bin", "helm")


class CleanHomeSubprocessPin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="helm-cold-start-")
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        mk = lambda p: os.path.join(self.tmp, p)
        self.env = {
            "HOME": mk("home"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "HELM_METAHARNESS": "none",
            "HELM_HOME": mk("helm-home"),
            "HELM_CHAT_DIR": mk("chat"),
            "HELM_ADOPTED_DIR": mk("adopted"),
            "HELM_CHAT_NODE_URL": "",
            "HELM_CHAT_ROOM": "main",
            "HELM_CACHE_DIR": mk("cache"),
            # Named but NEVER created: a truly cold estate has no scan root,
            # and an existing one is a live projection source that silently
            # bypasses the entire genesis-vs-orphan branch under test.
            "HELM_SCAN_ROOTS": mk("scan"),
            "HELM_CONFIG_ROOTS": mk("cfgroots"),
            # codex's review blocker 2, measured: even under env-from-scratch,
            # git reads /etc/gitconfig (env -i git config --system --get
            # filter.lfs.required answered from the machine). NOSYSTEM closes
            # the last ambient door git itself holds open.
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        os.makedirs(self.env["HOME"])
        os.makedirs(self.env["HELM_ADOPTED_DIR"])
        # cwd: a fresh one-commit git repo, so nothing ambient leaks in from
        # whatever repository the test runner happens to stand in.
        self.repo = mk("repo")
        os.makedirs(self.repo)
        self._git("init", "-q")
        self._git("config", "user.email", "cold@start")
        self._git("config", "user.name", "cold start")
        with open(os.path.join(self.repo, "README"), "w") as fh:
            fh.write("cold\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "first")

    def _git(self, *args):
        subprocess.run(("git",) + args, cwd=self.repo, env=self.env,
                       check=True, capture_output=True, timeout=30)

    def helm(self, verb):
        return subprocess.run([sys.executable, HELM, verb], cwd=self.repo,
                              env=self.env, text=True, capture_output=True,
                              timeout=60)

    def _sync_clean(self):
        sync = self.helm("sync")
        blob = sync.stdout + sync.stderr
        self.assertEqual(sync.returncode, 0, blob)
        # The actual count line, not merely rc: 0 projects on a clean home.
        self.assertIn("helm sync: 0 projects known (0 new, 0 refreshed)",
                      sync.stdout)
        return sync

    def test_env_isolation_invariant(self):  # noqa: VACUOUS_ASSERTION — positive control below binds every OVERRIDDEN key tempdir-rooted
        """Every hermetic-setUp key is either absent or aimed at the tempdir —
        the fixture can never regrow the ambient-leak the council flagged."""
        for key in ENV_KEYS:
            if key not in OVERRIDDEN:
                # intentional absence; the positive control on the same
                # observable follows: every OVERRIDDEN key tempdir-rooted
                # (or exactly empty).
                self.assertNotIn(key, self.env, key)
        for key in OVERRIDDEN - {"HELM_CHAT_NODE_URL", "HELM_CHAT_ROOM"}:
            self.assertTrue(self.env[key].startswith(self.tmp + os.sep),
                            "%s=%s leaks outside the tempdir"
                            % (key, self.env[key]))
        self.assertEqual(self.env["HELM_CHAT_NODE_URL"], "")
        self.assertFalse(self.env["HELM_CHAT_DIR"].startswith("/dev/shm"),
                         "chat dir must not touch the live fleet node")
        # The named-but-ABSENT law for projection sources: an existing scan
        # or config root is a live source that silently bypasses the whole
        # genesis-vs-orphan branch under test. Named, aimed at the tempdir,
        # and provably nonexistent — all three at once.
        for key in ("HELM_SCAN_ROOTS", "HELM_CONFIG_ROOTS"):
            self.assertFalse(os.path.exists(self.env[key]),
                             "%s exists — a live projection source leaked "
                             "into the cold estate" % key)
        # And git's own ambient door stays closed (codex measured /etc/
        # gitconfig answering under env-from-scratch without this).
        self.assertEqual(self.env.get("GIT_CONFIG_NOSYSTEM"), "1")

    def test_clean_home_sync_then_doctor_exits_0_with_exact_genesis(self):  # noqa: VACUOUS_ASSERTION — positive genesis rows asserted below; seeded control fires the detector
        """The council's control: fresh estate, documented first commands,
        subprocess boundary — sync reports zero, doctor exits 0, and the
        genesis rows are asserted POSITIVELY, not as a silent non-FAIL."""
        self._sync_clean()
        doctor = self.helm("doctor")
        blob = doctor.stdout + doctor.stderr
        self.assertEqual(doctor.returncode, 0, blob)
        # intentional absence; paired on the same observable with the
        # positive "exact clean genesis" rows just below, and with
        # test_seeded_orphan_is_still_flagged, where this exact detector
        # MUST fire and exit 1.
        self.assertNotIn("ORPHANED", blob)
        # Effect, not absence: the projections that sync just wrote are
        # recognized BY NAME as exact clean genesis, and the tally says so.
        for row in ("registry", "codex-cwd-cache"):
            self.assertIn("%s: exact clean genesis" % row, doctor.stdout, blob)
        self.assertIn(", 0 fail", doctor.stdout, blob)

    def test_seeded_orphan_is_still_flagged(self):
        """The positive control that keeps the clean-home absence honest:
        plant real data into the projection (the historical defect's shape —
        bytes on disk whose every declared source is gone, and the bytes now
        carry truth to lose) and doctor MUST exit 1 naming ORPHANED."""
        self._sync_clean()
        reg = os.path.join(self.env["HELM_HOME"], "_global", "registry.json")
        with open(reg, encoding="utf-8") as fh:
            body = json.load(fh)
        body["projects"]["lost"] = {"path": "/gone"}
        with open(reg, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        doctor = self.helm("doctor")
        blob = doctor.stdout + doctor.stderr
        self.assertEqual(doctor.returncode, 1, blob)
        self.assertIn("registry: ORPHANED", doctor.stdout, blob)
        self.assertIn("the copy just became the only truth", doctor.stdout)


class DefaultCacheRootColdStartPin(CleanHomeSubprocessPin):
    """The same cold estate with HELM_CACHE_DIR UNSET, the way a newcomer runs
    helm, so the classified cache root is $HOME/.cache/helm.

    The pin above sets HELM_CACHE_DIR, and the session catalog does not read
    it: the catalog writes syn-cache.json under $HOME/.cache/helm whatever that
    variable says, so the doctor there surveys a root the catalog never wrote
    into and cannot see the row at all. Here the two roots are one.

    The catalog writes syn-cache.json only on its cv path, so a machine with
    no cv never meets this row. The estate therefore puts a cv with no
    sessions first on PATH (`cv ls --json` answers `[]`), the state of a
    newcomer who installed cv and has not run an agent yet. `helm sync`
    writes no syn-cache; `helm sessions`, the quickstart's next read, builds
    the catalog and writes syn-cache.json as `{}`, and the `helm doctor` the
    quickstart runs after it must read that as exact clean genesis rather
    than ORPHANED. Every arm of the pin above runs again under this estate,
    and the two below name the syn-cache row itself, with a seeded control
    that must still fire."""

    def setUp(self):
        super().setUp()
        del self.env["HELM_CACHE_DIR"]
        self.syn = os.path.join(self.env["HOME"], ".cache", "helm",
                                "syn-cache.json")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        cv = os.path.join(bindir, "cv")
        with open(cv, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\n'
                     '# a cv with no sessions yet\n'
                     'if [ "$1" = ls ]; then echo "[]"; exit 0; fi\n'
                     'exit 1\n')
        os.chmod(cv, 0o755)
        self.env["PATH"] = bindir + os.pathsep + self.env["PATH"]

    def _sessions_clean(self):
        """sync, then the catalog read that writes syn-cache.json."""
        self._sync_clean()
        sessions = self.helm("sessions")
        blob = sessions.stdout + sessions.stderr
        self.assertEqual(sessions.returncode, 0, blob)
        self.assertIn("helm sessions: none.", sessions.stdout, blob)

    def test_env_isolation_invariant(self):
        """The parent's invariant, with HELM_CACHE_DIR absent on purpose: the
        default cache root it leaves behind sits under the tempdir HOME."""
        self.assertNotIn("HELM_CACHE_DIR", self.env)
        self.assertTrue(self.env["HOME"].startswith(self.tmp + os.sep))
        self.assertTrue(self.syn.startswith(self.tmp + os.sep), self.syn)
        for key in OVERRIDDEN - {"HELM_CHAT_NODE_URL", "HELM_CHAT_ROOM",
                                 "HELM_CACHE_DIR"}:
            self.assertTrue(self.env[key].startswith(self.tmp + os.sep),
                            "%s=%s leaks outside the tempdir"
                            % (key, self.env[key]))
        for key in ("HELM_SCAN_ROOTS", "HELM_CONFIG_ROOTS"):
            self.assertFalse(os.path.exists(self.env[key]), key)

    def test_first_catalog_writes_syn_cache_and_doctor_reads_it_as_genesis(self):  # noqa: VACUOUS_ASSERTION — the empty `{}` IS the genesis under test; the doctor row is asserted positively below, and test_seeded_syn_cache_is_still_flagged plants data in the same file and must fire
        self._sessions_clean()
        with open(self.syn, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})   # the row's file, and empty
        doctor = self.helm("doctor")
        blob = doctor.stdout + doctor.stderr
        self.assertEqual(doctor.returncode, 0, blob)
        self.assertIn("syn-cache: exact clean genesis", blob)
        self.assertNotIn("syn-cache: ORPHANED", blob)

    def test_seeded_syn_cache_is_still_flagged(self):
        """The control on the same row: a peek result for a transcript whose
        every source is gone is data, not genesis, so doctor MUST exit 1."""
        self._sessions_clean()
        with open(self.syn, "w", encoding="utf-8") as fh:
            json.dump({"/gone/session.jsonl": [1, True]}, fh)
        doctor = self.helm("doctor")
        blob = doctor.stdout + doctor.stderr
        self.assertEqual(doctor.returncode, 1, blob)
        self.assertIn("syn-cache: ORPHANED", doctor.stdout, blob)


if __name__ == "__main__":
    unittest.main()
