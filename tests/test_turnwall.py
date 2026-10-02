#!/usr/bin/env python3
"""helm moves work off a credential-exhausted seat by itself (task/3587).

Measured on the fleet: grok's last turn ended on "API Error: 402 Grok Build
usage balance exhausted" while proxywatch read its upstream HEALTHY and burn
read GREY, so the dispatch door admitted a new build row to it; kimi, on a
quota wall, still owned six tasks, and nothing moved work off either. Three
legs, pinned here:

  (1) a billing or credential refusal on a seat's OWN last turn is typed as a
      MONEY or REACH wall, and burn reads the family RED; an ordinary 429
      rate limit is not a wall, and a later successful turn reads live again;
  (2) the send door refuses a seat whose last turn ended on one, naming a
      live seat instead, and `--force --reason` still passes;
  (3) the dark-seat mover on the idle-dispatch tick moves each unstarted row
      and task of a family confirmed dark past the grace to a live seat,
      dry-run by default, never twice, and never on an unread fact.

Real rows through the real door (`DispatchBase`'s ledger, temp home and
fixture repo). The doubles are the proxy and burn states (the snapshot
`cached_flags` answers), the roster (the usability join) and the reviewer
ladder where an arm says so.

THE MODULES ARE IMPORTED, NEVER THEIR NAMES, so discovery does not run the
dispatch suite twice (see tests/test_dispatch_project_light.py)."""
import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import burnflags, dispatches, pk, seats, tasks
from tests import test_dispatches as td
from tests import test_seat_hold as tsh

GROK_402 = "API Error: 402 Grok Build usage balance exhausted"


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def err_turn(at, text=GROK_402, flagged=True, model="<synthetic>"):
    """One transcript line in the shape Claude Code records a failed request:
    its own assistant record, the synthetic model, text "API Error ..."."""
    rec = {"type": "assistant", "timestamp": _iso(at),
           "message": {"model": model, "role": "assistant",
                       "content": [{"type": "text", "text": text}]}}
    if flagged:
        rec["isApiErrorMessage"] = True
    return json.dumps(rec)


def ok_turn(at, text="done"):
    return json.dumps({"type": "assistant", "timestamp": _iso(at),
                       "message": {"model": "grok-4", "role": "assistant",
                                   "content": [{"type": "text",
                                                "text": text}]}})


def turnwall():
    from helm import turnwall as tw
    return tw


class ARefusalIsTypedByItsWordingTest(unittest.TestCase):
    """(1) The classifier: MONEY, REACH, or nothing at all."""

    def typed(self, text):
        got = turnwall().classify(text)
        return (got["axis"], got["code"]) if got else None

    def test_402_429_quota_and_403_verify_are_walls(self):
        self.assertEqual(self.typed(" 402 Grok Build usage balance exhausted"),
                         ("money", 402))
        self.assertEqual(self.typed(
            ' 429 {"error":{"message":"You exceeded your current quota, '
            'please check your plan and billing details.","type":'
            '"insufficient_quota"}}'), ("money", 429))
        self.assertEqual(self.typed(
            ' 403 {"error":{"message":"Please verify your account to '
            'continue using this model"}}'), ("reach", 403))
        # NO STATUS, THE WORDING ALONE: a pane or proxy that drops the code.
        self.assertEqual(self.typed(": usage balance exhausted"),
                         ("money", None))

    def test_an_ordinary_429_rate_limit_is_NOT_a_wall(self):
        """The discriminator: exhaustion wording AND no short window. A
        per-minute limit, an RPM/TPM budget or a retry-after clause clears by
        itself, whatever else the refusal says."""
        for text in (
                ' 429 {"error":{"message":"Rate limit reached for requests '
                'per min (RPM): Limit 3. Please try again in 20s."}}',
                " 429 Too Many Requests",
                ' 429 {"error":{"message":"Quota exceeded for quota metric '
                '\'Generate Content API requests per minute\'"}}',
                " 429 usage quota exhausted, retry after 30s",
                ' 429 {"type":"rate_limit_error","message":"This request '
                'would exceed your rate limit of 40,000 input tokens per '
                'minute"}'):
            with self.subTest(text=text):
                self.assertIsNone(self.typed(text))

    def test_request_server_and_plain_403_errors_are_not_walls(self):
        for text in (" 400 prompt is too long", " 500 internal error",
                     " 403 Forbidden", " 404 model not found",
                     " 503 overloaded"):
            with self.subTest(text=text):
                self.assertIsNone(self.typed(text))


class TheSeatsOwnLastTurnDecidesTest(unittest.TestCase):
    """(1) Position decides: only the NEWEST main-chain turn is read."""

    def test_a_turn_ending_on_402_is_a_money_wall(self):
        now = time.time()
        state, typed, at = turnwall().last_turn(
            [ok_turn(now - 300), err_turn(now - 60)])
        self.assertEqual(state, "WALL")
        self.assertEqual(typed["axis"], "money")
        self.assertAlmostEqual(at, now - 60, delta=1)

    def test_a_later_successful_turn_reads_live_again(self):
        now = time.time()
        state, typed, _at = turnwall().last_turn(
            [err_turn(now - 300), ok_turn(now - 60)])
        self.assertEqual((state, typed), ("CLEAN", None))

    def test_a_reply_QUOTING_the_error_is_not_one(self):
        """A real model's reply that quotes the refusal (a seat discussing
        this very module) is a successful turn, not a wall."""
        now = time.time()
        state, _typed, _at = turnwall().last_turn(
            [err_turn(now - 60, text=GROK_402 + " is what grok said",
                      flagged=False, model="grok-4")])
        self.assertEqual(state, "CLEAN")

    def test_a_sidechain_error_does_not_speak_for_the_main_chain(self):
        now = time.time()
        side = json.loads(err_turn(now - 10))
        side["isSidechain"] = True
        state, _typed, _at = turnwall().last_turn(
            [ok_turn(now - 60), json.dumps(side)])
        self.assertEqual(state, "CLEAN")

    def test_an_untyped_error_says_nothing_either_way(self):
        now = time.time()
        state, typed, _at = turnwall().last_turn(
            [ok_turn(now - 300), err_turn(now - 60,
                                          text="API Error: 500 internal")])
        self.assertEqual((state, typed), (None, None))


class BurnReadsTheWallRedTest(unittest.TestCase):
    """(1) The fold: a standing turn wall is RED on its own axis."""

    def walls(self, axis, at=None):
        return {"grok": {axis: {"seat": "grok", "family": "grok",
                                "state": "WALL", "axis": axis,
                                "code": 402 if axis == "money" else 403,
                                "label": "the label",
                                "at": at or time.time() - 60}}}

    def test_a_money_turn_wall_reads_the_family_RED(self):
        grey = burnflags.fold({})["families"]["grok"]
        self.assertNotEqual(grey["colour"], burnflags.RED)
        flag = burnflags.fold({"turn_walls": self.walls("money")})[
            "families"]["grok"]
        self.assertEqual(flag["axes"]["money"], burnflags.RED)
        self.assertEqual(flag["colour"], burnflags.RED)
        self.assertEqual(flag["cause_id"], "money:turn-wall")
        self.assertIn("seat grok", flag["cause"])

    def test_a_verify_account_turn_wall_reads_REACH_red(self):
        flag = burnflags.fold({"turn_walls": self.walls("reach")})[
            "families"]["grok"]
        self.assertEqual(flag["axes"]["reach"], burnflags.RED)
        self.assertEqual(flag["colour"], burnflags.RED)

    def test_a_healthy_upstream_record_does_not_clear_it(self):
        """The measured case: proxywatch read grok's upstream HEALTHY."""
        up = {"grok": {"state": "HEALTHY", "dark": False,
                       "since": pk.now_ts()}}
        flag = burnflags.fold({"upstream": up,
                               "turn_walls": self.walls("money")})[
            "families"]["grok"]
        self.assertEqual(flag["axes"]["money"], burnflags.RED)


class TheDoorAndTheFoldReadTheTranscriptTest(tsh.BrokenSeatDoorBase):
    """(1)+(2) Planted transcripts, read through the real readers."""

    def test_read_inputs_carries_the_standing_wall_and_drops_a_cured_one(self):
        now = time.time()
        from helm import autocompact
        tw = turnwall()
        self.transcript([ok_turn(now - 300), err_turn(now - 120),
                         err_turn(now - 60)])
        # ONE SEAT, HOWEVER MANY WALLED TURNS, never walls its family
        self.assertNotIn("grok", burnflags.read_inputs(now=now).get(
            "turn_walls") or {})
        # TWO DISTINCT SEATS do: grok-2 reads the same wall on its own turn
        real = tw.seat_reading

        def reading(name, lines=None, now=None):
            got, why = real("grok", lines=lines, now=now)
            return (dict(got, seat=name) if got else got), why
        with mock.patch.object(autocompact, "proxy_seats",
                               return_value=["grok", "grok-2"]), \
                mock.patch.object(tw, "seat_reading", reading):
            inputs = burnflags.read_inputs(now=now)
            walls = inputs.get("turn_walls") or {}
            self.assertEqual(walls["grok"]["money"]["seat"], "grok")
            self.assertEqual(burnflags.fold(inputs, now=now)[
                "families"]["grok"]["axes"]["money"], burnflags.RED)
        # A SUCCESSFUL TURN AFTER IT: the seat recovered.
        self.transcript([err_turn(now - 300), ok_turn(now - 60)])
        self.assertNotIn("grok", burnflags.read_inputs(now=now).get(
            "turn_walls") or {})

    def test_a_stale_wall_says_nothing_and_its_age_is_a_variable(self):
        now = time.time()
        self.transcript([err_turn(now - 7 * 3600)])
        self.assertIsNone(turnwall().seat_wall("grok", now=now))
        with mock.patch.dict(os.environ,
                             {"HELM_TURNWALL_MAX_AGE_S": str(8 * 3600)}):
            self.assertIsNotNone(turnwall().seat_wall("grok", now=now))

    def test_the_door_refuses_a_walled_seat_and_names_a_live_one(self):
        now = time.time()
        self.transcript([ok_turn(now - 300), err_turn(now - 60)])
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("TURN-WALL since %s" % pk.epoch_ts(now - 60), why)
        self.assertIn("MONEY", why)
        self.assertIn("Use @kimi instead", why)
        self.assertIn("--force --reason R", why)

    def test_a_transient_429_and_a_recovered_seat_are_admitted(self):
        now = time.time()
        self.transcript([ok_turn(now - 300), err_turn(
            now - 60, text="API Error: 429 Rate limit reached for requests "
                           "per min (RPM). Please try again in 20s.")])
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.transcript([err_turn(now - 300), ok_turn(now - 60)])
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)

    def test_force_with_a_reason_passes_and_is_recorded(self):
        now = time.time()
        self.transcript([err_turn(now - 60)])
        rc, _out, err = self.cli("--force")
        self.assertEqual(rc, 2)
        self.assertIn("--force past TURN-WALL on grok needs --reason", err)
        rc, _out, err = self.cli("--force", "--reason", "top up the balance")
        self.assertEqual(rc, 0, err)
        with open(tsh.holds_path()) as f:
            forced = [json.loads(ln) for ln in f
                      if json.loads(ln)["ev"] == "forced"]
        self.assertEqual(forced[-1]["facts"], ["TURN-WALL"])

    def test_the_seat_named_instead_is_never_itself_walled(self):
        now = time.time()
        self.fleet["kimi-2"] = tsh._row("kimi-2", "kimi", age=30)
        self.transcript([err_turn(now - 60)])
        # kimi's own last turn is walled too; kimi-2 is the live seat.
        self.RECIPIENT = "kimi"
        self.transcript([err_turn(now - 60, text="API Error: 403 please "
                                                 "verify your account")])
        self.RECIPIENT = "grok"
        self.assertIsNotNone(turnwall().seat_wall("kimi"))
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("Use @kimi-2 instead", why)


RED_GROK = {"grok": {"family": "grok", "colour": "RED",
                     "axes": {"money": "RED", "reach": None},
                     "cause": "grok's own last turn ended on a 402"},
            "kimi": {"family": "kimi", "colour": "GREEN",
                     "axes": {"money": "GREEN", "reach": None},
                     "cause": "fine"}}


GREEN_ALL = {"grok": dict(RED_GROK["grok"], colour="GREEN",
                          axes={"money": "GREEN", "reach": None},
                          cause="fine"),
             "kimi": RED_GROK["kimi"]}


class MoverBase(tsh.BrokenSeatDoorBase):
    """The mover's fixture: its own chat lines, no grace, an empty claims
    ledger, and a burn snapshot that reads grok RED."""

    def setUp(self):
        # THE ENV PATCH STARTS BEFORE THE BASE'S, so its cleanup, which runs
        # last, restores the environment from before this test. Started after,
        # it snapshotted the base's HELM_* fixture env and put it back after
        # the base's tearDown had removed it: a leak the gate refuses.
        env = mock.patch.dict(os.environ, {"HELM_DARK_MOVE_GRACE_S": "0"})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.posts = []
        from helm import chat
        # THE MOVER'S OWN LINES ONLY: a send DMs its recipient too. Its lines
        # are the #helm notices and, since task/3881, the #seats row.
        patch = mock.patch.object(chat, "post", lambda text, **kw: (
            self.posts.append(text)
            if kw.get("who") in ("idle-dispatch", "seat-events")
            else None) or {"id": "x"})
        patch.start()
        self.addCleanup(patch.stop)
        self.claims({})

    def claims(self, value):
        path = seats.claims_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, value)

    def dark(self, flags=RED_GROK):
        return mock.patch.object(burnflags, "cached_flags",
                                 return_value=(flags, 5))

    def tick(self, apply=False):
        from helm import darkmove
        with self.dark():
            return darkmove.run(apply=apply)


class TheDarkSeatMoverTest(MoverBase):
    """(3) The mover, over real dispatch and task ledgers."""

    def test_dry_run_is_the_default_and_moves_nothing(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        lines, moves = self.tick()
        self.assertEqual([(m["id"], m["to"], m["moved"]) for m in moves],
                         [(row["id"], "kimi", False)], lines)
        self.assertIn("would move dispatch", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")
        self.assertEqual(self.posts, [])

    def test_apply_rebinds_the_row_records_it_and_posts_one_line(self):  # noqa: VACUOUS_ASSERTION — the one moved row, its cancelled parent, its kimi child and the one posted line are the positive controls before the next tick moves nothing
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        lines, moves = self.tick(apply=True)
        self.assertEqual([m["moved"] for m in moves], [True], lines)
        old = dispatches.rows()[row["id"]]
        self.assertEqual(old["status"], "cancelled")
        self.assertIn("dark-seat mover", json.dumps(old))
        kids = [r for r in dispatches.rows().values()
                if r.get("supersedes") == row["id"]]
        self.assertEqual([k["recipient"] for k in kids], ["kimi"])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("@grok", self.posts[0])
        self.assertIn("moved 1 build row to @kimi", self.posts[0])
        # NEVER TWICE: the next tick finds nothing owed on grok.
        _lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [])
        self.assertEqual(len(self.posts), 1)

    def test_a_started_row_never_moves(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.claims({dispatches.autoclaim_resource(row["id"]): {
            "holder": "grok", "exp": time.time() + 3600,
            "exp_mono": time.monotonic() + 3600}})
        with mock.patch.object(dispatches, "live_claims", return_value={
                dispatches.autoclaim_resource(row["id"]):
                    {"holder": "grok"}}):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [])
        self.assertIn("stays on @grok", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_an_unread_fact_moves_nothing_and_says_so_in_one_line(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        from helm import darkmove
        # NO BURN SNAPSHOT: no family is confirmed dark.
        lines, moves = darkmove.run(apply=True)
        self.assertEqual(moves, [])
        self.assertIn("no fresh burn snapshot", lines[0])
        # AN UNREADABLE CLAIMS LEDGER: whether a row started is unknown.
        with mock.patch.object(dispatches, "live_claims", return_value=None):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [])
        self.assertIn("claims ledger did not read", "\n".join(lines))
        # A ROSTER THAT RAISES: one line, no traceback.
        from helm import seat_usability
        with mock.patch.object(seat_usability, "join",
                               side_effect=OSError("roster gone")):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [])
        self.assertIn("roster did not read", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_inside_the_grace_nothing_moves(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        with mock.patch.dict(os.environ, {"HELM_DARK_MOVE_GRACE_S": "600"}):
            lines, moves = self.tick(apply=True)
            self.assertEqual(moves, [])
            self.assertIn("inside the 600s grace", "\n".join(lines))
            # THE FIRST SIGHTING IS LATCHED: ten minutes on, it is confirmed.
            from helm import darkmove
            with self.dark():
                _lines, moves = darkmove.run(apply=False,
                                             now=time.time() + 601)
        self.assertEqual([m["to"] for m in moves], ["kimi"])

    def test_no_live_seat_leaves_the_row_and_reports_it_once(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        flags = dict(RED_GROK, kimi=dict(RED_GROK["kimi"],
                                         axes={"money": "GREEN",
                                               "reach": "RED"}))
        from helm import darkmove
        with self.dark(flags):
            lines, moves = darkmove.run(apply=True)
        self.assertEqual(moves, [])
        stays = [ln for ln in lines if "stays on @grok" in ln]
        self.assertEqual(len(stays), 1, lines)
        self.assertIn("no live seat", stays[0])
        self.assertEqual(len(self.posts), 1)
        with self.dark(flags):
            lines, _moves = darkmove.run(apply=True)
        self.assertEqual([ln for ln in lines if "stays on @grok" in ln], [])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_an_owned_task_moves_claimed_or_not(self):
        """task/3881's remainder: an in_progress task is STARTED work, and
        with no room holding uncommitted changes it moves too, keeping its
        status (the capability moves the owner only)."""
        from helm import seat_reassign
        # THIS HELM'S OWN PROJECT: a task's lanes are read in its repository.
        project = os.path.basename(self.repo)
        owed, err = tasks.add("the balance top-up", "grok", project=project)
        self.assertIsNone(err, err)
        started, err = tasks.add("the proxy fix", "grok", project=project)
        self.assertIsNone(err, err)
        _row, err = tasks.update(started["id"], status="in_progress",
                                 owner="grok")
        self.assertIsNone(err, err)
        # THE OWNER IS NOT MEASURABLY LIVE: a LIVE one is a veto no force
        # overrides (CURE2 F2, pinned below).
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "pane gone")):
            lines, moves = self.tick(apply=True)
        self.assertEqual(sorted((m["id"], m["moved"]) for m in moves),
                         sorted([(owed["id"], True), (started["id"], True)]),
                         lines)
        self.assertEqual(tasks.rows()[owed["id"]]["owner"], "kimi")
        self.assertEqual((tasks.rows()[started["id"]]["owner"],
                          tasks.rows()[started["id"]]["status"]),
                         ("kimi", "in_progress"))

    def unreachable(self, pane):
        """grok, as the usability join measures a seat with no armed beacon:
        its pane GONE (`pane` False) or still live."""
        from helm import seat_usability
        self.fleet["grok"] = dict(
            self.fleet["grok"], verdict=seat_usability.UNUSABLE,
            can_take_work=False, pane=pane, reachable=False,
            reachable_why="no live beacon",
            refusals=(("pane",) if pane is False else ()) + (
                seat_usability.REFUSE_REACHABLE,))

    def test_a_seat_with_no_route_is_dark_and_its_row_moves(self):
        """No family wall at all: the pane is GONE and no beacon is armed, so
        nothing can deliver the row or wake the seat (task/3168)."""
        from helm import darkmove
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.unreachable(pane=False)
        with self.dark(GREEN_ALL), \
                mock.patch.dict(os.environ, {"HELM_DARK_MOVE_PANE_S": "0"}):
            lines, moves = darkmove.run(apply=True)
        self.assertEqual([(m["id"], m["to"], m["moved"]) for m in moves],
                         [(row["id"], "kimi", True)], lines)
        self.assertIn("pane is GONE", self.posts[0])

    def test_a_deaf_seat_with_a_live_pane_keeps_its_rows_reported_once(self):
        """A live pane with no beacon has a route: the census re-arm, or one
        typed wake. It keeps its rows, and the route is said once."""
        from helm import darkmove
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.unreachable(pane=True)
        with self.dark(GREEN_ALL):
            lines, moves = darkmove.run(apply=True)
        self.assertEqual(moves, [])
        deaf = [ln for ln in lines if "keeps its rows" in ln]
        self.assertEqual(len(deaf), 1, lines)
        self.assertIn("resume-turn --nudge", deaf[0])
        with self.dark(GREEN_ALL):
            lines, _moves = darkmove.run(apply=True)
        self.assertEqual([ln for ln in lines if "keeps its rows" in ln], [])
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_a_seat_walled_on_its_own_turn_moves_while_its_family_is_not(self):
        from helm import darkmove
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.transcript([err_turn(time.time() - 60)])
        with self.dark(GREEN_ALL):
            lines, moves = darkmove.run(apply=False)
        self.assertEqual([(m["id"], m["to"]) for m in moves],
                         [(row["id"], "kimi")], lines)
        self.assertIn("TURN-WALL", "\n".join(lines))

    def test_a_relay_driven_seat_is_never_judged_dark(self):
        """A remote seat's liveness is its relay session (task/3219): the
        mover reuses idle-dispatch's relay predicate and moves nothing the
        relay speaks for, live or unaccounted."""
        from helm import idle_dispatch
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        for reading in ((True, "relay session live"),
                        (False, "the relay journal holds no session")):
            with self.subTest(reading=reading), mock.patch.object(
                    idle_dispatch, "_relay_reading", return_value=reading):
                _lines, moves = self.tick(apply=True)
            self.assertEqual(moves, [])
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_the_idle_dispatch_tick_runs_the_mover(self):
        from helm import idle_dispatch
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        res = {"findings": [], "alerted": [], "woke": [], "undelivered": [],
               "redeliverable": []}
        buf = io.StringIO()
        with self.dark(), \
                mock.patch.object(idle_dispatch, "check", return_value=res), \
                contextlib.redirect_stdout(buf):
            rc = idle_dispatch.cmd_idle_dispatch(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("would move dispatch %s" % row["id"][:12],
                      buf.getvalue())
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")


    def test_a_junk_tail_refuses_before_any_pass(self):
        """--apply writes, so `--bogus --apply` and a misspelt `--aply`
        refuse rc 2 before the sweep or the mover runs."""
        from helm import darkmove, idle_dispatch
        for argv in (["--bogus", "--apply"], ["--aply"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(idle_dispatch, "check") as check, \
                    mock.patch.object(darkmove, "run") as run, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(idle_dispatch.cmd_idle_dispatch(argv), 2)
            self.assertFalse(check.called or run.called)


class AReviewGoesOnlyToAnEligibleReaderTest(unittest.TestCase):
    """(3) The review target is the ladder's answer, never re-derived: the
    first measured-eligible reader outside a dark family."""

    def pick(self, report, err=None):
        from helm import darkmove
        row = {"id": "r" * 12, "recipient": "grok", "kind": "review",
               "sender": "kimi"}
        return darkmove._reviewer(row, {"grok": "dark"},
                                  lambda rid: (report, err))

    def report(self, eligible, seats_, unreadable=None):
        return {"measured_eligible": eligible, "unreadable": unreadable or {},
                "seats": [{"seat": s, "family": f} for s, f in seats_]}

    def test_the_first_eligible_reader_outside_a_dark_family(self):
        rep = self.report(["grok-2", "codex"],
                          [("grok-2", "grok"), ("codex", "codex"),
                           ("kimi", "kimi")])
        self.assertEqual(self.pick(rep), ("codex", None))

    def test_the_author_the_ladder_excluded_is_never_chosen(self):
        # kimi is the row's sender: the ladder's chain rung excludes it, so it
        # is not in measured_eligible and the mover does not reach for it.
        rep = self.report([], [("kimi", "kimi")])
        to, why = self.pick(rep)
        self.assertIsNone(to)
        self.assertIn("no measured-eligible", why)

    def test_a_partial_or_unread_ladder_moves_nothing(self):
        rep = self.report(["codex"], [("codex", "codex")],
                          unreadable={"chain": "ledger unreadable"})
        self.assertEqual(self.pick(rep)[0], None)
        self.assertEqual(self.pick(None, err="no such row")[0], None)


GEMINI_429 = (
    'API Error: 429 {"error":{"code":429,"message":"You exceeded your current '
    'quota, please check your plan and billing details. For more information '
    'on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.'
    '\\n* Quota exceeded for metric: generativelanguage.googleapis.com/'
    'generate_content_free_tier_requests, limit: 10, model: gemini-2.5-pro'
    '\\nPlease retry in 41.2s.","status":"RESOURCE_EXHAUSTED","details":'
    '[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":'
    '"41s"}]}}')


class AStatedShortResetIsNotAWallTest(unittest.TestCase):
    """REVIEW P1 + P2 (claim gap): a refusal that names when it clears, and
    that clears within `SHORT_RESET_S`, is transient; a long reset is still
    MONEY; a 401 credential refusal is REACH, a 400 low balance is MONEY."""

    def typed(self, text):
        got = turnwall().classify(text)
        return (got["axis"], got["code"]) if got else None

    def test_geminis_free_tier_retry_in_41s_is_not_a_wall(self):
        self.assertIsNone(self.typed(GEMINI_429[len("API Error"):]))
        now = time.time()
        state, _typed, _at = turnwall().last_turn(
            [ok_turn(now - 300), err_turn(now - 60, text=GEMINI_429)])
        self.assertIsNone(state)

    def test_reset_in_try_again_at_and_retry_delay_are_transient(self):
        for text in (
                " 429 Usage limit exhausted for this minute window, reset "
                "in 30s",
                " 429 credit balance exhausted. Please try again at 5pm",
                " 429 quota exhausted; resets at 17:00 UTC",
                ' 429 {"error":{"message":"Quota exceeded","details":'
                '[{"retryDelay":"20s"}]}}',
                " 429 usage quota exhausted, retry in 2 hours"):
            with self.subTest(text=text):
                self.assertIsNone(self.typed(text))

    def test_a_long_stated_reset_is_still_money(self):
        self.assertGreater(turnwall().SHORT_RESET_S, 3600)
        for text in (" 429 usage quota exhausted, resets in 5 days",
                     " 429 insufficient balance, try again in 30 days"):
            with self.subTest(text=text):
                self.assertEqual(self.typed(text), ("money", 429))

    def test_a_401_credential_refusal_is_reach(self):
        for text in (
                ' 401 {"type":"error","error":{"type":"authentication_error",'
                '"message":"OAuth token has expired. Please obtain a new '
                'token or refresh your existing token."}}',
                ' 401 {"error":{"message":"Incorrect API key provided: '
                'sk-xx***","type":"invalid_request_error"}}'):
            with self.subTest(text=text):
                self.assertEqual(self.typed(text), ("reach", 401))

    def test_a_400_low_credit_balance_is_money(self):
        self.assertEqual(self.typed(
            ' 400 {"type":"error","error":{"type":"invalid_request_error",'
            '"message":"Your credit balance is too low to access the '
            'Anthropic API. Please go to Plans & Billing to upgrade or '
            'purchase credits."}}'), ("money", 400))
        self.assertIsNone(self.typed(" 400 prompt is too long"))


class AFamilyNeedsTwoWallsTest(unittest.TestCase):
    """REVIEW P1, as CURE2 F1 corrected it: a family walls only on walls of
    TWO DISTINCT SEATS. One spent account nudged twice is one seat, and it
    must not dark every busy seat of its family."""

    def reading(self, seat, at, turns=1, fam="grok", state="WALL"):
        return {"seat": seat, "family": fam, "state": state, "at": at,
                "axis": "money", "code": 402, "label": "402", "turns": turns}

    def walls(self, readings):
        by = {r["seat"]: r for r in readings}
        return turnwall().family_walls(seats=sorted(by), now=time.time(),
                                       read=lambda s: by[s])

    def test_one_seat_one_turn_does_not_wall_the_family(self):
        now = time.time()
        self.assertEqual(self.walls([self.reading("grok", now - 60)]), {})

    def test_two_seats_wall_the_family(self):
        now = time.time()
        got = self.walls([self.reading("grok", now - 60),
                          self.reading("grok-2", now - 30)])
        self.assertEqual(sorted(got), ["grok"])
        self.assertIn("money", got["grok"])

    def test_two_turns_of_one_seat_do_NOT_wall_the_family(self):
        """CURE2 F1: turnwall counted one seat's streak toward FAMILY_WALLS,
        so a codex account refused twice walled all of codex."""
        now = time.time()
        self.assertEqual(self.walls([self.reading("grok", now - 60,
                                                  turns=5)]), {})
        # CONTROL: the same streak beside a second walled seat does wall it
        got = self.walls([self.reading("grok", now - 60, turns=5),
                          self.reading("grok-2", now - 30)])
        self.assertIn("money", got.get("grok") or {})

    def test_the_reading_counts_consecutive_walled_turns(self):
        now = time.time()
        one, _ = turnwall().seat_reading(
            "grok", lines=[ok_turn(now - 300), err_turn(now - 60)], now=now)
        two, _ = turnwall().seat_reading(
            "grok", lines=[ok_turn(now - 300), err_turn(now - 120),
                           err_turn(now - 60)], now=now)
        self.assertEqual((one or {}).get("turns"), 1)
        self.assertEqual((two or {}).get("turns"), 2)


class TheMoverRefusesStartedWorkTest(MoverBase):
    """REVIEW P2 + P3: a lease, lane commits or a held room is started work;
    an unread one moves nothing; the dry latch never silences --apply; moves
    are capped and balanced; a task stays inside its project's team."""

    def project(self):
        return os.path.basename(self.repo)

    def lane_commit(self, branch, base=None):
        self.git("checkout", "-q", "-b", branch, base or self.a)
        sha = self.commit("lane work on " + branch)
        self.git("checkout", "-q", self.main)
        return sha

    def owed_task(self, title="the balance top-up"):
        row, err = tasks.add(title, "grok", project=self.project())
        self.assertIsNone(err, err)
        return row

    def reassign_live(self, state=None):
        """The owner's measured disposition: not LIVE unless an arm says so,
        so a task that stays, stays for the reason the arm names."""
        from helm import seat_reassign
        return mock.patch.object(seat_reassign, "source_disposition",
                                 return_value=(state or seat_reassign.SOURCE_DEAD,
                                               "pane measured"))

    # STARTED WORK MOVES WITH ITS HOLDING (task/3881's remainder). These
    # arms pin what is READ as started, on a dry pass: the line names the
    # lease or the lane that moves with the task.

    def test_a_task_whose_owner_holds_its_lane_lease_is_started_work(self):
        task = self.owed_task()
        n = task["id"].split("/")[-1]
        with self.reassign_live(), mock.patch.object(
                dispatches, "live_claims", return_value={
                    "worktree:repo:task-%s" % n: {"holder": "grok"}}):
            lines, moves = self.tick()
        self.assertEqual([(m["id"], m.get("started")) for m in moves],
                         [(task["id"], True)], lines)
        self.assertIn("would move started task", "\n".join(lines))
        self.assertIn("lease worktree:repo:task-%s" % n, "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")

    def test_a_task_whose_lane_has_commits_off_trunk_is_started_work(self):
        task = self.owed_task()
        n = task["id"].split("/")[-1]
        sha = self.lane_commit("lane/task-%s" % n)
        with self.reassign_live():
            lines, moves = self.tick()
        self.assertEqual([(m["id"], m.get("started")) for m in moves],
                         [(task["id"], True)], lines)
        self.assertIn("lane/task-%s at %s" % (n, sha[:12]), "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")

    def test_a_task_moves_nothing_when_leases_or_lanes_do_not_read(self):
        from helm import landreq
        task = self.owed_task()
        with self.reassign_live(), mock.patch.object(
                dispatches, "live_claims", return_value=None):
            _lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [])
        with self.reassign_live(), mock.patch.object(
                landreq, "_git", return_value=None):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")

    def test_a_build_row_whose_lane_has_commits_off_trunk_stays(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.lane_commit("lane/" + row["lane"])
        lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [], lines)
        self.assertIn("not on trunk", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    FENCE = {"lane": "worktree:repo:lane-broken-1", "holder": "grok",
             "remaining": 300}

    def test_a_held_room_is_started_and_the_row_stays(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        with mock.patch.object(dispatches, "rebind_room_fence",
                               return_value=[self.FENCE]):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [], lines)
        self.assertIn("still holds the room", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")

    def test_a_fence_the_rebind_reports_rides_the_line_and_the_post(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        with mock.patch.object(dispatches, "rebind_room_fence",
                               side_effect=[[], [self.FENCE]]):
            lines, moves = self.tick(apply=True)
        self.assertEqual([m["moved"] for m in moves], [True], lines)
        self.assertIn("still holds the room", "\n".join(lines))
        self.assertIn("still holds the room", self.posts[0])

    def test_a_dry_tick_never_silences_the_apply_report(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        flags = dict(RED_GROK, kimi=dict(RED_GROK["kimi"],
                                         axes={"money": "GREEN",
                                               "reach": "RED"}))
        from helm import darkmove
        with self.dark(flags):
            lines, _moves = darkmove.run(apply=False)
        self.assertEqual(len([ln for ln in lines if "stays on @grok" in ln]),
                         1, lines)
        self.assertEqual(self.posts, [])
        with self.dark(flags):
            lines, _moves = darkmove.run(apply=True)
        self.assertEqual(len([ln for ln in lines if "stays on @grok" in ln]),
                         1, lines)
        self.assertEqual(len(self.posts), 1)

    def test_moves_per_tick_are_capped(self):
        from helm import darkmove
        self.assertGreater(darkmove.MOVE_CAP, 0)
        for _ in range(3):
            row, why, _ = self.send()
            self.assertIsNotNone(row, why)
        with mock.patch.dict(os.environ, {"HELM_DARK_MOVE_MAX": "2"}):
            lines, moves = self.tick()
        self.assertEqual(len(moves), 2, lines)
        self.assertIn("next tick", "\n".join(lines))

    def test_targets_are_balanced_not_always_the_first(self):
        self.fleet["kimi-2"] = tsh._row("kimi-2", "kimi", age=30)
        for _ in range(2):
            row, why, _ = self.send()
            self.assertIsNotNone(row, why)
        lines, moves = self.tick()
        self.assertEqual(sorted(m["to"] for m in moves), ["kimi", "kimi-2"],
                         lines)

    def test_a_task_moves_only_inside_its_projects_team(self):
        from helm import teams
        self.fleet["kimi-2"] = tsh._row("kimi-2", "kimi", age=30)
        task = self.owed_task()
        team = {"authored": True, "members": [{"seat": "kimi-2"}]}
        with self.reassign_live(), mock.patch.object(
                teams, "read", return_value=team):
            lines, moves = self.tick(apply=True)
        self.assertEqual([(m["id"], m["to"]) for m in moves],
                         [(task["id"], "kimi-2")], lines)
        task = self.owed_task("the quota reset watch")
        with self.reassign_live(), mock.patch.object(
                teams, "read", side_effect=OSError("registry gone")):
            lines, moves = self.tick(apply=True)
        self.assertEqual(moves, [], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")


class TheDoorFaultsTest(MoverBase):
    """CURE2 3587, opus-integrator's door read at 9dd7e134: lanes are named
    <slug>-N (13 of 401 carry task-N), force overrode the LIVE veto, build
    rows went roster-wide, and a stale burn reading moved work."""

    project = TheMoverRefusesStartedWorkTest.project
    lane_commit = TheMoverRefusesStartedWorkTest.lane_commit
    owed_task = TheMoverRefusesStartedWorkTest.owed_task
    reassign_live = TheMoverRefusesStartedWorkTest.reassign_live

    def n(self, task):
        return task["id"].split("/")[-1]

    def test_a_lease_on_a_slug_N_lane_is_started_work(self):
        task = self.owed_task()
        with self.reassign_live(), mock.patch.object(
                dispatches, "live_claims", return_value={
                    "worktree:repo:balance-top-up-%s" % self.n(task):
                        {"holder": "grok"}}):
            lines, moves = self.tick()
        self.assertEqual([m.get("started") for m in moves], [True], lines)
        self.assertIn("lease worktree:repo:balance-top-up-%s" % self.n(task),
                      "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")

    def test_a_slug_N_lane_with_commits_off_trunk_is_started_work(self):
        task = self.owed_task()
        self.lane_commit("lane/balance-top-up-%s" % self.n(task))
        with self.reassign_live():
            lines, moves = self.tick()
        self.assertEqual([m.get("started") for m in moves], [True], lines)
        self.assertIn("lane/balance-top-up-%s at" % self.n(task),
                      "\n".join(lines))

    def test_the_tasks_recorded_lane_is_read_too(self):
        """A lane whose name carries no number is the task's when the task
        records it (a `lane:<name>` ref)."""
        row, err = tasks.add("the balance top-up", "grok",
                             project=self.project(),
                             refs=["lane:balance-top-up"])
        self.assertIsNone(err, err)
        self.lane_commit("lane/balance-top-up")
        with self.reassign_live():
            lines, moves = self.tick()
        self.assertEqual([m.get("started") for m in moves], [True], lines)
        self.assertIn("lane/balance-top-up at", "\n".join(lines))
        self.assertEqual(tasks.rows()[row["id"]]["owner"], "grok")

    def test_force_never_overrides_a_LIVE_owner(self):
        from helm import seat_reassign
        task = self.owed_task()
        with self.reassign_live(seat_reassign.SOURCE_LIVE):
            lines, moves = self.tick(apply=True)
        self.assertEqual([m["moved"] for m in moves], [False], lines)
        self.assertIn("LIVE", "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")

    def test_a_build_row_moves_only_inside_its_projects_team(self):
        from helm import teams
        self.fleet["kimi-2"] = tsh._row("kimi-2", "kimi", age=30)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        team = {"authored": True, "members": [{"seat": "kimi-2"}]}
        with mock.patch.object(teams, "read", return_value=team):
            lines, moves = self.tick()
        self.assertEqual([(m["id"], m["to"]) for m in moves],
                         [(row["id"], "kimi-2")], lines)
        with mock.patch.object(teams, "read",
                               side_effect=OSError("registry gone")):
            lines, moves = self.tick()
        self.assertEqual(moves, [], lines)

    def test_a_stale_burn_reading_moves_nothing(self):
        """grok is walled on its own turn, but no fresh burn snapshot says
        whether kimi's family can spend: the mover fails closed."""
        from helm import darkmove
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.transcript([err_turn(time.time() - 60)])
        lines, moves = darkmove.run(apply=True)
        self.assertEqual(moves, [], lines)
        self.assertIn("no fresh burn snapshot", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")
        # A SNAPSHOT THAT DOES NOT NAME THE TARGET'S FAMILY is no fresher
        with self.dark({"grok": GREEN_ALL["grok"]}):
            lines, moves = darkmove.run(apply=True)
        self.assertEqual(moves, [], lines)
        # CONTROL: fresh, and naming kimi's family, it moves
        with self.dark(GREEN_ALL):
            lines, moves = darkmove.run(apply=True)
        self.assertEqual([(m["to"], m["moved"]) for m in moves],
                         [("kimi", True)], lines)


class TheReviewTargetWroteNoRoundTest(unittest.TestCase):
    """REVIEW P2, as the owner ruled it: the review door needs ONE
    approval-tier read by a NON-AUTHOR, not another family. The ladder the
    mover reads already excludes every seat that wrote a round, the patch
    author of a cure included; a same-family non-author is a fine target."""

    def test_a_patch_author_is_a_chain_writer_the_ladder_excludes(self):
        from helm import landreq_close, reviewer_eligibility
        build = {"id": "b" * 32, "kind": "build", "recipient": "grok",
                 "sender": "integrator", "ref": "a" * 40}
        review = {"id": "r" * 32, "kind": "review", "recipient": "kimi",
                  "sender": "grok", "ref": "c" * 40, "supersedes": build["id"],
                  "verdict": "FIX", "patch_tip": "d" * 40,
                  "patch_author": "codex"}
        rows = {build["id"]: build, review["id"]: review}
        wrote = landreq_close.chain_writers([build, review], rows, writers={})
        self.assertIn("codex", wrote)
        self.assertIn("grok", wrote)
        self.assertEqual(
            reviewer_eligibility._rung_chain("codex", wrote, None)[0],
            "exclude")
        self.assertEqual(
            reviewer_eligibility._rung_chain("grok-2", wrote, None)[0],
            "pass")

    def test_no_cross_family_filter_a_same_family_non_author_is_taken(self):
        from helm import darkmove
        row = {"id": "r" * 12, "recipient": "grok-3", "kind": "review",
               "sender": "grok"}
        report = {"measured_eligible": ["grok-2"], "unreadable": {},
                  "seats": [{"seat": "grok-2", "family": "grok"}]}
        self.assertEqual(darkmove._reviewer(row, {}, lambda rid: (report,
                                                                  None)),
                         ("grok-2", None))


if __name__ == "__main__":
    unittest.main()
