#!/usr/bin/env python3
"""helm orcaadopt — SEE/TRACK/RESUME a pane the metaharness launched.

The transport tests drive a REAL Unix socket rather than a mocked one: the whole
point of this seam is that it degrades safely when orca is absent, and a mock of
the socket cannot show that the socket code degrades. `_tmphome` moves HELM_HOME
only, so every test here also pins ORCA_USER_DATA_PATH into a temp dir — without
it the adapter falls back to $HOME/.config/orca and a unit test talks to the
owner's LIVE daemon.
"""
import contextlib
import io
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from tests._tmphome import home as _tmp_home  # noqa: E402

_tmp_home()

import time                              # noqa: E402
from helm import harness, orcaadopt, pk, seats_common  # noqa: E402

# A pane whose composer is EMPTY — one that took its turn. Synthetic, but a
# real frame's shape: `submit` proves delivery by READING THE COMPOSER BACK,
# so a double returning "" would model an UNREADABLE pane (UNKNOWN).
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))



def _proc(pid, seat=None, pane_key=None, resume_sid=None, worktree_id=None,
          start="1000"):
    """A row shaped like `claude_processes()` produces one.

    `start` defaults to a stamp rather than to None on purpose: a real /proc
    row always has one, and a fixture that omitted it would exercise the
    unstamped REFUSAL path in every test instead of the behaviour under test.
    Pass start=None only when the unreadable-stat case IS the subject.
    """
    return {"pid": pid, "start": start, "seat": seat, "pane_key": pane_key,
            "resume_sid": resume_sid, "worktree_id": worktree_id}


def _ident(pid, start="1000"):
    """What an identity ladder hands a delivery leg — pid PLUS birth stamp."""
    return orcaadopt.ProcIdent(pid, start)


# A SYNTHETIC /proc HAS TO MODEL THE WALKER TOO. `claude_processes()` seeds its
# walk with helm's own pid — a walk that cannot see the process doing the
# walking did not happen, so an empty `glob` is blindness and never "this host
# runs no claude". Every fixture below that fakes `glob` therefore also pins
# `os.getpid` to the pid it enumerates: without it the fixture describes a host
# helm is not running on, and the seed would correctly call the census blind.


from tests import socket_dir  # noqa: E402
# The one-shot orca runtime lives in tests/_fakeorca.py: test_codexhomes'
# sync-orca leg drives the same real AF_UNIX transport — one fixture, not two.
from tests._fakeorca import FakeDaemon as _FakeDaemon  # noqa: E402


class RpcFailOpenTest(unittest.TestCase):
    """An absent/broken orca must degrade to today's behaviour: one honest line,
    never an exception out of a helm verb."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-orca-rpc-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        patch = mock.patch.dict(os.environ,
                                {"ORCA_USER_DATA_PATH": self.tmp}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HELM_ORCA_RPC", None)
        self.ad = harness.OrcaAdapter("/fake/bin/orca")

    def test_no_runtime_metadata_at_all(self):
        result, err = self.ad.rpc("terminal.list", {})
        self.assertIsNone(result)
        self.assertIn("orca runtime metadata unavailable", err)

    def test_runtime_metadata_naming_a_dead_socket(self):
        with open(os.path.join(self.tmp, "orca-runtime.json"), "w") as f:
            json.dump({"runtimeId": "rt1", "authToken": "t",
                       "transports": [{"kind": "unix",
                                       "endpoint": os.path.join(
                                           socket_dir(self), "gone.sock")}]},
                      f)
        result, err = self.ad.rpc("terminal.list", {})
        self.assertIsNone(result)
        self.assertTrue(err)

    def test_garbage_on_the_wire_is_an_error_not_a_crash(self):
        d = _FakeDaemon(self.tmp, raw=b"this is not json\n")
        self.addCleanup(d.close)
        result, err = self.ad.rpc("terminal.list", {})
        self.assertIsNone(result)
        self.assertTrue(err)

    def test_malformed_runtime_json_is_an_error_not_a_crash(self):
        with open(os.path.join(self.tmp, "orca-runtime.json"), "w") as f:
            f.write("{not json")
        result, err = self.ad.rpc("terminal.list", {})
        self.assertIsNone(result)
        self.assertIn("orca runtime metadata unavailable", err)

    def test_a_working_daemon_answers_and_carries_the_auth_token(self):
        d = _FakeDaemon(self.tmp, reply={"terminals": [
            {"handle": "h1", "title": "t", "connected": True,
             "writable": True, "worktreePath": "/w"}]})
        self.addCleanup(d.close)
        rows, err = self.ad.panes()
        self.assertIsNone(err)
        self.assertEqual([r["handle"] for r in rows], ["h1"])
        self.assertEqual(rows[0]["status"], "connected")
        self.assertEqual(d.seen[0]["authToken"], "tok")
        self.assertEqual(d.seen[0]["method"], "terminal.list")

    def test_panes_never_answers_a_bare_empty_list_on_failure(self):
        """([], None) means "orca is up and holds nothing"; a failure must carry
        a reason so a caller cannot read one as the other."""
        rows, err = self.ad.panes()
        self.assertEqual(rows, [])
        self.assertTrue(err)

    def test_kill_switch_short_circuits_before_touching_the_socket(self):
        d = _FakeDaemon(self.tmp, reply={"terminals": []})
        self.addCleanup(d.close)
        with mock.patch.dict(os.environ, {"HELM_ORCA_RPC": "off"}):
            result, err = self.ad.rpc("terminal.list", {})
        self.assertIsNone(result)
        self.assertIn("HELM_ORCA_RPC", err)
        self.assertEqual(d.seen, [])

    def test_pane_rows_with_no_metaharness_is_a_reason_not_a_crash(self):
        with mock.patch.object(harness, "detect", return_value=None):
            rows, note = orcaadopt.pane_rows()
        self.assertEqual(rows, [])
        self.assertEqual(note, harness.RECOMMENDATION)


class SeatLivenessTest(unittest.TestCase):
    """The duplicate-session guard. A duplicate on ONE session is the hazard the
    owner named by name, so every branch here is a refusal decision."""

    def _roster(self, mapping, failed=False):
        return mock.patch("helm.seats.roster_checked",
                          return_value=(mapping, failed))

    def test_a_process_naming_the_seat_is_live(self):
        procs = [_proc(11, seat="console-design")]
        with self._roster({}):
            state, why = orcaadopt.seat_liveness("console-design", procs=procs,
                                                 unreadable=[])
        self.assertEqual(state, orcaadopt.LIVE)
        self.assertIn("11", why)

    def test_the_self_resumed_under_a_new_sid_case(self):
        """THE case this guard exists for. The seat was resumed and now runs
        under a NEW session id; the OLD transcript went 51 minutes cold. Rung 1
        is keyed on the SEAT, so the new id changes nothing and the stale
        transcript is never consulted."""
        procs = [_proc(22, seat="console-design", resume_sid="NEW-sid")]
        with self._roster({"console-design": {"session": "OLD-sid"}}):
            state, why = orcaadopt.seat_liveness("console-design", procs=procs,
                                                 unreadable=[])
        self.assertEqual(state, orcaadopt.LIVE)
        self.assertIn("22", why)

    def test_a_roster_session_held_by_a_live_pid_is_live(self):
        """Rung 2: the pane never announced a HELM_CHAT_NAME (measured: this is
        spiral-claude-2), so only the session history can catch it."""
        procs = [_proc(33, seat=None, resume_sid="sid-a")]
        with self._roster({"spiral-claude-2": {"session": "sid-a"}}), \
                mock.patch("helm.sessions.live_sids",
                           return_value={"sid-a": 33}):
            state, why = orcaadopt.seat_liveness("spiral-claude-2", procs=procs,
                                                 unreadable=[])
        self.assertEqual(state, orcaadopt.LIVE)
        self.assertIn("sid-a"[:8], why)

    def test_a_session_only_in_history_still_counts(self):
        """`sessions` is the seat's history — a seat that rolled over is still
        LIVE on an older id someone else is holding open."""
        with self._roster({"s": {"session": "new", "sessions": ["old"]}}), \
                mock.patch("helm.sessions.live_sids",
                           return_value={"old": 44}):
            state, _ = orcaadopt.seat_liveness("s", procs=[], unreadable=[])
        self.assertEqual(state, orcaadopt.LIVE)

    def test_nothing_holding_it_is_dead(self):
        with self._roster({"s": {"session": "sid-x"}}), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            state, _ = orcaadopt.seat_liveness("s", procs=[], unreadable=[])
        self.assertEqual(state, orcaadopt.DEAD)

    def test_an_unreadable_claude_environ_is_unknown_never_dead(self):
        """A probe that CANNOT LOOK must not report absent — the blast-radius
        lesson. An unidentifiable claude process may be this seat."""
        with self._roster({"s": {"session": "sid-x"}}), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            state, why = orcaadopt.seat_liveness(
                "s", procs=[_proc(55)], unreadable=[55])
        self.assertEqual(state, orcaadopt.UNKNOWN)
        self.assertIn("55", why)

    def test_an_unreadable_roster_is_unknown_never_dead(self):
        with self._roster({}, failed=True):
            state, why = orcaadopt.seat_liveness("s", procs=[], unreadable=[])
        self.assertEqual(state, orcaadopt.UNKNOWN)
        self.assertIn("roster", why)

    def test_transcript_age_is_never_consulted(self):
        """Guard against a future 'optimisation': a pane thinking for an hour and
        one that exited an hour ago are identical by mtime. If mtime ever became
        evidence, this seat would be called DEAD while pid 66 holds it."""
        procs = [_proc(66, seat="s")]
        with self._roster({"s": {"session": "ancient"}}):
            state, _ = orcaadopt.seat_liveness("s", procs=procs, unreadable=[])
        self.assertEqual(state, orcaadopt.LIVE)


class ResumeGuardTest(unittest.TestCase):
    """resume must REFUSE before it spawns, not after."""

    def test_live_seat_refuses_and_spawns_nothing(self):
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.LIVE, "pid 9 holds it")), \
                mock.patch("helm.sessions.spawn_resume") as spawn:
            rc, lines = orcaadopt.resume("console-design")
        self.assertEqual(rc, 1)
        spawn.assert_not_called()
        self.assertIn("refusing to resume", lines[0])
        self.assertIn("--force", " ".join(lines))

    def test_unknown_seat_refuses_and_spawns_nothing(self):
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.UNKNOWN, "cannot see")), \
                mock.patch("helm.sessions.spawn_resume") as spawn:
            rc, _ = orcaadopt.resume("s")
        self.assertEqual(rc, 1)
        spawn.assert_not_called()

    def test_force_overrides_but_says_so(self):
        row = {"i": "sid-1234abcd", "h": "claude", "cwd": "/w", "mt": 1}
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.LIVE, "pid 9")), \
                mock.patch.object(orcaadopt, "newest_session_row",
                                  return_value=(row, None)), \
                mock.patch("helm.sessions.resume_warnings", return_value=[]), \
                mock.patch("helm.sessions.credhome_for", return_value=None), \
                mock.patch("helm.sessions.kick_resumed", return_value=True), \
                mock.patch("helm.sessions.spawn_resume",
                           return_value=("/p.sh", "h9", "orca")) as spawn, \
                mock.patch.object(harness, "detect", return_value=object()):
            rc, lines = orcaadopt.resume("s", force=True)
        self.assertEqual(rc, 0)
        self.assertIn("--force: proceeding despite LIVE", " ".join(lines))
        self.assertEqual(spawn.call_args[1]["env"], {"HELM_CHAT_NAME": "s"})

    def test_a_dead_seat_resumes_carrying_its_seat_identity(self):
        """The resumed pane must come back AS the seat: without HELM_CHAT_NAME it
        rejoins chat as an anonymous pane that no longer answers to its name."""
        row = {"i": "sid-1234abcd", "h": "claude", "cwd": "/w", "mt": 1}
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.DEAD, "nothing holds it")), \
                mock.patch.object(orcaadopt, "newest_session_row",
                                  return_value=(row, None)), \
                mock.patch("helm.sessions.resume_warnings", return_value=[]), \
                mock.patch("helm.sessions.credhome_for", return_value=None), \
                mock.patch("helm.sessions.kick_resumed", return_value=True), \
                mock.patch("helm.sessions.spawn_resume",
                           return_value=("/p.sh", "h9", "orca")) as spawn, \
                mock.patch.object(harness, "detect", return_value=object()):
            rc, lines = orcaadopt.resume("console-design")
        self.assertEqual(rc, 0)
        self.assertEqual(spawn.call_args[1]["env"],
                         {"HELM_CHAT_NAME": "console-design"})
        self.assertEqual(spawn.call_args[1]["title"], "console-design")
        self.assertIn("orca-adopted", " ".join(lines))

    def test_no_transcript_is_a_refusal_not_a_blank_pane(self):
        with mock.patch.object(orcaadopt, "seat_liveness",
                               return_value=(orcaadopt.DEAD, "dead")), \
                mock.patch.object(orcaadopt, "newest_session_row",
                                  return_value=(None, "no transcript")), \
                mock.patch("helm.sessions.spawn_resume") as spawn:
            rc, lines = orcaadopt.resume("s")
        self.assertEqual(rc, 1)
        spawn.assert_not_called()
        self.assertIn("no transcript", " ".join(lines))


class ProvenanceTest(unittest.TestCase):
    """helm-spawned and orca-adopted are never merged into one list."""

    def test_rows_are_labelled_by_provenance(self):
        rows = [{"handle": "h-helm"}, {"handle": "h-orca"}, {"handle": "h-none"}]

        class Ad:
            name = "orca"

            def panes(self):
                return list(rows), None

            def resolve_pane(self, key):
                return {"handle": "h-orca"} if key == "pk-1" else {}

        with mock.patch.object(orcaadopt, "helm_spawned",
                               return_value={"codex": {"handle": "h-helm"}}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([_proc(1, seat="console-design",
                                                       pane_key="pk-1")], [])):
            out, note = orcaadopt.pane_rows(adapter=Ad())
        self.assertIsNone(note)
        got = {r["handle"]: (r["provenance"], r["seat"]) for r in out}
        self.assertEqual(got["h-helm"], (orcaadopt.HELM_SPAWNED, "codex"))
        self.assertEqual(got["h-orca"],
                         (orcaadopt.ORCA_ADOPTED, "console-design"))
        self.assertEqual(got["h-none"], (orcaadopt.UNOWNED, None))

    def test_a_failing_resolve_pane_does_not_lose_the_inventory(self):
        class Ad:
            name = "orca"

            def panes(self):
                return [{"handle": "h1"}], None

            def resolve_pane(self, key):
                raise harness.HarnessError("pane gone")

        with mock.patch.object(orcaadopt, "helm_spawned", return_value={}), \
                mock.patch.object(orcaadopt, "claude_processes",
                                  return_value=([_proc(1, seat="s",
                                                       pane_key="pk")], [])):
            out, note = orcaadopt.pane_rows(adapter=Ad())
        self.assertIsNone(note)
        self.assertEqual(out[0]["provenance"], orcaadopt.UNOWNED)


class ResolveTest(unittest.TestCase):

    def test_a_seat_helm_has_never_heard_of_answers_none(self):
        """So `seat where <typo>` keeps its "unknown seat" message — a typo must
        still look like a typo rather than like an adopted pane."""
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch("helm.seats.roster_checked", return_value=({}, False)):
            self.assertIsNone(orcaadopt.resolve("no-such-seat"))

    def test_a_roster_only_seat_resolves_without_a_pane(self):
        """Honest partial answer: the seat is LIVE by session evidence, but its
        pane cannot be attributed because no live process names it."""
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([_proc(7)], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"seafan-claude": {"session": "sid-z"}},
                                         False)), \
                mock.patch("helm.sessions.live_sids",
                           return_value={"sid-z": 7}), \
                mock.patch.object(harness, "detect", return_value=None):
            info = orcaadopt.resolve("seafan-claude")
        self.assertEqual(info["provenance"], orcaadopt.ORCA_ADOPTED)
        self.assertEqual(info["state"], orcaadopt.LIVE)
        self.assertNotIn("handle", info)
        self.assertEqual(info["sessions"], ["sid-z"])


class CensusIsTheInputNotTheVerdictTest(unittest.TestCase):
    """`resolve(census=...)` exists so a caller asking about a whole fleet pays
    for ONE walk of /proc instead of one per seat. The property that makes it
    safe is that it caches the INPUT and never the ANSWER: every rung still
    runs, on the supplied rows, so a caller can buy a staler reading but never
    a different verdict."""

    def _roster(self, seat="seat-a", sid="sid-z", pid=7):
        return (mock.patch("helm.seats.roster_checked",
                           return_value=({seat: {"session": sid}}, False)),
                mock.patch("helm.sessions.live_sids", return_value={sid: pid}),
                mock.patch.object(harness, "detect", return_value=None))

    def test_a_supplied_census_gives_the_same_answer_without_the_walk(self):
        """The whole point, stated as an equality: same rows in, same dict out,
        and the process walk that costs most of the call does not happen."""
        rows = ([_proc(7, seat="seat-a")], [])
        calls = []

        def counted():
            calls.append(1)
            return rows

        roster, sids, detect = self._roster()
        with mock.patch.object(orcaadopt, "claude_processes", counted), \
                roster, sids, detect:
            walked = orcaadopt.resolve("seat-a")
            # MUST-HIT: without a census the walk really is the thing being
            # replaced. Without this the zero below could mean the call never
            # reached the walk for some unrelated reason.
            self.assertEqual(len(calls), 1, "the un-censused call never walked")
            supplied = orcaadopt.resolve("seat-a", census=rows)
            self.assertEqual(len(calls), 1,
                             "a supplied census still walked /proc")
        self.assertEqual(walked, supplied)
        self.assertEqual(supplied["pids"], [7])

    def test_a_census_omitting_the_seats_process_invents_no_holder(self):
        """The failure this parameter could have introduced: a caller supplies
        rows that do not contain this seat and the memo answers from an older,
        richer reading anyway. The verdict must follow the rows it was given."""
        roster, sids, detect = self._roster()
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=AssertionError(
                                   "a supplied census must not be topped up")), \
                roster, sids, detect:
            held = orcaadopt.resolve("seat-a",
                                     census=([_proc(7, seat="seat-a")], []))
            absent = orcaadopt.resolve("seat-a", census=([], []))
        self.assertEqual(held["pids"], [7])
        self.assertEqual(absent["pids"], [],
                         "a census without the process still named a holder")
        self.assertEqual(absent["pane_pids"], [])

    def test_the_blind_refusal_reads_the_supplied_unreadable_list(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on the SAME observable is `blind`: one call to the same function with the same seat returns a dict and an UNKNOWN state, so `clean is None` cannot be satisfied by resolve answering None for everything.
        """A refusal rung, not a positive path: `unreadable` travels in the same
        pair, and R3 refuses to call a seat unheard-of while a process it could
        not read might BE that seat. Supplying the census must not route around
        the rung that turns an unreadable row into an honest UNKNOWN."""
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=AssertionError(
                                   "a supplied census must not be topped up")), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch.object(harness, "detect", return_value=None):
            blind = orcaadopt.resolve("seat-b", census=([], [9]))
            # MUST-MISS control: with nothing unreadable the SAME unknown seat
            # keeps its typo answer, so the dict above is the rung firing and
            # not this parameter making every name resolve.
            clean = orcaadopt.resolve("seat-b", census=([], []))
        self.assertIsNone(clean)
        self.assertIsNotNone(blind)
        self.assertEqual(blind["state"], orcaadopt.UNKNOWN)


class AdoptHomeTest(unittest.TestCase):
    """The slice-0 coverage gap: an ADOPTED pane gets the same private home a
    helm-spawned seat does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-adopt-home-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        import subprocess

        def git(*a):
            subprocess.run(["git", "-C", self.repo] + list(a),
                           capture_output=True, text=True, check=False)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        with open(os.path.join(self.repo, "f"), "w") as f:
            f.write("x\n")
        git("add", "-A")
        git("commit", "-qm", "seed")

    def test_produces_the_same_deterministic_path_as_a_spawned_seat(self):
        class Ad:
            name = "orca"

            def ensure_home_worktree(self, seat, root, base="main"):
                return harness._native_home_worktree(root, seat, base=base)

            def adopt_worktree(self, root, path):
                return True, "adopted"

        path, detail = orcaadopt.adopt_home("console-design", self.repo,
                                            adapter=Ad())
        self.assertEqual(path,
                         harness.seat_worktree_path(self.repo, "console-design"))
        self.assertTrue(os.path.isdir(path))
        self.assertIn("adopted", detail)

    def test_a_failed_orca_adoption_still_yields_a_working_checkout(self):
        class Ad:
            name = "orca"

            def ensure_home_worktree(self, seat, root, base="main"):
                return harness._native_home_worktree(root, seat, base=base)

            def adopt_worktree(self, root, path):
                return False, "orca holds it as a 'folder' context"

        path, detail = orcaadopt.adopt_home("console-design", self.repo,
                                            adapter=Ad())
        self.assertTrue(os.path.isdir(path))
        self.assertIn("DID NOT adopt", detail)
        self.assertIn("folder", detail)

    def test_it_is_idempotent(self):
        class Ad:
            name = "orca"

            def ensure_home_worktree(self, seat, root, base="main"):
                return harness._native_home_worktree(root, seat, base=base)

            def adopt_worktree(self, root, path):
                return True, "adopted"

        first, _ = orcaadopt.adopt_home("s", self.repo, adapter=Ad())
        second, _ = orcaadopt.adopt_home("s", self.repo, adapter=Ad())
        self.assertEqual(first, second)

    def test_no_metaharness_falls_back_to_the_native_floor(self):
        with mock.patch.object(harness, "detect", return_value=None):
            path, detail = orcaadopt.adopt_home("s", self.repo)
        self.assertEqual(path, harness.seat_worktree_path(self.repo, "s"))
        self.assertTrue(os.path.isdir(path))
        self.assertIn("no orca adoption seam", detail)


class SeatVerbFallThroughTest(unittest.TestCase):
    """`seat where` / `seat resume` must reach an adopted pane WITHOUT changing
    what they do for a helm-spawned seat or for a genuine typo."""

    def _run(self, argv):
        import contextlib
        import io
        from helm import seat
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seat.cmd_seat(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_where_resolves_an_adopted_pane_and_labels_provenance(self):
        info = {"seat": "console-design", "provenance": orcaadopt.ORCA_ADOPTED,
                "state": orcaadopt.LIVE, "evidence": "pid 42 names it",
                "sessions": ["sid-abcdefgh"], "pids": [42], "handle": "term_x",
                "pane_key": "pk"}
        with mock.patch.object(orcaadopt, "resolve", return_value=info):
            rc, out, _ = self._run(["where", "console-design"])
        self.assertEqual(rc, 0)
        self.assertIn("orca-adopted", out)
        self.assertIn("LIVE", out)
        self.assertIn("pid 42 names it", out)
        self.assertIn("term_x", out)

    def test_where_on_a_genuine_typo_still_says_unknown_seat(self):
        with mock.patch.object(orcaadopt, "resolve", return_value=None):
            rc, _, err = self._run(["where", "definitely-not-a-seat"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)
        self.assertIn("families:", err)

    def test_where_json_is_machine_readable(self):
        info = {"seat": "s", "provenance": orcaadopt.ORCA_ADOPTED,
                "state": orcaadopt.DEAD, "evidence": "e", "sessions": [],
                "pids": []}
        with mock.patch.object(orcaadopt, "resolve", return_value=info):
            rc, out, _ = self._run(["where", "s", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["provenance"], orcaadopt.ORCA_ADOPTED)

    def test_resume_routes_an_adopted_seat_to_the_guarded_path(self):
        with mock.patch.object(orcaadopt, "resolve", return_value={"seat": "s"}), \
                mock.patch.object(orcaadopt, "resume",
                                  return_value=(1, ["refused"])) as res:
            rc, _, err = self._run(["resume", "console-design"])
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        res.assert_called_once_with("console-design", force=False)

    def test_resume_force_reaches_the_guard(self):
        with mock.patch.object(orcaadopt, "resolve", return_value={"seat": "s"}), \
                mock.patch.object(orcaadopt, "resume",
                                  return_value=(0, ["ok"])) as res:
            rc, _, _ = self._run(["resume", "console-design", "--force"])
        self.assertEqual(rc, 0)
        res.assert_called_once_with("console-design", force=True)

    def test_resume_on_a_genuine_typo_still_says_unknown_seat(self):
        with mock.patch.object(orcaadopt, "resolve", return_value=None):
            rc, _, err = self._run(["resume", "definitely-not-a-seat"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown seat", err)

    def test_resume_still_refuses_trailing_junk_before_it_fires(self):
        with mock.patch.object(orcaadopt, "resume") as res:
            rc, _, _ = self._run(["resume", "codex", "--bogus"])
        self.assertEqual(rc, 2)
        res.assert_not_called()

    def test_panes_groups_by_provenance_and_never_flattens(self):
        rows = [{"handle": "h1", "status": "connected", "worktree": "/w",
                 "provenance": orcaadopt.HELM_SPAWNED, "seat": "codex"},
                {"handle": "h2", "status": "connected", "worktree": "/w",
                 "provenance": orcaadopt.ORCA_ADOPTED, "seat": "console-design"}]
        with mock.patch.object(orcaadopt, "pane_rows",
                               return_value=(rows, None)):
            rc, out, _ = self._run(["panes"])
        self.assertEqual(rc, 0)
        self.assertLess(out.index(orcaadopt.HELM_SPAWNED),
                        out.index("codex"))
        self.assertLess(out.index(orcaadopt.ORCA_ADOPTED),
                        out.index("console-design"))

    def test_panes_admits_when_it_could_not_identify_every_claude(self):
        """An `unowned` row claims "no helm seat identity found". That is only
        honest when the lookup could see everything, so an unreadable claude
        environ has to be reported next to the listing rather than silently
        strengthening the label."""
        rows = [{"handle": "h1", "status": "connected", "worktree": "/w",
                 "provenance": orcaadopt.UNOWNED, "seat": None}]
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([_proc(91)], [91])), \
                mock.patch.object(orcaadopt, "pane_rows",
                                  return_value=(rows, None)):
            rc, out, _ = self._run(["panes"])
        self.assertEqual(rc, 0)
        self.assertIn("could not be identified", out)
        self.assertIn("91", out)

    def test_panes_says_nothing_extra_when_every_claude_was_readable(self):
        rows = [{"handle": "h1", "status": "connected", "worktree": "/w",
                 "provenance": orcaadopt.UNOWNED, "seat": None}]
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([_proc(91, seat="s")], [])), \
                mock.patch.object(orcaadopt, "pane_rows",
                                  return_value=(rows, None)):
            rc, out, _ = self._run(["panes"])
        self.assertEqual(rc, 0)
        self.assertNotIn("could not be identified", out)

    def test_panes_without_an_inventory_is_an_honest_failure(self):
        with mock.patch.object(orcaadopt, "pane_rows",
                               return_value=([], "orca is not running")):
            rc, _, err = self._run(["panes"])
        self.assertEqual(rc, 1)
        self.assertIn("orca is not running", err)


class ResumeScriptEnvTest(unittest.TestCase):
    """The resumed pane's seat identity rides the MINTED SCRIPT, so it behaves
    the same on orca, herdr and headless."""

    def test_env_is_exported_before_exec_and_is_shell_quoted(self):
        from helm import sessions
        tmp = tempfile.mkdtemp(prefix="helm-resume-mint-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        row = {"i": "sid-1", "h": "claude", "cwd": tmp}
        with mock.patch.object(sessions, "RESUME_DIR", tmp), \
                mock.patch.object(sessions, "resume_exec",
                                  return_value="claude --resume sid-1"):
            path = sessions.mint_resume_script(
                row, env={"HELM_CHAT_NAME": "a b; rm -rf /"})
        with open(path) as f:
            text = f.read()
        self.assertIn("export HELM_CHAT_NAME='a b; rm -rf /'", text)
        self.assertLess(text.index("export HELM_CHAT_NAME"),
                        text.index("exec claude"))

    def test_no_env_leaves_the_script_byte_identical_to_before(self):
        from helm import sessions
        tmp = tempfile.mkdtemp(prefix="helm-resume-mint-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        row = {"i": "sid-1", "h": "claude", "cwd": tmp}
        with mock.patch.object(sessions, "RESUME_DIR", tmp), \
                mock.patch.object(sessions, "resume_exec",
                                  return_value="claude --resume sid-1"):
            with open(sessions.mint_resume_script(row)) as f:
                text = f.read()
        self.assertNotIn("export", text)


class SessionJoinTest(unittest.TestCase):
    """A proc row's `seat` comes from the process ENVIRONMENT (HELM_CHAT_NAME),
    which launch.sh exports for seats helm SPAWNS. A seat the operator launched
    himself carries no such var, so its row reads seat=None and an env-only
    match cannot find it — even though the row holds a live pane_key.

    MEASURED: the seat driving fleet recovery showed
    `pane=?` and could not be sent /compact or a plan-approval keystroke. It had
    to ask the owner to press a key — the human gate every recovery verb exists
    to remove. The join was already in hand: roster_sessions() maps seat->session
    and every proc row carries resume_sid."""

    def test_a_nameless_proc_is_found_by_its_session(self):
        proc = _proc(44, pane_key="pk-1", resume_sid="sid-a")   # seat=None
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([proc], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"lone-seat": {"session": "sid-a"}},
                                         False)), \
                mock.patch("helm.sessions.live_sids", return_value={"sid-a": 44}), \
                mock.patch.object(harness, "detect", return_value=None):
            info = orcaadopt.resolve("lone-seat")
        self.assertEqual(info["pids"], [44], "session join did not find the proc")

    def test_the_env_label_still_wins_and_order_is_unchanged(self):
        # ADDITIVE: a spawned seat resolves exactly as before, env row FIRST.
        named = _proc(1, seat="spawned", pane_key="pk-n", resume_sid="sid-x")
        other = _proc(2, pane_key="pk-o", resume_sid="sid-x")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([other, named], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"spawned": {"session": "sid-x"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={"sid-x": 1}), \
                mock.patch.object(harness, "detect", return_value=None):
            info = orcaadopt.resolve("spawned")
        self.assertEqual(info["pids"][0], 1, "env-labelled row must stay first")
        self.assertEqual(len(info["pids"]), 2)

    def test_a_row_matching_both_is_counted_once(self):
        both = _proc(5, seat="dual", pane_key="pk", resume_sid="sid-d")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([both], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"dual": {"session": "sid-d"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={"sid-d": 5}), \
                mock.patch.object(harness, "detect", return_value=None):
            info = orcaadopt.resolve("dual")
        self.assertEqual(info["pids"], [5], "pid must not be duplicated")

    def test_another_seats_session_is_never_claimed(self):
        # MUST-NOT-HIT, and the one that matters: a wrong join here would inject
        # a keystroke into SOMEONE ELSE'S pane (verify-pane-identity-before-
        # injection: the incident where a rebrief hit the wrong seat).
        mine = _proc(9, pane_key="pk-mine", resume_sid="sid-mine")
        theirs = _proc(10, pane_key="pk-theirs", resume_sid="sid-theirs")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([mine, theirs], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-mine"},
                                          "them": {"session": "sid-theirs"}}, False)), \
                mock.patch("helm.sessions.live_sids",
                           return_value={"sid-mine": 9, "sid-theirs": 10}), \
                mock.patch.object(harness, "detect", return_value=None):
            info = orcaadopt.resolve("me")
        self.assertEqual(info["pids"], [9], "resolved another seat's process")


class _PaneAd(harness._CLIAdapter):
    """An orca adapter that turns pane keys into handles and RECORDS its sends.

    Subclasses the real base so it inherits the REAL `submit` — split send plus
    composer read-back — rather than a second implementation of it.

    `dead` names pane keys that no longer resolve. That is the stale half of the
    wrong-pane repro and it is not decoration: the send only reaches the wrong
    pane when the seat's OWN row cannot be turned into a handle, so a fixture
    where every pane resolves would pass no matter what the code did.
    """

    name = "orca"

    def __init__(self, dead=()):
        self.sent, self.dead = [], set(dead)
        self.typed = {}

    def resolve_pane(self, key):
        return {} if key in self.dead else {"handle": "handle-of-" + key}

    def read(self, handle, limit=3000, timeout=60):
        text = self.typed.get(handle)
        if text is not None:
            return ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.typed.pop(handle, None)
        else:
            self.typed[handle] = text
        # The bare-Enter leg carries no text; recording it would double every
        # assertion below without saying anything the split tests do not
        # already pin at the adapter seam.
        if text:
            self.sent.append((handle, text))


class SessionAddressingRefusalTest(unittest.TestCase):
    """The session join resolves an ADDRESS, so every ambiguity must answer NO
    PANE rather than a plausible one.

    REPRODUCED against the first draft of the join (cross-family FIX,
    dispatch 9f04b633): a pane declaring one seat's HELM_CHAT_NAME and resumed on a
    sid still listed in another seat's roster HISTORY was folded in, supplied
    the handle, and send_to_pane injected /compact into the declaring pane and returned
    "resumed" — while expect_pids named the correct pid the entire time. An
    unreachable seat fails loudly; a mis-reachable one types into someone else's
    session and says it worked, which is strictly worse than the bug the join
    was written to fix.
    """

    def test_stale_session_history_never_addresses_another_pane(self):
        # THE ADVERSARIAL MUST-HIT, end to end through the real injection path.
        # sid-old is in this seat's roster history and is codex-3's CURRENT
        # session; the seat's own pane has gone stale, which is what makes the
        # wrong handle reachable at all.
        mine = _proc(101, seat="helm-claude-2", pane_key="pk-mine",
                     resume_sid="sid-current")
        theirs = _proc(202, seat="codex-3", pane_key="pk-theirs",
                       resume_sid="sid-old")
        ad = _PaneAd(dead=["pk-mine"])
        roster = {"helm-claude-2": {"session": "sid-current",
                                    "sessions": ["sid-old"]},
                  "codex-3": {"session": "sid-old"}}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([mine, theirs], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(roster, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "helm-claude-2", "/compact", expect_pids=[_ident(101)], adapter=ad)
        self.assertEqual(ad.sent, [],
                         "INJECTED INTO ANOTHER AGENT'S PANE: %s" % (ad.sent,))
        self.assertEqual(mode, "manual", detail)

    def test_a_nameless_pane_on_a_history_sid_is_not_this_seat(self):
        """The stale-history shape NO other rung catches: the holder is
        nameless, so there is no contradiction to spot, and it is unique, so
        there is no ambiguity either. Only "the current session is the address"
        refuses it — and it must, because a seat whose roster says its current
        session is sid-now is not sitting in a pane replaying sid-old. Somebody
        reopened an old transcript, and that pane is a different agent.
        """
        ghost = _proc(202, pane_key="pk-ghost", resume_sid="sid-old")
        roster = {"me": {"session": "sid-now", "sessions": ["sid-old"]}}
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([ghost], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=(roster, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("me", adapter=_PaneAd())
        self.assertEqual(info["pids"], [],
                         "claimed a pane replaying a STALE session")
        self.assertNotIn("handle", info)

    def test_a_row_declaring_another_seat_names_no_pane(self):
        # CONTRADICTION: the row carries this seat's CURRENT session and yet
        # declares a different seat. _nameless_identity already refuses exactly
        # this ("must not inject into a named seat's pane").
        theirs = _proc(202, seat="codex-3", pane_key="pk-theirs",
                       resume_sid="sid-now")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([theirs], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("me", adapter=_PaneAd())
        self.assertEqual(info["pids"], [], "claimed a row declaring codex-3")
        self.assertNotIn("handle", info)
        self.assertIn("codex-3", info["session_refused"])

    def test_one_session_claimed_by_two_live_pids_names_no_pane(self):
        # AMBIGUITY, and the gap the pid dedup cannot reach: the two candidates
        # ARE different pids, so deduping by pid leaves both in place.
        a = _proc(11, pane_key="pk-a", resume_sid="sid-now")
        b = _proc(12, pane_key="pk-b", resume_sid="sid-now")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([a, b], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("me", adapter=_PaneAd())
        self.assertEqual(info["pids"], [], "guessed between two panes")
        self.assertNotIn("handle", info)
        self.assertIn("11, 12", info["session_refused"])

    def test_the_send_guard_counts_session_joined_panes_too(self):
        # THE DIVERGENCE. send_to_pane recomputed its ambiguity candidates from
        # the ENV LABEL while resolve() had folded in a SESSION row, so one env
        # pane plus one session pane counted as ONE candidate, the guard never
        # fired, and the name-derived handle went to the session row's pane
        # while expect_pids named the env row. Both rows here are legitimately
        # this seat's, so nothing upstream refuses — only the guard can.
        mine = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-was")
        other = _proc(202, pane_key="pk-other", resume_sid="sid-now")
        ad = _PaneAd(dead=["pk-mine"])
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([mine, other], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101)], adapter=ad)
        self.assertEqual(ad.sent, [],
                         "sent to a pane expect_pids did not name: %s" % (ad.sent,))
        self.assertEqual(mode, "manual")
        self.assertIn("101", detail)

    def test_the_handle_binds_to_the_AUTHORIZED_pid_not_the_first_in_order(self):
        """Scenario 1 on dispatch 9f04b633, the MIRROR of the shape
        above: here the env-labelled row is the stale one and the session-only
        row is correct, so expect_pids names the SESSION pid while resolve()'s
        env-first ordering hands back the ENV row's pane. Ordering is not
        authorization — the selected handle must bind to the authorized pid.

        Both rows legitimately belong to this seat, so no refusal rung fires;
        this is purely about which handle the send is bound to.
        """
        stale_env = _proc(101, seat="me", pane_key="pk-stale",
                          resume_sid="sid-was")
        correct = _proc(202, pane_key="pk-correct", resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([stale_env, correct], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(202)], adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent, [("handle-of-pk-correct", "/compact")],
                         "bound to the env row's pane, not the authorized pid's")

    def test_a_repeated_pid_cannot_slip_through_the_join(self):
        """The dedup finding, pinned against a HOSTILE producer rather than
        against the producer's goodwill.

        `seen` was computed once, before the session rows were appended, so a
        list repeating a pid appended it twice. MEASURED: claude_processes()
        cannot actually produce that — it globs /proc/[0-9]*/cmdline, one entry
        per pid — so the literal duplicate is unreachable through the real
        producer. The rewrite makes the question moot from a different
        direction, which is the point of pinning it: multiplicity of ANY kind
        refuses, so there is nothing left for a dedup to get wrong.
        """
        row = _proc(11, pane_key="pk-a", resume_sid="sid-now")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([row, dict(row)], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("me", adapter=_PaneAd())
        self.assertEqual(info["pids"], [], "a repeated pid was appended twice")
        self.assertNotIn("handle", info)

    def test_the_refusal_reaches_the_operator_not_the_floor(self):
        # "I could not tell" and "here is the answer" must not be the same
        # value, and the reason must survive to the surface an operator reads.
        theirs = _proc(202, seat="codex-3", pane_key="pk-theirs",
                       resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([theirs], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(202)], adapter=ad)
        self.assertEqual(mode, "manual")
        self.assertIn("codex-3", detail)
        self.assertNotIn("no live pane resolves", detail)


class SendTimeSnapshotTest(unittest.TestCase):
    """A SHRINKING /proc SNAPSHOT IS A REFUSAL, NEVER A NARROWING.

    Repro 2/2 on dispatch 1b4039cc, reproduced at exact tip 761e20f.
    This is a time-of-check-to-time-of-use bug, and naming it that is what
    tells you which fixes cannot work: re-FILTERING the caller's snapshot
    leaves it exactly as stale as it was, so the identity has to be re-proven
    against a FRESH observation taken at send time, bound to the pid the send
    was authorized to address.

    THE SHAPE. Two panes answer the seat. `resolve()` scans /proc, takes the
    FIRST resolvable pane (the NEIGHBOUR's, because ordering is not
    authorization), and the survival check passes because the authorized pid
    was still alive in THAT scan. The authorized pid then exits. The guard
    re-scans, finds only ONE pane-carrying candidate, concludes there is
    nothing to disambiguate — and sends the neighbour the directive, returning
    "resumed". The set shrinking is precisely what SILENCED the guard.
    """

    def test_a_candidate_that_EXITS_between_check_and_send_ABORTS(self):
        # THE ADVERSARIAL MUST-HIT. The first snapshot's ORDER is load-bearing:
        # pid 202 comes first so resolve() binds ITS handle, while expect_pids
        # authorizes pid 101 — then 101 exits before the send-time scan.
        other = _proc(202, seat="me", pane_key="pk-other",
                      resume_sid="sid-other")
        expected = _proc(101, seat="me", pane_key="pk-expected",
                         resume_sid="sid-expected")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=[([other, expected], []),
                                            ([other], [])]), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101)], adapter=ad)
        self.assertEqual(ad.sent, [],
                         "INJECTED INTO A NEIGHBOUR'S PANE after the "
                         "authorized pid exited: %s" % (ad.sent,))
        self.assertEqual(mode, "manual", detail)
        self.assertIn("101", detail, "the refusal must name the pid that left")
        self.assertNotIn("handle-of-pk-other", detail)

    def test_the_same_two_panes_still_deliver_while_the_pid_is_ALIVE(self):
        """The positive control, and it is not decoration: a refusal that fires
        whenever two panes answer a seat would strand every busy seat, which is
        the mistake `turn_restart_identity` documents one rung earlier. The
        authorized pid is alive in BOTH observations here, so the send must
        land — on ITS pane, never on the neighbour that happened to sort
        first."""
        other = _proc(202, seat="me", pane_key="pk-other",
                      resume_sid="sid-other")
        expected = _proc(101, seat="me", pane_key="pk-expected",
                         resume_sid="sid-expected")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([other, expected], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101)], adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent, [("handle-of-pk-expected", "/compact")],
                         "bound to the neighbour's pane, not the authorized "
                         "pid's")

    def test_adopted_resume_retries_a_stuck_first_Enter(self):
        """The retained trunk symbol proves immediate retry is now forbidden."""
        expected = _proc(101, seat="me", pane_key="pk-expected",
                         resume_sid="sid-expected")
        held = ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0/compact\n")

        class RetryAdapter(_PaneAd):
            def __init__(self):
                super().__init__()
                self.frames = ([ADVANCED_PANE] +
                               [held] * (harness.SUBMIT_VERIFY_READS + 1))
                self.turns = []

            def read(self, handle, limit=3000, timeout=60):
                return self.frames.pop(0)

            def send(self, handle, text, enter=True):
                self.turns.append((handle, text, enter))

        ad = RetryAdapter()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([expected], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101)], adapter=ad)
        handle = "handle-of-pk-expected"
        self.assertEqual(mode, "manual", detail)
        self.assertNotIn("retry", detail)
        self.assertEqual(ad.turns, [(handle, "/compact", False),
                                    (handle, "", True)])

    def test_a_send_with_NO_authorized_pid_refuses_rather_than_guessing(self):
        """The shape both holes share, stated as a rule: with nothing decided
        there is no process to bind a pane to, and "the first pane that answers
        this name" is a guess wearing an answer's clothes."""
        row = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([row], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane("me", "/compact", adapter=ad)
        self.assertEqual(ad.sent, [], "injected with nothing authorizing it")
        self.assertEqual(mode, "manual", detail)
        self.assertIn("authorized no pid", detail)


# ---------------------------------------------------------------------------
# the send-site census — what turns "fixed today" into "cannot come back"
# ---------------------------------------------------------------------------
#
# EVERY pane injection in helm/ is declared here with the authority that picked
# its handle. A new send site FAILS THIS SUITE until someone writes down which
# authority proves its pane, which is the whole point: six wrong-pane defects
# in this file across two review rounds were all the same class — a handle
# chosen on stale, historical or ambiguous evidence — and each was found only
# after it shipped. This is the pattern helm already runs for subprocess spawns
# (tests/test_vcs.py pins `_GIT_SPAWNS_OUTSIDE`), applied to the actuator whose
# failure mode is typing into another agent's live session.
#
# THREE AUTHORITIES, AND THEY DO NOT COLLAPSE INTO ONE. This was measured
# rather than assumed, because "route every send through one primitive" was the
# proposal and it is wrong in a specific, load-bearing way:
#
#   registered   The spawn REGISTER proves the pane, re-read inside the
#                per-seat lifecycle LOCK (`autocompact._pane_action` ->
#                `seat._resolve_registered_pane`). A written record plus a lock
#                is STRICTLY STRONGER proof than any /proc scan, and pushing
#                these through the adopted primitive would DOWNGRADE them —
#                the same reason `helm fleet` was left unwired.
#   fresh-spawn  The handle IS the return value of the `spawn()` call a few
#                lines above. There is no candidate set and no "which pane"
#                question to get wrong.
#   adopted      No register, no lock — /proc and the chat roster are all there
#                is. THIS is the only substrate where a candidate SET exists,
#                and every wrong-pane defect has lived here. It is the one that
#                gets the choke point.
#
# So the collapse is real but BOUNDED: the two adopted paths (name-addressed
# and session-addressed) became modes of ONE primitive with one refusal
# contract, and the other six sites are pinned with the authority they already
# had rather than moved onto weaker evidence.
_SEND_AUTHORITIES = {
    ("autocompact.py", "_rebrief_after_clear"): (
        "delegated", "re-brief after /clear; `handle` is a PARAMETER, proven "
        "by _retry_rebrief's _pane_action transaction and handed down"),
    ("autocompact.py", "_fire_clear"): (
        "delegated", "/clear into an overflowed pane; `handle` is a PARAMETER, "
        "proven by _overflow_action's _pane_action transaction"),
    ("autocompact.py", "_actuate_compact"): (
        "delegated", "/compact actuation; `handle` is a PARAMETER proven by "
        "_fire's enclosing _pane_action transaction"),
    ("resumeturn.py", "deliver"): (
        "registered", "the turn-restart directive for a helm-SPAWNED seat; the "
        "adopted branch above it delegates to orcaadopt instead"),
    ("seat.py", "_spawn_submit"): (
        "delegated", "the ONE onboarding submit both spawn legs share; "
        "`handle` is a PARAMETER, and each caller (the proxy leg in `_spawn`, "
        "the native leg in `_spawn_native`) assigns it from its OWN ad.spawn() "
        "return line immediately above the call — pinned structurally by "
        "test_the_shared_onboarding_submit_is_handed_a_freshly_spawned_handle"),
    ("seat.py", "_resume"): (
        "fresh-spawn", "the wake-path re-arm prompt into the pane ad.spawn() "
        "just returned (row #153: a resumed seat's beacon died with the old "
        "process, and only an injected turn can re-arm it); and, on the "
        "into_pane leg `seat resume --all` alone drives, the terminal-disarm "
        "line then the launch line into a pane the sweep proved holds NO "
        "process and that the register's own pane key resolves to"),
    ("sessions.py", "kick_resumed"): (
        "delegated", "resume kick; `handle` is a PARAMETER, supplied by the "
        "caller's own ad.spawn() return a few lines earlier"),
    ("orcaadopt.py", "send_to_pane"): (
        "adopted", "name-addressed injection into an adopted pane"),
    ("orcaadopt.py", "send_to_sid_pane"): (
        "adopted", "session-addressed injection into a NAMELESS adopted pane"),
    ("harness.py", "submit"): (
        "delegated", "the TURN verb's text leg — it types only after proving a "
        "clean composer, then requires exact equality before delegating Enter; "
        "its caller already proved the handle"),
    ("harness.py", "choose_in_modal"): (
        "delegated", "the MODAL-CHOICE verb, which is NOT a submit leg: it "
        "re-reads the pane and derives the option from THAT tail, refuses "
        "anything that is not a recognised dialog still offering the asked "
        "intent, spends NO Enter, and types into the handle its caller "
        "(resumeturn's registered transaction or orcaadopt's authorized "
        "primitive) already proved"),
    ("harness.py", "_press_enter"): (
        "delegated", "the TURN verb's bare-Enter leg — submit reaches it only "
        "through the Enter act door's final HOLDS capture, and recovery only "
        "through its own final exact recorded-text capture"),
    ("seat_rehome.py", "apply_rehome"): (
        "adopted", "two keystrokes into ONE pane: the /exit, submitted from an "
        "operation inside send_to_pane that refuses unless the handle bound at "
        "send time is the one orcaadopt.authorized_handle bound in this "
        "function; and then the `helm launch` line into that same handle. The "
        "exit proof (that exact process PROVEN gone through beacons.pid_alive "
        "on the same birth stamp) is what makes the pane a bare shell rather "
        "than another agent's session by the time the second one is typed"),
    ("planprompt.py", "_send_choice"): (
        "registered", "the plan-execution choice into a registered seat; the "
        "handle, blocked state, prompt type, plan path and contents, and exact "
        "affirmative choice are all re-proved under the seat lifecycle lock "
        "immediately before the send"),
}

# EVERY method name that can put keystrokes into a pane. `send` is the
# transport; `submit` is the turn verb (text, then a bare Enter, then a
# read-back). A census that knows only one of them is blind to the other.
_INJECT = ("send", "submit")

# `submit` is ALSO concurrent.futures' method, and helm runs thread pools. The
# ONE exclusion is by receiver name, from this explicit list — deliberately
# NOT a type resolution, and deliberately erring the SAFE way: an executor
# bound to some other name is over-matched, which costs one declaration line
# and a moment of confusion. Under-matching is what makes a census report zero
# and get believed, which is the failure this whole guard exists to prevent.
_EXECUTOR_RECEIVERS = ("ex", "pool", "executor")


def _is_executor_submit(fn):
    import ast
    return (fn.attr == "submit" and isinstance(fn.value, ast.Name)
            and fn.value.id in _EXECUTOR_RECEIVERS)


def _is_non_pane_send(fn):
    """Exact owner-layer transports whose method name is not pane injection."""
    import ast
    return (isinstance(fn.value, ast.Name)
            and ((fn.attr == "send" and fn.value.id == "dispatches")
                 # fabgate.submit posts a frozen gate-job contract through the
                 # Fab builder sink (HTTP/ssh) — no handle, no composer, no
                 # pane. Same exact receiver-name+method pair idiom as the
                 # dispatches.send exclusion above; the delegated-handle arm
                 # proved the declaration route wrong for a non-pane site.
                 or (fn.attr == "submit" and fn.value.id == "builder")))

# PINNED so the actuator's reach cannot grow quietly. Moving this number means
# a new way to type into a pane exists; declare it above with its authority.
# MEASURED, not estimated: autocompact's three /clear+/compact sends, the
# harness TURN split across submit's text leg and _press_enter's one bare
# Enter, sessions' resume kick, the turn-verb callers, and planprompt's
# prompt-revalidated choice. Removing submit's immediate retry removed one site.
# +2 — _resume's into_pane leg types the disarm line and then the
# launch line into the pane a post-reboot sweep proved dead (seat_resume_all).
# +1 — a vendor modal is a DIFFERENT OPERATION, not a submit leg: it has no
# composer to protect and its option must be re-derived at the keystroke, so
# choose_in_modal is its own pane-injection site.
# +0 — task/2440 MOVED spawn's onboarding submit out of `_spawn` and into the
# shared `_spawn_submit` both spawn legs now call. The COUNT is unchanged (one
# call site, relocated), but the declaration key is the enclosing function, so
# the census correctly demanded a re-declaration: a refactor that hands a new
# function the keystrokes must re-state whose proof chose the pane.
# +2 — task/2573: `seat rehome` submits the /exit and then types the launch
# line into the pane that /exit emptied. Both keystrokes are attributed to
# `apply_rehome` (the census keys on the OUTERMOST function, and the /exit is
# submitted from a nested operation), and the second is the first adopted send
# whose pane holds no claude process at the keystroke — which is why the
# declaration names the exit proof beside authorized_handle. The two are ONE
# binding by construction: the operation refuses to submit unless the pane
# send_to_pane bound is the one apply_rehome bound, and the launch line then
# goes to that same handle.
_SEND_SITES = 17


def _helm_sources(root):
    """Every .py under helm/, RECURSIVELY, as (key, absolute path).

    `os.listdir` saw only the immediate helm/*.py and therefore could not see
    seven nested packages — clarity/ configs/ cred/ inject/ premise/ store/
    work/ (the finding). A new `ad.send` in any of them was
    invisible to the census and the suite stayed green, so the guard's
    coverage stopped at a directory boundary that means nothing to an
    actuator. Measured at the time of the fix: those subtrees hold ZERO send
    sites, so the blindness was LATENT — the guard was never wrong, it was
    merely unable to become wrong in the place it was needed.

    The key stays the bare filename for a top-level module, so every existing
    `_SEND_AUTHORITIES` entry keeps its spelling; nested files key on their
    path under helm/ (`work/queue.py`).
    """
    out = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(here, name)
            out.append((os.path.relpath(path, root), path))
    return sorted(out)


def _send_sites():
    """[(module, outermost_function, lineno, source_of_that_function)] for every
    `<something>.send(...)` in helm/.

    Deliberately matches on the METHOD NAME alone rather than on a resolved
    adapter type. Over-matching costs one declaration line; under-matching is
    how a census reports zero and gets believed — and the receiver here is
    whatever `harness.detect()` returned, which no static pass can resolve.

    `submit` JOINS `send` in `_INJECT` for exactly that reason. When the turn
    verb was introduced and the adopted call sites moved onto it, a census
    matching only "send" reported ZERO adopted sites and stayed green — the
    guard did not fail, it went BLIND, which is the failure it exists to
    prevent. Any future verb that reaches a pane belongs in `_INJECT` in the
    same commit that creates it.
    """
    import ast
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "helm")
    out = []
    for name, path in _helm_sources(root):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)

        def walk(node, outer):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, outer or child)
                    continue
                if isinstance(child, ast.Call):
                    fn = child.func
                    if isinstance(fn, ast.Attribute) and fn.attr in _INJECT \
                            and not _is_executor_submit(fn) \
                            and not _is_non_pane_send(fn):
                        params = [a.arg for a in outer.args.args] if outer else []
                        out.append((name, outer.name if outer else "<module>",
                                    child.lineno,
                                    (ast.get_source_segment(src, outer) or "")
                                    if outer else src,
                                    params))
                walk(child, outer)

        walk(tree, None)
    return out


class SendSiteCensusTest(unittest.TestCase):
    """Every pane injection in helm/ declares the authority that chose its
    handle — enforced, not documented."""

    def test_every_send_site_is_declared_with_an_authority(self):
        sites = _send_sites()
        seen = {(mod, fn) for mod, fn, _ln, _src, _p in sites}
        undeclared = sorted(seen - set(_SEND_AUTHORITIES))
        self.assertEqual(
            undeclared, [],
            "a NEW way to type into a pane appeared. Declare it in "
            "_SEND_AUTHORITIES with the authority that proves its pane — "
            "'adopted' sends must route through orcaadopt.authorized_handle: "
            "%r" % (undeclared,))
        stale = sorted(set(_SEND_AUTHORITIES) - seen)
        self.assertEqual(stale, [], "these send sites are gone; drop them from "
                         "_SEND_AUTHORITIES: %r" % (stale,))
        self.assertEqual(len(sites), _SEND_SITES,
                         "the number of pane injections in helm/ moved "
                         "(%d -> %d)" % (_SEND_SITES, len(sites)))
        for key, (authority, reason) in _SEND_AUTHORITIES.items():
            self.assertIn(authority,
                          ("registered", "fresh-spawn", "adopted", "delegated"),
                          key)
            self.assertGreater(len(reason), 20, key)

    def test_every_ADOPTED_send_routes_through_the_one_primitive(self):
        """The rung that makes the shape unrepresentable rather than merely
        currently-correct. An adopted send that picks its own handle is the
        exact defect this lane closed twice; if a third one is ever written,
        this fails before it can reach a pane."""
        checked = 0
        for mod, fn, lineno, src, _params in _send_sites():
            # .get, not [] — an UNDECLARED site is reported by the census
            # test above; it must not turn these three into KeyErrors and
            # bury the one message that says what to do about it.
            authority = (_SEND_AUTHORITIES.get((mod, fn)) or (None, None))[0]
            if authority != "adopted":
                continue
            checked += 1
            self.assertIn("authorized_handle(", src,
                          "%s:%s (line %d) sends into an ADOPTED pane without "
                          "going through the addressing primitive"
                          % (mod, fn, lineno))
        # FOUR SITES, THREE ADDRESSING MODES: `seat_rehome.apply_rehome`
        # contributes TWO of them under one declaration — the /exit it
        # submits from an operation inside send_to_pane, and the launch line
        # it then types into the handle that exit was spent on. This count is
        # of SITES, not of declarations, so a function that grows a second
        # keystroke moves it; that is the census working, because the second
        # keystroke is the one whose pane holds no claude process.
        self.assertEqual(checked, 4, "the adopted send sites moved; the census "
                         "must still cover every addressing mode (name, "
                         "session, and the rehome's exit-proven pane)")

    def test_every_REGISTERED_send_stays_inside_the_seat_transaction(self):
        """The negative half: these must NOT drift onto the /proc substrate.
        Their proof (spawn register re-read under the lifecycle lock) is
        stronger than anything the adopted primitive can offer, so 'route
        everything through one function' would be a downgrade here."""
        for mod, fn, lineno, src, _params in _send_sites():
            # .get, not [] — an UNDECLARED site is reported by the census
            # test above; it must not turn these three into KeyErrors and
            # bury the one message that says what to do about it.
            authority = (_SEND_AUTHORITIES.get((mod, fn)) or (None, None))[0]
            if authority != "registered":
                continue
            self.assertTrue(
                "_pane_action(" in src or "_resolve_registered_pane(" in src,
                "%s:%s (line %d) is declared register-authorized but no longer "
                "resolves its pane through the seat transaction"
                % (mod, fn, lineno))

    def test_the_shared_onboarding_submit_is_handed_a_freshly_spawned_handle(self):
        """The declaration above says `_spawn_submit` is DELEGATED, and the
        structural arm below only proves it TAKES a `handle`. This one proves the
        other half — that each caller's handle is the return value of that
        caller's own `ad.spawn()` — because a shared submit is exactly the shape
        under which a third caller could hand it a handle it read out of a
        register or a /proc scan, and the census could not tell.

        CONTROL / non-vacuity: the checked count is asserted to be 2 (the proxy
        leg and the native leg), so a rename or a lost call site fails here
        rather than passing an empty loop. Blast radius: helm/seat.py's two
        onboarding call sites only — this reads source, actuates nothing.
        """
        import ast
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm")
        with open(os.path.join(root, "seat.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        callers = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [c for c in ast.walk(node)
                     if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Name)
                     and c.func.id == "_spawn_submit"]
            if not calls:
                continue
            callers.append(node.name)
            spawned = {t.id for a in ast.walk(node)
                       if isinstance(a, ast.Assign)
                       and isinstance(a.value, ast.Call)
                       and isinstance(a.value.func, ast.Attribute)
                       and a.value.func.attr == "spawn"
                       for t in a.targets if isinstance(t, ast.Name)}
            for call in calls:
                handle = call.args[1] if len(call.args) > 1 else None
                self.assertTrue(
                    isinstance(handle, ast.Name) and handle.id in spawned,
                    "seat.py:%s (line %d) hands the shared onboarding submit a "
                    "handle it did not get from its own ad.spawn() — it is "
                    "CHOOSING a pane while declaring the submit delegated"
                    % (node.name, call.lineno))
        self.assertEqual(sorted(callers), ["_spawn", "_spawn_native"],
                         "the onboarding submit's callers moved; re-point this "
                         "arm rather than deleting it")

    def test_the_census_REACHES_A_NESTED_PACKAGE(self):
        """NON-VACUITY, and without it the recursion is a wider guard with the
        same hole. The round-3 finding 3: the census walked `os.listdir`,
        so it saw helm/*.py and NONE of the seven nested packages (clarity/
        configs/ cred/ inject/ premise/ store/ work/). A new `ad.send` in any
        of them typed into a pane with no declared authority and the suite
        stayed green.

        MEASURED WHEN THE RECURSION LANDED: those subtrees hold zero send
        sites, so nothing was exploiting it — which is exactly why only a
        PLANTED one can prove the guard now reaches there. Asserting the count
        is still 8 proves nothing; asserting that a fake site MAKES IT FAIL
        does."""
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm")
        pkg = os.path.join(root, "work")
        self.assertTrue(os.path.isdir(pkg),
                        "helm/work/ is the nested package this pins — if it "
                        "moved, re-point the probe rather than deleting it")
        probe = os.path.join(pkg, "_census_nonvacuity_probe.py")

        def clean():
            if os.path.exists(probe):
                os.unlink(probe)

        self.addCleanup(clean)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("def sneaks_past(ad, text):\n"
                     "    ad.send('handle-i-picked-myself', text, enter=True)\n")
        planted = [(mod, fn) for mod, fn, _l, _s, _p in _send_sites()]
        self.assertIn(("work/_census_nonvacuity_probe.py", "sneaks_past"),
                      planted,
                      "the census cannot SEE a send site in a nested package, "
                      "so its declaration requirement stops at helm/*.py")
        with self.assertRaises(AssertionError):
            self.test_every_send_site_is_declared_with_an_authority()
        clean()
        self.test_every_send_site_is_declared_with_an_authority()

    def test_a_DELEGATED_send_really_does_receive_its_handle(self):
        """`delegated` is the one label that could become a loophole — it says
        "somebody else proved this pane" — so it is the one the census checks
        STRUCTURALLY rather than trusting the note. The handle must genuinely
        arrive as a parameter; a function that starts CHOOSING a handle while
        still declaring itself delegated fails here."""
        for mod, fn, lineno, _src, params in _send_sites():
            # .get, not [] — an UNDECLARED site is reported by the census
            # test above; it must not turn these three into KeyErrors and
            # bury the one message that says what to do about it.
            authority = (_SEND_AUTHORITIES.get((mod, fn)) or (None, None))[0]
            if authority != "delegated":
                continue
            self.assertIn("handle", params,
                          "%s:%s (line %d) claims a delegated handle but does "
                          "not take one — it is choosing a pane"
                          % (mod, fn, lineno))


class ProcIncarnationTest(unittest.TestCase):
    """A PID IS A SLOT THE KERNEL RECYCLES, NOT A NAME.

    The round-3 finding 1 (dispatch a4051c69), reproduced at 6c279fe:
    `authorized_handle` bound authorization to the NUMBER, so a decided pid
    that exited and had its number handed to another agent's pane still
    answered "yes, alive" at send time — and the directive went out, returning
    "resumed". The identity a send re-proves is (pid, birth stamp).
    """

    def test_the_birth_stamp_is_parsed_from_the_LAST_paren(self):
        """THE MUST-HIT FOR THE PARSER. /proc/<pid>/stat field 2 is `comm` in
        parentheses, and comm may itself contain spaces AND parentheses. A
        naive `.split()` mis-numbers every field after it, so this fixture is
        built to make a naive parse return the WRONG number rather than fail —
        a silent wrong stamp calls two different processes the same one."""
        raw = (b"4242 (claude (odd) v2) S 1 4242 4242 0 -1 4194304 900 0 0 0 "
               b"11 22 0 0 20 0 33 0 88401 123456 789 18446744073709551615\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=raw)):
            self.assertEqual(orcaadopt.proc_start(4242), "88401")
        naive = raw.decode().split()[21]
        self.assertNotEqual(naive, "88401",
                            "the fixture no longer punishes a naive split, so "
                            "this test would pass against a broken parser")

    def test_the_birth_stamp_of_a_REAL_process_is_read_and_is_stable(self):
        """Ground truth, not a fixture: this very process. A stamp the kernel
        writes once and never edits must read the same twice."""
        mine = orcaadopt.proc_start(os.getpid())
        self.assertTrue(mine and mine.isdigit(), mine)
        self.assertEqual(mine, orcaadopt.proc_start(os.getpid()))

    def test_a_short_or_absent_stat_is_UNKNOWN_never_a_stamp(self):
        self.assertIsNone(orcaadopt.proc_start(0x7FFFFFF0))
        with mock.patch("builtins.open", mock.mock_open(read_data=b"1 (x) S 1\n")):
            self.assertIsNone(orcaadopt.proc_start(1))

    def test_a_RECYCLED_pid_REFUSES_instead_of_matching(self):
        """THE ADVERSARIAL MUST-HIT, and it is the reported shape exactly. Snapshot
        one holds pid 101 in MY pane; it exits; snapshot two holds pid 101
        again — same number, another agent's pane. Everything a number-keyed
        check can ask ("is 101 alive?", "did the candidate set shrink?", "does
        the prior handle's pid match?") answers YES here, which is why this
        survived two rounds of careful counting."""
        old = _proc(101, seat="me", pane_key="pk-old", resume_sid="sid-now",
                    start="1000")
        new = _proc(101, seat="me", pane_key="pk-new", resume_sid="sid-now",
                    start="9999")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               side_effect=[([old], []), ([new], [])]), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "SECRET", expect_pids=[_ident(101, "1000")], adapter=ad)
        self.assertEqual(ad.sent, [],
                         "TYPED INTO A RECYCLED PID'S PANE: %s" % (ad.sent,))
        self.assertEqual(mode, "manual", detail)
        self.assertIn("DIFFERENT process", detail)
        self.assertIn("9999", detail, "the refusal must show what it observed")

    def test_the_SAME_incarnation_still_delivers(self):
        """The positive control. A refusal that fired whenever a pid recurred
        would refuse every send there has ever been, and would pass the test
        above for the wrong reason."""
        row = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now",
                    start="1000")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([row], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101, "1000")], adapter=ad)
        self.assertEqual(mode, "resumed", detail)
        self.assertEqual(ad.sent, [("handle-of-pk-mine", "/compact")])

    def test_an_UNSTAMPED_authorization_refuses_rather_than_comparing_numbers(self):
        """A caller that hands over a bare int is naming a slot, and a slot
        cannot be re-proven. Accepting it would silently restore the pid-only
        comparison this whole type exists to end."""
        row = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([row], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[101], adapter=ad)
        self.assertEqual(ad.sent, [])
        self.assertEqual(mode, "manual", detail)
        self.assertIn("birth stamp", detail)

    def test_the_identity_ladder_hands_down_a_STAMPED_token(self):
        """The two halves have to meet: a decision that returns a bare int
        would make every send above refuse, so the ladder's own output is
        pinned rather than assumed."""
        row = _proc(101, seat="me", pane_key="pk", resume_sid="sid-now",
                    start="4242")
        with mock.patch("helm.seats.roster_checked",
                        return_value=({"me": {"session": "sid-now"}}, False)):
            pid, why = orcaadopt.turn_restart_identity("me", "sid-now",
                                                       procs=[row], unreadable=[])
        self.assertEqual(pid, 101, why)
        self.assertEqual(pid.start, "4242")
        self.assertEqual(orcaadopt.ident_token(pid), "101:4242")
        self.assertEqual(orcaadopt.ident_key(orcaadopt.parse_ident("101:4242")),
                         (101, "4242"))

    def test_claude_processes_carries_the_stamp_for_every_row(self):
        """The producer half. A row without a stamp cannot authorize anything,
        so a scan that stopped gathering them would disarm every send while
        every unit test above still passed on its fixtures."""
        with mock.patch.object(orcaadopt, "proc_start", return_value="5150"):
            procs, _unreadable = orcaadopt.claude_processes()
        for p in procs:
            self.assertEqual(p.get("start"), "5150", p)


class RosterIdentityTest(unittest.TestCase):
    """History is LIVENESS evidence; only the current session is an ADDRESS.

    A false LIVE costs one loud refusal. A false address types into another
    agent's pane, silently. Measured on the live roster: one seat
    carried 7 historical sids and another 8.
    """

    def test_history_is_kept_for_liveness_and_current_is_split_out(self):
        roster = {"s": {"session": "sid-now", "sessions": ["sid-old", "sid-now"]}}
        with mock.patch("helm.seats.roster_checked", return_value=(roster, False)):
            current, sids, failed = orcaadopt.roster_identity("s")
        self.assertFalse(failed)
        self.assertEqual(current, "sid-now")
        self.assertEqual(sids, ["sid-now", "sid-old"])

    def test_an_unreadable_roster_yields_no_current_session(self):
        # UNKNOWN must never arrive as "this seat has no current session",
        # which would read as permission to fall back to something weaker.
        with mock.patch("helm.seats.roster_checked", return_value=({}, True)):
            current, sids, failed = orcaadopt.roster_identity("s")
        self.assertTrue(failed)
        self.assertIsNone(current)
        self.assertEqual(sids, [])


class _TearingProc:
    """A /proc for ONE pid THAT CHANGES UNDER THE READER.

    The recycle lands at a fixed point in the read order — right after environ,
    the last payload file — rather than after the Nth stat read. That matters:
    keying the fixture on a call COUNT would encode how many stamps a correct
    implementation happens to take, and would then pass or fail for reasons
    unrelated to whether the row is coherent. Keying it on the payload boundary
    states the physical event ("the kernel handed the number to a successor
    while helm was reading") and lets any implementation meet it or not.

    `before`/`after` are the two birth stamps; either may be None, which is the
    stat read failing — the process is gone, or /proc will not answer.
    """

    STAT = (b"101 (claude) S 1 101 101 0 -1 4194304 900 0 0 0 11 22 0 0 20 0 "
            b"33 0 %s 123456 789 18446744073709551615\n")

    # THE FIXTURE SID IS UUID-SHAPED BECAUSE THE READER NOW CHECKS THE SHAPE.
    # `--resume` also takes a human session TITLE, and taking the token after
    # the flag as a sid whatever it was is what declared a live mid-turn pane
    # gone (measured, argv `--resume "helm coordinator 7-22"`). A
    # placeholder that no real session could wear would make these positive
    # controls assert a value the reader is now right to discard.
    def __init__(self, before, after, seat="victim", pane_key="pk-victim",
                 resume_sid="5f1c9a20-1111-2222-3333-444444444444", comm=b"claude\n"):
        self.before, self.after = before, after
        self.seat, self.pane_key, self.resume_sid = seat, pane_key, resume_sid
        self.comm = comm
        self.recycled = False
        self.stat_reads = []

    def _read(self, path):
        name = path.rsplit("/", 1)[-1]
        if name == "stat":
            stamp = self.after if self.recycled else self.before
            self.stat_reads.append(stamp)
            if stamp is None:
                raise OSError(3, "No such process")
            return self.STAT % stamp.encode()
        if name == "comm":
            if self.comm is None:
                raise OSError(2, "No such file")
            return self.comm
        if name == "cmdline":
            return b"claude\0--resume\0%s\0" % self.resume_sid.encode()
        if name == "environ":
            out = (b"HELM_CHAT_NAME=%s\0ORCA_PANE_KEY=%s\0"
                   % (self.seat.encode(), self.pane_key.encode()))
            self.recycled = True     # the number changes hands HERE
            return out
        raise OSError(2, "No such file")

    def scan(self):
        """Run the real `claude_processes()` against this fake /proc."""
        import io
        real_open = open

        def opener(path, mode="r", *a, **kw):
            if str(path).startswith("/proc/"):
                return io.BytesIO(self._read(str(path)))
            return real_open(path, mode, *a, **kw)

        with mock.patch("builtins.open", opener), \
                mock.patch.object(orcaadopt.os, "getpid", return_value=101), \
                mock.patch.object(orcaadopt.glob, "glob",
                                  return_value=["/proc/101/cmdline"]):
            return orcaadopt.claude_processes()


class TornSnapshotTest(unittest.TestCase):
    """/proc IS NOT A SNAPSHOT, so the row must be read under a seqlock.

    The round-4 finding (dispatch ad157755, reproduced at 9bdc9fb3):
    `claude_processes` read cmdline/comm/environ and THEN one trailing birth
    stamp, so a pid recycled inside that window produced a row that describes
    no process — the victim's `--resume` sid and pane key welded to the
    successor's stamp. Round 4 made the identity unforgeable; this makes its
    CONSTRUCTION untearable. Measured before the fix: procs=[{'pid': 101,
    'start': '9999', 'pane_key': 'pk-victim', 'resume_sid': '5f1c9a20-1111-2222-3333-444444444444'}]
    from a single stat read.
    """

    def test_a_stamp_that_CHANGES_mid_read_DISCARDS_the_row(self):
        """THE ADVERSARIAL MUST-HIT. The payload is entirely readable and
        entirely self-consistent — only the stamp moves. A guard that merely
        re-read the stamp and kept the fields anyway would pass every other
        test in this class and still hand out the forged row."""
        world = _TearingProc("1000", "9999")
        procs, unreadable = world.scan()
        self.assertEqual(
            procs, [],
            "TORN ROW SURVIVED: the victim's session welded to a stranger's "
            "birth stamp — %s" % (procs,))
        self.assertEqual(unreadable, [101],
                         "a pid helm could not read coherently must reach the "
                         "honest-UNKNOWN bucket, not vanish from both lists")
        self.assertEqual(world.stat_reads, ["1000", "9999"],
                         "the stamp must be read BEFORE and AFTER the payload; "
                         "one read cannot detect a change")

    def test_the_torn_payload_can_NEVER_become_an_ADDRESS(self):
        """The outcome, not the bookkeeping. Neither the victim's stamp nor
        the successor's may spend the torn row's pane key: the whole point is
        that helm cannot say which process `pk-victim` belongs to, so BOTH
        answers are refusals rather than a coin flip between two panes."""
        for stamp in ("1000", "9999"):
            world = _TearingProc("1000", "9999")
            ad = _PaneAd()
            import io
            real_open = open

            def opener(path, mode="r", *a, **kw):
                if str(path).startswith("/proc/"):
                    return io.BytesIO(world._read(str(path)))
                return real_open(path, mode, *a, **kw)

            with mock.patch("builtins.open", opener), \
                    mock.patch.object(orcaadopt.os, "getpid",
                                      return_value=101), \
                    mock.patch.object(orcaadopt.glob, "glob",
                                      return_value=["/proc/101/cmdline"]):
                handle, why = orcaadopt.authorized_handle(
                    [_ident(101, stamp)], ad)
            self.assertIsNone(handle,
                              "authorized at %s and got an address anyway: %s"
                              % (stamp, handle))
            self.assertEqual(ad.sent, [])
            self.assertNotIn("pk-victim", why or "",
                             "the discarded pane key must not resurface as an "
                             "address in the refusal")

    def test_a_NULL_stamp_on_the_SECOND_read_REFUSES(self):
        """`proc_start` returning None is "I could not read it", never
        "unchanged". None == None is the accident that would quietly turn two
        failed reads into a matched pair."""
        world = _TearingProc("1000", None)
        procs, unreadable = world.scan()
        self.assertEqual(procs, [], procs)
        self.assertEqual(unreadable, [101])
        self.assertEqual(world.stat_reads, ["1000", None])

    def test_a_NULL_stamp_on_the_FIRST_read_REFUSES(self):
        """The mirror half. An unreadable opening stamp leaves nothing to
        compare against, so accepting the closing one would authorize a row on
        exactly one observation — the defect, rebuilt from the other end."""
        world = _TearingProc(None, "9999")
        procs, unreadable = world.scan()
        self.assertEqual(procs, [], procs)
        self.assertEqual(unreadable, [101])

    def test_BOTH_stamps_null_is_STILL_a_refusal_not_a_match(self):
        """The one that None == None would wave straight through."""
        world = _TearingProc(None, None)
        procs, unreadable = world.scan()
        self.assertEqual(procs, [], procs)
        self.assertEqual(unreadable, [101])

    def test_a_STABLE_stamp_still_produces_the_row(self):
        """The positive control. A seqlock that discarded everything would pass
        every test above and disarm the whole substrate — no row means no seat
        is ever addressable."""
        world = _TearingProc("1000", "1000")
        procs, unreadable = world.scan()
        self.assertEqual(unreadable, [])
        self.assertEqual(len(procs), 1, procs)
        self.assertEqual(procs[0], {"pid": 101, "start": "1000",
                                    "seat": "victim", "pane_key": "pk-victim",
                                    "worktree_id": None,
                                    "resume_sid": "5f1c9a20-1111-2222-3333-444444444444"})

    def test_a_pid_that_was_never_claude_is_SKIPPED_not_UNKNOWN(self):
        """`unreadable` means "a claude process helm could not identify". A
        non-claude pid is not one, and folding it in would make every scan on a
        busy host report blindness and refuse."""
        gone = _TearingProc("1000", "1000", comm=None)       # comm unreadable
        self.assertEqual(gone.scan(), ([], []))
        other = _TearingProc("1000", "1000", comm=b"bash\n")  # not claude
        self.assertEqual(other.scan(), ([], []))
        self.assertEqual(other.stat_reads, [],
                         "the seqlock must open AFTER the claude filter, or "
                         "every pid on the host pays two stat reads")


class _Inc:
    """ONE process that held a pid: what it IS, what it RAN, what it inherited.

    `gone` is the whole-process version of UNKNOWN: /proc has no directory at
    all, so EVERY read fails. That is deliberately not the same as a readable
    process with an unreadable field, and a fixture that let a departed process
    keep answering `cmdline` would be quietly asserting something the kernel
    never does.
    """

    def __init__(self, comm=b"claude\n", argv0="claude", resume_sid="5f1c9a20-1111-2222-3333-444444444444",
                 seat="victim", pane_key="pk-victim", start="1000", gone=False):
        self.comm, self.argv0, self.resume_sid = comm, argv0, resume_sid
        self.seat, self.pane_key, self.start = seat, pane_key, start
        self.gone = gone


GONE = _Inc(gone=True)                  # the number is free; nothing answers


class _RecycledPid:
    """A pid whose number CHANGES HANDS at a named READ BOUNDARY.

    Two incarnations, one number. `hand_over_after` names the /proc file whose
    read is the LAST one the PREDECESSOR answers; every read after it is
    answered by the SUCCESSOR. That states the physical event — the kernel gave
    the number away at this point in helm's read order — rather than a call
    COUNT, which would encode how many reads a correct implementation happens
    to make and would then pass or fail for reasons unrelated to whether the
    row describes one process or two.

    Repeat reads of the boundary file are answered by the successor, which is
    the point: re-reading a file is only worth anything if the fixture lets the
    second read differ from the first.
    """

    STAT = (b"101 (%s) S 1 101 101 0 -1 4194304 900 0 0 0 11 22 0 0 20 0 "
            b"33 0 %s 123456 789 18446744073709551615\n")

    def __init__(self, pred, succ, hand_over_after="comm"):
        self.pred, self.succ, self.boundary = pred, succ, hand_over_after
        self.handed_over = False
        self.reads = []

    def _read(self, path):
        name = path.rsplit("/", 1)[-1]
        who = self.succ if self.handed_over else self.pred
        self.reads.append((name, "succ" if self.handed_over else "pred"))
        if name == self.boundary:
            self.handed_over = True     # the number changes hands HERE
        if who.gone:
            raise OSError(2, "No such file or directory")
        if name == "comm":
            if who.comm is None:
                raise OSError(2, "No such file")
            return who.comm
        if name == "stat":
            if who.start is None:
                raise OSError(3, "No such process")
            label = who.comm.strip() if who.comm else b"?"
            return self.STAT % (label, who.start.encode())
        if name == "cmdline":
            return b"%s\0--resume\0%s\0" % (who.argv0.encode(),
                                            who.resume_sid.encode())
        if name == "environ":
            return (b"HELM_CHAT_NAME=%s\0ORCA_PANE_KEY=%s\0"
                    % (who.seat.encode(), who.pane_key.encode()))
        raise OSError(2, "No such file")

    def _opener(self):
        import io
        real_open = open

        def opener(path, mode="r", *a, **kw):
            if str(path).startswith("/proc/"):
                return io.BytesIO(self._read(str(path)))
            return real_open(path, mode, *a, **kw)
        return opener

    def scan(self):
        """Run the real `claude_processes()` against this fake /proc."""
        with mock.patch("builtins.open", self._opener()), \
                mock.patch.object(orcaadopt.os, "getpid", return_value=101), \
                mock.patch.object(orcaadopt.glob, "glob",
                                  return_value=["/proc/101/cmdline"]):
            return orcaadopt.claude_processes()

    def address(self, ident, ad):
        """Run the real `authorized_handle()` against this fake /proc."""
        with mock.patch("builtins.open", self._opener()), \
                mock.patch.object(orcaadopt.os, "getpid", return_value=101), \
                mock.patch.object(orcaadopt.glob, "glob",
                                  return_value=["/proc/101/cmdline"]):
            return orcaadopt.authorized_handle([ident], ad)


# The successor in the repro: a shell, not claude, whose argv[0] nonetheless
# ends in "claude", holding the seat env it inherited from the seat's shell.
def _impostor(start="9999"):
    return _Inc(comm=b"bash\n", argv0="/tmp/bash-claude", start=start)


class CommOutsideTheWindowTest(unittest.TestCase):
    """WHAT a process IS belongs to the row, so it belongs under the stamps.

    The round-5 finding (dispatch ad2aed0f, reproduced at b98a7780):
    `comm` was read BEFORE `start_before` as a cheap WHAT-is-this pre-filter,
    on the reasoning that a filter is not payload. But a filter that says YES
    is deciding that a row exists, and it was deciding it about whoever held
    the number a moment EARLIER. Hand the pid over between the pre-filter and
    the window and every field the seqlock covers agrees with every other —
    stamps, argv, environ, all the successor's — while the one field that says
    "this is claude" describes a process that is gone.

    Measured before the fix, from the identical fixture below:
      procs      = [{'pid': 101, 'start': '9999', 'seat': 'victim',
                     'pane_key': 'pk-victim', 'resume_sid': '5f1c9a20-1111-2222-3333-444444444444'}]
      unreadable = []
      authorized_handle -> 'handle-of-pk-victim'
    A /tmp/bash-claude was reported LIVE and then SPENT AN ADDRESS.
    """

    def test_a_NON_CLAUDE_successor_produces_NO_row(self):
        """THE ADVERSARIAL MUST-HIT. Nothing inside the window disagrees with
        anything else inside the window — the torn-stamp guard is satisfied and
        cannot fire. Only re-reading comm under the stamps can catch this."""
        world = _RecycledPid(_Inc(), _impostor(), hand_over_after="comm")
        procs, unreadable = world.scan()
        self.assertEqual(
            procs, [],
            "A NON-CLAUDE PROCESS WAS REPORTED LIVE: the predecessor's comm "
            "welded to a successor's stamps, argv and inherited seat env — %s"
            % (procs,))
        self.assertEqual(unreadable, [101],
                         "a pid that passed the identity filter and then read "
                         "back as something else is UNKNOWN, not absent: it "
                         "must not vanish from both lists")

    def test_the_impostor_can_NEVER_become_an_ADDRESS(self):
        """The outcome, not the bookkeeping. The forged row's whole cost is
        that `pk-victim` — inherited, so it really does name the seat's pane —
        gets spent on a directive by a process that is not the seat's agent."""
        for stamp in ("1000", "9999"):
            world = _RecycledPid(_Inc(), _impostor(), hand_over_after="comm")
            ad = _PaneAd()
            handle, why = world.address(_ident(101, stamp), ad)
            self.assertIsNone(
                handle,
                "authorized at %s and a /tmp/bash-claude got an address: %s"
                % (stamp, handle))
            self.assertEqual(ad.sent, [])
            self.assertNotIn("pk-victim", why or "",
                             "the refused pane key must not resurface as an "
                             "address in the refusal text")

    def test_an_impostor_produces_NO_row_WHEREVER_the_pid_changes_hands(self):
        """NO GAP BETWEEN THE TWO GUARDS. Walk the handover across every read
        in the order and the answer must not move: before the window the comm
        recheck catches it, inside the window the stamps do, and there is no
        position in between where a non-claude process becomes a row. This is
        the statement the two guards make TOGETHER, and neither makes alone."""
        for boundary in ("comm", "stat", "cmdline", "environ"):
            world = _RecycledPid(_Inc(), _impostor(), hand_over_after=boundary)
            procs, unreadable = world.scan()
            self.assertEqual(procs, [],
                             "handover after %s produced a row for a process "
                             "that is not claude: %s" % (boundary, procs))
            self.assertEqual(unreadable, [101],
                             "handover after %s: the pid passed the identity "
                             "filter and was then incoherent, so it belongs in "
                             "the honest-UNKNOWN bucket" % boundary)

    def test_the_argv_check_is_NOT_a_substitute_for_comm(self):
        """NON-VACUITY. If the impostor's argv[0] did not end in "claude" the
        test above would pass for a reason that has nothing to do with the
        defect, and the guard could be deleted with everything still green.
        This asserts the fixture is actually adversarial: the argv rung lets
        this process straight through, and only the comm rung stops it."""
        succ = _impostor()
        self.assertTrue(succ.argv0.endswith("claude"),
                        "the impostor must survive the argv test, or the comm "
                        "test below proves nothing")
        argv = _RecycledPid(_Inc(), succ)._read("/proc/101/cmdline")
        self.assertTrue(argv.decode().split("\0")[0].endswith("claude"))
        self.assertNotEqual(succ.comm.strip(), b"claude")

    def test_a_CLAUDE_successor_still_produces_ITS_OWN_row(self):
        """THE CONTROL THAT MATTERS. The guard must key on what the window
        SEES, never on "the number changed hands" — that is unobservable, and a
        guard that refused every recycle would refuse the ordinary case where a
        pid is handed to a real claude before the window even opens. That row
        is entirely the successor's and entirely coherent, so it stands."""
        heir = _Inc(seat="heir", pane_key="pk-heir", resume_sid="7ae3b1c4-5555-6666-7777-888888888888",
                    start="9999")
        world = _RecycledPid(_Inc(), heir, hand_over_after="comm")
        procs, unreadable = world.scan()
        self.assertEqual(unreadable, [])
        self.assertEqual(procs, [{"pid": 101, "start": "9999", "seat": "heir",
                                  "pane_key": "pk-heir", "worktree_id": None,
                                  "resume_sid": "7ae3b1c4-5555-6666-7777-888888888888"}],
                         "a coherent observation of a REAL claude must survive "
                         "— refusing it would disarm the substrate")

    def test_a_STABLE_claude_still_produces_its_row(self):
        """The plain positive control: one process, no handover, one row. A
        comm recheck that always refused would pass every test above."""
        steady = _Inc()
        world = _RecycledPid(steady, steady, hand_over_after="comm")
        procs, unreadable = world.scan()
        self.assertEqual(unreadable, [])
        self.assertEqual(procs, [{"pid": 101, "start": "1000",
                                  "seat": "victim", "pane_key": "pk-victim",
                                  "worktree_id": None,
                                  "resume_sid": "5f1c9a20-1111-2222-3333-444444444444"}])

    def test_a_pid_that_VANISHES_inside_the_window_is_SKIPPED_not_UNKNOWN(self):
        """GONE and CONTRADICTED are different answers. "No process here" is
        complete — there is no row and nothing to be unsure about — so it takes
        the skip, exactly as the cmdline read already does. Folding it into
        `unreadable` would make every ordinary exit look like blindness."""
        world = _RecycledPid(_Inc(), GONE, hand_over_after="comm")
        procs, unreadable = world.scan()
        self.assertEqual(procs, [])
        self.assertEqual(unreadable, [],
                         "an exited pid is not a claude helm failed to read")

    def test_a_NON_CLAUDE_pid_still_costs_ZERO_stat_reads(self):
        """The performance invariant bought in review and this must not sell:
        the pre-filter may still REJECT early, so a host full of non-claude
        pids never opens the window at all."""
        world = _RecycledPid(_Inc(comm=b"bash\n"), _Inc(comm=b"bash\n"))
        self.assertEqual(world.scan(), ([], []))
        self.assertEqual([n for n, _ in world.reads], ["comm"],
                         "a rejected pid must cost exactly one small read — "
                         "%s" % (world.reads,))


class _BlindAt:
    """One claude candidate that fails ONE named /proc read — two ways.

    THE DISTINCTION THIS FIXTURE EXISTS TO HOLD APART, and it is the reason the
    errno is a parameter rather than a constant: `ENOENT`/`ESRCH` is the kernel
    saying THE PROCESS IS NOT THERE, and `EACCES`/`EPERM` is the kernel saying
    I WILL NOT LET YOU LOOK. Both arrive at the same `except OSError:` and they
    are OPPOSITE answers — one is a complete fact about the world, the other is
    a hole in helm's view of it. A census that skips on both makes an
    unreadable process indistinguishable from a dead one.

    AND THE TWO MODES ARE NOT ONE PARAMETER. `blind` fails exactly one read and
    lets every other one answer, because a live process helm may not read keeps
    answering the reads it is allowed to. `vanish` fails that read AND EVERY
    READ AFTER IT, because a departed process does not selectively answer —
    a fixture that let it keep serving `cmdline` would be quietly asserting
    something the kernel never does, and the inverse control would then pass
    for a reason unrelated to the errno.

    `nth` picks WHICH read of that filename it is (1-based): the candidate is
    read comm, stat, comm, cmdline, environ, stat, and the second comm read is
    a different question from the first.
    """

    STAT = (b"101 (claude) S 1 101 101 0 -1 4194304 900 0 0 0 11 22 0 0 20 0 "
            b"33 0 %s 123456 789 18446744073709551615\n")

    def __init__(self, where, err, nth=1, start="1000", mode="blind"):
        self.where, self.err, self.nth, self.start = where, err, nth, start
        self.mode, self.departed = mode, False
        self.counts = {}
        self.reads = []

    def _read(self, path):
        name = path.rsplit("/", 1)[-1]
        self.counts[name] = self.counts.get(name, 0) + 1
        self.reads.append(name)
        if name == self.where and self.counts[name] == self.nth:
            if self.mode == "vanish":
                self.departed = True
            raise OSError(self.err, os.strerror(self.err))
        if self.departed:
            raise OSError(self.err, os.strerror(self.err))
        if name == "comm":
            return b"claude\n"
        if name == "stat":
            return self.STAT % self.start.encode()
        if name == "cmdline":
            return b"claude\x00--resume\x005f1c9a20-1111-2222-3333-444444444444\x00"
        if name == "environ":
            return b"HELM_CHAT_NAME=victim\0ORCA_PANE_KEY=pk-victim\0"
        raise OSError(2, "No such file")

    def scan(self):
        import io
        real_open = open

        def opener(path, mode="r", *a, **kw):
            if str(path).startswith("/proc/"):
                return io.BytesIO(self._read(str(path)))
            return real_open(path, mode, *a, **kw)

        with mock.patch("builtins.open", opener), \
                mock.patch.object(orcaadopt.os, "getpid", return_value=101), \
                mock.patch.object(orcaadopt.glob, "glob",
                                  return_value=["/proc/101/cmdline"]):
            return orcaadopt.claude_processes()


class UnreadableIsNotGoneTest(unittest.TestCase):
    """`except OSError: continue` ANSWERS TWO QUESTIONS WITH ONE EXIT.

    The round-7 finding (dispatch 90845108, tip 4995691): "Inner
    comm/cmdline OSError also skip without proving process exit."

    The collector's job is to say, for every pid, one of three things: this is
    a coherent claude row, this pid is GONE, or this is a live claude helm
    COULD NOT READ. A bare `continue` says the second when it has only earned
    the third. Measured before the fix, with EACCES on the inner comm read:
    ``([], [])`` — the pid vanished from BOTH lists, so every consumer saw a
    host with no rival at all.

    The inverse control lives here too and is the harder half: a genuinely
    exited pid must STILL be skipped. Fix this carelessly — treat every OSError
    as blindness — and every ordinary process exit reports the whole census
    blind, which refuses every send on a busy host. The two cases look
    identical at the `except` and are told apart only by errno.
    """

    # EACCES is the shape /proc actually produces for a live process helm may
    # not read (another user's, or a privileged one). EPERM and EIO stand for
    # "some other failure": the rule is keyed on what PROVES exit, not on a
    # denylist of the errnos somebody thought of.
    BLIND = (13, 1, 5)
    GONE = (2, 3)

    def test_an_UNREADABLE_inner_comm_is_UNKNOWN_not_absent(self):
        """THE ADVERSARIAL MUST-HIT. The pre-filter said claude, so this pid IS
        a claude process — and then the read under the stamps failed for a
        reason that says nothing about whether it exited."""
        for err in self.BLIND:
            world = _BlindAt("comm", err, nth=2)
            procs, unreadable = world.scan()
            self.assertEqual(procs, [], "errno %d: %s" % (err, procs))
            self.assertEqual(
                unreadable, [101],
                "errno %d (%s) means helm COULD NOT LOOK, and the pid "
                "vanished from both lists — an unreadable process is now "
                "indistinguishable from a dead one"
                % (err, os.strerror(err)))

    def test_an_UNREADABLE_cmdline_is_UNKNOWN_not_absent(self):
        for err in self.BLIND:
            world = _BlindAt("cmdline", err)
            procs, unreadable = world.scan()
            self.assertEqual(procs, [], "errno %d: %s" % (err, procs))
            self.assertEqual(unreadable, [101],
                             "errno %d on cmdline skipped a live claude" % err)

    def test_an_UNREADABLE_pre_filter_comm_is_UNKNOWN_not_absent(self):
        """The read BEFORE the window is the same question. It may only
        REJECT — but a failed read is not a rejection, it is no answer, and a
        pid helm cannot classify must not be silently dropped from a census
        whose whole job is to say whether a rival exists."""
        for err in self.BLIND:
            world = _BlindAt("comm", err, nth=1)
            procs, unreadable = world.scan()
            self.assertEqual(procs, [], "errno %d: %s" % (err, procs))
            self.assertEqual(unreadable, [101],
                             "errno %d on the pre-filter dropped a pid helm "
                             "could not classify" % err)

    def test_a_GENUINELY_EXITED_pid_is_STILL_SKIPPED(self):
        """THE INVERSE CONTROL, and the one a careless fix breaks. ENOENT/ESRCH
        is the kernel answering completely: there is no process here, so there
        is no row and nothing to be unsure about. Folding these into
        `unreadable` would make every ordinary exit read as blindness and
        refuse every send on a busy host.

        Walked across EVERY position in the read order, because "gone" has to
        be a complete answer wherever the departure lands — the blind case is
        told from it by the errno alone, never by where it happened."""
        for where, nth in (("comm", 1), ("stat", 1), ("comm", 2),
                           ("cmdline", 1), ("environ", 1)):
            for err in self.GONE:
                world = _BlindAt(where, err, nth=nth, mode="vanish")
                procs, unreadable = world.scan()
                self.assertEqual(procs, [], "%s#%d/%d: %s"
                                 % (where, nth, err, procs))
                self.assertEqual(
                    unreadable, [],
                    "%s#%d errno %d: an EXITED pid is not a claude helm "
                    "failed to read — conflating them makes the census useless"
                    % (where, nth, err))

    def test_a_fully_readable_claude_still_produces_its_row(self):
        """The positive control: a fix that reported blindness on every read
        would pass every test above and address nothing, ever."""
        world = _BlindAt("nothing-fails", 13)
        procs, unreadable = world.scan()
        self.assertEqual(unreadable, [])
        self.assertEqual(procs, [{"pid": 101, "start": "1000", "seat": "victim",
                                  "pane_key": "pk-victim", "worktree_id": None,
                                  "resume_sid": "5f1c9a20-1111-2222-3333-444444444444"}])

    def test_a_WALK_THAT_NEVER_RAN_is_blind_not_an_empty_host(self):
        """THE ENUMERATION IS A READ TOO, and it is the one above every read
        the bracket covers. `glob` does not raise: an unmounted /proc, a
        hidepid mount and a chroot with no procfs all answer [], and ([], [])
        is the confident claim "there are no claude processes on this host" —
        the same fail-open, one level up from where it was first found.

        The seed is helm's OWN pid, because a walk that cannot see the process
        doing the walking did not happen. Its absence proves that without
        needing to know why, which is what makes it a floor rather than one
        more special case."""
        with mock.patch.object(orcaadopt.glob, "glob", return_value=[]):
            procs, unreadable = orcaadopt.claude_processes()
        self.assertEqual(procs, [])
        self.assertEqual(len(unreadable), 1,
                         "an empty /proc walk answered 'no claude processes' "
                         "with confidence: %r" % (unreadable,))
        self.assertIn(str(os.getpid()), unreadable[0])
        self.assertTrue(orcaadopt.cannot_look(unreadable, "x"),
                        "the one predicate must refuse on this too, or the "
                        "consumers never hear about it")

    def test_a_REAL_walk_of_the_REAL_host_is_NOT_blind(self):
        """The control, and it runs against this machine's actual /proc rather
        than a fixture — a seed that could never be hit would refuse every
        census on every host forever."""
        procs, unreadable = orcaadopt.claude_processes()
        self.assertEqual(
            [u for u in unreadable if not str(u).strip().isdigit()], [],
            "the live /proc walk reported itself blind: %r" % (unreadable,))
        self.assertIsInstance(procs, list)


class OneBracketTest(unittest.TestCase):
    """THERE ARE NO INDIVIDUAL /proc READS LEFT TO BRACKET.

    Rounds 3-7 were the same shape found five times, and each time at ONE MORE
    READ: the send-time comparison, the metadata read before the trailing
    stamp, `comm` read before `start_before`, then the OSError exits that
    skipped without proving exit. Handling reads one at a time cannot
    terminate, because the next read is always outside whatever set was
    enumerated.

    So the reads are not handled individually any more. Every /proc access in
    this module happens inside exactly TWO functions — `proc_start`, which
    reads the identity, and `_read_candidate`, which reads the whole candidate
    under one bracket and returns one of three verdicts. This test is what
    stops a sixth one being added: a new `open("/proc/...")` anywhere else in
    the module fails here before it can be reviewed as correct-in-isolation.
    """

    BRACKETS = ("proc_start", "_read_candidate")

    def test_every_proc_read_lives_inside_the_bracket(self):
        import ast
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "helm", "orcaadopt.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        stray = []

        def walk(node, outer):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, outer or child)
                    continue
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                        and child.func.id == "open":
                    stray.append((outer.name if outer else "<module>",
                                  child.lineno,
                                  ast.get_source_segment(src, child) or ""))
                walk(child, outer)

        walk(tree, None)
        outside = [s for s in stray if s[0] not in self.BRACKETS]
        self.assertEqual(
            outside, [],
            "a file read appeared OUTSIDE the bracket. Every field of a "
            "candidate must be read inside `_read_candidate`, under one pair "
            "of birth stamps, or it can describe a different process from the "
            "rest of the row — that is rounds 4, 5 and 6, three times. Note "
            "that a read outside `_read_candidate` also loses the GONE/BLIND "
            "errno split, which is the finding: %r" % (outside,))
        # NON-VACUITY. The collapse means the path is BUILT at four call sites
        # and OPENED in one place, so counting `open(` counts the readers, not
        # the reads — two is the whole point. A matcher that had stopped
        # matching would report zero and pass.
        self.assertEqual(
            sorted(s[0] for s in stray), sorted(self.BRACKETS),
            "the two readers moved. There must be exactly two — the identity "
            "read and the whole-candidate read — and nothing else in this "
            "module may touch the filesystem: %r" % (stray,))

    def test_the_verdict_is_THREE_valued_and_a_partial_row_is_unrepresentable(self):
        """The collapse's whole claim, asserted rather than described. There is
        no fourth answer, and — the round-7 half — no candidate can be a row
        AND unreadable at once. That dual membership was the ONE partial row
        this collector could emit (an unreadable environ used to yield a row
        with seat=None plus an entry in `unreadable`), and it is what every
        consumer downstream had to remember to cross-check by hand."""
        self.assertEqual({orcaadopt.ROW, orcaadopt.ABSENT, orcaadopt.BLIND},
                         {"ROW", "ABSENT", "BLIND"},
                         "ABSENT, not GONE: the verdict covers a pid that "
                         "EXITED and a pid that is simply not claude, and "
                         "naming it after only the first would invite a "
                         "fourth verdict for the second")
        world = _BlindAt("environ", 13)          # EACCES: a live, unreadable one
        procs, unreadable = world.scan()
        self.assertEqual(procs, [], "a partial row survived an unreadable "
                                    "environ: %s" % (procs,))
        self.assertEqual(unreadable, [101])
        for fixture in (_BlindAt("environ", 13), _BlindAt("comm", 13, nth=2),
                        _BlindAt("nothing-fails", 13)):
            rows, blind = fixture.scan()
            self.assertEqual(
                {p["pid"] for p in rows} & set(blind), set(),
                "a pid is BOTH a row and unreadable — the partial row is back")


class UnreadableRivalTest(unittest.TestCase):
    """AN UNREADABLE RIVAL POISONS THE ANSWER. It does not lose the vote.

    The round-7 finding (dispatch 90845108, tip 4995691): "Still
    fail-open: identity authorizes one readable candidate despite an unreadable
    rival; resolve maps ([],[pid]) to None; proxywatch maps it to off."

    Earlier findings made the collector HONEST — it now reports the pids it could not
    identify. This is the layer that reads that report. A consumer that filters
    `procs` and never looks at `unreadable` is asking "is there a READABLE
    rival?" and answering the different question "is there a rival?". The
    unreadable pid is a LIVE claude process; helm cannot see whose pane it is,
    and one of the things it could be is the very pane this decision is about.

    `seat_liveness` rung R3 has enforced exactly this since the file was
    written. These are the consumers that did not.
    """

    def test_the_named_ladder_REFUSES_beside_an_unreadable_rival(self):
        """THE MUST-HIT: a readable candidate + an unreadable rival must NOT
        produce an address."""
        mine = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        ident, why = orcaadopt.turn_restart_identity(
            "me", "sid-now", procs=[mine], unreadable=[303])
        self.assertIsNone(ident,
                          "AUTHORIZED AN ADDRESS while a live claude process "
                          "helm could not identify was on the host: %r" % (ident,))
        self.assertIn("303", why)

    def test_the_nameless_ladder_REFUSES_beside_an_unreadable_rival(self):
        ghost = _proc(202, pane_key="pk-ghost", resume_sid="sid-now")
        ident, why = orcaadopt._nameless_identity("sid-now", [ghost], [303])
        self.assertIsNone(ident, "nameless ladder authorized %r" % (ident,))
        self.assertIn("303", why)

    def test_the_send_itself_REFUSES_beside_an_unreadable_rival(self):
        """The outcome, not the bookkeeping — through the real injection path.
        The send-time census is the authoritative observation (rule 2), so a
        census that cannot see the whole host may not spend an address."""
        mine = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([mine], [303])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            mode, detail = orcaadopt.send_to_pane(
                "me", "/compact", expect_pids=[_ident(101)], adapter=ad)
        self.assertEqual(ad.sent, [],
                         "INJECTED while a live claude was unidentified: %s"
                         % (ad.sent,))
        self.assertEqual(mode, "manual", detail)

    def test_the_NAMELESS_send_REFUSES_beside_an_unreadable_rival(self):
        ghost = _proc(202, pane_key="pk-ghost", resume_sid="sid-now")
        ad = _PaneAd()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([ghost], [303])):
            mode, detail = orcaadopt.send_to_sid_pane(
                "sid-now", "/compact", expect_pids=[_ident(202)], adapter=ad)
        self.assertEqual(ad.sent, [], "%s" % (ad.sent,))
        self.assertEqual(mode, "manual", detail)

    def test_resolve_does_not_call_a_BLIND_host_an_unknown_seat(self):
        """`resolve()` returning None is a POSITIVE claim — "helm has never
        heard of this name" — and the caller prints it as a typo. With a live
        claude process helm could not identify, that claim is unearned: one of
        them may be this very seat."""
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [303])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("me", adapter=_PaneAd())
        self.assertIsNotNone(
            info,
            "([], [303]) answered None — 'I could not look' collapsed into "
            "'no such seat', which the CLI prints as a typo")
        self.assertEqual(info["state"], orcaadopt.UNKNOWN, info)
        self.assertIn("303", info["evidence"])

    def test_the_OPERATOR_SURFACE_says_the_census_was_incomplete(self):
        """A field nobody prints is a field nobody has. `seat where` publishes
        `pids` and a `handle`, and beside an unidentified claude those are a
        partial census that LOOKS complete — the same overclaim the `unowned`
        note under `seat panes` exists to prevent."""
        from helm import seat as seatmod
        mine = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([mine], [303])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"me": {"session": "sid-now"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}), \
                mock.patch.object(harness, "detect", return_value=_PaneAd()):
            info = orcaadopt.resolve("me")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                seatmod._where_adopted("me", [], "unknown seat 'me'")
        self.assertIn("303", info.get("unidentified") or "",
                      "resolve() dropped the blind fact on the floor")
        self.assertIn("INCOMPLETE", buf.getvalue(),
                      "the operator sees pids and a handle and is never told "
                      "the census behind them had a hole in it")
        self.assertIn("303", buf.getvalue())

    def test_a_GENUINE_typo_on_a_READABLE_host_still_answers_None(self):
        """THE CONTROL THAT MATTERS, and the one the fix above could destroy: a
        typo must still look like a typo. Nothing unreadable, nothing in the
        roster, no process — that is a proven absence and it keeps its message."""
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            self.assertIsNone(orcaadopt.resolve("typoo", adapter=_PaneAd()))

    def test_resolve_and_liveness_agree_on_session_joined_process(self):
        """#138: resolve() and seat_liveness() must return LIVE when a process is
        joined by session, never declaring DEAD or state=None while naming pids."""
        nameless = _proc(8888, seat=None, pane_key="pk-8888", resume_sid="sid-joined")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"adopted-seat": {"session": "sid-joined"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("adopted-seat")
            self.assertIsNotNone(info)
            self.assertEqual(info["state"], orcaadopt.LIVE)
            self.assertEqual(info["pids"], [8888])
            self.assertIn("liveness", info)
            self.assertEqual(info["liveness"]["state"], orcaadopt.LIVE)

    def test_a_readable_host_still_AUTHORIZES_the_ordinary_case(self):
        """The positive control for the whole class. A veto that fired
        unconditionally would pass every test above and strand the fleet."""
        mine = _proc(101, seat="me", pane_key="pk-mine", resume_sid="sid-now")
        ident, why = orcaadopt.turn_restart_identity(
            "me", "sid-now", procs=[mine], unreadable=[])
        self.assertEqual(int(ident), 101, why)
        ghost = _proc(202, pane_key="pk-ghost", resume_sid="sid-now")
        ident, why = orcaadopt._nameless_identity("sid-now", [ghost], [])
        self.assertEqual(int(ident), 202, why)


# Every function that PRODUCES a process census — a value whose second half is
# "and here is what I could not see" — keyed by the file that DEFINES it. A
# consumer of any of them is a consumer of the census, however many hops from
# /proc it sits: `autocompact` reads `proxywatch._live_seats`, never
# `claude_processes`, and the round-7 signature change broke it silently
# because the first draft of this census only knew the bottom one.
#
# THE DEFINING FILE IS PART OF THE KEY because `seats.py` has its OWN
# `_live_seats` — the chat roster's presence beats, nothing to do with /proc.
# A bare call is attributed to its own module; a dotted call is matched on the
# attribute name alone and may over-match, which costs one declaration line.
# Under-matching is what makes a census report zero and get believed.
_CENSUS_PRODUCERS = {"claude_processes": "orcaadopt.py",
                     "_live_seats": "proxywatch.py"}


def _census_consumers():
    """[(module, function, source)] for every function in helm/ that CONSUMES a
    process census — by calling a producer, or by taking an `unreadable`
    parameter (which is the census arriving pre-taken).

    The census-of-consumers, built the same way `_send_sites` is built and for
    the same reason. Round 7 was four separate consumers that each dropped the
    same bucket, found one at a time across four review rounds; a per-case
    handler can never be completed by adding cases, because the next case is
    always outside the set somebody enumerated. This enumerates the set from
    the SOURCE instead, so a fifth consumer cannot be written silently.
    """
    import ast
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "helm")
    out = set()
    for name, path in _helm_sources(root):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)

        def walk(node, outer):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if "unreadable" in [a.arg for a in child.args.args] \
                            and outer is None:
                        out.add((name, child.name,
                                 ast.get_source_segment(src, child) or ""))
                    walk(child, outer or child)
                    continue
                if isinstance(child, ast.Call):
                    fn = child.func
                    if isinstance(fn, ast.Attribute):
                        called, home = fn.attr, name
                    elif isinstance(fn, ast.Name):
                        called, home = fn.id, name
                    else:
                        called, home = None, None
                    if _CENSUS_PRODUCERS.get(called) is not None \
                            and outer is not None and outer.name != called \
                            and (isinstance(fn, ast.Attribute)
                                 or _CENSUS_PRODUCERS[called] == home):
                        out.add((name, outer.name,
                                 ast.get_source_segment(src, outer) or ""))
                walk(child, outer)

        walk(tree, None)
    return sorted(out)


# Every census consumer in helm/, WHAT IT MUST NEVER SAY WHILE BLIND, and a
# probe that runs it both ways. Kinds are kept as documentation of intent:
#
#   gate      it refuses / answers UNKNOWN rather than deciding.
#   forward   it does not decide; it hands both halves to something that does.
#   report    its output is an inventory that names the unidentified pids.
#
# THE RULE THIS TABLE USED TO ENFORCE WAS THE WRONG RULE. Rounds 4 through 8
# all asked "does blindness dominate every CONFIDENT return", and checked it by
# looking for the substring `cannot_look(` in the function's source. Both halves
# were wrong at once.
#
# The check was wrong because a token appearing anywhere in a function proves
# nothing about which returns it guards — round 8, exactly.
#
# The RULE was wrong because "confident" is not the dangerous property.
# `seat_liveness` returns LIVE before any gate and that is SOUND: LIVE and
# UNKNOWN have the same effect on every caller, both refuse the resume.
# `proxywatch` sets pane_live=True on a readable hit while blind and that is
# SOUND too: a process helm READ named that seat, and blindness about some
# other pid does not weaken an identification helm actually made. Measured,
# both of them, before this table was rewritten.
#
# The real rule is that no consumer may emit ITS OWN UNSAFE VALUE while blind,
# and which value is unsafe is a per-consumer fact no source-text check can
# know. So each entry names it, and each is checked by a PAIR OF REAL RUNS:
#
#   sighted -> the probe MUST produce the unsafe value. Without this the blind
#              assertion is vacuous — a probe that cannot reach the unsafe
#              value at all passes for the wrong reason, which is how a census
#              reports zero and gets believed.
#   blind   -> it MUST NOT.
#
# A consumer with no working pair FAILS, so a fifteenth consumer cannot be
# written silently AND a probe cannot rot into a vacuous pass.

_BLIND_PID = 999999


def _census(blind, procs=()):
    """What a consumer sees. Blind = one live claude helm could not identify."""
    return (list(procs), [_BLIND_PID] if blind else [])


class _CensusAd(harness._CLIAdapter):
    """An adapter that resolves every pane, so nothing REFUSES for an unrelated
    reason and hides the property under test. Inherits the REAL `submit`."""

    name = "orca"

    def __init__(self):
        self.sent = []
        self.typed = {}

    def resolve_pane(self, key):
        return {"handle": "handle-of-%s" % key}

    def read(self, handle, limit=3000, timeout=60):
        text = self.typed.get(handle)
        if text is not None:
            return ADVANCED_PANE.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return ADVANCED_PANE

    def send(self, handle, text, enter=True):
        if enter:
            self.typed.pop(handle, None)
            return                     # the bare-Enter leg; see _PaneAd
        self.typed[handle] = text
        self.sent.append((handle, text))
        return True

    def panes(self):
        return [{"pane_key": "pk-1", "handle": "handle-of-pk-1"}], None


def _p_cannot_look(blind):
    return "NO-REFUSAL" if orcaadopt.cannot_look(
        _census(blind)[1], "a claim") is None else "REFUSED"


def _p_seat_liveness(blind):
    with mock.patch.object(orcaadopt, "roster_sessions",
                           return_value=([], False)):
        state, _why = orcaadopt.seat_liveness("nobody-holds-this",
                                              *_census(blind))
    return state


def _p_turn_restart_identity(blind):
    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    with mock.patch.object(orcaadopt, "roster_sessions",
                           return_value=([], False)):
        ident, _why = orcaadopt.turn_restart_identity(
            "s1", "sid-1", *_census(blind, procs))
    return "ADDRESS" if ident else "REFUSED"


def _p_nameless_identity(blind):
    procs = [_proc(4242, resume_sid="sid-1", pane_key="pk-1")]
    ident, _why = orcaadopt._nameless_identity("sid-1", *_census(blind, procs))
    return "ADDRESS" if ident else "REFUSED"


def _p_pane_rows(blind):
    """THE SELF-CENSUS PATH, the only one where blindness can be lost. A caller
    that SUPPLIES procs/unreadable holds it and owns what to do with it
    (seat._panes prints the pids, seat_identity gates on them). A caller that
    supplies nothing has no other channel."""
    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        _rows, note = orcaadopt.pane_rows(_CensusAd())
    return "SILENT" if not note else "NAMED"


def _p_resolve(blind):
    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)), \
            mock.patch.object(orcaadopt, "roster_sessions",
                              return_value=([], False)):
        got = orcaadopt.resolve("not-a-seat-here", adapter=_CensusAd())
    return "NONE" if got is None else str(got.get("state") or "ROW")


def _p_authorized_handle(blind):
    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        handle, _proof = orcaadopt.authorized_handle(
            {orcaadopt.ProcIdent(4242, "1000")}, _CensusAd())
    return "HANDLE" if handle else "REFUSED"


def _p_send_to_sid_pane(blind):
    procs = [_proc(4242, resume_sid="sid-1", pane_key="pk-1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        mode, _detail = orcaadopt.send_to_sid_pane("sid-1", "hello",
                                                   adapter=_CensusAd())
    return str(mode)


def _p_live_seats(blind):
    from helm import proxywatch
    procs = [_proc(4242, seat="s1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        _names, flag, _per = proxywatch._live_seats()
    return "DROPPED" if flag is None else "CARRIED"


def _p_health(blind):
    from helm import proxywatch
    procs = [_proc(4242, seat="s1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        rep = proxywatch.health(seats=["nobody-holds-this"],
                                include_upstream=False)
    row = rep["seats"][0]
    return "OFF" if row.get("pane_live") is False else str(row.get("pane_live"))


def _p_live_seat_names(blind):
    from helm import autocompact
    procs = [_proc(4242, seat="s1")]
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)):
        got = autocompact._live_seat_names()
    return "SET" if got is not None else "UNKNOWN"


def _p_seat_panes(blind):
    """The adapter is PINNED. Without it `harness.detect()` finds no
    metaharness under the test home, `_panes` prints "no pane inventory" and
    returns before the census matters — and BOTH runs answer SILENT, so the
    sighted control passes for a reason that has nothing to do with the rule.
    Exactly the vacuity this pair exists to expose; it exposed it here first."""
    from helm import seat as seatmod
    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    out = io.StringIO()
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)), \
            mock.patch.object(harness, "detect", return_value=_CensusAd()), \
            contextlib.redirect_stdout(out):
        seatmod._panes([])
    return "SILENT" if str(_BLIND_PID) not in out.getvalue() else "NAMED"


def _p_evidence(blind):
    """An alias declared onto a target helm has no evidence for. Sighted that is
    a real misconfiguration; blind it may be the seat helm could not read. THE
    UNSAFE VALUE IS THE CONFIDENT DIAGNOSIS, not the refusal — both answers
    refuse the alias, and the difference is entirely whose fault the human is
    told it is."""
    from helm import seat_identity
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind)), \
            mock.patch.object(seat_identity, "_canonical_sources",
                              return_value=({}, {}, set(), {})):
        _canon, _ev, err = seat_identity._evidence(
            "nick", raw="owner:nick=nobody-holds-this")
    if not err:
        return "CLEAN"
    return "BLAMED-CONFIG" if "is not an exact" in err else "COULD-NOT-CHECK"


def _p_native_registered(blind):
    from helm import resumeturn, seats as seatsmod
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind)), \
            mock.patch.object(seatsmod, "roster_checked",
                              return_value=({}, False)):
        _reason, _pids, recognized = resumeturn._native_registered(
            "nobody-holds-this", "sid-1")
    return "TYPO-VERDICT" if not recognized else "SUPPRESSED"


def _p_recovery_owner(blind):
    from helm import resumeturn, seats as seatsmod, seats_work_offer
    with mock.patch.object(seats_work_offer, "_live_seats",
                           return_value={"peer"}), \
            mock.patch.object(seatsmod, "owner_names", return_value=set()), \
            mock.patch.object(seatsmod, "roster_checked",
                              return_value=({"peer": {}}, blind)), \
            mock.patch.object(seatsmod, "beacon_procs",
                              return_value=([4242], None)):
        owner, _detail = resumeturn._recovery_owner("subject")
    return "ASSIGNED" if owner else "UNKNOWN"


def _p_retitle(blind):
    """`helm seat retitle` supplies its OWN census to `pane_rows`, which makes
    the blindness this verb's to report — pane_rows' stated law. A blind
    census can only SUBTRACT from the named set (an unreadable process
    contributes no handle-to-seat edge), so it costs a title and never writes
    a wrong one; the unsafe answer is printing the table as though the
    inventory were complete. THE ADAPTER IS PINNED for the same reason
    `_p_seat_panes` pins one: without it the verb exits on "no orca
    metaharness" before the census matters and BOTH runs answer SILENT."""
    from helm import orcatitle

    class _TitleAd(_CensusAd):
        def panes(self):
            return ([{"handle": "handle-of-pk-1", "title": "", "worktree":
                      "/w/lane", "pty_id": "pty-1", "status": "connected",
                      "writable": True, "orphaned": False}], None)

        def rename(self, handle, title=None):
            return title

    procs = [_proc(4242, seat="s1", pane_key="pk-1")]
    out = io.StringIO()
    with mock.patch.object(orcaadopt, "claude_processes",
                           return_value=_census(blind, procs)), \
            mock.patch.object(harness, "detect", return_value=_TitleAd()), \
            contextlib.redirect_stdout(out):
        orcatitle.cmd_retitle([])
    return "SILENT" if str(_BLIND_PID) not in out.getvalue() else "NAMED"


_CENSUS_CONSUMERS = {
    ("orcaadopt.py", "cannot_look"): (
        "report", "NO-REFUSAL", _p_cannot_look,
        "THE RULE ITSELF — the one implementation every gate below calls. Its "
        "unsafe answer is making no refusal at all while blind"),
    ("orcaadopt.py", "seat_liveness"): (
        "gate", "DEAD", _p_seat_liveness,
        "DEAD means SAFE TO RESUME. Emitting it while blind resumes a seat "
        "something may already hold. LIVE before the gate is fine: LIVE and "
        "UNKNOWN both refuse"),
    ("orcaadopt.py", "pane_rows"): (
        "forward", "SILENT", _p_pane_rows,
        "on the SELF-census path the caller has no other channel, so returning "
        "labels with no note presents UNOWNED as 'no identity found' when helm "
        "could not look"),
    ("orcaadopt.py", "resolve"): (
        "gate", "NONE", _p_resolve,
        "None is what the CLI prints as an unknown-seat typo — a confident "
        "accusation about the operator's spelling"),
    ("orcaadopt.py", "turn_restart_identity"): (
        "gate", "ADDRESS", _p_turn_restart_identity,
        "an ident is an ADDRESS a directive gets sent to; a blind census may "
        "not produce one"),
    ("orcaadopt.py", "_nameless_identity"): (
        "gate", "ADDRESS", _p_nameless_identity,
        "same, keyed on the session's own sid"),
    ("orcaadopt.py", "authorized_handle"): (
        "gate", "HANDLE", _p_authorized_handle,
        "the send-time census: a handle IS the authorization to inject text "
        "into somebody's pane"),
    ("orcaadopt.py", "send_to_sid_pane"): (
        "forward", "resumed", _p_send_to_sid_pane,
        "`resumed` means the text LANDED in a pane; while blind it must fall "
        "back to `manual` rather than deliver to a maybe-stranger"),
    ("proxywatch.py", "_live_seats"): (
        "forward", "DROPPED", _p_live_seats,
        "dropping the blind flag makes an absent seat read as `off`, which "
        "this module's vocabulary defines as DELIBERATE"),
    ("proxywatch.py", "health"): (
        "forward", "OFF", _p_health,
        "pane_live=False becomes `off`; three-valued is what lets turn_state "
        "answer hung-unknown. True on a READABLE hit is sound — positive "
        "evidence is not weakened by blindness about some other pid"),
    ("autocompact.py", "_live_seat_names"): (
        "forward", "SET", _p_live_seat_names,
        "a set while blind is 'these are the live seats'; None is UNKNOWN, "
        "which its rungs already read correctly"),
    ("seat.py", "_panes"): (
        "report", "SILENT", _p_seat_panes,
        "an inventory that does not print the unidentified pids lets every "
        "UNOWNED row overclaim"),
    ("seat_identity.py", "_evidence"): (
        "report", "BLAMED-CONFIG", _p_evidence,
        "'target is not an exact roster, spawn, or live seat' is an ACCUSATION "
        "about the operator's config; while blind the target may be exactly "
        "the seat helm could not read"),
    ("resumeturn.py", "_native_registered"): (
        "forward", "TYPO-VERDICT", _p_native_registered,
        "recognized=False prints the proxy-family verdict, which reads as "
        "'you typed a seat that does not exist'"),
    ("orcatitle.py", "cmd_retitle"): (
        "report", "SILENT", _p_retitle,
        "a title table that does not name the unidentified pids presents its "
        "own coverage as complete — every pane helm could not name reads as "
        "'not a seat' rather than 'helm could not look'"),
    ("resumeturn.py", "_recovery_owner"): (
        "gate", "ASSIGNED", _p_recovery_owner,
        "an assignee is authority to DM and mark the durable recovery task in "
        "progress; an unreadable checked roster must leave the task unowned "
        "rather than guess a canonical non-owner identity"),
}


class ALaunchTimeRecordIsNotACurrentIdentityTest(unittest.TestCase):
    """`_session_joined`'s declared-name rung, against a seat RENAMED WHILE
    LIVE (task/2739).

    /proc/<pid>/environ is written once at exec and the kernel never rewrites
    it, so HELM_CHAT_NAME says what a process was LAUNCHED as for as long as
    it lives. A roster key is re-pointed by `helm chat seat rename` under the
    running process. "environ says X, the roster says Y" is therefore EQUALLY
    the signature of a rename that worked perfectly and of a process that
    belongs to somebody else, so the rung may call it neither "contradictory
    evidence" nor a send that would "type into another seat's session".

    Every arm here pairs with the arm beside it: the ADMIT arms would pass
    against a rung that admitted everything, and the REFUSE arms would pass
    against one that refused everything. Neither survives both.
    """

    SEAT, OLD, SID = "seat-b", "seat-a", "sid-current"

    def _rows(self, old=None, until=None, key=None):
        """A roster whose `key` row carries `old` as a rename alias."""
        row = {"session": self.SID}
        if old is not None:
            row[seats_common.RENAME_ALIAS_FIELD] = {
                "old": old, "at": "2026-09-17T00:00:00Z",
                "until": pk.epoch_ts(time.time() + 3600
                                     if until is None else until),
                "prior": []}
        return {key or self.SEAT: row}

    def _join(self, rows, declared=OLD, pid=3200319):
        return orcaadopt._session_joined(
            self.SEAT, [_proc(pid, seat=declared, resume_sid=self.SID)],
            self.SID, [], rows=rows)

    def test_the_seats_PRE_RENAME_spelling_is_the_seat(self):
        """THE CURE. The roster itself records that the old spelling IS this
        seat; the rung now asks it. The no-alias control below is on the SAME
        fixture and the SAME pid, so this cannot be a rung that admits
        everything. (The live case that filed the row is in
        `orcaadopt._declared_verdict`, which names the seats and the pid.)"""
        hits, why = self._join(self._rows(old=self.OLD))
        self.assertIsNone(why)
        self.assertEqual([p["pid"] for p in hits], [3200319])
        hits, why = self._join(self._rows())          # CONTROL: no alias
        self.assertEqual(hits, [])
        self.assertTrue(why)

    def test_an_EXPIRED_alias_window_is_not_an_identity(self):
        """A WINDOW IS THE FIELD'S LIFECYCLE. The rename record outlives the
        window it grants, so reading the record instead of `live_alias` would
        admit a name the roster stopped honouring. The live-window control is
        the same row with the same `old`."""
        hits, why = self._join(self._rows(old=self.OLD, until=time.time() - 60))
        self.assertEqual(hits, [])
        self.assertIn("no live rename alias", why)
        hits, _why = self._join(self._rows(old=self.OLD))   # CONTROL: in window
        self.assertEqual([p["pid"] for p in hits], [3200319])

    def test_a_name_that_is_ITS_OWN_ROSTER_ROW_still_refuses(self):
        """THE TWO-SEATS CASE THIS RUNG EXISTS FOR, and the reason admitting an
        alias does not loosen it: `live_alias`'s own law is that an exact
        roster key is never an alias. Re-admit the old spelling as a row of
        its own and the identical alias record stops resolving."""
        rows = self._rows(old=self.OLD)
        rows[self.OLD] = {"session": "sid-other"}     # re-admitted, live again
        hits, why = self._join(rows)
        self.assertEqual(hits, [])
        self.assertTrue(why)
        del rows[self.OLD]                            # CONTROL: same alias row
        self.assertEqual([p["pid"] for p in self._join(rows)[0]], [3200319])

    def test_the_refusal_says_what_environ_IS_and_asserts_no_staleness(self):
        """WHAT THE MESSAGE MAY CLAIM. The old text asserted the send would
        "type into another seat's session" — a claim about a session nobody
        measured — and called a launch-time record "contradictory evidence".
        The admit arm above is this arm's control: the rung does not simply
        always refuse."""
        _hits, why = self._join(self._rows())
        self.assertIn("written at exec", why)
        self.assertIn("no live rename alias", why)
        for forbidden in ("contradictory evidence", "would type into",
                          "stale process"):
            self.assertNotIn(forbidden, why,
                             "the refusal implies something it never measured")
        self.assertIn(self.SEAT, why)
        self.assertIn(self.OLD, why)

    def test_an_UNREADABLE_roster_refuses_ABOUT_THE_ROSTER(self):
        """A RUNG MUST SAY WHICH OF ITS INPUTS FAILED. With no roster there is
        no alias record to consult, and `seats_common.roster()` answers {} for
        a file nobody could open — so resolving against it would produce the
        no-alias refusal, true in its verdict and WRONG about what decided it.
        The readable-roster control is the arm above."""
        with mock.patch("helm.seats.roster_checked", return_value=({}, True)):
            _hits, why = orcaadopt._session_joined(
                self.SEAT, [_proc(7, seat=self.OLD, resume_sid=self.SID)],
                self.SID, [])
        self.assertIn("roster", why)
        self.assertIn("could not be read", why)
        self.assertNotIn("no live rename alias", why,
                         "an unread roster was reported as a measured absence")

    def test_a_HOSTILE_declared_name_is_laundered_into_the_refusal(self):
        """The declared name comes out of a FOREIGN process's environ and lands
        in operator prose, so it goes through the display launder like any
        other emitted name. The legit-name assertion is the unconditional
        positive control ON THE SAME OBSERVABLE: this refusal DOES carry the
        declared name, so the absences below are laundering rather than a
        message that never quotes it."""
        _hits, why = self._join(self._rows(), declared=self.OLD + "\u202e\u0007")
        self.assertIn(self.OLD, why)
        self.assertNotIn("\u202e", why)
        self.assertNotIn("\u0007", why)

    def test_the_rung_costs_NO_roster_read_on_the_common_path(self):
        """A NAMELESS process is the normal shape — 12 of the 24 live claude
        processes on this host carry no HELM_CHAT_NAME — and it must not pay
        for a rung it never reaches. The declaring control proves the read
        happens at all, so this is not a probe that can never see one."""
        with mock.patch("helm.seats.roster_checked",
                        return_value=({}, False)) as r:
            orcaadopt._session_joined(
                self.SEAT, [_proc(9, seat=None, resume_sid=self.SID)],
                self.SID, [])
            self.assertEqual(r.call_count, 0)
            orcaadopt._session_joined(          # CONTROL: declaring pid reads
                self.SEAT, [_proc(9, seat=self.OLD, resume_sid=self.SID)],
                self.SID, [])
            self.assertEqual(r.call_count, 1)



class CensusConsumerCensusTest(unittest.TestCase):
    """EVERY consumer of the process census is RUN both ways — enforced.

    Rounds 4 through 8 each found one more place the same shape hid, and the
    guard built to end that sequence checked SPELLING, so it passed
    every consumer whether or not the rule held. This is the test that ends it:
    not by handling a ninth case, and not by reading source, but by running
    each consumer under a blind census and under a sighted one and requiring
    the pair to differ in the one direction that matters.
    """

    def test_every_census_consumer_declares_how_it_handles_BLINDNESS(self):
        seen = {(mod, fn) for mod, fn, _src in _census_consumers()}
        undeclared = sorted(seen - set(_CENSUS_CONSUMERS))
        self.assertEqual(
            undeclared, [],
            "a NEW consumer of the process census appeared. Declare it in "
            "_CENSUS_CONSUMERS with its UNSAFE value and a probe that runs it "
            "both ways — a consumer that reads `procs` and drops `unreadable` "
            "is asking 'is there a READABLE rival?' and answering 'is there a "
            "rival?': %r" % (undeclared,))
        stale = sorted(set(_CENSUS_CONSUMERS) - seen)
        self.assertEqual(stale, [], "these census consumers are gone; drop "
                         "them from _CENSUS_CONSUMERS: %r" % (stale,))
        for key, (kind, unsafe, probe, why) in _CENSUS_CONSUMERS.items():
            self.assertIn(kind, ("gate", "forward", "report"), key)
            self.assertTrue(callable(probe), key)
            self.assertTrue(str(unsafe).strip(), key)
            self.assertGreater(len(why), 20, key)

    def test_the_SIGHTED_run_actually_reaches_the_unsafe_value(self):
        """THE CONTROL, and the half eight rounds never had. A probe that
        cannot produce the unsafe value even when the census can see proves
        nothing when it fails to produce it while blind — it passes for the
        wrong reason, which is how a census reports zero and gets believed."""
        for (mod, fn), (_kind, unsafe, probe, _why) in _CENSUS_CONSUMERS.items():
            self.assertEqual(
                probe(False), unsafe,
                "%s:%s — the SIGHTED probe does not reach %r, so the blind "
                "assertion below is vacuous. Fix the probe (not the code) "
                "until a seeing census produces the unsafe value."
                % (mod, fn, unsafe))

    def test_no_consumer_emits_its_UNSAFE_value_while_BLIND(self):
        """THE RULE. A pid in `unreadable` is a LIVE claude process helm could
        not identify, so no consumer may answer as though it had looked."""
        for (mod, fn), (kind, unsafe, probe, why) in _CENSUS_CONSUMERS.items():
            self.assertNotEqual(
                probe(True), unsafe,
                "%s:%s emitted %r while the census was BLIND — %s (declared "
                "%s)" % (mod, fn, unsafe, why, kind))

    def test_the_consumer_census_REACHES_A_NESTED_PACKAGE(self):
        """NON-VACUITY, the same probe `_send_sites` earns. A census that
        cannot see a new consumer reports zero and gets believed."""
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm")
        pkg = os.path.join(root, "work")
        self.assertTrue(os.path.isdir(pkg))
        probe = os.path.join(pkg, "_census_consumer_probe.py")

        def clean():
            if os.path.exists(probe):
                os.unlink(probe)

        self.addCleanup(clean)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("from .. import orcaadopt\n\n\n"
                     "def i_drop_the_bucket():\n"
                     "    procs, _unreadable = orcaadopt.claude_processes()\n"
                     "    return [p for p in procs if p.get('seat')]\n")
        planted = [(m, f) for m, f, _s in _census_consumers()]
        self.assertIn(("work/_census_consumer_probe.py", "i_drop_the_bucket"),
                      planted,
                      "the consumer census cannot SEE a nested package")
        with self.assertRaises(AssertionError):
            self.test_every_census_consumer_declares_how_it_handles_BLINDNESS()
        clean()
        self.test_every_census_consumer_declares_how_it_handles_BLINDNESS()


class ResumeTokenShapeTest(unittest.TestCase):
    """`--resume` takes a session id OR a human TITLE, and rung 2 compared
    whatever it got against a session id with `!=`.

    MEASURED. The integrator compacted and rung 2 posted to the
    room: "every addressable process for the seat holds a different
    session (pid 407141/helm coo…) — the pane that compacted (56a628d4…) is
    gone". pid 407141 WAS the seat's own live pane, mid-turn; its argv is
    `--resume "helm coordinator 7-22"` and `helm coo` is the first eight
    characters of that title. A title is never equal to a session id, so the
    rung answered "different session" for a question the data cannot address.

    The alert names pane input as the fallback, so acting on it means typing
    into a pane that is working.
    """

    SID = "56a628d4-4a18-45b5-9b03-2741c4c1d265"
    OTHER = "f0e1d2c3-b4a5-4968-8778-695a4b3c2d1e"

    def _ladder(self, procs, session=None):
        with mock.patch("helm.seats.roster_checked",
                        return_value=({"me": {"session": session or self.SID}},
                                      False)):
            return orcaadopt.turn_restart_identity(
                "me", session or self.SID, procs=procs, unreadable=[])

    def test_a_TITLE_is_not_a_different_session(self):
        """THE regression. The seat's own pane, resumed by name, must not be
        reported gone."""
        pane = _proc(407141, seat="me", pane_key="pk", start="1",
                     resume_sid="helm coordinator 7-22")
        ident, why = self._ladder([pane])
        self.assertIsNotNone(
            ident, "the seat's own live pane was declared gone: %s" % why)
        self.assertEqual(int(ident), 407141)

    def test_a_WELL_FORMED_different_session_IS_still_a_contradiction(self):
        """NEGATIVE CONTROL, and the one that matters most here: the cure for a
        too-narrow predicate is a wider one, and a predicate that got too wide
        would hand rung 3 candidates it must not have — rung 3's own comment
        records a handoff injected into a pane running a DIFFERENT live
        session. So a real session id that differs must still refuse."""
        other = _proc(999, seat="me", pane_key="pk", start="1",
                      resume_sid=self.OTHER)
        ident, why = self._ladder([other])
        self.assertIsNone(ident, "a genuinely different session was addressed")
        self.assertIn("is gone", why)

    def test_the_refusal_still_fires_when_EVERY_pane_holds_a_real_other_sid(self):
        a = _proc(11, seat="me", pane_key="pk-a", start="1",
                  resume_sid=self.OTHER)
        b = _proc(12, seat="me", pane_key="pk-b", start="1",
                  resume_sid="a1b2c3d4-1111-4222-8333-444455556666")
        ident, why = self._ladder([a, b])
        self.assertIsNone(ident)
        self.assertIn("11", why)
        self.assertIn("12", why)

    def test_an_exact_match_still_wins_outright(self):
        pane = _proc(77, seat="me", pane_key="pk", start="1",
                     resume_sid=self.SID)
        ident, _why = self._ladder([pane])
        self.assertEqual(int(ident), 77)


class SessionShapedTest(unittest.TestCase):
    def test_a_real_session_id_is_shaped(self):
        self.assertTrue(
            orcaadopt.session_shaped("56a628d4-4a18-45b5-9b03-2741c4c1d265"))

    def test_uppercase_and_surrounding_space_are_tolerated(self):
        """DELIBERATE DIVERGENCE from session._SID_RE, which is strict.

        Pinning the PURPOSE, not the accident. `_SID_RE` is the MINT shape and
        strict is right there; this is the PARSE shape, where a shaped token is
        CONTRADICTION evidence and an unshaped one is NO evidence. A strict
        reader meeting a real uppercase id would call it a title, pass rung 2,
        and let a handoff reach a pane holding a DIFFERENT session — silent, and
        the incident class this rung exists for. Lenient meeting an uppercase
        TITLE merely refuses: loud and retryable. Wrong-refuse is the safe
        failure here, so the tolerance is the feature.
        """
        self.assertTrue(
            orcaadopt.session_shaped("  56A628D4-4A18-45B5-9B03-2741C4C1D265 "))

    def test_the_MINT_shape_stays_strict_and_that_is_not_a_bug(self):
        """The producer/consumer pair, asserted so the divergence cannot be
        'tidied' into agreement by a later reader who sees only one side."""
        from helm import session
        upper = "56A628D4-4A18-45B5-9B03-2741C4C1D265"
        self.assertIsNone(session._SID_RE.fullmatch(upper),
                          "the MINT shape must stay strict — a helm that mints "
                          "an uppercase id is a bug to catch, not a form to bless")
        self.assertTrue(orcaadopt.session_shaped(upper),
                        "the PARSE shape must accept it, because reading a real "
                        "id as a TITLE is the silent wrong-address direction")

    def test_a_human_title_is_not_shaped(self):
        for title in ("helm coordinator 7-22", "sid-a", "NEW-sid",
                      "my session", "1"):
            self.assertFalse(orcaadopt.session_shaped(title), title)

    def test_absent_and_empty_are_not_shaped(self):
        for empty in (None, "", "   "):
            self.assertFalse(orcaadopt.session_shaped(empty), repr(empty))

    def test_a_TRUNCATED_id_is_not_shaped(self):
        """A prefix is what a log or an alert prints, and it must not be
        mistaken for the thing itself."""
        self.assertFalse(orcaadopt.session_shaped("56a628d4"))
        self.assertFalse(
            orcaadopt.session_shaped("56a628d4-4a18-45b5-9b03-2741c4c1d2"))

    def test_a_longer_string_CONTAINING_an_id_is_not_shaped(self):
        """fullmatch, not search — otherwise `--resume "run 56a628d4-...-265
        again"` would read as an id."""
        self.assertFalse(orcaadopt.session_shaped(
            "run 56a628d4-4a18-45b5-9b03-2741c4c1d265 again"))


def _joining_adapter(rows, key_to_handle):
    """An orca adapter whose inventory is `rows` and whose resolve_pane answers
    from `key_to_handle` — the one bridge from a pane key to a live handle."""

    class Ad:
        name = "orca"

        def panes(self):
            return [dict(r) for r in rows], None

        def resolve_pane(self, key):
            handle = key_to_handle.get(key)
            return {"handle": handle} if handle else {}

    return Ad()


class PaneRowsWalksTheSessionJoin(unittest.TestCase):
    """`seat panes` must reach every seat `seat where` reaches.

    MEASURED: `seat where <seat>` answered `orca-adopted — LIVE`
    with a pid, a pane key and a resolved handle while `seat panes` called that
    same live pane `unowned` in the same second, because `resolve()` walks the
    env join AND the session join and this listing walked only the first. An
    operator read the weaker surface and prescribed a relaunch for a seat that
    needed none. Every arm below fails apart on purpose.
    """

    def test_a_nameless_pane_is_labelled_by_its_current_session(self):
        """THE DEFECT. No HELM_CHAT_NAME, a pane key, and a live process on the
        seat's CURRENT roster sid — that is an identity, and it used to read as
        `unowned`."""
        out, note = orcaadopt.pane_rows(
            adapter=_joining_adapter([{"handle": "h-nameless"}],
                                     {"pk-n": "h-nameless"}),
            procs=[_proc(11, seat=None, pane_key="pk-n", resume_sid="sid-a")],
            unreadable=[], roster_sids={"helm-claude": "sid-a"})
        self.assertIsNone(note)
        row = out[0]
        # THE VALUE, not the key: a `seat` of None still has a `seat` key.
        self.assertEqual(row["seat"], "helm-claude")
        self.assertEqual(row["provenance"], orcaadopt.ORCA_ADOPTED)
        self.assertNotIn("identity_partial", row)

    def test_the_env_join_is_untouched_when_the_session_join_runs(self):
        """ADDITIVITY, with a positive control in the same call: the named row
        keeps its answer while the nameless row gains one."""
        out, _ = orcaadopt.pane_rows(
            adapter=_joining_adapter(
                [{"handle": "h-named"}, {"handle": "h-nameless"}],
                {"pk-named": "h-named", "pk-n": "h-nameless"}),
            procs=[_proc(1, seat="codex", pane_key="pk-named"),
                   _proc(2, seat=None, pane_key="pk-n", resume_sid="sid-a")],
            unreadable=[], roster_sids={"helm-claude": "sid-a"})
        got = {r["handle"]: r["seat"] for r in out}
        self.assertEqual(got["h-named"], "codex")       # control: unchanged
        self.assertEqual(got["h-nameless"], "helm-claude")

    def test_the_env_join_wins_a_contested_handle(self):
        """A DECLARATION OUTRANKS A JOIN. Both rungs reach one handle; the
        process that NAMED itself keeps it."""
        out, _ = orcaadopt.pane_rows(
            adapter=_joining_adapter([{"handle": "h1"}],
                                     {"pk-a": "h1", "pk-b": "h1"}),
            procs=[_proc(1, seat="codex", pane_key="pk-a"),
                   _proc(2, seat=None, pane_key="pk-b", resume_sid="sid-a")],
            unreadable=[], roster_sids={"helm-claude": "sid-a"})
        self.assertEqual(out[0]["seat"], "codex")

    def test_two_seats_reaching_one_pane_leaves_it_unowned(self):
        """THE REFUSAL THIS RUNG ADDS. `_session_joined` reasons about ONE seat
        so it cannot see this; awarding the pane to whichever sorted first would
        type into somebody's session."""
        ad = _joining_adapter([{"handle": "h1"}], {"pk-a": "h1", "pk-b": "h1"})
        contested = [_proc(1, seat=None, pane_key="pk-a", resume_sid="sid-a"),
                     _proc(2, seat=None, pane_key="pk-b", resume_sid="sid-b")]
        roster = {"seat-one": "sid-a", "seat-two": "sid-b"}
        out, _ = orcaadopt.pane_rows(adapter=ad, procs=contested,
                                     unreadable=[], roster_sids=roster)
        self.assertEqual(out[0]["seat"], None)
        self.assertEqual(out[0]["provenance"], orcaadopt.UNOWNED)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE — drop the second claimant
        # and the very same pane IS labelled. Without this the arm would pass
        # against a fixture that could never label anything.
        out, _ = orcaadopt.pane_rows(adapter=ad, procs=contested[:1],
                                     unreadable=[], roster_sids=roster)
        self.assertEqual(out[0]["seat"], "seat-one")
        self.assertEqual(out[0]["provenance"], orcaadopt.ORCA_ADOPTED)

    def test_one_session_on_two_live_processes_names_no_pane(self):
        """`_session_joined`'s AMBIGUITY refusal still holds through this path.
        The single-process control below proves the fixture can label at all."""
        args = dict(adapter=_joining_adapter([{"handle": "h1"}],
                                             {"pk-a": "h1", "pk-b": "h1"}),
                    unreadable=[], roster_sids={"helm-claude": "sid-a"})
        out, _ = orcaadopt.pane_rows(
            procs=[_proc(1, seat=None, pane_key="pk-a", resume_sid="sid-a"),
                   _proc(2, seat=None, pane_key="pk-b", resume_sid="sid-a")],
            **args)
        self.assertEqual(out[0]["seat"], None)
        out, _ = orcaadopt.pane_rows(                   # CONTROL: one holder
            procs=[_proc(1, seat=None, pane_key="pk-a", resume_sid="sid-a")],
            **args)
        self.assertEqual(out[0]["seat"], "helm-claude")

    def test_a_process_declaring_another_seat_names_no_pane(self):
        """`_session_joined`'s CONTRADICTION refusal still holds: a row on our
        sid that calls itself something else is not our pane."""
        out, _ = orcaadopt.pane_rows(
            adapter=_joining_adapter([{"handle": "h1"}], {"pk-a": "h1"}),
            procs=[_proc(1, seat="codex-3", pane_key="pk-a",
                         resume_sid="sid-a")],
            unreadable=[], roster_sids={"helm-claude": "sid-a"})
        # It is the declaring seat's by its own declaration, never the roster's.
        self.assertEqual(out[0]["seat"], "codex-3")

    def test_a_history_sid_is_not_an_address(self):
        """ADDRESSING USES THE CURRENT SESSION ONLY. A process resumed on a sid
        the seat once held is whoever reopened that transcript."""
        args = dict(adapter=_joining_adapter([{"handle": "h1"}],
                                             {"pk-a": "h1"}),
                    unreadable=[], roster_sids={"helm-claude": "sid-CURRENT"})
        out, _ = orcaadopt.pane_rows(
            procs=[_proc(1, seat=None, pane_key="pk-a",
                         resume_sid="sid-OLD")], **args)
        self.assertEqual(out[0]["seat"], None)
        # POSITIVE CONTROL, same observable: the CURRENT sid on the same pane
        # does address it — so the refusal above is about the sid, not about a
        # fixture that never labels.
        out, _ = orcaadopt.pane_rows(
            procs=[_proc(1, seat=None, pane_key="pk-a",
                         resume_sid="sid-CURRENT")], **args)
        self.assertEqual(out[0]["seat"], "helm-claude")


class AFreshProcessIsNamedFromItsSessionRecord(unittest.TestCase):
    """task/2673 PART C. A pane Orca opened runs a bare `claude`: no
    HELM_CHAT_NAME and no `--resume <sid>`, so neither join could name it even
    after the pane joined the roster. Claude's own pid-keyed session record
    names its session, and it is taken only when its procStart is this
    process's birth stamp. Every refusal of the session join still holds."""

    ROSTER = {"proj-claude": "sid-a"}

    def rows(self, procs, records, rows=({"handle": "h1"},),
             keys=None):
        ad = _joining_adapter(list(rows), keys or {"pk-a": "h1", "pk-b": "h1"})
        with mock.patch("helm.beacons.holder_records", return_value=records):
            out, note = orcaadopt.pane_rows(adapter=ad, procs=procs,
                                            unreadable=[],
                                            roster_sids=self.ROSTER)
        self.assertIsNone(note)
        return out

    def test_a_fresh_process_is_named_by_its_record(self):
        out = self.rows([_proc(11, pane_key="pk-a", start="1000")],
                        {"sid-a": (11, "1000")})
        self.assertEqual(out[0]["seat"], "proj-claude")
        self.assertEqual(out[0]["provenance"], orcaadopt.ORCA_ADOPTED)

    def test_a_procstart_mismatch_leaves_it_unowned(self):
        out = self.rows([_proc(11, pane_key="pk-a", start="1000")],
                        {"sid-a": (11, "999")})
        self.assertIsNone(out[0]["seat"])
        self.assertEqual(out[0]["provenance"], orcaadopt.UNOWNED)

    def test_two_processes_on_one_session_are_refused(self):
        procs = [_proc(11, pane_key="pk-a", start="1000"),
                 _proc(12, pane_key="pk-b", resume_sid="sid-a")]
        out = self.rows(procs, {"sid-a": (11, "1000")})
        self.assertEqual(out[0]["provenance"], orcaadopt.UNOWNED)
        self.assertIsNone(out[0]["seat"])  # noqa: VACUOUS_ASSERTION — control follows on the same observable
        # CONTROL: the record-named process alone IS labelled.
        out = self.rows(procs[:1], {"sid-a": (11, "1000")})
        self.assertEqual(out[0]["seat"], "proj-claude")

    def test_unenumerable_records_say_so_on_the_unowned_row(self):
        out = self.rows([_proc(11, pane_key="pk-a", start="1000")], None)
        self.assertIsNone(out[0]["seat"])
        self.assertIn("session records", out[0]["identity_partial"])

    def test_an_unowned_pane_says_whether_it_holds_an_agent(self):
        out = self.rows([_proc(11, pane_key="pk-a", start="1000")], {},
                        rows=({"handle": "h1"}, {"handle": "h-shell"}),
                        keys={"pk-a": "h1"})
        got = {r["handle"]: r["holds_agent"] for r in out}
        self.assertEqual(got, {"h1": True, "h-shell": False})


class ResolveWalksTheRecordRungToo(unittest.TestCase):
    """`seat where` and the send path must name the same panes `seat panes`
    names. A bare `claude` Orca opened (no HELM_CHAT_NAME, no --resume) is
    resolved from Claude's own session record, only on a (pid, procStart)
    match that exactly one sid claims; otherwise it stays unresolved."""

    SEAT = "seat-under-test"

    def resolve(self, procs, records, sid="sid-a"):
        ad = _joining_adapter([{"handle": "h1"}], {"pk-a": "h1", "pk-b": "h2"})
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=(procs, [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({self.SEAT: {"session": sid}}, False)), \
                mock.patch("helm.beacons.holder_records", return_value=records), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            return orcaadopt.resolve(self.SEAT, adapter=ad)

    def test_a_record_only_process_resolves(self):
        info = self.resolve([_proc(11, pane_key="pk-a", start="1000")],
                            {"sid-a": (11, "1000")})
        self.assertEqual(info.get("handle"), "h1")
        self.assertEqual([int(p) for p in info["pids"]], [11])
        self.assertEqual(info["pids"][0].start, "1000")

    def test_a_procstart_mismatch_stays_unresolved(self):
        info = self.resolve([_proc(11, pane_key="pk-a", start="1000")],
                            {"sid-a": (11, "999")})
        self.assertEqual(info["pids"], [])
        self.assertNotIn("handle", info)
        # CONTROL on the same fixture: the matching stamp resolves.
        info = self.resolve([_proc(11, pane_key="pk-a", start="1000")],
                            {"sid-a": (11, "1000")})
        self.assertEqual(info.get("handle"), "h1")

    def test_two_sids_claiming_one_process_stay_unresolved(self):
        info = self.resolve([_proc(11, pane_key="pk-a", start="1000")],
                            {"sid-a": (11, "1000"), "sid-b": (11, "1000")})
        self.assertEqual(info["pids"], [])
        self.assertNotIn("handle", info)
        # CONTROL: one claimant resolves.
        info = self.resolve([_proc(11, pane_key="pk-a", start="1000")],
                            {"sid-a": (11, "1000")})
        self.assertEqual(info.get("handle"), "h1")

    def test_an_argv_resume_still_resolves_as_before(self):
        # CONTROL: the argv rung is untouched, with or without records, and
        # a record never overrides an argv --resume.
        for records in ({}, None, {"sid-other": (11, "1000")}):
            info = self.resolve([_proc(11, pane_key="pk-a", start="1000",
                                       resume_sid="sid-a")], records)
            self.assertEqual(info.get("handle"), "h1", records)
            self.assertEqual([int(p) for p in info["pids"]], [11])


class AnUnreadableRosterIsSaidNotSwallowed(unittest.TestCase):

    def test_unowned_rows_carry_the_reason_and_labelled_rows_do_not(self):
        """"I could not tell" and "no identity found" must never be the same
        value. The env-labelled row is the control: it has positive evidence, so
        it carries no doubt."""
        out, note = orcaadopt.pane_rows(
            adapter=_joining_adapter(
                [{"handle": "h-named"}, {"handle": "h-none"}],
                {"pk-named": "h-named"}),
            procs=[_proc(1, seat="codex", pane_key="pk-named")],
            unreadable=[], roster_failed=True)
        self.assertIsNone(note)                 # the listing still returns
        got = {r["handle"]: r for r in out}
        self.assertEqual(got["h-named"]["seat"], "codex")
        self.assertNotIn("identity_partial", got["h-named"])
        self.assertIn("roster", got["h-none"]["identity_partial"])
        self.assertEqual(got["h-none"]["seat"], None)

    def test_a_readable_roster_leaves_no_doubt_marker(self):
        """The control for the arm above — without it, an `identity_partial`
        that was ALWAYS present would pass it."""
        args = dict(adapter=_joining_adapter([{"handle": "h-none"}], {}),
                    procs=[], unreadable=[])
        out, _ = orcaadopt.pane_rows(roster_sids={}, **args)
        self.assertNotIn("identity_partial", out[0])
        self.assertEqual(out[0]["provenance"], orcaadopt.UNOWNED)
        # POSITIVE CONTROL ON THE SAME FIELD — the identical unowned row DOES
        # carry the marker when the roster is blind, so its absence above is
        # the readable roster and not a field that is never set.
        out, _ = orcaadopt.pane_rows(roster_failed=True, **args)
        self.assertIn("roster", out[0]["identity_partial"])


class TheCurrentSessionRuleHasOneDefinition(unittest.TestCase):
    """Two readers of "current" is how one surface starts addressing a seat the
    other would refuse."""

    def _roster(self, row):
        return mock.patch("helm.seats.roster_checked",
                          return_value=({"s": row}, False))

    def test_both_readers_agree_on_every_malformed_shape(self):
        # UNCONDITIONAL POSITIVE CONTROL FIRST. The loop below is mostly
        # absence assertions; if both readers were broken to return None
        # always, every one of them would pass. This one cannot.
        with self._roster({"session": "sid-live"}):
            self.assertEqual(orcaadopt.roster_identity("s")[0], "sid-live")
            self.assertEqual(orcaadopt.roster_current_sids()[0],
                             {"s": "sid-live"})
        for row, expected in (({"session": "sid-a"}, "sid-a"),
                              ({"session": ""}, None),
                              ({"session": None}, None),
                              ({"session": 42}, None),
                              ({}, None)):
            with self.subTest(row=row), self._roster(row):
                one, _sids, failed = orcaadopt.roster_identity("s")
                self.assertFalse(failed)
                many, m_failed = orcaadopt.roster_current_sids()
                self.assertFalse(m_failed)
                self.assertEqual(one, expected)
                # The map OMITS a seat with no current sid rather than
                # storing None — an address of None is not an address.
                self.assertEqual(many.get("s"), expected)

    def test_an_unreadable_roster_is_failed_not_empty(self):
        with mock.patch("helm.seats.roster_checked", return_value=({}, True)):
            sids, failed = orcaadopt.roster_current_sids()
        self.assertTrue(failed)
        self.assertEqual(sids, {})

    def test_the_whole_roster_is_read_exactly_once_per_listing(self):
        """ONE READING SHARED BY THE PASS. Per-seat reads let a live tmpfs file
        change underneath one listing, so its rows would never have described a
        single moment."""
        probe = mock.Mock(return_value=({"a": {"session": "sid-a"},
                                         "b": {"session": "sid-b"},
                                         "c": {"session": "sid-c"}}, False))
        with mock.patch("helm.seats.roster_checked", probe):
            orcaadopt.pane_rows(
                adapter=_joining_adapter([{"handle": "h1"}], {"pk": "h1"}),
                procs=[_proc(1, seat=None, pane_key="pk",
                             resume_sid="sid-b")],
                unreadable=[])
        self.assertEqual(probe.call_count, 1)


class LivenessEvidenceNamesTheJoinThatFoundIt(unittest.TestCase):
    """A session join is weaker evidence than a declaration and has to LOOK
    weaker — an operator who believes HELM_CHAT_NAME is exported reaches for
    verbs that read it."""

    def _evidence(self, named):
        state, evidence = orcaadopt.seat_liveness(
            "helm-claude", procs=list(named), unreadable=[], named=named)
        self.assertEqual(state, orcaadopt.LIVE)
        return evidence

    def test_a_declared_process_is_named_by(self):
        ev = self._evidence([_proc(7, seat="helm-claude", pane_key="pk")])
        self.assertEqual(ev, "seat is named by 1 live claude process (pid 7)")

    def test_a_session_joined_process_is_not_called_named(self):
        """THE OVERCLAIM. pid 1214961 carried no HELM_CHAT_NAME and this line
        called it "named by"."""
        ev = self._evidence([_proc(9, seat=None, pane_key="pk",
                                   resume_sid="sid-a")])
        self.assertEqual(
            ev, "seat is joined by current session to 1 live claude "
                "process (pid 9)")

    def test_a_mixed_set_reports_both_joins_separately(self):
        ev = self._evidence([_proc(7, seat="helm-claude"),
                             _proc(9, seat=None, resume_sid="sid-a")])
        self.assertEqual(
            ev, "seat is named by 1 live claude process (pid 7) and joined by "
                "current session to 1 live claude process (pid 9)")


class ThePanesSurfaceSaysWhenItCouldNotName(unittest.TestCase):
    """HUMAN-SURFACE PARITY. `identity_partial` that no surface prints is a
    field, not a warning — and `seat panes` is the exact surface an operator
    reads before concluding a live seat is unreachable."""

    def _render(self, rows):
        from helm import seat
        buf = io.StringIO()
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([], [])), \
                mock.patch.object(orcaadopt, "pane_rows",
                                  return_value=(rows, None)), \
                contextlib.redirect_stdout(buf):
            rc = seat._panes([])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _row(self, **kw):
        base = {"handle": "h1", "provenance": orcaadopt.UNOWNED, "seat": None,
                "status": "connected", "worktree": "/w"}
        base.update(kw)
        return base

    def test_the_roster_blindness_reaches_the_operator(self):
        out = self._render([self._row(identity_partial="the chat roster could "
                                                       "not be read")])
        self.assertIn("the chat roster could not be read", out)
        self.assertIn("may be a seat this listing could not name", out)

    def test_a_clean_listing_prints_no_such_note(self):
        """The control. Without it, a note printed unconditionally would pass
        the arm above."""
        out = self._render([self._row()])
        self.assertNotIn("could not name", out)
        # POSITIVE CONTROL — the renderer DID run and DID print this row, so
        # the absence above is the missing marker and not a silent surface.
        self.assertIn("h1", out)
        self.assertIn("unowned (1)", out)

    def test_one_reason_shared_by_many_rows_prints_once(self):
        """Ten unowned rows are one blindness, not ten warnings — an operator
        surface that repeats itself per row buries the listing it annotates."""
        reason = "the chat roster could not be read"
        rows = [self._row(handle="h%d" % i, identity_partial=reason)
                for i in range(10)]
        self.assertEqual(self._render(rows).count("could not name"), 1)


if __name__ == "__main__":
    unittest.main()


class LivenessKeyAnswersItsOwnQuestionTest(unittest.TestCase):
    """Two questions were wearing one word.

    This module answers "is a process holding this seat open" — the
    duplicate-resume guard. `seat_liveness` answers "is this agent able to
    work" by reading the pane. They diverge exactly where a seat has BOTH,
    and a reader branching on RUNNING/IDLE/CONTEXT_FULL falls to its
    else-branch on a seat that is merely held open."""

    def test_RESOLVE_PUBLISHES_the_attached_key_not_just_the_constant(self):
        """My first version of this arm asserted only the CONSTANT, and a
        mutation deleting the whole `attached` key from resolve()'s payload
        SURVIVED it. Testing the name of a thing is not testing that the thing
        is published."""
        nameless = _proc(8888, seat=None, pane_key="pk-8888",
                         resume_sid="sid-joined")
        with mock.patch.object(orcaadopt, "claude_processes",
                               return_value=([nameless], [])), \
                mock.patch("helm.seats.roster_checked",
                           return_value=({"adopted-seat":
                                          {"session": "sid-joined"}}, False)), \
                mock.patch("helm.sessions.live_sids", return_value={}):
            info = orcaadopt.resolve("adopted-seat")
        self.assertIsNotNone(info)
        self.assertIn("attached", info)
        self.assertEqual(info["attached"]["state"], orcaadopt.LIVE)
        self.assertEqual(info["attached"]["question"],
                         orcaadopt.ATTACHED_QUESTION)
        # and the ANSWER states its question rather than leaving the key to imply
        self.assertEqual(info["liveness"]["question"],
                         orcaadopt.ATTACHED_QUESTION)
        self.assertEqual(orcaadopt.ATTACHED_QUESTION,
                         "is a process holding this seat open")
        self.assertNotIn("able to work", orcaadopt.ATTACHED_QUESTION)

    def test_liveness_KEEPS_its_value_because_a_consumer_pins_it(self):
        """ADD-AND-DEPRECATE, NOT REPOINT. Repointing a key's MEANING under a
        live reader is the silent kind of break, and one existing arm asserts
        liveness.state == orcaadopt.LIVE."""
        info = {"liveness": {"state": orcaadopt.RICH_STATE[orcaadopt.LIVE]},
                "attached": {"state": orcaadopt.RICH_STATE[orcaadopt.LIVE]}}
        # both surfaces answer the SAME question today; that is the point of a
        # deprecation window rather than a swap.
        self.assertEqual(info["liveness"]["state"], info["attached"]["state"])
        self.assertEqual(orcaadopt.RICH_STATE[orcaadopt.LIVE], orcaadopt.LIVE)
        self.assertEqual(orcaadopt.RICH_STATE[orcaadopt.DEAD], "GONE")

    def test_ORCAADOPT_NEVER_CALLS_seat_liveness_because_that_would_CYCLE(self):  # noqa: VACUOUS_ASSERTION — the real reverse dependency is the unconditional positive control
        """THE LOAD-BEARING CONSTRAINT, pinned as a contract of this module
        rather than left to whoever edits it next.

        `seat_liveness` FALLS BACK to resolve() for family-less seats
        (seat_lifecycle.py, _liveness_from_orcaadopt), so a call in this direction cycles
        on exactly the orca-adopted seats the fallback exists to serve. The
        obvious implementation of "make liveness return what seat_liveness
        returns" is that cycle, which is why this is a test and not a comment.
        """
        src = inspect.getsource(orcaadopt)
        # THE CROSS-MODULE call is the one that cycles. This module defines its
        # OWN seat_liveness — a DIFFERENT function, (state, evidence), for "is
        # anything already holding this seat open" — and calls it freely. My
        # first version of this assertion said "seat_liveness(" and failed on
        # that local definition, which is this lane's own two-questions-one-word
        # defect appearing at the FUNCTION-NAME level.
        self.assertNotIn("seat.seat_liveness(", src)
        self.assertNotIn("_seat.seat_liveness(", src)
        self.assertNotIn("from .seat import seat_liveness", src)
        # MUST-HIT control: the LOCAL one really is there and really is called,
        # so the assertions above are about direction, not about absence.
        self.assertIn("def seat_liveness(", src)
        self.assertIsNot(orcaadopt.seat_liveness,
                         __import__("helm.seat", fromlist=["x"]).seat_liveness)
        # MUST-HIT control: the reverse dependency IS real, so the assertion
        # above is about direction and not about a name that appears nowhere.
        from helm import seat as _seat, seat_lifecycle as _seat_lifecycle
        self.assertIn("_liveness_from_orcaadopt",
                      inspect.getsource(_seat_lifecycle))
        self.assertIn("orcaadopt.resolve", inspect.getsource(_seat))

    def test_the_DIVERGENCE_is_real_and_neither_function_is_wrong(self):
        """A seat with BOTH a process and a pane gets two different true
        answers. #141 keeps them apart on the consumer side — "LIVE stays LIVE,
        process evidence must not be upgraded to RUNNING's turn-in-flight
        claim" — and this pins that the producer agrees."""
        # attached speaks the transport vocabulary through RICH_STATE ...
        self.assertEqual(set(orcaadopt.RICH_STATE), {orcaadopt.LIVE,
                                                     orcaadopt.DEAD})
        # ... and every value it can publish is a DECLARED fleet state, so a
        # reader branching on the declared names never meets an unknown word.
        from helm import seat as _seat
        for v in orcaadopt.RICH_STATE.values():
            self.assertIn(v, _seat._STATE_NAMES)
        # UNKNOWN is the default for an unrecognised internal state, never a
        # confident answer.
        self.assertEqual(orcaadopt.RICH_STATE.get("nonsense", "UNKNOWN"),
                         "UNKNOWN")
