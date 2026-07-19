#!/usr/bin/env python3
"""ship tests — hermetic: tmp HELM_HOME with a REAL git repo inside the
tempdir; git config isolated (GIT_CONFIG_GLOBAL/SYSTEM -> /dev/null, so the
_ident fallback is exercised too). The real ~/.helm, ~/.claude and the user's
git identity are never read or written."""
import contextlib
import io
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import home, pk, ship, store  # noqa: E402

FAKE_TOKEN = "sk-ant-api03-" + "A" * 24  # fake, planted to trip the scan


def prior_text(pid, statement, status="live", replaced_by=""):
    extra = ("  replaced_by: " + replaced_by + "\n") if replaced_by else ""
    return ("---\nname: prior-%s\ndescription: \"premise: %s\"\nmetadata:\n"
            "  node_type: memory\n  type: prior\n  id: %s\n  statement: %s\n"
            "  confidence: 1.00\n  class: certain\n  load_class: jit\n"
            "  status: %s\n%s---\n\nPREMISE: %s\n"
            % (pid, pid, pid, statement, status, extra, statement))


class ShipBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-ship-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hh = os.path.join(self.tmp, "helm-home")
        self._env = mock.patch.dict(os.environ, {
            "HELM_HOME": self.hh,
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("MELD_HOME", None)
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        self._estate()

    def _estate(self):
        """A miniature ~/.helm: authored chain + every derived class + an
        adopted-by-symlink home."""
        g = os.path.join(self.hh, "_global")
        for d in ("premises", "heuristics", "lexicon", "reflexes", "know-your-user"):
            os.makedirs(os.path.join(g, d))
        self._write(g, "premises", "prior-old-law.md", prior_text("old-law", "The old law."))
        self._write(g, "know-your-user", "profile.md", "# operator\n")
        for d in ("premises", "journal", "evals"):
            os.makedirs(os.path.join(self.hh, "alpha", d))
        self._write(self.hh, "alpha/journal", "2026-07-19-entry.md", "# shipped day\n")
        # derived: projections (global + per-project mirror), state, seat AUTH
        pk.write_json(os.path.join(g, "registry.json"), {"projects": {
            "alpha": {"status": "active", "last_seen": 1900000000.0,
                      "sessions": {"claude": 3}, "path": "/x/alpha"}}})
        pk.write_json(os.path.join(self.hh, "alpha", "registry.json"), {"name": "alpha"})
        pk.write_json(os.path.join(g, "registry-authored.json"),
                      {"projects": {"alpha": {"edges": [], "path": "/x/alpha"}}})
        self._write(g, ".state", "inject-ledger.jsonl", "{}\n")
        self._write(g, "seats/codex/auth", "codex-team.json",
                    '{"token": "%s"}\n' % FAKE_TOKEN)
        ext = os.path.join(self.tmp, "external-mc")
        os.makedirs(ext)
        os.symlink(ext, os.path.join(self.hh, "mc-adopted"))

    def _write(self, *parts_and_text):
        *parts, name, text = parts_and_text
        d = os.path.join(*parts)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def g(self, cwd, *args):
        return subprocess.run(("git", "-C", cwd) + args,
                              capture_output=True, text=True)

    def run_ship(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ship.cmd_ship(args)
        return rc, out.getvalue(), err.getvalue()

    def tracked(self):
        return self.g(self.hh, "ls-files").stdout.splitlines()


class DryRunTest(ShipBase):
    def test_dry_default_touches_nothing(self):
        rc, out, err = self.run_ship([])
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.hh, ".git")))
        self.assertFalse(os.path.exists(os.path.join(self.hh, ".gitignore")))
        self.assertIn("not initialized", out)
        self.assertIn("would ship", out)
        self.assertIn("none configured", out)          # remote honesty
        self.assertIn("secret scan: clean", out)       # seats/ auth never in the set
        self.assertIn("seats/", out)                   # named among derived
        self.assertIn("mc-adopted", out)               # symlink home surfaced

    def test_dry_would_refuse_on_secret(self):
        self._write(self.hh, "_global/premises", "prior-leak.md",
                    prior_text("leak", "token " + FAKE_TOKEN))
        rc, out, err = self.run_ship([])
        self.assertEqual(rc, 1)
        self.assertIn("would REFUSE", err)
        self.assertIn("prior-leak.md", err)
        self.assertFalse(os.path.exists(os.path.join(self.hh, ".git")))


class ApplyTest(ShipBase):
    def test_classification_gitignore_and_registry_exclusion(self):
        rc, out, err = self.run_ship(["--apply"])
        self.assertEqual(rc, 0, err)
        tracked = self.tracked()
        self.assertIn("_global/premises/prior-old-law.md", tracked)
        self.assertIn("_global/registry-authored.json", tracked)   # lineage SHIPS
        self.assertIn("_global/know-your-user/profile.md", tracked)
        self.assertIn("alpha/journal/2026-07-19-entry.md", tracked)
        self.assertIn(".gitignore", tracked)
        host_block = "_global/hosts/%s.json" % socket.gethostname()
        self.assertIn(host_block, tracked)
        for path in tracked:                                       # derived NEVER ships
            self.assertNotEqual(os.path.basename(path), "registry.json", path)
            self.assertNotIn(".state/", path)
            self.assertNotIn("seats/", path)
            self.assertFalse(path.startswith("mc-adopted"), path)
        # git's own classifier agrees with the python-side one
        self.assertEqual(self.g(self.hh, "check-ignore", "_global/registry.json").returncode, 0)
        self.assertEqual(self.g(self.hh, "check-ignore", "alpha/registry.json").returncode, 0)
        self.assertEqual(self.g(self.hh, "check-ignore", "_global/seats/codex/auth/codex-team.json").returncode, 0)
        self.assertNotEqual(self.g(self.hh, "check-ignore", "_global/registry-authored.json").returncode, 0)
        # managed block is idempotent
        with open(os.path.join(self.hh, ".gitignore"), encoding="utf-8") as f:
            before = f.read()
        ship.write_gitignore(self.hh)
        with open(os.path.join(self.hh, ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(before, f.read())
        # no remote: commit-only, said explicitly
        self.assertIn("committed locally ONLY", out)

    def test_ship_idempotent(self):
        rc, out, _ = self.run_ship(["--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("committed", out)
        rc, out, _ = self.run_ship(["--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing new to ship", out)
        self.assertEqual(self.g(self.hh, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_secret_scan_refuses_and_unstages(self):
        self._write(self.hh, "_global/premises", "prior-leak.md",
                    prior_text("leak", "token " + FAKE_TOKEN))
        rc, out, err = self.run_ship(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", err)
        self.assertIn("_global/premises/prior-leak.md", err)
        self.assertIn("anthropic key", err)
        self.assertNotEqual(self.g(self.hh, "rev-parse", "HEAD").returncode, 0)  # no commit
        self.assertEqual(self.g(self.hh, "ls-files", "--cached").stdout, "")     # unstaged

    def test_hosts_observation_block(self):
        rc, _, err = self.run_ship(["--apply"])
        self.assertEqual(rc, 0, err)
        block = pk.read_json(os.path.join(
            home.global_dir(), "hosts", socket.gethostname() + ".json"))
        self.assertEqual(block["host"], socket.gethostname())
        self.assertEqual(block["projects"]["alpha"]["status"], "active")
        self.assertEqual(block["projects"]["alpha"]["sessions"], 3)
        rc, out, _ = self.run_ship(["hosts"])
        self.assertEqual(rc, 0)
        self.assertIn(socket.gethostname(), out)
        self.assertIn("1 projects (1 active)", out)


class PullTest(ShipBase):
    def test_pull_guards(self):
        rc, _, err = self.run_ship(["pull"])
        self.assertEqual(rc, 1)
        self.assertIn("not shipped yet", err)
        self.run_ship(["--apply"])
        rc, _, err = self.run_ship(["pull"])
        self.assertEqual(rc, 1)
        self.assertIn("no remote", err)

    def test_push_pull_merge_with_superseded_entry(self):
        bare = os.path.join(self.tmp, "remote.git")
        self.assertEqual(subprocess.run(("git", "init", "-q", "--bare", "-b", "main", bare),
                                        capture_output=True).returncode, 0)
        rc, out, err = self.run_ship(["--apply", "--remote", bare])
        self.assertEqual(rc, 0, err)
        self.assertIn("pushed to", out)

        # host B revises the chain: tombstone old-law, author its replacement
        b = os.path.join(self.tmp, "hostB")
        self.assertEqual(self.g(self.tmp, "clone", "-q", bare, b).returncode, 0)
        self._write(b, "_global/premises", "prior-old-law.md",
                    prior_text("old-law", "The old law.",
                               status="delete_eligible", replaced_by="new-law"))
        self._write(b, "_global/premises", "prior-new-law.md",
                    prior_text("new-law", "The new law."))
        ident = ("-c", "user.name=b", "-c", "user.email=b@test")
        self.g(b, "add", "-A")
        self.assertEqual(self.g(b, *ident, "commit", "-q", "-m", "hostB supersede").returncode, 0)
        self.assertEqual(self.g(b, "push", "-q", "origin", "main").returncode, 0)

        # host A authored in parallel: ship is REJECTED, told to pull first
        self._write(self.hh, "_global/premises", "prior-local-only.md",
                    prior_text("local-only", "Authored on A."))
        rc, _, err = self.run_ship(["--apply"])
        self.assertEqual(rc, 1)
        self.assertIn("helm ship pull", err)

        # pull: append-only merge (both sides' files land), projections re-derive
        with mock.patch.object(ship, "_resync", return_value=(5, 1)) as rs:
            rc, out, err = self.run_ship(["pull"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(rs.call_count, 1)
        self.assertIn("merged", out)
        self.assertIn("projections regenerated", out)
        prem = os.path.join(self.hh, "_global", "premises")
        self.assertTrue(os.path.exists(os.path.join(prem, "prior-new-law.md")))
        self.assertTrue(os.path.exists(os.path.join(prem, "prior-local-only.md")))
        with open(os.path.join(prem, "prior-old-law.md"), encoding="utf-8") as f:
            self.assertIn("delete_eligible", f.read())
        # the store reads the merge exactly as supersede semantics demand
        live = {e["id"] for e in store.load_all()}
        self.assertIn("new-law", live)
        self.assertIn("local-only", live)
        self.assertNotIn("old-law", live)
        every = {e["id"]: e for e in store.load_all(include_retired=True)}
        self.assertEqual(every["old-law"]["status"], "delete_eligible")
        # and the re-ship now lands cleanly
        rc, out, err = self.run_ship(["--apply"])
        self.assertEqual(rc, 0, err)
        self.assertIn("pushed to", out)
        # the remote never learned a registry.json
        names = self.g(self.hh, "ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertNotIn("registry.json\n", names.replace("registry-authored.json", ""))


if __name__ == "__main__":
    unittest.main()
