"""helm against both cv grammars: the 0.10 one and the 0.11+ one (cv 0.13).

The fixtures under tests/fixtures/cv/ are real `--json` output from the two
binaries, with the values made neutral and every key kept as the binary wrote
it. A reader test feeds both spellings to the same helm reader and asserts the
same answer; a writer test pins the version and reads the argv helm builds.
Nothing here runs cv: the version probe and every cv call are stubbed."""
import inspect
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import capability, catalog, cvcompat, session, transcripts
from helm.providers import ProviderError

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "cv")
OLD, NEW = (0, 10, 0), (0, 13, 0)
BANNERS = {OLD: "cv 0.10.0 (d6316544d516)\n", NEW: "cv 0.13.0 (39b743a7eaa7)\n"}


def fixture(name, v):
    with open(os.path.join(FIX, "%s-%d.%d.%d.json" % ((name,) + v))) as f:
        return json.load(f)


def speaking(v):
    return mock.patch.object(cvcompat, "version", return_value=v)


class VersionProbeTest(unittest.TestCase):
    def setUp(self):
        self._seen = list(cvcompat._seen)
        cvcompat._seen.clear()
        self.identity = mock.patch.object(cvcompat, "_binary_identity",
                                          return_value=("cv", 1))
        self.identity.start()

    def tearDown(self):
        self.identity.stop()
        cvcompat._seen[:] = self._seen

    def test_both_real_banners_parse(self):  # noqa: VACUOUS_ASSERTION — the None arm is the unparseable control beside two exact positives
        self.assertEqual(cvcompat.parse_version(BANNERS[OLD]), OLD)
        self.assertEqual(cvcompat.parse_version(BANNERS[NEW]), NEW)
        self.assertIsNone(cvcompat.parse_version("cv (unknown build)"))

    def test_the_probe_runs_once_per_process(self):
        proc = mock.Mock()
        proc.communicate.return_value = (BANNERS[NEW], None)
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            self.assertEqual(cvcompat.version(), NEW)
            self.assertEqual(cvcompat.version(), NEW)
            self.assertTrue(cvcompat.v2())
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args[0][0], ["cv", "--version"])

    def test_a_stubbed_subprocess_run_never_receives_the_probe(self):  # noqa: VACUOUS_ASSERTION — the probe's answer is asserted as the positive control
        proc = mock.Mock()
        proc.communicate.return_value = (BANNERS[OLD], None)
        with mock.patch("subprocess.Popen", return_value=proc), \
                mock.patch("subprocess.run") as run:
            self.assertEqual(cvcompat.drop_thinking(), "--thinking")
        run.assert_not_called()

    def test_no_cv_reads_as_the_current_grammar(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            self.assertIsNone(cvcompat.version())
            self.assertTrue(cvcompat.v2())

    def test_a_hung_probe_is_killed_and_reads_unknown(self):  # noqa: VACUOUS_ASSERTION — kill() called once is the positive observable
        proc = mock.Mock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["cv", "--version"], 15), ("", None)]
        with mock.patch("subprocess.Popen", return_value=proc):
            self.assertIsNone(cvcompat.version())
        proc.kill.assert_called_once_with()

    def test_timeout_does_not_cache_unknown_and_next_writer_retries(self):
        hung, ready = mock.Mock(), mock.Mock()
        hung.communicate.side_effect = [
            subprocess.TimeoutExpired(["cv", "--version"], 15), ("", None)]
        ready.communicate.return_value = (BANNERS[OLD], None)
        with mock.patch("subprocess.Popen", side_effect=[hung, ready]) as popen:
            self.assertIsNone(cvcompat.version())
            self.assertEqual(cvcompat.drop_thinking(), "--thinking")
        self.assertEqual(popen.call_count, 2)
        hung.kill.assert_called_once_with()

    def test_same_process_replaced_resolved_binary_reprobes(self):
        self.identity.stop()
        with tempfile.TemporaryDirectory() as d:
            binary = os.path.join(d, "cv-v1")
            link = os.path.join(d, "cv")
            with open(binary, "wb") as f:
                f.write(b"old")
            os.symlink(binary, link)
            def probe(*_a, **_kw):
                proc = mock.Mock()
                with open(os.path.realpath(link), "rb") as f:
                    proc.communicate.return_value = (
                        BANNERS[OLD if f.read() == b"old" else NEW], None)
                return proc
            with mock.patch("shutil.which", return_value=link), \
                    mock.patch("subprocess.Popen", side_effect=probe) as popen:
                self.assertEqual(cvcompat.drop_thinking(), "--thinking")
                replacement = os.path.join(d, "replacement")
                with open(replacement, "wb") as f:
                    f.write(b"new")
                os.replace(replacement, binary)
                self.assertEqual(cvcompat.drop_thinking(), "--drop-thinking")
                self.assertEqual(cvcompat.drop_thinking(), "--drop-thinking")
            self.assertEqual(popen.call_count, 2)

    def test_unreadable_binary_identity_never_reuses_cached_success(self):  # noqa: VACUOUS_ASSERTION — version() positively returns OLD before and after the unreadable-identity refusal
        self.identity.stop()
        with tempfile.TemporaryDirectory() as d:
            binary = os.path.join(d, "cv")
            with open(binary, "wb") as f:
                f.write(b"old")
            proc = mock.Mock()
            proc.communicate.return_value = (BANNERS[OLD], None)
            with mock.patch("shutil.which", return_value=binary), \
                    mock.patch("subprocess.Popen", return_value=proc) as popen:
                self.assertEqual(cvcompat.version(), OLD)
                with mock.patch("os.stat", side_effect=PermissionError):
                    self.assertIsNone(cvcompat.version())
                self.assertEqual(cvcompat.version(), OLD)
            self.assertEqual(popen.call_count, 1)


class WriterGrammarTest(unittest.TestCase):
    def test_prune_thinking_flag(self):
        with speaking(OLD):
            self.assertEqual(cvcompat.drop_thinking(), "--thinking")
        with speaking(NEW):
            self.assertEqual(cvcompat.drop_thinking(), "--drop-thinking")

    def test_message_window(self):
        with speaking(OLD):
            self.assertEqual(cvcompat.window(3, 9), "3-9")
        with speaking(NEW):
            self.assertEqual(cvcompat.window(3, 9), "3..9")

    def test_cv_show_argv_speaks_the_installed_grammar(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal pair, both arms always run
        done = mock.Mock(returncode=0, stdout='{"messages": []}', stderr="")
        for v, rng in ((OLD, "0-1"), (NEW, "0..1")):
            with speaking(v), mock.patch("subprocess.run", return_value=done) as run:
                transcripts._cv_show("deadbeef-1234", rng=(0, 1), harness="hermes")
            self.assertEqual(run.call_args[0][0],
                             ["cv", "show", "--json", "--range", rng,
                              "--harness", "hermes", "--", "deadbeef-1234"], v)


class ReaderParityTest(unittest.TestCase):
    """The same answer from either spelling, read off real output."""

    def test_field_reads_both_spellings(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal 4-tuple, every arm runs
        old, new = fixture("ls", OLD)[0], fixture("ls", NEW)[0]
        for snake in ("size_bytes", "message_count", "updated_at", "created_at"):
            self.assertIsNotNone(cvcompat.field(old, snake), snake)
            self.assertEqual(cvcompat.field(old, snake), cvcompat.field(new, snake))
        self.assertIsNone(cvcompat.field({}, "size_bytes"))

    def test_catalog_row_is_the_same_from_either_ls(self):  # noqa: VACUOUS_ASSERTION — the exact-values assertion is the positive control
        rows = [catalog._row_from_cv(fixture("ls", v)[0]) for v in (OLD, NEW)]
        self.assertEqual(rows[0], rows[1])
        self.assertEqual((rows[1]["z"], rows[1]["m"], rows[1]["u"], rows[1]["cr"]),
                         (123456, 42, "2026-09-02", "2026-09-01"))

    def test_deep_search_hit_is_the_same_from_either_search(self):
        got = []
        for v in (OLD, NEW):
            done = mock.Mock(returncode=0, stdout=json.dumps(fixture("search", v)),
                             stderr="")
            with mock.patch("subprocess.run", return_value=done), \
                    mock.patch.object(transcripts, "get_catalog",
                                      return_value={"rows": []}):
                got.append(transcripts.deep_search("needle")["hits"])
        self.assertEqual(got[0], got[1])
        self.assertEqual(got[1][0]["date"], "2026-09-02")

    def test_show_blocks_render_the_same_from_either_tag(self):
        items = []
        for v in (OLD, NEW):
            out = []
            for m in fixture("show", v)["messages"]:
                out.extend(transcripts._simplify_msg(m))
            items.append(out)
        self.assertEqual(items[0], items[1])
        self.assertEqual(sorted({i["k"] for i in items[1]}),
                         ["result", "text", "think", "tool"])

    def test_injected_context_is_not_a_transcript_turn(self):
        msgs = fixture("show", NEW)["messages"]
        injected = [m for m in msgs if m.get("kind") == "injected_context"]
        self.assertTrue(injected, "control: the real 0.13 window carries one")
        self.assertEqual(transcripts._simplify_msg(injected[0]), [])

    def test_prune_report_ids_from_either_spelling(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal pair, both arms always run
        for v in (OLD, NEW):
            r = fixture("prune", v)
            self.assertEqual(cvcompat.field(r, "new_id"),
                             "22222222-3333-4444-8555-666666666666", v)
            self.assertEqual(cvcompat.field(r, "source_id"),
                             "11111111-2222-4333-8444-555555555555", v)


class SessionTotalTest(unittest.TestCase):
    """The catalog's message count bounds `cv show` on cv 0.10. cv 0.11+ also
    lists what the harness injected as messages, so a session's show total
    runs past that count, and the transcript tail must still be the tail."""

    def total(self, n, bound, sid):
        probes = []

        def show(_sid, rng=None, harness=None):
            probes.append(rng)
            return {"messages": list(range(n))[rng[0]:rng[1]]}
        transcripts._state.pop("total:%s:%d:None" % (sid, bound), None)
        with mock.patch.object(transcripts, "_cv_show", show):
            got = transcripts._session_total(sid, bound)
        transcripts._state.pop("total:%s:%d:None" % (sid, bound), None)
        return got, len(probes)

    def test_a_session_longer_than_its_count_reads_its_true_total(self):
        self.assertEqual(self.total(9000, 5117, "cvc-past")[0], 9000)

    def test_an_exact_bound_costs_one_more_probe(self):
        self.assertEqual(self.total(5117, 5117, "cvc-exact"), (5117, 2))

    def test_a_loose_bound_still_bisects_down(self):
        self.assertEqual(self.total(300, 5117, "cvc-loose")[0], 300)

    def test_an_empty_session_does_not_invent_one_message(self):
        self.assertEqual(self.total(3, 5117, "cvc-nonempty")[0], 3)
        self.assertEqual(self.total(0, 5117, "cvc-empty")[0], 0)

    def test_nonempty_forever_has_a_finite_upper_bound_not_an_invented_total(self):  # noqa: VACUOUS_ASSERTION — last probe must positively reach the finite message ceiling before the absent-cache assertion
        sid = "cvc-unbounded"
        key = "total:%s:1:None" % sid
        transcripts._state.pop(key, None)
        probes = []
        def show(_sid, rng=None, harness=None):
            probes.append(rng)
            if len(probes) > 40:
                raise AssertionError("unbounded cv show probe")
            return {"messages": [1]}
        with mock.patch.object(transcripts, "_cv_show", side_effect=show):
            with self.assertRaisesRegex(ProviderError, "limit"):
                transcripts._session_total(sid, 1)
        self.assertEqual(probes[-1], (transcripts._MAX_SESSION_MESSAGES,
                                      transcripts._MAX_SESSION_MESSAGES + 1))
        self.assertLessEqual(len(probes), 40)
        self.assertNotIn(key, transcripts._state)

    def test_drawer_reports_limit_without_a_false_session_window(self):
        sid = "cvc-drawer-limit"
        with mock.patch.object(transcripts, "get_catalog", return_value={"rows": []}), \
                mock.patch.object(transcripts, "_cv_show",
                                  return_value={"messages": [1]}):
            result = transcripts.get_session(sid)
        self.assertIn("limit exceeded", result["error"])
        self.assertNotIn("messages", result)


class CheckpointDocumentationTest(unittest.TestCase):
    def test_checkpoint_docstring_names_the_actual_versioned_thinking_flag(self):
        doc = inspect.getdoc(session.cmd_checkpoint)
        self.assertIn("cvcompat.drop_thinking", doc)
        self.assertIn("--drop-thinking", doc)
        self.assertNotIn("revive + --thinking default", doc)


class RecallCapabilityTest(unittest.TestCase):
    def test_recall_names_the_tool_in_both_grammars(self):
        cap = next(c for c in capability.CAPABILITIES if c["id"] == "recall")
        self.assertIn("mcp__cv__search", cap["tool"])
        self.assertIn("mcp__cv__recall", cap["tool"])
        self.assertNotIn("cv recall", cap["verb"])


if __name__ == "__main__":
    unittest.main()
