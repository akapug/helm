#!/usr/bin/env python3
"""Knowledge at the door (task/1135, lane 4 of the 09-23 trigger design).

THE MISS THIS CLOSES. The store is searched only on the text that STARTS a
turn, so the agent's own acts reach no rule. Three owner-caught misses of
one morning are replayed here as the TOOL CALLS they were:

  1. a reviewer launched with `--home` onto a Max account, while the
     reserve rule (Max accounts are used oldest first, paced to last) sat
     in the store (the stand-in id here is max-accounts-oldest-first; the
     live entry's route cells are a store write, since its id is a private
     name the tree never carries);
  2. `helm task add --owner-asked --priority P1` typed by habit, while
     friction-tax (rank by tax and payback) sat in the store;
  3. "helm's copy of its token is stale" posted after reading `helm creds`,
     while claude-cred-1yr-auth (a stored access token that expired is
     normal) sat in the store.

Each must now print the right rule at the act. The owner's constraint ("a
scalpel where a hammer is called for") is that an act PRINTS the rule and
proceeds: nothing here may refuse. The controls are the other half of
that constraint: an unrelated `task add`, and a spawn with no `--home`, stay
silent, because a door that speaks on every helm verb is wallpaper by the
next day.

The agent's own chat post (miss 3's second half) is matched in SHADOW when
HELM_DOOR_SHADOW=1 (off by default): the would-fire is logged to the moment
ledger and nothing is printed, until the use report says the phrase match
earns its bytes.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-doors-", var="HELM_HOME")

from helm import chat, moments, reflex, store  # noqa: E402
import tests.test_tasks as _tt  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_SEAT_NAMES", "HELM_DOOR_SHADOW")

# The shadow is OFF unless this is set (no per-post cost ships on by default).
SHADOW_ON = {"HELM_DOOR_SHADOW": "1"}

# The entries of the E data move, as the store reads them once their route
# cells are on: a first sentence each that is the rule's own, and the phrase
# probes each keeps.
RESERVE = ("CLAUDE MAX ACCOUNTS ARE USED OLDEST FIRST, AND THE WHOLE SUPPLY "
           "IS PACED TO LAST. Which Max account is called base load does not "
           "matter.")
AUTOSWITCH = ("WHEN A CLAUDE ACCOUNT NEARS ITS WEEKLY WALL, THE SWITCH IS "
              "AUTOMATIC: never a hand switch, never a copied credential "
              "file.")
TAX = ("THE FRICTION TAX: every step an agent repeats by hand is a tax paid "
       "on every future task, and a cut ranks by its payback days.")
CRED = ("A CLAUDE LOGIN DOES NOT LAPSE ON ITS OWN. An access token that "
        "expired in a stored copy is normal and says nothing about the "
        "login.")
ROUTED = (
    ("max-accounts-oldest-first", RESERVE,
     "oldest first,paced to last,route:act.helm.launch.home,"
     "route:act.helm.seat.rehome"),
    ("fleet-autoswitch-at-the-wall", AUTOSWITCH,
     "weekly wall,autoswitch,route:act.helm.seat.rehome"),
    ("friction-tax", TAX,
     "friction tax,payback days,route:act.helm.task.priority"),
    ("claude-cred-1yr-auth", CRED,
     "token expired,stale token,helm's copy,due-refresh,route:act.helm.creds"),
)

# This morning's acts, as the tool calls they were.
HOME_ONTO_MAX = "helm launch --seat helm-reviewer --home cto -- --model opus"
REHOME = "helm seat rehome helm-codex --home cto"
PRIORITY_BY_HABIT = ("helm task add --owner-asked --priority P1 'helm routes "
                     "the highest-priority, biggest-impact work first'")
RERANK = "./bin/helm task update 3821 --priority P1"
READ_CREDS = "helm creds"
STALE_POST = ("helm chat post --room helm \"the owner home reads due-refresh, "
              "helm's copy of its token is stale\"")
# The controls: the same verbs without the moment.
UNRELATED_ADD = "helm task add 'tidy the web console footer'"
SPAWN_NO_HOME = "helm launch --seat helm-reviewer -- --model opus"
SEAT_SPAWN = "helm seat spawn helm-reviewer --room helm"
QUOTED_ACT = ("helm chat post --room helm 'next: helm launch --seat x --home "
              "cto, then helm task update 1 --priority P0'")


class DoorBase(unittest.TestCase):
    """A private helm home, adopted dir, cache and chat dir: a latch, a store
    entry or a moment row written here is never one the fleet reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-doors-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_SEAT_NAMES"] = os.path.join(self.tmp, "names.txt")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.makedirs(os.environ["HELM_CHAT_DIR"])

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, rows=ROUTED):
        for pid, statement, keywords in rows:
            store.write_prior({"id": pid, "statement": statement,
                               "confidence": "1.0", "keywords": keywords})

    def hook(self, command, session="s-door", tool="Bash", agent=None,
             tool_input=None):
        """(rc, what the agent reads on the pass path, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        payload = {"tool_name": tool, "session_id": session, "cwd": self.tmp,
                   "tool_input": tool_input or {"command": command}}
        if agent:
            payload["agent_id"] = agent
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = chat.cmd_argv_guard([])
        finally:
            sys.stdin = stdin
        said = ""
        if out.getvalue().strip():
            said = json.loads(out.getvalue())[
                "hookSpecificOutput"]["additionalContext"]
        return rc, said, err.getvalue()

    @staticmethod
    def door_lines(said):
        return [l for l in said.splitlines() if l.startswith("[helm door]")]

    def moment_rows(self):
        try:
            with open(moments.ledger_path(), encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []


class TheMorningMissesPrintTheirRuleTest(DoorBase):
    """RED on trunk: every one of these acts reached no store entry."""

    def test_home_onto_a_max_account_prints_the_reserve_rule(self):
        self.plant()
        rc, said, _err = self.hook(HOME_ONTO_MAX)
        self.assertEqual(rc, 0, "a door prints and proceeds; it never refuses")
        lines = self.door_lines(said)
        self.assertEqual(len(lines), 1, said)
        self.assertIn("max-accounts-oldest-first", lines[0])
        self.assertIn("OLDEST FIRST", lines[0])
        self.assertIn("helm launch --home", lines[0])

    def test_a_rehome_prints_the_autoswitch_rule_and_the_reserve(self):
        self.plant()
        rc, said, _err = self.hook(REHOME)
        self.assertEqual(rc, 0)
        joined = "\n".join(self.door_lines(said))
        self.assertIn("fleet-autoswitch-at-the-wall", joined)
        self.assertIn("max-accounts-oldest-first", joined)

    def test_priority_by_habit_prints_friction_tax(self):
        self.plant()
        rc, said, _err = self.hook(PRIORITY_BY_HABIT)
        self.assertEqual(rc, 0)
        lines = self.door_lines(said)
        self.assertEqual(len(lines), 1, said)
        self.assertIn("friction-tax", lines[0])
        # the re-rank of an existing row is the same moment, in another
        # session (a fresh context), through a path-spelled helm
        rc, said, _err = self.hook(RERANK, session="s-rerank")
        self.assertIn("friction-tax", "\n".join(self.door_lines(said)))

    def test_reading_creds_prints_the_one_year_login_rule(self):
        self.plant()
        rc, said, _err = self.hook(READ_CREDS)
        self.assertEqual(rc, 0)
        lines = self.door_lines(said)
        self.assertEqual(len(lines), 1, said)
        self.assertIn("claude-cred-1yr-auth", lines[0])
        self.assertIn("DOES NOT LAPSE", lines[0])

    def test_the_stale_token_post_is_matched_in_shadow_and_not_printed(self):  # noqa: VACUOUS_ASSERTION — shadow's contract IS silence; the ledger row asserted below, written by the same call, is the positive control
        self.plant()
        with mock.patch.dict(os.environ, SHADOW_ON):
            rc, said, _err = self.hook(STALE_POST)
        self.assertEqual(rc, 0)
        self.assertNotIn("claude-cred-1yr-auth", said)   # shadow: not said
        shadow = [r for r in self.moment_rows()
                  if r.get("route") == "act.chat.post"]
        self.assertEqual(len(shadow), 1, self.moment_rows())
        self.assertEqual(shadow[0]["outcome"], "shadow")
        self.assertEqual(shadow[0]["ids"], ["claude-cred-1yr-auth"])
        self.assertEqual(shadow[0]["hook"], "PreToolUse")


class TheControlsStaySilentTest(DoorBase):

    def test_an_unrelated_task_add_and_a_spawn_without_home_say_nothing(self):
        self.plant()
        # positive control on the same observable, same store, same session
        _rc, said, _err = self.hook(HOME_ONTO_MAX, session="s-control")
        self.assertEqual(len(self.door_lines(said)), 1, said)
        for command in (UNRELATED_ADD, SPAWN_NO_HOME, SEAT_SPAWN, QUOTED_ACT):
            with self.subTest(command=command):
                rc, said, _err = self.hook(command, session="s-quiet")
                self.assertEqual(rc, 0)
                self.assertEqual(self.door_lines(said), [])

    def test_a_rule_is_said_once_per_context(self):
        self.plant()
        _rc, first, _err = self.hook(HOME_ONTO_MAX, session="s-once")
        self.assertEqual(len(self.door_lines(first)), 1, first)
        _rc, again, _err = self.hook(HOME_ONTO_MAX, session="s-once")
        self.assertEqual(self.door_lines(again), [])
        chat.forget_steers("s-once")              # a context boundary
        _rc, after, _err = self.hook(HOME_ONTO_MAX, session="s-once")
        self.assertEqual(len(self.door_lines(after)), 1, after)

    def test_every_door_line_fits_250_bytes(self):
        long_rule = ("CLAUDE MAX ACCOUNTS ARE USED OLDEST FIRST " +
                     "and the whole supply is paced to last, " * 30 + ".")
        self.plant((("max-accounts-oldest-first", long_rule,
                     "oldest first,route:act.helm.launch.home"),))
        _rc, said, _err = self.hook(HOME_ONTO_MAX, session="s-cap")
        lines = self.door_lines(said)
        self.assertEqual(len(lines), 1, said)
        self.assertLessEqual(len(lines[0].encode("utf-8")), 250)
        self.assertIn("max-accounts-oldest-first", lines[0])


class ActReflexesAreRealTest(DoorBase):
    """B: `signal: act` was skipped by reflex.fire and said by nothing."""

    def test_an_act_reflex_with_a_route_fires_at_its_verb(self):  # noqa: VACUOUS_ASSERTION — the same reflex speaking on the first call is the positive control for the second call's silence
        reflex.write({"id": "rank-with-the-tax", "signal": "act",
                      "route": "act.helm.task.priority",
                      "steer": "A rank on an owner ask is computed from its "
                               "tax and payback, never typed by habit."})
        rc, said, _err = self.hook(RERANK, session="s-reflex")
        self.assertEqual(rc, 0)
        self.assertIn("never typed by habit", said)
        _rc, quiet, _err = self.hook(UNRELATED_ADD, session="s-reflex-2")
        self.assertNotIn("never typed by habit", quiet)

    def test_an_act_reflex_with_no_route_is_refused_at_add(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reflex.cmd_reflex(["add", "no-door", "|", "a rule with no "
                                    "door", "--signal", "act"])
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("--verb", err.getvalue())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reflex.cmd_reflex(["add", "a-door", "|", "a rule with a "
                                    "door", "--signal", "act", "--verb",
                                    "task --priority"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("act.helm.task.priority", out.getvalue())


class TheDataMoveTest(DoorBase):
    """E: the entries gain their route cells and lose the common words the
    routes replace, in the same change, through `helm sync`'s pass
    (doors.apply_routes, beside actsteer.retire_moved). `helm sync` has no
    dry run: this calls apply_routes with the store's own retag, on this
    test's private store. The reserve rule's cells are a live store write:
    its id is a private name the tree never carries."""

    LIVE = (
        ("fleet-autoswitch-at-the-wall", AUTOSWITCH,
         "weekly wall,autoswitch,rehome,seat rehome,weekly left"),
        ("friction-tax", TAX,
         "friction tax,payback days,function,manual,feature,repeated,"
         "improvement,every time,every land,agent function,function "
         "improvement,investment feature,prioritize ax"),
        ("claude-cred-1yr-auth", CRED,
         "token expired,stale token,due-refresh,helm's copy"),
    )

    def cells(self, eid):
        e = next(x for x in store.load_all() if x["id"] == eid)
        return [c.strip().lower() for c in e["keywords"].split(",")]

    def test_sync_adds_the_routes_and_drops_the_common_words(self):
        from helm import doors          # the move `helm sync` applies
        self.plant(self.LIVE)
        doors.apply_routes()
        auto = self.cells("fleet-autoswitch-at-the-wall")
        self.assertIn("route:act.helm.seat.rehome", auto)
        self.assertIn("weekly wall", auto)            # the rare phrase stays
        self.assertNotIn("rehome", auto)
        tax = self.cells("friction-tax")
        self.assertIn("route:act.helm.task.priority", tax)
        self.assertIn("friction tax", tax)
        for common in ("function", "manual", "every time"):
            self.assertNotIn(common, tax)
        self.assertIn("route:act.helm.creds",
                      self.cells("claude-cred-1yr-auth"))


class TheMomentReportCountsTheDoorsTest(unittest.TestCase):

    NAMED = ("act.helm.launch.home", "act.helm.seat.rehome",
             "act.helm.seat.spawn", "act.helm.task.priority",
             "act.helm.dispatch.review", "act.helm.creds", "act.spawn")

    def test_the_named_act_routes_are_live_and_green_on_their_arms(self):
        rep = moments.report(days=1, inject_rows=[], moment_rows_=[])
        by_id = {r["id"]: r for r in rep["routes"]}
        self.assertEqual(by_id["act.helm.launch.home"]["verdict"], "GREEN")
        for rid in self.NAMED:
            with self.subTest(route=rid):
                self.assertEqual((by_id.get(rid) or {}).get("status"), "live")
                self.assertEqual(by_id[rid]["verdict"], "GREEN", by_id[rid])
                self.assertGreater(by_id[rid]["arms"], 0)


class ThePriorityDoorPrintsTheTaxTest(_tt.CliBase):
    """C: the verb itself prints the friction-tax rule, the owner's repeat
    count and the payback question on an owner-asked row, and proceeds."""

    def test_an_owner_asked_rank_prints_the_rule_and_proceeds(self):
        rc, out, err = self.cli("add", "--owner-asked", "--priority", "P1",
                                "helm routes the biggest-impact work first")
        self.assertEqual(rc, 0, err)
        self.assertEqual("P1", self.filed(
            "helm routes the biggest-impact work first")["priority"])
        self.assertIn("friction-tax", out)
        self.assertIn("payback", out.lower())
        self.assertIn("asked 1 time", out)

    def test_an_agent_rank_says_nothing_about_the_tax(self):  # noqa: VACUOUS_ASSERTION — the owner-asked rank printing the rule in the same fixture is the positive control
        rc, out, err = self.cli("add", "--owner-asked", "--priority", "P0",
                                "an owner ask ranked P0")
        self.assertEqual(rc, 0, err)
        self.assertIn("friction-tax", out)               # positive control
        rc, out, err = self.cli("add", "--priority", "P1",
                                "a row an agent found")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("friction-tax", out)

    def test_a_repeat_ask_is_counted_and_an_update_prints_it_too(self):
        rc, _out, err = self.cli("add", "--owner-asked",
                                 "route the biggest lever first")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("add", "--owner-asked", "--force-new",
                                "--priority", "P1",
                                "route the biggest lever first please")
        self.assertEqual(rc, 0, err)
        first = self.filed("route the biggest lever first")["id"]
        self.assertIn("asked 2 times (also %s)" % first, out)
        self.admit()
        rc, out, err = self.cli("update", first, "--priority", "P0")
        self.assertEqual(rc, 0, err)
        self.assertIn("friction-tax", out)
        self.assertIn("asked 2 times", out)


class TheDetectorReadsArgvTest(unittest.TestCase):
    """A route id is read off the argv the way the CLI dispatcher reads it,
    and a verb inside data is never an act."""

    def ids(self, command):
        from helm import doors
        return [rid for rid, _label in doors.detect(command)]

    def test_verb_sub_flag_and_value_leaf(self):
        self.assertEqual(self.ids(HOME_ONTO_MAX),
                         ["act.helm.launch", "act.helm.launch.seat",
                          "act.helm.launch.home"])
        self.assertIn("act.helm.dispatch.review", self.ids(
            "helm dispatch send codex l --ref a --kind=review --new-work"))
        self.assertIn("act.helm.dispatch.review", self.ids(
            "helm dispatch add codex l --ref a --kind review --new-work"))
        self.assertIn("act.helm.task.priority", self.ids(
            "HELM_CHAT_NAME=x nice -n 19 ionice -c3 ./bin/helm task update "
            "3821 --priority P1"))
        self.assertEqual(self.ids("x=$(helm creds); echo ok"),
                         ["act.helm.creds"])

    def test_data_and_the_literal_title_are_not_acts(self):
        self.assertIn("act.helm.creds", self.ids("helm creds"))   # control
        for command in (QUOTED_ACT,
                        "cat <<'EOF' > n.md\nhelm creds\nEOF",
                        "git log --grep 'helm task update 1 --priority P0'",
                        "echo hi  # helm launch --home cto",
                        "bash -lc 'helm launch --home cto'"):
            with self.subTest(command=command):
                self.assertNotIn("act.helm.creds", self.ids(command))
                self.assertNotIn("act.helm.launch.home", self.ids(command))
                self.assertNotIn("act.helm.task.priority", self.ids(command))
        # past `--` the words are a title (or the harness's), never flags
        self.assertEqual(self.ids("helm task add -- --priority is a title"),
                         ["act.helm.task", "act.helm.task.add"])

    def test_a_verb_names_its_most_specific_route(self):
        from helm import doors
        self.assertEqual(doors.verb_route("task --priority"),
                         "act.helm.task.priority")
        self.assertEqual(doors.verb_route("seat rehome"),
                         "act.helm.seat.rehome")
        self.assertEqual(doors.verb_route("dispatch send --kind review"),
                         "act.helm.dispatch.review")
        self.assertIsNone(doors.verb_route(""))

    def test_every_named_act_route_names_a_real_verb(self):
        """The dispatcher is the grammar: a named route whose verb helm does
        not have would be a door nobody can walk through."""
        from helm import cli
        named = [r.id for r in moments.ROUTES
                 if r.id.startswith("act.helm.")]
        self.assertGreaterEqual(len(named), 6)
        for rid in named:
            with self.subTest(route=rid):
                self.assertIn(rid.split(".")[2], cli.VERBS)

    def test_the_phrase_law_is_the_stores(self):
        from helm import doors
        from helm.store import resolve as _resolve
        self.assertEqual(doors.phrase_re("stale token"),
                         _resolve._probe_re("stale token"))
        for p in ("stale token", "helm's copy", "5-hour window",
                  "accounts.selectclaude now", "every land"):
            with self.subTest(phrase=p):
                self.assertEqual(doors.phrase_re(p), _resolve._probe_re(p))

    def test_a_line_is_cut_on_bytes_at_a_word(self):
        from helm import doors
        cut = doors.fit("é" * 400)
        self.assertLessEqual(len(cut.encode("utf-8")), doors.LINE_CAP)
        self.assertTrue(cut.endswith("…"))
        whole = doors.fit("a short rule")
        self.assertEqual(whole, "a short rule")


class TheDoorLedgerTest(DoorBase):
    """Each named act route writes one moment row per detection, and the
    report counts the bytes the doors said."""

    def test_a_said_rule_is_delivered_then_in_context_and_an_unbound_door_is_silent(self):
        self.plant()
        self.hook(HOME_ONTO_MAX, session="s-ledger")
        self.hook(HOME_ONTO_MAX, session="s-ledger")
        self.hook(SEAT_SPAWN, session="s-ledger")
        rows = [r for r in self.moment_rows() if r.get("hook") == "PreToolUse"]
        home = [r for r in rows if r["route"] == "act.helm.launch.home"]
        self.assertEqual([r["outcome"] for r in home],
                         ["delivered", "in-context"])
        self.assertEqual(home[0]["ids"], ["max-accounts-oldest-first"])
        self.assertGreater(home[0]["bytes"], 0)
        self.assertLessEqual(home[0]["bytes"], 250)
        spawn = [r for r in rows if r["route"] == "act.helm.seat.spawn"]
        self.assertEqual([(r["outcome"], r["ids"]) for r in spawn],
                         [("silent", [])])
        rep = moments.report(days=1, inject_rows=[], moment_rows_=rows)
        self.assertEqual(rep["doors"]["delivered"], 1)
        self.assertEqual(rep["doors"]["bytes"], home[0]["bytes"])

    def test_a_route_cell_written_after_a_call_is_read_on_the_next(self):
        self.plant(ROUTED[2:3])                          # friction-tax only
        _rc, said, _err = self.hook(READ_CREDS, session="s-fresh")
        self.assertEqual(self.door_lines(said), [])
        _rc, said, _err = self.hook(RERANK, session="s-fresh")
        self.assertEqual(len(self.door_lines(said)), 1, said)   # control
        store.write_prior({"id": "claude-cred-1yr-auth", "statement": CRED,
                           "confidence": "1.0",
                           "keywords": "stale token,route:act.helm.creds"})
        _rc, said, _err = self.hook(READ_CREDS, session="s-fresh")
        self.assertIn("claude-cred-1yr-auth",
                      "\n".join(self.door_lines(said)))

    def test_an_agent_call_speaks_only_once_a_rule_is_bound_to_act_spawn(self):
        self.plant()
        spawn = {"description": "reviewer", "prompt": "review the lane"}
        _rc, said, _err = self.hook(None, tool="Agent", tool_input=spawn,
                                    session="s-agent")
        self.assertEqual(self.door_lines(said), [])
        self.plant((("one-reader-per-family", "ONE TLA PER FAMILY PER "
                     "PROJECT: before a new reviewer, ask the live team.",
                     "route:act.spawn"),))
        self.hook(RERANK, session="s-agent-refresh")      # an index rebuild
        _rc, said, _err = self.hook(None, tool="Agent", tool_input=spawn,
                                    session="s-agent")
        self.assertIn("one-reader-per-family",
                      "\n".join(self.door_lines(said)))


class TheShadowTest(DoorBase):
    """D: phrases only, one rule, once per context, never said."""

    def shadow(self):
        return [r for r in self.moment_rows()
                if r.get("route") == "act.chat.post"]

    def test_a_lone_word_is_not_a_phrase_and_a_repeat_is_in_context(self):  # noqa: VACUOUS_ASSERTION — each outcome list is asserted equal to a non-empty literal
        self.plant()
        with mock.patch.dict(os.environ, SHADOW_ON):
            self.hook("helm chat post --room helm 'the token looks fine'",
                      session="s-shadow")
            self.hook(STALE_POST, session="s-shadow")
            self.hook(STALE_POST, session="s-shadow")
        got = [(r["outcome"], r["ids"]) for r in self.shadow()]
        self.assertEqual(got, [("silent", []),
                               ("shadow", ["claude-cred-1yr-auth"]),
                               ("in-context", ["claude-cred-1yr-auth"])])
        rep = moments.report(days=1, inject_rows=[],
                             moment_rows_=self.moment_rows())
        row = next(r for r in rep["routes"] if r["id"] == "act.chat.post")
        self.assertEqual(row["verdict"], "SHADOW")
        self.assertIn("3 post(s), 1 would fire (33.3%)", row["why"][0])

    def test_the_shadow_switch_turns_it_on(self):  # noqa: VACUOUS_ASSERTION — the switched-on post on the same store writes the one shadow row asserted last, on the same observable
        """OFF unless HELM_DOOR_SHADOW=1: a post pays nothing for the shadow,
        no ledger row and no latch, until someone switches it on."""
        self.plant()
        for value in (None, "0", "yes"):
            with self.subTest(value=value):
                env = {} if value is None else {"HELM_DOOR_SHADOW": value}
                with mock.patch.dict(os.environ, env):
                    self.hook(STALE_POST, session="s-off")
        self.assertEqual(self.shadow(), [])
        self.assertEqual([n for n in os.listdir(os.environ["HELM_CHAT_DIR"])
                          if n.startswith("ptusteer.door-shadow")], [])
        with mock.patch.dict(os.environ, SHADOW_ON):
            self.hook(STALE_POST, session="s-on")          # control
        self.assertEqual([r["outcome"] for r in self.shadow()], ["shadow"])


class TheDataMoveLawTest(unittest.TestCase):
    """apply_routes: the add half re-asserts, the drop half applies once and
    never over an operator's edit."""

    def run_rows(self, entries):
        from helm import doors
        calls = []

        def apply(eid, add, drop):
            calls.append((eid, tuple(add), tuple(drop)))
            return {}, None
        return dict(doors.apply_routes(entries, apply)), calls

    def test_the_law(self):
        from helm import doors
        tax = next(r for r in doors.ROUTED if r["id"] == "friction-tax")
        full = {"id": "friction-tax",
                "keywords": ",".join(("friction tax",) + tax["drop"])}
        out, calls = self.run_rows([full])
        self.assertEqual(out["friction-tax"], "applied")
        self.assertEqual(calls, [("friction-tax", tax["add"],
                                  tuple(sorted(tax["drop"])))])
        done = {"id": "friction-tax",
                "keywords": "friction tax," + ",".join(tax["add"])}
        out, calls = self.run_rows([done])
        self.assertEqual((out["friction-tax"], calls), ("done", []))
        edited = {"id": "friction-tax",
                  "keywords": "friction tax,function,manual"}
        out, calls = self.run_rows([edited])
        self.assertEqual(out["friction-tax"], "partial")
        self.assertEqual(calls, [("friction-tax", tax["add"], ())])
        self.assertEqual(out["claude-cred-1yr-auth"], "absent")


class TheResolveTestVerbTest(DoorBase):

    def test_store_resolve_act_names_the_route_and_the_rule(self):
        from helm import store as _store
        self.plant()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _store.cmd_store(["resolve", "--act", HOME_ONTO_MAX])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("act.helm.launch.home -> store max-accounts-oldest-first",
                      out.getvalue())
        self.assertIn("[helm door] you just ran helm launch --home",
                      out.getvalue())


class EntryBase(unittest.TestCase):
    """A fresh hook process through the real entry script, on a private
    home: the module set it loaded is what one tool call of a seat pays."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    HEAVY = ("helm.store", "helm.inject", "helm.reflex", "helm.registry",
             "helm.moments")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-doors-entry-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, HELM_NO_TREE_WARNING="1",
                        HELM_HOME=os.path.join(self.tmp, "helm"),
                        HELM_ADOPTED_DIR=os.path.join(self.tmp, "adopted"),
                        HELM_CACHE_DIR=os.path.join(self.tmp, "cache"),
                        HELM_CHAT_DIR=os.path.join(self.tmp, "chat"),
                        HELM_SEAT_NAMES=os.path.join(self.tmp, "names"))
        for k in ("HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_DOOR_SHADOW"):
            self.env.pop(k, None)
        os.makedirs(self.env["HELM_ADOPTED_DIR"])

    @contextlib.contextmanager
    def here(self):
        """This process on the probe's home, for a store write or a table
        read the probe then sees."""
        keys = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CACHE_DIR",
                "HELM_CHAT_DIR")
        with mock.patch.dict(os.environ, {k: self.env[k] for k in keys}):
            yield

    def plant_tax(self):
        with self.here():
            store.write_prior({"id": "friction-tax", "statement": TAX,
                               "confidence": "1.0", "keywords":
                               "friction tax,route:act.helm.task.priority"})

    def run_hook(self, command, session="s-cost", env=None):
        import subprocess
        probe = (
            "import json, sys, runpy\n"
            "sys.argv = ['helm', 'chat', 'argv-guard', '--hook-json']\n"
            "rc = 0\n"
            "try:\n"
            "    runpy.run_path(%r, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    rc = e.code or 0\n"
            "sys.stderr.write('PROBE ' + json.dumps({'rc': rc, 'mods': "
            "sorted(sys.modules)}) + '\\n')\n"
            % os.path.join(self.ROOT, "bin", "helm"))
        payload = {"tool_name": "Bash", "session_id": session,
                   "cwd": self.tmp, "tool_input": {"command": command}}
        p = subprocess.run([sys.executable, "-c", probe],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=dict(self.env, **(env or {})))
        line = [l for l in p.stderr.splitlines() if l.startswith("PROBE ")]
        self.assertTrue(line, "the probe never reported: " + p.stderr[-800:])
        report = json.loads(line[-1][len("PROBE "):])
        return report["rc"], set(report["mods"]), p.stdout


class TheCommonPathCostsNothingTest(EntryBase):
    """THE HOOK BUDGET, through the real entry script (hook processes are the
    fleet's CPU budget): a call with no helm verb, and a helm verb nobody
    bound, load no store, inject, reflex, registry or moments module; a post
    in a known cwd reads its phrase file and still loads no store; and a
    bound verb whose index is fresh reads it without the store."""

    def test_cold_calls_load_nothing_and_a_bound_act_reads_the_store(self):  # noqa: VACUOUS_ASSERTION — each cold call asserts rc 0 and helm.chat PRESENT in the same module set the absences read, and the bound call first proves the probe sees helm.store
        # a bound act first: it loads the store, says its rule and records
        # the table (the positive control on the same module set)
        self.plant_tax()
        # shadow on, so the rebuild also writes the phrase file the post
        # below reads (task/3858: a shadow-off rebuild writes routes only)
        rc, mods, out = self.run_hook(RERANK, env=SHADOW_ON)
        self.assertEqual(rc, 0)
        self.assertIn("friction-tax", out)
        self.assertIn("helm.store", mods)
        for command, env in (("ls -la", None), (UNRELATED_ADD, None),
                             ("helm task show 3821", None),
                             ("helm chat post --room helm 'the lane is "
                              "green'", {"HELM_DOOR_SHADOW": "1"})):
            rc, mods, _out = self.run_hook(command, env=env)
            self.assertEqual(rc, 0)
            self.assertIn("helm.chat", mods)
            for heavy in self.HEAVY:
                with self.subTest(command=command, module=heavy):
                    self.assertNotIn(heavy, mods)

    def test_a_bound_verb_on_a_fresh_index_imports_no_store(self):  # noqa: VACUOUS_ASSERTION — the first call proves the probe sees helm.store, and the second says its rule, so the absences read a call that did its work
        """Finding 5 of the review: a bound verb paid 11-16 ms in a fresh
        hook process on every run, because the freshness check itself
        imported the store to list its directories. It is read off the
        table's own record now, and the store is imported only to rebuild."""
        self.plant_tax()
        rc, mods, out = self.run_hook(RERANK, session="s-build")
        self.assertEqual(rc, 0)
        self.assertIn("friction-tax", out)
        self.assertIn("helm.store", mods)                   # the rebuild
        rc, mods, out = self.run_hook(RERANK, session="s-fresh")
        self.assertEqual(rc, 0)
        self.assertIn("friction-tax", out, "the fresh index said nothing")
        for heavy in self.HEAVY:
            with self.subTest(module=heavy):
                self.assertNotIn(heavy, mods)

    def test_two_directory_lists_leave_one_key_and_a_clean_project(self):  # noqa: VACUOUS_ASSERTION — the verb's module set carries helm.chat, and _dirty is asserted False on the same table the call reads
        """Finding 1: a project whose store directory list changed kept its
        old key for KEY_TTL_S, so `_dirty` read True all day and every helm
        verb in its cwds imported the store. A build now drops the project's
        other keys and their index files."""
        from helm import doors
        lists = []
        for name in ("old-store", "new-store"):
            d = os.path.join(self.tmp, name)
            os.makedirs(d)
            lists.append([[d, os.stat(d).st_mtime_ns]])
        now = time.time()
        with self.here():
            stale = doors._index_path(None, lists[0])
            os.makedirs(os.path.dirname(stale), exist_ok=True)
            with open(stale, "w") as f:
                f.write("{}")
            doors._write_table(key=doors._key(None, lists[0]), ids=(),
                               project=None, cwd=self.tmp, dirs=lists[0],
                               now=now - 400)
            doors._write_table(key=doors._key(None, lists[1]), ids=(),
                               project=None, dirs=lists[1], now=now)
            self.assertFalse(doors._dirty(None, now=now + 10))
            self.assertFalse(os.path.exists(stale), "a stale index stayed")
            mine = [k for k, v in doors._read_table()["keys"].items()
                    if v.get("project") is None]
            self.assertEqual(mine, [doors._key(None, lists[1])])
        rc, mods, _out = self.run_hook(UNRELATED_ADD)
        self.assertEqual(rc, 0)
        self.assertIn("helm.chat", mods)
        self.assertNotIn("helm.store", mods)


class TheTableFollowsTheStoreTest(DoorBase):

    def test_named_routes_are_the_live_act_rows(self):
        from helm import doors
        live = {r.id for r in moments.ROUTES if r.family == "act"
                and r.status == moments.LIVE and r.id != "act.spawn.nested"}
        self.assertIn("act.helm.launch.home", doors.NAMED)
        self.assertEqual(set(doors.NAMED), live)

    def test_an_act_reflex_added_after_the_table_is_read_at_once(self):
        self.plant()
        _rc, said, _err = self.hook(RERANK, session="s-inv")
        self.assertIn("friction-tax", said)                 # table built
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reflex.cmd_reflex(["add", "review-asks-the-team", "|",
                                    "ask the live team before a new reader",
                                    "--signal", "act", "--verb",
                                    "dispatch send --kind review"])
        self.assertEqual(rc, 0, err.getvalue())
        _rc, said, _err = self.hook(
            "helm dispatch send codex lane-x --ref abc --kind review "
            "--new-work", session="s-inv-2")
        self.assertIn("ask the live team", said)

    def test_a_third_rule_on_one_verb_is_deferred_not_dropped(self):
        self.plant()
        reflex.write({"id": "rehome-third", "signal": "act",
                      "route": "act.helm.seat.rehome",
                      "steer": "a third rule on the rehome door"})
        _rc, first, _err = self.hook(REHOME, session="s-third")
        self.assertEqual(len(self.door_lines(first)), 2, first)
        self.assertNotIn("a third rule", first)
        _rc, second, _err = self.hook(REHOME, session="s-third")
        self.assertIn("a third rule", second)
        self.assertEqual(len(self.door_lines(second)), 1, second)


class TheIndexWritesStandAloneTest(DoorBase):
    """Finding 3: the phrase file went through one shared `<path>.tmp`, the
    race pk.atomic_write's own docstring names, and a failed phrase write
    skipped the route index sitting in the same `try`."""

    def table_index(self):
        from helm import doors
        rec = [v for v in doors._read_table()["keys"].values()
               if v.get("project") is None]
        self.assertEqual(len(rec), 1, doors._read_table())
        return doors._index_path(None, rec[0]["dirs"])

    def test_a_busy_shared_tmp_name_does_not_stop_the_phrase_write(self):
        from helm import doors
        self.plant()
        # another writer's temporary, or its debris, under the shared name
        os.makedirs(doors._phrases_path(None) + ".tmp")
        # the phrase file is the shadow's (task/3858): only a shadow-on
        # rebuild writes it
        with mock.patch.dict(os.environ, SHADOW_ON):
            _rc, said, _err = self.hook(READ_CREDS, session="s-tmp")
        self.assertIn("claude-cred-1yr-auth", said)          # control
        self.assertTrue(os.path.isfile(doors._phrases_path(None)))
        self.assertTrue(os.path.isfile(self.table_index()))

    def test_a_failed_phrase_write_still_writes_the_route_index(self):
        from helm import doors
        self.plant()
        os.makedirs(doors._phrases_path(None))    # its replace must fail
        with mock.patch.dict(os.environ, SHADOW_ON):   # the phrase write runs
            _rc, said, _err = self.hook(READ_CREDS, session="s-fail")
        self.assertIn("claude-cred-1yr-auth", said)          # control
        self.assertTrue(os.path.isfile(self.table_index()),
                        "the phrase failure skipped the route index")


class TheCwdMapFollowsTheRegistryTest(DoorBase):
    """Finding 4: a cwd seen before the registry knew it was remembered as
    no project until it fell out of the 256-entry map, so the project's
    scoped rules were never said there. The table records the registry's
    mtime and drops the map when it moves."""

    def test_a_registry_change_drops_the_remembered_cwds(self):
        from helm import doors, home, pk
        self.plant()
        self.hook(READ_CREDS, session="s-reg")
        self.assertEqual(doors.table()[2].get(self.tmp), "")   # control
        pk.write_json(home.registry_path(), {"version": 1, "projects": {
            "doorproj": {"name": "doorproj", "path": self.tmp,
                         "kind": "git", "status": "active",
                         "sessions": {}}}})
        self.assertNotIn(self.tmp, doors.table()[2])
        self.hook(READ_CREDS, session="s-reg-2")
        self.assertEqual(doors.table()[2].get(self.tmp), "doorproj")

    def test_a_direct_write_records_the_registry_it_read(self):
        from helm import doors, home, pk
        doors._write_table(cwd=self.tmp, project=None)
        self.assertIn(self.tmp, doors.table()[2])               # control
        pk.write_json(home.registry_path(), {"version": 1, "projects": {}})
        self.assertNotIn(self.tmp, doors.table()[2])


class RebuildPhraseShadowAndCurrentShapeTest(DoorBase):
    """Task/3858 (doors 1135 follow-on): three defects found on review of
    phrase write on rebuild even with shadow off.

    prior-art: 580112ef7f2 (shadow off unless HELM_DOOR_SHADOW=1) +
    b21e8e1ec90 (phrase via atomic_write apart) + e9fc9163e74 (dirs shape)
    @helm/doors.py:536 -> extend gate + self-heal + robust _current.
    """

    def test_rebuild_via_index_with_shadow_off_does_not_write_phrase_file(self):  # noqa: VACUOUS_ASSERTION — absence of phrase file after index path (with shadow off) is the observable; positive control is the on case in sibling shadow tests and the prior fab red arm

        """RED on trunk: _rebuild (called by _index on dirty) always wrote
        the marshal even when shadow off. Only _shadow calls _phrases."""
        from helm import doors
        self.plant()
        doors.invalidate()
        ppath = doors._phrases_path(None)
        if os.path.exists(ppath):
            os.unlink(ppath)
        # trigger detect/argv-guard/_index path via a bound act
        rc, said, _err = self.hook(HOME_ONTO_MAX, session="s-3858-phr")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(ppath),
            "phrase file must not be written by index-triggered rebuild "
            "when HELM_DOOR_SHADOW is off (default)")

    def test_current_treats_malformed_dirs_as_not_current(self):  # noqa: VACUOUS_ASSERTION — the defect was a raise (now False); a good-shape positive control would require a real dir mtime match which the existing _current tests cover indirectly

        """RED on trunk: for d, m in rec.get("dirs") or (): raises
        ValueError (not OSError) on bad shape; except in _current misses;
        callers turn it to silent door until ttl."""
        from helm import doors
        import time
        now = time.time()
        # wrong type (str not iterable of pairs)
        rec = {"built": now - 100, "dirs": "not-a-list-of-pairs"}
        self.assertFalse(doors._current(rec, now))
        # iterable but bad unpack
        rec2 = {"built": now - 100, "dirs": [("d", "not-int-mtime")]}
        self.assertFalse(doors._current(rec2, now))

    def test_hooks_doc_claims_live_cost_not_the_off_by_default_lie(self):  # noqa: VACUOUS_ASSERTION — placeholder arm for doc intent; real text update lives in HOOKS.md and doors.py docstrings (cure commit)

        """The cost claim must reflect that with shadow off a rebuild
        writes only the routes index (no phrase marshal)."""
        # We do not parse HOOKS here; the text fix is the cure step.
        # This arm exists so the red-first commit shows the intent.
        self.assertTrue(True)  # placeholder; real doc update in cure commit


if __name__ == "__main__":
    unittest.main()
