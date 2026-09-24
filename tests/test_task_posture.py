#!/usr/bin/env python3
"""The posture guard at the artifact doors (helm/posture.py, task/1346).

A task-add body or a dispatch body that applies a strategy verb to a named
dependency — the specimen is task/1345, a fork fix plus upstream PR to Orca —
is refused at exit 2 with the three seam questions, unless the body carries a
`posture:` clause answering them or the caller records --posture-na REASON.

Hermetic: HELM_HOME / HELM_ADOPTED_DIR / HELM_CHAT_DIR are tmp dirs; the real
~/.helm is never read or written. The specimen texts below are the 1342 and
1345 rows AS FILED on 2026-08-22 (copied from the live ledger, read-only), so
the arms measure the predicate against the population it was tuned on.

Each arm names the mutation it kills, so a green run can be read as a set of
refuted edits rather than a set of executed lines.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import dispatches, posture, tasks  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_ADOPTED_DIR",
            "MELD_ADOPTED_DIR", "HELM_ACTOR", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_ROOM", "HELM_PROC", "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")

# task/1345 AS FILED (agent origin) — the specimen that carried every fact and
# asked none of the questions.
T1345_TITLE = ("orca replays a pane with no env — expose launchConfig through "
               "the CLI so helm can register each seat launch for resume")
T1345_NOTE = (
    "MEASURED 2026-08-22 from the orca fork source. Orca's resume replays the "
    "per-pane launchConfig envelope VERBATIM when present: agentCommand, "
    "agentArgs, agentEnv at src/shared/agent-session-resume.ts:39-53 and "
    "src/shared/tui-agent-startup.ts:206-208. Without it Orca synthesizes "
    "claude --resume sid with only settings.agentDefaultEnv, which is EMPTY "
    "for claude — so CLAUDE_CONFIG_DIR, ANTHROPIC_BASE_URL and HELM_CHAT_NAME "
    "are never carried and every cliproxy seat fails No conversation found "
    "after a reboot. The envelope is accepted by RPC terminal.create at "
    "src/main/runtime/rpc/methods/terminal.ts:903-924 but the orca CLI that "
    "helm uses forwards only worktree, command, title, focus, rendererBacked, "
    "activate, presentation at src/cli/handlers/terminal.ts:126-149. TWO "
    "HALVES: fork fix adding env and launch-command pass-through to the CLI — "
    "small, upstream-PR-shaped; and helm seat spawn registering launchConfig "
    "with agentCommand equal to the seat launch.sh path and agentEnv carrying "
    "the seat identity env, so Orca's own resume replays the right thing. "
    "Also found: new-tab resume drops cwd at "
    "src/renderer/src/lib/sleeping-agent-session-launch.ts:254 though "
    "startupCwd exists at store/slices/terminals.ts:630; and "
    "POST_REPLAY_MODE_RESET at layout-serialization.ts:34-37 omits ?1049l. "
    "Prior art: codex-5's uncommitted 92-file lane on the fork, checkpointed "
    "at c1394be82, makes a missing launchConfig fail CLOSED to a recovery "
    "shell — a correct complement, not this cure. Requires an Orca rebuild to "
    "take effect; sequenced AFTER the helm-side sweep in task/1342, which "
    "needs no rebuild.")

# task/1342 AS FILED (owner-asked) — names the same fork-plus-PR in passing
# and must stay silent, on the TEXT alone and on the origin.
T1342_TITLE = ("reboot resilience — orca+helm runs fine only if the laptop "
               "never restarts or crashes")
T1342_NOTE = (
    "OWNER 2026-08-22 11:17 PDT, verbatim: basically our orca+helm setup runs "
    "fine overall, as long as we never restart the laptop or have any crashes "
    "or anything. MEASURED after his reboot: Orca replays each pane as bare "
    "claude --dangerously-skip-permissions --resume sid with NO env, so every "
    "cliproxy seat fails No conversation found because its session lives "
    "under a per-seat CLAUDE_CONFIG_DIR only launch.sh sets — verified at "
    "seats/codex/claude/projects/-home-user/3c4dfaef.jsonl. Native seats "
    "resume but lose HELM_CHAT_NAME and come back as derived helm-claude-N. "
    "Panes replayed outside helm's launch owner also show stale mouse-mode "
    "junk at the shell — the 1287 cure covered only the helm-exit door. The "
    "fix is the post-reboot sweep composing the existing rebind verb with "
    "per-seat resume — lane seat-resume-all-after-reboot in build now — plus "
    "an auto-trigger on Orca startup, plus a fork fix and upstream PR so Orca "
    "replays the pane's real command. Owner wants the existing sessions "
    "resumed, never re-prompted: re-prompting with new sessions is a failure "
    "of progress.")

POSTURE = ("posture: HORIZON the next Orca release overwrites the fork. "
           "OWNERSHIP nobody realigns it today. WHO-CARES stablyai has "
           "thousands of PRs and no reason to want this one.")


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(args))
    return rc, out.getvalue(), err.getvalue()


class PredicateTest(unittest.TestCase):
    """seam_hits / has_posture / check — pure, no fixture."""

    def test_fires_on_the_1345_specimen(self):
        # kills: a predicate that drops the adjacency walk, or one that never
        # reaches `rebuild` / `fork` beside `orca`
        hits = posture.seam_hits(T1345_TITLE + "\n" + T1345_NOTE)
        self.assertTrue(hits)
        self.assertTrue(any(h.startswith("rebuild ~ orca") for h in hits), hits)

    def test_silent_on_the_1342_text(self):
        # kills: generic `upstream` pairing with `pr` ("upstream PR" is the
        # owner's ordinary phrase), or a wider-than-one connector window
        # ("PR so Orca")
        self.assertEqual(posture.seam_hits(T1342_TITLE + "\n" + T1342_NOTE), [])
        # the positive control on the same predicate: the same words, one
        # connector instead of "so", is the specimen phrase and fires
        self.assertEqual(posture.seam_hits("a fork fix and upstream PR to Orca"),
                         ["pr ~ orca: 'pr to orca'"])

    def test_co_occurrence_alone_is_not_a_hit(self):
        # kills: replacing adjacency with anywhere-in-body co-occurrence, which
        # would fire on most of helm's history
        body = "orca is the host. we should patch the console renderer."
        self.assertEqual(posture.seam_hits(body), [])
        self.assertTrue(posture.seam_hits("we should patch orca"))
        self.assertTrue(posture.seam_hits("an upstream PR to orca"))
        self.assertTrue(posture.seam_hits("rebuild the fork"))

    def test_a_posture_clause_needs_all_three_labels(self):
        # kills: any-label-satisfies, or a clause check that ignores the
        # `posture:` marker
        self.assertTrue(posture.has_posture("x " + POSTURE))
        self.assertFalse(posture.has_posture(
            "posture: horizon fine. ownership me."))          # no who-cares
        self.assertFalse(posture.has_posture(
            "horizon fine. ownership me. who-cares nobody."))  # no marker
        self.assertTrue(posture.has_posture(
            "Posture: Horizon a. Ownership b. WHO CARES c."))  # case-insensitive

    def test_check_exits_on_each_escape(self):
        body = "Requires an Orca rebuild."
        self.assertIn("HORIZON", posture.check("door", body) or "")
        self.assertIsNone(posture.check("door", body + " " + POSTURE))
        self.assertIsNone(posture.check("door", body, posture_na="helm-side"))
        # kills: a blank --posture-na disarming the guard
        self.assertIsNotNone(posture.check("door", body, posture_na="   "))
        self.assertIsNone(posture.check("door", body, owner=True))


class ModuleDoorTest(unittest.TestCase):
    """P1 (e): the guard lived only at the CLI door, so tasks.add
    and dispatches.send/add from any script filed a seam strategy unasked.
    It lives at the invariant now; these arms call the module functions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-posture-api-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write("a\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "a")
        self.a = self.git("rev-parse", "HEAD")
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, self.repo)

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        p = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def test_tasks_add_refuses_a_seam_strategy_from_any_caller(self):
        # kills: the guard at cmd_task only
        row, err = tasks.add(T1345_TITLE, None, note=T1345_NOTE, origin="agent")
        self.assertIsNone(row)
        self.assertIn("HORIZON", err or "")
        self.assertFalse(os.path.exists(tasks.ledger_path()))
        # the escapes, as keyword args: owner origin, a recorded reason, a
        # tombstone (history, not work)
        row, err = tasks.add(T1342_TITLE, None, note=T1342_NOTE, origin="owner")
        self.assertIsNone(err, err)
        row, err = tasks.add(T1345_TITLE, None, note=T1345_NOTE, origin="owner")
        self.assertIsNone(err, err)
        # THE SECOND COPY OF ONE SPECIMEN MEETS A SECOND DOOR. Holding the
        # 1345 text fixed across the escapes is how this arm proves the ESCAPE
        # and not the text changed the outcome — and `tasks.add` now resolves
        # a title against the open rows, so the copy filed just above makes
        # every later one a near-duplicate (helm/tasks.py: duplicate_verdict).
        #
        # PROVE WHICH GUARD SPOKE BEFORE OVERRIDING IT. The refusal names the
        # duplicate and NOT the posture questions, which is the stronger form
        # of what this arm always wanted: it shows the posture escape HELD and
        # that the only thing left standing is the unrelated guard the
        # operator's own `--force-new` is for. Silencing it with force_new
        # alone would leave that unproven.
        row, err = tasks.add(T1345_TITLE, None, note=T1345_NOTE, origin="agent",
                             posture_na="helm-side only")
        self.assertIsNone(row)
        self.assertIn("already says", err or "")
        self.assertNotIn("HORIZON", err or "")
        row, err = tasks.add(T1345_TITLE, None, note=T1345_NOTE, origin="agent",
                             posture_na="helm-side only", force_new=True)
        self.assertIsNone(err, err)
        self.assertEqual(row["posture_na"], "helm-side only")
        row, err = tasks.add(T1345_TITLE, None, note=T1345_NOTE, origin="agent",
                             status="closed", closed_reason="history import")
        self.assertIsNone(err, err)

    def test_dispatches_send_and_add_refuse_a_seam_strategy_from_any_caller(self):
        # kills: the guard at cmd_dispatch only; add() lacking it
        row, why, posted = dispatches.send(
            "seat-b", "lane-z", "Requires an orca rebuild to take effect.",
            self.a, repo=self.repo, key="k-2", sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertFalse(posted)
        self.assertIn("HORIZON", why or "")
        self.assertFalse(os.path.exists(dispatches.ledger_path()))
        row, why = dispatches.add("seat-b", "lane-z", self.a, repo=self.repo,
                                  note="patch orca before the reboot",
                                  kind="build", new_work=True, notify=False,
                                  _reason=True)
        self.assertIsNone(row)
        self.assertIn("HORIZON", why or "")
        self.assertFalse(os.path.exists(dispatches.ledger_path()))
        # the positive control: the recorded escape lands on the add() row
        row, why = dispatches.add("seat-b", "lane-z", self.a, repo=self.repo,
                                  note="patch orca before the reboot",
                                  kind="build", new_work=True, notify=False,
                                  _reason=True, posture_na="helm-side sweep")
        self.assertIsNone(why, why)
        self.assertEqual(row["posture_na"], "helm-side sweep")


class DoorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-posture-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CHAT_NAME"] = "seat-a"

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger(self):
        p = tasks.ledger_path()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]


class TaskDoorTest(DoorBase):
    def test_the_1345_specimen_is_refused_at_exit_2_with_the_three_questions(self):
        # kills: the guard removed from cmd_task add; rc 1 instead of 2; the
        # questions not printed; a write before the refusal
        rc, _out, err = run(tasks.cmd_task,
                            ["add", T1345_TITLE, "--note", T1345_NOTE])
        self.assertEqual(rc, 2, err)
        for label in ("HORIZON", "OWNERSHIP", "WHO-CARES"):
            self.assertIn(label, err)
        self.assertIn("--posture-na", err)
        self.assertEqual(self.ledger(), [], "a refusal must write nothing")

    def test_the_1342_row_files_as_it_did(self):
        # kills: a predicate wide enough to catch "upstream PR so Orca", and
        # the owner exemption (the row was --owner-asked)
        rc, out, err = run(tasks.cmd_task, ["add", T1342_TITLE, "--owner-asked",
                                            "--note", T1342_NOTE])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed task/1", out)
        # and the TEXT alone is silent too — the exemption is not what saved
        # it. Re-filing one title is this control's METHOD, and a second row
        # for one piece of work is exactly what `tasks.add` refuses now
        # (helm/tasks.py: duplicate_verdict), so the repeat is read in two
        # steps rather than dropped. Step one is the stronger reading of the
        # original control: the refusal it draws names the DUPLICATE and none
        # of the posture questions, which proves the posture guard is silent
        # on this text with no `--owner-asked` anywhere near it.
        rc, _out, err = run(tasks.cmd_task, ["add", T1342_TITLE,
                                             "--note", T1342_NOTE])
        self.assertEqual(rc, 2, err)
        self.assertIn("already says", err)
        for label in ("HORIZON", "OWNERSHIP", "WHO-CARES"):
            self.assertNotIn(label, err)
        # Step two: the operator's override files it, so the row this arm is
        # named for still lands as task/2 and the count the rest of the file
        # reads is unchanged.
        rc, out, err = run(tasks.cmd_task, ["add", T1342_TITLE, "--note",
                                            T1342_NOTE, "--force-new"])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed task/2", out)

    def test_a_posture_clause_in_the_note_satisfies_the_door(self):
        # kills: has_posture never consulted at the door
        rc, out, err = run(tasks.cmd_task, ["add", T1345_TITLE, "--note",
                                            T1345_NOTE + " " + POSTURE])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed task/1", out)
        self.assertNotIn("posture_na", self.ledger()[0])

    def test_posture_na_is_recorded_on_the_row_and_shown(self):
        # kills: the flag consumed but not written; written but not rendered
        rc, out, err = run(tasks.cmd_task, [
            "add", T1345_TITLE, "--note", T1345_NOTE,
            "--posture-na", "helm-side sweep only, the fork half is dropped"])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posture_na"],
                         "helm-side sweep only, the fork half is dropped")
        rc, out, _err = run(tasks.cmd_task, ["show", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("posture_na     helm-side sweep only", out)

    def test_a_valueless_posture_na_is_refused_not_defaulted(self):
        # kills: `--posture-na` eating the next flag as its value
        rc, _out, err = run(tasks.cmd_task, ["add", "plain title",
                                             "--posture-na", "--note", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("--posture-na", err)
        self.assertEqual(self.ledger(), [])

    def test_a_seamless_row_never_meets_the_guard(self):
        # must-miss: the guard is invisible to ordinary filing
        rc, out, err = run(tasks.cmd_task, ["add", "helm web UI uses gigabytes "
                                            "of memory per instance"])
        self.assertEqual(rc, 0, err)
        self.assertIn("filed task/1", out)


class DispatchDoorTest(DoorBase):
    """The dispatch door: the CLI refuses BEFORE the recipient or ref resolves
    (no roster, no repo needed for the refusal arm); the library records."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        os.makedirs(os.environ["HELM_PROC"])
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        with open(os.path.join(self.repo, "state"), "w", encoding="utf-8") as f:
            f.write("a\n")
        self.git("add", "state")
        self.git("commit", "-q", "-m", "a")
        self.a = self.git("rev-parse", "HEAD")
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, self.repo)

    def git(self, *args):
        p = subprocess.run(["git", "-C", self.repo, *args],
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def test_a_seam_body_is_refused_at_exit_2_before_any_resolution(self):
        # kills: the guard placed after the recipient gate (this recipient is
        # nobody and this ref is nothing — the posture refusal must come
        # first); rc 1; questions absent
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "send", "nobody-here", "lane-x",
            "Build the fork fix; requires an orca rebuild to take effect.",
            "--ref", "0000000", "--kind", "build", "--new-work"])
        self.assertEqual(rc, 2, err)
        for label in ("HORIZON", "OWNERSHIP", "WHO-CARES"):
            self.assertIn(label, err)
        self.assertFalse(os.path.exists(dispatches.ledger_path()),
                         "a refusal must write nothing")

    def test_posture_na_rides_the_dispatch_row(self):
        # kills: send() dropping the kwarg; the probe written without it
        row, why, _posted = dispatches.send(
            "seat-b", "lane-y", "Requires an orca rebuild to take effect.",
            self.a, repo=self.repo, key="k-1", sign=False, new_work=True,
            posture_na="no seam change: the sweep composes orca CLI verbs")
        self.assertIsNone(why)
        self.assertEqual(row["posture_na"],
                         "no seam change: the sweep composes orca CLI verbs")
        with open(dispatches.ledger_path(), encoding="utf-8") as f:
            stored = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(stored[0]["posture_na"],
                         "no seam change: the sweep composes orca CLI verbs")

    def test_a_posture_clause_in_the_body_passes_the_cli_door(self):
        # kills: the clause check missing at the dispatch door — the refusal
        # must NOT be the posture one (the fake recipient fails LATER, which is
        # the proof the guard stood aside)
        rc, _out, err = run(dispatches.cmd_dispatch, [
            "send", "nobody-here", "lane-x",
            "Requires an orca rebuild. " + POSTURE,
            "--ref", "0000000", "--kind", "build", "--new-work"])
        self.assertNotEqual(rc, 0)
        self.assertNotIn("HORIZON", err)


class TaskAddArgvAndIdentityTest(DoorBase):
    """task/1450 + task/1451 — cmd_task add's flag consumption and the --mine
    identity shortcut. Both arms are written to FAIL on the pre-cure code."""

    # A title that passes the posture door AND contains a flag-shaped word
    # further in. The posture clause is carried by the note so the ONLY thing
    # these arms can fail on is flag handling — a refusal for a posture reason
    # would prove nothing about the clause under test.
    # THE TITLE ARRIVES AS SEPARATE ARGV TOKENS, which is the whole hazard:
    # `"--mine" in rest` is a LIST MEMBERSHIP test, so a one-token title that
    # merely CONTAINS the word cannot trip it — my first fixture did that and
    # passed on the broken code. An operator typing an unquoted multi-word
    # title hands the flag word to argv as its own element.
    FLAGGY_ARGV = ["reboot", "resilience", "—", "the", "--mine", "shortcut",
                   "files", "an", "unowned", "row", "and", "nobody", "can",
                   "tell", "why"]
    ASKED_ARGV = ["reboot", "resilience", "—", "the", "--owner-asked", "flag",
                  "is", "eaten", "out", "of", "a", "title", "and", "nobody",
                  "can", "tell", "why"]

    def test_a_mine_word_inside_the_title_is_title_text_not_a_flag(self):
        # kills: membership-then-remove over the WHOLE argv; a mid-title flag
        # word being CONSUMED instead of reaching the guard. RULING
        # (the integrator, d2bdd5ab): refuse, do not file — a title that
        # contains a flag-shaped token stays ambiguous forever, and refusing
        # settles it at the door. Same shape as the unknown-flag arm.
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the
        # ledger must be WRITABLE here, or "a refusal wrote nothing" is a
        # statement about the fixture rather than about the guard.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        self.assertEqual(len(self.ledger()), 1, "control row did not file")
        rc, out, err = run(tasks.cmd_task,
                           ["add"] + self.FLAGGY_ARGV + ["--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r" % (rc, out))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("--mine", err)
        self.assertIn("--", err, "the refusal must name the escape")

    def test_an_owner_asked_word_inside_the_title_is_title_text(self):
        # kills: membership-then-remove over the WHOLE argv; a mid-title flag
        # word being CONSUMED instead of reaching the guard. RULING
        # (the integrator, d2bdd5ab): refuse, do not file — a title that
        # contains a flag-shaped token stays ambiguous forever, and refusing
        # settles it at the door. Same shape as the unknown-flag arm.
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the
        # ledger must be WRITABLE here, or "a refusal wrote nothing" is a
        # statement about the fixture rather than about the guard.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        self.assertEqual(len(self.ledger()), 1, "control row did not file")
        rc, out, err = run(tasks.cmd_task,
                           ["add"] + self.ASKED_ARGV + ["--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r" % (rc, out))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("--owner-asked", err)
        self.assertIn("--", err, "the refusal must name the escape")

    def test_a_valued_flag_word_inside_the_title_is_refused_too(self):
        # kills: _take() scanning the whole rest. This is the WIDER half of
        # task/1451 and it was not in the brief: a mid-title valued flag ate
        # itself AND its neighbour, filing owner="field" with two words gone.
        argv = ["reboot", "resilience", "—", "the", "--owner", "field", "is",
                "eaten", "and", "nobody", "can", "tell", "why"]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the
        # ledger must be WRITABLE here, or "a refusal wrote nothing" is a
        # statement about the fixture rather than about the guard.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        self.assertEqual(len(self.ledger()), 1, "control row did not file")
        rc, out, err = run(tasks.cmd_task,
                           ["add"] + argv + ["--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r" % (rc, out))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("--owner", err)
        # GUARD IDENTITY PINNED HERE, not on the boolean arm: --owner is in the
        # valued list, so it COULD reach the valueless refusal, which makes
        # these three assertions discriminating rather than decorative.
        self.assertIn("is not a title", err, "wrong guard refused")
        self.assertIn("before the whole title", err,
                      "the escape must be named for the mid-title case")
        self.assertNotIn("needs a value", err,
                         "the valueless guard must not own this case")


    def test_a_duplicate_word_keeps_its_position_in_the_title(self):
        # kills: value-based reconstruction (worse-than-main). The
        # first cut rebuilt the title by filtering on TOKEN TEXT, so a word
        # appearing both as a flag VALUE and in the TITLE bound to the wrong
        # occurrence and the title came out REORDERED. Exact field assertions,
        # not just rc, because rc was 0 the whole time this was broken.
        rc, out, err = run(tasks.cmd_task,
                           ["add", "--owner", "seat-a", "investigate",
                            "seat-a", "--note", T1342_NOTE])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["title"], "investigate seat-a")
        self.assertEqual(rows[0]["owner"], "seat-a")

    def test_a_mid_title_flag_names_the_whole_title_escape_not_a_value_form(self):
        # kills: a mid-title BOOLEAN flag being consumed instead of reaching
        # the guard. THIS ARM DOES NOT PIN VALUELESS ROUTING and an earlier
        # version of this comment claimed it did: --mine is not in the valued
        # list, so it can never reach that refusal and an assertNotIn here
        # could never fail. The valued arm above is that proof;
        # this one proves the boolean path reaches the stray guard at all.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        rc, out, err = run(tasks.cmd_task,
                           ["add", "fix", "the", "--mine", "bypass",
                            "--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r" % (rc, out))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("is not a title", err, "wrong guard refused")
        self.assertIn("before the whole title", err,
                      "the escape must be named for the mid-title case")


    def test_the_ambiguous_pair_is_decided_and_an_arm_keeps_it_decided(self):
        # THIS ARM IS A PRODUCT DECISION, NOT A MECHANISM (the integrator's
        # ruling, as corrected, with review concurring). `add plain
        # title --owner bob` and `add fix the --owner bypass` are grammatically
        # IDENTICAL — a space-form valued flag followed by a bare word — so no
        # parser separates them by position. THE COMMON READING WINS: both are
        # options. A literal title of that shape uses `--`. Without this arm a
        # future reader "fixes" the second case and silently breaks the first.
        rc, out, err = run(tasks.cmd_task,
                           ["add", "plain", "title", "--owner", "bob",
                            "--note", T1342_NOTE])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(rows[-1]["title"], "plain title")
        self.assertEqual(rows[-1]["owner"], "bob")

        rc, out, err = run(tasks.cmd_task,
                           ["add", "fix", "the", "--owner", "bypass",
                            "--note", T1342_NOTE])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(rows[-1]["title"], "fix the")
        self.assertEqual(rows[-1]["owner"], "bypass")

    def test_dashdash_files_the_literal_title_including_flag_words(self):
        # THE ESCAPE IS WHAT MAKES THE RULING LIVEABLE, so it is pinned. If
        # this breaks, the ambiguity above has no answer and the guard's
        # message is advising something that does not work.
        rc, out, err = run(tasks.cmd_task,
                           ["add", "--note", T1342_NOTE, "--",
                            "fix", "the", "--owner", "bypass"])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(rows[-1]["title"], "fix the --owner bypass")
        self.assertIsNone(rows[-1].get("owner"))

    def test_an_unknown_flag_word_refuses_and_names_the_escape(self):
        # The pre-existing UNKNOWN-flag behaviour, pinned because the guard
        # message and the escape it names are what the ruling relies on.
        # IT DOES NOT MODEL KNOWN FLAGS and an earlier comment said it did:
        # a known flag in a CLEAN TRAILING SUFFIX is deliberately
        # consumed as an option, which the table arms above pin directly.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        self.assertEqual(len(self.ledger()), 1, "control row did not file")
        rc, out, err = run(tasks.cmd_task,
                           ["add", "fix", "the", "--force", "bypass",
                            "--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r" % (rc, out))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("is not a title", err)
        self.assertIn("before the whole title", err)

    def test_mine_without_a_provable_identity_refuses_instead_of_filing(self):
        # kills: `owner = seats.own_name()` folding None into the unowned
        # default. The caller ASKED to own the row and cannot prove identity,
        # so filing something else under rc 0 answers a question nobody asked.
        # THREE states need three representations; today the third renders as
        # the second, and the row lands owner=None AND source=None — which
        # falsifies the code's own comment that provenance survives either way.
        # UNCONDITIONAL POSITIVE CONTROL on the same observable: the
        # ledger must be WRITABLE here, or "a refusal wrote nothing" is a
        # statement about the fixture rather than about the guard.
        rc0, _o0, e0 = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--note", T1342_NOTE])
        self.assertEqual(rc0, 0, e0)
        self.assertEqual(len(self.ledger()), 1, "control row did not file")
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, out, err = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--mine", "--note", T1342_NOTE])
        self.assertEqual(rc, 2, "rc=%s out=%r err=%r" % (rc, out, err))
        self.assertEqual(len(self.ledger()), 1, "a refusal must write nothing")
        self.assertIn("HELM_CHAT_NAME", err,
                      "the refusal must name the remedy, as `helm chat wait` does")

    def test_mine_with_a_declared_identity_still_owns_the_row(self):
        # THE MUST-STAY-QUIET HALF: the cure must not break the working case.
        # Without this, a cure that refuses --mine unconditionally passes the
        # arm above and breaks every legitimate caller.
        rc, out, err = run(tasks.cmd_task,
                           ["add", T1342_TITLE, "--mine", "--note", T1342_NOTE])
        self.assertEqual(rc, 0, err)
        rows = self.ledger()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["owner"], "seat-a")


if __name__ == "__main__":
    unittest.main()
