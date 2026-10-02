"""helm.burnflags — the fold, the read path and the one line.

EVERY READING IS A FROZEN REDACTED CAPTURE, NEVER A LIVE PATH. The three
fixtures under `fixtures/burnflags/` were captured from this host's own state
in one pass and committed: the pooled codex budget snapshot, the freshest
usage-history row per native account (INCLUDING the three whose status is
`reauth-needed` because helm's own token copy expired), and the watchdog's
upstream family block. Every identity in all three — email, account id, user
id, pool filename, and any seat name that is not `<family>` or `<family>-N` —
was replaced by the first six hex characters of its digest before the file was
written, and both the FAMILY keys and the SEAT keys of the upstream block were
replaced by house-convention names (`family-a`, `seat-a`) because tests/ is
public-bound. Nothing is lost by that: the reach derivation reads the RECORD
and never the name, so the arms re-key the captured records onto the real
family vocabulary through `families()`, in the alphabetical order the capture
was written in. Nothing here reads a snapshot path on the machine it runs on: the
same instrument returned `mixed` and `capped` for the same pool hours apart,
so an arm that read the live file would be a test of the hour.

A FAMILY THAT ARRIVES AFTER THE FREEZE IS APPENDED, NEVER RE-FROZEN. The
upstream block pairs its anonymous records onto the real family vocabulary by
position, so a family admitted to the catalog makes the capture one record
short and every arm that reads the world dies at the pairing. A whole re-capture
would answer that and is the wrong instrument: several lanes widen these same
sets, and a re-freeze makes each one a conflict against bytes nobody can review.
So the append is PER FAMILY and its record is that family's own first measured
reading, which is a different instant from the original pass and carries no
`last_dark_state` or `transition_id` — a first reading has no prior state to
have transitioned from. `family-g` is such an append: the new family's first
upstream reading, HEALTHY, taken when its seat came up. The append is INERT by
construction and that is asserted rather than assumed
(`test_an_appended_family_leaves_every_earlier_colour_alone`): a HEALTHY record
contributes nothing to any axis, so the arms frozen against the earlier families
answer exactly what they answered before it existed.

A fixture is MUTATED by an arm on purpose — that is how a control is built —
and every mutation states what it changed and asserts the unmutated capture
gives the other answer, so a probe that saw no input cannot pass.
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

from helm import burnflags as bf  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "burnflags")


def _pool():
    with open(os.path.join(FIXTURES, "codex-pool-budget.json")) as fh:
        return json.load(fh)


def _history():
    rows = []
    with open(os.path.join(FIXTURES, "anthropic-usage-history.jsonl")) as fh:
        for line in fh:
            rows.append(json.loads(line))
    return rows


#: Catalog families that HAVE NO SEAT, so no live capture of the upstream
#: block can hold a record for one. They are excluded here for exactly the
#: reason the native credential is: a family with no proxy seat produces no
#: upstream reading, and pairing the capture's records onto one would hand a
#: real measurement to a family it was never about. `ds4flash` is declared so
#: a weak rung can be run without wearing a strong family's name; until a seat
#: is minted on it, it reads UNMEASURED on every burn surface, which is true.
#: `qwenlocal` and `bonsai` are here for the same reason: each family was
#: catalogued after this capture was taken, and no seat of it existed then.
#: So is `cursor`: its seat was hand-made and dark, and no catalog entry named
#: it, at the capture.
_UNSEATED_FAMILIES = ("ds4flash", "dots3", "qwenlocal", "bonsai", "cursor")  # noqa: SEAT_NAME — catalog FAMILY keys, which is what this list is about

#: Catalog families that HAVE A SEAT and STILL have no record in this capture,
#: which is a DIFFERENT fact from the tuple above and was measured rather than
#: assumed. Both antigravity-group families were spawned, their proxies
#: answered, and `helm proxywatch` reported `upstream=HEALTHY` for each — and
#: the persisted watch state's upstream block still listed six families, not
#: eight, on two consecutive passes. So a seat can be live, probed and healthy
#: while contributing nothing to the block this capture is a capture OF.
#: Folding them in here would pair another family's real record onto them,
#: which is the exact harm the exclusion above exists to prevent; the reason
#: is simply not "it has no seat".
_ABSENT_FROM_UPSTREAM_CAPTURE = ("opus46", "gptoss")  # noqa: SEAT_NAME — catalog FAMILY keys, which is what this list is about


def _upstream():
    """The captured family records, re-keyed onto the real vocabulary.

    `family-a` .. `family-g` are the capture's own families in alphabetical
    order, which is every family the capture actually holds a record for —
    excluding the native credential, every family in `_UNSEATED_FAMILIES`
    (no seat, so no reading can exist) and every family in
    `_ABSENT_FROM_UPSTREAM_CAPTURE` (a seat, and still no record). The two
    exclusions are kept apart because they are different facts about the
    world, and a single list would let a later reader "fix" one by minting a
    seat that changes nothing.

    THE PAIRING IS POSITIONAL, SO AN APPEND MUST SORT LAST on both sides or it
    re-keys records onto families they are not about. The one appended record
    pairs with the family whose key sorts last among the seated families, and
    the arms below assert the pairing they depend on by NAME rather than
    trusting the order — a family admitted between two others would otherwise
    move every record after it in silence."""
    with open(os.path.join(FIXTURES, "proxywatch-upstream.json")) as fh:
        payload = json.load(fh)
    skip = set(_UNSEATED_FAMILIES) | set(_ABSENT_FROM_UPSTREAM_CAPTURE)
    real = [f for f in bf.families()
            if f != bf.NATIVE_FAMILY and f not in skip]
    captured = sorted(payload["upstream"])
    if len(captured) != len(real):
        raise AssertionError(
            "the capture holds %d families and this tree knows %d: re-capture "
            "the upstream block rather than guessing the mapping"
            % (len(captured), len(real)))
    payload["upstream"] = {name: payload["upstream"][key]
                           for name, key in zip(real, captured)}
    return payload


def _owner_pool():
    """The owner's own reading of the native pool, frozen."""
    with open(os.path.join(FIXTURES, "owner-pool-reading.json")) as fh:
        return json.load(fh)


def _reference_world(now=None):
    """The captured readings, folded as one `inputs` record.

    `now` defaults to a minute after the pooled snapshot's own instant, which
    is the world those readings describe."""
    pool = _pool()
    history = _history()
    now = pool["ts"] + 60 if now is None else now
    rows, latest = bf.anthropic_money_rows(history, now=now)
    return now, {"ceiling": pool["ceiling"],
                 "money": {"codex": pool["rows"], "anthropic": rows},
                 "money_measured_at": {"codex": pool["ts"],
                                       "anthropic": latest},
                 "upstream": _upstream()["upstream"],
                 "anthropic_history": history,
                 "declarations": None}


def _owner_declarations(now):
    """The owner's own reading, as declarations: the colours he stated for the
    instant these captures were taken."""
    stated = {bf.NATIVE_FAMILY: bf.ORANGE, "codex": bf.RED}
    return {"families": {family: {"colour": stated.get(family, bf.YELLOW),
                                  "until": now + 86400,
                                  "declared_at": now - 3600,
                                  "why": "the owner read the fleet and said so"}
                         for family in bf.families()}}


def _row(pct, state="ok", reset_at=1790000000, seconds=604800, label="7d"):
    return {"state": state, "longest_pct": pct,
            "windows": [{"label": label, "used_percent": pct,
                         "reset_at": reset_at, "seconds": seconds}]}


class MoneyAxisTest(unittest.TestCase):
    """F1 must-go-red, F25 partial-never-worsens, F17 grey is not green."""

    def test_f1_every_readable_account_capped_is_red(self):
        pool = _pool()
        rows = pool["rows"]
        under = [r for r in rows if r["longest_pct"] < pool["ceiling"]]
        # THE CONTROL, and it is the capture itself: one pooled account is
        # under the ceiling in the frozen world, so the same fold that must
        # produce RED below must NOT produce it here. A probe reading an empty
        # or invented row list cannot satisfy both halves.
        self.assertEqual(len(under), 1)
        axis = bf.derive_money("codex", rows, ceiling=pool["ceiling"])
        self.assertNotEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["colour"], bf.YELLOW)
        capped = [r for r in rows if r["longest_pct"] >= pool["ceiling"]]
        red = bf.derive_money("codex", capped, ceiling=pool["ceiling"])
        self.assertEqual(red["colour"], bf.RED)
        self.assertEqual(red["cause_id"], "money:capped")
        # and the band between them: the same capture with the one loose
        # account moved to 85% — over the derived warning band, under the
        # ceiling — is ORANGE, not RED and not YELLOW
        warned = capped + [_row(85.0)]
        self.assertEqual(bf.derive_money("codex", warned,
                                         ceiling=pool["ceiling"])["colour"],
                         bf.ORANGE)

    def test_f1_the_warning_band_is_derived_from_the_ceiling(self):
        self.assertEqual(bf.orange_pct(90.0), 80.0)
        self.assertEqual(bf.orange_pct(95.0), 90.0)
        # one knob: moving the ceiling moves the band, and a hard-coded 80
        # would not move at all
        self.assertNotEqual(bf.orange_pct(95.0), bf.orange_pct(90.0))

    def test_f25_unread_accounts_never_worsen_and_cap_at_yellow(self):
        readable = [_row(12.0), _row(15.0), _row(9.0)]
        # CONTROL: fully read, in the green band by headroom — and still not
        # GREEN, because no abundance reading exists
        full = bf.derive_money("anthropic", readable, ceiling=90.0)
        self.assertEqual(full["colour"], bf.YELLOW)
        self.assertFalse(full["capped_by_coverage"])
        partial = readable + [_row(None, state="reauth-needed")] * 3
        axis = bf.derive_money("anthropic", partial, ceiling=90.0)
        self.assertEqual(axis["colour"], bf.YELLOW)
        self.assertNotEqual(axis["colour"], bf.ORANGE)
        self.assertTrue(axis["capped_by_coverage"])
        self.assertEqual(axis["coverage"], {"measured": 3, "unread": 3,
                                            "total": 6,
                                            "unread_why": "3 reauth-needed"})

    def test_f25_an_unread_account_cannot_reach_green(self):
        abundant = [{"verdict": "waste-danger"}]
        read = [_row(12.0), _row(15.0)]
        # CONTROL: the identical call with full coverage IS green, so the
        # refusal below is the coverage conjunct and not a dead code path
        self.assertEqual(bf.derive_money("anthropic", read, ceiling=90.0,
                                         abundance=abundant)["colour"],
                         bf.GREEN)
        capped = bf.derive_money("anthropic", read + [_row(None, "expired-token")],
                                 ceiling=90.0, abundance=abundant)
        self.assertEqual(capped["colour"], bf.YELLOW)
        self.assertTrue(capped["capped_by_coverage"])

    def test_f2_nothing_readable_is_grey_never_green(self):
        unread = [_row(None, "expired-token"), _row(None, "api-error")]
        axis = bf.derive_money("anthropic", unread, ceiling=90.0)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertEqual(axis["cause_id"], "money:unreadable")
        # CONTROL: one readable row in the same call answers a colour
        self.assertEqual(bf.derive_money("anthropic", unread + [_row(20.0)],
                                         ceiling=90.0)["colour"], bf.YELLOW)

    def test_f2_a_family_with_no_reader_is_grey_and_says_so(self):
        axis = bf.derive_money("kimi", None)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertEqual(axis["cause_id"], "money:no-reader")
        # CONTROL: the same family WITH rows answers, so "no reader" is about
        # the input and not about the family's name
        self.assertEqual(bf.derive_money("kimi", [_row(20.0)])["colour"],
                         bf.YELLOW)

    def test_f2_a_stale_reading_is_grey(self):
        axis = bf.derive_money("codex", [_row(20.0)], fresh=False,
                               measured_at=1000)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertEqual(axis["cause_id"], "money:stale")
        flag = bf.compose("codex", {"money": axis}, now=2000)
        self.assertEqual(flag["provenance"], "unmeasured")
        self.assertIsNone(flag["measured_at"])
        self.assertEqual(bf.derive_money("codex", [_row(20.0)],
                                         fresh=True)["colour"], bf.YELLOW)

    def test_a_derived_reading_warns_but_cannot_wall_or_claim_measured(self):
        row = dict(_row(100.0), source="derived")
        row["windows"] = [dict(row["windows"][0], source="derived",
                               unit="requests")]
        projected = bf._money_rows([row])
        self.assertEqual(projected[0]["source"], "derived")
        self.assertEqual(projected[0]["windows"][0]["source"], "derived")
        axis = bf.derive_money("codex", [row], ceiling=90.0,
                               measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.ORANGE, "money:estimated"))
        self.assertEqual(axis["provenance"], "derived")
        flag = bf.compose("codex", {"money": axis}, now=1001)
        self.assertEqual(flag["money_provenance"], "derived")
        with mock.patch.object(bf, "family_flag", return_value=flag):
            self.assertEqual(bf.can_spend("codex", now=1001)["answer"],
                             "unknown")

    def test_shorter_exhausted_window_walls_despite_green_longest_window(self):
        row = {"state": "ok", "longest_pct": 20.0, "source": "measured",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 20.0,
                    "reset_at": 9000, "source": "measured"}]}
        axis = bf.derive_money("gemini", [row], measured_at=1000)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["cause_id"], "money:window-wall")
        self.assertEqual(axis["expires_at"], 2000)
        flag = bf.compose("gemini", {"money": axis}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=flag):
            query = bf.can_spend("gemini", now=1000)
        self.assertEqual(query["answer"], "no")
        self.assertEqual(query["until"], 2000)
        credited = bf.derive_money(
            "gemini", [row], measured_at=1000,
            reset_credits=[{"spendable": 1}])
        self.assertEqual(credited["expires_kind"], "reset-credit")
        self.assertLess(credited["expires_at"], 2000)

    def test_hard_wall_expires_when_the_first_rotated_account_opens(self):
        rows = [
            {"state": "ok", "longest_pct": 100.0, "source": "measured",
             "windows": [
                 {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                  "reset_at": 2000, "source": "measured"},
                 {"label": "7d", "seconds": 604800, "used_percent": 100.0,
                  "reset_at": 9000, "source": "measured"}]},
            {"state": "ok", "longest_pct": 100.0, "source": "measured",
             "windows": [
                 {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                  "reset_at": 3000, "source": "measured"}]}]
        axis = bf.derive_money("gemini", rows, measured_at=1000)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["expires_at"], 3000)
        self.assertEqual(axis["expires_kind"], "session-reset")

    def test_capped_expiry_waits_for_every_over_ceiling_window_to_reset(self):
        row = {"state": "ok", "longest_pct": 95.0, "source": "measured",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 95.0,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 92.0,
                    "reset_at": 9000, "source": "measured"}]}
        axis = bf.derive_money("gemini", [row], ceiling=90,
                               measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.RED, "money:capped"))
        self.assertEqual(axis["expires_at"], 9000)
        self.assertEqual(axis["expires_kind"], "weekly-reset")

    def test_measured_hard_wall_expiry_includes_a_derived_hard_sibling(self):
        row = {"state": "ok", "longest_pct": 100.0, "source": "derived",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 100.0,
                    "reset_at": 9000, "source": "derived"}]}
        axis = bf.derive_money("gemini", [row], ceiling=90,
                               measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.RED, "money:window-wall"))
        self.assertEqual(axis["provenance"], "measured")
        self.assertEqual(axis["expires_at"], 9000)

    def test_capped_expiry_includes_a_derived_over_ceiling_sibling(self):
        row = {"state": "ok", "longest_pct": 95.0, "source": "derived",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 95.0,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 92.0,
                    "reset_at": 9000, "source": "derived"}]}
        axis = bf.derive_money("gemini", [row], ceiling=90,
                               measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.RED, "money:capped"))
        self.assertEqual(axis["expires_at"], 9000)

    def test_unknown_wall_reset_is_explicitly_red_with_unknown_expiry(self):
        row = {"state": "ok", "longest_pct": 20.0, "source": "measured",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                    "reset_at": None, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 20.0,
                    "reset_at": 9000, "source": "measured"}]}
        axis = bf.derive_money("gemini", [row], measured_at=1000)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertIsNone(axis["expires_at"])
        self.assertEqual(axis["expires_source"],
                         "money rows: wall reset unknown")

    def test_window_source_contradiction_is_derived_and_never_authoritative_yes(self):
        row = {"state": "ok", "longest_pct": 20.0, "source": "measured",
               "windows": [{"label": "7d", "seconds": 604800,
                            "used_percent": 20.0, "reset_at": 9000,
                            "source": "derived"}]}
        projected = bf._money_rows([row])
        self.assertEqual(projected[0]["source"], "derived")
        axis = bf.derive_money("gemini", [row], measured_at=1000)
        flag = bf.compose("gemini", {"money": axis}, now=1000)
        self.assertEqual(flag["money_provenance"], "derived")
        with mock.patch.object(bf, "family_flag", return_value=flag):
            self.assertEqual(bf.can_spend("gemini", now=1000)["answer"],
                             "unknown")

    def test_unmodeled_window_caps_spendability_at_unknown(self):
        row = {"state": "ok", "longest_pct": 20.0, "source": "measured",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": None,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 20.0,
                    "reset_at": 9000, "source": "measured"}]}
        axis = bf.derive_money("gemini", [row], measured_at=1000)
        self.assertTrue(axis["capped_by_coverage"])
        flag = bf.compose("gemini", {"money": axis}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=flag):
            self.assertEqual(bf.can_spend("gemini", now=1000)["answer"],
                             "unknown")
        absent = bf.derive_money(
            "gemini", [{"state": "ok", "longest_pct": 20.0,
                        "source": "measured", "windows": []}],
            measured_at=1000)
        self.assertTrue(absent["capped_by_coverage"])
        warning = bf.derive_money(
            "gemini", [{"state": "ok", "longest_pct": 80.0,
                        "source": "measured", "windows": [
                            {"label": "5h", "seconds": 18000,
                             "used_percent": None, "reset_at": 2000,
                             "source": "measured"},
                            {"label": "7d", "seconds": 604800,
                             "used_percent": 80.0, "reset_at": 9000,
                             "source": "measured"}]}],
            ceiling=90, measured_at=1000)
        self.assertEqual(warning["colour"], bf.ORANGE)
        self.assertEqual(warning["cause_id"], "money:estimated")
        self.assertTrue(warning["capped_by_coverage"])
        warning_flag = bf.compose("gemini", {"money": warning}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=warning_flag):
            query = bf.can_spend("gemini", now=1000)
            self.assertEqual(query["answer"], "unknown")
            self.assertIn("5h", query["why"])
        capped = bf.derive_money(
            "gemini", [{"state": "ok", "longest_pct": 95.0,
                        "source": "measured", "windows": [
                            {"label": "5h", "seconds": 18000,
                             "used_percent": None, "reset_at": 2000,
                             "source": "measured"},
                            {"label": "7d", "seconds": 604800,
                             "used_percent": 95.0, "reset_at": 9000,
                             "source": "measured"}]}],
            ceiling=90, measured_at=1000)
        self.assertEqual((capped["colour"], capped["cause_id"]),
                         (bf.RED, "money:capped"),
                         "a measured cap dominates an unread sibling window "
                         "on the same account")
        self.assertTrue(capped["capped_by_coverage"])
        capped_flag = bf.compose("gemini", {"money": capped}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=capped_flag):
            self.assertEqual(bf.can_spend("gemini", now=1000)["answer"], "no")

    def test_closed_account_state_table(self):
        cases = (
            ("wall", _row(100.0), bf.ACCOUNT_WALLED),
            ("cap", _row(95.0), bf.ACCOUNT_CAPPED),
            ("unread", _row(None, "reauth-needed"), bf.ACCOUNT_UNREAD),
            ("open", _row(20.0), bf.ACCOUNT_OPEN),
            ("derived-only", dict(_row(100.0), source="derived",
                                  windows=[dict(_row(100.0)["windows"][0],
                                                source="derived")]),
             bf.ACCOUNT_UNREAD),
            ("cap-with-unread-sibling", {
                "state": "ok", "longest_pct": 95.0, "source": "measured",
                "windows": [dict(_row(95.0)["windows"][0]), {
                    "label": "1m", "seconds": 60, "used_percent": None,
                    "reset_at": 1060, "source": "measured"}]},
             bf.ACCOUNT_CAPPED),
        )
        for name, raw, expected in cases:
            with self.subTest(name=name):
                row = bf._money_rows([raw])[0]
                self.assertEqual(bf._account_money(row, 90)["state"], expected)

        invalid = bf._account_money({
            "state": "ok", "longest_pct": 20, "source": "unknown",
            "windows": [{"label": "7d", "used_percent": 20,
                         "reset_at": 9000, "source": "unknown"}]}, 90)
        self.assertEqual(invalid["state"], bf.ACCOUNT_UNREAD)
        self.assertIn("outside", invalid["why"])

    def test_measured_pressure_plus_unread_account_is_orange_unknown(self):  # noqa: VACUOUS_ASSERTION — both loop arms positively assert ORANGE/cause before their unknown spendability assertions
        for pct in (95.0, 100.0):
            with self.subTest(pct=pct):
                axis = bf.derive_money(
                    "codex", [_row(pct), _row(None, "reauth-needed")],
                    ceiling=90, measured_at=1000)
                self.assertEqual((axis["colour"], axis["cause_id"]),
                                 (bf.ORANGE, "money:measured-capped-unread"))
                self.assertTrue(axis["capped_by_coverage"])
                self.assertIn("1 unread", axis["cause"])
                flag = bf.compose("codex", {"money": axis}, now=1000)
                with mock.patch.object(bf, "family_flag", return_value=flag):
                    self.assertEqual(
                        bf.can_spend("codex", now=1000)["answer"], "unknown")

    def test_an_account_opens_only_when_every_over_ceiling_window_resets(self):
        """task/2935 finding 6, the integrator's correction of its own table:
        an account's until is the instant it becomes OPEN, the LATEST reset
        among every window at or over the CEILING, walled or capped. A 5h at
        100% resetting at 2000 beside a 7d at 95% resetting at 9000 is not
        open at 2000: the 7d still caps it."""
        row = {"state": "ok", "longest_pct": 100.0, "source": "measured",
               "windows": [
                   {"label": "5h", "seconds": 18000, "used_percent": 100.0,
                    "reset_at": 2000, "source": "measured"},
                   {"label": "7d", "seconds": 604800, "used_percent": 95.0,
                    "reset_at": 9000, "source": "measured"}]}
        account = bf._account_money(bf._money_rows([row])[0], 90)
        self.assertEqual(account["state"], bf.ACCOUNT_WALLED)
        self.assertEqual(account["binding"]["label"], "7d")
        axis = bf.derive_money("gemini", [row], ceiling=90, measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.RED, "money:window-wall"))
        self.assertEqual((axis["expires_at"], axis["expires_kind"]),
                         (9000, "weekly-reset"))
        # the family until stays the EARLIEST account-open instant: a second
        # account that opens at 5000 opens the family then
        other = {"state": "ok", "longest_pct": 100.0, "source": "measured",
                 "windows": [{"label": "5h", "seconds": 18000,
                              "used_percent": 100.0, "reset_at": 5000,
                              "source": "measured"}]}
        self.assertEqual(bf.derive_money("gemini", [row, other], ceiling=90,
                                         measured_at=1000)["expires_at"], 5000)
        # CONTROL: under the ceiling the 7d does not hold the account, and
        # the 5h's own reset is the answer
        row["windows"][1]["used_percent"] = 60.0
        self.assertEqual(bf.derive_money("gemini", [row], ceiling=90,
                                         measured_at=1000)["expires_at"], 2000)

    def test_estimated_is_a_named_table_row(self):
        """task/2935 finding 5, the integrator's ruling: the estimated
        warning band was a per-case branch outside the table. It is the
        family table's ESTIMATED row: ORANGE, reason "estimated", spending
        UNKNOWN, dispatch ADMITS, and the estimated account is never
        counted as headroom."""
        self.assertEqual(bf._FAMILY_STATE_TABLE[(False, "estimated", False)],
                         "estimated")
        estimated = {"state": "ok", "longest_pct": 95.0, "source": "derived",
                     "windows": [{"label": "1m", "seconds": 60,
                                  "used_percent": 95.0, "reset_at": 1060,
                                  "source": "derived"}]}
        axis = bf.derive_money("gemini", [estimated], ceiling=90,
                               measured_at=1000)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.ORANGE, "money:estimated"))
        self.assertTrue(axis["cause"].startswith("estimated"))
        self.assertEqual(axis["coverage"]["measured"], 0)
        self.assertTrue(axis["capped_by_coverage"])
        flag = bf.compose("gemini", {"money": axis}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=flag):
            self.assertEqual(bf.can_spend("gemini", now=1000)["answer"],
                             "unknown")
            from helm import dispatches
            ok, refusal, note = dispatches._validate_recipient_budget(
                "gemini", False, family="gemini")
        self.assertEqual((ok, refusal), (True, None))
        self.assertIn("ORANGE", note)
        # beside an OPEN account the estimate is still not headroom: the
        # colour is the open account's, and the coverage says one unread
        opened = bf.derive_money("gemini", [estimated, _row(20.0)],
                                 ceiling=90, measured_at=1000)
        self.assertEqual((opened["colour"], opened["cause_id"]),
                         (bf.YELLOW, "money:has-headroom"))
        self.assertEqual((opened["coverage"]["measured"],
                          opened["coverage"]["unread"]), (1, 1))
        # CONTROL: an estimate under the band is the ordinary UNREAD row
        estimated["windows"][0]["used_percent"] = 40.0
        self.assertEqual(bf.derive_money("gemini", [estimated], ceiling=90,
                                         measured_at=1000)["cause_id"],
                         "money:unreadable")

    def test_family_cell_pressure_unread_open_reads_the_open_accounts(self):
        """task/2935 finding 7, the missing named arm: a walled account, an
        unread account and an open one. The open account is the headroom;
        the walled one is not headroom and the unread one caps GREEN, so
        spending is UNKNOWN and the colour is never RED."""
        self.assertEqual(bf._FAMILY_STATE_TABLE[(True, "unread", True)],
                         "open")
        rows = [_row(100.0), _row(None, "reauth-needed"), _row(20.0)]
        axis = bf.derive_money("gemini", rows, ceiling=90, measured_at=1000,
                               abundance=[{"verdict": "waste-danger"}])
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.YELLOW, "money:has-headroom"))
        self.assertIn("20%", axis["cause"])
        self.assertTrue(axis["capped_by_coverage"])
        self.assertEqual(axis["coverage"], {"measured": 2, "unread": 1,
                                            "total": 3,
                                            "unread_why": "1 reauth-needed"})
        flag = bf.compose("gemini", {"money": axis}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=flag):
            self.assertEqual(bf.can_spend("gemini", now=1000)["answer"],
                             "unknown")
        # CONTROL: the same family with the unread account read open is
        # GREEN on the same abundance, so the cap above is the unread cell
        self.assertEqual(bf.derive_money(
            "gemini", [_row(100.0), _row(30.0), _row(20.0)], ceiling=90,
            measured_at=1000, abundance=[{"verdict": "waste-danger"}])
            ["colour"], bf.GREEN)

    def test_shape_outside_the_account_table_is_unknown_with_reason(self):
        axis = bf.derive_money(
            "codex", [{"state": "ok", "longest_pct": 20,
                       "source": "mystery", "windows": [{
                           "label": "7d", "seconds": 604800,
                           "used_percent": 20, "reset_at": 9000,
                           "source": "mystery"}]}], measured_at=1000)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertIn("outside", axis["cause"])

    def test_f17_grey_is_not_green_and_carries_yellow_capacity(self):  # noqa: VACUOUS_ASSERTION — the absence is GREY's exclusion from the rank table, and the control is the measured family ranked RED through the same overall() call at the end of this method
        self.assertEqual(bf.BEHAVIOUR[bf.GREY]["capacity"],
                         bf.BEHAVIOUR[bf.YELLOW]["capacity"])
        self.assertNotEqual(bf.BEHAVIOUR[bf.GREY]["capacity"],
                            bf.BEHAVIOUR[bf.GREEN]["capacity"])
        self.assertNotIn(bf.GREY, bf._RANK)
        flags = {"kimi": bf.compose("kimi", {"money": bf.derive_money("kimi", None)})}
        head = bf.overall(flags)
        self.assertEqual(head["colour"], bf.GREY)
        self.assertEqual(head["capacity"], bf.BEHAVIOUR[bf.GREY]["capacity"])
        # CONTROL: a measured family in the same structure ranks normally
        flags["codex"] = bf.compose("codex", {"money": bf.derive_money(
            "codex", [_row(95.0), _row(99.0)], ceiling=90.0)})
        self.assertEqual(bf.overall(flags)["colour"], bf.RED)


class ReachAxisTest(unittest.TestCase):
    """F4 our-proxy-dark, F5 our-validation."""

    def _record(self, state, dark=True, age=4000):
        block = _upstream()["upstream"]["codex"]
        return {"state": state, "dark": dark,
                "falsification_bar_s": block["falsification_bar_s"],
                "since": "2026-01-01T00:00:00Z"}, \
            bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + age

    def test_f4_a_cooldown_we_imposed_is_orange_and_an_untyped_wall_is_red(self):
        rec, now = self._record("PROXY-COOLDOWN")
        ours = bf.derive_reach("codex", rec, now=now)
        self.assertEqual(ours["colour"], bf.ORANGE)
        self.assertEqual(ours["cause_id"], "reach:our-cooldown")
        # CONTROL: the same record with an upstream state nothing types is a
        # wall, so ORANGE above is the ORIGIN and not the dark flag
        rec, now = self._record("HTTP-503")
        theirs = bf.derive_reach("codex", rec, now=now)
        self.assertEqual(theirs["colour"], bf.RED)
        self.assertEqual(theirs["cause_id"], "reach:upstream-dark")

    def test_f5_an_empty_200_is_our_validation_and_is_grey(self):
        rec, now = self._record("EMPTY200")
        axis = bf.derive_reach("gemini", rec, now=now)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertTrue(axis["owner_ask"])
        rec, now = self._record("MALFORMED200")
        self.assertEqual(bf.derive_reach("gemini", rec, now=now)["colour"],
                         bf.GREY)
        # CONTROL: a typed wall through the same call is RED
        rec, now = self._record("HTTP-429")
        self.assertEqual(bf.derive_reach("gemini", rec, now=now)["colour"],
                         bf.RED)

    def test_f4_a_dark_state_is_not_believed_before_its_own_bar(self):  # noqa: VACUOUS_ASSERTION — the identical record one second past the bar is asserted to answer RED in this method
        rec, now = self._record("HTTP-503", age=10)
        self.assertIsNone(bf.derive_reach("codex", rec, now=now))
        # CONTROL: the identical record one second past the bar answers RED,
        # so the None above is the bar and not an unreadable record
        rec, now = self._record("HTTP-503", age=rec["falsification_bar_s"] + 1)
        self.assertEqual(bf.derive_reach("codex", rec, now=now)["colour"],
                         bf.RED)

    def test_quota_wall_carries_canonical_reset_on_money_not_reach(self):
        from helm import proxywatch
        rec, now = self._record(proxywatch._QUOTA_WALL)
        rec.update({"resets_at_ms": int((now + 600) * 1000),
                    "reset_kind": "vendor", "reset_source": "canary"})
        self.assertIsNone(bf.derive_reach("grok", rec, now=now))
        axis = bf.derive_quota_wall("grok", rec)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["axis"], "money")
        self.assertEqual(axis["cause_id"], "money:vendor-quota-wall")
        self.assertEqual(axis["expires_at"], now + 600)
        self.assertIn("canary", axis["expires_source"])

    def test_quota_wall_uses_owner_reset_with_provenance(self):
        from helm import proxywatch
        rec, now = self._record(proxywatch._QUOTA_WALL)
        rec.update({"resets_at_ms": int((now + 900) * 1000),
                    "reset_kind": "vendor", "reset_source": "owner"})
        axis = bf.derive_quota_wall("grok", rec)
        self.assertEqual(axis["expires_at"], now + 900)
        self.assertIn("owner", axis["expires_source"])

    def test_based_off_quota_refusal_is_reach_not_money(self):
        from helm import proxywatch, seat
        rec, now = self._record(proxywatch._QUOTA_WALL)
        rec.update({"resets_at_ms": int((now + 600) * 1000),
                    "reset_source": "canary"})
        row = _row(99.0)
        based = bf.fold({"money": {"cursor": [row]},
                         "upstream": {"cursor": rec}, "ceiling": 90},
                        now=now)["families"]["cursor"]
        self.assertEqual(based["axes"]["reach"], bf.RED)
        self.assertEqual(based["axes"]["money"], bf.ORANGE)
        self.assertEqual((based["axis"], based["cause_id"],
                          based["expires_at"]),
                         ("reach", "reach:vendor-quota-wall", now + 600))
        self.assertIsNone(bf.derive_quota_wall("cursor", dict(rec, dark=False)))
        # Unbased and absent claims must retain the old MONEY classification.
        for claim in ({"state": "off", "basis": "   "},
                      {"state": "on", "basis": "test"}, None):
            fam = dict(seat.FAMILIES["cursor"])
            if claim is None:
                fam.pop("on_demand", None)
            else:
                fam["on_demand"] = claim
            with self.subTest(claim=claim), mock.patch.dict(
                    seat.FAMILIES, {"cursor": fam}):
                flag = bf.fold({"money": {"cursor": [row]},
                                "upstream": {"cursor": rec}, "ceiling": 90},
                               now=now)["families"]["cursor"]
                self.assertEqual(flag["axes"]["reach"], None)
                self.assertEqual(flag["axes"]["money"], bf.RED)
                self.assertEqual(flag["cause_id"], "money:capped")
                self.assertEqual(bf.derive_quota_wall("cursor", rec)["cause_id"],
                                 "money:vendor-quota-wall")

    def test_based_off_turn_billing_refusal_is_reach(self):
        wall = {"money": {"seat": "cursor", "label": "vendor refused",
                          "at": 1789000000.0}}
        money, reach = bf.derive_turn_wall("cursor", wall)
        self.assertIsNone(money)
        self.assertEqual((reach["colour"], reach["cause_id"]),
                         (bf.RED, "reach:turn-quota-wall"))
        money, reach = bf.derive_turn_wall("grok", wall)
        self.assertEqual((money["colour"], money["cause_id"]),
                         (bf.RED, "money:turn-wall"))
        self.assertIsNone(reach)

    def test_a_healthy_or_disputed_family_contributes_nothing(self):  # noqa: VACUOUS_ASSERTION — a dark record through the same call is asserted to answer, unconditionally, at the end of this method
        block = _upstream()["upstream"]
        self.assertEqual(block["gemini"]["state"], "HEALTHY")
        self.assertIsNone(bf.derive_reach("gemini", block["gemini"]))
        # the captured codex family reads UNKNOWN — its seats DISAGREE — and a
        # disagreement is neither a wall nor a clearance
        self.assertEqual(block["codex"]["state"], "UNKNOWN")
        self.assertIsNone(bf.derive_reach("codex", block["codex"]))
        # CONTROL: a dark record through the same call answers a colour
        dark = dict(block["gemini"], dark=True, state="HTTP-503",
                    since="2026-01-01T00:00:00Z")
        self.assertIsNotNone(bf.derive_reach("gemini", dark,
                                             now=bf.pk.parse_ts_epoch(
                                                 "2026-01-01T02:00:00Z")))


class PolicyAxisTest(unittest.TestCase):
    """F26 policy needs absolute burn, F20 fable is not a family."""

    def _row(self, weekly, scoped, probed="2026-01-01T00:00:00Z"):
        return {"provider": "anthropic", "account": "aaaaaa",
                "probed_at": probed, "status": "allowed",
                "gauges": [{"label": "7d", "kind": "period",
                            "utilization": weekly, "reset": 1790000000},
                           {"label": "7d-fable", "kind": "period",
                            "utilization": scoped, "reset": 1790000000}]}

    def test_f26_a_ratio_over_nothing_is_silent(self):  # noqa: VACUOUS_ASSERTION — the same ratio over real burn is asserted to fire in this method
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        thin = self._row(0.02, 0.04)             # ratio 2.0, burn 2 percent
        self.assertIsNone(bf.derive_policy([thin], now=now))
        # CONTROL: the SAME ratio over real burn fires, so the silence above
        # is the minimum-burn gate and not an unparsed row
        fat = self._row(0.35, 0.70)
        axis = bf.derive_policy([fat], now=now)
        self.assertEqual(axis["colour"], bf.ORANGE)
        self.assertEqual(axis["cause_id"], "policy:scoped-share")
        self.assertEqual(axis["ratio"], 2.0)

    def test_f26_the_threshold_is_the_owners_lowest_overrun_ratio(self):  # noqa: VACUOUS_ASSERTION — the just-over row is asserted to fire in this method
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        just_under = self._row(0.40, 0.40 * (bf.POLICY_RATIO - 0.01))
        self.assertIsNone(bf.derive_policy([just_under], now=now))
        just_over = self._row(0.40, 0.40 * (bf.POLICY_RATIO + 0.01))
        self.assertIsNotNone(bf.derive_policy([just_over], now=now))

    def test_a_gauge_with_no_reading_never_enters_the_ratio(self):
        """A gauge the vendor sent with no utilization is a gauge with no
        reading. Dividing into one raised inside the fold, and `read_inputs`
        swallows that — the whole native policy reading disappeared with no
        line anywhere saying it had."""
        row = {"probed_at": "2026-09-18T01:39:00Z",
               "gauges": [{"label": bf.POLICY_ACCOUNT_LABEL, "utilization": 0.5,
                           "reset": 1790000000},
                          {"label": bf.POLICY_SCOPED_LABEL,
                           "utilization": None}]}
        now = bf.pk.parse_ts_epoch(row["probed_at"]) + 10
        self.assertIsNone(bf.derive_policy([row], now=now))
        # CONTROL: the same row with a reading on the scoped gauge fires, so
        # the silence above is the missing number and not a dead axis
        fires = json.loads(json.dumps(row))
        fires["gauges"][1]["utilization"] = 0.8
        self.assertEqual(bf.derive_policy([fires], now=now)["colour"],
                         bf.ORANGE)

    def test_a_stale_reading_never_reaches_the_policy_axis(self):  # noqa: VACUOUS_ASSERTION — the fresh reading through the same call answers ORANGE two lines above; that IS the control on this observable
        stamp = "2026-01-01T00:00:00Z"
        now = bf.pk.parse_ts_epoch(stamp) + 60
        fat = self._row(0.35, 0.70, probed=stamp)
        self.assertIsNotNone(bf.derive_policy([fat], now=now))
        self.assertIsNone(bf.derive_policy([fat],
                                           now=now + bf.max_age_s()))

    # ---------------------------------------------------------------- unread

    def _unread(self, probed="2026-01-01T00:00:00Z", status="reauth-needed"):
        """A row the probe wrote with NO reading — the shape `providers` writes
        for reauth-needed / no-credentials: a status and no gauges at all."""
        return {"provider": "anthropic", "account": "bbbbbb",
                "probed_at": probed, "status": status, "gauges": []}

    def _spread(self, *ratios, **kw):
        """Readable rows at the given scoped/account ratios, all over the
        minimum burn so the ratio is the only thing under test."""
        probed = kw.get("probed", "2026-01-01T00:00:00Z")
        return [self._row(0.40, 0.40 * r, probed=probed) for r in ratios]

    def test_an_unread_account_never_fires_the_policy_axis(self):
        """THE MEASURED HOST, reproduced. Four readable accounts at 0.99 /
        1.15 / 1.25 / 1.65 against a 1.19 threshold put the median of the
        READABLE subset at 1.25, and the axis fired ORANGE — capacity 1, one
        delegate at a time, and `route` dropping the native family. The family
        has SIX accounts; its own median is the fourth smallest, which is 1.15
        wherever the two unread ones sit below it. The colour turned on two
        numbers helm could not read."""
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        readable = self._spread(0.99, 1.15, 1.25, 1.65)
        # CONTROL, unconditional and on the SAME observable: the readable rows
        # ALONE fire ORANGE through this same call. So the GREY below is the
        # unread rows and not a dead axis, a thin burn or an unparsed row.
        control = bf.derive_policy(readable, now=now)
        self.assertEqual(control["colour"], bf.ORANGE)
        self.assertEqual(control["cause_id"], "policy:scoped-share")
        axis = bf.derive_policy(readable + [self._unread(), self._unread()],
                                now=now)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertEqual(axis["cause_id"], "policy:unread-census")
        self.assertNotEqual(axis["colour"], bf.ORANGE)

    def test_the_unread_arm_names_the_repair_and_its_own_coverage(self):
        """GREY here is not silence: "we cannot tell" and "there is no
        pressure" are different findings, and only one of them has a repair."""
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        axis = bf.derive_policy(self._spread(0.99, 1.15, 1.25, 1.65)
                                + [self._unread(), self._unread()], now=now)
        self.assertEqual(axis["coverage"],
                         {"measured": 4, "unread": 2, "total": 6})
        self.assertTrue(axis["capped_by_coverage"])
        self.assertIn("2 of 6 accounts carry no reading", axis["cause"])
        self.assertIn("not a wait and not a different model", axis["cause"])

    def test_a_wall_the_unread_rows_cannot_explain_still_fires(self):
        """THE CURE IS NOT A MUTE BUTTON. Where the census median lands on a
        REAL ratio the axis still fires, because then no assignment of the
        unread accounts could have made it quiet."""
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        readable = self._spread(1.50, 1.60, 1.70, 1.80)
        axis = bf.derive_policy(readable + [self._unread(), self._unread()],
                                now=now)
        self.assertEqual(axis["colour"], bf.ORANGE)
        self.assertEqual(axis["cause_id"], "policy:scoped-share")
        # the census median is a READ number, never an unread placeholder
        self.assertEqual(axis["ratio"], 1.6)
        # AND THE DENOMINATOR IS THE FAMILY, not the readable subset: the old
        # cause said "4 of 4 accounts" on a six-account family.
        self.assertIn("on 4 of 6 accounts", axis["cause"])
        self.assertTrue(axis["capped_by_coverage"])

    def test_no_pressure_stays_silent_rather_than_grey(self):  # noqa: VACUOUS_ASSERTION — the same call is asserted to answer GREY and ORANGE on the two shapes above it in this method
        """Only a reading the coverage SILENCED earns the GREY arm. A family
        whose readable accounts say nothing was never a finding."""
        now = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z") + 60
        quiet = self._spread(0.5, 0.6, 0.7, 0.8)
        self.assertIsNone(bf.derive_policy(quiet + [self._unread()], now=now))
        # CONTROLS on the same call: the silenced shape answers GREY and an
        # unsilenceable one answers ORANGE, so the None above is the verdict
        # and not a swallowed row.
        loud = self._spread(0.99, 1.15, 1.25, 1.65)
        self.assertEqual(bf.derive_policy(loud + [self._unread(),
                                                  self._unread()],
                                          now=now)["colour"], bf.GREY)
        self.assertEqual(bf.derive_policy(self._spread(1.5, 1.6, 1.7, 1.8),
                                          now=now)["colour"], bf.ORANGE)

    def test_a_stale_row_is_an_unread_row_on_this_axis_too(self):
        """A reading past the bound is not evidence about now — so it may not
        contribute a ratio AND it may not contribute a position."""
        stamp = "2026-01-01T00:00:00Z"
        now = bf.pk.parse_ts_epoch(stamp) + 60
        fresh = self._spread(0.99, 1.15, 1.25, 1.65, probed=stamp)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(now - bf.max_age_s() - 3600))
        self.assertEqual(bf.derive_policy(fresh, now=now)["colour"], bf.ORANGE)
        axis = bf.derive_policy(fresh + self._spread(0.5, 0.5, probed=old),
                                now=now)
        self.assertEqual(axis["colour"], bf.GREY)
        self.assertEqual(axis["coverage"]["unread"], 2)

    def test_f20_fable_is_a_window_not_a_family(self):
        for model in ("fable", "opus", "sonnet", "haiku"):
            self.assertNotIn(model, bf.families())
        self.assertIn("anthropic", bf.families())
        # the scoped window rides the same account row and is NEVER a money
        # window: folding it in would count one account's budget twice
        rows, _ = bf.anthropic_money_rows(_history(),
                                          now=_pool()["ts"] + 60)
        labels = {w["label"] for r in rows for w in r["windows"]}
        self.assertNotIn(bf.POLICY_SCOPED_LABEL, labels)
        # CONTROL: the captured history DOES carry the scoped window, so the
        # absence above is the filter and not an empty capture
        self.assertIn(bf.POLICY_SCOPED_LABEL,
                      {g["label"] for row in _history()
                       for g in row["gauges"]})


class ComposeTest(unittest.TestCase):
    """F22 declared is worse-only, F9 expiry purity."""

    def _axes(self, measured):
        return {"money": bf._axis("money", measured, "measured", "money:test")}

    def test_f22_a_declaration_may_worsen(self):
        axes = self._axes(bf.YELLOW)
        axes["declared"] = bf._axis("declared", bf.RED, "the owner said so",
                                    "declared:owner")
        flag = bf.compose("codex", axes)
        self.assertEqual(flag["colour"], bf.RED)
        self.assertEqual(flag["provenance"], "owner-declared")
        self.assertFalse(flag["declared_ignored"])

    def test_f22_a_declaration_may_never_improve_a_measured_wall(self):
        axes = self._axes(bf.RED)
        axes["declared"] = bf._axis("declared", bf.GREEN, "the owner said so",
                                    "declared:owner")
        flag = bf.compose("codex", axes)
        self.assertEqual(flag["colour"], bf.RED)
        self.assertEqual(flag["provenance"], "measured")
        self.assertTrue(flag["declared_ignored"])

    def test_f22_on_an_unmeasured_family_the_declaration_is_the_answer(self):
        grey = {"money": bf.derive_money("kimi", None)}
        self.assertEqual(bf.compose("kimi", grey)["colour"], bf.GREY)
        grey["declared"] = bf._axis("declared", bf.YELLOW, "the owner said so",
                                    "declared:owner")
        flag = bf.compose("kimi", grey)
        self.assertEqual(flag["colour"], bf.YELLOW)
        self.assertEqual(flag["provenance"], "owner-declared")

    def test_f22_an_expired_declaration_is_not_read(self):  # noqa: VACUOUS_ASSERTION — the same record one second before its own expiry answers RED in this method
        decl = {"families": {"codex": {"colour": bf.RED, "until": 1000,
                                       "why": "the owner said so"}}}
        self.assertIsNone(bf.derive_declared("codex", decl, now=1001))
        # CONTROL: one second before its own expiry the same record answers
        self.assertEqual(bf.derive_declared("codex", decl, now=999)["colour"],
                         bf.RED)

    def test_f22_a_declared_grey_is_a_coverage_statement_not_a_rank(self):
        """GREY is off the ordering, so `_RANK` cannot be asked about it: a
        declaration the owner makes GREY on a family something MEASURES must
        never be indexed into the rank table, because a KeyError on that path
        takes the whole fold down — every family's flag, on one
        declaration."""
        axes = {"money": bf._axis("money", bf.RED, "the reading said so",
                                  "money:capped", expires_at=900,
                                  expires_kind="weekly-reset"),
                "declared": bf._axis("declared", bf.GREY,
                                     "the readers here are all blind",
                                     "declared:owner", expires_at=1800,
                                     expires_kind="owner-declared")}
        flag = bf.compose("codex", axes, now=0)
        # the measured reading still owns the colour and the countdown
        self.assertEqual(flag["colour"], bf.RED)
        self.assertEqual(flag["axis"], "money")
        self.assertEqual(flag["expires_at"], 900)
        self.assertFalse(flag["declared_ignored"],
                         "a declared GREY is not outranked; it is off the "
                         "scale the ranking is on")
        # and it is SAID, on the table and on the family's own card
        self.assertEqual(flag["declared_unmeasured"],
                         "the readers here are all blind")
        snap = {"families": {"codex": flag}, "overall": bf.overall(
            {"codex": flag}), "ts": 0}
        self.assertIn("the readers here are all blind",
                      "\n".join(bf.render(snap, now=0)))
        self.assertIn("the readers here are all blind",
                      "\n".join(bf.render_why(flag, now=0)))
        # CONTROL: on a family nothing measures the same declaration IS the
        # answer, so the branch above is the ranking and not a dropped record
        alone = bf.compose("codex", {"declared": axes["declared"]}, now=0)
        self.assertEqual(alone["colour"], bf.GREY)
        self.assertEqual(alone["provenance"], "owner-declared")

    def test_f9_expiry_is_anchored_to_the_readings_own_instant(self):  # noqa: VACUOUS_ASSERTION — the control is the RENDERED countdown, asserted to differ over the same two folds
        now, inputs = _reference_world()
        first = bf.fold(inputs, now=now)
        later = bf.fold(inputs, now=now + 60)
        for family in first["families"]:
            for field in ("expires_at", "expires_kind", "expires_source"):
                self.assertEqual(first["families"][family][field],
                                 later["families"][family][field],
                                 "%s %s moved with the reader's clock"
                                 % (family, field))
        # CONTROL: the clock DID move between the two folds, unconditionally
        # and on the same records — the fold's own stamp and the age it
        # derives from the reading both differ, so the identity above is the
        # anchoring rather than two folds of a frozen clock. (The RENDERED
        # countdown is the wrong control: it rounds to hours and would be
        # identical for a minute whatever the fold did.)
        self.assertNotEqual(first["ts"], later["ts"])
        aged = [f for f in first["families"]
                if first["families"][f]["reading_age_s"] is not None]
        self.assertTrue(aged)
        for family in aged:
            self.assertEqual(later["families"][family]["reading_age_s"],
                             first["families"][family]["reading_age_s"] + 60)

    def test_f9_the_headline_axis_owns_the_countdown(self):
        axes = {"money": bf._axis("money", bf.YELLOW, "m", "money:test",
                                  expires_at=100, expires_kind="weekly-reset"),
                "declared": bf._axis("declared", bf.RED, "d", "declared:owner",
                                     expires_at=900,
                                     expires_kind="owner-declared")}
        flag = bf.compose("codex", axes)
        # the declaration is the headline, so its own expiry is when this
        # colour changes — the sooner instant belongs to a colour nobody sees
        self.assertEqual(flag["expires_at"], 900)
        self.assertEqual(flag["expires_kind"], "owner-declared")
        # CONTROL: with no declaration the money instant is the answer
        self.assertEqual(bf.compose("codex", {"money": axes["money"]})
                         ["expires_at"], 100)

    def test_f9_an_unknown_expiry_never_borrows_another_axiss_reset(self):
        """The headline dates the change. An axis at some OTHER colour resets
        without moving this family's band at all, so filling a missing expiry
        from one claims the wall clears at an instant that has nothing to do
        with the wall."""
        axes = {"money": bf._axis("money", bf.YELLOW, "m", "money:test",
                                  expires_at=100, expires_kind="weekly-reset"),
                "reach": bf._axis("reach", bf.RED,
                                  "dark on an untyped upstream state",
                                  "reach:upstream-dark", expires_kind="none")}
        flag = bf.compose("grok", axes, now=0)
        self.assertEqual(flag["colour"], bf.RED)
        self.assertIsNone(flag["expires_at"],
                          "the wall has no known end; the money window's "
                          "reset is a different colour's countdown")
        self.assertNotEqual(flag["expires_at"], 100)
        snap = {"families": {"grok": flag},
                "overall": bf.overall({"grok": flag}), "ts": 0}
        self.assertIn("unknown when", "\n".join(bf.render(snap, now=0)))
        # CONTROL: an axis of the SAME colour DOES lend its instant, which is
        # the precedence this keeps — so the None above is the colour test
        # and not an expiry that can never be filled
        axes["policy"] = bf._axis("policy", bf.RED, "p", "policy:test",
                                  expires_at=555, expires_kind="weekly-reset")
        self.assertEqual(bf.compose("grok", axes, now=0)["expires_at"], 555)

    def test_f10_the_reset_credit_is_expiry_rung_one(self):
        """A family holding a spendable rate-limit reset credit changes in
        MINUTES — the next watchdog pass may spend one — so dating its wall
        from the natural weekly reset is the largest expiry error this module
        can make. The rows are the PASS'S OWN; nothing here probes."""
        self.assertEqual(bf._EXPIRY_ORDER[0], "reset-credit")
        rows = [_row(100.0), _row(100.0)]
        credits = [{"account": "pool-a", "available": 2, "spendable": 1},
                   {"account": "pool-b", "available": 0, "spendable": 0}]
        self.assertEqual(bf.credit_count(credits), 1)
        axis = bf.derive_money("codex", rows, ceiling=90.0,
                               measured_at=1789000000.0,
                               reset_credits=credits)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["expires_kind"], "reset-credit")
        self.assertLess(axis["expires_at"] - 1789000000.0, 3600)
        self.assertIn("spendable", axis["expires_source"])
        # CONTROL: the identical wall with NO credit in hand keeps the old
        # order — the weekly reset, days out
        bare = bf.derive_money("codex", rows, ceiling=90.0,
                               measured_at=1789000000.0)
        self.assertEqual(bare["expires_kind"], "weekly-reset")
        self.assertEqual(bare["expires_at"], 1790000000)
        # and a credit nobody can spend is not a credit
        self.assertEqual(bf.credit_count([{"account": "pool-b",
                                           "available": 3,
                                           "spendable": 0}]), 0)


class PendingDeclarationTest(unittest.TestCase):
    """task/4027: a declaration the snapshot has not folded yet is PENDING.

    The owner declared kimi ORANGE for 7 days, and the sheet still read NOT
    MEASURED after a refresh: the declaration was saved, but the snapshot
    folds it only at the next watchdog pass. `pending_declarations` names
    each live declaration that the snapshot has not folded, so a reader can
    show it as declared and pending. It never changes a colour."""

    NOW = 1789000000.0

    def _declare(self, colour, declared_at, until=None, why="spend down"):
        return {"families": {"kimi": {
            "colour": colour, "declared_at": declared_at, "why": why,
            "until": until or self.NOW + 7 * 86400}}}

    def _fold(self, declarations, now):
        """The snapshot's families, folded by the REAL fold at `now`."""
        return bf.fold({"declarations": declarations}, now=now)["families"]

    def test_a_declaration_after_the_fold_is_pending_until_the_next(self):
        from helm import proxywatch
        decl = self._declare(bf.ORANGE, self.NOW)
        flags = self._fold({"families": {}}, self.NOW - 60)
        got = bf.pending_declarations(decl, flags, self.NOW - 60,
                                      now=self.NOW)
        self.assertEqual(sorted(got), ["kimi"])
        pend = got["kimi"]
        self.assertEqual((pend["colour"], pend["until"], pend["declared_at"]),
                         (bf.ORANGE, self.NOW + 7 * 86400, self.NOW))
        self.assertEqual(pend["why"], "spend down")
        self.assertEqual(pend["folds_by"],
                         self.NOW - 60 + proxywatch.INTERVAL_S)
        # NEVER AS MEASURED: the snapshot's own reading is not touched
        self.assertEqual(flags["kimi"]["colour"], bf.GREY)
        # CONTROL: the next fold reads the same record, and it is no longer
        # pending
        folded = self._fold(decl, self.NOW + 900)
        self.assertEqual(folded["kimi"]["colour"], bf.ORANGE)
        self.assertEqual(folded["kimi"]["declared_at"], self.NOW)
        self.assertEqual(bf.pending_declarations(decl, folded, self.NOW + 900,
                                                 now=self.NOW + 901), {})

    def test_a_declaration_folded_inside_a_pass_is_not_pending(self):
        """The watchdog stamps the snapshot at the START of its pass and reads
        the declarations near the end. A record written in between is folded
        although it is younger than the snapshot's own `ts`: the fold says
        WHICH record it read, so the instant does not decide."""
        decl = self._declare(bf.ORANGE, self.NOW + 30)
        flags = self._fold(decl, self.NOW)
        self.assertEqual(bf.pending_declarations(decl, flags, self.NOW,
                                                 now=self.NOW + 60), {})

    def test_a_redeclaration_in_the_same_colour_is_pending(self):
        """A second declaration in the same colour (a new expiry or reason)
        is a new record. The fold read the first one."""
        first = self._declare(bf.ORANGE, self.NOW - 600)
        flags = self._fold(first, self.NOW - 300)
        again = self._declare(bf.ORANGE, self.NOW, until=self.NOW + 86400)
        got = bf.pending_declarations(again, flags, self.NOW - 300,
                                      now=self.NOW + 1)
        self.assertEqual(got["kimi"]["until"], self.NOW + 86400)

    def test_an_older_snapshot_without_the_record_instant_reads_pending(self):
        """A snapshot written before the fold carried `declared_at` cannot
        say which record it read. It reads PENDING, for one pass at most,
        rather than FOLDED on a guess."""
        decl = self._declare(bf.ORANGE, self.NOW - 600)
        flags = self._fold(decl, self.NOW - 300)
        old = {f: {k: v for k, v in fl.items() if k != "declared_at"}
               for f, fl in flags.items()}
        self.assertIn("kimi", bf.pending_declarations(
            decl, old, self.NOW - 300, now=self.NOW))

    def test_an_expired_or_unreadable_declaration_is_not_pending(self):
        flags = self._fold({"families": {}}, self.NOW - 60)
        expired = self._declare(bf.ORANGE, self.NOW - 7200,
                                until=self.NOW - 1)
        self.assertEqual(bf.pending_declarations(expired, flags,
                                                 self.NOW - 60,
                                                 now=self.NOW), {})
        self.assertEqual(bf.pending_declarations(
            self._declare("PURPLE", self.NOW), flags, self.NOW - 60,
            now=self.NOW), {})
        self.assertEqual(bf.pending_declarations({"families": {}}, flags,
                                                 self.NOW - 60,
                                                 now=self.NOW), {})
        # CONTROL: the same record one second before its expiry is pending
        self.assertIn("kimi", bf.pending_declarations(
            expired, flags, self.NOW - 60, now=self.NOW - 2))

    def test_with_no_fresh_snapshot_every_live_declaration_is_pending(self):
        decl = self._declare(bf.RED, self.NOW)
        got = bf.pending_declarations(decl, None, None, now=self.NOW)
        self.assertEqual(got["kimi"]["colour"], bf.RED)
        self.assertIsNone(got["kimi"]["folds_by"])

    def test_the_reason_is_redacted_as_the_folded_cause_is(self):
        decl = self._declare(bf.ORANGE, self.NOW,
                             why="owner@example.com said stop")
        got = bf.pending_declarations(decl, None, None, now=self.NOW)
        self.assertNotIn("owner@example.com", got["kimi"]["why"])
        self.assertIn(bf.ADDRESS_TOKEN, got["kimi"]["why"])


class ReferenceWorldTest(unittest.TestCase):
    """F3 — the owner's own reading of this fleet, reproduced from the frozen
    captures of the same instant."""

    def test_f3_the_derivation_alone_does_not_reach_orange_on_two_of_six(self):
        """THE MODULE'S OWN FIRST LAW names this very capture as the failure
        it exists to prevent: "Half of one family's accounts read
        `reauth-needed` because HELM'S OWN token copy expired... folding that
        as pressure rations the fleet on our own broken reader."

        Four of the six accounts in the frozen world carry NO reading — three
        `reauth-needed` because helm's token copy expired, one past the
        freshness bound. The derivation reached ORANGE from a median of the
        remaining TWO, and ORANGE is capacity 1, one delegate at a time, and
        `route` dropping the native family at N1. A colour that turns on four
        numbers helm could not read is not a measurement of the family.

        THE OWNER'S ORANGE IS STILL REPRODUCED — through the DECLARED axis,
        where an owner's own reading belongs, which is what the sibling arm
        `..._with_his_declarations` asserts on this same world. What is no
        longer claimed is that the measurement derived it by itself."""
        now, inputs = _reference_world()
        snap = bf.fold(inputs, now=now)
        anthropic = snap["families"]["anthropic"]
        self.assertEqual(anthropic["colour"], bf.YELLOW)
        self.assertEqual(anthropic["axes"]["policy"], bf.GREY)
        self.assertTrue(anthropic["capped_by_coverage"])
        self.assertEqual(anthropic["coverage"]["unread"], 4)
        self.assertEqual(anthropic["coverage"]["total"], 6)
        axis = bf.derive_policy(inputs["anthropic_history"], now=now)
        self.assertEqual(axis["cause_id"], "policy:unread-census")
        # THE CONTROL, unconditional and on the SAME call: drop the rows that
        # carry no reading and the axis fires ORANGE on what is left. So the
        # GREY above is the coverage — not a dead axis, not an unparsed
        # capture, and not a threshold that stopped matching.
        readable = [r for r in inputs["anthropic_history"]
                    if str(r.get("status") or "").startswith(bf.READ_STATUSES)]
        control = bf.derive_policy(readable, now=now)
        self.assertEqual(control["colour"], bf.ORANGE)
        self.assertEqual(control["cause_id"], "policy:scoped-share")
        # AND IT IS NOT REACHABLE FROM HEADROOM, which is why the axis exists:
        # every readable account in the capture is far under the warning band
        money = bf.derive_money("anthropic", inputs["money"]["anthropic"],
                                ceiling=inputs["ceiling"])
        self.assertEqual(money["colour"], bf.YELLOW)
        self.assertLess(min(r["longest_pct"] for r in
                            inputs["money"]["anthropic"]
                            if r["longest_pct"] is not None),
                        bf.orange_pct(inputs["ceiling"]))

    def test_f3_the_owners_whole_reading_is_reproduced_with_his_declarations(self):  # noqa: VACUOUS_ASSERTION — every assertion here is a positive colour; nothing absent is asserted
        now, inputs = _reference_world()
        inputs["declarations"] = _owner_declarations(now)
        snap = bf.fold(inputs, now=now)
        colours = {f: fl["colour"] for f, fl in snap["families"].items()}
        self.assertEqual(colours["anthropic"], bf.ORANGE)
        self.assertEqual(colours["codex"], bf.RED)
        rest = [f for f in bf.families()
                if f not in (bf.NATIVE_FAMILY, "codex")]
        self.assertEqual(len(rest), len(bf.families()) - 2)
        for family in rest:
            self.assertEqual(colours[family], bf.YELLOW, family)
        # AN UNSEATED FAMILY REACHES THAT COLOUR FROM THE DECLARATION, NEVER
        # FROM A READING, and the two must not be confused by a reader
        # counting yellows: the owner's declaration here is fleet-wide, so a
        # family with no capture of its own still carries it -- as DECLARED.
        for family in _UNSEATED_FAMILIES:
            self.assertEqual(snap["families"][family]["provenance"],
                             "owner-declared", family)

    def test_f3_the_codex_red_is_declared_and_says_so(self):
        now, inputs = _reference_world()
        inputs["declarations"] = _owner_declarations(now)
        declared = bf.fold(inputs, now=now)["families"]["codex"]
        self.assertEqual(declared["colour"], bf.RED)
        self.assertEqual(declared["provenance"], "owner-declared")
        # THE CONTROL, and it is the point of the arm: the same captures with
        # no declaration do NOT reach red, so the colour came from what the
        # owner said and is never rendered as a measurement
        inputs["declarations"] = None
        measured = bf.fold(inputs, now=now)["families"]["codex"]
        self.assertNotEqual(measured["colour"], bf.RED)
        self.assertEqual(measured["provenance"], "measured")

    def test_an_appended_family_leaves_every_earlier_colour_alone(self):
        """THE APPEND'S OWN ARM. A family admitted after the capture was taken
        gets its first measured reading appended rather than the whole block
        re-frozen, and the property that makes that safe — a HEALTHY record
        contributes to no axis, so nothing frozen against the earlier families
        moves — is asserted here instead of assumed.

        The pairing is positional, so this reads the appended family BY NAME:
        a family admitted between two others would re-key every record after it
        and this arm is where that would surface."""
        now, inputs = _reference_world()
        block = inputs["upstream"]
        seated = [f for f in bf.families()
                  if f != bf.NATIVE_FAMILY and f not in _UNSEATED_FAMILIES]
        appended = seated[-1]
        self.assertIn(appended, block)
        self.assertEqual(block[appended]["state"], "HEALTHY")
        self.assertFalse(block[appended]["dark"])
        with_it = bf.fold(inputs, now=now)["families"]
        inputs["upstream"] = {f: rec for f, rec in block.items()
                              if f != appended}
        without = bf.fold(inputs, now=now)["families"]
        for family in without:
            if family == appended:
                continue
            self.assertEqual(
                (with_it[family]["colour"], with_it[family].get("axis"),
                 with_it[family]["provenance"]),
                (without[family]["colour"], without[family].get("axis"),
                 without[family]["provenance"]), family)
        # CONTROL: the same append carrying a DARK record DOES change the
        # answer, so the identity above is a healthy reading contributing
        # nothing rather than a record the fold never looked at.
        planted = bf.pk.parse_ts_epoch("2026-01-01T00:00:00Z")
        inputs["upstream"] = dict(block, **{appended: dict(
            block[appended], dark=True, state="HTTP-503",
            since="2026-01-01T00:00:00Z")})
        dark = bf.fold(inputs, now=planted + 7200)["families"][appended]
        self.assertEqual(dark["colour"], bf.RED)

    def test_f3_the_captured_pool_is_mixed_not_capped(self):
        """The capture is a MOMENT: one pooled account was under the ceiling
        when it was taken, and an arm that read the live file would swing with
        the hour."""
        pool = _pool()
        under = [r["longest_pct"] for r in pool["rows"]
                 if r["longest_pct"] < pool["ceiling"]]
        self.assertEqual(len(under), 1)
        self.assertEqual(len([r for r in pool["rows"]
                              if r["longest_pct"] >= pool["ceiling"]]), 5)

    def test_f3_half_the_native_accounts_are_unread_by_our_own_reader(self):
        """The unread half is OUR token copy expiring, not their budget — and
        the capture holds all three, which is why the coverage rule can be
        tested at all."""
        now, inputs = _reference_world()
        states = [r["state"] for r in inputs["money"]["anthropic"]]
        self.assertEqual(states.count("reauth-needed"), 3)
        self.assertEqual(states.count("stale"), 1)
        flag = bf.fold(inputs, now=now)["families"]["anthropic"]
        self.assertTrue(flag["capped_by_coverage"])
        self.assertEqual(flag["coverage"]["unread"], 4)
        self.assertEqual(flag["coverage"]["measured"], 2)


class NativePoolTest(unittest.TestCase):
    """The POOL law the owner calibrated. Every family with a proxy seat has
    ONE DOOR that rotates onto whichever account still has headroom, so the
    loosest account is the answer; the native credential has no proxy seat, a
    seat spends the credential its own home holds, and the question is how
    many of them can still carry base load."""

    def test_f3_the_owners_own_pool_reading_reproduces_orange(self):
        """THE MUST-HIT. His reading: most of the accounts need active
        management and no reset is coming up soon enough to carry base load
        while the others finish."""
        cap = _owner_pool()
        axis = bf.derive_money(bf.NATIVE_FAMILY, cap["rows"],
                               ceiling=cap["ceiling"],
                               measured_at=cap["measured_at"], rotated=False)
        self.assertEqual(axis["colour"], cap["colour"])
        self.assertEqual(axis["cause_id"], "money:pool-thin")
        self.assertIn("4 of 5", axis["cause"])
        # AND IT IS NOT REACHABLE FROM THE BANDS, which is why the pool
        # reading exists: the account with the MOST headroom — the only one
        # the bands look at — is far under the warning band, and none of the
        # five is at the ceiling
        self.assertLess(min(r["longest_pct"] for r in cap["rows"]),
                        bf.orange_pct(cap["ceiling"]))
        self.assertLess(max(r["longest_pct"] for r in cap["rows"]),
                        cap["ceiling"])
        # THE IMMINENT RESET IN THE CAPTURE IS A SESSION WINDOW, and a session
        # window does not move a family that is spent on its week — so the
        # ORANGE above is the binding reset and not an absent one
        soon = [w for r in cap["rows"] for w in r["windows"]
                if 0 < w["reset_at"] - cap["measured_at"] <= bf.carry_soon_s()]
        self.assertEqual([w["label"] for w in soon], ["5h"])
        # THE FLAG, not only the axis: the fold reads the native family as a
        # pool by itself
        snap = bf.fold({"ceiling": cap["ceiling"],
                        "money": {bf.NATIVE_FAMILY: cap["rows"]},
                        "money_measured_at": {
                            bf.NATIVE_FAMILY: cap["measured_at"]}},
                       now=cap["measured_at"] + 60)
        flag = snap["families"][bf.NATIVE_FAMILY]
        self.assertEqual(flag["colour"], bf.ORANGE)
        self.assertEqual(flag["axis"], "money")
        self.assertEqual(flag["provenance"], "measured")

    def test_a_short_reset_does_not_carry_while_another_window_stays_spent(self):
        now = 1000
        spent = {"state": "ok", "longest_pct": 75.0,
                 "source": "measured", "windows": [
                     {"label": "5h", "seconds": 18000,
                      "used_percent": 75.0, "reset_at": now + 3600,
                      "source": "measured"},
                     {"label": "7d", "seconds": 604800,
                      "used_percent": 60.0, "reset_at": now + 604800,
                      "source": "measured"}]}
        rows = [json.loads(json.dumps(spent)),
                json.loads(json.dumps(spent)), _row(10.0)]
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90,
                               measured_at=now, rotated=False)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.ORANGE, "money:pool-thin"))

    def test_an_unread_or_derived_sibling_cannot_prove_a_carrier(self):
        row = {"state": "ok", "longest_pct": 75.0,
               "source": "derived", "windows": [
                   {"label": "7d", "seconds": 604800,
                    "used_percent": 75.0, "reset_at": 2000,
                    "source": "measured"},
                   {"label": "1m", "seconds": 60,
                    "used_percent": 10.0, "reset_at": 1060,
                    "source": "derived"}]}
        complete = json.loads(json.dumps(row))
        complete["source"] = "measured"
        complete["windows"][1]["source"] = "measured"
        self.assertEqual(bf._account_reset_below(
            complete, bf.CARRY_PCT)["reset_at"], 2000)
        self.assertIsNone(bf._account_reset_below(row, bf.CARRY_PCT))
        row["windows"][1].update(source="measured", used_percent=None)
        self.assertIsNone(bf._account_reset_below(row, bf.CARRY_PCT))

    def test_f3_a_reset_that_carries_the_base_load_is_a_wait_not_a_wall(self):
        """MUTATED CAPTURE: the same five accounts, with the weekly window of
        the account nearest the ceiling resetting within two hours. A wall
        that lifts inside the window the fleet is already working in is a
        wait."""
        cap = _owner_pool()
        rows = json.loads(json.dumps(cap["rows"]))
        rows[3]["windows"][0]["reset_at"] = cap["measured_at"] + 7200
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=cap["ceiling"],
                               measured_at=cap["measured_at"], rotated=False)
        self.assertEqual(axis["colour"], bf.YELLOW)
        self.assertEqual(axis["cause_id"], "money:pool-reset-carries")
        self.assertEqual(axis["expires_at"], cap["measured_at"] + 7200)
        # CONTROL: the unmutated capture has no account whose complete blocker
        # set resets inside the carry window.
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, cap["rows"],
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.ORANGE)

    def test_f3_a_pool_that_still_carries_is_green(self):
        """MUTATED CAPTURE: the same five accounts with every Max account
        under 40% of its week. GREEN is reachable on the native family, which
        has no abundance reader at all — a MAJORITY of accounts still under
        half their window IS the pool-level abundance reading."""
        cap = _owner_pool()
        rows = json.loads(json.dumps(cap["rows"]))
        for row, pct in zip(rows, (39.0, 34.0, 20.0, 37.0)):
            row["longest_pct"] = pct
            row["windows"][0]["used_percent"] = pct
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=cap["ceiling"],
                               measured_at=cap["measured_at"], rotated=False)
        self.assertEqual(axis["colour"], bf.GREEN)
        self.assertEqual(axis["cause_id"], "money:pool-carries")
        # CONTROL A: the unmutated capture is ORANGE through the same call
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, cap["rows"],
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.ORANGE)
        # CONTROL B: one unread account and the same carrying pool cannot
        # reach GREEN — an unread account never worsens a colour and never
        # lets it be the best one either
        unread = rows + [_row(None, state="reauth-needed")]
        partial = bf.derive_money(bf.NATIVE_FAMILY, unread,
                                  ceiling=cap["ceiling"],
                                  measured_at=cap["measured_at"],
                                  rotated=False)
        self.assertEqual(partial["colour"], bf.YELLOW)
        self.assertTrue(partial["capped_by_coverage"])

    def test_green_needs_a_strict_majority_and_an_even_split_is_not_one(self):
        """BOTH POLES OF THE BOUNDARY through the real fold. Half the pool
        carrying is not a majority, so the pool says nothing and the ordinary
        bands answer YELLOW; half plus one carrying is the majority and reads
        GREEN. Without the boundary pinned, an even split read the owner's most
        permissive colour, more permissive than trunk's rotated law on the
        same rows."""
        cap = _owner_pool()
        base = json.loads(json.dumps(cap["rows"]))[:4]
        def fold(pcts):
            rows = json.loads(json.dumps(base))
            for row, pct in zip(rows, pcts):
                row["longest_pct"] = pct
                row["windows"][0]["used_percent"] = pct
            return bf.derive_money(bf.NATIVE_FAMILY, rows,
                                   ceiling=cap["ceiling"],
                                   measured_at=cap["measured_at"],
                                   rotated=False)
        half = fold((30.0, 30.0, 70.0, 70.0))
        self.assertEqual(half["colour"], bf.YELLOW)
        self.assertNotEqual(half["cause_id"], "money:pool-carries")
        # CONTROL: one more carrier is a strict majority and IS green
        majority = fold((30.0, 30.0, 30.0, 70.0))
        self.assertEqual(majority["colour"], bf.GREEN)
        self.assertEqual(majority["cause_id"], "money:pool-carries")

    def test_the_pool_reading_belongs_to_the_family_with_no_rotating_door(self):
        """The same rows read as a ROTATED family answer YELLOW: one door
        picks the account, so the loosest account is what the work lands on.
        The pool reading is the native family's law and not a change to every
        family's."""
        cap = _owner_pool()
        rotated = bf.derive_money("codex", cap["rows"], ceiling=cap["ceiling"],
                                  measured_at=cap["measured_at"])
        self.assertEqual(rotated["colour"], bf.YELLOW)
        self.assertEqual(rotated["cause_id"], "money:has-headroom")
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, cap["rows"],
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.ORANGE)


class SnapshotTest(unittest.TestCase):
    """F6 the staleness boundary, F18 no identity in the snapshot."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="burnflags-")
        self.path = os.path.join(self.dir, "burn-flags.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, now):
        _ignored, inputs = _reference_world(now=now)
        self.assertTrue(bf.write_snapshot(inputs=inputs, now=now,
                                          path=self.path))

    def test_f6_the_staleness_bound_is_read_on_both_sides(self):
        now = 1789000000.0
        self._write(now)
        bound = bf.max_age_s()
        flags, age = bf.cached_flags(now=now + bound, path=self.path)
        self.assertTrue(flags)
        self.assertEqual(round(age), bound)
        stale, stale_age = bf.cached_flags(now=now + bound + 1, path=self.path)
        self.assertEqual(stale, {})
        self.assertIsNone(stale_age)

    def test_f6_the_bound_is_the_watchdogs_own_constant_not_a_new_one(self):  # noqa: VACUOUS_ASSERTION — an equality between two live module constants, with no absence in it
        from helm import proxywatch
        self.assertEqual(bf.max_age_s(), proxywatch.UPSTREAM_CACHE_FRESH_S)

    def test_f2_an_absent_or_unknown_fold_reads_as_absent(self):
        self.assertEqual(bf.cached_flags(path=self.path), ({}, None))
        now = 1789000000.0
        self._write(now)
        # CONTROL: the file IS readable at this instant
        self.assertTrue(bf.cached_flags(now=now, path=self.path)[0])
        with open(self.path) as fh:
            payload = json.load(fh)
        payload["fold_version"] = bf.FOLD_VERSION + 1
        with open(self.path, "w") as fh:
            json.dump(payload, fh)
        self.assertEqual(bf.cached_flags(now=now, path=self.path), ({}, None))

    def test_family_flag_is_the_stable_read_api(self):
        now = 1789000000.0
        self._write(now)
        flag = bf.family_flag("anthropic", now=now, path=self.path)
        self.assertEqual(flag["family"], "anthropic")
        self.assertIn("behaviour", flag)
        self.assertIn("colour", flag)
        self.assertIsNone(bf.family_flag("anthropic", now=now + 99999,
                                         path=self.path))

    def test_can_spend_uses_the_cached_flag_and_grey_is_unknown(self):
        measured = bf.compose("codex", {"money": bf.derive_money(
            "codex", [_row(20)], measured_at=1000)}, now=1000)
        derived_row = dict(_row(20), source="derived")
        derived_row["windows"] = [dict(derived_row["windows"][0],
                                        source="derived")]
        derived = bf.compose("codex", {"money": bf.derive_money(
            "codex", [derived_row], measured_at=1000)}, now=1000)
        red = bf.compose("codex", {"money": bf.derive_money(
            "codex", [_row(99)], ceiling=90, measured_at=1000)}, now=1000)
        grey = bf.compose("kimi", {"money": bf.derive_money("kimi", None)},
                          now=1000)
        for flag, answer in ((measured, "yes"), (derived, "unknown"),
                             (red, "no"), (grey, "unknown")):
            with self.subTest(answer=answer, colour=flag["colour"]), \
                    mock.patch.object(bf, "family_flag", return_value=flag) as read:
                self.assertEqual(bf.can_spend(flag["family"], now=1000)["answer"],
                                 answer)
                read.assert_called_once_with(flag["family"], max_age=None,
                                             now=1000, path=None)
        with mock.patch.object(bf, "family_flag", return_value=None):
            self.assertEqual(bf.can_spend("codex")["answer"], "unknown")
        reach = bf._axis("reach", bf.ORANGE,
                         "our proxy refused this itself",
                         "reach:our-refusal", measured_at=1000)
        reached = bf.compose("codex", {
            "money": bf.derive_money("codex", [_row(20)], measured_at=1000),
            "reach": reach}, now=1000)
        with mock.patch.object(bf, "family_flag", return_value=reached):
            answer = bf.can_spend("codex", now=1000)
        self.assertEqual(answer["answer"], "no")
        self.assertEqual(answer["why"], "our proxy refused this itself")

    def test_f18_no_identity_reaches_the_snapshot_or_any_surface(self):  # noqa: VACUOUS_ASSERTION — the control is unconditional and on the same observable: the identities are asserted PRESENT in the input before the surfaces are searched
        """Every pooled row carries an email, an account id and a filename,
        and the budget renderers this module replaces print one. Nothing this
        module writes may."""
        now = 1789000000.0
        pool = _pool()
        rows = [dict(r, email="alice@example.com", account_id="ACCOUNT-ID-1",
                     file="codex-alice@example.com-team.json") for r in pool["rows"]]
        # CONTROL: the identities ARE in the input, so an empty search below
        # would be a probe that saw nothing
        self.assertIn("alice@example.com", json.dumps(rows))
        _ignored, inputs = _reference_world(now=now)
        inputs["money"]["codex"] = rows
        # the declared door is the one input `_money_rows` never projects, so
        # the fold carries a declaration with an address in it as well
        inputs["declarations"] = {"families": {"codex": {
            "colour": bf.RED, "until": now + 3600, "declared_at": now,
            "why": "hold alice@example.com until the reauth lands"}}}
        self.assertIn("alice@example.com", json.dumps(inputs["declarations"]))
        self.assertTrue(bf.write_snapshot(inputs=inputs, now=now,
                                          path=self.path))
        with open(self.path) as fh:
            written = fh.read()
        snap = json.loads(written)
        surfaces = [written, bf.line(snap, now=now),
                    "\n".join(bf.render(snap, now=now)),
                    "\n".join(bf.render_why(snap["families"]["codex"], now=now)),
                    json.dumps(bf.watch_notice(snap["families"], {})[0])]
        for surface in surfaces:
            for secret in ("alice@example.com", "ACCOUNT-ID-1", "@",
                           "codex-alice"):
                self.assertNotIn(secret, surface)

    def test_f18_an_owners_reason_is_redacted_before_it_is_recorded(self):
        """The declaration is the one input `_money_rows` does not project:
        the owner types it, it becomes the flag's cause, and the cause is
        rendered on the board, the one line, the JSON and `helm burn why`."""
        why = ("hold pool@example.test until the reauth lands, token "
               "eyJhbGciOiJIUzI1NiJ9.QUJDREVGR0hJSktMTU5PUFFS.c2lnbmF0dXJl")
        decl = {"families": {"codex": {"colour": bf.RED, "until": 2000,
                                       "declared_at": 0, "why": why}}}
        axis = bf.derive_declared("codex", decl, now=0)
        # CONTROL: the identity and the token ARE in the input, so an empty
        # search below would be a probe that saw nothing
        self.assertIn("pool@example.test", json.dumps(decl))
        self.assertNotIn("pool@example.test", axis["cause"])
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", axis["cause"])
        # the WHOLE address goes: no first letter, no domain, no '@'
        self.assertNotIn("example.test", axis["cause"])
        self.assertNotIn("@", axis["cause"])
        self.assertIn(bf.ADDRESS_TOKEN, axis["cause"])
        flag = bf.compose("codex", {"declared": axis}, now=0)
        snap = {"families": {"codex": flag},
                "overall": bf.overall({"codex": flag}), "ts": 0}
        surfaces = [json.dumps(snap), bf.line(snap, now=0),
                    "\n".join(bf.render(snap, now=0)),
                    "\n".join(bf.render_why(flag, now=0))]
        for surface in surfaces:
            self.assertNotIn("pool@example.test", surface)
            self.assertNotIn("example.test", surface)
            self.assertNotIn("@", surface)
            self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", surface)
        # CONTROL: a reason with nothing in it to redact survives verbatim
        plain = {"families": {"codex": {"colour": bf.RED, "until": 2000,
                                        "declared_at": 0,
                                        "why": "the owner read the fleet"}}}
        self.assertEqual(bf.derive_declared("codex", plain, now=0)["cause"],
                         "the owner read the fleet")

    def test_write_snapshot_never_raises_on_a_bad_target(self):  # noqa: VACUOUS_ASSERTION — a good target through the same call is asserted to write
        self.assertFalse(bf.write_snapshot(inputs={}, now=1.0,
                                           path=os.path.join(self.dir, "x",
                                                             "y", "z", "..",
                                                             "\x00bad")))
        # CONTROL: a good target through the same call writes
        self.assertTrue(bf.write_snapshot(inputs={}, now=1.0, path=self.path))


class CreditsFieldTest(unittest.TestCase):
    """task/3156: each flag carries the reset credits in hand, from the reset
    rung's own rows — counts only. Every credit read is in hand: a reset
    lifts a rate-limit wall and a workspace credits-depleted wall alike
    (measured on trunk), and a Pro credit the spend order holds is still
    held."""

    def rows(self):
        return [{"account": "ready@example.com", "reason": "ready",
                 "available": 1, "spendable": 1},
                {"account": "team-a@example.com", "reason": "ready",
                 "available": 2, "spendable": 2},
                {"account": "pro@example.com", "reason": "pro-credit-held",
                 "available": 1, "spendable": None},
                {"account": "idle@example.com",
                 "reason": "weekly-not-exhausted",
                 "available": None, "spendable": None}]

    def test_the_codex_flag_carries_the_credits_and_no_identity(self):
        now, inputs = _reference_world()
        inputs["reset_credits"] = {"codex": self.rows()}
        # CONTROL: the identities ARE in the input
        self.assertIn("ready@example.com", json.dumps(inputs["reset_credits"]))
        payload = bf.fold(inputs, now=now)
        got = payload["families"]["codex"]["credits"]
        self.assertEqual(got, {"usable": 4, "unread": 1,
                               "why": "1 account not asked for a balance "
                                      "this pass (no spendable wall)"})
        blob = json.dumps(payload)
        for secret in ("ready@example.com", "team-a@example.com",
                       "idle@example.com", "example.com"):
            self.assertNotIn(secret, blob)
        # a family the rung never ran for carries no credits field value
        self.assertIsNone(payload["families"][bf.NATIVE_FAMILY]["credits"])
        self.assertIn("credits", payload["families"][bf.NATIVE_FAMILY])

    def test_no_wall_makes_a_counted_credit_unusable(self):
        """Trunk measured that a reset lifts a workspace credits-depleted
        wall, and dropped that refusal from the rung; a summary that still
        split such credits off would name a reason the rung no longer
        writes."""
        got = bf.credit_summary(self.rows()[:3])
        self.assertEqual(got, {"usable": 4, "unread": 0, "why": None})
        self.assertIsNone(bf.credit_summary(None))
        self.assertEqual(bf.credit_summary([])["why"], None)

    def test_the_colour_is_untouched_by_credits(self):
        now, inputs = _reference_world()
        plain = bf.fold(inputs, now=now)["families"]["codex"]["colour"]
        inputs["reset_credits"] = {"codex": self.rows()}
        self.assertEqual(bf.fold(inputs, now=now)["families"]["codex"]
                         ["colour"], plain)


class NativeAdapterTest(unittest.TestCase):
    """The reader between the creds probe cycle's history log and the money
    axis: which rows carry a reading and which carry none."""

    def _hist(self, *specs, **kw):
        """History rows in the native probe's own shape: (status, weekly)."""
        at = kw.get("at", "2026-09-18T01:39:00Z")
        rows = []
        for i, (status, weekly) in enumerate(specs):
            gauges = ([] if weekly is None else
                      [{"label": "7d", "utilization": weekly,
                        "reset": 1790168400},
                       {"label": "5h", "utilization": 0.1,
                        "reset": 1789710060}])
            rows.append({"provider": "anthropic", "account": "acct-%d" % i,
                         "probed_at": at, "status": status, "gauges": gauges})
        return rows

    def test_f1_an_exhausted_native_account_is_read_not_unread(self):
        """The probe answers `blocked` WITH the vendor's gauges when an
        account is at 100%. Reading that as unreadable threw the measured
        walls into the coverage gap, and the family's colour IMPROVED from RED
        to GREY because the accounts that were certainly spent were exactly
        the ones dropped."""
        hist = self._hist(("blocked", 1.0), ("blocked", 1.0))
        now = bf.pk.parse_ts_epoch(hist[0]["probed_at"]) + 10
        rows, latest = bf.anthropic_money_rows(hist, now=now)
        self.assertEqual([r["state"] for r in rows], ["ok", "ok"])
        self.assertEqual([r["longest_pct"] for r in rows], [100.0, 100.0])
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90.0,
                               measured_at=latest, rotated=False)
        self.assertEqual(axis["colour"], bf.RED)
        self.assertEqual(axis["cause_id"], "money:window-wall")
        self.assertEqual(axis["coverage"], {"measured": 2, "unread": 0,
                                            "total": 2, "unread_why": ""})
        # CONTROL: an account our own reader could not read makes the family
        # partial, not RED. The measured account remains walled; the unread one
        # is neither headroom nor evidence that every account is walled.
        mixed = self._hist(("blocked", 1.0),
                           ("reauth-needed (helm's token expired)", None))
        rows, latest = bf.anthropic_money_rows(mixed, now=now)
        self.assertEqual([r["state"] for r in rows], ["ok", "reauth-needed"])
        capped = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90.0,
                                 measured_at=latest, rotated=False)
        self.assertEqual((capped["colour"], capped["cause_id"]),
                         (bf.ORANGE, "money:measured-capped-unread"))
        self.assertTrue(capped["capped_by_coverage"])
        self.assertEqual(capped["coverage"]["measured"], 1)
        self.assertEqual(capped["coverage"]["unread"], 1)

    def test_overage_and_other_models_scoped_gauges_are_not_plan_walls(self):
        at = "2026-09-18T01:39:00Z"
        history = [{"provider": "anthropic", "account": "acct",
                    "probed_at": at, "status": "allowed", "gauges": [
                        {"label": "5h", "kind": "session",
                         "utilization": 0.2, "reset": 2000},
                        {"label": "7d", "kind": "period",
                         "utilization": 0.3, "reset": 9000},
                        {"label": "overage", "kind": "overage",
                         "utilization": 1.0, "reset": None},
                        {"label": "7d-fable", "kind": "period",
                         "utilization": 1.0, "reset": 9000}]}]
        now = bf.pk.parse_ts_epoch(at) + 10
        rows, latest = bf.anthropic_money_rows(history, now=now)
        self.assertEqual({w["label"] for w in rows[0]["windows"]},
                         {"5h", "7d"})
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90,
                               measured_at=latest, rotated=False)
        self.assertNotEqual(axis["colour"], bf.RED)
        fable, _ = bf.anthropic_money_rows(history, now=now, model="fable")
        self.assertIn("7d-fable", {w["label"] for w in fable[0]["windows"]})
        self.assertNotIn("overage", {w["label"] for w in fable[0]["windows"]})

    def _produced(self, *limits):
        """(history rows, fold instant) for one native account per vendor
        body, taken from what `providers.NativeQuotaProvider._probe_one`
        itself writes. A planted row can say things the producer never does;
        this is the producer's own row, with only the network faked."""
        from helm import providers
        tmp = tempfile.mkdtemp(prefix="helm-test-bf-native-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        prov = providers.NativeQuotaProvider(
            history_path=os.path.join(tmp, "history.jsonl"))
        history = []
        for i, body in enumerate(limits):
            home = os.path.join(tmp, "home-%d" % i)
            os.makedirs(home)
            with open(os.path.join(home, ".credentials.json"), "w") as fh:
                json.dump({"claudeAiOauth": {"accessToken": "FAKE"}}, fh)
            with mock.patch.object(prov, "_get_json",
                                   return_value={"limits": body}):
                _cred, hist = prov._probe_one(
                    {"name": "acct-%d" % i, "provider": "anthropic",
                     "home": home, "tier": None})
            history.append(hist)
        return history, bf.pk.parse_ts_epoch(history[-1]["probed_at"]) + 10

    def test_native_gauge_without_utilization_stays_an_unread_window(self):  # noqa: VACUOUS_ASSERTION — the row-state absence is paired with the positive UNREAD account state from the same producer row, and the window map is asserted exactly
        """task/2935: the producer's own row. The earlier arm planted a
        history row `_probe_one` could not write — its exhausted test raised
        on the None utilization before any row existed."""
        history, now = self._produced([
            {"kind": "session", "percent": None, "resets_at": None},
            {"kind": "weekly_all", "percent": 20,
             "resets_at": "2026-09-25T00:00:00Z"}])
        self.assertEqual(history[0]["status"], "allowed")
        rows, latest = bf.anthropic_money_rows(history, now=now)
        self.assertEqual({w["label"]: w["used_percent"]
                          for w in rows[0]["windows"]}, {"5h": None, "7d": 20.0})
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90,
                               measured_at=latest, rotated=False)
        self.assertTrue(axis["capped_by_coverage"])
        # every gauge unread: the producer says no reading, and so does the
        # fold — unread, never an open account
        history, now = self._produced([
            {"kind": "session", "percent": None, "resets_at": None},
            {"kind": "weekly_all", "percent": None, "resets_at": None}])
        rows, latest = bf.anthropic_money_rows(history, now=now)
        self.assertNotIn(rows[0]["state"], ("ok", "no-window"))
        account = bf._account_money(bf._money_rows(rows)[0], 90)
        self.assertEqual(account["state"], bf.ACCOUNT_UNREAD)

    def test_a_measured_wall_beside_an_unread_weekly_is_walled(self):
        """task/2935 finding 4: the native `no-window` row (its 7d gauge is
        unread) with a MEASURED 5h at 100% lost its wall, because any row
        state other than ok/exhausted was read as outside the table. Windows
        decide first; the row state speaks only where no measured wall or cap
        exists."""
        history, now = self._produced([
            {"kind": "session", "percent": 100,
             "resets_at": "2026-09-18T06:00:00Z"},
            {"kind": "weekly_all", "percent": None, "resets_at": None}])
        self.assertEqual(history[0]["status"], "blocked")
        rows, latest = bf.anthropic_money_rows(history, now=now)
        self.assertEqual(rows[0]["state"], "no-window")
        account = bf._account_money(bf._money_rows(rows)[0], 90)
        self.assertEqual(account["state"], bf.ACCOUNT_WALLED)
        axis = bf.derive_money(bf.NATIVE_FAMILY, rows, ceiling=90,
                               measured_at=latest, rotated=False)
        self.assertEqual((axis["colour"], axis["cause_id"]),
                         (bf.RED, "money:window-wall"))
        self.assertIn("no-window", axis["coverage_why"])
        # CONTROL: the same producer row read past the freshness bound is a
        # STALE row, and a stale 100% is not a wall about now
        stale, _ = bf.anthropic_money_rows(history, now=now + 10 ** 6)
        self.assertEqual(stale[0]["state"], "stale")
        self.assertEqual(bf._account_money(bf._money_rows(stale)[0], 90)
                         ["state"], bf.ACCOUNT_UNREAD)
        self.assertNotEqual(bf.derive_money(
            bf.NATIVE_FAMILY, stale, ceiling=90, measured_at=latest,
            rotated=False)["colour"], bf.RED)


class OneLineTest(unittest.TestCase):
    """F24 exactly one line however many families cross, F15 edge-trigger."""

    def _snapshot(self, colours, now=1789000000.0):
        flags = {}
        for family, colour in colours.items():
            axes = {"money": bf._axis("money", colour, "the reading said so",
                                      "money:test", expires_at=now + 3600,
                                      expires_kind="weekly-reset")}
            flags[family] = bf.compose(family, axes, now=now)
        return {"families": flags, "overall": bf.overall(flags), "ts": now}

    def test_f24_three_families_crossing_at_once_produce_one_line(self):
        snap = self._snapshot({"codex": bf.RED, "gemini": bf.ORANGE,
                               "kimi": bf.RED, "grok": bf.YELLOW})
        text = bf.line(snap)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertLessEqual(len(text.encode("utf-8")), 180)
        self.assertIn(bf.RED, text)
        # the line reports the OVERALL and the bottleneck, never one entry per
        # family: three crossings, one name
        named = [f for f in ("codex", "gemini", "kimi", "grok") if f in text]
        self.assertEqual(len(named), 1)

    def test_f24_the_room_post_is_one_body_for_every_crossing(self):
        snap = self._snapshot({"codex": bf.RED, "gemini": bf.ORANGE,
                               "kimi": bf.RED})
        body, colours = bf.watch_notice(snap["families"], {})
        self.assertEqual(body.count(bf.NOTICE_TAG), 1)
        self.assertEqual(colours, {"codex": bf.RED, "gemini": bf.ORANGE,
                                   "kimi": bf.RED})
        for family in ("codex", "gemini", "kimi"):
            self.assertIn(family, body)

    def test_f15_the_notice_is_latched_on_the_colour_not_the_numbers(self):  # noqa: VACUOUS_ASSERTION — the first crossing is asserted delivered and a later colour change is asserted delivered, both in this method
        snap = self._snapshot({"codex": bf.RED})
        first, colours = bf.watch_notice(snap["families"], {})
        # CONTROL: THE FIRST ONE IS DELIVERED. A latch that suppressed the
        # opening crossing would be silent forever and look identical to a
        # quiet fleet.
        self.assertIsNotNone(first)
        again, _ = bf.watch_notice(snap["families"], colours)
        self.assertIsNone(again)
        moved = self._snapshot({"codex": bf.ORANGE})
        crossing, _ = bf.watch_notice(moved["families"], colours)
        self.assertIsNotNone(crossing)

    def test_f15_the_line_is_content_identical_while_the_colour_holds(self):  # noqa: VACUOUS_ASSERTION — a changed colour is asserted to produce a different line in this method
        """The delivery gate downstream dedupes on the line's own content, so
        an unchanged colour must produce a byte-identical line and a changed
        one must not."""
        now = 1789000000.0
        same = bf.line(self._snapshot({"codex": bf.RED}, now=now), now=now)
        self.assertEqual(same, bf.line(self._snapshot({"codex": bf.RED},
                                                      now=now), now=now))
        self.assertNotEqual(same, bf.line(self._snapshot({"codex": bf.ORANGE},
                                                         now=now), now=now))

    def test_f24_a_missing_snapshot_still_produces_one_line(self):  # noqa: VACUOUS_ASSERTION — the line is asserted to exist and to be one line; the only absence is the GREEN token, whose presence is proven by the other arms on this producer
        text = bf.line(None)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn(bf.GREEN, text)


class VerbTest(unittest.TestCase):
    """The read verb: exit codes, and the refusal on an unknown token."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="burnflags-verb-")
        self.home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self.dir

    def tearDown(self):
        import shutil
        if self.home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.home
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_an_unknown_subcommand_exits_two(self):  # noqa: VACUOUS_ASSERTION — every assertion is a positive exit code
        self.assertEqual(bf.cmd_burn(["bogus"]), 2)
        self.assertEqual(bf.cmd_burn(["why"]), 2)
        self.assertEqual(bf.cmd_burn(["why", "not-a-family"]), 2)

    def test_an_absent_snapshot_exits_three_not_zero(self):  # noqa: VACUOUS_ASSERTION — the sibling arm asserts exit 0 through the same verb once a snapshot exists
        rc = bf.cmd_burn([])
        self.assertEqual(rc, 3)
        self.assertNotEqual(rc, 0)
        self.assertEqual(bf.cmd_burn(["why", "codex"]), 3)

    def test_a_fresh_snapshot_exits_zero(self):
        now = time.time()
        _ignored, inputs = _reference_world(now=now)
        self.assertTrue(bf.write_snapshot(inputs=inputs, now=now))
        self.assertEqual(bf.cmd_burn([]), 0)
        self.assertEqual(bf.cmd_burn(["--json"]), 0)
        self.assertEqual(bf.cmd_burn(["why", "anthropic"]), 0)

    def test_a_declaration_round_trips_through_the_verb(self):
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() + 7200))
        self.assertEqual(bf.cmd_burn(["declare", "kimi", "orange", "--until",
                                      until, "the owner read the fleet"]), 0)
        axis = bf.derive_declared("kimi", bf.read_declarations())
        self.assertEqual(axis["colour"], bf.ORANGE)
        self.assertEqual(axis["cause"], "the owner read the fleet")
        # CONTROL: an unknown family and an unknown colour are both refused,
        # so the acceptance above is the parser and not a write-anything door
        self.assertEqual(bf.cmd_burn(["declare", "nobody", "orange",
                                      "--until", until]), 2)
        self.assertEqual(bf.cmd_burn(["declare", "kimi", "chartreuse",
                                      "--until", until]), 2)
        self.assertEqual(bf.cmd_burn(["declare", "kimi", "orange"]), 2)

    def test_a_declaration_missing_its_colour_prints_the_usage(self):
        # The usage branch writes to stderr: it must reach its own imports.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(bf.cmd_burn(["declare"]), 2)
        self.assertIn("helm burn declare", err.getvalue())

    def test_an_improving_declaration_exits_two_and_names_the_measured_colour(self):
        """A declaration that would lighten a measured colour is refused at
        the door, in the web door's sentence, and nothing is written. Worse,
        equal, and a family with no measured colour stay admitted."""
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() + 7200))
        # The headline colour is YELLOW on purpose: the refusal must name the
        # worst measured AXIS (reach RED), not that headline.
        improving = {"colour": bf.YELLOW,
                     "axes": {"money": bf.YELLOW, "reach": bf.RED,
                              "policy": None}}
        err = io.StringIO()
        with mock.patch.object(bf, "family_flag", return_value=improving), \
                contextlib.redirect_stderr(err):
            rc = bf.cmd_burn(["declare", "kimi", "orange", "--until", until,
                              "would lighten a red reach"])
        self.assertEqual(rc, 2)
        self.assertIn("measured RED", err.getvalue())
        self.assertIn("raise its share", err.getvalue())
        self.assertEqual(
            bf.read_declarations().get("families", {}).get("kimi", {})
            .get("colour", "UNWRITTEN"),
            "UNWRITTEN")
        with mock.patch.object(bf, "family_flag",
                               return_value={"axes": {"money": bf.ORANGE}}):
            self.assertEqual(bf.cmd_burn(["declare", "kimi", "orange",
                                          "--until", until, "held even"]), 0)
        self.assertEqual(bf.read_declarations()["families"]["kimi"]["colour"],
                         bf.ORANGE)
        with mock.patch.object(bf, "family_flag",
                               return_value={"axes": {"money": bf.YELLOW}}):
            self.assertEqual(bf.cmd_burn(["declare", "kimi", "red", "--until",
                                          until, "hold the family"]), 0)
        self.assertEqual(bf.read_declarations()["families"]["kimi"]["colour"],
                         bf.RED)
        with mock.patch.object(bf, "family_flag", return_value=None):
            self.assertEqual(bf.cmd_burn(["declare", "gemini", "orange",
                                          "--until", until,
                                          "nothing measured"]), 0)
        self.assertEqual(
            bf.read_declarations()["families"]["gemini"]["colour"], bf.ORANGE)
        # Equal is admitted at the web door too: the fold keeps a declaration
        # that matches the measured colour, so the door must write it.
        from helm import web
        even = {"colour": bf.ORANGE, "axes": {"money": bf.ORANGE},
                "expires_at": time.time() + 3600}
        with mock.patch.object(bf, "family_flag", return_value=even):
            body, status = web._api_burn_declare({
                "family": "codex", "colour": "ORANGE", "until": "24h",
                "reason": "held even at the web door"})
        self.assertEqual(status, 200, body)
        self.assertEqual(bf.read_declarations()["families"]["codex"]["colour"],
                         bf.ORANGE)

    def test_the_verb_is_wired_into_the_dispatch_table_and_the_help(self):
        from helm import cli
        self.assertIn("burn", cli.VERBS)
        help_text = cli._VERB_HELP["burn"]
        for token in ("why", "declare", "--until", "--json"):
            self.assertIn(token, help_text)


#: The local-names key the catalog names every local family's operator by.
OPERATOR_KEY = "local-operator-seat"


def _declare_operator(name):
    """THE OPERATOR IS THIS HOST'S DATA: its local-names file names the seat,
    as production reads it. None removes the declaration."""
    from helm import home, localnames
    os.makedirs(home.global_dir(), exist_ok=True)
    with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
              encoding="utf-8") as fh:
        json.dump({OPERATOR_KEY: name} if name else {}, fh)
    # the reader's cache is keyed on the file's stat, and a rewrite inside one
    # mtime tick keeps it
    localnames._cache["stat"] = None


class LocalCertificationTest(unittest.TestCase):
    """A LOCAL family — served from the operator's own GPU, which the catalog
    spells `base_url_from` on the family's default pool row — reads money
    GREEN while the seat that runs the hardware certifies it, and GREY again
    the moment the certification lapses or is revoked. Reach is untouched: an
    outage on the local server is still RED.

    NO CERTIFICATION HERE IS HAND-WRITTEN. Every record is minted by the
    producer (`certify_local`, `revoke_local` or the verb) into a temporary
    helm home or file, by seat-a, the declared operator seat, from its own
    roster-bound session. The one record the producer refuses to mint, a
    certification naming a non-local family, is built by re-keying a minted
    record, and that arm asserts the unmutated record still answers."""

    LOCAL = "qwenlocal"
    #: seat-a's harness session, in the shape the harness mints one
    SID = "a1b2c3d4-0000-4000-8000-000000000001"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="burnflags-local-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "certifications.json")
        # THE CHAT SURFACE TOO: the roster the verb checks the certifier
        # against lives there, and it must be this arm's, never the fleet's.
        env = mock.patch.dict(os.environ,
                              {"HELM_HOME": os.path.join(self.dir, "home"),
                               "HELM_CHAT_DIR": os.path.join(self.dir, "chat")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MELD_CHAT_DIR", None)
        # seat-a is the OPERATOR SEAT and is ROSTERED with its own session,
        # through the roster's own writer; seat-b is not rostered at all
        _declare_operator("seat-a")
        from helm import seats
        with self._as_seat("seat-a", session=None):
            seats.write_roster("seat-a", session=self.SID,
                               presence_beat=False)

    @contextlib.contextmanager
    def _as_seat(self, name, session=True):
        """The acting seat is `name` (None: no identity at all), presenting
        `session`: True is the seat's own bound session (seat-a's; no other
        seat has one), None is no session. Every other session key is
        scrubbed, so a runner's own session can never bind the verb to a seat
        this arm did not state."""
        if session is True:
            session = self.SID if name == "seat-a" else None
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name or ""}):
            for key in ("MELD_CHAT_NAME", "CLAUDE_CODE_SESSION_ID",
                        "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
                os.environ.pop(key, None)
            if session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = session
            yield

    def _certified(self, at, until, path=None):
        ok, err = bf.certify_local(self.LOCAL, until, by="seat-a",
                                   session=self.SID,
                                   note="the serving checks passed", now=at,
                                   path=path or self.path)
        self.assertTrue(ok, err)
        return bf.read_local_certifications(path or self.path)

    def _fold(self, certifications, now, upstream=None, critical=None):
        _ref, inputs = _reference_world()
        inputs["local_certifications"] = certifications
        if upstream:
            inputs["upstream"] = dict(inputs["upstream"], **upstream)
        if critical:
            inputs["critical"] = critical
        return bf.fold(inputs, now=now)

    def _verb(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bf.cmd_burn(["certify-local"] + list(args))
        return rc, err.getvalue()

    def test_a_fresh_certification_reads_a_local_familys_money_green(self):
        now, _inputs = _reference_world()
        until = now + 7200
        snap = self._fold(self._certified(now - 60, until), now=now)
        flag = snap["families"][self.LOCAL]
        self.assertEqual((flag["colour"], flag["axis"], flag["axes"]["money"]),
                         (bf.GREEN, "money", bf.GREEN))
        self.assertEqual(flag["cause_id"], "money:certified-local")
        # its own provenance, never a vendor measurement
        self.assertEqual(flag["provenance"], "certified-local")
        self.assertEqual(flag["money_provenance"], "certified-local")
        self.assertIn("operator's own GPU", flag["cause"])
        self.assertIn("seat-a", flag["cause"])
        self.assertIn(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
                      flag["cause"])
        self.assertEqual(flag["measured_at"], now - 60)
        self.assertIn("certified-local",
                      "\n".join(bf.render_why(flag, now=now)))
        self.assertIn("CERTIFIED", "\n".join(bf.render(snap, now=now)))
        # CONTROL: the same world with no certification reads the family
        # GREY for its missing reader, exactly as before this rule existed
        bare = self._fold({"families": {}}, now=now)
        self.assertEqual(bare["families"][self.LOCAL]["colour"], bf.GREY)
        self.assertEqual(bare["families"][self.LOCAL]["cause_id"],
                         "money:no-reader")
        self.assertNotIn("CERTIFIED", "\n".join(bf.render(bare, now=now)))

    def test_a_lapsed_certification_reads_grey_exactly_as_no_certification(self):  # noqa: VACUOUS_ASSERTION — the same record folded one second before its until is asserted GREEN at the end of this method
        now, _inputs = _reference_world()
        certs = self._certified(now - 7200, now + 60)
        lapsed = self._fold(certs, now=now + 60)["families"][self.LOCAL]
        self.assertEqual(lapsed["colour"], bf.GREY)
        self.assertEqual(lapsed["cause_id"], "money:no-reader")
        # a lapse is not an error: the flag is the one no file at all gives
        self.assertEqual(lapsed, self._fold({"families": {}}, now=now + 60)
                         ["families"][self.LOCAL])
        # CONTROL: one second before its own until the same record answers
        self.assertEqual(self._fold(certs, now=now + 59)
                         ["families"][self.LOCAL]["colour"], bf.GREEN)

    def test_a_non_local_family_is_refused_by_the_producer_and_ignored_by_the_fold(self):  # noqa: VACUOUS_ASSERTION — the local family is asserted in the set, a local record is asserted written, and the unmutated record in the same file is asserted GREEN
        local = bf.local_families()
        self.assertIn(self.LOCAL, local)
        self.assertIn("qwen27", local)
        for family in ("kimi", "codex", bf.NATIVE_FAMILY):
            self.assertNotIn(family, local)
        now, _inputs = _reference_world()
        ok, err = bf.certify_local("kimi", now + 7200, by="seat-a",
                                   session=self.SID, now=now,
                                   path=self.path)
        self.assertFalse(ok)
        self.assertIn("base_url_from", err)
        self.assertFalse(os.path.exists(self.path))
        with self._as_seat("seat-a"):
            for args in (("kimi", "--until", "2h"), ("codex", "--revoke"),
                         ("nobody", "--until", "2h")):
                rc, said = self._verb(*args)
                self.assertEqual(rc, 2, args)
                self.assertIn("not a local family", said)
        self.assertFalse(os.path.exists(bf.local_certifications_path()))
        # A FILE THAT NAMES ONE ANYWAY: the producer's own record re-keyed
        # onto kimi, a family with no money reader, so honouring it would
        # turn that family's money GREEN. That re-key is the one mutation.
        certs = self._certified(now - 60, now + 7200)
        self.assertTrue(os.path.exists(self.path))
        certs["families"]["kimi"] = dict(certs["families"][self.LOCAL],
                                         family="kimi")
        flags = self._fold(certs, now=now)["families"]
        self.assertEqual(flags["kimi"]["axes"]["money"], bf.GREY)
        self.assertNotEqual(flags["kimi"]["money_provenance"],
                            "certified-local")
        # CONTROL: the unmutated record in the same file still answers
        self.assertEqual(flags[self.LOCAL]["colour"], bf.GREEN)

    def test_an_outage_stays_red_on_reach_and_overall_with_a_fresh_certification(self):
        now, _inputs = _reference_world()
        certs = self._certified(now - 60, now + 7200)
        bar = _upstream()["upstream"]["codex"]["falsification_bar_s"] or 0
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(now - bar - 60))
        for state in ("AUTH-UNAVAILABLE", "UPSTREAM-5XX"):
            record = {"state": state, "dark": True,
                      "falsification_bar_s": bar, "since": since}
            snap = self._fold(certs, now=now, upstream={self.LOCAL: record},
                              critical=[self.LOCAL])
            flag = snap["families"][self.LOCAL]
            # a live outage contradicts "running optimally": the
            # certification is withdrawn and money reads as uncertified
            self.assertEqual(flag["axes"]["money"], bf.GREY, state)
            self.assertEqual(flag["axes"]["reach"], bf.RED, state)
            self.assertEqual((flag["colour"], flag["axis"]),
                             (bf.RED, "reach"), state)
            self.assertEqual((snap["overall"]["colour"],
                              snap["overall"]["family"]),
                             (bf.RED, self.LOCAL), state)
        # CONTROL: the same world with the server up reads GREEN overall
        # through the same family, so RED above is the outage
        snap = self._fold(certs, now=now, critical=[self.LOCAL])
        self.assertEqual((snap["overall"]["colour"], snap["overall"]["family"]),
                         (bf.GREEN, self.LOCAL))

    def test_a_certified_seat_busy_over_a_healthy_record_stays_green(self):
        """MUST-HIT (the ruling on busy local seats). The certified local
        seat's canary ran past the pass deadline on the proxy's keepalive
        bytes: busy, not failing. The pass keeps its HEALTHY record, so the
        certification stands and the family reads GREEN. CONTROL: the same
        busy reading with no prior record composes UNKNOWN, which withdraws
        the certification: the GREY every busy pass read before the ruling."""
        from helm import proxywatch
        now, _inputs = _reference_world()
        certs = self._certified(now - 60, now + 7200)
        busy = proxywatch._client_timeout(120000, 37)[:3]
        with mock.patch.object(proxywatch, "_upstream_once",
                               return_value=busy):
            reading = proxywatch.upstream_canary(
                "seat-a", family=self.LOCAL,
                sleep=mock.Mock(side_effect=AssertionError("confirmed")))
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        healthy = {"state": "HEALTHY", "dark": False, "since": since}
        priors = {"healthy": {"upstream": {self.LOCAL: dict(
            healthy, seats={"seat-a": dict(healthy)})}}, "none": {}}
        got = {}
        for name, prior in priors.items():
            with mock.patch.object(
                    proxywatch, "_seat_canary_observation",
                    return_value=(reading, None, "NO-REPRESENTATIVE")):
                record = proxywatch.upstream_health(
                    [{"seat": "seat-a", "family": self.LOCAL,
                      "probe": "healthy"}], now=now, prior=prior)[self.LOCAL]
            flag = self._fold(certs, now=now, upstream={self.LOCAL: record})[
                "families"][self.LOCAL]
            got[name] = (record["state"], flag["colour"],
                         flag["axes"]["money"])
        self.assertEqual(got, {"healthy": ("HEALTHY", bf.GREEN, bf.GREEN),
                               "none": ("UNKNOWN", bf.GREY, bf.GREY)}, got)

    def test_expires_at_is_the_certifications_own_until(self):  # noqa: VACUOUS_ASSERTION — both folds are asserted unconditionally after the loop to differ in ts and in reading age
        now, _inputs = _reference_world()
        until = now + 7200
        certs = self._certified(now - 60, until)
        first = self._fold(certs, now=now)
        later = self._fold(certs, now=now + 600)
        for snap in (first, later):
            flag = snap["families"][self.LOCAL]
            self.assertEqual(flag["expires_at"], until)
            self.assertEqual(flag["expires_kind"], "local-certification")
        # CONTROL: the clock moved between the two folds
        self.assertNotEqual(first["ts"], later["ts"])
        self.assertEqual(later["families"][self.LOCAL]["reading_age_s"],
                         first["families"][self.LOCAL]["reading_age_s"] + 600)

    def test_a_duration_until_resolves_to_the_producers_instant_plus_it(self):
        at = 1789000000.0
        for text, secs in (("2h", 7200), ("90m", 5400), ("1d", 86400),
                           ("3600", 3600)):
            ok, err = bf.certify_local(self.LOCAL, text, by="seat-a",
                                       session=self.SID, now=at,
                                       path=self.path)
            self.assertTrue(ok, (text, err))
            rec = bf.read_local_certifications(self.path)["families"][self.LOCAL]
            self.assertEqual((rec["certified_at"], rec["until"]),
                             (at, at + secs), text)
        # an ISO instant is stored as that instant
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at + 1800))
        self.assertTrue(bf.certify_local(self.LOCAL, iso, by="seat-a",
                                         session=self.SID, now=at,
                                         path=self.path)[0])
        stored = bf.read_local_certifications(self.path)
        self.assertEqual(stored["families"][self.LOCAL]["until"], at + 1800)
        # every other spelling, and an instant already past, is refused and
        # leaves the stored record alone
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at - 1))
        for text in ("0h", "2x", "-1h", "1.5h", "", None, past):
            ok, err = bf.certify_local(self.LOCAL, text, by="seat-a",
                                       session=self.SID, now=at,
                                       path=self.path)
            self.assertFalse(ok, text)
            self.assertTrue(err, text)
            self.assertEqual(bf.read_local_certifications(self.path), stored)
        # through the verb: the acting seat is recorded, the note is joined
        with self._as_seat("seat-a"):
            rc, said = self._verb(self.LOCAL, "--until", "2h", "checks",
                                  "passed")
        self.assertEqual(rc, 0, said)
        rec = bf.read_local_certifications()["families"][self.LOCAL]
        self.assertEqual(rec["until"] - rec["certified_at"], 7200)
        self.assertEqual((rec["by"], rec["note"], rec["family"]),
                         ("seat-a", "checks passed", self.LOCAL))
        with self._as_seat("seat-a"):
            self.assertEqual(self._verb(self.LOCAL, "--until", "2x")[0], 2)
            self.assertEqual(self._verb(self.LOCAL)[0], 2)
            self.assertEqual(self._verb(self.LOCAL, "--until", "2h",
                                        "--revoke")[0], 2)

    def test_a_revoke_ends_the_certification_and_the_family_reads_grey(self):  # noqa: VACUOUS_ASSERTION — the same family is asserted GREEN before the revoke and again after the re-certification
        with self._as_seat("seat-a"):
            rc, said = self._verb(self.LOCAL, "--until", "2h")
            self.assertEqual(rc, 0, said)
            certs = bf.read_local_certifications()
            now = certs["families"][self.LOCAL]["certified_at"] + 1
            # CONTROL: before the revoke the family reads GREEN
            self.assertEqual(self._fold(certs, now=now)
                             ["families"][self.LOCAL]["colour"], bf.GREEN)
            rc, said = self._verb(self.LOCAL, "--revoke", "serving", "window")
            self.assertEqual(rc, 0, said)
        rec = bf.read_local_certifications()["families"][self.LOCAL]
        self.assertEqual((rec["revoked_by"], rec["revoke_note"]),
                         ("seat-a", "serving window"))
        self.assertIsInstance(rec["revoked_at"], float)
        self.assertLessEqual(rec["until"], rec["revoked_at"])
        flag = self._fold(bf.read_local_certifications(), now=now)
        self.assertEqual(flag["families"][self.LOCAL]["colour"], bf.GREY)
        self.assertEqual(flag["families"][self.LOCAL]["cause_id"],
                         "money:no-reader")
        # and a fresh certification after it restores GREEN
        with self._as_seat("seat-a"):
            self.assertEqual(self._verb(self.LOCAL, "--until", "2h")[0], 0)
        certs = bf.read_local_certifications()
        later = certs["families"][self.LOCAL]["certified_at"] + 1
        self.assertEqual(self._fold(certs, now=later)
                         ["families"][self.LOCAL]["colour"], bf.GREEN)

    def test_a_certification_with_no_acting_seat_is_refused(self):  # noqa: VACUOUS_ASSERTION — the same call as a seat is asserted to exit 0 and to write the file
        with self._as_seat(None):
            rc, said = self._verb(self.LOCAL, "--until", "2h")
        self.assertEqual(rc, 2)
        self.assertTrue(said)
        self.assertFalse(os.path.exists(bf.local_certifications_path()))
        # CONTROL: the same call as a seat writes the record
        with self._as_seat("seat-a"):
            self.assertEqual(self._verb(self.LOCAL, "--until", "2h")[0], 0)
        self.assertTrue(os.path.exists(bf.local_certifications_path()))

    def test_only_the_exact_family_key_of_a_local_default_row_is_local(self):  # noqa: VACUOUS_ASSERTION — the exact key is asserted to certify and the table with the default row named is asserted local
        """An alias, a project instance label, a provider name and a family
        whose default row is missing are never local, for the producer or for
        `local_families`."""
        now = 1789000000.0
        for name in ("Qwenlocal", "qwen", "qwenlocal/inst-a", "qwenlocal ",
                     "local-vllm", ""):
            ok, err = bf.certify_local(name, now + 7200, by="seat-a",
                                       session=self.SID, now=now,
                                       path=self.path)
            self.assertFalse(ok, name)
            self.assertIn("not a local family", err, name)
        self.assertFalse(os.path.exists(self.path))
        row = {"base_url_from": "k", "rung": "free"}
        missing = {"fam": {"pool_default": "gone",
                           "pool_providers": {"here": row}}}
        self.assertEqual(bf.local_families(missing), ())
        self.assertEqual(bf.local_families({"fam": {"pool_providers":
                                                    {"here": row}}}), ())
        # CONTROL: the same table with the default naming that row is local,
        # and the exact key certifies
        self.assertEqual(bf.local_families(
            {"fam": dict(missing["fam"], pool_default="here")}), ("fam",))
        self.assertTrue(bf.certify_local(self.LOCAL, now + 7200, by="seat-a",
                                         session=self.SID,
                                         now=now, path=self.path)[0])

    def test_a_hand_written_file_greens_no_paid_family_and_a_bad_one_reads_grey(self):  # noqa: VACUOUS_ASSERTION — the producer's own local row in the same file is asserted GREEN
        from helm import pk
        now, _inputs = _reference_world()
        # THE ONE ROW NOT TYPED BY HAND is the local family's: the producer's
        # own record, minted into a scratch file and copied in. A hand-typed
        # local row carries no session key and reads GREY unverifiable
        # (CertifierSessionTest), so it could not be this arm's control.
        minted = self._certified(now - 60, now + 7200,
                                 path=self.path + ".minted")
        fresh = dict(minted["families"][self.LOCAL])
        paid = ("kimi", "ds4pro", "grok", "codex", bf.NATIVE_FAMILY)
        rows = {f: dict(fresh, family=f) for f in paid + ("qwenlocal/inst-a",)}
        rows[self.LOCAL] = dict(fresh)
        pk.atomic_write(self.path, json.dumps({"families": rows}))
        snap = self._fold(bf.read_local_certifications(self.path), now=now)
        flags = snap["families"]
        for family in paid:
            self.assertNotEqual(flags[family]["money_provenance"],
                                bf.CERTIFIED_LOCAL, family)
        self.assertNotIn("qwenlocal/inst-a", flags)
        self.assertEqual(snap["readers"]["certified_local"], [self.LOCAL])
        # CONTROL: the local row of the same file answers
        self.assertEqual(flags[self.LOCAL]["money_provenance"],
                         bf.CERTIFIED_LOCAL)
        bare = self._fold({"families": {}}, now=now)["families"][self.LOCAL]
        # A FILE THAT DOES NOT PARSE, OR PARSES TO THE WRONG SHAPE, is not an
        # absent file: it reads GREY with a cause that names it
        for text in ("{corrupt", "[]", '{"families": []}'):
            pk.atomic_write(self.path, text)
            flag = self._fold(bf.read_local_certifications(self.path),
                              now=now)["families"][self.LOCAL]
            self.assertEqual(flag["colour"], bf.GREY, text)
            self.assertEqual(flag["cause_id"],
                             "money:certification-unreadable", text)
            self.assertIn(self.path, flag["cause"], text)
            self.assertNotEqual(flag["cause"], bare["cause"], text)
        # a well-formed file whose ROW is malformed holds no certification
        for text in ('{"families": {"qwenlocal": "yes"}}',
                     '{"families": {"qwenlocal": {"until": "2099-01-01T00:00:00Z", "by": "seat-a"}}}',
                     '{"families": {"qwenlocal": {"until": NaN, "by": "seat-a"}}}',
                     '{"families": {"qwenlocal": {"until": 1e999, "by": "seat-a"}}}',
                     '{"families": {"qwenlocal": {"until": %r, "by": 7}}}'
                     % (now + 7200)):
            pk.atomic_write(self.path, text)
            flag = self._fold(bf.read_local_certifications(self.path),
                              now=now)["families"][self.LOCAL]
            self.assertEqual(flag, bare, text)

    def test_an_until_past_the_one_day_ceiling_is_refused(self):  # noqa: VACUOUS_ASSERTION — the same verb at exactly the ceiling is asserted to exit 0 and write the file
        self.assertEqual(bf.CERTIFICATION_CEILING_S, 86400)
        two_days = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(time.time() + 2 * 86400))
        with self._as_seat("seat-a"):
            for text in ("25h", "86401", "1789000000", two_days):
                rc, said = self._verb(self.LOCAL, "--until", text)
                self.assertEqual(rc, 2, text)
                self.assertIn("ceiling", said, text)
            # A BARE NUMBER IS A DURATION IN SECONDS, so an epoch typed as an
            # instant is refused by the ceiling and told how to write one
            said = self._verb(self.LOCAL, "--until", "1789000000")[1]
            self.assertIn("seconds", said)
            self.assertIn("ISO-8601", said)
        self.assertFalse(os.path.exists(bf.local_certifications_path()))
        # CONTROL: exactly the ceiling is a certification
        with self._as_seat("seat-a"):
            rc, said = self._verb(self.LOCAL, "--until", "1d")
        self.assertEqual(rc, 0, said)
        rec = bf.read_local_certifications()["families"][self.LOCAL]
        self.assertEqual(rec["until"] - rec["certified_at"], 86400)

    def test_an_unrostered_or_sessionless_acting_seat_is_refused(self):  # noqa: VACUOUS_ASSERTION — the operator's bound session through the same verb is asserted to exit 0 and be recorded as the certifier
        # an unrostered seat, and the rostered operator with NO session (the
        # timer shape round 2 admitted): neither has a session behind it
        from helm import home
        for name in ("seat-b", "seat-a"):
            with self._as_seat(name, session=None):
                self.assertIsNone(home.session_id())
                for args in (("--until", "2h"), ("--revoke",)):
                    rc, said = self._verb(self.LOCAL, *args)
                    self.assertEqual(rc, 2, (name, args, said))
                    self.assertIn("roster", said, (name, args))
        self.assertFalse(os.path.exists(bf.local_certifications_path()))
        # CONTROL: the operator seat in its own bound session certifies and
        # is recorded as the certifier
        with self._as_seat("seat-a"):
            self.assertEqual(home.session_id(), self.SID)
            rc, said = self._verb(self.LOCAL, "--until", "2h")
        self.assertEqual(rc, 0, said)
        self.assertEqual(
            bf.read_local_certifications()["families"][self.LOCAL]["by"],
            "seat-a")

    def test_a_family_with_a_paid_row_beside_its_local_default_is_not_local(self):  # noqa: VACUOUS_ASSERTION — the same entry with only its local default row is asserted local
        from helm import seat
        real = seat.FAMILIES["ds4pro"]
        self.assertNotIn("ds4pro", bf.local_families())
        # THE ONE MUTATION: the real ds4pro entry with its default row served
        # from the operator's box beside a paid alternate row. The catalog no
        # longer ships a two-row family (ds4pro is the DeepSeek direct key
        # alone since ds4-direct-off-peak-only), so the paid row is planted.
        default = real["pool_default"]
        rows = {default: dict(real["pool_providers"][default],
                              base_url_from="ds4pro"),
                "paid-alternate": {"base_url": "https://paid.example.invalid/v1"}}
        self.assertEqual(len(rows), 2)
        mixed = {"ds4pro": dict(real, pool_providers=rows)}
        self.assertEqual(bf.local_families(mixed), ())
        # CONTROL: the same entry with only that local row is local
        only = {"ds4pro": dict(real, pool_providers={default: rows[default]})}
        self.assertEqual(bf.local_families(only), ("ds4pro",))
        for family in bf.local_families():
            entry = seat.FAMILIES[family]
            self.assertTrue(all(r.get("base_url_from") for r in
                                entry["pool_providers"].values()), family)
        ok, err = bf.certify_local("ds4pro", 1789007200.0, by="seat-a",
                                   session=self.SID,
                                   now=1789000000.0, path=self.path)
        self.assertFalse(ok)
        self.assertIn("not a local family", err)

    def test_the_new_verb_is_named_in_the_usage_the_help_and_the_docs(self):
        from helm import cli_help
        for text in (bf._USAGE, cli_help._VERB_HELP["burn"]):
            for token in ("certify-local", "--revoke", "--until"):
                self.assertIn(token, text)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "VERBS.md"),
                  encoding="utf-8") as fh:
            self.assertIn("helm burn certify-local", fh.read())
        self.assertIn("local models should always be green burnflag",
                      bf.__doc__)


class CertifierSessionTest(unittest.TestCase):
    """ONLY THE OPERATOR SEAT, FROM ITS OWN ROSTER-BOUND SESSION, CERTIFIES.

    A declared name is a value any process can export. With no session behind
    it, HELM_CHAT_NAME certified as any rostered seat, and the fold took a
    record's `by` at its word, so a forged row turned a local family GREEN.
    The certifier is now the family's DECLARED operator seat, admitted
    through its own session, and every record carries that session's key.
    The fold honours a record only while the key still binds to the operator
    on the roster, and reads every other record GREY with a cause of its own.

    EVERY ARM RUNS THE PRODUCTION PATH: the verb writes the record, the
    posting pass (`proxywatch._burn_flags_pass`) folds it and writes the
    snapshot, and the colour is read back through `burnflags.cached_flags`.
    A record the verb refuses to write is made by ONE mutation of a record it
    did write, and the unmutated record is asserted GREEN in the same arm."""

    LOCAL = "qwenlocal"  # noqa: SEAT_NAME — a catalog FAMILY key, never a seat
    OPERATOR, OTHER = "seat-a", "seat-b"
    #: Harness session ids in the shape the harness mints them, so their
    #: sid8 keys differ.
    SID = {"seat-a": "a1b2c3d4-0000-4000-8000-000000000001",
           "seat-b": "e5f6a7b8-0000-4000-8000-000000000002"}
    UNVERIFIABLE = "money:certification-unverifiable"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="burnflags-certifier-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # THE CHAT SURFACE TOO: the roster the session binds through lives
        # there, and it must be this arm's, never the fleet's
        env = mock.patch.dict(os.environ,
                              {"HELM_HOME": os.path.join(self.dir, "home"),
                               "HELM_CHAT_DIR": os.path.join(self.dir, "chat"),
                               "HELM_CACHE_DIR": os.path.join(self.dir,
                                                              "cache")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MELD_CHAT_DIR", None)
        _declare_operator(self.OPERATOR)
        # BOTH SEATS JOIN WITH THEIR OWN SESSIONS, through the roster's writer
        from helm import seats
        for name in (self.OPERATOR, self.OTHER):
            with self._acting(name, None):
                seats.write_roster(name, session=self.SID[name],
                                   presence_beat=False)

    @contextlib.contextmanager
    def _acting(self, name, session):
        """The process declares `name` (None: nothing) and presents harness
        session `session` (None: none). Every session variable is set here,
        so a runner's own session can never bind the verb to a seat."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name or ""}):
            for key in ("MELD_CHAT_NAME", "CLAUDE_CODE_SESSION_ID",
                        "CLAUDE_SESSION_ID", "CODEX_SESSION_ID"):
                os.environ.pop(key, None)
            if session:
                os.environ["CLAUDE_CODE_SESSION_ID"] = session
            yield

    def _verb(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bf.cmd_burn(["certify-local"] + list(args))
        return rc, err.getvalue()

    def _certify(self, *note):
        """The operator certifies from its own bound session."""
        with self._acting(self.OPERATOR, self.SID[self.OPERATOR]):
            rc, said = self._verb(self.LOCAL, "--until", "2h", *note)
        self.assertEqual(rc, 0, said)

    def _flag(self):
        """The local family's flag as production reads it: the posting pass
        folds and writes the snapshot, `cached_flags` reads it back."""
        from helm import proxywatch
        now = time.time()
        report = {"ts": now, "seats": [], "upstream": {}, "proxy_runtime": {}}
        with mock.patch.object(bf, "usage_history", return_value=[]):
            self.assertIsNotNone(proxywatch._burn_flags_pass(report))
        flags, _age = bf.cached_flags(now=now)
        return flags[self.LOCAL]

    def _unverifiable(self, flag, label):
        self.assertEqual(flag["colour"], bf.GREY, label)
        self.assertEqual(flag["axes"]["money"], bf.GREY, label)
        self.assertEqual(flag["cause_id"], self.UNVERIFIABLE, label)
        self.assertNotEqual(flag["money_provenance"], bf.CERTIFIED_LOCAL,
                            label)
        # the cause names the seat whose session would make it verifiable
        self.assertIn(self.OPERATOR, flag["cause"], label)

    def test_a_sessionless_operator_name_is_refused_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the same name with its bound session is asserted to certify and read GREEN at the end of this method
        path = bf.local_certifications_path()
        for args in ((self.LOCAL, "--until", "2h"), (self.LOCAL, "--revoke")):
            with self._acting(self.OPERATOR, None):
                rc, said = self._verb(*args)
            self.assertEqual(rc, 2, (args, said))
            # the refusal says how to certify: from the operator's own session
            self.assertIn("its own session", said, args)
            self.assertFalse(os.path.exists(path), args)
        # CONTROL: the same name WITH its bound session certifies
        self._certify()
        self.assertEqual(self._flag()["colour"], bf.GREEN)

    def test_a_roster_bound_session_of_a_non_operator_seat_is_refused(self):  # noqa: VACUOUS_ASSERTION — the operator's own session is asserted to certify and read GREEN at the end of this method
        path = bf.local_certifications_path()
        # declared and corroborated, and the session alone: both are seat-b
        for declared in (self.OTHER, None):
            for args in ((self.LOCAL, "--until", "2h"),
                         (self.LOCAL, "--revoke")):
                with self._acting(declared, self.SID[self.OTHER]):
                    rc, said = self._verb(*args)
                self.assertEqual(rc, 2, (declared, args, said))
                self.assertIn("operator seat", said, (declared, args))
                self.assertIn(self.OPERATOR, said, (declared, args))
                self.assertFalse(os.path.exists(path), (declared, args))
        # CONTROL: the operator's own bound session certifies
        self._certify()
        self.assertEqual(self._flag()["colour"], bf.GREEN)

    def test_the_operators_bound_session_certifies_and_stamps_its_key(self):
        from helm.seats_cursor import _sid8
        key = _sid8(self.SID[self.OPERATOR])
        # declared and corroborated, and the session alone: both certify
        for declared in (self.OPERATOR, None):
            with self._acting(declared, self.SID[self.OPERATOR]):
                rc, said = self._verb(self.LOCAL, "--until", "2h", "checks",
                                      "passed")
            self.assertEqual(rc, 0, (declared, said))
            rec = bf.read_local_certifications()["families"][self.LOCAL]
            self.assertEqual((rec["by"], rec.get("session_key")),
                             (self.OPERATOR, key), declared)
            # THE KEY, NEVER THE SESSION ID: a whole id is a bearer value
            with open(bf.local_certifications_path(), encoding="utf-8") as fh:
                self.assertNotIn(self.SID[self.OPERATOR], fh.read())
            flag = self._flag()
            self.assertEqual((flag["colour"], flag["cause_id"],
                              flag["money_provenance"]),
                             (bf.GREEN, "money:certified-local",
                              bf.CERTIFIED_LOCAL), declared)
            self.assertIn(self.OPERATOR, flag["cause"])
        # the revoke comes from the same session and stamps it too
        with self._acting(None, self.SID[self.OPERATOR]):
            rc, said = self._verb(self.LOCAL, "--revoke", "serving", "window")
        self.assertEqual(rc, 0, said)
        rec = bf.read_local_certifications()["families"][self.LOCAL]
        self.assertEqual((rec["revoked_by"], rec.get("revoked_session_key")),
                         (self.OPERATOR, key))
        flag = self._flag()
        self.assertEqual((flag["colour"], flag["cause_id"]),
                         (bf.GREY, "money:no-reader"))

    def test_a_record_that_does_not_bind_to_the_operator_never_reads_green(self):  # noqa: VACUOUS_ASSERTION — the record as the verb wrote it is asserted GREEN before and after the mutations
        from helm import pk
        from helm.seats_cursor import _sid8
        self._certify()
        path = bf.local_certifications_path()
        with open(path, encoding="utf-8") as fh:
            minted = json.load(fh)
        rec = minted["families"][self.LOCAL]
        # CONTROL: the record as the verb wrote it answers GREEN
        self.assertEqual(self._flag()["colour"], bf.GREEN)
        now = time.time()
        forged = {
            # a bare `by`: the record with its session key taken off
            "a bare by": {k: v for k, v in rec.items() if k != "session_key"},
            # a key that binds to another seat, and one no roster knows
            "a key bound to another seat": dict(
                rec, session_key=_sid8(self.SID[self.OTHER])),
            "a key no roster knows": dict(rec, session_key="ffffffff"),
            # a certifier that is not the operator, with and without its own
            # bound session behind it
            "a non-operator certifier": dict(rec, by=self.OTHER),
            "a non-operator with its own key": dict(
                rec, by=self.OTHER, session_key=_sid8(self.SID[self.OTHER])),
            # the row a forger types from nothing
            "a hand-written row": {"family": self.LOCAL, "by": self.OPERATOR,
                                   "certified_at": now - 60,
                                   "until": now + 7200},
        }
        for label, row in forged.items():
            pk.atomic_write(path, json.dumps({"families": {self.LOCAL: row}}))
            self._unverifiable(self._flag(), label)
        # CONTROL: the unmutated record, restored, answers GREEN again
        pk.atomic_write(path, json.dumps(minted))
        self.assertEqual(self._flag()["colour"], bf.GREEN)

    def test_a_replayed_record_is_the_stated_residual(self):
        """THE KEY IS NOT A SECRET, AND THE DOCS SAY SO. A revoked row keeps
        the operator's session key, so a process that can write the file can
        put a later `until` on it and drop the revocation. The fold reads
        that GREEN, because the key still binds to the operator: this is the
        residual the docs state (the same-user trust boundary of the roster
        and the snapshot), MEASURED here so a cure that closes it has to
        flip this arm on purpose, and so no doc can claim a hand-written row
        always reads GREY."""
        from helm import pk
        from helm.seats_cursor import _sid8
        self._certify()
        with self._acting(None, self.SID[self.OPERATOR]):
            rc, said = self._verb(self.LOCAL, "--revoke")
        self.assertEqual(rc, 0, said)
        path = bf.local_certifications_path()
        with open(path, encoding="utf-8") as fh:
            revoked = json.load(fh)["families"][self.LOCAL]
        self.assertEqual(self._flag()["colour"], bf.GREY)
        # the key a replay needs is on the revoked row itself
        self.assertEqual(revoked["session_key"],
                         _sid8(self.SID[self.OPERATOR]))
        now = time.time()
        replay = {k: v for k, v in revoked.items()
                  if not k.startswith("revoke")}
        replay.update(certified_at=now - 60, until=now + 7200)
        pk.atomic_write(path, json.dumps({"families": {self.LOCAL: replay}}))
        self.assertEqual(self._flag()["colour"], bf.GREEN)

    def test_a_session_that_rebinds_elsewhere_reads_grey(self):  # noqa: VACUOUS_ASSERTION — the same record is asserted GREEN before the rebind, after the operator moved to a new session of its own
        from helm import seats, seats_roster
        self._certify()
        self.assertEqual(self._flag()["colour"], bf.GREEN)
        # THE OPERATOR MOVES ON: a new session of its own, the old one kept on
        # its row. A process presenting the old session is still the operator,
        # so the record still binds.
        fresh = "c9d0e1f2-0000-4000-8000-000000000003"
        with self._acting(self.OPERATOR, None):
            seats.write_roster(self.OPERATOR, session=fresh,
                               presence_beat=False)
        self.assertEqual(seats.seat_for_session(self.SID[self.OPERATOR]),
                         self.OPERATOR)
        self.assertEqual(self._flag()["colour"], bf.GREEN)
        # THE ROSTER'S OWN REPAIR HANDS THE CERTIFYING SESSION TO seat-b
        ok, why = seats_roster.disown_session(
            self.OPERATOR, self.SID[self.OPERATOR], to=self.OTHER)
        self.assertTrue(ok, why)
        self.assertEqual(seats.seat_for_session(self.SID[self.OPERATOR]),
                         self.OTHER)
        self._unverifiable(self._flag(), "rebound")

    def test_a_family_with_no_declared_operator_is_certified_by_nobody(self):  # noqa: VACUOUS_ASSERTION — the declared operator is asserted to certify and read GREEN in the middle of this method
        path = bf.local_certifications_path()
        _declare_operator(None)
        for args in ((self.LOCAL, "--until", "2h"), (self.LOCAL, "--revoke")):
            with self._acting(self.OPERATOR, self.SID[self.OPERATOR]):
                rc, said = self._verb(*args)
            self.assertEqual(rc, 2, (args, said))
            self.assertIn("no operator seat", said, args)
            self.assertFalse(os.path.exists(path), args)
        # CONTROL: declared, the same session certifies
        _declare_operator(self.OPERATOR)
        self._certify()
        self.assertEqual(self._flag()["colour"], bf.GREEN)
        # and the record reads GREY the moment the declaration is gone
        _declare_operator(None)
        flag = self._flag()
        self.assertEqual((flag["colour"], flag["cause_id"]),
                         (bf.GREY, self.UNVERIFIABLE))
        self.assertIn("no operator seat", flag["cause"])

    def test_the_catalog_names_each_local_operator_by_a_local_names_key(self):  # noqa: VACUOUS_ASSERTION — every local family is asserted to name the key, and the operator is asserted to resolve to seat-a before the declaration is removed
        from helm import localnames, seat
        self.assertIn(OPERATOR_KEY, localnames.KEYS)
        local = bf.local_families()
        self.assertEqual(set(local), {"qwen27", "qwenlocal", "bonsai"})  # noqa: SEAT_NAME — the catalog FAMILY keys of the local families, never seats
        for family in local:
            self.assertEqual(seat.FAMILIES[family].get("operator_seat_from"),
                             OPERATOR_KEY, family)
        # a family that is not local has no operator to declare
        for family in sorted(set(seat.FAMILIES) - set(local)):
            self.assertNotIn("operator_seat_from", seat.FAMILIES[family],
                             family)
        self.assertEqual(bf.local_operator(self.LOCAL), self.OPERATOR)
        # absent from this host's local names is no operator at all
        _declare_operator(None)
        self.assertIsNone(bf.local_operator(self.LOCAL))

    def test_the_help_and_the_docs_say_session_bound_and_operator_only(self):  # noqa: VACUOUS_ASSERTION — each of the four texts is asserted to carry both phrases, and the docs section to name the new cause
        from helm import cli_help
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "docs", "VERBS.md"),
                  encoding="utf-8") as fh:
            docs = fh.read()
        start = docs.index("#### `helm burn certify-local")
        section = docs[start:docs.index("\n#### ", start + 1)]
        for label, text in (("VERBS.md", section),
                            ("cli_help", cli_help._VERB_HELP["burn"]),
                            ("the verb", bf._cmd_certify_local.__doc__),
                            ("the module", bf.__doc__)):
            self.assertIn("operator seat", text, label)
            self.assertIn("roster-bound session", text, label)
            self.assertNotIn("timer", text, label)
            # a hand-written row carrying the operator's key reads GREEN
            # (test_a_replayed_record_is_the_stated_residual), so no surface
            # may promise that every hand-written row reads GREY
            self.assertNotIn("a hand-written row", text, label)
        for label, text in (("VERBS.md", section),
                            ("cli_help", cli_help._VERB_HELP["burn"]),
                            ("the module", bf.__doc__)):
            flat = " ".join(text.split()).casefold()
            self.assertIn("replay", flat, label)
            self.assertIn("not a secret", flat, label)
        self.assertIn(self.UNVERIFIABLE, section)


class NoNetworkTest(unittest.TestCase):
    """The fold is FILE-ONLY on every path this lane ships."""

    def test_no_socket_is_opened_by_a_fold_or_a_read(self):  # noqa: VACUOUS_ASSERTION — the spy's own list is asserted to hold exactly the control call, so an empty opened[] cannot pass
        import socket
        now, inputs = _reference_world()
        opened = []
        real = socket.socket.connect

        def spy(self, address):
            opened.append(address)
            return real(self, address)

        socket.socket.connect = spy
        try:
            snap = bf.fold(inputs, now=now)
            # the call's RESULT is asserted, not merely that it returned:
            # a fold that answered nothing would open no socket either
            self.assertEqual(sorted(snap["families"]), sorted(bf.families()))
            self.assertTrue(bf.line(snap, now=now))
            self.assertTrue(bf.render(snap, now=now))
            # CONTROL: the spy is installed on the real attribute, proven by
            # calling through it at a port nothing serves
            with self.assertRaises(OSError):
                socket.socket().connect(("127.0.0.1", 1))
        finally:
            socket.socket.connect = real
        self.assertEqual(opened, [("127.0.0.1", 1)])


class UnreadStreakTest(unittest.TestCase):
    """`unread_streaks` — how long helm's OWN copy has answered no reading.

    The seam nothing watched: `keepalive` skips a home whose refresh chain is
    spent, so the cadence rung stays green while that one copy rots. Every row
    here is written into a temp log; nothing reads this machine's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "native-usage-history.jsonl")
        self.now = 1790000000.0

    def _write(self, *rows):
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def _row(self, account, ago_s, status):
        return {"provider": "anthropic", "account": account,
                "status": status, "gauges": [],
                "probed_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - ago_s))}

    def test_a_streak_is_measured_from_the_last_readable_reading(self):
        """FROM THE LAST GOOD READING, never from the first bad row. The
        question a repair needs answered is "how long since helm last knew
        anything about this credential" — the gap BEFORE the failures started
        is part of that silence, not time the copy was known good.

        And a healthy account in the SAME log is the control: it proves the
        filter reads each row's status rather than returning every account."""
        self._write(
            self._row("dead@example.com", 10 * 86400, "allowed"),
            self._row("dead@example.com", 5 * 86400, "reauth-needed (x)"),
            self._row("dead@example.com", 3600, "reauth-needed (x)"),
            self._row("dead@example.com", 60, "reauth-needed (x)"),
            self._row("live@example.com", 10 * 86400, "reauth-needed (x)"),
            self._row("live@example.com", 60, "allowed"))
        rows = bf.unread_streaks(path=self.path, now=self.now)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["unread_s"], 10 * 86400)
        self.assertEqual(row["probes_since"], 3)
        self.assertFalse(row["floor_only"])
        self.assertEqual(row["newest_probe_age_s"], 60)

    def test_an_account_with_no_readable_row_reports_a_floor_not_a_guess(self):
        """Nothing in the retained log ever read it, so the streak is at LEAST
        the log's own span — and the row says that rather than inventing an
        instant it cannot know."""
        self._write(self._row("dead@example.com", 9 * 86400, "no-credentials"),
                    self._row("dead@example.com", 60, "no-credentials"))
        row = bf.unread_streaks(path=self.path, now=self.now)[0]
        self.assertTrue(row["floor_only"])
        self.assertIsNone(row["last_read_at"])
        self.assertEqual(row["unread_s"], 9 * 86400)
        # CONTROL on the same field: give the same account a readable row and
        # the floor becomes a measured instant
        self._write(self._row("dead@example.com", 9 * 86400, "allowed"),
                    self._row("dead@example.com", 60, "no-credentials"))
        measured = bf.unread_streaks(path=self.path, now=self.now)[0]
        self.assertFalse(measured["floor_only"])
        self.assertIsNotNone(measured["last_read_at"])

    def test_no_identity_leaves_this_reader(self):
        """The caller is a rung that PRINTS, so the mask is applied here —
        this module's own law at the only door that could carry an address."""
        self._write(self._row("secretlocalpart@example.com", 9 * 86400,
                              "reauth-needed (x)"),
                    self._row("secretlocalpart@example.com", 60,
                              "reauth-needed (x)"))
        row = bf.unread_streaks(path=self.path, now=self.now)[0]
        # ensure_ascii=False ON PURPOSE: the mask character is non-ASCII, and
        # the escaped rendering would put `…` in the blob so a control
        # looking for the masked form searches text the reader never produced.
        blob = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("secretlocalpart", blob)
        # CONTROL ON THE SAME OBSERVABLE: the masked form of that very account
        # IS in the same blob, so the absence above is the redaction working
        # and not an empty row, an empty read or a serialiser that dropped the
        # field the assertion was looking in.
        from helm import accounts
        self.assertIn(accounts.mask_identity("secretlocalpart@example.com"),
                      blob)
        self.assertIn("@example.com", blob)

    def test_a_readable_newest_row_ends_the_streak(self):  # noqa: VACUOUS_ASSERTION — the same call is asserted to return a streak on the unreadable shape in the same method
        self._write(self._row("a@example.com", 9 * 86400, "reauth-needed (x)"),
                    self._row("a@example.com", 60, "allowed"))
        self.assertEqual(bf.unread_streaks(path=self.path, now=self.now), [])
        # CONTROL: flip only the newest row's status and the streak appears
        self._write(self._row("a@example.com", 9 * 86400, "reauth-needed (x)"),
                    self._row("a@example.com", 60, "reauth-needed (x)"))
        self.assertEqual(len(bf.unread_streaks(path=self.path,
                                               now=self.now)), 1)

    def test_a_missing_log_is_no_rows_and_never_a_raise(self):  # noqa: VACUOUS_ASSERTION — the same call on a written log is asserted to return a row in the same method
        missing = os.path.join(self.tmp.name, "nope.jsonl")
        self.assertEqual(bf.unread_streaks(path=missing, now=self.now), [])
        # CONTROL: the same call reads a log that exists
        self._write(self._row("a@example.com", 9 * 86400, "no-credentials"),
                    self._row("a@example.com", 60, "no-credentials"))
        self.assertEqual(len(bf.unread_streaks(path=self.path,
                                               now=self.now)), 1)


class PoolPaceFoldTest(unittest.TestCase):
    """The pool's WEEKLY pace joins the native family's colour at the ONE
    place every consumer reads (task/3880): the family's colour is the WORSE
    of its 5h axis and the pool's pace colour. An EASE (ORANGE) pool holds a
    GREEN 5h at ORANGE; a GREEN pool never lifts a RED 5h; an unreadable
    (GREY) pool pace is a no-op, never an invented colour.

    The pace arrives as the fold's own input, exactly as the codex runway
    does: this drives `fold` with a synthetic `pool_pace` record in the shape
    `claudepace.pool_reading` emits, and each arm asserts the 5h base colour
    through `derive_money` (the control) before asserting the folded family."""

    def _pace(self, colour, state="unknown", runout=None):
        """One synthetic claudepace pool record, the keys `pool_reading` sets."""
        return {"state": state, "colour": colour, "advice": None, "why": None,
                "accounts": 4, "read": 4, "left_pct": 25.0, "pct_per_hour": 1.0,
                "span_s": 7200, "runout_at": runout, "horizon_at": None,
                "horizon_from": None, "ratio": 1.0}

    def _native_green(self):
        """The owner's own pool, mutated to a strict majority that still
        carries: four Max accounts under half their week -> the 5h axis is
        GREEN. The unmutated capture is ORANGE through the same call, so a
        fold that saw no input cannot pass (NativePoolTest's own law)."""
        cap = _owner_pool()
        rows = json.loads(json.dumps(cap["rows"]))
        for row, pct in zip(rows, (39.0, 34.0, 20.0, 37.0)):
            row["longest_pct"] = pct
            row["windows"][0]["used_percent"] = pct
        return cap, rows

    def _native_red(self):
        """One native account at 100% of a measured window: a window wall,
        RED, the family's hardest 5h reading."""
        cap = _owner_pool()
        now = cap["measured_at"]
        rows = [{"state": "ok", "longest_pct": 100.0,
                 "windows": [{"label": "5h", "seconds": 18000,
                              "used_percent": 100.0, "reset_at": now + 3600,
                              "source": "measured"}]}]
        return cap, rows

    def _fold(self, cap, rows, pace):
        inputs = {"ceiling": cap["ceiling"],
                  "money": {bf.NATIVE_FAMILY: rows},
                  "money_measured_at": {bf.NATIVE_FAMILY: cap["measured_at"]}}
        if pace is not None:
            inputs["pool_pace"] = {bf.NATIVE_FAMILY: pace}
        return bf.fold(inputs, now=cap["measured_at"] + 60)

    def test_orange_pool_holds_a_green_5h_at_orange(self):
        cap, rows = self._native_green()
        # CONTROL: the 5h axis alone is GREEN, and the unmutated pool is ORANGE
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, rows,
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.GREEN)
        # THE POLE the owner named: the pool eases (ORANGE), so the family
        # carries it even though the 5h window still carries.
        flag = self._fold(cap, rows, self._pace(bf.ORANGE, "ease",
                                                cap["measured_at"] + 43200))
        fam = flag["families"][bf.NATIVE_FAMILY]
        self.assertEqual(fam["colour"], bf.ORANGE)
        self.assertEqual(fam["axes"]["money"], bf.ORANGE)
        self.assertEqual(fam["axis"], "money")

    def test_a_green_pool_never_lifts_a_red_5h(self):  # noqa: VACUOUS_ASSERTION — RED tops _RANK, so no pool pace is worse than it; the worse-only law leaves the wall alone and no positive control exists
        cap, rows = self._native_red()
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, rows,
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.RED)
        # The pool can go faster (GREEN); that is a fact about the pool, not
        # a reason to lower a measured wall.
        flag = self._fold(cap, rows, self._pace(bf.GREEN, "faster"))
        self.assertEqual(flag["families"][bf.NATIVE_FAMILY]["colour"], bf.RED)

    def test_a_green_pool_on_a_green_5h_stays_green(self):  # noqa: VACUOUS_ASSERTION — GREEN is the floor of the ordering, so a GREEN pool is never worse than a GREEN 5h; the sibling arm's ORANGE pole is what proves the fold reads the pace
        cap, rows = self._native_green()
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, rows,
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.GREEN)
        flag = self._fold(cap, rows, self._pace(bf.GREEN, "faster"))
        self.assertEqual(flag["families"][bf.NATIVE_FAMILY]["colour"], bf.GREEN)

    def test_an_unreadable_pool_pace_leaves_the_5h_colour_alone(self):  # noqa: VACUOUS_ASSERTION — GREY is off the ordering (_RANK holds no key for it), so an unreadable pace cannot worsen, improve or invent a colour; the 5h reading stands by the module's first law
        cap = _owner_pool()
        rows = json.loads(json.dumps(cap["rows"]))
        # The unmutated capture: the 5h axis reads ORANGE on its own.
        self.assertEqual(bf.derive_money(bf.NATIVE_FAMILY, rows,
                                         ceiling=cap["ceiling"],
                                         measured_at=cap["measured_at"],
                                         rotated=False)["colour"], bf.ORANGE)
        # A pool that could not be read (GREY) invents nothing: the 5h
        # colour stands as it was measured.
        flag = self._fold(cap, rows, self._pace(bf.GREY, "unknown"))
        self.assertEqual(flag["families"][bf.NATIVE_FAMILY]["colour"], bf.ORANGE)
        self.assertEqual(flag["families"][bf.NATIVE_FAMILY]["axes"]["money"],
                         bf.ORANGE)

    def test_read_inputs_carries_the_pool_pace_the_fold_folds(self):  # noqa: VACUOUS_ASSERTION — the fresh-snapshot half asserts the pace IS carried and folded (ORANGE); the absent half's assertNotIn is that same control's no-snapshot counterpart, not the only observable
        """The data actually flows: a fresh claudepace snapshot the watchdog
        pass wrote is read back by `read_inputs` as the `pool_pace` input and
        folded into the family colour the arm above proves. An absent
        snapshot (`claudepace.cached` -> None, the missing watchdog pass) is
        no input at all, so nothing is invented. Mocks the reader, so this is
        the fold's own purity idiom and never touches a live path."""
        cap, rows = self._native_green()
        pace = self._pace(bf.ORANGE, "ease", cap["measured_at"] + 4000)
        snap = {"v": 1, "ts": cap["measured_at"] + 30, "accounts": {},
                "pool": pace}
        with mock.patch("helm.claudepace.cached", return_value=snap), \
                mock.patch.object(bf, "anthropic_money_rows",
                                  return_value=(rows, cap["measured_at"])):
            inputs = bf.read_inputs(now=cap["measured_at"] + 60)
        self.assertEqual(inputs["pool_pace"][bf.NATIVE_FAMILY], pace)
        self.assertEqual(bf.fold(inputs, now=cap["measured_at"] + 60)[
            "families"][bf.NATIVE_FAMILY]["colour"], bf.ORANGE)
        # The unmeasured world: no snapshot, so no `pool_pace` key, and the
        # 5h colour stands as the fold's own reading.
        with mock.patch("helm.claudepace.cached", return_value=None), \
                mock.patch.object(bf, "anthropic_money_rows",
                                  return_value=(rows, cap["measured_at"])):
            inputs = bf.read_inputs(now=cap["measured_at"] + 60)
        self.assertNotIn("pool_pace", inputs)
        self.assertEqual(bf.fold(inputs, now=cap["measured_at"] + 60)[
            "families"][bf.NATIVE_FAMILY]["colour"], bf.GREEN)


if __name__ == "__main__":
    unittest.main()
