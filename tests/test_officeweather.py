#!/usr/bin/env python3
"""task/3902, office floor slice 4: OFFICE WEATHER.

THE OWNER asked for one line about the whole floor on his phone, sent only
when it changes: sunny when every live seat is flowing or idle, cloudy when
something is off and a steward has it, stormy when he has to come over. The
same line heads `helm brief` and the owner console.

WHAT THESE PIN, one class per claim the module makes:

  WORD      sunny, cloudy or stormy from the floor's moods (task/3899) and the
            fleet's own alarms, with a stuck seat handled only by a LIVE
            steward that is not itself stuck, and an unreadable source never
            reading sunny;
  SOURCES   each real reader turns its owner's answer into findings: the
            stall watcher's episode, the chat node's boot diagnosis, the land
            train's state and its timer, the burn flags, and task/3876's
            steward table;
  PUSH      the pass the timer runs, under the owner's paging rule ("A P0
            cause turns the office weather stormy, and nothing else pages
            him"): a first reading is a
            baseline, a change settles only after it holds HOLD_S, only an
            edge INTO stormy and the matching all-clear OUT of it reach the
            phone, every other change is shown and never pushed, an
            all-clear waits out GAP_S since the storm's push and is dropped
            if the storm returns, a failed push stays owed, and a held lock
            pushes nothing;
  NO PAGE   no test run reaches the owner's phone through the weather, with
            HELM_NTFY_TOPIC in its environment or not: the suite plants the
            weather's switch off (tests/__init__.py), which makes the one
            phone seam inert and the idle tick's pass a no-op;
  FLEET     fleet-down has ONE definition, beacon_phone's qualifier, driven
            through the real `_fleet` over a faked census: the steward or a
            strict majority unreachable on a complete census turns stormy, an
            incomplete one never does, and the cutover's pager latch is read
            once and judged by the same rule;
  SURFACES  the settled line heads `helm brief` and rides /api/ready into the
            console's nav strip;
  HOOK      the idle-dispatch tick runs the pass, and its dry runs do not
            push;
  CLI       `helm office weather [--json] [--push]`.

Every world is a temp HELM_HOME, every reader is a fake, and the phone is a
fake: nothing here reads this machine's fleet or reaches the owner.
"""
import calendar
import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-officeweather-", var="HELM_HOME")

NOW = calendar.timegm((2026, 9, 30, 18, 0, 0, 0, 0, 0))
PHONE_KEYS = ("HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC", "HELM_TELEGRAM_TOKEN",
              "HELM_TELEGRAM_CHAT_ID", "MELD_TELEGRAM_TOKEN",
              "MELD_TELEGRAM_CHAT_ID")
#: The weather's own switch (officeweather.SWITCH_ENV), planted "off" for the
#: whole suite by tests/__init__.py. World never pops it.
SWITCH = "HELM_OFFICE_WEATHER"


def _ow():
    """The module under test, imported per arm so the pre-change tree reds
    arm by arm rather than as one collection error."""
    from helm import officeweather
    return officeweather


def mood(seat, state, reason="r", family=None):
    """One seat's reading in `seatmood.judge`'s shape."""
    wall = {"source": "family", "family": family, "axis": "money",
            "why": "w"} if family else None
    return {"seat": seat, "state": state, "score": 0, "reason": reason,
            "signals": {"wall": wall}}


def reads(moods=(), stewards=None, **fleet):
    """Every reader faked: the floor, a steward table {seat: steward}, and
    each fleet source's findings."""
    table = dict(stewards or {})

    def named(ms):
        return {m["seat"].casefold(): (table[m["seat"]], None)
                if table.get(m["seat"]) else (None, "nobody is declared")
                for m in ms}
    out = {"floor": lambda now: list(moods), "stewards": named}
    for name in ("stall", "chat", "land", "burn"):
        out[name] = (lambda got: lambda now: list(got))(fleet.get(name, ()))
    out["causes"] = (lambda got: lambda now: list(
        (word, "%s: %s" % (key, why)) for key, word, why in got))(
        fleet.get("causes", ()))
    out["fleet"] = (lambda got: lambda now: list(
        (word, "%s: %s" % (key, why)) for key, word, why in got))(
        fleet.get("fleet", ()))
    return out


@contextlib.contextmanager
def seatevents_as(fake):
    """task/3876's steward module as `fake` (None: absent), for both ways
    `from . import seatevents` can find it: the package attribute an earlier
    import left, and sys.modules."""
    import helm
    had, old = "seatevents" in vars(helm), vars(helm).get("seatevents")
    with mock.patch.dict(sys.modules, {"helm.seatevents": fake}):
        if fake is None:
            vars(helm).pop("seatevents", None)
        else:
            helm.seatevents = fake
        try:
            yield
        finally:
            vars(helm).pop("seatevents", None)
            if had:
                helm.seatevents = old


class Phone:
    """notify's two calls, recorded; `ok` is what a push returns."""

    def __init__(self, ok=True, available=True):
        self.ok, self.available, self.pushes = ok, available, []

    def configured(self):
        return self.available

    def owner_push(self, body, title="helm", receipt=None, reply_key=None):
        self.pushes.append((body, title))
        return self.ok


class World(unittest.TestCase):
    """A temp HELM_HOME, no phone keys and no weather knobs in the env. The
    suite's switch (SWITCH, "off") stays: an arm that drives delivery hands
    the pass a fake phone, which is the seam's replacement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-weather-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        for k in PHONE_KEYS + ("HELM_OFFICE_WEATHER_HOLD_S",
                               "HELM_OFFICE_WEATHER_GAP_S"):
            os.environ.pop(k, None)


# ------------------------------------------------------------------ WORD

class WordTest(World):

    def test_every_seat_flowing_or_idle_and_no_alarm_is_sunny(self):
        ow = _ow()
        word, line, reasons = ow.weather(NOW, reads(
            [mood("alpha", "flowing"), mood("beta", "idle")]))
        self.assertEqual((word, reasons), ("sunny", []))
        self.assertTrue(line.startswith(ow.GLYPH["sunny"] + " sunny: "), line)
        self.assertIn("2 live seats", line)

    def test_a_grinding_seat_with_a_live_steward_is_cloudy(self):
        ow = _ow()
        word, line, reasons = ow.weather(NOW, reads(
            [mood("alpha", "grinding"), mood("lead", "flowing")],
            stewards={"alpha": "lead"}))
        self.assertEqual(word, "cloudy")
        self.assertIn("alpha is grinding, lead has it", line)
        self.assertEqual([r["word"] for r in reasons], ["cloudy"])

    def test_a_stuck_seat_a_live_steward_has_is_cloudy(self):
        word, line, _r = _ow().weather(NOW, reads(
            [mood("alpha", "stuck"), mood("lead", "grinding")],
            stewards={"alpha": "lead", "lead": "boss"}))
        # the steward grinding is still able: only stuck, blocked or walled
        # stewards cannot handle anyone
        self.assertNotIn("stormy", line)
        self.assertIn("alpha is stuck, lead has it", line)
        self.assertEqual(word, "cloudy")

    def test_a_stuck_seat_whose_steward_is_not_live_is_cloudy(self):
        word, line, reasons = _ow().weather(NOW, reads(
            [mood("alpha", "stuck", reason="3 refusals by author-gate")],
            stewards={"alpha": "lead"}))
        self.assertEqual(word, "cloudy")
        self.assertIn("alpha is stuck and no steward has it", line)
        self.assertIn("lead is not live", reasons[0]["why"])
        self.assertIn("3 refusals by author-gate", reasons[0]["why"])

    def test_two_stuck_seats_stewarding_each_other_are_cloudy(self):
        word, _line, reasons = _ow().weather(NOW, reads(
            [mood("alpha", "stuck"), mood("beta", "stuck")],
            stewards={"alpha": "beta", "beta": "alpha"}))
        self.assertEqual(word, "cloudy")
        self.assertEqual(len(reasons), 2)
        self.assertIn("its steward beta is stuck too", reasons[0]["why"])

    def test_a_stuck_seat_without_steward_settles_cloudy_without_paging(self):
        ow, phone = _ow(), Phone()
        calm, stuck = [mood("alpha", "flowing")], [mood("alpha", "stuck")]
        self.assertEqual(ow.tick(NOW, reads=reads(calm), phone=phone)["word"],
                         "sunny")
        ow.tick(NOW + 10, reads=reads(stuck), phone=phone)
        got = ow.tick(NOW + 10 + ow.HOLD_S, reads=reads(stuck), phone=phone)
        self.assertEqual((got["word"], got["settled"]["word"]),
                         ("cloudy", "cloudy"))
        self.assertIn("no steward has it", got["line"])
        self.assertIn("shown, not pushed", got["phone"])
        self.assertEqual(phone.pushes, [])
        # Existing owner-action storm and all-family outage remain separate.
        word, _line, _ = ow.weather(NOW, reads(
            [mood("alpha", "blocked-on-owner")]))
        self.assertEqual(word, "stormy")
        from helm import burnflags
        with mock.patch.object(burnflags, "cached_snapshot", return_value=(
                {"families": {"kimi": {"colour": burnflags.RED}}}, 0)):
            self.assertEqual(ow._burn(NOW)[0][0], "stormy")

    def test_a_stuck_seat_that_is_its_own_steward_is_cloudy(self):
        word, _line, reasons = _ow().weather(NOW, reads(
            [mood("Lead", "stuck")], stewards={"Lead": "lead"}))
        self.assertEqual(word, "cloudy")
        self.assertIn("its own steward", reasons[0]["why"])

    def test_a_seat_blocked_on_the_owner_is_stormy_and_named(self):
        word, line, _r = _ow().weather(NOW, reads(
            [mood("alpha", "blocked-on-owner",
                  reason='waiting on the owner: decide card 7 "ship it"'),
             mood("beta", "flowing")]))
        self.assertEqual(word, "stormy")
        self.assertIn("alpha waits on you: waiting on the owner: decide "
                      "card 7", line)

    def test_a_walled_seat_is_cloudy_even_with_no_steward(self):
        word, line, _r = _ow().weather(NOW, reads(
            [mood("alpha", "walled", family="kimi")]))
        self.assertEqual(word, "cloudy")
        self.assertIn("alpha is walled (nobody is declared)", line)

    def test_a_fleet_storm_over_a_calm_floor_is_stormy(self):
        word, line, reasons = _ow().weather(NOW, reads(
            [mood("alpha", "flowing")],
            chat=[("stormy", "the chat node is not answering (down)")],
            land=[("cloudy", "auto-land is paused by lead")]))
        self.assertEqual(word, "stormy")
        self.assertEqual([r["word"] for r in reasons], ["stormy", "cloudy"])
        self.assertTrue(line.startswith(_ow().GLYPH["stormy"] + " stormy: "
                                        "the chat node is not answering"),
                        line)

    def test_an_unreadable_floor_is_cloudy_never_sunny(self):
        def broken(now):
            raise OSError("the roster could not be read")
        word, line, _r = _ow().weather(NOW, dict(reads(), floor=broken))
        self.assertEqual(word, "cloudy")
        self.assertIn("the floor could not be read (OSError: the roster",
                      line)

    def test_a_source_that_raises_is_named_and_cloudy(self):
        def broken(now):
            raise RuntimeError("boom")
        word, line, _r = _ow().weather(NOW, dict(
            reads([mood("alpha", "flowing")]), land=broken))
        self.assertEqual(word, "cloudy")
        self.assertIn("the land train could not be read (RuntimeError: "
                      "boom)", line)

    def test_an_idle_seat_with_unreadable_decision_cards_is_not_sunny(self):
        idle = mood("alpha", "idle")
        idle["signals"]["unread"] = ["decision cards (unreadable)"]
        word, line, reasons = _ow().weather(NOW, reads([idle]))
        self.assertEqual(word, "cloudy")
        self.assertTrue(reasons[0]["unread"])
        self.assertIn("alpha's mood sources could not be read", line)

    def test_the_line_is_one_short_printable_line(self):
        ow = _ow()
        floor = [mood("seat%02d" % i, "blocked-on-owner",
                      reason="\x1b\u202e\n" + "x" * 300) for i in range(9)]
        word, line, reasons = ow.weather(NOW, reads(floor))
        self.assertEqual((word, len(reasons)), ("stormy", 9))
        self.assertLessEqual(len(line), ow.LINE_MAX)
        # the count of what did not fit survives the cut
        self.assertTrue(line.endswith("…; +7 more"), line)
        self.assertIn("seat00 waits on you: xxx", line)
        for bad in ("\n", "\x1b", "\u202e"):
            self.assertNotIn(bad, line)
            self.assertNotIn(bad, reasons[0]["why"])
        short = ow.weather(NOW, reads(floor[:3]))[1]
        self.assertTrue(short.endswith("…; +1 more"), short)


# ------------------------------------------------------------------ SOURCES

class SourcesTest(World):

    def test_the_chat_node_answering_or_switched_off_is_no_finding(self):  # noqa: VACUOUS_ASSERTION — the sibling arm drives the same reader to a stormy finding; here the empty answer IS the claim, and the stormy control below runs unconditionally
        ow = _ow()
        from helm import chat, chatnode
        with mock.patch.object(chat, "node_url", return_value=None):
            self.assertEqual(ow._chat(NOW), [])
        with mock.patch.object(chat, "node_url",
                               return_value="http://127.0.0.1:1"), \
                mock.patch.object(chat, "node_head",
                                  return_value={"chain_index": 4}):
            self.assertEqual(ow._chat(NOW), [])
        with mock.patch.object(chat, "node_url",
                               return_value="http://127.0.0.1:1"), \
                mock.patch.object(chat, "node_head", return_value=None), \
                mock.patch.object(chatnode, "boot_diagnosis",
                                  return_value={"state": "down"}):
            self.assertEqual([w for w, _y in ow._chat(NOW)], ["stormy"])

    def test_a_silent_chat_node_is_stormy_unless_it_is_booting(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal 5-tuple whose every case asserts a NON-empty finding list
        ow = _ow()
        from helm import chat, chatnode
        for state, word in (("down", "stormy"), ("hung", "stormy"),
                            ("unknown", "stormy"),
                            ("initializing", "cloudy"),
                            ("preparing", "cloudy")):
            with self.subTest(state=state), \
                    mock.patch.object(chat, "node_url",
                                      return_value="http://127.0.0.1:1"), \
                    mock.patch.object(chat, "node_head", return_value=None), \
                    mock.patch.object(chatnode, "boot_diagnosis",
                                      return_value={"state": state,
                                                    "line": None,
                                                    "remedy": None}):
                got = ow._chat(NOW)
                self.assertEqual([w for w, _y in got], [word])
                self.assertIn("chat node", got[0][1])

    def _stall_state(self, state):
        from helm import pressurewatch
        path = pressurewatch.state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def test_an_open_stall_episode_is_stormy_and_a_stale_watcher_cloudy(self):
        ow = _ow()
        self.assertEqual(ow._stall(NOW), [], "no watcher on this host")
        episode = {"since": NOW - 300, "event": "e", "body": "b", "push": "p"}
        self._stall_state({"last": {"at": NOW - 30}, "episode": episode})
        got = ow._stall(NOW)
        self.assertEqual([w for w, _y in got], ["stormy"])
        self.assertIn("the fleet is stalled", got[0][1])
        self._stall_state({"last": {"at": NOW - 30}})
        self.assertEqual(ow._stall(NOW), [])
        self._stall_state({"last": {"at": NOW - ow.STALL_STALE_S - 60},
                           "episode": episode})
        got = ow._stall(NOW)
        self.assertEqual([w for w, _y in got], ["cloudy"])
        self.assertIn("pressure-watch has not read the fleet", got[0][1])

    def test_the_burn_flags_storm_only_when_every_measured_family_is_red(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal 4-tuple; two of its cases assert a non-empty finding on the same reader
        ow = _ow()
        from helm import burnflags as bf

        def snap(**colours):
            return ({"families": {f: {"colour": c}
                                  for f, c in colours.items()}}, 10.0)
        cases = ((snap(kimi=bf.RED, codex=bf.RED, local=bf.GREY), ["stormy"]),
                 (snap(kimi=bf.RED, codex=bf.GREEN), []),
                 (snap(kimi=bf.GREY), []),
                 ((None, None), ["cloudy"]))
        for value, words in cases:
            with self.subTest(value=value), \
                    mock.patch.object(bf, "cached_snapshot",
                                      return_value=value):
                self.assertEqual([w for w, _y in ow._burn(NOW)], words)

    def _land(self, control=({"paused": None}, None), active=(None, None),
              installed=True, census=([], None)):
        ow = _ow()
        from helm import autoland, timerhealth
        with mock.patch.object(ow, "_land_root", return_value=self.tmp), \
                mock.patch.object(autoland, "read_control",
                                  return_value=control), \
                mock.patch.object(autoland, "active", return_value=active), \
                mock.patch.object(autoland, "timer_installed",
                                  return_value=installed), \
                mock.patch.object(timerhealth, "census",
                                  return_value=census) as cen:
            return ow._land(NOW), cen

    def test_a_stopped_or_dead_land_train_is_stormy_a_paused_one_cloudy(self):  # noqa: VACUOUS_ASSERTION — the empty control is followed by three unconditional non-empty findings on the same reader
        from helm import autoland, timerhealth
        got, _c = self._land()
        self.assertEqual(got, [], "control: an idle train with a live timer")
        got, _c = self._land(active=({"state": autoland.STOPPED,
                                      "name": "train9", "train": "t9",
                                      "stopped": {"why": "the gate is red"}},
                                     None))
        self.assertEqual([w for w, _y in got], ["stormy"])
        self.assertIn("train9 is STOPPED: the gate is red", got[0][1])
        got, _c = self._land(control=({"paused": {"by": "lead"}}, None))
        self.assertEqual(got, [("cloudy", "auto-land is paused by lead")])
        got, _c = self._land(census=([(autoland.TIMER_NAME, timerhealth.TRAP,
                                       "inactive", "enabled"),
                                      ("other.timer", timerhealth.TRAP,
                                       "inactive", "enabled")], None))
        self.assertEqual([w for w, _y in got], ["stormy"])
        self.assertIn("the land train is dead", got[0][1])
        got, cen = self._land(installed=False,
                              census=([(autoland.TIMER_NAME, timerhealth.TRAP,
                                        "inactive", "enabled")], None))
        self.assertEqual(got, [])
        self.assertEqual(cen.call_count, 0, "no auto-land timer, no census")

    def test_the_stewards_come_from_the_declared_table(self):
        ow = _ow()
        from helm import burnflags

        def steward(component, project=None):
            return {("credentials", None): ("credseat", None),
                    ("local-serving", None): ("gpuseat", None),
                    ("project-seats", "proj"): ("projlead", None),
                    ("build-lanes", None): ("integ", None),
                    ("helm-friction", None): ("friction-seat", None)}.get(
                        (component, project), (None, "undeclared"))
        fake = types.SimpleNamespace(
            steward=steward,
            project_of=lambda seats: {s: ("proj" if s == "inproj" else None)
                                      for s in seats})
        moods = [mood("cloudwall", "walled", family="kimi"),
                 mood("gpuwall", "walled", family="localfam"),
                 mood("inproj", "stuck"), mood("loose", "grinding"),
                 mood("projlead", "stuck")]
        with seatevents_as(fake), \
                mock.patch.object(burnflags, "local_families",
                                  return_value=("localfam",)):
            got = ow._stewards(moods)
        self.assertEqual(got, {"cloudwall": ("credseat", None),
                               "gpuwall": ("gpuseat", None),
                               "inproj": ("projlead", None),
                               "loose": ("integ", None),
                               "projlead": ("integ", None)})

    def test_self_steward_falls_back_to_friction_steward(self):
        ow = _ow()
        from helm import burnflags
        calls = []

        def steward(component, project=None):
            calls.append((component, project))
            return {"project-seats": ("Lead", None),
                    "helm-friction": ("friction-seat", None),
                    "build-lanes": ("Lead", None)}[component]

        fake = types.SimpleNamespace(
            steward=steward, project_of=lambda seats: {s: "proj" for s in seats})
        seats = [mood("Lead", "stuck"), mood("friction-seat", "flowing")]
        with seatevents_as(fake), mock.patch.object(
                burnflags, "local_families", return_value=()):
            table = ow._stewards(seats)
        self.assertEqual(table["lead"], ("friction-seat", None))
        self.assertIn(("helm-friction", None), calls)
        word, line, _reasons = ow.weather(NOW, reads(
            seats, stewards={"Lead": table["lead"][0]}))
        self.assertEqual(word, "cloudy")
        self.assertIn("friction-seat has it", line)

    def test_no_steward_table_names_where_it_comes_from(self):
        ow = _ow()
        with seatevents_as(None):
            got = ow._stewards([mood("alpha", "stuck")])
        self.assertEqual(list(got), ["alpha"])
        self.assertIsNone(got["alpha"][0])
        self.assertIn("task/3876", got["alpha"][1])


# ------------------------------------------------------------------ ARM A
#
# The reader and its classifier are the NEW surface (task/3939). The WORD and
# SOURCES fakes replace `_causes`; these drive the REAL reader with `p0_causes`
# mocked, so a failure to classify by provenance (never by priority or seat
# count) reds here.

class CausesTest(World):

    def _causes(self, causes):
        """The real `_causes` reader with `p0_causes` mocked, driving the
        real `_land_path` classifier."""
        ow = _ow()
        with mock.patch(
                "helm.frictionpilot.p0_causes", return_value=causes):
            return ow._causes(NOW)

    def test_a_land_hook_refusal_turns_the_weather_stormy(self):  # noqa: VACUOUS_ASSERTION — the stormy finding and its source are both asserted; a non-firing reader returns [] which IS the claim
        got = self._causes([{"key": "helm|pre-push", "row": "r", "count": 1,
                             "seats": 1}])
        self.assertEqual([w for w, _ in got], ["stormy"])
        self.assertIn("land-path P0 cause", got[0][1])

    def test_a_pre_merge_commit_refusal_turns_the_weather_stormy(self):
        got = self._causes([{"key": "helm|pre-merge-commit", "row": "r",
                             "count": 2, "seats": 2}])
        self.assertEqual([w for w, _ in got], ["stormy"])

    def test_a_stopped_train_guard_named_turns_the_weather_stormy(self):  # noqa: VACUOUS_ASSERTION — the stormy finding is the positive control; the [] arm is the same reader with a stop text that names another guard
        # The reason is NOT a land hook, so only the stopped train's own
        # words (which name this cause's guard) make it land-path.
        cause = [{"key": "laneguard|some-plain-P0", "row": "r", "count": 1,
                  "seats": 1}]
        with mock.patch("helm.frictionpilot.land_stop_text",
                        return_value="laneguard refused the train's lane"):
            got = self._causes(cause)
        self.assertEqual([w for w, _ in got], ["stormy"])
        with mock.patch("helm.frictionpilot.land_stop_text",
                        return_value="another guard refused the lane"):
            self.assertEqual(self._causes(cause), [])

    def test_an_empty_guard_is_never_named_by_the_stop_text(self):  # noqa: VACUOUS_ASSERTION — test_a_stopped_train_guard_named_turns_the_weather_stormy drives the same reader to a stormy finding; the empty answer IS the claim
        """A cause filed with no guard keys "-" (`cause_key`); "-" as a word
        is in any stop text with a bare " - ", which must not make it
        land-path."""
        from helm import frictionpilot
        key = frictionpilot.cause_key("", "some plain refusal")
        self.assertTrue(key.startswith("-|"), key)
        with mock.patch("helm.frictionpilot.land_stop_text",
                        return_value="laneguard - refused the lane"):
            self.assertEqual(self._causes([{"key": key, "row": "r",
                                            "count": 1, "seats": 1}]), [])
        self.assertFalse(frictionpilot._names("", "a - b"))

    def test_a_plain_p0_is_not_land_path_and_stays_cloudy(self):  # noqa: VACUOUS_ASSERTION — the non-stormy word and the empty finding are both asserted; the claim is that a non-land-path P0 never pages the owner
        got = self._causes([{"key": "helm|some-plain-P0", "row": "r",
                             "count": 1, "seats": 1}])
        self.assertEqual(got, [])

    def test_a_p0_by_five_seats_stays_not_stormy(self):  # noqa: VACUOUS_ASSERTION — the empty finding and non-stormy word are both asserted; the claim is that seat count never lands a cause on the stormy surface
        ow = _ow()
        got = self._causes([{"key": "helm|some-plain-P0", "row": "r",
                             "count": 5, "seats": 5}])
        self.assertEqual(got, [])
        self.assertEqual(ow.weather(NOW, reads(
            [mood("alpha", "flowing")], causes=got))[0], "sunny")


class FleetOneDefinitionTest(World):
    """FLEET-DOWN HAS ONE DEFINITION (task/3939): the REAL `_fleet` over a
    faked census and a real roster file, judged by beacon_phone's qualifier.
    The steward down, or a strict majority (at least two) of the eligible
    seats unreachable, over a COMPLETE census that matches the roster's
    attendance register, turns the weather stormy; a RESTING or stray row
    cannot hold the storm off, and an incomplete census never storms."""

    SEATS = ("ops-steward", "beta", "gamma")

    def setUp(self):
        super().setUp()
        os.environ.pop("HELM_STEWARD_SEAT", None)
        from helm import seats_roster
        self.path = os.path.join(self.tmp, ".roster.json")
        patch = mock.patch.object(seats_roster, "roster_path",
                                  return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def census(self, states=None, seats=SEATS, sampled=None, probes=True,
               now=NOW):
        """What one `helm beacons --post` pass leaves: the attendance register
        for `seats` and, beside it, the census it attended (stamped with the
        register's `at`), which is handed back; `sampled` limits the census
        to some of them. Written by hand in the shape `beacons.attended`
        records, so an arm reds on behaviour, not on a missing name."""
        from helm import beacons
        states, rows, roster = states or {}, [], {}
        for name in seats:
            verdict = states.get(name, beacons.COVERED)
            row = {"seat": name, "verdict": verdict,
                   "live": verdict == beacons.COVERED}
            roster[name] = {"session": "fixture", "attendance": {
                "covered": NOW - 500, "at": now, "since": now,
                "state": verdict, "alarm": beacons.unreachable(row)}}
            if sampled is None or name in sampled:
                rows.append(row)
        rep = {"seats": rows, "live_probe": probes, "agent_probe": probes}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(roster, f)
        with open(self.path + ".census.json", "w", encoding="utf-8") as f:
            json.dump(dict(rep, at=now), f)
        return rep

    @staticmethod
    def raw(rep, states):
        """A census taken at ANOTHER instant than the one attended: the same
        rows with `states` {seat: verdict} as that instant read them."""
        from helm import beacons
        rows = [dict(r, verdict=states.get(r["seat"], r["verdict"]),
                     live=states.get(r["seat"], r["verdict"])
                     == beacons.COVERED) for r in rep["seats"]]
        return dict(rep, seats=rows)

    @staticmethod
    def floor(moods=None):
        r = reads(moods or [mood("alpha", "flowing")])
        del r["fleet"]                       # the REAL reader, never a word
        return r

    def weather(self, rep, now=NOW):
        with mock.patch("helm.beacons.census", return_value=rep):
            return _ow().weather(now, self.floor())

    def tick(self, rep, now, phone, moods=None):
        """One timer pass. `rep` is what a census taken by the weather itself
        would read (the retired design); the weather must judge the census
        `beacons --post` recorded, so a `rep` that differs never decides."""
        with mock.patch("helm.beacons.census", return_value=rep):
            return _ow().tick(now=now, push=True, reads=self.floor(moods),
                              phone=phone)

    def latch(self, delivered, cohort=SEATS):
        with open(self.path + ".phone.json", "w", encoding="utf-8") as f:
            json.dump({"phase": "down", "cohort": sorted(cohort),
                       "steward": "ops-steward", "delivered": delivered}, f)

    def fleet_reasons(self, reasons):
        return [r for r in reasons if "fleet" in r["why"]]

    # ---- the qualifier

    def test_the_steward_down_with_the_other_seats_up_turns_stormy(self):
        from helm import beacons
        word, line, reasons = self.weather(
            self.census({"ops-steward": beacons.DEAF}))
        self.assertEqual(word, "stormy", line)
        self.assertIn("steward", self.fleet_reasons(reasons)[0]["why"])

    def test_two_of_three_eligible_seats_unreachable_turns_stormy(self):
        from helm import beacons
        word, line, reasons = self.weather(
            self.census({"beta": beacons.DEAF, "gamma": beacons.DEAF}))
        self.assertEqual(word, "stormy", line)
        self.assertIn("2 of 3 seats", self.fleet_reasons(reasons)[0]["why"])

    def test_one_of_three_eligible_seats_unreachable_does_not_storm(self):
        from helm import beacons
        word, line, reasons = self.weather(
            self.census({"beta": beacons.DEAF}))
        self.assertEqual(word, "sunny", line)
        self.assertEqual(self.fleet_reasons(reasons), [])

    def test_an_incomplete_census_does_not_storm_and_says_so(self):  # noqa: VACUOUS_ASSERTION — the complete census of the same rows storming is the unconditional positive control, asserted before the loop
        from helm import beacons
        down = {s: beacons.DEAF for s in self.SEATS}
        self.assertEqual(self.weather(self.census(down))[0], "stormy",
                         "control: the same rows, complete, storm")
        for kw, said in (({"probes": False}, "incomplete"),
                         ({"sampled": ("beta", "gamma")},
                          "the beacons census lists 2 of 3 seats")):
            with self.subTest(**kw):
                word, line, reasons = self.weather(self.census(down, **kw))
                self.assertNotEqual(word, "stormy", line)
                self.assertIn(said, " ".join(r["why"] for r in reasons))

    def test_a_census_short_of_the_roll_is_cloudy_and_counts_it(self):  # noqa: VACUOUS_ASSERTION — the whole-roll census reading sunny is the unconditional control before the loop, and each subtest asserts its reason by value
        """task/3939 item 4: a recorded census with no rows, or lacking seats
        the register holds on the roll, establishes nothing. It is the cloudy
        unread reason with the count, never stormy and never silent."""
        word, line, _reasons = self.weather(self.census())
        self.assertEqual(word, "sunny", "control: the whole roll is clear")
        for sampled, n in (((), 0), (("beta", "gamma"), 2)):
            with self.subTest(listed=n):
                word, line, reasons = self.weather(self.census(
                    sampled=sampled))
                self.assertEqual(word, "cloudy", line)
                why = [r for r in reasons if "census" in r["why"]]
                self.assertEqual(why[0]["why"], "fleet-down is not "
                                 "established: the beacons census lists %d "
                                 "of 3 seats" % n)
                self.assertTrue(why[0].get("unread"))

    def test_a_cohort_seat_that_left_the_roster_is_owed_by_name(self):
        """task/3939 item 5: after a heard storm, gamma leaves the roster
        entirely and beta covers. Shrinking the roll never manufactures a
        clear, and the seat it still waits on is named."""
        phone = Phone()
        last, _down = self.settle_storm(phone)
        at = last + 300
        got = self.tick(self.census(seats=("ops-steward", "beta"), now=at),
                        at, phone)
        self.assertEqual(got["word"], "stormy", got["line"])
        why = " ".join(r["why"] for r in self.fleet_reasons(got["reasons"]))
        self.assertIn("1 of the episode's seats is not covered yet: "
                      "gamma (off the roster)", why)
        self.assertNotIn("beta", why)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)

    def test_reopens_only_on_a_different_outage(self):
        """task/3939 rule 2, the one pure function: after an expired
        episode, a down verdict whose unreachable seats are all among the
        expired episode's owed seats does not reopen; any other seat down,
        or no expired episode, does."""
        ow = _ow()
        expired = {"owed": ["Beta", "gamma"]}
        self.assertTrue(ow._reopens(None, ["beta"]))
        self.assertFalse(ow._reopens(expired, ["beta", "gamma"]))
        self.assertFalse(ow._reopens(expired, ["GAMMA"]))
        self.assertTrue(ow._reopens(expired, ["beta", "ops-steward"]))
        self.assertFalse(ow._reopens(expired, []))

    def test_a_malformed_expired_record_is_dropped_not_trusted(self):  # noqa: VACUOUS_ASSERTION — the well-formed record kept by value is the unconditional positive control before the loop
        """An expired episode whose `gone` or `page` is not the shape
        `_expire` writes is dropped: kept, it would raise in its own reason
        on every pass and mute the fleet reader for good."""
        ow = _ow()
        good = {"at": NOW, "owed": ["beta"], "gone": [], "page": None}
        self.assertEqual(ow._fleet_record({"expired": good})["expired"], good,
                         "control: the shape _expire writes is kept")
        for bad in ({"gone": [1, 2]}, {"gone": "beta"}, {"page": 7}):
            with self.subTest(**{k: repr(v) for k, v in bad.items()}):
                self.assertIsNone(ow._fleet_record(
                    {"expired": dict(good, **bad)})["expired"])

    def test_a_recorded_instant_the_clock_cannot_print_is_dropped(self):  # noqa: VACUOUS_ASSERTION — the well-formed records kept by value are the unconditional positive controls before each loop
        """An expired episode whose `at` or `opened` is NaN, infinite or
        beyond the platform clock is dropped (kept, its reason raises every
        pass and mutes the fleet reader), and an open episode's `opened` or
        `heard` that is no instant reads as unknown (kept, a NaN or infinite
        `heard` never reaches the bound, or reaches it at once)."""
        ow = _ow()
        good = {"at": NOW, "opened": NOW - 7200, "owed": ["beta"], "gone": [],
                "page": None}
        self.assertEqual(ow._fleet_record({"expired": good})["expired"], good,
                         "control: the shape _expire writes is kept")
        for bad in ({"at": float("inf")}, {"at": float("nan")}, {"at": 1e300},
                    {"opened": float("-inf")}, {"opened": -1e300}):
            with self.subTest(**{k: repr(v) for k, v in bad.items()}):
                self.assertIsNone(ow._fleet_record(
                    {"expired": dict(good, **bad)})["expired"])
        ep = {"phase": "down", "cohort": ["beta", "gamma"], "steward": None,
              "delivered": True}
        rec = {"episode": ep, "opened": NOW - 60, "heard": NOW}
        got = ow._fleet_record(rec)
        self.assertEqual((got["opened"], got["heard"]), (NOW - 60, NOW),
                         "control: recorded instants are kept")
        for bad in (float("inf"), float("-inf"), float("nan"), 1e300):
            with self.subTest(instant=repr(bad)):
                got = ow._fleet_record(dict(rec, opened=bad, heard=bad))
                self.assertEqual((got["opened"], got["heard"]), (None, None))

    def test_a_resting_seat_does_not_hold_off_a_majority_storm(self):
        from helm import beacons
        word, line, _reasons = self.weather(self.census(
            {"beta": beacons.DEAF, "gamma": beacons.DEAF,
             "delta": beacons.RESTING},
            seats=self.SEATS + ("delta",)))
        self.assertEqual(word, "stormy", line)

    def test_a_heard_fleet_storm_holds_until_its_whole_cohort_covers(self):
        from helm import beacons
        ow, phone = _ow(), Phone()
        self.tick(self.census(), NOW, phone)
        down = self.census({"beta": beacons.DEAF, "gamma": beacons.DEAF})
        self.tick(down, NOW + 10, phone)
        got = self.tick(down, NOW + 10 + ow.HOLD_S, phone)
        self.assertEqual(got["phone"], "pushed")
        # a majority no longer, but the episode's cohort is not all covered
        later = NOW + 20 + ow.HOLD_S
        got = self.tick(self.census({"gamma": beacons.DEAF}, now=later),
                        later, phone)
        self.assertEqual(got["word"], "stormy", got["line"])
        self.assertEqual(len(phone.pushes), 1)

    def test_a_rested_cohort_seat_lets_a_heard_storm_end_once(self):
        """After a heard fleet-down storm the owner rests one cohort seat and
        the others cover again. An open episode on a fleet that is up says it
        WAS down (never "the fleet is down"), and the rested seat does not
        hold the storm, nor every storm after it, open: one all-clear."""
        from helm import beacons
        ow, phone = _ow(), Phone()
        last, _down = self.settle_storm(phone)
        at = last + 300
        got = self.tick(self.census({"gamma": beacons.DEAF}, now=at), at,
                        phone)
        self.assertEqual(got["word"], "stormy", got["line"])
        why = " ".join(r["why"] for r in self.fleet_reasons(got["reasons"]))
        self.assertIn("the fleet was down and 1 of the episode's seats", why)
        self.assertNotIn("the fleet is down", why)
        for at in range(int(at) + 300, int(last + ow.GAP_S + 3 * ow.HOLD_S),
                        300):
            got = self.tick(self.census({"gamma": beacons.RESTING}, now=at),
                            at, phone)
        self.assertEqual(got["settled"]["word"], "sunny", got["line"])
        self.assertEqual(len(phone.pushes), 2, phone.pushes)
        self.assertTrue(phone.pushes[1][0].startswith("all clear: "))

    # ---- one observation, one owner: the census beacons attended

    def passes(self, down, raw_states, phone, start=NOW, n=6, step=300,
               seats=SEATS):
        """A sunny baseline, then `n` timer passes `step` apart on a fleet
        whose register and recorded census read `down`, while a census taken
        at the weather's own instant reads `raw_states` (a held or flapping
        seat) -> [(word, why of the fleet reasons)] per pass."""
        self.tick(self.census(now=start), start, phone)
        out = []
        for i in range(1, n + 1):
            at = start + i * step
            rep = self.census(down, seats=seats, now=at)
            states = raw_states(i) if callable(raw_states) else raw_states
            got = self.tick(self.raw(rep, states), at, phone)
            out.append((got["word"], [r["why"] for r in got["reasons"]
                                      if "fleet" in r["why"]]))
        return out

    def test_a_seat_held_deaf_in_effect_storms_within_the_debounce(self):
        """THE PROBE, arm one: the steward DEAF and beta HELD at
        DEAF-IN-EFFECT by attendance (its drain unproven), while a census of
        the weather's own reads beta COVERED. The register and the census
        beacons recorded agree, so the storm settles on the second pass and
        pages once over 25 minutes."""
        from helm import beacons
        phone = Phone()
        got = self.passes({"ops-steward": beacons.DEAF,
                           "beta": beacons.DEAF_IN_EFFECT},
                          {"beta": beacons.COVERED}, phone)
        self.assertEqual([w for w, _ in got], ["stormy"] * 6, got)
        self.assertIn("steward", got[0][1][0])
        self.assertEqual(len(phone.pushes), 1, phone.pushes)
        self.assertIn("the fleet is down", phone.pushes[0][0])

    def test_a_seat_flapping_busy_covered_waking_storms_within_the_debounce(self):  # noqa: VACUOUS_ASSERTION — six stormy words and exactly one push are asserted by value on the real tick
        """THE PROBE, arm two: beta reads BUSY in the census beacons
        attended, and COVERED or WAKING to a census taken a moment later. The
        flap is not an observation the weather makes, so it never resets the
        stormy candidate."""
        from helm import beacons
        phone = Phone()
        flap = (beacons.COVERED, beacons.WAKING, beacons.BUSY)
        got = self.passes({"ops-steward": beacons.DEAF, "beta": beacons.BUSY},
                          lambda i: {"beta": flap[i % 3]}, phone)
        self.assertEqual([w for w, _ in got], ["stormy"] * 6, got)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)

    def test_the_weather_takes_no_census_of_its_own(self):  # noqa: VACUOUS_ASSERTION — the stormy word on the same read is the unconditional positive; the uncalled census IS the claim
        from helm import beacons
        self.census({"ops-steward": beacons.DEAF})
        with mock.patch("helm.beacons.census") as taken:
            word, line, _reasons = _ow().weather(NOW, self.floor())
        self.assertEqual(word, "stormy", line)
        taken.assert_not_called()

    def test_a_stale_recorded_census_never_storms_and_says_how_old(self):  # noqa: VACUOUS_ASSERTION — the loop is a literal six passes each asserting a cloudy unread reason naming its age, and the fresh control storms unconditionally after it
        """The beacons timer stopped 20 minutes ago with the steward DEAF. A
        census that old speaks for nothing now: no storm and no page, and the
        reason names its age; a fresh census of the same rows storms."""
        from helm import beacons
        phone = Phone()
        down = {"ops-steward": beacons.DEAF}
        self.tick(self.census(), NOW - 1200, phone)
        old = self.census(down, now=NOW - 1200)
        for i in range(6):
            at = NOW + i * 300
            got = self.tick(old, at, phone)
            self.assertNotEqual(got["word"], "stormy", got["line"])
            why = [r for r in got["reasons"] if "fleet" in r["why"]]
            self.assertEqual(why[0]["word"], "cloudy")
            self.assertIn("the beacons census is %d min old"
                          % ((at - (NOW - 1200)) // 60), why[0]["why"])
            self.assertTrue(why[0].get("unread"))
        self.assertEqual(phone.pushes, [])
        self.assertEqual(self.weather(self.census(down, now=NOW + 1500),
                                      now=NOW + 1500)[0], "stormy",
                         "control: the same rows, fresh, storm")

    def settle_storm(self, phone):
        from helm import beacons
        ow = _ow()
        down = {"beta": beacons.DEAF, "gamma": beacons.DEAF}
        self.tick(self.census(), NOW, phone)
        self.tick(self.census(down, now=NOW + 10), NOW + 10, phone)
        at = NOW + 10 + ow.HOLD_S
        got = self.tick(self.census(down, now=at), at, phone)
        self.assertEqual(got["phone"], "pushed")
        return at, down

    def test_a_stale_census_does_not_clear_a_settled_storm(self):  # noqa: VACUOUS_ASSERTION — settle_storm asserts the push first; every pass asserts a stormy word and settled storm, and the fixture asserts the census went stale
        """The phone heard the storm; then the beacons timer stops. The last
        census it recorded ages past STALE_S while a census of the weather's
        own would read every seat covered: only a FRESH clear ends the storm,
        so the word stays stormy, says why, and no all-clear is sent."""
        from helm import beacons, beacon_phone
        ow, phone = _ow(), Phone()
        last, _down = self.settle_storm(phone)
        up = self.raw(self.census(now=last), {})
        # the register and the recorded census stay as the last pass left
        # them (the timer stopped), so rewrite them down as of `last`
        self.census({"beta": beacons.DEAF, "gamma": beacons.DEAF}, now=last)
        for at in range(int(last) + 300, int(last + ow.GAP_S + 3 * ow.HOLD_S),
                        300):
            got = self.tick(up, at, phone)
            self.assertEqual(got["word"], "stormy", got["line"])
            self.assertEqual(got["settled"]["word"], "stormy")
        self.assertIn("min old", " ".join(r["why"] for r in got["reasons"]))
        self.assertGreater(at - last, beacon_phone.STALE_S,
                           "fixture: the census never went stale")
        self.assertEqual(len(phone.pushes), 1, phone.pushes)

    def test_a_fresh_clear_ends_the_settled_storm_with_one_all_clear(self):
        ow, phone = _ow(), Phone()
        last, _down = self.settle_storm(phone)
        for at in range(int(last) + 300, int(last + ow.GAP_S + 3 * ow.HOLD_S),
                        300):
            got = self.tick(self.census(now=at), at, phone)
        self.assertEqual(got["settled"]["word"], "sunny", got["line"])
        self.assertEqual(len(phone.pushes), 2, phone.pushes)
        self.assertTrue(phone.pushes[1][0].startswith("all clear: "))

    # ---- the cutover's latch

    def test_the_migrated_latch_is_not_adopted_twice(self):
        """After the cutover adopts the retired pager's latch it is renamed
        `.migrated`; a lost weather record then finds nothing to adopt, so
        the all-clear the latch owed goes out once, never twice."""
        ow, phone = _ow(), Phone()
        self.latch(delivered=True)
        for at in (NOW, NOW + ow.HOLD_S, NOW + ow.GAP_S + ow.HOLD_S):
            self.tick(self.census(now=at), at, phone)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)
        os.unlink(ow.state_path())           # the weather's record is lost
        base = NOW + 2 * ow.GAP_S
        for at in (base, base + ow.HOLD_S, base + ow.GAP_S + ow.HOLD_S,
                   base + 2 * ow.GAP_S):
            self.tick(self.census(now=at), at, phone)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)
        self.assertFalse(os.path.exists(self.path + ".phone.json"))
        self.assertTrue(os.path.exists(self.path + ".phone.json.migrated"))

    def test_a_delivered_latch_whose_outage_cleared_sends_one_all_clear(self):
        ow, phone = _ow(), Phone()
        self.latch(delivered=True)
        with mock.patch.object(ow, "_beacon_latch",
                               wraps=ow._beacon_latch) as read:
            for at in (NOW, NOW + ow.HOLD_S, NOW + ow.GAP_S + ow.HOLD_S,
                       NOW + 2 * ow.GAP_S):
                self.tick(self.census(now=at), at, phone)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)
        self.assertTrue(phone.pushes[0][0].startswith("all clear: "))
        self.assertEqual(read.call_count, 1, "the latch is read once")

    def test_an_undelivered_latch_whose_outage_cleared_sends_nothing(self):  # noqa: VACUOUS_ASSERTION — the positive control on phone.pushes is test_an_undelivered_latch_for_a_majority_still_down_pages_once (same latch, outage still on: one page)
        # The floor is stormy for ANOTHER reason at the cutover (an owner
        # decision); a first reading is a baseline that pushes nothing, and a
        # page the beacons pager never sent is not owed once the outage ended.
        ow, phone = _ow(), Phone()
        self.latch(delivered=False)
        storm = [mood("alpha", "blocked-on-owner")]
        for at in (NOW, NOW + ow.HOLD_S, NOW + ow.GAP_S + ow.HOLD_S):
            self.tick(self.census(now=at), at, phone, moods=storm)
        self.assertEqual(phone.pushes, [])

    def test_an_undelivered_latch_for_a_majority_still_down_pages_once(self):
        from helm import beacons
        ow, phone = _ow(), Phone()
        self.latch(delivered=False)
        for at in (NOW, NOW + ow.HOLD_S, NOW + 2 * ow.HOLD_S):
            self.tick(self.census({"beta": beacons.DEAF,
                                   "gamma": beacons.DEAF}, now=at), at, phone)
        self.assertEqual(len(phone.pushes), 1, phone.pushes)
        self.assertFalse(phone.pushes[0][0].startswith("all clear"))

    def test_a_delivered_latch_for_an_outage_still_down_is_not_repeated(self):  # noqa: VACUOUS_ASSERTION — the settled storm is the unconditional positive on the same pass; the empty phone is the claim (the page already went out)
        from helm import beacons
        ow, phone = _ow(), Phone()
        self.latch(delivered=True)
        for at in (NOW, NOW + ow.HOLD_S, NOW + 2 * ow.HOLD_S):
            got = self.tick(self.census({"beta": beacons.DEAF,
                                         "gamma": beacons.DEAF}, now=at),
                            at, phone)
        self.assertEqual(got["settled"]["word"], "stormy")
        self.assertEqual(phone.pushes, [])


# ------------------------------------------------------------------ PUSH

class PushTest(World):

    def tick(self, now, moods, phone, push=True, **fleet):
        return _ow().tick(now=now, push=push, reads=reads(moods, **fleet),
                          phone=phone)

    def test_the_first_reading_is_a_baseline_even_when_stormy(self):  # noqa: VACUOUS_ASSERTION — the word and the settled word are asserted stormy on the same pass first; no push IS the claim
        phone = Phone()
        got = self.tick(NOW, [mood("alpha", "blocked-on-owner")], phone)
        self.assertEqual(got["word"], "stormy")
        self.assertEqual(got["settled"]["word"], "stormy")
        self.assertEqual(phone.pushes, [])
        self.assertIn("baseline", got["phone"])

    def test_a_change_pushes_once_after_it_holds(self):
        ow, phone = _ow(), Phone()
        calm, storm = [mood("alpha", "flowing")], [
            mood("alpha", "blocked-on-owner", reason="card 7")]
        self.tick(NOW, calm, phone)
        got = self.tick(NOW + 60, storm, phone)
        self.assertEqual((got["settled"]["word"], phone.pushes),
                         ("sunny", []), "a new word waits out the hold")
        got = self.tick(NOW + 60 + ow.HOLD_S, storm, phone)
        self.assertEqual(got["settled"]["word"], "stormy")
        self.assertEqual(phone.pushes, [(got["line"], ow.TITLE)])
        self.assertIn("alpha waits on you", phone.pushes[0][0])
        self.assertEqual(got["phone"], "pushed")
        self.tick(NOW + 60 + 2 * ow.HOLD_S, storm, phone)
        self.assertEqual(len(phone.pushes), 1, "the same word never re-pushes")

    def test_a_flap_between_passes_never_reaches_the_phone(self):  # noqa: VACUOUS_ASSERTION — test_a_change_pushes_once_after_it_holds is the positive control on the same phone fake and the same tick; the settled word is asserted here too
        ow, phone = _ow(), Phone()
        calm, storm = [mood("alpha", "flowing")], [
            mood("alpha", "blocked-on-owner")]
        t = NOW
        self.tick(t, calm, phone)
        for _ in range(6):
            t += ow.HOLD_S
            self.tick(t, storm, phone)
            t += ow.HOLD_S
            got = self.tick(t, calm, phone)
        self.assertEqual(phone.pushes, [])
        self.assertEqual(got["settled"]["word"], "sunny")

    def test_an_all_clear_waits_out_the_gap_and_a_storm_does_not(self):
        ow, phone = _ow(), Phone()
        calm = [mood("alpha", "flowing")]
        cloud = [mood("alpha", "grinding")]
        storm = [mood("alpha", "blocked-on-owner")]
        t = NOW
        self.tick(t, calm, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, storm, phone)
        self.assertEqual(len(phone.pushes), 1)
        stormed = t
        for t in (t + 10, t + 10 + ow.HOLD_S):
            got = self.tick(t, cloud, phone)
        self.assertEqual(got["settled"]["word"], "cloudy")
        self.assertEqual(len(phone.pushes), 1, "inside the gap: held")
        self.assertIn("held", got["phone"])
        got = self.tick(stormed + ow.GAP_S + 1, cloud, phone)
        self.assertEqual(len(phone.pushes), 2)
        self.assertIn("cloudy", phone.pushes[1][0])
        # a storm inside the gap goes at once
        t = stormed + ow.GAP_S + 1
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, storm, phone)
        self.assertEqual(len(phone.pushes), 3)
        self.assertIn("stormy", phone.pushes[2][0])

    def test_a_calm_change_is_shown_and_never_pushed(self):  # noqa: VACUOUS_ASSERTION — each settled word and its "shown, not pushed" phrase are asserted on the pass that settles it, and test_a_storm_pushes_and_its_all_clear_follows_once pushes through the same Phone fake; no push IS the claim
        """THE OWNER'S PAGING RULE: "A P0 cause turns the office weather
        stormy, and nothing else pages him." Sunny to cloudy and back
        settles, heads the surfaces, and never rings."""
        ow, phone = _ow(), Phone()
        calm, cloud = [mood("alpha", "flowing")], [mood("alpha", "grinding")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, cloud, phone)
        got = self.tick(NOW + 10 + ow.HOLD_S, cloud, phone)
        self.assertEqual(got["settled"]["word"], "cloudy")
        self.assertIn("shown, not pushed", got["phone"])
        self.assertEqual(ow.surface(NOW + 20 + ow.HOLD_S)["word"], "cloudy",
                         "the change is shown on the surfaces")
        t = NOW + 10 + ow.HOLD_S + ow.GAP_S
        self.tick(t, cloud, phone)
        self.tick(t + 10, calm, phone)
        got = self.tick(t + 10 + ow.HOLD_S, calm, phone)
        self.assertEqual(got["settled"]["word"], "sunny")
        self.assertIn("shown, not pushed", got["phone"])
        self.tick(t + 10 + ow.HOLD_S + ow.GAP_S, calm, phone)
        self.assertEqual(phone.pushes, [])

    def test_a_storm_pushes_and_its_all_clear_follows_once(self):
        ow, phone = _ow(), Phone()
        calm, cloud = [mood("alpha", "flowing")], [mood("alpha", "grinding")]
        storm = [mood("alpha", "blocked-on-owner", reason="card 7")]
        t = NOW
        self.tick(t, calm, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            got = self.tick(t, storm, phone)
        self.assertEqual((got["phone"], phone.pushes),
                         ("pushed", [(got["line"], ow.TITLE)]))
        stormed = t
        for t in (t + 10, t + 10 + ow.HOLD_S):
            got = self.tick(t, cloud, phone)
        self.assertEqual(len(phone.pushes), 1)
        self.assertIn("the all-clear waits", got["phone"])
        got = self.tick(stormed + ow.GAP_S + 1, cloud, phone)
        self.assertEqual(got["phone"], "pushed the all-clear")
        self.assertEqual(phone.pushes[1],
                         ("all clear: " + got["line"], ow.TITLE))
        # cloudy to sunny after the all-clear is a calm change: shown only
        t = stormed + ow.GAP_S + 1
        for t in (t + 10, t + 10 + ow.HOLD_S, t + 10 + ow.HOLD_S + ow.GAP_S):
            got = self.tick(t, calm, phone)
        self.assertEqual(got["settled"]["word"], "sunny")
        self.assertEqual(len(phone.pushes), 2)

    def test_a_recorded_instant_the_clock_cannot_print_reads_as_unknown(self):  # noqa: VACUOUS_ASSERTION — every arm asserts the storm push and the turning candidate by value before it corrupts, and the subTest matrix is a fixed literal that always runs
        """The weather's own instants (when the phone was last told, since
        when the floor was readable, since when a word was turning, when the
        last pass read) that are NaN, infinite or beyond the platform clock
        read as unknown: the pass never raises (a raising pass tells the
        phone nothing), the storm's all-clear still follows once, and the
        surface still speaks."""
        ow = _ow()
        cloud = [mood("alpha", "grinding")]
        storm = [mood("alpha", "blocked-on-owner")]
        for path in (("pushed", "at"), ("known_since",),
                     ("candidate", "since"), ("at",)):
            for bad in (float("inf"), float("-inf"), float("nan"), 1e300,
                        -1e300):
                with self.subTest(path=".".join(path), bad=repr(bad)):
                    if os.path.exists(ow.state_path()):
                        os.remove(ow.state_path())
                    phone, t = Phone(), NOW
                    self.tick(t, [mood("alpha", "flowing")], phone)
                    for t in (t + 10, t + 10 + ow.HOLD_S):
                        self.tick(t, storm, phone)
                    self.assertEqual(len(phone.pushes), 1,
                                     "control: the storm pushed")
                    stormed = t
                    self.tick(stormed + 10, cloud, phone)
                    st = ow._load()
                    self.assertIsNotNone(st["candidate"],
                                         "control: the floor is turning")
                    held = st
                    for k in path[:-1]:
                        held = held[k]
                    held[path[-1]] = bad
                    with open(ow.state_path(), "w") as f:
                        json.dump(st, f)
                    ow.surface(stormed + ow.GAP_S)
                    t = stormed + 10
                    while t < stormed + ow.GAP_S + 4 * ow.HOLD_S:
                        t += ow.HOLD_S
                        self.tick(t, cloud, phone)
                    self.assertEqual(len(phone.pushes), 2, phone.pushes)
                    self.assertTrue(phone.pushes[1][0].startswith(
                        "all clear: "), phone.pushes)

    def test_a_storm_the_phone_never_heard_has_no_all_clear(self):  # noqa: VACUOUS_ASSERTION — the pushes list holds the two failed storm tries (asserted by value), and the settled word and its phrase are asserted on the clearing pass
        ow, phone = _ow(), Phone(ok=False)
        calm, storm = [mood("alpha", "flowing")], [
            mood("alpha", "blocked-on-owner")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, storm, phone)
        got = self.tick(NOW + 10 + ow.HOLD_S, storm, phone)
        self.assertIn("owed", got["phone"])
        self.tick(NOW + 20 + ow.HOLD_S, calm, phone)
        got = self.tick(NOW + 20 + 2 * ow.HOLD_S, calm, phone)
        self.assertEqual(got["settled"]["word"], "sunny")
        self.assertIn("shown, not pushed", got["phone"])
        phone.ok = True
        self.tick(NOW + 20 + 2 * ow.HOLD_S + ow.GAP_S, calm, phone)
        # two tries at the storm, both failed; never an all-clear for it
        self.assertEqual([b for b, _t in phone.pushes],
                         [b for b, _t in phone.pushes[:1]] * 2)
        self.assertFalse(phone.pushes[0][0].startswith("all clear"))

    def test_an_all_clear_never_goes_while_the_storm_returns(self):
        ow, phone = _ow(), Phone()
        calm, cloud = [mood("alpha", "flowing")], [mood("alpha", "grinding")]
        storm = [mood("alpha", "blocked-on-owner")]
        t = NOW
        self.tick(t, calm, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, storm, phone)
        stormed = t
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, cloud, phone)
        self.tick(stormed + ow.GAP_S - 60, storm, phone)
        got = self.tick(stormed + ow.GAP_S + 1, storm, phone)
        self.assertEqual(got["settled"]["word"], "cloudy",
                         "the storm is still turning")
        self.assertIn("turning stormy", got["phone"])
        got = self.tick(stormed + ow.GAP_S + ow.HOLD_S, storm, phone)
        self.assertEqual(got["settled"]["word"], "stormy")
        self.assertEqual(len(phone.pushes), 1,
                         "the phone already holds the storm: no all-clear, "
                         "no second storm")

    def test_a_held_change_that_flaps_back_is_dropped(self):
        ow, phone = _ow(), Phone()
        calm, cloud = [mood("alpha", "flowing")], [mood("alpha", "grinding")]
        storm = [mood("alpha", "blocked-on-owner")]
        t = NOW
        self.tick(t, calm, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, storm, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            self.tick(t, cloud, phone)
        for t in (t + 10, t + 10 + ow.HOLD_S):
            got = self.tick(t, storm, phone)
        self.assertEqual(len(phone.pushes), 1,
                         "the phone already holds stormy: nothing changed "
                         "for it")
        got = self.tick(t + ow.GAP_S, storm, phone)
        self.assertEqual(len(phone.pushes), 1)
        self.assertEqual(got["phone"], "unchanged")

    def test_an_opted_out_phone_does_not_acknowledge_a_storm(self):
        ow, phone = _ow(), Phone(available=False)
        calm, storm = [mood("alpha", "idle")], [
            mood("alpha", "blocked-on-owner")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, storm, phone)
        got = self.tick(NOW + 10 + ow.HOLD_S, storm, phone)
        self.assertIn("no phone transport", got["phone"])
        self.assertEqual(phone.pushes, [])
        phone.available = True
        got = self.tick(NOW + 20 + ow.HOLD_S, storm, phone)
        self.assertEqual((got["phone"], len(phone.pushes)), ("pushed", 1))
        for t in (NOW + 30 + ow.HOLD_S,
                  NOW + 30 + 2 * ow.HOLD_S):
            self.tick(t, calm, phone)
        self.tick(NOW + 20 + ow.HOLD_S + ow.GAP_S + 1, calm, phone)
        self.assertEqual(len(phone.pushes), 2)
        self.assertTrue(phone.pushes[-1][0].startswith("all clear: "))

    def test_an_unreadable_floor_cannot_clear_a_heard_storm(self):
        ow, phone = _ow(), Phone()
        calm, storm = [mood("alpha", "idle")], [
            mood("alpha", "blocked-on-owner")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, storm, phone)
        self.tick(NOW + 10 + ow.HOLD_S, storm, phone)

        def broken(now):
            raise OSError("roster not readable")

        unread = dict(reads(), floor=broken)
        for t in (NOW + 20 + ow.HOLD_S,
                  NOW + 20 + 2 * ow.HOLD_S,
                  NOW + ow.HOLD_S + ow.GAP_S + 1):
            got = ow.tick(now=t, reads=unread, phone=phone)
        self.assertEqual(got["settled"]["word"], "cloudy")
        self.assertIn("readable floor", got["phone"])
        self.assertEqual(len(phone.pushes), 1)
        got = self.tick(t + 10, calm, phone)
        self.assertIn("readable floor", got["phone"])
        self.assertEqual(len(phone.pushes), 1)
        got = self.tick(t + 10 + ow.HOLD_S, calm, phone)
        self.assertEqual(got["phone"], "pushed the all-clear")
        self.assertEqual(len(phone.pushes), 2)

    def test_a_failed_push_stays_owed_and_lands_once(self):
        ow, phone = _ow(), Phone(ok=False)
        calm, storm = [mood("alpha", "flowing")], [
            mood("alpha", "blocked-on-owner")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, storm, phone)
        got = self.tick(NOW + 10 + ow.HOLD_S, storm, phone)
        self.assertEqual(len(phone.pushes), 1)
        self.assertIn("owed", got["phone"])
        phone.ok = True
        got = self.tick(NOW + 20 + ow.HOLD_S, storm, phone)
        self.assertEqual((len(phone.pushes), got["phone"]), (2, "pushed"))
        self.tick(NOW + 30 + ow.HOLD_S, storm, phone)
        self.assertEqual(len(phone.pushes), 2)

    def test_a_dry_pass_writes_nothing_and_pushes_nothing(self):  # noqa: VACUOUS_ASSERTION — the word is asserted stormy on the same pass; nothing written and nothing pushed IS the claim
        ow, phone = _ow(), Phone()
        got = self.tick(NOW, [mood("alpha", "blocked-on-owner")], phone,
                        push=False)
        self.assertEqual(got["word"], "stormy")
        self.assertFalse(os.path.exists(ow.state_path()))
        self.assertEqual(phone.pushes, [])

    def test_a_pass_that_cannot_take_the_lock_pushes_nothing(self):
        ow, phone = _ow(), Phone()
        calm, storm = [mood("alpha", "flowing")], [
            mood("alpha", "blocked-on-owner")]
        self.tick(NOW, calm, phone)
        self.tick(NOW + 10, storm, phone)
        with open(ow.state_path() + ".lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            got = self.tick(NOW + 10 + ow.HOLD_S, storm, phone)
        self.assertIn("another weather pass holds the lock", got["phone"])
        self.assertEqual(phone.pushes, [])
        self.tick(NOW + 20 + ow.HOLD_S, storm, phone)
        self.assertEqual(len(phone.pushes), 1, "control: unlocked, it pushes")

    def test_the_knobs_move_the_hold_and_the_gap(self):
        ow = _ow()
        self.assertEqual((ow.hold_s(), ow.gap_s()), (ow.HOLD_S, ow.GAP_S))
        with mock.patch.dict(os.environ, {"HELM_OFFICE_WEATHER_HOLD_S": "30",
                                          "HELM_OFFICE_WEATHER_GAP_S": "0"}):
            self.assertEqual((ow.hold_s(), ow.gap_s()), (30, ow.GAP_S))


# ------------------------------------------------------------------ NO PAGE

class _Answer(object):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestsNeverPageTest(World):
    """No test run reaches the owner's phone through the weather. A suite
    inherits the environment that started it, and HELM_NTFY_TOPIC is often
    in it, so a guard that scrubs the topic in some modules leaves every
    other module one settled storm from ringing a real phone. The cure is
    structural: the weather's push goes through ONE seam
    (`officeweather._phone`), and the suite plants the weather's switch off
    in tests/__init__.py, the one file every runner loads, so the seam is
    inert, and the idle tick's pass reads nothing, in every test module and
    every child a test runs. An arm about delivery hands the pass a fake
    phone instead."""

    def wire(self):
        """The network edge notify would use, recorded: every URL a push
        would have opened, ntfy or Telegram."""
        sent = []

        def urlopen(req, timeout=None, **_kw):
            sent.append(getattr(req, "full_url", req))
            return _Answer()
        return sent, mock.patch("urllib.request.urlopen", urlopen)

    def storm(self):
        """Baseline sunny, then a storm that settles: the pass that pushes
        when anything does. No phone is handed in: the default seam runs."""
        ow = _ow()
        calm = [mood("alpha", "flowing")]
        storm = [mood("alpha", "blocked-on-owner", reason="card 7")]
        ow.tick(now=NOW, push=True, reads=reads(calm))
        ow.tick(now=NOW + 10, push=True, reads=reads(storm))
        return ow.tick(now=NOW + 10 + ow.HOLD_S, push=True,
                       reads=reads(storm))

    def test_a_settled_storm_in_a_test_run_never_reaches_notify(self):
        ow = _ow()
        from helm import notify
        self.assertEqual(ow.SWITCH_ENV, SWITCH)
        self.assertEqual(os.environ.get(SWITCH), "off",
                         "tests/__init__.py plants the weather's switch off "
                         "for every test module and every child it runs")
        real = notify.owner_push
        sent, wire = self.wire()
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                wire, mock.patch.object(notify, "owner_push",
                                        wraps=real) as door:
            got = self.storm()
        self.assertEqual(got["settled"]["word"], "stormy")
        self.assertIn("off", got["phone"])
        self.assertEqual((door.call_count, sent), (0, []))
        # MUST-HIT: the same storm with the switch gone reaches the real
        # phone path, so the switch is what kept the arm above quiet
        os.remove(ow.state_path())
        sent, wire = self.wire()
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                wire, mock.patch.object(notify, "owner_push",
                                        wraps=real) as door:
            os.environ.pop(SWITCH)
            got = self.storm()
        self.assertEqual(got["phone"], "pushed")
        self.assertEqual(door.call_count, 1)
        self.assertEqual(sent, ["https://ntfy.sh/helm-fixture"])

    def test_the_idle_tick_in_a_test_run_reads_and_pages_nothing(self):  # noqa: VACUOUS_ASSERTION — the must-hit below drives the same tick with the switch gone and asserts the floor WAS read and the baseline written; nothing read IS the claim here
        ow = _ow()
        from helm import darkmove, idle_dispatch
        read = []

        def spy(name):
            def reader(*_a):
                read.append(name)
                return []
            return reader
        res = {"findings": [], "alerted": [], "woke": [], "undelivered": [],
               "redeliverable": []}

        def bare_tick():
            buf = io.StringIO()
            with mock.patch.dict(ow._READERS,
                                 {n: spy(n) for n in ow._READERS}), \
                    mock.patch.object(idle_dispatch, "check",
                                      return_value=res), \
                    mock.patch.object(darkmove, "run",
                                      return_value=([], [])), \
                    contextlib.redirect_stdout(buf):
                self.assertEqual(idle_dispatch.cmd_idle_dispatch([]), 0)
            return buf.getvalue()
        sent, wire = self.wire()
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                wire:
            out = bare_tick()
        self.assertIn("office weather: off", out)
        self.assertEqual((read, sent), ([], []))
        self.assertFalse(os.path.exists(ow.state_path()))
        # MUST-HIT: with the switch gone the same bare tick runs the pass
        # (it reads the floor and records a baseline, which pushes nothing)
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                wire:
            os.environ.pop(SWITCH)
            out = bare_tick()
        self.assertIn("floor", read)
        self.assertTrue(os.path.exists(ow.state_path()))
        self.assertIn("[phone: baseline", out)
        self.assertEqual(sent, [])


# ------------------------------------------------------------------ SURFACES

class SurfaceTest(World):

    def settle(self, moods, now=NOW):
        return _ow().tick(now=now, push=True, reads=reads(moods),
                          phone=Phone())

    def test_no_pass_yet_says_not_measured(self):
        got = _ow().surface(NOW)
        self.assertIsNone(got["word"])
        self.assertIn("not measured", got["shown"])
        self.assertNotRegex(got["shown"], r"\bhelm [a-z]",
                            "the console never tells the owner to run a verb")

    def test_the_settled_line_is_shown_and_ages_into_stale(self):
        ow = _ow()
        got = self.settle([mood("alpha", "grinding")])
        fresh = ow.surface(NOW + 60)
        self.assertEqual((fresh["word"], fresh["shown"], fresh["stale"]),
                         ("cloudy", got["line"], False))
        old = ow.surface(NOW + ow.STALE_S + 60)
        self.assertTrue(old["stale"])
        self.assertIn("(as of 18:00Z", old["shown"])
        ow.tick(now=NOW + 120, push=True,
                reads=reads([mood("alpha", "blocked-on-owner")]),
                phone=Phone())
        self.assertIn("(turning stormy)", ow.surface(NOW + 130)["shown"])

    def test_the_readiness_poll_carries_the_weather_to_the_console(self):
        from helm import ready, web
        got = self.settle([mood("alpha", "flowing")], now=time.time())
        for cache in (web._qstate, web._qinflight):
            cache.pop("ready", None)
            self.addCleanup(cache.pop, "ready", None)
        with mock.patch.object(ready, "gauge", return_value={"ready": "X"}):
            body = web.API["/api/ready"]()
        self.assertEqual(body["ready"], "X")
        self.assertEqual(body["weather"]["word"], "sunny")
        self.assertEqual(body["weather"]["shown"], got["line"])

    def test_the_nav_strip_is_wired_into_the_page(self):
        from helm import web_ui_loader
        ui = web_ui_loader.read_text()
        nav = ui[ui.index('<nav id="nav">'):ui.index("</nav>")]
        self.assertIn('<div id="navweather" hidden></div>', nav)
        poll = ui[ui.index("async function pollReady("):]
        poll = poll[:poll.index("\n}\n")]
        self.assertIn("weatherShow(d && d.weather)", poll)
        self.assertIn("weatherShow({unavailable: true})", poll)
        for name in ("function weatherStripHTML(", "function weatherShow("):
            self.assertIn(name, ui)


class StripRuntimeTest(unittest.TestCase):
    """weatherStripHTML under node: the exact source the page ships."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        from helm import web_ui_loader
        from tests.test_web_chat_client_runtime import _extract_fn
        src = web_ui_loader.read_text()
        line = [ln for ln in src.splitlines() if ln.startswith("const esc = ")]
        assert len(line) == 1, "assembled web UI's esc definition moved"
        cls.tmp = tempfile.mkdtemp(prefix="helm-weather-strip-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(line[0] + "\n\n" + _extract_fn(src, "weatherStripHTML")
                    + """

const cases = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {};
for (const name of Object.keys(cases)) out[name] = weatherStripHTML(cases[name]);
process.stdout.write(JSON.stringify(out));
""")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_the_strip_draws_the_words_and_colours_by_the_word(self):  # noqa: VACUOUS_ASSERTION — the storm and none cases assert non-empty markup before the loop over a literal 2-tuple
        cases = {"storm": {"word": "stormy",
                           "shown": "\u26c8 stormy: a <b>card</b> & more"},
                 "none": {"word": None,
                          "shown": "office weather: not measured yet"},
                 "dark": {"unavailable": True}, "missing": None}
        path = os.path.join(self.tmp, "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cases, f)
        p = subprocess.run([self.node, self.path, path], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIn('class="wx wx-stormy"', out["storm"])
        self.assertIn("a &lt;b&gt;card&lt;/b&gt; &amp; more", out["storm"])
        self.assertIn('class="wx wx-unread"', out["none"])
        self.assertIn("not measured yet", out["none"])
        for k in ("dark", "missing"):
            self.assertIn("office weather: not read", out[k])


# ------------------------------------------------------------------ HOOK

class HookTest(World):
    """The idle-dispatch tick (helm-idle-dispatch.timer, every 5 min) runs
    the pass: the weather is LIVE wherever that timer runs."""

    def run_tick(self, argv):
        from helm import darkmove, idle_dispatch
        res = {"findings": [], "alerted": [], "woke": [], "undelivered": [],
               "redeliverable": []}
        buf = io.StringIO()
        with mock.patch.object(idle_dispatch, "check", return_value=res), \
                mock.patch.object(darkmove, "run", return_value=([], [])), \
                mock.patch("helm.officeweather.ride",
                           return_value=["office weather: \u2600 sunny: x"]) \
                as ride, contextlib.redirect_stdout(buf):
            rc = idle_dispatch.cmd_idle_dispatch(argv)
        return rc, buf.getvalue(), ride

    def test_the_bare_tick_pushes_and_prints_the_line(self):
        rc, out, ride = self.run_tick([])
        self.assertEqual(rc, 0)
        ride.assert_called_once_with(push=True)
        self.assertIn("office weather: \u2600 sunny: x", out)

    def test_a_dry_or_quiet_tick_reads_without_pushing(self):  # noqa: VACUOUS_ASSERTION — the loop iterates a literal 2-tuple and assert_called_once_with is a positive call assertion
        for argv in (["--dry-run"], ["--quiet"]):
            with self.subTest(argv=argv):
                _rc, _out, ride = self.run_tick(argv)
                ride.assert_called_once_with(push=False)

    def test_the_json_tick_still_runs_the_pass_and_stays_json(self):
        rc, out, ride = self.run_tick(["--json"])
        ride.assert_called_once_with(push=True)
        self.assertIsInstance(json.loads(out), dict)

    def test_ride_never_raises_into_the_tick_it_rides(self):
        ow = _ow()
        # the pass itself, as it runs on the timer: the suite's switch is off
        # here, and TestsNeverPageTest drives that branch
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(SWITCH)
        with mock.patch.object(ow, "tick", side_effect=RuntimeError("boom")):
            self.assertEqual(ow.ride(push=True),
                             ["office weather: FAILED (RuntimeError: boom)"])
        with mock.patch.object(ow, "tick", return_value={
                "line": "\u2601 cloudy: y", "phone": "unchanged"}) as tick:
            self.assertEqual(ow.ride(push=False),
                             ["office weather: \u2601 cloudy: y "
                              "[phone: unchanged]"])
        tick.assert_called_once_with(push=False)


# ------------------------------------------------------------------ CLI

class CliTest(World):

    def run_cli(self, argv, moods=()):
        from helm import cli
        ow = _ow()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(ow._READERS, reads(moods)), \
                mock.patch("helm.notify.owner_push",
                           return_value=True) as push, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue(), push

    def test_the_verb_is_registered_documented_and_helped(self):
        from helm import cli, cli_help
        self.assertIn("office", cli.VERBS)
        self.assertIn("office weather", cli_help._VERB_HELP["office"])
        rc, out, _e, _p = self.run_cli(["office", "weather", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("usage: helm office weather", out)

    def test_json_prints_the_reading_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the parsed word and first reason are asserted stormy first; no state file and no push IS the claim
        ow = _ow()
        rc, out, err, push = self.run_cli(["office", "weather", "--json"],
                                          [mood("alpha", "stuck")])
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["word"], "cloudy")
        self.assertEqual(got["reasons"][0]["word"], "cloudy")
        self.assertFalse(os.path.exists(ow.state_path()))
        push.assert_not_called()

    def test_push_runs_the_timer_pass(self):
        ow = _ow()
        rc, out, err, push = self.run_cli(["office", "weather", "--push"],
                                          [mood("alpha", "flowing")])
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(ow.GLYPH["sunny"] + " sunny: "), out)
        self.assertIn("phone: baseline", out)
        self.assertTrue(os.path.exists(ow.state_path()))
        push.assert_not_called()

    def test_weather_alias_is_the_same_read_only_verb(self):
        rc, out, err, push = self.run_cli(["weather", "--json"],
                                          [mood("alpha", "idle")])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["word"], "sunny")
        push.assert_not_called()
        self.assertFalse(os.path.exists(_ow().state_path()))
        rc, _out, err, _push = self.run_cli(["weather", "--bad"])
        self.assertEqual(rc, 2)
        self.assertIn("--bad", err)

    def test_bare_office_is_the_weather_and_junk_is_refused(self):
        rc, out, _e, _p = self.run_cli(["office"], [mood("alpha", "idle")])
        self.assertEqual(rc, 0)
        self.assertIn("sunny", out)
        rc, _o, err, _p = self.run_cli(["office", "weather", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)
        rc, _o, err, _p = self.run_cli(["office", "rain"])
        self.assertEqual(rc, 2)
        self.assertIn("rain", err)


if __name__ == "__main__":
    unittest.main()
