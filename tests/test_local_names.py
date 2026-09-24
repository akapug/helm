"""This host's local names: the reader, and every consumer's two answers.

Each consumer is exercised twice: with no local-names file (the fresh-clone
case, which must take the neutral default) and with a planted file naming a
synthetic value (which must reach the behaviour). The names planted here are
made up, so a pass says nothing about any real host; the real host's file is
proven by the lane's read-only probes, not by this suite.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from helm import seat  # noqa: F401 — the facade before its impl module
from helm import doctor, findingspass, localnames, seat_catalog


class _Home(unittest.TestCase):
    """A private helm home per test; `plant` writes its local-names file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "helm-home")
        os.makedirs(os.path.join(self.home, "_global"))
        self.envp = mock.patch.dict(os.environ, {"HELM_HOME": self.home})
        self.envp.start()
        os.environ.pop("MELD_HOME", None)
        self.path = os.path.join(self.home, "_global", localnames.CONFIG)

    def tearDown(self):
        self.envp.stop()
        self.tmp.cleanup()

    def plant(self, table=None, raw=None):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(table))
        # a rewrite inside one mtime tick keeps its stat; the reader's cache
        # is keyed on stat, so a test that rewrites must not depend on luck
        localnames._cache["stat"] = None


class ReaderTest(_Home):
    def test_absent_configures_nothing_and_reports_nothing(self):  # noqa: VACUOUS_ASSERTION — the arm ends by planting a file on the same home and asserting the same readers return its values
        self.assertFalse(os.path.exists(self.path))
        for key in localnames.KEYS:
            self.assertIsNone(localnames.value(key))
        self.assertEqual(localnames.problems(), [])
        self.assertIsNone(localnames.predecessor())
        self.assertIsNone(localnames.legacy_env_key("CONFIG_ROOTS"))
        self.assertIsNone(localnames.legacy_path(".cache", "{}"))
        # the positive control on the same observables: a planted file is seen
        self.plant({"predecessor": "oldtool"})
        self.assertEqual(localnames.predecessor(), "oldtool")
        self.assertEqual(localnames.legacy_env_key("CONFIG_ROOTS"),
                         "OLDTOOL_CONFIG_ROOTS")

    def test_each_shape_reads_back(self):
        self.plant({"predecessor": "oldtool", "fab-node": " node-a ",
                    "generic-keywords": ["oldtool", " x "],
                    "_comment": ["documentation, never read"]})
        self.assertEqual(localnames.predecessor(), "oldtool")
        self.assertEqual(localnames.value("fab-node"), "node-a")
        self.assertEqual(localnames.words("generic-keywords"), ("oldtool", "x"))
        self.assertEqual(localnames.problems(), [])

    def test_an_undeclared_key_is_a_programming_error(self):
        """A reader asking for a key KEYS does not list would read a setting
        no document describes; that is refused at the call, not tolerated."""
        with self.assertRaises(KeyError):
            localnames.value("no-such-key")

    def test_unreadable_is_reported_and_configures_nothing(self):
        """Unreadable is not absent: the file is named as broken. What it
        configures is still nothing, because half a file cannot be acted on."""
        self.plant(raw="{not json")
        self.assertIsNone(localnames.predecessor())
        problems = localnames.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("did not read", problems[0])
        self.plant(raw="[1, 2]")
        self.assertIn("not a JSON object", localnames.problems()[0])

    def test_a_bad_key_or_shape_is_named_and_ignored(self):
        self.plant({"predecessor": "Old Tool", "fab-nod": "x",
                    "generic-keywords": "one"})
        self.assertIsNone(localnames.predecessor())
        self.assertEqual(localnames.words("generic-keywords"), ())
        said = " | ".join(localnames.problems())
        self.assertIn("unknown key 'fab-nod'", said)
        self.assertIn("'predecessor' is not a valid name value", said)
        self.assertIn("'generic-keywords' is not a valid words value", said)
        # the positive control: the same keys, well shaped, configure
        self.plant({"predecessor": "oldtool", "generic-keywords": ["one"]})
        self.assertEqual(localnames.predecessor(), "oldtool")
        self.assertEqual(localnames.words("generic-keywords"), ("one",))
        self.assertEqual(localnames.problems(), [])

    def test_an_edit_is_seen_without_a_restart(self):
        self.plant({"fab-node": "node-a"})
        self.assertEqual(localnames.value("fab-node"), "node-a")
        self.plant({"fab-node": "node-b"})
        self.assertEqual(localnames.value("fab-node"), "node-b")


class PredecessorTest(_Home):
    """The tool helm replaced: its spellings are honoured only where named."""

    def test_the_legacy_spelling_follows_the_declared_name(self):
        with mock.patch.dict(os.environ, {"OLDTOOL_QUOTA_CLI": "q"}):
            self.assertIsNone(localnames.legacy_env("QUOTA_CLI"))
            self.plant({"predecessor": "oldtool"})
            self.assertEqual(localnames.legacy_env_key("QUOTA_CLI"),
                             "OLDTOOL_QUOTA_CLI")
            self.assertEqual(localnames.legacy_env("QUOTA_CLI"), "q")
            self.assertEqual(localnames.legacy_env("UNSET_X", "d"), "d")
        self.assertEqual(
            localnames.legacy_path(".cache", "{}", "x.jsonl"),
            os.path.join(os.path.expanduser("~"), ".cache", "oldtool",
                         "x.jsonl"))

    def test_the_provider_env_falls_back_only_to_a_declared_predecessor(self):
        from helm import providers
        with mock.patch.dict(os.environ, {"OLDTOOL_PROVIDER": "cli"}):
            os.environ.pop("HELM_PROVIDER", None)
            self.assertIsNone(providers._env("PROVIDER"))
            self.plant({"predecessor": "oldtool"})
            self.assertEqual(providers._env("PROVIDER"), "cli")
            os.environ["HELM_PROVIDER"] = "native"
            self.assertEqual(providers._env("PROVIDER"), "native")

    def test_a_shard_drops_every_spelling_of_the_config_roots(self):  # noqa: VACUOUS_ASSERTION — both passes of the two-item loop always run, and each asserts the unrelated variable positively survives
        """Declared or not: the shard runner reads no local names, so it
        drops the whole family, and an unrelated variable survives."""
        from helm import gateshard
        with mock.patch.dict(os.environ, {"OLDTOOL_CONFIG_ROOTS": "/x",
                                          "HELM_CONFIG_ROOTS": "/y",
                                          "OLDTOOL_OTHER": "kept"}):
            for declared in (False, True):
                if declared:
                    self.plant({"predecessor": "oldtool"})
                env = gateshard._fresh_env("M")
                # BOOLEANS, NEVER THE MAPPING: a failed membership assertion
                # on `env` would print every ambient value into the report.
                for key in ("OLDTOOL_CONFIG_ROOTS", "HELM_CONFIG_ROOTS"):
                    self.assertFalse(key in env, "the shard kept %s" % key)
                self.assertEqual(env["OLDTOOL_OTHER"], "kept")

    def test_keepalive_counts_a_declared_predecessor_as_a_writer(self):
        import io
        from helm import keepalive
        rows = {"/proc/901/cmdline": b"python3\0/x/oldtool/keepalive.py\0",
                "/proc/902/cmdline": b"python3\0/x/other/keepalive.py\0"}
        real_open = open

        def fake_open(path, *a, **k):
            if path in rows:
                return io.BytesIO(rows[path])
            return real_open(path, *a, **k)

        def pids():
            with mock.patch.object(keepalive.glob, "glob",
                                   return_value=sorted(rows)), \
                    mock.patch("builtins.open", fake_open):
                return keepalive._predecessor_pids()
        self.assertEqual(pids(), [])
        self.plant({"predecessor": "oldtool"})
        self.assertEqual(pids(), [("901", "keepalive")])


#: The catalog FAMILY whose pool row is keyed by a local name — a table key,
#: not a seat.
FAMILY = "qwen27"  # noqa: SEAT_NAME — the catalog family under test


class PoolProviderTest(_Home):
    def test_the_neutral_name_keys_the_table_where_nothing_is_configured(self):
        """The suite's own helm home names nothing, so the table this process
        imported is keyed by the neutral name — and the route, the default
        and the mint all see that one key."""
        fam = seat_catalog.FAMILIES[FAMILY]
        self.assertEqual(fam["pool_default"], "local-llamacpp")
        self.assertEqual(list(fam["pool_providers"]), ["local-llamacpp"])
        self.assertEqual({r["provider"] for r in
                          seat_catalog.proxy_routes(FAMILY)},
                         {"local-llamacpp"})

    def test_a_host_name_replaces_it(self):
        self.assertEqual(seat_catalog.local_pool_provider(), "local-llamacpp")
        self.plant({"qwen27-provider": "box-llamacpp"})
        self.assertEqual(seat_catalog.local_pool_provider(), "box-llamacpp")


class DoctorTest(_Home):
    def test_the_rung_names_every_problem_and_is_registered(self):
        self.assertIn("check_local_names", doctor.CHECKS)
        self.assertEqual(doctor.check_local_names(), [])
        self.plant({"fab-nod": "x"})
        rows = doctor.check_local_names()
        self.assertEqual([lvl for lvl, _m in rows], [doctor.WARN])
        self.assertIn("unknown key 'fab-nod'", rows[0][1])

    def test_the_deploy_canon_reads_its_directory_from_the_local_names(self):
        """No local name: no deployed gate, no row. A named directory with no
        named owner is the LOUD case: something gates against nothing."""
        dest = os.path.join(self.tmp.name, "deploy-bin")
        os.makedirs(dest)
        self.assertEqual(doctor.check_deployed_artifact_canon(), [])
        self.plant({"deploy-dir": dest})
        rows = doctor.check_deployed_artifact_canon()
        self.assertEqual([lvl for lvl, _m in rows], [doctor.WARN])
        self.assertIn("no project is named as their owner", rows[0][1])
        self.plant({"deploy-dir": dest, "deploy-project": "proj"})
        rows = doctor.check_deployed_artifact_canon()
        self.assertEqual([lvl for lvl, _m in rows], [doctor.WARN])
        self.assertIn("'proj' checkout that owns them is not a registered",
                      rows[0][1])


class FindingsScriptTest(_Home):
    def test_env_then_local_names_then_nothing(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(findingspass.SCRIPT, None)
            self.assertEqual(findingspass.script_path(), "")
            self.plant({"local-review-script": "~/r/local-review.py"})
            self.assertEqual(findingspass.script_path(),
                             os.path.expanduser("~/r/local-review.py"))
            os.environ[findingspass.SCRIPT] = "/env/review.py"
            self.assertEqual(findingspass.script_path(), "/env/review.py")

    def test_an_unnamed_script_is_a_note_not_a_run(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(findingspass.SCRIPT, None)
            fields, _out = findingspass.examine("a" * 40, self.tmp.name)
        self.assertIn("no local-review script is named", json.dumps(fields))


class StoreKeywordsTest(_Home):
    def test_a_host_word_joins_the_generic_set_only_where_named(self):
        from helm.store import _common
        self.assertEqual(_common.host_generic_keywords(), frozenset())
        self.plant({"generic-keywords": ["OldTool"]})
        self.assertEqual(_common.host_generic_keywords(),
                         frozenset({"oldtool"}))
        # the set this process imported was built from the suite's own home,
        # which names none, so it is the English set alone
        self.assertNotIn("oldtool", _common.GENERIC_KEYWORDS)
        self.assertIn("should", _common.GENERIC_KEYWORDS)


if __name__ == "__main__":
    unittest.main()
