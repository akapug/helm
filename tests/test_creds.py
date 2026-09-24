#!/usr/bin/env python3
"""creds/swap tests — the account scorecard row mapping and the rollover-rescue
resume-block shape. Hermetic: the provider, homes list and session rows are all
stubbed in-process; no real cred home, catalog or provider is touched."""
import contextlib
import io
import json
import os
import shutil
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import creds, homes, providers, seat, sessions  # noqa: E402
from helm.providers import ProviderError  # noqa: E402

SID = "aaaaaaaa-1111-2222-3333-444444444444"


class StubProvider:
    """Canned rows in the exact shapes providers.py documents (the
    test_web_quota pattern)."""

    def __init__(self, accounts=(), states=(), windows=()):
        self._accounts = list(accounts)
        self._states = list(states)
        self._windows = list(windows)

    def accounts(self):
        return self._accounts

    def cred_state(self):
        return self._states

    def windows(self):
        return self._windows


class BrokenProvider:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise ProviderError("no quota provider on this machine")
        return boom


def two_account_provider():
    return StubProvider(
        accounts=[{"account": "dry@x.example", "provider": "anthropic", "tier": "max"},
                  {"account": "fresh@x.example", "provider": "anthropic", "tier": "pro"}],
        states=[{"account": "dry@x.example", "cred_state": "dry",
                 "headroom_pct": 2, "resets_at_ms": 1},
                {"account": "fresh@x.example", "cred_state": "ok",
                 "headroom_pct": 80, "resets_at_ms": None}],
        windows=[{"account": "fresh@x.example", "windows_left": 3.5,
                  "windows_per_week": 12.0, "verdict": "plenty"}])


HOMES = [{"name": "h-dry", "identity": "dry@x.example", "provider": "claude",
          "path": "/creds/h-dry", "aliases": ["dry"]},
         {"name": "h-fresh", "identity": "fresh@x.example", "provider": "claude",
          "path": "/creds/h-fresh", "aliases": []}]


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(args))
    return rc, out.getvalue(), err.getvalue()


class RowsTest(unittest.TestCase):
    def test_row_mapping(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=two_account_provider()):
            rows = creds._rows()
        self.assertEqual(len(rows), 2)
        dry = next(r for r in rows if r["account"] == "dry@x.example")
        self.assertEqual(dry["provider"], "anthropic")
        self.assertEqual(dry["tier"], "max")
        self.assertAlmostEqual(dry["headroom"], 0.02)  # pct -> fraction
        self.assertEqual(dry["state"], "dry")
        self.assertEqual(dry["resets_at_ms"], 1)
        self.assertIsNone(dry["windows_left"])
        fresh = next(r for r in rows if r["account"] == "fresh@x.example")
        self.assertAlmostEqual(fresh["headroom"], 0.80)
        self.assertEqual((fresh["windows_left"], fresh["windows_per_week"],
                          fresh["verdict"]), (3.5, 12.0, "plenty"))

    def test_cmd_creds_scorecard(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=two_account_provider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        self.assertIn("helm creds (2 accounts):", out)
        lines = out.splitlines()
        # headroom-desc within provider: fresh (80%) before dry (2%)
        self.assertLess(next(i for i, l in enumerate(lines) if "fresh@x.example" in l),
                        next(i for i, l in enumerate(lines) if "dry@x.example" in l))
        fresh_line = next(l for l in lines if "fresh@x.example" in l)
        self.assertIn("80%", fresh_line)
        self.assertIn("3.5/12.0", fresh_line)
        self.assertIn("plenty", fresh_line)
        dry_line = next(l for l in lines if "dry@x.example" in l)
        self.assertIn("2%", dry_line)
        self.assertIn("now", dry_line)  # resets_at_ms in the past -> "now"

    def test_the_weekly_column_HEADER_says_which_half_the_cell_is(self):
        """A CELL OF THE FORM X/Y IS AMBIGUOUS AND THE DEFAULT READING IS THE
        WRONG ONE. Every other X/Y a reader meets in this domain is
        USED/total, so a header saying only "weekly" invites exactly the
        inverted reading -- and got it: an account at 1.0/8.0 was read as
        barely touched when it had ONE WINDOW LEFT, and a cred plan built on
        that is backwards for every row it names.

        THE HEADER AND THE DATA ARE PINNED TOGETHER, which is the point of
        asserting both here. Pinning the rendered value alone (`3.5/12.0`)
        says nothing about which half is which, so it stays green if the
        producer ever swaps them; pinning the header alone stays green if the
        header keeps saying `left` after the cell stops being left. The
        numerator must be the SMALLER remaining figure for an account the
        provider reports as nearly spent."""
        # EVERY VALUE HERE IS ONE THE PRODUCER REALLY MINTS. `cred_state`
        # comes from providers.py ("ok" / "exhausted" / "unknown" /
        # "expired-token"), and the windows `verdict` vocabulary is
        # "ok" / "no-weekly" / "waste-danger". An invented state and an
        # invented verdict make the arm agree with a renderer that could
        # never receive them -- a fixture that invents its input tests no
        # world, and this rung is about what an operator actually reads.
        provider = StubProvider(
            accounts=[{"account": "low@x.example", "provider": "anthropic",
                       "tier": "max"}],
            states=[{"account": "low@x.example", "cred_state": "exhausted",
                     "headroom_pct": 2, "resets_at_ms": 1}],
            windows=[{"account": "low@x.example", "windows_left": 1.0,
                      "windows_per_week": 8.0, "verdict": "waste-danger"}])
        with mock.patch.object(creds, "default_provider",
                               return_value=provider):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        header = next(l for l in out.splitlines() if "provider" in l
                      and "account" in l)
        self.assertIn("weekly left", header,
                      "the header must name which half the cell is: %r"
                      % header)
        row = next(l for l in out.splitlines() if "low@x.example" in l)
        self.assertIn("1.0/8.0", row,
                      "MUST-HIT: the windows cell rendered at all")
        # AND THE DIRECTION IS REAL, not merely claimed by the header. The
        # provider reports this account as nearly spent, so the figure the
        # cell leads with must be the SMALL one; rendering total/left would
        # leave the header saying "left" over a number that is not.
        self.assertNotIn("8.0/1.0", row,
                         "the cell is left/total; total/left would make the "
                         "header a lie")
        # ASSERTED ON THE VALUE, NOT ON A COLUMN POSITION. Splitting the
        # rendered row and counting fields backwards pins the table LAYOUT,
        # so inserting any column moves the arm's subject without changing
        # what it claims to check -- and the claim here is about which half
        # of the ratio leads, which the ratio itself already says.
        self.assertIn("1.0/8.0", row)
        self.assertNotIn("8.0/1.0", row)

    def test_provider_error_degrades_with_the_reassurance(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=BrokenProvider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 1)
        self.assertIn("no quota provider on this machine", out)
        self.assertIn("sessions/resume still work", out)

    def test_no_accounts(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=StubProvider()):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        self.assertIn("no accounts found", out)


class SwapTest(unittest.TestCase):
    def swap(self, target, rows=None, homes_list=None):
        rows = rows if rows is not None else [
            {"h": "claude", "i": SID, "cwd": "/work/alpha"}]
        with mock.patch.object(homes, "homes_list",
                               return_value=homes_list or HOMES), \
                mock.patch.object(creds, "default_provider",
                                  return_value=two_account_provider()), \
                mock.patch.object(sessions, "rows_for", return_value=rows):
            return run(creds.cmd_swap, [target])

    def test_swap_picks_healthiest_alternative_and_prefixes_env(self):
        rc, out, _ = self.swap("h-dry")
        self.assertEqual(rc, 0)
        self.assertIn("healthiest claude alternative: fresh@x.example (headroom 80%",
                      out)
        self.assertIn(
            "    cd '/work/alpha' && env CLAUDE_CONFIG_DIR=/creds/h-fresh "
            + ResumeCommandTest.UNSET + "claude --resume " + SID, out)

    def test_swap_alias_resolves_the_home(self):
        rc, out, _ = self.swap("dry")
        self.assertEqual(rc, 0)
        self.assertIn("fresh@x.example", out)

    def test_swap_cwd_containing_provider_word_is_not_corrupted(self):
        # the pre-fix substring replace injected the env prefix INSIDE the
        # quoted cd path when the cwd contained "claude "
        rc, out, _ = self.swap(
            "h-dry", rows=[{"h": "claude", "i": SID, "cwd": "/work/claude stuff"}])
        self.assertEqual(rc, 0)
        self.assertIn(
            "    cd '/work/claude stuff' && env CLAUDE_CONFIG_DIR=/creds/h-fresh "
            + ResumeCommandTest.UNSET + "claude --resume " + SID, out)
        self.assertNotIn("cd '/work/env ", out)

    def test_swap_no_alternative(self):
        solo = [HOMES[0]]
        prov = StubProvider(
            accounts=[{"account": "dry@x.example", "provider": "anthropic"}],
            states=[{"account": "dry@x.example", "cred_state": "dry", "headroom_pct": 2}])
        with mock.patch.object(homes, "homes_list", return_value=solo), \
                mock.patch.object(creds, "default_provider", return_value=prov):
            rc, out, _ = run(creds.cmd_swap, ["h-dry"])
        self.assertEqual(rc, 1)
        self.assertIn("no alternative claude account", out)

    def test_swap_unknown_home(self):
        with mock.patch.object(homes, "homes_list", return_value=HOMES):
            rc, _, err = run(creds.cmd_swap, ["ghost"])
        self.assertEqual(rc, 1)
        self.assertIn("no cred home matches 'ghost'", err)

    def test_swap_usage(self):
        rc, _, err = run(creds.cmd_swap, [])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_swap_no_recent_sessions(self):
        rc, out, _ = self.swap("h-dry", rows=[])
        self.assertEqual(rc, 0)
        self.assertIn("no recent claude sessions in the catalog", out)


class ResumeCommandTest(unittest.TestCase):
    """Pin sessions.resume_command's exact per-harness format — the string
    cmd_swap's env-prefix injection depends on. The child-stamp unset run
    (env -u …, child-stamp-kills-seat-persistence) precedes the harness
    binary so a paste into a stamped shell can't resume as a subprocess
    child (transcript persistence silently OFF)."""

    # DERIVED FROM THE SEAM, NEVER RE-SPELLED. This constant used to hand-copy
    # the child-stamp prefix, so it went stale the instant the proxy triple
    # joined it (#107) — five tests failed for pinning a prefix rather than a
    # behaviour. What THESE tests own is COMPOSITION: that the command carries
    # the prefix, in the right place, without corrupting the quoted cd. The
    # prefix's CONTENTS are pinned exactly once, in test_seat's
    # paste-prefix arm, which fails if either register goes missing.
    UNSET = seat.paste_unset_prefix()

    def test_claude_format(self):
        self.assertEqual(
            sessions.resume_command({"h": "claude", "i": SID, "cwd": "/work/alpha"}),
            "cd '/work/alpha' && " + self.UNSET + "claude --resume " + SID)

    def test_codex_format(self):
        self.assertEqual(
            sessions.resume_command({"h": "codex", "i": SID, "cwd": "/work/beta"}),
            "cd '/work/beta' && " + self.UNSET + "codex resume " + SID)

    def test_missing_cwd_defaults_to_dot(self):
        self.assertEqual(
            sessions.resume_command({"h": "claude", "i": SID, "cwd": None}),
            "cd '.' && " + self.UNSET + "claude --resume " + SID)


if __name__ == "__main__":
    unittest.main()


class UnknownNamesItsReasonTest(unittest.TestCase):
    """An UNKNOWN account must say WHY, and must stay unknown while saying it.

    Measured 2026-08-26 during a live provider wall: `helm seat doctor` named the
    wall to the second while `helm creds` printed "unknown" for all seven codex
    accounts — whose status field said `needs_reauth` the whole time, and whose
    note said the token is refreshed on the next codex launch. Both were computed
    by the provider and dropped by the row builder, so the table was true and
    unactionable at the exact moment somebody would consult it."""

    @staticmethod
    def _provider(status="needs_reauth", note="token rejected; refreshed on launch",
                  verdict=None, accounts=1):
        names = ["a%d@x.example" % i for i in range(accounts)]
        return StubProvider(
            accounts=[{"account": n, "provider": "codex", "tier": ""} for n in names],
            states=[{"account": n, "cred_state": "unknown", "status": status,
                     "note": note, "headroom_pct": None, "resets_at_ms": None}
                    for n in names],
            windows=([{"account": n, "windows_left": 1.0, "windows_per_week": 2.0,
                       "verdict": verdict} for n in names] if verdict else []))

    def _run(self, prov):
        with mock.patch.object(creds, "default_provider", return_value=prov):
            return run(creds.cmd_creds, [])

    def test_the_status_reaches_that_accounts_own_line(self):
        rc, out, _ = self._run(self._provider())
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if "a0@x.example" in l)
        self.assertIn("needs_reauth", line,
                      "the reason never reached the row it explains: %r" % line)
        # AND IT IS STILL UNKNOWN. Naming the reason must not promote the row to a
        # state the probe never established — that is the whole failure this row
        # exists inside (a negative and an unreadable must not share a value).
        self.assertIn("unknown", line)

    def test_a_real_windows_verdict_is_never_overwritten(self):
        """MUST-MISS for the fill: a row that HAS a verdict keeps it.

        The status only fills a cell that was empty. If this ever inverts, the
        stronger fact (a measured windows verdict) is destroyed by a probe
        failure reason, which is strictly worse than the blank it replaced."""
        rc, out, _ = self._run(self._provider(verdict="plenty"))
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if "a0@x.example" in l)
        self.assertIn("plenty", line)
        self.assertNotIn("needs_reauth", line,
                         "the status overwrote a real verdict: %r" % line)

    def test_one_shared_note_is_footnoted_once_with_its_count(self):
        rc, out, _ = self._run(self._provider(accounts=3))
        self.assertEqual(rc, 0)
        foot = [l for l in out.splitlines() if l.strip().startswith("note (")]
        self.assertEqual(len(foot), 1,
                         "a wall hits every account with the SAME note; one line, "
                         "not one per row: %r" % foot)
        self.assertIn("3 accounts", foot[0])
        self.assertIn("token rejected; refreshed on launch", foot[0])

    def test_no_note_prints_no_footnote(self):
        """MUST-MISS for the footnote: absence of a note must print nothing.

        Without this the footnote block could emit an empty or placeholder line
        for every healthy estate, which is the noise that gets a diagnostic
        ignored."""
        rc, out, _ = self._run(self._provider(note=""))
        self.assertEqual(rc, 0)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: the run really did render a
        # table. Without this the assertion below is equally satisfied by a
        # crashed or empty run, which is the shape that makes an absence
        # assertion prove nothing.
        self.assertIn("a0@x.example", out)
        self.assertIn("unknown", out)
        self.assertEqual([l for l in out.splitlines() if l.strip().startswith("note (")], [])


class StateCellFitsItsVocabularyTest(unittest.TestCase):
    """The state cell must hold every state the provider can emit.

    Found by kimi reviewing the why-unknown surfacing (task/1644). That change
    made the creds table name the REASON an account is unknown, in the verdict
    cell, and did nothing for the STATE cell — a different field, which an 8-char
    column was still mangling. `expired-token` rendered as "expired-", dropping
    the one word that says WHAT expired and reading like a broken string rather
    than a state. Naming a reason does not help a state nobody can read."""

    # THE VOCABULARY IS READ FROM THE PRODUCER, NEVER COPIED. This attribute
    # must not be a literal tuple written here; a review refused a lane for
    # it: a copy is pinned to NOTHING. Adding `credential-expired` at the place
    # states are minted would have left this arm green while production
    # truncated it — the arm would have been measuring the author's memory of
    # the vocabulary instead of the vocabulary. The old copy also listed `dry`,
    # which nothing emits (it is a SEAT condition in creds.py prose), so the
    # fixture asserted over a value production has never produced.
    KNOWN_STATES = providers.CRED_STATES

    def _render(self, state):
        prov = StubProvider(
            accounts=[{"account": "a@x.example", "provider": "anthropic", "tier": ""}],
            states=[{"account": "a@x.example", "cred_state": state, "status": "",
                     "note": "", "headroom_pct": None, "resets_at_ms": None}])
        with mock.patch.object(creds, "default_provider", return_value=prov):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        return next(l for l in out.splitlines() if "a@x.example" in l)

    def test_every_state_the_provider_emits_renders_whole(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertTrue(KNOWN_STATES) and the assertEqual(checked, len(KNOWN_STATES)) after the loop are both positive controls on the same observable: they prove the vocabulary is non-empty AND that every member was actually rendered, so the in-loop assertIn cannot pass by never running
        # UNCONDITIONAL FIRST: the vocabulary is non-empty and the loop below
        # therefore runs. An assertion that lives only inside a for-loop passes
        # perfectly on an empty sequence, which would make this a test of nothing
        # the day someone refactors KNOWN_STATES into a computed list.
        self.assertTrue(self.KNOWN_STATES)
        self.assertIs(self.KNOWN_STATES, providers.CRED_STATES,
                      "the vocabulary must BE the producer's tuple, not a copy "
                      "of it — a copy stays green while production truncates")
        checked = 0
        for state in self.KNOWN_STATES:
            line = self._render(state)
            self.assertIn(state, line,
                          "%r was mangled by the state column: %r" % (state, line))
            checked += 1
        self.assertEqual(checked, len(self.KNOWN_STATES))

    def test_the_RENDERED_column_widens_when_the_producer_gains_a_state(self):
        """THE ARM MY FIRST TWO CUTS COULD NOT BE, and a review killed both.

        Cut one pinned a hardcoded 13 against a tuple COPIED into the test — a
        copy is pinned to nothing. Cut two read the producer but patched it and
        then RECOMPUTED max() in the test, asserting the test's own arithmetic;
        The verdict was exact: "recomputes max itself, and never exercises
        creds._STATE_W/header/row, SO HARDCODED 13 STILL PASSES." An arm that
        survives the regression it exists to catch is worse than no arm.

        So this one renders through cmd_creds and reads the COLUMN. It also
        forced a production fix: the width was a module constant computed at
        import, and creds imported the tuple by name, so patching the producer
        could not move either. Both are now read through the module at render
        time — which is the only reason this arm can work at all."""
        longer = "credential-expired-and-then-some"
        narrow = self._render("ok")
        with mock.patch.object(providers, "CRED_STATES",
                               providers.CRED_STATES + (longer,)):
            wide = self._render("ok")
        # THE OBSERVABLE IS THE RENDERED LINE, not a number this test computed.
        self.assertGreater(len(wide), len(narrow),
                           "adding a longer state to the PRODUCER must widen "
                           "the rendered row; if these are equal the width is "
                           "not derived and a hardcoded number would pass here")
        self.assertEqual(len(wide) - len(narrow), len(longer) - creds._state_w())
        # MUST-MISS: unpatched, the column is back to the real vocabulary.
        self.assertEqual(len(self._render("ok")), len(narrow))

    # THE AST CENSUS THAT USED TO LIVE HERE WAS DELETED ON PURPOSE, and the
    # reason outlives the code. It tried to prove STATICALLY that every way a
    # cred_state can be written is covered by CRED_STATES, and a review found SIX
    # real holes in it across six rounds, each narrower than the last: a copied
    # vocabulary; an arm that recomputed max() so a hardcoded width passed; a
    # scan covering one write seam of three; an exempt filter matching a
    # SUBSTRING so Call(func=Name(...)) escaped; an exemption phrased as a SHAPE
    # so any bare Name escaped; and one keyed on identifier TEXT so a nested def
    # shadowing the parameter escaped. Their call, and it is right: "the
    # verifier is more complex than the incident."
    #
    # THE CONTRACT THAT REPLACED IT IS DIRECTLY OBSERVABLE, and the arms below
    # are the whole of it: the render width DERIVES from CRED_STATES, every
    # known state renders WHOLE, and any unknown over-long value is bounded and
    # VISIBLY elided with `+`. That last arm is why the census was never
    # load-bearing — an un-censused state cannot reproduce task/1644, because it
    # renders elided and legible instead of as a broken-looking "expired-".
    # Deliberately NOT replaced with runtime enforcement at the mint site:
    # _probe_one promises "Never raises; failure degrades the row", and trading
    # a display bug for an outage is the wrong direction.


    def test_a_value_OUTSIDE_the_vocabulary_is_cut_VISIBLY(self):
        """`cred_state` is whatever the provider handed back, so a value longer
        than the cell stays possible however the width is derived. The original
        defect was not the missing characters — "expired-token" cut to
        "expired-" reads like a BROKEN STRING, not an elision. So an over-long
        value must still fit the cell AND announce that it was cut."""
        cell = creds._state_cell("credential-expired-and-then-some")
        self.assertEqual(len(cell), creds._state_w())
        self.assertTrue(cell.endswith("+"), "a cut value must say it was cut")
        # MUST-MISS: a value that FITS is never marked.
        self.assertEqual(creds._state_cell("expired-token"), "expired-token")
        self.assertFalse(creds._state_cell("ok").endswith("+"))

    def test_a_state_longer_than_the_column_is_still_bounded(self):
        """MUST-MISS: the render really is width-bounded.

        Without this, the arm above passes just as happily against an unbounded
        column that never truncates anything — which would make it a test of
        nothing. A state longer than the vocabulary must still be cut, so the
        arm above is measuring a WIDTH and not merely the absence of slicing."""
        # POSITIVE CONTROL ON THE SAME OBSERVABLE, before the absence: a SHORT
        # state really does reach the line this method reads. Without it the
        # assertNotIn below is satisfied just as well by a render that produced
        # nothing at all.
        short = self._render("ok")
        self.assertIn("a@x.example", short)
        self.assertIn("ok", short)
        over = "z" * 40
        line = self._render(over)
        self.assertIn("a@x.example", line)
        self.assertNotIn(over, line,
                         "the state cell is unbounded; the sibling arm proves nothing")


class CodexWindowsRideAContinuationLineTest(unittest.TestCase):
    """task/2480: the scorecard prints EVERY window a provider measured, and
    prints it WITHOUT disturbing a single byte of any other family's row."""

    def codex_provider(self, windows):
        return StubProvider(
            accounts=[{"account": "pool@x.example", "provider": "codex", "tier": "Team"},
                      {"account": "fresh@x.example", "provider": "anthropic", "tier": "pro"}],
            states=[{"account": "pool@x.example", "cred_state": "exhausted",
                     "headroom_pct": 0, "resets_at_ms": None, "status": "blocked",
                     "windows": windows},
                    {"account": "fresh@x.example", "cred_state": "ok",
                     "headroom_pct": 80, "resets_at_ms": None}],
            windows=[{"account": "fresh@x.example", "windows_left": 3.5,
                      "windows_per_week": 12.0, "verdict": "plenty"}])

    WINDOWS = [{"label": "7d", "used_percent": 100.0, "reset_after_seconds": 483728},
               {"label": "5h", "used_percent": 52.0, "reset_after_seconds": 5431}]

    def test_both_windows_and_both_resets_reach_the_codex_row(self):
        with mock.patch.object(creds, "default_provider",
                               return_value=self.codex_provider(self.WINDOWS)):
            rc, out, _ = run(creds.cmd_creds, [])
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if l.strip().startswith("windows:"))
        # THE PERCENTAGE CARRIES ITS DIRECTION. The `weekly left` column on
        # the same row runs the other way (remaining over total), so an
        # unlabelled percentage next to it left two numbers about one window
        # disagreeing with nobody to say which was which.
        self.assertIn("7d 100% used resets 134.4h", line)
        self.assertIn("5h 52% used resets 1.5h", line)

    def test_a_family_with_ONE_window_renders_the_one(self):
        with mock.patch.object(
                creds, "default_provider",
                return_value=self.codex_provider(self.WINDOWS[:1])):
            _rc, out, _ = run(creds.cmd_creds, [])
        line = next(l for l in out.splitlines() if l.strip().startswith("windows:"))
        self.assertIn("7d 100%", line)
        self.assertNotIn("5h", line)

    def test_the_NON_codex_table_is_byte_for_byte_what_it_was(self):  # noqa: VACUOUS_ASSERTION — the unconditional assertEqual(fresh_before, fresh_after) is the positive control on the same observable (the rendered non-codex line), and the closing assertTrue(any(startswith('windows:'))) proves the continuation line DID render in that same output, so the assertNotIn cannot pass over a renderer that printed nothing
        """THE CONTROL. The whole reason the per-window detail is a
        continuation line and not a ninth column: a row for a family the
        provider reports one window for must render exactly as it did before
        this lane existed."""
        with mock.patch.object(creds, "default_provider",
                               return_value=two_account_provider()):
            _rc, before, _ = run(creds.cmd_creds, [])
        with mock.patch.object(creds, "default_provider",
                               return_value=self.codex_provider(self.WINDOWS)):
            _rc, after, _ = run(creds.cmd_creds, [])
        fresh_before = next(l for l in before.splitlines() if "fresh@x.example" in l)
        fresh_after = next(l for l in after.splitlines() if "fresh@x.example" in l)
        self.assertEqual(fresh_before, fresh_after)
        # and no continuation line is minted for it
        lines = after.splitlines()
        self.assertNotIn("windows:", lines[lines.index(fresh_after) + 1])
        # THE POSITIVE CONTROL ON THIS ARM: a continuation line DOES exist in
        # this same output, so the assertion above is about placement and not
        # about a feature that never rendered.
        self.assertTrue(any(l.strip().startswith("windows:") for l in lines))


class TheRealProviderReachesTheCellTest(unittest.TestCase):
    """THE PRODUCER IS THE REAL ONE, AND THE CELL IS ITS OWN RATIO.

    An arm that hands the renderer a canned `windows_left` proves that the
    renderer prints what it is given; it says nothing about which half the
    provider puts in the numerator, and a provider that swapped used for
    left -- or raised -- leaves such an arm green. So these arms instantiate
    the real NativeQuotaProvider over a controlled history: two completed
    five-hour cycles at full session utilization, a live probe at 87.5%
    weekly utilization, and no observable session-to-weekly ratio, which by
    the provider's own documented model is intensity 1.0 x the 1/8 fallback
    = 0.125 weekly fraction per window, 0.125 remaining, one window left of
    the eight the week funds. windows() is stubbed NOWHERE; accounts() and
    cred_state() are replaced because they read real credential homes and
    probe the vendor.

    The cell is located by its HEADER, never by column number: the offset of
    "weekly left" in the header line is the offset of the cell in every row,
    because the table is fixed-width by construction."""

    ACCOUNT = "spent@x.example"

    def _provider(self):
        now = time.time()
        d = tempfile.mkdtemp(prefix="helm-creds-history-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "history.jsonl")
        weekly_reset = now + 20 * 3600
        gauge = providers.NativeQuotaProvider._gauge

        def probe(at, session_reset):
            return {"provider": "anthropic", "account": self.ACCOUNT,
                    "probed_at": providers._iso_z(at), "status": "ok",
                    "gauges": [gauge("5h", "session", 100.0, session_reset),
                               gauge("7d", "period", 87.5, weekly_reset)]}
        with open(path, "w", encoding="utf-8") as fh:
            for row in (probe(now - 20100, now - 20000),   # completed cycle
                        probe(now - 10100, now - 10000),   # completed cycle
                        probe(now - 10, now + 3600)):      # the live probe
                fh.write(json.dumps(row) + "\n")
        prov = providers.NativeQuotaProvider(history_path=path)
        prov.accounts = lambda: [
            {"name": self.ACCOUNT, "provider": "anthropic", "home": d,
             "tier": "Max", "usable": True, "active": False,
             "email": self.ACCOUNT}]
        prov.cred_state = lambda: [
            {"account": self.ACCOUNT, "provider": "anthropic",
             "cred_state": "exhausted", "headroom_pct": 0.0, "status": "ok",
             "resets_at_ms": int((now + 3600) * 1000), "tier": "Max",
             "home": d}]
        return prov

    def test_the_real_provider_yields_ONE_window_left_of_EIGHT(self):
        rows = self._provider().windows()
        self.assertEqual(1, len(rows), rows)
        row = rows[0]
        self.assertEqual((2, 0.125), (row["cycles"], row["cost_per_window"]),
                         "MUST-HIT: the controlled history did not produce "
                         "the documented cost model, so the ratio below is "
                         "not the one this arm is about: %r" % (row,))
        self.assertEqual((1.0, 8.0),
                         (row["windows_left"], row["windows_per_week"]), row)
        self.assertEqual("ok", row["verdict"])

    def test_the_cell_under_weekly_left_is_the_providers_own_ratio(self):
        prov = self._provider()
        out = io.StringIO()
        with mock.patch.object(creds, "default_provider", return_value=prov), \
                mock.patch.object(prov, "windows", wraps=prov.windows) as spy, \
                contextlib.redirect_stdout(out):
            rc = creds.cmd_creds([])
        self.assertEqual(0, rc, out.getvalue())
        self.assertGreaterEqual(spy.call_count, 1,
                                "the table was rendered without asking the "
                                "provider for its windows")
        lines = out.getvalue().splitlines()
        header = next(ln for ln in lines if "weekly left" in ln)
        col = header.index("weekly left")
        row = next(ln for ln in lines if self.ACCOUNT in ln)
        self.assertEqual("1.0/8.0", row[col:col + 14].strip(),
                         "the cell under 'weekly left' is not the provider's "
                         "remaining-over-funded ratio:\n%s" % out.getvalue())

