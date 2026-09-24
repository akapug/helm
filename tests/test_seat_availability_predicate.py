"""ONE availability predicate, and every surface that renders availability
reads it.

THE OWNER'S RULING, verbatim: "I basically just want them to be available
whenever their credits work. and shouldnt they show as unavailable in the roster
if their credits dont work automatically."

The state he was reading: a FAMILY-DARK line from `helm proxywatch`, beside a
`helm fleet` row whose only UNKNOWN was the SID/PANE probe column (most rows
carry it, healthy seats included), beside `helm chat seats` calling the same seat
`absent` and HIDING it by default while it held dozens of addressed rows. Three
surfaces, three answers, one seat, one minute. The fact existed the whole time
and only the web console and `helm seat list` could say it.

THE FIVE CONTROLS, and each one drives the SHIPPED PRODUCER over a PLANTED
proxywatch record rather than a hand-built answer:

  (a) MUST-HIT. A planted record carrying kimi AUTH-401 makes the real
      `helm fleet` renderer print UNAVAILABLE, the cause word and the since —
      and the vendor cell never reads UNKNOWN while that record is dark. The
      must-hit is the cause word itself: "AUTH-401" exists NOWHERE in this
      test's fleet table, only in the planted file, so a printed AUTH-401
      proves the shipped join read the record. An arm asserting the absence of
      UNKNOWN alone would pass over a renderer that printed nothing at all.
  (b) THE SAME FIELD FROM THE SAME PREDICATE. The web roster projection's
      published text is compared BYTE-FOR-BYTE against the string the fleet
      cell printed. Two surfaces agreeing on a word is the property; two
      surfaces each deriving a word is the defect this lane closes.
  (c) RECOVERY IS AUTOMATIC AND CHANGES NOTHING ELSE. The same planted record
      flipped to HEALTHY makes the same renderer say AVAILABLE, and the rest
      of the row is asserted IDENTICAL — no relaunch, no second edit, no other
      column moved. That is the owner's "available whenever their credits
      work", and it is also the arm that would catch a cure that cleared the
      wall by degrading everything around it.
  (d) UNAVAILABLE AND UNKNOWN NEVER SHARE A VALUE. A corrupt state file (the
      exact shape once found masquerading as clean) renders UNKNOWN WITH
      the reader's own reason, and asserts BOTH other words absent. A predicate
      that answered UNAVAILABLE here would accuse a provider of a failed read;
      one that answered AVAILABLE would be the founding defect restated.
  (e) THE REVIEWER PICKER PRINTS WHY. There is no picker function in the tree
      to skip anything — `git grep -rn -i least.loaded` over helm/ returns
      nothing — the pick is a human reading `helm lr list`. So the arm drives
      that renderer over a row owed by a walled seat and asserts the mark names
      the vendor, the cause and the fact that no relaunch cures it.

WHY A NEW MODULE. tests/test_fleet.py has 98 methods and ZERO occurrences of
proxywatch or upstream, so there was no fleet arm to extend; and the property
under test is not any one surface's rendering but the AGREEMENT of four, which
only reads as one subject in one file.
"""
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from helm import (dispatches, fleet, landreq, projscope,  # noqa: F401
                  proxywatch, seat, seat_usability, seats, seats_report,
                  web_roster)

SEAT = "kimi"
# The house-convention stand-in for a NATIVE claude seat: no catalogued family
# answers to it, which is the whole point — the availability question does not
# apply to a seat with no proxy behind it. Built directly rather than assembled
# from a real name, which would yield the same value past a weaker check.
NON_PROXY_SEAT = "seat-a"
SINCE = "2026-09-13T00:55:03Z"
CAUSE = "AUTH-401"


def _record(state, dark, since=SINCE):
    """One family record in the shape proxywatch PERSISTS — the composed family
    verdict plus its per-seat members, because `upstream_record` validates the
    schema and `_availability_reason` reaches for the seat member when the
    family token names no cause."""
    return {"state": state, "dark": dark, "since": since,
            "detail": "%s=%s" % (SEAT, state), "ms": 12, "seat": SEAT,
            "members": {SEAT: state},
            "seats": {SEAT: {"state": state, "dark": dark, "since": since}}}


class OneAvailabilityPredicateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-avail-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        self.assertIn(SEAT, seat.FAMILIES,
                      "fixture premise: the seat under test must be a "
                      "catalogued proxy family, or family_for answers "
                      "NO_VENDOR and no arm below is about a wall")

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the planted world ------------------------------------------------
    def _plant(self, state, dark, raw=None):
        """Write the record proxywatch's timer would have written. `raw`
        replaces the whole file body, for the unreadable arm."""
        path = proxywatch._state_path()
        with open(path, "w", encoding="utf-8") as f:
            if raw is not None:
                f.write(raw)
            else:
                json.dump({"ts": time.time(), "fingerprint": "planted",
                           "upstream": {SEAT: _record(state, dark)}}, f)
        return path

    # ---- the shipped fleet renderer, over a table with NO vendor keys -----
    def _fleet_table(self):
        """One row in the shape fleet's census builds, carrying NOTHING about a
        vendor. Every vendor word in the rendered output below therefore came
        from the planted file through the shipped join."""
        return [{"pid": 3038750, "seat": SEAT, "seat_src": "env",
                 "seat_unrenderable": False, "sid": "a6aef16b" + "0" * 28,
                 "sid_src": "record", "candidates": [], "double_open": False,
                 "probe_failed": False, "unknown": False,
                 "probe_unknown": False, "home": "~/.helm/_global/seats/kimi",
                 "cwd": "~/dev", "daemon": None, "daemon_state": "headless",
                 "pane": None, "stamps": 0, "deck": "helm"}]

    def _fleet_render(self):
        """The REAL `helm fleet` render over the REAL vendor join. Only the
        /proc census is doubled — it is not the subject, and leaving it live
        would make every assertion here a fact about this host."""
        table = self._fleet_table()
        for row in table:
            self.assertNotIn("vendor", row)      # the control on the control:
            self.assertNotIn("vendor_text", row)  # the fixture invents no answer
        fleet._join_vendor(table)                 # the SHIPPED producer
        flags = {"census_failed": False, "census_partial": False,
                 "who_failed": False, "daemons_failed": False,
                 "daemons_partial": False, "terms_failed": False}
        buf = StringIO()
        with mock.patch.object(fleet, "rows",
                               return_value=(table, [], flags)), \
                redirect_stdout(buf):
            rc = fleet.cmd_fleet([])
        return buf.getvalue(), rc, table

    def _vendor_cell(self, out):
        cells = [ln.strip()[len("vendor="):] for ln in out.splitlines()
                 if ln.strip().startswith("vendor=")]
        self.assertEqual(len(cells), 1,
                         "the fleet row must carry EXACTLY ONE vendor cell; "
                         "got %r from:\n%s" % (cells, out))
        return cells[0]

    # ---- (a) --------------------------------------------------------------
    def test_a_dark_family_makes_the_fleet_row_read_unavailable(self):
        self._plant(CAUSE, True)
        out, _rc, table = self._fleet_render()
        cell = self._vendor_cell(out)
        # THE MUST-HIT: the cause word lives only in the planted file.
        self.assertIn(CAUSE, cell,
                      "the vendor cell printed no cause word, so the shipped "
                      "join never read the planted record: %r" % cell)
        self.assertEqual("UNAVAILABLE %s %s since %s" % (SEAT, CAUSE, SINCE),
                         cell)
        self.assertNotIn(
            seat_usability.UNKNOWN, cell,
            "the family is measurably dark, so this cell may not read UNKNOWN")
        self.assertEqual(seat_usability.UNAVAILABLE, table[0]["vendor"])
        # the footer names the family once, and says a relaunch is not the cure
        self.assertIn("UNAVAILABLE", out)
        self.assertIn("relaunch does NOT cure any of these", out)
        # AND THE SID COLUMN IS UNTOUCHED. The UNKNOWN the owner saw on this row
        # was the SID/pane probe, a different claim; a cure that silenced it to
        # make this arm pass would delete a true measurement.
        self.assertIn("sid=a6aef16b", out)

    # ---- (b) --------------------------------------------------------------
    def test_b_the_web_roster_publishes_the_same_text(self):
        self._plant(CAUSE, True)
        out, _rc, _table = self._fleet_render()
        cell = self._vendor_cell(out)
        # the console's OWN shipped reader, not a hand-built map
        upstream, age, err = web_roster._upstream_by_family()
        self.assertIsNone(err, "the planted record must read cleanly here or "
                               "this arm is about a failed read instead")
        self.assertIn(SEAT, upstream)
        row = {"seat": SEAT, "runtime": None, "runtime_verified": False}
        web_roster._annotate_upstream(row, upstream, age, err)
        self.assertEqual(seat_usability.UNAVAILABLE, row["availability"])
        self.assertEqual(cell, row["availability_text"],
                         "the console and the fleet table must publish the "
                         "SAME sentence about this seat, or there are two "
                         "derivations of one fact again")
        # the pre-existing raw fields still ride the row: this lane ADDS the
        # one word, it does not replace the console's established contract
        self.assertEqual(CAUSE, row["upstream"])
        self.assertIs(True, row["upstream_dark"])

    # ---- (c) --------------------------------------------------------------
    def test_c_a_green_probe_flips_the_row_and_moves_nothing_else(self):
        self._plant(CAUSE, True)
        dark_out, _rc, _t = self._fleet_render()
        # the ONLY act: the next proxywatch pass records HEALTHY. No relaunch,
        # no seat touched, no second edit anywhere.
        self._plant("HEALTHY", False)
        green_out, _rc2, table = self._fleet_render()
        self.assertEqual("AVAILABLE %s" % SEAT, self._vendor_cell(green_out))
        self.assertEqual(seat_usability.AVAILABLE, table[0]["vendor"])
        dark_rest = [ln for ln in dark_out.splitlines()
                     if not ln.strip().startswith("vendor=")
                     and "UNAVAILABLE" not in ln]
        green_rest = [ln for ln in green_out.splitlines()
                      if not ln.strip().startswith("vendor=")]
        self.assertEqual(dark_rest, green_rest,
                         "flipping the vendor verdict moved something other "
                         "than the vendor cell and its footer")

    # ---- (d) --------------------------------------------------------------
    def test_d_an_unreadable_record_is_unknown_and_never_the_other_two(self):
        self._plant(None, None, raw="{corrupt json[")
        out, _rc, table = self._fleet_render()
        cell = self._vendor_cell(out)
        self.assertTrue(cell.startswith("UNKNOWN %s — " % SEAT), cell)
        # the reader's OWN reason, quoted rather than reinterpreted
        self.assertIn("unreadable", cell)
        self.assertNotIn(seat_usability.UNAVAILABLE, cell)
        self.assertNotIn(seat_usability.AVAILABLE, cell)
        self.assertEqual(seat_usability.UNKNOWN, table[0]["vendor"])
        # and the footer keeps the two apart in as many words
        self.assertIn("NEVER the same answer as UNAVAILABLE", out)

    def test_d2_a_stale_record_is_unknown_not_a_wall_and_not_healthy(self):
        """The staleness rung of the same law: a watcher that stopped describes
        a world that no longer exists, and the last thing it saw must not keep
        asserting itself. Planted with a real record and an OLD `ts`, which is
        the only field that differs from the (a) arm."""
        path = proxywatch._state_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time() - proxywatch.UPSTREAM_CACHE_FRESH_S
                       - 600, "upstream": {SEAT: _record(CAUSE, True)}}, f)
        rec = seat_usability.availability(SEAT)
        self.assertEqual(seat_usability.UNKNOWN, rec["state"])
        self.assertIn("watcher is not running", rec["why"])
        self.assertNotIn(CAUSE, rec["text"],
                         "a stale record may not keep naming its last cause "
                         "as the state NOW")

    # ---- (e) --------------------------------------------------------------
    def test_e_the_land_loop_row_prints_why_a_walled_reviewer_is_skipped(self):
        self._plant(CAUSE, True)
        lr = {"id": "lr-planted-row", "state": "AWAITING_REVIEW",
              "owed_by": "reviewer", "reviewer": SEAT, "author": "seat-b",
              "lane": "a-lane", "stalled": False, "terminal": False,
              "dwell_s": 60, "dwell_known": True, "review_sha": None,
              "created_ts": SINCE}
        look = landreq._availability_lookup()
        self.assertEqual(seat_usability.UNAVAILABLE, look(SEAT)["state"],
                         "the lookup the listing builds must see the planted "
                         "wall, or the arm below proves nothing about it")
        line = landreq._line(lr, avail=look)
        self.assertIn("VENDOR", line)
        self.assertIn(CAUSE, line)
        self.assertIn(SINCE, line)
        self.assertIn("No relaunch cures it", line)
        # A CONTROL ON THE MARK'S SCOPE: the same row, same renderer, with the
        # vendor green, prints NO vendor mark at all. Without this, a mark that
        # fired unconditionally would pass every assertion above.
        self._plant("HEALTHY", False)
        green = landreq._line(lr, avail=landreq._availability_lookup())
        self.assertNotIn("VENDOR", green)
        self.assertNotIn(CAUSE, green)

    # ---- the vocabulary itself -------------------------------------------
    def test_the_three_words_are_three_distinct_values(self):
        """The owner's constraint, asserted on the vocabulary rather than on a
        rendering: no two of these may be equal, and neither verdict word may
        be a SUBSTRING of the not-applicable answer — a surface filtering rows
        by `"UNAVAILABLE" in state` would otherwise match a seat the question
        does not apply to."""
        words = (seat_usability.AVAILABLE, seat_usability.UNAVAILABLE,
                 seat_usability.UNKNOWN, seat_usability.NO_VENDOR)
        self.assertEqual(len(set(words)), 4, words)
        for word in words[:3]:
            self.assertNotIn(word, seat_usability.NO_VENDOR)

    def test_a_seat_with_no_proxy_family_is_not_an_availability_answer(self):
        """A native claude seat has no vendor to be walled by. AVAILABLE there
        would be a confident claim from a signal nobody read; UNKNOWN there
        would badge the MAJORITY of this estate and teach every reader to skim
        the column. It answers NO_VENDOR and renders the empty string, which is
        the same silence web_roster already chose for those seats.

        THE POSITIVE CONTROL RUNS FIRST AND UNCONDITIONALLY: the same planted
        record, the same producer, a PROXY seat — and it must render a non-empty
        cell. Without it an empty string proves nothing, because a producer that
        was never reached renders empty too."""
        self._plant(CAUSE, True)
        proxied = seat_usability.availability(SEAT)
        self.assertEqual(seat_usability.UNAVAILABLE, proxied["state"])
        self.assertNotEqual("", proxied["text"])
        rec = seat_usability.availability(NON_PROXY_SEAT)
        self.assertEqual(seat_usability.NO_VENDOR, rec["state"])
        self.assertEqual("", rec["text"])
        table = [self._fleet_table()[0],
                 dict(self._fleet_table()[0], pid=1, seat=NON_PROXY_SEAT)]
        fleet._join_vendor(table)
        self.assertNotEqual("", table[0]["vendor_text"])     # the control
        self.assertEqual("", table[1]["vendor_text"])


class WalledPaneGoneDispatchTest(unittest.TestCase):
    """The MEASURED defect beside the missing renders: a walled seat whose pane
    is ALSO gone was admitted with NO advisory at all.

    helm/dispatches.py returned (True, None, None) whenever can_take_work was
    False AND pane was False, on the argument that a gone pane is a DELAY (the
    ledger is durable, the beacon replays on relaunch) and saying so on every
    such row would be noise. That argument is sound for a pane-ONLY outage and
    false when a wall is stacked under it: a seat in exactly that shape — a
    named wall with its pane GONE — took review dispatches silently. The row is
    STILL filed, because the wall clears itself; what changed is that the sender
    is told."""

    def _verdict(self, row):
        return mock.patch.object(
            seat_usability, "seat_verdict",
            return_value=(seat_usability.UNUSABLE, row["reason"], row))

    def _row(self, dark):
        return {"seat": "grok", "family": "grok", "pane": False,
                "can_take_work": False, "upstream_dark": dark,
                "upstream": "AUTH-UNAVAILABLE" if dark else "HEALTHY",
                "upstream_since": "2026-09-12T21:09:56Z",
                "reason": "pane GONE — no live process holds this seat"}

    def test_a_wall_under_a_gone_pane_is_named_and_still_admitted(self):
        with self._verdict(self._row(True)):
            ok, refusal, warning = dispatches._validate_recipient_usable(
                "grok", False)
        self.assertTrue(ok, "the row is durable and the wall self-clears, so "
                            "it is filed; only the silence was the defect")
        self.assertIsNone(refusal)
        self.assertIn("AUTH-UNAVAILABLE", warning or "")
        self.assertIn("2026-09-12T21:09:56Z", warning or "")
        self.assertIn("NO LIVE PANE", warning)
        self.assertIn("relaunch alone will NOT", warning)

    def test_a_pane_only_outage_stays_silent(self):
        """The control that proves the cure did not just start warning on every
        between-panes dispatch — which is the noise the original comment was
        right about, and the reason this branch exists at all."""
        with self._verdict(self._row(False)):
            ok, refusal, warning = dispatches._validate_recipient_usable(
                "grok", False)
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIsNone(warning)


class StackedWallRidesThePaneReasonTest(unittest.TestCase):
    """The second measured defect: the verdict ladder puts the PANE rung ahead
    of the WALL rung, deliberately and with a written rationale, so grok's
    roster REASON read only "pane GONE ... `helm seat spawn grok` respawns it"
    while its own upstream COLUMN said AUTH-UNAVAILABLE. The fact was on the
    line and the sentence a reader quotes was the wrong one — and the
    instrument that sentence prescribes would respawn a pane into a dark
    vendor. The precedence is UNCHANGED (it is the right routing answer); the
    reason now carries the wall and says what a respawn will not fix."""

    def _row(self, dark):
        return {"seat": "grok", "family": "grok", "pane": False,
                "scope": "proxy", "unknown": {}, "turn_state": "ok",
                "semantic_age_s": 10, "registered": True,
                "runtime_verified": True, "reachable": True,
                "upstream": "AUTH-UNAVAILABLE" if dark else "HEALTHY",
                "upstream_since": "2026-09-12T21:09:56Z",
                "upstream_dark": dark}

    def test_the_wall_is_named_on_the_pane_rung(self):
        state, why = seat_usability.verdict(self._row(True))
        self.assertEqual(seat_usability.UNUSABLE, state)
        self.assertIn("pane GONE", why)
        self.assertIn("AUTH-UNAVAILABLE", why)
        self.assertIn("does NOT clear it", why)

    def test_a_pane_gone_over_a_healthy_vendor_says_nothing_extra(self):
        state, why = seat_usability.verdict(self._row(False))
        self.assertEqual(seat_usability.UNUSABLE, state)
        self.assertIn("pane GONE", why)
        self.assertNotIn("is ALSO UNAVAILABLE", why)


class RosterSurfacesCarryTheCellTest(unittest.TestCase):
    """`helm chat seats` and the /api/chat presence payload — the two surfaces
    the measurement found carrying ZERO family awareness. The roster row is
    where the owner asked for the word, and the presence payload is what the
    owner's home-dashboard totals strip is fed: every presence row carried
    exactly {seat, runtime, presence, dot, last_seen, status, status_age,
    status_by, line, source, unverified, unverified_session, warn, ephemeral}
    and no vendor key at all, so a walled seat fell into the strip's "gone"
    bucket beside seats nobody had ever launched."""

    def test_the_published_cells_come_from_the_predicate(self):
        avail = {SEAT: seat_usability.availability(
            SEAT, upstream={SEAT: _record(CAUSE, True)})}
        cells = seats_report._avail_cells(avail, SEAT)
        text = "UNAVAILABLE %s %s since %s" % (SEAT, CAUSE, SINCE)
        self.assertEqual(seat_usability.UNAVAILABLE, cells["availability"])
        self.assertEqual(text, cells["availability_text"])
        self.assertEqual(SEAT, cells["availability_family"])
        # THE VERDICT AND THE MARK ARE PUBLISHED, not left to each reader to
        # derive: a consumer comparing the word holds a copy of a vocabulary it
        # does not own, which is how the CLI table and the console came to word
        # one seat two ways. The roster hide rule and the row render read these
        # two keys and nothing else.
        self.assertIs(True, cells["availability_walled"])
        self.assertEqual("⚫ " + text, cells["availability_mark"])
        # AND IT CARRIES NO LEADING WHITESPACE, because the publish boundary
        # strips every laundered string: a mark that indented itself arrived
        # with the indent gone and butted onto the column before it.
        self.assertEqual(cells["availability_mark"].strip(),
                         cells["availability_mark"])

    def test_a_seat_the_question_does_not_apply_to_publishes_no_cell(self):
        """Same shape as the predicate arm: the POSITIVE control on the same
        observable runs unconditionally first, so an empty dict here is the
        seat class answering and not this helper never having run."""
        up = {SEAT: _record(CAUSE, True)}
        avail = {SEAT: seat_usability.availability(SEAT, upstream=up),
                 NON_PROXY_SEAT: seat_usability.availability(
                     NON_PROXY_SEAT, upstream=up)}
        self.assertNotEqual({}, seats_report._avail_cells(avail, SEAT))
        self.assertEqual({}, seats_report._avail_cells(avail, NON_PROXY_SEAT))

    def test_the_cell_text_clears_the_launder_bar_uncut(self):
        """The published sentence must not lose its tail to the 80-byte default
        cap: `_pub_row` clips every string field it does not know about, and an
        UNAVAILABLE cell for an instance seat with a long cause word already
        runs past that. The cap entries are the fix; this arm is what notices
        if a future field is added without one."""
        text = "UNAVAILABLE %s %s since %s" % (SEAT, CAUSE, SINCE)
        cells = seat_usability.availability_cells(
            {"state": seat_usability.UNAVAILABLE, "text": text,
             "family": SEAT})
        row = seats_report._pub_row(dict(cells, seat=SEAT))
        self.assertEqual(text, row["availability_text"])
        self.assertEqual(cells["availability_mark"], row["availability_mark"])
        self.assertEqual(seat_usability.UNAVAILABLE, row["availability"])
        # EVERY STRING KEY THIS PUBLISHES OWES A DECLARED CAP, derived from the
        # cells themselves rather than transcribed — a future field added
        # without one would be clipped at 80 bytes mid-sentence and nothing
        # would say so.
        for key, value in cells.items():
            if isinstance(value, str):
                self.assertIn(key, seats_report._ROW_CAPS,
                              "%s publishes without a declared cap" % key)

class TheWallNeverAssertsAnOriginItCannotProveTest(unittest.TestCase):
    """A dark state says WHAT was observed. Only two tokens say WHERE.

    A PROXY-COOLDOWN is helm's OWN proxy refusing before any request leaves the
    box, so a surface reading "cannot be answered for by their provider" about
    one sends every reader at the provider, the account and the family catalog,
    where no reseed and no cred swap can help. That is the wrong-origin cost
    `_dark_reason` exists to prevent (task/1903), and why this lane reuses it
    instead of writing a second one.

    So the property under test is a NEGATIVE about the short form and a
    POSITIVE about the long one: `text` — the string every row and every badge
    prints — must name the state and assert no origin at all, while `detail`
    must carry the origin classification proxywatch owns."""

    def test_the_short_form_asserts_no_origin_for_any_dark_state(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control on this exact observable (`spelled["text"]`) runs above the loop, beside the must-hit that the vocabulary the loop walks is non-empty
        # THE LOOP'S INPUT IS ASSERTED BEFORE THE LOOP, and one arm is spelled
        # out beside it: a matrix over an empty vocabulary passes silently, and
        # every assertion below is inside the loop.
        self.assertIn("AUTH-401", proxywatch._UPSTREAM_DARK)
        self.assertGreaterEqual(len(proxywatch._UPSTREAM_DARK), 8)
        spelled = seat_usability.availability(
            SEAT, upstream={SEAT: _record("AUTH-401", True)})
        self.assertEqual("UNAVAILABLE %s AUTH-401 since %s" % (SEAT, SINCE),
                         spelled["text"])
        for token in sorted(proxywatch._UPSTREAM_DARK):
            rec = seat_usability.availability(
                SEAT, upstream={SEAT: _record(token, True)})
            self.assertEqual(seat_usability.UNAVAILABLE, rec["state"], token)
            self.assertEqual("UNAVAILABLE %s %s since %s"
                             % (SEAT, token, SINCE), rec["text"])
            for word in ("provider", "vendor", "credential", "account"):
                self.assertNotIn(word, rec["text"].lower(),
                                 "the short form must claim nothing about "
                                 "whose failure %s is" % token)

    def test_the_long_form_carries_the_origin_proxywatch_recorded(self):
        cases = {"PROXY-COOLDOWN": (proxywatch.DARK_OURS, "HELM'S OWN PROXY"),
                 "EMPTY200": (proxywatch.DARK_OUR_VALIDATION,
                              "HELM'S OWN validation"),
                 "AUTH-401": (proxywatch.DARK_UNTYPED, "no cause is asserted")}
        for token, (origin, phrase) in cases.items():
            rec = seat_usability.availability(
                SEAT, upstream={SEAT: _record(token, True)})
            self.assertEqual(origin, rec["origin"], token)
            self.assertIn(phrase, rec["detail"], token)
        # the discriminator: the three glosses are three different sentences
        details = {t: seat_usability.availability(
            SEAT, upstream={SEAT: _record(t, True)})["detail"]
            for t in cases}
        self.assertEqual(3, len(set(details.values())), details)

    def test_the_fleet_footer_quotes_the_long_form_never_its_own_claim(self):
        table = [{"seat": SEAT, "vendor": seat_usability.UNAVAILABLE,
                  "vendor_family": SEAT,
                  "vendor_text": "UNAVAILABLE %s PROXY-COOLDOWN since %s"
                                 % (SEAT, SINCE),
                  "vendor_detail": seat_usability.availability(
                      SEAT, upstream={SEAT: _record("PROXY-COOLDOWN", True)}
                  )["detail"],
                  "vendor_origin": proxywatch.DARK_OURS}]
        footer = " ".join(fleet._vendor_footer(table))
        self.assertIn("HELM'S OWN PROXY", footer)
        self.assertNotIn("by their provider", footer)
        self.assertIn("first green probe", footer)


class OneAcquisitionPerRenderTest(unittest.TestCase):
    """ONE RENDER, ONE INSTANT. The console roster row is composed by TWO
    producers — seats_report stamps the five availability cells, web_roster
    joins the raw upstream fields onto the same row — and each acquired the
    persisted record for itself. The five cells were then written by two
    observations, three fields from the later one, so a vendor RECOVERING in
    that window published AVAILABLE beside the earlier instant's UNAVAILABLE
    mark and a walled=True that steers routing. Overwriting five fields instead
    of three narrows the window; it does not close it.

    THE DOUBLE COUNTS ACQUISITIONS AND MOVES THE WORLD BETWEEN THEM. It wraps
    the two acquisition doors — the snapshot the console hands down and the
    predicate's own fallback read — and REWRITES THE PLANTED FILE before each
    one: the wall for the first, recovery for every later. The producers below
    are the shipped ones reading a real file through the real parse, so a render
    that acquires twice cannot publish an internally consistent row, and a
    render that acquires once cannot publish an inconsistent one. Every arm
    asserts the COUNT and the CONSISTENCY: the count alone would pass over a
    renderer that published nothing at all, and the consistency alone would
    pass over two acquisitions that happened to agree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-avail-one-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        # the fixture's OWN tree: a real absolute path keeps the row shaped
        # like production without pinning this arm to one machine's layout
        self._roster({SEAT: {"session": "s" * 36, "home_room": "helm",
                             "cwd": self.tmp}})
        self._plant(CAUSE, True)
        web_roster._ROSTER_REP_CACHE.clear()

    def tearDown(self):
        web_roster._ROSTER_REP_CACHE.clear()
        if self._prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self, rows):
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump(rows, f)

    def _plant(self, state, dark):
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "fingerprint": "planted",
                       "upstream": {SEAT: _record(state, dark)}}, f)

    def _recorder(self):
        """(acquisitions, patches) — the two acquisition doors, each recording
        its name and REPLANTING the file before the shipped reader touches it:
        the wall for the first acquisition of a render, recovery for every one
        after."""
        acquisitions = []
        real_snap = seat_usability.availability_snapshot
        real_fallback = proxywatch.upstream_snapshot

        def move_the_world():
            state, dark = ((CAUSE, True) if len(acquisitions) == 1
                           else ("HEALTHY", False))
            self._plant(state, dark)

        def snapshot_door(*a, **k):
            acquisitions.append("availability_snapshot")
            move_the_world()
            return real_snap(*a, **k)

        def fallback_door(*a, **k):
            acquisitions.append("upstream_snapshot")
            move_the_world()
            return real_fallback(*a, **k)

        return acquisitions, (
            mock.patch.object(seat_usability, "availability_snapshot",
                              side_effect=snapshot_door),
            mock.patch.object(proxywatch, "upstream_snapshot",
                              side_effect=fallback_door))

    def _assert_the_world_moves(self, acquisitions):
        """THE UNCONDITIONAL POSITIVE CONTROL, run through the SAME armed
        double after the render: the next acquisition really does see a
        recovered vendor. Without it, a consistent row proves nothing — a double
        that served the wall twice would satisfy every assertion above."""
        before = len(acquisitions)
        rec = seat_usability.availability(SEAT)
        self.assertEqual(before + 1, len(acquisitions),
                         "the control did not go through the recorded door")
        self.assertEqual(seat_usability.AVAILABLE, rec["state"],
                         "the double never moved the world, so no arm here is "
                         "about a recovery between two reads")

    def _assert_no_recovery_word(self, rendered):
        """No surface in this render says AVAILABLE of the recovered instant.

        ANCHORED, because AVAILABLE is a SUBSTRING of UNAVAILABLE: a bare
        `assertNotIn` here fails on the correct output, and its inverse — a
        careless `assertIn` — passes on it. The lookbehind is what makes this a
        claim about the recovery word rather than about either word."""
        self.assertIsNone(
            re.search(r"(?<!UN)" + seat_usability.AVAILABLE, rendered or ""),
            "this render quoted the RECOVERED instant: %r" % (rendered,))

    def _assert_row_is_one_instant(self, row):
        """The five published fields describe ONE observation, or this fails.

        Stated as the agreement AMONG them rather than as expected values,
        because the defect is not a wrong word: it is two right words about two
        different moments landing on one row."""
        word, text = row.get("availability"), row.get("availability_text")
        walled, mark = row.get("availability_walled"), row.get(
            "availability_mark")
        self.assertTrue(text, "the row published no availability sentence, so "
                              "there is nothing here to be consistent about")
        self.assertEqual(SEAT, row.get("availability_family"))
        self.assertEqual(walled, word == seat_usability.UNAVAILABLE,
                         "the walled flag and the availability word disagree: "
                         "%r vs %r" % (walled, word))
        self.assertIn(text, mark or text,
                      "the mark does not quote this row's own sentence: "
                      "%r vs %r" % (mark, text))
        self.assertEqual(word, seat_usability.UNAVAILABLE,
                         "the first acquisition saw the wall, so this row must "
                         "read UNAVAILABLE: %r" % (row,))
        self.assertTrue((mark or "").startswith("\u26ab"), mark)
        self.assertIn(CAUSE, text)
        self._assert_no_recovery_word(mark)

    def test_the_console_row_is_one_acquisition_and_one_instant(self):
        acquisitions, (p1, p2) = self._recorder()
        with p1, p2:
            rep = web_roster._roster_cached("helm")
            rows = [s for s in rep.get("seats", [])
                    if s.get("seat") == SEAT]
            self.assertEqual(1, len(rows), rep.get("seats"))
            self.assertEqual(
                ["availability_snapshot"], acquisitions,
                "the console rebuilt ONE roster and acquired the persisted "
                "record %d times; the five cells and the upstream join must "
                "share one acquisition" % len(acquisitions))
            self._assert_row_is_one_instant(rows[0])
            # the raw upstream fields describe the SAME instant as the cells
            self.assertEqual(CAUSE, rows[0].get("upstream"))
            self.assertIs(True, rows[0].get("upstream_dark"))
            self._assert_the_world_moves(acquisitions)

    def test_the_terminal_roster_is_one_acquisition_and_one_instant(self):
        acquisitions, (p1, p2) = self._recorder()
        buf = StringIO()
        with p1, p2:
            with redirect_stdout(buf):
                seats_report.render_roster("helm", True)
            out = buf.getvalue()
            self.assertEqual(
                ["upstream_snapshot"], acquisitions,
                "`helm chat seats` acquired the record %d times for one "
                "screen; the row mark and the walled flag deciding whether the "
                "row is shown at all must be one observation"
                % len(acquisitions))
            self.assertIn(SEAT, out)
            self.assertIn("UNAVAILABLE %s %s since %s" % (SEAT, CAUSE, SINCE),
                          out)
            self._assert_no_recovery_word(out)
            self._assert_the_world_moves(acquisitions)

    def test_the_screen_holds_ONE_memo_scope_over_everything_it_reads(self):
        """THE ONE-ACQUISITION RULE THIS CLASS IS ABOUT, EXTENDED TO THE READS
        THE ROSTER RULE NEVER COVERED.

        `render_roster`'s own comment says one acquisition feeds both halves of
        the screen, and that is true of the ROSTER. Everything the per-row
        helpers reach for was still asked per ROW — `chat.list_rooms()`, a
        getdents over a flat directory holding ninety-eight thousand entries to
        find four hundred rooms, was called once per roster row, 39 times, for
        2.8s of a 4.2s screen. A memo cannot fix that on its own: `projscope`
        caches NOTHING outside a scope, by contract, so the screen has to
        declare that it is one pass.

        THE CONTROL IS THE SAME CALL TAKEN DIRECTLY, FIRST, in this method: the
        report is NOT scoped when something else calls it, so the True below is
        about this door and not about an ambient scope some other fixture left
        open."""
        seen = []

        def report(room, availability=None):
            seen.append(projscope.active())
            return {"room": room, "seats": [], "claims": [],
                    "roster_failed": False}

        with mock.patch.object(seats_report, "roster_report", report):
            direct = seats_report.roster_report("helm")   # the control
            buf = StringIO()
            with redirect_stdout(buf):
                rc = seats_report.render_roster("helm", True)
        self.assertEqual("helm", direct["room"],
                         "the control call really went through this door")
        self.assertEqual(0, rc, buf.getvalue())
        self.assertEqual([False, True], seen,
                         "the direct call is unscoped and the screen's own "
                         "read is inside the screen's pass: %r" % (seen,))
        # the unconditional positive on the exact observable, inline
        with projscope.scope():
            self.assertTrue(projscope.active())
        self.assertFalse(projscope.active(),
                         "and the pass ended with the screen")

    def test_the_report_takes_the_callers_snapshot_rather_than_acquiring(self):
        """The seam itself, asserted where a future caller meets it: handed a
        snapshot, the report acquires NOTHING of its own. This is the arm that
        reddens if the parameter is ever accepted and ignored."""
        acquisitions, (p1, p2) = self._recorder()
        with p1, p2:
            snap = seat_usability.availability_snapshot()
            self.assertEqual(1, len(acquisitions))       # the acquisition
            rep = seats_report.roster_report("helm", availability=snap)
            self.assertEqual(
                ["availability_snapshot"], acquisitions,
                "the report acquired the record again while holding the "
                "caller's snapshot: %r" % (acquisitions,))
            rows = [s for s in rep.get("seats", []) if s.get("seat") == SEAT]
            self.assertEqual(1, len(rows))
            self._assert_row_is_one_instant(rows[0])
            self._assert_the_world_moves(acquisitions)

    def test_the_join_rewrites_every_cell_it_touches(self):
        """THE SECOND LOCK, at the producer that writes the cells rather than at
        the render that acquires. Handed a row ALREADY stamped from an earlier
        observation — exactly what the report hands it — the join must leave five
        fields describing ITS record, never three. One acquisition makes the two
        derivations agree, so this is the arm that still holds when someone
        supplies the triple from somewhere else: a partial write leaves the mark
        and the walled flag of a wall that the record no longer carries."""
        stale = seat_usability.availability_cells(
            seat_usability.availability(
                SEAT, upstream={SEAT: _record(CAUSE, True)}))
        self.assertTrue(stale["availability_walled"])      # the control: these
        self.assertIn(CAUSE, stale["availability_mark"])   # cells ARE a wall
        row = dict(stale, seat=SEAT, runtime=None, runtime_verified=False)
        web_roster._annotate_upstream(
            row, {SEAT: _record("HEALTHY", False)}, 0, None)
        self.assertEqual(seat_usability.AVAILABLE, row["availability"])
        self.assertFalse(row["availability_walled"],
                         "the walled flag still describes the earlier "
                         "observation: %r" % (row,))
        self.assertEqual("", row["availability_mark"],
                         "the row still wears the earlier wall's mark: %r"
                         % (row,))

    def test_the_staleness_bar_travels_inside_the_snapshot(self):
        """THE BAR RIDES WITH THE BYTES. A consumer holding its own number while
        reading someone else's record is the same two-opinions defect one field
        over — the console's bar is deliberately not the predicate's default —
        so `stale_s` is a field of the snapshot.

        The discriminating pair is the SAME aged bytes read under two bars. A
        single UNKNOWN would not say whether the age or the record decided it,
        and the fresh control proves these bytes are a wall to begin with."""
        self._plant(CAUSE, True)
        fresh = seat_usability.availability(
            SEAT, snapshot=seat_usability.availability_snapshot(stale_s=3600))
        self.assertEqual(seat_usability.UNAVAILABLE, fresh["state"],
                         "the control failed: these bytes are not a wall")
        os.utime(proxywatch._state_path(), (time.time() - 1800,) * 2)
        generous = seat_usability.availability(
            SEAT, snapshot=seat_usability.availability_snapshot(stale_s=3600))
        self.assertEqual(seat_usability.UNAVAILABLE, generous["state"],
                         "1800s is inside a 3600s bar, so the same wall stands")
        tight = seat_usability.availability(
            SEAT, snapshot=seat_usability.availability_snapshot(stale_s=60))
        self.assertEqual(seat_usability.UNKNOWN, tight["state"])
        self.assertIn("last wrote", tight["why"])
        self.assertNotIn(CAUSE, tight["text"],
                         "a record judged stale may not keep naming its last "
                         "cause as the state now")


class AMalformedLatchIsNotAMeasuredWallTest(unittest.TestCase):
    """proxywatch normalises a NON-BOOLEAN `dark` conservatively: it keeps
    delivery held and records `dark_invalid`, because a bad boolean must never
    mint recovery. `{"state": "UNKNOWN", "dark": "false"}` therefore normalises
    to dark=True — and a predicate reading `dark is True` published UNAVAILABLE
    from a field helm could not read. That is a delivery HOLD rendered as a
    measured vendor wall, and `availability_walled` is a routing input, not
    only a glyph.

    The surviving evidence is the NAMED state, so a named dark verdict still
    reads UNAVAILABLE and everything else reads UNKNOWN with the malformed
    field quoted. THE HOLD IS NOT TOUCHED: proxywatch's own latch still pauses
    delivery, which every arm below asserts alongside the display word."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-avail-bad-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, state, dark):
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(),
                       "upstream": {SEAT: _record(state, dark)}}, f)
        norm, err = proxywatch.upstream_record(
            {"upstream": {SEAT: _record(state, dark)}}, SEAT)
        self.assertIsNone(err, "fixture premise: proxywatch must ACCEPT this "
                               "record, or the arm is about a rejected shape")
        return norm

    def test_an_unreadable_latch_over_an_unnamed_state_is_unknown(self):
        norm = self._plant("UNKNOWN", "false")
        # the fixture premise, measured on proxywatch's own normalisation: the
        # record this predicate sees really does carry dark=True from a field
        # that was not boolean. Without this the arm proves nothing about the
        # conservative latch.
        self.assertIs(True, norm.get("dark"))
        self.assertIs(True, norm.get("dark_invalid"))
        rec = seat_usability.availability(SEAT)
        self.assertEqual(seat_usability.UNKNOWN, rec["state"])
        self.assertFalse(seat_usability.availability_walled(rec),
                         "an unreadable field may not steer routing")
        self.assertIn("not boolean", rec["why"])
        self.assertNotIn(seat_usability.UNAVAILABLE, rec["text"])
        self.assertTrue(seat_usability.availability_mark(rec).startswith("? "))
        # AND THE DELIVERY HOLD SURVIVES, which is the half this must not cure:
        # proxywatch keeps pausing on its own latch, and the display refuses
        # only to call that pause a measurement.
        self.assertTrue(proxywatch.beacon_paused(norm))

    def test_an_unreadable_latch_over_a_named_wall_stays_unavailable(self):
        """THE POSITIVE CONTROL, and the rule's other half: the named state is
        the surviving evidence. A record whose latch is junk but whose verdict
        is AUTH-401 has been measured dark BY NAME, and softening that would
        trade a false wall for a false recovery."""
        norm = self._plant(CAUSE, "false")
        self.assertIs(True, norm.get("dark_invalid"))
        rec = seat_usability.availability(SEAT)
        self.assertEqual(seat_usability.UNAVAILABLE, rec["state"])
        self.assertTrue(seat_usability.availability_walled(rec))
        self.assertIn(CAUSE, rec["text"])
        self.assertTrue(proxywatch.beacon_paused(norm))

    def test_a_clean_record_is_untouched_by_the_new_arm(self):
        """The second control: a well-formed boolean latch answers exactly as
        before in BOTH directions, so the malformed arm cannot be the reason any
        other arm in this file passes."""
        self._plant(CAUSE, True)
        self.assertEqual(seat_usability.UNAVAILABLE,
                         seat_usability.availability(SEAT)["state"])
        self._plant("HEALTHY", False)
        self.assertEqual(seat_usability.AVAILABLE,
                         seat_usability.availability(SEAT)["state"])


class TheFamilyIsLaunchMetadataNotTheNameTest(unittest.TestCase):
    """A pi harness calls its seat `pi-kimi` while the credential wall belongs
    to family `kimi`, and only the launch metadata the launched process
    self-wrote says so. Two surfaces handed the predicate a NAME alone — the
    fleet table, whose census carries no runtime, and the land-loop listing —
    so both answered NO_VENDOR for exactly the custom-runtime seats whose wall
    they exist to explain: a dark family on the console roster beside silence
    in the same minute on the board and the estate table.

    The cure is at the producer: supplied no runtimes, `availability_map` asks
    the roster rather than treating a name as authority. Every arm pairs the
    custom-runtime seat with a PLAIN seat through the same call, so a producer
    that answered nothing at all cannot pass."""

    PI = "pi-" + SEAT              # parses to no family: `pi` is not one, and
    #                                the tail is not a digit

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-avail-rt-")
        self._prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(),
                       "upstream": {SEAT: _record(CAUSE, True)}}, f)
        # the fixture premise: this display name really does resolve to no
        # family on its own, so every family word below came from the roster
        fam, err = seat.family_for(self.PI)
        self.assertIsNone(fam)
        self.assertIsNotNone(err)

    def tearDown(self):
        if self._prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _roster(self, verified):
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({self.PI: {"session": "p" * 36,
                                 "runtime": {"family": SEAT},
                                 "runtime_verified": verified},
                       SEAT: {"session": "k" * 36}}, f)

    def _fleet_rows(self):
        table = [{"seat": self.PI, "seat_unrenderable": False},
                 {"seat": SEAT, "seat_unrenderable": False}]
        fleet._join_vendor(table)
        return {row["seat"]: row for row in table}

    def test_the_fleet_table_bills_the_wall_to_the_verified_family(self):
        self._roster(True)
        rows = self._fleet_rows()
        self.assertEqual(seat_usability.UNAVAILABLE, rows[SEAT]["vendor"],
                         "the positive control failed: the plain seat must "
                         "read the planted wall through this same call")
        self.assertEqual(seat_usability.UNAVAILABLE, rows[self.PI]["vendor"])
        self.assertEqual(SEAT, rows[self.PI]["vendor_family"])
        self.assertIn(CAUSE, rows[self.PI]["vendor_text"])

    def test_an_unverified_runtime_is_display_evidence_not_authority(self):
        """THE DISCRIMINATING CONTROL. The same roster row with the SAME family
        recorded, differing only in whether the launched process self-wrote it,
        answers NO_VENDOR — so the arm above measures the metadata and not the
        seat name, and a foreign launch mirror still cannot steer a wall."""
        self._roster(False)
        rows = self._fleet_rows()
        self.assertEqual(seat_usability.UNAVAILABLE, rows[SEAT]["vendor"],
                         "the positive control failed: the plain seat must "
                         "still read the planted wall")
        self.assertEqual(seat_usability.NO_VENDOR, rows[self.PI]["vendor"])
        self.assertEqual("", rows[self.PI]["vendor_text"])

    def test_the_land_loop_row_bills_the_wall_to_the_same_family(self):
        self._roster(True)
        look = landreq._availability_lookup()
        self.assertEqual(seat_usability.UNAVAILABLE, look(SEAT)["state"],
                         "the positive control failed: the plain seat must "
                         "read the planted wall through this lookup")
        self.assertEqual(seat_usability.UNAVAILABLE, look(self.PI)["state"])
        lr = {"id": "lr-rt-row", "state": "AWAITING_REVIEW",
              "owed_by": "reviewer", "reviewer": self.PI, "author": "seat-b",
              "lane": "a-lane", "stalled": False, "terminal": False,
              "dwell_s": 60, "dwell_known": True, "review_sha": None,
              "created_ts": SINCE}
        line = landreq._line(lr, avail=look)
        self.assertIn("VENDOR", line)
        self.assertIn(CAUSE, line)
        self.assertIn(SEAT, line)

    def test_supplying_an_empty_map_still_means_parse_the_names(self):
        """The seam a fixture needs, and the one a caller must pass to mean it:
        `{}` is "I looked and there is nothing", which parses names; None is "I
        did not look", which asks the roster. The control is the plain seat,
        which parses to its family either way."""
        self._roster(True)
        parsed = seat_usability.availability_map([self.PI, SEAT], runtimes={})
        self.assertEqual(seat_usability.UNAVAILABLE, parsed[SEAT]["state"])
        self.assertEqual(seat_usability.NO_VENDOR, parsed[self.PI]["state"])
        asked = seat_usability.availability_map([self.PI, SEAT])
        self.assertEqual(seat_usability.UNAVAILABLE, asked[self.PI]["state"])



class AVendorReadThatRaisedIsUnknownTest(unittest.TestCase):
    """A READ THAT DID NOT ANSWER IS UNKNOWN, NEVER THE EMPTY CELL. `_avail`,
    `_avail_cells` and `availability_for_roster` each turned any exception
    into {}, and {} is what a seat with no vendor publishes: a walled seat's
    word vanished from every surface, with nothing recorded. Each raise now
    reaches every row of `helm chat seats`, the roster payload and the
    presence payload as UNKNOWN naming the exception, and leaves a breadcrumb.

    THE CONTROL IS THE ANSWERED READ OF THE SAME WORLD: the walled seat reads
    UNAVAILABLE with its cause, and the native seat publishes no cell at all,
    because the question does not apply to it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-avail-raise-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm-home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat")})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.path.dirname(proxywatch._state_path()), exist_ok=True)
        os.makedirs(os.path.dirname(seats.roster_path()), exist_ok=True)
        with open(seats.roster_path(), "w", encoding="utf-8") as f:
            json.dump({name: {"session": c * 36, "home_room": "helm",
                              "cwd": self.tmp}
                       for name, c in ((SEAT, "k"), (NON_PROXY_SEAT, "a"))}, f)
        with open(proxywatch._state_path(), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "fingerprint": "planted",
                       "upstream": {SEAT: _record(CAUSE, True)}}, f)

    def _surfaces(self):
        """{surface: {seat: row}} — the roster payload, the presence payload
        and the `helm chat seats` line, each from a render of its own."""
        buf = StringIO()
        with redirect_stdout(buf):
            seats_report.render_roster("helm", True)
        lines = {line.split()[1]: line for line in buf.getvalue().splitlines()
                 if len(line.split()) > 1}
        return ({s["seat"]: s for s in seats_report.roster_report("helm")[
                    "seats"]},
                {s["seat"]: s for s in seats_report.presence_report()},
                lines)

    def _crumbs(self, where):
        from helm import record
        return [c.get("exc") for c in record.swallows()
                if c.get("where") == where]

    def _every_row_reads_unknown(self, why):
        report, presence, lines = self._surfaces()
        for seat_name in (SEAT, NON_PROXY_SEAT):
            for payload in (report, presence):
                row = payload[seat_name]
                self.assertEqual(
                    (row.get("availability"), row.get("availability_walled")),
                    (seat_usability.UNKNOWN, False), row)
                self.assertIn(why, row.get("availability_text") or "")
                self.assertEqual(row.get("availability_mark"),
                                 "? " + row["availability_text"])
            self.assertIn("? UNKNOWN", lines.get(seat_name, ""))
            self.assertIn(why, lines.get(seat_name, ""))

    def test_the_answered_read_names_the_wall_and_the_native_seat_is_empty(self):
        report, presence, lines = self._surfaces()
        self.assertEqual([report[SEAT].get("availability"),
                          presence[SEAT].get("availability")],
                         [seat_usability.UNAVAILABLE] * 2)
        self.assertIn(CAUSE, report[SEAT].get("availability_text"))
        self.assertIn(CAUSE, presence[SEAT].get("availability_text"))
        self.assertEqual(
            [[k for k in payload[NON_PROXY_SEAT] if "availab" in k]
             for payload in (report, presence)], [[], []],
            "an answered read publishes no cell for a seat with no vendor")
        self.assertIn(CAUSE, lines[SEAT])
        self.assertIn(NON_PROXY_SEAT, lines)
        self.assertNotIn("UNKNOWN", lines[NON_PROXY_SEAT])

    def test_a_roster_read_that_raises_is_unknown_on_every_surface(self):  # noqa: VACUOUS_ASSERTION — _every_row_reads_unknown asserts the UNKNOWN word, the reason and the mark on every row of three surfaces, and the breadcrumb list is asserted exactly
        for exc in (RuntimeError("proxywatch state moved"),
                    ImportError("seat_usability")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(seat_usability, "availability_map",
                                      side_effect=exc):
                self._every_row_reads_unknown(
                    "the vendor availability read raised %s"
                    % type(exc).__name__)
        self.assertEqual(self._crumbs("seats_report._avail"),
                         ["ImportError"] * 3 + ["RuntimeError"] * 3,
                         "one breadcrumb per failed read, newest first")

    def test_a_cell_that_raises_is_unknown_for_its_row(self):
        with mock.patch.object(seat_usability, "availability_cells",
                               side_effect=KeyError("text")):
            self._every_row_reads_unknown(
                "the vendor availability cell raised KeyError")
        self.assertEqual(set(self._crumbs("seats_report._avail_cells")),
                         {"KeyError"})

    def test_the_producer_lets_the_raise_reach_the_fail_open(self):
        """The raise is not folded one hop in: availability_for_roster
        propagates it, so the ONE fail-open that turns it into UNKNOWN is the
        one that runs. The control is the same call answering."""
        rows = {SEAT: {"runtime": None}}
        self.assertEqual(
            seat_usability.availability_for_roster(rows)[SEAT]["state"],
            seat_usability.UNAVAILABLE)
        with mock.patch.object(seat_usability, "availability_map",
                               side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                seat_usability.availability_for_roster(rows)

    def test_the_report_builds_the_cells_the_predicate_would(self):
        """seats_report builds its UNKNOWN cells itself, because
        seat_usability may be what failed to import; they must be the bytes
        the predicate's own renderer makes of the same UNKNOWN record."""
        why = "the vendor availability read raised OSError"
        cells = seats_report._avail_unknown_cells(why)
        self.assertEqual(cells["availability"], seat_usability.UNKNOWN)
        self.assertIn(why, cells["availability_text"])
        self.assertEqual(
            cells,
            seat_usability.availability_cells(seat_usability._availability(
                seat_usability.UNKNOWN, None, why=why)))


if __name__ == "__main__":
    unittest.main()
