#!/usr/bin/env python3
"""The off-peak gate (helm/offpeak.py): a paid key spends only off-peak.

THE OWNER'S ASK: "make it last via offpeak usage". DeepSeek's
peak is 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday, and every other
hour bills at half price. The window is DECLARED on the catalog's pool row
and one pure function reads it; these arms pin that function on every
boundary the owner named, the day rollovers, the conservative holiday rule,
and then each layer that consumes it: the proxy config the reconciler writes,
the delivery hold, the canary skip, the timer's calendar and the surfaces.

EVERY ARM HAS ITS CONTROL: a closed instant beside an open one, a gated
family beside an ungated one, an operator's own flag beside the gate's.
"""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import offpeak, proxywatch, seat, seat_catalog  # noqa: E402
from helm import seat_launch_assets as sla  # noqa: E402

FAMILY = "ds4pro"     # noqa: SEAT_NAME — the catalog family key IS the subject
FLAT = "ds4flash"     # noqa: SEAT_NAME — the ungated control family key
PROVIDER = "deepseek-direct"
WINDOW = seat_catalog.DEEPSEEK_BILLING_WINDOW
REAL_CLOCK_ERROR = offpeak.clock_error    # before any fixture replaces it


def utc(stamp):
    """An ISO minute/second stamp read as UTC -> epoch."""
    return datetime.datetime.fromisoformat(stamp).replace(
        tzinfo=datetime.timezone.utc).timestamp()


MONDAY = "2026-09-28"
PEAK = utc(MONDAY + "T01:30:00")
OFF = utc("2026-09-26T01:30:00")          # a Saturday, same clock


class WindowTest(unittest.TestCase):
    """The one predicate, on every boundary the owner named."""

    def test_the_declared_window_is_the_owner_pasted_one(self):
        self.assertIsNone(offpeak.window_error(WINDOW))
        self.assertEqual(WINDOW["tz"], "UTC")
        self.assertEqual(WINDOW["peak_days"], (0, 1, 2, 3, 4))
        self.assertEqual(WINDOW["peak"], (("01:00", "04:00"),
                                          ("06:00", "10:00")))
        self.assertEqual((WINDOW["guard_lead_s"], WINDOW["guard_lag_s"],
                          WINDOW["clock_max_skew_s"]), (300, 300, 30))

    def test_every_named_boundary_on_a_monday(self):
        want = {"00:59": False, "01:00": True, "03:59": True, "04:00": False,
                "05:59": False, "06:00": True, "09:59": True, "10:00": False}
        got = {hhmm: offpeak.in_peak(WINDOW, utc("%sT%s:00" % (MONDAY, hhmm)))
               for hhmm in want}
        self.assertEqual(got, want)
        # the last second of a window is still inside it
        self.assertTrue(offpeak.in_peak(WINDOW, utc(MONDAY + "T03:59:59")))
        self.assertFalse(offpeak.in_peak(WINDOW, utc(MONDAY + "T00:59:59")))

    def test_the_guard_closes_five_minutes_early_and_opens_five_minutes_late(self):  # noqa: VACUOUS_ASSERTION — equality to a literal map holding six True rows is the positive control
        want = {"00:54:59": False, "00:55:00": True, "00:59:00": True,
                "04:00:00": True, "04:04:59": True, "04:05:00": False,
                "05:54:59": False, "05:55:00": True, "09:59:59": True,
                "10:00:00": True, "10:04:59": True, "10:05:00": False}
        got = {t: offpeak.guarded(WINDOW, utc("%sT%s" % (MONDAY, t)))
               for t in want}
        self.assertEqual(got, want)

    def test_friday_into_saturday(self):
        self.assertTrue(offpeak.in_peak(WINDOW, utc("2026-10-02T09:59:00")))
        for stamp in ("2026-10-03T00:55:00", "2026-10-03T01:00:00",
                      "2026-10-03T06:00:00", "2026-10-03T09:59:00"):
            self.assertFalse(offpeak.guarded(WINDOW, utc(stamp)), stamp)

    def test_sunday_into_monday(self):
        for stamp in ("2026-09-27T00:55:00", "2026-09-27T01:00:00",
                      "2026-09-27T23:59:59"):
            self.assertFalse(offpeak.guarded(WINDOW, utc(stamp)), stamp)
        self.assertTrue(offpeak.guarded(WINDOW, utc(MONDAY + "T00:55:00")))
        self.assertTrue(offpeak.in_peak(WINDOW, utc(MONDAY + "T01:00:00")))

    def test_a_holiday_is_peak_unless_the_table_names_it(self):
        """CONSERVATIVE: no table never spends at the peak price."""
        holiday = "2026-10-01"            # a Thursday
        at = utc(holiday + "T01:30:00")
        self.assertTrue(offpeak.in_peak(WINDOW, at))
        relaxed = dict(WINDOW, offpeak_dates=(holiday,))
        self.assertIsNone(offpeak.window_error(relaxed))
        self.assertFalse(offpeak.in_peak(relaxed, at))
        # the control: the table relaxes only the date it names
        self.assertTrue(offpeak.in_peak(relaxed, utc("2026-10-02T01:30:00")))

    def test_a_declared_peak_date_closes_a_weekend_day(self):
        saturday = "2026-10-10"
        at = utc(saturday + "T01:30:00")
        self.assertFalse(offpeak.in_peak(WINDOW, at))
        self.assertTrue(offpeak.in_peak(dict(WINDOW, peak_dates=(saturday,)),
                                        at))

    def test_the_next_and_last_change(self):  # noqa: VACUOUS_ASSERTION — every arm compares to a concrete non-None instant
        cases = {"2026-09-25T17:00:00": MONDAY + "T00:55:00",
                 MONDAY + "T01:30:00": MONDAY + "T04:05:00",
                 MONDAY + "T04:05:00": MONDAY + "T05:55:00",
                 "2026-10-02T10:05:00": "2026-10-05T00:55:00"}
        for at, want in cases.items():
            self.assertEqual(offpeak.next_change(WINDOW, utc(at)), utc(want),
                             at)
        self.assertEqual(offpeak.last_change(WINDOW, PEAK),
                         utc(MONDAY + "T00:55:00"))

    def test_the_close_lead_is_also_the_clock_error_bound(self):
        self.assertEqual(WINDOW["guard_lag_s"], WINDOW["guard_lead_s"])
        self.assertTrue(offpeak.guarded(
            WINDOW, utc(MONDAY + "T04:04:59")))
        self.assertFalse(offpeak.guarded(
            WINDOW, utc(MONDAY + "T04:05:00")))

    def test_a_malformed_window_is_named(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a six-row literal; the valid control is test_the_declared_window_is_the_owner_pasted_one
        for bad in (dict(WINDOW, tz="Asia/Shanghai"),
                    dict(WINDOW, peak=(("04:00", "01:00"),)),
                    dict(WINDOW, peak=(("1:00", "04:00"),)),
                    dict(WINDOW, peak_days=(7,)),
                    dict(WINDOW, clock_max_skew_s=301),
                    dict(WINDOW, holidays=()),
                    dict(WINDOW, offpeak_dates=("10/01/2026",))):
            self.assertIsNotNone(offpeak.window_error(bad), bad)


class CatalogTest(unittest.TestCase):

    def test_the_direct_key_is_the_pro_familys_only_route(self):
        fam = seat.FAMILIES[FAMILY]
        self.assertEqual(fam["port"], 8360)
        self.assertEqual(fam["mode"], "proxy-key")
        self.assertEqual(fam["pool_default"], "deepseek")
        (row,) = fam["pool_providers"].values()
        self.assertEqual(row["proxy_provider"], PROVIDER)
        self.assertEqual(row["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(row["upstream_model"], "deepseek-v4-pro")
        self.assertEqual(row["rung"], "paid")
        self.assertIs(row["billing_window"], WINDOW)

    def test_every_declared_window_is_readable(self):
        declared = [w for fam in seat.FAMILIES.values()
                    for _p, w in offpeak.gated_providers(fam)]
        self.assertTrue(declared, "the control: at least one window exists")
        for window in declared:
            self.assertIsNone(offpeak.window_error(window))

    def test_the_flat_subscription_family_is_never_gated(self):
        flat = seat.FAMILIES[FLAT]
        self.assertEqual(set(flat["pool_providers"]), {"opencode-go"})
        self.assertEqual(flat["pool_providers"]["opencode-go"]["rung"], "free")
        self.assertEqual(offpeak.gated_providers(flat), [])
        self.assertEqual(offpeak.status_line(FLAT, at=PEAK), "")
        # the control on the same instant: the direct key IS closed
        self.assertIn("CLOSED", offpeak.status_line(FAMILY, at=PEAK))

    def test_flash_start_and_session_launch_stay_staged(self):
        flat = seat.FAMILIES[FLAT]
        gate, notes = sla.family_start_refusal(FLAT, flat, "")
        self.assertEqual(notes, ())
        self.assertIsNotNone(gate)
        self.assertIn("x-opencode-session", gate)
        with mock.patch.object(sla, "_seat_surface_error", return_value=None), \
                mock.patch.object(sla, "_nested_surface_error", return_value=None), \
                mock.patch("helm.hooks.unresolved_externals", return_value=[]):
            refusal, short = sla._launch_surface_refusal(
                FLAT, FLAT, "/synthetic/ds4flash")
        self.assertEqual(short, ())
        self.assertIn("x-opencode-session", refusal)


def _config(port=8360, providers=((PROVIDER, "https://api.deepseek.com/v1",
                                   "deepseek-v4-pro", "ds4-pro"),)):
    rows = [{"provider": p, "base_url": b, "upstream": u, "alias": a,
             "frontmatter": i == 0} for i, (p, b, u, a) in enumerate(providers)]
    return sla._config_yaml_key(port, "t" * 64, None, None, None,
                                "sk-test-not-a-key", providers=rows)


_TWO_BLOCKS = _config(providers=(
    ("opencode-go", "https://opencode.ai/zen/go/v1", "deepseek-v4-pro",
     "ds4-pro"),
    (PROVIDER, "https://api.deepseek.com/v1", "deepseek-v4-pro", "ds4-pro")))


def _item(text, name):
    _prefix, blocks = sla._top_blocks(text)
    _head, items = sla._provider_sections(dict(blocks)["openai-compatibility"])
    return dict(items)[name]


class GateTextTest(unittest.TestCase):

    def test_an_unproved_vendor_clock_closes_an_otherwise_open_route(self):
        with mock.patch.object(offpeak, "now", return_value=OFF), \
                mock.patch.object(offpeak, "clock_error",
                                  return_value="vendor clock unavailable"):
            closed = offpeak.apply_gate(_config(), FAMILY)
            held = offpeak.canary_hold(FAMILY, at=OFF, prove_clock=True)
        self.assertTrue(sla._provider_disabled(_item(closed, PROVIDER)))
        self.assertIn("clock", held)

    def test_a_proved_vendor_clock_allows_an_off_peak_route(self):
        with mock.patch.object(offpeak, "now", return_value=OFF), \
                mock.patch.object(offpeak, "clock_error", return_value=None):
            self.assertEqual(offpeak.apply_gate(_config(), FAMILY), _config())

    def test_closed_writes_the_marked_flag_and_open_restores_the_bytes(self):
        text = _config()
        closed = offpeak.apply_gate(text, FAMILY, at=PEAK)
        self.assertIn("disabled: true  " + offpeak.MARK, closed)
        self.assertTrue(sla._provider_disabled(_item(closed, PROVIDER)))
        self.assertEqual(offpeak.apply_gate(closed, FAMILY, at=PEAK), closed)
        self.assertEqual(offpeak.apply_gate(closed, FAMILY, at=OFF), text)
        self.assertEqual(offpeak.apply_gate(text, FAMILY, at=OFF), text)
        self.assertIn("sk-test-not-a-key", closed)

    def test_an_operators_own_flag_is_never_removed_or_doubled(self):  # noqa: VACUOUS_ASSERTION — the gate's own flag is written by test_closed_writes_the_marked_flag_and_open_restores_the_bytes on the same text
        text = _config().replace(
            '  - name: "%s"\n' % PROVIDER,
            '  - name: "%s"\n    disabled: true\n' % PROVIDER)
        self.assertEqual(offpeak.apply_gate(text, FAMILY, at=OFF), text)
        self.assertEqual(offpeak.apply_gate(text, FAMILY, at=PEAK), text)

    def test_an_operators_false_flag_cannot_defeat_the_hard_gate(self):
        text = _config().replace(
            '  - name: "%s"\n' % PROVIDER,
            '  - name: "%s"\n    disabled: false  # operator default\n'
            % PROVIDER)
        closed = offpeak.apply_gate(text, FAMILY, at=PEAK)
        self.assertTrue(sla._provider_disabled(_item(closed, PROVIDER)))
        self.assertIn(offpeak.MARK, closed)
        self.assertEqual(offpeak.apply_gate(closed, FAMILY, at=OFF), text)

    def test_an_ungated_block_beside_a_gated_one_is_untouched(self):
        closed = offpeak.apply_gate(_TWO_BLOCKS, FAMILY, at=PEAK)
        text = _TWO_BLOCKS
        self.assertEqual(_item(closed, "opencode-go"),
                         _item(text, "opencode-go"))
        self.assertTrue(sla._provider_disabled(_item(closed, PROVIDER)))

    def test_an_ungated_family_is_never_rewritten(self):  # noqa: VACUOUS_ASSERTION — the gated family's rewrite of the same bytes is the control in the arm above
        text = _config(port=8318)
        self.assertEqual(offpeak.apply_gate(text, "kimi", at=PEAK), text)


class _Home(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp.name, "helm-home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp.name, "chat")})
        env.start()
        self.addCleanup(env.stop)
        clock = mock.patch.object(offpeak, "clock_error", return_value=None)
        clock.start()
        self.addCleanup(clock.stop)
        os.makedirs(os.environ["HELM_HOME"])

    def mint(self, text=None):
        d = seat.seat_dir(FAMILY)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or _config())
        return path

    def clock(self, at):
        pinned = mock.patch.object(offpeak, "now", return_value=at)
        pinned.start()
        self.addCleanup(pinned.stop)


class PlanTest(_Home):
    """The reconciler's desired state carries the gate, so `seat up`, the
    */3 `seat doctor --ensure` and a reboot's respawn all agree with the
    clock — and never by stopping the proxy."""

    def test_at_peak_the_plan_closes_the_only_route_without_raising(self):
        path = self.mint()
        self.clock(PEAK)
        plan = sla.proxy_config_plan(path, FAMILY)
        self.assertTrue(plan["changed"])
        self.assertTrue(sla._provider_disabled(_item(plan["text"], PROVIDER)))

    def test_off_peak_the_minted_config_is_already_the_desired_state(self):  # noqa: VACUOUS_ASSERTION — the peak arm above plans a change on the same config
        path = self.mint()
        self.clock(OFF)
        plan = sla.proxy_config_plan(path, FAMILY)
        self.assertFalse(plan["changed"], plan["text"])

    def test_off_peak_the_plan_reopens_a_config_closed_at_peak(self):
        path = self.mint(offpeak.apply_gate(_config(), FAMILY, at=PEAK))
        self.clock(OFF)
        plan = sla.proxy_config_plan(path, FAMILY)
        self.assertTrue(plan["changed"])
        self.assertFalse(sla._provider_disabled(_item(plan["text"], PROVIDER)))
        self.assertEqual(plan["text"], _config())

    def test_a_config_broken_while_open_still_refuses(self):
        """The control on the no-raise arm: the gate excuses only the
        emptiness IT caused."""
        path = self.mint(_config().replace("https://api.deepseek.com/v1",
                                           "http://example.com/v1"))
        self.clock(PEAK)
        with self.assertRaises(ValueError):
            sla.proxy_config_plan(path, FAMILY)


class LaunchGateTest(_Home):

    def test_a_running_paid_proxy_is_reconciled_before_a_pane_can_launch(self):
        self.mint()
        with mock.patch.object(seat, "_ensure_row",
                               return_value=(FAMILY, "unknown",
                                             "close transition failed")) as ensure:
            state, detail = seat._gated_proxy_before_pane(FAMILY, FAMILY)
        self.assertEqual((state, detail), ("unknown", "close transition failed"))
        ensure.assert_called_once_with(FAMILY, FAMILY)


class DeliveryHoldTest(_Home):

    def family(self, name):
        patch = mock.patch("helm.seat.family_for", return_value=(name, None))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_gated_seat_is_held_until_the_window_opens(self):
        self.family(FAMILY)
        pause, err = proxywatch.delivery_pause(FAMILY, state={}, now=PEAK)
        self.assertIsNone(err)
        self.assertEqual(pause["state"], offpeak.STATE)
        self.assertEqual(pause["until"], offpeak.iso(utc(MONDAY + "T04:05:00")))
        self.assertIn("OFF-PEAK-ONLY", pause["reason"])
        self.assertIn("stay owed", pause["reason"])
        self.assertIn("holidays count as peak", pause["reason"])

    def test_off_peak_the_same_seat_is_not_held(self):
        self.family(FAMILY)
        self.assertEqual(proxywatch.delivery_pause(FAMILY, state={}, now=OFF),
                         (None, None))

    def test_an_unproved_clock_holds_delivery_even_off_peak(self):
        self.family(FAMILY)
        with mock.patch.object(offpeak, "clock_error",
                               return_value="vendor clock unavailable"):
            pause, err = proxywatch.delivery_pause(FAMILY, state={}, now=OFF)
        self.assertIsNone(err)
        self.assertEqual(pause["state"], offpeak.STATE)
        self.assertIn("clock", pause["reason"])

    def test_a_seat_whose_config_keeps_an_open_route_is_never_held(self):
        """THE CONFIG DECIDES: the proxy routes what its config carries, so a
        seat holding the flat block beside the gated one still has a route."""
        self.family(FAMILY)
        self.assertEqual(proxywatch.delivery_pause(FAMILY, state={},
                                                   now=PEAK)[0]["state"],
                         offpeak.STATE)
        self.mint(_TWO_BLOCKS)
        self.assertEqual(proxywatch.delivery_pause(FAMILY, state={}, now=PEAK),
                         (None, None))

    def test_the_flat_family_is_never_held_by_the_gate(self):  # noqa: VACUOUS_ASSERTION — the gated seat's hold at the same instant is the control above
        self.family(FLAT)
        self.assertIsNone(offpeak.seat_pause(FLAT, FLAT, at=PEAK))


class CanaryHoldTest(_Home):

    def rows(self):
        return [{"family": FAMILY, "seat": FAMILY, "probe": "healthy"}]

    def test_the_canary_skips_a_closed_family_and_carries_its_record(self):
        prior = {"upstream": {FAMILY: {"state": "HEALTHY", "seats": {
            FAMILY: {"state": "HEALTHY", "since": "2026-09-26T00:00:00Z",
                     "dark": False, "detail": "ok", "ms": 900}}}}}
        with mock.patch.object(proxywatch, "_seat_canary_observation",
                               side_effect=AssertionError("spent")):
            out = proxywatch.upstream_health(self.rows(), now=PEAK,
                                             prior=prior)
        self.assertEqual(out[FAMILY]["seats"][FAMILY]["state"], "HEALTHY")
        self.assertTrue(offpeak.canary_hold(FAMILY, at=PEAK,
                                            seat_name=FAMILY))

    def test_off_peak_the_canary_runs(self):
        seen = []

        def observe(name, family):
            seen.append(name)
            return ("HEALTHY", "ok", 5), None, "NO-REPRESENTATIVE"
        with mock.patch.object(proxywatch, "_seat_canary_observation",
                               side_effect=observe):
            out = proxywatch.upstream_health(self.rows(), now=OFF, prior={})
        self.assertEqual(seen, [FAMILY])
        self.assertEqual(out[FAMILY]["seats"][FAMILY]["state"], "HEALTHY")
        self.assertEqual(offpeak.canary_hold(FAMILY, at=OFF,
                                             seat_name=FAMILY), "")

    def test_the_runtime_proof_canary_is_also_skipped_at_peak(self):
        self.mint()
        with mock.patch.object(proxywatch, "proxy_runtime_canary",
                               side_effect=AssertionError("spent")):
            self.assertEqual(proxywatch.proxy_runtime_proofs(
                self.rows(), observed_at=PEAK), {})

    def test_a_long_off_peak_pass_still_mints_the_runtime_proof(self):
        """THE PRODUCTION CALLER: `proxy_runtime_proofs` receives the pass's
        start instant after every upstream canary has run. With an exact host
        clock that age is not skew, so the paid seat keeps its proof."""
        import email.utils
        self.mint()
        live = OFF + 137
        reply = mock.MagicMock()
        reply.headers = {"Date": email.utils.formatdate(live, usegmt=True)}
        reply.__enter__.return_value = reply
        opener = mock.Mock()
        opener.open.return_value = reply
        offpeak._CLOCK_CACHE.clear()
        self.addCleanup(offpeak._CLOCK_CACHE.clear)
        proof = {"seat": FAMILY}
        with mock.patch.object(offpeak, "clock_error", REAL_CLOCK_ERROR), \
                mock.patch("urllib.request.build_opener", return_value=opener), \
                mock.patch.object(offpeak, "now", return_value=live), \
                mock.patch.object(proxywatch, "proxy_runtime_canary",
                                  return_value=(proof, None)) as canary:
            got = proxywatch.proxy_runtime_proofs(
                self.rows(), observed_at=int(OFF))
        self.assertEqual(got, {FAMILY: proof})
        canary.assert_called_once_with(
            FAMILY, observed_at=int(OFF),
            timeout=proxywatch.UPSTREAM_PASS_DEADLINE_S)


class TimerTest(unittest.TestCase):

    def test_the_calendar_is_derived_in_explicit_utc(self):
        self.assertEqual(offpeak.calendar_lines([WINDOW]), [
            "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 00:55:00 UTC",
            "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 04:05:00 UTC",
            "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 05:55:00 UTC",
            "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 10:05:00 UTC"])

    def test_a_lead_across_midnight_lands_on_the_previous_weekday(self):
        window = dict(WINDOW, peak=(("00:00", "01:00"),), peak_days=(0,))
        self.assertEqual(offpeak.calendar_lines([window]), [
            "OnCalendar=Mon *-*-* 01:05:00 UTC",
            "OnCalendar=Sun *-*-* 23:55:00 UTC"])

    def test_a_declared_peak_date_gets_its_own_edges(self):
        window = dict(WINDOW, peak_dates=("2026-10-10",))
        lines = offpeak.calendar_lines([window])
        self.assertIn("OnCalendar=2026-10-10 00:55:00 UTC", lines)
        self.assertIn("OnCalendar=2026-10-10 10:05:00 UTC", lines)

    def test_the_units_run_the_reconcile_on_boot_and_monotonic_backstop(self):
        _s, service, _t, timer = offpeak.timer_units()
        self.assertIn("offpeak --apply", service)
        self.assertNotIn("SuccessExitStatus=2", service,
                         "UNKNOWN reconciliation must leave systemd red")
        for line in ("OnBootSec=", "OnUnitActiveSec=1min", "Persistent=true",
                     "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 00:55:00 UTC"):
            self.assertIn(line, timer)

    def test_install_writes_the_units_and_enables_the_timer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []

        def run(argv, **_kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"HOME": tmp.name}), \
                mock.patch("helm.offpeak.subprocess.run", side_effect=run):
            ok, detail = offpeak.install_timer()
            unit = os.path.join(tmp.name, ".config", "systemd", "user",
                                "helm-offpeak.timer")
            self.assertTrue(ok, detail)
            self.assertTrue(os.path.isfile(unit))
        self.assertEqual(calls[-1][-1], "helm-offpeak.timer")


class ClockTest(unittest.TestCase):

    def setUp(self):
        offpeak._CLOCK_CACHE.clear()
        self.addCleanup(offpeak._CLOCK_CACHE.clear)

    def reply(self, epoch):
        import email.utils
        reply = mock.MagicMock()
        reply.headers = {"Date": email.utils.format_datetime(
            datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc),
            usegmt=True)}
        reply.__enter__.return_value = reply
        opener = mock.Mock()
        opener.open.return_value = reply
        return opener

    def test_the_vendor_date_proves_only_a_bounded_local_clock(self):
        opener = self.reply(OFF)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with mock.patch.object(offpeak, "now", return_value=OFF + 1):
                self.assertIsNone(offpeak.clock_error(WINDOW))
            with mock.patch.object(offpeak, "now", return_value=(
                    OFF + WINDOW["clock_max_skew_s"] + 1)):
                err = offpeak.clock_error(WINDOW)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("skew", err)

    def test_a_stale_evaluation_instant_is_not_clock_skew(self):
        """A proxywatch pass evaluates the `now` it began with after its
        canaries ran (137 s measured on the fleet host). The proof compares
        the vendor's Date with the LIVE host clock, so the pass's own age
        never closes an off-peak route whose host clock is exact."""
        live = OFF + 137
        opener = self.reply(live)
        with mock.patch("urllib.request.build_opener", return_value=opener), \
                mock.patch.object(offpeak, "now", return_value=live):
            gates = offpeak.gate(FAMILY, at=OFF, prove_clock=True)
        self.assertEqual([(g["closed"], g["clock_error"]) for g in gates],
                         [(False, None)])
        # the control: the same pass on a host clock genuinely 137 s off
        offpeak._CLOCK_CACHE.clear()
        with mock.patch("urllib.request.build_opener",
                        return_value=self.reply(OFF)), \
                mock.patch.object(offpeak, "now", return_value=live):
            gates = offpeak.gate(FAMILY, at=OFF, prove_clock=True)
        self.assertTrue(gates[0]["closed"])
        self.assertIn("skew", gates[0]["clock_error"])

    def test_a_missing_vendor_date_fails_closed(self):
        opener = self.reply(OFF)
        opener.open.return_value.headers = {}
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertIn("Date", offpeak.clock_error(WINDOW))

    def test_a_vendor_clock_redirect_fails_closed(self):
        import urllib.error
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://api.deepseek.com/user/balance", 302, "moved",
            {"Date": "Fri, 25 Sep 2026 19:41:33 GMT"}, None)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertIn("redirected", offpeak.clock_error(WINDOW))

    def test_a_malformed_vendor_http_reply_fails_closed(self):
        import http.client
        opener = mock.Mock()
        opener.open.side_effect = http.client.BadStatusLine("not HTTP")
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertIn("unreachable", offpeak.clock_error(WINDOW))


class BalanceTest(_Home):
    """MAKE IT LAST, MEASURED: the balance behind the gated key, ledgered
    without the key, and the days left at the measured burn."""

    ACCT = "abcd1234"

    def read(self, ts, usd):
        return {"v": 1, "ts": ts, "vendor": "deepseek", "account": self.ACCT,
                "usd": usd, "available": True, "error": None}

    def test_the_burn_is_the_drops_and_a_top_up_burns_nothing(self):
        day = 86400.0
        rows = [self.read(0, 9.99), self.read(day / 2, 9.79),
                self.read(day * 0.75, 19.79), self.read(day, 19.59)]
        self.assertAlmostEqual(offpeak.burn_per_day(rows, at=day), 0.40)

    def test_reads_under_an_hour_apart_measure_no_burn(self):
        rows = [self.read(0, 9.99), self.read(1800, 9.98)]
        self.assertIsNone(offpeak.burn_per_day(rows, at=1800))
        self.assertIn("burn unmeasured",
                      offpeak.balance_lines(at=1800, rows=rows)[0])

    def test_the_line_names_the_balance_and_the_days_left(self):
        rows = [self.read(0, 10.00), self.read(86400, 9.50)]
        (line,) = offpeak.balance_lines(at=86400, rows=rows)
        self.assertIn("$9.50", line)
        self.assertIn("burn $0.50/day", line)
        self.assertIn("~19.0 days left", line)

    def test_the_ledger_carries_the_digest_and_never_the_key(self):
        key = "sk-test-balance-key-never-ledgered"
        self.mint(_config().replace("sk-test-not-a-key", key))
        sent = []

        def fetch(vendor, secret):
            sent.append((vendor, secret))
            return {"usd": 9.99, "available": True}, None
        with mock.patch.object(offpeak, "_minted_gated",
                               return_value=iter([(FAMILY, FAMILY)])):
            (row,) = offpeak.record_balances(at=PEAK, fetch=fetch)
        self.assertEqual(sent, [("deepseek", key)])
        self.assertEqual((row["usd"], row["vendor"]), (9.99, "deepseek"))
        with open(offpeak.balance_ledger_path(), encoding="utf-8") as f:
            ledger = f.read()
        self.assertNotIn(key, ledger)
        self.assertIn(row["account"], ledger)
        from helm import creds
        self.assertIn("balance $9.99", " ".join(creds._offpeak_balances()))

    def test_a_closed_block_still_lends_its_key_to_the_free_read(self):
        self.mint(offpeak.apply_gate(_config(), FAMILY, at=PEAK))
        keys = offpeak._gated_keys(FAMILY, FAMILY)
        self.assertEqual(keys, [("deepseek", "sk-test-not-a-key")])

    def test_the_vendor_reply_is_parsed_and_a_redirect_is_refused(self):
        import http.server
        import json
        import threading
        seen = []
        body = json.dumps({"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "9.99",
             "granted_balance": "0.00", "topped_up_balance": "9.99"}]})

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                if self.path == "/moved":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/x")
                    self.end_headers()
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        loopback = {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
        with mock.patch.dict(os.environ, loopback), \
                mock.patch.dict(offpeak.BALANCE_URLS,
                                {"deepseek": base + "/user/balance"}):
            self.assertEqual(offpeak.fetch_balance("deepseek", "k1"),
                             ({"usd": 9.99, "available": True}, None))
        with mock.patch.dict(os.environ, loopback), \
                mock.patch.dict(offpeak.BALANCE_URLS,
                                {"deepseek": base + "/moved"}):
            self.assertEqual(offpeak.fetch_balance("deepseek", "k1"),
                             (None, "http-302"))
        self.assertEqual(seen, ["Bearer k1", "Bearer k1"])


class SeatSurfaceTest(_Home):
    """`helm seat status` and the proxywatch seat line read the SEAT's
    config: OFF-PEAK-ONLY when the gated block is its only route."""

    def test_the_status_row_and_the_watch_line_name_the_gate(self):
        self.mint()
        line = offpeak.seat_line(FAMILY, FAMILY, at=PEAK)
        self.assertTrue(line.startswith("OFF-PEAK-ONLY: deepseek-direct "
                                        "CLOSED"), line)
        clause = proxywatch._offpeak_clause({"family": FAMILY,
                                             "seat": FAMILY})
        self.assertIn(" gate=OFF-PEAK-ONLY: deepseek-direct ", clause)
        self.assertEqual(proxywatch._offpeak_clause(
            {"family": None, "seat": "seat-under-test"}), "")

    def test_an_open_block_beside_the_gated_one_is_named_a_route(self):
        self.mint(_TWO_BLOCKS)
        line = offpeak.seat_line(FAMILY, FAMILY, at=PEAK)
        self.assertTrue(line.startswith("off-peak route: deepseek-direct "
                                        "CLOSED"), line)


class SurfaceTest(unittest.TestCase):

    def test_the_status_line_names_the_state_and_the_next_change(self):
        self.assertEqual(
            offpeak.status_line(FAMILY, at=PEAK),
            "OFF-PEAK-ONLY: deepseek-direct CLOSED (peak guard) until "
            "2026-09-28T04:05:00Z")
        self.assertEqual(
            offpeak.status_line(FAMILY, at=OFF),
            "OFF-PEAK-ONLY: deepseek-direct OPEN until 2026-09-28T00:55:00Z")
        self.assertEqual(offpeak.status_line("kimi", at=PEAK), "")

    def test_the_verb_is_dispatched_and_documented(self):
        from helm import cli
        self.assertIn("offpeak", cli.VERBS)


if __name__ == "__main__":
    unittest.main()
