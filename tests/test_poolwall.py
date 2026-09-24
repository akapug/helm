#!/usr/bin/env python3
"""The per-seat proxy-pool wall (helm/poolwall.py) and its one door.

THE DEFECT: a codex proxy seat whose pool had every credential cooling down
printed the proxy's 429 refusal on every wake, and every wake path re-poked
it — the inbox beacon, resume-turn's nudge, the boot-brief re-arm — each
burning a request and adding one more error line. The family dark latch never
fired because sibling seats on other accounts were healthy.

WHAT THESE PIN. The refusal parses and a malformed reset never walls; the
expiry is the log row's own timestamp plus its duration under a PINNED clock;
the pool's latest statement decides (a completed request after the refusal is
recovery); `delivery_pause` — the door every wake path already obeys — holds
with one reason naming the reset instant, and fires without a wall (the
positive control); the pane classifier dates the same line from an anchor and
never walls without one; and one wall earns one room line, whatever the number
of observers.
"""
import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import poolwall, proxywatch, seat, seat_lifecycle  # noqa: E402

SEAT = "seat-under-test"
FAMILY = "codex"
#: Real producer spellings, copied from seat proxy logs (ids and addresses are
#: per-request hashes and localhost).
LINE = ("API Error: Request rejected (429) · no available credential for "
        "gpt-5.6-sol via provider codex: 6 cooling down (reset in 2h27m37s)")
MESSAGE = ("no available credential for gpt-5.6-sol via provider codex: "
           "6 cooling down (reset in 2h27m37s)")
RESET_S = 2 * 3600 + 27 * 60 + 37
NO_PROVIDER = ("no available credential for claude-opus-5: 5 cooling down "
               "(reset in 93h55m20s), 1 unavailable")
AUTH_FAILED = ("auth_unavailable: no available credential for grok-build-0.1 "
               "via provider xai: 1 auth-failed (providers=xai)")
STAMP = "2026-09-16 03:31:45"
OBSERVED = datetime.datetime(2026, 9, 16, 3, 31, 45)
COMPOSER = ("────────────────────────────\n❯\n────────────────────────────\n"
            "  opus-5 | ~/dev/akapug/helm\n  ⏵⏵ bypass permissions on")


def _epoch(stamp):
    return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))


def _row(stamp, code, message=None, path="/v1/messages?beta=true",
         origin="local"):
    """One proxy.log request row in the fork's shape."""
    head = ('[%s] [abcdef12] [warn ] [gin_logger.go:159] %d |            2ms '
            '|       127.0.0.1 | POST    "%s"' % (stamp, code, path))
    if message is None:
        return head
    body = ('{"type":"error","error":{"type":"rate_limit_error",'
            '"message":"%s"}}' % message).replace('"', '\\"')
    return '%s | refusal_origin_v1=%s | response_body="%s"' % (
        head, origin, body)


def _family_by_name():
    """The fixture seat is not a fleet name, so the by-name family resolver
    every door consults is told its family the way a numbered seat's name
    would tell it."""
    return mock.patch("helm.seat.family_for", return_value=(FAMILY, None))


class _Home(unittest.TestCase):
    """A private HELM_HOME per test: the log, the ledger and the roster all
    live under it, so no arm reads a live seat or writes a live room."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm-home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp.name, "chat")})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.makedirs(os.environ["HELM_HOME"])

    def write_log(self, *rows, seat_name=SEAT, family=FAMILY):
        path = poolwall.log_path(family, seat_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        return path


class ParseTest(unittest.TestCase):
    def test_the_pane_line_parses_to_model_provider_count_and_seconds(self):
        got = poolwall.parse_refusal(LINE)
        self.assertIsNotNone(got)
        self.assertEqual((got["model"], got["provider"], got["count"],
                          got["reset_s"], got["message"]),
                         ("gpt-5.6-sol", "codex", 6, RESET_S, MESSAGE))

    def test_a_refusal_without_a_provider_clause_still_parses(self):
        got = poolwall.parse_refusal(NO_PROVIDER)
        self.assertIsNotNone(got)
        self.assertEqual((got["model"], got["provider"], got["count"],
                          got["reset_s"]),
                         ("claude-opus-5", "", 5, 93 * 3600 + 55 * 60 + 20))

    def test_an_auth_failed_refusal_is_not_a_pool_cooldown(self):
        self.assertEqual(poolwall.parse_refusal(LINE)["count"], 6)  # control
        self.assertIsNone(poolwall.parse_refusal(AUTH_FAILED))
        self.assertIsNone(poolwall.parse_refusal(""))
        self.assertIsNone(poolwall.parse_refusal(None))

    def test_a_malformed_reset_clause_is_not_a_refusal(self):
        """A wall with no readable end would be a wall with no end."""
        self.assertEqual(poolwall.parse_refusal(LINE)["reset_s"], RESET_S)
        self.assertIsNone(poolwall.parse_refusal(
            LINE.replace("2h27m37s", "soon")))
        self.assertIsNone(poolwall.parse_refusal(LINE.replace("2h27m37s", "")))

    def test_reset_clause_spellings(self):
        self.assertEqual(poolwall.parse_reset("2h"), 7200)
        self.assertEqual(poolwall.parse_reset("37s"), 37)
        self.assertEqual(poolwall.parse_reset("27m37s"), 27 * 60 + 37)
        self.assertEqual(poolwall.parse_reset("127h35m21s"),
                         127 * 3600 + 35 * 60 + 21)
        self.assertIsNone(poolwall.parse_reset(""))
        self.assertIsNone(poolwall.parse_reset("0x"))
        self.assertIsNone(poolwall.parse_reset(None))


class LogWallTest(_Home):
    """The expiry is the LOG ROW'S timestamp plus its duration, judged
    against a clock the test supplies; the read-time clock never enters."""

    def test_in_force_before_the_reset_and_history_after_it(self):  # noqa: VACUOUS_ASSERTION — the two clocks are each other's control on one log
        self.write_log(_row(STAMP, 429, MESSAGE))
        at = _epoch(STAMP)
        wall, err = poolwall.seat_wall(SEAT, family=FAMILY, now=at + 60)
        self.assertIsNone(err)
        self.assertEqual(wall["expires_at"], at + RESET_S)
        self.assertEqual((wall["seat"], wall["family"], wall["model"],
                          wall["count"]), (SEAT, FAMILY, "gpt-5.6-sol", 6))
        gone, why = poolwall.seat_wall(SEAT, family=FAMILY,
                                       now=wall["expires_at"] + 1)
        self.assertIsNone(gone)
        self.assertIn("expired", why)

    def test_a_completed_request_after_the_refusal_is_recovery(self):
        self.write_log(_row(STAMP, 429, MESSAGE))
        at = _epoch(STAMP)
        self.assertIsNotNone(poolwall.seat_wall(SEAT, family=FAMILY,
                                                now=at + 60)[0])  # control
        self.write_log(_row(STAMP, 429, MESSAGE),
                       _row("2026-09-16 03:40:00", 200))
        wall, why = poolwall.seat_wall(SEAT, family=FAMILY, now=at + 60)
        self.assertIsNone(wall)
        self.assertIn("HTTP 200", why)

    def test_the_watch_probe_on_another_endpoint_neither_walls_nor_clears(self):
        """The watch writes a 401 to /v1/chat/completions every pass; a row
        off the agent path is not the pool's statement about the pool."""
        self.write_log(_row(STAMP, 429, MESSAGE),
                       _row("2026-09-16 03:40:00", 401,
                            path="/v1/chat/completions?helm_canary=1"))
        wall, _ = poolwall.seat_wall(SEAT, family=FAMILY,
                                     now=_epoch(STAMP) + 60)
        self.assertEqual(wall["expires_at"], _epoch(STAMP) + RESET_S)
        self.write_log(_row("2026-09-16 03:40:00", 401,
                            path="/v1/chat/completions?helm_canary=1"))
        wall, why = poolwall.seat_wall(SEAT, family=FAMILY,
                                       now=_epoch(STAMP) + 60)
        self.assertIsNone(wall)
        self.assertIn("no agent request", why)

    def test_a_canary_refusal_counts_because_the_reset_belongs_to_the_pool(self):
        self.write_log(_row(STAMP, 429, MESSAGE,
                            path="/v1/messages?beta=true&helm_canary=1"))
        wall, _ = poolwall.seat_wall(SEAT, family=FAMILY,
                                     now=_epoch(STAMP) + 60)
        self.assertEqual(wall["count"], 6)

    def test_an_unreadable_log_or_malformed_reset_never_walls(self):
        wall, why = poolwall.seat_wall(SEAT, family=FAMILY, now=1.0)
        self.assertIsNone(wall)
        self.assertIn("unreadable", why)
        self.write_log(_row(STAMP, 429, MESSAGE))
        self.assertEqual(poolwall.seat_wall(
            SEAT, family=FAMILY, now=_epoch(STAMP) + 60)[0]["count"], 6)
        self.write_log(_row(
            STAMP, 429, "no available credential for gpt-5.6-sol via provider "
                        "codex: 6 cooling down (reset in soon)"))
        wall, why = poolwall.seat_wall(SEAT, family=FAMILY,
                                       now=_epoch(STAMP) + 60)
        self.assertIsNone(wall)
        self.assertIn("HTTP 429", why)

    def test_a_seat_with_no_proxy_family_has_no_wall(self):
        self.write_log(_row(STAMP, 429, MESSAGE))
        with _family_by_name():
            self.assertEqual(poolwall.seat_wall(
                SEAT, now=_epoch(STAMP) + 60)[0]["count"], 6)  # control
        with mock.patch("helm.seat.family_for",
                        return_value=(None, "unknown seat")):
            wall, why = poolwall.seat_wall(SEAT, now=_epoch(STAMP) + 60)
        self.assertIsNone(wall)
        self.assertEqual(why, "no proxy family")


class PaneJudgeTest(unittest.TestCase):
    """The pane copy carries a duration and no timestamp: it is dated from an
    observation instant the caller supplies, and UNANCHORED without one."""

    NOW = OBSERVED + datetime.timedelta(hours=1)

    def test_without_an_anchor_the_line_is_UNANCHORED_not_a_wall(self):
        self.assertEqual(seat._wall_in_force(LINE, self.NOW, observed=OBSERVED),
                         seat.WALL_IN_FORCE)                        # control
        self.assertEqual(seat._wall_in_force(LINE, self.NOW),
                         seat.WALL_UNANCHORED)

    def test_anchored_the_duration_is_added_to_the_observation_instant(self):  # noqa: VACUOUS_ASSERTION — the two clocks are each other's control on one anchored line
        self.assertEqual(seat._wall_in_force(LINE, self.NOW, observed=OBSERVED),
                         seat.WALL_IN_FORCE)
        late = OBSERVED + datetime.timedelta(hours=3)
        self.assertEqual(seat._wall_in_force(LINE, late, observed=OBSERVED),
                         seat.WALL_EXPIRED)
        self.assertEqual(seat._pool_expiry(LINE, OBSERVED),
                         OBSERVED + datetime.timedelta(seconds=RESET_S))

    def test_a_callable_anchor_is_asked_per_line_and_None_is_UNANCHORED(self):
        seen = []

        def anchor(line):
            seen.append(line)
            return OBSERVED if "2h27m37s" in line else None
        self.assertEqual(seat._wall_in_force(LINE, self.NOW, observed=anchor),
                         seat.WALL_IN_FORCE)
        other = LINE.replace("2h27m37s", "9h")
        self.assertEqual(seat._wall_in_force(other, self.NOW, observed=anchor),
                         seat.WALL_UNANCHORED)
        self.assertEqual(seen, [LINE, other])

    def test_a_malformed_reset_is_UNANCHORED_even_with_an_anchor(self):
        self.assertEqual(seat._wall_in_force(LINE, self.NOW, observed=OBSERVED),
                         seat.WALL_IN_FORCE)                        # control
        self.assertEqual(seat._wall_in_force(
            LINE.replace("2h27m37s", "soon"), self.NOW, observed=OBSERVED),
            seat.WALL_UNANCHORED)

    def test_the_dated_native_spelling_is_unchanged(self):
        """Control: the vendor's own reset instant still answers as before,
        with or without an anchor, and the undated spelling stays UNDATED."""
        native = "You've hit your weekly limit · resets Sep 17, 12am (UTC)"
        self.assertEqual(seat._wall_in_force(native, self.NOW, observed=OBSERVED),
                         seat.WALL_IN_FORCE)
        self.assertEqual(seat._wall_in_force(native, self.NOW),
                         seat.WALL_IN_FORCE)
        self.assertEqual(seat._wall_in_force("usage balance exhausted", self.NOW),
                         seat.WALL_UNDATED)

    def _pinned(self):
        real = seat._wall_in_force
        return mock.patch.object(
            seat_lifecycle, "_wall_in_force",
            lambda line, now=self.NOW, observed=None: real(line, now, observed))

    def test_the_classifier_walls_the_pane_only_through_an_anchor(self):
        tail = "  ⎿ \xa0" + LINE + "\n" + COMPOSER
        with self._pinned():
            self.assertEqual(seat._classify_pane_tail(tail)[0], "IDLE")
            state, why = seat._classify_pane_tail(
                tail, observed=lambda line: OBSERVED)
        self.assertEqual(state, "BLOCKED_ON_QUOTA")
        self.assertIn("reset at 2026-09-16T05:59:22", why)

    def test_a_repeated_refusal_is_the_same_wall_not_work_below_it(self):
        """Two copies over a bare composer: the second copy is a restatement,
        and the latest copy is the one judged."""
        tail = "\n".join(["  ⎿ \xa0" + LINE, "  ⎿ \xa0" + LINE, COMPOSER])
        with self._pinned():
            state, _ = seat._classify_pane_tail(
                tail, observed=lambda line: OBSERVED)
        self.assertEqual(state, "BLOCKED_ON_QUOTA")

    def test_an_expired_anchored_line_is_history(self):
        tail = "  ⎿ \xa0" + LINE + "\n" + COMPOSER
        late = OBSERVED + datetime.timedelta(hours=3)
        real = seat._wall_in_force
        with mock.patch.object(
                seat_lifecycle, "_wall_in_force",
                lambda line, now=late, observed=None: real(line, now, observed)):
            self.assertEqual(seat._classify_pane_tail(
                tail, observed=lambda line: OBSERVED)[0], "IDLE")


class PaneAnchorTest(_Home):
    def test_the_anchor_is_the_log_row_carrying_the_same_message(self):
        self.write_log(_row("2026-09-16 03:31:30", 429, MESSAGE),
                       _row(STAMP, 429, MESSAGE))
        anchor = poolwall.pane_anchor(FAMILY, SEAT)
        self.assertEqual(anchor("  ⎿ \xa0" + LINE), OBSERVED)
        self.assertIsNone(anchor(LINE.replace("2h27m37s", "9h")))
        self.assertIsNone(anchor("unrelated prose"))

    def test_an_unreadable_log_anchors_nothing(self):
        self.assertIsNone(poolwall.pane_anchor(FAMILY, SEAT)(LINE))
        self.write_log(_row(STAMP, 429, MESSAGE))
        self.assertEqual(poolwall.pane_anchor(FAMILY, SEAT)(LINE), OBSERVED)


class DeliveryPauseDoorTest(_Home):
    """`proxywatch.delivery_pause` is the door every wake path obeys: beacon
    delivery, mention keystrokes, resume-turn's injection and nudge, the stop
    guard, the work offer, and the resume re-arm through `rearm_hold`."""

    def setUp(self):
        super().setUp()
        self.at = _epoch(STAMP)
        self.write_log(_row(STAMP, 429, MESSAGE))
        self.family = _family_by_name()
        self.family.start()
        self.addCleanup(self.family.stop)

    def test_the_seat_is_held_with_one_reason_naming_the_reset_instant(self):
        pause, err = proxywatch.delivery_pause(SEAT, state={}, now=self.at + 60)
        self.assertIsNone(err)
        self.assertEqual(pause["state"], poolwall.STATE)
        self.assertEqual(pause["seat"], SEAT)
        self.assertEqual(pause["until"], poolwall.iso(self.at + RESET_S))
        self.assertIn(poolwall.iso(self.at + RESET_S), pause["reason"])
        self.assertIn("6 cooling down", pause["reason"])

    def test_the_door_opens_after_the_reset_and_for_a_recovered_pool(self):
        """POSITIVE CONTROL first: the same door, the same log, a clock inside
        the wall — then a later clock, then a completed request on top."""
        self.assertEqual(proxywatch.delivery_pause(
            SEAT, state={}, now=self.at + 60)[0]["state"], poolwall.STATE)
        self.assertEqual(proxywatch.delivery_pause(
            SEAT, state={}, now=self.at + RESET_S + 1), (None, None))
        self.write_log(_row(STAMP, 429, MESSAGE),
                       _row("2026-09-16 03:40:00", 200))
        self.assertEqual(proxywatch.delivery_pause(
            SEAT, state={}, now=self.at + 60), (None, None))

    def test_the_family_latch_still_holds_when_the_pool_wall_has_expired(self):
        """The two walls compose: a seat wall in force wins, and without one
        the family record decides exactly as before."""
        dark = {"ts": self.at, "upstream": {FAMILY: {
            "state": "AUTH-401", "dark": True, "since": "x"}}}
        pause, _ = proxywatch.delivery_pause(SEAT, state=dark,
                                             now=self.at + RESET_S + 1)
        self.assertEqual(pause["state"], "AUTH-401")
        pause, _ = proxywatch.delivery_pause(SEAT, state=dark, now=self.at + 60)
        self.assertEqual(pause["state"], poolwall.STATE)

    def test_resume_turn_reads_the_same_verdict(self):
        from helm import resumeturn
        with mock.patch("time.time", return_value=self.at + 60):
            self.assertEqual(resumeturn._delivery_paused(SEAT, None),
                             "state %s" % poolwall.STATE)
        with mock.patch("time.time", return_value=self.at + RESET_S + 1):
            self.assertEqual(resumeturn._delivery_paused(SEAT, None), "")

    def test_the_resume_rearm_is_held_by_the_same_pause(self):
        with mock.patch("time.time", return_value=self.at + 60):
            held = poolwall.rearm_hold(SEAT, None)
        self.assertIn(poolwall.iso(self.at + RESET_S), held)
        self.assertIn(SEAT, held)
        with mock.patch("time.time", return_value=self.at + RESET_S + 1):
            self.assertEqual(poolwall.rearm_hold(SEAT, None), "")

    def test_an_unreadable_pause_holds_the_rearm(self):
        with mock.patch("helm.seats_identity._delivery_pause",
                        side_effect=OSError("boom")):
            held = poolwall.rearm_hold(SEAT, None)
        self.assertIn("could not be read", held)

    def _liveness(self, now, snapshot=({}, None)):
        rec = {"seat": SEAT, "harness": "orca", "handle": "term_x",
               "worktree": "/w", "room": "helm", "ts": "T", "session": "sid"}
        ad = mock.Mock()
        ad.read.return_value = "❯\n  ⏵⏵ bypass permissions on"
        with mock.patch.object(seat, "_spawn_record", return_value=rec), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=(FAMILY, None)), \
                mock.patch.object(seat, "_resolve_registered_pane",
                                  return_value=(ad, "term_x", "")), \
                mock.patch("time.time", return_value=now), \
                mock.patch("helm.proxywatch.upstream_snapshot",
                           return_value=snapshot):
            return seat.seat_liveness(SEAT)

    def test_the_liveness_row_renders_BLOCKED_ON_QUOTA_with_the_reset(self):
        row = self._liveness(self.at + 60)
        self.assertEqual(row["state"], "BLOCKED_ON_QUOTA")
        self.assertEqual(row["evidence"], "pane-tail+proxy-log")
        self.assertIn(poolwall.iso(self.at + RESET_S), row["blocked_on"])
        self.assertEqual((row["handle"], row["session"]), ("term_x", "sid"))
        self.assertEqual(self._liveness(self.at + RESET_S + 1)["state"], "IDLE")


class OneLinePerWallTest(_Home):
    def setUp(self):
        super().setUp()
        self.at = _epoch(STAMP)
        self.write_log(_row(STAMP, 429, MESSAGE))
        self.family = _family_by_name()
        self.family.start()
        self.addCleanup(self.family.stop)

    def test_two_observations_of_one_wall_post_once(self):
        first = poolwall.announcements([SEAT], now=self.at + 60)
        self.assertEqual([s for s, _ in first], [SEAT])
        self.assertIn(SEAT, first[0][1])
        self.assertIn("6 cooling down", first[0][1])
        self.assertIn(poolwall.iso(self.at + RESET_S), first[0][1])
        self.assertEqual(poolwall.announcements([SEAT], now=self.at + 120), [])
        # A LATER ROW OF THE SAME WALL agrees on the instant to the second
        # and is not a new wall.
        self.write_log(_row(STAMP, 429, MESSAGE),
                       _row("2026-09-16 03:31:50", 429,
                            MESSAGE.replace("2h27m37s", "2h27m32s")))
        self.assertEqual(poolwall.announcements([SEAT], now=self.at + 120), [])

    def test_a_fresh_refusal_after_expiry_is_a_new_wall_with_one_new_line(self):
        self.assertEqual(len(poolwall.announcements([SEAT], now=self.at + 60)), 1)
        later = "2026-09-16 06:30:00"
        self.write_log(_row(STAMP, 429, MESSAGE), _row(later, 429, MESSAGE))
        again = poolwall.announcements([SEAT], now=_epoch(later) + 60)
        self.assertEqual(len(again), 1)
        self.assertIn(poolwall.iso(_epoch(later) + RESET_S), again[0][1])
        self.assertEqual(poolwall.announcements([SEAT], now=_epoch(later) + 120),
                         [])

    def test_no_wall_posts_nothing(self):
        posted = []
        self.assertEqual(poolwall.announce(SEAT, now=self.at + 60,
                                           post=posted.append),
                         poolwall.room_line(poolwall.seat_wall(
                             SEAT, now=self.at + 60)[0]))         # control
        self.assertEqual(len(posted), 1)
        self.write_log(_row(STAMP, 429, MESSAGE),
                       _row("2026-09-16 03:40:00", 200))
        self.assertEqual(poolwall.announcements([SEAT], now=self.at + 120), [])
        self.assertIsNone(poolwall.announce(SEAT, now=self.at + 120,
                                            post=posted.append))
        self.assertEqual(len(posted), 1)

    def test_a_failed_post_releases_the_claim_for_the_next_observer(self):
        def broken(body):
            raise OSError("chat down")
        self.assertIsNone(poolwall.announce(SEAT, now=self.at + 60, post=broken))
        posted = []
        body = poolwall.announce(SEAT, now=self.at + 60, post=posted.append)
        self.assertEqual(posted, [body])
        self.assertIn(SEAT, body)
        self.assertIsNone(poolwall.announce(SEAT, now=self.at + 60,
                                            post=posted.append))
        self.assertEqual(len(posted), 1)


if __name__ == "__main__":
    unittest.main()
